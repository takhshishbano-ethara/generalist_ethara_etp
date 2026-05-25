/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const TEMPLATES = [
    {
        id: "centered",
        name: "Centered Card",
        description: "A classic card centered on a soft background.",
    },
    {
        id: "split",
        name: "Split Screen",
        description: "Brand image on one side, sign-in form on the other.",
    },
    {
        id: "fullscreen",
        name: "Full-Screen Background",
        description: "Sign-in card floating over a full-page image.",
    },
    {
        id: "minimal",
        name: "Minimal",
        description: "A clean, borderless form with no distractions.",
    },
];

const IMAGE_MIMETYPES = [
    ["/9j/", "image/jpeg"],
    ["R0lGOD", "image/gif"],
    ["UklGR", "image/webp"],
    ["PD94", "image/svg+xml"],
    ["PHN2Zy", "image/svg+xml"],
];

export class EtharaLoginTemplateField extends Component {
    static template = "ethara_etp_theme.LoginTemplateField";
    static props = { ...standardFieldProps };

    get templates() {
        return TEMPLATES;
    }

    get selected() {
        return this.props.record.data[this.props.name] || "centered";
    }

    selectTemplate(id) {
        this.props.record.update({ [this.props.name]: id });
    }
}

export const etharaLoginTemplateField = {
    component: EtharaLoginTemplateField,
    supportedTypes: ["selection"],
};

registry.category("fields").add("ethara_login_template_picker", etharaLoginTemplateField);

export class EtharaLoginPreviewField extends Component {
    static template = "ethara_etp_theme.LoginTemplatePreview";
    static props = { ...standardFieldProps };

    get selected() {
        return this.props.record.data[this.props.name] || "centered";
    }

    get imageSrc() {
        const data = this.props.record.data.ethara_login_image;
        if (!data) {
            return false;
        }
        const match = IMAGE_MIMETYPES.find(([prefix]) => data.startsWith(prefix));
        const mimetype = match ? match[1] : "image/png";
        return `data:${mimetype};base64,${data}`;
    }
}

export const etharaLoginPreviewField = {
    component: EtharaLoginPreviewField,
    supportedTypes: ["selection"],
};

registry.category("fields").add("ethara_login_template_preview", etharaLoginPreviewField);
