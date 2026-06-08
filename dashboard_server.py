#!/usr/bin/env python3
"""Power Lab — 本地 Web 智能调试服务。"""

from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import serial
import serial.tools.list_ports
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import hub_common  # noqa: F401 — 应用 SmartUSBHub 补丁

from smartusbhub import SmartUSBHub

from hub_common import ROOT
from log_analyzer import full_analyze
from hub_plug import plug_mode, plug_mode_needs_serial_reconnect, product_has_battery, product_type_label
from save_export import (
    _has_current_data,
    _has_voltage_data,
    save_diagnosis_report,
    save_measure_csv,
    save_power_csv,
    save_serial_log,
    save_vi_data,
    ensure_serial_timestamps,
    stamp_serial_line,
)
from settings_store import load_settings, public_settings, save_settings

WEB_DIR = ROOT / "web"
app = Flask(__name__, static_folder=str(WEB_DIR / "static"), static_url_path="/static")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_settings: dict[str, Any] = load_settings()
_settings_lock = threading.Lock()
_session_lock = threading.Lock()


def get_settings() -> dict[str, Any]:
    with _settings_lock:
        return dict(_settings)


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    global _settings
    with _settings_lock:
        _settings = save_settings(patch)
        return dict(_settings)


def list_all_ports() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for p in serial.tools.list_ports.comports():
        vid = f"{p.vid:04X}" if p.vid is not None else ""
        pid = f"{p.pid:04X}" if p.pid is not None else ""
        is_hub = p.vid == 0x1A86 and p.pid == 0xFE0C
        items.append(
            {
                "device": p.device,
                "description": p.description or "",
                "hwid": p.hwid or "",
                "vid": vid,
                "pid": pid,
                "is_hub_command": is_hub,
                "is_likely_esp32": _guess_esp32(p, is_hub),
            }
        )
    return items


def _guess_esp32(port: serial.tools.list_ports.ListPortInfo, is_hub: bool) -> bool:
    if is_hub:
        return False
    text = f"{port.description} {port.manufacturer or ''} {port.hwid or ''}".upper()
    keys = ("ESP", "USB JTAG", "SERIAL", "CH340", "CP210", "FTDI", "303A", "10C4")
    return any(k in text for k in keys)


def suggest_ports(ports: list[dict[str, str]]) -> dict[str, str]:
    hub = next((p["device"] for p in ports if p["is_hub_command"]), "")
    esp = next((p["device"] for p in ports if p["is_likely_esp32"]), "")
    if not esp:
        esp = next(
            (p["device"] for p in ports if not p["is_hub_command"]),
            "",
        )
    return {"hub_command_port": hub, "esp32_serial_port": esp}


def _port_is_hub_command(port: str) -> bool:
    """所选 COM 是否为 SmartUSB Hub 指令口（与 list_all_ports 判定一致）。"""
    for p in serial.tools.list_ports.comports():
        if p.device == port:
            return p.vid == 0x1A86 and p.pid == 0xFE0C
    return True


class HubManager:
    def __init__(self) -> None:
        self.hub: Optional[SmartUSBHub] = None
        self.last_error: str = ""
        self.info: Optional[dict] = None
        self.link_ok: bool = False

    def disconnect(self) -> None:
        if self.hub:
            try:
                self.hub.disconnect()
            except Exception:
                pass
        self.hub = None
        self.info = None
        self.link_ok = False

    def connect(self, port: str = "") -> bool:
        self.disconnect()
        self.last_error = ""
        try:
            if port and not _port_is_hub_command(port):
                self.last_error = (
                    f"{port} 不是 Hub 指令口（需 VID 1A86 / PID FE0C），"
                    "请在端口列表中选带 Hub 标记的 COM"
                )
                return False
            if port:
                self.hub = SmartUSBHub(port)
            else:
                self.hub = SmartUSBHub.scan_and_connect()
            if self.hub is None:
                self.last_error = "未找到 SmartUSB Hub"
                return False
            self.hub.com_timeout = HUB_COM_TIMEOUT_S
            self.info = {
                "id": port or "scan",
                "hardware_version": getattr(self.hub, "hardware_version", None),
                "firmware_version": getattr(self.hub, "firmware_version", None),
            }
            self.link_ok = False
            time.sleep(0.15)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.hub = None
            return False

    def ping(self) -> bool:
        """轻量探测：当前通道电源状态可读即视为 Hub 应答正常。"""
        if not self.hub:
            return False
        ch = self.channel()
        try:
            with _hub_uart_lock:
                status = self.hub.get_channel_power_status(ch)
            self.link_ok = status is not None
            return self.link_ok
        except Exception as exc:
            self.last_error = str(exc)
            self.link_ok = False
            return False

    def ensure_connected(self, port: str = "") -> bool:
        """连接并探测；无应答时断开重连一次。"""
        self.last_error = ""
        hub_port = port or (get_settings().get("hub_command_port") or "")
        if self.hub is None and not self.connect(hub_port):
            return False
        if self.ping():
            return True
        self.disconnect()
        time.sleep(0.25)
        if not self.connect(hub_port):
            return False
        if self.ping():
            return True
        self.last_error = (
            "Hub 无应答：请确认所选 COM 为 Hub 指令口（设备管理器中 VID 1A86 / PID FE0C），"
            "且未被其他软件占用"
        )
        return False

    def channel(self) -> int:
        return int(get_settings().get("hub_dut_channel", 1))

    def read_sample(self) -> tuple[Optional[int], Optional[int]]:
        if not self.hub or not self.link_ok:
            return None, None
        ch = self.channel()
        try:
            with _hub_uart_lock:
                v = self.hub.get_channel_voltage(ch)
                i = self.hub.get_channel_current(ch)
            if v is not None or i is not None:
                self.link_ok = True
            else:
                self.link_ok = False
            return v, i
        except Exception as exc:
            self.last_error = str(exc)
            self.link_ok = False
            return None, None


HUB_COM_TIMEOUT_S = 0.35
HUB_CMD_RETRIES = 3

_hub = HubManager()
_hub_uart_lock = threading.Lock()


