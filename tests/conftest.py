"""Keep the test suite hermetic: force the SQLite state store even when a local .env
defines DATABASE_URL, so tests never touch a real Postgres/Supabase database. Setting
it to empty (rather than deleting) survives the app's load_dotenv(override=False).
Individual tests that need the Postgres branch set DATABASE_URL via monkeypatch.
"""
import os

os.environ["DATABASE_URL"] = ""
