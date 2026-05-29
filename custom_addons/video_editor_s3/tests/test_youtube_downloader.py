# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged

from ..services.youtube_downloader import parse_youtube_url


@tagged("post_install", "-at_install", "video_editor_s3")
class TestParseYoutubeUrl(TransactionCase):

    def test_watch_url(self):
        vid, norm = parse_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")
        self.assertEqual(norm, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_watch_url_no_www(self):
        vid, _ = parse_youtube_url("https://youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_short_url(self):
        vid, _ = parse_youtube_url("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_shorts_url(self):
        vid, _ = parse_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_embed_url(self):
        vid, _ = parse_youtube_url("https://www.youtube.com/embed/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_v_url(self):
        vid, _ = parse_youtube_url("https://www.youtube.com/v/dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_music_url(self):
        vid, _ = parse_youtube_url("https://music.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_mobile_url(self):
        vid, _ = parse_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(vid, "dQw4w9WgXcQ")

    def test_empty_returns_none(self):
        self.assertEqual(parse_youtube_url(""), (None, None))

    def test_none_returns_none(self):
        self.assertEqual(parse_youtube_url(None), (None, None))

    def test_non_string_returns_none(self):
        self.assertEqual(parse_youtube_url(12345), (None, None))

    def test_wrong_host_returns_none(self):
        self.assertEqual(parse_youtube_url("https://vimeo.com/123456"), (None, None))

    def test_invalid_id_too_short(self):
        self.assertEqual(parse_youtube_url("https://www.youtube.com/watch?v=short"), (None, None))

    def test_invalid_id_bad_chars(self):
        self.assertEqual(parse_youtube_url("https://www.youtube.com/watch?v=invalid!@#$"), (None, None))

    def test_watch_without_v_param(self):
        self.assertEqual(parse_youtube_url("https://www.youtube.com/watch"), (None, None))

    def test_garbage_string(self):
        self.assertEqual(parse_youtube_url("not a url at all"), (None, None))
