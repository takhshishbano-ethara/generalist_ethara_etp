from odoo import fields, models


class TesseractShowcaseConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    tesseract_trajectories_url = fields.Char(
        string="Trajectories URL",
        config_parameter="tesseract_dashboard.trajectories_url",
        help="URL for the Trajectories link button. "
        "Leave empty to use the default GitHub URL.",
    )
    tesseract_dataset_url = fields.Char(
        string="Dataset URL",
        config_parameter="tesseract_dashboard.dataset_url",
        help="URL for the Dataset link button. "
        "Leave empty to use the default HuggingFace URL.",
    )
