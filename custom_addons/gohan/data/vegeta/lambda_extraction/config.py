"""
Leviathon configuration: SOP rules, banned phrases, rubric weights, breakpoints.
All constants derived from High_End_Design_SOP_v2.pdf and PRD_Scoring.xlsx.
"""

# -- SOP Design Categories --
CATEGORIES = {
    "normal_website": "Normal Website",
    "cool_transition": "Cool Transition",
    "representation": "Representation Format",
    "svg_vector": "SVG & Vector Graphics",
    "3d_webgl": "3D & WebGL / Game",
}

# -- PRD Word Count Constraints --
# Vegeta client requirement: 800 floor, 5000 hard ceiling. Aim 3,200-4,800.
PRD_MIN_WORDS = 800
PRD_MAX_WORDS = 5000
PRD_TARGET_MIN_WORDS = 3200
PRD_TARGET_MAX_WORDS = 4800

# -- Tier 1 Banned Phrases (each = -0.5 from S11.1; 5+ = R1 auto-reject) --
TIER1_BANNED_PHRASES = [
    "smooth animation",
    "modern ux",
    "clean layout",
    "nice",
    "beautiful",
    "sleek",
    "elegant",
    "dynamic effect",
    "subtle motion",
    "intuitive navigation",
    "seamless experience",
    "premium feel",
    "eye-catching",
    "visually appealing",
    "professional look",
    "user-friendly",
    "cutting-edge",
    "immersive journey",
    "pixel-perfect",
    "next-level",
    "stunning visuals",
    "state-of-the-art",
    "intuitive interface",
    "leverage cutting-edge",
]

# -- Tier 2 Banned Phrases (violation only if no specific values follow) --
TIER2_BANNED_PHRASES = [
    "responsive design",
    "fast loading",
    "animated elements",
    "hover effects",
    "parallax scrolling",
]

# -- Auto-Reject Triggers --
REJECT_TRIGGERS = {
    "R1": "5+ Tier 1 vague phrases without specific values",
    "R2": "Colors mentioned without hex codes",
    "R3": "No font family names",
    "R4": f"Under {PRD_MIN_WORDS} words",
    "R5": f"Over {PRD_MAX_WORDS} words",
}

# -- Responsive Breakpoints (SOP mandated) --
BREAKPOINTS = {
    "desktop_large": {"width": 1440, "height": 900, "label": "Desktop Large"},
    "tablet_landscape": {"width": 1024, "height": 768, "label": "Tablet Landscape"},
    "tablet_portrait": {"width": 768, "height": 1024, "label": "Tablet Portrait"},
    "mobile": {"width": 375, "height": 812, "label": "Mobile"},
}

# -- Screenshot viewport for primary captures --
PRIMARY_VIEWPORT = {"width": 1920, "height": 1080}

# -- Chromium launch args (AWS Lambda) --
# `--single-process` is REQUIRED on Lambda. Without it, Chromium tries to spawn
# child renderer/GPU processes and crashes immediately with TargetClosedError —
# Lambda's PID namespace + fork() restrictions break multi-process Chromium.
# Side-effect: heavy WebGL/3D sites can render black screenshots; the asset
# collector's blank-detect retry compensates by re-capturing on detection.
#
# The other flags reduce CPU/memory overhead and disable features Lambda can't
# satisfy (background networking, sync, extensions, etc.).
LAMBDA_CHROMIUM_ARGS = [
    "--single-process",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-zygote",
    "--disable-setuid-sandbox",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--force-color-profile=srgb",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-default-browser-check",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints,AudioServiceOutOfProcess,IsolateOrigins,site-per-process",
]

