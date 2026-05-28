from odoo import fields, models


class LifeDomain(models.Model):
    _name = "skoll_wildclaw.life_domain"
    _description = "Skoll Life Domain"
    _order = "name"
    name = fields.Char(required=True)
    code = fields.Char()
    description = fields.Text()


class ClusterTag(models.Model):
    _name = "skoll_wildclaw.cluster_tag"
    _description = "Skoll Cluster Tag"
    _order = "name"
    name = fields.Char(required=True)
    life_domain_id = fields.Many2one("skoll_wildclaw.life_domain")
    description = fields.Text()


class TaskTypeTag(models.Model):
    _name = "skoll_wildclaw.task_type_tag"
    _description = "Skoll Task-Type Tag"
    _order = "name"
    name = fields.Char(required=True)
    description = fields.Text()


class PatternTaxonomy(models.Model):
    _name = "skoll_wildclaw.pattern_taxonomy"
    _description = "Skoll Pattern Taxonomy"
    _order = "name"
    name = fields.Char(required=True)
    parent_id = fields.Many2one("skoll_wildclaw.pattern_taxonomy")
    child_ids = fields.One2many("skoll_wildclaw.pattern_taxonomy", "parent_id")
    description = fields.Text()
