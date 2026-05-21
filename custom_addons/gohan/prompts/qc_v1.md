# Gohan PRD QC

This is a quality-control prompt for Gohan-style PRDs. The QC checks whether a given PRD strictly adheres to the canonical PRD structural rules, word budgets, character hygiene, banned-token list, and hard rules - all of which are encoded directly in this document.

The QC is fully self-contained. Every rule the QC enforces is stated below; no external file needs to be present to run it. The conceptual source of these rules is referred to throughout as "the Generator" - that is the PRD-generation prompt these QC rules mirror. The QC does not require the Generator prompt as input; the rules are reproduced here in full.

Do not invent rules. Do not soften or strengthen the rules below. If a rule is not stated in this document, the QC does not enforce it.

## How to use this QC

Pass the PRD body (the full markdown beginning at `## 1. Product Overview`) as `<prd_to_qc>`. Then run the prompt below.

The QC outputs a `VERDICT` (`PASS` or `FAIL`), a `SUMMARY` block, and a per-check table. A single `FAIL` on any A-, B-, C-, H-, or M-class check (per the verdict policy) is sufficient to fail the PRD. The QC never edits the PRD - it only reports.

## ---BEGIN PROMPT---

You are a strict, deterministic QC reviewer. Your job is to verify that the PRD provided in `<prd_to_qc>` strictly follows the canonical Gohan PRD rules encoded in this prompt.

You do not write code. You do not rewrite the PRD. You do not soften the Generator's rules. You do not invent new rules.

For every check, you produce a single row in the report with one of: `PASS`, `FAIL`, `N/A`. Use `N/A` only when the check explicitly admits it (e.g., a section that the Generator permits to be omitted, like 4.4 Real-time when not applicable).

If you are uncertain whether a finding is a real violation or a false positive, mark the row `FAIL` and include the exact quoted span from the PRD plus your reasoning. Never silently downgrade an uncertain finding.

### Inputs

- `<prd_to_qc>`: the full PRD markdown, starting at `## 1. Product Overview` and ending on the final line of Section 8. No wrapping fences. No metadata above the first heading.

This is the only input the QC requires. All rules and reference tables (including the Category-Specific Feature Emphasis catalog used by G6) are embedded in this prompt.

### Output format

```
VERDICT: PASS | FAIL

SUMMARY:
- Word count (whitespace-split): <integer>
- H2 sections found, in order: <list>
- Banned tokens found: <integer count of UNIQUE banned-token violations across classes A and H, deduplicated per the tie-break order so a single em-dash is one violation, not three>
- Critical violations (any A/B/C/H FAIL, plus M1/M2/M3 FAILs, plus the specific E2/F3/F4/G4/G5/I2/I4/J2/J5/J6/J7/K2/L4/L6/L7 hard-rule FAILs): <count>
- Soft warnings (any other FAIL): <count>

CHECK TABLE:
| ID | Check | Status | Evidence |
|----|-------|--------|----------|
| A1 | ... | PASS / FAIL / N/A | quoted span or "no match" or N/A reason |
| A2 | ... | ... | ... |
... one row per check, in canonical ID order A1, A2, ..., A8, B1, ..., M4 ...

NOTES:
- <any false-positive carve-outs you applied and why>
- <any check you could not run and why>
- <any check marked N/A and the specific reason>
```

The CHECK TABLE rows MUST appear in canonical ID order (A1, A2, A3, A4, A5, A6, A7, A8, B1...B7, C1, C2, D1...D5, E1...E5, F1...F5, G1...G8, H1...H11, I1...I4, J1...J10, K1...K6, L1...L7, M1...M4). Do not reorder by status or by section.

Every `N/A` row MUST populate the evidence column with a reason (e.g. `"Section 4.4 omitted - within Generator carve-out"`).

### Verdict policy

`VERDICT: FAIL` if ANY of these are true:
- Any A-class (character hygiene) check is `FAIL`.
- Any B-class (section structure) check is `FAIL`.
- Any C-class (reduced-motion contract presence) check is `FAIL`.
- Any H-class (forbidden tokens / hard-rule banned content) check is `FAIL`.
- Any M-class (cross-section consistency) check is `FAIL`: M1 (invented-name consistency), M2 (source-brand leak), or M3 (source-URL leak).
- Any of these specific check FAILs (each enforces a canonical hard rule and therefore auto-FAILs the PRD): E2 (Color System `State restrictions` after the list), F3 (no real backend stack), F4 (perf inside Web Vitals Good band), G4 (page-list 4-field structure plus 2-space indentation), G5 (routes generic only), I2 (every motion line carries ms + cubic-bezier), I4 (Section 5 declares all seven required motion surfaces), J2 (Section 6 sub-section presence and order), J5 (Sign-up `->` arrow-step form), J6 (View Models 6-8 entities / 5-7 fields each), J7 (Main UI Flows 3-flow Create/lifecycle/role-gated triad), K2 (numeric WCAG contrast ratios), L4 (Section 8 structured-data type AND robots/sitemap/canonical/noindex policy, both required), L6 (no copyright/Terms/Privacy in Section 8), L7 (invented project name in meta titles plus og:site_name plus Organization).
- The global word count is outside the 800-1500 band (B5).
- The PRD has fewer than 8 H2 sections in the required order, or contains a 9th (B1).

Otherwise `VERDICT: PASS`. Soft warnings NEVER auto-FAIL the PRD - they are surfaced in the `Soft warnings` count for operator triage.

### How to count words

Use whitespace-split count on the entire PRD body. This matches `wc -w`. Headings, bullets, backticked code, and inline punctuation all contribute. Section-level word counts use the same rule, scoped to text under that section heading until the next H2 (for sub-section budgets, until the next H3 or H4).

### How to scan for banned tokens

For each banned-token regex, search the entire PRD body unless the check scopes it to a specific section. Quote the offending span (a few words before and after) as evidence. Apply the documented carve-outs (e.g., `Escape` mentioned ONCE per modal as accessibility dismiss key is allowed; verbatim quoted UI strings may keep their literal glyph if labeled).

Before applying any regex that targets prose syntax (e.g. A2 arrow separators, A4 double-dash), STRIP backticked spans from the line. Tokens inside backticks (`` `like-this` ``) are treated as literal code and are exempt from prose-syntax checks. Tokens inside backticks remain subject to character-set checks (A3, A6).

When a token might match multiple checks (for example `Cmd+K` matches both keyboard-shortcut and ASCII rules), report it once under the most specific check using this tie-break order, highest priority first:
1. H-class forbidden tokens (H1-H11) win over A-class.
2. Within H-class: H4 (keyboard shortcuts) > H8 (Framer Motion) > H6 (legal strings) > H1 (brand) > H5 (hype adjectives).
3. A-class wins over B/D-O classes for character-level violations.
4. Section-scoped checks (e.g. J5, J7) win over global checks when the violation is inside their scope.

If two checks have equal specificity, report under the lower-letter class (A before B before C ...).

---

## A. Character hygiene

The PRD must use keyboard-typeable ASCII only, with a tight allowed punctuation set. The canonical forbidden-token list is encoded in this section; the rules below are the authoritative reference for the QC.

### A1. No arrow glyph

Banned: `→` (U+2192) anywhere in the PRD.
Regex: `\u2192`
Generator anchor: "OUTPUT CHARACTER RULES" no-arrow rule + Unicode ban.
Fix guidance (surface in NOTES if found): replace with semantic prose - a period for flow steps, the word `via` or a comma for breadcrumbs, the word `to` for scale endpoints (e.g. `card scales from 0.95 to 1.0`). Do NOT recommend `->` as the replacement; `->` is banned per A2.

A1 defers to A3 -- if A3's `[^\x00-\x7F]` pass already caught the U+2192 glyph, report once under A3 per the tie-break order. A1 is a focused re-flag for the operator when A3 missed it.

### A2. No `->` arrow-step separator in prose

Banned in PRD prose: the `->` (ASCII arrow) sequence when used as a separator between flow steps, breadcrumb segments, or numeric endpoints. The Generator explicitly bans only `->` (and the U+2192 glyph, which A1 catches). The QC does NOT extend the ban to bare ` > ` or `=>` -- those forms are NOT in the Generator's pre-emit scan and may appear legitimately (e.g. `5 > 4 in priority`, blockquote markers, comparison operators inside backticked code). J7's step-separator density rule still catches abusive use of any glyph as a flow separator inside Main UI Flows.

Regex (heuristic): `[A-Za-z0-9'")\]]\s*->\s*[A-Za-z0-9'"(\[]`

Pre-scan step: strip backticked inline code spans before applying these regexes (per the global scan instruction).

Carve-outs:
- Markdown blockquote lines that start with `>` at the beginning of a line (rare in PRD prose) are not arrow-step separators.
- Inside backticked code, `->` may appear as literal source code (e.g. a Rust closure, a TypeScript return type). Backticks exempt the span.
- The word `to` is the recommended replacement for scale endpoints, e.g. `card scales from 0.95 to 1.0`. The character `>` alone is never the right replacement.
- **Sign-up / Sign-in UI Flow carve-out:** `->` is EXEMPT inside the Section 6 Sign-up / Sign-in UI Flow sub-section. The canonical rules REQUIRE `->` arrow-step form in that sub-section. Scope: between the `Sign-up / Sign-in UI Flow` heading at depth 3 (`### `) OR depth 4 (`#### `) and the next heading at the same depth or shallower.
- **Main UI Flows carve-out:** `->` is EXEMPT inside the Section 6 Main UI Flows sub-section. The canonical rules REQUIRE `->` arrow-step form for each of the 3 flows in that sub-section. Scope: between the `Main UI Flows` heading at depth 3 (`### `) OR depth 4 (`#### `) and the next heading at the same depth or shallower.
- `->` outside the two carve-outs above remains `FAIL`.

Generator anchor: the "OUTPUT CHARACTER RULES" no-`->` rule with Sign-up and Main UI Flows carve-outs + the canonical hard rule pair "Sign-up UI Flow uses one arrow-step paragraph" and "Main UI Flows uses arrow-step paragraphs".

### A3. Keyboard-typeable ASCII only

Allowed: `A-Z a-z 0-9`, space, newline, and `. , ; : ! ? ' " - _ ( ) [ ] { } / \ | @ # $ % ^ & * + = < > ~` and backtick.
Banned outside the allowed set: any other Unicode codepoint, including em-dash `\u2014`, en-dash `\u2013`, smart quotes (`\u2018` `\u2019` `\u201C` `\u201D`), ellipsis `\u2026`, bullet glyph `\u2022`, multiplication sign `\u00D7`, degree sign `\u00B0`, currency `\u20AC` `\u00A3` `\u00A5`, registered `\u00AE`, trademark `\u2122`, all emoji, zero-width characters, non-breaking spaces.
Regex: `[^\x00-\x7F]` flags every non-ASCII char. Whittle through the A3a carve-out.
Carve-out (A3a): the Generator allows literal non-ASCII glyphs only when the PRD is quoting a verbatim scraped UI string and the surrounding sentence calls it out as verbatim site copy. If a non-ASCII match is inside a single-quoted UI label AND the sentence labels it as verbatim, mark `PASS` and note the carve-out. If in doubt, `FAIL`.
Generator anchor: "OUTPUT CHARACTER RULES" allowed character set + verbatim glyph carve-out.

### A4. No double-dash ` -- ` as clause separator

