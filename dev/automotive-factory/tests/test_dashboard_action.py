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

import json
import os
import unittest
from unittest.mock import patch

from factory_simulator.dashboard import (
    _ALARM_WIDGET_ID,
    _CREATE_WORK_ORDER_ACTION_ID,
    _MAINTENANCE_ACTIONS_ENV,
    _dashboard_configuration,
)
from factory_simulator.dashboard_publication import _validate_artifact_tree


class WidgetDefaultsApi:
    def get_widget_type(self, fqn: str) -> dict[str, object]:
        widget_type, default_config = {
            "system.cards.entities_table": ("latest", {"settings": {}}),
            "system.time_series_chart": ("timeseries", {"settings": {}}),
            "system.alarm_widgets.alarms_table": (
                "alarm",
                {
                    "settings": {
                        "allowAcknowledgment": False,
                        "allowClear": True,
                    },
                    "alarmSource": {"dataKeys": []},
                },
            ),
        }[fqn]
        return {
            "descriptor": {
                "type": widget_type,
                "defaultConfig": default_config,
            }
        }


def _alarm_config(*, enabled: bool | None = None) -> dict[str, object]:
    configuration = _dashboard_configuration(
        WidgetDefaultsApi(),  # type: ignore[arg-type]
        ["device-1"],
        maintenance_actions_enabled=enabled,
    )
    return configuration["widgets"][_ALARM_WIDGET_ID]["config"]  # type: ignore[index,return-value]


class DashboardMaintenanceActionTests(unittest.TestCase):
    def test_action_is_absent_by_default_while_ack_remains_and_clear_is_disabled(self) -> None:
        with patch.dict(os.environ, {_MAINTENANCE_ACTIONS_ENV: "false"}, clear=False):
            alarm_config = _alarm_config()

        self.assertEqual({}, alarm_config["actions"])
        settings = alarm_config["settings"]
        self.assertTrue(settings["allowAcknowledgment"])  # type: ignore[index]
        self.assertFalse(settings["allowClear"])  # type: ignore[index]

    def test_only_an_explicit_case_insensitive_true_enables_one_stable_action(self) -> None:
        for value in ("true", "TRUE", " True "):
            with self.subTest(value=value):
                with patch.dict(os.environ, {_MAINTENANCE_ACTIONS_ENV: value}, clear=False):
                    alarm_config = _alarm_config()
                actions = alarm_config["actions"]["actionCellButton"]  # type: ignore[index]
                self.assertEqual(1, len(actions))
                self.assertEqual(_CREATE_WORK_ORDER_ACTION_ID, actions[0]["id"])

        for value in ("", "1", "yes", "enabled"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {_MAINTENANCE_ACTIONS_ENV: value}, clear=False):
                    self.assertEqual({}, _alarm_config()["actions"])

    def test_action_visibility_is_limited_to_approvable_active_pdm_risk(self) -> None:
        alarm_config = _alarm_config(enabled=True)
        action = alarm_config["actions"]["actionCellButton"][0]  # type: ignore[index]

        self.assertEqual("创建维护工单", action["name"])
        self.assertEqual("build", action["icon"])
        self.assertEqual("custom", action["type"])
        self.assertTrue(action["useShowWidgetActionFunction"])
        visibility = action["showWidgetActionFunction"]
        for required in (
            "PDM_FORECAST_RISK",
            "ACTIVE_UNACK",
            "ACTIVE_ACK",
            "risk_state === 'ACTIVE'",
            "maintenance_state === 'PENDING_APPROVAL'",
            "work_order_action_allowed === true",
            "details.alert_id",
            "details.maintenance_alert_version",
            "details.maintenance_alert_version >= 1",
        ):
            self.assertIn(required, visibility)

    def test_action_uses_preview_confirm_and_one_idempotent_relative_post(self) -> None:
        alarm_config = _alarm_config(enabled=True)
        action = alarm_config["actions"]["actionCellButton"][0]  # type: ignore[index]
        function = action["customFunction"]

        self.assertEqual(1, function.count("widgetContext.http.get("))
        self.assertEqual(1, function.count("widgetContext.http.post("))
        self.assertLess(
            function.index("widgetContext.http.get("),
            function.index("dialogs.confirm("),
        )
        self.assertLess(
            function.index("dialogs.confirm("),
            function.index("widgetContext.http.post("),
        )
        self.assertIn("'/api/v1/maintenance-alerts/' + alertId", function)
        self.assertIn("basePath + '/work-order-plan'", function)
        self.assertIn("basePath + '/actions'", function)
        self.assertIn("action: 'CREATE_WORK_ORDER'", function)
        self.assertIn("alarmVersion < 1", function)
        self.assertIn("expected_version: plan.expected_version", function)
        self.assertIn("plan.expected_version >= 1", function)
        self.assertIn("confirmed_plan_hash: plan.plan_hash", function)
        self.assertIn(
            "'Idempotency-Key': 'alert-action:' + alertId + ':CREATE_WORK_ORDER'",
            function,
        )
        self.assertIn("__pdmMaintenanceActionPending", function)
        self.assertIn("计划哈希：<code>' + plan.plan_hash", function)
        self.assertNotIn("retry", function.lower())

    def test_action_never_embeds_an_absolute_route_or_handles_session_identity(self) -> None:
        alarm_config = _alarm_config(enabled=True)
        action = alarm_config["actions"]["actionCellButton"][0]  # type: ignore[index]
        serialized = json.dumps(action, ensure_ascii=False)

        self.assertNotIn("http://", serialized)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("X-Authorization", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("Cookie", serialized)
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("window.fetch", serialized)
        self.assertNotIn("token", serialized.lower())
        _validate_artifact_tree(alarm_config, context="enabled dashboard action")


if __name__ == "__main__":
    unittest.main()
