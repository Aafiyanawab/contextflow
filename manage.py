"""Flask CLI entry point (FLASK_APP=manage.py).

The Flask app lives in the root-level app.py, whose module name is
shadowed by the app/ package — so `flask --app app.py` imports the
package and finds nothing. Loading app.py by file path sidesteps the
collision without renaming anything.

Usage:
    FLASK_APP=manage.py flask db upgrade
"""
import importlib.util
import os

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
_spec = importlib.util.spec_from_file_location("contextflow_app", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

app = _mod.app

# ── Admin assignment (RBAC) ──────────────────────────────
# Admins are assigned manually, never self-served:
#     FLASK_APP=manage.py flask set-admin you@example.com
#     FLASK_APP=manage.py flask set-admin you@example.com --revoke
import click  # noqa: E402
from app.models import db, User  # noqa: E402


@app.cli.command("set-admin")
@click.argument("email")
@click.option("--role", default="super_admin",
              type=click.Choice(["super_admin", "company_admin", "employee"]),
              help="Role to assign (default: super_admin).")
@click.option("--revoke", is_flag=True, help="Demote back to employee.")
def set_admin(email, role, revoke):
    """Set a user's role by email (default promotes to super_admin)."""
    user = User.query.filter_by(email=email.strip().lower()).first()
    if not user:
        click.echo(f"No user with email {email!r}.")
        return
    user.role = "employee" if revoke else role
    db.session.commit()
    click.echo(f"{user.email} is now {user.role}.")
