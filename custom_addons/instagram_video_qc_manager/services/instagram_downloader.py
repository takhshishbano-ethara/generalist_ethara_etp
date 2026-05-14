import base64
import logging
import mimetypes
import os
import re
import shutil
import tempfile
from contextlib import contextmanager

import requests
from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    import instaloader
except ImportError:
    instaloader = None


_SHORTCODE_RE = re.compile(r"(?:instagram\.com/)?(?:reel|p)/([A-Za-z0-9_-]+)")


@contextmanager
def _tempdir():
    path = tempfile.mkdtemp(prefix="ig_dl_")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class InstagramDownloader(models.AbstractModel):
    _name = "instagram.downloader"
    _description = "Instagram Video Downloader Service"

    @api.model
    def download_to_attachment(self, task, url, slot=1):
        if not url:
            raise UserError(_("Empty URL"))
        if instaloader is None:
            raise UserError(_("Python package 'instaloader' is not installed."))

        shortcode = self._extract_shortcode(url)
        if not shortcode:
            raise UserError(_("Could not extract a valid Instagram shortcode from: %s") % url)

        with _tempdir() as workdir:
            video_path, thumb_bytes = self._download_media(shortcode, workdir)
            attachment = self._store_attachment(task, slot, video_path)
            return attachment, thumb_bytes

    def _extract_shortcode(self, url):
        m = _SHORTCODE_RE.search(url)
        if m:
            return m.group(1)
        return False

    def _download_media(self, shortcode, workdir):
        L = instaloader.Instaloader()
        _logger.info("Fetching Instagram post shortcode=%s", shortcode)

        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
        except Exception as exc:
            raise UserError(_("Failed to fetch Instagram post %s: %s") % (shortcode, exc)) from exc

        if not post.is_video:
            raise UserError(_("The Instagram post %s is not a video reel.") % shortcode)

        video_url = post.video_url
        video_ext = "mp4"
        video_path = os.path.join(workdir, f"{shortcode}.{video_ext}")

        _logger.info("Downloading video from %s", video_url)
        try:
            resp = requests.get(video_url, stream=True, timeout=600)
            resp.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        except requests.RequestException as exc:
            raise UserError(_("Failed to download video for %s: %s") % (shortcode, exc)) from exc

        if not os.path.getsize(video_path):
            raise UserError(_("Downloaded video for %s is empty.") % shortcode)

        thumb_bytes = False
        thumb_url = getattr(post, "url", None)
        if thumb_url:
            try:
                thumb_resp = requests.get(thumb_url, timeout=30)
                if thumb_resp.ok:
                    thumb_bytes = base64.b64encode(thumb_resp.content)
            except requests.RequestException:
                _logger.warning("Could not fetch thumbnail for %s", shortcode)

        return video_path, thumb_bytes

    def _store_attachment(self, task, slot, path):
        with open(path, "rb") as fh:
            data = fh.read()
        mime, _enc = mimetypes.guess_type(path)
        attachment = self.env["ir.attachment"].sudo().create(
            {
                "name": f"{task.name}_original_{slot}_{os.path.basename(path)}",
                "datas": base64.b64encode(data),
                "res_model": task._name,
                "res_id": task.id,
                "mimetype": mime or "video/mp4",
                "video_task_id": task.id,
                "video_asset_kind": "original",
            }
        )
        return attachment

