// ============================================================================
// FuelGuard nozzle controller — ESP32 firmware
//
// Serves the phone-facing contract the FuelGuard app expects on the
// FuelMonitor access point (192.168.4.1):
//
//   GET /        start a session            (HTML, body ignored by the app)
//   GET /info    reachability probe         (JSON, any 200 works)
//   GET /data    live dual-sensor reading   (JSON, polled every second)
//   GET /capture photo at session end      (raw JPEG bytes)
//   GET /reset   clear the finished session
//
// When the device also has station WiFi it talks to the backend:
//   heartbeat ping, command polling (calibration / camera / OTA) and acks —
//   authenticated with the device id + key pair issued at registration.
// ============================================================================

#include <Arduino.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>

#if ENABLE_CAMERA
#include "esp_camera.h"
#endif

#include "config.h"

// ── Session state ────────────────────────────────────────────────────────────

enum SessionState : uint8_t { IDLE, RUNNING, FINISHED };

static SessionState state = IDLE;
static bool tamperLatched = false;

static volatile uint32_t flowPulses = 0;
static volatile uint32_t dispenserPulses = 0;

static float calibrationFactor = FLOW_CAL_PULSES_PER_LITRE;
static float calibrationOffset = FLOW_CAL_OFFSET_LITRES;
static float simOverReportPercent = SIM_OVER_REPORT_PERCENT;

static float flowmeterLitres = 0.0f;
static float dispenserLitres = 0.0f;
static float currentLpm = 0.0f;

static uint32_t lastPulseCountFlow = 0;
static uint32_t lastPulseCountDispenser = 0;
static uint32_t lastFlowSampleMs = 0;
static uint32_t lastActivityMs = 0;

static Preferences nvs;

static WebServer server(80);

// ── Pulse counting ───────────────────────────────────────────────────────────

static portMUX_TYPE pulseMux = portMUX_INITIALIZER_UNLOCKED;

void IRAM_ATTR onFlowPulse() {
  portENTER_CRITICAL_ISR(&pulseMux);
  flowPulses++;
  portEXIT_CRITICAL_ISR(&pulseMux);
}

#if DISPENSER_REAL_INPUT
void IRAM_ATTR onDispenserPulse() {
  portENTER_CRITICAL_ISR(&pulseMux);
  dispenserPulses++;
  portEXIT_CRITICAL_ISR(&pulseMux);
}
#endif

static void loadCalibration() {
  nvs.begin("fuelguard", false);
  calibrationFactor = nvs.getFloat("cal", FLOW_CAL_PULSES_PER_LITRE);
  calibrationOffset = nvs.getFloat("off", FLOW_CAL_OFFSET_LITRES);
  simOverReportPercent = nvs.getFloat("sim", SIM_OVER_REPORT_PERCENT);
  nvs.end();
}

static void saveCalibration() {
  nvs.begin("fuelguard", false);
  nvs.putFloat("cal", calibrationFactor);
  nvs.putFloat("off", calibrationOffset);
  nvs.putFloat("sim", simOverReportPercent);
  nvs.end();
}

// ── Sampling ─────────────────────────────────────────────────────────────────

static void sampleFlow() {
  const uint32_t now = millis();
  const uint32_t elapsed = now - lastFlowSampleMs;
  if (elapsed < 1000) return;
  lastFlowSampleMs = now;

  portENTER_CRITICAL(&pulseMux);
  const uint32_t fPulses = flowPulses;
  const uint32_t dPulses = dispenserPulses;
  flowPulses = 0;
  dispenserPulses = 0;
  portEXIT_CRITICAL(&pulseMux);

  // Litres this window, from raw pulses
  const float fLitres = (fPulses / calibrationFactor) + calibrationOffset;
  flowmeterLitres += max(0.0f, fLitres);

#if DISPENSER_REAL_INPUT
  const float dLitres = dPulses / calibrationFactor;  // same meter class
  dispenserLitres += max(0.0f, dLitres);
#else
  // Simulated dispenser with a dialled-in over-report — demo fraud scenarios
  dispenserLitres += max(0.0f, fLitres) * (1.0f + simOverReportPercent / 100.0f);
#endif

  currentLpm = (fPulses / calibrationFactor) * (60000.0f / elapsed);

  switch (state) {
    case IDLE:
      if (currentLpm > FLOW_START_LPM) {
        state = RUNNING;
        lastActivityMs = now;
      }
      break;
    case RUNNING:
      if (currentLpm > FLOW_START_LPM) {
        lastActivityMs = now;
      } else if (now - lastActivityMs > FINISH_QUIET_MS) {
        state = FINISHED;
      }
      break;
    case FINISHED:
      break;
  }
}

