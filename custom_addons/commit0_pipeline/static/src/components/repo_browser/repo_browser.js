/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { CodeEditor } from "@web/core/code_editor/code_editor";
import { SelectMenu } from "@web/core/select_menu/select_menu";
import { rpc } from "@web/core/network/rpc";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

import { Component, EventBus, useRef, useState, onWillStart, onWillUpdateProps, onMounted, onWillUnmount } from "@odoo/owl";

/**
 * Shared bus for syncing file selection and scroll position between paired
 * RepoBrowser panels at Stage 5 (clone_path_original ↔ clone_path_stubbed).
 */
const SYNC_FIELDS = new Set(["clone_path_original", "clone_path_stubbed"]);
const repoBrowserBus = new EventBus();

export class RepoBrowser extends Component {
    static template = "commit0_pipeline.RepoBrowser";
    static components = { CodeEditor, SelectMenu };
    static props = {
        ...standardFieldProps,
    };

    setup() {
        this._copyTimeout = null;
        this._syncInProgress = false;
        this._scrollSyncInProgress = false;
        this._aceEditor = null;
        this._scrollHandler = null;
        this.rootRef = useRef("rootRef");
        this.state = useState({
            tree: [],
            flatFiles: [],
            selectedPath: "",
            fileContent: "",
            aceMode: "python",
            loading: false,
            treeLoaded: false,
            repoName: "",
            fileSize: 0,
            error: "",
            darkTheme: true,
            copied: false,
            diffMode: false,
            diffContent: "",
            hasDiffChanges: false,
        });

        this._onSyncFileSelect = this._onSyncFileSelect.bind(this);
        this._onSyncScroll = this._onSyncScroll.bind(this);

        onWillStart(() => {});
        onMounted(() => {
            if (SYNC_FIELDS.has(this.props.name)) {
                repoBrowserBus.addEventListener("file-selected", this._onSyncFileSelect);
                repoBrowserBus.addEventListener("scroll-sync", this._onSyncScroll);
            }
        });
        onWillUnmount(() => {
            this._detachScrollListener();
            if (SYNC_FIELDS.has(this.props.name)) {
                repoBrowserBus.removeEventListener("file-selected", this._onSyncFileSelect);
                repoBrowserBus.removeEventListener("scroll-sync", this._onSyncScroll);
            }
        });
        onWillUpdateProps((nextProps) => {
            const currentPath = this.props.record.data[this.props.name];
            const nextPath = nextProps.record.data[nextProps.name];
            if (nextPath !== currentPath && nextPath) {
                this._detachScrollListener();
                this.state.treeLoaded = false;
                this.state.tree = [];
                this.state.flatFiles = [];
                this.state.selectedPath = "";
                this.state.fileContent = "";
            }
        });
    }

    _onSyncFileSelect({ detail }) {
        if (detail.sourceField === this.props.name) {
            return;
        }
        if (!this.state.treeLoaded) {
            return;
        }
        const hasFile = this.state.flatFiles.some((f) => f.path === detail.path);
        if (!hasFile) {
            return;
        }
        this._syncInProgress = true;
        this.onFileSelect(detail.path).finally(() => {
            this._syncInProgress = false;
        });
    }

    _onSyncScroll({ detail }) {
        if (detail.sourceField === this.props.name) {
            return;
        }
        if (!this._aceEditor || this._scrollSyncInProgress) {
            return;
        }
        this._scrollSyncInProgress = true;
        this._aceEditor.getSession().setScrollTop(detail.scrollTop);
        requestAnimationFrame(() => {
            this._scrollSyncInProgress = false;
        });
    }

    _attachScrollListener() {
        this._detachScrollListener();
        if (!SYNC_FIELDS.has(this.props.name) || !this.rootRef.el) {
            return;
        }
        const aceEl = this.rootRef.el.querySelector(".ace_editor");
        if (!aceEl || !window.ace) {
            return;
        }
        this._aceEditor = window.ace.edit(aceEl);
        this._scrollHandler = () => {
            if (this._scrollSyncInProgress) {
                return;
            }
            const scrollTop = this._aceEditor.getSession().getScrollTop();
            repoBrowserBus.trigger("scroll-sync", {
                scrollTop,
                sourceField: this.props.name,
            });
        };
        this._aceEditor.getSession().on("changeScrollTop", this._scrollHandler);
    }

