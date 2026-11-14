const vscode = require('vscode');
const https  = require('https');

// ══════════════════════════════════════════════════════════════
// CODE MONITOR v4.0 — AUTOMATED TECHNICAL SCREENING PLATFORM
// ══════════════════════════════════════════════════════════════

const CONFIG = {
    SERVER_HOST:               'screening-platform-tpku.onrender.com',
    SERVER_PORT:               443,
    SNAPSHOT_INTERVAL_MS:      60000,   // Send code every 60 seconds
    HEARTBEAT_INTERVAL_MS:     60000,   // Heartbeat every 60 seconds
    LAB_STATUS_INTERVAL_MS:    30000,   // Ask the server if a lab is running
    IDLE_TIMEOUT_MS:           60000,   // 60 seconds no typing = idle
    PASTE_CHAR_THRESHOLD:      50,      // 50+ chars at once = paste (~1 meaningful line; 10 was too sensitive to normal typing)
    RAPID_TAB_COUNT:           3,       // 3 switches = suspicious
    RAPID_TAB_WINDOW_MS:       30000,   // within 30 seconds
    AI_WATCHING_SPEED:         8,       // chars/sec = fast typing
    BURST_WINDOW_MS:           3000,    // 3 second window
    LONG_ABSENCE_THRESHOLD_MS: 30000,   // 30 seconds away = suspicious

    // ── New feature thresholds ──────────────────────────────────
    PAUSE_THRESHOLD_MS:        3000,    // gap between keystrokes = a "pause"
    FREEZE_CHECK_INTERVAL_MS:  10000,   // how often to check for screen freeze
    FREEZE_THRESHOLD_MS:       20000,   // focused + silent this long = freeze
    CODE_JUMP_LINE_THRESHOLD:  50,      // cursor moving >50 lines instantly
    LARGE_DELETION_THRESHOLD:  30,      // chars removed at once
    KPM_SAMPLE_INTERVAL_MS:    30000,   // how often to sample keys/min
    KEYSTROKE_HISTORY_LIMIT:   200,     // max intervals kept for stats
    SESSION_BREAK_THRESHOLD_MS: 120000, // 2 min silent = a "session break"
    SKILL_JUMP_SCORE_DELTA:    30,      // ai_score jump between snapshots
    RENAME_MIN_EDITS:          2,       // simultaneous edits = rename op
    EVENT_BATCH_INTERVAL_MS:   120000,  // low-priority behavioral events: flush queue every 2 min
};

// ══════════════════════════════════════════════════════════════
// GLOBAL STATE — original 16-feature state
// ══════════════════════════════════════════════════════════════
let studentId           = null;
let jwtToken            = null;
let isConnected         = false;
let statusBarItem       = null;
let heartbeatTimer      = null;
let idleTimer           = null;
let keystrokeTimestamps = [];
let lastTypingTime      = null;
let isIdle              = false;
let tabSwitchTimestamps = [];
let isWindowFocused     = true;
let windowBlurTime      = null;
let suspiciousScore     = 0;
let serverReachable     = true;
let sessionStats        = {
    totalKeystrokes:  0,
    totalPastes:      0,
    totalTabSwitches: 0,
    totalIdleEvents:  0,
    codeSnapshots:    0,
    suspiciousEvents: [],
    sessionStart:     null,
};

// ══════════════════════════════════════════════════════════════
// GLOBAL STATE — 18 new behavioral-tracking features
// ══════════════════════════════════════════════════════════════
let backspaceCount        = 0;                 // Feature: Backspace Frequency
let keystrokeIntervals    = [];                 // Feature: Keystroke Rhythm / Time Between Keystrokes
let lastKeystrokeAt       = null;
let kpmSamples            = [];                 // Feature: Keys Per Minute Over Time
let kpmTimer              = null;
let pauseEvents           = [];                 // Feature: Typing Pause Patterns
let freezeTimer           = null;               // Feature: Screen Freeze Detection
let labTimer              = null;               // Polls /student/lab-status
let examMode              = false;              // true while the student's class has a lab running
let activeLab             = null;               // { id, course_name, section, ... } from the server
let lastActivityAt        = null;
let lastCursorLine        = null;               // Feature: Sudden Code Jump
let codeJumpCount         = 0;
let deletionEvents        = [];                 // Feature: Code Deletion Pattern
let undoCount             = 0;                  // Feature: Undo/Redo Frequency
let redoCount             = 0;
let screenFreezeCount     = 0;                  // Feature: Screen Freeze Detection (running count)
let copyFromSameFileCount = 0;                  // Feature: Copy From Same File
let renameEventCount      = 0;                  // Feature: Variable Rename Detection
let skillJumpEvents       = [];                 // Feature: Sudden Skill Jump
let previousSnapshotScore = null;
let lineAddTimestamps     = [];                 // Feature: Time Per Line Ratio
let lastLineCount         = 0;
let sessionBreaks         = [];                 // Feature: Session Break Pattern
let scrollEvents          = [];                 // Feature: Scroll Behavior
let lastVisibleRangeLine  = null;
let speedComplexitySamples = [];                // Feature: Typing Speed vs Complexity
let lastCodeFingerprint   = null;               // Feature: Same Code As Classmate
let lastAIDetectionResult = null;
let lastCodeTokensHash    = null;               // Feature: Token-set hash for similarity report

// ══════════════════════════════════════════════════════════════
// GLOBAL STATE — debounce timers / batching (perf fixes)
// ══════════════════════════════════════════════════════════════
let keystrokeDebounceTimer  = null;
let selectionDebounceTimer  = null;
let scrollDebounceTimer     = null;
let pendingTextChangeEvents = [];               // buffered events awaiting the 500ms batch flush
let lastEnterTime           = 0;                // Enter-key batching (collapse rapid duplicate Enters)
let lastSnapshotCode        = '';               // skip sending a snapshot identical to the last one
let lastSnapshotLines       = null;             // array of lines from the last snapshot sent (for delta encoding)
let linesChangedSinceSnapshot = 0;              // smart trigger: added+removed lines since the last snapshot
let eventQueue              = [];               // low-priority behavioral events awaiting the 2-min batch flush
let batchTimer              = null;

