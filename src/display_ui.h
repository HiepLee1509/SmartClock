#ifndef DISPLAY_UI_H
#define DISPLAY_UI_H

#include "config.h"

// Khởi tạo màn hình
void display_init();

// Cập nhật giao diện tùy theo trạng thái màn hình
void display_update(ScreenState state);

// Hiển thị thông báo trước khi đi ngủ
void display_show_sleep_msg();

void display_poweroff();

// Hiển thị cảnh báo nhiệt độ phòng cao nhấp nháy
void display_show_temp_alert();

#endif // DISPLAY_UI_H