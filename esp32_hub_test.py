#!/usr/bin/env python3
"""
ESP32-S3 + SmartUSB Hub 测试脚本

功能：
  1. Hub 控制被测口电源/数据，模拟插拔与硬重启
  2. 采样 VBUS 电压、电流，检测重启（电流跌落）
  3. 并行抓取 ESP32-S3 USB 串口日志，保存到文件

使用前：
  copy config.example.yaml config.yaml
  pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import serial
    import yaml
    from smartusbhub import SmartUSBHub
except ImportError as exc:
    print("缺少依赖，请先执行: pip install -r requirements.txt")
    print(f"  ({exc})")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"
EXAMPLE_CONFIG = Path(__file__).resolve().parent / "config.example.yaml"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        if EXAMPLE_CONFIG.exists():
            print(f"未找到 {path}，请复制 config.example.yaml 为 config.yaml")
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cfg_get(cfg: dict, *keys: str, default: Any = None) -> Any:
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


# ---------------------------------------------------------------------------
# 串口日志
# ---------------------------------------------------------------------------


class SerialLogger:
    def __init__(self, port: str, baud: int, out_path: Path) -> None:
        self.port = port
        self.baud = baud
        self.out_path = out_path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ser: Optional[serial.Serial] = None

    def start(self) -> None:
        if not self.port:
            print("[serial] 未配置 esp32.serial_port，跳过日志抓取")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 30.0
        while not self._stop.is_set() and time.time() < deadline:
            try:
                self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
                break
            except serial.SerialException as exc:
                print(f"[serial] 等待 {self.port} 枚举… ({exc})")
                time.sleep(0.5)
        else:
            print(f"[serial] 超时：无法打开 {self.port}")
            return

        print(f"[serial] 记录 → {self.out_path}")
        with self.out_path.open("w", encoding="utf-8", errors="replace") as fout:
            fout.write(
                f"# port={self.port} baud={self.baud} "
                f"started={datetime.now().isoformat()}\n"
            )
            while not self._stop.is_set():
                try:
                    chunk = self._ser.read(4096)
                except serial.SerialException as exc:
                    fout.write(f"\n# serial error: {exc}\n")
                    break
                if chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    fout.write(text)
                    fout.flush()
                    sys.stdout.write(text)
                    sys.stdout.flush()

        if self._ser and self._ser.is_open:
            self._ser.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Hub + 电流采样
# ---------------------------------------------------------------------------


@dataclass
class RebootEvent:
    t_ms: int
    i_ma: int
    v_mv: int


@dataclass
class PowerSampler:
    hub: SmartUSBHub
    channel: int
    interval_s: float
    threshold_ma: int
    min_gap_ms: int
    csv_path: Path
    events: list[RebootEvent] = field(default_factory=list)
    _last_reboot_ms: int = -10_000

    def sample_once(self) -> tuple[Optional[int], Optional[int]]:
        v = self.hub.get_channel_voltage(self.channel)
        i = self.hub.get_channel_current(self.channel)
        return v, i

    def _maybe_detect_reboot(self, t_ms: int, v: Optional[int], i: Optional[int]) -> None:
        if i is None:
            return
        if i <= self.threshold_ma and (t_ms - self._last_reboot_ms) >= self.min_gap_ms:
            self._last_reboot_ms = t_ms
            self.events.append(RebootEvent(t_ms=t_ms, i_ma=i, v_mv=v or 0))
            print(f"\n[reboot?] t={t_ms} ms  i={i} mA  v={v} mV")

    def run(self, duration_s: float, on_tick: Optional[Any] = None) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_ms", "v_mV", "i_mA"])
            while time.time() - t0 < duration_s:
                t_ms = int((time.time() - t0) * 1000)
                v, i = self.sample_once()
                w.writerow([t_ms, v if v is not None else "", i if i is not None else ""])
                self._maybe_detect_reboot(t_ms, v, i)
                if on_tick:
                    on_tick(t_ms, v, i)
                time.sleep(self.interval_s)


def connect_hub(cfg: dict) -> SmartUSBHub:
    port = cfg_get(cfg, "hub", "command_port", default="") or ""
    if port:
        print(f"[hub] 连接 {port}")
        return SmartUSBHub(port)
    print("[hub] 自动扫描…")
    hub = SmartUSBHub.scan_and_connect()
    if hub is None:
        raise RuntimeError("未找到 SmartUSB Hub，请检查指令控制口 USB 线")
    info = hub.get_device_info()
    print(f"[hub] {info}")
    return hub


def hub_power(hub: SmartUSBHub, channel: int, on: bool) -> None:
    state = 1 if on else 0
    ok = hub.set_channel_power(channel, state=state)
    label = "上电" if on else "断电"
    print(f"[hub] 口{channel} {label} → {'OK' if ok else 'FAIL'}")


def hub_dataline(hub: SmartUSBHub, channel: int, connect: bool) -> None:
    state = 1 if connect else 0
    ok = hub.set_channel_dataline(channel, state=state)
    label = "连通数据" if connect else "断开数据"
    print(f"[hub] 口{channel} {label} → {'OK' if ok else 'FAIL'}")


def hard_reboot(hub: SmartUSBHub, channel: int, off_s: float) -> None:
    hub_power(hub, channel, False)
    time.sleep(off_s)
    hub_power(hub, channel, True)


def run_esptool_flash(cfg: dict, serial_port: str) -> int:
    flash = cfg.get("flash") or {}
    if not flash.get("enabled"):
        return 0

    firmware = flash.get("firmware") or ""
    if not firmware:
        print("[flash] flash.enabled=true 但未设置 flash.firmware")
        return 1

    port = flash.get("port") or serial_port
    if not port:
        print("[flash] 未配置串口（esp32.serial_port 或 flash.port）")
        return 1

    extra = flash.get("extra_args") or []
    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        str(flash.get("chip", "esp32s3")),
        "--port",
        port,
        "--baud",
        str(flash.get("baud", 921600)),
    ]
    if isinstance(extra, list) and extra:
        cmd.extend(str(x) for x in extra)
    cmd.extend(["write_flash", "0x0", firmware])

    print("[flash]", " ".join(cmd))
    return subprocess.call(cmd)


# ---------------------------------------------------------------------------
# 测试场景
# ---------------------------------------------------------------------------


def make_run_dir(cfg: dict, tag: str) -> Path:
    base = Path(cfg_get(cfg, "output", "directory", default="./logs"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base / f"{stamp}_{tag}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def cmd_monitor(args: argparse.Namespace, cfg: dict) -> int:
    """仅采样电流/电压 + 串口日志（烧录时另开终端执行 idf.py flash 亦可）"""
    channel = int(cfg_get(cfg, "hub", "dut_channel", default=1))
    duration = float(cfg_get(cfg, "test", "log_duration_seconds", default=120))
    interval = float(cfg_get(cfg, "test", "sample_interval_ms", default=10)) / 1000.0

    run_dir = make_run_dir(cfg, "monitor")
    hub = connect_hub(cfg)
    serial_port = cfg_get(cfg, "esp32", "serial_port", default="") or ""
    baud = int(cfg_get(cfg, "esp32", "baud", default=115200))

    logger = SerialLogger(serial_port, baud, run_dir / "serial.log")
    sampler = PowerSampler(
        hub=hub,
        channel=channel,
        interval_s=interval,
        threshold_ma=int(cfg_get(cfg, "test", "reboot_current_threshold_ma", default=30)),
        min_gap_ms=int(cfg_get(cfg, "test", "reboot_min_gap_ms", default=200)),
        csv_path=run_dir / "power.csv",
    )

    print(f"[run] 输出目录: {run_dir}")
    print(f"[run] 采样 {duration}s，Hub 口{channel}，按 Ctrl+C 提前结束")

    logger.start()

    def flash_worker() -> None:
        code = run_esptool_flash(cfg, serial_port)
        if code != 0:
            print(f"[flash] 退出码 {code}")

    flash_thread: Optional[threading.Thread] = None
    if cfg_get(cfg, "flash", "enabled", default=False):
        flash_thread = threading.Thread(target=flash_worker, daemon=True)
        flash_thread.start()

    try:
        sampler.run(duration)
    except KeyboardInterrupt:
        print("\n[run] 用户中断")
    finally:
        logger.stop()
        hub.disconnect()

    if flash_thread:
        flash_thread.join(timeout=10.0)

    _write_summary(run_dir, sampler.events)
    return 0


def cmd_boot_log(args: argparse.Namespace, cfg: dict) -> int:
    """先断电 → 开好串口 → Hub 上电，抓完整启动日志"""
    channel = int(cfg_get(cfg, "hub", "dut_channel", default=1))
    off_s = float(cfg_get(cfg, "test", "power_off_seconds", default=0.5))
    duration = float(cfg_get(cfg, "test", "log_duration_seconds", default=60))
    interval = float(cfg_get(cfg, "test", "sample_interval_ms", default=10)) / 1000.0

    run_dir = make_run_dir(cfg, "boot_log")
    hub = connect_hub(cfg)
    serial_port = cfg_get(cfg, "esp32", "serial_port", default="") or ""
    baud = int(cfg_get(cfg, "esp32", "baud", default=115200))

    print(f"[run] 输出目录: {run_dir}")
    hub_power(hub, channel, False)
    time.sleep(off_s)

    logger = SerialLogger(serial_port, baud, run_dir / "serial.log")
    logger.start()
    time.sleep(0.3)

    print("[run] Hub 上电，开始记录…")
    hub_power(hub, channel, True)

    sampler = PowerSampler(
        hub=hub,
        channel=channel,
        interval_s=interval,
        threshold_ma=int(cfg_get(cfg, "test", "reboot_current_threshold_ma", default=30)),
        min_gap_ms=int(cfg_get(cfg, "test", "reboot_min_gap_ms", default=200)),
        csv_path=run_dir / "power.csv",
    )
    try:
        sampler.run(duration)
    except KeyboardInterrupt:
        print("\n[run] 用户中断")
    finally:
        logger.stop()
        hub.disconnect()

    _write_summary(run_dir, sampler.events)
    return 0


def cmd_reboot(args: argparse.Namespace, cfg: dict) -> int:
    """Hub 硬重启一轮 + 记录电流与串口"""
    channel = int(cfg_get(cfg, "hub", "dut_channel", default=1))
    off_s = float(cfg_get(cfg, "test", "power_off_seconds", default=0.5))
    duration = float(cfg_get(cfg, "test", "log_duration_seconds", default=45))
    interval = float(cfg_get(cfg, "test", "sample_interval_ms", default=10)) / 1000.0

    run_dir = make_run_dir(cfg, "hard_reboot")
    hub = connect_hub(cfg)
    serial_port = cfg_get(cfg, "esp32", "serial_port", default="") or ""
    baud = int(cfg_get(cfg, "esp32", "baud", default=115200))

    logger = SerialLogger(serial_port, baud, run_dir / "serial.log")
    sampler = PowerSampler(
        hub=hub,
        channel=channel,
        interval_s=interval,
        threshold_ma=int(cfg_get(cfg, "test", "reboot_current_threshold_ma", default=30)),
        min_gap_ms=int(cfg_get(cfg, "test", "reboot_min_gap_ms", default=200)),
        csv_path=run_dir / "power.csv",
    )

    print(f"[run] 输出目录: {run_dir}")
    logger.start()
    time.sleep(0.5)

    def do_reboot_at_2s(t_ms: int, _v: Any, _i: Any) -> None:
        if t_ms >= 2000 and not getattr(do_reboot_at_2s, "done", False):
            do_reboot_at_2s.done = True  # type: ignore[attr-defined]
            print("[run] 触发 Hub 硬重启…")
            hard_reboot(hub, channel, off_s)

    try:
        sampler.run(duration, on_tick=do_reboot_at_2s)
    except KeyboardInterrupt:
        print("\n[run] 用户中断")
    finally:
        logger.stop()
        hub.disconnect()

    _write_summary(run_dir, sampler.events)
    return 0


def cmd_hub_only(args: argparse.Namespace, cfg: dict) -> int:
    """Hub 未到货时：仅测串口日志（不依赖 smartusbhub 硬件）"""
    duration = float(cfg_get(cfg, "test", "log_duration_seconds", default=60))
    run_dir = make_run_dir(cfg, "serial_only")
    serial_port = cfg_get(cfg, "esp32", "serial_port", default="") or ""
    baud = int(cfg_get(cfg, "esp32", "baud", default=115200))

    if not serial_port:
        print("请在 config.yaml 填写 esp32.serial_port")
        return 1

    print(f"[run] 仅串口日志 → {run_dir / 'serial.log'}，时长 {duration}s")
    logger = SerialLogger(serial_port, baud, run_dir / "serial.log")
    logger.start()
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass
    finally:
        logger.stop()
    return 0


def _write_summary(run_dir: Path, events: list[RebootEvent]) -> None:
    summary = run_dir / "summary.txt"
    lines = [
        f"finished={datetime.now().isoformat()}",
        f"reboot_events={len(events)}",
    ]
    for n, ev in enumerate(events, 1):
        lines.append(f"  #{n}: t={ev.t_ms}ms i={ev.i_ma}mA v={ev.v_mv}mV")
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[run] 摘要 → {summary}")
    print(f"[run] 电流 → {run_dir / 'power.csv'}（若已启用 Hub）")
    print(f"[run] 串口 → {run_dir / 'serial.log'}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ESP32-S3 + SmartUSB Hub 重启/电流/串口日志测试"
    )
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="配置文件路径（默认 config.yaml）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "monitor",
        help="记录电流+串口；可选 config 里 flash.enabled 同步烧录",
    )
    sub.add_parser(
        "boot-log",
        help="Hub 断电 → 先开串口监听 → 上电，抓完整启动 log",
    )
    sub.add_parser("reboot", help="运行中 Hub 硬重启一次，观察电流与 log")
    sub.add_parser("serial-only", help="Hub 未到时仅抓串口（验证 COM 与波特率）")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        return 1

    handlers = {
        "monitor": cmd_monitor,
        "boot-log": cmd_boot_log,
        "reboot": cmd_reboot,
        "serial-only": cmd_hub_only,
    }
    return handlers[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
