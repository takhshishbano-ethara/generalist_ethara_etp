/** @odoo-module */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillStart } from "@odoo/owl";

const DEFAULT_HARNESS_URL = "https://github.com/Ethara-ai/yc-bench";
const DEFAULT_DATASET_URL = "https://github.com/Ethara-Ai/rinzler-dataset";

export class RinzlerShowcase extends Component {
    static template = "rinzler_dashboard.RinzlerShowcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            harnessUrl: DEFAULT_HARNESS_URL,
            datasetUrl: DEFAULT_DATASET_URL,
        });

        onWillStart(async () => {
            const [harnessUrl, dsUrl] = await Promise.all([
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["rinzler_dashboard.harness_url", ""]
                ),
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["rinzler_dashboard.dataset_url", ""]
                ),
            ]);
            if (harnessUrl) {
                this.state.harnessUrl = harnessUrl;
            }
            if (dsUrl) {
                this.state.datasetUrl = dsUrl;
            }
        });
    }
}

registry.category("actions").add("rinzler_dashboard.showcase", RinzlerShowcase);
