/** @odoo-module */

import { registry } from "@web/core/registry";

const auroraBusService = {
    dependencies: ["bus_service", "notification"],

    start(env, { bus_service, notification }) {
        bus_service.subscribe("aurora/status_update", (payload) => {
            const { model, id, stage, progress_text } = payload;

            env.bus.trigger("AURORA:STATUS_UPDATE", payload);

            const isTerminal = ["done", "failed"].includes(stage);
            if (isTerminal) {
                const failed = stage === "failed";
                const label = model === "aurora.pipeline" ? "Pipeline" : "Evaluation";
                notification.add(
                    failed
                        ? `${label} #${id} failed.`
                        : `${label} #${id} completed successfully!`,
                    { type: failed ? "danger" : "success", sticky: false },
                );
            }
        });
    },
};

registry.category("services").add("aurora_bus_service", auroraBusService);