Banned in PRD prose: ` -- ` (space dash dash space) used as a sentence-level clause break.
Regex: ` -- ` (with surrounding spaces).
Carve-outs:
- Bullet markers are single `-`, not `--`.
- A bare `--` inside a backticked code span (e.g. a CLI flag `--verbose`) is allowed.
- The PRD does not naturally produce CLI flag prose - if you see ` -- ` outside of backticks, it is almost certainly a clause separator. `FAIL`.
Generator anchor: "OUTPUT CHARACTER RULES" keyboard-ASCII rule + Generator's own self-noted use of ` -- ` for model instructions (which the PRD output must not mirror).
Fix guidance: replace with period, comma, semicolon, or colon.

### A5. Hex codes always backticked, uppercase, 6-digit

Every color hex in the PRD must be backticked, start with `#`, and contain exactly six uppercase hex digits.
Passing form (regex literal): a backtick, then `#`, then six uppercase hex digits `[0-9A-F]{6}`, then a closing backtick.
Failing forms (any of):
- `#[0-9A-Fa-f]{3}\b` (3-digit form), unless inside a fenced code block
- `#[0-9a-f]{6}\b` (lowercase six-digit, even inside backticks)
- Mixed-case six-digit hex (at least one lowercase letter `[a-f]`) anywhere in the PRD, even inside backticks: `#[0-9A-Fa-f]{6}` containing at least one `[a-f]` = `FAIL` because the Generator mandates uppercase
- Bare `#[0-9A-F]{6}` not wrapped in backticks: detect via negative-lookaround `(?<!\x60)#[0-9A-Fa-f]{6}(?!\x60)`

Generator anchor: Color System bullet form in Section 2 + "OUTPUT CHARACTER RULES" hex rule.

### A6. No emoji, zero-width, or non-breaking whitespace

Any codepoint in the emoji ranges (U+1F300-U+1FAFF and related) or in `\u200B-\u200D` (zero-width) or `\u00A0` (NBSP) or `\u2060` (word joiner) is a violation.
Regex (heuristic, run AFTER A3): `[\u200B-\u200D\u00A0\u2060\u{1F300}-\u{1FAFF}]` for invisible/decorative. A6 defers to A3 -- if A3 caught the token, A6 reports `PASS` with note `covered under A3` per the tie-break order. A6 fires INDEPENDENTLY only when A3's `[^\x00-\x7F]` pass missed the token (regex coverage gap).
Generator anchor: "OUTPUT CHARACTER RULES" emoji + zero-width ban.

### A7. No markdown tables

The PRD must not contain markdown tables. A markdown table is signaled by a line matching `^\s*\|.*\|\s*$` followed by an alignment row `^\s*\|[\s:-]+\|\s*$`.
Regex (heuristic): two consecutive lines, first matches `^\s*\|.*\|\s*$`, second matches `^\s*\|[ \t\-:|]+\|\s*$`.
Generator anchor: "OUTPUT CHARACTER RULES" no markdown tables.

### A8. No em-dash, en-dash, smart quotes, ellipsis glyph (focused re-flag of A3)

Even though A3 catches these via the non-ASCII regex, the Generator's pre-emit forbidden-token scan calls these out by name. Flag any of the following individually for operator clarity:
- Em-dash `\u2014`
- En-dash `\u2013`
- Left/right single smart quotes `\u2018` `\u2019`
- Left/right double smart quotes `\u201C` `\u201D`
- Ellipsis `\u2026`
- Bullet glyph `\u2022`

If A3 fires on em-dash, en-dash, smart quote, ellipsis, or bullet glyph, suppress A8 -- report once under A3 per the tie-break order in the scanning rules. A8 fires INDEPENDENTLY only when A3's `[^\x00-\x7F]` pass missed the token (regex coverage gap).

Generator anchor: "OUTPUT CHARACTER RULES" explicit banned-glyph list.

---

## B. Section structure (Generator: section headers throughout, plus HARD RULES list)

### B1. Exactly 8 H2 sections, in this order

```
## 1. Product Overview
## 2. Visual & Brand Direction
## 3. Technical Ambition
## 4. Site Architecture & Page Specifications
## 5. Motion Language
## 6. Application Logic
## 7. Accessibility & Quality
## 8. Content & SEO
```

Checks:
- Count of lines beginning with `## ` is exactly 8.
- Each heading matches its expected text byte-for-byte (case-sensitive, with the literal `&` ampersand where shown).
- A 9th H2 = `FAIL`. A missing H2 = `FAIL`. A reordered H2 = `FAIL`.

Sub-section headings within a section may be H3 (`### `) or H4 (`#### `); the Generator's example output uses both. Do not FAIL on the H3-vs-H4 dimension. Treat any heading at depth 3 or 4 as a sub-section. Why both? The Generator's spec text says `### for sub-headings` but its example PRD output (Section 4.1, 4.2, 4.3, 4.4, and every Section 6 sub-section) uses `#### `. This is an internal Generator inconsistency; the QC admits both depths to remain robust to either form.

Generator anchor: per-section headers at the start of each section's spec.

### B2. First line of PRD is `## 1. Product Overview`

No title above, no metadata, no version line, no preamble paragraph, no front matter.
Check: first non-empty line of the PRD is exactly `## 1. Product Overview` (with no trailing characters).
Generator anchor: hard rule "Output is one markdown file" + Section 1 spec.

### B3. Section 6 heading is exactly `## 6. Application Logic`

Not `## 6. Backend & Application Logic`. Not `## 6. Backend Logic`. Not `## 6. Frontend Application Logic`. Not `## 6. App Logic`.
Generator anchor: Section 6 header + hard rule "No 'Backend' word in any heading".

### B4. No banned words in any heading, sub-heading, or label: `Backend`, `API Design`, `Server Logic`

Scope: every line matching `^#{1,6}\s+` (any heading depth, H1 through H6).
Regex (case-insensitive, applied only to heading lines) - any of these on a heading line is a `FAIL`:
- `\bbackend\b`
- `\bapi\s+design\b`
- `\bserver\s+logic\b`

The banned words may appear in BODY PROSE - the canonical ban applies only to headings, sub-headings, and labels (the bolded prefix of bullets like `**Backend Stack:**`, `**API Design Notes:**`, `**Server Logic:**`). Also flag any bolded label prefix of a bullet whose label text contains a banned word:
Regex (case-insensitive): `^\s*-\s+\*\*[^*]*\b(backend|api\s+design|server\s+logic)\b[^*]*\*\*`.

Evidence format on FAIL: `"Banned heading word found: <word> on line <N>: <heading text>"`.
Generator anchor: canonical rule "No 'Backend' word in any heading / sub-heading / label" + Section 6 spec rule "No sub-section is titled 'Backend Flow', 'API Design', 'Server Logic', etc.".

### B5. Global word count is in `[800, 1500]`

Both bounds strict. Use whitespace-split. `wc -w` on the file is the reference.
Output the integer count in `SUMMARY`. `FAIL` if `< 800` or `> 1500`.
Generator anchor: Word Budget block + hard rule "Global hard band 800-1500".

### B6. Per-section soft budgets (warning-class, not auto-FAIL)

| Section | Soft ceiling (words) |
|---------|----------------------|
| 1 | 70 |
| 2 | 210 |
| 3 | 160 |
| 4 | 320 |
| 5 | 80 |
| 6 | 470 |
| 7 | 60 |
| 8 | 60 |

Surface any over-budget section as a `Soft warning`. Per-section overflow alone is a warning - the Generator calls these "soft guidance". B5 catches a true global overflow.
Generator anchor: Word Budget block.

### B7. Document ends at the last line of Section 8

Nothing follows Section 8: no appendix, no changelog, no signature, no "End of PRD" marker, no horizontal rule afterward.
Check: the file's last non-empty content is in scope under `## 8. Content & SEO`.
Generator anchor: hard rule "Output is one markdown file. Document ends on final line of Section 8."

---

## C. Reduced-motion contract presence (Section 5 declares it, Section 7 cross-references it)

The canonical rules REQUIRE the PRD to declare a `prefers-reduced-motion` contract in Section 5 (Motion Language) and to cross-reference that contract from Section 7 (Accessibility & Quality). The C-class enforces the presence of both. (A PRD that mentions reduced motion only inside Section 7 without declaring the contract in Section 5 still fails C1; a PRD that declares the contract in Section 5 but omits the Section 7 cross-reference still fails C2.)

### C1. Section 5 declares a `prefers-reduced-motion` contract

Scope: text under `## 5. Motion Language` until the next H2.
Required: Section 5 MUST contain at least one statement of how the PRD honors `prefers-reduced-motion` (for example, "When `prefers-reduced-motion: reduce` is set, transitions collapse to 0ms and transforms are suppressed; opacity-only fades are retained for state legibility.").
Detection: scoped regex (case-insensitive) `prefers[\s-]?reduced[\s-]?motion` must match at least once inside Section 5 AND the surrounding sentence must describe the BEHAVIOR (durations, transforms, opacity, easing, or animation suppression) under reduced motion, not merely name the token.
`FAIL` if: no `prefers-reduced-motion` token in Section 5, OR the token appears without an accompanying behavior clause.
Generator anchor: Section 5 required surfaces include `prefers-reduced-motion` contract.

### C2. Section 7 cross-references Section 5 on reduced motion

Scope: text under `## 7. Accessibility & Quality` until the next H2.
Required: Section 7 MUST acknowledge reduced motion and defer the contract to Section 5. Acceptable forms include: "Reduced-motion behavior is defined in Section 5", "Honor the reduced-motion contract from Section 5", "See Section 5 for the `prefers-reduced-motion` contract".
Detection: scoped to Section 7, at least one sentence MUST mention reduced motion AND reference Section 5 (string `Section 5`, `section 5`, `Motion Language`, or `the motion section`).
`FAIL` if: Section 7 omits the cross-reference entirely, OR mentions reduced motion without pointing back to Section 5, OR re-declares a competing contract inside Section 7.
Generator anchor: Section 7 required surface "reduced-motion cross-reference to Section 5".

---

## D. Section 1 - Product Overview (Generator: Section 1 spec)

### D1. Up to 70 words

Word count of all text under `## 1. Product Overview` until `## 2. ...`.
Generator anchor: Section 1 word budget.

### D2. Uses the invented project name in body prose

The Generator mandates an invented short project name (1-2 syllables, pronounceable). It must be the name used in Section 1 body. Cross-check that the same name appears in Section 6 mock data sample values and Section 8 meta + structured data.

Best anchor for the invented name is the Section 8 `og:site_name` value (or the `Organization` structured-data name) - these are the most stable references. Look there first when locating the name.

