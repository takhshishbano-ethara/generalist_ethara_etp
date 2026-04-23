/** @odoo-module */
import { EventBus } from "@odoo/owl";
import { registry } from "@web/core/registry";

export const atlasChatBus = new EventBus();

export const atlasChatService = {
    dependencies: ["bus_service"],
    start(env, { bus_service }) {
        bus_service.subscribe("atlas.chat.chunk", (payload) => {
            atlasChatBus.trigger("chunk", payload);
        });
        bus_service.subscribe("atlas.chat.done", (payload) => {
            atlasChatBus.trigger("done", payload);
        });
        bus_service.subscribe("atlas.chat.error", (payload) => {
            atlasChatBus.trigger("error", payload);
        });
    },
};

registry.category("services").add("atlas_chat", atlasChatService);
