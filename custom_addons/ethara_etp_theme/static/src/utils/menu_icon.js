/** @odoo-module **/

export function resolveAppIcon(app) {
    const override = (window.etharaMenuIcons || {})[app && app.id];
    if (override) {
        if (override.type === "image" && override.src) {
            return { type: "image", src: override.src };
        }
        if (override.type === "font" && override.iconClass) {
            return {
                type: "font",
                iconClass: override.iconClass,
                color: override.color || "#ffffff",
                backgroundColor:
                    override.backgroundColor || "var(--ethara-primary)",
            };
        }
    }
    if (app && app.webIconData) {
        const data = app.webIconData;
        let src = data;
        if (!data.startsWith("data:image")) {
            const prefix = data.startsWith("P")
                ? "data:image/svg+xml;base64,"
                : "data:image/png;base64,";
            src = prefix + data.replace(/\s/g, "");
        }
        return { type: "image", src };
    }
    const [iconClass, color, backgroundColor] = ((app && app.webIcon) || "").split(",");
    if (backgroundColor !== undefined) {
        return {
            type: "font",
            iconClass: (iconClass || "").trim() || "fa fa-folder",
            color: (color || "#ffffff").trim(),
            backgroundColor: (backgroundColor || "").trim() || "var(--ethara-primary)",
        };
    }
    return { type: "image", src: "/web/static/img/default_icon_app.png" };
}
