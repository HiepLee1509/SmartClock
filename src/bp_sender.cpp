#include "bp_sender.h"
#include "config.h"
#include <WiFi.h>
#include <HTTPClient.h>

// ─── Trạng thái nội bộ ──────────────────────────────────────────────────────

// Buffer IR: dùng bộ đệm kép (snapshot) để task HTTP không race với loop chính
static float   irBuffer[IR_BUFFER_SIZE];      // Buffer gom mẫu (trong loop)
static float   irSnapshot[IR_BUFFER_SIZE];    // Bản sao gửi cho FreeRTOS task
static int     snapshotSize  = 0;

static int     irBufferIndex = 0;
static bool    isCollecting  = false;
static String  lastResult    = "";
static String  backendEndpoint = "";
static unsigned long lastSendTime = 0;

// ─── FreeRTOS task handle ────────────────────────────────────────────────────
// isSending: cờ ngăn tạo task mới khi task cũ chưa xong
static volatile bool isSending = false;

// ─── Task FreeRTOS: chạy HTTP POST ở core 0, không block loop ───────────────
static void httpPostTask(void* pvParameters) {
    // Làm việc trên irSnapshot (bản sao đã chép trước khi task được tạo)
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[BP] WiFi not connected, skip send.");
        isSending = false;
        vTaskDelete(NULL);
        return;
    }

    // Xây dựng JSON payload
    String payload = "{\"ir_data\":[";
    for (int i = 0; i < snapshotSize; i++) {
        if (i > 0) payload += ",";
        payload += String(irSnapshot[i], 2);
    }
    payload += "],\"sample_rate\":";
    payload += String(IR_SAMPLE_RATE);
    payload += "}";

    Serial.printf("[BP] Task: Sending %d samples (%d bytes)\n",
                  snapshotSize, payload.length());

    HTTPClient http;
    http.begin(backendEndpoint);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(8000);  // 8 giây timeout

    int httpCode = http.POST(payload);

    if (httpCode == 200) {
        String response = http.getString();
        Serial.printf("[BP] Response: %s\n", response.c_str());

        // Parse "prediction":"..."
        int predIdx = response.indexOf("\"prediction\":\"");
        if (predIdx >= 0) {
            predIdx += 14;
            int predEnd = response.indexOf("\"", predIdx);
            if (predEnd > predIdx) {
                lastResult = response.substring(predIdx, predEnd);
                Serial.printf("[BP] AI Prediction: %s\n", lastResult.c_str());
            }
        }
    } else {
        Serial.printf("[BP] HTTP POST failed, code: %d\n", httpCode);
    }

    http.end();
    lastSendTime = millis();
    isSending = false;          // Đánh dấu task đã xong
    vTaskDelete(NULL);          // Tự xóa task
}

// ─── Kích hoạt gửi: chép buffer → snapshot rồi spawn task ──────────────────
static void triggerSendAsync() {
    if (isSending) {
        Serial.println("[BP] Previous send still in progress, skip.");
        return;
    }

    // Chép buffer vào snapshot (thao tác nhanh, xong ngay trong loop)
    snapshotSize = irBufferIndex;
    memcpy(irSnapshot, irBuffer, snapshotSize * sizeof(float));

    isSending = true;

    // Tạo FreeRTOS task trên Core 0 (Arduino/WiFi chạy Core 1)
    // Stack 8KB đủ cho HTTPClient + String payload ~4KB
    xTaskCreatePinnedToCore(
        httpPostTask,    // Hàm task
        "bp_http_post", // Tên task (debug)
        8192,           // Stack size (bytes)
        NULL,           // Tham số truyền vào (không cần)
        1,              // Priority (thấp, không tranh CPU với loop)
        NULL,           // Task handle (không cần lưu)
        0               // Core 0 (để loop/display chạy thoải mái ở Core 1)
    );

    Serial.println("[BP] HTTP POST task spawned on Core 0.");
}

// ─── API Functions ──────────────────────────────────────────────────────────

void bp_sender_init(const char* backendUrl) {
    backendEndpoint = String(backendUrl);
    irBufferIndex = 0;
    isCollecting  = false;
    isSending     = false;
    lastResult    = "";
    lastSendTime  = 0;
    Serial.printf("[BP] Initialized. Endpoint: %s\n", backendEndpoint.c_str());
    Serial.printf("[BP] Buffer: %d samples (%ds @ %dHz), Cooldown: %ds\n",
                  IR_BUFFER_SIZE, IR_BUFFER_SECONDS, IR_SAMPLE_RATE,
                  BP_SEND_COOLDOWN_MS / 1000);
}

void bp_sender_update(long rawIR) {
    bool hasFinger = (rawIR > 50000);

    // ── Không có ngón tay → reset ────────────────────────────────────────
    if (!hasFinger) {
        if (isCollecting) {
            Serial.printf("[BP] Finger removed. Discarding %d samples.\n",
                          irBufferIndex);
            isCollecting  = false;
            irBufferIndex = 0;
        }
        return;
    }

    // ── Đang gửi hoặc trong cooldown → không thu mẫu mới ────────────────
    if (isSending) return;
    if (lastSendTime > 0 && (millis() - lastSendTime < BP_SEND_COOLDOWN_MS)) return;

    // ── Bắt đầu thu thập ─────────────────────────────────────────────────
    if (!isCollecting) {
        isCollecting  = true;
        irBufferIndex = 0;
        Serial.println("[BP] Started collecting IR samples...");
    }

    // ── Thêm mẫu (QUY ĐỊNH: chia 64) ─────────────────────────────────────
    if (irBufferIndex < IR_BUFFER_SIZE) {
        irBuffer[irBufferIndex++] = (float)rawIR / 64.0f;

        if (irBufferIndex % 100 == 0) {
            Serial.printf("[BP] Collecting: %d/%d\n", irBufferIndex, IR_BUFFER_SIZE);
        }
    }

    // ── Buffer đầy → kích hoạt gửi bất đồng bộ ──────────────────────────
    if (irBufferIndex >= IR_BUFFER_SIZE) {
        Serial.printf("[BP] Buffer full. Triggering async send...\n");
        triggerSendAsync();   // Không block — tạo task rồi return ngay
        isCollecting  = false;
        irBufferIndex = 0;
    }
}

String bp_get_last_result() { return lastResult; }
bool   bp_is_collecting()   { return isCollecting; }
bool   bp_is_sending()      { return isSending; }
