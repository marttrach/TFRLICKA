from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.storage import Database


class FakeTdx:
    def stations(self, fallback):
        return fallback + [{"value": "1234-測試站", "label": "測試站"}]

    def daily_timetable(self, start_id, end_id, ride_date):
        del start_id, end_id, ride_date
        return [
            {
                "TrainInfo": {
                    "TrainNo": "110",
                    "TrainTypeCode": "3",
                    "TrainTypeName": {"Zh_tw": "自強"},
                },
                "StopTimes": [
                    {"DepartureTime": "08:30"},
                    {"ArrivalTime": "10:00"},
                ],
            }
        ]


def _new_app(tmp_path, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("TRA_CORS_ORIGINS", "http://nas.local:43124")
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    return database, create_app(database, TokenManager("t" * 32), start_scheduler=False)


def test_member_and_task_flow(tmp_path) -> None:
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    booking = {
        "identity": "TEST-ID",
        "start_station": "1000-臺北",
        "end_station": "3300-臺中",
        "outbound": {
            "ride_date": (datetime.now().astimezone().date() + timedelta(days=1)).strftime(
                "%Y/%m/%d"
            ),
            "train_numbers": ["110"],
        },
    }

    with TestClient(app) as client:
        registered = client.post(
            "/auth/register",
            json={"email": "user@example.com", "password": "very-secure-password"},
        )
        assert registered.status_code == 201
        token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = client.post(
            "/tasks",
            headers=headers,
            json={
                "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "booking": booking,
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert "identity" not in created.text

        tasks = client.get("/tasks", headers=headers)
        assert tasks.status_code == 200
        assert tasks.json()[0]["route"] == "1000-臺北 → 3300-臺中"

        config = client.get(f"/tasks/{task_id}/config", headers=headers)
        assert config.json()["identity"] == "TEST-ID"

        cancelled = client.post(f"/tasks/{task_id}/cancel", headers=headers)
        assert cancelled.status_code == 204


def test_health_stations_login_me_logout_and_revocation(tmp_path) -> None:
    _, app = _new_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        stations = client.get("/stations")
        assert stations.status_code == 200
        assert any(station["value"] == "1000-臺北" for station in stations.json())

        registered = client.post(
            "/auth/register",
            json={"email": "member@example.com", "password": "very-secure-password"},
        )
        assert registered.status_code == 201
        login = client.post(
            "/auth/login",
            json={"email": "member@example.com", "password": "very-secure-password"},
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert client.get("/auth/me", headers=headers).json()["email"] == "member@example.com"

        assert client.post("/auth/logout", headers=headers).status_code == 204
        revoked = client.get("/auth/me", headers=headers)
        assert revoked.status_code == 401
        assert revoked.json()["detail"] == "Token has been revoked"


def test_login_is_rate_limited_after_five_failures(tmp_path) -> None:
    _, app = _new_app(tmp_path)
    with TestClient(app) as client:
        client.post(
            "/auth/register",
            json={"email": "limited@example.com", "password": "very-secure-password"},
        )
        for _ in range(4):
            response = client.post(
                "/auth/login",
                json={"email": "limited@example.com", "password": "wrong"},
            )
            assert response.status_code == 401
        locked = client.post(
            "/auth/login",
            json={"email": "limited@example.com", "password": "wrong"},
        )
        assert locked.status_code == 429
        assert locked.headers["retry-after"] == "900"

        correct_but_locked = client.post(
            "/auth/login",
            json={"email": "limited@example.com", "password": "very-secure-password"},
        )
        assert correct_but_locked.status_code == 429


def test_registration_password_policy_and_configured_cors(tmp_path, monkeypatch) -> None:
    _, app = _new_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        short = client.post(
            "/auth/register",
            json={"email": "short@example.com", "password": "tenletters"},
        )
        assert short.status_code == 422
        preflight = client.options(
            "/auth/login",
            headers={
                "Origin": "http://nas.local:43124",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://nas.local:43124"


def test_times_suggestions_and_encrypted_offline_snapshot(tmp_path) -> None:
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    app = create_app(
        database,
        TokenManager("t" * 32),
        tdx_client=FakeTdx(),
        start_scheduler=False,
    )
    ride_date = (datetime.now().astimezone().date() + timedelta(days=1)).strftime("%Y/%m/%d")
    with TestClient(app) as client:
        assert client.get("/times").json()[-1] == "23:59"
        assert any(item["value"] == "1234-測試站" for item in client.get("/stations").json())
        registered = client.post(
            "/auth/register",
            json={"email": "suggest@example.com", "password": "very-secure-password"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        suggested = client.post(
            "/suggestions",
            headers=headers,
            json={
                "start_station": "1000-臺北",
                "end_station": "3300-臺中",
                "ride_date": ride_date,
                "start_time": "08:00",
                "end_time": "12:00",
                "preferences": {"include_transfers": False},
            },
        )
        assert suggested.status_code == 200
        assert suggested.json()["availability_known"] is False
        assert suggested.json()["primary"][0]["seat_type_label"] == "對號列車"

        created = client.post(
            "/tasks",
            headers=headers,
            json={
                "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "booking": {
                    "identity": "TEST-ID",
                    "start_station": "1000-臺北",
                    "end_station": "3300-臺中",
                    "order_type": "BY_TIME",
                    "outbound": {
                        "ride_date": ride_date,
                        "start_time": "08:00",
                        "end_time": "12:00",
                    },
                    "candidate_suggestions": suggested.json(),
                },
            },
        )
        snapshot = client.get(
            f"/tasks/{created.json()['id']}/suggestions", headers=headers
        )
        assert snapshot.json()["primary"][0]["train_no"] == "110"


def test_unconfigured_tdx_keeps_api_available(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TDX_CLIENT_ID", raising=False)
    monkeypatch.delenv("TDX_CLIENT_SECRET", raising=False)
    database = Database(tmp_path / "api.db", encryption_key=Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        stations = client.get("/stations")
        assert stations.status_code == 200
        assert any(item["value"] == "1000-臺北" for item in stations.json())


def test_member_profile_does_not_return_password_and_can_supply_task_login(tmp_path) -> None:
    database = Database(tmp_path / "profile-api.db", encryption_key=Fernet.generate_key().decode())
    app = create_app(database, TokenManager("t" * 32), start_scheduler=False)
    ride_date = (datetime.now().astimezone().date() + timedelta(days=1)).strftime("%Y/%m/%d")
    with TestClient(app) as client:
        registered = client.post(
            "/auth/register",
            json={"email": "profile@example.com", "password": "very-secure-password"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        saved = client.put(
            "/profile",
            headers=headers,
            json={
                "identity": "A123456789",
                "member_account": "A123456789",
                "member_password": "railway-password",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["has_member_password"] is True
        assert "railway-password" not in saved.text
        assert "railway-password" not in client.get("/profile", headers=headers).text

        created = client.post(
            "/tasks",
            headers=headers,
            json={
                "scheduled_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "use_saved_member_login": True,
                "booking": {
                    "identity": "",
                    "start_station": "1000-臺北",
                    "end_station": "3300-臺中",
                    "outbound": {"ride_date": ride_date, "train_numbers": ["110"]},
                },
            },
        )
        assert created.status_code == 201
        payload = database.get_task_payload(created.json()["id"], 1)
        assert payload["identity"] == "A123456789"
        assert payload["member_login"]["password"] == "railway-password"
