"""The form engine — one engine, two form types.

The *stagelist* (what a pod member fills per task) and the *feedback taker* (what the
client said on their platform) are the same shape: sections of typed fields, built by a
GPM with no code. Keeping them as one engine with a ``form_type`` discriminator is the
prototype's strongest decision and it is preserved here.

The prototype's worst defect is also fixed here. Its "save template" handler deleted
every section, which cascaded to fields and then to *every answer ever submitted*.
Three independent defences replace it:

1. A published template is structurally immutable — sections and fields refuse to
   change (:meth:`_assert_mutable`).
2. Editing a published form clones it to a new draft version instead
   (:meth:`action_new_version`).
3. ``epo.form.value.field_id`` is ``ondelete='restrict'``: even a direct SQL-adjacent
   path cannot delete a field that has answers.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

FIELD_TYPES = [
    ('short_text', 'Short text'),
    ('paragraph', 'Paragraph'),
    ('number', 'Number'),
    ('email', 'Email'),
    ('date', 'Date'),
    ('dropdown', 'Dropdown'),
    ('radio', 'Multiple choice'),
    ('checkboxes', 'Checkboxes'),
    ('grid', 'Rating grid'),
    ('image', 'Image upload'),
    ('file', 'File upload'),
    ('section_header', 'Section header'),
]

CHOICE_TYPES = ('dropdown', 'radio', 'checkboxes')


class EpoFormTemplate(models.Model):
    _name = 'epo.form.template'
    _description = 'Project OS Form Template'
    _order = 'project_id, form_type, version desc'
    _inherit = ['mail.thread']

    project_id = fields.Many2one(
        'project.project', required=True, ondelete='cascade', index=True,
        domain=[('is_project_os', '=', True)])
    form_type = fields.Selection(
        [('stagelist', 'Stagelist'), ('feedback', 'Feedback Taker')],
        required=True, default='stagelist', index=True)
    name = fields.Char(required=True, default='Untitled form')
    description = fields.Text()
    version = fields.Integer(required=True, default=1, readonly=True)
    state = fields.Selection(
        [('draft', 'Draft'), ('published', 'Published'), ('archived', 'Archived')],
        required=True, default='draft', tracking=True, index=True)
    parent_id = fields.Many2one(
        'epo.form.template', string='Previous version', ondelete='set null')

    published_at = fields.Datetime(readonly=True)
    published_by_id = fields.Many2one('res.users', readonly=True, ondelete='restrict')
    archived_at = fields.Datetime(readonly=True)

    section_ids = fields.One2many('epo.form.section', 'template_id', string='Sections')
    field_ids = fields.One2many('epo.form.field', 'template_id', string='Fields')
    entry_ids = fields.One2many('epo.form.entry', 'template_id', string='Entries')
    entry_count = fields.Integer(compute='_compute_entry_count')

    _one_published = models.UniqueIndex(
        "(project_id, form_type) WHERE state = 'published'",
        'That project already has a published form of this type. Publishing a new '
        'version supersedes the old one automatically.')
    _version_uniq = models.Constraint(
        'UNIQUE (project_id, form_type, version)',
        'That version number is already used for this form.')
    _version_pos = models.Constraint(
        'CHECK (version >= 1)', 'Versions start at 1.')
    _no_self_parent = models.Constraint(
        'CHECK (parent_id IS NULL OR parent_id <> id)',
        'A form version cannot be its own predecessor.')

    def _compute_entry_count(self):
        Entry = self.env['epo.form.entry'].sudo()
        for tmpl in self:
            tmpl.entry_count = Entry.search_count([
                ('template_id', '=', tmpl.id), ('state', '=', 'submitted')])

    # ------------------------------------------------------------------
    # immutability
    # ------------------------------------------------------------------
    def _assert_mutable(self):
        for tmpl in self:
            if tmpl.state == 'published':
                raise UserError(_(
                    '"%(name)s" is published and in use. Create a new version to change '
                    'it — editing it in place would rewrite what people already '
                    'answered.', name=tmpl.name))

    def write(self, vals):
        structural = {'form_type', 'project_id'}
        if structural & set(vals):
            self._assert_mutable()
        return super().write(vals)

    def unlink(self):
        for tmpl in self:
            if tmpl.state == 'published':
                raise UserError(_('Archive the published form instead of deleting it.'))
            if tmpl.entry_count:
                raise UserError(_(
                    '"%s" already has submissions and cannot be deleted.', tmpl.name))
        return super().unlink()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def action_publish(self):
        """Publish this draft, superseding the current published version of the same
        type, and take the project live if that completes the gate."""
        for tmpl in self:
            if tmpl.state == 'published':
                continue
            if tmpl.state == 'archived':
                raise UserError(_('An archived form cannot be republished — clone it.'))
            if not tmpl.field_ids.filtered(lambda f: f.field_type != 'section_header'):
                raise UserError(_(
                    '"%s" has no answerable fields. A form nobody can fill will silently '
                    'block every submission.', tmpl.name))
            current = self.search([
                ('project_id', '=', tmpl.project_id.id),
                ('form_type', '=', tmpl.form_type),
                ('state', '=', 'published'),
                ('id', '!=', tmpl.id),
            ])
            current.write({'state': 'archived', 'archived_at': fields.Datetime.now()})
            # Flush before publishing this one. The ORM would otherwise defer the
            # archive UPDATE past the publish UPDATE, leaving two published forms of
            # the same type for an instant — which is exactly what `_one_published`
            # forbids, so publishing a new version failed outright.
            current.flush_recordset(['state'])
            tmpl.write({
                'state': 'published',
                'published_at': fields.Datetime.now(),
                'published_by_id': self.env.user.id,
            })
            self.env['epo.timeline.event'].log(
                event_type='form_published', project_id=tmpl.project_id.id,
                summary=_('%(type)s v%(v)s published',
                          type=dict(self._fields['form_type'].selection)[tmpl.form_type],
                          v=tmpl.version),
                record=tmpl)
            tmpl.project_id._recompute_gate()
            tmpl.project_id._try_auto_activate()
        return True

    def action_new_version(self):
        """Clone a published form into a fresh draft, structure and all.

        The old version keeps its own fields, so every answer ever given still resolves
        to the exact field it was given against — which is why the entry snapshots the
        version it was filled on.

        The sections and fields are copied explicitly: Odoo does not copy one2many
        relations by default, so a plain ``copy()`` produced an *empty* draft and a GPM
        wanting to change one label had to rebuild the whole form from memory.
        """
        self.ensure_one()
        next_version = max(self.search([
            ('project_id', '=', self.project_id.id),
            ('form_type', '=', self.form_type),
        ]).mapped('version') or [0]) + 1
        clone = self.copy({
            'version': next_version,
            'state': 'draft',
            'parent_id': self.id,
            'published_at': False,
            'published_by_id': False,
            'archived_at': False,
        })
        self._copy_structure_to(clone)
        self.env['epo.timeline.event'].log(
            event_type='form_versioned', project_id=self.project_id.id,
            summary=_('%(name)s v%(v)s drafted', name=self.name, v=next_version),
            record=clone)
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': clone.id,
            'view_mode': 'form',
        }

    def _copy_structure_to(self, target):
        """Rebuild this form's sections and fields on `target`.

        Field ids deliberately differ: an answer belongs to the field of the version it
        was filled against, and re-using ids across versions would let a later edit
        rewrite the meaning of an old answer.
        """
        self.ensure_one()
        Section = self.env['epo.form.section']
        Field = self.env['epo.form.field']
        section_map = {}
        for section in self.section_ids.sorted(lambda s: (s.sequence, s.id)):
            section_map[section.id] = Section.create({
                'template_id': target.id,
                'name': section.name,
                'help_text': section.help_text,
                'sequence': section.sequence,
            })
        for field in self.field_ids.sorted(lambda f: (f.sequence, f.id)):
            Field.create({
                'template_id': target.id,
                'section_id': section_map[field.section_id.id].id
                if field.section_id else False,
                'name': field.name,
                'help_text': field.help_text,
                'field_type': field.field_type,
                'is_required': field.is_required,
                'options_json': field.options_json,
                'grid_rows_json': field.grid_rows_json,
                'grid_cols_json': field.grid_cols_json,
                'sequence': field.sequence,
            })
        return target

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for vals in vals_list:
            vals.setdefault('state', 'draft')
        return vals_list

    def schema(self):
        """The template as the client renders it: sections, each with its fields, in
        order. One call, no N+1 — the filler is on the critical path every time
        somebody opens a form."""
        self.ensure_one()
        loose = self.field_ids.filtered(lambda f: not f.section_id)
        sections = []
        for section in self.section_ids.sorted(lambda s: (s.sequence, s.id)):
            sections.append({
                'id': section.id,
                'title': section.name,
                'help': section.help_text or '',
                'fields': [f._to_dict() for f in section.field_ids.sorted(
                    lambda f: (f.sequence, f.id))],
            })
        if loose:
            sections.append({
                'id': 0, 'title': self.name, 'help': '',
                'fields': [f._to_dict() for f in loose.sorted(lambda f: (f.sequence, f.id))],
            })
        return {
            'id': self.id,
            'project_id': self.project_id.id,
            'project_name': self.project_id.name,
            'form_type': self.form_type,
            'title': self.name,
            'description': self.description or '',
            'version': self.version,
            'state': self.state,
            'sections': sections,
        }


class EpoFormSection(models.Model):
    _name = 'epo.form.section'
    _description = 'Project OS Form Section'
    _order = 'template_id, sequence, id'

    template_id = fields.Many2one(
        'epo.form.template', required=True, ondelete='cascade', index=True)
    name = fields.Char(required=True, default='Section')
    help_text = fields.Text(string='Help')
    sequence = fields.Integer(default=10)
    field_ids = fields.One2many('epo.form.field', 'section_id', string='Fields')

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.mapped('template_id')._assert_mutable()
        return records

    def write(self, vals):
        self.mapped('template_id')._assert_mutable()
        return super().write(vals)

    def unlink(self):
        self.mapped('template_id')._assert_mutable()
        return super().unlink()


class EpoFormField(models.Model):
    _name = 'epo.form.field'
    _description = 'Project OS Form Field'
    _order = 'template_id, sequence, id'

    template_id = fields.Many2one(
        'epo.form.template', required=True, ondelete='cascade', index=True)
    section_id = fields.Many2one('epo.form.section', ondelete='cascade', index=True)
    name = fields.Char(string='Label', required=True, default='Field')
    help_text = fields.Text(string='Help')
    field_type = fields.Selection(FIELD_TYPES, required=True, default='short_text')
    is_required = fields.Boolean(default=False)
    options_json = fields.Text(
        default='[]', help='JSON array of choices for dropdown / radio / checkboxes.')
    grid_rows_json = fields.Text(default='[]')
    grid_cols_json = fields.Text(default='[]')
    sequence = fields.Integer(default=10)
    value_count = fields.Integer(compute='_compute_value_count')

    _header_not_required = models.Constraint(
        "CHECK (field_type <> 'section_header' OR is_required = false)",
        'A section header is decoration — marking it required makes the form '
        'permanently unsubmittable.')

    def _compute_value_count(self):
        Value = self.env['epo.form.value'].sudo()
        for rec in self:
            rec.value_count = Value.search_count([('field_id', '=', rec.id)])

    def _json(self, raw):
        import json
        try:
            parsed = json.loads(raw or '[]')
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []

    @property
    def options(self):
        return self._json(self.options_json)

    @api.constrains('field_type', 'options_json', 'grid_rows_json', 'grid_cols_json')
    def _check_shape(self):
        """A dropdown with no options is an unfillable required field that silently
        blocks every submission. Catch it at build time, not at 2am."""
        for rec in self:
            if rec.field_type in CHOICE_TYPES and not rec._json(rec.options_json):
                raise ValidationError(_(
                    'Field "%(label)s" is a %(type)s and needs at least one option.',
                    label=rec.name, type=rec.field_type))
            if rec.field_type == 'grid' and not (
                    rec._json(rec.grid_rows_json) and rec._json(rec.grid_cols_json)):
                raise ValidationError(_(
                    'Grid field "%s" needs at least one row and one column.', rec.name))

    @api.constrains('section_id', 'template_id')
    def _check_section_template(self):
        for rec in self:
            if rec.section_id and rec.section_id.template_id != rec.template_id:
                raise ValidationError(_(
                    'Field "%s" is attached to a section of a different form.', rec.name))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.mapped('template_id')._assert_mutable()
        return records

    def write(self, vals):
        self.mapped('template_id')._assert_mutable()
        return super().write(vals)

    def unlink(self):
        self.mapped('template_id')._assert_mutable()
        for rec in self:
            if rec.value_count:
                raise UserError(_(
                    'Field "%(label)s" holds %(n)s answers and cannot be deleted.',
                    label=rec.name, n=rec.value_count))
        return super().unlink()

    def _to_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'label': self.name,
            'help': self.help_text or '',
            'field_type': self.field_type,
            'required': self.is_required,
            'options': self._json(self.options_json),
            'grid_rows': self._json(self.grid_rows_json),
            'grid_cols': self._json(self.grid_cols_json),
            'sequence': self.sequence,
        }
