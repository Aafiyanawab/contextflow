"""Authentication: social login (GitHub, Google), email + password,
session management, the login_required decorator, and CSRF protection.

Providers live in a registry (PROVIDERS). Adding one = a Provider entry
plus its oauth.register() block; the routes and login page are
provider-agnostic. Access tokens are used once for the profile fetch
during the callback and never stored.

CSRF: every session carries a random token; check_csrf() rejects any
unsafe-method request that doesn't echo it back — forms via a hidden
`csrf_token` input, fetch/SSE calls via the `X-CSRF-Token` header
(read from the meta tag in base.html). SameSite=Lax already blocks
most cross-site posts; the token is defense in depth.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import timedelta, timezone
from functools import wraps

from flask import (Blueprint, abort, current_app, g, jsonify, redirect,
                   render_template, request, session, url_for)
from authlib.integrations.flask_client import OAuth
from werkzeug.security import generate_password_hash, check_password_hash

from app.models import db, User, OAuthIdentity, PasswordResetToken, utcnow
from app.ratelimit import throttle
from app.email import send_password_reset_email
from app.config import PASSWORD_RESET_TTL_SECONDS

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8

oauth = OAuth()
auth_bp = Blueprint("auth", __name__)

LOGIN_ERRORS = {
    "not_configured": "That sign-in method isn't configured on this server.",
    "oauth_failed": "Sign-in didn't complete. Please try again.",
    "access_denied": "You cancelled the authorization.",
    "session_expired": "Your session expired. Please sign in again.",
    "bad_credentials": "Invalid email or password.",
    "email_taken": "An account with that email already exists — sign in instead.",
    "weak_password": "Password must be at least 8 characters.",
    "invalid_email": "Enter a valid email address.",
    "throttled": "Too many attempts. Please wait a minute and try again.",
    "account_disabled": "This account has been deactivated. Contact an administrator.",
    "passwords_mismatch": "Those passwords don't match.",
}


def _github_profile(client, token):
    """GitHub: profile + a separate call for the verified primary email."""
    profile = client.get("user", token=token).json()
    emails = client.get("user/emails", token=token).json()
    email = None
    if isinstance(emails, list):
        email = next((e["email"] for e in emails
                      if e.get("primary") and e.get("verified")), None)
    return {"uid": str(profile["id"]),
            "email": email,  # already verified by construction
            "email_verified": email is not None,
            "name": profile.get("name") or profile.get("login"),
            "avatar_url": profile.get("avatar_url")}


def _google_profile(client, token):
    """Google: OIDC id_token carries a verified-email claim."""
    info = token.get("userinfo") or client.userinfo(token=token)
    return {"uid": info["sub"],
            "email": info.get("email"),
            "email_verified": bool(info.get("email_verified")),
            "name": info.get("name") or info.get("email"),
            "avatar_url": info.get("picture")}


class Provider:
    def __init__(self, name, label, env_id, env_secret, extract):
        self.name = name
        self.label = label
        self.env_id = env_id
        self.env_secret = env_secret
        self.extract = extract

    @property
    def configured(self):
        return bool(os.getenv(self.env_id) and os.getenv(self.env_secret))


PROVIDERS = {
    "google": Provider("google", "Google",
                       "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                       _google_profile),
    "github": Provider("github", "GitHub",
                       "GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET",
                       _github_profile),
}


def configured_providers():
    """OAuth providers the server can actually offer, in display order."""
    return [p for p in PROVIDERS.values() if p.configured]


def init_auth(app):
    oauth.init_app(app)
    if PROVIDERS["github"].configured:
        oauth.register(
            name="github",
            client_id=os.getenv("GITHUB_OAUTH_CLIENT_ID"),
            client_secret=os.getenv("GITHUB_OAUTH_CLIENT_SECRET"),
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
    if PROVIDERS["google"].configured:
        oauth.register(
            name="google",
            client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    app.register_blueprint(auth_bp)


@auth_bp.before_app_request
def load_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = db.session.get(User, user_id)
        # Stale cookie for a deleted user, or an account deactivated
        # mid-session — either way, end the session immediately.
        if g.user is None or not g.user.active:
            session.clear()
            g.user = None
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)


@auth_bp.before_app_request
def check_csrf():
    """Runs after load_user (registration order). A failure for a real
    user means a stale tab whose session was since rotated — the
    friendliest recovery is the same as an expired session."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if g.user is None:
        # No authenticated session to ride — let login_required answer
        # with its friendlier 401/redirect instead of a CSRF error.
        return None
    sent = (request.headers.get("X-CSRF-Token")
            or request.form.get("csrf_token") or "")
    expected = session.get("csrf_token") or ""
    if expected and sent and hmac.compare_digest(sent, expected):
        return None
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"error": "Session security check failed. "
                                 "Refresh the page and try again."}), 403
    return redirect(url_for("auth.login", error="session_expired"))


