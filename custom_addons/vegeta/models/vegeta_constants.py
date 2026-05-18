

PRD_MIN_WORDS = 800
PRD_MAX_WORDS = 5000
PRD_TARGET_MIN_WORDS = 3200
PRD_TARGET_MAX_WORDS = 4800



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

VEGETA_CATEGORY_KEYS = {
    "public_utility": "Public Utility",
    "news": "News",
    "publishing": "Publishing",
    "retail": "Retail",
    "services": "Services",
    "erp": "ERP",
    "knowledge": "Knowledge",
    "procurement": "Procurement",
    "vertical_markets": "Vertical Markets",
    "hcm": "HCM",
    "crm": "CRM",
    "gov_portal": "Gov. Portal",
    "community": "Community",
    "tms": "TMS",
    "multimedia": "Multimedia",
    "ai_platform": "AI Platform",
}

VEGETA_GATED_CATEGORIES = {"erp", "procurement", "hcm", "crm", "tms", "ai_platform"}



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



TIER1_PRECEDENCE_LADDER = (
    "machine-readable API contracts > schema.org > signup fields > "
    "pricing tiers > typed pages > XHR observations > rendered content > "
    "marketing copy"
)



SCRAPE_COVERAGE = {
    "marketing_only": (
        "Crawl reached only the marketing site. Authenticated app is Tier-3. "
        "Reconstruct confidently from category emphasis and named reference brands, "
        "anchored to feature lists, plan tiers, marketing screenshots, and docs pages."
    ),
    "public_app_surface": (
        "Crawl reached public app pages. Reverse-engineer data model, routes, and "
        "API from observable pages first (Tier 1 and 2); infer only gated and admin "
        "parts (Tier 3)."
    ),
    "authenticated_captured": (
        "Crawl reached authenticated pages. Ground most sections in observation; "
        "infer the least."
    ),
}



ASCII_ONLY_BANNED_GLYPHS = {
    "\u2190", "\u2191", "\u2192", "\u2193", "\u2194", "\u21d0", "\u21d2", "\u21d4",
    "\u2013", "\u2014",
    "\u2018", "\u2019", "\u201c", "\u201d", "\u201e", "\u201a", "\u00ab", "\u00bb",
    "\u2026",
    "\u2022", "\u25cf", "\u25cb", "\u25a0", "\u25a1", "\u25c6", "\u25c7", "\u2605", "\u2606",
    "\u2713", "\u2714", "\u2717", "\u2718", "\u2611", "\u2612", "\u2610",
    "\u00d7",
}



