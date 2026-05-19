/** @odoo-module **/

import {
    Component,
    onMounted,
    onWillStart,
    onWillUnmount,
    useRef,
    useState,
} from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const DURATIONS = [3, 4, 5, 6, 8, 9, 10, 12, 15];
const ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9"];
const RESOLUTIONS = ["480p", "720p", "1080p"];

const VARIANTS = [
    {
        value: "bytedance/seedance-2.0",
        label: "PRO",
        version: "V2.0",
        blurb: "\u2192 seedance-2.0 \u2014 full cinematic quality, ~$0.62/sec.",
    },
    {
        value: "bytedance/seedance-2.0-fast",
        label: "FAST",
        version: "V2.0",
        blurb: "\u2192 seedance-2.0/fast \u2014 ~$0.24/sec, lower latency.",
    },
];

const POLL_INTERVAL_MS = 4000;
const PROMPT_MAX = 4000;
const REEL_LIMIT = 12;
const MAX_ATTEMPTS = 3;
const WORKING_STATES = new Set(["queued", "submitting", "polling", "downloading"]);

export class CrowleyStudio extends Component {
    static template = "crowley_ai_vid_gen.Studio";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");

        this.durations = DURATIONS;
        this.aspectRatios = ASPECT_RATIOS;
        this.resolutions = RESOLUTIONS;
        this.variants = VARIANTS;
        this.promptMax = PROMPT_MAX;

        this.videoRef = useRef("canvasVideo");
        this.promptRef = useRef("promptInput");

        this.state = useState({
            tab: "text",
            prompt: "",
            duration: 15,
            aspectRatio: "16:9",
            resolution: "720p",
            generateAudio: true,
            variant: "bytedance/seedance-2.0",
            seed: null,
            seedLocked: false,
            seedInput: "",
            jobsCount: 0,
            now: this._formatClock(new Date()),
            status: "idle",
            currentJobId: null,
            currentJobState: null,
            currentAttemptId: null,
            currentAttempts: [],
            currentVideoUrl: "",
            currentVideoName: "",
            currentCost: 0,
            currentResolution: "",
            currentDuration: 0,
            currentFileSize: 0,
            currentError: "",
            elapsedSeconds: 0,
            reel: [],
            zipping: false,
        });

        this._pollTimer = null;
        this._clockTimer = null;
        this._elapsedTimer = null;
        this._workStartedAt = null;

        onWillStart(async () => {
            await this._refreshDashboard();
        });

        onMounted(() => {
            this._clockTimer = setInterval(() => {
                this.state.now = this._formatClock(new Date());
            }, 1000);
        });

