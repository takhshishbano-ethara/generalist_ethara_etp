/** @odoo-module */
import { Component, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

export class GoalGenerationStatus extends Component {
    static template = "atlas.GoalGenerationStatus";
    static props = { ...standardWidgetProps };

    setup() {
        this._onGenerationStarted = () => {
            this._forceGenerating = true;
            this.render();
        };
        this._onGenerationDone = () => {
            this._forceGenerating = false;
            this.render();
        };

        this._forceGenerating = false;
        this.env.bus.addEventListener("ATLAS:GENERATION_STARTED", this._onGenerationStarted);
        this.env.bus.addEventListener("ATLAS:GENERATION_DONE", this._onGenerationDone);

        onWillUnmount(() => {
            this.env.bus.removeEventListener("ATLAS:GENERATION_STARTED", this._onGenerationStarted);
            this.env.bus.removeEventListener("ATLAS:GENERATION_DONE", this._onGenerationDone);
        });
    }

    get goalStatus() {
        return this.props.record.data.goal_generation_status || "idle";
    }

    get rubricStatus() {
        return this.props.record.data.rubric_generation_status || "idle";
    }

    get isGenerating() {
        if (this._forceGenerating) return true;
        return this.goalStatus === "running" || this.rubricStatus === "running";
    }

    get goalDone() {
        return this.goalStatus === "done" || this.goalStatus === "error";
    }

    get rubricDone() {
        return this.rubricStatus === "done" || this.rubricStatus === "error";
    }

    get statusLabel() {
        const gRunning = this._forceGenerating || this.goalStatus === "running";
        const rRunning = this._forceGenerating || this.rubricStatus === "running";
        if (gRunning && rRunning) {
            return "Generating goal & rubric criteria\u2026";
        }
        if (gRunning) {
            return "Generating goal description\u2026";
        }
        if (rRunning) {
            return "Generating rubric criteria\u2026";
        }
        return "";
    }
}

export const goalGenerationStatusDef = { component: GoalGenerationStatus };
registry.category("view_widgets").add("atlas_goal_status", goalGenerationStatusDef);
