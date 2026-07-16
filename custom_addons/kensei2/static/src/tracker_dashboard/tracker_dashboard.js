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

export class Kensei2TrackerDashboard extends Kensei2DashboardBase {
    static template = "kensei2.TrackerDashboard";
    static components = { ProgressTable };

    setup() {
        super.setup();
        this.groups = GROUPS;
        this.state = useState({
            loading: true,
            teamComposition: [],
            funnel: [],
            stats: [],
            rows: [],
            groupBy: "pl",
            lastUpdated: "",
            dateFrom: "",
            dateTo: "",
        });
        onWillStart(() => this._load());
    }

    async _load() {
        const res = await this._fetch(
            "/kensei2/tracker/dashboard",
            { group_by: this.state.groupBy },
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

    // ---- CSV export -----------------------------------------------------
    // The progress table's columns, in the SAME order and wording as
    // progress_table.xml. Kept here (not derived from a row) so an all-zero export
    // still has every column, and so a stage-1 label can never be printed over a
    // stage-2 count — the whole point of the split.
    static PROGRESS_COLUMNS = [
        ["in_auth", "In Auth"],
        ["ready", "S1 Ready"],
        ["in_traj", "S1 Baseline"],
        ["s1_qc", "S1 Manual QC"],
        ["handed_off", "Next Stage"],
        ["pik_ready", "S2 P@K Ready"],
        ["pass_it_k", "S2 Pass @ K"],
        ["s2_qc", "S2 Manual QC"],
        ["verified", "Deliverable"],
        ["blocked", "Blocked"],
    ];

    csvFileName() {
        return `kensei2_dashboard_${this.state.groupBy}.csv`;
    }

    csvMeta() {
        return [
            ...super.csvMeta(),
            ["Group by", this.group.label],
        ];
    }

    csvSections() {
        const cols = this.constructor.PROGRESS_COLUMNS;
        return [
            {
                title: "Summary",
                headers: ["Metric", "Value"],
                rows: this.state.stats.map((s) => [s.label, s.value]),
            },
            {
                title: "Pipeline Funnel",
                headers: ["Step", "Count"],
                rows: this.state.funnel.map((c) => [c.label, c.value]),
            },
            {
                title: "Team Composition",
                headers: ["Role", "Count"],
                rows: this.state.teamComposition.map((c) => [c.label, c.value]),
            },
            {
                title: `Progress by ${this.group.label}`,
                headers: [this.group.label, "Total",
                    ...cols.map(([, label]) => label), "Avg Score"],
                rows: this.state.rows.map((r) => [
                    r.name, r.total,
                    ...cols.map(([key]) => r[key] ?? 0),
                    r.avg_score ?? "",
                ]),
            },
        ];
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
        // The STAGE matters: "In Trajectory" and "In Pass @ K" count the very same
        // statuses on different stages. Without it, clicking either card would open
        // both — the exact merge the funnel exists to undo.
        const domain = [["status", "in", card.statuses]];
        if (card.stage) {
            domain.push(["stage_no", "=", card.stage]);
        }
        this._openAllocations(domain, card.label);
    }

    // A row is one person on the currently selected axis; drill into their records.
    onRowClick(row) {
        const { label, field } = this.group;
        const domain = [[field, "=", row.id || false]];
        this._openAllocations(domain, `${label} — ${row.name}`);
    }
}

registry.category("actions").add("kensei2_tracker_dashboard", Kensei2TrackerDashboard);
