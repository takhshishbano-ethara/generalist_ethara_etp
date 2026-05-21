# -*- coding: utf-8 -*-
"""Comprehensive tests for the deterministic trajectory QC validator (pure functions)."""
import json

from odoo.tests import tagged

from .common import Kensei2TestCase
from ..controllers.trajectory_qc_validator import (
    validate_trajectory,
    build_report,
    parse_iso8601,
    check_seconds_overflow,
    validate_envelopes,
    detect_hints_mode,
    has_mixed_wrappers,
    unwrap_mixed_messages,
    make_check,
    pass_check,
    warn_check,
    fail_check,
    VALID_TASK_TYPES,
    VALID_COMPLETION_STATUSES,
    KNOWN_PLATFORMS,
    VALID_ROLES,
    VALID_CONTENT_TYPES,
    ALL_VALID_TOOLS,
    PLACEHOLDER_PATTERNS,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

_LABEL = "<test>"


def _minimal_trajectory(**overrides):
    """Return a minimal valid trajectory dict; caller can override any top-level key."""
    traj = {
        "meta_info": {
            "task_type": "home_and_organization",
            "task_description": (
                "A sufficiently long task description that exceeds fifty characters for the validator"
            ),
            "task_completion_status": "success",
            "system_prompt": "You are a helpful assistant.",
            "platform": "macOS",
        },
        "messages": [
            {
                "type": "message",
                "id": "aabb0001",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello"}],
                },
            },
            {
                "type": "message",
                "id": "aabb0002",
                "parentId": "aabb0001",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there!"}],
                },
            },
            {
                "type": "message",
                "id": "aabb0003",
                "parentId": "aabb0002",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Thanks"}],
                },
            },
        ],
    }
    traj.update(overrides)
    return traj


def _run(traj_dict):
    """Validate a trajectory dict and return the list of checks."""
    return validate_trajectory(_LABEL, json.dumps(traj_dict, ensure_ascii=False))


def _run_raw(raw_str):
    """Validate a raw string and return the list of checks."""
    return validate_trajectory(_LABEL, raw_str)


def _verdicts(checks, name=None):
    """Extract verdicts, optionally filtered by check name."""
    if name:
        return [c["verdict"] for c in checks if c["name"] == name]
    return [c["verdict"] for c in checks]


def _find_check(checks, name):
    """Return the first check matching the given name, or None."""
    for c in checks:
        if c["name"] == name:
            return c
    return None


