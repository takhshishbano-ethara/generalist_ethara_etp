/** @odoo-module */

import { registry } from "@web/core/registry";

const sandboxNotificationService = {
    dependencies: ["bus_service", "notification"],

    start(env, { bus_service, notification }) {
        bus_service.subscribe("kensei2/sandbox_ready", (payload) => {
            const status = payload.docker_status || payload.status;
            const sandboxId = payload.sandbox_id;
            const modelType = payload.model_type || "";
            const failed = status === "error";

            notification.add(
                failed
                    ? `Sandbox${modelType ? " (" + modelType + ")" : ""} failed to start. ${payload.error || "Check logs."}`
                    : `Sandbox${modelType ? " (" + modelType + ")" : ""} is ready!`,
                { type: failed ? "danger" : "success", sticky: failed },
            );

            env.bus.trigger("KENSEI2:SANDBOX_STATUS_CHANGED", payload);
        });

        bus_service.subscribe("kensei2/golden_ready", (payload) => {
            const failed = payload.status === "error";

            notification.add(
                failed
                    ? `Golden trajectory generation failed. ${payload.error || "Check logs."}`
                    : "Golden trajectory generated successfully!",
                { type: failed ? "danger" : "success", sticky: failed },
            );

            env.bus.trigger("KENSEI2:GOLDEN_STATUS_CHANGED", payload);
        });

        bus_service.subscribe("kensei2/auto_hint_result", (payload) => {
            env.bus.trigger("KENSEI2:AUTO_HINT_RESULT", payload);
        });
    },
};

registry
    .category("services")
    .add("kensei2_sandbox_notification", sandboxNotificationService);
