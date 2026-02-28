# Example: Smart Plug with Energy and Power Measurement
#
# This example demonstrates how to create a smart plug device that includes energy monitoring capabilities.
# It models a device with two endpoints:
# 1. Plug Endpoint: Standard on/off control for the plug.
# 2. Energy Endpoint: Exposes electrical measurements such as voltage, current, power, and cumulative energy.
#
# Key features demonstrated:
# - Using standard Matter device types (On/Off Plug-in Unit, Electrical Sensor).
# - Implementing measurement clusters (Electrical Power Measurement, Electrical Energy Measurement).
# - Updating complex attribute values (e.g., structures for energy measurement).
# - Simulating real-time power consumption data in a background loop.
# - Managing device state with thread safety using locks.

from matterbridge import (
    Bridge,
    DeviceConfigBuilder,
    EndpointPreset,
    DeviceTypeRef,
    device_type,
    cluster,
)
import os
import tempfile
import time
import threading

storage_path = os.environ.get(
    "BRIDGE_STORE",
    os.path.join(tempfile.gettempdir(), "matterbridge-plug"),
)

bridge = Bridge(storage_path)

plug_endpoint = EndpointPreset(DeviceTypeRef(0x010A, 1))
plug_endpoint.cluster_list(
    [cluster("on_off"), cluster("identify"), cluster("groups"), cluster("power_source")]
)
plug_endpoint.node_label("plug-1")

energy_endpoint = EndpointPreset(device_type("electrical_sensor"))
energy_endpoint.cluster_list(
    [
        cluster("identify"),
        cluster("electrical_energy_measurement"),
        cluster("electrical_power_measurement"),
        cluster("power_topology"),
        cluster("power_source"),
        cluster("groups"),
    ]
)
energy_endpoint.node_label("plug-1-energy")

CLUSTER_ON_OFF = cluster("on_off").id()
CLUSTER_ELECTRICAL_POWER = cluster("electrical_power_measurement").id()
CLUSTER_ELECTRICAL_ENERGY = cluster("electrical_energy_measurement").id()

ATTR_ON_OFF = 0x0000
ATTR_VOLTAGE = 0x0004
ATTR_ACTIVE_CURRENT = 0x0005
ATTR_ACTIVE_POWER = 0x0008
ATTR_CUMULATIVE_ENERGY_IMPORTED = 0x0001

cfg = DeviceConfigBuilder("plug-1")
cfg.vendor_name("3735943886")
cfg.product_name("Smart Plug")
cfg.serial_number("PLUG-1-0001")
cfg.endpoint(plug_endpoint)
cfg.endpoint(energy_endpoint)

dev = bridge.add_device(cfg.build())

endpoints = dev.endpoints()
plug_ep = endpoints[0]
energy_ep = endpoints[1]

state_lock = threading.Lock()
state = {
    "is_on": False,
    "voltage": 230.0,
    "current": 0.5,
    "power": 115.0,
    "energy_wh": 0.0,
    "seed": int(time.time_ns() & 0xFFFFFFFFFFFFFFFF),
}

dev.set_attribute(plug_ep, CLUSTER_ON_OFF, ATTR_ON_OFF, state["is_on"])
dev.set_attribute(energy_ep, CLUSTER_ELECTRICAL_POWER, ATTR_VOLTAGE, state["voltage"])
dev.set_attribute(energy_ep, CLUSTER_ELECTRICAL_POWER, ATTR_ACTIVE_CURRENT, state["current"])
dev.set_attribute(energy_ep, CLUSTER_ELECTRICAL_POWER, ATTR_ACTIVE_POWER, state["power"])
dev.set_attribute(
    energy_ep,
    CLUSTER_ELECTRICAL_ENERGY,
    ATTR_CUMULATIVE_ENERGY_IMPORTED,
    "electrical_energy_measurement",
    {"energy": 0},
)

if not bridge.is_commissioned():
    info = bridge.open_commissioning_window_qr()
if info is not None:
    print(info["qr_code_text"])
    print(info["qr_code"])

events, _runner = bridge.start()

stop_event = threading.Event()


def event_loop():
    while not stop_event.is_set():
        evt = events.recv_timeout(100)
        if evt is None:
            continue
        evt_type = evt.get("type")
        if evt_type == "on_off":
            print(
                f"event: ep={evt.get('endpoint_id')} on={evt.get('on')}"
            )
        elif evt_type == "attribute_updated":
            print(
                f"attr update: ep={evt.get('endpoint_id')} cluster={evt.get('cluster_id')} attr={evt.get('attribute_id')}"
            )
        else:
            print(evt)


def update_loop():
    next_on_off = time.monotonic()
    next_energy = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        if now >= next_on_off:
            with state_lock:
                state["is_on"] = not state["is_on"]
                is_on = state["is_on"]
            dev.update_attribute(plug_ep, CLUSTER_ON_OFF, ATTR_ON_OFF, is_on)
            print(f"local update: ep={plug_ep} on={is_on}")
            next_on_off = now + 10.0

        if now >= next_energy:
            with state_lock:
                seed = (state["seed"] * 6364136223846793005 + 1) & 0xFFFFFFFFFFFFFFFF
                state["seed"] = seed
                voltage = 210.0 + (seed % 30)
                current = 0.2 + ((seed >> 8) % 150) / 100.0
                power = voltage * current
                state["voltage"] = voltage
                state["current"] = current
                state["power"] = power
                state["energy_wh"] += power * (15.0 / 3600.0)
                energy_wh = state["energy_wh"]
            dev.update_attribute(energy_ep, CLUSTER_ELECTRICAL_POWER, ATTR_VOLTAGE, voltage)
            dev.update_attribute(
                energy_ep, CLUSTER_ELECTRICAL_POWER, ATTR_ACTIVE_CURRENT, current
            )
            dev.update_attribute(
                energy_ep, CLUSTER_ELECTRICAL_POWER, ATTR_ACTIVE_POWER, power
            )
            dev.update_attribute(
                energy_ep,
                CLUSTER_ELECTRICAL_ENERGY,
                ATTR_CUMULATIVE_ENERGY_IMPORTED,
                "electrical_energy_measurement",
                {"energy": int(energy_wh * 1000)},
            )
            print(f"energy update: ep={energy_ep} {energy_wh:.2f} Wh")
            next_energy = now + 15.0

        time.sleep(0.05)


event_thread = threading.Thread(target=event_loop, daemon=True)
event_thread.start()

try:
    update_loop()
except KeyboardInterrupt:
    stop_event.set()
    event_thread.join()
