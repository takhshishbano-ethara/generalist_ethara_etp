import {
    FaceLandmarker,
    FilesetResolver,
    ObjectDetector,
} from "/etp_applicant_assessment/static/lib/mediapipe/vision_bundle.mjs";

const WASM_PATH = "/etp_applicant_assessment/static/lib/mediapipe/wasm";
const FACE_MODEL = "/etp_applicant_assessment/static/lib/mediapipe/models/face_landmarker.task";
const OBJECT_MODEL = "/etp_applicant_assessment/static/lib/mediapipe/models/efficientdet_lite0.tflite";

const FACE_INTERVAL_MS = 300;
const OBJECT_INTERVAL_MS = 1500;
const SUSTAIN = 3;
const COOLDOWN_MS = 12000;
const YAW_LIMIT = 28;
const PITCH_LIMIT = 22;
const PHONE_SCORE = 0.45;
const MOUTH_OPEN_THRESHOLD = 0.15;
const MOUTH_WINDOW = 10;
const MOUTH_MIN_TRANSITIONS = 3;
const PHONE_LABELS = /cell phone|mobile phone|phone/i;

const DEFAULT_ENABLED = {
    no_face: true,
    other_person: true,
    look_away: true,
    lip_movement: true,
    mobile_phone: true,
};

export class WebcamDetector {
    constructor({ video, onSignal, onStatus, enabled }) {
        this.video = video;
        this.onSignal = onSignal || (() => {});
        this.onStatus = onStatus || (() => {});
        this.enabled = { ...DEFAULT_ENABLED, ...(enabled || {}) };
        this.faceLandmarker = null;
        this.objectDetector = null;
        this._running = false;
        this._rafId = null;
        this._lastFaceAt = 0;
        this._lastObjectAt = 0;
        this._streaks = { no_face: 0, other_person: 0, look_away: 0, lip_movement: 0, mobile_phone: 0 };
        this._cooldowns = { no_face: 0, other_person: 0, look_away: 0, lip_movement: 0, mobile_phone: 0 };
        this._mouthSamples = [];
    }

    async init() {
        const vision = await FilesetResolver.forVisionTasks(WASM_PATH);
        this.faceLandmarker = await FaceLandmarker.createFromOptions(vision, {
            baseOptions: { modelAssetPath: FACE_MODEL, delegate: "GPU" },
            runningMode: "VIDEO",
            numFaces: 3,
            outputFaceBlendshapes: true,
            outputFacialTransformationMatrixes: true,
        });
        this.objectDetector = await ObjectDetector.createFromOptions(vision, {
            baseOptions: { modelAssetPath: OBJECT_MODEL, delegate: "GPU" },
            runningMode: "VIDEO",
            scoreThreshold: PHONE_SCORE,
            maxResults: 5,
        });
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

        let faces = 0;
        let lookingAway = false;
        let phoneSeen = false;

        if (now - this._lastFaceAt >= FACE_INTERVAL_MS && this.faceLandmarker) {
            this._lastFaceAt = now;
            let res;
            try { res = this.faceLandmarker.detectForVideo(this.video, now); }
            catch (e) { return; }
            const marks = (res && res.faceLandmarks) || [];
            faces = marks.length;
            this._updateStreak("no_face", faces === 0);
            this._updateStreak("other_person", faces > 1);
            if (faces >= 1) {
                const matrices = (res.facialTransformationMatrixes || []);
                const mat = matrices[0];
                if (mat && mat.data && mat.data.length === 16) {
                    const yawDeg = _yawDeg(mat.data);
                    const pitchDeg = _pitchDeg(mat.data);
                    lookingAway = Math.abs(yawDeg) > YAW_LIMIT || Math.abs(pitchDeg) > PITCH_LIMIT;
                }
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
                this._updateStreak("look_away", false);
                this._updateStreak("lip_movement", false);
                this._mouthSamples = [];
            }
        }

        if (now - this._lastObjectAt >= OBJECT_INTERVAL_MS && this.objectDetector) {
            this._lastObjectAt = now;
            let res;
            try { res = this.objectDetector.detectForVideo(this.video, now); }
            catch (e) { return; }
            const dets = (res && res.detections) || [];
            for (const d of dets) {
                const cats = d.categories || [];
                for (const c of cats) {
                    if (c.score >= PHONE_SCORE && PHONE_LABELS.test(c.categoryName || "")) {
                        phoneSeen = true;
                        break;
                    }
                }
                if (phoneSeen) break;
            }
            this._updateStreak("mobile_phone", phoneSeen);
        }

        this.onStatus({ faces, lookingAway, phone: phoneSeen });
    }

    _updateStreak(kind, isPositive) {
        if (this.enabled[kind] === false) {
            this._streaks[kind] = 0;
            return;
        }
        if (isPositive) {
            this._streaks[kind]++;
            if (this._streaks[kind] >= SUSTAIN) {
                const now = performance.now();
                if (now - this._cooldowns[kind] >= COOLDOWN_MS) {
                    this._cooldowns[kind] = now;
                    try { this.onSignal(kind); } catch (e) {}
                }
                this._streaks[kind] = SUSTAIN;
            }
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
