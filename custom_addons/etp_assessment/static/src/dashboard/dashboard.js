/** @odoo-module */

import { registry } from "@web/core/registry";
import { Component, useState, onMounted, onWillUnmount, useRef, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const REFRESH_INTERVAL = 60000;

export class EtpAssessmentDashboard extends Component {
    static template = "etp_assessment.Dashboard";
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");

        this.state = useState({
            loaded: false,
            kpis: {},
            question_types: [],
            categories: [],
            active_work: [],
            evaluator_performance: [],
            assessment_options: [],
        });

        this.filters = useState({
            assessment_id: 0,
            state: "",
            category_id: 0,
            date_from: "",
            date_to: "",
        });

        this.questionTypeChart = useRef("questionTypeChart");
        this.categoryChart = useRef("categoryChart");
        this.completionChart = useRef("completionChart");

        this._timer = null;
        this._charts = [];

        onWillStart(async () => {
            await this._loadData();
        });

        onMounted(() => {
            this._renderCharts();
            this._timer = setInterval(() => this._loadData(), REFRESH_INTERVAL);
        });

        onWillUnmount(() => {
            if (this._timer) clearInterval(this._timer);
            this._destroyCharts();
        });
    }

    _buildFilters() {
        const f = {};
        if (this.filters.assessment_id) f.assessment_id = this.filters.assessment_id;
        if (this.filters.state) f.state = this.filters.state;
        if (this.filters.category_id) f.category_id = this.filters.category_id;
        if (this.filters.date_from) f.date_from = this.filters.date_from;
        if (this.filters.date_to) f.date_to = this.filters.date_to;
        return Object.keys(f).length > 0 ? f : null;
    }

    async _loadData() {
        try {
            const filters = this._buildFilters();
            const data = await this.orm.call(
                "etp.assessment",
                "get_dashboard_data",
                [filters]
            );
            this.state.kpis = data.kpis || {};
            this.state.question_types = data.question_types || [];
            this.state.categories = data.categories || [];
            this.state.active_work = data.active_work || [];
            this.state.evaluator_performance = data.evaluator_performance || [];
            this.state.assessment_options = data.assessment_options || [];
            this.state.loaded = true;
            this._renderCharts();
        } catch (e) {
            console.error("Dashboard load error:", e);
            this.state.loaded = true;
        }
    }

    onAssessmentFilter(ev) {
        this.filters.assessment_id = parseInt(ev.target.value) || 0;
        this._loadData();
    }

    onCategoryFilter(ev) {
        this.filters.category_id = parseInt(ev.target.value) || 0;
        this._loadData();
    }

    onStateFilter(val) {
        this.filters.state = this.filters.state === val ? "" : val;
        this._loadData();
    }

    onDateFromFilter(ev) {
        this.filters.date_from = ev.target.value || "";
        this._loadData();
    }

    onDateToFilter(ev) {
        this.filters.date_to = ev.target.value || "";
        this._loadData();
    }

    onClearFilters() {
        this.filters.assessment_id = 0;
        this.filters.state = "";
        this.filters.category_id = 0;
        this.filters.date_from = "";
        this.filters.date_to = "";
        this._loadData();
    }

    get hasActiveFilters() {
        return !!(
            this.filters.assessment_id ||
            this.filters.state ||
            this.filters.category_id ||
            this.filters.date_from ||
            this.filters.date_to
        );
    }

    _destroyCharts() {
        for (const c of this._charts) {
            c.destroy();
        }
        this._charts = [];
    }

    _renderCharts() {
        this._destroyCharts();
        this._renderQuestionTypeChart();
        this._renderCategoryChart();
        this._renderCompletionChart();
    }

    _renderQuestionTypeChart() {
        const el = this.questionTypeChart.el;
        if (!el) return;
        const items = (this.state.question_types || []).filter((d) => d.count > 0);
        if (!items.length) return;
        const ctx = el.getContext("2d");
        const chart = new Chart(ctx, {
            type: "doughnut",
            data: {
                labels: items.map((d) => d.type.replace("_", " ")),
                datasets: [{
                    data: items.map((d) => d.count),
                    backgroundColor: ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f"],
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: "bottom", labels: { padding: 12 } } },
            },
        });
        this._charts.push(chart);
    }

    _renderCategoryChart() {
        const el = this.categoryChart.el;
        if (!el) return;
        const items = (this.state.categories || []).filter((d) => d.count > 0);
        if (!items.length) return;
        const ctx = el.getContext("2d");
        const chart = new Chart(ctx, {
            type: "bar",
            data: {
                labels: items.map((d) => d.name),
                datasets: [{
                    label: "Questions",
                    data: items.map((d) => d.count),
                    backgroundColor: "#4e79a7",
                    borderRadius: 4,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } },
            },
        });
        this._charts.push(chart);
    }

    _renderCompletionChart() {
        const el = this.completionChart.el;
        if (!el) return;
        const ctx = el.getContext("2d");
        const completed = this.state.kpis.evaluators_submitted || 0;
        const remaining = (this.state.kpis.total_evaluators || 0) - completed;
        if (!completed && !remaining) return;
        const chart = new Chart(ctx, {
            type: "doughnut",
            data: {
                labels: ["Completed", "Remaining"],
                datasets: [{
                    data: [completed, remaining],
                    backgroundColor: ["#59a14f", "#e0e0e0"],
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: "bottom" } },
            },
        });
        this._charts.push(chart);
    }

    openAssessments(state) {
        const domain = state ? [["state", "=", state]] : [];
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Assessments",
            res_model: "etp.assessment",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain,
        });
    }

    openQuestions() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Question Bank",
            res_model: "etp.assessment.question",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
        });
    }

    openEvaluators() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Candidate Assignments",
            res_model: "etp.assessment.evaluator",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
        });
    }

    openResponses() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Responses",
            res_model: "etp.assessment.response",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
        });
    }

    openViolators() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Violations",
            res_model: "etp.assessment.evaluator",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain: [["is_violated", "=", true]],
        });
    }

    openAssessmentRecord(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "etp.assessment",
            res_id: id,
            view_mode: "form",
            views: [[false, "form"]],
        });
    }

    scorePercentage(score, max) {
        if (!max) return "0%";
        return Math.round((score / max) * 100) + "%";
    }
}

registry.category("actions").add("etp_assessment.dashboard", EtpAssessmentDashboard);
