#include "touch_handler.h"
#include <Arduino.h>

// Các biến static để lưu trạng thái cục bộ trong file này
static unsigned long touchStartTime = 0;
static bool isTouching = false;
static bool longPressHandled = false;

void touch_init() {
    pinMode(TOUCH_BUTTON_PIN, INPUT);
    
    // In ra lý do thức dậy (Wakeup reason) để debug
    #if DEBUG_SERIAL
    esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
    if (wakeup_reason == ESP_SLEEP_WAKEUP_GPIO) {
        Serial.println("Woke up from Deep Sleep via Touch Button!");
    }
    #endif
}

TouchAction touch_get_action() {
    bool currentState = digitalRead(TOUCH_BUTTON_PIN);
    TouchAction action = TOUCH_NONE;

    if (currentState && !isTouching) {
        // Sự kiện: Vừa mới chạm tay vào
        isTouching = true;
        touchStartTime = millis();
        longPressHandled = false;
    } 
    else if (currentState && isTouching) {
        // Sự kiện: Đang giữ tay
        if (!longPressHandled && (millis() - touchStartTime >= LONG_PRESS_TIME_MS)) {
            action = TOUCH_LONG;
            longPressHandled = true; // Đánh dấu đã bắt được Long Press để không trigger liên tục
        }
    } 
    else if (!currentState && isTouching) {
        // Sự kiện: Vừa nhả tay ra
        isTouching = false;
        unsigned long pressDuration = millis() - touchStartTime;
        
        // Nếu nhả tay mà chưa đạt ngưỡng long press, và đủ dài để loại bỏ nhiễu (debounce > 50ms)
        if (!longPressHandled && pressDuration > 50) {
            action = TOUCH_SHORT;
        }
    }

    return action;
}

void touch_check_deep_sleep(TouchAction action) {
    if (action == TOUCH_LONG) {
        #if DEBUG_SERIAL
        Serial.println("Long press detected! Going to Deep Sleep...");
        // Đợi 1 giây để người dùng nhả tay ra hẳn, tránh việc ESP32 vừa ngủ đã bị gọi dậy ngay
        delay(1000); 
        #endif
        
        // Cấu hình Wake up bằng chân Touch (GPIO3) ở mức HIGH
        esp_deep_sleep_enable_gpio_wakeup(1ULL << TOUCH_BUTTON_PIN, ESP_GPIO_WAKEUP_GPIO_HIGH);
        esp_deep_sleep_start();
    }
}