"""LeadQualifier companion service.

Owns everything n8n orchestrates around:
  intake form UI, enrichment, LLM scoring, notifications (mock Slack + real Slack
  webhook), the human-in-the-loop approval gate, the ops dashboard, and metrics.
The n8n workflow (n8n/workflows/leadqualifier.json) is the visible orchestration;
this service is the boring, reliable plumbing behind it.
"""
import hashlib
import hmac
import json
import os
import time

import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for

import enrich as enrich_mod
import scoring
import store

app = Flask(__name__)

N8N_INTAKE_URL = os.environ.get("N8N_INTAKE_URL", "http://n8n:5678/webhook/lead-intake")
N8N_DECISION_URL = os.environ.get("N8N_DECISION_URL", "http://n8n:5678/webhook/approval-decision")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")
APPROVAL_SECRET = os.environ.get("APPROVAL_SECRET", "change-me-in-production")


def _conn():
    return store.get_conn()


def approval_token(lead_id, decision):
    msg = f"{lead_id}:{decision}".encode()
    return hmac.new(APPROVAL_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify_token(lead_id, decision, token):
    return hmac.compare_digest(approval_token(lead_id, decision), token or "")


def post_to_slack(title, body, approve_url=None, reject_url=None):
    if not SLACK_WEBHOOK_URL:
        return False
    text = f"*{title}*\n{body}"
    if approve_url:
        text += f"\n<{approve_url}|Approve>   <{reject_url}|Reject>"
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - slack is best-effort; mock feed is the record
        app.logger.warning("slack post failed: %s", exc)
        return False


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "leadqualifier-companion"})


@app.get("/")
def intake_form():
    return render_template("form.html")


@app.post("/api/intake")
def api_intake():
    data = request.get_json(force=True, silent=True) or request.form.to_dict() or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    if not name or "@" not in email:
        return jsonify({"error": "name and a valid email are required"}), 400
    lead = {k: (data.get(k) or "").strip() for k in
            ("name", "email", "company", "role", "budget_range", "timeline", "message")}
    lead["source"] = (data.get("source") or "web-form").strip()
    conn = _conn()
    lead_id = store.create_lead(conn, lead)
    store.log_event(conn, lead_id, "intake_received", {"source": lead["source"]})
    # Hand off to n8n for enrichment -> scoring -> routing.
    try:
        r = requests.post(N8N_INTAKE_URL, json={"lead_id": lead_id, **lead}, timeout=15)
        r.raise_for_status()
        store.log_event(conn, lead_id, "handed_to_n8n", {"status": r.status_code})
        n8n_ok = True
    except Exception as exc:  # noqa: BLE001 - intake must never lose a lead
        store.log_event(conn, lead_id, "n8n_handoff_failed", {"error": str(exc)})
        store.add_dead_letter(conn, lead_id, "n8n_handoff", str(exc), lead)
        store.update_lead(conn, lead_id, {"status": "failed"})
        n8n_ok = False
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    if request.form:
        return redirect(url_for("thank_you"))
    return jsonify({"lead_id": lead_id, "n8n_accepted": n8n_ok})


@app.get("/thank-you")
def thank_you():
    return render_template("thankyou.html")


# ---- n8n-called plumbing endpoints -------------------------------------------

@app.post("/api/enrich")
def api_enrich():
    data = request.get_json(force=True) or {}
    result = enrich_mod.enrich(data.get("email", ""), data.get("company", ""),
                               data.get("message", ""))
    lead_id = data.get("lead_id")
    if lead_id:
        conn = _conn()
        store.update_lead(conn, lead_id, {
            "company_enriched": result["company_name"],
            "size_bucket": result["size_bucket"],
            "industry": result["industry"],
            "enrich_confidence": result["confidence"],
            "enrich_source": result["source"],
            "status": "enriched"})
        store.log_event(conn, lead_id, "enriched", result)
        lead = store.get_lead(conn, lead_id)
        if not os.environ.get("LEADQUALIFIER_TEST_DB"):
            conn.close()
        return jsonify({"lead": lead, "enrichment": result})
    return jsonify({"enrichment": result})


