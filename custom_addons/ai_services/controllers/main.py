import json
import logging

from odoo import http
from odoo.http import request

from ..services.bedrock_service import call_bedrock_extract, build_extraction_response
from ..services.config import get_bedrock_config

_logger = logging.getLogger(__name__)


class AIServicesController(http.Controller):
    @http.route(
        "/api/v1/ai/extract",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"],
        csrf=False,
        cors="*",
    )
    def extract_documents(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return http.Response(status=204)

        try:
            cfg = get_bedrock_config()
            if not cfg["api_key"] or not cfg["inference_arn"]:
                return self._json_error(
                    "Bedrock API is not configured. Set AI_SERVICES_BEDROCK_API_KEY and AI_SERVICES_BEDROCK_INFERENCE_ARN in .env",
                    status=503,
                )

            files = request.httprequest.files.getlist("files")
            if not files:
                single_file = request.httprequest.files.get("file")
                if single_file:
                    files = [single_file]

            if not files:
                return self._json_error(
                    "No files uploaded. Send files with key 'files' or 'file'.",
                    status=400,
                )

            documents = []
            filenames = []
            for f in files:
                file_bytes = f.read()
                if file_bytes:
                    documents.append((f.filename, file_bytes))
                    filenames.append(f.filename)

            if not documents:
                return self._json_error("All uploaded files were empty.", status=422)

            project_details = call_bedrock_extract(
                documents=documents,
                api_key=cfg["api_key"],
                inference_profile_arn=cfg["inference_arn"],
                region=cfg["region"],
            )

            combined_filename = ", ".join(filenames)
            response_data = build_extraction_response(
                combined_filename, project_details
            )

            return http.Response(
                response_data.model_dump_json(indent=2),
                content_type="application/json",
                status=200,
            )

        except (ConnectionError, ValueError, RuntimeError) as e:
            _logger.error("Extraction API error: %s", e)
            return self._json_error(str(e), status=500)
        except Exception as e:
            _logger.exception("Unexpected extraction API error")
            return self._json_error(f"Internal server error: {e}", status=500)

    @staticmethod
    def _json_error(message, status=400):
        return http.Response(
            json.dumps({"success": False, "error": message}),
            content_type="application/json",
            status=status,
        )
