"""Documents — files and links, in folders, stored in S3 or in Odoo.

The Knowledge and Management folders are an S3 layout. A document is either an object
in the bucket or an external link — there is no third case:

* **s3** — every uploaded file, without exception. The key mirrors the visible path, so
  an operator browsing the bucket sees the same shape as a PM browsing the folders.
  No bucket configured means no upload: refused with a message saying so, rather than
  quietly written somewhere else.
* **link** — the document *is* a URL. Nothing is stored.

Three rules that matter more than the storage choice:

1. **Nothing is ever served from a public URL.** Downloads go through
   :meth:`download_target`, which checks the caller can read the document and then
   mints a short-lived presigned URL. Client contracts behind a guessable CDN path is
   exactly the failure this design exists to avoid.
2. **A document must carry the payload its type implies** — a 'file' with no file is a
   broken link in somebody's face at 2am.
3. **Links must be http(s).** A ``javascript:`` URL rendered into a PM's browser is a
   privilege escalation, not a typo.
"""

import base64
import hashlib
import logging
import mimetypes
import re
import unicodedata
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .epo_folder import FOLDER_SLUGS, KNOWLEDGE_TREE, MANAGEMENT_TREE

_logger = logging.getLogger(__name__)

URL_RE = re.compile(r'^https?://', re.IGNORECASE)
MAX_UPLOAD_MB = 200


