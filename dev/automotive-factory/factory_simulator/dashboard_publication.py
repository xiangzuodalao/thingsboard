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
import os
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
PLAN_TTL = timedelta(minutes=30)
_SERVER_MANAGED_FIELDS = frozenset({"id", "version", "createdTime", "tenantId"})
_SENSITIVE_TERMS = ("token", "password", "authorization", "credential", "cookie")


class DashboardPublicationError(RuntimeError):
    """Raised when a managed dashboard plan cannot safely be created or applied."""


@dataclass(frozen=True)
class DashboardPlanResult:
    sha256: str
    desired_body_sha256: str
    desired_dashboard: dict[str, Any]
    path: Path


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _dashboard_body(dashboard: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in dashboard.items()
        if key not in _SERVER_MANAGED_FIELDS
    }


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


def _secret_free(payload: dict[str, Any], *, context: str) -> None:
    serialized = _canonical_bytes(payload).lower()
    if any(term.encode("ascii") in serialized for term in _SENSITIVE_TERMS):
        raise DashboardPublicationError(f"{context} contains a credential-like value")


def _write_owner_only(path: Path, payload: dict[str, Any]) -> None:
    _secret_free(payload, context=path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        temporary_path = Path(stream.name)
        try:
            os.chmod(temporary_path, 0o600)
            stream.write(_canonical_bytes(payload))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    temporary_path.replace(path)
    os.chmod(path, 0o600)


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise DashboardPublicationError("saved dashboard plan must have mode 0600")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DashboardPublicationError("saved dashboard plan is unavailable") from exc
    except json.JSONDecodeError as exc:
        raise DashboardPublicationError("saved dashboard plan is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DashboardPublicationError("saved dashboard plan is not a JSON object")
    _secret_free(payload, context=path.name)
    return payload


def _plan_sha256(plan: dict[str, Any]) -> str:
    canonical_plan = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return _sha256(canonical_plan)


def _plan_path(config: AppConfig) -> Path:
    return config.runtime_dir / PLAN_FILENAME


def _receipt_path(config: AppConfig) -> Path:
    return config.runtime_dir / RECEIPT_FILENAME


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
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> DashboardPlanResult:
    """Read the dashboard state and write a 30-minute, tenant-bound plan."""

    client = api or ThingsBoardApi(config)
    created_at = _now_utc(now)
    tenant_id = _require_expected_tenant(client)
    device_ids = _resolve_device_ids(client, config)
    existing = client.find_dashboard(DASHBOARD_TITLE)
    if existing is not None and not isinstance(existing, dict):
        raise DashboardPublicationError("existing dashboard response was not a JSON object")
    desired_dashboard = _dashboard_payload(
        _dashboard_configuration(client, device_ids), existing
    )
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
    actor_name = actor or getpass.getuser()
    if not actor_name:
        raise DashboardPublicationError("dashboard plan actor must not be empty")
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
    path = _plan_path(config)
    _write_owner_only(path, plan)
    return DashboardPlanResult(
        sha256=plan_sha256,
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
        if api.find_dashboard(DASHBOARD_TITLE) is not None:
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


def _receipt(
    *,
    plan: dict[str, Any],
    response: dict[str, Any],
    saved: bool,
    recovered_response_loss: bool,
    applied_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "applied_at": applied_at.isoformat(),
        "tenant_id": plan["tenant_id"],
        "actor": plan["actor"],
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
    api: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one exact, later-confirmed plan after checking every snapshot field."""

    if not plan_sha256 or plan_sha256 != confirmed_sha256:
        raise DashboardPublicationError("dashboard apply requires an identical later confirmation hash")
    plan = _read_plan(_plan_path(config))
    actual_plan_sha256 = _plan_sha256(plan)
    if plan.get("plan_sha256") != actual_plan_sha256 or plan_sha256 != actual_plan_sha256:
        raise DashboardPublicationError("dashboard apply plan hash does not match the saved plan")
    applied_at = _now_utc(now)
    if applied_at > _plan_expiry(plan):
        raise DashboardPublicationError("saved dashboard plan has expired")
    client = api or ThingsBoardApi(config)
    if _require_expected_tenant(client) != plan.get("tenant_id"):
        raise DashboardPublicationError("tenant drift detected before apply")
    desired = _validated_desired_dashboard(plan)
    current = _verify_snapshot(client, plan)
    if current is not None and _dashboard_body_sha256(current) == plan["desired_body_sha256"]:
        receipt = _receipt(
            plan=plan,
            response=current,
            saved=False,
            recovered_response_loss=False,
            applied_at=applied_at,
        )
        _write_owner_only(_receipt_path(config), receipt)
        return receipt

    try:
        response = client.save_dashboard(copy.deepcopy(desired))
    except (RequestException, ThingsBoardApiError) as exc:
        snapshot = plan["snapshot"]
        dashboard_id = snapshot.get("dashboard_id") if isinstance(snapshot, dict) else None
        if not isinstance(dashboard_id, str):
            raise DashboardPublicationError("dashboard save response was lost") from exc
        response = _exact_dashboard(client, dashboard_id)
        if _dashboard_body_sha256(response) != plan["desired_body_sha256"]:
            raise DashboardPublicationError("dashboard save response was lost and readback differs") from exc
        recovered = True
    else:
        if not isinstance(response, dict):
            raise DashboardPublicationError("dashboard save response was not a JSON object")
        recovered = False
    receipt = _receipt(
        plan=plan,
        response=response,
        saved=True,
        recovered_response_loss=recovered,
        applied_at=applied_at,
    )
    _write_owner_only(_receipt_path(config), receipt)
    return receipt
