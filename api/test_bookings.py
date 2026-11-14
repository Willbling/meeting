import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from fastapi.testclient import TestClient

from data.sqlite.init_db import initialize

from .main import create_app


def identity(person_id: str) -> dict[str, str]:
    return {"X-Person-ID": person_id}


class BookingApiTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.database = Path(folder) / "test.db"
        initialize(self.database)
        self.app = create_app(self.database)
        self.client = self.enterContext(TestClient(self.app))
        self.creator = identity("P000001")
        self.host = identity("P000002")
        self.member = identity("P000003")
        self.outsider = identity("P000004")
        self.start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=7)
        self.body = {
            "room_id": "R000001",
            "host_id": "P000002",
            "member_ids": ["P000003"],
            "starts_at": self.start.isoformat(),
            "ends_at": (self.start + timedelta(hours=1)).isoformat(),
            "subject": "预约接口验收",
        }

    def test_delegated_booking_lifecycle(self) -> None:
        result = self.client.post("/bookings", json=self.body, headers=self.creator)
        self.assertEqual(result.status_code, 201, result.text)
        created = result.json()
        path = f"/bookings/{created['booking_id']}"
        self.assertEqual(created["created_by"], "P000001")
        self.assertEqual(created["host_id"], "P000002")
        self.assertEqual(created["status"], "等待")
        self.assertTrue(created["starts_at"].endswith("+08:00"))
        self.assertEqual(datetime.fromisoformat(created["starts_at"]), self.start)
        self.assertEqual([item["role"] for item in created["attendees"]], ["主持人", "成员"])

        for actor in (self.creator, self.host, self.member):
            self.assertEqual(self.client.get(path, headers=actor).status_code, 200)
            result = self.client.get("/bookings", params={"room_id": "R000001", "status": "等待"}, headers=actor)
            self.assertIn(created["booking_id"], [item["booking_id"] for item in result.json()])
        self.assertEqual(self.client.get(path, headers=self.outsider).status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post(f"{path}/cancel", headers=self.member).status_code, 403)

        change = {"room_id": "R000002", "starts_at": (self.start + timedelta(minutes=30)).isoformat()}
        result = self.client.patch(path, json=change, headers=self.host)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["room_id"], "R000002")
        result = self.client.post(f"{path}/cancel", headers=self.creator)
        self.assertEqual(result.status_code, 200, result.text)
        canceled = result.json()
        self.assertEqual(canceled["status"], "取消")
        self.assertEqual(self.client.post(f"{path}/cancel", headers=self.host).json(), canceled)
        self.assertEqual(self.client.patch(path, json=change, headers=self.host).json()["code"], "BOOKING_NOT_WAITING")
        replacement = {**self.body, **change}
        result = self.client.post("/bookings", json=replacement, headers=self.creator)
        self.assertEqual(result.status_code, 201, result.text)

    def test_simultaneous_requests_and_conflicting_reschedule(self) -> None:
        barrier = Barrier(2, timeout=10)

        def reserve(actor: dict[str, str]):
            with TestClient(self.app) as client:
                barrier.wait()
                return client.post("/bookings", json=self.body, headers=actor)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, (self.creator, self.host)))
        self.assertEqual(sorted(result.status_code for result in results), [201, 409])
        self.assertEqual(next(result for result in results if result.status_code == 409).json()["code"], "ROOM_TIME_CONFLICT")

        adjacent = {**self.body, "starts_at": self.body["ends_at"], "ends_at": (self.start + timedelta(hours=2)).isoformat()}
        result = self.client.post("/bookings", json=adjacent, headers=self.creator)
        self.assertEqual(result.status_code, 201, result.text)
        before = result.json()
        path = f"/bookings/{before['booking_id']}"
        result = self.client.patch(path, json={"starts_at": self.body["starts_at"]}, headers=self.creator)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["code"], "ROOM_TIME_CONFLICT")
        self.assertEqual(self.client.get(path, headers=self.creator).json(), before)

    def test_queries_and_validation(self) -> None:
        result = self.client.get("/people", params={"name": "林晓"})
        self.assertEqual(result.json(), [{"person_id": "P000001", "name": "林晓"}])
        self.assertEqual(self.client.get("/rooms").status_code, 200)
        self.assertEqual(self.client.get("/rooms", params={"min_capacity": 8}).json()[0]["room_id"], "R000001")
        window = {key: self.body[key] for key in ("starts_at", "ends_at")}
        result = self.client.get("/rooms", params=window)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()), 2)
        result = self.client.post("/bookings", json=self.body, headers=self.creator)
        self.assertEqual(result.status_code, 201, result.text)
        result = self.client.get("/rooms", params=window)
        self.assertEqual([room["room_id"] for room in result.json()], ["R000002"])

        for changes, status, code in (
            ({"room_id": "R000003"}, 409, "ROOM_INACTIVE"),
            ({"room_id": "R000002", "member_ids": [f"P{number:06d}" for number in range(3, 9)]}, 409, "ROOM_CAPACITY_EXCEEDED"),
            ({"member_ids": ["P999999"]}, 404, "PERSON_NOT_FOUND"),
            ({"member_ids": ["P000002"]}, 422, "VALIDATION_ERROR"),
            ({"starts_at": "2026-11-13T14:00:00"}, 422, "VALIDATION_ERROR"),
        ):
            with self.subTest(code=code):
                result = self.client.post("/bookings", json={**self.body, **changes}, headers=self.creator)
                self.assertEqual(result.status_code, status, result.text)
                self.assertEqual(result.json()["code"], code)

        result = self.client.post("/bookings/A000007/cancel", headers=identity("P000007"))
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["code"], "BOOKING_NOT_WAITING")
        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM bookings").fetchone(), (9,))
            self.assertEqual(db.execute("SELECT count(*) FROM booking_members").fetchone(), (14,))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
