/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { resolveAppIcon } from "@ethara_etp_theme/utils/menu_icon";

class EtharaFavoritesHome extends Component {
    static template = "ethara_etp_theme.FavoritesHome";
    static props = ["*"];

    setup() {
        this.menuService = useService("menu");
        this.orm = useService("orm");
        const now = new Date();
        this.state = useState({
            favorites: [],
            time: this._formatTime(now),
            date: this._formatDate(now),
            bgUrl: "",
            bgMime: "",
        });
        onWillStart(() => Promise.all([this.loadFavorites(), this.loadBackground()]));
        onMounted(() => {
            this._clockTimer = setInterval(() => {
                const d = new Date();
                this.state.time = this._formatTime(d);
                this.state.date = this._formatDate(d);
            }, 1000);
        });
        onWillUnmount(() => {
            if (this._clockTimer) {
                clearInterval(this._clockTimer);
                this._clockTimer = null;
            }
        });
    }

    _formatTime(d) {
        const pad = (n) => String(n).padStart(2, "0");
        let h = d.getHours();
        const ampm = h >= 12 ? "PM" : "AM";
        h = h % 12 || 12;
        return `${pad(h)}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${ampm}`;
    }

    _formatDate(d) {
        return d.toLocaleDateString(undefined, {
            weekday: "long",
            year: "numeric",
            month: "long",
            day: "numeric",
        });
    }

    get userName() {
        return (user.name || "").split(" ")[0] || "there";
    }

    async loadBackground() {
        try {
            const res = await this.orm.call("ethara.theme", "get_favorites_bg", []);
            if (res && res.url) {
                this.state.bgUrl = res.url;
                this.state.bgMime = res.mime || "";
            } else {
                this.state.bgUrl = "";
                this.state.bgMime = "";
            }
        } catch {
            this.state.bgUrl = "";
            this.state.bgMime = "";
        }
    }

    get bgIsVideo() {
        return (this.state.bgMime || "").startsWith("video/");
    }

    async loadFavorites() {
        let ids = [];
        try {
            ids = await this.orm.call(
                "ethara.menu.favorite",
                "get_favorite_menu_ids",
                []
            );
        } catch {
            ids = [];
        }
        const favorites = [];
        for (const id of ids) {
            const menu = this.menuService.getMenu(id);
            if (menu) {
                favorites.push(menu);
            }
        }
        this.state.favorites = favorites;
    }

    getIcon(menu) {
        const app = this.menuService.getMenu(menu.appID) || menu;
        return resolveAppIcon(app);
    }

    getAppName(menu) {
        const app = this.menuService.getMenu(menu.appID);
        return app && app.id !== menu.id ? app.name : "";
    }

    openMenu(menu) {
        this.menuService.selectMenu(menu);
    }

    async removeFavorite(menu) {
        try {
            await this.orm.call(
                "ethara.menu.favorite",
                "toggle_favorite",
                [menu.id]
            );
        } finally {
            await this.loadFavorites();
        }
    }
}

registry.category("actions").add("ethara_favorites_home", EtharaFavoritesHome);
