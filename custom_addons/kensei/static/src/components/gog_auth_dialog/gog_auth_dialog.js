/** @odoo-module */
import { Component, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

const AUTH_STEPS = {
    IDLE: "idle",
    LOADING: "loading",
    WAITING_CLICK: "waiting_click",
    WAITING_REDIRECT: "waiting_redirect",
    EXCHANGING: "exchanging",
    DONE: "done",
    ERROR: "error",
};

export class GogAuthDialog extends Component {
    static template = "kensei.GogAuthDialog";
    static props = {
        taskId: Number,
        email: { type: String, optional: true },
        onClose: Function,
        onSuccess: Function,
    };

    setup() {
        this.state = useState({
            step: AUTH_STEPS.IDLE,
            authUrl: "",
            redirectUrl: "",
            error: "",
            email: this.props.email || "",
        });
    }

    async onStartAuth() {
        this.state.step = AUTH_STEPS.LOADING;
        this.state.error = "";
        console.log("[GogAuth] Starting auth for task=%d email=%s", this.props.taskId, this.props.email);

        try {
            const result = await rpc("/kensei/gog/start_auth", {
                task_id: this.props.taskId,
            });
            console.log("[GogAuth] start_auth response:", result);

            if (result.error) {
                console.error("[GogAuth] start_auth error:", result.error);
                this.state.step = AUTH_STEPS.ERROR;
                this.state.error = result.error;
                return;
            }

            this.state.authUrl = result.auth_url;
            this.state.email = result.email || this.state.email;
            this.state.step = AUTH_STEPS.WAITING_CLICK;
            console.log("[GogAuth] Auth URL received, waiting for user to click. email=%s", this.state.email);
        } catch (e) {
            console.error("[GogAuth] start_auth exception:", e);
            this.state.step = AUTH_STEPS.ERROR;
            this.state.error = e.data?.message || e.message || "Failed to start auth";
        }
    }

    onOpenAuthUrl() {
        if (this.state.authUrl) {
            console.log("[GogAuth] Opening auth URL in new tab:", this.state.authUrl);
            window.open(this.state.authUrl, "_blank", "noopener,noreferrer");
            this.state.step = AUTH_STEPS.WAITING_REDIRECT;
            console.log("[GogAuth] Waiting for user to paste redirect URL");
        }
    }

    async onExchangeToken() {
        const url = this.state.redirectUrl.trim();
        if (!url) {
            this.state.error = "Please paste the redirect URL";
            return;
        }

        this.state.step = AUTH_STEPS.EXCHANGING;
        this.state.error = "";
        console.log("[GogAuth] Exchanging token for task=%d redirect_url=%s", this.props.taskId, url);

        try {
            const result = await rpc("/kensei/gog/exchange_token", {
                task_id: this.props.taskId,
                redirect_url: url,
            });
            console.log("[GogAuth] exchange_token response:", result);

            if (result.error) {
                console.error("[GogAuth] exchange_token error:", result.error);
                this.state.step = AUTH_STEPS.ERROR;
                this.state.error = result.error;
                return;
            }

            console.log("[GogAuth] Token exchange SUCCESS for email=%s", result.email);
            this.state.step = AUTH_STEPS.DONE;
            setTimeout(() => {
                this.props.onSuccess();
            }, 1500);
        } catch (e) {
            console.error("[GogAuth] exchange_token exception:", e);
            this.state.step = AUTH_STEPS.ERROR;
            this.state.error = e.data?.message || e.message || "Token exchange failed";
        }
    }

    onRedirectUrlInput(ev) {
        this.state.redirectUrl = ev.target.value;
    }

    onRetry() {
        console.log("[GogAuth] Retrying auth flow");
        this.state.step = AUTH_STEPS.IDLE;
        this.state.error = "";
        this.state.authUrl = "";
        this.state.redirectUrl = "";
    }

    get isLoading() {
        return this.state.step === AUTH_STEPS.LOADING || this.state.step === AUTH_STEPS.EXCHANGING;
    }
}