        onWillUnmount(() => {
            this._stopPolling();
            this._stopElapsed();
            if (this._clockTimer) {
                clearInterval(this._clockTimer);
                this._clockTimer = null;
            }
        });
    }

    get promptLength() {
        return (this.state.prompt || "").length;
    }
    get promptEmpty() {
        return !this.state.prompt || !this.state.prompt.trim();
    }
    get canGenerate() {
        // Generate kicks off attempt #1 only — once a job exists, REFINE
        // takes over the action slot.
        return !this.promptEmpty && !this._inflight() && !this.state.currentJobId;
    }
    get hasAttemptInFlight() {
        return (this.state.currentAttempts || []).some((a) =>
            WORKING_STATES.has(a.state)
        );
    }
    get attemptsUsed() {
        return (this.state.currentAttempts || []).length;
    }
    get attemptsRemaining() {
        return Math.max(0, MAX_ATTEMPTS - this.attemptsUsed);
    }
    get canRefine() {
        return (
            this.state.currentJobId != null &&
            this.attemptsUsed >= 1 &&
            this.attemptsUsed < MAX_ATTEMPTS &&
            !this._inflight() &&
            !this.hasAttemptInFlight
        );
    }
    get showRefineSlot() {
        // The REFINE control replaces GENERATE once a job exists.
        return this.state.currentJobId != null && this.attemptsUsed >= 1;
    }
    get refineLabel() {
        if (this.attemptsUsed >= MAX_ATTEMPTS) return "\u2726 ALL ATTEMPTS USED";
        if (this.attemptsUsed === 2) return "\u2726 REFINE PROMPT (FINAL ATTEMPT)";
        return "\u2726 REFINE PROMPT";
    }
    get refineSubtext() {
        if (this.attemptsUsed >= MAX_ATTEMPTS) {
            return "All 3 attempts used \u2014 start a new job to keep iterating.";
        }
        if (this.hasAttemptInFlight) return "Waiting for current attempt to finish\u2026";
        if (this.attemptsUsed === 2) return "Last shot \u00B7 ATTEMPT 3 of 3";
        return "Revise the prompt above and re-render";
    }
    get promptDisabled() {
        // Lock the textarea while an attempt is in flight, or once all
        // three attempts have been spent. The user can revise again
        // whenever the canvas shows a ready state with attempts left.
        if (!this.state.currentJobId) return false;
        if (this._inflight() || this.hasAttemptInFlight) return true;
        if (this.attemptsUsed >= MAX_ATTEMPTS) return true;
        return false;
    }
    get promptCounterClass() {
        const n = this.promptLength;
        if (n >= 3900) return "o_crow_count o_crow_count--danger";
        if (n >= 3500) return "o_crow_count o_crow_count--warn";
        return "o_crow_count";
    }
    get variantBlurb() {
        const v = this.variants.find((x) => x.value === this.state.variant);
        return v ? v.blurb : "";
    }
    get jobsCountFormatted() {
        const n = this.state.jobsCount;
        if (n > 999) return "999+";
        return String(n).padStart(3, "0");
    }
    get reelCount() {
        return this.state.reel.length;
    }
    get canvasMode() {
        if (this.state.status === "failed") return "failed";
        if (this.state.status === "ready" && this.state.currentVideoUrl) return "ready";
        if (WORKING_STATES.has(this.state.status)) return "working";
        return "idle";
    }
    get workingLabel() {
        return ({
            queued: "QUEUED",
            submitting: "SUBMITTING",
            polling: "GENERATING",
            downloading: "DOWNLOADING",
        })[this.state.status] || "WORKING";
    }
    get elapsedFormatted() {
        const s = Math.max(0, this.state.elapsedSeconds);
        const m = Math.floor(s / 60);
        const r = s % 60;
        return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
    }
    get generateSubtext() {
        if (this._inflight()) return "GENERATING\u2026";
        if (this.promptEmpty) return "ENTER A PROMPT";
        return "READY";
    }
    get seedSubtitle() {
        if (this.state.seedLocked && this.state.seed != null) {
            return `LOCKED \u00B7 SEED ${this.state.seed}`;
        }
        return "RANDOM PER RUN";
    }
    get seedHelp() {
        return this.state.seedLocked
            ? "Locked: same prompt + seed produces deterministic output."
            : "Unlocked: each run uses a fresh random seed \u2014 explore variations.";
    }

    chipClass(active) {
        return active ? "o_crow_chip o_crow_chip--active" : "o_crow_chip";
    }
    chipBigClass(active) {
        return active ? "o_crow_chip o_crow_chip--big o_crow_chip--active" : "o_crow_chip o_crow_chip--big";
    }
    variantChipClass(value) {
        return this.state.variant === value
            ? "o_crow_chip o_crow_chip--variant o_crow_chip--active"
            : "o_crow_chip o_crow_chip--variant";
    }
    reelItemClass(id) {
        return this.state.currentJobId === id
            ? "o_crow_reel_item o_crow_reel_item--selected"
            : "o_crow_reel_item";
    }

    attemptChipClass(slot) {
        const att = this._attemptForSlot(slot);
        const classes = ["o_crowley_studio__attempt_chip"];
        if (!att) {
            classes.push("o_crowley_studio__attempt_chip--empty");
            return classes.join(" ");
        }
        const isActive =
            this.state.currentAttemptId === att.id ||
            (this.state.currentAttemptId == null && this._latestAttempt()?.id === att.id);
        if (isActive) classes.push("o_crowley_studio__attempt_chip--active");
        const stateMod = {
            ready: "ready",
            failed: "failed",
            cancelled: "cancelled",
        }[att.state] || (WORKING_STATES.has(att.state) ? "polling" : "polling");
        classes.push(`o_crowley_studio__attempt_chip--${stateMod}`);
        return classes.join(" ");
    }
    attemptChipTooltip(slot) {
        const att = this._attemptForSlot(slot);
        if (!att) return `Attempt ${slot} — not yet generated`;
        const head = `Attempt ${att.attempt_number} · ${att.state.toUpperCase()}`;
        const p = (att.prompt || "").trim();
        const trunc = p.length > 120 ? p.slice(0, 117) + "\u2026" : p;
        const cost = att.cost_usd ? ` · $${Number(att.cost_usd).toFixed(2)}` : "";
        return `${head}${cost}\n${trunc}`;
    }
    _attemptForSlot(slot) {
        return (this.state.currentAttempts || []).find(
            (a) => a.attempt_number === slot
        );
    }
    _latestAttempt() {
        const arr = this.state.currentAttempts || [];
        if (!arr.length) return null;
        return arr.reduce(
            (acc, a) => (acc == null || a.attempt_number > acc.attempt_number ? a : acc),
            null,
        );
    }
    _latestReadyAttempt() {
        const arr = (this.state.currentAttempts || []).filter((a) => a.state === "ready");
        if (!arr.length) return null;
        return arr.reduce(
            (acc, a) => (acc == null || a.attempt_number > acc.attempt_number ? a : acc),
            null,
        );
    }

    _inflight() {
        return WORKING_STATES.has(this.state.status);
    }

    onTabClick(tab) {
        if (tab !== "text") return;
        this.state.tab = tab;
    }
    onPromptInput(ev) {
        const v = ev.target.value || "";
        this.state.prompt = v.slice(0, PROMPT_MAX);
    }
    onDurationClick(value) {
        this.state.duration = value;
    }
    onAspectClick(value) {
        this.state.aspectRatio = value;
    }
    onResolutionClick(value) {
        this.state.resolution = value;
    }
    onAudioToggle() {
        this.state.generateAudio = !this.state.generateAudio;
    }
    onVariantClick(value) {
        this.state.variant = value;
    }

    onSeedInput(ev) {
        const v = (ev.target.value || "").trim();
        this.state.seedInput = v;
        if (!this.state.seedLocked) return;
        const n = parseInt(v, 10);
        if (!Number.isNaN(n) && n >= 0 && n < 2 ** 31) {
            this.state.seed = n;
        }
    }
    onSeedLockToggle() {
        if (this.state.seedLocked) {
            this.state.seedLocked = false;
            this.state.seed = null;
            this.state.seedInput = "";
            return;
        }
        let n = parseInt(this.state.seedInput, 10);
        if (Number.isNaN(n) || n < 0 || n >= 2 ** 31) {
            n = this._randomSeed();
        }
        this.state.seed = n;
        this.state.seedInput = String(n);
        this.state.seedLocked = true;
    }
    onSeedDice() {
        if (!this.state.seedLocked) return;
        const n = this._randomSeed();
        this.state.seed = n;
        this.state.seedInput = String(n);
    }

    _randomSeed() {
        return Math.floor(Math.random() * (2 ** 31 - 1));
    }

    async onApiKeyClick() {
        try {
            await this.action.doAction("base_setup.action_general_configuration");
        } catch (err) {
            this._notifyError(err);
        }
    }

    async onGenerateClick() {
        if (!this.canGenerate) return;

        this.state.status = "submitting";
        this.state.currentError = "";
        this.state.currentVideoUrl = "";
        this.state.currentJobId = null;
        this.state.currentJobState = null;
        this.state.currentAttemptId = null;
        this.state.currentAttempts = [];
        this.state.elapsedSeconds = 0;
        this._startElapsed();

        const payload = {
            prompt: this.state.prompt.trim(),
            model: this.state.variant,
            resolution: this.state.resolution,
            duration: this.state.duration,
            aspect_ratio: this.state.aspectRatio,
            generate_audio: this.state.generateAudio,
        };
        if (this.state.seedLocked && this.state.seed != null) {
            payload.seed = this.state.seed;
        }

        let jobId = null;
        try {
            jobId = await this.orm.create("crowley.ai.vid.gen.job", [payload]);
            if (Array.isArray(jobId)) jobId = jobId[0];
            this.state.currentJobId = jobId;
            await this.orm.call("crowley.ai.vid.gen.job", "action_generate", [[jobId]]);
            this.state.status = "polling";
            this._startPolling(jobId);
        } catch (err) {
            this._stopElapsed();
            this.state.status = "failed";
            this.state.currentError = this._formatError(err);
            this._notifyError(err);
        } finally {
            await this._refreshDashboard();
        }
    }

    _startPolling(jobId) {
        this._stopPolling();
        const tick = async () => {
            try {
                const rows = await this.orm.read(
                    "crowley.ai.vid.gen.job",
                    [jobId],
                    [
                        "state",
                        "video_play_url",
                        "error_message",
                        "cost_usd",
                        "name",
                        "resolution",
                        "duration",
                        "file_size",
                    ],
                );
                if (!rows || !rows.length) {
                    this._stopPolling();
                    this._stopElapsed();
                    this.state.status = "idle";
                    return;
                }
                const row = rows[0];
                this.state.currentJobState = row.state;
                this.state.currentVideoName = row.name || "";
                this.state.currentCost = row.cost_usd || 0;
                this.state.currentResolution = row.resolution || "";
                this.state.currentDuration = row.duration || 0;
                this.state.currentFileSize = row.file_size || 0;

                // Refresh attempts every tick so a fresh refinement appears
                // in the switcher strip mid-flight.
                await this._refreshAttempts(jobId);

                if (row.state === "ready") {
                    this.state.status = "ready";
                    this.state.currentError = "";
                    const latestReady = this._latestReadyAttempt();
                    if (latestReady) {
                        this.state.currentAttemptId = latestReady.id;
                        this.state.currentVideoUrl = latestReady.video_play_url || row.video_play_url || "";
                    } else {
                        this.state.currentVideoUrl = row.video_play_url || "";
                    }
                    this._stopPolling();
                    this._stopElapsed();
                    await this._refreshDashboard();
                } else if (row.state === "failed") {
                    this.state.status = "failed";
                    this.state.currentError = row.error_message || _t("Job failed.");
                    this._stopPolling();
                    this._stopElapsed();
                    await this._refreshDashboard();
                } else if (row.state === "cancelled") {
                    this.state.status = "idle";
                    this._stopPolling();
                    this._stopElapsed();
                    this.state.currentJobId = null;
                    await this._refreshDashboard();
                } else if (WORKING_STATES.has(row.state)) {
                    this.state.status = row.state;
                }
            } catch (err) {
                this.state.status = "failed";
                this.state.currentError = this._formatError(err);
                this._stopPolling();
                this._stopElapsed();
            }
        };
        tick();
        this._pollTimer = setInterval(tick, POLL_INTERVAL_MS);
    }

    async _refreshAttempts(jobId) {
        const targetId = jobId || this.state.currentJobId;
        if (!targetId) {
            this.state.currentAttempts = [];
            return;
        }
        try {
            const rows = await this.orm.searchRead(
                "crowley.ai.vid.gen.attempt",
                [["job_id", "=", targetId]],
                [
                    "id",
                    "attempt_number",
                    "state",
                    "video_play_url",
                    "prompt",
                    "cost_usd",
                ],
                { order: "attempt_number asc" },
            );
            this.state.currentAttempts = rows || [];
        } catch (err) {
            // eslint-disable-next-line no-console
            console.warn("[crowley_ai_vid_gen.studio] attempts refresh failed", err);
        }
    }

    async onRefineClick() {
        if (!this.canRefine) return;
        const trimmed = (this.state.prompt || "").trim();
        if (!trimmed) {
            this.notification.add(
                _t("Enter a revised prompt before refining."),
                { type: "danger", title: _t("Crowley Studio") },
            );
            return;
        }
        const latest = this._latestAttempt();
        if (latest && (latest.prompt || "").trim() === trimmed) {
            this.notification.add(
                _t("Refining with the same prompt — output may vary due to model nondeterminism."),
                { type: "info", title: _t("Crowley Studio") },
            );
        }
        const jobId = this.state.currentJobId;
        this.state.status = "submitting";
        this.state.currentError = "";
        this.state.elapsedSeconds = 0;
        this._startElapsed();
        try {
            await this.orm.call(
                "crowley.ai.vid.gen.job",
                "action_refine",
                [[jobId]],
                { new_prompt: trimmed },
            );
            this.state.status = "polling";
            await this._refreshAttempts(jobId);
            this._startPolling(jobId);
        } catch (err) {
            this._stopElapsed();
            this.state.status = "ready";
            this._notifyError(err);
        } finally {
            await this._refreshDashboard();
        }
    }

    async onAttemptThumbClick(slot) {
        const att = this._attemptForSlot(slot);
        if (!att) return;
        if (this.hasAttemptInFlight) return;
        this.state.currentAttemptId = att.id;
        if (att.state === "ready" && att.video_play_url) {
            this.state.currentVideoUrl = att.video_play_url;
            this.state.status = "ready";
            this.state.currentError = "";
        } else if (att.state === "failed") {
            this.state.status = "failed";
            this.state.currentVideoUrl = "";
            try {
                const rows = await this.orm.read(
                    "crowley.ai.vid.gen.attempt",
                    [att.id],
                    ["error_message"],
                );
                this.state.currentError = rows?.[0]?.error_message || _t("Attempt failed.");
            } catch (err) {
                this.state.currentError = this._formatError(err);
            }
        }
    }

    _stopPolling() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    _startElapsed() {
        this._stopElapsed();
        this._workStartedAt = Date.now();
        this._elapsedTimer = setInterval(() => {
            this.state.elapsedSeconds = Math.floor((Date.now() - this._workStartedAt) / 1000);
        }, 1000);
    }
    _stopElapsed() {
        if (this._elapsedTimer) {
            clearInterval(this._elapsedTimer);
            this._elapsedTimer = null;
        }
    }

    async onResumeDownload() {
        if (!this.state.currentJobId) return;
        try {
            await this.orm.call("crowley.ai.vid.gen.job", "action_resume_download", [
                [this.state.currentJobId],
            ]);
            this.state.status = "downloading";
            this.state.currentError = "";
            this._startElapsed();
            this._startPolling(this.state.currentJobId);
        } catch (err) {
            this._notifyError(err);
        }
    }
    async onRetry() {
        if (!this.state.currentJobId) return;
        try {
            await this.orm.call("crowley.ai.vid.gen.job", "action_retry", [
                [this.state.currentJobId],
            ]);
            this.state.status = "polling";
            this.state.currentError = "";
            this.state.elapsedSeconds = 0;
            this._startElapsed();
            this._startPolling(this.state.currentJobId);
        } catch (err) {
            this._notifyError(err);
        }
    }

    async _refreshDashboard() {
        try {
            const [count, reel] = await Promise.all([
                this.orm.searchCount("crowley.ai.vid.gen.job", []),
                this.orm.searchRead(
                    "crowley.ai.vid.gen.job",
                    [["state", "=", "ready"]],
                    [
                        "id",
                        "name",
                        "video_play_url",
                        "resolution",
                        "duration",
                        "mimetype",
                        "create_date",
                    ],
                    { limit: REEL_LIMIT, order: "create_date desc" },
                ),
            ]);
            this.state.jobsCount = count;
            this.state.reel = reel;
        } catch (err) {
            // eslint-disable-next-line no-console
            console.warn("[crowley_ai_vid_gen.studio] dashboard refresh failed", err);
        }
    }

    async onReelClick(itemId) {
        try {
            const rows = await this.orm.read(
                "crowley.ai.vid.gen.job",
                [itemId],
                [
                    "state",
                    "video_play_url",
                    "error_message",
                    "cost_usd",
                    "name",
                    "resolution",
                    "duration",
                    "file_size",
                ],
            );
            if (!rows || !rows.length) return;
            const row = rows[0];
            this._stopPolling();
            this._stopElapsed();
            this.state.currentJobId = itemId;
            this.state.currentJobState = row.state;
            this.state.currentVideoName = row.name || "";
            this.state.currentCost = row.cost_usd || 0;
            this.state.currentResolution = row.resolution || "";
            this.state.currentDuration = row.duration || 0;
            this.state.currentFileSize = row.file_size || 0;
            this.state.currentAttemptId = null;
            await this._refreshAttempts(itemId);

            if (row.state === "ready") {
                this.state.status = "ready";
                const latestReady = this._latestReadyAttempt();
                if (latestReady) {
                    this.state.currentAttemptId = latestReady.id;
                    this.state.currentVideoUrl = latestReady.video_play_url || row.video_play_url || "";
                } else {
                    this.state.currentVideoUrl = row.video_play_url || "";
                }
                this.state.currentError = "";
            } else if (row.state === "failed") {
                this.state.status = "failed";
                this.state.currentVideoUrl = "";
                this.state.currentError = row.error_message || "";
            } else if (WORKING_STATES.has(row.state)) {
                this.state.status = row.state;
                this.state.currentVideoUrl = "";
                this.state.currentError = "";
                this.state.elapsedSeconds = 0;
                this._startElapsed();
                this._startPolling(itemId);
            } else {
                this.state.status = "idle";
                this.state.currentVideoUrl = "";
            }
        } catch (err) {
            this._notifyError(err);
        }
    }

    onDownloadAll() {
        if (!this.state.reel || !this.state.reel.length) return;
        this.state.zipping = true;
        const w = window.open("/crowley/seedance/zip", "_self");
        if (!w) {
            this._notifyError(_t("Pop-up blocked. Allow downloads from this site."));
        }
        setTimeout(() => {
            this.state.zipping = false;
        }, 1500);
    }

    _formatClock(d) {
        const p = (n) => String(n).padStart(2, "0");
        return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
    _formatError(err) {
        if (!err) return _t("Unknown error.");
        if (typeof err === "string") return err;
        return err.data?.message || err.message || String(err);
    }
    _notifyError(err) {
        this.notification.add(this._formatError(err), {
            type: "danger",
            title: _t("Crowley Studio"),
        });
    }

    formatCanvasMeta() {
        const parts = [];
        if (this.state.currentVideoName) parts.push(this.state.currentVideoName);
        if (this.state.currentResolution) parts.push(this.state.currentResolution);
        if (this.state.currentDuration) parts.push(`${this.state.currentDuration}s`);
        if (this.state.currentCost) parts.push(`$${this.state.currentCost.toFixed(2)}`);
        return parts.join(" \u00B7 ");
    }
}

registry.category("actions").add("crowley_ai_vid_gen.studio", CrowleyStudio);
