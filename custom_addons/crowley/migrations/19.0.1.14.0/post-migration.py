"""Post-migration for 19.0.1.14.0.

PR #2 adds the human-override fields to crowley.video.review and introduces
the stored computed `effective_verdict`. Odoo's ORM will auto-init the column
during update, but we ensure every existing row has a non-NULL value (the
compute reads `human_verdict or verdict` which is fine; this is just an
explicit safety net + indexing nudge for older PostgreSQL rows that may have
been written before the column existed).
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE crowley_video_review
        SET effective_verdict = verdict
        WHERE effective_verdict IS NULL
          AND verdict IS NOT NULL
        """
    )
    affected = cr.rowcount or 0
    _logger.info(
        "crowley 19.0.1.14.0: backfilled effective_verdict on %d rows", affected,
    )

    cr.execute(
        """
        SELECT COUNT(*) FROM crowley_video_review
        WHERE human_verdict IS NOT NULL
        """
    )
    overridden = (cr.fetchone() or [0])[0]
    if overridden:
        _logger.warning(
            "crowley 19.0.1.14.0: %d row(s) already have human_verdict set "
            "before migration ran; leaving effective_verdict as-is.",
            overridden,
        )

    # PR #4: bump per-category attempt sequences from padding 6 -> 7 to match
    # the 523 master/failures dataset filename convention (T2AV_<cat>_NNNNNNN.mp4).
    # Live `number_next` values are preserved — only the zero-pad width changes,
    # so existing T2AV_<cat>_NNNNNN.mp4 filenames remain valid and new uploads
    # acquire NNNNNNN-padded names from the next slot onward.
    cr.execute(
        """
        UPDATE ir_sequence
        SET padding = 7
        WHERE code LIKE 'crowley.attempt.%%'
          AND padding < 7
        """
    )
    bumped = cr.rowcount or 0
    _logger.info(
        "crowley 19.0.1.14.0: bumped padding on %d per-category attempt sequence(s) "
        "from 6 to 7", bumped,
    )
