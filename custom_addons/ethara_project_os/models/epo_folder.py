"""The project's filing cabinet.

Every project gets the same skeleton the moment it is created, so a GPM never faces an
empty screen and a pod member always knows where to look:

    <project code>/
    ├── Knowledge/                  ← what a pod member reads before tasking
    │   ├── SOP/                    ← mandatory: the project cannot go live without it
    │   ├── Common Errors/
    │   ├── Task Videos/
    │   └── Other/
    └── Management/                 ← what the GPM keeps about the engagement
        └── Client Documents/       ← contracts, briefs, sign-offs — free nesting below

Two rules shape the design:

* **Knowledge is a fixed shape.** Its four folders exist on every project, cannot be
  renamed and cannot be deleted. That is what lets the onboarding screen say "read the
  SOP" without knowing anything about a particular project, and what lets the go-live
  gate ask a question with a stable answer.
* **Management is free-form.** Client work does not fit a fixed shape — one engagement
  has a single MSA, the next has forty files across six phases. Subfolders nest to any
  depth, and any of them can hold files.

The two sides also differ in *who may read them*: Knowledge is visible to anyone
allocated to the project, Management is GPM and Admin only. A pod member browsing the
folder tree simply does not see it (:meth:`_visible_to`).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# The skeleton created with every project. `slug` is the stable identifier code refers
# to — the *name* is a label a GPM could in principle translate, the slug never changes.
KNOWLEDGE_TREE = [
    ('sop', 'SOP', True),
    ('common_errors', 'Common Errors', False),
    ('task_videos', 'Task Videos', False),
    ('other', 'Other', False),
]
MANAGEMENT_TREE = [
    ('client_documents', 'Client Documents', False),
]


class EpoFolder(models.Model):
    _name = 'epo.folder'
    _description = 'Project OS Folder'
    _order = 'complete_name'
    _parent_store = True
    _rec_name = 'complete_name'

    project_id = fields.Many2one(
        'project.project', required=True, ondelete='cascade', index=True,
        domain=[('is_project_os', '=', True)])
    parent_id = fields.Many2one(
        'epo.folder', string='Parent folder', ondelete='cascade', index=True)
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many('epo.folder', 'parent_id', string='Subfolders')

    name = fields.Char(required=True)
    complete_name = fields.Char(
        compute='_compute_complete_name', store=True, recursive=True,
        string='Path')
    slug = fields.Char(
        index=True,
        help='Stable identifier for a system folder (sop, client_documents, …). '
             'Empty on folders a GPM created.')
    root = fields.Selection(
        [('knowledge', 'Knowledge'), ('management', 'Management')],
        required=True, index=True,
        help='Which side of the cabinet this folder lives on. Inherited from the '
             'parent; it decides who may read it.')
    is_system = fields.Boolean(
        default=False,
        help='Part of the skeleton every project gets. Cannot be renamed, moved or '
             'deleted — code and the onboarding screen depend on it being there.')
    is_mandatory = fields.Boolean(
        default=False, help='Must contain at least one document before go-live.')
    description = fields.Text()
    sequence = fields.Integer(default=10)

    document_ids = fields.One2many('epo.document', 'folder_id', string='Documents')
    document_count = fields.Integer(compute='_compute_counts')
    total_document_count = fields.Integer(
        compute='_compute_counts', string='Documents (incl. subfolders)')
    subfolder_count = fields.Integer(compute='_compute_counts')

    s3_prefix = fields.Char(
        compute='_compute_s3_prefix', store=True, recursive=True,
        help='Key prefix under which this folder\'s files are stored in the bucket. '
             'Mirrors the visible path so an operator browsing S3 sees the same shape.')

    active = fields.Boolean(default=True)

    _system_slug_uniq = models.UniqueIndex(
        '(project_id, slug) WHERE slug IS NOT NULL AND active',
        'That system folder already exists on this project.')
    _name_uniq_in_parent = models.UniqueIndex(
        '(project_id, parent_id, lower(name)) WHERE active AND parent_id IS NOT NULL',
        'A folder with that name already exists here.')
    _no_self_parent = models.Constraint(
        'CHECK (parent_id IS NULL OR parent_id <> id)',
        'A folder cannot be its own parent.')

    # ------------------------------------------------------------------
    # computes
    # ------------------------------------------------------------------
    @api.depends('name', 'parent_id.complete_name')
    def _compute_complete_name(self):
        for folder in self:
            folder.complete_name = (
                f'{folder.parent_id.complete_name} / {folder.name}'
                if folder.parent_id else folder.name)

    @api.depends('name', 'parent_id.s3_prefix', 'project_id.code')
    def _compute_s3_prefix(self):
        for folder in self:
            segment = self.env['epo.document']._slugify(folder.name)
            if folder.parent_id:
                folder.s3_prefix = f'{folder.parent_id.s3_prefix}/{segment}'
            else:
                code = self.env['epo.document']._slugify(
                    folder.project_id.code or f'project-{folder.project_id.id}')
                folder.s3_prefix = f'project-os/{code}/{segment}'

    def _compute_counts(self):
        for folder in self:
            folder.document_count = len(folder.document_ids.filtered('active'))
            folder.subfolder_count = len(folder.child_ids.filtered('active'))
            descendants = self.search([('id', 'child_of', folder.id)])
            folder.total_document_count = self.env['epo.document'].search_count([
                ('folder_id', 'in', descendants.ids), ('active', '=', True)])

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    @api.constrains('parent_id', 'project_id', 'root')
    def _check_tree(self):
        for folder in self:
            if not folder._check_recursion():
                raise ValidationError(_('Folders cannot contain themselves.'))
            parent = folder.parent_id
            if parent and parent.project_id != folder.project_id:
                raise ValidationError(_(
                    'A folder cannot sit inside another project\'s folder.'))
            if parent and parent.root != folder.root:
                raise ValidationError(_(
                    'A %(child)s folder cannot sit inside %(parent)s.',
                    child=folder.root, parent=parent.root))

    @api.constrains('is_system', 'parent_id')
    def _check_knowledge_is_flat(self):
        """Knowledge is a fixed shape so that "read the SOP" means the same thing on
        every project. Extra material goes in Other, not in a new folder."""
        for folder in self:
            if folder.root != 'knowledge' or not folder.parent_id:
                continue
            if folder.parent_id.parent_id:
                raise ValidationError(_(
                    'Knowledge has a fixed shape — put extra material in "Other" rather '
                    'than nesting new folders under %(parent)s.',
                    parent=folder.parent_id.name))

    def _assert_editable(self):
        for folder in self:
            if folder.is_system:
                raise UserError(_(
                    '"%s" is part of every project and cannot be renamed, moved or '
                    'deleted. You can add folders and files inside it.',
                    folder.complete_name))

    def write(self, vals):
        if {'name', 'parent_id', 'project_id', 'slug', 'root', 'is_system'} & set(vals):
            self._assert_editable()
        return super().write(vals)

    def unlink(self):
        self._assert_editable()
        for folder in self:
            if folder.total_document_count:
                raise UserError(_(
                    '"%(name)s" still holds %(n)s document(s). Empty it first — deleting '
                    'a folder must never quietly take files with it.',
                    name=folder.complete_name, n=folder.total_document_count))
        return super().unlink()

    # ------------------------------------------------------------------
    # the skeleton
    # ------------------------------------------------------------------
    @api.model
    def _build_skeleton(self, project):
        """Create the standard tree for a new project. Idempotent, so it can be re-run
        on an old project after the skeleton changes."""
        created = self.browse()
        for root, label, children in (
                ('knowledge', _('Knowledge'), KNOWLEDGE_TREE),
                ('management', _('Management'), MANAGEMENT_TREE)):
            parent = self.search([
                ('project_id', '=', project.id), ('slug', '=', root)], limit=1)
            if not parent:
                parent = self.create({
                    'project_id': project.id, 'name': label, 'slug': root,
                    'root': root, 'is_system': True,
                    'sequence': 10 if root == 'knowledge' else 20,
                })
                created |= parent
            seq = 10
            for slug, child_label, mandatory in children:
                existing = self.search([
                    ('project_id', '=', project.id), ('slug', '=', slug)], limit=1)
                if existing:
                    continue
                created |= self.create({
                    'project_id': project.id, 'parent_id': parent.id,
                    'name': child_label, 'slug': slug, 'root': root,
                    'is_system': True, 'is_mandatory': mandatory, 'sequence': seq,
                })
                seq += 10
        return created

    @api.model
    def _get(self, project, slug):
        return self.search([
            ('project_id', '=', project.id if hasattr(project, 'id') else project),
            ('slug', '=', slug)], limit=1)

    # ------------------------------------------------------------------
    # visibility
    # ------------------------------------------------------------------
    def _visible_to(self, user):
        """Knowledge is for everyone on the project; Management is GPM and Admin only.

        Client contracts and commercial paperwork are not a pod member's business, and
        the honest way to express that is to leave them out of the tree entirely rather
        than show a folder that errors when opened."""
        if user._epo_role() in ('gpm', 'admin'):
            return self
        return self.filtered(lambda f: f.root == 'knowledge')

    def to_dict(self, with_documents=False, user=None):
        self.ensure_one()
        data = {
            'id': self.id,
            'name': self.name,
            'path': self.complete_name,
            'slug': self.slug or '',
            'root': self.root,
            'parent_id': self.parent_id.id or None,
            'project_id': self.project_id.id,
            'is_system': self.is_system,
            'is_mandatory': self.is_mandatory,
            'description': self.description or '',
            'document_count': self.document_count,
            'total_document_count': self.total_document_count,
            'subfolder_count': self.subfolder_count,
        }
        if with_documents:
            data['documents'] = [d.to_dict() for d in
                                 self.document_ids.filtered('active')]
        return data

    @api.model
    def tree_for(self, project, user=None, with_documents=False):
        """The whole cabinet in one call, nested — a file browser needs the shape, and
        one round trip per folder would make it crawl."""
        folders = self.search([('project_id', '=', project.id)])
        if user is not None:
            folders = folders._visible_to(user)
        by_parent = {}
        for folder in folders:
            by_parent.setdefault(folder.parent_id.id or 0, []).append(folder)

        def branch(parent_id):
            nodes = []
            for folder in sorted(by_parent.get(parent_id, []),
                                 key=lambda f: (f.sequence, f.name)):
                node = folder.to_dict(with_documents=with_documents)
                node['children'] = branch(folder.id)
                nodes.append(node)
            return nodes

        return branch(0)
