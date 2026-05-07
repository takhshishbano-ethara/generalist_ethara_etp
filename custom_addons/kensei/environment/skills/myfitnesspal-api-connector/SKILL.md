---
name: myfitnesspal-api-connector
description: >
  Use when tracking nutrition, managing food diary entries, logging exercises,
  monitoring weight progress, or querying the MyFitnessPal HTTP endpoints for
  a user's health and fitness data.
---

# MyFitnessPal API Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `MYFITNESSPAL_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### User Profile

```
GET /v1/user/profile
PUT /v1/user/profile
```

**PUT body (partial update):**

```json
{
  "activity_level": "very_active",
  "daily_calorie_goal": 2000
}
```

### Goals

```
GET /v1/user/goals
PUT /v1/user/goals
```

**PUT body (update goals):**

```json
{
  "daily_calorie_goal": 1900,
  "macro_goals": {"protein_pct": 40, "carbs_pct": 35, "fat_pct": 25}
}
```

### Food Database

```
GET /v1/foods/search
GET /v1/foods/{food_id}
```

**Query params for GET /v1/foods/search:**

| Parameter | Description |
|-----------|-------------|
| `q` | Search food name or brand |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

### Food Diary

```
GET /v1/user/diary/{date}
GET /v1/user/diary
POST /v1/user/diary
PUT /v1/user/diary/{entry_id}
DELETE /v1/user/diary/{entry_id}
```

**Query params for GET /v1/user/diary/{date}:**

| Parameter | Description |
|-----------|-------------|
| `meal` | Filter by meal slot: `Breakfast`, `Lunch`, `Dinner`, `Snacks` |

**Query params for GET /v1/user/diary (range):**

| Parameter | Description |
|-----------|-------------|
| `start_date` | Start date (YYYY-MM-DD, required) |
| `end_date` | End date (YYYY-MM-DD, required) |

**POST body (log food entry):**

```json
{
  "date": "2025-04-28",
  "meal": "Lunch",
  "food_id": 1,
  "servings": 1.5
}
```

**PUT body (update entry):**

```json
{
  "servings": 2.0,
  "meal": "Dinner"
}
```

### Nutrition Summary

```
GET /v1/user/nutrition/{date}
GET /v1/user/nutrition/weekly/{end_date}
GET /v1/user/progress
```

**Query params for GET /v1/user/progress:**

| Parameter | Description |
|-----------|-------------|
| `days` | Number of days to show (1–90, default 30) |

### Exercise Types

```
GET /v1/exercises/types
GET /v1/exercises/types/{exercise_type_id}
```

**Query params for GET /v1/exercises/types:**

| Parameter | Description |
|-----------|-------------|
| `category` | Filter by category: `cardio`, `strength`, `flexibility` |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

### Exercise Log

```
GET /v1/user/exercises
GET /v1/user/exercises/{exercise_id}
POST /v1/user/exercises
```

**Query params for GET /v1/user/exercises:**

| Parameter | Description |
|-----------|-------------|
| `start_date` | Filter from date (YYYY-MM-DD) |
| `end_date` | Filter to date (YYYY-MM-DD) |
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

**POST body (log exercise):**

```json
{
  "date": "2025-04-28",
  "exercise_type_id": 3,
  "duration_minutes": 30,
  "calories_burned": 240,
  "notes": "Evening ride around the neighborhood"
}
```

### Weight Log

```
GET /v1/user/weight
GET /v1/user/weight/{weight_id}
POST /v1/user/weight
```

**Query params for GET /v1/user/weight:**

| Parameter | Description |
|-----------|-------------|
| `limit` | Max results (1–100, default 25) |
| `offset` | Skip N results (default 0) |

**POST body (log weight):**

```json
{
  "date": "2025-04-29",
  "weight_lbs": 191.5,
  "notes": "Morning weigh-in"
}
```

### Water Intake

```
GET /v1/user/water/{date}
POST /v1/user/water
PUT /v1/user/water/{date}
```

**POST body (log water):**

```json
{
  "date": "2025-04-29",
  "cups": 8,
  "notes": "Good hydration day"
}
```

**PUT body (update water):**

```json
{
  "cups": 10,
  "notes": "Updated after workout"
}
```

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /v1/user/profile` to load user context (name, goals, current weight).
3. `GET /v1/user/goals` to understand daily calorie and macro targets.
4. `GET /v1/user/diary/2025-04-28` to view today's food log organized by meal.
5. `GET /v1/user/nutrition/2025-04-28` to see daily totals vs goals and remaining budget.
6. `GET /v1/foods/search?q=chicken` to find foods in the database before logging.
7. `POST /v1/user/diary` to log a new food entry to a meal slot.
8. `GET /v1/user/exercises?start_date=2025-04-22&end_date=2025-04-28` to review recent exercise activity.
9. `GET /v1/user/progress?days=7` to check calorie/macro trends over the past week.
10. `GET /v1/user/weight` to review weight trend and track progress toward goal.

## Bundled Resources

### Scripts

- **`scripts/fetch_myfitnesspal_data.py`** — Helper script to query diary, foods, exercises, weight, water, and nutrition data. Run `python3 scripts/fetch_myfitnesspal_data.py --help` for usage.

### References

- **`references/myfitnesspal-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
