#!/usr/bin/env python3
"""Seed clearly-labeled SAMPLE leads so the dashboard/demo isn't empty.

Every row has is_sample=1 and is EXCLUDED from all dashboard metrics.
Run inside the companion container:  docker compose exec companion python seed_sample.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

SAMPLES = [
    # (name, email, company, role, budget, timeline, message, score, rationale, band,
    #  status, outcome)
    ("Priya Nair", "priya@acmerealty.com", "Acme Realty Group", "Founder",
     "$3k – $10k", "ASAP / this month",
     "New inquiries sit in Gmail for 5-6 hours before anyone replies. We lost two listings last month to slow follow-up.",
     88, "Founder with urgent, concrete follow-up pain and stated budget.", "hot",
     "routed_hot", "won"),
    ("Tom Becker", "tom@brightsmile.dental", "Bright Smile Dental", "Owner",
     "$1k – $3k", "This quarter",
     "Front desk copy-pastes appointment requests from web forms into our scheduler. Double bookings happen weekly.",
     76, "Owner, real manual-process pain, budget in range, timeline this quarter.", "hot",
     "routed_hot", "won"),
    ("Aisha Khan", "aisha@harborlaw.com", "Harbor Law LLP", "Partner",
     "$3k – $10k", "ASAP / this month",
     "Intake calls go to voicemail after hours and nobody calls back. We need every consultation request triaged same-day.",
     91, "Partner (decision-maker), urgent intake pain, strong budget signal.", "hot",
     "routed_hot", "won"),
    ("Marco Rossi", "marco@sunsetstays.com", "Sunset Stays", "Operations manager",
     "$1k – $3k", "This quarter",
     "Guest messages arrive on three platforms and the night team misses half of them.",
     64, "Ops manager (influencer, not decider), real multi-channel pain, loose timeline.", "warm",
     "routed_warm", "won"),
    ("Lena Fischer", "lena@gmail.com", "", "Marketing lead",
     "Not sure yet", "This quarter",
     "We get maybe 40 demo requests a month and SDRs cherry-pick. Want fair, fast routing.",
     58, "Practitioner title, real routing pain, but no company, no budget, freemail.", "warm",
     "routed_warm", "lost"),
    ("Dev Patel", "dev@northwindtraders.com", "Northwind Traders", "VP Sales",
     "$10k+", "ASAP / this month",
     "Inbound demo requests wait 24h for assignment. Competitors reply in minutes.",
     84, "VP Sales, urgent speed-to-lead pain, strong budget.", "hot",
     "routed_hot", None),
    ("Grace Okafor", "grace@yahoo.com", "", "",
     "", "Just researching",
     "just exploring what AI can do for small business",
     22, "No role, no company, no problem stated — classic junk.", "junk",
     "routed_junk", None),
    ("Sam Wilson", "sam.wilson@outlook.com", "", "Student",
     "Under $1k", "",
     "looking for a free tool for a class project on chatbots",
     15, "Student, no budget, need doesn't fit automation services.", "junk",
     "routed_junk", None),
    ("Nadia Hassan", "nadia@acmerealty.com", "Acme Realty Group", "Marketing lead",
     "$1k – $3k", "This quarter",
     "Agents complain good leads go to whoever shouts loudest. Need scoring we can defend.",
     61, "Influencer title, real fairness pain, budget and timeline present.", "warm",
     "declined", None),
    ("Omar Farouk", "omar@harborlaw.com", "Harbor Law LLP", "Office manager",
     "$1k – $3k", "This quarter",
     "We manually retype referral details from email into the case system. Typos cause real problems.",
     55, "Non-deciding role but concrete manual-entry pain with business impact.", "warm",
     "routed_warm", None),
    ("Julia Chen", "julia@sunsetstays.com", "Sunset Stays", "CEO",
     "$3k – $10k", "ASAP / this month",
     "OTA commission is killing us. Every direct booking inquiry must get an instant, personal reply.",
     86, "CEO, urgent revenue-tied pain, solid budget.", "hot",
     "routed_hot", None),
    ("Rob Taylor", "rob@gmail.com", "Rob's Plumbing", "Owner",
     "Under $1k", "Just researching",
     "maybe interested sometime next year, just looking around for now",
     38, "Owner title helps, but no budget, no timeline, vague need.", "junk",
     "routed_junk", None),
]


def main():
    store.init_db()
    conn = store.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM leads WHERE is_sample = 1")
    if cur.fetchone()[0]:
        print("sample leads already seeded — skipping")
        return
    # Spread created_at over the last 14 days for a realistic dashboard.
    for i, s in enumerate(SAMPLES):
        (name, email, company, role, budget, timeline, message,
         score, rationale, band, status, outcome) = s
        ts = (datetime.utcnow() - timedelta(days=(i * 37) % 14)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            """INSERT INTO leads
               (name,email,company,role,budget_range,timeline,message,source,status,
                company_enriched,size_bucket,industry,enrich_confidence,enrich_source,
                score,rationale,band,created_at,scored_at,routed_at,is_sample)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (name, email, company, role, budget, timeline, message, "sample-seed",
             status, company or "unknown", "11-50", "services", 0.5, "sample",
             score, rationale, band, ts, ts, ts))
        lid = cur.lastrowid
        if outcome:
            cur.execute("INSERT INTO outcomes (lead_id, outcome) VALUES (?, ?)",
                        (lid, outcome))
        cur.execute("INSERT INTO lead_events (lead_id, event, detail) VALUES (?, ?, ?)",
                    (lid, "sample_seeded", '{"note": "clearly-labeled demo sample data"}'))
    # NOTE: uses sqlite datetime() syntax; for postgres replace with NOW() - INTERVAL.
    conn.commit()
    print(f"seeded {len(SAMPLES)} sample leads (is_sample=1, excluded from metrics)")


if __name__ == "__main__":
    main()
