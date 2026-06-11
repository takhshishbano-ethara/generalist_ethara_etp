/** @odoo-module */

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart, useRef } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

export class EtpPromptGenerator extends Component {
    static template = "etp_assessment.PromptGenerator";
    static props = ["*"];

    setup() {
        this.notification = useService("notification");
        this.fileInput = useRef("fileInput");
        this.state = useState({
            promptId: null,
            title: "New Prompt",
            notes: "",
            goldenExample: "",
            maxQuestions: 0,
            categoryId: 0,
            categories: [],
            resources: [],         // [{id,name,char_count,extraction_error}]
            questions: [],         // [{id,name,question_prompt,question_type,state,image_state,...}]
            uploading: false,
            loadingQuestions: false,
            loadingImages: false,
            showSystemPrompts: false,
            sysSeed: "",
            sysScoring: "",
        });

        onWillStart(async () => {
            this.state.categories = await rpc("/etp_assessment/prompt/categories", {});
            const sp = await rpc("/etp_assessment/prompt/system_prompts", {});
            this.state.sysSeed = sp.seed || "";
            this.state.sysScoring = sp.scoring || "";
        });
    }

    get approvedCount() {
        return this.state.questions.filter((q) => q.state === "approved").length;
    }
    get pendingImgCount() {
        return this.state.questions.filter(
            (q) => q.question_type === "image_comparison" &&
                   q.state === "draft" && q.image_state !== "generated").length;
    }
    get totalChars() {
        return this.state.resources.reduce((n, r) => n + (r.char_count || 0), 0);
    }

    async _ensurePrompt() {
        if (this.state.promptId) return this.state.promptId;
        const res = await rpc("/etp_assessment/prompt/create", {
            title: this.state.title,
            category_id: this.state.categoryId || false,
        });
        this.state.promptId = res.id;
        return res.id;
    }

    // ---- resource upload ----
    onPickFiles() {
        this.fileInput.el && this.fileInput.el.click();
    }

    async onFilesSelected(ev) {
        const files = Array.from(ev.target.files || []);
        if (!files.length) return;
        this.state.uploading = true;
        try {
            const pid = await this._ensurePrompt();
            for (const f of files) {
                const b64 = await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(reader.result.split(",")[1]);
                    reader.onerror = reject;
                    reader.readAsDataURL(f);
                });
                const res = await rpc("/etp_assessment/prompt/upload_resource", {
                    prompt_id: pid, filename: f.name, file_b64: b64,
                });
                this.state.resources.push(res);
                if (res.extraction_error) {
                    this.notification.add(
                        `${f.name}: ${res.extraction_error}`, { type: "warning" });
                }
            }
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger", sticky: true });
        } finally {
            this.state.uploading = false;
            ev.target.value = "";
        }
    }

    async onRemoveResource(r) {
        try {
            await rpc("/etp_assessment/prompt/remove_resource", { resource_id: r.id });
            this.state.resources = this.state.resources.filter((x) => x.id !== r.id);
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger" });
        }
    }

    // ---- generation (seed: resources + golden example, ONE call) ----
    async onGenerate() {
        if (!this.state.resources.length && !this.state.notes.trim()) {
            this.notification.add("Upload at least one resource file first.", { type: "warning" });
            return;
        }
        if (!this.state.goldenExample.trim()) {
            this.notification.add("Paste a golden example question first.", { type: "warning" });
            return;
        }
        this.state.loadingQuestions = true;
        this.state.questions = [];
        try {
            const pid = await this._ensurePrompt();
            const res = await rpc("/etp_assessment/prompt/generate", {
                prompt_id: pid,
                title: this.state.title,
                notes: this.state.notes,
                golden_example: this.state.goldenExample,
                max_questions: this.state.maxQuestions || 0,
                category_id: this.state.categoryId || false,
            });
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

    // ---- image generation for imgcmp drafts ----
    async onGenerateImages() {
        this.state.loadingImages = true;
        try {
            const res = await rpc("/etp_assessment/prompt/generate_images", {
                prompt_id: this.state.promptId,
            });
            for (const updated of res.questions) {
                const q = this.state.questions.find((x) => x.id === updated.id);
                if (q) Object.assign(q, updated);
                if (updated.image_error) {
                    this.notification.add(
                        `${updated.name}: ${updated.image_error}`, { type: "warning" });
                }
            }
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger", sticky: true });
        } finally {
            this.state.loadingImages = false;
        }
    }

    imgUrl(q, side) {
        return `/web/image/etp.assessment.prompt.question/${q.id}/image_${side}`;
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

    async onApproveAll() {
        try {
            const res = await rpc("/etp_assessment/prompt/decision_bulk", {
                prompt_id: this.state.promptId, approve: true,
            });
            for (const u of res.updated) {
                const q = this.state.questions.find((x) => x.id === u.id);
                if (q) q.state = u.state;
            }
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger" });
        }
    }

    async onSaveSystemPrompt(which) {
        const value = which === "seed" ? this.state.sysSeed : this.state.sysScoring;
        await rpc("/etp_assessment/prompt/save_system_prompt", { which, value });
        this.notification.add("System prompt saved.", { type: "success" });
    }

    _err(e) {
        return (e && e.data && e.data.message) || (e && e.message) || "Something went wrong";
    }
}

registry.category("actions").add("etp_assessment.prompt_generator", EtpPromptGenerator);
