/** @odoo-module */

import { EventBus } from "@odoo/owl";
import { registry } from "@web/core/registry";

/**
 * One app-wide subscription to the judge's bus channel, re-broadcast on this EventBus.
 *
 * The background worker pushes "prompt_qc.stream" notifications to the run owner's partner
 * channel (delta chunks while the judge types, then one terminal "done" event). Subscribing once
 * here and re-emitting on a plain OWL EventBus lets any number of open QC form views listen — and
 * cleanly stop listening on unmount via `useBus` — without each component touching the bus service.
 *
 * Fire-forward: there is no replay. A view that opens mid-run sees tokens only from when it
 * subscribed; the durable verdict is always loaded from the record on the terminal event.
 */
export const promptQcStreamBus = new EventBus();

export const promptQcStreamService = {
    dependencies: ["bus_service"],
    start(env, { bus_service }) {
        bus_service.subscribe("prompt_qc.stream", (payload) => {
            promptQcStreamBus.trigger("message", payload);
        });
    },
};

registry.category("services").add("prompt_qc_stream", promptQcStreamService);
