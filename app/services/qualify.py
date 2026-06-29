from types import SimpleNamespace

from app.compat import dump_model
from app.db import session_scope
from app.models import Lead, Qualification
from app.services.alerts import notify


def create_or_update(lead_id: int, payload):
    with session_scope() as session:
        qualification = session.query(Qualification).filter_by(lead_id=lead_id).one_or_none()
        if not qualification:
            qualification = Qualification(lead_id=lead_id)
        data = dump_model(payload, exclude_none=True)
        for key, value in data.items():
            setattr(qualification, key, value)
        session.add(qualification)
        session.flush()
        return {"id": qualification.id, "lead_id": qualification.lead_id}


def _person_line(label: str, person: dict | None) -> str:
    if not person:
        return ""
    bits = []
    if person.get("dob"):
        bits.append(f"DOB {person['dob']}")
    if person.get("height_in"):
        bits.append(f"H {person['height_in']}in")
    if person.get("weight_lb"):
        bits.append(f"W {person['weight_lb']}lb")
    if "smoker" in person:
        bits.append("Smoker" if person["smoker"] else "Non-smoker")
    if person.get("hbp_hc"):
        bits.append("HBP/HC")
    if "meds" in person:
        bits.append(f"Meds:{'Y' if person['meds'] else 'N'}")
    if "surgeries" in person:
        bits.append(f"Surg:{'Y' if person['surgeries'] else 'N'}")
    if not bits:
        return ""
    return f"*{label}:* " + ", ".join([bit for bit in bits if bit])


def summary_for_owner_alert(lead_id: int) -> None:
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        qualification = session.query(Qualification).filter_by(lead_id=lead_id).one_or_none()
        if not (lead and qualification):
            return
        lines = [
            f"Qualified lead [{lead.platform}] score {lead.score:.1f}",
            f"Name: {qualification.name or lead.handle or '-'} | ZIP: {qualification.zip or '-'}",
            _person_line("Primary", qualification.primary),
            _person_line("Spouse", qualification.spouse),
            _person_line("Child1", qualification.child1),
            _person_line("Child2", qualification.child2),
            _person_line("Child3", qualification.child3),
            (
                f"Income: {qualification.income or '-'} | Start: {qualification.start_date or '-'} | "
                f"Carrier: {qualification.carrier or '-'} | Rate: {qualification.rate or '-'}"
            ),
            f"{lead.url}",
        ]
        message = "\n".join([line for line in lines if line.strip()])

        notify(
            SimpleNamespace(
                platform="qualifier",
                score=lead.score,
                url=lead.url,
                message=message,
                keywords=[],
            )
        )
