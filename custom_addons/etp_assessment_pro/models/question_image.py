# -*- coding: utf-8 -*-
import base64
import datetime
import json
import logging

from odoo import api, models, fields
from odoo.exceptions import UserError

from ..constants import ADVISORY_LOCK_IMAGE_DETECT

_logger = logging.getLogger(__name__)

_DETECT_MAX_ATTEMPTS = 3


class EtpAssessmentQuestionImage(models.Model):
    _name = "etp.assessment.pro.question.image"
    _description = "Assessment Question Image"
    _order = "sequence, id"

    question_id = fields.Many2one(
        "etp.assessment.pro.question",
        string="Question",
        required=True,
        ondelete="cascade",
    )
    question_type = fields.Selection(
        related="question_id.question_type",
        store=True,
        help="Mirror of the parent question type; gates which Slot selector "
             "is shown in the Image Evaluation list.")
    label = fields.Char(
        string="Label", required=True,
        help="Shown to the candidate, e.g. 'Response A', 'Reference', 'Output'.")
    slot = fields.Selection(
        [
            ("a", "Response A"),
            ("b", "Response B"),
            ("single", "Single"),
            ("reference", "Reference"),
            ("output", "Output"),
        ],
        string="Slot",
        required=True,
        default="single",
        help="image_ab uses A + B; image_prompt and image_label use Single / "
             "Reference / Output.",
    )
    slot_ab = fields.Selection(
        [("a", "Response A"), ("b", "Response B")],
        string="Slot",
        compute="_compute_slot_helpers",
        inverse="_inverse_slot_ab",
        help="image_ab slot selector; synced to the canonical slot field.")
    slot_text = fields.Selection(
        [("single", "Single"), ("reference", "Reference"), ("output", "Output")],
        string="Slot",
        compute="_compute_slot_helpers",
        inverse="_inverse_slot_text",
        help="image_prompt/image_label slot selector; synced to the canonical "
             "slot field.")
    image = fields.Binary(string="Image", attachment=True)
    image_url = fields.Char(
        string="Image URL",
        help="Optional external/S3 URL. When set, the portal serves this "
             "instead of the stored binary.")
    annotated_image = fields.Binary(
        string="Annotated Image", attachment=True,
        help="image_label: the source image with numbered red boxes drawn over "
             "each detected element (candidate-facing overlay).")
    annotated_image_url = fields.Char(
        string="Annotated Image URL",
        help="Optional external/S3 URL for the annotated overlay, preferred "
             "over the stored binary when set (mirrors image / image_url).")
    detections_json = fields.Text(
        string="Detections (JSON)", copy=False,
        help="image_label answer key: the label_key produced by detection, a "
             'JSON list of {"number","label","description","box_px"} entries '
             "keyed to the numbered boxes on the annotated image.")
    detection_attempts = fields.Integer(
        default=0, copy=False,
        help="How many times the detection cron has tried this image, so a "
             "repeatedly-failing detection flips out of the queue instead of "
             "retrying forever.")
    source_url = fields.Char(
        string="Source URL", copy=False,
        help="image_label: a LIVE web page URL. When set (and Playwright + "
             "Chromium are installed), detection captures the page DOM to draw "
             "boxes and draft a behavioural key deterministically instead of "
             "asking Gemini; otherwise the Gemini detection path runs.")
    dom_manifest_json = fields.Text(
        string="DOM Manifest (JSON)", copy=False,
        help="Per-box construction facts captured from the live DOM (tag, role, "
             "name, text, href, box_css, in_shadow, boxed_via_label) - the "
             "ground truth the behavioural key was drafted from.")
    behavioural_key_json = fields.Text(
        string="Behavioural Key (JSON)", copy=False,
        help="image_label answer key drafted from the DOM: a JSON list of "
             '{"number","element","functionality"} grading the ACTION each '
             "numbered element performs, not its nominal name.")
    capture_viewport = fields.Char(
        string="Capture Viewport", copy=False,
        help="Viewport + device scale factor and the UTC capture timestamp "
             "(e.g. '1440x900@2x 2026-07-13T10:20:30Z') for staleness.")
    capture_config_json = fields.Text(
        string="Capture Config (JSON)", copy=False,
        help='image_label DOM-capture directives {"viewport":{"width","height"},'
             '"wait_ms":int,"dismiss":[selectors]} threaded into '
             "capture_and_annotate so cookie/consent overlays are dismissed and "
             "the right viewport is used before the live DOM is enumerated.")
    capture_config_json = fields.Text(
        string="Capture Config (JSON)", copy=False,
        help='image_label DOM-capture directives {"viewport":{"width","height"},'
             '"wait_ms":int,"dismiss":["<accept selector>"]} threaded into the '
             "live capture so the settle delay and cookie/consent dismissal run "
             "before the DOM is enumerated.")
    omit_spec_json = fields.Text(
        string="Omit Spec (JSON)", copy=False,
        help="image_label: an optional capture directive "
             '{"match_tag","match_type","match_text"} that leaves ONE matching '
             "interactive element deliberately unboxed, so the coverage gate "
             'answer is "No" by construction. The omitted element is recorded in '
             "omitted_element_json.")
    coverage_expected = fields.Selection(
        [("yes", "Yes"), ("no", "No")],
        string="Coverage Expected", copy=False,
        help='The correct answer to "is every interactive element on the page '
             'boxed?": "no" when a DOM capture deliberately omitted an element '
             '(omit_spec_json), else "yes". Consumed by scoring as the coverage '
             "gate ground truth.")
    omitted_element_json = fields.Text(
        string="Omitted Element (JSON)", copy=False,
        help="Ground truth for a deliberately-omitted interactive element "
             "(tag/type/role/aria/text/href/box_css/reason) recorded at capture "
             'time so the coverage gate answer is provably "No".')
    label_application = fields.Char(
        string="Label Application", copy=False,
        help="image_label DENSE: the app/site the screenshot depicts. Graded as "
             "one identification checklist point in the behavioural rubric.")
    label_boxes_json = fields.Text(
        string="Label Boxes (JSON)", copy=False,
        help="image_label DENSE fallback geometry: the model-authored per-box map "
             '[{number, box_2d, label, description}] carried from the draft. When '
             "the real-page DOM capture is unavailable / fails / yields zero "
             "boxes, the synthetic fallback draws the numbered boxes from this map "
             "deterministically via imaging.annotate_image with ZERO Vertex "
             "detect calls, so the fallback screenshot is always labelled.")
    sequence = fields.Integer(default=10)
    image_admin_url = fields.Char(compute="_compute_admin_urls")
    annotated_admin_url = fields.Char(compute="_compute_admin_urls")

    @api.depends("image", "image_url", "annotated_image", "annotated_image_url",
                 "write_date")
    def _compute_admin_urls(self):
        for rec in self:
            base = "/etp_assessment/admin_qimage/%s" % rec.id if rec.id else False
            ver = int(rec.write_date.timestamp()) if rec.id and rec.write_date else 0
            rec.image_admin_url = (
                "%s?v=%s" % (base, ver)
                if base and (rec.image or rec.image_url) else False)
            rec.annotated_admin_url = (
                "%s?annotated=1&v=%s" % (base, ver)
                if base and (rec.annotated_image or rec.annotated_image_url)
                else False)

    @api.depends("slot")
    def _compute_slot_helpers(self):
        for rec in self:
            rec.slot_ab = rec.slot if rec.slot in ("a", "b") else False
            rec.slot_text = (
                rec.slot if rec.slot in ("single", "reference", "output")
                else False)

    def _inverse_slot_ab(self):
        for rec in self:
            if rec.slot_ab:
                rec.slot = rec.slot_ab

    def _inverse_slot_text(self):
        for rec in self:
            if rec.slot_text:
                rec.slot = rec.slot_text

    @api.onchange("question_type")
    def _onchange_question_type_slot_default(self):
        for rec in self:
            if rec.question_type == "image_ab" and rec.slot not in ("a", "b"):
                rec.slot = "a"
            elif (rec.question_type == "image_prompt"
                  and rec.slot not in ("single", "reference", "output")):
                rec.slot = "reference"
            elif (rec.question_type == "image_label"
                  and rec.slot not in ("single", "reference", "output")):
                rec.slot = "single"

    def _source_image_bytes(self):
        self.ensure_one()
        if self.image_url:
            from ..services import image_ingest
            b64, _ctype = image_ingest.download_bytes(self.env, self.image_url)
            if b64:
                return base64.b64decode(b64)
        if self.image:
            data = self.image
            if isinstance(data, str):
                data = data.encode("ascii", errors="ignore")
            try:
                return base64.b64decode(data)
            except (ValueError, TypeError):
                return b""
        return b""

    def _store_capture(self, result, viewport=(1600, 1000), dsf=2):
        # detections_json must be written here: a non-empty value is the cron's
        # skip guard for this image.
        self.ensure_one()
        from ..services import image_ingest, dom_capture
        screenshot_b64 = base64.b64encode(result["screenshot_png"]).decode()
        annotated_b64 = base64.b64encode(result["annotated_png"]).decode()
        url, stored_b64 = image_ingest.ingest(
            self.env, None, "data:image/png;base64,%s" % annotated_b64,
            key_hint="domannot-%s" % self.id)
        vals = {
            "image": screenshot_b64,
            "dom_manifest_json": json.dumps(
                result.get("dom_manifest") or [], ensure_ascii=False),
            "behavioural_key_json": json.dumps(
                result.get("behavioural_key") or [], ensure_ascii=False),
            "detections_json": json.dumps(
                result.get("label_key") or [], ensure_ascii=False),
            "capture_viewport": dom_capture.viewport_stamp(viewport, dsf),
            "annotated_image": stored_b64 or annotated_b64,
            "coverage_expected": result.get("coverage_expected") or "yes",
            "omitted_element_json": json.dumps(
                result.get("omitted_element"), ensure_ascii=False)
            if result.get("omitted_element") else False,
        }
        if url:
            vals["annotated_image_url"] = url
        self.write(vals)

    def _omit_spec(self):
        self.ensure_one()
        raw = (self.omit_spec_json or "").strip()
        if not raw:
            return None
        try:
            spec = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return spec if isinstance(spec, dict) and spec else None

    def _capture_config(self):
        self.ensure_one()
        raw = (self.capture_config_json or "").strip()
        if not raw:
            return {}
        try:
            cfg = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def _capture_kwargs(self):
        self.ensure_one()
        kwargs: dict = {"omit": self._omit_spec()}
        cfg = self._capture_config()
        vp = cfg.get("viewport")
        if isinstance(vp, dict):
            try:
                w, h = int(vp.get("width") or 0), int(vp.get("height") or 0)
            except (TypeError, ValueError):
                w = h = 0
            if w > 0 and h > 0:
                kwargs["viewport"] = (w, h)
        if isinstance(cfg.get("dismiss"), list):
            sels = [str(s) for s in cfg["dismiss"] if str(s).strip()]
            if sels:
                kwargs["dismiss"] = sels
        if cfg.get("wait_ms") is not None:
            try:
                kwargs["wait_ms"] = int(cfg["wait_ms"])
            except (TypeError, ValueError):
                pass
        return kwargs

    def _detect_and_annotate(self, ui=False):
        self.ensure_one()
        from ..services import dom_capture
        if self.source_url and dom_capture.PLAYWRIGHT_AVAILABLE:
            kwargs = self._capture_kwargs()
            try:
                result = dom_capture.capture_and_annotate(
                    self.source_url, **kwargs)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "DOM capture failed for image %s; falling back to the "
                    "synthetic render+detect path", self.id)
            else:
                if result.get("dom_manifest"):
                    self._store_capture(
                        result, viewport=kwargs.get("viewport", (1600, 1000)))
                    return True
                _logger.warning(
                    "DOM capture for image %s produced zero boxes; falling back "
                    "to the synthetic render+detect path", self.id)
        # DETECT-AFTER-RENDER (matches research renderers/ui.py 1:1). We do NOT
        # draw the generator's guessed `boxes` (the old _annotate_from_dense_map
        # short-circuit): those coordinates are authored by the TEXT model before
        # the screenshot exists, so they never align with what the IMAGE model
        # actually rendered - the cause of "labels at the wrong positions". The
        # only trustworthy boxes are (a) real DOM rects from a live page (handled
        # above) or (b) vision-detected controls in the ACTUAL rendered pixels
        # (below). One detection call per image is the correct cost/accuracy
        # trade - wrong boxes are worse than a saved quota call on an assessment.
        return self._annotate_from_bytes(self._source_image_bytes(), ui=ui)

    def _dense_detections(self):
        self.ensure_one()
        raw = (self.label_boxes_json or "").strip()
        if not raw:
            return []
        try:
            geometry = json.loads(raw)
        except (ValueError, TypeError):
            return []
        dets = []
        for g in geometry if isinstance(geometry, list) else []:
            if (isinstance(g, dict)
                    and isinstance(g.get("box_2d"), (list, tuple))
                    and len(g["box_2d"]) == 4):
                dets.append({
                    "box_2d": list(g["box_2d"]),
                    "label": str(g.get("label") or "").strip(),
                    "description": str(g.get("description") or "").strip(),
                })
        return dets

    def _annotate_from_dense_map(self):
        """Must stay Vertex-free: this fallback draws boxes from the stored map
        only, and runs ahead of detect to avoid burning image quota."""
        self.ensure_one()
        dets = self._dense_detections()
        if not dets:
            return False
        raw = self._source_image_bytes()
        if not raw:
            return False
        from ..services import imaging, image_ingest
        annotated_png, label_key = imaging.annotate_image(raw, dets)
        annotated_b64 = base64.b64encode(annotated_png).decode()
        url, stored_b64 = image_ingest.ingest(
            self.env, None, "data:image/png;base64,%s" % annotated_b64,
            key_hint="labeldense-%s" % self.id)
        vals = {
            "detections_json": json.dumps(label_key, ensure_ascii=False),
            "annotated_image": stored_b64 or annotated_b64,
        }
        if url:
            vals["annotated_image_url"] = url
        self.write(vals)
        return True

    @api.model
    def _annotate_bytes_core(self, raw, ui=False, usage_ctx=None,
                             key_hint="annot"):
        """Writes no record; return shape must stay in sync with the other
        caller, prompt._detect_label_on_render."""
        if not raw:
            return None
        from ..services import vertex, imaging, image_ingest
        image_b64 = base64.b64encode(raw).decode()
        detections = vertex.detect_image_elements(
            self.env, image_b64, ui=ui, usage_ctx=usage_ctx)
        annotated_png, label_key = imaging.annotate_image(raw, detections)
        annotated_b64 = base64.b64encode(annotated_png).decode()
        url, stored_b64 = image_ingest.ingest(
            self.env, None, "data:image/png;base64,%s" % annotated_b64,
            key_hint=key_hint)
        return {
            "detections_json": json.dumps(label_key, ensure_ascii=False),
            "annotated_b64": annotated_b64,
            "stored_b64": stored_b64 or "",
            "annotated_url": url or "",
        }

    def _annotate_from_bytes(self, raw, ui=False):
        self.ensure_one()
        core = self._annotate_bytes_core(
            raw, ui=ui,
            usage_ctx={"prompt_id": self.question_id.generator_id.id or False,
                       "note": (self.question_id.name or "")[:80]},
            key_hint="annot-%s" % self.id)
        if not core:
            return False
        vals = {"detections_json": core["detections_json"]}
        if core["annotated_url"]:
            vals["annotated_image_url"] = core["annotated_url"]
        if core["stored_b64"]:
            vals["annotated_image"] = core["stored_b64"]
        self.write(vals)
        return True

    def _detect_inline(self, raw, ui=False):
        """Takes bytes because a storage re-read fails on a DB copied without its
        filestore. Must swallow errors inside a savepoint (never fail the render)
        and must not touch detection_attempts: the cron retry budget stays intact.
        """
        self.ensure_one()
        if not raw:
            return False
        try:
            with self.env.cr.savepoint():
                return self._annotate_from_bytes(raw, ui=ui)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Inline detect failed for question image %s; leaving it for the "
                "detect cron to retry from storage", self.id)
            return False

    def _detection_ui(self):
        self.ensure_one()
        return self.question_id.detection_mode == "ui"

    def action_detect_now(self):
        self.ensure_one()
        if self.question_type != "image_label" or self.slot != "single":
            raise UserError(
                "Detection only applies to an Image - Labelling Single image.")
        if not (self.image or self.image_url):
            raise UserError("Upload a source image before detecting.")
        if self.detection_attempts >= _DETECT_MAX_ATTEMPTS:
            raise UserError(
                "This image has already failed detection %d times; fix the "
                "source image, then clear its attempts to retry."
                % _DETECT_MAX_ATTEMPTS)
        try:
            ok = self._detect_and_annotate(ui=self._detection_ui())
        except Exception as exc:  # noqa: BLE001
            _logger.exception("On-demand detect failed for image %s", self.id)
            self.detection_attempts += 1
            raise UserError("Detection failed: %s" % exc) from exc
        if not ok:
            self.detection_attempts += 1
            raise UserError("No source image to detect.")
        return True

    def action_capture_url(self):
        self.ensure_one()
        from ..services import dom_capture
        if not self.source_url:
            raise UserError(
                "Set a Source URL before capturing the live DOM.")
        if not dom_capture.PLAYWRIGHT_AVAILABLE:
            raise UserError(
                "Install playwright + chromium: "
                "pip install playwright && playwright install chromium")
        kwargs = self._capture_kwargs()
        try:
            result = dom_capture.capture_and_annotate(self.source_url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("URL capture failed for image %s", self.id)
            raise UserError("URL capture failed: %s" % exc) from exc
        self._store_capture(
            result, viewport=kwargs.get("viewport", (1600, 1000)))
        return True

    @api.model
    def _cron_detect_image_labels(self):
        # The lock must stay SESSION-level (pg_try_advisory_lock): a txn-level
        # lock would be released by the per-row commits below and let a second
        # worker double-detect.
        self.env.cr.execute(
            "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_IMAGE_DETECT,))
        if not self.env.cr.fetchone()[0]:
            return
        try:
            images = self.search([
                ("question_type", "=", "image_label"),
                ("slot", "=", "single"),
                ("detections_json", "in", (False, "")),
                ("detection_attempts", "<", _DETECT_MAX_ATTEMPTS),
                "|", "|", ("image", "!=", False), ("image_url", "!=", False),
                ("source_url", "!=", False),
            ], limit=2)
            if not images:
                return
            _logger.info(
                "etp_assessment detect cron: %d image_label image(s) to detect",
                len(images))
            done = 0
            for image in images:
                try:
                    with self.env.cr.savepoint():
                        if image._detect_and_annotate(ui=image._detection_ui()):
                            done += 1
                        else:
                            image.detection_attempts += 1
                    self.env.cr.commit()
                except Exception:  # noqa: BLE001
                    self.env.cr.rollback()
                    _logger.exception(
                        "Auto-detect failed for question image %s", image.id)
                    image.detection_attempts += 1
                    self.env.cr.commit()
                    continue
            _logger.info(
                "etp_assessment detect cron: annotated %d/%d image(s)",
                done, len(images))
        finally:
            self.env.cr.execute(
                "SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_IMAGE_DETECT,))
