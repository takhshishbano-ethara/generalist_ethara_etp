/** @odoo-module */

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

export class EtpPromptGenerator extends Component {
    static template = "etp_assessment.PromptGenerator";
    static props = ["*"];

    setup() {
        this.notification = useService("notification");
        this.state = useState({
            promptId: null,
            title: "New Prompt",
            sourceText: "",
            categoryId: 0,
            categories: [],
            skills: [],            // [{id,name,description,max_questions}]
            questions: [],         // [{id,skill,name,question_prompt,question_type,state}]
            loadingSkills: false,
            loadingQuestions: false,
            showSystemPrompts: false,
            sysSkills: "",
            sysQuestions: "",
            phase: "input",        // input | skills | questions
        });

        onWillStart(async () => {
            this.state.categories = await rpc("/etp_assessment/prompt/categories", {});
            const sp = await rpc("/etp_assessment/prompt/system_prompts", {});
            this.state.sysSkills = sp.skills;
            this.state.sysQuestions = sp.questions;
        });
    }

    get questionsBySkill() {
        const groups = {};
        for (const q of this.state.questions) {
            (groups[q.skill] = groups[q.skill] || []).push(q);
        }
        return Object.entries(groups).map(([skill, items]) => ({ skill, items }));
    }

    async _ensurePrompt() {
        if (this.state.promptId) return this.state.promptId;
        const res = await rpc("/etp_assessment/prompt/create", {
            title: this.state.title,
            source_text: this.state.sourceText,
            category_id: this.state.categoryId || false,
        });
        this.state.promptId = res.id;
        return res.id;
    }

    // ---- CALL 1: extract skills ----
    async onExtractSkills() {
        if (!this.state.sourceText.trim()) {
            this.notification.add("Paste some source text first.", { type: "warning" });
            return;
        }
        this.state.loadingSkills = true;
        this.state.skills = [];
        try {
            const pid = await this._ensurePrompt();
            const res = await rpc("/etp_assessment/prompt/extract_skills", {
                prompt_id: pid,
                source_text: this.state.sourceText,
                title: this.state.title,
                category_id: this.state.categoryId || false,
            });
            // staggered reveal so it looks like it streams in
            this.state.phase = "skills";
            for (const s of res.skills) {
                this.state.skills.push(s);
                await new Promise((r) => setTimeout(r, 120));
            }
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger", sticky: true });
        } finally {
            this.state.loadingSkills = false;
        }
    }

    // ---- CALL 2: generate ALL questions ----
    async onGenerateQuestions() {
        if (!this.state.skills.length) return;
        this.state.loadingQuestions = true;
        this.state.questions = [];
        try {
            // persist any edited max_questions first
            await rpc("/etp_assessment/prompt/save_skills", {
                skills: this.state.skills.map((s) => ({
                    id: s.id, max_questions: s.max_questions,
                })),
            });
            const res = await rpc("/etp_assessment/prompt/generate", {
                prompt_id: this.state.promptId,
            });
            this.state.phase = "questions";
            // staggered render, grouped by skill, looks progressive
            for (const q of res.questions) {
                this.state.questions.push(q);
                await new Promise((r) => setTimeout(r, 60));
            }
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger", sticky: true });
        } finally {
            this.state.loadingQuestions = false;
        }
    }

    async onDecision(q, approve) {
        try {
            const res = await rpc("/etp_assessment/prompt/decision", {
                question_id: q.id, approve,
            });
            q.state = res.state;
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger" });
        }
    }

    async onSaveSystemPrompt(which) {
        const value = which === "skills" ? this.state.sysSkills : this.state.sysQuestions;
        await rpc("/etp_assessment/prompt/save_system_prompt", { which, value });
        this.notification.add("System prompt saved.", { type: "success" });
    }

    get approvedCount() {
        return this.state.questions.filter((q) => q.state === "approved").length;
    }

    _err(e) {
        return (e && e.data && e.data.message) || (e && e.message) || "Something went wrong";
    }
}

registry.category("actions").add("etp_assessment.prompt_generator", EtpPromptGenerator);
