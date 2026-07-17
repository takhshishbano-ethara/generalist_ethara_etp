# -*- coding: utf-8 -*-
"""Readable-tag rework + drift collapse (v19.0.1.118.0).

Two data-driven steps, no hardcoded synonym dictionary:

1. Backfill the new ``display`` column on every tag from its label
   (Title-Case of the kebab value) so nothing is blank pre-first-extraction.
2. Collapse near-duplicate keys WITHIN each facet by string similarity, electing
   the HIGHEST-USAGE key as canonical and repointing the m2m rows off the losers,
   then archiving the emptied tags. This is the ranking fix: similar SOPs stop
   splintering across synonym keys and start sharing real tag rows again.

The clustering is computed at migration time from live data (difflib ratio over
the facet's own values) — it is NOT a maintained lookup map. Going forward the
frequency-ranked vocabulary feedback keeps runs converging on the survivors.
"""
import logging
from difflib import SequenceMatcher

_logger = logging.getLogger(__name__)

SIM_THRESHOLD = 0.72  # values this similar within a facet are treated as one idea


def _title(value):
    return value.replace("-", " ").title() if value else value


def migrate(cr, version):
    # 1) create + backfill display -------------------------------------------
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='etp_assessment_pro_tag' AND column_name='display'
    """)
    if not cr.fetchone():
        cr.execute("ALTER TABLE etp_assessment_pro_tag ADD COLUMN display varchar")
    cr.execute("""
        UPDATE etp_assessment_pro_tag
           SET display = initcap(replace(
                 CASE WHEN position(':' in name) > 0
                      THEN split_part(name, ':', 2) ELSE name END, '-', ' '))
         WHERE display IS NULL OR display = ''
    """)

    # 2) collapse drifted vocabulary -----------------------------------------
    # relation table for prompt<->tag m2m
    rel = "etp_assessment_pro_prompt_etp_assessment_pro_tag_rel"
    col_prompt = "etp_assessment_pro_prompt_id"
    col_tag = "etp_assessment_pro_tag_id"
    cr.execute("""
        SELECT to_regclass(%s)
    """, (rel,))
    if not cr.fetchone()[0]:
        _logger.info("etp tag migration: m2m rel %s absent; skipping collapse", rel)
        return

    # usage count per tag
    cr.execute(
        "SELECT {t} AS tid, COUNT(DISTINCT {p}) AS c FROM {rel} GROUP BY {t}"
        .format(t=col_tag, p=col_prompt, rel=rel))
    usage = {row[0]: row[1] for row in cr.fetchall()}

    # all faceted tags -> group by facet
    cr.execute("SELECT id, name FROM etp_assessment_pro_tag WHERE name LIKE '%:%'")
    by_facet = {}
    for tid, name in cr.fetchall():
        facet, value = name.split(":", 1)
        by_facet.setdefault(facet, []).append((tid, value))

    merges = 0
    archived = 0
    for facet, items in by_facet.items():
        # greedy clustering by string similarity within the facet
        clusters = []
        for tid, value in items:
            placed = False
            for cluster in clusters:
                _rep_id, rep_val, _members = cluster
                if SequenceMatcher(None, value, rep_val).ratio() >= SIM_THRESHOLD:
                    cluster[2].append((tid, value))
                    placed = True
                    break
            if not placed:
                clusters.append([tid, value, [(tid, value)]])

        for _rep_id, _rep_val, members in clusters:
            if len(members) < 2:
                continue
            # canonical = highest usage, tie-break shortest value then id
            canonical = max(
                members,
                key=lambda m: (usage.get(m[0], 0), -len(m[1]), -m[0]))
            canon_id = canonical[0]
            losers = [m for m in members if m[0] != canon_id]
            if not losers:
                continue
            loser_ids = tuple(m[0] for m in losers)
            # repoint m2m rows: insert canonical link where a loser link exists
            cr.execute(
                "INSERT INTO {rel} ({p}, {t}) "
                "SELECT DISTINCT r.{p}, %s FROM {rel} r "
                "WHERE r.{t} IN %s "
                "  AND NOT EXISTS (SELECT 1 FROM {rel} x "
                "     WHERE x.{p}=r.{p} AND x.{t}=%s)".format(
                    rel=rel, p=col_prompt, t=col_tag),
                (canon_id, loser_ids, canon_id))
            # drop the loser links, then archive the loser tags
            cr.execute(
                "DELETE FROM {rel} WHERE {t} IN %s".format(rel=rel, t=col_tag),
                (loser_ids,))
            # archive (soft) if the model has active; else delete rows
            cr.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='etp_assessment_pro_tag' AND column_name='active'
            """)
            if cr.fetchone():
                cr.execute(
                    "UPDATE etp_assessment_pro_tag SET active=false WHERE id IN %s",
                    (loser_ids,))
            else:
                cr.execute(
                    "DELETE FROM etp_assessment_pro_tag WHERE id IN %s",
                    (loser_ids,))
            merges += 1
            archived += len(loser_ids)
            _logger.info(
                "etp tag collapse [%s]: kept %r (id=%s), merged %s -> archived %d",
                facet, canonical[1], canon_id,
                [m[1] for m in losers], len(loser_ids))

    _logger.info(
        "etp tag migration done: %d clusters merged, %d duplicate tags archived",
        merges, archived)
