"""Seeed XIAO 及常见开发板 — 串口识别与日志规则配置。"""

from __future__ import annotations

import re
from typing import Any, Optional

# (pattern, severity, category, message)
LogPattern = tuple[str, str, str, str]

DEVICE_PROFILES: dict[str, dict[str, str]] = {
    "auto": {
        "label": "自动识别",
        "hint": "根据 COM 口描述与日志内容推断芯片系列",
    },
    "xiao_esp32": {
        "label": "XIAO ESP32（S3 / C3 / C5 / C6）",
        "hint": "Espressif ESP-IDF / Arduino-ESP32 日志",
    },
    "xiao_samd21": {
        "label": "XIAO SAMD21",
        "hint": "Microchip SAMD21 · Arduino 框架",
    },
    "xiao_rp2040": {
        "label": "XIAO RP2040 / RP2350",
        "hint": "Raspberry Pi Pico SDK / MicroPython / Arduino-Pico",
    },
    "xiao_nrf52": {
        "label": "XIAO nRF52840 / nRF54L15",
        "hint": "Nordic nRF Connect SDK / Arduino-nRF52",
    },
    "xiao_ra4m1": {
        "label": "XIAO RA4M1",
        "hint": "Renesas RA4M1 · Arduino / FSP",
    },
    "xiao_mg24": {
        "label": "XIAO MG24",
        "hint": "Silicon Labs EFR32 · Matter / Zigbee / Thread",
    },
    "generic": {
        "label": "通用 MCU（其他芯片）",
        "hint": "仅匹配跨平台常见异常关键字",
    },
}

# 各系列与 XIAO 文档一致：https://wiki.seeedstudio.com/SeeedStudio_XIAO_Series_Introduction/
XIAO_CHIP_FAMILIES: dict[str, list[str]] = {
    "xiao_esp32": ["ESP32-S3", "ESP32-C3", "ESP32-C5", "ESP32-C6"],
    "xiao_samd21": ["SAMD21"],
    "xiao_rp2040": ["RP2040", "RP2350"],
    "xiao_nrf52": ["nRF52840", "nRF54L15"],
    "xiao_ra4m1": ["RA4M1"],
    "xiao_mg24": ["EFR32MG24"],
}

_COMMON_PATTERNS: list[LogPattern] = [
    (r"assert\s+failed", "critical", "assert", "断言失败"),
    (r"HardFault|UsageFault|MemManage|BusFault", "critical", "hardfault", "Cortex-M HardFault / 用法错误"),
    (r"\bFATAL\b|\bfatal error\b", "critical", "fatal", "致命错误"),
    (r"watchdog|Watchdog|WDOG", "critical", "wdt", "看门狗超时或复位"),
    (r"Brownout|BOD|欠压", "warning", "power", "欠压检测或供电不足"),
    (r"stack overflow|Stack overflow|STACK OVERFLOW", "critical", "stack", "栈溢出"),
    (r"Rebooting\.\.\.|System restart|Resetting", "warning", "reboot", "日志中出现重启提示"),
    (r"Backtrace|back trace|Call stack", "warning", "backtrace", "崩溃回溯信息"),
    (r"CORRUPT HEAP|heap corruption|Heap corrupt", "critical", "heap", "堆损坏"),
]

_ESP32_PATTERNS: list[LogPattern] = [
    (r"Guru Meditation Error", "critical", "panic", "芯片 Panic（Guru Meditation）"),
    (r"abort\(\) was called", "critical", "abort", "固件 abort() 主动崩溃"),
    (r"Brownout detector", "warning", "power", "欠压检测（供电不足）"),
    (r"task watchdog", "critical", "wdt", "任务看门狗超时"),
    (r"Interrupt wdt timeout", "critical", "wdt", "中断看门狗超时"),
    (r"LoadProhibited|StoreProhibited|InstrFetchProhibited", "critical", "memory", "非法内存访问"),
    (r"ESP_ERROR_CHECK failed", "critical", "esp_err", "ESP_ERROR_CHECK 失败"),
    (r"rst:0x[0-9a-fA-F]+", "info", "reset", "复位原因寄存器（rst:）"),
    (r"POWERON_RESET|SW_RESET|DEEPSLEEP_RESET|RTC_SW_CPU_RST", "info", "hw_reset", "硬件/软件复位原因"),
    (r"ets_main\.c", "info", "boot", "ROM 启动阶段"),
    (r"boot: ESP-IDF", "info", "boot", "IDF 二次启动"),
    (r"wifi:", "info", "wifi", "WiFi 子系统日志（检查是否异常刷屏）"),
    (r"E \(", "warning", "esp_log", "ERROR 级别日志行"),
    (
        r"network\s*issue|Network\s*(error|fail|down)|no\s*network|wifi.*(?:fail|disconnect|error)|连接.*失败|网络.*异常",
        "warning",
        "network",
        "日志中出现网络相关异常提示",
    ),
]

