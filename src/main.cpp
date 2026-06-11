#include "config.h"
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

// Các biến lưu thời gian để chia luồng (Timer)
unsigned long lastDisplayUpdate = 0;
unsigned long lastSerialPrint = 0;
unsigned long lastEnvUpdate = 0;      // Timer cho SHT31
unsigned long lastMqttEnvPublish = 0; // Timer gửi dữ liệu môi trường lên MQTT
unsigned long lastMqttHealthPublish = 0; // Timer gửi dữ liệu sức khỏe lên MQTT

// Cấu hình cảnh báo nhiệt độ phòng cao
bool isTempAlertActive = false;
unsigned long tempAlertStartTime = 0;
unsigned long lastTempAlertEndTime =
    0; // Ghi nhận thời điểm lần cảnh báo trước kết thúc

void setup() {
  Serial.begin(115200);
  delay(2000);

  network_init(); // dua len dau de tranh loi sut ap khi ket noi wifi
  mqtt_init();    // Khởi tạo các cấu hình cho MQTT Client

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN, I2C_SPEED);

  touch_init();
  display_init();
  health_init();
  env_init();

  // QUAN TRỌNG: Khôi phục lại cấu hình I2C sau khi env_init()
  // Vì Adafruit_I2CDevice::begin() bên trong sht31.begin() gọi Wire.begin()
  // KHÔNG tham số, làm reset bus I2C về pin mặc định của ESP32-C3
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN, I2C_SPEED);

  env_update(); // Cập nhật dữ liệu môi trường lần đầu ngay khi khởi động
}

void loop() {
  // 1. Quét cảm biến nhịp tim liên tục (càng nhanh càng tốt để vét sạch FIFO)
  health_update();

  // 2. Quét cảm biến chạm UI
  TouchAction action = touch_get_action();

  if (action == TOUCH_SHORT) {
    currentState = (ScreenState)((currentState + 1) % SCREEN_MAX_STATES);
  } else if (action == TOUCH_LONG) {
    display_show_sleep_msg();
    delay(1500);
    display_poweroff();
    touch_check_deep_sleep(action);
  }

  // 3. Cập nhật dữ liệu môi trường mỗi 5 giây (đủ để hiển thị mà không làm chậm
  // I2C của MAX30102)
  if (millis() - lastEnvUpdate >= 5000) {
    env_update();
    lastEnvUpdate = millis();

    // Kích hoạt cảnh báo nếu nhiệt độ vượt quá 30 độ C và hết thời gian giãn
    // cách 30s
    if (get_temp() > 30.0) {
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

  // 4. Cập nhật OLED mỗi 30ms (~33 FPS) để giải phóng băng thông I2C cho
  // MAX30102
  if (millis() - lastDisplayUpdate >= 30) {
    if (isTempAlertActive && currentState == SCREEN_ENV) {
      display_show_temp_alert();
    } else {
      display_update(currentState);
    }
    lastDisplayUpdate = millis();
  }

  // 5. In dữ liệu ra Serial Monitor đúng 1 giây 1 lần
  if (millis() - lastSerialPrint >= 1000) {
    Serial.printf(
        "Temp: %.1fC | Humi: %.1f%% | IR: %ld | BPM: %d | SpO2: %d%%\n",
        get_temp(), get_humi(), get_raw_ir(), get_bpm(), get_spo2());
    lastSerialPrint = millis();
  }

  // 6. Xử lý vòng lặp MQTT duy trì kết nối (non-blocking)
  mqtt_loop();

  // 7. Gửi dữ liệu môi trường lên Adafruit IO mỗi 10 giây (non-blocking)
  // Gửi ngay lập tức ở giây đầu tiên khi kết nối thành công
  static bool envPublishPending = true;
  if (envPublishPending ||
      (millis() - lastMqttEnvPublish >= MQTT_PUBLISH_ENV_INTERVAL_MS)) {
    if (mqtt_publish_env(get_temp(), get_humi())) {
      lastMqttEnvPublish = millis();
      envPublishPending = false;
    }
  }

  // 8. Gửi dữ liệu sức khỏe lên Adafruit IO mỗi 3 giây (non-blocking)
  // Gửi số 0 một lần duy nhất ngay khi rút tay ra
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