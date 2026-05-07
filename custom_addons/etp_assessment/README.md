# ETP Assessment Module

Odoo 19 module for managing candidate assessments with a question bank, dimension-based scoring, and a secure public portal.

## Features

### Question Bank
- **Question Types**: Image Comparison, Text, Coding, Image+Text, Video
- **Categories**: Organize questions into categories for different assessments
- **Dimensions**: Each question has linked dimensions with options (e.g., Instruction Following, Visual Quality)
- **Correct Option Marking**: Mark one option per dimension as correct for automated scoring
- **Auto-populate Options**: Adding a dimension to a question automatically populates its options from the master

### Assessment Management
- **Category-based Selection**: Pick questions from a specific category
- **Question Limit**: Set how many questions to include (0 = all)
- **Shuffled Order**: Each candidate receives questions in a randomized order
- **Duration Timer**: Set a time limit (minutes) for candidates to complete the assessment
- **Auto-complete**: Assessment automatically moves to "Done" when all candidates submit
- **States**: Draft → In Progress → Done / Cancelled

### Candidate Management
- **Manual Assignment**: Add candidates via Many2many tags
- **CSV Import**: Bulk import candidates from CSV (name, email, job_title, department)
- **Auto-create Employees**: If email not found in hr.employee, creates one automatically
- **Template Download**: Download a sample CSV template

### Portal (Public Assessment Interface)
- **Token-based Access**: Each candidate gets a unique URL (no login required)
- **Instructions Screen**: Candidates see rules and assessment details before starting
- **One Question at a Time**: Sequential question display with progress bar
- **Countdown Timer**: Visual timer with warning states (yellow < 5min, red < 1min)
- **Auto-submit on Timeout**: When time expires, remaining questions are auto-submitted
- **Responsive Design**: Works on desktop and mobile

### Anti-Cheat Protections
- **No Text Selection/Copy**: CSS `user-select: none` + event handlers
- **No Right-click**: Context menu disabled
- **No Screenshots**: PrintScreen key detection, Ctrl+Shift+S blocked
- **No Tab Switching**: `visibilitychange` and `blur` events trigger auto-submit
- **No Developer Tools**: F12, Ctrl+Shift+I/J detection + window size monitoring
- **No Screen Capture**: `getDisplayMedia` API intercepted
- **Violation Overlay**: Red screen with reason shown before auto-submit

### Dashboard
- **KPI Cards**: Total assessments, questions, candidates, responses, completion rate
- **Charts**: Question types (doughnut), categories (bar), completion status
- **Active Work Panel**: In-progress assessments with candidate progress
- **Top Candidates**: Leaderboard by score
- **Dimension Analytics**: Accuracy percentage per dimension

### Email Notifications
- **Assessment Invitation**: Styled HTML email with assessment details, rules summary, and start link
- **QWeb Rendering**: Uses `web.base.url` for link generation

## Models

| Model | Description |
|-------|-------------|
| `etp.assessment` | Main assessment record |
| `etp.assessment.category` | Question categories |
| `etp.assessment.dimension` | Evaluation dimensions (master) |
| `etp.assessment.dimension.option` | Options per dimension (master) |
| `etp.assessment.question` | Question bank entries |
| `etp.assessment.question.dimension` | Question-dimension link with options |
| `etp.assessment.question.dimension.option` | Per-question dimension options with `is_correct` |
| `etp.assessment.evaluator` | Candidate assignment (token, state, timer) |
| `etp.assessment.response` | Candidate's response per question |
| `etp.assessment.response.line` | Selected option per dimension |

## Workflow

1. **Admin** creates dimensions + options in Configuration
2. **Admin** creates categories and questions in Question Bank
3. **Admin** adds dimensions to each question, marks correct option per dimension
4. **Admin** creates an Assessment: picks category, question limit, duration, dates
5. **Admin** assigns candidates (manual or CSV import)
6. **Admin** clicks **Start Assessment** → selects questions, shuffles per candidate, sends emails
7. **Candidate** receives email with rules summary + link
8. **Candidate** opens link → sees Instructions/Rules page
9. **Candidate** clicks "Start Assessment" → timer begins
10. **Candidate** answers questions one by one (dimensions + justification)
11. **Candidate** submits all → locked, sees completion page
12. **System** auto-marks assessment "Done" when all candidates complete

## Security

| Group | Access |
|-------|--------|
| Assessment Candidate (Evaluator) | Read masters, create/edit own responses |
| Assessment Manager | Full CRUD on all models |

## Installation

1. Place module in your addons path
2. Update app list: Settings → Apps → Update Apps List
3. Install "ETP Assessment"

## Dependencies

- `base`
- `web`
- `hr`
- `mail`
- `website`

## Configuration

### System Parameters
- `web.base.url`: Must be set to your domain for email links to work correctly

### Duration
- Set `Duration (Minutes)` on the assessment form (0 = no time limit)
- Timer starts when candidate clicks "Start Assessment" on the instructions page

## CSV Import Format

### Candidates CSV
```
name,email,job_title,department
John Doe,john@example.com,Evaluator,Engineering
Jane Smith,jane@example.com,Senior Evaluator,Design
```

### Question Bank CSV
```
name,category_id,question_type,prompt,description,code_snippet,code_language,video_url,image_a_url,image_b_url
```

## Technical Details

- **Version**: 19.0.0.4
- **License**: LGPL-3
- **OWL Dashboard**: Custom component with Chart.js
- **Portal**: Public routes with token auth (`auth="public"`)
- **Scoring**: Binary (1 = correct option selected, 0 = wrong)
