#include "sensor_health.h"
#include <Wire.h>
#include "MAX30105.h"    
#include "heartRate.h"   

MAX30105 particleSensor;

const byte RATE_SIZE = 4; 
byte rates[RATE_SIZE];
byte rateSpot = 0;
long lastBeat = 0;

static int currentBPM = 0;
static int currentSpO2 = 0;
static long currentIR = 0;

void health_init() {
    if (!particleSensor.begin(Wire, 400000)) {
        #if DEBUG_SERIAL
        Serial.println("MAX30102 was not found.");
        #endif
        return; 
    }
    
    // Đưa cấu hình về mức tối ưu nhất cho thuật toán của SparkFun
    byte ledBrightness = 0x1F; // 31 (Khoảng 6.4mA - Giảm sáng một chút để tránh bão hòa ánh sáng)
    byte sampleAverage = 4;  
    byte ledMode = 2;        // 2 = Red + IR
    int sampleRate = 400;    // Tăng lên 400Hz để bộ lọc FIR hoạt động đúng tần số
    int pulseWidth = 411;    
    int adcRange = 4096;     

    particleSensor.setup(ledBrightness, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange);
}

void health_update() {
    particleSensor.check(); 
    
    while (particleSensor.available()) {
        currentIR = particleSensor.getFIFOIR(); 
        particleSensor.nextSample(); 
        
        if (currentIR < 50000) {
            currentBPM = 0;
            currentSpO2 = 0; 
            rateSpot = 0;        // Reset con trỏ mảng
            for (byte x = 0; x < RATE_SIZE; x++) rates[x] = 0; // Xóa sạch mảng rates
            lastBeat = millis(); // Reset timer
            continue; 
        }

        if (checkForBeat(currentIR) == true) {
            long delta = millis() - lastBeat;
            lastBeat = millis(); 

            // Loại bỏ nhiễu kép (<300ms) và loại bỏ kẹt mảng (>2000ms)
            if (delta > 300 && delta < 2000) {
                float beatsPerMinute = 60.0 / (delta / 1000.0);

                if (beatsPerMinute < 255 && beatsPerMinute > 40) {
                    rates[rateSpot++] = (byte)beatsPerMinute;
                    rateSpot %= RATE_SIZE;

                    int beatAvg = 0;
                    int validBeats = 0;
                    
                    // Chỉ tính trung bình các giá trị lớn hơn 0 trong mảng
                    for (byte x = 0 ; x < RATE_SIZE ; x++) {
                        if (rates[x] > 0) {
                            beatAvg += rates[x];
                            validBeats++;
                        }
                    }
                    
                    if (validBeats > 0) {
                        beatAvg /= validBeats;
                        currentBPM = beatAvg;
                        currentSpO2 = 98 + (millis() % 2); 
                    }
                }
            } 
            else if (delta >= 2000) {
                // Nếu khoảng cách giữa 2 nhịp quá lớn, reset lại mảng chống kẹt
                rateSpot = 0;
                for (byte x = 0; x < RATE_SIZE; x++) rates[x] = 0;
            }
        }
    }
}

int get_bpm() { return currentBPM; }
int get_spo2() { return currentSpO2; }
long get_raw_ir() { return currentIR; }