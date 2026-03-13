"""
PersonaMeet Bot — Injected JavaScript Code
============================================

All JavaScript code that is injected into the Chrome browser page.
This file is imported by persona_meet_bot.py.

Contains:
  - STEALTH_SCRIPT    : Anti-bot-detection patches (hides Playwright markers)
  - INIT_SCRIPT       : Virtual audio system + recording system + WebRTC interception
  - JS_FIND_TOGGLE    : Finds mic/camera toggle buttons and returns their state & position
  - JS_DISMISS_POPUPS : Dismisses popups / dialogs that block the join flow
  - JS_FIND_JOIN      : Finds the "Join Now" / "Ask to Join" button
  - JS_PREJOIN_DETECTED : Detects whether the pre-join UI is visible
  - JS_IS_MEETING_OVER  : Detects meeting-end text on the page
"""


# ═══════════════════════════════════════════════════════════════════
# Stealth script injected BEFORE the page loads to prevent bot detection.
# Hides navigator.webdriver, fakes navigator.plugins, patches
# permissions query, and removes Playwright/automation markers.
# ═══════════════════════════════════════════════════════════════════

STEALTH_SCRIPT = r"""
(() => {
    // Hide navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true,
    });

    // Spoof navigator.plugins (empty array is a bot giveaway)
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [{
                name: 'Chrome PDF Plugin',
                description: 'Portable Document Format',
                filename: 'internal-pdf-viewer',
                length: 1,
                0: { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' },
            }, {
                name: 'Chrome PDF Viewer',
                description: '',
                filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                length: 1,
                0: { type: 'application/pdf', suffixes: 'pdf', description: '' },
            }, {
                name: 'Native Client',
                description: '',
                filename: 'internal-nacl-plugin',
                length: 2,
                0: { type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' },
                1: { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' },
            }];
            arr.refresh = () => {};
            return arr;
        },
        configurable: true,
    });

    // Spoof navigator.languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
        configurable: true,
    });

    // Fix Permissions API (Playwright gives inconsistent results)
    const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (parameters) => {
        if (parameters.name === 'notifications') {
            return Promise.resolve({ state: Notification.permission });
        }
        return originalQuery(parameters);
    };

    // Ensure window.chrome exists with expected properties
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};

    // Add chrome.csi and chrome.loadTimes (Google checks these)
    if (!window.chrome.csi) {
        window.chrome.csi = function() {
            return {
                startE: Date.now(),
                onloadT: Date.now(),
                pageT: performance.now(),
                tran: 15,
            };
        };
    }
    if (!window.chrome.loadTimes) {
        window.chrome.loadTimes = function() {
            return {
                commitLoadTime: Date.now() / 1000,
                connectionInfo: 'h2',
                finishDocumentLoadTime: Date.now() / 1000,
                finishLoadTime: Date.now() / 1000,
                firstPaintAfterLoadTime: 0,
                firstPaintTime: Date.now() / 1000,
                navigationType: 'Other',
                npnNegotiatedProtocol: 'h2',
                requestTime: Date.now() / 1000 - 0.16,
                startLoadTime: Date.now() / 1000,
                wasAlternateProtocolAvailable: false,
                wasFetchedViaSpdy: true,
                wasNpnNegotiated: true,
            };
        };
    }

    // Fix window.outerWidth/outerHeight (0 in headless)
    if (window.outerWidth === 0) {
        Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
    }
    if (window.outerHeight === 0) {
        Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight });
    }

    // Hide automation-related properties from iframe contentWindow
    const originalAttachShadow = Element.prototype.attachShadow;
    Element.prototype.attachShadow = function() {
        return originalAttachShadow.apply(this, arguments);
    };

    // Patch toString to hide overrides (anti-fingerprint evasion)
    const nativeToString = Function.prototype.toString;
    const overrides = new Map();
    const proxy = new Proxy(nativeToString, {
        apply: function(target, thisArg, args) {
            if (overrides.has(thisArg)) return overrides.get(thisArg);
            return nativeToString.call(thisArg);
        }
    });
    Function.prototype.toString = proxy;
    overrides.set(Function.prototype.toString, 'function toString() { [native code] }');
    overrides.set(navigator.permissions.query, 'function query() { [native code] }');
})();
"""


