from __future__ import annotations

import hmac
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
import pytz
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
CAL_SECRET = os.getenv("CAL_SECRET", "changeme").encode()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DB = os.getenv("NOTION_DB")
ZAPI_INSTANCE = os.getenv("ZAPI_INSTANCE")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
ADMIN_PHONES = [p.strip() for p in os.getenv("ADMIN_PHONES", "").split(",") if p]
TZ = pytz.timezone(os.getenv("TZ", "America/Sao_Paulo"))

HEADERS_NOTION = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

ZAPI_BASE = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"

# -----------------------------------------------------------------------------
# FastAPI app & scheduler
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Cal.com → Notion + WhatsApp Integration",
    description="Integration service that receives Cal.com webhooks and syncs with Notion and WhatsApp",
    version="1.0.0"
)

scheduler = AsyncIOScheduler()
scheduler.start()

# -----------------------------------------------------------------------------
# Pydantic models for webhook parsing (simplified)
# -----------------------------------------------------------------------------
class Attendee(BaseModel):
    name: str
    email: Optional[str] | None = None
    phone: Optional[str] | None = None


class Booking(BaseModel):
    start_time: str = Field(..., alias="startTime")
    end_time: str = Field(..., alias="endTime")
    attendees: List[Attendee]
    uid: str


class CalWebhookPayload(BaseModel):
    trigger_event: str = Field(..., alias="triggerEvent")
    payload: Booking


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def verify_signature(signature_header: str | None, raw_body: bytes) -> None:
    if not signature_header:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing signature")

    digest = hmac.new(CAL_SECRET, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, signature_header):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")


def format_pt_br(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y - %I:%M%p").lower().replace("am", "am").replace("pm", "pm")


def notion_find_page(email: str | None, phone: str | None) -> Optional[str]:
    if not (email or phone):
        return None

    filters = []
    if email:
        filters.append({"property": "Email", "email": {"equals": email}})
    if phone:
        filters.append({"property": "Telefone", "phone_number": {"equals": phone}})

    # Combine filters with OR if both present
    if len(filters) == 2:
        filter_json = {"or": filters}
    else:
        filter_json = filters[0]

    resp = httpx.post(
        f"https://api.notion.com/v1/databases/{NOTION_DB}/query",
        headers=HEADERS_NOTION,
        json={"filter": filter_json},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


def notion_update_datetime(page_id: str, when: str) -> None:
    payload = {
        "properties": {
            "Data Agendada pelo Lead": {
                "rich_text": [{"text": {"content": when}}]
            }
        }
    }
    resp = httpx.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS_NOTION,
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()


def send_wa_message(phone: str, message: str) -> None:
    payload = {"phone": phone, "message": message}
    httpx.post(f"{ZAPI_BASE}/send-message", json=payload, timeout=15)


def schedule_messages(first_name: str, meeting_dt: datetime) -> None:
    meeting_str = meeting_dt.strftime("%H:%M")

    # 1 day before
    scheduler.add_job(
        send_wa_bulk,
        trigger=DateTrigger(run_date=meeting_dt - timedelta(days=1)),
        args=[f"Olá {first_name}, amanhã temos nossa reunião às {meeting_str}. Estamos ansiosos para falar com você!"],
        id=f"whatsapp_{meeting_dt.timestamp()}_1day",
        replace_existing=True,
    )

    # 4 hours before
    scheduler.add_job(
        send_wa_bulk,
        trigger=DateTrigger(run_date=meeting_dt - timedelta(hours=4)),
        args=[f"Oi {first_name}, tudo certo para a nossa reunião hoje às {meeting_str}?"],
        id=f"whatsapp_{meeting_dt.timestamp()}_4h",
        replace_existing=True,
    )

    # 1 hour after
    scheduler.add_job(
        send_wa_bulk,
        trigger=DateTrigger(run_date=meeting_dt + timedelta(hours=1)),
        args=[f"{first_name}, obrigado pela reunião! Qualquer dúvida, estamos à disposição."],
        id=f"whatsapp_{meeting_dt.timestamp()}_after",
        replace_existing=True,
    )


def send_wa_bulk(message: str) -> None:
    for phone in ADMIN_PHONES:
        send_wa_message(phone, message)


# -----------------------------------------------------------------------------
# Webhook endpoint
# -----------------------------------------------------------------------------
@app.post("/webhook/cal")
async def cal_webhook(
    request: Request, x_cal_signature_256: str = Header(None)
):
    raw_body = await request.body()
    verify_signature(x_cal_signature_256, raw_body)

    # Log do payload recebido
    print("Payload recebido do Cal.com:")
    print(json.loads(raw_body))

    try:
        data = CalWebhookPayload.model_validate_json(raw_body)
    except ValidationError as e:
        print("Erro de validação:")
        print(e.json())
        raise HTTPException(
            status_code=400,
            detail=f"Payload inválido: {str(e)}"
        )

    if data.trigger_event not in {"BOOKING_CREATED", "BOOKING_RESCHEDULED"}:
        return {"ignored": data.trigger_event}

    attendee = data.payload.attendees[0]
    start_iso = data.payload.start_time
    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(TZ)

    formatted_pt = format_pt_br(start_dt)

    # Notion sync
    page_id = notion_find_page(attendee.email, attendee.phone)
    if page_id:
        notion_update_datetime(page_id, formatted_pt)

    # Schedule WhatsApp messages
    first_name = attendee.name.split()[0]
    schedule_messages(first_name, start_dt)

    return {"status": "ok", "scheduled": True}


# -----------------------------------------------------------------------------
# Health check endpoint
# -----------------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "status": "healthy",
        "version": "1.0.0",
        "timezone": str(TZ),
        "admin_phones_configured": len(ADMIN_PHONES),
    } 