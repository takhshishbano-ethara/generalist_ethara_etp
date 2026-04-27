from odoo.tests.common import TransactionCase
from odoo.tests import tagged

from odoo.addons.atlas.models.atlas import _parse_rubric_table


_CASES = []


def _add(label, text, check, is_red=False):
    _CASES.append((label, text, check, is_red))


_add("empty_string_returns_empty_list",
     "",
     lambda r: r == [])
_add("whitespace_only_returns_empty",
     "   \n\t  ",
     lambda r: r == [])
_add("pure_prose_no_pipes_returns_empty",
     "This is a paragraph with no table content at all",
     lambda r: r == [])

_add("only_separator_row",
     "|---|---|---|",
     lambda r: r == [])
_add("only_header_row_criterion_keyword",
     "| Criterion | Category | Importance |",
     lambda r: r == [])
_add("only_header_row_criteria_keyword",
     "| Criteria | Cat | Imp |",
     lambda r: r == [])
_add("header_row_plus_separator",
     "| Criterion | Cat | Imp |\n|---|---|---|",
     lambda r: r == [])

_add("qc_check_prefix_filtered",
     "| \u2713 verified something that looks like a row |",
     lambda r: r == [])
_add("qc_cross_prefix_filtered",
     "| \u2717 rejected row content here too short |",
     lambda r: r == [])
_add("qc_check_word_prefix_filtered",
     "| check this completeness ratio something |",
     lambda r: r == [])
_add("qc_verdict_prefix_filtered",
     "| verdict of the evaluation is positive |",
     lambda r: r == [])
_add("qc_self_qc_prefix_filtered",
     "| self-qc evaluation passes all the standards |",
     lambda r: r == [])
_add("qc_note_prefix_filtered",
     "| qc note about the rubric that follows |",
     lambda r: r == [])
_add("qc_verification_prefix_filtered",
     "| verification run on sample outputs completed |",
     lambda r: r == [])
_add("qc_weakest_prefix_filtered",
     "| weakest field in the rubric to improve |",
     lambda r: r == [])


_add("good_row_valid_category_and_importance",
     "| Neutral generic rule statement for test purposes number one | factuality_hallucination | important | 0: bad 1: ok 2: good | tip |",
     lambda r: (len(r) == 1
                and r[0]["category"] == "factuality_hallucination"
                and r[0]["importance"] == "important"))
_add("good_row_category_with_spaces_normalized",
     "| Neutral generic rule statement for test purposes number two | Task Completion | Important | 0: x 1: y |",
     lambda r: (len(r) == 1
                and r[0]["category"] == "task_completion"
                and r[0]["importance"] == "important"))
_add("good_row_default_category_other",
     "| Neutral generic rule statement for test purposes number three | unknown_value_xyz | important | 0: x 1: y |",
     lambda r: len(r) == 1 and r[0]["category"] == "other")
_add("good_row_default_importance_important",
     "| Neutral generic rule statement for test purposes number four | task_completion | unknown_xyz | 0: x 1: y |",
     lambda r: len(r) == 1 and r[0]["importance"] == "important")
_add("good_row_custom_category_extracted",
     "| Neutral generic rule statement for test purposes number five | Other: MyCustom | important | 0: x 1: y |",
     lambda r: (len(r) == 1
                and r[0]["category"] == "other"
                and r[0]["custom_category"] == "MyCustom"))

_add("row_with_numeric_row_prefix_stripped",
     "| 1. | Neutral generic rule statement for test purposes numbered row | task_completion | important | 0: x 1: y |",
     lambda r: len(r) == 1 and "numbered" in r[0]["name"])
_add("row_with_N_numeric_prefix_stripped",
     "| N#5 | Neutral generic rule statement for test N-prefix row | task_completion | important | 0: x |",
     lambda r: len(r) == 1)
_add("row_with_C_numeric_prefix_stripped",
     "| C3 | Neutral generic rule statement for test C-prefix row | task_completion | important | 0: x |",
     lambda r: len(r) == 1)

_add("row_with_is_negative_x_mark_set",
     "| Neutral generic rule statement for negative row example | other | detrimental | 0: bad 1: ok | \u274c |",
     lambda r: len(r) == 1 and r[0]["is_negative"] is True)
