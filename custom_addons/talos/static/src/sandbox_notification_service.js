/** @odoo-module */

import { registry } from "@web/core/registry";

const sandboxNotificationService = {
    dependencies: ["bus_service", "notification", "action"],

    start(env, { bus_service, notification, action }) {
        bus_service.subscribe("talos/sandbox_ready", (payload) => {
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

            env.bus.trigger("TALOS:SANDBOX_STATUS_CHANGED", payload);
        });

        bus_service.subscribe("talos/golden_ready", (payload) => {
            const failed = payload.status === "error";

            notification.add(
                failed
                    ? `Golden trajectory generation failed. ${payload.error || "Check logs."}`
                    : "Golden trajectory generated successfully!",
                { type: failed ? "danger" : "success", sticky: failed },
            );

            env.bus.trigger("TALOS:GOLDEN_STATUS_CHANGED", payload);

            setTimeout(() => {
                action.doAction("reload");
            }, 1500);
        });
    },
};

registry
    .category("services")
    .add("talos_sandbox_notification", sandboxNotificationService);
