# PRD Writing Instructions (for AI Agents)

Follow these steps to write a PRD that scores 95+ and passes QC.
This file works with any AI coding tool: Claude Code, OpenCode, Cursor, Aider, or terminal.

---

## Input

- **URL:** The target website URL
- **Output directory:** Determined after extraction (Step 1)

---

## Step 1: Extract site data

Run the extraction pipeline:

```bash
python3 main.py <URL>
```

Note the output directory printed at the end (e.g., `output/2026-04-29/site_name/`).

If the output directory already exists with a `raw_data/` folder, skip this step — extraction is already done.

---

## Step 2: Read the scoring spec

Read `prompts/prd_agent_spec.md` completely. This is your rulebook. It contains:
- All 11 scoring sections with exact point allocations
- The specific regex patterns the scorer looks for
- Banned phrases (auto-reject if 5+)
- QC validation criteria
- Scorer optimization tips

You MUST follow every rule in this file. Re-read it before every write attempt.

---

## Step 3: Read extracted data

Read `<output_dir>/prd_prompt_v2.md`. This contains ALL the data extracted from the website:
- Colors (hex codes), fonts, typography scale
- Animations, GSAP timelines, scroll triggers
- Tech stack, CMS detection
- Responsive behavior at 4 breakpoints
- Asset inventory, screenshots

This is the ONLY data you may use. Never fabricate values not present in this file.

---

## Step 4: Write the PRD

Following the spec rules exactly, write a complete PRD using ONLY the extracted data.
Save to `<output_dir>/prd_llm.md`.

Key reminders:
- Metadata block in first 500 characters (Project, Category, Version, URL, Target Resolution)
- Target ~3000 words (range: 800-3500)
- Every color with hex code, every font with family name, every animation with ms + easing
- Use bullet lists (`- **Key:** Value`), never markdown tables
- Include category addendum after Section 5
- Zero banned phrases

---

## Step 5: Score and validate

Run the scoring helper:

```bash
python3 scripts/score_and_validate.py <output_dir>/prd_llm.md <output_dir>
```

This produces `<output_dir>/feedback.md` with:
- Overall score and grade
- Per-section breakdown with FIX instructions
- Banned phrases found
- QC issues
- PASS/FAIL verdict

---

## Step 6: Read feedback

Read `<output_dir>/feedback.md`.

---

## Step 7: Check verdict

If the VERDICT says `PASS: YES`:
- Copy the content of `prd_llm.md` to `prd.md` and `final_prd.md` in the output directory
- Report success with the score

If the VERDICT says `PASS: NO`:
- Continue to Step 8

---

## Step 8: Iterate (max 5 attempts total)

**CRITICAL:** Re-read `prompts/prd_agent_spec.md` before each revision. This survives context compaction — the file on disk is your source of truth.

1. Read `feedback.md`
2. Fix every issue listed under "FIX:" lines
3. Rewrite and save to `<output_dir>/prd_llm.md`
4. Go back to Step 5

If still failing after 5 attempts, report:
- Best score achieved
- Remaining issues from feedback.md
- Which sections couldn't reach threshold

---

## Rules

- ALWAYS re-read the spec file (`prompts/prd_agent_spec.md`) before each write/rewrite
- Use ONLY extracted data from `prd_prompt_v2.md` — never fabricate values
- PRD must be 800-3500 words
- Every color needs a hex code, every font needs a family name
- Every animation needs ms + easing function
- No banned phrases (see spec Section F for full list)
- If data is missing, use documented library defaults with "(library default)" note
- Write like a lead engineer briefing a dev team, not a database export
