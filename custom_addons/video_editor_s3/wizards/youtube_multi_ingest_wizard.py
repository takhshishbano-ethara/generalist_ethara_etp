from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import youtube_downloader


class YoutubeMultiIngestWizard(models.TransientModel):
    _name = "video.editor.youtube.multi.ingest.wizard"
    _description = "Ingest one YouTube URL into one project per selected tier"

    youtube_url = fields.Char(required=True)
    tier_1080p = fields.Boolean(string="1080p")
    tier_1440p = fields.Boolean(string="1440p")
    tier_2160p = fields.Boolean(string="2160p")

    def _selected_tiers(self):
        tiers = []
        if self.tier_1080p:
            tiers.append("1080p")
        if self.tier_1440p:
            tiers.append("1440p")
        if self.tier_2160p:
            tiers.append("2160p")
        return tiers

    @api.constrains("youtube_url")
    def _check_youtube_url(self):
        for rec in self:
            video_id, _normalized = youtube_downloader.parse_youtube_url(rec.youtube_url or "")
            if not video_id:
                raise UserError(_("Invalid YouTube URL: %s") % (rec.youtube_url or ""))

    def action_run(self):
        self.ensure_one()
        tiers = self._selected_tiers()
        if not tiers:
            raise UserError(_("Pick at least one resolution tier."))
        url = (self.youtube_url or "").strip()
        cfg = self.env["video.editor.s3.settings"].sudo().get_youtube_ingest_config()
        available, formats_desc = youtube_downloader.list_available_tiers(
            url,
            cookies_path=cfg.get("cookies_path") or None,
            proxy_url=cfg.get("proxy_url") or None,
            cookies_from_browser=cfg.get("cookies_browser") or None,
        )
        chosen = [t for t in tiers if t in available]
        skipped = [t for t in tiers if t not in available]
        if not chosen:
            raise UserError(_(
                "None of the selected tiers (%(picked)s) are available for this video.\n"
                "Available tiers: %(avail)s.\n\nStreams reported by YouTube:\n%(streams)s"
            ) % {
                "picked": ", ".join(tiers),
                "avail": ", ".join(available) if available else "none",
                "streams": formats_desc,
            })
        Project = self.env["video.editor.project"]
        created_ids = []
        for tier in chosen:
            project = Project.create({
                "name": "YouTube %s [%s]" % (url[:80], tier),
                "youtube_url": url,
                "youtube_tier": tier,
            })
            project._kick_job("youtube_ingest", config={"youtube_url": url, "tier": tier})
            created_ids.append(project.id)
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "video_editor_s3.action_video_editor_project"
        )
        action.update({
            "domain": [("id", "in", created_ids)],
            "context": {},
        })
        if skipped:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Some tiers skipped"),
                    "message": _("Skipped (not in source): %(skipped)s. Queued: %(chosen)s.") % {
                        "skipped": ", ".join(skipped), "chosen": ", ".join(chosen),
                    },
                    "type": "warning",
                    "sticky": True,
                    "next": action,
                },
            }
        return action
