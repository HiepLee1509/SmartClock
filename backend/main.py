"""
Smart Health Clock - Backend Server
====================================
Tech Stack: FastAPI, paho-mqtt, SQLite3, google-generativeai, PyTorch

Luồng dữ liệu:
  1. ESP32 -> Adafruit IO (MQTT) -> Server subscribe -> Ghi SQLite
  2. Mobile App -> POST /api/profile -> Lưu RAM + SQLite
  3. ESP32 -> HTTP POST /api/predict-bp -> AI TorchScript inference -> Publish ai_feedback
  4. Mobile App -> POST /api/chat -> Gemini AI Cardiologist -> Trả lời
"""
import os
import json
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

import numpy as np
import torch
from scipy.signal import butter, filtfilt, find_peaks, resample

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from google import genai
from google.genai import types

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

# Gemini AI
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-2.5-flash"  # Model có quota trên project này (5 RPM free tier)

SYSTEM_PROMPT = (
    "Bạn là bác sĩ chuyên khoa tim mạch giỏi, tên là Dr. AI. "
    "Bạn sẽ được cung cấp dữ liệu sức khỏe thực tế từ các cảm biến và thông tin cá nhân của bệnh nhân. "
    "Hãy đưa ra những lời khuyên chính xác, hữu ích, dễ hiểu bằng tiếng Việt. "
    "Luôn dựa trên dữ liệu được cung cấp. Trả lời ngắn gọn, đi thẳng vào vấn đề. "
    "Nếu dữ liệu không đủ, hãy nói rõ để người dùng biết."
)

if GEMINI_API_KEY and GEMINI_API_KEY != "PASTE_YOUR_GEMINI_KEY_HERE":
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    logger.info("✅ Gemini AI client đã khởi tạo (model: %s)", GEMINI_MODEL)
else:
    gemini_client = None
    logger.warning("⚠️  GEMINI_API_KEY chưa được cấu hình. Endpoint /api/chat sẽ bị tắt.")

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
    """Tạo bảng sensor_history và user_profile nếu chưa tồn tại."""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                id        INTEGER PRIMARY KEY CHECK (id = 1),
                age       INTEGER NOT NULL,
                gender    INTEGER NOT NULL,
                weight    REAL    NOT NULL,
                height    REAL    NOT NULL,
                updated_at TEXT   NOT NULL
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

def save_profile_to_db(profile: dict):
    """Lưu profile vào SQLite (UPSERT vào row duy nhất id=1)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO user_profile (id, age, gender, weight, height, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                age        = excluded.age,
                gender     = excluded.gender,
                weight     = excluded.weight,
                height     = excluded.height,
                updated_at = excluded.updated_at
            """,
            (profile["age"], profile["gender"], profile["weight"],
             profile["height"], datetime.now().isoformat()),
        )
        conn.commit()
    logger.info("💾 DB ← Profile saved: age=%d, gender=%d, weight=%.1f, height=%.1f",
                profile["age"], profile["gender"], profile["weight"], profile["height"])

