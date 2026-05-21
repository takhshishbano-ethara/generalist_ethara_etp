/** @odoo-module */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillStart } from "@odoo/owl";

//const DEFAULT_GITHUB_URL = "https://github.com/Ethara-Ai/terra";
const DEFAULT_GITHUB_URL = "https://github.com/Ethara-Ai/terra";
const DEFAULT_DATASET_URL = "https://huggingface.co/datasets/ethara/terra";

export class TerraShowcase extends Component {
    static template = "nokor_dashboard.NokorShowcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            githubUrl: DEFAULT_GITHUB_URL,
            datasetUrl: DEFAULT_DATASET_URL,
        });

        onWillStart(async () => {
            const [ghUrl, dsUrl] = await Promise.all([
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["nokor_dashboard.github_url", ""]
                ),
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["nokor_dashboard.dataset_url", ""]
                ),
            ]);
            if (ghUrl) {
                this.state.githubUrl = ghUrl;
            }
            if (dsUrl) {
                this.state.datasetUrl = dsUrl;
            }
        });
    }
}

registry.category("actions").add("nokor_dashboard.showcase", TerraShowcase);
