/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const DEFAULT_GITHUB_URL = "https://github.com/EtharaOrion/milo-bench-samples";
const DEFAULT_HUGGINGFACE_URL = "https://huggingface.co/datasets/ethara/milo-bench-samples";
const DEFAULT_PAPER_URL = "https://github.com/EtharaOrion/milo-bench-samples#readme";

export class MilobenchShowcase extends Component {
    static template = "milobench_dashboard.MilobenchShowcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            githubUrl: DEFAULT_GITHUB_URL,
            huggingfaceUrl: DEFAULT_HUGGINGFACE_URL,
            paperUrl: DEFAULT_PAPER_URL,
            summary: null,
        });

        onWillStart(async () => {
            const [github, hf, paper, summary] = await Promise.all([
                this.orm.call("ir.config_parameter", "get_param", ["milobench_dashboard.github_url", DEFAULT_GITHUB_URL]),
                this.orm.call("ir.config_parameter", "get_param", ["milobench_dashboard.huggingface_url", DEFAULT_HUGGINGFACE_URL]),
                this.orm.call("ir.config_parameter", "get_param", ["milobench_dashboard.paper_url", DEFAULT_PAPER_URL]),
                this._fetchSummary(),
            ]);
            if (github) this.state.githubUrl = github;
            if (hf) this.state.huggingfaceUrl = hf;
            if (paper) this.state.paperUrl = paper;
            this.state.summary = summary;
            this.state.loading = false;
        });
    }

    async _fetchSummary() {
        try {
            const response = await fetch("/milobench-samples/api/summary", {
                headers: { Accept: "application/json" },
                cache: "no-store",
            });
            if (!response.ok) return null;
            return await response.json();
        } catch (_err) {
            return null;
        }
    }

    formatPercent(value) {
        if (value === null || value === undefined) return "—";
        return `${Number(value).toFixed(1)}%`;
    }

    formatMoney(value) {
        if (!value) return "$0";
        return `$${Number(value).toFixed(2)}`;
    }

    openTasks() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Tasks",
            res_model: "milobench.task",
            view_mode: "kanban,list,form,graph,pivot",
            views: [[false, "kanban"], [false, "list"], [false, "form"], [false, "graph"], [false, "pivot"]],
            target: "current",
        });
    }

    openRuns() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Runs",
            res_model: "milobench.run",
            view_mode: "list,form,graph,pivot",
            views: [[false, "list"], [false, "form"], [false, "graph"], [false, "pivot"]],
            target: "current",
        });
    }

    openPortal() {
        window.open("/milobench-samples", "_blank");
    }
}

registry.category("actions").add("milobench_dashboard.showcase", MilobenchShowcase);
