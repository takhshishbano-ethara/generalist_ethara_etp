# -*- coding: utf-8 -*-
import base64
import csv
import hashlib
import io
import zipfile
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged("post_install", "-at_install")
class TestAuroraImportTokensWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def _make_csv_b64(self, tokens):
        buf = io.StringIO()
        writer = csv.writer(buf)
        for t in tokens:
            writer.writerow([t])
        return base64.b64encode(buf.getvalue().encode()).decode()

    def _make_xlsx_b64(self, values):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                '</Types>')
            zf.writestr("_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>')
            zf.writestr("xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
                '</Relationships>')
            zf.writestr("xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
            ss_parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(values)}" uniqueCount="{len(values)}">']
            for v in values:
                escaped = str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                ss_parts.append(f"<si><t>{escaped}</t></si>")
            ss_parts.append("</sst>")
            zf.writestr("xl/sharedStrings.xml", "".join(ss_parts))
            sheet_parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
            for ri, v in enumerate(values, start=1):
                sheet_parts.append(f'<row r="{ri}"><c r="A{ri}" t="s"><v>{ri-1}</v></c></row>')
            sheet_parts.append("</sheetData></worksheet>")
            zf.writestr("xl/worksheets/sheet1.xml", "".join(sheet_parts))
        return base64.b64encode(buf.getvalue()).decode()

    def _create_wizard(self, b64_data, filename):
        return self.env["aurora.import.tokens.wizard"].create({
            "token_file": b64_data,
            "token_filename": filename,
        })

    # ═══════════════════════════════════════════════════════════════════════════
    # Constants
    # ═══════════════════════════════════════════════════════════════════════════

    def test_valid_prefixes(self):
        from ..models.import_tokens_wizard import _VALID_PREFIXES
        self.assertIn("ghp_", _VALID_PREFIXES)
        self.assertIn("gho_", _VALID_PREFIXES)
        self.assertIn("github_pat_", _VALID_PREFIXES)

    def test_header_names(self):
        from ..models.import_tokens_wizard import _HEADER_NAMES
        self.assertIn("token", _HEADER_NAMES)
        self.assertIn("pat", _HEADER_NAMES)
        self.assertIn("github_token", _HEADER_NAMES)

    def test_batch_size(self):
        from ..models.import_tokens_wizard import _BATCH_SIZE
        self.assertEqual(_BATCH_SIZE, 500)

    # ═══════════════════════════════════════════════════════════════════════════
    # _parse_csv
    # ═══════════════════════════════════════════════════════════════════════════

    def test_parse_csv_basic(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        raw = b"ghp_token1\nghp_token2\n"
        result = AuroraImportTokensWizard._parse_csv(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "ghp_token1")

    def test_parse_csv_with_header(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        raw = b"token\nghp_abc\nghp_def\n"
        result = AuroraImportTokensWizard._parse_csv(raw)
        self.assertEqual(len(result), 2)
        self.assertNotIn("token", result)

    def test_parse_csv_empty(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        result = AuroraImportTokensWizard._parse_csv(b"")
        self.assertEqual(result, [])

    def test_parse_csv_whitespace(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        raw = b"  ghp_tok  \n"
        result = AuroraImportTokensWizard._parse_csv(raw)
        self.assertEqual(result[0], "ghp_tok")

    def test_parse_csv_single_token(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        raw = b"ghp_single\n"
        result = AuroraImportTokensWizard._parse_csv(raw)
        self.assertEqual(len(result), 1)

    def test_parse_csv_bom(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        raw = b"\xef\xbb\xbfghp_bom\n"
        result = AuroraImportTokensWizard._parse_csv(raw)
        self.assertEqual(len(result), 1)

    # ═══════════════════════════════════════════════════════════════════════════
    # _parse_xlsx
    # ═══════════════════════════════════════════════════════════════════════════

    def test_parse_xlsx_basic(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        xlsx = base64.b64decode(self._make_xlsx_b64(["ghp_x1", "ghp_x2"]))
        result = AuroraImportTokensWizard._parse_xlsx(xlsx)
        self.assertEqual(len(result), 2)

    def test_parse_xlsx_with_header(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        xlsx = base64.b64decode(self._make_xlsx_b64(["token", "ghp_val"]))
        result = AuroraImportTokensWizard._parse_xlsx(xlsx)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], "ghp_val")

    def test_parse_xlsx_invalid_raises(self):
        from ..models.import_tokens_wizard import AuroraImportTokensWizard
        with self.assertRaises(UserError):
            AuroraImportTokensWizard._parse_xlsx(b"not a zip file")

    # ═══════════════════════════════════════════════════════════════════════════
    # action_import
    # ═══════════════════════════════════════════════════════════════════════════

    def test_import_no_file_raises(self):
        wiz = self.env["aurora.import.tokens.wizard"].create({})
        with self.assertRaises(UserError):
            wiz.action_import()

    def test_import_unsupported_format(self):
        b64 = base64.b64encode(b"data").decode()
        wiz = self._create_wizard(b64, "tokens.txt")
        with self.assertRaises(UserError):
            wiz.action_import()

    def test_import_csv_valid_tokens(self):
        b64 = self._make_csv_b64(["ghp_aaa111", "ghp_bbb222"])
        wiz = self._create_wizard(b64, "tokens.csv")
        wiz.action_import()
        tokens = self.env["aurora.github.token"].search([
            ("token_hash", "in", [
                hashlib.sha256(b"ghp_aaa111").hexdigest(),
                hashlib.sha256(b"ghp_bbb222").hexdigest(),
            ])
        ])
        self.assertEqual(len(tokens), 2)

    def test_import_csv_invalid_prefix_rejected(self):
        b64 = self._make_csv_b64(["invalid_token", "ghp_valid1"])
        wiz = self._create_wizard(b64, "tokens.csv")
        wiz.action_import()
        tokens = self.env["aurora.github.token"].search([
            ("token_hash", "=", hashlib.sha256(b"invalid_token").hexdigest())
        ])
        self.assertEqual(len(tokens), 0)

    def test_import_csv_all_invalid_raises(self):
        b64 = self._make_csv_b64(["bad1", "bad2"])
        wiz = self._create_wizard(b64, "tokens.csv")
        with self.assertRaises(UserError):
            wiz.action_import()

    def test_import_csv_no_tokens_raises(self):
        b64 = self._make_csv_b64([])
        wiz = self._create_wizard(b64, "tokens.csv")
        with self.assertRaises(UserError):
            wiz.action_import()

    def test_import_csv_dedup(self):
        h = hashlib.sha256(b"ghp_dup123").hexdigest()
        self.env["aurora.github.token"].sudo().create({
            "name": "Existing", "token": "ghp_dup123", "token_hash": h, "state": "active",
        })
        b64 = self._make_csv_b64(["ghp_dup123"])
        wiz = self._create_wizard(b64, "tokens.csv")
        wiz.action_import()
        self.assertIn("already exist", wiz.result_message)

    def test_import_csv_draft_state(self):
        b64 = self._make_csv_b64(["ghp_draft111"])
        wiz = self._create_wizard(b64, "tokens.csv")
        wiz.action_import()
        tok = self.env["aurora.github.token"].search([
            ("token_hash", "=", hashlib.sha256(b"ghp_draft111").hexdigest())
        ])
        self.assertEqual(tok.state, "draft")

    def test_import_csv_result_message(self):
        b64 = self._make_csv_b64(["ghp_msg111", "ghp_msg222"])
        wiz = self._create_wizard(b64, "tokens.csv")
        wiz.action_import()
        self.assertIn("2 tokens imported", wiz.result_message)

    def test_import_xlsx_valid(self):
        b64 = self._make_xlsx_b64(["ghp_xlsx1", "ghp_xlsx2"])
        wiz = self._create_wizard(b64, "tokens.xlsx")
        wiz.action_import()
        self.assertIn("2 tokens imported", wiz.result_message)

    def test_import_github_pat_prefix(self):
        b64 = self._make_csv_b64(["github_pat_abc123"])
        wiz = self._create_wizard(b64, "tokens.csv")
        wiz.action_import()
        tok = self.env["aurora.github.token"].search([
            ("token_hash", "=", hashlib.sha256(b"github_pat_abc123").hexdigest())
        ])
        self.assertEqual(len(tok), 1)

    def test_import_gho_prefix(self):
        b64 = self._make_csv_b64(["gho_orgtoken1"])
        wiz = self._create_wizard(b64, "tokens.csv")
        wiz.action_import()
        tok = self.env["aurora.github.token"].search([
            ("token_hash", "=", hashlib.sha256(b"gho_orgtoken1").hexdigest())
        ])
        self.assertEqual(len(tok), 1)

    def test_show_result(self):
        b64 = self._make_csv_b64(["ghp_show1"])
        wiz = self._create_wizard(b64, "tokens.csv")
        result = wiz.action_import()
        self.assertEqual(result["type"], "ir.actions.act_window")
        self.assertEqual(result["res_model"], "aurora.import.tokens.wizard")

    def test_import_mixed_valid_invalid(self):
        b64 = self._make_csv_b64(["ghp_good1", "bad_token", "ghp_good2", "another_bad"])
        wiz = self._create_wizard(b64, "tokens.csv")
        wiz.action_import()
        self.assertIn("2 tokens imported", wiz.result_message)
        self.assertIn("2 invalid", wiz.result_message)
