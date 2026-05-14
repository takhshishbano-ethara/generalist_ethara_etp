/** @odoo-module **/
/**
 * VideoPreview field widget.
 *
 * Renders an inline HTML5 ``<video controls>`` player for any Char field
 * whose value is a streaming URL (e.g. the ``/video_qc/task/<id>/original/<n>``
 * endpoints exposed by this module's controller). When the URL carries the
 * standard media-fragment hash (``#t=start,end``) the widget enforces it
 * client-side too: it seeks to ``start`` on metadata-loaded and auto-pauses
 * at ``end`` so the user only sees the trimmed segment regardless of how
 * the browser interprets the fragment natively.
 *
 * Usage in a view::
 *
 *     <field name="my_play_url" widget="video_preview"/>
 */
import { Component, useRef, useEffect } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class VideoPreviewField extends Component {
    static template = "instagram_video_qc_manager.VideoPreviewField";
    static props = { ...standardFieldProps };

    setup() {
        this.videoRef = useRef("video");

        useEffect(
            (url) => {
                const v = this.videoRef.el;
                if (!v || !url) return;
                v.load();
                const frag = this._parseFragment(url);
                if (!frag) return;
                const onMeta = () => {
                    if (frag.start != null) {
                        v.currentTime = frag.start;
                    }
                };
                const onTimeUpdate = () => {
                    if (frag.end != null && v.currentTime >= frag.end) {
                        v.pause();
                        v.currentTime = frag.end;
                    }
                };
                v.addEventListener("loadedmetadata", onMeta);
                v.addEventListener("timeupdate", onTimeUpdate);
                return () => {
                    v.removeEventListener("loadedmetadata", onMeta);
                    v.removeEventListener("timeupdate", onTimeUpdate);
                };
            },
            () => [this.value],
        );
    }

    get value() {
        return this.props.record?.data?.[this.props.name] || "";
    }

    /**
     * Parse a media-fragment ``#t=start[,end]`` (or `?t=start,end`) URL hash.
     * Returns ``{start, end}`` in seconds, or ``null`` if no fragment.
     */
    _parseFragment(url) {
        if (!url) return null;
        const idx = url.indexOf("#t=");
        const q = url.indexOf("?t=");
        const raw = idx >= 0 ? url.slice(idx + 3) : q >= 0 ? url.slice(q + 3) : null;
        if (!raw) return null;
        const parts = raw.split(/[,&]/)[0].split(",");
        const start = parseFloat(parts[0]);
        const end = parts[1] !== undefined ? parseFloat(parts[1]) : null;
        if (Number.isNaN(start)) return null;
        return {
            start: Number.isNaN(start) ? null : start,
            end: end !== null && !Number.isNaN(end) ? end : null,
        };
    }
}

export const videoPreviewField = {
    component: VideoPreviewField,
    displayName: "Video Preview",
    supportedTypes: ["char"],
};

registry.category("fields").add("video_preview", videoPreviewField);
