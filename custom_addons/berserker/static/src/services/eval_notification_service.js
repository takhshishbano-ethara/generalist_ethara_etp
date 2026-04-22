/** @odoo-module */

import { registry } from "@web/core/registry";

const evalNotificationService = {
    dependencies: ["bus_service", "notification", "action"],

    start(env, { bus_service, notification, action }) {
        bus_service.subscribe("berserker/eval_done", (payload) => {
            const taskLabel = payload.task_id || payload.record_id || "";
            const failed = payload.eval_status === "failed";

            notification.add(
                failed
                    ? `Evaluation failed for task ${taskLabel}. You may retry.`
                    : `Evaluation completed for task ${taskLabel}. Reloading...`,
                { type: failed ? "danger" : "success", sticky: failed },
            );

            env.bus.trigger("BERSERKER:EVAL_DONE", payload);

            // Auto-reload the current view after a brief delay so the
            // tasker sees fresh data (eval_status, scores, etc.) without
            // a manual page refresh.
            setTimeout(() => {
                const controller = action.currentController;
                if (controller && controller.action &&
                    controller.action.res_model === "berserker") {
                    action.doAction("reload");
                }
            }, 1500);
        });
    },
};

registry
    .category("services")
    .add("berserker.eval_notification", evalNotificationService);
