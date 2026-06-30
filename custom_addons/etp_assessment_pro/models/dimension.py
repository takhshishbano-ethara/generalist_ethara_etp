from odoo import models, fields, api
from odoo.exceptions import UserError


def _questions_using_dimension(env, dimension_ids):
    """How many distinct questions reference each given master dimension."""
    QDim = env["etp.assessment.pro.question.dimension"]
    counts = {}
    for did in dimension_ids:
        links = QDim.search([("dimension_id", "=", did)])
        counts[did] = len(set(links.mapped("question_id").ids))
    return counts


class EtpAssessmentDimension(models.Model):
    _name = "etp.assessment.pro.dimension"
    _description = "Assessment Dimension"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    option_ids = fields.One2many(
        "etp.assessment.pro.dimension.option", "dimension_id", string="Options"
    )
    option_count = fields.Integer(
        string="Options", compute="_compute_option_count"
    )
    options_display = fields.Char(
        string="Options Preview", compute="_compute_options_display"
    )
    question_use_count = fields.Integer(
        string="Used by Questions", compute="_compute_question_use_count",
        help="How many distinct questions reference this dimension. When >1, "
             "editing its options here changes ALL of them — duplicate it for "
             "a single question instead.")

    @api.depends("option_ids")
    def _compute_option_count(self):
        for rec in self:
            rec.option_count = len(rec.option_ids)

    def _compute_question_use_count(self):
        counts = _questions_using_dimension(self.env, self.ids)
        for rec in self:
            rec.question_use_count = counts.get(rec.id, 0)

    @api.depends("option_ids", "option_ids.name")
    def _compute_options_display(self):
        for rec in self:
            parts = [opt.name for opt in rec.option_ids.sorted("sequence")]
            rec.options_display = ", ".join(parts) if parts else ""

    def _guard_shared_option_edit(self):
        """Refuse add/remove of options on a master shared by >1 question (would rewrite every borrower)."""
        if self.env.context.get("allow_shared_dimension_edit"):
            return
        counts = _questions_using_dimension(self.env, self.ids)
        shared = [rec for rec in self if counts.get(rec.id, 0) > 1]
        if shared:
            names = ", ".join("%r (used by %s questions)"
                              % (r.name, counts[r.id]) for r in shared)
            raise UserError(
                "This dimension is shared across multiple questions: %s.\n\n"
                "Changing its options here would silently change every one of "
                "them. Duplicate the dimension for the specific question, or "
                "edit that question's own answer key instead." % names)

    def write(self, vals):
        if "option_ids" in vals:
            self._guard_shared_option_edit()
        return super().write(vals)


class EtpAssessmentDimensionOption(models.Model):
    _name = "etp.assessment.pro.dimension.option"
    _description = "Assessment Dimension Option"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    dimension_id = fields.Many2one(
        "etp.assessment.pro.dimension", required=True, ondelete="cascade"
    )

    def _guard_shared(self):
        """Block rename/reorder when the dimension is shared by >1 question (a stored related field mirrors it to every borrower)."""
        if self.env.context.get("allow_shared_dimension_edit"):
            return
        dim_ids = self.mapped("dimension_id").ids
        counts = _questions_using_dimension(self.env, dim_ids)
        shared = {d: n for d, n in counts.items() if n > 1}
        if shared:
            total = max(shared.values())
            raise UserError(
                "This option belongs to a dimension shared by %s questions. "
                "Renaming or reordering it here would silently change the same "
                "option on every one of them.\n\n"
                "To change just one question, duplicate its dimension (making "
                "it private) and edit that copy, or edit the question's own "
                "answer key." % total)

    def write(self, vals):
        # Rename/reorder is the cross-edit footgun; other writes are fine.
        if "name" in vals or "sequence" in vals:
            self._guard_shared()
        return super().write(vals)

    def unlink(self):
        self._guard_shared()
        return super().unlink()
