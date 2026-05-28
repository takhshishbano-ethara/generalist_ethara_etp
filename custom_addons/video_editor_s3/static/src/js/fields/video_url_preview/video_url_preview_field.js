/** @odoo-module **/
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Dialog } from "@web/core/dialog/dialog";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

class VideoPreviewDialog extends Component {
    static template = "video_editor_s3.VideoPreviewDialog";
    static components = { Dialog };
    static props = {
        title: { type: String, optional: true },
        src: String,
        close: Function,
    };
}

export class VideoUrlPreviewField extends Component {
    static template = "video_editor_s3.VideoUrlPreviewField";
    static props = {
        ...standardFieldProps,
        streamKind: { type: String, optional: true },
        dialogTitle: { type: String, optional: true },
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.dialog = useService("dialog");
    }

    get rawValue() {
        return this.props.record?.data?.[this.props.name] || "";
    }

    get projectId() {
        const id = this.props.record?.resId;
        return Number.isInteger(id) && id > 0 ? id : null;
    }

    get previewSrc() {
        if (this.props.streamKind) {
            const pid = this.projectId;
            if (!pid) {
                return null;
            }
            return `/video_editor/stream/${pid}/${this.props.streamKind}`;
        }
        const url = (this.rawValue || "").trim();
        if (url.startsWith("http://") || url.startsWith("https://")) {
            return url;
        }
        return null;
    }

    get canPreview() {
        return Boolean(this.previewSrc);
    }

    onInputChange(ev) {
        this.props.record.update({ [this.props.name]: ev.target.value });
    }

    onPreview() {
        const src = this.previewSrc;
        if (!src) {
            return;
        }
        this.dialog.add(VideoPreviewDialog, {
            title: this.props.dialogTitle || "Video Preview",
            src,
        });
    }
}

export const videoUrlPreviewField = {
    component: VideoUrlPreviewField,
    displayName: "Video URL with Preview",
    supportedTypes: ["char"],
    extractProps: (fieldInfo) => {
        const attrs = fieldInfo.attrs || {};
        const options = fieldInfo.options || {};
        return {
            placeholder: attrs.placeholder || "",
            streamKind: options.stream_kind || "",
            dialogTitle: options.dialog_title || "",
        };
    },
};

registry.category("fields").add("video_url_preview", videoUrlPreviewField);
