import os
import smtplib
from email.message import EmailMessage


def notify(lead) -> None:
    if os.getenv("SMTP_HOST"):
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[LeadBot] New {getattr(lead, 'platform', 'lead')} {lead.score:.1f}"
            msg["From"] = os.environ["ALERT_EMAIL_FROM"]
            msg["To"] = os.environ["ALERT_EMAIL_TO"]
            msg.set_content(
                f"{lead.url}\n\n{getattr(lead, 'message', '')}\n\n"
                f"Keywords: {getattr(lead, 'keywords', [])}"
            )
            with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
                server.starttls()
                server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                server.send_message(msg)
        except Exception:
            pass
