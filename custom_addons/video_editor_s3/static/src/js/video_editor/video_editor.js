/** @odoo-module **/

import {
    Component,
    onMounted,
    onWillStart,
    onWillUnmount,
    useEffect,
    useRef,
    useState,
} from "@odoo/owl";
import { registry } from "@web/core/registry";
import { router } from "@web/core/browser/router";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { EditorToolbar } from "./editor_toolbar";
import { EditorTimeline } from "./editor_timeline";

const POLL_INTERVAL_MS = 1500;
const CROP_MIN = 16;
const CROP_CORNERS = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const ASPECT_PRESETS = [
    { label: "Auto", value: null },
    { label: "9:16 (Reel)", value: 9 / 16 },
    { label: "16:9 (Wide)", value: 16 / 9 },
    { label: "1:1 (Square)", value: 1 },
    { label: "4:5 (Portrait)", value: 4 / 5 },
];
const RESIZE_PRESETS = [
    { label: "Original", value: null },
    { label: "1080p", value: { w: 1920, h: 1080 } },
    { label: "720p", value: { w: 1280, h: 720 } },
    { label: "480p", value: { w: 854, h: 480 } },
    { label: "1080×1920 (Reel)", value: { w: 1080, h: 1920 } },
    { label: "1080×1080 (Square)", value: { w: 1080, h: 1080 } },
];
const FILTER_DEFAULTS = { brightness: 0, contrast: 1, saturation: 1 };

function defaultConfig(duration) {
    return {
        trim: { start: 0, end: duration || 0 },
        crop: null,
        rotate: 0,
        resize: null,
        mute: false,
        filter: { ...FILTER_DEFAULTS },
    };
}

export class VideoEditor extends Component {
    static template = "video_editor_s3.VideoEditor";
    static components = { EditorToolbar, EditorTimeline };
    static props = ["*"];

    setup() {
        this.api = useService("video_editor_s3");
        this.notification = useService("notification");
        this.action = useService("action");
        this.videoRef = useRef("video");
        this.stageRef = useRef("stage");
        this.cropBoxRef = useRef("cropBox");
        this.editorRootRef = useRef("editorRoot");

        this.state = useState({
            loading: false,
            saving: false,
            rendering: false,
            exporting: false,
            mode: "trim",
            projectId: this.props.action?.params?.project_id || null,
            project: null,
            job: null,
            jobId: null,
            duration: 0,
            currentTime: 0,
            playing: false,
            videoError: null,
            videoDisplayTick: 0,
            sourceKind: "source",
            sourceUrl: null,
            config: defaultConfig(0),
            aspect: null,
            inputS3Url: "",
            polling: false,
            statusBanner: null,
        });

        this._cropDrag = null;
        this._cropResize = null;
        this._mouseMove = this._onCropMouseMove.bind(this);
        this._mouseUp = this._onCropMouseUp.bind(this);
        this._pollTimer = null;
        this._resizeObserver = null;

        onWillStart(async () => {
            if (this.state.projectId) {
                await this._refreshProject();
            }
        });

        onMounted(() => {
            window.addEventListener("mousemove", this._mouseMove);
            window.addEventListener("mouseup", this._mouseUp);
            if (this.stageRef.el && window.ResizeObserver) {
                this._resizeObserver = new ResizeObserver(() => {
                    this.state.videoDisplayTick++;
                });
                this._resizeObserver.observe(this.stageRef.el);
            }
        });

        onWillUnmount(() => {
            window.removeEventListener("mousemove", this._mouseMove);
            window.removeEventListener("mouseup", this._mouseUp);
            if (this._resizeObserver) {
                this._resizeObserver.disconnect();
                this._resizeObserver = null;
            }
            this._stopPolling();
        });

        useEffect(
            () => {
                const video = this.videoRef.el;
                if (!video) return;
                const onLoaded = () => {
                    this.state.duration = video.duration || 0;
                    if (!this.state.config.trim || !this.state.config.trim.end) {
                        this.state.config.trim = { start: 0, end: this.state.duration };
                    }
                    this.state.videoDisplayTick++;
                };
                const onTime = () => { this.state.currentTime = video.currentTime || 0; };
                const onPlay = () => { this.state.playing = true; };
                const onPause = () => { this.state.playing = false; };
                const onErr = () => { this.state.videoError = _t("Video failed to load."); };
                video.addEventListener("loadedmetadata", onLoaded);
                video.addEventListener("timeupdate", onTime);
                video.addEventListener("play", onPlay);
                video.addEventListener("pause", onPause);
                video.addEventListener("error", onErr);
                return () => {
                    video.removeEventListener("loadedmetadata", onLoaded);
                    video.removeEventListener("timeupdate", onTime);
                    video.removeEventListener("play", onPlay);
                    video.removeEventListener("pause", onPause);
                    video.removeEventListener("error", onErr);
                };
            },
            () => [this.state.sourceUrl]
        );
    }

