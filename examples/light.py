# Example: basic on/off, dimming, and color lights.
from matterbridge import (
    Bridge,
    DeviceConfigBuilder,
    EndpointPreset,
    device_type,
    cluster,
)
import os
import tempfile
import time
import threading

storage_path = os.environ.get(
    "BRIDGE_STORE",
    os.path.join(tempfile.gettempdir(), "matterbridge-light"),
)

bridge = Bridge(storage_path)

onoff_endpoint = EndpointPreset(device_type("on_off_light"))
onoff_endpoint.cluster_list([cluster("on_off")])
onoff_endpoint.node_label("light-onoff")

dimming_endpoint = EndpointPreset(device_type("dimmable_light"))
dimming_endpoint.cluster_list([cluster("on_off"), cluster("level_control")])
dimming_endpoint.node_label("light-dimming")

color_endpoint = EndpointPreset(device_type("extended_color_light"))
color_endpoint.cluster_list([cluster("on_off"), cluster("level_control"), cluster("color_control")])
color_endpoint.node_label("light-color")

CLUSTER_ON_OFF = cluster("on_off").id()
CLUSTER_LEVEL_CONTROL = cluster("level_control").id()

ATTR_ON_OFF = 0x0000
ATTR_CURRENT_LEVEL = 0x0000

cfg = DeviceConfigBuilder("light-suite")
cfg.vendor_name("3735943886")
cfg.product_name("Python Light Suite")
cfg.serial_number("PY-LIGHT-0001")
cfg.endpoint(onoff_endpoint)
cfg.endpoint(dimming_endpoint)
cfg.endpoint(color_endpoint)

dev = bridge.add_device(cfg.build())

endpoints = dev.endpoints()
onoff_ep = endpoints[0]
dimming_ep = endpoints[1]
color_ep = endpoints[2]

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
        if evt is not None:
            print(evt)


def update_loop():
    state = False
    while not stop_event.is_set():
        state = not state
        dev.set_attribute(onoff_ep, CLUSTER_ON_OFF, ATTR_ON_OFF, state)
        dev.set_attribute(dimming_ep, CLUSTER_ON_OFF, ATTR_ON_OFF, state)
        dev.set_attribute(color_ep, CLUSTER_ON_OFF, ATTR_ON_OFF, state)
        print(f"onoff={state}")
        time.sleep(30)


event_thread = threading.Thread(target=event_loop, daemon=True)
event_thread.start()

try:
    update_loop()
except KeyboardInterrupt:
    stop_event.set()
    event_thread.join()
