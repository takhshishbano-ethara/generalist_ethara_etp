/** @odoo-module */
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { rpc } from "@web/core/network/rpc";

const CATEGORY_META = {
    factuality_hallucination: { label: "Factuality & Hallucination", icon: "fa-exclamation-triangle", color: "#F44336" },
    task_completion: { label: "Task Completion", icon: "fa-check-circle", color: "#4CAF50" },
    instruction_following: { label: "Instruction Following", icon: "fa-list-ol", color: "#2196F3" },
    communication_style: { label: "Communication Style", icon: "fa-comments", color: "#9C27B0" },
    other: { label: "Other", icon: "fa-tag", color: "#607D8B" },
};

const IMPORTANCE_COLORS = {
    critically_detrimental: "#D32F2F",
    detrimental: "#E57373",
    slightly_detrimental: "#EF9A9A",
    slightly_important: "#A5D6A7",
    important: "#66BB6A",
    critically_important: "#2E7D32",
};

const POLL_INTERVAL_MS = 4000;

export class RubricEditor extends Component {
    static template = "atlas.RubricEditor";
    static props = { ...standardWidgetProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({
            criteria: [],
            loading: true,
            generationStatus: "idle",
            inlineAdd: false,
            editingId: null,
            form: this._emptyForm(),
        });
        this._pollTimer = null;

        onMounted(() => {
            this._loadCriteria();
            this._startPolling();
        });

        onWillUnmount(() => {
            this._stopPolling();
        });
    }

    get taskId() {
        return this.props.record.resId;
    }

    _emptyForm() {
        return {
            name: "",
            category: "other",
            custom_category: "",
            importance: "",
            weight: 5,
            is_negative: false,
            suggestion: "",
            levels: [
                { score: 0, label: "" },
                { score: 1, label: "" },
            ],
        };
    }

    _startPolling() {
        this._stopPolling();
        this._pollTimer = setInterval(() => this._pollGenerationStatus(), POLL_INTERVAL_MS);
    }

    _stopPolling() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    async _pollGenerationStatus() {
        if (!this.taskId) return;
        try {
            const res = await rpc("/atlas/generation/status", { task_id: this.taskId });
            if (!res || res.error) return;

            const rubricStatus = res.rubric_generation_status || "idle";
            const prevStatus = this.state.generationStatus;
            this.state.generationStatus = rubricStatus;

            if (prevStatus === "running" && (rubricStatus === "done" || rubricStatus === "error")) {
                await this._loadCriteria();
                if (rubricStatus === "done") {
                    this.notification.add("Rubric criteria generated", { type: "success" });
                }
                this._stopPolling();
            }

            if (rubricStatus !== "running") {
                this._stopPolling();
            }
        } catch {
            // silent
        }
    }

    async _loadCriteria() {
        if (!this.taskId) {
            this.state.criteria = [];
            this.state.loading = false;
            return;
        }
        try {
            const criteria = await this.orm.searchRead(
                "atlas.rubric.criterion",
                [["atlas_id", "=", this.taskId]],
                [
                    "id", "name", "category", "custom_category", "importance",
                    "weight", "is_negative", "suggestion", "sequence",
                    "qc_status", "qc_feedback", "qc_severity",
                ],
                { order: "sequence, id" },
            );
            for (const c of criteria) {
                c.levels = await this.orm.searchRead(
                    "atlas.rubric.level",
                    [["criterion_id", "=", c.id]],
                    ["id", "score", "label"],
                    { order: "score, id" },
                );
            }
            this.state.criteria = criteria;
        } catch {
            this.notification.add("Failed to load rubric criteria", { type: "danger" });
        }
        this.state.loading = false;
    }

    categoryMeta(cat) {
        return CATEGORY_META[cat] || CATEGORY_META.other;
    }

    importanceColor(imp) {
        return IMPORTANCE_COLORS[imp] || "#9E9E9E";
    }

    importanceLabel(imp) {
        const map = {
            critically_detrimental: "Critically Detrimental",
            detrimental: "Detrimental",
            slightly_detrimental: "Slightly Detrimental",
            slightly_important: "Slightly Important",
            important: "Important",
            critically_important: "Critically Important",
        };
        return map[imp] || "—";
    }

    onAddCriteria() {
        this.state.editingId = null;
        this.state.form = this._emptyForm();
        this.state.inlineAdd = true;
    }

    onCancelInline() {
        this.state.inlineAdd = false;
        this.state.editingId = null;
    }

