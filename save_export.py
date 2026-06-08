"""带时间戳导出电压/电流与串口日志。"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from hub_common import ROOT

_SERIAL_STAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:")


def stamp_serial_line(line: str, at: Optional[str] = None) -> str:
    """为单行串口日志加上接收时刻（ISO 8601）。"""
    if not line:
        return ""
    body = line.rstrip("\r\n")
    if not body:
        return "\n" if line.endswith("\n") else ""
    if _SERIAL_STAMP_RE.match(body.lstrip()):
        return body + ("\n" if line.endswith("\n") or line.endswith("\r\n") else "")
    wall = at or datetime.now().isoformat(timespec="milliseconds")
    suffix = "\n" if line.endswith("\n") or line.endswith("\r\n") else ""
    return f"[{wall}] {body.lstrip()}{suffix}"


def ensure_serial_timestamps(text: str, fallback_at: Optional[str] = None) -> str:
    """保存前为尚未带时间戳的行补打时间（旧缓冲数据）。"""
    if not text:
        return ""
    at = fallback_at or datetime.now().isoformat(timespec="milliseconds")
    parts: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            parts.append(line)
            continue
        if _SERIAL_STAMP_RE.match(line.lstrip()):
            parts.append(line)
        else:
            parts.append(stamp_serial_line(line + "\n", at).rstrip("\n"))
    trailing_nl = "\n" if text.endswith("\n") else ""
    return "\n".join(parts) + trailing_nl


def _logs_root() -> Path:
    return ROOT / "logs"


def _new_bundle_dir(tag: str) -> tuple[Path, str]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = _logs_root() / f"{stamp}_{tag}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder, stamp


def _meta(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "hub_command_port": settings.get("hub_command_port", ""),
        "hub_dut_channel": settings.get("hub_dut_channel", 1),
        "esp32_serial_port": settings.get("esp32_serial_port", ""),
        "esp32_baud": settings.get("esp32_baud", 115200),
        "product_type": settings.get("product_type", ""),
        "hub_available": settings.get("hub_available", False),
        "product_name": settings.get("product_name", ""),
        "product_brief": settings.get("product_brief", ""),
    }


def _wall_time_for_sample(
    s: dict[str, Any],
    saved_at: str,
    recording_t0: Optional[float] = None,
    chart_t0: Optional[float] = None,
) -> str:
    if s.get("at"):
        return str(s["at"])
    t_ms = s.get("t")
    if t_ms is None:
        return saved_at
    if recording_t0 is not None:
        return datetime.fromtimestamp(
            recording_t0 + int(t_ms) / 1000.0
        ).isoformat(timespec="milliseconds")
    if chart_t0 is not None:
        return datetime.fromtimestamp(
            chart_t0 + int(t_ms) / 1000.0
        ).isoformat(timespec="milliseconds")
    return saved_at


def _normalize_fields(fields: Optional[list[str]]) -> list[str]:
    if not fields:
        return ["v", "i"]
    out = [f for f in fields if f in ("v", "i")]
    return out or ["v", "i"]


def _filter_measures_by_fields(
    measures: list[dict[str, Any]], fields: list[str]
) -> list[dict[str, Any]]:
    kind_map = {"v": "voltage", "i": "current"}
    allowed = {kind_map[f] for f in fields}
    out: list[dict[str, Any]] = []
    for m in measures:
        kind = m.get("kind") or "both"
        if kind in allowed or kind == "both":
            out.append(m)
    return out


def save_power_csv(
    samples: list[dict[str, Any]],
    settings: dict[str, Any],
    bundle_dir: Optional[Path] = None,
    stamp: Optional[str] = None,
    recording_t0: Optional[float] = None,
    chart_t0: Optional[float] = None,
    fields: Optional[list[str]] = None,
) -> dict[str, Any]:
    meta = _meta(settings)
    flds = _normalize_fields(fields)
    if bundle_dir is None:
        bundle_dir, stamp = _new_bundle_dir("export")
    else:
        stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")

    path = bundle_dir / f"{stamp}_power.csv"
    header = ["wall_time", "t_ms"]
    if "v" in flds:
        header.append("v_mV")
    if "i" in flds:
        header.append("i_mA")
    header.append("snapshot")

    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(f"# saved_at={meta['saved_at']}\n")
        f.write(f"# hub_port={meta['hub_command_port']} channel={meta['hub_dut_channel']}\n")
        f.write(f"# export_fields={','.join(flds)}\n")
        w = csv.writer(f)
        w.writerow(header)
        for s in samples:
            t_ms = s.get("t")
            wall = _wall_time_for_sample(s, meta["saved_at"], recording_t0, chart_t0)
            row: list[Any] = [wall, t_ms if t_ms is not None else ""]
            if "v" in flds:
                row.append(s.get("v", ""))
            if "i" in flds:
                row.append(s.get("i", ""))
            row.append("1" if s.get("snapshot") else "")
            w.writerow(row)

    return {"path": str(path), "rows": len(samples), "saved_at": meta["saved_at"]}


def save_measure_csv(
    measures: list[dict[str, Any]],
    settings: dict[str, Any],
    bundle_dir: Optional[Path] = None,
    stamp: Optional[str] = None,
    fields: Optional[list[str]] = None,
) -> dict[str, Any]:
    meta = _meta(settings)
    flds = _normalize_fields(fields)
    measures = _filter_measures_by_fields(measures, flds)
    if bundle_dir is None:
        bundle_dir, stamp = _new_bundle_dir("export")
    else:
        stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")

    path = bundle_dir / f"{stamp}_measure.csv"
    header = ["wall_time", "kind", "channel"]
    if "v" in flds:
        header.append("v_mV")
    if "i" in flds:
        header.append("i_mA")

    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(f"# saved_at={meta['saved_at']}\n")
        f.write(f"# hub_port={meta['hub_command_port']} channel={meta['hub_dut_channel']}\n")
        f.write(f"# export_fields={','.join(flds)}\n")
        w = csv.writer(f)
        w.writerow(header)
        for m in measures:
            row: list[Any] = [
                m.get("at", meta["saved_at"]),
                m.get("kind", ""),
                m.get("channel", meta["hub_dut_channel"]),
            ]
            if "v" in flds:
                row.append(m.get("v_mV", m.get("v", "")))
            if "i" in flds:
                row.append(m.get("i_mA", m.get("i", "")))
            w.writerow(row)
    return {"path": str(path), "rows": len(measures), "saved_at": meta["saved_at"]}


def _measures_voltage(measures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in measures if (m.get("kind") or "") in ("voltage", "both")]


def _measures_current(measures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in measures if (m.get("kind") or "") in ("current", "both")]


def _has_voltage_data(samples: list[dict[str, Any]], measures: list[dict[str, Any]]) -> bool:
    if any(s.get("v") is not None for s in samples):
        return True
    return bool(_measures_voltage(measures))


def _has_current_data(samples: list[dict[str, Any]], measures: list[dict[str, Any]]) -> bool:
    if any(s.get("i") is not None for s in samples):
        return True
    return bool(_measures_current(measures))


def _write_voltage_csv(
    path: Path,
    meta: dict[str, Any],
    samples: list[dict[str, Any]],
    measures: list[dict[str, Any]],
    recording_t0: Optional[float],
    chart_t0: Optional[float],
) -> int:
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(f"# saved_at={meta['saved_at']}\n")
        f.write(f"# hub_port={meta['hub_command_port']} channel={meta['hub_dut_channel']}\n")
        w = csv.writer(f)
        w.writerow(["wall_time", "t_ms", "source", "v_mV"])
        for s in samples:
            if s.get("v") is None:
                continue
            t_ms = s.get("t")
            wall = _wall_time_for_sample(s, meta["saved_at"], recording_t0, chart_t0)
            w.writerow([wall, t_ms if t_ms is not None else "", "curve", s.get("v", "")])
            rows += 1
        for m in _measures_voltage(measures):
            w.writerow(
                [
                    m.get("at", meta["saved_at"]),
                    "",
                    "measure",
                    m.get("v_mV", m.get("v", "")),
                ]
            )
            rows += 1
    return rows


def _write_current_csv(
    path: Path,
    meta: dict[str, Any],
    samples: list[dict[str, Any]],
    measures: list[dict[str, Any]],
    recording_t0: Optional[float],
    chart_t0: Optional[float],
) -> int:
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(f"# saved_at={meta['saved_at']}\n")
        f.write(f"# hub_port={meta['hub_command_port']} channel={meta['hub_dut_channel']}\n")
        w = csv.writer(f)
        w.writerow(["wall_time", "t_ms", "source", "i_mA"])
        for s in samples:
            if s.get("i") is None:
                continue
            t_ms = s.get("t")
            wall = _wall_time_for_sample(s, meta["saved_at"], recording_t0, chart_t0)
            w.writerow([wall, t_ms if t_ms is not None else "", "curve", s.get("i", "")])
            rows += 1
        for m in _measures_current(measures):
            w.writerow(
                [
                    m.get("at", meta["saved_at"]),
                    "",
                    "measure",
                    m.get("i_mA", m.get("i", "")),
                ]
            )
            rows += 1
    return rows


def save_vi_data(
    samples: list[dict[str, Any]],
    measures: list[dict[str, Any]],
    settings: dict[str, Any],
    recording_t0: Optional[float] = None,
    chart_t0: Optional[float] = None,
) -> dict[str, Any]:
    """导出电压、电流各一份 CSV（同一目录）。"""
    bundle_dir, stamp = _new_bundle_dir("export")
    meta = _meta(settings)
    (bundle_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    voltage_info = None
    current_info = None

    if _has_voltage_data(samples, measures):
        v_path = bundle_dir / f"{stamp}_voltage.csv"
        v_rows = _write_voltage_csv(
            v_path, meta, samples, measures, recording_t0, chart_t0
        )
        voltage_info = {
            "path": str(v_path),
            "rows": v_rows,
            "saved_at": meta["saved_at"],
        }

    if _has_current_data(samples, measures):
        c_path = bundle_dir / f"{stamp}_current.csv"
        c_rows = _write_current_csv(
            c_path, meta, samples, measures, recording_t0, chart_t0
        )
        current_info = {
            "path": str(c_path),
            "rows": c_rows,
            "saved_at": meta["saved_at"],
        }

    return {
        "ok": True,
        "saved_at": meta["saved_at"],
        "directory": str(bundle_dir),
        "voltage": voltage_info,
        "current": current_info,
    }


def save_serial_log(
    text: str,
    settings: dict[str, Any],
    bundle_dir: Optional[Path] = None,
    stamp: Optional[str] = None,
) -> dict[str, Any]:
    meta = _meta(settings)
    if bundle_dir is None:
        bundle_dir, stamp = _new_bundle_dir("export")
    else:
        stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")

    path = bundle_dir / f"{stamp}_serial.log"
    body = ensure_serial_timestamps(text or "", meta["saved_at"])
    header = (
        f"# saved_at={meta['saved_at']}\n"
        f"# esp32_port={meta['esp32_serial_port']} baud={meta['esp32_baud']}\n"
        f"# product_type={meta['product_type']}\n"
        f"# line_format=[wall_time] log_line\n"
    )
    path.write_text(header + body, encoding="utf-8", errors="replace")
    return {
        "path": str(path),
        "chars": len(text or ""),
        "saved_at": meta["saved_at"],
    }


def save_bundle(
    samples: list[dict[str, Any]],
    serial_text: str,
    settings: dict[str, Any],
    recording_t0: Optional[float] = None,
    chart_t0: Optional[float] = None,
    measures: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    bundle_dir, stamp = _new_bundle_dir("export")
    meta = _meta(settings)
    (bundle_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    power = (
        save_power_csv(samples, settings, bundle_dir, stamp, recording_t0, chart_t0)
        if samples
        else None
    )
    serial = (
        save_serial_log(serial_text, settings, bundle_dir, stamp)
        if (serial_text or "").strip()
        else None
    )
    measure = (
        save_measure_csv(measures, settings, bundle_dir, stamp)
        if measures
        else None
    )
    return {
        "ok": True,
        "saved_at": meta["saved_at"],
        "directory": str(bundle_dir),
        "power": power,
        "serial": serial,
        "measure": measure,
    }


def _format_report_markdown(
    analysis: dict[str, Any],
    meta: dict[str, Any],
) -> str:
    lines = [
        "# Power Lab 诊断报告",
        "",
        f"**生成时间：** {meta['saved_at']}",
        f"**Hub 指令口：** {meta.get('hub_command_port') or '—'}  |  **被测通道：** {meta.get('hub_dut_channel', 1)}",
        f"**ESP32 日志口：** {meta.get('esp32_serial_port') or '—'}  |  **波特率：** {meta.get('esp32_baud', 115200)}",
        f"**产品类型：** {meta.get('product_type') or '—'}",
        "",
    ]
    pname = (meta.get("product_name") or "").strip()
    pbrief = (meta.get("product_brief") or "").strip()
    if pname or pbrief:
        lines.extend(["## 被测产品", ""])
        if pname:
            lines.append(f"**名称：** {pname}")
            lines.append("")
        if pbrief:
            lines.extend([pbrief, ""])
    user_obs = (analysis.get("user_observation") or "").strip()
    if user_obs:
        lines.extend(
            [
                "## 用户现场描述",
                "",
                user_obs,
                "",
            ]
        )
    lines.extend(
        [
            "## 诊断摘要",
            "",
            analysis.get("summary") or "（无摘要）",
            "",
        ]
    )
    stats = analysis.get("stats") or {}
    if stats:
        lines.extend(
            [
                "## 日志统计",
                "",
                f"- 日志行数：{stats.get('lines', 0)}",
                f"- 估算启动次数：{stats.get('boot_cycles_est', 0)}",
                f"- ERROR 行数：{stats.get('error_lines', 0)}",
                f"- WARN 行数：{stats.get('warn_lines', 0)}",
                "",
            ]
        )

    findings = analysis.get("findings") or []
    ai_struct = analysis.get("ai_structured")
    lines.append("## 异常发现")
    lines.append("")
    if ai_struct:
        lines.extend(
            [
                f"### AI 分析 · {ai_struct.get('title', '结论')}",
                "",
                f"- **发现来源：** {ai_struct.get('source', '—')}",
                f"- **可能原因：** {ai_struct.get('likely_cause', '—')}",
                f"- **建议排查：** {ai_struct.get('recommendation', '—')}",
            ]
        )
        if ai_struct.get("evidence"):
            lines.append(f"- **依据片段：** {ai_struct['evidence']}")
        lines.append("")
        if findings:
            lines.append("### 规则检出参考")
            lines.append("")
    elif not findings:
        lines.append("本次分析未检出规则定义的异常项。")
        lines.append("")
    if findings:
        for i, f in enumerate(findings, 1):
            sev = f.get("severity_label") or f.get("severity", "")
            lines.extend(
                [
                    f"### {i}. [{sev}] {f.get('message', '')}",
                    "",
                    f"- **发现来源：** {f.get('source', '—')}",
                    f"- **可能原因：** {f.get('likely_cause', '—')}",
                    f"- **建议排查：** {f.get('recommendation', '—')}",
                ]
            )
            if f.get("evidence"):
                lines.append(f"- **依据片段：** `{f['evidence']}`")
            lines.append("")

    if analysis.get("ai_text") and ai_struct:
        lines.extend(["## 完整 AI 回复", "", analysis["ai_text"], ""])
    elif analysis.get("ai_text"):
        lines.extend(["## AI 补充说明", "", analysis["ai_text"], ""])

    lines.extend(
        [
            "## 附件说明",
            "",
            "本目录同时包含原始数据，便于复核：",
            "",
            "| 文件 | 内容 |",
            "|------|------|",
            "| `*_power.csv` | 电压/电流曲线采样 |",
            "| `*_measure.csv` | 单次测电压/测电流记录（如有） |",
            "| `*_serial.log` | 完整串口日志 |",
            "| `analysis.json` | 结构化分析结果 |",
            "",
        ]
    )
    return "\n".join(lines)


def save_diagnosis_report(
    analysis: dict[str, Any],
    samples: list[dict[str, Any]],
    serial_text: str,
    settings: dict[str, Any],
    recording_t0: Optional[float] = None,
    chart_t0: Optional[float] = None,
    measures: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    bundle_dir, stamp = _new_bundle_dir("report")
    meta = _meta(settings)
    meta["report_generated_at"] = meta["saved_at"]

    report_path = bundle_dir / f"{stamp}_report.md"
    report_path.write_text(
        _format_report_markdown(analysis, meta),
        encoding="utf-8",
    )

    (bundle_dir / "analysis.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "analysis": analysis,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    power = (
        save_power_csv(samples, settings, bundle_dir, stamp, recording_t0, chart_t0)
        if samples
        else None
    )
    serial = (
        save_serial_log(serial_text, settings, bundle_dir, stamp)
        if (serial_text or "").strip()
        else None
    )
    measure = (
        save_measure_csv(measures, settings, bundle_dir, stamp)
        if measures
        else None
    )

    return {
        "ok": True,
        "saved_at": meta["saved_at"],
        "directory": str(bundle_dir),
        "report_path": str(report_path),
        "power": power,
        "serial": serial,
        "measure": measure,
    }