# -- Rubric Sections with weights and criticality --
RUBRIC_SECTIONS = {
    "S1": {
        "name": "Word Count & Format",
        "max_points": 5,
        "weight": 0.05,
        "criticality": "GATE",
        "consequence": "AUTO-REJECT if failed",
    },
    "S2": {
        "name": "Visual & Brand Direction",
        "max_points": 14,
        "weight": 0.14,
        "criticality": "CRITICAL",
        "consequence": "Score <7/14 caps max grade at C (79)",
    },
    "S3": {
        "name": "Site Architecture & Page Specifications",
        "max_points": 18,
        "weight": 0.18,
        "criticality": "CRITICAL",
        "consequence": "Score <9/18 caps max grade at C (79)",
    },
    "S4": {
        "name": "Motion Language",
        "max_points": 14,
        "weight": 0.14,
        "criticality": "CRITICAL",
        "consequence": "Score <7/14 caps max grade at C (79)",
    },
    "S5": {
        "name": "Technical Ambition",
        "max_points": 9,
        "weight": 0.09,
        "criticality": "IMPORTANT",
        "consequence": "Score <4.5/9 caps max grade at B (89)",
    },
    "S6": {
        "name": "Backend & Application Logic",
        "max_points": 5,
        "weight": 0.05,
        "criticality": "REQUIRED",
        "consequence": "Score of 0 = notable gap flagged",
    },
    "S7": {
        "name": "Data Model",
        "max_points": 9,
        "weight": 0.09,
        "criticality": "IMPORTANT",
        "consequence": "Score <4.5/9 caps max grade at B (89)",
    },
    "S8": {
        "name": "Accessibility & Quality",
        "max_points": 9,
        "weight": 0.09,
        "criticality": "IMPORTANT",
        "consequence": "Score <4.5/9 caps max grade at B (89)",
    },
    "S9": {
        "name": "Content & SEO",
        "max_points": 5,
        "weight": 0.05,
        "criticality": "REQUIRED",
        "consequence": "Score of 0 = notable gap flagged",
    },
    "S10": {
        "name": "Cool Transition Addendum",
        "max_points": 7,
        "weight": 0.07,
        "criticality": "CATEGORY-SPECIFIC",
        "consequence": "N/A for non-Cool-Transition; points redistributed (93 total / normalized)",
    },
    "S11": {
        "name": "Overall Quality & Coherence",
        "max_points": 5,
        "weight": 0.05,
        "criticality": "HOLISTIC",
        "consequence": "Reflects true specificity of entire submission",
    },
}

# -- Grade Scale --
GRADE_SCALE = [
    (90, 100, "A", "Production-ready"),
    (80, 89, "B", "Solid, minor gaps"),
    (70, 79, "C", "Usable, needs follow-up"),
    (60, 69, "D", "Significant holes"),
    (0, 59, "F", "Major rewrite"),
]

