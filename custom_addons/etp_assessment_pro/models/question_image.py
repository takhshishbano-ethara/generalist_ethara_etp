# -*- coding: utf-8 -*-
"""Image attachments for image-evaluation question types.

Each ``etp.assessment.pro.question.image`` is one picture shown to the
candidate in the portal for an ``image_ab`` / ``image_text`` question. The
``slot`` tells the portal/scorer which image is which:

- ``image_ab`` uses ``a`` + ``b`` (Response A vs Response B).
- ``image_text`` uses ``single`` / ``reference`` / ``output``.

Storage: primary = Odoo Binary (``attachment=True``), always served by
``/web/image/etp.assessment.pro.question.image/<id>/image``. An optional S3
offload (see ``question.action_offload_images_s3``) pushes the binary to S3
and stores the CDN/S3 URL in ``image_url``; when set, the portal prefers it.
"""
from odoo import api, models, fields


class EtpAssessmentQuestionImage(models.Model):
    _name = "etp.assessment.pro.question.image"
    _description = "Assessment Question Image"
    _order = "sequence, id"

    question_id = fields.Many2one(
        "etp.assessment.pro.question",
        string="Question",
        required=True,
        ondelete="cascade",
    )
    question_type = fields.Selection(
        related="question_id.question_type",
        store=True,
        help="Mirror of the parent question type; gates which Slot selector "
             "is shown in the Image Evaluation list.")
    label = fields.Char(
        string="Label", required=True,
        help="Shown to the candidate, e.g. 'Response A', 'Reference', 'Output'.")
    slot = fields.Selection(
        [
            ("a", "Response A"),
            ("b", "Response B"),
            ("single", "Single"),
            ("reference", "Reference"),
            ("output", "Output"),
        ],
        string="Slot",
        required=True,
        default="single",
        help="image_ab uses A + B; image_text uses Single / Reference / Output.",
    )
    # UI-only helpers: each exposes just the slots valid for one question type
    # and stays in sync with the canonical ``slot`` above (compute reads slot;
    # inverse writes it back when the author edits the selector).
    slot_ab = fields.Selection(
        [("a", "Response A"), ("b", "Response B")],
        string="Slot",
        compute="_compute_slot_helpers",
        inverse="_inverse_slot_ab",
        help="image_ab slot selector; synced to the canonical slot field.")
    slot_text = fields.Selection(
        [("single", "Single"), ("reference", "Reference"), ("output", "Output")],
        string="Slot",
        compute="_compute_slot_helpers",
        inverse="_inverse_slot_text",
        help="image_text slot selector; synced to the canonical slot field.")
    image = fields.Binary(string="Image", attachment=True)
    image_url = fields.Char(
        string="Image URL",
        help="Optional external/S3 URL. When set, the portal serves this "
             "instead of the stored binary.")
    sequence = fields.Integer(default=10)

    @api.depends("slot")
    def _compute_slot_helpers(self):
        """Project the canonical slot onto the per-type helper selectors."""
        for rec in self:
            rec.slot_ab = rec.slot if rec.slot in ("a", "b") else False
            rec.slot_text = (
                rec.slot if rec.slot in ("single", "reference", "output")
                else False)

    def _inverse_slot_ab(self):
        for rec in self:
            if rec.slot_ab:
                rec.slot = rec.slot_ab

    def _inverse_slot_text(self):
        for rec in self:
            if rec.slot_text:
                rec.slot = rec.slot_text

    @api.onchange("question_type")
    def _onchange_question_type_slot_default(self):
        """Give new rows a sensible slot for the parent question type so the
        canonical slot never lingers on an out-of-type default value."""
        for rec in self:
            if rec.question_type == "image_ab" and rec.slot not in ("a", "b"):
                rec.slot = "a"
            elif (rec.question_type == "image_text"
                  and rec.slot not in ("single", "reference", "output")):
                rec.slot = "single"
