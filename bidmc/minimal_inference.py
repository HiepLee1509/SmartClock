import torch, json, numpy as np
from scipy.signal import butter, filtfilt, find_peaks, resample

model = torch.jit.load("model_cnn2_scripted.pt").eval()
meta  = json.load(open("model_cnn2_meta.json"))
FS, BEAT_LEN = meta["fs"], meta["beat_len"]

def predict(signal_1d):
    # bandpass
    b, a = butter(3, [0.5/500, 8.0/500], btype="band")
    sig = filtfilt(b, a, signal_1d)
    norm = (sig - sig.mean()) / (sig.std() + 1e-8)
    peaks, _ = find_peaks(norm, distance=int(FS*0.4), prominence=0.4)
    beats = []
    for i in range(len(peaks)-1):
        onset  = (peaks[i-1]+peaks[i])//2 if i>0 else max(0, peaks[i]-(peaks[i+1]-peaks[i])//2)
        offset = (peaks[i]+peaks[i+1])//2
        seg = sig[onset:offset]
        if len(seg) < 30: continue
        seg = resample(seg, BEAT_LEN).astype(np.float32)
        seg = (seg - seg.mean()) / (seg.std() + 1e-8)
        beats.append(seg)
    if not beats:
        return None
    x     = torch.tensor(np.stack(beats)).unsqueeze(1)
    probs = torch.softmax(model(x), dim=1).mean(0)
    return meta["class_names"][probs.argmax().item()]
