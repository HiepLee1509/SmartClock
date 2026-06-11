"""
Beat-level 1D CNN — waveform only, no demographics.
Pipeline:
  1. Extract all beats from all subjects (all 3 recordings each)
  2. SMOTE on the full beat pool → ~1000 per class
  3. Random 80/20 train/test split on the oversampled pool
  4. Train CNN, evaluate on test set
"""

import os
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, TensorDataset
from torch.optim.lr_scheduler import CosineAnnealingLR

from scipy.signal import butter, filtfilt, find_peaks, resample as sp_resample
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix
from imblearn.over_sampling import SMOTE

# ── Config ────────────────────────────────────────────────────────────────────

FS            = 1000
BEAT_LEN      = 256
TARGET_N      = 2000   # samples per class after SMOTE
BATCH_SIZE    = 32
EPOCHS        = 1000
LR            = 2e-4
PATIENCE      = 40
DEVICE        = torch.device("cuda" if torch.cuda.is_available() else "cpu")
SEED          = 42

torch.manual_seed(SEED)
np.random.seed(SEED)

# ── Signal helpers ────────────────────────────────────────────────────────────

def bandpass(sig, lo=0.5, hi=8.0, fs=FS):
    b, a = butter(3, [lo / (fs / 2), hi / (fs / 2)], btype="band")
    return filtfilt(b, a, sig)

def load_signal(path):
    with open(path) as f:
        parts = f.readline().strip().split("\t")
    return np.array([float(x) for x in parts[1:]], dtype=np.float64)

def extract_beats(sig, fs=FS):
    filtered = bandpass(sig)
    norm = (filtered - filtered.mean()) / (filtered.std() + 1e-8)
    min_dist = int(fs * 0.4)
    peaks, _ = find_peaks(norm, distance=min_dist, prominence=0.4)
    beats = []
    for i in range(len(peaks) - 1):
        onset  = (peaks[i-1] + peaks[i]) // 2 if i > 0 else max(0, peaks[i] - (peaks[i+1] - peaks[i]) // 2)
        offset = (peaks[i] + peaks[i+1]) // 2
        seg = filtered[onset:offset]
        if len(seg) < 30:
            continue
        seg = sp_resample(seg, BEAT_LEN).astype(np.float32)
        seg = (seg - seg.mean()) / (seg.std() + 1e-8)
        beats.append(seg)
    return beats

# ── Load metadata ─────────────────────────────────────────────────────────────

meta = pd.read_excel("PPG-BP dataset.xlsx", sheet_name="cardiovascular dataset", header=1)
meta.columns = [
    "num", "subject_id", "sex", "age", "height", "weight",
    "sbp", "dbp", "heart_rate", "bmi",
    "hypertension", "diabetes", "cerebral_infarction", "cerebrovascular_disease",
]
meta = meta[meta["sex"] != "Sex(M/F)"].copy()
meta = meta.dropna(subset=["hypertension"])
meta["hypertension"] = meta["hypertension"].replace(
    {"Hypertension":        "Hypertension",
     "Stage 1 hypertension":"Hypertension",
     "Stage 2 hypertension":"Hypertension"}
)
meta["subject_id"] = meta["subject_id"].astype(int)

le = LabelEncoder()
meta["label"] = le.fit_transform(meta["hypertension"])
class_names = le.classes_
label_map   = dict(zip(meta["subject_id"], meta["label"]))

# ── Step 1: extract all beats from every subject ──────────────────────────────

print("Extracting beats from all subjects...")
all_beats, all_labels = [], []

for _, row in meta.iterrows():
    sid   = int(row["subject_id"])
    label = int(row["label"])
    for trial in [1, 2, 3]:
        path = f"0_subject/{sid}_{trial}.txt"
        if not os.path.exists(path):
            continue
        sig = load_signal(path)
        for beat in extract_beats(sig):
            all_beats.append(beat)
            all_labels.append(label)

X = np.array(all_beats)
y = np.array(all_labels)

