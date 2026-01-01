# main.py
from datetime import datetime
from datetime import timedelta
from typing import Optional, List, Dict, Any
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING

# ----------------------------------------------------------------------
# MongoDB setup (NO FALLBACK — MUST BE SET IN ENV)
# ----------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("❌ MONGO_URI environment variable is NOT set. Set it in Render.")

client = MongoClient(MONGO_URI)
db = client["dashboard_db"]          # Database name

messages_col = db["messages"]        # Collection: all messages
automation_col = db["automation_settings"]  # Collection: per-phone automation flag
alerts_col = db["new_message_alerts"]  # Collection: unread message alerts
contacts_col = db["contacts"]



# Indexes (important for speed)
messages_col.create_index([("id", ASCENDING)], unique=True)
messages_col.create_index([("phone", ASCENDING)])
messages_col.create_index([("timestamp", DESCENDING)])
messages_col.create_index([("meta_message_id", ASCENDING)])
contacts_col.create_index([("phone", ASCENDING)], unique=True)
messages_col.create_index(
    [("phone", ASCENDING), ("direction", ASCENDING), ("timestamp", DESCENDING)]
)

automation_col.create_index([("phone", ASCENDING)], unique=True)
alerts_col.create_index([("phone", ASCENDING)], unique=True)


def get_next_message_id() -> int:
    """Auto-increment integer ID like SQLite version."""
    doc = messages_col.find_one(sort=[("id", -1)], projection={"id": 1})
    return int(doc["id"]) + 1 if doc else 1


# ----------------------------------------------------------------------
# FastAPI + CORS
# ----------------------------------------------------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------------
# Pydantic models
# ----------------------------------------------------------------------
class LogMessage(BaseModel):
    phone: str
    client_name: Optional[str] = None
    direction: str
    message: str
    media_url: Optional[str] = None
    automation: Optional[str] = None
    timestamp: Optional[datetime] = None
    follow_up_needed: Optional[bool] = False
    meta_message_id: Optional[str] = None


class LogMessageFromDashboard(BaseModel):
    phone: str
    message: str
    timestamp: str
    # optional – we ignore raw direction and handled_by; keep them for backward-compat
    direction: Optional[str] = "dashboard"
    follow_up_needed: Optional[bool] = False
    notes: Optional[str] = ""
    handled_by: Optional[str] = "Dashboard User"


class ContactSummary(BaseModel):
    phone: str
    client_name: Optional[str]
    last_message: str
    last_time: datetime
    last_direction: str
    follow_up_open: bool
    notes: Optional[str] = ""

class ContactCreate(BaseModel):
    phone: str
    display_name: str
    notes: Optional[str] = ""


class ConversationMessage(BaseModel):
    id: int
    phone: str
    client_name: Optional[str]
    direction: str
    message: str
    media_url: Optional[str]
    automation: Optional[str]
    timestamp: datetime
    follow_up_needed: bool
    handled_by: Optional[str]
    notes: Optional[str]
    status: Optional[str]


class UpdateMessage(BaseModel):
    follow_up_needed: Optional[bool] = None
    handled_by: Optional[str] = None
    notes: Optional[str] = None


class DeleteResponse(BaseModel):
    success: bool
    message: str
    deleted_count: Optional[int] = None


# --- Automation toggle models ---
class AutomationStatus(BaseModel):
    phone: str
    automation_enabled: bool


class AutomationUpdate(BaseModel):
    automation_enabled: bool


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def normalize_direction(raw: str) -> str:
    """Normalize Make.com values -> 'user' or 'bot'."""
    if not raw:
        raise HTTPException(status_code=400, detail="direction is required")

    raw = raw.strip().lower()

    if raw in {"user", "incoming", "client"}:
        return "user"
    if raw in {"bot", "outgoing", "agent"}:
        return "bot"

    raise HTTPException(status_code=400, detail=f"Invalid direction: {raw}")

def is_whatsapp_session_active(phone: str) -> bool:
    last_user_msg = messages_col.find_one(
        {"phone": phone, "direction": "user"},
        sort=[("timestamp", -1)],
        projection={"timestamp": 1}
    )

    if not last_user_msg:
        return False

    last_time = last_user_msg["timestamp"]
    return datetime.utcnow() - last_time <= timedelta(hours=24)


def doc_to_message(doc: Dict[str, Any]) -> ConversationMessage:
    """Convert MongoDB doc to API schema."""
    return ConversationMessage(
        id=int(doc["id"]),
        phone=doc["phone"],
        client_name=doc.get("client_name"),
        direction=doc["direction"],
        message=doc["message"],
        media_url=doc.get("media_url"),
        automation=doc.get("automation"),
        timestamp=doc["timestamp"],
        follow_up_needed=bool(doc.get("follow_up_needed", False)),
        handled_by=doc.get("handled_by"),
        notes=doc.get("notes"),
        status=doc.get("status")
    )


