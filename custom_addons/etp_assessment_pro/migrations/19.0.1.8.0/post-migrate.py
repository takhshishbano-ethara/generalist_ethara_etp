# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.8.0. Runs once on the transition TO 1.8.0.

The `image_text` question type was split into `image_prompt` (the candidate
writes the text-to-image prompt for the shown image) and `image_label` (the
candidate labels/identifies elements in a single image), each with its own
scoring rubric. No automatic mapping from the old combined type to either new
one is meaningful, so the old data is HARD-DELETED here: every `image_text` row
in the question bank and in the draft prompt-question table is unlinked
(question.image children cascade via their ondelete)."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    questions = env["etp.assessment.pro.question"].search(
        [("question_type", "=", "image_text")])
    n_questions = len(questions)
    questions.unlink()
    drafts = env["etp.assessment.pro.prompt.question"].search(
        [("question_type", "=", "image_text")])
    n_drafts = len(drafts)
    drafts.unlink()
    _logger.info(
        "post-migrate 1.8.0: deleted %s image_text bank question(s) and %s "
        "image_text draft(s); image_text is now split into image_prompt and "
        "image_label", n_questions, n_drafts)
