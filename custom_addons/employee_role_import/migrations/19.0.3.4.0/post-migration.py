"""employee_code on res.users changed from a related (non-stored) field to a
stored, editable column. The upgrade adds an empty column, so backfill it from
each user's linked employee, and upper-case existing codes everywhere so they
match the new model-level normalization (stored UPPER + trimmed)."""


def migrate(cr, version):
    # Capitalise any existing employee codes (the =ilike unique check already
    # prevented case-only duplicates, so upper-casing cannot create a clash).
    cr.execute(
        """
        UPDATE hr_employee
           SET employee_code = upper(trim(employee_code))
         WHERE employee_code IS NOT NULL
           AND employee_code <> ''
           AND employee_code <> upper(trim(employee_code))
        """
    )
    # Backfill the user column (UPPER) from the linked employee where empty.
    cr.execute(
        """
        UPDATE res_users u
           SET employee_code = upper(trim(e.employee_code))
          FROM hr_employee e
         WHERE e.user_id = u.id
           AND e.employee_code IS NOT NULL
           AND e.employee_code <> ''
           AND (u.employee_code IS NULL OR u.employee_code = '')
        """
    )
