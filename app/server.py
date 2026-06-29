from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urlparse, urlunparse

from cryptography.fernet import Fernet, InvalidToken


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
ASSETS_DIR = ROOT / "assets"
DB_PATH = Path(os.getenv("LEAD_BOT_DB", DATA_DIR / "lead_bot.sqlite3"))
DEFAULT_PORT = int(os.getenv("PORT", "8080"))
AUTH_COOKIE = "leadbot_session"
SESSION_SECONDS = int(os.getenv("LEAD_BOT_SESSION_SECONDS", str(60 * 60 * 12)))
AUTOPILOT_INTERVAL_SECONDS = int(os.getenv("LEAD_BOT_AUTOPILOT_INTERVAL_SECONDS", "60"))
_BACKGROUND_AUTOPILOT_STARTED = False

STAGES = ("NEW", "TRIAGE", "QUALIFIED", "READY", "WON", "LOST", "SUPPRESSED")
REQUIRED_READY_FIELDS = ("appointment_at", "intent_confirmed")
QUALIFIER_STEPS = [
    ("coverage_for", "Ok, were you looking for coverage for the family or just yourself?"),
    ("plan_start_timing", "Ok, thanks for that info. How soon did you need a plan to begin?"),
    ("needs_dental_vision", "Ok, and were you looking for just a medical plan or did you need dental and vision as well?"),
    ("conditions_meds", "Also are there any pre-existing conditions or medications that you need covered?"),
    ("dob_height_weight_income", "Ok, I can help. Provide me with the DOB, height, weight & annualized income? I'll try to get you some numbers..."),
]
LICENSED_STATES = {
    "TX", "NM", "UT", "CO", "SD", "NE", "KS", "MO", "AR", "LA", "MS",
    "AL", "GA", "FL", "SC", "NC", "VA", "WV", "PA", "OH", "IN",
    "KY", "TN", "IL", "IA", "MI", "WI", "OK", "MD", "DE", "DC",
}
DEFAULT_SETTINGS = {
    "business_name": "Health Insurance Lead Bot",
    "licensed_states": ",".join(sorted(LICENSED_STATES)),
    "hubspot_access_token": "",
    "calendar_booking_url": "",
    "calendly_api_token": "",
    "calendly_sync_days": "60",
    "principal_email": "principal@example.com",
    "email_template": "Hi {name},\n\nYou requested health insurance options. Your assigned desk is {owner}.\n\nReply STOP to opt out.",
    "qualifier_sla_minutes": "15",
    "auto_assign_owner": "Qualifier Team",
    "ai_model": "gpt-4.1-mini",
    "qualifier_followup_hours": "2",
    "qualifier_max_followups": "2",
    "booking_followup_hours": "24",
    "booking_max_followups": "2",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Request body must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Request JSON must be an object.")
    return value


def normalize_phone(phone: str | None) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D+", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def parse_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        digits = re.sub(r"[^\d-]+", "", str(value))
        try:
            return int(digits) if digits else default
        except ValueError:
            return default


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def owner_username() -> str:
    return os.getenv("LEAD_BOT_USERNAME", "owner")


def owner_password() -> str:
    return os.getenv("LEAD_BOT_PASSWORD") or os.getenv("PRINCIPAL_API_KEY", "principal-dev-key")


def webhook_secret() -> str:
    return os.getenv("LEAD_BOT_WEBHOOK_SECRET", os.getenv("QUALIFIER_API_KEY", "qualifier-dev-key"))


def admin_allowed_ips() -> set[str]:
    raw = os.getenv("LEAD_BOT_ADMIN_IPS", "")
    configured = {part.strip() for part in raw.split(",") if part.strip()}
    return configured or {"127.0.0.1", "::1", "localhost"}


def openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def verify_owner(username: str, password: str) -> bool:
    return hmac.compare_digest(username, owner_username()) and hmac.compare_digest(password, owner_password())


def get_config() -> dict[str, Any]:
    settings = load_settings()
    if os.getenv("LICENSED_STATES"):
        settings["licensed_states"] = os.environ["LICENSED_STATES"]
    if os.getenv("CALENDAR_BOOKING_URL"):
        settings["calendar_booking_url"] = os.environ["CALENDAR_BOOKING_URL"]
    if os.getenv("CALENDLY_API_TOKEN"):
        settings["calendly_api_token"] = os.environ["CALENDLY_API_TOKEN"]
    if os.getenv("CALENDLY_SYNC_DAYS"):
        settings["calendly_sync_days"] = os.environ["CALENDLY_SYNC_DAYS"]
    if os.getenv("HUBSPOT_ACCESS_TOKEN"):
        settings["hubspot_access_token"] = os.environ["HUBSPOT_ACCESS_TOKEN"]
    for env_key, setting_key in (
        ("QUALIFIER_FOLLOWUP_HOURS", "qualifier_followup_hours"),
        ("QUALIFIER_MAX_FOLLOWUPS", "qualifier_max_followups"),
        ("BOOKING_FOLLOWUP_HOURS", "booking_followup_hours"),
        ("BOOKING_MAX_FOLLOWUPS", "booking_max_followups"),
    ):
        if os.getenv(env_key):
            settings[setting_key] = os.environ[env_key]
    return {
        "qualifier_api_key": os.getenv("QUALIFIER_API_KEY", "qualifier-dev-key"),
        "principal_api_key": os.getenv("PRINCIPAL_API_KEY", "principal-dev-key"),
        "hubspot_api_key": os.getenv("HUBSPOT_API_KEY") or settings.get("hubspot_access_token", ""),
        "principal_email": os.getenv("PRINCIPAL_EMAIL") or settings.get("principal_email", "principal@example.com"),
        "calendar_timezone": os.getenv("CALENDAR_TIMEZONE", "America/New_York"),
        "state_owner_map": parse_owner_map(os.getenv("STATE_OWNER_MAP", "")),
        "settings": settings,
    }


def load_settings() -> dict[str, str]:
    try:
        with open_db() as conn:
            ensure_settings_table(conn)
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            data = {row["key"]: row["value"] for row in rows}
    except sqlite3.Error:
        data = {}
    merged = DEFAULT_SETTINGS.copy()
    merged.update({key: value for key, value in data.items() if value is not None})
    return merged


def ensure_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def save_settings(conn: sqlite3.Connection, payload: dict[str, Any], actor: str) -> dict[str, str]:
    ensure_settings_table(conn)
    allowed = set(DEFAULT_SETTINGS)
    saved: dict[str, str] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        text = str(value or "").strip()
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, text, utcnow()),
        )
        saved[key] = text
    record_audit(conn, "settings.updated", {"keys": sorted(saved)}, None, actor)
    return {**load_settings(), **saved}


def licensed_states() -> set[str]:
    raw = os.getenv("LICENSED_STATES") or load_settings().get("licensed_states", "")
    values = {part.strip().upper() for part in raw.split(",") if part.strip()}
    return values or LICENSED_STATES


def parse_owner_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        state, owner = part.split(":", 1)
        state = state.strip().upper()
        owner = owner.strip()
        if state and owner:
            mapping[state] = owner
    return mapping


def calendly_link_problem(url: str) -> str:
    if not url:
        return "Add your public Calendly booking URL."
    parsed = urlparse(url)
    if "/app/" in url:
        return "Replace the Calendly admin URL with the public booking URL prospects can open."
    host = parsed.netloc.lower()
    if host.endswith("calendly.com"):
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 2:
            return "Use a specific Calendly event link, such as https://calendly.com/your-name/your-event."
    return ""


def load_fernet() -> Fernet:
    key = os.getenv("FERNET_KEY", "").strip()
    if key:
        return Fernet(key.encode("utf-8"))
    secret = os.getenv("APP_SECRET", "local-development-lead-bot-secret").encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


FERNET = load_fernet()


def encrypt_text(value: str | None) -> str:
    if not value:
        return ""
    return FERNET.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return FERNET.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return "[encrypted]"


