# LeadQualifier — AI Lead-Qualification Pipeline Demo

An end-to-end lead-qualification pipeline: a lead submits one form → gets enriched →
scored by an LLM against a real BANT rubric → routed to the right place, with a
human-in-the-loop gate for borderline scores. The live demo of the **Workflow
Automation Sprint** package ($1,500–$3,500 fixed).

**Why this exists:** founders don't lose deals for lack of leads — they lose them to
slow follow-up and manual triage. This pipeline removes the busywork *and* the
failure modes buyers actually fear: silent misfires, black-box decisions, and
demos that never survive contact with production.

> All numbers marked *illustrative* are planning estimates, not measured results.
> Nothing here invents client outcomes.

## Architecture

```
                    ┌──────────────────────────────────────────────┐
  Browser           │  Companion (Flask :5000)                     │
  ┌──────────┐      │  ┌────────────┐  ┌──────────┐  ┌───────────┐  │
  │ Intake   │─────▶│  │ /api/intake│  │ Mock     │  │ Ops       │  │
  │ form     │      │  │ form UI    │  │ Slack    │  │ dashboard │  │
  └──────────┘      │  └─────┬──────┘  │ feed     │  │ /metrics  │  │
                    │        │         └──────────┘  └───────────┘  │
                    └────────┼─────────────────────────────────────┘
                             │ POST /webhook/lead-intake
                    ┌────────▼─────────────────────────────────────┐
                    │  n8n (:5678) — the visible orchestration     │
                    │  lead-intake → normalize → enrich → score →   │
                    │  Is Hot? ──yes──▶ notify + route (senior rep) │
                    │     │no                                      │
                    │     ▼                                        │
                    │  Needs Approval? ──yes──▶ gate: notify with  │
                    │     │no              approve/reject links    │
                    │     ▼                    │                   │
                    │  Junk → auto-decline     │  ┌──────────────┐  │
                    │                          └──▶ approval-decision webhook → route │
                    └────────┬─────────────────────────────────────┘
                             │ HTTP (companion APIs)
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
       ┌────────────┐ ┌────────────┐ ┌─────────────┐
       │ Postgres   │ │ Ollama     │ │ Slack       │
       │ leads,     │ │ llama3.2:3b│ │ incoming    │
       │ events,    │ │ scoring +  │ │ webhook     │
       │ DLQ, notes │ │ enrichment │ │ (optional)  │
       └────────────┘ └────────────┘ └─────────────┘
```

**Design choice worth knowing:** the n8n workflow uses only Webhook, Code, IF, and
HTTP Request nodes — no n8n credentials needed. All state, scoring, and
notifications go through the companion's HTTP API. The workflow JSON imports into
any n8n instance with zero credential setup, which is exactly what a buyer wants
to see: *the orchestration is theirs to read and keep.*

n8n canvas, annotated (import `n8n/workflows/leadqualifier.json` to see it live):

```
[Lead Intake Webhook] → [Normalize Lead] → [Enrich Lead] → [Score Lead] → [Is Hot? ≥70]
                                                                              ├─ yes → [Notify Hot Rep] → [Mark Routed Hot]
                                                                              └─ no → [Needs Approval? ≥50]
                                                                                         ├─ yes → [Flag Pending Approval] → [Request Approval] ─(human clicks link)→
                         ┌──────────────────────────────────────────────────────────────────────────────────────────────┘
[Approval Decision Webhook] → [Build Routing Decision] → [Approved?]
                                                        ├─ yes → [Notify Warm Routed] → [Mark Routed Warm]
                                                        └─ no → [Notify Declined] → [Mark Declined]
                                                                                         └─ no → [Notify Junk] → [Mark Routed Junk]
```

A second workflow, `LeadQualifier - Error Handler`, is wired as the error workflow:
any node failure (after its own retries) lands the lead in the **dead-letter queue**
with the stage, error, and payload — plus an alert notification. Nothing is silently
dropped. Ever.

