# Documenso Connector — Public GET APIs

Two authenticated GET endpoints exposed by `documenso_connector` on top of `api_auth_gateway`. Both return compliance and contract documents from `documenso.contract`.

- Base path: `/api/v1/documenso/`
- Auth: `access_token` request header (obtain via `/api/v1/auth_token` from `api_auth_gateway`)
- Response envelope produced by `return_Response`: `{ "message", "status_code", "errors", ...data }`

**Note:** Both endpoints currently return dummy data. Real Odoo lookup logic is preserved (commented) in `controllers/main.py` and can be restored by uncommenting the block below the dummy return.

---

## 1. List Documents

```
GET /api/v1/documenso/documents
```

### Query Parameters (all optional)

| Param          | Type    | Values                                                                  | Description                                    |
| -------------- | ------- | ----------------------------------------------------------------------- | ---------------------------------------------- |
| `doc_class`    | string  | `contract` \| `compliance`                                              | Document type filter                           |
| `status`       | string  | `DRAFT` \| `SENT` \| `OPENED` \| `SIGNED` \| `REJECTED` \| `CANCELLED` \| `EXPIRED` | Lifecycle status filter          |
| `employee_id`  | integer | `hr.employee` ID                                                        | Filter by employee                             |
| `template_id`  | integer | `documenso.template` ID                                                 | Filter by template                             |
| `documenso_id` | string  | exact match                                                             | Documenso side ID                              |
| `search`       | string  | free text                                                               | Matches `name`, employee name, employee email  |
| `page`         | integer | `>= 1`, default `1`                                                     | Page number                                    |
| `limit`        | integer | `1..100`, default `20`                                                  | Page size                                      |

### Example URLs

```
# All documents (no filter)
GET /api/v1/documenso/documents

# Filter by employee AND doc type (both filters together)
GET /api/v1/documenso/documents?employee_id=11&doc_class=contract

# Filter by employee AND doc type - compliance
GET /api/v1/documenso/documents?employee_id=11&doc_class=compliance

# Employee + doc type + status combined
GET /api/v1/documenso/documents?employee_id=11&doc_class=contract&status=SIGNED

# Employee's compliance docs, paginated
GET /api/v1/documenso/documents?employee_id=11&doc_class=compliance&page=1&limit=20
```

### curl

```bash
# Employee 11 + contract docs
curl -H "access_token: $TOKEN" \
  "http://localhost:8069/api/v1/documenso/documents?employee_id=11&doc_class=contract"

# Employee 11 + compliance docs
curl -H "access_token: $TOKEN" \
  "http://localhost:8069/api/v1/documenso/documents?employee_id=11&doc_class=compliance"

# Employee 11 + signed contracts (all three filters)
curl -H "access_token: $TOKEN" \
  "http://localhost:8069/api/v1/documenso/documents?employee_id=11&doc_class=contract&status=SIGNED"

# Free text search
curl -H "access_token: $TOKEN" \
  "http://localhost:8069/api/v1/documenso/documents?search=aakash%40ethara.ai"
```

### Response

```json
{
  "message": "Success",
  "status_code": 200,
  "errors": [],
  "records": [
    {
      "id": 1,
      "name": "CT/2026/0001",
      "doc_class": "contract",
      "status": "SIGNED",
      "documenso_id": "doc_abc123",
      "envelope_id": "env_abc123",
      "signing_url": "https://documenso.example.com/sign/doc_abc123",
      "employee": {
        "id": 11,
        "name": "Aakash Vishwakarma",
        "email": "aakash@ethara.ai",
        "department_id": 3,
        "department_name": "Engineering"
      },
      "template": {
        "id": 5,
        "documenso_id": "tpl_offer_v2",
        "title": "Offer Letter v2"
      },
      "templates": [
        { "id": 5, "documenso_id": "tpl_offer_v2", "title": "Offer Letter v2" }
      ],
      "sent_at": "2026-07-01 10:15:30",
      "signed_at": "2026-07-02 14:22:10",
      "last_synced_at": "2026-07-02 14:23:00",
      "item_count": 1,
      "field_count": 6,
      "pdf_filename": "offer_letter_signed.pdf",
      "pdf_download_url": "http://localhost:8069/web/content/documenso.contract/1/pdf_binary/offer_letter_signed.pdf?download=true",
      "note": ""
    },
    {
      "id": 2,
      "name": "CM/2026/0002",
      "doc_class": "compliance",
      "status": "SENT",
      "documenso_id": "doc_xyz789",
      "envelope_id": "env_xyz789",
      "signing_url": "https://documenso.example.com/sign/doc_xyz789",
      "employee": {
        "id": 12,
        "name": "Priya Sharma",
        "email": "priya@ethara.ai",
        "department_id": 4,
        "department_name": "HR"
      },
      "template": {
        "id": 8,
        "documenso_id": "tpl_pos_v1",
        "title": "POSH Policy Acknowledgement"
      },
      "templates": [
        { "id": 8, "documenso_id": "tpl_pos_v1", "title": "POSH Policy Acknowledgement" }
      ],
      "sent_at": "2026-07-05 09:00:00",
      "signed_at": "",
      "last_synced_at": "2026-07-05 09:00:05",
      "item_count": 1,
      "field_count": 3,
      "pdf_filename": "",
      "pdf_download_url": "",
      "note": "Awaiting signature"
    }
  ],
  "pagination": {
    "total": 2,
    "page": 1,
    "limit": 20,
    "pages": 1
  }
}
```

