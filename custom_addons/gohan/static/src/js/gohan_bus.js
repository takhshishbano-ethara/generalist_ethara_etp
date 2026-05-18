/** @odoo-module **/
import { registry } from "@web/core/registry";

const gohanBusService = {
    dependencies: ["bus_service", "action"],
    start(env, { bus_service, action }) {
        let tickInterval = null;
        console.log("[gohan][diag] bus service starting");

        function reloadCurrentForm(payload) {
            console.log("[gohan][diag] bus notification received:", payload);
            const controller = action.currentController;
            if (!controller || !controller.props || !controller.props.resModel) {
                console.log("[gohan][diag] no current controller — skipping reload");
                return;
            }
            if (controller.props.resModel !== "gohan.job") {
                console.log(
                    "[gohan][diag] current view is %s, not gohan.job — skipping",
                    controller.props.resModel,
                );
                return;
            }
            if (payload.id && controller.props.resId && controller.props.resId !== payload.id) {
                console.log(
                    "[gohan][diag] payload.id=%s != current resId=%s — skipping",
                    payload.id, controller.props.resId,
                );
                return;
            }
            const model = controller.component && controller.component.model;
            if (model && model.root && typeof model.root.load === "function") {
                console.log("[gohan][diag] reloading form for job id=%s", payload.id);
                model.root.load();
            }
        }

        function startTicker() {
            if (tickInterval) return;
            tickInterval = setInterval(() => {
                const container = document.querySelector("[name='stage_progress_html']");
                if (!container) { stopTicker(); return; }
                const bolds = container.querySelectorAll("b");
                for (const b of bolds) {
                    const text = b.textContent.trim().replace(/^~/, "");
                    const match = text.match(/^(?:(\d+)m\s+)?(\d+)s$/);
                    if (!match) continue;
                    let mins = parseInt(match[1] || "0", 10);
                    let secs = parseInt(match[2], 10);
                    const prev = (b.previousSibling?.textContent || "").toLowerCase();
                    if (prev.includes("stage") || prev.includes("total")) {
                        // Tick up
                        secs += 1;
                        if (secs >= 60) { mins += 1; secs -= 60; }
                        b.textContent = mins ? `${mins}m ${String(secs).padStart(2, "0")}s` : `${secs}s`;
                    } else if (prev.includes("remaining")) {
                        // Tick down
                        if (secs > 0 || mins > 0) {
                            secs -= 1;
                            if (secs < 0) { mins = Math.max(0, mins - 1); secs = 59; }
                        }
                        const val = mins ? `${mins}m ${String(secs).padStart(2, "0")}s` : `${secs}s`;
                        b.textContent = `~${val}`;
                    }
                }
            }, 1000);
        }

        function stopTicker() {
            if (tickInterval) { clearInterval(tickInterval); tickInterval = null; }
        }

        const observer = new MutationObserver(() => {
            const el = document.querySelector("[name='stage_progress_html']");
            if (el && el.offsetParent !== null) { startTicker(); } else { stopTicker(); }
        });
        observer.observe(document.body, { childList: true, subtree: true });

        bus_service.subscribe("gohan/job_state", (p) => {
            console.log("[gohan][diag] event 'gohan/job_state':", p);
            reloadCurrentForm(p);
        });
        bus_service.subscribe("gohan/job_done", (p) => {
            console.log("[gohan][diag] event 'gohan/job_done':", p);
            reloadCurrentForm(p);
        });
        bus_service.addChannel("gohan_job_updates");
        console.log("[gohan][diag] subscribed to channel 'gohan_job_updates'");
    },
};

registry.category("services").add("gohan_bus", gohanBusService);
