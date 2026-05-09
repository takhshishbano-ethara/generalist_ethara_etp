/** @odoo-module **/
import { Component, useState } from "@odoo/owl";

const CONFIG_SECTIONS = [
    {
        key: "lora",
        label: "LoRA",
        fields: [
            { key: "lora_rank", label: "Rank", type: "number", step: 1 },
            { key: "lora_alpha", label: "Alpha", type: "number", step: 1 },
            { key: "lora_dropout", label: "Dropout", type: "number", step: 0.01 },
        ],
    },
    {
        key: "training",
        label: "Training",
        fields: [
            { key: "learning_rate", label: "Learning Rate", type: "number", step: "any" },
            { key: "batch_size", label: "Batch Size", type: "number", step: 1 },
            { key: "gradient_accumulation", label: "Grad Accumulation", type: "number", step: 1 },
            { key: "max_steps", label: "Max Steps", type: "number", step: 1 },
            { key: "warmup_steps", label: "Warmup Steps", type: "number", step: 1 },
            { key: "weight_decay", label: "Weight Decay", type: "number", step: 0.001 },
            { key: "max_grad_norm", label: "Max Grad Norm", type: "number", step: 0.1 },
        ],
    },
    {
        key: "gspo",
        label: "GSPO",
        fields: [
            { key: "gspo_beta", label: "Beta", type: "number", step: 0.01 },
            { key: "gspo_lambda", label: "Lambda", type: "number", step: 0.01 },
        ],
    },
    {
        key: "curriculum",
        label: "Curriculum",
        fields: [
            { key: "curriculum_stages", label: "Stages", type: "number", step: 1 },
        ],
    },
    {
        key: "hardware",
        label: "Hardware",
        fields: [
            { key: "gpu_count", label: "GPU Count", type: "number", step: 1 },
            { key: "precision", label: "Precision", type: "text" },
        ],
    },
];

export class StepConfiguration extends Component {
    static template = "rl_gym_dashboard.TrainingWizard.StepConfiguration";
    static props = {
        config: { type: Object },
        dataset: { type: [Object, { value: null }], optional: true },
        datasetSearch: { type: String },
        datasetResults: { type: Array },
        datasetInfo: { type: [Object, { value: null }], optional: true },
        splits: { type: Object },
        loadingDatasets: { type: Boolean },
        loadingInfo: { type: Boolean },
        sections: { type: Object },
        onConfigChange: { type: Function },
        onDatasetSearch: { type: Function },
        onDatasetSelect: { type: Function },
        onSplitChange: { type: Function },
        onToggleSection: { type: Function },
        onLoadDefaults: { type: Function },
    };

    setup() {
        this.searchTimeout = null;
    }

    get configSections() {
        return CONFIG_SECTIONS;
    }

    isSectionOpen(key) {
        return this.props.sections[key];
    }

    toggleSection(key) {
        this.props.onToggleSection(key);
    }

    getConfigValue(key) {
        return this.props.config[key];
    }

    onFieldChange(key, ev) {
        const val = ev.target.type === "number" ? parseFloat(ev.target.value) : ev.target.value;
        this.props.onConfigChange(key, val);
    }

    onSearchInput(ev) {
        const query = ev.target.value || "";
        clearTimeout(this.searchTimeout);
        this.searchTimeout = setTimeout(() => {
            this.props.onDatasetSearch(query);
        }, 400);
    }

    selectDataset(ds) {
        this.props.onDatasetSelect(ds);
    }

    onSplitInput(key, ev) {
        this.props.onSplitChange(key, parseFloat(ev.target.value));
    }
}
