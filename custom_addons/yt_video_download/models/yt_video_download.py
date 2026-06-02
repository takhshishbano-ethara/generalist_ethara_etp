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

# Rotation strategies: each tuple is (extractor_args, user_agent).
# Tried in order on bot-detection failure. Order matters: cheaper/more-permissive
# clients first, exotic ones last.
ROTATION_STRATEGIES = [
    (
        'youtube:player_client=default,tv,web_safari',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    ),
    (
        'youtube:player_client=tv_simply,mweb',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
    ),
    (
        'youtube:player_client=android_vr',
        'com.google.android.apps.youtube.vr.oculus/1.56.21 '
        '(Linux; U; Android 12; en_US; Quest 3) gzip',
    ),
    (
        'youtube:player_client=ios',
        'com.google.ios.youtube/19.16.3 (iPhone16,2; U; CPU iOS 17_4 like Mac OS X)',
    ),
    (
        'youtube:player_client=web_creator,mediaconnect',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    ),
    (
        'youtube:player_client=tv_embedded,web_embedded',
        'Mozilla/5.0 (X11; Linux x86_64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    ),
]

BOT_DETECTION_PATTERNS = (
    'sign in to confirm',
    'confirm you’re not a bot',
    "confirm you're not a bot",
    'please sign in',
    'http error 403',
    'unable to extract',
)


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
    probe_result = fields.Text(string='Probe Result', readonly=True)

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

    def _build_cmd(self, extractor_args, user_agent):
        self.ensure_one()
        section = '*%s-%s' % (self.start_time, self.end_time)
        output_template = os.path.join(
            self.output_dir or tempfile.gettempdir(),
            '%(title)s [%(id)s].%(ext)s',
        )
        cmd = [
            'yt-dlp',
            '--cookies', BUNDLED_COOKIES_PATH,
            '--user-agent', user_agent,
            '--extractor-args', extractor_args,
            '--download-sections', section,
            '-f', self.format_spec or DEFAULT_FORMAT,
            '-o', output_template,
            '--print', 'after_move:filepath',
            '--no-simulate',
            '--no-playlist',
            '--retries', '5',
            '--fragment-retries', '5',
            '--sleep-requests', '1',
            '--sleep-interval', '2',
            '--max-sleep-interval', '5',
        ]
        if self.extra_args:
            try:
                cmd += shlex.split(self.extra_args)
            except ValueError as exc:
                raise UserError(_('Invalid Extra Args: %s') % exc)
        cmd.append(self.youtube_url)
        return cmd

    @staticmethod
    def _is_bot_detection_error(stderr):
        s = (stderr or '').lower()
        return any(p in s for p in BOT_DETECTION_PATTERNS)

    def _rotation_strategies(self):
        """Strategies tried in order. First entry honors the record's own
        extractor_args/user_agent so user customization wins on attempt 1."""
        self.ensure_one()
        first = (
            self.extractor_args or DEFAULT_EXTRACTOR_ARGS,
            self.user_agent or DEFAULT_USER_AGENT,
        )
        rest = [s for s in ROTATION_STRATEGIES if s != first]
        return [first] + rest

    def action_probe(self):
        """Dry-run each rotation strategy with --simulate. No bytes downloaded.
        Writes a green/red summary to probe_result and shows a notification.
        Use the record's URL if set; otherwise fall back to a known public video."""
        self.ensure_one()
        test_url = self.youtube_url or 'https://www.youtube.com/watch?v=BaW_jenozKc'
        if not os.path.isfile(BUNDLED_COOKIES_PATH):
            raise UserError(_('Bundled cookies file missing at %s') % BUNDLED_COOKIES_PATH)

        lines = ['Probe target: %s' % test_url, '']
        any_pass = False
        for idx, (ext_args, ua) in enumerate(self._rotation_strategies(), start=1):
            cmd = [
                'yt-dlp',
                '--cookies', BUNDLED_COOKIES_PATH,
                '--user-agent', ua,
                '--extractor-args', ext_args,
                '--simulate',
                '--no-warnings',
                '--no-playlist',
                '--print', '%(title)s',
                test_url,
            ]
            _logger.info('Probe %d/%d: %s', idx, len(ROTATION_STRATEGIES),
                         ' '.join(shlex.quote(c) for c in cmd))
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=120, check=False,
                )
            except FileNotFoundError:
                raise UserError(_('yt-dlp is not installed on this server.'))
            except subprocess.TimeoutExpired:
                lines.append('[%d] %s' % (idx, ext_args))
                lines.append('    TIMEOUT (network slow or blocked)')
                lines.append('')
                continue

            if result.returncode == 0:
                title = (result.stdout or '').strip().splitlines()[0:1]
                title = title[0] if title else '(no title)'
                lines.append('[%d] %s' % (idx, ext_args))
                lines.append('    PASS  →  %s' % title[:80])
                lines.append('')
                any_pass = True
            else:
                err = (result.stderr or '').strip().splitlines()
                err = err[-1] if err else '(no stderr)'
                bot = self._is_bot_detection_error(result.stderr)
                tag = 'BOT-BLOCKED' if bot else 'FAIL'
                lines.append('[%d] %s' % (idx, ext_args))
                lines.append('    %s  →  %s' % (tag, err[:200]))
                lines.append('')

        verdict = (
            'VERDICT: at least one strategy works — module will succeed here.'
            if any_pass else
            'VERDICT: every strategy was blocked. Server IP appears flagged; '
            'this module will NOT work without a proxy or PO Token plugin.'
        )
        lines.append(verdict)
        self.probe_result = '\n'.join(lines)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Probe Complete'),
                'message': verdict,
                'type': 'success' if any_pass else 'danger',
                'sticky': True,
            },
        }

    def action_download(self):
        for rec in self:
            rec._validate_inputs()
            combined_log = []
            success = False
            last_result = None
            strategies = rec._rotation_strategies()
            for idx, (ext_args, ua) in enumerate(strategies, start=1):
                cmd = rec._build_cmd(extractor_args=ext_args, user_agent=ua)
                header = '\n=== Attempt %d/%d  player=%s ===\n' % (
                    idx, len(strategies), ext_args)
                _logger.info('yt-dlp attempt %d/%d: %s',
                             idx, len(strategies),
                             ' '.join(shlex.quote(c) for c in cmd))
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=1800,
                        check=False,
                    )
                except FileNotFoundError:
                    rec.write({'state': 'failed',
                               'log': 'yt-dlp executable not found on server.'})
                    raise UserError(_('yt-dlp is not installed on the server.'))
                except subprocess.TimeoutExpired:
                    rec.write({'state': 'failed',
                               'log': 'yt-dlp timed out after 30 minutes.'})
                    raise UserError(_('yt-dlp timed out.'))

                attempt_log = header + (result.stdout or '') + '\n' + (result.stderr or '')
                combined_log.append(attempt_log)
                last_result = result

                if result.returncode == 0:
                    success = True
                    break
                if not rec._is_bot_detection_error(result.stderr):
                    break  # non-bot failure → don't waste attempts
                _logger.warning(
                    'yt-dlp attempt %d hit bot detection, rotating strategy', idx)

            log_text = ''.join(combined_log)
            if not success:
                rec.write({'state': 'failed', 'log': log_text})
                stderr = (last_result.stderr if last_result else '') or ''
                raise UserError(_(
                    'yt-dlp failed after %d attempt(s). Last error:\n%s'
                ) % (len(combined_log), stderr.strip()[-500:] or 'unknown error'))

            final_path = ''
            for line in (last_result.stdout or '').splitlines():
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
