from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs
from urllib.request import Request, urlopen


FIELD_LABELS = {
    "name": "Name",
    "phone": "Phone",
    "email": "Email",
    "state": "State",
    "age": "Age",
    "household_size": "Household",
    "intent": "Details",
    "healthy": "Generally healthy",
    "tcpa_consent": "TCPA consent",
    "source": "Source",
}


def parse_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1].strip() if values else "" for key, values in parsed.items()}


def lead_body(data: dict[str, str]) -> str:
    lines = ["New quote request from ArielHealthSolutions.com", ""]
    for key, label in FIELD_LABELS.items():
        value = data.get(key, "")
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def send_resend_email(data: dict[str, str]) -> bool:
    api_key = os.getenv("RESEND_API_KEY", "")
    to_email = os.getenv("LEAD_NOTIFY_EMAIL", "")
    from_email = os.getenv("LEAD_FROM_EMAIL", "Ariel Health Solutions <onboarding@resend.dev>")
    if not api_key or not to_email:
        return False

    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": f"New quote request: {data.get('name', 'Website lead')}",
        "text": lead_body(data),
    }
    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return 200 <= response.status < 300


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", "0") or 0)
        data = parse_form(self.rfile.read(length))
        data["state"] = data.get("state", "").upper()

        print(json.dumps({"event": "quote_request", "lead": data}, sort_keys=True))

        try:
            send_resend_email(data)
        except Exception as exc:
            print(json.dumps({"event": "quote_email_error", "error": str(exc)}))

        self.send_response(303)
        self.send_header("Location", "/thank-you.html")
        self.end_headers()

    def do_GET(self):
        self.send_response(405)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Method not allowed")
