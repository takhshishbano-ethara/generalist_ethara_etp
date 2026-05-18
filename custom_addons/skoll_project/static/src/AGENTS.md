# Skoll Frontend — AGENTS.md

> OWL framework components for the Skoll UI. Pure Odoo frontend — no React, no Vue.

## Technology

- **Framework**: Odoo OWL (Odoo Web Library) — reactive component framework
- **Language**: JavaScript (ES modules), XML (templates), SCSS (styling)
- **Bundling**: Odoo asset bundling via `__manifest__.py` `assets` key
- **State**: OWL reactive state + Odoo services (rpc, orm, bus_service)

## Component Map

```
static/src/
├── task_dashboard/              # Main task management view (action)
│   ├── task_dashboard.js        # Action component, registered in action registry
│   ├── task_dashboard.xml       # QWeb template
│   └── task_dashboard.scss      # Styles
├── costing_dashboard/           # Token cost tracking view (action)
│   ├── costing_dashboard.js
│   ├── costing_dashboard.xml
│   └── costing_dashboard.scss
├── chat_widget/                 # Live chat interface (embedded in form)
│   ├── chat_widget.js
│   ├── chat_widget.xml
│   └── chat_widget.scss
├── chat_service.js              # Chat RPC service (shared singleton)
├── sandbox_notification_service.js  # Bus listener for sandbox events
├── components/
│   ├── sandbox_card/            # Sandbox status card (used in dashboard)
│   │   ├── sandbox_card.js
│   │   ├── sandbox_card.xml
│   │   └── sandbox_card.scss
│   ├── sandbox_iframe/          # Embedded sandbox browser view
│   │   ├── sandbox_iframe.js
│   │   ├── sandbox_iframe.xml
│   │   └── sandbox_iframe.scss
│   ├── task_progress/           # Progress indicator component
│   │   ├── task_progress.js
│   │   ├── task_progress.xml
│   │   └── task_progress.scss
│   └── gog_auth_dialog/        # Google OAuth popup dialog
│       ├── gog_auth_dialog.js
│       ├── gog_auth_dialog.xml
│       └── gog_auth_dialog.scss
└── views/fields/
    ├── markdown_field/          # Custom field widget: markdown with preview
    │   ├── markdown_field.js
    │   ├── markdown_field.xml
    │   └── markdown_field.scss
    └── json_field/              # Custom field widget: JSON viewer/editor
        ├── json_field.js
        ├── json_field.xml
        └── json_field.scss
```

## Conventions

### Component Structure (triplet pattern)

Every component consists of exactly 3 files:

```javascript
// component_name.js
/** @odoo-module */
import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class MyComponent extends Component {
    static template = "skoll.MyComponent";
    static props = { /* prop definitions */ };

    setup() {
        this.orm = useService("orm");
        this.rpc = useService("rpc");
        this.state = useState({ /* reactive state */ });
    }
}

// Register as action (for full-page views)
registry.category("actions").add("skoll.my_action", MyComponent);

// OR register as field widget
registry.category("fields").add("skoll_my_field", { component: MyComponent });
```

```xml
<!-- component_name.xml -->
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
    <t t-name="skoll.MyComponent">
        <!-- QWeb template -->
    </t>
</templates>
```

```scss
// component_name.scss
.o_skoll_my_component {
    // Scoped styles
}
```

### Naming Conventions

- Template names: `skoll.ComponentName` (module prefix + PascalCase)
- CSS classes: `o_skoll_component_name` (Odoo prefix + snake_case)
- File names: `component_name.js/xml/scss` (snake_case)
- Action registry keys: `skoll.action_name`
- Field widget names: `skoll_field_name`

### Services

- `chat_service.js` — Singleton managing WebSocket/RPC chat operations
- `sandbox_notification_service.js` — Listens to Odoo bus for sandbox lifecycle events

Services are registered in `@web/core/registry` under the `"services"` category and injected via `useService()`.

### Data Fetching

```javascript
// ORM calls (preferred for model operations)
const records = await this.orm.searchRead("skoll.skoll", domain, fields);
await this.orm.call("skoll.sandbox", "action_start_sandbox", [recordId]);

// RPC calls (for controller endpoints)
const result = await this.rpc("/skoll/chat/create_turn", { sandbox_id, prompt });
```

### Real-time Updates (Bus)

```javascript
setup() {
    this.busService = useService("bus_service");
    this.busService.subscribe("skoll/sandbox_status", this.onSandboxUpdate.bind(this));
}
```

## Asset Registration

In `__manifest__.py`:
```python
'assets': {
    'web.assets_backend': [
        'skoll/static/src/**/*.js',
        'skoll/static/src/**/*.xml',
        'skoll/static/src/**/*.scss',
    ],
},
```

## Anti-Patterns (DO NOT)

- **NEVER** use jQuery — use OWL reactive patterns
- **NEVER** manipulate DOM directly — use `t-ref` + `useRef()` sparingly
- **NEVER** import from other modules' `static/src/` directly — use registry/services
- **NEVER** skip the XML template — every component MUST have a `.xml` template file
- **NEVER** use inline styles — use SCSS with scoped `o_skoll_*` classes
- **NEVER** put business logic in components — delegate to services or RPC calls

## Adding a New Component

1. Create directory: `static/src/components/my_component/`
2. Create triplet: `my_component.js`, `my_component.xml`, `my_component.scss`
3. Use `static template = "skoll.MyComponent"` pattern
4. Register in appropriate registry category
5. Asset bundling is automatic (glob pattern in manifest)
