import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .auth import TokenManager, hash_password, verify_password
from .browser_session import (
    BookingSessionManager,
    SessionBusyError,
    run_booking_session,
)
from .logging_config import configure_logging
from .models import BOOKING_TIME_LABELS, BookingRequest
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
from .verification import VerificationProvider, create_verification_provider
from .vnc_proxy import relay_vnc

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DEFAULT_DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
LOGIN_RETRY_AFTER_SECONDS = 15 * 60
BOOKING_SESSION_NOTICE = (
    "正在準備或等待你接手。請在畫面上完成台鐵官方驗證並自行按下訂票；"
    "系統不會辨識驗證碼，也不會替你按送出。"
)
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
    # How the chosen train reads on the task card and the VNC header, so the
    # person always sees which train the session is for.
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


class BookingSessionResponse(BaseModel):
    task_id: str
    session_url: str
    expires_at: str
    notice: str


class BookingResultResponse(BaseModel):
    task_id: str
    status: str
    booking_code: str | None = None
    message: str = ""


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
    verification_provider: VerificationProvider | None = None,
    *,
    start_scheduler: bool = True,
    automator_factory: Any | None = None,
) -> FastAPI:
    db = database or Database()
    tokens = token_manager or TokenManager()
    sessions = BookingSessionManager()
    scheduler = TaskScheduler(db, session_manager=sessions)
    ocr = ocr_service or OcrService()
    tra_ocr = TraOcrService(ocr)
    tdx = tdx_client or TdxClient()
    suggestion_service = SuggestionService(tdx)
    verification = verification_provider or create_verification_provider()
    bearer = HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            active = sessions.active
            if active is not None:
                active.request_stop()

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
        return {
            "status": "ok",
            "scheduler": "human-in-the-loop",
            "verification": verification.capabilities.mode.value,
        }

    @app.get("/verification/capabilities")
    def verification_capabilities() -> dict[str, str | bool]:
        return verification.capabilities.as_dict()

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

    @app.post("/tasks", response_model=TaskResponse, status_code=201)
    def create_task(body: TaskCreate, user: CurrentUser) -> TaskResponse:
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
        return _task_response(task)

    @app.get("/tasks", response_model=list[TaskResponse])
    def list_tasks(user: CurrentUser) -> list[TaskResponse]:
        return [_task_response(task) for task in db.list_tasks(user.id)]

    @app.post("/suggestions")
    def suggestions(body: SuggestionRequest, user: CurrentUser) -> dict[str, Any]:
        del user
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

    def _build_automator(booking_request: BookingRequest) -> Any:
        del booking_request
        if automator_factory is not None:
            return automator_factory()
        from .automation import TRCBookingAutomator

        return TRCBookingAutomator(headless=False, verification_provider=verification)

    def _start_booking(task_id: str, user_id: int) -> BookingSessionResponse:
        task = db.get_task(task_id, user_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        active = sessions.active
        if (
            active is not None and active.task_id == task_id and active.user_id == user_id
            and sessions.resolve(active.token) is not None
        ):
            return BookingSessionResponse(
                task_id=task_id,
                session_url=f"/booking-session/{active.token}/",
                expires_at=active.expires_at.isoformat(),
                notice=BOOKING_SESSION_NOTICE,
            )
        if task.status not in {"scheduled", "monitoring", "waiting_human"}:
            raise HTTPException(status_code=409, detail="Task is not open for booking")
        try:
            payload = db.get_task_payload(task_id, user_id)
            payload.pop("member_login", None)  # Older tasks may still contain saved credentials.
            booking = BookingRequest.from_dict(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        automator = _build_automator(booking)
        session = sessions.acquire(task_id, user_id)
        try:
            if task.monitor_until:
                session.expires_at = min(session.expires_at, datetime.fromisoformat(task.monitor_until))
            if not db.pause_monitoring(task_id, user_id, "waiting_human"):
                raise HTTPException(status_code=409, detail="Task was cancelled or its window ended")
        except Exception:
            sessions.release(session.token)
            raise

        def on_ready(ready: Any) -> None:
            record = db.get_task(ready.task_id, ready.user_id)
            if record and record.status == "waiting_human" and scheduler.notifier.enabled:
                scheduler.notifier.notify(record, payload)

        finish_lock = threading.RLock()
        finish_done = False

        def on_finish(finished: Any) -> None:
            nonlocal finish_done
            with finish_lock:
                if finish_done:
                    record = db.get_task(finished.task_id, finished.user_id)
                    # Recovery may unblock cleanup after a code was already
                    # read. Keep that late real result, but never retry twice.
                    if not finished.booking_code or not record or record.booking_code:
                        return
                finish_done = True
                finish_once(finished)

        def finish_once(finished: Any) -> None:
            try:
                updated = db.update_task_status(
                    finished.task_id,
                    finished.user_id,
                    finished.status,
                    last_error=finished.message if finished.status != "completed" else None,
                    booking_code=finished.booking_code,
                )
                if not updated:
                    record = db.get_task(finished.task_id, finished.user_id)
                    if record:
                        finished.status = record.status
                        finished.booking_code = record.booking_code
            finally:
                sessions.release(finished.token)
            # A round that produced no booking code means this attempt did not
            # get there, so the task goes back in the poll loop and prepares
            # again one interval later. Cancellation is the person saying stop,
            # and a booking code means there is nothing left to retry.
            if (
                finished.handed_off
                and finished.status in {"failed", "timeout"}
                and not finished.booking_code
            ):
                db.resume_monitoring(
                    finished.task_id,
                    finished.user_id,
                    delay_seconds=task.poll_interval_seconds,
                )
            record = db.get_task(finished.task_id, finished.user_id)
            active = sessions.active
            if (finished.booking_code and active is not None
                    and active.task_id == finished.task_id and active.token != finished.token):
                active.request_stop()
            if record and scheduler.notifier.enabled:
                try:
                    scheduler.notifier.notify_result(
                        record, finished.status, finished.booking_code
                    )
                except Exception:
                    logger.exception(
                        "booking result webhook failed",
                        extra={
                            "event": "notification.webhook_failed",
                            "task_id": finished.task_id,
                        },
                    )

        def recover() -> None:
            # A stopped Python thread cannot be killed safely. Reset its actual
            # browser first; a late worker callback must not finish a new round.
            with finish_lock:
                if finish_done:
                    return
                automator.reset_browser()
                if not session.booking_code:
                    session.status = "cancelled" if session.cancelled_by_user else "timeout"
                    session.message = "瀏覽器未正常退出，已重新啟動並結束本輪。"
                on_finish(session)

        session.recover = recover

        try:
            threading.Thread(
                target=run_booking_session,
                args=(session,),
                kwargs={
                    "automator": automator,
                    "request": booking,
                    "on_finish": on_finish,
                    "on_ready": on_ready,
                },
                name=f"booking-session-{task_id}",
                daemon=True,
            ).start()
        except Exception:
            session.status = "failed"
            session.message = "無法啟動訂票工作"
            on_finish(session)
            raise

        logger.info(
            "booking session started",
            extra={"event": "booking_session.started", "task_id": task_id},
        )
        return BookingSessionResponse(
            task_id=task_id,
            session_url=f"/booking-session/{session.token}/",
            expires_at=session.expires_at.isoformat(),
            notice=BOOKING_SESSION_NOTICE,
        )

    def prepare_scheduled_booking(task: TaskRecord) -> None:
        _start_booking(task.id, task.user_id)

    scheduler.prepare_booking = prepare_scheduled_booking

    @app.post(
        "/tasks/{task_id}/booking-session",
        response_model=BookingSessionResponse,
        status_code=201,
    )
    def start_booking_session(task_id: str, user: CurrentUser) -> BookingSessionResponse:
        try:
            return _start_booking(task_id, user.id)
        except SessionBusyError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"另一個訂票 session 進行中（任務 {exc.active_task_id}），"
                    f"約 {exc.remaining_seconds} 秒後釋放。一次只能解一個驗證。"
                ),
                headers={"Retry-After": str(max(1, exc.remaining_seconds))},
            ) from exc

    @app.get("/booking-session/{session_token}/verify", status_code=204)
    def verify_booking_session(session_token: str) -> Response:
        """nginx auth_request target guarding the noVNC stream."""
        if sessions.resolve(session_token) is None:
            raise HTTPException(status_code=403, detail="Session is invalid or expired")
        return Response(status_code=204)

    @app.websocket("/booking-session/{session_token}/websockify")
    async def booking_stream(websocket: WebSocket, session_token: str) -> None:
        host = urlsplit(os.getenv("TRA_BROWSER_CDP_URL", "")).hostname
        if not host:
            await websocket.close(code=1011)
            return
        await relay_vnc(websocket, sessions, session_token, host)

    @app.get("/tasks/{task_id}/booking-result", response_model=BookingResultResponse)
    def booking_result(
        task_id: str, user: CurrentUser,
        session_token: Annotated[str | None, Header(alias="X-Booking-Session")] = None,
    ) -> BookingResultResponse:
        task = db.get_task(task_id, user.id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        active = sessions.active
        if (active and active.task_id == task_id and active.user_id == user.id
                and (session_token is None or session_token == active.token)):
            return BookingResultResponse(
                task_id=task_id,
                status=active.status,
                booking_code=active.booking_code,
                message=active.message,
            )
        if session_token and task.status in {"scheduled", "monitoring", "waiting_human"}:
            return BookingResultResponse(
                task_id=task_id, status="ended", booking_code=None,
                message="本輪已結束，請關閉畫面；下一輪就緒後可從任務重新開啟。",
            )
        return BookingResultResponse(
            task_id=task_id,
            status=task.status,
            booking_code=task.booking_code,
            message=task.last_error or "",
        )

    @app.delete("/booking-session/{session_token}", status_code=204)
    def cancel_booking_session(session_token: str, user: CurrentUser) -> Response:
        session = sessions.resolve(session_token)
        if session is None or session.user_id != user.id:
            raise HTTPException(status_code=403, detail="Session is invalid or expired")
        db.update_task_status(session.task_id, user.id, "cancelled")
        session.request_stop()
        return Response(status_code=204)

    @app.delete("/tasks/{task_id}", status_code=204)
    def delete_task(task_id: str, user: CurrentUser) -> Response:
        task = db.get_task(task_id, user.id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        active = sessions.active
        if active is not None and active.task_id == task_id:
            raise HTTPException(
                status_code=409,
                detail="這個任務正在訂票中；請先關閉訂票畫面再刪除。",
            )
        db.delete_task(task_id, user.id)
        return Response(status_code=204)

    @app.post("/tasks/{task_id}/cancel", status_code=204)
    def cancel_task(
        task_id: str,
        user: CurrentUser,
    ) -> Response:
        task = db.get_task(task_id, user.id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        # monitoring belongs here: the dashboard offers 停止並取消任務 for it,
        # and the patrol loop makes it the state a task spends most time in.
        if task.status not in {"scheduled", "monitoring", "waiting_human"}:
            raise HTTPException(status_code=409, detail="Task cannot be cancelled")
        if not db.update_task_status(task_id, user.id, "cancelled"):
            raise HTTPException(status_code=409, detail="Task has already finished")
        active = sessions.active
        if active is not None and active.task_id == task_id and active.user_id == user.id:
            active.request_stop()
        return Response(status_code=204)

    app.state.database = db
    app.state.scheduler = scheduler
    app.state.tdx = tdx
    app.state.verification = verification
    app.state.booking_sessions = sessions
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
