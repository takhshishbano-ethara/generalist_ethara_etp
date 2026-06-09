# Task: Build `etp_assessment_extension` (Odoo 19 + Flutter Web)

## Inputs
- **Project name**: `{{PROJECT_NAME}}`
- **Reference Odoo module**: `{{REFERENCE_MODULE}}` — lives under `/Users/apple/Desktop/egon/ethara-etp/custom_addons/{{REFERENCE_MODULE}}`
- **Frontend project root**: `/Users/apple/Desktop/egon/etp-flutter`
- **Frontend reference UI**: the existing project details page at `lib/features/projects/presentation/screens/project_detail_screen.dart` and its widgets under `lib/features/projects/presentation/widgets/`
- **Project requirements**: {{REQUIREMENTS_OR_SPEC}} (user stories, screenshots, written spec, or "infer from reference module")

## What "extension" means here
Every `*_extension` module exposes a clean REST API on top of an existing Odoo base module and surfaces that data inside the Flutter app. The **infrastructure** is identical across projects; the **endpoints, entities, widgets, and tabs are bespoke** to each project's actual data and requirements. Do NOT copy any other extension's specific endpoints, widgets, or screens. Derive them from `{{REFERENCE_MODULE}}`'s domain + `{{REQUIREMENTS_OR_SPEC}}`.

### Reusable infrastructure (identical for every project)
- Odoo module shape: `__manifest__.py`, `__init__.py`, `controllers/`, optional `models/`, `security/`, `data/`.
- Controller convention: `type='http'`, `auth='none'`, `csrf=False`, `cors='*'`, `@validate_token` from `api_auth_gateway`, response envelope `{message, status, data, errors}` via `return_Response`.
- Role gating helper from `api_auth_gateway` / `kensei2.analytics_dashboard.py:_user_role_tag` style (full / project_lead / qc_reviewer / tasker / 403).
- Optional in-memory cache for expensive aggregates (TTL configurable per route, not mandatory).
- Flutter clean architecture: `data → domain ← presentation` per feature, dartz `Either<APIError, T>`, retrofit + json_serializable + injectable + flutter_bloc, design tokens (`SpacingTokens`, `TypographyTokens`, `RadiusTokens`, `DurationTokens`, `colorScheme.*`, `context.semanticColors.*`, `context.statusColors.*`), `CommonButton` for every button.

### Bespoke per project (drive from requirements)
- Endpoint set, shapes, query params, response payloads.
- Entities, BLoCs, screens, and widgets.
- Whether the integration is a project-details tab, a top-level section, a per-row drawer, or all three.

## Mandatory Workflow

### Phase 0 — Discovery (PARALLEL background agents, then wait)
Fire these explore agents in parallel and do nothing else until all return:
1. `explore` → Map `/Users/apple/Desktop/egon/ethara-etp/custom_addons/{{REFERENCE_MODULE}}` completely. Return: every model with fields/types/computed flags/relations, every existing controller route, security rules, any reports/crons, what domain the module tracks (tasks? annotations? evaluations? something else?). This grounds the API design — different reference modules → different endpoints.
2. `explore` → Map `lib/features/projects/presentation/screens/project_detail_screen.dart` and every file under `lib/features/projects/presentation/widgets/`. Return a registry: `{section_name: {file, widget_type, bloc?, entity_fields_consumed, tab_index_or_anchor}}`. Identify the tab/section/drawer registration pattern the file uses today.
3. `explore` → Map `lib/features/projects/data/`, `domain/`, `presentation/bloc/`, and `lib/core/constants/api_constants.dart`. Return: existing project-related endpoints, repository methods, BLoCs, and which screens already consume them so the new work doesn't duplicate.

### Phase 1 — Requirement analysis & gap table
Synthesize Phase 0 results plus `{{REQUIREMENTS_OR_SPEC}}`. Produce TWO outputs and wait for explicit user confirmation before any code:

**A. Endpoint plan** — derived from the requirements + reference module, NOT from a template:

| Endpoint | HTTP | Query params | Response shape | Why (which requirement / widget it feeds) | Role gating | Cache? |
|---|---|---|---|---|---|---|

**B. UI gap table** — for each requirement, map to existing project-detail widgets:

| Requirement | Existing widget under `lib/features/projects/presentation/widgets/`? | Status | Action |
|---|---|---|---|

Status legend:
- :white_check_mark: Existing widget fits as-is → reuse, just wire to new BLoC + endpoint.
- :warning: Existing widget covers part of the requirement → extend with new fields/variant; keep the same file unless growth forces a split.
- :x: No matching widget → build a new one as a peer file under `lib/features/projects/presentation/widgets/`, matching the closest neighbor's structure, naming, and style.
- :no_entry_sign: Requirement does not belong on the project details page → propose an alternative integration point (sidebar section, drawer, dialog) and justify.

If a requirement has no clear data source in `{{REFERENCE_MODULE}}`, surface the gap to the user instead of inventing one.

### Phase 2 — Backend extension module
After user approves the endpoint plan:
1. Create `/Users/apple/Desktop/egon/ethara-etp/custom_addons/{{PROJECT_NAME}}_extension/` with `__manifest__.py`, `__init__.py`, `controllers/__init__.py`, plus one controller file per logical group (group by resource, not by endpoint count).
2. `__manifest__.py`: `version '19.0.1.0.0'`, `license 'LGPL-3'`, `application=False`, `installable=True`. Depends on `base`, `web`, `{{REFERENCE_MODULE}}`, `api_auth_gateway`, plus any other Odoo modules required for the project's data (e.g. `task_forge_bridge`, `project_extension`). Pick dependencies from what the reference module actually needs.
3. Every route follows the infrastructure conventions above (envelope, auth, CORS, role gating). Endpoint paths use `/api/v1/{{PROJECT_NAME}}_ext/<resource>` namespace.
4. Add caching ONLY where Phase 1 flagged it (expensive aggregates). Skip cache for cheap lookups.
5. Write a `README.md` in the module documenting every route: path, method, query params, response schema, role gating, cache TTL (if any).

