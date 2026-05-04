/** @odoo-module */

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillStart } from "@odoo/owl";

const PROJECTS = [
    { key: "kaiju", name: "Kaiju", description: "Library Generation from Scratch", defaultUrl: "https://projects.ethara.ai/kaiju" },
    { key: "kraken", name: "Kraken", description: "Code Review & Analysis", defaultUrl: "https://projects.ethara.ai/kraken" },
    { key: "aurora", name: "Aurora", description: "AI Training Pipeline", defaultUrl: "https://projects.ethara.ai/aurora" },
    { key: "valkyrie", name: "Valkyrie", description: "Quality Assurance Engine", defaultUrl: "https://projects.ethara.ai/valkyrie" },
    { key: "tesseract", name: "Tesseract", description: "Multi-Dimensional Benchmarks", defaultUrl: "https://projects.ethara.ai/tesseract" },
];

export class ProjectsHub extends Component {
    static template = "ethara_projects_hub.ProjectsHub";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({ projects: PROJECTS.map((p) => ({ ...p, url: p.defaultUrl })) });

        onWillStart(async () => {
            const params = await Promise.all(
                PROJECTS.map((p) =>
                    this.orm.call("ir.config_parameter", "get_param", [
                        `ethara_projects_hub.${p.key}_url`,
                        "",
                    ])
                )
            );
            this.state.projects = PROJECTS.map((p, i) => ({
                ...p,
                url: params[i] || p.defaultUrl,
            }));
        });
    }

    onCardClick(url) {
        window.open(url, "_blank", "noopener,noreferrer");
    }
}

registry.category("actions").add("ethara_projects_hub.hub", ProjectsHub);
