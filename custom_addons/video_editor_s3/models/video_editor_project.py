# -*- coding: utf-8 -*-
import logging
import os
import re
import shutil
from urllib.parse import parse_qs, urlparse

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services import llm_qc, youtube_downloader

_logger = logging.getLogger(__name__)

_HHMMSSMS_RE = re.compile(r"^\d{1,2}(:\d{1,3}){0,3}$")
_YT_T_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?$")


def _parse_hhmmssms_to_seconds(value):
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s:
        return 0.0
    try:
        return max(float(s), 0.0)
    except ValueError:
        pass
    if not _HHMMSSMS_RE.match(s):
        raise ValueError(_("Invalid time format: %r. Use HH:MM:SS:MS, HH:MM:SS, MM:SS or plain seconds.") % value)
    parts = s.split(":")
    nums = [int(p) for p in parts]
    if len(nums) == 1:
        return float(nums[0])
    if len(nums) == 2:
        return float(nums[0] * 60 + nums[1])
    if len(nums) == 3:
        return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    h, m, sec, ms = nums
    return float(h * 3600 + m * 60 + sec) + (ms / 1000.0)


def _seconds_to_hhmmssms(seconds):
    try:
        total = max(float(seconds or 0.0), 0.0)
    except (TypeError, ValueError):
        return ""
    if total <= 0.0:
        return ""
    ms = int(round((total - int(total)) * 1000))
    if ms >= 1000:
        total += 1
        ms = 0
    whole = int(total)
    h = whole // 3600
    m = (whole % 3600) // 60
    sec = whole % 60
    return "%02d:%02d:%02d:%03d" % (h, m, sec, ms)


def _parse_yt_t_param(value):
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    try:
        return max(float(s), 0.0)
    except ValueError:
        pass
    m = _YT_T_RE.match(s)
    if not m or not any(m.groups()):
        return None
    h, mn, se = m.groups()
    return float(int(h or 0) * 3600 + int(mn or 0) * 60) + float(se or 0)

PROJECT_STATES = [
    ("draft", "Draft"),
    ("processing", "Processing"),
    ("processed", "Processed"),
    ("exporting", "Exporting"),
    ("exported", "Done"),
    ("error", "Error"),
]

CATEGORIES = [
    ("animals_wildlife", "Animals & Wildlife"),
    ("animated_styles", "Animated Styles"),
    ("animated_text", "Animated Text"),
    ("av_sync_sound_effects", "AV-Sync Sound Effects"),
    ("camera_motion", "Camera Motion"),
    ("explainer_educational", "Explainer & Educational"),
    ("fantasy_surreal", "Fantasy & Surreal"),
    ("fine_grained_motion", "Fine-Grained Motion"),
    ("high_motion_action", "High-Motion Action"),
    ("human_activities", "Human Activities"),
    ("multi_speaker_dialogue", "Multi-Speaker Dialogue"),
    ("music_performance", "Music Performance"),
    ("narrative_cinematic", "Narrative & Cinematic"),
    ("natural_patterns", "Natural Patterns"),
    ("nature_weather", "Nature & Weather"),
    ("person_emoting", "Person Emoting"),
    ("speech_styles", "Speech Styles"),
    ("urban_scenes", "Urban Scenes"),
    ("vehicles_machines", "Vehicles & Machines"),
]

