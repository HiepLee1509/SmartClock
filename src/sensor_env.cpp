#include "sensor_env.h"
#include <Wire.h>
#include <Adafruit_SHT31.h>
#include <math.h>

Adafruit_SHT31 sht31 = Adafruit_SHT31();

static float currentTemp = 0.0;
static float currentHumi = 0.0;

void env_init() {
    // Khởi tạo SHT31 với địa chỉ 0x44
    if (!sht31.begin(0x44)) {
        Serial.println("SHT31 not found!");
    }
}

void env_update() {
    float t = NAN, h = NAN;

    // Dùng readBoth() để đo 1 lần I2C duy nhất, tránh tranh chấp bus với MAX30102
    // (readTemperature() + readHumidity() riêng lẻ = 2 lần I2C, dễ gây NaN)
    if (!sht31.readBoth(&t, &h)) {
        return;
    }

    if (!isnan(t)) {
        currentTemp = t;
    }

    if (!isnan(h)) {
        currentHumi = h;
    }
}

float get_temp() { return currentTemp; }
float get_humi() { return currentHumi; }