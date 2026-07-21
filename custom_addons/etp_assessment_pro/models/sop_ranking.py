# -*- coding: utf-8 -*-
"""SOP Rankings - a manager dashboard that ranks each generator (SOP) by the
performance of the candidates who sat an assessment built from it.

Pure aggregation of already-stored evaluator fields via grouped reads (never a
scan over raw evaluators, never an LLM call), mirroring dashboard.py's
leaderboard/breakdown builders. One SOP = one etp.assessment.pro.prompt
(generator); an assessment carries generator_id, and an evaluator reaches the
generator through assessment_id.generator_id.
"""
from markupsafe import Markup, escape

from odoo import api, fields, models


class EtpAssessmentSopRanking(models.TransientModel):
    _name = "etp.assessment.pro.sop.ranking"
    _description = "ETP Assessment Pro - SOP Rankings"

    total_sops = fields.Integer(string="SOPs Ranked", readonly=True)
    total_submitted = fields.Integer(string="Submitted Attempts", readonly=True)
    overall_avg = fields.Float(string="Overall Avg Score", readonly=True)

    # sanitize=False so inline numeric bar widths (width:NN%) survive; safe
    # because the markup is admin-only, readonly, built from numbers with every
    # dynamic string escaped via markupsafe below (same contract as dashboard.py).
    ranking_html = fields.Html(
        string="SOP Rankings", sanitize=False, readonly=True)

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "SOP Rankings"

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        Ev = self.env["etp.assessment.pro.evaluator"]
        rows = self._build_ranking_rows(Ev)
        submitted_total = sum(r["submitted"] for r in rows)
        weighted = sum(r["avg"] * r["submitted"] for r in rows)
        res.update({
            "total_sops": len(rows),
            "total_submitted": submitted_total,
            "overall_avg": round(
                (weighted / submitted_total) if submitted_total else 0.0, 1),
            "ranking_html": self._render_ranking(rows),
        })
        return res

    @api.model
    def _build_ranking_rows(self, Ev):
        """One row per generator (SOP) that has at least one submitted attempt,
        aggregated on the DB. Each grouped read walks the SMALL grouped result
        (one row per generator), never the raw evaluator set.

        Ranked by average score, then pass rate, then volume - the same ordering
        spirit as the candidate leaderboard, applied to SOPs.
        """
        Assess = self.env["etp.assessment.pro"]
        Question = self.env["etp.assessment.pro.question"]

        # Map assessment_id -> generator(prompt) once, so evaluator grouped reads
        # (which group by assessment_id) can roll up to the generator.
        assess_gen = {}
        for a in Assess.search_read([("generator_id", "!=", False)],
                                    ["generator_id"]):
            assess_gen[a["id"]] = a["generator_id"][0]

        data = {}

        def _bucket(gen_id, gen_name):
            if gen_id not in data:
                data[gen_id] = {
                    "id": gen_id, "name": gen_name,
                    "submitted": 0, "passed": 0, "avg": 0.0, "_score_num": 0.0,
                    "candidates": 0, "questions": 0,
                }
            return data[gen_id]

        # Per-assessment submitted count AND avg score in TWO grouped reads, then
        # roll both up to the generator, weighting each assessment's average by
        # its own submitted count so the SOP average is a true attempt-weighted
        # mean (not an average-of-averages).
        sub_by_assess = {}
        for assessment, count in Ev._read_group(
                [("state", "=", "submitted")], ["assessment_id"], ["__count"]):
            if not assessment:
                continue
            sub_by_assess[assessment.id] = count
            gen_id = assess_gen.get(assessment.id)
            if gen_id:
                _bucket(gen_id, assessment.generator_id.display_name)[
                    "submitted"] += count
        for assessment, avg in Ev._read_group(
                [("state", "=", "submitted")], ["assessment_id"],
                ["score_percent:avg"]):
            gen_id = assess_gen.get(assessment.id) if assessment else None
            if gen_id and gen_id in data:
                data[gen_id]["_score_num"] += (
                    (avg or 0.0) * sub_by_assess.get(assessment.id, 0))
        for assessment, count in Ev._read_group(
                [("state", "=", "submitted"), ("result", "=", "pass")],
                ["assessment_id"], ["__count"]):
            gen_id = assess_gen.get(assessment.id) if assessment else None
            if gen_id and gen_id in data:
                data[gen_id]["passed"] += count
        # unique candidates per generator (distinct applicants, any state)
        for assessment, count in Ev._read_group(
                [], ["assessment_id"], ["applicant_id:count_distinct"]):
            gen_id = assess_gen.get(assessment.id) if assessment else None
            if gen_id and gen_id in data:
                data[gen_id]["candidates"] += count

        # published question bank size per generator (independent of attempts)
        for generator, count in Question._read_group(
                [("active", "=", True)], ["generator_id"], ["__count"]):
            if generator and generator.id in data:
                data[generator.id]["questions"] = count

        rows = list(data.values())
        for r in rows:
            r["avg"] = round(
                (r.pop("_score_num", 0.0) / r["submitted"])
                if r["submitted"] else 0.0, 1)
            r["pass_rate"] = round(
                (r["passed"] / r["submitted"] * 100.0)
                if r["submitted"] else 0.0, 1)
        rows.sort(key=lambda r: (r["avg"], r["pass_rate"], r["submitted"]),
                  reverse=True)
        return rows

    @api.model
    def _render_ranking(self, rows):
        if not rows:
            return Markup(
                '<div class="etp-brk-empty">No submitted attempts yet - '
                'SOP rankings appear once candidates complete assessments '
                'built from a generator.</div>')

        head = Markup(
            '<div class="etp-brk-head">'
            '<span class="etp-brk-c-num">#</span>'
            '<span class="etp-brk-c-name">SOP / Generator</span>'
            '<span class="etp-brk-c-num">Cand.</span>'
            '<span class="etp-brk-c-num">Subm.</span>'
            '<span class="etp-brk-c-num">Qs</span>'
            '<span class="etp-brk-c-num">Pass %</span>'
            '<span class="etp-brk-c-bar">Avg Score</span>'
            "</div>")
        body = Markup("")
        for idx, row in enumerate(rows, start=1):
            avg = max(0.0, min(100.0, row["avg"]))
            medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(
                idx, str(idx))
            body += Markup(
                '<a class="etp-brk-row" '
                'href="/web#id={gid}&amp;model=etp.assessment.pro.prompt'
                '&amp;view_type=form">'
                '<span class="etp-brk-num etp-brk-rank">{medal}</span>'
                '<span class="etp-brk-name">{name}</span>'
                '<span class="etp-brk-num">{cand}</span>'
                '<span class="etp-brk-num">{subm}</span>'
                '<span class="etp-brk-num">{qs}</span>'
                '<span class="etp-brk-num">{prate}%</span>'
                '<span class="etp-brk-bar">'
                '<span class="etp-brk-bar-track">'
                '<span class="etp-brk-bar-fill" style="width:{avg}%"></span>'
                "</span>"
                '<span class="etp-brk-bar-pct">{avg}%</span>'
                "</span>"
                "</a>"
            ).format(
                gid=row["id"],
                medal=medal,
                name=escape(row["name"] or "Untitled SOP"),
                cand=row["candidates"],
                subm=row["submitted"],
                qs=row["questions"],
                prate=round(row["pass_rate"], 1),
                avg=round(avg, 1),
            )
        return Markup('<div class="etp-brk">{}{}</div>').format(head, body)
