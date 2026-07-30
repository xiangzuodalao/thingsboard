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
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory_simulator.api import ThingsBoardApiError
from factory_simulator.config import applicable_faults, load_config
from factory_simulator.dashboard import _dashboard_configuration
from factory_simulator.engine import DeviceRuntime, FactorySimulator, _next_metric


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yml"

COMMON_FIELDS = {
    "timestamp",
    "device_id",
    "device_type",
    "line_id",
    "operating_state",
    "online",
    "load_pct",
    "temperature",
    "vibration_rms",
    "current",
    "voltage",
    "power",
    "cycle_time",
    "running_hours",
    "alarm_code",
    "quality_status",
}

TYPE_FIELDS = {
    "CNC": {
        "spindle_speed",
        "spindle_load",
        "spindle_temperature",
        "tool_usage_count",
    },
    "INJECTION_MOLDING": {
        "injection_pressure",
        "mold_temperature",
        "clamping_force",
        "reject_rate",
    },
    "ASSEMBLY_ROBOT": {
        "joint_temperature",
        "joint_current",
        "position_deviation",
        "collision_count",
    },
    "TIGHTENING": {"torque", "angle", "tightening_time", "result_ok"},
    "AIR_COMPRESSOR": {
        "discharge_pressure",
        "discharge_temperature",
        "air_flow",
    },
    "EOL_TESTER": {
        "test_duration",
        "pass_rate",
        "measured_value",
        "calibration_status",
    },
}


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG_PATH)

    def test_default_topology_has_two_lines_and_twenty_devices(self) -> None:
        self.assertEqual(20, len(self.config.devices))
        by_line: dict[str, int] = {}
        for device in self.config.devices:
            by_line[device.line_id] = by_line.get(device.line_id, 0) + 1
        self.assertEqual({"LINE-A": 10, "LINE-B": 10}, by_line)
        self.assertEqual(
            "LINE-A-CNC-01",
            next(device.name for device in self.config.devices if device.device_type == "CNC"),
        )

    def test_every_device_has_common_and_type_specific_metrics(self) -> None:
        metric_common = COMMON_FIELDS - {
            "timestamp",
            "device_id",
            "device_type",
            "line_id",
            "operating_state",
            "online",
            "alarm_code",
            "quality_status",
        }
        for device in self.config.devices:
            with self.subTest(device=device.name):
                self.assertTrue(metric_common <= set(device.metrics))
                self.assertTrue(TYPE_FIELDS[device.device_type] <= set(device.metrics))

    def test_every_device_type_has_an_applicable_fault(self) -> None:
        for device_type in TYPE_FIELDS:
            with self.subTest(device_type=device_type):
                self.assertTrue(applicable_faults(self.config, device_type))


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG_PATH)

    def _runtime(self, offset: int = 0) -> DeviceRuntime:
        definition = self.config.devices[offset]
        return DeviceRuntime(
            definition=definition,
            device_uuid=f"uuid-{offset}",
            access_token=f"token-{offset}",
            rng=random.Random(offset + 1),
        )

    def _simulator(
        self,
        runtime_dir: Path,
        devices: list[DeviceRuntime],
        api: object,
    ) -> FactorySimulator:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        simulator = object.__new__(FactorySimulator)
        simulator.config = self.config
        simulator.api = api
        simulator.rng = random.Random(100)
        simulator.devices = {
            device.definition.name: device
            for device in devices
        }
        simulator.alarm_state_path = runtime_dir / "active_alarms.json"
        return simulator

    def _activate_fault(
        self,
        runtime: DeviceRuntime,
        fault_name: str = "HIGH_TEMPERATURE",
        alarm_id: str | None = "alarm-1",
    ) -> None:
        fault = self.config.fault_modes[fault_name]
        runtime.active_fault = fault_name
        runtime.alarm_id = alarm_id
        runtime.operating_state = fault["operating_state"]
        runtime.quality_status = fault["quality_status"]
        runtime.alarm_code = fault["alarm_code"]

    def test_payload_uses_thingsboard_timestamp_envelope_and_complete_fields(self) -> None:
        simulator = object.__new__(FactorySimulator)
        simulator.config = self.config
        for offset, definition in enumerate(self.config.devices):
            runtime = DeviceRuntime(
                definition=definition,
                device_uuid=f"uuid-{offset}",
                access_token=f"token-{offset}",
                rng=random.Random(offset),
            )
            payload = simulator.payload_values(runtime)
            with self.subTest(device=definition.name):
                self.assertEqual(payload["ts"], payload["values"]["timestamp"])
                self.assertTrue(COMMON_FIELDS <= set(payload["values"]))
                self.assertTrue(TYPE_FIELDS[definition.device_type] <= set(payload["values"]))
                self.assertEqual(1, payload["values"]["sequence"])

    def test_fault_overrides_are_data_driven(self) -> None:
        definition = next(
            device for device in self.config.devices if device.device_type == "CNC"
        )
        runtime = DeviceRuntime(
            definition=definition,
            device_uuid="uuid",
            access_token="token",
            rng=random.Random(1),
        )
        fault = self.config.fault_modes["HIGH_TEMPERATURE"]
        runtime.active_fault = "HIGH_TEMPERATURE"
        runtime.operating_state = fault["operating_state"]
        runtime.quality_status = fault["quality_status"]
        runtime.alarm_code = fault["alarm_code"]

        simulator = object.__new__(FactorySimulator)
        simulator.config = self.config
        values = simulator.payload_values(
            runtime,
            event_type="FAULT_RAISED",
            fault_mode="HIGH_TEMPERATURE",
        )["values"]

        self.assertEqual(105, values["temperature"])
        self.assertEqual("TEMP_HIGH", values["alarm_code"])
        self.assertEqual("FAULT_RAISED", values["event_type"])
        self.assertEqual("HIGH_TEMPERATURE", values["fault_mode"])

    def test_running_hours_only_advance_while_running(self) -> None:
        spec = {"generator": "counter", "initial": 10, "precision": 4}
        rng = random.Random(1)
        stopped = _next_metric(
            "running_hours",
            10,
            spec,
            rng,
            interval_seconds=2,
            running=False,
        )
        running = _next_metric(
            "running_hours",
            10,
            spec,
            rng,
            interval_seconds=2,
            running=True,
        )
        self.assertEqual(10, stopped)
        self.assertGreater(running, stopped)

    def test_intentional_disconnect_discards_stopped_paho_client(self) -> None:
        definition = self.config.devices[0]
        runtime = DeviceRuntime(
            definition=definition,
            device_uuid="uuid",
            access_token="token",
            rng=random.Random(1),
        )

        class FakeClient:
            disconnected = False
            loop_stopped = False

            def disconnect(self) -> None:
                self.disconnected = True

            def loop_stop(self) -> None:
                self.loop_stopped = True

        client = FakeClient()
        runtime.client = client
        runtime.connected.set()

        FactorySimulator.disconnect_device(runtime)

        self.assertTrue(client.disconnected)
        self.assertTrue(client.loop_stopped)
        self.assertIsNone(runtime.client)
        self.assertFalse(runtime.connected.is_set())

    def test_fault_duration_survives_a_simulator_restart(self) -> None:
        class FakeApi:
            @staticmethod
            def list_active_alarms(_device_id: str) -> list[dict[str, object]]:
                return []

            @staticmethod
            def create_or_update_alarm(**_kwargs: object) -> dict[str, object]:
                return {"id": {"id": "alarm-1"}}

        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            runtime = self._runtime()
            simulator = self._simulator(runtime_dir, [runtime], FakeApi())
            simulator._safe_publish = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

            with (
                patch("factory_simulator.engine.time.time", return_value=1_000.0),
                patch("factory_simulator.engine.time.monotonic", return_value=500.0),
            ):
                simulator.raise_fault(
                    runtime.definition.name,
                    "HIGH_TEMPERATURE",
                    30,
                )

            state = json.loads(
                simulator.alarm_state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                1_030_000,
                state[runtime.definition.name]["expires_at_ms"],
            )

            restored = self._runtime()
            restarted = self._simulator(runtime_dir, [restored], FakeApi())
            with (
                patch("factory_simulator.engine.time.time", return_value=1_005.0),
                patch("factory_simulator.engine.time.monotonic", return_value=700.0),
            ):
                restarted._load_alarm_state()

            self.assertEqual("HIGH_TEMPERATURE", restored.active_fault)
            self.assertEqual(1_030_000, restored.fault_expires_at_ms)
            self.assertEqual(725.0, restored.fault_until_monotonic)

    def test_alarm_create_timeout_is_reconciled_without_duplicate_state(self) -> None:
        alarm = {
            "id": {"id": "alarm-recovered"},
            "type": "SIM_HIGH_TEMPERATURE",
        }

        class FakeApi:
            lookup_count = 0

            def list_active_alarms(self, _device_id: str) -> list[dict[str, object]]:
                self.lookup_count += 1
                return [] if self.lookup_count == 1 else [alarm]

            @staticmethod
            def create_or_update_alarm(**_kwargs: object) -> dict[str, object]:
                raise ThingsBoardApiError("response timed out")

        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime()
            self._activate_fault(runtime, alarm_id=None)
            simulator = self._simulator(Path(directory), [runtime], FakeApi())

            self.assertTrue(simulator._ensure_remote_alarm(runtime, source="manual"))
            self.assertEqual("alarm-recovered", runtime.alarm_id)
            self.assertEqual(0, runtime.alarm_retry_count)

    def test_failed_alarm_clear_remains_durable_and_retries(self) -> None:
        alarm = {
            "id": {"id": "alarm-1"},
            "type": "SIM_HIGH_TEMPERATURE",
        }

        class FakeApi:
            @staticmethod
            def clear_alarm(_alarm_id: str) -> None:
                raise ThingsBoardApiError("service unavailable")

            @staticmethod
            def list_active_alarms(_device_id: str) -> list[dict[str, object]]:
                return [alarm]

        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime()
            self._activate_fault(runtime)
            simulator = self._simulator(Path(directory), [runtime], FakeApi())

            self.assertFalse(simulator.clear_fault(runtime.definition.name))
            self.assertEqual("HIGH_TEMPERATURE", runtime.active_fault)
            self.assertEqual("alarm-1", runtime.alarm_id)
            self.assertTrue(runtime.clear_requested)
            self.assertGreater(runtime.alarm_retry_after_monotonic, 0)
            state = json.loads(
                simulator.alarm_state_path.read_text(encoding="utf-8")
            )
            self.assertTrue(
                state[runtime.definition.name]["clear_requested"]
            )

    def test_clear_timeout_with_absent_remote_alarm_finishes_locally(self) -> None:
        class FakeApi:
            @staticmethod
            def clear_alarm(_alarm_id: str) -> None:
                raise ThingsBoardApiError("response timed out")

            @staticmethod
            def list_active_alarms(_device_id: str) -> list[dict[str, object]]:
                return []

        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime()
            self._activate_fault(runtime)
            simulator = self._simulator(Path(directory), [runtime], FakeApi())
            simulator._connect_device_safely = lambda _device: False  # type: ignore[method-assign]
            simulator._safe_publish = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

            self.assertTrue(simulator.clear_fault(runtime.definition.name))
            self.assertIsNone(runtime.active_fault)
            self.assertIsNone(runtime.alarm_id)
            self.assertFalse(runtime.clear_requested)
            self.assertEqual(
                {},
                json.loads(simulator.alarm_state_path.read_text(encoding="utf-8")),
            )

    def test_startup_reconciliation_restores_remote_simulator_alarm(self) -> None:
        alarm = {
            "id": {"id": "alarm-orphan"},
            "type": "SIM_HIGH_TEMPERATURE",
        }

        class FakeApi:
            @staticmethod
            def list_active_alarms(_device_id: str) -> list[dict[str, object]]:
                return [alarm]

        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime()
            simulator = self._simulator(Path(directory), [runtime], FakeApi())

            simulator.reconcile_alarm_state()

            self.assertEqual("HIGH_TEMPERATURE", runtime.active_fault)
            self.assertEqual("alarm-orphan", runtime.alarm_id)
            self.assertEqual("TEMP_HIGH", runtime.alarm_code)

    def test_one_device_failure_does_not_stop_other_telemetry(self) -> None:
        first = self._runtime(0)
        second = self._runtime(1)

        with tempfile.TemporaryDirectory() as directory:
            simulator = self._simulator(Path(directory), [first, second], object())
            published: list[str] = []

            def update(device: DeviceRuntime) -> bool:
                if device is first:
                    raise RuntimeError("broken metric")
                return False

            simulator.update_normal_metrics = update  # type: ignore[method-assign]
            simulator._safe_publish = (  # type: ignore[method-assign]
                lambda device, **_kwargs: published.append(device.definition.name) or True
            )
            simulator.maybe_raise_automatic_fault = (  # type: ignore[method-assign]
                lambda _device: None
            )

            simulator.process_telemetry_cycle()

            self.assertIn("telemetry cycle", first.last_error or "")
            self.assertEqual([second.definition.name], published)