# ── TestValidateTrajectory ───────────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestValidateTrajectory(Kensei2TestCase):
    """~28 methods testing validate_trajectory end-to-end."""

    # 1
    def test_valid_minimal_trajectory(self):
        """All checks pass on a well-formed trajectory."""
        checks = _run(_minimal_trajectory())
        fails = [c for c in checks if c["verdict"] == "FAIL"]
        self.assertEqual(fails, [], f"Unexpected FAILs: {fails}")
        # Expect at least the core checks to be present
        self.assertTrue(len(checks) >= 10)

    # 2
    def test_invalid_json(self):
        """Malformed JSON string → FAIL on json_validity."""
        checks = _run_raw("{not valid json!!!")
        c = _find_check(checks, "json_validity")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("Invalid JSON", c["reason"])
        # Early return — only one check produced
        self.assertEqual(len(checks), 1)

    # 3
    def test_root_not_dict(self):
        """Root is array → FAIL on json_validity (expected object)."""
        checks = _run_raw(json.dumps([1, 2, 3]))
        c = _find_check(checks, "json_validity")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("list", c["reason"])

    # 4
    def test_missing_meta_info(self):
        """Missing meta_info key → FAIL on top_level_keys."""
        traj = _minimal_trajectory()
        del traj["meta_info"]
        checks = _run(traj)
        c = _find_check(checks, "top_level_keys")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("meta_info", c["reason"])

    # 5
    def test_missing_messages(self):
        """Missing messages key → FAIL on messages_exists."""
        traj = _minimal_trajectory()
        del traj["messages"]
        checks = _run(traj)
        c = _find_check(checks, "messages_exists")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")

    # 6
    def test_meta_info_not_dict(self):
        """meta_info is a string → FAIL on meta_info_exists."""
        traj = _minimal_trajectory()
        traj["meta_info"] = "not a dict"
        checks = _run(traj)
        c = _find_check(checks, "meta_info_exists")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("str", c["reason"])

    # 7
    def test_meta_info_missing_required_keys(self):
        """meta_info missing required keys → FAIL with missing key list."""
        traj = _minimal_trajectory()
        traj["meta_info"] = {"task_type": "home_and_organization"}  # missing 4 keys
        checks = _run(traj)
        c = _find_check(checks, "meta_info_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("Missing required keys", c["reason"])

    # 8
    def test_meta_info_extra_keys(self):
        """meta_info with extra keys → WARN."""
        traj = _minimal_trajectory()
        traj["meta_info"]["surprise_key"] = "boo"
        checks = _run(traj)
        c = _find_check(checks, "meta_info_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("Extra keys", c["reason"])

    # 9
    def test_meta_info_invalid_task_type(self):
        """Invalid task_type → FAIL."""
        traj = _minimal_trajectory()
        traj["meta_info"]["task_type"] = "alien_invasion"
        checks = _run(traj)
        c = _find_check(checks, "task_type")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("Invalid task_type", c["reason"])

    # 10
    def test_meta_info_valid_task_types(self):
        """Each VALID_TASK_TYPE passes validation."""
        for tt in VALID_TASK_TYPES:
            traj = _minimal_trajectory()
            traj["meta_info"]["task_type"] = tt
            checks = _run(traj)
            c = _find_check(checks, "task_type")
            self.assertIsNotNone(c, f"No task_type check for {tt}")
            self.assertEqual(
                c["verdict"], "PASS", f"task_type '{tt}' should PASS, got {c}"
            )

    # 11
    def test_meta_info_placeholder_task_description(self):
        """Placeholder descriptions like 'tbd', 'todo', 'n/a' → FAIL."""
        for placeholder in ("tbd", "todo", "n/a", "placeholder", "test", "asdf"):
            traj = _minimal_trajectory()
            traj["meta_info"]["task_description"] = placeholder
            checks = _run(traj)
            c = _find_check(checks, "task_description")
            self.assertIsNotNone(c, f"No task_description check for '{placeholder}'")
            self.assertEqual(
                c["verdict"],
                "FAIL",
                f"Placeholder '{placeholder}' should FAIL, got {c}",
            )
            self.assertIn("placeholder", c["reason"].lower())

    # 12
    def test_meta_info_short_task_description(self):
        """<20 chars → FAIL, 20-49 chars → WARN, >=50 → PASS."""
        # Under 20 → FAIL
        traj = _minimal_trajectory()
        traj["meta_info"]["task_description"] = "Short desc here"  # 15 chars
        checks = _run(traj)
        c = _find_check(checks, "task_description")
        self.assertEqual(c["verdict"], "FAIL")

        # 20-49 → WARN
        traj["meta_info"]["task_description"] = "A description that is 25ch"  # 26 chars
        checks = _run(traj)
        c = _find_check(checks, "task_description")
        self.assertEqual(c["verdict"], "WARN")

        # >=50 → PASS
        traj["meta_info"]["task_description"] = "A" * 55
        checks = _run(traj)
        c = _find_check(checks, "task_description")
        self.assertEqual(c["verdict"], "PASS")

    # 13
    def test_meta_info_empty_task_description(self):
        """Empty task_description → FAIL."""
        traj = _minimal_trajectory()
        traj["meta_info"]["task_description"] = ""
        checks = _run(traj)
        c = _find_check(checks, "task_description")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("empty", c["reason"].lower())

    # 14
    def test_meta_info_missing_completion_status(self):
        """Missing task_completion_status → FAIL."""
        traj = _minimal_trajectory()
        del traj["meta_info"]["task_completion_status"]
        checks = _run(traj)
        # Missing key triggers meta_info_structure FAIL and task_completion_status FAIL
        c = _find_check(checks, "task_completion_status")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")

    # 15
    def test_meta_info_invalid_completion_status(self):
        """Invalid task_completion_status → FAIL."""
        traj = _minimal_trajectory()
        traj["meta_info"]["task_completion_status"] = "maybe"
        checks = _run(traj)
        c = _find_check(checks, "task_completion_status")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("Invalid", c["reason"])

    # 16
    def test_meta_info_empty_system_prompt(self):
        """Empty system_prompt → FAIL (the #1 most commonly failed check)."""
        traj = _minimal_trajectory()
        traj["meta_info"]["system_prompt"] = ""
        checks = _run(traj)
        c = _find_check(checks, "system_prompt")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("#1 most commonly failed check", c["reason"])

    # 17
    def test_meta_info_populated_system_prompt(self):
        """Populated system_prompt → PASS."""
        traj = _minimal_trajectory()
        traj["meta_info"]["system_prompt"] = "You are a helpful AI assistant."
        checks = _run(traj)
        c = _find_check(checks, "system_prompt")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "PASS")
        self.assertIn("populated", c["reason"])

    # 18
    def test_meta_info_unknown_platform(self):
        """Unknown platform → WARN."""
        traj = _minimal_trajectory()
        traj["meta_info"]["platform"] = "TempleOS"
        checks = _run(traj)
        c = _find_check(checks, "platform")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("TempleOS", c["reason"])

    # 19
    def test_messages_empty_array(self):
        """Empty messages array → FAIL on messages_exists."""
        traj = _minimal_trajectory()
        traj["messages"] = []
        checks = _run(traj)
        c = _find_check(checks, "messages_exists")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("empty", c["reason"])

    # 20
    def test_message_missing_type_field(self):
        """Message envelope missing 'type' → FAIL on message_envelope."""
        traj = _minimal_trajectory()
        msg = {
            "id": "aabb0001",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello"}],
            },
        }
        # No 'type' key at all
        traj["messages"] = [msg]
        checks = _run(traj)
        c = _find_check(checks, "message_envelope")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("missing 'type'", c["reason"])

    # 21
    def test_message_missing_id(self):
        """Message envelope missing 'id' → FAIL on message_envelope."""
        traj = _minimal_trajectory()
        msg = {
            "type": "message",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello"}],
            },
        }
        traj["messages"] = [msg]
        checks = _run(traj)
        c = _find_check(checks, "message_envelope")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("id", c["reason"].lower())

    # 22
    def test_message_invalid_role(self):
        """Invalid role (case-sensitive) → FAIL on message_role."""
        traj = _minimal_trajectory()
        traj["messages"][0]["message"]["role"] = "User"  # uppercase U
        checks = _run(traj)
        c = _find_check(checks, "message_role")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("case-sensitive", c["reason"])

    # 23
    def test_first_message_not_user(self):
        """First message role is assistant → WARN on first_message_role."""
        traj = _minimal_trajectory()
        traj["messages"][0]["message"]["role"] = "assistant"
        checks = _run(traj)
        c = _find_check(checks, "first_message_role")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("assistant", c["reason"])

    # 24
    def test_parentId_chain_broken(self):
        """parentId references non-existent id → detected in parent_id_chain."""
        traj = _minimal_trajectory()
        traj["messages"][1]["parentId"] = "deadbeef"  # valid hex8 but not a real id
        checks = _run(traj)
        c = _find_check(checks, "parent_id_chain")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("deadbeef", c["reason"])

    # 25
    def test_timestamp_non_monotonic(self):
        """Non-monotonic timestamps → WARN on timestamp_monotonicity."""
        traj = _minimal_trajectory()
        # Make 2nd message timestamp earlier than 1st
        traj["messages"][1]["timestamp"] = "2025-12-31T23:59:59Z"
        checks = _run(traj)
        c = _find_check(checks, "timestamp_monotonicity")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("before previous", c["reason"])

    # 26
    def test_timestamp_seconds_overflow(self):
        """Seconds >= 60 in timestamp → BLOCK marker in envelope."""
        traj = _minimal_trajectory()
        traj["messages"][0]["timestamp"] = "2026-01-01T00:00:61Z"
        checks = _run(traj)
        c = _find_check(checks, "message_envelope")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("seconds=61", c["reason"])

    # 27
    def test_toolCall_missing_toolResult(self):
        """A toolCall with no matching toolResult → orphaned call detected."""
        traj = _minimal_trajectory()
        traj["messages"] = [
            {
                "type": "message",
                "id": "aabb0001",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Search for cats"}],
                },
            },
            {
                "type": "message",
                "id": "aabb0002",
                "parentId": "aabb0001",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "toolCall": {
                                "id": "tc-orphan-001",
                                "name": "web_search",
                                "arguments": {"query": "cats"},
                            },
                        }
                    ],
                },
            },
            {
                "type": "message",
                "id": "aabb0003",
                "parentId": "aabb0002",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Here are results about cats."}],
                },
            },
        ]
        checks = _run(traj)
        c = _find_check(checks, "tool_call_result_pairing")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("tc-orphan-001", c["reason"])

    # 28
    def test_content_block_invalid_type(self):
        """Content block with unrecognized type → FAIL on content_block_type."""
        traj = _minimal_trajectory()
        traj["messages"][0]["message"]["content"] = [
            {"type": "video", "url": "http://example.com/cat.mp4"}
        ]
        checks = _run(traj)
        c = _find_check(checks, "content_block_type")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("video", c["reason"])


