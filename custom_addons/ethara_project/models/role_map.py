ROLE_XML_IDS = {
    'tpm': (
        'api_auth_gateway.role_tpm_technical',
    ),
    'pl': (
        'api_auth_gateway.role_pl_technical',
        'api_auth_gateway.role_pl_stem',
        'api_auth_gateway.role_pl_non_stem',
    ),
    'qc': (
        'api_auth_gateway.role_qc_technical',
        'api_auth_gateway.role_qc_stem',
    ),
    'qr': (
        'api_auth_gateway.role_qc_non_stem',
    ),
    'rnd': (
        'api_auth_gateway.role_rnd_technical',
    ),
    'cto': (
        'api_auth_gateway.role_cto_technical',
    ),
    'cfo': (
        'api_auth_gateway.role_cfo_technical',
    ),
}

ROLE_XML_IDS['pl_ql'] = (
    ROLE_XML_IDS['pl'] + ROLE_XML_IDS['qc'] + ROLE_XML_IDS['qr']
)

VALID_ROLE_KEYS = tuple(ROLE_XML_IDS.keys())


def resolve_role_ids(env, key):
    xml_ids = ROLE_XML_IDS.get(key)
    if not xml_ids:
        return []
    ids = []
    for xid in xml_ids:
        rec = env.ref(xid, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids
