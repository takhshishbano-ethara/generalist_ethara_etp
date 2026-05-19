---
name: quickbooks-api-connector
description: >
  Use when managing QuickBooks Online accounting — creating invoices, recording
  payments, tracking expenses, managing customers/vendors, querying financial
  data, or generating reports (P&L, Balance Sheet, AR/AP Aging) via the
  QuickBooks Online API v3 HTTP endpoints.
---

# QuickBooks Online API v3 Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `QUICKBOOKS_API_URL` | Base URL for all API requests |
| `REALM_ID` | QuickBooks company realm ID (e.g. `4620816365272861350`) |

All paths below are relative to this URL. Replace `{realmId}` with your company's realm ID.

## Endpoints

### Health

```
GET /health
```

### Company Info

```
GET /v3/company/{realmId}/companyinfo/{companyId}
```

### Customers

```
GET /v3/company/{realmId}/customer/{customerId}
POST /v3/company/{realmId}/customer              (create: no Id in body)
POST /v3/company/{realmId}/customer              (update: Id + SyncToken in body)
```

**POST body (create):**

```json
{
  "DisplayName": "New Student Name",
  "GivenName": "Jane",
  "FamilyName": "Kim",
  "PrimaryEmailAddr": {"Address": "jane.kim@email.com"},
  "PrimaryPhone": {"FreeFormNumber": "(503) 555-0199"},
  "BillAddr": {"Line1": "742 Cedar Rd", "City": "Beaverton", "CountrySubDivisionCode": "OR", "PostalCode": "97005"}
}
```

**POST body (update):**

```json
{
  "Id": "1",
  "DisplayName": "Updated Name",
  "SyncToken": "0"
}
```

### Vendors

```
GET /v3/company/{realmId}/vendor/{vendorId}
POST /v3/company/{realmId}/vendor              (create/update — same pattern as Customer)
```

### Items (Products/Services)

```
GET /v3/company/{realmId}/item/{itemId}
POST /v3/company/{realmId}/item                (create/update)
```

**POST body (create):**

```json
{
  "Name": "Private Lesson",
  "Description": "One-on-one martial arts instruction",
  "Type": "Service",
  "UnitPrice": 60.00,
  "IncomeAccountRef": {"value": "3", "name": "Membership Income"}
}
```

### Accounts (Chart of Accounts)

```
GET /v3/company/{realmId}/account/{accountId}
```

### Invoices

```
GET /v3/company/{realmId}/invoice/{invoiceId}
GET /v3/company/{realmId}/invoice/{invoiceId}/pdf
POST /v3/company/{realmId}/invoice              (create/update)
POST /v3/company/{realmId}/invoice/{invoiceId}?operation=void
POST /v3/company/{realmId}/invoice/{invoiceId}?include=send
```

**POST body (create invoice):**

```json
{
  "CustomerRef": {"value": "1", "name": "Abrams, Derek"},
  "Line": [
    {
      "Amount": 95.00,
      "DetailType": "SalesItemLineDetail",
      "Description": "Monthly membership — Kendo M/W/F",
      "SalesItemLineDetail": {
        "ItemRef": {"value": "1", "name": "Monthly Membership"},
        "UnitPrice": 95.00,
        "Qty": 1
      }
    }
  ],
  "TxnDate": "2025-05-01",
  "DueDate": "2025-05-31"
}
```

### Bills (Vendor Bills)

```
GET /v3/company/{realmId}/bill/{billId}
POST /v3/company/{realmId}/bill                 (create)
POST /v3/company/{realmId}/bill/{billId}?operation=pay
```

**POST body (create bill):**

```json
{
  "VendorRef": {"value": "5", "name": "Bushido Supply Co."},
  "Line": [
    {
      "Amount": 245.00,
      "DetailType": "AccountBasedExpenseLineDetail",
      "Description": "Training equipment and uniforms",
      "AccountBasedExpenseLineDetail": {
        "AccountRef": {"value": "11", "name": "Supplies & Equipment"}
      }
    }
  ],
  "TxnDate": "2025-05-01",
  "DueDate": "2025-05-31"
}
```

### Payments

```
GET /v3/company/{realmId}/payment/{paymentId}
POST /v3/company/{realmId}/payment              (create)
```

**POST body (record payment against invoice):**

```json
{
  "CustomerRef": {"value": "4", "name": "Bakshi, Priya"},
  "TotalAmt": 95.00,
  "Line": [
    {
      "Amount": 95.00,
      "LinkedTxn": [{"TxnId": "5001", "TxnType": "Invoice"}]
    }
  ],
  "TxnDate": "2025-05-01"
}
```

### Estimates

```
GET /v3/company/{realmId}/estimate/{estimateId}
POST /v3/company/{realmId}/estimate             (create)
POST /v3/company/{realmId}/estimate/{estimateId}?operation=convert
```

### Expenses (Purchases)

```
GET /v3/company/{realmId}/purchase/{purchaseId}
POST /v3/company/{realmId}/purchase             (create)
```

**POST body (create expense):**

```json
{
  "AccountRef": {"value": "11", "name": "Supplies & Equipment"},
  "PaymentType": "Cash",
  "Line": [
    {
      "Amount": 42.50,
      "DetailType": "AccountBasedExpenseLineDetail",
      "Description": "Replacement belt testing boards",
      "AccountBasedExpenseLineDetail": {
        "AccountRef": {"value": "11", "name": "Supplies & Equipment"}
      }
    }
  ],
  "TxnDate": "2025-05-01"
}
```

### Query (SQL-like)

```
GET /v3/company/{realmId}/query?query=SELECT * FROM Invoice
GET /v3/company/{realmId}/query?query=SELECT * FROM Customer WHERE Active = true
GET /v3/company/{realmId}/query?query=SELECT * FROM Invoice WHERE Balance > '0'
GET /v3/company/{realmId}/query?query=SELECT * FROM Invoice WHERE Status = 'Overdue'
```

**Supported entities:** Customer, Vendor, Item, Account, Invoice, Bill, Payment, Estimate, Purchase

### Reports

```
GET /v3/company/{realmId}/reports/ProfitAndLoss?start_date=2025-01-01&end_date=2025-04-30
GET /v3/company/{realmId}/reports/BalanceSheet?start_date=2025-01-01&end_date=2025-04-30
GET /v3/company/{realmId}/reports/AgedReceivableDetail
GET /v3/company/{realmId}/reports/AgedPayableDetail
```

## Key Patterns

- All entity endpoints: `/v3/company/{realmId}/{entityName}`
- Field names: PascalCase (TxnDate, TotalAmt, DueDate, DocNumber)
- Reference fields: `{Name}Ref` with `{"value": "id", "name": "display name"}`
- Create vs Update: same POST path — presence of `Id` in body = update
- Query response: `{"QueryResponse": {"Entity": [...], "startPosition": 1, "maxResults": N, "totalCount": N}}`
- Single read response: `{"Entity": {...}}`
- Line items: `Line` array with `DetailType` discriminator (`SalesItemLineDetail` or `AccountBasedExpenseLineDetail`)
