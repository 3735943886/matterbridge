# Example: Thermostat with Setpoint and Temperature Simulation
#
# This example demonstrates how to implement a Thermostat device with basic HVAC behavior.
# It covers the Thermostat cluster, user interface configuration, and command processing.
#
# Key features demonstrated:
# - Creating a Thermostat endpoint with UI configuration cluster.
# - Setting initial local temperature, heating/cooling setpoints, and system mode.
# - Handling SetpointRaiseLower commands from the Matter fabric.
# - Periodically updating local temperature to simulate ambient changes.
#
from matterbridge import Bridge, DeviceConfigBuilder, EndpointPreset, device_type, cluster
import os
import tempfile
import time
import threading

storage_path = os.environ.get(
    "BRIDGE_STORE",
    os.path.join(tempfile.gettempdir(), "matterbridge-thermostat"),
)

bridge = Bridge(storage_path)

thermostat_endpoint = EndpointPreset(device_type("thermostat"))
thermostat_endpoint.cluster_list(
    [
        cluster("thermostat"),
        cluster("thermostat_user_interface_configuration"),
    ]
)
thermostat_endpoint.node_label("thermostat-1")

CLUSTER_THERMOSTAT = cluster("thermostat").id()
CLUSTER_THERMOSTAT_UI = cluster("thermostat_user_interface_configuration").id()

ATTR_LOCAL_TEMPERATURE = 0x0000
ATTR_OCCUPIED_COOLING_SETPOINT = 0x0011
ATTR_OCCUPIED_HEATING_SETPOINT = 0x0012
ATTR_CONTROL_SEQUENCE = 0x001B
ATTR_SYSTEM_MODE = 0x001C

ATTR_TEMPERATURE_DISPLAY_MODE = 0x0000
ATTR_KEYPAD_LOCKOUT = 0x0001

CMD_SETPOINT_RAISE_LOWER = 0x00

SYSTEM_MODE_OFF = 0x00
SYSTEM_MODE_AUTO = 0x01
SYSTEM_MODE_COOL = 0x03
SYSTEM_MODE_HEAT = 0x04

CONTROL_SEQUENCE_COOLING_AND_HEATING = 0x04

DISPLAY_MODE_CELSIUS = 0x00
KEYPAD_LOCKOUT_NONE = 0x00

cfg = DeviceConfigBuilder("thermostat-1")
cfg.vendor_name("3735943886 HVAC")
cfg.product_name("Python Thermostat")
cfg.serial_number("THERMO-1-0001")
cfg.endpoint(thermostat_endpoint)

dev = bridge.add_device(cfg.build())
endpoint_id = dev.endpoints()[0]

state_lock = threading.Lock()
state = {
    "local_temperature": 2250,
    "occupied_heating_setpoint": 2200,
    "occupied_cooling_setpoint": 2400,
}

dev.set_attribute(
    endpoint_id, CLUSTER_THERMOSTAT, ATTR_LOCAL_TEMPERATURE, state["local_temperature"]
)
dev.set_attribute(
    endpoint_id,
    CLUSTER_THERMOSTAT,
    ATTR_CONTROL_SEQUENCE,
    CONTROL_SEQUENCE_COOLING_AND_HEATING,
)
dev.set_attribute(
    endpoint_id, CLUSTER_THERMOSTAT, ATTR_SYSTEM_MODE, SYSTEM_MODE_HEAT
)
dev.set_attribute(
    endpoint_id,
    CLUSTER_THERMOSTAT,
    ATTR_OCCUPIED_HEATING_SETPOINT,
    state["occupied_heating_setpoint"],
)
dev.set_attribute(
    endpoint_id,
    CLUSTER_THERMOSTAT,
    ATTR_OCCUPIED_COOLING_SETPOINT,
    state["occupied_cooling_setpoint"],
)
dev.set_attribute(
    endpoint_id,
    CLUSTER_THERMOSTAT_UI,
    ATTR_TEMPERATURE_DISPLAY_MODE,
    DISPLAY_MODE_CELSIUS,
)
dev.set_attribute(
    endpoint_id, CLUSTER_THERMOSTAT_UI, ATTR_KEYPAD_LOCKOUT, KEYPAD_LOCKOUT_NONE
)

info = None
if not bridge.is_commissioned():
    info = bridge.open_commissioning_window_qr()
