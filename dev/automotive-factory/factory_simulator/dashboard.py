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
import uuid
from typing import Any

from .api import ThingsBoardApi, ThingsBoardApiError
from .config import AppConfig


DASHBOARD_TITLE = "汽车零部件生产监控"

_ENTITIES_TABLE_FQN = "system.cards.entities_table"
_TIME_SERIES_CHART_FQN = "system.time_series_chart"
_ALARMS_TABLE_FQN = "system.alarm_widgets.alarms_table"

_TABLE_WIDGET_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "thingsboard.local/automotive-factory/widget/device-table")
)
_CHART_WIDGET_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "thingsboard.local/automotive-factory/widget/telemetry-chart")
)
_ALARM_WIDGET_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "thingsboard.local/automotive-factory/widget/alarm-table")
)
_DEVICE_ALIAS_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "thingsboard.local/automotive-factory/alias/devices")
)

_TABLE_LAYOUT = {"sizeX": 24, "sizeY": 7, "row": 0, "col": 0}
_CHART_LAYOUT = {"sizeX": 14, "sizeY": 8, "row": 7, "col": 0}
_ALARM_LAYOUT = {"sizeX": 10, "sizeY": 8, "row": 7, "col": 14}


def _load_widget_default(
    api: ThingsBoardApi,
    fqn: str,
    expected_type: str,
) -> tuple[str, dict[str, Any]]:
    widget_type = api.get_widget_type(fqn)
    descriptor = widget_type.get("descriptor")
    if not isinstance(descriptor, dict):
        raise ThingsBoardApiError(f"Widget type {fqn} returned no descriptor")

    actual_type = descriptor.get("type")
    if actual_type != expected_type:
        raise ThingsBoardApiError(
            f"Widget type {fqn} has type {actual_type!r}; expected {expected_type!r}"
        )

    raw_default = descriptor.get("defaultConfig")
    try:
        if isinstance(raw_default, str):
            default_config = json.loads(raw_default)
        elif isinstance(raw_default, dict):
            default_config = copy.deepcopy(raw_default)
        else:
            raise TypeError("defaultConfig is neither a JSON string nor an object")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ThingsBoardApiError(
            f"Widget type {fqn} returned an invalid defaultConfig: {exc}"
        ) from exc
    if not isinstance(default_config, dict):
        raise ThingsBoardApiError(
            f"Widget type {fqn} defaultConfig must decode to a JSON object"
        )
    return actual_type, default_config


def _resolve_device_ids(api: ThingsBoardApi, config: AppConfig) -> list[str]:
    device_ids: list[str] = []
    missing: list[str] = []
    for definition in config.devices:
        device = api.get_device(definition.name)
        if device is None:
            missing.append(definition.name)
            continue
        try:
            device_ids.append(device["id"]["id"])
        except (KeyError, TypeError) as exc:
            raise ThingsBoardApiError(
                f"Device {definition.name} returned an invalid entity id"
            ) from exc
    if missing:
        preview = ", ".join(missing[:5])
        if len(missing) > 5:
            preview += f", ... ({len(missing)} total)"
        raise ThingsBoardApiError(
            f"Cannot create dashboard because devices are missing: {preview}. "
            "Run device provisioning first."
        )
    return device_ids


def _table_key(
    name: str,
    label: str,
    color: str,
    *,
    key_type: str = "timeseries",
    units: str | None = None,
    decimals: int | None = None,
) -> dict[str, Any]:
    key: dict[str, Any] = {
        "name": name,
        "type": key_type,
        "label": label,
        "color": color,
        "settings": {
            "columnWidth": "0px",
            "useCellStyleFunction": False,
            "cellStyleFunction": "",
            "useCellContentFunction": False,
            "cellContentFunction": "",
        },
    }
    if units is not None:
        key["units"] = units
    if decimals is not None:
        key["decimals"] = decimals
    return key


def _chart_key(
    name: str,
    label: str,
    color: str,
    units: str,
    decimals: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "timeseries",
        "label": label,
        "color": color,
        "settings": {},
        "units": units,
        "decimals": decimals,
    }