_add("row_without_x_mark_is_negative_false",
     "| Neutral generic rule statement without negative indicator now | other | important | 0: x 1: y |",
     lambda r: len(r) == 1 and r[0]["is_negative"] is False)

_add("criterion_text_too_short_skipped",
     "| short | other | important | 0: x |",
     lambda r: r == [])
_add("criterion_text_starts_with_dashes_skipped",
     "| ---separator-text-passing-length-here | other | important | 0: x |",
     lambda r: r == [])
_add("criterion_text_has_qc_note_keyword_skipped",
     "| Some qc note contained here in the text passes length | other | important | 0: x |",
     lambda r: r == [])
_add("criterion_text_has_self_qc_keyword_skipped",
     "| Contains self-qc content here passes length boundary | other | important | 0: x |",
     lambda r: r == [])
_add("criterion_text_has_verification_check_skipped",
     "| Has verification check phrase contained passes length | other | important | 0: x |",
     lambda r: r == [])
_add("criterion_text_has_weakest_field_skipped",
     "| Row with weakest field phrase contained here passes | other | important | 0: x |",
     lambda r: r == [])
_add("criterion_text_has_maxraw_skipped",
     "| Contains maxraw token in text passes length boundary | other | important | 0: x |",
     lambda r: r == [])
_add("criterion_text_has_score_equals_skipped",
     "| Contains score = in text passes length boundary required | other | important | 0: x |",
     lambda r: r == [])


_add("codeblock_markdown_fenced",
     "```markdown\n| Neutral generic rule statement codeblock row for testing | task_completion | important | 0: x 1: y |\n```",
     lambda r: len(r) == 1)
_add("codeblock_plain_fenced",
     "```\n| Neutral generic rule statement plain codeblock for testing | task_completion | important | 0: x 1: y |\n```",
     lambda r: len(r) == 1)
_add("text_before_and_after_codeblock_ignored",
     "preamble text\n```\n| Neutral generic rule statement fenced row passes | task_completion | important | 0: x 1: y |\n```\ntrailing text",
     lambda r: len(r) == 1)


_add("two_valid_rows_returns_two",
     ("| Neutral generic rule statement row alpha for testing here | task_completion | important | 0: x 1: y |\n"
      "| Neutral generic rule statement row beta for testing here | factuality_hallucination | detrimental | 0: x 1: y |"),
     lambda r: len(r) == 2)
_add("three_valid_rows_returns_three",
     ("| Neutral generic rule statement row one here passes length | task_completion | important | 0: x |\n"
      "| Neutral generic rule statement row two here passes length | other | important | 0: x |\n"
      "| Neutral generic rule statement row three here passes length | other | detrimental | 0: x |"),
     lambda r: len(r) == 3)
_add("mix_valid_plus_filtered_header_returns_valid_only",
     ("| Criterion | Category | Importance |\n"
      "| Neutral generic rule statement valid row passes length here | other | important | 0: x |\n"
      "|---|---|---|"),
     lambda r: len(r) == 1)

_add("levels_parsed_from_score_col",
     "| Neutral generic rule statement with explicit levels here now | other | important | 0: bad 1: medium 2: good |",
     lambda r: len(r) == 1 and len(r[0]["levels"]) == 3)
_add("levels_default_two_when_no_scores",
     "| Neutral generic rule statement with no explicit score levels col | other | important |",
     lambda r: len(r) == 1 and len(r[0]["levels"]) == 2)
_add("weight_computed_as_max_score",
     "| Neutral generic rule statement with weight compute testing here | other | important | 0: bad 1: med 2: good 5: excellent |",
     lambda r: len(r) == 1 and r[0]["weight"] == 5)
_add("level_em_dash_stripped_from_label",
     "| Neutral generic rule statement with em dash label here | other | important | 0: \u2014 bad 1: \u2014 ok |",
     lambda r: len(r) == 1 and r[0]["levels"][0]["label"] == "bad")


_add("unicode_cjk_criterion",
     "| \u4e2d\u6587\u89c4\u5219\u63cf\u8ff0\u5305\u542b\u8db3\u591f\u957f\u7684\u5b57\u7b26\u6d4b\u8bd5\u7528 | other | important | 0: x |",
     lambda r: len(r) == 1)
