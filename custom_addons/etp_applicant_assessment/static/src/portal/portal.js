// ?v= busts the 7-day browser cache on these module files; keep in sync
// with the ?v= on portal.js itself in portal_templates.xml.
import { WebcamDetector } from "/etp_applicant_assessment/static/src/portal/webcam-detector.js?v=20260721-perm-fix";
import { ClipRecorder } from "/etp_applicant_assessment/static/src/portal/clip-recorder.js?v=20260721-perm-fix";

const SNAPSHOT_PERIOD_MS = 25000;
const SNAPSHOT_ON_SIGNAL_MIN_GAP_MS = 12000;
const CLIP_SECONDS = 10;
const MEDIA_ERROR_DEDUPE_MS = 30000;
// While the candidate stays out of full screen a fresh warning fires on
// this cadence until they return (or the warning cap auto-submits).
const FS_WARN_REPEAT_MS = 20000;
// On submit, give the in-flight evidence clip a moment to keep recording,
// then flush it early and wait (bounded) for its upload before navigating.
const SUBMIT_CLIP_GRACE_MS = 6000;
const SUBMIT_EVIDENCE_DRAIN_MS = 8000;
const BLOCKED_KEYS = new Set(["c", "v", "x", "p", "s", "u"]);
const WARNING_TOAST_MS = 4500;
const WARNING_LABELS = {
    look_away: "Please look at the screen",
    no_face: "Face not visible — stay in the camera view",
    other_person: "Another person detected in the frame",
    mobile_phone: "Mobile phone detected — put it away",
    lip_movement: "Please stop speaking",
    tab_switch: "Do not switch tabs during the test",
    window_change: "Please stay on this window",
    fullscreen_exit: "Please stay in full-screen mode",
    copy_paste: "Copy / paste / right-click is disabled during the test",
};

function ensureToastHost() {
    let host = document.querySelector(".eaa-toast-host");
    if (host) return host;
    host = document.createElement("div");
    host.className = "eaa-toast-host";
    document.body.appendChild(host);
    return host;
}

const activeToasts = new Map();
function buildToastContent(node, kind, count, cap) {
    const label = WARNING_LABELS[kind] || `Warning: ${kind}`;
    node.textContent = "";
    const labelSpan = document.createElement("div");
    labelSpan.className = "eaa-toast__label";
    labelSpan.textContent = label;
    node.appendChild(labelSpan);
    if (count > 0 && cap > 0) {
        const meta = document.createElement("div");
        meta.className = "eaa-toast__meta";
        const remaining = Math.max(cap - count, 0);
        if (remaining <= 0) {
            meta.textContent = `Warning ${count} of ${cap} — auto-submitting your test.`;
        } else if (remaining === 1) {
            meta.textContent = `Warning ${count} of ${cap} — one more and your test auto-submits.`;
        } else {
            meta.textContent = `Warning ${count} of ${cap}`;
        }
        node.appendChild(meta);
    }
}

function applyToastSeverity(node, count, cap) {
    node.classList.remove("eaa-toast--critical", "eaa-toast--final");
    if (cap > 0 && count > 0) {
        const remaining = cap - count;
        if (remaining <= 0) node.classList.add("eaa-toast--final");
        else if (remaining <= 1) node.classList.add("eaa-toast--critical");
    }
}

function showWarningToast(kind, count, cap) {
    const host = ensureToastHost();
    const existing = activeToasts.get(kind);
    if (existing) {
        clearTimeout(existing.timer);
        buildToastContent(existing.node, kind, count, cap);
        applyToastSeverity(existing.node, count, cap);
        existing.node.classList.remove("eaa-toast--pulse");
        // Restart the shake only when the situation is severe — shaking the
        // toast on every repeated minor signal reads as screen flicker.
        const severe = existing.node.classList.contains("eaa-toast--critical")
            || existing.node.classList.contains("eaa-toast--final");
        if (severe) {
            void existing.node.offsetWidth;
            existing.node.classList.add("eaa-toast--pulse");
        }
        existing.timer = setTimeout(() => {
            existing.node.remove();
            activeToasts.delete(kind);
        }, WARNING_TOAST_MS);
        return;
    }
    const node = document.createElement("div");
    node.className = "eaa-toast eaa-toast--warning";
    buildToastContent(node, kind, count, cap);
    applyToastSeverity(node, count, cap);
    host.appendChild(node);
    const record = { node, timer: null };
    record.timer = setTimeout(() => {
        node.remove();
        activeToasts.delete(kind);
    }, WARNING_TOAST_MS);
    activeToasts.set(kind, record);
}

function warningMetaText(count, cap) {
    if (!(count > 0 && cap > 0)) return "";
    const remaining = Math.max(cap - count, 0);
    if (remaining <= 0) return `Warning ${count} of ${cap} — auto-submitting your test.`;
    if (remaining === 1) return `Warning ${count} of ${cap} — one more and your test auto-submits.`;
    return `Warning ${count} of ${cap}`;
}

