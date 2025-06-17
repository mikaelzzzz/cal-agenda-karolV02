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
    version="1.0.1"
)

scheduler = AsyncIOScheduler()
scheduler.start()

# -----------------------------------------------------------------------------
# Pydantic models for webhook parsing (simplified)
# -----------------------------------------------------------------------------
class Attendee(BaseModel):
    name: str
    email: str
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    timeZone: Optional[str] = None


class UserFieldsResponses(BaseModel):
    Whatsapp: Optional[dict] = None


class Booking(BaseModel):
    start_time: str = Field(..., alias="startTime")
    end_time: str = Field(..., alias="endTime")
    attendees: List[Attendee]
    uid: str
    userFieldsResponses: Optional[UserFieldsResponses] = None


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
        # Remove caracteres não numéricos do telefone
        clean_phone = ''.join(filter(str.isdigit, phone))
        # Se começar com +55, remove
        if clean_phone.startswith('55'):
            clean_phone = clean_phone[2:]
        filters.append({"property": "Telefone", "phone_number": {"equals": clean_phone}})

    # Combine filters with OR if both present
    if len(filters) == 2:
        filter_json = {"or": filters}
    else:
        filter_json = filters[0]

    print(f"Buscando no Notion com filtro: {json.dumps(filter_json, indent=2)}")

    resp = httpx.post(
        f"https://api.notion.com/v1/databases/{NOTION_DB}/query",
        headers=HEADERS_NOTION,
        json={"filter": filter_json},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    
    if results:
        print(f"Encontrou página no Notion: {results[0]['id']}")
    else:
        print("Nenhuma página encontrada no Notion")
    
    return results[0]["id"] if results else None


def notion_update_datetime(page_id: str, when: str) -> None:
    print(f"Atualizando Notion page {page_id} com data {when}")
    payload = {
        "properties": {
            "Data Agendada pelo Lead": {
                "rich_text": [{"text": {"content": when}}]
            }
        }
    }
    try:
        resp = httpx.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=HEADERS_NOTION,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        print(f"Notion atualizado com sucesso: {resp.status_code}")
    except Exception as e:
        print(f"Erro ao atualizar Notion: {str(e)}")
        raise


def send_wa_message(phone: str, message: str, has_link: bool = False, link_data: dict = None) -> None:
    print(f"Enviando mensagem WhatsApp para {phone}")
    
    # Remove o '+' do início do número se existir
    if phone.startswith('+'):
        phone = phone[1:]
    
    if has_link and link_data:
        # Usa o endpoint send-link para mensagens com link
        endpoint = f"{ZAPI_BASE}/send-link"
        payload = {
            "phone": phone,
            "message": message,
            "linkUrl": link_data["url"],
            "title": link_data.get("title", "Reunião Zoom"),
            "linkDescription": link_data.get("description", "Link para a reunião"),
            "linkType": "LARGE"
        }
        if link_data.get("image"):
            payload["image"] = link_data["image"]
    else:
        # Usa o endpoint padrão para mensagens simples
        endpoint = f"{ZAPI_BASE}/send-message"
        payload = {"phone": phone, "message": message}

    try:
        resp = httpx.post(endpoint, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"Mensagem WhatsApp enviada com sucesso para {phone}")
        print(f"Resposta Z-API: {resp.text}")
    except Exception as e:
        print(f"Erro ao enviar mensagem WhatsApp para {phone}: {str(e)}")
        raise


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


def send_immediate_booking_notifications(attendee_name: str, whatsapp: str | None, start_dt: datetime) -> None:
    """Send immediate WhatsApp notifications when a booking is made."""
    formatted_dt = format_pt_br(start_dt)
    zoom_url = "https://us06web.zoom.us/j/8902841864?pwd=OIjXN37C7fjELriVg4y387EbXUSVsR.1"
    
    # Mensagem para o lead
    if whatsapp:
        lead_message = (
            f"Olá {attendee_name}, sua reunião foi agendada com sucesso! 🎉\n\n"
            f"📅 Data: {formatted_dt}\n"
            "🖥️ Link da reunião Zoom:\n"
            f"{zoom_url}"
        )
        
        # Dados do link para o Zoom
        link_data = {
            "url": zoom_url,
            "title": "Reunião Zoom",
            "description": f"Reunião agendada para {formatted_dt}",
            "image": "https://1000logos.net/wp-content/uploads/2021/06/Zoom-icon.png"
        }
        
        send_wa_message(whatsapp, lead_message, has_link=True, link_data=link_data)
    
    # Mensagem para o time de vendas
    sales_message = (
        f"💼 Nova Reunião Agendada!\n\n"
        f"👤 Cliente: {attendee_name}\n"
        f"📅 Data: {formatted_dt}"
    )
    for admin_phone in ADMIN_PHONES:
        send_wa_message(admin_phone, sales_message)


# -----------------------------------------------------------------------------
# Webhook endpoint
# -----------------------------------------------------------------------------
@app.post("/webhook/cal")
async def cal_webhook(
    request: Request, x_cal_signature_256: str = Header(None)
):
    print("\n=== Novo webhook recebido ===")
    print(f"Signature: {x_cal_signature_256}")
    
    raw_body = await request.body()
    try:
        verify_signature(x_cal_signature_256, raw_body)
        print("✓ Assinatura verificada com sucesso")
    except Exception as e:
        print(f"✗ Erro na verificação da assinatura: {str(e)}")
        raise

    # Log do payload recebido
    print("\nPayload recebido do Cal.com:")
    payload_json = json.loads(raw_body)
    print(json.dumps(payload_json, indent=2))

    try:
        data = CalWebhookPayload.model_validate_json(raw_body)
        print("✓ Payload validado com sucesso")
    except ValidationError as e:
        print("✗ Erro de validação:")
        print(e.json())
        raise HTTPException(
            status_code=400,
            detail=f"Payload inválido: {str(e)}"
        )

    print(f"\nTipo de evento: {data.trigger_event}")
    if data.trigger_event not in {"BOOKING_CREATED", "BOOKING_RESCHEDULED", "BOOKING_REQUESTED"}:
        print(f"Evento ignorado: {data.trigger_event}")
        return {"ignored": data.trigger_event}

    attendee = data.payload.attendees[0]
    start_iso = data.payload.start_time
    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(TZ)
    formatted_pt = format_pt_br(start_dt)

    print(f"\nDetalhes do agendamento:")
    print(f"Nome: {attendee.name}")
    print(f"Email: {attendee.email}")
    print(f"Data: {formatted_pt}")

    # Notion sync
    whatsapp = None
    if data.payload.userFieldsResponses and data.payload.userFieldsResponses.Whatsapp:
        whatsapp = data.payload.userFieldsResponses.Whatsapp.get("value")
        print(f"WhatsApp encontrado: {whatsapp}")
    else:
        print("Nenhum número de WhatsApp fornecido")

    try:
        page_id = notion_find_page(attendee.email, whatsapp)
        if page_id:
            notion_update_datetime(page_id, formatted_pt)
            print(f"✓ Notion atualizado com sucesso")
        else:
            print("✗ Página não encontrada no Notion")
    except Exception as e:
        print(f"✗ Erro na integração com Notion: {str(e)}")
        raise

    try:
        # Send immediate notifications
        print("\nEnviando notificações imediatas...")
        send_immediate_booking_notifications(attendee.name, whatsapp, start_dt)
        print("✓ Notificações imediatas enviadas com sucesso")
    except Exception as e:
        print(f"✗ Erro ao enviar notificações: {str(e)}")
        raise

    try:
        # Schedule future WhatsApp messages
        print("\nAgendando mensagens futuras...")
        first_name = attendee.name.split()[0]
        schedule_messages(first_name, start_dt)
        print("✓ Mensagens futuras agendadas com sucesso")
    except Exception as e:
        print(f"✗ Erro ao agendar mensagens: {str(e)}")
        raise

    print("\n=== Webhook processado com sucesso ===\n")
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