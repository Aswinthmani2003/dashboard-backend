# main.py
from datetime import datetime
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
messages_col = db["messages"]        # Collection name

# Indexes (important for speed)
messages_col.create_index([("id", ASCENDING)], unique=True)
messages_col.create_index([("phone", ASCENDING)])
messages_col.create_index([("timestamp", DESCENDING)])


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


class LogMessageFromDashboard(BaseModel):
    phone: str
    message: str
    direction: str
    timestamp: str
    follow_up_needed: Optional[bool] = False
    notes: Optional[str] = ""
    handled_by: Optional[str] = ""


class ContactSummary(BaseModel):
    phone: str
    client_name: Optional[str]
    last_message: str
    last_time: datetime
    last_direction: str
    follow_up_open: bool


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


class UpdateMessage(BaseModel):
    follow_up_needed: Optional[bool] = None
    handled_by: Optional[str] = None
    notes: Optional[str] = None


class DeleteResponse(BaseModel):
    success: bool
    message: str
    deleted_count: Optional[int] = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def normalize_direction(raw: str) -> str:
    """Normalize Make.com values -> user/bot (ONLY for webhook /log)."""
    if not raw:
        raise HTTPException(status_code=400, detail="direction is required")

    raw_l = raw.strip().lower()

    if raw_l in {"user", "incoming", "client"}:
        return "user"
    if raw_l in {"bot", "outgoing", "agent"}:
        return "bot"

    raise HTTPException(status_code=400, detail=f"Invalid direction: {raw}")


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
    )


# ----------------------------------------------------------------------
# ROUTES
# ----------------------------------------------------------------------

# 🔹 POST /log  (Make.com → backend)
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
    }

    messages_col.insert_one(doc)
    return doc_to_message(doc)


# 🔹 POST /log_message (Dashboard → backend)
@app.post("/log_message")
def log_message_from_dashboard(payload: LogMessageFromDashboard):

    # Parse timestamp
    try:
        ts = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
    except Exception:
        ts = datetime.utcnow()

    # IMPORTANT CHANGE:
    # For dashboard messages, KEEP direction as-is (e.g. "Dashboard User")
    direction = (payload.direction or "Dashboard User").strip() or "Dashboard User"

    # Get client name if already exists
    existing = messages_col.find_one({"phone": payload.phone})
    client_name = existing.get("client_name") if existing else None

    doc = {
        "id": get_next_message_id(),
        "phone": payload.phone,
        "client_name": client_name,
        "direction": direction,                 # <-- stored as "Dashboard User"
        "message": payload.message,
        "media_url": None,
        "automation": "Dashboard",
        "timestamp": ts,
        "follow_up_needed": payload.follow_up_needed or False,
        "handled_by": payload.handled_by or "Dashboard User",
        "notes": payload.notes or "",
    }

    messages_col.insert_one(doc)

    return {"status": "success", "id": doc["id"]}


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
        contacts.append(
            ContactSummary(
                phone=phone,
                client_name=m.get("client_name"),
                last_message=m["message"],
                last_time=m["timestamp"],
                last_direction=m["direction"],
                follow_up_open=fu,
            )
        )

    contacts.sort(key=lambda x: x.last_time, reverse=True)
    return contacts


# 🔹 GET /conversation/{phone}
@app.get("/conversation/{phone}", response_model=List[ConversationMessage])
def get_conversation(phone: str, limit: int = 50, offset: int = 0):

    cursor = (
        messages_col.find({"phone": phone})
        .sort("timestamp", ASCENDING)  # oldest → newest for proper chat order
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


# ----------------------------------------------------------------------
# ROOT
# ----------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "message": "WhatsApp Chat Logger API (MongoDB Version)",
        "version": "3.0",
    }