## Quickstart

Prerequisites: Docker + Docker Compose.

```bash
cp .env.example .env        # then set POSTGRES_PASSWORD, N8N_PASSWORD,
                            # N8N_ENCRYPTION_KEY, APPROVAL_SECRET
docker compose up -d --build
./scripts/import-workflows.sh
# optional: clearly-labeled sample leads for the dashboard
docker compose exec companion python seed_sample.py
```

Open:

| URL | What |
|---|---|
| http://localhost:5000/ | Intake form — submit a lead, watch the pipeline |
| http://localhost:5000/mock-slack | Notification feed (works with zero Slack credentials) |
| http://localhost:5000/dashboard | Ops dashboard: volume, median time-to-route, score distribution, conversion by band |
| http://localhost:5678/ | n8n editor (login: `N8N_USER` / `N8N_PASSWORD`) — activate both workflows |

The first `ollama-setup` run pulls `llama3.2:3b` (~2 GB, one time). The live demo
costs **$0** — scoring runs on the local model.

## The scoring rubric (transparency sells)

The exact prompt is versioned at [`prompts/bant-rubric.md`](prompts/bant-rubric.md) —
buyers can read and edit it; changing the file changes scoring on the next lead.

- **Budget (0–25)** — explicit budget or clear ability to afford the engagement.
- **Authority (0–25)** — founder/owner/director scores high; unknown title scores low.
- **Need (0–25)** — concrete, automation-shaped pain in their own words.
- **Timeline (0–25)** — ASAP/this month scores high; "just researching" scores low.

Total 0–100 → **hot (≥70)**, **warm (50–69, human gate)**, **junk (<50)**.
Anti-gaming rules are in the prompt: missing information scores *low* on that
dimension, flattery doesn't move the needle, and ties break downward — a false
"hot" wastes expensive rep time.

## Buyer screening Q&A

The questions smart buyers ask, answered before the first call:

**What do you refuse to automate?**
Anything customer-facing without a human review step, payment/refund decisions,
anything touching regulated data (health, finance) without the client's compliance
sign-off, and "fully autonomous" anything as a selling point. If a step can quietly
cost you a customer, it gets a gate or it doesn't get automated.

**Where is the human gate, and can it be skipped?**
Scores 50–69 pause in `pending_approval` and notify with one-click Approve/Reject
links (HMAC-signed, single-use semantics — a second click is rejected, never
double-routed). The gate is enforced in the n8n workflow itself: there is no code
path from scoring to routing that bypasses it. Skipping it means editing the
workflow — which the client owns (see below).

**What happens at 2am when it breaks?**
Every external call (enrichment, scoring, notifications) retries with backoff
(3 tries). If it still fails, the lead goes to the dead-letter queue with the
stage, error, and payload, an alert fires, and the intake path *never* loses the
lead — a failed n8n handoff is dead-lettered at intake too. Morning starts with a
queue to work through, not a mystery.

