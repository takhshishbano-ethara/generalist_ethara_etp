from odoo import fields, models


class WildclawMediaAttachment(models.Model):
    """Multimodal attachment record — references S3-uploaded media.

    Backs the multimedia features: image/video/PDF upload from chat widget,
    inline media → S3 replacement in trajectories, video-frame extraction,
    PDF document analysis. Wrapped task models (kensei_wildclaw.task,
    skoll_wildclaw.task, talos_wildclaw.task) attach these via Many2many.
    """

    _name = "wildclaw.media.attachment"
    _description = "WildClaw Multimodal Attachment (S3-backed)"
    _order = "create_date desc, id desc"

    name = fields.Char(string="Filename", required=True)
    mime_type = fields.Char(string="MIME Type", required=True, index=True)
    media_kind = fields.Selection(
        [
            ("image", "Image"),
            ("video", "Video"),
            ("audio", "Audio"),
            ("pdf", "PDF Document"),
            ("text", "Text Document"),
            ("other", "Other"),
        ],
        string="Kind",
        required=True,
        default="other",
        index=True,
    )
    byte_size = fields.Integer(string="Size (bytes)")
    s3_url = fields.Char(
        string="S3 HTTPS URL",
        help="Public/presigned HTTPS URL after _replace_inline_media_with_s3 upload.",
    )
    s3_key = fields.Char(string="S3 Object Key", index=True)
    sha256_hex = fields.Char(string="SHA-256", index=True)

    image_width = fields.Integer(string="Width (px)")
    image_height = fields.Integer(string="Height (px)")
    video_duration_s = fields.Float(string="Video Duration (s)")
    video_fps = fields.Float(string="Video FPS")
    frame_extract_count = fields.Integer(
        string="Frames Extracted", default=0,
        help="Populated by media_processor.extract_video_frames().",
    )
    pdf_page_count = fields.Integer(string="PDF Page Count")
    pdf_text = fields.Text(
        string="Extracted PDF Text",
        help="Populated by media_processor.extract_pdf_text() — used for multimodal-looker-style analysis.",
    )

    audio_duration_s = fields.Float(string="Audio Duration (s)")
    audio_sample_rate = fields.Integer(string="Audio Sample Rate (Hz)")
    audio_channels = fields.Integer(string="Audio Channels")
    audio_transcript = fields.Text(
        string="Audio Transcript",
        help="Populated by media_processor.transcribe_audio() if Whisper/Bedrock transcription is configured.",
    )

    sam3_mask_count = fields.Integer(string="SAM3 Mask Count", default=0,
        help="Populated by sam3_inference.segment_image() — number of detected masks.")
    sam3_masks_s3_key = fields.Char(string="SAM3 Masks S3 Key",
        help="S3 key for serialized SAM3 mask payload (JSON or .npz).")

    source_url = fields.Char(string="Source URL",
        help="If downloaded by prep_runner.download_video() (yt-dlp) or download_weights() (ModelScope), the source URL.")
    source_kind = fields.Selection(
        [("upload", "User Upload"), ("yt_dlp", "yt-dlp Download"),
         ("modelscope", "ModelScope Download"), ("hf_hub", "HuggingFace Hub"),
         ("archive_extract", "Extracted from Archive")],
        default="upload", string="Source",
    )

    sandbox_model = fields.Char(string="Sandbox Model", index=True)
    sandbox_id_int = fields.Integer(string="Sandbox ID", index=True)
    task_id_str = fields.Char(string="Task ID", index=True)

    uploaded_by_id = fields.Many2one("res.users", string="Uploaded By", default=lambda s: s.env.user)
    notes = fields.Text(string="Notes")