// ══════════════════════════════════════════════════════════════
// ACTIVATE
// ══════════════════════════════════════════════════════════════
function activate(context) {
    console.log('Code Monitor v4.0 Active!');

    // Create status bar
    statusBarItem = vscode.window.createStatusBarItem(
        vscode.StatusBarAlignment.Left, 100
    );
    updateStatusBar('disconnected');
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    // ══════════════════════════════════════════════════════════
    // COMMAND: LOGIN
    // ══════════════════════════════════════════════════════════
    let loginCommand = vscode.commands.registerCommand(
        'code-monitor.login',
        async () => {
            if (isConnected) {
                vscode.window.showInformationMessage(
                    `Already connected as ${studentId}`
                );
                return;
            }

            // Ask Student ID
            const id = await vscode.window.showInputBox({
                prompt:       '🎓 Enter your Student ID',
                placeHolder:  'e.g. 2023-2-60-148',
                validateInput: (val) => {
                    if (!val || val.trim().length < 3)
                        return 'Student ID is too short!';
                    return null;
                }
            });
            if (!id) return;

            // Ask login mode: normal or guest
            const loginMode = await vscode.window.showQuickPick(
                [
                    { label: '🔒 Normal Login', description: 'Student ID + Password', value: 'normal' },
                    { label: '🎓 Guest Login',  description: 'Student ID only (lab mode)', value: 'guest' },
                ],
                { placeHolder: 'Select login method', canPickMany: false }
            );
            if (!loginMode) return;

            let loginResult;
            if (loginMode.value === 'guest') {
                // Guest login — ID only
                updateStatusBar('loading');
                vscode.window.showInformationMessage('🔄 Connecting as guest...');
                loginResult = await callGuestLoginAPI(id.trim());
            } else {
                // Normal login — ask password
                const password = await vscode.window.showInputBox({
                    prompt:      '🔒 Enter your Exam Password',
                    placeHolder: 'Enter your password',
                    password:    true,
                    validateInput: (val) => {
                        if (!val || val.trim().length < 3)
                            return 'Password is too short!';
                        return null;
                    }
                });
                if (!password) return;

                updateStatusBar('loading');
                vscode.window.showInformationMessage('🔄 Verifying credentials...');
                loginResult = await callLoginAPI(id.trim(), password);
            }

            if (!loginResult.success) {
                updateStatusBar('disconnected');
                vscode.window.showErrorMessage(`❌ ${loginResult.message}`);
                return;
            }

            // Credentials verified!
            jwtToken        = loginResult.token;
            studentId       = id.trim();
            isConnected     = true;
            suspiciousScore = 0;
            serverReachable = true;

            sessionStats = {
                totalKeystrokes:  0,
                totalPastes:      0,
                totalTabSwitches: 0,
                totalIdleEvents:  0,
                codeSnapshots:    0,
                suspiciousEvents: [],
                sessionStart:     new Date().toISOString(),
            };
            resetBehavioralState();

            updateStatusBar('connected');

            sendToServer('/student/connect', {
                student_id:  studentId,
                timestamp:   new Date().toISOString(),
                device_info: {
                    platform:       process.platform,
                    vscode_version: vscode.version,
                }
            });

            vscode.window.showInformationMessage(
                `✅ Connected as: ${studentId} — Exam monitoring ACTIVE.`
            );

            startMonitoring();
        }
    );

    // ══════════════════════════════════════════════════════════
    // COMMAND: LOGOUT
    // ══════════════════════════════════════════════════════════
    let logoutCommand = vscode.commands.registerCommand(
        'code-monitor.logout',
        async () => {
            if (!isConnected) {
                vscode.window.showWarningMessage('You are not logged in.');
                return;
            }

            const confirm = await vscode.window.showWarningMessage(
                '⚠️ Are you sure you want to disconnect from the exam?',
                'Yes, Logout', 'Cancel'
            );
            if (confirm !== 'Yes, Logout') return;

            flushEventQueue();

            sendToServer('/student/disconnect', {
                student_id: studentId,
                timestamp:  new Date().toISOString(),
            });

            stopMonitoring();
            isConnected = false;
            studentId   = null;
            jwtToken    = null;
            updateStatusBar('disconnected');

            vscode.window.showInformationMessage(
                '👋 Logged out. Session report submitted.'
            );
        }
    );

    // ══════════════════════════════════════════════════════════
    // COMMAND: STATUS
    // ══════════════════════════════════════════════════════════
    let statusCommand = vscode.commands.registerCommand(
        'code-monitor.status',
        () => {
            if (!isConnected) {
                vscode.window.showInformationMessage(
                    '❌ Not connected. Use: Code Monitor: Login to Exam'
                );
                return;
            }
            vscode.window.showInformationMessage(
                `📊 Student: ${studentId} | ` +
                `Session: ${getSessionDuration()} | ` +
                `Keystrokes: ${sessionStats.totalKeystrokes} | ` +
                `Pastes: ${sessionStats.totalPastes} | ` +
                `Tab Switches: ${sessionStats.totalTabSwitches} | ` +
                `⚠️ Score: ${suspiciousScore}/100`
            );
        }
    );

    // ══════════════════════════════════════════════════════════
    // COMMAND: ANALYZE CODE — manual AI-detection trigger
    // ══════════════════════════════════════════════════════════
    let analyzeCommand = vscode.commands.registerCommand(
        'code-monitor.analyzeCode',
        async () => {
            if (!isConnected) {
                vscode.window.showWarningMessage(
                    '❌ Not connected. Use: Code Monitor: Login to Exam'
                );
                return;
            }
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                vscode.window.showWarningMessage('No active editor to analyze.');
                return;
            }
            vscode.window.showInformationMessage('🔎 Analyzing code for AI usage...');
            await runAIDetection(editor, 'MANUAL_COMMAND');
        }
    );

    // ══════════════════════════════════════════════════════════
    // LISTENER: Text Changes
    // Detects: paste, AI-watching (fast typing), backspace frequency,
    // keystroke rhythm, typing pauses, deletion patterns, undo/redo,
    // copy-from-same-file, variable rename, sudden skill jump inputs
    // ══════════════════════════════════════════════════════════
    let textChangeListener = vscode.workspace.onDidChangeTextDocument(
        (event) => {
            if (!isConnected) return;

            // Allow both saved files AND untitled documents
            const scheme = event.document.uri.scheme;
            if (scheme !== 'file' && scheme !== 'untitled') return;

            // ── Debounce / batch: buffer events, flush at most once per
            // 500ms so a fast typist doesn't run the full handler on every
            // single keystroke. Every event is kept (pushed to the buffer)
            // so no data is lost — they're just processed together.
            pendingTextChangeEvents.push(event);
            if (keystrokeDebounceTimer) clearTimeout(keystrokeDebounceTimer);
            keystrokeDebounceTimer = setTimeout(() => {
                const events = pendingTextChangeEvents;
                pendingTextChangeEvents = [];
                keystrokeDebounceTimer = null;
                events.forEach(processTextChangeEvent);
                events.forEach(trackSmartSnapshotTrigger);
                if (linesChangedSinceSnapshot >= 5) {
                    sendCodeSnapshot();
                }
            }, 500);
        }
    );

    // ── Smart snapshot trigger: count added+removed lines since the
    // last snapshot (from the raw edit ranges, not a full-document
    // diff — cheap enough to run on every batch of edits). ─────────
    function trackSmartSnapshotTrigger(event) {
        for (const change of event.contentChanges) {
            const removedLines = change.range.end.line - change.range.start.line;
            const addedLines    = (change.text.match(/\n/g) || []).length;
            linesChangedSinceSnapshot += removedLines + addedLines;
        }
    }

    function processTextChangeEvent(event) {
            const now = Date.now();
            lastActivityAt = now;

            // ── Enter key batching ──────────────────────────────
            // Auto-indent / multi-cursor Enter can fire several rapid
            // near-duplicate newline-only events; collapse any that arrive
            // within 200ms of the previous one.
            if (event.contentChanges.length === 1) {
                const soleText = event.contentChanges[0].text;
                if (soleText === '\n' || soleText === '\r\n') {
                    if (now - lastEnterTime < 200) {
                        return;
                    }
                    lastEnterTime = now;
                }
            }

            // ── Aggregate ALL changes in this event ────────────
            // VS Code sometimes splits a paste into multiple small
            // contentChanges so we sum them ALL up
            let totalCharsInEvent = 0;
            let totalCharsDeleted = 0;
            let pastePreview      = '';

            for (const change of event.contentChanges) {
                const len = change.text.length;

                // ── Backspace / deletion tracking ───────────────
                if (change.rangeLength > 0 && len === 0) {
                    backspaceCount++;
                    totalCharsDeleted += change.rangeLength;
                } else if (change.rangeLength > 0 && len < change.rangeLength) {
                    // A replace that nets a deletion (e.g. select+type)
                    totalCharsDeleted += (change.rangeLength - len);
                }

                if (len === 0) continue;

                totalCharsInEvent += len;
                sessionStats.totalKeystrokes += len;
                keystrokeTimestamps.push({ time: now, length: len });

                if (pastePreview.length < 120) {
                    pastePreview += change.text;
                }
            }

            // ── Keystroke Rhythm / Time Between Keystrokes ──────
            if (lastKeystrokeAt !== null && totalCharsInEvent > 0) {
                const interval = now - lastKeystrokeAt;
                keystrokeIntervals.push(interval);
                if (keystrokeIntervals.length > CONFIG.KEYSTROKE_HISTORY_LIMIT) {
                    keystrokeIntervals.shift();
                }

                // ── Typing Pause Patterns ───────────────────────
                if (interval >= CONFIG.PAUSE_THRESHOLD_MS) {
                    pauseEvents.push({ duration: interval, timestamp: now });
                    if (pauseEvents.length > 100) pauseEvents.shift();
                }
            }
            if (totalCharsInEvent > 0) lastKeystrokeAt = now;

            // ── Code Deletion Pattern ───────────────────────────
            if (totalCharsDeleted >= CONFIG.LARGE_DELETION_THRESHOLD) {
                deletionEvents.push({ chars: totalCharsDeleted, timestamp: now });
                if (deletionEvents.length > 100) deletionEvents.shift();

                sendToServer('/student/event', {
                    student_id: studentId,
                    event_type: 'LARGE_DELETION',
                    severity:   'MEDIUM',
                    details: {
                        chars_deleted: totalCharsDeleted,
                        message:       'Large block of code deleted',
                    },
                    timestamp: new Date().toISOString(),
                });
            }

            // ── Undo / Redo Frequency ───────────────────────────
            if (event.reason === vscode.TextDocumentChangeReason.Undo) {
                undoCount++;
            } else if (event.reason === vscode.TextDocumentChangeReason.Redo) {
                redoCount++;
            }

            // ── Variable Rename Detection ───────────────────────
            // VS Code's "Rename Symbol" (F2) applies several
            // simultaneous edits with identical replacement text
            // inside a single change event.
            if (event.contentChanges.length >= CONFIG.RENAME_MIN_EDITS) {
                const texts = event.contentChanges
                    .map(c => c.text)
                    .filter(t => /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(t));
                if (texts.length >= CONFIG.RENAME_MIN_EDITS &&
                    texts.every(t => t === texts[0])) {
                    renameEventCount++;
                    queueBehavioralEvent('VARIABLE_RENAME_DETECTED', 'INFO', {
                        new_name:    texts[0],
                        occurrences: texts.length,
                    });
                }
            }

            if (totalCharsInEvent === 0) return;

            keystrokeTimestamps = keystrokeTimestamps.filter(
                k => now - k.time < 30000
            );
            lastTypingTime = now;

            // ── DETECT: Paste ──────────────────────────────────
            // Threshold: 10 characters at once = paste detected
            if (totalCharsInEvent >= CONFIG.PASTE_CHAR_THRESHOLD) {
                sessionStats.totalPastes++;
                const preview = pastePreview.substring(0, 120);

                sessionStats.suspiciousEvents.push({
                    type:      'PASTE_DETECTED',
                    chars:     totalCharsInEvent,
                    timestamp: new Date().toISOString(),
                });

                // ── Copy From Same File ─────────────────────────
                // Reconstruct the document text as it was BEFORE this paste
                // was applied, then check whether the exact pasted text
                // already existed there. Only exact for a single contiguous
                // edit (the normal paste case); for the rare multi-part
                // paste VS Code sometimes splits into several contentChanges,
                // fall back to the post-edit document (a block pasted from
                // elsewhere in THIS file still duplicates text outside the
                // newly inserted range either way).
                const fullText = event.document.getText();
                let preEditText = fullText;
                if (event.contentChanges.length === 1) {
                    const change = event.contentChanges[0];
                    preEditText = fullText.slice(0, change.rangeOffset)
                        + fullText.slice(change.rangeOffset + change.text.length);
                }
                const pastedText = pastePreview.trim();
                const isCopyFromSameFile = pastedText.length > 5 && preEditText.includes(pastedText);
                if (isCopyFromSameFile) {
                    copyFromSameFileCount++;
                }

                // Same-file paste (refactor/move within this document) is
                // far less suspicious than pasting content that didn't
                // already exist here (e.g. from a browser or another app).
                addSuspiciousScore(
                    isCopyFromSameFile ? 2 : 15,
                    isCopyFromSameFile ? 'Same-file paste detected' : 'Paste detected'
                );

                sendToServer('/student/event', {
                    student_id: studentId,
                    event_type: 'PASTE_DETECTED',
                    severity:   'HIGH',
                    details: {
                        chars_pasted:         totalCharsInEvent,
                        paste_preview:        preview,
                        total_pastes:         sessionStats.totalPastes,
                        suspicious_score:     suspiciousScore,
                        copy_from_same_file:  isCopyFromSameFile,
                    },
                    timestamp: new Date().toISOString(),
                });

                vscode.window.showWarningMessage(
                    `⚠️ Paste detected (${totalCharsInEvent} chars) — Admin notified!`
                );
            }

            // ── DETECT: Fast burst typing (AI watching) ────────
            const recent = keystrokeTimestamps.filter(
                k => now - k.time < CONFIG.BURST_WINDOW_MS
            );
            const recentChars = recent.reduce((s, k) => s + k.length, 0);
            const speed       = recentChars / (CONFIG.BURST_WINDOW_MS / 1000);

            if (speed > CONFIG.AI_WATCHING_SPEED && recentChars > 30) {
                sendToServer('/student/event', {
                    student_id: studentId,
                    event_type: 'FAST_BURST_TYPING',
                    severity:   'MEDIUM',
                    details: {
                        speed_chars_per_sec: speed.toFixed(2),
                        chars_in_burst:      recentChars,
                        message:             'Unusually fast typing — possible AI watching',
                    },
                    timestamp: new Date().toISOString(),
                });
            }

            // ── Typing Speed vs Complexity ──────────────────────
            const complexity = estimateComplexity(event.document.getText());
            speedComplexitySamples.push({ speed, complexity, timestamp: now });
            if (speedComplexitySamples.length > 100) speedComplexitySamples.shift();

            // ── Time Per Line Ratio ─────────────────────────────
            const currentLineCount = event.document.lineCount;
            if (currentLineCount !== lastLineCount) {
                lineAddTimestamps.push({ lines: currentLineCount, timestamp: now });
                if (lineAddTimestamps.length > 200) lineAddTimestamps.shift();
                lastLineCount = currentLineCount;
            }

            resetIdleTimer();
    }

    // ══════════════════════════════════════════════════════════
    // LISTENER: Cursor / Selection changes
    // Detects: Sudden Code Jump
    // Debounced 300ms — selection events can fire very rapidly during
    // drag-selection / multi-cursor operations.
    // ══════════════════════════════════════════════════════════
    let selectionChangeListener = vscode.window.onDidChangeTextEditorSelection(
        (event) => {
            if (!isConnected) return;
            if (selectionDebounceTimer) clearTimeout(selectionDebounceTimer);
            selectionDebounceTimer = setTimeout(() => {
                selectionDebounceTimer = null;
                processSelectionChange(event);
            }, 300);
        }
    );

    function processSelectionChange(event) {
            const now = Date.now();
            lastActivityAt = now;

            const line = event.selections[0]?.active.line;
            if (line === undefined) return;

            if (lastCursorLine !== null) {
                const jump = Math.abs(line - lastCursorLine);
                if (jump >= CONFIG.CODE_JUMP_LINE_THRESHOLD) {
                    codeJumpCount++;
                    queueBehavioralEvent('SUDDEN_CODE_JUMP', 'LOW', {
                        from_line: lastCursorLine,
                        to_line:   line,
                        lines_jumped: jump,
                    });
                }
            }
            lastCursorLine = line;
    }

    // ══════════════════════════════════════════════════════════
    // LISTENER: Scroll Behavior
    // Debounced 1000ms — scrolling fires very frequently.
    // ══════════════════════════════════════════════════════════
    let scrollListener = vscode.window.onDidChangeTextEditorVisibleRanges(
        (event) => {
            if (!isConnected) return;
            if (scrollDebounceTimer) clearTimeout(scrollDebounceTimer);
            scrollDebounceTimer = setTimeout(() => {
                scrollDebounceTimer = null;
                processScrollChange(event);
            }, 1000);
        }
    );

    function processScrollChange(event) {
            const now = Date.now();
            const topLine = event.visibleRanges[0]?.start.line;
            if (topLine === undefined) return;

            if (lastVisibleRangeLine !== null) {
                scrollEvents.push({
                    delta: topLine - lastVisibleRangeLine,
                    timestamp: now,
                });
                if (scrollEvents.length > 200) scrollEvents.shift();
            }
            lastVisibleRangeLine = topLine;
    }

    // ══════════════════════════════════════════════════════════
    // LISTENER: Window Focus
    // Detects: tab switch, rapid switching, long absence, session breaks
    // ══════════════════════════════════════════════════════════
    let windowFocusListener = vscode.window.onDidChangeWindowState(
        (windowState) => {
            if (!isConnected) return;
            const now = Date.now();

            if (!windowState.focused) {
                // Student LEFT VS Code
                isWindowFocused = false;
                windowBlurTime  = now;
                tabSwitchTimestamps.push(now);
                sessionStats.totalTabSwitches++;

                tabSwitchTimestamps = tabSwitchTimestamps.filter(
                    t => now - t < CONFIG.RAPID_TAB_WINDOW_MS
                );

                // DETECT: Rapid Tab Switching
                if (tabSwitchTimestamps.length >= CONFIG.RAPID_TAB_COUNT) {
                    addSuspiciousScore(20, 'Rapid tab switching');

                    sendToServer('/student/event', {
                        student_id: studentId,
                        event_type: 'RAPID_TAB_SWITCHING',
                        severity:   'HIGH',
                        details: {
                            switches_in_window: tabSwitchTimestamps.length,
                            total_switches:     sessionStats.totalTabSwitches,
                            message:            'Rapid tab switching — highly suspicious!',
                        },
                        timestamp: new Date().toISOString(),
                    });

                    vscode.window.showErrorMessage(
                        '🚨 Rapid tab switching detected! Admin has been notified.'
                    );
                } else {
                    // Single tab switch
                    queueBehavioralEvent('WINDOW_UNFOCUSED', 'LOW', {
                        total_switches: sessionStats.totalTabSwitches,
                        message:        'Student left VS Code window',
                    });
                }

                updateStatusBar('suspicious');

            } else {
                // Student CAME BACK to VS Code
                isWindowFocused   = true;
                const awayMs      = windowBlurTime ? now - windowBlurTime : 0;
                const awaySecs    = (awayMs / 1000).toFixed(1);

                queueBehavioralEvent('WINDOW_REFOCUSED', 'INFO', {
                    away_duration_seconds: awaySecs,
                    message:               `Student returned after ${awaySecs}s`,
                });

                // DETECT: Long Absence
                if (awayMs > CONFIG.LONG_ABSENCE_THRESHOLD_MS) {
                    addSuspiciousScore(10, `Long absence: ${awaySecs}s`);

                    sendToServer('/student/event', {
                        student_id: studentId,
                        event_type: 'LONG_ABSENCE',
                        severity:   'MEDIUM',
                        details: {
                            away_duration_seconds: awaySecs,
                            message:               `Student was away for ${awaySecs} seconds!`,
                        },
                        timestamp: new Date().toISOString(),
                    });
                }

                // ── Session Break Pattern ───────────────────────
                if (awayMs >= CONFIG.SESSION_BREAK_THRESHOLD_MS) {
                    sessionBreaks.push({ duration_ms: awayMs, timestamp: now });
                    if (sessionBreaks.length > 50) sessionBreaks.shift();
                }

                updateStatusBar('connected');
                windowBlurTime = null;
            }
        }
    );

    // ══════════════════════════════════════════════════════════
    // LISTENER: File Switch
    // ══════════════════════════════════════════════════════════
    let editorChangeListener = vscode.window.onDidChangeActiveTextEditor(
        (editor) => {
            if (!isConnected || !editor) return;

            lastCursorLine       = null;
            lastVisibleRangeLine = null;
            lastLineCount        = editor.document.lineCount;

            queueBehavioralEvent('FILE_SWITCHED', 'INFO', {
                new_file: editor.document.fileName,
                language: editor.document.languageId,
            });
        }
    );

    // Register all commands and listeners
    context.subscriptions.push(
        loginCommand,
        logoutCommand,
        statusCommand,
        analyzeCommand,
        textChangeListener,
        selectionChangeListener,
        scrollListener,
        windowFocusListener,
        editorChangeListener,
    );
}

