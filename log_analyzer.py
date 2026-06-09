"""
串口日志 + 电流采样 — 规则检测与可选 AI 分析（支持 Seeed XIAO 多芯片系列）。
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from device_compat import (
    log_origin_label,
    profile_label,
    resolve_device_profile,
    get_log_patterns,
    get_boot_markers,
    count_boot_cycles,
    normalize_device_profile,
)
from hub_common import cfg_get


_SEVERITY_LABEL = {"critical": "严重", "warning": "警告", "info": "提示"}

_CATEGORY_META: dict[str, dict[str, str]] = {
    "panic": {
        "source": "串口日志关键字匹配",
        "likely_cause": "固件运行时异常（非法访问、栈溢出或未处理错误）触发芯片 Panic。",
        "recommendation": "根据日志中的 Backtrace 与任务名定位模块；检查近期代码变更与栈配置；保留完整 serial.log 复现。",
    },
    "abort": {
        "source": "串口日志关键字匹配",
        "likely_cause": "应用主动调用 abort() 或断言路径，通常为可预期的致命错误处理。",
        "recommendation": "向上追溯 abort 前的 ERROR 日志；对照固件版本与配置项。",
    },
    "stack": {
        "source": "串口日志关键字匹配",
        "likely_cause": "任务栈空间不足或递归过深。",
        "recommendation": "增大对应任务栈；减少局部大数组；用堆或分段处理大数据。",
    },
    "power": {
        "source": "电压/电流连续采样曲线",
        "likely_cause": "供电跌落、负载突变或设备复位导致 VBUS/电流异常。",
        "recommendation": "对照 power.csv 时间轴与串口 rst/boot；检查 Hub 供电、线材与峰值负载。",
    },
    "usb_inrush": {
        "source": "USB 插拔后约 8 秒电气窗口分析",
        "likely_cause": "接入瞬间浪涌电流过大，或下游负载同时上电。",
        "recommendation": "对比插拔前后曲线；评估软启动、限流及 Hub 辅助供电。",
    },
    "usb_vbus": {
        "source": "USB 插拔后约 8 秒电气窗口分析",
        "likely_cause": "VBUS 压降明显，Hub 带载能力不足、接触不良或线缆压降过大。",
        "recommendation": "换线/换口复测；确认 Hub 辅助供电；测量空载与满载电压。",
    },
    "usb_reboot": {
        "source": "USB 插拔后约 8 秒电气窗口分析",
        "likely_cause": "接入后设备掉电复位或 USB 重新枚举。",
        "recommendation": "对照串口 rst: 与 boot 次数；确认固件是否因 Brownout 或看门狗复位。",
    },
    "wdt": {
        "source": "串口日志关键字匹配",
        "likely_cause": "任务或中断长时间阻塞，未喂狗导致看门狗复位。",
        "recommendation": "定位阻塞任务；检查 WiFi/BLE/Flash 等耗时操作是否占用过久。",
    },
    "reboot": {
        "source": "串口日志启动痕迹统计",
        "likely_cause": "测试窗口内发生多次冷/热启动，可能为异常复位或反复插拔。",
        "recommendation": "统计 boot 时间间隔；与电流跌落、rst: 寄存器交叉验证。",
    },
    "hw_reset": {
        "source": "串口日志复位原因字段",
        "likely_cause": "硬件或 ROM 记录的复位源（上电、深度睡眠、软件复位等）。",
        "recommendation": "对照 ESP-IDF 复位原因说明；区分预期复位与异常复位。",
    },
    "serial": {
        "source": "串口日志",
        "likely_cause": "未采集到有效日志或设备未输出。",
        "recommendation": "确认设备日志 COM 口、波特率与固件日志输出通道（USB CDC / UART）。",
    },
    "ai": {
        "source": "分析服务",
        "likely_cause": "AI 接口未配置或请求失败。",
        "recommendation": "检查 API Key 与网络；可仅依据规则分析结果排查。",
    },
    "user_report": {
        "source": "测试人员现场描述",
        "likely_cause": "需与串口日志、电气曲线及规则检出项交叉验证。",
        "recommendation": "对照附件 serial.log、power.csv 的时间轴；复现现象并延长录制。",
    },
    "esp_log": {
        "source": "串口日志 ERROR 级别行",
        "likely_cause": "应用或驱动在运行中上报错误，可能与网络、外设或业务逻辑失败有关。",
        "recommendation": "在 serial.log 中搜索 ERROR 前后上下文；对照用户描述的现象发生时刻。",
    },
    "network": {
        "source": "串口日志关键字匹配",
        "likely_cause": "设备网络连接异常、DNS/路由失败或应用层判定无网络。",
        "recommendation": "确认 WiFi/以太网配置与信号；查看断连、重连、超时相关日志；复现时保持日志录制。",
    },
}

_DEFAULT_META = {
    "source": "规则引擎（串口日志或电气采样）",
    "likely_cause": "需结合异常描述、证据片段与附件数据综合判断。",
    "recommendation": "查阅附件 serial.log、power.csv；必要时延长录制并复测。",
}




def _log_line_at_match(text: str, pattern: str) -> tuple[int, str, str]:
    """返回 (行号, 整行原文, 匹配到的关键字片段)。"""
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if not m:
        return 0, "", ""
    line_no = text[: m.start()].count("\n") + 1
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end].strip()
    keyword = m.group(0).strip()
    return line_no, line[:800], keyword


def _log_line_containing(text: str, needle: str) -> tuple[int, str]:
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i, line.strip()[:800]
    return 0, ""


@dataclass
class Finding:
    severity: str  # critical | warning | info
    category: str
    message: str
    evidence: str = ""
    log_origin: str = ""
    log_location: str = ""
    log_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        meta = _CATEGORY_META.get(self.category, _DEFAULT_META)
        return {
            "severity": self.severity,
            "severity_label": _SEVERITY_LABEL.get(self.severity, self.severity),
            "category": self.category,
            "message": self.message,
            "evidence": self.evidence or self.log_excerpt,
            "source": meta["source"],
            "likely_cause": meta["likely_cause"],
            "recommendation": meta["recommendation"],
            "log_origin": self.log_origin,
            "log_location": self.log_location,
            "log_excerpt": self.log_excerpt,
        }


@dataclass
class AnalysisResult:
    summary: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    ai_used: bool = False
    ai_text: str = ""
    ai_structured: Optional[dict[str, str]] = None
    user_observation: str = ""
    ai_skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        findings_out = [f.to_dict() for f in self.findings]
        if self.ai_structured:
            findings_out = [
                f for f in findings_out if f.get("severity") in ("critical", "warning")
            ]
        out = {
            "summary": self.summary,
            "findings": findings_out,
            "stats": self.stats,
            "ai_used": self.ai_used,
            "ai_text": self.ai_text,
            "user_observation": self.user_observation,
        }
        if self.ai_structured:
            out["ai_structured"] = self.ai_structured
        if self.ai_skip_reason:
            out["ai_skip_reason"] = self.ai_skip_reason
        return out


_ERROR_LINE_PATTERNS = (
    r"\bE \(",
    r"\bERROR\b",
    r"\[ERROR\]",
    r"HardFault",
    r"FATAL",
    r"NRF_ERROR",
    r"FSP_ERR",
    r"SL_STATUS_\w+",
)


def analyze_power_samples(
    samples: list[dict[str, Any]],
    threshold_ma: int = 30,
    low_voltage_mv: int = 4500,
    hub_available: bool = True,
) -> list[Finding]:
    findings: list[Finding] = []
    if not samples:
        if hub_available:
            findings.append(
                Finding(
                    "info",
                    "power",
                    "暂无电压/电流曲线（可连接 Hub 或执行测电流/场景后重试）",
                )
            )
        return findings

    reboot_hits = 0
    prev_i: Optional[int] = None
    min_v = 99999
    max_i = 0

    for s in samples:
        i = s.get("i")
        v = s.get("v")
        if i is not None:
            max_i = max(max_i, int(i))
            if prev_i is not None and prev_i > 80 and int(i) <= threshold_ma:
                reboot_hits += 1
            prev_i = int(i)
        if v is not None:
            min_v = min(min_v, int(v))

    if reboot_hits > 0:
        findings.append(
            Finding(
                "warning",
                "power",
                f"检测到约 {reboot_hits} 次电流跌落（≤{threshold_ma} mA），疑似掉电/重启",
                evidence=f"采样点 {len(samples)}，峰值电流 {max_i} mA",
            )
        )
    if min_v < low_voltage_mv:
        findings.append(
            Finding(
                "warning",
                "power",
                f"VBUS 最低 {min_v} mV，低于 {low_voltage_mv} mV，检查供电/线损",
            )
        )
    if max_i > 3500:
        findings.append(
            Finding(
                "warning",
                "power",
                f"峰值电流 {max_i} mA 较高，确认 Hub 辅助供电与负载",
            )
        )
    return findings


def analyze_usb_reconnect(
    samples: list[dict[str, Any]],
    plug_t_ms: int,
    window_ms: int = 8000,
    low_voltage_mv: int = 4500,
    inrush_factor: float = 2.0,
    inrush_delta_ma: int = 150,
) -> list[Finding]:
    """插拔后窗口：USB 接入是否引起 V/I 异常、是否伴随掉电形态。"""
    findings: list[Finding] = []
    before = [
        s
        for s in samples
        if s.get("t") is not None
        and plug_t_ms - 3000 <= int(s["t"]) < plug_t_ms - 200
    ]
    after = [
        s
        for s in samples
        if s.get("t") is not None
        and plug_t_ms <= int(s["t"]) <= plug_t_ms + window_ms
    ]
    if not after:
        return findings

    def _vals(rows: list, key: str) -> list[int]:
        return [int(s[key]) for s in rows if s.get(key) is not None]

    base_i = _vals(before, "i")
    base_v = _vals(before, "v")
    aft_i = _vals(after, "i")
    aft_v = _vals(after, "v")

    base_i_med = sorted(base_i)[len(base_i) // 2] if base_i else 0
    if aft_i:
        peak_i = max(aft_i)
        if peak_i > max(base_i_med * inrush_factor, base_i_med + inrush_delta_ma) and peak_i > 80:
            findings.append(
                Finding(
                    "warning",
                    "usb_inrush",
                    f"USB 接入后电流尖峰 {peak_i} mA（插拔前约 {base_i_med} mA），排查上电浪涌/负载",
                    evidence=f"窗口 {plug_t_ms}~{plug_t_ms + window_ms} ms",
                )
            )
    if aft_v:
        min_v = min(aft_v)
        if min_v < low_voltage_mv:
            findings.append(
                Finding(
                    "warning",
                    "usb_vbus",
                    f"USB 接入后 VBUS 跌至 {min_v} mV，可能供电不足或接触不良",
                )
            )
        base_v_med = sorted(base_v)[len(base_v) // 2] if base_v else 5000
        if base_v and min_v < base_v_med - 300:
            findings.append(
                Finding(
                    "warning",
                    "usb_vbus",
                    f"接入后电压较插拔前下降 {base_v_med - min_v} mV，关注 Hub/线材负载",
                )
            )

    drops = 0
    prev = None
    for s in after:
        i = s.get("i")
        if i is None:
            continue
        if prev is not None and prev > 80 and int(i) <= 30:
            drops += 1
        prev = int(i)
    if drops > 0:
        findings.append(
            Finding(
                "warning",
                "usb_reboot",
                f"USB 接入后约 {drops} 次电流跌落，疑似硬件复位或再枚举",
                evidence="插拔后 8s 内采样",
            )
        )
    return findings


def analyze_serial_log(
    text: str,
    device_profile: str = "auto",
    port_hint: str = "",
) -> tuple[list[Finding], dict[str, Any]]:
    resolved = resolve_device_profile(device_profile, text, port_hint or None)
    log_origin = log_origin_label(resolved)
    patterns = get_log_patterns(resolved)
    findings: list[Finding] = []
    stats: dict[str, Any] = {
        "lines": len(text.splitlines()),
        "chars": len(text),
        "boot_cycles_est": count_boot_cycles(text, resolved),
        "error_lines": 0,
        "warn_lines": 0,
        "device_profile": device_profile or "auto",
        "device_profile_resolved": resolved,
        "device_profile_label": profile_label(resolved),
    }

    if not text.strip():
        findings.append(Finding("warning", "serial", "串口日志为空"))
        return findings, stats

    seen: set[str] = set()
    for pattern, severity, cat, msg in patterns:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            key = f"{cat}:{msg}"
            if key not in seen:
                seen.add(key)
                line_no, line_text, keyword = _log_line_at_match(text, pattern)
                location = f"第 {line_no} 行" if line_no else ""
                excerpt = line_text or keyword
                evidence = excerpt
                if line_no and keyword and keyword not in excerpt:
                    evidence = f"{excerpt}（匹配：{keyword}）"
                findings.append(
                    Finding(
                        severity,
                        cat,
                        msg,
                        evidence=evidence,
                        log_origin=log_origin,
                        log_location=location,
                        log_excerpt=line_text or keyword,
                    )
                )

    error_snippets: list[str] = []
    error_pattern = "|".join(f"(?:{p})" for p in _ERROR_LINE_PATTERNS)
    for line in text.splitlines():
        if re.search(error_pattern, line, re.IGNORECASE):
            stats["error_lines"] += 1
            if len(error_snippets) < 3:
                error_snippets.append(line.strip()[:240])
        if re.search(r"\bW \(", line) or " WARN " in line or "[WARN]" in line:
            stats["warn_lines"] += 1

    if error_snippets and stats["error_lines"] > 0:
        for f in findings:
            if f.category in ("esp_log", "fatal", "hardfault", "nrf_err", "fsp_err"):
                if not f.log_excerpt and error_snippets:
                    ln, line = _log_line_containing(text, error_snippets[0][:20])
                    if not line and error_snippets[0]:
                        line = error_snippets[0]
                    f.log_excerpt = line
                    f.evidence = line
                    f.log_origin = f.log_origin or log_origin
                    if ln:
                        f.log_location = f"第 {ln} 行"
                break
        else:
            ln, line = _log_line_containing(text, error_snippets[0][:20])
            findings.append(
                Finding(
                    "warning",
                    "esp_log",
                    f"日志中有 {stats['error_lines']} 行 ERROR / 异常",
                    evidence=line or " | ".join(error_snippets),
                    log_origin=log_origin,
                    log_location=f"第 {ln} 行" if ln else "",
                    log_excerpt=line or error_snippets[0],
                )
            )

    boots = stats["boot_cycles_est"]
    if boots >= 2:
        boot_markers = get_boot_markers(resolved) if resolved != "generic" else ("boot", "reset", "ESP-ROM:")
        ln, line = 0, ""
        for marker in boot_markers:
            ln, line = _log_line_containing(text, marker)
            if line:
                break
        findings.append(
            Finding(
                "warning",
                "reboot",
                f"日志中约 {boots} 次启动痕迹，可能存在多次重启",
                evidence=line or f"匹配 {profile_label(resolved)} 启动标记",
                log_origin=log_origin,
                log_location=f"第 {ln} 行" if ln else "多处启动标记",
                log_excerpt=line,
            )
        )

    return findings, stats


def _power_summary_text(power_samples: list[dict[str, Any]]) -> str:
    n = len(power_samples)
    ps = power_samples[-500:] if n > 500 else power_samples
    vmin = min((s["v"] for s in ps if s.get("v") is not None), default=None)
    vmax = max((s["v"] for s in ps if s.get("v") is not None), default=None)
    imin = min((s["i"] for s in ps if s.get("i") is not None), default=None)
    imax = max((s["i"] for s in ps if s.get("i") is not None), default=None)
    return (
        f"采样点 {n}；近期电压 {vmin}–{vmax} mV，电流 {imin}–{imax} mA"
        if n
        else "无电压/电流曲线数据"
    )


def _apply_user_observation(result: AnalysisResult, user_observation: str) -> None:
    text = (user_observation or "").strip()
    result.user_observation = text
    if not text:
        return
    preview = text if len(text) <= 120 else text[:117] + "…"
    result.findings.insert(
        0,
        Finding(
            "info",
            "user_report",
            f"用户描述：{preview}",
            evidence=text,
        ),
    )
    if result.summary:
        result.summary = f"已结合用户现场描述与采集数据。{result.summary}"
    else:
        result.summary = "已记录用户现场描述，请结合下方规则项与附件数据研判。"


def rule_based_analyze(
    serial_text: str,
    power_samples: list[dict[str, Any]],
    threshold_ma: int = 30,
    user_observation: str = "",
    hub_available: bool = True,
    device_profile: str = "auto",
    port_hint: str = "",
) -> AnalysisResult:
    log_findings, stats = analyze_serial_log(
        serial_text,
        device_profile=device_profile,
        port_hint=port_hint,
    )
    power_findings = analyze_power_samples(
        power_samples,
        threshold_ma=threshold_ma,
        hub_available=hub_available,
    )
    all_f = log_findings + power_findings

    critical = sum(1 for f in all_f if f.severity == "critical")
    warning = sum(1 for f in all_f if f.severity == "warning")

    if critical:
        summary = f"发现 {critical} 项严重问题、{warning} 项需关注，建议优先处理 Panic/看门狗/断言。"
    elif warning:
        summary = f"未发现芯片级崩溃关键字，但有 {warning} 项需关注（见下方条目）。"
    elif not serial_text.strip() and not power_samples:
        summary = "当前无足够日志或曲线数据；请填写现象描述并先录制/采集后再分析。"
    else:
        summary = "规则检测未发现明显异常；若仍有现象，可启用 AI 结合用户描述深度分析。"

    result = AnalysisResult(
        summary=summary,
        findings=all_f,
        stats=stats,
    )
    _apply_user_observation(result, user_observation)
    return result


def _product_context_block(cfg: dict[str, Any]) -> str:
    if cfg.get("hub_connected"):
        mode_label = "SmartUSB Hub 已连接（含电压电流与自动化场景）"
    elif cfg.get("hub_available"):
        mode_label = "已识别 SmartUSB Hub（可连接后使用电气与场景）"
    else:
        mode_label = "当前未识别 Hub，分析主要依据串口日志"
    name = (cfg.get("product_name") or "").strip()
    brief = (cfg.get("product_brief") or "").strip()
    profile = normalize_device_profile(cfg.get("device_profile"))
    resolved = cfg.get("device_profile_resolved") or profile
    if profile == "auto" and not cfg.get("device_profile_resolved"):
        resolved = resolve_device_profile(profile, cfg.get("serial_preview") or "", cfg.get("port_hint"))
    lines = [f"测试工具：{mode_label}"]
    lines.append(f"设备类型：{profile_label(resolved)}（配置：{profile_label(profile)}）")
    if name:
        lines.append(f"产品名称：{name}")
    lines.append(f"产品介绍：{brief if brief else '（未填写，请仅依据日志与现象分析）'}")
    return "\n".join(lines)


def _ssl_error_hints(exc: BaseException) -> str:
    msg = str(exc).lower()
    hints: list[str] = []
    if "ssl" in msg or "eof" in msg:
        hints.append("可换教程中的备用端点（api.chr6.com/v1 或 小忆.com/v1）")
        hints.append("确认 Chatbox 在同一网络下能「连接」成功")
        hints.append("若使用公司代理/VPN，请关闭或配置系统代理后重试")
    if "getaddrinfo" in msg or "failed to resolve" in msg or "name resolution" in msg:
        hints.append("域名无法解析：请核对是否为 api.chr1.com（数字1）而非 chrl（字母l）")
        hints.append("或改用教程备用端点 api.chr6.com/v1、https://小忆.com/v1")
    return "；".join(hints) if hints else ""


def _post_chat_completions(url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST OpenAI 兼容 chat/completions（优先 requests，SSL 更稳）。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "PowerLab/1.0",
    }
    body_bytes = json.dumps(payload).encode("utf-8")

    try:
        import requests

        resp = requests.post(
            url,
            headers=headers,
            data=body_bytes,
            timeout=90,
            verify=True,
        )
        if resp.status_code >= 400:
            text = (resp.text or "")[:500]
            raise urllib.error.HTTPError(
                url, resp.status_code, text, resp.headers, None
            )
        return resp.json()
    except ImportError:
        pass

    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=90, context=ctx) as resp:
        return json.loads(resp.read().decode())


def _parse_ai_structured(raw: str) -> dict[str, str]:
    """将 AI 回复解析为固定字段（JSON 优先，否则降级为全文）。"""
    text = (raw or "").strip()
    if not text:
        return {
            "title": "AI 分析结论",
            "source": "—",
            "likely_cause": "—",
            "recommendation": "—",
            "evidence": "",
        }
    candidate = text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return {
                "title": str(
                    obj.get("title") or obj.get("message") or obj.get("conclusion") or "AI 分析结论"
                ).strip(),
                "source": str(obj.get("source") or "AI 综合研判（日志·现象·产品背景）").strip(),
                "likely_cause": str(
                    obj.get("likely_cause") or obj.get("cause") or "—"
                ).strip(),
                "recommendation": str(
                    obj.get("recommendation") or obj.get("next_steps") or "—"
                ).strip(),
                "evidence": str(obj.get("evidence") or "").strip(),
            }
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return {
        "title": lines[0][:160] if lines else "AI 分析结论",
        "source": "AI 综合研判（日志·现象·产品背景）",
        "likely_cause": "—",
        "recommendation": text,
        "evidence": "",
    }


def openai_analyze(
    serial_text: str,
    power_summary: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: str = "https://api.openai.com/v1/chat/completions",
    user_observation: str = "",
    rule_summary: str = "",
    rule_findings: Optional[list[dict[str, Any]]] = None,
    product_context: str = "",
) -> str:
    """可选：调用 OpenAI 兼容 API 做补充分析。"""
    log_tail = serial_text[-12000:] if len(serial_text) > 12000 else serial_text
    user_block = (user_observation or "").strip() or "（未填写）"
    rules_block = rule_summary or "（无）"
    findings_lines: list[str] = []
    for f in rule_findings or []:
        if f.get("category") == "user_report":
            continue
        sev = f.get("severity_label") or f.get("severity", "")
        findings_lines.append(f"- [{sev}] {f.get('message', '')}")
    findings_text = "\n".join(findings_lines[:20]) if findings_lines else "（规则未检出项）"

    product_block = product_context or "（未提供产品背景）"
    prompt = f"""你是嵌入式硬件与固件测试专家，协助产品经理/测试人员定位 Bug。
