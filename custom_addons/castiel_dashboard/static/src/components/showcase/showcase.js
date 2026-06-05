/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class CastielShowcase extends Component {
    static template = "castiel_dashboard.Showcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            repo_url: "https://www.ethara.ai",
            paper_url: "https://arxiv.org/abs/2506.02548",
        });

        onWillStart(async () => {
            const repo = await this.orm.call(
                "ir.config_parameter",
                "get_param",
                ["castiel_dashboard.repo_url"],
            );
            if (repo) this.state.repo_url = repo;

            const paper = await this.orm.call(
                "ir.config_parameter",
                "get_param",
                ["castiel_dashboard.paper_url"],
            );
            if (paper) this.state.paper_url = paper;
        });
    }
}

registry.category("actions").add("castiel_dashboard.showcase", CastielShowcase);
