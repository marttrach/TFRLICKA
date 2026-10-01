import hmac
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.routing import Route

from .agent import INSTRUCTIONS as AGENT_INSTRUCTIONS
from .agent import BearerGuard
from .auth import TokenManager, hash_password, verify_password
from .logging_config import configure_logging
from .models import BOOKING_TIME_LABELS, TAIWAN_TZ, BookingRequest
from .ocr import MAX_IMAGE_BYTES, OcrService
from .scheduler import TaskScheduler
from .storage import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    MODE_BOOK_WHEN_AVAILABLE,
    TASK_MODES,
    Database,
    TaskRecord,
    UserRecord,
)
from .suggestions import SuggestionService
from .tdx import TdxClient, TdxError
from .tra_ocr import TraOcrService

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DEFAULT_DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
LOGIN_RETRY_AFTER_SECONDS = 15 * 60
OPEN_STATUSES = frozenset({"scheduled", "monitoring", "waiting_human"})
logger = logging.getLogger(__name__)
DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")


class LoginCredentials(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class RegistrationCredentials(LoginCredentials):
    password: str = Field(min_length=12, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    email: str
    created_at: str


# The start-time picker is a datetime-local, which cannot express seconds, so
# choosing the current minute yields a time up to 59 seconds past. Refusing that
# would reject a choice the person had no way to avoid making.
START_TIME_GRACE = timedelta(minutes=1)


class TaskCreate(BaseModel):
    # When monitoring starts. Named scheduled_at since before monitoring
    # existed; it has always been a start time, never an interval.
    # Omitted means "start now", stamped here rather than by the caller: a
    # caller's clock is not this clock, and the difference is not knowable.
    scheduled_at: datetime | None = None
    booking: dict[str, Any]
    # Accepted for older clients; booking no longer logs into a TRC account.
    use_saved_member_login: bool = Field(default=False, deprecated=True)
    # Preferred over sending the identity in `booking`: the number is looked up
    # server-side so it never has to round-trip through the browser.
    traveler_id: int | None = None
    # How the chosen train reads on the task card and in reminders.
    train_label: str = Field(default="", max_length=120)
    mode: str = MODE_BOOK_WHEN_AVAILABLE
    poll_interval_seconds: int = Field(
        default=DEFAULT_POLL_INTERVAL_SECONDS, ge=MIN_POLL_INTERVAL_SECONDS, le=86_400
    )
    # Omitted means retry until booked or cancelled (monitor_only still reminds once).
    monitor_until: datetime | None = None


class TravelerCreate(BaseModel):
    label: str = Field(min_length=1, max_length=32)
    identity: str = Field(min_length=1, max_length=32)


class TravelerResponse(BaseModel):
    id: int
    label: str
    identity: str
    updated_at: str


class TaskResponse(BaseModel):
    id: str
    status: str
    scheduled_at: str
    monitor_start_at: str
    route: str
    ride_date: str
    order_type: str
    created_at: str
    updated_at: str
    last_error: str | None
    booking_code: str | None = None
    mode: str = MODE_BOOK_WHEN_AVAILABLE
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    monitor_until: str | None = None
    last_checked_at: str | None = None
    next_check_at: str | None = None
    train_label: str | None = None
    # No authorised TRA seat-availability source exists (PLAN.md 7.1), so this
    # is always "unknown". It is a field rather than a silence so the UI has to
    # say so out loud instead of implying a seat was found.
    availability: str = "unknown"
    availability_note: str = "餘票資料來源尚未提供，系統無法得知是否有位"


class MemberProfileUpdate(BaseModel):
    # Identities live in /travelers now. This stays accepted so older clients
    # keep working; when omitted, the stored value is preserved.
    identity: str = Field(default="", max_length=32)
    member_account: str = Field(default="", max_length=64)
    member_password: str = Field(default="", max_length=128)


class MemberProfileResponse(BaseModel):
    identity: str
    member_account: str
    has_member_password: bool
    updated_at: str | None


class OcrResponse(BaseModel):
    text: str
    language: str
    width: int
    height: int


class TraDocumentFieldsResponse(BaseModel):
    document_type: str
    train_numbers: list[str]
    stations: list[str]
    dates: list[str]
    times: list[str]
    route: str | None


class TraOcrResponse(OcrResponse):
    fields: TraDocumentFieldsResponse
    warnings: list[str]


class BookedReport(BaseModel):
    booking_code: str = Field(min_length=4, max_length=32, pattern=r"^[A-Za-z0-9-]+$")


class SuggestionPreferences(BaseModel):
    prefer_reserved: bool = True
    include_transfers: bool = True


class SuggestionRequest(BaseModel):
    start_station: str = Field(min_length=1, max_length=64)
    end_station: str = Field(min_length=1, max_length=64)
    ride_date: str = Field(min_length=8, max_length=10)
    start_time: str
    end_time: str
    preferences: SuggestionPreferences = Field(default_factory=SuggestionPreferences)


# Offline fallback used when TDX has never been reachable. Counties are filled
# in so the two-level picker still works without credentials.
POPULAR_STATIONS = [
    {"value": "0900-基隆", "label": "基隆", "county": "基隆市"},
    {"value": "1000-臺北", "label": "臺北", "county": "臺北市"},
    {"value": "1020-板橋", "label": "板橋", "county": "新北市"},
    {"value": "1080-桃園", "label": "桃園", "county": "桃園市"},
    {"value": "1210-新竹", "label": "新竹", "county": "新竹市"},
    {"value": "3340-新烏日", "label": "新烏日", "county": "臺中市"},
    {"value": "3300-臺中", "label": "臺中", "county": "臺中市"},
    {"value": "3360-彰化", "label": "彰化", "county": "彰化縣"},
    {"value": "4080-嘉義", "label": "嘉義", "county": "嘉義市"},
    {"value": "4220-臺南", "label": "臺南", "county": "臺南市"},
    {"value": "4340-新左營", "label": "新左營", "county": "高雄市"},
    {"value": "4400-高雄", "label": "高雄", "county": "高雄市"},
    {"value": "7000-花蓮", "label": "花蓮", "county": "花蓮縣"},
    {"value": "6000-臺東", "label": "臺東", "county": "臺東縣"},
]


def _task_response(task: TaskRecord) -> TaskResponse:
    data = asdict(task)
    data.pop("check_failures", None)
    return TaskResponse(monitor_start_at=task.scheduled_at, **data)


def _cors_origins() -> list[str]:
    configured = os.getenv("TRA_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return origins or list(DEFAULT_DEV_ORIGINS)


def create_app(
    database: Database | None = None,
    token_manager: TokenManager | None = None,
    ocr_service: OcrService | None = None,
    tdx_client: TdxClient | None = None,
    *,
    start_scheduler: bool = True,
) -> FastAPI:
    db = database or Database()
    tokens = token_manager or TokenManager()
    scheduler = TaskScheduler(db)
    ocr = ocr_service or OcrService()
    tra_ocr = TraOcrService(ocr)
    tdx = tdx_client or TdxClient()
    suggestion_service = SuggestionService(tdx)
    bearer = HTTPBearer(auto_error=False)
    # Stateless: every agent call stands alone, so a restart drops nothing.
    agent = FastMCP(
        "tra-sniper",
        instructions=AGENT_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        # The bearer token is the gate. Host checks would only reject the
        # container name or NAS address the agent dials.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_scheduler:
            scheduler.start()
        try:
            async with agent.session_manager.run():
                yield
        finally:
            scheduler.stop()

    app = FastAPI(
        title="TRA-Sniper API",
        version="0.9.0",
        description="Accessible membership, booking task, and timetable suggestion API.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    BearerCredentials = Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer),
    ]

    def current_user(credentials: BearerCredentials) -> UserRecord:
        if not credentials:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
        try:
            claims = tokens.verify(credentials.credentials)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            ) from exc
        user = db.get_user(claims.user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        if claims.version != user.token_version:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
            )
        return user

    CurrentUser = Annotated[UserRecord, Depends(current_user)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "scheduler": "human-in-the-loop"}

    @app.get("/stations")
    def stations() -> list[dict[str, str]]:
        return tdx.stations(POPULAR_STATIONS)

    @app.get("/times")
    def times() -> list[str]:
        return list(BOOKING_TIME_LABELS)

    @app.post("/auth/register", response_model=TokenResponse, status_code=201)
    def register(body: RegistrationCredentials) -> TokenResponse:
        email = body.email.strip().lower()
        if not EMAIL_PATTERN.fullmatch(email):
            raise HTTPException(status_code=422, detail="Invalid email address")
        try:
            user = db.create_user(email, hash_password(body.password))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TokenResponse(access_token=tokens.issue(user.id, user.token_version))

    @app.post("/auth/login", response_model=TokenResponse)
    def login(body: LoginCredentials) -> TokenResponse:
        email = body.email.strip().lower()
        if db.is_login_locked(email):
            logger.warning("login temporarily locked", extra={"event": "auth.login_locked"})
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts; try again later",
                headers={"Retry-After": str(LOGIN_RETRY_AFTER_SECONDS)},
            )
        user = db.get_user_by_email(email)
        password_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
        if not verify_password(body.password, password_hash) or not user:
            db.record_login_attempt(email, succeeded=False)
            if db.is_login_locked(email):
                logger.warning("login temporarily locked", extra={"event": "auth.login_locked"})
                raise HTTPException(
                    status_code=429,
                    detail="Too many failed login attempts; try again later",
                    headers={"Retry-After": str(LOGIN_RETRY_AFTER_SECONDS)},
                )
            raise HTTPException(status_code=401, detail="Invalid email or password")
        db.record_login_attempt(email, succeeded=True)
        return TokenResponse(access_token=tokens.issue(user.id, user.token_version))

    @app.get("/auth/me", response_model=UserResponse)
    def me(user: CurrentUser) -> UserResponse:
        return UserResponse(id=user.id, email=user.email, created_at=user.created_at)

    @app.post("/auth/logout", status_code=204)
    def logout(user: CurrentUser) -> Response:
        db.revoke_user_tokens(user.id)
        logger.info("user tokens revoked", extra={"event": "auth.logout"})
        return Response(status_code=204)

    @app.get("/profile", response_model=MemberProfileResponse)
    def get_profile(user: CurrentUser) -> MemberProfileResponse:
        profile = db.get_member_profile(user.id)
        if not profile:
            return MemberProfileResponse(
                identity="", member_account="", has_member_password=False, updated_at=None
            )
        return MemberProfileResponse(
            identity=profile.identity,
            member_account=profile.member_account,
            has_member_password=bool(profile.member_password),
            updated_at=profile.updated_at,
        )

    @app.put("/profile", response_model=MemberProfileResponse)
    def save_profile(body: MemberProfileUpdate, user: CurrentUser) -> MemberProfileResponse:
        existing = db.get_member_profile(user.id)
        account = body.member_account.strip()
        password = body.member_password or (existing.member_password if existing else "")
        if bool(account) != bool(password):
            raise HTTPException(
                status_code=422,
                detail="台鐵會員帳號與密碼必須同時設定；若不使用會員登入，兩欄都留空。",
            )
        profile = db.save_member_profile(
            user.id,
            identity=body.identity or (existing.identity if existing else ""),
            member_account=account,
            member_password=password,
        )
        return MemberProfileResponse(
            identity=profile.identity,
            member_account=profile.member_account,
            has_member_password=bool(profile.member_password),
            updated_at=profile.updated_at,
        )

    @app.delete("/profile", status_code=204)
    def delete_profile(user: CurrentUser) -> Response:
        db.delete_member_profile(user.id)
        return Response(status_code=204)

    @app.delete("/profile/member-login", status_code=204)
    def clear_member_login(user: CurrentUser) -> Response:
        db.clear_member_login(user.id)
        return Response(status_code=204)

    @app.get("/travelers", response_model=list[TravelerResponse])
    def list_travelers(user: CurrentUser) -> list[TravelerResponse]:
        return [
            TravelerResponse(**asdict(traveler))
            for traveler in db.list_travelers(user.id)
        ]

    @app.post("/travelers", response_model=TravelerResponse, status_code=201)
    def create_traveler(body: TravelerCreate, user: CurrentUser) -> TravelerResponse:
        try:
            traveler = db.create_traveler(
                user.id, label=body.label, identity=body.identity
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TravelerResponse(**asdict(traveler))

    @app.put("/travelers/{traveler_id}", response_model=TravelerResponse)
    def update_traveler(
        traveler_id: int, body: TravelerCreate, user: CurrentUser
    ) -> TravelerResponse:
        try:
            traveler = db.update_traveler(
                traveler_id, user.id, label=body.label, identity=body.identity
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if traveler is None:
            raise HTTPException(status_code=404, detail="常用資料不存在")
        return TravelerResponse(**asdict(traveler))

    @app.delete("/travelers/{traveler_id}", status_code=204)
    def delete_traveler(traveler_id: int, user: CurrentUser) -> Response:
        if not db.delete_traveler(traveler_id, user.id):
            raise HTTPException(status_code=404, detail="常用資料不存在")
        return Response(status_code=204)

    def _create_task(body: TaskCreate, user: UserRecord) -> TaskRecord:
        booking_payload = dict(body.booking)
        booking_payload.pop("member_login", None)
        if body.traveler_id is not None:
            traveler = db.get_traveler(body.traveler_id, user.id)
            if traveler is None:
                raise HTTPException(status_code=404, detail="常用資料不存在")
            booking_payload["identity"] = traveler.identity
        if not str(booking_payload.get("identity", "")).strip():
            profile = db.get_member_profile(user.id)
            if profile:
                booking_payload["identity"] = profile.identity
        try:
            booking = BookingRequest.from_dict(booking_payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        scheduled_at = body.scheduled_at
        if scheduled_at is None:
            scheduled_at = datetime.now(UTC)
        elif scheduled_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="scheduled_at must include a timezone")
        elif scheduled_at.astimezone(UTC) < datetime.now(UTC) - START_TIME_GRACE:
            raise HTTPException(status_code=422, detail="scheduled_at cannot be in the past")
        if body.mode not in TASK_MODES:
            raise HTTPException(
                status_code=422, detail=f"mode must be one of: {', '.join(sorted(TASK_MODES))}"
            )
        monitor_until = body.monitor_until
        if monitor_until is not None:
            if monitor_until.tzinfo is None:
                raise HTTPException(
                    status_code=422, detail="monitor_until must include a timezone"
                )
            if monitor_until <= scheduled_at:
                raise HTTPException(
                    status_code=422, detail="monitor_until must be after the start time"
                )
        task = db.create_task(
            user.id,
            booking,
            scheduled_at.astimezone(UTC).isoformat(),
            booking_payload,
            mode=body.mode,
            poll_interval_seconds=body.poll_interval_seconds,
            monitor_until=monitor_until.astimezone(UTC).isoformat() if monitor_until else None,
            train_label=body.train_label.strip() or None,
        )
        return task

    @app.post("/tasks", response_model=TaskResponse, status_code=201)
    def create_task(body: TaskCreate, user: CurrentUser) -> TaskResponse:
        return _task_response(_create_task(body, user))

    @app.get("/tasks", response_model=list[TaskResponse])
    def list_tasks(user: CurrentUser) -> list[TaskResponse]:
        return [_task_response(task) for task in db.list_tasks(user.id)]

    def _suggest(body: SuggestionRequest) -> dict[str, Any]:
        if body.start_time not in BOOKING_TIME_LABELS or body.end_time not in BOOKING_TIME_LABELS:
            raise HTTPException(status_code=422, detail="請選擇有效的開始與結束時段")
        if body.start_time >= body.end_time:
            raise HTTPException(status_code=422, detail="開始時段必須早於結束時段，請調整後重新查詢")
        if body.start_station == body.end_station:
            raise HTTPException(status_code=422, detail="出發站與抵達站不可相同，請重新選擇")
        try:
            date.fromisoformat(body.ride_date.replace("/", "-"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="乘車日期格式不正確，請重新選擇日期") from exc
        try:
            return suggestion_service.suggest(
                start_station=body.start_station,
                end_station=body.end_station,
                ride_date=body.ride_date,
                start_time=body.start_time,
                end_time=body.end_time,
                prefer_reserved=body.preferences.prefer_reserved,
                include_transfers=body.preferences.include_transfers,
            )
        except TdxError as exc:
            raise HTTPException(
                status_code=503,
                detail="TDX 時刻表暫時不可用，請稍後重試，或改用「直接輸入車次」",
            ) from exc

    @app.post("/suggestions")
    def suggestions(body: SuggestionRequest, user: CurrentUser) -> dict[str, Any]:
        del user
        return _suggest(body)

    @app.post("/ocr", response_model=OcrResponse)
    async def recognize_image(
        user: CurrentUser,
        image: Annotated[UploadFile, File(description="PNG, JPEG, or WebP image")],
        language: Annotated[str, Form()] = "zh-TW",
    ) -> OcrResponse:
        del user  # Authentication is required; OCR results are not persisted.
        if image.content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise HTTPException(status_code=415, detail="Only PNG, JPEG, and WebP are supported")
        image_data = await image.read(MAX_IMAGE_BYTES + 1)
        await image.close()
        if len(image_data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image exceeds the 8 MB limit")
        try:
            started_at = time.perf_counter()
            result = ocr.recognize(image_data, language)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        logger.info(
            "OCR image processed",
            extra={
                "event": "ocr.completed",
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        )
        return OcrResponse(
            text=result.text,
            language=result.language,
            width=result.width,
            height=result.height,
        )

    @app.post("/ocr/tra", response_model=TraOcrResponse)
    async def recognize_tra_document(
        user: CurrentUser,
        image: Annotated[
            UploadFile,
            File(description="TRA ticket, booking-result, or timetable screenshot"),
        ],
        language: Annotated[str, Form()] = "zh-TW",
    ) -> TraOcrResponse:
        del user  # Authentication is required; images and results are not persisted.
        if image.content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise HTTPException(status_code=415, detail="Only PNG, JPEG, and WebP are supported")
        image_data = await image.read(MAX_IMAGE_BYTES + 1)
        await image.close()
        if len(image_data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image exceeds the 8 MB limit")
        cached_stations = (
            tdx.load_cached_stations() if hasattr(tdx, "load_cached_stations") else []
        )
        station_records = cached_stations or POPULAR_STATIONS
        try:
            started_at = time.perf_counter()
            result = tra_ocr.recognize(
                image_data,
                language=language,
                station_names=[item["label"] for item in station_records],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        logger.info(
            "TRA document OCR processed",
            extra={
                "event": "ocr.tra_completed",
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        )
        return TraOcrResponse(
            text=result.text,
            language=result.language,
            width=result.width,
            height=result.height,
            fields=TraDocumentFieldsResponse(
                document_type=result.fields.document_type,
                train_numbers=list(result.fields.train_numbers),
                stations=list(result.fields.stations),
                dates=list(result.fields.dates),
                times=list(result.fields.times),
                route=result.fields.route,
            ),
            warnings=list(result.warnings),
        )

    @app.get("/tasks/{task_id}/config")
    def task_config(task_id: str, user: CurrentUser, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return db.get_task_payload(task_id, user.id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc

    @app.get("/tasks/{task_id}/suggestions")
    def task_suggestions(task_id: str, user: CurrentUser) -> dict[str, Any]:
        try:
            payload = db.get_task_payload(task_id, user.id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        suggestions = payload.get("candidate_suggestions")
        return suggestions if isinstance(suggestions, dict) else {
            "primary": [],
            "alternatives": [],
            "transfers": [],
            "availability_known": False,
        }

    def _official_link(task_id: str, user_id: int) -> tuple[str, str]:
        """Ask TDX for the official pre-filled page; returns (url, train number)."""
        try:
            booking = BookingRequest.from_dict(db.get_task_payload(task_id, user_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not booking.outbound.train_numbers:
            raise HTTPException(status_code=422, detail="這個任務沒有指定車次，無法產生官方訂票連結")
        if not tdx.configured:
            raise HTTPException(status_code=409, detail="尚未設定 TDX 金鑰，無法產生官方訂票連結")
        # The official page has three train fields, so the first one is enough
        # to land there; each extra link would cost another TDX call.
        train_no = booking.outbound.train_numbers[0]
        try:
            url = tdx.booking_link(
                booking.start_station,
                booking.end_station,
                train_no,
                booking.outbound.ride_date,
                booking.quantity,
            )
        except TdxError as exc:
            raise HTTPException(
                status_code=503,
                detail="TDX 無法產生官方訂票連結；請確認帳號已開通「臺鐵訂票導訂」，或稍後重試",
            ) from exc
        return url, train_no

    @app.post("/tasks/{task_id}/booking-link")
    def task_booking_link(task_id: str, user: CurrentUser, response: Response) -> dict[str, str]:
        """A TDX link to the official page, pre-filled, for the person's own browser."""
        response.headers["Cache-Control"] = "no-store"
        url, train_no = _official_link(task_id, user.id)
        return {"url": url, "train_no": train_no}

    def _open_link_signature(task_id: str, user_id: int) -> str:
        return tokens.sign(f"booking-link:{user_id}:{task_id}")

    def _open_link_url(task: TaskRecord) -> str | None:
        """The link a notification carries: stable, and redirects to a fresh TDX link.

        TDX links expire within minutes, far sooner than a message gets read,
        so the notification points here and the TDX link is issued on the tap.
        """
        if not tdx.configured:
            return None
        signature = _open_link_signature(task.id, task.user_id)
        # "/api" is where frontend/nginx.conf mounts this API on the public origin.
        return (
            f"{scheduler.notifier.public_url}/api/tasks/{task.id}/booking-link/open"
            f"?u={task.user_id}&sig={signature}"
        )

    @app.get("/tasks/{task_id}/booking-link/open")
    def open_booking_link(task_id: str, u: int, sig: str) -> Response:
        """Opened from a notification, so the signature stands in for the login."""
        if not hmac.compare_digest(sig, _open_link_signature(task_id, u)):
            raise HTTPException(status_code=403, detail="連結無效")
        task = db.get_task(task_id, u)
        if not task or task.status not in OPEN_STATUSES:
            raise HTTPException(status_code=410, detail="這個任務已結束，連結不再有效")
        url, _ = _official_link(task_id, u)
        return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})

    scheduler.notifier.booking_url_for = _open_link_url

    def _cancel_task(task_id: str, user_id: int) -> None:
        task = db.get_task(task_id, user_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in OPEN_STATUSES:
            raise HTTPException(status_code=409, detail="Task cannot be cancelled")
        if not db.update_task_status(task_id, user_id, "cancelled"):
            raise HTTPException(status_code=409, detail="Task has already finished")

    def _report_booked(task_id: str, user_id: int, booking_code: str) -> TaskRecord:
        """Record the code the person got on the official page; that ends the task."""
        if not db.get_task(task_id, user_id):
            raise HTTPException(status_code=404, detail="Task not found")
        # Allowed from any state: a task cancelled here may still have been
        # booked by hand, and update_task_status lets a code complete it.
        db.update_task_status(task_id, user_id, "completed", booking_code=booking_code.upper())
        task = db.get_task(task_id, user_id)
        assert task is not None
        if scheduler.notifier.enabled:
            try:
                scheduler.notifier.notify_result(task, "completed", task.booking_code)
            except Exception:
                logger.exception(
                    "booking result webhook failed",
                    extra={"event": "notification.webhook_failed", "task_id": task_id},
                )
        return task

    @app.post("/tasks/{task_id}/booked", response_model=TaskResponse)
    def report_booked(task_id: str, body: BookedReport, user: CurrentUser) -> TaskResponse:
        return _task_response(_report_booked(task_id, user.id, body.booking_code))

    @app.delete("/tasks/{task_id}", status_code=204)
    def delete_task(task_id: str, user: CurrentUser) -> Response:
        task = db.get_task(task_id, user.id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        db.delete_task(task_id, user.id)
        return Response(status_code=204)

    @app.post("/tasks/{task_id}/cancel", status_code=204)
    def cancel_task(
        task_id: str,
        user: CurrentUser,
    ) -> Response:
        _cancel_task(task_id, user.id)
        return Response(status_code=204)

    app.state.database = db
    app.state.scheduler = scheduler

    # ---- MCP tools for an AI agent ------------------------------------------
    # The agent acts as the dashboard account named by TRA_AGENT_EMAIL. Errors
    # surface to it as tool errors carrying the same message the dashboard shows.

    def _agent_user() -> UserRecord:
        user = db.get_user_by_email(os.getenv("TRA_AGENT_EMAIL", "").strip().lower())
        if not user:
            raise ValueError("TRA_AGENT_EMAIL 沒有對應的帳號；請先在儀表板註冊這個 email")
        return user

    def _as_tool_error(call: Any) -> Any:
        try:
            return call()
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc

    def _agent_task(task: TaskRecord) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "status": task.status,
            "route": task.route,
            "ride_date": task.ride_date,
            "train": task.train_label,
            "booking_code": task.booking_code,
            "next_reminder_at": task.next_check_at,
            "monitor_until": task.monitor_until,
            "booking_url": _open_link_url(task) if task.status in OPEN_STATUSES else None,
        }

    @agent.tool()
    def find_stations(keyword: str) -> list[dict[str, str]]:
        """Find TRA stations by Chinese name, e.g. "板橋". Pass the returned
        `value` (like "1020-板橋") to the other tools."""
        keyword = keyword.strip().replace("台", "臺")
        found = [
            station for station in tdx.stations(POPULAR_STATIONS)
            if keyword and (keyword in station["label"] or keyword in station["value"])
        ]
        return found[:20]

    @agent.tool()
    def search_trains(
        from_station: str,
        to_station: str,
        date: str,
        start_time: str = "00:00",
        end_time: str = "23:59",
    ) -> list[dict[str, Any]]:
        """List trains departing between start_time and end_time on date
        (YYYY-MM-DD, Taiwan time). Times are on the half hour, or 23:59.
        Stations are `value`s from find_stations. Says nothing about free seats."""
        body = SuggestionRequest(
            start_station=from_station, end_station=to_station, ride_date=date,
            start_time=start_time, end_time=end_time,
            preferences=SuggestionPreferences(include_transfers=False),
        )
        result = _as_tool_error(lambda: _suggest(body))
        trains = [*result["primary"], *result["alternatives"]]
        return sorted(
            (
                {key: item[key] for key in (
                    "train_no", "train_type_name", "departure_time", "arrival_time",
                    "duration_minutes", "is_reserved_type",
                )}
                for item in trains
            ),
            key=lambda item: item["departure_time"],
        )

    @agent.tool()
    def create_booking_task(
        from_station: str,
        to_station: str,
        date: str,
        train_numbers: list[str],
        quantity: int = 1,
        start_at: str | None = None,
        remind_every_minutes: int = 5,
        monitor_until: str | None = None,
        remind_once: bool = False,
    ) -> dict[str, Any]:
        """Watch one to three train numbers on date (YYYY-MM-DD) for 1-6 tickets.

        From start_at (ISO time, Taiwan time if no offset; default now) it sends
        a webhook reminder every remind_every_minutes (min 1) until booked,
        cancelled or monitor_until; remind_once sends a single reminder. The
        returned booking_url opens the pre-filled official page any time the
        task is open, so it can be sent to the person straight away."""
        def moment(value: str | None) -> datetime | None:
            if not value:
                return None
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=TAIWAN_TZ)

        body = TaskCreate(
            scheduled_at=moment(start_at),
            booking={
                "start_station": from_station,
                "end_station": to_station,
                "quantity": quantity,
                "outbound": {"ride_date": date.replace("-", "/"), "train_numbers": train_numbers},
            },
            train_label="、".join(train_numbers),
            mode="monitor_only" if remind_once else MODE_BOOK_WHEN_AVAILABLE,
            poll_interval_seconds=max(remind_every_minutes, 1) * 60,
            monitor_until=moment(monitor_until),
        )
        user = _agent_user()
        return _agent_task(_as_tool_error(lambda: _create_task(body, user)))

    @agent.tool(name="list_tasks")
    def agent_list_tasks() -> list[dict[str, Any]]:
        """All booking tasks, newest first, with status and booking_url."""
        return [_agent_task(task) for task in db.list_tasks(_agent_user().id)]

    @agent.tool()
    def get_booking_link(task_id: str) -> dict[str, str]:
        """A fresh official booking link for the task, to send to the person now.
        It expires within minutes; the person enters their ID, passes the
        verification and presses 訂票 themselves."""
        user = _agent_user()
        url, train_no = _as_tool_error(lambda: _official_link(task_id, user.id))
        return {"official_url": url, "train_no": train_no}

    @agent.tool(name="report_booked")
    def agent_report_booked(task_id: str, booking_code: str) -> dict[str, Any]:
        """Record the booking code (電腦代碼) the person got. Ends the reminders."""
        BookedReport(booking_code=booking_code)  # same validation as the dashboard
        user = _agent_user()
        return _agent_task(_as_tool_error(lambda: _report_booked(task_id, user.id, booking_code)))

    @agent.tool(name="cancel_task")
    def agent_cancel_task(task_id: str) -> str:
        """Stop a task's reminders for good."""
        user = _agent_user()
        _as_tool_error(lambda: _cancel_task(task_id, user.id))
        return "cancelled"

    # Mounted as a plain route so /mcp answers without a trailing-slash redirect.
    mcp_endpoint = agent.streamable_http_app().routes[0].endpoint
    app.router.routes.append(
        Route(
            "/mcp",
            BearerGuard(mcp_endpoint, os.getenv("TRA_AGENT_TOKEN", "")),
            methods=["GET", "POST", "DELETE"],
        )
    )

    app.state.tdx = tdx
    return app


app = create_app()


def run() -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(
        "tra_sniper.api:app",
        host=os.getenv("TRA_API_HOST", "0.0.0.0"),
        port=int(os.getenv("TRA_API_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    run()
