import base64
import json
import logging
import os

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AuroraImportDatasetWizard(models.TransientModel):
    _name = "aurora.import.dataset.wizard"
    _description = "Import Evaluation Dataset JSONL"

    evaluation_id = fields.Many2one(
        "aurora.evaluation",
        string="Evaluation",
        required=True,
        readonly=True,
    )
    jsonl_file = fields.Binary(string="JSONL File", required=True)
    jsonl_filename = fields.Char()

    def action_import(self):
        self.ensure_one()
        if not self.jsonl_file:
            raise UserError("Please upload a JSONL file.")

        filename = (self.jsonl_filename or "dataset.jsonl").strip()
        if not filename.lower().endswith(".jsonl"):
            raise UserError("Only .jsonl files are supported.")

        raw = base64.b64decode(self.jsonl_file)
        lines = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
        if not lines:
            raise UserError("The uploaded file is empty.")

        normalized: list = []
        synth_count = 0
        for idx, line in enumerate(lines, start=1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise UserError(
                    f"Invalid JSONL: line {idx} is not valid JSON: {exc}"
                ) from exc
            had_instance_id = bool(rec.get("instance_id"))
            from .import_dataset_normalize import normalize_record
            normalize_record(rec)
            if not had_instance_id and rec.get("instance_id"):
                synth_count += 1
            if not rec.get("instance_id"):
                raise UserError(
                    f"Line {idx}: cannot derive instance_id — record needs "
                    "either `instance_id`, `tag_start`+`tag_end`, "
                    "`base.label` (with '..' separator), OR `number`."
                )
            normalized.append(rec)

        ICP = self.env["ir.config_parameter"].sudo()
        base_dir = ICP.get_param("aurora.output_dir", "/tmp/aurora_output")
        cache_dir = os.path.join(base_dir, "dataset_cache")
        os.makedirs(cache_dir, exist_ok=True)

        safe_name = f"imported_{self.evaluation_id.id}_{filename}"
        local_path = os.path.join(cache_dir, safe_name)
        with open(local_path, "w", encoding="utf-8") as fh:
            for rec in normalized:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if synth_count:
            _logger.info(
                "Custom JSONL import: synthesized instance_id for %d of %d record(s)",
                synth_count, len(normalized),
            )

        from . import artifact_collector, s3_storage

        s3_config = artifact_collector.load_s3_config()
        s3_url = None
        run_number = None

        eval_rec = self.evaluation_id
        pl = eval_rec.pipeline_id
        if (
            s3_storage.is_configured(s3_config)
            and pl
            and pl.github_org
            and pl.github_repo
        ):
            org = pl.github_org
            repo = pl.github_repo
            folder = (s3_config.get("folder") or "").strip("/")
            phase = "aurora_phase2"
            run_number = s3_storage.get_next_run_number(
                s3_config, org, repo, folder=folder, phase=phase
            )
            s3_key = s3_storage.build_s3_key(
                org, repo, run_number, "dataset.jsonl", folder=folder, phase=phase
            )
            s3_url = s3_storage.upload_file(s3_config, local_path, s3_key)

        vals: dict = {"dataset_file": local_path}
        if run_number:
            vals["s3_run_number"] = run_number
        if s3_url:
            vals["dataset_jsonl_url"] = s3_url

        eval_rec.write(vals)

        note = f"Dataset imported from file: <b>{filename}</b>."
        if s3_url:
            note += f" Uploaded to S3: {s3_url}"
        eval_rec.message_post(
            body=note,
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

        return {
            "type": "ir.actions.act_window",
            "res_model": "aurora.evaluation",
            "res_id": eval_rec.id,
            "view_mode": "form",
            "target": "current",
        }