def get_automation_enabled_for_phone(phone: str) -> bool:
    """Return True/False if automation is enabled for this phone. Default = True."""
    doc = automation_col.find_one({"phone": phone})
    if not doc:
        return True
    return bool(doc.get("automation_enabled", True))


# ----------------------------------------------------------------------
# ROUTES
# ----------------------------------------------------------------------

# 🔹 POST /log  (Make.com → backend, bot + user messages)
@app.post("/log", response_model=ConversationMessage)
def log_message(payload: LogMessage):

    norm_direction = normalize_direction(payload.direction)

    doc = {
        "id": get_next_message_id(),
        "phone": payload.phone,
        "client_name": payload.client_name,
        "direction": norm_direction,
        "message": payload.message,
        "media_url": payload.media_url,
        "automation": payload.automation,
        "timestamp": payload.timestamp or datetime.utcnow(),
        "follow_up_needed": bool(payload.follow_up_needed),
        "handled_by": None,
        "notes": None,
        "meta_message_id": payload.meta_message_id,  # 🔑
        "status": "sent"  # default initial state
    }

    messages_col.insert_one(doc)
    return doc_to_message(doc)


# 🔹 POST /log_message (Dashboard → backend, manual replies)
@app.post("/log_message")
def log_message_from_dashboard(payload: LogMessageFromDashboard):

    # Parse timestamp from ISO string
    try:
        ts = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
    except Exception:
        ts = datetime.utcnow()

    # Always store dashboard messages with direction = "dashboard"
    direction = "dashboard"

    # Get client name if already exists from any previous message
    existing = messages_col.find_one({"phone": payload.phone})
    client_name = existing.get("client_name") if existing else None

    doc = {
        "id": get_next_message_id(),
        "phone": payload.phone,
        "client_name": client_name,
        "direction": direction,
        "message": payload.message,
        "media_url": None,
        "automation": "Dashboard",
        "timestamp": ts,
        "follow_up_needed": payload.follow_up_needed or False,
        "handled_by": payload.handled_by or "Dashboard User",
        "notes": payload.notes or "",
        "status": "queued",
    }

    messages_col.insert_one(doc)

    return {"status": "success", "id": doc["id"]}

@app.post("/meta/status")
def meta_status(payload: dict):
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]

        status_event = change["statuses"][0]

        meta_message_id = status_event["id"]
        status = status_event["status"]  # sent | delivered | read | failed

        result = messages_col.update_one(
            {"meta_message_id": meta_message_id},
            {"$set": {"status": status, "updated_at": datetime.utcnow()}}
        )

        if result.matched_count == 0:
            print(f"⚠️ Status update for unknown meta_message_id: {meta_message_id}")
        return {"success": True}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.route("/api/session/<phone>")
def get_session_status(phone):
    active = is_whatsapp_session_active(phone)
    return jsonify({
        "session_active": active
    })


# 🔹 GET /contacts
@app.get("/contacts", response_model=List[ContactSummary])
def list_contacts(only_follow_up: bool = Query(False)):

    docs = list(
        messages_col.find().sort([("phone", ASCENDING), ("timestamp", DESCENDING)])
    )

    latest_per_phone: Dict[str, Dict[str, Any]] = {}
    followups: Dict[str, bool] = {}

    for m in docs:
        phone = m["phone"]
        if phone not in latest_per_phone:
            latest_per_phone[phone] = m
        if phone not in followups:
            followups[phone] = False
        if m.get("follow_up_needed"):
            followups[phone] = True

    contacts: List[ContactSummary] = []

    for phone, m in latest_per_phone.items():
        fu = followups[phone]
        if only_follow_up and not fu:
            continue

        # 🔑 Fetch contact metadata safely
        contact_doc = contacts_col.find_one({"phone": phone}) or {}

        client_name = contact_doc.get("display_name") or m.get("client_name")
        notes = contact_doc.get("notes", "")

        contacts.append(
            ContactSummary(
                phone=phone,
                client_name=client_name,
                last_message=m["message"],
                last_time=m["timestamp"],
                last_direction=m["direction"],
                follow_up_open=fu,
                notes=notes,
            )
        )

    contacts.sort(key=lambda x: x.last_time, reverse=True)
    return contacts

@app.post("/contacts")
def create_contact(payload: ContactCreate):
    contacts_col.update_one(
        {"phone": payload.phone},
        {
            "$set": {
                "phone": payload.phone,
                "display_name": payload.display_name,
                "notes": payload.notes,
                "updated_at": datetime.utcnow(),
            },
            "$setOnInsert": {"created_at": datetime.utcnow()},
        },
        upsert=True
    )
    return {"success": True}

from fastapi import Query