def open_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with open_db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS leads (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL,
                name TEXT NOT NULL,
                phone_enc TEXT NOT NULL,
                email_enc TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                email_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                county TEXT NOT NULL,
                age INTEGER,
                household_size INTEGER,
                annual_income INTEGER,
                healthy INTEGER NOT NULL DEFAULT 0,
                has_current_coverage INTEGER NOT NULL DEFAULT 0,
                intent TEXT NOT NULL,
                consent_text TEXT NOT NULL,
                tcpa_consent INTEGER NOT NULL DEFAULT 0,
                dnc_checked INTEGER NOT NULL DEFAULT 0,
                dnc_suppressed INTEGER NOT NULL DEFAULT 0,
                segment TEXT NOT NULL,
                score INTEGER NOT NULL,
                stage TEXT NOT NULL,
                owner TEXT NOT NULL,
                assigned_to TEXT NOT NULL,
                appointment_at TEXT NOT NULL,
                intent_confirmed INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL,
                lost_reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dnc_entries (
                hash TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value_hint TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consent_logs (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                consent_text TEXT NOT NULL,
                ip TEXT NOT NULL,
                user_agent TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                lead_id TEXT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_logs (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                response TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS webhook_logs (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                lead_id TEXT,
                payload TEXT NOT NULL,
                response TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automation_logs (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                job TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                direction TEXT NOT NULL,
                sender TEXT NOT NULL,
                body TEXT NOT NULL,
                channel TEXT NOT NULL,
                external_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS owner_notifications (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                read_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ip TEXT NOT NULL,
                user_agent TEXT NOT NULL
            );
            """
        )
        ensure_settings_table(conn)
        migrate_columns(conn)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, utcnow()),
            )
        count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        if count == 0:
            seed_demo_data(conn)


def migrate_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    columns = {
        "source_url": "TEXT NOT NULL DEFAULT ''",
        "contact_status": "TEXT NOT NULL DEFAULT 'CONTACTABLE'",
        "compliance_status": "TEXT NOT NULL DEFAULT 'PENDING'",
        "last_contacted_at": "TEXT NOT NULL DEFAULT ''",
        "external_id": "TEXT NOT NULL DEFAULT ''",
        "phone_status": "TEXT NOT NULL DEFAULT 'UNVERIFIED'",
        "coverage_for": "TEXT NOT NULL DEFAULT ''",
        "plan_start_timing": "TEXT NOT NULL DEFAULT ''",
        "needs_dental_vision": "TEXT NOT NULL DEFAULT ''",
        "conditions_meds": "TEXT NOT NULL DEFAULT ''",
        "dob": "TEXT NOT NULL DEFAULT ''",
        "height": "TEXT NOT NULL DEFAULT ''",
        "weight": "TEXT NOT NULL DEFAULT ''",
        "annualized_income_text": "TEXT NOT NULL DEFAULT ''",
        "auto_qualifier_enabled": "INTEGER NOT NULL DEFAULT 1",
        "qualification_step": "TEXT NOT NULL DEFAULT ''",
        "booking_link_sent_at": "TEXT NOT NULL DEFAULT ''",
        "ready_notified_at": "TEXT NOT NULL DEFAULT ''",
        "qualifier_followup_count": "INTEGER NOT NULL DEFAULT 0",
        "last_qualifier_followup_at": "TEXT NOT NULL DEFAULT ''",
        "booking_followup_count": "INTEGER NOT NULL DEFAULT 0",
        "last_booking_followup_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {definition}")


def create_session(conn: sqlite3.Connection, username: str, ip: str, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires = now + timedelta(seconds=SESSION_SECONDS)
    conn.execute(
        "INSERT INTO sessions (token_hash, username, created_at, expires_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
        (token_hash(token), username, now.isoformat(), expires.isoformat(), ip, user_agent[:250]),
    )
    record_audit(conn, "auth.login", {"username": username, "ip": ip}, None, username)
    return token


def destroy_session(conn: sqlite3.Connection, token: str, actor: str) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        record_audit(conn, "auth.logout", {"actor": actor}, None, actor)


def session_user(conn: sqlite3.Connection, token: str | None) -> str | None:
    if not token:
        return None
    row = conn.execute("SELECT username, expires_at FROM sessions WHERE token_hash = ?", (token_hash(token),)).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None
    if expires < datetime.now(timezone.utc):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        return None
    return row["username"]


def seed_demo_data(conn: sqlite3.Connection) -> None:
    samples = [
        {
            "source": "Meta Lead Ad",
            "name": "Marissa Chen",
            "phone": "(305) 555-0192",
            "email": "marissa@example.com",
            "state": "FL",
            "county": "Miami-Dade",
            "age": 34,
            "household_size": 3,
            "annual_income": 46000,
            "healthy": True,
            "has_current_coverage": False,
            "intent": "Needs ACA plan before next month",
            "tcpa_consent": True,
            "consent_text": "I agree to be contacted about health insurance options.",
        },
        {
            "source": "Google Search",
            "name": "Devon Price",
            "phone": "(512) 555-0131",
            "email": "devon@example.com",
            "state": "TX",
            "county": "Travis",
            "age": 42,
            "household_size": 1,
            "annual_income": 94000,
            "healthy": True,
            "has_current_coverage": True,
            "intent": "Comparing private PPO options",
            "tcpa_consent": True,
            "consent_text": "Please contact me about private health insurance.",
        },
        {
            "source": "Website Form",
            "name": "Nina Alvarez",
            "phone": "(404) 555-0147",
            "email": "nina@example.com",
            "state": "GA",
            "county": "Fulton",
            "age": 29,
            "household_size": 2,
            "annual_income": 38000,
            "healthy": False,
            "has_current_coverage": False,
            "intent": "Lost Medicaid and needs coverage urgently",
            "tcpa_consent": True,
            "consent_text": "I consent to calls and texts about health coverage.",
        },
    ]
    for sample in samples:
        create_lead(conn, sample, actor="system", ip="local", user_agent="seed", deliver=False)


def record_audit(
    conn: sqlite3.Connection,
    action: str,
    detail: dict[str, Any] | str,
    lead_id: str | None = None,
    actor: str = "system",
) -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, sort_keys=True)
    conn.execute(
        "INSERT INTO audit_logs (id, lead_id, created_at, actor, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), lead_id, utcnow(), actor, action, detail),
    )


def check_dnc(conn: sqlite3.Connection, phone_hash: str, email_hash: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM dnc_entries WHERE hash IN (?, ?) LIMIT 1", (phone_hash, email_hash)
    ).fetchall()
    return bool(rows)


def compliance_check(lead: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if lead.get("dnc_suppressed"):
        reasons.append("DNC suppressed")
    if lead.get("contact_status") == "OPTED_OUT":
        reasons.append("Opted out")
    if not lead.get("tcpa_consent"):
        reasons.append("Missing TCPA consent")
    if not str(lead.get("consent_text") or "").strip():
        reasons.append("Missing consent text")
    state = str(lead.get("state") or "").upper()
    if state and state not in licensed_states():
        reasons.append(f"Unlicensed state: {state}")
    if not state:
        reasons.append("Missing state")
    return ("BLOCKED" if reasons else "CONTACTABLE", reasons)


def refresh_compliance(conn: sqlite3.Connection, lead_id: str) -> dict[str, Any]:
    lead = get_lead(conn, lead_id, reveal=True)
    status, reasons = compliance_check(lead)
    updates = {
        "compliance_status": status,
        "contact_status": "BLOCKED" if status == "BLOCKED" and lead.get("contact_status") != "OPTED_OUT" else lead.get("contact_status", "CONTACTABLE"),
        "updated_at": utcnow(),
    }
    apply_updates(conn, lead_id, updates)
    if reasons:
        record_audit(conn, "compliance.blocked", {"reasons": reasons}, lead_id)
    else:
        record_audit(conn, "compliance.cleared", {"status": status}, lead_id)
    return get_lead(conn, lead_id, reveal=True)


def estimate_segment(payload: dict[str, Any]) -> str:
    income = parse_int(payload.get("annual_income"))
    household = max(parse_int(payload.get("household_size"), 1), 1)
    healthy = bool(payload.get("healthy"))
    has_coverage = bool(payload.get("has_current_coverage"))
    per_person = income / household
    if healthy and income >= 65000 and (has_coverage or per_person >= 45000):
        return "Private-Prime"
    return "ACA-Subsidy"


def score_lead(payload: dict[str, Any], segment: str, suppressed: bool) -> int:
    score = 35
    intent = str(payload.get("intent") or "").lower()
    source = str(payload.get("source") or "").lower()
    income = parse_int(payload.get("annual_income"))
    age = parse_int(payload.get("age"))
    if suppressed:
        return 0
    if any(word in intent for word in ("urgent", "lost", "needs", "quote", "compare", "before")):
        score += 20
    if any(
        word in source
        for word in (
            "cobraprospect",
            "google",
            "healthsherpa",
            "jobloss",
            "layoff",
            "marketplace",
            "publicsocial",
            "referral",
            "retargeting",
            "ushealth",
            "website",
            "yellowpages",
        )
    ):
        score += 10
    if payload.get("tcpa_consent"):
        score += 15
    if payload.get("healthy"):
        score += 8
    if segment == "Private-Prime":
        score += 12 if income >= 80000 else 6
    else:
        score += 12 if income and income <= 60000 else 5
    if 26 <= age <= 62:
        score += 6
    if any(word in intent for word in ("spam", "job", "free money")):
        score -= 30
    return max(0, min(score, 100))


def route_owner(state: str, segment: str) -> str:
    state = state.upper()
    mapping = get_config()["state_owner_map"]
    if state in mapping:
        return mapping[state]
    auto_owner = get_config()["settings"].get("auto_assign_owner", "").strip()
    if auto_owner:
        return auto_owner
    if segment == "Private-Prime":
        return "Private Desk"
    return "ACA Desk"


def row_to_lead(row: sqlite3.Row, reveal: bool = False) -> dict[str, Any]:
    lead = dict(row)
    lead["phone"] = decrypt_text(lead.pop("phone_enc")) if reveal else mask_hash(lead["phone_hash"], "phone")
    lead["email"] = decrypt_text(lead.pop("email_enc")) if reveal else mask_hash(lead["email_hash"], "email")
    for key in ("healthy", "has_current_coverage", "tcpa_consent", "dnc_checked", "dnc_suppressed", "intent_confirmed"):
        lead[key] = bool(lead[key])
    return lead


def mask_hash(value: str, kind: str) -> str:
    if not value:
        return ""
    suffix = value[-6:]
    return f"{kind}:...{suffix}"


def create_lead(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    actor: str,
    ip: str,
    user_agent: str,
    deliver: bool = True,
) -> dict[str, Any]:
    phone = normalize_phone(payload.get("phone"))
    email = normalize_email(payload.get("email"))
    if not phone and not email:
        raise ValueError("A phone or email is required.")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Lead name is required.")
    phone_hash = stable_hash(phone)
    email_hash = stable_hash(email)
    suppressed = check_dnc(conn, phone_hash, email_hash)
    segment = estimate_segment(payload)
    score = score_lead(payload, segment, suppressed)
    stage = "SUPPRESSED" if suppressed else "NEW"
    lead_id = str(uuid.uuid4())
    now = utcnow()
    values = {
        "id": lead_id,
        "created_at": now,
        "updated_at": now,
        "source": str(payload.get("source") or "Manual"),
        "name": name,
        "phone_enc": encrypt_text(phone),
        "email_enc": encrypt_text(email),
        "phone_hash": phone_hash,
        "email_hash": email_hash,
        "state": str(payload.get("state") or "").strip().upper(),
        "county": str(payload.get("county") or "").strip(),
        "age": parse_int(payload.get("age")),
        "household_size": parse_int(payload.get("household_size"), 1),
        "annual_income": parse_int(payload.get("annual_income")),
        "healthy": int(bool(payload.get("healthy"))),
        "has_current_coverage": int(bool(payload.get("has_current_coverage"))),
        "intent": str(payload.get("intent") or "").strip(),
        "consent_text": str(payload.get("consent_text") or "").strip(),
        "tcpa_consent": int(bool(payload.get("tcpa_consent"))),
        "dnc_checked": 1,
        "dnc_suppressed": int(suppressed),
        "segment": segment,
        "score": score,
        "stage": stage,
        "owner": route_owner(str(payload.get("state") or ""), segment),
        "assigned_to": "",
        "appointment_at": "",
        "intent_confirmed": 0,
        "notes": str(payload.get("notes") or "").strip(),
        "lost_reason": "",
    }
    conn.execute(
        """
        INSERT INTO leads (
            id, created_at, updated_at, source, name, phone_enc, email_enc, phone_hash, email_hash,
            state, county, age, household_size, annual_income, healthy, has_current_coverage,
            intent, consent_text, tcpa_consent, dnc_checked, dnc_suppressed, segment, score,
            stage, owner, assigned_to, appointment_at, intent_confirmed, notes, lost_reason
        ) VALUES (
            :id, :created_at, :updated_at, :source, :name, :phone_enc, :email_enc, :phone_hash, :email_hash,
            :state, :county, :age, :household_size, :annual_income, :healthy, :has_current_coverage,
            :intent, :consent_text, :tcpa_consent, :dnc_checked, :dnc_suppressed, :segment, :score,
            :stage, :owner, :assigned_to, :appointment_at, :intent_confirmed, :notes, :lost_reason
        )
        """,
        values,
    )
    apply_updates(
        conn,
        lead_id,
        {
            "source_url": str(payload.get("source_url") or payload.get("url") or "").strip(),
            "external_id": str(payload.get("external_id") or "").strip(),
            "updated_at": now,
        },
    )
    if values["tcpa_consent"]:
        conn.execute(
            "INSERT INTO consent_logs (id, lead_id, created_at, consent_text, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), lead_id, now, values["consent_text"], ip, user_agent),
        )
    record_audit(conn, "lead.created", {"source": values["source"], "stage": stage, "score": score}, lead_id, actor)
    lead = refresh_compliance(conn, lead_id)
    if stage == "NEW" and lead.get("compliance_status") != "BLOCKED":
        run_auto_qualifier_for_lead(conn, lead_id)
    if deliver and stage == "READY":
        deliver_ready_lead(conn, lead_id)
    return lead


def get_lead(conn: sqlite3.Connection, lead_id: str, reveal: bool = False) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        raise KeyError("Lead not found.")
    return row_to_lead(row, reveal=reveal)


def list_leads(conn: sqlite3.Connection, stage: str | None = None, reveal: bool = False) -> list[dict[str, Any]]:
    if stage:
        rows = conn.execute("SELECT * FROM leads WHERE stage = ? ORDER BY created_at DESC", (stage.upper(),)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()
    return [row_to_lead(row, reveal=reveal) for row in rows]


def conversation_rows(conn: sqlite3.Connection, lead_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM conversation_messages WHERE lead_id = ? ORDER BY created_at ASC",
        (lead_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def add_conversation_message(
    conn: sqlite3.Connection,
    lead_id: str,
    payload: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    get_lead(conn, lead_id, reveal=False)
    direction = str(payload.get("direction") or "outbound").lower()
    if direction not in ("inbound", "outbound", "internal"):
        raise ValueError("direction must be inbound, outbound, or internal.")
    body = str(payload.get("body") or payload.get("message") or "").strip()
    if not body:
        raise ValueError("message body is required.")
    message = {
        "id": str(uuid.uuid4()),
        "lead_id": lead_id,
        "created_at": str(payload.get("created_at") or utcnow()),
        "direction": direction,
        "sender": str(payload.get("sender") or actor),
        "body": body,
        "channel": str(payload.get("channel") or "manual"),
        "external_id": str(payload.get("external_id") or ""),
    }
    conn.execute(
        """
        INSERT INTO conversation_messages
            (id, lead_id, created_at, direction, sender, body, channel, external_id)
        VALUES
            (:id, :lead_id, :created_at, :direction, :sender, :body, :channel, :external_id)
        """,
        message,
    )
    if direction in ("inbound", "outbound"):
        apply_updates(conn, lead_id, {"last_contacted_at": message["created_at"], "updated_at": utcnow()})
    record_audit(conn, "conversation.message_added", {"direction": direction, "channel": message["channel"]}, lead_id, actor)
    return message


def next_missing_qualification_step(lead: dict[str, Any]) -> str:
    for key, _question in QUALIFIER_STEPS:
        if key == "dob_height_weight_income":
            if not (lead.get("dob") and lead.get("height") and lead.get("weight") and lead.get("annualized_income_text")):
                return key
        elif not str(lead.get(key) or "").strip():
            return key
    return "complete"


def question_for_step(step: str) -> str:
    for key, question in QUALIFIER_STEPS:
        if key == step:
            return question
    return ""


def parse_dob_height_weight_income(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    dob_match = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b", text)
    if dob_match:
        result["dob"] = dob_match.group(1)
    height_match = re.search(
        r"(\d)\s*['’]\s*(\d{1,2})|"
        r"(\d)\s*(?:ft|feet|foot)\s*(\d{1,2})?\s*(?:in|inches)?|"
        r"(\d{2,3})\s*(?:in|inches)",
        text,
        re.I,
    )
    if height_match:
        if height_match.group(1):
            result["height"] = f"{height_match.group(1)}'{height_match.group(2)}"
        elif height_match.group(3):
            inches = height_match.group(4) or "0"
            result["height"] = f"{height_match.group(3)}'{inches}"
        else:
            result["height"] = f"{height_match.group(5)} in"
    weight_match = re.search(r"\b(?:weight\s*(?:is|:)?\s*)?(\d{2,3})\s*(?:lb|lbs|pounds)\b", text, re.I)
    if weight_match:
        result["weight"] = f"{weight_match.group(1)} lb"
    income_match = re.search(
        r"(?:annual(?:ized)?\s*)?income\s*(?:is|:)?\s*\$?\s*(\d{2,3}(?:,\d{3})+|\d{5,6}|\d{2,3}\s*k)\b|"
        r"\$\s*(\d{2,3}(?:,\d{3})+|\d{5,6}|\d{2,3}\s*k)\b",
        text,
        re.I,
    )
    if income_match:
        income = (income_match.group(1) or income_match.group(2) or "").replace(" ", "")
        if income.lower().endswith("k"):
            income = str(int(income[:-1].replace(",", "")) * 1000)
        result["annualized_income_text"] = "$" + income.replace(",", ",")
    return result


def capture_qualifier_answer(conn: sqlite3.Connection, lead_id: str, text: str, actor: str) -> dict[str, Any]:
    lead = get_lead(conn, lead_id, reveal=True)
    step = lead.get("qualification_step") or next_missing_qualification_step(lead)
    updates: dict[str, Any] = {"updated_at": utcnow()}
    clean = text.strip()
    if step == "coverage_for":
        lower = clean.lower()
        updates["coverage_for"] = "Family" if any(word in lower for word in ("family", "wife", "husband", "spouse", "kids", "children")) else "Just myself"
    elif step == "plan_start_timing":
        updates["plan_start_timing"] = clean
    elif step == "needs_dental_vision":
        lower = clean.lower()
        if "dental" in lower and "vision" in lower:
            updates["needs_dental_vision"] = "Medical + dental + vision"
        elif "dental" in lower:
            updates["needs_dental_vision"] = "Medical + dental"
        elif "vision" in lower:
            updates["needs_dental_vision"] = "Medical + vision"
        else:
            updates["needs_dental_vision"] = "Medical only"
    elif step == "conditions_meds":
        updates["conditions_meds"] = clean
    elif step == "dob_height_weight_income":
        parsed = parse_dob_height_weight_income(clean)
        updates.update(parsed)
        if "dob" not in updates:
            updates["dob"] = clean
    apply_updates(conn, lead_id, updates)
    updated = get_lead(conn, lead_id, reveal=True)
    next_step = next_missing_qualification_step(updated)
    apply_updates(conn, lead_id, {"qualification_step": next_step, "updated_at": utcnow()})
    record_audit(conn, "auto_qualifier.answer_captured", {"step": step, "next_step": next_step}, lead_id, actor)
    return get_lead(conn, lead_id, reveal=True)


def append_auto_outbound(conn: sqlite3.Connection, lead_id: str, body: str, actor: str = "auto-qualifier") -> dict[str, Any]:
    message = add_conversation_message(
        conn,
        lead_id,
        {"direction": "outbound", "sender": "Manual Follow-up", "body": body, "channel": "manual"},
        actor,
    )
    conn.execute(
        "INSERT INTO delivery_logs (id, lead_id, created_at, target, status, response) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), lead_id, utcnow(), "manual-outreach", "ready", "Saved for personal follow-up by Ariel."),
    )
    return message


def run_auto_qualifier_for_lead(conn: sqlite3.Connection, lead_id: str, actor: str = "auto-qualifier") -> dict[str, Any]:
    lead = get_lead(conn, lead_id, reveal=True)
    if not lead.get("auto_qualifier_enabled"):
        return {"status": "disabled", "lead": lead}
    if lead.get("stage") in ("READY", "WON", "LOST", "SUPPRESSED"):
        return {"status": "ignored", "reason": f"stage {lead.get('stage')}", "lead": lead}
    if lead.get("compliance_status") == "BLOCKED" or lead.get("contact_status") in ("BLOCKED", "OPTED_OUT"):
        return {"status": "blocked", "lead": lead}
    step = next_missing_qualification_step(lead)
    apply_updates(conn, lead_id, {"qualification_step": step, "updated_at": utcnow()})
    if step == "complete":
        if lead.get("stage") in ("NEW", "TRIAGE"):
            qualify_lead(conn, lead_id, {"notes": "Auto qualifier completed required intake.", "intent_confirmed": True}, actor)
        lead = get_lead(conn, lead_id, reveal=True)
        if not lead.get("booking_link_sent_at"):
            try:
                send_booking_link(conn, lead_id, actor)
            except ValueError as exc:
                create_owner_notification(
                    conn,
                    lead_id,
                    "SETUP",
                    "Booking link not sent",
                    f"{lead.get('name')} qualified, but the booking link could not be sent: {exc}",
                )
                record_audit(conn, "auto_qualifier.booking_not_sent", {"reason": str(exc)}, lead_id, actor)
                return {"status": "booking_not_sent", "reason": str(exc), "lead": get_lead(conn, lead_id, reveal=True)}
        return {"status": "booking_sent", "lead": get_lead(conn, lead_id, reveal=True)}
    previous_questions = [
        message for message in conversation_rows(conn, lead_id)
        if message["direction"] == "outbound" and question_for_step(step) in message["body"]
    ]
    if not previous_questions:
        append_auto_outbound(conn, lead_id, question_for_step(step), actor)
        return {"status": "question_sent", "step": step, "lead": get_lead(conn, lead_id, reveal=True)}
    return {"status": "waiting_for_reply", "step": step, "lead": lead}


def run_auto_qualifier(conn: sqlite3.Connection, actor: str = "auto-qualifier") -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id FROM leads
        WHERE auto_qualifier_enabled = 1
          AND stage IN ('NEW', 'TRIAGE', 'QUALIFIED')
          AND compliance_status != 'BLOCKED'
          AND contact_status NOT IN ('BLOCKED', 'OPTED_OUT')
        ORDER BY created_at ASC
        LIMIT 50
        """
    ).fetchall()
    results = [run_auto_qualifier_for_lead(conn, row["id"], actor) for row in rows]
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), "auto_qualifier", "complete", json.dumps({"processed": len(results)})),
    )
    return {"processed": len(results), "results": results}


def send_booking_link(conn: sqlite3.Connection, lead_id: str, actor: str = "dashboard") -> dict[str, Any]:
    lead = get_lead(conn, lead_id, reveal=True)
    if lead.get("compliance_status") == "BLOCKED" or lead.get("contact_status") in ("BLOCKED", "OPTED_OUT"):
        raise ValueError("Lead is blocked or opted out; booking link was not sent.")
    booking = calendly_booking_url(lead)
    if not booking:
        raise ValueError("Booking URL is not configured.")
    message = append_auto_outbound(
        conn,
        lead_id,
        "You're qualified. Please book a time here so Ariel can go over plans with you: " + booking,
        actor,
    )
    apply_updates(conn, lead_id, {"booking_link_sent_at": utcnow(), "updated_at": utcnow()})
    record_audit(conn, "booking.link_prepared", {"channel": "manual", "manual": True}, lead_id, actor)
    return {"lead": get_lead(conn, lead_id, reveal=True), "message": message}


def create_owner_notification(conn: sqlite3.Connection, lead_id: str, kind: str, title: str, body: str) -> dict[str, Any]:
    notification = {
        "id": str(uuid.uuid4()),
        "lead_id": lead_id,
        "created_at": utcnow(),
        "kind": kind,
        "title": title,
        "body": body,
    }
    conn.execute(
        "INSERT INTO owner_notifications (id, lead_id, created_at, kind, title, body) VALUES (:id, :lead_id, :created_at, :kind, :title, :body)",
        notification,
    )
    return notification


def create_owner_notification_once(conn: sqlite3.Connection, lead_id: str, kind: str, title: str, body: str) -> dict[str, Any]:
    existing = conn.execute(
        """
        SELECT * FROM owner_notifications
        WHERE lead_id = ? AND kind = ? AND title = ? AND body = ? AND read_at = ''
        LIMIT 1
        """,
        (lead_id, kind, title, body),
    ).fetchone()
    if existing:
        return dict(existing)
    return create_owner_notification(conn, lead_id, kind, title, body)


def owner_notifications(conn: sqlite3.Connection, unread_only: bool = False) -> list[dict[str, Any]]:
    if unread_only:
        rows = conn.execute("SELECT * FROM owner_notifications WHERE read_at = '' ORDER BY created_at DESC LIMIT 50").fetchall()
    else:
        rows = conn.execute("SELECT * FROM owner_notifications ORDER BY created_at DESC LIMIT 50").fetchall()
    return [dict(row) for row in rows]


def notify_owner_ready(conn: sqlite3.Connection, lead_id: str, reason: str) -> None:
    lead = get_lead(conn, lead_id, reveal=True)
    if lead.get("ready_notified_at"):
        return
    body = (
        f"{lead['name']} booked an appointment for {lead.get('appointment_at')}. "
        f"Segment: {lead.get('segment')}. Score: {lead.get('score')}. Reason: {reason}."
    )
    create_owner_notification(conn, lead_id, "READY", "Lead ready for Ariel", body)
    apply_updates(conn, lead_id, {"ready_notified_at": utcnow(), "updated_at": utcnow()})
    record_audit(conn, "owner.ready_notified", {"reason": reason}, lead_id, "system")


def set_phone_status(conn: sqlite3.Connection, lead_id: str, status: str, actor: str) -> dict[str, Any]:
    status = status.upper().replace(" ", "_")
    allowed = {"UNVERIFIED", "VERIFIED", "WRONG_NUMBER", "NEEDS_REVIEW"}
    if status not in allowed:
        raise ValueError(f"phone status must be one of: {', '.join(sorted(allowed))}")
    updates = {"phone_status": status, "updated_at": utcnow()}
    if status == "WRONG_NUMBER":
        updates["contact_status"] = "BLOCKED"
        updates["compliance_status"] = "BLOCKED"
        updates["stage"] = "SUPPRESSED"
    apply_updates(conn, lead_id, updates)
    record_audit(conn, "lead.phone_status_changed", {"phone_status": status}, lead_id, actor)
    if status == "WRONG_NUMBER":
        add_conversation_message(
            conn,
            lead_id,
            {"direction": "internal", "body": "Marked wrong number. Lead suppressed until contact info is corrected.", "channel": "note"},
            actor,
        )
    return get_lead(conn, lead_id, reveal=True)


def find_lead_by_phone_or_email(conn: sqlite3.Connection, phone: str = "", email: str = "") -> str | None:
    phone_hash = stable_hash(normalize_phone(phone))
    email_hash = stable_hash(normalize_email(email))
    row = conn.execute(
        "SELECT id FROM leads WHERE (? != '' AND phone_hash = ?) OR (? != '' AND email_hash = ?) ORDER BY created_at DESC LIMIT 1",
        (phone_hash, phone_hash, email_hash, email_hash),
    ).fetchone()
    return row["id"] if row else None


def parse_request_payload(handler: "LeadBotHandler") -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length)
    content_type = handler.headers.get("Content-Type", "")
    if "application/x-www-form-urlencoded" in content_type:
        parsed = parse_qs(raw.decode("utf-8"))
        return {key: values[0] if values else "" for key, values in parsed.items()}
    return parse_json(raw)


def run_test_lead_flow(conn: sqlite3.Connection, actor: str) -> dict[str, Any]:
    suffix = str(int(time.time()))
    lead = create_lead(
        conn,
        {
            "source": "Test Run",
            "name": "Test Qualified Lead",
            "phone": f"813555{suffix[-4:]}",
            "email": f"test-lead-{suffix}@example.com",
            "state": "FL",
            "county": "Hillsborough",
            "age": 38,
            "household_size": 2,
            "annual_income": 88000,
            "healthy": True,
            "has_current_coverage": True,
            "intent": "Testing the full qualified lead follow-up thread and Calendly booking flow.",
            "tcpa_consent": True,
            "consent_text": "Test lead consent for demo workflow.",
        },
        actor,
        "local",
        "test-run",
    )
    lead_id = lead["id"]
    assign_lead(conn, lead_id, {"assigned_to": "Auto Qualifier"}, actor)
    apply_updates(conn, lead_id, {"qualification_step": "coverage_for", "updated_at": utcnow()})
    add_conversation_message(conn, lead_id, {"direction": "outbound", "sender": "Manual Follow-up", "body": "Ok, were you looking for coverage for the family or just yourself?", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "inbound", "sender": "Lead", "body": "For my family.", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "outbound", "sender": "Qualifier Demo", "body": "Ok, thanks for that info. How soon did you need a plan to begin?", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "inbound", "sender": "Lead", "body": "Ideally next month.", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "outbound", "sender": "Qualifier Demo", "body": "Ok, and were you looking for just a medical plan or did you need dental and vision as well?", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "inbound", "sender": "Lead", "body": "Medical with dental and vision.", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "outbound", "sender": "Qualifier Demo", "body": "Also are there any pre-existing conditions or medications that you need covered?", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "inbound", "sender": "Lead", "body": "Nothing major, no current meds.", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "outbound", "sender": "Qualifier Demo", "body": "Ok, I can help. Provide me with the DOB, height, weight & annualized income? I'll try to get you some numbers...", "channel": "manual"}, actor)
    add_conversation_message(conn, lead_id, {"direction": "inbound", "sender": "Lead", "body": "DOB 04/12/1987, 5'10, 184 lb, about $88k annualized.", "channel": "manual"}, actor)
    intake = {
        "coverage_for": "Family",
        "plan_start_timing": "Next month",
        "needs_dental_vision": "Medical + dental + vision",
        "conditions_meds": "Nothing major, no current meds.",
        "dob": "04/12/1987",
        "height": "5'10",
        "weight": "184 lb",
        "annualized_income_text": "$88,000",
    }
    save_qualification_intake(conn, lead_id, intake, actor)
    qualify_lead(conn, lead_id, {"notes": "Demo: full qualifier intake captured; ready for Calendly booking.", "intent_confirmed": True}, actor)
    set_phone_status(conn, lead_id, "VERIFIED", actor)
    lead = get_lead(conn, lead_id, reveal=True)
    return {
        "lead": lead,
        "conversation": conversation_rows(conn, lead_id),
        "booking_url": calendly_booking_url(lead),
    }


def set_stage(conn: sqlite3.Connection, lead_id: str, stage: str, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
    stage = stage.upper()
    if stage not in STAGES:
        raise ValueError(f"Stage must be one of: {', '.join(STAGES)}")
    lead = get_lead(conn, lead_id, reveal=True)
    if lead["stage"] == "SUPPRESSED" and stage != "SUPPRESSED":
        raise ValueError("Suppressed leads cannot re-enter the pipeline until removed from DNC.")
    updates: dict[str, Any] = {"stage": stage, "updated_at": utcnow()}
    if stage == "READY":
        appointment_at = str(payload.get("appointment_at") or lead["appointment_at"]).strip()
        intent_confirmed = bool(payload.get("intent_confirmed", lead["intent_confirmed"]))
        if not appointment_at or not intent_confirmed:
            raise ValueError("READY requires appointment_at and intent_confirmed=true.")
        updates["appointment_at"] = appointment_at
        updates["intent_confirmed"] = 1
    if stage in ("WON", "LOST"):
        if lead["stage"] != "READY":
            raise ValueError("Principal results can only be set after the lead is READY.")
        updates["lost_reason"] = str(payload.get("lost_reason") or "").strip() if stage == "LOST" else ""
    apply_updates(conn, lead_id, updates)
    record_audit(conn, "lead.stage_changed", {"stage": stage}, lead_id, actor)
    if stage == "READY":
        deliver_ready_lead(conn, lead_id)
    return get_lead(conn, lead_id, reveal=True)


def assign_lead(conn: sqlite3.Connection, lead_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    assignee = str(payload.get("assigned_to") or payload.get("qualifier") or "").strip()
    if not assignee:
        raise ValueError("assigned_to is required.")
    apply_updates(conn, lead_id, {"assigned_to": assignee, "stage": "TRIAGE", "updated_at": utcnow()})
    record_audit(conn, "lead.assigned", {"assigned_to": assignee}, lead_id, actor)
    return get_lead(conn, lead_id, reveal=True)


def qualify_lead(conn: sqlite3.Connection, lead_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    notes = str(payload.get("notes") or "").strip()
    updates = {
        "stage": "QUALIFIED",
        "updated_at": utcnow(),
        "notes": notes,
        "intent_confirmed": int(bool(payload.get("intent_confirmed", True))),
    }
    if payload.get("appointment_at"):
        updates["appointment_at"] = str(payload["appointment_at"])
    apply_updates(conn, lead_id, updates)
    record_audit(conn, "lead.qualified", {"notes": notes[:160]}, lead_id, actor)
    return get_lead(conn, lead_id, reveal=True)


def save_qualification_intake(conn: sqlite3.Connection, lead_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    get_lead(conn, lead_id, reveal=False)
    fields = {
        "coverage_for": str(payload.get("coverage_for") or "").strip(),
        "plan_start_timing": str(payload.get("plan_start_timing") or "").strip(),
        "needs_dental_vision": str(payload.get("needs_dental_vision") or "").strip(),
        "conditions_meds": str(payload.get("conditions_meds") or "").strip(),
        "dob": str(payload.get("dob") or "").strip(),
        "height": str(payload.get("height") or "").strip(),
        "weight": str(payload.get("weight") or "").strip(),
        "annualized_income_text": str(payload.get("annualized_income_text") or payload.get("annualized_income") or "").strip(),
        "updated_at": utcnow(),
    }
    apply_updates(conn, lead_id, fields)
    summary = qualification_summary({**get_lead(conn, lead_id, reveal=True), **fields})
    add_conversation_message(
        conn,
        lead_id,
        {"direction": "internal", "sender": "Qualifier Intake", "body": summary, "channel": "note"},
        actor,
    )
    record_audit(conn, "lead.qualification_intake_saved", {key: bool(value) for key, value in fields.items() if key != "updated_at"}, lead_id, actor)
    return get_lead(conn, lead_id, reveal=True)


def qualification_summary(lead: dict[str, Any]) -> str:
    lines = [
        "Qualification intake:",
        f"Coverage for: {lead.get('coverage_for') or '-'}",
        f"Plan start: {lead.get('plan_start_timing') or '-'}",
        f"Dental/Vision: {lead.get('needs_dental_vision') or '-'}",
        f"Conditions/Meds: {lead.get('conditions_meds') or '-'}",
        f"DOB: {lead.get('dob') or '-'}",
        f"Height: {lead.get('height') or '-'}",
        f"Weight: {lead.get('weight') or '-'}",
        f"Annualized income: {lead.get('annualized_income_text') or lead.get('annual_income') or '-'}",
    ]
    return "\n".join(lines)


def apply_updates(conn: sqlite3.Connection, lead_id: str, updates: dict[str, Any]) -> None:
    fields = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(f"UPDATE leads SET {fields} WHERE id = ?", (*updates.values(), lead_id))


def add_dnc(conn: sqlite3.Connection, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    kind = str(payload.get("kind") or "phone").lower()
    value = normalize_phone(payload.get("value")) if kind == "phone" else normalize_email(payload.get("value"))
    if not value:
        raise ValueError("DNC value is required.")
    value_hash = stable_hash(value)
    reason = str(payload.get("reason") or "manual suppression").strip()
    conn.execute(
        "INSERT OR REPLACE INTO dnc_entries (hash, kind, value_hint, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (value_hash, kind, value[-4:] if kind == "phone" else value[:3] + "...", reason, utcnow()),
    )
    affected = conn.execute(
        "UPDATE leads SET dnc_suppressed = 1, stage = 'SUPPRESSED', updated_at = ? WHERE phone_hash = ? OR email_hash = ?",
        (utcnow(), value_hash, value_hash),
    ).rowcount
    record_audit(conn, "dnc.added", {"kind": kind, "affected_leads": affected}, None, actor)
    return {"kind": kind, "value_hint": value[-4:] if kind == "phone" else value[:3] + "...", "affected_leads": affected}


def opt_out(conn: sqlite3.Connection, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    phone = normalize_phone(payload.get("phone") or payload.get("value"))
    email = normalize_email(payload.get("email") or payload.get("value"))
    hashes = [stable_hash(value) for value in (phone, email) if value]
    if not hashes:
        raise ValueError("A phone or email is required for opt-out.")
    affected = 0
    for value_hash in hashes:
        hint = (phone[-4:] if phone else email[:3] + "...") if (phone or email) else "..."
        conn.execute(
            "INSERT OR REPLACE INTO dnc_entries (hash, kind, value_hint, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (value_hash, "opt-out", hint, "consumer opt-out", utcnow()),
        )
        affected += conn.execute(
            """
            UPDATE leads
            SET contact_status = 'OPTED_OUT', compliance_status = 'BLOCKED', dnc_suppressed = 1,
                stage = CASE WHEN stage IN ('WON', 'LOST') THEN stage ELSE 'SUPPRESSED' END,
                updated_at = ?
            WHERE phone_hash = ? OR email_hash = ?
            """,
            (utcnow(), value_hash, value_hash),
        ).rowcount
    record_audit(conn, "lead.opted_out", {"affected_leads": affected}, None, actor)
    return {"ok": True, "affected_leads": affected}


def webhook_source_name(provider: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", provider.lower())
    names = {
        "cobraprospect": "COBRA / Job Loss",
        "google": "Google Search",
        "healthsherpa": "HealthSherpa",
        "jobloss": "COBRA / Job Loss",
        "layoff": "Layoff Signal",
        "layoffsignal": "Layoff Signal",
        "marketplace": "Marketplace Shopper",
        "marketplaceshopper": "Marketplace Shopper",
        "meta": "Meta Lead Ad",
        "publicsocial": "Public Social Intent",
        "publicsocialintent": "Public Social Intent",
        "referralpartner": "Referral Partner",
        "retargeting": "Retargeting Ad",
        "retargetingad": "Retargeting Ad",
        "ushealth": "USHealth Group",
        "ushealthgroup": "USHealth Group",
        "website": "Website Form",
        "yellowpages": "Yellow Pages",
    }
    return names.get(normalized, provider.replace("-", " ").replace("_", " ").title())


def normalize_webhook_payload(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    fields = payload.get("field_data") if isinstance(payload.get("field_data"), list) else None
    field_map: dict[str, Any] = {}
    if fields:
        for item in fields:
            name = str(item.get("name") or "").lower()
            values = item.get("values") or []
            field_map[name] = values[0] if values else ""
    data = {**payload, **field_map}
    full_name = data.get("full_name") or data.get("name") or data.get("first_name") or "Unknown Lead"
    if data.get("last_name") and data.get("first_name"):
        full_name = f"{data.get('first_name')} {data.get('last_name')}"
    consent = data.get("consent_text") or data.get("tcpa_consent_text") or data.get("privacy_consent") or ""
    return {
        "source": webhook_source_name(provider),
        "source_url": data.get("source_url") or data.get("url") or data.get("ad_url") or data.get("healthsherpa_url") or data.get("ushealthgroup_url") or data.get("yellowpages_url") or data.get("signal_url") or "",
        "external_id": str(data.get("id") or data.get("lead_id") or data.get("leadgen_id") or data.get("external_id") or ""),
        "name": full_name,
        "phone": data.get("phone") or data.get("phone_number") or data.get("mobile_phone") or "",
        "email": data.get("email") or data.get("email_address") or "",
        "state": data.get("state") or data.get("region") or "",
        "county": data.get("county") or "",
        "age": parse_int(data.get("age")),
        "household_size": parse_int(data.get("household_size"), 1),
        "annual_income": parse_int(data.get("annual_income") or data.get("income")),
        "healthy": bool(data.get("healthy") in (True, "true", "yes", "Yes", "Y", "y", "1", 1)),
        "has_current_coverage": bool(data.get("has_current_coverage") in (True, "true", "yes", "Yes", "Y", "y", "1", 1)),
        "intent": data.get("intent") or data.get("message") or data.get("notes") or "",
        "tcpa_consent": bool(data.get("tcpa_consent") in (True, "true", "yes", "Yes", "Y", "y", "1", 1)) or bool(consent),
        "consent_text": consent,
        "notes": data.get("notes") or "",
    }


def ingest_webhook(
    conn: sqlite3.Connection,
    provider: str,
    payload: dict[str, Any],
    actor: str,
    ip: str,
    user_agent: str,
) -> dict[str, Any]:
    lead_payload = normalize_webhook_payload(provider, payload)
    try:
        lead = create_lead(conn, lead_payload, actor, ip, user_agent)
        status = "created"
        response = "lead created"
        lead_id = lead["id"]
    except Exception as exc:
        lead = {}
        status = "error"
        response = str(exc)
        lead_id = None
    conn.execute(
        "INSERT INTO webhook_logs (id, created_at, provider, status, lead_id, payload, response) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), provider, status, lead_id, json.dumps(payload), response[:500]),
    )
    if status == "error":
        raise ValueError(response)
    return {"status": status, "lead": lead}


def handle_calendly_webhook(conn: sqlite3.Connection, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    event = payload.get("event") or payload.get("trigger") or ""
    body = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    email = normalize_email(
        body.get("email")
        or body.get("invitee_email")
        or (body.get("invitee") or {}).get("email")
    )
    name = body.get("name") or body.get("invitee_name") or (body.get("invitee") or {}).get("name") or ""
    scheduled_at = (
        body.get("start_time")
        or body.get("scheduled_event", {}).get("start_time")
        or body.get("event_start_time")
        or ""
    )
    if not email:
        raise ValueError("Calendly webhook did not include invitee email.")
    row = conn.execute(
        "SELECT id FROM leads WHERE email_hash = ? ORDER BY created_at DESC LIMIT 1",
        (stable_hash(email),),
    ).fetchone()
    if not row:
        lead = create_lead(
            conn,
            {
                "source": "Calendly",
                "name": name or "Calendly Invitee",
                "email": email,
                "phone": body.get("phone") or "",
                "state": body.get("state") or "",
                "intent": "Booked through Calendly",
                "tcpa_consent": bool(body.get("tcpa_consent")),
                "consent_text": body.get("consent_text") or "Calendly booking received.",
                "source_url": body.get("uri") or body.get("event") or "",
            },
            actor,
            "webhook",
            "calendly",
        )
        lead_id = lead["id"]
    else:
        lead_id = row["id"]
    lead = get_lead(conn, lead_id, reveal=True)
    if "canceled" in str(event).lower():
        apply_updates(conn, lead_id, {"appointment_at": "", "updated_at": utcnow()})
        record_audit(conn, "calendly.canceled", payload, lead_id, actor)
        return {"status": "canceled", "lead": get_lead(conn, lead_id, reveal=True)}
    if scheduled_at:
        apply_updates(
            conn,
            lead_id,
            {
                "appointment_at": scheduled_at,
                "intent_confirmed": 1,
                "stage": "READY" if lead.get("compliance_status") != "BLOCKED" else lead["stage"],
                "updated_at": utcnow(),
            },
        )
        record_audit(conn, "calendly.booked", {"appointment_at": scheduled_at}, lead_id, actor)
        if get_lead(conn, lead_id, reveal=True)["stage"] == "READY":
            deliver_ready_lead(conn, lead_id)
            notify_owner_ready(conn, lead_id, "Calendly appointment booked")
    return {"status": "booked", "lead": get_lead(conn, lead_id, reveal=True)}


def calendly_token() -> str:
    return get_config()["settings"].get("calendly_api_token", "").strip()


def calendly_api_get(path_or_url: str, token: str) -> dict[str, Any]:
    url = path_or_url if path_or_url.startswith("http") else "https://api.calendly.com" + path_or_url
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "HealthLeadBot/1.0 (+https://calendly.com/arielvahnishhealth)",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def calendly_event_uuid(uri: str) -> str:
    return uri.rstrip("/").split("/")[-1]


def calendly_invitee_phone(invitee: dict[str, Any]) -> str:
    for key in ("text_reminder_number", "phone_number", "phone"):
        if invitee.get(key):
            return str(invitee[key])
    for item in invitee.get("questions_and_answers", []) or []:
        question = str(item.get("question") or "").lower()
        answer = str(item.get("answer") or "")
        if "phone" in question or "number" in question:
            return answer
    return ""


def calendly_sync_bookings(conn: sqlite3.Connection, actor: str = "calendly-sync") -> dict[str, Any]:
    token = calendly_token()
    if not token:
        return {"status": "missing", "detail": "CALENDLY_API_TOKEN is not configured.", "synced": 0, "created": 0, "ready": 0}
    settings = get_config()["settings"]
    days = max(parse_int(settings.get("calendly_sync_days"), 60) or 60, 1)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    min_start = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    max_start = (now + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    me = calendly_api_get("/users/me", token).get("resource", {})
    user_uri = me.get("uri")
    if not user_uri:
        raise ValueError("Calendly API did not return user URI.")
    query = urlencode(
        {
            "user": user_uri,
            "min_start_time": min_start,
            "max_start_time": max_start,
            "status": "active",
            "count": 100,
            "sort": "start_time:asc",
        }
    )
    events = calendly_api_get(f"/scheduled_events?{query}", token).get("collection", [])
    synced = 0
    created = 0
    ready = 0
    errors: list[str] = []
    for event in events:
        event_uri = str(event.get("uri") or "")
        if not event_uri:
            continue
        event_id = calendly_event_uuid(event_uri)
        try:
            invitees = calendly_api_get(f"/scheduled_events/{quote(event_id)}/invitees?count=100", token).get("collection", [])
        except Exception as exc:
            errors.append(f"{event_id}: {exc}")
            continue
        for invitee in invitees:
            if str(invitee.get("status") or "active").lower() == "canceled":
                continue
            email = normalize_email(invitee.get("email"))
            if not email:
                continue
            before = find_lead_by_phone_or_email(conn, "", email)
            payload = {
                "event": "invitee.created",
                "payload": {
                    "email": email,
                    "name": invitee.get("name") or "",
                    "phone": calendly_invitee_phone(invitee),
                    "uri": invitee.get("uri") or event_uri,
                    "scheduled_event": event,
                    "start_time": event.get("start_time") or "",
                    "consent_text": "Calendly booking synced from API.",
                    "tcpa_consent": False,
                },
            }
            try:
                result = handle_calendly_webhook(conn, payload, actor)
                synced += 1
                if not before:
                    created += 1
                if result.get("lead", {}).get("stage") == "READY":
                    ready += 1
            except Exception as exc:
                errors.append(f"{email}: {exc}")
    detail = {"status": "complete", "synced": synced, "created": created, "ready": ready, "errors": errors[:10]}
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), "calendly_sync", "complete" if not errors else "partial", json.dumps(detail)),
    )
    record_audit(conn, "calendly.sync", detail, None, actor)
    return detail


def source_metrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source,
               COUNT(*) AS total,
               ROUND(AVG(score), 1) AS avg_score,
               SUM(CASE WHEN stage = 'READY' THEN 1 ELSE 0 END) AS ready,
               SUM(CASE WHEN stage = 'WON' THEN 1 ELSE 0 END) AS won,
               SUM(CASE WHEN compliance_status = 'BLOCKED' THEN 1 ELSE 0 END) AS blocked
        FROM leads
        GROUP BY source
        ORDER BY total DESC, avg_score DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def compliance_export_bytes(conn: sqlite3.Connection) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("audit_logs.json", json.dumps(audit_rows(conn, 5000), indent=2))
        zf.writestr("consent_logs.json", json.dumps([dict(row) for row in conn.execute("SELECT * FROM consent_logs ORDER BY created_at DESC")], indent=2))
        zf.writestr("dnc_entries.json", json.dumps([dict(row) for row in conn.execute("SELECT * FROM dnc_entries ORDER BY created_at DESC")], indent=2))
        zf.writestr("delivery_logs.json", json.dumps(delivery_rows(conn, 5000), indent=2))
        zf.writestr("source_metrics.json", json.dumps(source_metrics(conn), indent=2))
    return buf.getvalue()


def backup_bytes(conn: sqlite3.Connection) -> bytes:
    backup_path = Path("/private/tmp") / f"leadbot-backup-{uuid.uuid4()}.sqlite3"
    backup_conn = sqlite3.connect(backup_path)
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("leads.csv", csv_bytes(conn))
        zf.writestr("lead_bot.sqlite3", backup_path.read_bytes())
        zf.writestr("settings.json", json.dumps(load_settings(), indent=2))
        zf.writestr("compliance_export.zip", compliance_export_bytes(conn))
    try:
        backup_path.unlink()
    except OSError:
        pass
    return buf.getvalue()


def run_daily_ops(conn: sqlite3.Connection, actor: str) -> dict[str, Any]:
    data = counts(conn)
    qualifier = run_auto_qualifier(conn, actor)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=int(load_settings().get("qualifier_sla_minutes", "15") or 15))
    stale = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE stage = 'NEW' AND created_at < ?",
        (stale_cutoff.replace(microsecond=0).isoformat(),),
    ).fetchone()[0]
    detail = {"counts": data, "stale_new_leads": stale, "source_metrics": source_metrics(conn), "auto_qualifier": qualifier}
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), "daily_ops", "complete", json.dumps(detail)),
    )
    record_audit(conn, "automation.daily_ops", detail, None, actor)
    return detail


def run_setup_autopilot(conn: sqlite3.Connection, actor: str = "setup-autopilot") -> dict[str, Any]:
    settings = load_settings()
    fixed: list[str] = []
    actions: list[str] = []
    needs: list[str] = []

    auto_defaults = {
        "licensed_states": ",".join(sorted(LICENSED_STATES)),
        "email_template": DEFAULT_SETTINGS["email_template"],
        "qualifier_sla_minutes": DEFAULT_SETTINGS["qualifier_sla_minutes"],
        "auto_assign_owner": DEFAULT_SETTINGS["auto_assign_owner"],
    }
    for key, value in auto_defaults.items():
        if not str(settings.get(key) or "").strip():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, utcnow()),
            )
            fixed.append(key)

    config = get_config()
    booking_url = config["settings"].get("calendar_booking_url", "").strip()
    booking_problem = calendly_link_problem(booking_url)
    if booking_problem:
        needs.append(booking_problem)
        create_owner_notification_once(
            conn,
            "",
            "SETUP",
            "Calendly booking URL needed",
            booking_problem,
        )

    if not calendly_token():
        needs.append("Add a Calendly API token so the app can pull real booked meetings from Calendly.")
        create_owner_notification_once(
            conn,
            "",
            "SETUP",
            "Calendly API token needed",
            "The public Calendly link sends leads to book, but it does not let this app read your private Calendly calendar. Add a Calendly personal access token in Automation, then press Sync Calendly.",
        )

    if not os.getenv("LEAD_BOT_WEBHOOK_SECRET"):
        needs.append("Set LEAD_BOT_WEBHOOK_SECRET before exposing webhooks publicly.")
        create_owner_notification_once(
            conn,
            "",
            "SETUP",
            "Webhook secret should be set",
            "Set LEAD_BOT_WEBHOOK_SECRET in .env before putting website or Calendly webhooks online.",
        )

    if not os.getenv("LEAD_BOT_PASSWORD"):
        needs.append("Set LEAD_BOT_PASSWORD before using this outside localhost.")
        create_owner_notification_once(
            conn,
            "",
            "SETUP",
            "Private password needed",
            "Set LEAD_BOT_PASSWORD in .env before putting the dashboard online.",
        )

    rows = conn.execute(
        """
        SELECT id FROM leads
        WHERE stage = 'QUALIFIED'
          AND booking_link_sent_at = ''
          AND compliance_status != 'BLOCKED'
          AND contact_status NOT IN ('BLOCKED', 'OPTED_OUT')
        ORDER BY updated_at ASC
        LIMIT 25
        """
    ).fetchall()
    for row in rows:
        try:
            send_booking_link(conn, row["id"], actor)
            actions.append(f"sent_booking_link:{row['id']}")
        except ValueError as exc:
            actions.append(f"booking_link_not_sent:{row['id']}:{exc}")

    ready_rows = conn.execute(
        "SELECT id FROM leads WHERE stage = 'READY' AND ready_notified_at = '' ORDER BY updated_at ASC LIMIT 25"
    ).fetchall()
    for row in ready_rows:
        notify_owner_ready(conn, row["id"], "Setup autopilot found an unnotified READY lead.")
        actions.append(f"ready_notified:{row['id']}")

    result = {
        "fixed": fixed,
        "actions": actions,
        "needs": needs,
        "manual_outreach": True,
        "booking_configured": bool(booking_url),
    }
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            utcnow(),
            "setup_autopilot",
            "needs_setup" if needs else "complete",
            json.dumps(result),
        ),
    )
    if fixed or actions or needs:
        record_audit(conn, "automation.setup_autopilot", result, None, actor)
    return result


