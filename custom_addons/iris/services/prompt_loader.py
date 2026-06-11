"""Prompt loading for Iris: role override → ICP override → bundled file.

Follows the gohan/vegeta convention: prompts ship as version-controlled
``prompts/*.md`` files inside the addon, and each can be overridden at
runtime via an ``ir.config_parameter`` (Settings) without redeploying.
v1.1 adds an optional per-role FULL override layer on top (an
``iris.role.profile`` carries ``<name>_prompt`` Text fields for a subset
of the prompt names — missing/empty fields fall through).

This is the ONE services module allowed to import from ``odoo``
(``UserError`` for a clean, user-facing failure).
"""

from __future__ import annotations

from pathlib import Path

from odoo.exceptions import UserError

#: Valid prompt names → bundled file stems (``prompts/<NAME upper>.md``).
PROMPT_NAMES = (
    "screening",
    "questions",
    "scorecard",
    "batch_consistency",
    "jd_critique",
    "jd_rewrite",
    "assessment_review",
    "clarifying_questions",
)

#: ICP key template for the Settings override.
_ICP_KEY = "iris.prompt_{name}"

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def get_prompt(env, name: str, role=None) -> str:
    """Return the system prompt text for ``name``.

    Resolution order:

    1. The role profile's ``<name>_prompt`` field when ``role`` is given
       and the field exists with a non-empty (after strip) value — the
       per-role FULL override. Role profiles only define a subset of the
       prompt names (screening / questions / scorecard /
       batch_consistency); the rest fall through silently.
    2. ``ir.config_parameter`` ``iris.prompt_<name>`` when set to a
       non-empty (after strip) value — the Settings override.
    3. The bundled file ``prompts/<NAME>.md`` shipped with the addon.

    Args:
        env: Odoo environment (used for ICP lookup).
        name: One of :data:`PROMPT_NAMES`.
        role: Optional ``iris.role.profile`` record (an empty recordset or
            ``None`` skips the role layer).

    Returns:
        str: The prompt text (never empty).

    Raises:
        UserError: If ``name`` is unknown, or no resolution layer yields
            non-empty content.
    """
    if name not in PROMPT_NAMES:
        raise UserError(
            f"Unknown Iris prompt '{name}'. Valid names: {', '.join(PROMPT_NAMES)}."
        )

    if role:
        role_override = getattr(role, f"{name}_prompt", "") or ""
        if role_override.strip():
            return role_override

    icp = env["ir.config_parameter"].sudo()
    override = icp.get_param(_ICP_KEY.format(name=name), "")
    if override and override.strip():
        return override

    prompt_path = _PROMPTS_DIR / f"{name.upper()}.md"
    try:
        content = prompt_path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    if content and content.strip():
        return content

    raise UserError(
        f"Iris prompt '{name}' is empty: no Settings override is configured and "
        f"the bundled file {prompt_path} is missing or empty. "
        "Reinstall the iris addon or set the prompt override in Settings."
    )
