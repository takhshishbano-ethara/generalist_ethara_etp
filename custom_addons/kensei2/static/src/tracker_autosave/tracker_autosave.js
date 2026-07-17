/** @odoo-module */
import { registry } from "@web/core/registry";
import { formView } from "@web/views/form/form_view";
import { FormController } from "@web/views/form/form_controller";
import { RelationalModel } from "@web/model/relational_model/relational_model";
import { Record } from "@web/model/relational_model/record";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { useEffect } from "@odoo/owl";

// Auto-save cadence: persist a valid, dirty record at most this often.
const AUTOSAVE_INTERVAL_MS = 3000;

/**
 * Task Allocation record.
 *
 * Validity is UNCHANGED (so auto-save can reliably tell a saveable record from an
 * incomplete one) — we only suppress the generic "Missing required fields" toast. On
 * this form the highlighted red box (and its red "*") IS the message; no dialog in the
 * user's face. Scoped to this view's Model, so no other form changes.
 */
export class Kensei2TrackerRecord extends Record {
    _displayInvalidFieldNotification() {
        // No-op closer — honours the base contract (the caller stores and may invoke
        // the return value to dismiss the toast).
        return () => {};
    }
}

export class Kensei2TrackerModel extends RelationalModel {}
Kensei2TrackerModel.Record = Kensei2TrackerRecord;

/**
 * Task Allocation form auto-save.
 *
 * PRINCIPLE: auto-save PERSISTS a valid record, and otherwise HOLDS the user's edits in
 * the form (nothing is lost). It never pushes an incomplete/invalid record to the
 * backend — so the server's stage @api.constrains are never hit by a half-filled state,
 * and there is no repeated-error loop. Odoo already sends only the CHANGED fields on
 * save (a diff), but the record is validated as a whole, so a save can only go through
 * once the record is in a consistent state.
 *
 * Triggers:
 *   - focusout: leaving a field / switching tab — save if valid, else flag what's missing;
 *   - 3s tick: persist a valid, dirty record (a no-op once saved, or while invalid).
 * A save the SERVER rejects is not retried on the timer until the user edits again.
 */
export class Kensei2TrackerAutoSaveController extends FormController {
    setup() {
        super.setup();
        this.notification = useService("notification");
        this._autoSaving = false;
        this._autoSaveInterval = null;
        // A server-rejected save is not retried by the timer until the user edits again
        // (prevents an unmirrorable constraint error, e.g. an out-of-range score, from
        // hammering the backend every 3s).
        this._saveBlocked = false;
        // Throttles the "what's still missing" notification to once per distinct set.
        this._lastMissingKey = null;

        useEffect(
            () => {
                const el = this.rootRef.el;
                if (!el) {
                    return;
                }
                // focusout bubbles to the root when focus leaves any field — covers
                // blurring a field and clicking another notebook tab.
                const onFocusOut = () => this._autoSave({ fromBlur: true });
                // Any fresh edit clears a previous server rejection, so the next valid
                // state can be retried. input covers typing; change covers dropdowns.
                const onEdit = () => {
                    this._saveBlocked = false;
                };
                el.addEventListener("focusout", onFocusOut);
                el.addEventListener("input", onEdit);
                el.addEventListener("change", onEdit);
                this._autoSaveInterval = setInterval(
                    () => this._autoSave(),
                    AUTOSAVE_INTERVAL_MS
                );
                return () => {
                    el.removeEventListener("focusout", onFocusOut);
                    el.removeEventListener("input", onEdit);
                    el.removeEventListener("change", onEdit);
                    if (this._autoSaveInterval) {
                        clearInterval(this._autoSaveInterval);
                        this._autoSaveInterval = null;
                    }
                };
            },
            () => []
        );
    }

    async _autoSave({ fromBlur = false } = {}) {
        const record = this.model.root;
        if (!record || this._autoSaving || this._saveBlocked) {
            return;
        }
        this._autoSaving = true;
        try {
            // isDirty() flushes any pending in-field edit into the record first (the
            // bare `dirty` property stays false while a field is still focused), then
            // reports dirtiness. See record.js:57.
            if (!(await record.isDirty())) {
                return;
            }
            // Only push a VALID record. An incomplete one — a missing required field,
            // or a gate marked Done/Failed without its data — is held in the form
            // (nothing lost) and NEVER sent to the backend, so no constraint fires and
            // there is no error loop. On a click-away we point at what's missing.
            if (!record._checkValidity({ silent: true })) {
                if (fromBlur) {
                    this._flagInvalid(record);
                }
                return;
            }
            // reload:false keeps scroll and focus where they are.
            await this.save({ reload: false });
            this._lastMissingKey = null;
        } finally {
            this._autoSaving = false;
        }
    }

    /**
     * Loud feedback when the user clicks away from a form that can't be saved yet:
     * paint the offending inputs red and name them in a notification. Throttled to once
     * per distinct set of missing fields so moving around the form doesn't spam it.
     */
    _flagInvalid(record) {
        // Non-silent check populates record._invalidFields (the red boxes / red "*");
        // our _displayInvalidFieldNotification is a no-op, so no generic toast fires —
        // we raise a targeted one below.
        record._checkValidity();
        const missing = [...record._invalidFields];
        if (!missing.length) {
            return;
        }
        const key = missing.slice().sort().join(",");
        if (key === this._lastMissingKey) {
            return;
        }
        this._lastMissingKey = key;
        const labels = missing.map((name) => record.fields[name]?.string || name);
        this.notification.add(_t("Fill in %s to save.", labels.join(", ")), {
            title: _t("Not saved yet"),
            type: "warning",
        });
        // Bring the first missing field into view once the red class has rendered. No
        // forced focus — that would hijack wherever the user just clicked.
        setTimeout(() => {
            const el = this.rootRef.el?.querySelector(".o_field_invalid");
            el?.scrollIntoView({ behavior: "smooth", block: "center" });
        }, 50);
    }

    async onSaveError(error, options, leaving) {
        if (this._autoSaving) {
            // Don't let the 3s timer re-push this exact rejected state — wait for the
            // next edit (which clears _saveBlocked via the input/change listener).
            this._saveBlocked = true;
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
    Model: Kensei2TrackerModel,
};

registry.category("views").add("kensei2_tracker_autosave", kensei2TrackerAutoSaveView);
