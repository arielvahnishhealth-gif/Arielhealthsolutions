# API Reference

## Create Lead

```http
POST /api/leads
Content-Type: application/json
X-API-Key: qualifier-dev-key
```

```json
{
  "source": "Website Form",
  "name": "Jordan Taylor",
  "phone": "(813) 555-0184",
  "email": "jordan@example.com",
  "state": "FL",
  "county": "Hillsborough",
  "age": 37,
  "household_size": 2,
  "annual_income": 72000,
  "healthy": true,
  "has_current_coverage": true,
  "intent": "Comparing private PPO options and wants a quote this week.",
  "tcpa_consent": true,
  "consent_text": "I agree to be contacted about health insurance options."
}
```

## HealthSherpa Webhook

```http
POST /api/webhooks/healthsherpa?secret=your-webhook-secret
Content-Type: application/json
```

```json
{
  "lead_id": "hs_12345",
  "name": "Jordan Taylor",
  "phone": "(813) 555-0184",
  "email": "jordan@example.com",
  "state": "FL",
  "intent": "HealthSherpa shopper comparing plans",
  "healthsherpa_url": "https://www.healthsherpa.com/"
}
```

## USHealth Group Webhook

```http
POST /api/webhooks/ushealthgroup?secret=your-webhook-secret
Content-Type: application/json
```

```json
{
  "lead_id": "ushg_12345",
  "name": "Jordan Taylor",
  "phone": "(813) 555-0184",
  "email": "jordan@example.com",
  "state": "FL",
  "intent": "Looking for USHealth Group plans",
  "ushealthgroup_url": "https://www.ushealthgroup.com/"
}
```

## Warm Intent Webhooks

These routes use the same payload shape as other lead webhooks:

```http
POST /api/webhooks/marketplace?secret=your-webhook-secret
POST /api/webhooks/cobraprospect?secret=your-webhook-secret
POST /api/webhooks/layoffsignal?secret=your-webhook-secret
POST /api/webhooks/publicsocial?secret=your-webhook-secret
POST /api/webhooks/referralpartner?secret=your-webhook-secret
POST /api/webhooks/retargeting?secret=your-webhook-secret
```

```json
{
  "lead_id": "warm_12345",
  "name": "Jordan Taylor",
  "phone": "(813) 555-0184",
  "email": "jordan@example.com",
  "state": "FL",
  "intent": "Lost employer coverage and comparing COBRA alternatives",
  "signal_url": "https://example.com/source",
  "tcpa_consent": true,
  "consent_text": "I agree to be contacted about health insurance options."
}
```

## Yellow Pages Webhook

Use this for permitted business-directory records, not bulk scraping.

```http
POST /api/webhooks/yellowpages?secret=your-webhook-secret
Content-Type: application/json
```

```json
{
  "lead_id": "yp_12345",
  "name": "Taylor Family Contracting",
  "phone": "(813) 555-0184",
  "email": "office@example.com",
  "state": "FL",
  "intent": "Small business health insurance review",
  "yellowpages_url": "https://www.yellowpages.com/example"
}
```

## Qualifier Flow

```http
POST /api/leads/{id}/assign
POST /api/leads/{id}/qualify
POST /api/leads/{id}/stage/ready
```

`READY` requires:

```json
{
  "appointment_at": "2026-06-19T14:00:00-04:00",
  "intent_confirmed": true
}
```

## Principal Result

```http
POST /api/leads/{id}/stage/result
X-API-Key: principal-dev-key
```

```json
{
  "result": "WON"
}
```

## Exports

```http
GET /leads.csv
GET /leads/download.zip
GET /metrics/prom
```
