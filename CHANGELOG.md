# Change Log

All notable changes to the "code-monitor" extension will be documented in this file.

Check [Keep a Changelog](http://keepachangelog.com/) for recommendations on how to structure this file.

## [Unreleased]

- Initial release

## [1.0.6]

- Feat: independent per-file scoring sessions — each open file tracks its own snapshot/score history, keyed by filename, instead of one shared global session
- Feat: new "Code Monitor" activity-bar sidebar listing every tracked file with its latest AI-suspicion score
- Fix: raised PASTE_CHAR_THRESHOLD from 10 to 50 chars (10 was too sensitive to normal typing)

## [1.0.3]

- Fix: same-file paste now scores +2 instead of +15 (external paste unchanged)
- Fix: screen_freeze_events is now a running counter, not a derived value

## [1.0.4]

- Perf: debounce text-change (500ms batch), selection (300ms) and scroll (1000ms) listeners; raised FREEZE_CHECK_INTERVAL_MS to 10000 and HEARTBEAT_INTERVAL_MS to 60000
- Fix: collapse rapid duplicate Enter-key events arriving within 200ms of each other
- Fix: skip sending a code snapshot identical to the last one
- Perf: slimmed the AI-detection signals payload — dropped code_snapshots, is_window_focused and is_idle
- Feature: added code_tokens_hash (order-independent token-set hash) to snapshots and signals; backend now exposes GET /admin/similarity-report with a Similarity Report button in the teacher dashboard