# ═══════════════════════════════════════════════════════════════════
# JavaScript injected into the page BEFORE it loads (via add_init_script).
#
# This mirrors inject.js from the extension:
#   - Overrides navigator.mediaDevices.getUserMedia → returns virtual audio stream
#   - Overrides navigator.mediaDevices.enumerateDevices → injects virtual mic device
#   - Sets up AudioContext + GainNode for playing audio through virtual mic
#
# PLUS recording support (replaces the extension's tabCapture + offscreen.js):
#   - Intercepts RTCPeerConnection to capture remote audio tracks
#   - Mixes all remote audio via AudioContext into a single stream
#   - Records the mixed stream with MediaRecorder
#
# Exposes window.__personaMeetBot for Playwright to call from Python.
# ═══════════════════════════════════════════════════════════════════

INIT_SCRIPT = r"""
(() => {
    'use strict';
    const LOG = '[PersonaMeet Bot]';

    // ═══════════════════════════════════════
    // SECTION 1: Virtual Audio System
    // ═══════════════════════════════════════
    let audioContext = null;
    let audioDestination = null;
    let virtualStream = null;
    let gainNode = null;
    let silentOscillator = null;
    let songSource = null;
    let songBuffer = null;
    let isSpeaking = false;

    // Save originals before overriding
    const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    const originalEnumerateDevices = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);

    // Override enumerateDevices — inject virtual mic if no physical mic exists
    // (mirrors inject.js enumerateDevices override)
    navigator.mediaDevices.enumerateDevices = async function () {
        let devices = [];
        try {
            devices = await originalEnumerateDevices();
        } catch (err) {
            console.log(LOG, 'Original enumerateDevices failed:', err.message);
        }
        const hasAudioInput = devices.some(d => d.kind === 'audioinput');
        if (!hasAudioInput) {
            console.log(LOG, 'No physical mic — injecting virtual microphone device');
            devices.push({
                deviceId: 'virtual-persona-mic',
                kind: 'audioinput',
                label: 'PersonaMeet Virtual Microphone',
                groupId: 'virtual-persona-group',
                toJSON() {
                    return {
                        deviceId: this.deviceId, kind: this.kind,
                        label: this.label, groupId: this.groupId
                    };
                }
            });
        }
        return devices;
    };

    // Create / reuse the virtual audio stream
    async function getVirtualAudioStream() {
        if (virtualStream && virtualStream.active) return virtualStream;

        if (!audioContext) {
            audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
            console.log(LOG, 'AudioContext created, state:', audioContext.state);

            // Resume AudioContext on any user gesture (click/keydown/mousedown)
            const resumeOnGesture = async () => {
                if (audioContext && audioContext.state === 'suspended') {
                    try {
                        await audioContext.resume();
                        console.log(LOG, 'AudioContext RESUMED via gesture, state:', audioContext.state);
                    } catch (_) {}
                }
                startSilentOscillator();
            };
            document.addEventListener('click', resumeOnGesture);
            document.addEventListener('keydown', resumeOnGesture);
            document.addEventListener('mousedown', resumeOnGesture);
        }

        if (!audioDestination) {
            audioDestination = audioContext.createMediaStreamDestination();
        }
        if (!gainNode) {
            gainNode = audioContext.createGain();
            gainNode.gain.value = 10.0;  // Volume boost (matches inject.js)
            gainNode.connect(audioDestination);
        }
        if (audioContext.state === 'running') startSilentOscillator();

        virtualStream = audioDestination.stream;
        console.log(LOG, 'Virtual audio stream ready, tracks:', virtualStream.getAudioTracks().length);
        return virtualStream;
    }

    // Near-silent oscillator keeps the virtual mic stream producing frames
    function startSilentOscillator() {
        if (silentOscillator) return;
        if (!audioContext || !audioDestination || audioContext.state !== 'running') return;
        silentOscillator = audioContext.createOscillator();
        silentOscillator.frequency.value = 440;
        const g = audioContext.createGain();
        g.gain.value = 0.001;
        silentOscillator.connect(g);
        g.connect(audioDestination);
        silentOscillator.start();
        console.log(LOG, 'Silent oscillator started');
    }

    // Override getUserMedia — return virtual audio for any audio request
    // (mirrors inject.js getUserMedia override)
    navigator.mediaDevices.getUserMedia = async function (constraints) {
        console.log(LOG, 'getUserMedia intercepted:', JSON.stringify(constraints));
        if (constraints && constraints.audio) {
            const vStream = await getVirtualAudioStream();
            if (constraints.video) {
                try {
                    const vidStream = await originalGetUserMedia({ video: constraints.video });
                    const combined = new MediaStream();
                    vStream.getAudioTracks().forEach(t => combined.addTrack(t));
                    vidStream.getVideoTracks().forEach(t => combined.addTrack(t));
                    return combined;
                } catch (_) {
                    return vStream;
                }
            }
            return vStream;
        }
        return originalGetUserMedia(constraints);
    };
    console.log(LOG, 'getUserMedia + enumerateDevices overrides installed');

    // ═══════════════════════════════════════
    // SECTION 2: Recording System (replaces tabCapture + offscreen.js)
    // Uses AudioContext as a mixer: remote WebRTC audio tracks are
    // connected to a single MediaStreamDestination, which feeds a
    // MediaRecorder.  New tracks arriving mid-recording are
    // automatically mixed in.
    // ═══════════════════════════════════════
    let recCtx = null;        // Recording AudioContext
    let recDest = null;       // MediaStreamDestination for mixed audio
    let mediaRecorder = null;
    let recordedChunks = [];
    let totalRecBytes = 0;
    let isRecording = false;
    let connectedTrackIds = new Set();

    function ensureRecordingContext() {
        if (!recCtx) {
            recCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
            recDest = recCtx.createMediaStreamDestination();
            console.log(LOG, 'Recording AudioContext created');
        }
        if (recCtx.state === 'suspended') recCtx.resume().catch(() => {});
        return { ctx: recCtx, dest: recDest };
    }

    // Connect a remote audio track into the recording mixer
    function connectTrackToRecorder(track) {
        if (connectedTrackIds.has(track.id)) return;
        try {
            const { ctx, dest } = ensureRecordingContext();
            if (ctx.state === 'suspended') ctx.resume().catch(() => {});
            const src = ctx.createMediaStreamSource(new MediaStream([track]));
            src.connect(dest);
            connectedTrackIds.add(track.id);
            console.log(LOG, 'Remote track connected to recorder:', track.id.substring(0, 20));
        } catch (err) {
            console.error(LOG, 'Error connecting track:', err);
        }
    }

    // ═══════════════════════════════════════
    // SECTION 3: WebRTC Interception
    // Wraps RTCPeerConnection so we capture every remote audio track
    // that Google Meet delivers.
    // ═══════════════════════════════════════
    const OrigRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (OrigRTC) {
        // Wrapper constructor — returns a real RTCPeerConnection instance
        // with an extra 'track' listener for recording
        function PersonaRTCPeerConnection(...args) {
            const pc = new OrigRTC(...args);
            pc.addEventListener('track', (event) => {
                if (event.track.kind === 'audio') {
                    console.log(LOG, 'Remote audio track received via WebRTC');
                    connectTrackToRecorder(event.track);
                    event.track.addEventListener('ended', () => {
                        connectedTrackIds.delete(event.track.id);
                        console.log(LOG, 'Remote audio track ended');
                    });
                }
            });
            return pc;  // 'new' returns this object (not 'this')
        }

        // Preserve the prototype chain so instanceof checks work
        PersonaRTCPeerConnection.prototype = OrigRTC.prototype;

        // Copy static methods (e.g. generateCertificate)
        for (const key of Object.getOwnPropertyNames(OrigRTC)) {
            if (key === 'prototype' || key === 'length' || key === 'name') continue;
            try {
                Object.defineProperty(PersonaRTCPeerConnection, key,
                    Object.getOwnPropertyDescriptor(OrigRTC, key));
            } catch (_) {}
        }

        window.RTCPeerConnection = PersonaRTCPeerConnection;
        if (window.webkitRTCPeerConnection) {
            window.webkitRTCPeerConnection = PersonaRTCPeerConnection;
        }
        console.log(LOG, 'RTCPeerConnection interceptor installed');
    }

    // ═══════════════════════════════════════
    // SECTION 4: API exposed to Playwright
    // (called via page.evaluate)
    // ═══════════════════════════════════════
    window.__personaMeetBot = {
        getVirtualAudioStream,

        // ── Play song through virtual mic ──────────────
        playSong: async function (songUrl) {
            if (isSpeaking) { console.log(LOG, 'Already speaking'); return false; }
            try {
                if (audioContext && audioContext.state === 'suspended') await audioContext.resume();
                if (!songBuffer) {
                    console.log(LOG, 'Loading song from', songUrl);
                    const resp = await fetch(songUrl);
                    if (!resp.ok) throw new Error('Fetch failed: ' + resp.status);
                    const ab = await resp.arrayBuffer();
                    songBuffer = await audioContext.decodeAudioData(ab);
                    console.log(LOG, 'Song loaded:', songBuffer.duration.toFixed(2) + 's',
                                songBuffer.numberOfChannels + 'ch', songBuffer.sampleRate + 'Hz');
                }
                songSource = audioContext.createBufferSource();
                songSource.buffer = songBuffer;
                songSource.connect(gainNode);
                isSpeaking = true;
                return new Promise(resolve => {
                    songSource.onended = () => {
                        console.log(LOG, 'Song finished');
                        isSpeaking = false;
                        songSource = null;
                        resolve(true);
                    };
                    songSource.start(0);
                    console.log(LOG, 'Song playing through virtual mic (' +
                                songBuffer.duration.toFixed(2) + 's, ' +
                                gainNode.gain.value + 'x volume)');
                });
            } catch (err) {
                console.error(LOG, 'Play error:', err);
                isSpeaking = false;
                return false;
            }
        },

        isSpeaking: () => isSpeaking,

        // ── Start recording ────────────────────────────
        startRecording: function () {
            if (isRecording) return true;
            const { ctx, dest } = ensureRecordingContext();
            try {
                if (ctx.state === 'suspended') ctx.resume().catch(() => {});
                const stream = dest.stream;
                if (stream.getAudioTracks().length === 0) {
                    console.log(LOG, 'No audio tracks in recording stream');
                    return false;
                }
                const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                    ? 'audio/webm;codecs=opus' : 'audio/webm';
                mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
                recordedChunks = [];
                totalRecBytes = 0;

                mediaRecorder.ondataavailable = (e) => {
                    if (e.data && e.data.size > 0) {
                        recordedChunks.push(e.data);
                        totalRecBytes += e.data.size;
                    }
                };
                mediaRecorder.onerror = (e) => console.error(LOG, 'Recorder error:', e.error || e);
                mediaRecorder.start(3000);  // chunk every 3 s (same as offscreen.js)
                isRecording = true;
                console.log(LOG, 'Recording started (' + mime + '), connected tracks:', connectedTrackIds.size);
                return true;
            } catch (err) {
                console.error(LOG, 'Failed to start recording:', err);
                return false;
            }
        },

        // ── Stop recording and return data URL ─────────
        stopRecording: function () {
            if (!mediaRecorder || !isRecording) return Promise.resolve(null);
            return new Promise(resolve => {
                mediaRecorder.onstop = () => {
                    isRecording = false;
                    console.log(LOG, 'Recorder stopped, chunks:', recordedChunks.length,
                                'total:', totalRecBytes, 'bytes');
                    if (recordedChunks.length === 0 || totalRecBytes === 0) {
                        resolve(null); return;
                    }
                    try {
                        const blob = new Blob(recordedChunks, { type: 'audio/webm' });
                        if (blob.size === 0) { resolve(null); return; }
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.onerror = () => resolve(null);
                        reader.readAsDataURL(blob);
                    } catch (err) {
                        console.error(LOG, 'Blob error:', err);
                        resolve(null);
                    }
                };
                try {
                    if (mediaRecorder.state !== 'inactive') mediaRecorder.stop();
                    else resolve(null);
                } catch (_) { resolve(null); }
            });
        },

        // ── Status for monitoring ──────────────────────
        getStatus: function () {
            return {
                isRecording,
                chunks: recordedChunks.length,
                totalBytes: totalRecBytes,
                connectedTracks: connectedTrackIds.size,
                isSpeaking
            };
        }
    };

    console.log(LOG, 'In-page system fully initialized');
})();
"""


