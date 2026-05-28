# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    storage = env["video.editor.s3.media.storage"].sudo()
    root = storage.get_media_root()
    _logger.info("video_editor_s3: media root resolved to %s", root)
