/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { NavBar } from "@web/webclient/navbar/navbar";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";
import { useState } from "@odoo/owl";
import { resolveAppIcon } from "@ethara_etp_theme/utils/menu_icon";

patch(NavBar.prototype, {
    setup() {
        super.setup();
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.notification = useService("notification");
        const startCollapsed =
            getComputedStyle(document.documentElement)
                .getPropertyValue("--ethara-sidebar-start")
                .trim() === "collapsed";
        const darkMode = localStorage.getItem("ethara_dark_mode") === "1";
        this.etharaState = useState({
            collapsed: startCollapsed,
            expandedAppId: this.menuService.getCurrentApp()?.id ?? null,
            expandedNodes: {},
            activeMenuId: null,
            searchQuery: "",
            darkMode: darkMode,
            favoriteIds: [],
            activitiesCount: 0,
            conversationsCount: 0,
            mobileOpen: false,
        });
        document.body.classList.toggle("o_ethara_sidebar_collapsed", startCollapsed);
        document.documentElement.classList.toggle("o_ethara_dark", darkMode);
        this.applyInputStyle();
        this.loadFavorites();
        this.loadQuickBadges();
    },

    applyInputStyle() {
        const style =
            getComputedStyle(document.documentElement)
                .getPropertyValue("--ethara-input-style")
                .trim() || "soft";
        const root = document.documentElement;
        for (const name of ["outlined", "filled", "underline", "soft"]) {
            root.classList.toggle("o_ethara_input_" + name, name === style);
        }
    },

    get sidebarApps() {
        return this.menuService.getApps();
    },

    get etharaUserName() {
        return user.name || "User";
    },

    get etharaUserInitials() {
        const parts = (user.name || "U").trim().split(/\s+/);
        const initials = (parts[0]?.[0] || "") + (parts[1]?.[0] || "");
        return initials.toUpperCase() || "U";
    },

    get etharaCompanyName() {
        return user.activeCompany?.name || "Ethara";
    },

    get etharaCompanyLogo() {
        const companyId = user.activeCompany?.id;
        return companyId
            ? `/web/binary/company_logo?company=${companyId}`
            : "/web/binary/company_logo";
    },

    get etharaUserAvatar() {
        return user.userId
            ? `/web/image/res.users/${user.userId}/avatar_128`
            : "/web/static/img/user_menu_avatar.png";
    },

    get etharaGreeting() {
        const h = new Date().getHours();
        if (h < 12) return "Good Morning";
        if (h < 17) return "Good Afternoon";
        return "Good Evening";
    },

    getAppSections(app) {
        return this.menuService.getMenuAsTree(app.id).childrenTree || [];
    },

    get etharaSearchResults() {
        const query = (this.etharaState.searchQuery || "").trim().toLowerCase();
        if (!query) {
            return [];
        }
        const results = [];
        const walk = (node, trail) => {
            const name = (node.name || "").toString();
            if (node.actionID && name.toLowerCase().includes(query)) {
                results.push({ node, path: trail.join("  /  ") });
            }
            const nextTrail = name ? trail.concat(name) : trail;
            for (const child of node.childrenTree || []) {
                walk(child, nextTrail);
            }
        };
        for (const app of this.menuService.getApps()) {
            walk(this.menuService.getMenuAsTree(app.id), []);
        }
        return results.slice(0, 30);
    },

    onSearchInput(ev) {
        this.etharaState.searchQuery = ev.target.value;
    },

    clearSearch() {
        this.etharaState.searchQuery = "";
    },

    onSearchResultClick(node) {
        this.etharaState.searchQuery = "";
        this.etharaState.activeMenuId = node.id;
        if (node.appID != null) {
            this.etharaState.expandedAppId = node.appID;
        }
        this.menuService.selectMenu(node);
        this.closeMobileSidebar();
    },

    getAppIcon(app) {
        return resolveAppIcon(app);
    },

    isAppActive(app) {
        return this.menuService.getCurrentApp() === app;
    },

    isSectionExpanded(app) {
        return this.etharaState.expandedAppId === app.id;
    },

    toggleSection(app) {
        this.etharaState.expandedAppId = this.isSectionExpanded(app) ? null : app.id;
    },

    isNodeExpanded(node) {
        return this.etharaState.expandedNodes[node.id] === true;
    },

    toggleNode(node) {
        this.etharaState.expandedNodes[node.id] = !this.isNodeExpanded(node);
    },

    isMenuActive(node) {
        return this.etharaState.activeMenuId === node.id;
    },

    isSubgroupActive(node) {
        const activeId = this.etharaState.activeMenuId;
        if (activeId == null) {
            return false;
        }
        const contains = (n) =>
            (n.childrenTree || []).some(
                (child) => child.id === activeId || contains(child)
            );
        return contains(node);
    },

    async loadFavorites() {
        try {
            this.etharaState.favoriteIds = await this.orm.call(
                "ethara.menu.favorite",
                "get_favorite_menu_ids",
                []
            );
        } catch {
            this.etharaState.favoriteIds = [];
        }
    },

    get favoriteMenus() {
        const menus = [];
        for (const id of this.etharaState.favoriteIds) {
            const menu = this.menuService.getMenu(id);
            if (menu) {
                menus.push(menu);
            }
        }
        return menus;
    },

    isFavorite(node) {
        return !!node && this.etharaState.favoriteIds.includes(node.id);
    },

    async toggleFavorite(node) {
        try {
            await this.orm.call(
                "ethara.menu.favorite",
                "toggle_favorite",
                [node.id]
            );
        } finally {
            await this.loadFavorites();
        }
    },

    getFavoriteIcon(menu) {
        const app = this.menuService.getMenu(menu.appID) || menu;
        return resolveAppIcon(app);
    },

    openFavorite(menu) {
        this.etharaState.activeMenuId = menu.id;
        if (menu.appID != null) {
            this.etharaState.expandedAppId = menu.appID;
        }
        this.menuService.selectMenu(menu);
        this.closeMobileSidebar();
    },

    openHome() {
        this.actionService.doAction(
            "ethara_etp_theme.ethara_favorites_home_action"
        );
        this.closeMobileSidebar();
    },

    onSidebarAppClick(app) {
        this.etharaState.expandedAppId = app.id;
        this.etharaState.activeMenuId = null;
        this.menuService.selectMenu(app);
        if (!this.getAppSections(app).length) {
            this.closeMobileSidebar();
        }
    },

    onSidebarMenuClick(menu) {
        this.etharaState.activeMenuId = menu.id;
        this.menuService.selectMenu(menu);
        this.closeMobileSidebar();
    },

    toggleMobileSidebar() {
        this.etharaState.mobileOpen = !this.etharaState.mobileOpen;
        document.body.classList.toggle(
            "o_ethara_mobile_open",
            this.etharaState.mobileOpen
        );
    },

    closeMobileSidebar() {
        if (!this.etharaState.mobileOpen) {
            return;
        }
        this.etharaState.mobileOpen = false;
        document.body.classList.remove("o_ethara_mobile_open");
    },

    toggleSidebar() {
        this.etharaState.collapsed = !this.etharaState.collapsed;
        this.etharaState.searchQuery = "";
        document.body.classList.toggle(
            "o_ethara_sidebar_collapsed",
            this.etharaState.collapsed
        );
        this.etharaNotify(
            this.etharaState.collapsed ? "Sidebar collapsed" : "Sidebar expanded"
        );
    },

    toggleDarkMode() {
        this.etharaState.darkMode = !this.etharaState.darkMode;
        document.documentElement.classList.toggle(
            "o_ethara_dark",
            this.etharaState.darkMode
        );
        localStorage.setItem(
            "ethara_dark_mode",
            this.etharaState.darkMode ? "1" : "0"
        );
        this.etharaNotify(
            this.etharaState.darkMode ? "Dark mode on" : "Light mode on"
        );
    },

    etharaNotify(message, options = {}) {
        if (!this.notification) {
            return;
        }
        this.notification.add(message, { type: "success", ...options });
    },

    get etharaIsDebug() {
        return /[?&]debug=([^&]+)/.test(window.location.search);
    },

    onQuickLanguage() {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "res.users",
            res_id: user.userId,
            views: [[false, "form"]],
            target: "new",
            name: "Preferences",
        });
        this.etharaNotify(`Current language: ${user.lang || "—"}`, {
            type: "info",
        });
    },

    onQuickCompany() {
        const companyName = user.activeCompany?.name || "—";
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "res.users",
            res_id: user.userId,
            views: [[false, "form"]],
            target: "new",
            name: "Preferences",
        });
        this.etharaNotify(`Active company: ${companyName}`, { type: "info" });
    },

    onQuickDebug() {
        const url = new URL(window.location.href);
        const enabling = !this.etharaIsDebug;
        if (enabling) {
            url.searchParams.set("debug", "1");
        } else {
            url.searchParams.delete("debug");
        }
        this.etharaNotify(
            enabling ? "Enabling debug mode…" : "Disabling debug mode…"
        );
        window.location.href = url.toString();
    },

    onQuickThemeSettings() {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "res.config.settings",
            views: [[false, "form"]],
            target: "inline",
            context: { module: "ethara_etp_theme" },
        });
        this.etharaNotify("Opening theme settings…", { type: "info" });
    },

    async onQuickConversations() {
        try {
            await this.actionService.doAction("mail.action_discuss");
        } catch {
            this.etharaNotify("Discuss is not installed", { type: "warning" });
        }
    },

    onQuickActivities() {
        try {
            this.actionService.doAction({
                type: "ir.actions.act_window",
                name: "My Activities",
                res_model: "mail.activity",
                views: [[false, "list"], [false, "form"]],
                domain: [["user_id", "=", user.userId]],
                target: "current",
            });
        } catch {
            this.etharaNotify("Activities are not available", { type: "warning" });
        }
    },

    async loadQuickBadges() {
        try {
            const activities = await this.orm.searchCount(
                "mail.activity",
                [["user_id", "=", user.userId]]
            );
            this.etharaState.activitiesCount = activities || 0;
        } catch {
            this.etharaState.activitiesCount = 0;
        }
        try {
            const unread = await this.orm.searchCount(
                "mail.notification",
                [
                    ["res_partner_id", "=", user.partnerId],
                    ["is_read", "=", false],
                ]
            );
            this.etharaState.conversationsCount = unread || 0;
        } catch {
            this.etharaState.conversationsCount = 0;
        }
    },
});