请综合被测产品背景、用户现场描述、规则引擎结论、串口日志与（若有）电压电流数据，给出诊断结论。

【被测产品与工具】
{product_block}

【用户现场描述】
{user_block}

【规则引擎摘要】
{rules_block}

【规则检出项】
{findings_text}

【电压/电流采样摘要】
{power_summary}

【串口日志尾部】
```
{log_tail}
```

请**仅输出一个 JSON 对象**（不要 markdown 代码块外的其它文字），字段如下：
- title: 综合结论（一句话，说明是否支持用户描述的现象）
- source: 发现来源（依据哪些数据得出，如串口日志、用户描述、电气曲线等）
- likely_cause: 可能原因（结合产品背景与日志的具体推断，勿写空泛套话）
- recommendation: 建议排查（可操作的下一步，面向测试/产品人员）
- evidence: 依据片段（引用关键日志行或用户原话，可多行）
"""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是硬件调试助手。必须只输出合法 JSON 对象，字段为 title/source/likely_cause/recommendation/evidence，内容为中文。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    data = _post_chat_completions(base_url, api_key, payload)
    return data["choices"][0]["message"]["content"]


def _normalize_chat_completions_url(url: str) -> str:
    """将用户填写的根地址 /v1/ 规范为 chat/completions 完整路径。"""
    u = (url or "").strip().rstrip("/")
    if not u:
        return "https://api.openai.com/v1/chat/completions"
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return f"{u}/chat/completions"
    if "/v1/" in u:
        return f"{u}/chat/completions" if not u.endswith("/completions") else u
    return f"{u}/v1/chat/completions"


def _ai_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    nested = cfg.get("ai")
    if isinstance(nested, dict) and nested:
        return nested
    return {
        "enabled": cfg.get("ai_enabled", True),
        "openai_api_key": cfg.get("openai_api_key", ""),
        "model": cfg.get("openai_model", "gpt-4o-mini"),
        "base_url": _normalize_chat_completions_url(
            str(
                cfg.get("openai_base_url", "https://api.openai.com/v1/chat/completions")
            )
        ),
    }


def full_analyze(
    serial_text: str,
    power_samples: list[dict[str, Any]],
    cfg: dict[str, Any],
    user_observation: str = "",
) -> AnalysisResult:
    threshold = int(
        cfg.get("reboot_current_threshold_ma")
        or cfg_get(cfg, "test", "reboot_current_threshold_ma", default=30)
    )
    user_obs = (user_observation or cfg.get("user_observation") or "").strip()
    hub_ok = bool(cfg.get("hub_available"))
    device_profile = str(cfg.get("device_profile") or "auto")
    port_hint = str(cfg.get("port_hint") or cfg.get("esp32_serial_port") or "")
    result = rule_based_analyze(
        serial_text,
        power_samples,
        threshold_ma=threshold,
        user_observation=user_obs,
        hub_available=hub_ok,
        device_profile=device_profile,
        port_hint=port_hint,
    )

    plug_times = cfg.get("scenario_plug_times_ms")
    if plug_times and isinstance(plug_times, list):
        for idx, plug_t in enumerate(plug_times, 1):
            try:
                for finding in analyze_usb_reconnect(power_samples, int(plug_t)):
                    prefix = f"第{idx}次插拔" if len(plug_times) > 1 else ""
                    msg = finding.message
                    if prefix and not msg.startswith(prefix):
                        msg = f"{prefix}：{msg}"
                    result.findings.append(
                        Finding(
                            finding.severity,
                            finding.category,
                            msg,
                            finding.evidence,
                            log_origin=finding.log_origin,
                            log_location=finding.log_location,
                            log_excerpt=finding.log_excerpt,
                        )
                    )
            except (TypeError, ValueError):
                pass
    else:
        plug_t = cfg.get("scenario_plug_t_ms")
        if plug_t is not None:
            try:
                result.findings.extend(
                    analyze_usb_reconnect(power_samples, int(plug_t))
                )
            except (TypeError, ValueError):
                pass

    ai_cfg = _ai_cfg(cfg)
    if not ai_cfg.get("enabled"):
        return result

    key = (ai_cfg.get("openai_api_key") or "").strip()
    if not key:
        result.ai_skip_reason = (
            "未配置 API Key，本次仅完成规则分析。可在右侧「分析配置」中填写 OpenAI 兼容 Key 后重新分析。"
        )
        return result

    power_summary = _power_summary_text(power_samples)

    try:
        result.ai_text = openai_analyze(
            serial_text,
            power_summary,
            api_key=key,
            model=str(ai_cfg.get("model", "gpt-4o-mini")),
            base_url=str(
                ai_cfg.get("base_url", "https://api.openai.com/v1/chat/completions")
            ),
            user_observation=user_obs,
            rule_summary=result.summary,
            rule_findings=[f.to_dict() for f in result.findings],
            product_context=_product_context_block(cfg),
        )
        result.ai_structured = _parse_ai_structured(result.ai_text)
        result.ai_used = True
        result.summary += " （已附加 AI 分析）"
    except Exception as exc:
        detail = str(exc)
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                detail = f"{resp.status_code} {(resp.text or '')[:400]}"
            except Exception:
                pass
        elif isinstance(exc, urllib.error.HTTPError):
            try:
                body = exc.read().decode("utf-8", errors="replace")[:400]
                if body:
                    detail = f"{exc.code} {body}"
            except OSError:
                detail = f"{exc.code} {getattr(exc, 'reason', exc)}"
        hints = _ssl_error_hints(exc)
        result.ai_skip_reason = f"AI 请求失败：{detail}" + (f"。{hints}" if hints else "")
    return result
