"""
Loads classical models, scalers, and the Fusion ANN checkpoint once at
import time and exposes a single `predict_ai_detection()` entry point.

Transformer models (CodeBERT / UniXcoder) are NOT loaded locally.
They are called via the HuggingFace Inference API in a periodic background
job (see periodic_analysis.py).  During a normal /analyze/ai-detection
request, any stored transformer_score for the student is blended in; if none
is available yet, placeholder 0.5 is used.
"""
import io
import logging
import os
import pickle
import zipfile

import torch

from .fusion_ann import FusionANN
from .feature_extractor import FEATURE_COLUMNS, extract_features
from .behavioral import behavioral_risk_score, has_behavioral_data, normalize_behavioral_signals

logger = logging.getLogger(__name__)

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")

# HuggingFace Inference API endpoints (serverless, free tier)
CODEBERT_HF_REPO  = os.getenv("CODEBERT_HF_REPO",  "ahisrat10/codebert-ai-detector")
UNIXCODER_HF_REPO = os.getenv("UNIXCODER_HF_REPO", "ahisrat10/unixcoder-ai-detector")
HF_TOKEN          = os.getenv("HF_TOKEN") or None

# Weight of the behavioral heuristic in the final ai_probability.
BEHAVIORAL_BLEND = 0.15

# Placeholder used when transformer score is not yet available
_TRANSFORMER_PLACEHOLDER = 0.5


def _load_pickle(name: str):
    with open(os.path.join(MODELS_DIR, name), "rb") as f:
        return pickle.load(f)


def _load_fusion_state_dict(dir_name: str) -> dict:
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
rf_model  = _load_pickle("RandomForest.pkl")
lr_model  = _load_pickle("LogisticRegression.pkl")

scaler_classical = _load_pickle("scaler_classical.pkl")
scaler_fusion    = _load_pickle("scaler_fusion.pkl")

fusion_model = FusionANN(input_dim=33)
fusion_model.load_state_dict(_load_fusion_state_dict("fusion_ann_v3"))
fusion_model.eval()

FUSION_FEATURE_NAMES = _load_pickle("feature_names.pkl")

logger.info("Classical models + Fusion ANN loaded (transformers deferred to HF API)")


# ── HuggingFace Inference API ────────────────────────────────────

def call_hf_inference_api(hf_repo: str, code: str, timeout: int = 20) -> float:
    """
    Call the HuggingFace Inference API for a text-classification model.
    Returns probability of class 1 (AI-generated), or 0.5 on any error.
    """
    import requests

    url     = f"https://api-inference.huggingface.co/models/{hf_repo}"
    headers = {"Content-Type": "application/json"}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"

    payload = {"inputs": code[:1800]}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 503:
            logger.info("HF model %s loading (503); will retry next cycle", hf_repo)
            return _TRANSFORMER_PLACEHOLDER
        resp.raise_for_status()
        results = resp.json()
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict) and item.get("label") in ("LABEL_1", "1", "AI"):
                    return float(item["score"])
            label1 = next((x for x in results if "1" in str(x.get("label", ""))), None)
            if label1:
                return float(label1["score"])
        return _TRANSFORMER_PLACEHOLDER
    except Exception:
        logger.warning("HF Inference API call failed for %s", hf_repo, exc_info=True)
        return _TRANSFORMER_PLACEHOLDER


# ── Classical scoring ────────────────────────────────────────────

def _classical_scores(feature_vector):
    import numpy as np
    x_raw    = np.array([feature_vector], dtype=float)
    x_scaled = scaler_classical.transform(x_raw)
    lr_prob  = float(lr_model.predict_proba(x_scaled)[0][1])
    rf_prob  = float(rf_model.predict_proba(x_scaled)[0][1])
    xgb_prob = float(xgb_model.predict_proba(x_scaled)[0][1])
    return lr_prob, rf_prob, xgb_prob