def run_qualifier_followup_bot(conn: sqlite3.Connection, actor: str = "qualifier-followup-bot") -> dict[str, Any]:
    settings = get_config()["settings"]
    hours = max(parse_int(settings.get("qualifier_followup_hours"), 2) or 2, 1)
    max_followups = max(parse_int(settings.get("qualifier_max_followups"), 2) or 2, 0)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    rows = conn.execute(
        """
        SELECT id FROM leads
        WHERE stage IN ('NEW', 'TRIAGE')
          AND auto_qualifier_enabled = 1
          AND compliance_status != 'BLOCKED'
          AND contact_status NOT IN ('BLOCKED', 'OPTED_OUT')
          AND qualification_step != ''
          AND qualification_step != 'complete'
          AND last_contacted_at != ''
          AND last_contacted_at < ?
          AND qualifier_followup_count < ?
          AND (last_qualifier_followup_at = '' OR last_qualifier_followup_at < ?)
        ORDER BY last_contacted_at ASC
        LIMIT 25
        """,
        (cutoff, max_followups, cutoff),
    ).fetchall()
    actions: list[str] = []
    for row in rows:
        lead = get_lead(conn, row["id"], reveal=True)
        question = question_for_step(lead.get("qualification_step") or next_missing_qualification_step(lead))
        body = "Just checking in so I can finish your health coverage quote. " + (question or "Reply when you have a minute.")
        append_auto_outbound(conn, lead["id"], body, actor)
        apply_updates(
            conn,
            lead["id"],
            {
                "qualifier_followup_count": int(lead.get("qualifier_followup_count") or 0) + 1,
                "last_qualifier_followup_at": utcnow(),
                "updated_at": utcnow(),
            },
        )
        actions.append(lead["id"])
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), "qualifier_followup_bot", "complete", json.dumps({"sent": len(actions), "lead_ids": actions})),
    )
    return {"sent": len(actions), "lead_ids": actions}


