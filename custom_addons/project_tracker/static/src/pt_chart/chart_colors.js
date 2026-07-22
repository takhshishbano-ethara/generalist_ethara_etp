/** @odoo-module */

// Shared chart palette for the Project Tracker dashboards (org + tasker), so the
// two never drift apart.
export const CHART_COLORS = {
    wip: "#8b7fd6", delivered: "#34c38f", failed: "#f46a6a",
    purple: "#8b7fd6", teal: "#50c8c0", pink: "#e78ab5", amber: "#f2b661",
    grid: "rgba(0,0,0,0.06)",
};

// One distinct colour per pipeline stage (keyed by the funnel bucket key).
export const PIPELINE_COLORS = {
    in_authoring: "#f2b661", in_trajectory: "#50c8c0", manual_qc_s1: "#e78ab5",
    ready_next_stage: "#8b7fd6", pass_it_k: "#2bb6a8", manual_qc_s2: "#d96aa0",
    verified: "#34c38f", failed: "#f46a6a",
};
