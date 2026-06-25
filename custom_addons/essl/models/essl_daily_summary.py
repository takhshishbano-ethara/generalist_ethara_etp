from odoo import fields, models, tools


class EsslDailySummary(models.Model):
    _name = "essl_pull_api.daily.summary"
    _description = "ESSL Daily Punch Summary (per user per day)"
    _auto = False
    _order = "punch_date desc, employee_name, device_user_id"
    _rec_name = "device_user_id"

    sr_no = fields.Integer(string="#", readonly=True)
    punch_date = fields.Date(string="Date", readonly=True)
    device_user_id = fields.Char(string="User ID", readonly=True, index=True)
    employee_id = fields.Many2one("hr.employee", string="Employee", readonly=True, index=True)
    employee_name = fields.Char(string="Name", readonly=True)
    employee_code = fields.Char(string="Employee Code", readonly=True)
    punch_in_time = fields.Char(string="Punch In", readonly=True)
    punch_in_type = fields.Char(string="In Type", readonly=True)
    punch_out_time = fields.Char(string="Punch Out", readonly=True)
    punch_out_type = fields.Char(string="Out Type", readonly=True)
    total_punches = fields.Integer(string="Total Punches", readonly=True)
    device_id = fields.Many2one("essl.device", string="Last Device", readonly=True)
    device_name = fields.Char(string="Last Device Name", readonly=True)
    location = fields.Char(string="Location", readonly=True)
    entry_device_id = fields.Many2one("essl.device", string="Entry Device", readonly=True)
    entry_device_name = fields.Char(string="Entry Device Name", readonly=True)
    entry_location = fields.Char(string="Entry Location", readonly=True)
    matched = fields.Boolean(string="Mapped", readonly=True, index=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE OR REPLACE VIEW {self._table} AS
            WITH base AS (
                SELECT
                    l.id AS log_id,
                    l.punch_timestamp,
                    l.punch_timestamp::date AS punch_date,
                    l.device_user_id,
                    l.device_id,
                    d.name AS device_name,
                    COALESCE(
                        NULLIF(TRIM(d.location), ''),
                        NULLIF(TRIM(d.device_location), ''),
                        d.name
                    ) AS location,
                    e.id AS employee_id,
                    e.name AS employee_name,
                    e.employee_code,
                    COALESCE(e.id::text, 'unmatched:' || l.device_user_id) AS user_key
                FROM essl_attendance_log l
                LEFT JOIN essl_device d
                    ON d.id = l.device_id
                LEFT JOIN hr_employee e
                    ON e.id = l.employee_id
            ),
            agg AS (
                SELECT
                    punch_date,
                    user_key,
                    MIN(device_user_id) AS device_user_id,
                    employee_id,
                    employee_name,
                    employee_code,
                    MIN(punch_timestamp) AS first_time,
                    MAX(punch_timestamp) AS last_time,
                    COUNT(*) AS total_punches,
                    MIN(log_id) AS min_log_id
                FROM base
                GROUP BY
                    punch_date, user_key,
                    employee_id, employee_name, employee_code
            ),
            entry_punch AS (
                SELECT DISTINCT ON (punch_date, user_key)
                    punch_date,
                    user_key,
                    device_id  AS entry_device_id,
                    device_name AS entry_device_name,
                    location   AS entry_location
                FROM base
                ORDER BY punch_date, user_key, punch_timestamp ASC
            ),
            last_punch AS (
                SELECT DISTINCT ON (punch_date, user_key)
                    punch_date,
                    user_key,
                    device_id  AS last_device_id,
                    device_name AS last_device_name,
                    location   AS last_location
                FROM base
                ORDER BY punch_date, user_key, punch_timestamp DESC
            )
            SELECT
                ROW_NUMBER() OVER (
                    ORDER BY a.punch_date DESC, a.employee_name NULLS LAST, a.device_user_id
                ) AS sr_no,
                a.min_log_id AS id,
                a.punch_date,
                a.device_user_id,
                a.employee_id,
                a.employee_name,
                a.employee_code,
                TO_CHAR(a.first_time, 'YYYY-MM-DD HH24:MI:SS') AS punch_in_time,
                'Check In' AS punch_in_type,
                TO_CHAR(a.last_time, 'YYYY-MM-DD HH24:MI:SS') AS punch_out_time,
                'Check Out' AS punch_out_type,
                a.total_punches::int AS total_punches,
                lp.last_device_id   AS device_id,
                lp.last_device_name AS device_name,
                lp.last_location    AS location,
                ep.entry_device_id   AS entry_device_id,
                ep.entry_device_name AS entry_device_name,
                ep.entry_location    AS entry_location,
                (a.employee_id IS NOT NULL) AS matched
            FROM agg a
            LEFT JOIN last_punch lp
                ON lp.punch_date = a.punch_date
               AND lp.user_key = a.user_key
            LEFT JOIN entry_punch ep
                ON ep.punch_date = a.punch_date
               AND ep.user_key = a.user_key
        """)
