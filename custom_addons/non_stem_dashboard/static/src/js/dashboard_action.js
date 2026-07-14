/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
class NonStemDashboardView extends Component {
    static template = "non_stem_dashboard.DashboardView";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            runs: [],
            selectedRun: null,
            dashboardType: "tasker",
            loading: true,
            isManager: false,
        });

        onWillStart(async () => {
            this.state.isManager = await this.orm.call(
                "non.stem.run", "check_is_manager", []
            );
            await this.loadRuns();
        });
    }

    async loadRuns() {
        this.state.loading = true;
        const runs = await this.orm.searchRead(
            "non.stem.run",
            [["state", "=", "done"]],
            ["id", "name", "create_date"],
            { order: "create_date desc", limit: 50 },
        );
        this.state.runs = runs;
        if (runs.length > 0 && !this.state.selectedRun) {
            this.state.selectedRun = runs[0].id;
        }
        this.state.loading = false;
    }

    get dashboardUrl() {
        if (!this.state.selectedRun) return "";
        const type = this.state.dashboardType;
        return `/non_stem_dashboard/${type}/${this.state.selectedRun}`;
    }

    onRunChange(ev) {
        this.state.selectedRun = parseInt(ev.target.value);
    }

    onTypeChange(ev) {
        this.state.dashboardType = ev.target.value;
    }

    openNewRun() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "non.stem.run",
            view_mode: "form",
            views: [[false, "form"]],
            target: "current",
        });
    }

    openFullScreen() {
        if (this.dashboardUrl) {
            window.open(this.dashboardUrl, "_blank");
        }
    }
}

registry.category("actions").add("non_stem_dashboard_view", NonStemDashboardView);
