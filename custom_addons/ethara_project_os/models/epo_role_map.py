"""Where a Project OS role comes from — ``api.role``, not a registry of our own.

This deployment already has a role registry: ``api.role``, shipped by
``api_auth_gateway`` and populated for the pod structure by ``pod_roles``. Users are
assigned through ``res.users.user_role``. Modules consume it by mapping their own
vocabulary onto its records — see ``ethara_project/models/role_map.py``, which is the
pattern this file follows.

Project OS therefore does **not** own a role table. It owns four Odoo groups (because
ACLs and record rules can only be written against groups) and derives membership from
whatever ``user_role`` says.

The four levels, weakest to strongest:

    tasker → pl → pm → admin

**On the vocabulary.** These names match ``pod_roles`` exactly, and that is deliberate.
An earlier version of this module called the bottom level ``pm`` (for "pod member") and
the programme-management level ``gpm``. The organisation then settled on *Tasker* for
the bottom and *PM — Programme Manager* for the level above Pod Lead, which made the
old names actively dangerous: ``pm`` meant the *weakest* level here and the
*second-strongest* everywhere else. Anything that carried a role string across the
boundary — an API response, a ``min_role`` check — could be read with exactly inverted
privilege. The levels were renamed in 19.0.1.5.0 (``pm`` → ``tasker``, then ``gpm`` →
``pm``, in that order; see that migration) so that one word means one thing everywhere.

Two things make deriving from the registry safe:

* **Missing roles resolve to nothing, they do not raise.** If a mapped ``api.role`` is
  absent, the lookup returns an empty list and the derivation finds nobody — it does not
  crash, and it does not strip anybody's access.
* **``epo.role.assignment`` stays as the fallback.** While ``user_role`` yields no
  Project OS level for somebody, their groups continue to come from their grant. The
  moment a mapped ``api.role`` is set on their user, that wins.
"""

import logging

_logger = logging.getLogger(__name__)

# Project OS level → the api.role xml-ids that mean it.
#
# `pod_roles` is the intended source; the `api_auth_gateway` entries are the older
# records that predate it and are kept so a deployment without `pod_roles` still maps.
#
# `role_tpm_technical` is deliberately NOT mapped: TPM is a different job, and guessing
# here would hand project-creation and allocation rights to the wrong people.
ROLE_XML_IDS = {
    'tasker': (
        'pod_roles.role_tasker',
        'api_auth_gateway.role_tasker_technical',
        'api_auth_gateway.role_tasker_stem',
        'api_auth_gateway.role_tasker_non_stem',
    ),
    'pl': (
        'pod_roles.role_pl',
        'api_auth_gateway.role_pl_technical',
        'api_auth_gateway.role_pl_stem',
        'api_auth_gateway.role_pl_non_stem',
    ),
    'pm': (
        'pod_roles.role_pm',
    ),
    'admin': (
        'pod_roles.role_admin',
        'api_auth_gateway.role_admin_technical',
    ),
}

# Second way in, for roles created through the UI. A record made by hand has NO xml-id,
# so the map above cannot see it. Matching `user_type` as well means either route works.
#
# Matched case-insensitively, exact string only. Deliberately NOT substring matching:
# 'TPM' contains 'PM', and a substring rule would quietly map every TPM onto the
# programme-management level. This is the rule that lets `pm` appear below safely.
#
# 'gpm' is retained on the `pm` level as a legacy alias: it is what this level was
# called before 19.0.1.5.0, and an api.role record may still carry it.
ROLE_USER_TYPES = {
    'tasker': ('tasker', 'tasker-stem', 'tasker-non-stem', 'pod_member', 'pod member'),
    'pl': ('pl', 'pl-stem', 'pl-non-stem', 'pod_lead', 'pod lead'),
    'pm': ('pm', 'gpm', 'general program management', 'general programme management'),
    'admin': ('admin',),
}

# Ordered weakest → strongest, so "the highest level this user holds" has one answer.
ROLE_RANK = ['tasker', 'pl', 'pm', 'admin']

# Project OS level → the Odoo group that carries it. The groups are what the record
# rules and ACLs are written against; nothing outside this module needs to know them.
ROLE_GROUPS = {
    'tasker': 'ethara_project_os.group_epo_tasker',
    'pl': 'ethara_project_os.group_epo_pod_lead',
    'pm': 'ethara_project_os.group_epo_pm',
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
    """Whether ``api.role`` yet carries **every** one of the four levels.

    While this is false, at least one level cannot be derived and
    ``epo.role.assignment`` must remain available as the fallback — retiring the grant
    model early would leave nobody able to hold the missing level.

    Deliberately ``all`` and not ``any``: with ``any``, three of four levels resolving
    was enough to report a ready registry while the programme-management level had no
    record at all, which is precisely the state that broke project creation.
    """
    return all(resolve_role_ids(env, level) for level in ROLE_RANK)
