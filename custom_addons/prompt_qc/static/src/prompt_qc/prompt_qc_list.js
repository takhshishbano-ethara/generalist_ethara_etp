/** @odoo-module **/

import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";
import { useService } from "@web/core/utils/hooks";
import { onMounted, onWillUnmount } from "@odoo/owl";
import { BulkImportDialog } from "./bulk_import_dialog";

// Non-terminal states. While any visible run is in one of these, the list polls so its badge
// transitions queued -> running -> done/failed live, the same "watch it progress" feel as the
// single-run form in Stage 1.
const TRANSIENT_STATES = new Set(["draft", "queued", "running", "streaming"]);
const POLL_INTERVAL_MS = 3000;

export class PromptQcListController extends ListController {
    setup() {
        super.setup();
        this.dialogService = useService("dialog");
        this._pollHandle = null;
        // Start the poller unconditionally: its first tick reloads and self-stops if nothing is
        // transient, so a list opened with runs already in flight (e.g. a bulk started elsewhere)
        // also refreshes live, not just one started in this session.
        onMounted(() => this._startPoll());
        onWillUnmount(() => this._stopPoll());
    }

    /** Opens the Bulk Import modal. On a successful start we reload and begin polling. */
    onBulkImport() {
        this.dialogService.add(BulkImportDialog, {
            onStarted: async () => {
                await this._reload();
                this._startPoll();
            },
        });
    }

    async _reload() {
        try {
            await this.model.root.load();
        } catch (e) {
            // A transient reload failure is non-fatal; the next poll tick retries.
            console.warn("[prompt_qc] list reload failed", e);
        }
    }

    _hasTransientRecords() {
        const records = (this.model.root && this.model.root.records) || [];
        return records.some((r) => TRANSIENT_STATES.has(r.data.state));
    }

    _startPoll() {
        if (this._pollHandle) {
            return;
        }
        this._pollHandle = setInterval(async () => {
            await this._reload();
            if (!this._hasTransientRecords()) {
                this._stopPoll();
            }
        }, POLL_INTERVAL_MS);
    }

    _stopPoll() {
        if (this._pollHandle) {
            clearInterval(this._pollHandle);
            this._pollHandle = null;
        }
    }
}

registry.category("views").add("prompt_qc_run_list", {
    ...listView,
    Controller: PromptQcListController,
    buttonTemplate: "prompt_qc.ListButtons",
});