# ── TestBuildReport ──────────────────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestBuildReport(Kensei2TestCase):
    """~6 methods testing build_report severity and count logic."""

    # 1
    def test_all_pass_severity_low(self):
        """No fails/warns → severity 'low'."""
        checks = [
            pass_check(1, "a", "ok"),
            pass_check(2, "b", "ok"),
            pass_check(3, "c", "ok"),
        ]
        report = build_report(_LABEL, checks)
        self.assertEqual(report["severity"], "low")

    # 2
    def test_warns_only_medium(self):
        """1+ warns, 0 fails → severity 'medium'."""
        checks = [
            pass_check(1, "a", "ok"),
            warn_check(2, "b", "suspicious"),
        ]
        report = build_report(_LABEL, checks)
        self.assertEqual(report["severity"], "medium")

    # 3
    def test_many_warns_high(self):
        """5+ warns, 0 fails → severity 'high'."""
        checks = [warn_check(i, f"w{i}", "warn") for i in range(1, 7)]
        report = build_report(_LABEL, checks)
        self.assertEqual(report["severity"], "high")
        # Exactly 5 should also be high
        checks5 = [warn_check(i, f"w{i}", "warn") for i in range(1, 6)]
        report5 = build_report(_LABEL, checks5)
        self.assertEqual(report5["severity"], "high")

    # 4
    def test_any_fail_critical(self):
        """1+ fail → severity 'critical' regardless of warns."""
        checks = [
            pass_check(1, "a", "ok"),
            fail_check(2, "b", "broken"),
            warn_check(3, "c", "suspicious"),
        ]
        report = build_report(_LABEL, checks)
        self.assertEqual(report["severity"], "critical")

    # 5
    def test_counts_correct(self):
        """total_fails, total_warns, total_passes are accurate."""
        checks = [
            pass_check(1, "a", "ok"),
            pass_check(2, "b", "ok"),
            warn_check(3, "c", "hmm"),
            fail_check(4, "d", "bad"),
            fail_check(5, "e", "bad"),
        ]
        report = build_report(_LABEL, checks)
        self.assertEqual(report["total_passes"], 2)
        self.assertEqual(report["total_warns"], 1)
        self.assertEqual(report["total_fails"], 2)

    # 6
    def test_summary_populated(self):
        """Summary string is non-empty and includes file path label."""
        checks = [pass_check(1, "a", "ok")]
        report = build_report(_LABEL, checks)
        self.assertIsInstance(report["summary"], str)
        self.assertTrue(len(report["summary"]) > 0)
        self.assertIn(_LABEL, report["summary"])
        self.assertIn("All checks passed", report["summary"])