counts_before = dict(zip(*np.unique(y, return_counts=True)))
print(f"Raw beats: {len(X)}")
print(f"  Per class: { {class_names[k]: v for k, v in counts_before.items()} }")

# ── Step 2: SMOTE to TARGET_N per class ──────────────────────────────────────

print(f"\nApplying SMOTE -> {TARGET_N} per class...")
sm = SMOTE(sampling_strategy={c: TARGET_N for c in np.unique(y)},
           random_state=SEED,
           k_neighbors=5)
X_res, y_res = sm.fit_resample(X, y)

counts_after = dict(zip(*np.unique(y_res, return_counts=True)))
new_total    = len(X_res)
print(f"After SMOTE: {new_total} beats")
print(f"  Per class: { {class_names[k]: v for k, v in counts_after.items()} }")
new_synthetic = new_total - len(X)
print(f"  Synthetic beats added: {new_synthetic} ({100*new_synthetic/new_total:.1f}% of pool)\n")

# ── Step 3: train / test split on the oversampled pool ───────────────────────

X_train, X_test, y_train, y_test = train_test_split(
    X_res, y_res, test_size=0.2, random_state=SEED, stratify=y_res
)
print(f"Train beats: {len(X_train)}  |  Test beats: {len(X_test)}")
print(f"  Train per class: { dict(zip(*np.unique(y_train, return_counts=True))) }")
print(f"  Test  per class: { dict(zip(*np.unique(y_test,  return_counts=True))) }\n")

# ── Dataset / loaders ─────────────────────────────────────────────────────────

def make_loader(X, y, shuffle, augment=False):
    X_t = torch.tensor(X).unsqueeze(1)   # (N, 1, BEAT_LEN)
    y_t = torch.tensor(y, dtype=torch.long)

    class AugDataset(Dataset):
        def __init__(self):
            self.X = X_t
            self.y = y_t
        def __len__(self):
            return len(self.y)
        def __getitem__(self, i):
            x = self.X[i].clone()
            if augment:
                x += torch.randn_like(x) * 0.05
                x *= np.random.uniform(0.9, 1.1)
                x  = torch.roll(x, np.random.randint(-10, 10), dims=-1)
            return x, self.y[i]

    return DataLoader(AugDataset(), batch_size=BATCH_SIZE, shuffle=shuffle)

train_dl = make_loader(X_train, y_train, shuffle=True,  augment=True)
test_dl  = make_loader(X_test,  y_test,  shuffle=False, augment=False)

# ── Model ─────────────────────────────────────────────────────────────────────

class BeatCNN(nn.Module):
    def __init__(self, n_classes=3):
        super().__init__()
        self.features = nn.Sequential(
            # 256 → 128  (wider stem: 1→64)
            nn.Conv1d(1, 64, kernel_size=16, stride=2, padding=7),
            nn.BatchNorm1d(64), nn.ReLU(),
            nn.Dropout(0.2),

            # 128 → 64
            nn.Conv1d(64, 128, kernel_size=8, stride=2, padding=3),
            nn.BatchNorm1d(128), nn.ReLU(),
            nn.Dropout(0.2),

            # 64 → 32
            nn.Conv1d(128, 256, kernel_size=4, stride=2, padding=1),
            nn.BatchNorm1d(256), nn.ReLU(),
            nn.Dropout(0.2),

            # 32 → 32  (extra layer, no downsampling)
            nn.Conv1d(256, 256, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm1d(256), nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
        )
        self.head = nn.Sequential(
            nn.Flatten(),
            nn.Linear(256, 128), nn.ReLU(), nn.Dropout(0.5),
            nn.Linear(128, n_classes),
        )

    def forward(self, x):
        return self.head(self.features(x))

# ── Training ──────────────────────────────────────────────────────────────────

model     = BeatCNN(n_classes=len(class_names)).to(DEVICE)
optimizer = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-3)
scheduler = CosineAnnealingLR(optimizer, T_max=EPOCHS)
criterion = nn.CrossEntropyLoss()   # balanced already via SMOTE

