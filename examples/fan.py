# Example: fan control with periodic state updates.
from matterbridge import Bridge, DeviceConfigBuilder, EndpointPreset, device_type, cluster
import os
import tempfile
import time
import threading

storage_path = os.environ.get(
    "BRIDGE_STORE",
    os.path.join(tempfile.gettempdir(), "matterbridge-fan"),
)

bridge = Bridge(storage_path)

fan_endpoint = EndpointPreset(device_type("fan"))
fan_endpoint.cluster_list([cluster("fan_control"), cluster("on_off")])
fan_endpoint.node_label("fan-1")

CLUSTER_FAN_CONTROL = cluster("fan_control").id()
CLUSTER_ON_OFF = cluster("on_off").id()

ATTR_FAN_MODE = 0x0000
ATTR_FAN_MODE_SEQUENCE = 0x0001
ATTR_PERCENT_SETTING = 0x0002
ATTR_PERCENT_CURRENT = 0x0003
ATTR_AIRFLOW_DIRECTION = 0x000B

ATTR_ON_OFF = 0x0000

CMD_STEP = 0x00

MODE_OFF = 0x00
MODE_LOW = 0x01
MODE_MED = 0x02
MODE_HIGH = 0x03
MODE_ON = 0x04
MODE_AUTO = 0x05
MODE_SMART = 0x06

SEQUENCE_OFF_LOW_MED_HIGH = 0x00
SEQUENCE_OFF_LOW_HIGH = 0x01
SEQUENCE_OFF_HIGH = 0x02
SEQUENCE_OFF_LOW_MED_HIGH_AUTO = 0x03
SEQUENCE_OFF_LOW_HIGH_AUTO = 0x04
SEQUENCE_OFF_HIGH_AUTO = 0x05

AIRFLOW_FORWARD = 0x00
AIRFLOW_REVERSE = 0x01

cfg = DeviceConfigBuilder("fan-1")
cfg.vendor_name("3735943886 HVAC")
cfg.product_name("Matter Fan")
cfg.serial_number("FAN-1-0001")
cfg.endpoint(fan_endpoint)

dev = bridge.add_device(cfg.build())
endpoint_id = dev.endpoints()[0]

FAN_ATTR_NAME = {
    ATTR_FAN_MODE: "fan_mode",
    ATTR_FAN_MODE_SEQUENCE: "fan_mode_sequence",
    ATTR_PERCENT_SETTING: "percent_setting",
    ATTR_PERCENT_CURRENT: "percent_current",
    ATTR_AIRFLOW_DIRECTION: "airflow_direction",
}

ON_OFF_COMMAND_NAME = {
    0x00: "off",
    0x01: "on",
    0x02: "toggle",
}

FAN_MODE_NAME = {
    MODE_OFF: "off",
    MODE_LOW: "low",
    MODE_MED: "med",
    MODE_HIGH: "high",
    MODE_ON: "on",
    MODE_AUTO: "auto",
    MODE_SMART: "smart",
}

AIRFLOW_NAME = {
    AIRFLOW_FORWARD: "forward",
    AIRFLOW_REVERSE: "reverse",
}

def auto_supported(sequence):
    return sequence in (
        SEQUENCE_OFF_LOW_MED_HIGH_AUTO,
        SEQUENCE_OFF_LOW_HIGH_AUTO,
        SEQUENCE_OFF_HIGH_AUTO,
    )


def speed_modes_for_sequence(sequence):
    if sequence in (SEQUENCE_OFF_LOW_MED_HIGH, SEQUENCE_OFF_LOW_MED_HIGH_AUTO):
        return [MODE_LOW, MODE_MED, MODE_HIGH]
    if sequence in (SEQUENCE_OFF_LOW_HIGH, SEQUENCE_OFF_LOW_HIGH_AUTO):
        return [MODE_LOW, MODE_HIGH]
    if sequence in (SEQUENCE_OFF_HIGH, SEQUENCE_OFF_HIGH_AUTO):
        return [MODE_HIGH]
    return [MODE_LOW, MODE_MED, MODE_HIGH]


def normalize_fan_mode(mode, sequence):
    current = mode
    if current == MODE_ON:
        current = MODE_HIGH
    if current == MODE_SMART:
        current = MODE_AUTO
    if current == MODE_AUTO and not auto_supported(sequence):
        current = MODE_HIGH
    return current


