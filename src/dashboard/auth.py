from __future__ import annotations

import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, session

from src.dashboard.i18n import (
    DEFAULT_LOCALE,
    LOCALE_STORAGE_KEY,
    LOCALE_TIMESTAMP_STORAGE_KEY,
    SUPPORTED_LOCALES,
    normalize_locale,
    tr,
    translation_payload,
)


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="{{ locale }}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title data-i18n="auth.page_title">{{ tr("auth.page_title", locale) }}</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #111827; color: #e5e7eb; }
    main { box-sizing: border-box; width: min(360px, calc(100% - 32px)); padding: 28px; border: 1px solid #374151; border-radius: 14px; background: #1f2937; box-shadow: 0 20px 45px #0005; }
    .login-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
    h1 { margin: 0 0 22px; font-size: 1.5rem; }
    .login-header h1 { margin: 0; }
    .locale-switch { display: inline-flex; flex: 0 0 auto; padding: 3px; border: 1px solid #4b5563; border-radius: 8px; }
    .locale-switch button { width: auto; min-width: 42px; padding: 6px 8px; border-radius: 5px; background: transparent; color: #cbd5e1; }
    .locale-switch button[aria-pressed="true"] { background: #2563eb; color: #fff; }
    label { display: block; font-weight: 600; }
    .password-label-row { display: flex; align-items: center; gap: 7px; margin: 14px 0 6px; }
    .password-field { position: relative; }
    .password-input { box-sizing: border-box; width: 100%; padding: 11px 46px 11px 11px; border: 1px solid #4b5563; border-radius: 7px; background: #111827; color: #fff; }
    .password-input:focus { border-color: #60a5fa; outline: 2px solid #60a5fa55; }
    fieldset { margin: 18px 0; padding: 0; border: 0; }
    fieldset label { display: flex; gap: 9px; align-items: center; margin: 10px 0; font-weight: 400; }
    button { width: 100%; padding: 11px; border: 0; border-radius: 7px; background: #2563eb; color: #fff; font-weight: 700; cursor: pointer; }
    button:disabled { cursor: not-allowed; opacity: .55; }
    .icon-button { display: inline-grid; place-items: center; width: 24px; height: 24px; padding: 0; border: 1px solid #64748b; border-radius: 50%; background: transparent; color: #cbd5e1; }
    .icon-button:hover, .icon-button:focus-visible { border-color: #93c5fd; color: #fff; outline: none; }
    .help-button { flex: 0 0 auto; font-size: .8rem; line-height: 1; }
    .password-toggle { position: absolute; top: 50%; right: 8px; width: 32px; height: 32px; border: 0; transform: translateY(-50%); }
    .password-toggle svg { width: 21px; height: 21px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }
    .password-toggle .eye-off { display: none; }
    .password-toggle[aria-pressed="true"] .eye { display: none; }
    .password-toggle[aria-pressed="true"] .eye-off { display: block; }
    .live-submit { margin-top: 16px; }
    .error { padding: 10px; border-radius: 7px; background: #7f1d1d; color: #fecaca; }
    small { color: #9ca3af; }
    code { color: #bfdbfe; overflow-wrap: anywhere; }
    .login-modal[hidden] { display: none; }
    .login-modal { position: fixed; inset: 0; z-index: 10; display: grid; place-items: center; padding: 16px; }
    .login-modal__backdrop { position: absolute; inset: 0; background: #020617c7; }
    .login-modal__panel { position: relative; width: min(480px, calc(100% - 32px)); max-height: calc(100vh - 48px); overflow-y: auto; box-sizing: border-box; padding: 24px; border: 1px solid #475569; border-radius: 12px; background: #1f2937; box-shadow: 0 24px 60px #0009; }
    .login-modal__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
    .login-modal__header h2 { margin: 0; font-size: 1.2rem; }
    .login-modal__close { flex: 0 0 auto; width: 30px; height: 30px; padding: 0; border: 0; background: transparent; color: #cbd5e1; font-size: 1.5rem; line-height: 1; }
    .login-modal__panel p { color: #cbd5e1; line-height: 1.5; }
    .login-modal__steps { margin: 12px 0 20px; padding-left: 22px; color: #cbd5e1; line-height: 1.5; }
    .login-modal__steps li + li { margin-top: 9px; }
  </style>
</head>
<body>
  <main>
    <div class="login-header">
      <h1>FinRep</h1>
      <div class="locale-switch" role="group" data-i18n-aria-label="dashboard.locale_label" aria-label="{{ tr('dashboard.locale_label', locale) }}">
        <button type="button" data-locale="ru" aria-pressed="{{ 'true' if locale == 'ru' else 'false' }}">RU</button>
        <button type="button" data-locale="en" aria-pressed="{{ 'true' if locale == 'en' else 'false' }}">EN</button>
      </div>
    </div>
    {% if error %}<p class="error" data-i18n="auth.error">{{ error }}</p>{% endif %}
    <form method="post" action="/login">
      <input type="hidden" name="data_mode" value="live">
      <input type="hidden" name="locale" value="{{ locale }}">
      <div class="password-label-row">
        <label for="password" data-i18n="auth.password">{{ tr("auth.password", locale) }}</label>
        <button id="password-help-button" class="icon-button help-button" type="button" data-i18n-aria-label="auth.password_help_label" aria-label="{{ tr('auth.password_help_label', locale) }}" aria-controls="password-help-modal" aria-expanded="false">?</button>
      </div>
      <div class="password-field">
        <input class="password-input" id="password" name="password" type="password" {% if live_enabled %}required autofocus{% else %}disabled{% endif %} autocomplete="current-password">
        <button id="toggle-password" class="icon-button password-toggle" type="button" data-i18n-aria-label="auth.show_password" aria-label="{{ tr('auth.show_password', locale) }}" aria-controls="password" aria-pressed="false" {% if not live_enabled %}disabled{% endif %}>
          <svg class="eye" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"></path><circle cx="12" cy="12" r="2.5"></circle></svg>
          <svg class="eye-off" viewBox="0 0 24 24" aria-hidden="true"><path d="m3 3 18 18M10.6 6.2A9 9 0 0 1 12 6c6 0 9.5 6 9.5 6a16 16 0 0 1-2.3 3M6.4 6.4C3.9 8.2 2.5 12 2.5 12s3.5 6 9.5 6c1.3 0 2.5-.3 3.5-.7M9.9 9.9a3 3 0 0 0 4.2 4.2"></path></svg>
        </button>
      </div>
      <button class="live-submit" type="submit" {% if not live_enabled %}disabled{% endif %}><span data-i18n="auth.live_submit">{{ tr("auth.live_submit", locale) }}</span></button>
      {% if not live_enabled %}<p><small data-i18n="auth.live_unavailable">{{ tr("auth.live_unavailable", locale) }}</small></p>{% endif %}
    </form>
    <hr style="margin: 24px 0; border-color: #374151;">
    <form method="post" action="/login">
      <input type="hidden" name="data_mode" value="test">
      <input type="hidden" name="locale" value="{{ locale }}">
      <button type="submit" style="background:#d97706;"><span data-i18n="auth.demo_submit">{{ tr("auth.demo_submit", locale) }}</span></button>
      <p><small data-i18n="auth.demo_note">{{ tr("auth.demo_note", locale) }}</small></p>
    </form>
  </main>
  <div id="password-help-modal" class="login-modal" hidden>
    <div class="login-modal__backdrop" data-close-password-help></div>
    <section class="login-modal__panel" role="dialog" aria-modal="true" aria-labelledby="password-help-title">
      <div class="login-modal__header">
        <h2 id="password-help-title" data-i18n="auth.help_title">{{ tr("auth.help_title", locale) }}</h2>
        <button id="password-help-close" class="login-modal__close" type="button" data-i18n-aria-label="action.close" aria-label="{{ tr('action.close', locale) }}">&times;</button>
      </div>
      <p data-i18n="auth.help_intro">{{ tr("auth.help_intro", locale) }}</p>
      <ol class="login-modal__steps">
        <li><span data-i18n="auth.help_env_file_prefix">{{ tr("auth.help_env_file_prefix", locale) }}</span> <code>.env</code> <span data-i18n="auth.help_env_file_suffix">{{ tr("auth.help_env_file_suffix", locale) }}</span> <code>.env.example</code>).</li>
        <li><span data-i18n="auth.help_password_prefix">{{ tr("auth.help_password_prefix", locale) }}</span> <code>FINREP_DASH_PASSWORD=your-password</code>.</li>
        <li><span data-i18n="auth.help_secret_prefix">{{ tr("auth.help_secret_prefix", locale) }}</span> <code>FINREP_DASH_SECRET_KEY=random-key</code>. <span data-i18n="auth.help_secret_suffix">{{ tr("auth.help_secret_suffix", locale) }}</span> <code>uv run python -c "import secrets; print(secrets.token_hex(32))"</code>.</li>
        <li data-i18n="auth.help_restart">{{ tr("auth.help_restart", locale) }}</li>
      </ol>
      <p><small data-i18n="auth.help_demo">{{ tr("auth.help_demo", locale) }}</small></p>
      <button id="password-help-done" type="button"><span data-i18n="auth.help_done">{{ tr("auth.help_done", locale) }}</span></button>
    </section>
  </div>
  <script>
    const translations = {{ translations | tojson }};
    const supportedLocales = {{ supported_locales | tojson }};
    const localeStorageKey = {{ locale_storage_key | tojson }};
    const localeTimestampStorageKey = {{ locale_timestamp_storage_key | tojson }};
    const normalizedLocale = (value) => supportedLocales.includes(value) ? value : {{ default_locale | tojson }};
    const readStoredLocale = () => {
      try {
        const value = window.localStorage.getItem(localeStorageKey);
        if (value === null) return null;
        try {
          return JSON.parse(value);
        } catch (_error) {
          return value;
        }
      } catch (_error) {
        return null;
      }
    };
    const writeStoredLocale = (value) => {
      try {
        window.localStorage.setItem(localeStorageKey, JSON.stringify(value));
        window.localStorage.setItem(localeTimestampStorageKey, String(Date.now()));
      } catch (_error) {
        // The selected language still works for this page when storage is unavailable.
      }
    };
    let currentLocale = normalizedLocale(readStoredLocale() || {{ locale | tojson }});
    const translate = (key) => translations[currentLocale]?.[key] || translations.ru[key] || key;
    const applyLocale = (locale) => {
      currentLocale = normalizedLocale(locale);
      document.documentElement.lang = currentLocale;
      document.title = translate("auth.page_title");
      document.querySelectorAll("[data-i18n]").forEach((element) => {
        element.textContent = translate(element.dataset.i18n);
      });
      document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
        element.setAttribute("aria-label", translate(element.dataset.i18nAriaLabel));
      });
      document.querySelectorAll("input[name='locale']").forEach((input) => {
        input.value = currentLocale;
      });
      document.querySelectorAll("[data-locale]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.locale === currentLocale));
      });
    };
    document.querySelectorAll("[data-locale]").forEach((button) => {
      button.addEventListener("click", () => {
        writeStoredLocale(button.dataset.locale);
        applyLocale(button.dataset.locale);
      });
    });
    applyLocale(currentLocale);

    const passwordInput = document.getElementById("password");
    const passwordToggle = document.getElementById("toggle-password");
    passwordToggle.addEventListener("click", () => {
      const showPassword = passwordInput.type === "password";
      passwordInput.type = showPassword ? "text" : "password";
      passwordToggle.setAttribute("aria-pressed", String(showPassword));
      passwordToggle.dataset.i18nAriaLabel = showPassword ? "auth.hide_password" : "auth.show_password";
      passwordToggle.setAttribute("aria-label", translate(passwordToggle.dataset.i18nAriaLabel));
      passwordInput.focus();
    });

    const helpButton = document.getElementById("password-help-button");
    const helpModal = document.getElementById("password-help-modal");
    const helpClose = document.getElementById("password-help-close");
    const pageContent = document.querySelector("main");
    const helpFocusableSelector = "button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex='-1'])";
    const closeHelp = () => {
      helpModal.hidden = true;
      pageContent.inert = false;
      pageContent.removeAttribute("aria-hidden");
      helpButton.setAttribute("aria-expanded", "false");
      helpButton.focus();
    };
    helpButton.addEventListener("click", () => {
      helpModal.hidden = false;
      pageContent.inert = true;
      pageContent.setAttribute("aria-hidden", "true");
      helpButton.setAttribute("aria-expanded", "true");
      helpClose.focus();
    });
    helpClose.addEventListener("click", closeHelp);
    document.getElementById("password-help-done").addEventListener("click", closeHelp);
    document.querySelector("[data-close-password-help]").addEventListener("click", closeHelp);
    document.addEventListener("keydown", (event) => {
      if (helpModal.hidden) return;
      if (event.key === "Escape") {
        closeHelp();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...helpModal.querySelectorAll(helpFocusableSelector)];
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && (document.activeElement === first || !helpModal.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  </script>
</body>
</html>
"""


def configure_auth(server: Flask) -> None:
    _load_dashboard_secrets_from_dotenv()
    password = os.environ.get("FINREP_DASH_PASSWORD")
    secret_key = os.environ.get("FINREP_DASH_SECRET_KEY")
    if password and not secret_key:
        raise RuntimeError("FINREP_DASH_SECRET_KEY is required when FINREP_DASH_PASSWORD is configured")

    live_enabled = bool(password)
    server.secret_key = secret_key or secrets.token_hex(32)
    server.config["FINREP_LIVE_AUTH_ENABLED"] = live_enabled
    server.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    @server.before_request
    def require_login():
        if request.path in {"/login", "/healthz"}:
            return None
        if session.get("authenticated") is True:
            return None
        if request.path.startswith("/_dash-"):
            return {"error": "authentication required"}, 401
        return redirect("/login")

    @server.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET" and session.get("authenticated") is True:
            return redirect("/")
        locale = normalize_locale(request.form.get("locale") if request.method == "POST" else None)
        error = None
        if request.method == "POST":
            requested_mode = request.form.get("data_mode")
            if requested_mode == "test":
                session.clear()
                session["authenticated"] = True
                session["data_mode"] = "test"
                session.permanent = True
                return redirect("/")
            supplied = request.form.get("password", "")
            if password and hmac.compare_digest(supplied.encode("utf-8"), password.encode("utf-8")):
                session.clear()
                session["authenticated"] = True
                session["data_mode"] = "live"
                session.permanent = True
                return redirect("/")
            error = tr("auth.error", locale)
        return render_template_string(
            LOGIN_TEMPLATE,
            error=error,
            live_enabled=live_enabled,
            locale=locale,
            tr=tr,
            translations=translation_payload(),
            supported_locales=SUPPORTED_LOCALES,
            default_locale=DEFAULT_LOCALE,
            locale_storage_key=LOCALE_STORAGE_KEY,
            locale_timestamp_storage_key=LOCALE_TIMESTAMP_STORAGE_KEY,
        ), (401 if error else 200)

    @server.post("/logout")
    def logout():
        session.clear()
        return redirect("/login")


def _load_dashboard_secrets_from_dotenv() -> None:
    """Load only FinRep auth secrets without evaluating unrelated .env content."""
    env_path = Path.cwd() / ".env"
    if not env_path.is_file():
        return
    wanted = {"FINREP_DASH_PASSWORD", "FINREP_DASH_SECRET_KEY"}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in wanted or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            os.environ[key] = value
