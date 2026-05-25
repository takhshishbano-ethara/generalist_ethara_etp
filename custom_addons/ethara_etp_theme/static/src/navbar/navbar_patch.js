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
        });
        document.body.classList.toggle("o_ethara_sidebar_collapsed", startCollapsed);
        document.documentElement.classList.toggle("o_ethara_dark", darkMode);
        this.applyInputStyle();
        this.loadFavorites();
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
    },

    openHome() {
        this.actionService.doAction(
            "ethara_etp_theme.ethara_favorites_home_action"
        );
    },

    onSidebarAppClick(app) {
        this.etharaState.expandedAppId = app.id;
        this.etharaState.activeMenuId = null;
        this.menuService.selectMenu(app);
    },

    onSidebarMenuClick(menu) {
        this.etharaState.activeMenuId = menu.id;
        this.menuService.selectMenu(menu);
    },

    toggleSidebar() {
        this.etharaState.collapsed = !this.etharaState.collapsed;
        this.etharaState.searchQuery = "";
        document.body.classList.toggle(
            "o_ethara_sidebar_collapsed",
            this.etharaState.collapsed
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
    },
});