@app.post("/api/score")
def api_score():
    data = request.get_json(force=True) or {}
    lead = data.get("lead") or {}
    enrichment = data.get("enrichment")
    started = time.time()
    try:
        result = scoring.score_lead(lead, enrichment)
    except Exception as exc:  # noqa: BLE001 - a scoring failure must not lose the lead
        lead_id = lead.get("lead_id") or data.get("lead_id")
        if lead_id:
            conn = _conn()
            store.add_dead_letter(conn, lead_id, "scoring", str(exc), lead)
            store.update_lead(conn, lead_id, {"status": "failed"})
            store.log_event(conn, lead_id, "scoring_failed", {"error": str(exc)})
            if not os.environ.get("LEADQUALIFIER_TEST_DB"):
                conn.close()
        return jsonify({"error": f"scoring failed: {exc}"}), 502
    lead_id = lead.get("lead_id") or data.get("lead_id")
    if lead_id:
        conn = _conn()
        store.update_lead(conn, lead_id, {
            "score": result["score"], "rationale": result["rationale"],
            "band": result["band"], "status": "scored",
            "scored_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        store.log_event(conn, lead_id, "scored",
                        {**result, "latency_s": round(time.time() - started, 1)})
        full_lead = store.get_lead(conn, lead_id)
        if not os.environ.get("LEADQUALIFIER_TEST_DB"):
            conn.close()
        return jsonify({"lead": full_lead, "enrichment": enrichment, **result})
    return jsonify(result)


@app.post("/api/lead-event")
def api_lead_event():
    data = request.get_json(force=True) or {}
    conn = _conn()
    store.log_event(conn, data["lead_id"], data["event"], data.get("detail"))
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/lead/<int:lead_id>/update")
def api_lead_update(lead_id):
    data = request.get_json(force=True) or {}
    allowed = {"status", "band", "score", "rationale", "routed_at"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if "routed_at" in fields and fields["routed_at"] == "now":
        fields["routed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    store.update_lead(conn, lead_id, fields)
    if data.get("event"):
        store.log_event(conn, lead_id, data["event"], data.get("detail"))
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return jsonify({"ok": True})


@app.post("/api/notify")
def api_notify():
    """Create a notification (mock-Slack feed row) and optionally forward to Slack."""
    data = request.get_json(force=True) or {}
    lead_id, kind = data.get("lead_id"), data.get("kind", "info")
    title, body = data.get("title", ""), data.get("body", "")
    approve_url = reject_url = None
    if kind == "warm_gate" and lead_id:
        approve_url = (f"{PUBLIC_BASE_URL}/api/approval?lead_id={lead_id}"
                       f"&decision=approve&token={approval_token(lead_id, 'approve')}")
        reject_url = (f"{PUBLIC_BASE_URL}/api/approval?lead_id={lead_id}"
                      f"&decision=reject&token={approval_token(lead_id, 'reject')}")
    sent = post_to_slack(title, body, approve_url, reject_url)
    conn = _conn()
    nid = store.add_notification(conn, lead_id, kind, title, body,
                                 approve_url, reject_url, sent_to_slack=sent)
    store.log_event(conn, lead_id, "notified",
                    {"kind": kind, "notification_id": nid, "slack": sent})
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return jsonify({"notification_id": nid, "slack_sent": sent,
                    "approve_url": approve_url, "reject_url": reject_url})


# ---- dead-letter intake (used by the n8n error workflow) ---------------------------

@app.post("/api/deadletter")
def api_deadletter():
    data = request.get_json(force=True) or {}
    lead_id, stage = data.get("lead_id"), data.get("stage", "unknown")
    error = data.get("error", "unknown error")
    conn = _conn()
    store.add_dead_letter(conn, lead_id, stage, error, data.get("payload"),
                          data.get("attempts", 1))
    if lead_id:
        store.log_event(conn, lead_id, "dead_lettered",
                        {"stage": stage, "error": error})
        store.update_lead(conn, lead_id, {"status": "failed"})
    store.add_notification(conn, lead_id, "alert",
                           f"Pipeline failure at stage '{stage}'",
                           f"{error}\n\nLead #{lead_id or '?'} is in the dead-letter queue."
                           " Nothing was silently dropped.")
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return jsonify({"ok": True})


# ---- human-in-the-loop approval gate ------------------------------------------

@app.get("/api/approval")
def api_approval():
    lead_id = request.args.get("lead_id", type=int)
    decision = request.args.get("decision", "")
    token = request.args.get("token", "")
    if decision not in ("approve", "reject") or not lead_id \
            or not verify_token(lead_id, decision, token):
        return render_template("approval.html", ok=False,
                               message="Invalid or expired approval link."), 403
    conn = _conn()
    lead = store.get_lead(conn, lead_id)
    if not lead:
        if not os.environ.get("LEADQUALIFIER_TEST_DB"):
            conn.close()
        return render_template("approval.html", ok=False, message="Lead not found."), 404
    if lead["status"] not in ("pending_approval", "scored"):
        if not os.environ.get("LEADQUALIFIER_TEST_DB"):
            conn.close()
        return render_template("approval.html", ok=False, message=(
            f"This lead was already handled (status: {lead['status']}).")), 409
    new_status = "approved" if decision == "approve" else "declined"
    store.update_lead(conn, lead_id, {"status": new_status})
    store.log_event(conn, lead_id, f"human_{decision}d", {})
    # Resume the pipeline in n8n, which performs the final routing.
    n8n_ok = True
    try:
        r = requests.post(N8N_DECISION_URL,
                          json={"lead_id": lead_id, "decision": decision,
                                "score": lead["score"], "band": lead["band"]},
                          timeout=15)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        n8n_ok = False
        store.add_dead_letter(conn, lead_id, "approval_resume", str(exc),
                              {"decision": decision})
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return render_template("approval.html", ok=True, decision=decision,
                           lead=lead, n8n_ok=n8n_ok)


# ---- mock Slack feed, dashboard, metrics ---------------------------------------

@app.get("/mock-slack")
def mock_slack():
    conn = _conn()
    notes = store.list_notifications(conn)
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return render_template("mockslack.html", notifications=notes,
                           slack_configured=bool(SLACK_WEBHOOK_URL))


@app.get("/dashboard")
def dashboard():
    conn = _conn()
    m = store.metrics(conn)
    notes = store.list_notifications(conn, limit=10)
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return render_template("dashboard.html", m=m, notifications=notes)


@app.get("/api/metrics")
def api_metrics():
    conn = _conn()
    m = store.metrics(conn)
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return jsonify(m)


@app.post("/api/outcome")
def api_outcome():
    data = request.get_json(force=True) or {}
    if data.get("outcome") not in ("won", "lost"):
        return jsonify({"error": "outcome must be 'won' or 'lost'"}), 400
    conn = _conn()
    store.record_outcome(conn, data["lead_id"], data["outcome"])
    store.log_event(conn, data["lead_id"], "outcome_recorded",
                    {"outcome": data["outcome"]})
    if not os.environ.get("LEADQUALIFIER_TEST_DB"):
        conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    store.init_db()
    app.run(host="0.0.0.0", port=5000)
