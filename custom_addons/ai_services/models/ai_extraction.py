import base64
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.bedrock_service import call_bedrock_extract
from ..services.config import get_bedrock_config

_logger = logging.getLogger(__name__)


class AIDocumentExtraction(models.Model):
    _name = "ai.document.extraction"
    _description = "AI Document Extraction"
    _order = "create_date desc"
    _rec_name = "name"

    # ── Core Fields ───────────────────────────────────────────────────────
    name = fields.Char(
        string="Reference",
        default=lambda self: (
            self.env["ir.sequence"].next_by_code("ai.document.extraction") or "New"
        ),
        readonly=True,
        copy=False,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Status",
        default="draft",
        readonly=True,
        copy=False,
    )

    # ── Input: Documents ──────────────────────────────────────────────────
    document_ids = fields.Many2many(
        "ir.attachment",
        "ai_extraction_attachment_rel",
        "extraction_id",
        "attachment_id",
        string="Documents",
        required=True,
        help="Upload PDF, DOCX, TXT, XLSX or CSV files to extract project details from.",
    )

    # ── Extracted Fields (matching ProjectDetails schema) ─────────────────
    client_project_name = fields.Char(
        string="Client Project Name",
        readonly=True,
    )
    internal_project_name = fields.Char(
        string="Internal Project Name",
        readonly=True,
    )
    client_name = fields.Char(
        string="Client Name",
        readonly=True,
    )
    internal_client_name = fields.Char(
        string="Internal Client Name",
        readonly=True,
    )
    project_category = fields.Selection(
        [
            ("stem", "STEM"),
            ("non_stem", "Non-STEM"),
            ("technical", "Technical"),
        ],
        string="Project Category",
        readonly=True,
    )
    project_type = fields.Selection(
        [
            ("single_turn", "Single Turn"),
            ("multi_turn", "Multi Turn"),
        ],
        string="Project Type",
        readonly=True,
    )
    start_date = fields.Date(
        string="Start Date",
        readonly=True,
    )
    end_date = fields.Date(
        string="End Date",
        readonly=True,
    )
    sample_task_number = fields.Integer(
        string="Sample Task Number",
        readonly=True,
    )
    description = fields.Text(
        string="Description",
        readonly=True,
    )

    # ── Metadata ──────────────────────────────────────────────────────────
    extracted_at = fields.Datetime(
        string="Extracted At",
        readonly=True,
    )
    raw_response = fields.Text(
        string="Raw LLM Response",
        readonly=True,
    )
    extracted_text = fields.Text(
        string="Extracted Document Text",
        readonly=True,
    )
    error_message = fields.Text(
        string="Error Message",
        readonly=True,
    )

    # ── Actions ───────────────────────────────────────────────────────────

    def action_extract(self):
        """Extract project details from uploaded documents using Bedrock LLM."""
        self.ensure_one()

        if not self.document_ids:
            raise UserError("Please upload at least one document before extracting.")

        cfg = get_bedrock_config()

        if not cfg["api_key"]:
            raise UserError(
                "Bedrock API Key is not configured. "
                "Set AI_SERVICES_BEDROCK_API_KEY in .env"
            )
        if not cfg["inference_arn"]:
            raise UserError(
                "Bedrock Inference Profile ARN is not configured. "
                "Set AI_SERVICES_BEDROCK_INFERENCE_ARN in .env"
            )

        self.write({"state": "processing", "error_message": False})

        try:
            documents = []
            for attachment in self.document_ids:
                if attachment.datas:
                    file_bytes = base64.b64decode(attachment.datas)
                    documents.append((attachment.name, file_bytes))

            if not documents:
                raise UserError("No readable content in the uploaded documents.")

            _logger.info(
                "Sending %d document(s) to Bedrock for extraction %s",
                len(documents),
                self.name,
            )

            project_details = call_bedrock_extract(
                documents=documents,
                api_key=cfg["api_key"],
                inference_profile_arn=cfg["inference_arn"],
                region=cfg["region"],
            )

            vals = self._map_extraction_result(project_details)
            vals.update(
                {
                    "state": "done",
                    "extracted_at": fields.Datetime.now(),
                    "raw_response": project_details.model_dump_json(indent=2),
                    "extracted_text": False,
                    "error_message": False,
                }
            )
            self.write(vals)

            _logger.info(
                "Extraction %s completed successfully: %s",
                self.name,
                project_details.client_project_name,
            )

        except (ConnectionError, ValueError, RuntimeError) as e:
            self.write(
                {
                    "state": "error",
                    "error_message": str(e),
                }
            )
            _logger.error("Extraction %s failed: %s", self.name, e)
            raise UserError(f"Extraction failed: {e}")
        except Exception as e:
            self.write(
                {
                    "state": "error",
                    "error_message": str(e),
                }
            )
            _logger.exception("Unexpected error during extraction %s", self.name)
            raise UserError(f"Unexpected error: {e}")

    def _map_extraction_result(self, details):
        category_raw = (
            details.project_category.value if details.project_category else ""
        ).lower()
        category_map = {
            "stem": "stem",
            "non-stem": "non_stem",
            "non stem": "non_stem",
            "nonstem": "non_stem",
            "technical": "technical",
        }
        project_category = category_map.get(category_raw)

        ptype_raw = (details.project_type.value if details.project_type else "").lower()
        if "single" in ptype_raw:
            project_type = "single_turn"
        elif "multi" in ptype_raw:
            project_type = "multi_turn"
        else:
            project_type = False

        start_date = details.start_date or False
        end_date = details.end_date or False

        return {
            "client_project_name": details.client_project_name or "",
            "internal_project_name": details.internal_project_name or "",
            "client_name": details.client_name or "",
            "internal_client_name": details.internal_client_name or "",
            "project_category": project_category,
            "project_type": project_type,
            "start_date": start_date,
            "end_date": end_date,
            "sample_task_number": details.sample_task_number or 0,
            "description": details.description or "",
        }

    def action_reset_draft(self):
        """Reset to draft state so the user can re-upload or re-extract."""
        self.ensure_one()
        self.write(
            {
                "state": "draft",
                "error_message": False,
            }
        )

    def action_view_raw_response(self):
        """Open a popup showing the raw LLM response."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Raw LLM Response",
            "res_model": "ai.document.extraction",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": {"show_raw_response": True},
        }
