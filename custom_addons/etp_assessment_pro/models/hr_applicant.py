from markupsafe import Markup, escape

from odoo import api, fields, models


class HrApplicant(models.Model):
    _inherit = "hr.applicant"

    candidate_user_id = fields.Many2one(
        "res.users",
        string="Candidate Portal User",
        ondelete="set null",
        index=True,
        copy=False,
    )

    etp_evaluator_ids = fields.One2many(
        "etp.assessment.pro.evaluator",
        "applicant_id",
        string="ETP Assessments",
    )

    etp_assessment_count = fields.Integer(
        string="Assessments", compute="_compute_etp_stats")
    etp_submitted_count = fields.Integer(
        string="Submitted", compute="_compute_etp_stats")
    etp_pass_count = fields.Integer(
        string="Passed", compute="_compute_etp_stats")
    etp_fail_count = fields.Integer(
        string="Failed", compute="_compute_etp_stats")
    etp_pending_count = fields.Integer(
        string="Awaiting Result", compute="_compute_etp_stats")
    etp_avg_score = fields.Float(
        string="Average Score", compute="_compute_etp_stats")
    etp_pass_rate = fields.Float(
        string="Pass Rate", compute="_compute_etp_stats")
    etp_best_score = fields.Float(
        string="Best Score", compute="_compute_etp_stats")

    # sanitize=False so inline numeric styles (width:NN%, conic-gradient
    # degrees) survive; safe because markup is admin-only, readonly, built
    # from numbers only, with every dynamic string escaped via markupsafe.
    etp_scores_chart_html = fields.Html(
        string="Scores by Assessment", sanitize=False, readonly=True,
        compute="_compute_etp_scores_chart_html")
    etp_result_donut_html = fields.Html(
        string="Result Breakdown", sanitize=False, readonly=True,
        compute="_compute_etp_result_donut_html")

    @api.depends(
        "etp_evaluator_ids.state",
        "etp_evaluator_ids.result",
        "etp_evaluator_ids.score_percent",
    )
    def _compute_etp_stats(self):
        for rec in self:
            evals = rec.etp_evaluator_ids
            submitted = evals.filtered(lambda e: e.state == "submitted")
            passed = evals.filtered(lambda e: e.result == "pass")
            failed = evals.filtered(lambda e: e.result == "fail")
            pending = evals.filtered(lambda e: e.result == "pending")

            rec.etp_assessment_count = len(evals)
            rec.etp_submitted_count = len(submitted)
            rec.etp_pass_count = len(passed)
            rec.etp_fail_count = len(failed)
            rec.etp_pending_count = len(pending)

            sub_scores = submitted.mapped("score_percent")
            rec.etp_avg_score = round(
                sum(sub_scores) / len(sub_scores), 1) if sub_scores else 0.0
            rec.etp_pass_rate = round(
                len(passed) / len(submitted) * 100.0, 1) if submitted else 0.0
            all_scores = evals.mapped("score_percent")
            rec.etp_best_score = round(max(all_scores), 1) if all_scores else 0.0

    @api.depends(
        "etp_evaluator_ids.score_percent",
        "etp_evaluator_ids.result",
        "etp_evaluator_ids.submitted_at",
    )
    def _compute_etp_scores_chart_html(self):
        labels = {"pass": "Pass", "fail": "Fail", "pending": "Pending"}
        for rec in self:
            evals = list(rec.etp_evaluator_ids)
            evals.sort(
                key=lambda e: (
                    e.submitted_at or fields.Datetime.to_datetime("1900-01-01"),
                    e.score_percent or 0.0,
                ),
                reverse=True,
            )
            if not evals:
                rec.etp_scores_chart_html = Markup(
                    '<div class="etp-chart-empty">No assessments yet</div>')
                continue

            rows = Markup("")
            for ev in evals:
                pct = max(0.0, min(100.0, ev.score_percent or 0.0))
                result = ev.result if ev.result in labels else "pending"
                rows += Markup(
                    '<a class="etp-cbar-row" '
                    'href="/web#id={eid}&amp;model=etp.assessment.pro.evaluator'
                    '&amp;view_type=form">'
                    '<span class="etp-cbar-name">{name}</span>'
                    '<span class="etp-cbar-track">'
                    '<span class="etp-cbar-fill" style="width:{pct}%"></span>'
                    "</span>"
                    '<span class="etp-cbar-val">{val}% '
                    '<span class="etp-feed-pill etp-feed-pill-{cls}">{label}</span>'
                    "</span>"
                    "</a>"
                ).format(
                    eid=ev.id,
                    name=escape(
                        ev.assessment_id.display_name or "Untitled"),
                    pct=round(pct, 1),
                    val=round(pct, 1),
                    cls=result,
                    label=labels[result],
                )
            rec.etp_scores_chart_html = Markup(
                '<div class="etp-cbar-wrap etp-cand-scores">{}</div>').format(rows)

    @api.depends(
        "etp_evaluator_ids.result",
        "etp_evaluator_ids.state",
    )
    def _compute_etp_result_donut_html(self):
        for rec in self:
            evals = rec.etp_evaluator_ids
            pass_count = len(evals.filtered(lambda e: e.result == "pass"))
            fail_count = len(evals.filtered(lambda e: e.result == "fail"))
            pending_count = len(evals.filtered(lambda e: e.result == "pending"))
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
                rec.etp_result_donut_html = Markup(
                    '<div class="etp-donut-wrap">{}{}</div>').format(ring, legend)
                continue

            pass_deg = round(pass_count / total * 360.0, 2)
            pass_fail_deg = round((pass_count + fail_count) / total * 360.0, 2)
            ring = Markup(
                '<div class="etp-donut" style="background:conic-gradient('
                "var(--etp-pass) 0 {a}deg,"
                "var(--etp-fail) {a}deg {b}deg,"
                "var(--etp-muted) {b}deg 360deg)\">"
                '<div class="etp-donut-hole"></div></div>'
            ).format(a=pass_deg, b=pass_fail_deg)
            rec.etp_result_donut_html = Markup(
                '<div class="etp-donut-wrap">{}{}</div>').format(ring, legend)
