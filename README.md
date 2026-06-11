# USBDebug Lab

本地 Web 调试平台：采集被测设备串口日志，可选接入 SmartUSB Hub 采集电气数据，通过规则引擎与 AI 生成诊断报告。

规则分析按芯片平台匹配日志语法（ESP32、SAMD21、RP2040 等）；串口监测与 Hub 电气测试适用于任意 USB 串口设备。

> **测试状态**：当前仅在 **ESP32-S3** 实机验证。其他芯片平台规则为通用匹配，未经完整测试。使用中若有问题，欢迎通过 [GitHub Issues](https://github.com/RaaayXX/UsbDebug-lab/issues) 反馈。

## 功能

| 方式 | 说明 |
|------|------|
| Web 仪表盘（推荐） | 串口监测、Hub 控制、测试场景、规则/AI 分析、数据导出 |
| 命令行 `esp32_hub_test.py` | 无图形界面下的串口日志与 Hub 采样 |

Web 操作详见 **[docs/Web仪表盘使用指南.md](docs/Web仪表盘使用指南.md)**；启动服务后亦可点击页面 **使用指南**。

## 安装

```powershell
cd <项目目录>
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 启动

```powershell
.\venv\Scripts\python.exe dashboard_server.py
```

浏览器访问 **http://127.0.0.1:8765**（须通过服务地址打开，不可直接打开 `web/index.html`）。

### Web 能力摘要

| 模块 | 无 Hub | 有 Hub |
|------|--------|--------|
| 设置、串口输出、智能分析 | ✓ | ✓ |
| 电压 · 电流、快捷指令、测试场景 | — | ✓ |

配置保存在本机 `user_settings.json`。

## 命令行（可选）

```powershell
.\venv\Scripts\python.exe esp32_hub_test.py serial-only
.\venv\Scripts\python.exe esp32_hub_test.py monitor
.\venv\Scripts\python.exe esp32_hub_test.py boot-log
.\venv\Scripts\python.exe esp32_hub_test.py reboot
```

| 子命令 | 说明 |
|--------|------|
| `serial-only` | 仅串口日志 |
| `monitor` | 电流采样 + 串口 |
| `boot-log` | 断电上电后抓取启动日志 |
| `reboot` | Hub 硬重启一轮 |

在 `config.yaml` 中启用 `flash.enabled` 时，`monitor` 可同步调用 esptool 烧录固件，**仅支持 Espressif ESP32**（默认 `esp32s3`）。其他芯片请使用各自官方烧录工具。

## Hub 接线

```
PC ── SmartUSB Hub USB 上行口
PC ── SmartUSB Hub 指令控制口（COM）
Hub 被测通道 ── 被测开发板
```

Hub 型号：指令口 VID `1A86` / PID `FE0C`。

## 参考

- [Web 仪表盘使用指南](docs/Web仪表盘使用指南.md)
- [SmartUSB Hub](https://github.com/mixedsignal-labs/smartusbhub)
- [问题反馈](https://github.com/RaaayXX/UsbDebug-lab/issues)