// Blocking pop-up shown when a mobile phone is detected. Unlike the corner
// toast, it interrupts the candidate until acknowledged.
let phoneAlert = null;
function showPhoneAlert(count, cap) {
    if (phoneAlert) {
        phoneAlert.meta.textContent = warningMetaText(count, cap);
        return;
    }
    const overlay = document.createElement("div");
    overlay.className = "eaa-alert eaa-alert--phone";
    const card = document.createElement("div");
    card.className = "eaa-alert__card";
    const icon = document.createElement("div");
    icon.className = "eaa-alert__icon eaa-alert__icon--phone";
    const title = document.createElement("h2");
    title.className = "eaa-alert__title";
    title.textContent = "Mobile phone detected";
    const text = document.createElement("p");
    text.className = "eaa-alert__text";
    text.textContent = "Put your phone away immediately. This incident has been recorded and will be reported to the recruiter.";
    const meta = document.createElement("p");
    meta.className = "eaa-alert__meta";
    meta.textContent = warningMetaText(count, cap);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-primary";
    btn.textContent = "I understand";
    btn.addEventListener("click", () => {
        overlay.remove();
        phoneAlert = null;
    });
    card.append(icon, title, text, meta, btn);
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    phoneAlert = { overlay, meta };
}

document.addEventListener("DOMContentLoaded", async () => {
    const root = document.querySelector(".eaa-test");
    if (!root) return;

    const token = root.dataset.token;
    const warningCap = parseInt(root.dataset.warningCap || "0", 10);
    const requireWebcam = root.dataset.requireWebcam === "1";
    const requireMic = root.dataset.requireMic === "1";
    const requireFullscreen = root.dataset.requireFullscreen === "1";
    const blockCopyPaste = root.dataset.blockCopyPaste === "1";
    const blockRightClick = root.dataset.blockRightClick === "1";
    const detectWindowSwitch = root.dataset.detectWindowSwitch === "1";
    const detectorEnabled = {
        no_face: root.dataset.detectNoFace === "1",
        other_person: root.dataset.detectOtherPerson === "1",
        look_away: root.dataset.detectLookAway === "1",
        lip_movement: root.dataset.detectLipMovement === "1",
        mobile_phone: root.dataset.detectMobilePhone === "1",
    };
    const deadlineIso = root.dataset.deadline || "";

    const preview = root.querySelector('[data-role="preview"]');
    const camStatus = root.querySelector('[data-role="cam-status"]');
    const warnEl = root.querySelector('[data-role="warning-count"]');
    const timerEl = root.querySelector('[data-role="timer"]');
    const submitBtn = root.querySelector('[data-role="submit-btn"]');
    const saveStatusEl = root.querySelector('[data-role="save-status"]');
    const recPill = root.querySelector('[data-role="rec-pill"]');
    const consentModal = document.querySelector('[data-role="consent-modal"]');
    const consentAccept = document.querySelector('[data-role="consent-accept"]');
    const consentCheckbox = document.querySelector('[data-role="consent-checkbox"]');

    const state = {
        stream: null,
        detector: null,
        recorder: null,
        submitted: false,
        completing: false,
        warningCount: 0,
        lastSnapshotAt: 0,
        clipInFlight: false,
        pendingClip: null,
        evidenceInFlight: 0,
    };

    const mediaErrorLastFire = new Map();
    const url = (path) => `/applicant-assessment/${token}${path}`;

    function renderTimer() {
        if (!deadlineIso) { timerEl.textContent = ""; return; }
        const deadline = new Date(deadlineIso + (deadlineIso.endsWith("Z") ? "" : "Z"));
        const remaining = Math.max(0, Math.floor((deadline - new Date()) / 1000));
        const mm = String(Math.floor(remaining / 60)).padStart(2, "0");
        const ss = String(remaining % 60).padStart(2, "0");
        timerEl.textContent = `${mm}:${ss}`;
        timerEl.classList.toggle("eaa-timer--low", remaining <= 60);
        if (remaining <= 0 && !state.submitted) doSubmit();
    }
    setInterval(renderTimer, 1000);
    renderTimer();

    async function beaconMediaError(step, message, status) {
        const last = mediaErrorLastFire.get(step) || 0;
        if (Date.now() - last < MEDIA_ERROR_DEDUPE_MS) return;
        mediaErrorLastFire.set(step, Date.now());
        try {
            await fetch(url("/proctoring/media-error"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ step, message: String(message || "").slice(0, 200), status }),
                credentials: "same-origin",
                keepalive: true,
            });
        } catch (e) {}
    }

    async function postJson(path, payload) {
        const resp = await fetch(url(path), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload || {}),
            credentials: "same-origin",
        });
        return resp;
    }

    async function captureEvidence(kind, warningId) {
        // The counter is bumped synchronously so a submit landing right
        // after the warning still waits for this evidence to settle.
        state.evidenceInFlight++;
        try {
            let snapshotUrl = null;
            let snapshotKey = null;
            if (Date.now() - state.lastSnapshotAt >= SNAPSHOT_ON_SIGNAL_MIN_GAP_MS) {
                state.lastSnapshotAt = Date.now();
                const snap = await captureSnapshot(kind);
                if (snap) {
                    snapshotUrl = snap.url || null;
                    snapshotKey = snap.s3_key || null;
                }
            }
            if (state.recorder) {
                await captureAndUploadClip(kind, snapshotUrl, snapshotKey, warningId);
            }
        } finally {
            state.evidenceInFlight--;
        }
    }

    function notifyWarning(kind, count, cap) {
        if (kind === "mobile_phone") {
            showPhoneAlert(count, cap);
            return;
        }
        showWarningToast(kind, count, cap);
    }

    async function postEvent(kind, meta) {
        if (state.submitted) return;
        // Setup phase: browser permission prompts blur the window and (in
        // Chrome) exit fullscreen — none of that is the candidate's fault,
        // so no warning may fire until media permissions are settled.
        if (!proctorReady) return;
        // Pop the phone alert the instant the detector fires — the server
        // roundtrip below only refines the warning count on the overlay.
        if (kind === "mobile_phone") {
            showPhoneAlert(state.warningCount + 1, warningCap);
        }
        let warningId = null;
        let terminal = false;
        try {
            const r = await postJson("/event", { kind, meta: meta || {} });
            if (r.ok) {
                const data = await r.json();
                warningId = data.warning_id || null;
                const count = typeof data.warning_count === "number"
                    ? data.warning_count
                    : state.warningCount;
                notifyWarning(kind, count, warningCap);
                if (typeof data.warning_count === "number") updateWarnings(data.warning_count);
                terminal = data.state === "submitted" || data.state === "scored";
            } else {
                notifyWarning(kind, state.warningCount, warningCap);
            }
        } catch (e) {
            notifyWarning(kind, state.warningCount, warningCap);
        }
        // Start evidence capture BEFORE completing: the warning that trips
        // the cap is exactly the one whose clip must survive. Both submit
        // paths wait (bounded) for in-flight evidence.
        captureEvidence(kind, warningId);
        if (terminal) forceCompleted();
    }

    async function captureSnapshot(reason) {
        if (!state.stream || !preview || preview.readyState < 2) {
            await beaconMediaError("snapshot-not-ready", `readyState=${preview && preview.readyState}`);
            return null;
        }
        const canvas = document.createElement("canvas");
        const w = preview.videoWidth || 640;
        const h = preview.videoHeight || 480;
        if (!w || !h) return null;
        const scale = 320 / w;
        canvas.width = 320;
        canvas.height = Math.round(h * scale);
        const ctx = canvas.getContext("2d");
        ctx.drawImage(preview, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.6));
        if (!blob || !blob.size) {
            await beaconMediaError("snapshot-empty", "toBlob null");
            return null;
        }
        const form = new FormData();
        form.append("file", blob, `snap-${Date.now()}.jpg`);
        form.append("reason", reason);
        try {
            const resp = await fetch(url("/proctoring/snapshot"), {
                method: "POST", body: form, credentials: "same-origin",
            });
            if (!resp.ok) {
                await beaconMediaError("snapshot-not-stored", `status=${resp.status}`, resp.status);
                return null;
            }
            const data = await resp.json();
            if (!data.url) return null;
            return { url: data.url, s3_key: data.s3_key || "" };
        } catch (e) {
            await beaconMediaError("snapshot-error", String(e && e.message || e));
            return null;
        }
    }

    // One clip records at a time; a warning that lands mid-recording is
    // queued (latest wins) instead of silently dropped, and is captured as
    // soon as the current clip settles.
    async function captureAndUploadClip(reason, snapshotUrl, snapshotKey, warningId) {
        if (!state.recorder) return;
        if (state.clipInFlight) {
            state.pendingClip = { reason, snapshotUrl, snapshotKey, warningId };
            return;
        }
        state.clipInFlight = true;
        try {
            let job = { reason, snapshotUrl, snapshotKey, warningId };
            while (job) {
                try {
                    await uploadOneClip(job);
                } catch (e) {
                    await beaconMediaError("capture-error", String(e && e.message || e));
                }
                job = state.submitted ? null : state.pendingClip;
                state.pendingClip = null;
            }
        } finally {
            state.clipInFlight = false;
        }
    }

    function handleWarningResponse(data) {
        if (typeof data.warning_count === "number") updateWarnings(data.warning_count);
        if (data.state === "submitted" || data.state === "scored") forceCompleted();
    }

    async function uploadOneClip({ reason, snapshotUrl, snapshotKey, warningId }) {
        const blob = await state.recorder.captureClip();
        if (!blob) {
            await beaconMediaError("clip-empty", "recorder returned empty");
            return;
        }
        let presign = null;
        try {
            const r = await postJson("/proctoring/video/presign", {});
            if (r.ok) {
                presign = (await r.json()).presign || null;
                if (!presign) await beaconMediaError("presign-null", "no bucket configured");
            } else {
                await beaconMediaError("presign-error", `status=${r.status}`, r.status);
            }
        } catch (e) {
            await beaconMediaError("presign-error", String(e && e.message || e));
        }

        let committed = false;
        if (presign) {
            const form = new FormData();
            for (const [k, v] of Object.entries(presign.fields || {})) form.append(k, v);
            form.append("file", blob, `clip-${Date.now()}.webm`);
            let directOk = false;
            try {
                const s3Resp = await fetch(presign.url, { method: "POST", body: form });
                directOk = !!s3Resp.ok;
                if (!directOk) {
                    await beaconMediaError("s3-post-failed", `status=${s3Resp.status}`, s3Resp.status);
                }
            } catch (e) {
                await beaconMediaError("s3-post-error", String(e && e.message || e));
            }
            if (directOk) {
                try {
                    const r = await postJson("/proctoring/video/commit", {
                        key: presign.key,
                        reason,
                        snapshot_url: snapshotUrl || "",
                        snapshot_key: snapshotKey || "",
                        warning_id: warningId || 0,
                    });
                    if (r.ok) {
                        handleWarningResponse(await r.json());
                        committed = true;
                    } else {
                        await beaconMediaError("commit-error", `status=${r.status}`, r.status);
                    }
                } catch (e) {
                    await beaconMediaError("commit-error", String(e && e.message || e));
                }
            }
        }
        if (committed) return;

        // Server-side fallback. Reached when presign is unavailable (S3 not
        // configured), the direct S3 POST failed (CORS/policy/region), or
        // the commit didn't land. The server stores to S3 or, failing that,
        // as a local attachment — the clip is never dropped.
        try {
            const fallback = new FormData();
            fallback.append("file", blob, `clip-${Date.now()}.webm`);
            fallback.append("reason", reason);
            if (snapshotUrl) fallback.append("snapshot_url", snapshotUrl);
            if (snapshotKey) fallback.append("snapshot_key", snapshotKey);
            if (warningId) fallback.append("warning_id", String(warningId));
            const fbResp = await fetch(url("/proctoring/video/upload"), {
                method: "POST",
                body: fallback,
                credentials: "same-origin",
            });
            if (!fbResp.ok) {
                await beaconMediaError("commit-error", `fallback status=${fbResp.status}`, fbResp.status);
                return;
            }
            handleWarningResponse(await fbResp.json());
        } catch (e) {
            await beaconMediaError("commit-error", String(e && e.message || e));
        }
    }

    function updateWarnings(n) {
        state.warningCount = n;
        if (warnEl) warnEl.textContent = String(n);
        const warnWrap = warnEl ? warnEl.closest(".eaa-warnings") : null;
        if (warnWrap) warnWrap.classList.toggle("eaa-warnings--active", n > 0);
        if (fsGuard) fsGuard.meta.textContent = warningMetaText(n, warningCap);
        if (warningCap > 0 && n >= warningCap && !state.submitted) doSubmit();
    }

    const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

    // Navigating away aborts in-flight fetches and kills an in-progress
    // MediaRecorder — which used to lose the clip of the very warning that
    // ended the test. Give the recording a short grace period, flush it
    // early (partial clip), then wait for uploads to settle — bounded, so
    // the candidate is never stuck here.
    async function drainEvidence(graceMs) {
        if (state.evidenceInFlight <= 0) return;
        if (saveStatusEl) saveStatusEl.textContent = "Saving proctoring evidence…";
        if (graceMs > 0 && state.clipInFlight) await sleep(graceMs);
        if (state.recorder) state.recorder.flush();
        const deadline = Date.now() + SUBMIT_EVIDENCE_DRAIN_MS;
        while (state.evidenceInFlight > 0 && Date.now() < deadline) {
            if (state.recorder) state.recorder.flush();
            await sleep(250);
        }
    }

    async function forceCompleted() {
        if (state.completing) return;
        state.completing = true;
        state.submitted = true;
        await drainEvidence(SUBMIT_CLIP_GRACE_MS);
        stopMedia();
        window.location.href = url("");
    }

    function stopMedia() {
        try { state.detector && state.detector.stop(); } catch (e) {}
        try {
            if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
        } catch (e) {}
    }

    function collectAnswer(section) {
        const qid = parseInt(section.dataset.questionId, 10);
        const qtype = section.dataset.questionType;
        if (qtype === "mcq_single" || qtype === "true_false" || qtype === "rating") {
            const chosen = section.querySelector('input[type="radio"]:checked');
            return { question_id: qid, option_ids: chosen ? [parseInt(chosen.value, 10)] : [] };
        }
        if (qtype === "consent") {
            const cb = section.querySelector('input[type="checkbox"][data-role="consent"]');
            return { question_id: qid, text_answer: cb && cb.checked ? "accepted" : "" };
        }
        if (qtype === "mcq_multi") {
            const checked = section.querySelectorAll('input[type="checkbox"]:checked');
            return {
                question_id: qid,
                option_ids: Array.from(checked).map((el) => parseInt(el.value, 10)),
            };
        }
        if (qtype === "dropdown") {
            const sel = section.querySelector("select");
            const val = sel && sel.value ? parseInt(sel.value, 10) : null;
            return { question_id: qid, option_ids: val ? [val] : [] };
        }
        const input = section.querySelector('input[type="text"], input[type="url"], input[type="date"], input[type="number"], textarea');
        return { question_id: qid, text_answer: input ? input.value : "" };
    }

    async function uploadAnswerFile(section, file) {
        const qid = parseInt(section.dataset.questionId, 10);
        const statusEl = section.querySelector('[data-role="q-status"]');
        const uploadsEl = section.querySelector('[data-role="uploads"]');
        if (statusEl) statusEl.textContent = "Uploading…";
        try {
            const presignRes = await fetch(url("/answer/file/presign"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                credentials: "same-origin",
                body: JSON.stringify({ question_id: qid, mime: file.type, filename: file.name, size: file.size }),
            });
            const presignData = await presignRes.json();
            if (!presignData.ok || !presignData.presign) throw new Error(presignData.reason || "presign_failed");
            const { url: s3Url, fields } = presignData.presign;
            const form = new FormData();
            Object.entries(fields).forEach(([k, v]) => form.append(k, v));
            form.append("file", file);
            const putRes = await fetch(s3Url, { method: "POST", body: form });
            if (!putRes.ok) throw new Error("s3_put_failed");
            const storageKey = fields.key || fields.Key;
            const commitRes = await fetch(url("/answer/file/commit"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                credentials: "same-origin",
                body: JSON.stringify({
                    question_id: qid,
                    storage_key: storageKey,
                    filename: file.name,
                    mime: file.type,
                    s3_url: s3Url.replace(/\/$/, "") + "/" + storageKey,
                }),
            });
            const commitData = await commitRes.json();
            if (!commitData.ok) throw new Error(commitData.reason || "commit_failed");
            if (uploadsEl) {
                const tile = document.createElement("div");
                tile.className = "eaa-upload-tile";
                tile.textContent = commitData.name;
                uploadsEl.appendChild(tile);
            }
            if (statusEl) statusEl.textContent = "Uploaded";
        } catch (err) {
            if (statusEl) statusEl.textContent = "Upload failed";
        }
    }

    let saveBusy = 0;
    function setSaveStatus(kind) {
        if (!saveStatusEl) return;
        if (kind === "saving") {
            saveBusy++;
            saveStatusEl.textContent = "Saving…";
            return;
        }
        saveBusy = Math.max(0, saveBusy - 1);
        if (saveBusy === 0) {
            saveStatusEl.textContent = kind === "saved" ? "Saved" : "Save failed";
        }
    }

    function saveAnswer(section) {
        const payload = collectAnswer(section);
        const statusEl = section.querySelector('[data-role="q-status"]');
        if (statusEl) statusEl.textContent = "Saving…";
        setSaveStatus("saving");
        fetch(url("/answer"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            credentials: "same-origin",
        }).then((r) => {
            if (statusEl) statusEl.textContent = r.ok ? "Saved" : "Save failed";
            setSaveStatus(r.ok ? "saved" : "failed");
        }).catch(() => {
            if (statusEl) statusEl.textContent = "Save failed";
            setSaveStatus("failed");
        });
    }

    // Native file dialogs steal focus from the window (fires window.blur /
    // visibilitychange), which would otherwise trigger a proctor warning
    // even though the candidate is just picking a file. Suppress those
    // signals for a short grace window around the file-picker interaction.
    let filePickerBusyUntil = 0;
    function markFilePickerBusy(ms) {
        filePickerBusyUntil = performance.now() + ms;
    }
    function filePickerBusy() {
        return performance.now() < filePickerBusyUntil;
    }

    const sections = root.querySelectorAll(".eaa-question");
    sections.forEach((section) => {
        section.querySelectorAll('input[type="file"][data-role="answer-file"]').forEach((fileInp) => {
            fileInp.addEventListener("click", () => markFilePickerBusy(30000));
            fileInp.addEventListener("change", () => {
                markFilePickerBusy(2000);
                const f = fileInp.files && fileInp.files[0];
                if (!f) return;
                uploadAnswerFile(section, f);
                fileInp.value = "";
            });
        });
        section.querySelectorAll("input, textarea, select").forEach((inp) => {
            if (inp.type === "file") return;
            let timer = null;
            const handler = () => {
                if (timer) clearTimeout(timer);
                timer = setTimeout(() => saveAnswer(section), 400);
            };
            inp.addEventListener("change", handler);
            inp.addEventListener("input", handler);
        });
    });

    function checkConsentGate() {
        const missing = [];
        sections.forEach((section) => {
            if (section.dataset.questionType !== "consent") return;
            const cb = section.querySelector('input[type="checkbox"][data-role="consent"]');
            if (!cb || !cb.checked) {
                const num = section.querySelector(".eaa-question__num");
                missing.push(num ? num.textContent : "one question");
            }
        });
        return missing;
    }

    async function doSubmit() {
        if (state.submitted) return;
        const missingConsents = checkConsentGate();
        if (missingConsents.length) {
            const host = document.querySelector(".eaa-toast-host") || ensureToastHost();
            const err = document.createElement("div");
            err.className = "eaa-toast eaa-toast--critical";
            err.innerHTML = "<div class='eaa-toast__label'>Please accept required consents</div>"
                + "<div class='eaa-toast__meta'>Missing: " + missingConsents.join(", ") + "</div>";
            host.appendChild(err);
            setTimeout(() => err.remove(), 5000);
            return;
        }
        state.submitted = true;
        const promises = [];
        sections.forEach((section) => {
            promises.push(fetch(url("/answer"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(collectAnswer(section)),
                credentials: "same-origin",
            }).catch(() => {}));
        });
        await Promise.all(promises);
        await drainEvidence(SUBMIT_CLIP_GRACE_MS);
        stopMedia();
        const form = document.createElement("form");
        form.method = "POST";
        form.action = url("/submit");
        document.body.appendChild(form);
        form.submit();
    }

    // ── Section pagination + Review & submit (HRMS attempt-player flow).
    // Pages are shown/hidden with CSS only: every question stays in the DOM,
    // so autosave, collectAnswer() and doSubmit() behave exactly as before.
    const pages = Array.from(root.querySelectorAll(".eaa-section"));
    const questionsMain = root.querySelector(".eaa-questions");
    const navEl = root.querySelector('[data-role="nav"]');
    const prevBtn = root.querySelector('[data-role="prev-btn"]');
    const nextBtn = root.querySelector('[data-role="next-btn"]');
    const backBtn = root.querySelector('[data-role="back-btn"]');
    const reviewPanel = root.querySelector('[data-role="review-panel"]');
    const reviewGrid = root.querySelector('[data-role="review-grid"]');
    const answeredCountEl = root.querySelector('[data-role="answered-count"]');
    const sectionIndicator = root.querySelector('[data-role="section-indicator"]');
    const submitModal = document.querySelector('[data-role="submit-modal"]');
    const submitCancel = document.querySelector('[data-role="submit-cancel"]');
    const submitConfirm = document.querySelector('[data-role="submit-confirm"]');
    const unansweredNote = document.querySelector('[data-role="unanswered-note"]');
    let pageIdx = 0;

    function isAnswered(section) {
        if (section.dataset.questionType === "file_upload") {
            const uploads = section.querySelector('[data-role="uploads"]');
            return !!(uploads && uploads.children.length);
        }
        if (section.querySelector('input[type="radio"]:checked, input[type="checkbox"]:checked')) {
            return true;
        }
        const sel = section.querySelector("select");
        if (sel && sel.value) return true;
        const inp = section.querySelector('input[type="text"], input[type="url"], input[type="date"], input[type="number"], textarea');
        return !!(inp && inp.value.trim());
    }

    function showPage(i) {
        if (pages.length) {
            pageIdx = Math.max(0, Math.min(i, pages.length - 1));
            pages.forEach((p, n) => p.classList.toggle("eaa-page--hidden", n !== pageIdx));
        }
        if (sectionIndicator) {
            if (pages.length > 1) {
                const title = pages[pageIdx].querySelector(".eaa-section__title");
                sectionIndicator.textContent =
                    `Section ${pageIdx + 1} of ${pages.length}` +
                    (title ? ` · ${title.textContent}` : "");
            } else {
                sectionIndicator.textContent = "";
            }
        }
        if (prevBtn) prevBtn.disabled = !pages.length || pageIdx === 0;
        if (nextBtn) {
            nextBtn.textContent =
                (!pages.length || pageIdx === pages.length - 1) ? "Review" : "Next";
        }
        window.scrollTo(0, 0);
    }

    function buildReviewGrid() {
        let answered = 0;
        if (reviewGrid) reviewGrid.textContent = "";
        sections.forEach((section, i) => {
            const done = isAnswered(section);
            if (done) answered++;
            if (reviewGrid) {
                const cell = document.createElement("span");
                cell.className = "eaa-review__cell" + (done ? " eaa-review__cell--done" : "");
                cell.textContent = String(i + 1);
                reviewGrid.appendChild(cell);
            }
        });
        if (answeredCountEl) answeredCountEl.textContent = String(answered);
        return answered;
    }

    function enterReview() {
        buildReviewGrid();
        if (questionsMain) questionsMain.classList.add("d-none");
        if (navEl) navEl.classList.add("d-none");
        if (reviewPanel) reviewPanel.classList.remove("d-none");
        if (sectionIndicator) sectionIndicator.textContent = "Review & submit";
        window.scrollTo(0, 0);
    }

    function exitReview() {
        if (reviewPanel) reviewPanel.classList.add("d-none");
        if (questionsMain) questionsMain.classList.remove("d-none");
        if (navEl) navEl.classList.remove("d-none");
        showPage(pageIdx);
    }

    if (prevBtn) prevBtn.addEventListener("click", () => showPage(pageIdx - 1));
    if (nextBtn) {
        nextBtn.addEventListener("click", () => {
            if (!pages.length || pageIdx >= pages.length - 1) enterReview();
            else showPage(pageIdx + 1);
        });
    }
    if (backBtn) backBtn.addEventListener("click", exitReview);
    showPage(0);

    function openSubmitModal() {
        const answered = buildReviewGrid();
        const unanswered = sections.length - answered;
        if (unansweredNote) {
            unansweredNote.textContent = unanswered > 0
                ? `You have ${unanswered} unanswered question${unanswered === 1 ? "" : "s"}.`
                : "";
        }
        if (submitModal) {
            submitModal.classList.remove("d-none");
        } else if (confirm("Submit your assessment? You cannot change your answers after submitting.")) {
            doSubmit();
        }
    }
    if (submitBtn) submitBtn.addEventListener("click", openSubmitModal);
    if (submitCancel) {
        submitCancel.addEventListener("click", () => {
            if (submitModal) submitModal.classList.add("d-none");
        });
    }
    if (submitConfirm) {
        submitConfirm.addEventListener("click", () => {
            if (submitModal) submitModal.classList.add("d-none");
            doSubmit();
        });
    }

    if (detectWindowSwitch) {
        document.addEventListener("visibilitychange", () => {
            if (document.hidden && !filePickerBusy()) {
                postEvent("tab_switch", { at: new Date().toISOString() });
            }
        });
        window.addEventListener("blur", () => {
            if (filePickerBusy()) return;
            postEvent("window_change", { at: new Date().toISOString() });
        });
        window.addEventListener("focus", () => {
            if (filePickerBusy()) markFilePickerBusy(500);
        });
    }
    window.addEventListener("beforeunload", (e) => {
        if (!state.submitted) {
            e.preventDefault();
            e.returnValue = "Leaving this page will submit your assessment.";
        }
    });

    if (blockCopyPaste) {
        ["copy", "cut", "paste"].forEach((ev) => {
            document.addEventListener(ev, (e) => {
                e.preventDefault();
                postEvent("copy_paste", { via: ev });
            });
        });
        document.addEventListener("keydown", (e) => {
            if ((e.ctrlKey || e.metaKey) && BLOCKED_KEYS.has((e.key || "").toLowerCase())) {
                e.preventDefault();
                postEvent("copy_paste", { key: e.key });
            }
        });
    }
    if (blockRightClick) {
        document.addEventListener("contextmenu", (e) => {
            e.preventDefault();
            postEvent("copy_paste", { via: "contextmenu" });
        });
    }

    // ── Fullscreen enforcement ────────────────────────────────────────
    // A blocking overlay covers the test whenever the tab is not in full
    // screen (or is hidden/minimized), and keeps re-warning on a fixed
    // cadence until the candidate returns — leaving full screen once no
    // longer costs just a single warning.
    let fsGuard = null;
    // False until camera/mic permissions are settled (or not needed).
    // While false, no warnings fire and the fullscreen guard stays hidden
    // — the permission prompt itself takes the page out of fullscreen.
    let proctorReady = false;
    let tryEnterFullscreen = () => {};

    if (requireFullscreen) {
        const fsBtn = root.querySelector('[data-role="fs-btn"]');
        let fsEverEntered = false;
        let fsLastWarnAt = 0;

        tryEnterFullscreen = () => {
            const el = document.documentElement;
            if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
        };

        function ensureFsGuard() {
            if (fsGuard) return fsGuard;
            const overlay = document.createElement("div");
            overlay.className = "eaa-alert eaa-alert--fs d-none";
            const card = document.createElement("div");
            card.className = "eaa-alert__card";
            const icon = document.createElement("div");
            icon.className = "eaa-alert__icon eaa-alert__icon--fs";
            const title = document.createElement("h2");
            title.className = "eaa-alert__title";
            title.textContent = "Return to full screen";
            const text = document.createElement("p");
            text.className = "eaa-alert__text";
            text.textContent = "This test must stay in full-screen mode. You will keep receiving warnings until you return, and the test pauses on this screen.";
            const meta = document.createElement("p");
            meta.className = "eaa-alert__meta";
            meta.textContent = warningMetaText(state.warningCount, warningCap);
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "btn btn-primary";
            btn.textContent = "Return to full screen";
            btn.addEventListener("click", tryEnterFullscreen);
            card.append(icon, title, text, meta, btn);
            overlay.appendChild(card);
            document.body.appendChild(overlay);
            fsGuard = { overlay, meta };
            return fsGuard;
        }

        const fsCompliant = () =>
            !!document.fullscreenElement && !document.hidden;

        function syncFsGuard() {
            if (state.submitted || !proctorReady) {
                if (fsGuard) fsGuard.overlay.classList.add("d-none");
                return;
            }
            if (fsCompliant()) {
                if (fsGuard) fsGuard.overlay.classList.add("d-none");
                return;
            }
            const guard = ensureFsGuard();
            guard.meta.textContent = warningMetaText(state.warningCount, warningCap);
            guard.overlay.classList.remove("d-none");
            // Warnings only start once the candidate has been in full
            // screen — before that the blocking overlay is enforcement
            // enough (programmatic entry without a gesture is rejected
            // by the browser, so t=0 is never compliant).
            if (
                fsEverEntered
                && !filePickerBusy()
                && performance.now() - fsLastWarnAt >= FS_WARN_REPEAT_MS
            ) {
                fsLastWarnAt = performance.now();
                postEvent("fullscreen_exit", { at: new Date().toISOString() });
            }
        }

        if (fsBtn) fsBtn.addEventListener("click", tryEnterFullscreen);
        tryEnterFullscreen();
        setTimeout(() => {
            if (fsBtn) fsBtn.classList.toggle("d-none", !!document.fullscreenElement);
        }, 800);
        document.addEventListener("fullscreenchange", () => {
            if (fsBtn) fsBtn.classList.toggle("d-none", !!document.fullscreenElement);
            if (document.fullscreenElement) {
                fsEverEntered = true;
            } else if (!proctorReady) {
                // The camera/mic permission prompt yanks the page out of
                // fullscreen (Chrome does this by design). Disarm exit
                // warnings: the candidate must re-enter via the guard
                // button once setup is done, and only exits AFTER that
                // count against them.
                fsEverEntered = false;
            } else if (fsEverEntered && !state.submitted && !filePickerBusy()) {
                // Immediate strike on exit; syncFsGuard repeats it every
                // FS_WARN_REPEAT_MS until compliance.
                fsLastWarnAt = performance.now();
                postEvent("fullscreen_exit", { at: new Date().toISOString() });
            }
            syncFsGuard();
        });
        document.addEventListener("visibilitychange", syncFsGuard);
        window.addEventListener("resize", syncFsGuard);
        setInterval(syncFsGuard, 1000);
    }

    function showConsent(resolve) {
        if (!consentModal) { resolve(true); return; }
        consentModal.classList.remove("d-none");
        const enable = () => {
            if (consentAccept) consentAccept.disabled = !(consentCheckbox && consentCheckbox.checked);
        };
        if (consentCheckbox) {
            consentCheckbox.addEventListener("change", enable);
            enable();
        }
        if (consentAccept) {
            consentAccept.addEventListener("click", () => {
                consentModal.classList.add("d-none");
                // The click is a user gesture — the one moment the browser
                // will honour a fullscreen request without a dedicated
                // button press.
                tryEnterFullscreen();
                resolve(true);
            }, { once: true });
        }
    }

    // Blocking overlay shown while the browser permission prompt is up —
    // it keeps the questions covered (no unproctored reading time) and
    // explains what to click. Swaps to a "blocked + reload" card if the
    // candidate denies access.
    let setupOverlay = null;
    function showSetupOverlay(blocked) {
        const deviceLabel = requireMic ? "camera and microphone" : "camera";
        if (!setupOverlay) {
            const overlay = document.createElement("div");
            overlay.className = "eaa-alert eaa-alert--setup";
            const card = document.createElement("div");
            card.className = "eaa-alert__card";
            const icon = document.createElement("div");
            icon.className = "eaa-alert__icon eaa-alert__icon--cam";
            const title = document.createElement("h2");
            title.className = "eaa-alert__title";
            const text = document.createElement("p");
            text.className = "eaa-alert__text";
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "btn btn-primary d-none";
            btn.textContent = "Reload page";
            btn.addEventListener("click", () => window.location.reload());
            card.append(icon, title, text, btn);
            overlay.appendChild(card);
            document.body.appendChild(overlay);
            setupOverlay = { overlay, title, text, btn };
        }
        if (blocked) {
            setupOverlay.title.textContent = "Camera access blocked";
            setupOverlay.text.textContent =
                `This test cannot start without your ${deviceLabel}. `
                + "Enable access for this site in your browser settings, then reload.";
            setupOverlay.btn.classList.remove("d-none");
        } else {
            setupOverlay.title.textContent = `Allow ${deviceLabel} access`;
            setupOverlay.text.textContent =
                "Click “Allow” on the browser permission pop-up to start your proctored test.";
            setupOverlay.btn.classList.add("d-none");
        }
    }
    function hideSetupOverlay() {
        if (setupOverlay) {
            setupOverlay.overlay.remove();
            setupOverlay = null;
        }
    }

    async function startProctoring() {
        if (!requireWebcam) {
            if (camStatus) camStatus.textContent = "Camera: not required";
            proctorReady = true;
            return;
        }
        showSetupOverlay(false);
        try {
            const consentResp = await postJson("/proctoring/consent", { version: "v1" });
            if (!consentResp.ok) {
                await beaconMediaError("consent-error", `status=${consentResp.status}`, consentResp.status);
            }
        } catch (e) {
            await beaconMediaError("consent-error", String(e && e.message || e));
        }

        try {
            state.stream = await navigator.mediaDevices.getUserMedia({
                video: { width: { ideal: 1280 }, height: { ideal: 720 } },
                audio: requireMic ? {
                    echoCancellation: true,
                    noiseSuppression: true,
                } : false,
            });
            preview.srcObject = state.stream;
            if (camStatus) camStatus.textContent = "Camera: live";
        } catch (e) {
            if (camStatus) camStatus.textContent = "Camera: BLOCKED — assessment cannot start";
            showSetupOverlay(true);
            proctorReady = true;
            return;
        }
        hideSetupOverlay();
        // Permissions settled — warnings are armed from here on.
        proctorReady = true;

        // The detector reports status on every animation frame; around a
        // decision boundary the label would flap several times a second and
        // every width change reflows the proctor strip (visible flicker).
        // Only relabel the pill once a state has held for a short window.
        let pillShown = "REC";
        let pillPending = null;
        let pillPendingAt = 0;
        const PILL_STABLE_MS = 400;

        // Append ?proctordebug=1 to the test URL (or set
        // localStorage.eaaProctorDebug = "1") to get per-pass detector
        // logs — model/delegate chosen, every object detection with its
        // label + score, and each fired signal — in the console.
        let proctorDebug = false;
        try {
            proctorDebug = new URLSearchParams(window.location.search).has("proctordebug")
                || window.localStorage.getItem("eaaProctorDebug") === "1";
        } catch (e) {}

        try {
            state.detector = new WebcamDetector({
                video: preview,
                enabled: detectorEnabled,
                debug: proctorDebug,
                onDiagnostic: (step, message) => beaconMediaError(step, message),
                onStatus: ({ faces, lookingAway, phone }) => {
                    if (!recPill) return;
                    let text = "REC";
                    if (faces === 0) text = "No face";
                    else if (faces > 1) text = "Multiple faces";
                    else if (phone) text = "Phone seen";
                    else if (lookingAway) text = "Look at screen";
                    if (text === pillShown) {
                        pillPending = null;
                        return;
                    }
                    const now = performance.now();
                    if (pillPending !== text) {
                        pillPending = text;
                        pillPendingAt = now;
                        return;
                    }
                    if (now - pillPendingAt >= PILL_STABLE_MS) {
                        pillShown = text;
                        pillPending = null;
                        recPill.textContent = text;
                    }
                },
                onSignal: (kind, detail) => {
                    postEvent(kind, { at: new Date().toISOString(), ...(detail || {}) });
                },
            });
            await state.detector.init();
            state.detector.start();
        } catch (e) {
            await beaconMediaError("detector-error", String(e && e.message || e));
        }

        if (ClipRecorder.isSupported()) {
            state.recorder = new ClipRecorder(state.stream, { seconds: CLIP_SECONDS });
        } else {
            await beaconMediaError("recorder-unsupported", "MediaRecorder or webm not supported");
        }

        setTimeout(() => captureSnapshot("start").then((u) => { if (u) state.lastSnapshotAt = Date.now(); }), 1500);
        setInterval(() => {
            if (!state.submitted) captureSnapshot("periodic").then((u) => { if (u) state.lastSnapshotAt = Date.now(); });
        }, SNAPSHOT_PERIOD_MS);
    }

    await new Promise((resolve) => showConsent(resolve));
    startProctoring();
});