### Phase 3 — Flutter integration
1. **Constants** — append `// {{PROJECT_NAME}} Extension` block to `lib/core/constants/api_constants.dart` with the approved paths.
2. **Domain layer** under `lib/features/{{PROJECT_NAME}}/domain/`:
   - Entities — one per response payload, all `final`, `const` constructor, no Flutter imports.
   - Repository abstract — failable methods return `Future<Either<APIError, T>>`.
   - Use cases — `@lazySingleton`, propagate Either, no try/catch.
3. **Data layer** under `lib/features/{{PROJECT_NAME}}/data/`:
   - Response models with envelope wrapper + typed inner data. `@JsonSerializable`, `@JsonKey(name: 'snake_case')`, `toEntity()`.
   - Remote DS: abstract `@RestApi()` + factory + `part 'x.g.dart'`. Paths from ApiConstants.
   - Repository impl `@LazySingleton(as: SomeRepo)` with the standard `try { unwrap data, Right(toEntity) } on DioException { Left(APIError(ErrorHandler.handle(e).message, code)) } on Exception { Left(APIError(e.toString())) }` pattern.
4. **DI** — register the remote DS in `lib/core/di/register_module.dart` with `@lazySingleton`. Run `dart run build_runner build --delete-conflicting-outputs`.
5. **BLoCs** under `lib/features/{{PROJECT_NAME}}/presentation/bloc/` — one per logical UI section (not one per endpoint, unless they happen to match). `@injectable` (factory), sealed events/states with `part` files, states Initial/Loading/Loaded(entity)/Failure(message). Always `result.fold((err)=>emit(Failure(err.message)), (data)=>emit(Loaded(data)))`.
6. **L10n** — add user-facing strings to `lib/l10n/app_en.arb` with prefix `{{PROJECT_NAME}}*`. Run `flutter gen-l10n`. Dummy placeholder data is exempt.
7. **Widgets** — for every :warning:/:x: in the gap table:
   - File goes under `lib/features/projects/presentation/widgets/` (peer of existing widgets) so it's discoverable next to siblings.
   - Match the closest neighbor's naming, file structure, and StatelessWidget/StatefulWidget choice.
   - Provide BLoC via `BlocProvider(create: (_) => getIt<XBloc>()..add(initialEvent))`. NEVER `.value`.
   - All conventions in Phase 4 apply.
8. **Tab/section wiring** — use whatever pattern Phase 0 found in `project_detail_screen.dart` today (TabBar + TabBarView, IndexedStack + segmented control, anchored sections, drawer panels — whichever exists). Append the new section the same way; do not invent a new pattern.

### Phase 4 — Convention enforcement (NON-NEGOTIABLE)
- Flutter Web ONLY — no `dart:io`, no `SocketException`, no `flutter_secure_storage`.
- Spacing: only `SpacingTokens.space*`. Radius: only `RadiusTokens.radius*` via `BorderRadius.circular(...)`. Durations: only `DurationTokens.*`.
- Typography: only `TypographyTokens.*` styles + token weight constants. Use `Text.rich`, NOT `RichText`. Never `GoogleFonts.*` directly.
- Colors: `colorScheme.*` / `context.semanticColors.*` / `context.statusColors.*` / `context.sidebarTheme.*`. Never `Color(0xFF...)`.
- Buttons: only `CommonButton` from `lib/core/widgets/common_button.dart` (variants: default, `CommonButtonVariant.outlined`, `destructive`). Never raw `ElevatedButton`/`OutlinedButton`.
- API rule: never send `null` in request bodies — send `''`. For GET query params, convert empty strings to null inside the repository helper so they're omitted from the URL.
- Type safety: never `as any`, never `@ts-ignore`-equivalent suppressions. Fix the real type issue.

### Phase 5 — Verification gate
Task is NOT complete until all pass:
1. `dart run build_runner build --delete-conflicting-outputs` from `/Users/apple/Desktop/egon/etp-flutter` succeeds with no errors.
2. `flutter gen-l10n` succeeds.
3. `flutter analyze` shows zero NEW issues in files you created or modified. Pre-existing issues elsewhere are out of scope — list them, don't fix unless asked.
4. `flutter run -d chrome`, navigate to a project, and confirm the new section(s) render and load data (or show the empty/error state correctly). State this explicitly — never claim done without confirming it renders.

### Phase 6 — Handoff
Report:
- Tree of files created (backend + frontend).
- Final endpoint plan + UI gap table with actions taken per row.
- For each endpoint: path, method, query params, sample response.
- For each new/modified widget: file path, what tab/section it lives in, which BLoC + endpoint feeds it.
- `flutter analyze` output.
- Any deferred items (requirements with no data source, widgets that need backend changes you couldn't make).

## Style & tone
- Verbalize intent at the start (Phase 0 routing decision).
- `todowrite` immediately with the Phases as items. Mark one `in_progress` at a time, complete as soon as done.
- Fire `explore`/`librarian` agents in parallel; never re-search the same thing yourself after delegating.
- Consult Oracle only on 2+ failed fix attempts or genuine architecture tradeoffs.
- No emojis, no flattery, no "I'm on it" preambles. Work, then report.