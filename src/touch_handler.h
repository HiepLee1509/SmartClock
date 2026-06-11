#ifndef TOUCH_HANDLER_H
#define TOUCH_HANDLER_H

#include "config.h"

// Khởi tạo chân cảm biến
void touch_init();

// Hàm quét trạng thái cảm biến (gọi liên tục trong loop/task)
TouchAction touch_get_action();

// Hàm xử lý đưa ESP32 vào chế độ ngủ sâu
void touch_check_deep_sleep(TouchAction action);

#endif // TOUCH_HANDLER_H