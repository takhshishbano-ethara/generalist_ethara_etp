/** @odoo-module **/
import { registry } from "@web/core/registry";

const leviathanBusService = {
    dependencies: ["bus_service", "action"],
    start(env, { bus_service, action }) {

        function reloadCurrentForm(payload) {
            const controller = action.currentController;
            if (!controller || !controller.props || !controller.props.resModel) {
                return;
            }
            if (controller.props.resModel !== "leviathan.job") {
                return;
            }
            // Only reload if we're viewing the specific job (or any job for done)
            if (payload.id && controller.props.resId && controller.props.resId !== payload.id) {
                return;
            }
            // Reload the form record in-place (no navigation)
            const model = controller.component && controller.component.model;
            if (model && model.root && typeof model.root.load === "function") {
                model.root.load();
            }
        }

        bus_service.subscribe("leviathan/job_state", reloadCurrentForm);
        bus_service.subscribe("leviathan/job_done", reloadCurrentForm);
        bus_service.addChannel("leviathan_job_updates");
    },
};

registry.category("services").add("leviathan_bus", leviathanBusService);
