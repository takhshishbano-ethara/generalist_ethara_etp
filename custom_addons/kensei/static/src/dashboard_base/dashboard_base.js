/** @odoo-module */
import { Component } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { isoLocal, fmt } from "@kensei/tracker_common";

/**
 * Shared behaviour for the Kensei dashboards (org overview + tasker performance):
 * the date-range filter, the refresh/loading skeleton and value formatting.
 *
 * A subclass builds its own reactive ``state`` — which MUST include
 * ``dateFrom``, ``dateTo``, ``loading`` and ``lastUpdated`` — and implements
 * ``_load()`` (usually calling ``this._fetch(route, params)``).
 */
export class KenseiDashboardBase extends Component {
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.notification = useService("notification");
        this.action = useService("action");
        // Monotonic request counter — see _fetch.
        this._seq = 0;
    }

    /**
     * RPC helper that folds the current date range into the params and manages
     * ``loading`` + ``lastUpdated``. Returns the payload, or null on a hard
     * failure (a soft ``{error}`` payload is returned for the caller to handle).
     *
     * Returns null for a STALE response too. Every filter control (setPreset,
     * onDateInput, setGroupBy, setStage) calls _load() immediately, so two quick
     * clicks put two RPCs in flight; without sequencing, whichever resolves LAST
     * wins — and that is often the older, slower one, which then overwrites the
     * fresh data with results for a filter the user already moved off. The counter
     * makes late arrivals identifiable, so they can be dropped instead of applied.
     *
     * Callers already treat null as "nothing to do" (hard failure), which is
     * exactly the right handling for a stale response as well.
     */
    async _fetch(route, params = {}, errorMsg = "Failed to load data.") {
        const seq = ++this._seq;
        const isStale = () => seq !== this._seq;
        this.state.loading = true;
        try {
            const res = await rpc(route, {
                date_from: this.state.dateFrom || false,
                date_to: this.state.dateTo || false,
                ...params,
            });
            if (isStale()) {
                return null;
            }
            if (res && !res.error) {
                this.state.lastUpdated = new Date().toLocaleTimeString("en-US", {
                    hour: "2-digit",
                    minute: "2-digit",
                });
            }
            return res;
        } catch (e) {
            if (isStale()) {
                return null;
            }
            this.notification.add(errorMsg, { type: "danger" });
            console.error("[kensei-dashboard]", e);
            return null;
        } finally {
            // Only the newest request owns the spinner. A stale response resolving
            // late must not clear `loading` while the current one is still running.
            if (!isStale()) {
                this.state.loading = false;
            }
        }
    }

    // ---- date-range filter (operates on state.dateFrom / state.dateTo) ----
    /** days=null clears the range (All time); otherwise the last N days incl. today. */
    setPreset(days) {
        if (days === null) {
            this.state.dateFrom = "";
            this.state.dateTo = "";
        } else {
            const to = new Date();
            const from = new Date();
            from.setDate(to.getDate() - (days - 1));
            this.state.dateFrom = isoLocal(from);
            this.state.dateTo = isoLocal(to);
        }
        this._load();
    }

    onDateInput(which, ev) {
        this.state[which] = ev.target.value || "";
        this._load();
    }

    get isFiltered() {
        return Boolean(this.state.dateFrom || this.state.dateTo);
    }

    onRefresh() {
        this._load();
    }

    /** Metric formatter exposed to the templates. */
    fmt(value, suffix) {
        return fmt(value, suffix);
    }

    /** Subclasses implement the actual data load. */
    async _load() {}
}
