"""
Multimango Performance Pipeline
================================
Usage: python mm_performance_pipeline.py <input_csv>

Outputs (same directory as input):
  tasker_daily_deduped.csv          — Deduplicated: one row per tasker per day
  daily_summary.csv                 — Pipeline 2: team-level daily view
  tasker_rolling_scorecard.csv      — Pipeline 1: individual tasker scorecard
  tasker_performance_dashboard.html — Shareable self-contained dashboard

Scoring design
--------------
  Effort Score  (0-100) : absolute hours performance — min(daily_hours / target, 1) × 100,
                          averaged across non-anomaly days worked. Comparable cross-project.
  Output Score  (0-100) : project-relative performance — percentile rank within the
                          (date × task) cohort for submissions and AHT. Submissions vary
                          by task complexity so relative-only is the fair comparison.
  Final Score   (0-100) : W_HOURS × effort_score + W_SUBS × subs_score + W_AHT × aht_score

  Attendance % is retained as an informational column only — it does not multiply the score.
  Total hours already captures the volume/consistency signal implicitly.
"""

import csv
import sys
import os
import json as _json
import numpy as np
from collections import defaultdict
from scipy import stats

# ──────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────

HOURS_PER_TASKER_TARGET = 6     # daily target hours per annotator

# Day-score weights — applied per day, final score = mean(day_scores)
W_HOURS = 0.70
W_SUBS  = 0.20
W_AHT   = 0.10

# Days where team-wide hours < ANOMALY_THRESHOLD × n × target are excluded from all scoring.
ANOMALY_THRESHOLD = 0.15

# (date × task) cohorts with fewer than this many active taskers are skipped for
# submissions/AHT scoring — percentile ranking is meaningless in tiny groups.
# Hours scoring is unaffected (it is absolute, requires no cohort).
MIN_SCOREABLE_TASKERS = 10

# Daily Spotlight — top/bottom 10% for the latest scoreable day
SPOTLIGHT_MIN_HOURS  = 2.0   # taskers below this hours threshold are excluded from ranking
SPOTLIGHT_MIN_POOL   = 10    # hard floor: fewer eligible taskers → no labels assigned
SPOTLIGHT_THIN_PCT   = 0.50  # relative warning: < 50% of period avg → thin-pool banner
SPOTLIGHT_TOP_PCT    = 0.10
SPOTLIGHT_BOTTOM_PCT = 0.10

# Quadrant splits — both relative, derived from the team distribution each run.
# Hours  : team-median Effort Score  → top/bottom 50% on effort
# Subs   : project-cohort subs percentile ≥ team-median subs score → top/bottom 50% on output
# Each quadrant should contain roughly 25% of the team.

# Leads + managers/non-full-time. Their hours ARE counted in raw totals (daily_summary,
# tasker_daily_deduped, tasker_rolling_scorecard) and flagged there via "Is Lead" — but they
# are excluded from all scoring math, percentile cohorts, medians, dashboards, spotlight, and
# pod_performance, since they aren't full-time taskers and scoring them against taskers (or
# taskers against them) would be misleading in both directions.
LEADS = {
    # Project Leads
    "ansh.dixit@ethara.ai", "ayush.parasher@ethara.ai", "ayushmaan.dwivedi@ethara.ai",
    "bhavna.dhatrak@ethara.ai", "charitra.ethara@ethara.ai", "deepanshu.d@ethara.ai",
    "devansh.kakwani18@ethara.ai", "gunjit.arora@ethara.ai", "hardika@ethara.ai",
    "karun.raj@ethara.ai", "kumail.mujtaba@ethara.ai", "laxman.singh13@ethara.ai",
    "madhav.khode@ethara.ai", "mohammad.adnan@ethara.ai", "prashant.patel@ethara.ai",
    "saket.kukatkar@ethara.ai", "samarth.dubey@ethara.ai", "sapna.siradhna@ethara.ai",
    "saurabh.baghel@ethara.ai", "shashwat.suman@ethara.ai", "udit.parashar@ethara.ai",
    "ujala.singh@ethara.ai", "vatsal.jain@ethara.ai", "shreya.agrawal@ethara.ai",
    "sagun.rai@ethara.ai", "gaurav.siradhana@ethara.ai", "prince.mishra@ethara.ai",
    # Managers / not full-time taskers
    "sathvik.boorgu@ethara.ai", "nisarg.gandhi@ethara.ai", "ashutosh.sharma@ethara.ai",
    "vanshika.juneja@ethara.ai", "anjali.bhagoria@ethara.ai",
}

assert abs(W_HOURS + W_SUBS + W_AHT - 1.0) < 1e-9, "Scorecard weights must sum to 1.0"
assert MIN_SCOREABLE_TASKERS >= 2, "MIN_SCOREABLE_TASKERS must be >= 2"

# ──────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────

def _peek_dates(filepath):
    with open(filepath, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))

def percentile_ranks(arr):
    return np.array([stats.percentileofscore(arr, v, kind='rank') for v in arr])

def weighted_mean(score_weight_pairs):
    """Weighted mean of (score, weight) pairs. Returns None if list is empty."""
    if not score_weight_pairs:
        return None
    vals    = np.array([s for s, _ in score_weight_pairs], dtype=float)
    weights = np.array([w for _, w in score_weight_pairs], dtype=float)
    total_w = weights.sum()
    return float((vals * weights).sum() / total_w) if total_w > 0 else float(np.mean(vals))

# ──────────────────────────────────────────────────
# STEP 1: INGEST
# ──────────────────────────────────────────────────
# Returns (raw_task, raw_day).
#
# raw_task[(date, email, task)] retains the task dimension needed for
# project-cohort scoring of submissions and AHT.
#
# raw_day[(date, email)] aggregates across tasks for the hours score
# (hours are cross-project absolute, task dimension irrelevant) and
# for anomaly-day detection.

def ingest(filepath):
    raw_task = defaultdict(lambda: {"hours": 0.0, "submissions": 0, "aht_numer": 0.0})

    with open(filepath, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            email = row['Annotator Email'].strip().strip('"')
            try:
                hours = float(row['Total Hours'])
                subs  = int(float(row['Total Submissions']))
                ratio = float(row['Tasker AHT/Project AHT'].strip().replace('%', '')) / 100
            except (ValueError, KeyError):
                continue
            date = row['Date From'].strip()
            task = row['Task'].strip().strip('"')
            raw_task[(date, email, task)]["hours"]      += hours
            raw_task[(date, email, task)]["submissions"] += subs
            raw_task[(date, email, task)]["aht_numer"]  += subs * ratio

    for vals in raw_task.values():
        vals["aht_ratio"] = (
            vals["aht_numer"] / vals["submissions"] if vals["submissions"] > 0 else 1.0
        )

    # Aggregate to (date, email) for hours scoring and anomaly detection
    raw_day = defaultdict(lambda: {"hours": 0.0, "submissions": 0, "aht_numer": 0.0})
    for (date, email, task), vals in raw_task.items():
        raw_day[(date, email)]["hours"]      += vals["hours"]
        raw_day[(date, email)]["submissions"] += vals["submissions"]
        raw_day[(date, email)]["aht_numer"]  += vals["aht_ratio"] * vals["submissions"]
    for vals in raw_day.values():
        vals["aht_ratio"] = (
            vals["aht_numer"] / vals["submissions"] if vals["submissions"] > 0 else 1.0
        )

    return raw_task, raw_day

# ──────────────────────────────────────────────────
# STEP 2: IDENTIFY ANOMALY DAYS
# ──────────────────────────────────────────────────
# Anomaly days (team-wide hours < ANOMALY_THRESHOLD) are excluded from
# both hours and output scoring — something went wrong platform-wide.
#
# Sparse (date × task) cohorts (< MIN_SCOREABLE_TASKERS) are handled
# inline during scoring — they only suppress output scoring for that
# specific project day; hours scoring for those taskers is unaffected.

def find_anomaly_days(raw_day):
    daily = defaultdict(lambda: {"annotators": set(), "hours": 0.0})
    for (date, email), vals in raw_day.items():
        if email in LEADS:
            continue
        daily[date]["annotators"].add(email)
        daily[date]["hours"] += vals["hours"]

    anomaly_days = set()
    for date, d in daily.items():
        n   = len(d["annotators"])
        pct = d["hours"] / (n * HOURS_PER_TASKER_TARGET) if n > 0 else 0.0
        if pct < ANOMALY_THRESHOLD:
            anomaly_days.add(date)

    if anomaly_days:
        print(f"  Anomaly days excluded from scoring ({ANOMALY_THRESHOLD:.0%} threshold): "
              f"{', '.join(sorted(anomaly_days))}")
    return anomaly_days

# ──────────────────────────────────────────────────
# OUTPUT 1: DEDUPLICATED DAILY TASKER DATA
# ──────────────────────────────────────────────────

def pipeline_deduped(raw_task, out_path):
    # Aggregate to (date, email) but expose which tasks were worked
    day_data = defaultdict(lambda: {"hours": 0.0, "submissions": 0,
                                    "aht_numer": 0.0, "tasks": set()})
    for (date, email, task), vals in raw_task.items():
        day_data[(date, email)]["hours"]      += vals["hours"]
        day_data[(date, email)]["submissions"] += vals["submissions"]
        day_data[(date, email)]["aht_numer"]  += vals["aht_ratio"] * vals["submissions"]
        day_data[(date, email)]["tasks"].add(task)

    rows = []
    for (date, email), vals in day_data.items():
        aht = vals["aht_numer"] / vals["submissions"] if vals["submissions"] > 0 else 1.0
        rows.append({
            "date":         date,
            "email":        email,
            "is_lead":      "Yes" if email in LEADS else "No",
            "tasks":        "; ".join(sorted(vals["tasks"])),
            "hours":        round(vals["hours"], 2),
            "submissions":  vals["submissions"],
            "aht_ratio":    round(aht, 3),
            "vs_benchmark": f"{round((aht - 1) * 100, 1):+.1f}%",
        })
    rows.sort(key=lambda x: (x["date"], -x["hours"]))

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Date', 'Annotator Email', 'Is Lead', 'Tasks', 'Total Hours',
                         'Total Submissions', 'AHT Ratio vs Benchmark', 'vs Benchmark %'])
        for r in rows:
            writer.writerow([r["date"], r["email"], r["is_lead"], r["tasks"], r["hours"],
                             r["submissions"], r["aht_ratio"], r["vs_benchmark"]])

    print(f"  Deduped data  → {out_path}  ({len(rows)} rows)")

# ──────────────────────────────────────────────────
# PIPELINE 2: TEAM DAILY SUMMARY
# ──────────────────────────────────────────────────

def pipeline_daily_summary(raw_day, anomaly_days, out_path):
    daily = defaultdict(lambda: {"annotators": set(), "hours": 0.0, "lead_hours": 0.0})
    for (date, email), vals in raw_day.items():
        daily[date]["annotators"].add(email)
        daily[date]["hours"] += vals["hours"]
        if email in LEADS:
            daily[date]["lead_hours"] += vals["hours"]

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Date', 'Unique Annotators', 'Total Hours', 'Lead Hours', 'Target',
                         '% Target Achieved', 'Scoring Status'])
        for date in sorted(daily.keys()):
            n          = len(daily[date]["annotators"])
            hrs        = round(daily[date]["hours"], 2)
            lead_hrs   = round(daily[date]["lead_hours"], 2)
            target     = n * HOURS_PER_TASKER_TARGET
            pct        = round((hrs / target) * 100, 1) if target > 0 else 0.0
            status     = "Anomaly" if date in anomaly_days else ""
            writer.writerow([date, n, hrs, lead_hrs, target, f"{pct}%", status])

    print(f"  Pipeline 2    → {out_path}")

# ──────────────────────────────────────────────────
# PIPELINE 1: TASKER ROLLING SCORECARD
# ──────────────────────────────────────────────────

