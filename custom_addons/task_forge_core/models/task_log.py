from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError
from datetime import datetime, date
import json
import logging

_logger = logging.getLogger(__name__)

QC_REVIEW_SYSTEM_PROMPT = """# SYSTEM PROMPT: Image Comparison QC Evaluator

You are reviewing an annotator's evaluation of two AI-generated images against a text prompt. Your job: determine if their scoring is correct. Flag errors. Be specific.

---

## INPUTS YOU RECEIVE

- **Text Prompt**: The generation instruction (treat as binding specification)
- **Response A**: AI-generated image
- **Response B**: AI-generated image
- **Annotator's Scores**: Overall Preference (OP), Visual Quality (VQ), Absence of AI Artifacts (AI), Instruction Following (IF)
- **Annotator's Justification** (if provided)

---

## EVALUATION DIMENSIONS (assess in this order)

### 1. INSTRUCTION FOLLOWING (IF) — Highest Priority

The prompt is a specification. Evaluate compliance literally.

**Check each of these:**

- **Count**: "5 balloons" means exactly 5. Not 4. Not 6.
- **Identity**: Named subjects, species, objects must be correct.
- **Spatial relationships**: "above," "left of," "inside" — verify each stated relationship.
- **Style/Medium**: "oil painting" must exhibit actual medium characteristics (visible brushwork, impasto texture, canvas grain) — not a digital render with a filter.
- **Text content**: Any specified text must be spelled correctly, fully present, legible. One wrong letter = failure.
- **Omissions**: Missing a requested element = failure.
- **Additions**: Elements not requested = failure (extra objects, text, decorations the prompt didn't ask for).
- **Actions/State**: "running," "sleeping," "melting" — the depicted action must match.
- **Implicit physics**: "helium balloon released 2 seconds ago" = balloon is ABOVE release point, moving upward. "Ball dropped from height" = ball is below, moving down. Apply real-world physics to temporal/causal descriptions.

**Scoring logic:**

- One image compliant, other non-compliant → non-compliant image LOSES. Never a tie.
- Both non-compliant → the one with fewer/less severe violations wins, unless equally broken.
- Both fully compliant → Tie.

---

### 2. ABSENCE OF AI ARTIFACTS (AI)

Look for outputs that reveal the model doesn't understand physical reality.

**Anatomy:**

- Hands: Count fingers (5 per hand). Check joint angles, no fusion, no extra digits.
- Limbs: Correct count, attach at anatomically plausible points.
- Eyes: Symmetric sizing, consistent iris detail, species-appropriate pupil shape.
- Faces: No smearing, melting, or asymmetric distortion (especially on background figures).
- Teeth: Reasonable count, consistent sizing, no impossible overlaps.

**Physics & Logic:**

- Gravity applies unless a reason is depicted (wings, propulsion, floating medium).
- Shadows share a consistent light source direction.
- Reflections match their source objects.
- Mechanical objects must be structurally plausible (springs, gears, hinges).
- Fluid behavior: water flows downhill, smoke rises, etc.

**Surfaces & Materials:**

- Text on objects must warp consistently with the surface geometry.
- Material transitions must be intentional, not glitchy.
- Metallic reflections must be physically plausible.
- Fabric follows gravity and body contour.

**Data Integrity (charts, diagrams, maps):**

- Axes must be sequential and labeled correctly.
- Pie slices must sum to 100%.
- Numbers/labels must not duplicate or contradict.

**Heuristic:** "Which image would cost less to fix for client delivery?" That image wins.

**Scoring logic:**

- One image has artifacts the other doesn't → cleaner image wins.
- Both have artifacts → fewer/less severe artifacts wins.
- Artifacts equally minor or absent in both → Tie.

---

### 3. VISUAL QUALITY (VQ) — Craft and Execution

- **Composition**: Subject framing, visual hierarchy, breathing room.
- **Lighting**: Consistent source, believable shadows/highlights.
- **Color**: Harmonious palette, appropriate saturation, no banding.
- **Detail**: Appropriate to the style. A clean illustration can outscore a noisy photorealistic attempt.
- **Clarity**: Rendering is clean at intended viewing size.

**You must cite specifics.** "Looks more polished" is not an assessment. *What* is more polished? Spacing? Edge quality? Color grading? Typography weight?

**Scoring logic:**

- Demonstrably better craft in one image → that image wins.
- Comparable quality, different strengths → Tie.
- Both poor → less poor wins, unless indistinguishable.

---

### 4. OVERALL PREFERENCE (OP) — Logical Consequence

OP must follow from the dimension scores. Apply this decision table:

| IF  | AI  | VQ  | OP Must Be              |
| --- | --- | --- | ----------------------- |
| A   | A   | A   | A                       |
| A   | A   | Tie | A                       |
| A   | Tie | A   | A                       |
| A   | Tie | Tie | A                       |
| A   | B   | B   | Judgment call — justify |
| Tie | Tie | Tie | Tie                     |

**Conflict resolution** (when dimensions disagree): IF dominates, then AI, then VQ. A technically beautiful image that doesn't follow the prompt loses to an uglier one that does.

**The only override:** If an image has a single dimension failure so severe it makes the image unusable for its intended purpose (e.g., a portrait with a three-armed subject), that catastrophic failure can override favorable scores in other dimensions. You must name the specific catastrophic defect and explain why it overrides.

---

## ANNOTATOR ERROR PATTERNS

Flag these when detected:

### Tie Spam

Annotator picks "Tie" on 3+ dimensions when clear differences exist. Verify each dimension independently — actual ties across all dimensions are uncommon.

### One-Side Sweep (valid OR invalid)

Annotator picks the same image across all dimensions. **This can be correct** — some images genuinely dominate. Verify independently. Only flag if you find a dimension where the other image is clearly better.

### "Tie Both Good" With Visible Defects

One image has a missing finger, broken text, wrong count, or physics violation — and the annotator called it a tie. This is the most common annotator failure. If a defect exists in one image but not the other, it cannot be a tie on that dimension.

### "Tie Both Bad" When One Is Less Bad

Both images can be flawed. If one has *fewer* or *less severe* flaws, it wins. "Both Bad" requires the flaws to be equivalent in severity and quantity.

### Contradictory Justification

The written comment describes flaws in Image X, but the annotator scored Image X as the winner or called a tie. Their own words condemn their call.

### Physics/Domain Blindness

Annotator evaluates only aesthetics while ignoring factual, physical, or domain-specific errors. Examples: balloon moving the wrong direction, anatomically impossible animal features, nonsensical chart data, incorrect geographic layout.

### AI-Written Justification

Comment is generic, overly balanced, non-committal, uses hedging language ("both responses adequately address..."), or describes strengths of both without taking a position that matches their actual scores. Real evaluator notes are terse, specific, decisive.

---

## DOMAIN KNOWLEDGE PROTOCOL

When a prompt requires specialized knowledge (biology, physics, geography, data visualization, calligraphy, architecture, etc.):

1. **Assess whether you can verify** the domain requirement with confidence.
2. **If yes**: Evaluate and cite your reasoning.
3. **If uncertain**: State your confidence level and the specific claim you cannot verify. Still evaluate what you CAN assess (composition, artifacts, general quality), and flag the domain-specific element as "requires specialist verification."

Do not pretend expertise you don't have. Do not ignore domain requirements because they're hard to verify.

---

## CALIBRATION EXAMPLES

### Example 1: Clear IF Failure

**Prompt**: "A red fox sitting on a blue park bench, autumn leaves falling"

- Image A: Red fox on blue bench, leaves falling. ✓
- Image B: Red fox on GREEN bench, leaves falling.

**Correct scoring**: IF=A (bench color wrong in B). Everything else evaluated independently. If VQ and AI are similar, OP=A.

**Common annotator error**: Calling IF a tie because "both show a fox and leaves." Wrong — the bench color is a stated requirement.

---

### Example 2: Artifact vs. Quality Tradeoff

**Prompt**: "Portrait of an elderly woman smiling"

- Image A: Beautiful lighting, masterful composition. Woman has 6 fingers on left hand.
- Image B: Flatter lighting, simpler composition. Anatomically correct.

**Correct scoring**: IF=Tie (both depict the subject), AI=B (6 fingers is an artifact), VQ=A (better craft), OP=Judgment call. The artifact is immediately noticeable in a portrait context → OP likely B, with justification that a hand deformity in a portrait is disqualifying for production use.

---

### Example 3: Legitimate Sweep

**Prompt**: "Diagram showing the water cycle with labels: evaporation, condensation, precipitation, collection"

- Image A: All labels present and correctly placed, clean diagram, accurate arrows showing cycle direction.
- Image B: Missing "collection" label, arrows inconsistent, one label misspelled as "percipitation," blurry rendering.

**Correct scoring**: IF=A, AI=A, VQ=A, OP=A. This is NOT one-side spam — Image B genuinely fails on every dimension. A sweep is valid here.

---

### Example 4: Legitimate Tie

**Prompt**: "A cozy coffee shop interior, warm lighting"

- Image A: Warm tones, wooden furniture, visible steam from cups, soft window light.
- Image B: Warm tones, brick walls, pendant lights, books on shelves, coffee equipment visible.

**Correct scoring**: IF=Tie (both depict a cozy coffee shop with warm lighting — the prompt is open-ended), AI=Tie (no artifacts in either), VQ=Tie (different aesthetic choices, comparable execution), OP=Tie.

---

## YOUR OUTPUT FORMAT

For each reviewed annotation:

```
TASK: [ID]
ANNOTATOR: [identifier]
PROMPT: [1-line summary of specification]

ANNOTATOR'S CALLS: OP=[X] | VQ=[X] | AI=[X] | IF=[X]

MY ASSESSMENT:
• IF: [Agree/Disagree] — [specific reason if disagree, citing prompt requirement]
• AI: [Agree/Disagree] — [specific defect identified if disagree]
• VQ: [Agree/Disagree] — [specific craft element if disagree]
• OP: [Agree/Disagree] — [logical derivation from above]

FLAGS: [Pattern name(s) from error patterns, or "None"]
CONFIDENCE: [High/Medium/Low] — [state what you couldn't fully verify, if applicable]
SCORE: [Good / Acceptable / Below Expectations / Unacceptable]
RATIONALE: [2-3 sentences max. Terse. Specific. No hedging.]
```

---

## SCORING THE ANNOTATOR

| Score                 | Criteria                                                                                                                   |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **Good**              | All dimensions correct. Justification specific and defensible.                                                             |
| **Acceptable**        | One dimension debatable (reasonable people could disagree). Overall preference correct.                                     |
| **Below Expectations**| 1-2 dimensions incorrect, but annotator showed effort (missed something, didn't spam). Coachable.                          |
| **Unacceptable**      | Multiple dimensions wrong, OR evidence of spam/laziness, OR contradictory logic. Work cannot ship.                         |

---

## OPERATING PRINCIPLES

1. The prompt is the specification. Miss the spec, fail the task. Aesthetics never override compliance.
2. A wrong annotation that enters training data is actively harmful. Err toward flagging.
3. Judge each dimension independently before deriving OP. Do not work backward from a "gut feeling."
4. Be specific or say nothing. Vague assessments ("looks better") have zero value.
5. Acknowledge your limits. Flag domain uncertainty rather than guessing.
6. A legitimate sweep is legitimate. A legitimate tie is legitimate. Judge the evidence, not the pattern.

---

## DESIGN NOTES (for prompt maintainers)

- **No forced-diversity bias.** If one image genuinely dominates all dimensions, score it that way. Do not manufacture disagreements to appear "balanced."
- **Domain humility.** Flag what you can't verify with confidence rather than bluffing.
- **IF > AI > VQ hierarchy is explicit and binding.** When dimensions conflict, this priority order resolves OP.
- **Catastrophic override requires named evidence.** You cannot invoke it vaguely.
- **No persona theater.** Criteria precision drives rigor, not fictional backstories.
"""


