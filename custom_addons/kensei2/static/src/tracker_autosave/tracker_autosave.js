/** @odoo-module */
import { registry } from "@web/core/registry";
import { formView } from "@web/views/form/form_view";
import { FormController } from "@web/views/form/form_controller";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { useEffect } from "@odoo/owl";

// How long to wait after the last keystroke before an idle auto-save fires.
const AUTOSAVE_IDLE_MS = 3000;

/**
 * Task Allocation form with auto-save on blur / tab-switch AND on idle.
 *
 * Auto-save reuses the STANDARD save path (this.save), so it inherits everything the
 * manual Save button does — most importantly force_save, which is what lets a
 * stage-completing edit persist even though the record turns read-only in the same
 * change. It never touches the model, the status ladder, or the locks.
 *
 * Two triggers:
 *   - focusout: leaving a field (or clicking another tab) saves immediately;
 *   - idle debounce: while you type, the save is pushed out; once you PAUSE for
 *     AUTOSAVE_IDLE_MS it fires — this covers the "typed but never left the field"
 *     case that blur alone misses.
 *
 * Validation is NOT skipped: a blur that leaves the record invalid still runs the
 * checks and surfaces the error. The only difference from a manual save is WHERE the
 * error goes — auto-save must not throw a modal in the user's face, so a server-side
 * ValidationError is shown as a (non-blocking) notification instead. Client-side
 * required fields are handled by core, which flags them red. The manual Save button
 * keeps the full modal behaviour.
 */
export class Kensei2TrackerAutoSaveController extends FormController {
    setup() {
        super.setup();
        this.notification = useService("notification");
        this._autoSaving = false;
        this._idleTimer = null;

        useEffect(
            () => {
                const el = this.rootRef.el;
                if (!el) {
                    return;
                }
                // focusout bubbles to the root when focus leaves any field — covers
                // blurring a field and clicking another notebook tab.
                const onFocusOut = () => this._autoSave();
                // input bubbles as the user types; each keystroke restarts the idle
                // timer, so the save fires only once typing pauses.
                const onInput = () => this._scheduleIdleSave();
                el.addEventListener("focusout", onFocusOut);
                el.addEventListener("input", onInput);
                return () => {
                    el.removeEventListener("focusout", onFocusOut);
                    el.removeEventListener("input", onInput);
                    this._clearIdleTimer();
                };
            },
            () => []
        );
    }

    _clearIdleTimer() {
        if (this._idleTimer) {
            clearTimeout(this._idleTimer);
            this._idleTimer = null;
        }
    }

    /** (Re)start the idle countdown; the previous pending save is cancelled so a
     *  burst of keystrokes results in ONE save, AUTOSAVE_IDLE_MS after the last. */
    _scheduleIdleSave() {
        this._clearIdleTimer();
        this._idleTimer = setTimeout(() => {
            this._idleTimer = null;
            this._autoSave();
        }, AUTOSAVE_IDLE_MS);
    }

    async _autoSave() {
        // A blur (or a fresh idle tick) supersedes any pending idle save.
        this._clearIdleTimer();
        const record = this.model.root;
        if (!record || !record.dirty || this._autoSaving) {
            return;
        }
        this._autoSaving = true;
        try {
            // Let focus settle: moving between two fields fires focusout then
            // focusin, so a microtask hop avoids saving mid-transition (e.g. while a
            // many2one dropdown is open).
            await Promise.resolve();
            if (!record.dirty) {
                return;
            }
            // A brand-new record that is not yet valid: don't nag and don't persist a
            // junk row — nothing is saved, so nothing can be lost. It auto-saves once
            // it becomes a real, valid record. Existing records always validate and
            // report, which is the whole point of auto-saving them.
            if (record.isNew && !record._checkValidity({ silent: true })) {
                return;
            }
            // reload:false keeps scroll and focus where they are — a full reload on
            // every save would be jarring. Status / is_locked are already kept in
            // sync client-side by onchange.
            await this.save({ reload: false });
        } finally {
            this._autoSaving = false;
        }
    }

    async onSaveError(error, options, leaving) {
        if (this._autoSaving) {
            const message = error.data?.message || error.message || String(error);
            this.notification.add(message, { title: _t("Not saved"), type: "danger" });
            return;
        }
        return super.onSaveError(error, options, leaving);
    }
}

export const kensei2TrackerAutoSaveView = {
    ...formView,
    Controller: Kensei2TrackerAutoSaveController,
};

registry.category("views").add("kensei2_tracker_autosave", kensei2TrackerAutoSaveView);
