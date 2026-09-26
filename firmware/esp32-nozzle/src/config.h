// ============================================================================
// FuelGuard nozzle controller — build configuration
//
// Copy this file to config.h (it is gitignored by convention) and fill in the
// values that identify THIS device. Everything else works out of the box.
// ============================================================================

#pragma once

// ── Device identity ──────────────────────────────────────────────────────────
// Must match a device registered in the admin panel (IoT Devices page).
// The device key is shown once at registration — paste it here.
#define DEVICE_ID        "FG-ESP32-0001"
#define DEVICE_KEY       "paste-your-device-key"
#define NOZZLE_ID        "NZ001"
#define FIRMWARE_VERSION "1.0.0"

// ── Access point the phone joins (the app expects these values) ──────────────
#define AP_SSID          "FuelMonitor"
#define AP_PASSWORD      "12345678"   // WPA2, minimum 8 chars

// ── Station WiFi (optional, needed for the command channel + OTA) ────────────
#define STA_SSID         ""           // empty = command polling disabled
#define STA_PASSWORD     ""
#define BACKEND_HOST     "fuelguard-backend-production.up.railway.app"
#define BACKEND_PORT     443
#define BACKEND_USE_TLS  1

// ── Sensor pins ──────────────────────────────────────────────────────────────
#define FLOW_PIN         27           // YF-S201 pulse output (true volume)
#define DISPENSER_PIN    26           // Pulse tap on the pump display side
#define TAMPER_PIN       25           // Magnetic reed / vibration module
#define TAMPER_ACTIVE_LOW 1           // Reed closes to GND when cover opens

// ── Dispenser reading source ─────────────────────────────────────────────────
// 1 = real second pulse input on DISPENSER_PIN
// 0 = simulated: dispenser = flowmeter * (1 + SIM_OVER_REPORT_PERCENT/100)
//     which lets you dial in a fraud scenario for demos.
#define DISPENSER_REAL_INPUT 1
#define SIM_OVER_REPORT_PERCENT 8.0f

// ── Camera ───────────────────────────────────────────────────────────────────
// 0 = no camera on this board, /capture returns 501.
// 1 = ESP32-CAM (AI-Thinker pin map). On a plain ESP32+cam wiring, review the
// pin block in cameraInit() first.
#define ENABLE_CAMERA    0

// ── Behaviour tuning ─────────────────────────────────────────────────────────
#define FLOW_CAL_PULSES_PER_LITRE 7.5f  // YF-S201 nominal; calibrate per unit
#define FLOW_CAL_OFFSET_LITRES    0.0f
#define FLOW_START_LPM  0.8f            // L/min above which a session starts
#define FINISH_QUIET_MS 6000UL          // quiet time before state=finished
#define COMMAND_POLL_MS 30000UL         // backend command poll interval
#define PING_MS         60000UL         // backend heartbeat interval