    get aspectPresets() { return ASPECT_PRESETS; }
    get resizePresets() { return RESIZE_PRESETS; }
    get cropCorners() { return CROP_CORNERS; }

    get videoFilterStyle() {
        const f = this.state.config.filter || FILTER_DEFAULTS;
        return `filter: brightness(${1 + (f.brightness || 0)}) contrast(${f.contrast || 1}) saturate(${f.saturation || 1});`;
    }

    get stageFrameStyle() {
        if (!this.state.aspect) return "";
        return `aspect-ratio: ${this.state.aspect};`;
    }

    get cropOverlayStyle() {
        const proj = this._videoProjection();
        const crop = this.state.config.crop;
        if (!proj || !crop) return "display:none;";
        const sx = proj.sx;
        const sy = proj.sy;
        const left = proj.left + crop.x * sx;
        const top = proj.top + crop.y * sy;
        const width = crop.w * sx;
        const height = crop.h * sy;
        return `left:${left}px;top:${top}px;width:${width}px;height:${height}px;`;
    }

    _videoProjection() {
        const video = this.videoRef.el;
        const stage = this.stageRef.el;
        if (!video || !stage || !video.videoWidth || !video.videoHeight) return null;
        const rect = video.getBoundingClientRect();
        const stageRect = stage.getBoundingClientRect();
        const naturalRatio = video.videoWidth / video.videoHeight;
        const elementRatio = rect.width / rect.height;
        let drawW, drawH;
        if (naturalRatio > elementRatio) {
            drawW = rect.width;
            drawH = rect.width / naturalRatio;
        } else {
            drawH = rect.height;
            drawW = rect.height * naturalRatio;
        }
        const left = (rect.left - stageRect.left) + (rect.width - drawW) / 2;
        const top = (rect.top - stageRect.top) + (rect.height - drawH) / 2;
        return {
            left, top, drawW, drawH,
            sx: drawW / video.videoWidth,
            sy: drawH / video.videoHeight,
            videoWidth: video.videoWidth,
            videoHeight: video.videoHeight,
        };
    }

