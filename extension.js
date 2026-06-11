const vscode = require('vscode');
const http = require('http');

// ╔══════════════════════════════════════════════════════════════╗
// ║        CODE MONITOR — ADVANCED EXAM MONITORING SYSTEM        ║
// ║         Automated Technical Screening Platform v2.0          ║
// ╚══════════════════════════════════════════════════════════════╝

// ── CONFIGURATION ──────────────────────────────────────────────
const CONFIG = {
    SERVER_HOST:               'localhost',
    SERVER_PORT:               8000,
    SNAPSHOT_INTERVAL_MS:      30000,   // Send code every 30s
    HEARTBEAT_INTERVAL_MS:     10000,   // Heartbeat every 10s
    IDLE_TIMEOUT_MS:           60000,   // 60s no typing = idle
    PASTE_CHAR_THRESHOLD:      30,      // 50+ chars at once = paste (Fix 2: raised from 30)
    RAPID_TAB_COUNT:           3,       // 3 switches in window = suspicious
    RAPID_TAB_WINDOW_MS:       30000,   // 30s window for tab tracking
    AI_WATCHING_SPEED:         8,       // chars/sec = suspiciously fast
    BURST_WINDOW_MS:           3000,    // 3s window for burst analysis
    LONG_ABSENCE_THRESHOLD_MS: 30000,   // 30s away = suspicious
    RECONNECT_INTERVAL_MS:     5000,    // (Fix 2) retry interval
};

// ── GLOBAL STATE ───────────────────────────────────────────────
let studentId          = null;
let isConnected        = false;
let statusBarItem      = null;

// Timers
let snapshotTimer      = null;
let heartbeatTimer     = null;
let idleTimer          = null;

// Typing analysis
let keystrokeTimestamps = [];
let lastTypingTime      = null;
let isIdle              = false;

// Tab/focus tracking
let tabSwitchTimestamps = [];
let isWindowFocused     = true;
let windowBlurTime      = null;

// Suspicious score (0–100)
let suspiciousScore = 0;

// Session statistics
let sessionStats = {
    totalKeystrokes:   0,
    totalPastes:       0,
    totalTabSwitches:  0,
    totalIdleEvents:   0,
    codeSnapshots:     0,
    suspiciousEvents:  [],
    sessionStart:      null,
};

