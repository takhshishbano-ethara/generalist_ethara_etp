/** @odoo-module **/

import { Component, useState, useRef } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class DocumensoTemplateDesigner extends Component {
    static template = "documenso_connector.TemplateDesigner";
    static props = {
        action: Object,
        actionId: { type: [Number, String], optional: true },
        className: { type: String, optional: true },
        globalState: { type: Object, optional: true },
        updateActionState: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.iframeRef = useRef("iframe");

        const params = (this.props.action && this.props.action.params) || {};
        this.state = useState({
            templateId: params.template_id || null,
            documensoId: params.documenso_id || "",
            title: params.title || _t("Documenso Template"),
            editorUrl: params.editor_url || "",
            refreshing: false,
        });
    }

    onReloadIframe() {
        if (this.iframeRef.el) {
            this.iframeRef.el.src = this.state.editorUrl;
        }
    }

    async onClose() {
        if (this.state.templateId) {
            this.state.refreshing = true;
            try {
                await this.orm.call(
                    "documenso.template",
                    "action_refresh_one",
                    [[this.state.templateId]],
                );
            } catch (error) {
                this.notification.add(
                    _t("Could not refresh template from Documenso: %s", error.message || error),
                    { type: "warning" },
                );
            } finally {
                this.state.refreshing = false;
            }
            await this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "documenso.template",
                res_id: this.state.templateId,
                views: [[false, "form"]],
                target: "current",
            });
        } else {
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "documenso.template",
                views: [[false, "list"], [false, "form"]],
                target: "current",
            });
        }
    }
}

registry.category("actions").add("documenso_template_designer", DocumensoTemplateDesigner);
