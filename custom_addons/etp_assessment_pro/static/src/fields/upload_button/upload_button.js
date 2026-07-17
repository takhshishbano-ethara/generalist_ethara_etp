import { registry } from "@web/core/registry";
import { BinaryField, binaryField } from "@web/views/fields/binary/binary_field";
import { _t } from "@web/core/l10n/translation";

/**
 * Compact variant of the stock binary field.
 *
 * Stock renders a full btn-primary "Upload your file" and, once a file is set, a
 * readonly input + pencil + trash + download cluster. On the generator form these
 * uploads are routine secondary actions sitting beside the real primary action
 * (Generate Questions), so both states are trimmed to a single quiet line.
 *
 * options:
 *   label      - button text; also the tooltip when icon_only (default "Upload")
 *   icon_only  - render a bare "+" with no text, for the reference-files picker
 *
 * extractProps always supplies label/iconOnly, so they are deliberately absent
 * from defaultProps: a module-level _t() can evaluate to a lazy translation
 * object rather than a String and trip OWL's prop validation.
 */
export class EtpUploadButton extends BinaryField {
    static template = "etp_assessment_pro.EtpUploadButton";
    static props = {
        ...BinaryField.props,
        iconOnly: { type: Boolean, optional: true },
        label: { type: String, optional: true },
    };
}

export const etpUploadButton = {
    ...binaryField,
    component: EtpUploadButton,
    displayName: _t("Compact file upload"),
    supportedOptions: [
        ...binaryField.supportedOptions,
        { label: _t("Button label"), name: "label", type: "string" },
        { label: _t("Icon only"), name: "icon_only", type: "boolean" },
    ],
    extractProps: (info) => ({
        ...binaryField.extractProps(info),
        iconOnly: info.options.icon_only || false,
        label: info.options.label || _t("Upload"),
    }),
};

registry.category("fields").add("etp_upload_button", etpUploadButton);
