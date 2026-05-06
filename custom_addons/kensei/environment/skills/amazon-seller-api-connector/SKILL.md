---
name: amazon-seller-api-connector
description: >
  Use when managing an Amazon seller account — listing products, processing orders,
  tracking inventory, analyzing pricing, handling returns, or monitoring account
  health via the Amazon Selling Partner API HTTP endpoints.
---

# Amazon Selling Partner API Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `AMAZON_SELLER_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### Seller Account

```
GET /sellers/v1/account
GET /sellers/v1/account/health
GET /notifications/v1/notifications
```

**Query params for GET notifications:**

| Parameter | Description |
|-----------|-------------|
| `severity` | Filter by severity: `WARNING`, `CRITICAL`, `INFO` |

### Catalog Items

```
GET /catalog/2022-04-01/items
GET /catalog/2022-04-01/items/{asin}
```

**Query params for GET items:**

| Parameter | Description |
|-----------|-------------|
| `keywords` | Search title and description |
| `identifiers` | Comma-separated ASINs or SKUs |
| `identifiersType` | `ASIN` or `SKU` |
| `pageSize` | Max results (1–20, default 10) |
| `marketplaceIds` | Marketplace ID (default `ATVPDKIKX0DER`) |
| `includedData` | Data to include: `summaries`, `images`, `attributes` |

### Listings Items

```
GET  /listings/2021-08-01/items/{sellerId}/{sku}
PUT  /listings/2021-08-01/items/{sellerId}/{sku}
PATCH /listings/2021-08-01/items/{sellerId}/{sku}
DELETE /listings/2021-08-01/items/{sellerId}/{sku}
```

**PUT body (create or full update):**

```json
{
  "productType": "USB_CABLE",
  "title": "VoltEdge 6ft USB-C Cable",
  "description": "Premium braided USB-C cable with 100W PD support.",
  "brand": "VoltEdge Tech",
  "bulletPoints": ["100W Power Delivery", "Braided nylon", "6ft length"],
  "price": 16.99,
  "quantity": 50,
  "fulfillmentChannel": "AFN",
  "condition": "NEW",
  "category": "Electronics"
}
```

**PATCH body (partial update):**

```json
{
  "price": 14.99,
  "quantity": 75
}
```

### Orders

```
GET  /orders/v0/orders
GET  /orders/v0/orders/{orderId}
GET  /orders/v0/orders/{orderId}/orderItems
POST /orders/v0/orders/{orderId}/shipmentConfirmation
```

**Query params for GET orders:**

| Parameter | Description |
|-----------|-------------|
| `CreatedAfter` | Filter from date (ISO 8601) |
| `CreatedBefore` | Filter to date (ISO 8601) |
| `OrderStatuses` | Comma-separated: `Pending`, `Unshipped`, `Shipped`, `Canceled` |
| `FulfillmentChannels` | `AFN` (FBA) or `MFN` (merchant) |
| `MarketplaceIds` | Marketplace ID |
| `MaxResultsPerPage` | Max results (1–100, default 100) |

**POST shipmentConfirmation body:**

```json
{
  "carrierCode": "UPS",
  "trackingNumber": "1Z999AA10123456784",
  "shipDate": "2026-04-29T10:00:00Z"
}
```

### Inventory

```
GET /fba/inventory/v1/summaries
PUT /fba/inventory/v1/items/{sellerSku}
```

**Query params for GET summaries:**

| Parameter | Description |
|-----------|-------------|
| `sellerSkus` | Comma-separated SKU filter |
| `granularityType` | `Marketplace` (default) |
| `granularityId` | Marketplace ID |
| `marketplaceIds` | Marketplace ID |

**PUT body (update quantity):**

```json
{
  "sellerSku": "VE-CHRG-USB3",
  "quantity": 75
}
```

### Reports

```
GET  /reports/2021-06-30/reports
GET  /reports/2021-06-30/reports/{reportId}
POST /reports/2021-06-30/reports
```

**Query params for GET reports:**

| Parameter | Description |
|-----------|-------------|
| `reportTypes` | Comma-separated report type codes |
| `processingStatuses` | `DONE`, `IN_PROGRESS`, `IN_QUEUE` |

**POST body (create report):**

```json
{
  "reportType": "GET_FLAT_FILE_OPEN_LISTINGS_DATA",
  "dataStartTime": "2026-05-01T00:00:00Z",
  "dataEndTime": "2026-05-06T23:59:59Z",
  "marketplaceIds": ["ATVPDKIKX0DER"]
}
```

### Product Pricing

```
GET /products/pricing/v0/competitivePrice
GET /products/pricing/v0/items/{Asin}/offers
```

**Query params for GET competitivePrice:**

| Parameter | Description |
|-----------|-------------|
| `Asin` | ASIN to look up |
| `Sku` | SKU to look up (alternative to Asin) |
| `MarketplaceId` | Marketplace ID |
| `ItemType` | `Asin` or `Sku` |

**Query params for GET item offers:**

| Parameter | Description |
|-----------|-------------|
| `MarketplaceId` | Marketplace ID |
| `ItemCondition` | `New`, `Used`, etc. |

### Returns

```
GET  /returns/v0/returns
GET  /returns/v0/returns/{returnId}
POST /returns/v0/returns/{returnId}/authorize
POST /returns/v0/returns/{returnId}/close
```

**Query params for GET returns:**

| Parameter | Description |
|-----------|-------------|
| `status` | `Authorized`, `Completed`, `Closed` |
| `orderId` | Filter by Amazon order ID |

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /sellers/v1/account` to load seller profile and context.
3. `GET /sellers/v1/account/health` to check account performance metrics for any issues.
4. `GET /notifications/v1/notifications` to identify urgent items needing attention.
5. `GET /catalog/2022-04-01/items` to browse the product catalog; use `?keywords=` to search.
6. `GET /orders/v0/orders?OrderStatuses=Unshipped` to find orders needing fulfillment.
7. `POST /orders/v0/orders/{orderId}/shipmentConfirmation` to mark orders as shipped.
8. `GET /fba/inventory/v1/summaries` to check stock levels; look for low/zero quantities.
9. `GET /products/pricing/v0/competitivePrice?Asin=...` to analyze Buy Box competitiveness.
10. `GET /returns/v0/returns?status=Authorized` to find returns pending resolution.

## Bundled Resources

### Scripts

- **`scripts/fetch_amazon_seller_data.py`** — Helper script to list catalog items, view orders, check inventory, and inspect account health. Run `python3 scripts/fetch_amazon_seller_data.py --help` for usage.

### References

- **`references/amazon-seller-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