# -- Tech Detection Patterns --
TECH_DETECTION = {
    "three_js": {
        "globals": ["THREE", "__THREE__"],
        "script_patterns": ["three.js", "three.min.js", "three.module.js", "@three"],
        "dom_markers": ["canvas[data-engine]"],
    },
    "gsap": {
        "globals": ["gsap", "GreenSock", "TweenMax", "TweenLite", "TimelineMax"],
        "script_patterns": ["gsap", "greensock", "ScrollTrigger", "ScrollSmoother"],
        "dom_markers": [],
    },
    "lenis": {
        "globals": ["Lenis", "__lenis"],
        "script_patterns": ["lenis", "@studio-freight/lenis"],
        "dom_markers": ["html.lenis", "[data-lenis]"],
    },
    "framer_motion": {
        "globals": [],
        "script_patterns": ["framer-motion", "motion"],
        "dom_markers": ["[data-framer-component-type]", "[data-projection-id]"],
    },
    "lottie": {
        "globals": ["lottie", "bodymovin"],
        "script_patterns": ["lottie", "bodymovin"],
        "dom_markers": ["lottie-player", "dotlottie-player", "[data-anim-type]"],
    },
    "react": {
        "globals": ["__REACT_DEVTOOLS_GLOBAL_HOOK__", "__NEXT_DATA__"],
        "script_patterns": ["react", "next"],
        "dom_markers": ["#__next", "[data-reactroot]"],
    },
    "vue": {
        "globals": ["__VUE__", "__NUXT__"],
        "script_patterns": ["vue", "nuxt"],
        "dom_markers": ["#__nuxt", "[data-v-]"],
    },
    "svelte": {
        "globals": [],
        "script_patterns": ["svelte"],
        "dom_markers": [".svelte-"],
    },
    "bootstrap": {
        "globals": ["bootstrap"],
        "script_patterns": ["bootstrap"],
        "dom_markers": [".navbar-toggler", ".modal-dialog"],
    },
    "material_ui": {
        "globals": [],
        "script_patterns": ["@mui", "@material-ui"],
        "dom_markers": ["[class*='MuiButton']", "[class*='MuiPaper']"],
    },
    "chakra_ui": {
        "globals": [],
        "script_patterns": ["@chakra-ui"],
        "dom_markers": ["[class*='chakra-']"],
    },
    "ant_design": {
        "globals": [],
        "script_patterns": ["antd"],
        "dom_markers": ["[class*='ant-btn']", "[class*='ant-modal']"],
    },
    "vuetify": {
        "globals": [],
        "script_patterns": ["vuetify"],
        "dom_markers": [".v-application", ".v-btn"],
    },
    "quasar": {
        "globals": [],
        "script_patterns": ["quasar"],
        "dom_markers": [".q-page", ".q-btn"],
    },
    "bulma": {
        "globals": [],
        "script_patterns": ["bulma"],
        "dom_markers": [".hero-body", ".is-primary"],
    },
    "mantine": {
        "globals": [],
        "script_patterns": ["@mantine"],
        "dom_markers": ["[class*='mantine-']"],
    },
    "radix_ui": {
        "globals": [],
        "script_patterns": ["@radix-ui"],
        "dom_markers": ["[data-radix-collection-item]"],
    },
    "headless_ui": {
        "globals": [],
        "script_patterns": ["@headlessui"],
        "dom_markers": ["[data-headlessui-state]"],
    },
    "carbon": {
        "globals": [],
        "script_patterns": ["@carbon"],
        "dom_markers": ["[class*='bx--']", "[class*='cds--']"],
    },
    "fluent_ui": {
        "globals": [],
        "script_patterns": ["@fluentui"],
        "dom_markers": ["[class*='fui-']"],
    },
    "foundation": {
        "globals": [],
        "script_patterns": ["foundation"],
        "dom_markers": [".grid-x", ".callout"],
    },
    "barba_js": {
        "globals": ["barba"],
        "script_patterns": ["barba", "barba.js", "@barba/core"],
        "dom_markers": ["[data-barba]", "[data-barba-namespace]"],
    },
    "swup": {
        "globals": ["swup"],
        "script_patterns": ["swup"],
        "dom_markers": [],
    },
    "highway": {
        "globals": ["Highway"],
        "script_patterns": ["highway", "@unseenco/taxi"],
        "dom_markers": ["[data-router-wrapper]", "[data-router-view]"],
    },
    "taxi": {
        "globals": ["Taxi"],
        "script_patterns": ["@unseenco/taxi", "taxi.js"],
        "dom_markers": ["[data-taxi]"],
    },
}

PAGE_TRANSITION_LIBS = ["barba_js", "swup", "highway", "taxi"]

# -- Category Classification Rules --
CATEGORY_RULES = [
    {
        "category": "3d_webgl",
        "required_any": ["three_js", "babylon", "pixi"],
        "condition": "canvas must be fullscreen or >800k px area",
        "description": "Three.js / WebGL canvas detected as primary element",
    },
    {
        "category": "representation",
        "signals": ["horizontal_scroll", "parallax_heavy", "gsap_pin_scrub"],
        "description": "Horizontal scroll or pinned scroll-driven narrative",
    },
    {
        "category": "svg_vector",
        "required_any": ["lottie", "rive", "DrawSVGPlugin", "MorphSVGPlugin"],
        "signals": ["svg_animated_count>=3", "d3_svg_count>=5", "anime+svg_paths"],
        "description": "SVG/vector animation as primary visual language",
    },
    {
        "category": "cool_transition",
        "required_all": ["gsap"],
        "condition": "gsap + (ScrollTrigger OR Lenis) + enough triggers, OR gsap + page transition lib",
        "bonus": ["lenis", "barba", "swup"],
        "description": "GSAP with scroll choreography and/or page transitions",
    },
    {
        "category": "normal_website",
        "signals": [],
        "description": "Default — typography and layout craft, no heavy animation framework detected",
    },
]

