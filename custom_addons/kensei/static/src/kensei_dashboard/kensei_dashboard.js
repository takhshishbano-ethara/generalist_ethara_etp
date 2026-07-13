/** @odoo-module */
import { Component, onWillStart, useState, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { KenseiTrackerDashboard } from "@kensei/tracker_dashboard/tracker_dashboard";
import { KenseiTaskerDashboard } from "@kensei/tasker_dashboard/tasker_dashboard";

/**
 * Single "Dashboard" entry point. Menu-level `groups` can't express "taskers but
 * not QL/PL" (no negation on menuitems), so one menu points here and we pick the
 * view by role: QL / PL / admin get the org overview, a plain tasker gets their
 * personal dashboard.
 */
export class KenseiDashboard extends Component {
    static template = xml`
        <t t-if="state.ready">
            <KenseiTrackerDashboard t-if="state.isLead" action="props.action"/>
            <KenseiTaskerDashboard t-else="" action="props.action"/>
        </t>`;
    static components = { KenseiTrackerDashboard, KenseiTaskerDashboard };
    static props = { "*": true };

    setup() {
        this.state = useState({ ready: false, isLead: false });
        onWillStart(async () => {
            this.state.isLead =
                (await user.hasGroup("kensei.group_kensei_ql")) ||
                (await user.hasGroup("base.group_system"));
            this.state.ready = true;
        });
    }
}

registry.category("actions").add("kensei_dashboard", KenseiDashboard);
