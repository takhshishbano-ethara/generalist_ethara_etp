/** @odoo-module */
import { Component, useState } from "@odoo/owl";

export class SelectivePromptDialog extends Component {
    static template = "kensei2.SelectivePromptDialog";
    static props = {
        pods: Array,
        initialPrompt: { type: String, optional: true },
        attachmentCount: { type: Number, optional: true },
        onClose: Function,
        onSend: Function,
    };

    setup() {
        const initialSelection = {};
        for (const p of this.props.pods) {
            initialSelection[p.id] = true;
        }
        this.state = useState({
            prompt: this.props.initialPrompt || "",
            selection: initialSelection,
            sending: false,
            error: "",
        });
    }

    isSelected(podId) {
        return !!this.state.selection[podId];
    }

    onTogglePod(podId) {
        this.state.selection[podId] = !this.state.selection[podId];
    }

    onSelectAll() {
        for (const p of this.props.pods) {
            this.state.selection[p.id] = true;
        }
    }

    onSelectNone() {
        for (const p of this.props.pods) {
            this.state.selection[p.id] = false;
        }
    }

    onPromptInput(ev) {
        this.state.prompt = ev.target.value;
    }

    get selectedIds() {
        return this.props.pods
            .filter((p) => this.state.selection[p.id])
            .map((p) => p.id);
    }

    get selectedCount() {
        return this.selectedIds.length;
    }

    get canSend() {
        const hasPrompt = (this.state.prompt || "").trim().length > 0;
        const hasAttachments = (this.props.attachmentCount || 0) > 0;
        return (
            !this.state.sending &&
            this.selectedCount > 0 &&
            (hasPrompt || hasAttachments)
        );
    }

    async onSendClick() {
        if (!this.canSend) return;
        this.state.error = "";
        this.state.sending = true;
        try {
            await this.props.onSend({
                prompt: this.state.prompt.trim(),
                sandboxIds: this.selectedIds,
            });
        } catch (e) {
            this.state.error = e?.data?.message || e?.message || String(e);
        } finally {
            this.state.sending = false;
        }
    }

    onOverlayClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.sending) {
            this.props.onClose();
        }
    }
}