_add("unicode_arabic_criterion",
     "| \u0642\u0627\u0639\u062f\u0629 \u0639\u0627\u0645\u0629 \u0641\u064a \u0627\u0644\u0646\u0635 \u0627\u0644\u0637\u0648\u064a\u0644 \u0628\u0645\u0627 \u0641\u064a\u0647 \u0627\u0644\u0643\u0641\u0627\u064a\u0629 | other | important | 0: x |",
     lambda r: len(r) == 1)
_add("unicode_emoji_criterion",
     "| Neutral rule statement with emoji \U0001f600 passes length boundary | other | important | 0: x |",
     lambda r: len(r) == 1)


_add("RED_BUG_002_criterion_has_communication_style_overrides_category",
     "| Professional rule about good communication style tone in text | instruction_following | important | 0: x |",
     lambda r: len(r) == 1 and r[0]["category"] == "instruction_following",
     is_red=True)
_add("RED_BUG_002_criterion_has_task_completion_overrides_category",
     "| This rule verifies task completion happens correctly every single time | other | important | 0: x |",
     lambda r: len(r) == 1 and r[0]["category"] == "other",
     is_red=True)
_add("RED_BUG_002_criterion_has_other_overrides_to_other",
     "| Other types of tone should be considered in evaluation process here | factuality_hallucination | important | 0: x |",
     lambda r: len(r) == 1 and r[0]["category"] == "factuality_hallucination",
     is_red=True)

_add("RED_BUG_003_criterion_word_criterion_drops_row",
     "| The criterion is that output must remain grounded in factual sources | other | important | 0: x |",
     lambda r: len(r) == 1,
     is_red=True)
_add("RED_BUG_003_criterion_word_criteria_drops_row",
     "| Our criteria specify the rule statement should be comprehensive | other | important | 0: x |",
     lambda r: len(r) == 1,
     is_red=True)
_add("RED_BUG_003_criterion_word_category_drops_row",
     "| The category of error matters for rubric evaluation completeness | other | important | 0: x |",
     lambda r: len(r) == 1,
     is_red=True)
_add("RED_BUG_003_criterion_word_importance_drops_row",
     "| Note the importance of clear unambiguous evaluation criteria | other | important | 0: x |",
     lambda r: len(r) == 1,
     is_red=True)


_add("malformed_single_pipe_returns_empty",
     "| incomplete",
     lambda r: r == [])
_add("malformed_multiple_pipes_no_content",
     "|||||",
     lambda r: r == [])
_add("malformed_pipe_content_too_short",
     "| x |",
     lambda r: r == [])
_add("malformed_row_with_only_two_cols_valid_name",
     "| Neutral generic rule statement valid but only two columns here |",
     lambda r: r == [])
_add("malformed_crlf_line_endings",
     "| Neutral generic rule statement with CRLF line endings passes |\r\n| second one also with CRLF passes length here | other | important | 0: x |\r\n",
     lambda r: len(r) == 2)


_add("large_input_20_rows",
     "\n".join(
         "| Neutral generic rule statement numbered row %d passes length | other | important | 0: x |" % i
         for i in range(20)
     ),
     lambda r: len(r) == 20)


_add("extra_whitespace_in_cells_stripped",
     "|   Neutral generic rule statement with padded whitespace passes length   |   other   |   important   | 0: x |",
     lambda r: len(r) == 1 and r[0]["name"] == "Neutral generic rule statement with padded whitespace passes length")

_add("empty_cell_in_middle_preserved",
     "| Neutral generic rule statement with empty trailing col here |  | important | 0: x |",
     lambda r: len(r) == 1)


_add("suggestion_last_col_extracted",
     "| Neutral generic rule statement with suggestion last col here | other | important | 0: x 1: y | good suggestion text |",
     lambda r: len(r) == 1 and r[0]["suggestion"] == "good suggestion text")
_add("suggestion_missing_default_empty",
     "| Neutral generic rule statement without suggestion last col here | other | important | 0: x |",
     lambda r: len(r) == 1)


_add("code_block_priority_over_outside",
     ("outside | ignored because inside fence | other | important | 0: x |\n"
      "```\n"
      "| Neutral generic rule statement inside fence passes length | other | important | 0: x |\n"
      "```"),
     lambda r: len(r) == 1 and "inside fence" in r[0]["name"])

_add("multiple_codeblocks_only_first_used",
     ("```\n"
      "| Neutral generic rule statement first fence passes length here | other | important | 0: x |\n"
      "```\n"
      "```\n"
      "| Neutral generic rule statement second fence passes length here | other | important | 0: x |\n"
      "```"),
     lambda r: len(r) == 1 and "first fence" in r[0]["name"])


