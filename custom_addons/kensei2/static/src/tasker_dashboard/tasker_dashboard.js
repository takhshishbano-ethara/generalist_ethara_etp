/** @odoo-module */
import { useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { router } from "@web/core/browser/router";
import { Kensei2DashboardBase } from "@kensei2/dashboard_base/dashboard_base";

// member_id must survive the URL. When a client action mounts, the action manager
// runs its OWN router.pushState(..., {replace:true}); replace mode keeps ONLY the
// "locked" keys (computeNextState -> pick(state, ..._lockedKeys)) plus the action
// stack, so an un-locked member_id we added gets wiped and a refresh recovers
// nothing. Locking it makes the manager's replace preserve it. Registered once.
router.addLockedKey("member_id");

/**
 * Per-tasker performance dashboard. Serves two entry points:
 *  - Dashboard menu (plain taskers)  -> no member_id  -> the current user.
 *  - Team Management button          -> params.member_id -> that member
 *    (QL/PL only, enforced server-side).
 */
export class Kensei2TaskerDashboard extends Kensei2DashboardBase {
    static template = "kensei2.TaskerDashboard";

    setup() {
        super.setup();
        // The action params are the single source of truth: on a fresh drill-in
        // action_view_performance sets member_id; on a REFRESH Odoo rebuilds the
        // action from the URL and copies the (locked) member_id back into the params
        // (_getActionParams -> params: state). So no separate URL read is needed, and
        // reading the URL directly would surface a STALE locked value when you next
        // open your own dashboard. The server authorises every member_id, so a
        // hand-typed one is safe.
        this.memberId = Number(this.props.action?.params?.member_id) || false;
        // Reflect the current view in the URL so a refresh recovers it: keep the id
        // for a tasker (locked, so the action manager's replace preserves it), and
        // clear it for the viewer's own dashboard (undefined is dropped by
        // sanitizeSearch) so a stale id never leaks in.
        router.replaceState(
            { member_id: this.memberId || undefined }, { sync: true });
        // member_id is a LOCKED url key so it survives a refresh — but that also
        // means it would linger on every other page. Clear it when the dashboard is
        // left via in-app navigation. A browser REFRESH tears down the VM without
        // firing this, so the id still survives a reload (which is the whole point).
        onWillUnmount(() => {
            router.replaceState({ member_id: undefined }, { sync: true });
        });
        this.state = useState({
            loading: true,
            denied: false,
            subject: {},
            kpis: [],
            funnel: [],
            tasks: [],
            // Sort defaults to the server order (newest assignment first).
            sortKey: "assigned",
            sortAsc: false,
            // Table filters.
            search: "",
            statusFilter: "",       // "" = all statuses
            currentOnly: true,      // collapse a task's stage rows to its current one
            // Pagination.
            page: 1,
            pageSize: 15,
            dateFrom: "",
            dateTo: "",
            lastUpdated: "",
        });
        onWillStart(() => this._load());
    }

    async _load() {
        const res = await this._fetch(
            "/kensei2/tracker/performance",
            { member_id: this.memberId || false },
            "Failed to load performance data.");
        if (!res) {
            return;
        }
        if (res.error) {
            this.state.denied = true;
            this.notification.add(
                res.error === "access_denied"
                    ? "You are not allowed to view this tasker."
                    : "No performance data available.",
                { type: "warning" });
            return;
        }
        this.state.denied = false;
        this.state.subject = res.subject || {};
        this.state.kpis = res.kpis || [];
        this.state.funnel = res.funnel || [];
        this.state.tasks = res.tasks || [];
    }

    get title() {
        return this.memberId ? this.state.subject.name || "Tasker" : "My Dashboard";
    }

    // ---- Task table: filter -> sort -> paginate --------------------------

    /** Distinct statuses present, for the Status filter dropdown. */
    get statusOptions() {
        const seen = new Map();
        for (const t of this.state.tasks) {
            if (t.status_label && !seen.has(t.status_label)) {
                seen.set(t.status_label, true);
            }
        }
        return [...seen.keys()].sort((a, b) => a.localeCompare(b));
    }

    /** Rows after the Current-stage / Status / text-search filters. */
    get filteredTasks() {
        const q = this.state.search.trim().toLowerCase();
        const status = this.state.statusFilter;
        const currentOnly = this.state.currentOnly;
        return this.state.tasks.filter((t) => {
            if (currentOnly && !t.is_current) {
                return false;
            }
            if (status && t.status_label !== status) {
                return false;
            }
            if (q) {
                const hay = [t.task_id, t.persona, t.l1, t.l2, t.pl, t.status_label]
                    .join(" ").toLowerCase();
                if (!hay.includes(q)) {
                    return false;
                }
            }
            return true;
        });
    }

    /** Filtered rows, sorted by the active column. */
    get sortedTasks() {
        const key = this.state.sortKey;
        const dir = this.state.sortAsc ? 1 : -1;
        const val = (r) => {
            const v = r[key];
            return v === null || v === undefined ? "" : v;
        };
        return [...this.filteredTasks].sort((a, b) => {
            const va = val(a);
            const vb = val(b);
            if (typeof va === "number" && typeof vb === "number") {
                return (va - vb) * dir;
            }
            // numeric:true keeps "2/2" and "10" ordering sane for mixed values.
            return String(va).localeCompare(String(vb), undefined, { numeric: true }) * dir;
        });
    }

    get pageCount() {
        return Math.max(1, Math.ceil(this.sortedTasks.length / this.state.pageSize));
    }

    /** Page clamped to the valid range (a filter may have shrunk the result set). */
    get currentPage() {
        return Math.min(this.state.page, this.pageCount);
    }

    /** The current page's rows (page is clamped in case a filter shrank the set). */
    get pagedTasks() {
        const page = Math.min(this.state.page, this.pageCount);
        const start = (page - 1) * this.state.pageSize;
        return this.sortedTasks.slice(start, start + this.state.pageSize);
    }

    /** 1-based index of the first row shown, for the "x–y of N" label. */
    get pageStart() {
        return this.sortedTasks.length === 0
            ? 0
            : (Math.min(this.state.page, this.pageCount) - 1) * this.state.pageSize + 1;
    }

    get pageEnd() {
        return Math.min(this.pageStart + this.state.pageSize - 1, this.sortedTasks.length);
    }

    setSort(key) {
        if (this.state.sortKey === key) {
            this.state.sortAsc = !this.state.sortAsc;
        } else {
            this.state.sortKey = key;
            this.state.sortAsc = true;
        }
    }

    // Any filter change resets to the first page so results aren't hidden off-page.
    onSearchInput(ev) {
        this.state.search = ev.target.value;
        this.state.page = 1;
    }

    onStatusFilter(ev) {
        this.state.statusFilter = ev.target.value;
        this.state.page = 1;
    }

    toggleCurrentOnly() {
        this.state.currentOnly = !this.state.currentOnly;
        this.state.page = 1;
    }

    prevPage() {
        if (this.state.page > 1) {
            this.state.page--;
        }
    }

    nextPage() {
        if (this.state.page < this.pageCount) {
            this.state.page++;
        }
    }

    // ---- CSV export -----------------------------------------------------
    csvFileName() {
        const who = (this.state.subject.name || "me")
            .toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
        return `kensei2_performance_${who || "me"}.csv`;
    }

    csvMeta() {
        return [
            ...super.csvMeta(),
            ["Tasker", this.state.subject.name || "Me"],
        ];
    }

    csvSections() {
        return [
            {
                title: "Performance",
                headers: ["Metric", "Value"],
                // suffix (%, days) is part of what the KPI MEANS, so fold it in —
                // a bare "87" loses whether it was 87% or 87 days.
                rows: this.state.kpis.map((k) => [
                    k.label,
                    k.value === null || k.value === undefined
                        ? "" : `${k.value}${k.suffix || ""}`,
                ]),
            },
            {
                title: "My Pipeline",
                headers: ["Step", "Count"],
                rows: this.state.funnel.map((c) => [c.label, c.value]),
            },
            {
                title: "My Tasks",
                headers: ["Task ID", "Stage", "Persona", "L1", "L2", "PL",
                    "Status", "Overall", "Assigned"],
                rows: this.sortedTasks.map((r) => [
                    r.task_id,
                    `${r.stage} / ${r.total_stages}`,
                    r.persona,
                    r.l1 || "",
                    r.l2 || "",
                    r.pl || "",
                    r.status_label,
                    r.overall ?? "",
                    r.assigned || "",
                ]),
            },
        ];
    }

    openTask(row) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "kensei2.tracker.allocation",
            res_id: row.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    /** Keyboard equivalent of clicking a task row (the <tr> is t-on-click). */
    onRowKeydown(ev, row) {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();
            this.openTask(row);
        }
    }
}

registry.category("actions").add("kensei2_tasker_dashboard", Kensei2TaskerDashboard);
