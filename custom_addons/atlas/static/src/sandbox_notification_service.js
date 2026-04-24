/** @odoo-module */

import { registry } from "@web/core/registry";

const sandboxNotificationService = {
    dependencies: ["bus_service", "notification"],

    start(env, { bus_service, notification }) {
        bus_service.subscribe("atlas/sandbox_ready", (payload) => {
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

            env.bus.trigger("ATLAS:SANDBOX_STATUS_CHANGED", payload);
        });

        bus_service.subscribe("atlas/generation_done", (payload) => {
            const goalStatus = payload.goal_status || "done";
            const rubricStatus = payload.rubric_status || "done";
            const anyError = goalStatus === "error" || rubricStatus === "error";

            if (anyError) {
                notification.add(
                    "Generation completed with errors. Check goal/rubric status.",
                    { type: "warning" },
                );
            } else {
                notification.add("Goal & rubric generated successfully!", {
                    type: "success",
                });
            }

            env.bus.trigger("ATLAS:GENERATION_DONE", payload);
        });
    },
};

registry
    .category("services")
    .add("atlas_sandbox_notification", sandboxNotificationService);
