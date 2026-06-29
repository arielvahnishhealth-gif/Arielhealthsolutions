import os

try:
    from hubspot import HubSpot
    from hubspot.crm.contacts import SimplePublicObjectInput
except ImportError:  # pragma: no cover - optional integration
    HubSpot = None
    SimplePublicObjectInput = None

from app.db import session_scope
from app.models import Lead, Qualification


def _fmt_person(person: dict | None) -> str:
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
    return ", ".join([bit for bit in bits if bit])


def sync_new_leads(min_score: float = 2.5) -> None:
    key = os.getenv("HUBSPOT_API_KEY")
    if not key or HubSpot is None or SimplePublicObjectInput is None:
        return
    api = HubSpot(access_token=key)
    with session_scope() as session:
        leads = (
            session.query(Lead)
            .filter(Lead.score >= min_score, Lead.converted == False)  # noqa: E712
            .limit(200)
        )
        for lead in leads:
            qualification = session.query(Qualification).filter_by(lead_id=lead.id).one_or_none()
            qualification_note = ""
            if qualification:
                qualification_note = (
                    f"\n\nQUALIFYING:\nZIP {qualification.zip or '-'} | "
                    f"Income {qualification.income or '-'} | Start {qualification.start_date or '-'}\n"
                    f"Primary: {_fmt_person(qualification.primary)}\n"
                    f"Spouse: {_fmt_person(qualification.spouse)}\n"
                    f"Child1: {_fmt_person(qualification.child1)}\n"
                    f"Child2: {_fmt_person(qualification.child2)}\n"
                    f"Child3: {_fmt_person(qualification.child3)}"
                )
            props = {
                "firstname": lead.name or (lead.handle or ""),
                "email": lead.email or "",
                "phone": lead.phone or "",
                "website": lead.url,
                "lifecyclestage": "marketingqualifiedlead",
                "notes": ((lead.message or "")[:300] + qualification_note)[:500],
                "hs_lead_status": "NEW",
            }
            api.crm.contacts.basic_api.create(SimplePublicObjectInput(properties=props))
            lead.converted = True
            session.add(lead)
