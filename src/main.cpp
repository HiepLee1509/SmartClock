#include "config.h"
#include "bp_sender.h"
#include "display_ui.h"
#include "mqtt_manager.h"
#include "network_manager.h"
#include "sensor_env.h"
#include "sensor_health.h"
#include "touch_handler.h"
#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>

ScreenState currentState = SCREEN_CLOCK;

unsigned long lastDisplayUpdate = 0;
unsigned long lastEnvUpdate = 0;
unsigned long lastMqttEnvPublish = 0;
unsigned long lastMqttHealthPublish = 0;

// Cấu hình cảnh báo nhiệt độ phòng cao
bool isTempAlertActive = false;
unsigned long tempAlertStartTime = 0;
unsigned long lastTempAlertEndTime = 0;

void setup() {
  Serial.begin(115200);
  delay(2000);

  network_init();
  mqtt_init();

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN, I2C_SPEED);

  touch_init();
  display_init();
  health_init();
  env_init();

  // QUAN TRỌNG: Khôi phục lại cấu hình I2C sau khi env_init()
  // Vì Adafruit_I2CDevice::begin() bên trong sht31.begin() gọi Wire.begin()
  // KHÔNG tham số, làm reset bus I2C về pin mặc định của ESP32-C3
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN, I2C_SPEED);

  env_update();

  // Khởi tạo module gửi IR buffer lên Backend cho AI huyết áp
  bp_sender_init(BACKEND_BP_URL);
}

void loop() {
  health_update();

  // Cấp nhật module BP sender: gửi raw IR vào để tự động gom buffer và gửi
  bp_sender_update(get_raw_ir());

  TouchAction action = touch_get_action();

  if (action == TOUCH_SHORT) {
    currentState = (ScreenState)((currentState + 1) % SCREEN_MAX_STATES);
  } else if (action == TOUCH_LONG) {
    display_show_sleep_msg();
    delay(1500);
    display_poweroff();
    touch_check_deep_sleep(action);
  }

  // Cập nhật dữ liệu môi trường mỗi 5 giây
  if (millis() - lastEnvUpdate >= 5000) {
    env_update();
    lastEnvUpdate = millis();

    // Kích hoạt cảnh báo chỉ khi đã có dữ liệu hợp lệ và nhiệt độ vượt 30°C
    if (env_is_valid() && get_temp() > 30.0) {
      if (!isTempAlertActive && (lastTempAlertEndTime == 0 ||
                                 millis() - lastTempAlertEndTime >= 20000)) {
        isTempAlertActive = true;
        tempAlertStartTime = millis();
      }
    }
  }

  // Kiểm tra kết thúc thời gian cảnh báo (hết 3 giây)
  if (isTempAlertActive && (millis() - tempAlertStartTime >= 3000)) {
    isTempAlertActive = false;
    lastTempAlertEndTime = millis(); // Ghi nhận thời điểm kết thúc cảnh báo
  }

  // Cập nhật OLED mỗi 30ms (~33 FPS)
  if (millis() - lastDisplayUpdate >= 30) {
    if (isTempAlertActive && currentState == SCREEN_ENV) {
      display_show_temp_alert();
    } else {
      display_update(currentState);
    }
    lastDisplayUpdate = millis();
  }

  mqtt_loop();

  // Gửi dữ liệu môi trường lên Adafruit IO mỗi 10 giây
  static bool envPublishPending = true;
  if (envPublishPending ||
      (millis() - lastMqttEnvPublish >= MQTT_PUBLISH_ENV_INTERVAL_MS)) {
    if (mqtt_publish_env(get_temp(), get_humi())) {
      lastMqttEnvPublish = millis();
      envPublishPending = false;
    }
  }

  // Gửi dữ liệu sức khỏe lên Adafruit IO mỗi 3 giây
  static bool wasHealthZero = true;
  int bpm = get_bpm();
  int spo2 = get_spo2();
  bool hasFinger = (bpm > 0 && spo2 > 0);

  if (hasFinger) {
    if (millis() - lastMqttHealthPublish >= MQTT_PUBLISH_HEALTH_INTERVAL_MS) {
      if (mqtt_publish_health(bpm, spo2)) {
        lastMqttHealthPublish = millis();
        wasHealthZero = false;
      }
    }
  } else {
    // Không có ngón tay: chỉ gửi số 0 một lần duy nhất khi vừa rút tay ra
    // (wasHealthZero đang là false)
    if (!wasHealthZero) {
      if (mqtt_publish_health(0, 0)) {
        wasHealthZero = true;
        lastMqttHealthPublish = millis();
      }
    }
  }
}