def run_booking_followup_bot(conn: sqlite3.Connection, actor: str = "booking-followup-bot") -> dict[str, Any]:
    settings = get_config()["settings"]
    booking = settings.get("calendar_booking_url", "").strip()
    if not booking:
        return {"sent": 0, "reason": "booking URL missing", "lead_ids": []}
    hours = max(parse_int(settings.get("booking_followup_hours"), 24) or 24, 1)
    max_followups = max(parse_int(settings.get("booking_max_followups"), 2) or 2, 0)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    rows = conn.execute(
        """
        SELECT id FROM leads
        WHERE stage = 'QUALIFIED'
          AND appointment_at = ''
          AND booking_link_sent_at != ''
          AND booking_link_sent_at < ?
          AND compliance_status != 'BLOCKED'
          AND contact_status NOT IN ('BLOCKED', 'OPTED_OUT')
          AND booking_followup_count < ?
          AND (last_booking_followup_at = '' OR last_booking_followup_at < ?)
        ORDER BY booking_link_sent_at ASC
        LIMIT 25
        """,
        (cutoff, max_followups, cutoff),
    ).fetchall()
    actions: list[str] = []
    for row in rows:
        lead = get_lead(conn, row["id"], reveal=True)
        body = "Quick reminder: you are qualified. Please pick a time here so Ariel can go over plan options with you: " + booking
        append_auto_outbound(conn, lead["id"], body, actor)
        apply_updates(
            conn,
            lead["id"],
            {
                "booking_followup_count": int(lead.get("booking_followup_count") or 0) + 1,
                "last_booking_followup_at": utcnow(),
                "updated_at": utcnow(),
            },
        )
        actions.append(lead["id"])
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), "booking_followup_bot", "complete", json.dumps({"sent": len(actions), "lead_ids": actions})),
    )
    return {"sent": len(actions), "lead_ids": actions}


