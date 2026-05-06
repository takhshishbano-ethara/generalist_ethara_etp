---
name: google-classroom-api-connector
description: >
  Use when managing Google Classroom courses — creating assignments, grading
  submissions, tracking student work, posting announcements, or organizing
  course materials via the Google Classroom API HTTP endpoints.
---

# Google Classroom API Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `GOOGLE_CLASSROOM_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### Courses

```
GET /v1/courses
GET /v1/courses/{courseId}
POST /v1/courses
PATCH /v1/courses/{courseId}
POST /v1/courses/{courseId}:archive
```

**Query params for GET courses:**

| Parameter | Description |
|-----------|-------------|
| `courseStates` | Filter by state: `ACTIVE`, `ARCHIVED`, `PROVISIONED`, `DECLINED`, `SUSPENDED` |
| `pageSize` | Max results (1–100, default 20) |
| `pageToken` | Pagination token (offset as string) |

**POST body (create course):**

```json
{
  "name": "Data Structures (Spring 2025)",
  "section": "Period 7",
  "description": "Advanced data structures using Java",
  "room": "Room 214"
}
```

**PATCH body (update course):**

```json
{
  "description": "Updated course description",
  "room": "Room 215"
}
```

### Course Work

```
GET /v1/courses/{courseId}/courseWork
GET /v1/courses/{courseId}/courseWork/{courseWorkId}
POST /v1/courses/{courseId}/courseWork
PATCH /v1/courses/{courseId}/courseWork/{courseWorkId}
DELETE /v1/courses/{courseId}/courseWork/{courseWorkId}
```

**Query params for GET courseWork:**

| Parameter | Description |
|-----------|-------------|
| `topicId` | Filter by topic ID |
| `courseWorkStates` | Filter by state: `PUBLISHED`, `DRAFT` |
| `orderBy` | Sort: `dueDate desc`, `updateTime desc` |
| `pageSize` | Max results (1–100, default 20) |
| `pageToken` | Pagination token |

**POST body (create assignment):**

```json
{
  "title": "Recursion Challenge",
  "description": "Implement recursive solutions for factorial, fibonacci, and tower of hanoi.",
  "workType": "ASSIGNMENT",
  "maxPoints": 75,
  "topicId": "topic_107",
  "dueDate": {"year": 2025, "month": 5, "day": 9},
  "dueTime": {"hours": 23, "minutes": 59}
}
```

**POST body (create question):**

```json
{
  "title": "CSS Box Model Quiz",
  "description": "What is the difference between content-box and border-box?",
  "workType": "SHORT_ANSWER_QUESTION",
  "maxPoints": 10,
  "topicId": "topic_202"
}
```

**PATCH body (update coursework):**

```json
{
  "dueDate": {"year": 2025, "month": 5, "day": 5},
  "maxPoints": 120
}
```

### Topics

```
GET /v1/courses/{courseId}/topics
GET /v1/courses/{courseId}/topics/{topicId}
POST /v1/courses/{courseId}/topics
PATCH /v1/courses/{courseId}/topics/{topicId}
DELETE /v1/courses/{courseId}/topics/{topicId}
```

**POST body (create topic):**

```json
{
  "name": "Unit 8: 2D Arrays"
}
```

### Student Submissions

```
GET /v1/courses/{courseId}/courseWork/{courseWorkId}/studentSubmissions
GET /v1/courses/{courseId}/courseWork/{courseWorkId}/studentSubmissions/{submissionId}
PATCH /v1/courses/{courseId}/courseWork/{courseWorkId}/studentSubmissions/{submissionId}
POST /v1/courses/{courseId}/courseWork/{courseWorkId}/studentSubmissions/{submissionId}:return
POST /v1/courses/{courseId}/courseWork/{courseWorkId}/studentSubmissions/{submissionId}:reclaim
```

**Query params for GET studentSubmissions:**

| Parameter | Description |
|-----------|-------------|
| `states` | Filter by state: `NEW`, `CREATED`, `TURNED_IN`, `RETURNED`, `RECLAIMED_BY_STUDENT` |
| `late` | Filter late submissions: `true` or `false` |
| `pageSize` | Max results (1–100, default 20) |
| `pageToken` | Pagination token |

**PATCH body (grade submission):**

```json
{
  "assignedGrade": 45,
  "draftGrade": 45
}
```

### Students

```
GET /v1/courses/{courseId}/students
GET /v1/courses/{courseId}/students/{userId}
POST /v1/courses/{courseId}/students
DELETE /v1/courses/{courseId}/students/{userId}
```

**POST body (invite student):**

```json
{
  "emailAddress": "newstudent@westlake.edu",
  "fullName": "New Student"
}
```

### Teachers

```
GET /v1/courses/{courseId}/teachers
GET /v1/courses/{courseId}/teachers/{userId}
```

### Announcements

```
GET /v1/courses/{courseId}/announcements
GET /v1/courses/{courseId}/announcements/{announcementId}
POST /v1/courses/{courseId}/announcements
PATCH /v1/courses/{courseId}/announcements/{announcementId}
DELETE /v1/courses/{courseId}/announcements/{announcementId}
```

**Query params for GET announcements:**

| Parameter | Description |
|-----------|-------------|
| `announcementStates` | Filter by state: `PUBLISHED`, `DRAFT` |
| `pageSize` | Max results (1–100, default 20) |
| `pageToken` | Pagination token |

**POST body (create announcement):**

```json
{
  "text": "Extra credit: attend the CS guest speaker event Thursday at 3pm."
}
```

### Course Work Materials

```
GET /v1/courses/{courseId}/courseWorkMaterials
GET /v1/courses/{courseId}/courseWorkMaterials/{materialId}
POST /v1/courses/{courseId}/courseWorkMaterials
```

**POST body (create material):**

```json
{
  "title": "ArrayList Tutorial Video",
  "description": "Comprehensive video tutorial on Java ArrayList operations",
  "topicId": "topic_107",
  "materials": [{"link": {"url": "https://youtube.com/watch?v=example", "title": "ArrayList Tutorial"}}]
}
```

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /v1/courses?courseStates=ACTIVE` to load the teacher's active courses.
3. `GET /v1/courses/{courseId}/courseWork` to list assignments for a course; add `?topicId=topic_104` to filter by unit.
4. `GET /v1/courses/{courseId}/courseWork/{courseWorkId}/studentSubmissions?states=TURNED_IN` to find submissions awaiting grading.
5. `PATCH /v1/courses/{courseId}/courseWork/{courseWorkId}/studentSubmissions/{submissionId}` with grade to score a submission.
6. `POST /v1/courses/{courseId}/courseWork/{courseWorkId}/studentSubmissions/{submissionId}:return` to hand back a graded submission.
7. `GET /v1/courses/{courseId}/students` to view the class roster.
8. `POST /v1/courses/{courseId}/courseWork` to create a new assignment or question.
9. `POST /v1/courses/{courseId}/announcements` to post a class announcement.
10. `GET /v1/courses/{courseId}/courseWorkMaterials` to check shared resources.

## Bundled Resources

### Scripts

- **`scripts/fetch_classroom_data.py`** — Helper script to list courses, coursework, submissions, students, and announcements. Run `python3 scripts/fetch_classroom_data.py --help` for usage.

### References

- **`references/classroom-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