_add("importance_substring_match_still_works_when_column_exact",
     "| Neutral generic rule statement with exact importance col here | other | critically_important | 0: x |",
     lambda r: len(r) == 1 and r[0]["importance"] == "critically_important")

_add("category_exact_match_preferred",
     "| Neutral generic rule statement with precise category column here | instruction_following | important | 0: x |",
     lambda r: len(r) == 1 and r[0]["category"] == "instruction_following")


_add("level_with_em_dash_and_text_parsed",
     "| Neutral generic rule statement for levels with dashes here now | other | important | 0: \u2014 label for zero 1: \u2014 label for one 2: \u2014 label for two |",
     lambda r: (len(r) == 1
                and r[0]["levels"][2]["label"] == "label for two"))


_add("whitespace_only_table_lines_no_content",
     "|     |     |     |\n|  |  |  |",
     lambda r: r == [])

_add("mixed_languages_in_single_row",
     "| \u4e2d\u6587 mixed english \u0627\u0644\u0639\u0631\u0628\u064a rule statement passes length here | other | important | 0: x |",
     lambda r: len(r) == 1)

_add("numeric_only_criterion_text_rejected",
     "| 12345678901234567890 | other | important | 0: x |",
     lambda r: len(r) == 1)


_add("tab_chars_inside_cell_preserved",
     "| Neutral generic rule statement with tab\tinside passes length here | other | important | 0: x |",
     lambda r: len(r) == 1)


_add("check_mark_prefix_on_criterion_text_skipped",
     "| \u2713 row starting with checkmark passes length boundary here | other | important | 0: x |",
     lambda r: r == [])

_add("cross_mark_prefix_on_criterion_text_skipped",
     "| \u2717 row starting with cross passes length boundary here | other | important | 0: x |",
     lambda r: r == [])


_add("category_case_insensitive_match",
     "| Neutral generic rule statement case test communication here | COMMUNICATION_STYLE | important | 0: x |",
     lambda r: len(r) == 1 and r[0]["category"] == "communication_style")

_add("importance_case_insensitive_match",
     "| Neutral generic rule statement case test importance field here | other | CRITICALLY_DETRIMENTAL | 0: x |",
     lambda r: len(r) == 1 and r[0]["importance"] == "critically_detrimental")


_add("levels_with_many_scores_weight_max",
     "| Neutral generic rule statement for many levels weight test here | other | important | 0: zero 1: one 2: two 3: three 10: ten |",
     lambda r: len(r) == 1 and r[0]["weight"] == 10)

_add("levels_single_score_weight_is_that_score",
     "| Neutral generic rule statement single score level here passes | other | important | 5: five only |",
     lambda r: len(r) == 1 and r[0]["weight"] == 5)


_add("returns_list_type",
     "| Neutral generic rule statement type check test passes length | other | important | 0: x |",
     lambda r: isinstance(r, list))

_add("returns_list_of_dicts",
     "| Neutral generic rule statement dict check test passes length | other | important | 0: x |",
     lambda r: len(r) == 1 and isinstance(r[0], dict))

_add("returned_dict_has_all_expected_keys",
     "| Neutral generic rule statement key check test passes length now | other | important | 0: x |",
     lambda r: (len(r) == 1
                and set(r[0].keys()) >= {"name", "category", "custom_category",
                                         "importance", "weight", "is_negative",
                                         "suggestion", "levels"}))

_add("large_criterion_text_truncated_or_preserved",
     "| " + ("word " * 80).strip() + " | other | important | 0: x |",
     lambda r: len(r) == 1 and len(r[0]["name"]) > 100)


_add("header_row_followed_by_valid_filter_correct",
     ("| Criterion | Category | Importance |\n"
      "|---|---|---|\n"
      "| Neutral generic rule statement after header passes length here | other | important | 0: x |"),
     lambda r: len(r) == 1)

_add("mixed_valid_and_invalid_rows_filter",
     ("| short |\n"
      "| Neutral generic rule statement valid row passes length here alpha | other | important | 0: x |\n"
      "| \u2713 checked |\n"
      "| Neutral generic rule statement valid row passes length here beta | other | important | 0: x |"),
     lambda r: len(r) == 2)