    async _refreshProject() {
        if (!this.state.projectId) return;
        this.state.loading = true;
        try {
            const project = await this.api.fetchProject(this.state.projectId);
            this._applyProject(project);
        } catch (err) {
            this.notification.add(err.message || String(err), { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    _applyProject(project) {
        if (!project) return;
        this.state.project = project;
        if (project.id && this.state.projectId !== project.id) {
            this.state.projectId = project.id;
        }
        if (project.id) {
            router.pushState({ project_id: project.id });
        }
        if (project.editing_config && Object.keys(project.editing_config).length) {
            this.state.config = { ...defaultConfig(project.duration_seconds || 0), ...project.editing_config };
        } else if (!this.state.config.trim.end && project.duration_seconds) {
            this.state.config.trim.end = project.duration_seconds;
        }
        let kind = null;
        if (project.has_edited) kind = "edited";
        else if (project.has_preview) kind = "preview";
        else if (project.has_source) kind = "source";
        if (kind) {
            this.state.sourceKind = kind;
            this.state.sourceUrl = this.api.streamUrl(project.id, kind);
        } else {
            this.state.sourceKind = "source";
            this.state.sourceUrl = null;
            if (project.s3_source_url) {
                this.state.videoError = _t("Source URL is set but not playable. Configure S3 credentials in Settings.");
            }
        }
        if (project.active_job_id) {
            this.state.jobId = project.active_job_id;
            this._startPolling();
        }
        this._refreshBanner();
    }

    _refreshBanner() {
        const p = this.state.project;
        if (!p) { this.state.statusBanner = null; return; }
        if (p.state === "processing") {
            this.state.statusBanner = _t("Rendering…");
        } else if (p.state === "exporting") {
            this.state.statusBanner = _t("Uploading to S3…");
        } else if (p.state === "error") {
            this.state.statusBanner = _t("Last operation failed. See Jobs tab.");
        } else {
            this.state.statusBanner = null;
        }
    }

    async onLoadFromUrl() {
        const url = (this.state.inputS3Url || "").trim();
        if (!url) {
            this.notification.add(_t("Provide an S3 URL"), { type: "warning" });
            return;
        }
        this.state.loading = true;
        try {
            const payload = await this.api.loadProject({ s3Url: url });
            this.state.projectId = payload.id;
            this._applyProject(payload);
            this.state.jobId = payload.active_job_id || null;
            if (this.state.jobId) this._startPolling();
        } catch (err) {
            this.notification.add(err.message || String(err), { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    _startPolling() {
        if (this.state.polling) return;
        this.state.polling = true;
        const tick = async () => {
            if (!this.state.jobId) {
                this.state.polling = false;
                return;
            }
            try {
                const job = await this.api.getJobStatus(this.state.jobId);
                this.state.job = job;
                if (["done", "failed", "cancelled"].includes(job.status)) {
                    const completedJobType = job.job_type;
                    this.state.polling = false;
                    this.state.jobId = null;
                    await this._refreshProject();
                    if (job.status === "failed") {
                        this.notification.add(job.error_message || _t("Job failed"), { type: "danger" });
                    } else if (job.status === "done") {
                        this.notification.add(_t("Job complete"), { type: "success" });
                        if (
                            completedJobType === "render" &&
                            this.state.project &&
                            this.state.project.has_edited
                        ) {
                            await this.onExport();
                        }
                    }
                    return;
                }
            } catch (err) {
                this.state.polling = false;
                this.notification.add(err.message || String(err), { type: "danger" });
                return;
            }
            this._pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
        };
        this._pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
    }

    _stopPolling() {
        if (this._pollTimer) {
            clearTimeout(this._pollTimer);
            this._pollTimer = null;
        }
        this.state.polling = false;
    }

    onChangeMode(mode) { this.state.mode = mode; }

    onChangeAspect(value) {
        const num = value === "" || value === "null" ? null : parseFloat(value);
        this.state.aspect = Number.isFinite(num) ? num : null;
        this.state.videoDisplayTick++;
    }

    async onPlayPause() {
        const v = this.videoRef.el;
        if (!v) return;
        if (v.paused) {
            try {
                await v.play();
            } catch (err) {
                this.state.videoError = err && err.message ? err.message : _t("Video failed to play.");
            }
        } else {
            v.pause();
        }
    }

    onSeek(seconds) {
        const v = this.videoRef.el;
        if (v) v.currentTime = Math.max(0, Math.min(this.state.duration, seconds || 0));
    }

    onScrub(ev) {
        this.onSeek(parseFloat(ev.target.value));
    }

    onStageClick() {
        if (this.state.mode === "crop" && !this.state.config.crop) {
            const proj = this._videoProjection();
            if (!proj) return;
            const w = Math.round(proj.videoWidth * 0.6);
            const h = Math.round(proj.videoHeight * 0.6);
            this.state.config.crop = {
                x: Math.round((proj.videoWidth - w) / 2),
                y: Math.round((proj.videoHeight - h) / 2),
                w, h,
            };
        }
    }

    onCropDragStart(ev) {
        if (this.state.mode !== "crop" || !this.state.config.crop) return;
        ev.preventDefault();
        ev.stopPropagation();
        this._cropDrag = {
            startX: ev.clientX,
            startY: ev.clientY,
            crop: { ...this.state.config.crop },
        };
    }

    onCropResizeStart(corner, ev) {
        if (this.state.mode !== "crop" || !this.state.config.crop) return;
        ev.preventDefault();
        ev.stopPropagation();
        this._cropResize = {
            corner,
            startX: ev.clientX,
            startY: ev.clientY,
            crop: { ...this.state.config.crop },
        };
    }

    _onCropMouseMove(ev) {
        if (!this._cropDrag && !this._cropResize) return;
        const proj = this._videoProjection();
        if (!proj) return;
        const dx = (ev.clientX - (this._cropDrag || this._cropResize).startX) / proj.sx;
        const dy = (ev.clientY - (this._cropDrag || this._cropResize).startY) / proj.sy;
        if (this._cropDrag) {
            const c = this._cropDrag.crop;
            const nx = Math.round(Math.max(0, Math.min(proj.videoWidth - c.w, c.x + dx)));
            const ny = Math.round(Math.max(0, Math.min(proj.videoHeight - c.h, c.y + dy)));
            this.state.config.crop = { ...c, x: nx, y: ny };
        } else if (this._cropResize) {
            const { corner, crop } = this._cropResize;
            let { x, y, w, h } = crop;
            if (corner.includes("n")) { y = crop.y + dy; h = crop.h - dy; }
            if (corner.includes("s")) { h = crop.h + dy; }
            if (corner.includes("w")) { x = crop.x + dx; w = crop.w - dx; }
            if (corner.includes("e")) { w = crop.w + dx; }
            if (w < CROP_MIN) { w = CROP_MIN; if (corner.includes("w")) x = crop.x + crop.w - CROP_MIN; }
            if (h < CROP_MIN) { h = CROP_MIN; if (corner.includes("n")) y = crop.y + crop.h - CROP_MIN; }
            x = Math.max(0, Math.min(proj.videoWidth - w, x));
            y = Math.max(0, Math.min(proj.videoHeight - h, y));
            w = Math.min(proj.videoWidth - x, w);
            h = Math.min(proj.videoHeight - y, h);
            this.state.config.crop = { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
        }
    }

    _onCropMouseUp() {
        this._cropDrag = null;
        this._cropResize = null;
    }

    onClearCrop() { this.state.config.crop = null; }

    onTrimChange(trim) {
        this.state.config.trim = trim;
        const v = this.videoRef.el;
        if (v && (v.currentTime < trim.start || v.currentTime > trim.end)) {
            v.currentTime = trim.start;
        }
    }

    onResetSlot(field) {
        if (field === "trim") this.state.config.trim = { start: 0, end: this.state.duration };
        else if (field === "crop") this.state.config.crop = null;
        else if (field === "rotate") this.state.config.rotate = 0;
        else if (field === "resize") this.state.config.resize = null;
        else if (field === "mute") this.state.config.mute = false;
        else if (field === "filter") this.state.config.filter = { ...FILTER_DEFAULTS };
    }

    onToggleMute() { this.state.config.mute = !this.state.config.mute; }
    onUpdateFilter(filter) { this.state.config.filter = filter; }
    onUpdateRotate(deg) { this.state.config.rotate = deg; }
    onUpdateResize(preset) { this.state.config.resize = preset; }

    async onRender(preview = false) {
        if (!this.state.projectId) {
            this.notification.add(_t("Load a video first"), { type: "warning" });
            return;
        }
        const flag = preview ? "saving" : "rendering";
        this.state[flag] = true;
        try {
            const job = await this.api.processProject({
                projectId: this.state.projectId,
                config: this.state.config,
                preview,
            });
            this.state.jobId = job.id;
            this.state.job = job;
            this._startPolling();
            this.notification.add(preview ? _t("Preview queued") : _t("Render queued"), { type: "info" });
        } catch (err) {
            this.notification.add(err.message || String(err), { type: "danger" });
        } finally {
            this.state[flag] = false;
        }
    }

    async onExport() {
        if (!this.state.projectId) return;
        this.state.exporting = true;
        try {
            const job = await this.api.exportToS3({ projectId: this.state.projectId });
            this.state.jobId = job.id;
            this.state.job = job;
            this._startPolling();
            this.notification.add(_t("Export queued"), { type: "info" });
        } catch (err) {
            this.notification.add(err.message || String(err), { type: "danger" });
        } finally {
            this.state.exporting = false;
        }
    }

    async onCancelJob() {
        if (!this.state.jobId) return;
        try {
            await this.api.cancelJob(this.state.jobId);
            this.notification.add(_t("Cancellation requested"), { type: "info" });
        } catch (err) {
            this.notification.add(err.message || String(err), { type: "danger" });
        }
    }

    onSwitchSourceKind(kind) {
        if (!this.state.project) return;
        const has = {
            source: this.state.project.has_source,
            edited: this.state.project.has_edited,
            preview: this.state.project.has_preview,
        };
        if (!has[kind]) return;
        this.state.sourceKind = kind;
        this.state.sourceUrl = this.api.streamUrl(this.state.project.id, kind);
    }

    onClose() {
        this.action.doAction("video_editor_s3.action_video_editor_project", {
            clearBreadcrumbs: true,
        });
    }
}

registry.category("actions").add("video_editor_s3.video_editor", VideoEditor);
