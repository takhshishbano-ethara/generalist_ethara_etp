/** @odoo-module **/

import { Component, useRef, useState, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";

/**
 * Compact per-row uploader for use inside seller-offer list views.
 * Renders: <count> ☁⬆  — click the icon to pick files; root form auto-saves
 * (so a brand-new offer commits with task_id propagated) and the queued
 * files stream to S3 via the deliverable controller.
 */
export class FenrirDeliverableInline extends Component {
    static template = "fenrir.DeliverableInline";
    static props = { ...standardFieldProps };

    setup() {
        this.notification = useService("notification");
        this.fileInput = useRef("fileInput");
        this.state = useState({ uploading: false });
        this._pending = [];   // File[] queued for upload
        this._installSaveHook();
    }

    // Re-uses the same hooks Set as the full widget so multiple inline
    // rows on the same root play nicely with each other.
    _installSaveHook() {
        const root = this.props.record.model.root;
        this._hook = () => this._uploadPending();

        if (!root.__fenrirDeliverableHooks) {
            root.__fenrirDeliverableHooks = new Set();
            const original = root.save.bind(root);
            root.save = async (...args) => {
                const ok = await original(...args);
                if (ok && root.__fenrirDeliverableHooks) {
                    for (const hook of root.__fenrirDeliverableHooks) {
                        // eslint-disable-next-line no-await-in-loop
                        await hook();
                    }
                }
                return ok;
            };
        }
        root.__fenrirDeliverableHooks.add(this._hook);

        onWillUnmount(() => {
            root.__fenrirDeliverableHooks &&
                root.__fenrirDeliverableHooks.delete(this._hook);
        });
    }

    get offerId() {
        return this.props.record.resId;
    }

    get count() {
        const field = this.props.record.data[this.props.name];
        return (field && field.records) ? field.records.length : 0;
    }

    get files() {
        const field = this.props.record.data[this.props.name];
        if (!field || !field.records) return [];
        return field.records.map((r) => ({
            id: r.resId,
            name: r.data.file_name || "",
            s3_key: r.data.s3_key || "",
        }));
    }

    onUploadClick(ev) {
        ev.stopPropagation();  // don't open the row's form dialog
        if (this.state.uploading) return;
        this.fileInput.el && this.fileInput.el.click();
    }

    onDownloadClick(ev, file) {
        ev.stopPropagation();
        if (!file.id) return;
        window.open(`/fenrir/deliverable/${file.id}/download`, "_blank");
    }

    async onDeleteClick(ev, file) {
        ev.stopPropagation();
        if (!file.id || this.state.uploading) return;
        if (!window.confirm(
                _t("Delete %s? This also removes the S3 object.")
                    .replace("%s", file.name))) {
            return;
        }
        const resp = await fetch(`/fenrir/deliverable/${file.id}/delete`, {
            method: "POST",
            headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            this.notification.add(data.error || _t("Delete failed."),
                { type: "danger" });
            return;
        }
        await this.props.record.load();
    }

    async onFileChange(ev) {
        const files = ev.target.files;
        if (files && files.length) {
            for (const f of files) this._pending.push(f);
            await this.props.record.model.root.save();
        }
        ev.target.value = "";
    }

    async _uploadPending() {
        if (this.state.uploading) return;
        if (this._pending.length === 0) return;
        if (!this.offerId) return;
        this.state.uploading = true;
        let okCount = 0;
        while (this._pending.length > 0) {
            const f = this._pending.shift();
            // eslint-disable-next-line no-await-in-loop
            const ok = await this._uploadOne(f);
            if (ok) okCount += 1;
        }
        this.state.uploading = false;
        if (okCount) {
            this.notification.add(
                _t("Uploaded %s file(s) to S3.").replace("%s", okCount),
                { type: "success" });
        }
        await this.props.record.load();
    }

    _uploadOne(file) {
        return new Promise((resolve) => {
            const url = `/fenrir/seller-offer/${this.offerId}/deliverable/upload`;
            const form = new FormData();
            form.append("file", file, file.name);
            const xhr = new XMLHttpRequest();
            xhr.open("POST", url, true);
            xhr.onloadend = () => {
                let data = null;
                try { data = JSON.parse(xhr.responseText); } catch (_e) { }
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(true);
                } else {
                    const msg = (data && data.error) || xhr.statusText
                        || _t("Upload failed.");
                    this.notification.add(`${file.name}: ${msg}`,
                        { type: "danger", sticky: true });
                    resolve(false);
                }
            };
            xhr.onerror = () => {
                this.notification.add(
                    `${file.name}: ${_t("Network error during upload.")}`,
                    { type: "danger", sticky: true });
                resolve(false);
            };
            xhr.send(form);
        });
    }
}

export const fenrirDeliverableInline = {
    component: FenrirDeliverableInline,
    displayName: _t("S3 Deliverables (inline)"),
    supportedTypes: ["one2many"],
    relatedFields: () => [
        { name: "file_name", type: "char" },
        { name: "s3_key", type: "char" },
    ],
};

registry.category("fields").add(
    "fenrir_deliverable_inline", fenrirDeliverableInline);
