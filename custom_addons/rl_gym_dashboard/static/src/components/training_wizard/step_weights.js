/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";

export class StepWeights extends Component {
    static template = "rl_gym_dashboard.TrainingWizard.StepWeights";
    static props = {
        jobId: { type: [Number, { value: null }], optional: true },
        jobName: { type: String, optional: true },
        rpc: { type: Function },
    };

    setup() {
        this.state = useState({
            weightId: null,
            weightName: this.props.jobName || "training-weights",
            fileSize: "~2.4 GB",
            s3Configured: true,
            uploading: false,
            s3Url: null,
            uploadError: null,
        });

        onMounted(async () => {
            await this._createWeight();
        });
    }

    async _createWeight() {
        try {
            const result = await this.props.rpc("/rl_gym/weights/create", {
                values: {
                    job_id: this.props.jobId,
                    name: this.state.weightName,
                    format: "safetensors",
                },
            });
            this.state.weightId = result.id;
            this.state.weightName = result.name || this.state.weightName;
        } catch (e) {
            console.error("Failed to create weight record:", e);
        }
    }

    async uploadWeights() {
        if (!this.state.weightId) return;
        this.state.uploading = true;
        this.state.uploadError = null;
        this.state.s3Url = null;

        try {
            const result = await this.props.rpc("/rl_gym/weights/upload", {
                weight_id: this.state.weightId,
            });
            if (result.error) {
                this.state.uploadError = result.error;
            } else {
                this.state.s3Url = result.s3_url;
            }
        } catch (e) {
            this.state.uploadError = e.message || "Upload failed. Please try again.";
        }
        this.state.uploading = false;
    }

    copyUrl() {
        if (this.state.s3Url) {
            navigator.clipboard.writeText(this.state.s3Url).catch(() => {});
        }
    }
}
