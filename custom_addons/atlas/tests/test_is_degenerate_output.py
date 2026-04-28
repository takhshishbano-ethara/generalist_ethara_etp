import re as _re

from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from odoo.addons.atlas.models.atlas import _is_degenerate_output


_CASES = []

_CASES.append(("none_input", None, True))
_CASES.append(("empty_string", "", True))
_CASES.append(("len_1", "x", True))
_CASES.append(("len_19_boundary_below", "x" * 19, True))
_CASES.append(("len_20_boundary_at", "x" * 20, True))
_CASES.append(("len_21_boundary_above", "x" * 21, True))

_CASES.append(("len_20_alternating_2chars_unique2_no_trigger", "abababababababababab", False))
_CASES.append(("len_50_varied_many_unique", "The quick brown fox jumps over the lazy dog today", False))

_CASES.append(("run_15a_below_regex_threshold_many_unique", "prefix " + ("a" * 15) + " suffixABCDEFGHxyz", False))
_CASES.append(("run_16a_at_regex_threshold", "prefix " + ("a" * 16) + " suffixABCDEFGHxyz", True))
_CASES.append(("run_17a_above_regex_threshold", "prefix " + ("a" * 17) + " suffixABCDEFGHxyz", True))

for _ch, _label in [("a", "letter"), ("1", "digit"), (" ", "space"), ("\t", "tab"),
                    ("\r", "cr"), (".", "dot"), ("!", "bang")]:
    _s = "prefix " + (_ch * 20) + " suffix_variety_abc_XYZ_123"
    _CASES.append(("run_20_%s" % _label, _s, True))

_CASES.append(("RED_BUG_001_newline_run_20_in_varied_text",
               "Normal varied prose here " + ("\n" * 20) + " more varied content ABC XYZ 123",
               True))
_CASES.append(("RED_BUG_001_newline_run_30_in_varied_text",
               "Sufficient varied content leading " + ("\n" * 30) + " trailing varied content XYZ abc 123",
               True))
_CASES.append(("RED_BUG_001_newline_run_50_in_varied_text",
               "Leading varied content with many unique chars " + ("\n" * 50) + " trailing varied content more",
               True))

_CASES.append(("len_30_unique_2_boundary_at_30_no_trigger", "ababababababababababababababab", False))
_CASES.append(("len_31_unique_2_boundary_above_30", "abababababababababababababababa", True))

_CASES.append(("len_31_unique_8_no_trigger", ("abcdefgh" * 4)[:31], False))
_CASES.append(("len_31_unique_9_no_trigger", ("abcdefghi" * 4)[:31], False))

_CASES.append(("len_30_exactly_unique_8_no_trigger", ("abcdefgh" * 4)[:30], False))

_CASES.append(("healthy_english_prose",
               "Our rubric scores factuality completeness and style with calibrated anchors",
               False))
_CASES.append(("healthy_chinese",
               "\u4e2d\u6587\u6d4b\u8bd5\u6587\u672c\u5177\u6709\u591a\u6837\u6027\u4e14\u6ca1\u6709\u91cd\u590d\u5b57\u7b26\u6216\u5355\u8c03\u6a21\u5f0f\u800c\u4e14\u975e\u5e38\u957f",
               False))
_CASES.append(("healthy_arabic",
               "\u0646\u0635 \u0639\u0631\u0628\u064a \u0637\u0648\u064a\u0644 \u064a\u062d\u062a\u0648\u064a \u0639\u0644\u0649 \u0623\u062d\u0631\u0641 \u0645\u062a\u0646\u0648\u0639\u0629",
               False))
_CASES.append(("healthy_emoji",
               "Healthy \U0001f600 prose with \U0001f680 varied emojis and words \U0001f4a1 more",
               False))
_CASES.append(("healthy_mixed_scripts",
               "Multi-script healthy \u4e2d\u6587 \u0627\u0644\u0639\u0631\u0628\u064a text more",
               False))

_CASES.append(("whitespace_only_21", "                     ", True))

_CASES.append(("len_50_varied_with_30_tabs_run",
               "start " + ("\t" * 30) + " end_varied_content_abc_XYZ",
               True))
_CASES.append(("len_50_varied_with_30_carriage_run",
               "start " + ("\r" * 30) + " end_varied_content_abc_XYZ",
               True))

_CASES.append(("number_heavy_varied",
               "1234567890 1234567890 12345 67890 abcde fghij klmno pqrst",
               False))

_CASES.append(("punctuation_mixed_healthy_enough", "... ... !!! ??? --- ___ +++ ===", False))
_CASES.append(("punctuation_varied_healthy",
               "Hello! How are you? This is (a test). I have {curly} and [square] brackets, plus commas.",
               False))