# -- Required PRD Sections --
PRD_SECTIONS = [
    "Product Overview",
    "Visual & Brand Direction",
    "Technical Ambition",
    "Site Architecture & Page Specifications",
    "Motion Language",
    "Backend & Application Logic",
    "Accessibility & Quality",
    "Content & SEO",
    "Responsive Behavior",
    "Reference Prototypes",
]

# -- Cool Transition Extra Sections --
COOL_TRANSITION_EXTRAS = [
    "A. Page-to-Page Transition Timing",
    "B. Scroll-Triggered Animation Map",
    "C. Staggered Reveal Sequences",
    "D. Micro-Interaction Specs",
]

# -- Screenshot Strategy (SOP image ordering) --
SCREENSHOT_STRATEGY = {
    "style_targets": {
        "count": 3,
        "description": "Primary style target - full viewport of key pages",
        "positions": ["hero_top", "secondary_page", "tertiary_page"],
    },
    "component_closeups": {
        "count": 3,
        "description": "Component references - close-ups of UI elements",
        "positions": ["navbar", "card_grid", "footer"],
    },
    "structural": {
        "count": 3,
        "description": "Structural/wireframe - layout grid and content hierarchy",
        "positions": ["mid_scroll", "transition_frame", "mobile_view"],
    },
}

# -- Scroll Capture Resolution --
SCROLL_STEP_PERCENT = 5

# -- Interactive Element Selectors --
INTERACTIVE_SELECTORS = [
    "a",
    "button",
    "[role='button']",
    "input",
    "select",
    "textarea",
    ".card",
    "[class*='card']",
    "[class*='Card']",
    "[data-hover]",
    "[class*='btn']",
    "[class*='Btn']",
    "nav a",
    "nav button",
]

# -- Asset MIME Types to Capture --
ASSET_MIME_TYPES = {
    "images": ["image/png", "image/jpeg", "image/webp", "image/avif", "image/gif"],
    "svgs": ["image/svg+xml"],
    "videos": ["video/mp4", "video/webm"],
    "fonts": [
        "font/woff2",
        "font/woff",
        "font/ttf",
        "font/otf",
        "application/font-woff2",
        "application/font-woff",
        "application/x-font-ttf",
    ],
    "json": ["application/json"],
}


VEGETA_CATEGORIES = [
    "Public Utility",
    "News",
    "Publishing",
    "Retail",
    "Services",
    "ERP",
    "Knowledge",
    "Procurement",
    "Vertical Markets",
    "HCM",
    "CRM",
    "Gov. Portal",
    "Community",
    "TMS",
    "Multimedia",
    "AI Platform",
]