static void resetSession() {
  flowmeterLitres = 0.0f;
  dispenserLitres = 0.0f;
  currentLpm = 0.0f;
  state = IDLE;
  // Tamper stays latched until a reboot: a cover opening mid-shift must not
  // silently clear because a session restarted.
}

// ── WiFi ─────────────────────────────────────────────────────────────────────

static bool staConnected = false;

static void wifiStart() {
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_SSID, AP_PASSWORD);
  Serial.printf("[WiFi] AP '%s' up at %s\n", AP_SSID,
                WiFi.softAPIP().toString().c_str());

#if STA_SSID[0] != '\0'
  WiFi.begin(STA_SSID, STA_PASSWORD);
  Serial.println("[WiFi] connecting to station network...");
#endif
}

static void wifiMaintain() {
#if STA_SSID[0] != '\0'
  const bool up = (WiFi.status() == WL_CONNECTED);
  if (up != staConnected) {
    staConnected = up;
    Serial.printf("[WiFi] station %s (%s)\n", up ? "connected" : "down",
                  WiFi.localIP().toString().c_str());
  }
#endif
}

// ── Backend channel (device key auth) ────────────────────────────────────────

static String urlEncode(const String &value);
static void backendRequest(const String &method, const String &path,
                           const String &body = "");

static String backendGet(const String &path) {
#if BACKEND_USE_TLS
  WiFiClientSecure client;
  client.setInsecure();  // public API over TLS; pin the cert for production fleets
#else
  WiFiClient client;
#endif
  if (!client.connect(BACKEND_HOST, BACKEND_PORT)) return "";
  client.print(String("GET ") + path + " HTTP/1.1\r\nHost: " + BACKEND_HOST +
               "\r\nX-Device-Id: " DEVICE_ID
               "\r\nX-Device-Key: " DEVICE_KEY
               "\r\nConnection: close\r\n\r\n");
  const uint32_t start = millis();
  while (!client.available() && millis() - start < 5000) { delay(10); }
  String body;
  bool bodyStarted = false;
  while (client.available()) {
    String line = client.readStringUntil('\n');
    if (!bodyStarted && line.length() <= 1) { bodyStarted = true; continue; }
    if (bodyStarted) body += line + "\n";
  }
  return body;
}

