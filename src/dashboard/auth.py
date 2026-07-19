from __future__ import annotations

import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, session


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FinRep — вход</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #111827; color: #e5e7eb; }
    main { width: min(360px, calc(100% - 32px)); padding: 28px; border: 1px solid #374151; border-radius: 14px; background: #1f2937; box-shadow: 0 20px 45px #0005; }
    h1 { margin: 0 0 22px; font-size: 1.5rem; }
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
    <h1>FinRep Dashboard</h1>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post" action="/login">
      <input type="hidden" name="data_mode" value="live">
      <div class="password-label-row">
        <label for="password">Пароль</label>
        <button id="password-help-button" class="icon-button help-button" type="button" aria-label="Что делать, если пароль не установлен" aria-controls="password-help-modal" aria-expanded="false">?</button>
      </div>
      <div class="password-field">
        <input class="password-input" id="password" name="password" type="password" {% if live_enabled %}required autofocus{% else %}disabled{% endif %} autocomplete="current-password">
        <button id="toggle-password" class="icon-button password-toggle" type="button" aria-label="Показать пароль" aria-controls="password" aria-pressed="false" {% if not live_enabled %}disabled{% endif %}>
          <svg class="eye" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"></path><circle cx="12" cy="12" r="2.5"></circle></svg>
          <svg class="eye-off" viewBox="0 0 24 24" aria-hidden="true"><path d="m3 3 18 18M10.6 6.2A9 9 0 0 1 12 6c6 0 9.5 6 9.5 6a16 16 0 0 1-2.3 3M6.4 6.4C3.9 8.2 2.5 12 2.5 12s3.5 6 9.5 6c1.3 0 2.5-.3 3.5-.7M9.9 9.9a3 3 0 0 0 4.2 4.2"></path></svg>
        </button>
      </div>
      <button class="live-submit" type="submit" {% if not live_enabled %}disabled{% endif %}>Войти в LIVE</button>
      {% if not live_enabled %}<p><small>LIVE недоступен: задайте FINREP_DASH_PASSWORD и FINREP_DASH_SECRET_KEY в .env.</small></p>{% endif %}
    </form>
    <hr style="margin: 24px 0; border-color: #374151;">
    <form method="post" action="/login">
      <input type="hidden" name="data_mode" value="test">
      <button type="submit" style="background:#d97706;">Открыть demo без пароля</button>
      <p><small>Demo использует только read-only sample_data.</small></p>
    </form>
  </main>
  <div id="password-help-modal" class="login-modal" hidden>
    <div class="login-modal__backdrop" data-close-password-help></div>
    <section class="login-modal__panel" role="dialog" aria-modal="true" aria-labelledby="password-help-title">
      <div class="login-modal__header">
        <h2 id="password-help-title">Как настроить пароль</h2>
        <button id="password-help-close" class="login-modal__close" type="button" aria-label="Закрыть">&times;</button>
      </div>
      <p>Чтобы открыть LIVE с вашими данными:</p>
      <ol class="login-modal__steps">
        <li>Создайте файл <code>.env</code> в корне проекта (можно скопировать <code>.env.example</code>).</li>
        <li>Задайте свой пароль: <code>FINREP_DASH_PASSWORD=ваш-пароль</code>.</li>
        <li>Добавьте стабильный случайный ключ сессий: <code>FINREP_DASH_SECRET_KEY=случайный-ключ</code>. Его можно получить командой <code>uv run python -c "import secrets; print(secrets.token_hex(32))"</code>.</li>
        <li>Перезапустите приложение и войдите с новым паролем.</li>
      </ol>
      <p><small>Demo по-прежнему доступно без пароля и использует только read-only sample_data.</small></p>
      <button id="password-help-done" type="button">Понятно</button>
    </section>
  </div>
  <script>
    const passwordInput = document.getElementById("password");
    const passwordToggle = document.getElementById("toggle-password");
    passwordToggle.addEventListener("click", () => {
      const showPassword = passwordInput.type === "password";
      passwordInput.type = showPassword ? "text" : "password";
      passwordToggle.setAttribute("aria-pressed", String(showPassword));
      passwordToggle.setAttribute("aria-label", showPassword ? "Скрыть пароль" : "Показать пароль");
      passwordInput.focus();
    });

    const helpButton = document.getElementById("password-help-button");
    const helpModal = document.getElementById("password-help-modal");
    const helpClose = document.getElementById("password-help-close");
    const closeHelp = () => {
      helpModal.hidden = true;
      helpButton.setAttribute("aria-expanded", "false");
      helpButton.focus();
    };
    helpButton.addEventListener("click", () => {
      helpModal.hidden = false;
      helpButton.setAttribute("aria-expanded", "true");
      helpClose.focus();
    });
    helpClose.addEventListener("click", closeHelp);
    document.getElementById("password-help-done").addEventListener("click", closeHelp);
    document.querySelector("[data-close-password-help]").addEventListener("click", closeHelp);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !helpModal.hidden) closeHelp();
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
            error = "LIVE недоступен или введен неверный пароль."
        return render_template_string(LOGIN_TEMPLATE, error=error, live_enabled=live_enabled), (401 if error else 200)

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
