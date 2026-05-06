---
name: linear-api-connector
description: >
  Use when managing engineering work in Linear — triaging issues, updating
  priorities/states/assignees, tracking sprints/cycles, querying projects, or
  searching across a team's backlog via the Linear REST API endpoints.
---

# Linear API Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `LINEAR_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### Teams

```
GET /v1/teams
GET /v1/teams/{team_id}
GET /v1/teams/{team_id}/members
GET /v1/teams/{team_id}/issues
GET /v1/teams/{team_id}/projects
GET /v1/teams/{team_id}/cycles
GET /v1/teams/{team_id}/workflow-states
GET /v1/teams/{team_id}/labels
```

**Query params for GET /v1/teams/{team_id}/issues:**

| Parameter | Description |
|-----------|-------------|
| `limit` | Max results (1–100, default 50) |
| `offset` | Skip N results (default 0) |

### Users

```
GET /v1/users
GET /v1/users/{user_id}
GET /v1/users/{user_id}/issues
```

**Query params for GET /v1/users/{user_id}/issues:**

| Parameter | Description |
|-----------|-------------|
| `limit` | Max results (1–100, default 50) |
| `offset` | Skip N results (default 0) |

### Workflow States

```
GET /v1/workflow-states
GET /v1/workflow-states/{state_id}
```

**Query params for GET /v1/workflow-states:**

| Parameter | Description |
|-----------|-------------|
| `teamId` | Filter states by team ID |
| `limit` | Max results (1–100, default 50) |
| `offset` | Skip N results (default 0) |

### Labels

```
GET /v1/labels
GET /v1/labels/{label_id}
POST /v1/labels
```

**Query params for GET /v1/labels:**

| Parameter | Description |
|-----------|-------------|
| `teamId` | Filter by team (includes shared labels) |
| `limit` | Max results (1–100, default 50) |
| `offset` | Skip N results (default 0) |

**POST body (create label):**

```json
{
  "name": "needs-review",
  "color": "#F2C94C",
  "description": "Issues requiring additional review",
  "teamId": "team-backend"
}
```

### Projects

```
GET /v1/projects
GET /v1/projects/{project_id}
POST /v1/projects
PUT /v1/projects/{project_id}
GET /v1/projects/{project_id}/issues
```

**POST body (create project):**

```json
{
  "name": "Mobile App MVP",
  "description": "Build first version of the mobile companion app",
  "state": "planned",
  "leadId": "user-06",
  "teamIds": ["team-frontend", "team-backend"],
  "startDate": "2025-06-01",
  "targetDate": "2025-09-30"
}
```

**PUT body (update project):**

```json
{
  "state": "completed",
  "targetDate": "2025-07-01"
}
```

### Cycles

```
GET /v1/cycles
GET /v1/cycles/{cycle_id}
POST /v1/cycles
GET /v1/cycles/{cycle_id}/issues
```

**Query params for GET /v1/cycles:**

| Parameter | Description |
|-----------|-------------|
| `teamId` | Filter by team |
| `status` | Filter by: `current`, `past`, `upcoming` |
| `limit` | Max results (1–100, default 50) |
| `offset` | Skip N results (default 0) |

**POST body (create cycle):**

```json
{
  "name": "Sprint 25",
  "teamId": "team-backend",
  "startsAt": "2025-05-19",
  "endsAt": "2025-06-01"
}
```

### Issues

```
GET /v1/issues
GET /v1/issues/{issue_id}
GET /v1/issues/search
POST /v1/issues
PUT /v1/issues/{issue_id}
DELETE /v1/issues/{issue_id}
```

**Query params for GET /v1/issues:**

| Parameter | Description |
|-----------|-------------|
| `stateId` | Filter by workflow state ID |
| `assigneeId` | Filter by assignee user ID |
| `projectId` | Filter by project ID |
| `cycleId` | Filter by cycle ID |
| `teamId` | Filter by team ID |
| `priority` | Filter by priority (0=None, 1=Urgent, 2=High, 3=Medium, 4=Low) |
| `labelId` | Filter by label ID |
| `limit` | Max results (1–100, default 50) |
| `offset` | Skip N results (default 0) |

**Query params for GET /v1/issues/search:**

| Parameter | Description |
|-----------|-------------|
| `q` | Search query (matches title, description, identifier) |
| `limit` | Max results |
| `offset` | Skip N results |

**POST body (create issue):**

```json
{
  "title": "Add rate limit headers to API responses",
  "teamId": "team-backend",
  "description": "Include X-RateLimit headers in all API responses",
  "priority": 3,
  "estimate": 2,
  "stateId": "state-bkd-todo",
  "assigneeId": "user-02",
  "projectId": "proj-api-v2",
  "cycleId": "cycle-bkd-2",
  "labelIds": ["label-feature", "label-api"],
  "dueDate": "2025-05-10"
}
```

**PUT body (update issue):**

```json
{
  "stateId": "state-bkd-inprogress",
  "assigneeId": "user-05",
  "priority": 2
}
```

### Comments

```
GET /v1/issues/{issue_id}/comments
GET /v1/comments/{comment_id}
POST /v1/comments
PUT /v1/comments/{comment_id}
DELETE /v1/comments/{comment_id}
```

**POST body (create comment):**

```json
{
  "body": "Started working on this. PR coming by end of day.",
  "issueId": "issue-01",
  "userId": "user-02"
}
```

**PUT body (update comment):**

```json
{
  "body": "Updated: PR is ready for review."
}
```

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /v1/teams` to list available teams and their IDs.
3. `GET /v1/teams/{team_id}/workflow-states` to understand the state machine for a team.
4. `GET /v1/issues?teamId={team_id}&stateId={state_id}` to find issues in a specific state (e.g., Todo, In Progress).
5. `GET /v1/issues/{issue_id}` to get full details on a specific issue.
6. `PUT /v1/issues/{issue_id}` to update state, assignee, priority, or labels.
7. `GET /v1/cycles?teamId={team_id}&status=current` to find the active sprint.
8. `GET /v1/cycles/{cycle_id}/issues` to see all work in the current sprint.
9. `GET /v1/issues/search?q={keyword}` to find issues by keyword across the workspace.
10. `POST /v1/comments` to leave a status update or question on an issue.

## Bundled Resources

### Scripts

- **`scripts/fetch_linear_data.py`** — Helper script to list issues, teams, users, projects, cycles, and search. Run `python3 scripts/fetch_linear_data.py --help` for usage.

### References

- **`references/linear-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
