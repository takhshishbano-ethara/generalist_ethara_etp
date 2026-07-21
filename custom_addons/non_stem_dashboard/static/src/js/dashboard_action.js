/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
class NonStemDashboardView extends Component {
    static template = "non_stem_dashboard.DashboardView";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            runs: [],
            selectedRun: null,
            dashboardType: "tasker",
            loading: true,
            role: false,
        });

        onWillStart(async () => {
            this.state.role = await this.orm.call(
                "non.stem.run", "get_user_role", []
            );
            if (this.state.role === "viewer") {
                this.state.dashboardType = "management";
            }
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
        if (this.state.role !== "admin") {
            this.notification.add(
                "You don't have access to create a new run. Please contact your administrator.",
                { type: "danger", sticky: false }
            );
            return;
        }
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

// Client action to copy public link to clipboard
async function copyPublicLink(env, action) {
    const url = action.params && action.params.url;
    if (url) {
        await navigator.clipboard.writeText(url);
        env.services.notification.add("Public link copied to clipboard!", {
            type: "success",
        });
    }
}
registry.category("actions").add("non_stem_copy_public_link", copyPublicLink);
