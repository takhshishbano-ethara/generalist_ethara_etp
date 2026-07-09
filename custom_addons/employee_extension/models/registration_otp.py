import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# --- OTP policy -------------------------------------------------------------
OTP_LENGTH = 6                 # number of digits in the code
OTP_TTL_MINUTES = 10           # how long a code stays valid after it is issued
MAX_VERIFY_ATTEMPTS = 5        # wrong tries before the code is locked out
RESEND_COOLDOWN_SECONDS = 60   # min gap between two send_otp calls for one email


def _normalize_email(email):
    return (email or '').strip().lower()


class EmployeeRegistrationOtp(models.Model):
    """Email one-time-passwords issued during employee/candidate self-registration.

    A single row per email is kept alive at a time: requesting a new code for an
    address deletes the previous one. Codes are short lived, rate limited on both
    send (cooldown) and verify (attempt cap), and cleaned up by a daily cron.
    """
    _name = 'employee.registration.otp'
    _description = 'Self-Registration Email OTP'
    _order = 'id desc'
    _rec_name = 'email'

    email = fields.Char(string='Email', required=True, index=True)
    otp_code = fields.Char(string='OTP Code', required=True)
    expires_at = fields.Datetime(string='Expires At', required=True)
    attempts = fields.Integer(string='Failed Attempts', default=0)

    @staticmethod
    def _generate_code():
        """Return a cryptographically-secure numeric code, zero padded."""
        return str(secrets.randbelow(10 ** OTP_LENGTH)).zfill(OTP_LENGTH)

    def generate_for_email(self, email):
        """Issue a fresh OTP for ``email``.

        Enforces the resend cooldown, drops any earlier code for the same
        address so only the newest one is ever valid, and returns
        ``(record, error_message)``. ``record`` is ``False`` when a soft error
        (e.g. cooldown) blocks generation.
        """
        normalized = _normalize_email(email)
        if not normalized:
            return False, "Email is required"

        now = fields.Datetime.now()
        existing = self.search([('email', '=', normalized)], order='id desc')
        if existing:
            elapsed = (now - existing[0].create_date).total_seconds()
            wait = RESEND_COOLDOWN_SECONDS - elapsed
            if wait > 0:
                return False, (
                    "Please wait %d second(s) before requesting another OTP"
                    % int(wait + 0.999)
                )
            existing.unlink()

        record = self.create({
            'email': normalized,
            'otp_code': self._generate_code(),
            'expires_at': now + timedelta(minutes=OTP_TTL_MINUTES),
            'attempts': 0,
        })
        return record, None

    def verify_for_email(self, email, code):
        """Check ``code`` against the active OTP for ``email``.

        Increments the failed-attempt counter on a mismatch and enforces the
        attempt cap and expiry. The code is intentionally *not* consumed on
        success: the caller may fail a later validation step (Aadhaar, duplicate
        account, ...) and should be able to resubmit the same code while it is
        still valid. A registered email cannot be reused anyway, so the row is
        left for the expiry/cron to reap. Returns ``(ok, message)``.
        """
        normalized = _normalize_email(email)
        code = (code or '').strip()
        if not normalized:
            return False, "Email is required"
        if not code:
            return False, "OTP is required. Please verify your email first."

        otp = self.search([('email', '=', normalized)], order='id desc', limit=1)
        if not otp:
            return False, "No OTP found for this email. Please request a new one."
        if otp.attempts >= MAX_VERIFY_ATTEMPTS:
            return False, "Too many incorrect attempts. Please request a new OTP."
        if fields.Datetime.now() > otp.expires_at:
            return False, "OTP has expired. Please request a new one."

        try:
            match = secrets.compare_digest(otp.otp_code, code)
        except (TypeError, ValueError):
            match = False
        if not match:
            otp.attempts += 1
            remaining = MAX_VERIFY_ATTEMPTS - otp.attempts
            if remaining <= 0:
                return False, "Too many incorrect attempts. Please request a new OTP."
            return False, "Invalid OTP. %d attempt(s) remaining." % remaining

        return True, "OTP verified"

    @api.model
    def _gc_expired_otps(self):
        """Cron entry point: purge codes that expired more than a day ago."""
        cutoff = fields.Datetime.now() - timedelta(days=1)
        stale = self.search([('expires_at', '<', cutoff)])
        count = len(stale)
        if count:
            stale.unlink()
            _logger.info("Garbage-collected %d expired registration OTP(s)", count)
        return count
