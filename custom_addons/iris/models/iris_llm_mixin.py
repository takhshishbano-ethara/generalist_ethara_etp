"""Shared LLM job mixin for Iris artifacts (screening / interview / scorecard).

Each LLM-produced artifact embeds this AbstractModel to get:

* a uniform ``llm_status`` lifecycle (none → queued → running → done /
  failed / needs_review),
* full call metadata (model used, tokens, cost, latency, raw response,
  audited prompt input),
* the i2i-style async pattern: ``_llm_enqueue()`` sets ``queued`` and
  instant-triggers the queue cron; ``_llm_run_sync()`` performs the actual
  OpenRouter chat completion synchronously (called by the cron).

Concrete models implement the three template methods:

* :meth:`_llm_build_messages` — returns ``(system_prompt, user_text)``
* :meth:`_llm_on_success` — persist content + drive candidate state
* :meth:`_llm_on_failure` — deterministic candidate state revert
"""

import base64
import json
import logging

import markdown as markdown_lib

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_sanitize

from . import credential_manager
from ..services import llm_client

_logger = logging.getLogger(__name__)

#: Default model slug used when ``iris.llm_model`` is unset.
DEFAULT_LLM_MODEL = "anthropic/claude-sonnet-4.5"


class IrisLlmJobMixin(models.AbstractModel):
    _name = "iris.llm.job.mixin"
    _description = "Iris LLM Job Mixin"

    llm_status = fields.Selection(
        selection=[
            ("none", "None"),
            ("queued", "Queued"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("needs_review", "Needs Review"),
        ],
        string="LLM Status",
        default="none",
        copy=False,
        index=True,
    )
    llm_error = fields.Text(string="LLM Error", copy=False)
    llm_model_used = fields.Char(string="LLM Model Used", copy=False)
    llm_prompt_tokens = fields.Integer(string="Prompt Tokens", copy=False)
    llm_completion_tokens = fields.Integer(string="Completion Tokens", copy=False)
    llm_cost_usd = fields.Float(
        string="LLM Cost (USD)", digits=(12, 6), copy=False,
    )
    llm_latency_ms = fields.Integer(string="LLM Latency (ms)", copy=False)
    llm_started_at = fields.Datetime(string="LLM Started At", copy=False)
    llm_completed_at = fields.Datetime(string="LLM Completed At", copy=False)
    llm_raw_response = fields.Text(
        string="LLM Raw Response", copy=False,
        help="Full decoded API response body, persisted before any parsing.",
    )
    llm_prompt_input = fields.Text(
        string="LLM Prompt Input", copy=False,
        help="Audit copy of the user message sent to the LLM. The system "
             "prompt is reproducible from prompts/*.md + Settings overrides.",
    )

    # ------------------------------------------------------------------
    # Template methods (implemented by concrete models)
    # ------------------------------------------------------------------
    def _llm_build_messages(self):
        """Build the chat messages for this artifact.

        :return: tuple ``(system_prompt, user_text)``.
        """
        raise NotImplementedError(
            "%s must implement _llm_build_messages()" % self._name
        )

    def _llm_on_success(self, content, meta):
        """Persist LLM ``content`` and drive the candidate state machine.

        :param content: assistant message text (markdown).
        :param meta: full normalised result dict from ``llm_client``.
        """
        raise NotImplementedError(
            "%s must implement _llm_on_success()" % self._name
        )

    def _llm_on_failure(self, msg):
        """Deterministically revert the candidate state after a failure.

        :param msg: human-readable failure description.
        """
        raise NotImplementedError(
            "%s must implement _llm_on_failure()" % self._name
        )

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------
    def _llm_enqueue(self):
        """Queue this artifact for LLM processing.

        Raises a clean :class:`UserError` immediately when no API key is
        configured (so UI buttons / API calls fail fast instead of leaving
        the record stuck in ``queued``), then marks the record ``queued``
        and instant-triggers the queue cron (i2i pattern).
        """
        if not credential_manager.get_openrouter_api_key(self.env):
            raise UserError(_(
                "OpenRouter API key not configured. Set it in Settings → Iris."
            ))
        self.sudo().write({"llm_status": "queued", "llm_error": False})
        cron = self.env.ref(
            "iris.cron_process_llm_queue",
            raise_if_not_found=False,
        )
        if cron:
            cron.sudo()._trigger()

    # ------------------------------------------------------------------
    # Synchronous execution (called by the cron, one item at a time)
    # ------------------------------------------------------------------
    def _llm_run_sync(self):
        """Run the LLM chat completion for this record synchronously.

        Sets ``running`` + ``llm_started_at``, calls the provider-agnostic
        ``services.llm_client.chat_completion``, stores all ``llm_*``
        metadata, then delegates to :meth:`_llm_on_success`. Any exception
        from the call or the success callback writes ``failed`` +
        ``llm_error`` and delegates to :meth:`_llm_on_failure`.

        :return: True on success, False on a handled failure.
        """
        self.ensure_one()
        # Key check BEFORE flipping to running: raising here would roll the
        # cron's savepoint back and leave the record queued forever (retried
        # every 2 min). A key removed after enqueue routes to failed instead.
        api_key = credential_manager.get_openrouter_api_key(self.env)
        if not api_key:
            self._llm_mark_failed(
                "OpenRouter API key not configured. Set it in Settings → Iris."
            )
            return False

        self.sudo().write({
            "llm_status": "running",
            "llm_started_at": fields.Datetime.now(),
            "llm_error": False,
        })

        ICP = self.env["ir.config_parameter"].sudo()
        model = ICP.get_param("iris.llm_model", DEFAULT_LLM_MODEL)
        base_url = ICP.get_param(
            "iris.openrouter_base_url", llm_client.DEFAULT_BASE_URL,
        )
        try:
            timeout = int(ICP.get_param("iris.llm_timeout", "180"))
        except (TypeError, ValueError):
            timeout = 180
        try:
            max_retries = int(ICP.get_param("iris.openrouter_max_retries", "3"))
        except (TypeError, ValueError):
            max_retries = 3
        try:
            usd_per_mtoken = float(ICP.get_param("iris.usd_per_mtoken", "3.0"))
        except (TypeError, ValueError):
            usd_per_mtoken = 3.0

        try:
            system_prompt, user_text = self._llm_build_messages()
            self.sudo().write({"llm_prompt_input": user_text})
            result = llm_client.chat_completion(
                api_key,
                model=model,
                system_prompt=system_prompt,
                user_text=user_text,
                base_url=base_url,
                timeout=timeout,
                max_retries=max_retries,
            )
        except Exception as exc:  # noqa: BLE001 — any failure routes to failed
            _logger.exception("Iris LLM call failed for %s(%s)", self._name, self.ids)
            self._llm_mark_failed(str(exc))
            return False

        tokens = (result.get("prompt_tokens") or 0) + (result.get("completion_tokens") or 0)
        cost_usd = result.get("cost_usd")
        if cost_usd is None:
            cost_usd = (tokens / 1_000_000.0) * usd_per_mtoken if tokens else 0.0
        try:
            raw_text = json.dumps(result.get("raw") or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw_text = str(result.get("raw"))

        # Persist the raw response + metadata BEFORE any parsing happens in
        # the success callback, so a parse crash never loses the output.
        self.sudo().write({
            "llm_status": "done",
            "llm_error": False,
            "llm_model_used": result.get("model") or model,
            "llm_prompt_tokens": result.get("prompt_tokens") or 0,
            "llm_completion_tokens": result.get("completion_tokens") or 0,
            "llm_cost_usd": cost_usd,
            "llm_latency_ms": result.get("latency_ms") or 0,
            "llm_completed_at": fields.Datetime.now(),
            "llm_raw_response": raw_text,
        })

        try:
            self._llm_on_success(result["content"], result)
        except Exception as exc:  # noqa: BLE001 — callback failure routes to failed
            _logger.exception(
                "Iris LLM success callback failed for %s(%s)", self._name, self.ids,
            )
            self._llm_mark_failed(str(exc))
            return False
        return True

    def _llm_mark_failed(self, msg):
        """Write the failed state + error, then run the failure callback."""
        self.sudo().write({
            "llm_status": "failed",
            "llm_error": msg,
            "llm_completed_at": fields.Datetime.now(),
        })
        try:
            self._llm_on_failure(msg)
        except Exception:  # noqa: BLE001 — never mask the original failure
            _logger.exception(
                "Iris LLM failure callback crashed for %s(%s)", self._name, self.ids,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _markdown_to_html(self, text):
        """Render markdown ``text`` to sanitized HTML for form display."""
        if not text:
            return ""
        html = markdown_lib.markdown(
            text, extensions=["tables", "fenced_code"],
        )
        return html_sanitize(html)

    def _make_md_attachment(self, filename, markdown_text, res_model, res_id):
        """Create an ``ir.attachment`` holding ``markdown_text``.

        :param filename: attachment name, e.g. ``screening-doe-2026-06-11.md``.
        :param markdown_text: file content (str).
        :param res_model: model the attachment is linked to.
        :param res_id: record id the attachment is linked to.
        :return: the created ``ir.attachment`` record.
        """
        return self.env["ir.attachment"].sudo().create({
            "name": filename,
            "datas": base64.b64encode((markdown_text or "").encode("utf-8")),
            "mimetype": "text/markdown",
            "res_model": res_model,
            "res_id": res_id,
        })
