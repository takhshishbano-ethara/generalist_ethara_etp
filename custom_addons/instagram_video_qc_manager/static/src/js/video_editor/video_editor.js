/** @odoo-module **/
/**
 * Fullscreen OWL video editor for the Instagram Video QC Manager.
 *
 * Builds a JSON editing configuration (trim/crop/filters) that is POSTed to
 * the server-side controller for FFmpeg rendering.
 *
 * UX overview
 * -----------
 * - Center stage = video element (no native controls) + crop overlay with
 *   eight resize handles and a "darken everything outside the crop" effect.
 *   The overlay is anchored exactly to the rendered video pixels (we
 *   recompute on every resize) so the cropped FFmpeg output matches what
 *   the user sees.
 * - Right under the stage: a play/pause button + a *scrub bar* that always
 *   shows the current and total time. Clicking anywhere on the bar seeks.
 * - Below that: a *trim bar* with two draggable handles defining the
 *   start/end of the trimmed segment that will be exported.
 *
 * On save+render the FFmpeg processor uses both ``crop=W:H:X:Y`` and
 * ``-ss``/``-t`` so the rendered video contains ONLY the cropped pixel
 * window and ONLY the selected time range.
 */
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
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { EditorTimeline } from "./editor_timeline";
import { EditorToolbar } from "./editor_toolbar";
// NOTE: EditorPromptPanel is intentionally NOT imported here anymore.
// Prompt + QC review have been moved to the dashboard form view, so the
// editor stage now focuses solely on trim / crop / filter operations.

export class VideoEditor extends Component {
    static template = "instagram_video_qc_manager.VideoEditor";
    static components = { EditorTimeline, EditorToolbar };
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.videoQC = useService("video_qc");
        this.notification = useService("notification");
        this.action = useService("action");

        this.params = this.props.action?.params || {};
        this.videoRef = useRef("video");
        this.stageRef = useRef("stage");
        this.cropBoxRef = useRef("cropBox");
        // Root <div class="o_video_qc_editor"> — used to set the
        // ``--vqc-zoom`` CSS variable that drives the editor's zoom level.
        this.editorRootRef = useRef("editorRoot");

        this.state = useState({
            loading: true,
            saving: false,
            rendering: false,
            mode: "trim",
            task: null,
            version: null,
            taskId: this.params.task_id,
            versionId: this.params.version_id || null,

            sourceKind: this.params.source_kind || "original_1",

            duration: 0,
            currentTime: 0,
            playing: false,
            videoError: null,
            // Force re-renders when the rendered video size changes so the
            // crop overlay can re-project itself.
            videoDisplayTick: 0,

            availableSources: [],

            // Per-slot editing config.  The user picks "always render
            // both slots", so a single version owns two trim windows +
            // two crop rectangles, one for each source.  Shared
            // filters/transforms live at the top level and are merged
            // into both slot configs by the backend renderer.
            config: {
                slot_1: {
                    trim: { start: 0, end: 0 },
                    crop: null,
                },
                slot_2: {
                    trim: { start: 0, end: 0 },
                    crop: null,
                },
                rotate: 0,
                resize: null,
                mute: false,
                brightness: 0,
                contrast: 1,
                saturation: 1,
            },
            aspect: "free",
            history: [],

            // ---- Zoom ----
            // Current zoom multiplier applied to the editor body.  1.0
            // means "designed at 1440x900".  We allow [0.5, 2.0].
            zoom: 1,
            // When true, the zoom auto-adjusts on viewport resize to keep
            // the whole editor on screen.  Manual button clicks flip this
            // to false until the user clicks the "Fit" button again.
            zoomIsAuto: true,

            // ---- Stage aspect ratio ----
            // The aspect ratio the *preview frame* is locked to so the
            // user sees the clip as it will appear in its target format
            // (e.g. a 9:16 Instagram reel).  "auto" lets the video keep
            // its natural intrinsic ratio.  This is purely a viewfinder
            // — the render is still controlled by the Crop tool.
            stageAspect: "auto",
        });

