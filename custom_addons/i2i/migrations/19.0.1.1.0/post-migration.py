import logging

_logger = logging.getLogger(__name__)


_REMAP = {
    "edit_only_instructed": ("instruction_aligned", "no"),
    "images_aligned": ("images_aligned", "no"),
    "free_of_ai_slop": ("slop_free", "no"),
}


def _remap_column(cr, column, positive, fallback):
    cr.execute(
        f"""
        UPDATE i2i_item
        SET {column} = CASE
            WHEN {column} = 'yes' THEN %s
            WHEN {column} = 'partial' THEN %s
            ELSE {column}
        END
        WHERE {column} IN ('yes', 'partial')
        """,
        (positive, fallback),
    )
    return cr.rowcount


def migrate(cr, version):
    if not version:
        return
    total = 0
    for field, (positive, fallback) in _REMAP.items():
        total += _remap_column(cr, field, positive, fallback)
        llm_field = f"llm_{field}"
        total += _remap_column(cr, llm_field, positive, fallback)
    if total:
        _logger.info("i2i 19.0.1.1.0 migration: remapped %s rating values", total)
