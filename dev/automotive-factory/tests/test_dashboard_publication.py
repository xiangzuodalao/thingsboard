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

from factory_simulator.cli import _cmd_dashboard
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


class DashboardPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {"TB_PDM_EXPECTED_TENANT_ID": TENANT_ID},
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
