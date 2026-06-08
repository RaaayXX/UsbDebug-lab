/** 产品类型：是否内置电池（决定可选测试场景） */

window.POWER_LAB_PRODUCT_TYPES = [

  {

    id: "battery",

    label: "带电池",

    hint: "设备内置电池，可脱离 USB 供电独立运行",

  },

  {

    id: "usb_only",

    label: "不带电池",

    hint: "仅 USB 供电，Hub 断电后设备停止",

  },

];



window.POWER_LAB_SCENARIOS = [

  {

    id: "usb_power_cycle",

    title: "USB 插拔 + 重启诊断",

    enabled: true,

    products: ["battery", "usb_only"],

    hint: "模拟 USB 线缆拔出与重新接入，同步观测 V/I 与串口复位日志",

    confirm:

      "将按设定次数重复 USB 插拔，并联合录制串口日志与 V/I 曲线。期间请勿烧录。是否继续？",

    help: {

      goal: "用 SmartUSB Hub 模拟 USB 线缆拔出与重新接入，同步观测 V/I 曲线与串口日志。",

      byProduct: [

        { type: "带电池", action: "拔线：断电+断数据 → 插入：上电+连数据" },

        { type: "不带电池", action: "断电 → 上电" },

      ],

      flow: [

        "自动连接 Hub/串口，开始 V/I 曲线与日志录制",

        "记录约 3s 插拔前基线",

        "按设定次数重复：模拟拔线 → 插入 → 重连串口 → 等待间隔",

        "全部完成后最后观察约 25s",

        "对每次插拔分别分析浪涌、VBUS 跌落、复位迹象，并汇总串口日志",

      ],

      note: "重复次数与插拔间隔可在下方参数区调节。",

    },

  },

  {

    id: "battery_only_serial",

    title: "仅电池供电 + 串口监测",

    enabled: true,

    products: ["battery"],

    hint: "切断 Hub VBUS、保持数据线连通，设备改电池供电且串口不断",

    confirm:

      "将切断 Hub VBUS（设备改由电池供电），USB 数据保持连通以便持续看串口。期间请勿烧录。是否继续？",

    help: {

      goal:

        "模拟拔掉 USB 供电线但保持数据线连接：设备仅靠电池运行，PC 端串口日志不中断，用于复现「仅电池供电卡死」等问题。",

      flow: [

        "自动连接 Hub/串口，确保 USB 数据已连通，开始 V/I 曲线与日志录制",

        "记录约 3s USB 供电基线",

        "切断 VBUS（设备改电池供电，串口保持）",

        "在仅电池模式下持续观测（建议 ≥ 60s）",

        "恢复 VBUS 供电",

        "可选重复多轮后，对串口与电流曲线做规则/AI 分析",

      ],

      note: "仅「带电池」产品可选此场景。重复轮数为切断/恢复 VBUS 次数；观测时长为每轮仅电池运行秒数。",

    },

  },

];



window.POWER_LAB_HUB_COMMANDS = [

  { cmd: "上电", group: "通道电源", desc: "打开 VBUS 5V" },

  { cmd: "断电", group: "通道电源", desc: "关闭 VBUS 5V" },

  { cmd: "连接数据", group: "USB 数据", desc: "接通 D+ / D-" },

  { cmd: "断开数据", group: "USB 数据", desc: "断开 D+ / D-" },

  {
    cmd: "断供电·保数据",
    group: "USB 数据",
    desc: "先连接数据，再断 VBUS；Hub 侧模拟纯数据线，设备改电池供电",
  },

  { cmd: "硬重启", group: "通道电源", desc: "断电→上电；独占 Hub 串口，完成后重连串口" },

  { cmd: "Hub 自检", group: "通道电源", desc: "断电 → 上电" },

  { cmd: "测电压", group: "采样", desc: "点击开始/停止 VBUS 曲线；自动连接 Hub/串口" },

  { cmd: "测电流", group: "采样", desc: "点击开始/停止电流曲线；自动连接 Hub/串口" },

];


