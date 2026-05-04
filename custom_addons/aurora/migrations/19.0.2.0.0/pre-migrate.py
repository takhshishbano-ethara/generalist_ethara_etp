"""Aurora 19.0.2.0.0 pre-migration: LHT-only identity model.

Version 19.0.2.0.0 removes ``pr_number`` from aurora.pipeline.result and
aurora.evaluation.instance, replacing it with LHT fields (instance_id,
tag_start, tag_end, pr_numbers, pr_attribution_method, version_scheme).

Legacy rows populated under 19.0.1.0.0 carry only ``pr_number`` and cannot
be migrated automatically: the tag_start/tag_end values for those rows
were never captured. This script checks for legacy rows and raises a
UserError so the upgrade is not silently performed.

Operator options when legacy rows are present:
  1. DELETE FROM aurora_pipeline_result;
     DELETE FROM aurora_evaluation_instance;
     (re-run evaluations after upgrade to repopulate with LHT fields)

  2. Backfill tag_start/tag_end/instance_id manually via a custom SQL
     script, then drop pr_number, then run the upgrade.

No option is chosen for you. The upgrade will stop until the operator
decides.
"""
import logging

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'aurora_evaluation_instance' AND column_name = 'pr_number'"
    )
    legacy_column_exists = bool(cr.fetchone())
    if not legacy_column_exists:
        _logger.info("Aurora 19.0.2.0.0: no legacy pr_number column, skipping check.")
        return

    cr.execute("SELECT COUNT(*) FROM aurora_evaluation_instance")
    eval_rows = cr.fetchone()[0]
    cr.execute("SELECT COUNT(*) FROM aurora_pipeline_result")
    pipe_rows = cr.fetchone()[0]

    if eval_rows == 0 and pipe_rows == 0:
        _logger.info(
            "Aurora 19.0.2.0.0: legacy pr_number column exists but both tables "
            "are empty; safe to proceed."
        )
        return

    msg = (
        "Aurora 19.0.2.0.0 migration blocked.\n\n"
        f"Found {eval_rows} legacy row(s) in aurora_evaluation_instance and "
        f"{pipe_rows} row(s) in aurora_pipeline_result using the pre-LHT "
        "pr_number schema. These rows cannot be migrated automatically "
        "because tag_start/tag_end were not captured under the old schema.\n\n"
        "Resolve one of the following and re-run the upgrade:\n"
        "  (a) Delete legacy rows:\n"
        "        DELETE FROM aurora_pipeline_result;\n"
        "        DELETE FROM aurora_evaluation_instance;\n"
        "      (re-run evaluations after upgrade to repopulate)\n\n"
        "  (b) Backfill tag_start, tag_end, instance_id, pr_numbers "
        "manually via a custom SQL script before upgrading.\n"
    )
    _logger.error(msg)
    raise UserError(msg)
