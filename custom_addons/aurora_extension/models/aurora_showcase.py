"""Seeded, admin-editable content for the Aurora / Milo-Bench showcase.

These models hold the descriptive "paper" content that the Flutter Aurora
page used to keep as hardcoded static data (methodology principles, pipeline
steps, external resource links, per-model summary stats). They are seeded from
``data/aurora_showcase_data.xml`` and can be edited by an Aurora admin without
a frontend release.

The benchmark *instances table* and the KPIs / quality tiers are NOT stored
here — those are computed live from ``aurora.evaluation.instance`` by the
controllers.
"""

from odoo import fields, models


def _bullets_to_list(text):
    """Split a newline-separated bullet block into a clean list of strings."""
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


class AuroraShowcaseResource(models.Model):
    _name = "aurora.showcase.resource"
    _description = "Aurora Showcase External Resource"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    label = fields.Char(string="Label", required=True)
    title = fields.Char(string="Title", required=True)
    url = fields.Char(string="URL", required=True)
    active = fields.Boolean(default=True)

    def to_dict(self):
        self.ensure_one()
        return {
            "label": self.label or "",
            "title": self.title or "",
            "url": self.url or "",
        }


class AuroraShowcaseMethodology(models.Model):
    _name = "aurora.showcase.methodology"
    _description = "Aurora Showcase Methodology Principle"
    _order = "sequence, principle_number, id"

    sequence = fields.Integer(default=10)
    principle_number = fields.Integer(string="Principle Number", required=True)
    title = fields.Char(string="Title", required=True)
    bullet_text = fields.Text(
        string="Bullets",
        help="One bullet per line.",
    )
    active = fields.Boolean(default=True)

    def to_dict(self):
        self.ensure_one()
        return {
            "principle_number": self.principle_number,
            "title": self.title or "",
            "bullets": _bullets_to_list(self.bullet_text),
        }


class AuroraShowcasePipelineStep(models.Model):
    _name = "aurora.showcase.pipeline.step"
    _description = "Aurora Showcase Pipeline Step"
    _order = "sequence, phase, id"

    sequence = fields.Integer(default=10)
    phase = fields.Integer(string="Phase", required=True)
    title = fields.Char(string="Title", required=True)
    bullet_text = fields.Text(
        string="Bullets",
        help="One bullet per line.",
    )
    active = fields.Boolean(default=True)

    def to_dict(self):
        self.ensure_one()
        return {
            "phase": self.phase,
            "title": self.title or "",
            "bullets": _bullets_to_list(self.bullet_text),
        }


class AuroraShowcaseModelStat(models.Model):
    _name = "aurora.showcase.model.stat"
    _description = "Aurora Showcase Model Summary Stat"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    key = fields.Char(
        string="Key",
        required=True,
        help="Stable identifier, e.g. 'kimi' or 'glm5'.",
    )
    name = fields.Char(string="Display Name", required=True)
    overall_pass_rate = fields.Char(string="Overall Pass Rate")
    short_trajectories = fields.Char(string="Short Trajectories")
    long_trajectories = fields.Char(string="Long Trajectories")
    avg_cost_per_instance = fields.Char(string="Avg Cost / Instance")
    active = fields.Boolean(default=True)

    def to_dict(self):
        self.ensure_one()
        return {
            "key": self.key or "",
            "name": self.name or "",
            "overall_pass_rate": self.overall_pass_rate or "",
            "short_trajectories": self.short_trajectories or "",
            "long_trajectories": self.long_trajectories or "",
            "avg_cost_per_instance": self.avg_cost_per_instance or "",
        }
