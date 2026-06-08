"""Shared config and SmartUSB Hub helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yaml"
EXAMPLE_CONFIG = ROOT / "config.example.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or DEFAULT_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"未找到 {path}，请复制 config.example.yaml")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cfg_get(cfg: dict, *keys: str, default: Any = None) -> Any:
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


def connect_hub(cfg: dict):
    from smartusbhub import SmartUSBHub

    port = cfg_get(cfg, "hub", "command_port", default="") or ""
    if port:
        return SmartUSBHub(port)
    hub = SmartUSBHub.scan_and_connect()
    if hub is None:
        raise RuntimeError("未找到 SmartUSB Hub")
    return hub


def hub_channel(cfg: dict) -> int:
    return int(cfg_get(cfg, "hub", "dut_channel", default=1))


def _patch_smartusbhub_signal() -> None:
    """Web 服务在 Flask 工作线程里连接 Hub，须跳过非主线程的 signal 注册。"""
    import signal
    import threading

    from smartusbhub import SmartUSBHub

    if getattr(SmartUSBHub, "_pl_signal_patch", False):
        return

    def _start(self):
        self.stop_event = threading.Event()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_handler)
        self.uart_recv_thread = threading.Thread(target=self._uart_recv_task)
        self.uart_recv_thread.start()

    SmartUSBHub._start = _start
    SmartUSBHub._pl_signal_patch = True


def _ensure_hub_status_dicts(hub: Any) -> None:
    """防止库在超时后把状态表置为 None，导致 UART 接收线程崩溃。"""
    for name in (
        "channel_default_power_status",
        "channel_default_dataline_status",
        "channel_default_power_flag",
        "channel_default_dataline_flag",
        "channel_power_status",
        "channel_dataline_status",
        "channel_voltages",
        "channel_currents",
    ):
        if getattr(hub, name, None) is None:
            setattr(hub, name, {})


def _patch_smartusbhub_status_dicts() -> None:
    """修复 smartusbhub：get_device_info 超时会把状态 dict 写成 None。"""
    import logging
    import sys

    from smartusbhub import SmartUSBHub

    if getattr(SmartUSBHub, "_pl_status_dict_patch", False):
        return

    _orig_init = SmartUSBHub.__init__
    _orig_pwr = SmartUSBHub._handle_get_default_power_status
    _orig_data = SmartUSBHub._handle_get_default_dataline_status
    _orig_set_pwr = SmartUSBHub._handle_set_default_power_status
    _orig_set_data = SmartUSBHub._handle_set_default_dataline_status
    log = logging.getLogger("smartusbhub")

    def get_device_info(self):
        _ensure_hub_status_dicts(self)
        self.hardware_version = self.get_hardware_version()
        self.firmware_version = self.get_firmware_version()
        self.operate_mode = self.get_operate_mode()
        self.auto_restore_status = self.get_auto_restore_status()
        self.button_control_status = self.get_button_control_status()
        self.device_address = self.get_device_address()
        self.get_default_power_status(1, 2, 3, 4)
        self.get_default_dataline_status(1, 2, 3, 4)
        _ensure_hub_status_dicts(self)
        return {
            "id": self.port.split("/")[-1],
            "address": self.device_address,
            "hardware_version": self.hardware_version,
            "firmware_version": self.firmware_version,
            "operate_mode": (
                "normal"
                if self.operate_mode == 0
                else "interlock"
                if self.operate_mode == 1
                else "N/A"
            ),
            "auto_restore": (
                "enabled" if self.auto_restore_status == 1 else "disabled"
            ),
            "button_control_status": (
                "enabled" if self.button_control_status == 1 else "disabled"
            ),
        }

    def _handle_get_default_power_status(self, channel, value):
        _ensure_hub_status_dicts(self)
        return _orig_pwr(self, channel, value)

    def _handle_get_default_dataline_status(self, channel, value):
        _ensure_hub_status_dicts(self)
        return _orig_data(self, channel, value)

    def _handle_set_default_power_status(self, channel, value):
        _ensure_hub_status_dicts(self)
        return _orig_set_pwr(self, channel, value)

    def _handle_set_default_dataline_status(self, channel, value):
        _ensure_hub_status_dicts(self)
        return _orig_set_data(self, channel, value)

    def _init_skip_device_info(self):
        """连接阶段不查询整机信息，避免 8+ 条指令挤占串口导致全部 No ACK。"""
        _ensure_hub_status_dicts(self)
        return {"id": self.port.split("/")[-1]}

    def __init__(self, port):
        import time

        old_exit = sys.exit
        old_gdi = SmartUSBHub.get_device_info

        def _no_exit(code=0):
            return None

        sys.exit = _no_exit
        SmartUSBHub.get_device_info = _init_skip_device_info
        try:
            _orig_init(self, port)
        finally:
            sys.exit = old_exit
            SmartUSBHub.get_device_info = get_device_info
        time.sleep(0.2)
        mode = self.get_operate_mode()
        if mode is None:
            log.warning(
                "Hub 串口已打开但未应答（请确认 COM 为 Hub 指令口 1A86:FE0C，且未被占用）"
            )
        else:
            self.operate_mode = mode
            log.info(
                "Hub 已连接 %s，模式: %s",
                self.port,
                "normal" if mode == 0 else "interlock",
            )

    SmartUSBHub.get_device_info = get_device_info
    SmartUSBHub._handle_get_default_power_status = _handle_get_default_power_status
    SmartUSBHub._handle_get_default_dataline_status = _handle_get_default_dataline_status
    SmartUSBHub._handle_set_default_power_status = _handle_set_default_power_status
    SmartUSBHub._handle_set_default_dataline_status = _handle_set_default_dataline_status
    SmartUSBHub.__init__ = __init__
    SmartUSBHub._pl_status_dict_patch = True


_patch_smartusbhub_signal()
_patch_smartusbhub_status_dicts()
