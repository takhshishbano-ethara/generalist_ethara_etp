import base64
import csv
import io

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.crowley_generation import CATEGORY_SELECTION

_VALID_DURATIONS = {"4", "5", "6", "7", "8", "9", "10", "12", "15"}
_VALID_RESOLUTIONS = {"480p", "720p", "1080p"}
_VALID_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
_VALID_STYLES = {"casual", "precise", "narrative", "terse", "exhaustive", "creative"}
_VALID_PRIORITIES = {"medium", "high", "highest"}
_VALID_COMPLEXITIES = {"simple", "moderate", "complex"}
_BOOL_TRUE = {"true", "1", "yes", "y", "t"}
_BOOL_FALSE = {"false", "0", "no", "n", "f"}
_MAX_PROMPT_LEN = 2000
_MAX_TOPIC_LEN = 200
_MAX_SUB_CATEGORY_LEN = 200
_MAX_LANGUAGE_LEN = 50
_MAX_DIALOGUE_TRANSCRIPT_LEN = 4000
_MAX_SEED = 2_147_483_647
_MAX_SPEAKER_COUNT = 16

_REQUIRED_COLUMNS = ("prompt", "category")
_OPTIONAL_COLUMNS = (
    "negative_prompt", "duration", "resolution",
    "aspect_ratio", "seed", "generate_audio",
    "sub_category", "style", "priority", "topic", "complexity",
    "language", "speaker_count", "dialogue_transcript",
)


