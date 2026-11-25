"""
Behavioral-signal handling for AI detection.

The VS Code extension sends its own field names (total_pastes,
total_tab_switches, suspicious_score, ...). `normalize_behavioral_signals`
maps those onto the internal/legacy names used by the fusion model's feature
set and the behavioral_data table, so both sides stay in sync. Legacy names
are still accepted as a fallback.
"""
import json


def _num(raw: dict, *names, default=0.0) -> float:
    """First present, non-None value among `names`, as a float."""
    for name in names:
        value = raw.get(name)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return float(default)


def normalize_behavioral_signals(raw: dict) -> dict:
    """Extension-named signals -> internal names. Unknown keys are kept."""
    raw = raw or {}
    out = dict(raw)

    keystrokes = _num(raw, "total_keystrokes")
    undo = _num(raw, "undo_count")
    redo = _num(raw, "redo_count")
    duration_ms = _num(raw, "session_duration_ms")

    # ── Five inputs the fusion model was trained on ───────────────
    out["paste_count"] = int(_num(raw, "total_pastes", "paste_count"))
    out["tab_switches"] = int(_num(raw, "total_tab_switches", "tab_switches", "tab_switch_count"))
    out["backspace_freq"] = _num(raw, "backspace_frequency", "backspace_freq")
    out["suspicion_score"] = _num(raw, "suspicious_score", "suspicion_score")
    out["typing_ratio"] = _num(raw, "typing_ratio")

    # ── Extension signals added to the backend ────────────────────
    out["total_keystrokes"] = int(keystrokes)
    out["session_duration_ms"] = duration_ms
    out["typing_pause_count"] = int(_num(raw, "typing_pause_count"))
    out["undo_count"] = int(undo)
    out["redo_count"] = int(redo)
    out["scroll_event_count"] = int(_num(raw, "scroll_event_count", "scroll_events"))
    out["sudden_code_jump_count"] = int(_num(raw, "sudden_code_jump_count", "sudden_code_jumps"))
    out["code_deletion_events"] = int(_num(raw, "code_deletion_events"))
    out["total_idle_events"] = int(_num(raw, "total_idle_events"))
    out["avg_time_between_keys_ms"] = _num(raw, "avg_time_between_keys_ms", "avg_time_between_keys")
    out["keystroke_rhythm_stddev_ms"] = _num(raw, "keystroke_rhythm_stddev_ms", "keystroke_rhythm_std")

    # ── Mirror onto behavioral_data column names ──────────────────
    out["backspace_frequency"] = out["backspace_freq"]
    # NOTE: raw is data.behavioral.model_dump() — a strongly-typed Pydantic
    # model, so EVERY declared field is always present (0 if the extension
    # didn't send it). raw.get("backspace_count") is therefore never None
    # even when truly absent, so the old _num(raw, "backspace_count",
    # "backspace_frequency") fallback chain never reached its second name.
    # Read backspace_frequency directly instead.
    out["backspace_count"] = int(_num(raw, "backspace_frequency"))
    out["tab_switch_count"] = out["tab_switches"]
    out["keys_per_minute"] = _num(raw, "keys_per_minute_avg", "keys_per_minute")
    out["keystroke_rhythm_std"] = out["keystroke_rhythm_stddev_ms"]
    out["avg_time_between_keys"] = out["avg_time_between_keys_ms"]
    out["undo_redo_count"] = int(undo + redo) or int(_num(raw, "undo_redo_count"))
    out["scroll_events"] = out["scroll_event_count"]
    out["sudden_code_jumps"] = out["sudden_code_jump_count"]
    out["copy_from_same_file"] = int(_num(raw, "copy_from_same_file_count", "copy_from_same_file"))
    out["screen_freeze_count"] = int(_num(raw, "screen_freeze_events", "screen_freeze_count"))
    out["total_session_seconds"] = duration_ms / 1000.0 if duration_ms else _num(raw, "total_session_seconds")
    out["sudden_skill_jump"] = bool(_num(raw, "sudden_skill_jump_count", "sudden_skill_jump"))
    return out


def has_behavioral_data(sig: dict) -> bool:
    """True if the client actually sent telemetry (not all schema defaults)."""
    return bool(sig.get("total_keystrokes") or sig.get("session_duration_ms")
                or sig.get("paste_count") or sig.get("suspicion_score"))


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


