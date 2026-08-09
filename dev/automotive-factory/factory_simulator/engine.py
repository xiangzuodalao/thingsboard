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
import math
import os
import random
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .api import ThingsBoardApi, load_device_credentials
from .config import AppConfig, DeviceDefinition, applicable_faults


class SimulatorError(RuntimeError):
    """Raised for a simulator lifecycle or publish failure."""


ALARM_RETRY_BASE_SECONDS = 2.0
ALARM_RETRY_MAX_SECONDS = 30.0
MQTT_RETRY_BASE_SECONDS = 2.0
MQTT_RETRY_MAX_SECONDS = 30.0


def _rounded(value: float, precision: int) -> int | float:
    return int(round(value)) if precision == 0 else round(value, precision)


def _initial_metric(spec: dict[str, Any], rng: random.Random) -> Any:
    generator = spec.get("generator", "walk")
    if generator in {"walk", "counter"}:
        return _rounded(float(spec["initial"]), int(spec.get("precision", 2)))
    if generator == "boolean":
        return rng.random() < float(spec["true_probability"])
    if generator == "choice":
        return rng.choice(spec["choices"])
    raise SimulatorError(f"Unsupported metric generator: {generator}")


def _next_metric(
    name: str,
    current: Any,
    spec: dict[str, Any],
    rng: random.Random,
    *,
    interval_seconds: float,
    running: bool,
) -> Any:
    generator = spec.get("generator", "walk")
    precision = int(spec.get("precision", 2))
    if generator == "walk":
        candidate = float(current) + rng.uniform(-float(spec["step"]), float(spec["step"]))
        candidate = min(float(spec["max"]), max(float(spec["min"]), candidate))
        return _rounded(candidate, precision)
    if generator == "counter":
        if name == "running_hours":
            increment = interval_seconds / 3600 if running else 0
        else:
            probability = float(spec.get("increment_probability", 1))
            increment = float(spec.get("increment", 1)) if rng.random() < probability else 0
        return _rounded(float(current) + increment, precision)
    if generator == "boolean":
        return rng.random() < float(spec["true_probability"])
    if generator == "choice":
        return rng.choice(spec["choices"])
    raise SimulatorError(f"Unsupported metric generator: {generator}")


@dataclass
class DeviceRuntime:
    definition: DeviceDefinition
    device_uuid: str
    access_token: str
    rng: random.Random
    metrics: dict[str, Any] = field(default_factory=dict)
    operating_state: str = "RUNNING"
    online: bool = True
    quality_status: str = "OK"
    alarm_code: str = "NONE"
    active_fault: str | None = None
    fault_until_monotonic: float | None = None
    fault_expires_at_ms: int | None = None
    alarm_id: str | None = None
    clear_requested: bool = False
    clear_source: str | None = None
    alarm_retry_count: int = 0
    alarm_retry_after_monotonic: float = 0.0
    connection_retry_count: int = 0
    connection_retry_after_monotonic: float = 0.0
    last_error: str | None = None
    sequence: int = 0
    client: Any = None
    connected: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.metrics = {
            name: _initial_metric(spec, self.rng)
            for name, spec in self.definition.metrics.items()
        }