VEGETA_CATEGORY_EMPHASIS = {
    "Public Utility": {
        "core_entities": ["account", "service", "bill_or_invoice", "payment", "application_or_request", "document"],
        "core_flows": ["account_lookup_and_link", "view_and_pay_bill", "submit_application", "track_request_status"],
        "defining_mechanic": "citizen self-service with accessibility as a legal floor, multilingual, low-bandwidth fallbacks",
        "reference_brands": ["healthcare.gov", "irs.gov", "USPS"],
        "signature_routes": ["/account", "/pay", "/services", "/forms", "/track"],
        "gated": False,
    },
    "News": {
        "core_entities": ["article", "section_or_topic", "author", "homepage_layout", "subscription", "comment"],
        "core_flows": ["browse_by_section", "search", "metered_paywall_and_subscribe", "editorial_publish_workflow", "breaking_news_realtime"],
        "defining_mechanic": "editorial CMS plus metered access plus ad slots",
        "reference_brands": ["NYTimes", "Washington Post", "The Guardian"],
        "signature_routes": ["/section", "/article", "/topic", "/subscribe", "/author"],
        "gated": False,
    },
    "Publishing": {
        "core_entities": ["post", "author", "publication", "subscriber", "newsletter_issue"],
        "core_flows": ["write_in_rich_editor", "schedule_and_publish", "manage_subscribers", "send_newsletter", "reader_subscribes_to_tier"],
        "defining_mechanic": "author workspace plus subscription tiers plus email and RSS distribution",
        "reference_brands": ["Substack", "Medium", "Ghost"],
        "signature_routes": ["/p", "/posts", "/archive", "/subscribe", "/about", "/@author"],
        "gated": False,
    },
    "Retail": {
        "core_entities": ["product", "variant", "cart", "order", "customer", "inventory", "return", "payment"],
        "core_flows": ["browse_and_filter_catalog", "product_detail", "add_to_cart", "checkout_and_pay", "track_order", "request_return"],
        "defining_mechanic": "catalog plus checkout plus order lifecycle plus inventory",
        "reference_brands": ["Shopify storefront", "Amazon", "Target"],
        "signature_routes": ["/products", "/collections", "/cart", "/checkout", "/account/orders"],
        "gated": False,
    },
    "Services": {
        "core_entities": ["provider", "service", "availability_slot", "appointment", "customer", "review"],
        "core_flows": ["search_and_compare_providers", "view_availability", "book_appointment", "reschedule_or_cancel", "reminders_and_no_show"],
        "defining_mechanic": "directory plus booking calendar plus appointment lifecycle",
        "reference_brands": ["Zocdoc", "Calendly", "Booksy"],
        "signature_routes": ["/providers", "/book", "/availability", "/appointments"],
        "gated": False,
    },
    "ERP": {
        "core_entities": ["org_unit", "project", "document", "kpi_or_metric", "task", "integration"],
        "core_flows": ["navigate_multi_module_shell", "manage_project", "edit_doc", "view_kpi_dashboards", "run_batch_operations", "configure_integrations"],
        "defining_mechanic": "multi-module shell plus org hierarchy plus granular RBAC plus audit trail",
        "reference_brands": ["SAP", "Oracle NetSuite", "Odoo"],
        "signature_routes": ["/finance", "/projects", "/inventory", "/reports", "/admin"],
        "gated": True,
    },
    "Knowledge": {
        "core_entities": ["course", "lesson", "enrollment", "quiz", "question", "attempt", "certificate"],
        "core_flows": ["browse_and_enroll", "take_lesson", "track_progress", "take_graded_quiz", "earn_certificate", "instructor_authoring"],
        "defining_mechanic": "course and lesson model plus progress tracking plus grading",
        "reference_brands": ["Coursera", "Khan Academy", "Wikipedia"],
        "signature_routes": ["/courses", "/learn", "/lessons", "/quiz", "/certificate", "/instructor"],
        "gated": False,
    },
    "Procurement": {
        "core_entities": ["supplier", "rfq", "bid", "contract", "approval", "purchase_order"],
        "core_flows": ["discover_suppliers", "raise_rfq", "collect_multi_party_bids", "run_approval_chain", "award_and_contract"],
        "defining_mechanic": "RFQ workflow plus multi-party bidding plus approval chains",
        "reference_brands": ["Coupa", "Ariba", "Procurify"],
        "signature_routes": ["/rfq", "/suppliers", "/bids", "/contracts", "/approvals"],
        "gated": True,
    },
    "Vertical Markets": {
        "core_entities": ["listing", "host", "guest", "booking", "review", "payment", "payout", "message_thread"],
        "core_flows": ["search_with_map_and_filters", "listing_detail", "book_and_pay", "host_listing_management", "host_calendar_and_pricing", "two_sided_reviews"],
        "defining_mechanic": "two-sided marketplace plus booking lifecycle plus trust and payout mechanics",
        "reference_brands": ["Airbnb", "Vrbo", "Booking.com"],
        "signature_routes": ["/listings", "/search", "/host", "/trips", "/messages", "/reviews"],
        "gated": False,
    },
    "HCM": {
        "core_entities": ["employee", "org_unit", "time_off_request", "payroll_record", "onboarding_task", "approval"],
        "core_flows": ["manage_employee_record", "view_org_chart", "request_time_off", "payroll_views", "onboarding_workflow"],
        "defining_mechanic": "employee record plus approval chains plus onboarding workflows",
        "reference_brands": ["Workday", "BambooHR", "Gusto"],
        "signature_routes": ["/employees", "/time-off", "/payroll", "/onboarding", "/org-chart"],
        "gated": True,
    },
    "CRM": {
        "core_entities": ["contact", "company", "deal", "pipeline_stage", "activity", "sequence", "report"],
        "core_flows": ["work_the_pipeline", "manage_contacts_and_companies", "log_activities_timeline", "run_sequences", "view_reports_and_dashboards"],
        "defining_mechanic": "relational contact and company and deal model plus pipeline plus activity timeline",
        "reference_brands": ["Salesforce", "HubSpot", "Pipedrive"],
        "signature_routes": ["/contacts", "/companies", "/deals", "/pipeline", "/sequences", "/reports"],
        "gated": True,
    },
    "Gov. Portal": {
        "core_entities": ["citizen_account", "service_request_or_case", "form_submission", "document", "identity_verification", "audit_record"],
        "core_flows": ["verify_identity", "submit_form_driven_service_request", "upload_documents", "track_case_status", "staff_case_processing"],
        "defining_mechanic": "form-driven service requests plus case tracking plus identity verification plus audit-grade logging",
        "reference_brands": ["GOV.UK", "USA.gov", "Singapore Gov"],
        "signature_routes": ["/services", "/apply", "/cases", "/verify", "/documents"],
        "gated": False,
    },
    "Community": {
        "core_entities": ["post", "comment", "vote", "user_or_reputation", "moderation_action", "notification"],
        "core_flows": ["post_and_reply_in_threads", "vote", "build_reputation", "work_moderation_queue", "receive_notifications"],
        "defining_mechanic": "threaded discussion plus voting and reputation plus moderation tooling",
        "reference_brands": ["Reddit", "Discourse", "Stack Overflow"],
        "signature_routes": ["/r", "/c", "/posts", "/comments", "/profile", "/moderate"],
        "gated": False,
    },
    "TMS": {
        "core_entities": ["workspace", "project", "task", "board_or_view", "assignee", "due_date", "dependency", "comment"],
        "core_flows": ["create_and_assign_tasks", "switch_views_kanban_list_calendar", "set_dependencies_and_due_dates", "collaborate_via_comments"],
        "defining_mechanic": "tasks and projects across multiple views plus assignees plus dependencies",
        "reference_brands": ["Asana", "Linear", "Monday"],
        "signature_routes": ["/projects", "/tasks", "/board", "/calendar", "/team"],
        "gated": True,
    },
    "Multimedia": {
        "core_entities": ["media_asset", "playlist", "channel_or_creator", "view_history", "recommendation", "comment"],
        "core_flows": ["browse_and_discover", "play_media", "build_playlists", "view_history", "creator_upload_workspace"],
        "defining_mechanic": "media catalog plus player plus recommendations plus creator workspace",
        "reference_brands": ["YouTube", "Spotify", "Vimeo"],
        "signature_routes": ["/watch", "/listen", "/channel", "/playlist", "/upload", "/library"],
        "gated": False,
    },
    "AI Platform": {
        "core_entities": ["model", "playground_session", "api_key", "usage_record", "rate_limit_policy", "project"],
        "core_flows": ["browse_model_catalog", "run_playground", "manage_api_keys", "view_usage_dashboards", "set_rate_limits"],
        "defining_mechanic": "model catalog plus playground plus API keys plus usage and cost metering",
        "reference_brands": ["OpenAI Platform", "Anthropic Console", "Hugging Face"],
        "signature_routes": ["/playground", "/models", "/api-keys", "/usage", "/docs", "/projects"],
        "gated": True,
    },
}


