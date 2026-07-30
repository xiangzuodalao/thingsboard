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

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from requests import RequestException

from .api import ThingsBoardApi, ThingsBoardApiError, provision_devices
from .config import AppConfig, ConfigError, applicable_faults, load_config
from .dashboard_publication import (
    DashboardPublicationError,
    apply_dashboard_plan,
    create_dashboard_plan,
)
from .engine import (
    FactorySimulator,
    SimulatorError,
    enqueue_command,
    simulator_is_running,
)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yml"

BASE_TELEMETRY_KEYS = (
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
    "sequence",
)


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def _load_env_file(path: Path) -> None:
    """Load the local .env without overriding explicitly exported variables."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"Unable to read environment file {path}: {exc}") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise ConfigError(f"{path}:{line_number}: invalid environment variable name")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _load_app_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    _load_env_file(config_path.parent / ".env")
    return load_config(config_path)


def _cmd_validate(config: AppConfig, _args: argparse.Namespace) -> int:
    line_counts = Counter(device.line_id for device in config.devices)
    type_counts = Counter(device.device_type for device in config.devices)
    print(f"Configuration valid: {config.path}")
    print(
        f"Expanded devices: {len(config.devices)}; "
        f"lines: {len(line_counts)}; fault modes: {len(config.fault_modes)}"
    )
    print("Devices by line: " + ", ".join(f"{name}={count}" for name, count in sorted(line_counts.items())))
    print("Devices by type: " + ", ".join(f"{name}={count}" for name, count in sorted(type_counts.items())))
    return 0


def _cmd_provision(config: AppConfig, _args: argparse.Namespace) -> int:
    provision_devices(config)
    return 0


def _cmd_dashboard(config: AppConfig, _args: argparse.Namespace) -> int:
    if os.environ.get("TB_PDM_DASHBOARD_MANAGED_PUBLICATION", "false").lower() == "true":
        raise DashboardPublicationError(
            "managed publication is enabled; use dashboard-plan then dashboard-apply"
        )
    # Imported lazily so configuration validation and simulation do not depend
    # on dashboard provisioning internals.
    from .dashboard import provision_dashboard

    provision_dashboard(config)
    return 0


def _cmd_dashboard_plan(config: AppConfig, args: argparse.Namespace) -> int:
    result = create_dashboard_plan(config, actor=args.actor)
    print(f"Dashboard plan SHA-256: {result.sha256}")
    print(f"Expires at: 30 minutes after creation; correlation ID is recorded in the plan.")
    return 0


def _cmd_dashboard_apply(config: AppConfig, args: argparse.Namespace) -> int:
    receipt = apply_dashboard_plan(
        config,
        plan_sha256=args.plan_sha256,
        confirmed_sha256=args.confirm_sha256,
    )
    outcome = "saved" if receipt["saved"] else "already matched"
    print(f"Dashboard apply {outcome}; receipt saved with plan SHA-256 {receipt['plan_sha256']}")
    return 0


def _cmd_run(config: AppConfig, _args: argparse.Namespace) -> int:
    FactorySimulator(config).run()
    return 0


def _require_running(config: AppConfig) -> int:
    running, pid = simulator_is_running(config)
    if not running or pid is None:
        raise SimulatorError("Simulator is not running; start it before queuing commands")
    return pid


def _find_device(config: AppConfig, name: str) -> Any:
    for device in config.devices:
        if device.name == name:
            return device
    raise SimulatorError(f"Unknown device '{name}'")


def _cmd_fault(config: AppConfig, args: argparse.Namespace) -> int:
    definition = _find_device(config, args.device)
    if args.fault not in config.fault_modes:
        raise SimulatorError(f"Unknown fault mode '{args.fault}'")
    if args.fault not in applicable_faults(config, definition.device_type):
        raise SimulatorError(
            f"Fault {args.fault} does not apply to {definition.device_type}"
        )
    pid = _require_running(config)
    command_path = enqueue_command(
        config,
        {
            "action": "fault",
            "device": definition.name,
            "fault": args.fault,
            "duration_seconds": args.duration,
            "replace": args.replace,
        },
    )
    duration = "until cleared" if args.duration is None else f"{args.duration:g}s"
    print(
        f"Queued fault {args.fault} for {definition.name} "
        f"({duration}, simulator PID {pid}): {command_path}"
    )
    return 0


def _cmd_clear(config: AppConfig, args: argparse.Namespace) -> int:
    definition = _find_device(config, args.device)
    pid = _require_running(config)
    command_path = enqueue_command(
        config,
        {"action": "clear", "device": definition.name},
    )
    print(
        f"Queued fault clear for {definition.name} "
        f"(simulator PID {pid}): {command_path}"
    )
    return 0


def _read_status(config: AppConfig) -> dict[str, Any] | None:
    status_path = config.runtime_dir / "status.json"
    if not status_path.is_file():
        return None
    try:
        with status_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise SimulatorError(f"Unable to read simulator status {status_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SimulatorError(f"Invalid simulator status payload in {status_path}")
    return payload


def _cmd_status(config: AppConfig, args: argparse.Namespace) -> int:
    running, pid = simulator_is_running(config)
    status = _read_status(config)
    if args.json:
        print(
            json.dumps(
                {"running": running, "pid": pid, "status": status},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    lifecycle = "running" if running else "stopped"
    pid_text = f" (PID {pid})" if pid is not None else ""
    print(f"Simulator: {lifecycle}{pid_text}")
    if status is None:
        print(f"Status file not found: {config.runtime_dir / 'status.json'}")
        return 0

    updated_at = int(status.get("updated_at", 0) or 0)
    age_seconds = max(0.0, (time.time() * 1000 - updated_at) / 1000) if updated_at else 0.0
    print(
        "Devices: "
        f"{status.get('device_count', 0)} total, "
        f"{status.get('connected_count', 0)} connected, "
        f"{status.get('active_fault_count', 0)} active faults"
    )
    if updated_at:
        print(f"Status age: {age_seconds:.1f}s")

    devices = status.get("devices", {})
    if isinstance(devices, dict):
        active = [
            (name, item)
            for name, item in sorted(devices.items())
            if isinstance(item, dict) and item.get("active_fault")
        ]
        for name, item in active:
            print(
                f"FAULT {name}: {item.get('active_fault')} "
                f"state={item.get('operating_state')} "
                f"alarm={item.get('alarm_code')}"
            )
    return 0


def _latest_value(telemetry: dict[str, Any], key: str) -> Any:
    entries = telemetry.get(key)
    if not isinstance(entries, list) or not entries:
        return None
    first = entries[0]
    return first.get("value") if isinstance(first, dict) else None


def _integer_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if str(parsed) == value.strip() else None
    return None


def _verify_runtime_status(
    config: AppConfig,
    *,
    max_age_seconds: float,
) -> dict[str, Any]:
    issues: list[str] = []
    expected_count = len(config.devices)
    running = False
    pid: int | None = None
    try:
        running, pid = simulator_is_running(config)
    except (OSError, ValueError) as exc:
        issues.append(f"unable to inspect simulator process: {exc}")

    if not running or pid is None:
        issues.append("simulator process is not running")

    status: dict[str, Any] | None = None
    try:
        status = _read_status(config)
    except SimulatorError as exc:
        issues.append(str(exc))

    status_pid: int | None = None
    device_count: int | None = None
    connected_count: int | None = None
    status_age_seconds: float | None = None
    if status is None:
        issues.append(f"simulator status file not found: {config.runtime_dir / 'status.json'}")
    else:
        status_pid = _integer_value(status.get("pid"))
        device_count = _integer_value(status.get("device_count"))
        connected_count = _integer_value(status.get("connected_count"))

        if status_pid is None:
            issues.append("simulator status contains an invalid PID")
        elif pid is not None and status_pid != pid:
            issues.append(
                f"simulator PID mismatch: process={pid}, status.json={status_pid}"
            )
        if device_count != expected_count:
            issues.append(
                "simulator status device_count mismatch: "
                f"{device_count!r} (expected {expected_count})"
            )
        if connected_count != expected_count:
            issues.append(
                "simulator status connected_count mismatch: "
                f"{connected_count!r} (expected {expected_count})"
            )

        status_devices = status.get("devices")
        if not isinstance(status_devices, dict):
            issues.append("simulator status contains an invalid devices map")
        else:
            missing_status_devices = [
                definition.name
                for definition in config.devices
                if definition.name not in status_devices
            ]
            if missing_status_devices:
                issues.append(
                    "simulator status is missing devices: "
                    + ", ".join(missing_status_devices)
                )

        updated_at = _integer_value(status.get("updated_at"))
        if updated_at is None or updated_at <= 0:
            issues.append("simulator status contains an invalid updated_at timestamp")
        else:
            status_age_seconds = max(
                0.0,
                (time.time() * 1000 - updated_at) / 1000,
            )
            if status_age_seconds > max_age_seconds:
                issues.append(
                    "simulator status is stale "
                    f"({status_age_seconds:.1f}s old; limit {max_age_seconds:.1f}s)"
                )

    return {
        "running": running,
        "pid": pid,
        "status_pid": status_pid,
        "device_count": device_count,
        "connected_count": connected_count,
        "status_age_seconds": status_age_seconds,
        "issues": issues,
    }


def _verify_once(
    config: AppConfig,
    api: ThingsBoardApi,
    *,
    max_age_seconds: float,
) -> dict[str, Any]:
    runtime = _verify_runtime_status(config, max_age_seconds=max_age_seconds)
    issues: list[str] = list(runtime["issues"])
    found_count = 0
    telemetry_count = 0
    sequences: dict[str, int] = {}
    now_ms = int(time.time() * 1000)

    for definition in config.devices:
        try:
            device = api.get_device(definition.name)
        except (ThingsBoardApiError, RequestException, ValueError) as exc:
            issues.append(f"{definition.name}: device lookup failed: {exc}")
            continue
        if device is None:
            issues.append(f"{definition.name}: device does not exist")
            continue
        found_count += 1
        try:
            device_id = device["id"]["id"]
        except (KeyError, TypeError):
            issues.append(f"{definition.name}: REST response has no device UUID")
            continue

        keys = sorted(set(BASE_TELEMETRY_KEYS).union(definition.metrics))
        try:
            telemetry = api.latest_telemetry(device_id, keys)
        except (ThingsBoardApiError, RequestException, ValueError) as exc:
            issues.append(f"{definition.name}: telemetry lookup failed: {exc}")
            continue

        missing = [
            key
            for key in keys
            if not isinstance(telemetry.get(key), list) or not telemetry[key]
        ]
        if missing:
            issues.append(
                f"{definition.name}: missing latest telemetry keys: {', '.join(missing)}"
            )
            continue

        identity_mismatches: list[str] = []
        expected_identity = {
            "device_id": definition.name,
            "device_type": definition.device_type,
            "line_id": definition.line_id,
        }
        for key, expected in expected_identity.items():
            actual = _latest_value(telemetry, key)
            if str(actual) != expected:
                identity_mismatches.append(f"{key}={actual!r} (expected {expected!r})")
        if identity_mismatches:
            issues.append(
                f"{definition.name}: telemetry identity mismatch: "
                + "; ".join(identity_mismatches)
            )
            continue

        sequence = _integer_value(_latest_value(telemetry, "sequence"))
        if sequence is None or sequence < 0:
            issues.append(f"{definition.name}: telemetry sequence is not a valid integer")
            continue

        try:
            oldest_latest_ts = min(
                int(telemetry[key][0]["ts"])
                for key in keys
            )
        except (KeyError, TypeError, ValueError):
            issues.append(f"{definition.name}: telemetry contains an invalid timestamp")
            continue
        age_seconds = max(0.0, (now_ms - oldest_latest_ts) / 1000)
        if age_seconds > max_age_seconds:
            issues.append(
                f"{definition.name}: latest telemetry is stale "
                f"({age_seconds:.1f}s old; limit {max_age_seconds:.1f}s)"
            )
            continue
        sequences[definition.name] = sequence
        telemetry_count += 1

    return {
        "expected_count": len(config.devices),
        "found_count": found_count,
        "telemetry_count": telemetry_count,
        "sequences": sequences,
        "runtime": runtime,
        "max_age_seconds": max_age_seconds,
        "issues": issues,
    }


def _sequence_progress(
    previous: dict[str, int],
    current: dict[str, int],
) -> tuple[int, list[str]]:
    issues: list[str] = []
    advanced_count = 0
    for device_name, previous_sequence in previous.items():
        current_sequence = current.get(device_name)
        if current_sequence is None:
            issues.append(f"{device_name}: no sequence value in the second sample")
        elif current_sequence <= previous_sequence:
            issues.append(
                f"{device_name}: sequence did not advance "
                f"({previous_sequence} -> {current_sequence})"
            )
        else:
            advanced_count += 1
    return advanced_count, issues


def _cmd_verify(config: AppConfig, args: argparse.Namespace) -> int:
    api = ThingsBoardApi(config)
    api.wait_until_ready()
    interval_seconds = float(config.simulation["interval_seconds"])
    max_age_seconds = (
        args.max_age_seconds
        if args.max_age_seconds is not None
        else max(15.0, interval_seconds * 5)
    )
    deadline = time.monotonic() + args.timeout
    report: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    next_sample_at: float | None = None
    progress_issues: list[str] = []
    advanced_count = 0

    while True:
        now = time.monotonic()
        if baseline is not None and next_sample_at is not None and now < next_sample_at:
            remaining = deadline - now
            if remaining <= 0:
                progress_issues = [
                    "timed out before a full reporting interval elapsed "
                    f"({interval_seconds:.1f}s required)"
                ]
                report = baseline
                break
            time.sleep(min(next_sample_at - now, remaining))
            continue

        report = _verify_once(config, api, max_age_seconds=max_age_seconds)
        now = time.monotonic()
        if report["issues"]:
            if now >= deadline:
                break
            time.sleep(min(2.0, max(0.0, deadline - now)))
            continue

        if baseline is None:
            baseline = report
            next_sample_at = now + interval_seconds
            if next_sample_at > deadline:
                progress_issues = [
                    "timeout leaves less than one full reporting interval for "
                    f"the sequence check ({interval_seconds:.1f}s required)"
                ]
                break
            continue

        baseline_pid = baseline["runtime"]["pid"]
        current_pid = report["runtime"]["pid"]
        if current_pid != baseline_pid:
            baseline = report
            next_sample_at = now + interval_seconds
            progress_issues = []
            if next_sample_at > deadline:
                progress_issues = [
                    "simulator restarted during verification and there is not enough "
                    "time for another full reporting interval"
                ]
                break
            continue

        advanced_count, progress_issues = _sequence_progress(
            baseline["sequences"],
            report["sequences"],
        )
        if not progress_issues:
            break
        if now >= deadline:
            break
        time.sleep(
            min(
                max(0.25, min(1.0, interval_seconds / 4)),
                max(0.0, deadline - now),
            )
        )

    assert report is not None
    expected = report["expected_count"]
    runtime = report["runtime"]
    pid_text = runtime["pid"] if runtime["pid"] is not None else "not running"
    print(
        "Simulator runtime: "
        f"PID {pid_text}; "
        f"status devices {runtime['device_count']}/{expected}; "
        f"connected {runtime['connected_count']}/{expected}"
    )
    print(f"Configured devices found through REST: {report['found_count']}/{expected}")
    print(
        "Devices with complete, fresh telemetry: "
        f"{report['telemetry_count']}/{expected} "
        f"(maximum age {max_age_seconds:.1f}s)"
    )
    print(
        "Devices whose sequence advanced across samples: "
        f"{advanced_count}/{expected} "
        f"(sampling interval {interval_seconds:.1f}s)"
    )
    issues = [*report["issues"], *progress_issues]
    if issues:
        print("Verification failed:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1
    print("Verification passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory-simulator",
        description="Manage the local automotive factory ThingsBoard simulator.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"configuration YAML path (default: {DEFAULT_CONFIG_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="validate and summarize the simulator configuration"
    )
    validate_parser.set_defaults(handler=_cmd_validate)

    provision_parser = subparsers.add_parser(
        "provision", help="create or reuse devices and cache their access tokens"
    )
    provision_parser.set_defaults(handler=_cmd_provision)

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="create or update the local factory dashboard"
    )
    dashboard_parser.set_defaults(handler=_cmd_dashboard)

    dashboard_plan_parser = subparsers.add_parser(
        "dashboard-plan", help="write a read-only, 30-minute managed dashboard plan"
    )
    dashboard_plan_parser.add_argument(
        "--actor", help="human operator recorded in the dashboard plan"
    )
    dashboard_plan_parser.set_defaults(handler=_cmd_dashboard_plan)

    dashboard_apply_parser = subparsers.add_parser(
        "dashboard-apply", help="apply one later-confirmed managed dashboard plan"
    )
    dashboard_apply_parser.add_argument("--plan-sha256", required=True)
    dashboard_apply_parser.add_argument("--confirm-sha256", required=True)
    dashboard_apply_parser.set_defaults(handler=_cmd_dashboard_apply)

    run_parser = subparsers.add_parser(
        "run", help="run all configured MQTT device simulators in the foreground"
    )
    run_parser.set_defaults(handler=_cmd_run)

    fault_parser = subparsers.add_parser(
        "fault", help="queue a fault injection command for the running simulator"
    )
    fault_parser.add_argument("device", help="expanded device name")
    fault_parser.add_argument("fault", help="fault mode name from config.yml")
    fault_parser.add_argument(
        "--duration",
        type=_positive_float,
        help="automatically clear the fault after this many seconds",
    )
    fault_parser.add_argument(
        "--replace",
        action="store_true",
        help="clear an existing fault before raising the requested fault",
    )
    fault_parser.set_defaults(handler=_cmd_fault)

    clear_parser = subparsers.add_parser(
        "clear", help="queue a fault-clear command for the running simulator"
    )
    clear_parser.add_argument("device", help="expanded device name")
    clear_parser.set_defaults(handler=_cmd_clear)

    status_parser = subparsers.add_parser(
        "status", help="show simulator process and device status"
    )
    status_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    status_parser.set_defaults(handler=_cmd_status)

    verify_parser = subparsers.add_parser(
        "verify", help="verify configured devices and their latest telemetry through REST"
    )
    verify_parser.add_argument(
        "--timeout",
        type=_non_negative_float,
        default=20.0,
        help="seconds to wait for all devices and telemetry (default: 20)",
    )
    verify_parser.add_argument(
        "--max-age-seconds",
        type=_positive_float,
        help="maximum allowed age of each latest telemetry value",
    )
    verify_parser.set_defaults(handler=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _load_app_config(args.config)
        return int(args.handler(config, args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (
        ConfigError,
        ThingsBoardApiError,
        DashboardPublicationError,
        SimulatorError,
        RequestException,
        OSError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
