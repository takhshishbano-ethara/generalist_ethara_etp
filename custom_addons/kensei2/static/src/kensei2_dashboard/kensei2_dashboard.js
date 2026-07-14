/** @odoo-module */
import { Component, onWillStart, useState, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { Kensei2TrackerDashboard } from "@kensei2/tracker_dashboard/tracker_dashboard";
import { Kensei2TaskerDashboard } from "@kensei2/tasker_dashboard/tasker_dashboard";

/**
 * Single "Dashboard" entry point. Menu-level `groups` can't express "taskers but
 * not QL/PL" (no negation on menuitems), so one menu points here and we pick the
 * view by role: QL / PL / admin get the org overview, a plain tasker gets their
 * personal dashboard.
 */
export class Kensei2Dashboard extends Component {
    static template = xml`
        <t t-if="state.ready">
            <Kensei2TrackerDashboard t-if="state.isLead" action="props.action"/>
            <Kensei2TaskerDashboard t-else="" action="props.action"/>
        </t>`;
    static components = { Kensei2TrackerDashboard, Kensei2TaskerDashboard };
    static props = { "*": true };

    setup() {
        this.state = useState({ ready: false, isLead: false });
        onWillStart(async () => {
            this.state.isLead =
                (await user.hasGroup("kensei2.group_kensei2_ql")) ||
                (await user.hasGroup("base.group_system"));
            this.state.ready = true;
        });
    }
}

registry.category("actions").add("kensei2_dashboard", Kensei2Dashboard);