def run_source_intelligence_bot(conn: sqlite3.Connection, actor: str = "source-intelligence-bot") -> dict[str, Any]:
    insights: list[str] = []
    for source in source_metrics(conn):
        total = int(source.get("total") or 0)
        blocked = int(source.get("blocked") or 0)
        ready = int(source.get("ready") or 0)
        won = int(source.get("won") or 0)
        avg = float(source.get("avg_score") or 0)
        if total >= 3 and blocked / max(total, 1) >= 0.5:
            insights.append(f"{source['source']} has a high blocked rate: {blocked}/{total}. Check form quality, consent, or targeting.")
        if total >= 3 and (ready + won) / max(total, 1) >= 0.35:
            insights.append(f"{source['source']} is producing stronger prospects: {ready} READY and {won} WON from {total}, avg score {avg}.")
    for insight in insights:
        create_owner_notification_once(conn, "", "INSIGHT", "Source insight", insight)
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), "source_intelligence_bot", "complete", json.dumps({"insights": insights})),
    )
    return {"insights": insights}


def run_daily_owner_summary_bot(conn: sqlite3.Connection, actor: str = "owner-summary-bot") -> dict[str, Any]:
    today = datetime.now(timezone.utc).date().isoformat()
    existing = conn.execute(
        """
        SELECT 1 FROM automation_logs
        WHERE job = 'owner_summary_bot'
          AND created_at >= ?
        LIMIT 1
        """,
        (today + "T00:00:00+00:00",),
    ).fetchone()
    if existing:
        return {"sent": False, "reason": "already sent today"}
    data = counts(conn)
    health = automation_health(conn)
    stage_counts = data.get("stage_counts", {})
    body = (
        f"Daily automation summary: {data.get('total', 0)} total leads. "
        f"NEW {stage_counts.get('NEW', 0)}, QUALIFIED {stage_counts.get('QUALIFIED', 0)}, "
        f"READY {stage_counts.get('READY', 0)}, WON {stage_counts.get('WON', 0)}. "
        f"Health: {health.get('label')}."
    )
    create_owner_notification_once(conn, "", "SUMMARY", "Daily bot summary", body)
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), "owner_summary_bot", "complete", json.dumps({"body": body})),
    )
    return {"sent": True, "body": body}


def run_bot_team(conn: sqlite3.Connection, actor: str = "bot-team") -> dict[str, Any]:
    result = {
        "setup": run_setup_autopilot(conn, actor),
        "calendly": calendly_sync_bookings(conn, actor) if calendly_token() else {"status": "missing", "synced": 0},
        "qualifier": run_auto_qualifier(conn, actor),
        "qualifier_followup": run_qualifier_followup_bot(conn, actor),
        "booking_followup": run_booking_followup_bot(conn, actor),
        "source_intelligence": run_source_intelligence_bot(conn, actor),
        "owner_summary": run_daily_owner_summary_bot(conn, actor),
    }
    conn.execute(
        "INSERT INTO automation_logs (id, created_at, job, status, detail) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), "bot_team", "complete", json.dumps(result)),
    )
    return result


def run_background_autopilot_once(actor: str = "background-autopilot") -> dict[str, Any]:
    with open_db() as conn:
        result = run_bot_team(conn, actor)
        conn.commit()
        return result


def ai_context(conn: sqlite3.Connection) -> dict[str, Any]:
    data = counts(conn)
    return {
        "pipeline_counts": data,
        "automation_health": automation_health(conn),
        "source_metrics": source_metrics(conn),
        "recent_audit": audit_rows(conn, 10),
        "delivery_logs": delivery_rows(conn, 10),
        "settings": {
            key: value
            for key, value in load_settings().items()
            if key not in {"hubspot_access_token"}
        },
        "available_actions": [
            "Create leads from dashboard or POST /api/leads",
            "Receive webhooks at /api/webhooks/{provider}",
            "Run daily ops with POST /api/automation/run",
            "Download compliance export at /api/compliance/export.zip",
            "Download backup at /api/backup.zip",
            "Update settings from the Automation panel",
        ],
        "required_qualification_intake": [
            "Coverage for family or just self",
            "How soon the plan needs to begin",
            "Medical only or dental/vision too",
            "Pre-existing conditions or medications that need covered",
            "DOB",
            "Height",
            "Weight",
            "Annualized income",
        ],
    }


def local_ai_reply(question: str, context: dict[str, Any]) -> str:
    q = question.lower()
    counts_data = context.get("pipeline_counts", {})
    stage_counts = counts_data.get("stage_counts", {})
    suggestions = []
    if "fix" in q or "broken" in q or "error" in q:
        suggestions.extend(
            [
                "Check the Audit tab for the newest error or blocked delivery.",
                "Run the daily ops check from the Automation panel.",
                "Download a backup before changing credentials or routing rules.",
                "If a lead is not delivering, confirm it is READY, CONTACTABLE, has TCPA consent, and is in a licensed state.",
            ]
        )
    elif "lead" in q or "more" in q or "source" in q:
        suggestions.extend(
            [
                "Connect website, Meta, and Google form webhooks to /api/webhooks/website, /api/webhooks/meta, and /api/webhooks/google.",
                "Use the webhook secret from your .env in the X-Webhook-Secret header.",
                "Watch the Sources panel for average score, READY count, WON count, and blocked leads.",
                "Prioritize sources with higher average score and lower blocked rate.",
            ]
        )
    elif "compliance" in q or "dnc" in q or "consent" in q:
        suggestions.extend(
            [
                "Every outreach-ready lead must have consent text, TCPA consent, licensed-state fit, and no DNC/opt-out match.",
                "Use the Opt-Out tool for STOP replies and the DNC tool for manual suppression.",
                "Use the AUD export for audit logs, consent logs, DNC entries, delivery logs, and source metrics.",
            ]
        )
    else:
        suggestions.extend(
            [
                "Use the Automation panel to set Calendly, licensed states, owner, and templates.",
                "Let the auto qualifier send Calendly only after the required intake is complete.",
                "Use READY only after Calendly confirms the appointment, so you step in for booked prospects.",
            ]
        )
    return (
        "Here is what I see right now:\n"
        f"- Total leads: {counts_data.get('total', 0)}\n"
        f"- NEW: {stage_counts.get('NEW', 0)} | QUALIFIED: {stage_counts.get('QUALIFIED', 0)} | READY: {stage_counts.get('READY', 0)} | WON: {stage_counts.get('WON', 0)}\n\n"
        "Recommended next steps:\n- "
        + "\n- ".join(suggestions)
    )


def call_openai_ai(question: str, context: dict[str, Any]) -> tuple[str, str]:
    key = openai_api_key()
    if not key:
        return "local", local_ai_reply(question, context)
    model = os.getenv("AI_MODEL") or load_settings().get("ai_model", "gpt-4.1-mini")
    payload = {
        "model": model,
        "instructions": (
            "You are a private AI operations helper embedded in a health insurance lead bot. "
            "Help the owner improve, troubleshoot, and operate the app. Be concrete and concise. "
            "Do not ask for sensitive health details, diagnosis names, or medication names. "
            "Never claim you changed files or ran commands from the app. Suggest safe next steps."
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "App context, with phone/email/token values intentionally omitted:\n"
                            + json.dumps(context, indent=2)
                            + "\n\nOwner question:\n"
                            + question
                        ),
                    }
                ],
            }
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("output_text"):
            return "openai", body["output_text"]
        parts: list[str] = []
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in ("output_text", "text") and content.get("text"):
                    parts.append(content["text"])
        return "openai", "\n".join(parts).strip() or "I did not receive a usable response."
    except Exception as exc:
        return "local", local_ai_reply(question, context) + f"\n\nOpenAI fallback note: {exc}"


