/** @odoo-module */
import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";

// Each stage's pipeline, in order. `done`/`failed` read the SAME fields the status
// ladder does (kensei2_tracker_allocation._compute_status), so the stepper can never
// disagree with the status bar. `reason` names the field that explains a failure.
// Kept as a pure description so computeSteps() is unit-testable without a DOM.
const STAGE_1_STEPS = [
    { key: "tasker_qc", label: "Tasker QC",
      done: (d) => !!d.drive_link,
      action: "Add the Drive Link to submit for verification." },
    { key: "pl_verification", label: "PL Verification",
      done: (d) => d.pl_verified_status === "done",
      failed: (d) => d.pl_verified_status === "failed", reason: "pl_verified_reason",
      action: "PL verifies the authored task." },
    { key: "ready_baseline", label: "Ready for Baseline",
      done: (d) => d.baseline_ready_status === "done",
      failed: (d) => d.baseline_ready_status === "failed", reason: "baseline_ready_reason",
      action: "Sign off that the task is ready for its baseline." },
    { key: "baseline", label: "Baseline",
      done: (d) => d.baseline_gen_status === "done" && !!d.qced_by
          && d.rubric_score > 0 && d.rubric_score <= 100
          && d.pytest_score > 0 && d.pytest_score <= 100
          && d.overall_score > 0 && d.overall_score <= 100,
      failed: (d) => d.baseline_gen_status === "failed", reason: "baseline_gen_reason",
      action: "Generate the baseline trajectory, then score and QC it." },
    { key: "manual_qc", label: "Manual QC",
      done: (d) => d.manual_qc_status === "done",
      failed: (d) => d.manual_qc_status === "failed", reason: "manual_qc_reason",
      action: "Run manual QC on the trajectory." },
    { key: "next_stage", label: "Ready for Next Stage",
      done: (d) => d.status === "ready_next_stage" || d.status === "deliverable",
      action: "Hand off to Stage 2." },
];

const STAGE_2_STEPS = [
    { key: "pass_it_k", label: "Pass @ K",
      done: (d) => d.baseline_gen_status === "done" && !!d.qced_by
          && d.rubric_score > 0 && d.rubric_score <= 100
          && d.pytest_score > 0 && d.pytest_score <= 100
          && d.overall_score > 0 && d.overall_score <= 100,
      failed: (d) => d.baseline_gen_status === "failed", reason: "baseline_gen_reason",
      action: "Generate the Pass @ K trajectory, then score and QC it." },
    { key: "manual_qc", label: "Manual QC",
      done: (d) => d.manual_qc_status === "done",
      failed: (d) => d.manual_qc_status === "failed", reason: "manual_qc_reason",
      action: "Run manual QC on the trajectory." },
    { key: "deliverable", label: "Deliverable",
      done: (d) => d.status === "deliverable",
      action: "Task delivered." },
];

/**
 * Resolve every step to exactly one state: done / current / failed / locked.
 *
 * Walk the pipeline in order. The FIRST step that failed stops the walk (its
 * predecessors are done, everything after is locked). Otherwise the first
 * not-yet-done step is the current one, and the rest are locked. `data` is a plain
 * map of field values, so this is pure and testable.
 */
export function computeSteps(data, stageNo) {
    const steps = stageNo >= 2 ? STAGE_2_STEPS : STAGE_1_STEPS;
    const out = [];
    let resolved = false; // once we hit the failed or current step, the rest lock
    for (const step of steps) {
        let state;
        let detail = "";
        if (resolved) {
            state = "locked";
        } else if (step.failed && step.failed(data)) {
            state = "failed";
            detail = (step.reason && data[step.reason]) || "Failed at this step.";
            resolved = true;
        } else if (step.done(data)) {
            state = "done";
        } else {
            state = "current";
            detail = step.action || "";
            resolved = true;
        }
        out.push({ key: step.key, label: step.label, state, detail });
    }
    return out;
}

export class Kensei2TrackerStepper extends Component {
    static template = "kensei2.TrackerStepper";
    static props = { "*": true };

    get stageNo() {
        return this.props.record.data.stage_no || 1;
    }

    get steps() {
        return computeSteps(this.props.record.data, this.stageNo);
    }

    // The step the task is sitting on, for the header line.
    get currentStep() {
        return this.steps.find((s) => s.state === "current" || s.state === "failed");
    }

    get daysInStatus() {
        return this.props.record.data.days_in_status || 0;
    }

    get ageLabel() {
        const n = this.daysInStatus;
        if (n <= 0) {
            return "today";
        }
        return n === 1 ? "1 day" : `${n} days`;
    }

    // Draws attention once a task has sat in one step too long — the aging signal.
    get isStale() {
        return this.daysInStatus >= 7 && !!this.currentStep;
    }

    iconFor(state) {
        return {
            done: "fa-check-circle",
            current: "fa-dot-circle-o",
            failed: "fa-times-circle",
            locked: "fa-circle-o",
        }[state] || "fa-circle-o";
    }
}

registry.category("view_widgets").add("kensei2_tracker_stepper", {
    component: Kensei2TrackerStepper,
});
