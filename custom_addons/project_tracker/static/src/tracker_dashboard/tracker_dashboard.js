/** @odoo-module */
import { useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { Kensei2DashboardBase } from "@project_tracker/dashboard_base/dashboard_base";
import { ProgressTable } from "@project_tracker/progress_table/progress_table";
import { PtChart } from "@project_tracker/pt_chart/pt_chart";
import { CHART_COLORS, PIPELINE_COLORS } from "@project_tracker/pt_chart/chart_colors";

// The progress table's grouping axis: label + the allocation field to drill into.
const GROUPS = [
    { key: "pl", label: "PL", field: "pl_id" },
    { key: "ql", label: "QL", field: "assigned_ql_id" },
    { key: "tasker", label: "Tasker", field: "tasker_member_id" },
];

export class Kensei2TrackerDashboard extends Kensei2DashboardBase {
    static template = "project_tracker.TrackerDashboard";
    static components = { ProgressTable, PtChart };

    setup() {
        super.setup();
        this.groups = GROUPS;
        // Opened from a project workspace ("Open Dashboard") the action carries
        // active_project_id, so the dashboard lands pre-filtered to that project.
        // From the menu there is no such key → all-projects view.
        const ctx = (this.props.action && this.props.action.context) || {};
        this.state = useState({
            loading: true,
            funnel: [],
            kpis: [],
            teamComposition: [],
            stageMix: { stage1: 0, stage2: 0 },
            trend: [],
            byProject: [],
            workload: [],
            feedbackOutcomes: { shippable: 0, rework: 0, rejected: 0 },
            personaPool: { assigned: 0, unassigned: 0 },
            rows: [],
            groupBy: "pl",
            projectId: ctx.active_project_id || null,
            projects: [],
            lastUpdated: "",
            dateFrom: "",
            dateTo: "",
            // Whole-dashboard view: "overview" (cards/tables) or "charts".
            viewMode: "overview",
        });
        onWillStart(() => this._load());
    }

    setViewMode(mode) {
        this.state.viewMode = mode;
    }

    // Pipeline distribution as a doughnut — one segment per stage.
    get pipelineChartData() {
        const f = this.state.funnel;
        return {
            labels: f.map((c) => c.label),
            datasets: [{
                data: f.map((c) => c.value || 0),
                backgroundColor: f.map((c) => PIPELINE_COLORS[c.key] || CHART_COLORS.purple),
                borderColor: "#fff", borderWidth: 1,
            }],
        };
    }

    get pipelineChartOptions() {
        return { plugins: { legend: { position: "right" } }, cutout: "55%" };
    }

    onPipelineClick(index) {
        const card = this.state.funnel[index];
        if (card) {
            this.onStatusCardClick(card);
        }
    }

    // ---- Chart.js data + options (Charts view) --------------------------
    // Throughput COMBO: delivered/day bars + a 7-day rolling-average line so the
    // trend reads through the daily noise.
    get trendComboChartData() {
        const t = this.state.trend;
        const vals = t.map((d) => d.value);
        const roll = vals.map((_v, i) => {
            const from = Math.max(0, i - 6);
            const win = vals.slice(from, i + 1);
            return Math.round((win.reduce((a, b) => a + b, 0) / win.length) * 10) / 10;
        });
        return {
            labels: t.map((d) => d.label),
            datasets: [
                {
                    type: "bar", label: "Delivered", data: vals,
                    backgroundColor: "rgba(52,195,143,0.35)",
                    borderColor: CHART_COLORS.delivered, borderWidth: 1,
                    borderRadius: 3, order: 2,
                },
                {
                    type: "line", label: "7-day avg", data: roll,
                    borderColor: CHART_COLORS.purple, backgroundColor: "transparent",
                    tension: 0.35, pointRadius: 0, borderWidth: 2, order: 1,
                },
            ],
        };
    }

    get comboOptions() {
        return {
            plugins: { legend: { position: "bottom" } },
            scales: {
                x: { grid: { display: false } },
                y: { beginAtZero: true, grid: { color: CHART_COLORS.grid } },
            },
        };
    }

    // Current tasks split by pipeline stage (Stage 1 vs Stage 2).
    get stageMixChartData() {
        const s = this.state.stageMix;
        return {
            labels: ["Stage 1", "Stage 2"],
            datasets: [{
                data: [s.stage1, s.stage2],
                backgroundColor: [CHART_COLORS.purple, CHART_COLORS.teal],
                borderWidth: 0,
            }],
        };
    }

    onStageClick(index) {
        // Stage 2 covers stage_no >= 2 (any stage past the first runs that ladder).
        const domain = index === 0
            ? [["stage_no", "=", 1]]
            : [["stage_no", ">=", 2]];
        this._openAllocations(
            [...domain, ["is_current_stage", "=", true]],
            `Stage ${index === 0 ? 1 : 2}`);
    }

    // Delivery-rate GAUGE — a half-doughnut; the % is overlaid in the template.
    get gaugeValue() {
        const k = this.state.kpis.find((x) => x.key === "delivery_rate");
        return k && (k.value || k.value === 0) ? k.value : null;
    }

    get gaugeChartData() {
        const v = this.gaugeValue || 0;
        return {
            labels: ["Delivered", "Remaining"],
            datasets: [{
                data: [v, Math.max(0, 100 - v)],
                backgroundColor: [CHART_COLORS.delivered, "#eceff3"],
                borderWidth: 0,
            }],
        };
    }

    get gaugeOptions() {
        return {
            rotation: -90, circumference: 180, cutout: "72%",
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
        };
    }


    get byProjectChartData() {
        const p = this.state.byProject;
        return {
            labels: p.map((r) => r.project),
            datasets: [
                { label: "In Progress", data: p.map((r) => r.wip), backgroundColor: CHART_COLORS.wip, stack: "s" },
                { label: "Delivered", data: p.map((r) => r.delivered), backgroundColor: CHART_COLORS.delivered, stack: "s" },
                { label: "Failed", data: p.map((r) => r.failed), backgroundColor: CHART_COLORS.failed, stack: "s" },
            ],
        };
    }

    get byProjectChartOptions() {
        return {
            plugins: { legend: { position: "bottom" } },
            scales: {
                x: { stacked: true, grid: { display: false } },
                y: { stacked: true, beginAtZero: true, grid: { color: CHART_COLORS.grid } },
            },
        };
    }

    get workloadChartData() {
        const w = this.state.workload;
        return {
            labels: w.map((r) => r.tasker),
            datasets: [{
                label: "In Progress", data: w.map((r) => r.wip),
                backgroundColor: CHART_COLORS.purple, borderRadius: 4,
            }],
        };
    }

    get workloadChartOptions() {
        return {
            indexAxis: "y",
            plugins: { legend: { display: false } },
            scales: {
                x: { beginAtZero: true, grid: { color: CHART_COLORS.grid } },
                y: { grid: { display: false } },
            },
        };
    }

    get personaPoolChartData() {
        const p = this.state.personaPool;
        return {
            labels: ["Assigned", "Unassigned"],
            datasets: [{
                data: [p.assigned, p.unassigned],
                backgroundColor: [CHART_COLORS.delivered, CHART_COLORS.amber],
                borderWidth: 0,
            }],
        };
    }

    get feedbackChartData() {
        const f = this.state.feedbackOutcomes;
        return {
            labels: ["Shippable", "Rework", "Rejected"],
            datasets: [{
                data: [f.shippable, f.rework, f.rejected],
                backgroundColor: [CHART_COLORS.delivered, CHART_COLORS.amber, CHART_COLORS.failed],
                borderWidth: 0,
            }],
        };
    }

    onFeedbackClick(index) {
        const status = ["shippable", "rework", "rejected"][index];
        if (status) {
            this._openAllocations(
                [["feedback_status", "=", status], ["is_current_stage", "=", true]],
                `Feedback — ${status}`);
        }
    }

    get doughnutOptions() {
        return { plugins: { legend: { position: "bottom" } }, cutout: "62%" };
    }

    // ---- chart drill-downs (reuse the list openers) ---------------------
    onByProjectClick(index) {
        const p = this.state.byProject[index];
        if (p) {
            this._openAllocations(
                [["project_id", "=", p.project_id || false]],
                `Project — ${p.project}`);
        }
    }

    onWorkloadClick(index) {
        const w = this.state.workload[index];
        if (w) {
            this._openAllocations(
                [["tasker_member_id", "=", w.tasker_id],
                 ["is_current_stage", "=", true],
                 ["status", "not in", ["deliverable", "failed"]]],
                `Workload — ${w.tasker}`);
        }
    }

    onPersonaPoolClick(index) {
        const status = index === 0 ? "assigned" : "unassigned";
        this.action.doAction({
            type: "ir.actions.act_window",
            name: `Personas — ${status}`,
            res_model: "kensei2.persona",
            domain: [["pt_assignment_status", "=", status]],
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    async _load() {
        const res = await this._fetch(
            "/project_tracker/dashboard",
            { group_by: this.state.groupBy, project_id: this.state.projectId },
            "Failed to load dashboard data.");
        if (!res) {
            return;
        }
        if (res.error) {
            this.notification.add("You are not allowed to view this dashboard.",
                { type: "warning" });
            return;
        }
        this.state.funnel = res.funnel || [];
        this.state.kpis = res.kpis || [];
        this.state.teamComposition = res.team_composition || [];
        this.state.stageMix = res.stage_mix || { stage1: 0, stage2: 0 };
        this.state.trend = res.trend || [];
        this.state.byProject = res.by_project || [];
        this.state.workload = res.workload || [];
        this.state.feedbackOutcomes = res.feedback_outcomes || { shippable: 0, rework: 0, rejected: 0 };
        this.state.personaPool = res.persona_pool || { assigned: 0, unassigned: 0 };
        this.state.rows = res.rows || [];
        this.state.projects = res.projects || [];
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
        return `project_tracker_main_dashboard_${this.state.groupBy}.csv`;
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
                title: "KPIs",
                headers: ["Metric", "Value"],
                rows: this.state.kpis.map((s) => [s.label, s.value ?? ""]),
            },
            {
                title: "Pipeline Funnel",
                headers: ["Step", "Count"],
                rows: this.state.funnel.map((c) => [c.label, c.value]),
            },
            {
                title: "By Project",
                headers: ["Project", "In Progress", "Delivered", "Failed"],
                rows: this.state.byProject.map(
                    (r) => [r.project, r.wip, r.delivered, r.failed]),
            },
            {
                title: "Workload",
                headers: ["Tasker", "In Progress"],
                rows: this.state.workload.map((r) => [r.tasker, r.wip]),
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
            res_model: "project.tracker.allocation",
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
            res_model: "project.tracker.team.member",
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

    // A row is one person on the currently selected axis; the drill-down opens
    // exactly the records the row counted.
    onRowClick(row) {
        const { label, field } = this.group;
        this._openAllocations(
            [[field, "=", row.id || false]], `${label} — ${row.name}`);
    }
}

registry.category("actions").add("project_tracker_dashboard", Kensei2TrackerDashboard);
