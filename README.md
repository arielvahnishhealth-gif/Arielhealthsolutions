# Health Insurance Lead Bot

A lead generation and qualification bot built from the handoff bundle and the follow-up end-to-end repo spec.

It now has two entry points:

- `run.py`: quick local dashboard with SQLite, encrypted phone/email fields, DNC suppression, TCPA logs, qualifier-first pipeline, READY-only delivery logs, metrics, exports, and calendar invites.
- `app/main.py`: optional Docker/FastAPI stack with Postgres, Redis/RQ workers, Reddit/X/SerpAPI/Trends listeners, HubSpot sync, email alerts, 31-state geo scoring, and a sheet-style qualifier form.

## Quick Local Dashboard

Start the polished local dashboard:

```bash
scripts/start_dashboard.sh
```

Then open:

```text
http://127.0.0.1:8080/dashboard
```

Private login:

```text
Username: owner
Password: principal-dev-key
```

For real use, set these before starting the app:

```bash
export LEAD_BOT_USERNAME=your-username
export LEAD_BOT_PASSWORD='use-a-long-private-password'
export LEAD_BOT_WEBHOOK_SECRET='use-a-different-long-random-secret'
```

If you deploy behind HTTPS, also set:

```bash
export LEAD_BOT_COOKIE_SECURE=true
```

AI helper:

```bash
export OPENAI_API_KEY='your-openai-api-key'
export AI_MODEL=gpt-4.1-mini
```

If `OPENAI_API_KEY` is blank, the dashboard AI Helper still works with a built-in local advisor. The AI context intentionally omits phone numbers, emails, and provider tokens.

Setup panel:

- Shows which business integrations are ready or missing.
- Tests Calendly and HubSpot when configured.
- Webhook secret and private login should be marked ready before using real lead sources.
- HubSpot, booking URL, owner, licensed states, and templates are configured in the Automation panel.

## Docker/FastAPI Stack

Start the FastAPI lead engine locally, without Docker:

```bash
scripts/start_fastapi_local.sh
```

Then open:

```text
http://127.0.0.1:8090/docs
```

This local mode uses SQLite and automatically runs lead ingest inline when Redis is not running.

```bash
cp .env.example .env
docker compose up -d --build
curl -s localhost:8080/health
curl -X POST localhost:8080/run/listeners
```

Manual test:

```bash
curl -X POST localhost:8080/ingest \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/post/123","message":"Looking for private PPO in Texas. COBRA is expensive.","name":"Test Prospect"}'
```

Qualifier form:

```text
http://localhost:8080/qualify/form/<lead_id>
```

FastAPI stack smoke test after installing `requirements.txt`:

```bash
DATABASE_URL=sqlite:///data/smoke_fastapi.sqlite3 python scripts/smoke_fastapi_stack.py
```

## Configuration

Copy `.env.example` into your runtime environment or export the values you need before starting the app.

Important defaults:

- `QUALIFIER_API_KEY=qualifier-dev-key`
- `PRINCIPAL_API_KEY=principal-dev-key`
- `PORT=8080`
- `LEAD_BOT_DB=data/lead_bot.sqlite3`
- `DATABASE_URL=postgresql://postgres:postgres@db:5432/leads`
- `REDIS_URL=redis://redis:6379/0`

If `FERNET_KEY` is not set, the app derives a local key from `APP_SECRET`. Set a durable `FERNET_KEY` before using the database for real leads.

Generate one with:

