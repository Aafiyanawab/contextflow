"""Gunicorn entry point:  gunicorn wsgi:app

The Flask instance lives in the root-level app.py, whose module name is
shadowed by the app/ package — so `gunicorn app:app` would import the
package and find no `app` attribute. Loading app.py by file path (the
same trick manage.py uses) sidesteps the collision without renaming
anything.
"""
import importlib.util
import os

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
_spec = importlib.util.spec_from_file_location("contextflow_app", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

app = _mod.app
