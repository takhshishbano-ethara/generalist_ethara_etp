/** @odoo-module */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillStart } from "@odoo/owl";

const DEFAULT_GITHUB_URL =
//    "https://github.com/Ethara-Ai/swefficiency-Kraken";
    "https://github.com/Ethara-Ai/valkyrie";
const DEFAULT_DATASET_URL =
    "https://huggingface.co/datasets/ethara/Valkyrie";

export class ValkyrieShowcase extends Component {
    static template = "valkyrie_dashboard.ValkyrieShowcase";
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
                    ["valkyrie_dashboard.github_url", ""]
                ),
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["valkyrie_dashboard.dataset_url", ""]
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

registry.category("actions").add("valkyrie_dashboard.showcase", ValkyrieShowcase);