static void backendAck(const String &commandId, const char *status,
                       int progress = 0, const char *error = "") {
  String path = String("/api/v1/iot/devices/") + DEVICE_ID +
                "/commands/" + commandId + "/ack?status=" + status;
  if (progress > 0) path += "&progress=" + String(progress);
  if (error[0] != '\0') path += "&error=" + urlEncode(error);

#if BACKEND_USE_TLS
  WiFiClientSecure client;
  client.setInsecure();
#else
  WiFiClient client;
#endif
  if (!client.connect(BACKEND_HOST, BACKEND_PORT)) return;
  client.print(String("POST ") + path + " HTTP/1.1\r\nHost: " + BACKEND_HOST +
               "\r\nX-Device-Id: " DEVICE_ID
               "\r\nX-Device-Key: " DEVICE_KEY
               "\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
  delay(200);
  client.stop();
}

static String urlEncode(const String &value) {
  String out;
  char buf[8];
  for (unsigned int i = 0; i < value.length(); i++) {
    char c = value[i];
    if (isalnum(c) || c == '-' || c == '_' || c == '.') {
      out += c;
    } else {
      snprintf(buf, sizeof(buf), "%%%02X", c);
      out += buf;
    }
  }
  return out;
}

// ── OTA (HTTPUpdate) ─────────────────────────────────────────────────────────

static void handleFirmwareUpdate(const String &commandId, JsonObject payload) {
  const String url = payload["download_url"].as<String>();
  if (url.isEmpty()) {
    backendAck(commandId, "failed", 0, "missing download_url");
    return;
  }
  backendAck(commandId, "progress", 5);

  WiFiClient client;
  HTTPClient http;
  http.begin(client, url);
  const int code = http.GET();
  if (code != 200) {
    backendAck(commandId, "failed", 0, String("download http ") + code);
    http.end();
    return;
  }
  const int len = http.getSize();
  if (!Update.begin(len)) {
    backendAck(commandId, "failed", 0, "not enough flash space");
    http.end();
    return;
  }
  size_t written = 0;
  WiFiClient *stream = http.getStreamPtr();
  uint8_t buf[1024];
  while (written < (size_t)len) {
    const size_t avail = stream->available();
    if (avail) {
      const size_t n = stream->readBytes(buf, min(avail, sizeof(buf)));
      if (Update.write(buf, n) != n) {
        backendAck(commandId, "failed", 0, "flash write error");
        http.end();
        return;
      }
      written += n;
      const int percent = (written * 90) / len;  // keep 10% for the reboot
      static int lastReported = -1;
      if (percent - lastReported >= 20) {
        lastReported = percent;
        backendAck(commandId, "progress", percent);
      }
    }
    delay(2);
  }
  http.end();

  if (Update.end(true)) {
    backendAck(commandId, "complete", 100);
    Serial.println("[OTA] complete, rebooting");
    delay(500);
    ESP.restart();
  } else {
    backendAck(commandId, "failed", 0, Update.errorString());
  }
}

static void pollCommands() {
#if STA_SSID[0] == '\0'
  return;
#endif
  if (!staConnected) return;

  const String body = backendGet(
      String("/api/v1/iot/devices/") + DEVICE_ID + "/commands/pending");
  if (body.isEmpty()) return;

  JsonDocument doc;
  if (deserializeJson(doc, body)) return;

  for (JsonObject cmd : doc["commands"].as<JsonArray>()) {
    const String id = cmd["id"].as<String>();
    const String command = cmd["command"].as<String>();
    JsonObject payload = cmd["payload"].as<JsonObject>();
    Serial.printf("[CMD] %s (%s)\n", command.c_str(), id.c_str());

    if (command == "flow_meter_calibrate") {
      if (payload["calibration_factor"].is<float>()) {
        calibrationFactor = payload["calibration_factor"].as<float>();
      }
      if (payload["offset"].is<float>()) {
        calibrationOffset = payload["offset"].as<float>();
      }
      saveCalibration();
      backendAck(id, "complete");
    } else if (command == "camera_capture") {
#if ENABLE_CAMERA
      // Frames are served on demand via /capture; the ack just proves liveness.
      backendAck(id, "complete");
#else
      backendAck(id, "failed", 0, "camera not fitted");
#endif
    } else if (command == "firmware_update") {
      handleFirmwareUpdate(id, payload);
    } else {
      backendAck(id, "failed", 0, "unknown command");
    }
  }
}

static void sendPing() {
#if STA_SSID[0] == '\0'
  return;
#endif
  if (!staConnected) return;
  backendAck("ping", "complete");  // heartbeat piggybacks the ack path shape
}

// ── HTTP endpoints ───────────────────────────────────────────────────────────

static void handleStart() {
  resetSession();
  state = RUNNING;
  lastActivityMs = millis();
  server.send(200, "text/html",
              "<html><body><h3>FuelGuard session started</h3></body></html>");
}

static void handleInfo() {
  JsonDocument doc;
  doc["device"] = DEVICE_ID;
  doc["nozzle"] = NOZZLE_ID;
  doc["firmware"] = FIRMWARE_VERSION;
  doc["state"] = state == RUNNING ? "running" : state == FINISHED ? "finished" : "idle";
  doc["calibration"] = calibrationFactor;
  doc["ip"] = WiFi.localIP().toString();
  String out;
  serializeJson(doc, out);
  server.send(200, "application/json", out);
}

static void handleData() {
  JsonDocument doc;
  doc["flowmeter"] = serialized(String(flowmeterLitres, 3));
  doc["dispenser"] = serialized(String(dispenserLitres, 3));
  doc["state"] = state == RUNNING ? "running" : state == FINISHED ? "finished" : "idle";
  doc["tamper"] = tamperLatched;
  String out;
  serializeJson(doc, out);
  server.send(200, "application/json", out);
}

static void handleReset() {
  resetSession();
  server.send(200, "text/html", "<html><body>reset</body></html>");
}

#if ENABLE_CAMERA
static camera_config_t cameraConfig() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = 5;  c.pin_d1 = 18; c.pin_d2 = 19; c.pin_d3 = 21;
  c.pin_d4 = 36; c.pin_d5 = 39; c.pin_d6 = 34; c.pin_d7 = 35;
  c.pin_xclk = 0;  c.pin_pclk = 22; c.pin_vsync = 25;
  c.pin_href = 23; c.pin_sccb_sda = 26; c.pin_sccb_scl = 27;
  c.pin_pwdn = 32; c.pin_reset = -1;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAMESIZE_VGA;
  c.jpeg_quality = 12;
  c.fb_count = 1;
  c.grab_mode = CAMERA_GRAB_LATEST;
  return c;
}
#endif