# ── TestHelperFunctions ──────────────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestHelperFunctions(Kensei2TestCase):
    """Tests for pure helper functions."""

    # 1
    def test_parse_iso8601_valid(self):
        """Valid ISO 8601 string → returns datetime, error is None."""
        dt, err = parse_iso8601("2026-01-15T10:30:00Z")
        self.assertIsNotNone(dt)
        self.assertIsNone(err)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.hour, 10)
        self.assertEqual(dt.minute, 30)

    # 2
    def test_parse_iso8601_invalid(self):
        """Invalid string → returns None + error message."""
        dt, err = parse_iso8601("not-a-timestamp")
        self.assertIsNone(dt)
        self.assertIsNotNone(err)
        self.assertIn("does not match", err)

        # Non-string input
        dt2, err2 = parse_iso8601(12345)
        self.assertIsNone(dt2)
        self.assertIn("not a string", err2)

    # 3
    def test_check_seconds_overflow_normal(self):
        """Normal seconds (< 60) → returns (False, None)."""
        overflow, secs = check_seconds_overflow("2026-01-01T12:30:45Z")
        self.assertFalse(overflow)
        self.assertIsNone(secs)

    # 4
    def test_check_seconds_overflow_60(self):
        """Seconds >= 60 → returns (True, seconds_value)."""
        overflow, secs = check_seconds_overflow("2026-01-01T12:30:60Z")
        self.assertTrue(overflow)
        self.assertEqual(secs, 60)

        overflow2, secs2 = check_seconds_overflow("2026-01-01T12:30:99Z")
        self.assertTrue(overflow2)
        self.assertEqual(secs2, 99)

    # 5
    def test_detect_hints_mode(self):
        """Presence of past_conversations key → hints mode detected."""
        self.assertTrue(detect_hints_mode({"past_conversations": [], "messages": []}))
        self.assertFalse(detect_hints_mode({"messages": []}))
        self.assertFalse(detect_hints_mode({}))

    # 6
    def test_has_mixed_wrappers(self):
        """Detects is_accepted key in message list items."""
        plain = [{"type": "message", "id": "1"}]
        self.assertFalse(has_mixed_wrappers(plain))

        mixed = [{"is_accepted": 1, "message": {"type": "message"}}]
        self.assertTrue(has_mixed_wrappers(mixed))

        # Non-list input
        self.assertFalse(has_mixed_wrappers("not a list"))
        self.assertFalse(has_mixed_wrappers(None))

    # 7
    def test_unwrap_mixed_messages(self):
        """Strips wrapper (is_accepted + message), keeps inner envelope."""
        inner = {"type": "message", "id": "aabb0001", "message": {"role": "user"}}
        wrapped = [{"is_accepted": 1, "hints": None, "message": inner}]
        result = unwrap_mixed_messages(wrapped)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], inner)

        # Non-wrapper items pass through unchanged
        plain = {"type": "message", "id": "aabb0002"}
        result2 = unwrap_mixed_messages([plain])
        self.assertEqual(result2[0], plain)

        # Non-dict items pass through
        result3 = unwrap_mixed_messages(["string_item", 42])
        self.assertEqual(result3, ["string_item", 42])

    # ── make_check / pass_check / warn_check / fail_check ────────────────

    def test_make_check_structure(self):
        """make_check returns dict with expected keys."""
        c = make_check(1, "test_name", "PASS", "All good")
        self.assertEqual(c["check"], 1)
        self.assertEqual(c["name"], "test_name")
        self.assertEqual(c["verdict"], "PASS")
        self.assertEqual(c["reason"], "All good")
        self.assertNotIn("fix", c)

    def test_make_check_with_fix(self):
        """make_check includes fix when provided."""
        c = make_check(2, "fixable", "FAIL", "broken", fix="repair it")
        self.assertEqual(c["fix"], "repair it")

    def test_pass_check_defaults(self):
        """pass_check defaults reason to 'OK'."""
        c = pass_check(1, "check_a")
        self.assertEqual(c["verdict"], "PASS")
        self.assertEqual(c["reason"], "OK")

    def test_warn_check_verdict(self):
        """warn_check sets verdict to WARN."""
        c = warn_check(1, "check_b", "something off")
        self.assertEqual(c["verdict"], "WARN")

    def test_fail_check_verdict(self):
        """fail_check sets verdict to FAIL."""
        c = fail_check(1, "check_c", "broken")
        self.assertEqual(c["verdict"], "FAIL")

    # ── validate_envelopes ───────────────────────────────────────────────

    def test_validate_envelopes_returns_expected_keys(self):
        """validate_envelopes returns a dict with all expected result keys."""
        envelopes = [
            {
                "type": "message",
                "id": "aabb0001",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hi"}],
                },
            }
        ]
        result = validate_envelopes(envelopes, "messages")
        expected_keys = {
            "envelope_issues",
            "inner_msg_issues",
            "role_issues",
            "content_array_issues",
            "content_type_issues",
            "content_structure_issues",
            "timestamp_issues",
            "seen_ids",
            "duplicate_id_issues",
            "tool_call_ids",
            "tool_result_ids",
            "parent_id_issues",
            "first_role",
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_validate_envelopes_clean_input(self):
        """Clean envelope input → no issues."""
        envelopes = [
            {
                "type": "message",
                "id": "aabb0001",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hi"}],
                },
            }
        ]
        result = validate_envelopes(envelopes, "messages")
        self.assertEqual(result["envelope_issues"], [])
        self.assertEqual(result["inner_msg_issues"], [])
        self.assertEqual(result["role_issues"], [])
        self.assertEqual(result["first_role"], "user")

    # ── Additional edge-case coverage ────────────────────────────────────

    def test_valid_completion_statuses_accepted(self):
        """Each VALID_COMPLETION_STATUS passes validation."""
        for status in VALID_COMPLETION_STATUSES:
            traj = _minimal_trajectory()
            traj["meta_info"]["task_completion_status"] = status
            checks = _run(traj)
            c = _find_check(checks, "task_completion_status")
            self.assertEqual(
                c["verdict"], "PASS", f"Status '{status}' should PASS"
            )

    def test_known_platforms_accepted(self):
        """Each KNOWN_PLATFORM passes validation."""
        for platform in KNOWN_PLATFORMS:
            traj = _minimal_trajectory()
            traj["meta_info"]["platform"] = platform
            checks = _run(traj)
            c = _find_check(checks, "platform")
            self.assertEqual(
                c["verdict"], "PASS", f"Platform '{platform}' should PASS"
            )

    def test_whitespace_only_task_description(self):
        """Whitespace-only task_description → FAIL (empty after strip)."""
        traj = _minimal_trajectory()
        traj["meta_info"]["task_description"] = "   \t\n  "
        checks = _run(traj)
        c = _find_check(checks, "task_description")
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("empty", c["reason"].lower())

    def test_parse_iso8601_with_timezone_offset(self):
        """ISO 8601 with +HH:MM offset parses correctly."""
        dt, err = parse_iso8601("2026-06-15T14:30:00+05:30")
        self.assertIsNotNone(dt)
        self.assertIsNone(err)
        self.assertEqual(dt.hour, 14)

    def test_parse_iso8601_with_fractional_seconds(self):
        """ISO 8601 with fractional seconds parses correctly."""
        dt, err = parse_iso8601("2026-06-15T14:30:00.123456Z")
        self.assertIsNotNone(dt)
        self.assertIsNone(err)


