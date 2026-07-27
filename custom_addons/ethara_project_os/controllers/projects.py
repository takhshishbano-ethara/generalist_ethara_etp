"""GPM-facing API: the project, its content and its forms.

Everything here is the kickoff path — create a project, fill its knowledge folder, set
training, link the (externally authored) assessment, build the stagelist and the
feedback form, publish, go live.

Read endpoints are open to any role but scoped: a pod member sees the projects they are
allocated to and nothing else. Write endpoints need GPM or Admin.
"""

import base64

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

from .utils import need_int, need_str, payload, respond, route


def _project_dict(project, detailed=False):
    data = {
        'id': project.id,
        'name': project.name,
        'code': project.code or '',
        'project_type': project.ethara_project_type,
        'platform': project.platform or '',
        # `state` in this API has always meant the delivery lifecycle, so it keeps
        # meaning that. The registry's commercial state is exposed alongside it rather
        # than under the name an existing client already reads.
        'state': project.ethara_state,
                        'description': project.description or '',
        'gpm': project.gpm_id.display_name or '',
        'date_start': str(project.date_start or ''),
        'date_end': str(project.date or ''),
        'has_sop': project.has_sop,
        'has_stagelist': project.has_stagelist,
        'has_training': project.has_training,
        'has_assessment': project.has_assessment,
        'gate_blockers': project.gate_blockers or '',
        'min_assessment_score': project.min_assessment_score,
        'allocated_today': project.allocated_today,
        'onboarding_count': project.onboarding_count,
        'tasking_count': project.tasking_count,
        'submission_count': project.submission_count,
    }
    if detailed:
        knowledge = project.document_ids.filtered(
            lambda d: d.active and d.root == 'knowledge')
        grouped = {}
        for document in knowledge:
            grouped.setdefault(document.category or 'other', []).append(
                document.to_dict())
        data['knowledge'] = grouped
        data['folders'] = {
            'knowledge_id': project.knowledge_folder_id.id or None,
            'management_id': project.management_folder_id.id or None,
        }
        data['training'] = [{
            'id': t.id, 'name': t.name, 'mode': t.mode, 'url': t.url or '',
            'notes': t.notes or '', 'is_mandatory': t.is_mandatory,
            'scheduled_at': str(t.scheduled_at or ''),
            'trainer': t.trainer_id.display_name or '',
        } for t in project.training_ids.filtered('active')]
        assessment = project.ethara_assessment_id
        data['assessment'] = assessment.to_dict() if assessment else None
        data['templates'] = [{
            'id': t.id, 'form_type': t.form_type, 'name': t.name,
            'version': t.version, 'state': t.state, 'entry_count': t.entry_count,
        } for t in project.template_ids]
    return data


def _os_project(ctx, project_id):
    """A project this API is allowed to talk about, or an empty recordset.

    ``project.project`` is shared with Odoo's Project app and three other modules.
    A project outside this pipeline is not a 404 by accident — this API has no vocabulary for it, and answering with one would
    offer an Activate button on a record with no knowledge folder behind it.
    """
    project = ctx.env['project.project'].browse(project_id).exists()
    return project if project and project.is_project_os else project.browse()


def _folder_guard(ctx, folder):
    """Management is commercial paperwork: contracts, briefs, sign-offs. A pod member
    or a pod lead has no business in it, so the answer is a plain 403 rather than a
    filtered list that quietly hides half the folder."""
    if not folder:
        return respond(message='No such folder.', status=404)
    if folder.root == 'management' and not ctx.is_manager:
        return respond(
            message='Management documents are visible to GPM and Admin only.',
            status=403)
    try:
        ctx.assert_can_see_project(folder.project_id.id)
    except AccessError as exc:
        return respond(message=str(exc), status=403)
    return None


