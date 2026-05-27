/** @odoo-module **/
import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";

class GohanListController extends ListController {
    async onStartTask() {
        // Open the standard gohan.job creation form. The tasker fills the
        // Website Category field; on save, the create() override on
        // gohan.job auto-claims an unassigned URL from the admin's
        // uploaded pool for that category and fills it in.
        // (The top-menu "New Task" goes through the pop-up wizard instead
        // — same end-result, different entry UX.)
        await this.actionService.doAction({
            type: "ir.actions.act_window",
            name: "New Task",
            res_model: "gohan.job",
            view_mode: "form",
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("views").add("gohan_list", {
    ...listView,
    Controller: GohanListController,
    buttonTemplate: "gohan.ListButtons",
});
