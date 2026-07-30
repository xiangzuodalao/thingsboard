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
import concurrent.futures
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from requests import ConnectionError

from factory_simulator import dashboard_publication
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
    def __init__(
        self, payload: dict[str, object], *, ok: bool = True, status_code: int = 200, text: str = ""
    ) -> None:
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
            return _Response({"descriptor": {"type": widget_type, "defaultConfig": default_config}})
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

    def _api_with_provider_default(self, value: object) -> RecordingApi:
        api = RecordingApi(_existing_dashboard())
        get_widget_type = api.get_widget_type

        def inject_provider_value(fqn: str) -> dict[str, object]:
            response = get_widget_type(fqn)
            if fqn == "system.cards.entities_table":
                response["descriptor"]["defaultConfig"]["providerValue"] = value  # type: ignore[index]
            return response

        api.get_widget_type = inject_provider_value  # type: ignore[method-assign]
        return api

    def test_plan_writer_rejects_nonfinite_provider_values_before_creating_a_file(
        self,
    ) -> None:
        for index, value in enumerate((float("nan"), float("inf"), float("-inf"))):
            with self.subTest(value=value):
                artifact_dir = Path(self.temporary_directory.name) / f"nonfinite-{index}"
                artifact_dir.mkdir(mode=0o700)
                output_path = artifact_dir / "plan.json"
                api = self._api_with_provider_default(value)

                with self.assertRaisesRegex(
                    DashboardPublicationError,
                    "canonical JSON",
                ):
                    create_dashboard_plan(
                        self.config,
                        api=api,
                        actor="dashboard-reviewer",
                        output_path=output_path,
                        now=NOW,
                    )

                self.assertEqual([], list(artifact_dir.iterdir()))
                self.assertEqual({"GET"}, set(api.calls))

    def test_plan_writer_rejects_oversized_provider_data_before_creating_a_file(
        self,
    ) -> None:
        artifact_dir = Path(self.temporary_directory.name) / "oversized-writer"
        artifact_dir.mkdir(mode=0o700)
        output_path = artifact_dir / "plan.json"
        api = self._api_with_provider_default("x" * (8 * 1024 * 1024))

        with self.assertRaisesRegex(DashboardPublicationError, "size limit"):
            create_dashboard_plan(
                self.config,
                api=api,
                actor="dashboard-reviewer",
                output_path=output_path,
                now=NOW,
            )

        self.assertEqual([], list(artifact_dir.iterdir()))
        self.assertEqual({"GET"}, set(api.calls))

    def test_plan_writer_rejects_a_tuple_wrapped_managed_bearer_before_writes(
        self,
    ) -> None:
        artifact_dir = Path(self.temporary_directory.name) / "tuple-bearer-writer"
        artifact_dir.mkdir(mode=0o700)
        output_path = artifact_dir / "plan.json"
        opaque_bearer = "opaque-provider-credential-68b51a2c"
        api = self._api_with_provider_default((f"prefix:{opaque_bearer}:suffix",))

        with patch.dict(
            os.environ,
            {"TB_PDM_DASHBOARD_BEARER_TOKEN": opaque_bearer},
            clear=False,
        ):
            with self.assertRaises(DashboardPublicationError) as raised:
                create_dashboard_plan(
                    self.config,
                    api=api,
                    actor="dashboard-reviewer",
                    output_path=output_path,
                    now=NOW,
                )

        self.assertNotIn(opaque_bearer, str(raised.exception))
        self.assertEqual([], list(artifact_dir.iterdir()))
        self.assertEqual({"GET"}, set(api.calls))

    def test_cli_maps_deep_provider_default_recursion_to_a_stable_error_before_writes(
        self,
    ) -> None:
        artifact_dir = Path(self.temporary_directory.name) / "deep-provider-writer"
        artifact_dir.mkdir(mode=0o700)
        output_path = artifact_dir / "plan.json"
        opaque_bearer = "opaque-deep-provider-credential-6c4b31e2"
        provider_value: object = f"prefix:{opaque_bearer}:suffix"
        for _ in range(1200):
            provider_value = [provider_value]
        api = self._api_with_provider_default(provider_value)
        error_output = io.StringIO()

        with patch.dict(
            os.environ,
            {"TB_PDM_DASHBOARD_BEARER_TOKEN": opaque_bearer},
            clear=False,
        ):
            with patch(
                "factory_simulator.dashboard_publication._managed_api",
                return_value=api,
            ):
                with contextlib.redirect_stderr(error_output):
                    try:
                        exit_code: int | str = main(
                            [
                                "--config",
                                str(CONFIG_PATH),
                                "dashboard-plan",
                                "--actor",
                                "dashboard-reviewer",
                                "--output",
                                str(output_path),
                            ]
                        )
                    except RecursionError:
                        exit_code = "uncaught recursion"

        self.assertEqual(1, exit_code)
        self.assertIn("ERROR:", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())
        self.assertNotIn("RecursionError", error_output.getvalue())
        self.assertNotIn(opaque_bearer, error_output.getvalue())
        self.assertEqual([], list(artifact_dir.iterdir()))
        self.assertEqual({"GET"}, set(api.calls))

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
        self.assertEqual((NOW + timedelta(minutes=30)).isoformat(), plan["expires_at"])
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
                output_path=Path(self.temporary_directory.name) / "missing" / "plan.json",
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
        cli_config_path = Path(self.temporary_directory.name) / "config.yml"
        cli_config_path.write_bytes(CONFIG_PATH.read_bytes())
        cli_config_path.chmod(0o600)
        plan_output = io.StringIO()

        with patch(
            "factory_simulator.dashboard_publication._managed_api",
            return_value=api,
        ):
            with contextlib.redirect_stdout(plan_output):
                plan_exit = main(
                    [
                        "--config",
                        str(cli_config_path),
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
                    str(cli_config_path),
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
        self.assertNotIn(
            "preauthenticated-secret",
            (self.config.runtime_dir / "pdm-dashboard-plan.json").read_text(),
        )

    def test_plan_rejects_an_opaque_managed_bearer_as_actor_without_leaking_it(
        self,
    ) -> None:
        api = RecordingApi(_existing_dashboard())
        opaque_bearer = "opaque-dashboard-credential-04f8a621"
        plan_path = self.config.runtime_dir / "pdm-dashboard-plan.json"

        with patch.dict(
            os.environ,
            {"TB_PDM_DASHBOARD_BEARER_TOKEN": opaque_bearer},
            clear=False,
        ):
            with self.assertRaises(DashboardPublicationError) as raised:
                create_dashboard_plan(
                    self.config,
                    api=api,
                    actor=opaque_bearer,
                    now=NOW,
                )

        self.assertNotIn(opaque_bearer, str(raised.exception))
        self.assertFalse(plan_path.exists())

    def test_plan_rejects_an_actor_containing_the_complete_managed_bearer(
        self,
    ) -> None:
        api = RecordingApi(_existing_dashboard())
        opaque_bearer = "opaque-dashboard-credential-a1425c90"
        actor = f"reviewer:{opaque_bearer}:phase2"
        plan_path = self.config.runtime_dir / "pdm-dashboard-plan.json"

        with patch.dict(
            os.environ,
            {"TB_PDM_DASHBOARD_BEARER_TOKEN": opaque_bearer},
            clear=False,
        ):
            with self.assertRaises(DashboardPublicationError) as raised:
                create_dashboard_plan(
                    self.config,
                    api=api,
                    actor=actor,
                    now=NOW,
                )

        self.assertNotIn(opaque_bearer, str(raised.exception))
        self.assertEqual([], api.calls)
        self.assertFalse(plan_path.exists())

    def test_actor_rejects_c1_control_characters(self) -> None:
        actor = "dashboard\u0080operator"

        with self.assertRaises(DashboardPublicationError) as raised:
            dashboard_publication._actor(actor, field="dashboard actor")

        self.assertNotIn(actor, str(raised.exception))

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

    def test_copied_plans_concurrently_share_one_hash_bound_consumption_claim(
        self,
    ) -> None:
        class ConcurrentRecordingApi(RecordingApi):
            def __init__(self) -> None:
                super().__init__(_existing_dashboard())
                self.snapshot_barrier = threading.Barrier(2)
                self.save_lock = threading.Lock()

            def get_dashboard(self, dashboard_id: str) -> dict[str, object]:
                dashboard = super().get_dashboard(dashboard_id)
                self.snapshot_barrier.wait(timeout=10)
                return dashboard

            def save_dashboard(self, dashboard: dict[str, object]) -> dict[str, object]:
                with self.save_lock:
                    return super().save_dashboard(dashboard)

        api = ConcurrentRecordingApi()
        plans_dir = Path(self.temporary_directory.name) / "plans"
        receipts_dir = Path(self.temporary_directory.name) / "receipts"
        plans_dir.mkdir(mode=0o700)
        receipts_dir.mkdir(mode=0o700)
        first_plan_path = plans_dir / "first.json"
        second_plan_path = plans_dir / "second.json"
        plan = create_dashboard_plan(
            self.config,
            api=api,
            actor="dashboard-reviewer",
            output_path=first_plan_path,
            now=NOW,
        )
        second_plan_path.write_bytes(first_plan_path.read_bytes())
        second_plan_path.chmod(0o600)
        receipt_paths = (receipts_dir / "first.json", receipts_dir / "second.json")
        api.calls.clear()

        def apply(plan_path: Path, receipt_path: Path) -> object:
            try:
                return apply_dashboard_plan(
                    self.config,
                    plan_path=plan_path,
                    receipt_path=receipt_path,
                    plan_sha256=plan.sha256,
                    confirmed_sha256=plan.sha256,
                    actor="dashboard-applier",
                    api=api,
                    now=NOW + timedelta(minutes=1),
                )
            except Exception as exc:
                return exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(apply, (first_plan_path, second_plan_path), receipt_paths))

        successes = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], DashboardPublicationError)
        self.assertIn("consumed", str(failures[0]))
        self.assertEqual(1, api.calls.count("POST"))
        self.assertEqual(1, sum(path.exists() for path in receipt_paths))

        namespace = self.config.runtime_dir / ".pdm-dashboard-consumption"
        marker_path = namespace / f"{plan.sha256}.json"
        self.assertEqual(0o700, stat.S_IMODE(namespace.stat().st_mode))
        self.assertEqual(os.geteuid(), namespace.stat().st_uid)
        self.assertEqual([marker_path], list(namespace.iterdir()))
        self.assertEqual(0o600, stat.S_IMODE(marker_path.stat().st_mode))
        self.assertEqual(
            plan.sha256,
            json.loads(marker_path.read_text(encoding="utf-8"))["plan_sha256"],
        )

    def test_apply_rejects_an_unsafe_consumption_namespace_before_provider_reads(
        self,
    ) -> None:
        for mutation in ("symlink", "mode", "owner"):
            with self.subTest(mutation=mutation):
                api = RecordingApi(_existing_dashboard())
                plans_dir = Path(self.temporary_directory.name) / f"plans-{mutation}"
                receipts_dir = Path(self.temporary_directory.name) / f"receipts-{mutation}"
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
                self.config.runtime_dir.mkdir(mode=0o700, exist_ok=True)
                namespace = self.config.runtime_dir / ".pdm-dashboard-consumption"
                outside = Path(self.temporary_directory.name) / f"outside-{mutation}"
                outside.mkdir(mode=0o700)
                if mutation == "symlink":
                    namespace.symlink_to(outside, target_is_directory=True)
                else:
                    namespace.mkdir(mode=0o700)
                    if mutation == "mode":
                        namespace.chmod(0o755)
                api.calls.clear()

                real_lstat = Path.lstat

                def lstat_with_wrong_namespace_owner(
                    candidate: Path,
                ) -> os.stat_result:
                    result = real_lstat(candidate)
                    if mutation == "owner" and candidate == namespace:
                        fields = list(result)
                        fields[4] = result.st_uid + 1
                        return os.stat_result(fields)
                    return result

                try:
                    with patch.object(Path, "lstat", lstat_with_wrong_namespace_owner):
                        with self.assertRaisesRegex(
                            DashboardPublicationError,
                            "consumption namespace",
                        ):
                            apply_dashboard_plan(
                                self.config,
                                plan_path=plan_path,
                                receipt_path=receipts_dir / "receipt.json",
                                plan_sha256=plan.sha256,
                                confirmed_sha256=plan.sha256,
                                actor="dashboard-applier",
                                api=api,
                                now=NOW + timedelta(minutes=1),
                            )

                    self.assertEqual([], api.calls)
                    self.assertEqual([], list(outside.iterdir()))
                finally:
                    if namespace.is_symlink():
                        namespace.unlink()
                    else:
                        namespace.chmod(0o700)
                        namespace.rmdir()

    def test_apply_rejects_a_world_writable_consumption_root_before_provider_reads(
        self,
    ) -> None:
        api = RecordingApi(_existing_dashboard())
        plans_dir = self.config.root_dir / "safe-plans"
        receipts_dir = self.config.root_dir / "safe-receipts"
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
        api.calls.clear()
        self.config.root_dir.chmod(0o777)

        try:
            with self.assertRaisesRegex(
                DashboardPublicationError,
                "runtime directory",
            ):
                apply_dashboard_plan(
                    self.config,
                    plan_path=plan_path,
                    receipt_path=receipts_dir / "receipt.json",
                    plan_sha256=plan.sha256,
                    confirmed_sha256=plan.sha256,
                    actor="dashboard-applier",
                    api=api,
                    now=NOW + timedelta(minutes=1),
                )
        finally:
            self.config.root_dir.chmod(0o700)

        self.assertEqual([], api.calls)
        self.assertFalse(self.config.runtime_dir.exists())
        self.assertFalse((receipts_dir / "receipt.json").exists())

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
        self.config.runtime_dir.mkdir(mode=0o700, exist_ok=True)
        consumption_namespace = self.config.runtime_dir / ".pdm-dashboard-consumption"
        consumption_namespace.mkdir(mode=0o700)
        api.calls.clear()

        with self.assertRaisesRegex(DashboardPublicationError, "distinct"):
            apply_dashboard_plan(
                self.config,
                plan_path=plan_path,
                receipt_path=consumption_namespace / f"{plan.sha256}.json",
                plan_sha256=plan.sha256,
                confirmed_sha256=plan.sha256,
                actor="dashboard-applier",
                api=api,
                now=NOW + timedelta(minutes=1),
            )

        self.assertEqual([], api.calls)

    def test_apply_rejects_an_opaque_managed_bearer_as_actor_before_posting(
        self,
    ) -> None:
        api = RecordingApi(_existing_dashboard())
        opaque_bearer = "opaque-dashboard-credential-e16a9b72"
        plan = self._plan(api)
        api.calls.clear()

        with patch.dict(
            os.environ,
            {"TB_PDM_DASHBOARD_BEARER_TOKEN": opaque_bearer},
            clear=False,
        ):
            with self.assertRaises(DashboardPublicationError) as raised:
                apply_dashboard_plan(
                    self.config,
                    plan_sha256=plan.sha256,
                    confirmed_sha256=plan.sha256,
                    actor=opaque_bearer,
                    api=api,
                    now=NOW + timedelta(minutes=1),
                )

        self.assertNotIn(opaque_bearer, str(raised.exception))
        self.assertEqual([], api.calls)
        receipt_path = self.config.runtime_dir / "pdm-dashboard-receipt.json"
        self.assertFalse(receipt_path.exists())
        self.assertNotIn(
            opaque_bearer,
            "\n".join(
                path.read_text(encoding="utf-8")
                for path in self.config.runtime_dir.rglob("*")
                if path.is_file()
            ),
        )

    def test_apply_rejects_an_actor_containing_the_complete_managed_bearer(
        self,
    ) -> None:
        api = RecordingApi(_existing_dashboard())
        opaque_bearer = "opaque-dashboard-credential-b7138fd4"
        actor = f"reviewer:{opaque_bearer}:phase2"
        plan = self._plan(api)
        api.calls.clear()

        with patch.dict(
            os.environ,
            {"TB_PDM_DASHBOARD_BEARER_TOKEN": opaque_bearer},
            clear=False,
        ):
            with self.assertRaises(DashboardPublicationError) as raised:
                apply_dashboard_plan(
                    self.config,
                    plan_sha256=plan.sha256,
                    confirmed_sha256=plan.sha256,
                    actor=actor,
                    api=api,
                    now=NOW + timedelta(minutes=1),
                )

        self.assertNotIn(opaque_bearer, str(raised.exception))
        self.assertEqual([], api.calls)
        self.assertFalse((self.config.runtime_dir / "pdm-dashboard-receipt.json").exists())
        self.assertNotIn(
            opaque_bearer,
            "\n".join(
                path.read_text(encoding="utf-8")
                for path in self.config.runtime_dir.rglob("*")
                if path.is_file()
            ),
        )

    def test_apply_rejects_a_plan_swapped_to_a_symlink_after_path_validation(
        self,
    ) -> None:
        api = RecordingApi(_existing_dashboard())
        plan = self._plan(api)
        plan_path = self.config.runtime_dir / "pdm-dashboard-plan.json"
        replacement = self.config.runtime_dir / "replacement.json"
        replacement.write_bytes(plan_path.read_bytes())
        replacement.chmod(0o600)
        api.calls.clear()
        real_read_plan = dashboard_publication._read_plan

        def swap_then_read(selected_path: Path) -> dict[str, object]:
            selected_path.unlink()
            selected_path.symlink_to(replacement)
            return real_read_plan(selected_path)

        with patch(
            "factory_simulator.dashboard_publication._read_plan",
            side_effect=swap_then_read,
        ):
            with self.assertRaisesRegex(
                DashboardPublicationError,
                "safe dashboard plan file",
            ):
                apply_dashboard_plan(
                    self.config,
                    plan_sha256=plan.sha256,
                    confirmed_sha256=plan.sha256,
                    actor="dashboard-applier",
                    api=api,
                    now=NOW + timedelta(minutes=1),
                )

        self.assertEqual([], api.calls)

    def test_plan_reader_rejects_a_fifo_without_blocking(self) -> None:
        fifo_path = Path(self.temporary_directory.name) / "plan.fifo"
        os.mkfifo(fifo_path, mode=0o600)
        script = """
import sys
from pathlib import Path
from factory_simulator.dashboard_publication import (
    DashboardPublicationError,
    _read_plan,
)

try:
    _read_plan(Path(sys.argv[1]))
except DashboardPublicationError:
    raise SystemExit(0)
raise SystemExit(1)
"""
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        try:
            completed = subprocess.run(
                [sys.executable, "-c", script, str(fifo_path)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("dashboard plan reader blocked while opening a FIFO")

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_plan_reader_rejects_canonical_input_larger_than_eight_mebibytes(
        self,
    ) -> None:
        oversized_plan = Path(self.temporary_directory.name) / "oversized-plan.json"
        oversized_plan.write_bytes(b'{"padding":"' + (b"a" * (8 * 1024 * 1024)) + b'"}\n')
        oversized_plan.chmod(0o600)

        with self.assertRaisesRegex(DashboardPublicationError, "size limit"):
            dashboard_publication._read_plan(oversized_plan)

    def test_plan_reader_rejects_nonfinite_json_with_a_stable_error(self) -> None:
        for index, literal in enumerate((b"NaN", b"Infinity", b"-Infinity", b"1e999")):
            with self.subTest(literal=literal):
                plan_path = Path(self.temporary_directory.name) / f"nonfinite-plan-{index}.json"
                plan_path.write_bytes(b'{"value":' + literal + b"}\n")
                plan_path.chmod(0o600)

                with self.assertRaisesRegex(
                    DashboardPublicationError,
                    "not canonical JSON",
                ):
                    dashboard_publication._read_plan(plan_path)

    def test_cli_maps_deep_json_recursion_to_a_stable_error_before_provider_reads(
        self,
    ) -> None:
        api = RecordingApi(_existing_dashboard())
        plan_path = Path(self.temporary_directory.name) / "deep-plan.json"
        receipt_path = Path(self.temporary_directory.name) / "deep-receipt.json"
        plan_path.write_bytes(b'{"nested":' + (b"[" * 1200) + b"0" + (b"]" * 1200) + b"}\n")
        plan_path.chmod(0o600)
        error_output = io.StringIO()

        with patch(
            "factory_simulator.dashboard_publication._managed_api",
            return_value=api,
        ):
            with contextlib.redirect_stderr(error_output):
                try:
                    exit_code: int | str = main(
                        [
                            "--config",
                            str(CONFIG_PATH),
                            "dashboard-apply",
                            "--plan",
                            str(plan_path),
                            "--plan-hash",
                            "a" * 64,
                            "--confirmed-hash",
                            "a" * 64,
                            "--actor",
                            "dashboard-applier",
                            "--receipt",
                            str(receipt_path),
                        ]
                    )
                except RecursionError:
                    exit_code = "uncaught recursion"

        self.assertEqual(1, exit_code)
        self.assertIn("ERROR:", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())
        self.assertNotIn("RecursionError", error_output.getvalue())
        self.assertEqual([], api.calls)
        self.assertFalse(receipt_path.exists())

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
        payload["desired_dashboard"]["configuration"]["nested"] = {"apiKey": "credential-value"}
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
