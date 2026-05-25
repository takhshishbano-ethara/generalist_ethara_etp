# Ethara ETP Theme

A complete backend theme for **Odoo 19** that transforms the web client
into the **Ethara ETP** look & feel.

The top menu bar is replaced by a permanent **left-panel navigation**
that lists every application and its sub-menus, with collapsible groups,
favorites, an in-sidebar menu search and a dark-mode toggle. The whole
visual language — colors, font, spacing, corner radius, shadows,
animation speed, input style, login page layout — is driven by a single
**Settings > Ethara Theme** panel; no Python or SCSS edits are needed to
re-skin the backend.

* **Name**: Ethara ETP Theme
* **Version**: `19.0.1.0.0`
* **Category**: Themes / Backend
* **License**: LGPL-3
* **Depends**: `web`, `base_setup`
* **Author**: Deepak Kumar
* **Portfolio**: https://deepak-portfolio-livid.vercel.app/
* **GitHub**: https://github.com/deepak325251

---

## Table of Contents

1. [Highlights](#highlights)
2. [Installation](#installation)
3. [Settings Panel](#settings-panel)
   - [Appearance](#appearance)
   - [Colors](#colors)
   - [Typography & Motion](#typography--motion)
   - [Layout](#layout)
   - [Application Icons](#application-icons)
   - [Chatter](#chatter)
   - [Login Page](#login-page)
4. [Sidebar Navigation](#sidebar-navigation)
5. [Quick Action Toolbar](#quick-action-toolbar)
6. [Responsive Layout (Tablet & Mobile)](#responsive-layout-tablet--mobile)
7. [Favorites & Home Page](#favorites--home-page)
8. [Custom Application Icons](#custom-application-icons)
9. [Chatter Position & Resize](#chatter-position--resize)
10. [List View Column Filters](#list-view-column-filters)
11. [Login Page Templates](#login-page-templates)
12. [Light & Dark Themes](#light--dark-themes)
13. [Models Added by This Module](#models-added-by-this-module)
14. [Technical Architecture](#technical-architecture)
15. [File Structure](#file-structure)
16. [How to Reset to Defaults](#how-to-reset-to-defaults)

---

## Highlights

* Permanent left sidebar that lists every Odoo application and its
  sub-menus, with collapsible groups and active-item highlighting.
* Collapsible (icon-only) sidebar mode, with the chosen state persisted
  across page loads via a CSS variable.
* In-sidebar menu search — type to filter across every app, jump to a
  result with breadcrumbs.
* Per-user favorites: pin any menu and re-open it from the sidebar or
  from a dedicated **Home / Favorites** page.
* **Profile Card with Quick Actions** in the sidebar — user avatar,
  name and time-of-day greeting, plus one-click shortcuts for language,
  company, debug, theme settings, sidebar pin, dark mode, conversations
  and activities (with live unread / pending badges and toast feedback).
  Dark-mode preference is persisted in the browser.
* **Full-height workspace.** The entire top Odoo navbar (app launcher,
  menu, breadcrumb bar, systray) is hidden; the content area starts
  from the very top of the viewport, beside the sidebar. All navigation
  happens through the sidebar profile card and menu tree.
* **Responsive layout.** Sidebar shrinks on tablets and turns into a
  slide-in drawer with floating hamburger toggle + backdrop on phones,
  so the theme is fully usable from any device.
* Four selectable login page templates (Centered Card, Split Screen,
  Full-Screen Background, Minimal) with a **live preview** inside the
  Settings panel.
* Per-application icon overrides — FontAwesome glyph or uploaded image —
  without touching native Odoo menus.
* **Chatter Position switch** — globally render the chatter on the side
  (Odoo default responsive layout) or always at the bottom of every form.
* **Resizable Chatter Panel** — drag the left edge of the side chatter to
  resize, double-click to reset. Width is remembered per browser.
* **Per-column filter row** on every list view — type a value under any
  column header to filter the records server-side; selection, boolean,
  date and numeric columns get smart widgets.
* Single **Settings > Ethara Theme** panel with eight blocks controlling
  the design tokens that drive the entire backend through CSS variables.
* Inter / Roboto / Poppins / Lato webfonts are auto-loaded from Google
  Fonts when selected; a System Default option uses native fonts only.
* Indigo / purple SaaS design language with white cards, light surfaces,
  rounded corners and four selectable input styles.

The sidebar renders the existing Odoo menu tree through the `menu`
service — **no new menus are created**.

---

## Installation

The module lives on the configured `addons_path` (in this repository:
`deepak_addons/custom_addons`). Update the Apps list and install
**Ethara ETP Theme**, or from the shell:

```bash
./odoo-bin -c etp.conf -u ethara_etp_theme
```

After installation, open **Settings > Ethara Theme** to customise the
look, then save. The settings take effect on the next page load
(reload the browser with a hard refresh the first time so the asset
bundle is rebuilt).

---

## Settings Panel

All theme settings live in **Settings > Ethara Theme** and are stored
as `ir.config_parameter` records under the `ethara_etp_theme.*`
namespace. Every change emits new CSS variables on the `<html>` element
the next time the page is rendered.

### Appearance

| Field | Default | Description |
| --- | --- | --- |
| **Theme Mode** | `Light` | Switch the entire backend between the light and dark palette. |
| **Border Radius** | `10 px` | Corner roundness of cards, buttons, inputs and menu items. Drives both `--ethara-radius` and `--ethara-radius-lg`. |
| **Density** | `Comfortable` | Vertical spacing in the sidebar navigation. `Comfortable` = 8 px, `Compact` = 4 px. |
| **Card Shadow** | `Subtle` | Elevation of cards, kanban records and form sheets. `None`, `Subtle` or `Elevated`. |
| **Input Style** | `Soft` | Visual style of form input fields. One of `Outlined`, `Filled`, `Underline`, `Soft`. The chosen value is exposed as the `--ethara-input-style` CSS variable and applied to `<html>` as `o_ethara_input_<style>`. |

### Colors

All five fields use the standard Odoo `color` widget and accept any
3, 6 or 8-character HEX value. Invalid HEX strings silently fall back
to the default.

| Field | Default | Description |
| --- | --- | --- |
| **Primary Color** | `#6366f1` | Main accent — primary buttons, active items, indicator dots. |
| **Hover / Active Shade** | `#4f46e5` | Darker accent used on hover and pressed states. |
| **Soft Highlight** | `#eef2ff` | Tinted background behind the active menu items. |
| **Sidebar & Card Background** | `#ffffff` | Background of the sidebar, top bar and cards. *Ignored in dark mode.* |
| **Content Background** | `#f6f7fb` | Background of the main content area. *Ignored in dark mode.* |

In dark mode the sidebar, surface, content background, border, hover
and text colors are taken from a hard-coded dark palette
(`#0f1117` / `#181b23` / `#2a2e3a` ...). Only the primary color and
hover shade still apply.

### Typography & Motion

| Field | Default | Description |
| --- | --- | --- |
| **Font Family** | `Inter` | Typeface used across the entire backend. Choices: `Inter`, `Roboto`, `Poppins`, `Lato`, `System Default`. The corresponding Google Fonts stylesheet is injected automatically in `<head>`; `System Default` injects no webfont. |
| **Transition Speed** | `Normal` | Speed of sidebar and panel animations. `Off`, `Fast` (0.10 s), `Normal` (0.18 s) or `Slow` (0.32 s). `Off` disables theme animations entirely. |

### Layout

| Field | Default | Description |
| --- | --- | --- |
| **Sidebar Width** | `240 px` | Width of the expanded left sidebar. |
| **Top Bar Height** | `48 px` | Height of the top navigation bar. |
| **App Icons** | `Shown` | Show or hide application icons in the sidebar. `Hidden` hides them only while the sidebar is expanded; they remain visible in collapsed mode for navigation. |
| **Sidebar Default State** | `Expanded` | Whether the sidebar starts expanded or collapsed on each page load. Stored as the `--ethara-sidebar-start` CSS variable. |
| **Active Item Style** | `Colored` | `Colored` paints the active menu item with the soft primary background; `Plain` uses a neutral hover background and the standard text color. |

### Application Icons

A single shortcut button that opens the **Custom Menu Icons** list
where you can override the sidebar icon of each application. See
[Custom Application Icons](#custom-application-icons) below.

### Chatter

| Field | Default | Description |
| --- | --- | --- |
| **Chatter Position** | `Side` | Where the chatter renders on every form view. `Side` uses Odoo's native responsive layout (chatter on the side on XXL screens, at the bottom on smaller viewports). `Bottom` forces the chatter to always render below the form, regardless of screen size. |
| **Resizable Width** | — | Informational. When the chatter is shown on the side, a thin handle appears on its left edge — drag it to resize, double-click to reset. The chosen width is remembered per browser. |

### Login Page

| Field | Default | Description |
| --- | --- | --- |
| **Login Template** | `Centered Card` | Picks one of the four login layouts (see [Login Page Templates](#login-page-templates)). Rendered with a custom **picker** widget that shows a thumbnail for each layout. |
| **Side / Background Image** | *empty* | Image used by the **Split Screen** and **Full-Screen Background** templates. A landscape image at least 1200 px wide works best. Stored as an `ir.attachment` and exposed via `/web/image/<id>`. |
| **Live Preview** | — | A read-only widget that re-renders a miniature of the selected template (with the uploaded image) any time you change either field. The preview updates instantly; **Save** is still required to apply the change to the real login page. |

---

## Sidebar Navigation

The sidebar is built by patching the core `web.NavBar` OWL component
(`static/src/navbar/navbar_patch.js`, ~243 lines) and extending its
template with `static/src/navbar/navbar.xml`. It always renders the
**existing** Odoo menu tree via the `menu` service.

The sidebar contains, from top to bottom:

* **Brand block** — Odoo company logo and name.
* **Menu search** — a text input that walks `menuService.getApps()` and
  matches on `name.toLowerCase().includes(query)`. Up to 30 results are
  shown with their full breadcrumb path; clicking a result opens the
  menu via `actionService.doAction`.
* **Home row** — opens the **Favorites Home** client action
  (`ethara_etp_theme.ethara_favorites_home_action`).
* **Favorites group** — every menu the current user has pinned, with a
  star icon that toggles the favorite state via `ethara.menu.favorite`.
* **Main app list** — every top-level Odoo application, with a
  collapsible header. Sub-menus nest underneath; the currently-open app
  is auto-expanded. Each entry is rendered with its app icon (custom
  override or default) and supports the `App Icons = Hidden` setting.
* **Footer** — current user avatar, name and role, a **dark mode**
  toggle (persisted in `localStorage.ethara_dark_mode`) and a
  **collapse** toggle (drives the `--ethara-sidebar-start` CSS var and
  the `o_ethara_sidebar_collapsed` class on `<body>`).

The sidebar state is held in a reactive `etharaState` `useState` object
with the following keys:

```text
collapsed       — boolean, sidebar in icon-only mode
expandedAppId   — id of the currently-expanded app group
expandedNodes   — ids of sub-menu nodes expanded inside that app
activeMenuId    — id of the currently-active menu
searchQuery     — current value of the menu search input
darkMode        — boolean, mirrored to localStorage
favoriteIds     — list of menu ids pinned by the current user
```

`applyInputStyle()` reads the `--ethara-input-style` CSS variable and
toggles the `o_ethara_input_<style>` class on `<html>` whenever the
theme settings change.

---

## Quick Action Toolbar

Directly below the brand block, the sidebar shows a **profile card**
that combines the current user's identity with a row of one-click
shortcuts. The card has three regions:

1. **Profile header** — circular avatar (`/web/image/res.users/<id>/avatar_128`),
   user name, and a time-of-day greeting ("Good Morning" / "Good
   Afternoon" / "Good Evening", computed from `new Date().getHours()`).
   A soft radial halo painted with `--ethara-primary-soft` sits behind
   the avatar.
2. **Main actions row** — six icons: Language, Company, Debug, Theme
   Settings, Sidebar Pin/Unpin, Dark/Light Mode.
3. **Badged actions row** — two icons with live counters: Conversations
   and Activities.

The card itself uses a gradient from `--ethara-active-bg` to
`--ethara-surface`, a `--ethara-border` outline and the theme
`--ethara-shadow` — so its colour always matches the active palette
(light or dark) and the configured primary tint.

| Icon | Action | Behaviour |
| --- | --- | --- |
| `fa fa-language` | **Language** | Opens the current user's preferences dialog (`res.users` form, `target="new"`) so the language can be changed. Notifies with the active language. |
| `fa fa-building-o` | **Company** | Opens the same preferences dialog focused on the company picker. Notifies with the active company name. |
| `fa fa-code` | **Debug** | Toggles the `?debug=1` URL parameter and reloads the page. The button shows an `o_ethara_quick_btn_active` accent when debug mode is on. |
| `fa fa-cog` | **Theme Settings** | Opens **Settings → Ethara Theme** inline via `res.config.settings`. |
| `fa fa-thumb-tack` / `fa fa-lock` | **Sidebar Pin / Unpin** | Collapses or expands the sidebar. Icon and title swap based on `etharaState.collapsed`. |
| `fa fa-sun-o` / `fa fa-lightbulb-o` | **Dark / Light Mode** | Same toggle as the footer button; icon and title swap based on `etharaState.darkMode`. Persisted in `localStorage.ethara_dark_mode`. |
| `fa fa-comments-o` | **Conversations** | Opens **Discuss** (`mail.action_discuss`). A primary-coloured badge shows the count of unread mail notifications for the current user. |
| `fa fa-clock-o` | **Activities** | Opens **My Activities** (list / form of `mail.activity` filtered by the current user). A primary-coloured badge shows the count of pending activities. |

**Live badges.** When the navbar mounts, `loadQuickBadges()` issues two
`searchCount` RPCs (one on `mail.activity`, one on
`mail.notification`) and writes the results into the reactive
`etharaState`. The badge spans only render when the count is `> 0`.

**Toast feedback.** Every button calls `etharaNotify(message)`, a thin
wrapper around `this.notification.add(message, { type: "success" })`.
The `toggleSidebar()` and `toggleDarkMode()` methods also fire a toast
so the user always sees feedback when a setting changes.

**Top navbar hidden, content starts at the top.** Because the profile
card already surfaces the user's identity and all the common
shortcuts, the entire top Odoo bar (`.o_main_navbar` — app menu,
breadcrumbs, systray) is hidden via a CSS rule in `sidebar.scss`. The
content area (`.o_action_manager`) is then pulled up to `top: 0` so
form views, list views and breadcrumb bars start right at the top of
the viewport, beside the sidebar. The sidebar footer (avatar pill,
dark-mode and collapse buttons) has been removed since the profile
card replaces it.

**Collapsed sidebar.** The profile card is hidden entirely when the
sidebar is in icon-only mode — when `body.o_ethara_sidebar_collapsed`
is set, `.o_ethara_profile_card { display: none; }` ensures the card
does not overflow the narrow rail.

The card markup lives in `static/src/navbar/navbar.xml` (inserted via
`t-inherit` right after the brand block), all behaviour is in
`static/src/navbar/navbar_patch.js` (`etharaUserAvatar`,
`etharaGreeting`, plus the `onQuick*` handlers), and the styling is in
`static/src/scss/sidebar.scss` (`.o_ethara_profile_card`,
`.o_ethara_profile_top`, `.o_ethara_profile_avatar`,
`.o_ethara_profile_name`, `.o_ethara_profile_greeting`,
`.o_ethara_profile_actions`, `.o_ethara_quick_btn`,
`.o_ethara_quick_btn_active`, `.o_ethara_quick_badge`).

---

## Responsive Layout (Tablet & Mobile)

The sidebar, profile card and content area all adapt to smaller
viewports so the theme stays usable on a tablet held in portrait or a
phone in either orientation.

| Breakpoint            | Behaviour                                                                                                                                                                          |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **> 1024 px** desktop | Sidebar 240 px wide (or the value set in **Settings → Layout → Sidebar Width**), content area pushed right by the same amount. Standard layout.                                    |
| **≤ 1024 px** tablet  | Sidebar shrinks to 220 px, profile card padding and quick-button height shrink slightly so the eight action icons stay on two clean rows. Content keeps reading room.              |
| **≤ 768 px** phone    | Sidebar becomes an **off-canvas drawer** (`transform: translateX(-100%)`, width 280 px / max 85 vw). Content area reclaims the full viewport width. A floating **hamburger toggle** appears top-left, a semi-transparent **backdrop** dims the page when the drawer is open, and tapping a menu / favorite / search result auto-closes the drawer. |

The toggle button is rendered as a sibling of the sidebar in
`static/src/navbar/navbar.xml`:

```xml
<button type="button" class="o_ethara_mobile_toggle"
        aria-label="Toggle navigation"
        t-on-click="() => this.toggleMobileSidebar()">
    <i class="fa" t-att-class="etharaState.mobileOpen ? 'fa-times' : 'fa-bars'"/>
</button>
<div class="o_ethara_mobile_backdrop"
     t-att-class="{ o_ethara_mobile_backdrop_visible: etharaState.mobileOpen }"
     t-on-click="() => this.closeMobileSidebar()"/>
```

Behaviour lives in `static/src/navbar/navbar_patch.js`:

* `etharaState.mobileOpen` — reactive flag mirrored onto `<body>` as
  the `o_ethara_mobile_open` class.
* `toggleMobileSidebar()` — flips the flag and toggles the body class.
* `closeMobileSidebar()` — no-op when already closed; called
  automatically by `openHome`, `openFavorite`, `onSearchResultClick`,
  `onSidebarMenuClick`, and by `onSidebarAppClick` when the app has no
  sub-sections (so the drawer doesn't close while the user is just
  expanding a group).

All responsive CSS lives at the bottom of `static/src/scss/sidebar.scss`
in three blocks: the desktop-default hamburger / backdrop chrome
(`.o_ethara_mobile_toggle`, `.o_ethara_mobile_backdrop`), the
`@media (max-width: 1024px)` tablet adjustments, and the
`@media (max-width: 768px)` phone drawer block. Themed via the existing
`--ethara-*` CSS variables so it picks up palette and transition
settings automatically — including dark mode.

The desktop **Pin sidebar** quick-action button is hidden on phones via
`[aria-label="Collapse sidebar"], [aria-label="Expand sidebar"]
{ display: none !important; }` inside the phone media query — the
hamburger is the canonical open/close affordance on small screens.

---

## Favorites & Home Page

Per-user menu favorites are stored in the `ethara.menu.favorite` model
with a unique `(user_id, menu_id)` constraint. Users can manage their
own favorites through two RPC helpers exposed on the model:

* `get_favorite_menu_ids()` — returns the list of menu ids pinned by
  the current user.
* `toggle_favorite(menu_id)` — adds the menu if it is not already a
  favorite, removes it otherwise; returns the new state.

Both are called from the sidebar (star button next to every menu) and
from the **Favorites Home** client action
(`ethara_favorites_home`), which is opened by clicking the **Home** row
in the sidebar. The action renders a dashboard of all the user's
favorites with the same app icons as the sidebar.

Access rules (`security/ethara_security.xml`) restrict every row to its
owner: regular users can read, write, create and delete only their own
favorites; the `ir.model.access.csv` rule grants `(read, write, create,
unlink)` on the model.

---

## Custom Application Icons

The `ethara.menu.icon` model lets administrators replace the sidebar
icon of any top-level application without touching native Odoo menus.
Each row links a `Many2one('ir.ui.menu')` (restricted to top-level
menus by `domain="[('parent_id', '=', False)]"`) to either:

* a **FontAwesome glyph** — `Icon Class` (e.g. `fa fa-rocket`),
  `Glyph Color` and `Background Color`; or
* an **uploaded image** — `Icon Image` (PNG or SVG square asset),
  served from `/web/image/ethara.menu.icon/<id>/icon_image`.

A SQL constraint enforces one override per application
(`unique(menu_id)`).

Overrides are configured from **Settings > Ethara Theme >
Application Icons > Configure Menu Icons** (the button opens
`ethara_menu_icon_action`). Access is restricted to administrators via
`ir.model.access.csv`.

At page load, `EtharaTheme.get_menu_icons_script()` serialises every
override into a JSON dict and publishes it as `window.etharaMenuIcons`
for the sidebar JS (`static/src/utils/menu_icon.js` →
`resolveAppIcon(app)`) to consume.

---

## Chatter Position & Resize

This module ships a global control over how the chatter renders on form
views and a per-browser resize handle.

**Position** is driven by the `ethara_etp_theme.chatter_position`
`ir.config_parameter` and surfaced as the **Chatter Position** field in
**Settings > Ethara Theme > Chatter**:

* `sided` — Odoo's native responsive behaviour. The chatter is shown on
  the side on XXL viewports and falls back to the bottom on smaller
  screens.
* `bottom` — the chatter is always rendered below the form, regardless
  of viewport size.

The setting is published to the browser by injecting
`document.documentElement.dataset.etharaChatter = '<value>'` in `<head>`
(via `EtharaTheme.get_theme_init_script()`). A patch on
`FormRenderer.prototype.mailLayout()` then maps the side layouts to
their bottom equivalents when the attribute is `bottom`, so the form
compiler renders the chatter below without any fragile CSS overrides.

**Resize** is implemented entirely in the browser when the chatter is
shown on the side (i.e. `props.isChatterAside` is true):

* A thin vertical handle is added on the **left edge** of the chatter
  via a `t-inherit` extension of `mail.Chatter`.
* `pointerdown` on the handle starts a drag — the new width is written
  to a `--ethara-chatter-width` CSS variable on `<html>`, clamped to
  `[320px, 60% of viewport]`.
* `pointerup` persists the final width to
  `localStorage.ethara_chatter_width`. The width is restored on every
  page load.
* **Double-clicking** the handle clears the stored value and resets the
  chatter to its default width.

The width override is applied via a single SCSS rule
(`static/src/scss/chatter.scss`):

```scss
.o-mail-Form-chatter.o-aside,
.o-mail-ChatterContainer.o-aside {
    width: var(--ethara-chatter-width, calc(380px + var(--Chatter-asideExtraWidth, 0px)));
}
```

When the chatter is in `bottom` mode the handle is hidden via
`html[data-ethara-chatter="bottom"] .o_ethara_chatter_resize_handle { display: none; }`.

---

## List View Column Filters

Every list view shows a **filter row** directly under its column
headers. Each cell renders a small input whose type matches the
underlying field, so a user can filter records by typing/selecting a
value without opening the search bar:

| Field type | Widget |
| --- | --- |
| `selection` | `<select>` populated from the field's selection list |
| `boolean` | `<select>` with `Yes` / `No` / `Any` |
| `date` | `<input type="date">` |
| `datetime` | `<input type="datetime-local">` |
| `integer`, `float`, `monetary` | `<input type="number">` |
| `many2one` | text input matched against the display name (`ilike`) |
| `many2many`, `one2many` | text input matched against the display name (`ilike`) |
| `char`, `text`, default | text input matched with `ilike` |

The values are merged with the search bar's current domain
(`env.searchModel.domain`) and passed to `props.list.load({ domain })`.
The patch also subscribes to `env.searchModel`'s `update` event so the
column filters survive search-bar changes, filter toggles and group-by
operations (otherwise `list.load({ domain })` would be silently
overwritten on the next refresh).

Text and numeric inputs are debounced (350 ms) so each keystroke does
not trigger an RPC; selection, boolean and date inputs apply
immediately. The filter row is suppressed on grouped lists, and the
leading/trailing cells (record selector, optional-fields menu, open-form
column) are kept blank so the row stays aligned with the header on
column resize.

A compact-density variant kicks in when the user sets
**Settings > Ethara Theme > Appearance > Density = Compact**.

---

## Login Page Templates

`views/login_templates.xml` overrides `web.login_layout` with
`priority="99"` so it wins over the website module's own login layout
on website-enabled databases. The picked template is read from the
`ethara_etp_theme.login_template` config parameter and applied as a
class on `<body>`:

| Value | Body class | Description |
| --- | --- | --- |
| `centered` | `o_ethara_login_centered` | Classic card centered on a soft background. |
| `split` | `o_ethara_login_split` | Brand image on one side, sign-in form on the other. |
| `fullscreen` | `o_ethara_login_fullscreen` | Sign-in card floating over a full-page image. |
| `minimal` | `o_ethara_login_minimal` | Clean, borderless form with no decorative chrome. |

The optional **Side / Background Image** (managed by the standard
`image` widget on `res.config.settings`) is stored as an
`ir.attachment` and exposed via `/web/image/<id>`. The custom widget
`ethara_login_template_picker` shows clickable thumbnails for each of
the four layouts, and `ethara_login_template_preview` re-renders a
miniature of the selected layout in the settings panel, refreshing in
real time when the image or template changes.

Login styles are bundled in `web.assets_frontend` so they apply to the
login / sign-up / password-reset pages without loading the full
backend bundle.

---

## Light & Dark Themes

Light and dark palettes are hard-coded in `models/ethara_theme.py`
(`LIGHT_PALETTE` / `DARK_PALETTE` dicts). In dark mode the
user-configured Sidebar and Content backgrounds are ignored; the
backend uses a fixed dark palette (`bg: #0f1117`, `surface: #181b23`,
`border: #2a2e3a`, etc.). The primary color and hover shade still
apply.

The toggle is available **in two places**:

* the **Theme Mode** field in **Settings > Ethara Theme > Appearance**
  (persisted server-side via `ir.config_parameter`); and
* the **dark mode** button in the sidebar footer (persisted in
  `localStorage.ethara_dark_mode` for instant per-user override).

The `darkmode.scss` asset contains the overrides applied when
`.o_ethara_dark` is present on `<html>`.

---

## Models Added by This Module

| Model | Type | Purpose |
| --- | --- | --- |
| `ethara.theme` | AbstractModel | Runtime helper. Reads ~15 `ir.config_parameter` values, validates them (HEX regex for colors, integers with minimums for sizes), and renders the `:root { --ethara-* }` CSS variable block plus the chosen Google Fonts URL into `<head>`. Also publishes `window.etharaMenuIcons`. |
| `ethara.menu.icon` | Model | Per-application sidebar icon override. One row per top-level menu (`unique(menu_id)`); FontAwesome glyph or uploaded image. Admin-only. |
| `ethara.menu.favorite` | Model | Per-user pinned menus. Unique `(user_id, menu_id)`. Users manage only their own rows. |
| `res.config.settings` | TransientModel (inherited) | Adds ~20 fields, every one backed by `config_parameter="ethara_etp_theme.*"`. Also manages the **Login Image** as a dedicated `ir.attachment` and stores `..login_image_attachment_id` and `..login_image_url` in `ir.config_parameter` so the public login layout can reference it. |

---

## Technical Architecture

* **CSS variables, not SCSS recompiles.** The theme is driven by
  a single `:root { --ethara-* }` block emitted by
  `EtharaTheme.get_theme_head()`. The block is read from
  `ir.config_parameter` on every request, validated (`_HEX_RE` for
  colors, `_size()` for integers, `_choice()` for enums) and injected
  into `<head>` by `views/webclient_templates.xml`. SCSS files in
  `static/src/scss/` consume these variables; they never need to be
  recompiled to re-skin the backend.
* **NavBar patch over component replacement.** The sidebar is added
  with `patch(NavBar.prototype, { ... })` and a `t-inherit` extension
  of `web.NavBar`. The original navbar component, layout and routing
  are preserved.
* **No new menus.** The sidebar reads the existing menu tree via the
  `menu` service. Removing this module restores the stock top-bar
  navigation immediately.
* **Per-asset bundling.** Backend assets (`web.assets_backend`) hold
  the sidebar SCSS/JS/XML and the settings widgets; frontend assets
  (`web.assets_frontend`) hold only `variables.scss` and `login.scss`
  so the login page stays light.
* **Login image as `ir.attachment`.** Uploading a login image creates
  (or updates) a single public attachment and stores its id + URL in
  `ir.config_parameter`; clearing the field unlinks the attachment.
  The login template references it via the stored URL — no base64 is
  inlined in the login HTML.
* **Security.** `ethara.menu.favorite` is row-restricted to its owner
  by `ethara_security.xml`. `ethara.menu.icon` is admin-only via
  `ir.model.access.csv`. All `config_parameter` writes go through the
  standard Odoo Settings flow (`sudo()` only inside controlled model
  methods).

---

## File Structure

```text
ethara_etp_theme/
  __manifest__.py
  __init__.py
  README.md
  models/
    __init__.py
    ethara_theme.py            # AbstractModel: builds CSS vars + menu icons JSON
    res_config_settings.py     # Settings fields, login image attachment plumbing
    ethara_menu_icon.py        # Per-app sidebar icon overrides
    ethara_menu_favorite.py    # Per-user pinned menus
  security/
    ir.model.access.csv        # Users CRUD their favorites; admins manage icons
    ethara_security.xml        # Favorites row rule (user_id == uid)
  views/
    webclient_templates.xml    # Injects --ethara-* CSS vars + font into <head>
    login_templates.xml        # Overrides web.login_layout (priority 99)
    res_config_settings_views.xml  # Settings panel (7 blocks)
    ethara_favorites_views.xml # Favorites Home client action
    ethara_menu_icon_views.xml # List/form/action for icon overrides
  static/src/
    scss/
      variables.scss           # Shared design tokens
      theme.scss               # Global backend restyle
      sidebar.scss             # Left sidebar
      views.scss               # Form / kanban / list / search overrides
      inputs.scss              # Four input styles (outlined/filled/underline/soft)
      listview.scss            # List view tweaks
      chatter.scss             # Chatter resize handle + width variable + bottom mode
      darkmode.scss            # Dark mode overrides
      login.scss               # Login page (frontend bundle)
    navbar/
      navbar_patch.js          # OWL patch on web.NavBar adding the sidebar
      navbar.xml               # Template inherit injecting the <aside>
    favorites/
      favorites_home.js
      favorites_home.xml
      favorites_home.scss
    listview/
      column_filter.js         # ListRenderer patch: per-column filter row + domain builder
      column_filter.xml        # Template inherit injecting the filter <tr> under <thead>
      column_filter.scss       # Filter row layout + themed inputs
    chatter/
      chatter_patch.js         # FormRenderer + Chatter patch: position switch + resize
      chatter_patch.xml        # Template inherit injecting the resize handle
    login/
      login_preview.scss
      login_template_field.js  # ethara_login_template_picker + _preview widgets
      login_template_field.xml
    utils/
      menu_icon.js             # resolveAppIcon(app) helper
```

---

## How to Reset to Defaults

The theme reads every value through `ir.config_parameter`. To reset a
single field, clear its parameter via the Odoo shell:

```python
self.env["ir.config_parameter"].sudo().set_param(
    "ethara_etp_theme.primary_color", ""
)
```

To reset everything, delete all parameters under the
`ethara_etp_theme.*` namespace:

```python
self.env["ir.config_parameter"].sudo().search(
    [("key", "=like", "ethara_etp_theme.%")]
).unlink()
```

Defaults will be applied again on the next page load (the
`EtharaTheme._color`, `_size` and `_choice` helpers fall back to the
defaults whenever a parameter is missing or invalid).