def ai_chat(conn: sqlite3.Connection, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    question = str(payload.get("message") or payload.get("question") or "").strip()
    if not question:
        raise ValueError("message is required.")
    context = ai_context(conn)
    provider, answer = call_openai_ai(question, context)
    record_audit(conn, "ai.chat", {"provider": provider, "question": question[:200]}, None, actor)
    return {"provider": provider, "answer": answer, "context": {"counts": context["pipeline_counts"]}}


def setup_status(conn: sqlite3.Connection) -> dict[str, Any]:
    config = get_config()
    settings = config["settings"]
    booking_url = settings.get("calendar_booking_url", "")
    booking_problem = calendly_link_problem(booking_url)
    calendly_api_ready = bool(settings.get("calendly_api_token", "").strip())
    checks = [
        {
            "key": "private_login",
            "label": "Private Login",
            "ok": bool(os.getenv("LEAD_BOT_PASSWORD")),
            "detail": "Owner username and password are configured.",
        },
        {
            "key": "webhook_secret",
            "label": "Webhook Secret",
            "ok": bool(os.getenv("LEAD_BOT_WEBHOOK_SECRET")),
            "detail": "External forms must send X-Webhook-Secret.",
        },
        {
            "key": "quote_page",
            "label": "Public Quote Page",
            "ok": True,
            "detail": "Lead capture form is live at /quote and auto-starts the qualifier.",
        },
        {
            "key": "licensed_states",
            "label": "Licensed States",
            "ok": bool(settings.get("licensed_states")),
            "detail": settings.get("licensed_states", ""),
        },
        {
            "key": "hubspot",
            "label": "HubSpot",
            "ok": bool(config.get("hubspot_api_key")),
            "detail": "READY leads will create HubSpot contacts when configured.",
        },
        {
            "key": "booking",
            "label": "Booking URL",
            "ok": bool(booking_url) and not booking_problem,
            "detail": (
                "Calendly link is configured."
                if not booking_problem
                else booking_problem
            )
            if booking_url
            else "Add Calendly or booking link for qualifier workflow.",
        },
        {
            "key": "calendly_api",
            "label": "Calendly Booking Sync",
            "ok": calendly_api_ready,
            "detail": "Calendly API token is configured; the app can pull booked meetings."
            if calendly_api_ready
            else "Add a Calendly API token so this local app can see meetings booked in Calendly.",
        },
        {
            "key": "templates",
            "label": "Outreach Templates",
            "ok": bool(settings.get("email_template")),
            "detail": "Manual follow-up and email templates are saved.",
        },
        {
            "key": "ai",
            "label": "AI Helper",
            "ok": True,
            "detail": "Using OpenAI model." if openai_api_key() else "Using built-in local advisor until OPENAI_API_KEY is added.",
        },
    ]
    return {
        "checks": checks,
        "complete": sum(1 for check in checks if check["ok"]),
        "total": len(checks),
        "counts": counts(conn),
        "webhook_urls": {
            "website": "/api/webhooks/website",
            "meta": "/api/webhooks/meta",
            "google": "/api/webhooks/google",
            "healthsherpa": "/api/webhooks/healthsherpa",
            "marketplace": "/api/webhooks/marketplace",
            "cobraprospect": "/api/webhooks/cobraprospect",
            "layoffsignal": "/api/webhooks/layoffsignal",
            "publicsocial": "/api/webhooks/publicsocial",
            "referralpartner": "/api/webhooks/referralpartner",
            "retargeting": "/api/webhooks/retargeting",
            "ushealthgroup": "/api/webhooks/ushealthgroup",
            "yellowpages": "/api/webhooks/yellowpages",
        },
    }


def age_label_since(value: str | None) -> str:
    if not value:
        return ""
    try:
        then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - then
    if age.total_seconds() < 0:
        return "now"
    if age.days:
        return f"{age.days}d"
    hours = age.seconds // 3600
    minutes = (age.seconds % 3600) // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def appointment_label(value: str | None) -> str:
    if not value:
        return ""
    try:
        appointment = parse_dt(str(value)).astimezone(timezone.utc)
    except ValueError:
        return "Check"
    return appointment.strftime("%b %d %H:%M UTC")


def automation_health(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = get_config()["settings"]
    booking_url = settings.get("calendar_booking_url", "").strip()
    booking_problem = calendly_link_problem(booking_url)
    calendly_api_ready = bool(settings.get("calendly_api_token", "").strip())
    sla_minutes = parse_int(settings.get("qualifier_sla_minutes"), 15) or 15
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=sla_minutes)
    stale_cutoff_text = stale_cutoff.replace(microsecond=0).isoformat()
    qualified_waiting = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE stage = 'QUALIFIED' AND appointment_at = ''"
    ).fetchone()[0]
    qualified_needs_link = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE stage = 'QUALIFIED' AND booking_link_sent_at = ''"
    ).fetchone()[0]
    booking_followup_hours = max(parse_int(settings.get("booking_followup_hours"), 24) or 24, 1)
    booking_max_followups = max(parse_int(settings.get("booking_max_followups"), 2) or 2, 0)
    booking_followup_cutoff = (datetime.now(timezone.utc) - timedelta(hours=booking_followup_hours)).replace(microsecond=0).isoformat()
    booking_followups_due = conn.execute(
        """
        SELECT COUNT(*) FROM leads
        WHERE stage = 'QUALIFIED'
          AND appointment_at = ''
          AND booking_link_sent_at != ''
          AND booking_link_sent_at < ?
          AND compliance_status != 'BLOCKED'
          AND contact_status NOT IN ('BLOCKED', 'OPTED_OUT')
          AND booking_followup_count < ?
          AND (last_booking_followup_at = '' OR last_booking_followup_at < ?)
        """,
        (booking_followup_cutoff, booking_max_followups, booking_followup_cutoff),
    ).fetchone()[0]
    ready_needs_review = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE stage = 'READY'"
    ).fetchone()[0]
    ready_appointment_rows = conn.execute(
        "SELECT appointment_at FROM leads WHERE stage = 'READY' AND appointment_at != ''"
    ).fetchall()
    now = datetime.now(timezone.utc)
    next_ready_appointment: datetime | None = None
    ready_past_due = 0
    invalid_ready_appointments = 0
    for row in ready_appointment_rows:
        try:
            appointment = parse_dt(row["appointment_at"]).astimezone(timezone.utc)
        except ValueError:
            invalid_ready_appointments += 1
            continue
        if next_ready_appointment is None or appointment < next_ready_appointment:
            next_ready_appointment = appointment
        if appointment <= now:
            ready_past_due += 1
    wrong_numbers = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE phone_status = 'WRONG_NUMBER'"
    ).fetchone()[0]
    unread_owner_alerts = conn.execute(
        "SELECT COUNT(*) FROM owner_notifications WHERE read_at = ''"
    ).fetchone()[0]
    unread_ready_alerts = conn.execute(
        "SELECT COUNT(*) FROM owner_notifications WHERE read_at = '' AND kind = 'READY'"
    ).fetchone()[0]
    oldest_unread_alert = conn.execute(
        "SELECT MIN(created_at) FROM owner_notifications WHERE read_at = ''"
    ).fetchone()[0]
    oldest_unread_alert_age = age_label_since(oldest_unread_alert)
    stale_threads = conn.execute(
        """
        SELECT COUNT(*) FROM leads
        WHERE stage IN ('NEW', 'TRIAGE')
          AND auto_qualifier_enabled = 1
          AND compliance_status != 'BLOCKED'
          AND contact_status NOT IN ('BLOCKED', 'OPTED_OUT')
          AND last_contacted_at != ''
          AND last_contacted_at < ?
          AND qualification_step != 'complete'
        """,
        (stale_cutoff_text,),
    ).fetchone()[0]
    last_job = conn.execute(
        "SELECT created_at, job, status FROM automation_logs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    last_job_age = age_label_since(last_job["created_at"]) if last_job else ""
    last_job_status = str(last_job["status"]) if last_job else ""
    last_job_seconds = None
    if last_job:
        try:
            last_job_at = datetime.fromisoformat(str(last_job["created_at"]).replace("Z", "+00:00"))
            if last_job_at.tzinfo is None:
                last_job_at = last_job_at.replace(tzinfo=timezone.utc)
            last_job_seconds = (datetime.now(timezone.utc) - last_job_at).total_seconds()
        except ValueError:
            last_job_seconds = None
    autopilot_disabled = os.getenv("LEAD_BOT_AUTOPILOT_DISABLED", "").lower() in ("1", "true", "yes")
    stale_bot_seconds = max(AUTOPILOT_INTERVAL_SECONDS * 3, 180)
    priorities: list[dict[str, str]] = []
    if not last_job and not autopilot_disabled:
        priorities.append({"level": "warning", "text": "No automation run has been recorded yet. Run Bot Team once to verify setup."})
    elif last_job_status and last_job_status not in ("complete", "needs_setup"):
        priorities.append({"level": "warning", "text": f"Last automation job {last_job['job']} finished with status {last_job_status}."})
    elif last_job_seconds is not None and last_job_seconds > stale_bot_seconds and not autopilot_disabled:
        priorities.append({"level": "warning", "text": f"Last automation run was {last_job_age} ago. Confirm the dashboard process is still running."})
    if booking_problem:
        priorities.append({"level": "danger", "text": booking_problem})
    if booking_url and not calendly_api_ready:
        priorities.append({"level": "warning", "text": "Calendly link is ready, but booking sync is not connected. Add a Calendly API token so booked meetings appear in this app."})
    if ready_needs_review:
        priorities.append({"level": "success", "text": f"{ready_needs_review} READY lead(s) are booked and need your plan review."})
    if unread_ready_alerts:
        age_text = f" Oldest is {oldest_unread_alert_age} old." if oldest_unread_alert_age else ""
        priorities.append({"level": "warning", "text": f"{unread_ready_alerts} unread READY alert(s) are waiting in Owner Alerts.{age_text}"})
    elif unread_owner_alerts:
        age_text = f" Oldest is {oldest_unread_alert_age} old." if oldest_unread_alert_age else ""
        priorities.append({"level": "info", "text": f"{unread_owner_alerts} unread owner alert(s) are waiting.{age_text}"})
    if ready_past_due:
        priorities.append({"level": "warning", "text": f"{ready_past_due} READY appointment(s) are due or past. Review these before newer bookings."})
    if invalid_ready_appointments:
        priorities.append({"level": "warning", "text": f"{invalid_ready_appointments} READY appointment date(s) could not be parsed. Open the lead and correct the appointment time."})
    if qualified_waiting:
        priorities.append({"level": "info", "text": f"{qualified_waiting} qualified lead(s) are waiting on Calendly booking."})
    if qualified_needs_link:
        priorities.append({"level": "warning", "text": f"{qualified_needs_link} qualified lead(s) still need the booking link sent."})
    if booking_followups_due:
        priorities.append({"level": "warning", "text": f"{booking_followups_due} qualified lead(s) are due for a booking reminder."})
    if stale_threads:
        priorities.append({"level": "warning", "text": f"{stale_threads} active qualifier thread(s) have not replied inside the {sla_minutes}-minute SLA."})
    if wrong_numbers:
        priorities.append({"level": "warning", "text": f"{wrong_numbers} lead(s) are marked wrong number and should be replaced or suppressed."})
    if not priorities:
        priorities.append({"level": "success", "text": "Automation looks smooth: Calendly is set, no READY backlog, and no stuck queues."})
    if booking_problem or not calendly_api_ready:
        status = "needs_setup"
    elif any(item["level"] in ("danger", "warning") for item in priorities):
        status = "needs_attention"
    else:
        status = "smooth"
    return {
        "status": status,
        "label": {
            "needs_setup": "Needs setup",
            "needs_attention": "Needs attention",
            "smooth": "Smooth",
        }[status],
        "cards": [
            {
                "label": "Setup Bot",
                "value": "Off" if autopilot_disabled else "On",
            },
            {"label": "Last Bot", "value": last_job_age or "Never"},
            {"label": "Calendly Link", "value": "Check" if booking_problem and booking_url else "Ready" if booking_url else "Missing"},
            {"label": "Calendly Sync", "value": "API" if calendly_api_ready else "Missing"},
            {"label": "Outreach", "value": "Manual"},
            {"label": "Waiting Book", "value": qualified_waiting},
            {"label": "Booking F/U", "value": booking_followups_due},
            {"label": "Ready Review", "value": ready_needs_review},
            {"label": "Owner Alerts", "value": unread_owner_alerts},
            {"label": "Next Ready", "value": appointment_label(next_ready_appointment.isoformat()) if next_ready_appointment else "-"},
            {"label": "Stale Threads", "value": stale_threads},
        ],
        "priorities": priorities,
        "last_automation": dict(last_job) if last_job else None,
    }


def test_integration(conn: sqlite3.Connection, target: str, actor: str) -> dict[str, Any]:
    target = target.lower()
    config = get_config()
    if target == "hubspot":
        token = config.get("hubspot_api_key")
        if not token:
            return {"target": target, "status": "missing", "detail": "HubSpot token is not configured."}
        request = urllib.request.Request(
            "https://api.hubapi.com/crm/v3/objects/contacts?limit=1",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                detail = response.read(300).decode("utf-8", "replace")
                status = str(response.status)
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", "replace")
            status = str(exc.code)
        except urllib.error.URLError as exc:
            detail = str(exc)
            status = "error"
        record_audit(conn, "integration.test", {"target": target, "status": status}, None, actor)
        return {"target": target, "status": status, "detail": detail}
    if target == "calendly":
        booking_url = config["settings"].get("calendar_booking_url", "")
        token = config["settings"].get("calendly_api_token", "").strip()
        if token:
            try:
                me = calendly_api_get("/users/me", token).get("resource", {})
                detail = f"Calendly API connected as {me.get('name') or me.get('email') or 'Calendly user'}."
                status = "configured"
            except urllib.error.HTTPError as exc:
                detail = exc.read(300).decode("utf-8", "replace")
                status = str(exc.code)
            except Exception as exc:
                detail = str(exc)
                status = "error"
            record_audit(conn, "integration.test", {"target": target, "status": status}, None, actor)
            return {"target": target, "status": status, "detail": detail}
        return {
            "target": target,
            "status": "link-only" if booking_url else "missing",
            "detail": (booking_url + " is set, but CALENDLY_API_TOKEN is missing so bookings cannot sync back into this local app.") if booking_url else "Calendly booking URL is not configured.",
        }
    if target in ("outreach", "manual"):
        return {
            "target": "outreach",
            "status": "manual",
            "detail": "Outreach is manual. Call or message leads personally from the lead record.",
        }
    raise ValueError("target must be calendly, hubspot, or outreach.")


def deliver_ready_lead(conn: sqlite3.Connection, lead_id: str) -> None:
    lead = refresh_compliance(conn, lead_id)
    if lead["stage"] != "READY":
        return
    compliance_status, reasons = compliance_check(lead)
    if compliance_status == "BLOCKED":
        response = "; ".join(reasons)
        conn.execute(
            "INSERT INTO delivery_logs (id, lead_id, created_at, target, status, response) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), lead_id, utcnow(), "compliance", "blocked", response),
        )
        record_audit(conn, "delivery.blocked", {"reasons": reasons}, lead_id)
        return
    config = get_config()
    status, response = "calendly-only", "READY lead uses Calendly booking workflow."
    conn.execute(
        "INSERT INTO delivery_logs (id, lead_id, created_at, target, status, response) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), lead_id, utcnow(), "calendly", status, response[:500]),
    )
    if config["hubspot_api_key"]:
        status, response = sync_hubspot_contact(config["hubspot_api_key"], lead)
    else:
        status, response = "skipped", "HUBSPOT_API_KEY is not configured"
    conn.execute(
        "INSERT INTO delivery_logs (id, lead_id, created_at, target, status, response) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), lead_id, utcnow(), "hubspot", status, response[:500]),
    )
    record_audit(conn, "lead.ready_delivered", {"calendly": bool(config["settings"].get("calendar_booking_url")), "hubspot": bool(config["hubspot_api_key"])}, lead_id)


def sync_hubspot_contact(token: str, lead: dict[str, Any]) -> tuple[str, str]:
    properties = {
        "email": lead.get("email", ""),
        "phone": lead.get("phone", ""),
        "firstname": lead.get("name", ""),
        "state": lead.get("state", ""),
        "lifecyclestage": "marketingqualifiedlead",
        "hs_lead_status": "QUALIFIED",
        "notes": (
            f"Segment: {lead.get('segment')} | Score: {lead.get('score')} | "
            f"Appointment: {lead.get('appointment_at')} | Intent: {lead.get('intent')}"
        )[:500],
    }
    payload = {"properties": {key: value for key, value in properties.items() if value}}
    request = urllib.request.Request(
        "https://api.hubapi.com/crm/v3/objects/contacts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return str(resp.status), resp.read(500).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", "replace")
        return str(exc.code), body
    except urllib.error.URLError as exc:
        return "error", str(exc)


def post_json(url: str, payload: dict[str, Any]) -> tuple[str, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return str(resp.status), resp.read(500).decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return "error", str(exc)


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    stage_counts = {stage: 0 for stage in STAGES}
    for row in conn.execute("SELECT stage, COUNT(*) count FROM leads GROUP BY stage"):
        stage_counts[row["stage"]] = row["count"]
    total = sum(stage_counts.values())
    ready = stage_counts["READY"] + stage_counts["WON"]
    avg_score = conn.execute("SELECT COALESCE(ROUND(AVG(score), 1), 0) FROM leads").fetchone()[0]
    private_prime = conn.execute("SELECT COUNT(*) FROM leads WHERE segment = 'Private-Prime'").fetchone()[0]
    return {
        "total": total,
        "stage_counts": stage_counts,
        "ready_or_won": ready,
        "avg_score": avg_score,
        "private_prime": private_prime,
        "aca_subsidy": total - private_prime,
    }


def audit_rows(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def delivery_rows(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM delivery_logs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def csv_bytes(conn: sqlite3.Connection) -> bytes:
    leads = list_leads(conn, reveal=True)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "id",
            "created_at",
            "source",
            "name",
            "phone",
            "email",
            "state",
            "county",
            "segment",
            "score",
            "stage",
            "owner",
            "assigned_to",
            "appointment_at",
            "intent",
            "notes",
        ],
    )
    writer.writeheader()
    for lead in leads:
        writer.writerow({field: lead.get(field, "") for field in writer.fieldnames})
    return buf.getvalue().encode("utf-8")


def zip_bytes(conn: sqlite3.Connection) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("leads.csv", csv_bytes(conn))
        zf.writestr("counts.json", json.dumps(counts(conn), indent=2))
        zf.writestr("audit_logs.json", json.dumps(audit_rows(conn, 500), indent=2))
        zf.writestr("delivery_logs.json", json.dumps(delivery_rows(conn, 500), indent=2))
    return buf.getvalue()


def prometheus_text(conn: sqlite3.Connection) -> str:
    data = counts(conn)
    lines = [
        "# HELP leadbot_leads_total Number of leads by stage.",
        "# TYPE leadbot_leads_total gauge",
    ]
    for stage, value in data["stage_counts"].items():
        lines.append(f'leadbot_leads_total{{stage="{stage}"}} {value}')
    lines.extend(
        [
            "# HELP leadbot_score_average Average lead score.",
            "# TYPE leadbot_score_average gauge",
            f"leadbot_score_average {data['avg_score']}",
        ]
    )
    return "\n".join(lines) + "\n"


def make_ics(lead: dict[str, Any]) -> str:
    start = parse_dt(lead["appointment_at"]) if lead["appointment_at"] else datetime.now(timezone.utc) + timedelta(days=1)
    end = start + timedelta(minutes=30)
    uid = f"{lead['id']}@health-leads-bot"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    def fmt(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Health Insurance Lead Bot//Qualifier Pipeline//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{fmt(start)}",
            f"DTEND:{fmt(end)}",
            f"SUMMARY:Health insurance consult - {escape_ics(lead['name'])}",
            f"DESCRIPTION:Segment {lead['segment']} | Score {lead['score']} | Phone {lead['phone']} | Email {lead['email']}",
            f"ATTENDEE;CN={escape_ics(lead['name'])}:MAILTO:{lead['email'] or 'lead@example.com'}",
            f"ORGANIZER:MAILTO:{get_config()['principal_email']}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


def parse_dt(value: str) -> datetime:
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def escape_ics(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def calendly_booking_url(lead: dict[str, Any]) -> str:
    base = get_config()["settings"].get("calendar_booking_url", "").strip()
    if not base:
        return ""
    parsed = urlparse(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if lead.get("name"):
        query.setdefault("name", lead["name"])
    if lead.get("email") and not str(lead["email"]).startswith("email:..."):
        query.setdefault("email", lead["email"])
    query.setdefault("hide_event_type_details", "1")
    return urlunparse(parsed._replace(query=urlencode(query)))


def booking_email_link(lead: dict[str, Any]) -> str:
    url = calendly_booking_url(lead)
    if not url or not lead.get("email"):
        return ""
    subject = quote("Book your health insurance appointment")
    body = quote(
        f"Hi {lead.get('name')},\n\n"
        "Thanks for confirming your interest. Please book your appointment here:\n\n"
        f"{url}\n\n"
        "Reply STOP to opt out."
    )
    return f"mailto:{quote(lead['email'])}?subject={subject}&body={body}"


def login_page(error: str = "") -> bytes:
    error_html = f"<p class='error'>{error}</p>" if error else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Lead Bot Login</title>
    <style>
      body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#f7f8fa; color:#1c2430; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
      form {{ width:min(420px, calc(100vw - 32px)); background:#fff; border:1px solid #d9e0e8; border-radius:8px; box-shadow:0 14px 40px rgba(30,45,70,.12); padding:24px; display:grid; gap:14px; }}
      h1 {{ margin:0; font-size:1.7rem; }}
      p {{ color:#647184; margin:0; }}
      label {{ color:#647184; display:grid; font-size:.8rem; font-weight:800; gap:6px; text-transform:uppercase; }}
      input {{ border:1px solid #d9e0e8; border-radius:8px; min-height:42px; padding:9px 10px; font:inherit; }}
      button {{ background:#1e63b6; border:0; border-radius:8px; color:white; cursor:pointer; font:inherit; font-weight:800; min-height:44px; }}
      .error {{ color:#a23a3a; font-weight:800; }}
      .hint {{ font-size:.86rem; line-height:1.45; }}
    </style>
  </head>
  <body>
    <form method="post" action="/login">
      <div>
        <h1>Lead Bot</h1>
        <p>Private owner access</p>
      </div>
      {error_html}
      <label>Username<input name="username" autocomplete="username" required /></label>
      <label>Password<input name="password" type="password" autocomplete="current-password" required /></label>
      <button type="submit">Sign In</button>
      <p class="hint">Set <strong>LEAD_BOT_USERNAME</strong>, <strong>LEAD_BOT_PASSWORD</strong>, and <strong>LEAD_BOT_WEBHOOK_SECRET</strong> before putting this online.</p>
    </form>
  </body>
</html>""".encode("utf-8")


def quote_page(error: str = "", values: dict[str, Any] | None = None) -> bytes:
    values = values or {}
    error_html = f"<p class='error'>{escape_html(error)}</p>" if error else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Health Coverage Quote</title>
    <style>
      :root {{ color-scheme: light; --ink:#1c2430; --muted:#647184; --line:#d9e0e8; --paper:#f7f8fa; --blue:#1e63b6; --green:#167a5b; --red:#a23a3a; }}
      * {{ box-sizing:border-box; }}
      body {{ margin:0; background:var(--paper); color:var(--ink); font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
      main {{ width:min(760px, calc(100vw - 28px)); margin:0 auto; padding:28px 0 42px; }}
      header {{ margin-bottom:18px; }}
      h1 {{ font-size:clamp(2rem, 7vw, 4rem); line-height:.98; margin:0 0 8px; }}
      p {{ color:var(--muted); line-height:1.5; margin:0; }}
      form {{ background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:0 14px 40px rgba(30,45,70,.12); display:grid; gap:14px; padding:20px; }}
      label {{ color:var(--muted); display:grid; font-size:.78rem; font-weight:800; gap:6px; text-transform:uppercase; }}
      input, select, textarea {{ border:1px solid var(--line); border-radius:8px; color:var(--ink); font:inherit; min-height:42px; padding:9px 10px; width:100%; }}
      textarea {{ min-height:88px; resize:vertical; }}
      .grid {{ display:grid; gap:12px; grid-template-columns:repeat(2,minmax(0,1fr)); }}
      .check {{ align-items:flex-start; display:flex; gap:10px; text-transform:none; }}
      .check input {{ min-height:auto; margin-top:3px; width:auto; }}
      button {{ background:var(--blue); border:0; border-radius:8px; color:#fff; cursor:pointer; font:inherit; font-weight:850; min-height:46px; }}
      .error {{ color:var(--red); font-weight:800; }}
      .fine {{ font-size:.85rem; }}
      @media (max-width:620px) {{ .grid {{ grid-template-columns:1fr; }} }}
    </style>
  </head>
  <body>
    <main>
      <header>
        <h1>Find Health Coverage</h1>
        <p>Answer a few basics and Ariel's automated qualifier will follow up to help route you to a plan review.</p>
      </header>
      <form method="post" action="/quote">
        {error_html}
        <label>Name<input name="name" required value="{escape_attr(values.get('name', ''))}" /></label>
        <div class="grid">
          <label>Phone<input name="phone" required inputmode="tel" value="{escape_attr(values.get('phone', ''))}" /></label>
          <label>Email<input name="email" type="email" value="{escape_attr(values.get('email', ''))}" /></label>
        </div>
        <div class="grid">
          <label>State<input name="state" maxlength="2" required value="{escape_attr(values.get('state', ''))}" /></label>
          <label>County<input name="county" value="{escape_attr(values.get('county', ''))}" /></label>
        </div>
        <div class="grid">
          <label>Age<input name="age" type="number" min="18" max="99" value="{escape_attr(values.get('age', ''))}" /></label>
          <label>Household Size<input name="household_size" type="number" min="1" max="12" value="{escape_attr(values.get('household_size', '1'))}" /></label>
        </div>
        <div class="grid">
          <label>Annual Income<input name="annual_income" type="number" min="0" step="1000" value="{escape_attr(values.get('annual_income', ''))}" /></label>
          <label>Current Coverage
            <select name="has_current_coverage">
              <option value="">Select</option>
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
          </label>
        </div>
        <label>What are you looking for?<textarea name="intent" required placeholder="Example: private plan, family coverage, self-employed, need coverage next month...">{escape_html(values.get('intent', ''))}</textarea></label>
        <label class="check"><input type="checkbox" name="healthy" value="yes" /> Generally healthy</label>
        <label class="check"><input type="checkbox" name="tcpa_consent" value="yes" required /> I agree to be contacted by call/text/email about health insurance options. Reply STOP to opt out.</label>
        <input type="hidden" name="source" value="Public Quote Page" />
        <input type="hidden" name="consent_text" value="I agree to be contacted by call/text/email about health insurance options. Reply STOP to opt out." />
        <button type="submit">Request Quote</button>
        <p class="fine">This is not a policy application. A licensed agent may review options with you if there is a fit.</p>
      </form>
    </main>
  </body>
</html>""".encode("utf-8")


def quote_success_page(lead: dict[str, Any]) -> bytes:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Quote Request Received</title>
    <style>
      body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#f7f8fa; color:#1c2430; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
      section {{ width:min(560px, calc(100vw - 32px)); background:#fff; border:1px solid #d9e0e8; border-radius:8px; box-shadow:0 14px 40px rgba(30,45,70,.12); padding:24px; }}
      h1 {{ margin:0 0 8px; font-size:2rem; }}
      p {{ color:#647184; line-height:1.5; margin:0; }}
      strong {{ color:#167a5b; }}
    </style>
  </head>
  <body>
    <section>
      <h1>Request received</h1>
      <p><strong>{escape_html(lead.get('name', 'Your request'))}</strong> is in the qualifier pipeline. Watch for the next text or email so we can finish the intake and get you booked.</p>
    </section>
  </body>
</html>""".encode("utf-8")


def escape_html(value: Any) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#039;")


def escape_attr(value: Any) -> str:
    return escape_html(value)


class LeadBotHandler(BaseHTTPRequestHandler):
    server_version = "HealthLeadBot/1.0"

    def do_GET(self) -> None:
        self.route()

    def do_POST(self) -> None:
        self.route()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def route(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path in ("/login", "/dashboard", "/logout") and not self.is_admin_access_allowed():
                raise KeyError("Route not found.")
            if self.command == "GET" and path == "/login":
                return self.bytes_response(login_page(), "text/html; charset=utf-8")
            if self.command == "POST" and path == "/login":
                return self.handle_login()
            if self.command == "GET" and path == "/quote":
                return self.bytes_response(quote_page(), "text/html; charset=utf-8")
            if self.command == "POST" and path == "/quote":
                return self.handle_quote_submit()
            if self.command == "GET" and path == "/":
                return self.serve_file(STATIC_DIR / "site.html", "text/html; charset=utf-8")
            if path == "/logout":
                return self.handle_logout()
            if self.command == "GET" and path == "/favicon.ico":
                return self.serve_file(ASSETS_DIR / "ariel-vahnish-health-solutions-logo.png", "image/png")
            if self.command == "GET" and path.startswith("/static/"):
                return self.serve_file(STATIC_DIR / path.removeprefix("/static/"))
            if self.command == "GET" and path.startswith("/assets/"):
                return self.serve_file(ASSETS_DIR / path.removeprefix("/assets/"))
            if self.command == "GET" and path == "/health":
                return self.json_response({"ok": True, "service": "health-leads-bot", "time": utcnow()})
            if self.command == "GET" and path == "/health/details":
                with open_db() as conn:
                    lead_total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
                    setting_count = conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
                return self.json_response(
                    {
                        "ok": True,
                        "service": "health-leads-bot",
                        "time": utcnow(),
                        "database": "ok",
                        "leads": lead_total,
                        "settings": setting_count,
                        "calendly_link": bool(get_config()["settings"].get("calendar_booking_url")),
                        "calendly_sync": bool(calendly_token()),
                        "outreach": "manual",
                    }
                )
            if self.command == "GET" and path == "/dashboard":
                if not self.is_authenticated_request():
                    return self.redirect("/login")
                return self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if path.startswith("/api/") or path in ("/metrics/prom", "/leads.csv", "/leads/download.zip", "/leads/counts"):
                if not self.is_public_route(path) and not self.is_authenticated_request():
                    raise PermissionError("Login required.")
                return self.handle_api(path, parse_qs(parsed.query))
            self.error_response(HTTPStatus.NOT_FOUND, "Route not found.")
        except ValueError as exc:
            self.error_response(HTTPStatus.BAD_REQUEST, str(exc))
        except KeyError as exc:
            self.error_response(HTTPStatus.NOT_FOUND, str(exc).strip("'"))
        except PermissionError as exc:
            self.error_response(HTTPStatus.UNAUTHORIZED, str(exc))
        except Exception as exc:
            self.error_response(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unexpected error: {exc}")

    def handle_api(self, path: str, query: dict[str, list[str]]) -> None:
        with open_db() as conn:
            if self.command == "GET" and path == "/api/leads":
                stage = query.get("stage", [None])[0]
                reveal = self.has_role("principal") or self.has_role("qualifier")
                return self.json_response({"leads": list_leads(conn, stage, reveal=reveal)})
            if self.command == "POST" and path == "/api/leads":
                self.require_role("qualifier")
                payload = self.body_json()
                lead = create_lead(conn, payload, self.actor(), self.client_address[0], self.headers.get("User-Agent", ""))
                conn.commit()
                return self.json_response({"lead": lead}, HTTPStatus.CREATED)
            if self.command == "POST" and path == "/api/dnc":
                self.require_role("qualifier")
                result = add_dnc(conn, self.body_json(), self.actor())
                conn.commit()
                return self.json_response(result, HTTPStatus.CREATED)
            if self.command == "POST" and path == "/api/opt-out":
                result = opt_out(conn, self.body_json(), self.actor())
                conn.commit()
                return self.json_response(result)
            if self.command == "GET" and path == "/api/settings":
                self.require_any_role()
                return self.json_response({"settings": load_settings()})
            if self.command == "POST" and path == "/api/settings":
                self.require_role("principal")
                result = save_settings(conn, self.body_json(), self.actor())
                conn.commit()
                return self.json_response({"settings": result})
            webhook_route = re.match(r"^/api/webhooks/([a-zA-Z0-9_-]+)$", path)
            if self.command == "POST" and webhook_route:
                provider = webhook_route.group(1)
                payload = parse_request_payload(self)
                if provider.lower() == "calendly":
                    result = handle_calendly_webhook(conn, payload, self.actor())
                    conn.execute(
                        "INSERT INTO webhook_logs (id, created_at, provider, status, lead_id, payload, response) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), utcnow(), provider, result["status"], result["lead"]["id"], json.dumps(payload), "calendly processed"),
                    )
                else:
                    result = ingest_webhook(
                        conn,
                        provider,
                        payload,
                        self.actor(),
                        self.client_address[0],
                        self.headers.get("User-Agent", ""),
                    )
                conn.commit()
                return self.json_response(result, HTTPStatus.CREATED)
            if self.command == "POST" and path in ("/api/thread", "/api/sms/thread"):
                self.require_any_role()
                payload = self.body_json()
                lead_id = str(payload.get("lead_id") or "") or find_lead_by_phone_or_email(
                    conn,
                    str(payload.get("phone") or payload.get("from") or payload.get("to") or ""),
                    str(payload.get("email") or ""),
                )
                if not lead_id:
                    raise ValueError("No matching lead found for thread message.")
                message = add_conversation_message(conn, lead_id, payload, self.actor())
                if message["direction"] == "inbound":
                    capture_qualifier_answer(conn, lead_id, message["body"], self.actor())
                    run_auto_qualifier_for_lead(conn, lead_id)
                conn.commit()
                return self.json_response({"message": message}, HTTPStatus.CREATED)
            if self.command == "GET" and path == "/api/dashboard":
                self.require_any_role()
                return self.json_response(
                    {
                        "counts": counts(conn),
                        "leads": list_leads(conn, reveal=True),
                        "audit": audit_rows(conn),
                        "deliveries": delivery_rows(conn),
                        "source_metrics": source_metrics(conn),
                        "settings": get_config()["settings"],
                        "notifications": owner_notifications(conn, unread_only=True),
                        "automation_health": automation_health(conn),
                    }
                )
            if self.command == "GET" and path == "/api/source-metrics":
                self.require_any_role()
                return self.json_response({"sources": source_metrics(conn)})
            if self.command == "POST" and path == "/api/automation/run":
                self.require_role("principal")
                result = run_daily_ops(conn, self.actor())
                conn.commit()
                return self.json_response(result)
            if self.command == "POST" and path == "/api/automation/qualifier/run":
                self.require_role("principal")
                result = run_auto_qualifier(conn, self.actor())
                conn.commit()
                return self.json_response(result)
            if self.command == "POST" and path == "/api/automation/setup/run":
                self.require_role("principal")
                result = run_setup_autopilot(conn, self.actor())
                conn.commit()
                return self.json_response(result)
            if self.command == "POST" and path == "/api/automation/bots/run":
                self.require_role("principal")
                result = run_bot_team(conn, self.actor())
                conn.commit()
                return self.json_response(result)
            if self.command == "POST" and path == "/api/calendly/sync":
                self.require_role("principal")
                result = calendly_sync_bookings(conn, self.actor())
                conn.commit()
                return self.json_response(result)
            if self.command == "GET" and path == "/api/notifications":
                self.require_role("principal")
                return self.json_response({"notifications": owner_notifications(conn)})
            notification_route = re.match(r"^/api/notifications/([^/]+)/read$", path)
            if self.command == "POST" and notification_route:
                self.require_role("principal")
                conn.execute("UPDATE owner_notifications SET read_at = ? WHERE id = ?", (utcnow(), unquote(notification_route.group(1))))
                conn.commit()
                return self.json_response({"ok": True})
            if self.command == "POST" and path == "/api/test-run":
                self.require_role("principal")
                result = run_test_lead_flow(conn, self.actor())
                conn.commit()
                return self.json_response(result, HTTPStatus.CREATED)
            if self.command == "POST" and path == "/api/ai/chat":
                self.require_role("principal")
                result = ai_chat(conn, self.body_json(), self.actor())
                conn.commit()
                return self.json_response(result)
            if self.command == "GET" and path == "/api/setup/status":
                self.require_role("principal")
                return self.json_response(setup_status(conn))
            integration_route = re.match(r"^/api/integrations/([a-zA-Z0-9_-]+)/test$", path)
            if self.command == "POST" and integration_route:
                self.require_role("principal")
                result = test_integration(conn, integration_route.group(1), self.actor())
                conn.commit()
                return self.json_response(result)
            if self.command == "GET" and path == "/api/compliance/export.zip":
                self.require_role("principal")
                return self.bytes_response(compliance_export_bytes(conn), "application/zip", "compliance-export.zip")
            if self.command == "GET" and path == "/api/backup.zip":
                self.require_role("principal")
                return self.bytes_response(backup_bytes(conn), "application/zip", "lead-bot-backup.zip")
            if self.command == "GET" and path == "/leads/counts":
                return self.json_response(counts(conn))
            if self.command == "GET" and path == "/metrics/prom":
                return self.bytes_response(prometheus_text(conn).encode("utf-8"), "text/plain; version=0.0.4")
            if self.command == "GET" and path == "/leads.csv":
                self.require_role("principal")
                return self.bytes_response(csv_bytes(conn), "text/csv; charset=utf-8", "leads.csv")
            if self.command == "GET" and path == "/leads/download.zip":
                self.require_role("principal")
                return self.bytes_response(zip_bytes(conn), "application/zip", "lead-bot-export.zip")

            lead_route = re.match(r"^/api/leads/([^/]+)(?:/(.+))?$", path)
            if lead_route:
                lead_id = unquote(lead_route.group(1))
                action = lead_route.group(2) or ""
                if self.command == "GET" and not action:
                    self.require_any_role()
                    return self.json_response({"lead": get_lead(conn, lead_id, reveal=True)})
                if self.command == "POST" and action == "assign":
                    self.require_role("qualifier")
                    lead = assign_lead(conn, lead_id, self.body_json(), self.actor())
                    conn.commit()
                    return self.json_response({"lead": lead})
                if self.command == "POST" and action == "qualify":
                    self.require_role("qualifier")
                    payload = self.body_json()
                    if any(key in payload for key in ("coverage_for", "plan_start_timing", "needs_dental_vision", "conditions_meds", "dob", "height", "weight", "annualized_income_text")):
                        save_qualification_intake(conn, lead_id, payload, self.actor())
                    lead = qualify_lead(conn, lead_id, payload, self.actor())
                    conn.commit()
                    return self.json_response({"lead": lead})
                if self.command == "POST" and action == "qualification-intake":
                    self.require_role("qualifier")
                    lead = save_qualification_intake(conn, lead_id, self.body_json(), self.actor())
                    conn.commit()
                    return self.json_response({"lead": lead})
                if self.command == "POST" and action == "stage/ready":
                    self.require_role("qualifier")
                    lead = set_stage(conn, lead_id, "READY", self.actor(), self.body_json())
                    conn.commit()
                    return self.json_response({"lead": lead})
                if self.command == "POST" and action == "stage/result":
                    self.require_role("principal")
                    payload = self.body_json()
                    result = str(payload.get("result") or "").upper()
                    if result not in ("WON", "LOST"):
                        raise ValueError("result must be WON or LOST.")
                    lead = set_stage(conn, lead_id, result, self.actor(), payload)
                    conn.commit()
                    return self.json_response({"lead": lead})
                if self.command == "GET" and action == "calendar/ics":
                    self.require_any_role()
                    lead = get_lead(conn, lead_id, reveal=True)
                    return self.bytes_response(
                        make_ics(lead).encode("utf-8"),
                        "text/calendar; charset=utf-8",
                        f"{lead['name'].replace(' ', '-')}-consult.ics",
                    )
                if self.command == "GET" and action == "booking-link":
                    self.require_any_role()
                    lead = get_lead(conn, lead_id, reveal=True)
                    return self.json_response(
                        {
                            "booking_url": calendly_booking_url(lead),
                            "email_url": booking_email_link(lead),
                        }
                    )
                if self.command == "POST" and action in ("booking-note", "booking-sms"):
                    self.require_role("qualifier")
                    result = send_booking_link(conn, lead_id, self.actor())
                    conn.commit()
                    return self.json_response(result, HTTPStatus.CREATED)
                if self.command == "GET" and action == "booking":
                    self.require_any_role()
                    lead = get_lead(conn, lead_id, reveal=True)
                    url = calendly_booking_url(lead)
                    if not url:
                        raise ValueError("Booking URL is not configured.")
                    record_audit(conn, "booking.opened", {"url": url}, lead_id, self.actor())
                    conn.commit()
                    return self.redirect(url)
                if self.command == "GET" and action == "conversation":
                    self.require_any_role()
                    return self.json_response({"messages": conversation_rows(conn, lead_id)})
                if self.command == "POST" and action == "conversation":
                    self.require_role("qualifier")
                    message = add_conversation_message(conn, lead_id, self.body_json(), self.actor())
                    if message["direction"] == "inbound":
                        capture_qualifier_answer(conn, lead_id, message["body"], self.actor())
                        run_auto_qualifier_for_lead(conn, lead_id)
                    conn.commit()
                    return self.json_response({"message": message}, HTTPStatus.CREATED)
                if self.command == "POST" and action == "phone-status":
                    self.require_role("qualifier")
                    payload = self.body_json()
                    lead = set_phone_status(conn, lead_id, str(payload.get("status") or ""), self.actor())
                    conn.commit()
                    return self.json_response({"lead": lead})
            self.error_response(HTTPStatus.NOT_FOUND, "API route not found.")

    def handle_login(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            payload = json.loads(raw or "{}")
        else:
            form = parse_qs(raw)
            payload = {key: values[0] for key, values in form.items()}
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not verify_owner(username, password):
            with open_db() as conn:
                record_audit(conn, "auth.failed", {"username": username, "ip": self.client_address[0]}, None, "anonymous")
                conn.commit()
            return self.bytes_response(login_page("Invalid username or password."), "text/html; charset=utf-8", status=HTTPStatus.UNAUTHORIZED)
        with open_db() as conn:
            token = create_session(conn, username, self.client_address[0], self.headers.get("User-Agent", ""))
            conn.commit()
        self.send_response(HTTPStatus.SEE_OTHER.value)
        self.send_header("Location", "/dashboard")
        self.send_header("Set-Cookie", self.session_cookie(token))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def handle_quote_submit(self) -> None:
        payload = parse_request_payload(self)
        payload["source"] = payload.get("source") or "Public Quote Page"
        payload["healthy"] = str(payload.get("healthy") or "").lower() in ("1", "true", "yes", "on")
        payload["has_current_coverage"] = str(payload.get("has_current_coverage") or "").lower() in ("1", "true", "yes", "on")
        payload["tcpa_consent"] = str(payload.get("tcpa_consent") or "").lower() in ("1", "true", "yes", "on")
        if payload["tcpa_consent"] and not str(payload.get("consent_text") or "").strip():
            payload["consent_text"] = "I agree to be contacted by call/text/email about health insurance options. Reply STOP to opt out."
        payload["dnc_checked"] = True
        try:
            with open_db() as conn:
                lead = create_lead(conn, payload, "public-quote", self.client_address[0], self.headers.get("User-Agent", ""))
                conn.commit()
            self.bytes_response(quote_success_page(lead), "text/html; charset=utf-8", status=HTTPStatus.CREATED)
        except Exception as exc:
            self.bytes_response(quote_page(str(exc), payload), "text/html; charset=utf-8", status=HTTPStatus.BAD_REQUEST)

    def handle_logout(self) -> None:
        token = self.cookie_value(AUTH_COOKIE)
        with open_db() as conn:
            destroy_session(conn, token or "", self.actor())
            conn.commit()
        self.send_response(HTTPStatus.SEE_OTHER.value)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", self.expired_session_cookie())
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        return parse_json(self.rfile.read(length))

    def actor(self) -> str:
        with open_db() as conn:
            user = session_user(conn, self.cookie_value(AUTH_COOKIE))
        return self.headers.get("X-Actor") or user or "local-user"

    def has_role(self, role: str) -> bool:
        key = self.headers.get("X-API-Key", "")
        config = get_config()
        if role == "qualifier":
            return hmac.compare_digest(key, config["qualifier_api_key"]) or self.is_authenticated_request()
        if role == "principal":
            return hmac.compare_digest(key, config["principal_api_key"]) or self.is_authenticated_request()
        return False

    def require_role(self, role: str) -> None:
        if not self.has_role(role):
            raise PermissionError(f"{role.title()} API key required.")

    def require_any_role(self) -> None:
        if not (self.has_role("qualifier") or self.has_role("principal")):
            raise PermissionError("Qualifier or Principal API key required.")

    def is_local_dashboard(self) -> bool:
        host = self.client_address[0]
        return host in ("127.0.0.1", "::1", "localhost")

    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        return self.client_address[0]

    def is_admin_access_allowed(self) -> bool:
        return self.client_ip() in admin_allowed_ips()

    def is_authenticated_request(self) -> bool:
        if not self.is_admin_access_allowed():
            return False
        if self.headers.get("X-API-Key") in (get_config()["qualifier_api_key"], get_config()["principal_api_key"]):
            return True
        with open_db() as conn:
            return bool(session_user(conn, self.cookie_value(AUTH_COOKIE)))

    def is_webhook_authorized(self) -> bool:
        supplied = self.headers.get("X-Webhook-Secret") or self.headers.get("X-Lead-Bot-Secret")
        if not supplied:
            parsed = urlparse(self.path)
            supplied = parse_qs(parsed.query).get("secret", [""])[0]
        return bool(supplied) and hmac.compare_digest(supplied, webhook_secret())

    def is_public_route(self, path: str) -> bool:
        if path in ("/", "/login", "/quote", "/health", "/health/details"):
            return True
        if path == "/favicon.ico" or path.startswith("/static/") or path.startswith("/assets/"):
            return True
        if path.startswith("/api/webhooks/"):
            return self.is_webhook_authorized()
        return False

    def cookie_value(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == name:
                return value
        return None

    def session_cookie(self, token: str) -> str:
        secure = "; Secure" if os.getenv("LEAD_BOT_COOKIE_SECURE", "").lower() in ("1", "true", "yes") else ""
        return f"{AUTH_COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict{secure}"

    def expired_session_cookie(self) -> str:
        secure = "; Secure" if os.getenv("LEAD_BOT_COOKIE_SECURE", "").lower() in ("1", "true", "yes") else ""
        return f"{AUTH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER.value)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def serve_file(self, path: Path, content_type: str | None = None) -> None:
        path = path.resolve()
        allowed_roots = (STATIC_DIR.resolve(), ASSETS_DIR.resolve())
        if not any(path == root or root in path.parents for root in allowed_roots):
            return self.error_response(HTTPStatus.FORBIDDEN, "Forbidden.")
        if not path.exists() or not path.is_file():
            return self.error_response(HTTPStatus.NOT_FOUND, "File not found.")
        if content_type is None:
            content_type = guess_type(path)
        self.bytes_response(path.read_bytes(), content_type)

    def json_response(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        self.bytes_response(json.dumps(payload, indent=2).encode("utf-8"), "application/json; charset=utf-8", status=status)

    def bytes_response(
        self,
        payload: bytes,
        content_type: str,
        filename: str | None = None,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Date", formatdate(timeval=None, localtime=False, usegmt=True))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{quote(filename)}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def error_response(self, status: HTTPStatus, message: str) -> None:
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def guess_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".png": "image/png",
        ".pdf": "application/pdf",
        ".json": "application/json; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
    }.get(suffix, "application/octet-stream")


def background_autopilot_loop() -> None:
    while True:
        try:
            result = run_background_autopilot_once()
            setup = result.get("setup", {})
            summary = {
                "setup_needs": setup.get("needs", []),
                "calendly_synced": result.get("calendly", {}).get("synced", 0),
                "qualifier_processed": result.get("qualifier", {}).get("processed", 0),
                "qualifier_followups": result.get("qualifier_followup", {}).get("sent", 0),
                "booking_followups": result.get("booking_followup", {}).get("sent", 0),
                "source_insights": len(result.get("source_intelligence", {}).get("insights", [])),
            }
            print(f"Bot team: {json.dumps(summary, sort_keys=True)}")
        except Exception as exc:
            print(f"Bot team error: {exc}")
        time.sleep(max(AUTOPILOT_INTERVAL_SECONDS, 15))


def start_background_autopilot() -> None:
    global _BACKGROUND_AUTOPILOT_STARTED
    if _BACKGROUND_AUTOPILOT_STARTED:
        return
    if os.getenv("LEAD_BOT_AUTOPILOT_DISABLED", "").lower() in ("1", "true", "yes"):
        return
    _BACKGROUND_AUTOPILOT_STARTED = True
    thread = threading.Thread(target=background_autopilot_loop, name="setup-autopilot", daemon=True)
    thread.start()


def run(port: int = DEFAULT_PORT) -> None:
    init_db()
    start_background_autopilot()
    server = ThreadingHTTPServer(("127.0.0.1", port), LeadBotHandler)
    print(f"Health Insurance Lead Bot running at http://127.0.0.1:{port}/dashboard")
    server.serve_forever()


if __name__ == "__main__":
    run()
