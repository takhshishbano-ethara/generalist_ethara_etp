"""Bulk-import fenrir.task records from an Excel (.xlsx) or CSV spreadsheet.

Each data row becomes one task. Columns are matched by header name
(case/space/punctuation-insensitive), so the column order is irrelevant.

Recognised columns
    Task ID          -> code            (optional; when blank it is generated
                                         as <CATEGORY_CODE>-<PHASE_NO><SERIAL>;
                                         must be unique)
    Category         -> category_id     (matched by name; optionally created,
                                         with an auto-generated short code)
    Phase            -> phase_id        (matched by name; blank/unknown falls
                                         back to the default phase)
    Title            -> title
    Overview         -> overview
    Scope of Work    -> scope_of_work
    Company Details  -> company_details
    PRD              -> prd_link         (link, stored for reference)
    Assets           -> assets_link      (link, stored for reference)
    Instruction.md   -> instruction_md_link
    Rubrics          -> rubrics_link
    Price Tier       -> price_tier        (one of $0-$50, $50-$100,
                                            $100-$150, $150-$200)
    Buyer            -> buyer_id          (existing user; matched by login,
                                            email, then name)
    Name             -> lead_user_id      (the tasker; existing user, matched
                                            like Buyer; blank leaves it unset)
    Reviewer         -> reviewer_id       (existing user; matched like Buyer;
                                            blank leaves it unset)
"""

import base64
import csv
import io
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


def _norm_header(value):
    """Normalise a header cell to a comparison key: lowercase, alnum only."""
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def _norm_tier(value):
    """Normalise a price-tier cell to digits+hyphen for matching against the
    ``price_tier`` selection, e.g. '$50 - $100' / '50 to 100' -> '50-100'."""
    s = re.sub(r"[^0-9-]", "", (value or "").strip())
    return re.sub(r"-{2,}", "-", s).strip("-")


# Normalised-header -> task field. Several spellings map to the same field so
# the importer tolerates "Task ID"/"Code", "Scope of Work"/"Scope", etc.
HEADER_MAP = {
    "taskid": "code",
    "code": "code",
    "taskcode": "code",
    "category": "category",
    "title": "title",
    "overview": "overview",
    "scopeofwork": "scope_of_work",
    "scope": "scope_of_work",
    "companydetails": "company_details",
    "company": "company_details",
    "prd": "prd_link",
    "prdlink": "prd_link",
    "assets": "assets_link",
    "assetslink": "assets_link",
    "instructionmd": "instruction_md_link",
    "instructionmdlink": "instruction_md_link",
    "rubrics": "rubrics_link",
    "rubric": "rubrics_link",
    "rubricslink": "rubrics_link",
    "pricetier": "price_tier",
    "tier": "price_tier",
    "price": "price_tier",
    "buyer": "buyer_id",
    "buyerid": "buyer_id",
    "buyername": "buyer_id",
    "buyeremail": "buyer_id",
    "buyerlogin": "buyer_id",
    "name": "lead_user_id",
    "tasker": "lead_user_id",
    "taskername": "lead_user_id",
    "leaduser": "lead_user_id",
    "lead": "lead_user_id",
    "assignee": "lead_user_id",
    "reviewer": "reviewer_id",
    "reviewerid": "reviewer_id",
    "reviewername": "reviewer_id",
    "revieweremail": "reviewer_id",
    "reviewerlogin": "reviewer_id",
    "phase": "phase_id",
    "phaseno": "phase_id",
    "phasenumber": "phase_id",
    "deliveryphase": "phase_id",
}

# Plain text fields copied straight from their column to the task.
_TEXT_FIELDS = (
    "code", "title", "overview", "scope_of_work", "company_details",
    "prd_link", "assets_link", "instruction_md_link", "rubrics_link",
)