static void handleCapture() {
#if ENABLE_CAMERA
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    server.send(500, "text/plain", "capture failed");
    return;
  }
  server.setContentLength(fb->len);
  server.send(200, "image/jpeg", "");
  server.sendContent(reinterpret_cast<const char *>(fb->buf), fb->len);
  esp_camera_fb_return(fb);
#else
  server.send(501, "text/plain", "camera not fitted on this board");
#endif
}

// ── Setup / loop ─────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);

  pinMode(TAMPER_PIN, INPUT_PULLUP);

  loadCalibration();

  server.on("/", handleStart);
  server.on("/info", handleInfo);
  server.on("/data", handleData);
  server.on("/reset", handleReset);
  server.on("/capture", handleCapture);
  server.onNotFound([]() { server.send(404, "text/plain", "not found"); });
  server.begin();

#if ENABLE_CAMERA
  if (esp_camera_init(&cameraConfig()) != ESP_OK) {
    Serial.println("[CAM] init failed - /capture will 500");
  }
#endif

  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), onFlowPulse, RISING);
#if DISPENSER_REAL_INPUT
  attachInterrupt(digitalPinToInterrupt(DISPENSER_PIN), onDispenserPulse, RISING);
#endif

  wifiStart();
}

void loop() {
  server.handleClient();
  sampleFlow();
  wifiMaintain();

  // Tamper is latched: once true it stays true until reboot.
  if (!tamperLatched) {
#if TAMPER_ACTIVE_LOW
    tamperLatched = (digitalRead(TAMPER_PIN) == LOW);
#else
    tamperLatched = (digitalRead(TAMPER_PIN) == HIGH);
#endif
  }

  static uint32_t lastCommandMs = 0;
  if (millis() - lastCommandMs > COMMAND_POLL_MS) {
    lastCommandMs = millis();
    pollCommands();
  }

  static uint32_t lastPingMs = 0;
  if (millis() - lastPingMs > PING_MS) {
    lastPingMs = millis();
    sendPing();
  }

  delay(2);
}
