/** @odoo-module **/
import { Component, useRef, useEffect, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class CrowleyVideoPreviewField extends Component {
    static template = "crowley_ai_vid_gen.VideoPreviewField";
    static props = { ...standardFieldProps };

    setup() {
        this.videoRef = useRef("video");
        this.state = useState({ status: "idle", errorCode: null, errorMessage: null });

        useEffect(
            (url) => {
                const v = this.videoRef.el;
                if (!v) return;
                if (!url) {
                    this.state.status = "idle";
                    return;
                }
                this.state.status = "loading";
                this.state.errorCode = null;
                this.state.errorMessage = null;

                const onLoadedMetadata = () => { this.state.status = "ready"; };
                const onError = () => {
                    const err = v.error;
                    this.state.status = "error";
                    this.state.errorCode = err ? err.code : null;
                    this.state.errorMessage = err ? (err.message || "Unknown") : "Unknown";
                };

                v.addEventListener("loadedmetadata", onLoadedMetadata);
                v.addEventListener("error", onError);
                v.load();

                return () => {
                    v.removeEventListener("loadedmetadata", onLoadedMetadata);
                    v.removeEventListener("error", onError);
                };
            },
            () => [this.value],
        );
    }

    get value() {
        return this.props.record?.data?.[this.props.name] || "";
    }

    get displayError() {
        const codeMap = {
            1: "MEDIA_ERR_ABORTED",
            2: "MEDIA_ERR_NETWORK",
            3: "MEDIA_ERR_DECODE",
            4: "MEDIA_ERR_SRC_NOT_SUPPORTED",
        };
        const name = codeMap[this.state.errorCode] || "MEDIA_ERR_UNKNOWN";
        return `${name}: ${this.state.errorMessage || ""}`;
    }
}

export const crowleyVideoPreviewField = {
    component: CrowleyVideoPreviewField,
    displayName: "Crowley Video Preview",
    supportedTypes: ["char"],
};

registry.category("fields").add("crowley_video_preview", crowleyVideoPreviewField);