// ══════════════════════════════════════════════════════════════
// START / STOP MONITORING
// ══════════════════════════════════════════════════════════════
function startMonitoring() {
    heartbeatTimer = setInterval(sendHeartbeat, CONFIG.HEARTBEAT_INTERVAL_MS);
    kpmTimer       = setInterval(sampleKeysPerMinute, CONFIG.KPM_SAMPLE_INTERVAL_MS);
    freezeTimer    = setInterval(checkScreenFreeze, CONFIG.FREEZE_CHECK_INTERVAL_MS);
    labTimer       = setInterval(pollLabStatus, CONFIG.LAB_STATUS_INTERVAL_MS);
    batchTimer     = setInterval(flushEventQueue, CONFIG.EVENT_BATCH_INTERVAL_MS);
    pollLabStatus();   // don't wait 30s to learn a lab is already running
    resetIdleTimer();
}

function stopMonitoring() {
    clearInterval(heartbeatTimer);
    clearInterval(kpmTimer);
    clearInterval(freezeTimer);
    clearInterval(labTimer);
    clearInterval(batchTimer);
    clearTimeout(idleTimer);
    heartbeatTimer = kpmTimer = freezeTimer = idleTimer = labTimer = batchTimer = null;
    examMode  = false;
    activeLab = null;
}