@app.patch("/contacts/{phone}")
def update_contact(phone: str, display_name: str):
    contacts_col.update_one(
        {"phone": phone},
        {
            "$set": {
                "display_name": display_name,
                "updated_at": datetime.utcnow(),
            },
            "$setOnInsert": {
                "created_at": datetime.utcnow()
            }
        },
        upsert=True
    )

    return {"success": True}

def get_contact_name(phone: str) -> Optional[str]:
    doc = contacts_col.find_one({"phone": phone})
    return doc.get("display_name") if doc else None


# 🔹 GET /conversation/{phone}
@app.get("/conversation/{phone}", response_model=List[ConversationMessage])
def get_conversation(phone: str, limit: int = 50, offset: int = 0):

    cursor = (
        messages_col.find({"phone": phone})
        .sort("id", ASCENDING)
        .skip(offset)
        .limit(limit)
    )
    docs = list(cursor)
    return [doc_to_message(d) for d in docs]


# 🔹 PATCH /message/{msg_id}
@app.patch("/message/{msg_id}", response_model=ConversationMessage)
def update_message(msg_id: int, payload: UpdateMessage):

    msg = messages_col.find_one({"id": int(msg_id)})
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    updates: Dict[str, Any] = {}

    if payload.follow_up_needed is not None:
        updates["follow_up_needed"] = payload.follow_up_needed
    if payload.handled_by is not None:
        updates["handled_by"] = payload.handled_by
    if payload.notes is not None:
        updates["notes"] = payload.notes

    if updates:
        messages_col.update_one({"id": int(msg_id)}, {"$set": updates})
        msg.update(updates)

    return doc_to_message(msg)


# 🔹 DELETE /message/{msg_id}
@app.delete("/message/{msg_id}", response_model=DeleteResponse)
def delete_message(msg_id: int):

    result = messages_col.delete_one({"id": int(msg_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")

    return DeleteResponse(
        success=True,
        message=f"Message {msg_id} deleted successfully",
        deleted_count=result.deleted_count,
    )


# 🔹 DELETE /conversation/{phone}
@app.delete("/conversation/{phone}", response_model=DeleteResponse)
def delete_conversation(phone: str):

    result = messages_col.delete_many({"phone": phone})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="No messages found for this phone")

    return DeleteResponse(
        success=True,
        message=f"Deleted {result.deleted_count} messages for {phone}",
        deleted_count=result.deleted_count,
    )


# 🔹 GET /automation/{phone}
@app.get("/automation/{phone}", response_model=AutomationStatus)
def get_automation(phone: str):
    enabled = get_automation_enabled_for_phone(phone)
    return AutomationStatus(phone=phone, automation_enabled=enabled)

# 🔹 GET /session/{phone}
@app.get("/session/{phone}")
def get_session_status(phone: str):
    return {
        "phone": phone,
        "session_active": is_whatsapp_session_active(phone)
    }


# 🔹 PATCH /automation/{phone}
@app.patch("/automation/{phone}", response_model=AutomationStatus)
def set_automation(phone: str, update: AutomationUpdate):
    enabled = bool(update.automation_enabled)
    automation_col.update_one(
        {"phone": phone},
        {
            "$set": {
                "phone": phone,
                "automation_enabled": enabled,
                "updated_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )
    return AutomationStatus(phone=phone, automation_enabled=enabled)


# ----------------------------------------------------------------------
# NEW: Alert Management Routes
# ----------------------------------------------------------------------

# 🔹 GET /alerts/{phone}
@app.get("/alerts/{phone}")
def get_alert_status(phone: str):
    doc = alerts_col.find_one({"phone": phone})
    has_alert = bool(doc.get("has_alert", False)) if doc else False
    return {"phone": phone, "has_alert": has_alert}


# 🔹 POST /alerts/{phone}
@app.post("/alerts/{phone}")
def set_alert_status(phone: str, has_alert: bool = Query(True)):
    alerts_col.update_one(
        {"phone": phone},
        {"$set": {"phone": phone, "has_alert": has_alert, "updated_at": datetime.utcnow()}},
        upsert=True
    )
    return {"phone": phone, "has_alert": has_alert}


# 🔹 DELETE /alerts/{phone}
@app.delete("/alerts/{phone}")
def clear_alert(phone: str):
    alerts_col.delete_one({"phone": phone})
    return {"phone": phone, "has_alert": False}


# 🔹 GET /alerts (get all alerts)
@app.get("/alerts")
def get_all_alerts():
    docs = list(alerts_col.find({"has_alert": True}, {"_id": 0, "phone": 1}))
    return {"alerts": [doc["phone"] for doc in docs]}


# ----------------------------------------------------------------------
# ROOT
# ----------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "message": "WhatsApp Chat Logger API (MongoDB Version)",
        "version": "3.2",
    }
