# FuelGuard ESP32 Nozzle Controller

Firmware for the dispenser-side controller that serves the FuelGuard mobile
app over the `FuelMonitor` access point and reports to the FuelGuard backend
over station WiFi.

## What it does

| Surface | Detail |
|---|---|
| Access point | `FuelMonitor` (WPA2) at `192.168.4.1` for the phone |
| Session API | `GET /` start, `/info` probe, `/data` live readings, `/capture` photo, `/reset` clear |
| Live readings | Dual sensor JSON: `{ "flowmeter": L, "dispenser": L, "state": idle\|running\|finished, "tamper": bool }` |
| Flow meter | YF-S201 pulse counting with per-device calibration stored in NVS |
| Dispenser side | Second pulse tap on the pump display, or a simulated over-report for demos (`DISPENSER_REAL_INPUT 0`) |
| Tamper | Latched magnetic reed / vibration input — once true it stays true until reboot |
| Backend link | Heartbeat ping, command polling, calibration and OTA — all authenticated with the device id + key pair |
| OTA | Pulls the published binary through the backend command queue with progress acks |

## Wiring (defaults in config.h)

```
YF-S201 flow meter   OUT -> GPIO 27    (+5V, GND; 3.3V-tolerant output is fine)
Dispenser pulse tap  OUT -> GPIO 26    (same pulse class as the flow meter)
Tamper reed/module   OUT -> GPIO 25    (closes to GND when the cover opens)
```

Every pin, threshold and credential lives in `src/config.h`.

## Provisioning a new device

1. Register the device in the admin panel: **IoT Devices → Register device**.
   Use the same `device_id` you will flash, and copy the device key — it is
   shown exactly once.
2. Fill in `src/config.h`: `DEVICE_ID`, `DEVICE_KEY`, `NOZZLE_ID`, and the
   station WiFi credentials if the device should reach the backend
   (needed for calibration commands and OTA).
3. Flash:
   ```bash
   pio run -t upload && pio device monitor
   ```
   (Arduino IDE works too: combine `src/main.cpp` and `src/config.h` into one
   sketch and install ArduinoJson.)

## Verifying

* Join `FuelMonitor` on the phone, open the app, scan the nozzle QR — the
  session screen should show both readings moving.
* `curl http://192.168.4.1/info` from a phone browser returns the device JSON.
* With station WiFi connected the device appears **Online** in the admin
  IoT Devices page within a minute; the calibration and firmware buttons queue
  commands that the device picks up within 30 seconds.
* Fraud demo path: set `DISPENSER_REAL_INPUT 0` and `SIM_OVER_REPORT_PERCENT`
  to a value above the flagging threshold (default 5%), then run a session —
  the app flags the transaction and the attendant and station escalate.

## Security notes

* Device-facing backend calls are authenticated with `X-Device-Id` and
  `X-Device-Key`; the key is stored only as a hash server-side.
* The TLS client uses `setInsecure()` for the university demo. For a real
  deployment, pin the server certificate and ship it in the firmware image.
* The AP password is committed in the app UI by design (the customer must be
  able to join). Keep it different from the station's real WiFi password.