# ═══════════════════════════════════════════════════════════════════
# JavaScript snippets used by the bot (mirror inject.js logic)
# ═══════════════════════════════════════════════════════════════════

# Returns { state: 'on'|'off'|'unknown'|null, x, y } for a mic/camera toggle
JS_FIND_TOGGLE = r"""
(type) => {
    const els = document.querySelectorAll('button, [role="button"], [data-is-muted]');
    for (const el of els) {
        const label = [
            el.getAttribute('aria-label') || '',
            el.getAttribute('data-tooltip') || '',
            el.getAttribute('title') || '',
        ].join(' ').toLowerCase();

        if (label.includes('settings') || label.includes('option') ||
            label.includes('effect') || label.includes('layout') || label.includes('tile'))
            continue;

        let isMatch = false;
        if (type === 'microphone') {
            if (label.includes('microphone') || label.includes(' mic ') || /\bmic\b/.test(label))
                isMatch = true;
        }
        if (type === 'camera') {
            if (label.includes('camera')) isMatch = true;
            if (label.includes('video') && (label.includes('turn off') || label.includes('turn on')))
                isMatch = true;
        }

        if (isMatch) {
            const rect = el.getBoundingClientRect();
            let state = 'unknown';
            if (label.includes('turn off')) state = 'on';
            else if (label.includes('turn on') || label.includes('is off')) state = 'off';
            return { state, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
        }
    }
    return null;
}
"""

