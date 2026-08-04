#
# Copyright © 2016-2026 The Thingsboard Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Confirmed, tenant-bound publication for the predictive-maintenance dashboard."""

from __future__ import annotations

import copy
import getpass
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from requests import RequestException

from .api import ThingsBoardApi, ThingsBoardApiError
from .config import AppConfig
from .dashboard import (
    DASHBOARD_TITLE,
    _dashboard_configuration,
    _dashboard_payload,
    _resolve_device_ids,
)


PLAN_FILENAME = "pdm-dashboard-plan.json"
RECEIPT_FILENAME = "pdm-dashboard-receipt.json"
_CONSUMPTION_NAMESPACE = ".pdm-dashboard-consumption"
_MAX_PLAN_BYTES = 8 * 1024 * 1024
PLAN_TTL = timedelta(minutes=30)
_SERVER_MANAGED_FIELDS = frozenset(
    {"id", "version", "createdTime", "tenantId", "name"}
)
_SENSITIVE_KEY_TERMS = (
    "token",
    "password",
    "authorization",
    "credential",
    "cookie",
    "secret",
    "apikey",
)
_MANAGED_CREDENTIAL_ENV_NAMES = ("TB_PDM_DASHBOARD_BEARER_TOKEN",)


class DashboardPublicationError(RuntimeError):
    """Raised when a managed dashboard plan cannot safely be created or applied."""


@dataclass(frozen=True)
class DashboardPlanResult:
    sha256: str
    tenant_id: str
    current_dashboard_id: str | None
    current_version: int | None
    current_body_sha256: str | None
    desired_body_sha256: str
    desired_dashboard: dict[str, Any]
    path: Path


def _managed_credentials() -> tuple[str, ...]:
    return tuple(
        credential for name in _MANAGED_CREDENTIAL_ENV_NAMES if (credential := os.environ.get(name))
    )


def _validate_artifact_tree(payload: Any, *, context: str) -> None:
    managed_credentials = _managed_credentials()
    pending: list[Any] = [payload]
    visited_containers: set[int] = set()
    while pending:
        value = pending.pop()
        value_type = type(value)
        if value_type is dict:
            identity = id(value)
            if identity in visited_containers:
                continue
            visited_containers.add(identity)
            for key, nested in value.items():
                if type(key) is not str:
                    raise DashboardPublicationError(f"{context} is not canonical JSON")
                if any(credential in key for credential in managed_credentials):
                    raise DashboardPublicationError(f"{context} contains a credential-like value")
                normalized = re.sub(r"[^a-z]", "", key.lower())
                if any(term in normalized for term in _SENSITIVE_KEY_TERMS):
                    raise DashboardPublicationError(f"{context} contains a credential-like value")
                pending.append(nested)
        elif value_type is list:
            identity = id(value)
            if identity in visited_containers:
                continue
            visited_containers.add(identity)
            pending.extend(value)
        elif value_type is str:
            if (
                any(credential in value for credential in managed_credentials)
                or re.search(r"(?i)\bbearer\s+\S+", value)
                or re.fullmatch(
                    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
                    value,
                )
            ):
                raise DashboardPublicationError(f"{context} contains a credential-like value")
        elif value is None or value_type is bool or value_type is int:
            continue
        elif value_type is float:
            if not math.isfinite(value):
                raise DashboardPublicationError(f"{context} is not canonical JSON")
        else:
            raise DashboardPublicationError(f"{context} is not canonical JSON")


def _canonical_bytes(payload: dict[str, Any], *, context: str = "publication artifact") -> bytes:
    _validate_artifact_tree(payload, context=context)
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise DashboardPublicationError("publication artifact is not canonical JSON") from None


def _sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _dashboard_body(dashboard: dict[str, Any]) -> dict[str, Any]:
    body = {
        key: copy.deepcopy(value)
        for key, value in dashboard.items()
        if key not in _SERVER_MANAGED_FIELDS
    }
    # ThingsBoard derives ``name`` from ``title`` and does not persist an empty
    # resources collection. Its save/readback response therefore returns null
    # for the explicit [] used by this managed dashboard payload.
    if "resources" in body and body["resources"] is None:
        body["resources"] = []
    return body


def _dashboard_body_sha256(dashboard: dict[str, Any]) -> str:
    return _sha256(_dashboard_body(dashboard))