def percent_from_fan_mode(mode, sequence):
    mode = normalize_fan_mode(mode, sequence)
    if mode == MODE_OFF:
        return 0
    if mode == MODE_AUTO:
        return 100
    modes = speed_modes_for_sequence(sequence)
    if not modes:
        return 0
    idx = modes.index(mode) if mode in modes else 0
    return int((idx + 1) * 100 / len(modes))


def fan_mode_from_percent(percent, sequence):
    percent = min(100, max(0, percent))
    if percent == 0:
        return MODE_OFF
    modes = speed_modes_for_sequence(sequence)
    if not modes:
        return MODE_OFF
    length = len(modes)
    idx = int((percent * length + 99) / 100) - 1
    idx = max(0, min(length - 1, idx))
    return normalize_fan_mode(modes[idx], sequence)


def step_mode_by_sequence(current, direction, wrap, lowest_off, sequence):
    modes = [MODE_OFF] + speed_modes_for_sequence(sequence)
    if auto_supported(sequence):
        modes.append(MODE_AUTO)
    if not lowest_off:
        modes = [m for m in modes if m != MODE_OFF]
    if not modes:
        return current
    if current not in modes:
        return modes[0]
    idx = modes.index(current)
    if direction == 1:
        idx -= 1
    else:
        idx += 1
    if wrap:
        idx %= len(modes)
    else:
        idx = max(0, min(len(modes) - 1, idx))
    return modes[idx]


class VirtualFan:
    def __init__(self, sequence):
        self.sequence = sequence
        self.mode = MODE_OFF
        self.percent_setting = percent_from_fan_mode(self.mode, sequence)
        self.percent_current = self.percent_setting
        self.target_percent = self.percent_setting
        self.airflow_direction = AIRFLOW_FORWARD
        self.auto_phase = 0
        self.breeze_phase = 0
        self.last_non_off_mode = MODE_HIGH
        self.last_non_off_percent = percent_from_fan_mode(self.last_non_off_mode, sequence)

    def set_mode(self, mode):
        normalized = normalize_fan_mode(mode, self.sequence)
        self.mode = normalized
        percent = percent_from_fan_mode(normalized, self.sequence)
        self.percent_setting = percent
        self.target_percent = percent
        self.percent_current = percent
        if normalized != MODE_OFF:
            self.last_non_off_mode = normalized
            self.last_non_off_percent = percent

    def set_percent_setting(self, percent):
        self.percent_setting = min(100, max(0, percent))
        self.mode = fan_mode_from_percent(self.percent_setting, self.sequence)
        self.target_percent = self.percent_setting
        self.percent_current = self.percent_setting

    def set_on_off(self, on):
        if on:
            percent = self.last_non_off_percent or percent_from_fan_mode(MODE_HIGH, self.sequence)
            self.set_percent_setting(percent)
        else:
            self.mode = MODE_OFF
            self.percent_setting = 0
            self.target_percent = 0
            self.percent_current = 0

    def step(self, direction, wrap, lowest_off):
        next_mode = step_mode_by_sequence(self.mode, direction, wrap, lowest_off, self.sequence)
        self.set_mode(next_mode)

    def tick(self):
        if self.mode == MODE_AUTO:
            self.auto_phase = (self.auto_phase + 1) % 40
            offset = self.auto_phase - 10 if self.auto_phase < 20 else 30 - self.auto_phase
            self.target_percent = max(0, min(100, 90 + int(offset)))
        elif self.mode == MODE_SMART:
            self.breeze_phase = (self.breeze_phase + 1) % 30
            offset = self.breeze_phase - 7 if self.breeze_phase < 15 else 22 - self.breeze_phase
            self.target_percent = max(0, min(100, self.percent_setting + offset))
        else:
            self.target_percent = self.percent_setting
        self.percent_current = self.target_percent


fan_sequence = SEQUENCE_OFF_LOW_MED_HIGH_AUTO
fan = VirtualFan(fan_sequence)

dev.set_attribute(endpoint_id, CLUSTER_FAN_CONTROL, ATTR_FAN_MODE, fan.mode)
dev.set_attribute(endpoint_id, CLUSTER_FAN_CONTROL, ATTR_FAN_MODE_SEQUENCE, fan_sequence)
dev.set_attribute(endpoint_id, CLUSTER_FAN_CONTROL, ATTR_PERCENT_SETTING, fan.percent_setting)
dev.set_attribute(endpoint_id, CLUSTER_FAN_CONTROL, ATTR_PERCENT_CURRENT, fan.percent_current)
dev.set_attribute(endpoint_id, CLUSTER_FAN_CONTROL, ATTR_AIRFLOW_DIRECTION, fan.airflow_direction)
dev.set_attribute(endpoint_id, CLUSTER_ON_OFF, ATTR_ON_OFF, fan.mode != MODE_OFF)