# (signal, weight) — 11 signals. Heuristic thresholds, NOT calibrated on data.
def behavioral_risk_score(sig: dict):
    """
    Returns (score in [0,1], per-signal breakdown). Higher = more AI-like
    behaviour (heavy pasting, sudden jumps, almost no editing, inhuman
    typing rhythm, high extension suspicion score).
    """
    keys = sig.get("total_keystrokes", 0)
    enough_typing = keys >= 50
    minutes = sig.get("session_duration_ms", 0) / 60000.0

    parts = {
        "paste_count":        (_clip(sig.get("paste_count", 0) / 5),             0.18),
        "suspicion_score":    (_clip(sig.get("suspicion_score", 0) / 100),       0.16),
        "sudden_code_jumps":  (_clip(sig.get("sudden_code_jump_count", 0) / 3),  0.14),
        "tab_switches":       (_clip(sig.get("tab_switches", 0) / 10),           0.08),
        # Little correction while typing real volume: people backspace/undo.
        "backspace_rate":     ((1 - _clip(sig.get("backspace_freq", 0) / max(keys, 1) / 0.05)) if enough_typing else 0.0, 0.10),
        "undo_count":         ((1.0 if sig.get("undo_count", 0) == 0 else 0.0) if enough_typing else 0.0, 0.04),
        "code_deletions":     ((1.0 if sig.get("code_deletion_events", 0) == 0 else 0.0) if enough_typing else 0.0, 0.06),
        # Very few keystrokes for the time spent / very fast overall.
        "typing_pauses":      ((1.0 if sig.get("typing_pause_count", 0) == 0 else 0.0) if minutes >= 2 else 0.0, 0.06),
        "session_duration":   ((1 - _clip(minutes / 10)) if sig.get("session_duration_ms", 0) else 0.0, 0.06),
        "scroll_events":      ((1.0 if sig.get("scroll_event_count", 0) == 0 else 0.0) if minutes >= 2 else 0.0, 0.04),
        # Machine-regular rhythm: low stddev relative to the mean interval.
        "keystroke_rhythm":   ((1 - _clip((sig.get("keystroke_rhythm_stddev_ms", 0) / max(sig.get("avg_time_between_keys_ms", 0), 1)) / 0.5))
                               if enough_typing and sig.get("avg_time_between_keys_ms", 0) > 0 else 0.0, 0.08),
    }
    total_w = sum(w for _, w in parts.values())
    score = sum(v * w for v, w in parts.values()) / total_w
    return round(score, 4), {k: round(v, 3) for k, (v, _) in parts.items()}


# ── Aggregating discrete student_events into behavioral_data columns ───
# The extension's /analyze/ai-detection payload only ever carried AGGREGATE
# counters (total_pastes, total_tab_switches, ...). Several behavioral_data
# columns were meant to hold finer-grained counts that the extension instead
# reports as one-off events on /student/event — those columns were always 0.
# This reads the matching events for the current session window and fills
# them in. Columns this *can't* reconstruct from discrete events alone
# (typing_ratio, code_deletion_ratio, time_per_line_ratio — true ratios, not
# counts) are left as sent by the client.
EVENT_COUNT_COLUMNS = {
    "rapid_tab_switches":     "RAPID_TAB_SWITCHING",
    "single_tab_switch":      "WINDOW_UNFOCUSED",
    "window_unfocused_count": "WINDOW_UNFOCUSED",
    "window_refocused_count": "WINDOW_REFOCUSED",
    "long_absence_count":     "LONG_ABSENCE",
    "fast_burst_count":       "FAST_BURST_TYPING",
    "file_switches":          "FILE_SWITCHED",
}


def aggregate_event_counts(db, student_id: str, since) -> dict:
    """Query student_events for `student_id` since the `since` datetime and
    return the behavioral_data columns derivable from them. `db` is a
    SQLAlchemy Session; import of models is local to dodge any import-order
    issues between app.ml and the main app package."""
    from .. import models

    events = db.query(models.StudentEvent).filter(
        models.StudentEvent.student_id == student_id,
        models.StudentEvent.timestamp >= since,
    ).all()

    counts = {col: 0 for col in EVENT_COUNT_COLUMNS}
    total_unfocused_seconds = 0.0
    paste_sizes = []
    paste_timestamps = []
    tab_event_timestamps = []

    for ev in events:
        for col, event_type in EVENT_COUNT_COLUMNS.items():
            if ev.event_type == event_type:
                counts[col] += 1

        if ev.event_type in ("WINDOW_REFOCUSED", "RAPID_TAB_SWITCHING", "WINDOW_UNFOCUSED"):
            tab_event_timestamps.append(ev.timestamp)

        if ev.event_type == "WINDOW_REFOCUSED":
            try:
                details = json.loads(ev.details) if ev.details else {}
            except (TypeError, ValueError):
                details = {}
            try:
                total_unfocused_seconds += float(details.get("away_duration_seconds") or 0)
            except (TypeError, ValueError):
                pass

        if ev.event_type == "PASTE_DETECTED":
            try:
                details = json.loads(ev.details) if ev.details else {}
            except (TypeError, ValueError):
                details = {}
            chars = details.get("chars_pasted")
            if isinstance(chars, (int, float)):
                paste_sizes.append(chars)
            paste_timestamps.append(ev.timestamp)

    # A paste is "after tab" if a tab-switch/refocus event landed in the 15s
    # right before it — i.e. the student just came back from somewhere else.
    paste_after_tab = 0
    for pt in paste_timestamps:
        if any(0 <= (pt - tt).total_seconds() <= 15 for tt in tab_event_timestamps):
            paste_after_tab += 1

    out = dict(counts)
    out["total_unfocused_seconds"] = round(total_unfocused_seconds, 1)
    out["large_paste_count"] = sum(1 for c in paste_sizes if c >= 50)
    out["small_paste_count"] = sum(1 for c in paste_sizes if c < 50)
    out["avg_paste_size"] = round(sum(paste_sizes) / len(paste_sizes), 1) if paste_sizes else 0.0
    out["paste_after_tab"] = paste_after_tab
    return out
