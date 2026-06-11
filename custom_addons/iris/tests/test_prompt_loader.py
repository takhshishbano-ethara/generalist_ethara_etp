"""Tests for ``services/prompt_loader.py`` (file-first + ICP override)."""

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import IrisCase
from odoo.addons.iris.services.prompt_loader import PROMPT_NAMES, get_prompt

#: Known anchor string per bundled prompt file.
ANCHORS = {
    "screening": "Forensic Ladder",
    "questions": "Steering Ladder",
    "scorecard": "Recommendation bands",
}


@tagged("post_install", "-at_install", "iris")
class TestPromptLoader(IrisCase):
    def test_all_bundled_prompts_load_non_empty(self):
        for name in PROMPT_NAMES:
            content = get_prompt(self.env, name)
            self.assertTrue(
                content and content.strip(),
                f"Bundled prompt '{name}' is empty",
            )

    def test_bundled_prompts_contain_known_anchors(self):
        for name, anchor in ANCHORS.items():
            self.assertIn(
                anchor, get_prompt(self.env, name),
                f"Prompt '{name}' is missing its anchor string '{anchor}'",
            )

    def test_icp_override_wins(self):
        icp = self.env["ir.config_parameter"].sudo()
        for name in PROMPT_NAMES:
            override = f"OVERRIDE PROMPT FOR {name.upper()}"
            icp.set_param(f"iris.prompt_{name}", override)
            self.assertEqual(get_prompt(self.env, name), override)

    def test_whitespace_only_override_falls_back_to_file(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("iris.prompt_screening", "   \n\t  ")
        content = get_prompt(self.env, "screening")
        self.assertIn(ANCHORS["screening"], content)

    def test_empty_override_falls_back_to_file(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("iris.prompt_questions", "")
        content = get_prompt(self.env, "questions")
        self.assertIn(ANCHORS["questions"], content)

    def test_unknown_prompt_name_raises(self):
        with self.assertRaises(UserError):
            get_prompt(self.env, "nonexistent")
