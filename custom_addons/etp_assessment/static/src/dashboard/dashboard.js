/** @odoo-module */

import { registry } from "@web/core/registry";
import { Component, useState, onMounted, onWillUnmount, useRef } from "@odoo/owl";
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
            dimension_stats: [],
            assessment_options: [],
        });

        this.filters = useState({
            assessment_id: false,
            state: "",
            category_id: false,
            date_from: "",
            date_to: "",
        });

        this.questionTypeChart = useRef("questionTypeChart");
        this.categoryChart = useRef("categoryChart");
        this.dimensionChart = useRef("dimensionChart");
        this.completionChart = useRef("completionChart");

        this._timer = null;
        this._charts = [];

        onMounted(async () => {
            await this._loadData();
            this._timer = setInterval(() => this._loadData(), REFRESH_INTERVAL);
        });

        onWillUnmount(() => {
            if (this._timer) clearInterval(this._timer);
            this._destroyCharts();
        });
    }

    _getFilters() {
        const f = {};
        if (this.filters.assessment_id) f.assessment_id = this.filters.assessment_id;
        if (this.filters.state) f.state = this.filters.state;
        if (this.filters.category_id) f.category_id = this.filters.category_id;
        if (this.filters.date_from) f.date_from = this.filters.date_from;
        if (this.filters.date_to) f.date_to = this.filters.date_to;
        return f;
    }

    async _loadData() {
        try {
            const data = await this.orm.call("etp.assessment", "get_dashboard_data", [this._getFilters()]);
            Object.assign(this.state, data, { loaded: true });
            this._renderCharts();
        } catch {
            this.state.loaded = true;
        }
    }

    async applyFilters() {
        await this._loadData();
    }

    clearFilters() {
        this.filters.assessment_id = false;
        this.filters.state = "";
        this.filters.category_id = false;
        this.filters.date_from = "";
        this.filters.date_to = "";
        this._loadData();
    }

    onAssessmentChange(ev) {
        const val = ev.target.value;
        this.filters.assessment_id = val ? parseInt(val) : false;
        this._loadData();
    }

    onStateChange(state) {
        this.filters.state = this.filters.state === state ? "" : state;
        this._loadData();
    }

    onCategoryChange(ev) {
        const val = ev.target.value;
        this.filters.category_id = val ? parseInt(val) : false;
        this._loadData();
    }

    onDateFromChange(ev) {
        this.filters.date_from = ev.target.value;
        if (this.filters.date_from && this.filters.date_to) this._loadData();
    }

    onDateToChange(ev) {
        this.filters.date_to = ev.target.value;
        if (this.filters.date_from && this.filters.date_to) this._loadData();
    }

    get hasActiveFilters() {
        return this.filters.assessment_id || this.filters.state || this.filters.category_id || this.filters.date_from || this.filters.date_to;
    }

    _destroyCharts() {
        for (const chart of this._charts) {
            chart.destroy();
        }
        this._charts = [];
    }

    _renderCharts() {
        this._destroyCharts();
        this._renderQuestionTypeChart();
        this._renderCategoryChart();
        this._renderDimensionChart();
        this._renderCompletionChart();
    }

    _renderQuestionTypeChart() {
        const el = this.questionTypeChart.el;
        if (!el) return;
        const data = this.state.question_types.filter((d) => d.count > 0);
        if (!data.length) return;
        const ctx = el.getContext("2d");
        const labels = data.map((d) => d.type.replace("_", " "));
        const values = data.map((d) => d.count);
        const colors = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f"];

        const chart = new Chart(ctx, {
            type: "doughnut",
            data: {
                labels,
                datasets: [{ data: values, backgroundColor: colors }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { position: "bottom", labels: { padding: 12 } },
                },
            },
        });
        this._charts.push(chart);
    }

    _renderCategoryChart() {
        const el = this.categoryChart.el;
        if (!el) return;
        const data = this.state.categories.filter((d) => d.count > 0);
        if (!data.length) return;
        const ctx = el.getContext("2d");
        const labels = data.map((d) => d.name);
        const values = data.map((d) => d.count);

        const chart = new Chart(ctx, {
            type: "bar",
            data: {
                labels,
                datasets: [{
                    label: "Questions",
                    data: values,
                    backgroundColor: "#4e79a7",
                    borderRadius: 4,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { beginAtZero: true, ticks: { stepSize: 1 } },
                },
            },
        });
        this._charts.push(chart);
    }

    _renderDimensionChart() {
        const el = this.dimensionChart.el;
        if (!el || !this.state.dimension_stats.length) return;
        const ctx = el.getContext("2d");
        const labels = this.state.dimension_stats.map((d) => d.name);
        const accuracy = this.state.dimension_stats.map((d) => d.accuracy);

        const chart = new Chart(ctx, {
            type: "bar",
            data: {
                labels,
                datasets: [{
                    label: "Accuracy %",
                    data: accuracy,
                    backgroundColor: "#59a14f",
                    borderRadius: 4,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: "y",
                plugins: { legend: { display: false } },
                scales: {
                    x: { beginAtZero: true },
                },
            },
        });
        this._charts.push(chart);
    }

    _renderCompletionChart() {
        const el = this.completionChart.el;
        if (!el) return;
        const ctx = el.getContext("2d");
        const kpis = this.state.kpis;
        const completed = kpis.evaluators_submitted || 0;
        const remaining = (kpis.total_evaluators || 0) - completed;
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
                plugins: {
                    legend: { position: "bottom" },
                },
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