class MonitorSession:
    def __init__(self) -> None:
        self.serial_open = False
        self.serial_capturing = False
        self.vi_running = False
        self.v_sampling = False
        self.i_sampling = False
        self._stop_serial = threading.Event()
        self._stop_vi = threading.Event()
        self._serial_thread: Optional[threading.Thread] = None
        self._serial_handle: Optional[serial.Serial] = None
        self._vi_thread: Optional[threading.Thread] = None
        self.power_samples: list[dict[str, Any]] = []
        self.serial_full: str = ""
        self.vi_run_dir: Optional[Path] = None
        self.log_run_dir: Optional[Path] = None
        self.vi_t0: float = 0.0
        self.log_t0: float = 0.0
        self.scenario_running = False
        self.plug_event_t_ms: Optional[int] = None
        self.plug_event_times_ms: list[int] = []
        self._hub_cmd_hold = threading.Event()

    def open_serial(self) -> dict[str, Any]:
        s = get_settings()
        port = s.get("esp32_serial_port") or ""
        if not port:
            return {"ok": False, "msg": "请选择 ESP32 日志口"}
        if self.serial_open:
            return {"ok": True, "msg": "串口已打开"}
        self._stop_serial.clear()
        self._serial_thread = threading.Thread(target=self._serial_loop, daemon=True)
        self._serial_thread.start()
        for _ in range(50):
            if self.serial_open:
                return {"ok": True, "msg": f"串口已打开 {port}"}
            time.sleep(0.1)
        return {"ok": False, "msg": f"无法打开串口 {port}"}

    def _release_serial_handle(self) -> None:
        """关闭串口句柄，便于读线程在重连/断开时尽快退出。"""
        handle = self._serial_handle
        self._serial_handle = None
        if handle is None:
            return
        try:
            if handle.is_open:
                handle.close()
        except serial.SerialException:
            pass

    def close_serial(self, *, stop_capture: bool = True) -> None:
        if stop_capture and self.serial_capturing:
            self.stop_serial_capture()
        self._stop_serial.set()
        self._release_serial_handle()
        if self._serial_thread:
            self._serial_thread.join(timeout=3.0)
            self._serial_thread = None
        self.serial_open = False
        _broadcast_status()

    def reopen_serial(self, *, preserve_capture: bool = True) -> dict[str, Any]:
        """重开日志串口；场景插拔后 preserve_capture 保留已录日志并继续录制。"""
        was_capturing = self.serial_capturing
        if self.serial_open:
            self.close_serial(stop_capture=not preserve_capture)
        if preserve_capture and was_capturing:
            marker = (
                f"\n# [{datetime.now().isoformat(timespec='seconds')}] "
                "串口重连（插拔后，继续录制）\n"
            )
            self.serial_full += marker
            socketio.emit("serial_line", {"line": marker})
        info = self.open_serial()
        if info.get("ok") and preserve_capture and was_capturing:
            self.serial_capturing = True
            _broadcast_status()
        return info

    def start_serial_capture(self) -> dict[str, Any]:
        if not self.serial_open:
            return {"ok": False, "msg": "请先应用并连接，打开串口"}
        if self.serial_capturing:
            return {"ok": False, "msg": "日志已在录制中"}
        self.serial_full = ""
        self.log_t0 = time.time()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_run_dir = ROOT / "logs" / f"{stamp}_serial"
        self.log_run_dir.mkdir(parents=True, exist_ok=True)
        self.serial_capturing = True
        _broadcast_status()
        return {"ok": True, "dir": str(self.log_run_dir)}

    def stop_serial_capture(self) -> dict[str, Any]:
        if not self.serial_capturing:
            return {"ok": False, "msg": "未在录制日志"}
        self.serial_capturing = False
        saved_at = datetime.now().isoformat(timespec="seconds")
        if self.log_run_dir:
            header = (
                f"# saved_at={saved_at}\n"
                f"# recording_started={datetime.fromtimestamp(self.log_t0).isoformat(timespec='seconds')}\n"
            )
            (self.log_run_dir / "serial.log").write_text(
                header + ensure_serial_timestamps(self.serial_full, saved_at),
                encoding="utf-8",
                errors="replace",
            )
        _broadcast_status()
        return {"ok": True, "dir": str(self.log_run_dir) if self.log_run_dir else ""}

    def _sampling_active(self) -> bool:
        return self.v_sampling or self.i_sampling

    def start_vi(self, *, voltage: bool = True, current: bool = True) -> dict[str, Any]:
        """启动曲线采集；场景测试默认同时采电压与电流。"""
        if not _hub.hub:
            return {"ok": False, "msg": "Hub 未连接，无法采集电压/电流"}
        if voltage:
            self.v_sampling = True
        if current:
            self.i_sampling = True
        if self.vi_running:
            _broadcast_status()
            return {"ok": True, "msg": "曲线已在采集", "dir": str(self.vi_run_dir or "")}
        return self._start_vi_thread()

    def toggle_measure(self, kind: str) -> dict[str, Any]:
        if not _hub.hub:
            return {"ok": False, "msg": "Hub 未连接，无法采集电压/电流"}
        if kind == "voltage":
            self.v_sampling = not self.v_sampling
            label = "电压"
            now_on = self.v_sampling
        elif kind == "current":
            self.i_sampling = not self.i_sampling
            label = "电流"
            now_on = self.i_sampling
        else:
            return {"ok": False, "msg": f"未知采样类型 {kind}"}

        msg = f"{'已开始' if now_on else '已停止'}{label}采集"

        if self._sampling_active():
            if self.vi_running:
                _broadcast_status()
                return {
                    "ok": True,
                    "msg": msg,
                    "v_sampling": self.v_sampling,
                    "i_sampling": self.i_sampling,
                }
            return self._start_vi_thread(msg=msg)

        self.stop_vi()
        return {
            "ok": True,
            "msg": msg,
            "v_sampling": False,
            "i_sampling": False,
        }

    def _start_vi_thread(self, msg: str = "已开始采集") -> dict[str, Any]:
        self.power_samples.clear()
        self._stop_vi.clear()
        self.vi_running = True
        self.vi_t0 = time.time()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.vi_run_dir = ROOT / "logs" / f"{stamp}_vi"
        self.vi_run_dir.mkdir(parents=True, exist_ok=True)
        self._vi_thread = threading.Thread(target=self._vi_loop, daemon=True)
        self._vi_thread.start()
        _broadcast_status()
        return {
            "ok": True,
            "msg": msg,
            "dir": str(self.vi_run_dir),
            "v_sampling": self.v_sampling,
            "i_sampling": self.i_sampling,
        }

    def stop_vi(self) -> None:
        if not self.vi_running:
            self.v_sampling = False
            self.i_sampling = False
            return
        self._stop_vi.set()
        self.vi_running = False
        self.v_sampling = False
        self.i_sampling = False
        if self._vi_thread:
            self._vi_thread.join(timeout=2.0)
        self._vi_thread = None
        self._save_vi_csv()
        _broadcast_status()

    def _save_vi_csv(self) -> None:
        if not self.vi_run_dir or not self.power_samples:
            return
        saved_at = datetime.now().isoformat(timespec="seconds")
        with (self.vi_run_dir / "power.csv").open("w", newline="", encoding="utf-8") as f:
            f.write(f"# saved_at={saved_at}\n")
            f.write(
                f"# recording_started={datetime.fromtimestamp(self.vi_t0).isoformat(timespec='seconds')}\n"
            )
            w = csv.writer(f)
            w.writerow(["wall_time", "t_ms", "v_mV", "i_mA"])
            for row in self.power_samples:
                t_ms = row.get("t")
                if t_ms is not None:
                    wall = datetime.fromtimestamp(
                        self.vi_t0 + int(t_ms) / 1000.0
                    ).isoformat(timespec="milliseconds")
                else:
                    wall = saved_at
                w.writerow([wall, t_ms if t_ms is not None else "", row.get("v", ""), row.get("i", "")])

    def _vi_loop(self) -> None:
        interval_ms = max(int(get_settings().get("sample_interval_ms", 50)), 50)
        interval = max(0.05, interval_ms / 1000.0)
        threshold = int(get_settings().get("reboot_current_threshold_ma", 30))
        last_reboot_ms = -10_000
        while not self._stop_vi.is_set():
            if self._hub_cmd_hold.is_set():
                time.sleep(0.05)
                continue
            t_ms = int((time.time() - self.vi_t0) * 1000)
            v_raw, i_raw = _hub.read_sample()
            sample: dict[str, Any] = {"t": t_ms}
            store: dict[str, Any] = {"t": t_ms}
            if self.v_sampling:
                sample["v"] = v_raw
                store["v"] = v_raw
            if self.i_sampling:
                sample["i"] = i_raw
                store["i"] = i_raw
            self.power_samples.append(store)
            socketio.emit("power_sample", sample)
            i = i_raw if self.i_sampling else None
            if i is not None and i <= threshold and (t_ms - last_reboot_ms) >= 200:
                last_reboot_ms = t_ms
                socketio.emit(
                    "toast",
                    {
                        "level": "warn",
                        "msg": f"[{t_ms} ms] 电流跌至 {i} mA，疑似掉电/重启",
                    },
                )
                socketio.emit("reboot_hint", {"t": t_ms, "i": i, "v": v_raw})
            time.sleep(interval)

    def _serial_loop(self) -> None:
        s = get_settings()
        port = s.get("esp32_serial_port") or ""
        baud = int(s.get("esp32_baud", 115200))
        ser: Optional[serial.Serial] = None
        deadline = time.time() + 20.0
        while not self._stop_serial.is_set() and time.time() < deadline:
            try:
                ser = serial.Serial(port, baud, timeout=0.05)
                self._serial_handle = ser
                self.serial_open = True
                _broadcast_status()
                break
            except serial.SerialException:
                time.sleep(0.4)
        if ser is None:
            self.serial_open = False
            _broadcast_status()
            return
        try:
            while not self._stop_serial.is_set():
                try:
                    chunk = ser.read(2048)
                except serial.SerialException:
                    break
                if not chunk:
                    continue
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines(keepends=True):
                    stamped = stamp_serial_line(line)
                    if not stamped:
                        continue
                    if self.serial_capturing:
                        self.serial_full += stamped
                    socketio.emit("serial_line", {"line": stamped})
        finally:
            self._release_serial_handle()
            self.serial_open = False
            _broadcast_status()


