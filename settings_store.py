"""运行时配置：由 Web 界面读写，保存到 user_settings.json。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hub_common import ROOT, cfg_get, load_config

SETTINGS_PATH = ROOT / "user_settings.json"

DEFAULTS: dict[str, Any] = {
    "product_name": "",
    "product_brief": "",
    "product_brief_file": "",
    "hub_command_port": "",
    "hub_dut_channel": 1,
    "esp32_serial_port": "",
    "esp32_baud": 115200,
    "device_profile": "auto",
    "product_type": "battery",
    "active_scenario": "usb_power_cycle",
    "power_off_seconds": 0.8,
    "sample_interval_ms": 50,
    "reboot_current_threshold_ma": 30,
    "scenario_wait_seconds": 25,
    "scenario_repeat_count": 5,
    "scenario_cycle_wait_seconds": 4,
    "scenario_baseline_seconds": 3.0,
    "scenario_battery_only_seconds": 120,
    "dataline_delay_seconds": 0.15,
    "serial_reconnect_delay_seconds": 1.5,
    "serial_reconnect_retries": 8,
    "serial_reconnect_retry_seconds": 1.0,
    "ai_enabled": True,
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "openai_base_url": "https://api.openai.com/v1/chat/completions",
}


def _from_yaml() -> dict[str, Any]:
    try:
        cfg = load_config()
    except FileNotFoundError:
        return {}
    return {
        "hub_command_port": cfg_get(cfg, "hub", "command_port", default="") or "",
        "hub_dut_channel": int(cfg_get(cfg, "hub", "dut_channel", default=1)),
        "esp32_serial_port": cfg_get(cfg, "esp32", "serial_port", default="") or "",
        "esp32_baud": int(cfg_get(cfg, "esp32", "baud", default=115200)),
        "power_off_seconds": float(cfg_get(cfg, "test", "power_off_seconds", default=0.8)),
        "sample_interval_ms": int(cfg_get(cfg, "test", "sample_interval_ms", default=10)),
        "reboot_current_threshold_ma": int(
            cfg_get(cfg, "test", "reboot_current_threshold_ma", default=30)
        ),
        "ai_enabled": bool(cfg_get(cfg, "ai", "enabled", default=True)),
        "openai_api_key": cfg_get(cfg, "ai", "openai_api_key", default="") or "",
        "openai_model": str(cfg_get(cfg, "ai", "model", default="gpt-4o-mini")),
        "openai_base_url": str(
            cfg_get(cfg, "ai", "base_url", default="https://api.openai.com/v1/chat/completions")
        ),
    }


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULTS)
    data.update(_from_yaml())
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                data.update(stored)
        except (json.JSONDecodeError, OSError):
            pass
    if not data.get("product_type"):
        data["product_type"] = "battery" if data.pop("device_has_battery", True) else "usb_only"
    data.pop("device_has_battery", None)
    from hub_plug import normalize_product_type, product_has_battery

    old_pt = data.get("product_type")
    if old_pt in ("battery_vbus", "vbus_only", "battery_power") and not data.get("active_scenario"):
        data["active_scenario"] = "battery_only_serial"
    data["product_type"] = normalize_product_type(data.get("product_type"))
    if not data.get("active_scenario"):
        data["active_scenario"] = "usb_power_cycle"
    if data["active_scenario"] == "battery_only_serial" and not product_has_battery(data):
        data["active_scenario"] = "usb_power_cycle"
    from device_compat import normalize_device_profile

    data["device_profile"] = normalize_device_profile(data.get("device_profile"))
    return data


def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    merged = load_settings()
    for key in DEFAULTS:
        if key in data:
            merged[key] = data[key]
    if "product_type" in data:
        merged["product_type"] = data["product_type"]
    if "active_scenario" in data:
        merged["active_scenario"] = data["active_scenario"]
    for key in ("product_name", "product_brief", "product_brief_file", "openai_base_url", "openai_model"):
        if key in data:
            merged[key] = data[key]
    merged.pop("test_mode", None)
    merged.pop("device_has_battery", None)
    SETTINGS_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return merged


def public_settings(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    key = out.get("openai_api_key") or ""
    out["openai_api_key_set"] = bool(key)
    out["openai_api_key"] = ""
    return out
