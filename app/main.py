import os
from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue

from app.compat import dump_model
from app.db import init_db
from app.db import session_scope
from app.jobs import reddit, score, serp, trends, twitter
from app.models import Lead
from app.schema import QualifyIn
from app.services.hubspot import sync_new_leads
from app.services.qualify import create_or_update, summary_for_owner_alert


app = FastAPI(title="Health Insurance Lead Bot")
templates = Jinja2Templates(directory="app/templates")


def get_queue() -> Queue:
    redis = Redis.from_url(os.environ["REDIS_URL"])
    redis.ping()
    return Queue("lead_queue", connection=redis)


def enqueue_or_run(func: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        job = get_queue().enqueue(func, *args, **kwargs)
        return {"status": "queued", "job_id": job.id}
    except RedisError:
        result = func(*args, **kwargs)
        return {"status": "ran-local", "result": result}


def lead_to_dict(lead: Lead) -> dict[str, Any]:
    return {
        "id": lead.id,
        "name": lead.name,
        "handle": lead.handle,
        "platform": lead.platform,
        "url": lead.url,
        "message": lead.message,
        "location": lead.location,
        "email": lead.email,
        "phone": lead.phone,
        "keywords": lead.keywords or [],
        "score": lead.score,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "seen_at": lead.seen_at.isoformat() if lead.seen_at else None,
        "converted": bool(lead.converted),
    }


class IngestPayload(BaseModel):
    name: str | None = None
    handle: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    keywords: list[str] = Field(default_factory=list)
    message: str | None = None
    source: str = "web"
    url: str


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/ingest")
def ingest(payload: IngestPayload) -> dict[str, Any]:
    return enqueue_or_run(score.process_manual_lead, dump_model(payload))


@app.post("/run/listeners")
def run_listeners(background: BackgroundTasks) -> dict[str, Any]:
    try:
        q = get_queue()
    except RedisError:
        background.add_task(reddit.run_once)
        background.add_task(serp.run_once)
        background.add_task(trends.run_once)
        if os.getenv("TWITTER_BEARER_TOKEN"):
            background.add_task(twitter.run_once)
        return {"status": "ran-local"}
    background.add_task(q.enqueue, reddit.run_once)
    background.add_task(q.enqueue, serp.run_once)
    background.add_task(q.enqueue, trends.run_once)
    if os.getenv("TWITTER_BEARER_TOKEN"):
        background.add_task(q.enqueue, twitter.run_once)
    return {"status": "queued"}


@app.post("/sync/crm")
def sync_crm() -> dict[str, Any]:
    return enqueue_or_run(sync_new_leads)


@app.get("/leads")
def leads(limit: int = 100) -> dict[str, list[dict[str, Any]]]:
    with session_scope() as session:
        rows = session.query(Lead).order_by(Lead.score.desc(), Lead.created_at.desc()).limit(limit).all()
        return {"leads": [lead_to_dict(lead) for lead in rows]}


@app.get("/qualify/form/{lead_id}", response_class=HTMLResponse)
def qualify_form(request: Request, lead_id: int) -> HTMLResponse:
    return templates.TemplateResponse(
        "qualify_form.html", {"request": request, "lead_id": lead_id}
    )


@app.post("/qualify/{lead_id}")
def qualify_submit(lead_id: int, payload: QualifyIn) -> dict[str, int | bool]:
    create_or_update(lead_id, payload)
    summary_for_owner_alert(lead_id)
    return {"ok": True, "qualified_lead_id": lead_id}