def load_profile_from_db() -> dict | None:
    """Đọc profile từ SQLite khi server khởi động. Trả về None nếu chưa có."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if row:
        profile = {"age": row["age"], "gender": row["gender"],
                   "weight": row["weight"], "height": row["height"]}
        logger.info("📂 Profile loaded from DB: %s", profile)
        return profile
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  AI Blood Pressure Model (TorchScript CNN)
# ══════════════════════════════════════════════════════════════════════════════

# Đường dẫn tới model và metadata (nằm trong thư mục bidmc cùng cấp với backend)
BIDMC_DIR = Path(__file__).resolve().parent.parent / "bidmc"
MODEL_PATH = BIDMC_DIR / "model_cnn2_scripted.pt"
META_PATH  = BIDMC_DIR / "model_cnn2_meta.json"

# Load metadata (fs gốc của training, beat_len, class_names)
try:
    with open(META_PATH) as f:
        model_meta = json.load(f)
    MODEL_BEAT_LEN   = model_meta["beat_len"]    # 256
    MODEL_TRAIN_FS   = model_meta["fs"]          # 1000 (chỉ để tham khảo, KHOONG dùng cho filter)
    MODEL_CLASSES    = model_meta["class_names"]  # ["Hypertension", "Normal", "Prehypertension"]
    logger.info("🧠 Model metadata loaded: beat_len=%d, train_fs=%d, classes=%s",
                MODEL_BEAT_LEN, MODEL_TRAIN_FS, MODEL_CLASSES)
except Exception as exc:
    logger.error("❌ Không load được model metadata: %s", exc)
    model_meta = None
    MODEL_BEAT_LEN = 256
    MODEL_TRAIN_FS = 1000
    MODEL_CLASSES  = ["Hypertension", "Normal", "Prehypertension"]

# Load TorchScript model
try:
    bp_model = torch.jit.load(str(MODEL_PATH), map_location="cpu").eval()
    logger.info("✅ TorchScript model loaded: %s", MODEL_PATH)
except Exception as exc:
    bp_model = None
    logger.error("❌ Không load được TorchScript model: %s", exc)


def preprocess_and_predict(ir_data: list[float], sample_rate: float) -> str | None:
    """
    Tiền xử lý tín hiệu IR từ MAX30102 và chạy AI inference.

    QUAN TRỌNG: Tham số bộ lọc được tính toán dựa trên `sample_rate` thực tế
    (từ ESP32 gửi lên), KHÔNG dùng FS=1000 của training.

    Pipeline:
      1. Bandpass filter [0.5 - 8.0] Hz (Nyquist = sample_rate / 2)
      2. Z-score normalize
      3. Find R-peaks với distance tự động theo sample_rate
      4. Cắt từng nhịp tim (beat segmentation)
      5. Resample mỗi beat về BEAT_LEN=256
      6. Normalize mỗi beat
      7. Stack vào batch, chạy model, lấy softmax trung bình
    """
    if bp_model is None:
        logger.error("❌ Model chưa được load, bỏ qua inference.")
        return None

    signal_1d = np.array(ir_data, dtype=np.float64)

    if len(signal_1d) < 50:
        logger.warning("⚠️  Tín hiệu quá ngắn (%d samples), bỏ qua.", len(signal_1d))
        return None

    # ── 1. Bandpass Filter (điều chỉnh Nyquist theo sample_rate thực tế) ────
    nyquist = sample_rate / 2.0
    low_cut  = 0.5   # Hz
    high_cut = 8.0   # Hz

    # Đảm bảo dải lọc nằm trong giới hạn hợp lệ (0, 1) khi chuyen thành Wn
    if high_cut >= nyquist:
        high_cut = nyquist * 0.95  # Clíp xuống 95% Nyquist để tránh lỗi
        logger.warning("⚠️  high_cut đã được clip xuống %.2f Hz (Nyquist=%.1f Hz)",
                        high_cut, nyquist)

    wn_low  = low_cut  / nyquist
    wn_high = high_cut / nyquist

    logger.info("📡 Preprocessing: FS=%.0f Hz, Nyquist=%.1f Hz, Wn=[%.4f, %.4f]",
                sample_rate, nyquist, wn_low, wn_high)

    b, a = butter(3, [wn_low, wn_high], btype="band")
    filtered = filtfilt(b, a, signal_1d)

    # ── 2. Normalize ─────────────────────────────────────────────────────
    norm = (filtered - filtered.mean()) / (filtered.std() + 1e-8)

    # ── 3. Find Peaks (distance thích ứng với sample_rate) ──────────────
    # Khoảng cách tối thiểu giữa 2 peak = 0.4 giây (tương đương 150 BPM max)
    min_distance = int(sample_rate * 0.4)
    if min_distance < 1:
        min_distance = 1

    peaks, _ = find_peaks(norm, distance=min_distance, prominence=0.4)
    logger.info("🔍 Tìm được %d peaks (distance=%d samples)", len(peaks), min_distance)

    if len(peaks) < 2:
        logger.warning("⚠️  Không đủ peaks để cắt nhịp tim (%d peaks).", len(peaks))
        return None

    # ── 4. Cắt từng nhịp tim (beat segmentation) ───────────────────────
    beats = []
    for i in range(len(peaks) - 1):
        # Tính onset: điểm giữa peak trước và peak hiện tại
        if i > 0:
            onset = (peaks[i - 1] + peaks[i]) // 2
        else:
            onset = max(0, peaks[i] - (peaks[i + 1] - peaks[i]) // 2)
        # Tính offset: điểm giữa peak hiện tại và peak tiếp theo
        offset = (peaks[i] + peaks[i + 1]) // 2

        seg = filtered[onset:offset]
        if len(seg) < 30:
            continue

        # Resample về đúng BEAT_LEN=256 (giữ nguyên logic từ training)
        seg = resample(seg, MODEL_BEAT_LEN).astype(np.float32)
        seg = (seg - seg.mean()) / (seg.std() + 1e-8)
        beats.append(seg)

    logger.info("💓 Cắt được %d beats hợp lệ từ %d peaks.", len(beats), len(peaks))

    if not beats:
        logger.warning("⚠️  Không cắt được beat nào hợp lệ.")
        return None

    # ── 5. Inference ─────────────────────────────────────────────────────
    x = torch.tensor(np.stack(beats)).unsqueeze(1)  # (N, 1, 256)

    with torch.no_grad():
        logits = bp_model(x)
        probs  = torch.softmax(logits, dim=1).mean(0)  # Trung bình softmax của tất cả beats

    predicted_idx   = probs.argmax().item()
    predicted_label = MODEL_CLASSES[predicted_idx]
    confidence      = probs[predicted_idx].item() * 100

    logger.info("🎯 AI Prediction: %s (confidence: %.1f%%)", predicted_label, confidence)
    logger.info("📊 Probabilities: %s",
                {c: f"{p:.3f}" for c, p in zip(MODEL_CLASSES, probs.tolist())})

    return predicted_label

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

    # (AI inference cho huyết áp đã chuyển sang endpoint /api/predict-bp qua HTTP POST)

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
    global user_profile
    logger.info("🔧 Khởi động Smart Health Backend...")
    init_db()
    # Khôi phục profile từ DB vào RAM (nếu đã từng lưu)
    with profile_lock:
        user_profile = load_profile_from_db()
    if user_profile:
        logger.info("✅ Profile đã được khôi phục từ DB.")
    else:
        logger.info("ℹ️  Chưa có profile nào trong DB.")
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

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Câu hỏi của người dùng")
    ai_feedback: str | None = Field(None, description="Kết quả từ AI model (nếu có)")

class IRDataRequest(BaseModel):
    """Payload từ ESP32: mảng IR (đã chia 64) + tần số lấy mẫu thực tế."""
    ir_data: list[float] = Field(..., min_length=50, description="Mảng dữ liệu IR (đã chia 64), 300-500 phần tử")
    sample_rate: float   = Field(..., gt=0,           description="Tần số lấy mẫu thực tế (Hz), ví dụ 100")

def get_latest_sensor_data() -> dict:
    """Lấy dữ liệu cảm biến mới nhất cho mỗi loại từ SQLite."""
    result = {"bpm": None, "spo2": None, "temp": None, "humi": None}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for field in result:
            row = conn.execute(
                f"SELECT {field}, timestamp FROM sensor_history "
                f"WHERE {field} IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                result[field] = {"value": row[field], "timestamp": row["timestamp"]}
    return result

# API Endpoints
@app.post("/api/profile", summary="Cập nhật hồ sơ người dùng")
async def update_profile(profile: UserProfile):
    """
    Nhận và lưu hồ sơ người dùng vào RAM và SQLite.
    Dữ liệu này sẽ được kết hợp với BPM để chạy AI model.
    """
    global user_profile
    data = profile.model_dump()
    with profile_lock:
        user_profile = data
    # Lưu bền vững vào SQLite
    save_profile_to_db(data)

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
        "bp_model_loaded": bp_model is not None,
        "timestamp": datetime.now().isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  API Endpoint: Nhận mảng IR từ ESP32 và dự đoán huyết áp
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/predict-bp", summary="Dự đoán huyết áp từ tín hiệu IR")
async def predict_blood_pressure(request: IRDataRequest):
    """
    Nhận mảng dữ liệu IR từ ESP32-C3 (qua HTTP POST), tiền xử lý tín hiệu
    với tham số filter/peak tự thích ứng theo sample_rate thực tế,
    chạy TorchScript model và trả kết quả.

    Kết quả đồng thời được publish lên Adafruit IO feed "ai-feedback".
    """
    if bp_model is None:
        raise HTTPException(
            status_code=503,
            detail="AI model chưa được load. Kiểm tra file model_cnn2_scripted.pt."
        )

    logger.info("📥 Nhận %d mẫu IR từ ESP32 (sample_rate=%.0f Hz)",
                len(request.ir_data), request.sample_rate)

    # Chạy tiền xử lý + inference
    prediction = preprocess_and_predict(request.ir_data, request.sample_rate)

    if prediction is None:
        raise HTTPException(
            status_code=422,
            detail="Không thể xử lý tín hiệu IR. Có thể tín hiệu quá nhiễu, "
                   "không tìm thấy nhịp tim hoặc chất lượng dữ liệu kém."
        )

    # Publish kết quả lên Adafruit IO feed ai-feedback
    if mqtt_client.is_connected():
        mqtt_client.publish(FEED_AI, prediction)
        logger.info("📤 MQTT → [ai-feedback] = '%s'", prediction)
    else:
        logger.warning("⚠️  MQTT chưa kết nối, không thể publish ai-feedback.")

    return {
        "status": "success",
        "prediction": prediction,
        "samples_received": len(request.ir_data),
        "sample_rate": request.sample_rate,
    }

@app.post("/api/chat", summary="Hỏi bác sĩ AI tim mạch")
async def chat_with_ai(request: ChatRequest):
    """
    Nhận câu hỏi từ người dùng, xây dựng context từ dữ liệu cảm biến
    và profile, sau đó gọi Gemini AI để trả lời như một bác sĩ tim mạch.
    """
    if gemini_client is None:
        raise HTTPException(
            status_code=503,
            detail="Gemini AI chưa được cấu hình. Vui lòng thêm GEMINI_API_KEY vào file .env."
        )

    # Lấy dữ liệu thực tế
    sensor_data = get_latest_sensor_data()
    with profile_lock:
        profile = user_profile

    # --- Xây dựng context cho Gemini ---
    profile_text = "Chưa có thông tin cá nhân." if profile is None else (
        f"- Tuổi: {profile['age']}\n"
        f"- Giới tính: {'Nam' if profile['gender'] == 1 else 'Nữ'}\n"
        f"- Cân nặng: {profile['weight']} kg\n"
        f"- Chiều cao: {profile['height']} cm\n"
        f"- BMI: {profile['weight'] / (profile['height'] / 100) ** 2:.1f}"
    )

    def fmt(field_data):
        if field_data is None:
            return "Không có dữ liệu"
        return f"{field_data['value']:.1f} (lúc {field_data['timestamp'][:19]})"

    sensor_text = (
        f"- Nhịp tim (BPM): {fmt(sensor_data['bpm'])}\n"
        f"- SpO2: {fmt(sensor_data['spo2'])}%\n"
        f"- Nhiệt độ phòng: {fmt(sensor_data['temp'])}°C\n"
        f"- Độ ẩm phòng: {fmt(sensor_data['humi'])}%"
    )

    ai_text = (
        f"- Nhận định từ AI model: {request.ai_feedback}"
        if request.ai_feedback else "- Không có nhận định từ AI model."
    )

    context_prompt = (
        f"=== THÔNG TIN BỆNH NHÂN ===\n{profile_text}\n\n"
        f"=== DỮ LIỆU CẢM BIẾN MỚI NHẤT ===\n{sensor_text}\n\n"
        f"=== NHẬN ĐỊNH AI ===\n{ai_text}\n\n"
        f"=== CÂU HỎI CỦA BỆNH NHÂN ===\n{request.message}"
    )

    logger.info("🤖 Gọi Gemini AI với câu hỏi: '%s'", request.message)

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=context_prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.7,
            ),
        )
        answer = response.text
        logger.info("✅ Gemini phản hồi thành công (%d ký tự)", len(answer))
        return {
            "status": "success",
            "question": request.message,
            "answer": answer,
        }
    except Exception as exc:
        logger.error("❌ Gemini API lỗi: %s", exc)
        raise HTTPException(status_code=500, detail=f"Gemini API lỗi: {exc}")


# Entry Point
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