if info is not None:
    print(info["qr_code_text"])
    print(info["qr_code"])

events, _runner = bridge.start()

stop_event = threading.Event()


def parse_tlv_ints(payload: bytes, limit: int = 2):
    values = []
    i = 0
    depth = 0
    while i < len(payload) and len(values) < limit:
        control = payload[i]
        i += 1
        tag_type = control >> 5
        value_type = control & 0x1F
        tag_size = [0, 1, 2, 4, 2, 4, 6, 8][tag_type]
        if i + tag_size > len(payload):
            break
        i += tag_size
        if value_type == 24:
            if depth > 0:
                depth -= 1
            continue
        if value_type in (21, 22, 23):
            depth += 1
            continue
        fixed_sizes = {
            0: 1,
            1: 2,
            2: 4,
            3: 8,
            4: 1,
            5: 2,
            6: 4,
            7: 8,
            10: 4,
            11: 8,
        }
        if value_type in fixed_sizes:
            size = fixed_sizes[value_type]
            if i + size > len(payload):
                break
            signed = value_type in (0, 1, 2, 3)
            values.append(
                int.from_bytes(payload[i : i + size], "little", signed=signed)
            )
            i += size
            continue
        variable_sizes = {12: 1, 13: 2, 14: 4, 15: 8, 16: 1, 17: 2, 18: 4, 19: 8}
        if value_type in variable_sizes:
            len_size = variable_sizes[value_type]
            if i + len_size > len(payload):
                break
            length = int.from_bytes(payload[i : i + len_size], "little")
            i += len_size + length
            continue
        if value_type in (8, 9, 20):
            continue
        break
    return values


def apply_setpoint_raise_lower(mode: int, amount: int):
    delta = amount * 10
    with state_lock:
        if mode == 0:
            state["occupied_heating_setpoint"] += delta
        elif mode == 1:
            state["occupied_cooling_setpoint"] += delta
        elif mode == 2:
            state["occupied_heating_setpoint"] += delta
            state["occupied_cooling_setpoint"] += delta


def push_setpoints():
    with state_lock:
        heat = state["occupied_heating_setpoint"]
        cool = state["occupied_cooling_setpoint"]
    dev.update_attribute(
        endpoint_id, CLUSTER_THERMOSTAT, ATTR_OCCUPIED_HEATING_SETPOINT, heat
    )
    dev.update_attribute(
        endpoint_id, CLUSTER_THERMOSTAT, ATTR_OCCUPIED_COOLING_SETPOINT, cool
    )
    print(f"setpoints: heat={heat} cool={cool}")


def event_loop():
    while not stop_event.is_set():
        evt = events.recv_timeout(100)
        if evt is None:
            continue
        evt_type = evt.get("type")
        if evt_type == "command_received":
            cluster_id = evt.get("cluster_id")
            command_id = evt.get("command_id")
            payload = evt.get("payload") or b""
            if cluster_id == CLUSTER_THERMOSTAT and command_id == CMD_SETPOINT_RAISE_LOWER:
                values = parse_tlv_ints(payload, 2)
                if len(values) >= 2:
                    mode = values[0]
                    amount = values[1]
                    apply_setpoint_raise_lower(mode, amount)
                    push_setpoints()
                else:
                    print(
                        f"setpoint_raise_lower payload parse failed: payload={payload!r}"
                    )
        elif evt_type == "attribute_updated":
            print(
                f"attr update: ep={evt.get('endpoint_id')} cluster={evt.get('cluster_id')} attr={evt.get('attribute_id')}"
            )
        else:
            print(evt)


def update_loop():
    rising = True
    while not stop_event.is_set():
        with state_lock:
            if rising:
                state["local_temperature"] += 10
                if state["local_temperature"] >= 2350:
                    rising = False
            else:
                state["local_temperature"] -= 10
                if state["local_temperature"] <= 2150:
                    rising = True
            local_temp = state["local_temperature"]
        dev.update_attribute(
            endpoint_id, CLUSTER_THERMOSTAT, ATTR_LOCAL_TEMPERATURE, local_temp
        )
        print(f"local_temperature={local_temp}")
        time.sleep(5)


event_thread = threading.Thread(target=event_loop, daemon=True)
event_thread.start()

try:
    update_loop()
except KeyboardInterrupt:
    stop_event.set()
    event_thread.join()
