/** @odoo-module */
import { EventBus } from "@odoo/owl";
import { registry } from "@web/core/registry";

export const kensei2ChatBus = new EventBus();

export const kensei2ChatService = {
    dependencies: ["bus_service"],
    start(env, { bus_service }) {
        bus_service.subscribe("kensei2.chat.chunk", (payload) => {
            kensei2ChatBus.trigger("chunk", payload);
        });
        bus_service.subscribe("kensei2.chat.done", (payload) => {
            kensei2ChatBus.trigger("done", payload);
        });
        bus_service.subscribe("kensei2.chat.error", (payload) => {
            kensei2ChatBus.trigger("error", payload);
        });
    },
};

registry.category("services").add("kensei2_chat", kensei2ChatService);