        // Non-reactive drag state for the crop box and the bottom strips.
        this._cropDrag = null;
        this._cropResize = null;
        this._scrubDrag = null;
        this._trimDrag = null;
        this._resizeObserver = null;
        this._onWindowResize = () => {
            if (this.state.zoomIsAuto) {
                this._applyZoom(this._autoFitZoom());
            }
        };

        onWillStart(async () => {
            await this._initialise();
        });

        onMounted(() => {
            this._attachVideoListeners();
            this._observeStageResize();
            this._initZoom();
            window.addEventListener("resize", this._onWindowResize);
        });

        onWillUnmount(() => {
            if (this._resizeObserver) {
                this._resizeObserver.disconnect();
                this._resizeObserver = null;
            }
            window.removeEventListener("resize", this._onWindowResize);
        });

        useEffect(
            (src) => {
                const v = this.videoRef.el;
                if (v && src) {
                    v.load();
                }
            },
            () => [this.sourceUrl],
        );
    }

    // ------------------------------------------------------------------
    // Lifecycle helpers
    // ------------------------------------------------------------------
    async _initialise() {
        try {
            this.state.task = await this.videoQC.loadTask(this.state.taskId);
            this._computeAvailableSources();
            if (!this.state.availableSources.find((s) => s.key === this.state.sourceKind)) {
                this.state.sourceKind = this.state.availableSources[0]?.key || "original_1";
            }
            if (!this.state.versionId) {
                const created = await this.videoQC.newVersion(this.state.taskId);
                this.state.versionId = created.version_id;
            }
            this.state.version = await this.videoQC.loadVersion(this.state.versionId);
            this._hydrateConfigFromVersion();
            this.state.history = await this.videoQC.listEditHistory(this.state.versionId);
        } catch (err) {
            this.notification.add(err.message || _t("Failed to load editor."), {
                type: "danger",
            });
        } finally {
            this.state.loading = false;
        }
    }

    _computeAvailableSources() {
        const t = this.state.task;
        const sources = [];
        if (t?.original_video_1_attachment) {
            sources.push({ key: "original_1", label: _t("Original #1") });
        }
        if (t?.original_video_2_attachment) {
            sources.push({ key: "original_2", label: _t("Original #2") });
        }
        if (this.state.version?.edited_attachment_id) {
            sources.push({ key: "edited", label: _t("Latest Edited") });
        }
        this.state.availableSources = sources;
    }

    _hydrateConfigFromVersion() {
        const v = this.state.version;
        if (!v) return;
        if (v.editing_json) {
            try {
                const parsed = JSON.parse(v.editing_json);
                // Merge so we keep our default slot scaffolding even if
                // the persisted payload is missing one of the slots.
                if (parsed.slot_1 || parsed.slot_2) {
                    this.state.config.slot_1 = {
                        ...this.state.config.slot_1,
                        ...(parsed.slot_1 || {}),
                    };
                    this.state.config.slot_2 = {
                        ...this.state.config.slot_2,
                        ...(parsed.slot_2 || {}),
                    };
                    for (const k of Object.keys(parsed)) {
                        if (k !== "slot_1" && k !== "slot_2") {
                            this.state.config[k] = parsed[k];
                        }
                    }
                } else {
                    // Legacy flat payload — treat as slot 1.
                    if (parsed.trim) this.state.config.slot_1.trim = parsed.trim;
                    if ("crop" in parsed) this.state.config.slot_1.crop = parsed.crop;
                    for (const k of Object.keys(parsed)) {
                        if (k !== "trim" && k !== "crop") {
                            this.state.config[k] = parsed[k];
                        }
                    }
                }
            } catch (e) {
                /* ignore malformed JSON */
            }
        }
        // Mirror per-slot trim columns into the in-memory config in case
        // editing_json was empty but the columns exist.
        if (v.trim_1_start || v.trim_1_end) {
            this.state.config.slot_1.trim = {
                start: v.trim_1_start || 0,
                end: v.trim_1_end || 0,
            };
        }
        if (v.trim_2_start || v.trim_2_end) {
            this.state.config.slot_2.trim = {
                start: v.trim_2_start || 0,
                end: v.trim_2_end || 0,
            };
        }
        this._computeAvailableSources();
    }

    /**
     * Which slot the user is currently editing.  ``original_1`` ->
     * ``slot_1``, ``original_2`` -> ``slot_2``.  When the user is
     * viewing the rendered "edited" preview we keep editing slot_1's
     * config since that's the primary slot.
     */
    get activeSlotKey() {
        return this.state.sourceKind === "original_2" ? "slot_2" : "slot_1";
    }
    get activeSlotConfig() {
        return this.state.config[this.activeSlotKey];
    }

    _attachVideoListeners() {
        const vid = this.videoRef.el;
        if (!vid) return;
        vid.addEventListener("loadedmetadata", () => {
            this.state.duration = vid.duration || 0;
            this.state.videoError = null;
            const slot = this.activeSlotConfig;
            if (!slot.trim.end) {
                slot.trim.end = vid.duration;
            }
            // Snap the playhead inside the trim window.
            if (vid.currentTime < slot.trim.start) {
                vid.currentTime = slot.trim.start;
            }
            this._bumpDisplayTick();
        });
        vid.addEventListener("timeupdate", () => {
            this.state.currentTime = vid.currentTime;
            const end = this.activeSlotConfig.trim.end;
            if (end && vid.currentTime >= end) {
                vid.pause();
                vid.currentTime = end;
            }
        });
        vid.addEventListener("play", () => (this.state.playing = true));
        vid.addEventListener("pause", () => (this.state.playing = false));
        vid.addEventListener("error", () => {
            this.state.videoError = _t("Cannot load this video. Did the download finish?");
        });
    }

    _observeStageResize() {
        if (typeof ResizeObserver === "undefined") return;
        const target = this.stageRef.el;
        if (!target) return;
        this._resizeObserver = new ResizeObserver(() => this._bumpDisplayTick());
        this._resizeObserver.observe(target);
        if (this.videoRef.el) {
            this._resizeObserver.observe(this.videoRef.el);
        }
    }

    _bumpDisplayTick() {
        // Cheap reactive bump: lets ``cropOverlayStyle`` recompute.
        this.state.videoDisplayTick = (this.state.videoDisplayTick + 1) % 1_000_000;
    }

    // ------------------------------------------------------------------
    // Source URL resolution
    // ------------------------------------------------------------------
    get sourceUrl() {
        const k = this.state.sourceKind;
        const t = this.state.taskId;
        if (k === "original_1") return this.videoQC.taskOriginalUrl(t, 1);
        if (k === "original_2") return this.videoQC.taskOriginalUrl(t, 2);
        if (k === "edited" && this.state.versionId) {
            return this.videoQC.editedUrl(this.state.versionId);
        }
        return this.state.versionId ? this.videoQC.sourceUrl(this.state.versionId) : "";
    }

    get editedUrl() {
        return this.state.versionId ? this.videoQC.editedUrl(this.state.versionId) : "";
    }

    onSourceKindChange(kind) {
        this.state.sourceKind = kind;
        // Each source slot has its OWN trim window + crop in
        // state.config.slot_<n>, so we DON'T reset here — switching
        // sources just swaps which slot's config the trim handles +
        // crop overlay are bound to.  ``loadedmetadata`` will fill in
        // an end-of-clip default if this slot's trim.end is still 0.
        this.state.currentTime = 0;
        this._bumpDisplayTick();
    }

    get videoFilterStyle() {
        const { brightness = 0, contrast = 1, saturation = 1, rotate = 0 } = this.state.config;
        const cssBrightness = 1 + (brightness || 0);
        return `filter: brightness(${cssBrightness}) contrast(${contrast}) saturate(${saturation}); transform: rotate(${rotate || 0}deg);`;
    }

    // ------------------------------------------------------------------
    // Playback
    // ------------------------------------------------------------------
    togglePlay() {
        const v = this.videoRef.el;
        if (!v) return;
        if (v.paused) {
            const { start, end } = this.activeSlotConfig.trim;
            if (v.currentTime < start || (end && v.currentTime >= end)) {
                v.currentTime = start || 0;
            }
            v.play();
        } else {
            v.pause();
        }
    }

    onStageClick() {
        // Click anywhere on the stage (outside the crop overlay) = toggle play.
        if (this.state.mode === "crop") return;
        this.togglePlay();
    }

    seek(t) {
        if (!this.videoRef.el) return;
        const clamped = Math.max(0, Math.min(t, this.state.duration || t));
        this.videoRef.el.currentTime = clamped;
        this.state.currentTime = clamped;
    }

    // -- scrub bar ------------------------------------------------------
    onScrubMouseDown(ev) {
        ev.preventDefault();
        this._scrubDrag = ev.currentTarget;
        this._scrubAt(ev.clientX);
        const move = (e) => this._scrubAt(e.clientX);
        const up = () => {
            this._scrubDrag = null;
            window.removeEventListener("mousemove", move);
            window.removeEventListener("mouseup", up);
        };
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
    }
    _scrubAt(clientX) {
        const el = this._scrubDrag;
        if (!el) return;
        const rect = el.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
        this.seek(ratio * (this.state.duration || 0));
    }

    // ------------------------------------------------------------------
    // Trim range slider (bottom strip)
    // ------------------------------------------------------------------
    get trimStartPct() {
        return this._pct(this.activeSlotConfig.trim.start);
    }
    get trimEndPct() {
        return this._pct(this.activeSlotConfig.trim.end || this.state.duration);
    }
    get scrubCursorPct() {
        return this._pct(this.state.currentTime);
    }
    _pct(t) {
        const d = this.state.duration || 1;
        return Math.min(100, Math.max(0, (t / d) * 100));
    }

    onTrimHandleMouseDown(handle, ev) {
        ev.preventDefault();
        ev.stopPropagation();
        const track = ev.currentTarget.parentElement;
        this._trimDrag = { handle, track };
        const move = (e) => this._trimMove(e);
        const up = () => {
            this._trimDrag = null;
            window.removeEventListener("mousemove", move);
            window.removeEventListener("mouseup", up);
        };
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
    }
    _trimMove(ev) {
        const d = this._trimDrag;
        if (!d) return;
        const rect = d.track.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
        const t = ratio * (this.state.duration || 0);
        const slot = this.activeSlotConfig;
        if (d.handle === "start") {
            slot.trim.start = Math.min(
                t,
                (slot.trim.end || this.state.duration) - 0.1,
            );
        } else {
            slot.trim.end = Math.max(t, slot.trim.start + 0.1);
        }
    }
    onTrimTrackClick(ev) {
        // Click on bare track = jump the playhead, don't change handles.
        if (this._trimDrag) return;
        const rect = ev.currentTarget.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
        this.seek(ratio * (this.state.duration || 0));
    }

    onTrimInputStart(ev) {
        const v = parseFloat(ev.target.value) || 0;
        const slot = this.activeSlotConfig;
        const max = (slot.trim.end || this.state.duration) - 0.1;
        slot.trim.start = Math.max(0, Math.min(v, max));
    }
    onTrimInputEnd(ev) {
        const v = parseFloat(ev.target.value) || 0;
        const slot = this.activeSlotConfig;
        const min = (slot.trim.start || 0) + 0.1;
        slot.trim.end = Math.max(min, Math.min(v, this.state.duration));
    }
    setStartToCurrent() {
        const slot = this.activeSlotConfig;
        slot.trim.start = Math.max(
            0,
            Math.min(this.state.currentTime, (slot.trim.end || this.state.duration) - 0.1),
        );
    }
    setEndToCurrent() {
        const slot = this.activeSlotConfig;
        slot.trim.end = Math.max(
            (slot.trim.start || 0) + 0.1,
            Math.min(this.state.currentTime, this.state.duration),
        );
    }
    resetTrim() {
        this.activeSlotConfig.trim = { start: 0, end: this.state.duration };
    }
    onTrimChange({ start, end }) {
        this.activeSlotConfig.trim = { start, end };
    }

    // ------------------------------------------------------------------
    // Crop handlers — independent rectangle per source slot.
    // ------------------------------------------------------------------
    onModeChange(mode) {
        this.state.mode = mode;
        const slot = this.activeSlotConfig;
        if (mode === "crop" && !slot.crop) {
            const v = this.videoRef.el;
            if (v && v.videoWidth) {
                const w = Math.round(v.videoWidth * 0.8);
                const h = Math.round(v.videoHeight * 0.8);
                slot.crop = {
                    x: Math.round((v.videoWidth - w) / 2),
                    y: Math.round((v.videoHeight - h) / 2),
                    w,
                    h,
                    aspect: "free",
                };
            }
        }
        this._bumpDisplayTick();
    }

    onAspectChange(aspect) {
        this.state.aspect = aspect;
        if (aspect === "free") return;
        const v = this.videoRef.el;
        if (!v || !v.videoWidth) return;
        const ratios = { "1:1": 1, "9:16": 9 / 16, "16:9": 16 / 9, "4:5": 4 / 5 };
        const r = ratios[aspect];
        let w = v.videoWidth;
        let h = v.videoHeight;
        const sourceRatio = w / h;
        if (sourceRatio > r) {
            w = Math.round(h * r);
        } else {
            h = Math.round(w / r);
        }
        const x = Math.round((v.videoWidth - w) / 2);
        const y = Math.round((v.videoHeight - h) / 2);
        this.activeSlotConfig.crop = { x, y, w, h, aspect };
    }

    onCropInput(field, ev) {
        const value = parseInt(ev.target.value, 10) || 0;
        const v = this.videoRef.el;
        const max = field === "w" || field === "x" ? v?.videoWidth || 9999 : v?.videoHeight || 9999;
        const slot = this.activeSlotConfig;
        slot.crop = {
            ...(slot.crop || { x: 0, y: 0, w: 0, h: 0 }),
            [field]: Math.max(0, Math.min(value, max)),
        };
    }
    resetCrop() {
        this.activeSlotConfig.crop = null;
    }

    // Project the crop rectangle (in video-pixel space) onto the rendered
    // pixel area of the <video> element.  The "video-projection" computation
    // takes object-fit:contain letterboxing into account.
    _videoProjection() {
        const v = this.videoRef.el;
        if (!v || !v.videoWidth || !v.videoHeight) return null;
        const stageRect = v.getBoundingClientRect();
        const vRatio = v.videoWidth / v.videoHeight;
        const sRatio = stageRect.width / stageRect.height;
        let drawW;
        let drawH;
        if (vRatio > sRatio) {
            drawW = stageRect.width;
            drawH = stageRect.width / vRatio;
        } else {
            drawH = stageRect.height;
            drawW = stageRect.height * vRatio;
        }
        return {
            left: (stageRect.width - drawW) / 2,
            top: (stageRect.height - drawH) / 2,
            width: drawW,
            height: drawH,
            sx: drawW / v.videoWidth,
            sy: drawH / v.videoHeight,
        };
    }

    get cropOverlayStyle() {
        const crop = this.activeSlotConfig.crop;
        const proj = this._videoProjection();
        if (!crop || !proj) return "display:none;";
        // touch the tick so this getter is reactive to layout changes
        // eslint-disable-next-line no-unused-vars
        const _ = this.state.videoDisplayTick;
        return (
            `left: ${proj.left + crop.x * proj.sx}px;` +
            `top: ${proj.top + crop.y * proj.sy}px;` +
            `width: ${crop.w * proj.sx}px;` +
            `height: ${crop.h * proj.sy}px;`
        );
    }
    get cropDimStyle() {
        // Cheap "everything outside the crop is dim" effect via a huge
        // box-shadow on the overlay rather than 4 separate <div>s.
        return this.state.mode === "crop" && this.activeSlotConfig.crop ? "" : "display:none;";
    }

    onCropDragStart(ev) {
        const slot = this.activeSlotConfig;
        if (this.state.mode !== "crop" || !slot.crop) return;
        ev.preventDefault();
        ev.stopPropagation();
        const proj = this._videoProjection();
        if (!proj) return;
        this._cropDrag = {
            startX: ev.clientX,
            startY: ev.clientY,
            cropX: slot.crop.x,
            cropY: slot.crop.y,
            sx: proj.sx,
            sy: proj.sy,
        };
        const move = (e) => this._onCropDragMove(e);
        const up = () => {
            this._cropDrag = null;
            window.removeEventListener("mousemove", move);
            window.removeEventListener("mouseup", up);
        };
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
    }
    _onCropDragMove(ev) {
        const d = this._cropDrag;
        if (!d) return;
        const v = this.videoRef.el;
        const dx = (ev.clientX - d.startX) / d.sx;
        const dy = (ev.clientY - d.startY) / d.sy;
        const slot = this.activeSlotConfig;
        const crop = slot.crop;
        crop.x = Math.max(0, Math.min(d.cropX + dx, v.videoWidth - crop.w));
        crop.y = Math.max(0, Math.min(d.cropY + dy, v.videoHeight - crop.h));
        slot.crop = { ...crop };
    }

    // Resize handles: nw, n, ne, e, se, s, sw, w
    onCropResizeStart(corner, ev) {
        const slot = this.activeSlotConfig;
        if (this.state.mode !== "crop" || !slot.crop) return;
        ev.preventDefault();
        ev.stopPropagation();
        const proj = this._videoProjection();
        if (!proj) return;
        const c = slot.crop;
        this._cropResize = {
            corner,
            startX: ev.clientX,
            startY: ev.clientY,
            cropX: c.x,
            cropY: c.y,
            cropW: c.w,
            cropH: c.h,
            sx: proj.sx,
            sy: proj.sy,
        };
        const move = (e) => this._onCropResizeMove(e);
        const up = () => {
            this._cropResize = null;
            window.removeEventListener("mousemove", move);
            window.removeEventListener("mouseup", up);
        };
        window.addEventListener("mousemove", move);
        window.addEventListener("mouseup", up);
    }
    _onCropResizeMove(ev) {
        const r = this._cropResize;
        if (!r) return;
        const v = this.videoRef.el;
        if (!v) return;
        const dx = (ev.clientX - r.startX) / r.sx;
        const dy = (ev.clientY - r.startY) / r.sy;
        let { cropX, cropY, cropW, cropH } = r;
        if (r.corner.includes("w")) {
            const newX = Math.max(0, Math.min(cropX + dx, cropX + cropW - 10));
            cropW = cropW - (newX - cropX);
            cropX = newX;
        }
        if (r.corner.includes("e")) {
            cropW = Math.max(10, Math.min(cropW + dx, v.videoWidth - cropX));
        }
        if (r.corner.includes("n")) {
            const newY = Math.max(0, Math.min(cropY + dy, cropY + cropH - 10));
            cropH = cropH - (newY - cropY);
            cropY = newY;
        }
        if (r.corner.includes("s")) {
            cropH = Math.max(10, Math.min(cropH + dy, v.videoHeight - cropY));
        }
        const slot = this.activeSlotConfig;
        slot.crop = {
            ...slot.crop,
            x: Math.round(cropX),
            y: Math.round(cropY),
            w: Math.round(cropW),
            h: Math.round(cropH),
        };
    }

    // ------------------------------------------------------------------
    // Filter / transform / audio handlers
    // ------------------------------------------------------------------
    onFilterChange(field, value) {
        this.state.config[field] = value;
    }
    onRotate(deg) {
        this.state.config.rotate = ((this.state.config.rotate || 0) + deg) % 360;
        this._bumpDisplayTick();
    }
    onToggleMute() {
        this.state.config.mute = !this.state.config.mute;
        if (this.videoRef.el) {
            this.videoRef.el.muted = this.state.config.mute;
        }
    }

    // ------------------------------------------------------------------
    // Save / render / QC
    // ------------------------------------------------------------------
    async saveDraft() {
        if (!this.state.versionId) {
            this.notification.add(_t("No version selected to save."), { type: "warning" });
            return;
        }
        this.state.saving = true;
        try {
            await this.videoQC.saveEdit(this.state.versionId, this.state.config);
            this.state.history = await this.videoQC.listEditHistory(this.state.versionId);
        } catch (err) {
            this.notification.add(err.message || _t("Failed to save."), { type: "danger" });
        } finally {
            this.state.saving = false;
        }
    }

    async saveAndRender() {
        if (!this.state.versionId) {
            this.notification.add(_t("No version selected to render."), { type: "warning" });
            return;
        }
        this.state.rendering = true;
        try {
            await this.videoQC.saveEdit(this.state.versionId, this.state.config, {
                render: true,
            });
            setTimeout(async () => {
                this.state.version = await this.videoQC.loadVersion(this.state.versionId);
                this.state.history = await this.videoQC.listEditHistory(this.state.versionId);
                this._computeAvailableSources();
            }, 4000);
        } catch (err) {
            this.notification.add(err.message || _t("Failed to render."), { type: "danger" });
        } finally {
            this.state.rendering = false;
        }
    }

    async sendToQC() {
        await this.saveDraft();
        await this.videoQC.sendToQC(this.state.taskId);
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "video.task",
            res_id: this.state.taskId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async newVersion() {
        const result = await this.videoQC.newVersion(this.state.taskId);
        this.state.versionId = result.version_id;
        this.state.version = await this.videoQC.loadVersion(result.version_id);
        this._hydrateConfigFromVersion();
        this.notification.add(
            _t("New version created (v%s).").replace("%s", result.version_no),
            { type: "success" },
        );
    }

    close() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "video.task",
            res_id: this.state.taskId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    // ------------------------------------------------------------------
    // Zoom
    // ------------------------------------------------------------------
    // The "design size" — the viewport the editor was laid out against.
    // Auto-fit scales relative to this so anything smaller gets a
    // proportional zoom-out and anything bigger stays at 1.0 (we cap so
    // it doesn't grow on huge monitors).
    static DESIGN_WIDTH = 1440;
    static DESIGN_HEIGHT = 900;
    static ZOOM_MIN = 0.5;
    static ZOOM_MAX = 2.0;
    static ZOOM_STEP = 0.1;
    static ZOOM_STORAGE_KEY = "instagram_video_qc.editor.zoom";

    _initZoom() {
        const saved = this._readSavedZoom();
        if (saved && saved.mode === "manual" && typeof saved.value === "number") {
            this.state.zoomIsAuto = false;
            this._applyZoom(saved.value);
        } else {
            this.state.zoomIsAuto = true;
            this._applyZoom(this._autoFitZoom());
        }
    }

    _autoFitZoom() {
        // Auto-fit "respects the user's screen aspect ratio": at 1.0 the
        // editor's position:fixed wrapper already fills the viewport
        // exactly (regardless of whether it's 16:9, 16:10, or ultrawide),
        // so we only zoom DOWN when the viewport is smaller than the
        // design baseline.  We never zoom up — that just wastes pixels
        // and forces unnecessary scrolling.
        const { DESIGN_WIDTH, DESIGN_HEIGHT, ZOOM_MIN } = this.constructor;
        const w = window.innerWidth || DESIGN_WIDTH;
        const h = window.innerHeight || DESIGN_HEIGHT;
        const s = Math.min(w / DESIGN_WIDTH, h / DESIGN_HEIGHT);
        return Math.max(ZOOM_MIN, Math.min(1.0, s));
    }

    _applyZoom(value) {
        const { ZOOM_MIN, ZOOM_MAX } = this.constructor;
        const clamped = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, value));
        // Round to 2 decimals so the "% display" doesn't jitter.
        const rounded = Math.round(clamped * 100) / 100;
        this.state.zoom = rounded;
        const el = this.editorRootRef.el;
        if (el) {
            el.style.setProperty("--vqc-zoom", rounded);
        }
        // Force the crop overlay to re-project on the new scale.
        this._bumpDisplayTick();
    }

    _readSavedZoom() {
        try {
            const raw = window.localStorage.getItem(this.constructor.ZOOM_STORAGE_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (e) {
            return null;
        }
    }
    _saveManualZoom() {
        try {
            window.localStorage.setItem(
                this.constructor.ZOOM_STORAGE_KEY,
                JSON.stringify({ mode: "manual", value: this.state.zoom }),
            );
        } catch (e) {
            /* ignore — privacy mode etc. */
        }
    }
    _clearSavedZoom() {
        try {
            window.localStorage.removeItem(this.constructor.ZOOM_STORAGE_KEY);
        } catch (e) {
            /* ignore */
        }
    }

    zoomIn() {
        this.state.zoomIsAuto = false;
        this._applyZoom(this.state.zoom + this.constructor.ZOOM_STEP);
        this._saveManualZoom();
    }
    zoomOut() {
        this.state.zoomIsAuto = false;
        this._applyZoom(this.state.zoom - this.constructor.ZOOM_STEP);
        this._saveManualZoom();
    }
    zoomReset() {
        // Click on the "%" label = jump back to 100%.
        this.state.zoomIsAuto = false;
        this._applyZoom(1);
        this._saveManualZoom();
    }
    zoomFit() {
        // Resume auto-fit and clear the persisted manual choice.
        this.state.zoomIsAuto = true;
        this._applyZoom(this._autoFitZoom());
        this._clearSavedZoom();
    }

    get zoomPct() {
        return Math.round(this.state.zoom * 100);
    }

    // ------------------------------------------------------------------
    // Stage aspect ratio (viewfinder)
    // ------------------------------------------------------------------
    static STAGE_ASPECTS = [
        { key: "auto", label: "Auto", ratio: null },
        { key: "9:16", label: "9:16  Reel", ratio: 9 / 16 },
        { key: "16:9", label: "16:9  Wide", ratio: 16 / 9 },
        { key: "1:1", label: "1:1  Square", ratio: 1 },
        { key: "4:5", label: "4:5  Portrait", ratio: 4 / 5 },
    ];

    get stageAspectOptions() {
        return this.constructor.STAGE_ASPECTS;
    }

    onStageAspectChange(key) {
        this.state.stageAspect = key;
        // The crop overlay's projection depends on the rendered video
        // box, which changes when the frame switches aspect ratios.
        this._bumpDisplayTick();
    }

    /**
     * Inline-style string applied to the .stage-frame wrapper around the
     * <video> element.  Returns an empty string when stageAspect is
     * "auto" so the video keeps its natural dimensions.
     */
    get stageFrameStyle() {
        const opt = this.constructor.STAGE_ASPECTS.find(
            (a) => a.key === this.state.stageAspect,
        );
        if (!opt || !opt.ratio) return "";
        return `aspect-ratio: ${opt.key.replace(":", " / ")};`;
    }

    // ------------------------------------------------------------------
    // UI helpers
    // ------------------------------------------------------------------
    fmt(sec) {
        const s = Math.max(0, sec || 0);
        const mm = Math.floor(s / 60).toString().padStart(2, "0");
        const ss = (s % 60).toFixed(1).padStart(4, "0");
        return `${mm}:${ss}`;
    }
    get trimLength() {
        const slot = this.activeSlotConfig;
        return (slot.trim.end || this.state.duration) - (slot.trim.start || 0);
    }
    /** Human label for the active slot — used by the header chip. */
    get activeSlotLabel() {
        return this.state.sourceKind === "original_2"
            ? _t("Editing Source #2")
            : _t("Editing Source #1");
    }
    get cropResizeCorners() {
        return ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
    }
}

registry
    .category("actions")
    .add("instagram_video_qc_manager.video_editor", VideoEditor);
