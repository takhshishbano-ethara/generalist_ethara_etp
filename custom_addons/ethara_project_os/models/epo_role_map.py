"""Where a Project OS role comes from — ``api.role``, not a registry of our own.

This deployment already has a role registry: ``api.role``, shipped by
``api_auth_gateway`` and assigned per user through ``res.users.user_role``. Modules
consume it by mapping their own vocabulary onto its xml-ids — see
``ethara_project/models/role_map.py``, which is the pattern this file follows.

Project OS therefore does **not** own a role table. It owns four Odoo groups (because
ACLs and record rules can only be written against groups) and derives membership from
whatever ``user_role`` says.

Two things make the switchover safe:

* **Missing roles resolve to nothing, they do not raise.** The four Project OS roles do
  not exist in ``api.role`` yet. Until they are created, every lookup here returns an
  empty list and the derivation simply finds nobody — it does not crash, and it does not
  strip anybody's access.
* **``epo.role.assignment`` stays as the fallback.** While ``user_role`` yields no
  Project OS level for somebody, their groups continue to come from their grant, exactly
  as before. The moment a mapped ``api.role`` is set on their user, that wins. Nobody
  loses access on the day this ships, and the grant model can be retired once the
  registry is populated.

To finish the migration, create these four ``api.role`` records in
``api_auth_gateway`` — the names follow that module's existing ``role_<name>_<type>``
convention — and set ``user_role`` on each user:

    api_auth_gateway.role_tasker_technical     → PM  (pod member)
    api_auth_gateway.role_pl_technical         → PL  (pod lead)
    api_auth_gateway.role_gpm_technical        → GPM (general programme management)
    api_auth_gateway.role_admin_technical      → Admin

The stem / non-stem variants of tasker and PL are already there and are mapped too: one
Project OS level legitimately covers several ``api.role`` records, which is why each
entry is a tuple rather than a single id.
"""

import logging

_logger = logging.getLogger(__name__)

# Project OS level → the api.role xml-ids that mean it.
#
# `role_gpm_technical` does NOT exist yet; it is listed so that creating it is the only
# step needed to finish the switchover. `role_tpm_technical` is deliberately NOT mapped
# to GPM: TPM is a different job, and guessing here would hand project-creation and
# allocation rights to the wrong people.
ROLE_XML_IDS = {
    'pm': (
        'api_auth_gateway.role_tasker_technical',
        'api_auth_gateway.role_tasker_stem',
        'api_auth_gateway.role_tasker_non_stem',
    ),
    'pl': (
        'api_auth_gateway.role_pl_technical',
        'api_auth_gateway.role_pl_stem',
        'api_auth_gateway.role_pl_non_stem',
    ),
    'gpm': (
        'api_auth_gateway.role_gpm_technical',
    ),
    'admin': (
        'api_auth_gateway.role_admin_technical',
    ),
}

# Second way in, for roles created through the UI. A record made by hand has NO xml-id,
# so the map above cannot see it — and "the roles will be added later" usually means
# somebody adds them in Settings, not in a data file. Matching `user_type` as well means
# either route works.
#
# Matched case-insensitively, exact string only. Deliberately NOT substring matching:
# 'TPM' contains 'PM', and a substring rule would quietly map every TPM onto a Project
# OS level nobody intended.
ROLE_USER_TYPES = {
    'pm': ('tasker', 'tasker-stem', 'tasker-non-stem', 'pod_member', 'pod member'),
    'pl': ('pl', 'pl-stem', 'pl-non-stem', 'pod_lead', 'pod lead'),
    'gpm': ('gpm', 'general program management', 'general programme management'),
    'admin': ('admin',),
}

# Ordered weakest → strongest, so "the highest level this user holds" has one answer.
ROLE_RANK = ['pm', 'pl', 'gpm', 'admin']

# Project OS level → the Odoo group that carries it. The groups are what the record
# rules and ACLs are written against; nothing outside this module needs to know them.
ROLE_GROUPS = {
    'pm': 'ethara_project_os.group_epo_member',
    'pl': 'ethara_project_os.group_epo_pod_lead',
    'gpm': 'ethara_project_os.group_epo_manager',
    'admin': 'ethara_project_os.group_epo_admin',
}


def resolve_role_ids(env, level):
    """The ``api.role`` ids that mean ``level``.

    Two routes, because a role can arrive either way:

    1. **xml-id** — a record shipped in a data file. Explicit and versionable.
    2. **user_type** — a record somebody created in Settings, which has no xml-id at
       all. Without this, roles added through the UI would never map and the derivation
       would silently find nobody.

    Missing records are skipped rather than raising, so a half-populated registry
    degrades to the ``epo.role.assignment`` fallback instead of breaking.
    """
    ids = set()
    for xml_id in ROLE_XML_IDS.get(level, ()):
        record = env.ref(xml_id, raise_if_not_found=False)
        if record:
            ids.add(record.id)

    wanted = ROLE_USER_TYPES.get(level, ())
    if wanted and 'api.role' in env:
        for role in env['api.role'].sudo().search([('user_type', '!=', False)]):
            if (role.user_type or '').strip().lower() in wanted:
                ids.add(role.id)
    return sorted(ids)


def level_for_api_role(env, api_role):
    """The Project OS level an ``api.role`` maps to, or ``False``.

    Returns the strongest match, so a role listed under two levels cannot make somebody
    silently weaker than they should be.
    """
    if not api_role:
        return False
    best = False
    for level in ROLE_RANK:
        if api_role.id in resolve_role_ids(env, level):
            best = level
    return best


def registry_is_populated(env):
    """Whether ``api.role`` yet carries any of the four levels.

    While this is false the derivation has nothing to work with, and
    ``epo.role.assignment`` remains the source of truth. Logged once per call site so a
    half-finished migration is visible rather than silent.
    """
    return any(resolve_role_ids(env, level) for level in ROLE_RANK)
