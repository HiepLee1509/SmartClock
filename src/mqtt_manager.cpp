#include "mqtt_manager.h"
#include "config.h"
#include <PubSubClient.h>
#include <WiFi.h>

static WiFiClient espClient;
static PubSubClient mqttClient(espClient);

static unsigned long lastReconnectAttempt = 0;

// Hàm nội bộ để gửi dữ liệu lên feed cụ thể
static bool publish_feed(const char *feed_name, const char *payload) {
  if (!mqttClient.connected())
    return false;

  // Định dạng topic Adafruit IO: {username}/feeds/{feed_key}
  String topic = String(ADAFRUIT_IO_USERNAME) + "/feeds/" + String(feed_name);

  Serial.printf("MQTT Publish [%s]: %s\n", topic.c_str(), payload);

  return mqttClient.publish(topic.c_str(), payload);
}

void mqtt_init() { mqttClient.setServer(MQTT_BROKER, MQTT_PORT); }

void mqtt_loop() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  if (!mqttClient.connected()) {
    unsigned long now = millis();
    // Kiểm tra xem đã đủ thời gian giãn cách giữa các lần reconnect chưa
    if (now - lastReconnectAttempt >= MQTT_RECONNECT_INTERVAL_MS) {
      lastReconnectAttempt = now;

      // Sinh Client ID duy nhất dựa trên địa chỉ MAC của ESP32
      String clientId =
          "ESP32C3-Clock-" + String((uint32_t)ESP.getEfuseMac(), HEX);

      if (mqttClient.connect(clientId.c_str(), ADAFRUIT_IO_USERNAME,
                             ADAFRUIT_IO_KEY)) {
        Serial.println("MQTT Connected!");
        lastReconnectAttempt = 0; // Reset thời gian chờ kết nối lại
      } else {
        Serial.printf("MQTT Connect Failed, rc=%d\n", mqttClient.state());
      }
    }
  } else {
    mqttClient.loop();
  }
}

bool mqtt_publish_env(float temp, float humi) {
  if (!mqttClient.connected()) {
    return false;
  }

  bool success = true;

  char temp_str[10];
  char humi_str[10];

  dtostrf(temp, 1, 1, temp_str);
  dtostrf(humi, 1, 1, humi_str);

  success &= publish_feed("temp", temp_str);
  success &= publish_feed("humi", humi_str);

  return success;
}

bool mqtt_publish_health(int bpm, int spo2) {
  if (!mqttClient.connected()) {
    return false;
  }

  bool success = true;

  char bpm_str[10];
  char spo2_str[10];

  itoa(bpm, bpm_str, 10);
  itoa(spo2, spo2_str, 10);

  // Chỉ gửi thông số sức khỏe (BPM và SpO2) khi có tay đo thực tế (giá trị > 0)
  // để tránh vẽ giá trị 0 gây nhiễu biểu đồ trên Adafruit IO Dashboard
  if (bpm > 0) {
    success &= publish_feed("bpm", bpm_str);
  }
  if (spo2 > 0) {
    success &= publish_feed("spo2", spo2_str);
  }

  return success;
}
