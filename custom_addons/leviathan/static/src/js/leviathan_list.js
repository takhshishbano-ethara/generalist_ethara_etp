/** @odoo-module **/
import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";

class LeviathanListController extends ListController {
    async onStartTask() {
        const action = await this.model.orm.call(
            "leviathan.job",
            "action_start_task",
            [[]]
        );
        if (action && action.type) {
            await this.actionService.doAction(action);
        }
    }
}

registry.category("views").add("leviathan_list", {
    ...listView,
    Controller: LeviathanListController,
    buttonTemplate: "leviathan.ListButtons",
});
