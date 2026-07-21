import {
    FaceLandmarker,
    FilesetResolver,
    ObjectDetector,
} from "/etp_applicant_assessment/static/lib/mediapipe/vision_bundle.mjs";

const WASM_PATH = "/etp_applicant_assessment/static/lib/mediapipe/wasm";
const FACE_MODEL = "/etp_applicant_assessment/static/lib/mediapipe/models/face_landmarker.task";
// Ordered by accuracy. Lite2 runs a 448px input vs Lite0's 320px — a
// hand-held phone a metre from the webcam is only ~40px tall for Lite0,
// which is why it practically never crossed the score threshold. Lite0
// stays as the fallback when the Lite2 asset is missing or fails to load.
const OBJECT_MODELS = [
    { name: "efficientdet_lite2", path: "/etp_applicant_assessment/static/lib/mediapipe/models/efficientdet_lite2.tflite" },
    { name: "efficientdet_lite0", path: "/etp_applicant_assessment/static/lib/mediapipe/models/efficientdet_lite0.tflite" },
];

const FACE_INTERVAL_MS = 300;
// Object cadence self-tunes to ~2× inference time between these bounds
// (fast GPUs sit at the floor; slow CPUs back off instead of janking),
// and a confirmation pass runs sooner while a phone hit is pending.
const OBJECT_INTERVAL_MS = 300;
const OBJECT_INTERVAL_MAX_MS = 2000;
const OBJECT_CONFIRM_INTERVAL_MS = 400;
const SUSTAIN = 3;
const PHONE_SUSTAIN = 2;
const COOLDOWN_MS = 12000;
const YAW_LIMIT = 28;
const PITCH_LIMIT = 22;
const DETECTOR_MIN_SCORE = 0.20;
// Moderate hits need a quick second pass to confirm; a single hit at or
// above the strong score fires on its own (phone clearly in view).
const PHONE_SCORE = 0.25;
const PHONE_SCORE_STRONG = 0.45;
const MOUTH_OPEN_THRESHOLD = 0.15;
const MOUTH_WINDOW = 10;
const MOUTH_MIN_TRANSITIONS = 3;
const PHONE_LABELS = /cell phone|mobile phone|phone/i;
// After this many consecutive runtime failures the object detector is
// rebuilt on the CPU delegate — a GPU context that creates fine but
// throws on every inference otherwise kills phone detection silently.
const OBJECT_FAILURES_BEFORE_REINIT = 5;

const DEFAULT_ENABLED = {
    no_face: true,
    other_person: true,
    look_away: true,
    lip_movement: true,
    mobile_phone: true,
};

export class WebcamDetector {
    constructor({ video, onSignal, onStatus, onDiagnostic, enabled, debug }) {
        this.video = video;
        this.onSignal = onSignal || (() => {});
        this.onStatus = onStatus || (() => {});
        // (step, message) — wired to the media-error beacon so detector
        // failures show up in the assessment's proctoring report instead
        // of dying silently in the candidate's console.
        this.onDiagnostic = onDiagnostic || (() => {});
        this.enabled = { ...DEFAULT_ENABLED, ...(enabled || {}) };
        this.debug = !!debug;
        this.faceLandmarker = null;
        this.objectDetector = null;
        this.objectModelName = "";
        this.objectDelegate = "";
        this._vision = null;
        this._running = false;
        this._rafId = null;
        this._lastFaceAt = 0;
        this._lastObjectAt = 0;
        this._objectIntervalMs = OBJECT_INTERVAL_MS;
        this._objectFailures = 0;
        this._objectReiniting = false;
        this._streaks = { no_face: 0, other_person: 0, look_away: 0, lip_movement: 0, mobile_phone: 0 };
        // Negative start so the very first sustained detection can signal
        // immediately — performance.now() begins at 0 on page load, so a
        // 0-initialised cooldown silently muted every camera signal for
        // the first COOLDOWN_MS of the test.
        this._cooldowns = {
            no_face: -COOLDOWN_MS, other_person: -COOLDOWN_MS, look_away: -COOLDOWN_MS,
            lip_movement: -COOLDOWN_MS, mobile_phone: -COOLDOWN_MS,
        };
        this._mouthSamples = [];
        this._lastFaces = 0;
        this._lastLookingAway = false;
        this._lastPhoneSeen = false;
        this._lastPhoneScore = 0;
    }

    _log(...args) {
        if (this.debug) console.log("[proctor]", ...args);
    }

    async init() {
        this._vision = await FilesetResolver.forVisionTasks(WASM_PATH);
        // The GPU delegate fails outright on machines without usable
        // WebGL2 (VMs, older iGPUs, remote desktops); retry on CPU.
        this.faceLandmarker = await this._createWithCpuFallback(
            "face_landmarker",
            (delegate) => FaceLandmarker.createFromOptions(this._vision, {
                baseOptions: { modelAssetPath: FACE_MODEL, delegate },
                runningMode: "VIDEO",
                numFaces: 3,
                outputFaceBlendshapes: true,
                outputFacialTransformationMatrixes: true,
            }),
        );
        await this._initObjectDetector();
    }

