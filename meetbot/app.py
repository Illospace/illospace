"""FastAPI surface for the standalone meetbot service."""

from __future__ import annotations

import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from meetbot.callback import MeetingWebhookCallback, MeetingWebhookSender
from meetbot.config import MeetbotConfig
from meetbot.engine import PlaywrightMeetEngine
from meetbot.models import MeetEngine, Origin, SessionStatus
from meetbot.session import (
    ActiveSessionError,
    SessionManager,
    SessionNotActiveError,
    SessionNotFoundError,
)

logger = logging.getLogger(__name__)

_MEET_URL = re.compile(
    r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(?:\?[^#\s]*)?"
)


class OriginRequest(BaseModel):
    channel: str = Field(default="", max_length=240)
    thread_ts: str = Field(default="", max_length=240)

    @field_validator("channel", "thread_ts")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class JoinRequest(BaseModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    meeting_url: str
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    origin: OriginRequest
    requested_by: str | None = Field(default=None, max_length=240)

    @field_validator("meeting_url")
    @classmethod
    def validate_meeting_url(cls, value: str) -> str:
        url = value.strip()
        if not _MEET_URL.fullmatch(url):
            raise ValueError("meeting_url must be a valid https://meet.google.com meeting URL")
        return url

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str | None) -> str | None:
        session_id = str(value or "").strip()
        if not session_id:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id):
            raise ValueError(
                "session_id must contain only letters, numbers, underscores, and hyphens"
            )
        return session_id

    @field_validator("display_name", "requested_by", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> str | None:
        text = str(value or "").strip()
        return text or None


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("text must not be blank")
        return text


class JoinResponse(BaseModel):
    session_id: str
    status: SessionStatus


class SessionResponse(BaseModel):
    session_id: str
    status: SessionStatus
    meeting_url: str
    joined_at: str | None
    caption_lines: int
    transcript_path: str
    error: str | None
    warning: str | None
    end_reason: str | None


class ActionResponse(BaseModel):
    session_id: str
    status: SessionStatus


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    commit: str


def create_app(
    *,
    config: MeetbotConfig | None = None,
    engine: MeetEngine | None = None,
    webhook_sender: MeetingWebhookSender | None = None,
) -> FastAPI:
    """Create an app with injectable browser and webhook boundaries."""

    resolved_config = config or MeetbotConfig.from_env()
    build_commit = os.getenv("ILLO_BUILD_COMMIT", "unknown").strip() or "unknown"
    resolved_engine = engine or PlaywrightMeetEngine(resolved_config)
    resolved_sender = webhook_sender or MeetingWebhookCallback(resolved_config)
    manager = SessionManager(resolved_config, resolved_engine, resolved_sender)

    if not resolved_config.api_token:
        logger.warning(
            "ILLO_MEETBOT_TOKEN is unset; meetbot HTTP API authentication is disabled."
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await manager.shutdown()

    application = FastAPI(title="Illospace Meetbot", lifespan=lifespan)
    application.state.session_manager = manager

    async def require_token(
        token: Annotated[str | None, Header(alias="X-Meetbot-Token")] = None,
    ) -> None:
        expected = resolved_config.api_token
        if expected is None:
            return
        if token is None or not secrets.compare_digest(token, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing meetbot token.",
            )

    protected = [Depends(require_token)]

    @application.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(commit=build_commit)

    @application.post(
        "/join",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=JoinResponse,
        dependencies=protected,
    )
    async def join(request: JoinRequest) -> JoinResponse | JSONResponse:
        try:
            record = await manager.join(
                session_id=request.session_id,
                meeting_url=request.meeting_url,
                display_name=request.display_name,
                origin=Origin(
                    channel=request.origin.channel,
                    thread_ts=request.origin.thread_ts,
                ),
                requested_by=request.requested_by,
            )
        except ActiveSessionError as exc:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"active_session_id": exc.session_id},
            )
        return JoinResponse(session_id=record.session_id, status=record.status)

    @application.get(
        "/sessions/{session_id}",
        response_model=SessionResponse,
        dependencies=protected,
    )
    async def get_session(session_id: str) -> SessionResponse:
        try:
            record = manager.get(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Meeting session not found.") from exc
        return SessionResponse.model_validate(record.status_response())

    @application.post(
        "/sessions/{session_id}/leave",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ActionResponse,
        dependencies=protected,
    )
    async def leave_session(session_id: str) -> ActionResponse:
        try:
            record = await manager.leave(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Meeting session not found.") from exc
        return ActionResponse(session_id=record.session_id, status=record.status)

    @application.post(
        "/sessions/{session_id}/chat",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ActionResponse,
        dependencies=protected,
    )
    async def send_chat(session_id: str, request: ChatRequest) -> ActionResponse:
        try:
            record = await manager.chat(session_id, request.text)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Meeting session not found.") from exc
        except (SessionNotActiveError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ActionResponse(session_id=record.session_id, status=record.status)

    return application


app = create_app()
