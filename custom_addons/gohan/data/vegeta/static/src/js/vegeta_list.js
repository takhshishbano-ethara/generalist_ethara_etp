/** @odoo-module **/
import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";

class VegetaListController extends ListController {
    async onStartTask() {
        await this.actionService.doAction("vegeta.action_vegeta_start_task_wizard");
    }
}

registry.category("views").add("vegeta_list", {
    ...listView,
    Controller: VegetaListController,
    buttonTemplate: "vegeta.ListButtons",
});