best_val_loss    = float("inf")
patience_counter = 0
best_state       = None

# use 10% of train as in-loop validation
val_size = max(1, int(0.1 * len(X_train)))
X_val, y_val = X_train[-val_size:], y_train[-val_size:]
X_tr,  y_tr  = X_train[:-val_size], y_train[:-val_size]
fit_dl  = make_loader(X_tr,  y_tr,  shuffle=True,  augment=True)
inval_dl = make_loader(X_val, y_val, shuffle=False, augment=False)

print(f"{'Epoch':>6}  {'Train Loss':>10}  {'Train Acc':>10}  {'Val Loss':>10}  {'Val Acc':>10}")
print("-" * 55)

for epoch in range(1, EPOCHS + 1):
    model.train()
    t_loss, t_correct, t_total = 0.0, 0, 0
    for x, y_b in fit_dl:
        x, y_b = x.to(DEVICE), y_b.to(DEVICE)
        optimizer.zero_grad()
        logits = model(x)
        loss   = criterion(logits, y_b)
        loss.backward()
        optimizer.step()
        t_loss    += loss.item() * len(y_b)
        t_correct += (logits.argmax(1) == y_b).sum().item()
        t_total   += len(y_b)

    model.eval()
    v_loss, v_correct, v_total = 0.0, 0, 0
    with torch.no_grad():
        for x, y_b in inval_dl:
            x, y_b = x.to(DEVICE), y_b.to(DEVICE)
            logits  = model(x)
            v_loss    += criterion(logits, y_b).item() * len(y_b)
            v_correct += (logits.argmax(1) == y_b).sum().item()
            v_total   += len(y_b)

    t_acc = t_correct / t_total
    v_acc = v_correct / v_total
    t_l   = t_loss    / t_total
    v_l   = v_loss    / v_total

    if epoch % 20 == 0 or epoch == 1:
        print(f"{epoch:>6}  {t_l:>10.4f}  {t_acc:>10.3f}  {v_l:>10.4f}  {v_acc:>10.3f}")

    scheduler.step()

    if v_l < best_val_loss:
        best_val_loss    = v_l
        patience_counter = 0
        best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
    else:
        patience_counter += 1
        if patience_counter >= PATIENCE:
            print(f"\nEarly stopping at epoch {epoch}")
            break

# ── Evaluation ────────────────────────────────────────────────────────────────

model.load_state_dict(best_state)
model.eval()

all_preds, all_true = [], []
with torch.no_grad():
    for x, y_b in test_dl:
        logits = model(x.to(DEVICE))
        all_preds.extend(logits.argmax(1).cpu().tolist())
        all_true.extend(y_b.tolist())

acc = sum(p == l for p, l in zip(all_preds, all_true)) / len(all_true)
print(f"\nTest accuracy: {acc:.3f}  ({len(all_true)} beats)\n")

print("Classification report:")
print(classification_report(all_true, all_preds, target_names=class_names))

cm = confusion_matrix(all_true, all_preds)
print("Confusion matrix:")
print(pd.DataFrame(cm,
    index  =[f"A:{c}" for c in class_names],
    columns=[f"P:{c}" for c in class_names]))

# ── Export ────────────────────────────────────────────────────────────────────

import json

torch.save(best_state, "model_cnn2.pt")
print("\nSaved weights -> model_cnn2.pt")

meta_export = {
    "class_names": list(class_names),
    "beat_len": BEAT_LEN,
    "fs": FS,
    "accuracy": round(acc, 4),
}
with open("model_cnn2_meta.json", "w") as f:
    json.dump(meta_export, f, indent=2)
print("Saved metadata -> model_cnn2_meta.json")

dummy = torch.zeros(1, 1, BEAT_LEN, device="cpu")
model.cpu()
scripted = torch.jit.trace(model, dummy)
scripted.save("model_cnn2_scripted.pt")
print("Saved TorchScript -> model_cnn2_scripted.pt")