class FactorySimulator:
    def __init__(self, config: AppConfig):
        self.config = config
        self.credentials = load_device_credentials(config)
        self.api = ThingsBoardApi(config)
        self.api.wait_until_ready()
        seed = int(config.simulation.get("random_seed", 0))
        self.rng = random.Random(seed)
        self.devices: dict[str, DeviceRuntime] = {}
        for offset, definition in enumerate(config.devices):
            credential = self.credentials[definition.name]
            self.devices[definition.name] = DeviceRuntime(
                definition=definition,
                device_uuid=credential["device_id"],
                access_token=credential["access_token"],
                rng=random.Random(seed + offset + 1),
            )
        self.stop_requested = threading.Event()
        self.command_dir = config.runtime_dir / "commands"
        self.status_path = config.runtime_dir / "status.json"
        self.alarm_state_path = config.runtime_dir / "active_alarms.json"
        self.pid_path = config.runtime_dir / "simulator.pid"
        self._load_alarm_state()

    def _load_alarm_state(self) -> None:
        if not self.alarm_state_path.is_file():
            return
        try:
            with self.alarm_state_path.open("r", encoding="utf-8") as stream:
                state = json.load(stream)
            if not isinstance(state, dict):
                raise ValueError("alarm state root must be a JSON object")
            now_ms = int(time.time() * 1000)
            now_monotonic = time.monotonic()
            for device_name, alarm in state.items():
                device = self.devices.get(device_name)
                if device and isinstance(alarm, dict):
                    active_fault = alarm.get("fault")
                    if active_fault not in self.config.fault_modes:
                        continue
                    fault = self.config.fault_modes[active_fault]
                    device.active_fault = active_fault
                    device.alarm_id = alarm.get("alarm_id")
                    expires_at_ms = alarm.get("expires_at_ms")
                    if expires_at_ms is not None:
                        expires_at_ms = int(expires_at_ms)
                        device.fault_expires_at_ms = expires_at_ms
                        remaining_seconds = max(0.0, (expires_at_ms - now_ms) / 1000)
                        device.fault_until_monotonic = now_monotonic + remaining_seconds
                    device.clear_requested = bool(alarm.get("clear_requested"))
                    device.clear_source = alarm.get("clear_source")
                    device.operating_state = fault["operating_state"]
                    device.quality_status = fault["quality_status"]
                    device.alarm_code = fault["alarm_code"]
                    device.online = not bool(fault.get("disconnect_mqtt"))
        except (OSError, ValueError, TypeError):
            print("Warning: ignored invalid active alarm state file")

    def _save_alarm_state(self) -> None:
        payload = {
            name: {
                "fault": device.active_fault,
                "alarm_id": device.alarm_id,
                "expires_at_ms": device.fault_expires_at_ms,
                "clear_requested": device.clear_requested,
                "clear_source": device.clear_source,
            }
            for name, device in self.devices.items()
            if device.active_fault or device.alarm_id
        }
        temporary = self.alarm_state_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        temporary.replace(self.alarm_state_path)

    @staticmethod
    def _alarm_uuid(alarm: dict[str, Any]) -> str | None:
        alarm_id = alarm.get("id")
        if isinstance(alarm_id, dict):
            value = alarm_id.get("id")
            return str(value) if value else None
        return str(alarm_id) if alarm_id else None

    @staticmethod
    def _fault_name_from_alarm(alarm: dict[str, Any]) -> str | None:
        alarm_type = alarm.get("type")
        if not isinstance(alarm_type, str) or not alarm_type.startswith("SIM_"):
            return None
        return alarm_type[4:]

    def _matching_alarm(
        self,
        alarms: list[dict[str, Any]],
        fault_name: str,
    ) -> dict[str, Any] | None:
        expected_type = f"SIM_{fault_name}"
        return next((alarm for alarm in alarms if alarm.get("type") == expected_type), None)

    def _record_device_error(
        self,
        device: DeviceRuntime,
        context: str,
        error: Exception | str,
    ) -> None:
        message = str(error)
        device.last_error = f"{context}: {message}"
        print(f"Warning: {device.definition.name} {context} failed: {message}")

    def _reset_alarm_retry(self, device: DeviceRuntime) -> None:
        device.alarm_retry_count = 0
        device.alarm_retry_after_monotonic = 0.0
        device.last_error = None

    def _schedule_alarm_retry(
        self,
        device: DeviceRuntime,
        context: str,
        error: Exception | str,
    ) -> None:
        device.alarm_retry_count += 1
        delay = min(
            ALARM_RETRY_MAX_SECONDS,
            ALARM_RETRY_BASE_SECONDS * (2 ** min(device.alarm_retry_count - 1, 4)),
        )
        device.alarm_retry_after_monotonic = time.monotonic() + delay
        self._record_device_error(device, context, error)
        self._save_alarm_state()

    def _fault_details(
        self,
        device: DeviceRuntime,
        fault_name: str,
        source: str,
    ) -> dict[str, Any]:
        fault = self.config.fault_modes[fault_name]
        return {
            "device_id": device.definition.name,
            "device_type": device.definition.device_type,
            "line_id": device.definition.line_id,
            "alarm_code": fault["alarm_code"],
            "fault_mode": fault_name,
            "label": fault.get("label", fault_name),
            "source": source,
            "simulated": True,
        }

    def _ensure_remote_alarm(
        self,
        device: DeviceRuntime,
        *,
        source: str,
    ) -> bool:
        fault_name = device.active_fault
        if fault_name is None:
            return True

        try:
            existing = self._matching_alarm(
                self.api.list_active_alarms(device.device_uuid),
                fault_name,
            )
            if existing is not None:
                alarm_id = self._alarm_uuid(existing)
                if not alarm_id:
                    raise SimulatorError("active alarm response has no alarm id")
                device.alarm_id = alarm_id
                self._reset_alarm_retry(device)
                self._save_alarm_state()
                return True
        except Exception as lookup_error:
            self._record_device_error(device, "active alarm lookup", lookup_error)

        fault = self.config.fault_modes[fault_name]
        try:
            alarm = self.api.create_or_update_alarm(
                device_id=device.device_uuid,
                alarm_type=f"SIM_{fault_name}",
                severity=fault["severity"],
                details=self._fault_details(device, fault_name, source),
            )
            alarm_id = self._alarm_uuid(alarm)
            if not alarm_id:
                raise SimulatorError("created alarm response has no alarm id")
            device.alarm_id = alarm_id
            self._reset_alarm_retry(device)
            self._save_alarm_state()
            return True
        except Exception as create_error:
            # The request may have reached ThingsBoard even if the HTTP response
            # was lost. Query by the deduplicated alarm type before retrying.
            try:
                recovered = self._matching_alarm(
                    self.api.list_active_alarms(device.device_uuid),
                    fault_name,
                )
                recovered_id = self._alarm_uuid(recovered) if recovered else None
                if recovered_id:
                    device.alarm_id = recovered_id
                    self._reset_alarm_retry(device)
                    self._save_alarm_state()
                    return True
            except Exception as reconcile_error:
                self._record_device_error(
                    device,
                    "alarm reconciliation",
                    reconcile_error,
                )
            self._schedule_alarm_retry(device, "alarm creation", create_error)
            return False

    def _restore_remote_fault(
        self,
        device: DeviceRuntime,
        fault_name: str,
        alarm_id: str,
    ) -> None:
        fault = self.config.fault_modes[fault_name]
        device.active_fault = fault_name
        device.alarm_id = alarm_id
        device.fault_until_monotonic = None
        device.fault_expires_at_ms = None
        device.clear_requested = False
        device.clear_source = None
        device.operating_state = fault["operating_state"]
        device.quality_status = fault["quality_status"]
        device.alarm_code = fault["alarm_code"]
        device.online = not bool(fault.get("disconnect_mqtt"))
        self._reset_alarm_retry(device)

    def _reset_fault_state(self, device: DeviceRuntime) -> None:
        device.active_fault = None
        device.fault_until_monotonic = None
        device.fault_expires_at_ms = None
        device.alarm_id = None
        device.clear_requested = False
        device.clear_source = None
        device.operating_state = "RUNNING"
        device.online = True
        device.quality_status = "OK"
        device.alarm_code = "NONE"
        self._reset_alarm_retry(device)

    def reconcile_alarm_state(self) -> None:
        """Reconcile the local durable fault state with active ThingsBoard alarms."""
        changed = False
        for device in self.devices.values():
            try:
                alarms = self.api.list_active_alarms(device.device_uuid)
            except Exception as exc:
                self._record_device_error(device, "startup alarm reconciliation", exc)
                continue

            if device.active_fault:
                matching = self._matching_alarm(alarms, device.active_fault)
                matching_id = self._alarm_uuid(matching) if matching else None
                if matching_id:
                    if device.alarm_id != matching_id:
                        device.alarm_id = matching_id
                        changed = True
                    self._reset_alarm_retry(device)
                elif device.clear_requested:
                    self._reset_fault_state(device)
                    changed = True
                else:
                    if device.alarm_id is not None:
                        device.alarm_id = None
                        changed = True
                    device.alarm_retry_after_monotonic = 0.0
                continue

            for alarm in alarms:
                fault_name = self._fault_name_from_alarm(alarm)
                alarm_id = self._alarm_uuid(alarm)
                if fault_name in self.config.fault_modes and alarm_id:
                    self._restore_remote_fault(device, fault_name, alarm_id)
                    changed = True
                    break

        if changed:
            self._save_alarm_state()

    def _new_mqtt_client(self, device: DeviceRuntime) -> Any:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise SimulatorError(
                "paho-mqtt is not installed. Run ./dev.sh prepare first."
            ) from exc

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"tb-factory-{device.definition.name}-{os.getpid()}",
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        client.username_pw_set(device.access_token)

        def on_connect(
            _client: Any,
            _userdata: Any,
            _flags: Any,
            reason_code: Any,
            _properties: Any,
        ) -> None:
            if reason_code == 0:
                device.connected.set()
            else:
                print(f"MQTT connect rejected for {device.definition.name}: {reason_code}")

        def on_disconnect(
            _client: Any,
            _userdata: Any,
            _disconnect_flags: Any,
            _reason_code: Any,
            _properties: Any,
        ) -> None:
            device.connected.clear()

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        return client

    def connect_device(self, device: DeviceRuntime, timeout_seconds: float = 10) -> None:
        if device.client is None:
            device.client = self._new_mqtt_client(device)
            device.client.connect(
                self.config.mqtt["host"],
                int(self.config.mqtt["port"]),
                int(self.config.mqtt.get("keepalive_seconds", 60)),
            )
            device.client.loop_start()
        else:
            device.client.reconnect()
        if not device.connected.wait(timeout_seconds):
            raise SimulatorError(f"MQTT connection timed out for {device.definition.name}")

    def connect_all(self) -> None:
        connected_count = 0
        for device in self.devices.values():
            if not device.online:
                continue
            if self._connect_device_safely(device):
                connected_count += 1
        print(f"Connected {connected_count} MQTT device clients with QoS 1.")

    @staticmethod
    def disconnect_device(device: DeviceRuntime) -> None:
        if device.client is None:
            return
        client = device.client
        try:
            client.disconnect()
        finally:
            try:
                client.loop_stop()
            finally:
                device.client = None
                device.connected.clear()

    def _discard_mqtt_client(self, device: DeviceRuntime) -> None:
        try:
            self.disconnect_device(device)
        except Exception as exc:
            # A broken Paho client must never remain attached: connect_device()
            # would otherwise keep trying to reconnect an unusable instance.
            device.client = None
            device.connected.clear()
            self._record_device_error(device, "MQTT client cleanup", exc)

    def _reset_connection_retry(self, device: DeviceRuntime) -> None:
        device.connection_retry_count = 0
        device.connection_retry_after_monotonic = 0.0

    def _schedule_connection_retry(
        self,
        device: DeviceRuntime,
        context: str,
        error: Exception | str,
    ) -> None:
        device.connection_retry_count += 1
        delay = min(
            MQTT_RETRY_MAX_SECONDS,
            MQTT_RETRY_BASE_SECONDS * (2 ** min(device.connection_retry_count - 1, 4)),
        )
        device.connection_retry_after_monotonic = time.monotonic() + delay
        self._record_device_error(device, context, error)

    def _connect_device_safely(self, device: DeviceRuntime) -> bool:
        if not device.online:
            return False
        if device.connected.is_set():
            self._reset_connection_retry(device)
            return True
        if time.monotonic() < device.connection_retry_after_monotonic:
            return False
        try:
            self.connect_device(device)
        except Exception as exc:
            self._discard_mqtt_client(device)
            self._schedule_connection_retry(device, "MQTT connection", exc)
            return False
        self._reset_connection_retry(device)
        device.last_error = None
        return True

    def process_device_connections(self) -> None:
        for device in self.devices.values():
            try:
                self._connect_device_safely(device)
            except Exception as exc:
                # Keep a programming or third-party client error scoped to the
                # affected device. The next cycle will retry it.
                self._schedule_connection_retry(device, "MQTT recovery", exc)

    def disconnect_all(self) -> None:
        for device in self.devices.values():
            if device.client is not None:
                try:
                    self.disconnect_device(device)
                except Exception as exc:
                    print(f"Warning: MQTT disconnect failed for {device.definition.name}: {exc}")

    def _choose_normal_state(self) -> str:
        weights = self.config.simulation["normal_state_weights"]
        return self.rng.choices(list(weights), weights=list(weights.values()), k=1)[0]

    def update_normal_metrics(self, device: DeviceRuntime) -> bool:
        state_changed = False
        if device.active_fault is None and self.rng.random() < float(
            self.config.simulation.get("state_change_probability", 0)
        ):
            new_state = self._choose_normal_state()
            if new_state != device.operating_state:
                device.operating_state = new_state
                state_changed = True
        running = device.operating_state == "RUNNING"
        interval = float(self.config.simulation["interval_seconds"])
        for name, spec in device.definition.metrics.items():
            device.metrics[name] = _next_metric(
                name,
                device.metrics[name],
                spec,
                device.rng,
                interval_seconds=interval,
                running=running,
            )
        return state_changed

    def payload_values(
        self,
        device: DeviceRuntime,
        *,
        event_type: str | None = None,
        fault_mode: str | None = None,
    ) -> dict[str, Any]:
        timestamp = int(time.time() * 1000)
        values: dict[str, Any] = {
            "timestamp": timestamp,
            "device_id": device.definition.name,
            "device_type": device.definition.device_type,
            "line_id": device.definition.line_id,
            "operating_state": device.operating_state,
            "online": device.online,
            "alarm_code": device.alarm_code,
            "quality_status": device.quality_status,
            **device.metrics,
        }
        if device.active_fault:
            fault = self.config.fault_modes[device.active_fault]
            for key, value in fault.get("overrides", {}).items():
                if key in values:
                    values[key] = value
        if event_type:
            values.update(
                {
                    "event_type": event_type,
                    "event_id": str(uuid.uuid4()),
                    "fault_mode": fault_mode or device.active_fault or "NONE",
                }
            )
        device.sequence += 1
        values["sequence"] = device.sequence
        return {"ts": timestamp, "values": values}

    def publish(
        self,
        device: DeviceRuntime,
        *,
        event_type: str | None = None,
        fault_mode: str | None = None,
    ) -> bool:
        if not device.connected.is_set():
            return False
        payload = self.payload_values(device, event_type=event_type, fault_mode=fault_mode)
        info = device.client.publish(
            self.config.mqtt["telemetry_topic"],
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            qos=int(self.config.mqtt["qos"]),
            retain=False,
        )
        info.wait_for_publish(timeout=float(self.config.mqtt.get("publish_timeout_seconds", 5)))
        if not info.is_published():
            raise SimulatorError(f"QoS 1 publish was not acknowledged for {device.definition.name}")
        return True

    def _safe_publish(
        self,
        device: DeviceRuntime,
        *,
        event_type: str | None = None,
        fault_mode: str | None = None,
    ) -> bool:
        try:
            return self.publish(
                device,
                event_type=event_type,
                fault_mode=fault_mode,
            )
        except Exception as exc:
            self._record_device_error(device, "MQTT publish", exc)
            self._discard_mqtt_client(device)
            self._schedule_connection_retry(device, "MQTT reconnect", exc)
            return False

    def raise_fault(
        self,
        device_name: str,
        fault_name: str,
        duration_seconds: float | None,
        *,
        replace: bool = False,
        source: str = "manual",
    ) -> None:
        if device_name not in self.devices:
            raise SimulatorError(f"Unknown device '{device_name}'")
        if fault_name not in self.config.fault_modes:
            raise SimulatorError(f"Unknown fault mode '{fault_name}'")
        device = self.devices[device_name]
        fault = self.config.fault_modes[fault_name]
        applies_to = fault["applies_to"]
        if "*" not in applies_to and device.definition.device_type not in applies_to:
            raise SimulatorError(
                f"Fault {fault_name} does not apply to {device.definition.device_type}"
            )
        if device.active_fault:
            if not replace:
                raise SimulatorError(
                    f"{device_name} already has active fault {device.active_fault}; "
                    "clear it first or use --replace"
                )
            if not self.clear_fault(device_name, source="replace"):
                raise SimulatorError(
                    f"{device_name} previous fault clear is pending; replacement was not applied"
                )

        device.active_fault = fault_name
        device.operating_state = fault["operating_state"]
        device.quality_status = fault["quality_status"]
        device.alarm_code = fault["alarm_code"]
        device.online = True
        device.alarm_id = None
        device.clear_requested = False
        device.clear_source = None
        self._reset_alarm_retry(device)
        if duration_seconds is not None and duration_seconds > 0:
            device.fault_until_monotonic = time.monotonic() + duration_seconds
            device.fault_expires_at_ms = int(time.time() * 1000 + duration_seconds * 1000)
        else:
            device.fault_until_monotonic = None
            device.fault_expires_at_ms = None

        # Persist intent before either network operation. A crash or response
        # timeout can then be reconciled without losing the fault command.
        self._save_alarm_state()
        self._safe_publish(device, event_type="FAULT_RAISED", fault_mode=fault_name)
        self._ensure_remote_alarm(device, source=source)
        if fault.get("disconnect_mqtt"):
            device.online = False
            self.disconnect_device(device)
        self._save_alarm_state()
        alarm_status = device.alarm_id or "pending"
        print(
            f"FAULT_RAISED {device_name} {fault_name} "
            f"source={source} alarm={alarm_status}"
        )

    def _finalize_fault_clear(
        self,
        device: DeviceRuntime,
        previous_fault: str | None,
        *,
        source: str,
        publish_event: bool = True,
    ) -> None:
        self._reset_fault_state(device)
        # Commit the cleared state before reconnecting or publishing. A failed
        # MQTT event must not resurrect an alarm that ThingsBoard has cleared.
        self._save_alarm_state()
        if publish_event:
            self._connect_device_safely(device)
            self._safe_publish(
                device,
                event_type="FAULT_CLEARED",
                fault_mode=previous_fault,
            )
        print(
            f"FAULT_CLEARED {device.definition.name} "
            f"{previous_fault or 'UNKNOWN'} source={source}"
        )

    def clear_fault(self, device_name: str, *, source: str = "manual") -> bool:
        if device_name not in self.devices:
            raise SimulatorError(f"Unknown device '{device_name}'")
        device = self.devices[device_name]
        previous_fault = device.active_fault
        if previous_fault is None and device.alarm_id is None:
            raise SimulatorError(f"{device_name} has no active fault")

        device.clear_requested = True
        device.clear_source = source
        self._save_alarm_state()

        if not device.alarm_id and previous_fault:
            try:
                matching = self._matching_alarm(
                    self.api.list_active_alarms(device.device_uuid),
                    previous_fault,
                )
            except Exception as exc:
                self._schedule_alarm_retry(device, "alarm lookup before clear", exc)
                return False
            device.alarm_id = self._alarm_uuid(matching) if matching else None
            self._save_alarm_state()

        if not device.alarm_id:
            self._finalize_fault_clear(device, previous_fault, source=source)
            return True

        alarm_id = device.alarm_id
        try:
            self.api.clear_alarm(alarm_id)
        except Exception as clear_error:
            # A timeout may mean ThingsBoard cleared the alarm but its response
            # was lost. Only discard local state after verifying it is absent.
            try:
                matching = (
                    self._matching_alarm(
                        self.api.list_active_alarms(device.device_uuid),
                        previous_fault,
                    )
                    if previous_fault
                    else None
                )
            except Exception as reconcile_error:
                self._record_device_error(
                    device,
                    "alarm clear reconciliation",
                    reconcile_error,
                )
                self._schedule_alarm_retry(device, "alarm clear", clear_error)
                return False
            if matching is not None:
                device.alarm_id = self._alarm_uuid(matching) or alarm_id
                self._schedule_alarm_retry(device, "alarm clear", clear_error)
                return False

        self._finalize_fault_clear(device, previous_fault, source=source)
        return True

    def maybe_raise_automatic_fault(self, device: DeviceRuntime) -> None:
        automatic = self.config.simulation["automatic_faults"]
        if not automatic.get("enabled") or device.active_fault:
            return
        if self.rng.random() >= float(automatic["probability_per_device_cycle"]):
            return
        candidates = applicable_faults(self.config, device.definition.device_type)
        if not candidates:
            return
        duration_config = automatic["duration_seconds"]
        duration = self.rng.uniform(
            float(duration_config["min"]),
            float(duration_config["max"]),
        )
        self.raise_fault(
            device.definition.name,
            self.rng.choice(candidates),
            duration,
            source="automatic",
        )

    def process_expired_faults(self) -> None:
        now = time.monotonic()
        for device in self.devices.values():
            try:
                if (
                    device.clear_requested
                    and now >= device.alarm_retry_after_monotonic
                ):
                    self.clear_fault(
                        device.definition.name,
                        source=device.clear_source or "recovery",
                    )
                elif (
                    device.active_fault
                    and device.fault_until_monotonic is not None
                    and now >= device.fault_until_monotonic
                ):
                    self.clear_fault(device.definition.name, source="timeout")
                elif (
                    device.active_fault
                    and device.alarm_id is None
                    and now >= device.alarm_retry_after_monotonic
                ):
                    self._ensure_remote_alarm(device, source="recovery")
            except Exception as exc:
                self._schedule_alarm_retry(device, "fault processing", exc)

    def process_telemetry_cycle(self) -> None:
        for device in self.devices.values():
            try:
                state_changed = self.update_normal_metrics(device)
                if state_changed:
                    self._safe_publish(device, event_type="STATE_CHANGED")
                if device.online:
                    self._safe_publish(device)
                self.maybe_raise_automatic_fault(device)
            except Exception as exc:
                self._record_device_error(device, "telemetry cycle", exc)

    def process_commands(self) -> None:
        self.command_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.command_dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    command = json.load(stream)
                action = command.get("action")
                if action == "fault":
                    self.raise_fault(
                        command["device"],
                        command["fault"],
                        command.get("duration_seconds"),
                        replace=bool(command.get("replace")),
                    )
                elif action == "clear":
                    self.clear_fault(command["device"])
                else:
                    raise SimulatorError(f"Unsupported command action '{action}'")
            except Exception as exc:
                print(f"Command {path.name} failed: {exc}")
            finally:
                path.unlink(missing_ok=True)

    def write_status(self) -> None:
        payload = {
            "updated_at": int(time.time() * 1000),
            "pid": os.getpid(),
            "device_count": len(self.devices),
            "connected_count": sum(
                1 for device in self.devices.values() if device.connected.is_set()
            ),
            "active_fault_count": sum(
                1 for device in self.devices.values() if device.active_fault
            ),
            "devices": {
                name: {
                    "device_type": device.definition.device_type,
                    "line_id": device.definition.line_id,
                    "connected": device.connected.is_set(),
                    "operating_state": device.operating_state,
                    "online": device.online,
                    "active_fault": device.active_fault,
                    "alarm_id": device.alarm_id,
                    "clear_requested": device.clear_requested,
                    "fault_expires_at_ms": device.fault_expires_at_ms,
                    "alarm_code": device.alarm_code,
                    "quality_status": device.quality_status,
                    "sequence": device.sequence,
                    "last_error": device.last_error,
                }
                for name, device in self.devices.items()
            },
        }
        temporary = self.status_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(self.status_path)

    def request_stop(self, *_args: Any) -> None:
        self.stop_requested.set()

    def _acquire_pid(self) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.pid_path.is_file():
            try:
                existing_pid = int(self.pid_path.read_text(encoding="utf-8").strip())
                os.kill(existing_pid, 0)
            except (ValueError, ProcessLookupError):
                self.pid_path.unlink(missing_ok=True)
            except PermissionError as exc:
                raise SimulatorError(f"Simulator PID {existing_pid} is not inspectable") from exc
            else:
                raise SimulatorError(f"Simulator is already running with PID {existing_pid}")
        self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def run(self) -> None:
        self._acquire_pid()
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        interval = float(self.config.simulation["interval_seconds"])
        self.command_dir.mkdir(parents=True, exist_ok=True)
        for stale_command in self.command_dir.glob("*.json"):
            stale_command.unlink()
        next_publish = time.monotonic()
        try:
            self.reconcile_alarm_state()
            self.connect_all()
            for device in self.devices.values():
                if device.online:
                    self._safe_publish(device, event_type="SIMULATOR_STARTED")
            self.write_status()
            while not self.stop_requested.is_set():
                self.process_commands()
                self.process_expired_faults()
                self.process_device_connections()
                now = time.monotonic()
                if now >= next_publish:
                    self.process_telemetry_cycle()
                    self.write_status()
                    skipped = max(0, math.floor((now - next_publish) / interval))
                    next_publish += interval * (skipped + 1)
                self.stop_requested.wait(min(0.2, max(0.01, next_publish - time.monotonic())))
        finally:
            for device in self.devices.values():
                if device.connected.is_set():
                    device.online = False
                    self._safe_publish(device, event_type="SIMULATOR_STOPPED")
            self.disconnect_all()
            self.write_status()
            self.pid_path.unlink(missing_ok=True)
            print("Simulator stopped.")


def enqueue_command(config: AppConfig, command: dict[str, Any]) -> Path:
    command_dir = config.runtime_dir / "commands"
    command_dir.mkdir(parents=True, exist_ok=True)
    command_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
    temporary = command_dir / f".{command_id}.tmp"
    destination = command_dir / f"{command_id}.json"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(command, stream, ensure_ascii=False)
        stream.write("\n")
    temporary.replace(destination)
    return destination


def simulator_is_running(config: AppConfig) -> tuple[bool, int | None]:
    pid_path = config.runtime_dir / "simulator.pid"
    if not pid_path.is_file():
        return False, None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True, pid
    except (ValueError, ProcessLookupError):
        return False, None
