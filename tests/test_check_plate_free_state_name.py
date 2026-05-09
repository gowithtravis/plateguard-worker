from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import check_plate_free as check_plate_free_router


def test_check_plate_free_accepts_full_state_name(monkeypatch):
    app = FastAPI()
    app.include_router(check_plate_free_router.router, prefix="/api")
    client = TestClient(app)

    def _fake_check_plate_free_rmc_sync(plate_number: str, state: str):
        assert plate_number == "4LBZ81"
        assert state == "MA"
        return (
            [
                {
                    "city": "Somerville",
                    "amount": 25.0,
                    "date": "2026-05-09T00:00:00+00:00",
                    "status": "open",
                }
            ],
            ["Somerville"],
        )

    monkeypatch.setattr(
        check_plate_free_router,
        "check_plate_free_rmc_sync",
        _fake_check_plate_free_rmc_sync,
    )

    response = client.post(
        "/api/check-plate-free",
        json={"plate_number": "4LBZ81", "state": "Massachusetts"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["plate_number"] == "4LBZ81"
    assert data["state"] == "MA"
    assert data["violations_found"] == 1


def test_check_plate_free_accepts_district_of_columbia(monkeypatch):
    app = FastAPI()
    app.include_router(check_plate_free_router.router, prefix="/api")
    client = TestClient(app)

    def _fake_check_plate_free_rmc_sync(plate_number: str, state: str):
        assert plate_number == "AB1234"
        assert state == "DC"
        return ([], ["Somerville"])

    monkeypatch.setattr(
        check_plate_free_router,
        "check_plate_free_rmc_sync",
        _fake_check_plate_free_rmc_sync,
    )

    response = client.post(
        "/api/check-plate-free",
        json={"plate_number": "AB1234", "state": "district of columbia"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "DC"
    assert data["violations_found"] == 0


def test_check_plate_free_rejects_invalid_state(monkeypatch):
    app = FastAPI()
    app.include_router(check_plate_free_router.router, prefix="/api")
    client = TestClient(app)

    def _fake_check_plate_free_rmc_sync(plate_number: str, state: str):
        raise AssertionError("Portal check should not run for invalid state input")

    monkeypatch.setattr(
        check_plate_free_router,
        "check_plate_free_rmc_sync",
        _fake_check_plate_free_rmc_sync,
    )

    response = client.post(
        "/api/check-plate-free",
        json={"plate_number": "AB1234", "state": "Massachusettz"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid state."
