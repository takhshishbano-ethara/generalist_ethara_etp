/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { WebClient } from "@web/webclient/webclient";

patch(WebClient.prototype, {
    async _loadDefaultApp() {
        try {
            await this.actionService.doAction(
                "ethara_etp_theme.ethara_favorites_home_action",
                { clearBreadcrumbs: true }
            );
            return true;
        } catch (_error) {
            return super._loadDefaultApp(...arguments);
        }
    },
});
