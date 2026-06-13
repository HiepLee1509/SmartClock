#include "display_ui.h"
#include <U8g2lib.h>
#include <Wire.h>
#include "sensor_health.h"
#include "sensor_env.h"
#include "network_manager.h"

U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);

// Mảng lưu trữ dữ liệu để vẽ biểu đồ PPG (128 điểm ảnh ngang)
#define GRAPH_WIDTH 128
static long ir_buffer[GRAPH_WIDTH] = {0};
static int ir_index = 0;

void display_init() {
    u8g2.begin();
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_ncenB08_tr); 
    u8g2.drawStr(15, 35, "SYSTEM BOOTING...");
    
    esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
    if (wakeup_reason == ESP_SLEEP_WAKEUP_GPIO) {
        u8g2.drawStr(20, 50, "Woke Up by Touch!");
    }
    u8g2.sendBuffer();
}

void display_poweroff() {
    u8g2.clearDisplay();  
    u8g2.setPowerSave(1); 
}

void display_show_sleep_msg() {
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_ncenB10_tr);
    u8g2.drawStr(25, 35, "SLEEPING...");
    u8g2.sendBuffer();
}

void display_update(ScreenState state) {
    u8g2.clearBuffer();
    
    // Vẽ Header chung
    u8g2.setFont(u8g2_font_ncenB08_tr);
    u8g2.drawStr(0, 10, "SMART HEALTH");
    u8g2.drawLine(0, 13, 128, 13); 

    // TRANG 1: ĐỒNG HỒ (HOME)
    if (state == SCREEN_CLOCK) {
        String timeStr = get_current_time();
        String dateStr = get_current_date();

        // Font giờ
        u8g2.setFont(u8g2_font_ncenB18_tr);
        // Do u8g2.drawStr yêu cầu const char*, ta dùng hàm .c_str() để ép kiểu
        u8g2.drawStr(15, 38, timeStr.c_str()); 
        
        // Font ngày
        u8g2.setFont(u8g2_font_ncenB08_tr);
        u8g2.drawStr(30, 58, dateStr.c_str());
    }
    // TRANG 2: MÔI TRƯỜNG KÝ TÚC XÁ
    else if (state == SCREEN_ENV) {
        u8g2.setFont(u8g2_font_ncenB12_tr);
        u8g2.setCursor(10, 35);
        u8g2.print("Temp: ");
        if (env_is_valid()) { u8g2.print(get_temp(), 1); u8g2.print(" C"); }
        else                { u8g2.print("--- C"); }

        u8g2.setCursor(10, 55);
        u8g2.print("Humi: ");
        if (env_is_valid()) { u8g2.print(get_humi(), 1); u8g2.print(" %"); }
        else                { u8g2.print("--- %"); }
    } 
    // TRANG 3: NHỊP TIM & SpO2
    else if (state == SCREEN_HEALTH) {
        long raw_ir = get_raw_ir();

        if (raw_ir < 50000) {
            // Trạng thái 1: Chưa đặt ngón tay
            u8g2.setFont(u8g2_font_ncenB08_tr);
            u8g2.drawStr(10, 40, "Put finger on sensor");
            for(int i = 0; i < GRAPH_WIDTH; i++) ir_buffer[i] = 0;
        } else {
            // Cập nhật buffer PPG để vẽ sóng (luôn làm dù đang warming up)
            ir_buffer[ir_index] = raw_ir;
            ir_index = (ir_index + 1) % GRAPH_WIDTH;

            u8g2.setFont(u8g2_font_ncenB12_tr);
            u8g2.setCursor(0, 32);
            u8g2.print("BPM:");

            if (health_is_ready()) {
                // Trạng thái 3: Đã đủ mẫu — hiện giá trị thực
                u8g2.print(get_bpm());

                u8g2.setCursor(80, 32);
                int spo2 = get_spo2();
                if (spo2 > 0) { u8g2.print(spo2); u8g2.print("%"); }
                else          { u8g2.print("---%"); }
            } else {
                // Trạng thái 2: Có ngón tay nhưng chưa đủ mẫu
                u8g2.print("---");
                u8g2.setCursor(80, 32);
                u8g2.print("---%");
            }

            // Vẽ sóng PPG
            long min_val = ir_buffer[0];
            long max_val = ir_buffer[0];
            for (int i = 1; i < GRAPH_WIDTH; i++) {
                if (ir_buffer[i] < min_val && ir_buffer[i] > 0) min_val = ir_buffer[i];
                if (ir_buffer[i] > max_val) max_val = ir_buffer[i];
            }

            if (max_val - min_val > 50) {
                int prev_y = 0;
                for (int i = 0; i < GRAPH_WIDTH; i++) {
                    int data_idx = (ir_index + i) % GRAPH_WIDTH;
                    if (ir_buffer[data_idx] == 0) continue;

                    int y = 63 - ((ir_buffer[data_idx] - min_val) * 28 / (max_val - min_val));

                    if (i > 0) {
                        u8g2.drawLine(i - 1, prev_y, i, y);
                    }
                    prev_y = y;
                }
            }
        }
    }

    u8g2.sendBuffer(); 
}

void display_show_temp_alert() {
    u8g2.clearBuffer();
    
    // Vẽ khung viền cảnh báo nhấp nháy
    bool blink = (millis() / 250) % 2 == 0;
    if (blink) {
        u8g2.drawFrame(0, 0, 128, 64);
        u8g2.drawFrame(2, 2, 124, 60);
    } else {
        u8g2.drawFrame(1, 1, 126, 62);
    }
    
    // Nhấp nháy to nhỏ chữ "ROOM TOO HOT!"
    bool showBig = (millis() / 300) % 2 == 0;
    if (showBig) {
        u8g2.setFont(u8g2_font_ncenB12_tr);
    } else {
        u8g2.setFont(u8g2_font_ncenB08_tr);
    }
    
    int strWidth = u8g2.getStrWidth("ROOM TOO HOT!");
    int x = (128 - strWidth) / 2;
    int y = 38; // Căn giữa theo trục Y (chiều cao oled 64px)
    
    u8g2.drawStr(x, y, "ROOM TOO HOT!");
    u8g2.sendBuffer();
}