def _canonical_uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise DashboardPublicationError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise DashboardPublicationError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise DashboardPublicationError(f"{field} must be a canonical UUID")
    return value


def _expected_tenant_id() -> str:
    return _canonical_uuid(
        os.environ.get("TB_PDM_EXPECTED_TENANT_ID"),
        field="TB_PDM_EXPECTED_TENANT_ID",
    )


def _current_tenant_id(api: Any) -> str:
    direct = getattr(api, "get_current_tenant_id", None)
    if callable(direct):
        return _canonical_uuid(direct(), field="authenticated tenant ID")

    response = api.request("GET", "/api/auth/user")
    payload = response.json()
    try:
        tenant_id = payload["tenantId"]["id"]
    except (KeyError, TypeError) as exc:
        raise DashboardPublicationError(
            "authenticated user response did not include a tenant UUID"
        ) from exc
    return _canonical_uuid(tenant_id, field="authenticated tenant ID")


def _require_expected_tenant(api: Any) -> str:
    expected = _expected_tenant_id()
    actual = _current_tenant_id(api)
    if actual != expected:
        raise DashboardPublicationError("authenticated tenant drifted from the expected tenant")
    return actual


def _dashboard_id(dashboard: dict[str, Any]) -> str:
    try:
        value = dashboard["id"]["id"]
    except (KeyError, TypeError) as exc:
        raise DashboardPublicationError("dashboard response did not include an ID") from exc
    if not isinstance(value, str) or not value:
        raise DashboardPublicationError("dashboard response did not include an ID")
    return value


def _dashboard_version(dashboard: dict[str, Any]) -> int:
    value = dashboard.get("version")
    if not isinstance(value, int) or isinstance(value, bool):
        raise DashboardPublicationError("dashboard response did not include an integer version")
    return value


def _exact_dashboard(api: Any, dashboard_id: str) -> dict[str, Any]:
    direct = getattr(api, "get_dashboard", None)
    if callable(direct):
        dashboard = direct(dashboard_id)
    else:
        dashboard = api.request("GET", f"/api/dashboard/{dashboard_id}").json()
    if not isinstance(dashboard, dict):
        raise DashboardPublicationError("dashboard readback was not a JSON object")
    return dashboard


def _actor(value: str | None, *, field: str, default: str | None = None) -> str:
    actor = value if value is not None else default
    if (
        not isinstance(actor, str)
        or not actor
        or actor != actor.strip()
        or len(actor) > 128
        or not actor.isprintable()
    ):
        raise DashboardPublicationError(f"{field} must be a non-empty operator name")
    if any(credential in actor for credential in _managed_credentials()):
        raise DashboardPublicationError(f"{field} must not match a managed credential")
    return actor


def _safe_artifact_path(
    path: Path,
    *,
    must_exist: bool,
    context: str,
) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts or candidate.name in {"", ".", ".."}:
        raise DashboardPublicationError(f"{context} must use a safe artifact path")
    try:
        parent = candidate.parent
        resolved_parent = parent.resolve(strict=True)
        if resolved_parent != parent:
            raise DashboardPublicationError(f"{context} must use a safe artifact path")
        parent_stat = parent.stat()
        parent_mode = stat.S_IMODE(parent_stat.st_mode)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or parent_mode & 0o022
            or parent_mode & 0o700 != 0o700
        ):
            raise DashboardPublicationError(f"{context} must use a safe artifact path")
    except OSError as exc:
        raise DashboardPublicationError(f"{context} must use a safe artifact path") from exc
    try:
        target_stat = candidate.lstat()
    except FileNotFoundError:
        if must_exist:
            raise DashboardPublicationError(f"{context} is unavailable") from None
        return candidate
    except OSError as exc:
        raise DashboardPublicationError(f"{context} must use a safe artifact path") from exc

    if stat.S_ISLNK(target_stat.st_mode):
        raise DashboardPublicationError(f"{context} must use a safe artifact path")
    if not must_exist:
        raise DashboardPublicationError(f"{context} already exists")
    if (
        not stat.S_ISREG(target_stat.st_mode)
        or target_stat.st_uid != os.geteuid()
        or stat.S_IMODE(target_stat.st_mode) != 0o600
        or target_stat.st_nlink != 1
    ):
        raise DashboardPublicationError(f"{context} must use a safe artifact path")
    return candidate