def pipeline_tasker_scorecard(raw_task, raw_day, anomaly_days, out_path, email_to_pod=None):
    all_dates       = sorted({date for (date, _) in raw_day})
    total_work_days = len(all_dates)

    # ── Hours scoring ────────────────────────────────────────────────────────
    # Fully absolute: min(daily_hours / target, 1.0) × 100.
    # Scored per (date, email) on all non-anomaly days.
    # No cohort needed — every tasker's score stands alone.
    hours_scores = {}   # (date, email) → h_score  [0, 100]
    for (date, email), vals in raw_day.items():
        if date in anomaly_days or email in LEADS:
            continue
        hours_scores[(date, email)] = min(vals["hours"] / HOURS_PER_TASKER_TARGET, 1.0) * 100

    # ── Output scoring ───────────────────────────────────────────────────────
    # Percentile rank within the (date × task) cohort for submissions and AHT.
    # Skips anomaly days, leads, and cohorts with < MIN_SCOREABLE_TASKERS.
    by_date_task = defaultdict(dict)   # (date, task) → {email: vals}
    for (date, email, task), vals in raw_task.items():
        if email in LEADS:
            continue
        by_date_task[(date, task)][email] = vals

    output_scores    = {}   # (date, email, task) → (s_pct, aht_pct)
    n_scored_cohorts = 0
    n_sparse_cohorts = 0
    for (date, task), cohort in by_date_task.items():
        if date in anomaly_days:
            continue
        if len(cohort) < MIN_SCOREABLE_TASKERS:
            n_sparse_cohorts += 1
            continue
        n_scored_cohorts += 1
        emails  = list(cohort.keys())
        s_arr   = np.array([cohort[e]["submissions"] for e in emails], dtype=float)
        aht_arr = np.array([cohort[e]["aht_ratio"]   for e in emails], dtype=float)
        s_pct   = percentile_ranks(s_arr)
        aht_pct = 100.0 - percentile_ranks(aht_arr)   # invert: lower ratio = better
        for i, email in enumerate(emails):
            output_scores[(date, email, task)] = (float(s_pct[i]), float(aht_pct[i]))

    # ── Day-level aggregated output scores ───────────────────────────────────
    # Aggregate task-level subs/AHT percentiles to (date, email) level so we
    # can form a single day_score per tasker per day.
    day_agg = {}   # (date, email) → (day_subs_pct, day_aht_pct)
    _day_output_tmp = defaultdict(lambda: {"s_w": [], "aht_w": []})
    for (date, email, task), (s_pct, aht_pct) in output_scores.items():
        weight = max(raw_task[(date, email, task)]["submissions"], 1)
        _day_output_tmp[(date, email)]["s_w"].append((s_pct, weight))
        _day_output_tmp[(date, email)]["aht_w"].append((aht_pct, weight))
    for (date, email), v in _day_output_tmp.items():
        s = weighted_mean(v["s_w"])
        a = weighted_mean(v["aht_w"])
        if s is not None and a is not None:
            day_agg[(date, email)] = (s, a)

    # ── Roll up per tasker ────────────────────────────────────────────────────
    summary = defaultdict(lambda: {
        "effort_scores":      [],    # h_score per non-anomaly day (for effort_score column)
        "s_scores_w":         [],    # (s_pct, weight) per scored (date, task) (for output_score column)
        "aht_scores_w":       [],    # (aht_pct, weight) per scored (date, task)
        "day_scores":         [],    # composite day_score for fully scored days (drives final/consistency/peak)
        "output_scored_dates": set(),
        "all_active_days":    set(),
        "total_hours_all":    0.0,
        "total_subs_all":     0,
        "aht_numer_all":      0.0,
    })

    # All-day totals — include anomaly days for accurate reporting
    for (date, email, task), vals in raw_task.items():
        summary[email]["all_active_days"].add(date)
        summary[email]["total_hours_all"]  += vals["hours"]
        summary[email]["total_subs_all"]   += vals["submissions"]
        summary[email]["aht_numer_all"]    += vals["aht_ratio"] * vals["submissions"]

    for (date, email), h_score in hours_scores.items():
        summary[email]["effort_scores"].append(h_score)
        # Day score requires fully scored day (hours + subs + AHT all available)
        if (date, email) in day_agg:
            s_pct, aht_pct = day_agg[(date, email)]
            summary[email]["day_scores"].append(
                W_HOURS * h_score + W_SUBS * s_pct + W_AHT * aht_pct
            )

    for (date, email, task), (s_pct, aht_pct) in output_scores.items():
        # Weight by submissions so high-volume task-days carry proportionally more signal
        weight = max(raw_task[(date, email, task)]["submissions"], 1)
        summary[email]["s_scores_w"].append((s_pct, weight))
        summary[email]["aht_scores_w"].append((aht_pct, weight))
        summary[email]["output_scored_dates"].add(date)

    rolling = []
    for email, vals in summary.items():
        is_lead = email in LEADS

        # Skip taskers who only ever worked on anomaly days. Leads always have empty
        # score lists by design (they're excluded from scoring above) but must still
        # appear as an unscored row, so they're never skipped here.
        if not is_lead and not vals["effort_scores"] and not vals["s_scores_w"]:
            continue

        active_days  = len(vals["all_active_days"])
        total_hours  = round(vals["total_hours_all"], 2)
        total_subs   = vals["total_subs_all"]
        overall_aht  = vals["aht_numer_all"] / total_subs if total_subs > 0 else 1.0

        avg_h = total_hours / active_days if active_days > 0 else 0.0
        avg_s = total_subs  / active_days if active_days > 0 else 0.0

        effort_scored_days  = len(vals["effort_scores"])
        output_scored_days  = len(vals["output_scored_dates"])

        day_scores_list = vals["day_scores"]
        n_day_scored    = len(day_scores_list)

        if is_lead:
            # Leads are never scored — blank rather than 0, so they don't read as
            # poor performers and can't be mistaken for a ranked tasker.
            final_score = effort_score = output_score = consistency_score = peak_score = None
            subs_score = 0.0   # only used for the (excluded) quadrant temp field
        else:
            effort_score = float(np.mean(vals["effort_scores"])) if vals["effort_scores"] else 0.0

            subs_score_raw = weighted_mean(vals["s_scores_w"])
            aht_score_raw  = weighted_mean(vals["aht_scores_w"])
            subs_score     = subs_score_raw if subs_score_raw is not None else 0.0
            aht_score      = aht_score_raw  if aht_score_raw  is not None else 0.0

            # Output Score: subs and AHT normalised to 0-100
            output_denom = W_SUBS + W_AHT
            output_score = (W_SUBS * subs_score + W_AHT * aht_score) / output_denom

            # ── Daily composite distribution ─────────────────────────────────────
            # final_score  = mean(day_scores)  where each day_score = W_H*h + W_S*s + W_A*a
            #                only includes days with full scoring (h + subs + AHT available)
            # consistency  = 100 - 2*std(day_scores) — higher = more predictable day-to-day
            # peak_score   = 90th-percentile of day_scores — best representative performance
            if day_scores_list:
                final_score       = float(np.mean(day_scores_list))
                peak_score        = float(np.percentile(day_scores_list, 90))
                consistency_score = max(0.0, 100.0 - 2.0 * float(np.std(day_scores_list))) \
                                    if n_day_scored >= 2 else 100.0
            else:
                # Fallback for taskers whose every active day had sparse cohorts
                final_score       = W_HOURS * effort_score
                peak_score        = W_HOURS * max(vals["effort_scores"], default=0.0)
                consistency_score = 0.0

        attendance_ratio = active_days / total_work_days

        _pod_info = (email_to_pod or {}).get(email, {})
        rolling.append({
            "email":               email,
            "is_lead":             is_lead,
            "pod_lead":            _pod_info.get("pl", ""),
            "pod_name":            _pod_info.get("pod_name", ""),
            "final_score":         round(final_score, 1)       if final_score       is not None else None,
            "effort_score":        round(effort_score, 1)      if effort_score      is not None else None,
            "output_score":        round(output_score, 1)      if output_score      is not None else None,
            "consistency_score":   round(consistency_score, 1) if consistency_score is not None else None,
            "peak_score":          round(peak_score, 1)        if peak_score        is not None else None,
            # good_days_pct and quadrant filled in the second pass below
            "attendance":          f"{round(attendance_ratio * 100, 1)}%",
            "active_days":         active_days,
            "daily_scored_days":   n_day_scored,
            "effort_scored_days":  effort_scored_days,
            "output_scored_days":  output_scored_days,
            "total_work_days":     total_work_days,
            "total_hours":         total_hours,
            "avg_hours_day":       round(avg_h, 2),
            "total_subs":          total_subs,
            "avg_subs_day":        round(avg_s, 1),
            "aht_ratio":           round(overall_aht, 3),
            "vs_benchmark":        f"{round((overall_aht - 1) * 100, 1):+.1f}%",
            "_subs_score":         subs_score,    # temp for quadrant
            "_day_scores":         day_scores_list,  # temp for good_days_pct
        })

    # ── Second pass: quadrant + good_days_pct ────────────────────────────────
    # Quadrant: relative 50/50 on both dimensions (team-median splits).
    # Leads are excluded from the medians so they can't shift the split for taskers.
    scored_rows      = [r for r in rolling if not r["is_lead"]]
    effort_median    = float(np.median([r["effort_score"] for r in scored_rows]))
    subs_median      = float(np.median([r["_subs_score"]  for r in scored_rows]))
    # Good Days: fraction of a tasker's day_scores that beat the team median day_score
    all_day_scores   = [s for r in scored_rows for s in r["_day_scores"]]
    team_day_median  = float(np.median(all_day_scores)) if all_day_scores else 50.0

    for r in rolling:
        if r["is_lead"]:
            r["quadrant"]      = "Lead"
            r["good_days_pct"] = None
            del r["_subs_score"]
            del r["_day_scores"]
            continue
        hi_h = r["effort_score"] >= effort_median
        hi_s = r["_subs_score"]  >= subs_median
        r["quadrant"] = (
            "Star"           if hi_h and hi_s else
            "Plodder"        if hi_h and not hi_s else
            "Sprinter"       if not hi_h and hi_s else
            "Underperformer"
        )
        ds = r["_day_scores"]
        r["good_days_pct"] = (
            round(sum(1 for s in ds if s > team_day_median) / len(ds) * 100, 1)
            if ds else 0.0
        )
        del r["_subs_score"]
        del r["_day_scores"]

    # Rank taskers by Final Score as before; leads are unranked and appended after,
    # sorted by email, so they never affect competitive ranking.
    taskers = [r for r in rolling if not r["is_lead"]]
    leads   = [r for r in rolling if r["is_lead"]]
    taskers.sort(key=lambda x: -x["final_score"])
    leads.sort(key=lambda x: x["email"])
    for rank, r in enumerate(taskers, 1):
        r["rank"] = rank
    for r in leads:
        r["rank"] = "Lead"
    rolling = taskers + leads

    def _fmt(v):
        return "" if v is None else v

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'Rank', 'Annotator Email', 'Is Lead', 'Pod Lead', 'Pod Name',
            'Final Score (0-100)', 'Effort Score (0-100)', 'Output Score (0-100)',
            'Consistency Score (0-100)', 'Peak Score (0-100)', 'Good Days %',
            'Attendance %', 'Active Days', 'Daily Scored Days',
            'Effort Scored Days', 'Output Scored Days', 'Total Working Days',
            'Total Hours', 'Avg Hours/Day',
            'Total Submissions', 'Avg Subs/Day',
            'AHT Ratio vs Benchmark', 'vs Benchmark %', 'Quadrant',
        ])
        for r in rolling:
            writer.writerow([
                r["rank"], r["email"], "Yes" if r["is_lead"] else "No",
                r["pod_lead"], r["pod_name"],
                _fmt(r["final_score"]), _fmt(r["effort_score"]), _fmt(r["output_score"]),
                _fmt(r["consistency_score"]), _fmt(r["peak_score"]), _fmt(r["good_days_pct"]),
                r["attendance"], r["active_days"], r["daily_scored_days"],
                r["effort_scored_days"], r["output_scored_days"], r["total_work_days"],
                r["total_hours"], r["avg_hours_day"],
                r["total_subs"], r["avg_subs_day"],
                r["aht_ratio"], r["vs_benchmark"], r["quadrant"],
            ])

    print(f"  Pipeline 1    → {out_path}")
    print(f"  Taskers scored: {len(taskers)}  |  Leads (unscored): {len(leads)}")
    print(f"  Days: {total_work_days} total | {total_work_days - len(anomaly_days)} scored "
          f"| {len(anomaly_days)} anomaly")
    print(f"  Task-day cohorts: {n_scored_cohorts} scored | {n_sparse_cohorts} sparse "
          f"(< {MIN_SCOREABLE_TASKERS} taskers)")
    print(f"  Day-score weights: Hours {W_HOURS:.0%} | Subs {W_SUBS:.0%} | AHT {W_AHT:.0%}"
          f"  →  Final = mean(day_scores)  |  team day-median: {team_day_median:.1f}")

    return rolling

# ──────────────────────────────────────────────────
# HTML DASHBOARD
# ──────────────────────────────────────────────────

