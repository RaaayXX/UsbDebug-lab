/** 产品类型与测试场景 */

window.POWER_LAB_PRODUCT_TYPES = [
  { id: "battery", label: "带电池" },
  { id: "usb_only", label: "不带电池" },
];

window.POWER_LAB_SCENARIOS = [
  {
    id: "usb_power_cycle",
    title: "USB 插拔 + 重启诊断",
    enabled: true,
    products: ["battery", "usb_only"],
    confirm: "运行「USB 插拔 + 重启诊断」？期间请勿烧录。",
    help: {
      byProduct: [
        { type: "带电池", action: "断电+断开数据 → 上电+连接数据" },
        { type: "不带电池", action: "断电 → 上电" },
      ],
    },
  },
  {
    id: "battery_only_serial",
    title: "仅电池供电 + 串口监测",
    enabled: true,
    products: ["battery"],
    confirm: "运行「仅电池供电 + 串口监测」？期间请勿烧录。",
    help: {
      flow: [
        "切断 VBUS，保持数据线",
        "仅电池运行（按设定时长）",
        "恢复 VBUS",
      ],
    },
  },
];

window.POWER_LAB_HUB_COMMANDS = [
  { cmd: "上电", group: "通道电源", desc: "打开 VBUS 5V" },
  { cmd: "断电", group: "通道电源", desc: "关闭 VBUS 5V" },
  { cmd: "连接数据", group: "USB 数据", desc: "接通 D+ / D-" },
  { cmd: "断开数据", group: "USB 数据", desc: "断开 D+ / D-" },
  { cmd: "硬重启", group: "通道电源", desc: "断电 → 上电" },
  { cmd: "Hub 自检", group: "通道电源", desc: "断电 → 上电" },
  { cmd: "测电压", group: "采样", desc: "VBUS 曲线采集" },
  { cmd: "测电流", group: "采样", desc: "电流曲线采集" },
];