    async _initObjectDetector(forceDelegate) {
        let lastErr = null;
        for (const model of OBJECT_MODELS) {
            try {
                this.objectDetector = await this._createWithCpuFallback(
                    model.name,
                    (delegate) => ObjectDetector.createFromOptions(this._vision, {
                        baseOptions: { modelAssetPath: model.path, delegate },
                        runningMode: "VIDEO",
                        scoreThreshold: DETECTOR_MIN_SCORE,
                        maxResults: 5,
                    }),
                    forceDelegate,
                );
                this.objectModelName = model.name;
                if (model !== OBJECT_MODELS[0]) {
                    this.onDiagnostic(
                        "object-model-fallback",
                        `${OBJECT_MODELS[0].name} unavailable, using ${model.name}`,
                    );
                }
                this._log("object detector ready:", model.name, this.objectDelegate);
                return;
            } catch (e) {
                lastErr = e;
            }
        }
        this.objectDetector = null;
        this.onDiagnostic(
            "object-detector-error",
            `init failed: ${String(lastErr && lastErr.message || lastErr).slice(0, 120)}`,
        );
    }

    async _createWithCpuFallback(label, create, forceDelegate) {
        if (forceDelegate) {
            const inst = await create(forceDelegate);
            this.objectDelegate = forceDelegate;
            return inst;
        }
        try {
            const inst = await create("GPU");
            this.objectDelegate = "GPU";
            return inst;
        } catch (e) {
            this._log(label, "GPU delegate failed, retrying on CPU:", e);
            const inst = await create("CPU");
            this.objectDelegate = "CPU";
            return inst;
        }
    }

    start() {
        if (this._running) return;
        this._running = true;
        const loop = () => {
            if (!this._running) return;
            this._tick();
            this._rafId = requestAnimationFrame(loop);
        };
        this._rafId = requestAnimationFrame(loop);
    }

    stop() {
        this._running = false;
        if (this._rafId) cancelAnimationFrame(this._rafId);
        this._rafId = null;
        try { this.faceLandmarker && this.faceLandmarker.close(); } catch (e) {}
        try { this.objectDetector && this.objectDetector.close(); } catch (e) {}
    }

    _tick() {
        const now = performance.now();
        if (!this.video || this.video.readyState < 2) return;

        if (now - this._lastFaceAt >= FACE_INTERVAL_MS && this.faceLandmarker) {
            this._lastFaceAt = now;
            let res = null;
            // A transient face-detector error must not abort the tick:
            // an early return here would also starve the phone detector.
            try { res = this.faceLandmarker.detectForVideo(this.video, now); }
            catch (e) {
                res = null;
                this.onDiagnostic("face-detector-error", String(e && e.message || e).slice(0, 120));
            }
            if (res) this._handleFaceResult(res);
        }

        // While a phone hit awaits confirmation, poll sooner than the
        // tuned cadence — the second (confirming) pass is what unlocks
        // the warning, so it should not wait a full slow-CPU interval.
        const phonePending = this._streaks.mobile_phone > 0
            && this._streaks.mobile_phone < PHONE_SUSTAIN;
        const objInterval = phonePending
            ? Math.min(this._objectIntervalMs, OBJECT_CONFIRM_INTERVAL_MS)
            : this._objectIntervalMs;
        if (now - this._lastObjectAt >= objInterval && this.objectDetector) {
            this._lastObjectAt = now;
            let res = null;
            try {
                res = this.objectDetector.detectForVideo(this.video, now);
                this._objectFailures = 0;
            } catch (e) {
                res = null;
                this._objectFailures++;
                this.onDiagnostic("object-detector-error", String(e && e.message || e).slice(0, 120));
                if (this._objectFailures >= OBJECT_FAILURES_BEFORE_REINIT && !this._objectReiniting) {
                    this._objectReiniting = true;
                    this._log("object detector failing repeatedly, rebuilding on CPU");
                    try { this.objectDetector.close(); } catch (err) {}
                    this.objectDetector = null;
                    this._initObjectDetector("CPU").finally(() => {
                        this._objectFailures = 0;
                        this._objectReiniting = false;
                    });
                }
            }
            if (res) {
                this._handleObjectResult(res);
                // Track ~2× inference time so fast machines poll at the
                // floor and slow CPUs back off instead of janking.
                const took = performance.now() - now;
                const tuned = Math.min(
                    OBJECT_INTERVAL_MAX_MS,
                    Math.max(OBJECT_INTERVAL_MS, Math.round(took * 2)),
                );
                if (Math.abs(tuned - this._objectIntervalMs) > 100) {
                    this._log(`object pass ${Math.round(took)}ms; interval -> ${tuned}ms`);
                }
                this._objectIntervalMs = tuned;
            }
        }

        this.onStatus({
            faces: this._lastFaces,
            lookingAway: this._lastLookingAway,
            phone: this._lastPhoneSeen,
        });
    }

