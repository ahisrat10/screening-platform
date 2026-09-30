"""
Loads all classical models, scalers, and the Fusion ANN checkpoint once at
import time and exposes a single `predict_ai_detection()` entry point.
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
CODEBERT_DIR = os.path.join(MODELS_DIR, "codebert", "final_model")
UNIXCODER_DIR = os.path.join(MODELS_DIR, "unixcoder", "final_model")

TRANSFORMER_MAX_LENGTH = 512

# Weight of the behavioral heuristic in the final ai_probability.
BEHAVIORAL_BLEND = 0.15


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

# Fallback used for a transformer whenever it fails to load or run.
_TRANSFORMER_PLACEHOLDER = 0.5


def _load_transformer(model_dir: str, name: str):
    """Load a tokenizer + sequence-classification model from `model_dir`.

    Returns (tokenizer, model), or (None, None) if loading fails for any
    reason (missing files, bad checkpoint, transformers not installed,
    etc.) — callers fall back to _TRANSFORMER_PLACEHOLDER in that case.
    """
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(model_dir)
        model = AutoModelForSequenceClassification.from_pretrained(model_dir)
        model.eval()
        return tokenizer, model
    except Exception:
        logger.warning("Failed to load %s model from %s; falling back to placeholder 0.5", name, model_dir, exc_info=True)
        return None, None


codebert_tokenizer, codebert_model = _load_transformer(CODEBERT_DIR, "CodeBERT")
unixcoder_tokenizer, unixcoder_model = _load_transformer(UNIXCODER_DIR, "UniXcoder")


def _transformer_prob(tokenizer, model, code: str) -> float:
    """Probability (class 1 = AI-generated) from a loaded transformer, or
    the placeholder if the model/tokenizer isn't available."""
    if tokenizer is None or model is None:
        return _TRANSFORMER_PLACEHOLDER

    try:
        inputs = tokenizer(
            code,
            return_tensors="pt",
            truncation=True,
            padding=True,
            max_length=TRANSFORMER_MAX_LENGTH,
        )
        with torch.no_grad():
            logits = model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)
        return float(probs[0][1].item())
    except Exception:
        logger.warning("Transformer inference failed; falling back to placeholder 0.5", exc_info=True)
        return _TRANSFORMER_PLACEHOLDER


def _transformer_scores(code: str):
    codebert_prob = _transformer_prob(codebert_tokenizer, codebert_model, code)
    unixcoder_prob = _transformer_prob(unixcoder_tokenizer, unixcoder_model, code)
    return codebert_prob, unixcoder_prob


def _classical_scores(feature_vector):
    """feature_vector: list[float] in FEATURE_COLUMNS order (unscaled)."""
    import numpy as np

    x_raw = np.array([feature_vector], dtype=float)
    x_scaled = scaler_classical.transform(x_raw)

    lr_prob = float(lr_model.predict_proba(x_scaled)[0][1])
    rf_prob = float(rf_model.predict_proba(x_scaled)[0][1])
    xgb_prob = float(xgb_model.predict_proba(x_scaled)[0][1])
    return lr_prob, rf_prob, xgb_prob


def _fusion_score(lr_prob, rf_prob, xgb_prob, codebert_prob, unixcoder_prob, code_features: dict, behavioral: dict):
    import numpy as np

    row = {
        "lr_prob": lr_prob,
        "rf_prob": rf_prob,
        "xgb_prob": xgb_prob,
        "codebert_prob": codebert_prob,
        "unixcoder_prob": unixcoder_prob,
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
    codebert_prob, unixcoder_prob = _transformer_scores(code)

    # Accept the extension's field names (total_pastes, total_tab_switches,
    # backspace_frequency, suspicious_score, ...) as well as the legacy ones.
    sig = normalize_behavioral_signals(behavioral_signals)

    # The fusion ANN was trained on exactly these five behavioral inputs.
    behavioral = {
        "paste_count": sig["paste_count"],
        "typing_ratio": sig["typing_ratio"],
        "tab_switch": sig["tab_switches"],
        "backspace_freq": sig["backspace_freq"],
        "suspicion_score": sig["suspicion_score"],
    }

    fusion_prob = _fusion_score(lr_prob, rf_prob, xgb_prob, codebert_prob, unixcoder_prob, code_features, behavioral)

    ai_probability = (
        lr_prob * 0.10
        + rf_prob * 0.25
        + xgb_prob * 0.30
        + codebert_prob * 0.175
        + unixcoder_prob * 0.175
    )

    # The fusion ANN can only see 5 behavioral inputs (fixed at training
    # time), so the remaining signals feed a separate heuristic risk score
    # that is blended in only when the client actually sent telemetry.
    behavioral_risk, behavioral_breakdown = behavioral_risk_score(sig)
    behavioral_used = has_behavioral_data(sig)
    if behavioral_used:
        ai_probability = ai_probability * (1 - BEHAVIORAL_BLEND) + behavioral_risk * BEHAVIORAL_BLEND

    prediction = "AI" if ai_probability >= 0.5 else "Human"

    placeholders_used = []
    if codebert_model is None:
        placeholders_used.append("codebert_prob")
    if unixcoder_model is None:
        placeholders_used.append("unixcoder_prob")

    if placeholders_used:
        notes = (
            f"{' and '.join(placeholders_used)} could not be loaded and used the "
            "placeholder value (0.5); fusion score quality is reduced until the "
            "checkpoint is fixed."
        )
    else:
        notes = "All model scores, including codebert_prob and unixcoder_prob, are from loaded checkpoints."

    if behavioral_used:
        notes += f" Behavioral risk score {behavioral_risk} (11 signals) blended at {BEHAVIORAL_BLEND:.0%}."
    else:
        notes += " No behavioral telemetry received; behavioral score not applied."

    return {
        "prediction": prediction,
        "confidence": round(ai_probability if prediction == "AI" else 1 - ai_probability, 4),
        "ai_probability": round(ai_probability, 4),
        "model_scores": {
            "logistic_regression": round(lr_prob, 4),
            "random_forest": round(rf_prob, 4),
            "xgboost": round(xgb_prob, 4),
            "codebert": round(codebert_prob, 4),
            "unixcoder": round(unixcoder_prob, 4),
            "fusion_ann": round(fusion_prob, 4),
        },
        "code_features": code_features,
        "behavioral_risk": behavioral_risk,
        "behavioral_breakdown": behavioral_breakdown,
        "behavioral_signals": sig,
        "notes": notes,
    }
