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
            rubricQcStatus: "idle",
            rubricQcSeverity: false,
            rubricQcFeedback: "",
            rubricQcExpanded: false,
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
        const recordQcAll = this.props.record.data.rubric_qc_all_status;
        const recordQcAllSeverity = this.props.record.data.rubric_qc_all_severity;
        const recordQcAllFeedback = this.props.record.data.rubric_qc_all_feedback;
        if (recordQcAll && this.state.rubricQcStatus !== "running") {
            this.state.rubricQcStatus = recordQcAll === "pending" ? "idle" : recordQcAll;
            this.state.rubricQcSeverity = recordQcAllSeverity || false;
            this.state.rubricQcFeedback = recordQcAllFeedback || "";
        }
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

    async onQcAll() {
        if (!this.taskId) return;
        if (this.state.rubricQcStatus === "running") return;
        if (!this.state.criteria.length) {
            this.notification.add("No criteria to evaluate", { type: "warning" });
            return;
        }

        this.state.rubricQcStatus = "running";
        this.state.rubricQcSeverity = false;
        this.state.rubricQcFeedback = "";
        this.state.rubricQcExpanded = true;

        try {
            const res = await rpc("/atlas/rubric/qc_all", { task_id: this.taskId });
            if (res?.error) {
                this.state.rubricQcStatus = "error";
                this.state.rubricQcFeedback = res.error;
                this.notification.add(res.error, { type: "danger" });
                return;
            }
            this._startRubricQcAllPoll();
        } catch {
            this.state.rubricQcStatus = "error";
            this.state.rubricQcFeedback = "Failed to start rubric QC";
            this.notification.add("Failed to start rubric QC", { type: "danger" });
        }
    }

    _startRubricQcAllPoll() {
        const poll = async () => {
            if (!this.taskId) return;
            try {
                const data = await this.orm.read(
                    "atlas.atlas",
                    [this.taskId],
                    ["rubric_qc_all_status", "rubric_qc_all_severity", "rubric_qc_all_feedback"],
                );
                if (!data.length) return;
                const updated = data[0];
                this.state.rubricQcStatus = updated.rubric_qc_all_status || "idle";
                this.state.rubricQcSeverity = updated.rubric_qc_all_severity || false;
                this.state.rubricQcFeedback = updated.rubric_qc_all_feedback || "";

                if (this.state.rubricQcStatus === "running") {
                    setTimeout(poll, 3000);
                } else if (this.state.rubricQcStatus === "done") {
                    this.notification.add("Rubric QC complete", { type: "success" });
                }
            } catch {
                // silent
            }
        };
        setTimeout(poll, 3000);
    }

    toggleRubricQcDetails() {
        this.state.rubricQcExpanded = !this.state.rubricQcExpanded;
    }

    get rubricQcSeverityColor() {
        const m = { low: "#198754", medium: "#ffc107", high: "#fd7e14", critical: "#dc3545" };
        return m[this.state.rubricQcSeverity] || "#6c757d";
    }

    qcStatusIcon(status) {
        const map = {
            running: "fa-spinner fa-spin",
            done: "fa-check-circle",
            error: "fa-exclamation-triangle",
        };
        return map[status] || "";
    }

    // Render the holistic "QC All Criteria" report returned by the LLM.
    // Supported CommonMark subset: leading "## Rubric QC Verdict: PASS|FAIL" banner,
    // "### Section" headings, pipe-tables (header + `---` separator + rows, arbitrary
    // column count, PASS/FAIL pills on "Result" column, ✅/❌ on "+/-" column),
    // ordered lists (`^\d+\. `), indented (2+ space) monospace scoring blocks,
    // `**bold**` inline, and plain paragraphs. Missing sections degrade gracefully;
    // plain-string inputs (error state) render as a single paragraph.
    formatQcFeedback(text) {
        if (!text) return "";
        const esc = (s) => String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
        const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

        const raw = String(text).replace(/\r\n?/g, "\n");
        const lines = raw.split("\n");
        const out = [];

        let i = 0;
        while (i < lines.length && lines[i].trim() === "") i++;
        const verdictRe = /^##\s*\*{0,2}\s*Rubric QC Verdict\s*:\s*\*{0,2}\s*(PASS|FAIL)\s*\*{0,2}\s*$/i;
        if (i < lines.length && verdictRe.test(lines[i].trim())) {
            const m = lines[i].trim().match(verdictRe);
            const verdict = m[1].toUpperCase();
            const cls = verdict === "PASS" ? "o_rqc_verdict_pass" : "o_rqc_verdict_fail";
            const icon = verdict === "PASS" ? "fa-check-circle" : "fa-exclamation-circle";
            out.push(
                `<div class="o_rqc_verdict ${cls}">` +
                `<i class="fa ${icon}"></i>` +
                `<span>Rubric QC Verdict: ${verdict}</span>` +
                `</div>`
            );
            i++;
        }

        const isSectionHeading = (ln) => /^###\s+(.+?)\s*$/.exec(ln.trim());
        const isTableHeader = (ln) => /^\|.*\|\s*$/.test(ln);
        const isTableSep = (ln) => /^\|[\s\-:|]+\|\s*$/.test(ln);
        const isOlItem = (ln) => /^\d+\.\s+/.test(ln);
        const isIndented = (ln) => /^[ \t]{2,}\S/.test(ln);

        const renderTable = (headers, rows) => {
            const headLc = headers.map((h) => h.trim().toLowerCase());
            const resultIdx = headLc.findIndex((h) => h === "result");
            const signIdx = headLc.findIndex((h) => h === "+/-" || h === "+/−");
            const checkIdx = headLc.findIndex((h) => h === "check");
            // Tag each <th> with a column class so SCSS can size specific
            // columns (Check wide, Result narrow) regardless of column order.
            const th = headers
                .map((h, idx) => {
                    const cls =
                        idx === checkIdx
                            ? ' class="o_rqc_col_check"'
                            : idx === resultIdx
                            ? ' class="o_rqc_col_result"'
                            : idx === signIdx
                            ? ' class="o_rqc_col_sign"'
                            : "";
                    return `<th${cls}>${inline(h.trim())}</th>`;
                })
                .join("");
            const body = rows
                .map((cells) => {
                    const tds = cells.map((c, idx) => {
                        const val = c.trim();
                        if (idx === resultIdx) {
                            const norm = val
                                .replace(/[*_`]/g, "")
                                .replace(/[^A-Za-z]/g, "")
                                .toUpperCase();
                            if (norm === "PASS") {
                                return `<td class="o_rqc_col_result"><span class="o_rqc_pass_pill">PASS</span></td>`;
                            }
                            if (norm === "FAIL") {
                                return `<td class="o_rqc_col_result"><span class="o_rqc_fail_pill">FAIL</span></td>`;
                            }
                            return `<td class="o_rqc_col_result">${inline(val)}</td>`;
                        }
                        if (idx === signIdx) {
                            return `<td class="o_rqc_sign_cell o_rqc_col_sign">${inline(val)}</td>`;
                        }
                        if (idx === checkIdx) {
                            return `<td class="o_rqc_col_check">${inline(val)}</td>`;
                        }
                        return `<td>${inline(val)}</td>`;
                    });
                    while (tds.length < headers.length) {
                        tds.push("<td></td>");
                    }
                    return `<tr>${tds.join("")}</tr>`;
                })
                .join("");
            return `<table class="o_rqc_table"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`;
        };

        const splitPipe = (ln) => {
            let s = ln.trim();
            if (s.startsWith("|")) s = s.slice(1);
            if (s.endsWith("|")) s = s.slice(0, -1);
            return s.split("|");
        };

        const renderBody = (bodyLines, sectionName) => {
            const pieces = [];
            let j = 0;
            const N = bodyLines.length;
            while (j < N) {
                const ln = bodyLines[j];
                if (ln.trim() === "") { j++; continue; }

                if (isTableHeader(ln) && j + 1 < N && isTableSep(bodyLines[j + 1])) {
                    const headers = splitPipe(ln);
                    j += 2;
                    const rows = [];
                    while (j < N && isTableHeader(bodyLines[j]) && !isTableSep(bodyLines[j])) {
                        rows.push(splitPipe(bodyLines[j]));
                        j++;
                    }
                    pieces.push(renderTable(headers, rows));
                    continue;
                }

                if (isOlItem(ln)) {
                    const items = [];
                    while (j < N) {
                        const cur = bodyLines[j];
                        if (!isOlItem(cur)) break;
                        const m = /^(\d+)\.\s+(.*)$/.exec(cur);
                        let numStr = m[1];
                        let content = m[2];
                        j++;
                        while (
                            j < N &&
                            bodyLines[j].trim() !== "" &&
                            !isOlItem(bodyLines[j]) &&
                            !isTableHeader(bodyLines[j]) &&
                            !isSectionHeading(bodyLines[j])
                        ) {
                            content += " " + bodyLines[j].trim();
                            j++;
                        }
                        items.push(
                            `<li class="o_rqc_issue"><span class="o_rqc_issue_num">${esc(numStr)}.</span> ${inline(content)}</li>`
                        );
                    }
                    pieces.push(`<ol class="o_rqc_issues">${items.join("")}</ol>`);
                    continue;
                }

                if (isIndented(ln)) {
                    const buf = [];
                    while (j < N && (isIndented(bodyLines[j]) || (bodyLines[j].trim() === "" && j + 1 < N && isIndented(bodyLines[j + 1])))) {
                        buf.push(bodyLines[j]);
                        j++;
                    }
                    pieces.push(`<pre class="o_rqc_scoring">${esc(buf.join("\n"))}</pre>`);
                    continue;
                }

                const para = [];
                while (
                    j < N &&
                    bodyLines[j].trim() !== "" &&
                    !isTableHeader(bodyLines[j]) &&
                    !isOlItem(bodyLines[j]) &&
                    !isIndented(bodyLines[j]) &&
                    !isSectionHeading(bodyLines[j])
                ) {
                    para.push(bodyLines[j].trim());
                    j++;
                }
                if (para.length) {
                    const txt = para.join(" ");
                    const sectLc = (sectionName || "").toLowerCase();
                    if (sectLc.includes("weakest passing")) {
                        pieces.push(`<div class="o_rqc_callout">${inline(txt)}</div>`);
                    } else {
                        pieces.push(`<p>${inline(txt)}</p>`);
                    }
                }
            }
            return pieces.join("");
        };

        let firstSection = -1;
        for (let k = i; k < lines.length; k++) {
            if (isSectionHeading(lines[k])) { firstSection = k; break; }
        }

        if (firstSection === -1) {
            const body = renderBody(lines.slice(i), "");
            if (body) out.push(body);
        } else {
            if (firstSection > i) {
                const lead = renderBody(lines.slice(i, firstSection), "");
                if (lead) out.push(lead);
            }
            let cur = firstSection;
            while (cur < lines.length) {
                const m = isSectionHeading(lines[cur]);
                if (!m) { cur++; continue; }
                const sectionName = m[1].trim();
                let end = lines.length;
                for (let k = cur + 1; k < lines.length; k++) {
                    if (isSectionHeading(lines[k])) { end = k; break; }
                }
                const body = renderBody(lines.slice(cur + 1, end), sectionName);
                out.push(
                    `<div class="o_rqc_section">` +
                    `<div class="o_rqc_section_label">${inline(sectionName)}</div>` +
                    body +
                    `</div>`
                );
                cur = end;
            }
        }

        return markup(out.join(""));
    }
}

export const rubricEditorDef = { component: RubricEditor };
registry.category("view_widgets").add("atlas_rubric_editor", rubricEditorDef);
