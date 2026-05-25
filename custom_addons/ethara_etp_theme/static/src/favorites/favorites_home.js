/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { resolveAppIcon } from "@ethara_etp_theme/utils/menu_icon";

class EtharaFavoritesHome extends Component {
    static template = "ethara_etp_theme.FavoritesHome";
    static props = ["*"];

    setup() {
        this.menuService = useService("menu");
        this.orm = useService("orm");
        this.state = useState({ favorites: [] });
        onWillStart(() => this.loadFavorites());
    }

    get userName() {
        return (user.name || "").split(" ")[0] || "there";
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
