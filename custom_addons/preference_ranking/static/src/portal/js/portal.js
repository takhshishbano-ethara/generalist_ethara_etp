/**
 * Vindex Portal — Interactive annotation interface.
 *
 * Handles: tab navigation, score/preference selection, auto-save on blur,
 * evaluate, submit, collapsibles, and markdown+LaTeX rendering.
 */
(function () {
    "use strict";

    const app = document.getElementById("vx-app");
    if (!app) return; // Only run on the task detail page

    const TASK_ID = app.dataset.taskId;
    const CSRF_TOKEN = (document.querySelector('meta[name="csrf_token"]') || {}).content || "";

    // Pending field changes (debounced save buffer)
    let pendingFields = {};
    let saveTimer = null;

    // ── Collapsibles ────────────────────────────────────────────
    document.querySelectorAll(".vx-collapsible-toggle").forEach(function (btn) {
        btn.addEventListener("click", function () {
            const targetId = this.dataset.toggleTarget;
            const body = document.getElementById(targetId);
            if (!body) return;
            const isOpen = body.style.display !== "none";
            body.style.display = isOpen ? "none" : "block";
            this.classList.toggle("open", !isOpen);
            if (!isOpen) renderMarkdown(body);
        });
    });

    // ── Score Badge Selection (1-6) ─────────────────────────────
    document.querySelectorAll(".vx-score-badges").forEach(function (group) {
        const field = group.dataset.field;
        if (!field) return;
        group.querySelectorAll(".vx-score-btn").forEach(function (btn) {
            btn.addEventListener("click", function () {
                // Deactivate siblings
                group.querySelectorAll(".vx-score-btn").forEach(function (b) {
                    b.classList.remove("active", "error");
                });
                this.classList.add("active");
                queueSave(field, this.dataset.value);
            });
        });
    });

    // ── Preference Badge Selection (-3 to +3) ───────────────────
    document.querySelectorAll(".vx-pref-badges").forEach(function (group) {
        var field = group.dataset.field;
        if (!field) return;
        group.querySelectorAll(".vx-pref-btn").forEach(function (btn) {
            btn.addEventListener("click", function () {
                group.querySelectorAll(".vx-pref-btn").forEach(function (b) {
                    b.classList.remove("active");
                });
                this.classList.add("active");
                queueSave(field, this.dataset.value);
            });
        });
    });

    // ── Textarea / Input auto-save on blur ──────────────────────
    document.querySelectorAll("[data-field]").forEach(function (el) {
        if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
            el.addEventListener("blur", function () {
                queueSave(this.dataset.field, this.value);
            });
            // Also save on Enter for single-line inputs
            if (el.tagName === "INPUT") {
                el.addEventListener("keydown", function (e) {
                    if (e.key === "Enter") {
                        queueSave(this.dataset.field, this.value);
                    }
                });
            }
        }
    });

    // ── Save Queue (debounced 500ms) ────────────────────────────
    function queueSave(field, value) {
        pendingFields[field] = value;
        showSaveStatus("saving", "Saving...");
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(flushSave, 500);
    }

    function flushSave() {
        if (Object.keys(pendingFields).length === 0) return;
        var fieldsToSave = Object.assign({}, pendingFields);
        pendingFields = {};

        fetch("/vindex/tasks/" + TASK_ID + "/save", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": CSRF_TOKEN,
            },
            body: JSON.stringify(fieldsToSave),
        })
        .then(function (res) { return res.json(); })
        .then(function (data) {
            if (data.success) {
                showSaveStatus("saved", "Saved");
            } else {
                showSaveStatus("error", "Save failed: " + (data.message || "Unknown error"));
            }
        })
        .catch(function (err) {
            showSaveStatus("error", "Save error: " + err.message);
        });
    }

    function showSaveStatus(cls, text) {
        var el = document.getElementById("vx-save-status");
        if (!el) return;
        el.className = "vx-save-status " + cls;
        el.textContent = text;
        if (cls === "saved") {
            setTimeout(function () {
                if (el.textContent === "Saved") el.textContent = "";
            }, 2000);
        }
    }

    // ── Evaluate Button ─────────────────────────────────────────
    var evalBtn = document.getElementById("vx-btn-evaluate");
    if (evalBtn) {
        evalBtn.addEventListener("click", function () {
            // Flush any pending saves first
            flushSave();

            this.disabled = true;
            this.textContent = "Evaluating...";
            showSaveStatus("saving", "Evaluating scores...");

            fetch("/vindex/tasks/" + TASK_ID + "/evaluate", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": CSRF_TOKEN,
                },
            })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data.success) {
                    showSaveStatus("saved", "Evaluation done (" + data.errors_found + " errors)");
                    // Reload the page to show updated error highlights
                    setTimeout(function () { window.location.reload(); }, 500);
                } else {
                    showSaveStatus("error", "Evaluation failed: " + (data.message || ""));
                    evalBtn.disabled = false;
                    evalBtn.textContent = "Evaluate";
                }
            })
            .catch(function (err) {
                showSaveStatus("error", "Evaluation error: " + err.message);
                evalBtn.disabled = false;
                evalBtn.textContent = "Evaluate";
            });
        });
    }

    // ── Submit Button ───────────────────────────────────────────
    var submitBtn = document.getElementById("vx-btn-submit");
    if (submitBtn) {
        submitBtn.addEventListener("click", function () {
            if (!confirm("Submit this task? This action cannot be undone.")) return;

            // Flush pending saves
            flushSave();

            this.disabled = true;
            this.textContent = "Submitting...";
            showSaveStatus("saving", "Submitting...");

            fetch("/vindex/tasks/" + TASK_ID + "/submit", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": CSRF_TOKEN,
                },
            })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data.success) {
                    showSaveStatus("saved", "Submitted!");
                    var badge = document.getElementById("vx-task-status-badge");
                    if (badge) {
                        badge.textContent = "Submitted";
                        badge.className = "vx-badge vx-badge-lg vx-badge-success";
                    }
                    submitBtn.textContent = "Submitted";
                    // Redirect to list after a short delay
                    setTimeout(function () { window.location.href = "/vindex/tasks"; }, 1000);
                } else {
                    showSaveStatus("error", "Submit failed: " + (data.message || ""));
                    submitBtn.disabled = false;
                    submitBtn.textContent = "Submit Task";
                }
            })
            .catch(function (err) {
                showSaveStatus("error", "Submit error: " + err.message);
                submitBtn.disabled = false;
                submitBtn.textContent = "Submit Task";
            });
        });
    }

    // ── Markdown + LaTeX Rendering ──────────────────────────────
    function renderMarkdown(container) {
        if (!container) container = document;
        var elements = container.querySelectorAll("[data-render-markdown]");
        elements.forEach(function (el) {
            if (el.dataset.rendered === "true") return;
            var raw = el.textContent || "";
            if (!raw.trim()) return;

            try {
                // Render markdown if marked.js is available
                if (typeof marked !== "undefined") {
                    el.innerHTML = marked.parse(raw, {
                        breaks: true,
                        gfm: true,
                    });
                }
                // Render LaTeX if KaTeX is available
                if (typeof katex !== "undefined") {
                    renderLatex(el);
                }
            } catch (e) {
                // Fallback: keep original text
                console.warn("Markdown render error:", e);
            }
            el.dataset.rendered = "true";
        });
    }

    function renderLatex(container) {
        if (typeof katex === "undefined") return;
        var html = container.innerHTML;

        // Display math: $$...$$
        html = html.replace(/\$\$([\s\S]+?)\$\$/g, function (match, tex) {
            try {
                return katex.renderToString(tex.trim(), { displayMode: true, throwOnError: false });
            } catch (e) {
                return match;
            }
        });

        // Inline math: $...$  (avoid matching $$)
        html = html.replace(/\$([^\$\n]+?)\$/g, function (match, tex) {
            try {
                return katex.renderToString(tex.trim(), { displayMode: false, throwOnError: false });
            } catch (e) {
                return match;
            }
        });

        // \[...\] display math
        html = html.replace(/\\\[([\s\S]+?)\\\]/g, function (match, tex) {
            try {
                return katex.renderToString(tex.trim(), { displayMode: true, throwOnError: false });
            } catch (e) {
                return match;
            }
        });

        // \(...\) inline math
        html = html.replace(/\\\(([\s\S]+?)\\\)/g, function (match, tex) {
            try {
                return katex.renderToString(tex.trim(), { displayMode: false, throwOnError: false });
            } catch (e) {
                return match;
            }
        });

        container.innerHTML = html;
    }

    // Initial render on page load — single scrollable page, render all markdown
    document.addEventListener("DOMContentLoaded", function () {
        renderMarkdown(document);
    });

    // Run immediately for elements already in DOM
    renderMarkdown(document);

})();
