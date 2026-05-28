/** @odoo-module **/

import { registry } from "@web/core/registry";
import { formView } from "@web/views/form/form_view";
import { FormController } from "@web/views/form/form_controller";
import { onMounted, onWillUnmount } from "@odoo/owl";

const POLL_INTERVAL_MS = 2500;

class VideoEditorProjectFormController extends FormController {
    setup() {
        super.setup();
        this._pollTimer = null;
        onMounted(() => this._scheduleNext());
        onWillUnmount(() => {
            if (this._pollTimer) {
                clearTimeout(this._pollTimer);
                this._pollTimer = null;
            }
        });
    }

    _scheduleNext() {
        this._pollTimer = setTimeout(async () => {
            const root = this.model && this.model.root;
            if (root && root.data && root.data.active_job_id) {
                try {
                    await root.load();
                } catch (e) {
                    // swallow transient load errors so the poll loop survives
                }
            }
            this._scheduleNext();
        }, POLL_INTERVAL_MS);
    }
}

registry.category("views").add("video_editor_s3_project_form", {
    ...formView,
    Controller: VideoEditorProjectFormController,
});