function resetBehavioralState() {
    backspaceCount          = 0;
    keystrokeIntervals      = [];
    lastKeystrokeAt         = null;
    kpmSamples              = [];
    pauseEvents             = [];
    lastActivityAt          = Date.now();
    lastCursorLine          = null;
    codeJumpCount           = 0;
    deletionEvents          = [];
    undoCount               = 0;
    redoCount               = 0;
    screenFreezeCount       = 0;
    copyFromSameFileCount   = 0;
    renameEventCount        = 0;
    skillJumpEvents         = [];
    previousSnapshotScore   = null;
    lineAddTimestamps       = [];
    lastLineCount           = 0;
    sessionBreaks           = [];
    scrollEvents            = [];
    lastVisibleRangeLine    = null;
    speedComplexitySamples  = [];
    lastCodeFingerprint     = null;
    lastAIDetectionResult   = null;
    lastCodeTokensHash      = null;
    lastSnapshotCode        = '';
    lastSnapshotLines       = null;
    linesChangedSinceSnapshot = 0;
    eventQueue              = [];
}

// ══════════════════════════════════════════════════════════════
// CALL BACKEND LOGIN API
// ══════════════════════════════════════════════════════════════
function callGuestLoginAPI(sid) {
    return new Promise((resolve) => {
        const body    = JSON.stringify({ student_id: sid });
        const options = {
            hostname: CONFIG.SERVER_HOST,
            port:     CONFIG.SERVER_PORT,
            path:     '/auth/guest-login',
            method:   'POST',
            headers: {
                'Content-Type':   'application/json',
                'Content-Length': Buffer.byteLength(body),
            },
        };

        const req = https.request(options, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    const parsed = JSON.parse(data);
                    if (res.statusCode === 200) {
                        resolve({ success: true, token: parsed.access_token, name: parsed.full_name });
                    } else {
                        resolve({ success: false, message: parsed.detail || 'Guest login failed' });
                    }
                } catch (e) {
                    resolve({ success: false, message: 'Server returned invalid response' });
                }
            });
        });

        req.on('error', (err) => {
            resolve({ success: false, message: err.code === 'ECONNREFUSED' ? 'Cannot connect to server.' : `Connection error: ${err.message}` });
        });
        req.setTimeout(8000, () => { req.destroy(); resolve({ success: false, message: 'Server timeout.' }); });
        req.write(body);
        req.end();
    });
}

