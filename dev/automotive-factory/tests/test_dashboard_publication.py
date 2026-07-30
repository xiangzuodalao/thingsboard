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

from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from requests import ConnectionError

from factory_simulator.api import ThingsBoardApi, ThingsBoardApiError
from factory_simulator.cli import _cmd_dashboard, main
from factory_simulator.config import load_config
from factory_simulator.dashboard import DASHBOARD_TITLE
from factory_simulator.dashboard_publication import (
    DashboardPublicationError,
    apply_dashboard_plan,
    create_dashboard_plan,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yml"
TENANT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)


class RecordingApi:
    """A complete in-memory ThingsBoard boundary for publication tests."""

    def __init__(self, dashboard: dict[str, object] | None = None) -> None:
        self.dashboard = copy.deepcopy(dashboard)
        self.calls: list[str] = []
        self.raise_response_loss = False
        self.discovered_dashboards: list[dict[str, object]] | None = None
        self.discovered_after_save: list[dict[str, object]] | None = None

    def get_current_tenant_id(self) -> str:
        self.calls.append("GET")
        return TENANT_ID

    def get_device(self, name: str) -> dict[str, object]:
        self.calls.append("GET")
        return {"id": {"id": f"device-{name}"}}

    def get_widget_type(self, fqn: str) -> dict[str, object]:
        self.calls.append("GET")
        widget_type, default_config = {
            "system.cards.entities_table": ("latest", {"settings": {}}),
            "system.time_series_chart": ("timeseries", {"settings": {}}),
            "system.alarm_widgets.alarms_table": (
                "alarm",
                {"settings": {}, "alarmSource": {"dataKeys": []}},
            ),
        }[fqn]
        return {
            "descriptor": {
                "type": widget_type,
                "defaultConfig": default_config,
            }
        }

    def find_dashboard(self, title: str) -> dict[str, object] | None:
        self.calls.append("GET")
        if self.dashboard is None or self.dashboard.get("title") != title:
            return None
        return copy.deepcopy(self.dashboard)

    def find_dashboards(self, title: str) -> list[dict[str, object]]:
        self.calls.append("GET")
        if "POST" in self.calls and self.discovered_after_save is not None:
            return copy.deepcopy(self.discovered_after_save)
        if self.discovered_dashboards is not None:
            return copy.deepcopy(self.discovered_dashboards)
        if self.dashboard is None or self.dashboard.get("title") != title:
            return []
        return [copy.deepcopy(self.dashboard)]

    def get_dashboard(self, dashboard_id: str) -> dict[str, object]:
        self.calls.append("GET")
        if self.dashboard is None or self.dashboard["id"]["id"] != dashboard_id:  # type: ignore[index]
            raise DashboardPublicationError("dashboard disappeared")
        return copy.deepcopy(self.dashboard)

    def save_dashboard(self, dashboard: dict[str, object]) -> dict[str, object]:
        self.calls.append("POST")
        saved = copy.deepcopy(dashboard)
        if "id" not in saved:
            saved["id"] = {"id": "dashboard-1"}
            saved["version"] = 1
        else:
            saved["version"] = int(saved.get("version", 0)) + 1
        self.dashboard = copy.deepcopy(saved)
        if self.raise_response_loss:
            raise ConnectionError("response was lost")
        return saved


def _existing_dashboard() -> dict[str, object]:
    return {
        "id": {"id": "dashboard-1"},
        "version": 7,
        "title": DASHBOARD_TITLE,
        "mobileHide": False,
        "mobileOrder": None,
        "configuration": {"widgets": {"old": {"config": {}}}},
        "resources": [],
    }


class _Response:
    def __init__(self, payload: dict[str, object], *, ok: bool = True, status_code: int = 200, text: str = "") -> None:
        self.payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, object]:
        return copy.deepcopy(self.payload)