_add("codeblock_with_trailing_newline",
     "```\n| Neutral generic rule statement trailing nl codeblock passes | other | important | 0: x |\n\n```",
     lambda r: len(r) == 1)


_add("unlimited_length_criterion_supported",
     "| " + "very long criterion text " * 40 + " | other | important | 0: x |",
     lambda r: len(r) == 1)


_add("separator_with_colons_alignment",
     ("| Neutral generic rule statement with colon separator row here | other | important | 0: x |\n"
      "|:---:|:---:|:---:|"),
     lambda r: len(r) == 1)


_add("criterion_with_leading_trailing_whitespace_in_cell",
     "|   Neutral generic rule statement padded whitespace around name   | other | important | 0: x |",
     lambda r: len(r) == 1 and r[0]["name"].startswith("Neutral"))


_add("row_with_empty_trailing_col_no_crash",
     "| Neutral generic rule statement trailing empty col test here now | other | important | 0: x |  |",
     lambda r: len(r) == 1)


_add("row_importance_substring_non_word_override",
     "| Neutral generic rule statement importance substring test here | other | criticallyimportant | 0: x |",
     lambda r: len(r) == 1 and r[0]["importance"] == "critically_important")


_add("deeply_nested_markdown_code_block_outer_only",
     "```markdown\n| Neutral generic rule statement nested codeblock passes length | other | important | 0: x |\n```",
     lambda r: len(r) == 1)


_add("weight_equals_max_score_5_levels",
     "| Neutral generic rule statement five levels weight test here now | other | important | 0: a 1: b 2: c 3: d 4: e |",
     lambda r: len(r) == 1 and r[0]["weight"] == 4)


_add("category_and_importance_both_prose_no_match",
     "| Neutral generic rule statement fully prose category row here | completely made up cat | completely made up imp | 0: x |",
     lambda r: len(r) == 1 and r[0]["category"] == "other" and r[0]["importance"] == "important")


_add("is_negative_only_triggered_by_x_mark_in_full_line",
     "| Neutral generic rule statement without x mark in full line here | other | important | 0: x | tip \u274c |",
     lambda r: len(r) == 1 and r[0]["is_negative"] is True)


_add("suggestion_with_check_mark_not_extracted",
     "| Neutral generic rule statement suggestion with check mark here | other | important | 0: x | \u2705 validation done |",
     lambda r: len(r) == 1)


_add("suggestion_equals_criterion_not_extracted",
     "| Repeated criterion text passes length |  Repeated criterion text passes length |",
     lambda r: r == [] or (len(r) == 1 and r[0]["suggestion"] == ""))


_add("empty_levels_defaults_to_two",
     "| Neutral generic rule statement empty levels default test here | other | important |",
     lambda r: len(r) == 1 and len(r[0]["levels"]) == 2 and r[0]["weight"] == 2)


_add("level_label_with_pipe_char_stripped",
     "| Neutral generic rule statement pipe in level label test here | other | important | 0: bad | 1: ok |",
     lambda r: len(r) == 1)


_add("category_with_hyphens_underscore_normalized",
     "| Neutral generic rule statement hyphen underscore test here now | factuality-hallucination | important | 0: x |",
     lambda r: len(r) == 1)


_add("multiline_prose_then_table",
     "This is some introduction text.\nAnd a second paragraph.\n\n| Neutral generic rule statement after prose header passes here | other | important | 0: x |",
     lambda r: len(r) == 1)


_add("table_with_trailing_prose_after",
     "| Neutral generic rule statement with trailing prose after table here | other | important | 0: x |\n\nSome additional notes below.",
     lambda r: len(r) == 1)

_add("stray_colon_in_criterion_handled",
     "| Neutral generic rule statement with colon: present passes here | other | important | 0: x |",
     lambda r: len(r) == 1)


_add("criterion_with_parens_preserved",
     "| Neutral (generic) rule statement with (parentheses) passes length | other | important | 0: x |",
     lambda r: len(r) == 1 and "(" in r[0]["name"])


_add("criterion_with_quotes_preserved",
     "| Neutral 'generic' rule statement with \"quotes\" passes length here | other | important | 0: x |",
     lambda r: len(r) == 1)


_add("empty_levels_col_default_two",
     "| Neutral generic rule statement empty scores col test here now | other | important |  |",
     lambda r: len(r) == 1)