_SAMD21_PATTERNS: list[LogPattern] = [
    (r"Device started|Setup started|Setup finished", "info", "boot", "Arduino setup 阶段"),
    (r"SAM[BCD]|SAMD21", "info", "boot", "SAMD 芯片标识"),
    (r"Error opening|failed to init", "warning", "peripheral", "外设初始化失败"),
]

_RP2040_PATTERNS: list[LogPattern] = [
    (r"\*\*\* PANIC \*\*\*|panic at", "critical", "panic", "Pico SDK Panic"),
    (r"CPU:\s|Vectored IRQ|second_stage|boot2", "info", "boot", "RP2040/RP2350 启动阶段"),
    (r"MPY:|MicroPython", "info", "boot", "MicroPython 运行时"),
    (r"Watchdog fired|watchdog timeout", "critical", "wdt", "看门狗复位（Pico）"),
]

_NRF52_PATTERNS: list[LogPattern] = [
    (r"<error>|NRF_ERROR|APP_ERROR_CHECK", "critical", "nrf_err", "Nordic SDK 错误码"),
    (r"Reset reason|reset reason|RESETREAS", "info", "hw_reset", "Nordic 复位原因"),
    (r"---\|\d+", "info", "boot", "nRF 启动横幅"),
    (r"SoftDevice|BLE_GAP|NRF_LOG", "info", "ble", "BLE / SoftDevice 日志"),
]

_RA4M1_PATTERNS: list[LogPattern] = [
    (r"RA4M1|Renesas|FSP_ERR", "info", "boot", "Renesas RA / FSP 日志"),
    (r"FSP_ERR_\w+", "critical", "fsp_err", "FSP 驱动返回错误"),
]

_MG24_PATTERNS: list[LogPattern] = [
    (r"EFR32|MG24|Silicon Labs|SL_STATUS", "info", "boot", "Silicon Labs EFR32 日志"),
    (r"SL_STATUS_\w+|MATTER|Zigbee|OpenThread", "warning", "connectivity", "无线协议栈异常或状态"),
]

_PROFILE_PATTERNS: dict[str, list[LogPattern]] = {
    "xiao_esp32": _ESP32_PATTERNS,
    "xiao_samd21": _SAMD21_PATTERNS,
    "xiao_rp2040": _RP2040_PATTERNS,
    "xiao_nrf52": _NRF52_PATTERNS,
    "xiao_ra4m1": _RA4M1_PATTERNS,
    "xiao_mg24": _MG24_PATTERNS,
    "generic": [],
}

_PROFILE_BOOT_MARKERS: dict[str, tuple[str, ...]] = {
    "xiao_esp32": ("ESP-ROM:", "boot: ESP-IDF", "rst:0x", "entry 0x"),
    "xiao_samd21": ("Device started", "Setup started", "SAM", "Arduino"),
    "xiao_rp2040": ("CPU:", "Pico", "MPY:", "boot2", "Vectored IRQ"),
    "xiao_nrf52": ("---|", "Reset reason", "Starting", "NRF52840", "nRF54"),
    "xiao_ra4m1": ("RA4M1", "Renesas", "FSP", "Setup started"),
    "xiao_mg24": ("EFR32", "MG24", "Silicon Labs", "Gecko SDK"),
    "generic": ("boot", "reset", "start", "init"),
}

_PROFILE_LOG_ORIGIN: dict[str, str] = {
    "xiao_esp32": "ESP32 串口日志",
    "xiao_samd21": "SAMD21 串口日志",
    "xiao_rp2040": "RP2040/RP2350 串口日志",
    "xiao_nrf52": "nRF52 串口日志",
    "xiao_ra4m1": "RA4M1 串口日志",
    "xiao_mg24": "EFR32 MG24 串口日志",
    "generic": "设备串口日志",
}

# Seeed XIAO 常见 USB 描述 / VID
_SERIAL_PORT_HINTS = (
    "XIAO",
    "SEEED",
    "ESP32",
    "ESP",
    "SAMD21",
    "SAMD",
    "RP2040",
    "RP2350",
    "PICO",
    "NRF52",
    "NRF54",
    "RA4M1",
    "MG24",
    "EFR32",
    "USB JTAG",
    "SERIAL",
    "CH340",
    "CP210",
    "FTDI",
    "303A",  # Espressif
    "2886",  # Seeed Studio
    "2E8A",  # Raspberry Pi / RP2040
    "10C4",  # Silicon Labs CP210x
    "2341",  # Arduino
)