def _write_owner_only(
    path: Path,
    payload: dict[str, Any],
    *,
    context: str,
    exists_error: str | None = None,
    max_bytes: int | None = None,
) -> None:
    serialized = _canonical_bytes(payload, context=context) + b"\n"
    if max_bytes is not None and len(serialized) > max_bytes:
        raise DashboardPublicationError(f"{context} exceeds the size limit")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            os.chmod(temporary_path, 0o600)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise DashboardPublicationError(f"{context} could not be written") from exc
    try:
        assert temporary_path is not None
        os.link(temporary_path, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        raise DashboardPublicationError(exists_error or f"{context} already exists") from None
    except OSError as exc:
        raise DashboardPublicationError(f"{context} could not be written") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_plan(path: Path) -> dict[str, Any]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError:
        raise DashboardPublicationError(
            "saved dashboard plan is not a safe dashboard plan file"
        ) from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            raise DashboardPublicationError(
                "saved dashboard plan is not a safe dashboard plan file"
            )
        if before.st_size > _MAX_PLAN_BYTES:
            raise DashboardPublicationError("saved dashboard plan exceeds the size limit")
        chunks: list[bytes] = []
        remaining = _MAX_PLAN_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) > _MAX_PLAN_BYTES
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != len(raw)
            or not stat.S_ISREG(after.st_mode)
            or after.st_uid != os.geteuid()
            or stat.S_IMODE(after.st_mode) != 0o600
            or after.st_nlink != 1
        ):
            raise DashboardPublicationError("saved dashboard plan changed while it was being read")
    except OSError:
        raise DashboardPublicationError("saved dashboard plan is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise DashboardPublicationError("saved dashboard plan is not valid JSON") from exc
    except json.JSONDecodeError as exc:
        raise DashboardPublicationError("saved dashboard plan is not valid JSON") from exc
    except RecursionError:
        raise DashboardPublicationError("saved dashboard plan is not valid JSON") from None
    if not isinstance(payload, dict):
        raise DashboardPublicationError("saved dashboard plan is not a JSON object")
    if raw != _canonical_bytes(payload, context="saved dashboard plan") + b"\n":
        raise DashboardPublicationError("saved dashboard plan is not canonical JSON")
    return payload


def _plan_sha256(plan: dict[str, Any]) -> str:
    canonical_plan = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return _sha256(canonical_plan)


def _plan_path(config: AppConfig) -> Path:
    return config.runtime_dir / PLAN_FILENAME


def _receipt_path(config: AppConfig) -> Path:
    return config.runtime_dir / RECEIPT_FILENAME


def _ensure_owner_directory(
    path: Path,
    *,
    context: str,
    exact_mode: bool,
) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts or candidate.name in {"", ".", ".."}:
        raise DashboardPublicationError(f"{context} is not owner-controlled")
    try:
        parent = candidate.parent
        if parent.resolve(strict=True) != parent:
            raise DashboardPublicationError(f"{context} is not owner-controlled")
        parent_stat = parent.lstat()
        parent_mode = stat.S_IMODE(parent_stat.st_mode)
        if (
            stat.S_ISLNK(parent_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or parent_mode & 0o022
            or parent_mode & 0o700 != 0o700
        ):
            raise DashboardPublicationError(f"{context} is not owner-controlled")
        try:
            os.mkdir(candidate, 0o700)
        except FileExistsError:
            pass
        candidate_stat = candidate.lstat()
        candidate_mode = stat.S_IMODE(candidate_stat.st_mode)
        if (
            stat.S_ISLNK(candidate_stat.st_mode)
            or not stat.S_ISDIR(candidate_stat.st_mode)
            or candidate_stat.st_uid != os.geteuid()
            or candidate.resolve(strict=True) != candidate
            or (exact_mode and candidate_mode != 0o700)
            or (not exact_mode and (candidate_mode & 0o022 or candidate_mode & 0o700 != 0o700))
        ):
            raise DashboardPublicationError(f"{context} is not owner-controlled")
    except DashboardPublicationError:
        raise
    except OSError:
        raise DashboardPublicationError(f"{context} is not owner-controlled") from None
    return candidate


def _consumption_marker_path(config: AppConfig, plan_sha256: str) -> Path:
    runtime_dir = _ensure_owner_directory(
        config.runtime_dir,
        context="dashboard runtime directory",
        exact_mode=False,
    )
    namespace = _ensure_owner_directory(
        runtime_dir / _CONSUMPTION_NAMESPACE,
        context="dashboard consumption namespace",
        exact_mode=True,
    )
    return namespace / f"{plan_sha256}.json"


def _managed_api(config: AppConfig) -> ThingsBoardApi:
    bearer_token = os.environ.get("TB_PDM_DASHBOARD_BEARER_TOKEN")
    if not bearer_token:
        raise DashboardPublicationError(
            "managed dashboard publication requires a preauthenticated bearer credential"
        )
    return ThingsBoardApi(config, bearer_token=bearer_token)


def _dashboard_candidates(api: Any) -> list[dict[str, Any]]:
    direct = getattr(api, "find_dashboards", None)
    if callable(direct):
        candidates = direct(DASHBOARD_TITLE)
    else:
        dashboard = api.find_dashboard(DASHBOARD_TITLE)
        candidates = [] if dashboard is None else [dashboard]
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        raise DashboardPublicationError("dashboard discovery returned an invalid response")
    return candidates


def _now_utc(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise DashboardPublicationError("publication time must include a timezone")
    return current.astimezone(timezone.utc)


def create_dashboard_plan(
    config: AppConfig,
    *,
    api: Any | None = None,
    actor: str | None = None,
    output_path: Path | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> DashboardPlanResult:
    """Read the dashboard state and write a 30-minute, tenant-bound plan."""

    if output_path is None:
        config.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = _plan_path(config)
    else:
        path = Path(output_path)
    path = _safe_artifact_path(path, must_exist=False, context="dashboard plan output")
    actor_name = _actor(actor, field="dashboard plan actor", default=getpass.getuser())
    client = api or _managed_api(config)
    created_at = _now_utc(now)
    tenant_id = _require_expected_tenant(client)
    device_ids = _resolve_device_ids(client, config)
    candidates = _dashboard_candidates(client)
    if len(candidates) > 1:
        raise DashboardPublicationError("dashboard discovery is ambiguous")
    existing = candidates[0] if candidates else None
    try:
        desired_dashboard = _dashboard_payload(
            _dashboard_configuration(client, device_ids),
            existing,
        )
    except RecursionError:
        raise DashboardPublicationError("dashboard provider data is too deeply nested") from None
    desired_body_sha256 = _dashboard_body_sha256(desired_dashboard)
    snapshot: dict[str, Any]
    if existing is None:
        snapshot = {"dashboard_id": None, "version": None, "body_sha256": None}
    else:
        snapshot = {
            "dashboard_id": _dashboard_id(existing),
            "version": _dashboard_version(existing),
            "body_sha256": _dashboard_body_sha256(existing),
        }
    correlation = _canonical_uuid(correlation_id or str(uuid.uuid4()), field="correlation ID")
    plan = {
        "schema_version": 1,
        "created_at": created_at.isoformat(),
        "expires_at": (created_at + PLAN_TTL).isoformat(),
        "tenant_id": tenant_id,
        "actor": actor_name,
        "correlation_id": correlation,
        "snapshot": snapshot,
        "desired_dashboard": desired_dashboard,
        "desired_body_sha256": desired_body_sha256,
    }
    plan_sha256 = _plan_sha256(plan)
    plan["plan_sha256"] = plan_sha256
    _write_owner_only(
        path,
        plan,
        context="dashboard plan output",
        max_bytes=_MAX_PLAN_BYTES,
    )
    return DashboardPlanResult(
        sha256=plan_sha256,
        tenant_id=tenant_id,
        current_dashboard_id=snapshot["dashboard_id"],
        current_version=snapshot["version"],
        current_body_sha256=snapshot["body_sha256"],
        desired_body_sha256=desired_body_sha256,
        desired_dashboard=copy.deepcopy(desired_dashboard),
        path=path,
    )


def _plan_expiry(plan: dict[str, Any]) -> datetime:
    value = plan.get("expires_at")
    if not isinstance(value, str):
        raise DashboardPublicationError("saved dashboard plan has no expiry")
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DashboardPublicationError("saved dashboard plan has an invalid expiry") from exc
    if expiry.tzinfo is None:
        raise DashboardPublicationError("saved dashboard plan has an invalid expiry")
    return expiry.astimezone(timezone.utc)


def _verify_snapshot(api: Any, plan: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = plan.get("snapshot")
    if not isinstance(snapshot, dict):
        raise DashboardPublicationError("saved dashboard plan has no snapshot")
    dashboard_id = snapshot.get("dashboard_id")
    if dashboard_id is None:
        if _dashboard_candidates(api):
            raise DashboardPublicationError("dashboard drift detected before apply")
        return None
    if not isinstance(dashboard_id, str):
        raise DashboardPublicationError("saved dashboard plan has an invalid dashboard ID")
    current = _exact_dashboard(api, dashboard_id)
    if (
        _dashboard_id(current) != dashboard_id
        or _dashboard_version(current) != snapshot.get("version")
        or _dashboard_body_sha256(current) != snapshot.get("body_sha256")
    ):
        raise DashboardPublicationError("dashboard drift detected before apply")
    return current


def _validated_desired_dashboard(plan: dict[str, Any]) -> dict[str, Any]:
    desired = plan.get("desired_dashboard")
    if not isinstance(desired, dict):
        raise DashboardPublicationError("saved dashboard plan has no desired dashboard")
    expected_hash = plan.get("desired_body_sha256")
    if not isinstance(expected_hash, str) or _dashboard_body_sha256(desired) != expected_hash:
        raise DashboardPublicationError("saved dashboard plan desired body hash does not match")
    return desired


def _verify_saved_dashboard(
    response: dict[str, Any],
    *,
    desired_body_sha256: str,
    expected_dashboard_id: str | None,
) -> None:
    response_id = _dashboard_id(response)
    if expected_dashboard_id is not None and response_id != expected_dashboard_id:
        raise DashboardPublicationError(
            "saved dashboard response does not match the planned dashboard ID"
        )
    if _dashboard_body_sha256(response) != desired_body_sha256:
        raise DashboardPublicationError("saved dashboard response does not match the desired body")


def _receipt(
    *,
    plan: dict[str, Any],
    actor: str,
    response: dict[str, Any],
    saved: bool,
    recovered_response_loss: bool,
    applied_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "applied_at": applied_at.isoformat(),
        "tenant_id": plan["tenant_id"],
        "actor": actor,
        "plan_actor": plan["actor"],
        "correlation_id": plan["correlation_id"],
        "plan_sha256": plan["plan_sha256"],
        "dashboard_id": _dashboard_id(response),
        "version": _dashboard_version(response),
        "response_sha256": _sha256(response),
        "saved": saved,
        "recovered_response_loss": recovered_response_loss,
    }


def apply_dashboard_plan(
    config: AppConfig,
    *,
    plan_sha256: str,
    confirmed_sha256: str,
    plan_path: Path | None = None,
    receipt_path: Path | None = None,
    actor: str | None = None,
    api: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one exact, later-confirmed plan after checking every snapshot field."""

    sha256_pattern = re.compile(r"^[0-9a-f]{64}$")
    if (
        not isinstance(plan_sha256, str)
        or not isinstance(confirmed_sha256, str)
        or not sha256_pattern.fullmatch(plan_sha256)
        or not sha256_pattern.fullmatch(confirmed_sha256)
    ):
        raise DashboardPublicationError(
            "dashboard apply hashes must be exact lowercase SHA-256 values"
        )
    if plan_sha256 != confirmed_sha256:
        raise DashboardPublicationError(
            "dashboard apply requires an identical later confirmation hash"
        )

    selected_plan_path = Path(plan_path) if plan_path is not None else _plan_path(config)
    if receipt_path is None:
        config.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        selected_receipt_path = _receipt_path(config)
    else:
        selected_receipt_path = Path(receipt_path)
    selected_plan_path = _safe_artifact_path(
        selected_plan_path,
        must_exist=True,
        context="dashboard plan",
    )
    if (
        not selected_receipt_path.is_absolute()
        or ".." in selected_receipt_path.parts
        or selected_receipt_path.name in {"", ".", ".."}
    ):
        raise DashboardPublicationError(
            "dashboard plan and receipt must use distinct safe artifact paths"
        )
    try:
        receipt_parent = selected_receipt_path.parent.resolve(strict=True)
    except OSError as exc:
        raise DashboardPublicationError("dashboard receipt must use a safe artifact path") from exc
    if receipt_parent / selected_receipt_path.name == selected_plan_path:
        raise DashboardPublicationError(
            "dashboard plan and receipt must use distinct safe artifact paths"
        )
    selected_receipt_path = _safe_artifact_path(
        selected_receipt_path,
        must_exist=False,
        context="dashboard receipt",
    )

    plan = _read_plan(selected_plan_path)
    actual_plan_sha256 = _plan_sha256(plan)
    if plan.get("plan_sha256") != actual_plan_sha256 or plan_sha256 != actual_plan_sha256:
        raise DashboardPublicationError("dashboard apply plan hash does not match the saved plan")
    if not sha256_pattern.fullmatch(actual_plan_sha256):
        raise DashboardPublicationError("saved dashboard plan hash is invalid")
    consumed_path = _consumption_marker_path(config, actual_plan_sha256)
    if receipt_parent / selected_receipt_path.name == consumed_path:
        raise DashboardPublicationError(
            "dashboard plan, receipt, and consumption marker must use distinct paths"
        )
    try:
        consumed_path.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise DashboardPublicationError(
            "saved dashboard plan consumption state is unavailable"
        ) from None
    else:
        raise DashboardPublicationError("saved dashboard plan has already been consumed")
    applied_at = _now_utc(now)
    if applied_at >= _plan_expiry(plan):
        raise DashboardPublicationError("saved dashboard plan has expired")
    applying_actor = _actor(
        actor,
        field="dashboard apply actor",
        default=plan.get("actor") if isinstance(plan.get("actor"), str) else None,
    )
    client = api or _managed_api(config)
    if _require_expected_tenant(client) != plan.get("tenant_id"):
        raise DashboardPublicationError("tenant drift detected before apply")
    desired = _validated_desired_dashboard(plan)
    current = _verify_snapshot(client, plan)
    _write_owner_only(
        consumed_path,
        {
            "schema_version": 1,
            "consumed_at": applied_at.isoformat(),
            "plan_sha256": actual_plan_sha256,
            "actor": applying_actor,
        },
        context="dashboard plan consumption marker",
        exists_error="saved dashboard plan has already been consumed",
    )
    if current is not None and _dashboard_body_sha256(current) == plan["desired_body_sha256"]:
        receipt = _receipt(
            plan=plan,
            actor=applying_actor,
            response=current,
            saved=False,
            recovered_response_loss=False,
            applied_at=applied_at,
        )
        _write_owner_only(
            selected_receipt_path,
            receipt,
            context="dashboard receipt",
        )
        return receipt

    try:
        response = client.save_dashboard(copy.deepcopy(desired))
    except (RequestException, ThingsBoardApiError):
        snapshot = plan["snapshot"]
        dashboard_id = snapshot.get("dashboard_id") if isinstance(snapshot, dict) else None
        if isinstance(dashboard_id, str):
            response = _exact_dashboard(client, dashboard_id)
        else:
            candidates = _dashboard_candidates(client)
            if len(candidates) != 1:
                raise DashboardPublicationError(
                    "dashboard save response was lost and discovery is ambiguous"
                ) from None
            response = candidates[0]
        try:
            _verify_saved_dashboard(
                response,
                desired_body_sha256=plan["desired_body_sha256"],
                expected_dashboard_id=dashboard_id if isinstance(dashboard_id, str) else None,
            )
        except DashboardPublicationError:
            raise DashboardPublicationError(
                "dashboard save response was lost and readback differs"
            ) from None
        recovered = True
    else:
        if not isinstance(response, dict):
            raise DashboardPublicationError("dashboard save response was not a JSON object")
        recovered = False
    snapshot = plan["snapshot"]
    expected_dashboard_id = snapshot.get("dashboard_id") if isinstance(snapshot, dict) else None
    _verify_saved_dashboard(
        response,
        desired_body_sha256=plan["desired_body_sha256"],
        expected_dashboard_id=(
            expected_dashboard_id if isinstance(expected_dashboard_id, str) else None
        ),
    )
    receipt = _receipt(
        plan=plan,
        actor=applying_actor,
        response=response,
        saved=True,
        recovered_response_loss=recovered,
        applied_at=applied_at,
    )
    _write_owner_only(
        selected_receipt_path,
        receipt,
        context="dashboard receipt",
    )
    return receipt