---

## 2. Get Document Detail

```
GET /api/v1/documenso/documents/<int:doc_id>
```

### Path Parameter

| Param    | Type    | Description                    |
| -------- | ------- | ------------------------------ |
| `doc_id` | integer | `documenso.contract` record ID |

### Example URLs

```
GET /api/v1/documenso/documents/17
GET /api/v1/documenso/documents/42
```

### curl

```bash
curl -H "access_token: $TOKEN" \
  "http://localhost:8069/api/v1/documenso/documents/17"
```

### Response

```json
{
  "message": "Success",
  "status_code": 200,
  "errors": [],
  "record": {
    "id": 17,
    "name": "CT/2026/0017",
    "doc_class": "contract",
    "status": "SIGNED",
    "documenso_id": "doc_abc123",
    "envelope_id": "env_abc123",
    "signing_url": "https://documenso.example.com/sign/doc_abc123",
    "employee": {
      "id": 11,
      "name": "Aakash Vishwakarma",
      "email": "aakash@ethara.ai",
      "department_id": 3,
      "department_name": "Engineering"
    },
    "template": {
      "id": 5,
      "documenso_id": "tpl_offer_v2",
      "title": "Offer Letter v2"
    },
    "templates": [
      { "id": 5, "documenso_id": "tpl_offer_v2", "title": "Offer Letter v2" },
      { "id": 6, "documenso_id": "tpl_nda_v1", "title": "Mutual NDA v1" }
    ],
    "sent_at": "2026-07-01 10:15:30",
    "signed_at": "2026-07-02 14:22:10",
    "last_synced_at": "2026-07-02 14:23:00",
    "item_count": 2,
    "field_count": 6,
    "pdf_filename": "offer_letter_signed.pdf",
    "pdf_download_url": "http://localhost:8069/web/content/documenso.contract/17/pdf_binary/offer_letter_signed.pdf?download=true",
    "note": "",
    "items": [
      {
        "id": 101,
        "item_id": "item_offer_letter",
        "title": "Offer Letter",
        "category": "offer_letter",
        "sequence": 1,
        "pdf_filename": "offer_letter.pdf",
        "pdf_download_url": "http://localhost:8069/web/content/documenso.contract.item/101/pdf_binary/offer_letter.pdf?download=true",
        "redirect_url": "https://documenso.example.com/redirect/offer"
      },
      {
        "id": 102,
        "item_id": "item_nda",
        "title": "Mutual NDA",
        "category": "nda",
        "sequence": 2,
        "pdf_filename": "nda.pdf",
        "pdf_download_url": "http://localhost:8069/web/content/documenso.contract.item/102/pdf_binary/nda.pdf?download=true",
        "redirect_url": "https://documenso.example.com/redirect/nda"
      }
    ],
    "fields": [
      {
        "id": 201,
        "documenso_id": "fld_name",
        "label": "Full Name",
        "field_type": "TEXT",
        "value": "Aakash Vishwakarma",
        "inserted": true,
        "recipient_email": "aakash@ethara.ai",
        "page": 1
      },
      {
        "id": 202,
        "documenso_id": "fld_ctc",
        "label": "Annual CTC (INR)",
        "field_type": "NUMBER",
        "value": "2400000",
        "inserted": true,
        "recipient_email": "aakash@ethara.ai",
        "page": 1
      },
      {
        "id": 203,
        "documenso_id": "fld_join_date",
        "label": "Joining Date",
        "field_type": "DATE",
        "value": "2026-07-15",
        "inserted": true,
        "recipient_email": "aakash@ethara.ai",
        "page": 1
      },
      {
        "id": 204,
        "documenso_id": "fld_sig",
        "label": "Signature",
        "field_type": "SIGNATURE",
        "value": "",
        "inserted": true,
        "recipient_email": "aakash@ethara.ai",
        "page": 2
      }
    ]
  }
}
```

---

## Status Codes

| Code | Meaning                                              |
| ---- | ---------------------------------------------------- |
| 200  | Success                                              |
| 400  | Invalid query params (bad `doc_class`, `status`, `page`, `limit`) |
| 401  | Missing / expired / invalid `access_token`           |
| 404  | Document not found (detail endpoint only)            |

---

## Authentication

```bash
# 1. Obtain token
TOKEN=$(curl -s -X POST http://localhost:8069/api/v1/auth_token \
  -H "Content-Type: application/json" \
  -d '{"login": "admin", "password": "admin"}' | jq -r .access_token)

# 2. Use token
curl -H "access_token: $TOKEN" \
  "http://localhost:8069/api/v1/documenso/documents?employee_id=11&doc_class=contract"
```
