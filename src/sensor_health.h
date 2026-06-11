#ifndef SENSOR_HEALTH_H
#define SENSOR_HEALTH_H

#include "config.h"

// Khởi tạo cảm biến
void health_init();

// Quét dữ liệu liên tục (đặt trong loop)
void health_update();

// Lấy thông số để hiển thị
int get_bpm();
int get_spo2();
long get_raw_ir(); // Cần thiết để vẽ biểu đồ hình sin (PPG) trên OLED

#endif // SENSOR_HEALTH_H