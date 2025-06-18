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
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status, Body
from pydantic import BaseModel, Field
import pytz
from dotenv import load_dotenv
import requests

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
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN")
ADMIN_PHONES = [p.strip() for p in os.getenv("ADMIN_PHONES", "").split(",") if p]
TZ = pytz.timezone(os.getenv("TZ", "America/Sao_Paulo"))

HEADERS_NOTION = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

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
    eventDescription: Optional[str] = None
    videoCallData: Optional[dict] = None


class CalWebhookPayload(BaseModel):
    trigger_event: str = Field(..., alias="triggerEvent")
    payload: Booking


class ScheduleTestRequest(BaseModel):
    first_name: str
    meeting_datetime: str  # ISO format string


class ScheduleLeadTestRequest(BaseModel):
    email: str
    meeting_datetime: str  # ISO format string
    first_name: str = "Lead"


class SendLeadMessageRequest(BaseModel):
    email: str
    meeting_datetime: str  # ISO format string
    first_name: str = "Lead"
    which: str = "1d"  # opções: "1d", "4h", "after"
    send_now: bool = True


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
            },
            "Status": {
                "status": {"name": "Agendado reunião"}
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


def send_wa_message(phone: str, message: str, has_link: bool = False, link_data: dict | None = None) -> None:
    """Send a WhatsApp message using Z-API."""
    # Limpar o número de telefone (remover caracteres não numéricos)
    clean_phone = ''.join(filter(str.isdigit, phone))
    # Garantir que comece com 55 (Brasil)
    if not clean_phone.startswith('55'):
        clean_phone = '55' + clean_phone
    
    print(f"Enviando mensagem WhatsApp para {clean_phone}")
    print(f"Conteúdo da mensagem: {message}")
    
    headers = {
        "Client-Token": ZAPI_CLIENT_TOKEN,
        "Content-Type": "application/json"
    }
    
    base_url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"
    
    # Se tiver link, usar o endpoint de send-link
    if has_link and link_data:
        url = f"{base_url}/send-link"
        payload = {
            "phone": clean_phone,
            "message": message,
            "image": link_data.get("image"),  # Optional
            "linkUrl": link_data["url"],
            "title": link_data["title"],
            "linkDescription": link_data["description"],
            "linkType": "LARGE"  # Use LARGE para melhor visualização
        }
        print(f"Enviando link com payload: {json.dumps(payload, indent=2)}")
    else:
        # Tentar endpoint /send-text
        url = f"{base_url}/send-text"
        payload = {
            "phone": clean_phone,
            "message": message
        }
        print(f"Enviando texto com payload: {json.dumps(payload, indent=2)}")
    
    print(f"URL da requisição: {url}")
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response_text = response.text
        print(f"Status code: {response.status_code}")
        print(f"Resposta Z-API: {response_text}")
        
        if response.status_code != 200 or "error" in response_text.lower():
            print("Erro detectado na resposta!")
            print(f"Headers enviados: {json.dumps(headers, indent=2)}")
        else:
            print("Mensagem enviada com sucesso!")
            
        response.raise_for_status()
    except Exception as e:
        print(f"Erro ao enviar mensagem WhatsApp: {str(e)}")
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


def extract_zoom_info(description: str) -> dict:
    """Extract Zoom meeting information from event description."""
    info = {
        "url": None,
        "id": None,
        "password": None
    }
    
    if not description:
        return info
    
    # Try to find the Zoom URL
    import re
    url_match = re.search(r'https://[^\s<>\[\]]+zoom.us/j/\d+\?[^\s<>\[\]]+', description)
    if url_match:
        info["url"] = url_match.group(0)
    
    # Try to find meeting ID
    id_match = re.search(r'ID da reunião:\*\*\*\* (\d+(?:\s*\d+)*)', description)
    if id_match:
        info["id"] = id_match.group(1).replace(" ", "")
    
    # Try to find password
    pwd_match = re.search(r'Senha:\*\*\*\* ([^\n<]+)', description)
    if pwd_match:
        info["password"] = pwd_match.group(1)
    
    return info


def send_immediate_booking_notifications(attendee_name: str, whatsapp: str | None, start_dt: datetime) -> None:
    """Send immediate WhatsApp notifications when a booking is made."""
    formatted_dt = format_pt_br(start_dt)
    zoom_url = "https://us06web.zoom.us/j/8902841864?pwd=OIjXN37C7fjELriVg4y387EbXUSVsR.1"
    
    # Mensagem para o lead
    if whatsapp:
        lead_message = (
            f"Olá {attendee_name}, sua reunião foi agendada com sucesso! 🎉\n\n"
            f"📅 Data: {formatted_dt}\n"
            f"🖥️ Link da reunião Zoom:\n{zoom_url} "
        )
        send_wa_message(whatsapp, lead_message)
    
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
    # 1. Tenta userFieldsResponses (como dict ou objeto)
    ufr = getattr(data.payload, 'userFieldsResponses', None)
    if ufr:
        if isinstance(ufr, dict) and 'WhatsApp' in ufr and 'value' in ufr['WhatsApp']:
            whatsapp = ufr['WhatsApp']['value']
        elif hasattr(ufr, 'Whatsapp') and hasattr(ufr.Whatsapp, 'get'):
            whatsapp = ufr.Whatsapp.get("value")
    # 2. Tenta responses se não encontrou
    if not whatsapp:
        resp = getattr(data.payload, 'responses', None)
        if resp:
            if isinstance(resp, dict) and 'WhatsApp' in resp and 'value' in resp['WhatsApp']:
                whatsapp = resp['WhatsApp']['value']
            elif hasattr(resp, 'WhatsApp') and hasattr(resp.WhatsApp, 'value'):
                whatsapp = resp.WhatsApp.value
    # 3. Se ainda não encontrou, busca no Notion (propriedade Telefone, tipo Rich text)
    if not whatsapp and attendee.email:
        page_id = notion_find_page(attendee.email, None)
        if page_id:
            resp = httpx.get(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=HEADERS_NOTION,
                timeout=15,
            )
            resp.raise_for_status()
            page = resp.json()
            props = page.get("properties", {})
            for k, v in props.items():
                if k == "Telefone" and v.get("type") == "rich_text" and v.get("rich_text"):
                    whatsapp = v["rich_text"][0]["plain_text"]
                    break
    if whatsapp:
        print(f"WhatsApp encontrado: {whatsapp}")
    else:
        print(f"Nenhum número de WhatsApp fornecido. userFieldsResponses: {ufr}, responses: {getattr(data.payload, 'responses', None)}")

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
        print("\nEnviando notificações imediatas...")
        send_immediate_booking_notifications(attendee.name, whatsapp, start_dt)
        print("✓ Notificações imediatas enviadas com sucesso")
    except Exception as e:
        print(f"✗ Erro ao enviar notificações: {str(e)}")
        raise

    print("\nAgendando mensagens futuras...")
    schedule_messages(attendee.name, start_dt)
    print("✓ Mensagens futuras agendadas com sucesso")

    print("\n=== Webhook processado com sucesso ===")
    return {"success": True}


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


@app.post("/test/schedule-messages", tags=["Testes"])
def test_schedule_messages(
    req: ScheduleTestRequest = Body(...)
):
    """Agende mensagens futuras para teste (admins)."""
    try:
        dt = datetime.fromisoformat(req.meeting_datetime)
        schedule_messages(req.first_name, dt)
        return {"success": True, "scheduled_for": req.meeting_datetime}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/test/schedule-lead-messages", tags=["Testes"])
def test_schedule_lead_messages(
    req: ScheduleLeadTestRequest = Body(...)
):
    """Agende mensagens futuras para um lead (buscando telefone pelo e-mail no Notion)."""
    try:
        dt = datetime.fromisoformat(req.meeting_datetime)
        # Buscar telefone no Notion
        page_id = notion_find_page(req.email, None)
        if not page_id:
            return {"success": False, "error": "Lead não encontrado no Notion"}
        # Buscar telefone na página do Notion
        # Buscar detalhes da página
        resp = httpx.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=HEADERS_NOTION,
            timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        phone = None
        props = page.get("properties", {})
        for k, v in props.items():
            if v.get("type") == "phone_number" and v.get("phone_number"):
                phone = v["phone_number"]
                break
        if not phone:
            return {"success": False, "error": "Telefone não encontrado para o lead no Notion"}
        # Agendar mensagens futuras para o lead
        meeting_str = dt.strftime("%H:%M")
        scheduler.add_job(
            send_wa_message,
            trigger=DateTrigger(run_date=dt - timedelta(days=1)),
            args=[phone, f"Olá {req.first_name}, amanhã temos nossa reunião às {meeting_str}. Estamos ansiosos para falar com você!"],
            id=f"lead_whatsapp_{dt.timestamp()}_1day",
            replace_existing=True,
        )
        scheduler.add_job(
            send_wa_message,
            trigger=DateTrigger(run_date=dt - timedelta(hours=4)),
            args=[phone, f"Oi {req.first_name}, tudo certo para a nossa reunião hoje às {meeting_str}?"],
            id=f"lead_whatsapp_{dt.timestamp()}_4h",
            replace_existing=True,
        )
        scheduler.add_job(
            send_wa_message,
            trigger=DateTrigger(run_date=dt + timedelta(hours=1)),
            args=[phone, f"{req.first_name}, obrigado pela reunião! Qualquer dúvida, estamos à disposição."],
            id=f"lead_whatsapp_{dt.timestamp()}_after",
            replace_existing=True,
        )
        return {"success": True, "scheduled_for": req.meeting_datetime, "phone": phone}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/test/send-lead-message", tags=["Testes"])
def test_send_lead_message(
    req: SendLeadMessageRequest = Body(...)
):
    """Envie ou agende uma mensagem específica para o lead (buscando telefone pelo e-mail no Notion)."""
    try:
        dt = datetime.fromisoformat(req.meeting_datetime)
        # Buscar telefone no Notion
        page_id = notion_find_page(req.email, None)
        if not page_id:
            return {"success": False, "error": "Lead não encontrado no Notion"}
        # Buscar telefone na página do Notion
        resp = httpx.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=HEADERS_NOTION,
            timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        phone = None
        props = page.get("properties", {})
        for k, v in props.items():
            if v.get("type") == "phone_number" and v.get("phone_number"):
                phone = v["phone_number"]
                break
        if not phone:
            return {"success": False, "error": "Telefone não encontrado para o lead no Notion"}
        meeting_str = dt.strftime("%H:%M")
        # Escolher mensagem
        if req.which == "1d":
            msg = f"Olá {req.first_name}, amanhã temos nossa reunião às {meeting_str}. Estamos ansiosos para falar com você!"
            when = dt - timedelta(days=1)
        elif req.which == "4h":
            msg = f"Oi {req.first_name}, tudo certo para a nossa reunião hoje às {meeting_str}?"
            when = dt - timedelta(hours=4)
        elif req.which == "after":
            msg = f"{req.first_name}, obrigado pela reunião! Qualquer dúvida, estamos à disposição."
            when = dt + timedelta(hours=1)
        else:
            return {"success": False, "error": "Tipo de mensagem inválido. Use: 1d, 4h ou after."}
        if req.send_now:
            send_wa_message(phone, msg)
            return {"success": True, "sent_now": True, "phone": phone, "message": msg}
        else:
            scheduler.add_job(
                send_wa_message,
                trigger=DateTrigger(run_date=when),
                args=[phone, msg],
                id=f"lead_whatsapp_{dt.timestamp()}_{req.which}",
                replace_existing=True,
            )
            return {"success": True, "scheduled_for": when.isoformat(), "phone": phone, "message": msg}
    except Exception as e:
        return {"success": False, "error": str(e)} 