function callLoginAPI(sid, password) {
    return new Promise((resolve) => {
        const body    = JSON.stringify({ student_id: sid, password: password });
        const options = {
            hostname: CONFIG.SERVER_HOST,
            port:     CONFIG.SERVER_PORT,
            path:     '/auth/login',
            method:   'POST',
            headers: {
                'Content-Type':   'application/json',
                'Content-Length': Buffer.byteLength(body),
            },
        };

        const req = https.request(options, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    const parsed = JSON.parse(data);
                    if (res.statusCode === 200) {
                        resolve({
                            success: true,
                            token:   parsed.access_token,
                            name:    parsed.full_name,
                        });
                    } else {
                        resolve({
                            success: false,
                            message: parsed.detail || 'Login failed',
                        });
                    }
                } catch (e) {
                    resolve({
                        success: false,
                        message: 'Server returned invalid response'
                    });
                }
            });
        });

        req.on('error', (err) => {
            if (err.code === 'ECONNREFUSED') {
                resolve({
                    success: false,
                    message: 'Cannot connect to server. Make sure backend is running!'
                });
            } else {
                resolve({
                    success: false,
                    message: `Connection error: ${err.message}`
                });
            }
        });

        req.setTimeout(8000, () => {
            req.destroy();
            resolve({
                success: false,
                message: 'Server timeout. Backend not responding.'
            });
        });

        req.write(body);
        req.end();
    });
}

// ══════════════════════════════════════════════════════════════
// LINE-LEVEL DIFF (git-style delta snapshots)
// Produces an ordered edit script — hunks of {start, removed, added}
// positioned against oldLines — so the backend can deterministically
// reconstruct the new full text. Returns null if the document is too
// large to diff cheaply; caller falls back to sending the full code.
// ══════════════════════════════════════════════════════════════
function diffLines(oldLines, newLines) {
    const n = oldLines.length, m = newLines.length;
    if (n * m > 2000000) return null;

    const dp = new Array(n + 1);
    for (let i = 0; i <= n; i++) dp[i] = new Int32Array(m + 1);
    for (let i = n - 1; i >= 0; i--) {
        for (let j = m - 1; j >= 0; j--) {
            dp[i][j] = oldLines[i] === newLines[j]
                ? dp[i + 1][j + 1] + 1
                : Math.max(dp[i + 1][j], dp[i][j + 1]);
        }
    }

    const ops = [];
    let i = 0, j = 0;
    while (i < n || j < m) {
        if (i < n && j < m && oldLines[i] === newLines[j]) {
            i++; j++;
            continue;
        }
        const start = i;
        let removed = 0;
        const added = [];
        while (i < n || j < m) {
            if (i < n && j < m && oldLines[i] === newLines[j]) break;
            if (i < n && (j >= m || dp[i + 1][j] >= dp[i][j + 1])) {
                removed++; i++;
            } else if (j < m) {
                added.push(newLines[j]); j++;
            } else {
                break;
            }
        }
        ops.push({ start, removed, added });
    }
    return ops;
}

// ══════════════════════════════════════════════════════════════
// SEND CODE SNAPSHOT — triggered once 5+ lines have changed
// This counts as a "code submission", so it also triggers AI detection.
// ══════════════════════════════════════════════════════════════
function sendCodeSnapshot() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;

    const code = editor.document.getText();
    if (!code || code.trim().length === 0) return;

    // Skip sending a snapshot that is identical to the last one.
    if (code.trim() === lastSnapshotCode.trim()) return;
    lastSnapshotCode = code;

    const analysis = analyzeCodeForAIPatterns(code);
    sessionStats.codeSnapshots++;

    // ── Sudden Skill Jump ────────────────────────────────────
    if (previousSnapshotScore !== null) {
        const delta = analysis.ai_score - previousSnapshotScore;
        if (Math.abs(delta) >= CONFIG.SKILL_JUMP_SCORE_DELTA) {
            skillJumpEvents.push({ delta, timestamp: Date.now() });
            if (skillJumpEvents.length > 50) skillJumpEvents.shift();

            sendToServer('/student/event', {
                student_id: studentId,
                event_type: 'SUDDEN_SKILL_JUMP',
                severity:   'MEDIUM',
                details: {
                    previous_score: previousSnapshotScore,
                    current_score:  analysis.ai_score,
                    delta:          delta,
                    message:        'Sudden change in code sophistication between snapshots',
                },
                timestamp: new Date().toISOString(),
            });
        }
    }
    previousSnapshotScore = analysis.ai_score;

    const lines = code.split('\n');
    let codeField  = code;
    let deltaField = undefined;
    if (lastSnapshotLines !== null) {
        const ops = diffLines(lastSnapshotLines, lines);
        if (ops !== null) {
            codeField  = undefined;
            deltaField = { ops };
        }
    }
    lastSnapshotLines         = lines;
    linesChangedSinceSnapshot = 0;

    sendToServer('/student/submit-code', {
        student_id:      studentId,
        filename:        editor.document.fileName,
        language:        editor.document.languageId,
        code:            codeField,
        delta:           deltaField,
        line_count:      lines.length,
        analysis:        analysis,
        code_fingerprint: computeCodeFingerprint(code), // Same Code As Classmate (server-side match)
        code_tokens_hash: computeCodeTokensHash(code),  // Similarity Report (token-set hash)
        snapshot_number: sessionStats.codeSnapshots,
        timestamp:       new Date().toISOString(),
    });

    console.log(`📸 Snapshot #${sessionStats.codeSnapshots} sent`);

    // A periodic snapshot counts as a code submission — run AI detection.
    runAIDetection(editor, 'AUTO_SNAPSHOT');
}

// ══════════════════════════════════════════════════════════════
// AI CODE PATTERN ANALYSIS (static heuristics on the code text)
// ══════════════════════════════════════════════════════════════
function analyzeCodeForAIPatterns(code) {
    const lines = code.split('\n');

    // Check 1: Comment ratio (AI over-comments)
    const commentLines = lines.filter(l =>
        l.trim().startsWith('//') ||
        l.trim().startsWith('#')  ||
        l.trim().startsWith('/*') ||
        l.trim().startsWith('*')
    ).length;
    const commentRatio = lines.length > 0 ? commentLines / lines.length : 0;

    // Check 2: AI-style variable names
    const aiVarPattern = /\b(result|output|data|value|item|element|node|temp|current|previous|index|count|total|sum|average|maximum|minimum|iterator|container|handler|callback|response|request|target|source|payload|buffer|cache|queue|stack|heap)\b/g;
    const aiVarCount   = (code.match(aiVarPattern) || []).length;

    // Check 3: Perfect empty line formatting
    const emptyLines     = lines.filter(l => l.trim() === '').length;
    const emptyLineRatio = lines.length > 0 ? emptyLines / lines.length : 0;

    // Check 4: Docstrings (AI loves docstrings)
    const hasDocstrings = /"""[\s\S]*?"""|\/\*\*[\s\S]*?\*\//.test(code);

    // Check 5: Long descriptive function names
    const longFuncNames = (code.match(
        /def [a-z][a-z_]{10,}|function [a-z][a-zA-Z]{10,}/g
    ) || []).length;

    // Check 6: Human abbreviations (humans make shortcuts)
    const humanVarCount = (code.match(
        /\b(cnt|idx|tmp|val|arr|lst|dct|func|var|str|num|calc|res|ret|i|j|k|n|m)\b/g
    ) || []).length;

    // Build flags
    const flags = [];
    if (commentRatio > 0.3)   flags.push('EXCESSIVE_COMMENTS');
    if (aiVarCount > 5)       flags.push('AI_VARIABLE_NAMES');
    if (emptyLineRatio > 0.25) flags.push('PERFECT_FORMATTING');
    if (hasDocstrings)         flags.push('HAS_DOCSTRINGS');
    if (longFuncNames > 2)     flags.push('LONG_FUNCTION_NAMES');
    if (humanVarCount === 0 && lines.length > 10)
        flags.push('NO_HUMAN_SHORTCUTS');

    // AI suspicion score 0-100
    const aiScore = Math.min(100,
        (commentRatio > 0.3       ? 20 : 0) +
        (aiVarCount > 5           ? 20 : 0) +
        (hasDocstrings            ? 20 : 0) +
        (emptyLineRatio > 0.25    ? 15 : 0) +
        (longFuncNames > 2        ? 15 : 0) +
        (humanVarCount === 0 && lines.length > 10 ? 10 : 0)
    );

    return {
        total_lines:       lines.length,
        comment_ratio:     commentRatio.toFixed(2),
        ai_variable_count: aiVarCount,
        human_var_count:   humanVarCount,
        has_docstrings:    hasDocstrings,
        ai_flags:          flags,
        ai_score:          aiScore,
        likely_ai_written: aiScore >= 40,
    };
}