class TaskForgeLog(models.Model):
    _name = 'task.forge.log'
    _description = 'Task Forge Task Log'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Task Name', required=True, tracking=True)
    sequence = fields.Char(string='Reference', readonly=True, copy=False, default='New')
    employee_id = fields.Many2one(
        'hr.employee', string='Tasker', required=True,
        default=lambda self: self.env.user.employee_id,
        tracking=True,
    )
    project_id = fields.Many2one('project.project', string='Project', tracking=True)
    is_justification_required = fields.Boolean(related='project_id.is_justification_required')

    date = fields.Date(string='Date', default=fields.Date.context_today, required=True)
    state = fields.Selection([
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('qc_approved', 'QC Approved'),
        ('qc_rejected', 'QC Rejected'),
        ('blocker', 'Blocker'),
        ('returned', 'Returned'),
        ('ack', 'Acknowledged'),
        ('escalated', 'Escalated'),
        ('overdue', 'Overdue'),
    ], string='Status', default='in_progress', tracking=True)

    start_time = fields.Datetime(string='Start Time')
    end_time = fields.Datetime(string='End Time')
    time_taken_mins = fields.Integer(
        string='Time Taken (mins)',
        compute='_compute_time_taken',
        store=True,
    )
    pause_time = fields.Char(string="Pause Time")
    start_screenshot_url = fields.Char(string='Start Screenshot URL')
    end_screenshot_url = fields.Char(string='End Screenshot URL')

    blocker_reason = fields.Text(string='Blocker Reason')
    quality_score = fields.Integer(string='Quality Score')
    prompt_justification = fields.Text(string='Prompt Justification')
    prompt_text = fields.Text(string='Prompt')
    justification_text = fields.Text(string='Justification')
    feedback_note = fields.Text(string='Feedback Note')

    grammar_checked = fields.Boolean(string='Grammar Checked', default=False, index=True)
    grammar_is_perfect = fields.Boolean(string='Grammar Perfect', default=False, index=True)
    prompt_error_percentage = fields.Float(string='Prompt Error %', default=0)
    justification_error_percentage = fields.Float(string='Justification Error %', default=0)
    prompt_issue_count = fields.Integer(string='Prompt Issues', default=0)
    justification_issue_count = fields.Integer(string='Justification Issues', default=0)
    total_grammar_issues = fields.Integer(string='Total Grammar Issues', default=0, index=True)
    prompt_corrected = fields.Text(string='Corrected Prompt')
    justification_corrected = fields.Text(string='Corrected Justification')

    prompt_grammar_count = fields.Integer(string='Prompt Grammar Errors', default=0)
    prompt_misspelling_count = fields.Integer(string='Prompt Misspelling Errors', default=0)
    prompt_punctuation_count = fields.Integer(string='Prompt Punctuation Errors', default=0)
    prompt_clarity_count = fields.Integer(string='Prompt Clarity Errors', default=0)
    prompt_typography_count = fields.Integer(string='Prompt Typography Errors', default=0)
    prompt_capitalization_count = fields.Integer(string='Prompt Capitalization Errors', default=0)
    prompt_miscellaneous_count = fields.Integer(string='Prompt Miscellaneous Errors', default=0)

    justification_grammar_count = fields.Integer(string='Justification Grammar Errors', default=0)
    justification_misspelling_count = fields.Integer(string='Justification Misspelling Errors', default=0)
    justification_punctuation_count = fields.Integer(string='Justification Punctuation Errors', default=0)
    justification_clarity_count = fields.Integer(string='Justification Clarity Errors', default=0)
    justification_typography_count = fields.Integer(string='Justification Typography Errors', default=0)
    justification_capitalization_count = fields.Integer(string='Justification Capitalization Errors', default=0)
    justification_miscellaneous_count = fields.Integer(string='Justification Miscellaneous Errors', default=0)

    grammar_input_tokens = fields.Integer(string='Grammar Input Tokens', default=0)
    grammar_output_tokens = fields.Integer(string='Grammar Output Tokens', default=0)

    blocker_ids = fields.One2many('task.forge.blocker', 'task_id', string='Blockers')
    bug_report_ids = fields.One2many('task.forge.bug.report', 'task_id', string='Bug Reports')
    rubric_rating_ids = fields.One2many(
        'task.forge.rubric.rating', 'log_id', string='Rubric Ratings',
    )
    rubric_completed = fields.Boolean(
        string='Rubric Completed',
        compute='_compute_rubric_completed',
        store=True,
    )

    response_ids = fields.One2many(
        'task.forge.response', 'task_id', string='Responses',
    )
    response_completed = fields.Boolean(
        string='Responses Completed',
        compute='_compute_response_completed',
        store=True,
    )

    employee_name = fields.Char(related='employee_id.name', store=True)
    project_name = fields.Char(related='project_id.name', store=True)
    image_url_lines = fields.One2many('task.forge.image', 'task_id', string="Image Url Lines")
    chain_of_thought = fields.Text(string='Chain of Thought')

    # QC Review
    reviewed_by_id = fields.Many2one('hr.employee', string='Reviewed By', readonly=True)
    review_date = fields.Datetime(string='Review Date', readonly=True)
    rejection_reason = fields.Text(string='Rejection Reason')

    # LLM QC Results
    qc_llm_score = fields.Selection([
        ('good', 'Good'),
        ('acceptable', 'Acceptable'),
        ('below_expectations', 'Below Expectations'),
        ('unacceptable', 'Unacceptable'),
    ], string='LLM QC Score')
    qc_llm_flag = fields.Char(string='LLM QC Flag')
    qc_llm_feedback = fields.Text(string='LLM QC Feedback')
    qc_llm_raw_response = fields.Text(string='LLM QC Raw Response')
    qc_llm_input_tokens = fields.Integer(string='Input Tokens', default=0)
    qc_llm_output_tokens = fields.Integer(string='Output Tokens', default=0)

    # Rating System

    rubric_text = fields.Text(string='Rubric Text')

    def action_parse_rubric_text(self):
        self.ensure_one()
        if not self.rubric_text:
            raise UserError('Please paste rubric text first.')
        if not self.project_id:
            raise UserError('Select a project first.')

        from odoo.addons.task_forge_core.services.rubric_text_parser import (
            parse_rubric_text, match_and_create_rubric
        )

        parsed = parse_rubric_text(self.rubric_text)
        if not parsed:
            raise UserError('Could not parse any rubric structure from the text.')

        result = match_and_create_rubric(self.env, self.project_id, parsed)

        new_dims = [d for d in result['dimensions'] if d['is_new']]
        new_opts = [o for o in result['options'] if o['is_new']]

        msg = f"{len(new_dims)} new dimension(s) and {len(new_opts)} new option(s) created as temp. PL needs to approve them in project settings before they appear on tasks."
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Rubric Parsed',
                'message': msg,
                'type': 'success' if (new_dims or new_opts) else 'info',
                'sticky': False,
            }
        }

    # Rating Syste
    task_score = fields.Integer(string='Task Score')
    comment = fields.Char(string='Comment')

    @api.depends('start_time', 'end_time')
    def _compute_time_taken(self):
        for rec in self:
            if rec.start_time and rec.end_time:
                delta = rec.end_time - rec.start_time
                rec.time_taken_mins = int(delta.total_seconds() / 60)
            else:
                rec.time_taken_mins = 0

    @api.depends(
        'project_id', 'project_id.is_rubrics_required',
        'project_id.rubric_category_ids',
        'project_id.rubric_category_ids.dimension_ids',
        'project_id.rubric_category_ids.dimension_ids.is_required',
        'rubric_rating_ids', 'rubric_rating_ids.dimension_id',
    )
    def _compute_rubric_completed(self):
        for rec in self:
            project = rec.project_id
            if not project or not project.is_rubrics_required:
                rec.rubric_completed = True
                continue
            required_dims = self.env['rubric.dimension']
            for cat in project.rubric_category_ids:
                required_dims |= cat.dimension_ids.filtered(lambda d: d.is_required)
            if not required_dims:
                rec.rubric_completed = True
                continue
            rated_dim_ids = set(rec.rubric_rating_ids.mapped('dimension_id.id'))
            rec.rubric_completed = all(d.id in rated_dim_ids for d in required_dims)

    @api.depends(
        'project_id', 'project_id.is_response_required',
        'project_id.no_of_responses',
        'response_ids', 'response_ids.value',
    )
    def _compute_response_completed(self):
        for rec in self:
            project = rec.project_id
            if not project or not project.is_response_required:
                rec.response_completed = True
                continue
            required_count = project.no_of_responses or 0
            if not required_count:
                rec.response_completed = True
                continue
            filled = rec.response_ids.filtered(lambda r: r.value)
            rec.response_completed = len(filled) >= required_count

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('sequence', 'New') == 'New':
                vals['sequence'] = self.env['ir.sequence'].next_by_code('task.forge.log') or 'New'
        return super().create(vals_list)

    def _check_punch_in(self, employee_id):
        """Verify the employee has an active attendance record for today."""
        today = date.today()
        attendance = self.env['hr.attendance'].sudo().search([
            ('employee_id', '=', employee_id),
            ('check_in', '>=', datetime.combine(today, datetime.min.time())),
            ('check_in', '<', datetime.combine(today, datetime.max.time())),
        ], limit=1)
        if not attendance:
            raise UserError('You must punch in before starting a task.')
        return attendance

    def _check_no_active_task(self, employee_id):
        """Ensure no other task is in_progress for this employee."""
        active = self.sudo().search([
            ('employee_id', '=', employee_id),
            ('state', '=', 'in_progress'),
        ], limit=1)
        if active:
            raise UserError(f'You already have an active task: {active.name}. End it first.')

    def action_start(self):
        """Start a task - called from API."""
        self.ensure_one()
        self._check_punch_in(self.employee_id.id)
        # self._check_no_active_task(self.employee_id.id)
        self.write({
            'state': 'in_progress',
            'start_time': datetime.now(),
        })

    @api.model
    def _cron_inactivity_check(self):
        """Find members inactive 3+ days and notify their PLs."""
        from datetime import timedelta
        today = date.today()
        three_days_ago = today - timedelta(days=3)
        Employee = self.env['hr.employee'].sudo()
        Attendance = self.env['hr.attendance'].sudo()
        Notification = self.env['kubera.notification'].sudo()

        active_employees = Employee.search([('task_forge_active', '=', True)])
        for emp in active_employees:
            role = emp._get_task_forge_role()
            if role != 'tasker':
                continue
            recent = Attendance.search_count([
                ('employee_id', '=', emp.id),
                ('check_in', '>=', datetime.combine(three_days_ago, datetime.min.time())),
            ])
            if recent == 0 and emp.task_forge_pl_id and emp.task_forge_pl_id.user_id:
                Notification.create({
                    'title': 'Inactivity Alert',
                    'message': f'{emp.name} has been inactive for 3+ days.',
                    'user_id': emp.task_forge_pl_id.user_id.id,
                    'priority': '2',
                })

    def action_qc_with_llm(self):
        self.ensure_one()
        if not self.project_id:
            raise UserError('Task must have a project assigned for QC.')
        if not self.project_id.is_rubrics_required:
            raise UserError('This project does not have rubrics configured.')

        categories = self.project_id.rubric_category_ids
        if not categories:
            raise UserError('No rubric categories configured for this project.')

        rubric_spec = []
        for cat in categories:
            dims = []
            for dim in cat.dimension_ids:
                opts = [{'id': o.id, 'name': o.name, 'value': o.value} for o in dim.option_ids]
                dims.append({
                    'dimension_id': dim.id,
                    'name': dim.name,
                    'description': dim.description or '',
                    'is_required': dim.is_required,
                    'options': opts,
                })
            rubric_spec.append({
                'category': cat.name,
                'description': cat.description or '',
                'dimensions': dims,
            })

        annotator_ratings = ''
        if self.rubric_rating_ids:
            for r in self.rubric_rating_ids:
                dim_name = r.dimension_name_snapshot or (r.dimension_id.name or '')
                opt_name = r.option_name_snapshot or (r.option_id.name or '')
                annotator_ratings += f"\n  {dim_name} = {opt_name}"

        responses_text = ''
        images = []
        if self.response_ids:
            for r in self.response_ids.sorted('sequence'):
                if r.response_url:
                    from odoo.addons.task_forge_core.services.kimi_client import fetch_image_as_base64
                    b64, media_type = fetch_image_as_base64(r.response_url)
                    if b64:
                        fmt = media_type.split('/')[-1] if media_type else 'jpeg'
                        images.append({'base64': b64, 'format': fmt})
                        responses_text += f"\n  Response {r.sequence}: (image {len(images)} attached as image block)"
                    else:
                        responses_text += f"\n  Response {r.sequence}: (FAILED to download from {r.response_url})"
                else:
                    responses_text += f"\n  Response {r.sequence}: (no URL provided)"

        _logger.info('QC Review: %d images downloaded for task %d', len(images), self.id)

        user_content = f"Task ID: {self.id}\n"
        user_content += f"Task Name: {self.name or ''}\n"
        user_content += f"Annotator: {self.employee_id.name or ''}\n\n"
        user_content += f"Prompt: {self.prompt_text or '(none)'}\n\n"
        user_content += f"Justification/Comment: {self.justification_text or '(none)'}\n\n"
        user_content += f"Chain of Thought: {self.chain_of_thought or '(none)'}\n\n"
        if responses_text:
            user_content += f"Images (Response A=image 1, Response B=image 2, etc.):{responses_text}\n\n"
        if annotator_ratings:
            user_content += f"Annotator's Ratings:{annotator_ratings}\n\n"
        user_content += f"Rubric Schema:\n{json.dumps(rubric_spec, indent=2)}"

        system_prompt = QC_REVIEW_SYSTEM_PROMPT

        from odoo.addons.task_forge_core.services.kimi_client import call_kimi, parse_json_response

        result = call_kimi(system_prompt, user_content, temperature=0.1, images=images if images else None)
        raw_text = result.get('text', '')
        _logger.info('QC LLM response (%d chars): %s', len(raw_text), raw_text[:500])

        usage = result.get('usage', {})
        input_tokens = usage.get('input_tokens', 0)
        output_tokens = usage.get('output_tokens', 0)

        parsed = parse_json_response(raw_text)
        if not parsed:
            self.write({
                'qc_llm_raw_response': raw_text,
                'qc_llm_feedback': 'LLM returned invalid JSON. See raw response.',
                'qc_llm_input_tokens': input_tokens,
                'qc_llm_output_tokens': output_tokens,
            })
            raise UserError('LLM returned an invalid response. Raw response saved for inspection.')

        overall_feedback = parsed.get('overall_feedback', '')
        flag_category = parsed.get('flag_category', '')
        score = parsed.get('score', '')

        score_map = {
            'Good': 'good',
            'Acceptable': 'acceptable',
            'Below Expectations': 'below_expectations',
            'Unacceptable': 'unacceptable',
        }

        self.write({
            'qc_llm_score': score_map.get(score, False),
            'qc_llm_flag': flag_category or '',
            'qc_llm_feedback': overall_feedback or '',
            'qc_llm_raw_response': raw_text,
            'feedback_note': overall_feedback or '',
            'qc_llm_input_tokens': input_tokens,
            'qc_llm_output_tokens': output_tokens,
        })

        notify_msg = 'QC Review complete.'
        if score:
            notify_msg += f' Score: {score}.'
        if flag_category and flag_category != 'Clean':
            notify_msg += f' Flag: {flag_category}.'

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'QC Review Complete',
                'message': notify_msg,
                'type': 'success' if score in ('Good', 'Acceptable') else 'warning',
                'sticky': False,
            }
        }

    def action_end(self, end_screenshot_url=None, blocker_reason=None):
        """End a task with either completion or blocker."""
        self.ensure_one()
        vals = {
            'end_time': datetime.now(),
        }
        if end_screenshot_url:
            vals['end_screenshot_url'] = end_screenshot_url
            vals['image_url_lines'] = [(0, 0, {'image_url': end_screenshot_url, 'image_type': 'end'})]

        if blocker_reason:
            vals['state'] = 'blocker'
            vals['blocker_reason'] = blocker_reason
            self.write(vals)
            # Create blocker record
            blocker = self.env['task.forge.blocker'].sudo().create({
                'name': blocker_reason[:100],
                'task_id': self.id,
                'employee_id': self.employee_id.id,
                'qr_id': self.employee_id.task_forge_qr_id.id if self.employee_id.task_forge_qr_id else False,
                'pl_id': self.employee_id.task_forge_pl_id.id if self.employee_id.task_forge_pl_id else False,
                'blocker_reason': blocker_reason,
                'state': 'pending',
            })
            # Notify QR
            if self.employee_id.task_forge_qr_id and self.employee_id.task_forge_qr_id.user_id:
                self.env['kubera.notification'].sudo().create({
                    'title': 'New Blocker Raised',
                    'message': f'{self.employee_id.name} reported a blocker on task "{self.name}": {blocker_reason[:200]}',
                    'user_id': self.employee_id.task_forge_qr_id.user_id.id,
                    'priority': '2',
                    'res_model': 'task.forge.blocker',
                    'res_id': blocker.id,
                    'project_id': self.project_id.id if self.project_id else False,
                })
            return blocker
        else:
            vals['state'] = 'completed'
            self.write(vals)
            return self

class TaskForgeImages(models.Model):
    _name = 'task.forge.image'

    image_url = fields.Char(string='Image URL')
    image_type = fields.Selection([('start', 'Start'), ('end', 'End')])
    task_id = fields.Many2one('task.forge.log', string='Task')
    status = fields.Selection([('draft', 'Draft'), ('rejected', 'Rejected'), ('approved', 'Approved')], default='draft')