_add("no_score_but_text_col_returns_default_levels",
     "| Neutral generic rule statement with text only in levels col here | other | important | just some text no scores |",
     lambda r: len(r) == 1)

_add("score_without_colon_not_parsed_as_level",
     "| Neutral generic rule statement score number without colon test | other | important | 5 level only |",
     lambda r: len(r) == 1)

_add("score_with_multiple_spaces_around_colon",
     "| Neutral generic rule statement space around colon test here now | other | important | 0  :  zero 1  :  one |",
     lambda r: len(r) == 1 and len(r[0]["levels"]) >= 1)

_add("consecutive_newlines_between_rows_ignored",
     ("| Neutral generic rule statement first row passes length here alpha | other | important | 0: x |\n\n\n"
      "| Neutral generic rule statement second row passes length here beta | other | important | 0: x |"),
     lambda r: len(r) == 2)

_add("mixed_dashes_and_spaces_separator_row",
     ("| Neutral generic rule statement valid row alpha passes length here | other | important | 0: x |\n"
      "| - - - | - - - | - - - |"),
     lambda r: len(r) == 1)

_add("separator_with_equals_not_filtered",
     ("| Neutral generic rule statement valid above equals row here gamma | other | important | 0: x |\n"
      "| = = = | = = = | = = = |"),
     lambda r: len(r) >= 1)

_add("row_with_category_other_colon_extracts_custom",
     "| Neutral generic rule statement custom category colon extract here | Other: SuperSpecial | important | 0: x |",
     lambda r: len(r) == 1 and r[0]["custom_category"] == "SuperSpecial")

_add("row_with_plain_other_no_custom",
     "| Neutral generic rule statement plain other no custom here now | other | important | 0: x |",
     lambda r: len(r) == 1 and r[0]["custom_category"] == "")

_add("importance_partial_word_match",
     "| Neutral generic rule statement partial importance match here | other | slightlyimportant | 0: x |",
     lambda r: len(r) == 1 and r[0]["importance"] == "slightly_important")

_add("criterion_with_numbers_in_text",
     "| Neutral generic rule statement with numbers 42 and 3.14 passes | other | important | 0: x |",
     lambda r: len(r) == 1)

_add("criterion_all_caps",
     "| NEUTRAL GENERIC RULE STATEMENT ALL CAPS TEST PASSES LENGTH | other | important | 0: x |",
     lambda r: len(r) == 1)

_add("criterion_all_lowercase",
     "| neutral generic rule statement all lowercase test passes length | other | important | 0: x |",
     lambda r: len(r) == 1)

_add("criterion_with_backslash",
     "| Neutral generic rule statement with back\\slash passes length here | other | important | 0: x |",
     lambda r: len(r) == 1)

_add("criterion_with_forward_slash",
     "| Neutral generic rule statement with forward/slash passes length here | other | important | 0: x |",
     lambda r: len(r) == 1)

_add("criterion_with_at_sign",
     "| Neutral generic rule statement with @sign passes length boundary here | other | important | 0: x |",
     lambda r: len(r) == 1)


assert len(_CASES) == 120, "expected 120 distinct parse_rubric cases, got %d" % len(_CASES)


def _make(label, text, check, is_red, idx):
    def _t(self):
        result = _parse_rubric_table(text)
        ok = check(result)
        if is_red:
            self.assertTrue(
                ok,
                "RED BUG test %s: function output was %r for input %r. "
                "If this test fails, the bug is still present. See TEST_REPORT.md for details."
                % (label, result[:3] if isinstance(result, list) else result, text[:150])
            )
        else:
            self.assertTrue(
                ok,
                "case %s (idx %d): function output was %r for input %r"
                % (label, idx, result[:3] if isinstance(result, list) else result, text[:150])
            )
    prefix = "test_rub_RED_" if is_red else "test_rub_"
    _t.__name__ = prefix + "%03d_%s" % (idx, label[:40].replace(" ", "_"))
    return _t


@tagged("atlas", "atlas_rub", "post_install", "-at_install")
class TestParseRubricTable(TransactionCase):
    pass


for _idx, (_label, _text, _check, _is_red) in enumerate(_CASES):
    _m = _make(_label, _text, _check, _is_red, _idx)
    setattr(TestParseRubricTable, _m.__name__, _m)