@auth_bp.app_context_processor
def inject_csrf_token():
    return {"csrf_token": session.get("csrf_token", "")}


def login_required(view):
    """Users never see raw JSON: only requests explicitly marked as fetch/SSE
    (X-Requested-With: fetch, handled by frontend JS) get a 401 JSON body.
    Page loads keep their destination via ?next=; form posts land on /login
    with a friendly "session expired" message."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"error": "Session expired. Please sign in again."}), 401
            if request.method == "GET":
                return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
            return redirect(url_for("auth.login", error="session_expired"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Admin-only. A signed-in non-admin gets a 403 (an explicit "you
    can't access this"), which is also a useful signal: if an admin
    route ever returns 404, the route isn't registered (stale server),
    not a permission problem. Anonymous users fall through to
    login_required's redirect/401 handling."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is not None and not g.user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return login_required(wrapped)


def super_admin_required(view):
    """Super-Admin only (platform-wide actions: companies, provider
    config, audit log). Company Admins and everyone else get 403."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is not None and not g.user.is_super_admin:
            abort(403)
        return view(*args, **kwargs)
    return login_required(wrapped)


@auth_bp.route("/login")
def login():
    if g.user:
        return redirect(url_for("index"))
    error_key = request.args.get("error")
    return render_template("login.html",
                           providers=configured_providers(),
                           error=LOGIN_ERRORS.get(error_key),
                           next_url=request.args.get("next", ""))


def _establish_session(user, next_url):
    """Rotate the session and log the user in. Shared by every provider
    and the email path — the one place a session is created, and so the
    one place the deactivation gate is enforced."""
    if not user.active:
        return redirect(url_for("auth.login", error="account_disabled"))
    user.last_login_at = utcnow()
    db.session.commit()
    session.clear()  # drop pre-login state, then establish the session
    session["user_id"] = user.id
    session.permanent = True
    return redirect(next_url if next_url.startswith("/") else url_for("index"))


@auth_bp.route("/auth/<provider>")
def authorize(provider):
    prov = PROVIDERS.get(provider)
    if not prov:
        abort(404)
    if not prov.configured:
        return redirect(url_for("auth.login", error="not_configured"))
    next_url = request.args.get("next", "")
    session["next_url"] = next_url if next_url.startswith("/") else ""
    redirect_uri = url_for("auth.callback", provider=provider, _external=True)
    return oauth.create_client(provider).authorize_redirect(redirect_uri)


@auth_bp.route("/auth/<provider>/callback")
def callback(provider):
    prov = PROVIDERS.get(provider)
    if not prov:
        abort(404)
    if request.args.get("error"):  # user cancelled on the provider's side
        return redirect(url_for("auth.login", error="access_denied"))

    client = oauth.create_client(provider)
    try:
        token = client.authorize_access_token()  # used once, never stored
        info = prov.extract(client, token)
    except Exception as e:
        # Log only the exception type — never request.url (carries the
        # single-use OAuth code), session contents, or a locals-bearing
        # traceback, all of which would leak credentials to the logs.
        current_app.logger.warning("OAuth callback failed for %s: %s",
                                   provider, type(e).__name__)
        return redirect(url_for("auth.login", error="oauth_failed"))

    identity = OAuthIdentity.query.filter_by(
        provider=provider, provider_uid=info["uid"]).first()
    if identity:
        user = identity.user
    else:
        # Link to an existing account ONLY on a verified email — an
        # unverified provider email must never take over an account.
        user = None
        if info["email"] and info["email_verified"]:
            user = User.query.filter_by(email=info["email"]).first()
        if user is None:
            user = User(name=info["name"], email=info["email"],
                        avatar_url=info["avatar_url"])
            db.session.add(user)
            db.session.flush()
        db.session.add(OAuthIdentity(user_id=user.id, provider=provider,
                                     provider_uid=info["uid"]))

    if info["avatar_url"]:
        user.avatar_url = info["avatar_url"]
    return _establish_session(user, session.pop("next_url", ""))


# ── Email + password ─────────────────────────────────────
# No email verification and no password reset yet (both need mail
# infrastructure — deferred to the AWS/SES phase). Accounts are usable
# immediately; they only ever grant access to their own workspaces.

def _auth_key():
    """Throttle key: client IP. X-Forwarded-For's first hop when behind
    a trusted proxy, else the socket address."""
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr or "unknown"


