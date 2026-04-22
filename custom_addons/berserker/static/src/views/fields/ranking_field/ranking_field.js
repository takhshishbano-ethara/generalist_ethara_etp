/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, useState, useRef, onMounted } from "@odoo/owl";

export class RankingDragDropField extends Component {
    static template = "berserker.RankingDragDropField";
    static props = { ...standardFieldProps };

    setup() {
        this.containerRef = useRef("container");
        const raw = this.props.record.data[this.props.name] || "gpt,gemini,claude";
        const order = raw.split(",").map((s) => s.trim()).filter(Boolean);
        this.state = useState({ items: order.length === 3 ? order : ["gpt", "gemini", "claude"] });
        this.dragIndex = null;

        this.labels = { gpt: "GPT", gemini: "Gemini", claude: "Claude" };
        this.colors = { gpt: "#10a37f", gemini: "#4285f4", claude: "#c45228" };
    }

    get isReadonly() {
        return this.props.readonly;
    }

    rankLabel(idx) {
        return ["🥇 1st", "🥈 2nd", "🥉 3rd"][idx] || "";
    }

    onDragStart(ev, idx) {
        if (this.isReadonly) return;
        this.dragIndex = idx;
        ev.dataTransfer.effectAllowed = "move";
        ev.target.classList.add("o_ranking_dragging");
    }

    onDragEnd(ev) {
        ev.target.classList.remove("o_ranking_dragging");
        this.dragIndex = null;
    }

    onDragOver(ev, idx) {
        if (this.isReadonly || this.dragIndex === null || this.dragIndex === idx) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
    }

    onDragEnter(ev, idx) {
        if (this.isReadonly || this.dragIndex === null || this.dragIndex === idx) return;
        ev.preventDefault();
        ev.target.closest(".o_ranking_card")?.classList.add("o_ranking_drop_target");
    }

    onDragLeave(ev) {
        ev.target.closest(".o_ranking_card")?.classList.remove("o_ranking_drop_target");
    }

    onDrop(ev, targetIdx) {
        ev.preventDefault();
        ev.target.closest(".o_ranking_card")?.classList.remove("o_ranking_drop_target");
        if (this.isReadonly || this.dragIndex === null || this.dragIndex === targetIdx) return;

        const items = [...this.state.items];
        const [moved] = items.splice(this.dragIndex, 1);
        items.splice(targetIdx, 0, moved);
        this.state.items = items;
        this.dragIndex = null;

        const newValue = items.join(",");
        this.props.record.update({ [this.props.name]: newValue });
    }
}

export const rankingDragDropField = {
    component: RankingDragDropField,
    displayName: "Ranking (Drag & Drop)",
    supportedTypes: ["char"],
    extractProps: () => ({}),
};

registry.category("fields").add("berserker_ranking", rankingDragDropField);