# Dismiss popups / dialogs (mirrors inject.js dismissPopups)
JS_DISMISS_POPUPS = r"""
() => {
    const dismiss = ['got it', 'dismiss', 'close', 'ok', 'no thanks',
                     'continue without microphone', 'continue without mic'];
    for (const btn of document.querySelectorAll('button, [role="button"]')) {
        const text = (btn.innerText || '').trim().toLowerCase();
        if (dismiss.some(d => text.includes(d))) btn.click();
    }
    for (const el of document.querySelectorAll('[role="dialog"] button, [role="alertdialog"] button')) {
        const text = (el.innerText || '').trim().toLowerCase();
        if (text.includes('continue') || text.includes('got it') ||
            text.includes('use without') || text.includes('ok'))
            el.click();
    }
}
"""

# Find the Join / Ask-to-join button and return its center coords
JS_FIND_JOIN = r"""
() => {
    const targets = ['join now', 'ask to join'];
    for (const btn of document.querySelectorAll('button')) {
        const text = (btn.innerText || '').trim().toLowerCase();
        if (targets.some(t => text.includes(t))) {
            const r = btn.getBoundingClientRect();
            return { text: (btn.innerText || '').trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }
    }
    for (const span of document.querySelectorAll('button span')) {
        const text = (span.innerText || '').trim().toLowerCase();
        if (targets.some(t => text.includes(t))) {
            const btn = span.closest('button');
            if (btn) {
                const r = btn.getBoundingClientRect();
                return { text: (btn.innerText || '').trim(), x: r.left + r.width / 2, y: r.top + r.height / 2 };
            }
        }
    }
    return null;
}
"""

