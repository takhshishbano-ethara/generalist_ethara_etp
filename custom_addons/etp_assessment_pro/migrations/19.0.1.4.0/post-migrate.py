# -*- coding: utf-8 -*-
"""Post-migrate for 19.0.1.4.0. Runs once on the transition TO 1.4.0.

bp-1 unified TWO old global thresholds — `etp_assessment_pro.pass_threshold`
(overall candidate pass %) and `etp_assessment_pro.subjective_pass_threshold`
(per-answer subjective %) — into ONE per-assessment `subjective_threshold`. This:

  1. seeds `subjective_threshold` from the old params (prefer the OVERALL bar, the
     candidate-facing Pass/Fail decision), warning loudly if the two differed;
  2. flags assessments-with-candidates for pass/fail recompute (Option A removed
     the @api.depends, so the stored rollups would otherwise stay stale);
  3. backfills `invite_state='sent'` on existing candidates (they were all already
     invited under 1.3.1) so a later 'Resend Invitations' can't mass re-email the
     historical cohort.
"""
import logging

_logger = logging.getLogger(__name__)


def _param(cr, key):
    cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", (key,))
    row = cr.fetchone()
    if not (row and row[0]):
        return None
    try:
        v = float(row[0])
    except (TypeError, ValueError):
        return None
    v = v * 100.0 if v <= 1.0 else v
    return v if 0.0 <= v <= 100.0 else None


def migrate(cr, version):
    if not version:
        return

    overall = _param(cr, "etp_assessment_pro.pass_threshold")
    per_answer = _param(cr, "etp_assessment_pro.subjective_pass_threshold")
    seed = overall if overall is not None else (
        per_answer if per_answer is not None else 70.0)

    if (overall is not None and per_answer is not None
            and abs(overall - per_answer) > 0.001):
        _logger.warning(
            "post-migrate 1.4.0: the two old global thresholds DIFFERED "
            "(pass_threshold=%s, subjective_pass_threshold=%s). bp-1 unifies "
            "them into ONE per-assessment 'Subjective Pass Threshold' seeded to "
            "%s. REVIEW per-assessment thresholds: assessments that relied on the "
            "overall and per-answer bars differing will re-decide Pass/Fail.",
            overall, per_answer, seed)

    cr.execute("UPDATE etp_assessment_pro SET subjective_threshold = %s", (seed,))
    _logger.info("post-migrate 1.4.0: seeded subjective_threshold=%s on %s "
                 "assessment(s)", seed, cr.rowcount)

    cr.execute("""
        UPDATE etp_assessment_pro SET threshold_recompute_pending = TRUE
        WHERE id IN (SELECT DISTINCT assessment_id
                     FROM etp_assessment_pro_evaluator)
    """)
    _logger.info("post-migrate 1.4.0: flagged %s assessment(s) with candidates "
                 "for background pass/fail recompute", cr.rowcount)

    cr.execute("UPDATE etp_assessment_pro_evaluator "
               "SET invite_state = 'sent' WHERE invite_state = 'none'")
    _logger.info("post-migrate 1.4.0: marked %s existing candidate(s) invite "
                 "'sent' (no mass re-invite of the historical cohort)",
                 cr.rowcount)
