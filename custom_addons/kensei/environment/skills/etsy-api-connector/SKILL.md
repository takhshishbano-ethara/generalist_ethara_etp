---
name: etsy-api-connector
description: >
  Use when managing an Etsy shop — listing products, updating inventory/pricing,
  viewing orders/receipts, handling reviews, or managing shipping profiles via
  the Etsy Open API v3 HTTP endpoints.
---

# Etsy Open API v3 Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `ETSY_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### Shop

```
GET /v3/application/shops/{shop_id}
PUT /v3/application/shops/{shop_id}
```

**PUT body (partial update):**

```json
{
  "announcement": "New announcement text",
  "is_vacation": false
}
```

### Shop Sections

```
GET /v3/application/shops/{shop_id}/sections
GET /v3/application/shops/{shop_id}/sections/{section_id}
```

### Listings

```
GET /v3/application/shops/{shop_id}/listings
GET /v3/application/listings/{listing_id}
POST /v3/application/shops/{shop_id}/listings
PUT /v3/application/listings/{listing_id}
DELETE /v3/application/listings/{listing_id}
```

**Query params for GET listings:**

| Parameter | Description |
|-----------|-------------|
| `state` | Filter by state: `active`, `draft`, `sold_out`, `expired` |
| `sort_on` | Sort by: `created`, `price`, `updated`, `score` |
| `sort_order` | `asc` or `desc` |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |
| `section_id` | Filter by shop section |
| `q` | Search title and description |

**POST body (create listing):**

```json
{
  "title": "Handmade Ceramic Mug",
  "description": "Wheel-thrown stoneware mug...",
  "price": 32.00,
  "quantity": 10,
  "who_made": "i_did",
  "when_made": "2020_2026",
  "taxonomy_id": 2078,
  "tags": ["ceramic mug", "handmade", "pottery"],
  "materials": ["stoneware clay", "glaze"],
  "shipping_profile_id": 50001,
  "return_policy_id": 60001
}
```

**PUT body (update listing):**

```json
{
  "price": 35.00,
  "quantity": 15,
  "state": "active"
}
```

### Listing Images

```
GET /v3/application/listings/{listing_id}/images
GET /v3/application/listings/{listing_id}/images/{image_id}
DELETE /v3/application/listings/{listing_id}/images/{image_id}
```

### Receipts (Orders)

```
GET /v3/application/shops/{shop_id}/receipts
GET /v3/application/shops/{shop_id}/receipts/{receipt_id}
PUT /v3/application/shops/{shop_id}/receipts/{receipt_id}
```

**Query params for GET receipts:**

| Parameter | Description |
|-----------|-------------|
| `status` | Filter by status: `paid`, `completed`, `shipped`, `open`, `cancelled`, `return_requested` |
| `min_created` | Filter from date (ISO format) |
| `max_created` | Filter to date (ISO format) |
| `was_shipped` | `true` or `false` |
| `was_paid` | `true` or `false` |
| `sort_on` | `created` or `updated` |
| `sort_order` | `asc` or `desc` |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results |

**PUT body (mark shipped):**

```json
{
  "shipping_carrier": "USPS",
  "tracking_code": "9400111899223100456789",
  "was_shipped": true
}
```

### Transactions (Line Items)

```
GET /v3/application/shops/{shop_id}/receipts/{receipt_id}/transactions
GET /v3/application/shops/{shop_id}/transactions/{transaction_id}
```

### Reviews

```
GET /v3/application/shops/{shop_id}/reviews
GET /v3/application/listings/{listing_id}/reviews
```

**Query params for GET reviews:**

| Parameter | Description |
|-----------|-------------|
| `listing_id` | Filter by listing (on shop-level endpoint) |
| `min_rating` | Minimum star rating (1–5) |
| `limit` | Max results |
| `offset` | Skip N results |

### Shipping Profiles

```
GET /v3/application/shops/{shop_id}/shipping-profiles
GET /v3/application/shops/{shop_id}/shipping-profiles/{profile_id}
```

### Return Policies

```
GET /v3/application/shops/{shop_id}/return-policies
GET /v3/application/shops/{shop_id}/return-policies/{policy_id}
```

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /v3/application/shops/{shop_id}` to load shop profile and context.
3. `GET /v3/application/shops/{shop_id}/sections` to understand shop organization.
4. `GET /v3/application/shops/{shop_id}/listings` to browse the product catalog; add `?state=draft` to find unpublished items.
5. `GET /v3/application/listings/{listing_id}` for full details on a specific listing.
6. `GET /v3/application/shops/{shop_id}/receipts?status=paid&was_shipped=false` to find orders that need shipping.
7. `PUT /v3/application/shops/{shop_id}/receipts/{receipt_id}` with tracking info to mark shipped.
8. `GET /v3/application/shops/{shop_id}/reviews?min_rating=1&limit=5` to check for negative reviews needing attention.
9. `PUT /v3/application/listings/{listing_id}` to update pricing or quantity based on demand.

## Bundled Resources

### Scripts

- **`scripts/fetch_etsy_data.py`** — Helper script to list listings, view receipts, check reviews, and inspect shop details. Run `python3 scripts/fetch_etsy_data.py --help` for usage.

### References

- **`references/etsy-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