_PROFILE_DETECT_FROM_TEXT: list[tuple[str, str]] = [
    ("xiao_esp32", r"ESP-ROM:|boot: ESP-IDF|rst:0x|Guru Meditation|ESP_ERROR_CHECK"),
    ("xiao_rp2040", r"\*\*\* PANIC \*\*\*|CPU:\s|MPY:|Pico SDK"),
    ("xiao_nrf52", r"NRF_ERROR|APP_ERROR|Reset reason|---\|\d+|nRF52840|nRF54"),
    ("xiao_ra4m1", r"RA4M1|Renesas|FSP_ERR"),
    ("xiao_mg24", r"EFR32|MG24|SL_STATUS|Silicon Labs"),
    ("xiao_samd21", r"SAMD21|Device started|Setup finished"),
]

_PROFILE_DETECT_FROM_PORT: list[tuple[str, str]] = [
    ("xiao_esp32", r"ESP32|ESP\s|303A|JTAG"),
    ("xiao_samd21", r"SAMD21|SAMD|2886.*21"),
    ("xiao_rp2040", r"RP2040|RP2350|PICO|2E8A"),
    ("xiao_nrf52", r"NRF52840|NRF54|NRF52"),
    ("xiao_ra4m1", r"RA4M1|RENESAS"),
    ("xiao_mg24", r"MG24|EFR32"),
]


def public_device_profiles() -> list[dict[str, str]]:
    return [{"id": k, **v} for k, v in DEVICE_PROFILES.items()]


def profile_label(profile: str) -> str:
    return DEVICE_PROFILES.get(profile, DEVICE_PROFILES["generic"])["label"]


def normalize_device_profile(value: str | None) -> str:
    key = (value or "auto").strip()
    return key if key in DEVICE_PROFILES else "auto"


def is_likely_serial_device(port: Any, is_hub: bool) -> bool:
    if is_hub:
        return False
    text = f"{getattr(port, 'description', '')} {getattr(port, 'manufacturer', '') or ''} {getattr(port, 'hwid', '')}".upper()
    return any(h in text for h in _SERIAL_PORT_HINTS)


def suggest_device_profile_from_port(port: Any) -> Optional[str]:
    text = f"{getattr(port, 'description', '')} {getattr(port, 'manufacturer', '') or ''} {getattr(port, 'hwid', '')}".upper()
    for profile, pattern in _PROFILE_DETECT_FROM_PORT:
        if re.search(pattern, text, re.IGNORECASE):
            return profile
    return None


def detect_device_profile_from_text(text: str) -> Optional[str]:
    if not text.strip():
        return None
    for profile, pattern in _PROFILE_DETECT_FROM_TEXT:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            return profile
    return None


def resolve_device_profile(
    profile: str | None,
    serial_text: str = "",
    port_hint: str | None = None,
) -> str:
    normalized = normalize_device_profile(profile)
    if normalized != "auto":
        return normalized
    from_log = detect_device_profile_from_text(serial_text)
    if from_log:
        return from_log
    if port_hint:
        for prof, pattern in _PROFILE_DETECT_FROM_PORT:
            if re.search(pattern, port_hint, re.IGNORECASE):
                return prof
    return "generic"


def get_log_patterns(resolved_profile: str) -> list[LogPattern]:
    specific = list(_PROFILE_PATTERNS.get(resolved_profile, []))
    if resolved_profile == "auto":
        specific = list(_ESP32_PATTERNS)
    seen: set[str] = set()
    merged: list[LogPattern] = []
    for item in specific + _COMMON_PATTERNS:
        key = item[3]
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def get_boot_markers(resolved_profile: str) -> tuple[str, ...]:
    markers = _PROFILE_BOOT_MARKERS.get(resolved_profile)
    if markers:
        return markers
    return _PROFILE_BOOT_MARKERS["generic"]


def log_origin_label(resolved_profile: str) -> str:
    base = _PROFILE_LOG_ORIGIN.get(resolved_profile, _PROFILE_LOG_ORIGIN["generic"])
    return f"{base}（Web 串口输出区 / 场景录制）"


def count_boot_cycles(text: str, resolved_profile: str) -> int:
    markers = get_boot_markers(resolved_profile)
    n = 0
    for line in text.splitlines():
        s = line.strip()
        if any(m in s for m in markers):
            n += 1
    if resolved_profile == "xiao_esp32":
        return max(0, n // 2)
    return max(0, n // 2 if n >= 2 else n)
