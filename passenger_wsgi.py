"""Passenger entry point for cPanel shared hosting.

cPanel's "Setup Python App" looks for this file at the application root and
imports the name `application` from it. Everything else (settings, .env
loading) is the normal Django stack.

After changing any code on the server, restart the app with either the
"Restart" button in cPanel or:

    touch ~/guestlink/tmp/restart.txt
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Passenger does not always run with the app root on sys.path.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "guestlink.settings")

from guestlink.wsgi import application  # noqa: E402  (must follow sys.path setup)