// ══════════════════════════════════════════════════════════════
// COMPLEXITY ESTIMATE — naive cyclomatic-style heuristic, used by
// the "Typing Speed vs Complexity" feature
// ══════════════════════════════════════════════════════════════
function estimateComplexity(code) {
    const branchPattern = /\b(if|else|for|while|case|catch|switch|&&|\|\|)\b/g;
    return (code.match(branchPattern) || []).length;
}

// ══════════════════════════════════════════════════════════════
// CODE FINGERPRINT — normalized hash used server-side for
// "Same Code As Classmate" cross-student comparison
// ══════════════════════════════════════════════════════════════
function computeCodeFingerprint(code) {
    const normalized = code
        .replace(/\/\/.*$/gm, '')
        .replace(/#.*$/gm, '')
        .replace(/\s+/g, '')
        .toLowerCase();

    // djb2 string hash — good enough as a similarity fingerprint,
    // the actual cross-student match is performed by the backend.
    let hash = 5381;
    for (let i = 0; i < normalized.length; i++) {
        hash = ((hash << 5) + hash + normalized.charCodeAt(i)) >>> 0;
    }
    lastCodeFingerprint = hash.toString(16);
    return lastCodeFingerprint;
}

// ══════════════════════════════════════════════════════════════
// CODE TOKENS HASH — order-independent token-set hash used by the
// backend's "Similarity Report" (catches renamed/reordered copies that
// computeCodeFingerprint's literal-text hash would miss).
// ══════════════════════════════════════════════════════════════
function computeCodeTokensHash(code) {
    const normalized = code
        .toLowerCase()
        .replace(/\/\/.*$/gm, '')
        .replace(/#.*$/gm, '')
        .replace(/[^\w\s]/g, ' ');

    const tokens = normalized.split(/\s+/).filter(t => t.length > 0).sort();
    const joined = tokens.join('');

    let hash = 5381;
    for (let i = 0; i < joined.length; i++) {
        hash = ((hash << 5) + hash + joined.charCodeAt(i)) >>> 0;
    }
    lastCodeTokensHash = hash.toString(16);
    return lastCodeTokensHash;
}

// ══════════════════════════════════════════════════════════════
// LAB STATUS / EXAM MODE — follows the teacher's "Start Lab" / "End Lab"
// ══════════════════════════════════════════════════════════════
// GET a JSON endpoint with the JWT. Resolves null on any failure so a
// network blip never flips exam mode by accident.
function getFromServer(endpoint) {
    return new Promise((resolve) => {
        const headers = {};
        if (jwtToken) headers['Authorization'] = `Bearer ${jwtToken}`;

        const req = https.request({
            hostname: CONFIG.SERVER_HOST,
            port:     CONFIG.SERVER_PORT,
            path:     endpoint,
            method:   'GET',
            headers:  headers,
        }, (res) => {
            let body = '';
            res.on('data', (chunk) => body += chunk);
            res.on('end', () => {
                if (res.statusCode !== 200) return resolve(null);
                try { resolve(JSON.parse(body)); } catch (e) { resolve(null); }
            });
            res.on('error', () => resolve(null));
        });
        req.on('error', () => resolve(null));
        req.setTimeout(5000, () => { req.destroy(); resolve(null); });
        req.end();
    });
}

function refreshStatusBar() {
    if (!isConnected) return;
    updateStatusBar(isIdle ? 'idle' : (isWindowFocused ? 'connected' : 'suspicious'));
}

function enableExamMode(lab) {
    examMode  = true;
    activeLab = lab || null;
    refreshStatusBar();
    const where = lab ? `${lab.course_name} · Section ${lab.section}` : 'your class';
    vscode.window.showInformationMessage(`📝 Exam mode ON — lab started for ${where}.`);
    sendToServer('/student/event', {
        student_id: studentId,
        event_type: 'EXAM_MODE_ENABLED',
        severity:   'INFO',
        details:    { lab_id: lab ? lab.id : null, course: lab ? lab.course_name : null },
        timestamp:  new Date().toISOString(),
    });
}

function disableExamMode() {
    const lab = activeLab;
    examMode  = false;
    activeLab = null;
    refreshStatusBar();
    vscode.window.showInformationMessage('✅ Exam mode OFF — the lab has ended.');
    sendToServer('/student/event', {
        student_id: studentId,
        event_type: 'EXAM_MODE_DISABLED',
        severity:   'INFO',
        details:    { lab_id: lab ? lab.id : null },
        timestamp:  new Date().toISOString(),
    });
}

async function pollLabStatus() {
    if (!isConnected || !jwtToken) return;
    const status = await getFromServer('/student/lab-status');
    // Ignore failures and replies that arrive after logout.
    if (!status || typeof status.active !== 'boolean' || !isConnected) return;

    if (status.active && !examMode)       enableExamMode(status.lab);
    else if (!status.active && examMode)  disableExamMode();
    else if (status.active)               activeLab = status.lab || activeLab;
}

// ══════════════════════════════════════════════════════════════
// KEYS PER MINUTE — sampled periodically
// ══════════════════════════════════════════════════════════════
function sampleKeysPerMinute() {
    if (!isConnected) return;
    const now       = Date.now();
    const windowMs  = CONFIG.KPM_SAMPLE_INTERVAL_MS;
    const recent    = keystrokeTimestamps.filter(k => now - k.time < windowMs);
    const charCount = recent.reduce((s, k) => s + k.length, 0);
    const kpm       = Math.round(charCount / (windowMs / 60000));

    kpmSamples.push({ kpm, timestamp: now });
    if (kpmSamples.length > 100) kpmSamples.shift();
}

// ══════════════════════════════════════════════════════════════
// SCREEN FREEZE DETECTION
// Window is focused but there has been zero activity (keystrokes,
// cursor movement, scrolling) for an unusually long time.
// ══════════════════════════════════════════════════════════════
function checkScreenFreeze() {
    if (!isConnected || !isWindowFocused || isIdle) return;
    if (lastActivityAt === null) return;

    const silentMs = Date.now() - lastActivityAt;
    if (silentMs >= CONFIG.FREEZE_THRESHOLD_MS) {
        screenFreezeCount++;
        sendToServer('/student/event', {
            student_id: studentId,
            event_type: 'SCREEN_FREEZE_DETECTED',
            severity:   'LOW',
            details: {
                silent_ms: silentMs,
                message:   'Window focused but no editor activity detected',
            },
            timestamp: new Date().toISOString(),
        });
        // Avoid re-firing every check until real activity resumes
        lastActivityAt = Date.now();
    }
}

// ══════════════════════════════════════════════════════════════
// BEHAVIORAL SIGNALS — aggregates all 34 tracked signals for the
// AI-detection backend call
// ══════════════════════════════════════════════════════════════
function collectBehavioralSignals() {
    const avgInterval = keystrokeIntervals.length
        ? keystrokeIntervals.reduce((a, b) => a + b, 0) / keystrokeIntervals.length
        : 0;
    const intervalVariance = keystrokeIntervals.length > 1
        ? keystrokeIntervals.reduce((s, v) => s + Math.pow(v - avgInterval, 2), 0) / keystrokeIntervals.length
        : 0;
    const intervalStdDev = Math.sqrt(intervalVariance);

    const avgKpm = kpmSamples.length
        ? kpmSamples.reduce((s, k) => s + k.kpm, 0) / kpmSamples.length
        : 0;

    const avgPause = pauseEvents.length
        ? pauseEvents.reduce((s, p) => s + p.duration, 0) / pauseEvents.length
        : 0;

    const linesPerMs = lineAddTimestamps.length > 1
        ? (lineAddTimestamps[lineAddTimestamps.length - 1].timestamp - lineAddTimestamps[0].timestamp) /
          Math.max(1, lineAddTimestamps[lineAddTimestamps.length - 1].lines - lineAddTimestamps[0].lines)
        : 0;

    const avgSpeed = speedComplexitySamples.length
        ? speedComplexitySamples.reduce((s, v) => s + v.speed, 0) / speedComplexitySamples.length
        : 0;
    const avgComplexity = speedComplexitySamples.length
        ? speedComplexitySamples.reduce((s, v) => s + v.complexity, 0) / speedComplexitySamples.length
        : 0;

    return {
        // ── Original signals ────────────────────────────────
        total_keystrokes:        sessionStats.totalKeystrokes,
        total_pastes:            sessionStats.totalPastes,
        total_tab_switches:      sessionStats.totalTabSwitches,
        total_idle_events:       sessionStats.totalIdleEvents,
        suspicious_score:        suspiciousScore,
        session_duration_ms:     sessionStats.sessionStart
            ? Date.now() - new Date(sessionStats.sessionStart).getTime() : 0,

        // ── 18 new behavioral features ──────────────────────
        backspace_frequency:        backspaceCount,
        keystroke_rhythm_stddev_ms: Math.round(intervalStdDev),
        avg_time_between_keys_ms:   Math.round(avgInterval),
        keys_per_minute_avg:        Math.round(avgKpm),
        keys_per_minute_series:     kpmSamples.slice(-20),
        typing_pause_count:         pauseEvents.length,
        typing_pause_avg_ms:        Math.round(avgPause),
        screen_freeze_events:       screenFreezeCount,
        sudden_code_jump_count:     codeJumpCount,
        code_deletion_events:       deletionEvents.length,
        code_deletion_chars_total:  deletionEvents.reduce((s, d) => s + d.chars, 0),
        undo_count:                 undoCount,
        redo_count:                 redoCount,
        copy_from_same_file_count:  copyFromSameFileCount,
        variable_rename_count:      renameEventCount,
        sudden_skill_jump_count:    skillJumpEvents.length,
        typing_speed_variance:      Math.round(intervalVariance),
        time_per_line_ms:           Math.round(linesPerMs),
        session_break_count:        sessionBreaks.length,
        scroll_event_count:         scrollEvents.length,
        typing_speed_avg_cps:       Number(avgSpeed.toFixed(2)),
        code_complexity_avg:        Number(avgComplexity.toFixed(2)),
        code_fingerprint:           lastCodeFingerprint,
        code_tokens_hash:           lastCodeTokensHash,
    };
}

// ══════════════════════════════════════════════════════════════
// AI DETECTION — sends code + all behavioral signals to backend
// ══════════════════════════════════════════════════════════════
async function runAIDetection(editor, trigger) {
    const code = editor.document.getText();
    if (!code || code.trim().length === 0) return;

    const signals = collectBehavioralSignals();

    const result = await callAIDetectionAPI({
        student_id: studentId,
        code:       code,
        filename:   editor.document.fileName,
        language:   editor.document.languageId,
        trigger:    trigger,
        signals:    signals,
        timestamp:  new Date().toISOString(),
    });

    if (!result.success) {
        console.log(`AI detection call failed: ${result.message}`);
        return;
    }

    lastAIDetectionResult = result;
    showAIDetectionResult(result);

    sendToServer('/student/event', {
        student_id: studentId,
        event_type: 'AI_DETECTION_RESULT',
        severity:   result.isAI ? 'HIGH' : 'INFO',
        details: {
            trigger:         trigger,
            ai_probability:  result.aiProbability,
            human_probability: result.humanProbability,
            verdict:         result.isAI ? 'AI' : 'HUMAN',
        },
        timestamp: new Date().toISOString(),
    });
}

function callAIDetectionAPI(payload) {
    return new Promise((resolve) => {
        const body    = JSON.stringify(payload);
        const headers = {
            'Content-Type':   'application/json',
            'Content-Length': Buffer.byteLength(body),
        };
        if (jwtToken) headers['Authorization'] = `Bearer ${jwtToken}`;

        const options = {
            hostname: CONFIG.SERVER_HOST,
            port:     CONFIG.SERVER_PORT,
            path:     '/analyze/ai-detection',
            method:   'POST',
            headers:  headers,
        };

        const req = https.request(options, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    const parsed = JSON.parse(data);
                    if (res.statusCode !== 200) {
                        resolve({ success: false, message: parsed.detail || 'AI detection failed' });
                        return;
                    }
                    const confidencePct = Number(
                        parsed.confidence ?? parsed.ai_probability ?? parsed.ai_score ?? parsed.probability ?? 0
                    ) * (parsed.confidence !== undefined ? 100 : 1);
                    const isAI = parsed.prediction !== undefined
                        ? String(parsed.prediction).toUpperCase() === 'AI'
                        : (parsed.is_ai ?? confidencePct >= 50);
                    const aiProbability    = isAI ? confidencePct : (100 - confidencePct);
                    const humanProbability = isAI ? (100 - confidencePct) : confidencePct;
                    resolve({
                        success:           true,
                        isAI:              isAI,
                        aiProbability:     aiProbability,
                        humanProbability:  humanProbability,
                    });
                } catch (e) {
                    resolve({ success: false, message: 'Invalid response from AI detection service' });
                }
            });
        });

        req.on('error', (err) => resolve({ success: false, message: err.message }));
        req.setTimeout(8000, () => {
            req.destroy();
            resolve({ success: false, message: 'AI detection request timed out' });
        });

        req.write(body);
        req.end();
    });
}