    onEditCriteria(criterionId) {
        const c = this.state.criteria.find((x) => x.id === criterionId);
        if (!c) return;
        this.state.editingId = criterionId;
        this.state.inlineAdd = false;
        this.state.form = {
            name: c.name || "",
            category: c.category || "other",
            custom_category: c.custom_category || "",
            importance: c.importance || "",
            weight: c.weight || 5,
            is_negative: c.is_negative || false,
            suggestion: c.suggestion || "",
            levels: (c.levels || []).map((l) => ({ ...l })),
        };
    }

    onCancelEdit() {
        this.state.editingId = null;
    }

    async onSaveCriteria() {
        const form = this.state.form;
        if (!form.name.trim()) {
            this.notification.add("Criteria description is required", { type: "warning" });
            return;
        }

        const vals = {
            name: form.name.trim(),
            category: form.category,
            custom_category: form.category === "other" ? form.custom_category.trim() : "",
            importance: form.importance || false,
            weight: parseInt(form.weight) || 5,
            is_negative: form.is_negative || false,
            suggestion: (form.suggestion || "").trim(),
        };

        try {
            if (this.state.editingId) {
                await this.orm.write("atlas.rubric.criterion", [this.state.editingId], vals);
                const existingLevels = await this.orm.searchRead(
                    "atlas.rubric.level",
                    [["criterion_id", "=", this.state.editingId]],
                    ["id"],
                );
                if (existingLevels.length) {
                    await this.orm.unlink("atlas.rubric.level", existingLevels.map((l) => l.id));
                }
                for (const level of form.levels) {
                    await this.orm.create("atlas.rubric.level", [{
                        criterion_id: this.state.editingId,
                        score: level.score,
                        label: level.label || "",
                    }]);
                }
                this.state.editingId = null;
            } else {
                const ids = await this.orm.create("atlas.rubric.criterion", [{
                    ...vals,
                    atlas_id: this.taskId,
                }]);
                const newId = ids[0];
                for (const level of form.levels) {
                    await this.orm.create("atlas.rubric.level", [{
                        criterion_id: newId,
                        score: level.score,
                        label: level.label || "",
                    }]);
                }
                this.state.inlineAdd = false;
            }

            await this._loadCriteria();
        } catch (e) {
            this.notification.add(
                e.data?.message || e.message || "Failed to save criteria",
                { type: "danger" },
            );
        }
    }

    async onDeleteCriteria(criterionId) {
        try {
            await this.orm.unlink("atlas.rubric.criterion", [criterionId]);
            await this._loadCriteria();
        } catch {
            this.notification.add("Failed to delete criteria", { type: "danger" });
        }
    }

    async onQcCheck(criterionId) {
        const c = this.state.criteria.find((x) => x.id === criterionId);
        if (!c) return;

        c.qc_status = "running";
        c.qc_feedback = false;
        c.qc_severity = false;

        try {
            const res = await rpc("/atlas/rubric/qc", { criterion_id: criterionId });
            if (res?.error) {
                c.qc_status = "error";
                c.qc_feedback = res.error;
                this.notification.add(res.error, { type: "danger" });
                return;
            }
            this._startQcPoll(criterionId);
        } catch (e) {
            c.qc_status = "error";
            c.qc_feedback = "Failed to start QC";
            this.notification.add("Failed to start QC check", { type: "danger" });
        }
    }

    _startQcPoll(criterionId) {
        const poll = async () => {
            try {
                const data = await this.orm.read(
                    "atlas.rubric.criterion",
                    [criterionId],
                    ["qc_status", "qc_feedback", "qc_severity"],
                );
                if (!data.length) return;
                const updated = data[0];
                const c = this.state.criteria.find((x) => x.id === criterionId);
                if (!c) return;

                c.qc_status = updated.qc_status;
                c.qc_feedback = updated.qc_feedback;
                c.qc_severity = updated.qc_severity;

                if (updated.qc_status === "running") {
                    setTimeout(poll, 3000);
                }
            } catch {
                // silent
            }
        };
        setTimeout(poll, 3000);
    }

    qcSeverityClass(severity) {
        const map = {
            low: "text-success",
            medium: "text-warning",
            high: "text-danger",
            critical: "text-danger fw-bold",
        };
        return map[severity] || "";
    }

    qcStatusIcon(status) {
        const map = {
            running: "fa-spinner fa-spin",
            done: "fa-check-circle",
            error: "fa-exclamation-triangle",
        };
        return map[status] || "";
    }
}

export const rubricEditorDef = { component: RubricEditor };
registry.category("view_widgets").add("atlas_rubric_editor", rubricEditorDef);
