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
import re
import time
from pathlib import Path
from typing import Any

import requests

from .config import AppConfig, DeviceDefinition


class ThingsBoardApiError(RuntimeError):
    """Raised for an unsuccessful ThingsBoard REST operation."""


def redact_sensitive_text(value: str) -> str:
    """Keep credentials out of operator-facing exceptions and logs."""

    redacted = re.sub(
        r"(?i)\bbearer\s+[^\s,;]+", "Bearer [REDACTED]", value
    )
    redacted = re.sub(
        r"(?i)\b((?:access[_ -]?token|refresh[_ -]?token|api[_ -]?key|"
        r"password|secret|credential)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )
    return redacted


class ThingsBoardApi:
    def __init__(self, config: AppConfig, *, bearer_token: str | None = None):
        self.config = config
        self.base_url = config.thingsboard["base_url"].rstrip("/")
        self.timeout = float(config.thingsboard.get("request_timeout_seconds", 10))
        self._preauthenticated = bool(bearer_token)
        if self._preauthenticated:
            self.username = None
            self.password = None
        else:
            self.username, self.password = config.tenant_credentials()
        self.session = requests.Session()
        self.token = bearer_token
        self.refresh_token: str | None = None
        if bearer_token:
            self.session.headers.update({"X-Authorization": f"Bearer {bearer_token}"})

    def login(self) -> None:
        if self._preauthenticated:
            raise ThingsBoardApiError("preauthenticated bearer credential was rejected")
        response = self.session.post(
            f"{self.base_url}/api/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if not response.ok:
            raise ThingsBoardApiError(f"ThingsBoard login failed ({response.status_code})")
        try:
            payload = response.json()
            self.token = payload["token"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ThingsBoardApiError("ThingsBoard login returned an invalid response") from exc
        self.refresh_token = payload.get("refreshToken")
        self.session.headers.update({"X-Authorization": f"Bearer {self.token}"})

    def request(
        self,
        method: str,
        path: str,
        *,
        allow_not_found: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        if not self.token:
            self.login()
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code == 401 and not self._preauthenticated:
            self.login()
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                **kwargs,
            )
        if allow_not_found and response.status_code == 404:
            return response
        if not response.ok:
            raise ThingsBoardApiError(f"{method} {path} failed ({response.status_code})")
        return response

    def wait_until_ready(self, timeout_seconds: float = 180) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error = "not attempted"
        while time.monotonic() < deadline:
            try:
                self.login()
                return
            except (requests.RequestException, ThingsBoardApiError) as exc:
                last_error = redact_sensitive_text(str(exc))
                time.sleep(2)
        raise ThingsBoardApiError(
            f"ThingsBoard did not become ready within {timeout_seconds:.0f}s: {last_error}"
        )

    def get_device(self, name: str) -> dict[str, Any] | None:
        response = self.request(
            "GET",
            "/api/tenant/device",
            params={"deviceName": name},
            allow_not_found=True,
        )
        return None if response.status_code == 404 else response.json()

    def create_device(self, definition: DeviceDefinition) -> dict[str, Any]:
        response = self.request(
            "POST",
            "/api/device",
            json={
                "name": definition.name,
                "type": definition.device_type,
                "label": definition.label,
                "additionalInfo": {
                    "description": "Generated by automotive factory simulator",
                    "simulated": True,
                },
            },
        )
        return response.json()

    def get_or_create_device(self, definition: DeviceDefinition) -> tuple[dict[str, Any], bool]:
        device = self.get_device(definition.name)
        if device is not None:
            return device, False
        return self.create_device(definition), True

    def get_device_credentials(self, device_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/device/{device_id}/credentials").json()

    def save_server_attributes(self, device_id: str, attributes: dict[str, Any]) -> None:
        self.request(
            "POST",
            f"/api/plugins/telemetry/DEVICE/{device_id}/attributes/SERVER_SCOPE",
            json=attributes,
        )

    def create_or_update_alarm(
        self,
        *,
        device_id: str,
        alarm_type: str,
        severity: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/alarm",
            json={
                "type": alarm_type,
                "originator": {"entityType": "DEVICE", "id": device_id},
                "severity": severity,
                "details": details,
                "propagate": False,
                "propagateToOwner": True,
                "propagateToTenant": True,
            },
        ).json()

    def clear_alarm(self, alarm_id: str) -> None:
        self.request("POST", f"/api/alarm/{alarm_id}/clear")

    def latest_telemetry(self, device_id: str, keys: list[str]) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries",
            params={"keys": ",".join(keys), "useStrictDataTypes": "true"},
        ).json()

    def list_active_alarms(self, device_id: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            f"/api/alarm/DEVICE/{device_id}",
            params={
                "pageSize": 100,
                "page": 0,
                "searchStatus": "ACTIVE",
                "sortProperty": "startTs",
                "sortOrder": "DESC",
            },
        ).json()
        return payload.get("data", [])

    def find_dashboards(self, title: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            "/api/tenant/dashboards",
            params={"pageSize": 100, "page": 0, "textSearch": title},
        ).json()
        dashboards: list[dict[str, Any]] = []
        for item in payload.get("data", []):
            if item.get("title") == title:
                dashboard_id = item["id"]["id"]
                dashboards.append(self.request("GET", f"/api/dashboard/{dashboard_id}").json())
        return dashboards

    def find_dashboard(self, title: str) -> dict[str, Any] | None:
        dashboards = self.find_dashboards(title)
        return dashboards[0] if dashboards else None

    def save_dashboard(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/dashboard", json=dashboard).json()

    def get_widget_type(self, fqn: str) -> dict[str, Any]:
        return self.request("GET", "/api/widgetType", params={"fqn": fqn}).json()


def provision_devices(config: AppConfig) -> dict[str, dict[str, Any]]:
    api = ThingsBoardApi(config)
    api.wait_until_ready()
    credentials: dict[str, dict[str, Any]] = {}
    created_count = 0
    interval_ms = int(float(config.simulation["interval_seconds"]) * 1000)

    for definition in config.devices:
        device, created = api.get_or_create_device(definition)
        created_count += int(created)
        device_id = device["id"]["id"]
        credential = api.get_device_credentials(device_id)
        if credential.get("credentialsType") != "ACCESS_TOKEN":
            raise ThingsBoardApiError(
                f"Device {definition.name} does not use ACCESS_TOKEN credentials"
            )
        token = credential.get("credentialsId")
        if not token:
            raise ThingsBoardApiError(f"Device {definition.name} returned an empty access token")
        api.save_server_attributes(
            device_id,
            {
                "device_id": definition.name,
                "device_type": definition.device_type,
                "line_id": definition.line_id,
                "simulated": True,
                "report_interval_ms": interval_ms,
            },
        )
        credentials[definition.name] = {
            "device_id": device_id,
            "access_token": token,
            "device_type": definition.device_type,
            "line_id": definition.line_id,
            "label": definition.label,
        }

    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    output_path = config.runtime_dir / "devices.json"
    temporary_path = config.runtime_dir / "devices.json.tmp"
    with temporary_path.open("w", encoding="utf-8") as stream:
        json.dump(credentials, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(output_path)
    print(
        f"Provisioned {len(credentials)} devices "
        f"({created_count} created, {len(credentials) - created_count} reused)."
    )
    print(f"Credentials saved with mode 0600: {output_path}")
    return credentials


def load_device_credentials(config: AppConfig) -> dict[str, dict[str, Any]]:
    path = config.runtime_dir / "devices.json"
    if not path.is_file():
        raise ThingsBoardApiError(f"Device credentials not found. Run provision first: {path}")
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    expected = {device.name for device in config.devices}
    missing = expected - set(payload)
    if missing:
        raise ThingsBoardApiError(f"Credential file is missing devices: {sorted(missing)}")
    return payload