function showAIDetectionResult(result) {
    if (result.isAI) {
        statusBarItem.text  = `$(flame) 🤖 AI: ${Math.round(result.aiProbability)}%`;
        statusBarItem.color = '#F44336';
    } else {
        statusBarItem.text  = `$(person) 👤 Human: ${Math.round(result.humanProbability)}%`;
        statusBarItem.color = '#4CAF50';
    }
    statusBarItem.tooltip = 'AI Detection Result — Click for status';
    statusBarItem.command = 'code-monitor.status';

    // Return the status bar to the normal connection view after a while
    setTimeout(() => {
        if (isConnected) updateStatusBar(isIdle ? 'idle' : (isWindowFocused ? 'connected' : 'suspicious'));
    }, 8000);
}

// ══════════════════════════════════════════════════════════════
// HEARTBEAT — every 30 seconds
// ══════════════════════════════════════════════════════════════
function sendHeartbeat() {
    const editor      = vscode.window.activeTextEditor;
    const isTypingNow = lastTypingTime
        ? (Date.now() - lastTypingTime < 5000)
        : false;

    sendToServer('/student/heartbeat', {
        student_id:        studentId,
        is_typing:         isTypingNow,
        is_idle:           isIdle,
        is_window_focused: isWindowFocused,
        suspicious_score:  suspiciousScore,
        stats: {
            keystrokes:   sessionStats.totalKeystrokes,
            pastes:       sessionStats.totalPastes,
            tab_switches: sessionStats.totalTabSwitches,
            snapshots:    sessionStats.codeSnapshots,
        },
        current_language: editor ? editor.document.languageId : null,
        timestamp:        new Date().toISOString(),
    }, handleHeartbeatResponse);
}

// The teacher can force-disconnect a student; the server says so in the
// heartbeat reply ({ force_disconnect: true }).
function handleHeartbeatResponse(reply) {
    if (!reply || reply.force_disconnect !== true || !isConnected) return;

    flushEventQueue();
    stopMonitoring();
    isConnected = false;
    studentId   = null;
    jwtToken    = null;
    updateStatusBar('disconnected');
    vscode.window.showWarningMessage('You have been disconnected by the teacher');
}

// ══════════════════════════════════════════════════════════════
// IDLE DETECTION — 60 seconds no typing = idle
// ══════════════════════════════════════════════════════════════
function resetIdleTimer() {
    if (idleTimer) clearTimeout(idleTimer);

    if (isIdle) {
        isIdle = false;
        updateStatusBar('connected');
    }

    idleTimer = setTimeout(() => {
        if (!isConnected) return;
        isIdle = true;
        sessionStats.totalIdleEvents++;
        updateStatusBar('idle');

        sendToServer('/student/event', {
            student_id: studentId,
            event_type: 'STUDENT_IDLE',
            severity:   'MEDIUM',
            details: {
                idle_seconds:     CONFIG.IDLE_TIMEOUT_MS / 1000,
                total_idle_count: sessionStats.totalIdleEvents,
                message:          `Student idle for ${CONFIG.IDLE_TIMEOUT_MS / 1000} seconds`,
            },
            timestamp: new Date().toISOString(),
        });
    }, CONFIG.IDLE_TIMEOUT_MS);
}

