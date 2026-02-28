# Example: Window Covering (Curtain) Control
#
# This example demonstrates how to implement a Window Covering device (e.g., blinds, curtains).
# It handles the Window Covering cluster which involves complex command processing for movement control.
#
# Key features demonstrated:
# - Implementing the Window Covering device type and cluster.
# - detailed TLV (Type-Length-Value) parsing for command payloads using a helper class.
# - Handling various movement commands: Up/Open, Down/Close, Stop, GoToLiftValue, GoToLiftPercentage.
# - Maintaining and updating position attributes (Lift, Tilt) and operational status.
# - Simulating the physical movement of the curtain over time in a separate thread.
# - Synchronization between command reception and state updates.

from matterbridge import Bridge, DeviceConfigBuilder, EndpointPreset, device_type, cluster
import os
import tempfile
import time
import threading

storage_path = os.environ.get(
    "BRIDGE_STORE",
    os.path.join(tempfile.gettempdir(), "matterbridge-curtain"),
)

bridge = Bridge(storage_path)

curtain_endpoint = EndpointPreset(device_type("window_covering"))
curtain_endpoint.cluster_list([cluster("window_covering"), cluster("identify"), cluster("groups")])
curtain_endpoint.node_label("curtain-1")

CLUSTER_WINDOW_COVERING = cluster("window_covering").id()

class CoverHelper:
    ATTR_TYPE = 0x0000
    ATTR_CONFIG_STATUS = 0x0007
    ATTR_CURRENT_POSITION_LIFT_PERCENT_100THS = 0x000E
    ATTR_CURRENT_POSITION_TILT_PERCENT_100THS = 0x000F
    ATTR_OPERATIONAL_STATUS = 0x000A
    ATTR_TARGET_POSITION_LIFT_PERCENT_100THS = 0x000B
    ATTR_TARGET_POSITION_TILT_PERCENT_100THS = 0x000C

    OP_STATUS_STOPPED = 0x00
    OP_STATUS_OPENING = 0x01
    OP_STATUS_CLOSING = 0x02

    CMD_UP_OR_OPEN = 0x00
    CMD_DOWN_OR_CLOSE = 0x01
    CMD_STOP_MOTION = 0x02
    CMD_GO_TO_LIFT_VALUE = 0x04
    CMD_GO_TO_LIFT_PERCENTAGE = 0x05
    CMD_GO_TO_TILT_VALUE = 0x07
    CMD_GO_TO_TILT_PERCENTAGE = 0x08

    @staticmethod
    def parse_tlv_u16(payload: bytes):
        i = 0
        depth = 0
        while i < len(payload):
            control = payload[i]
            i += 1
            tag_type = control >> 5
            value_type = control & 0x1F
            tag_size = [0, 1, 2, 4, 2, 4, 6, 8][tag_type]
            if i + tag_size > len(payload):
                return None
            i += tag_size
            if value_type == 24:
                if depth == 0:
                    continue
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
                    return None
                if value_type in (0, 1, 2, 3, 4, 5, 6, 7):
                    return int.from_bytes(
                        payload[i : i + size],
                        "little",
                        signed=value_type in (0, 1, 2, 3),
                    )
                i += size
                continue
            variable_sizes = {12: 1, 13: 2, 14: 4, 15: 8, 16: 1, 17: 2, 18: 4, 19: 8}
            if value_type in variable_sizes:
                len_size = variable_sizes[value_type]
                if i + len_size > len(payload):
                    return None
                length = int.from_bytes(payload[i : i + len_size], "little")
                i += len_size + length
                continue
            if value_type in (8, 9, 20):
                continue
            return None
        return None

open_position = 0
closed_position = 10_000

cfg = DeviceConfigBuilder("curtain-suite")
cfg.vendor_name("3735943886")
cfg.product_name("Python Curtain Suite")
cfg.serial_number("PY-CURTAIN-0001")
cfg.endpoint(curtain_endpoint)

dev = bridge.add_device(cfg.build())

endpoint_id = dev.endpoints()[0]

state_lock = threading.Lock()
state = {
    "lift_position": 0,
    "tilt_position": 0,
    "target_lift": 0,
    "target_tilt": 0,
}

dev.set_attribute(endpoint_id, CLUSTER_WINDOW_COVERING, CoverHelper.ATTR_TYPE, 0x08)
dev.set_attribute(endpoint_id, CLUSTER_WINDOW_COVERING, CoverHelper.ATTR_CONFIG_STATUS, 0x03)
dev.set_attribute(endpoint_id, CLUSTER_WINDOW_COVERING, CoverHelper.ATTR_CURRENT_POSITION_LIFT_PERCENT_100THS, open_position)
dev.set_attribute(endpoint_id, CLUSTER_WINDOW_COVERING, CoverHelper.ATTR_CURRENT_POSITION_TILT_PERCENT_100THS, open_position)
dev.set_attribute(
    endpoint_id,
    CLUSTER_WINDOW_COVERING,
    CoverHelper.ATTR_TARGET_POSITION_LIFT_PERCENT_100THS,
    state["target_lift"],
)
dev.set_attribute(
    endpoint_id,
    CLUSTER_WINDOW_COVERING,
    CoverHelper.ATTR_TARGET_POSITION_TILT_PERCENT_100THS,
    state["target_tilt"],
)
dev.set_attribute(endpoint_id, CLUSTER_WINDOW_COVERING, CoverHelper.ATTR_OPERATIONAL_STATUS, CoverHelper.OP_STATUS_STOPPED)