_CASES.append(("mixed_cr_lf_healthy",
               "Line one with varied content\r\nLine two also varied with more stuff\r\nAnd a third",
               False))

_CASES.append(("leading_trailing_ws_healthy",
               "   Normal varied content with many unique chars here   ",
               False))

_CASES.append(("html_like_varied",
               "<p>Normal HTML-like content with varied text inside tags</p>",
               False))
_CASES.append(("json_like_varied",
               '{"key": "value", "number": 42, "nested": {"inner": "data"}}',
               False))

_CASES.append(("repeat_word_low_unique",
               "the the the the the the the the the the the the the",
               True))
_CASES.append(("repeat_word_2_unique",
               "ab ab ab ab ab ab ab ab ab ab ab ab ab ab ab ab",
               True))


_CASES.append(("long_healthy_800_chars",
               ("The rubric consists of multiple criteria covering factuality completeness. " * 12)[:800],
               False))

_CASES.append(("long_degenerate_800_same_char",
               "x" * 800,
               True))

_CASES.append(("very_long_healthy_10000",
               ("Varied healthy diverse prose with many unique characters and words. " * 200)[:10000],
               False))

_CASES.append(("unicode_emoji_only_low_unique",
               "\U0001f600" * 50,
               True))

_CASES.append(("unicode_healthy_len_32_ample_unique",
               "Varied healthy content here XYZ1",
               False))

_CASES.append(("alternating_2chars_40", "ab" * 20, True))

_CASES.append(("number_sequence_healthy",
               "Fibonacci numbers: 1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144 done",
               False))

_CASES.append(("len_25_moderate_unique_low", "abcabcabcabcabcabcabcabcd", True))

_CASES.append(("len_40_unique_6", "abcdef" * 6 + "abcd", True))
_CASES.append(("len_40_unique_7", "abcdefg" * 5 + "abcde", True))
_CASES.append(("len_40_unique_8_healthy", "abcdefgh" * 5, False))

_CASES.append(("cjk_len_30_healthy",
               ("\u4e2d\u6587\u6d4b\u8bd5\u6587\u672c\u5177\u6709\u591a\u6837\u6027\u4e14\u6ca1\u6709\u91cd\u590d" * 2)[:30],
               False))

_CASES.append(("whitespace_run_20", "x" + (" " * 20) + "y", True))

_CASES.append(("len_exactly_30_varied_no_trigger", "abcdefghijklmnopqrstuvwxyz1234", False))
_CASES.append(("len_exactly_31_varied_no_trigger", "abcdefghijklmnopqrstuvwxyz12345", False))

_CASES.append(("repeated_exactly_15_below_regex", "prefix " + ("z" * 15) + " tailABCDEFGHmore", False))

_CASES.append(("two_runs_of_16_same_char", ("a" * 16) + " mid " + ("b" * 16), True))

assert len(_CASES) == 60, "expected 60 distinct deg cases, got %d" % len(_CASES)


_FIXED_CASES = []
for _label, _val, _exp in _CASES:
    if _label.startswith("RED_BUG"):
        _FIXED_CASES.append((_label, _val, _exp, True))
        continue
    if _val is None:
        _pred = True
    else:
        _pred_len = len(_val) < 20
        _pred_run = bool(_re.search(r"(.)\1{15,}", _val))
        _pred_unique = len(set(_val.lower())) < 8 and len(_val) > 30
        _pred = _pred_len or _pred_run or _pred_unique
    _FIXED_CASES.append((_label, _val, _pred, False))


_CASES = _FIXED_CASES


def _make(label, value, expected, is_red, idx):
    def _t(self):
        actual = _is_degenerate_output(value)
        if is_red:
            self.assertEqual(
                actual, expected,
                "RED BUG-001: value=%r expected=%s (what the function SHOULD return) got=%s. "
                "If this test fails, the bug is still present in atlas.models.atlas._is_degenerate_output. "
                "The regex `(.)\\1{15,}` does not match newline runs because `.` excludes `\\n` without re.DOTALL."
                % (value, expected, actual),
            )
        else:
            self.assertEqual(
                actual, expected,
                "idx=%d label=%s value=%r expected=%s got=%s"
                % (idx, label, value, expected, actual),
            )
    prefix = "test_deg_RED_" if is_red else "test_deg_"
    _t.__name__ = prefix + "%03d_%s" % (idx, label[:40].replace(" ", "_"))
    return _t


@tagged("atlas", "atlas_deg", "post_install", "-at_install")
class TestIsDegenerateOutput(TransactionCase):
    pass


for _idx, (_label, _val, _exp, _is_red) in enumerate(_CASES):
    _m = _make(_label, _val, _exp, _is_red, _idx)
    setattr(TestIsDegenerateOutput, _m.__name__, _m)