class CrowleyImportWizard(models.TransientModel):
    _name = "crowley.import.wizard"
    _description = "Crowley CSV Import Wizard"

    csv_file = fields.Binary(string="CSV File", required=True)
    csv_filename = fields.Char(string="Filename")

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("Please upload a CSV file."))

        try:
            content = base64.b64decode(self.csv_file).decode("utf-8-sig")
        except Exception as exc:
            raise UserError(_(
                "Could not read the file. Ensure it is a valid UTF-8 CSV."
            )) from exc

        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            raise UserError(_("CSV is empty or has no header row."))

        header_map = {
            (name or "").strip().lower(): name
            for name in reader.fieldnames
        }
        missing_required = [c for c in _REQUIRED_COLUMNS if c not in header_map]
        if missing_required:
            raise UserError(_(
                "CSV is missing required column(s): %(cols)s. "
                "Required: prompt, category. "
                "Optional: negative_prompt, duration, resolution, "
                "aspect_ratio, seed, generate_audio, "
                "sub_category, style, priority, topic, complexity, "
                "language, speaker_count, dialogue_transcript."
            ) % {"cols": ", ".join(missing_required)})

        category_lookup = self._build_category_lookup()
        parsed_vals = []
        errors = []

        for row_num, row in enumerate(reader, start=2):
            row_errors, vals = self._validate_row(
                row_num, row, header_map, category_lookup,
            )
            if row_errors:
                errors.extend(row_errors)
            else:
                parsed_vals.append(vals)

        if not parsed_vals and not errors:
            raise UserError(_("CSV has no data rows."))

        if errors:
            preview = "\n".join(errors[:20])
            extra = (
                _("\n... and %d more error(s).") % (len(errors) - 20)
                if len(errors) > 20 else ""
            )
            raise UserError(_(
                "Import aborted. %(count)d row(s) failed validation. "
                "No records were created.\n\n%(preview)s%(extra)s"
            ) % {"count": len(errors), "preview": preview, "extra": extra})

        Job = self.env["crowley.generation"]
        created = Job.create(parsed_vals)

        return {
            "type": "ir.actions.act_window",
            "name": _("Imported Generations"),
            "res_model": "crowley.generation",
            "view_mode": "list,form",
            "domain": [("id", "in", created.ids)],
            "target": "current",
            "context": {"create": False},
        }

    def _build_category_lookup(self):
        lookup = {}
        for slug, label in CATEGORY_SELECTION:
            lookup[slug.lower()] = slug
            lookup[label.lower()] = slug
        return lookup

    def _validate_row(self, row_num, row, header_map, category_lookup):
        errors = []

        def get(col):
            src_name = header_map.get(col)
            if src_name is None:
                return ""
            return (row.get(src_name) or "").strip()

        prompt = get("prompt")
        if not prompt:
            errors.append(_("Row %d: prompt is required.") % row_num)
        elif len(prompt) > _MAX_PROMPT_LEN:
            errors.append(_(
                "Row %(row)d: prompt is %(len)d chars; max is %(max)d."
            ) % {"row": row_num, "len": len(prompt), "max": _MAX_PROMPT_LEN})

        category_raw = get("category")
        category_slug = None
        if not category_raw:
            errors.append(_("Row %d: category is required.") % row_num)
        else:
            category_slug = category_lookup.get(category_raw.lower())
            if not category_slug:
                valid = ", ".join(s for s, _l in CATEGORY_SELECTION)
                errors.append(_(
                    "Row %(row)d: category '%(val)s' is not valid. "
                    "Allowed: %(valid)s."
                ) % {"row": row_num, "val": category_raw, "valid": valid})

        vals = {
            "prompt": prompt,
            "category": category_slug,
            "source": "import",
            "user_id": self.env.user.id,
        }

        negative_prompt = get("negative_prompt")
        if negative_prompt:
            vals["negative_prompt"] = negative_prompt

        duration = get("duration")
        if duration:
            if duration not in _VALID_DURATIONS:
                errors.append(_(
                    "Row %(row)d: duration '%(val)s' invalid. "
                    "Allowed: %(valid)s."
                ) % {
                    "row": row_num, "val": duration,
                    "valid": ", ".join(sorted(_VALID_DURATIONS, key=int)),
                })
            else:
                vals["duration"] = duration

        resolution = get("resolution")
        if resolution:
            normalized = resolution.lower()
            if normalized not in _VALID_RESOLUTIONS:
                errors.append(_(
                    "Row %(row)d: resolution '%(val)s' invalid. "
                    "Allowed: 480p, 720p, 1080p."
                ) % {"row": row_num, "val": resolution})
            else:
                vals["resolution"] = normalized

        aspect_ratio = get("aspect_ratio")
        if aspect_ratio:
            if aspect_ratio not in _VALID_ASPECT_RATIOS:
                errors.append(_(
                    "Row %(row)d: aspect_ratio '%(val)s' invalid. "
                    "Allowed: %(valid)s."
                ) % {
                    "row": row_num, "val": aspect_ratio,
                    "valid": ", ".join(sorted(_VALID_ASPECT_RATIOS)),
                })
            else:
                vals["aspect_ratio"] = aspect_ratio

        seed = get("seed")
        if seed:
            try:
                seed_int = int(seed)
            except (TypeError, ValueError):
                errors.append(_(
                    "Row %(row)d: seed '%(val)s' is not an integer."
                ) % {"row": row_num, "val": seed})
            else:
                if seed_int < 0 or seed_int > _MAX_SEED:
                    errors.append(_(
                        "Row %(row)d: seed must be 0..2,147,483,647."
                    ) % {"row": row_num})
                else:
                    vals["seed"] = seed_int

        generate_audio = get("generate_audio")
        if generate_audio:
            normalized = generate_audio.lower()
            if normalized in _BOOL_TRUE:
                vals["generate_audio"] = True
            elif normalized in _BOOL_FALSE:
                vals["generate_audio"] = False
            else:
                errors.append(_(
                    "Row %(row)d: generate_audio '%(val)s' invalid. "
                    "Use true/false, yes/no, 1/0."
                ) % {"row": row_num, "val": generate_audio})

        style = get("style")
        if style:
            normalized = style.lower()
            if normalized not in _VALID_STYLES:
                errors.append(_(
                    "Row %(row)d: style '%(val)s' invalid. Allowed: %(valid)s."
                ) % {
                    "row": row_num, "val": style,
                    "valid": ", ".join(sorted(_VALID_STYLES)),
                })
            else:
                vals["style"] = normalized

        priority = get("priority")
        if priority:
            normalized = priority.lower()
            if normalized not in _VALID_PRIORITIES:
                errors.append(_(
                    "Row %(row)d: priority '%(val)s' invalid. Allowed: %(valid)s."
                ) % {
                    "row": row_num, "val": priority,
                    "valid": ", ".join(sorted(_VALID_PRIORITIES)),
                })
            else:
                vals["priority"] = normalized

        complexity = get("complexity")
        if complexity:
            normalized = complexity.lower()
            if normalized not in _VALID_COMPLEXITIES:
                errors.append(_(
                    "Row %(row)d: complexity '%(val)s' invalid. Allowed: %(valid)s."
                ) % {
                    "row": row_num, "val": complexity,
                    "valid": ", ".join(sorted(_VALID_COMPLEXITIES)),
                })
            else:
                vals["complexity"] = normalized

        topic = get("topic")
        if topic:
            if len(topic) > _MAX_TOPIC_LEN:
                errors.append(_(
                    "Row %(row)d: topic is %(len)d chars; max is %(max)d."
                ) % {"row": row_num, "len": len(topic), "max": _MAX_TOPIC_LEN})
            else:
                vals["topic"] = topic

        sub_category = get("sub_category")
        if sub_category:
            if len(sub_category) > _MAX_SUB_CATEGORY_LEN:
                errors.append(_(
                    "Row %(row)d: sub_category is %(len)d chars; max is %(max)d."
                ) % {"row": row_num, "len": len(sub_category), "max": _MAX_SUB_CATEGORY_LEN})
            else:
                vals["sub_category"] = sub_category

        language = get("language")
        if language:
            if len(language) > _MAX_LANGUAGE_LEN:
                errors.append(_(
                    "Row %(row)d: language is %(len)d chars; max is %(max)d."
                ) % {"row": row_num, "len": len(language), "max": _MAX_LANGUAGE_LEN})
            else:
                vals["language"] = language

        speaker_count = get("speaker_count")
        if speaker_count:
            try:
                speaker_count_int = int(speaker_count)
            except (TypeError, ValueError):
                errors.append(_(
                    "Row %(row)d: speaker_count '%(val)s' is not an integer."
                ) % {"row": row_num, "val": speaker_count})
            else:
                if speaker_count_int < 0 or speaker_count_int > _MAX_SPEAKER_COUNT:
                    errors.append(_(
                        "Row %(row)d: speaker_count must be 0..%(max)d."
                    ) % {"row": row_num, "max": _MAX_SPEAKER_COUNT})
                else:
                    vals["speaker_count"] = speaker_count_int

        dialogue_transcript = get("dialogue_transcript")
        if dialogue_transcript:
            if len(dialogue_transcript) > _MAX_DIALOGUE_TRANSCRIPT_LEN:
                errors.append(_(
                    "Row %(row)d: dialogue_transcript is %(len)d chars; max is %(max)d."
                ) % {
                    "row": row_num, "len": len(dialogue_transcript),
                    "max": _MAX_DIALOGUE_TRANSCRIPT_LEN,
                })
            else:
                vals["dialogue_transcript"] = dialogue_transcript

        return errors, vals
