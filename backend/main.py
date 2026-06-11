"""
Smart Health Clock - Backend Server
====================================
Tech Stack: FastAPI, paho-mqtt, SQLite3, joblib (mocked)

Luồng dữ liệu:
  1. ESP32 -> Adafruit IO (MQTT) -> Server subscribe -> Ghi SQLite
  2. Mobile App -> POST /api/profile -> Lưu RAM
  3. BPM mới đến -> Kết hợp với profile -> AI predict -> Publish ai_feedback
"""

import os
import sqlite3
import random
import logging
import threading
from datetime import datetime
from contextlib import asynccontextmanager

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Cấu hình Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Adafruit IO Credentials
AIO_USERNAME   = os.getenv("AIO_USERNAME", "hsLee1509")
AIO_ACTIVE_KEY = os.getenv("AIO_ACTIVE_KEY", "")

AIO_BROKER     = "io.adafruit.com"
AIO_PORT       = 1883

# Feed topics
FEED_TEMP     = f"{AIO_USERNAME}/feeds/temp"
FEED_HUMI     = f"{AIO_USERNAME}/feeds/humi"
FEED_BPM      = f"{AIO_USERNAME}/feeds/bpm"
FEED_SPO2     = f"{AIO_USERNAME}/feeds/spo2"
FEED_AI       = f"{AIO_USERNAME}/feeds/ai-feedback"

# Database SQLite
DB_PATH = "sensor_history.db"

def init_db():
    """Tạo bảng sensor_history nếu chưa tồn tại."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                temp      REAL,
                humi      REAL,
                bpm       REAL,
                spo2      REAL
            )
        """)
        conn.commit()
    logger.info("✅ SQLite database sẵn sàng: %s", DB_PATH)

def log_sensor_to_db(field: str, value: float):
    """
    Ghi một giá trị cảm biến vào DB.
    Mỗi message tạo một row mới với cột tương ứng được điền,
    các cột còn lại để NULL.
    """
    timestamp = datetime.now().isoformat()
    columns = {"temp": None, "humi": None, "bpm": None, "spo2": None}
    if field not in columns:
        logger.warning("⚠️  Field không hợp lệ: %s", field)
        return
    columns[field] = value
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sensor_history (timestamp, temp, humi, bpm, spo2) VALUES (?, ?, ?, ?, ?)",
            (timestamp, columns["temp"], columns["humi"], columns["bpm"], columns["spo2"]),
        )
        conn.commit()
    logger.info("💾 DB ← [%s] %s = %.2f", timestamp, field.upper(), value)

# User Profile (RAM - single-user system)
user_profile: dict | None = None
profile_lock = threading.Lock()

# AI Model Mock
def load_model(model_path: str = "model.pkl"):
    """
    Mock: Giả lập việc load file .pkl bằng joblib.
    Bỏ comment đoạn thật khi bạn có file model.pkl.
    """
    # --- KHI CÓ MODEL THẬT ---
    # import joblib
    # model = joblib.load(model_path)
    # return model
    # ------------------------------------

    logger.info("🤖 [MOCK] Đã load AI model từ '%s'", model_path)

    class MockModel:
        LABELS = ["Normal", "Fatigue", "Stress", "Hypertension Risk", "Bradycardia"]

        def predict(self, features: list) -> str:
            result = random.choice(self.LABELS)
            logger.info("🤖 [MOCK] Input features: %s → Prediction: %s", features, result)
            return result

    return MockModel()

# Khởi tạo model một lần duy nhất
ai_model = load_model()

def run_ai_inference(bpm: float) -> str | None:
    """
    Kết hợp BPM với user profile rồi chạy model.predict().
    Trả về None nếu profile chưa được cấu hình.
    """
    with profile_lock:
        profile = user_profile

    if profile is None:
        logger.error(
            "❌ AI Inference bị bỏ qua: Chưa nhận được profile người dùng qua POST /api/profile. "
            "Vui lòng gửi profile trước."
        )
        return None

    features = [
        bpm,
        profile["age"],
        profile["gender"],
        profile["weight"],
        profile["height"],
    ]
    prediction = ai_model.predict(features)
    return prediction

# MQTT Client
mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="smart_health_backend",
    protocol=mqtt.MQTTv311,
)

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logger.info("✅ MQTT kết nối thành công đến %s:%d", AIO_BROKER, AIO_PORT)
        client.subscribe(FEED_TEMP)
        client.subscribe(FEED_HUMI)
        client.subscribe(FEED_BPM)
        client.subscribe(FEED_SPO2)
        logger.info(
            "📡 Đã subscribe: temp | humi | bpm | spo2"
        )
    else:
        logger.error("❌ MQTT kết nối thất bại, reason_code=%s", reason_code)

