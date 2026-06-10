/** @odoo-module **/

import { Component, useRef, useState, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const FOLDERS = [
    { value: "resources", label: "Resources" },
    { value: "root", label: "Root" },
    { value: "tests", label: "Tests" },
    { value: "environment", label: "Environment" },
    { value: "data", label: "Data" },
];

export class FenrirAttachmentUploader extends Component {
    static template = "fenrir.AttachmentUploader";
    static props = {
        record: { type: Object },
        readonly: { type: Boolean, optional: true },
        "*": { optional: true },
    };

    setup() {
        this.notification = useService("notification");
        this.fileInput = useRef("fileInput");
        const initialFolder = (this.props.defaultFolder
            && FOLDERS.some((f) => f.value === this.props.defaultFolder))
            ? this.props.defaultFolder
            : "resources";
        this.state = useState({
            folder: initialFolder,
            pendingFiles: [],   // [{id, name, size, file, folder}]
            uploading: false,
            progress: 0,
            currentName: "",
            currentIndex: 0,
            totalCount: 0,
            dragOver: false,
        });
        this._installSaveHook();
    }

    // Patch the ROOT record's save so a normal form submit also flushes
    // queued S3 uploads. Using root (not the bound record) handles the
    // case where this widget renders inside a subview.
    _installSaveHook() {
        const root = this.props.record.model.root;
        this._hook = () => this._uploadPending();

        if (!root.__fenrirAttachmentHooks) {
            root.__fenrirAttachmentHooks = new Set();
            const original = root.save.bind(root);
            root.save = async (...args) => {
                const ok = await original(...args);
                if (ok && root.__fenrirAttachmentHooks) {
                    for (const hook of root.__fenrirAttachmentHooks) {
                        // eslint-disable-next-line no-await-in-loop
                        await hook();
                    }
                }
                return ok;
            };
        }
        root.__fenrirAttachmentHooks.add(this._hook);

        onWillUnmount(() => {
            root.__fenrirAttachmentHooks &&
                root.__fenrirAttachmentHooks.delete(this._hook);
        });
    }

    get taskId() {
        return this.props.record.resId;
    }
    get pendingCount() {
        return this.state.pendingFiles.length;
    }
    get folders() { return FOLDERS; }

    onFolderChange(ev) { this.state.folder = ev.target.value; }
    onDragOver(ev) { ev.preventDefault(); this.state.dragOver = true; }
    onDragLeave() { this.state.dragOver = false; }
    async onDrop(ev) {
        ev.preventDefault();
        this.state.dragOver = false;
        const files = ev.dataTransfer && ev.dataTransfer.files;
        if (files && files.length) {
            this._enqueue(files);
            await this._autoSaveAndUpload();
        }
    }
    onPickClick() {
        if (this.state.uploading) return;
        this.fileInput.el && this.fileInput.el.click();
    }
    async onFileChange(ev) {
        const files = ev.target.files;
        if (files && files.length) {
            this._enqueue(files);
            await this._autoSaveAndUpload();
        }
        ev.target.value = "";
    }

    // Drop = save + upload. Root save satisfies task_id (the task is
    // the root). The patched save runs the upload via the hook.
    async _autoSaveAndUpload() {
        await this.props.record.model.root.save();
    }

    _enqueue(files) {
        for (const f of files) {
            this.state.pendingFiles.push({
                id: Math.random().toString(36).slice(2),
                name: f.name,
                size: f.size,
                file: f,
                folder: this.state.folder,
            });
        }
    }

    removePending(pendingId) {
        this.state.pendingFiles = this.state.pendingFiles.filter(
            (pf) => pf.id !== pendingId);
    }

    // Explicit "retry" — same as the drop handler.
    async onSaveAndUpload() {
        await this._autoSaveAndUpload();
    }

    async _uploadPending() {
        if (this.state.uploading) return;
        if (this.state.pendingFiles.length === 0) return;
        if (!this.taskId) {
            // save didn't yield an id — keep queue, user fixes validation
            return;
        }
        this.state.uploading = true;
        this.state.totalCount = this.state.pendingFiles.length;
        let okCount = 0;
        const total = this.state.totalCount;
        for (let i = 0; i < total; i += 1) {
            this.state.currentIndex = i + 1;
            const pf = this.state.pendingFiles[0];
            // eslint-disable-next-line no-await-in-loop
            const ok = await this._uploadOne(pf.file, pf.folder);
            this.state.pendingFiles.shift();
            if (ok) okCount += 1;
        }
        this.state.uploading = false;
        this.state.totalCount = 0;
        this.state.currentIndex = 0;
        if (okCount) {
            this.notification.add(
                _t("Uploaded %s file(s) to S3.").replace("%s", okCount),
                { type: "success" });
        }
        await this.props.record.load();
    }

    _uploadOne(file, folder) {
        return new Promise((resolve) => {
            const url = `/fenrir/task/${this.taskId}/attachment/upload`
                + `?folder=${encodeURIComponent(folder || "resources")}`;
            const form = new FormData();
            form.append("file", file, file.name);
            const xhr = new XMLHttpRequest();
            xhr.open("POST", url, true);
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) {
                    this.state.progress = Math.round((e.loaded / e.total) * 100);
                }
            };
            xhr.onloadstart = () => {
                this.state.progress = 0;
                this.state.currentName = file.name;
            };
            xhr.onloadend = () => {
                this.state.progress = 0;
                this.state.currentName = "";
                let data = null;
                try { data = JSON.parse(xhr.responseText); } catch (_e) { /* ignore */ }
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(true);
                } else {
                    const msg = (data && data.error) || xhr.statusText
                        || _t("Upload failed.");
                    this.notification.add(
                        `${file.name}: ${msg}`,
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

export const fenrirAttachmentUploader = {
    component: FenrirAttachmentUploader,
    extractProps: ({ attrs }) => ({
        defaultFolder: attrs && attrs.default_folder,
    }),
};

registry.category("view_widgets").add(
    "fenrir_attachment_uploader", fenrirAttachmentUploader);
