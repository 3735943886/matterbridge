# matterbridge

Pythonic API for creating a Matter bridge, registering devices/endpoints, and processing events.

## Requirements
- Python 3.8+ (abi3)
- zeroconf runtime dependency (required for mDNS)

## Installation
```bash
pip install matterbridge
```

## Quick start
```python
from matterbridge import Bridge, DeviceConfigBuilder, EndpointPreset, device_type, cluster

bridge = Bridge("/tmp/matterbridge-py")

endpoint = EndpointPreset(device_type("on_off_light"))
endpoint.cluster_list([cluster("on_off")])
endpoint.node_label("light-1")

cfg = DeviceConfigBuilder("light-1")
cfg.vendor_name("Bridge")
cfg.product_name("On/Off Light")
cfg.serial_number("LIGHT-0001")
cfg.endpoint(endpoint)

dev = bridge.add_device(cfg.build())

if not bridge.is_commissioned():
    bridge.open_commissioning_window_qr()

events, _runner = bridge.start()

while True:
    evt = events.recv_timeout(100)
    if evt is not None:
        print(evt)
```

## Examples

Check out the [examples/](./examples/) directory for common usage scenarios:

- [light.py](./examples/light.py) - Basic On/Off, Dimmable, and Extended Color Light implementation.
- [plugpower.py](./examples/plugpower.py) - Smart Plug with electrical energy and power measurement.
- [sensors.py](./examples/sensors.py) - Multi-sensor suite (Temperature, Humidity, Pressure, Light, Occupancy, Contact).
- [fan.py](./examples/fan.py) - Fan Control with multi-speed support and mode sequencing.
- [thermostat.py](./examples/thermostat.py) - Thermostat with setpoints and local temperature updates.
- [curtain.py](./examples/curtain.py) - Window Covering (Curtain) control with position management.
- [doorlock.py](./examples/doorlock.py) - Door Lock with security command handling.

## Threading notes
- `EventStream` is a single-consumer stream; avoid reading from multiple threads at once.
- All other binding APIs are thread-safe; operations are marshaled to a single worker thread.
