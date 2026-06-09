/** @odoo-module **/

import { Component, useRef, useState, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";

function humanSize(bytes) {
    if (!bytes && bytes !== 0) {
        return "—";
    }
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
        value /= 1024;
        i += 1;
    }
    return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fileIcon(name, mime) {
    const m = (mime || "").toLowerCase();
    const ext = (name || "").split(".").pop().toLowerCase();
    if (m.startsWith("image/")) return "fa fa-file-image-o";
    if (m.startsWith("video/")) return "fa fa-file-video-o";
    if (m.startsWith("audio/")) return "fa fa-file-audio-o";
    if (m === "application/pdf" || ext === "pdf") return "fa fa-file-pdf-o";
    if (["zip", "rar", "7z", "gz", "bz2", "tar", "xz"].includes(ext)) {
        return "fa fa-file-archive-o";
    }
    if (["doc", "docx", "odt", "rtf"].includes(ext)) return "fa fa-file-word-o";
    if (["xls", "xlsx", "ods", "csv"].includes(ext)) return "fa fa-file-excel-o";
    if (["ppt", "pptx", "odp"].includes(ext)) return "fa fa-file-powerpoint-o";
    if (["js", "ts", "tsx", "jsx", "py", "rb", "go", "rs", "java", "c", "cpp",
         "h", "sh", "html", "css", "scss", "json", "xml", "yml", "yaml", "md"
        ].includes(ext)) return "fa fa-file-code-o";
    if (["txt", "log"].includes(ext) || m.startsWith("text/")) {
        return "fa fa-file-text-o";
    }
    return "fa fa-file-o";
}

export class FenrirDeliverableFiles extends Component {
    static template = "fenrir.DeliverableFiles";
    static props = { ...standardFieldProps };

    setup() {
        this.notification = useService("notification");
        this.fileInput = useRef("fileInput");
        this.state = useState({
            pendingFiles: [],   // [{id, name, size, file}]
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
    // queued S3 uploads. Patching the root (not the inline child offer)
    // means the parent task can be saved in the same transaction —
    // necessary when the offer is an inline child whose required
    // task_id is only resolvable via the parent save.
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

    get pendingCount() {
        return this.state.pendingFiles.length;
    }

    get rows() {
        const field = this.props.record.data[this.props.name];
        const stored = ((field && field.records) || []).map((r) => ({
            id: `db_${r.resId}`,
            db_id: r.resId,
            file_name: r.data.file_name,
            file_size: r.data.file_size,
            file_size_h: humanSize(r.data.file_size),
            mime_type: r.data.mime_type,
            s3_key: r.data.s3_key,
            s3_uploaded_at: r.data.s3_uploaded_at
                ? String(r.data.s3_uploaded_at).replace(/T/, " ").slice(0, 16)
                : "",
            icon: fileIcon(r.data.file_name, r.data.mime_type),
            pending: false,
        }));
        const queued = this.state.pendingFiles.map((pf) => ({
            id: `pf_${pf.id}`,
            file_name: pf.name,
            file_size: pf.size,
            file_size_h: humanSize(pf.size),
            mime_type: (pf.file && pf.file.type) || "",
            s3_key: "",
            s3_uploaded_at: "",
            icon: fileIcon(pf.name, pf.file && pf.file.type),
            pending: true,
        }));
        return [...stored, ...queued];
    }

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

    // Drop = save + upload in one move. Root save commits the parent
    // (task) plus this inline offer, satisfying task_id. The patched
    // save then runs _uploadPending automatically via the hook.
    async _autoSaveAndUpload() {
        await this.props.record.model.root.save();
        // If save failed (validation), Odoo shows its own error and the
        // queued files remain in this.state.pendingFiles. The user
        // sees them in the list and can retry after fixing.
    }

    _enqueue(files) {
        for (const f of files) {
            this.state.pendingFiles.push({
                id: Math.random().toString(36).slice(2),
                name: f.name,
                size: f.size,
                file: f,
            });
        }
    }

    // Explicit "retry" button for cases where files are queued (e.g.
    // earlier save failed validation). Same code path as drop.
    async onSaveAndUpload() {
        await this._autoSaveAndUpload();
    }

    async _uploadPending() {
        if (this.state.uploading) return;
        if (this.state.pendingFiles.length === 0) return;
        if (!this.offerId) {
            // record save didn't yield an id (validation error) — keep files queued
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
            const ok = await this._uploadOne(pf.file);
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

    _uploadOne(file) {
        return new Promise((resolve) => {
            const url = `/fenrir/seller-offer/${this.offerId}/deliverable/upload`;
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

    onDownload(row) {
        if (row.pending) return;
        window.open(`/fenrir/deliverable/${row.db_id}/download`, "_blank");
    }

    async onDelete(row) {
        if (row.pending) {
            // Drop from local queue — never reached S3
            this.state.pendingFiles = this.state.pendingFiles.filter(
                (pf) => `pf_${pf.id}` !== row.id);
            return;
        }
        if (!window.confirm(_t("Delete %s? This also removes the S3 object.")
                .replace("%s", row.file_name))) {
            return;
        }
        const resp = await fetch(`/fenrir/deliverable/${row.db_id}/delete`, {
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
}

export const fenrirDeliverableFiles = {
    component: FenrirDeliverableFiles,
    displayName: _t("S3 Deliverable Files"),
    supportedTypes: ["one2many"],
    relatedFields: () => [
        { name: "file_name", type: "char" },
        { name: "file_size", type: "integer" },
        { name: "mime_type", type: "char" },
        { name: "s3_key", type: "char" },
        { name: "s3_uploaded_at", type: "datetime" },
    ],
};

registry.category("fields").add("fenrir_deliverable_files", fenrirDeliverableFiles);
