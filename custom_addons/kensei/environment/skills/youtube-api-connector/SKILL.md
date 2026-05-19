---
name: youtube-api-connector
description: >
  Use when managing a YouTube channel — listing/updating videos, managing playlists,
  moderating comments, searching content, viewing analytics, or managing channel
  settings via the YouTube Data API v3 HTTP endpoints.
---

# YouTube Data API v3 Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `YOUTUBE_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### Channels

```
GET /youtube/v3/channels
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `id` | Channel ID (default: `UC_EquineHealthChannel`) |
| `part` | Resource parts to include (default: all) |

### Videos

```
GET /youtube/v3/videos
PUT /youtube/v3/videos
DELETE /youtube/v3/videos
```

**Query params for GET videos:**

| Parameter | Description |
|-----------|-------------|
| `id` | Comma-separated video IDs |
| `channelId` | Filter by channel |
| `part` | Resource parts: `snippet,contentDetails,statistics,status` |
| `maxResults` | Max results (1–50, default 25) |

**PUT body (update video):**

```json
{
  "id": "vid_005",
  "snippet": {
    "title": "Updated Video Title",
    "description": "New description",
    "tags": ["tag1", "tag2"],
    "categoryId": "27"
  },
  "status": {
    "privacyStatus": "public",
    "embeddable": true
  }
}
```

**DELETE query param:** `?id=vid_030`

### Playlists

```
GET /youtube/v3/playlists
POST /youtube/v3/playlists
PUT /youtube/v3/playlists
DELETE /youtube/v3/playlists
```

**Query params for GET playlists:**

| Parameter | Description |
|-----------|-------------|
| `id` | Comma-separated playlist IDs |
| `channelId` | Filter by channel |
| `part` | Resource parts: `snippet,contentDetails,status` |
| `maxResults` | Max results (1–50, default 25) |

**POST body (create playlist):**

```json
{
  "snippet": {
    "title": "New Playlist Title",
    "description": "Playlist description"
  },
  "status": {
    "privacyStatus": "public"
  }
}
```

**PUT body (update playlist):**

```json
{
  "id": "PL_005",
  "snippet": {
    "title": "Updated Playlist Title",
    "description": "Updated description"
  },
  "status": {
    "privacyStatus": "unlisted"
  }
}
```

### Playlist Items

```
GET /youtube/v3/playlistItems
POST /youtube/v3/playlistItems
PUT /youtube/v3/playlistItems
DELETE /youtube/v3/playlistItems
```

**Query params for GET playlistItems:**

| Parameter | Description |
|-----------|-------------|
| `playlistId` | Required — playlist to list items from |
| `part` | Resource parts: `snippet,contentDetails` |
| `maxResults` | Max results (1–50, default 25) |

**POST body (add video to playlist):**

```json
{
  "snippet": {
    "playlistId": "PL_001",
    "resourceId": {
      "kind": "youtube#video",
      "videoId": "vid_020"
    },
    "position": 2
  }
}
```

**PUT body (reorder item):**

```json
{
  "id": "PLI_003",
  "snippet": {
    "position": 5
  }
}
```

### Comment Threads

```
GET /youtube/v3/commentThreads
POST /youtube/v3/commentThreads
```

**Query params for GET commentThreads:**

| Parameter | Description |
|-----------|-------------|
| `videoId` | Filter by video |
| `channelId` | Filter by channel |
| `part` | Resource parts: `snippet,replies` |
| `maxResults` | Max results (1–100, default 20) |
| `moderationStatus` | `published`, `heldForReview`, or `rejected` |

**POST body (create top-level comment):**

```json
{
  "snippet": {
    "videoId": "vid_001",
    "topLevelComment": {
      "snippet": {
        "textOriginal": "Great video! Thanks for sharing."
      }
    }
  }
}
```

### Comments

```
GET /youtube/v3/comments
POST /youtube/v3/comments
PUT /youtube/v3/comments
DELETE /youtube/v3/comments
POST /youtube/v3/comments/setModerationStatus
```

**Query params for GET comments:**

| Parameter | Description |
|-----------|-------------|
| `parentId` | Required — parent comment ID to get replies |
| `part` | Resource parts: `snippet` |
| `maxResults` | Max results (1–100, default 20) |

**POST body (reply to comment):**

```json
{
  "snippet": {
    "parentId": "cmt_005",
      "textOriginal": "Great question! I covered that in the nutrition series."
  }
}
```

**PUT body (edit comment):**

```json
{
  "id": "cmt_003",
  "snippet": {
    "textOriginal": "Updated comment text."
  }
}
```

**Set moderation status query params:** `?id=cmt_028&moderationStatus=published`

### Search

```
GET /youtube/v3/search
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `q` | Search query keyword |
| `channelId` | Restrict to a channel |
| `part` | Resource parts: `snippet` |
| `order` | Sort: `relevance`, `date`, `viewCount`, `rating` |
| `maxResults` | Max results (1–50, default 25) |
| `type` | Resource type filter (default: `video`) |

### Video Categories

```
GET /youtube/v3/videoCategories
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `regionCode` | Region (default: `US`) |
| `part` | Resource parts: `snippet` |

### Captions

```
GET /youtube/v3/captions
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `videoId` | Required — video to list captions for |
| `part` | Resource parts: `snippet` |

### Channel Sections

```
GET /youtube/v3/channelSections
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `channelId` | Required — channel ID |
| `part` | Resource parts: `snippet,contentDetails` |

### Analytics

```
GET /youtube/analytics/v2/reports
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `ids` | Channel identifier (e.g. `channel==UC_EquineHealthChannel`) |
| `filters` | Video filter (e.g. `video==vid_001`) |
| `metrics` | Metrics to return (e.g. `views,estimatedMinutesWatched,likes`) |
| `startDate` | Start date (ISO format) |
| `endDate` | End date (ISO format) |

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /youtube/v3/channels?id=UC_EquineHealthChannel` to load channel profile, stats, and branding.
3. `GET /youtube/v3/videos?channelId=UC_EquineHealthChannel&maxResults=10` to browse recent uploads.
4. `GET /youtube/v3/videos?id=vid_001` for full details on a specific video (snippet, stats, status).
5. `GET /youtube/v3/search?q=colic&channelId=UC_EquineHealthChannel` to find videos matching a keyword.
6. `GET /youtube/v3/commentThreads?videoId=vid_001&moderationStatus=heldForReview` to check comments needing moderation.
7. `POST /youtube/v3/comments` to reply to viewer questions on popular videos.
8. `PUT /youtube/v3/videos` to update a video's title, description, or tags for SEO improvement.
9. `GET /youtube/analytics/v2/reports?filters=video==vid_001` to check performance metrics for a video.
10. `GET /youtube/v3/playlists?channelId=UC_EquineHealthChannel` to review playlist organization and add videos.

## Bundled Resources

### Scripts

- **`scripts/fetch_youtube_data.py`** — Helper script to list videos, playlists, comments, search content, and view analytics. Run `python3 scripts/fetch_youtube_data.py --help` for usage.

### References

- **`references/youtube-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
