# Mock API Data Integration Report

**Source:** `/Users/apple/Downloads/final`
**Target:** `/Users/apple/Documents/ethara-etp/custom_addons/kensei/environment/`
**Date:** 2025-07-16
**Constraint:** Zero changes to any `server.py` or `*_data.py` file

---

## Executive Summary

73 CSV files and 34 JSON files from 7 APIs were analyzed against the existing mock API data layer code. Of these:

| Verdict | Count | Description |
|---------|-------|-------------|
| **SAFE — drop-in** | ~60 files | Schema matches, no code changes needed |
| **SAFE with cleanup** | ~18 files | Minor issues (extra columns, sparse nullable fields) that won't crash but should be trimmed |
| **CRASH — must fix data** | 6 files | Will cause `ValueError` / `TypeError` / `KeyError` at import time |
| **REJECT — wrong data** | 7 files | Misplaced directories, wrong API data in wrong service, or entirely unsupported files |

---

## Severity Legend

| Severity | Meaning |
|----------|---------|
| 🔴 **CRASH** | Server will fail to start or throw unhandled exceptions at runtime |
| 🟡 **DATA ISSUE** | Won't crash but produces incorrect behavior, wrong results, or reduced functionality |
| 🟢 **SAFE** | Drop-in compatible, no issues |
| ⚪ **REJECT** | Must not be integrated — wrong data, wrong directory, or unsupported files |

---

## API-by-API Integration Plan

---

### 1. Amazon Seller API

**Contributor:** `Anchal_Mittal`

| File | Rows | Verdict | Notes |
|------|------|---------|-------|
| `catalog_items.csv` | 6 | 🟢 SAFE | Schema exact match |
| `inventory.csv` | 6 | 🟢 SAFE | Schema exact match |
| `pricing.csv` | 6 | 🟢 SAFE | Schema exact match |

**Missing files (not provided by contributor):**
`orders.csv`, `order_items.csv`, `returns.csv`, `reports.csv`, `seller_account.json`

