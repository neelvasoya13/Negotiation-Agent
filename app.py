"""
FastAPI backend for Negotiation Chatbot.
Handles login, chat sessions, and LangGraph workflow execution.
"""
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from logger_config import get_logger

logger = get_logger("api")
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend import NegotiationState, workflow_maker
from db import fetch_builder_by_email_and_password

# Compile workflow once (uses NegotiationState)
graph_app = workflow_maker(NegotiationState)

# In-memory session store: session_token -> {thread_id, builder_info}
sessions: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="Negotiation Chatbot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:3000", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Request/Response models ---
class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    success: bool
    session_token: Optional[str] = None
    builder_name: Optional[str] = None
    error: Optional[str] = None


class ChatMessageRequest(BaseModel):
    message: str
    session_token: str


class ChatResponse(BaseModel):
    chat: list
    conversation_ended: bool
    error: Optional[str] = None


class StartChatRequest(BaseModel):
    session_token: str


# --- API routes ---
@app.post("/api/login", response_model=LoginResponse)
def login(req: LoginRequest):
    """Authenticate user and create session with builder_info."""
    logger.info("Login attempt: email=%s", req.email)
    builder = fetch_builder_by_email_and_password(req.email, req.password)
    if not builder:
        logger.warning("Login failed: invalid credentials for %s", req.email)
        return LoginResponse(success=False, error="Invalid email or password")

    session_token = str(uuid.uuid4())
    builder_info_dict = {
        "builder_id": builder.builder_id,
        "builder_name": builder.builder_name,
        "city": builder.city,
        "payment_history": builder.payment_history,
        "total_orders": builder.total_orders,
        "total_value": builder.total_value,
    }
    sessions[session_token] = {
        "thread_id": None,  # set on first chat start
        "builder_info": builder_info_dict,
    }
    logger.info("Login success: builder=%s, session_token=%s", builder.builder_name, session_token[:8])
    return LoginResponse(
        success=True,
        session_token=session_token,
        builder_name=builder.builder_name,
    )


def _get_session(session_token: str) -> Dict[str, Any]:
    if session_token not in sessions:
        logger.warning("Invalid session token: %s", session_token[:8] if session_token else "None")
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return sessions[session_token]


@app.post("/api/chat/start")
def start_chat(req: StartChatRequest):
    """Start a new chat. Creates thread and initializes graph with builder_info."""
    logger.info("Start chat request")
    sess = _get_session(req.session_token)
    thread_id = str(uuid.uuid4())
    sess["thread_id"] = thread_id

    # Initial state with builder_info (from login)
    initial_state = NegotiationState(
        builder_info=sess["builder_info"],
        chat_history_reply=[],
    ).model_dump()

    config = {"configurable": {"thread_id": thread_id}}

    # Invoke - will interrupt before User_input_1
    logger.debug("Invoking graph for thread_id=%s", thread_id)
    graph_app.invoke(initial_state, config=config)

    # Get state from checkpoint (invoke may return wrapped format)
    state_snapshot = graph_app.get_state(config)
    values = getattr(state_snapshot, "values", state_snapshot) if state_snapshot else {}
    chat = values.get("chat_history_reply", []) if isinstance(values, dict) else []
    ended = values.get("conversation_ended", False) if isinstance(values, dict) else False

    logger.info("Chat started: thread_id=%s", thread_id)
    return {"chat": chat, "conversation_ended": ended}


@app.post("/api/chat", response_model=ChatResponse)
def send_message(req: ChatMessageRequest):
    """Send user message and run graph. Returns updated chat and conversation_ended flag."""
    sess = _get_session(req.session_token)
    logger.info("Chat message: msg_len=%d, first_message=%s", len(req.message), not sess.get("thread_id"))
    thread_id = sess.get("thread_id")
    builder_info = sess["builder_info"]

    # First message: no thread yet, create thread and run full flow
    if not thread_id:
        thread_id = str(uuid.uuid4())
        sess["thread_id"] = thread_id
        initial_state = NegotiationState(
            builder_info=builder_info,
            chat_history_reply=[],
            last_user_message=req.message,
        ).model_dump()
        config = {"configurable": {"thread_id": thread_id}}
        try:
            graph_app.invoke(initial_state, config=config)  # Interrupts before User_input_1
            graph_app.invoke(None, config=config)  # Continue: run User_input_1 and rest of graph
        except Exception as e:
            logger.exception("Graph invoke error (first message): %s", e)
            return ChatResponse(chat=[], conversation_ended=False, error=str(e))
    else:
        config = {"configurable": {"thread_id": thread_id}}
        try:
            # With interrupt_before, resume requires: 1) update state with user message, 2) invoke(None) to continue
            state_snapshot = graph_app.get_state(config)
            values = getattr(state_snapshot, "values", {}) or {}
            current_chat = list(values.get("chat_history_reply") or [])
            current_chat.append({"role": "user", "content": req.message})
            graph_app.update_state(config, {"chat_history_reply": current_chat})
            graph_app.invoke(None, config=config)
        except Exception as e:
            logger.exception("Graph invoke error: %s", e)
            return ChatResponse(chat=[], conversation_ended=False, error=str(e))

    # Get state from checkpoint (reliable source after invoke)
    state_snapshot = graph_app.get_state(config)
    values = getattr(state_snapshot, "values", state_snapshot) if state_snapshot else {}
    chat = values.get("chat_history_reply", []) if isinstance(values, dict) else []
    ended = values.get("conversation_ended", False) if isinstance(values, dict) else False

    logger.info("Chat response: messages=%d, conversation_ended=%s", len(chat), ended)
    return ChatResponse(chat=chat, conversation_ended=ended)


@app.post("/api/chat/start-new")
def start_new_chat(req: StartChatRequest):
    """Reset conversation: new thread_id, clear checkpoint."""
    sess = _get_session(req.session_token)
    sess["thread_id"] = None
    logger.info("Start new chat: session reset")
    return {"chat": [], "conversation_ended": False}


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting API server on 0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
