/** @odoo-module */
// Shared helpers for the Kensei2 Tracker dashboards (dashboard / tasker / daily).
// Extracted to remove the three-way copy-paste of date + format helpers and to
// fix the UTC-vs-local "today" discrepancy (toISOString is UTC).

/** Format a Date as a LOCAL 'YYYY-MM-DD' (never UTC). */
export function isoLocal(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}

/** Local ISO date for today. */
export function todayIso() {
    return isoLocal(new Date());
}

/** Local ISO date for N days ago. */
export function daysAgoIso(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return isoLocal(d);
}

/** Metric formatter: "—" for empty, thousands-separated numbers, optional suffix. */
export function fmt(value, suffix) {
    if (value === null || value === undefined || value === "") {
        return "—";
    }
    const s = typeof value === "number" ? value.toLocaleString("en-US") : value;
    return suffix ? `${s}${suffix}` : s;
}
