"""Settings, all read from environment variables (see ../.env.example)."""

import os

# The image sets sqlite:////data/librarian.sqlite3, on the named volume. This
# default is for a run outside Docker: data/ next to wherever it is started.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/librarian.sqlite3")

# Shared secret for /api/v1, sent in the X-API-Key header. Empty turns auth off.
API_KEY = os.getenv("API_KEY", "")
