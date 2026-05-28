/** @odoo-module **/

import { registry } from "@web/core/registry";

async function postJson(url, payload) {
    const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify(payload || {}),
    });
    const text = await response.text();
    let data = null;
    try {
        data = text ? JSON.parse(text) : null;
    } catch (err) {
        throw new Error(`Invalid JSON response (${response.status}): ${text.slice(0, 200)}`);
    }
    if (!response.ok) {
        const message = (data && (data.message || data.error)) || `HTTP ${response.status}`;
        throw new Error(message);
    }
    if (data && data.error) {
        throw new Error(data.message || data.error);
    }
    return data ? data.result : null;
}

async function getJson(url) {
    const response = await fetch(url, {
        method: "GET",
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
    });
    const text = await response.text();
    let data = null;
    try {
        data = text ? JSON.parse(text) : null;
    } catch (err) {
        throw new Error(`Invalid JSON response (${response.status}): ${text.slice(0, 200)}`);
    }
    if (!response.ok) {
        const message = (data && (data.message || data.error)) || `HTTP ${response.status}`;
        throw new Error(message);
    }
    if (data && data.error) {
        throw new Error(data.message || data.error);
    }
    return data ? data.result : null;
}

export const videoEditorS3Service = {
    dependencies: [],
    start() {
        return {
            async loadProject({ s3Url, projectId, name } = {}) {
                return postJson("/video_editor/load", {
                    s3_url: s3Url,
                    project_id: projectId,
                    name,
                });
            },
            async fetchProject(projectId) {
                return getJson(`/video_editor/project/${projectId}`);
            },
            async processProject({ projectId, config, preview = false }) {
                return postJson("/video_editor/process", {
                    project_id: projectId,
                    config,
                    preview,
                });
            },
            async exportToS3({ projectId, s3Key } = {}) {
                return postJson("/video_editor/export", {
                    project_id: projectId,
                    s3_key: s3Key,
                });
            },
            async getJobStatus(jobId) {
                return getJson(`/video_editor/status/${jobId}`);
            },
            async cancelJob(jobId) {
                return postJson(`/video_editor/cancel/${jobId}`, {});
            },
            streamUrl(projectId, kind = "source") {
                return `/video_editor/stream/${projectId}/${kind}`;
            },
        };
    },
};

registry.category("services").add("video_editor_s3", videoEditorS3Service);