def _fusion_score(lr_prob, rf_prob, xgb_prob, codebert_prob, unixcoder_prob,
                  code_features: dict, behavioral: dict):
    import numpy as np
    row = {
        "lr_prob":        lr_prob,
        "rf_prob":        rf_prob,
        "xgb_prob":       xgb_prob,
        "codebert_prob":  codebert_prob,
        "unixcoder_prob": unixcoder_prob,
        **code_features,
        **behavioral,
    }
    ordered  = [row[name] for name in FUSION_FEATURE_NAMES]
    x_raw    = np.array([ordered], dtype=float)
    x_scaled = scaler_fusion.transform(x_raw)
    with torch.no_grad():
        logit = fusion_model(torch.tensor(x_scaled, dtype=torch.float32))
        prob  = torch.sigmoid(logit).item()
    return prob


# ── Main prediction entry point ──────────────────────────────────

def predict_ai_detection(
    code: str,
    language: str,
    behavioral_signals: dict,
    transformer_score=None,
) -> dict:
    """
    Returns AI detection result using classical models + Fusion ANN.
    transformer_score (0-1 float): pre-computed transformer probability stored
    in DB by the periodic background job.  If None, placeholder 0.5 is used.
    """
    code_features  = extract_features(code, language)
    feature_vector = [code_features[col] for col in FEATURE_COLUMNS]

    lr_prob, rf_prob, xgb_prob = _classical_scores(feature_vector)

    codebert_prob  = transformer_score if transformer_score is not None else _TRANSFORMER_PLACEHOLDER
    unixcoder_prob = transformer_score if transformer_score is not None else _TRANSFORMER_PLACEHOLDER

    sig = normalize_behavioral_signals(behavioral_signals)

    behavioral = {
        "paste_count":     sig["paste_count"],
        "typing_ratio":    sig["typing_ratio"],
        "tab_switch":      sig["tab_switches"],
        "backspace_freq":  sig["backspace_freq"],
        "suspicion_score": sig["suspicion_score"],
    }

    fusion_prob = _fusion_score(
        lr_prob, rf_prob, xgb_prob,
        codebert_prob, unixcoder_prob,
        code_features, behavioral,
    )

    ai_probability = (
        lr_prob  * 0.10
        + rf_prob  * 0.25
        + xgb_prob * 0.30
        + codebert_prob  * 0.175
        + unixcoder_prob * 0.175
    )

    behavioral_risk, behavioral_breakdown = behavioral_risk_score(sig)
    behavioral_used = has_behavioral_data(sig)
    if behavioral_used:
        ai_probability = ai_probability * (1 - BEHAVIORAL_BLEND) + behavioral_risk * BEHAVIORAL_BLEND

    prediction = "AI" if ai_probability >= 0.5 else "Human"

    transformer_note = (
        f"Transformer score from last periodic analysis: {transformer_score:.3f}"
        if transformer_score is not None
        else "Transformer score not yet available; placeholder 0.5 used. Will update in <= 10 min."
    )
    if behavioral_used:
        transformer_note += f" Behavioral risk {behavioral_risk:.3f} blended at {BEHAVIORAL_BLEND:.0%}."
    else:
        transformer_note += " No behavioral telemetry received."

    return {
        "prediction":           prediction,
        "confidence":           round(ai_probability if prediction == "AI" else 1 - ai_probability, 4),
        "ai_probability":       round(ai_probability, 4),
        "model_scores": {
            "logistic_regression": round(lr_prob, 4),
            "random_forest":       round(rf_prob, 4),
            "xgboost":             round(xgb_prob, 4),
            "codebert":            round(codebert_prob, 4),
            "unixcoder":           round(unixcoder_prob, 4),
            "fusion_ann":          round(fusion_prob, 4),
        },
        "code_features":        code_features,
        "behavioral_risk":      behavioral_risk,
        "behavioral_breakdown": behavioral_breakdown,
        "behavioral_signals":   sig,
        "transformer_updated":  transformer_score is not None,
        "notes":                transformer_note,
    }