class ProjectOsProjects(http.Controller):

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------
    @route('/api/project-os/me', ['GET'])
    def me(self, ctx):
        """Who am I, what am I on today, and what may I do."""
        employee = ctx.employee
        today = ctx.today()
        roster = ctx.env['epo.roster.day'].sudo().search([
            ('employee_id', '=', employee.id), ('business_date', '=', today)], limit=1)
        allocation = employee.epo_current_allocation_id
        return respond({
            'user': {
                'id': ctx.user.id, 'name': ctx.user.name, 'email': ctx.user.login,
                'role': ctx.role,
            },
            'employee': {
                'id': employee.id, 'name': employee.display_name,
                'work_email': employee.work_email or '',
                'pod': employee.epo_pod_id.display_name or '',
                'pod_lead': employee.epo_pod_lead_id.display_name or '',
                'seat': employee.epo_seat_no or '',
                'manager': employee.parent_id.display_name or '',
                'engagement': employee.epo_job_status or '',
            } if employee else None,
            # What a project picker should be driven from. Rostering or submitting
            # against anything else is refused further down, so offering the full
            # project list is how a screen sets somebody up to fail.
            'allowed_projects': [
                {'id': p.id, 'name': p.name, 'code': p.code or ''}
                for p in employee.epo_allowed_project_ids
            ] if employee else [],
            'today': {
                'business_date': str(today),
                'tasking_status': roster.tasking_status or None,
                'present': roster.present,
                'project_id': roster.project_id.id or None,
                'project_name': roster.project_id.name or '',
                'issue': roster.issue or '',
            },
            'allocation': {
                'id': allocation.id, 'project_id': allocation.project_id.id,
                'project_name': allocation.project_id.name,
                'since': str(allocation.date_from),
                'current_phase': allocation.current_phase or '',
                'days_total': allocation.days_total,
            } if allocation else None,
        })

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------
    @route('/api/project-os/projects', ['GET'])
    def list_projects(self, ctx):
        # The registry is shared with the budget side; this API only ever serves the
        # projects that run through the Project OS pipeline.
        domain = [('is_project_os', '=', True)]
        if not ctx.is_manager:
            # A pod member or lead sees live projects they are attached to. Setup-state
            # projects are a GPM's workbench, not a public list.
            domain += [('id', 'in', ctx.accessible_project_ids),
                       ('ethara_state', '=', 'active')]
        projects = ctx.env['project.project'].search(domain)
        return respond([_project_dict(p) for p in projects])

    @route('/api/project-os/projects/<int:project_id>', ['GET'])
    def get_project(self, ctx, project_id):
        ctx.assert_can_see_project(project_id)
        project = _os_project(ctx, project_id)
        if not project:
            return respond(message='No such project.', status=404)
        return respond(_project_dict(project, detailed=True))

    @route('/api/project-os/projects', ['POST'], min_role='gpm')
    def create_project(self, ctx):
        body = payload()
        project = ctx.env['project.project'].create({
            # The registry is shared. Without this flag the row is a budget-side
            # project: no folder skeleton, no gate, not in this API's listings.
            'is_project_os': True,
            'name': need_str(body, 'name', 'A project name'),
            'code': body.get('code') or False,
            'ethara_project_type': body.get('project_type') or 'external',
            'platform': body.get('platform') or 'Multimango',
            'description': body.get('description') or False,
            'gpm_id': body.get('gpm_id') or ctx.employee.id,
            'min_assessment_score': body.get('min_assessment_score', 0.0),
            'date_start': body.get('date_start') or False,
            'date': body.get('date_end') or False,
        })
        return respond(_project_dict(project, detailed=True), message='Project created.')

    @route('/api/project-os/projects/<int:project_id>', ['PATCH', 'PUT'], min_role='gpm')
    def update_project(self, ctx, project_id):
        project = _os_project(ctx, project_id)
        if not project:
            return respond(message='No such project.', status=404)
        body = payload()
        # Keyed by the name the API has always used, mapped to the registry's column.
        writable = {
            'name': 'name', 'code': 'code',
            'project_type': 'ethara_project_type', 'platform': 'platform',
            'description': 'description', 'gpm_id': 'gpm_id',
            'date_start': 'date_start', 'date_end': 'date',
            'min_assessment_score': 'min_assessment_score',
        }
        project.write({writable[k]: v for k, v in body.items() if k in writable})
        return respond(_project_dict(project, detailed=True), message='Saved.')

    @route('/api/project-os/projects/<int:project_id>/activate', ['POST'], min_role='gpm')
    def activate_project(self, ctx, project_id):
        project = _os_project(ctx, project_id)
        if not project:
            return respond(message='No such project.', status=404)
        project.action_activate()
        return respond(_project_dict(project), message='Project is live.')

    @route('/api/project-os/projects/<int:project_id>/archive', ['POST'], min_role='gpm')
    def archive_project(self, ctx, project_id):
        project = _os_project(ctx, project_id)
        if not project:
            return respond(message='No such project.', status=404)
        project.action_archive_project()
        return respond(_project_dict(project), message='Project archived.')

    # ------------------------------------------------------------------
    # folders — the project's filing cabinet
    # ------------------------------------------------------------------
    @route('/api/project-os/projects/<int:project_id>/folders', ['GET'])
    def folder_tree(self, ctx, project_id):
        """The whole cabinet, nested, in one call.

        A file browser needs the shape before it can draw anything, and one round trip
        per folder would make it crawl. Management folders are omitted entirely for a
        pod member or a lead — showing a folder that errors when opened is worse than
        not showing it.
        """
        ctx.assert_can_see_project(project_id)
        project = _os_project(ctx, project_id)
        if not project:
            return respond(message='No such project.', status=404)
        with_docs = request.params.get('with_documents', '1') in ('1', 'true', 'True')
        return respond({
            'project_id': project.id,
            'project_code': project.code or '',
            'tree': ctx.env['epo.folder'].tree_for(
                project, user=ctx.user, with_documents=with_docs),
        })

    @route('/api/project-os/projects/<int:project_id>/folders', ['POST'], min_role='gpm')
    def create_folder(self, ctx, project_id):
        """Add a subfolder. Management nests freely; Knowledge is a fixed shape, so the
        model refuses a new folder there and says why."""
        body = payload()
        name = need_str(body, 'name', 'A folder name')
        parent = ctx.env['epo.folder'].browse(
            need_int(body, 'parent_id', 'The parent folder')).exists()
        if not parent:
            return respond(message='No such parent folder.', status=404)
        if parent.project_id.id != project_id:
            return respond(message='That folder belongs to another project.', status=400)
        folder = ctx.env['epo.folder'].create({
            'project_id': project_id,
            'parent_id': parent.id,
            'root': parent.root,
            'name': name,
            'description': body.get('description') or False,
        })
        return respond(folder.to_dict(), message='Folder created.')

    @route('/api/project-os/folders/<int:folder_id>', ['GET'])
    def get_folder(self, ctx, folder_id):
        folder = ctx.env['epo.folder'].browse(folder_id).exists()
        if not folder:
            return respond(message='No such folder.', status=404)
        error = _folder_guard(ctx, folder)
        if error:
            return error
        return respond({
            **folder.to_dict(with_documents=True),
            'children': [c.to_dict() for c in folder.child_ids.filtered('active')],
        })

    @route('/api/project-os/folders/<int:folder_id>', ['PATCH'], min_role='gpm')
    def update_folder(self, ctx, folder_id):
        folder = ctx.env['epo.folder'].browse(folder_id).exists()
        if not folder:
            return respond(message='No such folder.', status=404)
        body = payload()
        vals = {}
        if 'name' in body:
            vals['name'] = body['name']
        if 'description' in body:
            vals['description'] = body['description']
        if 'parent_id' in body:
            vals['parent_id'] = need_int(body, 'parent_id')
        folder.write(vals)
        return respond(folder.to_dict(), message='Saved.')

    @route('/api/project-os/folders/<int:folder_id>', ['DELETE'], min_role='gpm')
    def delete_folder(self, ctx, folder_id):
        folder = ctx.env['epo.folder'].browse(folder_id).exists()
        if not folder:
            return respond(message='No such folder.', status=404)
        folder.unlink()
        return respond(message='Folder deleted.')

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    @route('/api/project-os/folders/<int:folder_id>/documents', ['GET'])
    def list_documents(self, ctx, folder_id):
        folder = ctx.env['epo.folder'].browse(folder_id).exists()
        if not folder:
            return respond(message='No such folder.', status=404)
        error = _folder_guard(ctx, folder)
        if error:
            return error
        return respond([d.to_dict() for d in folder.document_ids.filtered('active')])

    @route('/api/project-os/folders/<int:folder_id>/documents', ['POST'], min_role='gpm')
    def add_document(self, ctx, folder_id):
        """Upload a file or record a link.

        Takes a multipart upload, a base64 blob or a URL — whichever the client finds
        easier. Bytes go to S3 when a bucket is configured and to an Odoo attachment
        otherwise; either way they are served through an authorised endpoint, never
        from a public path.
        """
        folder = ctx.env['epo.folder'].browse(folder_id).exists()
        if not folder:
            return respond(message='No such folder.', status=404)
        files = request.httprequest.files
        body = payload() if not files else dict(request.httprequest.form)
        vals = {
            'folder_id': folder.id,
            'name': body.get('name') or '',
            'description': body.get('description') or False,
            'is_mandatory': str(body.get('is_mandatory', '')).lower() in ('1', 'true'),
        }
        upload = files.get('file') if files else None
        if upload:
            vals.update({
                'doc_type': 'file',
                'file_data': base64.b64encode(upload.read()),
                'file_name': upload.filename,
                'name': vals['name'] or upload.filename,
            })
        elif body.get('file_data'):
            vals.update({
                'doc_type': 'file',
                'file_data': body['file_data'],
                'file_name': body.get('file_name') or vals['name'],
                'name': vals['name'] or body.get('file_name') or 'File',
            })
        elif body.get('url'):
            vals.update({'doc_type': 'link', 'url': body['url'],
                         'name': vals['name'] or body['url']})
        else:
            return respond(message='Provide a file or a link URL.', status=400)
        document = ctx.env['epo.document'].create(vals)
        return respond(document.to_dict(), message='Uploaded.')

    @route('/api/project-os/documents/<int:document_id>/download', ['GET'])
    def download_document(self, ctx, document_id):
        """Mint a short-lived link to the bytes, after checking the caller may read it.

        Returns the URL rather than redirecting so a browser client can decide whether
        to open a tab or fetch it; pass ``?redirect=1`` to get a 302 instead."""
        document = ctx.env['epo.document'].browse(document_id).exists()
        if not document:
            return respond(message='No such document.', status=404)
        error = _folder_guard(ctx, document.folder_id)
        if error:
            return error
        target = document.sudo().download_target()
        if request.params.get('redirect') in ('1', 'true', 'True'):
            return request.redirect(target['url'], local=False)
        return respond(target)

    @route('/api/project-os/documents/<int:document_id>', ['DELETE'], min_role='gpm')
    def delete_document(self, ctx, document_id):
        document = ctx.env['epo.document'].browse(document_id).exists()
        if not document:
            return respond(message='No such document.', status=404)
        document.unlink()
        return respond(message='Removed.')

    @route('/api/project-os/projects/<int:project_id>/knowledge', ['GET'])
    def list_knowledge(self, ctx, project_id):
        """Flat view of the Knowledge side, grouped by folder.

        The onboarding screen wants "the SOP" and "the common errors", not a tree.
        """
        ctx.assert_can_see_project(project_id)
        documents = ctx.env['epo.document'].search([
            ('project_id', '=', project_id), ('root', '=', 'knowledge'),
            ('active', '=', True)])
        grouped = {}
        for document in documents:
            grouped.setdefault(document.category or 'other', []).append(
                document.to_dict())
        return respond(grouped)

    # ------------------------------------------------------------------
    # training
    # ------------------------------------------------------------------
    @route('/api/project-os/projects/<int:project_id>/training', ['GET'])
    def list_training(self, ctx, project_id):
        ctx.assert_can_see_project(project_id)
        sessions = ctx.env['epo.training'].search([
            ('project_id', '=', project_id), ('active', '=', True)])
        return respond([{
            'id': t.id, 'name': t.name, 'mode': t.mode, 'url': t.url or '',
            'notes': t.notes or '', 'is_mandatory': t.is_mandatory,
            'scheduled_at': str(t.scheduled_at or ''),
            'duration_mins': t.duration_mins,
            'trainer': t.trainer_id.display_name or '',
        } for t in sessions])

    @route('/api/project-os/projects/<int:project_id>/training', ['POST'], min_role='gpm')
    def add_training(self, ctx, project_id):
        body = payload()
        session = ctx.env['epo.training'].create({
            'project_id': project_id,
            'name': body.get('name') or 'Training session',
            'mode': body.get('mode') or 'recorded',
            'url': body.get('url') or False,
            'notes': body.get('notes') or False,
            'scheduled_at': body.get('scheduled_at') or False,
            'duration_mins': body.get('duration_mins') or 0,
            'trainer_id': body.get('trainer_id') or False,
            'is_mandatory': body.get('is_mandatory', True),
        })
        return respond({'id': session.id}, message='Training saved.')

    @route('/api/project-os/training/<int:training_id>', ['DELETE'], min_role='gpm')
    def remove_training(self, ctx, training_id):
        session = ctx.env['epo.training'].browse(training_id).exists()
        if not session:
            return respond(message='No such session.', status=404)
        session.active = False
        return respond(message='Training removed.')

    # ------------------------------------------------------------------
    # assessment — linked, not built
    # ------------------------------------------------------------------
    @route('/api/project-os/projects/<int:project_id>/assessment', ['POST'], min_role='gpm')
    def link_assessment(self, ctx, project_id):
        """Point the project at the external application where the paper lives.

        v2 §4.6.2: a link and nothing else. No questions are imported, no attempts are
        stored, and nothing is graded here — a PM reads the result over there and
        verifies it on the tasker's onboarding record.
        """
        body = payload()
        if not body.get('url'):
            return respond(
                message='Provide the url of the external assessment.', status=400)
        existing = ctx.env['ethara.assessment'].search([
            ('project_id', '=', project_id), ('active', '=', True)])
        existing.write({'active': False})
        record = ctx.env['ethara.assessment'].create({
            'project_id': project_id,
            'title': body.get('title') or 'External Assessment',
            'url': body['url'],
            'provider': body.get('provider') or '',
            'pass_score': body.get('pass_score', 60),
            'notes': body.get('notes') or '',
            'is_mandatory': body.get('is_mandatory', True),
        })
        return respond(record.to_dict(), message='Assessment linked.')





    @route('/api/project-os/projects/<int:project_id>/templates', ['GET'])
    def list_templates(self, ctx, project_id):
        ctx.assert_can_see_project(project_id)
        templates = ctx.env['epo.form.template'].search([('project_id', '=', project_id)])
        return respond([{
            'id': t.id, 'form_type': t.form_type, 'name': t.name,
            'version': t.version, 'state': t.state, 'entry_count': t.entry_count,
            'published_at': str(t.published_at or ''),
        } for t in templates])

    @route('/api/project-os/templates/<int:template_id>', ['GET'])
    def get_template(self, ctx, template_id):
        template = ctx.env['epo.form.template'].browse(template_id).exists()
        if not template:
            return respond(message='No such form.', status=404)
        ctx.assert_can_see_project(template.project_id.id)
        return respond(template.schema())

    @route('/api/project-os/templates', ['POST'], min_role='gpm')
    def create_template(self, ctx):
        body = payload()
        template = ctx.env['epo.form.template'].create({
            'project_id': need_int(body, 'project_id'),
            'form_type': body.get('form_type') or 'stagelist',
            'name': body.get('name') or 'Untitled form',
            'description': body.get('description') or False,
        })
        ctx.env['epo.form.section'].create({
            'template_id': template.id, 'name': 'Section 1', 'sequence': 10})
        return respond(template.schema(), message='Draft created.')

    @route('/api/project-os/templates/<int:template_id>', ['PUT'], min_role='gpm')
    def save_template(self, ctx, template_id):
        """Replace the structure of a DRAFT form.

        This is the operation that destroyed every submitted answer in the prototype.
        Here it refuses outright on a published form — call new-version first, which
        clones it and leaves the old version (and its answers) untouched."""
        template = ctx.env['epo.form.template'].browse(template_id).exists()
        if not template:
            return respond(message='No such form.', status=404)
        if template.state != 'draft':
            return respond(
                message='That form is published. Create a new version to change it.',
                status=409)
        body = payload()
        template.write({
            'name': body.get('name') or template.name,
            'description': body.get('description') or False,
        })
        template.section_ids.unlink()
        import json as _json
        seq_section = 10
        for section_body in body.get('sections') or []:
            section = ctx.env['epo.form.section'].create({
                'template_id': template.id,
                'name': section_body.get('title') or 'Section',
                'help_text': section_body.get('help') or False,
                'sequence': seq_section,
            })
            seq_section += 10
            seq_field = 10
            for field_body in section_body.get('fields') or []:
                ctx.env['epo.form.field'].create({
                    'template_id': template.id,
                    'section_id': section.id,
                    'name': field_body.get('label') or 'Field',
                    'help_text': field_body.get('help') or False,
                    'field_type': field_body.get('field_type') or 'short_text',
                    'is_required': bool(field_body.get('required')),
                    'options_json': _json.dumps(field_body.get('options') or []),
                    'grid_rows_json': _json.dumps(field_body.get('grid_rows') or []),
                    'grid_cols_json': _json.dumps(field_body.get('grid_cols') or []),
                    'sequence': seq_field,
                })
                seq_field += 10
        return respond(template.schema(), message='Saved.')

    @route('/api/project-os/templates/<int:template_id>/publish', ['POST'], min_role='gpm')
    def publish_template(self, ctx, template_id):
        template = ctx.env['epo.form.template'].browse(template_id).exists()
        if not template:
            return respond(message='No such form.', status=404)
        template.action_publish()
        return respond({
            **template.schema(),
            'project_state': template.project_id.ethara_state,
        }, message='Published.')

    @route('/api/project-os/templates/<int:template_id>/new-version', ['POST'],
           min_role='gpm')
    def new_version(self, ctx, template_id):
        template = ctx.env['epo.form.template'].browse(template_id).exists()
        if not template:
            return respond(message='No such form.', status=404)
        action = template.action_new_version()
        clone = ctx.env['epo.form.template'].browse(action['res_id'])
        return respond(clone.schema(), message='New draft version created.')