SUB_CATEGORIES_BY_CATEGORY = {
    "animals_wildlife": [
        ("birds_song", "Birds & Song"),
        ("domestic_pets", "Domestic Pets"),
        ("insects_small", "Insects & Small Creatures"),
        ("pet_interaction", "Pet Interaction"),
        ("predator_prey", "Predator & Prey"),
        ("underwater_marine", "Underwater & Marine"),
    ],
    "animated_styles": [
        ("2d_cartoon", "2D Cartoon"),
        ("3d_cgi", "3D CGI"),
        ("anime_manga", "Anime / Manga"),
        ("artistic_animation", "Artistic Animation"),
        ("motion_graphics", "Motion Graphics"),
        ("pixel_retro", "Pixel / Retro"),
        ("stop_motion", "Stop Motion"),
    ],
    "animated_text": [
        ("cinematic_titles", "Cinematic Titles"),
        ("kinetic_typography", "Kinetic Typography"),
        ("logo_brand_animation", "Logo / Brand Animation"),
        ("subtitle_caption_styles", "Subtitle / Caption Styles"),
        ("text_with_backgrounds", "Text with Backgrounds"),
    ],
    "av_sync_sound_effects": [
        ("asmr_ambient", "ASMR / Ambient"),
        ("cooking_kitchen", "Cooking / Kitchen"),
        ("environmental_ambient", "Environmental Ambient"),
        ("explicit_audio_tags", "Explicit Audio Tags"),
        ("footsteps_movement", "Footsteps / Movement"),
        ("impact_collision", "Impact / Collision"),
        ("mechanical_tool", "Mechanical / Tool"),
        ("organic_nature", "Organic / Nature"),
    ],
    "camera_motion": [
        ("crane_aerial", "Crane / Aerial"),
        ("dolly_tracking", "Dolly / Tracking"),
        ("drone_aerial", "Drone Aerial"),
        ("fpv_immersive", "FPV Immersive"),
        ("handheld_pov", "Handheld POV"),
        ("pan_tilt", "Pan / Tilt"),
        ("pov_subjective", "POV / Subjective"),
        ("zoom_focus", "Zoom / Focus"),
    ],
    "explainer_educational": [
        ("animated_concept", "Animated Concept"),
        ("animated_data_story", "Animated Data Story"),
        ("animated_nature_science", "Animated Nature & Science"),
        ("animated_process", "Animated Process"),
        ("diy_tutorial", "DIY Tutorial"),
        ("science_physics", "Science / Physics"),
        ("whiteboard_lecture", "Whiteboard / Lecture"),
    ],
    "fantasy_surreal": [
        ("fantasy_worlds", "Fantasy Worlds"),
        ("fluid_physics_sim", "Fluid Physics Sim"),
        ("game_movie_parody", "Game / Movie Parody"),
        ("scale_miniature", "Scale / Miniature"),
        ("surreal_dreamlike", "Surreal / Dreamlike"),
    ],
    "fine_grained_motion": [
        ("cooking_prep", "Cooking Prep"),
        ("craft_assembly", "Craft Assembly"),
        ("musical_fingers", "Musical Fingers"),
        ("sewing_textile", "Sewing / Textile"),
        ("writing_drawing", "Writing / Drawing"),
    ],
    "high_motion_action": [
        ("combat_sports", "Combat Sports"),
        ("dance_choreography", "Dance / Choreography"),
        ("extreme_sports", "Extreme Sports"),
        ("gymnastics_acrobatics", "Gymnastics / Acrobatics"),
        ("martial_arts_fighting", "Martial Arts / Fighting"),
        ("racing_speed", "Racing / Speed"),
        ("racket_individual", "Racket / Individual"),
        ("sports_gameplay", "Sports Gameplay"),
        ("team_field_sports", "Team / Field Sports"),
        ("track_field", "Track & Field"),
        ("vehicle_chase", "Vehicle Chase"),
        ("water_sports", "Water Sports"),
        ("winter_sports", "Winter Sports"),
    ],
    "human_activities": [
        ("celebrations_events", "Celebrations / Events"),
        ("child_family", "Child / Family"),
        ("commuting_errands", "Commuting / Errands"),
        ("construction_trades", "Construction / Trades"),
        ("cooking_food_prep", "Cooking / Food Prep"),
        ("eating_dining", "Eating / Dining"),
        ("fishing_boating", "Fishing / Boating"),
        ("fitness_gym", "Fitness / Gym"),
        ("gaming_tabletop", "Gaming / Tabletop"),
        ("gardening_outdoor_work", "Gardening / Outdoor Work"),
        ("haircuts_styling", "Haircuts / Styling"),
        ("household_chores", "Household Chores"),
        ("makeup_skincare", "Makeup / Skincare"),
        ("medical_healthcare", "Medical / Healthcare"),
        ("morning_routine", "Morning Routine"),
        ("motorcycle_bicycle", "Motorcycle / Bicycle"),
        ("nail_art", "Nail Art"),
        ("religious_spiritual", "Religious / Spiritual"),
        ("shopping_browsing", "Shopping / Browsing"),
        ("smartphone_device", "Smartphone / Device"),
        ("social_interactions", "Social Interactions"),
        ("tattoo_piercing", "Tattoo / Piercing"),
        ("theme_park_rides", "Theme Park / Rides"),
    ],
    "multi_speaker_dialogue": [
        ("accents_dialects", "Accents / Dialects"),
        ("comedy_performance", "Comedy Performance"),
        ("conversation_two_people", "Conversation (Two People)"),
        ("dialogue_with_sfx", "Dialogue with SFX"),
        ("emotional_monologue", "Emotional Monologue"),
        ("group_discussion", "Group Discussion"),
        ("narration_voiceover", "Narration / Voiceover"),
        ("news_professional", "News / Professional"),
        ("object_character_speech", "Object / Character Speech"),
        ("professional_service", "Professional Service"),
        ("speaking_contexts", "Speaking Contexts"),
    ],
    "music_performance": [
        ("concert_live", "Concert / Live"),
        ("ensemble_band", "Ensemble / Band"),
        ("singing_vocals", "Singing / Vocals"),
        ("solo_instrument", "Solo Instrument"),
    ],
    "narrative_cinematic": [
        ("comedy_slapstick", "Comedy / Slapstick"),
        ("dramatic_tension", "Dramatic Tension"),
        ("epic_cinematic", "Epic Cinematic"),
    ],
    "natural_patterns": [
        ("earth_sand_stone", "Earth / Sand / Stone"),
        ("fire_smoke", "Fire / Smoke"),
        ("ice_frost_snow", "Ice / Frost / Snow"),
        ("light_electricity", "Light / Electricity"),
        ("organic_living", "Organic / Living"),
        ("water_liquid", "Water / Liquid"),
    ],
    "nature_weather": [
        ("seasons_transitions", "Seasons / Transitions"),
        ("storms_extreme", "Storms / Extreme"),
        ("water_features", "Water Features"),
    ],
    "person_emoting": [
        ("anger_frustration", "Anger / Frustration"),
        ("complex_mixed", "Complex / Mixed"),
        ("disgust_discomfort", "Disgust / Discomfort"),
        ("fear_surprise", "Fear / Surprise"),
        ("joy_excitement", "Joy / Excitement"),
        ("sadness_grief", "Sadness / Grief"),
    ],
    "speech_styles": [
        ("emotional_tone", "Emotional Tone"),
        ("pauses_intonation", "Pauses / Intonation"),
        ("shout_scream", "Shout / Scream"),
        ("singing_vocal", "Singing / Vocal"),
        ("speed_variation", "Speed Variation"),
        ("whisper_asmr", "Whisper / ASMR"),
    ],
    "urban_scenes": [
        ("nightlife_entertainment", "Nightlife / Entertainment"),
        ("street_life", "Street Life"),
        ("traffic_vehicles", "Traffic / Vehicles"),
    ],
    "vehicles_machines": [
        ("aircraft_flight", "Aircraft / Flight"),
        ("cars_driving", "Cars / Driving"),
        ("industrial_machinery", "Industrial Machinery"),
        ("trains_transit", "Trains / Transit"),
    ],
}

