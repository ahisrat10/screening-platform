"""
Continual-learning retrain: fine-tunes the already-loaded Fusion ANN on
teacher-submitted labels (TeacherFeedback rows from the per-class monitoring
view's Human/AI/Paraphrased buttons).

Each usable sample needs a TeacherFeedback row with session_id + a Submission
found for that student/session (same lookup `_flat_row` in export.py uses)
that has code_features and model_scores already computed. Behavioral inputs
for the fusion vector are approximated from the linked BehavioralData row
(paste_count = large+small paste counts, etc.) since the original raw
request-shaped behavioral dict used at prediction time isn't stored.
"""
import copy
import io
import logging
import os
import shutil
import zipfile
from datetime import datetime

import numpy as np
import torch
from torch import nn

from . import model_loader
from .. import models

logger = logging.getLogger("screening.retrain")

LABEL_TARGETS = {"human": 0.0, "ai_paraphrased": 0.5, "paraphrased": 0.5, "ai": 1.0}

MIN_SAMPLES = 5
EPOCHS = 20


def _behavioral_from_row(beh) -> dict:
    if beh is None:
        return {
            "paste_count": 0.0, "typing_ratio": 0.0, "tab_switch": 0.0,
            "backspace_freq": 0.0, "suspicion_score": 0.0,
        }
    return {
        "paste_count":     float((beh.large_paste_count or 0) + (beh.small_paste_count or 0)),
        "typing_ratio":    float(beh.typing_ratio or 0.0),
        "tab_switch":      float(beh.tab_switch_count or 0.0),
        "backspace_freq":  float(beh.backspace_frequency or 0.0),
        "suspicion_score": float(beh.suspicion_score or 0.0),
    }


def _find_submission(db, sess: "models.StudentSession"):
    sid = sess.student_id
    started = sess.connected_at
    q = db.query(models.Submission).filter(
        models.Submission.student_id == sid,
        models.Submission.ai_prediction.isnot(None),
    )
    if started:
        q = q.filter(models.Submission.timestamp >= started)
    return q.order_by(models.Submission.timestamp.desc(), models.Submission.id.desc()).first()


def _build_vector(db, fb: "models.TeacherFeedback"):
    """Returns a scaled 1-D numpy feature vector, or None if this feedback
    row doesn't have enough data to train on."""
    sess = db.query(models.StudentSession).filter(models.StudentSession.id == fb.session_id).first()
    if not sess:
        return None
    sub = _find_submission(db, sess)
    if not sub or not sub.code_features or not sub.model_scores:
        return None

    beh = None
    if sub.behavioral_data_id:
        beh = db.query(models.BehavioralData).filter(models.BehavioralData.id == sub.behavioral_data_id).first()

    row = {
        "lr_prob":        sub.model_scores.get("logistic_regression"),
        "rf_prob":        sub.model_scores.get("random_forest"),
        "xgb_prob":       sub.model_scores.get("xgboost"),
        "codebert_prob":  sub.model_scores.get("codebert"),
        "unixcoder_prob": sub.model_scores.get("unixcoder"),
        **sub.code_features,
        **_behavioral_from_row(beh),
    }
    try:
        ordered = [row[name] for name in model_loader.FUSION_FEATURE_NAMES]
    except KeyError:
        return None
    if any(v is None for v in ordered):
        return None
    return np.array(ordered, dtype=float)


