/** @odoo-module **/
import { registry } from "@web/core/registry";

// States that should trigger live polling. Terminal states (done / failed /
// discarded / reviewed / shipped) don't need polling — the bus event will
// fire the final reload and after that the record won't change.
const TRANSIENT_STATES = new Set([
    "draft", "extracting", "generating", "scoring",
]);
const POLL_INTERVAL_MS = 3000;

const gohanBusService = {
    dependencies: ["bus_service", "action", "orm"],
    start(env, { bus_service, action, orm }) {
        let tickInterval = null;
        console.log("[gohan][diag] bus service starting (with always-on poller)");

        function getCurrentJobController() {
            const controller = action.currentController;
            if (!controller || !controller.props || !controller.props.resModel) return null;
            if (controller.props.resModel !== "gohan.job") return null;
            return controller;
        }

        async function reloadCurrentForm(payload, source) {
            const controller = getCurrentJobController();
            if (!controller) return;
            const resId = controller.props.resId;
            if (payload && payload.id && resId && resId !== payload.id) return;
            if (!resId) return;
            console.log("[gohan][diag] reload (%s) id=%s", source, resId);
            // Layered reload: try the cheap path first (model.root.load), then
            // notify reactive listeners, then fall back to a full action
            // re-execution if the form still hasn't visibly updated. Odoo's
            // public API for "refresh current form" varies across point
            // releases — this covers the common shapes.
            const model = controller.component && controller.component.model;
            let loaded = false;
            if (model && model.root && typeof model.root.load === "function") {
                try {
                    await model.root.load();
                    if (typeof model.notify === "function") model.notify();
                    loaded = true;
                } catch (e) {
                    console.warn("[gohan][diag] model.root.load failed", e);
                }
            }
            // Hard fallback: re-execute the current form action. This always
            // re-renders. Slight cost: resets transient editor focus, but the
            // operator is watching a read-only status — they aren't typing.
            if (!loaded) {
                try {
                    await action.doAction({
                        type: "ir.actions.act_window",
                        res_model: "gohan.job",
                        res_id: resId,
                        views: [[false, "form"]],
                        target: "current",
                    }, { clearBreadcrumbs: false });
                } catch (e) {
                    console.warn("[gohan][diag] doAction reload failed", e);
                }
            }
        }

        // Always-on poller. Wakes every POLL_INTERVAL_MS, checks if the user
        // is on a gohan.job form, ORM-reads the current state, and reloads
        // when the DB-state has actually transitioned from the last poll.
        // Tracking DB-to-DB (not DB-to-form) avoids the infinite-reload loop
        // that happens when the form-side state field isn't readable in this
        // Odoo build.
        let lastSeenState = null;
        let lastSeenId = null;
        setInterval(async () => {
            const controller = getCurrentJobController();
            if (!controller || !controller.props.resId) {
                lastSeenState = null;
                lastSeenId = null;
                return;
            }
            const resId = controller.props.resId;
            // New record opened — establish baseline without reloading.
            if (lastSeenId !== resId) {
                lastSeenId = resId;
                try {
                    const rows = await orm.read("gohan.job", [resId], ["state"]);
                    lastSeenState = (rows && rows[0]) ? rows[0].state : null;
                } catch (e) { lastSeenState = null; }
                console.log("[gohan][diag] poll: tracking id=%s baseline state=%s",
                            resId, lastSeenState);
                return;
            }
            let dbState = null;
            try {
                const rows = await orm.read("gohan.job", [resId], ["state"]);
                if (rows && rows[0]) dbState = rows[0].state;
            } catch (e) {
                return;
            }
            if (dbState && dbState !== lastSeenState) {
                console.log("[gohan][diag] poll: state %s -> %s on id=%s",
                            lastSeenState, dbState, resId);
                lastSeenState = dbState;
                await reloadCurrentForm({id: resId}, "poll");
            }
        }, POLL_INTERVAL_MS);

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
            reloadCurrentForm(p, "bus");
        });
        bus_service.subscribe("gohan/job_done", (p) => {
            console.log("[gohan][diag] event 'gohan/job_done':", p);
            reloadCurrentForm(p, "bus");
        });
        bus_service.addChannel("gohan_job_updates");
        console.log("[gohan][diag] subscribed to channel 'gohan_job_updates'");
    },
};

registry.category("services").add("gohan_bus", gohanBusService);
