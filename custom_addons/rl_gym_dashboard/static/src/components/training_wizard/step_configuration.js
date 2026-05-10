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
            { key: "lora_a_init", label: "Init Method", type: "select", options: [
                { value: "xavier", label: "Xavier" },
                { value: "kaiming", label: "Kaiming" },
            ]},
            { key: "lora_exclude_modules", label: "Exclude Modules", type: "text" },
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
            { key: "clip_low", label: "Clip Low", type: "number", step: 0.01 },
            { key: "clip_high", label: "Clip High", type: "number", step: 0.01 },
            { key: "gspo_kl_coeff", label: "KL Coefficient", type: "number", step: 0.001 },
            { key: "gtpo_gamma", label: "Discount Gamma", type: "number", step: 0.01,
              showWhen: "gtpo" },
            { key: "gtpo_ent_threshold", label: "Entropy Threshold", type: "number", step: 0.01,
              showWhen: "gtpo" },
            { key: "gtpo_ent_scale", label: "Entropy Scale", type: "number", step: 0.01,
              showWhen: "gtpo" },
            { key: "dual_clip", label: "Dual Clip", type: "checkbox" },
            { key: "dual_clip_coef", label: "Dual Clip Coef", type: "number", step: 0.5 },
            { key: "norm_adv_by_std", label: "Normalize Advantages", type: "checkbox" },
            { key: "advantage_mode", label: "Advantage Mode", type: "select", options: [
                { value: "gtpo", label: "GTPO" },
                { value: "rloo", label: "RLOO" },
                { value: "step_wise", label: "Step-Wise" },
                { value: "hybrid", label: "Hybrid" },
            ]},
            { key: "prm_weight", label: "PRM Weight", type: "number", step: 0.05 },
            { key: "shaping_alpha", label: "Shaping Alpha", type: "number", step: 0.05 },
            { key: "min_lr_ratio", label: "Min LR Ratio", type: "number", step: 0.01 },
        ],
    },
    {
        key: "generation",
        label: "Generation / Rollout",
        fields: [
            { key: "temperature", label: "Temperature", type: "number", step: 0.1 },
            { key: "top_p", label: "Top P", type: "number", step: 0.05 },
            { key: "max_new_tokens", label: "Max New Tokens", type: "number", step: 256 },
        ],
    },
    {
        key: "reward",
        label: "Reward Shaping",
        fields: [
            { key: "outcome_pass", label: "Outcome Pass", type: "number", step: 0.1 },
            { key: "outcome_fail", label: "Outcome Fail", type: "number", step: 0.05 },
            { key: "outcome_empty", label: "Outcome Empty", type: "number", step: 0.05 },
            { key: "outcome_timeout", label: "Outcome Timeout", type: "number", step: 0.1 },
            { key: "length_penalty_weight", label: "Length Penalty", type: "number", step: 0.01 },
            { key: "partial_credit_enabled", label: "Partial Credit", type: "checkbox" },
            { key: "partial_credit_alpha", label: "Partial Credit \u03b1", type: "number", step: 0.1 },
            { key: "format_penalty_enabled", label: "Format Penalty", type: "checkbox" },
            { key: "format_penalty_value", label: "Format Penalty Value", type: "number", step: 0.05 },
            { key: "overlong_penalty", label: "Overlong Penalty", type: "checkbox" },
            { key: "overlong_penalty_threshold", label: "Overlong Threshold", type: "number", step: 1 },
        ],
    },
    {
        key: "monitoring",
        label: "Monitoring / Safety",
        fields: [
            { key: "checkpoint_every", label: "Checkpoint Every", type: "number", step: 5 },
            { key: "eval_every", label: "Eval Every", type: "number", step: 5 },
            { key: "echo_trap_threshold", label: "Echo Trap Threshold", type: "number", step: 0.005 },
            { key: "echo_trap_window", label: "Echo Trap Window", type: "number", step: 5 },
            { key: "grad_explosion_threshold", label: "Grad Explosion", type: "number", step: 10 },
            { key: "dead_training_window", label: "Dead Training Window", type: "number", step: 5 },
        ],
    },
    {
        key: "curriculum",
        label: "Curriculum",
        fields: [
            { key: "curriculum_enabled", label: "Enabled", type: "checkbox" },
            { key: "curriculum_stages", label: "Phases", type: "number", step: 1 },
            { key: "advance_threshold", label: "Advance Threshold", type: "number", step: 0.05 },
            { key: "advance_window", label: "Advance Window", type: "number", step: 1 },
            { key: "phase_max_turns", label: "Phase Max Turns", type: "text" },
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
            { key: "tp_size", label: "Tensor Parallel", type: "number", step: 1 },
            { key: "max_model_len", label: "Max Model Length", type: "number", step: 1024 },
            { key: "docker_containers", label: "Docker Containers", type: "number", step: 1 },
            { key: "docker_timeout", label: "Docker Timeout (s)", type: "number", step: 60 },
            { key: "vllm_gpus", label: "vLLM GPUs", type: "number", step: 1 },
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
