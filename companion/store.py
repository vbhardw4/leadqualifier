"""LeadQualifier data layer.

Production uses PostgreSQL (psycopg2); tests swap in SQLite via the
LEADQUALIFIER_TEST_DB env var. SQL is written with `?` placeholders and translated
to `%s` for psycopg. JSON columns are TEXT (parsed by callers).
"""
import json
import os
import sqlite3
import statistics

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  company TEXT,
  role TEXT,
  budget_range TEXT,
  timeline TEXT,
  message TEXT,
  source TEXT DEFAULT 'web-form',
  status TEXT NOT NULL DEFAULT 'new',
  company_enriched TEXT,
  size_bucket TEXT,
  industry TEXT,
  enrich_confidence REAL,
  enrich_source TEXT,
  score INTEGER,
  rationale TEXT,
  band TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  scored_at TIMESTAMP,
  routed_at TIMESTAMP,
  is_sample INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lead_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL,
  event TEXT NOT NULL,
  detail TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  approve_url TEXT,
  reject_url TEXT,
  sent_to_slack INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS dead_letter (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER,
  stage TEXT NOT NULL,
  error TEXT NOT NULL,
  payload TEXT,
  attempts INTEGER DEFAULT 1,
  resolved INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outcomes (
  lead_id INTEGER PRIMARY KEY,
  outcome TEXT NOT NULL,
  recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_test_conn = None


def _translate(sql):
    if os.environ.get("LEADQUALIFIER_TEST_DB"):
        return sql
    return sql.replace("?", "%s")


def get_conn():
    global _test_conn
    if os.environ.get("LEADQUALIFIER_TEST_DB"):
        if _test_conn is None:
            _test_conn = sqlite3.connect(":memory:")
            _test_conn.row_factory = sqlite3.Row
            _test_conn.executescript(SCHEMA)
        return _test_conn
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(os.environ.get(
        "DATABASE_URL", "postgresql://leadqualifier:leadqualifier@localhost:5432/leadqualifier"))
    conn.autocommit = True
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    if os.environ.get("LEADQUALIFIER_TEST_DB"):
        cur.executescript(SCHEMA)
    else:
        # SERIAL instead of AUTOINCREMENT for postgres
        ddl = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        cur.execute(ddl)
    cur.close()
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()


def _row_to_dict(row):
    return dict(row) if row is not None else None


def create_lead(conn, data):
    cur = conn.cursor()
    cur.execute(_translate(
        "INSERT INTO leads (name,email,company,role,budget_range,timeline,message,source)"
        " VALUES (?,?,?,?,?,?,?,?) RETURNING id"),
        (data.get("name"), data.get("email"), data.get("company"), data.get("role"),
         data.get("budget_range"), data.get("timeline"), data.get("message"),
         data.get("source", "web-form")))
    row = cur.fetchone()
    # sqlite has no RETURNING in older versions? it does since 3.35; fallback:
    lead_id = row[0] if row else cur.lastrowid
    cur.close()
    return lead_id


def update_lead(conn, lead_id, fields):
    fields = dict(fields)
    cur = conn.cursor()
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur.execute(_translate(f"UPDATE leads SET {sets} WHERE id = ?"),
                (*fields.values(), lead_id))
    cur.close()


def get_lead(conn, lead_id):
    cur = conn.cursor()
    cur.execute(_translate("SELECT * FROM leads WHERE id = ?"), (lead_id,))
    lead = _row_to_dict(cur.fetchone())
    cur.close()
    return lead


def log_event(conn, lead_id, event, detail=None):
    cur = conn.cursor()
    cur.execute(_translate(
        "INSERT INTO lead_events (lead_id, event, detail) VALUES (?,?,?)"),
        (lead_id, event, json.dumps(detail or {})))
    cur.close()


def add_notification(conn, lead_id, kind, title, body, approve_url=None,
                     reject_url=None, sent_to_slack=False):
    cur = conn.cursor()
    cur.execute(_translate(
        "INSERT INTO notifications (lead_id,kind,title,body,approve_url,reject_url,sent_to_slack)"
        " VALUES (?,?,?,?,?,?,?) RETURNING id"),
        (lead_id, kind, title, body, approve_url, reject_url,
         1 if sent_to_slack else 0))
    row = cur.fetchone()
    nid = row[0] if row else cur.lastrowid
    cur.close()
    return nid


def add_dead_letter(conn, lead_id, stage, error, payload=None, attempts=1):
    cur = conn.cursor()
    cur.execute(_translate(
        "INSERT INTO dead_letter (lead_id,stage,error,payload,attempts)"
        " VALUES (?,?,?,?,?)"),
        (lead_id, stage, error, json.dumps(payload or {}), attempts))
    cur.close()


def record_outcome(conn, lead_id, outcome):
    cur = conn.cursor()
    if os.environ.get("LEADQUALIFIER_TEST_DB"):
        cur.execute(_translate(
            "INSERT OR REPLACE INTO outcomes (lead_id,outcome) VALUES (?,?)"),
            (lead_id, outcome))
    else:
        cur.execute(_translate(
            "INSERT INTO outcomes (lead_id,outcome) VALUES (?,?)"
            " ON CONFLICT (lead_id) DO UPDATE SET outcome = EXCLUDED.outcome"),
            (lead_id, outcome))
    cur.close()


def list_notifications(conn, limit=50):
    cur = conn.cursor()
    cur.execute(_translate(
        "SELECT n.*, l.name AS lead_name, l.company AS lead_company"
        " FROM notifications n LEFT JOIN leads l ON l.id = n.lead_id"
        " ORDER BY n.id DESC LIMIT ?"), (limit,))
    rows = [_row_to_dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def metrics(conn):
    """Aggregate ops metrics. Medians computed in Python (portable)."""
    cur = conn.cursor()
    cur.execute(_translate(
        "SELECT COUNT(*) c FROM leads WHERE is_sample = 0"))
    total = cur.fetchone()[0]
    cur.execute(_translate(
        "SELECT status, COUNT(*) c FROM leads WHERE is_sample = 0 GROUP BY status"))
    by_status = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(_translate(
        "SELECT score FROM leads WHERE is_sample = 0 AND score IS NOT NULL"))
    scores = [r[0] for r in cur.fetchall()]
    if os.environ.get("LEADQUALIFIER_TEST_DB"):
        cur.execute(_translate(
            "SELECT CAST((julianday(routed_at) - julianday(created_at)) * 86400 AS INTEGER)"
            " FROM leads WHERE is_sample = 0 AND routed_at IS NOT NULL"))
        route_secs = [r[0] for r in cur.fetchall() if r[0] is not None]
    else:
        cur.execute(_translate(
            "SELECT EXTRACT(EPOCH FROM (routed_at - created_at))::INTEGER"
            " FROM leads WHERE is_sample = 0 AND routed_at IS NOT NULL"))
        route_secs = [r[0] for r in cur.fetchall() if r[0] is not None]
    cur.execute(_translate(
        "SELECT band, COUNT(*) c FROM leads WHERE is_sample = 0 AND band IS NOT NULL"
        " GROUP BY band"))
    by_band = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(_translate(
        "SELECT l.band, o.outcome, COUNT(*) c FROM outcomes o"
        " JOIN leads l ON l.id = o.lead_id"
        " WHERE l.is_sample = 0 GROUP BY l.band, o.outcome"))
    conv = {}
    for band, outcome, c in cur.fetchall():
        conv.setdefault(band or "unscored", {})[outcome] = c
    cur.execute(_translate(
        "SELECT COUNT(*) c FROM dead_letter WHERE resolved = 0"))
    dlq_open = cur.fetchone()[0]
    cur.execute(_translate(
        "SELECT COUNT(*) c FROM leads WHERE is_sample = 1"))
    sample_count = cur.fetchone()[0]
    cur.close()

    hist = {"0-24": 0, "25-49": 0, "50-69": 0, "70-100": 0}
    for s in scores:
        if s < 25:
            hist["0-24"] += 1
        elif s < 50:
            hist["25-49"] += 1
        elif s < 70:
            hist["50-69"] += 1
        else:
            hist["70-100"] += 1
    return {
        "total_leads": total,
        "by_status": by_status,
        "by_band": by_band,
        "score_histogram": hist,
        "median_seconds_to_route": int(statistics.median(route_secs)) if route_secs else None,
        "conversion_by_band": conv,
        "dlq_open": dlq_open,
        "sample_leads": sample_count,
    }