info = None
if not bridge.is_commissioned():
    info = bridge.open_commissioning_window_qr()
if info is not None:
    print(info["qr_code_text"])
    print(info["qr_code"])

events, _runner = bridge.start()

stop_event = threading.Event()

step = 2000
tick_interval = 1.0


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
            if cluster_id == CLUSTER_WINDOW_COVERING:
                with state_lock:
                    next_target_lift = state["target_lift"]
                    next_target_tilt = state["target_tilt"]
                if command_id == CoverHelper.CMD_UP_OR_OPEN:
                    next_target_lift = open_position
                    next_target_tilt = open_position
                elif command_id == CoverHelper.CMD_DOWN_OR_CLOSE:
                    next_target_lift = closed_position
                    next_target_tilt = closed_position
                elif command_id == CoverHelper.CMD_STOP_MOTION:
                    with state_lock:
                        next_target_lift = state["lift_position"]
                        next_target_tilt = state["tilt_position"]
                elif command_id in (
                    CoverHelper.CMD_GO_TO_LIFT_VALUE,
                    CoverHelper.CMD_GO_TO_LIFT_PERCENTAGE,
                ):
                    value = CoverHelper.parse_tlv_u16(payload)
                    if value is not None:
                        next_target_lift = max(open_position, min(closed_position, value))
                elif command_id in (
                    CoverHelper.CMD_GO_TO_TILT_VALUE,
                    CoverHelper.CMD_GO_TO_TILT_PERCENTAGE,
                ):
                    value = CoverHelper.parse_tlv_u16(payload)
                    if value is not None:
                        next_target_tilt = max(open_position, min(closed_position, value))
                with state_lock:
                    state["target_lift"] = next_target_lift
                    state["target_tilt"] = next_target_tilt
        elif evt_type == "attribute_updated":
            print(
                f"attr update: ep={evt.get('endpoint_id')} cluster={evt.get('cluster_id')} attr={evt.get('attribute_id')}"
            )
        elif evt_type == "switch":
            print(f"switch event: ep={evt.get('endpoint_id')} event={evt.get('event')}")
        else:
            print(evt)


def update_loop():
    next_tick = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        if now < next_tick:
            time.sleep(0.05)
            continue

        def move_towards(current, target):
            if current < target:
                return min(current + step, target)
            if current > target:
                return max(current - step, target)
            return current
        with state_lock:
            lift_position = move_towards(state["lift_position"], state["target_lift"])
            tilt_position = move_towards(state["tilt_position"], state["target_tilt"])
            state["lift_position"] = lift_position
            state["tilt_position"] = tilt_position
            current_lift = lift_position
            current_tilt = tilt_position
            next_target_lift = (
                current_lift if lift_position == state["target_lift"] else state["target_lift"]
            )
            next_target_tilt = (
                current_tilt if tilt_position == state["target_tilt"] else state["target_tilt"]
            )

        if lift_position != next_target_lift:
            operational_status = (
                CoverHelper.OP_STATUS_OPENING
                if lift_position > next_target_lift
                else CoverHelper.OP_STATUS_CLOSING
            )
        elif tilt_position != next_target_tilt:
            operational_status = (
                CoverHelper.OP_STATUS_OPENING
                if tilt_position > next_target_tilt
                else CoverHelper.OP_STATUS_CLOSING
            )
        else:
            operational_status = CoverHelper.OP_STATUS_STOPPED

        dev.update_attribute(
            endpoint_id,
            CLUSTER_WINDOW_COVERING,
            CoverHelper.ATTR_CURRENT_POSITION_LIFT_PERCENT_100THS,
            current_lift,
        )
        dev.update_attribute(
            endpoint_id,
            CLUSTER_WINDOW_COVERING,
            CoverHelper.ATTR_CURRENT_POSITION_TILT_PERCENT_100THS,
            current_tilt,
        )
        dev.update_attribute(
            endpoint_id,
            CLUSTER_WINDOW_COVERING,
            CoverHelper.ATTR_TARGET_POSITION_LIFT_PERCENT_100THS,
            next_target_lift,
        )
        dev.update_attribute(
            endpoint_id,
            CLUSTER_WINDOW_COVERING,
            CoverHelper.ATTR_TARGET_POSITION_TILT_PERCENT_100THS,
            next_target_tilt,
        )
        dev.update_attribute(
            endpoint_id,
            CLUSTER_WINDOW_COVERING,
            CoverHelper.ATTR_OPERATIONAL_STATUS,
            operational_status,
        )
        print(f"local update: ep={endpoint_id} lift={current_lift} tilt={current_tilt}")
        next_tick = now + tick_interval


event_thread = threading.Thread(target=event_loop, daemon=True)
event_thread.start()

try:
    update_loop()
except KeyboardInterrupt:
    stop_event.set()
    event_thread.join()
