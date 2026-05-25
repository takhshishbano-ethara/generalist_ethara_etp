# -*- coding: utf-8 -*-
import json
import re

from markupsafe import Markup

from odoo import api, models

FONT_STACKS = {
    "inter": "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    "roboto": "'Roboto', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "poppins": "'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "lato": "'Lato', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "system": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif",
}

FONT_URLS = {
    "inter": "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap",
    "roboto": "https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap",
    "poppins": "https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap",
    "lato": "https://fonts.googleapis.com/css2?family=Lato:wght@400;700&display=swap",
    "system": "",
}

TRANSITION_SPEEDS = {
    "none": ("0s", "0s"),
    "fast": ("0.1s", "0.07s"),
    "normal": ("0.18s", "0.12s"),
    "slow": ("0.32s", "0.22s"),
}

DENSITY = {
    "comfortable": "8px",
    "compact": "4px",
}

CARD_SHADOWS = {
    "none": "none",
    "subtle": "0 1px 3px rgba(16, 24, 40, .07)",
    "elevated": "0 6px 18px rgba(16, 24, 40, .12)",
}

LIGHT_PALETTE = {
    "border": "#e5e7eb",
    "hover": "#f3f4f6",
    "dot": "#d1d5db",
    "text": "#1f2937",
    "text_muted": "#6b7280",
    "text_faint": "#9ca3af",
    "shadow_lg": "0 12px 32px rgba(16, 24, 40, .16)",
}

