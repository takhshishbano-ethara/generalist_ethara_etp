/** @odoo-module */
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

const POLL_INTERVAL_MS = 5000;
const PAGE_SIZE = 12;

function fmtNumber(n) {
    if (!n && n !== 0) return "0";
    return Number(n).toLocaleString("en-US");
}

function fmtMoney(n) {
    if (n == null) return "$0";
    return n < 1 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

function fmtDuration(seconds) {
    if (!seconds) return "0s";
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const mins = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    return `${mins}m ${secs}s`;
}

function fmtPct(n) {
    if (n == null) return "0%";
    return `${Number(n).toFixed(1)}%`;
}

function fmtRound(n) {
    if (n == null) return "0";
    return String(Math.round(Number(n)));
}

function fmtScorePct(n) {
    if (n == null) return "-";
    return `${Number(n).toFixed(0)}%`;
}

function truncate(text, len) {
    if (!text) return "";
    const s = String(text);
    return s.length > len ? s.substring(0, len) + "…" : s;
}

function keyLabel(key) {
    return key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

function autoFmt(key, val) {
    if (val == null) return "—";
    const k = key.toLowerCase();
    if (typeof val === "boolean") return val ? "Yes" : "No";
    if (typeof val === "string") return val;
    if (k.indexOf("cost") !== -1) return val < 1 ? `$${val.toFixed(4)}` : `$${val.toFixed(2)}`;
    if (k.indexOf("pct") !== -1) return `${val.toFixed(1)}%`;
    if (typeof val === "number") {
        if (Number.isInteger(val)) return val.toLocaleString();
        return val.toFixed(2);
    }
    return String(val);
}

export class ArcMonitorDashboard extends Component {
    static template = "arc_monitor.Dashboard";
    static props = { action: { type: Object, optional: true }, "*": true };

    setup() {
        this.actionService = useService("action");
        this.notification = useService("notification");
        this.fmtNumber = fmtNumber;
        this.fmtMoney = fmtMoney;
        this.fmtDuration = fmtDuration;
        this.fmtPct = fmtPct;
        this.fmtRound = fmtRound;
        this.fmtScorePct = fmtScorePct;
        this.truncate = truncate;
        this.keyLabel = keyLabel;
        this.autoFmt = autoFmt;
        this.strLen = (v) => v ? String(v).length : 0;

        this.state = useState({
            loading: true,
            runs: [],
            models: [],
            games: [],
            totals: { runs: 0, solved: 0, partial: 0, failed: 0, total_cost: 0, total_tokens: 0, total_elapsed_s: 0 },
            sessions: [],
            active_scan: null,
            latest_scan_id: null,
            latest_scan_name: "",
            latest_scan_path: "",
            latest_scan_date: "",
            latest_scan_trigger: "",
            latest_scan_source_type: "",
            latest_scan_duration: 0,
            // Filters
            search: "",
            modelFilter: "",
            gameFilter: "",
            statusFilter: "",
            sortBy: "timestamp",
            // Pagination
            page: 1,
            // Detail modal
            detailOpen: false,
            detailLoading: false,
            detailData: null,
            detailError: "",
            // Log viewer
            logSearch: "",
            logLevel: "ALL",
            // Expanded text cells
            expandedCells: {},
        });

        this._pollInterval = null;
        this._reloading = false;

        onMounted(() => {
            this._loadData();
            this._startPolling();
        });

        onWillUnmount(() => {
            this._stopPolling();
        });
    }

    // ─── Polling ───

    _startPolling() {
        this._pollInterval = setInterval(() => this._poll(), POLL_INTERVAL_MS);
    }

    _stopPolling() {
        if (this._pollInterval) {
            clearInterval(this._pollInterval);
            this._pollInterval = null;
        }
    }

    async _poll() {
        if (this._reloading) return;
        this._reloading = true;
        try {
            await this._fetchDashboardData();
        } finally {
            this._reloading = false;
        }
    }

    // ─── Data loading ───

    async _loadData() {
        this.state.loading = true;
        try {
            await this._fetchDashboardData();
        } catch (e) {
            this.notification.add("Failed to load monitor data", { type: "danger" });
            console.error("[arc-monitor]", e);
        } finally {
            this.state.loading = false;
        }
    }

    async _fetchDashboardData() {
        const res = await rpc("/arc_monitor/dashboard/data", {});
        this.state.runs = res.runs || [];
        this.state.models = res.models || [];
        this.state.games = res.games || [];
        this.state.totals = res.totals || this.state.totals;
        this.state.sessions = res.sessions || [];
        this.state.active_scan = res.active_scan || null;
        this.state.latest_scan_id = res.latest_scan_id || null;
        this.state.latest_scan_name = res.latest_scan_name || "";
        this.state.latest_scan_path = res.latest_scan_path || "";
        this.state.latest_scan_date = res.latest_scan_date || "";
        this.state.latest_scan_trigger = res.latest_scan_trigger || "";
        this.state.latest_scan_source_type = res.latest_scan_source_type || "";
        this.state.latest_scan_duration = res.latest_scan_duration || 0;
    }

    onRefresh() {
        this._loadData();
    }

    // ─── Filters ───

    get filteredRuns() {
        let runs = this.state.runs;
        const q = this.state.search.toLowerCase();
        const mf = this.state.modelFilter;
        const gf = this.state.gameFilter;
        const sf = this.state.statusFilter;

        runs = runs.filter(r => {
            if (mf && r.model_name !== mf) return false;
            if (gf && r.game_id !== gf) return false;
            if (sf && r.status !== sf) return false;
            if (q) {
                const blob = `${r.run_dir} ${r.model_name} ${r.game_id} ${r.session_id || ""}`.toLowerCase();
                if (!blob.includes(q)) return false;
            }
            return true;
        });

        const cmpMap = {
            timestamp: (a, b) => (b.timestamp || b.run_dir).localeCompare(a.timestamp || a.run_dir),
            timestamp_asc: (a, b) => (a.timestamp || a.run_dir).localeCompare(b.timestamp || b.run_dir),
            cost_desc: (a, b) => b.total_cost - a.total_cost,
            cost_asc: (a, b) => a.total_cost - b.total_cost,
            score_desc: (a, b) => b.avg_score_pct - a.avg_score_pct,
            score_asc: (a, b) => a.avg_score_pct - b.avg_score_pct,
            elapsed_desc: (a, b) => b.elapsed_seconds - a.elapsed_seconds,
        };
        const cmp = cmpMap[this.state.sortBy] || cmpMap.timestamp;
        return [...runs].sort(cmp);
    }

    get paginatedRuns() {
        const all = this.filteredRuns;
        const start = (this.state.page - 1) * PAGE_SIZE;
        return all.slice(start, start + PAGE_SIZE);
    }

    get totalPages() {
        return Math.max(1, Math.ceil(this.filteredRuns.length / PAGE_SIZE));
    }

    onSearchInput(ev) {
        this.state.search = ev.target.value;
        this.state.page = 1;
    }

    onModelFilter(ev) {
        this.state.modelFilter = ev.target.value;
        this.state.page = 1;
    }

    onGameFilter(ev) {
        this.state.gameFilter = ev.target.value;
        this.state.page = 1;
    }

    onStatusFilter(ev) {
        this.state.statusFilter = ev.target.value;
        this.state.page = 1;
    }

    onSortChange(ev) {
        this.state.sortBy = ev.target.value;
        this.state.page = 1;
    }

    onPagePrev() {
        if (this.state.page > 1) this.state.page--;
    }

    onPageNext() {
        if (this.state.page < this.totalPages) this.state.page++;
    }

    // ─── Detail modal ───

    async onCardClick(run) {
        this.state.detailOpen = true;
        this.state.detailLoading = true;
        this.state.detailData = null;
        this.state.detailError = "";
        try {
            const res = await rpc("/arc_monitor/dashboard/run_detail", {
                run_path: run.run_path,
                game_id: run.game_id,
                model_name: run.model_name,
            });
            if (res.error) {
                this.state.detailError = res.error;
            } else {
                this.state.detailData = res;
            }
        } catch (e) {
            this.state.detailError = e.message || "Failed to load detail";
        } finally {
            this.state.detailLoading = false;
        }
    }

    onCloseModal() {
        this.state.detailOpen = false;
        this.state.detailData = null;
        this.state.detailError = "";
        this.state.logSearch = "";
        this.state.logLevel = "ALL";
        this.state.expandedCells = {};
    }

    onModalBackdrop(ev) {
        if (ev.target === ev.currentTarget) {
            this.onCloseModal();
        }
    }

    // ─── Log Viewer ───

    onLogSearchInput(ev) {
        this.state.logSearch = ev.target.value;
    }

    onLogLevelChange(ev) {
        this.state.logLevel = ev.target.value;
    }

    get filteredLog() {
        const data = this.state.detailData;
        if (!data) return "";
        const entries = data.entries || [];
        const raw = (entries[0] && entries[0].session_log) || "";
        if (!raw) return "";
        const kw = this.state.logSearch.toLowerCase();
        const level = this.state.logLevel;
        return raw.split("\n").filter(line => {
            if (level !== "ALL" && line.indexOf(` ${level} `) === -1) return false;
            if (kw && line.toLowerCase().indexOf(kw) === -1) return false;
            return true;
        }).join("\n");
    }

    logLineClass(line) {
        if (line.indexOf(" ERROR ") !== -1) return "o_am_log_error";
        if (line.indexOf(" WARN ") !== -1) return "o_am_log_warn";
        if (line.indexOf(" DEBUG ") !== -1) return "o_am_log_debug";
        return "";
    }

    // ─── Expand cells ───

    toggleExpand(key) {
        const expanded = { ...this.state.expandedCells };
        expanded[key] = !expanded[key];
        this.state.expandedCells = expanded;
    }

    isExpanded(key) {
        return !!this.state.expandedCells[key];
    }

    // ─── Meta helpers ───

    getMetaKeys(obj) {
        if (!obj || typeof obj !== "object") return [];
        return Object.keys(obj).filter(k => {
            const v = obj[k];
            return v !== null && typeof v !== "object";
        });
    }

    // ─── Navigation ───

    onNewScan() {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "arc.monitor.scan",
            views: [[false, "form"]],
            target: "current",
        });
    }

    onOpenScan(scanId) {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "arc.monitor.scan",
            res_id: scanId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    onViewAllScans() {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "arc.monitor.scan",
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    // ─── Helpers ───

    statusIcon(status) {
        if (status === "pass") return "\u2705";
        if (status === "partial") return "\uD83D\uDFE1";
        return "\u274C";
    }

    statusClass(status) {
        if (status === "pass") return "o_am_status_pass";
        if (status === "partial") return "o_am_status_partial";
        return "o_am_status_fail";
    }

    get hasData() {
        return this.state.runs.length > 0;
    }

    get passRate() {
        const t = this.state.totals;
        if (!t.runs) return 0;
        return Math.round(100 * t.solved / t.runs);
    }

    get passRateColor() {
        const pct = this.passRate;
        if (pct >= 80) return "#28a745";
        if (pct >= 50) return "#fd7e14";
        return "#dc3545";
    }
}

registry.category("actions").add("arc_monitor_dashboard", ArcMonitorDashboard);
