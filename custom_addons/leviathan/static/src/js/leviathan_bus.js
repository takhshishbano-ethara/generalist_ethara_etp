import { registry } from "@web/core/registry";

const leviathanBusService = {
    dependencies: ["bus_service", "action"],
    start(env, { bus_service, action }) {
        bus_service.subscribe("leviathan/job_state", (payload) => {
            // Reload the current form view if it's showing this job
            const controller = action.currentController;
            if (!controller || !controller.props || !controller.props.resModel) {
                return;
            }
            if (
                controller.props.resModel === "leviathan.job" &&
                controller.props.resId === payload.id
            ) {
                action.restore();
            }
        });
        // Also subscribe to the done notification
        bus_service.subscribe("leviathan/job_done", (payload) => {
            const controller = action.currentController;
            if (!controller || !controller.props || !controller.props.resModel) {
                return;
            }
            if (controller.props.resModel === "leviathan.job") {
                action.restore();
            }
        });
        bus_service.addChannel("leviathan_job_updates");
    },
};

registry.category("services").add("leviathan_bus", leviathanBusService);