# ── Hints-mode helpers ───────────────────────────────────────────────────────


def _hint_envelope(id_, parentId, role, text, ts):
    """Return a standard message envelope for use inside hint wrappers."""
    return {
        "type": "message",
        "id": id_,
        "parentId": parentId,
        "timestamp": ts,
        "message": {
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
    }


def _hints_trajectory(**overrides):
    """Return a minimal valid hints-mode trajectory dict."""
    traj = {
        "meta_info": {
            "task_type": "home_and_organization",
            "task_description": (
                "A sufficiently long task description that exceeds fifty characters for the validator"
            ),
            "task_completion_status": "success",
            "system_prompt": "You are a helpful assistant.",
            "platform": "macOS",
            "conv_id": "conv-abc-123",
        },
        "past_conversations": [
            _hint_envelope("cc000001", None, "user", "Old question", "2025-12-31T00:00:00Z"),
            _hint_envelope("cc000002", "cc000001", "assistant", "Old answer", "2025-12-31T00:00:01Z"),
        ],
        "messages": [
            {
                "is_accepted": 0,
                "hints": None,
                "message": _hint_envelope("aabb0001", None, "user", "Hello", "2026-01-01T00:00:00Z"),
            },
            {
                "is_accepted": 0,
                "hints": "Try rephrasing",
                "message": _hint_envelope("aabb0002", "aabb0001", "assistant", "Hi!", "2026-01-01T00:00:01Z"),
            },
            {
                "is_accepted": 1,
                "hints": None,
                "message": _hint_envelope("aabb0003", "aabb0002", "user", "Thanks", "2026-01-01T00:00:02Z"),
            },
        ],
    }
    traj.update(overrides)
    return traj


# ── TestHintsMode ────────────────────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestHintsMode(Kensei2TestCase):
    """12 methods testing hints-mode specific validation."""

    # 1
    def test_hints_mode_valid(self):
        """Trajectory with past_conversations + proper wrappers → all pass."""
        checks = _run(_hints_trajectory())
        fails = [c for c in checks if c["verdict"] == "FAIL"]
        self.assertEqual(fails, [], f"Unexpected FAILs: {fails}")
        c = _find_check(checks, "hint_wrapper")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "PASS")
        c2 = _find_check(checks, "hint_flow_pattern")
        self.assertIsNotNone(c2)
        self.assertEqual(c2["verdict"], "PASS")

    # 2
    def test_hints_mode_missing_past_conversations(self):
        """past_conversations set to None (key present) → triggers hints mode, FAIL validation."""
        traj = _hints_trajectory()
        traj["past_conversations"] = None
        checks = _run(traj)
        c = _find_check(checks, "past_conversations")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")

    # 3
    def test_hints_mode_past_conversations_non_array(self):
        """past_conversations is string type → FAIL."""
        traj = _hints_trajectory()
        traj["past_conversations"] = "not an array"
        checks = _run(traj)
        c = _find_check(checks, "past_conversations")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("str", c["reason"])

    # 4
    def test_hints_mode_wrapper_missing_is_accepted(self):
        """Required key is_accepted missing from wrapper → FAIL."""
        traj = _hints_trajectory()
        del traj["messages"][0]["is_accepted"]
        checks = _run(traj)
        c = _find_check(checks, "hint_wrapper")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("is_accepted", c["reason"])

    # 5
    def test_hints_mode_wrapper_missing_hints(self):
        """Required key hints missing from wrapper → FAIL."""
        traj = _hints_trajectory()
        del traj["messages"][1]["hints"]
        checks = _run(traj)
        c = _find_check(checks, "hint_wrapper")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("hints", c["reason"])

    # 6
    def test_hints_mode_wrapper_missing_message(self):
        """Required key message missing from wrapper → FAIL."""
        traj = _hints_trajectory()
        del traj["messages"][0]["message"]
        checks = _run(traj)
        c = _find_check(checks, "hint_wrapper")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("message", c["reason"])

    # 7
    def test_hints_mode_is_accepted_not_integer(self):
        """is_accepted is bool type → FAIL."""
        traj = _hints_trajectory()
        traj["messages"][2]["is_accepted"] = True
        checks = _run(traj)
        c = _find_check(checks, "hint_wrapper")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("bool", c["reason"])

    # 8
    def test_hints_mode_is_accepted_invalid_value(self):
        """is_accepted=2 → FAIL (expected 0 or 1)."""
        traj = _hints_trajectory()
        traj["messages"][2]["is_accepted"] = 2
        checks = _run(traj)
        c = _find_check(checks, "hint_wrapper")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("expected 0 or 1", c["reason"])

    # 9
    def test_hints_mode_multiple_accepted(self):
        """Two entries with is_accepted=1 → FAIL."""
        traj = _hints_trajectory()
        traj["messages"][1]["is_accepted"] = 1
        checks = _run(traj)
        c = _find_check(checks, "hint_flow_pattern")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("Multiple", c["reason"])

    # 10
    def test_hints_mode_accepted_not_last(self):
        """is_accepted=1 at index 0, not last → FAIL."""
        traj = _hints_trajectory()
        traj["messages"][0]["is_accepted"] = 1
        traj["messages"][2]["is_accepted"] = 0
        checks = _run(traj)
        c = _find_check(checks, "hint_flow_pattern")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("must be the last element", c["reason"])

    # 11
    def test_hints_mode_first_entry_has_hint(self):
        """First entry has non-null hints → WARN."""
        traj = _hints_trajectory()
        traj["messages"][0]["hints"] = "Unexpected hint on first entry"
        checks = _run(traj)
        c = _find_check(checks, "hint_flow_pattern")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("First entry", c["reason"])

    # 12
    def test_hints_mode_conv_id_required(self):
        """No conv_id in hints mode → FAIL."""
        traj = _hints_trajectory()
        del traj["meta_info"]["conv_id"]
        checks = _run(traj)
        c = _find_check(checks, "conv_id")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("required", c["reason"])


