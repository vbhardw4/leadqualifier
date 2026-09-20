"""LeadQualifier tests — run with:  LEADQUALIFIER_TEST_DB=1 LLM_PROVIDER=stub python -m pytest
No Docker, no network, no models needed."""
import os
import sys

import pytest

os.environ["LEADQUALIFIER_TEST_DB"] = "1"
os.environ["LLM_PROVIDER"] = "stub"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402
import enrich as enrich_mod  # noqa: E402
import scoring  # noqa: E402
import store  # noqa: E402

flask_app = appmod.app
flask_app.config["TESTING"] = True


@pytest.fixture(autouse=True)
def clean_db():
    store.init_db()
    yield
    conn = store.get_conn()
    for t in ("outcomes", "dead_letter", "notifications", "lead_events", "leads"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


@pytest.fixture()
def client(monkeypatch):
    def fake_post(url, **kwargs):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass
        fake_post.urls.append(url)
        return R()
    fake_post.urls = []
    monkeypatch.setattr("requests.post", fake_post)
    return flask_app.test_client(), fake_post


# ---- enrichment ----

def test_enrich_known_domain():
    r = enrich_mod.enrich("jane@acmerealty.com", "", "we need help")
    assert r["company_name"] == "Acme Realty Group"
    assert r["industry"] == "real estate"
    assert r["source"] == "mock_directory"


def test_enrich_freemail_no_company_uses_llm_stub():
    r = enrich_mod.enrich("bob@gmail.com", "", "I run a dental clinic", provider="stub")
    assert r["source"] == "llm_inference"
    assert r["confidence"] <= 0.6


def test_enrich_self_reported_company():
    r = enrich_mod.enrich("bob@gmail.com", "Bob's Plumbing", "need automation")
    assert r["company_name"] == "Bob's Plumbing"
    assert r["source"] == "self_reported"


# ---- scoring (stub) ----

def test_stub_scores_hot_lead_high():
    lead = {"name": "Jane", "email": "j@acme.com", "role": "Founder",
            "budget_range": "$3k – $10k", "timeline": "ASAP",
            "message": "leads sit in gmail for hours, we lose track of follow-ups"}
    r = scoring.score_lead(lead, provider="stub")
    assert r["band"] == "hot" and r["score"] >= 70
    assert set(r["dimensions"]) == {"budget", "authority", "need", "timeline"}


def test_stub_scores_vague_lead_low():
    lead = {"name": "X", "email": "x@gmail.com", "role": "",
            "message": "just exploring AI"}
    r = scoring.score_lead(lead, provider="stub")
    assert r["band"] == "junk" and r["score"] < 50


def test_band_boundaries():
    assert scoring.band_for(70) == "hot"
    assert scoring.band_for(69) == "warm"
    assert scoring.band_for(50) == "warm"
    assert scoring.band_for(49) == "junk"


def test_normalize_clamps():
    r = scoring._normalize({"score": 999, "rationale": "x",
                            "dimensions": {"budget": 99, "authority": -5,
                                           "need": 10, "timeline": 10}})
    assert r["score"] == 100 and r["dimensions"]["budget"] == 25
    assert r["dimensions"]["authority"] == 0


# ---- approval tokens ----

def test_approval_token_roundtrip():
    t = appmod.approval_token(42, "approve")
    assert appmod.verify_token(42, "approve", t)
    assert not appmod.verify_token(42, "reject", t)
    assert not appmod.verify_token(43, "approve", t)


# ---- HTTP endpoints ----

def test_intake_validation(client):
    c, _ = client
    r = c.post("/api/intake", json={"name": "", "email": "bad"})
    assert r.status_code == 400


def test_intake_hands_off_to_n8n(client):
    c, fake_post = client
    r = c.post("/api/intake", json={"name": "Jane", "email": "jane@acmerealty.com",
                                    "message": "help"})
    assert r.status_code == 200
    assert fake_post.urls and "lead-intake" in fake_post.urls[0]
    conn = store.get_conn()
    lead = store.get_lead(conn, r.get_json()["lead_id"])
    assert lead["status"] == "new"


def test_intake_n8n_down_dead_letters(monkeypatch):
    def boom(url, **kwargs):
        raise ConnectionError("n8n down")
    monkeypatch.setattr("requests.post", boom)
    c = flask_app.test_client()
    r = c.post("/api/intake", json={"name": "Jane", "email": "j@x.com"})
    assert r.status_code == 200 and r.get_json()["n8n_accepted"] is False
    conn = store.get_conn()
    lead = store.get_lead(conn, r.get_json()["lead_id"])
    assert lead["status"] == "failed"
    cur = conn.execute("SELECT COUNT(*) FROM dead_letter")
    assert cur.fetchone()[0] == 1


def test_enrich_endpoint(client):
    c, _ = client
    r = c.post("/api/enrich", json={"lead_id": None, "email": "jane@acmerealty.com",
                                    "company": "", "message": ""})
    assert r.get_json()["enrichment"]["industry"] == "real estate"


def test_score_endpoint_persists(client):
    c, _ = client
    conn = store.get_conn()
    lid = store.create_lead(conn, {"name": "Jane", "email": "j@acme.com",
                                   "role": "Founder", "timeline": "ASAP",
                                   "budget_range": "$3k+", "message": "leads go cold"})
    r = c.post("/api/score", json={"lead_id": lid, "lead": {
        "lead_id": lid, "name": "Jane", "role": "Founder",
        "timeline": "ASAP", "budget_range": "$3k+",
        "message": "leads go cold, manual follow-up"}})
    body = r.get_json()
    assert r.status_code == 200 and body["band"] in ("hot", "warm", "junk")
    lead = store.get_lead(conn, lid)
    assert lead["status"] == "scored" and lead["score"] == body["score"]


def test_notify_warm_gate_builds_signed_links(client):
    c, _ = client
    r = c.post("/api/notify", json={"lead_id": 7, "kind": "warm_gate",
                                    "title": "Approval needed", "body": "score 62"})
    body = r.get_json()
    assert "token=" in body["approve_url"] and "decision=approve" in body["approve_url"]
    assert "decision=reject" in body["reject_url"]


def test_approval_flow_approve_then_reject_conflict(client):
    c, fake_post = client
    conn = store.get_conn()
    lid = store.create_lead(conn, {"name": "Sam", "email": "s@x.com"})
    store.update_lead(conn, lid, {"status": "pending_approval", "score": 62,
                                  "band": "warm", "rationale": "r"})
    tok = appmod.approval_token(lid, "approve")
    r = c.get(f"/api/approval?lead_id={lid}&decision=approve&token={tok}")
    assert r.status_code == 200
    assert store.get_lead(conn, lid)["status"] == "approved"
    assert any("approval-decision" in u for u in fake_post.urls)
    # second click on the same link must be rejected, not double-routed
    r2 = c.get(f"/api/approval?lead_id={lid}&decision=approve&token={tok}")
    assert r2.status_code == 409


def test_approval_bad_token(client):
    c, _ = client
    r = c.get("/api/approval?lead_id=1&decision=approve&token=nope")
    assert r.status_code == 403


def test_metrics_and_outcome(client):
    c, _ = client
    conn = store.get_conn()
    lid = store.create_lead(conn, {"name": "A", "email": "a@x.com"})
    store.update_lead(conn, lid, {"status": "routed_hot", "score": 80, "band": "hot",
                                  "routed_at": "2026-01-01 00:00:10",
                                  "created_at": "2026-01-01 00:00:00"})
    c.post("/api/outcome", json={"lead_id": lid, "outcome": "won"})
    m = c.get("/api/metrics").get_json()
    assert m["total_leads"] == 1
    assert m["by_band"]["hot"] == 1
    assert m["conversion_by_band"]["hot"]["won"] == 1
    assert m["median_seconds_to_route"] == pytest.approx(10, abs=2)


def test_pages_render(client):
    c, _ = client
    for path in ("/", "/thank-you", "/mock-slack", "/dashboard", "/health"):
        assert c.get(path).status_code == 200, path


# ---- scoring (gemini provider, mocked HTTP) ----

_GEMINI_SCORE_BODY = {
    "candidates": [{
        "content": {"parts": [{"text": (
            '{"score": 82, "rationale": "CTO with $8k budget and ASAP timeline.", '
            '"dimensions": {"budget": 20, "authority": 22, "need": 20, "timeline": 20}}'
        )}]},
    }],
}

_GEMINI_ENRICH_BODY = {
    "candidates": [{
        "content": {"parts": [{"text": (
            '{"company_name": "Acme Dental", "industry": "healthcare", '
            '"size_bucket": "11-50", "confidence": 0.9, '
            '"basis": "lead said \\"our dental clinic\\""}'
        )}]},
    }],
}


def _fake_gemini_post(url, **kwargs):
    assert "generativelanguage.googleapis.com" in url
    assert kwargs["headers"]["x-goog-api-key"] == "test-key"
    body = kwargs["json"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseSchema" in body["generationConfig"]
    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return _fake_gemini_post.payload
    return R()


def test_gemini_scoring_path_mocked(monkeypatch):
    monkeypatch.setattr("requests.post", _fake_gemini_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _fake_gemini_post.payload = _GEMINI_SCORE_BODY
    lead = {"name": "Jane", "email": "j@acmedental.com", "role": "CTO",
            "budget_range": "$8k", "timeline": "ASAP",
            "message": "our dental clinic needs automation now"}
    r = scoring.score_lead(lead, provider="gemini")
    assert r["score"] == 82 and r["band"] == "hot"
    assert r["rationale"].startswith("CTO")
    assert set(r["dimensions"]) == {"budget", "authority", "need", "timeline"}


def test_gemini_enrichment_path_mocked(monkeypatch):
    monkeypatch.setattr("requests.post", _fake_gemini_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _fake_gemini_post.payload = _GEMINI_ENRICH_BODY
    r = scoring.llm_infer_enrichment("I run our dental clinic in Brampton", provider="gemini")
    assert r["company_name"] == "Acme Dental"
    assert r["source"] == "llm_inference"
    # low-confidence fallback: model said 0.9, must be capped at 0.6
    assert r["confidence"] == pytest.approx(0.6)


def test_gemini_missing_key_fails_fast(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(KeyError):
        scoring.score_lead({"name": "X"}, provider="gemini")
