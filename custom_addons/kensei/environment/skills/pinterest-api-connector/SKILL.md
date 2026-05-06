---
name: pinterest-api-connector
description: >
  Use when managing a Pinterest business account — creating/updating pins,
  organizing boards and sections, viewing analytics, searching content, or
  managing ad campaigns via the Pinterest API v5 HTTP endpoints.
---

# Pinterest API v5 Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `PINTEREST_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### User Account

```
GET /v5/user_account
GET /v5/user_account/analytics
```

**Query params for GET analytics:**

| Parameter | Description |
|-----------|-------------|
| `start_date` | Start date (YYYY-MM-DD) |
| `end_date` | End date (YYYY-MM-DD) |

### Boards

```
GET /v5/boards
GET /v5/boards/{board_id}
POST /v5/boards
PATCH /v5/boards/{board_id}
DELETE /v5/boards/{board_id}
GET /v5/boards/{board_id}/pins
```

**Query params for GET boards:**

| Parameter | Description |
|-----------|-------------|
| `privacy` | Filter by privacy: `PUBLIC`, `SECRET` |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

**POST body (create board):**

```json
{
  "name": "Outdoor Living Spaces",
  "description": "Patio and garden design ideas",
  "privacy": "PUBLIC"
}
```

**PATCH body (update board):**

```json
{
  "description": "Updated description for this board"
}
```

### Board Sections

```
GET /v5/boards/{board_id}/sections
POST /v5/boards/{board_id}/sections
GET /v5/boards/{board_id}/sections/{section_id}/pins
```

**POST body (create section):**

```json
{
  "name": "Electrical Projects"
}
```

### Pins

```
GET /v5/pins
GET /v5/pins/{pin_id}
POST /v5/pins
PATCH /v5/pins/{pin_id}
DELETE /v5/pins/{pin_id}
GET /v5/pins/{pin_id}/analytics
```

**Query params for GET pins:**

| Parameter | Description |
|-----------|-------------|
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

**Query params for GET pin analytics:**

| Parameter | Description |
|-----------|-------------|
| `start_date` | Start date (YYYY-MM-DD) |
| `end_date` | End date (YYYY-MM-DD) |

**POST body (create pin):**

```json
{
  "board_id": "board_1001",
  "title": "Boho Living Room Makeover",
  "description": "Transform your space with boho-chic design tips #boho #livingroom",
  "link": "https://www.cozynestinteriors.com/blog/boho-makeover",
  "media_type": "image",
  "alt_text": "A boho-styled living room with macrame and plants"
}
```

**PATCH body (update pin):**

```json
{
  "title": "Updated Pin Title",
  "description": "New description text"
}
```

### Search

```
GET /v5/search/pins
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `query` | Search term (required) |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

### Media

```
GET /v5/media/{media_id}
```

### Ad Accounts

```
GET /v5/ad_accounts
GET /v5/ad_accounts/{ad_account_id}
GET /v5/ad_accounts/{ad_account_id}/campaigns
```

**Query params for GET ad accounts:**

| Parameter | Description |
|-----------|-------------|
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

**Query params for GET campaigns:**

| Parameter | Description |
|-----------|-------------|
| `status` | Filter by status: `ACTIVE`, `PAUSED` |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /v5/user_account` to load the business account profile and context.
3. `GET /v5/boards` to see all boards and their organization.
4. `GET /v5/boards/{board_id}/pins` to browse pins on a specific board.
5. `GET /v5/pins/{pin_id}/analytics` to check performance of specific pins.
6. `GET /v5/user_account/analytics` to review overall account metrics.
7. `GET /v5/search/pins?query=keyword` to find pins matching a topic.
8. `POST /v5/pins` to create new content on a board.
9. `PATCH /v5/pins/{pin_id}` to update pin titles or descriptions for better SEO.
10. `GET /v5/ad_accounts/{ad_account_id}/campaigns` to review ad campaign performance.

## Bundled Resources

### Scripts

- **`scripts/fetch_pinterest_data.py`** — Helper script to list pins, boards, analytics, and ad campaigns. Run `python3 scripts/fetch_pinterest_data.py --help` for usage.

### References

- **`references/pinterest-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