VEGETA_PRD_SECTIONS = [
    "Overview",
    "Goals & Non-Goals",
    "User Roles & Permissions",
    "Authentication & Onboarding",
    "Core Features & User Flows",
    "Data Model",
    "API Design",
    "UI/UX Requirements",
    "Error Handling & Edge Cases",
    "Non-Functional Requirements",
    "Category-Specific Guidelines",
]


VEGETA_BANNED_PHRASES = [
    "modern UX",
    "seamless",
    "intuitive",
    "stunning",
    "leverage",
    "best-in-class",
    "robust",
    "world-class",
    "cutting-edge",
    "next-generation",
    "industry-leading",
    "state-of-the-art",
    "game-changing",
    "revolutionary",
    "powerful",
    "delightful",
    "elegant solution",
    "user-friendly",
]


PRIORITY_CRAWL_PATHS = [
    "/pricing",
    "/plans",
    "/features",
    "/product",
    "/products",
    "/solutions",
    "/about",
    "/api",
    "/docs",
    "/documentation",
    "/developers",
    "/help",
    "/support",
    "/security",
    "/dashboard",
    "/app",
    "/login",
    "/signin",
    "/sign-in",
    "/signup",
    "/sign-up",
    "/register",
    "/contact",
    "/integrations",
    "/enterprise",
    "/customers",
    "/blog",
    "/changelog",
    "/terms",
    "/privacy",
]