    _handleFaceResult(res) {
        const marks = res.faceLandmarks || [];
        const faces = marks.length;
        this._lastFaces = faces;
        this._updateStreak("no_face", faces === 0);
        this._updateStreak("other_person", faces > 1);
        if (faces >= 1) {
            let lookingAway = false;
            const matrices = (res.facialTransformationMatrixes || []);
            const mat = matrices[0];
            if (mat && mat.data && mat.data.length === 16) {
                const yawDeg = _yawDeg(mat.data);
                const pitchDeg = _pitchDeg(mat.data);
                lookingAway = Math.abs(yawDeg) > YAW_LIMIT || Math.abs(pitchDeg) > PITCH_LIMIT;
            }
            this._lastLookingAway = lookingAway;
            this._updateStreak("look_away", lookingAway);
            const shapes = (res.faceBlendshapes || [])[0];
            let jawOpen = 0;
            if (shapes && shapes.categories) {
                const jaw = shapes.categories.find((c) => c.categoryName === "jawOpen");
                jawOpen = jaw ? jaw.score : 0;
            }
            const open = jawOpen >= MOUTH_OPEN_THRESHOLD;
            this._mouthSamples.push(open);
            if (this._mouthSamples.length > MOUTH_WINDOW) this._mouthSamples.shift();
            let transitions = 0;
            for (let i = 1; i < this._mouthSamples.length; i++) {
                if (this._mouthSamples[i] !== this._mouthSamples[i - 1]) transitions++;
            }
            const talking = (
                this._mouthSamples.length >= MOUTH_WINDOW
                && transitions >= MOUTH_MIN_TRANSITIONS
            );
            this._updateStreak("lip_movement", talking);
        } else {
            this._lastLookingAway = false;
            this._updateStreak("look_away", false);
            this._updateStreak("lip_movement", false);
            this._mouthSamples = [];
        }
    }

    _handleObjectResult(res) {
        let phoneSeen = false;
        let phoneScore = 0;
        const dets = res.detections || [];
        if (this.debug && dets.length) {
            this._log(
                "objects:",
                dets.map((d) => (d.categories || [])
                    .map((c) => `${c.categoryName} ${c.score.toFixed(2)}`)
                    .join("/")).join(", "),
            );
        }
        for (const d of dets) {
            for (const c of (d.categories || [])) {
                if (c.score >= PHONE_SCORE && PHONE_LABELS.test(c.categoryName || "")) {
                    phoneSeen = true;
                    phoneScore = Math.max(phoneScore, c.score);
                }
            }
        }
        this._lastPhoneSeen = phoneSeen;
        this._lastPhoneScore = phoneScore;
        const strong = phoneScore >= PHONE_SCORE_STRONG;
        this._updateStreak("mobile_phone", phoneSeen, {
            sustain: PHONE_SUSTAIN,
            decay: true,
            // A confident sighting satisfies the whole sustain in one
            // pass — the warning fires without waiting to re-confirm.
            increment: strong ? PHONE_SUSTAIN : 1,
            detail: {
                confidence: Math.round(phoneScore * 100) / 100,
                model: this.objectModelName,
                strong,
            },
        });
    }

    _updateStreak(kind, isPositive, opts) {
        if (this.enabled[kind] === false) {
            this._streaks[kind] = 0;
            return;
        }
        const sustain = (opts && opts.sustain) || SUSTAIN;
        if (isPositive) {
            this._streaks[kind] += (opts && opts.increment) || 1;
            if (this._streaks[kind] >= sustain) {
                const now = performance.now();
                if (now - this._cooldowns[kind] >= COOLDOWN_MS) {
                    this._cooldowns[kind] = now;
                    this._log("signal:", kind, (opts && opts.detail) || "");
                    try { this.onSignal(kind, (opts && opts.detail) || {}); } catch (e) {}
                }
                this._streaks[kind] = sustain;
            }
        } else if (opts && opts.decay) {
            // Detection flickers frame to frame on a hand-held phone; a
            // miss halves the progress instead of erasing it, so an
            // alternating hit/miss pattern still accumulates to a signal.
            this._streaks[kind] = Math.max(0, this._streaks[kind] - 0.5);
        } else {
            this._streaks[kind] = 0;
        }
    }
}

function _yawDeg(m) {
    return Math.atan2(-m[2], Math.sqrt(m[0] * m[0] + m[1] * m[1])) * 180 / Math.PI;
}

function _pitchDeg(m) {
    return Math.atan2(m[6], m[10]) * 180 / Math.PI;
}