if not bridge.is_commissioned():
    info = bridge.open_commissioning_window_qr()
if info is not None:
    print(info["qr_code_text"])
    print(info["qr_code"])

events, _runner = bridge.start()

stop_event = threading.Event()
state_lock = threading.Lock()
last_sent = {
    "mode": fan.mode,
    "setting": fan.percent_setting,
    "current": fan.percent_current,
    "direction": fan.airflow_direction,
}


def send_updates():
    changed = False
    if fan.mode != last_sent["mode"]:
        dev.update_attribute(endpoint_id, CLUSTER_FAN_CONTROL, ATTR_FAN_MODE, fan.mode)
        last_sent["mode"] = fan.mode
        changed = True
    if fan.percent_setting != last_sent["setting"]:
        dev.update_attribute(
            endpoint_id, CLUSTER_FAN_CONTROL, ATTR_PERCENT_SETTING, fan.percent_setting
        )
        last_sent["setting"] = fan.percent_setting
        changed = True
    if fan.percent_current != last_sent["current"]:
        dev.update_attribute(
            endpoint_id, CLUSTER_FAN_CONTROL, ATTR_PERCENT_CURRENT, fan.percent_current
        )
        last_sent["current"] = fan.percent_current
        changed = True
    if fan.airflow_direction != last_sent["direction"]:
        dev.update_attribute(
            endpoint_id, CLUSTER_FAN_CONTROL, ATTR_AIRFLOW_DIRECTION, fan.airflow_direction
        )
        last_sent["direction"] = fan.airflow_direction
        changed = True
    if changed:
        dev.update_attribute(endpoint_id, CLUSTER_ON_OFF, ATTR_ON_OFF, fan.mode != MODE_OFF)
    return changed


def event_loop():
    while not stop_event.is_set():
        evt = events.recv_timeout(100)
        if evt is None:
            continue
        evt_type = evt.get("type")
        if evt_type == "command_received":
            cluster_id = evt.get("cluster_id")
            command_id = evt.get("command_id")
            raw_payload = evt.get("payload")
            payload_hex = (
                raw_payload.hex() if isinstance(raw_payload, (bytes, bytearray)) else None
            )
            if cluster_id == CLUSTER_ON_OFF:
                command_name = ON_OFF_COMMAND_NAME.get(command_id, "unknown")
                print(
                    "on_off command: ep={endpoint} command={command} payload={payload}".format(
                        endpoint=evt.get("endpoint_id"),
                        command=command_name,
                        payload=payload_hex,
                    )
                )
            else:
                print(
                    "command: ep={endpoint} cluster={cluster} command={command} payload={payload}".format(
                        endpoint=evt.get("endpoint_id"),
                        cluster=cluster_id,
                        command=command_id,
                        payload=payload_hex,
                    )
                )
            if cluster_id == CLUSTER_FAN_CONTROL and command_id == CMD_STEP:
                with state_lock:
                    fan.step(0, True, False)
                    send_updates()
        elif evt_type == "attribute_updated":
            raw_value = evt.get("value")
            payload_hex = raw_value.hex() if isinstance(raw_value, (bytes, bytearray)) else None
            cluster_id = evt.get("cluster_id")
            attr_id = evt.get("attribute_id")
            attr_name = FAN_ATTR_NAME.get(attr_id, "attr")
            print(
                "attr: ep={endpoint} cluster={cluster} {name}({attr}) payload={payload}".format(
                    endpoint=evt.get("endpoint_id"),
                    cluster=cluster_id,
                    name=attr_name,
                    attr=attr_id,
                    payload=payload_hex,
                )
            )
        elif evt_type == "on_off":
            on = evt.get("on")
            print(f"on_off: ep={evt.get('endpoint_id')} on={on}")
            with state_lock:
                fan.set_on_off(bool(on))
                send_updates()
        elif evt_type == "level_control":
            level = evt.get("level")
            print(f"level_control: ep={evt.get('endpoint_id')} level={level}")
        else:
            print(f"event: {evt}")


def update_loop():
    next_tick = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        if now < next_tick:
            time.sleep(0.05)
            continue
        with state_lock:
            fan.tick()
            send_updates()
        next_tick = now + 0.2


event_thread = threading.Thread(target=event_loop, daemon=True)
event_thread.start()

try:
    update_loop()
except KeyboardInterrupt:
    stop_event.set()
    event_thread.join()