class DashboardTests(unittest.TestCase):
    def test_dashboard_contains_all_devices_and_three_widget_types(self) -> None:
        widget_types = {
            "system.cards.entities_table": ("latest", {"settings": {}}),
            "system.time_series_chart": ("timeseries", {"settings": {}}),
            "system.alarm_widgets.alarms_table": (
                "alarm",
                {"settings": {}, "alarmSource": {"dataKeys": []}},
            ),
        }

        class FakeApi:
            @staticmethod
            def get_widget_type(fqn: str) -> dict[str, object]:
                widget_type, default_config = widget_types[fqn]
                return {
                    "descriptor": {
                        "type": widget_type,
                        "defaultConfig": default_config,
                    }
                }

        device_ids = [f"device-{index}" for index in range(20)]
        configuration = _dashboard_configuration(FakeApi(), device_ids)  # type: ignore[arg-type]

        self.assertEqual(3, len(configuration["widgets"]))
        alias = next(iter(configuration["entityAliases"].values()))
        self.assertEqual(device_ids, alias["filter"]["entityList"])
        self.assertEqual(
            {"latest", "timeseries", "alarm"},
            {widget["type"] for widget in configuration["widgets"].values()},
        )
        table_widget = next(
            widget
            for widget in configuration["widgets"].values()
            if widget["typeFullFqn"] == "system.cards.entities_table"
        )
        table_keys = {
            (key["name"], key["type"])
            for key in table_widget["config"]["datasources"][0]["dataKeys"]
        }
        self.assertIn(("equipment_id", "attribute"), table_keys)
        self.assertIn(("cmms_asset_id", "attribute"), table_keys)
        alarm_widget = next(
            widget
            for widget in configuration["widgets"].values()
            if widget["typeFullFqn"] == "system.alarm_widgets.alarms_table"
        )
        self.assertEqual({}, alarm_widget["config"]["actions"])


if __name__ == "__main__":
    unittest.main()