def _save_fusion_state_dict(state_dict: dict, dir_name: str):
    """Inverse of model_loader._load_fusion_state_dict: writes `state_dict`
    as a torch zip archive, then unpacks it over the model directory so the
    on-disk layout stays byte-compatible with the loader."""
    target_dir = os.path.join(model_loader.MODELS_DIR, dir_name)
    backup_dir = target_dir + f"_backup_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    shutil.copytree(target_dir, backup_dir)

    buf = io.BytesIO()
    torch.save(state_dict, buf)
    buf.seek(0)

    shutil.rmtree(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(buf) as zf:
        for info in zf.infolist():
            # Archive entries are prefixed "archive/..."; strip it back off.
            rel = info.filename.split("/", 1)[1] if "/" in info.filename else info.filename
            if not rel:
                continue
            dest = os.path.join(target_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                out.write(src.read())
    return backup_dir


def _set_setting(db, key: str, value: str):
    row = db.query(models.AppSetting).filter(models.AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(models.AppSetting(key=key, value=value))


def get_status(db) -> dict:
    def _get(key):
        row = db.query(models.AppSetting).filter(models.AppSetting.key == key).first()
        return row.value if row else None

    current_count = db.query(models.TeacherFeedback).filter(
        models.TeacherFeedback.session_id.isnot(None),
        models.TeacherFeedback.teacher_label.isnot(None),
    ).count()
    remainder = current_count % 10
    return {
        "last_retrain_at":      _get("retrain_last_at"),
        "samples_used":         int(_get("retrain_last_samples") or 0),
        "current_feedback_count": current_count,
        # No real schedule exists — this is the feedback-count threshold
        # the auto-retrain trigger waits for, not a wall-clock time.
        "next_retrain_at": f"after {10 - remainder if remainder else 10} more labeled sample(s) (every 10)",
    }


def run_retrain(db) -> dict:
    """Runs synchronously; call this from a BackgroundTasks callback so the
    HTTP request that triggered it doesn't block on training."""
    try:
        rows = db.query(models.TeacherFeedback).filter(
            models.TeacherFeedback.session_id.isnot(None),
            models.TeacherFeedback.teacher_label.isnot(None),
        ).all()

        X, y = [], []
        for fb in rows:
            target = LABEL_TARGETS.get((fb.teacher_label or "").lower())
            if target is None:
                continue
            vec = _build_vector(db, fb)
            if vec is None:
                continue
            X.append(vec)
            y.append(target)

        if len(X) < MIN_SAMPLES:
            return {
                "success": False,
                "message": f"Not enough usable labeled samples to retrain (need {MIN_SAMPLES}, got {len(X)}).",
                "samples_used": len(X),
            }

        X_scaled = model_loader.scaler_fusion.transform(np.array(X, dtype=float))
        x_tensor = torch.tensor(X_scaled, dtype=torch.float32)
        y_tensor = torch.tensor(y, dtype=torch.float32).unsqueeze(1)

        # Train a fresh copy first; only commit it to the live global model
        # and to disk if training succeeds, so a mid-training error can
        # never leave `model_loader.fusion_model` half-updated.
        trial_model = copy.deepcopy(model_loader.fusion_model)
        trial_model.train()
        optimizer = torch.optim.Adam(trial_model.parameters(), lr=1e-3)
        loss_fn = nn.BCEWithLogitsLoss()

        final_loss = None
        for _epoch in range(EPOCHS):
            optimizer.zero_grad()
            logits = trial_model(x_tensor)
            loss = loss_fn(logits, y_tensor)
            loss.backward()
            optimizer.step()
            final_loss = loss.item()

        trial_model.eval()
        backup_dir = _save_fusion_state_dict(trial_model.state_dict(), "fusion_ann_v3")

        model_loader.fusion_model.load_state_dict(trial_model.state_dict())
        model_loader.fusion_model.eval()

        now_iso = datetime.utcnow().isoformat()
        _set_setting(db, "retrain_last_at", now_iso)
        _set_setting(db, "retrain_last_samples", str(len(X)))
        _set_setting(db, "retrain_last_loss", str(final_loss))
        db.commit()

        logger.info("Retrain complete: %d samples, final_loss=%.4f, backup=%s", len(X), final_loss, backup_dir)
        return {"success": True, "samples_used": len(X), "epochs": EPOCHS, "final_loss": round(final_loss, 6)}
    except Exception:
        logger.exception("Retrain failed")
        db.rollback()
        return {"success": False, "message": "Retrain failed; the live model was not changed. See server logs."}
