# Example: sensor suite emitting periodic measurements.
from matterbridge import (
    Bridge,
    DeviceConfigBuilder,
    EndpointPreset,
    device_type,
    cluster,
)
import math
import os
import tempfile

storage_path = os.environ.get(
    "BRIDGE_STORE",
    os.path.join(tempfile.gettempdir(), "matterbridge-sensors"),
)

bridge = Bridge(storage_path)

temp_endpoint = EndpointPreset(device_type("temperature_sensor"))
temp_endpoint.cluster_list([cluster("temperature_measurement")])
temp_endpoint.node_label("temp-1")

humidity_endpoint = EndpointPreset(device_type("humidity_sensor"))
humidity_endpoint.cluster_list([cluster("relative_humidity_measurement")])
humidity_endpoint.node_label("humidity-1")

pressure_endpoint = EndpointPreset(device_type("pressure_sensor"))
pressure_endpoint.cluster_list([cluster("pressure_measurement")])
pressure_endpoint.node_label("pressure-1")

light_endpoint = EndpointPreset(device_type("light_sensor"))
light_endpoint.cluster_list([cluster("illuminance_measurement")])
light_endpoint.node_label("light-1")

occupancy_endpoint = EndpointPreset(device_type("occupancy_sensor"))
occupancy_endpoint.cluster_list([cluster("occupancy_sensing")])
occupancy_endpoint.node_label("occupancy-1")

contact_endpoint = EndpointPreset(device_type("contact_sensor"))
contact_endpoint.cluster_list([cluster("boolean_state")])
contact_endpoint.node_label("contact-1")

CLUSTER_TEMPERATURE = cluster("temperature_measurement").id()
CLUSTER_HUMIDITY = cluster("relative_humidity_measurement").id()
CLUSTER_PRESSURE = cluster("pressure_measurement").id()
CLUSTER_ILLUMINANCE = cluster("illuminance_measurement").id()
CLUSTER_OCCUPANCY = cluster("occupancy_sensing").id()
CLUSTER_BOOLEAN_STATE = cluster("boolean_state").id()

ATTR_MEASURED_VALUE = 0x0000
ATTR_OCCUPANCY = 0x0000
ATTR_STATE_VALUE = 0x0000

cfg = DeviceConfigBuilder("sensor-suite")
cfg.vendor_name("3735943886")
cfg.product_name("Python Sensor Suite")
cfg.serial_number("PY-SENSOR-0001")
cfg.endpoint(temp_endpoint)
cfg.endpoint(humidity_endpoint)
cfg.endpoint(pressure_endpoint)
cfg.endpoint(light_endpoint)
cfg.endpoint(occupancy_endpoint)
cfg.endpoint(contact_endpoint)

dev = bridge.add_device(cfg.build())

endpoints = dev.endpoints()
temp_ep = endpoints[0]
humidity_ep = endpoints[1]
pressure_ep = endpoints[2]
light_ep = endpoints[3]
occupancy_ep = endpoints[4]
contact_ep = endpoints[5]

if not bridge.is_commissioned():
    info = bridge.open_commissioning_window_qr()
if info is not None:
    print(info["qr_code_text"])
    print(info["qr_code"])

events, _runner = bridge.start()

tick = 0.0
while True:
    temp = float(20.0 + 2.0 * math.sin(tick))
    humidity = int(4500 + 500 * math.sin(tick / 2))
    pressure = int(1013 + 10 * math.sin(tick / 3))
    lux = int(100 + 20 * math.sin(tick / 4))
    occupancy = int(tick) % 2 == 0
    contact_open = int(tick) % 2 == 0

    dev.set_attribute(temp_ep, CLUSTER_TEMPERATURE, ATTR_MEASURED_VALUE, temp)
    dev.set_attribute(humidity_ep, CLUSTER_HUMIDITY, ATTR_MEASURED_VALUE, humidity)
    dev.set_attribute(pressure_ep, CLUSTER_PRESSURE, ATTR_MEASURED_VALUE, pressure)
    dev.set_attribute(light_ep, CLUSTER_ILLUMINANCE, ATTR_MEASURED_VALUE, lux)
    dev.set_attribute(occupancy_ep, CLUSTER_OCCUPANCY, ATTR_OCCUPANCY, occupancy)
    dev.set_attribute(contact_ep, CLUSTER_BOOLEAN_STATE, ATTR_STATE_VALUE, contact_open)

    print(
        f"temp={temp:.2f} humidity={humidity} pressure={pressure} lux={lux} occupancy={occupancy} contact={contact_open}"
    )

    tick += 0.5

    evt = events.recv_timeout(10)
    if evt is not None:
        print(evt)