**Integration action:** Merge the 3 provided files into the existing data files. Since `orders.csv`, `order_items.csv`, `returns.csv`, `reports.csv`, and `seller_account.json` were not provided, the existing files for those must be preserved as-is. The `asin` values in `catalog_items.csv`, `inventory.csv`, and `pricing.csv` should cross-reference consistently (they do in this contributor's data).

**Risk: NONE**

---

### 2. Etsy API

**Contributors:** `Samya-Vasta_etsy`, `prakhar_etsy_api 2`, `Megha_quickbook_api` (misplaced)

#### Samya-Vasta_etsy — 🟢 SAFE

| File | Rows | Verdict | Notes |
|------|------|---------|-------|
| `listings.csv` | 6 | 🟢 SAFE | Schema exact match |
| `receipts.csv` | 6 | 🟢 SAFE | Schema exact match |
| `transactions.csv` | 6 | 🟢 SAFE | Schema exact match |
| `listing_images.csv` | 6 | 🟢 SAFE | Schema exact match |
| `reviews.csv` | 6 | 🟢 SAFE | Schema exact match |
| `shipping_profiles.csv` | 3 | 🟢 SAFE | Schema exact match |
| `return_policies.csv` | 3 | 🟢 SAFE | Schema exact match |
| `shop_sections.csv` | 4 | 🟢 SAFE | Schema exact match |
| `shop.json` | — | 🟢 SAFE | Schema exact match |

Empty cells exist in `gift_message`, `shipping_carrier`, `tracking_code`, `image_url`, `variations` — all nullable fields with existing coercion handling (`if r["field"] else None` or `or ""`).

#### prakhar_etsy_api 2 — 🟡 CLEANUP NEEDED

| File | Rows | Verdict | Notes |
|------|------|---------|-------|
| `listings.csv` | 10 | 🟡 CLEANUP | **3 extra columns:** `rating`, `reviews_count`, `image_quality_score` |
| `listing_images.csv` | 10 | 🟢 SAFE | Schema match |
| `return_policies.csv` | 2 | 🟢 SAFE | Schema match |
| `reviews.csv` | 10 | 🟢 SAFE | Schema match |
| `shipping_profiles.csv` | 3 | 🟢 SAFE | Schema match |
| `shop_sections.csv` | 3 | 🟢 SAFE | Schema match |

**Issue detail — `listings.csv` extra columns:**
The `etsy_data.py` coercion function builds listing dicts using explicit field access (`r["listing_id"]`, `r["title"]`, etc.) and does NOT use `**r` spread. The extra columns `rating`, `reviews_count`, `image_quality_score` will be **silently ignored** by the CSV DictReader — they won't crash, but they're dead weight.

**Action:** Remove the 3 extra columns from `listings.csv` before integration. No other changes needed.

**Missing files:** No `receipts.csv`, `transactions.csv`, `shop.json` provided.

#### Megha_quickbook_api — ⚪ REJECT (MISPLACED)

This directory is located at `etsy-api/Megha_quickbook_api/` but contains QuickBooks data:
- `accounts.csv`, `items.csv`, `vendors.csv` (CSV with QuickBooks schema)
- `bills.json` (QuickBooks bills format)

**Action:** Move to QuickBooks contributor pool, NOT Etsy. Do not integrate under Etsy.

---

### 3. Instagram API

**Contributors:** `Piyush_instagram_api`, `anuj_instagram_api`

#### Piyush_instagram_api — 🟢 SAFE

| File | Rows | Verdict | Notes |
|------|------|---------|-------|
| `hashtags.csv` | 10 | 🟢 SAFE | Schema exact match |
| `media.csv` | 10 | 🟢 SAFE | Schema exact match |
| `media_insights.csv` | 10 | 🟢 SAFE | Schema exact match |
| `stories.csv` | 6 | 🟢 SAFE | Schema exact match |

Empty cells in `thumbnail_url`, `link`, `poll_question`, `poll_options` — all handled by existing coercion (`or None`, `or ""`).

#### anuj_instagram_api — 🔴 MOSTLY REJECT

| File | Rows | Verdict | Notes |
|------|------|---------|-------|
| `conversations.csv` | — | ⚪ REJECT | **No server endpoint exists** |
| `dm_reactions.csv` | — | ⚪ REJECT | **No server endpoint exists** |
| `dm_read_receipts.csv` | — | ⚪ REJECT | **No server endpoint exists** |
| `messages.csv` | 24 | ⚪ REJECT | **No server endpoint exists**; also 50% empty `content`, 96% empty `file_type`/`file_size_kb` |
| `user.json` | — | 🟡 DATA ISSUE | Single object `{}` vs existing array `[{},...]` of 5 users |

**Issue detail — unsupported files:**
`instagram_data.py` loads exactly: `user.json`, `media.csv`, `stories.csv`, `hashtags.csv`, `media_insights.csv`. The 4 DM-related files (`conversations.csv`, `dm_reactions.csv`, `dm_read_receipts.csv`, `messages.csv`) have no loading logic, no endpoints in `server.py`, and no route definitions. They are completely unsupported.

**Issue detail — `user.json` type mismatch:**
- **Existing format:** `[{"user_id": "usr_001", ...}, {"user_id": "usr_002", ...}, ...]` (array of 5 users)
- **New format:** `{"user_id": "usr_001", ...}` (single object)
- **Code behavior:** `instagram_data.py` line 145: `_user_list = _user_raw if isinstance(_user_raw, list) else [_user_raw]`
- **Result:** Won't crash, but reduces the user pool from 5 to 1. The `GET /users` endpoint returns all users, `GET /users/{id}` finds by ID. With only 1 user, multi-user tasks become impossible.

**Action:**
- REJECT all 4 DM files — do not integrate.
- For `user.json`: If used, wrap in array `[{...}]` to match existing format. Better: append the new user to the existing array if their `user_id` is unique.

---

### 4. Linear API

**Contributor:** `Kshitz`

| File | Rows | Verdict | Notes |
|------|------|---------|-------|
| `issues.csv` | **0 data rows** | 🔴 CRASH | Header only — `max()` on empty sequence |
| `projects.csv` | **0 data rows** | 🔴 CRASH | Header only — no data to work with |
| `teams.csv` | 5 | 🟢 SAFE | Schema exact match |
| `labels.csv` | 10 | 🟢 SAFE | 5/10 empty `teamId` — handled by coercion |
| `cycles.csv` | 6 | 🟢 SAFE | 2/6 empty `completedAt` — handled by coercion |
| `comments.csv` | 6 | 🟢 SAFE | Schema exact match |
| `users.csv` | 5 | 🟢 SAFE | Schema exact match |
| `workflow_states.csv` | 8 | 🟢 SAFE | Schema exact match |
| `workspace.json` | — | 🟢 SAFE | Schema exact match |

**CRASH detail — `issues.csv` (0 rows):**
```python
# linear_data.py line 198
_next_issue_number = max(i["number"] for i in _issues_store) + 1
```
If `_issues_store` is empty, `max()` receives an empty generator → `ValueError: max() arg is an empty sequence`. The server will fail to start.

**CRASH detail — `projects.csv` (0 rows):**
While `projects.csv` doesn't have a direct `max()` call, an empty projects list means any `GET /projects/{id}` returns 404, and `POST /issues` with a `projectId` referencing a nonexistent project produces inconsistent state.

**Action:**
- `issues.csv`: Must contain at least 1 valid data row. Either populate with seed data or do not replace existing file.
- `projects.csv`: Must contain at least 1 valid data row. Same remedy.
- All other files: Safe to integrate directly.

---

### 5. Pinterest API

**Contributor:** `Nakul`

| File | Rows | Verdict | Notes |
|------|------|---------|-------|
| `boards.csv` | 5 | 🟢 SAFE | Schema exact match |
| `board_sections.csv` | 6 | 🟢 SAFE | Schema exact match |
| `pins.csv` | 10 | 🟢 SAFE | Nullable empties in `board_section_id`, `link`, `alt_text` — handled |
| `ad_accounts.csv` | 3 | 🟢 SAFE | Schema exact match |
| `campaigns.csv` | 4 | 🟢 SAFE | Schema exact match |
| `ad_groups.csv` | 4 | 🟢 SAFE | Schema exact match |
| `analytics.csv` | 10 | 🟢 SAFE | Schema exact match |
| `user_account.json` | — | 🟡 MINOR | Single object `{}` vs existing array `[{}]` — code handles both |
| `user.json` | — | ⚪ REJECT | **File does not exist** in current env; contains Instagram-like fields |

**Issue detail — `user_account.json`:**
- **Existing format:** `[{"username": "crafty_pinner", ...}]` (array with 1 element)
- **New format:** `{"username": "...", ...}` (bare object)
- **Code behavior:** `pinterest_data.py` line 134: `_user_account = _user_account_raw[0] if isinstance(_user_account_raw, list) else _user_account_raw`
- **Result:** Both formats produce the same result. No crash, no functional difference.

**Issue detail — `user.json`:**
This file doesn't exist in the current Pinterest environment. The `pinterest_data.py` only loads `user_account.json`. Furthermore, the content looks like Instagram user data (has `followers_count`, `follows_count`, `media_count` fields) — this is clearly from a wrong template.

**Action:**
- `user.json`: REJECT — do not integrate.
- `user_account.json`: Safe to use as-is (code handles both formats).
- All CSV files: Safe to integrate directly.

---

### 6. QuickBooks API

**Contributors:** `Aditya_Rana`, `Nakul_quickbook`, `Nistha_Diwedi`, `Piyush_quickbook_api`, `shamy-mock-quickbooks`, `shamy_quickbooks_mock-data`, `Megha_quickbook_api` (from Etsy dir), `anuj-quickbooks` (if present)

This is the most complex API with 8 contributors. Analysis is split by file type.

#### CSV Files

| Contributor | File | Rows | Verdict | Notes |
|-------------|------|------|---------|-------|
| Aditya_Rana | `accounts.csv` | 10 | 🟢 SAFE | Match |
| Aditya_Rana | `items.csv` | 10 | 🟢 SAFE | Match |
| Aditya_Rana | `customers.csv` | 10 | 🟢 SAFE | Match |
| Aditya_Rana | `vendors.csv` | 10 | 🟢 SAFE | Match |
| Nakul_quickbook | `accounts.csv` | 12 | 🟢 SAFE | Match |
| Nakul_quickbook | `items.csv` | 15 | 🟢 SAFE | Match |
| Nakul_quickbook | `customers.csv` | 25 | 🟡 CLEANUP | 13/25 empty `GivenName`/`FamilyName`, 12/25 empty `CompanyName` |
| Nakul_quickbook | `vendors.csv` | 15 | 🟢 SAFE | Match |
| Nistha_Diwedi | `accounts.csv` | 10 | 🟢 SAFE | Match |
| Nistha_Diwedi | `items.csv` | 8 | 🟢 SAFE | Match |
| Nistha_Diwedi | `customers.csv` | 8 | 🟢 SAFE | Match |
| Nistha_Diwedi | `vendors.csv` | **0 rows** | 🔴 CRASH | Empty file → `max()` crash |
| Piyush | `accounts.csv` | 10 | 🟢 SAFE | Match |
| Piyush | `items.csv` | 10 | 🟢 SAFE | Match |
| Piyush | `customers.csv` | 3 | 🟡 CLEANUP | 3/3 empty `GivenName`/`FamilyName` |
| Piyush | `vendors.csv` | 5 | 🟢 SAFE | Match |
| shamy-mock | `accounts.csv` | **0 rows** | 🔴 CRASH | Empty file — no `max()` for accounts but empty store breaks `GET /accounts` |
| shamy-mock | `items.csv` | 5 | 🟢 SAFE | Match |
| shamy-mock | `customers.csv` | 5 | 🟢 SAFE | Match |
| shamy-mock | `vendors.csv` | 5 | 🟢 SAFE | Match |
| shamy_mock-data | `accounts.csv` | 8 | 🟢 SAFE | Match |
| shamy_mock-data | `items.csv` | 6 | 🟢 SAFE | Match |
| shamy_mock-data | `customers.csv` | 8 | 🟢 SAFE | Match |
| shamy_mock-data | `vendors.csv` | 12 | 🟡 CLEANUP | 9/12 empty email/phone/address |
| Megha (from Etsy) | `accounts.csv` | 6 | 🟢 SAFE | Match |
| Megha (from Etsy) | `items.csv` | 5 | 🟢 SAFE | Match |
| Megha (from Etsy) | `vendors.csv` | 3 | 🟢 SAFE | Match |

**CRASH detail — `Nistha_Diwedi/vendors.csv` (0 rows):**
```python
# quickbooks_data.py line 148
_next_vendor_id = max(int(v["Id"]) for v in _vendors_store) + 1
```
Empty `_vendors_store` → `ValueError: max() arg is an empty sequence`.

**CRASH detail — `shamy-mock-quickbooks/accounts.csv` (0 rows):**
While accounts don't have a `max()` auto-increment, an empty accounts store means `GET /accounts` returns `[]` and the P&L report's expense categorization produces empty results. Functionally broken.

#### JSON Files

| Contributor | File | Verdict | Notes |
|-------------|------|---------|-------|
| Aditya_Rana | `bills.json` | 🟡 MINOR | Extra keys `MetaData`, `Status`, `SyncToken` — ignored by code |
| Aditya_Rana | `expenses.json` | 🟡 MINOR | Extra keys `AccountRef`, `PaymentType` — ignored |
| Aditya_Rana | `invoices.json` | 🟡 DATA ISSUE | Contains non-invoice keys: `CaseId`, `ConflictType`, `DuplicateGroupId`, `ExpenseType`, `HouseholdId`, `IntakeLimit`, etc. — see note below |
| Aditya_Rana | `company_info.json` | 🟡 MINOR | Extra keys, missing `IndustryType` — code just returns dict |
| Nakul_quickbook | `bills.json` | 🟡 MINOR | Extra keys — ignored |
| Nakul_quickbook | `expenses.json` | 🟡 MINOR | Extra keys — ignored |
| Nakul_quickbook | `invoices.json` | 🟢 SAFE | If schema matches |
| Nakul_quickbook | `company_info.json` | 🟡 MINOR | Extra keys — ignored |
| Nakul_quickbook | `user.json` | ⚪ REJECT | Contains Instagram user data — wrong API |
| Nistha_Diwedi | `bills.json` | 🟡 MINOR | Extra keys — ignored |
| Nistha_Diwedi | `expenses.json` | 🟡 MINOR | Extra keys — ignored |
| Nistha_Diwedi | `invoices.json` | 🟢 SAFE | If schema matches |
| Piyush | `bills.json` | 🟡 MINOR | Extra keys — ignored |
| Piyush | `expenses.json` | 🟡 MINOR | Extra keys — ignored |
| shamy variants | `bills.json` | 🟡 MINOR | Extra keys — ignored |
| shamy variants | `expenses.json` | 🟡 MINOR | Extra keys — ignored |

**DATA ISSUE — `Aditya_Rana/invoices.json` contamination:**
This file contains records with keys like `CaseId`, `ConflictType`, `DuplicateGroupId`, `ExpenseType`, `HouseholdId`, `IntakeLimit`, `IntakeRecordTotal`, `MileageExportTotal`, `OverIntakeLimit`, `ReceiptTotal`, `ReimbursementSummaryTotal`. These are not QuickBooks invoice fields — they appear to be from a different data source mixed in. The records still have `TotalAmt` which gets summed in the P&L report (line 803: `sum(inv.get("TotalAmt", 0) for inv in paid_invoices)`). If the `TotalAmt` values are nonsensical, P&L reports will produce wrong numbers.

**Action:** Manually inspect `Aditya_Rana/invoices.json` to verify `TotalAmt`, `TxnDate`, `Id`, `CustomerRef`, and `Line` fields are valid QuickBooks invoice data before integrating. Strip non-standard keys.

---

### 7. Ring API

**Contributor:** `Nistha`

| File | Rows | Verdict | Notes |
|------|------|---------|-------|
| `events.csv` | 42 | 🟢 SAFE | Schema match; all `snapshot_url` empty but stored as `""` — fine |
| `devices.csv` | 4 | 🟢 SAFE | Schema match |
| `notification_prefs.csv` | 1 | 🟢 SAFE | Empty `ding_alerts`/`package_alerts` handled by coercion |
| `groups.csv` | 3 | 🟢 SAFE | Schema match |
| `active_dings.json` | — | 🟢 SAFE | Fewer items (1 vs 2) but valid schema |
| `devices.json` | — | 🟡 DATA ISSUE | **Missing `chimes` and `doorbots` keys** — only `stickup_cams` |
| `location.json` | — | 🔴 CRASH | **Array `[{}]` instead of object `{}`** |

**CRASH detail — `location.json`:**
- **Existing format:** `{"location_id": "loc_001", "mode": "home", ...}` (plain object)
- **New format:** `[{"location_id": "loc_001", "mode": "home", ...}]` (array wrapping object)
- **Code behavior:** `ring_data.py` line 191+: `_location_store["location_id"]` — directly accesses keys on the loaded JSON
- **Result:** Accessing `["location_id"]` on a list → `TypeError: list indices must be integers or slices, not str`. Server crashes or returns 500 on every location-related endpoint.

**DATA ISSUE — `devices.json` missing keys:**
- **Existing format:** `{"doorbots": [...], "chimes": [...], "stickup_cams": [...]}`
- **New format:** `{"stickup_cams": [...]}`
- **Code behavior:** `ring_data.py` uses `_devices_store.get("doorbots", [])`, `.get("chimes", [])` etc.
- **Result:** Won't crash (`.get()` with default handles it), but `GET /devices` will return no doorbots or chimes — significantly reduced functionality for tasks that involve doorbell or chime operations.

**Action:**
- `location.json`: MUST unwrap from array to plain object: `[{...}]` → `{...}`
- `devices.json`: Add empty `"doorbots": []` and `"chimes": []` keys at minimum. Better: populate with at least 1 device each for functional tasks.

---

## Summary of All Blocking Issues (Must Fix Before Integration)

### 🔴 CRASH — Server Won't Start

| # | API | File | Problem | Fix |
|---|-----|------|---------|-----|
| 1 | Linear | `issues.csv` | 0 data rows → `max()` crash | Add at least 1 issue row |
| 2 | Linear | `projects.csv` | 0 data rows → broken references | Add at least 1 project row |
| 3 | QuickBooks | `Nistha/vendors.csv` | 0 data rows → `max()` crash | Add at least 1 vendor row |
| 4 | QuickBooks | `shamy-mock/accounts.csv` | 0 data rows → empty store | Add at least 1 account row |
| 5 | Ring | `location.json` | Array `[{}]` instead of object `{}` | Unwrap: `[{...}]` → `{...}` |

### ⚪ REJECT — Do Not Integrate

| # | API | File/Dir | Reason |
|---|-----|----------|--------|
| 1 | Etsy | `Megha_quickbook_api/` dir | QuickBooks data in Etsy directory |
| 2 | Instagram | `conversations.csv` | No endpoint exists |
| 3 | Instagram | `dm_reactions.csv` | No endpoint exists |
| 4 | Instagram | `dm_read_receipts.csv` | No endpoint exists |
| 5 | Instagram | `messages.csv` | No endpoint exists |
| 6 | Pinterest | `user.json` | File doesn't exist in env; contains Instagram-like data |
| 7 | QuickBooks | `Nakul/user.json` | Instagram user data in QuickBooks dir |

### 🟡 DATA ISSUES — Should Fix for Quality

| # | API | File | Problem | Fix |
|---|-----|------|---------|-----|
| 1 | Etsy | `prakhar/listings.csv` | 3 extra columns | Remove `rating`, `reviews_count`, `image_quality_score` columns |
| 2 | Instagram | `anuj/user.json` | Single object vs array | Wrap in array `[{...}]` |
| 3 | Ring | `devices.json` | Missing `chimes`, `doorbots` | Add keys with empty arrays or seed data |
| 4 | QuickBooks | `Aditya/invoices.json` | Non-invoice keys contaminating data | Verify `TotalAmt` values; strip foreign keys |
| 5 | QuickBooks | `Nakul/customers.csv` | 52% empty name fields | Fill or trim rows with no name data |
| 6 | QuickBooks | `Piyush/customers.csv` | 100% empty name fields | Fill names — these are required for meaningful display |
| 7 | QuickBooks | `shamy_mock-data/vendors.csv` | 75% empty contact info | Acceptable but low quality |

---

## Integration Procedure (Recommended Order)

### Phase 1: Pre-processing (Fix Blocking Issues)

```
1. Fix Linear issues.csv        — add ≥1 seed issue row
2. Fix Linear projects.csv      — add ≥1 seed project row
3. Fix QB Nistha vendors.csv    — add ≥1 seed vendor row
4. Fix QB shamy-mock accounts.csv — add ≥1 seed account row
5. Fix Ring location.json       — unwrap array to object
6. Move etsy-api/Megha_quickbook_api/ → quickbooks-api/ pool
7. Delete instagram-api/anuj/ DM files (conversations, dm_reactions, dm_read_receipts, messages)
8. Delete pinterest-api/Nakul/user.json
9. Delete quickbooks-api/Nakul/user.json
```

### Phase 2: Data Cleanup

```
1. Etsy prakhar listings.csv     — remove 3 extra columns
2. Instagram anuj user.json      — wrap in array [{}]
3. Ring devices.json             — add "chimes": [], "doorbots": [] keys
4. QB Aditya invoices.json       — audit TotalAmt values, strip foreign keys
5. QB Nakul/Piyush customers.csv — fill empty name fields
```

### Phase 3: Integration (Per Contributor → Per API)

For each contributor dataset that passes Phase 1+2:

1. **Validate** — run a lightweight Python script that:
   - Loads each CSV with `csv.DictReader` and verifies required columns exist
   - Loads each JSON with `json.load()` and verifies top-level structure
   - Checks no CSV has 0 data rows
   - Verifies all `int()` / `float()` coercion fields contain valid numbers (not empty strings)
2. **Replace** the corresponding files in `environment/<api-name>/`
3. **Smoke test** — start each mock API independently:
   ```bash
   cd environment/<api-name>
   python server.py &
   curl http://localhost:<port>/health
   # Verify key endpoints return 200 with data
   kill %1
   ```

### Phase 4: Verification

For each integrated API:
1. Hit the `/health` endpoint
2. Hit every `GET` list endpoint — verify non-empty responses
3. Hit one `POST` create endpoint — verify auto-increment IDs work
4. Hit the `/audit/requests` endpoint — verify tracking middleware still captures requests

---

## Files With No Data Provided (Unchanged)

These existing files have no replacement data and MUST be preserved as-is:

| API | Files |
|-----|-------|
| Amazon | `orders.csv`, `order_items.csv`, `returns.csv`, `reports.csv`, `seller_account.json` |
| Google Classroom | **All files** (empty contributor directory) |
| MyFitnessPal | **All files** (empty contributor directory) |
| YouTube | **All files** (empty contributor directory) |
| Etsy | `receipts.csv`, `transactions.csv` (not in prakhar set) |

---

## Empty String vs None Coercion Reference

For reference, here's how each `*_data.py` handles empty CSV cells for critical field types:

| Type | Empty cell behavior | Example |
|------|-------------------|---------|
| `int(r["field"])` | ❌ `ValueError` — crashes on `""` | IDs, counts, quantities |
| `float(r["field"])` | ❌ `ValueError` — crashes on `""` | Prices, amounts |
| `r["field"].lower() == "true"` | ✅ Returns `False` for `""` | Boolean fields |
| `r["field"] or None` | ✅ Returns `None` | Optional string fields |
| `r["field"] or ""` | ✅ Returns `""` | Optional string fields |
| `int(r["field"]) if r["field"] else None` | ✅ Guarded | Optional numeric fields |
| `r["field"].split("\|")` | ✅ Returns `[""]` — benign | Pipe-delimited lists |

**Key rule for contributors:** Any column that passes through bare `int()` or `float()` **must not have empty cells**. Check the `_coerce_*` function in each `*_data.py` to identify which fields these are.