def _device_table_config(
    default_config: dict[str, Any],
    device_count: int,
) -> dict[str, Any]:
    widget_config = copy.deepcopy(default_config)
    widget_config.update(
        {
            "title": "产线设备状态",
            "showTitle": True,
            "enableFullscreen": True,
            "showTitleIcon": True,
            "titleIcon": "precision_manufacturing",
            "datasources": [
                {
                    "type": "entity",
                    "name": "产线设备",
                    "entityAliasId": _DEVICE_ALIAS_ID,
                    "filterId": None,
                    "dataKeys": [
                        _table_key(
                            "device_type", "设备类型", "#5C6BC0", key_type="attribute"
                        ),
                        _table_key("line_id", "产线", "#26A69A", key_type="attribute"),
                        _table_key(
                            "equipment_id", "设备编号", "#546E7A", key_type="attribute"
                        ),
                        _table_key(
                            "cmms_asset_id", "CMMS 资产编号", "#78909C", key_type="attribute"
                        ),
                        _table_key("operating_state", "运行状态", "#42A5F5"),
                        _table_key("online", "在线", "#66BB6A"),
                        _table_key("load_pct", "负载", "#FFA726", units="%", decimals=1),
                        _table_key(
                            "temperature", "温度", "#EF5350", units="°C", decimals=1
                        ),
                        _table_key(
                            "vibration_rms",
                            "振动 RMS",
                            "#AB47BC",
                            units="mm/s",
                            decimals=2,
                        ),
                        _table_key("power", "功率", "#78909C", units="kW", decimals=2),
                        _table_key(
                            "cycle_time", "节拍", "#8D6E63", units="s", decimals=2
                        ),
                        _table_key("quality_status", "质量状态", "#EC407A"),
                        _table_key("alarm_code", "告警代码", "#D32F2F"),
                    ],
                }
            ],
            "actions": {},
        }
    )
    settings = widget_config.setdefault("settings", {})
    settings.update(
        {
            "entitiesTitle": "生产设备",
            "enableSearch": True,
            "displayEntityName": True,
            "displayEntityLabel": False,
            "displayEntityType": False,
            "entityNameColumnTitle": "设备",
            "displayPagination": True,
            "defaultPageSize": max(1, min(device_count, 100)),
            "defaultSortOrder": "entityName",
            "enableStickyHeader": True,
        }
    )
    return widget_config


def _telemetry_chart_config(default_config: dict[str, Any]) -> dict[str, Any]:
    widget_config = copy.deepcopy(default_config)
    widget_config.update(
        {
            "title": "设备遥测趋势（最近 10 分钟）",
            "showTitle": True,
            "enableFullscreen": True,
            "showTitleIcon": True,
            "titleIcon": "monitoring",
            "useDashboardTimewindow": True,
            "datasources": [
                {
                    "type": "entity",
                    "name": "产线设备",
                    "entityAliasId": _DEVICE_ALIAS_ID,
                    "filterId": None,
                    "dataKeys": [
                        _chart_key("load_pct", "负载", "#FB8C00", "%", 1),
                        _chart_key("temperature", "温度", "#E53935", "°C", 1),
                        _chart_key(
                            "vibration_rms", "振动 RMS", "#8E24AA", "mm/s", 2
                        ),
                        _chart_key("power", "功率", "#546E7A", "kW", 2),
                    ],
                    "latestDataKeys": [],
                }
            ],
            "actions": {},
        }
    )
    settings = widget_config.setdefault("settings", {})
    settings["showLegend"] = True
    return widget_config


def _alarm_table_config(default_config: dict[str, Any]) -> dict[str, Any]:
    widget_config = copy.deepcopy(default_config)
    alarm_source = widget_config.get("alarmSource")
    if not isinstance(alarm_source, dict) or not isinstance(
        alarm_source.get("dataKeys"), list
    ):
        raise ThingsBoardApiError(
            f"Widget type {_ALARMS_TABLE_FQN} defaultConfig has no alarm data keys"
        )
    alarm_source.update(
        {
            "type": "entity",
            "name": "产线设备告警",
            "entityAliasId": _DEVICE_ALIAS_ID,
            "filterId": None,
        }
    )
    widget_config.update(
        {
            "title": "设备告警",
            "showTitle": True,
            "enableFullscreen": True,
            "showTitleIcon": True,
            "titleIcon": "warning",
            "useDashboardTimewindow": False,
            "displayTimewindow": True,
            "alarmSource": alarm_source,
            "alarmSearchStatus": "ANY",
            "searchPropagatedAlarms": False,
            "alarmStatusList": [],
            "alarmSeverityList": [],
            "alarmTypeList": [],
            "alarmFilterConfig": {
                "statusList": [],
                "severityList": [],
                "typeList": [],
                "searchPropagatedAlarms": False,
            },
            "timewindow": {
                "displayValue": "",
                "selectedTab": 0,
                "realtime": {
                    "realtimeType": 0,
                    "interval": 1000,
                    "timewindowMs": 86_400_000,
                },
                "aggregation": {"type": "NONE", "limit": 200},
            },
            "actions": {},
        }
    )
    settings = widget_config.setdefault("settings", {})
    settings.update(
        {
            "alarmsTitle": "最近 24 小时告警",
            "displayPagination": True,
            "defaultPageSize": 20,
            "defaultSortOrder": "-createdTime",
            "enableSearch": True,
            "enableFilter": True,
        }
    )
    return widget_config


