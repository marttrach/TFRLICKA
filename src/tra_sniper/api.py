import logging
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .auth import TokenManager, hash_password, verify_password
from .logging_config import configure_logging
from .models import BOOKING_TIME_LABELS, BookingRequest
from .ocr import MAX_IMAGE_BYTES, OcrService
from .scheduler import TaskScheduler
from .storage import Database, TaskRecord, UserRecord
from .suggestions import SuggestionService
from .tdx import TdxClient, TdxError
from .tra_ocr import TraOcrService
from .verification import VerificationProvider, create_verification_provider

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DEFAULT_DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
LOGIN_RETRY_AFTER_SECONDS = 15 * 60
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


class TaskCreate(BaseModel):
    scheduled_at: datetime
    booking: dict[str, Any]
    use_saved_member_login: bool = False


class TaskResponse(BaseModel):
    id: str
    status: str
    scheduled_at: str
    route: str
    ride_date: str
    order_type: str
    created_at: str
    updated_at: str
    last_error: str | None


class MemberProfileUpdate(BaseModel):
    identity: str = Field(min_length=1, max_length=32)
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


POPULAR_STATIONS = [
    {"value": "0900-基隆", "label": "基隆"},
    {"value": "1000-臺北", "label": "臺北"},
    {"value": "1020-板橋", "label": "板橋"},
    {"value": "1080-桃園", "label": "桃園"},
    {"value": "1210-新竹", "label": "新竹"},
    {"value": "3340-新烏日", "label": "新烏日"},
    {"value": "3300-臺中", "label": "臺中"},
    {"value": "3360-彰化", "label": "彰化"},
    {"value": "4080-嘉義", "label": "嘉義"},
    {"value": "4220-臺南", "label": "臺南"},
    {"value": "4340-新左營", "label": "新左營"},
    {"value": "4400-高雄", "label": "高雄"},
    {"value": "7000-花蓮", "label": "花蓮"},
    {"value": "6000-臺東", "label": "臺東"},
]


def _task_response(task: TaskRecord) -> TaskResponse:
    return TaskResponse(**asdict(task))


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
) -> FastAPI:
    db = database or Database()
    tokens = token_manager or TokenManager()
    scheduler = TaskScheduler(db)
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
            identity=body.identity,
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

    @app.post("/tasks", response_model=TaskResponse, status_code=201)
    def create_task(body: TaskCreate, user: CurrentUser) -> TaskResponse:
        booking_payload = dict(body.booking)
        if body.use_saved_member_login:
            profile = db.get_member_profile(user.id)
            if not profile or not profile.member_account or not profile.member_password:
                raise HTTPException(status_code=422, detail="尚未設定完整的台鐵會員登入資料")
            booking_payload["member_login"] = {
                "account": profile.member_account,
                "password": profile.member_password,
            }
        if not str(booking_payload.get("identity", "")).strip():
            profile = db.get_member_profile(user.id)
            if profile:
                booking_payload["identity"] = profile.identity
        try:
            booking = BookingRequest.from_dict(booking_payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        scheduled_at = body.scheduled_at
        if scheduled_at.tzinfo is None:
            raise HTTPException(status_code=422, detail="scheduled_at must include a timezone")
        if scheduled_at.astimezone(UTC) < datetime.now(UTC):
            raise HTTPException(status_code=422, detail="scheduled_at cannot be in the past")
        task = db.create_task(
            user.id,
            booking,
            scheduled_at.astimezone(UTC).isoformat(),
            booking_payload,
        )
        return _task_response(task)

    @app.get("/tasks", response_model=list[TaskResponse])
    def list_tasks(user: CurrentUser) -> list[TaskResponse]:
        return [_task_response(task) for task in db.list_tasks(user.id)]

    @app.post("/suggestions")
    def suggestions(body: SuggestionRequest, user: CurrentUser) -> dict[str, Any]:
        del user
        if body.start_time not in BOOKING_TIME_LABELS or body.end_time not in BOOKING_TIME_LABELS:
            raise HTTPException(status_code=422, detail="Invalid booking time label")
        if body.start_time >= body.end_time:
            raise HTTPException(status_code=422, detail="start_time must precede end_time")
        if body.start_station == body.end_station:
            raise HTTPException(status_code=422, detail="start_station and end_station must differ")
        try:
            date.fromisoformat(body.ride_date.replace("/", "-"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid ride_date") from exc
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
                detail="TDX timetable suggestions are temporarily unavailable; use train-number mode or try again later",
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

    @app.post("/tasks/{task_id}/cancel", status_code=204)
    def cancel_task(
        task_id: str,
        user: CurrentUser,
    ) -> Response:
        task = db.get_task(task_id, user.id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in {"scheduled", "waiting_human"}:
            raise HTTPException(status_code=409, detail="Task cannot be cancelled")
        db.update_task_status(task_id, user.id, "cancelled")
        return Response(status_code=204)

    app.state.database = db
    app.state.scheduler = scheduler
    app.state.tdx = tdx
    app.state.verification = verification
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
