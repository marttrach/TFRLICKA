import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .auth import TokenManager, hash_password, verify_password
from .models import BookingRequest
from .scheduler import TaskScheduler
from .storage import Database, TaskRecord, UserRecord

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=256)


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


def create_app(
    database: Database | None = None,
    token_manager: TokenManager | None = None,
    *,
    start_scheduler: bool = True,
) -> FastAPI:
    db = database or Database()
    tokens = token_manager or TokenManager()
    scheduler = TaskScheduler(db)
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
        version="0.2.0",
        description="Local membership and human-in-the-loop TRA booking task API.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
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
        return user

    CurrentUser = Annotated[UserRecord, Depends(current_user)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "scheduler": "human-in-the-loop"}

    @app.get("/stations")
    def stations() -> list[dict[str, str]]:
        return POPULAR_STATIONS

    @app.post("/auth/register", response_model=TokenResponse, status_code=201)
    def register(body: Credentials) -> TokenResponse:
        email = body.email.strip().lower()
        if not EMAIL_PATTERN.fullmatch(email):
            raise HTTPException(status_code=422, detail="Invalid email address")
        try:
            user = db.create_user(email, hash_password(body.password))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TokenResponse(access_token=tokens.issue(user.id))

    @app.post("/auth/login", response_model=TokenResponse)
    def login(body: Credentials) -> TokenResponse:
        user = db.get_user_by_email(body.email)
        if not user or not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        return TokenResponse(access_token=tokens.issue(user.id))

    @app.get("/auth/me", response_model=UserResponse)
    def me(user: CurrentUser) -> UserResponse:
        return UserResponse(id=user.id, email=user.email, created_at=user.created_at)

    @app.post("/tasks", response_model=TaskResponse, status_code=201)
    def create_task(body: TaskCreate, user: CurrentUser) -> TaskResponse:
        try:
            booking = BookingRequest.from_dict(body.booking)
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
            body.booking,
        )
        return _task_response(task)

    @app.get("/tasks", response_model=list[TaskResponse])
    def list_tasks(user: CurrentUser) -> list[TaskResponse]:
        return [_task_response(task) for task in db.list_tasks(user.id)]

    @app.get("/tasks/{task_id}/config")
    def task_config(task_id: str, user: CurrentUser) -> dict[str, Any]:
        try:
            return db.get_task_payload(task_id, user.id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc

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
    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(
        "tra_sniper.api:app",
        host=os.getenv("TRA_API_HOST", "0.0.0.0"),
        port=int(os.getenv("TRA_API_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    run()
