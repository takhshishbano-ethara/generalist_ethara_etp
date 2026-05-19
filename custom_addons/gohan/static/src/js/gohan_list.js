/** @odoo-module **/
import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";

class GohanListController extends ListController {
    async onStartTask() {
        await this.actionService.doAction("gohan.action_gohan_start_task_wizard");
    }
}

registry.category("views").add("gohan_list", {
    ...listView,
    Controller: GohanListController,
    buttonTemplate: "gohan.ListButtons",
});
