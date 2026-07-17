/** @odoo-module */
import { useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { router } from "@web/core/browser/router";
import { Kensei2DashboardBase } from "@project_tracker/dashboard_base/dashboard_base";

// member_id must survive the URL. When a client action mounts, the action manager
// runs its OWN router.pushState(..., {replace:true}); replace mode keeps ONLY the
// "locked" keys (computeNextState -> pick(state, ..._lockedKeys)) plus the action
// stack, so an un-locked member_id we added gets wiped and a refresh recovers
// nothing. Locking it makes the manager's replace preserve it. Registered once.
router.addLockedKey("member_id");

/**
 * Per-tasker performance dashboard. Serves two entry points:
 *  - Dashboard menu (plain taskers)  -> no member_id  -> the current user.
 *  - Team Management button          -> params.member_id -> that member
 *    (QL/PL only, enforced server-side).
 */
export class Kensei2TaskerDashboard extends Kensei2DashboardBase {
    static template = "project_tracker.TaskerDashboard";

    setup() {
        super.setup();
        // The action params are the single source of truth: on a fresh drill-in
        // action_view_performance sets member_id; on a REFRESH Odoo rebuilds the
        // action from the URL and copies the (locked) member_id back into the params
        // (_getActionParams -> params: state). So no separate URL read is needed, and
        // reading the URL directly would surface a STALE locked value when you next
        // open your own dashboard. The server authorises every member_id, so a
        // hand-typed one is safe.
        this.memberId = Number(this.props.action?.params?.member_id) || false;
        // Reflect the current view in the URL so a refresh recovers it: keep the id
        // for a tasker (locked, so the action manager's replace preserves it), and
        // clear it for the viewer's own dashboard (undefined is dropped by
        // sanitizeSearch) so a stale id never leaks in.
        router.replaceState(
            { member_id: this.memberId || undefined }, { sync: true });
        // member_id is a LOCKED url key so it survives a refresh — but that also
        // means it would linger on every other page. Clear it when the dashboard is
        // left via in-app navigation. A browser REFRESH tears down the VM without
        // firing this, so the id still survives a reload (which is the whole point).
        onWillUnmount(() => {
            router.replaceState({ member_id: undefined }, { sync: true });
        });
        this.state = useState({
            loading: true,
            denied: false,
            subject: {},
            kpis: [],
            funnel: [],
            recent: [],
            dateFrom: "",
            dateTo: "",
            lastUpdated: "",
        });
        onWillStart(() => this._load());
    }

    async _load() {
        const res = await this._fetch(
            "/project_tracker/performance",
            { member_id: this.memberId || false },
            "Failed to load performance data.");
        if (!res) {
            return;
        }
        if (res.error) {
            this.state.denied = true;
            this.notification.add(
                res.error === "access_denied"
                    ? "You are not allowed to view this tasker."
                    : "No performance data available.",
                { type: "warning" });
            return;
        }
        this.state.denied = false;
        this.state.subject = res.subject || {};
        this.state.kpis = res.kpis || [];
        this.state.funnel = res.funnel || [];
        this.state.recent = res.recent || [];
    }

    get title() {
        return this.memberId ? this.state.subject.name || "Tasker" : "My Dashboard";
    }

    // ---- CSV export -----------------------------------------------------
    csvFileName() {
        const who = (this.state.subject.name || "me")
            .toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
        return `project_tracker_performance_${who || "me"}.csv`;
    }

    csvMeta() {
        return [
            ...super.csvMeta(),
            ["Tasker", this.state.subject.name || "Me"],
        ];
    }

    csvSections() {
        return [
            {
                title: "Performance",
                headers: ["Metric", "Value"],
                // suffix (%, days) is part of what the KPI MEANS, so fold it in —
                // a bare "87" loses whether it was 87% or 87 days.
                rows: this.state.kpis.map((k) => [
                    k.label,
                    k.value === null || k.value === undefined
                        ? "" : `${k.value}${k.suffix || ""}`,
                ]),
            },
            {
                title: "My Pipeline",
                headers: ["Step", "Count"],
                rows: this.state.funnel.map((c) => [c.label, c.value]),
            },
            {
                title: "Recent Tasks",
                headers: ["Task ID", "Stage", "Persona", "Status",
                    "Overall", "Assigned", "Completed"],
                rows: this.state.recent.map((r) => [
                    r.task_id,
                    `${r.stage} / ${r.total_stages}`,
                    r.persona,
                    r.status_label,
                    r.overall ?? "",
                    r.assigned || "",
                    r.completed || "",
                ]),
            },
        ];
    }

    openTask(row) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "project.tracker.allocation",
            res_id: row.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    /** Keyboard equivalent of clicking a recent-task row (the <tr> is t-on-click). */
    onRowKeydown(ev, row) {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();
            this.openTask(row);
        }
    }
}

registry.category("actions").add("project_tracker_tasker_dashboard", Kensei2TaskerDashboard);