DARK_PALETTE = {
    "bg": "#0f1117",
    "surface": "#181b23",
    "border": "#2a2e3a",
    "hover": "#242834",
    "dot": "#3a4050",
    "text": "#e6e8ee",
    "text_muted": "#9aa1b1",
    "text_faint": "#6b7280",
    "primary_soft": "#2c2b52",
    "active_fg": "#c7d2fe",
    "shadow_lg": "0 12px 32px rgba(0, 0, 0, .55)",
}

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class EtharaTheme(models.AbstractModel):
    _name = "ethara.theme"
    _description = "Ethara ETP Theme runtime settings helper"

    @api.model
    def _get_param(self, key, default):
        value = self.env["ir.config_parameter"].sudo().get_param(
            "ethara_etp_theme.%s" % key, default
        )
        return value if value not in (False, None, "") else default

    @api.model
    def _color(self, key, default):
        value = str(self._get_param(key, default)).strip()
        return value if _HEX_RE.match(value) else default

    @api.model
    def _size(self, key, default, minimum=1):
        try:
            value = int(self._get_param(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= minimum else default

    @api.model
    def _choice(self, key, default, allowed):
        value = self._get_param(key, default)
        return value if value in allowed else default

    @api.model
    def get_theme_head(self):
        """Dynamic <head> assets (CSS variables + webfont URL) built from
        the theme settings stored in ir.config_parameter."""
        font = self._choice("font", "inter", FONT_STACKS)
        transition = self._choice("transition", "normal", TRANSITION_SPEEDS)
        trans, trans_fast = TRANSITION_SPEEDS[transition]
        density = self._choice("density", "comfortable", DENSITY)
        shadow = CARD_SHADOWS[self._choice("card_shadow", "subtle", CARD_SHADOWS)]
        dark = self._choice("theme_mode", "light", ("light", "dark")) == "dark"
        accent = self._choice("sidebar_accent", "accent", ("accent", "plain"))
        sidebar_start = self._choice(
            "sidebar_default", "expanded", ("expanded", "collapsed")
        )
        icons_hidden = (
            self._choice("app_icons", "shown", ("shown", "hidden")) == "hidden"
        )
        input_style = self._choice(
            "input_style", "soft", ("outlined", "filled", "underline", "soft")
        )

        radius = self._size("border_radius", 10, minimum=0)
        primary = self._color("primary_color", "#6366f1")
        primary_dark = self._color("primary_dark_color", "#4f46e5")

        if dark:
            surface = DARK_PALETTE["surface"]
            bg = DARK_PALETTE["bg"]
            border = DARK_PALETTE["border"]
            hover = DARK_PALETTE["hover"]
            dot = DARK_PALETTE["dot"]
            text = DARK_PALETTE["text"]
            text_muted = DARK_PALETTE["text_muted"]
            text_faint = DARK_PALETTE["text_faint"]
            primary_soft = DARK_PALETTE["primary_soft"]
            accent_fg = DARK_PALETTE["active_fg"]
            shadow_lg = DARK_PALETTE["shadow_lg"]
        else:
            surface = self._color("sidebar_bg", "#ffffff")
            bg = self._color("content_bg", "#f6f7fb")
            border = LIGHT_PALETTE["border"]
            hover = LIGHT_PALETTE["hover"]
            dot = LIGHT_PALETTE["dot"]
            text = LIGHT_PALETTE["text"]
            text_muted = LIGHT_PALETTE["text_muted"]
            text_faint = LIGHT_PALETTE["text_faint"]
            primary_soft = self._color("primary_soft_color", "#eef2ff")
            accent_fg = primary_dark
            shadow_lg = LIGHT_PALETTE["shadow_lg"]

        if accent == "plain":
            active_bg = hover
            active_fg = text
        else:
            active_bg = primary_soft
            active_fg = accent_fg

        navbar_bg = self._color("navbar_bg", surface)

        css = (
            "html:root{"
            "--ethara-primary:%(primary)s;"
            "--ethara-primary-dark:%(primary_dark)s;"
            "--ethara-primary-soft:%(primary_soft)s;"
            "--ethara-active-bg:%(active_bg)s;"
            "--ethara-active-fg:%(active_fg)s;"
            "--ethara-bg:%(bg)s;"
            "--ethara-surface:%(surface)s;"
            "--ethara-sidebar-bg:%(navbar_bg)s;"
            "--ethara-border:%(border)s;"
            "--ethara-hover:%(hover)s;"
            "--ethara-dot:%(dot)s;"
            "--ethara-text:%(text)s;"
            "--ethara-text-muted:%(text_muted)s;"
            "--ethara-text-faint:%(text_faint)s;"
            "--ethara-sidebar-width:%(sidebar_width)spx;"
            "--ethara-navbar-height:%(navbar_height)spx;"
            "--ethara-sidebar-start:%(sidebar_start)s;"
            "--ethara-radius:%(radius)spx;"
            "--ethara-radius-lg:%(radius_lg)spx;"
            "--ethara-shadow:%(shadow)s;"
            "--ethara-shadow-lg:%(shadow_lg)s;"
            "--ethara-density-y:%(density)s;"
            "--ethara-transition:%(trans)s;"
            "--ethara-transition-fast:%(trans_fast)s;"
            "--ethara-font:%(font)s;"
            "--ethara-input-style:%(input_style)s;"
            "}"
        ) % {
            "primary": primary,
            "primary_dark": primary_dark,
            "primary_soft": primary_soft,
            "active_bg": active_bg,
            "active_fg": active_fg,
            "bg": bg,
            "surface": surface,
            "navbar_bg": navbar_bg,
            "border": border,
            "hover": hover,
            "dot": dot,
            "text": text,
            "text_muted": text_muted,
            "text_faint": text_faint,
            "sidebar_width": self._size("sidebar_width", 240),
            "navbar_height": self._size("navbar_height", 48),
            "sidebar_start": sidebar_start,
            "radius": radius,
            "radius_lg": radius + 2,
            "shadow": shadow,
            "shadow_lg": shadow_lg,
            "density": DENSITY[density],
            "trans": trans,
            "trans_fast": trans_fast,
            "font": FONT_STACKS[font],
            "input_style": input_style,
        }
        if icons_hidden:
            css += (
                "body:not(.o_ethara_sidebar_collapsed) "
                ".o_ethara_app_icon{display:none;}"
            )
        return {"css": Markup(css), "font_url": FONT_URLS[font]}

    @api.model
    def get_theme_init_script(self):
        """Return a small <script> body that propagates a few theme
        choices to the DOM as data-* attributes on <html>, so that
        purely-CSS rules can react before any OWL component mounts."""
        chatter_position = self._choice(
            "chatter_position", "sided", ("sided", "bottom")
        )
        return Markup(
            "document.documentElement.dataset.etharaChatter = '%s';"
            % chatter_position
        )

    @api.model
    def get_favorites_bg(self):
        params = self.env["ir.config_parameter"].sudo()
        url = params.get_param("ethara_etp_theme.favorites_bg_url") or ""
        if not url:
            return False
        mime = params.get_param("ethara_etp_theme.favorites_bg_mime") or ""
        return {"url": url, "mime": mime}

    @api.model
    def get_menu_icons_script(self):
        """Return a <script> body publishing the per-app sidebar icon
        overrides as ``window.etharaMenuIcons`` for navbar_patch.js."""
        icons = {}
        if "ethara.menu.icon" in self.env:
            records = self.env["ethara.menu.icon"].sudo().search([])
            for rec in records:
                if not rec.menu_id:
                    continue
                if rec.icon_type == "image" and rec.icon_image:
                    unique = int(rec.write_date.timestamp()) if rec.write_date else 0
                    icons[rec.menu_id.id] = {
                        "type": "image",
                        "src": "/web/image/ethara.menu.icon/%s/icon_image?unique=%s"
                        % (rec.id, unique),
                    }
                elif rec.icon_type == "font" and rec.icon_class:
                    icons[rec.menu_id.id] = {
                        "type": "font",
                        "iconClass": rec.icon_class.strip(),
                        "color": (rec.icon_color or "#ffffff").strip(),
                        "backgroundColor": (rec.icon_bg_color or "#6366f1").strip(),
                    }
        payload = json.dumps(icons).replace("<", "\\u003c")
        return Markup("window.etharaMenuIcons = %s;" % payload)
