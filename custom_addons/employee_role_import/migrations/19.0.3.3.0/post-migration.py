"""Normalize existing Employee IDs to upper-case.

The Employee ID (hr_employee.employee_code) is now stored upper-cased on every
create/write. Bring already-stored values in line so the list/detail/edit/users
views are consistent.
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE hr_employee
           SET employee_code = upper(trim(employee_code))
         WHERE employee_code IS NOT NULL
           AND employee_code <> upper(trim(employee_code))
        """
    )
