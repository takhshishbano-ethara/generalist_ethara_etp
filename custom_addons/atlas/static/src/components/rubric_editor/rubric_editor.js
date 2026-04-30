/** @odoo-module */
import { Component, useState, onMounted, onWillUnmount, onWillRender, markup } from "@odoo/owl";
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

const POLL_INTERVAL_MS = 3000;
const LOG_PREFIX = "[ATLAS:RUBRIC]";

function log(event, data) {
    if (data !== undefined) {
        console.log(`${LOG_PREFIX} ${event}`, data);
    } else {
        console.log(`${LOG_PREFIX} ${event}`);
    }
}

export class RubricEditor extends Component {
    static template = "atlas.RubricEditor";
    static props = { ...standardWidgetProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({
            criteria: [],
            loading: true,
            goalStatus: "idle",
            rubricStatus: "idle",
            initialized: false,
            inlineAdd: false,
            editingId: null,
            form: this._emptyForm(),
        });
        this._pollTimer = null;

        this._onGenerationStarted = () => {
            log("bus:GENERATION_STARTED received", { taskId: this.taskId });
            this.state.goalStatus = "running";
            this.state.rubricStatus = "running";
            this.state.criteria = [];
            this._startPolling();
        };
        this._onGenerationDone = async (ev) => {
            const payload = ev.detail || {};
            log("bus:GENERATION_DONE received", { taskId: this.taskId, payload });
            this.state.goalStatus = payload.goal_status || "done";
            this.state.rubricStatus = payload.rubric_status || "done";
            this._stopPolling();
            await this._loadCriteria();
            this.props.record.load();
        };

        this.env.bus.addEventListener("ATLAS:GENERATION_STARTED", this._onGenerationStarted);
        this.env.bus.addEventListener("ATLAS:GENERATION_DONE", this._onGenerationDone);

        onWillRender(() => {
            this._syncFromRecord();
        });

        onMounted(async () => {
            log("onMounted", { taskId: this.taskId });
            await this._loadCriteria();
            await this._fetchServerStatus();
            if (this.state.goalStatus === "running" || this.state.rubricStatus === "running") {
                log("onMounted -> status running, starting poll", {
                    goalStatus: this.state.goalStatus,
                    rubricStatus: this.state.rubricStatus,
                });
                this._startPolling();
            }
        });

        onWillUnmount(() => {
            log("onWillUnmount", { taskId: this.taskId });
            this._stopPolling();
            this.env.bus.removeEventListener("ATLAS:GENERATION_STARTED", this._onGenerationStarted);
            this.env.bus.removeEventListener("ATLAS:GENERATION_DONE", this._onGenerationDone);
        });
    }

    get taskId() {
        return this.props.record.resId;
    }

    get isGenerating() {
        return this.state.goalStatus === "running" || this.state.rubricStatus === "running";
    }

    _syncFromRecord() {
        const recordGoal = this.props.record.data.goal_generation_status;
        const recordRubric = this.props.record.data.rubric_generation_status;
        const isRecordRunning = recordGoal === "running" || recordRubric === "running";
        const isStateRunning = this.state.goalStatus === "running" || this.state.rubricStatus === "running";
        if (isRecordRunning && !isStateRunning) {
            log("_syncFromRecord: record running, state not -> syncing", {
                taskId: this.taskId,
                recordGoal,
                recordRubric,
                prevGoal: this.state.goalStatus,
                prevRubric: this.state.rubricStatus,
                criteriaCount: this.state.criteria.length,
            });
            this.state.goalStatus = recordGoal || this.state.goalStatus;
            this.state.rubricStatus = recordRubric || this.state.rubricStatus;
            this._startPolling();
            return;
        }
        if (this.state.goalStatus === "running" && recordGoal && recordGoal !== "running") {
            log("_syncFromRecord: state goal running but record done -> clearing", {
                taskId: this.taskId,
                recordGoal,
            });
            this.state.goalStatus = recordGoal;
        }
        if (this.state.rubricStatus === "running" && recordRubric && recordRubric !== "running") {
            log("_syncFromRecord: state rubric running but record done -> clearing", {
                taskId: this.taskId,
                recordRubric,
            });
            this.state.rubricStatus = recordRubric;
            this._loadCriteria();
        }
        if (this.state.goalStatus !== "running" && this.state.rubricStatus !== "running") {
            this._stopPolling();
        }
    }

    _emptyForm() {
        return {
            name: "",
            category: "other",
            custom_category: "",
            importance: "",
            is_negative: false,
            suggestion: "",
            levels: [
                { score: 0, label: "" },
                { score: 1, label: "" },
            ],
        };
    }

    async _fetchServerStatus() {
        if (!this.taskId) return;
        try {
            const res = await rpc("/atlas/generation/status", { task_id: this.taskId });
            log("_fetchServerStatus response", { taskId: this.taskId, res });
            if (!res || res.error) return;
            this.state.goalStatus = res.goal_generation_status || "idle";
            this.state.rubricStatus = res.rubric_generation_status || "idle";
        } catch (e) {
            console.warn(`${LOG_PREFIX} _fetchServerStatus failed`, { taskId: this.taskId, error: e });
        }
        this.state.initialized = true;
    }

    _startPolling() {
        this._stopPolling();
        log("_startPolling", { taskId: this.taskId, intervalMs: POLL_INTERVAL_MS });
        this._pollTimer = setInterval(() => this._pollGenerationStatus(), POLL_INTERVAL_MS);
    }

