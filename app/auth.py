"""Authentication: OAuth login (GitHub now, Google later), session
management, the login_required decorator, and CSRF protection.

Adding a provider = one oauth.register() block in init_auth() plus a
button on the login page. Access tokens are used for the profile fetch
during the callback and never stored.

CSRF: every session carries a random token; check_csrf() rejects any
unsafe-method request that doesn't echo it back — forms via a hidden
`csrf_token` input, fetch/SSE calls via the `X-CSRF-Token` header
(read from the meta tag in base.html). SameSite=Lax already blocks
most cross-site posts; the token is defense in depth.
"""
import hmac
import os
import secrets
from functools import wraps

from flask import (Blueprint, abort, current_app, g, jsonify, redirect,
                   render_template, request, session, url_for)
from authlib.integrations.flask_client import OAuth

from app.models import db, User, OAuthIdentity, utcnow

oauth = OAuth()
auth_bp = Blueprint("auth", __name__)

LOGIN_ERRORS = {
    "not_configured": "GitHub login isn't configured on this server — "
                      "set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET.",
    "oauth_failed": "GitHub sign-in didn't complete. Please try again.",
    "access_denied": "You cancelled the GitHub authorization.",
    "session_expired": "Your session expired. Please sign in again.",
}


def init_auth(app):
    oauth.init_app(app)
    oauth.register(
        name="github",
        client_id=os.getenv("GITHUB_OAUTH_CLIENT_ID"),
        client_secret=os.getenv("GITHUB_OAUTH_CLIENT_SECRET"),
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "read:user user:email"},
    )
    app.register_blueprint(auth_bp)


@auth_bp.before_app_request
def load_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = db.session.get(User, user_id)
        if g.user is None:  # stale cookie for a deleted user
            session.clear()
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


@auth_bp.route("/login")
def login():
    if g.user:
        return redirect(url_for("index"))
    error_key = request.args.get("error")
    return render_template("login.html",
                           error=LOGIN_ERRORS.get(error_key),
                           next_url=request.args.get("next", ""))


@auth_bp.route("/auth/<provider>")
def authorize(provider):
    if provider != "github":
        abort(404)
    if not os.getenv("GITHUB_OAUTH_CLIENT_ID"):
        return redirect(url_for("auth.login", error="not_configured"))
    next_url = request.args.get("next", "")
    session["next_url"] = next_url if next_url.startswith("/") else ""
    redirect_uri = url_for("auth.callback", provider=provider, _external=True)
    return oauth.create_client(provider).authorize_redirect(redirect_uri)


@auth_bp.route("/auth/<provider>/callback")
def callback(provider):
    if provider != "github":
        abort(404)
    if request.args.get("error"):  # user cancelled on GitHub's side
        return redirect(url_for("auth.login", error="access_denied"))

    client = oauth.create_client(provider)
    try:
        token = client.authorize_access_token()  # used once, never stored
        profile = client.get("user", token=token).json()
        emails = client.get("user/emails", token=token).json()
    except Exception as e:
        # Log only the exception type — never request.url (carries the
        # single-use OAuth code), session contents, or a locals-bearing
        # traceback, all of which would leak credentials to the logs.
        current_app.logger.warning("OAuth callback failed for %s: %s",
                                   provider, type(e).__name__)
        return redirect(url_for("auth.login", error="oauth_failed"))

    provider_uid = str(profile["id"])
    email = None
    if isinstance(emails, list):
        email = next((e["email"] for e in emails
                      if e.get("primary") and e.get("verified")), None)

    identity = OAuthIdentity.query.filter_by(provider=provider,
                                             provider_uid=provider_uid).first()
    if identity:
        user = identity.user
    else:
        # Link to an existing account with the same verified email,
        # otherwise create a new user.
        user = User.query.filter_by(email=email).first() if email else None
        if user is None:
            user = User(name=profile.get("name") or profile.get("login"),
                        email=email,
                        avatar_url=profile.get("avatar_url"))
            db.session.add(user)
            db.session.flush()
        db.session.add(OAuthIdentity(user_id=user.id, provider=provider,
                                     provider_uid=provider_uid))

    user.last_login_at = utcnow()
    user.avatar_url = profile.get("avatar_url") or user.avatar_url
    db.session.commit()

    next_url = session.pop("next_url", "")
    session.clear()  # drop any pre-login state, then establish the session
    session["user_id"] = user.id
    session.permanent = True
    return redirect(next_url if next_url.startswith("/") else url_for("index"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
