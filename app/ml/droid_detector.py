"""
Optional DroidDetect (project-droid) AI-code detector.

Opt-in: only active when the env var DROID_MODEL_DIR points at a folder that
holds a downloaded DroidDetect checkpoint (pytorch_model.bin + tokenizer
files + config.json). When it is unset, or torch/transformers are missing,
`droid_ai_probability()` returns None and callers fall back to the old
HuggingFace Inference API path.

Labels (Large-Ternary): 0 HUMAN, 1 MACHINE_GENERATED, 2 MACHINE_REFINED
Labels (Base, 4-class): + 3 MACHINE_GENERATED_ADVERSARIAL
AI probability = 1 - P(HUMAN).

Loaded lazily and once, so importing this module costs nothing. Needs
roughly 2-3 GB of RAM for Large, ~1 GB for Base: do not enable on a 512 MB
host.
"""
import logging
import os
import threading

logger = logging.getLogger(__name__)

DROID_MODEL_DIR = os.getenv("DROID_MODEL_DIR") or None
DROID_MAX_LEN = int(os.getenv("DROID_MAX_LEN", "512"))

_lock = threading.Lock()
_state = {"tried": False, "ok": False, "model": None, "tok": None, "torch": None}


def _build(model_dir: str):
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from transformers import AutoTokenizer, ModernBertConfig, ModernBertModel

    sd = torch.load(os.path.join(model_dir, "pytorch_model.bin"), map_location="cpu", weights_only=True)

    def find(suffix):
        for k in sd:
            if k.endswith(suffix):
                return k
        raise KeyError(suffix)

    proj_w = sd[find("text_projection.weight")]
    cls_w = sd[find("classifier.weight")]
    hidden = proj_w.shape[1]
    num_classes = cls_w.shape[0]

    # Config is inferred from tensor shapes, so no hub download is needed.
    layers = len({k.split(".layers.")[1].split(".")[0] for k in sd if ".layers." in k})
    inter_key = next(k for k in sd if k.endswith("mlp.Wo.weight"))
    inter = sd[inter_key].shape[1]
    heads = 16 if hidden == 1024 else 12
    cfg = ModernBertConfig(
        hidden_size=hidden, intermediate_size=inter,
        num_hidden_layers=layers, num_attention_heads=heads,
    )

    class DroidModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.text_encoder = ModernBertModel(cfg)
            self.text_projection = nn.Linear(hidden, proj_w.shape[0])
            self.classifier = nn.Linear(proj_w.shape[0], num_classes)

        def forward(self, input_ids, attention_mask):
            h = self.text_encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
            h = h.mean(dim=1)  # same pooling as the training code
            return self.classifier(F.relu(self.text_projection(h)))

    model = DroidModel()
    # checkpoint keys may be prefixed or not; strip anything before "text_encoder."/etc.
    clean = {}
    for k, v in sd.items():
        for root in ("text_encoder.", "text_projection.", "classifier."):
            i = k.find(root)
            if i != -1:
                clean[k[i:]] = v
                break
    missing, unexpected = model.load_state_dict(clean, strict=False)
    real_missing = [m for m in missing if "rotary" not in m]
    if real_missing:
        raise RuntimeError(f"Droid checkpoint mismatch, missing keys: {real_missing[:5]}")
    model.eval()
    tok = AutoTokenizer.from_pretrained(model_dir)
    return torch, model, tok, num_classes


def _ensure_loaded() -> bool:
    if _state["tried"]:
        return _state["ok"]
    with _lock:
        if _state["tried"]:
            return _state["ok"]
        _state["tried"] = True
        if not DROID_MODEL_DIR:
            return False
        try:
            torch, model, tok, n = _build(DROID_MODEL_DIR)
            _state.update(ok=True, model=model, tok=tok, torch=torch)
            logger.info("DroidDetect loaded from %s (%d classes)", DROID_MODEL_DIR, n)
        except Exception:
            logger.warning("DroidDetect unavailable, using fallback", exc_info=True)
        return _state["ok"]


def droid_ai_probability(code: str):
    """Return P(AI) in 0-1, or None when the detector is not enabled/available."""
    if not code or not _ensure_loaded():
        return None
    torch = _state["torch"]
    try:
        enc = _state["tok"](code, return_tensors="pt", truncation=True, max_length=DROID_MAX_LEN)
        with torch.no_grad():
            logits = _state["model"](enc["input_ids"], enc["attention_mask"])
            probs = torch.softmax(logits, dim=-1)[0]
        return float(1.0 - probs[0])
    except Exception:
        logger.warning("DroidDetect inference failed", exc_info=True)
        return None
