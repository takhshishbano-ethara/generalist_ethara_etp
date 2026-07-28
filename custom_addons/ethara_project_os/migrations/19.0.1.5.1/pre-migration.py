"""Refresh the four group labels, which ``noupdate="1"`` froze at the old vocabulary.

``19.0.1.5.0`` renamed the group xml-ids (``group_epo_member`` → ``group_epo_tasker``,
``group_epo_manager`` → ``group_epo_pm``) by rewriting ``ir_model_data``, which is what
preserved every membership. But ``security/epo_groups.xml`` sits in a ``noupdate="1"``
block, so the loader will not write field values onto a record that already exists — and
the *labels* stayed behind:

    group_epo_pm      ->  "GPM — General Program Management"
    group_epo_tasker  ->  "PM — Pod Member"

Which is the worst possible half-state: a group whose xml-id says ``pm`` displaying a name
that says ``Pod Member``, in the Settings screen where somebody decides who gets which
access level. Exactly the inversion the rename existed to remove.

Same root cause as the mail template fixed in 19.0.1.5.0: ``noupdate="1"`` protects an
operator's edits, and the price is that our own corrections do not land either. Anything
shipped in a noupdate block has to be migrated explicitly when it changes.

Written to be idempotent and unconditional — it asserts the canonical label rather than
translating the old one, so it is safe on every future upgrade and will re-heal the labels
if they ever drift again. ``name`` is jsonb (``translate=True``); ``jsonb_set`` is used so
only ``en_US`` is touched and any other language a deployment has added survives.
"""

import logging

_logger = logging.getLogger(__name__)

# xml-id -> the label it must carry.
CANONICAL_LABELS = {
    'group_epo_tasker': 'Tasker',
    'group_epo_pod_lead': 'PL — Pod Lead',
    'group_epo_pm': 'PM — Programme Manager',
    'group_epo_admin': 'Admin — Full Control',
}


def migrate(cr, version):
    if not version:
        return

    for xml_id, label in CANONICAL_LABELS.items():
        cr.execute("""
            UPDATE res_groups g
               SET name = jsonb_set(
                       COALESCE(g.name, '{}'::jsonb), '{en_US}', to_jsonb(%s::text), true)
              FROM ir_model_data d
             WHERE d.res_id = g.id
               AND d.model = 'res.groups'
               AND d.module = 'ethara_project_os'
               AND d.name = %s
               AND COALESCE(g.name ->> 'en_US', '') <> %s
        """, (label, xml_id, label))
        if cr.rowcount:
            _logger.info('Project OS upgrade: group %s relabelled to "%s".',
                         xml_id, label)
