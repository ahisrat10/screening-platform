"""
Loads all classical models, scalers, and the Fusion ANN checkpoint once at
import time and exposes a single `predict_ai_detection()` entry point.
"""
import io
import os
import pickle
import zipfile

import torch

from .fusion_ann import FusionANN
from .feature_extractor import FEATURE_COLUMNS, extract_features

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")


def _load_pickle(name: str):
    with open(os.path.join(MODELS_DIR, name), "rb") as f:
        return pickle.load(f)


def _load_fusion_state_dict(dir_name: str) -> dict:
    """
    fusion_ann_v3 was saved with torch.save() and is on disk as an already
    -unzipped PyTorch archive directory (data.pkl + data/0..N + version).
    torch.load() only accepts a file, so we re-zip it into an in-memory
    buffer with the archive-name prefix PyTorch's zip format requires.
    """
    src = os.path.join(MODELS_DIR, dir_name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for root, _dirs, files in os.walk(src):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, src).replace(os.sep, "/")
                zf.write(full, f"archive/{rel}")
    buf.seek(0)
    return torch.load(buf, map_location="cpu", weights_only=False)


# ── Load once at import time ────────────────────────────────────
xgb_model = _load_pickle("XGBoost.pkl")
rf_model = _load_pickle("RandomForest.pkl")
lr_model = _load_pickle("LogisticRegression.pkl")

scaler_classical = _load_pickle("scaler_classical.pkl")
scaler_fusion = _load_pickle("scaler_fusion.pkl")

fusion_model = FusionANN(input_dim=33)
fusion_model.load_state_dict(_load_fusion_state_dict("fusion_ann_v3"))
fusion_model.eval()

FUSION_FEATURE_NAMES = _load_pickle("feature_names.pkl")

# Placeholder until CodeBERT / UniXcoder inference is wired up.
_TRANSFORMER_PLACEHOLDER = 0.5


def _classical_scores(feature_vector):
    """feature_vector: list[float] in FEATURE_COLUMNS order (unscaled)."""
    import numpy as np

    x_raw = np.array([feature_vector], dtype=float)
    x_scaled = scaler_classical.transform(x_raw)

    lr_prob = float(lr_model.predict_proba(x_scaled)[0][1])
    rf_prob = float(rf_model.predict_proba(x_scaled)[0][1])
    xgb_prob = float(xgb_model.predict_proba(x_scaled)[0][1])
    return lr_prob, rf_prob, xgb_prob


def _fusion_score(lr_prob, rf_prob, xgb_prob, code_features: dict, behavioral: dict):
    import numpy as np

    row = {
        "lr_prob": lr_prob,
        "rf_prob": rf_prob,
        "xgb_prob": xgb_prob,
        "codebert_prob": _TRANSFORMER_PLACEHOLDER,
        "unixcoder_prob": _TRANSFORMER_PLACEHOLDER,
        **code_features,
        **behavioral,
    }
    ordered = [row[name] for name in FUSION_FEATURE_NAMES]

    x_raw = np.array([ordered], dtype=float)
    x_scaled = scaler_fusion.transform(x_raw)

    with torch.no_grad():
        logit = fusion_model(torch.tensor(x_scaled, dtype=torch.float32))
        prob = torch.sigmoid(logit).item()
    return prob


def predict_ai_detection(code: str, language: str, behavioral_signals: dict) -> dict:
    code_features = extract_features(code, language)
    feature_vector = [code_features[col] for col in FEATURE_COLUMNS]

    lr_prob, rf_prob, xgb_prob = _classical_scores(feature_vector)

    behavioral = {
        "paste_count": behavioral_signals.get("paste_count", 0),
        "typing_ratio": behavioral_signals.get("typing_ratio", 0.0),
        "tab_switch": behavioral_signals.get("tab_switches", 0),
        "backspace_freq": behavioral_signals.get("backspace_freq", 0.0),
        "suspicion_score": behavioral_signals.get("suspicion_score", 0),
    }

    fusion_prob = _fusion_score(lr_prob, rf_prob, xgb_prob, code_features, behavioral)

    prediction = "AI" if fusion_prob >= 0.5 else "Human"

    return {
        "prediction": prediction,
        "confidence": round(fusion_prob if prediction == "AI" else 1 - fusion_prob, 4),
        "ai_probability": round(fusion_prob, 4),
        "model_scores": {
            "logistic_regression": round(lr_prob, 4),
            "random_forest": round(rf_prob, 4),
            "xgboost": round(xgb_prob, 4),
            "fusion_ann": round(fusion_prob, 4),
        },
        "code_features": code_features,
        "notes": (
            "codebert_prob and unixcoder_prob are placeholder values (0.5) — "
            "transformer checkpoints were not provided; fusion score quality "
            "is reduced until real embeddings are wired in."
        ),
    }