def _ensure_vi_started() -> bool:
    with _session_lock:
        already_both = (
            _session.vi_running and _session.v_sampling and _session.i_sampling
        )
        info = _session.start_vi(voltage=True, current=True)
    if not info.get("ok"):
        socketio.emit("toast", {"level": "error", "msg": info.get("msg", "曲线采集启动失败")})
        return False
    if not already_both:
        socketio.emit("toast", {"level": "info", "msg": "已开始采集电压/电流曲线"})
    return True


def _ensure_log_capture_started() -> bool:
    with _session_lock:
        if not _session.serial_open:
            open_info = _session.open_serial()
            if not open_info.get("ok"):
                socketio.emit(
                    "toast",
                    {"level": "error", "msg": open_info.get("msg", "无法打开串口")},
                )
                return False
        if _session.serial_capturing:
            return True
        info = _session.start_serial_capture()
    if not info.get("ok"):
        socketio.emit("toast", {"level": "error", "msg": info.get("msg", "日志录制启动失败")})
        return False
    socketio.emit("toast", {"level": "info", "msg": "已开始录制串口日志"})
    return True


_session = MonitorSession()
_status_thread_started = False


def hub_available(ports: Optional[list[dict[str, str]]] = None) -> bool:
    """本机是否识别到 SmartUSB Hub（已连接、端口列表或已选指令口）。"""
    if _hub.hub is not None:
        return True
    if ports is None:
        ports = list_all_ports()
    if any(p.get("is_hub_command") for p in ports):
        return True
    s = get_settings()
    hub_port = (s.get("hub_command_port") or "").strip()
    if hub_port and any(p.get("device") == hub_port for p in ports):
        return True
    return False


_CONNECT_SETTING_KEYS = (
    "hub_command_port",
    "hub_dut_channel",
    "esp32_serial_port",
    "esp32_baud",
)


def _settings_patch_from_body(body: Optional[dict]) -> dict[str, Any]:
    if not body:
        return {}
    return {k: body[k] for k in _CONNECT_SETTING_KEYS if k in body}


