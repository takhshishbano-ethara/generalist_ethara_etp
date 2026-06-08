/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { CogMenu } from "@web/search/cog_menu/cog_menu";
import { download } from "@web/core/network/download";

const T2AV_MODEL = "t2av.generation";

patch(CogMenu.prototype, {
    get hasItems() {
        return super.hasItems || this.isT2AVCompleteVisible;
    },

    get isT2AVCompleteVisible() {
        return this.env?.searchModel?.resModel === T2AV_MODEL;
    },

    _t2avSelectedIds() {
        const getActiveIds = this.props?.getActiveIds;
        if (typeof getActiveIds !== "function") {
            return [];
        }
        const ids = getActiveIds() || [];
        return ids.filter((id) => Number.isInteger(id));
    },

    async _downloadT2AVComplete(format) {
        const data = { format };
        const ids = this._t2avSelectedIds();
        if (ids.length) {
            data.ids = ids.join(",");
        }
        await download({
            url: "/t2av/export/complete",
            data,
        });
    },

    onExportT2AVCompleteCSV() {
        return this._downloadT2AVComplete("csv");
    },

    onExportT2AVCompleteXLSX() {
        return this._downloadT2AVComplete("xlsx");
    },
});
