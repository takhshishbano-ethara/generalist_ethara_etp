/** @odoo-module */
import { EventBus } from "@odoo/owl";
import { registry } from "@web/core/registry";

export const talosChatBus = new EventBus();

export const talosChatService = {
    dependencies: ["bus_service"],
    start(env, { bus_service }) {
        bus_service.subscribe("talos.chat.chunk", (payload) => {
            talosChatBus.trigger("chunk", payload);
        });
        bus_service.subscribe("talos.chat.done", (payload) => {
            talosChatBus.trigger("done", payload);
        });
        bus_service.subscribe("talos.chat.error", (payload) => {
            talosChatBus.trigger("error", payload);
        });
    },
};

registry.category("services").add("talos_chat", talosChatService);