def _reconnect_devices(
    *,
    require_hub: bool = True,
    refresh_serial: bool = False,
    settings_patch: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """尝试连接 Hub 与 ESP32 串口（快捷指令/场景前调用）。"""
    if settings_patch:
        patch = _settings_patch_from_body(settings_patch)
        if patch:
            update_settings(patch)

    result: dict[str, Any] = {
        "ok": True,
        "hub_connected": _hub.hub is not None,
        "serial_open": _session.serial_open,
        "hub_error": "",
        "serial_error": "",
    }

    s = get_settings()
    hub_port = s.get("hub_command_port") or ""
    esp_port = s.get("esp32_serial_port") or ""

    if hub_available():
        ok = _hub.ensure_connected(hub_port)
        result["hub_connected"] = ok
        if not ok:
            result["hub_error"] = _hub.last_error or "Hub 未连接，请检查指令口"
            if require_hub:
                result["ok"] = False
    elif require_hub:
        result["hub_error"] = "未检测到 SmartUSB Hub"
        result["ok"] = False

    if esp_port:
        with _session_lock:
            if refresh_serial and _session.serial_open:
                if _session.scenario_running and _session.serial_capturing:
                    serial_info = _session.reopen_serial(preserve_capture=True)
                    result["serial_open"] = bool(serial_info.get("ok"))
                    if not serial_info.get("ok"):
                        result["serial_error"] = serial_info.get("msg", "串口打开失败")
                else:
                    _session.close_serial()
            if not _session.serial_open:
                serial_info = _session.open_serial()
                result["serial_open"] = bool(serial_info.get("ok"))
                if not serial_info.get("ok"):
                    result["serial_error"] = serial_info.get("msg", "串口打开失败")
            else:
                result["serial_open"] = True
    else:
        result["serial_error"] = "未选择 ESP32 日志口"

    _broadcast_status()
    return result


def _prepare_shortcut(
    data: Optional[dict] = None,
    *,
    require_hub: bool = True,
    refresh_serial: bool = False,
) -> Optional[str]:
    body = data or {}
    refresh = bool(body.get("refresh_serial", refresh_serial))
    info = _reconnect_devices(
        require_hub=require_hub,
        refresh_serial=refresh,
        settings_patch=body,
    )
    if info.get("serial_error") and not info.get("serial_open"):
        socketio.emit(
            "toast",
            {"level": "warn", "msg": info["serial_error"]},
        )
    if not info.get("ok"):
        return info.get("hub_error") or "Hub 未连接"
    return None


def build_device_status() -> dict[str, Any]:
    s = get_settings()
    ports = list_all_ports()
    esp_port = s.get("esp32_serial_port") or ""
    hub_port = s.get("hub_command_port") or ""
    esp_listed = any(p["device"] == esp_port for p in ports)
    hub_listed = any(p["device"] == hub_port for p in ports) if hub_port else False
    hub_ok = hub_available(ports)

    v, i = (None, None)
    sampling = _session.v_sampling or _session.i_sampling
    if (
        _hub.hub
        and _hub.link_ok
        and sampling
        and not _session._hub_cmd_hold.is_set()
    ):
        v, i = _hub.read_sample()

    return {
        "hub_connected": _hub.hub is not None and _hub.link_ok,
        "hub_link_ok": _hub.link_ok,
        "hub_available": hub_ok,
        "hub_error": _hub.last_error,
        "hub_info": _hub.info,
        "hub_port_configured": bool(hub_port),
        "hub_port_listed": hub_listed,
        "hub_channel": int(s.get("hub_dut_channel", 1)),
        "esp32_port_configured": bool(esp_port),
        "esp32_port_listed": esp_listed,
        "serial_open": _session.serial_open,
        "serial_capturing": _session.serial_capturing,
        "vi_running": _session.vi_running,
        "v_sampling": _session.v_sampling,
        "i_sampling": _session.i_sampling,
        "recording": _session.vi_running or _session.serial_capturing,
        "scenario_running": _session.scenario_running,
        "product_type": s.get("product_type", "usb_only"),
        "plug_mode": plug_mode(s),
        "product_label": product_type_label(s),
        "product_name": s.get("product_name", ""),
        "live_v_mv": v,
        "live_i_ma": i,
    }


def _broadcast_status() -> None:
    socketio.emit("device_status", build_device_status())


def _emit_progress(step: str, detail: str = "", pct: int = 0) -> None:
    socketio.emit(
        "scenario_progress",
        {"step": step, "detail": detail, "pct": pct},
    )


def _hub_run_cmd(cmd: Callable[[], bool]) -> bool:
    """在 UART 锁下执行 Hub 命令，失败时短暂重试并可触发重连。"""
    if not _hub.hub:
        return False
    for attempt in range(HUB_CMD_RETRIES):
        with _hub_uart_lock:
            if cmd():
                time.sleep(0.08)
                return True
        if attempt < HUB_CMD_RETRIES - 1:
            time.sleep(0.12)
    hub_port = get_settings().get("hub_command_port") or ""
    if _hub.ensure_connected(hub_port):
        with _hub_uart_lock:
            if cmd():
                time.sleep(0.08)
                return True
    return False


def _hub_power(on: bool) -> bool:
    if not _hub.hub:
        return False
    ch = _hub.channel()
    state = 1 if on else 0
    return _hub_run_cmd(lambda: bool(_hub.hub.set_channel_power(ch, state=state)))


def _hub_dataline(connect: bool) -> bool:
    if not _hub.hub:
        return False
    ch = _hub.channel()
    state = 1 if connect else 0
    return _hub_run_cmd(
        lambda: bool(_hub.hub.set_channel_dataline(ch, state=state))
    )


def _read_hub_channel_state() -> dict[str, Optional[int]]:
    """读取当前被测通道 VBUS 与数据线开关状态（1=开/通，0=关/断）。"""
    if not _hub.hub:
        return {"power": None, "dataline": None}
    ch = _hub.channel()
    try:
        with _hub_uart_lock:
            power = _hub.hub.get_channel_power_status(ch)
            dataline = _hub.hub.get_channel_dataline_status(ch)
        if isinstance(power, dict):
            power = power.get(ch)
        if isinstance(dataline, dict):
            dataline = dataline.get(ch)
        return {"power": power, "dataline": dataline}
    except Exception:
        return {"power": None, "dataline": None}


def _format_hub_channel_state(state: dict[str, Optional[int]]) -> str:
    pwr = state.get("power")
    data = state.get("dataline")
    p_txt = "开" if pwr == 1 else "关" if pwr == 0 else "—"
    d_txt = "通" if data == 1 else "断" if data == 0 else "—"
    return f"VBUS={p_txt}，数据线={d_txt}"


def _do_hard_reboot(*, refresh_serial: bool = True) -> tuple[bool, str]:
    """通道硬重启：断电 → 等待 → 上电（独占 Hub 串口，避免与曲线采样冲突）。"""
    hub_port = get_settings().get("hub_command_port") or ""
    if not _hub.ensure_connected(hub_port):
        return False, _hub.last_error or "Hub 未连接，请先应用并连接"
    off_s = float(get_settings().get("power_off_seconds", 0.8))
    _session._hub_cmd_hold.set()
    try:
        if not _hub_power(False):
            return False, "硬重启失败：断电无应答，请检查 Hub 指令口"
        time.sleep(off_s)
        if not _hub_power(True):
            return False, "硬重启失败：上电无应答，请检查 Hub 指令口"
    finally:
        time.sleep(0.15)
        _session._hub_cmd_hold.clear()
    if refresh_serial:
        _reconnect_serial_after_plug(quiet=False)
    return True, f"硬重启完成（断电 {off_s:g}s → 上电），已尝试重连串口"


def _cycle_usb_plug() -> Optional[int]:
    """模拟 USB 拔线/插入，返回「插入完成」时刻（相对曲线 t0 的 ms）。"""
    _session._hub_cmd_hold.set()
    try:
        return _cycle_usb_plug_locked()
    finally:
        time.sleep(0.15)
        _session._hub_cmd_hold.clear()


def _cycle_usb_plug_locked() -> Optional[int]:
    """模拟 USB 拔线/插入，返回「插入完成」时刻（相对曲线 t0 的 ms）。"""
    s = get_settings()
    off_s = float(s.get("power_off_seconds", 0.8))
    dataline_delay = float(s.get("dataline_delay_seconds", 0.15))
    mode = plug_mode(s)
    label = product_type_label(s)

    def _mark_plug() -> int:
        return int((time.time() - _session.vi_t0) * 1000)

    if mode == "both":
        _emit_progress("disconnect", f"{label}：拔线（断电 + 断开数据）", 20)
        _hub_power(False)
        _hub_dataline(False)
        time.sleep(off_s)
        _emit_progress("connect", "插入：VBUS 上电", 32)
        _hub_power(True)
        time.sleep(dataline_delay)
        _emit_progress("connect", "插入：连接 USB 数据", 40)
        _hub_dataline(True)
        return _mark_plug()
    if mode == "dataline":
        _emit_progress("disconnect", f"{label}：断开 USB 数据", 20)
        _hub_dataline(False)
        time.sleep(off_s)
        _emit_progress("connect", "重新连接 USB 数据", 40)
        _hub_dataline(True)
        return _mark_plug()
    if mode == "vbus_only":
        _emit_progress("disconnect", f"{label}：切断 VBUS（电池供电，串口保持）", 20)
        _hub_power(False)
        time.sleep(off_s)
        _emit_progress("connect", "恢复 VBUS（USB 供电）", 40)
        _hub_power(True)
        return _mark_plug()
    _emit_progress("disconnect", f"{label}：断电（模拟拔线）", 20)
    _hub_power(False)
    time.sleep(off_s)
    _emit_progress("connect", "上电（模拟插入）", 40)
    _hub_power(True)
    return _mark_plug()


def _reconnect_serial_after_plug(*, quiet: bool = False) -> None:
    """插拔后设备可能复位，延迟后重开日志串口并继续录制。"""
    delay = float(get_settings().get("serial_reconnect_delay_seconds", 1.5))
    max_retries = int(get_settings().get("serial_reconnect_retries", 8))
    retry_interval = float(get_settings().get("serial_reconnect_retry_seconds", 1.0))
    if delay > 0:
        time.sleep(delay)
    last_msg = "串口打开失败"
    with _session_lock:
        for attempt in range(max_retries):
            info = _session.reopen_serial(preserve_capture=True)
            if info.get("ok"):
                if not quiet:
                    socketio.emit(
                        "toast",
                        {"level": "ok", "msg": "串口已重连，继续录制复位日志"},
                    )
                _broadcast_status()
                return
            last_msg = info.get("msg", last_msg)
            if attempt < max_retries - 1:
                time.sleep(retry_interval)
    socketio.emit(
        "toast",
        {"level": "warn", "msg": f"插拔后串口重连失败（{max_retries} 次）：{last_msg}"},
    )


def _plug_toast_message(mode: str, v0: Optional[int], i0: Optional[int]) -> str:
    v_txt = f"{v0} mV" if v0 is not None else "—"
    i_txt = f"{i0} mA" if i0 is not None else "—"
    if mode == "both":
        action = "已模拟 USB 拔插（断电+断数据 → 上电+连数据）"
    elif mode == "dataline":
        action = "已模拟 USB 数据重连（断数据 → 连数据）"
    elif mode == "vbus_only":
        action = "已切断并恢复 VBUS（电池供电期间串口保持）"
    else:
        action = "已模拟 USB 拔插（断电 → 上电）"
    return f"{action}；接入后 V={v_txt} I={i_txt}，观察日志与曲线"


def _run_usb_cycle_scenario(data: Optional[dict] = None) -> None:
    err = _prepare_shortcut(data, require_hub=True, refresh_serial=True)
    if err:
        socketio.emit("toast", {"level": "error", "msg": err})
        return
    if _session.scenario_running:
        socketio.emit("toast", {"level": "warn", "msg": "已有测试在进行"})
        return
    s = get_settings()
    if not s.get("esp32_serial_port"):
        socketio.emit("toast", {"level": "error", "msg": "请先选择 ESP32 串口"})
        return

    def worker() -> None:
        _session.scenario_running = True
        _broadcast_status()
        try:
            if not _ensure_vi_started():
                return
            if not _ensure_log_capture_started():
                return
            if not _session.serial_capturing:
                socketio.emit("toast", {"level": "error", "msg": "串口日志录制未启动"})
                return

            _session.plug_event_t_ms = None
            _session.plug_event_times_ms = []
            baseline_s = float(s.get("scenario_baseline_seconds", 3.0))
            _emit_progress("baseline", f"记录插拔前基线 {baseline_s:.0f}s…", 5)
            time.sleep(baseline_s)

            repeat = max(1, int((data or {}).get("repeat_count") or s.get("scenario_repeat_count", 5)))
            cycle_wait = max(
                0,
                int((data or {}).get("cycle_wait_seconds") or s.get("scenario_cycle_wait_seconds", 4)),
            )
            mode = plug_mode(s)

            for cycle in range(1, repeat + 1):
                pct = 10 + int(50 * (cycle - 1) / repeat)
                _emit_progress("cycle", f"第 {cycle}/{repeat} 次 USB 插拔…", pct)

                marker = (
                    f"\n# [{datetime.now().isoformat(timespec='seconds')}] "
                    f"=== 第 {cycle}/{repeat} 次 USB 插拔 ===\n"
                )
                _session.serial_full += marker
                socketio.emit("serial_line", {"line": marker})

                plug_t = _cycle_usb_plug()
                if plug_t is not None:
                    _session.plug_event_times_ms.append(plug_t)
                    _session.plug_event_t_ms = plug_t

                v0, i0 = _hub.read_sample()
                if plug_t is not None and (v0 is not None or i0 is not None):
                    socketio.emit(
                        "power_sample",
                        {
                            "t": plug_t,
                            "v": v0,
                            "i": i0,
                            "snapshot": True,
                            "cycle": cycle,
                        },
                    )
                msg = _plug_toast_message(mode, v0, i0)
                socketio.emit(
                    "toast",
                    {"level": "info", "msg": f"第 {cycle}/{repeat} 次：{msg}"},
                )

                if plug_mode_needs_serial_reconnect(mode):
                    _emit_progress(
                        "reconnect",
                        f"第 {cycle}/{repeat} 次：插拔后重连串口…",
                        pct + 5,
                    )
                    _reconnect_serial_after_plug(quiet=(cycle > 1))

                if cycle < repeat and cycle_wait > 0:
                    _emit_progress(
                        "observe",
                        f"第 {cycle}/{repeat} 次完成，{cycle_wait}s 后进行下一次插拔…",
                        pct + 8,
                    )
                    for _ in range(cycle_wait):
                        if not _session.vi_running:
                            break
                        time.sleep(1.0)

            wait_s = int(s.get("scenario_wait_seconds", 25))
            _emit_progress(
                "observe",
                f"全部 {repeat} 次插拔完成，最后观察 {wait_s}s…",
                65,
            )
            for n in range(wait_s):
                if not _session.vi_running:
                    break
                pct = 65 + int(30 * (n + 1) / wait_s)
                _emit_progress("observe", f"剩余 {wait_s - n}s", pct)
                time.sleep(1.0)

            _emit_progress("analyze", "分析全部插拔周期的 V/I 与复位日志…", 92)
            cfg = dict(get_settings())
            if _session.plug_event_times_ms:
                cfg["scenario_plug_times_ms"] = list(_session.plug_event_times_ms)
                cfg["scenario_plug_t_ms"] = _session.plug_event_times_ms[-1]
            elif _session.plug_event_t_ms is not None:
                cfg["scenario_plug_t_ms"] = _session.plug_event_t_ms
            serial_text = _session.serial_full
            if not serial_text.strip():
                socketio.emit(
                    "toast",
                    {
                        "level": "warn",
                        "msg": "串口日志为空：请确认 ESP32 日志口、波特率，或插拔后是否成功重连",
                    },
                )
            result = full_analyze(serial_text, _session.power_samples, cfg)
            analysis_dir = _session.log_run_dir or _session.vi_run_dir
            if analysis_dir:
                (analysis_dir / "analysis.json").write_text(
                    json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            socketio.emit("analysis_result", result.to_dict())
            verdict = "疑似软件/日志异常" if any(
                f.get("severity") == "critical" for f in result.to_dict().get("findings", [])
            ) else "未发现典型软件崩溃特征"
            if any(
                f.get("category") == "power" for f in result.to_dict().get("findings", [])
            ):
                verdict += "；供电侧有异常迹象，建议查硬件/Hub"
            _emit_progress("done", verdict, 100)
            socketio.emit(
                "toast",
                {
                    "level": "ok",
                    "msg": f"场景测试完成（{repeat} 次插拔）：{verdict}",
                },
            )
        finally:
            _session.scenario_running = False
            _broadcast_status()

    threading.Thread(target=worker, daemon=True).start()


def _mark_vi_event_ms() -> int:
    return int((time.time() - _session.vi_t0) * 1000)


def _cut_vbus_for_battery() -> Optional[int]:
    """仅断 VBUS，D+/D- 保持连通，设备改电池供电且串口不断。"""
    _session._hub_cmd_hold.set()
    try:
        _emit_progress("disconnect", "切断 VBUS（改电池供电，串口保持）", 25)
        if not _hub_power(False):
            socketio.emit("toast", {"level": "error", "msg": "切断 VBUS 失败，请检查 Hub"})
            return None
        return _mark_vi_event_ms()
    finally:
        time.sleep(0.15)
        _session._hub_cmd_hold.clear()


def _restore_vbus_from_battery() -> Optional[int]:
    """恢复 Hub VBUS 供电。"""
    _session._hub_cmd_hold.set()
    try:
        _emit_progress("connect", "恢复 VBUS（USB 供电）", 55)
        if not _hub_power(True):
            socketio.emit("toast", {"level": "error", "msg": "恢复 VBUS 失败，请检查 Hub"})
            return None
        return _mark_vi_event_ms()
    finally:
        time.sleep(0.15)
        _session._hub_cmd_hold.clear()


def _ensure_usb_data_connected() -> bool:
    """电池场景前确保数据通路已接通。"""
    if not _hub_dataline(True):
        socketio.emit("toast", {"level": "warn", "msg": "连接 USB 数据无应答，请检查 Hub 通道"})
        return False
    return True


def _run_battery_only_scenario(data: Optional[dict] = None) -> None:
    err = _prepare_shortcut(data, require_hub=True, refresh_serial=True)
    if err:
        socketio.emit("toast", {"level": "error", "msg": err})
        return
    if _session.scenario_running:
        socketio.emit("toast", {"level": "warn", "msg": "已有测试在进行"})
        return
    s = get_settings()
    if not product_has_battery(s):
        socketio.emit(
            "toast",
            {
                "level": "error",
                "msg": "「仅电池供电 + 串口监测」仅适用于带电池产品，请将产品类型选为「带电池」",
            },
        )
        return
    if not s.get("esp32_serial_port"):
        socketio.emit("toast", {"level": "error", "msg": "请先选择 ESP32 串口"})
        return

    def worker() -> None:
        _session.scenario_running = True
        _broadcast_status()
        try:
            if not _ensure_vi_started():
                return
            if not _ensure_log_capture_started():
                return
            if not _session.serial_capturing:
                socketio.emit("toast", {"level": "error", "msg": "串口日志录制未启动"})
                return
            if not _ensure_usb_data_connected():
                return

            _session.plug_event_t_ms = None
            _session.plug_event_times_ms = []
            baseline_s = float(s.get("scenario_baseline_seconds", 3.0))
            _emit_progress("baseline", f"记录 USB 供电基线 {baseline_s:.0f}s…", 5)
            time.sleep(baseline_s)

            repeat = max(1, int((data or {}).get("repeat_count") or s.get("scenario_repeat_count", 5)))
            raw_bat = (data or {}).get("battery_only_seconds")
            if raw_bat is not None:
                battery_s = max(1, int(raw_bat))
            else:
                from_wait = (data or {}).get("cycle_wait_seconds")
                if from_wait is not None and int(from_wait) >= 10:
                    battery_s = max(1, int(from_wait))
                else:
                    battery_s = max(1, int(s.get("scenario_battery_only_seconds", 120)))

            for cycle in range(1, repeat + 1):
                pct_base = 10 + int(70 * (cycle - 1) / repeat)
                _emit_progress(
                    "cycle",
                    f"第 {cycle}/{repeat} 轮：准备切断 VBUS…",
                    pct_base,
                )

                marker = (
                    f"\n# [{datetime.now().isoformat(timespec='seconds')}] "
                    f"=== 第 {cycle}/{repeat} 轮 仅电池供电（串口保持）===\n"
                )
                _session.serial_full += marker
                socketio.emit("serial_line", {"line": marker})

                cut_t = _cut_vbus_for_battery()
                if cut_t is not None:
                    _session.plug_event_times_ms.append(cut_t)
                    _session.plug_event_t_ms = cut_t

                v_cut, i_cut = _hub.read_sample()
                if cut_t is not None and (v_cut is not None or i_cut is not None):
                    socketio.emit(
                        "power_sample",
                        {
                            "t": cut_t,
                            "v": v_cut,
                            "i": i_cut,
                            "snapshot": True,
                            "cycle": cycle,
                            "phase": "battery_cut",
                        },
                    )
                socketio.emit(
                    "toast",
                    {
                        "level": "info",
                        "msg": (
                            f"第 {cycle}/{repeat} 轮：VBUS 已切断，设备改电池供电；"
                            f"串口保持，观测 {battery_s}s…"
                        ),
                    },
                )

                for n in range(battery_s):
                    if not _session.vi_running:
                        break
                    pct = pct_base + int(40 * (n + 1) / battery_s)
                    _emit_progress(
                        "observe",
                        f"第 {cycle}/{repeat} 轮仅电池运行中，剩余 {battery_s - n}s",
                        pct,
                    )
                    time.sleep(1.0)

                restore_t = _restore_vbus_from_battery()
                if restore_t is not None:
                    _session.plug_event_times_ms.append(restore_t)

                v_rst, i_rst = _hub.read_sample()
                if restore_t is not None and (v_rst is not None or i_rst is not None):
                    socketio.emit(
                        "power_sample",
                        {
                            "t": restore_t,
                            "v": v_rst,
                            "i": i_rst,
                            "snapshot": True,
                            "cycle": cycle,
                            "phase": "vbus_restore",
                        },
                    )
                socketio.emit(
                    "toast",
                    {
                        "level": "info",
                        "msg": (
                            f"第 {cycle}/{repeat} 轮：VBUS 已恢复；"
                            f"V={v_rst if v_rst is not None else '—'} mV "
                            f"I={i_rst if i_rst is not None else '—'} mA"
                        ),
                    },
                )

            wait_s = int(s.get("scenario_wait_seconds", 25))
            _emit_progress(
                "observe",
                f"全部 {repeat} 轮完成，USB 供电下再观察 {wait_s}s…",
                85,
            )
            for n in range(wait_s):
                if not _session.vi_running:
                    break
                pct = 85 + int(10 * (n + 1) / wait_s)
                _emit_progress("observe", f"USB 供电观察剩余 {wait_s - n}s", pct)
                time.sleep(1.0)

            _emit_progress("analyze", "分析仅电池供电期间的串口与 V/I…", 96)
            cfg = dict(get_settings())
            cfg["scenario_kind"] = "battery_only_serial"
            if _session.plug_event_times_ms:
                cfg["scenario_plug_times_ms"] = list(_session.plug_event_times_ms)
                cfg["scenario_plug_t_ms"] = _session.plug_event_times_ms[-1]
            elif _session.plug_event_t_ms is not None:
                cfg["scenario_plug_t_ms"] = _session.plug_event_t_ms
            serial_text = _session.serial_full
            if not serial_text.strip():
                socketio.emit(
                    "toast",
                    {
                        "level": "warn",
                        "msg": "串口日志为空：请确认 ESP32 日志口、波特率及数据线是否保持连通",
                    },
                )
            result = full_analyze(serial_text, _session.power_samples, cfg)
            analysis_dir = _session.log_run_dir or _session.vi_run_dir
            if analysis_dir:
                (analysis_dir / "analysis.json").write_text(
                    json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            socketio.emit("analysis_result", result.to_dict())
            verdict = "疑似软件/日志异常" if any(
                f.get("severity") == "critical" for f in result.to_dict().get("findings", [])
            ) else "未发现典型软件崩溃特征"
            if any(
                f.get("category") == "power" for f in result.to_dict().get("findings", [])
            ):
                verdict += "；供电侧有异常迹象，建议查硬件/PMIC"
            _emit_progress("done", verdict, 100)
            socketio.emit(
                "toast",
                {
                    "level": "ok",
                    "msg": f"仅电池供电场景完成（{repeat} 轮 × {battery_s}s）：{verdict}",
                },
            )
        finally:
            _session.scenario_running = False
            _broadcast_status()

    threading.Thread(target=worker, daemon=True).start()


def _run_hub_self_test(data: Optional[dict] = None) -> None:
    err = _prepare_shortcut(data, require_hub=True, refresh_serial=True)
    if err:
        socketio.emit("toast", {"level": "error", "msg": err})
        return

    def worker() -> None:
        try:
            socketio.emit("toast", {"level": "info", "msg": "Hub 自检：通道电源 断电→上电"})
            ok, msg = _do_hard_reboot(refresh_serial=False)
            if ok:
                v, i = _hub.read_sample()
                msg = f"{msg}；当前 V={v} mV I={i} mA"
            socketio.emit("toast", {"level": "ok" if ok else "error", "msg": msg})
        except Exception as exc:
            socketio.emit("toast", {"level": "error", "msg": str(exc)})
        _broadcast_status()

    threading.Thread(target=worker, daemon=True).start()


def _start_status_poller() -> None:
    global _status_thread_started
    if _status_thread_started:
        return
    _status_thread_started = True

    def loop() -> None:
        while True:
            try:
                _broadcast_status()
            except Exception:
                pass
            time.sleep(1.2)

    threading.Thread(target=loop, daemon=True).start()


@app.route("/")
def index() -> Any:
    return send_from_directory(WEB_DIR, "index.html")


GUIDE_PATH = ROOT / "docs" / "Web仪表盘使用指南.md"


@app.route("/api/guide")
def api_guide() -> Any:
    if not GUIDE_PATH.exists():
        return jsonify({"ok": False, "markdown": "", "error": "指南文件不存在"}), 404
    return jsonify(
        {
            "ok": True,
            "markdown": GUIDE_PATH.read_text(encoding="utf-8"),
            "updated": GUIDE_PATH.stat().st_mtime,
        }
    )


@app.route("/api/ports")
def api_ports() -> Any:
    ports = list_all_ports()
    return jsonify(
        {
            "ports": ports,
            "suggest": suggest_ports(ports),
            "hub_available": hub_available(ports),
        }
    )


@app.route("/api/settings", methods=["GET"])
def api_settings_get() -> Any:
    return jsonify(public_settings(get_settings()))


@app.route("/api/settings", methods=["POST"])
def api_settings_post() -> Any:
    body = request.get_json(silent=True) or {}
    merged = update_settings(body)
    hub_port = merged.get("hub_command_port") or ""
    esp_port = merged.get("esp32_serial_port") or ""
    msgs: list[str] = []

    hub_ch = int(merged.get("hub_dut_channel", 1))

    if body.get("connect_hub", True) and hub_available():
        if hub_port:
            if _hub.ensure_connected(hub_port):
                msgs.append(f"Hub 已连接（被测通道 {hub_ch}）")
            else:
                msgs.append(_hub.last_error or "Hub 未连接，请检查指令口")
        else:
            if _hub.ensure_connected(""):
                msgs.append(f"Hub 已连接（被测通道 {hub_ch}）")
            else:
                msgs.append("Hub 未连接（可留空指令口稍后重试）")

    serial_info: dict[str, Any] = {"ok": False, "msg": "未选择 ESP32 日志口"}
    with _session_lock:
        if esp_port:
            if _session.serial_open:
                _session.close_serial()
            serial_info = _session.open_serial()
        elif _session.serial_open:
            _session.close_serial()

    if esp_port:
        if serial_info.get("ok"):
            msgs.append(serial_info.get("msg", "串口已打开"))
        else:
            msgs.append(serial_info.get("msg", "串口打开失败"))

    _broadcast_status()
    return jsonify(
        {
            "ok": True,
            "settings": public_settings(merged),
            "hub_connected": _hub.hub is not None and _hub.link_ok,
            "serial_open": _session.serial_open,
            "message": "；".join(msgs) if msgs else "配置已保存",
        }
    )


@app.route("/api/reconnect", methods=["POST"])
def api_reconnect() -> Any:
    body = request.get_json(silent=True) or {}
    info = _reconnect_devices(
        require_hub=bool(body.get("require_hub", True)),
        refresh_serial=bool(body.get("refresh_serial", False)),
        settings_patch=body,
    )
    return jsonify(info)


@app.route("/api/hub/connect", methods=["POST"])
def api_hub_connect() -> Any:
    s = get_settings()
    port = (request.get_json(silent=True) or {}).get("port") or s.get("hub_command_port") or ""
    ok = _hub.ensure_connected(port)
    _broadcast_status()
    return jsonify({"ok": ok, "error": _hub.last_error, "info": _hub.info})


@app.route("/api/hub/disconnect", methods=["POST"])
def api_hub_disconnect() -> Any:
    _hub.disconnect()
    _broadcast_status()
    return jsonify({"ok": True})


def _power_from_request(body: dict) -> list[dict[str, Any]]:
    if body.get("power"):
        return list(body["power"])
    return list(_session.power_samples)


def _serial_from_request(body: dict) -> str:
    return str(body.get("serial") or _session.serial_full or "")


def _recording_t0() -> Optional[float]:
    return _session.vi_t0 if _session.vi_running and _session.vi_t0 else None


def _chart_t0(body: dict) -> Optional[float]:
    v = body.get("chart_t0")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _measures_from_request(body: dict) -> list[dict[str, Any]]:
    raw = body.get("measures")
    return list(raw) if raw else []


def _export_fields(body: dict) -> list[str]:
    raw = body.get("fields")
    if not raw:
        return ["v", "i"]
    return [str(x) for x in raw if str(x) in ("v", "i")]


def _persist_ai_from_body(body: dict) -> None:
    """分析/保存时把 Key、地址、模型写入 user_settings.json。"""
    patch: dict[str, Any] = {}
    key = (body.get("openai_api_key") or "").strip()
    if key:
        patch["openai_api_key"] = key
    if body.get("openai_base_url"):
        patch["openai_base_url"] = str(body["openai_base_url"]).strip()
    if body.get("openai_model"):
        patch["openai_model"] = str(body["openai_model"]).strip()
    if "ai_enabled" in body:
        patch["ai_enabled"] = body["ai_enabled"]
    if patch:
        update_settings(patch)


def _analysis_cfg_from_body(body: dict) -> dict[str, Any]:
    cfg = dict(get_settings())
    if body.get("openai_api_key"):
        cfg["openai_api_key"] = body["openai_api_key"]
    if "ai_enabled" in body:
        cfg["ai_enabled"] = body["ai_enabled"]
    for key in (
        "product_name",
        "product_brief",
        "user_observation",
        "hub_available",
        "hub_connected",
        "openai_base_url",
        "openai_model",
    ):
        if key in body:
            cfg[key] = body[key]
    if "hub_connected" not in cfg:
        cfg["hub_connected"] = _hub.hub is not None
    if "hub_available" not in cfg:
        cfg["hub_available"] = hub_available()
    return cfg


@app.route("/api/save/vi", methods=["POST"])
def api_save_vi() -> Any:
    body = request.get_json(silent=True) or {}
    samples = _power_from_request(body)
    measures = _measures_from_request(body)
    if not _has_voltage_data(samples, measures) and not _has_current_data(samples, measures):
        return jsonify({"ok": False, "msg": "暂无电压/电流数据可保存"}), 400
    result = save_vi_data(
        samples,
        measures,
        get_settings(),
        recording_t0=_recording_t0(),
        chart_t0=_chart_t0(body),
    )
    if not result.get("voltage") and not result.get("current"):
        return jsonify({"ok": False, "msg": "暂无电压/电流数据可保存"}), 400
    return jsonify(result)


@app.route("/api/save/power", methods=["POST"])
def api_save_power() -> Any:
    body = request.get_json(silent=True) or {}
    samples = _power_from_request(body)
    if not samples:
        return jsonify({"ok": False, "msg": "无电压/电流数据可保存"}), 400
    info = save_power_csv(
        samples,
        get_settings(),
        recording_t0=_recording_t0(),
        chart_t0=_chart_t0(body),
        fields=_export_fields(body),
    )
    return jsonify({"ok": True, **info})


@app.route("/api/save/measure", methods=["POST"])
def api_save_measure() -> Any:
    body = request.get_json(silent=True) or {}
    measures = _measures_from_request(body)
    if not measures:
        return jsonify({"ok": False, "msg": "无电流/电压测试记录可保存"}), 400
    info = save_measure_csv(measures, get_settings(), fields=_export_fields(body))
    return jsonify({"ok": True, **info})


@app.route("/api/save/serial", methods=["POST"])
def api_save_serial() -> Any:
    body = request.get_json(silent=True) or {}
    text = _serial_from_request(body)
    if not text.strip():
        return jsonify({"ok": False, "msg": "无串口日志可保存"}), 400
    info = save_serial_log(text, get_settings())
    return jsonify({"ok": True, **info})


@app.route("/api/save/report", methods=["POST"])
def api_save_report() -> Any:
    body = request.get_json(silent=True) or {}
    cfg = _analysis_cfg_from_body(body)

    analysis = body.get("analysis")
    if not analysis:
        serial_text = _serial_from_request(body)
        samples = _power_from_request(body)
        user_obs = (body.get("user_observation") or "").strip()
        if not serial_text.strip() and not samples and not user_obs:
            return jsonify({"ok": False, "msg": "请先执行智能分析，或填写现象描述并确保有日志/曲线数据"}), 400
        analysis = full_analyze(
            serial_text, samples, cfg, user_observation=user_obs
        ).to_dict()

    samples = _power_from_request(body)
    text = _serial_from_request(body)
    measures = _measures_from_request(body)
    result = save_diagnosis_report(
        analysis,
        samples,
        text,
        get_settings(),
        recording_t0=_recording_t0(),
        chart_t0=_chart_t0(body),
        measures=measures or None,
    )
    result["analysis"] = analysis
    return jsonify(result)


@app.route("/api/analyze", methods=["POST"])
def api_analyze() -> Any:
    body = request.get_json(silent=True) or {}
    _persist_ai_from_body(body)
    cfg = _analysis_cfg_from_body(body)
    serial_text = body.get("serial") or _session.serial_full
    samples = body.get("power") or _session.power_samples
    user_obs = (body.get("user_observation") or "").strip()
    result = full_analyze(serial_text, samples, cfg, user_observation=user_obs)
    return jsonify(result.to_dict())


@socketio.on("connect")
def on_connect() -> None:
    emit("device_status", build_device_status())
    emit("toast", {"level": "info", "msg": "已连接调试服务"})


@socketio.on("start_serial_capture")
def on_start_serial_capture() -> None:
    with _session_lock:
        info = _session.start_serial_capture()
    emit(
        "capture_state",
        {
            "serial_capturing": _session.serial_capturing,
            "vi_running": _session.vi_running,
            **info,
        },
    )
    emit("toast", {"level": "ok" if info.get("ok") else "error", "msg": info.get("msg", "")})


@socketio.on("stop_serial_capture")
def on_stop_serial_capture() -> None:
    with _session_lock:
        info = _session.stop_serial_capture()
    emit(
        "capture_state",
        {
            "serial_capturing": _session.serial_capturing,
            "vi_running": _session.vi_running,
            **info,
        },
    )
    msg = info.get("msg", "")
    if info.get("ok"):
        msg = f"日志已保存\n{info.get('dir', '')}"
    emit("toast", {"level": "ok" if info.get("ok") else "error", "msg": msg})


@socketio.on("hub_measure")
def on_hub_measure(data: Optional[dict] = None) -> None:
    err = _prepare_shortcut(data, require_hub=True, refresh_serial=False)
    if err:
        emit("toast", {"level": "error", "msg": err})
        return
    kind = (data or {}).get("kind") or "voltage"
    with _session_lock:
        info = _session.toggle_measure(kind)
    if not info.get("ok"):
        emit("toast", {"level": "error", "msg": info.get("msg", "操作失败")})
        return
    level = "ok" if info.get("v_sampling") or info.get("i_sampling") else "info"
    emit("toast", {"level": level, "msg": info.get("msg", "")})
    emit(
        "measure_state",
        {
            "v_sampling": info.get("v_sampling", _session.v_sampling),
            "i_sampling": info.get("i_sampling", _session.i_sampling),
            "reset_chart": info.get("dir") is not None and _session.vi_running,
        },
    )
    _broadcast_status()


@socketio.on("hub_vbus_only_off")
def on_hub_vbus_only_off(data: Optional[dict] = None) -> None:
    """先接通数据线，再断 VBUS，模拟纯数据线（设备改电池/USB 侧不供电）。"""
    err = _prepare_shortcut(data, require_hub=True, refresh_serial=False)
    if err:
        emit("toast", {"level": "error", "msg": err})
        return
    if not _hub_dataline(True):
        emit("toast", {"level": "error", "msg": "连接 USB 数据失败，请检查 Hub 通道"})
        return
    time.sleep(0.12)
    if not _hub_power(False):
        emit("toast", {"level": "error", "msg": "切断 VBUS 失败，请检查 Hub 通道"})
        return
    state = _read_hub_channel_state()
    emit(
        "toast",
        {
            "level": "ok",
            "msg": (
                f"已断 VBUS、保持数据线连通（{_format_hub_channel_state(state)}）。"
                "若串口仍消失，多为设备在 VBUS 掉落后主动断开 USB，需固件/硬件支持无 VBUS 维持枚举。"
            ),
        },
    )
    _broadcast_status()


@socketio.on("hub_dataline")
def on_hub_dataline(data: dict) -> None:
    connect = bool(data.get("on", True))
    refresh = bool(data.get("refresh_serial", False))
    err = _prepare_shortcut(data, require_hub=True, refresh_serial=refresh)
    if err:
        emit("toast", {"level": "error", "msg": err})
        return
    if _hub_dataline(connect):
        emit(
            "toast",
            {"level": "ok", "msg": "USB 数据已连接" if connect else "USB 数据已断开"},
        )
        if connect:

            def worker() -> None:
                _reconnect_serial_after_plug(quiet=False)

            threading.Thread(target=worker, daemon=True).start()
    else:
        emit("toast", {"level": "error", "msg": _hub.last_error or "操作失败"})
    _broadcast_status()


@socketio.on("hub_power")
def on_hub_power(data: dict) -> None:
    refresh = bool(data.get("refresh_serial", False))
    err = _prepare_shortcut(data, require_hub=True, refresh_serial=refresh)
    if err:
        emit("toast", {"level": "error", "msg": err})
        return
    on = bool(data.get("on", True))
    if _hub_power(on):
        emit("toast", {"level": "ok", "msg": "上电" if on else "断电"})
        if on:

            def worker() -> None:
                _reconnect_serial_after_plug(quiet=False)

            threading.Thread(target=worker, daemon=True).start()
    else:
        emit("toast", {"level": "error", "msg": _hub.last_error or "Hub 未连接"})
    _broadcast_status()


@socketio.on("hub_reboot")
def on_hub_reboot(data: Optional[dict] = None) -> None:
    err = _prepare_shortcut(data, require_hub=True, refresh_serial=True)
    if err:
        emit("toast", {"level": "error", "msg": err})
        return

    def worker() -> None:
        ok, msg = _do_hard_reboot(refresh_serial=True)
        socketio.emit("toast", {"level": "ok" if ok else "error", "msg": msg})
        _broadcast_status()

    emit("toast", {"level": "info", "msg": "硬重启执行中（断电→上电）…"})
    threading.Thread(target=worker, daemon=True).start()


@socketio.on("run_scenario")
def on_run_scenario(data: dict) -> None:
    name = (data or {}).get("name", "")
    if name == "usb_power_cycle":
        _run_usb_cycle_scenario(data)
    elif name == "battery_only_serial":
        _run_battery_only_scenario(data)
    elif name == "hub_self_test":
        _run_hub_self_test(data)
    else:
        emit("toast", {"level": "error", "msg": f"未知场景 {name}"})


def main() -> None:
    _start_status_poller()
    port = 8765
    try:
        from hub_common import load_config, cfg_get

        cfg = load_config()
        port = int(cfg_get(cfg, "dashboard", "port", default=8765))
    except FileNotFoundError:
        pass
    print(f"Power Lab: http://127.0.0.1:{port}")
    socketio.run(app, host="127.0.0.1", port=port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
