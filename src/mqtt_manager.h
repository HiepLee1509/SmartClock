#ifndef MQTT_MANAGER_H
#define MQTT_MANAGER_H

#include <Arduino.h>

// Khởi tạo các cấu hình MQTT
void mqtt_init();

// Vòng lặp MQTT kiểm tra trạng thái kết nối và chạy client.loop() (non-blocking)
void mqtt_loop();

// Gửi dữ liệu môi trường lên các feed tương ứng trên Adafruit IO (non-blocking)
bool mqtt_publish_env(float temp, float humi);

// Gửi dữ liệu sức khỏe lên các feed tương ứng trên Adafruit IO (non-blocking)
bool mqtt_publish_health(int bpm, int spo2);

#endif // MQTT_MANAGER_H
