/** @odoo-module **/
import { Component, useState } from "@odoo/owl";

export class StepInference extends Component {
    static template = "rl_gym_dashboard.TrainingWizard.StepInference";
    static props = {
        jobId: { type: [Number, { value: null }], optional: true },
        rpc: { type: Function },
    };

    setup() {
        this.state = useState({
            prompt: "",
            maxTokens: 256,
            temperature: 0.7,
            running: false,
            response: null,
            history: [],
        });
    }

    onPromptInput(ev) {
        this.state.prompt = ev.target.value;
    }

    onMaxTokensChange(ev) {
        this.state.maxTokens = parseInt(ev.target.value, 10);
    }

    onTemperatureChange(ev) {
        this.state.temperature = parseFloat(ev.target.value);
    }

    async runInference() {
        if (!this.state.prompt.trim() || this.state.running) return;
        this.state.running = true;
        this.state.response = null;

        try {
            const result = await this.props.rpc("/rl_gym/inference/run", {
                job_id: this.props.jobId,
                prompt: this.state.prompt,
                max_tokens: this.state.maxTokens,
                temperature: this.state.temperature,
            });
            this.state.response = result;
            this.state.history.unshift({
                prompt: this.state.prompt,
                response: result.response,
                tokens_used: result.tokens_used,
                model: result.model,
            });
        } catch (e) {
            this.state.response = {
                response: "Error: " + (e.message || "Inference failed"),
                tokens_used: 0,
                model: "—",
            };
        }
        this.state.running = false;
    }

    loadHistoryItem(item) {
        this.state.prompt = item.prompt;
        this.state.response = item;
    }

    clearHistory() {
        this.state.history = [];
    }
}
