/** @odoo-module */
import { EventBus } from "@odoo/owl";
import { registry } from "@web/core/registry";

export const skollChatBus = new EventBus();

export const skollChatService = {
    dependencies: ["bus_service"],
    start(env, { bus_service }) {
        bus_service.subscribe("skoll.chat.chunk", (payload) => {
            skollChatBus.trigger("chunk", payload);
        });
        bus_service.subscribe("skoll.chat.done", (payload) => {
            skollChatBus.trigger("done", payload);
        });
        bus_service.subscribe("skoll.chat.error", (payload) => {
            skollChatBus.trigger("error", payload);
        });
    },
};

registry.category("services").add("skoll_chat", skollChatService);
