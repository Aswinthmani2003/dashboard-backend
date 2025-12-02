# main.py
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ----------------------------------------------------------------------
# DB setup
# ----------------------------------------------------------------------
DATABASE_URL = "sqlite:///./chatlog.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, index=True)
    client_name = Column(String, nullable=True)
    # we will store only "user" or "bot" here
    direction = Column(String)
    message = Column(String)
    media_url = Column(String, nullable=True)
    automation = Column(String, nullable=True)  # e.g. "SIP_Reminder", "Birthday"
    timestamp = Column(DateTime, default=datetime.utcnow)
    follow_up_needed = Column(Boolean, default=False)
    handled_by = Column(String, nullable=True)
    notes = Column(String, nullable=True)


Base.metadata.create_all(bind=engine)

# ----------------------------------------------------------------------
# FastAPI app + CORS
# ----------------------------------------------------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # you can tighten this later
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
    # Make can send: "user"/"bot" OR "incoming"/"outgoing"
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
# DB dependency
# ----------------------------------------------------------------------
def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def normalize_direction(raw: str) -> str:
    """
    Accepts various values from Make and normalises to:
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


# ----------------------------------------------------------------------
# API routes
# ----------------------------------------------------------------------
@app.post("/log", response_model=ConversationMessage)
def log_message(payload: LogMessage, db: Session = Depends(get_db)):
    # normalise direction to "user" / "bot"
    norm_direction = normalize_direction(payload.direction)

    msg = Message(
        phone=payload.phone,
        client_name=payload.client_name,
        direction=norm_direction,
        message=payload.message,
        media_url=payload.media_url,
        automation=payload.automation,
        timestamp=payload.timestamp or datetime.utcnow(),
        follow_up_needed=bool(payload.follow_up_needed),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


@app.post("/log_message")
def log_message_from_dashboard(payload: LogMessageFromDashboard, db: Session = Depends(get_db)):
    """
    New endpoint specifically for dashboard-sent messages.
    Stores messages sent via the dashboard UI.
    """
    try:
        # Parse timestamp
        if isinstance(payload.timestamp, str):
            ts = datetime.fromisoformat(payload.timestamp.replace('Z', '+00:00'))
        else:
            ts = datetime.utcnow()
        
        # Normalize direction
        norm_direction = normalize_direction(payload.direction)
        
        # Get client name from existing messages if available
        existing_msg = db.query(Message).filter(Message.phone == payload.phone).first()
        client_name = existing_msg.client_name if existing_msg else None
        
        msg = Message(
            phone=payload.phone,
            client_name=client_name,
            direction=norm_direction,
            message=payload.message,
            media_url=None,
            automation="Dashboard",  # Mark as sent from dashboard
            timestamp=ts,
            follow_up_needed=payload.follow_up_needed or False,
            handled_by=payload.handled_by or "Dashboard User",
            notes=payload.notes or "",
        )
        
        db.add(msg)
        db.commit()
        db.refresh(msg)
        
        return {
            "status": "success",
            "message": "Message logged successfully",
            "id": msg.id
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to log message: {str(e)}")


@app.get("/contacts", response_model=List[ContactSummary])
def list_contacts(
    only_follow_up: bool = Query(False),
    db: Session = Depends(get_db),
):
    # Get all messages ordered by time; we'll collapse per phone in Python
    msgs = (
        db.query(Message)
        .order_by(Message.phone, Message.timestamp.desc())
        .all()
    )

    latest_per_phone: dict[str, Message] = {}
    followup_flags: dict[str, bool] = {}

    for m in msgs:
        if m.phone not in latest_per_phone:
            latest_per_phone[m.phone] = m
        if m.phone not in followup_flags:
            followup_flags[m.phone] = False
        if m.follow_up_needed:
            followup_flags[m.phone] = True

    contacts: List[ContactSummary] = []
    for phone, m in latest_per_phone.items():
        fu = followup_flags.get(phone, False)
        if only_follow_up and not fu:
            continue
        contacts.append(
            ContactSummary(
                phone=phone,
                client_name=m.client_name,
                last_message=m.message,
                last_time=m.timestamp,
                last_direction=m.direction,
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
    db: Session = Depends(get_db)
):
    """
    Get conversation messages with pagination support.
    Returns messages in ascending order (oldest first).
    """
    msgs = (
        db.query(Message)
        .filter(Message.phone == phone)
        .order_by(Message.timestamp.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [
        ConversationMessage(
            id=m.id,
            phone=m.phone,
            client_name=m.client_name,
            direction=m.direction,
            message=m.message,
            media_url=m.media_url,
            automation=m.automation,
            timestamp=m.timestamp,
            follow_up_needed=m.follow_up_needed,
            handled_by=m.handled_by,
            notes=m.notes,
        )
        for m in msgs
    ]


@app.patch("/message/{msg_id}", response_model=ConversationMessage)
def update_message(
    msg_id: int,
    payload: UpdateMessage,
    db: Session = Depends(get_db),
):
    msg = db.query(Message).filter(Message.id == msg_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    if payload.follow_up_needed is not None:
        msg.follow_up_needed = payload.follow_up_needed
    if payload.handled_by is not None:
        msg.handled_by = payload.handled_by
    if payload.notes is not None:
        msg.notes = payload.notes

    db.commit()
    db.refresh(msg)
    return msg


# ----------------------------------------------------------------------
# DELETE ENDPOINTS
# ----------------------------------------------------------------------

@app.delete("/message/{msg_id}", response_model=DeleteResponse)
def delete_message(msg_id: int, db: Session = Depends(get_db)):
    """Delete a single message by ID"""
    msg = db.query(Message).filter(Message.id == msg_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    
    db.delete(msg)
    db.commit()
    
    return DeleteResponse(
        success=True,
        message=f"Message {msg_id} deleted successfully",
        deleted_count=1
    )


@app.delete("/conversation/{phone}", response_model=DeleteResponse)
def delete_conversation(phone: str, db: Session = Depends(get_db)):
    """Delete all messages for a specific phone number"""
    msgs = db.query(Message).filter(Message.phone == phone).all()
    
    if not msgs:
        raise HTTPException(status_code=404, detail="No messages found for this phone number")
    
    count = len(msgs)
    
    for msg in msgs:
        db.delete(msg)
    
    db.commit()
    
    return DeleteResponse(
        success=True,
        message=f"Deleted {count} messages for {phone}",
        deleted_count=count
    )


@app.get("/")
def root():
    return {
        "message": "WhatsApp Chat Logger API",
        "version": "2.1",
        "endpoints": {
            "log": "POST /log",
            "log_message": "POST /log_message (for dashboard)",
            "contacts": "GET /contacts",
            "conversation": "GET /conversation/{phone}?limit=50&offset=0",
            "update": "PATCH /message/{msg_id}",
            "delete_message": "DELETE /message/{msg_id}",
            "delete_conversation": "DELETE /conversation/{phone}"
        }
    }