# ── TestEnvelopeEdgeCases ────────────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestEnvelopeEdgeCases(Kensei2TestCase):
    """12 methods testing envelope and content block edge cases."""

    # 1
    def test_envelope_non_dict_item(self):
        """String in messages list → issue on message_envelope."""
        traj = _minimal_trajectory()
        traj["messages"] = ["not a dict"]
        checks = _run(traj)
        c = _find_check(checks, "message_envelope")
        self.assertIsNotNone(c)
        self.assertIn("not an object", c["reason"])

    # 2
    def test_envelope_duplicate_ids(self):
        """Same id twice → duplicate issue on parent_id_chain."""
        traj = _minimal_trajectory()
        traj["messages"][1]["id"] = traj["messages"][0]["id"]
        checks = _run(traj)
        c = _find_check(checks, "parent_id_chain")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("Duplicate", c["reason"])

    # 3
    def test_envelope_parentId_null_non_first(self):
        """Second message parentId=None → issue on parent_id_chain."""
        traj = _minimal_trajectory()
        traj["messages"][1]["parentId"] = None
        checks = _run(traj)
        c = _find_check(checks, "parent_id_chain")
        self.assertIsNotNone(c)
        self.assertIn("parentId is null", c["reason"])

    # 4
    def test_envelope_parentId_non_string(self):
        """parentId=123 → issue on message_envelope."""
        traj = _minimal_trajectory()
        traj["messages"][1]["parentId"] = 123
        checks = _run(traj)
        c = _find_check(checks, "message_envelope")
        self.assertIsNotNone(c)
        self.assertIn("int", c["reason"])

    # 5
    def test_envelope_missing_timestamp(self):
        """No timestamp → issue on message_envelope."""
        traj = _minimal_trajectory()
        del traj["messages"][0]["timestamp"]
        checks = _run(traj)
        c = _find_check(checks, "message_envelope")
        self.assertIsNotNone(c)
        self.assertIn("timestamp", c["reason"])

    # 6
    def test_envelope_missing_message_field(self):
        """No message key → issue on message_envelope."""
        traj = _minimal_trajectory()
        del traj["messages"][0]["message"]
        checks = _run(traj)
        c = _find_check(checks, "message_envelope")
        self.assertIsNotNone(c)
        self.assertIn("message", c["reason"])

    # 7
    def test_content_empty_text_block(self):
        """text="" → WARN on content_block_structure."""
        traj = _minimal_trajectory()
        traj["messages"][0]["message"]["content"] = [{"type": "text", "text": ""}]
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("empty", c["reason"])

    # 8
    def test_content_thinking_block_valid(self):
        """thinking + thinkingSignature → PASS on content_block_structure."""
        traj = _minimal_trajectory()
        traj["messages"][1]["message"]["content"] = [
            {
                "type": "thinking",
                "thinking": "Let me think about this...",
                "thinkingSignature": "sig123",
            }
        ]
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "PASS")

    # 9
    def test_content_thinking_block_missing_signature(self):
        """No thinkingSignature → WARN on content_block_structure."""
        traj = _minimal_trajectory()
        traj["messages"][1]["message"]["content"] = [
            {"type": "thinking", "thinking": "Some thought process"}
        ]
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("thinkingSignature", c["reason"])

    # 10
    def test_content_thinking_block_missing_thinking_field(self):
        """No thinking key → BLOCK/FAIL on content_block_structure."""
        traj = _minimal_trajectory()
        traj["messages"][1]["message"]["content"] = [
            {"type": "thinking", "thinkingSignature": "sig123"}
        ]
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("missing 'thinking'", c["reason"])

    # 11
    def test_content_thinking_block_empty_thinking(self):
        """thinking="" → WARN on content_block_structure."""
        traj = _minimal_trajectory()
        traj["messages"][1]["message"]["content"] = [
            {
                "type": "thinking",
                "thinking": "",
                "thinkingSignature": "sig123",
            }
        ]
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("empty", c["reason"])

    # 12
    def test_content_thinking_block_non_string_thinking(self):
        """thinking=123 → BLOCK/FAIL on content_block_structure."""
        traj = _minimal_trajectory()
        traj["messages"][1]["message"]["content"] = [
            {
                "type": "thinking",
                "thinking": 123,
                "thinkingSignature": "sig123",
            }
        ]
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("int", c["reason"])


