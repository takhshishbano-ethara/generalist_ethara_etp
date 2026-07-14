/** @odoo-module */

import { registry } from "@web/core/registry";

/**
 * Pops a success toast when a Kensei2 Tracker team member is added.
 * The backend (kensei2.tracker.team.member.create) pushes a
 * "kensei2/team_member_added" message to the current user's bus channel.
 */
const teamMemberNotificationService = {
    dependencies: ["bus_service", "notification"],

    start(_env, { bus_service, notification }) {
        bus_service.subscribe("kensei2/team_member_added", (payload) => {
            const name = payload.name || "Member";
            const role = payload.role ? ` (${payload.role})` : "";
            notification.add(`${name}${role} added to the Kensei2 team.`, {
                type: "success",
            });
        });

        bus_service.subscribe("kensei2/team_member_updated", (payload) => {
            const count = payload.count || 1;
            const message =
                count > 1
                    ? `${count} team members updated.`
                    : `${payload.name || "Team member"} updated.`;
            notification.add(message, { type: "success" });
        });
    },
};

registry
    .category("services")
    .add("kensei2_team_member_notification", teamMemberNotificationService);