**What does month three cost?**
Honest answer: with the default local model, ~$0 in AI costs. With a paid provider
(OpenAI/Anthropic on the client's own key), roughly **$0.05–0.15 per lead**
*(illustrative)*. n8n self-hosted has no per-workflow license fee. The server for
this demo stack is ~€5/month *(illustrative)*. No retainer is required — but a
small one is offered for rubric tuning and monitoring (quoted separately, never
assumed).

**Who owns the workflows, prompts, and logins when you leave?**
The client — in writing, before work starts. The n8n workflow JSON, the rubric
prompts, the runbook, and all credentials are handed over; nothing phones home.

## Fixed scope + EXCLUSIONS

**The Workflow Automation Sprint ($1,500–$3,500 fixed) includes:**
- One intake channel (web form → webhook; Typeform/Google Form on request)
- Enrichment (directory/lookup + LLM fallback), BANT scoring, three-lane routing
- Human-in-the-loop gate with approve/reject, Slack or mock-Slack notifications
- Ops dashboard + dead-letter queue + runbook + handover session (1 hr)

**EXCLUSIONS (not in the fixed price):**
- CRM/ESP integrations beyond one webhook target (quoted separately)
- Custom model training or fine-tuning
- More than one workflow / additional intake channels
- Ongoing retainer, monitoring SLAs, or 24/7 support (separate offer)
- Compliance review for regulated industries (client's counsel signs off)

Scope changes mid-project are re-quoted in writing before work continues.

## Shadow-run testing (how we go live without surprises)

Before a client's pipeline goes live, it runs in **shadow mode** against their real
history: the last 30–90 days of actual leads are replayed through scoring and
routing, but notifications go to a test channel and no rep is pinged. We then review
together: which leads would have been mis-scored, where the rubric needs tuning,
what the human-gate volume really looks like. Only when the shadow run's routing
matches the client's judgment do we flip the live switch — usually with the gate
threshold set conservatively (wider human review) for the first two weeks.

## Impact table

| Metric | Before (manual) | After (LeadQualifier) |
|---|---|---|
| Time to first touch on a hot lead | hours *(illustrative)* | ~60 seconds *(illustrative)* |
| Triage time per lead | ~8 min *(illustrative)* | ~45 sec of compute, 0 human min *(illustrative)* |
| 200 leads/month | ~27 hrs of staff time *(illustrative)* | ~2.5 hrs, only on gated/won leads *(illustrative)* |
| Misrouted / dropped leads | unknown, unmeasured *(typical)* | every lead logged, gated, or dead-lettered — none silent |

*All figures in this table are illustrative planning estimates, not measured
results. The honest version of this table is built from the client's own baseline
during the paid mini-audit — that's the first engagement for a reason.*

## Ops notes

- **Change the rubric:** edit `prompts/bant-rubric.md`, restart the companion.
  Scores change on the next lead. No code changes.
- **Add a routing rule:** edit the IF nodes in the n8n workflow (e.g. split "hot"
  into tiers) or add a notification kind in the companion — routing rules are config.
- **Monitor failures:** the dashboard shows open dead-letter items; each links the
  stage, error, and payload. Replay a fixed lead by re-posting it to the intake
  webhook.
- **Switch LLM provider:** set `LLM_PROVIDER=openai|anthropic` + API key in `.env`
  (client pays their own key). `stub` is a deterministic test double for CI.

## Slack

- **Zero credentials:** everything lands in the mock-Slack feed at `/mock-slack`.
- **Real Slack:** set `SLACK_WEBHOOK_URL` to an incoming webhook — notifications
  forward there too. Approve/Reject arrive as links (they need `PUBLIC_BASE_URL`
  reachable from the clicker's browser).

## Demo

[`demo/60-second-demo-script.md`](demo/60-second-demo-script.md) — the shot-by-shot
script. Recorded by Vishal; nobody else records demos for this portfolio.

## Project structure

```
├── docker-compose.yml      # n8n + postgres + ollama + companion
├── .env.example
├── companion/              # Flask service: intake UI, enrich, score, notify,
│   ├── app.py              #   approval gate, dashboard, metrics
│   ├── scoring.py          #   LLM provider routing (ollama/openai/anthropic/stub)
│   ├── enrich.py           #   mock directory + LLM-inference fallback
│   ├── store.py            #   Postgres data layer (SQLite in tests)
│   ├── seed_sample.py      #   clearly-labeled sample leads (is_sample=1)
│   └── tests/              #   18 tests, no Docker/network needed
├── n8n/workflows/
│   ├── leadqualifier.json         # main pipeline (import-ready)
│   └── leadqualifier-errors.json  # error workflow → dead-letter queue
├── prompts/
│   ├── bant-rubric.md        # the actual scoring prompt (auditable)
│   └── enrichment-prompt.md
├── scripts/import-workflows.sh
├── demo/60-second-demo-script.md
└── .github/workflows/build.yml
```
