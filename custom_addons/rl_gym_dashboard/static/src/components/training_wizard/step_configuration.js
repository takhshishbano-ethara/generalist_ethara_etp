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
        key: "policy",
        label: "Policy",
        fields: [
            { key: "policy_type", label: "Policy Type", type: "select", options: [
                { value: "gspo", label: "GSPO" },
                { value: "gtpo", label: "GTPO" },
            ]},
            { key: "gspo_group_size", label: "Group Size", type: "number", step: 1 },
            { key: "clip_low", label: "Clip Low", type: "number", step: 0.0001 },
            { key: "clip_high", label: "Clip High", type: "number", step: 0.0001 },
            { key: "gspo_kl_coeff", label: "KL Coefficient", type: "number", step: 0.001,
              showWhen: "gspo" },
            { key: "gtpo_gamma", label: "Discount Gamma", type: "number", step: 0.01,
              showWhen: "gtpo" },
            { key: "gtpo_ent_threshold", label: "Entropy Threshold", type: "number", step: 0.01,
              showWhen: "gtpo" },
            { key: "gtpo_ent_scale", label: "Entropy Scale", type: "number", step: 0.01,
              showWhen: "gtpo" },
        ],
    },
    {
        key: "curriculum",
        label: "Curriculum",
        fields: [
            { key: "curriculum_enabled", label: "Enabled", type: "checkbox" },
            { key: "curriculum_stages", label: "Phases", type: "number", step: 1 },
        ],
    },
    {
        key: "hardware",
        label: "Hardware",
        fields: [
            { key: "gpu_count", label: "GPU Count", type: "number", step: 1 },
            { key: "precision", label: "Precision", type: "select", options: [
                { value: "bf16", label: "BF16" },
                { value: "fp16", label: "FP16" },
                { value: "fp32", label: "FP32" },
            ]},
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
        rewardDescription: { type: String, optional: true },
        datasetConfigApplied: { type: Boolean, optional: true },
        onConfigChange: { type: Function },
        onDatasetSearch: { type: Function },
        onDatasetSelect: { type: Function },
        onSplitChange: { type: Function },
        onToggleSection: { type: Function },
        onLoadDefaults: { type: Function },
    };

    setup() {
        this.searchTimeout = null;
        this.state = useState({ showPreviewModal: false, previewPage: 0 });
    }

    get pageSize() { return 10; }

    get totalRows() {
        const preview = this.props.datasetInfo && this.props.datasetInfo.preview;
        if (!preview || !preview.rows) return 0;
        return preview.rows.length;
    }

    get totalPages() {
        return Math.max(1, Math.ceil(this.totalRows / this.pageSize));
    }

    get pagedRows() {
        const preview = this.props.datasetInfo && this.props.datasetInfo.preview;
        if (!preview || !preview.rows) return [];
        const start = this.state.previewPage * this.pageSize;
        return preview.rows.slice(start, start + this.pageSize);
    }

    openPreview() {
        this.state.previewPage = 0;
        this.state.showPreviewModal = true;
    }

    closePreview() {
        this.state.showPreviewModal = false;
    }

    prevPage() {
        if (this.state.previewPage > 0) this.state.previewPage--;
    }

    nextPage() {
        if (this.state.previewPage < this.totalPages - 1) this.state.previewPage++;
    }

    goToPage(page) {
        this.state.previewPage = Math.max(0, Math.min(page, this.totalPages - 1));
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

    isFieldVisible(field) {
        if (!field.showWhen) return true;
        return this.props.config.policy_type === field.showWhen;
    }

    onFieldChange(key, ev) {
        let val;
        if (ev.target.type === "checkbox") {
            val = ev.target.checked;
        } else if (ev.target.type === "number" || ev.target.tagName === "INPUT" && ev.target.step) {
            val = parseFloat(ev.target.value);
        } else {
            val = ev.target.value;
        }
        this.props.onConfigChange(key, val);
    }

    onSelectChange(key, ev) {
        this.props.onConfigChange(key, ev.target.value);
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
