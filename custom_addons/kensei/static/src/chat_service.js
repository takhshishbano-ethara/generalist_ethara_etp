/** @odoo-module */
import { EventBus } from "@odoo/owl";
import { registry } from "@web/core/registry";

export const kenseiChatBus = new EventBus();

export const kenseiChatService = {
    dependencies: ["bus_service"],
    start(env, { bus_service }) {
        bus_service.subscribe("kensei.chat.chunk", (payload) => {
            kenseiChatBus.trigger("chunk", payload);
        });
        bus_service.subscribe("kensei.chat.done", (payload) => {
            kenseiChatBus.trigger("done", payload);
        });
        bus_service.subscribe("kensei.chat.error", (payload) => {
            kenseiChatBus.trigger("error", payload);
        });
    },
};

registry.category("services").add("kensei_chat", kenseiChatService);
