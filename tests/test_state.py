"""State store factory + SQLite backend behaviour (Postgres path needs a live DB)."""
from app.ingest import load_sqlite
from app.state import open_state, SqliteState, PostgresState


def test_factory_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert isinstance(open_state(load_sqlite()), SqliteState)


def test_factory_uses_postgres_when_url_set(monkeypatch):
    # Constructing PostgresState would connect; assert the factory *selects* it without
    # touching the network by stubbing the class.
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@host:6543/postgres")
    made = {}

    def fake(dsn):
        made["dsn"] = dsn
        return "PG"

    monkeypatch.setattr("app.state.PostgresState", fake)
    assert open_state(load_sqlite()) == "PG" and made["dsn"].startswith("postgresql://")


def test_sqlite_state_record_and_audit():
    st = SqliteState(load_sqlite())
    ref = st.record_action("create_escalation", {"ticket_id": "TKT-501"})
    assert ref.startswith("ACT-")
    st.write_audit("rid1", "staff", "action_committed", {"ref": ref})
    entries = st.recent_audit(10)
    assert entries and entries[0]["event"] == "action_committed" and entries[0]["role"] == "staff"
