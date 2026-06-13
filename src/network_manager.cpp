#include "network_manager.h"
#include <WiFi.h>
#include <time.h>
#include "config.h"

// Cấu hình Server thời gian và Múi giờ (UTC+7 = 7 * 3600 = 25200 giây)
const char* ntpServer1 = "time.google.com";      // Server của Google (rất nhanh ở VN)
const char* ntpServer2 = "vn.pool.ntp.org";      // Server pool khu vực Việt Nam
const char* ntpServer3 = "time.cloudflare.com";  // Server của Cloudflare
const long  gmtOffset_sec = 25200; 
const int   daylightOffset_sec = 0;

void network_init() {
    Serial.println("\n--- WIFI CONNECTING ---");
    
    // Reset cấu hình cũ
    WiFi.disconnect(true, true);
    delay(500);
    WiFi.mode(WIFI_STA);

    // Xóa cấu hình IP tĩnh cũ (nếu có) để nhận DHCP mới
    WiFi.config(INADDR_NONE, INADDR_NONE, INADDR_NONE);

    WiFi.begin(WIFI_SSID, WIFI_PASS);

    // Đặt công suất phát sóng Wi-Fi về 8.5dBm để tránh sụt áp và nhiễu phản xạ RF trên ESP32-C3 Super Mini
    WiFi.setTxPower(WIFI_POWER_8_5dBm);
    
    // Đợi kết nối Wi-Fi (Tối đa 15 giây)
    int timeout = 0;
    while (WiFi.status() != WL_CONNECTED && timeout < 30) {
        delay(500);
        Serial.print(".");
        timeout++;
    }
    
    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\nWi-Fi Connected!");
        Serial.print("IP Address: ");
        Serial.println(WiFi.localIP());
        
        // Cấu hình đồng bộ thời gian với 3 Server có độ ưu tiên giảm dần
        configTime(gmtOffset_sec, daylightOffset_sec, ntpServer1, ntpServer2, ntpServer3);
        
        Serial.print("NTP Syncing");
        
        // Đợi đồng bộ thời gian thực tế trong tối đa 5 giây (50 chu kỳ * 100ms)
        time_t now = time(nullptr);
        int ntp_retry = 0;
        while (now < 24 * 3600 && ntp_retry < 50) {
            delay(100);
            Serial.print(".");
            now = time(nullptr);
            ntp_retry++;
        }
        
        if (now > 24 * 3600) {
            Serial.println(" OK!");
        } else {
            Serial.println(" FAILED (will retry in background)");
        }
    } else {
        Serial.println("\nWi-Fi FAILED!");
    }
}

String get_current_time() {
    struct tm timeinfo;
    // Nếu chưa có giờ chuẩn (chưa đồng bộ được), trả về chuỗi mặc định
    if (!getLocalTime(&timeinfo, 10)) {
        return "--:--:--";
    }
    
    char timeStringBuff[10];
    strftime(timeStringBuff, sizeof(timeStringBuff), "%H:%M:%S", &timeinfo);
    return String(timeStringBuff);
}

String get_current_date() {
    struct tm timeinfo;
    if (!getLocalTime(&timeinfo, 10)) {
        return "--/--/----";
    }
    
    char dateStringBuff[15];
    // Định dạng Ngày/Tháng/Năm
    strftime(dateStringBuff, sizeof(dateStringBuff), "%d/%m/%Y", &timeinfo);
    return String(dateStringBuff);
}