def _widget(
    widget_id: str,
    fqn: str,
    widget_type: str,
    layout: dict[str, int],
    config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": widget_id,
        "typeFullFqn": fqn,
        "type": widget_type,
        **layout,
        "config": config,
    }


def _dashboard_configuration(
    api: ThingsBoardApi,
    device_ids: list[str],
) -> dict[str, Any]:
    table_type, table_default = _load_widget_default(
        api, _ENTITIES_TABLE_FQN, "latest"
    )
    chart_type, chart_default = _load_widget_default(
        api, _TIME_SERIES_CHART_FQN, "timeseries"
    )
    alarm_type, alarm_default = _load_widget_default(
        api, _ALARMS_TABLE_FQN, "alarm"
    )

    widgets = {
        _TABLE_WIDGET_ID: _widget(
            _TABLE_WIDGET_ID,
            _ENTITIES_TABLE_FQN,
            table_type,
            _TABLE_LAYOUT,
            _device_table_config(table_default, len(device_ids)),
        ),
        _CHART_WIDGET_ID: _widget(
            _CHART_WIDGET_ID,
            _TIME_SERIES_CHART_FQN,
            chart_type,
            _CHART_LAYOUT,
            _telemetry_chart_config(chart_default),
        ),
        _ALARM_WIDGET_ID: _widget(
            _ALARM_WIDGET_ID,
            _ALARMS_TABLE_FQN,
            alarm_type,
            _ALARM_LAYOUT,
            _alarm_table_config(alarm_default),
        ),
    }
    widget_layouts = {
        widget_id: {
            **layout,
            "mobileOrder": mobile_order,
            "mobileHeight": layout["sizeY"],
        }
        for widget_id, layout, mobile_order in (
            (_TABLE_WIDGET_ID, _TABLE_LAYOUT, 1),
            (_CHART_WIDGET_ID, _CHART_LAYOUT, 2),
            (_ALARM_WIDGET_ID, _ALARM_LAYOUT, 3),
        )
    }

    return {
        "widgets": widgets,
        "states": {
            "default": {
                "name": DASHBOARD_TITLE,
                "root": True,
                "layouts": {
                    "main": {
                        "widgets": widget_layouts,
                        "gridSettings": {
                            "layoutType": "default",
                            "backgroundColor": "#eeeeee",
                            "color": "rgba(0,0,0,0.87)",
                            "columns": 24,
                            "margin": 10,
                            "outerMargin": True,
                            "backgroundSizeMode": "100%",
                            "autoFillHeight": True,
                            "mobileAutoFillHeight": False,
                            "mobileRowHeight": 70,
                        },
                    }
                },
            }
        },
        "entityAliases": {
            _DEVICE_ALIAS_ID: {
                "id": _DEVICE_ALIAS_ID,
                "alias": "产线设备",
                "filter": {
                    "type": "entityList",
                    "resolveMultiple": True,
                    "entityType": "DEVICE",
                    "entityList": device_ids,
                },
            }
        },
        "filters": {},
        "timewindow": {
            "displayValue": "",
            "selectedTab": 0,
            "hideAggregation": False,
            "hideAggInterval": False,
            "realtime": {
                "realtimeType": 0,
                "interval": 10_000,
                "timewindowMs": 600_000,
            },
            "aggregation": {
                "type": "AVG",
                "interval": 10_000,
                "limit": 5_000,
            },
        },
        "settings": {
            "stateControllerId": "default",
            "showTitle": False,
            "showDashboardsSelect": True,
            "showEntitiesSelect": False,
            "showFilters": False,
            "showDashboardTimewindow": True,
            "showDashboardExport": True,
            "toolbarAlwaysOpen": True,
            "hideToolbar": False,
            "showDashboardLogo": False,
            "showUpdateDashboardImage": True,
        },
    }


def _dashboard_payload(
    configuration: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": DASHBOARD_TITLE,
        "mobileHide": False,
        "mobileOrder": None,
        "configuration": configuration,
        "resources": [],
    }
    if existing:
        for key in (
            "id",
            "version",
            "image",
            "assignedCustomers",
            "externalId",
        ):
            if key in existing:
                payload[key] = existing[key]
    return payload


def provision_dashboard(config: AppConfig) -> dict[str, Any]:
    """Create or update the local automotive-factory dashboard."""

    api = ThingsBoardApi(config)
    api.wait_until_ready()
    device_ids = _resolve_device_ids(api, config)
    existing = api.find_dashboard(DASHBOARD_TITLE)
    configuration = _dashboard_configuration(api, device_ids)
    saved = api.save_dashboard(_dashboard_payload(configuration, existing))

    dashboard_id = saved["id"]["id"]
    action = "Updated" if existing else "Created"
    print(f"{action} dashboard '{DASHBOARD_TITLE}': {api.base_url}/dashboards/{dashboard_id}")
    return saved