def _cell_str(value):
    """Coerce a spreadsheet cell (str/int/float/None) to a trimmed string."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        # openpyxl reads "1234567" as 1234567.0 — drop the spurious ".0".
        value = int(value)
    return str(value).strip()


class FenrirTaskImportWizard(models.TransientModel):
    _name = "fenrir.task.import.wizard"
    _description = "Fenrir — Import Tasks from Spreadsheet"

    data_file = fields.Binary(string="Spreadsheet", required=True)
    data_filename = fields.Char(string="Filename")
    has_header = fields.Boolean(
        string="First row is header",
        default=True,
        help="The first row holds the column titles (Task ID, Category, ...).")
    create_missing_categories = fields.Boolean(
        string="Create missing categories",
        default=True,
        help="When a Category name is not found, create it. If unchecked, "
             "the task is created with no category instead.")

    # ------------------------------------------------------------------ parse
    def _parse_rows(self):
        """Return a list of row-lists (each a list of cell strings)."""
        raw = base64.b64decode(self.data_file)
        name = (self.data_filename or "").lower()
        is_xlsx = name.endswith(".xlsx") or raw[:2] == b"PK"

        if is_xlsx:
            try:
                import openpyxl
            except ImportError as exc:
                raise UserError(_(
                    "Reading .xlsx files needs the 'openpyxl' Python library, "
                    "which is not installed. Either install it, or re-save the "
                    "spreadsheet as CSV and upload that.")) from exc
            try:
                wb = openpyxl.load_workbook(
                    io.BytesIO(raw), read_only=True, data_only=True)
            except Exception as exc:  # noqa: BLE001
                raise UserError(_(
                    "Could not read the spreadsheet: %s") % exc) from exc
            ws = wb.active
            rows = [[_cell_str(c) for c in row]
                    for row in ws.iter_rows(values_only=True)]
            wb.close()
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("latin-1")
                except UnicodeDecodeError as exc:
                    raise UserError(_(
                        "Unable to decode the CSV file. Re-export it as "
                        "'CSV UTF-8' and retry.")) from exc
            try:
                dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = [[_cell_str(c) for c in row]
                    for row in csv.reader(io.StringIO(text), dialect=dialect)]

        # Drop fully-empty rows.
        return [r for r in rows if any(c for c in r)]

    # ----------------------------------------------------------------- import
    def action_import(self):
        self.ensure_one()
        if not self.data_file:
            raise UserError(_("Please upload a spreadsheet."))

        rows = self._parse_rows()
        if not rows:
            raise UserError(_("The spreadsheet is empty."))

        if not self.has_header:
            raise UserError(_(
                "A header row is required so columns can be matched by name. "
                "Tick 'First row is header'."))

        header = rows[0]
        data_rows = rows[1:]
        if not data_rows:
            raise UserError(_(
                "The spreadsheet has a header row but no data rows."))

        # Map each column index to a task field via its header.
        col_to_field = {}
        for idx, cell in enumerate(header):
            field = HEADER_MAP.get(_norm_header(cell))
            if field:
                col_to_field[idx] = field

        if not col_to_field:
            raise UserError(_(
                "No recognised columns found. Detected headers: %s"
            ) % ", ".join(h for h in header if h))

        Task = self.env["fenrir.task"]
        Category = self.env["fenrir.category"]
        existing_codes = set(
            Task.with_context(active_test=False).search([]).mapped("code"))
        category_cache = {}  # normalised name -> category id

        # Normalised value -> canonical price_tier key, derived from the model
        # so it stays in sync with the field's selection.
        tier_selection = Task._fields["price_tier"].selection
        if callable(tier_selection):
            tier_selection = tier_selection(Task)
        tier_lookup = {_norm_tier(key): key for key, _label in tier_selection}

        Users = self.env["res.users"]
        user_cache = {}  # normalised token -> user id (or False if unmatched)

        def resolve_user(token):
            """Resolve a user cell (Buyer / Name) to a res.users id by login,
            then email, then name. Never creates users; returns False when
            unmatched."""
            token = (token or "").strip()
            if not token:
                return False
            key = token.lower()
            if key in user_cache:
                return user_cache[key]
            user = (Users.search([("login", "=ilike", token)], limit=1)
                    or Users.search([("email", "=ilike", token)], limit=1)
                    or Users.search([("name", "=ilike", token)], limit=1))
            user_cache[key] = user.id if user else False
            return user_cache[key]

        def resolve_category(name):
            key = name.strip().lower()
            if not key:
                return False
            if key in category_cache:
                return category_cache[key]
            cat = Category.search([("name", "=ilike", name.strip())], limit=1)
            if not cat and self.create_missing_categories:
                cat = Category.create({"name": name.strip()})
            category_cache[key] = cat.id if cat else False
            return category_cache[key]

        Phase = self.env["fenrir.phase"]
        default_phase = (Phase.search([("is_default", "=", True)], limit=1)
                         or Phase.search([], limit=1))
        phase_cache = {}  # normalised name -> (phase record, found?)

        def resolve_phase(token):
            """Resolve a Phase cell to a fenrir.phase record. Blank or unknown
            falls back to the default phase; never creates phases. Returns
            (phase, found) so the caller can flag unrecognised names."""
            token = (token or "").strip()
            if not token:
                return default_phase, True
            key = token.lower()
            if key not in phase_cache:
                ph = Phase.search([("name", "=ilike", token)], limit=1)
                phase_cache[key] = (ph or default_phase, bool(ph))
            return phase_cache[key]

        serial_counters = {}  # "<CAT>-<PHASE>" prefix -> last serial used

        def make_task_code(category_rec, phase_rec):
            """Next unique <CAT>-<PHASE_NO><SERIAL> code, tracking serials per
            category+phase across this batch and existing tasks. Delegates the
            prefix (and category-code generation) to the task model."""
            prefix = Task._task_code_prefix(category_rec, phase_rec)
            if prefix not in serial_counters:
                best = 0
                for existing in existing_codes:
                    if existing.startswith(prefix):
                        tail = existing[len(prefix):]
                        if tail.isdigit():
                            best = max(best, int(tail))
                serial_counters[prefix] = best
            serial = serial_counters[prefix] + 1
            code = f"{prefix}{serial:02d}"
            while code in existing_codes or code in seen_codes:
                serial += 1
                code = f"{prefix}{serial:02d}"
            serial_counters[prefix] = serial
            return code

        to_create = []
        seen_codes = set()
        skipped_dup = []
        skipped_blank = 0
        generated = 0
        unknown_phase = 0
        invalid_tier = 0
        unknown_buyer = 0
        unknown_tasker = 0
        unknown_reviewer = 0

        for row in data_rows:
            row_vals = {}
            category_name = ""
            raw_tier = ""
            raw_buyer = ""
            raw_tasker = ""
            raw_reviewer = ""
            raw_phase = ""
            for idx, field in col_to_field.items():
                value = row[idx] if idx < len(row) else ""
                if field == "category":
                    category_name = value
                elif field == "phase_id":
                    raw_phase = value
                elif field == "price_tier":
                    raw_tier = value
                elif field == "buyer_id":
                    raw_buyer = value
                elif field == "lead_user_id":
                    raw_tasker = value
                elif field == "reviewer_id":
                    raw_reviewer = value
                elif field in _TEXT_FIELDS:
                    row_vals[field] = value

            code = (row_vals.get("code") or "").strip()

            # Resolve category (may create it with an auto-generated code) and
            # phase up front — both feed auto-generated task codes.
            cat_id = resolve_category(category_name)
            category_rec = Category.browse(cat_id) if cat_id else Category
            phase_rec, phase_found = resolve_phase(raw_phase)
            if raw_phase.strip() and not phase_found:
                unknown_phase += 1
                _logger.warning(
                    "Fenrir import: phase %r not found; using default phase "
                    "%r.", raw_phase, default_phase.name or "(none)")

            # Task ID is optional — generate <CAT>-<PHASE><SERIAL> when blank.
            if not code:
                if not any(v for v in row_vals.values()) and not category_name \
                        and not (raw_tier or raw_buyer or raw_tasker or raw_reviewer
                                 or raw_phase):
                    skipped_blank += 1  # genuinely empty row
                    continue
                code = make_task_code(category_rec, phase_rec)
                generated += 1

            if code in existing_codes or code in seen_codes:
                skipped_dup.append(code)
                continue
            seen_codes.add(code)

            row_vals["code"] = code
            if cat_id:
                row_vals["category_id"] = cat_id
            if phase_rec:
                row_vals["phase_id"] = phase_rec.id

            # Price Tier is a selection — normalise and only set on a match;
            # a blank cell keeps the model default, an unrecognised one is
            # reported and left at the default too.
            raw_tier = (raw_tier or "").strip()
            if raw_tier:
                canonical = tier_lookup.get(_norm_tier(raw_tier))
                if canonical:
                    row_vals["price_tier"] = canonical
                else:
                    invalid_tier += 1
                    _logger.warning(
                        "Fenrir import: task %s has unrecognised Price Tier "
                        "%r; leaving default. Valid values: %s",
                        code, raw_tier, ", ".join(tier_lookup.values()))

            # Buyer is an existing res.users — resolve and assign; a blank cell
            # leaves it unset, an unknown one is reported and left unset too.
            raw_buyer = (raw_buyer or "").strip()
            buyer_id = resolve_user(raw_buyer) if raw_buyer else False
            row_vals["buyer_id"] = buyer_id
            if raw_buyer and not buyer_id:
                unknown_buyer += 1
                _logger.warning(
                    "Fenrir import: task %s references unknown Buyer %r "
                    "(no user by login/email/name); leaving unset.",
                    code, raw_buyer)

            # Name (the tasker) is an existing res.users — resolve and assign;
            # a blank cell leaves it unset, an unknown one is reported and left
            # unset too.
            raw_tasker = (raw_tasker or "").strip()
            tasker_id = resolve_user(raw_tasker) if raw_tasker else False
            row_vals["lead_user_id"] = tasker_id
            if raw_tasker and not tasker_id:
                unknown_tasker += 1
                _logger.warning(
                    "Fenrir import: task %s references unknown Name/tasker "
                    "%r (no user by login/email/name); leaving unset.",
                    code, raw_tasker)

            # Reviewer is an existing res.users — resolve and assign; a blank
            # cell leaves it unset, an unknown one is reported and left unset too.
            raw_reviewer = (raw_reviewer or "").strip()
            if raw_reviewer:
                reviewer_id = resolve_user(raw_reviewer)
                if reviewer_id:
                    row_vals["reviewer_id"] = reviewer_id
                else:
                    unknown_reviewer += 1
                    _logger.warning(
                        "Fenrir import: task %s references unknown Reviewer %r "
                        "(no user by login/email/name); leaving unset.",
                        code, raw_reviewer)

            # Drop empty strings so we don't overwrite defaults with "". But
            # keep explicit False for buyer_id/lead_user_id to override the
            # model's default of the current user.
            ALWAYS_KEEP = ("code", "buyer_id", "lead_user_id")
            row_vals = {k: v for k, v in row_vals.items()
                        if k in ALWAYS_KEEP or v not in ("", None, False)}
            to_create.append(row_vals)

        if not to_create:
            raise UserError(_(
                "No tasks were created. %(dup)d row(s) duplicated an existing "
                "Task ID and %(blank)d row(s) were empty."
            ) % {"dup": len(skipped_dup), "blank": skipped_blank})

        created = Task.with_context(fenrir_codes_final=True).create(to_create)
        _logger.info(
            "Fenrir: imported %d tasks from %s (%d auto-coded, %d duplicates "
            "skipped, %d empty, %d unknown phase, %d invalid price tier, "
            "%d unknown buyer, %d unknown tasker, %d unknown reviewer)", len(created),
            self.data_filename or "(no name)", generated, len(skipped_dup),
            skipped_blank, unknown_phase, invalid_tier, unknown_buyer,
            unknown_tasker, unknown_reviewer)

        summary = _("%d created") % len(created)
        if generated:
            summary += _(", %d auto-coded") % generated
        if skipped_dup:
            summary += _(", %d skipped (duplicate Task ID)") % len(skipped_dup)
        if skipped_blank:
            summary += _(", %d skipped (empty row)") % skipped_blank
        if unknown_phase:
            summary += _(", %d with unknown Phase (used default)") % (
                unknown_phase)
        if invalid_tier:
            summary += _(", %d with invalid Price Tier (left default)") % (
                invalid_tier)
        if unknown_buyer:
            summary += _(", %d with unknown Buyer (left unset)") % unknown_buyer
        if unknown_tasker:
            summary += _(", %d with unknown Name/tasker (left default)") % (
                unknown_tasker)
        if unknown_reviewer:
            summary += _(", %d with unknown Reviewer (left unset)") % (
                unknown_reviewer)

        return {
            "type": "ir.actions.act_window",
            "name": _("Imported Tasks — %s") % summary,
            "res_model": "fenrir.task",
            "view_mode": "list,form",
            "domain": [("id", "in", created.ids)],
            "context": {"from_all_tasks": True},
            "target": "current",
        }
