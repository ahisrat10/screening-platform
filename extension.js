const vscode = require('vscode');
const http   = require('http');

// ══════════════════════════════════════════════════════════════
// CODE MONITOR v3.0 — AUTOMATED TECHNICAL SCREENING PLATFORM
// ══════════════════════════════════════════════════════════════

const CONFIG = {
    SERVER_HOST:               'localhost',
    SERVER_PORT:               8000,
    SNAPSHOT_INTERVAL_MS:      30000,   // Send code every 30 seconds
    HEARTBEAT_INTERVAL_MS:     10000,   // Heartbeat every 10 seconds
    IDLE_TIMEOUT_MS:           60000,   // 60 seconds no typing = idle
    PASTE_CHAR_THRESHOLD:      10,      // 10+ chars at once = paste
    RAPID_TAB_COUNT:           3,       // 3 switches = suspicious
    RAPID_TAB_WINDOW_MS:       30000,   // within 30 seconds
    AI_WATCHING_SPEED:         8,       // chars/sec = fast typing
    BURST_WINDOW_MS:           3000,    // 3 second window
    LONG_ABSENCE_THRESHOLD_MS: 30000,   // 30 seconds away = suspicious
};

// ── Global State ───────────────────────────────────────────────
let studentId           = null;
let jwtToken            = null;
let isConnected         = false;
let statusBarItem       = null;
let snapshotTimer       = null;
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
// ACTIVATE
// ══════════════════════════════════════════════════════════════
function activate(context) {
    console.log('Code Monitor v3.0 Active!');

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

            // Ask Password
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

            // Show loading
            updateStatusBar('loading');
            vscode.window.showInformationMessage('🔄 Verifying credentials...');

            // Call backend to verify
            const loginResult = await callLoginAPI(id.trim(), password);

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
    // LISTENER: Text Changes
    // Detects: paste, AI-watching (fast typing)
    // ══════════════════════════════════════════════════════════
    let textChangeListener = vscode.workspace.onDidChangeTextDocument(
        (event) => {
            if (!isConnected) return;

            // Allow both saved files AND untitled documents
            const scheme = event.document.uri.scheme;
            if (scheme !== 'file' && scheme !== 'untitled') return;

            const now = Date.now();

            // ── Aggregate ALL changes in this event ────────────
            // VS Code sometimes splits a paste into multiple small
            // contentChanges so we sum them ALL up
            let totalCharsInEvent = 0;
            let pastePreview      = '';

            for (const change of event.contentChanges) {
                const len = change.text.length;
                if (len === 0) continue;

                totalCharsInEvent += len;
                sessionStats.totalKeystrokes += len;
                keystrokeTimestamps.push({ time: now, length: len });

                if (pastePreview.length < 120) {
                    pastePreview += change.text;
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

                addSuspiciousScore(15, 'Paste detected');

                sendToServer('/student/event', {
                    student_id: studentId,
                    event_type: 'PASTE_DETECTED',
                    severity:   'HIGH',
                    details: {
                        chars_pasted:     totalCharsInEvent,
                        paste_preview:    preview,
                        total_pastes:     sessionStats.totalPastes,
                        suspicious_score: suspiciousScore,
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
                    sendToServer('/student/event', {
                        student_id: studentId,
                        event_type: 'WINDOW_UNFOCUSED',
                        severity:   'LOW',
                        details: {
                            total_switches: sessionStats.totalTabSwitches,
                            message:        'Student left VS Code window',
                        },
                        timestamp: new Date().toISOString(),
                    });
                }

                updateStatusBar('suspicious');

            } else {
                // Student CAME BACK to VS Code
                isWindowFocused   = true;
                const awayMs      = windowBlurTime ? now - windowBlurTime : 0;
                const awaySecs    = (awayMs / 1000).toFixed(1);

                sendToServer('/student/event', {
                    student_id: studentId,
                    event_type: 'WINDOW_REFOCUSED',
                    severity:   'INFO',
                    details: {
                        away_duration_seconds: awaySecs,
                        message:               `Student returned after ${awaySecs}s`,
                    },
                    timestamp: new Date().toISOString(),
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

    // Register all commands and listeners
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
// START / STOP MONITORING
// ══════════════════════════════════════════════════════════════
function startMonitoring() {
    snapshotTimer  = setInterval(sendCodeSnapshot, CONFIG.SNAPSHOT_INTERVAL_MS);
    heartbeatTimer = setInterval(sendHeartbeat, CONFIG.HEARTBEAT_INTERVAL_MS);
    resetIdleTimer();
}

function stopMonitoring() {
    clearInterval(snapshotTimer);
    clearInterval(heartbeatTimer);
    clearTimeout(idleTimer);
    snapshotTimer = heartbeatTimer = idleTimer = null;
}

// ══════════════════════════════════════════════════════════════
// CALL BACKEND LOGIN API
// ══════════════════════════════════════════════════════════════
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

        const req = http.request(options, (res) => {
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
// SEND CODE SNAPSHOT — every 30 seconds
// ══════════════════════════════════════════════════════════════
function sendCodeSnapshot() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;

    const code     = editor.document.getText();
    if (!code || code.trim().length === 0) return;

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
// AI CODE PATTERN ANALYSIS
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
// HEARTBEAT — every 10 seconds
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

    switch (status) {
        case 'connected':
            statusBarItem.text    = `$(check) Monitor: ${studentId}${score}`;
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
            statusBarItem.text    = `$(warning) Monitor: ${studentId}${score}`;
            statusBarItem.color   = '#FF9800';
            statusBarItem.tooltip = 'Suspicious activity detected!';
            statusBarItem.command = 'code-monitor.status';
            break;
        case 'idle':
            statusBarItem.text    = `$(clock) Monitor: ${studentId} 💤 Idle`;
            statusBarItem.color   = '#9E9E9E';
            statusBarItem.tooltip = 'You appear idle!';
            statusBarItem.command = 'code-monitor.status';
            break;
    }
}

// ══════════════════════════════════════════════════════════════
// SEND TO SERVER — with JWT token
// ══════════════════════════════════════════════════════════════
function sendToServer(endpoint, data) {
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

    const req = http.request(options, (res) => {
        if (!serverReachable) {
            serverReachable = true;
            updateStatusBar('connected');
            vscode.window.showInformationMessage(
                '✅ Reconnected to exam server!'
            );
        }
        // Consume response
        res.on('data', () => {});
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

function deactivate() {
    stopMonitoring();
    if (statusBarItem) statusBarItem.dispose();
}

module.exports = { activate, deactivate };