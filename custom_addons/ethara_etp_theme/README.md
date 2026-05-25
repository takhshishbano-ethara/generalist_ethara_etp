# Ethara ETP Theme

A backend theme for **Odoo 19** that restyles the web client into the
**Ethara ETP** look & feel and replaces the top menu bar with a
**left-panel navigation** showing every app (menu) and its sub-menus.

## Features

- Permanent ~240px left sidebar (white, indigo accent)
- Apps render as collapsible groups; sub-menus nest underneath
- Active app highlighted with an indigo pill; current app auto-expands
- Collapsible sidebar (icon-only mode)
- Indigo / purple SaaS design language — white cards, light surfaces,
  rounded corners, Inter typeface

## Install

The module lives on the configured `addons_path`
(`deepak_addons/ethara_etp/custom_addons`). Update the Apps list and install
**Ethara ETP Theme**, or from the shell:

```bash
./odoo-bin -c etp.conf -u ethara_etp_theme
```

## How it works

- `static/src/navbar/navbar_patch.js` — patches the core `NavBar` OWL
  component with sidebar state (expanded groups, collapsed mode) and helpers.
- `static/src/navbar/navbar.xml` — `t-inherit` extends `web.NavBar` to inject
  the `<aside>` sidebar plus a recursive sub-menu renderer.
- `static/src/scss/*` — design tokens, sidebar styling, global restyle.

No new menus are created — the sidebar renders the existing Odoo menu tree
via the `menu` service.
