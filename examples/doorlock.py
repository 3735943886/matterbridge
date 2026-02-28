# Example: Door Lock with Command Handling
#
# This example implements a Door Lock device, demonstrating how to handle security-related commands and states.
# It simulates a smart lock that can be locked, unlocked, and unbolted via Matter commands.
#
# Key features demonstrated:
# - Implementing the Door Lock device type and cluster.
# - Handling specific Door Lock commands: Lock Door, Unlock Door, Unbolt Door, Unlock with Timeout.
# - Managing Lock State (Locked, Unlocked, Unlatched) and Door State (Open, Closed).
# - Thread-safe state management for processing commands and updating attributes.
# - reflecting local state changes back to the Matter fabric (dev.update_attribute).
# - Simple event loop for processing incoming command events.

from matterbridge import Bridge, DeviceConfigBuilder, EndpointPreset, device_type, cluster
import os
import tempfile
import threading

storage_path = os.environ.get(
    "BRIDGE_STORE",
    os.path.join(tempfile.gettempdir(), "matterbridge-door-lock"),
)

bridge = Bridge(storage_path)

doorlock_endpoint = EndpointPreset(device_type("door_lock"))
doorlock_endpoint.cluster_list([cluster("door_lock"), cluster("identify"), cluster("groups")])
doorlock_endpoint.node_label("front-door")

CLUSTER_DOOR_LOCK = cluster("door_lock").id()

ATTR_LOCK_STATE = 0x0000
ATTR_DOOR_STATE = 0x0003
ATTR_OPERATING_MODE = 0x0025

CMD_LOCK_DOOR = 0x00
CMD_UNLOCK_DOOR = 0x01
CMD_UNBOLT_DOOR = 0x02
CMD_UNLOCK_WITH_TIMEOUT = 0x03

LOCK_STATE_LOCKED = 0x01
LOCK_STATE_UNLOCKED = 0x02
LOCK_STATE_UNLATCHED = 0x03

DOOR_STATE_OPEN = 0x00
DOOR_STATE_CLOSED = 0x01

OPERATING_MODE_NORMAL = 0x00

cfg = DeviceConfigBuilder("door-lock-1")
cfg.vendor_name("3735943886 Security")
cfg.product_name("Front Door Lock")
cfg.serial_number("DOORLOCK-1-0001")
cfg.endpoint(doorlock_endpoint)

dev = bridge.add_device(cfg.build())
endpoint_id = dev.endpoints()[0]

state_lock = threading.Lock()
state = {
    "lock_state": LOCK_STATE_LOCKED,
    "door_state": DOOR_STATE_CLOSED,
}

dev.set_attribute(endpoint_id, CLUSTER_DOOR_LOCK, ATTR_LOCK_STATE, state["lock_state"])
dev.set_attribute(endpoint_id, CLUSTER_DOOR_LOCK, ATTR_DOOR_STATE, state["door_state"])
dev.set_attribute(endpoint_id, CLUSTER_DOOR_LOCK, ATTR_OPERATING_MODE, OPERATING_MODE_NORMAL)

if not bridge.is_commissioned():
    info = bridge.open_commissioning_window_qr()
if info is not None:
    print(info["qr_code_text"])
    print(info["qr_code"])

events, _runner = bridge.start()

stop_event = threading.Event()


def apply_state():
    with state_lock:
        lock_state = state["lock_state"]
        door_state = state["door_state"]
    dev.update_attribute(endpoint_id, CLUSTER_DOOR_LOCK, ATTR_LOCK_STATE, lock_state)
    dev.update_attribute(endpoint_id, CLUSTER_DOOR_LOCK, ATTR_DOOR_STATE, door_state)
    print(
        f"local update: ep={endpoint_id} lock_state={lock_state} door_state={door_state}"
    )


def event_loop():
    while not stop_event.is_set():
        evt = events.recv_timeout(100)
        if evt is None:
            continue
        evt_type = evt.get("type")
        if evt_type == "command_received":
            cluster_id = evt.get("cluster_id")
            command_id = evt.get("command_id")
            if cluster_id == CLUSTER_DOOR_LOCK:
                with state_lock:
                    if command_id == CMD_LOCK_DOOR:
                        state["lock_state"] = LOCK_STATE_LOCKED
                        state["door_state"] = DOOR_STATE_CLOSED
                    elif command_id == CMD_UNLOCK_DOOR:
                        state["lock_state"] = LOCK_STATE_UNLOCKED
                        state["door_state"] = DOOR_STATE_CLOSED
                    elif command_id == CMD_UNLOCK_WITH_TIMEOUT:
                        state["lock_state"] = LOCK_STATE_UNLOCKED
                        state["door_state"] = DOOR_STATE_CLOSED
                    elif command_id == CMD_UNBOLT_DOOR:
                        state["lock_state"] = LOCK_STATE_UNLATCHED
                        state["door_state"] = DOOR_STATE_OPEN
                apply_state()
        elif evt_type == "attribute_updated":
            print(
                f"attr update: ep={evt.get('endpoint_id')} cluster={evt.get('cluster_id')} attr={evt.get('attribute_id')}"
            )
        else:
            print(evt)


event_thread = threading.Thread(target=event_loop, daemon=True)
event_thread.start()

try:
    event_thread.join()
except KeyboardInterrupt:
    stop_event.set()
    event_thread.join()
