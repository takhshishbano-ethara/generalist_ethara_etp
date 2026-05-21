/** @odoo-module */

import { registry } from "@web/core/registry";

const sandboxNotificationService = {
    dependencies: ["bus_service", "notification"],

    start(env, { bus_service, notification }) {
        bus_service.subscribe("skoll/sandbox_ready", (payload) => {
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

            env.bus.trigger("SKOLL:SANDBOX_STATUS_CHANGED", payload);
        });

        bus_service.subscribe("skoll/golden_ready", (payload) => {
            const failed = payload.status === "error";

            notification.add(
                failed
                    ? `Golden trajectory generation failed. ${payload.error || "Check logs."}`
                    : "Golden trajectory generated successfully!",
                { type: failed ? "danger" : "success", sticky: failed },
            );

            env.bus.trigger("SKOLL:GOLDEN_STATUS_CHANGED", payload);
        });

    },
};

registry
    .category("services")
    .add("skoll_sandbox_notification", sandboxNotificationService);
