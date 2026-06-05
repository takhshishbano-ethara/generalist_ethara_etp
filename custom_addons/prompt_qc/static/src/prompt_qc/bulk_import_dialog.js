/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const RUBRIC_EXTENSIONS = [".json"];

// Hard cap on rows per bulk submit. The browser holds every uploaded rubric (base64) in memory
// AND re-copies it on JSON.stringify at submit, so an unbounded list OOMs the tab before the
// server is ever reached. 200 keeps peak memory sane; the server enforces the same cap.
const MAX_ROWS = 200;

let _rowSeq = 0;
function emptyRow() {
    return {
        key: ++_rowSeq,
        user_prompt: "",
        rubric_b64: "",
        rubric_filename: "",
        error: "",
    };
}

function hasExtension(name, exts) {
    const lower = (name || "").toLowerCase();
    return exts.some((ext) => lower.endsWith(ext));
}

/**
 * Bulk Import modal. Each row becomes one independent QC run (queued -> running -> done/failed).
 * A row carries the user prompt + an optional .json rubric; the judge's system prompt is global
 * (configured in Settings), not per-row. Validation runs client-side for fast feedback; the
 * server re-validates authoritatively and any per-row errors it returns are mapped back onto the
 * rows so the user can fix just those without losing the valid ones.
 */
export class BulkImportDialog extends Component {
    static template = "prompt_qc.BulkImportDialog";
    static components = { Dialog };
    static props = {
        onStarted: { type: Function, optional: true },
        close: { type: Function, optional: true },
    };

    setup() {
        this.notification = useService("notification");
        this.state = useState({
            rows: [emptyRow()],
            submitting: false,
            globalError: "",
            maxFileMb: 1,
        });
        this._loadConfig();
    }

    async _loadConfig() {
        try {
            const cfg = await rpc("/prompt_qc/bulk/config", {});
            if (cfg && cfg.max_file_size_mb) {
                this.state.maxFileMb = cfg.max_file_size_mb;
            }
        } catch (e) {
            // Keep the safe default (1 MB); the server enforces the real limit anyway.
            console.warn("[prompt_qc] could not load bulk config", e);
        }
    }

    get showRemove() {
        return this.state.rows.length > 1;
    }

    addRow() {
        if (this.state.rows.length >= MAX_ROWS) {
            this.state.globalError = _t(
                "You can import at most %s rows at a time. Submit these first, then start another batch."
            ).replace("%s", MAX_ROWS);
            return;
        }
        this.state.rows.push(emptyRow());
    }

    removeRow(key) {
        if (!this.showRemove) {
            return;
        }
        const idx = this.state.rows.findIndex((r) => r.key === key);
        if (idx >= 0) {
            this.state.rows.splice(idx, 1);
        }
    }

    onPromptInput(row, ev) {
        row.user_prompt = ev.target.value;
        row.error = "";
        this.state.globalError = "";
    }

    async onRubricChange(row, ev) {
        row.error = "";
        this.state.globalError = "";
        const file = ev.target.files && ev.target.files[0];
        if (!file) {
            this._clearRubric(row);
            return;
        }

        const maxBytes = this.state.maxFileMb * 1024 * 1024;
        if (file.size > maxBytes) {
            row.error = _t("File exceeds the %s MB limit.").replace("%s", this.state.maxFileMb);
            this._clearRubric(row);
            ev.target.value = "";
            return;
        }

        if (!hasExtension(file.name, RUBRIC_EXTENSIONS)) {
            row.error = _t("Rubric must be a .json file.");
            this._clearRubric(row);
            ev.target.value = "";
            return;
        }

        try {
            row.rubric_b64 = await this._readBase64(file);
            row.rubric_filename = file.name;
        } catch (e) {
            row.error = _t("Could not read the file.");
            this._clearRubric(row);
            ev.target.value = "";
        }
    }

    _clearRubric(row) {
        row.rubric_b64 = "";
        row.rubric_filename = "";
    }

    _readBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve((reader.result || "").split(",")[1] || "");
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(file);
        });
    }

    _isEmptyRow(row) {
        return !row.user_prompt.trim() && !row.rubric_b64;
    }

    /** Returns the rows that will actually be submitted (non-empty), or null if validation fails. */
    _collectRows() {
        let ok = true;
        const submitted = [];
        for (const row of this.state.rows) {
            row.error = "";
            if (this._isEmptyRow(row)) {
                continue; // empty rows are silently skipped
            }
            if (!row.user_prompt.trim()) {
                row.error = _t("User prompt is required.");
                ok = false;
            }
            submitted.push(row);
        }
        if (submitted.length === 0) {
            this.state.globalError = _t("Add at least one row with a user prompt.");
            return null;
        }
        return ok ? submitted : null;
    }

    async onStart() {
        if (this.state.submitting) {
            return;
        }
        this.state.globalError = "";
        const submitted = this._collectRows();
        if (!submitted) {
            return;
        }

        this.state.submitting = true;
        const payload = {
            rows: submitted.map((r) => ({
                user_prompt: r.user_prompt,
                rubric_b64: r.rubric_b64,
                rubric_filename: r.rubric_filename,
            })),
        };

        try {
            const res = await rpc("/prompt_qc/bulk/start", payload);
            if (res.error) {
                this.state.globalError = res.error;
                return;
            }
            if (res.errors && res.errors.length) {
                this._applyServerErrors(res.errors, submitted);
                this.notification.add(
                    _t("Some rows need fixing before they can start."),
                    { type: "warning" }
                );
                return;
            }
            this.notification.add(
                _t("%s QC run(s) queued.").replace("%s", res.created),
                { type: "success" }
            );
            if (this.props.onStarted) {
                await this.props.onStarted(res.created);
            }
            this.props.close();
        } catch (e) {
            this.state.globalError =
                (e && e.data && e.data.message) || (e && e.message) || _t("Bulk start failed.");
        } finally {
            this.state.submitting = false;
        }
    }

    // Server row indices are 1-based against the submitted (non-empty) rows, in order.
    _applyServerErrors(errors, submitted) {
        for (const err of errors) {
            const idx = (err.row || 0) - 1;
            if (idx >= 0 && idx < submitted.length) {
                submitted[idx].error = err.message;
            } else {
                this.state.globalError = err.message;
            }
        }
    }

    onCancel() {
        this.props.close();
    }
}
