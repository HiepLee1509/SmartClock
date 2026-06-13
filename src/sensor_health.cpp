#include "sensor_health.h"
#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"
#include "spo2_algorithm.h"

MAX30105 particleSensor;

// --- BPM tracking (checkForBeat - real-time, tốt cho hiển thị từng nhịp) ---
const byte RATE_SIZE = 4;   // 4 mẫu: cân bằng giữa tốc độ phản hồi và độ ổn định
byte       rates[RATE_SIZE];
byte       rateSpot     = 0;
long       lastBeat     = 0;
byte       validCount   = 0; // Đếm số ô đã thực sự có dữ liệu (0..RATE_SIZE)

// --- SpO2 tracking (thuật toán Maxim - cần buffer đủ lớn) ---
#define SPO2_BUFFER_LEN 100  // Maxim yêu cầu FreqS*4 = 25*4 = 100 mẫu
static uint32_t irBuffer[SPO2_BUFFER_LEN];
static uint32_t redBuffer[SPO2_BUFFER_LEN];
static int  bufHead    = 0;   // Chỉ số vòng (circular)
static int  bufFilled  = 0;   // Số mẫu đã có trong buffer (tăng đến 100 rồi dừng)

// Bộ đếm để gọi thuật toán Maxim không quá thường xuyên (nặng CPU)
static unsigned long lastSpo2Calc = 0;
#define SPO2_CALC_INTERVAL_MS 2000 // Tính lại SpO2 mỗi 2 giây

// --- Kết quả trả về ngoài ---
static int  currentBPM  = 0;
static int  currentSpO2 = 0;
static long currentIR   = 0;
static bool healthReady = false; // true khi BPM đã ổn định (đủ mẫu)

// --- Giới hạn samples xử lý mỗi lần gọi health_update() ---
// Tránh vòng while(available) chạy quá lâu khi FIFO tích lũy nhiều
#define MAX_SAMPLES_PER_CALL 12

void health_init() {
    if (!particleSensor.begin(Wire, 400000)) {
        #if DEBUG_SERIAL
        Serial.println("MAX30102 was not found.");
        #endif
        return;
    }

    byte ledBrightness = 0x1F; // ~6.4mA
    byte sampleAverage = 4;
    byte ledMode       = 2;    // Red + IR
    int  sampleRate    = 400;
    int  pulseWidth    = 411;
    int  adcRange      = 4096;

    particleSensor.setup(ledBrightness, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange);
}

// =====================================================================
// Tính SpO2 bằng thuật toán Maxim từ buffer IR + Red tích lũy
// Chỉ gọi khi bufFilled >= SPO2_BUFFER_LEN (có đủ 100 mẫu)
// =====================================================================
static void _recalcSpO2() {
    int32_t spo2Val   = 0;
    int8_t  spo2Valid = 0;
    int32_t hrVal     = 0;
    int8_t  hrValid   = 0;

    // Sắp xếp buffer circular thành mảng tuyến tính trước khi truyền vào thuật toán
    uint32_t irLin[SPO2_BUFFER_LEN];
    uint32_t redLin[SPO2_BUFFER_LEN];
    for (int i = 0; i < SPO2_BUFFER_LEN; i++) {
        int idx   = (bufHead + i) % SPO2_BUFFER_LEN;
        irLin[i]  = irBuffer[idx];
        redLin[i] = redBuffer[idx];
    }

    maxim_heart_rate_and_oxygen_saturation(irLin, SPO2_BUFFER_LEN, redLin,
                                           &spo2Val, &spo2Valid,
                                           &hrVal,   &hrValid);

    if (spo2Valid && spo2Val > 80 && spo2Val <= 100) {
        currentSpO2 = (int)spo2Val;
    }
    // Nếu thuật toán Maxim cũng cho kết quả HR hợp lệ, dùng để cross-check
    // (không thay currentBPM vì checkForBeat nhanh hơn và tức thời hơn)
}

// =====================================================================
void health_update() {
    particleSensor.check();

    int processed = 0;

    while (particleSensor.available() && processed < MAX_SAMPLES_PER_CALL) {
        currentIR        = particleSensor.getFIFOIR();
        uint32_t rawRed  = particleSensor.getFIFORed();
        particleSensor.nextSample();
        processed++;

        // --- Không có ngón tay: reset toàn bộ trạng thái ---
        if (currentIR < 50000) {
            currentBPM  = 0;
            currentSpO2 = 0;
            healthReady = false;
            validCount  = 0;
            rateSpot    = 0;
            for (byte x = 0; x < RATE_SIZE; x++) rates[x] = 0;
            lastBeat   = millis();
            bufFilled  = 0;
            bufHead    = 0;
            continue;
        }

        // --- Đưa mẫu vào SpO2 circular buffer ---
        irBuffer[bufHead]  = (uint32_t)currentIR;
        redBuffer[bufHead] = rawRed;
        bufHead = (bufHead + 1) % SPO2_BUFFER_LEN;
        if (bufFilled < SPO2_BUFFER_LEN) bufFilled++;

        // --- Phát hiện nhịp tim ---
        if (checkForBeat(currentIR)) {
            long delta = millis() - lastBeat;
            lastBeat   = millis();

            if (delta > 300 && delta < 2000) {
                float bpm = 60000.0f / (float)delta;

                if (bpm > 40 && bpm < 220) {
                    rates[rateSpot] = (byte)bpm;
                    rateSpot        = (rateSpot + 1) % RATE_SIZE;
                    if (validCount < RATE_SIZE) validCount++;

                    // Hiển thị BPM sớm từ 2 mẫu trở lên (tăng dần độ chính xác)
                    if (validCount >= 2) {
                        int count = min((int)validCount, (int)RATE_SIZE);
                        int sum = 0;
                        for (byte x = 0; x < count; x++) sum += rates[x];
                        currentBPM  = sum / count;
                        healthReady = true;
                    }
                }
            } else if (delta >= 2000) {
                // Khoảng cách nhịp quá lớn → chỉ reset lastBeat, giữ dữ liệu cũ
                // (tránh mất toàn bộ tiến trình nếu ngón tay nhấc thoáng qua)
                lastBeat = millis();
            }
        }
    }

    // --- Tính lại SpO2 theo chu kỳ (không chặn loop) ---
    if (bufFilled >= SPO2_BUFFER_LEN &&
        millis() - lastSpo2Calc >= SPO2_CALC_INTERVAL_MS) {
        _recalcSpO2();
        lastSpo2Calc = millis();
    }
}

int  get_bpm()        { return currentBPM; }
int  get_spo2()       { return currentSpO2; }
long get_raw_ir()     { return currentIR; }
bool health_is_ready(){ return healthReady; }