# -*- coding: utf-8 -*-
import logging
import os
import re
import shlex
import subprocess
import tempfile

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_FORMAT = 'bv*[height=2160]+ba/b[height=2160]'
DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)
DEFAULT_EXTRACTOR_ARGS = 'youtube:player_client=default,tv,web_safari'
BUNDLED_COOKIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'cookies.txt',
)
TIME_RE = re.compile(r'^(\d{1,2}:)?\d{1,2}:\d{2}(\.\d+)?$|^\d+(\.\d+)?$')


class YtVideoDownload(models.Model):
    _name = 'yt.video.download'
    _description = 'YouTube Video Download'
    _order = 'create_date desc'

    name = fields.Char(
        string='Name',
        default=lambda self: _('New'),
    )
    youtube_url = fields.Char(
        string='YouTube URL',
        required=True,
        help='Full YouTube video URL',
    )
    start_time = fields.Char(
        string='Start Time',
        required=True,
        help='Start timestamp, e.g. 00:01:30 or 90',
    )
    end_time = fields.Char(
        string='End Time',
        required=True,
        help='End timestamp, e.g. 00:02:45 or 165',
    )
    output_dir = fields.Char(
        string='Output Directory',
        default=lambda self: tempfile.gettempdir(),
        help='Directory where the downloaded file will be saved. '
             'Must be writable by the Odoo process user.',
    )
    format_spec = fields.Char(
        string='Format',
        default=DEFAULT_FORMAT,
        help='yt-dlp -f format selector',
    )
    user_agent = fields.Char(
        string='User-Agent',
        default=DEFAULT_USER_AGENT,
        help='HTTP User-Agent sent to YouTube. Helps avoid bot detection.',
    )
    extractor_args = fields.Char(
        string='Extractor Args',
        default=DEFAULT_EXTRACTOR_ARGS,
        help='Passed to yt-dlp --extractor-args. '
             'Default rotates YouTube player clients to bypass bot checks.',
    )
    extra_args = fields.Char(
        string='Extra yt-dlp Args',
        help='Additional command-line args appended verbatim '
             '(e.g. "--proxy http://user:pass@host:port"). '
             'Use with caution — split by shell rules.',
    )
    state = fields.Selection(
        [('draft', 'Draft'),
         ('done', 'Done'),
         ('failed', 'Failed')],
        string='Status',
        default='draft',
    )
    output_path = fields.Char(string='Output Path', readonly=True)
    log = fields.Text(string='Log', readonly=True)

    def _validate_inputs(self):
        self.ensure_one()
        if not self.youtube_url or not self.youtube_url.startswith(('http://', 'https://')):
            raise UserError(_('Please provide a valid YouTube URL.'))
        if not TIME_RE.match(self.start_time or ''):
            raise UserError(_('Invalid start time format. Use HH:MM:SS, MM:SS or seconds.'))
        if not TIME_RE.match(self.end_time or ''):
            raise UserError(_('Invalid end time format. Use HH:MM:SS, MM:SS or seconds.'))
        if not os.path.isfile(BUNDLED_COOKIES_PATH):
            raise UserError(_('Bundled cookies file is missing at %s') % BUNDLED_COOKIES_PATH)
        if self.output_dir and not os.path.isdir(self.output_dir):
            try:
                os.makedirs(self.output_dir, exist_ok=True)
            except OSError as exc:
                raise UserError(_('Cannot create output directory: %s') % exc)

    def _build_cmd(self):
        self.ensure_one()
        section = '*%s-%s' % (self.start_time, self.end_time)
        output_template = os.path.join(
            self.output_dir or tempfile.gettempdir(),
            '%(title)s [%(id)s].%(ext)s',
        )
        cmd = [
            'yt-dlp',
            '--cookies', BUNDLED_COOKIES_PATH,
        ]
        if self.user_agent:
            cmd += ['--user-agent', self.user_agent]
        if self.extractor_args:
            cmd += ['--extractor-args', self.extractor_args]
        cmd += [
            '--download-sections', section,
            '-f', self.format_spec or DEFAULT_FORMAT,
            '-o', output_template,
            '--print', 'after_move:filepath',
            '--no-simulate',
            '--no-playlist',
            '--retries', '5',
            '--fragment-retries', '5',
        ]
        if self.extra_args:
            try:
                cmd += shlex.split(self.extra_args)
            except ValueError as exc:
                raise UserError(_('Invalid Extra Args: %s') % exc)
        cmd.append(self.youtube_url)
        return cmd

    def action_download(self):
        for rec in self:
            rec._validate_inputs()
            cmd = rec._build_cmd()
            _logger.info('Running yt-dlp: %s', ' '.join(shlex.quote(c) for c in cmd))
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )
            except FileNotFoundError:
                rec.write({'state': 'failed', 'log': 'yt-dlp executable not found on server.'})
                raise UserError(_('yt-dlp is not installed on the server.'))
            except subprocess.TimeoutExpired:
                rec.write({'state': 'failed', 'log': 'yt-dlp timed out after 30 minutes.'})
                raise UserError(_('yt-dlp timed out.'))

            log_text = (result.stdout or '') + '\n' + (result.stderr or '')
            if result.returncode != 0:
                rec.write({'state': 'failed', 'log': log_text})
                raise UserError(_('yt-dlp failed (exit %s):\n%s') % (
                    result.returncode, result.stderr or result.stdout))

            final_path = ''
            for line in (result.stdout or '').splitlines():
                line = line.strip()
                if line and os.path.isabs(line):
                    final_path = line
            if rec.name == _('New') or not rec.name:
                rec.name = os.path.basename(final_path) or rec.youtube_url
            rec.write({
                'state': 'done',
                'output_path': final_path,
                'log': log_text,
            })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Download Complete'),
                'message': _('Video downloaded successfully.'),
                'type': 'success',
                'sticky': False,
            },
        }
