/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

/**
 * Right side panel: prompt editor, QC status display, version history.
 */
export class EditorPromptPanel extends Component {
    static template = "instagram_video_qc_manager.EditorPromptPanel";
    static props = {
        version: { type: Object, optional: true },
        versionId: [Number, Boolean],
        taskId: Number,
        history: Array,
        onNewVersion: Function,
    };

    setup() {
        this.videoQC = useService("video_qc");
        this.notification = useService("notification");
        this.state = useState({
            promptText: this.props.version?.prompt_text || "",
            promptResponse: this.props.version?.prompt_response || "",
            saving: false,
        });
    }

    async savePrompt() {
        if (!this.props.versionId) {
            this.notification.add(_t("No version selected."), { type: "warning" });
            return;
        }
        this.state.saving = true;
        try {
            await this.videoQC.savePrompt(
                this.props.versionId,
                this.state.promptText,
                this.state.promptResponse,
            );
            this.notification.add(_t("Prompt saved."), { type: "success" });
        } finally {
            this.state.saving = false;
        }
    }

    get qcBadgeClass() {
        return this.props.version?.qc_status || "none";
    }
}
