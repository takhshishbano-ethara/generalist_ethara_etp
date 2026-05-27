"""Unit tests for the Seedance 2.0 token-cost formula in services/cost.py.

The expected values below are hand-computed from the formula
``tokens = (w * h * fps * duration) // 1024`` and
``usd = tokens * 7 / 1_000_000`` (Seedance 2.0 pricing at $7 per 1M tokens),
NOT derived by calling ``cost.estimate`` itself.
"""

from odoo.tests.common import BaseCase, tagged

from odoo.addons.t2av.services import cost


@tagged("post_install", "-at_install", "t2av")
class TestCost(BaseCase):
    """Pure-Python tests for the cost estimator."""

    # ---- estimate() ---------------------------------------------------- #

    def test_720p_5s_16_9(self):
        # (1280 * 720 * 24 * 5) // 1024 = 108_000 tokens
        # 108_000 * 7 / 1_000_000 = 0.756 USD
        tokens, usd = cost.estimate("720p", 5, "16:9")
        self.assertEqual(tokens, 108_000)
        self.assertAlmostEqual(usd, 0.756, places=6)

    def test_1080p_6s_16_9(self):
        # (1920 * 1080 * 24 * 6) // 1024 = 291_600 tokens
        # 291_600 * 7 / 1_000_000 = 2.0412 USD
        tokens, usd = cost.estimate("1080p", 6, "16:9")
        self.assertEqual(tokens, 291_600)
        self.assertAlmostEqual(usd, 2.0412, places=6)

    def test_480p_4s_16_9(self):
        # (854 * 480 * 24 * 4) // 1024 = 38_430 tokens
        # 38_430 * 7 / 1_000_000 = 0.26901 USD
        tokens, usd = cost.estimate("480p", 4, "16:9")
        self.assertEqual(tokens, 38_430)
        self.assertAlmostEqual(usd, 0.26901, places=6)

    # ---- pixels_for() -------------------------------------------------- #

    def test_portrait_9_16_at_720p_5s(self):
        # 720p baseline height = 720 -> portrait short side = 720,
        # long side = round_even(720 * 16/9) = 1280. So (720, 1280),
        # which is the 16:9 landscape (1280, 720) with axes swapped.
        width, height = cost.pixels_for("720p", "9:16")
        self.assertEqual(width, 720)
        self.assertEqual(height, 1280)

        # Sanity: it really is the swap of the landscape baseline.
        land_w, land_h = cost.pixels_for("720p", "16:9")
        self.assertEqual((width, height), (land_h, land_w))

        # Token count for a 5s clip at 24 fps:
        # (720 * 1280 * 24 * 5) // 1024 = 108_000.
        tokens, usd = cost.estimate("720p", 5, "9:16")
        self.assertEqual(tokens, 108_000)
        self.assertAlmostEqual(usd, 0.756, places=6)

    def test_square_1_1_at_720p(self):
        width, height = cost.pixels_for("720p", "1:1")
        self.assertEqual(width, 720)
        self.assertEqual(height, 720)

    # ---- error handling ------------------------------------------------ #

    def test_unknown_resolution_raises_value_error(self):
        with self.assertRaises(ValueError):
            cost.pixels_for("4k", "16:9")
        with self.assertRaises(ValueError):
            cost.estimate("4k", 5, "16:9")

    def test_malformed_aspect_ratio_raises_value_error(self):
        with self.assertRaises(ValueError):
            cost.pixels_for("720p", "169")
        with self.assertRaises(ValueError):
            cost.pixels_for("720p", "16:9:1")
        with self.assertRaises(ValueError):
            cost.pixels_for("720p", "abc:def")
        with self.assertRaises(ValueError):
            cost.pixels_for("720p", "0:9")
