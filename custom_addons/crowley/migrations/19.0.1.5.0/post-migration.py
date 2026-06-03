import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info("Crowley: post-migration to 19.0.1.5.0 starting (duplicate-prompt prevention)")

    cr.execute("""
        UPDATE crowley_attempt a
        SET original_prompt = g.original_prompt
        FROM crowley_generation g
        WHERE a.job_id = g.id
          AND a.original_prompt IS NULL
          AND g.original_prompt IS NOT NULL
          AND btrim(g.original_prompt) <> ''
    """)
    _logger.info("Crowley: backfilled original_prompt on %d attempt(s)", cr.rowcount)

    cr.execute(r"""
        UPDATE crowley_attempt
        SET prompt_normalized = NULLIF(
            LOWER(REGEXP_REPLACE(BTRIM(prompt), '\s+', ' ', 'g')),
            ''
        )
        WHERE prompt IS NOT NULL
    """)
    _logger.info("Crowley: normalized prompt on %d attempt(s)", cr.rowcount)

    cr.execute(r"""
        UPDATE crowley_attempt
        SET original_prompt_normalized = NULLIF(
            LOWER(REGEXP_REPLACE(BTRIM(original_prompt), '\s+', ' ', 'g')),
            ''
        )
        WHERE original_prompt IS NOT NULL
    """)
    _logger.info("Crowley: normalized original_prompt on %d attempt(s)", cr.rowcount)

    cr.execute("""
        SELECT prompt_normalized, COUNT(*) AS n, array_agg(id ORDER BY id) AS ids
        FROM crowley_attempt
        WHERE state IN ('queued', 'submitting', 'processing', 'downloading', 'done')
          AND prompt_normalized IS NOT NULL
        GROUP BY prompt_normalized
        HAVING COUNT(*) > 1
    """)
    prompt_dups = cr.fetchall()

    cr.execute("""
        SELECT original_prompt_normalized, COUNT(*) AS n, array_agg(id ORDER BY id) AS ids
        FROM crowley_attempt
        WHERE state IN ('queued', 'submitting', 'processing', 'downloading', 'done')
          AND original_prompt_normalized IS NOT NULL
        GROUP BY original_prompt_normalized
        HAVING COUNT(*) > 1
    """)
    original_dups = cr.fetchall()

    if prompt_dups or original_dups:
        _logger.warning(
            "Crowley: found %d duplicate prompt group(s) and %d duplicate original_prompt "
            "group(s) in existing done attempts. Partial unique indexes will NOT be created. "
            "Application-level _check_duplicate_prompts still blocks new submissions. "
            "Clean up duplicates and re-run module upgrade to enable DB-level enforcement.",
            len(prompt_dups),
            len(original_dups),
        )
        for norm, n, ids in prompt_dups[:5]:
            _logger.warning(
                "  prompt-dup: %d copies, attempt ids=%s, sample=%r",
                n, ids, (norm or "")[:80],
            )
        for norm, n, ids in original_dups[:5]:
            _logger.warning(
                "  original_prompt-dup: %d copies, attempt ids=%s, sample=%r",
                n, ids, (norm or "")[:80],
            )
    else:
        cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS crowley_attempt_dup_prompt_active_idx
            ON crowley_attempt (prompt_normalized)
            WHERE state IN ('queued', 'submitting', 'processing', 'downloading', 'done')
              AND prompt_normalized IS NOT NULL
        """)
        cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS crowley_attempt_dup_original_prompt_active_idx
            ON crowley_attempt (original_prompt_normalized)
            WHERE state IN ('queued', 'submitting', 'processing', 'downloading', 'done')
              AND original_prompt_normalized IS NOT NULL
        """)
        _logger.info(
            "Crowley: created partial unique indexes "
            "(crowley_attempt_dup_prompt_active_idx, crowley_attempt_dup_original_prompt_active_idx)"
        )

    _logger.info("Crowley: post-migration to 19.0.1.5.0 complete")