Check (heuristic):
1. Identify the invented project name in Section 1 (subject of opening paragraph; sometimes quoted or backticked).
2. Confirm it appears at least once in Section 6 fixtures (typically as a sample value in an Organization or Project entity's sample row).
3. Confirm it appears at least once in Section 8 meta references (typically `og:site_name` or the Organization structured-data name).
4. If you cannot locate the name unambiguously across all three sections, surface in NOTES and mark `Soft warning`.

Generator anchor: Phase 0 Project Name + hard rule "All sections sync invented project name".

### D3. Target users bullets present (2-3 short bullets)

The Generator: "`**Target users:**` 2-3 short bullets".
Check: a line `**Target users:**` followed by 2-3 list items beginning with `- `.
Generator anchor: Section 1 spec target-users line.

### D4. No `Target Resolution:` line in Section 1

The Generator: "Section 1 does NOT emit a `Target Resolution:` line - implicit."
Regex: case-insensitive match for `target\s*resolution` in Section 1 = `FAIL`.
Generator anchor: Phase 0 Target Resolution + Section 1 spec.

### D5. No success-metric / measured-target line in Section 1 (FAIL)

The Generator explicitly forbids a metric / measured-target line in Section 1. If the PRD includes a `Success metric:`, `KPI:`, `Goal:`, `Target:`, `North star:`, or similar single-line metric statement in Section 1, `FAIL`.
Regex hints (case-insensitive, scoped to Section 1):
- `^\s*(success metric|kpi|north star|target metric|measured target|goal)\s*:`
- A line containing both a percentage and a verb like `reach`, `achieve`, `hit` is suspicious.
Generator anchor: Section 1 spec "no success metric, no measured-target line".

---

## E. Section 2 - Visual & Brand Direction (Generator: Section 2 spec)

### E1. Up to 210 words

Generator anchor: Section 2 word budget.

### E2. Color System: 6-12 tokens, each bullet form

Form: `- **TokenName** \`#HEXCODE\` Role-phrase`
Checks:
- Between 6 and 12 color bullets, inclusive.
- Each bullet has the boldfaced token name first.
- Each bullet has a backticked, uppercase, 6-digit hex.
- Each bullet has a role-phrase after the hex.

A `State restrictions` / usage statement MUST follow the color-token list (e.g. accessibility constraints such as `4.5:1 minimum body contrast`, brand-usage notes, do-not-use guidance). Missing the restrictions statement = `FAIL`. The canonical rule wording is `State restrictions after the list`, which is an imperative, not a soft guide.

Generator anchor: Section 2 Color System sub-section + canonical rule "State restrictions after the list".

### E3. Typography: Google Fonts / Fontsource only, 2-3 typeface bullets

Required: a Typography sub-section with 2-3 typeface bullets in the form `- **Family Name** - usage, weights N, N, N.`

Banned typeface names: any typeface NOT in Google Fonts or Fontsource. The Generator explicitly names these (`Haffer`, `Sohne` / `Söhne`, `GT America`, `Circular`, `Founders Grotesk`, `Aktiv Grotesk`, `FF Real`, `Brown`), and reviewer-known paid fonts also fail (`Apercu`, `Maison Neue`, `Graphik`, `Untitled Sans`, `Suisse Int'l`, `Suisse Intl`, `ABC Diatype`, `ABC Favorit`, `ABC Whyte`, `Neue Haas Grotesk`, `Pangram Sans`, `Editorial New`, `PP Editorial`, and any other non-Google-Fonts / non-Fontsource face).

Allowed (non-exhaustive): Inter, Manrope, IBM Plex Sans, Plus Jakarta Sans, Geist, Outfit, DM Sans, Public Sans, Work Sans, Source Sans 3, Figtree, Lexend, Onest, Albert Sans, Hanken Grotesk, Space Grotesk, Be Vietnam Pro, Sora, Schibsted Grotesk; serif: Source Serif 4, Fraunces, Lora, EB Garamond, Newsreader, Crimson Pro; display: Bricolage Grotesque, Unbounded, Big Shoulders, Boldonse, Limelight.

Carve-out: a banned name is allowed only inside a single-quoted, labeled-verbatim UI string (per A3a). Otherwise `FAIL`.

Also banned in Typography sub-section: the parenthetical `(free alternative for X)` (silent substitution for fonts only, no annotation).

If fewer than 2 or more than 3 typeface bullets, `Soft warning`.

Generator anchor: Section 2 Typography sub-section + hard rule "Typography names only Google Fonts (or Fontsource). Silently substitute paid->Google. No `(free alternative for X)` annotation for fonts."

### E4. Type scale block present at Desktop 1920x1080, on its own paragraph

The canonical scale: H1 64/72 w700, H2 40/48 w600, H3 24/32 w600, body 16/24 w400, small 13/20 w400. The PRD should emit a type-scale line anchored to Desktop 1920x1080.

Checks (rolled into one E4 row):
- E4a. Presence of five size/leading/weight specs covering H1, H2, H3, body, and small.
- E4b. The type-scale line is a STANDALONE paragraph - on its own line, preceded by a blank line, NOT inlined into a typeface bullet (`- **Family Name** ... body 16/24 ...` is FAIL). The canonical form is a separate paragraph after the typeface bullets, beginning `Desktop 1920x1080 scale:` (or equivalent leading phrase) and ending with a period.

If either sub-check fails, E4 = `FAIL` with evidence naming the failing sub-check.

Generator anchor: Section 2 Typography sub-section type-scale spec + hard rule "Type-scale line on its own new line as standalone paragraph".

### E5. Layout block present

Must specify: grid (column count, max-width in px, gutters per breakpoint), page shell (header height, sidebar width), responsive notes.
Generator anchor: Section 2 Layout sub-section.

---

## F. Section 3 - Technical Ambition (Generator: Section 3 spec + Free OSS substitution table)

### F1. Up to 160 words

Generator anchor: Section 3 word budget.

### F2. Core Stack covers required topics

Required topics (each must have at least one bullet's worth of coverage, except as marked):
- framework
- styling
- state
- animation
- rich text (REQUIRED only if a rich-text editor is named anywhere in the PRD, typically in Section 4.3 Interactive Elements; otherwise the Core Stack may omit this bullet)
- forms/validation
- routing
- build/deploy host
- font delivery
- image delivery
- fixture pattern (e.g. inline JSON imports, generated seeds, MSW dev mocks)
- mock auth pattern (e.g. localStorage token-string plus mock user switcher)

`FAIL` if any required topic is missing (rich text is conditional - check Section 4.3 first).
Generator anchor: Section 3 Core Stack spec "rich text (if relevant)".

### F3. No real backend stack

Banned in Section 3:
- Real ORMs: Prisma, Drizzle, TypeORM, Sequelize.
- Real databases: Postgres, MySQL, SQLite-as-prod, MongoDB, DynamoDB, Supabase-as-DB.
- Real auth providers: Better Auth, Auth.js, NextAuth, Lucia, Supabase Auth, Clerk, Auth0.
- Real payment processors: Stripe Elements/SDK, Lemon Squeezy, Paddle (Stripe-as-name in Section 6 UI affordance carve-out is OK there but not in Section 3 stack).
- Real email senders: Resend, Postmark, SendGrid.
- Real queues: BullMQ, Sidekiq, Resque, RabbitMQ, SQS.
- Real rate limiters or other backend infra.

Carve-out: real third-party integration partners are allowed in Section 6 ONLY as UI affordances ("connect Gmail", "connect Slack"). Section 3 stack remains frontend-only.

Generator anchor: hard rule "Frontend-only PRD. No real backend specs."

### F4. Performance targets sit in Web Vitals Good band

Required to all be present, each as its own check row:

- F4a. Lighthouse floor stated (e.g. `Lighthouse 95+ on Performance, Accessibility, SEO`).
- F4b. LCP target `<=2.5s` (or smaller).
- F4c. CLS target `<=0.1` (or smaller).
- F4d. INP target `<=200ms` (Web Vitals ceiling); aim line `100ms p75` is the recommended explicit aim but not required.
- F4e. Bundle ceiling stated in kB gzipped (e.g. `JS bundle <= 180 kB gzipped per route`) AND frame-rate target (e.g. `60fps motion, no jank`).

Banned in Section 3: backend SLAs (API p95, DB p95, queue latency, server CPU budget).

Roll up F4a-F4e into a single F4 row in the CHECK TABLE for brevity. If any of the five sub-targets is missing, F4 = `FAIL` and the evidence column uses the form `Missing: <F4a|F4b|F4c|F4d|F4e>` listing each absent sub-target (e.g. `Missing: F4b, F4d`).

Generator anchor: Section 3 Performance Targets spec + hard rule "Performance targets sit inside Good band per Web Vitals".

### F5. `(free alternative for X)` annotation rules

Allowed: when a genuine paid product is silently swapped for a free OSS equivalent in NON-font categories (e.g. `GlitchTip (free alternative for Sentry)`).
Banned: the specific phrase `Motion (free alternative for Framer Motion)` or any variant. Motion and Framer Motion are the same OSS package - the rename is not a substitution. Use `Motion` alone.
Banned: any `(free alternative for X)` in the Typography sub-section (silent font substitution only).
Generator anchor: Free OSS substitution table + hard rules on font substitution and Motion/Framer Motion.

---

## G. Section 4 - Site Architecture & Page Specifications

### G1. Up to 320 words total

Generator anchor: Section 4 word budget.

### G2. 4.1 Global Elements sub-section present

Must cover: header (height, scroll behavior), footer (NO copyright string, NO `Terms`, NO `Privacy` link text), toasts, skeleton policy, landmark order, global search affordance.
Specifically banned in 4.1:
- Any literal string matching `(c)`, `(C)`, `\u00A9` (©), `Terms`, `Privacy` as a footer link or legal line. (The Unicode `©` is already caught by A3; this check covers the ASCII `(C)` and the words `Terms` / `Privacy`.)
- Naming keyboard shortcuts for global search (no `Cmd+K`, `Ctrl+K`, `/`, `Cmd+/`, etc.). Refer to triggers by visible label (e.g. `header search icon`).

Generator anchor: Section 4.1 Global Elements spec + hard rules on footer legal text and keyboard shortcuts.

### G3. 4.2 Pages & Navigation Flows: nav structure block present

A nav-structure block at the top of 4.2 with 3 required bullets and up to 2 conditional bullets:

REQUIRED (must all appear):
- `**Primary nav:**` comma-separated labels.
- `**Post-login default route:**` single backticked path.
- `**Unauthenticated redirect:**` single backticked path.

CONDITIONAL (omit ENTIRELY when not applicable - do not emit a placeholder):
- `**Secondary nav:**` (omit if no secondary nav exists in the design).
- `**Role-gated nav items:**` (omit if no role-gated nav exists).

Checks:
- All 3 required bullets present. Missing any = `FAIL`.
- Each route inside `Post-login default route:` and `Unauthenticated redirect:` is backticked.
- If a conditional bullet is present, its content must be meaningful (no `N/A`, no `not applicable`, no empty string).

Generator anchor: Section 4.2 nav-structure block spec.

### G4. 4.2 Page list: parent bullet + 3 nested sub-fields, indented exactly 2 spaces

Each page block follows this structure - sub-field labels are NOT bolded (they match the Generator's example output):

```
- **Page Name**
  - Path: `/route/path`
  - Connects From: comma-separated list of arrival sources (nav items, button labels, redirects, links from other pages -- any are valid)
  - Key Interactions: prose sentence describing the user-facing interactions
```

Hard rules:
- Top-level bullet: `- **Page Name**` (the page name IS bolded).
- 3 nested sub-fields, indented by EXACTLY 2 spaces before the `-`:
  1. `Path:` (followed by a backticked route)
  2. `Connects From:` (followed by a comma-separated list of arrival sources: nav items, button labels, redirects, or links from other pages -- the Generator's own examples use `Logo, nav`, `'Write' button on dashboard`, `nav (after login)`, `byline click on article`; any such arrival shape is acceptable)
  3. `Key Interactions:` (followed by a prose sentence)
- Sub-field labels (`Path:`, `Connects From:`, `Key Interactions:`) are NOT bolded. If the PRD bolds them (`**Path:**`), this is a `Soft warning` - the Generator's examples show unbolded labels but tolerant parsing applies.
- Blank line between page blocks. Missing blank line = `Soft warning`.
- No markdown table (A7 already covers this).
- 6-10 pages typical. Outside this range = `Soft warning`, not auto-`FAIL`.

Checks:
- Locate the page list under 4.2. For each top-level `- **...**` bullet, the immediately following lines indented by 2 spaces must contain exactly the three required sub-fields in the order Path, Connects From, Key Interactions.
- A sub-field indented by 4 spaces or a tab is `FAIL`.
- A sub-field in the wrong order (e.g. Key Interactions before Connects From) is `FAIL`.
- A missing sub-field is `FAIL`.
- Routes inside `Path:` MUST be backticked. An unbacked route is `FAIL`.

Generator anchor: Section 4.2 page-list 4-field structure spec (wrong-vs-right examples in Generator) + hard rules on page-list structure and indentation.

### G5. 4.2 Routes are generic only

A route segment must not contain the source brand name (e.g. `pipedrive`, `salesforce`, `notion`, etc.) or the invented project name (e.g. if the invented name is `Trakr`, no `/trakr-deals`).
Check: for each backticked route in Section 4, verify it contains no project-name substring (case-insensitive) and no recognizable source-brand substring (from H1 brand list). If detection is ambiguous, surface in NOTES.
Generator anchor: hard rule "Routes are generic paths only".

### G6. 4.2 Page list satisfies category-required UI surfaces

The canonical Category-Specific Feature Emphasis catalog lists required UI surfaces per category. The page list must include every required surface for the chosen category.

Procedure: infer the PRD's category from Section 1's product description and Section 3's stack hints, then check Section 4.2 against the embedded catalog below. Output `Soft warning` for any non-defining missing surface. Output `FAIL` if a category-defining surface is missing (e.g. for News: an article-detail page; for CRM: a deals/pipeline view; for Knowledge: a course/lesson detail).

Embedded Category-Specific Feature Emphasis catalog (the 16 canonical categories and their required UI surfaces):

1. **Public Utility -- Public Services:** Bill viewer with payment status (paid / due / overdue); Service schedule / outage calendar; Document upload form; Account dashboard; Multi-step intake form (e.g. enrollment, service request).
2. **News -- Content:** Article reader (long-form, distraction-free); Topic / category navigation; Search and filter; Newsletter signup; Author profile.
3. **Publishing -- Content:** Rich text editor (Tiptap) with image embed, slash commands; Article reading view with estimated read time; Author profile and publication branding; Newsletter signup with subscriber list view; Publish flow (draft to preview to publish, with schedule option).
4. **Retail -- Transaction:** Product browse with filter (category, price, availability); Product detail with gallery and variant picker; Cart drawer with quantity controls; Multi-step checkout (shipping, payment form, review); Order confirmation and history.
5. **Services -- Transaction:** Service / provider discovery with filter; Provider detail with reviews and availability; Booking calendar with time-slot picker; Appointment confirmation and reschedule; Saved providers / appointments list.
6. **ERP -- SaaS Platforms:** Project board (kanban or list); Documentation / wiki reader-editor; KPI dashboard with widget grid; Integration settings panel; User and team management page.
7. **Knowledge -- Content:** Course / lesson structure with progress bars; Video player + transcript sidebar; Quiz or assessment components with results; Enrollment flow and certificate display; My-courses list with progress.
8. **Procurement -- Transaction:** Supplier discovery with filter (category, region, rating); RFQ wizard (multi-step request for quote); Bulk order form with line-item table; Comparison table (suppliers side-by-side, sticky columns); Order history and approval status.
9. **Vertical Markets -- Transaction:** Listing search with filter (location, price, type); Listing detail with gallery, map, contact form; Booking or inquiry flow; Saved listings / favorites; Compare listings side-by-side.
10. **HCM -- SaaS Platforms:** Employee profile with edit; Time-off calendar with request flow; Org chart with drill-down; Payroll view (read-only fixture); Onboarding checklist.
11. **CRM -- SaaS Platforms:** Contact / lead list with pipeline stage chip; Deal board (kanban) with drag across stages; Activity timeline per contact / deal; Dashboard with sales-metrics charts (sparklines per KPI); Email integration UI (linked emails on contact timeline); Team / shared-view selector.
12. **Gov. Portal -- Public Services:** Multi-step form with progress indicator; Document upload with validation; Application status tracker; Search across services / forms; Account dashboard with submitted-application list.
13. **Community -- Content:** Forum thread list with vote / reply counts; Thread detail with nested replies and vote buttons; User profile with reputation badges; Notification center; Tag / category navigation.
14. **TMS -- SaaS Platforms:** Kanban board with drag, swimlanes; Calendar view of tasks / sprints; Sprint backlog with planning poker chip; Comment thread on each task; Settings (workflows, statuses, custom fields).
15. **Multimedia -- Content:** Media player with controls (play, seek, volume, captions, speed); Playlist / queue management; Content discovery grid with category rails; Search with filter (genre, duration, year); My-library / favorites.
16. **AI Platform -- SaaS Platforms:** Model / prompt playground with parameter sliders; API key list with rotate / revoke modal; Usage dashboard with token charts; Conversation / history list; Settings (model defaults, system prompts).

Every page in the emphasis list must appear in Section 4.2; every flow implied by the list informs Section 6 Main UI Flows.

If the PRD's category does not clearly map to one of the 16 catalog entries, infer the closest match, name the chosen catalog category in evidence, and proceed. If two catalog categories are equally plausible, evaluate against both and `FAIL` only if a surface is missing under both.

Generator anchor: hard rule "Page list must include every required UI surface from Category-Specific Feature Emphasis block".

### G7. 4.3 Interactive Elements: triggers by visible label, motion specs, error wording

Each interactive element bullet (modal, drawer, dropdown, popover, tab, editor, global search modal, toast, form) must include:
- Trigger described by visible button/icon label, never by keyboard shortcut.
- Key UI labels in single quotes.
- Exact ms value AND a `cubic-bezier(` easing function (e.g. `220ms cubic-bezier(0.2, 0.0, 0.0, 1.0)`).
- Per-field error wording in single quotes for any form bullet.

Checks:
- No keyboard shortcut tokens (defer to H4 - if H4 fires inside Section 4.3 scope, that row captures the violation; G7 only re-flags if H4 missed it).
- `Escape` may appear at most ONCE per modal bullet as the accessibility dismiss key. More than one `Escape` per modal-affordance bullet, or `Escape` named as the primary trigger, is `FAIL`.
- Every animation/transition line in Section 4.3 has BOTH a `\d+ms` value AND a `cubic-bezier(` substring. Named easings (e.g. `ease-out`, `ease-in-out`) alone are `FAIL` - the Generator mandates explicit cubic-bezier on every motion line.
- Editor rich-text engines named must be from {`Tiptap`, `Lexical`, `ProseMirror`, `Slate`, `BlockNote`}. `document.execCommand` is `FAIL`.

Generator anchor: Section 4.3 Interactive Elements spec + hard rule "Every animation: exact ms + cubic-bezier easing".

### G8. 4.4 Real-time Simulation & Notifications

The Generator permits omitting 4.4 entirely (no heading, no body) if not in scope. Do not flag a missing 4.4 as `FAIL` - mark G8 `N/A` with evidence `"Section 4.4 omitted - within Generator carve-out"`.

If 4.4 IS present, it must specify:
- Polling interval or simulated WebSocket driver (e.g. `KPIs polled every 30s from fixtures` or `setInterval driver mutating client state`).
- Presence simulation.
- In-app notifications.
- Optimistic UI + rollback contract.

If 4.4 is present, the body MUST NOT contain placeholder strings (`not applicable`, `N/A`, `not in scope`, `limited to self-service`). These placeholders are forbidden - omit the entire sub-section instead.
Regex (case-insensitive, scoped to 4.4 body if present): `(?i)\b(not\s+applicable|not\s+in\s+scope|limited\s+to\s+self[\s-]service|\bn/?a\b)\b`. If a match occurs and 4.4 is present, `FAIL`.

Generator anchor: Section 4.4 spec + hard rule "Omit not-in-scope sub-sections entirely. No 'not applicable' placeholder."

---

## H. Forbidden tokens and hard-rule banned content (Generator: pre-emit forbidden-token scan + HARD RULES list)

### H1. No source product / brand name

Banned: any common brand name that the PRD might have been "based on". The Generator does not enumerate brands but the hard rule forbids naming the source product.

Heuristic banned-list (extend as the reviewer recognizes more):
- CRM/sales: `Pipedrive`, `Salesforce`, `HubSpot`, `Close`, `Copper`, `Zoho CRM`, `Freshsales`
- Productivity/notes: `Notion`, `Obsidian`, `Roam`, `Coda`, `Craft`
- Project/work mgmt: `Linear`, `Asana`, `Trello`, `Monday`, `Monday.com`, `Jira`, `ClickUp`, `Basecamp`
- HR/recruiting: `Workday`, `BambooHR`, `Greenhouse`, `Lever`, `Gusto`, `Rippling`
- Learning: `Coursera`, `Udemy`, `Khan Academy`, `Skillshare`, `Pluralsight`, `LinkedIn Learning`
- Commerce: `Shopify`, `Walmart`, `Target`, `Amazon (as marketplace)`, `eBay`, `Etsy`, `Wayfair`
- Publishing/news: `Substack`, `Medium`, `Ghost`, `New York Times`, `The Guardian`, `Bloomberg`, `WSJ`, `Reuters`, `CNN`, `BBC`, `Vimeo`
- Media: `Spotify`, `YouTube`, `Netflix`, `Twitch`, `Apple Music`, `Disney+`
- Community/social: `Reddit`, `Discord`, `Slack` (Slack is OK as integration carve-out in Section 6 only), `Twitter`, `X`, `Mastodon`, `Bluesky`, `LinkedIn`, `Stack Overflow`, `Hacker News`
- Data/analytics: `Palantir`, `Snowflake`, `Databricks`, `dbt`, `Looker`, `Tableau`, `Power BI`
- DevTools/AI: `OpenAI`, `Anthropic`, `Replicate`, `Hugging Face`, `GitHub Copilot`
- Comms: `Twilio`, `Sendgrid`, `Mailchimp`, `Intercom`, `Zendesk`, `Freshdesk`, `Drift`
- Real estate/travel: `Zillow`, `Realtor.com`, `Booking.com`, `Airbnb`, `Expedia`
- ERP: `NetSuite`, `SAP`, `Microsoft Dynamics`, `Oracle ERP`
- Hiring/jobs: `Indeed`, `Glassdoor`, `LinkedIn (as jobs)`, `AngelList`
- Search: `Algolia`, `Typesense (as paid-cloud only)`
- Government: `IRS`, `GOV.UK`, `USA.gov`, `Service Canada`
- Procurement (B2B): `Alibaba (B2B)`, `SAP Ariba`, `Coupa`

Add to this list any brand the QC reviewer recognizes in the PRD. The list is not exhaustive.

Scope: entire PRD body.

Carve-outs:
- A brand may be named in Section 6 UI flows ONLY when the Generator's "real third-party integration partners" carve-out applies: Gmail, Outlook, Google Calendar, Slack, Zapier, Stripe-as-payment-processor, GitHub (for OSS code-host integrations), Dropbox/Google Drive (for file-import integrations). In that carve-out, the brand is described as a UI affordance ("connect Gmail"), not as the source product being cloned.
- The Generator's own substitution table may name brands as banned (e.g. `Sentry self-hosted` is banned in F3/H9). Mentioning them once in a "do not use X" context is acceptable - but the Generator's output PRD never says "do not use X"; it just doesn't use X.

Generator anchor: hard rule "Never name real source product / brand".

### H2. No source URL, no fabricated external URLs in body

Scope: entire PRD body.
Banned (any of):
- Any HTTP/HTTPS URL pointing to a recognized source brand's marketing or app domain.
- Any fabricated external URL invented for narrative purposes (e.g. `https://api.example.com/things`, `https://acme-corp.io`).

Regex (heuristic): `https?://[^\s)`]+`

For each match:
1. If the host is on a known source-brand domain (heuristic from H1 list), `FAIL`.
2. If the host is a fabricated external domain, `FAIL`.
3. Carve-out: backticked URLs inside Open Graph examples in Section 8 (`og:url`) MAY be the invented project's own placeholder domain (e.g. `https://trakr.app`) - this is OK because it's the project's own canonical URL.

Generator anchor: hard rules "Never expose source URL" + "Never invent fake external URLs in body".

### H3. No API endpoint paths in flow text

Banned in Section 6 and Section 4: `PATCH /v1/...`, `POST /api/...`, `GET /...`, `DELETE /...`, `PUT /...` and similar verb+path patterns.
Regex: `\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/[A-Za-z0-9/_.{}-]+`
No carve-out: the canonical rule forbids the HTTP-verb-plus-path notation regardless of formatting (backticks, fenced code blocks, plain prose). Every regex match = `FAIL`.

Note for reviewer: HTTP verb + slash combos that appear in prose ("the user clicks GET /") would be highly unusual; almost all violations will be table-form or arrow-form endpoint listings. Document false-negative risk in NOTES if the PRD describes API calls in prose without the verb-prefix convention.

Generator anchor: hard rule "No API endpoint paths in flow text".

### H4. No keyboard shortcuts anywhere

Banned tokens (case-sensitive matching - the shortcuts use capitalized modifier prefixes):

Regex patterns (each is a separate scan):
- `\bCmd[\s+\-][A-Z0-9/\\]` (e.g. `Cmd+K`, `Cmd-/`, `Cmd K`)
- `\bCtrl[\s+\-][A-Z0-9/\\]`
- `\bAlt[\s+\-][A-Z0-9/\\]`
- `\bShift[\s+\-][A-Z0-9/\\]`
- `\bMeta[\s+\-][A-Z0-9/\\]`
- `\bWin[\s+\-][A-Z0-9/\\]`
- `\bOption[\s+\-][A-Z0-9/\\]`
- `\bMeta(?=[\s+\-][A-Z]\b)` (Meta standalone followed by a single uppercase letter at word boundary -- the shortcut-key shape, e.g. `Meta K`; multi-letter follow-ons like `Meta Title`, `Meta tag`, `Meta data` are not shortcuts and pass)
- `\bWin\b(?=[\s+\-A-Z])` (Win standalone followed by shortcut context)
- Parenthetical asides: `\((Cmd|Ctrl|Alt|Shift|Meta|Win|Option)[\s+\-][A-Z0-9/\\]`

Carve-outs:
- `Escape` may appear ONCE per modal as the accessibility dismiss key. It must never be named as a primary feature trigger.
- The word `control` (lowercase, no `+`/`-` suffix) is allowed in prose: `access control`, `version control`, `quality control`.
- The word `option` (lowercase, no `+`/`-` suffix) is allowed in prose: `select an option`.
- The word `meta` (lowercase, no shortcut context) is allowed in `metadata`, `meta tag`, `og:site_name` discussion.
- Capitalized `Meta` is allowed when followed by a meta-tag word (`Meta tag`, `Meta title`, `Meta description`, `Meta keywords`, `Meta data`) -- the refined `\bMeta(?=[\s+\-][A-Z]\b)` lookahead skips these because it requires a single uppercase letter at word boundary (the shortcut shape) and `Title`, `Description`, `Tag` are multi-letter follow-ons.

Known gap (document in NOTES): a PRD might quote a verbatim UI element named "Command Bar" or "Command Palette". The regex above does not match `Command` (no `+`/`-`), but the reviewer should still flag any UI element that implies a keyboard-driven invocation pattern - that pattern is the very thing the Generator forbids when describing global search affordances.

Generator anchor: hard rule "No keyboard shortcuts anywhere in PRD output, including parenthetical asides".

### H5. No banned hype adjectives without numeric/concrete reference

Banned bare adjectives: `modern`, `clean`, `sleek`, `seamless`, `beautiful`, `intuitive`, `robust`, `scalable`, `elegant`, `cutting-edge`, `world-class`, `next-generation`, `best-in-class`, `delightful`, `frictionless`.

Each adjective is `FAIL` if it appears without a numeric or concrete reference within the same sentence (e.g. `intuitive 3-tap onboarding` is `PASS`; `intuitive interface` is `FAIL`).

Implementation note: this check is heuristic and prone to false positives. If a banned word appears in a sentence containing a numeric value, a millisecond duration, a hex color, a pixel measurement, or a quoted UI label, mark `PASS` with carve-out. Otherwise `FAIL`. Document the heuristic call in NOTES when ambiguous.

Known gap (document in NOTES): the heuristic does not catch a sentence with a numeric reference that is purely decorative (e.g. `intuitive interface with 4 main pages` - the `4` is not anchoring the adjective). Reviewer judgment applies.

Generator anchor: hard rule "Banned words unless backed by numeric/concrete reference".

### H6. No copyright line, no `Terms` / `Privacy` legal strings (narrow carve-outs for non-legal uses)

Scope: entire PRD.
Primary regex (case-insensitive): `\bcopyright\b`, `\([Cc]\)`, `©`, `\bterms\b`, `\bprivacy\b`
Secondary regex (full copyright line, case-insensitive): `copyright\s+(\([cC]\)|©)?\s*\d{4}`

Carve-outs (narrow, scope to non-legal contexts only):
- The word `privacy` is allowed ONLY when it does NOT refer to a footer link, a meta field, a route, or a legal policy. `user privacy considerations` in Section 7 ARIA prose = `PASS`. `Privacy Policy` link text = `FAIL`. `/privacy-policy` route = `FAIL`. A meta field named `privacy` = `FAIL`.
- The word `terms` is allowed ONLY when it does NOT refer to legal acceptance. `search terms` or `terms of art` = `PASS`. `Terms of Service` link text or sign-up checkbox = `FAIL`. A sign-up flow that says `the user accepts the platform agreement` (without the literal word `Terms`) = `PASS`.
- A `(C)`, `(c)`, or `©` copyright glyph anywhere = `FAIL` with no carve-out. The phrase `Copyright YYYY` (any year) anywhere = `FAIL`.

Banned forms (all auto-`FAIL`):
- A footer line in Section 4.1 mentioning copyright, a `Terms` legal link, or a `Privacy` legal link.
- A meta or structured-data block in Section 8 with a `copyright` field.
- A `Privacy Policy` or `Terms of Service` link as a footer affordance or sign-up checkbox.
- A `(C)` / `(c)` / `©` glyph anywhere.
- A `Copyright 2026` (or any year) line anywhere.

Generator anchor: hard rule "No copyright line, Terms link text, or Privacy line in PRD output" + Section 4.1 footer spec + Section 8 spec. The Generator scopes the ban to legal/footer strings; the carve-outs above preserve faithful usage of `privacy` in ARIA prose and `terms` in search-term references.

### H7. No unfilled `{{placeholder}}`

Regex: `\{\{[^}]+\}\}`
Any match is `FAIL`.
Generator anchor: pre-emit forbidden-token scan "Any `{{placeholder}}` left unfilled".

### H8. No `Framer Motion` reference (use `Motion` alone)

Banned tokens (case-insensitive): `Framer Motion`, `Framer-Motion`, `framer/motion`, `framer.motion`.
The npm package coincidentally is `framer-motion` (legacy), but the PRD must say `Motion` alone (modern OSS package name).
Regex: `(?i)framer[-\s/.]motion`
Carve-out: backticked code import lines like `import { motion } from 'motion/react'` are fine - the new package name is `motion`. `npm install motion` is fine. The string `motion` alone (no `framer` prefix) is fine.
Generator anchor: Free OSS substitution table "Motion (fully free under Webflow since 2024)" + hard rule "Never `Motion (free alternative for Framer Motion)`".

### H9. No banned animation, hosting, rich-text, error-tracking, image-tooling, or auth choices

This is the "free OSS stack hygiene" pass. Surface as `Soft warning` for each banned choice, plus the recommended replacement.

| Banned | Recommended replacement |
|--------|-------------------------|
| Cloudflare Pages | Cloudflare Workers (Static Assets), Netlify free tier, GitHub Pages |
| Sentry self-hosted (BSL, not OSS) | GlitchTip |
| Squoosh CLI (archived 2024) | sharp, vite-imagetools, imagemin |
| `document.execCommand` (deprecated since 2015) | Tiptap, Lexical, ProseMirror, Slate, BlockNote |
| Better Auth, Auth.js, NextAuth, Lucia, Supabase Auth, Clerk, Auth0 (in Section 6 Session UI Shell) | localStorage token string + mock user switcher; do not name a provider |
| Socket.IO server (or any real WebSocket backend) | setInterval polling, mock event bus, MSW WebSocket mocks |
| OpenStreetMap public tiles (in production stack) | MapLibre GL JS + OpenFreeMap, or self-hosted Protomaps |

Generator anchor: Free OSS substitution table in the Generator.

### H10. No `[VERIFY]` annotations in PRD output

Banned: any reviewer-style `[VERIFY -- not in canonical table]` or similar `[VERIFY ...]` markers. The Generator's confidence-only policy says commit cleanly when confident, omit when not.

Regex: `\[VERIFY[^\]]*\]`

Carve-out: `[GAP -- no full-parity OSS substitute]` IS allowed, sparingly. This marker indicates a genuine open question where the Generator could not find a free substitute and explicitly flags it. Do not flag `[GAP -- ...]` as a violation.

Generator anchor: Free OSS substitution rules confidence-only policy + Generator's hard rule on off-table substitutions ("no [VERIFY] markers").

### H11. Category match before substitution (heuristic, Soft warning)

The Generator: "Category match before substitution". If the PRD substitutes a stack item that does not fit the chosen category (e.g. naming a payments library in a category that has no commerce surfaces; naming a maps library in a category with no maps surfaces), surface as `Soft warning`.

Implementation note: this is genuinely heuristic. A PRD with no commerce surfaces should not name `Stripe` in Section 6 Checkout (the Checkout sub-section should be omitted entirely). A PRD with no maps should not name MapLibre. Cross-check Section 3 stack against Section 4 page list and Section 6 flows. Flag mismatches as `Soft warning`.

Generator anchor: hard rule "Category match before substitution".

---

## I. Section 5 - Motion Language (Generator: Section 5 spec)

### I1. Up to 80 words

Generator anchor: Section 5 word budget.

### I2. Every motion line has a millisecond value AND a cubic-bezier easing

Scope: text under `## 5. Motion Language`.
Check: identify all motion-spec sentences/bullets (lines describing a transition, animation, route transition, stagger, hover, scroll-triggered behavior). Each such sentence MUST contain:
- A `\d+ms` numeric value.
- A `cubic-bezier(` substring with concrete numeric arguments.

Named easings alone (`ease-out`, `ease-in-out`, `linear`) are `FAIL` - the Generator mandates explicit `cubic-bezier(` on every motion line.

Generator anchor: Section 5 motion-physics spec + hard rule "Every animation: exact ms + cubic-bezier easing".

### I3. Section 5 declares the reduced-motion contract

Defers to C1 scoped to Section 5. If C1 passes (Section 5 declares a `prefers-reduced-motion` contract with an accompanying behavior clause), I3 = `PASS`. If C1 fails, I3 = `FAIL` with evidence pointing to the C1 row. I3 never independently FAILs - it mirrors the C1 outcome for operator clarity in the Section 5 view of the report.
Generator anchor: Section 5 required surfaces include the `prefers-reduced-motion` contract.

### I4. Section 5 declares the required motion surfaces

The Generator requires Section 5 to cover these surfaces (one short statement each is sufficient):
- Default duration band (e.g. `motion durations sit between 180ms and 320ms`).
- Named cubic-bezier easing curves (at least one `cubic-bezier(...)` call).
- Route/page transitions.
- Stagger policy (e.g. list stagger, grid stagger).
- Hover defaults (e.g. `cards lift 4px on hover over 160ms`).
- Scroll-triggered behavior (e.g. `hero parallax`, `reveal on scroll`).

Checks (each as a sub-flag rolled into one I4 row):
- I4a. Default duration band stated.
- I4b. At least one explicit `cubic-bezier(` call.
- I4c. Route transitions described.
- I4d. Stagger policy described.
- I4e. Hover defaults described.
- I4f. Scroll-triggered behavior described.
- I4g. `prefers-reduced-motion` contract stated with a behavior clause (this surface is also enforced by C1; I4g passes iff C1 passes).

If any of I4a-I4g is missing, I4 = `FAIL` and the evidence column names which sub-surface is absent.

Generator anchor: Section 5 motion-language required surfaces (including the `prefers-reduced-motion` contract).

---

## J. Section 6 - Application Logic (Generator: Section 6 spec)

### J1. Heading and word budget

Heading: `## 6. Application Logic` exactly (see B3).
Word budget: up to 470 words, including H3 sub-section headings.
Generator anchor: Section 6 heading + word budget.

### J2. Sub-section presence and order

Up to 7 sub-sections (H3 or H4 in the Generator's example), in this order. Two of these (sub-sections 6 and 7 below) are omittable entirely when not in scope. A third omittable sub-section in the Generator spec (4.4 Real-time) lives in Section 4 and is handled by G8, not by J2.
1. User Roles (required)
2. Session UI Shell (required)
3. Sign-up / Sign-in UI Flow (required)
4. View Models & Fixtures (required)
5. Main UI Flows (required)
6. Checkout & Billing UI (omittable - omit entirely if no self-service checkout/billing)
7. Admin UI Surfaces (omittable - omit entirely if no organization-level admin)

`FAIL` if any required sub-section is missing.
`FAIL` if an omittable sub-section is present but contains a placeholder string (`not applicable`, `N/A`, `not in scope`, `limited to self-service`).
`Soft warning` if order is inverted.

If an omittable sub-section is correctly omitted, mark the corresponding J8 or J9 row `N/A` with evidence `"Sub-section omitted - within Generator carve-out"`.

Generator anchor: Section 6 sub-section spec + hard rule "Omit not-in-scope sub-sections entirely".

### J3. User Roles: 2-4 numbered bullets, UI / nav contract only

Form: `1. **RoleName:** which screens, nav regions, primary UI actions.`
`FAIL` if a role bullet describes server-side authz, JWT scopes, RBAC enforcement at the API layer, or any backend mechanism. The role is described in terms of "which UI surfaces this role sees and can interact with".
Word budget: up to 50 words for the whole User Roles sub-section.
Generator anchor: Section 6 User Roles spec.

### J4. Session UI Shell: localStorage token shape, no real auth provider

Required content (full form): which auth screens exist (sign-in, sign-up, forgot-password if applicable), the mock user switcher behavior, the localStorage token shape (high-level, e.g. `{ user_id, role, expires_at }`), the session-timeout banner behavior.

Banned: naming a real auth provider (Better Auth, Auth.js, NextAuth, Lucia, Supabase Auth, Clerk, Auth0). See H9.

Carve-out for short-form: the Generator permits a very short form when the product does not have public signup (e.g. private-invitation only). The Generator's example: `Session: mock user in localStorage; no signup (private invitation only).` This compact form is acceptable when the product surface justifies it.

Word budget: up to 60 words.

Generator anchor: Section 6 Session UI Shell spec.

### J5. Sign-up / Sign-in UI Flow: ONE paragraph of `->` arrow-linked steps

Form: a single paragraph of short steps joined by the ASCII right-arrow `->`. NOT prose with periods, NOT a numbered list, NOT bullet steps.

The Generator REQUIRES `->` arrows specifically here, and carves out this sub-section from the global `->` ban (A2). Faithful example from the Generator:
`User clicks 'Sign up' -> fills email, password, and company_name -> clicks 'Create account' -> simulated success delay of 1200ms -> onboarding screen appears -> user lands on the default route.`

Required content (FAIL if missing):
- At least 3 `->` separators in the paragraph (a 3-arrow minimum captures the multi-step flow shape).
- Validation rules per Section 4.3; per-field error wording in single quotes if errors are referenced.

Soft-warning surfaces (canonical example has these but spec text does not explicitly mandate per-flow):
- A simulated success delay clause, e.g. `simulated success delay of 1200ms` or any text matching `(?i)simulated.*\d+\s*ms`. The Generator's example uses `1200ms`; any reasonable ms value (200ms-3000ms range) is acceptable. Missing = `Soft warning`.

Banned (FAIL conditions):
- Period-only prose form with NO `->` arrows (this was an earlier QC form; the current Generator requires arrows here).
- A numbered list (e.g. `1. User clicks 'Sign up' ...`).
- Bullet-point steps (3+ lines starting with `-` inside the sub-section body describing each step).
- Mixing `->` with ` > ` or `=>` inconsistently as step separators (Soft warning -- pick one form; `->` is the Generator-required form).
- A real auth provider name (Better Auth, Auth.js, NextAuth, Lucia, Supabase Auth, Clerk, Auth0).

Word budget: up to 60 words.

Scoping: the J5 check applies to text between the `#### Sign-up / Sign-in UI Flow` heading and the next `####` heading (or `###` / `##` if the sub-section is last in Section 6).

Regex hints (scoped to Sign-up sub-section body):
- Required arrow count: count occurrences of ` -> ` (with surrounding whitespace) in the sub-section body. `FAIL` if 0 arrows (means the period-prose form was used instead of arrow form). `Soft warning` if 1-2 arrows (less than the 3-arrow heuristic minimum the Generator example suggests). `PASS` if `>= 3` arrows.
- The Sign-up sub-section permits `->` as the canonical separator. ` > ` and `=>` are not Generator-banned (per the QC's A2 narrowing), so they neither pass nor fail J5 explicitly; if they appear in unusual quantity, that is a Soft warning, not auto-FAIL.
- Numbered list: any line matching `^\s*\d+\.\s+` within the sub-section body - `FAIL`.
- Bullet steps: 3+ lines matching `^\s*-\s+` within the sub-section body (a single paragraph wrap is allowed) - `FAIL`.
- Missing simulated delay: `(?i)simulated.*\d+\s*ms` is the Generator's example pattern (1200ms in the canonical line). If no match, `Soft warning` -- the simulated-delay clause is in the Generator example but not explicitly mandated by the spec text.

A2 dedup: A2 explicitly carves out the Sign-up sub-section from the `->` ban (see A2 carve-outs). Inside J5's scope, A2 does not fire on `->`. If a regression-test version of A2 fires on `->` inside the Sign-up sub-section, the operator should treat that A2 hit as a false positive and trust J5's reading.

Generator anchor: Section 6 Sign-up flow `->` arrow-step form spec + hard rule "Sign-up / Sign-in UI Flow uses one paragraph of `->` arrow-linked steps".

### J6. View Models & Fixtures: EXACTLY 6-8 entities, 5-7 fields each

Form: one TOP-LEVEL bullet per entity. Each bullet starts `- **EntityName:**` and is followed by 5-7 comma-separated fields.

Scoping rule (CRITICAL for counting): "TOP-LEVEL bullets" means lines matching `^- \*\*[A-Za-z][A-Za-z0-9_]*:\*\*` (zero leading whitespace, `- **`, an identifier, then `:**`). Nested sub-bullets (indented, or under a `Relationships:` block) are excluded from the entity count.

Foreign keys are written `foreign_key_id (id ref to OtherEntity)`. ANY field name ending in `_id` (other than the primary key field bare `id`) MUST be followed immediately by `(id ref to ...)` annotation.

For rich content: pick ONE storage shape - either `content (JSON AST)` OR `content_html (string)` - not both for the same entity.

For money: `total_cents (integer)` or similar `_cents` suffix is the canonical form.

Sample values use the invented project name where a sample row is emitted (e.g. `name: 'Trakr'`). Sample VALUES (not entity names) carry the project name. The Generator does not require Section 6 to emit a sample row inline -- View Models is a fields list; the invented-name consistency check lives in M1. No warning if a sample row is not emitted.

Then a `Relationships:` sub-block with 4-6 statements. Each Relationships statement must contain at least one of: `has many`, `belongs to`, `may convert to`.

Checks:
- Count of TOP-LEVEL `- **EntityName:**` bullets is in `[6, 8]`. Outside this range = `FAIL`.
- Each bullet has 5-7 comma-separated fields after the colon. Outside this range per entity = `FAIL`.
- Every field ending in `_id` (except bare `id`) is followed by `(id ref to <EntityName>)`. Missing annotation = `FAIL`.
- No entity has BOTH `content (JSON AST)` AND `content_html` for the same rich-content storage.
- All money fields end in `_cents` and are typed `(integer)`. A bare `total` field without `_cents` is `Soft warning`.
- `Relationships:` block has 4-6 statements.
- Each Relationships statement contains at least one of `has many` / `belongs to` / `may convert to`. Missing the verb = `Soft warning`.

Word budget: up to 110 words.

Generator anchor: Section 6 View Models & Fixtures spec + hard rule "View Models cover exactly 6-8 entities with 5-7 fields each".

### J7. Main UI Flows: EXACTLY 3 flows, arrow-step paragraphs, Create/lifecycle/role-gated triad

Form: three flows, each a SINGLE arrow-step paragraph using ` -> ` (ASCII arrow with spaces) as the step separator, 25-35 words each. Each flow starts `**Flow Name (Role):**` followed by the arrow-step paragraph. Example shape: `**Create Deal (Sales Rep):** User clicks 'New deal' -> drawer opens -> fills name, amount, stage -> clicks 'Save' -> simulated submit of 1200ms -> fixture updates -> success toast displays -> the new deal appears at the top of the pipeline.`

The three flows MUST cover the Create/dominant lifecycle/role-gated triad:
- Flow (a): a CREATE flow (creating the primary entity for this category - e.g. create a deal, create an article, create a course).
- Flow (b): the DOMINANT LIFECYCLE action for this category (e.g. for CRM: advancing a deal stage; for News: publishing an article; for Knowledge: enrolling/completing a course).
- Flow (c): a ROLE-GATED action visible only to admins/managers/owners (the role in the flow header should be different from the two roles in flows a and b).

Checks:
- EXACTLY 3 flows. Outside this count = `FAIL`.
- Each flow's header `**Flow Name (Role):**` is present.
- Each flow paragraph is in `[25, 35]` words (whitespace-split). Outside this range per flow = `FAIL` (the canonical rule "25-35 words" is binding on each flow paragraph, not a soft guide).
- Each flow paragraph contains at least 3 ` -> ` separators (minimum arrow count to qualify as an arrow-step paragraph). Fewer than 3 = `FAIL`.
- Flow (a) is a Create action: the verb in the flow name or first sentence is `create`, `add`, `new`, `start`, `compose`, `draft`, or similar.
- Flow (b) describes the category's primary lifecycle verb (heuristic, requires reviewer judgment).
- Flow (c) is gated by a role distinct from flows (a) and (b) (e.g. flow (c) header says `(Admin)` or `(Manager)` or `(Owner)`).

Banned:
- Numbered lists (e.g. `1. User clicks ...`).
- Period-separated prose form (the older "short sentences separated by periods" shape). A flow paragraph whose only step separator is the period, with no ` -> ` arrows, = `FAIL`.
- API endpoint paths (caught by H3 and J10).
- Mention of real backend mechanics (database transactions, queue dispatch, server-side validation, JWT issuance).

A2 carve-out applies: ` -> ` is EXEMPT inside Main UI Flows. ` -> ` outside the Sign-up and Main UI Flows sub-sections still FAILs A2.

Each flow MUST mention (Generator-required form):
- A user action with the visible label in single quotes (e.g. `User clicks 'Save'`). Missing the single-quoted label = `FAIL` (this is the canonical `User clicks 'X'` shape).

Soft-warning surfaces (canonical examples have these but do not strictly mandate per-flow):
- A simulated submit/load delay (e.g. `simulated submit of 1200ms`). Regex hint: `(?i)simulated.*\d+\s*ms`. Missing = `Soft warning`.
- A fixture update (e.g. `the fixture updates`, `the deal appears in the pipeline`). Missing = `Soft warning`.
- A terminal UI state (e.g. `a success toast displays`, `the row appears at the top of the list`). Missing = `Soft warning`.

Word budget: up to 100 words across all three flows combined.

Generator anchor: Section 6 Main UI Flows arrow-step form spec + hard rule "Main UI Flows uses arrow-step paragraphs (not numbered lists)".

### J8. Checkout & Billing UI: omit entirely if not in scope; else up to 30 words

If absent from Section 6, mark J8 `N/A` with evidence `"Checkout sub-section omitted - within Generator carve-out"`.

If present:
- Word budget: up to 30 words.
- Form: cart, review, mock payment form, confirmation (described in PROSE; no literal `->` or ` > ` separator).
- Banned: real payment provider implementations (`Stripe Elements`, `Lemon Squeezy`, `Paddle`) named as the implementation. `Stripe` may be named ONLY as a UI affordance ("connect Stripe-as-payment-processor"). Mock checkout only.

Generator anchor: Section 6 Checkout & Billing UI spec.

### J9. Admin UI Surfaces: omit entirely if not in scope; else up to 30 words, 3-5 bullets

If absent from Section 6, mark J9 `N/A` with evidence `"Admin sub-section omitted - within Generator carve-out"`.

If present:
- Word budget: up to 30 words.
- Form: 3-5 bullets (typical: user list, custom-field settings, audit log, workspace settings).

Generator anchor: Section 6 Admin UI Surfaces spec.

### J10. No API endpoint paths anywhere in Section 6

Re-application of H3 scoped to Section 6. If H3 fires inside Section 6 scope, J10 = `FAIL` with same evidence; if H3 passes, J10 = `PASS`.
Generator anchor: hard rule "No API endpoint paths in flow text".

---

## K. Section 7 - Accessibility & Quality (Generator: Section 7 spec)

### K1. Up to 60 words

Generator anchor: Section 7 word budget.

### K2. Contrast ratios as numeric WCAG AA (or AAA), both ratios stated

Required: explicit numeric ratios. WCAG AA requires BOTH `4.5:1` (body) AND `3:1` (large text). WCAG AAA requires BOTH `7:1` (body) AND `4.5:1` (large text).

`FAIL` if Section 7 mentions "WCAG AA" or "WCAG AAA" without numeric ratios in the same line.
`FAIL` if only one of the two required ratios appears (e.g. only `4.5:1` without the corresponding `3:1` for AA).

Regex hints (scoped to Section 7):
- Look for `4.5:1` AND `3:1` together for AA compliance.
- Look for `7:1` AND `4.5:1` together for AAA compliance.

Evidence guidance: if exactly one ratio appears in Section 7, the evidence column should name (a) which WCAG standard the PRD seems to target (AA or AAA, based on which ratio it cited) and (b) which complementary ratio is missing (e.g. `AA requires both 4.5:1 body and 3:1 large; only 4.5:1 found -- 3:1 missing`).

Generator anchor: Section 7 contrast spec.

### K3. Touch targets at least 44px

Required line or bullet: `44px minimum` (or larger - e.g. `48px`).
Regex hint: `\b(4[4-9]|[5-9]\d|\d{3,})\s*px\b` in proximity to `touch`, `tap`, `hit area`.
Generator anchor: Section 7 touch-target spec.

### K4. Keyboard navigation + focus ring specified

Required: focus ring color (typically a backticked hex), width in px, offset in px.
Generator anchor: Section 7 keyboard navigation spec.

### K5. Screen-reader pattern

Required content: ARIA on icon-only buttons, labels on form fields, live regions for async state.
Generator anchor: Section 7 screen-reader spec.

### K6. Reduced-motion cross-reference to Section 5

Defers to C2. If C2 passes (Section 7 cross-references Section 5 on reduced motion), K6 = `PASS`. If C2 fails, K6 = `FAIL` with evidence pointing to the C2 row. K6 never independently FAILs - it mirrors the C2 outcome for operator clarity in the Section 7 view of the report.
Generator anchor: Section 7 required surface "reduced-motion cross-reference to Section 5".

---

## L. Section 8 - Content & SEO (Generator: Section 8 spec)

### L1. Up to 60 words

Generator anchor: Section 8 word budget.

### L2. One `<h1>` per route specified

Required: a line stating that each route uses a single `<h1>` tag for semantic HTML.
Generator anchor: Section 8 semantic-HTML spec.

### L3. Open Graph image 1200x630

Required: an `og:image` reference with the 1200x630 dimensions stated explicitly.
Regex hint: `og:image` in proximity to `1200x630` or `1200 by 630` or `1200,630`.
Generator anchor: Section 8 Open Graph spec.

### L4. Structured data per template

Required (BOTH surfaces; missing either = `FAIL`):
(a) At least one structured-data type named (Organization, Article, Product, FAQ, Course, JobPosting, Event, BreadcrumbList, etc.).
(b) Explicit reference to `robots` / `sitemap` / `canonical` / `noindex` policy (at least one of the four named; all four named is the canonical form).

Evidence format on FAIL: `"Missing: structured-data type"` or `"Missing: robots/sitemap/canonical/noindex policy"`.
Generator anchor: Section 8 required surfaces "structured data per template" AND "robots/sitemap/canonical/noindex" (co-equal bullets, not optional flourish).

### L5. Microcopy formulas

Required: at least one mention of how titles, descriptions, or meta tags are templated (e.g. `<Page Title> | <Project Name>`).
Generator anchor: Section 8 microcopy spec.

### L6. No copyright line, no `Terms` / `Privacy`

Re-application of H6 scoped to Section 8. The meta and structured-data references must use the invented project name as `og:site_name` and as the Organization name (no real source brand, no copyright field).
Generator anchor: hard rule + Section 8 spec.

### L7. Invented project name appears in meta titles, og:site_name, AND structured-data Organization

Cross-checks D2 and M1. The invented project name MUST appear in ALL THREE of:
- (a) at least one meta title example (e.g. `meta title '<Category-relevant phrase> | <ProjectName>'`).
- (b) the `og:site_name` field.
- (c) the structured-data Organization name.

Missing any of the three = `FAIL`. The Generator's exact wording is `All meta titles, og:site_name, and structured-data Organization name use the invented project name`.

Generator anchor: Section 8 spec "All meta titles, og:site_name, and structured-data Organization name use the invented project name" + hard rule "All sections sync invented project name".

---

## M. Cross-section consistency

### M1. Invented project name consistent across Sections 1, 6, 8

The same exact string for the invented project name appears in:
- Section 1 product description.
- Section 6 fixture sample values (typically an Organization or Project entity's sample row).
- Section 8 meta tag templates and structured-data Organization name.

Section 4's page list often uses generic page names ("Dashboard", "Pipeline", "Articles") that may not contain the invented name - DO NOT require the invented name in Section 4. Section 4 is excluded from M1.

Editorial NOTE: the canonical rules state two adjacent requirements that can read as tension: (i) "All sections sync the invented project name" (a global sync rule), and (ii) "Routes are generic only; no source brand AND no invented name in any route segment" (a Section 4 exclusion). The QC resolves this by reading the route-segment exclusion as the more specific rule for Section 4 path fields, and reading the global sync rule as binding on Section 1 product description, Section 6 fixture sample values, and Section 8 meta / og / structured-data surfaces. Section 4 page TITLES (the `**Page Name**` bullet) MAY contain the invented name if natural, but are not required to; route segments MUST NOT.

`Soft warning` if the name varies in capitalization across the three required sections (Section 1 / 6 / 8).
`FAIL` if a substantively different project name is used in one of the three sections than in another.

Generator anchor: hard rule "All sections sync invented project name (Section 4 page list refs, Section 6 mock data sample values, Section 8 meta + structured data)" - reading carefully, Section 4 refs are conditional ("where relevant"), so M1 enforces only the three reliable anchors.

Overlap dedup: D2 (Section 1 invented-name presence) is a Soft warning. L7 (Section 8 meta sync) is a `FAIL` with AND-logic across meta titles + og:site_name + Organization. J6 (Section 6 sample values) does not flag when a sample row is missing. M1 is the canonical cross-section name-consistency check and the only one that auto-FAILs the PRD for a name mismatch. When a name issue surfaces in multiple rows, report once under the most specific row (D2 for Section 1 alone, L7 for Section 8 meta alone, M1 for any cross-section mismatch).

### M2. No source brand name anywhere

Re-application of H1, global scope. If H1 fires, M2 = `FAIL` with the same evidence and triggers the verdict-policy critical-violation count.
Generator anchor: hard rule "Never name real source product / brand".

### M3. No source URL anywhere

Re-application of H2, global scope. If H2 fires for a source-brand domain, M3 = `FAIL` with the same evidence and triggers the verdict-policy critical-violation count.
Generator anchor: hard rule "Never expose source URL".

### M4. Required UI surfaces in 4.2 align with Section 6 roles and flows (Soft warning by default)

Cross-check: every role mentioned in J3 should correspond to at least one page in 4.2 that the role can access; every flow mentioned in J7 should correspond to at least one page in 4.2 that hosts the trigger or terminal state.

Status thresholds:
- `Soft warning` if a flow does not map cleanly to a page (likely a phrasing mismatch, not a structural defect).
- `Soft warning` if a role does not explicitly name a screen it owns.
- `FAIL` ONLY if a role description explicitly names a screen by name that does NOT appear in the 4.2 page list (e.g. role bullet says "Editors use the 'Drafts' page" but no `Drafts` page block exists in 4.2). Bare absence of mapping (where neither side names a specific screen) is at most a `Soft warning`.

This check is intentionally heuristic - the Generator does not require a strict mapping table.

Generator anchor: implicit from Section 4 + Section 6 page/role/flow consistency; not a hard rule.

---

## N. Invalid input handling

If `<prd_to_qc>` is not a markdown PRD - for example it is a feature spec, a code file, a chat log, or empty - do NOT attempt to run the checks. Instead emit:

```
VERDICT: FAIL

SUMMARY:
- Input does not appear to be a Gohan-style PRD.
- Reason: <one-sentence reason - e.g. "first line is not '## 1. Product Overview'", "no H2 headings found", "appears to be source code, not prose">.

CHECK TABLE: (not run)

NOTES:
- Re-run with a PRD body starting at `## 1. Product Overview`.
```

If `<prd_to_qc>` appears to be a PRD generated by an EARLIER version of the canonical PRD ruleset (e.g. it OMITS a `prefers-reduced-motion` contract from Section 5, OMITS a reduced-motion cross-reference from Section 7, uses period-separated prose form for Main UI Flows instead of arrow-step paragraphs, or has a `Target Resolution:` line in Section 1), still run the QC - those will fail naturally - but include in NOTES the suspicion that the PRD was generated by an older ruleset.

Omittable sub-sections (4.4 Real-time, 6 Checkout, 6 Admin) are NOT "unrunnable" - they are within the Generator's carve-out. Mark the corresponding G8, J8, J9 rows `N/A` with the carve-out reason. Do not mark them `FAIL`.

If a check cannot be run for a different reason (e.g. the input is partially malformed, the section heading is missing, the page list cannot be parsed), mark that specific row `FAIL` with evidence `"could not parse - see NOTES"` and document the parsing failure in NOTES. NEVER silently skip a check.

---

## O. Style of QC output

Be terse. Quote only the offending span and a few words of context. Do NOT paste large PRD chunks. Do NOT lecture about why the rule exists. Do NOT propose extensive rewrites - the QC reports failures, the human (or a downstream agent) fixes them.

When you are uncertain, mark `FAIL` and explain the uncertainty in the row's evidence column. Never silently `PASS` an ambiguous case.

Cite the Generator anchor in the evidence column when a `FAIL` references a specific Generator rule. Use the format `(gen: <section or rule name>)` after the quoted span (e.g. `(gen: the No reduced-motion language hard rule)` or `(gen: Section 4.2 page-list spec)`). Do not cite hard rules by number -- the Generator's hard rules are an unnumbered bullet list; use the rule's descriptive name instead.

---

## ---END PROMPT---

---

## Operator notes

These notes are for the human operator who is running the QC, not for the QC agent. They sit outside the BEGIN/END PROMPT block.

### Running the QC

Typical invocation:

1. Paste the QC prompt (everything between BEGIN PROMPT and END PROMPT) as the system or user prompt to the reviewing model.
2. Paste the candidate PRD body as `<prd_to_qc>`.
3. Run. Read the `VERDICT` and `CHECK TABLE`. If `FAIL`, share findings with whoever produced the PRD and re-generate.

### Regex implementation tips

Most of the regex hints in this document are heuristics intended to bound the search. A literal-minded language-model reviewer can apply them by visual scanning; a script-based runner should treat them as starting points and tune for the PRD's actual content. The most reliable scans are:

- A1 / A3 (non-ASCII): trivial `[^\x00-\x7F]` pass.
- A2 (arrow separators): grep for the literal sequence `->` after stripping backticked spans, then visually filter for "is this an arrow step in prose or legitimate code". The QC no longer enforces bare ` > ` or `=>` under A2 (the canonical rules do not name them); J7's density rule still catches abusive use as flow separators.
- A5 (bare hex): use negative-lookaround `(?<!\x60)#[0-9A-Fa-f]{6}(?!\x60)` to catch unbacked hex codes.
- C1 / C2 (reduced-motion contract presence): case-insensitive grep for `prefers-reduced-motion` scoped to Section 5 (must be present, paired with a behavior clause), and grep for `Section 5` / `Motion Language` / `motion section` scoped to Section 7 (must be present, paired with reduced-motion language).
- G4 (4.2 indentation): line-by-line. Look at the leading whitespace before each `-` under each page bullet. Tabs and 4-space indents both fail; only `2 spaces + dash` passes.
- H4 (keyboard shortcuts): the disambiguation between `Ctrl` (shortcut) and `control` (English word) requires a `[+\- ]` anchor followed by a single uppercase letter or digit.

### Design notes on previous QC iterations

This appendix records why some checks look the way they do. It is informational only - the QC's rules are fully stated above and do not depend on these notes.

1. An earlier QC iteration said: replace `->` (arrow glyph) with `>` or `->`. The current QC forbids `->` as a replacement outside the two carve-outs (Sign-up and Main UI Flows); semantic prose -- period, `via`, `to`, comma -- is the right fix everywhere else. Bare ` > ` and `=>` are not enforced by A2 since the canonical rules do not name them.
2. An earlier iteration of the canonical rules specified the Sign-up flow as `->` arrow-step paragraph. A subsequent iteration inverted this to a period-only prose paragraph. The current ruleset restores the `->` arrow-step paragraph as the required form for Sign-up. The carve-out is scoped to the Sign-up / Sign-in UI Flow sub-section (see A2 + J5).
3. An earlier iteration specified Main UI Flows as period-separated prose paragraphs with NO arrows. The current ruleset inverts this: Main UI Flows must use ` -> ` arrow-step paragraphs (mirror of J5, minimum 3 arrows per flow). A2 carves out this sub-section from the global `->` ban (see A2 + J7).
4. An earlier iteration BANNED `prefers-reduced-motion` language anywhere in the PRD. The current ruleset inverts this entirely: Section 5 MUST declare a `prefers-reduced-motion` contract with a behavior clause. See C1.
5. An earlier iteration FORBADE a reduced-motion cross-reference between Section 7 and Section 5. The current ruleset REQUIRES that cross-reference. See C2 and K6.
6. An earlier iteration required Section 5 to declare only six motion surfaces (duration band, easing, route transitions, stagger, hover, scroll). The current ruleset adds a seventh required surface: the `prefers-reduced-motion` contract. See I4g.
7. An earlier iteration did not include a double-dash ` -- ` clause-separator ban. The current QC adds it as A4.
8. An earlier iteration did not require Section 4.2 page-list sub-fields to be indented by EXACTLY 2 spaces. The current QC adds it as G4.
9. An earlier design note said the QC intentionally overrode the canonical rules on the `->` glyph; this is obsolete - the canonical rules now define the carve-outs explicitly and the QC matches them.

Additional checks introduced by the current QC (not present in earlier iterations):
- A8: explicit em-dash, en-dash, smart quote re-flag (caught under A3 but separately surfaced for operator clarity).
- B4: scope tightened to headings and bolded-label bullet prefixes only - body prose may use `Backend` if it appears in unstructured prose (rare but allowed by the canonical heading-only ban).
- D5: success-metric / KPI line in Section 1 is a hard FAIL (the canonical rules explicitly forbid it).
- F4: split into F4a-F4e for Lighthouse, LCP, CLS, INP, bundle+frame-rate sub-checks.
- G4: sub-fields are UNBOLDED (`- Path:` not `- **Path:**`) per the canonical examples; bolded sub-fields are a Soft warning.
- H6: narrow carve-outs for non-legal uses of `privacy` (ARIA prose, accessibility considerations) and `terms` (search terms, terms of art); legal/footer/checkbox/meta uses remain absolute `FAIL`. Secondary regex `copyright\s+(\([cC]\)|©)?\s*\d{4}` catches full copyright lines.
- H10: ban on `[VERIFY -- not in canonical table]` markers.
- H11: category match before substitution (Soft warning).
- I2/G7: cubic-bezier is MANDATORY on every motion line; named easings alone are FAIL.
- I4: Section 5 must declare all seven motion surfaces (duration band, easing, route transitions, stagger, hover, scroll, `prefers-reduced-motion` contract).
- J5: arrow-step form REQUIRED per the canonical rules (single paragraph of `->` arrow-linked steps, minimum 3 arrows); simulated-delay clause requirement preserved (`(?i)simulated.*\d+\s*ms`). A2 carves out this sub-section from the global `->` ban.
- J7: arrow-step form REQUIRED per the canonical rules (3 flows, each a ` -> ` arrow-step paragraph, minimum 3 arrows each, 25-35 words each); triad enforcement (Create / dominant lifecycle / role-gated). A2 carves out this sub-section from the global `->` ban.
- K2: both contrast ratios (4.5:1 + 3:1 for AA, or 7:1 + 4.5:1 for AAA) required - not either/or.
- K6: cross-reference to Section 5 on reduced motion REQUIRED. Defers to C2.
- M1: Section 4 removed from must-appear list (page names often generic).

### Known false-positive risks

- A2 (arrow separators): may false-trip on `<-` if the regex is sloppy. Anchor on the two-char sequence `->` specifically. The bare ` > ` regex requires spaces on both sides; bare HTML-like `<tag>` will not match. Backticked code spans are stripped before A2 applies.
- H1 (source brand name): the heuristic list is not exhaustive. If a less-common brand is leaked (e.g. an obscure regional ERP), the QC may miss it. Rely on the reviewing model to recognize common source brands by feel.
- H3 (API paths): if the PRD describes API calls in prose without the HTTP-verb-prefix convention (e.g. "the user's data is fetched from the deals endpoint"), the regex will not catch it. Reviewer judgment applies; document in NOTES.
- H4 (keyboard shortcuts): `Cmd` matches the shortcut, but in rare cases a PRD might describe a verbatim UI element called "Command Bar" or "Command Palette". The current regex anchors on `+`/`-`/space-uppercase, so it will not false-positive on "Command Bar" alone - but the underlying intent (no keyboard-driven invocation patterns in PRD prose) may still be violated by a "Command Bar" UI element. Reviewer judgment applies.
- H5 (hype adjectives): heuristic. Sentences containing both a hype adjective AND a numeric reference are `PASS`; bare adjectives are `FAIL`. Some legitimate uses may be over-flagged. Reviewer judgment applies.
- J6 (View Models entity count): the count is scoped to TOP-LEVEL bullets (zero leading whitespace, `- **Name:**` form). Sub-bullets under `Relationships:` or nested-entity bullets are excluded. If the PRD uses a non-standard form, manually verify.
- L4 (structured data): the check is presence-based, not correctness-based. A reviewer should confirm that the structured-data type matches the page template (e.g. Product for product pages, Article for content).

### What this QC does NOT check

- Style polish, prose elegance, voice consistency beyond the banned-adjective list.
- Whether the invented project name is a "good" name. The canonical Phase 0 guidance ("1-2 syllables, pronounceable") is qualitative; the QC only checks that the name is consistent across sections, not that it is well-chosen.
- Whether the chosen aesthetic vector fits the category. The canonical Phase 0 guidance is qualitative.
- Implementation feasibility of the stack choices. The QC checks that no banned-stack item is named, not that the chosen items can actually be wired together.
- The factual correctness of the category's required UI surfaces. The QC asserts that the surfaces in the embedded Category-Specific Feature Emphasis catalog (see G6) are present, but does not verify that those surfaces are themselves a good description of the category.
- Whether the Main UI Flows triad (J7) correctly identifies the DOMINANT lifecycle action for the category. The QC checks the presence of a Create flow and a role-gated flow; the lifecycle identification is judged heuristically.

### Failure-mode tolerance

If the QC agent encounters a section that does not parse cleanly (e.g. a malformed bullet list, an H3 inside a fenced code block, a heading at an unexpected level), it should:

1. Note the parsing failure in NOTES.
2. Run every check it can on the surrounding context.
3. Mark unrunnable checks as `FAIL` with `evidence: "could not parse - see NOTES"`.
4. Never silently skip a check.

This makes the QC robust to malformed PRDs: a syntactically broken PRD will fail loudly rather than accidentally passing.

Omittable sub-sections (G8 4.4 Real-time, J8 Checkout, J9 Admin) are NOT "unrunnable" when omitted - they are within the canonical carve-out. Mark these `N/A` with the carve-out reason in evidence. Do not mark them `FAIL`.

### Citations: anchors as descriptive labels

Citations in this QC are written as descriptive anchors (e.g. `Section 5 required surfaces include the prefers-reduced-motion contract`, `Section 4.2 page-list spec`) rather than exact line numbers. These are stable labels naming the rule or section being enforced; the QC does not require any external file to be consulted, since every rule the QC enforces is reproduced in this document.

For reference, the canonical PRD structure these rules enforce is:

- Section 1 through Section 8 specs (with sub-section specs nested).
- Phase 0 internal inference (silent): project name, category, version, target resolution, aesthetic vector, color tokens, typography, view models, UI flows, real-time signals, payments.
- Category-Specific Feature Emphasis catalog (16 categories) - reproduced in G6 above.
- Word budget block (global 800-1500, plus per-section budgets enforced in B5 and per-section checks).
- Free and Open-Source Only substitution policy (paid -> free swaps, silent for fonts, annotated for other stack items).
- Output character rules (forbidden glyphs, `->` ban with Sign-up and Main UI Flows carve-outs, keyboard-shortcut ban).
- Required reduced-motion contract surfaces (Section 5 declares the `prefers-reduced-motion` contract; Section 7 cross-references Section 5).
- Hard rules (the absolute prohibitions enforced across the QC's check classes).

When a QC FAIL cites a hard rule or section spec, the anchor names the rule or section directly; the operator does not need to look anything up elsewhere.

---

(End of QC.)