    _detachScrollListener() {
        if (this._aceEditor && this._scrollHandler) {
            this._aceEditor.getSession().off("changeScrollTop", this._scrollHandler);
        }
        this._scrollHandler = null;
        this._aceEditor = null;
    }

    get clonePath() {
        return this.props.record.data[this.props.name] || "";
    }

    get entryId() {
        return this.props.record.resId;
    }

    get pathField() {
        return this.props.name;
    }

    get selectMenuChoices() {
        return this.state.flatFiles.map((f) => ({
            value: f.path,
            label: f.path,
        }));
    }

    async onClickLoadTree() {
        await this.props.record.save();
        await this._loadTree();
    }

    async _loadTree() {
        if (!this.entryId) {
            return;
        }

        this.state.loading = true;
        this.state.error = "";

        try {
            const result = await rpc("/commit0/file_tree", {
                eval_id: this.entryId,
                path_field: this.pathField,
            });

            if (result.error) {
                this.state.error = result.error;
                return;
            }

            this.state.tree = result.tree || [];
            this.state.repoName = result.repo_name || "";
            this.state.flatFiles = this._flattenTree(result.tree || []);
            this.state.treeLoaded = true;
        } catch (e) {
            this.state.error = e.message || "Failed to load file tree";
        } finally {
            this.state.loading = false;
        }
    }

    _flattenTree(nodes, result = []) {
        for (const node of nodes) {
            if (node.is_dir && node.children) {
                this._flattenTree(node.children, result);
            } else if (!node.is_dir) {
                result.push(node);
            }
        }
        return result;
    }

    async onFileSelect(path) {
        if (!path || path === this.state.selectedPath) {
            return;
        }

        this._detachScrollListener();
        this.state.loading = true;
        this.state.error = "";

        try {
            const result = await rpc("/commit0/file_content", {
                eval_id: this.entryId,
                path_field: this.pathField,
                file_path: path,
            });

            if (result.error) {
                this.state.error = result.error;
                return;
            }

            this.state.selectedPath = path;
            this.state.fileContent = result.content || "";
            this.state.aceMode = result.mode || "python";
            this.state.fileSize = result.size || 0;

            if (this.state.diffMode && this.canShowDiff) {
                await this._loadDiff();
            }

            if (!this._syncInProgress && SYNC_FIELDS.has(this.props.name)) {
                repoBrowserBus.trigger("file-selected", {
                    path,
                    sourceField: this.props.name,
                });
            }

            requestAnimationFrame(() => this._attachScrollListener());
        } catch (e) {
            this.state.error = e.message || "Failed to load file";
        } finally {
            this.state.loading = false;
        }
    }

    async onCopy() {
        if (!this.state.fileContent) return;
        try {
            await navigator.clipboard.writeText(this.state.fileContent);
            this.state.copied = true;
            if (this._copyTimeout) clearTimeout(this._copyTimeout);
            this._copyTimeout = setTimeout(() => {
                this.state.copied = false;
                this._copyTimeout = null;
            }, 2000);
        } catch {
        }
    }

    toggleTheme() {
        this.state.darkTheme = !this.state.darkTheme;
    }

    get canShowDiff() {
        return this.props.name === "clone_path_stubbed";
    }

    async toggleDiff() {
        this.state.diffMode = !this.state.diffMode;
        if (this.state.diffMode && this.state.selectedPath) {
            await this._loadDiff();
        }
    }

    async _loadDiff() {
        this.state.loading = true;
        try {
            const result = await rpc("/commit0/file_diff", {
                eval_id: this.entryId,
                file_path: this.state.selectedPath,
            });
            this.state.diffContent = result.diff || "";
            this.state.hasDiffChanges = result.has_changes || false;
        } catch (e) {
            this.state.error = e.message || "Failed to load diff";
        } finally {
            this.state.loading = false;
        }
    }

    get diffLines() {
        return (this.state.diffContent || "").split("\n");
    }

    get editorTheme() {
        return this.state.darkTheme ? "monokai" : "";
    }

    get fileName() {
        if (!this.state.selectedPath) return "";
        return this.state.selectedPath.split("/").pop();
    }

    get fileSizeFormatted() {
        const bytes = this.state.fileSize;
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
        return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }
}

export const repoBrowserField = {
    component: RepoBrowser,
    displayName: _t("Repository Browser"),
    supportedTypes: ["char"],
    extractProps: () => ({}),
};

registry.category("fields").add("repo_browser", repoBrowserField);
