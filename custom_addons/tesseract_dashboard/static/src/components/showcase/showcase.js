/** @odoo-module */

/*
 * Backend OWL component for the Tesseract showcase.
 * Registered as a client action via views/tesseract_dashboard_menus.xml
 * (tag: tesseract_dashboard.showcase). Renders the same content as the
 * public /tesseract portal page, reading link URLs from
 * ir.config_parameter via ORM so admin overrides in Settings -> Tesseract
 * Showcase take effect in the backend view as well.
 */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillStart } from "@odoo/owl";

// const DEFAULT_TRAJECTORIES_URL = "https://github.com/Ethara-Ai/tesseract";
const DEFAULT_TRAJECTORIES_URL = "https://github.com/Ethara-Ai/tesseract-harness";
const DEFAULT_DATASET_URL =
    "https://huggingface.co/datasets/ethara/tesseract";

export class TesseractShowcase extends Component {
    static template = "tesseract_dashboard.TesseractShowcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            trajectoriesUrl: DEFAULT_TRAJECTORIES_URL,
            datasetUrl: DEFAULT_DATASET_URL,
        });

        onWillStart(async () => {
            const [trajUrl, dsUrl] = await Promise.all([
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["tesseract_dashboard.trajectories_url", ""]
                ),
                this.orm.call(
                    "ir.config_parameter", "get_param",
                    ["tesseract_dashboard.dataset_url", ""]
                ),
            ]);
            if (trajUrl) {
                this.state.trajectoriesUrl = trajUrl;
            }
            if (dsUrl) {
                this.state.datasetUrl = dsUrl;
            }
        });
    }
}

registry.category("actions").add(
    "tesseract_dashboard.showcase",
    TesseractShowcase
);
