/** @odoo-module */
import { useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { Kensei2DashboardBase } from "@kensei2/dashboard_base/dashboard_base";
import { ProgressTable } from "@kensei2/progress_table/progress_table";

// The progress table's grouping axis: label + the allocation field to drill into.
const GROUPS = [
    { key: "pl", label: "PL", field: "pl_id" },
    { key: "ql", label: "QL", field: "assigned_ql_id" },
    { key: "tasker", label: "Tasker", field: "tasker_member_id" },
];

// The stage axis. `value: null` means "count every stage record", which is what
// credits a person for each stage they actually worked.
const STAGES = [
    { key: "all", label: "All Stages", value: null },
    { key: "s1", label: "Stage 1", value: 1 },
    { key: "s2", label: "Stage 2", value: 2 },
];

export class Kensei2TrackerDashboard extends Kensei2DashboardBase {
    static template = "kensei2.TrackerDashboard";
    static components = { ProgressTable };

    setup() {
        super.setup();
        this.groups = GROUPS;
        this.stages = STAGES;
        this.state = useState({
            loading: true,
            teamComposition: [],
            funnel: [],
            stats: [],
            rows: [],
            groupBy: "pl",
            stage: null,
            lastUpdated: "",
            dateFrom: "",
            dateTo: "",
        });
        onWillStart(() => this._load());
    }

    async _load() {
        const res = await this._fetch(
            "/kensei2/tracker/dashboard",
            { group_by: this.state.groupBy, stage: this.state.stage },
            "Failed to load dashboard data.");
        if (!res) {
            return;
        }
        if (res.error) {
            this.notification.add("You are not allowed to view this dashboard.",
                { type: "warning" });
            return;
        }
        this.state.teamComposition = res.team_composition || [];
        this.state.funnel = res.funnel || [];
        this.state.stats = res.stats || [];
        this.state.rows = res.rows || [];
    }

    get group() {
        return GROUPS.find((g) => g.key === this.state.groupBy) || GROUPS[0];
    }

    setGroupBy(key) {
        if (this.state.groupBy !== key) {
            this.state.groupBy = key;
            this._load();
        }
    }

    setStage(value) {
        if (this.state.stage !== value) {
            this.state.stage = value;
            this._load();
        }
    }

    // ---- drill-down: open the filtered Task Allocation / Team Management list ----
    _openAllocations(domain, name) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: "kensei2.tracker.allocation",
            domain,
            views: [
                [false, "list"],
                [false, "form"],
            ],
            target: "current",
        });
    }

    _openMembers(domain, name) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: "kensei2.tracker.team.member",
            domain,
            views: [
                [false, "list"],
                [false, "form"],
            ],
            target: "current",
        });
    }

    /**
     * Keyboard equivalent of clicking a drill-down card.
     *
     * The cards are <div t-on-click>, so without this the entire drill-down is
     * mouse-only: unreachable by keyboard and invisible to a screen reader. The
     * segmented controls right below them are real <button>s with aria-pressed —
     * this brings the cards up to the same bar.
     */
    onCardKeydown(ev, activate) {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();
            activate();
        }
    }

    onCompositionClick(card) {
        const domain = card.role ? [["role", "=", card.role]] : [];
        this._openMembers(domain, `Team — ${card.label}`);
    }

    onStatusCardClick(card) {
        if (!card.statuses || !card.statuses.length) {
            return; // e.g. "Avg Score" has nothing to open
        }
        this._openAllocations([["status", "in", card.statuses]], card.label);
    }

    // A row is one person on the currently selected axis. The drill-down repeats
    // the stage filter so the list shows exactly the records the row counted.
    onRowClick(row) {
        const { label, field } = this.group;
        const domain = [[field, "=", row.id || false]];
        let name = `${label} — ${row.name}`;
        if (this.state.stage) {
            domain.push(["stage_no", "=", this.state.stage]);
            name += ` (Stage ${this.state.stage})`;
        }
        this._openAllocations(domain, name);
    }
}

registry.category("actions").add("kensei2_tracker_dashboard", Kensei2TrackerDashboard);
