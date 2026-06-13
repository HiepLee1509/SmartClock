#ifndef BP_SENDER_H
#define BP_SENDER_H

#include <Arduino.h>

// ─── Cấu hình IR Buffer cho AI Blood Pressure ───────────────────────────────
// Effective Sample Rate = sampleRate / sampleAverage = 400 / 4 = 100 Hz
// Buffer 4 giây ở 100Hz = 400 mẫu (nằm trong khoảng 300-500)
#define IR_SAMPLE_RATE      100       // Hz (tần số lấy mẫu thực tế)
#define IR_BUFFER_SECONDS   4         // Thu 4 giây dữ liệu
#define IR_BUFFER_SIZE      (IR_SAMPLE_RATE * IR_BUFFER_SECONDS)  // = 400

// Cooldown giữa các lần gửi (tránh spam backend) — 30 giây
#define BP_SEND_COOLDOWN_MS 30000

// ─── Hàm API ────────────────────────────────────────────────────────────────
// Gọi trong setup() sau khi WiFi đã kết nối
void bp_sender_init(const char* backendUrl);

// Gọi trong loop() — tự động gom mẫu IR khi có ngón tay, gửi khi đủ buffer
void bp_sender_update(long rawIR);

// Trả về kết quả AI gần nhất (hoặc "" nếu chưa có)
String bp_get_last_result();

// Trả về true nếu đang trong quá trình gom mẫu
bool bp_is_collecting();

// Trả về true nếu HTTP POST đang chạy trên background task
bool bp_is_sending();

#endif // BP_SENDER_H
