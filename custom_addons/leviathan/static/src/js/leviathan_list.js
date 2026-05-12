/** @odoo-module **/
import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";

class LeviathanListController extends ListController {
    async onStartTask() {
        await this.actionService.doAction("leviathan.action_leviathan_start_task_wizard");
    }
}

registry.category("views").add("leviathan_list", {
    ...listView,
    Controller: LeviathanListController,
    buttonTemplate: "leviathan.ListButtons",
});