def pipeline_html_report(rolling, period_str, out_path):
    """Emit a self-contained HTML performance dashboard for team sharing."""

    for rank, r in enumerate(rolling, 1):
        r["rank"] = rank

    taskers = []
    for r in rolling:
        taskers.append({
            "e":   r["email"],
            "rk":  r["rank"],
            "f":   r["final_score"],
            "ef":  r["effort_score"],
            "op":  r["output_score"],
            "cs":  r["consistency_score"],
            "pk":  r["peak_score"],
            "gd":  r["good_days_pct"],
            "q":   r["quadrant"],
            "at":  r["attendance"],
            "ad":  r["active_days"],
            "dsd": r["daily_scored_days"],
            "esd": r["effort_scored_days"],
            "osd": r["output_scored_days"],
            "wd":  r["total_work_days"],
            "th":  r["total_hours"],
            "ah":  r["avg_hours_day"],
            "ts":  r["total_subs"],
            "asv": r["avg_subs_day"],
            "ar":  r["aht_ratio"],
            "vb":  r["vs_benchmark"],
        })

    data_blob = _json.dumps({"period": period_str, "taskers": taskers}, separators=(',', ':'))
    html = _HTML_TEMPLATE.replace('__DATA__', data_blob).replace('__PERIOD__', period_str)

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  HTML dashboard → {out_path}")


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MM Performance Dashboard &middot; __PERIOD__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#F1F5F9;color:#1E293B;line-height:1.5}
a{color:inherit}
header{background:#1E293B;color:#fff;padding:18px 24px}
header h1{font-size:1.25rem;font-weight:700}
header p{font-size:0.82rem;color:#94A3B8;margin-top:3px}
.container{max-width:1120px;margin:0 auto;padding:24px 16px}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.1);padding:24px;margin-bottom:20px}
h2{font-size:1.05rem;font-weight:700;margin-bottom:6px}
h3{font-size:0.95rem;font-weight:700;margin-bottom:4px}
.subtitle{font-size:0.85rem;color:#64748B;margin-bottom:16px}
.search-row{display:flex;gap:10px;margin-top:14px}
.ta-wrap{flex:1;position:relative}
.ta-wrap input{width:100%;border:1.5px solid #CBD5E1;border-radius:8px;padding:10px 14px;font-size:15px;outline:none;transition:border-color .15s;background:#fff;color:#1E293B}
.ta-wrap input:focus{border-color:#FF6B35}
.ta-drop{position:absolute;top:calc(100% + 4px);left:0;right:0;background:#fff;border:1.5px solid #CBD5E1;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.12);z-index:100;max-height:260px;overflow-y:auto}
.ta-item{padding:9px 14px;font-size:14px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#1E293B}
.ta-item:hover,.ta-item.ta-active{background:#FFF4EF;color:#FF6B35}
.err{color:#EF4444;font-size:13px;margin-top:8px}
.hidden{display:none!important}
.team-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.t-box{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:16px;text-align:center}
.t-val{font-size:1.85rem;font-weight:700;color:#1E293B}
.t-lbl{font-size:0.73rem;color:#64748B;margin-top:4px;text-transform:uppercase;letter-spacing:.04em}
.quad-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.qd-box{border-radius:10px;padding:16px;text-align:center}
.qd-Star{background:#FEF3C7}.qd-Plodder{background:#DBEAFE}.qd-Sprinter{background:#D1FAE5}.qd-Underperformer{background:#F1F5F9}
.qd-val{font-size:1.85rem;font-weight:700}
.qd-lbl{font-size:0.73rem;margin-top:4px;text-transform:uppercase;letter-spacing:.04em;color:#475569}
.charts-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
.chart-card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.1);padding:20px}
.chart-note{font-size:0.8rem;color:#64748B;margin-top:8px;text-align:center}
.chart-wrap{height:280px;position:relative}
.score-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}
.score-box{background:#F8FAFC;border-radius:10px;padding:18px;text-align:center}
.score-num{font-size:2.5rem;font-weight:700;line-height:1}
.score-meta{font-size:0.72rem;color:#64748B;text-transform:uppercase;letter-spacing:.06em;margin-top:5px}
.tip-i{display:inline-flex;align-items:center;justify-content:center;width:13px;height:13px;border-radius:50%;background:#CBD5E1;color:#475569;font-size:.58rem;font-weight:700;margin-left:3px;cursor:help;vertical-align:middle;flex-shrink:0;line-height:1}
.tip-i:hover{background:#94A3B8}
.score-sub{font-size:0.82rem;color:#94A3B8;margin-top:5px}
.q-badge{display:inline-block;border-radius:20px;padding:4px 14px;font-size:0.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px}
.q-Star{background:#FEF3C7;color:#92400E}.q-Plodder{background:#DBEAFE;color:#1E40AF}
.q-Sprinter{background:#D1FAE5;color:#065F46}.q-Underperformer{background:#F1F5F9;color:#475569}
.q-msg{font-size:0.88rem;color:#475569;line-height:1.65;margin-bottom:18px}
.stats-mini{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;border-top:1px solid #E2E8F0;padding-top:16px}
.stat-item{display:flex;flex-direction:column}
.stat-lbl{font-size:0.68rem;color:#94A3B8;text-transform:uppercase;letter-spacing:.05em}
.stat-val{font-size:0.98rem;font-weight:600;color:#1E293B;margin-top:3px}
.guide-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:14px}
.guide-box{border-radius:10px;padding:16px}
.guide-box h4{font-size:0.88rem;font-weight:700;margin-bottom:6px}
.guide-box p{font-size:0.8rem;line-height:1.6;color:#475569}
.guide-Star{background:#FEF3C7}.guide-Plodder{background:#DBEAFE}
.guide-Sprinter{background:#D1FAE5}.guide-Underperformer{background:#F1F5F9}
details summary{cursor:pointer;font-weight:600;color:#475569;font-size:0.9rem;list-style:none;padding:2px 0}
details summary::before{content:"+ ";color:#FF6B35}
details[open] summary::before{content:"- ";color:#FF6B35}
.explainer p{margin-top:10px;font-size:0.85rem;line-height:1.75;color:#475569}
.explainer strong{color:#1E293B}
footer{text-align:center;padding:24px;font-size:0.8rem;color:#94A3B8}
@media(max-width:768px){
  .team-strip,.quad-strip,.guide-grid{grid-template-columns:repeat(2,1fr)}
  .charts-grid{grid-template-columns:1fr}
  .stats-mini{grid-template-columns:repeat(2,1fr)}
}
@media(max-width:480px){
  .score-grid{grid-template-columns:1fr}
  .search-row{flex-direction:column}
}
</style>
</head>
<body>
<header>
  <h1>&#x1F96D; Multimango Performance Dashboard</h1>
  <p>Period: __PERIOD__ &nbsp;&middot;&nbsp; Open this file in any browser &nbsp;&middot;&nbsp; Enter an email address to view scores</p>
</header>
<div class="container">

  <!-- Search -->
  <div class="card">
    <h2>Look up a performance card</h2>
    <div class="search-row">
      <div class="ta-wrap">
        <input type="text" id="searchInput" placeholder="Start typing an email address&hellip;" autocomplete="off" spellcheck="false" />
        <div id="taDrop" class="ta-drop hidden"></div>
      </div>
    </div>
    <p id="searchError" class="err hidden"></p>
  </div>

  <!-- Personal card (hidden until search) -->
  <div id="personalSection" class="hidden">
    <div id="personalCard"></div>
  </div>

  <!-- Team overview -->
  <div class="team-strip">
    <div class="t-box"><div class="t-val" id="totalTaskers">-</div><div class="t-lbl">Taskers scored</div></div>
    <div class="t-box"><div class="t-val" id="medianFinal">-</div><div class="t-lbl">Median final score</div></div>
    <div class="t-box"><div class="t-val" id="medianEffort">-</div><div class="t-lbl">Median effort score</div></div>
    <div class="t-box"><div class="t-val" id="medianOutput">-</div><div class="t-lbl">Median output score</div></div>
  </div>
  <div class="quad-strip">
    <div class="qd-box qd-Star"><div class="qd-val" id="cntStar">-</div><div class="qd-lbl">&#x2B50; Stars</div></div>
    <div class="qd-box qd-Plodder"><div class="qd-val" id="cntPlodder">-</div><div class="qd-lbl">&#x1F4AA; Plodders</div></div>
    <div class="qd-box qd-Sprinter"><div class="qd-val" id="cntSprinter">-</div><div class="qd-lbl">&#x26A1; Sprinters</div></div>
    <div class="qd-box qd-Underperformer"><div class="qd-val" id="cntUnder">-</div><div class="qd-lbl">&#x1F4C8; Developing</div></div>
  </div>

  <!-- Charts -->
  <div class="charts-grid">
    <div class="chart-card">
      <h3>Score Distribution</h3>
      <p class="subtitle">Final score spread across all taskers</p>
      <div class="chart-wrap"><canvas id="histChart"></canvas></div>
      <p id="histNote" class="chart-note hidden"></p>
    </div>
    <div class="chart-card">
      <h3>Effort vs Output</h3>
      <p class="subtitle">Each dot is one tasker &mdash; coloured by quadrant</p>
      <div class="chart-wrap"><canvas id="scatterChart"></canvas></div>
    </div>
  </div>

  <!-- Quadrant guide -->
  <div class="card">
    <h3>What does my quadrant mean?</h3>
    <p class="subtitle">Both splits are relative to the current team. Hours: above or below the team&rsquo;s median Effort Score. Submissions: above or below median rank within your project cohort. Each quadrant contains roughly 25% of the team.</p>
    <div class="guide-grid">
      <div class="guide-box guide-Star"><h4>&#x2B50; Star</h4><p>&ge;&nbsp;1.5h/day <em>and</em> above-median submissions in your project. Strong on both dimensions.</p></div>
      <div class="guide-box guide-Plodder"><h4>&#x1F4AA; Plodder</h4><p>&ge;&nbsp;1.5h/day but below-median submissions within your project cohort. Time is there &mdash; focus on throughput.</p></div>
      <div class="guide-box guide-Sprinter"><h4>&#x26A1; Sprinter</h4><p>Above-median submissions in your project but below 1.5h/day. More hours will multiply your score.</p></div>
      <div class="guide-box guide-Underperformer"><h4>&#x1F4C8; Developing</h4><p>Below both thresholds. Hours carry 50% of the score &mdash; improving daily attendance is the highest-leverage step.</p></div>
    </div>
  </div>

  <!-- How scores work -->
  <div class="card">
    <details>
      <summary>How are scores calculated?</summary>
      <div class="explainer">
        <p><strong>Day Score</strong> &mdash; computed each fully scored day: 0.70 &times; hours performance + 0.20 &times; submissions rank within project + 0.10 &times; speed rank within project.</p>
        <p><strong>Final Score</strong> &mdash; mean of your daily scores across all fully scored days. Hours carry 70% because they are the most universal and consistent signal.</p>
        <p><strong>Effort Score</strong> &mdash; hours-only, computed across all non-anomaly days including sparse-cohort days. Shown separately so you can see your raw hours signal independently of the combined score.</p>
        <p><strong>Output Score</strong> &mdash; project-relative performance (submissions + speed) normalised to 0&ndash;100. Diagnostic companion to Final Score.</p>
        <p><strong>Consistency Score</strong> &mdash; 100 &minus; 2&times;std(day scores). Higher means your performance is predictable day to day. Two identical Final Scores can have very different Consistency Scores.</p>
        <p><strong>Peak Score</strong> &mdash; your 90th-percentile day score. What you are capable of on a good day, independent of your average.</p>
        <p><strong>Good Days %</strong> &mdash; percentage of your scored days where your day score beat the team median. A direct read on how often you are competitive.</p>
        <p><strong>Anomaly days</strong> (team-wide hours below 15% of target) are excluded from scoring but still count in your totals &mdash; you showed up and that work is recorded.</p>
        <p><strong>Attendance %</strong> is informational only &mdash; it does not multiply your score. Taskers on short projects should not be penalised for simply not having more days assigned.</p>
        <p><strong>Quadrant</strong> uses two relative 50/50 splits derived from the current team&rsquo;s distribution. Hours: your Effort Score vs the team median (excludes anomaly days, averages daily scores, caps at 6h &mdash; more accurate than a raw cumulative average). Submissions: your project-cohort percentile vs the team-median project-cohort percentile. Both splits target roughly equal quadrant sizes (~25% each).</p>
      </div>
    </details>
  </div>

</div>
<footer>Multimango Performance Dashboard &middot; __PERIOD__ &middot; Scores are computed by the MM Performance Pipeline</footer>

<script>
const DATA = __DATA__;

const TIPS = {
  final:   'Mean of your daily scores across all fully scored days. Each day = 70% hours vs 6h target + 20% submissions rank within your project + 10% speed rank.',
  effort:  'Absolute hours performance: min(daily hours / 6h target, 100). Not compared to peers. 3h = 50 pts, 6h = 100 pts. Includes all non-anomaly days.',
  output:  'Your percentile rank on submissions and speed within your specific project on each day. Only compared to taskers on the same project on the same day.',
  consist: 'How stable your daily scores are: 100 minus 2x the standard deviation. Higher = more predictable. Informational — does not affect your rank.',
  peak:    'Your 90th-percentile daily score. What you achieve on a strong day. Compare to Final Score to see your headroom.',
  good:    'Percentage of your scored days where your day score beat the team median. 50% means you are competitive half the time.',
  quad:    'Based on where you sit vs the team median on two dimensions: effort (hours) and output (project submissions rank). Both thresholds come from the current team distribution.'
};
function ti(k){return '<span class="tip-i" data-tip="'+TIPS[k]+'">?</span>';}

let allFinals, allEfforts, allOutputs;
let scatterChart, histChart;

const QUAD_MSGS = {
  "Star": "You are strong on both hours and output — you are setting the pace. Keep the consistency going.",
  "Plodder": "Your Effort Score is above the team median, but your submissions rank below median within your project cohort. Focus on task throughput — the hours are there, use them to complete more tasks.",
  "Sprinter": "You rank above median in your project cohort on submissions, but your Effort Score is below the team median. Each additional working day multiplies your Effort Score significantly — attendance is your highest-leverage improvement.",
  "Underperformer": "Both tracks need work. Hours carry 50% of the final score — improving attendance and daily hours is the highest-impact step you can take right now. Once hours are up, focus on output within your project."
};
const QUAD_LABELS = {
  'Star': 'Star', 'Plodder': 'Plodder',
  'Sprinter': 'Sprinter', 'Underperformer': 'Developing'
};
const QUAD_COLORS = {
  'Star':          'rgba(245,158,11,0.65)',
  'Plodder':       'rgba(59,130,246,0.65)',
  'Sprinter':      'rgba(16,185,129,0.65)',
  'Underperformer':'rgba(148,163,184,0.55)'
};
const BIN_LABELS = ['0–10','10–20','20–30','30–40','40–50','50–60','60–70','70–80','80–90','90–100'];
const BASE_COLORS = [
  'rgba(239,68,68,.7)','rgba(249,115,22,.7)','rgba(249,115,22,.7)',
  'rgba(245,158,11,.7)','rgba(234,179,8,.7)','rgba(132,204,22,.7)',
  'rgba(34,197,94,.7)','rgba(16,185,129,.7)','rgba(16,185,129,.7)','rgba(16,185,129,.7)'
];

function med(arr) {
  const s = [...arr].sort((a,b)=>a-b);
  const m = Math.floor(s.length/2);
  return s.length%2===0 ? ((s[m-1]+s[m])/2).toFixed(1) : s[m].toFixed(1);
}
function betterThan(arr, val) {
  return Math.round(arr.filter(v => v < val).length / arr.length * 100);
}
function scoreColor(score, arr) {
  const p = betterThan(arr, score);
  return p >= 75 ? '#10B981' : p >= 25 ? '#F59E0B' : '#EF4444';
}

function init() {
  allFinals  = DATA.taskers.map(t => t.f);
  allEfforts = DATA.taskers.map(t => t.ef);
  allOutputs = DATA.taskers.map(t => t.op);

  document.getElementById('totalTaskers').textContent = DATA.taskers.length;
  document.getElementById('medianFinal').textContent  = med(allFinals);
  document.getElementById('medianEffort').textContent = med(allEfforts);
  document.getElementById('medianOutput').textContent = med(allOutputs);

  const qc = {Star:0,Plodder:0,Sprinter:0,Underperformer:0};
  DATA.taskers.forEach(t => qc[t.q]++);
  document.getElementById('cntStar').textContent       = qc['Star'];
  document.getElementById('cntPlodder').textContent    = qc['Plodder'];
  document.getElementById('cntSprinter').textContent   = qc['Sprinter'];
  document.getElementById('cntUnder').textContent      = qc['Underperformer'];

  initScatter(null);
  initHistogram(null);
}

function doSearch() {
  const raw = document.getElementById('searchInput').value.trim().toLowerCase();
  const t = DATA.taskers.find(t => t.e.toLowerCase() === raw);
  const errEl = document.getElementById('searchError');
  const sec   = document.getElementById('personalSection');

  if (!t) {
    errEl.textContent = 'Email not found — check spelling and try again.';
    errEl.classList.remove('hidden');
    sec.classList.add('hidden');
    updateScatter(null);
    updateHist(null);
    return;
  }
  errEl.classList.add('hidden');
  document.getElementById('personalCard').innerHTML = renderCard(t);
  sec.classList.remove('hidden');
  sec.scrollIntoView({behavior:'smooth', block:'nearest'});
  updateScatter(t);
  updateHist(t.f);
}

function renderCard(t) {
  const fc = scoreColor(t.f, allFinals);
  const ec = scoreColor(t.ef, allEfforts);
  const oc = scoreColor(t.op, allOutputs);
  const fb = betterThan(allFinals, t.f);
  const eb = betterThan(allEfforts, t.ef);
  const ob = betterThan(allOutputs, t.op);
  const impliedH = (t.ef / 100 * 6).toFixed(1);
  const ahtColor = t.ar <= 1.0 ? '#10B981' : '#EF4444';
  const noOutput = t.osd === 0;
  const ql = QUAD_LABELS[t.q] || t.q;

  const allConsistency = DATA.taskers.map(x => x.cs);
  const allPeaks       = DATA.taskers.map(x => x.pk);
  const cc = scoreColor(t.cs, allConsistency);
  const pc = scoreColor(t.pk, allPeaks);
  const cb = betterThan(allConsistency, t.cs);
  const pb = betterThan(allPeaks, t.pk);

  return '<div class="card">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:14px">'
    + '<span style="font-weight:700;font-size:1.05rem">' + t.e + '</span>'
    + '<span style="color:#64748B;font-size:0.88rem">Rank #' + t.rk + ' of ' + DATA.taskers.length + '</span>'
    + '</div>'
    + '<div class="score-grid">'
    + '<div class="score-box"><div class="score-num" style="color:' + fc + '">' + t.f + '</div>'
    + '<div class="score-meta">Final Score' + ti('final') + '</div>'
    + '<div class="score-sub">Better than ' + fb + '%</div></div>'
    + '<div class="score-box"><div class="score-num" style="color:' + ec + '">' + t.ef + '</div>'
    + '<div class="score-meta">Effort Score' + ti('effort') + '</div>'
    + '<div class="score-sub">≈ ' + impliedH + 'h/day &middot; better than ' + eb + '%</div></div>'
    + '<div class="score-box"><div class="score-num" style="color:' + oc + '">' + (noOutput ? '—' : t.op) + '</div>'
    + '<div class="score-meta">Output Score' + ti('output') + '</div>'
    + '<div class="score-sub">' + (noOutput ? 'No scored cohort' : 'Better than ' + ob + '%') + '</div></div>'
    + '<div class="score-box"><div class="score-num" style="color:' + cc + '">' + t.cs + '</div>'
    + '<div class="score-meta">Consistency' + ti('consist') + '</div>'
    + '<div class="score-sub">Better than ' + cb + '%</div></div>'
    + '<div class="score-box"><div class="score-num" style="color:' + pc + '">' + t.pk + '</div>'
    + '<div class="score-meta">Peak Score' + ti('peak') + '</div>'
    + '<div class="score-sub">90th pct &middot; better than ' + pb + '%</div></div>'
    + '<div class="score-box"><div class="score-num" style="color:#64748B">' + t.gd + '%</div>'
    + '<div class="score-meta">Good Days' + ti('good') + '</div>'
    + '<div class="score-sub">Days above team median</div></div>'
    + '</div>'
    + '<div><span class="q-badge q-' + t.q + '">' + ql + '</span></div>'
    + '<p class="q-msg">' + (QUAD_MSGS[t.q] || '') + '</p>'
    + '<div class="stats-mini">'
    + '<div class="stat-item"><span class="stat-lbl">Total Hours</span><span class="stat-val">' + t.th + 'h</span></div>'
    + '<div class="stat-item"><span class="stat-lbl">Avg Hours / Day</span><span class="stat-val">' + t.ah + 'h</span></div>'
    + '<div class="stat-item"><span class="stat-lbl">Total Submissions</span><span class="stat-val">' + t.ts.toLocaleString() + '</span></div>'
    + '<div class="stat-item"><span class="stat-lbl">Avg Subs / Day</span><span class="stat-val">' + t.asv + '</span></div>'
    + '<div class="stat-item"><span class="stat-lbl">Attendance</span><span class="stat-val">' + t.at + '</span></div>'
    + '<div class="stat-item"><span class="stat-lbl">Active Days</span><span class="stat-val">' + t.ad + ' / ' + t.wd + '</span></div>'
    + '<div class="stat-item"><span class="stat-lbl">AHT vs Benchmark</span><span class="stat-val" style="color:' + ahtColor + '">' + t.vb + '</span></div>'
    + '<div class="stat-item"><span class="stat-lbl">Daily Scored Days</span><span class="stat-val">' + t.dsd + '</span></div>'
    + '</div>'
    + '</div>';
}

// ── Scatter chart ──────────────────────────────────────────────────────────
function buildScatterDatasets(highlightEmail) {
  const quads = ['Star','Plodder','Sprinter','Underperformer'];
  const ql    = ['Star','Plodder','Sprinter','Developing'];
  const ds = quads.map((q, i) => ({
    label: ql[i],
    data: DATA.taskers.filter(t => t.q === q && t.e !== highlightEmail)
                      .map(t => ({x: t.ef, y: t.op, rk: t.rk})),
    backgroundColor: QUAD_COLORS[q],
    pointRadius: 5,
    pointHoverRadius: 7,
  }));
  if (highlightEmail) {
    const t = DATA.taskers.find(t => t.e === highlightEmail);
    if (t) ds.push({
      label: 'You',
      data: [{x: t.ef, y: t.op, rk: t.rk}],
      backgroundColor: '#FF6B35',
      borderColor: '#fff',
      borderWidth: 2,
      pointRadius: 11,
      pointHoverRadius: 13,
    });
  }
  return ds;
}

function initScatter(highlightEmail) {
  const ctx = document.getElementById('scatterChart').getContext('2d');
  if (scatterChart) scatterChart.destroy();
  scatterChart = new Chart(ctx, {
    type: 'scatter',
    data: {datasets: buildScatterDatasets(highlightEmail)},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        tooltip: {callbacks: {label: ctx => {
          const d = ctx.raw;
          return ctx.dataset.label === 'You' ? 'You — Rank #' + d.rk : 'Rank #' + d.rk;
        }}},
        legend: {position:'bottom', labels:{boxWidth:10, font:{size:11}}}
      },
      scales: {
        x: {min:0, max:100, title:{display:true, text:'Effort Score (hours vs 6h/day target)'}},
        y: {min:0, max:100, title:{display:true, text:'Output Score (vs project peers)'}}
      }
    }
  });
}

function updateScatter(t) {
  scatterChart.data.datasets = buildScatterDatasets(t ? t.e : null);
  scatterChart.update();
}

// ── Histogram ──────────────────────────────────────────────────────────────
function buildHistData(highlightScore) {
  const bins = Array(10).fill(0);
  DATA.taskers.forEach(t => bins[Math.min(Math.floor(t.f / 10), 9)]++);
  const userBin = highlightScore != null ? Math.min(Math.floor(highlightScore / 10), 9) : -1;
  const bg = BASE_COLORS.map((c, i) => i === userBin ? '#FF6B35' : c);
  const bd = bg.map((c, i) => i === userBin ? '#cc4a1a' : 'transparent');
  return {bins, bg, bd};
}

function initHistogram(highlightScore) {
  const ctx = document.getElementById('histChart').getContext('2d');
  if (histChart) histChart.destroy();
  const {bins, bg, bd} = buildHistData(highlightScore);
  histChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: BIN_LABELS,
      datasets: [{label:'Taskers', data:bins, backgroundColor:bg, borderColor:bd, borderWidth:2, borderRadius:5}]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: {display:false},
        tooltip: {callbacks: {label: ctx => ctx.raw + ' taskers'}}
      },
      scales: {
        x: {title:{display:true, text:'Final Score'}},
        y: {beginAtZero:true, title:{display:true, text:'Taskers'}}
      }
    }
  });
}

function updateHist(score) {
  const {bins, bg, bd} = buildHistData(score);
  histChart.data.datasets[0].backgroundColor = bg;
  histChart.data.datasets[0].borderColor = bd;
  histChart.update();
  const note = document.getElementById('histNote');
  if (score != null) {
    const t = DATA.taskers.find(t => t.f === score);
    const rk = t ? t.rk : '?';
    note.textContent = 'Your score: ' + score + ' (rank #' + rk + ' of ' + DATA.taskers.length + ')';
    note.classList.remove('hidden');
  } else {
    note.classList.add('hidden');
  }
}

// Typeahead
(function(){
  var inp = document.getElementById('searchInput');
  var drop = document.getElementById('taDrop');
  var filtered = [];
  var activeIdx = -1;

  function showDrop(items) {
    filtered = items; activeIdx = -1;
    if (!items.length) { drop.classList.add('hidden'); return; }
    var h = '';
    items.forEach(function(e, i) { h += '<div class="ta-item" data-idx="' + i + '">' + e + '</div>'; });
    drop.innerHTML = h;
    drop.classList.remove('hidden');
  }
  function hideDrop() { drop.classList.add('hidden'); activeIdx = -1; filtered = []; }
  function setActive(idx) {
    var items = drop.querySelectorAll('.ta-item');
    items.forEach(function(el) { el.classList.remove('ta-active'); });
    if (idx >= 0 && idx < items.length) { items[idx].classList.add('ta-active'); items[idx].scrollIntoView({block:'nearest'}); }
    activeIdx = idx;
  }
  function selectEmail(email) { inp.value = email; hideDrop(); doSearch(); }

  inp.addEventListener('input', function() {
    var q = this.value.trim().toLowerCase();
    if (!q) { hideDrop(); return; }
    var emails = DATA.taskers.map(function(t){ return t.e; });
    var prefix   = emails.filter(function(e){ return e.toLowerCase().startsWith(q); });
    var contains = emails.filter(function(e){ return !e.toLowerCase().startsWith(q) && e.toLowerCase().includes(q); });
    showDrop(prefix.concat(contains).slice(0, 10));
  });

  inp.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!drop.classList.contains('hidden')) setActive(Math.min(activeIdx + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (!drop.classList.contains('hidden')) setActive(Math.max(activeIdx - 1, 0));
    } else if (e.key === 'Enter') {
      if (activeIdx >= 0 && filtered[activeIdx]) { e.preventDefault(); selectEmail(filtered[activeIdx]); }
      else { hideDrop(); doSearch(); }
    } else if (e.key === 'Escape') {
      hideDrop();
    }
  });

  drop.addEventListener('mousedown', function(e) {
    var item = e.target.closest('.ta-item');
    if (item) { e.preventDefault(); selectEmail(filtered[+item.dataset.idx]); }
  });

  document.addEventListener('click', function(e) {
    if (!inp.contains(e.target) && !drop.contains(e.target)) hideDrop();
  });
})();

(function(){
  var T=document.createElement('div');
  T.style.cssText='position:fixed;background:#1E293B;color:#fff;padding:8px 11px;border-radius:7px;font-size:.72rem;line-height:1.45;max-width:240px;z-index:9999;pointer-events:none;display:none;box-shadow:0 4px 14px rgba(0,0,0,.25);white-space:normal';
  document.body.appendChild(T);
  function mv(e){if(T.style.display==='none')return;var x=e.clientX+14,y=e.clientY-8;T.style.left=x+'px';T.style.top=y+'px';var r=T.getBoundingClientRect();if(r.right>window.innerWidth-8)T.style.left=(e.clientX-r.width-14)+'px';if(r.bottom>window.innerHeight-8)T.style.top=(e.clientY-r.height-10)+'px';}
  document.addEventListener('mouseover',function(e){var el=e.target.closest('[data-tip]');if(el){T.textContent=el.dataset.tip;T.style.display='block';mv(e);}});
  document.addEventListener('mousemove',mv);
  document.addEventListener('mouseout',function(e){if(!e.relatedTarget||!e.relatedTarget.closest('[data-tip]'))T.style.display='none';});
})();

init();
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────
# MANAGEMENT HTML DASHBOARD
# ──────────────────────────────────────────────────

def pipeline_html_mgmt_report(rolling, raw_task, raw_day, anomaly_days, period_str, pod_rows, spotlight_result, out_path):
    """Management-facing dashboard: full leaderboard, quadrant, attention flags,
    team health, and project breakdown. All tasker emails visible."""

    # ── Performance tiers ─────────────────────────────────────────────────
    finals        = [r["final_score"]       for r in rolling]
    consis_scores = [r["consistency_score"] for r in rolling]
    p10           = float(np.percentile(finals, 10))
    p25           = float(np.percentile(finals, 25))
    p75           = float(np.percentile(finals, 75))
    p90           = float(np.percentile(finals, 90))
    team_median_f = float(np.median(finals))
    # volatile threshold = bottom-quartile consistency for this team (always relative)
    cons_p25      = float(np.percentile(consis_scores, 25))

    def get_tier(f):
        if f >= p90: return "Exceptional"
        if f >= p75: return "Strong"
        if f >= p25: return "Solid"
        if f >= p10: return "Developing"
        return "Needs Support"

    def get_flags(r, tier):
        flags = []
        if r["daily_scored_days"] < 5:
            return flags
        if tier == "Exceptional" and r["consistency_score"] >= 70:
            flags.append("recognize")
        if r["quadrant"] == "Sprinter" and r["output_score"] >= 60:
            flags.append("high_potential")
        if r["final_score"] >= team_median_f and r["consistency_score"] < cons_p25:
            flags.append("volatile")
        if (r["final_score"] < team_median_f
                and 15 <= r["good_days_pct"] < 45
                and tier != "Needs Support"):
            flags.append("develop")
        if tier == "Needs Support" and r["good_days_pct"] < 15:
            flags.append("critical")
        return flags

    # ── Tasker blob ────────────────────────────────────────────────────────
    taskers_data = []
    for r in rolling:
        tier  = get_tier(r["final_score"])
        flags = get_flags(r, tier)
        taskers_data.append({
            "e":    r["email"],
            "pl":   r.get("pod_lead", ""),
            "rk":   r.get("rank", 0),
            "f":    r["final_score"],
            "ef":   r["effort_score"],
            "op":   r["output_score"],
            "cs":   r["consistency_score"],
            "pk":   r["peak_score"],
            "gd":   r["good_days_pct"],
            "q":    r["quadrant"],
            "tier": tier,
            "flags":flags,
            "at":   r["attendance"],
            "ad":   r["active_days"],
            "dsd":  r["daily_scored_days"],
            "th":   r["total_hours"],
            "ah":   r["avg_hours_day"],
            "ts":   r["total_subs"],
            "asv":  r["avg_subs_day"],
            "ar":   r["aht_ratio"],
            "vb":   r["vs_benchmark"],
        })

    # ── Daily summary ──────────────────────────────────────────────────────
    daily_agg = defaultdict(lambda: {"taskers": set(), "hours": 0.0})
    for (date, email), vals in raw_day.items():
        daily_agg[date]["taskers"].add(email)
        daily_agg[date]["hours"] += vals["hours"]

    daily_data = []
    for date in sorted(daily_agg):
        d   = daily_agg[date]
        n   = len(d["taskers"])
        hrs = round(d["hours"], 1)
        tgt = n * HOURS_PER_TASKER_TARGET
        pct = round(hrs / tgt * 100, 1) if tgt > 0 else 0.0
        daily_data.append({
            "dt": date, "n": n, "hrs": hrs,
            "tgt": tgt, "pct": pct,
            "anom": date in anomaly_days,
        })

    # ── Project breakdown ──────────────────────────────────────────────────
    proj_agg = defaultdict(lambda: {
        "dates": set(), "taskers": set(),
        "hours": 0.0, "subs": 0, "aht_numer": 0.0,
    })
    for (date, email, task), vals in raw_task.items():
        p = proj_agg[task]
        p["dates"].add(date)
        p["taskers"].add(email)
        p["hours"]    += vals["hours"]
        p["subs"]     += vals["submissions"]
        p["aht_numer"] += vals["aht_ratio"] * vals["submissions"]

    project_data = []
    for task, p in sorted(proj_agg.items(), key=lambda x: -x[1]["hours"]):
        aht = p["aht_numer"] / p["subs"] if p["subs"] > 0 else 1.0
        dts = sorted(p["dates"])
        project_data.append({
            "nm":      task,
            "days":    len(p["dates"]),
            "taskers": len(p["taskers"]),
            "hrs":     round(p["hours"], 1),
            "subs":    p["subs"],
            "aht":     round(aht, 3),
            "start":   dts[0]  if dts else "",
            "end":     dts[-1] if dts else "",
        })

    pods_data = [{
        "rk":  r["rank"],
        "pl":  r["pl"],
        "nm":  r["pod_name"],
        "sz":  r["pod_size"],
        "sc":  r["scored"],
        "un":  r["unscored"],
        "cv":  r["coverage"],
        "af":  r["avg_final"],
        "mf":  r["med_final"],
        "ae":  r["avg_effort"],
        "ao":  r["avg_output"],
        "ac":  r["avg_consist"],
        "ap":  r["avg_peak"],
        "ag":  r["avg_good_days"],
        "th":  r["total_hours"],
        "ah":  r["avg_h_member"],
        "star":r["stars"],
        "plod":r["plodders"],
        "spr": r["sprinters"],
        "dev": r["developing"],
        "te":  r["top_email"],
        "tf":  r["top_score"],
        "be":  r["bot_email"],
        "bf":  r["bot_score"],
        "proj":r["projects"],
    } for r in (pod_rows or [])]

    data_blob = _json.dumps({
        "period":    period_str,
        "taskers":   taskers_data,
        "daily":     daily_data,
        "projects":  project_data,
        "pods":      pods_data,
        "spotlight": _build_spotlight_blob(spotlight_result),
    }, separators=(',', ':'))

    html = (_MGMT_HTML_TEMPLATE
            .replace('__DATA__', data_blob)
            .replace('__PERIOD__', period_str)
            .replace('__SPOTLIGHT_MIN_POOL__', str(SPOTLIGHT_MIN_POOL)))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  Management dashboard  → {out_path}")


_MGMT_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MM Management Dashboard &middot; __PERIOD__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#F1F5F9;color:#1E293B;font-size:13px}
.hdr{background:#1E293B;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.hdr-t{font-size:1rem;font-weight:700}.hdr-s{font-size:.73rem;color:#94A3B8;margin-top:3px}
.tabs{background:#fff;border-bottom:2px solid #E2E8F0;padding:0 24px;display:flex;overflow-x:auto}
.tab{padding:11px 15px;cursor:pointer;font-size:.8rem;font-weight:600;color:#64748B;border-bottom:2px solid transparent;margin-bottom:-2px;white-space:nowrap;transition:color .15s,border-color .15s}
.tab:hover{color:#FF6B35}.tab.active{color:#FF6B35;border-bottom-color:#FF6B35}
.pane{display:none;padding:16px 24px}.pane.active{display:block}
.wrap{max-width:1400px;margin:0 auto}
.card{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:16px;margin-bottom:14px}
.card-h{font-size:.72rem;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.g5{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
.kpi{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:14px;text-align:center}
.kpi-v{font-size:1.65rem;font-weight:800;line-height:1.1}.kpi-l{font-size:.65rem;color:#64748B;text-transform:uppercase;letter-spacing:.05em;margin-top:4px}
.tip-i{display:inline-flex;align-items:center;justify-content:center;width:13px;height:13px;border-radius:50%;background:#CBD5E1;color:#475569;font-size:.58rem;font-weight:700;margin-left:3px;cursor:help;vertical-align:middle;flex-shrink:0;line-height:1}
.tip-i:hover{background:#94A3B8}
.tier-bar{display:flex;height:26px;border-radius:6px;overflow:hidden;gap:1px;margin-bottom:8px}
.ts{display:flex;align-items:center;justify-content:center;font-size:.67rem;font-weight:700;color:#fff;min-width:14px;cursor:default}
.tleg{display:flex;flex-wrap:wrap;gap:8px;font-size:.7rem}
.tleg-i{display:flex;align-items:center;gap:4px}
.tleg-d{width:9px;height:9px;border-radius:2px;display:inline-block}
.qbox{border-radius:10px;padding:12px;text-align:center}
.qbox-v{font-size:1.5rem;font-weight:800}.qbox-l{font-size:.67rem;text-transform:uppercase;letter-spacing:.05em;margin-top:3px;font-weight:700}
.tbl-wrap{overflow-x:auto;border-radius:8px;border:1px solid #E2E8F0;max-height:60vh;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:.77rem}
thead{position:sticky;top:0;z-index:1;background:#F8FAFC}
th{padding:8px 9px;text-align:left;font-weight:700;color:#475569;cursor:pointer;white-space:nowrap;border-bottom:2px solid #E2E8F0;user-select:none}
th:hover{color:#FF6B35}th.sa::after{content:" ↑"}th.sd::after{content:" ↓"}
td{padding:6px 9px;border-bottom:1px solid #F1F5F9;white-space:nowrap;vertical-align:middle}
tr:hover>td{background:#FFF8F5}
.badge{display:inline-block;border-radius:10px;padding:2px 7px;font-size:.68rem;font-weight:700}
.fs{border:1px solid #E2E8F0;border-radius:10px;overflow:hidden;margin-bottom:8px}
.fh{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;cursor:pointer;background:#F8FAFC}
.fh:hover{background:#F1F5F9}.ft{font-weight:700;font-size:.82rem}
.fn{border-radius:10px;padding:2px 8px;font-size:.7rem;font-weight:700;color:#fff}
.fb{display:none;padding:12px 14px;border-top:1px solid #E2E8F0}.fb.open{display:block}
.fb-why{font-size:.78rem;color:#64748B;margin-bottom:8px;line-height:1.5}
.fb-act{font-size:.75rem;color:#1E293B;margin-bottom:10px;line-height:1.5}
.sbar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.sbar input,.sbar select{border:1.5px solid #CBD5E1;border-radius:7px;padding:6px 10px;font-size:.77rem;outline:none;background:#fff;color:#1E293B}
.sbar input:focus,.sbar select:focus{border-color:#FF6B35}.sbar input{min-width:180px}
.sbar-c{font-size:.73rem;color:#64748B;white-space:nowrap}
.ch{position:relative}
.anom-r{background:#FEF2F2!important}
@media(max-width:900px){.g5{grid-template-columns:repeat(3,1fr)}.g4{grid-template-columns:repeat(2,1fr)}.g2{grid-template-columns:1fr}}
@media(max-width:600px){.g5,.g4{grid-template-columns:repeat(2,1fr)}}
.sp-warn{background:#FFFBEB;border:1px solid #FCD34D;border-radius:8px;padding:9px 14px;font-size:.75rem;color:#92400E;margin-bottom:12px;display:flex;align-items:flex-start;gap:8px}
.sp-badge-top{background:#D1FAE5;color:#065F46;border-radius:10px;padding:2px 8px;font-size:.68rem;font-weight:700;white-space:nowrap}
.sp-badge-bot{background:#FEE2E2;color:#991B1B;border-radius:10px;padding:2px 8px;font-size:.68rem;font-weight:700;white-space:nowrap}
.sp-badge-none{color:#94A3B8;font-size:.68rem}
.sp-card-top{border-top:3px solid #10B981}
.sp-card-bot{border-top:3px solid #EF4444}
</style>
</head>
<body>
<div class="hdr">
  <div>
    <div class="hdr-t">&#x1F4CA; Multimango &mdash; Management Dashboard</div>
    <div class="hdr-s">Period: __PERIOD__ &nbsp;&middot;&nbsp; Confidential &mdash; management use only</div>
  </div>
  <div style="text-align:right">
    <div style="font-size:1.25rem;font-weight:800" id="hdrN">&mdash;</div>
    <div style="font-size:.68rem;color:#94A3B8">taskers active</div>
  </div>
</div>

<div class="tabs">
  <div class="tab active" data-pane="ov">Overview</div>
  <div class="tab" data-pane="lb">Leaderboard</div>
  <div class="tab" data-pane="qd">Quadrant Mix</div>
  <div class="tab" data-pane="fl">Attention Flags</div>
  <div class="tab" data-pane="th">Team Health</div>
  <div class="tab" data-pane="pr">Projects</div>
  <div class="tab" id="tab-pd" data-pane="pd" style="display:none">Pods</div>
  <div class="tab" data-pane="sp">Spotlight</div>
</div>

<div id="pane-ov" class="pane active"><div class="wrap">
  <div class="g5" style="margin-top:2px" id="kpiRow"></div>
  <div class="g2">
    <div class="card">
      <div class="card-h">Performance Tier Distribution</div>
      <div class="tier-bar" id="tierBar"></div>
      <div class="tleg" id="tierLeg"></div>
    </div>
    <div class="card">
      <div class="card-h">Quadrant Mix</div>
      <div class="g4" id="quadBoxes"></div>
    </div>
  </div>
  <div class="g2">
    <div class="card">
      <div class="card-h">Attention Flags &mdash; Quick Count</div>
      <div id="flagSumEl"></div>
    </div>
    <div class="card">
      <div class="card-h">Final Score Distribution</div>
      <div class="ch" style="height:175px"><canvas id="ovHist"></canvas></div>
    </div>
  </div>
  <div class="card">
    <div class="card-h">Top 10 Performers</div>
    <div class="tbl-wrap" style="max-height:none"><table id="ov10Tbl">
      <thead><tr>
        <th onclick="sortOv(0)">#</th><th onclick="sortOv(1)">Email</th><th onclick="sortOv(2)">Final</th><th onclick="sortOv(3)">Effort</th><th onclick="sortOv(4)">Output</th>
        <th onclick="sortOv(5)">Consistency</th><th onclick="sortOv(6)">Peak</th><th onclick="sortOv(7)">Good Days%</th><th onclick="sortOv(8)">Hrs/Day</th><th onclick="sortOv(9)">Quadrant</th><th onclick="sortOv(10)">Tier</th>
      </tr></thead>
      <tbody id="top10"></tbody>
    </table></div>
  </div>
</div></div>

<div id="pane-lb" class="pane"><div class="wrap">
  <div class="sbar" style="margin-top:4px">
    <input type="text" id="lbQ" placeholder="Filter by email&#x2026;" oninput="renderLB()">
    <select id="lbTier" onchange="renderLB()">
      <option value="">All tiers</option>
      <option>Exceptional</option><option>Strong</option><option>Solid</option>
      <option>Developing</option><option value="Needs Support">Needs Support</option>
    </select>
    <select id="lbQuad" onchange="renderLB()">
      <option value="">All quadrants</option>
      <option>Star</option><option>Plodder</option><option>Sprinter</option>
      <option value="Underperformer">Developing (quadrant)</option>
    </select>
    <span class="sbar-c" id="lbCnt"></span>
  </div>
  <div class="tbl-wrap"><table id="lbTbl">
    <thead><tr>
      <th onclick="sortLB(0)">#</th>
      <th onclick="sortLB(1)">Email</th>
      <th onclick="sortLB(2)">Final <span class="tip-i" data-tip="Mean of daily scores. Each day = 70% hours vs 6h target + 20% submissions rank within project + 10% speed rank.">?</span></th>
      <th onclick="sortLB(3)">Effort <span class="tip-i" data-tip="Absolute hours performance: min(daily hours / 6h target, 100). Not compared to peers. 3h = 50 pts, 6h = 100 pts.">?</span></th>
      <th onclick="sortLB(4)">Output <span class="tip-i" data-tip="Percentile rank on submissions and speed within each project cohort, normalised to 0-100. Only compared to taskers on the same project on the same day.">?</span></th>
      <th onclick="sortLB(5)">Consistency <span class="tip-i" data-tip="100 minus 2x the std of daily scores. Higher = more predictable day to day. Informational — does not affect rank.">?</span></th>
      <th onclick="sortLB(6)">Peak <span class="tip-i" data-tip="90th-percentile daily score. What this tasker achieves on a strong day. Compare to Final Score to see headroom.">?</span></th>
      <th onclick="sortLB(7)">Good Days% <span class="tip-i" data-tip="% of scored days where the tasker beat the team median day score. 50% means competitive half the time.">?</span></th>
      <th onclick="sortLB(8)">Hrs/Day <span class="tip-i" data-tip="Total hours divided by active days. Includes anomaly-day hours in the total; those days are excluded from scoring.">?</span></th>
      <th onclick="sortLB(9)">Total Hrs</th>
      <th onclick="sortLB(10)">Active Days</th>
      <th onclick="sortLB(11)">Quadrant <span class="tip-i" data-tip="Above/below team median on two axes: effort (hours) and output (project submissions rank). Both thresholds are derived from the current team distribution.">?</span></th>
      <th onclick="sortLB(12)">Tier <span class="tip-i" data-tip="Performance percentile band: Exceptional = top 10%, Strong = 10-25%, Solid = 25-75%, Developing = 75-90%, Needs Support = bottom 10%.">?</span></th>
      <th onclick="sortLB(13)">Pod Lead <span class="tip-i" data-tip="Pod lead responsible for this tasker, from the resource sheet.">?</span></th>
    </tr></thead>
    <tbody id="lbBody"></tbody>
  </table></div>
</div></div>

<div id="pane-qd" class="pane"><div class="wrap">
  <div class="g2">
    <div class="card">
      <div class="card-h">Effort Score vs Output Score</div>
      <p style="font-size:.73rem;color:#64748B;margin-bottom:8px">Each dot is a tasker. Hover to identify. Coloured by quadrant.</p>
      <div class="ch" style="height:360px"><canvas id="efOpSc"></canvas></div>
    </div>
    <div class="card">
      <div class="card-h">Consistency vs Peak Score</div>
      <p style="font-size:.73rem;color:#64748B;margin-bottom:8px">Top-right = reliable high performers. Top-left = volatile but capable.</p>
      <div class="ch" style="height:360px"><canvas id="cpSc"></canvas></div>
    </div>
  </div>
  <div class="card">
    <div class="card-h">Quadrant Playbook</div>
    <div class="g4" id="quadPlay"></div>
  </div>
</div></div>

<div id="pane-fl" class="pane"><div class="wrap">
  <p style="font-size:.78rem;color:#475569;margin:4px 0 12px">Flags are non-exclusive &mdash; a tasker can appear in multiple categories. Requires &ge; 5 daily-scored days. Each flag includes a recommended management action.</p>
  <div id="flagSecs"></div>
</div></div>

<div id="pane-th" class="pane"><div class="wrap">
  <div class="card">
    <div class="card-h">Daily Attendance &amp; Hours vs Target</div>
    <p style="font-size:.73rem;color:#64748B;margin-bottom:10px">Blue bars = normal days. Red bars = anomaly days excluded from all scoring.</p>
    <div class="ch" style="height:290px"><canvas id="healthCh"></canvas></div>
  </div>
  <div class="card">
    <div class="card-h">Day-by-Day Summary</div>
    <div class="tbl-wrap" style="max-height:none"><table id="healthTbl">
      <thead><tr>
        <th onclick="sortHth(0)">Date</th><th onclick="sortHth(1)">Taskers</th><th onclick="sortHth(2)">Total Hrs</th><th onclick="sortHth(3)">Target</th><th onclick="sortHth(4)">% of Target</th><th onclick="sortHth(5)">Status</th>
      </tr></thead>
      <tbody id="healthBody"></tbody>
    </table></div>
  </div>
</div></div>

<div id="pane-pr" class="pane"><div class="wrap">
  <div class="sbar" style="margin-top:4px">
    <input type="text" id="prQ" placeholder="Filter projects&#x2026;" oninput="renderPr()">
    <span class="sbar-c" id="prCnt"></span>
  </div>
  <div class="card">
    <div class="card-h">Project / Task Breakdown</div>
    <div class="tbl-wrap" style="max-height:none"><table id="prTbl">
      <thead><tr>
        <th onclick="sortPr(0)">Project / Task</th>
        <th onclick="sortPr(1)">Days Active</th>
        <th onclick="sortPr(2)">Taskers</th>
        <th onclick="sortPr(3)">Total Hours</th>
        <th onclick="sortPr(4)">Total Subs</th>
        <th onclick="sortPr(5)">AHT vs Benchmark</th>
        <th onclick="sortPr(6)">Hrs / Tasker / Day</th>
        <th onclick="sortPr(7)">Date Range</th>
      </tr></thead>
      <tbody id="prBody"></tbody>
    </table></div>
  </div>
</div></div>

<div id="pane-pd" class="pane"><div class="wrap">
  <div class="g5" style="margin-top:2px" id="podKpiRow"></div>
  <div class="card" style="margin-top:16px">
    <div class="card-h">Pod Leaderboard</div>
    <div style="background:#FFFBEB;border:1px solid #FCD34D;border-radius:8px;padding:9px 14px;margin-bottom:12px;font-size:.75rem;color:#92400E;display:flex;align-items:flex-start;gap:8px">
      <span style="font-size:1rem;line-height:1.3">&#9888;</span>
      <span><strong>Faded rows</strong> have fewer than 3 scored members or below 25% pod coverage in this period. Their averages may not represent the full pod &mdash; check the <strong>Coverage %</strong> and <strong>Scored / Size</strong> columns before drawing conclusions.</span>
    </div>
    <div class="sbar">
      <input type="text" id="pdQ" placeholder="Filter by pod lead or location&#x2026;" oninput="renderPd()" style="min-width:280px">
      <span class="sbar-c" id="pdCnt"></span>
    </div>
    <div class="tbl-wrap" style="max-height:none"><table id="pdTbl">
      <thead><tr>
        <th onclick="sortPd(0)">#</th>
        <th onclick="sortPd(1)">Pod Lead</th>
        <th onclick="sortPd(2)">Location</th>
        <th onclick="sortPd(3)">Projects <span class="tip-i" data-tip="Current projects assigned to this pod from the resource sheet. A pod may span multiple projects.">?</span></th>
        <th onclick="sortPd(4)">Scored / Size <span class="tip-i" data-tip="Taskers with performance data this period / total members listed under this pod lead in the resource sheet.">?</span></th>
        <th onclick="sortPd(5)">Coverage % <span class="tip-i" data-tip="Percentage of pod members with data in this period. Pods below 25% or fewer than 3 scored members are faded — their averages may not reflect the full pod.">?</span></th>
        <th onclick="sortPd(6)">Avg Final <span class="tip-i" data-tip="Mean Final Score across all scored pod members. Most reliable when Coverage is high. Sorted by this column by default.">?</span></th>
        <th onclick="sortPd(7)">Med Final <span class="tip-i" data-tip="Median Final Score within the pod. More robust than the mean for small or uneven pods.">?</span></th>
        <th onclick="sortPd(8)">Avg Effort <span class="tip-i" data-tip="Mean Effort Score (hours vs 6h/day target) across pod members. Pure hours — not compared across pods.">?</span></th>
        <th onclick="sortPd(9)">Avg Output <span class="tip-i" data-tip="Mean Output Score (project-cohort submissions + speed rank) across pod members.">?</span></th>
        <th onclick="sortPd(10)">Consistency <span class="tip-i" data-tip="Mean Consistency Score across pod members. 100 minus 2x std of daily scores per person, then averaged. Higher = more predictable output.">?</span></th>
        <th onclick="sortPd(11)">Good Days % <span class="tip-i" data-tip="Mean of individual Good Days % across pod members. Share of scored days where each tasker beat the team median day score.">?</span></th>
        <th onclick="sortPd(12)">Total Hrs</th>
        <th onclick="sortPd(13)">Stars <span class="tip-i" data-tip="Above team median on both effort (hours) and output (submissions quality). The ideal quadrant.">?</span></th>
        <th onclick="sortPd(14)">Plodders <span class="tip-i" data-tip="High hours effort but below-median output. Engaged but not yet producing efficiently.">?</span></th>
        <th onclick="sortPd(15)">Sprinters <span class="tip-i" data-tip="Above-median output but below-median hours. Good output when tasking but lower overall commitment.">?</span></th>
        <th onclick="sortPd(16)">Developing <span class="tip-i" data-tip="Below team median on both hours and output. Priority for coaching.">?</span></th>
        <th onclick="sortPd(17)">Top Performer <span class="tip-i" data-tip="Pod member with the highest Final Score.">?</span></th>
        <th onclick="sortPd(18)">Bottom Performer <span class="tip-i" data-tip="Pod member with the lowest Final Score. Check attendance and quadrant before intervening.">?</span></th>
      </tr></thead>
      <tbody id="pdBody"></tbody>
    </table></div>
  </div>
</div></div>

<div id="pane-sp" class="pane"><div class="wrap">
  <div id="spWarning"></div>
  <div class="g4" style="margin-top:4px" id="spKpiRow"></div>
  <div style="margin-top:14px" id="spTables"></div>
  <div class="card" id="spFullCard" style="margin-top:14px">
    <div class="card-h">All Eligible Taskers — Ranked by Day Score</div>
    <div class="tbl-wrap"><table id="spTbl">
      <thead><tr>
        <th onclick="sortSp(0)">#</th>
        <th onclick="sortSp(1)">Email</th>
        <th onclick="sortSp(2)">Pod Lead</th>
        <th onclick="sortSp(3)">Day Score <span class="tip-i" data-tip="Composite score for this day only: 70% hours vs 6h target + 20% submissions rank + 10% speed rank. Same formula as the rolling scorecard.">?</span></th>
        <th onclick="sortSp(4)">Effort (Day) <span class="tip-i" data-tip="min(hours / 6h target, 1) × 100. Independent of peers.">?</span></th>
        <th onclick="sortSp(5)">Output (Day) <span class="tip-i" data-tip="Weighted submissions + speed percentile within project cohort, normalised 0–100. Blank if cohort < 10 taskers.">?</span></th>
        <th onclick="sortSp(6)">Hours</th>
        <th onclick="sortSp(7)">Subs</th>
        <th onclick="sortSp(8)">Note</th>
        <th onclick="sortSp(9)">Spotlight</th>
        <th onclick="sortSp(10)">Rolling Rank <span class="tip-i" data-tip="This tasker's rank in the full-period rolling scorecard. Gives context on whether today's result is typical.">?</span></th>
        <th onclick="sortSp(11)">Rolling Score</th>
      </tr></thead>
      <tbody id="spBody"></tbody>
    </table></div>
  </div>
</div></div>

<footer style="text-align:center;padding:14px;font-size:.7rem;color:#94A3B8">
  Multimango Management Dashboard &middot; __PERIOD__ &middot; Confidential
</footer>

<script>
const DATA = __DATA__;

const TC = {Exceptional:"#10B981",Strong:"#3B82F6",Solid:"#94A3B8",Developing:"#F59E0B","Needs Support":"#EF4444"};
const QC = {
  Star:          {bg:"rgba(245,158,11,.65)",  box:"#FEF3C7", txt:"#92400E"},
  Plodder:       {bg:"rgba(59,130,246,.65)",  box:"#DBEAFE", txt:"#1E40AF"},
  Sprinter:      {bg:"rgba(16,185,129,.65)",  box:"#D1FAE5", txt:"#065F46"},
  Underperformer:{bg:"rgba(148,163,184,.5)",  box:"#F1F5F9", txt:"#475569"}
};
const FLAGS = {
  recognize:      {icon:"&#11088;", label:"Recognize Now",           color:"#10B981",
    why:"Exceptional tier (top 10%) with consistent performance (Consistency &ge; 70). Public recognition retains top talent and models the standard for the rest of the team.",
    act:"Send a personalised acknowledgement. Nominate for a team spotlight. Explore expanded responsibilities or a mentoring role."},
  high_potential: {icon:"&#128640;", label:"High Potential",         color:"#3B82F6",
    why:"Sprinter quadrant: strong project output score (&ge; 60) but below-median hours. The ceiling is high &mdash; a commitment conversation could unlock significant additional contribution.",
    act:"One-on-one: explore barriers to more hours. Co-create a 30-day hours commitment goal with a scheduled check-in."},
  volatile:       {icon:"&#9889;", label:"Coach: Consistency",       color:"#F59E0B",
    why:"Above-median final score but consistency in the bottom quartile for this team. Capable of strong output but unpredictable day-to-day. Root cause is often task-context switching or external distractions.",
    act:"Identify the pattern on low-scoring days. Is it a specific project type, day of week, or time window? Set a floor expectation and review weekly."},
  develop:        {icon:"&#128200;", label:"Development Plan",        color:"#6366F1",
    why:"Below-median final score but some competitive days (15&ndash;45% good days). Not checked out &mdash; coachable. The gap between their good days and average days is the coaching insight.",
    act:"Run a structured 30/60/90 plan. Focus on replicating what they do on their good days. Set measurable fortnightly milestones."},
  critical:       {icon:"&#128308;", label:"Needs Immediate Attention", color:"#EF4444",
    why:"Bottom 10% (Needs Support tier) with rarely competitive days (&lt; 15% good days). Persistent underperformance with enough data to rule out noise.",
    act:"Direct manager conversation this week. Set a measurable 2-week improvement target with specific metrics. Document the discussion."}
};
const T_ORDER = ["Exceptional","Strong","Solid","Developing","Needs Support"];

function f1(n){return typeof n==="number"?n.toFixed(1):String(n);}
function med(a){if(!a.length)return 0;const s=[...a].sort((x,y)=>x-y),m=Math.floor(s.length/2);return s.length%2===0?(s[m-1]+s[m])/2:s[m];}
function sc(v){return v>=70?"#10B981":v>=45?"#F59E0B":"#EF4444";}
function qLabel(q){return q==="Underperformer"?"Developing":q;}
function qBadge(q){const c=QC[q]||QC.Underperformer;return "<span class=\\"badge\\" style=\\"background:"+c.box+";color:"+c.txt+"\\">"+qLabel(q)+"</span>";}
function tBadge(t){return "<span class=\\"badge\\" style=\\"background:"+TC[t]+";color:#fff\\">"+t+"</span>";}
function sCell(v){return "<td style=\\"font-weight:600;color:"+sc(v)+"\\">"+ f1(v)+"</td>";}

document.querySelectorAll(".tab").forEach(tab=>{
  tab.addEventListener("click",()=>{
    document.querySelectorAll(".tab").forEach(t=>t.classList.remove("active"));
    document.querySelectorAll(".pane").forEach(p=>p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById("pane-"+tab.dataset.pane).classList.add("active");
  });
});

function initOv(){
  const T=DATA.taskers;
  document.getElementById("hdrN").textContent=T.length;
  const kpis=[
    {v:T.length,         l:"Total Taskers"},
    {v:med(T.map(t=>t.f)).toFixed(1), l:"Median Final Score"},
    {v:med(T.map(t=>t.cs)).toFixed(1),l:"Median Consistency"},
    {v:med(T.map(t=>t.gd)).toFixed(1)+"%",l:"Median Good Days"},
    {v:T.filter(t=>t.q==="Star").length,   l:"Star Quadrant"}
  ];
  const kr=document.getElementById("kpiRow");
  kpis.forEach(k=>{
    const d=document.createElement("div");d.className="kpi";
    d.innerHTML="<div class=\\"kpi-v\\">"+k.v+"</div><div class=\\"kpi-l\\">"+k.l+"</div>";
    kr.appendChild(d);
  });
  const tc={};T_ORDER.forEach(t=>tc[t]=0);
  T.forEach(t=>tc[t.tier]=(tc[t.tier]||0)+1);
  const bar=document.getElementById("tierBar"),leg=document.getElementById("tierLeg");
  T_ORDER.forEach(tier=>{
    const n=tc[tier],pct=n/T.length*100;
    const seg=document.createElement("div");seg.className="ts";
    seg.style.flex=String(n);seg.style.background=TC[tier];
    seg.title=tier+": "+n+" ("+pct.toFixed(1)+"%)";
    if(pct>4)seg.textContent=n;
    bar.appendChild(seg);
    const li=document.createElement("div");li.className="tleg-i";
    li.innerHTML="<div class=\\"tleg-d\\" style=\\"background:"+TC[tier]+"\\"></div>"+tier+" ("+n+")";
    leg.appendChild(li);
  });
  const qIcons={Star:"&#11088;",Plodder:"&#128170;",Sprinter:"&#9889;",Underperformer:"&#128200;"};
  const qEl=document.getElementById("quadBoxes");
  ["Star","Plodder","Sprinter","Underperformer"].forEach(q=>{
    const n=T.filter(t=>t.q===q).length,c=QC[q];
    const d=document.createElement("div");d.className="qbox";d.style.background=c.box;
    d.innerHTML="<div class=\\"qbox-v\\" style=\\"color:"+c.txt+"\\">"+n+"</div>"
      +"<div class=\\"qbox-l\\" style=\\"color:"+c.txt+"\\">"+qIcons[q]+" "+qLabel(q)+"</div>";
    qEl.appendChild(d);
  });
  const fEl=document.getElementById("flagSumEl");
  Object.entries(FLAGS).forEach(([k,cfg])=>{
    const n=T.filter(t=>t.flags.includes(k)).length;
    const d=document.createElement("div");
    d.style.cssText="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid #F1F5F9";
    d.innerHTML="<span style=\\"font-size:.79rem\\">"+cfg.icon+" "+cfg.label+"</span>"
      +"<span style=\\"background:"+cfg.color+";color:#fff;border-radius:10px;padding:2px 8px;font-size:.7rem;font-weight:700\\">"+n+"</span>";
    fEl.appendChild(d);
  });
  const bins=Array(10).fill(0);
  T.forEach(t=>bins[Math.min(Math.floor(t.f/10),9)]++);
  new Chart(document.getElementById("ovHist").getContext("2d"),{
    type:"bar",
    data:{labels:["0","10","20","30","40","50","60","70","80","90"],
      datasets:[{data:bins,backgroundColor:bins.map((_,i)=>i<=2?"rgba(239,68,68,.7)":i<=5?"rgba(245,158,11,.7)":"rgba(16,185,129,.7)"),borderRadius:3}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{title:ctx=>"Score "+ctx[0].label+"–18"+(+ctx[0].label+10)}}},
      scales:{x:{title:{display:true,text:"Final Score",font:{size:10}}},y:{beginAtZero:true}}}
  });
  OV_DATA=DATA.taskers.slice(0,10);
  renderOv10();
  document.querySelectorAll("#ov10Tbl th").forEach((th,i)=>{if(i===OVC)th.classList.add(OVA?"sa":"sd");});
}

let OVC=0,OVA=true;
const OV_KEYS=["rk","e","f","ef","op","cs","pk","gd","ah","q","tier"];
let OV_DATA=[];
function renderOv10(){
  let rows=[...OV_DATA];
  const k=OV_KEYS[OVC];
  if(k)rows.sort((a,b)=>typeof a[k]==="number"?(OVA?a[k]-b[k]:b[k]-a[k]):(OVA?String(a[k]).localeCompare(String(b[k])):String(b[k]).localeCompare(String(a[k]))));
  let _html="";
  rows.forEach(t=>{
    _html+="<tr><td>"+t.rk+"</td>"
      +"<td style='font-family:monospace;font-size:.7rem'>"+t.e+"</td>"
      +sCell(t.f)+sCell(t.ef)+sCell(t.op)+sCell(t.cs)+sCell(t.pk)
      +"<td>"+t.gd+"%</td><td>"+t.ah+"h</td>"
      +"<td>"+qBadge(t.q)+"</td><td>"+tBadge(t.tier)+"</td></tr>";
  });
  document.getElementById("top10").innerHTML=_html;
}
window.sortOv=function(col){
  if(OVC===col)OVA=!OVA;else{OVC=col;OVA=col<=1;}
  document.querySelectorAll("#ov10Tbl th").forEach((th,i)=>{th.classList.remove("sa","sd");if(i===col)th.classList.add(OVA?"sa":"sd");});
  renderOv10();
};

let LBC=0,LBA=true;
const LBKEYS=["rk","e","f","ef","op","cs","pk","gd","ah","th","ad","q","tier","pl"];
function renderLB(){
  const q=(document.getElementById("lbQ").value||"").toLowerCase();
  const tf=document.getElementById("lbTier").value;
  const qf=document.getElementById("lbQuad").value;
  let rows=DATA.taskers.filter(t=>{
    if(q&&!t.e.toLowerCase().includes(q)&&!(t.pl||"").toLowerCase().includes(q))return false;
    if(tf&&t.tier!==tf)return false;
    if(qf&&t.q!==qf)return false;
    return true;
  });
  const k=LBKEYS[LBC];
  rows.sort((a,b)=>{
    const va=a[k],vb=b[k];
    return typeof va==="number"?(LBA?va-vb:vb-va):(LBA?String(va).localeCompare(String(vb)):String(vb).localeCompare(String(va)));
  });
  const tb=document.getElementById("lbBody");tb.innerHTML="";
  rows.forEach(t=>{
    const tr=document.createElement("tr");
    tr.innerHTML="<td>"+t.rk+"</td>"
      +"<td style=\\"font-family:monospace;font-size:.7rem\\">"+t.e+"</td>"
      +sCell(t.f)+sCell(t.ef)+sCell(t.op)+sCell(t.cs)+sCell(t.pk)
      +"<td>"+t.gd+"%</td><td>"+t.ah+"h</td><td>"+t.th+"h</td><td>"+t.ad+"</td>"
      +"<td>"+qBadge(t.q)+"</td><td>"+tBadge(t.tier)+"</td>"
      +"<td style=\\"font-family:monospace;font-size:.7rem\\">"+(t.pl||"—")+"</td>";
    tb.appendChild(tr);
  });
  document.getElementById("lbCnt").textContent=rows.length+" of "+DATA.taskers.length+" taskers";
}
function sortLB(col){
  if(LBC===col)LBA=!LBA;else{LBC=col;LBA=col<=1;}
  document.querySelectorAll("#lbTbl th").forEach((th,i)=>{th.classList.remove("sa","sd");if(i===col)th.classList.add(LBA?"sa":"sd");});
  renderLB();
}

function initQd(){
  const T=DATA.taskers;
  const qs=["Star","Plodder","Sprinter","Underperformer"];
  const ql=["Star","Plodder","Sprinter","Developing"];
  function mkDS(xk,yk){
    return qs.map((q,i)=>({
      label:ql[i],
      data:T.filter(t=>t.q===q).map(t=>({x:t[xk],y:t[yk],e:t.e,rk:t.rk,f:t.f})),
      backgroundColor:QC[q].bg,pointRadius:4,pointHoverRadius:7
    }));
  }
  function sOpts(xl,yl){
    return {responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:"bottom",labels:{boxWidth:9,font:{size:10}}},
        tooltip:{callbacks:{label:ctx=>{const d=ctx.raw;return[d.e,"Rank #"+d.rk,"Final: "+f1(d.f),xl+": "+f1(d.x),yl+": "+f1(d.y)];}}}},
      scales:{x:{min:0,max:100,title:{display:true,text:xl,font:{size:11}}},
              y:{min:0,max:100,title:{display:true,text:yl,font:{size:11}}}}};
  }
  new Chart(document.getElementById("efOpSc").getContext("2d"),{type:"scatter",data:{datasets:mkDS("ef","op")},options:sOpts("Effort Score","Output Score")});
  new Chart(document.getElementById("cpSc").getContext("2d"),{type:"scatter",data:{datasets:mkDS("cs","pk")},options:sOpts("Consistency Score","Peak Score")});
  const pb=[
    {q:"Star",      icon:"&#11088;",  play:"Recognize and retain. Assign stretch work: complex tasks, peer mentoring, project leads. Public acknowledgement signals what the organisation values."},
    {q:"Plodder",   icon:"&#128170;", play:"Coach on throughput and task quality. Are they on the right project type? Consider pairing with a Star for knowledge transfer. Review their AHT trend."},
    {q:"Sprinter",  icon:"&#9889;",   play:"Commitment conversation. Explore what prevents more hours. Co-create a specific daily hours goal. Frame as unlocking already-demonstrated potential."},
    {q:"Underperformer",icon:"&#128200;",play:"Hours coaching first (70% of score). Then output. Distinguish volatile from uniformly low using Consistency Score. Set 2-week measurable milestones."}
  ];
  const pe=document.getElementById("quadPlay");
  pb.forEach(p=>{
    const c=QC[p.q];
    const d=document.createElement("div");
    d.className="qbox";d.style.cssText="background:"+c.box+";text-align:left;padding:14px";
    d.innerHTML="<div style=\\"font-weight:700;margin-bottom:6px;color:"+c.txt+"\\">"+p.icon+" "+qLabel(p.q)+"</div>"
      +"<div style=\\"font-size:.73rem;color:#475569;line-height:1.5\\">"+p.play+"</div>";
    pe.appendChild(d);
  });
}

const FL_DATA={},FL_SORT={};
function renderFlagTbl(k){
  const ts=FL_DATA[k]||[],st=FL_SORT[k]||{c:0,a:true};
  const ks=["rk","e","f","ef","op","cs","pk","gd","ah","q","tier"];
  let rows=[...ts],ki=ks[st.c];
  if(ki)rows.sort((a,b)=>typeof a[ki]==="number"?(st.a?a[ki]-b[ki]:b[ki]-a[ki]):(st.a?String(a[ki]).localeCompare(String(b[ki])):String(b[ki]).localeCompare(String(a[ki]))));
  let h="";
  rows.forEach(t=>{
    h+="<tr><td>"+t.rk+"</td>"
      +"<td style='font-family:monospace;font-size:.7rem'>"+t.e+"</td>"
      +sCell(t.f)+sCell(t.ef)+sCell(t.op)+sCell(t.cs)+sCell(t.pk)
      +"<td>"+t.gd+"%</td><td>"+t.ah+"h</td>"
      +"<td>"+qBadge(t.q)+"</td><td>"+tBadge(t.tier)+"</td></tr>";
  });
  document.getElementById("flagTbl-"+k).querySelector("tbody").innerHTML=h;
}
window.sortFlag=function(th){
  const k=th.dataset.k,col=+th.dataset.c;
  if(!FL_SORT[k])FL_SORT[k]={c:0,a:true};
  const st=FL_SORT[k];
  if(st.c===col)st.a=!st.a;else{st.c=col;st.a=col<=1;}
  document.getElementById("flagTbl-"+k).querySelectorAll("th").forEach((t,i)=>{t.classList.remove("sa","sd");if(i===col)t.classList.add(st.a?"sa":"sd");});
  renderFlagTbl(k);
};

function initFlags(){
  const el=document.getElementById("flagSecs");
  Object.entries(FLAGS).forEach(([k,cfg])=>{
    const taskers=DATA.taskers.filter(t=>t.flags.includes(k));
    let tbl="";
    if(taskers.length){
      FL_DATA[k]=taskers;FL_SORT[k]={c:0,a:true};
      const flCols=["#","Email","Final","Effort","Output","Consistency","Peak","Good Days%","Hrs/Day","Quadrant","Tier"];
      let flHd="";
      flCols.forEach((c,i)=>flHd+="<th onclick='sortFlag(this)' data-k='"+k+"' data-c='"+i+"'>"+c+"</th>");
      tbl="<div class='tbl-wrap' style='max-height:320px'><table id='flagTbl-"+k+"'><thead><tr>"+flHd+"</tr></thead><tbody></tbody></table></div>";
    }else{
      tbl="<p style=\\"color:#64748B;font-size:.78rem\\">No taskers in this category with sufficient scored data.</p>";
    }
    const sec=document.createElement("div");sec.className="fs";
    sec.innerHTML="<div class=\\"fh\\" onclick=\\"this.nextElementSibling.classList.toggle('open')\\">"
      +"<span class=\\"ft\\">"+cfg.icon+" "+cfg.label+"</span>"
      +"<span class=\\"fn\\" style=\\"background:"+cfg.color+"\\">"+taskers.length+"</span></div>"
      +"<div class=\\"fb\\">"
      +"<p class=\\"fb-why\\">"+cfg.why+"</p>"
      +"<p class=\\"fb-act\\"><strong>Recommended action:</strong> "+cfg.act+"</p>"
      +tbl+"</div>";
    el.appendChild(sec);
    if(taskers.length){renderFlagTbl(k);document.getElementById("flagTbl-"+k).querySelector("th").classList.add("sa");}
  });
}

let HTC=0,HTA=true;
const HTH_KEYS=["dt","n","hrs","tgt","pct","anom"];
let HT_DATA=[];
function renderHealth(){
  let rows=[...HT_DATA];
  const k=HTH_KEYS[HTC];
  rows.sort((a,b)=>{
    const va=a[k],vb=b[k];
    if(typeof va==="number")return HTA?va-vb:vb-va;
    if(typeof va==="boolean")return HTA?(va?1:0)-(vb?1:0):(vb?1:0)-(va?1:0);
    return HTA?String(va).localeCompare(String(vb)):String(vb).localeCompare(String(va));
  });
  let _html="";
  rows.forEach(d=>{
    _html+="<tr"+(d.anom?" class='anom-r'":"")+">"
      +"<td>"+d.dt+"</td><td>"+d.n+"</td><td>"+d.hrs+"h</td><td>"+d.tgt+"h</td>"
      +"<td style='font-weight:600;color:"+(d.pct>=50?"#10B981":d.pct>=25?"#F59E0B":"#EF4444")+"'>"
      +d.pct+"%</td>"
      +"<td>"+(d.anom?"<span style='color:#EF4444;font-weight:700'>Anomaly &mdash; excluded from scoring</span>":"")+"</td></tr>";
  });
  document.getElementById("healthBody").innerHTML=_html;
}
window.sortHth=function(col){
  if(HTC===col)HTA=!HTA;else{HTC=col;HTA=col===0;}
  document.querySelectorAll("#healthTbl th").forEach((th,i)=>{th.classList.remove("sa","sd");if(i===col)th.classList.add(HTA?"sa":"sd");});
  renderHealth();
};

function initHealth(){
  const D=DATA.daily;
  const colors=D.map(d=>d.anom?"rgba(239,68,68,.8)":"rgba(59,130,246,.6)");
  new Chart(document.getElementById("healthCh").getContext("2d"),{
    data:{labels:D.map(d=>d.dt.slice(5)),datasets:[
      {type:"bar",  label:"Active Taskers",   data:D.map(d=>d.n),  backgroundColor:colors,yAxisID:"y", order:2,borderRadius:3},
      {type:"line", label:"Hours % of Target",data:D.map(d=>d.pct),
        borderColor:"#FF6B35",backgroundColor:"transparent",pointRadius:3,tension:0.3,yAxisID:"y2",order:1}
    ]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:"bottom",labels:{boxWidth:9,font:{size:10}}},
        tooltip:{callbacks:{title:ctx=>D[ctx[0].dataIndex].dt}}},
      scales:{
        y: {beginAtZero:true,title:{display:true,text:"Taskers",font:{size:10}},position:"left"},
        y2:{beginAtZero:true,max:110,title:{display:true,text:"Hours %",font:{size:10}},position:"right",grid:{drawOnChartArea:false}}
      }}
  });
  HT_DATA=D;
  renderHealth();
  document.querySelectorAll("#healthTbl th").forEach((th,i)=>{if(i===HTC)th.classList.add(HTA?"sa":"sd");});
}

let PRC=3,PRA=false;
const PRKEYS=["nm","days","taskers","hrs","subs","aht",null,"start"];
function renderPr(){
  const q=(document.getElementById("prQ").value||"").toLowerCase();
  let rows=DATA.projects.filter(p=>!q||p.nm.toLowerCase().includes(q));
  const k=PRKEYS[PRC];
  if(k)rows.sort((a,b)=>typeof a[k]==="number"?(PRA?a[k]-b[k]:b[k]-a[k]):(PRA?String(a[k]).localeCompare(String(b[k])):String(b[k]).localeCompare(String(a[k]))));
  const tb=document.getElementById("prBody");tb.innerHTML="";
  rows.forEach(p=>{
    const hptd=(p.days&&p.taskers)?(p.hrs/(p.days*p.taskers)).toFixed(2)+"h":"&mdash;";
    const ap=((p.aht-1)*100).toFixed(1);
    const tr=document.createElement("tr");
    tr.innerHTML="<td style=\\"max-width:260px;overflow:hidden;text-overflow:ellipsis\\" title=\\""+p.nm+"\\">"+p.nm+"</td>"
      +"<td>"+p.days+"</td><td>"+p.taskers+"</td><td>"+p.hrs+"h</td>"
      +"<td>"+p.subs.toLocaleString()+"</td>"
      +"<td style=\\"font-weight:600;color:"+(p.aht<=1?"#10B981":"#EF4444")+"\\">"+( ap>=0?"+":"")+ap+"%</td>"
      +"<td>"+hptd+"</td>"
      +"<td style=\\"font-size:.7rem;color:#64748B\\">"+p.start+" &rarr; "+p.end+"</td>";
    tb.appendChild(tr);
  });
  document.getElementById("prCnt").textContent=rows.length+" projects";
}
function sortPr(col){
  if(PRC===col)PRA=!PRA;else{PRC=col;PRA=col===0||col===7;}
  document.querySelectorAll("#prTbl th").forEach((th,i)=>{th.classList.remove("sa","sd");if(i===col)th.classList.add(PRA?"sa":"sd");});
  renderPr();
}

let PDC=6,PDA=false;
const PDSORT=["rk","pl","nm",null,"sc","cv","af","mf","ae","ao","ac","ag","th","star","plod","spr","dev","tf","bf"];

function initPd(){
  if(!DATA.pods||!DATA.pods.length)return;
  document.getElementById('tab-pd').style.display='';
  const rel=DATA.pods.filter(p=>p.cv>=25&&p.sc>=3);
  const kd=[
    {v:DATA.pods.length, l:'Pods Active'},
    {v:rel.length?rel[0].af.toFixed(1):'—', l:'Best Pod Score'},
    {v:rel.length?med(rel.map(p=>p.af)).toFixed(1):'—', l:'Median Pod Score'},
    {v:DATA.pods.filter(p=>p.cv>=100).length, l:'Full-Coverage Pods'},
    {v:DATA.pods.reduce((s,p)=>s+p.th,0).toFixed(1)+'h', l:'Total Pod Hours'},
  ];
  const kr=document.getElementById('podKpiRow');
  kd.forEach(k=>{
    const d=document.createElement('div');d.className='kpi';
    d.innerHTML='<div class="kpi-v">'+k.v+'</div><div class="kpi-l">'+k.l+'</div>';
    kr.appendChild(d);
  });
  renderPd();
}

function renderPd(){
  const q=(document.getElementById('pdQ').value||'').toLowerCase();
  let rows=DATA.pods.filter(p=>!q||p.pl.toLowerCase().includes(q)||p.nm.toLowerCase().includes(q));
  const k=PDSORT[PDC];
  if(k) rows.sort((a,b)=>{
    const va=a[k],vb=b[k];
    if(typeof va==='number') return PDA?va-vb:vb-va;
    return PDA?String(va).localeCompare(String(vb)):String(vb).localeCompare(String(va));
  });
  const tb=document.getElementById('pdBody');tb.innerHTML='';
  rows.forEach(p=>{
    const low=p.cv<25||p.sc<3;
    const cvCol=p.cv>=75?'#10B981':p.cv>=25?'#F59E0B':'#EF4444';
    const tr=document.createElement('tr');
    if(low)tr.style.opacity='0.55';
    const projHTML=(p.proj||[]).map(pr=>'<div style="background:#EFF6FF;color:#1D4ED8;border-radius:3px;padding:1px 5px;margin-bottom:2px;font-size:.64rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+pr+'">'+pr+'</div>').join('')||'<span style="color:#94A3B8">—</span>';
    tr.innerHTML='<td>'+p.rk+'</td>'
      +'<td style="font-family:monospace;font-size:.69rem">'+p.pl+'</td>'
      +'<td style="font-size:.73rem;color:#64748B;max-width:180px;overflow:hidden;text-overflow:ellipsis" title="'+p.nm+'">'+p.nm+'</td>'
      +'<td style="width:180px;max-width:180px;overflow:hidden">'+projHTML+'</td>'
      +'<td style="font-weight:600">'+p.sc+' / '+p.sz+'</td>'
      +'<td style="font-weight:700;color:'+cvCol+'">'+p.cv+'%</td>'
      +sCell(p.af)+sCell(p.mf)+sCell(p.ae)+sCell(p.ao)+sCell(p.ac)
      +'<td>'+p.ag+'%</td>'
      +'<td>'+p.th+'h</td>'
      +'<td style="font-weight:700;color:#F59E0B">'+p.star+'</td>'
      +'<td>'+p.plod+'</td>'
      +'<td style="color:#10B981;font-weight:600">'+p.spr+'</td>'
      +'<td style="color:#EF4444">'+p.dev+'</td>'
      +'<td style="font-family:monospace;font-size:.67rem">'+p.te+'<br><span style="color:#10B981;font-weight:600">'+p.tf+'</span></td>'
      +'<td style="font-family:monospace;font-size:.67rem">'+p.be+'<br><span style="color:#EF4444;font-weight:600">'+p.bf+'</span></td>';
    tb.appendChild(tr);
  });
  document.getElementById('pdCnt').textContent=rows.length+' pods';
}

function sortPd(col){
  if(!PDSORT[col])return;
  if(PDC===col)PDA=!PDA;else{PDC=col;PDA=col<=2;}
  document.querySelectorAll('#pdTbl th').forEach((th,i)=>{th.classList.remove('sa','sd');if(i===col)th.classList.add(PDA?'sa':'sd');});
  renderPd();
}

const SPOTLIGHT_MIN_POOL_JS = __SPOTLIGHT_MIN_POOL__;

function initSp(){
  const S = DATA.spotlight;
  const warn = document.getElementById('spWarning');

  function showUnavailable(msg){
    warn.innerHTML = '<div class="sp-warn"><span style="font-size:1rem">&#9888;</span><span>' + msg + '</span></div>';
    document.getElementById('spKpiRow').style.display = 'none';
    document.getElementById('spTables').style.display = 'none';
    document.getElementById('spFullCard').style.display = 'none';
  }

  if(!S || !S.enabled){
    const msgs = {
      not_run:      'Daily Spotlight was not run in this pipeline session.',
      all_anomalies:'All days in this input are anomaly days — no spotlight scoring was possible.',
    };
    showUnavailable(msgs[S && S.reason] || 'Spotlight unavailable.');
    return;
  }

  if(S.date_was_anomaly)
    warn.innerHTML += '<div class="sp-warn"><span style="font-size:1rem">&#9888;</span>'
      + '<span>Latest calendar date <strong>' + S.cal_date + '</strong> is an anomaly day. '
      + 'Spotlight uses <strong>' + S.date + '</strong> (last scoreable day).</span></div>';

  if(S.pool_too_small)
    warn.innerHTML += '<div class="sp-warn"><span style="font-size:1rem">&#9888;</span>'
      + '<span>Only <strong>' + S.active + '</strong> eligible taskers on ' + S.date
      + ' (minimum is ' + SPOTLIGHT_MIN_POOL_JS + '). Spotlight labels not assigned — pool is too small.</span></div>';

  if(S.pool_thin && !S.pool_too_small)
    warn.innerHTML += '<div class="sp-warn"><span style="font-size:1rem">&#9888;</span>'
      + '<span>Today&#39;s eligible pool (<strong>' + S.active + '</strong>) is below 50% of the period average '
      + '(<strong>' + S.period_avg + '</strong>). Labels are assigned but treat them with caution.</span></div>';

  const kpis = [
    {v: S.date,   l: 'Spotlight Date'},
    {v: S.active, l: 'Eligible Taskers'},
    {v: S.top_cutoff != null ? S.top_cutoff.toFixed(1) : '—', l: 'Top 10% Cutoff'},
    {v: S.bot_cutoff != null ? S.bot_cutoff.toFixed(1) : '—', l: 'Bottom 10% Cutoff'},
  ];
  const kr = document.getElementById('spKpiRow');
  kpis.forEach(k => {
    const d = document.createElement('div'); d.className = 'kpi';
    d.innerHTML = '<div class="kpi-v">' + k.v + '</div><div class="kpi-l">' + k.l + '</div>';
    kr.appendChild(d);
  });

  function spBadge(sp){
    if(sp === 'Recognition Candidate')  return '<span class="sp-badge-top">' + sp + '</span>';
    if(sp === 'Check-in Recommended')   return '<span class="sp-badge-bot">' + sp + '</span>';
    if(sp === 'Insufficient pool')      return '<span class="sp-badge-none">' + sp + '</span>';
    return '<span class="sp-badge-none">—</span>';
  }

  const MINIKEYS = ['dr','e','pl','ds','hr','rr','rf'];
  const miniSort = {top:{c:3,a:false}, bot:{c:3,a:false}};
  const miniData = {top:[], bot:[]};
  function miniRows(which){
    const ms = miniSort[which];
    let rows = [...miniData[which]];
    rows.sort((a,b) => {
      const mk = MINIKEYS[ms.c], va = a[mk], vb = b[mk];
      if(typeof va === 'number' || va == null){
        const na = va != null ? va : -Infinity, nb = vb != null ? vb : -Infinity;
        return ms.a ? na - nb : nb - na;
      }
      return ms.a ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });
    let h = '';
    rows.forEach(r => {
      h += '<tr><td>' + r.dr + '</td>'
        + '<td style="font-family:monospace;font-size:.69rem">' + r.e + '</td>'
        + '<td style="font-size:.71rem">' + (r.pl || '—') + '</td>'
        + sCell(r.ds)
        + '<td>' + r.hr + 'h</td>'
        + '<td style="color:#64748B">' + (r.rr != null ? '#' + r.rr : '—') + '</td>'
        + '<td style="font-weight:600;color:' + sc(r.rf != null ? r.rf : 0) + '">'
          + (r.rf != null ? f1(r.rf) : '—') + '</td>'
        + '</tr>';
    });
    return h;
  }
  window.sortMini = function(th){
    const which = th.dataset.w, col = +th.dataset.c;
    const ms = miniSort[which];
    if(ms.c === col) ms.a = !ms.a; else { ms.c = col; ms.a = col <= 2; }
    const sfx = which === 'top' ? 'Top' : 'Bot';
    document.getElementById('spMini' + sfx).querySelectorAll('th').forEach((t,i) => {
      t.classList.remove('sa','sd'); if(i === col) t.classList.add(ms.a ? 'sa' : 'sd');
    });
    document.getElementById('spMini' + sfx + 'Body').innerHTML = miniRows(which);
  };
  function miniTbl(which, title, cssClass, emptyMsg){
    const rows = miniData[which];
    if(!rows.length)
      return '<div class="card ' + cssClass + '"><div class="card-h">' + title + '</div>'
        + '<p style="font-size:.78rem;color:#64748B">' + emptyMsg + '</p></div>';
    const sfx = which === 'top' ? 'Top' : 'Bot';
    const lbs = ['#','Email','Pod Lead','Day Score','Hours','Rolling Rank','Rolling Score'];
    let hdr = '';
    lbs.forEach((l,i) => hdr += '<th onclick="sortMini(this)" data-w="' + which + '" data-c="' + i + '">' + l + '</th>');
    return '<div class="card ' + cssClass + '"><div class="card-h">' + title
      + ' (' + rows.length + ')</div><div class="tbl-wrap">'
      + '<table id="spMini' + sfx + '"><thead><tr>' + hdr + '</tr></thead>'
      + '<tbody id="spMini' + sfx + 'Body">' + miniRows(which) + '</tbody>'
      + '</table></div></div>';
  }
  const tops = S.rows.filter(r => r.sp === 'Recognition Candidate');
  const bots = S.rows.filter(r => r.sp === 'Check-in Recommended');
  const noLbl = S.pool_too_small ? 'Insufficient pool.' : 'No taskers in this category.';
  miniData.top = tops; miniData.bot = bots;
  document.getElementById('spTables').innerHTML =
    miniTbl('top', '&#x1F3C6; Recognition Candidates', 'sp-card-top', noLbl)
    + miniTbl('bot', '&#x1F4CB; Check-in Recommended', 'sp-card-bot', noLbl);
  ['top','bot'].forEach(function(w){
    const sfx = w === 'top' ? 'Top' : 'Bot';
    const tbl = document.getElementById('spMini' + sfx);
    if(tbl){ const ms = miniSort[w]; tbl.querySelectorAll('th').forEach((th,i) => { if(i === ms.c) th.classList.add(ms.a ? 'sa' : 'sd'); }); }
  });

  let SPC = 3, SPA = false;
  const SPKEYS = ['dr','e','pl','ds','ef','op','hr','sb','sn','sp','rr','rf'];
  function renderSp(){
    let rows = [...S.rows];
    rows.sort((a,b) => {
      const va = a[SPKEYS[SPC]], vb = b[SPKEYS[SPC]];
      if(typeof va === 'number' || va == null){
        const na = va != null ? va : -Infinity, nb = vb != null ? vb : -Infinity;
        return SPA ? na - nb : nb - na;
      }
      return SPA ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });
    const tb = document.getElementById('spBody');
    let _html = '';
    rows.forEach(r => {
      _html += '<tr>'
        + '<td>' + r.dr + '</td>'
        + '<td style="font-family:monospace;font-size:.69rem">' + r.e + '</td>'
        + '<td style="font-size:.71rem">' + (r.pl || '—') + '</td>'
        + sCell(r.ds) + sCell(r.ef)
        + '<td style="font-weight:600;color:' + sc(r.op != null ? r.op : 0) + '">'
          + (r.op != null ? f1(r.op) : '—') + '</td>'
        + '<td>' + r.hr + 'h</td>'
        + '<td>' + (r.sb != null ? r.sb.toLocaleString() : '—') + '</td>'
        + '<td style="font-size:.68rem;color:#94A3B8">' + (r.sn || '') + '</td>'
        + '<td>' + spBadge(r.sp) + '</td>'
        + '<td style="color:#64748B">' + (r.rr != null ? '#' + r.rr : '—') + '</td>'
        + '<td style="font-weight:600;color:' + sc(r.rf != null ? r.rf : 0) + '">'
          + (r.rf != null ? f1(r.rf) : '—') + '</td>'
        + '</tr>';
    });
    tb.innerHTML = _html;
  }
  window.sortSp = function(col){
    if(SPC === col) SPA = !SPA; else { SPC = col; SPA = col <= 1; }
    document.querySelectorAll('#spTbl th').forEach((th,i) => {
      th.classList.remove('sa','sd'); if(i === col) th.classList.add(SPA ? 'sa' : 'sd');
    });
    renderSp();
  };
  renderSp();
  document.querySelectorAll('#spTbl th').forEach((th,i) => { if(i===SPC) th.classList.add(SPA?'sa':'sd'); });
}

initOv(); renderLB();
document.querySelectorAll("#lbTbl th").forEach((th,i)=>{if(i===LBC)th.classList.add(LBA?"sa":"sd");});
initQd(); initFlags(); initHealth(); renderPr();
document.querySelectorAll("#prTbl th").forEach((th,i)=>{if(i===PRC)th.classList.add(PRA?"sa":"sd");});
initPd();
document.querySelectorAll("#pdTbl th").forEach((th,i)=>{if(i===PDC)th.classList.add(PDA?"sa":"sd");});
initSp();

(function(){
  var T=document.createElement('div');
  T.style.cssText='position:fixed;background:#1E293B;color:#fff;padding:8px 11px;border-radius:7px;font-size:.72rem;line-height:1.45;max-width:240px;z-index:9999;pointer-events:none;display:none;box-shadow:0 4px 14px rgba(0,0,0,.25);white-space:normal';
  document.body.appendChild(T);
  function mv(e){if(T.style.display==='none')return;var x=e.clientX+14,y=e.clientY-8;T.style.left=x+'px';T.style.top=y+'px';var r=T.getBoundingClientRect();if(r.right>window.innerWidth-8)T.style.left=(e.clientX-r.width-14)+'px';if(r.bottom>window.innerHeight-8)T.style.top=(e.clientY-r.height-10)+'px';}
  document.addEventListener('mouseover',function(e){var el=e.target.closest('[data-tip]');if(el){T.textContent=el.dataset.tip;T.style.display='block';mv(e);}});
  document.addEventListener('mousemove',mv);
  document.addEventListener('mouseout',function(e){if(!e.relatedTarget||!e.relatedTarget.closest('[data-tip]'))T.style.display='none';});
})();
</script>
</body>
</html>'''


# ──────────────────────────────────────────────────
# POD PERFORMANCE (requires resource sheet)
# ──────────────────────────────────────────────────

def load_resource_sheet(rs_path):
    """Parse resource sheet CSV.
    Returns email_to_pod, pod_all_emails, pod_projects."""
    email_to_pod   = {}
    pod_all_emails = defaultdict(set)
    pod_projects   = defaultdict(set)
    _SKIP = {'', 'unassigned', 'n/a', '-'}
    with open(rs_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        # Some resource sheets append extra pivot-table columns after the
        # roster columns, re-using header names like "PL" / "Pod Name/Location".
        # csv.DictReader keeps only the LAST duplicate-named column's value per
        # row, which would silently read from the (mostly empty) pivot columns
        # instead of the real roster ones. Rename duplicates after the first
        # occurrence so DictReader always resolves to the first (roster) column.
        seen = set()
        deduped_header = []
        for i, h in enumerate(reader.fieldnames):
            if h in seen:
                deduped_header.append(f'{h}__dup{i}')
            else:
                seen.add(h)
                deduped_header.append(h)
        reader.fieldnames = deduped_header
        for row in reader:
            email    = row.get('Email', '').strip().lower()
            pl_raw   = row.get('PL', '').strip()
            pod_name = row.get('Pod Name/Location', '').strip()
            proj     = row.get('Current Project', '').strip()
            if not email or not pl_raw:
                continue
            pl = pl_raw.split('/')[0].strip().lower()
            email_to_pod[email] = {'pl': pl, 'pod_name': pod_name}
            pod_all_emails[pl].add(email)
            if proj.lower() not in _SKIP:
                pod_projects[pl].add(proj)
    return email_to_pod, pod_all_emails, pod_projects


def pipeline_pod_scorecard(rolling, email_to_pod, pod_all_emails, pod_projects, out_path):
    """Aggregate rolling scorecard by pod. Writes pod_performance.csv."""

    # Group scored taskers by pod
    pod_members  = defaultdict(list)
    pod_name_map = {}
    scored_lower = {r['email'].lower() for r in rolling}

    for r in rolling:
        el = r['email'].lower()
        if el in email_to_pod:
            info = email_to_pod[el]
            pl   = info['pl']
            pod_members[pl].append(r)
            pod_name_map[pl] = info['pod_name']

    # Also capture pod_name for PLs with zero scored members
    for email, info in email_to_pod.items():
        pl = info['pl']
        if pl not in pod_name_map:
            pod_name_map[pl] = info['pod_name']

    # Compute pod rows
    pod_rows = []
    for pl, members in pod_members.items():
        all_in_rs  = pod_all_emails.get(pl, set())
        n_scored   = len(members)
        n_unscored = len(all_in_rs - scored_lower)
        n_total    = len(all_in_rs)

        finals   = [r['final_score']       for r in members]
        efforts  = [r['effort_score']       for r in members]
        outputs  = [r['output_score']       for r in members]
        consis   = [r['consistency_score']  for r in members]
        peaks    = [r['peak_score']         for r in members]
        gd       = [r['good_days_pct']      for r in members]
        hours    = [r['total_hours']         for r in members]
        avg_hrs  = [r['avg_hours_day']       for r in members]

        qd = defaultdict(int)
        for r in members:
            qd[r['quadrant']] += 1

        top    = max(members, key=lambda r: r['final_score'])
        bottom = min(members, key=lambda r: r['final_score'])

        coverage = round(n_scored / n_total * 100, 1) if n_total > 0 else 0.0
        pod_rows.append({
            'pl':           pl,
            'pod_name':     pod_name_map.get(pl, ''),
            'pod_size':     n_total,
            'scored':       n_scored,
            'unscored':     n_unscored,
            'coverage':     coverage,
            'avg_final':    round(float(np.mean(finals)), 1),
            'med_final':    round(float(np.median(finals)), 1),
            'avg_effort':   round(float(np.mean(efforts)), 1),
            'avg_output':   round(float(np.mean(outputs)), 1),
            'avg_consist':  round(float(np.mean(consis)), 1),
            'avg_peak':     round(float(np.mean(peaks)), 1),
            'avg_good_days':round(float(np.mean(gd)), 1),
            'total_hours':  round(sum(hours), 1),
            'avg_h_member': round(float(np.mean(avg_hrs)), 2),
            'stars':        qd['Star'],
            'plodders':     qd['Plodder'],
            'sprinters':    qd['Sprinter'],
            'developing':   qd['Underperformer'],
            'top_email':    top['email'],
            'top_score':    top['final_score'],
            'bot_email':    bottom['email'],
            'bot_score':    bottom['final_score'],
            'projects':     sorted(pod_projects.get(pl, set())),
        })

    # Sort: full-coverage pods first by avg_final; low-coverage (<25% or <3 scored) appended at bottom
    MIN_SCORED = 3
    MIN_COVERAGE = 25.0
    reliable = [r for r in pod_rows if r['scored'] >= MIN_SCORED and r['coverage'] >= MIN_COVERAGE]
    low_cov  = [r for r in pod_rows if r['scored'] < MIN_SCORED or r['coverage'] < MIN_COVERAGE]
    reliable.sort(key=lambda x: -x['avg_final'])
    low_cov.sort(key=lambda x: -x['avg_final'])
    pod_rows = reliable + low_cov

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'Pod Rank', 'Pod Lead', 'Pod Name / Location',
            'Pod Size (RS)', 'Scored Members', 'Unscored / Absent', 'Coverage %',
            'Avg Final Score', 'Median Final Score',
            'Avg Effort Score', 'Avg Output Score',
            'Avg Consistency', 'Avg Peak Score', 'Avg Good Days %',
            'Total Hours', 'Avg Hrs / Member / Day',
            'Stars', 'Plodders', 'Sprinters', 'Developing',
            'Top Performer', 'Top Score',
            'Bottom Performer', 'Bottom Score',
            'Projects',
        ])
        for rank, r in enumerate(pod_rows, 1):
            r['rank'] = rank
            writer.writerow([
                rank, r['pl'], r['pod_name'],
                r['pod_size'], r['scored'], r['unscored'], f"{r['coverage']}%",
                r['avg_final'], r['med_final'],
                r['avg_effort'], r['avg_output'],
                r['avg_consist'], r['avg_peak'], r['avg_good_days'],
                r['total_hours'], r['avg_h_member'],
                r['stars'], r['plodders'], r['sprinters'], r['developing'],
                r['top_email'], r['top_score'],
                r['bot_email'], r['bot_score'],
                ' | '.join(r['projects']),
            ])

    unmatched = sum(1 for pl in pod_all_emails if pl not in pod_members)
    print(f"  Pod scorecard       → {out_path}  "
          f"({len(pod_rows)} pods ranked  |  {unmatched} pods with no scored members)")
    return pod_rows


# ──────────────────────────────────────────────────
# DAILY SPOTLIGHT
# ──────────────────────────────────────────────────

def _build_spotlight_blob(sr):
    if sr is None:
        return {"enabled": False, "reason": "not_run"}
    if sr["all_anomalies"]:
        return {"enabled": False, "reason": "all_anomalies", "date": ""}
    rows_out = []
    for r in sr["rows"]:
        rows_out.append({
            "e":  r["email"],
            "pl": r.get("pod_lead", ""),
            "pn": r.get("pod_name", ""),
            "dr": r["day_rank"],
            "dp": r["day_percentile"],
            "ds": round(r["day_score"], 1),
            "ef": round(r["effort_score_day"], 1),
            "op": round(r["output_score_day"], 1) if r["output_score_day"] is not None else None,
            "hr": round(r["hours"], 2),
            "sb": r["submissions"],
            "sn": r["scoring_note"],
            "sp": r["spotlight"],
            "rr": r["rolling_rank"],
            "rf": r["rolling_final_score"],
        })
    return {
        "enabled":          True,
        "reason":           "pool_too_small" if sr["pool_too_small"] else ("pool_thin" if sr["pool_thin"] else "ok"),
        "date":             sr["spotlight_date"],
        "cal_date":         sr["latest_cal_date"],
        "date_was_anomaly": sr["date_was_anomaly"],
        "pool_too_small":   sr["pool_too_small"],
        "pool_thin":        sr["pool_thin"],
        "period_avg":       sr["period_avg"],
        "active":           sr["active_count"],
        "top_cutoff":       round(sr["top_cutoff"], 1) if sr["top_cutoff"] is not None else None,
        "bot_cutoff":       round(sr["bot_cutoff"], 1) if sr["bot_cutoff"] is not None else None,
        "rows":             rows_out,
    }


def pipeline_daily_spotlight(raw_task, raw_day, anomaly_days, rolling, out_dir, email_to_pod=None):
    """Identify top/bottom 10% taskers for the latest scoreable day.

    Returns a spotlight_result dict consumed by pipeline_html_mgmt_report.
    Also writes daily_spotlight_YYYY-MM-DD.csv into out_dir.
    """
    _empty = {
        "spotlight_date":  "",
        "latest_cal_date": "",
        "date_was_anomaly": False,
        "all_anomalies":   True,
        "pool_too_small":  False,
        "pool_thin":       False,
        "period_avg":      0,
        "active_count":    0,
        "top_cutoff":      None,
        "bot_cutoff":      None,
        "rows":            [],
    }

    all_dates       = sorted({date for (date, _) in raw_day})
    latest_cal      = all_dates[-1] if all_dates else ""
    scoreable_dates = [d for d in all_dates if d not in anomaly_days]

    if not scoreable_dates:
        print("  Daily Spotlight: no scoreable dates — all days are anomaly days. Skipping.")
        return _empty

    spotlight_date   = scoreable_dates[-1]
    date_was_anomaly = spotlight_date != latest_cal

    if date_was_anomaly:
        print(f"  Daily Spotlight: latest calendar date {latest_cal} is an anomaly day; "
              f"using {spotlight_date} as spotlight date.")

    # ── Period average daily eligible taskers (for thin-pool detection) ──────
    # Eligible = worked on that day with hours >= SPOTLIGHT_MIN_HOURS
    daily_eligible_counts = []
    for d in scoreable_dates:
        cnt = sum(
            1 for (date, email), vals in raw_day.items()
            if date == d and email not in LEADS and vals["hours"] >= SPOTLIGHT_MIN_HOURS
        )
        if cnt > 0:
            daily_eligible_counts.append(cnt)
    period_avg = float(np.mean(daily_eligible_counts)) if daily_eligible_counts else 0.0

    # ── Hours scores for spotlight_date ─────────────────────────────────────
    # Leads are excluded from the eligible pool — spotlight is a tasker ranking.
    day_raw = {
        email: vals
        for (date, email), vals in raw_day.items()
        if date == spotlight_date and email not in LEADS
    }
    h_scores = {
        email: min(vals["hours"] / HOURS_PER_TASKER_TARGET, 1.0) * 100
        for email, vals in day_raw.items()
    }

    # ── Output scoring per (spotlight_date, task) cohort ────────────────────
    by_task = defaultdict(dict)   # task → {email: vals}
    for (date, email, task), vals in raw_task.items():
        if date == spotlight_date and email not in LEADS:
            by_task[task][email] = vals

    task_output = {}   # (email, task) → (s_pct, aht_pct)
    for task, cohort in by_task.items():
        if len(cohort) < MIN_SCOREABLE_TASKERS:
            continue
        emails  = list(cohort.keys())
        s_arr   = np.array([cohort[e]["submissions"] for e in emails], dtype=float)
        aht_arr = np.array([cohort[e]["aht_ratio"]   for e in emails], dtype=float)
        s_pct   = percentile_ranks(s_arr)
        aht_pct = 100.0 - percentile_ranks(aht_arr)
        for i, email in enumerate(emails):
            task_output[(email, task)] = (float(s_pct[i]), float(aht_pct[i]))

    # Aggregate task-level output to tasker level (weighted by submissions)
    tasker_output = {}   # email → (day_s_pct, day_aht_pct)
    _agg = defaultdict(lambda: {"s_w": [], "aht_w": []})
    for (date, email, task), vals in raw_task.items():
        if date != spotlight_date:
            continue
        if (email, task) in task_output:
            s_pct, aht_pct = task_output[(email, task)]
            weight = max(vals["submissions"], 1)
            _agg[email]["s_w"].append((s_pct, weight))
            _agg[email]["aht_w"].append((aht_pct, weight))
    for email, v in _agg.items():
        s = weighted_mean(v["s_w"])
        a = weighted_mean(v["aht_w"])
        if s is not None and a is not None:
            tasker_output[email] = (s, a)

    # ── Compose day_score per tasker + apply minimum hours filter ───────────
    _pod_map = email_to_pod or {}
    scored = []
    for email, vals in day_raw.items():
        if vals["hours"] < SPOTLIGHT_MIN_HOURS:
            continue
        h_score = h_scores[email]
        if email in tasker_output:
            s_pct, aht_pct = tasker_output[email]
            day_score      = W_HOURS * h_score + W_SUBS * s_pct + W_AHT * aht_pct
            output_denom   = W_SUBS + W_AHT
            output_score   = (W_SUBS * s_pct + W_AHT * aht_pct) / output_denom
            scoring_note   = ""
        else:
            day_score    = W_HOURS * h_score
            output_score = None
            scoring_note = "Sparse cohort — hours-only score"

        # aggregate raw totals from raw_task for this day
        total_subs = sum(
            v["submissions"] for (d, e, _t), v in raw_task.items()
            if d == spotlight_date and e == email
        )
        _pod = _pod_map.get(email, {})
        scored.append({
            "email":           email,
            "pod_lead":        _pod.get("pl", ""),
            "pod_name":        _pod.get("pod_name", ""),
            "day_score":       day_score,
            "effort_score_day": h_score,
            "output_score_day": output_score,
            "hours":           vals["hours"],
            "submissions":     total_subs,
            "scoring_note":    scoring_note,
            "spotlight":       "",
        })

    n = len(scored)

    # ── Rolling context ──────────────────────────────────────────────────────
    rolling_lookup = {r["email"]: (r.get("rank"), r["final_score"]) for r in rolling}
    for r in scored:
        rk, fs = rolling_lookup.get(r["email"], (None, None))
        r["rolling_rank"]        = rk
        r["rolling_final_score"] = fs

    # ── Rank by day_score ────────────────────────────────────────────────────
    scored.sort(key=lambda x: -x["day_score"])
    for i, r in enumerate(scored, 1):
        r["day_rank"] = i
        r["day_percentile"] = round((n - i) / (n - 1) * 100, 1) if n > 1 else 100.0

    # ── Pool size guards ─────────────────────────────────────────────────────
    pool_too_small = n < SPOTLIGHT_MIN_POOL
    pool_thin      = (not pool_too_small
                      and period_avg > 0
                      and n / period_avg < SPOTLIGHT_THIN_PCT)

    if pool_too_small:
        print(f"  Daily Spotlight: only {n} eligible taskers on {spotlight_date} "
              f"(hard floor is {SPOTLIGHT_MIN_POOL}). No spotlight labels will be assigned.")
        for r in scored:
            r["spotlight"] = "Insufficient pool"
    elif pool_thin:
        print(f"  Daily Spotlight: thin pool — {n} eligible taskers on {spotlight_date} "
              f"vs period average {period_avg:.0f}. Labels assigned with caution warning.")

    top_cutoff = None
    bot_cutoff = None

    if not pool_too_small and n > 0:
        top_k = max(1, round(n * SPOTLIGHT_TOP_PCT))
        bot_k = max(1, round(n * SPOTLIGHT_BOTTOM_PCT))
        top_cutoff = scored[top_k - 1]["day_score"]
        bot_cutoff = scored[n - bot_k]["day_score"]
        for r in scored:
            if r["day_score"] >= top_cutoff:
                r["spotlight"] = "Recognition Candidate"
            elif r["day_score"] <= bot_cutoff:
                r["spotlight"] = "Check-in Recommended"

    # ── Write CSV ────────────────────────────────────────────────────────────
    out_path = os.path.join(out_dir, f"daily_spotlight_{spotlight_date}.csv")
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'Date', 'Annotator Email', 'Pod Lead', 'Pod Name',
            'Day Rank', 'Day Percentile', 'Day Score',
            'Effort Score (Day)', 'Output Score (Day)',
            'Hours', 'Submissions', 'Scoring Note', 'Spotlight',
            'Rolling Rank', 'Rolling Final Score',
        ])
        for r in scored:
            writer.writerow([
                spotlight_date, r["email"], r["pod_lead"], r["pod_name"],
                r["day_rank"], r["day_percentile"], round(r["day_score"], 1),
                round(r["effort_score_day"], 1),
                round(r["output_score_day"], 1) if r["output_score_day"] is not None else "",
                round(r["hours"], 2), r["submissions"],
                r["scoring_note"], r["spotlight"],
                r["rolling_rank"] if r["rolling_rank"] is not None else "",
                r["rolling_final_score"] if r["rolling_final_score"] is not None else "",
            ])

    n_top = sum(1 for r in scored if r["spotlight"] == "Recognition Candidate")
    n_bot = sum(1 for r in scored if r["spotlight"] == "Check-in Recommended")
    print(f"  Daily Spotlight  → {out_path}  "
          f"({n} eligible | {n_top} recognition | {n_bot} check-in)")

    return {
        "spotlight_date":   spotlight_date,
        "latest_cal_date":  latest_cal,
        "date_was_anomaly": date_was_anomaly,
        "all_anomalies":    False,
        "pool_too_small":   pool_too_small,
        "pool_thin":        pool_thin,
        "period_avg":       round(period_avg, 1),
        "active_count":     n,
        "top_cutoff":       top_cutoff,
        "bot_cutoff":       bot_cutoff,
        "rows":             scored,
    }


# ──────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python mm_performance_pipeline.py <input_csv> [--rs <resource_sheet.csv>]")
        sys.exit(1)

    args = sys.argv[1:]
    rs_path = None
    if '--rs' in args:
        idx = args.index('--rs')
        if idx + 1 >= len(args):
            print("Error: --rs requires a file path argument")
            sys.exit(1)
        rs_path = args[idx + 1]
        del args[idx:idx + 2]
    input_csv = args[0]

    if not os.path.exists(input_csv):
        print(f"File not found: {input_csv}")
        sys.exit(1)

    all_dates = sorted({row['Date From'].strip() for row in _peek_dates(input_csv)})
    date_from = all_dates[0].replace('-', '')
    date_to   = all_dates[-1].replace('-', '')
    run_dir   = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"mm_performance_{date_from}_to_{date_to}"
    )
    os.makedirs(run_dir, exist_ok=True)

    print(f"\nInput: {input_csv}")
    print(f"Output folder: {run_dir}")
    print(f"Weights — Hours: {W_HOURS:.0%} | Submissions: {W_SUBS:.0%} | "
          f"AHT ratio (inverted): {W_AHT:.0%}")
    print(f"Hours target: {HOURS_PER_TASKER_TARGET}h/day (absolute)  |  "
          f"Subs/AHT: project-cohort percentile")
    print(f"Quadrant splits — relative: team-median Effort Score × team-median Subs percentile")
    print(f"Anomaly threshold: < {ANOMALY_THRESHOLD:.0%} team target  |  "
          f"Sparse cohort: < {MIN_SCOREABLE_TASKERS} taskers\n")

    raw_task, raw_day = ingest(input_csv)
    anomaly_days      = find_anomaly_days(raw_day)

    email_to_pod   = {}
    pod_all_emails = {}
    pod_projects   = {}
    if rs_path:
        if not os.path.exists(rs_path):
            print(f"  Warning: resource sheet not found: {rs_path}")
        else:
            email_to_pod, pod_all_emails, pod_projects = load_resource_sheet(rs_path)

    pipeline_deduped(
        raw_task,
        os.path.join(run_dir, "tasker_daily_deduped.csv")
    )
    pipeline_daily_summary(
        raw_day, anomaly_days,
        os.path.join(run_dir, "daily_summary.csv")
    )
    period_str = f"{all_dates[0]} to {all_dates[-1]}"
    rolling = pipeline_tasker_scorecard(
        raw_task, raw_day, anomaly_days,
        os.path.join(run_dir, "tasker_rolling_scorecard.csv"),
        email_to_pod=email_to_pod,
    )
    # Dashboards, pod rollup, and spotlight are ranking surfaces built around a numeric
    # score — leads (unscored by design) are excluded here, same as before. They still
    # appear, flagged, in daily_summary.csv, tasker_daily_deduped.csv, and
    # tasker_rolling_scorecard.csv.
    rolling_taskers = [r for r in rolling if not r["is_lead"]]
    pipeline_html_report(
        rolling_taskers, period_str,
        os.path.join(run_dir, "tasker_performance_dashboard.html")
    )
    pod_rows = []
    if email_to_pod:
        pod_rows = pipeline_pod_scorecard(
            rolling_taskers, email_to_pod, pod_all_emails, pod_projects,
            os.path.join(run_dir, "pod_performance.csv")
        )
    spotlight_result = pipeline_daily_spotlight(
        raw_task, raw_day, anomaly_days, rolling_taskers,
        run_dir,
        email_to_pod=email_to_pod if email_to_pod else None,
    )
    pipeline_html_mgmt_report(
        rolling_taskers, raw_task, raw_day, anomaly_days, period_str, pod_rows,
        spotlight_result,
        os.path.join(run_dir, "management_dashboard.html")
    )

    print("\nDone.")
