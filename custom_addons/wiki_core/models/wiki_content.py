from odoo import models, fields


class WikiArticle(models.Model):
    """A Foundation-hub card that opens a rich, sectioned article.

    Drives both the Foundation hub cards (title/description/icon) and the
    article reader (sections). `route_key` lets a card jump to a special
    frontend route (e.g. the live Org Chart) instead of opening sections.
    """
    _name = 'wiki.article'
    _description = 'Wiki Article'
    _order = 'sequence, id'

    name = fields.Char(string='Title', required=True)
    slug = fields.Char(
        string='Slug', required=True,
        help='Stable key the app uses to open this article, e.g. "core_values".')
    description = fields.Char(string='Card Description')
    icon = fields.Char(string='Icon Key', help='UI icon identifier.')
    route_key = fields.Char(
        string='Route Key',
        help='If set, the card opens this frontend route (e.g. "org_chart") '
             'instead of the article body.')
    meta = fields.Char(string='Meta', help='e.g. "Updated 11 Jun 2026 · HR · v3".')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    section_ids = fields.One2many(
        'wiki.article.section', 'article_id', string='Sections')


class WikiArticleSection(models.Model):
    _name = 'wiki.article.section'
    _description = 'Wiki Article Section'
    _order = 'sequence, id'

    article_id = fields.Many2one(
        'wiki.article', string='Article', required=True, ondelete='cascade')
    heading = fields.Char(string='Heading', required=True)
    body = fields.Text(string='Body')
    sequence = fields.Integer(default=10)


class WikiProcessFlow(models.Model):
    """A documented process (e.g. the reimbursement approval flow) shown as an
    ordered chain of coloured stages in the app's Process Flow tab."""
    _name = 'wiki.process.flow'
    _description = 'Wiki Process Flow'
    _order = 'sequence, id'

    name = fields.Char(string='Title', required=True)
    meta = fields.Char(string='Meta', help='e.g. "Owner: Operations · v1.2".')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    stage_ids = fields.One2many(
        'wiki.process.stage', 'flow_id', string='Stages')


class WikiProcessStage(models.Model):
    _name = 'wiki.process.stage'
    _description = 'Wiki Process Stage'
    _order = 'sequence, id'

    flow_id = fields.Many2one(
        'wiki.process.flow', string='Flow', required=True, ondelete='cascade')
    name = fields.Char(string='Label', required=True)
    kind = fields.Selection(
        [('step', 'Step'), ('decision', 'Decision / review'),
         ('outcome', 'Outcome')],
        string='Kind', required=True, default='step')
    sequence = fields.Integer(default=10)