class EpoDocument(models.Model):
    _name = 'epo.document'
    _description = 'Project OS Document'
    _order = 'folder_id, sequence, id'

    folder_id = fields.Many2one(
        'epo.folder', required=True, ondelete='restrict', index=True,
        help='Deleting a folder must never quietly take its files with it.')
    project_id = fields.Many2one(
        'project.project', related='folder_id.project_id', store=True, index=True)
    root = fields.Selection(related='folder_id.root', store=True, index=True)
    # The document's type — SOP / Common Errors / Task Videos / Other — and the field a
    # person actually picks. The folder remains the single source of truth on disk; this
    # reads it and, when set, moves the document to the matching folder of its own
    # project. Two directions, one fact, so the two can never disagree.
    #
    # It was `related='folder_id.slug'` and therefore READ-ONLY, which made it useless:
    # the column sat blank until you found the folder picker, and the folder picker shows
    # "Knowledge / SOP" rather than "SOP". One editable column replaces both.
    #
    # The stored values are unchanged (`sop`, `common_errors`, …), so _recompute_gate's
    # `category == 'sop'`, every search filter and every migration are untouched.
    category = fields.Selection(
        selection='_selection_category', string='Type', store=True, index=True,
        readonly=False, compute='_compute_category', inverse='_inverse_category',
        help='Which of the fixed folders this document sits in. Empty inside a folder a '
             'PM created, because that is not one of the fixed types.')

    name = fields.Char(required=True)
    description = fields.Text()
    sequence = fields.Integer(default=10)
    is_mandatory = fields.Boolean(
        default=False, help='Mandatory documents must be acknowledged during onboarding.')

    doc_type = fields.Selection(
        [('file', 'File'), ('link', 'Link')], required=True, default='file')
    storage = fields.Selection(
        [('s3', 'S3'), ('link', 'External link'), ('odoo', 'Odoo attachment (legacy)')],
        compute='_compute_storage', store=True, readonly=True,
        help='New uploads are always S3. "Legacy" only appears on rows written before '
             'the bucket became mandatory; they still download correctly.')

    @api.model
    def _selection_category(self):
        """The types offered, narrowed to the tab being edited.

        A Selection takes no ``domain``, so the Knowledge grid would otherwise offer
        Client Documents and filing a document there would make it vanish from the tab it
        was created in. The one2many passes ``epo_category_root`` and this narrows to that
        side; with no context the full set is returned, which is what keeps the stored
        value valid everywhere else (the ORM validates writes against this same list).
        """
        root = self.env.context.get('epo_category_root')
        if root == 'knowledge':
            return [(slug, label) for slug, label, _m in KNOWLEDGE_TREE]
        if root == 'management':
            return [(slug, label) for slug, label, _m in MANAGEMENT_TREE]
        return FOLDER_SLUGS

    @api.depends('folder_id.slug')
    def _compute_category(self):
        for doc in self:
            doc.category = doc.folder_id.slug or False

    def _inverse_category(self):
        """Picking a type files the document in that folder.

        Only ever moves within the document's own project — `_folder_for` searches by
        project_id — so choosing a type can never fling a document into another
        project's folder. A type with no matching folder (or a document not yet attached
        to a project) is left alone rather than guessed at; the folder is required, so
        the create path below has already resolved one by this point.
        """
        for doc in self:
            if not doc.category or doc.folder_id.slug == doc.category:
                continue
            folder = self._folder_for(doc.project_id.id, doc.category)
            if folder:
                doc.folder_id = folder

    @api.model
    def _folder_for(self, project_id, slug):
        """The one system folder of `project_id` carrying `slug`, or an empty recordset."""
        if not project_id or not slug:
            return self.env['epo.folder']
        return self.env['epo.folder'].search([
            ('project_id', '=', project_id),
            ('slug', '=', slug),
            ('active', '=', True),
        ], limit=1)

    # --- payloads ------------------------------------------------------
    url = fields.Char(help='For a link document, and for nothing else.')
    attachment_id = fields.Many2one(
        'ir.attachment', ondelete='restrict', copy=False, readonly=True,
        help='Legacy only — kept so documents stored before the bucket became '
             'mandatory still resolve. Never written by a new upload.')
    s3_key = fields.Char(copy=False, help='Object key in the bucket. Never public.')
    s3_bucket = fields.Char(copy=False)
    file_data = fields.Binary(
        string='Upload', attachment=False,
        help='Write-only staging field. create()/write() pop the bytes and hand them to '
             'the storage backend, so this column is never actually written — the file '
             'lives in S3 or in an attachment, not inline in the row.')
    file_name = fields.Char()
    file_size = fields.Integer(readonly=True)
    mime_type = fields.Char(readonly=True)
    checksum = fields.Char(readonly=True, help='SHA-256 of the stored bytes.')

    uploaded_by_id = fields.Many2one(
        'res.users', readonly=True, default=lambda s: s.env.user, ondelete='restrict')
    active = fields.Boolean(default=True)

    _name_uniq = models.UniqueIndex(
        '(folder_id, lower(name)) WHERE active',
        'That folder already has a document with this name.')
    _payload_matches = models.Constraint(
        "CHECK ((doc_type = 'file' AND url IS NULL) "
        "    OR (doc_type = 'link' AND url IS NOT NULL))",
        'A file document must not carry a URL, and a link document must.')

    # ------------------------------------------------------------------
    # computes and guards
    # ------------------------------------------------------------------
    @api.depends('doc_type', 's3_key', 'attachment_id')
    def _compute_storage(self):
        for doc in self:
            if doc.doc_type == 'link':
                doc.storage = 'link'
            elif doc.s3_key:
                doc.storage = 's3'
            else:
                doc.storage = 'odoo'

    @api.constrains('doc_type', 'url')
    def _check_url_scheme(self):
        for doc in self:
            if doc.doc_type == 'link' and not URL_RE.match(doc.url or ''):
                raise ValidationError(_(
                    'Links must start with http:// or https:// — "%(url)s" does not.',
                    url=doc.url or ''))

    @api.constrains('doc_type', 'attachment_id', 's3_key')
    def _check_file_present(self):
        """A 'file' document with no file is a broken link in somebody's face at 2am.

        Skipped while a record is mid-creation: the row has to exist before the bytes
        can be keyed to it, so ``create`` stores the payload and then runs this check
        explicitly with the flag cleared.
        """
        if self.env.context.get('epo_deferred_payload'):
            return
        for doc in self:
            if doc.doc_type == 'file' and not (doc.attachment_id or doc.s3_key):
                raise ValidationError(_(
                    '"%s" is a file document but nothing was uploaded.', doc.name))

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        # Resolve the folder from the picked type. The inverse above cannot do this at
        # create time: it needs the project, and `project_id` is a stored related on
        # `folder_id`, which is precisely what is still missing. The one2many on the
        # project form supplies `project_id` in the vals, so use that.
        for vals in vals_list:
            if vals.get('folder_id') or not vals.get('category'):
                continue
            project_id = (vals.get('project_id')
                          or self.env.context.get('default_project_id'))
            folder = self._folder_for(project_id, vals['category'])
            if folder:
                vals['folder_id'] = folder.id
        staged = [vals.pop('file_data', None) for vals in vals_list]
        # The row must exist before its bytes can be keyed to it, so the
        # "a file document has a file" check is deferred to just after storage.
        docs = super(EpoDocument,
                     self.with_context(epo_deferred_payload=True)).create(vals_list)
        for doc, payload in zip(docs, staged):
            if payload:
                doc._store(payload, doc.file_name or doc.name)
        docs.with_context(epo_deferred_payload=False)._check_file_present()
        docs.mapped('project_id')._recompute_gate()
        for doc in docs:
            self.env['epo.timeline.event'].log(
                event_type='knowledge_added', project_id=doc.project_id.id,
                summary=_('Document added: %(path)s / %(name)s',
                          path=doc.folder_id.complete_name, name=doc.name),
                payload={'folder': doc.folder_id.complete_name,
                         'storage': doc.storage}, record=doc)
        return docs

    def write(self, vals):
        payload = vals.pop('file_data', None)
        res = super().write(vals)
        if payload:
            for doc in self:
                doc._store(payload, vals.get('file_name') or doc.file_name or doc.name)
        if {'folder_id', 'active'} & set(vals):
            self.mapped('project_id')._recompute_gate()
        return res

    def unlink(self):
        projects = self.mapped('project_id')
        removed = [(d.project_id.id, d.folder_id.complete_name, d.name) for d in self]
        res = super().unlink()
        projects._recompute_gate()
        for project_id, path, name in removed:
            self.env['epo.timeline.event'].log(
                event_type='knowledge_removed', project_id=project_id,
                summary=_('Document removed: %(path)s / %(name)s', path=path, name=name))
        return res

    # ------------------------------------------------------------------
    # storage
    # ------------------------------------------------------------------
    @api.model
    def _slugify(self, text):
        """Filesystem- and URL-safe segment. Keeps the bucket browsable by a human."""
        text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode()
        text = re.sub(r'[^\w\s.-]', '', text).strip().lower()
        return re.sub(r'[-\s]+', '-', text) or 'untitled'

    @api.model
    def _s3_connector(self):
        """The configured bucket, or an empty recordset when none is set up.

        Never raises: a missing bucket must degrade to Odoo storage, not block a PM
        from uploading an SOP."""
        if 's3.connector' not in self.env:
            return None
        params = self.env['ir.config_parameter'].sudo()
        connector_id = params.get_param('epo.s3.connector_id')
        Connector = self.env['s3.connector'].sudo()
        if connector_id:
            connector = Connector.browse(int(connector_id)).exists()
            if connector:
                return connector
        return Connector.search([], limit=1)

    def _store(self, b64_payload, filename):
        """Put the bytes in the bucket and record what was stored.

        S3 is the only place project files live. There is no second storage path: a
        file is either in the bucket under its folder's prefix, or the upload failed
        and nothing was saved. Falling back to an Odoo attachment would mean the same
        folder holds documents in two different places, and "where is this file?"
        stops having one answer.
        """
        self.ensure_one()
        try:
            raw = base64.b64decode(b64_payload)
        except Exception as exc:  # noqa: BLE001
            raise UserError(_('That upload is not valid file data.')) from exc
        if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
            raise UserError(_(
                'That file is %(size).1f MB. The limit is %(max)s MB — link to it '
                'instead of uploading it.',
                size=len(raw) / 1024 / 1024, max=MAX_UPLOAD_MB))

        connector = self._s3_connector()
        if not connector:
            raise UserError(_(
                'No document bucket is configured, so there is nowhere to put this '
                'file. Set one in Settings → Project OS → Document storage, or add the '
                'document as a link instead.'))

        filename = filename or 'file'
        mimetype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        key = self._s3_key_for(filename)
        try:
            self._s3_put(connector, key, raw, mimetype)
        except Exception as exc:  # noqa: BLE001
            _logger.warning('Project OS: S3 upload failed for %s: %s', key, exc)
            raise UserError(_(
                'The file could not be uploaded to the bucket, so nothing was saved. '
                '(%s)', exc)) from exc

        self.sudo().write({
            's3_key': key, 's3_bucket': connector.name, 'file_name': filename,
            'file_size': len(raw), 'mime_type': mimetype,
            'checksum': hashlib.sha256(raw).hexdigest(),
        })

    def _s3_key_for(self, filename):
        """Mirror the visible path, then make the leaf unique.

        The uuid means re-uploading "contract.pdf" never silently overwrites last
        month's contract.pdf — versions are separate objects, and the folder shows both.
        """
        self.ensure_one()
        stem = self._slugify(filename.rsplit('.', 1)[0])
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
        ext = re.sub(r'[^a-z0-9]', '', ext)[:10] or 'bin'
        return f'{self.folder_id.s3_prefix}/{uuid.uuid4().hex[:12]}-{stem}.{ext}'

    def _s3_put(self, connector, key, raw, mimetype):
        """Private put. Deliberately does NOT reuse s3.connector.upload_to_s3, which
        sets ACL='public-read' — correct for product images, wrong for a client MSA."""
        client = connector._get_s3_client()
        client.put_object(
            Bucket=connector.name, Key=key, Body=raw, ContentType=mimetype)

    def download_target(self):
        """Where the caller should fetch the bytes from.

        Returns a dict the API hands straight back: a short-lived presigned URL for S3,
        Odoo's authenticated content route for an attachment, or the link itself.
        """
        self.ensure_one()
        if self.doc_type == 'link':
            return {'kind': 'link', 'url': self.url}
        if self.s3_key:
            connector = self._s3_connector()
            if not connector:
                raise UserError(_(
                    'This file lives in S3 but no bucket is configured any more.'))
            ttl = int(self.env['ir.config_parameter'].sudo().get_param(
                'epo.s3.url_ttl', 300))
            client = connector._get_s3_client()
            url = client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.s3_bucket or connector.name, 'Key': self.s3_key},
                ExpiresIn=ttl)
            return {'kind': 's3', 'url': url, 'expires_in': ttl,
                    'filename': self.file_name or self.name}
        if self.attachment_id:
            return {'kind': 'attachment',
                    'url': f'/web/content/{self.attachment_id.id}?download=true',
                    'filename': self.file_name or self.name}
        raise UserError(_('"%s" has no stored file.', self.name))

    def read_bytes(self):
        """The raw bytes of this document, or ``None`` when there are none to read.

        ``download_target`` mints a presigned URL for a browser; this is for handing the
        content to another module in-process — the assessment app's prompt resources
        want a Binary, not a link.

        Returns ``None`` for a link document: there is no file, and the caller has to
        decide what to do about that rather than be handed empty bytes.
        """
        self.ensure_one()
        if self.doc_type == 'link':
            return None
        if self.s3_key:
            connector = self._s3_connector()
            if not connector:
                raise UserError(_(
                    '"%s" lives in S3 but no bucket is configured any more, so its '
                    'contents cannot be read.', self.name))
            client = connector._get_s3_client()
            obj = client.get_object(
                Bucket=self.s3_bucket or connector.name, Key=self.s3_key)
            return obj['Body'].read()
        if self.attachment_id:
            return base64.b64decode(self.attachment_id.datas or b'')
        return None

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description or '',
            'folder_id': self.folder_id.id,
            'folder_path': self.folder_id.complete_name,
            'category': self.category or '',
            'root': self.root,
            'doc_type': self.doc_type,
            'storage': self.storage,
            'url': self.url or '',
            'file_name': self.file_name or '',
            'file_size': self.file_size,
            'mime_type': self.mime_type or '',
            'is_mandatory': self.is_mandatory,
            'uploaded_by': self.uploaded_by_id.name or '',
            'uploaded_at': str(self.create_date or ''),
            # Not a URL to the bytes — the caller asks for one when it actually needs
            # it, and gets a link that expires.
            'download_endpoint': f'/api/project-os/documents/{self.id}/download',
        }
