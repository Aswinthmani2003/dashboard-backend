# main.py
from datetime import datetime
from typing import Optional, List, Dict, Any

import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING

# ----------------------------------------------------------------------
# MongoDB setup
# ----------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

client = MongoClient(MONGO_URI)
db = client["dashboard_db"]          # choose any db name you like
messages_col = db["messages"]        # main collection for messages

# Ensure useful indexes
messages_col.create_index([("id", ASCENDING)], unique=True)
messages_col.create_index([("phone", ASCENDING), ("timestamp", DESCENDING)])


def get_next_message_id() -> int:
    """
    Simple auto-increment for message id.
    Good enough for your current low-volume use case.
    """
    doc = messages_col.find_one(sort=[("id", -1)], projection={"id": 1})
    if doc and "id" in doc:
        return int(doc["id"]) + 1
    return 1


# ----------------------------------------------------------------------
# FastAPI app + CORS
# ----------------------------------------------------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if needed
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
    direction: str  # "outgoing"
    message_type: Optional[str] = "text"
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
    """
    Normalise to:
    - "user"  (client message)
    - "bot"   (your automation / agent message)
    """
    if raw is None:
        raise HTTPException(status_code=400, detail="direction is required")

    value = raw.strip().lower()

    if value in {"user", "incoming", "client"}:
        return "user"
    if value in {"bot", "outgoing", "agent"}:
        return "bot"

    raise HTTPException(status_code=400, detail=f"Invalid direction: {raw}")


def doc_to_conversation_message(doc: Dict[str, Any]) -> ConversationMessage:
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
# API routes
# ----------------------------------------------------------------------
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
    return doc_to_conversation_message(doc)


@app.post("/log_message")
def log_message_from_dashboard(payload: LogMessageFromDashboard):
    """
    Endpoint specifically for dashboard-sent messages.
    """
    try:
        # Parse timestamp
        if isinstance(payload.timestamp, str):
            ts = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
        else:
            ts = datetime.utcnow()

        norm_direction = normalize_direction(payload.direction)

        # Get client name from existing messages if available
        existing = messages_col.find_one({"phone": payload.phone})
        client_name = existing.get("client_name") if existing else None

        doc = {
            "id": get_next_message_id(),
            "phone": payload.phone,
            "client_name": client_name,
            "direction": norm_direction,
            "message": payload.message,
            "media_url": None,
            "automation": "Dashboard",
            "timestamp": ts,
            "follow_up_needed": payload.follow_up_needed or False,
            "handled_by": payload.handled_by or "Dashboard User",
            "notes": payload.notes or "",
        }

        messages_col.insert_one(doc)

        return {
            "status": "success",
            "message": "Message logged successfully",
            "id": doc["id"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to log message: {str(e)}")


@app.get("/contacts", response_model=List[ContactSummary])
def list_contacts(
    only_follow_up: bool = Query(False),
):
    # Fetch all messages, sorted like the SQL version
    docs = list(
        messages_col.find().sort(
            [("phone", ASCENDING), ("timestamp", DESCENDING)]
        )
    )

    latest_per_phone: Dict[str, Dict[str, Any]] = {}
    followup_flags: Dict[str, bool] = {}

    for m in docs:
        phone = m["phone"]
        if phone not in latest_per_phone:
            latest_per_phone[phone] = m
        if phone not in followup_flags:
            followup_flags[phone] = False
        if m.get("follow_up_needed"):
            followup_flags[phone] = True

    contacts: List[ContactSummary] = []
    for phone, m in latest_per_phone.items():
        fu = followup_flags.get(phone, False)
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

    contacts.sort(key=lambda c: c.last_time, reverse=True)
    return contacts


@app.get("/conversation/{phone}", response_model=List[ConversationMessage])
def get_conversation(
    phone: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Get conversation messages with pagination support.
    Returns messages in ascending order (oldest first).
    """
    cursor = (
        messages_col.find({"phone": phone})
        .sort("timestamp", ASCENDING)
        .skip(offset)
        .limit(limit)
    )
    docs = list(cursor)
    return [doc_to_conversation_message(m) for m in docs]


@app.patch("/message/{msg_id}", response_model=ConversationMessage)
def update_message(
    msg_id: int,
    payload: UpdateMessage,
):
    msg = messages_col.find_one({"id": int(msg_id)})
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    update: Dict[str, Any] = {}
    if payload.follow_up_needed is not None:
        update["follow_up_needed"] = payload.follow_up_needed
    if payload.handled_by is not None:
        update["handled_by"] = payload.handled_by
    if payload.notes is not None:
        update["notes"] = payload.notes

    if update:
        messages_col.update_one({"id": int(msg_id)}, {"$set": update})
        msg.update(update)

    return doc_to_conversation_message(msg)


# ----------------------------------------------------------------------
# DELETE ENDPOINTS
# ----------------------------------------------------------------------
@app.delete("/message/{msg_id}", response_model=DeleteResponse)
def delete_message(msg_id: int):
    """Delete a single message by ID"""
    result = messages_col.delete_one({"id": int(msg_id)})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Message not found")

    return DeleteResponse(
        success=True,
        message=f"Message {msg_id} deleted successfully",
        deleted_count=result.deleted_count,
    )


@app.delete("/conversation/{phone}", response_model=DeleteResponse)
def delete_conversation(phone: str):
    """Delete all messages for a specific phone number"""
    result = messages_col.delete_many({"phone": phone})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="No messages found for this phone number")

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
        "message": "WhatsApp Chat Logger API",
        "version": "3.0-mongo",
        "endpoints": {
            "log": "POST /log",
            "log_message": "POST /log_message (for dashboard)",
            "contacts": "GET /contacts",
            "conversation": "GET /conversation/{phone}?limit=50&offset=0",
            "update": "PATCH /message/{msg_id}",
            "delete_message": "DELETE /message/{msg_id}",
            "delete_conversation": "DELETE /conversation/{phone}",
        },
    }
