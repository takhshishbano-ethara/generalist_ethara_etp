/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { rpc } from "@web/core/network/rpc";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, useState, onWillUnmount } from "@odoo/owl";

export class SkollSpawnTreeField extends Component {
    static template = "skoll.SpawnTreeField";
    static props = { ...standardFieldProps };

    setup() {
        this.state = useState({
            treeText: "",
            loading: false,
            copied: false,
        });

        this._onTreeReady = (ev) => this._handleTreeReady(ev.detail);
        this.env.bus.addEventListener("SKOLL_SPAWN_TREE_READY", this._onTreeReady);

        useRecordObserver((record) => {
            this.state.treeText = record.data[this.props.name] || "";
        });
        this.state.treeText = this.props.record.data[this.props.name] || "";

        onWillUnmount(() => {
            this.env.bus.removeEventListener("SKOLL_SPAWN_TREE_READY", this._onTreeReady);
        });
    }

    _handleTreeReady(data) {
        if (data && data.spawn_tree) {
            this.state.treeText = data.spawn_tree;
        }
    }

    get hasTree() {
        return !!this.state.treeText.trim();
    }

    async onCopy() {
        const text = this.state.treeText;
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
        }
        this.state.copied = true;
        setTimeout(() => { this.state.copied = false; }, 2000);
    }

    async onRefreshTree() {
        const recordId = this.props.record.resId;
        if (!recordId || this.state.loading) return;

        if (this.props.record.isDirty) {
            await this.props.record.save();
        }

        this.state.loading = true;
        try {
            const result = await rpc("/skoll/spawn_tree", { record_id: recordId });
            if (result.status === "success" && result.spawn_tree) {
                this.state.treeText = result.spawn_tree;
                await this.props.record.load();
            }
        } catch (err) {
            console.warn("Spawn tree refresh failed:", err);
        } finally {
            this.state.loading = false;
        }
    }
}

export const skollSpawnTreeField = {
    component: SkollSpawnTreeField,
    displayName: _t("Spawn Tree"),
    supportedTypes: ["text"],
    extractProps: () => ({}),
};

registry.category("fields").add("skoll_spawn_tree", skollSpawnTreeField);
