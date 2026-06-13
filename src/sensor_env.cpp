#include "sensor_env.h"
#include <Adafruit_SHT31.h>
#include <Wire.h>
#include <math.h>
#include "config.h"

Adafruit_SHT31 sht31 = Adafruit_SHT31();

static float currentTemp = NAN;
static float currentHumi = NAN;
static bool envValid = false; // true khi đã có ít nhất 1 lần đọc thành công

void env_init() {
  // sht31.begin() nội bộ gọi Wire.begin() KHÔNG tham số → reset I2C bus
  // về default pins → init fail → sensor không được soft-reset → readBoth() NaN.
  // Giải pháp: Nếu lần 1 fail, khôi phục Wire đúng pins rồi thử lại.
  if (!sht31.begin(0x44)) {
    Serial.println("SHT31 init failed (I2C bus reset). Restoring Wire and retrying...");
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN, I2C_SPEED);
    delay(10);
    if (!sht31.begin(0x44)) {
      Serial.println("SHT31 not found after retry!");
    } else {
      Serial.println("SHT31 init OK on retry.");
    }
  }
}

void env_update() {
  float t = NAN, h = NAN;

  // Dùng readBoth() để đo 1 lần I2C duy nhất, tránh tranh chấp bus với MAX30102
  // (readTemperature() + readHumidity() riêng lẻ = 2 lần I2C, dễ gây NaN)
  if (!sht31.readBoth(&t, &h)) {
    // Retry 1 lần sau 5ms nếu lỗi I2C thoáng qua (VD: OLED vừa sendBuffer xong)
    delay(5);
    if (!sht31.readBoth(&t, &h)) {
      return; // Thất bại thật sự → giữ nguyên giá trị cũ, không reset về 0
    }
  }

  // Chỉ cập nhật khi giá trị hợp lệ
  if (!isnan(t)) {
    currentTemp = t;
    envValid = true;
  }

  if (!isnan(h)) {
    currentHumi = h;
  }
}

float get_temp() { return currentTemp; }
float get_humi() { return currentHumi; }
bool env_is_valid() { return envValid; }