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

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the simulator configuration is invalid."""


@dataclass(frozen=True)
class DeviceDefinition:
    name: str
    label: str
    device_type: str
    line_id: str
    index: int
    metrics: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class AppConfig:
    path: Path
    raw: dict[str, Any]
    devices: tuple[DeviceDefinition, ...]

    @property
    def root_dir(self) -> Path:
        return self.path.parent

    @property
    def runtime_dir(self) -> Path:
        return self.root_dir / ".runtime"

    @property
    def thingsboard(self) -> dict[str, Any]:
        return self.raw["thingsboard"]

    @property
    def mqtt(self) -> dict[str, Any]:
        return self.raw["mqtt"]

    @property
    def simulation(self) -> dict[str, Any]:
        return self.raw["simulation"]

    @property
    def fault_modes(self) -> dict[str, dict[str, Any]]:
        return self.raw["fault_modes"]

    def tenant_credentials(self) -> tuple[str, str]:
        username_env = self.thingsboard["tenant_username_env"]
        password_env = self.thingsboard["tenant_password_env"]
        username = os.environ.get(username_env)
        password = os.environ.get(password_env)
        if not username or not password:
            raise ConfigError(
                f"Missing tenant credentials. Set {username_env} and {password_env} "
                "in dev/automotive-factory/.env."
            )
        return username, password


def _required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"Missing '{key}' in {context}")
    return mapping[key]


def _validate_metric(name: str, spec: dict[str, Any], context: str) -> None:
    generator = spec.get("generator", "walk")
    if generator == "walk":
        for key in ("min", "max", "initial", "step"):
            _required(spec, key, f"{context}.{name}")
        if spec["min"] > spec["max"]:
            raise ConfigError(f"{context}.{name}: min must not exceed max")
        if not spec["min"] <= spec["initial"] <= spec["max"]:
            raise ConfigError(f"{context}.{name}: initial must be within min/max")
    elif generator == "counter":
        _required(spec, "initial", f"{context}.{name}")
    elif generator == "boolean":
        probability = float(_required(spec, "true_probability", f"{context}.{name}"))
        if not 0 <= probability <= 1:
            raise ConfigError(f"{context}.{name}: true_probability must be between 0 and 1")
    elif generator == "choice":
        choices = _required(spec, "choices", f"{context}.{name}")
        if not isinstance(choices, list) or not choices:
            raise ConfigError(f"{context}.{name}: choices must be a non-empty list")
    else:
        raise ConfigError(f"{context}.{name}: unsupported generator '{generator}'")


def _expand_devices(raw: dict[str, Any]) -> tuple[DeviceDefinition, ...]:
    device_types = _required(raw, "device_types", "root")
    common_metrics = _required(raw, "common_metrics", "root")
    for name, spec in common_metrics.items():
        _validate_metric(name, spec, "common_metrics")

    devices: list[DeviceDefinition] = []
    seen_names: set[str] = set()
    for line in _required(raw, "lines", "root"):
        line_id = str(_required(line, "id", "line")).strip()
        if not line_id:
            raise ConfigError("line.id must not be empty")
        counts = _required(line, "devices", f"line {line_id}")
        for device_type, count_value in counts.items():
            if device_type not in device_types:
                raise ConfigError(f"Line {line_id} references unknown device type '{device_type}'")
            count = int(count_value)
            if count < 0:
                raise ConfigError(f"Line {line_id} has a negative count for '{device_type}'")
            type_config = device_types[device_type]
            label = _required(type_config, "label", f"device_types.{device_type}")
            type_metrics = _required(type_config, "metrics", f"device_types.{device_type}")
            for name, spec in type_metrics.items():
                _validate_metric(name, spec, f"device_types.{device_type}.metrics")
            metric_specs = {**common_metrics, **type_metrics}
            for index in range(1, count + 1):
                name = f"{line_id}-{device_type}-{index:02d}"
                if name in seen_names:
                    raise ConfigError(f"Duplicate expanded device name '{name}'")
                seen_names.add(name)
                devices.append(
                    DeviceDefinition(
                        name=name,
                        label=f"{line_id} {label} {index:02d}",
                        device_type=device_type,
                        line_id=line_id,
                        index=index,
                        metrics=metric_specs,
                    )
                )
    if not devices:
        raise ConfigError("Configuration must expand to at least one device")
    return tuple(devices)


def _validate_faults(raw: dict[str, Any], devices: tuple[DeviceDefinition, ...]) -> None:
    known_types = {device.device_type for device in devices}
    faults = _required(raw, "fault_modes", "root")
    if not faults:
        raise ConfigError("fault_modes must not be empty")
    for name, spec in faults.items():
        applies_to = _required(spec, "applies_to", f"fault_modes.{name}")
        if not isinstance(applies_to, list) or not applies_to:
            raise ConfigError(f"fault_modes.{name}.applies_to must be a non-empty list")
        unknown = set(applies_to) - known_types - {"*"}
        if unknown:
            raise ConfigError(f"fault_modes.{name} references unknown types: {sorted(unknown)}")
        _required(spec, "alarm_code", f"fault_modes.{name}")
        _required(spec, "severity", f"fault_modes.{name}")
        _required(spec, "operating_state", f"fault_modes.{name}")
        _required(spec, "quality_status", f"fault_modes.{name}")
        overrides = spec.get("overrides", {})
        if not isinstance(overrides, dict):
            raise ConfigError(f"fault_modes.{name}.overrides must be a mapping")


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"Configuration file does not exist: {config_path}")
    with config_path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ConfigError("Configuration root must be a mapping")
    if int(_required(raw, "version", "root")) != 1:
        raise ConfigError("Unsupported configuration version")
    for section in ("thingsboard", "mqtt", "simulation", "lines", "device_types"):
        _required(raw, section, "root")

    qos = int(_required(raw["mqtt"], "qos", "mqtt"))
    if qos != 1:
        raise ConfigError("This simulator requires mqtt.qos=1")
    interval = float(_required(raw["simulation"], "interval_seconds", "simulation"))
    if interval <= 0:
        raise ConfigError("simulation.interval_seconds must be greater than zero")

    devices = _expand_devices(raw)
    _validate_faults(raw, devices)
    return AppConfig(path=config_path, raw=raw, devices=devices)


def applicable_faults(config: AppConfig, device_type: str) -> list[str]:
    return [
        name
        for name, spec in config.fault_modes.items()
        if "*" in spec["applies_to"] or device_type in spec["applies_to"]
    ]
