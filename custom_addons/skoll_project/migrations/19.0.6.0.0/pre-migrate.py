"""Pre-migration: single-sandbox → multi-sandbox per task.

For each skoll_skoll row:
  1. INSERT a 'claude' sandbox copying docker_* fields from the task.
  2. INSERT 'glm' and '1p' sandboxes with stopped/not_started defaults.
  3. UPDATE skoll_turn.sandbox_id to point at the claude sandbox.
"""

import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    )
    return cr.fetchone() is not None


def migrate(cr, version):
    if not version:
        return

    # Guard: only run if source columns still exist on skoll_skoll
    if not _column_exists(cr, "skoll_skoll", "docker_compose_project"):
        _logger.info(
            "pre-migrate 19.0.6.0.0: docker_compose_project column absent "
            "— migration already applied or not needed."
        )
        return

    # Guard: skoll_sandbox table must already exist (created by ORM before
    # pre-migrate of the *new* version — Odoo creates tables before running
    # pre-migrate).  If it doesn't exist yet we can't proceed.
    cr.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'skoll_sandbox'
        LIMIT 1
        """
    )
    if not cr.fetchone():
        _logger.warning(
            "pre-migrate 19.0.6.0.0: skoll_sandbox table does not exist yet "
            "— skipping (will be handled by post-migrate or manual step)."
        )
        return

    _logger.info(
        "pre-migrate 19.0.6.0.0: creating sandbox rows from skoll_skoll docker fields"
    )

    # ── 1. Insert 'claude' sandbox per task ─────────────────────────────
    cr.execute(
        """
        INSERT INTO skoll_sandbox (
            skoll_id,
            model_type,
            docker_compose_project,
            docker_status,
            docker_port,
            docker_litellm_port,
            docker_gateway_token,
            docker_error,
            docker_workdir,
            session_status,
            create_uid,
            write_uid,
            create_date,
            write_date
        )
        SELECT
            t.id,
            'claude',
            t.docker_compose_project,
            t.docker_status,
            t.docker_port,
            t.docker_litellm_port,
            t.docker_gateway_token,
            t.docker_error,
            t.docker_workdir,
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM skoll_turn turn WHERE turn.skoll_id = t.id
                ) THEN 'in_progress'
                ELSE 'not_started'
            END,
            t.create_uid,
            t.write_uid,
            NOW() AT TIME ZONE 'UTC',
            NOW() AT TIME ZONE 'UTC'
        FROM skoll_skoll t
        WHERE NOT EXISTS (
            SELECT 1 FROM skoll_sandbox s
            WHERE s.skoll_id = t.id AND s.model_type = 'claude'
        )
        """
    )
    claude_count = cr.rowcount
    _logger.info(
        "pre-migrate 19.0.6.0.0: inserted %d claude sandbox rows", claude_count
    )

    # ── 2. Insert 'glm' and '1p' sandboxes (stopped / not_started) ─────
    for model_type in ("glm", "1p"):
        cr.execute(
            """
            INSERT INTO skoll_sandbox (
                skoll_id,
                model_type,
                docker_status,
                session_status,
                create_uid,
                write_uid,
                create_date,
                write_date
            )
            SELECT
                t.id,
                %s,
                'stopped',
                'not_started',
                t.create_uid,
                t.write_uid,
                NOW() AT TIME ZONE 'UTC',
                NOW() AT TIME ZONE 'UTC'
            FROM skoll_skoll t
            WHERE NOT EXISTS (
                SELECT 1 FROM skoll_sandbox s
                WHERE s.skoll_id = t.id AND s.model_type = %s
            )
            """,
            (model_type, model_type),
        )
        _logger.info(
            "pre-migrate 19.0.6.0.0: inserted %d %s sandbox rows",
            cr.rowcount,
            model_type,
        )

    # ── 3. Link existing turns to their claude sandbox ──────────────────
    if _column_exists(cr, "skoll_turn", "sandbox_id"):
        cr.execute(
            """
            UPDATE skoll_turn
            SET sandbox_id = (
                SELECT s.id
                FROM skoll_sandbox s
                WHERE s.skoll_id = skoll_turn.skoll_id
                  AND s.model_type = 'claude'
                LIMIT 1
            )
            WHERE sandbox_id IS NULL
            """
        )
        _logger.info(
            "pre-migrate 19.0.6.0.0: linked %d turns to claude sandbox",
            cr.rowcount,
        )
    else:
        _logger.info(
            "pre-migrate 19.0.6.0.0: sandbox_id column not yet on skoll_turn "
            "— turn linking will be handled after column creation."
        )