SUB_CATEGORIES = [
    (val, "%s · %s" % (cat_label, sub_label))
    for cat_key, cat_label in CATEGORIES
    for val, sub_label in SUB_CATEGORIES_BY_CATEGORY.get(cat_key, [])
]


class VideoEditorProject(models.Model):
    _name = "video.editor.project"
    _description = "Crowley Sourcing project"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(
        string="Name",
        required=True,
        default=lambda self: self._default_name(),
        tracking=True,
    )
    project_name = fields.Char(
        string="Project Name",
    )
    topic_name = fields.Char(
        string="Topic",
    )
    s3_source_url = fields.Char(
        string="Source S3 URL",
        tracking=True,
    )
    s3_source_key = fields.Char(
        string="Source S3 Key",
        compute="_compute_s3_source_key",
        store=True,
    )
    source_metadata = fields.Json(string="Source Metadata")
    duration_seconds = fields.Float(
        string="Duration (s)",
        compute="_compute_source_summary",
        store=True,
    )
    resolution = fields.Char(
        string="Resolution",
        compute="_compute_source_summary",
        store=True,
    )
    source_fps = fields.Float(
        string="Source FPS",
        compute="_compute_source_summary",
        store=True,
    )
    source_size_mb = fields.Float(
        string="Source Size (MB)",
        compute="_compute_source_summary",
        store=True,
    )

    youtube_url = fields.Char(string="YouTube URL", tracking=True)
    youtube_video_id = fields.Char(
        string="YouTube Video ID",
        compute="_compute_youtube_video_id",
        store=True,
        index=True,
    )
    youtube_title = fields.Char(string="YouTube Title", readonly=True, tracking=True)
    youtube_channel = fields.Char(string="YouTube Channel", readonly=True)
    youtube_thumbnail_url = fields.Char(string="YouTube Thumbnail", readonly=True)
    youtube_duration_seconds = fields.Float(string="YouTube Duration (s)", readonly=True)
    youtube_resolution = fields.Char(string="YouTube Resolution", readonly=True)
    youtube_fps = fields.Float(string="YouTube FPS", readonly=True)
    youtube_tier = fields.Selection(
        [("2160p", "2160p")],
        string="YouTube Tier",
        default="2160p",
    )
    youtube_start_time = fields.Char(
        string="Start Time",
        default="00:00:00:000",
        help="Download trim start in HH:MM:SS:MS (e.g. 00:06:50:000). "
             "Leave as 00:00:00:000 to start from the beginning.",
    )
    youtube_end_time = fields.Char(
        string="End Time",
        default="00:00:00:000",
        help="Download trim end in HH:MM:SS:MS (e.g. 00:07:03:000). "
             "Leave as 00:00:00:000 to download until the end.",
    )
    youtube_ingested_at = fields.Datetime(string="YouTube Ingested At", readonly=True)
    youtube_local_blob = fields.Binary(
        string="YouTube Local Blob",
        attachment=False,
        help="Durable copy of the locally-downloaded YouTube clip held in the database between the local-extractor download and the S3 upload, so the upload survives an Odoo worker restart. Cleared automatically once the file is uploaded to S3.",
    )
    youtube_local_blob_filename = fields.Char(string="YouTube Local Blob Filename")

    prompt = fields.Text(string="Prompt")

    llm_qc_result = fields.Selection(
        [("pass", "Pass"), ("fail", "Fail"), ("flag", "Flag")],
        string="LLM QC Result",
        readonly=True,
    )
    llm_failure_reason = fields.Text(string="T2AV Failure Reason", readonly=True)
    llm_fixed_prompt = fields.Text(string="T2AV Fixed Prompt", readonly=True)
    llm_evaluated_at = fields.Datetime(string="T2AV Evaluated At", readonly=True)

    llm_qc_force_passed = fields.Boolean(
        string="LLM QC Force Passed",
        readonly=True,
        copy=False,
        tracking=True,
        help="Manually forced to PASS via the Force Pass button. The original "
             "reviewer verdict in LLM QC Result is preserved. Audit info is "
             "captured in the by/at/reason fields below and posted to the "
             "project chatter.",
    )
    llm_qc_force_passed_by = fields.Many2one(
        "res.users", string="Force-Passed By", readonly=True, copy=False, tracking=True,
    )
    llm_qc_force_passed_at = fields.Datetime(
        string="Force-Passed At", readonly=True, copy=False, tracking=True,
    )
    llm_qc_force_pass_reason = fields.Char(
        string="Force-Pass Reason", readonly=True, copy=False, tracking=True,
    )

    category = fields.Selection(
        CATEGORIES,
        string="Category",
    )
    sub_category = fields.Selection(
        SUB_CATEGORIES,
        string="Sub-Category",
    )

    @api.onchange("category")
    def _onchange_category(self):
        for rec in self:
            if not rec.sub_category:
                continue
            allowed = {val for val, _label in SUB_CATEGORIES_BY_CATEGORY.get(rec.category or "", [])}
            if rec.sub_category not in allowed:
                rec.sub_category = False

    @api.constrains("youtube_start_time", "youtube_end_time")
    def _check_youtube_time_format(self):
        for rec in self:
            try:
                start = _parse_hhmmssms_to_seconds(rec.youtube_start_time)
                end = _parse_hhmmssms_to_seconds(rec.youtube_end_time)
            except ValueError as exc:
                raise ValidationError(str(exc))
            if end > 0.0 and end <= start:
                raise ValidationError(_(
                    "YouTube End Time (%(end)s) must be greater than Start Time (%(start)s)."
                ) % {"end": rec.youtube_end_time or "0", "start": rec.youtube_start_time or "0"})

    @api.onchange("youtube_url")
    def _onchange_youtube_url_parse_times(self):
        for rec in self:
            url = (rec.youtube_url or "").strip()
            if not url:
                continue
            try:
                parsed = urlparse(url)
            except (TypeError, ValueError):
                continue
            params = parse_qs(parsed.query or "")
            try:
                start_set = _parse_hhmmssms_to_seconds(rec.youtube_start_time) > 0.0
                end_set = _parse_hhmmssms_to_seconds(rec.youtube_end_time) > 0.0
            except ValueError:
                start_set = end_set = False
            if not start_set:
                for key in ("t", "start"):
                    raw = (params.get(key) or [None])[0]
                    start_sec = _parse_yt_t_param(raw)
                    if start_sec is not None and start_sec > 0:
                        rec.youtube_start_time = _seconds_to_hhmmssms(start_sec)
                        break
            if not end_set:
                for key in ("end", "stop"):
                    raw = (params.get(key) or [None])[0]
                    end_sec = _parse_yt_t_param(raw)
                    if end_sec is not None and end_sec > 0:
                        rec.youtube_end_time = _seconds_to_hhmmssms(end_sec)
                        break

    style = fields.Selection(
        [
            ("casual", "Casual"),
            ("precise", "Precise"),
            ("exhaustive", "Exhaustive"),
            ("terse", "Terse"),
            ("creative", "Creative"),
            ("narrative", "Narrative"),
        ],
        string="Style",
    )

    editing_config = fields.Json(string="Editing Config")
    edited_file_path = fields.Char(string="Edited File")
    edited_blob = fields.Binary(
        string="Edited Video Blob",
        attachment=False,
        help="Durable copy of the rendered video held in the database between render and export-to-S3 so export survives a wiped/ephemeral local media root. Cleared automatically once the file is uploaded to S3.",
    )
    edited_blob_filename = fields.Char(string="Edited Blob Filename")
    preview_file_path = fields.Char(string="Preview File")
    output_s3_url = fields.Char(string="Trimmed S3 URL", tracking=True)

    trim_start_seconds = fields.Float(string="Trim Start (s)", readonly=True)
    trim_end_seconds = fields.Float(string="Trim End (s)", readonly=True)
    trim_duration_seconds = fields.Float(string="Trim Duration (s)", readonly=True)
    edited_resolution = fields.Char(string="Edited Resolution", readonly=True)
    edited_fps = fields.Float(string="Edited FPS", readonly=True)

    state = fields.Selection(
        PROJECT_STATES,
        default="draft",
        required=True,
        tracking=True,
        index=True,
    )

    assigned_to = fields.Many2one(
        "res.users",
        string="Assigned To",
        default=lambda self: self.env.user,
        tracking=True,
    )

    job_ids = fields.One2many(
        "video.editor.job", "project_id", string="Jobs",
    )
    active_job_id = fields.Many2one(
        "video.editor.job",
        string="Active Job",
        compute="_compute_active_job",
    )
    processing_log_ids = fields.One2many(
        "video.editor.processing.log", "project_id", string="Processing Logs",
    )

    @api.model
    def _default_name(self):
        seq = self.env["ir.sequence"].next_by_code("video.editor.project")
        return seq or _("Project")

    @api.depends("s3_source_url")
    def _compute_s3_source_key(self):
        for rec in self:
            url = (rec.s3_source_url or "").strip()
            if not url:
                rec.s3_source_key = False
                continue
            try:
                if url.startswith("s3://"):
                    _, key = url[len("s3://"):].split("/", 1)
                else:
                    parsed = urlparse(url)
                    key = parsed.path.lstrip("/")
                    host = parsed.netloc or ""
                    if host.endswith(".amazonaws.com") and (host.startswith("s3.") or host.startswith("s3-")):
                        if "/" in key:
                            _, key = key.split("/", 1)
                rec.s3_source_key = key or False
            except (ValueError, AttributeError):
                rec.s3_source_key = False

    @api.depends("source_metadata")
    def _compute_source_summary(self):
        for rec in self:
            meta = rec.source_metadata or {}
            rec.duration_seconds = float(meta.get("duration") or 0.0)
            rec.resolution = meta.get("resolution") or ""
            rec.source_fps = float(meta.get("fps") or 0.0)
            rec.source_size_mb = float(meta.get("size_bytes") or 0.0) / (1024 * 1024)

    @api.depends("job_ids.status")
    def _compute_active_job(self):
        for rec in self:
            running = rec.job_ids.filtered(lambda j: j.status in ("queued", "running"))
            rec.active_job_id = running[:1] if running else False

    @api.depends("youtube_url")
    def _compute_youtube_video_id(self):
        for rec in self:
            video_id, _normalized = youtube_downloader.parse_youtube_url(rec.youtube_url or "")
            rec.youtube_video_id = video_id or False

    @api.constrains("s3_source_url")
    def _check_s3_url(self):
        for rec in self:
            url = (rec.s3_source_url or "").strip()
            if not url:
                continue
            if url.startswith("s3://"):
                if "/" not in url[len("s3://"):]:
                    raise ValidationError(_("Invalid s3:// URL — missing key part."))
                continue
            if not url.startswith(("http://", "https://")):
                raise ValidationError(_(
                    "Source URL must be s3://… or https://… (got %s)") % url[:80])
            parsed = urlparse(url)
            if not parsed.netloc or not parsed.path:
                raise ValidationError(_("Invalid S3 URL."))

    @api.constrains("youtube_url")
    def _check_youtube_url(self):
        for rec in self:
            url = (rec.youtube_url or "").strip()
            if not url:
                continue
            video_id, _normalized = youtube_downloader.parse_youtube_url(url)
            if not video_id:
                raise ValidationError(_(
                    "Invalid YouTube URL. Expected youtube.com/watch?v=…, youtu.be/…, shorts, embed, or /v/ form."))

    @api.constrains("prompt")
    def _check_prompt_word_limit(self):
        max_words = self.env["video.editor.s3.settings"].get_prompt_max_words()
        for rec in self:
            text = (rec.prompt or "").strip()
            if not text:
                continue
            word_count = len(text.split())
            if word_count > max_words:
                raise ValidationError(_(
                    "Prompt is too long: %(current)d words. Maximum allowed: %(max)d words."
                ) % {"current": word_count, "max": max_words})

    @api.onchange("prompt")
    def _onchange_prompt_word_limit(self):
        max_words = self.env["video.editor.s3.settings"].get_prompt_max_words()
        text = (self.prompt or "").strip()
        if not text:
            return
        word_count = len(text.split())
        if word_count > max_words:
            raise UserError(_(
                "Prompt is too long: %(current)d words. Maximum allowed: %(max)d words."
            ) % {"current": word_count, "max": max_words})

    def _kick_job(self, job_type, *, config=None):
        self.ensure_one()
        active = self.job_ids.filtered(lambda j: j.status in ("queued", "running"))
        if active:
            raise UserError(_(
                "Another job is already running for this project (#%d, %s)."
            ) % (active[0].id, active[0].job_type))
        job = self.env["video.editor.job"].create({
            "project_id": self.id,
            "job_type": job_type,
            "status": "queued",
            "config_json": config or {},
        })

        def _submit():
            job._submit_async()

        self.env.cr.postcommit.add(_submit)
        return job

    def _check_trim_duration_in_range(self, config):
        settings = self.env["video.editor.s3.settings"]
        trim_min = settings.get_trim_min_seconds()
        trim_max = settings.get_trim_max_seconds()
        trim = (config or {}).get("trim") or {}
        try:
            trim_start = float(trim.get("start") or 0.0)
        except (TypeError, ValueError):
            trim_start = 0.0
        try:
            trim_end = float(trim.get("end") or 0.0)
        except (TypeError, ValueError):
            trim_end = 0.0
        trim_duration = max(trim_end - trim_start, 0.0)
        if trim_duration <= 0.0:
            trim_duration = float(self.duration_seconds or 0.0)
        if trim_duration <= 0.0:
            raise UserError(_("Cannot determine trim duration. Set a trim range before rendering."))
        if trim_duration < trim_min - 1e-4 or trim_duration > trim_max + 1e-4:
            raise UserError(_(
                "Trimmed video duration must be between %(min).1f and %(max).1f seconds. "
                "Current selection: %(current).2f seconds."
            ) % {"min": trim_min, "max": trim_max, "current": trim_duration})

    def action_render(self, config=None):
        self.ensure_one()
        if not self.s3_source_url:
            raise UserError(_("Set a source S3 URL first."))
        self._check_trim_duration_in_range(config)
        self.write({"state": "processing"})
        return self._kick_job("render", config=config)

    def action_preview(self, config=None):
        self.ensure_one()
        if not self.s3_source_url:
            raise UserError(_("Set a source S3 URL first."))
        return self._kick_job("preview", config=config)

    def action_export(self, s3_key=None):
        self.ensure_one()
        if not self.edited_file_path:
            raise UserError(_("Render the project before exporting."))
        cfg = {}
        if s3_key:
            cfg["s3_key"] = s3_key
        self.write({"state": "exporting"})
        return self._kick_job("export", config=cfg)

    def _build_youtube_job_config(self):
        self.ensure_one()
        cfg = {
            "youtube_url": self.youtube_url,
            "tier": self.youtube_tier or "2160p",
        }
        start = _parse_hhmmssms_to_seconds(self.youtube_start_time)
        end = _parse_hhmmssms_to_seconds(self.youtube_end_time)
        if start > 0.0:
            cfg["start_seconds"] = start
        if end > 0.0:
            cfg["end_seconds"] = end
        return cfg

    def action_ingest_youtube(self):
        self.ensure_one()
        if not self.youtube_url:
            raise UserError(_("Set a YouTube URL first."))
        cfg = self._build_youtube_job_config()
        is_clip = "start_seconds" in cfg or "end_seconds" in cfg
        if not is_clip:
            self._probe_youtube_or_raise()
        job = self._kick_job("youtube_ingest", config=cfg)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("YouTube ingestion queued"),
                "message": _(
                    "Job #%s is downloading the video and uploading to S3. "
                    "Refresh this form when the job finishes — the Source S3 URL "
                    "will be populated automatically."
                ) % job.id,
                "type": "info",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_download_youtube_local(self):
        self.ensure_one()
        if not self.youtube_url:
            raise UserError(_("Set a YouTube URL first."))
        base_url = self.env["video.editor.s3.settings"].sudo().get_local_extractor_url()
        if not base_url:
            raise UserError(_(
                "Local Extractor URL is not configured. Set it under "
                "Settings > Crowley Sourcing > YouTube Ingest."
            ))
        if not base_url.startswith(("http://", "https://")):
            raise UserError(_(
                "Local Extractor URL must start with http:// or https:// "
                "(got %s). Configure it as the base URL of the running "
                "scripts/local_youtube_extractor.py HTTP server, "
                "e.g. http://127.0.0.1:8081 or your Tailscale/cloudflared URL."
            ) % base_url[:120])
        cfg = self._build_youtube_job_config()
        job = self._kick_job("youtube_local_download", config=cfg)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Local YouTube download queued"),
                "message": _(
                    "Job #%s is downloading the clip via the local extractor "
                    "and uploading it to S3. Refresh this form when the job "
                    "finishes - the Source S3 URL will be populated automatically."
                ) % job.id,
                "type": "info",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_copy_source_to_trimmed_url(self):
        self.ensure_one()
        if not self.s3_source_url:
            raise UserError(_("Source S3 URL is empty - nothing to copy."))
        self.write({"output_s3_url": self.s3_source_url})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Trimmed S3 URL set"),
                "message": _("Copied Source S3 URL into Trimmed S3 URL."),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_open_editor(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "video_editor_s3.video_editor",
            "name": self.name,
            "params": {"project_id": self.id},
        }

    def action_view_jobs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Jobs"),
            "res_model": "video.editor.job",
            "view_mode": "list,form",
            "domain": [("project_id", "=", self.id)],
            "context": {"default_project_id": self.id},
        }

    def _probe_youtube_or_raise(self):
        """Run the 2160p50/60 gate synchronously so a UserError surfaces as a modal popup."""
        self.ensure_one()
        if not self.youtube_url:
            return
        cfg = self.env["video.editor.s3.settings"].sudo().get_youtube_ingest_config()
        youtube_downloader.probe_and_select(
            self.youtube_url,
            cookies_path=cfg.get("cookies_path"),
            proxy_url=cfg.get("proxy_url"),
            cookies_from_browser=cfg.get("cookies_browser"),
        )

    def _maybe_auto_ingest_youtube(self):
        for rec in self:
            if not rec.youtube_url:
                continue
            if rec.youtube_ingested_at and rec.s3_source_url:
                continue
            if rec.job_ids.filtered(lambda j: j.status in ("queued", "running")):
                continue
            video_id, _normalized = youtube_downloader.parse_youtube_url(rec.youtube_url)
            if not video_id:
                continue
            try:
                cfg = rec._build_youtube_job_config()
            except ValueError as exc:
                _logger.info("auto-ingest skipped for project %s: bad time format %s", rec.id, exc)
                continue
            is_clip = "start_seconds" in cfg or "end_seconds" in cfg
            if not is_clip:
                try:
                    rec._probe_youtube_or_raise()
                except UserError as exc:
                    _logger.info("auto-ingest probe failed for project %s: %s", rec.id, exc)
                    continue
            try:
                rec._kick_job("youtube_ingest", config=cfg)
            except UserError as exc:
                _logger.info("auto-ingest skipped for project %s: %s", rec.id, exc)

    def _maybe_probe_s3_source(self):
        for rec in self:
            if not rec.s3_source_url:
                continue
            meta = rec.source_metadata or {}
            if meta.get("size_bytes") or meta.get("duration"):
                continue
            if rec.job_ids.filtered(
                lambda j: j.job_type in ("s3_probe", "youtube_ingest") and j.status in ("queued", "running")
            ):
                continue
            try:
                rec._kick_job("s3_probe")
            except UserError as exc:
                _logger.info("s3_probe skipped for project %s: %s", rec.id, exc)

    def action_run_llm_qc(self):
        self.ensure_one()
        if not self.output_s3_url:
            raise UserError(_(
                "Render and export the trimmed clip to S3 before running LLM QC. "
                "LLM QC reviews the trimmed video, not the source."
            ))
        if not self.prompt:
            raise UserError(_("Write a prompt before running LLM QC."))
        if not self.category:
            raise UserError(_("Pick a category before running LLM QC."))
        if not self.style:
            raise UserError(_("Pick a style before running LLM QC."))
        job = self._kick_job("llm_qc")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("LLM QC queued"),
                "message": _(
                    "Job #%s is reviewing this row against the T2AV spec. "
                    "Refresh the form when the job finishes - results will "
                    "appear in the LLM QC section."
                ) % job.id,
                "type": "info",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_force_pass_llm_qc(self):
        self.ensure_one()
        if not self.llm_evaluated_at:
            raise UserError(_(
                "Run LLM QC first. Force Pass overrides a real verdict; it is "
                "not a substitute for running QC."
            ))
        if self.llm_qc_force_passed:
            raise UserError(_("This project's LLM QC is already force-passed."))
        reason = (self.env.context.get("default_llm_qc_force_pass_reason") or "").strip()
        if not reason:
            return {
                "type": "ir.actions.act_window",
                "name": _("Force Pass LLM QC"),
                "res_model": "video.editor.llm.qc.force.pass.wizard",
                "view_mode": "form",
                "target": "new",
                "context": {"default_project_id": self.id},
            }
        now = fields.Datetime.now()
        self.write({
            "llm_qc_force_passed": True,
            "llm_qc_force_passed_by": self.env.user.id,
            "llm_qc_force_passed_at": now,
            "llm_qc_force_pass_reason": reason,
        })
        original = dict(self._fields["llm_qc_result"].selection).get(
            self.llm_qc_result, self.llm_qc_result or "(no verdict)"
        )
        self.message_post(
            body=_(
                "<b>LLM QC force-passed</b> by %(user)s. "
                "Original reviewer verdict: <b>%(original)s</b>. "
                "Reason: %(reason)s"
            ) % {
                "user": self.env.user.display_name,
                "original": original,
                "reason": reason or _("(none provided)"),
            },
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("LLM QC force-passed"),
                "message": _("Override recorded on the project chatter."),
                "type": "warning",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_apply_fixed_prompt(self):
        self.ensure_one()
        if not self.llm_fixed_prompt:
            raise UserError(_("No fixed prompt is set."))
        self.write({"prompt": self.llm_fixed_prompt})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Fixed prompt applied"),
                "message": _("Re-run LLM QC to verify the new prompt."),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _maybe_run_llm_qc(self):
        for rec in self:
            if not rec.prompt:
                continue
            if rec.job_ids.filtered(lambda j: j.job_type == "prompt_qc" and j.status in ("queued", "running")):
                continue
            try:
                rec._kick_job("llm_qc")
            except UserError as exc:
                _logger.info("prompt_qc skipped for project %s: %s", rec.id, exc)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._maybe_probe_s3_source()
        return records

    def write(self, vals):
        res = super().write(vals)
        if "s3_source_url" in vals:
            self._maybe_probe_s3_source()
        return res

    def unlink(self):
        storage = self.env["video.editor.s3.media.storage"].sudo()
        roots = []
        for rec in self:
            try:
                roots.append(storage.project_dir(rec))
            except UserError:
                continue
        result = super().unlink()
        for path in roots:
            try:
                if path and os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError as exc:
                _logger.warning("project dir purge failed for %s: %s", path, exc)
        return result
