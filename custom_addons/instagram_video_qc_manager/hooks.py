# -*- coding: utf-8 -*-
"""Module install / upgrade hooks.

``post_init_hook`` walks every existing video.task.version row and
migrates the bytes of any legacy ``ir.attachment``-backed render
(``edited_attachment_*_id`` / ``preview_attachment_id``) to the new
on-disk layout at ``<media_root>/<task.id>/v<n>_<kind>[_slot<N>].mp4``,
then populates the corresponding ``*_file_*_path`` Char column.

The migration is **idempotent** — rows whose path field is already
populated are skipped.  It is also **non-destructive**: the
underlying ``ir.attachment`` rows are NOT deleted, so the legacy
fallback in :meth:`controllers.main.VideoQCController._stream` keeps
working for any rows we missed (or any that fail to dump).  An
operator can purge the attachments by hand once they've verified
the move was clean.

Triggered automatically by ``-i instagram_video_qc_manager`` or
``-u instagram_video_qc_manager`` because of the ``post_init_hook``
entry in ``__manifest__.py``.
"""

import base64
import logging
import os

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Migrate legacy ir.attachment-backed renders to on-disk paths."""
    storage = env["video.qc.media.storage"].sudo()
    Version = env["video.task.version"].sudo()
    versions = Version.search([])
    if not versions:
        _logger.info("video_qc migration: no versions to migrate.")
        return
    _logger.info(
        "video_qc migration: scanning %s versions for legacy renders...",
        len(versions),
    )

    moved = 0
    skipped = 0
    failures = 0

    # ((attachment-field, path-field, kind, slot), ...)
    SCHEMA = (
        ("edited_attachment_1_id", "edited_file_1_path", "edited", 1),
        ("edited_attachment_2_id", "edited_file_2_path", "edited", 2),
        ("edited_attachment_id",   "edited_file_path",   "edited", None),
        ("preview_attachment_id",  "preview_file_path",  "preview", None),
    )

    for idx, version in enumerate(versions, start=1):
        for att_field, path_field, kind, slot in SCHEMA:
            existing_path = version[path_field]
            if existing_path:
                # Already migrated.
                skipped += 1
                continue
            attachment = version[att_field]
            if not attachment:
                continue
            try:
                target = storage.path_for(version, kind, slot=slot)
                if os.path.isfile(target):
                    # File already exists on disk (e.g. partial prior
                    # migration).  Just populate the path field.
                    version.write({path_field: storage.relative(target)})
                    moved += 1
                    continue
                raw = attachment.raw or (
                    base64.b64decode(attachment.datas) if attachment.datas else b""
                )
                if not raw:
                    _logger.warning(
                        "Empty attachment id=%s on version id=%s — skipping.",
                        attachment.id, version.id,
                    )
                    failures += 1
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(raw)
                version.write({path_field: storage.relative(target)})
                moved += 1
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "Migration failed for version id=%s field=%s",
                    version.id, att_field,
                )
                failures += 1

        if idx % 100 == 0:
            _logger.info(
                "video_qc migration: %s/%s versions processed "
                "(moved=%s skipped=%s failures=%s)",
                idx, len(versions), moved, skipped, failures,
            )

    _logger.info(
        "video_qc migration: done. moved=%s skipped=%s failures=%s",
        moved, skipped, failures,
    )
