/** @odoo-module **/
/**
 * Thin RPC service used by the OWL video editor.  Centralising network access
 * here keeps the components themselves declarative.
 */
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

export const videoQCService = {
    dependencies: ["orm", "notification"],
    start(env, { orm, notification }) {
        return {
            // ---- model helpers -------------------------------------------
            async loadTask(taskId) {
                const [task] = await orm.read(
                    "video.task",
                    [taskId],
                    [
                        "name",
                        "description",
                        "state",
                        "original_video_1_attachment",
                        "original_video_2_attachment",
                        "latest_version_id",
                        "total_versions_count",
                    ],
                );
                return task;
            },

            async loadVersion(versionId) {
                if (!versionId) return null;
                const [v] = await orm.read(
                    "video.task.version",
                    [versionId],
                    [
                        "version_no",
                        "trim_start",
                        "trim_end",
                        "duration",
                        "crop_data_json",
                        "editing_json",
                        "ffmpeg_command",
                        "status",
                        "qc_status",
                        "qc_comment",
                        "prompt_text",
                        "prompt_response",
                        "edited_attachment_id",
                        "preview_attachment_id",
                        "original_attachment_id",
                    ],
                );
                return v;
            },

            async listVersions(taskId) {
                return orm.searchRead(
                    "video.task.version",
                    [["task_id", "=", taskId]],
                    [
                        "id",
                        "version_no",
                        "status",
                        "qc_status",
                        "duration",
                        "is_latest",
                        "create_date",
                    ],
                    { order: "version_no desc" },
                );
            },

            async listEditHistory(versionId) {
                return orm.searchRead(
                    "video.task.edit.history",
                    [["version_id", "=", versionId]],
                    ["action_type", "action_data", "create_date", "created_by"],
                    { order: "create_date desc", limit: 50 },
                );
            },

            // ---- write paths ---------------------------------------------
            async newVersion(taskId, editNotes = "") {
                return rpc(`/video_qc/task/${taskId}/new_version`, {
                    edit_notes: editNotes,
                });
            },

            async saveEdit(versionId, config, { render = false } = {}) {
                const result = await rpc(`/video_qc/version/${versionId}/save_edit`, {
                    config,
                    render,
                });
                notification.add(
                    render
                        ? _t("Render queued. Refresh in a moment to see results.")
                        : _t("Edit saved."),
                    { type: "success" },
                );
                return result;
            },

            async savePrompt(versionId, prompt_text, prompt_response = "") {
                return rpc(`/video_qc/version/${versionId}/save_prompt`, {
                    prompt_text,
                    prompt_response,
                });
            },

            async sendToQC(taskId) {
                await orm.call("video.task", "action_send_to_qc", [[taskId]]);
                notification.add(_t("Sent to QC."), { type: "success" });
            },

            async triggerDownload(taskId) {
                return rpc(`/video_qc/task/${taskId}/download`, {});
            },

            // ---- url helpers ---------------------------------------------
            sourceUrl(versionId) {
                return `/video_qc/version/${versionId}/source`;
            },
            editedUrl(versionId) {
                return `/video_qc/version/${versionId}/edited`;
            },
            previewUrl(versionId) {
                return `/video_qc/version/${versionId}/preview`;
            },
            taskOriginalUrl(taskId, slot) {
                return `/video_qc/task/${taskId}/original/${slot}`;
            },
        };
    },
};

registry.category("services").add("video_qc", videoQCService);
