# Power Lab — ESP32-S3 与 SmartUSB Hub 调试工具

Power Lab 提供 **Web 仪表盘** 与 **命令行工具**，面向固件体验测试与 Bug 定位：可仅看串口日志 + 描述现象做 AI/规则分析，也可接入 SmartUSB Hub 做供电与插拔场景测试。

## 功能概览

| 方式 | 适用场景 |
|------|----------|
| Web 仪表盘（推荐） | 可视化监测、自动化插拔场景、规则/AI 分析、数据导出 |
| 命令行脚本 | 脚本化批处理、无图形界面环境 |

完整 Web 操作说明见 **[docs/Web仪表盘使用指南.md](docs/Web仪表盘使用指南.md)**；启动服务后亦可点击页面右上角 **「使用指南」** 查阅。

## 安装

```powershell
cd <项目目录>
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

PowerShell 下激活虚拟环境：`.\venv\Scripts\Activate.ps1`。若执行策略限制脚本运行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 启动 Web 仪表盘

```powershell
.\venv\Scripts\python.exe dashboard_server.py
```

浏览器访问：**http://127.0.0.1:8765**

请在服务启动后通过上述地址打开页面，不要直接打开本地 `web/index.html` 文件。

### Web 端主要能力

- 测试项目：产品背景本地保存；识别到 Hub 时自动启用电气与场景功能  
- 连接配置：**应用并连接** 打开串口（Hub 模式另连 Hub）；顶部状态条显示连接与采集情况  
- 实时电压/电流仪表与曲线（测电压/电流或场景时自动采集）  
- Hub 快捷指令与自动化测试场景  
- 串口实时显示；日志区可单独开始/停止录制并导出（含时间戳）  
- 规则检测与可选 AI 分析  

配置保存在本机 `user_settings.json`。

## 命令行工具（可选）

```powershell
.\venv\Scripts\python.exe esp32_hub_test.py serial-only
.\venv\Scripts\python.exe esp32_hub_test.py monitor
.\venv\Scripts\python.exe esp32_hub_test.py boot-log
.\venv\Scripts\python.exe esp32_hub_test.py reboot
```

| 子命令 | 说明 |
|--------|------|
| `serial-only` | 仅串口日志（无 Hub） |
| `monitor` | 电流采样 CSV + 串口 |
| `boot-log` | 断电上电后抓取启动日志 |
| `reboot` | 硬重启一轮 |

## 接线参考

```
PC ── SmartUSB Hub USB 上行口
PC ── SmartUSB Hub 指令控制口
Hub 被测通道 ── ESP32-S3
```

## 参考链接

- [SmartUSB Hub](https://github.com/mixedsignal-labs/smartusbhub)
