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
