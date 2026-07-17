# -*- coding: utf-8 -*-
"""Retire data/llm_config_parameters.xml.

The file seeded ir.config_parameter rows with noupdate="1". A row shadows the
code default (_param -> get_param(key, default) returns the row), so the seeded
value FROZE each setting at install: bumping e.g. GENERATION_DEFAULT_MODEL in
constants.py reached no existing deployment. It also aborted the upgrade whenever
a key already existed from a Settings save (ir_config_parameter_key_uniq is on
key; _load_records matches on xmlid).

Deleting the file is not enough: _process_end skips noupdate=True rows
(ir_model.py, `COALESCE(noupdate, false) != True`), so they would survive frozen
forever. This drops them.

DISCRIMINATOR: the xmlid, not the value. A row the data file created carries our
ir_model_data row; a row an admin created via set_param does not. Value alone is
NOT sufficient -- the seeded values changed over time (vertex_model was seeded as
gemini-2.5-pro, then gemini-3.1-pro-preview, then gemini-3-pro-image), so a DB
installed in June holds a stale seed that matches none of today's values and
would be misread as an admin's choice. _SEEDED therefore lists EVERY value each
key was ever seeded with, harvested from all 5 revisions of the deleted file.

  ours + known seed + never written -> untouched seed -> drop row (constants.py answers)
  ours + anything else             -> admin edited it -> keep row, drop our xmlid
  not ours                         -> admin created it -> keep row

write_date > create_date is the second guard. Value alone cannot tell an untouched
seed from an admin who deliberately re-typed a value that happens to equal an old
seed (e.g. pinning vertex_model back to gemini-2.5-pro, or vertex_location to
us-central1 -- both retired seeds that differ from today's constants default, so
dropping them would silently change the model/region). A seeded row is written once
by _load_records and never again (noupdate="1"), so an untouched seed always has
write_date == create_date; any Settings save bumps write_date. If the signal is ever
absent we keep the row -- this fails safe.

Settings needs no default= to stay usable: a blank char field saves as False,
which set_values skips (no row written), and the code default answers. That is
what the "Leave empty to fall back to ..." help text already promises. Adding
default= would be actively harmful -- the first Settings save would persist it
and re-create the freeze.
"""
import logging

_logger = logging.getLogger(__name__)

_MODULE = "etp_assessment_pro"

# key -> (xmlid, {every value the data file ever seeded, across its 5 revisions})
# vertex_image_model is a RETIRED key (seeded 2026-06-25, renamed to image_model);
# no code reads it, so on a DB from that window it is dead frozen cruft.
_SEEDED = {
    "etp_assessment_pro.vertex_location": (
        "param_vertex_location", {"us-central1", "global"}),
    "etp_assessment_pro.vertex_model": (
        "param_vertex_model",
        {"gemini-2.5-pro", "gemini-3.1-pro-preview", "gemini-3-pro-image"}),
    "etp_assessment_pro.vertex_image_model": (
        "param_vertex_image_model", {"gemini-3-pro-image"}),
    "etp_assessment_pro.generation_model": (
        "param_generation_model", {"gemini-3.1-pro-preview"}),
    "etp_assessment_pro.scoring_model": (
        "param_scoring_model", {"gemini-3.1-pro-preview"}),
    "etp_assessment_pro.image_model": (
        "param_image_model", {"gemini-3-pro-image"}),
    "etp_assessment_pro.verify_flaw_render": (
        "param_verify_flaw_render", {"1"}),
    "etp_assessment_pro.video_model": (
        "param_video_model", {"veo-3.1-generate-001"}),
    "etp_assessment_pro.video_location": (
        "param_video_location", {"us-central1"}),
    "etp_assessment_pro.video_default_duration_s": (
        "param_video_default_duration_s", {"6"}),
    "etp_assessment_pro.s3_region": ("param_s3_region", {"us-east-1"}),
    "etp_assessment_pro.s3_folder": ("param_s3_folder", {"etp_assessment"}),
    "etp_assessment_pro.s3_max_retries": ("param_s3_max_retries", {"3"}),
}


def migrate(cr, version):
    if not version:
        return
    dropped = kept = 0
    for key, (xmlid, seeds) in _SEEDED.items():
        cr.execute(
            """
            SELECT p.id, p.value, d.id,
                   COALESCE(p.write_date, p.create_date) > p.create_date
              FROM ir_config_parameter p
              LEFT JOIN ir_model_data d
                ON d.model = 'ir.config_parameter' AND d.res_id = p.id
               AND d.module = %s AND d.name = %s
             WHERE p.key = %s
            """,
            (_MODULE, xmlid, key),
        )
        row = cr.fetchone()
        if row:
            param_id, value, data_id, was_written = row
            if data_id and value in seeds and not was_written:
                cr.execute("DELETE FROM ir_config_parameter WHERE id = %s",
                           (param_id,))
                dropped += 1
            else:
                kept += 1
                if not data_id:
                    why = "not seeded by us"
                elif was_written:
                    why = "written since install"
                else:
                    why = "value is not a known seed"
                _logger.info(
                    "post-migrate 19.0.1.117.0: keeping %s=%r (%s)",
                    key, value, why)
        # Drop our xmlid claim either way; also clears rows left dangling by a
        # param deleted earlier (set_param(key, False) unlinks the param only).
        cr.execute(
            """
            DELETE FROM ir_model_data
             WHERE module = %s AND name = %s AND model = 'ir.config_parameter'
            """,
            (_MODULE, xmlid),
        )
    _logger.info(
        "post-migrate 19.0.1.117.0: llm_config_parameters.xml retired; dropped "
        "%d un-edited seed(s), kept %d admin-set value(s). constants.py is the "
        "default again.", dropped, kept)