// ══════════════════════════════════════════════════════════════
// ACTIVATE — runs when extension starts
// ══════════════════════════════════════════════════════════════
function activate(context) {
    console.log('🚀 Code Monitor Extension Activated!');

    // ── Create Status Bar ──────────────────────────────────────
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
                prompt: '🎓 Enter your Student ID',
                placeHolder: 'e.g. STU-2024-001',
                validateInput: (val) => {
                    if (!val || val.trim().length < 3)
                        return 'Student ID is too short!';
                    return null;
                }
            });
            if (!id) return;

            // Ask Password
            const password = await vscode.window.showInputBox({
                prompt: '🔒 Enter your Exam Password',
                placeHolder: 'Enter your password',
                password: true,
                validateInput: (val) => {
                    if (!val || val.trim().length < 3)
                        return 'Password is too short!';
                    return null;
                }
            });
            if (!password) return;

            // Set state
            studentId        = id.trim();
            isConnected      = true;
            suspiciousScore  = 0;
            serverReachable  = true;  // reset on fresh login
            sessionStats = {
                totalKeystrokes:  0,
                totalPastes:      0,
                totalTabSwitches: 0,
                totalIdleEvents:  0,
                codeSnapshots:    0,
                suspiciousEvents: [],
                sessionStart:     new Date().toISOString(),
            };

            updateStatusBar('connected');

            // Tell server student connected
            sendToServer('/student/connect', {
                student_id: studentId,
                timestamp:  new Date().toISOString(),
                device_info: {
                    platform:       process.platform,
                    vscode_version: vscode.version,
                }
            });

            vscode.window.showInformationMessage(
                `✅ Connected as: ${studentId} — Exam monitoring is now ACTIVE.`
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

            // Send final session report
            sendToServer('/student/disconnect', {
                student_id:       studentId,
                suspicious_score: suspiciousScore,
                session_summary: {
                    duration:           getSessionDuration(),
                    total_keystrokes:   sessionStats.totalKeystrokes,
                    total_pastes:       sessionStats.totalPastes,
                    total_tab_switches: sessionStats.totalTabSwitches,
                    suspicious_events:  sessionStats.suspiciousEvents.length,
                },
                timestamp: new Date().toISOString(),
            });

            stopMonitoring();
            isConnected = false;
            studentId   = null;
            updateStatusBar('disconnected');

            vscode.window.showInformationMessage(
                '👋 Disconnected. Your session report has been submitted.'
            );
        }
    );

    // ══════════════════════════════════════════════════════════
    // COMMAND: VIEW STATUS
    // ══════════════════════════════════════════════════════════
    let statusCommand = vscode.commands.registerCommand(
        'code-monitor.status',
        () => {
            if (!isConnected) {
                vscode.window.showInformationMessage(
                    '❌ Not connected. Use: Code Monitor: Login'
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
    // LISTENER: Text Changes
    // Detects: copy-paste, typing speed, AI-watching pattern
    // ══════════════════════════════════════════════════════════
    let textChangeListener = vscode.workspace.onDidChangeTextDocument(
        (event) => {
            if (!isConnected) return;
            if (event.document.uri.scheme !== 'file') return;

            const now = Date.now();

            for (const change of event.contentChanges) {
                const textLength = change.text.length;
                if (textLength === 0) continue; // skip deletions

                // Track keystrokes
                sessionStats.totalKeystrokes += textLength;
                keystrokeTimestamps.push({ time: now, length: textLength });

                // Keep only last 30 seconds
                keystrokeTimestamps = keystrokeTimestamps.filter(
                    k => now - k.time < 30000
                );
                lastTypingTime = now;

                // ── DETECT: Copy-Paste ───────────────────────
                if (textLength >= CONFIG.PASTE_CHAR_THRESHOLD) {
                    sessionStats.totalPastes++;
                    const preview = change.text.substring(0, 120);

                    sessionStats.suspiciousEvents.push({
                        type:      'PASTE_DETECTED',
                        chars:     textLength,
                        preview:   preview,
                        timestamp: new Date().toISOString(),
                    });

                    addSuspiciousScore(15, 'Large paste detected');

                    sendToServer('/student/event', {
                        student_id: studentId,
                        event_type: 'PASTE_DETECTED',
                        severity:   'HIGH',
                        details: {
                            chars_pasted:     textLength,
                            paste_preview:    preview,
                            total_pastes:     sessionStats.totalPastes,
                            suspicious_score: suspiciousScore,
                        },
                        timestamp: new Date().toISOString(),
                    });

                    vscode.window.showWarningMessage(
                        `⚠️ Paste detected (${textLength} chars) — Reported to admin!`
                    );
                }

                // ── DETECT: AI-Watching (fast burst typing) ──
                // When student reads code from AI screen and types fast
                const recentKeys = keystrokeTimestamps.filter(
                    k => now - k.time < CONFIG.BURST_WINDOW_MS
                );
                const recentChars = recentKeys.reduce(
                    (sum, k) => sum + k.length, 0
                );
                const burstSpeed = recentChars / (CONFIG.BURST_WINDOW_MS / 1000);

                if (burstSpeed > CONFIG.AI_WATCHING_SPEED && recentChars > 30) {
                    sendToServer('/student/event', {
                        student_id: studentId,
                        event_type: 'FAST_BURST_TYPING',
                        severity:   'MEDIUM',
                        details: {
                            speed_chars_per_sec: burstSpeed.toFixed(2),
                            chars_in_burst:      recentChars,
                            message: 'Unusually fast typing — possible AI watching',
                        },
                        timestamp: new Date().toISOString(),
                    });
                }
            }

            // Reset idle timer every time student types
            resetIdleTimer();
        }
    );

    // ══════════════════════════════════════════════════════════
    // LISTENER: Window Focus
    // Detects: tab switch, rapid switching, long absence
    // ══════════════════════════════════════════════════════════
    let windowFocusListener = vscode.window.onDidChangeWindowState(
        (windowState) => {
            if (!isConnected) return;
            const now = Date.now();

            if (!windowState.focused) {
                // ── Student LEFT VS Code ─────────────────────
                isWindowFocused  = false;
                windowBlurTime   = now;
                tabSwitchTimestamps.push(now);
                sessionStats.totalTabSwitches++;

                // Remove old timestamps outside window
                tabSwitchTimestamps = tabSwitchTimestamps.filter(
                    t => now - t < CONFIG.RAPID_TAB_WINDOW_MS
                );

                // ── DETECT: Rapid Tab Switching ──────────────
                if (tabSwitchTimestamps.length >= CONFIG.RAPID_TAB_COUNT) {
                    addSuspiciousScore(20, 'Rapid tab switching');

                    sendToServer('/student/event', {
                        student_id: studentId,
                        event_type: 'RAPID_TAB_SWITCHING',
                        severity:   'HIGH',
                        details: {
                            switches_in_window: tabSwitchTimestamps.length,
                            window_seconds:     CONFIG.RAPID_TAB_WINDOW_MS / 1000,
                            total_switches:     sessionStats.totalTabSwitches,
                            message: 'Rapid tab switching — highly suspicious!',
                        },
                        timestamp: new Date().toISOString(),
                    });

                    vscode.window.showErrorMessage(
                        '🚨 Rapid tab switching detected! Admin has been notified.'
                    );

                } else {
                    // Single tab switch — log it
                    sendToServer('/student/event', {
                        student_id: studentId,
                        event_type: 'WINDOW_UNFOCUSED',
                        severity:   'LOW',
                        details: {
                            total_switches: sessionStats.totalTabSwitches,
                            message: 'Student left VS Code window',
                        },
                        timestamp: new Date().toISOString(),
                    });
                }

                updateStatusBar('suspicious');

            } else {
                // ── Student CAME BACK to VS Code ─────────────
                isWindowFocused      = true;
                const awayMs         = windowBlurTime ? now - windowBlurTime : 0;
                const awaySeconds    = (awayMs / 1000).toFixed(1);

                sendToServer('/student/event', {
                    student_id: studentId,
                    event_type: 'WINDOW_REFOCUSED',
                    severity:   'INFO',
                    details: {
                        away_duration_seconds: awaySeconds,
                        message: `Student returned after ${awaySeconds}s`,
                    },
                    timestamp: new Date().toISOString(),
                });

                // ── DETECT: Long Absence ─────────────────────
                if (awayMs > CONFIG.LONG_ABSENCE_THRESHOLD_MS) {
                    addSuspiciousScore(10, `Long absence: ${awaySeconds}s`);

                    sendToServer('/student/event', {
                        student_id: studentId,
                        event_type: 'LONG_ABSENCE',
                        severity:   'MEDIUM',
                        details: {
                            away_duration_seconds: awaySeconds,
                            message: `Student was away for ${awaySeconds} seconds!`,
                        },
                        timestamp: new Date().toISOString(),
                    });
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

            sendToServer('/student/event', {
                student_id: studentId,
                event_type: 'FILE_SWITCHED',
                severity:   'INFO',
                details: {
                    new_file: editor.document.fileName,
                    language: editor.document.languageId,
                },
                timestamp: new Date().toISOString(),
            });
        }
    );

    // Register all
    context.subscriptions.push(
        loginCommand,
        logoutCommand,
        statusCommand,
        textChangeListener,
        windowFocusListener,
        editorChangeListener,
    );
}

// ══════════════════════════════════════════════════════════════
// START MONITORING — runs after login
// ══════════════════════════════════════════════════════════════
function startMonitoring() {
    // 1. Code snapshot every 30s
    snapshotTimer = setInterval(() => {
        sendCodeSnapshot();
    }, CONFIG.SNAPSHOT_INTERVAL_MS);

    // 2. Heartbeat every 10s
    heartbeatTimer = setInterval(() => {
        sendHeartbeat();
    }, CONFIG.HEARTBEAT_INTERVAL_MS);

    // 3. Idle detection
    resetIdleTimer();
}

// ══════════════════════════════════════════════════════════════
// STOP MONITORING — runs on logout
// ══════════════════════════════════════════════════════════════
function stopMonitoring() {
    clearInterval(snapshotTimer);
    clearInterval(heartbeatTimer);
    clearTimeout(idleTimer);
    snapshotTimer  = null;
    heartbeatTimer = null;
    idleTimer      = null;
}

// ══════════════════════════════════════════════════════════════
// SEND CODE SNAPSHOT — every 30 seconds
// ══════════════════════════════════════════════════════════════
function sendCodeSnapshot() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;

    const code     = editor.document.getText();
    const analysis = analyzeCodeForAIPatterns(code);

    sessionStats.codeSnapshots++;

    sendToServer('/student/submit-code', {
        student_id:      studentId,
        filename:        editor.document.fileName,
        language:        editor.document.languageId,
        code:            code,
        line_count:      code.split('\n').length,
        analysis:        analysis,
        snapshot_number: sessionStats.codeSnapshots,
        timestamp:       new Date().toISOString(),
    });

    console.log(`📸 Snapshot #${sessionStats.codeSnapshots} sent`);
}

// ══════════════════════════════════════════════════════════════
// ANALYZE CODE FOR AI PATTERNS  (Fix 3 — stronger detection)
// ══════════════════════════════════════════════════════════════
function analyzeCodeForAIPatterns(code) {
    const lines = code.split('\n');

    // ── Check 1: Comment ratio ────────────────────────────────
    const commentLines = lines.filter(l =>
        l.trim().startsWith('//') ||
        l.trim().startsWith('#')  ||
        l.trim().startsWith('/*') ||
        l.trim().startsWith('*')
    ).length;
    const commentRatio = lines.length > 0
        ? commentLines / lines.length : 0;

    // ── Check 2: AI-style variable names ─────────────────────
    const aiVarPattern = /\b(result|output|data|value|item|element|node|temp|current|previous|index|count|total|sum|average|maximum|minimum|iterator|container|handler|callback|response|request|target|source|destination|payload|buffer|cache|queue|stack|heap|graph|matrix|vector|tensor)\b/g;
    const aiVarCount   = (code.match(aiVarPattern) || []).length;

    // ── Check 3: Perfect formatting (AI always formats well) ──
    const emptyLines     = lines.filter(l => l.trim() === '').length;
    const emptyLineRatio = lines.length > 0
        ? emptyLines / lines.length : 0;

    // ── Check 4: Docstring presence (AI loves docstrings) ─────
    const hasDocstrings = /"""[\s\S]*?"""|\/\*\*[\s\S]*?\*\//.test(code);

    // ── Check 5: Consistent indentation (AI is always consistent)
    const indentedLines = lines.filter(l => l.startsWith('    ') || l.startsWith('\t'));
    const indentRatio   = lines.length > 0
        ? indentedLines.length / lines.length : 0;

    // ── Check 6: Long descriptive function names (AI tendency) ─
    const longFuncNames = (code.match(/def [a-z][a-z_]{10,}|function [a-z][a-zA-Z]{10,}/g) || []).length;

    // ── Check 7: No typos/abbreviations (humans make typos) ───
    const typoPattern   = /\b(cnt|idx|tmp|val|arr|lst|dct|func|var|str|num|calc|res|ret)\b/g;
    const humanVarCount = (code.match(typoPattern) || []).length;

    // ── Build flags ───────────────────────────────────────────
    const flags = [];
    if (commentRatio > 0.3)                              flags.push('EXCESSIVE_COMMENTS');
    if (aiVarCount > 5)                                  flags.push('AI_VARIABLE_NAMES');
    if (emptyLineRatio > 0.25)                           flags.push('PERFECT_FORMATTING');
    if (hasDocstrings)                                   flags.push('HAS_DOCSTRINGS');
    if (longFuncNames > 2)                               flags.push('LONG_FUNCTION_NAMES');
    if (humanVarCount === 0 && lines.length > 10)        flags.push('NO_HUMAN_SHORTCUTS');

    // ── Calculate AI suspicion score (0–100) ──────────────────
    const aiScore = Math.min(100,
        (commentRatio > 0.3                       ? 20 : 0) +
        (aiVarCount > 5                           ? 20 : 0) +
        (hasDocstrings                            ? 20 : 0) +
        (emptyLineRatio > 0.25                    ? 15 : 0) +
        (longFuncNames > 2                        ? 15 : 0) +
        (humanVarCount === 0 && lines.length > 10 ? 10 : 0)
    );

    return {
        total_lines:       lines.length,
        comment_ratio:     commentRatio.toFixed(2),
        ai_variable_count: aiVarCount,
        human_var_count:   humanVarCount,
        has_docstrings:    hasDocstrings,
        long_func_names:   longFuncNames,
        indent_ratio:      indentRatio.toFixed(2),
        ai_flags:          flags,
        ai_score:          aiScore,
        likely_ai_written: aiScore >= 40,
    };
}

// ══════════════════════════════════════════════════════════════
// HEARTBEAT — proves student is alive every 10s
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
    });
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
                message: `Student idle for ${CONFIG.IDLE_TIMEOUT_MS / 1000} seconds`,
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

    // Warn student if score is very high
    if (suspiciousScore >= 80) {
        vscode.window.showErrorMessage(
            `🚨 Suspicious score: ${suspiciousScore}/100 — Admin has been alerted!`
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

    const score = isConnected ? ` | ⚠️ ${suspiciousScore}/100` : '';

    switch (status) {
        case 'connected':
            statusBarItem.text    = `$(check) Monitor: ${studentId}${score}`;
            statusBarItem.color   = '#4CAF50'; // green
            statusBarItem.tooltip = 'Connected — Click for session stats';
            statusBarItem.command = 'code-monitor.status';
            break;

        case 'disconnected':
            statusBarItem.text    = '$(circle-slash) Monitor: Not Connected';
            statusBarItem.color   = '#9E9E9E'; // gray
            statusBarItem.tooltip = 'Click to Login';
            statusBarItem.command = 'code-monitor.login';
            break;

        case 'suspicious':
            statusBarItem.text    = `$(warning) Monitor: ${studentId}${score}`;
            statusBarItem.color   = '#FF9800'; // orange
            statusBarItem.tooltip = 'Suspicious activity detected!';
            statusBarItem.command = 'code-monitor.status';
            break;

        case 'idle':
            statusBarItem.text    = `$(clock) Monitor: ${studentId} 💤`;
            statusBarItem.color   = '#9E9E9E'; // gray
            statusBarItem.tooltip = 'You appear idle — start coding!';
            statusBarItem.command = 'code-monitor.status';
            break;

        case 'server-down':
            statusBarItem.text    = `$(sync~spin) Monitor: ${studentId} — Reconnecting...`;
            statusBarItem.color   = '#F44336'; // red
            statusBarItem.tooltip = 'Lost connection to exam server — retrying...';
            statusBarItem.command = 'code-monitor.status';
            break;
    }
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

// ══════════════════════════════════════════════════════════════
// SEND TO SERVER — with auto-reconnection  (Fix 2)
// ══════════════════════════════════════════════════════════════
let serverReachable = true;
let reconnectTimer  = null;

function sendToServer(endpoint, data) {
    if (!data) return;

    const jsonData = JSON.stringify(data);
    const options  = {
        hostname: CONFIG.SERVER_HOST,
        port:     CONFIG.SERVER_PORT,
        path:     endpoint,
        method:   'POST',
        headers: {
            'Content-Type':   'application/json',
            'Content-Length': Buffer.byteLength(jsonData),
        },
    };

    const req = http.request(options, (res) => {
        let body = '';
        res.on('data', chunk => body += chunk);
        res.on('end', () => {
            // Server is back online — clear reconnect state
            if (!serverReachable) {
                serverReachable = true;
                if (reconnectTimer) {
                    clearInterval(reconnectTimer);
                    reconnectTimer = null;
                }
                updateStatusBar('connected');
                if (isConnected) {
                    vscode.window.showInformationMessage(
                        '✅ Reconnected to exam server!'
                    );
                }
            }

            if (res.statusCode !== 200) {
                console.log(`[${endpoint}] Server responded: ${res.statusCode}`);
            }
        });
    });

    req.on('error', (err) => {
        if (err.code === 'ECONNREFUSED') {
            if (serverReachable) {
                // First time noticing server is down
                serverReachable = false;
                console.log('⚠️ Server unreachable. Will retry automatically...');

                if (isConnected) {
                    updateStatusBar('server-down');
                    vscode.window.showWarningMessage(
                        '⚠️ Lost connection to exam server. Retrying every 5s...'
                    );
                }

                // Start a periodic reconnect probe (ping heartbeat endpoint)
                if (!reconnectTimer) {
                    reconnectTimer = setInterval(() => {
                        if (!isConnected) {
                            clearInterval(reconnectTimer);
                            reconnectTimer = null;
                            return;
                        }
                        // Probe server with a lightweight heartbeat
                        sendHeartbeat();
                    }, CONFIG.RECONNECT_INTERVAL_MS);
                }
            }
        } else {
            console.error(`[${endpoint}] Error: ${err.message}`);
        }
    });

    req.setTimeout(5000, () => req.destroy());
    req.write(jsonData);
    req.end();
}

// ══════════════════════════════════════════════════════════════
function deactivate() {
    stopMonitoring();
    if (reconnectTimer) clearInterval(reconnectTimer);
    if (statusBarItem) statusBarItem.dispose();
}

module.exports = { activate, deactivate };