# -*- coding: utf-8 -*-
from odoo import fields, models


class SkollTagLifeDomain(models.Model):
    _name = "skoll.tag.life_domain"
    _description = "Life Domain Tag"
    _order = "name"

    name = fields.Char(required=True, index=True, unique=True)


class SkollTagCluster(models.Model):
    _name = "skoll.tag.cluster"
    _description = "Cluster Tag"
    _order = "name"

    name = fields.Char(required=True, index=True, unique=True)


class SkollTagTaskType(models.Model):
    _name = "skoll.tag.task_type"
    _description = "Task Type Tag"
    _order = "name"

    name = fields.Char(required=True, index=True, unique=True)


class SkollTagPatternTaxonomy(models.Model):
    _name = "skoll.tag.pattern_taxonomy"
    _description = "Pattern Taxonomy Tag"
    _order = "name"

    name = fields.Char(required=True, index=True, unique=True)
