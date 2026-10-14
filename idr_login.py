"""Shared login for the IDR tools (Flask apps). Same file in every Flask tool repo.

- One shared username/password from the environment: APP_USERNAME, APP_PASSWORD.
  If either is unset, login is off and the tool is open (as before).
- Signing in on one tool signs you in to every tool on the same server: the
  "idr_login" cookie is set for path "/" and signed with SECRET_KEY, which must be
  the same for all tools on the server (it comes from the shared idr-tools.env).
- Changing APP_PASSWORD signs everyone out.
- Also makes the app work behind nginx under a path such as /translate/
  (X-Forwarded-Prefix / -Proto / -For).

Usage in app.py:   from idr_login import init_login;  init_login(app, "Translate")
"""

import hashlib
import os
import secrets
import time

from flask import jsonify, redirect, render_template, request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix

COOKIE = "idr_login"
MAX_AGE = 14 * 24 * 3600  # stay signed in for two weeks


def _credentials():
    user, pw = os.environ.get("APP_USERNAME"), os.environ.get("APP_PASSWORD")
    return (user, pw) if user and pw else (None, None)


def _serializer(user, pw):
    key = os.environ.get("SECRET_KEY") or hashlib.sha256(f"idr-tools|{user}|{pw}".encode()).hexdigest()
    return URLSafeTimedSerializer(key, salt="idr-login")


def _fingerprint(pw):
    """Part of the cookie, so a new password invalidates old sign-ins."""
    return hashlib.sha256(pw.encode()).hexdigest()[:16]


def signed_in():
    user, pw = _credentials()
    if user is None:
        return True
    try:
        data = _serializer(user, pw).loads(request.cookies.get(COOKIE, ""), max_age=MAX_AGE)
    except BadSignature:
        return False
    return data.get("u") == user and data.get("p") == _fingerprint(pw)


def _safe_next(target):
    """Only allow redirects to a path on this server (no //evil.com)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return request.script_root + "/"


def init_login(app, tool_name, public=("static",), local_ok=()):
    """public: endpoints that never need a login. local_ok: endpoints allowed from the
    server itself (127.0.0.1 without going through nginx), e.g. for scheduled jobs."""
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    # Each tool keeps its own Flask session cookie so tools on one server don't clash.
    if app.config.get("SESSION_COOKIE_NAME", "session") == "session":
        app.config["SESSION_COOKIE_NAME"] = f"{tool_name.lower().replace(' ', '_')}_session"

    @app.context_processor
    def _login_context():
        return {"login_enabled": _credentials()[0] is not None,
                "logout_url": request.script_root + "/logout"}

    @app.before_request
    def _require_login():
        if request.endpoint in public or request.endpoint in ("idr_login", "idr_logout"):
            return None
        if request.endpoint in local_ok and request.remote_addr == "127.0.0.1" \
                and "X-Forwarded-For" not in request.headers:
            return None
        if signed_in():
            return None
        if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
            return jsonify({"error": "Please sign in again. · कृपया फिर से साइन इन करें।"}), 401
        return redirect(request.script_root + "/login?next=" + _quote(request.script_root + request.full_path.rstrip("?")))

    @app.route("/login", methods=["GET", "POST"], endpoint="idr_login")
    def _login():
        user, pw = _credentials()
        nxt = _safe_next(request.values.get("next"))
        if user is None:
            return redirect(nxt)
        error = None
        if request.method == "POST":
            ok = secrets.compare_digest(request.form.get("username", ""), user) & \
                secrets.compare_digest(request.form.get("password", ""), pw)
            if ok:
                resp = redirect(nxt)
                token = _serializer(user, pw).dumps({"u": user, "p": _fingerprint(pw)})
                resp.set_cookie(COOKIE, token, max_age=MAX_AGE, path="/", httponly=True,
                                secure=request.is_secure, samesite="Lax")
                return resp
            time.sleep(1)  # slow down password guessing
            error = "That username or password is not right. · यूज़रनेम या पासवर्ड सही नहीं है।"
        return render_template("login.html", tool_name=tool_name, error=error, next=nxt,
                               username=request.form.get("username", "")), (401 if error else 200)

    @app.route("/logout", endpoint="idr_logout")
    def _logout():
        resp = redirect(request.script_root + "/login")
        resp.delete_cookie(COOKIE, path="/")
        return resp


def _quote(s):
    from urllib.parse import quote
    return quote(s, safe="/")
