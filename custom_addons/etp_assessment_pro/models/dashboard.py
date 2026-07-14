from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.tools import format_datetime


class EtpAssessmentDashboard(models.TransientModel):
    _name = "etp.assessment.pro.dashboard"
    _description = "ETP Assessment Pro — Analytics Dashboard"

    total_assessments = fields.Integer(string="Assessments", readonly=True)
    total_candidates = fields.Integer(string="Candidates", readonly=True)
    unique_candidates = fields.Integer(string="Unique Candidates", readonly=True)
    submitted_count = fields.Integer(string="Submitted", readonly=True)
    in_progress_count = fields.Integer(string="In Progress", readonly=True)
    pending_count = fields.Integer(string="Not Started", readonly=True)
    total_questions_bank = fields.Integer(string="Question Bank", readonly=True)

    pass_count = fields.Integer(string="Passed", readonly=True)
    fail_count = fields.Integer(string="Failed", readonly=True)
    result_pending_count = fields.Integer(string="Awaiting Result", readonly=True)

    pass_rate = fields.Float(string="Pass Rate", readonly=True)
    avg_score = fields.Float(string="Average Score", readonly=True)
    completion_rate = fields.Float(string="Completion Rate", readonly=True)

    avg_objective = fields.Float(string="Avg Objective", readonly=True)
    avg_subjective = fields.Float(string="Avg Subjective", readonly=True)

    # sanitize=False needed so inline numeric styles (width:NN%, conic-gradient
    # degrees) survive; safe because markup is admin-only, readonly, built from
    # numbers only, with dynamic strings escaped via markupsafe below.
    chart_by_assessment_html = fields.Html(
        string="Average Score by Assessment", sanitize=False, readonly=True
    )
    chart_result_donut_html = fields.Html(
        string="Result Breakdown", sanitize=False, readonly=True
    )
    chart_score_dist_html = fields.Html(
        string="Score Distribution", sanitize=False, readonly=True
    )
    assessment_breakdown_html = fields.Html(
        string="Assessment Performance", sanitize=False, readonly=True
    )
    recent_submissions_html = fields.Html(
        string="Recent Submissions", sanitize=False, readonly=True
    )

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "Assessment Analytics"

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)

        Ev = self.env["etp.assessment.pro.evaluator"]
        Assess = self.env["etp.assessment.pro"]
        Question = self.env["etp.assessment.pro.question"]

        total_candidates = Ev.search_count([])
        unique_candidates = len(
            Ev._read_group([], ["applicant_id"], ["__count"]))
        total_assessments = Assess.search_count([])
        total_questions_bank = Question.search_count([])

        state_counts = {
            state: count
            for state, count in Ev._read_group([], ["state"], ["__count"])
        }
        submitted = state_counts.get("submitted", 0)
        in_progress = state_counts.get("in_progress", 0)
        pending = state_counts.get("pending", 0)

        result_counts = {
            result: count
            for result, count in Ev._read_group([], ["result"], ["__count"])
        }
        pass_count = result_counts.get("pass", 0)
        fail_count = result_counts.get("fail", 0)
        result_pending = result_counts.get("pending", 0)

        avg_rows = Ev._read_group(
            [("state", "=", "submitted")],
            [],
            ["score_percent:avg", "total_score:avg", "llm_total_score:avg"],
        )
        avg_score, avg_objective, avg_subjective = (
            avg_rows[0] if avg_rows else (0.0, 0.0, 0.0)
        )

        pass_rate = (pass_count / submitted * 100.0) if submitted else 0.0
        completion_rate = (
            (submitted / total_candidates * 100.0) if total_candidates else 0.0
        )

        res.update(
            {
                "total_assessments": total_assessments,
                "total_candidates": total_candidates,
                "unique_candidates": unique_candidates,
                "submitted_count": submitted,
                "in_progress_count": in_progress,
                "pending_count": pending,
                "total_questions_bank": total_questions_bank,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "result_pending_count": result_pending,
                "pass_rate": round(pass_rate, 1),
                "avg_score": round(avg_score or 0.0, 1),
                "completion_rate": round(completion_rate, 1),
                "avg_objective": round(avg_objective or 0.0, 1),
                "avg_subjective": round(avg_subjective or 0.0, 1),
                "chart_by_assessment_html": self._build_chart_by_assessment(Ev),
                "chart_result_donut_html": self._build_chart_result_donut(
                    pass_count, fail_count, result_pending
                ),
                "chart_score_dist_html": self._build_chart_score_dist(Ev),
                "assessment_breakdown_html": self._build_assessment_breakdown(Ev),
                "recent_submissions_html": self._build_recent_submissions(Ev),
            }
        )
        return res

    def _drill_action(self, name, res_model, domain=None):
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": res_model,
            "view_mode": "list,form",
            "target": "current",
            "domain": domain or [],
            "context": {},
        }

    def action_open_assessments(self):
        return self.env["ir.actions.act_window"]._for_xml_id(
            "etp_assessment_pro.action_etp_assessment")

    def action_open_candidates(self):
        list_view = self.env.ref(
            "etp_assessment_pro.etp_hr_applicant_candidate_list")
        form_view = self.env.ref(
            "etp_assessment_pro.etp_hr_applicant_candidate_dashboard_form")
        return {
            "type": "ir.actions.act_window",
            "name": "Candidates",
            "res_model": "hr.applicant",
            "views": [(list_view.id, "list"), (form_view.id, "form")],
            "domain": [("etp_evaluator_ids", "!=", False)],
            "target": "current",
            "context": {},
        }

    def action_open_submitted(self):
        return self._drill_action(
            "Submitted Candidates", "etp.assessment.pro.evaluator",
            [("state", "=", "submitted")])

    def action_open_passed(self):
        return self._drill_action(
            "Passed Candidates", "etp.assessment.pro.evaluator",
            [("result", "=", "pass")])

    def action_open_failed(self):
        return self._drill_action(
            "Failed Candidates", "etp.assessment.pro.evaluator",
            [("result", "=", "fail")])

    def action_open_pending(self):
        return self._drill_action(
            "Not Started Candidates", "etp.assessment.pro.evaluator",
            [("state", "=", "pending")])

    @api.model
    def _build_chart_by_assessment(self, Ev):
        rows = Ev._read_group(
            [("state", "=", "submitted")],
            ["assessment_id"],
            ["score_percent:avg"],
        )
        rows = [(a, avg or 0.0) for a, avg in rows if a]
        rows.sort(key=lambda r: r[1], reverse=True)
        rows = rows[:8]

        if not rows:
            return Markup('<div class="etp-chart-empty">No submissions yet</div>')

        bars = Markup("")
        for assessment, avg in rows:
            pct = max(0.0, min(100.0, avg))
            bars += Markup(
                '<div class="etp-cbar-row">'
                '<span class="etp-cbar-name">{name}</span>'
                '<span class="etp-cbar-track">'
                '<span class="etp-cbar-fill" style="width:{pct}%"></span>'
                "</span>"
                '<span class="etp-cbar-val">{val}%</span>'
                "</div>"
            ).format(
                name=escape(assessment.display_name or "Untitled"),
                pct=round(pct, 1),
                val=round(pct, 1),
            )
        return Markup('<div class="etp-cbar-wrap">{}</div>').format(bars)

    @api.model
    def _build_chart_result_donut(self, pass_count, fail_count, pending_count):
        total = pass_count + fail_count + pending_count
        legend = Markup(
            '<ul class="etp-donut-legend">'
            '<li><span class="etp-dot etp-dot-pass"></span>Pass<b>{p}</b></li>'
            '<li><span class="etp-dot etp-dot-fail"></span>Fail<b>{f}</b></li>'
            '<li><span class="etp-dot etp-dot-pending"></span>Pending<b>{n}</b></li>'
            "</ul>"
        ).format(p=pass_count, f=fail_count, n=pending_count)

        if not total:
            ring = Markup(
                '<div class="etp-donut" style="background:var(--etp-muted)">'
                '<div class="etp-donut-hole"></div></div>'
            )
            return Markup('<div class="etp-donut-wrap">{}{}</div>').format(
                ring, legend
            )

        pass_deg = round(pass_count / total * 360.0, 2)
        pass_fail_deg = round((pass_count + fail_count) / total * 360.0, 2)
        ring = Markup(
            '<div class="etp-donut" style="background:conic-gradient('
            "var(--etp-pass) 0 {a}deg,"
            "var(--etp-fail) {a}deg {b}deg,"
            "var(--etp-muted) {b}deg 360deg)\">"
            '<div class="etp-donut-hole"></div></div>'
        ).format(a=pass_deg, b=pass_fail_deg)
        return Markup('<div class="etp-donut-wrap">{}{}</div>').format(ring, legend)

    @api.model
    def _build_chart_score_dist(self, Ev):
        buckets = [(0, 50, "0–49"), (50, 70, "50–69"), (70, 85, "70–84"),
                   (85, 101, "85–100")]
        counts = []
        for lo, hi, label in buckets:
            counts.append(
                (
                    label,
                    Ev.search_count(
                        [
                            ("state", "=", "submitted"),
                            ("score_percent", ">=", lo),
                            ("score_percent", "<", hi),
                        ]
                    ),
                )
            )
        peak = max((c for _, c in counts), default=0)
        if not peak:
            return Markup('<div class="etp-chart-empty">No submissions yet</div>')

        cols = Markup("")
        for label, count in counts:
            height = round(count / peak * 100.0, 1)
            cols += Markup(
                '<div class="etp-dist-col">'
                '<span class="etp-dist-count">{count}</span>'
                '<span class="etp-dist-bar-track">'
                '<span class="etp-dist-bar" style="height:{h}%"></span>'
                "</span>"
                '<span class="etp-dist-label">{label}</span>'
                "</div>"
            ).format(count=count, h=height, label=escape(label))
        return Markup('<div class="etp-dist-wrap">{}</div>').format(cols)

    @api.model
    def _build_assessment_breakdown(self, Ev):
        # Aggregate on the DB via grouped reads — never iterate raw evaluators.
        # Each loop below walks the SMALL grouped result set (one row per
        # assessment), assembling a per-assessment dict keyed by id.
        data = {}
        for assessment, count in Ev._read_group([], ["assessment_id"], ["__count"]):
            if assessment:
                data[assessment.id] = {
                    "rec": assessment,
                    "total": count,
                    "submitted": 0,
                    "passed": 0,
                    "avg": 0.0,
                }
        for assessment, count in Ev._read_group(
            [("state", "=", "submitted")], ["assessment_id"], ["__count"]
        ):
            if assessment and assessment.id in data:
                data[assessment.id]["submitted"] = count
        for assessment, count in Ev._read_group(
            [("result", "=", "pass")], ["assessment_id"], ["__count"]
        ):
            if assessment and assessment.id in data:
                data[assessment.id]["passed"] = count
        for assessment, avg in Ev._read_group(
            [("state", "=", "submitted")], ["assessment_id"], ["score_percent:avg"]
        ):
            if assessment and assessment.id in data:
                data[assessment.id]["avg"] = avg or 0.0

        rows = sorted(data.values(), key=lambda r: r["total"], reverse=True)[:10]
        if not rows:
            return Markup(
                '<div class="etp-brk-empty">No assessments yet</div>'
            )

        head = Markup(
            '<div class="etp-brk-head">'
            '<span class="etp-brk-c-name">Assessment</span>'
            '<span class="etp-brk-c-num">Cand.</span>'
            '<span class="etp-brk-c-num">Subm.</span>'
            '<span class="etp-brk-c-bar">Pass Rate</span>'
            '<span class="etp-brk-c-num">Avg</span>'
            "</div>"
        )
        body = Markup("")
        for row in rows:
            submitted = row["submitted"]
            pass_rate = (row["passed"] / submitted * 100.0) if submitted else 0.0
            pass_rate = max(0.0, min(100.0, pass_rate))
            avg = max(0.0, min(100.0, row["avg"]))
            body += Markup(
                '<a class="etp-brk-row" '
                'href="/web#id={aid}&amp;model=etp.assessment.pro&amp;view_type=form">'
                '<span class="etp-brk-name">{name}</span>'
                '<span class="etp-brk-num">{total}</span>'
                '<span class="etp-brk-num">{subm}</span>'
                '<span class="etp-brk-bar">'
                '<span class="etp-brk-bar-track">'
                '<span class="etp-brk-bar-fill" style="width:{rate}%"></span>'
                "</span>"
                '<span class="etp-brk-bar-pct">{rate}%</span>'
                "</span>"
                '<span class="etp-brk-num etp-brk-avg">{avg}%</span>'
                "</a>"
            ).format(
                aid=row["rec"].id,
                name=escape(row["rec"].display_name or "Untitled"),
                total=row["total"],
                subm=submitted,
                rate=round(pass_rate, 1),
                avg=round(avg, 1),
            )
        return Markup('<div class="etp-brk">{}{}</div>').format(head, body)

    @api.model
    def _build_recent_submissions(self, Ev):
        # One bounded search (limit=6) — not a scan over the full recordset.
        recs = Ev.search(
            [("state", "=", "submitted")],
            order="submitted_at desc, id desc",
            limit=6,
        )
        if not recs:
            return Markup(
                '<div class="etp-feed-empty">No submissions yet</div>'
            )

        labels = {"pass": "Pass", "fail": "Fail", "pending": "Pending"}
        rows = Markup("")
        for ev in recs:
            result = ev.result if ev.result in labels else "pending"
            when = (
                escape(format_datetime(self.env, ev.submitted_at))
                if ev.submitted_at
                else Markup("&mdash;")
            )
            rows += Markup(
                '<a class="etp-feed-row" '
                'href="/web#id={eid}&amp;model=etp.assessment.pro.evaluator'
                '&amp;view_type=form">'
                '<span class="etp-feed-main">'
                '<span class="etp-feed-name">{name}</span>'
                '<span class="etp-feed-meta">{assessment}'
                '<span class="etp-feed-date">{when}</span></span>'
                "</span>"
                '<span class="etp-feed-end">'
                '<span class="etp-feed-score">{score}%</span>'
                '<span class="etp-feed-pill etp-feed-pill-{cls}">{label}</span>'
                "</span>"
                "</a>"
            ).format(
                eid=ev.id,
                name=escape(ev.applicant_id.display_name or "Unknown"),
                assessment=escape(ev.assessment_id.display_name or "Untitled"),
                when=when,
                score=int(round(ev.score_percent or 0.0)),
                cls=result,
                label=labels[result],
            )
        return Markup('<div class="etp-feed">{}</div>').format(rows)
