# Implementation Checklist
- Configure .env with database, Redis, Calendly, optional HubSpot, and lead-source API keys.
- Run: docker compose up -d --build
- Connect Reddit/SerpAPI, Meta, Google webhooks.
- Qualifier workflow: assign -> qualify form -> stage ready.
- Principal workflow: receive READY -> mark WON/LOST.
