def migrate(cr, version):
    cr.execute(
        """
        UPDATE hr_employee
           SET employee_code = upper(trim(employee_code))
         WHERE employee_code IS NOT NULL
           AND employee_code <> ''
           AND employee_code <> upper(trim(employee_code))
        """
    )
    # employee_code on res.users was briefly a stored column in 19.0.3.4.0 but
    # is now a non-stored related field (related to employee_id.employee_code).
    # Drop the column on any DB that did create it, so the loaded model and the
    # schema stay aligned.
    cr.execute("ALTER TABLE res_users DROP COLUMN IF EXISTS employee_code")
