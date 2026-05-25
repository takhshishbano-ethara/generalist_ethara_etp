/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { ListRenderer } from "@web/views/list/list_renderer";
import { useBus } from "@web/core/utils/hooks";
import { useState } from "@odoo/owl";

const DEBOUNCE_MS = 350;

patch(ListRenderer.prototype, {
    setup() {
        super.setup();
        this.etharaFilters = useState({});
        this._etharaDebouncers = {};
        this._etharaApplying = false;

        if (this.env.searchModel) {
            // Re-apply column filters whenever the search bar reloads,
            // otherwise list.load({domain}) gets overwritten on every
            // search/filter/group-by change.
            useBus(this.env.searchModel, "update", () => {
                this._etharaReapplyAfterSearch();
            });
        }
    },

    getEtharaColumnField(column) {
        const fields = this.props.list && this.props.list.fields;
        if (!fields || !column || !column.name) {
            return null;
        }
        return fields[column.name] || null;
    },

    getEtharaFilterValue(column) {
        return this.etharaFilters[column.name] || "";
    },

    getEtharaSelectionOptions(column) {
        const field = this.getEtharaColumnField(column);
        return (field && field.selection) || [];
    },

    onEtharaFilterChange(column, value) {
        this._etharaSetFilter(column.name, value);
        this._etharaApply();
    },

    onEtharaFilterInput(column, ev) {
        const name = column.name;
        const value = ev.target.value;
        clearTimeout(this._etharaDebouncers[name]);
        this._etharaDebouncers[name] = setTimeout(() => {
            this._etharaSetFilter(name, value);
            this._etharaApply();
        }, DEBOUNCE_MS);
    },

    _etharaSetFilter(name, value) {
        if (value === "" || value === null || value === undefined) {
            delete this.etharaFilters[name];
        } else {
            this.etharaFilters[name] = value;
        }
    },

    _etharaBuildDomain() {
        const fields = this.props.list && this.props.list.fields;
        if (!fields) {
            return [];
        }
        const domain = [];
        for (const [name, rawValue] of Object.entries(this.etharaFilters)) {
            const field = fields[name];
            if (!field || rawValue === "" || rawValue === null || rawValue === undefined) {
                continue;
            }
            const type = field.type;
            if (type === "boolean") {
                domain.push([name, "=", rawValue === "true"]);
            } else if (type === "selection") {
                domain.push([name, "=", rawValue]);
            } else if (type === "date" || type === "datetime") {
                domain.push([name, "=", rawValue]);
            } else if (type === "integer") {
                const num = parseInt(rawValue, 10);
                if (!Number.isNaN(num)) {
                    domain.push([name, "=", num]);
                }
            } else if (type === "float" || type === "monetary") {
                const num = parseFloat(rawValue);
                if (!Number.isNaN(num)) {
                    domain.push([name, "=", num]);
                }
            } else if (type === "many2one") {
                domain.push([`${name}.display_name`, "ilike", rawValue]);
            } else if (type === "many2many" || type === "one2many") {
                domain.push([name, "ilike", rawValue]);
            } else {
                domain.push([name, "ilike", rawValue]);
            }
        }
        return domain;
    },

    async _etharaApply() {
        if (this._etharaApplying) {
            return;
        }
        const list = this.props.list;
        if (!list || typeof list.load !== "function") {
            return;
        }
        const etharaDomain = this._etharaBuildDomain();
        const searchDomain = (this.env.searchModel && this.env.searchModel.domain) || [];
        const fullDomain = [...searchDomain, ...etharaDomain];
        this._etharaApplying = true;
        try {
            await list.load({ domain: fullDomain });
        } finally {
            this._etharaApplying = false;
        }
    },

    _etharaReapplyAfterSearch() {
        if (!Object.keys(this.etharaFilters).length) {
            return;
        }
        this._etharaApply();
    },
});