# ── TestToolCallEdgeCases ────────────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestToolCallEdgeCases(Kensei2TestCase):
    """6 methods testing toolCall/toolResult content block edge cases."""

    def _traj_with_tool_content(self, content_blocks):
        """Helper: build trajectory where messages[1] (assistant) has given content."""
        traj = _minimal_trajectory()
        traj["messages"][1]["message"]["content"] = content_blocks
        return traj

    # 1
    def test_toolCall_nested_format(self):
        """toolCall with nested toolCall object → valid."""
        traj = self._traj_with_tool_content([
            {
                "type": "toolCall",
                "toolCall": {
                    "id": "tc-001",
                    "name": "web_search",
                    "arguments": {"query": "cats"},
                },
            }
        ])
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertNotEqual(c["verdict"], "FAIL")

    # 2
    def test_toolCall_flat_format(self):
        """toolCall with id/name directly on block → valid."""
        traj = self._traj_with_tool_content([
            {
                "type": "toolCall",
                "id": "tc-002",
                "name": "web_fetch",
                "arguments": {"url": "https://example.com"},
            }
        ])
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertNotEqual(c["verdict"], "FAIL")

    # 3
    def test_toolCall_missing_id_and_name(self):
        """Neither nested toolCall nor id/name on block → BLOCK/FAIL."""
        traj = self._traj_with_tool_content([
            {"type": "toolCall", "arguments": {"query": "cats"}}
        ])
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("missing", c["reason"].lower())

    # 4
    def test_toolCall_unrecognized_name(self):
        """Custom/unknown tool name → WARN on content_block_structure."""
        traj = self._traj_with_tool_content([
            {
                "type": "toolCall",
                "toolCall": {
                    "id": "tc-003",
                    "name": "my_custom_nonexistent_tool",
                    "arguments": {},
                },
            }
        ])
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "WARN")
        self.assertIn("not a recognized tool", c["reason"])

    # 5
    def test_toolCall_arguments_invalid_json_string(self):
        """arguments is a string but not valid JSON → BLOCK/FAIL."""
        traj = self._traj_with_tool_content([
            {
                "type": "toolCall",
                "toolCall": {
                    "id": "tc-004",
                    "name": "web_search",
                    "arguments": "{not valid json",
                },
            }
        ])
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("not valid JSON", c["reason"])

    # 6
    def test_toolResult_block_missing_nested(self):
        """toolResult block without nested toolResult object → BLOCK/FAIL."""
        traj = self._traj_with_tool_content([
            {"type": "toolResult", "id": "tr-001", "isError": False}
        ])
        checks = _run(traj)
        c = _find_check(checks, "content_block_structure")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("missing nested", c["reason"].lower())


