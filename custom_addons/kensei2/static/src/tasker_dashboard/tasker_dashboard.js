/** @odoo-module */
import { useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { Kensei2DashboardBase } from "@kensei2/dashboard_base/dashboard_base";

/**
 * Per-tasker performance dashboard. Serves two entry points:
 *  - Dashboard menu (plain taskers)  -> no member_id  -> the current user.
 *  - Team Management button          -> params.member_id -> that member
 *    (QL/PL only, enforced server-side).
 */
export class Kensei2TaskerDashboard extends Kensei2DashboardBase {
    static template = "kensei2.TaskerDashboard";

    setup() {
        super.setup();
        this.memberId = this.props.action?.params?.member_id || false;
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
            "/kensei2/tracker/performance",
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
        return `kensei2_performance_${who || "me"}.csv`;
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
            res_model: "kensei2.tracker.allocation",
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

registry.category("actions").add("kensei2_tasker_dashboard", Kensei2TaskerDashboard);