    _stopPolling() {
        if (this._pollTimer) {
            log("_stopPolling", { taskId: this.taskId });
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    async _pollGenerationStatus() {
        if (!this.taskId) return;
        try {
            const res = await rpc("/atlas/generation/status", { task_id: this.taskId });
            if (!res || res.error) {
                console.warn(`${LOG_PREFIX} poll: invalid response`, { taskId: this.taskId, res });
                return;
            }

            const goalStatus = res.goal_generation_status || "idle";
            const rubricStatus = res.rubric_generation_status || "idle";
            const prevGoal = this.state.goalStatus;
            const prevRubric = this.state.rubricStatus;

            this.state.goalStatus = goalStatus;
            this.state.rubricStatus = rubricStatus;

            if (prevGoal !== goalStatus || prevRubric !== rubricStatus) {
                log("poll: status changed", {
                    taskId: this.taskId,
                    goal: `${prevGoal} -> ${goalStatus}`,
                    rubric: `${prevRubric} -> ${rubricStatus}`,
                });
            }

            if (prevGoal === "running" && goalStatus !== "running") {
                log("poll: goal transitioned off running, reloading record", { taskId: this.taskId });
                this.props.record.load();
            }

            if (prevRubric === "running" && rubricStatus !== "running") {
                log("poll: rubric transitioned off running, reloading criteria", {
                    taskId: this.taskId,
                    newStatus: rubricStatus,
                });
                await this._loadCriteria();
                if (rubricStatus === "done") {
                    this.notification.add("Rubric criteria generated", { type: "success" });
                }
            }

            if (goalStatus !== "running" && rubricStatus !== "running") {
                this._stopPolling();
            }
        } catch (e) {
            console.warn(`${LOG_PREFIX} poll failed`, { taskId: this.taskId, error: e });
        }
    }

    async _loadCriteria() {
        if (!this.taskId) {
            log("_loadCriteria: no taskId, skipping");
            this.state.criteria = [];
            this.state.loading = false;
            return;
        }
        log("_loadCriteria: fetching", { taskId: this.taskId });
        try {
            const criteria = await this.orm.searchRead(
                "atlas.rubric.criterion",
                [["atlas_id", "=", this.taskId]],
                [
                    "id", "name", "category", "custom_category", "importance",
                    "is_negative", "suggestion", "sequence",
                    "qc_status", "qc_feedback", "qc_severity",
                ],
                { order: "sequence, id" },
            );
            log("_loadCriteria: criteria fetched", {
                taskId: this.taskId,
                count: criteria.length,
                ids: criteria.map((c) => c.id),
            });
            const criterionIds = criteria.map((c) => c.id);
            let allLevels = [];
            if (criterionIds.length) {
                allLevels = await this.orm.searchRead(
                    "atlas.rubric.level",
                    [["criterion_id", "in", criterionIds]],
                    ["id", "score", "label", "criterion_id"],
                    { order: "score, id" },
                );
                log("_loadCriteria: levels fetched", {
                    taskId: this.taskId,
                    levelCount: allLevels.length,
                });
            }
            const levelsByCriterion = {};
            for (const lv of allLevels) {
                const cId = lv.criterion_id[0];
                if (!levelsByCriterion[cId]) {
                    levelsByCriterion[cId] = [];
                }
                levelsByCriterion[cId].push(lv);
            }
            for (const c of criteria) {
                c.levels = levelsByCriterion[c.id] || [];
            }
            this.state.criteria = criteria;
            log("_loadCriteria: state updated", {
                taskId: this.taskId,
                stateCount: this.state.criteria.length,
            });
        } catch (e) {
            console.error(`${LOG_PREFIX} _loadCriteria failed`, { taskId: this.taskId, error: e });
            if (!this.isGenerating) {
                this.notification.add("Failed to load rubric criteria", { type: "danger" });
            }
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
            is_negative: c.is_negative || false,
            suggestion: c.suggestion || "",
            levels: (c.levels || []).map((l) => ({ ...l })),
        };
    }

    onCancelEdit() {
        this.state.editingId = null;
    }

    onAddLevel() {
        const levels = this.state.form.levels;
        const nextScore = levels.length > 0 ? Math.max(...levels.map((l) => l.score)) + 1 : 0;
        levels.push({ score: nextScore, label: "" });
    }

    onRemoveLevel(index) {
        if (this.state.form.levels.length <= 2) return;
        this.state.form.levels.splice(index, 1);
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
        } catch {
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

    qcStatusIcon(status) {
        const map = {
            running: "fa-spinner fa-spin",
            done: "fa-check-circle",
            error: "fa-exclamation-triangle",
        };
        return map[status] || "";
    }

    formatQcFeedback(text) {
        if (!text) return "";
        let html = text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
        html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
        html = html.replace(/^(\d+)\.\s+/gm, '<div class="o_qc_item"><span class="o_qc_num">$1.</span> ');
        html = html.replace(/(<div class="o_qc_item">.*?)(?=<div class="o_qc_item">|$)/gs, "$1</div>");
        html = html.replace(/\n/g, "<br/>");
        return markup(html);
    }
}

export const rubricEditorDef = { component: RubricEditor };
registry.category("view_widgets").add("atlas_rubric_editor", rubricEditorDef);
