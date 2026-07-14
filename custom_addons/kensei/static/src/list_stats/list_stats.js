/** @odoo-module */
import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useBus } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";

/**
 * The stat strip above a list view.
 *
 * Presentational; the controller owns fetching and filtering. Each cell is a real
 * control — clicking it narrows the list to the rows it counts — so cells that
 * cannot narrow anything (the total, an average) render inert rather than
 * pretending to be clickable.
 */
export class KenseiListStats extends Component {
    static template = "kensei.ListStats";
    static props = {
        cards: { type: Array },
        loading: { type: Boolean, optional: true },
        onCardClick: { type: Function, optional: true },
    };

    /** "—" for a metric with no data, so an empty cell never reads as a zero. */
    display(card) {
        if (card.value === null || card.value === undefined || card.value === "") {
            return "—";
        }
        return card.suffix ? `${card.value}${card.suffix}` : `${card.value}`;
    }

    /**
     * The share lives here rather than as another mark on the card.
     *
     * It is genuinely useful — "15 failed" means little without "of 128" — but the
     * Daily Tracker's card anatomy is icon + value + label, and adding a bar would
     * break the very consistency this design exists to keep. On hover costs
     * nothing and stays out of the way.
     */
    tooltip(card) {
        const parts = [];
        if (card.share !== null && card.share !== undefined) {
            parts.push(`${Math.round(card.share * 100)}% of total`);
        }
        if (this.isClickable(card)) {
            parts.push(`Click to filter by ${card.label}`);
        }
        return parts.join(" · ");
    }

    isClickable(card) {
        return Boolean(card.domain && card.domain.length);
    }

    // Clickable cells are real <button>s, so Enter/Space come for free — no
    // keydown handler, no tabindex, no role. That is the whole reason to reach for
    // the right element instead of a <div>.
    onClick(card) {
        if (this.isClickable(card) && this.props.onCardClick) {
            this.props.onCardClick(card);
        }
    }
}

/**
 * A ListController that shows a stat strip above the rows.
 *
 * ONE js_class serves all three lists (Task Allocation, Personas, Team
 * Management): the stats source is derived from the view's own resModel and the
 * server picks the matching builder from its whitelist. A fourth list is a
 * js_class attribute plus a dict entry — no new JS.
 *
 * The figures track the SEARCH DOMAIN, not just the initial load: numbers that
 * contradict the rows beneath them are worse than no numbers at all.
 */
export class KenseiStatsListController extends ListController {
    static template = "kensei.StatsListView";
    static components = { ...ListController.components, KenseiListStats };

    setup() {
        super.setup();
        this.statsState = useState({ cards: [], loading: true });
        // Guards a slow early response from overwriting a fresh one when the user
        // changes filters quickly.
        this._statsSeq = 0;

        this._loadStats();
        useBus(this.env.searchModel, "update", () => this._loadStats());
    }

    async _loadStats() {
        const seq = ++this._statsSeq;
        this.statsState.loading = true;
        try {
            const res = await rpc("/kensei/tracker/list_stats", {
                model: this.props.resModel,
                domain: this.env.searchModel.domain,
            });
            if (seq !== this._statsSeq) {
                return; // stale — a newer search already went out
            }
            this.statsState.cards = (res && res.cards) || [];
        } catch (e) {
            if (seq !== this._statsSeq) {
                return;
            }
            // The list itself still works, so this must never be fatal: drop the
            // strip and let the rows speak for themselves.
            this.statsState.cards = [];
            console.warn("[kensei-list-stats]", e);
        } finally {
            if (seq === this._statsSeq) {
                this.statsState.loading = false;
            }
        }
    }

    /**
     * Clicking a cell narrows the list to the rows it counts.
     *
     * Goes through the SearchModel rather than mutating the domain directly, so it
     * lands as a visible facet in the search bar — the user can see what happened
     * and undo it with the same "x" as any other filter. A silent domain change
     * they cannot see or remove would be a trap.
     */
    onCardClick(card) {
        if (!card.domain || !card.domain.length) {
            return;
        }
        this.env.searchModel.createNewFilters([{
            description: card.label,
            domain: JSON.stringify(card.domain),
        }]);
    }
}

registry.category("views").add("kensei_stats_list", {
    ...listView,
    Controller: KenseiStatsListController,
});
