from odoo import api, fields, models
from markupsafe import escape

from ._constants import SEV_COLORS


class ArcQcGameResult(models.Model):

    _name = 'arc.qc.game.result'
    _description = 'ARC QC Game Result'
    _order = 'verdict_order asc, game_id asc'

    session_id = fields.Many2one(
        comodel_name='arc.qc.session',
        string='QC Session',
        required=True,
        ondelete='cascade',
        index=True,
    )
    game_id = fields.Char(
        string='Game ID',
        required=True,
        index=True,
    )
    game_path = fields.Char(string='Game Path')

    verdict = fields.Selection(
        selection=[
            ('ship', 'SHIP'),
            ('conditional_ship', 'CONDITIONAL SHIP'),
            ('block', 'BLOCK'),
        ],
        readonly=True,
        index=True,
    )
    passed = fields.Boolean(
        string='Passed',
        compute='_compute_passed',
        store=True,
    )
    verdict_order = fields.Integer(
        compute='_compute_verdict_order',
        store=True,
        index=True,
    )

    models_found = fields.Integer(string='Models Found')
    models_expected = fields.Integer(string='Models Expected', default=4)
    runs_checked = fields.Integer(string='Runs')
    steps_checked = fields.Integer(string='Steps')

    critical_count = fields.Integer(string='Critical')
    high_count = fields.Integer(string='High')
    medium_count = fields.Integer(string='Medium')
    low_count = fields.Integer(string='Low')
    finding_count = fields.Integer(
        string='Total Findings',
        compute='_compute_finding_count',
        store=True,
    )

    summary_html = fields.Html(
        string='Summary',
        compute='_compute_summary_html',
        sanitize=False,
    )

    finding_ids = fields.One2many(
        comodel_name='arc.qc.finding',
        inverse_name='game_result_id',
        string='Findings',
        readonly=True,
    )

    model_result_ids = fields.One2many(
        comodel_name='arc.qc.model.result',
        inverse_name='game_result_id',
        string='Model Results',
        readonly=True,
    )

    @api.depends('verdict')
    def _compute_passed(self):
        for rec in self:
            rec.passed = rec.verdict == 'ship'

    @api.depends('verdict')
    def _compute_verdict_order(self):
        order_map = {'block': 1, 'conditional_ship': 2, 'ship': 3}
        for rec in self:
            rec.verdict_order = order_map.get(rec.verdict, 99)

    @api.depends('critical_count', 'high_count', 'medium_count', 'low_count')
    def _compute_finding_count(self):
        for rec in self:
            rec.finding_count = (
                rec.critical_count + rec.high_count
                + rec.medium_count + rec.low_count
            )

    @api.depends('finding_ids', 'finding_ids.phase', 'finding_ids.severity',
                 'finding_ids.code', 'verdict', 'models_found', 'models_expected')
    def _compute_summary_html(self):
        """Build per-phase check/cross summary for each game result."""
        # All QC phases in order
        phases = [
            ('discovery', 'Discovery', 'Model directories present'),
            ('structural', 'Structural', 'Files exist, valid UTF-8, parseable JSONL'),
            ('runs', 'Runs Validation', '25 required fields, invariants'),
            ('steps', 'Steps Validation', '23 required fields, action allow-list, sequences'),
            ('cross_run', 'Cross-Run', 'run_id linkage, step counts match'),
            ('content_safety', 'Content Safety', 'No leakage, injection, PII'),
            ('smell_tests', 'Smell Tests', 'No statistical anomalies'),
        ]

        for rec in self:
            if not rec.verdict:
                rec.summary_html = ''
                continue

            # Group findings by phase and severity
            phase_findings = {}
            for f in rec.finding_ids:
                phase_key = f.phase
                if phase_key not in phase_findings:
                    phase_findings[phase_key] = {
                        'critical': 0, 'high': 0, 'medium': 0, 'low': 0,
                    }
                phase_findings[phase_key][f.severity] = (
                    phase_findings[phase_key].get(f.severity, 0) + 1
                )

            lines = []
            lines.append('<div style="margin-bottom:12px;">')

            # Phase checklist
            for phase_key, phase_label, phase_desc in phases:
                pf = phase_findings.get(phase_key, {})
                crit = pf.get('critical', 0)
                high = pf.get('high', 0)
                med = pf.get('medium', 0)
                low = pf.get('low', 0)

                if crit > 0:
                    icon = '<span style="color:#dc3545;font-size:16px;">&#10007;</span>'
                    status_text = f'<span style="color:#dc3545;font-weight:bold;">{crit} critical</span>'
                    if high:
                        status_text += f', {high} high'
                    if med:
                        status_text += f', {med} medium'
                    if low:
                        status_text += f', {low} low'
                elif high > 0:
                    icon = '<span style="color:#fd7e14;font-size:16px;">&#10007;</span>'
                    status_text = f'<span style="color:#fd7e14;font-weight:bold;">{high} high</span>'
                    if med:
                        status_text += f', {med} medium'
                    if low:
                        status_text += f', {low} low'
                elif med > 0:
                    icon = '<span style="color:#ffc107;font-size:16px;">&#9888;</span>'
                    status_text = f'<span style="color:#ffc107;">{med} medium</span>'
                    if low:
                        status_text += f', {low} low'
                elif low > 0:
                    icon = '<span style="color:#6c757d;font-size:16px;">~</span>'
                    status_text = f'<span style="color:#6c757d;">{low} low</span>'
                else:
                    icon = '<span style="color:#28a745;font-size:16px;">&#10003;</span>'
                    status_text = '<span style="color:#28a745;">Passed</span>'

                lines.append(
                    f'<div style="display:flex;align-items:center;gap:8px;padding:4px 0;'
                    f'border-bottom:1px solid #eee;">'
                    f'  {icon}'
                    f'  <div style="flex:1;">'
                    f'    <strong>{phase_label}</strong>'
                    f'    <span style="color:#666;font-size:12px;margin-left:8px;">{phase_desc}</span>'
                    f'  </div>'
                    f'  <div style="text-align:right;min-width:120px;">{status_text}</div>'
                    f'</div>'
                )

            lines.append('</div>')

            # Top finding codes for this game
            code_counts = {}
            for f in rec.finding_ids:
                key = (f.code, f.severity)
                code_counts[key] = code_counts.get(key, 0) + 1

            if code_counts:
                top_codes = sorted(code_counts.items(), key=lambda x: -x[1])[:10]
                lines.append('<h5 style="margin-top:12px;">Top Finding Codes</h5>')
                lines.append('<table style="border-collapse:collapse;width:100%;">')
                lines.append(
                    '<thead><tr style="border-bottom:2px solid #dee2e6;">'
                    '<th style="text-align:left;padding:3px 6px;">Code</th>'
                    '<th style="text-align:left;padding:3px 6px;">Severity</th>'
                    '<th style="text-align:right;padding:3px 6px;">Count</th>'
                    '</tr></thead><tbody>'
                )
                for (code, sev), count in top_codes:
                    color = SEV_COLORS.get(sev, '#000')
                    lines.append(
                        f'<tr style="border-bottom:1px solid #eee;">'
                        f'<td style="padding:3px 6px;font-family:monospace;">{escape(code)}</td>'
                        f'<td style="padding:3px 6px;color:{color};font-weight:bold;">{escape(sev.upper())}</td>'
                        f'<td style="padding:3px 6px;text-align:right;">{count:,}</td>'
                        f'</tr>'
                    )
                lines.append('</tbody></table>')

            rec.summary_html = '\n'.join(lines)

    def action_print_report(self):
        """Generate and download the PDF QC report for this game."""
        self.ensure_one()
        return self.env.ref(
            'arc_qc.action_report_game_result_findings'
        ).report_action(self)
