/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { CodeEditor } from "@web/core/code_editor/code_editor";
import { SelectMenu } from "@web/core/select_menu/select_menu";
import { rpc } from "@web/core/network/rpc";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

import { Component, useState, onWillStart, onWillUpdateProps, onMounted } from "@odoo/owl";

export class RepoBrowser extends Component {
    static template = "commit0_pipeline.RepoBrowser";
    static components = { CodeEditor, SelectMenu };
    static props = {
        ...standardFieldProps,
    };

    setup() {
        this._copyTimeout = null;
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

        onWillStart(() => {});
        onMounted(() => {});
        onWillUpdateProps((nextProps) => {
            const currentPath = this.props.record.data[this.props.name];
            const nextPath = nextProps.record.data[nextProps.name];
            if (nextPath !== currentPath && nextPath) {
                this.state.treeLoaded = false;
                this.state.tree = [];
                this.state.flatFiles = [];
                this.state.selectedPath = "";
                this.state.fileContent = "";
            }
        });
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