// ══════════════════════════════════════════════════════════════
// SUSPICIOUS SCORE — 0 to 100
// ══════════════════════════════════════════════════════════════
function addSuspiciousScore(points, reason) {
    suspiciousScore = Math.min(100, suspiciousScore + points);

    sessionStats.suspiciousEvents.push({
        reason:    reason,
        points:    points,
        new_score: suspiciousScore,
        timestamp: new Date().toISOString(),
    });

    updateStatusBar('suspicious');

    if (suspiciousScore >= 80) {
        vscode.window.showErrorMessage(
            `🚨 Suspicious score: ${suspiciousScore}/100 — Admin alerted!`
        );
    } else if (suspiciousScore >= 50) {
        vscode.window.showWarningMessage(
            `⚠️ Your activity score is ${suspiciousScore}/100`
        );
    }
}

// ══════════════════════════════════════════════════════════════
// STATUS BAR — live color-coded indicator
// ══════════════════════════════════════════════════════════════
function updateStatusBar(status) {
    if (!statusBarItem) return;
    const score = isConnected ? ` | ⚠️${suspiciousScore}/100` : '';
    const exam  = (isConnected && examMode) ? ' 📝 EXAM' : '';

    switch (status) {
        case 'connected':
            statusBarItem.text    = `$(check) Monitor: ${studentId}${score}${exam}`;
            statusBarItem.color   = '#4CAF50';
            statusBarItem.tooltip = 'Connected — Click for status';
            statusBarItem.command = 'code-monitor.status';
            break;
        case 'disconnected':
            statusBarItem.text    = '$(circle-slash) Monitor: Not Connected';
            statusBarItem.color   = '#9E9E9E';
            statusBarItem.tooltip = 'Click to Login';
            statusBarItem.command = 'code-monitor.login';
            break;
        case 'loading':
            statusBarItem.text    = '$(loading~spin) Monitor: Verifying...';
            statusBarItem.color   = '#2196F3';
            statusBarItem.tooltip = 'Checking credentials...';
            break;
        case 'suspicious':
            statusBarItem.text    = `$(warning) Monitor: ${studentId}${score}${exam}`;
            statusBarItem.color   = '#FF9800';
            statusBarItem.tooltip = 'Suspicious activity detected!';
            statusBarItem.command = 'code-monitor.status';
            break;
        case 'idle':
            statusBarItem.text    = `$(clock) Monitor: ${studentId} 💤 Idle${exam}`;
            statusBarItem.color   = '#9E9E9E';
            statusBarItem.tooltip = 'You appear idle!';
            statusBarItem.command = 'code-monitor.status';
            break;
    }
}

// ══════════════════════════════════════════════════════════════
// BATCHED BEHAVIORAL EVENTS — low-priority signals (file switches,
// cursor jumps, rename detection, brief window blur/refocus) don't
// need to reach the teacher's live dashboard instantly, so they're
// queued and flushed together every 2 minutes instead of one POST
// per event. HIGH/MEDIUM-severity proctoring alerts (paste, rapid
// tab switching, skill jumps, screen freeze, long absence, idle,
// exam mode changes, AI detection) still send immediately via
// sendToServer('/student/event', ...) so live monitoring during an
// exam isn't delayed.
// ══════════════════════════════════════════════════════════════
function queueBehavioralEvent(eventType, severity, details) {
    eventQueue.push({
        event_type: eventType,
        severity:   severity,
        details:    details,
        timestamp:  new Date().toISOString(),
    });
}

function flushEventQueue() {
    if (!isConnected || eventQueue.length === 0) return;
    const events = eventQueue;
    eventQueue = [];
    sendToServer('/student/batch-events', { events });
}

// ══════════════════════════════════════════════════════════════
// SEND TO SERVER — with JWT token
// ══════════════════════════════════════════════════════════════
function sendToServer(endpoint, data, onResponse) {
    if (!data) return;

    const jsonData = JSON.stringify(data);
    const headers  = {
        'Content-Type':   'application/json',
        'Content-Length': Buffer.byteLength(jsonData),
    };

    // Attach JWT token
    if (jwtToken) {
        headers['Authorization'] = `Bearer ${jwtToken}`;
    }

    const options = {
        hostname: CONFIG.SERVER_HOST,
        port:     CONFIG.SERVER_PORT,
        path:     endpoint,
        method:   'POST',
        headers:  headers,
    };

    const req = https.request(options, (res) => {
        if (!serverReachable) {
            serverReachable = true;
            updateStatusBar('connected');
            vscode.window.showInformationMessage(
                '✅ Reconnected to exam server!'
            );
        }
        // Consume response (optionally hand the parsed JSON to the caller)
        let body = '';
        res.on('data', (chunk) => { if (onResponse) body += chunk; });
        res.on('end', () => {
            if (!onResponse) return;
            try { onResponse(JSON.parse(body)); } catch (e) { /* non-JSON response */ }
        });
    });

    req.on('error', (err) => {
        if (err.code === 'ECONNREFUSED' && serverReachable) {
            serverReachable = false;
            if (isConnected) {
                vscode.window.showWarningMessage(
                    '⚠️ Lost connection to server. Retrying...'
                );
            }
        }
    });

    req.setTimeout(5000, () => req.destroy());
    req.write(jsonData);
    req.end();
}

// ══════════════════════════════════════════════════════════════
// HELPERS
// ══════════════════════════════════════════════════════════════
function getSessionDuration() {
    if (!sessionStats.sessionStart) return '0m 0s';
    const ms      = Date.now() - new Date(sessionStats.sessionStart).getTime();
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    return `${minutes}m ${seconds}s`;
}

// POST JSON and resolve when the request finishes, fails or times out.
// Unlike sendToServer (fire-and-forget), this lets deactivate() keep the
// extension host alive until the request has actually gone out.
function postAndWait(endpoint, data, timeoutMs) {
    return new Promise((resolve) => {
        const jsonData = JSON.stringify(data);
        const headers  = {
            'Content-Type':   'application/json',
            'Content-Length': Buffer.byteLength(jsonData),
        };
        if (jwtToken) headers['Authorization'] = `Bearer ${jwtToken}`;

        let settled = false;
        const done = () => { if (!settled) { settled = true; resolve(); } };

        const req = https.request({
            hostname: CONFIG.SERVER_HOST,
            port:     CONFIG.SERVER_PORT,
            path:     endpoint,
            method:   'POST',
            headers:  headers,
        }, (res) => {
            res.on('data', () => {});
            res.on('end', done);
            res.on('error', done);
        });
        req.on('error', done);
        req.setTimeout(timeoutMs, () => { req.destroy(); done(); });
        req.write(jsonData);
        req.end();
    });
}

// Called when VS Code closes or the extension is disabled/reloaded. Returning
// the promise makes VS Code wait (up to its shutdown limit) for the
// disconnect so the server records the session end even if the student
// never pressed Logout.
function deactivate() {
    const wasConnected = isConnected && studentId && jwtToken;
    const payload = wasConnected
        ? { student_id: studentId, timestamp: new Date().toISOString() }
        : null;

    const pendingEvents = (wasConnected && eventQueue.length > 0)
        ? postAndWait('/student/batch-events', { events: eventQueue }, 3000)
        : null;
    eventQueue = [];

    // Stop heartbeats first: a late heartbeat would mark the student connected again.
    stopMonitoring();
    if (statusBarItem) statusBarItem.dispose();

    if (!payload) return pendingEvents || undefined;

    const pendingDisconnect = postAndWait('/student/disconnect', payload, 3000);
    isConnected = false;
    return pendingEvents ? Promise.all([pendingEvents, pendingDisconnect]) : pendingDisconnect;
}

module.exports = { activate, deactivate };