class RequestRecorder:
    """Records the actual requests API boundary receives without a network service."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.methods: list[str] = []

    def post(self, _url: str, **_kwargs: object) -> _Response:
        self.methods.append("POST")
        return _Response({"token": "login-token"})

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.methods.append(method)
        if url.endswith("/api/auth/user"):
            return _Response({"tenantId": {"id": TENANT_ID}})
        if url.endswith("/api/tenant/device"):
            name = kwargs["params"]["deviceName"]  # type: ignore[index]
            return _Response({"id": {"id": f"device-{name}"}})
        if url.endswith("/api/tenant/dashboards"):
            return _Response({"data": []})
        if url.endswith("/api/widgetType"):
            fqn = kwargs["params"]["fqn"]  # type: ignore[index]
            widget_type, default_config = {
                "system.cards.entities_table": ("latest", {"settings": {}}),
                "system.time_series_chart": ("timeseries", {"settings": {}}),
                "system.alarm_widgets.alarms_table": (
                    "alarm",
                    {"settings": {}, "alarmSource": {"dataKeys": []}},
                ),
            }[fqn]
            return _Response(
                {"descriptor": {"type": widget_type, "defaultConfig": default_config}}
            )
        raise AssertionError(f"unexpected URL: {url}")


class DashboardPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "TB_PDM_EXPECTED_TENANT_ID": TENANT_ID,
                "TB_TENANT_USERNAME": "test-tenant@example.invalid",
                "TB_TENANT_PASSWORD": "test-password",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base_config = load_config(CONFIG_PATH)
        self.config = replace(
            base_config,
            path=Path(self.temporary_directory.name) / "config.yml",
        )

    def _plan(self, api: RecordingApi):
        (self.config.runtime_dir / "pdm-dashboard-plan.json").unlink(missing_ok=True)
        return create_dashboard_plan(
            self.config,
            api=api,
            actor="dashboard-reviewer",
            now=NOW,
            correlation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )

    def test_plan_is_get_only_and_writes_a_secret_free_canonical_expiring_record(self) -> None:
        api = RecordingApi(_existing_dashboard())

        result = self._plan(api)

        self.assertTrue(api.calls)
        self.assertEqual({"GET"}, set(api.calls))
        plan_path = self.config.runtime_dir / "pdm-dashboard-plan.json"
        self.assertEqual(0o600, stat.S_IMODE(plan_path.stat().st_mode))
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(result.sha256, plan["plan_sha256"])
        self.assertEqual(TENANT_ID, plan["tenant_id"])
        self.assertEqual("dashboard-1", plan["snapshot"]["dashboard_id"])
        self.assertEqual(7, plan["snapshot"]["version"])
        self.assertEqual("dashboard-reviewer", plan["actor"])
        self.assertEqual("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", plan["correlation_id"])
        self.assertEqual(
            (NOW + timedelta(minutes=30)).isoformat(), plan["expires_at"]
        )
        self.assertEqual(plan["desired_body_sha256"], result.desired_body_sha256)
        self.assertNotIn("token", json.dumps(plan).lower())

    def test_plan_writes_only_to_the_explicit_absolute_output(self) -> None:
        api = RecordingApi(_existing_dashboard())
        artifact_dir = Path(self.temporary_directory.name) / "plans"
        artifact_dir.mkdir(mode=0o700)
        output = artifact_dir / "phase2-dashboard.json"

        result = create_dashboard_plan(
            self.config,
            api=api,
            actor="dashboard-reviewer",
            output_path=output,
            now=NOW,
            correlation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )

        self.assertEqual(output, result.path)
        self.assertEqual({"GET"}, set(api.calls))
        self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
        payload = json.loads(output.read_text(encoding="utf-8"))
        expected_bytes = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        self.assertEqual(expected_bytes, output.read_bytes())
        self.assertFalse((self.config.runtime_dir / "pdm-dashboard-plan.json").exists())

    def test_plan_rejects_symlink_alias_and_unsafe_parent_before_provider_reads(self) -> None:
        api = RecordingApi(_existing_dashboard())
        artifact_dir = Path(self.temporary_directory.name) / "plans"
        artifact_dir.mkdir(mode=0o700)
        target = artifact_dir / "target.json"
        target.write_text("unchanged", encoding="utf-8")
        alias = artifact_dir / "alias.json"
        alias.symlink_to(target)

        with self.assertRaisesRegex(DashboardPublicationError, "safe artifact path"):
            create_dashboard_plan(
                self.config,
                api=api,
                actor="dashboard-reviewer",
                output_path=alias,
                now=NOW,
            )
        self.assertEqual([], api.calls)

        read_only_dir = Path(self.temporary_directory.name) / "read-only"
        read_only_dir.mkdir(mode=0o700)
        read_only_dir.chmod(0o500)
        self.addCleanup(read_only_dir.chmod, 0o700)
        with self.assertRaisesRegex(DashboardPublicationError, "safe artifact path"):
            create_dashboard_plan(
                self.config,
                api=api,
                actor="dashboard-reviewer",
                output_path=read_only_dir / "plan.json",
                now=NOW,
            )
        self.assertEqual([], api.calls)

        with self.assertRaisesRegex(DashboardPublicationError, "safe artifact path"):
            create_dashboard_plan(
                self.config,
                api=api,
                actor="dashboard-reviewer",
                output_path=Path(self.temporary_directory.name)
                / "missing"
                / "plan.json",
                now=NOW,
            )
        self.assertEqual([], api.calls)
        self.assertEqual("unchanged", target.read_text(encoding="utf-8"))

        unsafe_dir = Path(self.temporary_directory.name) / "unsafe"
        unsafe_dir.mkdir(mode=0o700)
        unsafe_dir.chmod(0o777)
        with self.assertRaisesRegex(DashboardPublicationError, "safe artifact path"):
            create_dashboard_plan(
                self.config,
                api=api,
                actor="dashboard-reviewer",
                output_path=unsafe_dir / "plan.json",
                now=NOW,
            )
        self.assertEqual([], api.calls)

    def test_cli_exposes_the_exact_phase2_plan_and_apply_artifact_contract(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plans_dir = Path(self.temporary_directory.name) / "plans"
        receipts_dir = Path(self.temporary_directory.name) / "receipts"
        plans_dir.mkdir(mode=0o700)
        receipts_dir.mkdir(mode=0o700)
        plan_path = plans_dir / "phase2-dashboard.json"
        receipt_path = receipts_dir / "phase2-dashboard.json"
        plan_output = io.StringIO()

        with patch(
            "factory_simulator.dashboard_publication._managed_api",
            return_value=api,
        ):
            with contextlib.redirect_stdout(plan_output):
                plan_exit = main(
                    [
                        "--config",
                        str(CONFIG_PATH),
                        "dashboard-plan",
                        "--actor",
                        "phase2-operator",
                        "--output",
                        str(plan_path),
                    ]
                )

        self.assertEqual(0, plan_exit)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertIn(TENANT_ID, plan_output.getvalue())
        self.assertIn(plan["snapshot"]["body_sha256"], plan_output.getvalue())
        self.assertIn(plan["desired_body_sha256"], plan_output.getvalue())
        self.assertIn(plan["plan_sha256"], plan_output.getvalue())

        with patch(
            "factory_simulator.dashboard_publication._managed_api",
            return_value=api,
        ):
            apply_exit = main(
                [
                    "--config",
                    str(CONFIG_PATH),
                    "dashboard-apply",
                    "--plan",
                    str(plan_path),
                    "--plan-hash",
                    plan["plan_sha256"],
                    "--confirmed-hash",
                    plan["plan_sha256"],
                    "--actor",
                    "phase2-operator",
                    "--receipt",
                    str(receipt_path),
                ]
            )

        self.assertEqual(0, apply_exit)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("phase2-operator", receipt["actor"])
        self.assertEqual(0o600, stat.S_IMODE(receipt_path.stat().st_mode))
        self.assertNotIn("token", json.dumps(receipt).lower())

    def test_plan_uses_the_preauthenticated_bearer_at_the_real_request_boundary(self) -> None:
        recorder = RequestRecorder()
        real_api = ThingsBoardApi(self.config)
        real_api.session = recorder  # type: ignore[assignment]

        def api_factory(_config: object, **kwargs: object) -> ThingsBoardApi:
            bearer = kwargs.get("bearer_token")
            if isinstance(bearer, str):
                real_api.token = bearer
                real_api.session.headers["X-Authorization"] = f"Bearer {bearer}"
            return real_api

        with patch.dict(
            os.environ,
            {"TB_PDM_DASHBOARD_BEARER_TOKEN": "preauthenticated-secret"},
            clear=False,
        ):
            with patch("factory_simulator.dashboard_publication.ThingsBoardApi", api_factory):
                create_dashboard_plan(self.config, actor="dashboard-reviewer", now=NOW)

        self.assertTrue(recorder.methods)
        self.assertEqual({"GET"}, set(recorder.methods))
        self.assertNotIn("preauthenticated-secret", (self.config.runtime_dir / "pdm-dashboard-plan.json").read_text())

    def test_api_and_cli_errors_redact_response_bodies_and_bearer_values(self) -> None:
        leaked = "Bearer very-secret-token"

        class FailingSession:
            headers: dict[str, str] = {}

            @staticmethod
            def post(_url: str, **_kwargs: object) -> _Response:
                return _Response({}, ok=False, status_code=401, text=leaked)

        api = ThingsBoardApi(self.config)
        api.session = FailingSession()  # type: ignore[assignment]
        with self.assertRaises(ThingsBoardApiError) as raised:
            api.login()
        self.assertNotIn("very-secret-token", str(raised.exception))

        output = io.StringIO()
        with patch(
            "factory_simulator.cli._cmd_dashboard_plan",
            side_effect=ThingsBoardApiError(leaked),
        ):
            with contextlib.redirect_stderr(output):
                exit_code = main(
                    [
                        "--config",
                        str(CONFIG_PATH),
                        "dashboard-plan",
                        "--actor",
                        "dashboard-reviewer",
                        "--output",
                        str(Path(self.temporary_directory.name) / "plan.json"),
                    ]
                )
        self.assertEqual(1, exit_code)
        self.assertNotIn("very-secret-token", output.getvalue())

    def test_apply_requires_an_identical_later_confirmation_before_posting(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plan = self._plan(api)

        with self.assertRaisesRegex(DashboardPublicationError, "confirmation"):
            apply_dashboard_plan(
                self.config,
                plan_sha256=plan.sha256,
                confirmed_sha256="c" * 64,
                api=api,
                now=NOW + timedelta(minutes=1),
            )

        self.assertNotIn("POST", api.calls)

    def test_apply_requires_exact_lowercase_sha256_values(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plan = self._plan(api)
        api.calls.clear()

        with self.assertRaisesRegex(DashboardPublicationError, "lowercase SHA-256"):
            apply_dashboard_plan(
                self.config,
                plan_sha256=plan.sha256.upper(),
                confirmed_sha256=plan.sha256.upper(),
                api=api,
                now=NOW + timedelta(minutes=1),
            )

        self.assertEqual([], api.calls)

        with self.assertRaisesRegex(DashboardPublicationError, "lowercase SHA-256"):
            apply_dashboard_plan(
                self.config,
                plan_sha256=None,  # type: ignore[arg-type]
                confirmed_sha256=None,  # type: ignore[arg-type]
                api=api,
                now=NOW + timedelta(minutes=1),
            )
        self.assertEqual([], api.calls)

    def test_apply_rejects_the_plan_at_its_exact_expiry_before_provider_reads(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plan = self._plan(api)
        api.calls.clear()

        with self.assertRaisesRegex(DashboardPublicationError, "expired"):
            apply_dashboard_plan(
                self.config,
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                api=api,
                now=NOW + timedelta(minutes=30),
            )

        self.assertEqual([], api.calls)

    def test_explicit_apply_rejects_path_aliases_before_posting(self) -> None:
        api = RecordingApi(_existing_dashboard())
        artifact_dir = Path(self.temporary_directory.name) / "artifacts"
        artifact_dir.mkdir(mode=0o700)
        (artifact_dir / "child").mkdir(mode=0o700)
        plan_path = artifact_dir / "phase2-dashboard.json"
        plan = create_dashboard_plan(
            self.config,
            api=api,
            actor="dashboard-reviewer",
            output_path=plan_path,
            now=NOW,
        )
        api.calls.clear()

        with self.assertRaisesRegex(DashboardPublicationError, "distinct"):
            apply_dashboard_plan(
                self.config,
                plan_path=plan_path,
                receipt_path=artifact_dir / "child" / ".." / "phase2-dashboard.json",
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                actor="dashboard-applier",
                api=api,
                now=NOW + timedelta(minutes=1),
            )

        self.assertEqual([], api.calls)

    def test_apply_consumes_the_plan_even_when_a_different_receipt_is_requested(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plans_dir = Path(self.temporary_directory.name) / "plans"
        receipts_dir = Path(self.temporary_directory.name) / "receipts"
        plans_dir.mkdir(mode=0o700)
        receipts_dir.mkdir(mode=0o700)
        plan_path = plans_dir / "phase2-dashboard.json"
        plan = create_dashboard_plan(
            self.config,
            api=api,
            actor="dashboard-reviewer",
            output_path=plan_path,
            now=NOW,
        )
        first_receipt = receipts_dir / "first.json"
        apply_dashboard_plan(
            self.config,
            plan_path=plan_path,
            receipt_path=first_receipt,
            plan_sha256=plan.sha256,
            confirmed_sha256=plan.sha256,
            actor="dashboard-applier",
            api=api,
            now=NOW + timedelta(minutes=1),
        )
        posts_after_first_apply = api.calls.count("POST")

        with self.assertRaisesRegex(DashboardPublicationError, "consumed"):
            apply_dashboard_plan(
                self.config,
                plan_path=plan_path,
                receipt_path=receipts_dir / "second.json",
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                actor="dashboard-applier",
                api=api,
                now=NOW + timedelta(minutes=2),
            )

        self.assertEqual(posts_after_first_apply, api.calls.count("POST"))

    def test_apply_rejects_a_receipt_path_that_aliases_its_consumption_marker(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plans_dir = Path(self.temporary_directory.name) / "plans"
        plans_dir.mkdir(mode=0o700)
        plan_path = plans_dir / "phase2-dashboard.json"
        plan = create_dashboard_plan(
            self.config,
            api=api,
            actor="dashboard-reviewer",
            output_path=plan_path,
            now=NOW,
        )
        api.calls.clear()

        with self.assertRaisesRegex(DashboardPublicationError, "distinct"):
            apply_dashboard_plan(
                self.config,
                plan_path=plan_path,
                receipt_path=plans_dir / ".phase2-dashboard.json.consumed",
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                actor="dashboard-applier",
                api=api,
                now=NOW + timedelta(minutes=1),
            )

        self.assertEqual([], api.calls)

    def test_apply_rechecks_tenant_dashboard_version_and_body_before_one_save(self) -> None:
        for mutation in ("tenant", "version", "body"):
            with self.subTest(mutation=mutation):
                api = RecordingApi(_existing_dashboard())
                plan = self._plan(api)
                if mutation == "tenant":
                    api.get_current_tenant_id = lambda: "cccccccc-cccc-4ccc-8ccc-cccccccccccc"  # type: ignore[method-assign]
                elif mutation == "version":
                    api.dashboard["version"] = 8  # type: ignore[index]
                else:
                    api.dashboard["configuration"] = {"widgets": {"drift": {}}}  # type: ignore[index]

                with self.assertRaisesRegex(DashboardPublicationError, "drift"):
                    apply_dashboard_plan(
                        self.config,
                        plan_sha256=plan.sha256,
                        confirmed_sha256=plan.sha256,
                        api=api,
                        now=NOW + timedelta(minutes=1),
                    )

                self.assertNotIn("POST", api.calls)

    def test_apply_skips_post_when_the_dashboard_already_matches_the_plan(self) -> None:
        api = RecordingApi(_existing_dashboard())
        first_plan = self._plan(api)
        api.dashboard = copy.deepcopy(first_plan.desired_dashboard)
        api.dashboard["id"] = {"id": "dashboard-1"}  # type: ignore[index]
        api.dashboard["version"] = 7  # type: ignore[index]
        api.calls.clear()
        plan = self._plan(api)
        api.calls.clear()

        receipt = apply_dashboard_plan(
            self.config,
            plan_sha256=plan.sha256,
            confirmed_sha256=plan.sha256,
            api=api,
            now=NOW + timedelta(minutes=1),
        )

        self.assertFalse(receipt["saved"])
        self.assertNotIn("POST", api.calls)

    def test_response_loss_recovers_only_after_exact_desired_dashboard_readback(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plan = self._plan(api)
        api.raise_response_loss = True
        api.calls.clear()

        receipt = apply_dashboard_plan(
            self.config,
            plan_sha256=plan.sha256,
            confirmed_sha256=plan.sha256,
            api=api,
            now=NOW + timedelta(minutes=1),
        )

        self.assertTrue(receipt["recovered_response_loss"])
        self.assertEqual(["GET", "GET", "POST", "GET"], api.calls)
        receipt_path = self.config.runtime_dir / "pdm-dashboard-receipt.json"
        self.assertEqual(0o600, stat.S_IMODE(receipt_path.stat().st_mode))
        self.assertNotIn("token", receipt_path.read_text(encoding="utf-8").lower())

    def test_creation_response_loss_discovers_one_exact_desired_dashboard(self) -> None:
        api = RecordingApi()
        plan = self._plan(api)
        api.raise_response_loss = True
        api.calls.clear()

        receipt = apply_dashboard_plan(
            self.config,
            plan_sha256=plan.sha256,
            confirmed_sha256=plan.sha256,
            api=api,
            now=NOW + timedelta(minutes=1),
        )

        self.assertTrue(receipt["recovered_response_loss"])
        self.assertEqual("dashboard-1", receipt["dashboard_id"])

    def test_creation_response_loss_rejects_ambiguous_discovery(self) -> None:
        api = RecordingApi()
        plan = self._plan(api)
        api.raise_response_loss = True
        matching = copy.deepcopy(plan.desired_dashboard)
        matching["id"] = {"id": "dashboard-1"}
        matching["version"] = 1
        desired = copy.deepcopy(plan.desired_dashboard)
        desired["id"] = {"id": "another-dashboard"}
        desired["version"] = 1
        api.discovered_after_save = [matching, desired]

        with self.assertRaisesRegex(DashboardPublicationError, "ambiguous"):
            apply_dashboard_plan(
                self.config,
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                api=api,
                now=NOW + timedelta(minutes=1),
            )

    def test_apply_rejects_a_success_response_that_does_not_match_the_plan(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plan = self._plan(api)
        api.save_dashboard = lambda _dashboard: {  # type: ignore[method-assign]
            "id": {"id": "different-dashboard"},
            "version": 8,
            "title": DASHBOARD_TITLE,
            "configuration": {"widgets": {"drift": {}}},
        }

        with self.assertRaisesRegex(DashboardPublicationError, "does not match"):
            apply_dashboard_plan(
                self.config,
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                api=api,
                now=NOW + timedelta(minutes=1),
            )

    def test_response_loss_error_does_not_chain_a_bearer_value(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plan = self._plan(api)

        def lose_response(_dashboard: dict[str, object]) -> dict[str, object]:
            raise ConnectionError("Bearer response-loss-secret")

        api.save_dashboard = lose_response  # type: ignore[method-assign]
        with self.assertRaises(DashboardPublicationError) as raised:
            apply_dashboard_plan(
                self.config,
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                api=api,
                now=NOW + timedelta(minutes=1),
            )

        self.assertNotIn("response-loss-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_plan_and_receipt_reject_nested_credential_like_data(self) -> None:
        api = RecordingApi(_existing_dashboard())
        plan = self._plan(api)
        plan_path = self.config.runtime_dir / "pdm-dashboard-plan.json"
        payload = json.loads(plan_path.read_text())
        payload["desired_dashboard"]["configuration"]["nested"] = {
            "apiKey": "credential-value"
        }
        plan_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(plan_path, 0o600)

        with self.assertRaisesRegex(DashboardPublicationError, "credential-like"):
            apply_dashboard_plan(
                self.config,
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                api=api,
                now=NOW + timedelta(minutes=1),
            )

    def test_legacy_dashboard_command_is_blocked_in_managed_mode(self) -> None:
        with patch.dict(
            os.environ,
            {"TB_PDM_DASHBOARD_MANAGED_PUBLICATION": "true"},
            clear=False,
        ):
            with self.assertRaisesRegex(DashboardPublicationError, "dashboard-plan"):
                _cmd_dashboard(self.config, object())


if __name__ == "__main__":
    unittest.main()
