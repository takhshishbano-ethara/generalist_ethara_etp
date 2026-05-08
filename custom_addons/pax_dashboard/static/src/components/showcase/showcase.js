import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class PaxShowcase extends Component {
    static template = "pax_dashboard.Showcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            trajectories_url: "https://github.com/Ethara-Ai/pax",
            dataset_url: "https://huggingface.co/datasets/ethara/Pax",
        });

        onWillStart(async () => {
            const params = await this.orm.call(
                "ir.config_parameter",
                "get_param",
                ["pax_dashboard.trajectories_url"],
            );
            if (params) this.state.trajectories_url = params;

            const dsUrl = await this.orm.call(
                "ir.config_parameter",
                "get_param",
                ["pax_dashboard.dataset_url"],
            );
            if (dsUrl) this.state.dataset_url = dsUrl;
        });
    }
}

registry.category("actions").add("pax_dashboard.showcase", PaxShowcase);
