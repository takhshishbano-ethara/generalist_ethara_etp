### 3. Prompt Correctness (Does it answer the prompt?) — Weight: 0.20

**SCORING GUIDANCE:** Score based on the correctness audit results below. Let the ratio of correct to incorrect/missing elements determine the score.

**MANDATORY CORRECTNESS AUDIT:**
1. Break down the prompt into distinct questions/requirements
2. For each requirement, mark: [CORRECT], [PARTIALLY CORRECT], [INCORRECT], [MISSING]
3. Verify factual claims against known standards
4. Check for outdated information (especially in tech/science domains)

**DEDUCTION TRIGGERS:**
- Any [PARTIALLY CORRECT] element → cap at 4
- Any [INCORRECT] element → cap at 3
- Any [MISSING] element → -1 point
- Unverifiable technical claims stated as fact → -1 point
- Information older than 2 years in fast-moving fields → -1 point
- Wrong conclusion even with correct reasoning → cap at 3
- Correct conclusion with flawed reasoning → cap at 4

| Score | Criteria | Correctness Rate |
|-------|----------|------------------|
| 6 | Every requirement answered correctly and verifiably | 100% [CORRECT] |
| 5 | All major requirements correct; trivial omissions only | 95%+ correct |
| 4 | Mostly correct but misses 1 key element or has minor inaccuracies | 80-94% correct |
| 3 | Relevant attempt but wrong conclusion or significant gaps | 60-79% correct |
| 2 | Fundamentally flawed approach or largely incorrect | 40-59% correct |
| 1 | Does not address the prompt or completely wrong | <40% correct |

**BEFORE SCORING 5 OR 6:** List each prompt requirement and show how it was correctly answered. Any requirement you cannot verify = automatic cap at 4.
