/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class KrakenShowcase extends Component {
    static template = "kraken_dashboard.Showcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
//            trajectories_url: "https://github.com/Ethara-Ai/Kraken-Dataset",
            trajectories_url: "https://github.com/Ethara-Ai/kraken",
            dataset_url: "https://huggingface.co/datasets/ethara/Kraken",
            paper_url: "https://arxiv.org/abs/2511.06090",
        });

        onWillStart(async () => {
            const params = await this.orm.call(
                "ir.config_parameter",
                "get_param",
                ["kraken_dashboard.trajectories_url"],
            );
            if (params) this.state.trajectories_url = params;

            const dsUrl = await this.orm.call(
                "ir.config_parameter",
                "get_param",
                ["kraken_dashboard.dataset_url"],
            );
            if (dsUrl) this.state.dataset_url = dsUrl;
        });
    }
}

registry.category("actions").add("kraken_dashboard.showcase", KrakenShowcase);