# Check whether the pre-join UI is visible (mic/camera toggles, join button, OR name input)
JS_PREJOIN_DETECTED = r"""
() => {
    // Check for mic/camera toggle buttons
    const buttons = document.querySelectorAll('button, [role="button"]');
    for (const btn of buttons) {
        const labels = [
            btn.getAttribute('aria-label') || '',
            btn.getAttribute('data-tooltip') || '',
            btn.getAttribute('title') || '',
        ].join(' ').toLowerCase();
        if (labels.includes('microphone') || labels.includes('camera')) return true;
    }
    // Check for Join / Ask to Join buttons
    for (const btn of document.querySelectorAll('button')) {
        const text = (btn.innerText || '').trim().toLowerCase();
        if (text.includes('join now') || text.includes('ask to join')) return true;
    }
    // Check for "Your name" input field (shown when not signed in)
    const nameInput = document.querySelector('input[placeholder="Your name"]');
    if (nameInput) return true;
    // Check for "What's your name?" text on page
    const bodyText = (document.body && document.body.innerText) || '';
    if (bodyText.includes("What's your name") || bodyText.includes("Your name")) return true;
    return false;
}
"""

# Meeting-end detection (mirrors inject.js isMeetingOver)
JS_IS_MEETING_OVER = r"""
() => {
    const text = (document.body && document.body.innerText) || '';
    return (
        text.includes('You left the meeting') ||
        text.includes('The meeting has ended') ||
        text.includes("You've been removed from the meeting") ||
        text.includes('You were removed from this meeting') ||
        text.includes('Return to home screen') ||
        text.includes("You can't join this video call") ||
        text.includes("can't join this video call")
    );
}
"""
