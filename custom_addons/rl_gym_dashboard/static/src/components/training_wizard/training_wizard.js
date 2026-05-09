/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { StepModelSelection } from "./step_model_selection";
import { StepConfiguration } from "./step_configuration";
import { StepTraining } from "./step_training";
import { StepMetrics } from "./step_metrics";
import { StepWeights } from "./step_weights";
import { StepInference } from "./step_inference";

class TrainingWizard extends Component {
    static template = "rl_gym_dashboard.TrainingWizard";
    static components = {
        StepModelSelection,
        StepConfiguration,
        StepTraining,
        StepMetrics,
        StepWeights,
        StepInference,
    };
    static props = ["*"];

    setup() {
        this.rpc = rpc;

        this.stepsMeta = [
            { index: 0, title: "Model" },
            { index: 1, title: "Config" },
            { index: 2, title: "Training" },
            { index: 3, title: "Metrics" },
            { index: 4, title: "Weights" },
            { index: 5, title: "Inference" },
        ];

        this.state = useState({
            currentStep: 0,
            models: [],
            loadingModels: false,
            datasetSearch: "",
            datasetResults: [],
            datasetInfo: null,
            loadingDatasets: false,
            loadingInfo: false,
            rewardDescription: "",
            datasetConfigApplied: false,
            configSections: {
                lora: true,
                training: true,
                policy: false,
                curriculum: false,
                hardware: false,
            },
            trainingState: {
                jobId: null,
                status: "idle",
            },
            wizardData: {
                selectedModelId: null,
                customName: "",
                config: {
                    policy_type: "gspo",
                    lora_rank: 64,
                    lora_alpha: 128,
                    lora_dropout: 0.05,
                    learning_rate: 3e-6,
                    batch_size: 64,
                    gradient_accumulation: 4,
                    max_steps: 450,
                    warmup_steps: 50,
                    weight_decay: 0.01,
                    max_grad_norm: 1.0,
                    gspo_group_size: 8,
                    gspo_kl_coeff: 0.01,
                    clip_low: 0.2,
                    clip_high: 0.28,
                    gtpo_gamma: 0.9,
                    gtpo_ent_threshold: 0.7,
                    gtpo_ent_scale: 0.1,
                    curriculum_enabled: true,
                    curriculum_stages: 4,
                    gpu_count: 8,
                    precision: "bf16",
                },
                dataset: null,
                splits: { train: 0.8, val: 0.1, test: 0.1 },
                configId: null,
                jobId: null,
            },
        });

        onWillStart(async () => {
            await this._loadModels();
        });
    }

    async _loadModels() {
        this.state.loadingModels = true;
        try {
            const result = await this.rpc("/rl_gym/models", {});
            this.state.models = result || [];
        } catch (e) {
            console.error("Failed to load models:", e);
            this.state.models = [];
        }
        this.state.loadingModels = false;
    }

    get canProceed() {
        switch (this.state.currentStep) {
            case 0:
                return this.state.wizardData.selectedModelId && this.state.wizardData.customName.trim();
            case 1:
                return true;
            case 2:
                return this.state.trainingState.status === "completed";
            default:
                return true;
        }
    }

    onSelectModel(modelId) {
        this.state.wizardData.selectedModelId = modelId;
        this._loadModelDefaults(modelId);
    }

    onNameChange(name) {
        this.state.wizardData.customName = name;
    }

    onConfigChange(key, value) {
        this.state.wizardData.config[key] = value;
    }

    async onDatasetSearch(query) {
        this.state.datasetSearch = query || "";
        this.state.loadingDatasets = true;
        try {
            const result = await this.rpc("/rl_gym/datasets/search", {
                query: query || "",
                author: "ethara",
            });
            if (Array.isArray(result)) {
                this.state.datasetResults = result;
            } else if (result && result.error) {
                console.error("Dataset search error:", result.error);
                this.state.datasetResults = [];
            } else {
                this.state.datasetResults = [];
            }
        } catch (e) {
            console.error("Dataset search failed:", e);
            this.state.datasetResults = [];
        }
        this.state.loadingDatasets = false;
    }

    async onDatasetSelect(dataset) {
        this.state.wizardData.dataset = dataset;
        this.state.datasetInfo = null;
        this.state.loadingInfo = true;
        this.state.rewardDescription = "";
        this.state.datasetConfigApplied = false;
        try {
            const info = await this.rpc("/rl_gym/datasets/info", { repo_id: dataset.id });
            this.state.datasetInfo = info;
        } catch (e) {
            console.error("Dataset info failed:", e);
            this.state.datasetInfo = null;
        }
        this.state.loadingInfo = false;
        this._loadDatasetDefaults(dataset.id);
    }

    async _loadDatasetDefaults(repoId) {
        try {
            const defaults = await this.rpc("/rl_gym/config/dataset_defaults", { repo_id: repoId });
            if (defaults && typeof defaults === "object" && Object.keys(defaults).length > 0) {
                const reward = defaults.reward_description || "";
                Object.keys(defaults).forEach((k) => {
                    if (k === "reward_description") return;
                    if (k in this.state.wizardData.config) {
                        this.state.wizardData.config[k] = defaults[k];
                    }
                });
                this.state.rewardDescription = reward;
                this.state.datasetConfigApplied = true;
                setTimeout(() => {
                    this.state.datasetConfigApplied = false;
                }, 3000);
            }
        } catch (e) {
            console.error("Load dataset defaults failed:", e);
        }
    }

    onSplitChange(key, value) {
        this.state.wizardData.splits[key] = value;
    }

    onToggleSection(key) {
        this.state.configSections[key] = !this.state.configSections[key];
    }

    async onLoadDefaults() {
        if (!this.state.wizardData.selectedModelId) return;
        await this._loadModelDefaults(this.state.wizardData.selectedModelId);
    }

    async _loadModelDefaults(modelId) {
        try {
            const defaults = await this.rpc("/rl_gym/config/defaults", { model_id: modelId });
            if (defaults && typeof defaults === "object" && !Array.isArray(defaults)) {
                Object.keys(defaults).forEach((k) => {
                    if (k in this.state.wizardData.config) {
                        this.state.wizardData.config[k] = defaults[k];
                    }
                });
            }
        } catch (e) {
            console.error("Load defaults failed:", e);
        }
    }

    onBack() {
        if (this.state.currentStep > 0) {
            this.state.currentStep--;
        }
    }

    async onNext() {
        if (this.state.currentStep === 0 && this.state.datasetResults.length === 0) {
            this.onDatasetSearch("");
        }
        if (this.state.currentStep === 1) {
            try {
                if (this.state.wizardData.dataset) {
                    await this.rpc("/rl_gym/datasets/save", {
                        values: {
                            hf_repo_id: this.state.wizardData.dataset.id,
                            name: this.state.wizardData.dataset.name || this.state.wizardData.dataset.id.split('/').pop(),
                        },
                    });
                }
                const configResult = await this.rpc("/rl_gym/config/save", {
                    values: this.state.wizardData.config,
                    model_id: this.state.wizardData.selectedModelId,
                    job_name: this.state.wizardData.customName,
                });
                this.state.wizardData.configId = configResult.id;
            } catch (e) {
                console.error("Save config/dataset failed:", e);
            }
        }
        if (this.state.currentStep < 5) {
            this.state.currentStep++;
        }
    }

    onFinish() {
        window.history.back();
    }
}

registry.category("actions").add("rl_gym_dashboard.training_wizard", TrainingWizard);