```bash
/Users/arielvahnish/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Flow

The pipeline follows the handoff:

```text
NEW -> TRIAGE -> QUALIFIED -> READY -> WON/LOST
```

Qualified leads receive the configured Calendly booking link, and `READY` leads are created after a Calendly booking is confirmed. HubSpot remains optional for contact sync.

The Docker/FastAPI listener flow finds active insurance intent from Reddit, X/Twitter, Google results, and trends. It prioritizes private PPO/HSA/executive language, still captures ACA intent, and geo-boosts mentions of licensed states.

Licensed states:

```text
TX, NM, UT, CO, SD, NE, KS, MO, AR, LA, MS, AL, GA, FL, SC, NC, VA, WV, PA, OH, IN, KY, TN, IL, IA, MI, WI, OK, MD, DE, DC
```

## Local Dashboard API

Local dashboard requests are trusted from `127.0.0.1`. External calls should use `X-API-Key`.

Qualifier endpoints:

```text
POST /api/leads
POST /api/leads/{id}/assign
POST /api/leads/{id}/qualify
POST /api/leads/{id}/stage/ready
POST /api/dnc
GET  /api/leads/{id}/calendar/ics
```

Principal endpoints:

```text
POST /api/leads/{id}/stage/result
GET  /leads.csv
GET  /leads/download.zip
```

Monitoring and dashboard data:

```text
GET /health
GET /api/dashboard
GET /leads/counts
GET /metrics/prom
```

Business automation endpoints:

```text
GET  /api/settings
POST /api/settings
POST /api/webhooks/meta
POST /api/webhooks/google
POST /api/webhooks/website
POST /api/webhooks/healthsherpa
POST /api/webhooks/marketplace
POST /api/webhooks/cobraprospect
POST /api/webhooks/layoffsignal
POST /api/webhooks/publicsocial
POST /api/webhooks/referralpartner
POST /api/webhooks/retargeting
POST /api/webhooks/ushealthgroup
POST /api/webhooks/yellowpages
POST /api/opt-out
POST /api/automation/run
GET  /api/source-metrics
GET  /api/compliance/export.zip
GET  /api/backup.zip
```

Daily ops job:

```bash
LEAD_BOT_URL=http://127.0.0.1:8080 PRINCIPAL_API_KEY=principal-dev-key .venv/bin/python scripts/run_daily_ops.py
```

Webhook payloads can use direct fields such as `name`, `phone`, `email`, `state`, `intent`, `tcpa_consent`, and `consent_text`. Meta-style `field_data` arrays are also accepted.

Webhook requests must include your secret:

```text
X-Webhook-Secret: your-secret
```

or:

```text
/api/webhooks/website?secret=your-secret
```

HealthSherpa leads can post to:

```text
/api/webhooks/healthsherpa?secret=your-secret
```

The payload can use the same direct fields as other lead sources. `lead_id` and `healthsherpa_url` are also accepted when available.

USHealth Group plan-search leads can post to:

```text
/api/webhooks/ushealthgroup?secret=your-secret
```

The payload can use the same direct fields as other lead sources. `lead_id` and `ushealthgroup_url` are also accepted when available.

Yellow Pages or other permitted business-directory records can post to:

```text
/api/webhooks/yellowpages?secret=your-secret
```

The payload can use the same direct fields as other lead sources. `lead_id`, `company`, and `yellowpages_url` are also accepted when available. Use this only for records you are allowed to import or contact.

Warm-intent sources can post to these endpoints:

```text
/api/webhooks/marketplace?secret=your-secret
/api/webhooks/cobraprospect?secret=your-secret
/api/webhooks/layoffsignal?secret=your-secret
/api/webhooks/publicsocial?secret=your-secret
/api/webhooks/referralpartner?secret=your-secret
/api/webhooks/retargeting?secret=your-secret
```

Use these for permissioned marketplace shoppers, COBRA/job-loss form fills, public social intent routed through a consent form, referral partners, and retargeting campaigns. Public signals should guide targeting; direct call/text outreach still needs documented consent and DNC/TCPA checks.

Qualified-to-Calendly flow:

- QUALIFIED leads show a `Book` action that opens the configured Calendly link.
- The app pre-fills `name` and `email` in the Calendly URL when available.
- The `Email` action opens a prefilled email draft with the Calendly booking link.
- Calendly can post booking events to `POST /api/webhooks/calendly` with `X-Webhook-Secret`, or the dashboard can pull bookings with `CALENDLY_API_TOKEN`.
- When a Calendly booking webhook or API sync includes the invitee email and start time, the matching lead is moved to READY and delivery rules run.

Use a public Calendly event link for prospects, usually something like:

```text
https://calendly.com/your-name/your-event
```

The Calendly admin URL under `/app/scheduling/...` is useful for setup, but prospects usually cannot book through that private admin page.

Manual follow-up threads:

- Click `Thread` on any lead row to view saved qualifier/lead messages.
- Add outbound, inbound, or internal notes from the Follow-up Thread panel.
- Use `Verified` when the number is confirmed.
- Use `Wrong #` when the number is bad; the app marks the lead blocked and suppresses it.
- Run `Test Run` from the Setup panel to create a demo qualified lead with sample manual follow-up notes and a Calendly booking link.
- External systems can append thread notes with `POST /api/thread` using `lead_id`, or a matching `phone`/`email`.

Required qualifier intake:

- Coverage for family or just self
- How soon the plan needs to begin
- Medical only or dental/vision too
- Pre-existing conditions or medications that need covered
- DOB
- Height
- Weight
- Annualized income

The Qualify action captures these fields and writes a qualification summary into the lead's follow-up thread.

Manual outreach mode:

- New compliant leads are queued for personal follow-up.
- Inbound/manual notes through the thread are parsed into the required intake fields.
- The bot prepares the next question as a saved follow-up note.
- When all intake fields are captured, the lead is marked QUALIFIED and the Calendly booking link is prepared for personal follow-up.
- When Calendly posts a booking webhook, the lead is moved to READY and an Owner Alert appears.
- You step in only when the lead is READY/booked.

Lead acquisition setup:

- Connect Reddit, SerpAPI, Google/Meta forms, website forms, permitted directory imports, and partner/referral webhooks.
- Use `POST /run/listeners` in the FastAPI worker stack to run configured Reddit, X, SerpAPI, and Trends listeners.
- Use `POST /api/webhooks/{provider}?secret=your-webhook-secret` for approved source integrations.
- Keep DNC, consent, licensed-state, and source-permission checks in place before personal outreach.

The app does not send automated outreach. Ariel personally calls or messages leads from the lead record.

## FastAPI Worker API

```text
POST /ingest
POST /run/listeners
POST /sync/crm
GET  /qualify/form/{lead_id}
POST /qualify/{lead_id}
```

## Handoff Materials

- `assets/lead_pipeline_flow.png`
- `assets/qualifier_loom_script.pdf`
- `docs/checklist.md`
- `docs/executive-summary.md`
- `docs/postman.json`
- `docs/conversation_archive.txt`

## Notes

The quick local dashboard is self-contained with no install step. The Docker/FastAPI stack expects Docker plus credentials in `.env`.

Compliance notes: keep data minimal, avoid collecting diagnoses or medication names, log consent, honor opt-outs, and connect a live DNC provider before production outreach.

## Verification

Current local checks:

```bash
.venv/bin/python -m py_compile run.py app/*.py app/jobs/*.py app/services/*.py scripts/*.py
.venv/bin/python scripts/smoke_fastapi_stack.py
/Users/arielvahnish/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --check static/app.js
```

Docker was not available in the Codex environment used for this build, so `docker compose up -d --build` still needs to be run on a machine with Docker installed.
