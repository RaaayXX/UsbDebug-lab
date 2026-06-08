"""根据产品类型与测试场景决定 Hub 插拔序列。"""



from __future__ import annotations



from typing import Any, Literal



# power: 仅 VBUS；dataline: 仅 D+/D-；both: 断电且断数据（最接近拔线）

# vbus_only: 仅断/上 VBUS，数据保持连通（仅电池供电但串口可见）

PlugMode = Literal["power", "dataline", "both", "vbus_only"]



_BATTERY_PRODUCTS = frozenset(

    {"battery", "with_battery", "battery_dataline", "battery_vbus", "dataline_only", "vbus_only", "battery_power"}

)

_USB_ONLY_PRODUCTS = frozenset({"usb_only", "usb_powered", "no_battery"})





def normalize_product_type(product_type: str | None) -> str:

    pt = (product_type or "").strip()

    if pt in _USB_ONLY_PRODUCTS:

        return "usb_only"

    if pt in _BATTERY_PRODUCTS or pt == "":

        return "battery"

    return pt





def product_has_battery(settings: dict[str, Any]) -> bool:

    pt = normalize_product_type(settings.get("product_type"))

    if pt == "battery":

        return True

    return bool(settings.get("device_has_battery"))





def active_scenario(settings: dict[str, Any]) -> str:

    scenario = (settings.get("active_scenario") or "").strip()

    if scenario in ("usb_power_cycle", "battery_only_serial"):

        if scenario == "battery_only_serial" and not product_has_battery(settings):

            return "usb_power_cycle"

        return scenario

    return "usb_power_cycle"





def plug_mode(settings: dict[str, Any]) -> PlugMode:

    if active_scenario(settings) == "battery_only_serial":

        return "vbus_only"



    pt = normalize_product_type(settings.get("product_type"))

    if pt == "battery" or settings.get("device_has_battery"):

        return "both"

    return "power"





def plug_mode_needs_serial_reconnect(mode: PlugMode) -> bool:

    """数据通路未断开时无需插拔后重连串口。"""

    return mode != "vbus_only"





def product_type_label(settings: dict[str, Any]) -> str:

    if active_scenario(settings) == "battery_only_serial":

        return "带电池产品（仅电池供电·串口保持）"

    m = plug_mode(settings)

    if m == "both":

        return "带电池产品"

    if m == "dataline":

        return "带电池产品（仅 USB 数据）"

    if m == "vbus_only":

        return "带电池产品（仅电池供电·串口保持）"

    return "不带电池产品"


