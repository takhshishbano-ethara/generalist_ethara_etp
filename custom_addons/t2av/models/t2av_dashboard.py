from odoo import api, fields, models

# Dashboard aggregation service for the T2AV dashboard.
# Kept in its own small model so the dashboard data contract is easy to read
# and review in isolation from the large t2av.generation model.

# The lifecycle states we treat as "still working" (not terminal).
_IN_PROGRESS_STATES = ("queued", "submitting", "processing", "downloading")


class T2AVDashboard(models.AbstractModel):
    _name = "t2av.dashboard"
    _description = "T2AV Dashboard data provider"

    @api.model
    def get_dashboard_data(self, domain=None):
        """Return every number the dashboard shows, in one call.

        Optional ``domain`` lets the front-end scope the whole dashboard
        (e.g. last 24h, or a single category) and have all cards recompute.
        """
        Gen = self.env["t2av.generation"]
        base_domain = domain or []

        # --- headline counts via grouped reads (cheap, single queries) -----
        def count(extra):
            return Gen.search_count(base_domain + extra)

        total = count([])
        done = count([("state", "=", "done")])
        failed = count([("state", "in", ("failed", "cancelled"))])
        in_progress = count([("state", "in", _IN_PROGRESS_STATES)])
        draft = count([("state", "=", "draft")])

        # --- total cost across the (optionally scoped) set -----------------
        cost_groups = Gen.read_group(base_domain, ["cost_usd:sum"], [])
        total_cost = (cost_groups[0].get("cost_usd") or 0.0) if cost_groups else 0.0

        # --- lifecycle breakdown (the Pipeline Lifecycle card) -------------
        status_rows = Gen.read_group(
            base_domain, ["state"], ["state"], lazy=False,
        )
        by_status = [
            {"key": r["state"], "label": self._state_label(r["state"]),
             "count": r["__count"]}
            for r in status_rows if r.get("state")
        ]

        # --- per-category breakdown (the main grid) ------------------------
        cat_labels = dict(Gen._fields["category"].selection)
        by_category = []
        for slug, label in Gen._fields["category"].selection:
            cat_dom = base_domain + [("category", "=", slug)]
            c_total = Gen.search_count(cat_dom)
            if not c_total:
                continue
            by_category.append({
                "key": slug,
                "label": label,
                "total": c_total,
                "done": Gen.search_count(cat_dom + [("state", "=", "done")]),
                "in_progress": Gen.search_count(
                    cat_dom + [("state", "in", _IN_PROGRESS_STATES)]),
                "failed": Gen.search_count(
                    cat_dom + [("state", "in", ("failed", "cancelled"))]),
            })
        by_category.sort(key=lambda r: r["total"], reverse=True)

        # --- recent activity feed ------------------------------------------
        recent = []
        for g in Gen.search(base_domain, order="create_date desc", limit=12):
            recent.append({
                "id": g.id,
                "name": g.name,
                "category": cat_labels.get(g.category, g.category or "—"),
                "prompt": (g.prompt or "")[:90],
                "state": g.state,
                "state_label": self._state_label(g.state),
                "cost": g.cost_usd,
                "create_date": fields.Datetime.to_string(g.create_date),
            })

        return {
            "kpis": {
                "total": total, "done": done, "in_progress": in_progress,
                "failed": failed, "draft": draft,
                "total_cost": round(total_cost, 2),
            },
            "by_status": by_status,
            "by_category": by_category,
            "recent": recent,
        }

    # -- label helper so the front-end shows human text, not slugs ---------
    @api.model
    def _state_label(self, key):
        return dict(
            self.env["t2av.generation"]._fields["state"].selection
        ).get(key, key or "—")
