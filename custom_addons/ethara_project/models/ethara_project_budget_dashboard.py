from odoo import fields, models


class EtharaProjectBudgetDashboard(models.AbstractModel):
    _name = "ethara.project.budget.dashboard"
    _description = "Ethara Project Budget Health Dashboard Data Provider"

    def get_dashboard_data(self, project_id=None):
        Budget = self.env["ethara.project.budget"].sudo()
        domain = [("active", "=", True)]
        if project_id:
            domain.append(("ethara_project_id", "=", int(project_id)))
        budgets = Budget.search(domain)

        rows = []
        agg_total_env = 0.0
        agg_consumed = 0.0
        agg_remaining = 0.0
        health_buckets = {
            "unknown": 0,
            "healthy": 0,
            "warning": 0,
            "at_risk": 0,
            "critical": 0,
        }
        for b in budgets:
            rows.append({
                "id": b.id,
                "name": b.name or "",
                "project_id": b.ethara_project_id.id,
                "project_name": b.ethara_project_id.name or "",
                "state": b.state,
                "project_type": b.project_type,
                "budget_amount": float(b.budget_amount or 0.0),
                "consumed_amount": float(b.consumed_amount or 0.0),
                "remaining_amount": float(b.remaining_amount or 0.0),
                "consumed_pct": float(b.consumed_pct or 0.0),
                "health_status": b.health_status or "unknown",
                "last_fetched_at": (
                    b.last_fetched_at.isoformat() if b.last_fetched_at else None
                ),
            })
            agg_total_env += float(b.budget_amount or 0.0)
            agg_consumed += float(b.consumed_amount or 0.0)
            agg_remaining += float(b.remaining_amount or 0.0)
            key = b.health_status if b.health_status in health_buckets else "unknown"
            health_buckets[key] = health_buckets.get(key, 0) + 1

        return {
            "totals": {
                "budget_amount": agg_total_env,
                "consumed_amount": agg_consumed,
                "remaining_amount": agg_remaining,
                "consumed_pct": (
                    (agg_consumed / agg_total_env * 100.0)
                    if agg_total_env else 0.0
                ),
                "budget_count": len(rows),
            },
            "health_counts": health_buckets,
            "budgets": rows,
        }