VEGETA_CATEGORY_EMPHASIS = {
    "public_utility": {
        "core_entities": "account, service, bill/invoice, payment, application/request, document",
        "core_flows": "account lookup and linking, view and pay a bill, submit an application, track request status",
        "defining_mechanic": "citizen self-service with accessibility as a legal floor, multilingual, low-bandwidth fallbacks",
        "reference_brands": "PG&E, Con Edison, National Grid",
        "gated": False,
    },
    "news": {
        "core_entities": "article (workflow status + revisions), section/topic, author, homepage layout, subscription, comment",
        "core_flows": "browse by section, search, hit metered paywall and subscribe, editorial publish workflow, breaking-news realtime",
        "defining_mechanic": "editorial CMS plus metered access plus ad slots",
        "reference_brands": "The New York Times, The Guardian, The Washington Post",
        "gated": False,
    },
    "publishing": {
        "core_entities": "post (draft/scheduled/published/archived), author, publication, subscriber, newsletter issue",
        "core_flows": "write in a rich editor, schedule and publish, manage subscribers, send a newsletter, reader subscribes to a tier",
        "defining_mechanic": "author workspace plus subscription tiers plus email/RSS distribution",
        "reference_brands": "Substack, Ghost, Medium",
        "gated": False,
    },
    "retail": {
        "core_entities": "product, variant, cart, order, customer, inventory, return, payment",
        "core_flows": "browse and filter the catalog, product detail, add to cart, checkout and pay, track an order, request a return",
        "defining_mechanic": "catalog plus checkout plus order lifecycle plus inventory",
        "reference_brands": "Shopify storefronts, Amazon, Allbirds",
        "gated": False,
    },
    "services": {
        "core_entities": "provider, service, availability slot, appointment, customer, review",
        "core_flows": "search and compare providers, view availability, book an appointment, reschedule or cancel, reminders and no-show handling",
        "defining_mechanic": "directory plus booking calendar plus appointment lifecycle",
        "reference_brands": "Booksy, Zocdoc, Calendly",
        "gated": False,
    },
    "erp": {
        "core_entities": "org unit, project, document, KPI/metric, task, integration",
        "core_flows": "navigate the multi-module shell, manage a project, edit a doc, view KPI dashboards, run batch operations, configure integrations",
        "defining_mechanic": "multi-module shell plus org hierarchy plus granular RBAC plus audit trail",
        "reference_brands": "NetSuite, SAP S/4HANA, Odoo",
        "gated": True,
    },
    "knowledge": {
        "core_entities": "course, lesson, enrollment, quiz, question, attempt, certificate",
        "core_flows": "browse and enroll, take a lesson, track progress, take a graded quiz, earn a certificate, instructor authoring",
        "defining_mechanic": "course/lesson model plus progress tracking plus grading",
        "reference_brands": "Coursera, Udemy, Khan Academy",
        "gated": False,
    },
    "procurement": {
        "core_entities": "supplier, RFQ, bid, contract, approval, purchase order",
        "core_flows": "discover suppliers, raise an RFQ, collect multi-party bids, run an approval chain, award and contract",
        "defining_mechanic": "RFQ workflow plus multi-party bidding plus approval chains",
        "reference_brands": "Coupa, SAP Ariba, GEP",
        "gated": True,
    },
    "vertical_markets": {
        "core_entities": "listing, host, guest, booking, review, payment, payout, message thread",
        "core_flows": "search with map and filters, listing detail, book and pay, host listing management, host calendar and pricing, two-sided reviews",
        "defining_mechanic": "two-sided marketplace plus booking lifecycle plus trust and payout mechanics",
        "reference_brands": "Airbnb, Vrbo, Booking.com",
        "gated": False,
    },
    "hcm": {
        "core_entities": "employee, org unit, time-off request, payroll record, onboarding task, approval",
        "core_flows": "manage an employee record, view the org chart, request time-off through an approval chain, payroll views, run an onboarding workflow",
        "defining_mechanic": "employee record plus approval chains plus onboarding workflows",
        "reference_brands": "Workday, BambooHR, Rippling",
        "gated": True,
    },
    "crm": {
        "core_entities": "contact, company, deal, pipeline stage, activity, sequence, report",
        "core_flows": "work the pipeline, manage contacts and companies, log activities on a timeline, run sequences, view reports and dashboards",
        "defining_mechanic": "relational contact/company/deal model plus pipeline plus activity timeline",
        "reference_brands": "Salesforce, HubSpot, Pipedrive",
        "gated": True,
    },
    "gov_portal": {
        "core_entities": "citizen account, service request/case, form submission, document, identity verification, audit record",
        "core_flows": "verify identity, submit a form-driven service request, upload documents, track case status, staff case processing",
        "defining_mechanic": "form-driven service requests plus case tracking plus identity verification plus audit-grade logging",
        "reference_brands": "GOV.UK, USA.gov, Estonia e-Residency",
        "gated": False,
    },
    "community": {
        "core_entities": "post, comment (threaded), vote, user/reputation, moderation action, notification",
        "core_flows": "post and reply in threads, vote, build reputation, work a moderation queue, receive notifications",
        "defining_mechanic": "threaded discussion plus voting and reputation plus moderation tooling",
        "reference_brands": "Reddit, Hacker News, Discourse",
        "gated": False,
    },
    "tms": {
        "core_entities": "workspace, project, task, board/view, assignee, due date, dependency, comment",
        "core_flows": "create and assign tasks, switch views (kanban/list/calendar), set dependencies and due dates, collaborate via comments",
        "defining_mechanic": "tasks and projects across multiple views plus assignees plus dependencies",
        "reference_brands": "Asana, Linear, ClickUp",
        "gated": True,
    },
    "multimedia": {
        "core_entities": "media asset, playlist, channel/creator, view history, recommendation, comment",
        "core_flows": "browse and discover, play media, build playlists, view history, creator upload workspace",
        "defining_mechanic": "media catalog plus player plus recommendations plus creator workspace",
        "reference_brands": "YouTube, Spotify, Twitch",
        "gated": False,
    },
    "ai_platform": {
        "core_entities": "model, playground session, API key, usage record (tokens/cost), rate-limit policy, project",
        "core_flows": "browse the model catalog, run the playground with parameter controls, manage API keys, view usage dashboards with token and cost metering, set rate limits",
        "defining_mechanic": "model catalog plus playground plus API keys plus usage and cost metering",
        "reference_brands": "OpenAI, Anthropic, Replicate",
        "gated": True,
    },
}