# ── TestMetaInfoEdgeCases ────────────────────────────────────────────────────


@tagged("post_install", "-at_install")
class TestMetaInfoEdgeCases(Kensei2TestCase):
    """5 methods testing meta_info field edge cases."""

    # 1
    def test_task_type_case_mismatch(self):
        """'Home_And_Organization' (wrong casing) → FAIL with hint."""
        traj = _minimal_trajectory()
        traj["meta_info"]["task_type"] = "Home_And_Organization"
        checks = _run(traj)
        c = _find_check(checks, "task_type")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("did you mean", c["reason"].lower())

    # 2
    def test_task_description_non_string(self):
        """task_description is integer → FAIL."""
        traj = _minimal_trajectory()
        traj["meta_info"]["task_description"] = 42
        checks = _run(traj)
        c = _find_check(checks, "task_description")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("int", c["reason"])

    # 3
    def test_system_prompt_non_string(self):
        """system_prompt is list → FAIL."""
        traj = _minimal_trajectory()
        traj["meta_info"]["system_prompt"] = ["not", "a", "string"]
        checks = _run(traj)
        c = _find_check(checks, "system_prompt")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")
        self.assertIn("list", c["reason"])

    # 4
    def test_platform_missing(self):
        """No platform key → FAIL."""
        traj = _minimal_trajectory()
        del traj["meta_info"]["platform"]
        checks = _run(traj)
        c = _find_check(checks, "platform")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "FAIL")

    # 5
    def test_session_id_valid_uuid(self):
        """UUID format session_id → PASS."""
        traj = _minimal_trajectory()
        traj["meta_info"]["session_id"] = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        checks = _run(traj)
        c = _find_check(checks, "session_id")
        self.assertIsNotNone(c)
        self.assertEqual(c["verdict"], "PASS")
        self.assertIn("valid UUID", c["reason"])
