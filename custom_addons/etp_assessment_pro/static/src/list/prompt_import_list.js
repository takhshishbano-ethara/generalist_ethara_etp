/** @odoo-module **/
/*
 * Adds a persistent "Import" button beside "New" in the Generators list
 * control panel. A declarative <header> button only shows when a row is
 * selected (it's a batch action), so to get an always-visible top-bar button
 * we supply a custom list view (js_class) with its own buttonTemplate, which
 * the list controller renders in the layout-buttons slot next to "New".
 */
import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";
import { useService } from "@web/core/utils/hooks";

export class PromptImportListController extends ListController {
    setup() {
        super.setup();
        this.actionService = useService("action");
    }

    onImportQuestionBank() {
        // Open the same wizard the menu item / list-selection button uses.
        this.actionService.doAction(
            "etp_assessment_pro.action_etp_assessment_pro_bank_import",
            {
                onClose: () => this.model.root.load(),
            }
        );
    }
}

registry.category("views").add("etp_prompt_import_list", {
    ...listView,
    Controller: PromptImportListController,
    buttonTemplate: "etp_assessment_pro.PromptListButtons",
});