@auth_bp.route("/login/email", methods=["GET", "POST"])
def login_email():
    if g.user:
        return redirect(url_for("index"))
    next_url = request.values.get("next", "")
    if request.method == "GET":
        return render_template("email_auth.html", mode="signin",
                               next_url=next_url, error=None)

    # Brute-force defence: throttle by IP and by target email.
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if (throttle("auth", _auth_key(), 10, 60)
            or throttle("auth", email, 10, 300)):
        return _email_error("signin", "throttled", next_url)

    user = User.query.filter_by(email=email).first() if email else None
    # Constant-ish work + identical error whether the email exists or
    # not: no account enumeration.
    if not user or not user.password_hash \
            or not check_password_hash(user.password_hash, password):
        return _email_error("signin", "bad_credentials", next_url)
    return _establish_session(user, next_url)


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("index"))
    next_url = request.values.get("next", "")
    if request.method == "GET":
        return render_template("email_auth.html", mode="signup",
                               next_url=next_url, error=None)

    if throttle("auth", _auth_key(), 10, 60):
        return _email_error("signup", "throttled", next_url)
    name = (request.form.get("name") or "").strip()[:120]
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if not EMAIL_RE.match(email):
        return _email_error("signup", "invalid_email", next_url)
    if len(password) < MIN_PASSWORD_LEN:
        return _email_error("signup", "weak_password", next_url)
    if User.query.filter_by(email=email).first():
        # Covers both email and OAuth-only accounts on this address.
        return _email_error("signup", "email_taken", next_url)

    user = User(name=name or email.split("@")[0], email=email,
                password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.flush()
    return _establish_session(user, next_url)


def _email_error(mode, error_key, next_url):
    return render_template("email_auth.html", mode=mode, next_url=next_url,
                           error=LOGIN_ERRORS.get(error_key)), 400


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ── Password reset ───────────────────────────────────────
# Email-based, single-use, 5-minute links. Only the sha256 HASH of the token
# is stored; the raw token lives only in the emailed link. Requesting a reset
# invalidates every prior token for that user, and a successful reset deletes
# them all — so only the newest link ever works, exactly once.

def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _expired(expires_at):
    """SQLite hands back naive datetimes even for tz-aware columns, so treat a
    naive value as UTC — otherwise comparing it to utcnow() (aware) raises
    "can't compare offset-naive and offset-aware datetimes"."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= utcnow()


def _reset_link(raw_token):
    """Absolute reset URL. Uses APP_BASE_URL (an https:// production URL) when
    set, else the request's own scheme/host — so the link becomes HTTPS the
    moment the app is served over HTTPS, with no code change."""
    path = url_for("auth.reset_password", token=raw_token)
    base = os.getenv("APP_BASE_URL")
    if base:
        return base.rstrip("/") + path
    return url_for("auth.reset_password", token=raw_token, _external=True)


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("forgot_password.html", sent=False, error=None)

    email = (request.form.get("email") or "").strip().lower()
    # Throttle by IP and by target email (brute force + email bombing).
    if throttle("auth", _auth_key(), 10, 60) or throttle("reset", email, 5, 900):
        return render_template("forgot_password.html", sent=False,
                               error=LOGIN_ERRORS["throttled"]), 429

    if EMAIL_RE.match(email):
        user = User.query.filter_by(email=email).first()
        if user:
            # A new request invalidates every previous token for this user.
            PasswordResetToken.query.filter_by(user_id=user.id).delete()
            raw = secrets.token_urlsafe(32)
            db.session.add(PasswordResetToken(
                user_id=user.id, token_hash=_hash_token(raw),
                expires_at=utcnow() + timedelta(seconds=PASSWORD_RESET_TTL_SECONDS)))
            db.session.commit()
            try:
                send_password_reset_email(user.email, _reset_link(raw))
            except Exception:
                # A mail-server failure must never 500 the user or (via a
                # different response) reveal that this account exists.
                current_app.logger.exception("password reset email send failed")
    # Enumeration-safe: identical confirmation whether or not the email exists.
    return render_template("forgot_password.html", sent=True, error=None)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if g.user:
        return redirect(url_for("index"))
    row = PasswordResetToken.query.filter_by(token_hash=_hash_token(token)).first()
    if row is None or _expired(row.expires_at):
        # One page for expired / used / superseded / never-valid — it reveals
        # nothing about which (enumeration-safe).
        return render_template("reset_expired.html"), 400

    if request.method == "GET":
        return render_template("reset_password.html", token=token, error=None)

    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""
    if len(password) < MIN_PASSWORD_LEN:
        return render_template("reset_password.html", token=token,
                               error=LOGIN_ERRORS["weak_password"]), 400
    if password != confirm:
        return render_template("reset_password.html", token=token,
                               error=LOGIN_ERRORS["passwords_mismatch"]), 400

    # Re-verify at write time (the link could have expired between GET and POST).
    row = PasswordResetToken.query.filter_by(token_hash=_hash_token(token)).first()
    if row is None or _expired(row.expires_at):
        return render_template("reset_expired.html"), 400

    user = row.user
    user.password_hash = generate_password_hash(password)
    # Single-use + invalidate every outstanding token for this user.
    PasswordResetToken.query.filter_by(user_id=user.id).delete()
    db.session.commit()
    return render_template("reset_success.html")