def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code != 0:
        logger.warning("⚠️  MQTT ngắt kết nối bất ngờ (code=%s). Đang thử lại...", reason_code)

def on_message(client, userdata, msg):
    """
    Callback xử lý mọi message đến từ 4 feeds.
    - Ghi log + lưu vào SQLite.
    - Nếu là feed BPM: kích hoạt AI inference và publish kết quả.
    """
    topic = msg.topic
    try:
        value = float(msg.payload.decode("utf-8").strip())
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error("❌ Không parse được payload từ topic '%s': %s", topic, exc)
        return

    # Xác định field từ topic
    field_map = {
        FEED_TEMP: "temp",
        FEED_HUMI: "humi",
        FEED_BPM:  "bpm",
        FEED_SPO2: "spo2",
    }
    field = field_map.get(topic)
    if field is None:
        logger.warning("⚠️  Nhận được topic không xác định: %s", topic)
        return

    logger.info("📥 MQTT ← [%s] = %.2f", field.upper(), value)

    # Ghi vào SQLite
    log_sensor_to_db(field, value)

    # Kích hoạt AI chỉ khi nhận BPM
    if field == "bpm":
        prediction = run_ai_inference(bpm=value)
        if prediction is not None:
            client.publish(FEED_AI, prediction)
            logger.info("📤 MQTT → [ai_feedback] = '%s'", prediction)

def start_mqtt():
    """Cấu hình và khởi động MQTT client ở background."""
    mqtt_client.username_pw_set(AIO_USERNAME, AIO_ACTIVE_KEY)
    mqtt_client.on_connect    = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message    = on_message

    try:
        mqtt_client.connect(AIO_BROKER, AIO_PORT, keepalive=60)
        mqtt_client.loop_start()  # Non-blocking background thread
        logger.info("🚀 MQTT loop_start() đã chạy ở background.")
    except Exception as exc:
        logger.error("❌ Không thể kết nối MQTT: %s", exc)

# FastAPI App - Lifespan (thay thế on_event deprecated)
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Khởi động DB và MQTT khi server start."""
    logger.info("🔧 Khởi động Smart Health Backend...")
    init_db()
    start_mqtt()
    yield
    # Cleanup khi shutdown
    logger.info("🛑 Đang dừng MQTT client...")
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    logger.info("👋 Server đã tắt.")

app = FastAPI(
    title="Smart Health Clock - Backend API",
    description="Backend server nhận dữ liệu từ ESP32 qua Adafruit IO MQTT và cung cấp AI inference.",
    version="1.0.0",
    lifespan=lifespan,
)

# Pydantic Schema
class UserProfile(BaseModel):
    age:    int   = Field(..., ge=1, le=120, description="Tuổi người dùng (1-120)")
    gender: int   = Field(..., ge=0, le=1,   description="Giới tính: 0=Nữ, 1=Nam")
    weight: float = Field(..., gt=0,          description="Cân nặng (kg)")
    height: float = Field(..., gt=0,          description="Chiều cao (cm)")

# API Endpoints
@app.post("/api/profile", summary="Cập nhật hồ sơ người dùng")
async def update_profile(profile: UserProfile):
    """
    Nhận và lưu hồ sơ người dùng vào RAM.
    Dữ liệu này sẽ được kết hợp với BPM để chạy AI model.
    """
    global user_profile
    with profile_lock:
        user_profile = profile.model_dump()

    logger.info(
        "👤 Profile cập nhật: age=%d, gender=%d, weight=%.1fkg, height=%.1fcm",
        profile.age, profile.gender, profile.weight, profile.height,
    )
    return {
        "status": "success",
        "message": "Hồ sơ người dùng đã được lưu.",
        "profile": user_profile,
    }

@app.get("/api/profile", summary="Xem hồ sơ người dùng hiện tại")
async def get_profile():
    """Trả về profile đang lưu trong RAM (debug/kiểm tra)."""
    with profile_lock:
        if user_profile is None:
            raise HTTPException(status_code=404, detail="Chưa có profile nào được cấu hình.")
    return {"status": "success", "profile": user_profile}

@app.get("/api/history", summary="Xem lịch sử cảm biến gần nhất")
async def get_history(limit: int = 50):
    """Trả về `limit` bản ghi gần nhất từ SQLite."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM sensor_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"status": "success", "count": len(rows), "data": [dict(r) for r in rows]}

@app.get("/health", summary="Kiểm tra trạng thái server")
async def health_check():
    """Health check endpoint."""
    mqtt_connected = mqtt_client.is_connected()
    return {
        "status": "ok",
        "mqtt_connected": mqtt_connected,
        "profile_loaded": user_profile is not None,
        "timestamp": datetime.now().isoformat(),
    }

# Entry Point
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
