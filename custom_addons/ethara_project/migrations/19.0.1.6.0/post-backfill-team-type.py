"""Backfill `team_type` on existing project budgets.

Introduced with the Team Type feature. Budgets created before this upgrade
have no `team_type`, but per-project uniqueness is now keyed on
(team_type, budget_sub_type). Classify the legacy rows so the new key stays
sound:

  * R&D budgets (project_type='rnd')        -> team_type='rnd'
  * Production budgets (project_type='operations') -> team_type='generalist'

Only one operations budget per project existed under the old
(project, project_type) rule, so defaulting them all to 'generalist' cannot
create a duplicate. 'generalist' is the chosen default for legacy Production
budgets; change here if a different classification is wanted. Runs once, on the
upgrade that ships version 19.0.1.6.0.
"""


def migrate(cr, version):
    # R&D budgets map cleanly onto the R&D team.
    cr.execute(
        """
        UPDATE ethara_project_budget
        SET team_type = 'rnd'
        WHERE project_type = 'rnd' AND team_type IS NULL
        """
    )
    # Legacy Production (operations) budgets default to the Generalist team.
    cr.execute(
        """
        UPDATE ethara_project_budget
        SET team_type = 'generalist'
        WHERE project_type = 'operations' AND team_type IS NULL
        """
    )