API_DOC_PROBE_PATHS = [
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/swagger.yaml",
    "/api-docs",
    "/api-docs.json",
    "/api/openapi.json",
    "/api/swagger.json",
    "/api/v1/openapi.json",
    "/api/v1/swagger.json",
    "/api/v2/openapi.json",
    "/.well-known/openapi",
    "/.well-known/security.txt",
    "/.well-known/ai-plugin.json",
    "/docs/openapi.json",
    "/v1/openapi.json",
    "/graphql",
    "/api/graphql",
    "/query",
    "/api/query",
]


AUTH_FLOW_ROUTES = [
    "/signup",
    "/sign-up",
    "/register",
    "/create-account",
    "/login",
    "/signin",
    "/sign-in",
    "/forgot-password",
    "/password-reset",
    "/reset-password",
    "/verify-email",
    "/verify",
    "/mfa",
    "/two-factor",
    "/onboarding",
]


PRICING_ROUTES = [
    "/pricing",
    "/plans",
    "/pricing-plans",
    "/subscribe",
    "/upgrade",
    "/billing",
]


PAGE_TYPE_HINTS = {
    "landing": ["^/$", "^/home", "^/index"],
    "pricing": ["/pricing", "/plans", "/billing", "/subscribe", "/upgrade"],
    "auth": ["/login", "/signin", "/signup", "/register", "/auth", "/forgot", "/reset", "/verify"],
    "dashboard": ["/dashboard", "/app", "/console", "/admin", "/portal", "/workspace"],
    "docs": ["/docs", "/documentation", "/help", "/support", "/api", "/developers", "/guide", "/learn", "/reference"],
    "settings": ["/settings", "/preferences", "/account", "/profile", "/billing"],
    "listing": ["/products", "/listings", "/catalog", "/browse", "/explore", "/search", "/discover", "/courses", "/jobs", "/contacts", "/companies", "/deals", "/employees", "/posts", "/articles", "/playlist", "/channel", "/models"],
    "detail": ["/products/", "/listings/", "/p/", "/post/", "/article/", "/course/", "/lesson/", "/r/", "/job/", "/u/", "/users/", "/watch", "/video/", "/track/", "/album/", "/episode/", "/employees/", "/contacts/", "/companies/", "/deals/", "/models/"],
    "search": ["/search", "/find", "/explore"],
    "checkout": ["/checkout", "/cart", "/order", "/purchase"],
    "about": ["/about", "/team", "/company", "/contact", "/careers"],
    "legal": ["/terms", "/privacy", "/cookies", "/legal", "/disclaimer"],
}


SCRAPE_COVERAGE = {
    "marketing_only": "marketing_only",
    "public_app_surface": "public_app_surface",
    "authenticated_captured": "authenticated_captured",
}
