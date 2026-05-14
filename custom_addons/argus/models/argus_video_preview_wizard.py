# -*- coding: utf-8 -*-
"""Argus Video Preview Wizard.

A TransientModel whose only job is to render an Instagram reel inside
an Odoo modal dialog so the operator can scrub through the input or
output video without leaving the task form.

Why a wizard and not just a URL action?
---------------------------------------
``ir.actions.act_url`` with ``target="new"`` opens the reel in a new
browser TAB, which yanks the operator out of Odoo.  Wrapping the
embed iframe in a TransientModel and returning an
``ir.actions.act_window`` with ``target="new"`` makes Odoo render
the form inside a modal dialog on the SAME page — that's the
"popup" UX the spec calls for.

The preview HTML is composed by ``argus.task._open_preview`` and
passed in via context (``default_preview_html``).  The HTML field
is declared with ``sanitize=False`` so the inline ``<iframe>`` tag
survives the write — by default Odoo's Html field strips iframes as
an XSS guard.  Because we control the entire string locally (built
from the shortcode the record already validated), the sanitizer
adds no real security here and would just break the embed.
"""

from odoo import fields, models


class ArgusVideoPreviewWizard(models.TransientModel):
    _name = "argus.video.preview.wizard"
    _description = "Argus Video Preview (modal dialog)"

    title = fields.Char(
        string="Title",
        readonly=True,
    )
    video_url = fields.Char(
        string="Source URL",
        readonly=True,
        help="Original Instagram URL (link visible at the bottom of "
             "the preview for copy-paste / open-in-new-tab).",
    )
    preview_html = fields.Html(
        string="Preview",
        readonly=True,
        sanitize=False,
        help="Pre-built ``<iframe>`` markup pointing at Instagram's "
             "official embed URL for the reel.  Built by "
             "``argus.task._open_preview`` and injected via context.",
    )
