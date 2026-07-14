# -*- coding: utf-8 -*-
"""Image attachments for image-evaluation questions; ``slot`` identifies each picture.

Storage: Odoo Binary, optionally offloaded to S3 (``image_url``, preferred when set).
"""
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
             "name, text, href, box_css, in_shadow, boxed_via_label) — the "
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
        """Project the canonical slot onto the per-type helper selectors."""
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
        """Default a new row's slot to one valid for its parent question type."""
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
        """Raw bytes of the source picture: the external image_url when set,
        else the stored binary. Returns b"" when neither is available."""
        self.ensure_one()
        if self.image_url:
            from ..services import image_ingest
            b64, _ctype = image_ingest._download(self.image_url)
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
        """Persist a DOM capture: the screenshot becomes the source image, the
        numbered overlay the candidate-facing annotated image, plus the manifest,
        the behavioural key, the geometry ``detections_json`` (so the portal
        renders the boxes and the cron skips this image), and a viewport stamp."""
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
        """Parse omit_spec_json to a directive dict for capture_and_annotate, or
        None when unset/blank/unparseable (so a bad spec never blocks capture)."""
        self.ensure_one()
        raw = (self.omit_spec_json or "").strip()
        if not raw:
            return None
        try:
            spec = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return spec if isinstance(spec, dict) and spec else None

    def _detect_and_annotate(self, ui=False):
        """Detect the elements in this image's source picture, draw the numbered
        overlay, and store ``annotated_image`` (+url) and the ``detections_json``
        answer key. Returns True on success, False when there is no source image.

        When a ``source_url`` is set AND Playwright/Chromium are available, the
        DOM-truth capture path runs instead (deterministic boxes + behavioural
        key); a DOM-capture failure falls back to the Gemini path below."""
        self.ensure_one()
        from ..services import vertex, imaging, image_ingest, dom_capture
        if self.source_url and dom_capture.PLAYWRIGHT_AVAILABLE:
            try:
                result = dom_capture.capture_and_annotate(
                    self.source_url, omit=self._omit_spec())
            except Exception:  # noqa: BLE001 - degrade to Gemini, never crash
                _logger.exception(
                    "DOM capture failed for image %s; falling back to Gemini",
                    self.id)
            else:
                self._store_capture(result)
                return True
        raw = self._source_image_bytes()
        if not raw:
            return False
        image_b64 = base64.b64encode(raw).decode()
        detections = vertex.detect_image_elements(
            self.env, image_b64, ui=ui,
            usage_ctx={"prompt_id": self.question_id.generator_id.id or False,
                       "note": (self.question_id.name or "")[:80]})
        annotated_png, label_key = imaging.annotate_image(raw, detections)
        annotated_b64 = base64.b64encode(annotated_png).decode()
        url, stored_b64 = image_ingest.ingest(
            self.env, None, "data:image/png;base64,%s" % annotated_b64,
            key_hint="annot-%s" % self.id)
        vals = {"detections_json": json.dumps(label_key, ensure_ascii=False)}
        if url:
            vals["annotated_image_url"] = url
        if stored_b64:
            vals["annotated_image"] = stored_b64
        self.write(vals)
        return True

    def _detection_ui(self):
        self.ensure_one()
        return self.question_id.detection_mode == "ui"

    def action_detect_now(self):
        """Detect + annotate this source image on demand (respecting the
        question's detection_mode), so an admin need not wait for the cron.
        Reuses the cron's per-image attempt cap and turns a Vertex failure into
        a friendly UserError instead of a traceback."""
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
        except Exception as exc:  # noqa: BLE001 - surface to the admin, cap tries
            _logger.exception("On-demand detect failed for image %s", self.id)
            self.detection_attempts += 1
            raise UserError("Detection failed: %s" % exc) from exc
        if not ok:
            self.detection_attempts += 1
            raise UserError("No source image to detect.")
        return True

    def action_capture_url(self):
        """Capture the live ``source_url`` DOM synchronously: numbered boxes at
        real element geometry + a deterministic behavioural key, with ZERO model
        inference. Raises a friendly UserError when no URL is set or when
        Playwright/Chromium are not installed (so the admin knows to fall back to
        Gemini detection or install the browser)."""
        self.ensure_one()
        from ..services import dom_capture
        if not self.source_url:
            raise UserError(
                "Set a Source URL before capturing the live DOM.")
        if not dom_capture.PLAYWRIGHT_AVAILABLE:
            raise UserError(
                "Install playwright + chromium: "
                "pip install playwright && playwright install chromium")
        try:
            result = dom_capture.capture_and_annotate(
                self.source_url, omit=self._omit_spec())
        except Exception as exc:  # noqa: BLE001 - surface to the admin
            _logger.exception("URL capture failed for image %s", self.id)
            raise UserError("URL capture failed: %s" % exc) from exc
        self._store_capture(result)
        return True

    @api.model
    def _cron_detect_image_labels(self):
        """Background drainer for image_label source images awaiting detection.
        Extends the image-processing cron path: once an image_label question's
        'single' image exists, run detection + numbered-box annotation ONCE per
        image (guard: detections_json still empty, a source image present). A
        SESSION-level advisory lock (survives the per-row commits) serializes
        drains so two workers never double-detect; per-row failures are isolated
        and logged like the render cron, and a per-image attempt counter flips a
        repeatedly-failing image out of the queue instead of retrying forever."""
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
                except Exception:  # noqa: BLE001 - isolate per image
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
