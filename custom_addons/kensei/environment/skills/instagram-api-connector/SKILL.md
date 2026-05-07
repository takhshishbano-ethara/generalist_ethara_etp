---
name: instagram-api-connector
description: >
  Use when managing an Instagram Business/Creator account — viewing media posts,
  reading comments, checking insights/analytics, managing stories, searching
  hashtags, publishing content, or moderating comments via the Instagram Graph
  API HTTP endpoints.
---

# Instagram Graph API Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `INSTAGRAM_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### User / Account

```
GET /{user_id}
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `fields` | Comma-separated list of fields to return (e.g. `id,username,name,followers_count`) |

### Media

```
GET /{user_id}/media
GET /media/{media_id}
DELETE /media/{media_id}
```

**Query params for GET /{user_id}/media:**

| Parameter | Description |
|-----------|-------------|
| `media_type` | Filter by type: `IMAGE`, `VIDEO`, `CAROUSEL_ALBUM` |
| `fields` | Comma-separated fields to return |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

### Carousel Children

```
GET /media/{media_id}/children
```

Returns child media items for a CAROUSEL_ALBUM post.

### Comments

```
GET /media/{media_id}/comments
GET /comment/{comment_id}
GET /comment/{comment_id}/replies
POST /media/{media_id}/comments
DELETE /media/{media_id}/comments/{comment_id}
PUT /media/{media_id}/comments/{comment_id}/hide
```

**Query params for GET comments:**

| Parameter | Description |
|-----------|-------------|
| `fields` | Comma-separated fields to return |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

**POST body (create comment/reply):**

```json
{
  "message": "Thanks for the love! See you this weekend!",
  "parent_id": "17800001003"
}
```

**PUT body (hide/unhide comment):**

```json
{
  "hide": true
}
```

### Stories

```
GET /{user_id}/stories
GET /stories/{story_id}
```

### Insights / Analytics

```
GET /{user_id}/insights
GET /media/{media_id}/insights
```

**Query params for GET /{user_id}/insights:**

| Parameter | Description |
|-----------|-------------|
| `metric` | Comma-separated metrics: `impressions`, `reach`, `follower_count`, `profile_views`, `website_clicks` |
| `period` | Time period: `day`, `week`, `days_28`, `lifetime` |

**Query params for GET /media/{media_id}/insights:**

| Parameter | Description |
|-----------|-------------|
| `metric` | Comma-separated metrics: `impressions`, `reach`, `engagement`, `saved`, `shares`, `profile_visits`, `follows` |

### Hashtags

```
GET /ig_hashtag_search
GET /hashtag/{hashtag_id}
GET /hashtag/{hashtag_id}/recent_media
```

**Query params for GET /ig_hashtag_search:**

| Parameter | Description |
|-----------|-------------|
| `q` | Search query (required) |

**Query params for GET /hashtag/{hashtag_id}/recent_media:**

| Parameter | Description |
|-----------|-------------|
| `user_id` | The user ID performing the search (required) |
| `fields` | Comma-separated fields |
| `limit` | Max results (1–50, default 25) |

### Mentions / Tags

```
GET /{user_id}/tags
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `fields` | Comma-separated fields |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results |

### Content Publishing

```
POST /{user_id}/media
POST /{user_id}/media_publish
GET /container/{container_id}
```

**POST body (create media container):**

```json
{
  "image_url": "https://example.com/new_coffee_photo.jpg",
  "caption": "Fresh roast Friday! Our new Costa Rica Tarrazu is here \u2615\n\n#specialtycoffee #freshroast",
  "media_type": "IMAGE"
}
```

**POST body (publish container):**

```json
{
  "creation_id": "17920001001"
}
```

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /{user_id}` to load account profile (followers, bio, media count).
3. `GET /{user_id}/media` to browse recent posts; add `?media_type=VIDEO` to filter reels.
4. `GET /media/{media_id}` for full details on a specific post (caption, likes, comments count).
5. `GET /media/{media_id}/comments` to read audience engagement on a post.
6. `GET /{user_id}/insights` to check overall account performance metrics.
7. `GET /media/{media_id}/insights` for per-post analytics (reach, saves, shares).
8. `POST /media/{media_id}/comments` to reply to a comment or engage with audience.
9. `PUT /media/{media_id}/comments/{comment_id}/hide` to moderate spam or inappropriate comments.
10. `POST /{user_id}/media` then `POST /{user_id}/media_publish` to schedule and publish new content.

## Bundled Resources

### Scripts

- **`scripts/fetch_instagram_data.py`** — Helper script to list media, view comments, check insights, search hashtags, and inspect account details. Run `python3 scripts/fetch_instagram_data.py --help` for usage.

### References

- **`references/instagram-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
