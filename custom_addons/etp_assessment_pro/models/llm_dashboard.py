# -*- coding: utf-8 -*-
"""LLM Budget dashboard — read-only aggregate view over the LLM usage ledger.

Mirrors models/dashboard.py: a TransientModel that computes KPIs in a
default_get override using _read_group/search_count only (NO python record
loops), and builds CSS charts as sanitize=False Html from numbers + escaped
strings, reusing the .etp-dash-analytics SCSS hooks."""
from markupsafe import Markup, escape

from odoo import api, fields, models


class EtpLlmDashboard(models.TransientModel):
    _name = "etp.assessment.pro.llm.dashboard"
    _description = "ETP Assessment Pro - LLM Budget Dashboard"

    total_cost = fields.Float(string="Total Cost (USD)", digits=(12, 4),
                              readonly=True)
    total_tokens = fields.Integer(string="Total Tokens", readonly=True)
    total_requests = fields.Integer(string="Requests", readonly=True)
    total_images = fields.Integer(string="Images", readonly=True)
    total_videos = fields.Integer(string="Videos", readonly=True)
    avg_cost_per_request = fields.Float(string="Avg Cost / Request",
                                        digits=(12, 5), readonly=True)
    tokens_in_total = fields.Integer(string="Tokens In", readonly=True)
    tokens_out_total = fields.Integer(string="Tokens Out", readonly=True)
    thoughts_total = fields.Integer(string="Thinking Tokens", readonly=True)
    total_video_seconds = fields.Float(string="Video Seconds", digits=(12, 1),
                                       readonly=True)

    # sanitize=False needed so inline numeric styles (width:NN%,
    # grid-template-columns:repeat(N,1fr)) survive; safe because markup is
    # admin-only, readonly, built from numbers only, with dynamic strings
    # escaped via markupsafe below.
    chart_cost_by_operation_html = fields.Html(
        string="Cost by Operation", sanitize=False, readonly=True)
    chart_tokens_by_operation_html = fields.Html(
        string="Tokens by Operation", sanitize=False, readonly=True)
    chart_cost_by_project_html = fields.Html(
        string="Cost by Project", sanitize=False, readonly=True)
    chart_tokens_by_model_html = fields.Html(
        string="Tokens by Model", sanitize=False, readonly=True)
    chart_cost_dist_html = fields.Html(
        string="Requests by Operation", sanitize=False, readonly=True)

    def _compute_display_name(self):
        # Transient models otherwise show the raw NewId in the breadcrumb.
        for rec in self:
            rec.display_name = "LLM Budget"

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)

        Usage = self.env["etp.assessment.pro.llm.usage"]

        total_requests = Usage.search_count([])
        totals = Usage._read_group(
            [], [],
            ["cost_usd:sum", "total_tokens:sum", "image_count:sum",
             "tokens_in:sum", "tokens_out:sum", "thoughts_tokens:sum",
             "video_seconds:sum"],
        )
        (cost_sum, tok_sum, img_sum, tin_sum, tout_sum, thoughts_sum,
         vid_sum) = (
            totals[0] if totals else (0.0, 0, 0, 0, 0, 0, 0.0)
        )
        cost_sum = cost_sum or 0.0
        avg_cost = (cost_sum / total_requests) if total_requests else 0.0

        video_count = Usage.search_count([("operation", "=", "submit_video_op")])

        res.update(
            {
                "total_cost": round(cost_sum, 4),
                "total_tokens": tok_sum or 0,
                "total_requests": total_requests,
                "total_images": img_sum or 0,
                "total_videos": video_count,
                "avg_cost_per_request": round(avg_cost, 5),
                "tokens_in_total": tin_sum or 0,
                "tokens_out_total": tout_sum or 0,
                "thoughts_total": thoughts_sum or 0,
                "total_video_seconds": round(vid_sum or 0.0, 1),
                "chart_cost_by_operation_html":
                    self._build_chart_cost_by_operation(Usage),
                "chart_tokens_by_operation_html":
                    self._build_chart_tokens_by_operation(Usage),
                "chart_cost_by_project_html":
                    self._build_chart_cost_by_project(Usage),
                "chart_tokens_by_model_html":
                    self._build_chart_tokens_by_model(Usage),
                "chart_cost_dist_html":
                    self._build_chart_requests_by_operation(Usage),
            }
        )
        return res

    @api.model
    def _operation_labels(self):
        # Selection here is a static list, so dict() gives {value: label}.
        return dict(
            self.env["etp.assessment.pro.llm.usage"]._fields["operation"].selection
        )

    @api.model
    def _build_chart_cost_by_operation(self, Usage):
        labels = self._operation_labels()
        rows = Usage._read_group([], ["operation"], ["cost_usd:sum"])
        rows = [(op, cost or 0.0) for op, cost in rows if op]
        rows.sort(key=lambda r: r[1], reverse=True)

        peak = max((c for _, c in rows), default=0.0)
        if not peak:
            return Markup('<div class="etp-chart-empty">No LLM spend yet</div>')

        bars = Markup("")
        for op, cost in rows:
            pct = round(cost / peak * 100.0, 1)
            bars += Markup(
                '<div class="etp-cbar-row">'
                '<span class="etp-cbar-name">{name}</span>'
                '<span class="etp-cbar-track">'
                '<span class="etp-cbar-fill" style="width:{pct}%"></span>'
                "</span>"
                '<span class="etp-cbar-val">${val}</span>'
                "</div>"
            ).format(
                name=escape(labels.get(op, op)),
                pct=pct,
                val="{:.4f}".format(cost),
            )
        return Markup('<div class="etp-cbar-wrap">{}</div>').format(bars)

    @api.model
    def _build_chart_tokens_by_operation(self, Usage):
        """Where the tokens actually go: per operation, split into input /
        output / thinking. Thinking tokens matter — Gemini-3 reasoning models
        can burn more hidden 'thoughts' than visible output, and that spend is
        invisible on a plain total. Grouped read only, no python ledger loop."""
        labels = self._operation_labels()
        rows = Usage._read_group(
            [], ["operation"],
            ["tokens_in:sum", "tokens_out:sum", "thoughts_tokens:sum"])
        data = [(op, tin or 0, tout or 0, th or 0)
                for op, tin, tout, th in rows if op]
        data.sort(key=lambda r: r[1] + r[2] + r[3], reverse=True)

        peak = max((tin + tout + th for _, tin, tout, th in data), default=0)
        if not peak:
            return Markup('<div class="etp-chart-empty">No token usage yet</div>')

        bars = Markup("")
        for op, tin, tout, th in data:
            total = tin + tout + th
            i_pct = round(tin / peak * 100.0, 1)
            o_pct = round(tout / peak * 100.0, 1)
            t_pct = round(th / peak * 100.0, 1)
            seg = Markup("")
            if i_pct > 0:
                seg += Markup(
                    '<span class="etp-proj-seg etp-proj-seg--auth" '
                    'style="width:{p}%" title="Input {v} tokens"></span>'
                ).format(p=i_pct, v="{:,}".format(tin))
            if o_pct > 0:
                seg += Markup(
                    '<span class="etp-proj-seg etp-proj-seg--eval" '
                    'style="width:{p}%" title="Output {v} tokens"></span>'
                ).format(p=o_pct, v="{:,}".format(tout))
            if t_pct > 0:
                seg += Markup(
                    '<span class="etp-proj-seg etp-proj-seg--un" '
                    'style="width:{p}%" title="Thinking {v} tokens"></span>'
                ).format(p=t_pct, v="{:,}".format(th))
            bars += Markup(
                '<div class="etp-cbar-row">'
                '<span class="etp-cbar-name">{name}</span>'
                '<span class="etp-proj-track">{seg}</span>'
                '<span class="etp-cbar-val">{val}</span>'
                "</div>"
            ).format(
                name=escape(labels.get(op, op)),
                seg=seg,
                val="{:,}".format(total))
        legend = Markup(
            '<div class="etp-tok-legend">'
            '<span class="etp-proj-legend etp-proj-legend--auth">Input</span>'
            '<span class="etp-proj-legend etp-proj-legend--eval">Output</span>'
            '<span class="etp-tok-legend--think">Thinking</span>'
            '</div>')
        return Markup(
            '<div class="etp-cbar-wrap">{legend}{bars}</div>').format(
            legend=legend, bars=bars)

    @api.model
    def _project_cost_rows(self, Usage):
        """Aggregate LLM spend per project (generator) with NO python loop over
        the ledger — two _read_group passes + one bounded pass over the distinct
        evaluators that carry evaluation spend.

        Attribution:
        * Authoring (generate_questions / generate_image / extract_tags /
          extract_skills / detect / verify / video) — every such row carries
          ``prompt_id`` (the generator), so we group authoring by prompt_id.
        * Evaluation (``score_subjective``) — those rows carry ``evaluator_id``,
          not prompt_id. We group them by evaluator, then resolve
          evaluator -> assessment_id -> generator_id and fold the cost into that
          project's "Evaluation" component.
        * Unattributed — everything not resolvable to a generator (old rows with
          no prompt_id, evaluation rows whose evaluator has no generator, etc.).
          Derived by reconciliation against the grand totals so the buckets
          always sum back to the ledger totals.

        Returns ``(projects, unattributed)`` where ``projects`` is a list of
        per-generator dicts sorted by total cost desc, and ``unattributed`` is a
        dict for the residual bucket (may be all-zero)."""
        projects = {}

        def bucket(gen):
            d = projects.get(gen.id)
            if d is None:
                d = projects[gen.id] = {
                    "gen": gen, "auth": 0.0, "eval": 0.0,
                    "tokens": 0, "requests": 0}
            return d

        auth_rows = Usage._read_group(
            [("prompt_id", "!=", False)],
            ["prompt_id"],
            ["cost_usd:sum", "total_tokens:sum", "__count"])
        for prompt, cost, tokens, cnt in auth_rows:
            d = bucket(prompt)
            d["auth"] += cost or 0.0
            d["tokens"] += tokens or 0
            d["requests"] += cnt or 0

        eval_rows = Usage._read_group(
            [("prompt_id", "=", False), ("operation", "=", "score_subjective"),
             ("evaluator_id", "!=", False)],
            ["evaluator_id"],
            ["cost_usd:sum", "total_tokens:sum", "__count"])
        for evaluator, cost, tokens, cnt in eval_rows:
            gen = evaluator.assessment_id.generator_id
            if not gen:
                continue
            d = bucket(gen)
            d["eval"] += cost or 0.0
            d["tokens"] += tokens or 0
            d["requests"] += cnt or 0

        grand = Usage._read_group([], [], ["cost_usd:sum", "total_tokens:sum"])
        grand_cost, grand_tokens = grand[0] if grand else (0.0, 0)
        attr_cost = sum(d["auth"] + d["eval"] for d in projects.values())
        attr_tokens = sum(d["tokens"] for d in projects.values())
        attr_requests = sum(d["requests"] for d in projects.values())
        unattributed = {
            "cost": round((grand_cost or 0.0) - attr_cost, 6),
            "tokens": (grand_tokens or 0) - attr_tokens,
            "requests": Usage.search_count([]) - attr_requests,
        }

        rows = sorted(
            projects.values(), key=lambda d: (d["auth"] + d["eval"]),
            reverse=True)
        return rows, unattributed

    @api.model
    def _build_chart_cost_by_project(self, Usage):
        rows, unattributed = self._project_cost_rows(Usage)

        peak = max(
            [d["auth"] + d["eval"] for d in rows] + [unattributed["cost"]],
            default=0.0)
        if peak <= 0:
            return Markup(
                '<div class="etp-chart-empty">No project spend yet</div>')

        def _split_bar(auth, evl):
            a_pct = round(max(auth, 0.0) / peak * 100.0, 1)
            e_pct = round(max(evl, 0.0) / peak * 100.0, 1)
            seg = Markup("")
            if a_pct > 0:
                seg += Markup(
                    '<span class="etp-proj-seg etp-proj-seg--auth" '
                    'style="width:{p}%" title="Authoring ${v}"></span>'
                ).format(p=a_pct, v="{:.4f}".format(auth))
            if e_pct > 0:
                seg += Markup(
                    '<span class="etp-proj-seg etp-proj-seg--eval" '
                    'style="width:{p}%" title="Evaluation ${v}"></span>'
                ).format(p=e_pct, v="{:.4f}".format(evl))
            return Markup('<span class="etp-proj-track">{}</span>').format(seg)

        cards = Markup("")
        for d in rows:
            gen = d["gen"]
            total = d["auth"] + d["eval"]
            n_q = gen.approved_count or gen.question_count or 0
            n_tags = len(gen.tag_ids)
            sop = gen.resource_ids.filtered(
                lambda r: r.category == "sop")[:1]
            chips = Markup(
                '<span class="etp-proj-chip">'
                '<i class="fa fa-question-circle"></i> {q} Q</span>'
                '<span class="etp-proj-chip">'
                '<i class="fa fa-tags"></i> {t} tags</span>'
            ).format(q=n_q, t=n_tags)
            if sop:
                chips += Markup(
                    '<span class="etp-proj-chip etp-proj-chip--file" '
                    'title="{full}"><i class="fa fa-file-text-o"></i> {name}'
                    '</span>'
                ).format(
                    full=escape(sop.name or ""),
                    name=escape((sop.name or "")[:28]))
            cards += Markup(
                '<div class="etp-proj-row">'
                '<div class="etp-proj-head">'
                '<span class="etp-proj-name">{name}</span>'
                '<span class="etp-proj-meta">{chips}</span>'
                '<span class="etp-proj-cost">${total}</span>'
                '</div>'
                '{bar}'
                '<div class="etp-proj-foot">'
                '<span class="etp-proj-legend etp-proj-legend--auth">'
                'Authoring ${auth}</span>'
                '<span class="etp-proj-legend etp-proj-legend--eval">'
                'Evaluation ${evl}</span>'
                '<span class="etp-proj-foot-sp">{tokens} tokens · '
                '{requests} req</span>'
                '</div></div>'
            ).format(
                name=escape(gen.name or "Untitled"),
                chips=chips,
                total="{:.4f}".format(total),
                bar=_split_bar(d["auth"], d["eval"]),
                auth="{:.4f}".format(d["auth"]),
                evl="{:.4f}".format(d["eval"]),
                tokens="{:,}".format(d["tokens"]),
                requests=d["requests"])

        if unattributed["cost"] > 0.00005 or unattributed["requests"] > 0:
            u_pct = round(
                max(unattributed["cost"], 0.0) / peak * 100.0, 1)
            cards += Markup(
                '<div class="etp-proj-row etp-proj-row--un">'
                '<div class="etp-proj-head">'
                '<span class="etp-proj-name">Unattributed</span>'
                '<span class="etp-proj-meta">'
                '<span class="etp-proj-chip">no resolvable project</span>'
                '</span>'
                '<span class="etp-proj-cost">${total}</span>'
                '</div>'
                '<span class="etp-proj-track">'
                '<span class="etp-proj-seg etp-proj-seg--un" '
                'style="width:{p}%"></span></span>'
                '<div class="etp-proj-foot">'
                '<span class="etp-proj-foot-sp">{tokens} tokens · '
                '{requests} req</span></div></div>'
            ).format(
                total="{:.4f}".format(max(unattributed["cost"], 0.0)),
                p=u_pct,
                tokens="{:,}".format(max(unattributed["tokens"], 0)),
                requests=max(unattributed["requests"], 0))

        return Markup('<div class="etp-proj-wrap">{}</div>').format(cards)

    @api.model
    def _build_chart_tokens_by_model(self, Usage):
        rows = Usage._read_group([], ["model"], ["total_tokens:sum"])
        rows = [(m, tok or 0) for m, tok in rows if m]
        rows.sort(key=lambda r: r[1], reverse=True)
        rows = rows[:6]

        peak = max((t for _, t in rows), default=0)
        if not peak:
            return Markup('<div class="etp-chart-empty">No token usage yet</div>')

        bars = Markup("")
        for model, tok in rows:
            pct = round(tok / peak * 100.0, 1)
            bars += Markup(
                '<div class="etp-cbar-row">'
                '<span class="etp-cbar-name">{name}</span>'
                '<span class="etp-cbar-track">'
                '<span class="etp-cbar-fill" style="width:{pct}%"></span>'
                "</span>"
                '<span class="etp-cbar-val">{val}</span>'
                "</div>"
            ).format(name=escape(model), pct=pct, val="{:,}".format(tok))
        return Markup('<div class="etp-cbar-wrap">{}</div>').format(bars)

    @api.model
    def _build_chart_requests_by_operation(self, Usage):
        labels = self._operation_labels()
        rows = Usage._read_group([], ["operation"], ["__count"])
        rows = [(op, cnt or 0) for op, cnt in rows if op]
        rows.sort(key=lambda r: r[1], reverse=True)

        peak = max((c for _, c in rows), default=0)
        if not peak:
            return Markup('<div class="etp-chart-empty">No LLM calls yet</div>')

        cols = Markup("")
        for op, count in rows:
            height = round(count / peak * 100.0, 1)
            cols += Markup(
                '<div class="etp-dist-col">'
                '<span class="etp-dist-count">{count}</span>'
                '<span class="etp-dist-bar-track">'
                '<span class="etp-dist-bar" style="height:{h}%"></span>'
                "</span>"
                '<span class="etp-dist-label">{label}</span>'
                "</div>"
            ).format(count=count, h=height, label=escape(labels.get(op, op)))
        # .etp-dist-wrap hardcodes repeat(4,1fr); override to the actual count.
        return Markup(
            '<div class="etp-dist-wrap" '
            'style="grid-template-columns:repeat({n},1fr)">{cols}</div>'
        ).format(n=len(rows), cols=cols)
