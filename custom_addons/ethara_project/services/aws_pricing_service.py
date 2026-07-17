import json
import logging
import uuid
from datetime import datetime, timedelta

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PRIMARY_ATTRS = [
    "instanceType",
    "storageClass",
    "group",
    "volumeType",
    "usagetype",
    "productFamily",
]

SKIP_FOR_PRIMARY = {
    "termType",
    "servicecode",
    "servicename",
    "location",
    "locationType",
    "regionCode",
    "operation",
    "Restriction",
}

NOISE_DEFAULTS = {
    "operatingSystem": "Linux",
    "tenancy": "Shared",
    "capacitystatus": "Used",
    "preInstalledSw": "NA",
    "marketoption": "OnDemand",
    "deploymentOption": "Single-AZ",
}

HIDE_ATTRS = {
    "servicecode",
    "servicename",
    "locationType",
    "regionCode",
    "location",
    "operation",
    "termType",
    "usagetype",
    "availabilityzone",
    "productFamily",
}


def _client(env):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise UserError(_("Python package 'boto3' is not installed."))

    creds = env["ethara.project.aws.credentials"].sudo().get_singleton()
    access_key = creds.access_key_id or ""
    secret_key = creds.secret_key or ""
    if not access_key or not secret_key:
        raise UserError(_(
            "AWS credentials are missing. Set the Access Key ID and Secret "
            "Access Key in Ethara Projects \u2192 AWS \u2192 Credentials, "
            "then click Save."
        ))
    region = (
        creds.region_name
        or env["ir.config_parameter"].sudo().get_param(
            "ethara_project.aws_pricing.region", "us-east-1"
        )
    )
    return boto3.client(
        "pricing",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def primary_attr(attribute_names):
    if not attribute_names:
        return None
    for a in PRIMARY_ATTRS:
        if a in attribute_names:
            return a
    for a in attribute_names:
        if a not in SKIP_FOR_PRIMARY:
            return a
    return attribute_names[0]


def list_services(env):
    client = _client(env)
    out = []
    api_calls = 0
    for page in client.get_paginator("describe_services").paginate(
        FormatVersion="aws_v1"
    ):
        api_calls += 1
        for s in page.get("Services", []):
            out.append(
                {
                    "serviceCode": s["ServiceCode"],
                    "attributeNames": list(s.get("AttributeNames") or []),
                }
            )
    out.sort(key=lambda s: s["serviceCode"])
    return out, api_calls


def list_items(env, service_code, attribute_name):
    if not service_code or not attribute_name:
        return [], 0
    client = _client(env)
    values = []
    api_calls = 0
    for page in client.get_paginator("get_attribute_values").paginate(
        ServiceCode=service_code, AttributeName=attribute_name
    ):
        api_calls += 1
        for v in page.get("AttributeValues", []):
            val = v.get("Value")
            if val:
                values.append(val)
    values.sort()
    return values, api_calls


def _first_on_demand(terms):
    for offer in (terms.get("OnDemand") or {}).values():
        for dim in (offer.get("priceDimensions") or {}).values():
            return {
                "usd": (dim.get("pricePerUnit") or {}).get("USD"),
                "unit": dim.get("unit"),
                "description": dim.get("description"),
            }
    return None


def get_pricing(env, service_code, attribute_names, primary, item, region, limit=100):
    client = _client(env)
    has_location = "location" in (attribute_names or [])

    base = [{"Type": "TERM_MATCH", "Field": primary, "Value": item}]
    loc_filter = (
        [{"Type": "TERM_MATCH", "Field": "location", "Value": region}]
        if has_location and primary != "usagetype" and region
        else []
    )
    noise = [
        {"Type": "TERM_MATCH", "Field": k, "Value": v}
        for k, v in NOISE_DEFAULTS.items()
        if k in (attribute_names or []) and k != primary
    ]

    filter_stages = [
        [*base, *loc_filter, *noise],
        [*base, *loc_filter],
        [*base],
    ]

    api_calls = 0
    for filters in filter_stages:
        rows = []
        for page in client.get_paginator("get_products").paginate(
            ServiceCode=service_code, FormatVersion="aws_v1", Filters=filters
        ):
            api_calls += 1
            for s in page.get("PriceList", []):
                p = json.loads(s) if isinstance(s, str) else s
                od = _first_on_demand(p.get("terms", {}) or {})
                product = p.get("product", {}) or {}
                rows.append(
                    {
                        "sku": product.get("sku"),
                        "attributes": product.get("attributes", {}) or {},
                        "onDemandUSD": od["usd"] if od else None,
                        "unit": od["unit"] if od else None,
                        "priceDescription": od["description"] if od else None,
                        "hasReserved": bool((p.get("terms", {}) or {}).get("Reserved")),
                    }
                )
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        if rows:
            return rows, api_calls
    return [], api_calls


def summarize(rows):
    if not rows:
        return {"specs": {}, "varying": [], "rows": rows}
    keys = set()
    for r in rows:
        keys.update((r.get("attributes") or {}).keys())
    specs, varying = {}, []
    for k in keys:
        vals = {(r.get("attributes") or {}).get(k) for r in rows}
        if len(vals) <= 1:
            if k not in HIDE_ATTRS:
                specs[k] = (rows[0].get("attributes") or {}).get(k)
        else:
            if k not in HIDE_ATTRS:
                varying.append(k)
    return {"specs": specs, "varying": varying[:8], "rows": rows}


def _upsert_service(env, service_code, attribute_names):
    Infra = env["ethara.project.infra.type"].sudo()
    prim = primary_attr(attribute_names) or ""
    now = datetime.utcnow()
    # Keep AWS-synced services linked to the AWS provider so the Provider
    # column / filter stays correct (seeded via infra_provider_data.xml).
    aws_provider = env.ref(
        "ethara_project.infra_provider_aws", raise_if_not_found=False
    )
    aws_provider_id = aws_provider.id if aws_provider else False
    existing = Infra.search(
        [("aws_service_code", "=", service_code), ("is_aws_managed", "=", True)],
        limit=1,
    )
    if existing:
        vals = {
            "primary_attr": prim,
            "attribute_names": json.dumps(attribute_names or []),
            "last_synced_at": now,
            "active": True,
        }
        if aws_provider_id and not existing.provider_id:
            vals["provider_id"] = aws_provider_id
        existing.write(vals)
        return existing, False
    return (
        Infra.create(
            {
                "name": service_code,
                "code": service_code.lower(),
                "aws_service_code": service_code,
                "primary_attr": prim,
                "attribute_names": json.dumps(attribute_names or []),
                "is_aws_managed": True,
                "sync_enabled": True,
                "active": True,
                "last_synced_at": now,
                "provider_id": aws_provider_id,
            }
        ),
        True,
    )


def action_sync_all(env, triggered_by="cron", user_id=None):
    Log = env["ethara.project.aws.pricing.sync.log"].sudo()

    batch_id = uuid.uuid4().hex
    log = Log.create(
        {
            "batch_id": batch_id,
            "started_at": datetime.utcnow(),
            "triggered_by": triggered_by,
            "user_id": user_id,
            "status": "running",
        }
    )
    services_fetched = services_upserted = 0
    items_fetched = items_upserted = 0
    api_calls = 0
    detail_lines = []
    try:
        services, calls = list_services(env)
        api_calls += calls
        services_fetched = len(services)

        for svc in services:
            code = svc["serviceCode"]
            attrs = svc["attributeNames"]
            try:
                _rec, created = _upsert_service(env, code, attrs)
                services_upserted += 1
                if created:
                    detail_lines.append(f"CREATED {code}")
            except Exception as e:
                detail_lines.append(f"SERVICE-ERR {code}: {e}")
                _logger.warning(
                    "AWS Pricing sync: upsert service %s failed: %s", code, e
                )

        env.cr.commit()

        log.write(
            {
                "ended_at": datetime.utcnow(),
                "services_fetched": services_fetched,
                "services_upserted": services_upserted,
                "items_fetched": items_fetched,
                "items_upserted": items_upserted,
                "api_call_count": api_calls,
                "status": "success",
                "detail_log": "\n".join(detail_lines[-500:]),
            }
        )
    except Exception as e:
        _logger.exception("AWS Pricing sync failed: %s", e)
        log.write(
            {
                "ended_at": datetime.utcnow(),
                "services_fetched": services_fetched,
                "services_upserted": services_upserted,
                "items_fetched": items_fetched,
                "items_upserted": items_upserted,
                "api_call_count": api_calls,
                "status": "failed",
                "error_message": str(e),
                "detail_log": "\n".join(detail_lines[-500:]),
            }
        )
    return log


def fetch_items_for_service(env, infra, force_refresh=False, cache_hours=24):
    Item = env["ethara.project.aws.pricing.item"].sudo()
    if not infra or not infra.aws_service_code or not infra.primary_attr:
        return {
            "items_fetched": 0,
            "items_upserted": 0,
            "items_deactivated": 0,
            "from_cache": False,
        }

    now = datetime.utcnow()
    if not force_refresh and infra.last_synced_at:
        age_seconds = (now - infra.last_synced_at).total_seconds()
        if age_seconds < cache_hours * 3600:
            existing_count = Item.search_count(
                [("infra_type_id", "=", infra.id), ("active", "=", True)]
            )
            if existing_count > 0:
                return {
                    "items_fetched": existing_count,
                    "items_upserted": 0,
                    "items_deactivated": 0,
                    "from_cache": True,
                }

    code = infra.aws_service_code
    prim = infra.primary_attr
    names, _calls = list_items(env, code, prim)

    existing_items = Item.search([("infra_type_id", "=", infra.id)])
    existing_by_name = {r.name: r for r in existing_items}
    seen = set()
    upserted = 0
    for nm in names:
        seen.add(nm)
        if nm in existing_by_name:
            existing_by_name[nm].write({"active": True, "last_synced_at": now})
        else:
            Item.create(
                {
                    "infra_type_id": infra.id,
                    "name": nm,
                    "active": True,
                    "last_synced_at": now,
                }
            )
            upserted += 1
    to_deactivate = [r.id for r in existing_items if r.name not in seen and r.active]
    if to_deactivate:
        Item.browse(to_deactivate).write({"active": False})
    infra.write({"item_count": len(names), "last_synced_at": now})
    return {
        "items_fetched": len(names),
        "items_upserted": upserted,
        "items_deactivated": len(to_deactivate),
        "from_cache": False,
    }


def fetch_pricing_cached(env, service_code, item_name, region_label, force_refresh=False):
    Infra = env["ethara.project.infra.type"].sudo()
    Item = env["ethara.project.aws.pricing.item"].sudo()
    Sku = env["ethara.project.aws.pricing.sku"].sudo()

    infra = Infra.search([("aws_service_code", "=", service_code)], limit=1)
    if not infra:
        return None
    item = Item.search(
        [("infra_type_id", "=", infra.id), ("name", "=", item_name)], limit=1
    )
    if not item:
        return None

    now = datetime.utcnow()
    if not force_refresh:
        cached = Sku.search(
            [
                ("item_id", "=", item.id),
                ("region_label", "=", region_label),
                ("cache_expiry_at", ">", now),
                ("active", "=", True),
            ]
        )
        if cached:
            return _skus_to_pricing_dict(
                cached, service_code, item_name, region_label, cached=True
            )

    attribute_names = json.loads(infra.attribute_names or "[]")
    prim = infra.primary_attr or primary_attr(attribute_names) or "instanceType"
    rows, _api = get_pricing(env, service_code, attribute_names, prim, item_name, region_label)
    if not rows:
        return {
            "service_code": service_code,
            "item": item_name,
            "region": region_label,
            "min_price": None,
            "unit": None,
            "cached": False,
            "specs": {},
            "varying": [],
            "rows": [],
        }

    ttl_hours = int(
        env["ir.config_parameter"]
        .sudo()
        .get_param("ethara_project.aws_pricing.cache_ttl_hours", "24")
    )
    expiry = now + timedelta(hours=ttl_hours)
    batch_id = uuid.uuid4().hex

    Sku.search(
        [
            ("item_id", "=", item.id),
            ("region_label", "=", region_label),
        ]
    ).write({"active": False})

    for r in rows:
        aws_sku = r.get("sku")
        if not aws_sku:
            continue
        attrs = r.get("attributes") or {}
        vals = {
            "item_id": item.id,
            "aws_sku": aws_sku,
            "region_label": region_label,
            "on_demand_usd": float(r.get("onDemandUSD") or 0.0),
            "unit": r.get("unit") or "",
            "price_description": r.get("priceDescription") or "",
            "vcpu": _safe_int(attrs.get("vcpu")),
            "memory": attrs.get("memory") or "",
            "storage": attrs.get("storage") or "",
            "network_performance": attrs.get("networkPerformance") or "",
            "operating_system": attrs.get("operatingSystem") or "",
            "tenancy": attrs.get("tenancy") or "",
            "instance_family": attrs.get("instanceFamily") or "",
            "attributes_json": json.dumps(attrs),
            "has_reserved": bool(r.get("hasReserved")),
            "last_synced_at": now,
            "cache_expiry_at": expiry,
            "sync_batch_id": batch_id,
            "active": True,
        }
        existing = Sku.search([("aws_sku", "=", aws_sku)], limit=1)
        if existing:
            existing.write(vals)
        else:
            Sku.create(vals)

    sku_records = Sku.search(
        [
            ("item_id", "=", item.id),
            ("region_label", "=", region_label),
            ("sync_batch_id", "=", batch_id),
            ("active", "=", True),
        ]
    )
    return _skus_to_pricing_dict(
        sku_records, service_code, item_name, region_label, cached=False
    )


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _skus_to_pricing_dict(sku_records, service_code, item_name, region_label, cached):
    rows = []
    for s in sku_records:
        try:
            attrs = json.loads(s.attributes_json or "{}")
        except (ValueError, TypeError):
            attrs = {}
        rows.append(
            {
                "sku": s.aws_sku,
                "onDemandUSD": s.on_demand_usd,
                "unit": s.unit,
                "priceDescription": s.price_description,
                "hasReserved": s.has_reserved,
                "attributes": attrs,
            }
        )
    summary = summarize(rows)
    prices = [r["onDemandUSD"] for r in rows if r.get("onDemandUSD")]
    return {
        "service_code": service_code,
        "item": item_name,
        "region": region_label,
        "min_price": min(prices) if prices else None,
        "unit": rows[0]["unit"] if rows else None,
        "cached": cached,
        "specs": summary["specs"],
        "varying": summary["varying"],
        "rows": rows,
    }


EBS_VOLUME_LABELS = {
    "gp3": "General Purpose SSD (gp3)",
    "gp2": "General Purpose SSD (gp2)",
    "io2": "Provisioned IOPS SSD (io2)",
    "io1": "Provisioned IOPS SSD (io1)",
    "st1": "Throughput HDD (st1)",
    "sc1": "Cold HDD (sc1)",
    "standard": "Magnetic (standard)",
}

EBS_STATIC_FALLBACK = [
    {"volume_code": "gp3", "volume_label": EBS_VOLUME_LABELS["gp3"], "rate_usd_per_gb_mo": 0.08, "unit": "GB-Mo"},
    {"volume_code": "gp2", "volume_label": EBS_VOLUME_LABELS["gp2"], "rate_usd_per_gb_mo": 0.10, "unit": "GB-Mo"},
    {"volume_code": "io2", "volume_label": EBS_VOLUME_LABELS["io2"], "rate_usd_per_gb_mo": 0.125, "unit": "GB-Mo"},
    {"volume_code": "io1", "volume_label": EBS_VOLUME_LABELS["io1"], "rate_usd_per_gb_mo": 0.125, "unit": "GB-Mo"},
    {"volume_code": "st1", "volume_label": EBS_VOLUME_LABELS["st1"], "rate_usd_per_gb_mo": 0.045, "unit": "GB-Mo"},
    {"volume_code": "sc1", "volume_label": EBS_VOLUME_LABELS["sc1"], "rate_usd_per_gb_mo": 0.015, "unit": "GB-Mo"},
    {"volume_code": "standard", "volume_label": EBS_VOLUME_LABELS["standard"], "rate_usd_per_gb_mo": 0.05, "unit": "GB-Mo"},
]


def list_ebs_volumes(env, region_label):
    client = _client(env)
    filters = [
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
        {"Type": "TERM_MATCH", "Field": "location", "Value": region_label},
    ]
    result_by_code = {}
    for page in client.get_paginator("get_products").paginate(
        ServiceCode="AmazonEC2", FormatVersion="aws_v1", Filters=filters
    ):
        for s in page.get("PriceList", []):
            try:
                p = json.loads(s)
            except (ValueError, TypeError):
                continue
            attrs = p.get("product", {}).get("attributes", {}) or {}
            code = attrs.get("volumeApiName")
            if not code:
                continue
            od = _first_on_demand(p.get("terms", {}))
            if not od or not od.get("usd"):
                continue
            unit = (od.get("unit") or "").strip()
            if unit and unit.lower() != "gb-mo":
                continue
            try:
                rate = float(od["usd"])
            except (ValueError, TypeError):
                continue
            prev = result_by_code.get(code)
            if prev is None or rate < prev["rate_usd_per_gb_mo"]:
                result_by_code[code] = {
                    "volume_code": code,
                    "volume_label": EBS_VOLUME_LABELS.get(code, code),
                    "rate_usd_per_gb_mo": rate,
                    "unit": unit or "GB-Mo",
                }
    return sorted(result_by_code.values(), key=lambda v: v["volume_code"])


def fetch_ebs_pricing_cached(env, region_label, force_refresh=False):
    Cache = env["ethara.project.aws.ebs.pricing"].sudo()
    now = datetime.utcnow()
    if not force_refresh:
        cached = Cache.search(
            [
                ("region_label", "=", region_label),
                ("cache_expiry_at", ">", now),
                ("active", "=", True),
            ]
        )
        if cached:
            volumes = sorted(
                (
                    {
                        "volume_code": r.volume_code,
                        "volume_label": r.volume_label or r.volume_code,
                        "rate_usd_per_gb_mo": r.rate_usd_per_gb_mo,
                        "unit": r.unit or "GB-Mo",
                    }
                    for r in cached
                ),
                key=lambda v: v["volume_code"],
            )
            return {
                "source": cached[0].source or "live",
                "region": region_label,
                "count": len(volumes),
                "volumes": volumes,
                "cached": True,
            }

    ttl_hours = int(
        env["ir.config_parameter"]
        .sudo()
        .get_param("ethara_project.aws_pricing.cache_ttl_hours", "24")
    )
    expiry = now + timedelta(hours=ttl_hours)

    source = "live"
    try:
        volumes = list_ebs_volumes(env, region_label)
        if not volumes:
            volumes = list(EBS_STATIC_FALLBACK)
            source = "fallback"
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "EBS pricing fetch failed for region %s, using static fallback: %s",
            region_label,
            exc,
        )
        volumes = list(EBS_STATIC_FALLBACK)
        source = "fallback"

    Cache.search([("region_label", "=", region_label)]).write({"active": False})

    for v in volumes:
        Cache.create(
            {
                "region_label": region_label,
                "volume_code": v["volume_code"],
                "volume_label": v.get("volume_label") or v["volume_code"],
                "rate_usd_per_gb_mo": v["rate_usd_per_gb_mo"],
                "unit": v.get("unit") or "GB-Mo",
                "last_synced_at": now,
                "cache_expiry_at": expiry,
                "source": source,
                "active": True,
            }
        )

    return {
        "source": source,
        "region": region_label,
        "count": len(volumes),
        "volumes": volumes,
        "cached": False,
    }
