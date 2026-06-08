import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# Fields whose transitions raise a kubera.notification for the assignee.
NOTIFY_FIELDS = ("state", "review_status", "llm_qc_result")

# Values that warrant a high-priority notification.
HIGH_PRIORITY_VALUES = ("error", "fail", "rejected")

TITLES = {
    "state": "Video Project Stage Updated",
    "review_status": "Video Project Review Updated",
    "llm_qc_result": "Video Project QC Verdict",
}


class VideoEditorProject(models.Model):
    _inherit = "video.editor.project"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        try:
            for record in records:
                record._notify_kubera(
                    title="Video Project Created",
                    message=f'Project "{record.name}" created (stage: '
                            f'{record._field_label("state", record.state)}).',
                    priority="1",
                )
        except Exception:
            _logger.exception(
                "kubera create-notifications failed for video.editor.project %s",
                records.ids,
            )
        return records

    def write(self, vals):
        # Snapshot old values before the write; never let the notification
        # machinery break the underlying create/write.
        tracked = {}
        try:
            tracked = {
                field: {rec.id: rec[field] for rec in self}
                for field in NOTIFY_FIELDS
                if field in vals
            }
        except Exception:
            _logger.exception(
                "kubera notification snapshot failed for video.editor.project %s",
                self.ids,
            )

        res = super().write(vals)

        try:
            for field, old_by_id in tracked.items():
                for record in self:
                    old = old_by_id.get(record.id)
                    new = record[field]
                    if old == new:
                        continue
                    old_label = record._field_label(field, old) or "none"
                    new_label = record._field_label(field, new) or "none"
                    priority = "2" if new in HIGH_PRIORITY_VALUES else "1"
                    record._notify_kubera(
                        title=TITLES[field],
                        message=f'Project "{record.name}": '
                                f"{old_label} → {new_label}.",
                        priority=priority,
                    )
        except Exception:
            _logger.exception(
                "kubera state-notifications failed for video.editor.project %s",
                self.ids,
            )
        return res

    def _field_label(self, field, value):
        if not value:
            return ""
        return dict(self._fields[field].selection).get(value, value)

    def _notify_kubera(self, title, message, priority="1"):
        """Create a kubera.notification for the assignee.

        Never raises: project state is also written from worker threads and
        Lambda/EC2 callbacks, and a notification failure must not roll back
        the underlying transition.
        """
        self.ensure_one()
        try:
            user = self.assigned_to or self.env.user
            external_project = self.env["project.project"].sudo().search(
                [("connected_table", "=", "video.editor.project")], limit=1
            )
            self.env["kubera.notification"].sudo().create({
                "title": title,
                "message": message,
                "user_id": user.id,
                "priority": priority,
                "res_model": "video.editor.project",
                "res_id": self.id,
                "project_id": external_project.id or False,
            })
        except Exception:
            _logger.exception(
                "kubera notification failed for video.editor.project %s", self.id
            )
