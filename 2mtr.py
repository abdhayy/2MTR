 #!/usr/bin/env python3
import RPi.GPIO as GPIO
import time
import threading
from datetime import datetime
import json
import paho.mqtt.client as mqtt

# Constants
PULSES_PER_REVOLUTION = 200
WHEEL_CIRCUMFERENCE = 0.2  # in meters
DEBOUNCE_TIME = 0.001
SPEED_CALC_INTERVAL = 1.0
UPDATE_INTERVAL = 0.5
INACTIVITY_THRESHOLD = 1

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "encoder/data"
MODE_TOPIC = "2mtr/mode_change"   # matches your dashboard's Format Mode Change -> mqtt out node

shift_mode = 1
current_model = "Unknown"

# Only encoder pins remain physical - selector switches removed
ENCODER_PIN_A = 20
ENCODER_PIN_B = 21

model_start_pulse = 0
current_distance = 0.0
pulse_count = 0
distance = 0.0
encoder_running = False
last_a = 0
last_b = 0
speed = 0.0
last_pulse_count = 0
runtime = 0.0
downtime = 0.0
model_change_time = 0.0
rest_time = 0.0
planned_stop_time = 0.0
start_time = None
last_active_time = time.time()
last_update_time = time.time()
last_runtime_time = time.time()
last_downtime_time = time.time()
last_model_change_time = time.time()
last_rest_time = time.time()
last_planned_stop_time = time.time()
machine_status = 0

# machine_status codes - match your OEE dashboard's statusColor/statusText mapping
MODE_NORMAL = 0
MODE_DOWNTIME = 1
MODE_MODEL_CHANGE = 3
MODE_REST = 4
MODE_PLANNED_STOP = 5

# Mode is now set by MQTT from the dashboard, not GPIO pins
current_mode = MODE_NORMAL
mode_lock = threading.Lock()

distance_thread_running = True

