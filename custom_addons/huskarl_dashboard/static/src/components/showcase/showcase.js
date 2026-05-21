/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class HuskarlShowcase extends Component {
    static template = "huskarl_dashboard.Showcase";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
//            github_url: "https://github.com/harbor-framework/harbor",
            github_url: "https://github.com/Ethara-Ai/mars",
            docs_url: "https://harborframework.com/docs",
        });

        onWillStart(async () => {
            const ghUrl = await this.orm.call(
                "ir.config_parameter",
                "get_param",
                ["huskarl_dashboard.github_url"],
            );
            if (ghUrl) this.state.github_url = ghUrl;

            const docsUrl = await this.orm.call(
                "ir.config_parameter",
                "get_param",
                ["huskarl_dashboard.docs_url"],
            );
            if (docsUrl) this.state.docs_url = docsUrl;
        });
    }
}

registry.category("actions").add("huskarl_dashboard.showcase", HuskarlShowcase);