def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(ENCODER_PIN_A, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENCODER_PIN_B, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def read_encoder():
    global pulse_count, last_a, last_b, encoder_running, last_active_time, start_time
    while distance_thread_running:
        a = GPIO.input(ENCODER_PIN_A)
        b = GPIO.input(ENCODER_PIN_B)
        if a != last_a:
            if a == b:
                pulse_count += 1
            else:
                pulse_count -= 1
            encoder_running = True
            last_active_time = time.time()
            if start_time is None:
                start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        last_a = a
        last_b = b
        time.sleep(DEBOUNCE_TIME)

def get_distance():
    global pulse_count, distance, current_distance, model_start_pulse
    while distance_thread_running:
        revolutions = pulse_count / PULSES_PER_REVOLUTION
        distance = revolutions * WHEEL_CIRCUMFERENCE
        model_revolutions = (pulse_count - model_start_pulse) / PULSES_PER_REVOLUTION
        current_distance = model_revolutions * WHEEL_CIRCUMFERENCE
        time.sleep(0.1)

def track_times():
    """Normal mode: encoder pulses drive runtime. Unexpected stalls
    (operator hasn't logged Downtime yet) still auto-accumulate as downtime,
    same safety net as the original selector-based version."""
    global runtime, downtime, encoder_running, last_active_time
    global last_runtime_time, last_downtime_time, machine_status
    current_time = time.time()
    elapsed_time = current_time - last_active_time
    if encoder_running and elapsed_time > INACTIVITY_THRESHOLD:
        encoder_running = False
        last_downtime_time = current_time
    if encoder_running:
        runtime += current_time - last_runtime_time
        last_runtime_time = current_time
        last_downtime_time = current_time
        machine_status = MODE_NORMAL
    else:
        downtime += current_time - last_downtime_time
        last_downtime_time = current_time
        last_runtime_time = current_time
        machine_status = MODE_DOWNTIME

def track_downtime():
    """Explicit Downtime logged from the dashboard (Quality/Production/Machine)."""
    global downtime, last_runtime_time, last_downtime_time, machine_status
    current_time = time.time()
    downtime += current_time - last_downtime_time
    last_downtime_time = current_time
    last_runtime_time = current_time
    machine_status = MODE_DOWNTIME

def track_model_change_time():
    global model_change_time, last_runtime_time, last_downtime_time
    global last_model_change_time, machine_status
    current_time = time.time()
    model_change_time += current_time - last_model_change_time
    last_downtime_time = current_time
    last_runtime_time = current_time
    last_model_change_time = current_time
    machine_status = MODE_MODEL_CHANGE

def track_rest_time():
    global rest_time, last_runtime_time, last_downtime_time, last_rest_time, machine_status
    current_time = time.time()
    rest_time += current_time - last_rest_time
    last_downtime_time = current_time
    last_runtime_time = current_time
    last_rest_time = current_time
    machine_status = MODE_REST

def track_planned_stop_time():
    global planned_stop_time, last_runtime_time, last_downtime_time, last_planned_stop_time, machine_status
    current_time = time.time()
    planned_stop_time += current_time - last_planned_stop_time
    last_downtime_time = current_time
    last_runtime_time = current_time
    last_planned_stop_time = current_time
    machine_status = MODE_PLANNED_STOP

def calculate_speed():
    global speed, last_pulse_count
    while distance_thread_running:
        time.sleep(SPEED_CALC_INTERVAL)
        pulse_diff = pulse_count - last_pulse_count
        revolutions_per_second = pulse_diff / PULSES_PER_REVOLUTION / SPEED_CALC_INTERVAL
        speed = revolutions_per_second * WHEEL_CIRCUMFERENCE * 60
        last_pulse_count = pulse_count

def reset_values():
    global pulse_count, model_start_pulse, distance, runtime, downtime
    global model_change_time, rest_time, planned_stop_time, speed
    global encoder_running, start_time, current_distance
    model_start_pulse = 0
    pulse_count = 0
    distance = 0.0
    runtime = 0.0
    downtime = 0.0
    model_change_time = 0.0
    rest_time = 0.0
    planned_stop_time = 0.0
    speed = 0.0
    encoder_running = False
    start_time = None
    current_distance = 0.0
    print("Values reset at:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

def publish_data(client, current_distance, distance, runtime, downtime, model_change_time, rest_time, planned_stop_time, velocity, machine_status):
    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "current_distance": round(current_distance, 2),
        "distance": round(distance, 2),
        "runtime": round(runtime, 2),
        "downtime": round(downtime, 2),
        "model_change_time": round(model_change_time, 2),
        "rest_time": round(rest_time, 2),
        "planned_stop_time": round(planned_stop_time, 2),
        "velocity": round(velocity, 2),
        "machine_status": machine_status
    }
    json_payload = json.dumps(payload)
    client.publish(MQTT_TOPIC, json_payload)
    print(f"Published: {json_payload}")

def start_distance_thread():
    threading.Thread(target=get_distance, daemon=True).start()
    threading.Thread(target=read_encoder, daemon=True).start()
    threading.Thread(target=calculate_speed, daemon=True).start()

setup_gpio()
start_distance_thread()

def on_message(client, userdata, msg):
    global shift_mode, current_model, current_mode
    global model_start_pulse, pulse_count
    global last_runtime_time, last_downtime_time, last_model_change_time
    global last_rest_time, last_planned_stop_time
    try:
        topic = msg.topic
        payload_str = msg.payload.decode()

        if topic == "encoder/shift":
            if payload_str in ["0", "1"]:
                shift_mode = int(payload_str)
                print(f"Shift mode updated: {shift_mode} ({'OT' if shift_mode == 0 else 'No OT'})")
            return

        if topic == "encoder/model":
            current_model = payload_str
            print(f"Model updated from MQTT: {current_model}")
            return

        if topic == MODE_TOPIC:
            data = json.loads(payload_str)
            new_mode_str = data.get("mode", "Normal")

            # dashboard payload already carries model + OT status - stay in sync
            if data.get("model"):
                current_model = str(data["model"])
            if "overtime" in data:
                shift_mode = 0 if data["overtime"] is True else 1

            now = time.time()
            with mode_lock:
                last_runtime_time = now
                last_downtime_time = now
                last_model_change_time = now
                last_rest_time = now
                last_planned_stop_time = now

                if new_mode_str == "Normal":
                    current_mode = MODE_NORMAL
                elif new_mode_str == "Rest":
                    current_mode = MODE_REST
                elif new_mode_str == "Planned Stop":
                    current_mode = MODE_PLANNED_STOP
                elif new_mode_str == "Downtime":
                    current_mode = MODE_DOWNTIME
                elif new_mode_str == "Model Change":
                    current_mode = MODE_MODEL_CHANGE
                    model_start_pulse = pulse_count
                elif new_mode_str == "End Shift":
                    current_mode = MODE_NORMAL
                    reset_values()
                else:
                    current_mode = MODE_NORMAL

            print(f"[MODE] Switched to: {new_mode_str} (status {current_mode})")

    except Exception as e:
        print("Error parsing MQTT message:", e)

client = mqtt.Client(protocol=mqtt.MQTTv311)
client.on_message = on_message
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.subscribe("encoder/shift")
client.subscribe("encoder/model")
client.subscribe(MODE_TOPIC)
client.loop_start()

print("2MTR ENCODER SERVICE STARTED (dashboard-driven mode)")

try:
    while True:
        with mode_lock:
            mode_now = current_mode

        if mode_now == MODE_NORMAL:
            track_times()
        elif mode_now == MODE_DOWNTIME:
            track_downtime()
        elif mode_now == MODE_MODEL_CHANGE:
            track_model_change_time()
        elif mode_now == MODE_REST:
            track_rest_time()
        elif mode_now == MODE_PLANNED_STOP:
            track_planned_stop_time()

        current_time = time.time()
        if current_time - last_update_time >= UPDATE_INTERVAL:
            names = {0: 'Normal', 1: 'Downtime', 3: 'Model Change', 4: 'Rest', 5: 'Planned Stop'}
            print(f"\nMode: {names.get(mode_now, 'Unknown')}")
            publish_data(client, current_distance, distance, runtime, downtime, model_change_time, rest_time, planned_stop_time, speed, machine_status)
            last_update_time = current_time

        time.sleep(1)

except KeyboardInterrupt:
    distance_thread_running = False
    GPIO.cleanup()