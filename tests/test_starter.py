import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
import pymongo
from pymongo.database import Database

import app.main as main_module
from app.main import app, db, client, compute_late_minutes, compute_work_hours, compute_overtime, determine_attendance_date, IST

test_client = TestClient(app)


class TestStarterFixes(unittest.TestCase):
    def setUp(self):
        # Clean test records before each test for total test isolation
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def tearDown(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    # ----------------------------------------------------------------------- #
    # Task 1: /health
    # ----------------------------------------------------------------------- #
    def test_health_success(self):
        resp = test_client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_health_database_failure(self):
        with patch.object(Database, "command", side_effect=Exception("Database connection failure")):
            resp = test_client.get("/health")
            self.assertEqual(resp.status_code, 503)
            self.assertIn("detail", resp.json())

    # ----------------------------------------------------------------------- #
    # Task 2: POST /employees
    # ----------------------------------------------------------------------- #
    def test_create_employee_success(self):
        emp_data = {
            "emp_code": "EMP9901",
            "name": "Test User 1",
            "email": "user1@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        }
        resp = test_client.post("/employees", json=emp_data)
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["emp_code"], "EMP9901")
        self.assertNotIn("_id", body)
        self.assertNotIn("id", body)
        self.assertIsInstance(body["created_at"], int)
        self.assertGreaterEqual(body["created_at"], 100000000000)

    def test_create_employee_duplicate_conflict(self):
        emp_data = {
            "emp_code": "EMP9901",
            "name": "Test User 1",
            "email": "user1@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        }
        resp1 = test_client.post("/employees", json=emp_data)
        self.assertEqual(resp1.status_code, 201)

        # Duplicate must return 409
        resp2 = test_client.post("/employees", json=emp_data)
        self.assertEqual(resp2.status_code, 409)

    def test_create_employee_validation_failures(self):
        # Invalid emp_code format
        resp = test_client.post("/employees", json={
            "emp_code": "INVALID_CODE",
            "name": "Bad Code",
            "email": "bad@example.com",
            "department": "QA",
            "joined_on": "2026-01-01"
        })
        self.assertEqual(resp.status_code, 422)

        # Equal shift_start and shift_end
        resp = test_client.post("/employees", json={
            "emp_code": "EMP9902",
            "name": "Bad Shifts",
            "email": "bad_shifts@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01"
        })
        self.assertEqual(resp.status_code, 422)

        # Bad email
        resp = test_client.post("/employees", json={
            "emp_code": "EMP9902",
            "name": "Bad Email",
            "email": "not-an-email",
            "department": "QA",
            "joined_on": "2026-01-01"
        })
        self.assertEqual(resp.status_code, 422)

    # ----------------------------------------------------------------------- #
    # Task 3: GET /employees
    # ----------------------------------------------------------------------- #
    def test_list_employees_pagination_and_sorting(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9902",
            "name": "Beta User",
            "email": "beta@example.com",
            "department": "TestHR",
            "joined_on": "2026-02-01"
        })
        test_client.post("/employees", json={
            "emp_code": "EMP9901",
            "name": "Alpha User",
            "email": "alpha@example.com",
            "department": "TestEngineering",
            "joined_on": "2026-01-01"
        })

        # Test page 1 with page_size 1: MUST NOT skip first item
        resp1 = test_client.get("/employees?department=TestEngineering&page=1&page_size=1")
        self.assertEqual(resp1.status_code, 200)
        body1 = resp1.json()
        self.assertEqual(body1["total"], 1)
        self.assertEqual(len(body1["items"]), 1)
        self.assertEqual(body1["items"][0]["emp_code"], "EMP9901")

        # Test sorting order: EMP9901 before EMP9902
        resp_all = test_client.get("/employees?page=1&page_size=10")
        items = resp_all.json()["items"]
        emp_codes = [i["emp_code"] for i in items if i["emp_code"].startswith("EMP99")]
        self.assertEqual(emp_codes, ["EMP9901", "EMP9902"])

    # ----------------------------------------------------------------------- #
    # Task 4: POST /attendance/punch-in & Business Rules
    # ----------------------------------------------------------------------- #
    def test_compute_late_minutes_boundaries(self):
        # Shift 09:30 on 2026-07-06
        # 09:40:00 -> 0
        dt_40_00 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
        self.assertEqual(compute_late_minutes(dt_40_00, "09:30", "2026-07-06"), 0)

        # 09:40:01 -> 10
        dt_40_01 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
        self.assertEqual(compute_late_minutes(dt_40_01, "09:30", "2026-07-06"), 10)

        # 10:15:59 -> 45
        dt_15_59 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
        self.assertEqual(compute_late_minutes(dt_15_59, "09:30", "2026-07-06"), 45)

        # Early 09:25:00 -> 0
        dt_early = datetime(2026, 7, 6, 9, 25, 0, tzinfo=IST)
        self.assertEqual(compute_late_minutes(dt_early, "09:30", "2026-07-06"), 0)

    def test_overnight_shift_date_attribution(self):
        # Overnight shift 22:00 to 06:00
        # Punch-in at 2026-07-06 21:55:00 IST -> belongs to 2026-07-06
        dt1 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
        self.assertEqual(determine_attendance_date(dt1, "22:00", "06:00"), "2026-07-06")

        # Punch-in at 2026-07-07 01:30:00 IST (< 06:00) -> belongs to 2026-07-06
        dt2 = datetime(2026, 7, 7, 1, 30, 0, tzinfo=IST)
        self.assertEqual(determine_attendance_date(dt2, "22:00", "06:00"), "2026-07-06")

        # Punch-in at 2026-07-07 06:10:00 IST (>= 06:00) -> belongs to 2026-07-07
        dt3 = datetime(2026, 7, 7, 6, 10, 0, tzinfo=IST)
        self.assertEqual(determine_attendance_date(dt3, "22:00", "06:00"), "2026-07-07")

    def test_punch_in_endpoint(self):
        # Create employee first
        test_client.post("/employees", json={
            "emp_code": "EMP9901",
            "name": "Test User 1",
            "email": "user1@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        # 404 for non-existent employee
        resp_404 = test_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
        self.assertEqual(resp_404.status_code, 404)

        # 422 for invalid status (e.g. ABSENT)
        resp_bad_status = test_client.post("/attendance/punch-in", json={"emp_code": "EMP9901", "status": "ABSENT"})
        self.assertEqual(resp_bad_status.status_code, 422)

        # 422 for seconds-looking timestamp (< 100000000000)
        resp_seconds = test_client.post("/attendance/punch-in", json={"emp_code": "EMP9901", "punched_at": 1783312500})
        self.assertEqual(resp_seconds.status_code, 422)

        # Successful punch-in at 2026-07-06 09:40:01 IST
        # 2026-07-06 09:40:01 IST is 2026-07-06 04:10:01 UTC
        punch_dt = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
        punch_ms = int(punch_dt.timestamp() * 1000)

        resp_punch = test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP9901",
            "punched_at": punch_ms,
            "status": "PRESENT"
        })
        self.assertEqual(resp_punch.status_code, 201)
        body = resp_punch.json()
        self.assertEqual(body["emp_code"], "EMP9901")
        self.assertEqual(body["date"], "2026-07-06")
        self.assertEqual(body["late_minutes"], 10)
        self.assertEqual(body["punch_in"], punch_ms)
        self.assertIsNone(body["punch_out"])
        self.assertNotIn("_id", body)
        self.assertNotIn("id", body)

        # 409 for duplicate punch-in on same date
        resp_dup = test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP9901",
            "punched_at": punch_ms,
            "status": "PRESENT"
        })
        self.assertEqual(resp_dup.status_code, 409)

    # ----------------------------------------------------------------------- #
    # Task 5: GET /attendance
    # ----------------------------------------------------------------------- #
    def test_list_attendance(self):
        # 422 when date_from > date_to
        resp_bad_dates = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
        self.assertEqual(resp_bad_dates.status_code, 422)

        # Create employee and punch-in record
        test_client.post("/employees", json={
            "emp_code": "EMP9901",
            "name": "Test User 1",
            "email": "user1@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        punch_dt = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
        punch_ms = int(punch_dt.timestamp() * 1000)
        test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP9901",
            "punched_at": punch_ms,
            "status": "PRESENT"
        })

        # Valid listing
        resp = test_client.get("/attendance?emp_code=EMP9901")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["emp_code"], "EMP9901")
        self.assertEqual(item["date"], "2026-07-06")
        self.assertIsInstance(item["punch_in"], int)
        self.assertNotIn("_id", item)
        self.assertNotIn("id", item)

    # ----------------------------------------------------------------------- #
    # Concurrency verification tests
    # ----------------------------------------------------------------------- #
    def test_concurrent_employee_creation(self):
        from concurrent.futures import ThreadPoolExecutor

        emp_data = {
            "emp_code": "EMP9905",
            "name": "Concurrent User",
            "email": "concurrent@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        }

        def make_req():
            c = TestClient(app)
            return c.post("/employees", json=emp_data).status_code

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(make_req) for _ in range(5)]
            results = [f.result() for f in futures]

        self.assertEqual(results.count(201), 1)
        self.assertEqual(results.count(409), 4)

    def test_concurrent_punch_in(self):
        from concurrent.futures import ThreadPoolExecutor

        test_client.post("/employees", json={
            "emp_code": "EMP9906",
            "name": "Concurrent Puncher",
            "email": "puncher@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        punch_dt = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
        punch_ms = int(punch_dt.timestamp() * 1000)
        punch_data = {
            "emp_code": "EMP9906",
            "punched_at": punch_ms,
            "status": "PRESENT"
        }

        def make_punch():
            c = TestClient(app)
            return c.post("/attendance/punch-in", json=punch_data).status_code

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(make_punch) for _ in range(5)]
            results = [f.result() for f in futures]

        self.assertEqual(results.count(201), 1)
        self.assertEqual(results.count(409), 4)

    # ----------------------------------------------------------------------- #
    # Step 3A: POST /attendance/punch-out Tests
    # ----------------------------------------------------------------------- #
    def test_punch_out_success(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9910",
            "name": "Punchout User",
            "email": "punchout@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        p_in_ms = int(p_in_dt.timestamp() * 1000)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9910", "punched_at": p_in_ms})

        p_out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        p_out_ms = int(p_out_dt.timestamp() * 1000)
        resp = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9910", "punched_at": p_out_ms})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["emp_code"], "EMP9910")
        self.assertEqual(body["date"], "2026-07-06")
        self.assertEqual(body["punch_in"], p_in_ms)
        self.assertEqual(body["punch_out"], p_out_ms)
        self.assertEqual(body["work_hours"], 9.12)
        self.assertEqual(body["overtime_minutes"], 0)
        self.assertEqual(body["half_day"], False)
        self.assertEqual(body["history"], [])
        self.assertNotIn("_id", body)
        self.assertNotIn("id", body)

    def test_punch_out_missing_employee_and_record(self):
        resp1 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
        self.assertEqual(resp1.status_code, 404)

        test_client.post("/employees", json={
            "emp_code": "EMP9911",
            "name": "No Punch User",
            "email": "nopunch@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        resp2 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9911"})
        self.assertEqual(resp2.status_code, 404)

    def test_punch_out_already_punched_out(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9912",
            "name": "Double Punchout User",
            "email": "double@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9912", "punched_at": int(p_in_dt.timestamp() * 1000)})
        resp1 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9912", "punched_at": int(p_out_dt.timestamp() * 1000)})
        self.assertEqual(resp1.status_code, 200)

        resp2 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9912", "punched_at": int(p_out_dt.timestamp() * 1000)})
        self.assertEqual(resp2.status_code, 409)

    def test_punch_out_invalid_timestamps(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9913",
            "name": "Invalid TS User",
            "email": "invalidts@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        resp_sec = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9913", "punched_at": 1783312500})
        self.assertEqual(resp_sec.status_code, 422)

        resp_flt = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9913", "punched_at": 1783312500000.5})
        self.assertEqual(resp_flt.status_code, 422)

        resp_str = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9913", "punched_at": "1783312500000"})
        self.assertEqual(resp_str.status_code, 422)

    def test_punch_out_before_or_equal_to_punch_in(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9914",
            "name": "Time Order User",
            "email": "timeorder@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_in_ms = int(p_in_dt.timestamp() * 1000)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9914", "punched_at": p_in_ms})

        resp_eq = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9914", "punched_at": p_in_ms})
        self.assertEqual(resp_eq.status_code, 422)

        resp_before = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9914", "punched_at": p_in_ms - 60000})
        self.assertEqual(resp_before.status_code, 422)

        resp_over24 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9914", "punched_at": p_in_ms + (25 * 3600 * 1000)})
        self.assertEqual(resp_over24.status_code, 422)

    def test_punch_out_work_hours_half_up_rounding(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9915",
            "name": "Rounding User",
            "email": "round@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_out_dt = p_in_dt + timedelta(seconds=7650)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9915", "punched_at": int(p_in_dt.timestamp() * 1000)})
        resp = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9915", "punched_at": int(p_out_dt.timestamp() * 1000)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["work_hours"], 2.13)

    def test_punch_out_overtime_thresholds(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9916",
            "name": "Overtime User",
            "email": "ot@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_out_29 = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9916", "punched_at": int(p_in_dt.timestamp() * 1000)})
        resp_29 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9916", "punched_at": int(p_out_29.timestamp() * 1000)})
        self.assertEqual(resp_29.status_code, 200)
        self.assertEqual(resp_29.json()["overtime_minutes"], 0)

        p_in_dt2 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
        p_out_40 = datetime(2026, 7, 7, 13, 40, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9916", "punched_at": int(p_in_dt2.timestamp() * 1000)})
        resp_40 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9916", "punched_at": int(p_out_40.timestamp() * 1000)})
        self.assertEqual(resp_40.status_code, 200)
        self.assertEqual(resp_40.json()["overtime_minutes"], 40)

    def test_punch_out_half_day_threshold(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9917",
            "name": "Halfday User",
            "email": "halfday@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_out_449 = p_in_dt + timedelta(seconds=16164)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9917", "punched_at": int(p_in_dt.timestamp() * 1000)})
        resp_449 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9917", "punched_at": int(p_out_449.timestamp() * 1000)})
        self.assertEqual(resp_449.status_code, 200)
        self.assertEqual(resp_449.json()["work_hours"], 4.49)
        self.assertEqual(resp_449.json()["half_day"], True)

        p_in_dt2 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
        p_out_450 = p_in_dt2 + timedelta(seconds=16200)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9917", "punched_at": int(p_in_dt2.timestamp() * 1000)})
        resp_450 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9917", "punched_at": int(p_out_450.timestamp() * 1000)})
        self.assertEqual(resp_450.status_code, 200)
        self.assertEqual(resp_450.json()["work_hours"], 4.50)
        self.assertEqual(resp_450.json()["half_day"], False)

    def test_punch_out_overnight_shift(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9918",
            "name": "Night Owl",
            "email": "night@example.com",
            "department": "Engineering",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
        p_out_dt = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9918", "punched_at": int(p_in_dt.timestamp() * 1000)})
        resp = test_client.post("/attendance/punch-out", json={"emp_code": "EMP9918", "punched_at": int(p_out_dt.timestamp() * 1000)})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["emp_code"], "EMP9918")
        self.assertEqual(body["date"], "2026-07-06")
        self.assertEqual(body["work_hours"], 8.75)
        self.assertEqual(body["overtime_minutes"], 40)
        self.assertEqual(body["half_day"], False)

    def test_concurrent_punch_out(self):
        from concurrent.futures import ThreadPoolExecutor

        test_client.post("/employees", json={
            "emp_code": "EMP9919",
            "name": "Concurrent Out Puncher",
            "email": "cout@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9919", "punched_at": int(p_in_dt.timestamp() * 1000)})

        p_out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
        p_out_ms = int(p_out_dt.timestamp() * 1000)

        def make_punch_out():
            c = TestClient(app)
            return c.post("/attendance/punch-out", json={"emp_code": "EMP9919", "punched_at": p_out_ms}).status_code

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(make_punch_out) for _ in range(5)]
            results = [f.result() for f in futures]

        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 4)

    # ----------------------------------------------------------------------- #
    # Step 3B: PATCH /attendance/{emp_code}/{date} Regularization Tests
    # ----------------------------------------------------------------------- #
    def test_patch_status_correction(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9920",
            "name": "Status User",
            "email": "status@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9920", "punched_at": int(p_in_dt.timestamp() * 1000), "status": "PRESENT"})

        resp = test_client.patch("/attendance/EMP9920/2026-07-06", json={
            "status": "WFH",
            "reason": "Worked from home approved",
            "regularized_by": "hr.lead"
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "WFH")
        self.assertEqual(len(body["history"]), 1)
        self.assertEqual(body["history"][0]["by"], "hr.lead")
        self.assertEqual(body["history"][0]["reason"], "Worked from home approved")
        self.assertEqual(body["history"][0]["changes"], {"status": {"from": "PRESENT", "to": "WFH"}})

    def test_patch_punch_time_correction(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9921",
            "name": "Correction User",
            "email": "correct@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
        p_out_dt = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9921", "punched_at": int(p_in_dt.timestamp() * 1000)})
        test_client.post("/attendance/punch-out", json={"emp_code": "EMP9921", "punched_at": int(p_out_dt.timestamp() * 1000)})

        new_in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        new_in_ms = int(new_in_dt.timestamp() * 1000)
        resp = test_client.patch("/attendance/EMP9921/2026-07-06", json={
            "punch_in": new_in_ms,
            "reason": "Biometric terminal glitch",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(body["punch_in"], new_in_ms)
        self.assertIn("punch_in", body["history"][0]["changes"])
        self.assertEqual(body["history"][0]["changes"]["late_minutes"], {"from": 35, "to": 0})
        self.assertIn("work_hours", body["history"][0]["changes"])

    def test_patch_missing_attendance_record(self):
        resp1 = test_client.patch("/attendance/EMP9999/2026-07-06", json={
            "status": "WFH",
            "reason": "Missing employee",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp1.status_code, 404)

        test_client.post("/employees", json={
            "emp_code": "EMP9922",
            "name": "No Record User",
            "email": "norec@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        resp2 = test_client.patch("/attendance/EMP9922/2026-07-06", json={
            "status": "WFH",
            "reason": "Record not found",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp2.status_code, 404)

    def test_patch_invalid_status_and_punch_time_combinations(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9923",
            "name": "Combo User",
            "email": "combo@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_in_ms = int(p_in_dt.timestamp() * 1000)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9923", "punched_at": p_in_ms})

        resp_absent = test_client.patch("/attendance/EMP9923/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": p_in_ms,
            "reason": "Cannot have punch_in with ABSENT",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp_absent.status_code, 422)

        resp_leave = test_client.patch("/attendance/EMP9923/2026-07-06", json={
            "status": "LEAVE",
            "punch_out": p_in_ms + 3600000,
            "reason": "Cannot have punch_out with LEAVE",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp_leave.status_code, 422)

        resp_bad_order = test_client.patch("/attendance/EMP9923/2026-07-06", json={
            "punch_out": p_in_ms - 1000,
            "reason": "Punch out before punch in",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp_bad_order.status_code, 422)

        resp_derived = test_client.patch("/attendance/EMP9923/2026-07-06", json={
            "status": "WFH",
            "work_hours": 8.5,
            "reason": "Forbidden derived field",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp_derived.status_code, 422)

    def test_patch_invalid_timestamps(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9924",
            "name": "Invalid TS User",
            "email": "invalidts2@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9924", "punched_at": int(p_in_dt.timestamp() * 1000)})

        resp1 = test_client.patch("/attendance/EMP9924/2026-07-06", json={
            "punch_in": 1783312500,
            "reason": "Seconds timestamp",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp1.status_code, 422)

        resp2 = test_client.patch("/attendance/EMP9924/2026-07-06", json={
            "punch_in": "1783312500000",
            "reason": "String timestamp",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp2.status_code, 422)

    def test_patch_rejection_of_no_actual_changes(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9925",
            "name": "No Change User",
            "email": "nochange@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9925", "punched_at": int(p_in_dt.timestamp() * 1000), "status": "PRESENT"})

        resp = test_client.patch("/attendance/EMP9925/2026-07-06", json={
            "status": "PRESENT",
            "reason": "Same status requested",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp.status_code, 422)

    def test_patch_recalculation_all_derived_fields(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9926",
            "name": "Recalc User",
            "email": "recalc@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_out_dt = datetime(2026, 7, 6, 7, 30, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9926", "punched_at": int(p_in_dt.timestamp() * 1000)})
        test_client.post("/attendance/punch-out", json={"emp_code": "EMP9926", "punched_at": int(p_out_dt.timestamp() * 1000)})

        new_out_dt = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
        resp = test_client.patch("/attendance/EMP9926/2026-07-06", json={
            "punch_out": int(new_out_dt.timestamp() * 1000),
            "reason": "Forgot to punch out on departure",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["work_hours"], 9.67)
        self.assertEqual(body["overtime_minutes"], 40)
        self.assertEqual(body["half_day"], False)
        ch = body["history"][0]["changes"]
        self.assertEqual(ch["half_day"], {"from": True, "to": False})
        self.assertEqual(ch["overtime_minutes"], {"from": 0, "to": 40})

    def test_patch_history_all_and_only_changed_fields(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9927",
            "name": "Only Changed User",
            "email": "only@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9927", "punched_at": int(p_in_dt.timestamp() * 1000), "status": "PRESENT"})

        resp = test_client.patch("/attendance/EMP9927/2026-07-06", json={
            "status": "ON_DUTY",
            "reason": "On official duty",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp.status_code, 200)
        ch = resp.json()["history"][0]["changes"]
        self.assertIn("status", ch)
        self.assertNotIn("punch_in", ch)
        self.assertNotIn("punch_out", ch)
        self.assertNotIn("late_minutes", ch)
        self.assertNotIn("overtime_minutes", ch)
        self.assertNotIn("half_day", ch)

    def test_patch_preservation_of_existing_history(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9928",
            "name": "Multi History User",
            "email": "multihist@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9928", "punched_at": int(p_in_dt.timestamp() * 1000)})

        resp1 = test_client.patch("/attendance/EMP9928/2026-07-06", json={
            "status": "WFH",
            "reason": "First correction",
            "regularized_by": "hr.one"
        })
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(len(resp1.json()["history"]), 1)

        resp2 = test_client.patch("/attendance/EMP9928/2026-07-06", json={
            "status": "ON_DUTY",
            "reason": "Second correction",
            "regularized_by": "hr.two"
        })
        self.assertEqual(resp2.status_code, 200)
        body2 = resp2.json()
        self.assertEqual(len(body2["history"]), 2)
        self.assertEqual(body2["history"][0]["by"], "hr.one")
        self.assertEqual(body2["history"][1]["by"], "hr.two")

    def test_patch_concurrent_regularizations_without_lost_history(self):
        from concurrent.futures import ThreadPoolExecutor

        test_client.post("/employees", json={
            "emp_code": "EMP9929",
            "name": "Concurrent Patch User",
            "email": "cpatch@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9929", "punched_at": int(p_in_dt.timestamp() * 1000)})

        statuses = ["WFH", "ON_DUTY"]

        def make_patch(st, idx):
            c = TestClient(app)
            return c.patch("/attendance/EMP9929/2026-07-06", json={
                "status": st,
                "reason": f"Concurrent correction {idx}",
                "regularized_by": f"hr.agent_{idx}"
            }).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(make_patch, statuses[i], i) for i in range(2)]
            results = [f.result() for f in futures]

        self.assertIn(200, results)
        get_resp = test_client.get("/attendance?emp_code=EMP9929")
        hist = get_resp.json()["items"][0]["history"]
        self.assertEqual(len(hist), results.count(200))

    def test_patch_serialization_of_history_timestamps(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9930",
            "name": "Serial User",
            "email": "serial@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_in_ms = int(p_in_dt.timestamp() * 1000)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9930", "punched_at": p_in_ms})

        new_in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        new_in_ms = int(new_in_dt.timestamp() * 1000)
        resp = test_client.patch("/attendance/EMP9930/2026-07-06", json={
            "punch_in": new_in_ms,
            "reason": "Timestamp serialization check",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp.status_code, 200)
        entry = resp.json()["history"][0]
        self.assertIsInstance(entry["at"], int)
        self.assertGreaterEqual(entry["at"], 100000000000)
        ch_in = entry["changes"]["punch_in"]
        self.assertEqual(ch_in["from"], p_in_ms)
        self.assertEqual(ch_in["to"], new_in_ms)

    def test_patch_overnight_shift_correction(self):
        test_client.post("/employees", json={
            "emp_code": "EMP9931",
            "name": "Overnight Patch User",
            "email": "onpatch@example.com",
            "department": "Engineering",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01"
        })
        p_in_dt = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
        test_client.post("/attendance/punch-in", json={"emp_code": "EMP9931", "punched_at": int(p_in_dt.timestamp() * 1000)})

        valid_in = datetime(2026, 7, 6, 19, 0, 0, tzinfo=timezone.utc)
        resp_valid = test_client.patch("/attendance/EMP9931/2026-07-06", json={
            "punch_in": int(valid_in.timestamp() * 1000),
            "reason": "Overnight shift valid adjustment",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp_valid.status_code, 200)

        invalid_in = datetime(2026, 7, 7, 1, 30, 0, tzinfo=timezone.utc)
        resp_invalid = test_client.patch("/attendance/EMP9931/2026-07-06", json={
            "punch_in": int(invalid_in.timestamp() * 1000),
            "reason": "Overnight shift invalid date attribution",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(resp_invalid.status_code, 422)


class TestEmployeeMonthlyAnalytics(unittest.TestCase):
    def setUp(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def tearDown(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def _create_employee(self, emp_code="EMP9940", joined_on="2026-01-01"):
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": "Analytics Test User",
            "email": f"{emp_code.lower()}@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined_on,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })

    def test_employee_not_found(self):
        resp = test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "Employee not found")

    def test_month_parameter_validation(self):
        self._create_employee("EMP9941")
        # Valid month formats
        resp_valid = test_client.get("/analytics/employees/EMP9941/monthly?month=2026-07")
        self.assertEqual(resp_valid.status_code, 200)

        # Invalid month formats
        for bad_m in ["2026-13", "2026-00", "2026-7", "2026", "2026-07-01", "invalid"]:
            resp = test_client.get(f"/analytics/employees/EMP9941/monthly?month={bad_m}")
            self.assertEqual(resp.status_code, 422)

        # Missing month parameter
        resp_missing = test_client.get("/analytics/employees/EMP9941/monthly")
        self.assertEqual(resp_missing.status_code, 422)

    def test_employee_joined_before_month(self):
        self._create_employee("EMP9942", joined_on="2026-01-01")
        # July 2026 has 23 weekdays (Mon-Fri)
        resp = test_client.get("/analytics/employees/EMP9942/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["emp_code"], "EMP9942")
        self.assertEqual(data["month"], "2026-07")
        self.assertEqual(data["working_days"], 23)

    def test_employee_joined_mid_month(self):
        # July 15, 2026 is Wednesday. Weekdays between July 15 and July 31 = 13.
        self._create_employee("EMP9943", joined_on="2026-07-15")
        resp = test_client.get("/analytics/employees/EMP9943/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["working_days"], 13)

        # Employee who joined after the month: working_days = 0, attendance_pct = None
        self._create_employee("EMP9944", joined_on="2026-08-01")
        resp2 = test_client.get("/analytics/employees/EMP9944/monthly?month=2026-07")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["working_days"], 0)
        self.assertIsNone(resp2.json()["attendance_pct"])

    def test_full_day_and_half_day_records(self):
        self._create_employee("EMP9945", joined_on="2026-07-01")
        # Mon 2026-07-06: full day (1.0)
        db.attendance_logs.insert_one({
            "emp_code": "EMP9945",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })
        # Tue 2026-07-07: half day (0.5)
        db.attendance_logs.insert_one({
            "emp_code": "EMP9945",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 7, 7, 30, tzinfo=timezone.utc),
            "work_hours": 3.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": [],
        })
        resp = test_client.get("/analytics/employees/EMP9945/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["present_days"], 1.5)

    def test_presence_and_non_presence_statuses(self):
        self._create_employee("EMP9946", joined_on="2026-07-01")
        # Mon: PRESENT (1.0), Tue: WFH (1.0), Wed: ON_DUTY (1.0), Thu: LEAVE (0.0, leave_days+1), Fri: ABSENT (0.0)
        logs = [
            {"emp_code": "EMP9946", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9946", "date": "2026-07-07", "status": "WFH", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9946", "date": "2026-07-08", "status": "ON_DUTY", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9946", "date": "2026-07-09", "status": "LEAVE", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9946", "date": "2026-07-10", "status": "ABSENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
        ]
        db.attendance_logs.insert_many(logs)
        resp = test_client.get("/analytics/employees/EMP9946/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["present_days"], 3.0)
        self.assertEqual(data["leave_days"], 1)

    def test_weekday_versus_weekend_records(self):
        self._create_employee("EMP9947", joined_on="2026-07-01")
        # Mon 2026-07-06: Weekday PRESENT full day
        # Sat 2026-07-11: Weekend PRESENT full day, late 20, ot 40
        # Sun 2026-07-12: Weekend PRESENT full day, late 15, ot 50
        logs = [
            {"emp_code": "EMP9947", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9947", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 20, "overtime_minutes": 40},
            {"emp_code": "EMP9947", "date": "2026-07-12", "status": "PRESENT", "half_day": False, "late_minutes": 15, "overtime_minutes": 50},
        ]
        db.attendance_logs.insert_many(logs)
        resp = test_client.get("/analytics/employees/EMP9947/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # present_days only counts Mon-Fri
        self.assertEqual(data["present_days"], 1.0)
        # But late and overtime totals DO include weekend records per R7
        self.assertEqual(data["late_count"], 2)
        self.assertEqual(data["total_late_minutes"], 35)
        self.assertEqual(data["total_overtime_minutes"], 90)

    def test_missing_attendance_records_on_weekdays(self):
        self._create_employee("EMP9948", joined_on="2026-07-01")
        # Only 2 logs in the entire month of 23 working days
        logs = [
            {"emp_code": "EMP9948", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9948", "date": "2026-07-07", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
        ]
        db.attendance_logs.insert_many(logs)
        resp = test_client.get("/analytics/employees/EMP9948/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["working_days"], 23)
        self.assertEqual(data["present_days"], 2.0)
        self.assertEqual(data["leave_days"], 0)
        # 2 / 23 * 100 = 8.695652... -> 8.70
        self.assertEqual(data["attendance_pct"], 8.70)

    def test_late_count_and_total_late_minutes(self):
        self._create_employee("EMP9949", joined_on="2026-07-01")
        logs = [
            {"emp_code": "EMP9949", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9949", "date": "2026-07-07", "status": "PRESENT", "half_day": False, "late_minutes": 15, "overtime_minutes": 0},
            {"emp_code": "EMP9949", "date": "2026-07-08", "status": "PRESENT", "half_day": False, "late_minutes": 30, "overtime_minutes": 0},
        ]
        db.attendance_logs.insert_many(logs)
        resp = test_client.get("/analytics/employees/EMP9949/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["late_count"], 2)
        self.assertEqual(data["total_late_minutes"], 45)

    def test_total_overtime_minutes(self):
        self._create_employee("EMP9950", joined_on="2026-07-01")
        logs = [
            {"emp_code": "EMP9950", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 30},
            {"emp_code": "EMP9950", "date": "2026-07-07", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 45},
            {"emp_code": "EMP9950", "date": "2026-07-08", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
        ]
        db.attendance_logs.insert_many(logs)
        resp = test_client.get("/analytics/employees/EMP9950/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total_overtime_minutes"], 75)

    def test_attendance_pct_and_half_up_rounding(self):
        # We test R8 half-up rounding specifically:
        # e.g. 2.5 / 16 * 100 = 15.625
        # Python's built-in round(15.625, 2) produces 15.62 (rounds to even).
        # Half-up rounding MUST produce 15.63.
        # July 10, 2026 is Friday. Between July 10 and July 31 there are 16 weekdays!
        self._create_employee("EMP9951", joined_on="2026-07-10")
        logs = [
            {"emp_code": "EMP9951", "date": "2026-07-10", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9951", "date": "2026-07-13", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP9951", "date": "2026-07-14", "status": "PRESENT", "half_day": True, "late_minutes": 0, "overtime_minutes": 0},
        ]
        db.attendance_logs.insert_many(logs)
        resp = test_client.get("/analytics/employees/EMP9951/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["working_days"], 16)
        self.assertEqual(data["present_days"], 2.5)
        # 2.5 / 16 * 100 = 15.625 -> half-up rounding yields 15.63
        self.assertEqual(data["attendance_pct"], 15.63)

    def test_employee_with_no_attendance_logs(self):
        self._create_employee("EMP9952", joined_on="2026-07-01")
        resp = test_client.get("/analytics/employees/EMP9952/monthly?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["emp_code"], "EMP9952")
        self.assertEqual(data["month"], "2026-07")
        self.assertEqual(data["working_days"], 23)
        self.assertEqual(data["present_days"], 0.0)
        self.assertEqual(data["leave_days"], 0)
        self.assertEqual(data["late_count"], 0)
        self.assertEqual(data["total_late_minutes"], 0)
        self.assertEqual(data["total_overtime_minutes"], 0)
        self.assertEqual(data["attendance_pct"], 0.0)

    def test_aggregation_pipeline_used(self):
        self._create_employee("EMP9953", joined_on="2026-07-01")
        with patch.object(main_module, "build_employee_monthly_pipeline", wraps=main_module.build_employee_monthly_pipeline) as mock_pipe:
            resp = test_client.get("/analytics/employees/EMP9953/monthly?month=2026-07")
            self.assertEqual(resp.status_code, 200)
            mock_pipe.assert_called_once_with("EMP9953", "2026-07")
        # Verify the pipeline structure built by the helper
        pipeline = main_module.build_employee_monthly_pipeline("EMP9953", "2026-07")
        self.assertEqual(pipeline[0]["$match"]["emp_code"], "EMP9953")
        self.assertEqual(pipeline[0]["$match"]["date"]["$gte"], "2026-07-01")
        self.assertEqual(pipeline[0]["$match"]["date"]["$lte"], "2026-07-31")
        self.assertIn("$group", pipeline[2])


class TestDepartmentSummaryAnalytics(unittest.TestCase):
    def setUp(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def tearDown(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def _create_employee(self, emp_code, department="Engineering", joined_on="2026-01-01"):
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": f"User {emp_code}",
            "email": f"{emp_code.lower()}@example.com",
            "department": department,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined_on,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })

    def test_department_summary_schema_and_sorting(self):
        self._create_employee("EMP9960", department="Sales")
        self._create_employee("EMP9961", department="Engineering")
        self._create_employee("EMP9962", department="Finance")

        resp = test_client.get("/analytics/departments/summary?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["month"], "2026-07")
        self.assertIsInstance(data["items"], list)

        # Filter to only our test departments
        test_depts = [item for item in data["items"] if item["department"] in ["Sales", "Engineering", "Finance"]]
        self.assertEqual([d["department"] for d in test_depts], ["Engineering", "Finance", "Sales"])
        for d in test_depts:
            self.assertIn("headcount", d)
            self.assertIn("present_days", d)
            self.assertIn("avg_work_hours", d)
            self.assertIn("late_count", d)
            self.assertIn("total_late_minutes", d)
            self.assertIn("leave_count", d)
            self.assertIn("on_duty_count", d)

    def test_month_and_department_parameters(self):
        self._create_employee("EMP9963", department="Support")

        # Invalid month
        for bad_m in ["2026-13", "2026-00", "2026", "2026-7", "invalid"]:
            resp = test_client.get(f"/analytics/departments/summary?month={bad_m}")
            self.assertEqual(resp.status_code, 422)

        # Missing month
        resp_missing = test_client.get("/analytics/departments/summary")
        self.assertEqual(resp_missing.status_code, 422)

        # Filter by department
        resp_dept = test_client.get("/analytics/departments/summary?month=2026-07&department=Support")
        self.assertEqual(resp_dept.status_code, 200)
        items = resp_dept.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["department"], "Support")

        # Filter by department with no employees
        resp_none = test_client.get("/analytics/departments/summary?month=2026-07&department=NonExistentDept")
        self.assertEqual(resp_none.status_code, 200)
        self.assertEqual(resp_none.json()["items"], [])

    def test_headcount_includes_zero_log_employees(self):
        self._create_employee("EMP9964", department="QA", joined_on="2026-07-01")
        self._create_employee("EMP9965", department="QA", joined_on="2026-07-01")
        self._create_employee("EMP9966", department="QA", joined_on="2026-07-01")

        db.attendance_logs.insert_one({
            "emp_code": "EMP9964",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        })

        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=QA")
        self.assertEqual(resp.status_code, 200)
        qa = resp.json()["items"][0]
        self.assertEqual(qa["headcount"], 3)
        self.assertEqual(qa["present_days"], 1.0)

    def test_exclude_future_joiners_from_headcount(self):
        self._create_employee("EMP9967", department="DevOps", joined_on="2026-07-31")
        self._create_employee("EMP9968", department="DevOps", joined_on="2026-08-01")
        self._create_employee("EMP9969", department="DevOps", joined_on="2026-09-01")

        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=DevOps")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["headcount"], 1)

    def test_department_with_all_future_joiners_omitted(self):
        self._create_employee("EMP9970", department="FutureOnly", joined_on="2026-08-15")
        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=FutureOnly")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])

    def test_attendance_status_metrics(self):
        self._create_employee("EMP9971", department="HR", joined_on="2026-07-01")
        logs = [
            {"emp_code": "EMP9971", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 9.0},
            {"emp_code": "EMP9971", "date": "2026-07-07", "status": "WFH", "half_day": False, "late_minutes": 0, "work_hours": 8.0},
            {"emp_code": "EMP9971", "date": "2026-07-08", "status": "ON_DUTY", "half_day": False, "late_minutes": 0, "work_hours": 7.0},
            {"emp_code": "EMP9971", "date": "2026-07-09", "status": "LEAVE", "half_day": False, "late_minutes": 0, "work_hours": None},
            {"emp_code": "EMP9971", "date": "2026-07-10", "status": "ABSENT", "half_day": False, "late_minutes": 0, "work_hours": None},
        ]
        db.attendance_logs.insert_many(logs)

        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=HR")
        self.assertEqual(resp.status_code, 200)
        hr = resp.json()["items"][0]
        self.assertEqual(hr["present_days"], 3.0)
        self.assertEqual(hr["leave_count"], 1)
        self.assertEqual(hr["on_duty_count"], 1)
        self.assertEqual(hr["avg_work_hours"], 8.0)

    def test_half_day_attendance_in_department_summary(self):
        self._create_employee("EMP9972", department="Security", joined_on="2026-07-01")
        logs = [
            {"emp_code": "EMP9972", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 9.0},
            {"emp_code": "EMP9972", "date": "2026-07-07", "status": "PRESENT", "half_day": True, "late_minutes": 0, "work_hours": 4.0},
        ]
        db.attendance_logs.insert_many(logs)

        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=Security")
        self.assertEqual(resp.status_code, 200)
        sec = resp.json()["items"][0]
        self.assertEqual(sec["present_days"], 1.5)
        self.assertEqual(sec["avg_work_hours"], 6.5)

    def test_avg_work_hours_individual_records_not_average_of_averages(self):
        self._create_employee("EMP9973", department="Analytics", joined_on="2026-07-01")
        self._create_employee("EMP9974", department="Analytics", joined_on="2026-07-01")

        logs = [
            {"emp_code": "EMP9973", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 9.0},
            {"emp_code": "EMP9974", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 6.0},
            {"emp_code": "EMP9974", "date": "2026-07-07", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 6.0},
        ]
        db.attendance_logs.insert_many(logs)

        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=Analytics")
        self.assertEqual(resp.status_code, 200)
        dept = resp.json()["items"][0]
        self.assertEqual(dept["avg_work_hours"], 7.0)

    def test_department_with_no_attendance_records(self):
        self._create_employee("EMP9975", department="SilentDept", joined_on="2026-07-01")
        self._create_employee("EMP9976", department="SilentDept", joined_on="2026-07-01")

        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=SilentDept")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 1)
        dept = items[0]
        self.assertEqual(dept["headcount"], 2)
        self.assertEqual(dept["present_days"], 0.0)
        self.assertIsNone(dept["avg_work_hours"])
        self.assertEqual(dept["late_count"], 0)
        self.assertEqual(dept["total_late_minutes"], 0)
        self.assertEqual(dept["leave_count"], 0)
        self.assertEqual(dept["on_duty_count"], 0)

    def test_half_up_rounding_for_avg_work_hours(self):
        self._create_employee("EMP9977", department="RoundingDept", joined_on="2026-07-01")
        logs = []
        for i in range(7):
            logs.append({
                "emp_code": "EMP9977",
                "date": f"2026-07-{i+1:02d}",
                "status": "PRESENT",
                "half_day": False,
                "late_minutes": 0,
                "work_hours": 7.0
            })
        logs.append({
            "emp_code": "EMP9977",
            "date": "2026-07-08",
            "status": "PRESENT",
            "half_day": False,
            "late_minutes": 0,
            "work_hours": 8.0
        })
        db.attendance_logs.insert_many(logs)

        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=RoundingDept")
        self.assertEqual(resp.status_code, 200)
        dept = resp.json()["items"][0]
        self.assertEqual(dept["avg_work_hours"], 7.13)

    def test_weekday_vs_weekend_records_in_department_summary(self):
        self._create_employee("EMP9978", department="WeekendDept", joined_on="2026-07-01")
        logs = [
            {"emp_code": "EMP9978", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "work_hours": 8.0},
            {"emp_code": "EMP9978", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 25, "work_hours": 8.0},
        ]
        db.attendance_logs.insert_many(logs)

        resp = test_client.get("/analytics/departments/summary?month=2026-07&department=WeekendDept")
        self.assertEqual(resp.status_code, 200)
        dept = resp.json()["items"][0]
        self.assertEqual(dept["present_days"], 1.0)
        self.assertEqual(dept["late_count"], 1)
        self.assertEqual(dept["total_late_minutes"], 25)
        self.assertEqual(dept["avg_work_hours"], 8.0)

    def test_aggregation_pipeline_used_for_department_summary(self):
        self._create_employee("EMP9979", department="PipelineDept", joined_on="2026-07-01")
        with patch.object(main_module, "build_department_summary_pipeline", wraps=main_module.build_department_summary_pipeline) as mock_pipe:
            resp = test_client.get("/analytics/departments/summary?month=2026-07&department=PipelineDept")
            self.assertEqual(resp.status_code, 200)
            mock_pipe.assert_called_once_with("2026-07", "PipelineDept")
        pipeline = main_module.build_department_summary_pipeline("2026-07", "PipelineDept")
        self.assertEqual(pipeline[0]["$match"]["department"], "PipelineDept")
        self.assertEqual(pipeline[0]["$match"]["joined_on"]["$lte"], "2026-07-31")
        self.assertEqual(pipeline[1]["$lookup"]["from"], "attendance_logs")


class TestLateLeaderboardAnalytics(unittest.TestCase):
    def setUp(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def tearDown(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def _create_employee(self, emp_code, name=None, department="Engineering", joined_on="2026-01-01"):
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": name or f"User {emp_code}",
            "email": f"{emp_code.lower()}@example.com",
            "department": department,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined_on,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })

    def test_response_schema_and_parameter_validation(self):
        self._create_employee("EMP9980", name="Late User 1", department="LeaderboardSchemaDept")
        db.attendance_logs.insert_one({
            "emp_code": "EMP9980",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 25,
            "overtime_minutes": 0,
            "half_day": False,
        })

        # Valid default query with isolated department
        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=LeaderboardSchemaDept")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["month"], "2026-07")
        self.assertIsInstance(data["items"], list)
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["rank"], 1)
        self.assertEqual(item["emp_code"], "EMP9980")
        self.assertEqual(item["name"], "Late User 1")
        self.assertEqual(item["department"], "LeaderboardSchemaDept")
        self.assertEqual(item["total_late_minutes"], 25)
        self.assertEqual(item["late_count"], 1)

        # Invalid month formats
        for bad_m in ["2026-13", "2026-00", "2026", "2026-7", "invalid"]:
            resp = test_client.get(f"/analytics/leaderboard/late?month={bad_m}")
            self.assertEqual(resp.status_code, 422)

        # Missing month
        resp_missing = test_client.get("/analytics/leaderboard/late")
        self.assertEqual(resp_missing.status_code, 422)

        # Invalid limits (min 1, max 50)
        self.assertEqual(test_client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code, 422)
        self.assertEqual(test_client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code, 422)
        self.assertEqual(test_client.get("/analytics/leaderboard/late?month=2026-07&limit=-5").status_code, 422)

    def test_late_counts_and_total_late_minutes(self):
        self._create_employee("EMP9981", name="Multiple Late User")
        db.attendance_logs.insert_many([
            {"emp_code": "EMP9981", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 15},
            {"emp_code": "EMP9981", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 25},
            {"emp_code": "EMP9981", "date": "2026-07-08", "status": "PRESENT", "late_minutes": 0},
        ])

        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        user_item = [it for it in items if it["emp_code"] == "EMP9981"][0]
        self.assertEqual(user_item["late_count"], 2)
        self.assertEqual(user_item["total_late_minutes"], 40)

    def test_competition_ranking_1_2_2_4(self):
        self._create_employee("EMP9982", name="User A", department="RanksDept")
        self._create_employee("EMP9983", name="User B", department="RanksDept")
        self._create_employee("EMP9984", name="User C", department="RanksDept")
        self._create_employee("EMP9985", name="User D", department="RanksDept")

        db.attendance_logs.insert_many([
            {"emp_code": "EMP9982", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 60},
            {"emp_code": "EMP9983", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 40},
            {"emp_code": "EMP9984", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 40},
            {"emp_code": "EMP9985", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 20},
        ])

        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=RanksDept")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 4)

        ranks = [it["rank"] for it in items]
        self.assertEqual(ranks, [1, 2, 2, 4])
        self.assertEqual(items[0]["emp_code"], "EMP9982")
        self.assertEqual(items[3]["emp_code"], "EMP9985")

    def test_deterministic_emp_code_ordering_for_ties(self):
        self._create_employee("EMP9987", name="User Y", department="TieDept")
        self._create_employee("EMP9986", name="User X", department="TieDept")

        db.attendance_logs.insert_many([
            {"emp_code": "EMP9987", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 45},
            {"emp_code": "EMP9986", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 45},
        ])

        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=TieDept")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["emp_code"], "EMP9986")
        self.assertEqual(items[1]["emp_code"], "EMP9987")
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[1]["rank"], 1)

    def test_tie_crossing_result_limit(self):
        self._create_employee("EMP9988", name="Lead 1", department="LimitDept")
        self._create_employee("EMP9989", name="Lead 2", department="LimitDept")
        self._create_employee("EMP9990", name="Lead 3", department="LimitDept")
        self._create_employee("EMP9991", name="Lead 4", department="LimitDept")

        db.attendance_logs.insert_many([
            {"emp_code": "EMP9988", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 50},
            {"emp_code": "EMP9989", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 30},
            {"emp_code": "EMP9990", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 30},
            {"emp_code": "EMP9991", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 10},
        ])

        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=LimitDept&limit=2")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual([it["rank"] for it in items], [1, 2, 2])
        self.assertEqual([it["emp_code"] for it in items], ["EMP9988", "EMP9989", "EMP9990"])

    def test_zero_late_minutes_and_empty_results(self):
        self._create_employee("EMP9992", name="Punctual User", department="ZeroDept")
        db.attendance_logs.insert_one({
            "emp_code": "EMP9992",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 0,
        })

        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=ZeroDept")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"month": "2026-07", "items": []})

    def test_department_filter_ranks_within_department(self):
        self._create_employee("EMP9993", department="DeptA")
        self._create_employee("EMP9994", department="DeptB")

        db.attendance_logs.insert_many([
            {"emp_code": "EMP9993", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 100},
            {"emp_code": "EMP9994", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 80},
        ])

        resp_b = test_client.get("/analytics/leaderboard/late?month=2026-07&department=DeptB")
        self.assertEqual(resp_b.status_code, 200)
        items = resp_b.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["emp_code"], "EMP9994")
        self.assertEqual(items[0]["rank"], 1)

    def test_weekend_records_contribute_to_lateness(self):
        self._create_employee("EMP9995", department="WeekendLateDept")
        db.attendance_logs.insert_many([
            {"emp_code": "EMP9995", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 15},
            {"emp_code": "EMP9995", "date": "2026-07-12", "status": "PRESENT", "late_minutes": 25},
        ])

        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=WeekendLateDept")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["total_late_minutes"], 40)
        self.assertEqual(items[0]["late_count"], 2)

    def test_orphaned_logs_ignored(self):
        db.attendance_logs.insert_one({
            "emp_code": "EMP9996",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 999,
        })

        resp = test_client.get("/analytics/leaderboard/late?month=2026-07")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("EMP9996", [it["emp_code"] for it in resp.json()["items"]])

    def test_reporting_period_boundaries(self):
        self._create_employee("EMP9997", department="BoundaryDept")
        db.attendance_logs.insert_many([
            {"emp_code": "EMP9997", "date": "2026-06-30", "status": "PRESENT", "late_minutes": 50},
            {"emp_code": "EMP9997", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 20},
            {"emp_code": "EMP9997", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 40},
        ])

        resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=BoundaryDept")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["total_late_minutes"], 20)
        self.assertEqual(items[0]["late_count"], 1)

    def test_aggregation_pipeline_used_for_leaderboard(self):
        self._create_employee("EMP9998", department="PipeLateDept")
        db.attendance_logs.insert_one({
            "emp_code": "EMP9998",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 10,
        })
        with patch.object(main_module, "build_late_leaderboard_pipeline", wraps=main_module.build_late_leaderboard_pipeline) as mock_pipe:
            resp = test_client.get("/analytics/leaderboard/late?month=2026-07&department=PipeLateDept&limit=5")
            self.assertEqual(resp.status_code, 200)
            mock_pipe.assert_called_once_with("2026-07", 5, "PipeLateDept")
        pipeline = main_module.build_late_leaderboard_pipeline("2026-07", 5, "PipeLateDept")
        self.assertEqual(pipeline[0]["$match"]["date"]["$gte"], "2026-07-01")
        self.assertEqual(pipeline[0]["$match"]["late_minutes"]["$gt"], 0)
        window_stage = [st for st in pipeline if "$setWindowFields" in st][0]
        self.assertIn("rank", window_stage["$setWindowFields"]["output"])
        self.assertIn("$rank", window_stage["$setWindowFields"]["output"]["rank"])


class TestDepartmentTrendAnalytics(unittest.TestCase):
    def setUp(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def tearDown(self):
        db.employees.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP99\d{2,4}$"}})

    def _create_employee(self, emp_code, name=None, department="Engineering", joined_on="2026-01-01"):
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": name or f"User {emp_code}",
            "email": f"{emp_code.lower()}@example.com",
            "department": department,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined_on,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })

    def test_unknown_department_returns_404(self):
        resp = test_client.get("/analytics/departments/UnknownDept99/trend?from=2026-07-01&to=2026-07-05")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("detail", resp.json())

    def test_parameter_validation_422(self):
        self._create_employee("EMP9901", department="TrendValDept")

        # Invalid date format
        self.assertEqual(test_client.get("/analytics/departments/TrendValDept/trend?from=invalid&to=2026-07-05").status_code, 422)
        self.assertEqual(test_client.get("/analytics/departments/TrendValDept/trend?from=2026-07-01&to=2026-13-01").status_code, 422)

        # to < from
        resp_reversed = test_client.get("/analytics/departments/TrendValDept/trend?from=2026-07-10&to=2026-07-05")
        self.assertEqual(resp_reversed.status_code, 422)

        # Range > 92 days (e.g. 100 days)
        resp_large = test_client.get("/analytics/departments/TrendValDept/trend?from=2026-01-01&to=2026-04-10")
        self.assertEqual(resp_large.status_code, 422)

        # Missing required parameters
        self.assertEqual(test_client.get("/analytics/departments/TrendValDept/trend?from=2026-07-01").status_code, 422)
        self.assertEqual(test_client.get("/analytics/departments/TrendValDept/trend?to=2026-07-05").status_code, 422)

    def test_response_schema_and_ascending_order(self):
        self._create_employee("EMP9902", department="TrendOrderDept", joined_on="2026-07-01")
        resp = test_client.get("/analytics/departments/TrendOrderDept/trend?from=2026-07-01&to=2026-07-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["department"], "TrendOrderDept")
        self.assertIsInstance(data["items"], list)
        self.assertEqual(len(data["items"]), 7)

        expected_dates = [
            "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04",
            "2026-07-05", "2026-07-06", "2026-07-07"
        ]
        actual_dates = [item["date"] for item in data["items"]]
        self.assertEqual(actual_dates, expected_dates)

        for item in data["items"]:
            self.assertIn("date", item)
            self.assertIn("is_working_day", item)
            self.assertIn("headcount", item)
            self.assertIn("present_count", item)
            self.assertIn("late_count", item)
            self.assertIn("attendance_rate", item)
            self.assertIn("moving_avg_7d", item)

    def test_gap_filling_for_weekends_and_missing_records(self):
        self._create_employee("EMP9903", department="TrendGapDept", joined_on="2026-07-01")
        db.attendance_logs.insert_one({
            "emp_code": "EMP9903",
            "date": "2026-07-03",
            "status": "PRESENT",
            "late_minutes": 0,
            "half_day": False
        })

        resp = test_client.get("/analytics/departments/TrendGapDept/trend?from=2026-07-01&to=2026-07-05")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 5)

        # 2026-07-01 (Wed) - no logs -> present_count=0.0, rate=0.0
        self.assertTrue(items[0]["is_working_day"])
        self.assertEqual(items[0]["present_count"], 0.0)
        self.assertEqual(items[0]["late_count"], 0)
        self.assertEqual(items[0]["attendance_rate"], 0.0)

        # 2026-07-03 (Fri) - 1 log -> present_count=1.0, rate=1.0
        self.assertTrue(items[2]["is_working_day"])
        self.assertEqual(items[2]["present_count"], 1.0)
        self.assertEqual(items[2]["attendance_rate"], 1.0)

        # 2026-07-04 (Sat) - weekend -> is_working_day=False, attendance_rate=None
        self.assertFalse(items[3]["is_working_day"])
        self.assertIsNone(items[3]["attendance_rate"])

        # 2026-07-05 (Sun) - weekend -> is_working_day=False, attendance_rate=None
        self.assertFalse(items[4]["is_working_day"])
        self.assertIsNone(items[4]["attendance_rate"])

    def test_attendance_metrics_and_half_day(self):
        self._create_employee("EMP9904", department="TrendMetricDept", joined_on="2026-07-01")
        self._create_employee("EMP9905", department="TrendMetricDept", joined_on="2026-07-01")

        db.attendance_logs.insert_many([
            {"emp_code": "EMP9904", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 15, "half_day": False},
            {"emp_code": "EMP9905", "date": "2026-07-01", "status": "WFH", "late_minutes": 0, "half_day": True},
        ])

        resp = test_client.get("/analytics/departments/TrendMetricDept/trend?from=2026-07-01&to=2026-07-01")
        self.assertEqual(resp.status_code, 200)
        item = resp.json()["items"][0]
        self.assertEqual(item["headcount"], 2)
        self.assertEqual(item["present_count"], 1.5)
        self.assertEqual(item["late_count"], 1)
        self.assertEqual(item["attendance_rate"], 0.75)

    def test_presence_statuses_vs_absent_leave(self):
        self._create_employee("EMP9906", department="TrendStatusDept", joined_on="2026-07-01")

        db.attendance_logs.insert_many([
            {"emp_code": "EMP9906", "date": "2026-07-01", "status": "ABSENT", "late_minutes": 0, "half_day": False},
            {"emp_code": "EMP9906", "date": "2026-07-02", "status": "LEAVE", "late_minutes": 0, "half_day": False},
            {"emp_code": "EMP9906", "date": "2026-07-03", "status": "ON_DUTY", "late_minutes": 0, "half_day": False},
        ])

        resp = test_client.get("/analytics/departments/TrendStatusDept/trend?from=2026-07-01&to=2026-07-03")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        # ABSENT
        self.assertEqual(items[0]["present_count"], 0.0)
        self.assertEqual(items[0]["attendance_rate"], 0.0)
        # LEAVE
        self.assertEqual(items[1]["present_count"], 0.0)
        self.assertEqual(items[1]["attendance_rate"], 0.0)
        # ON_DUTY
        self.assertEqual(items[2]["present_count"], 1.0)
        self.assertEqual(items[2]["attendance_rate"], 1.0)

    def test_weekend_record_presence_and_lateness(self):
        self._create_employee("EMP9907", department="TrendWeekendDept", joined_on="2026-07-01")
        db.attendance_logs.insert_one({
            "emp_code": "EMP9907",
            "date": "2026-07-04",  # Saturday
            "status": "PRESENT",
            "late_minutes": 20,
            "half_day": False
        })

        resp = test_client.get("/analytics/departments/TrendWeekendDept/trend?from=2026-07-04&to=2026-07-04")
        self.assertEqual(resp.status_code, 200)
        item = resp.json()["items"][0]
        self.assertFalse(item["is_working_day"])
        self.assertEqual(item["present_count"], 1.0)
        self.assertEqual(item["late_count"], 1)
        self.assertIsNone(item["attendance_rate"])

    def test_headcount_by_joined_on(self):
        self._create_employee("EMP9908", department="TrendJoinDept", joined_on="2026-07-01")
        self._create_employee("EMP9909", department="TrendJoinDept", joined_on="2026-07-03")

        resp = test_client.get("/analytics/departments/TrendJoinDept/trend?from=2026-07-01&to=2026-07-03")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(items[0]["date"], "2026-07-01")
        self.assertEqual(items[0]["headcount"], 1)
        self.assertEqual(items[1]["date"], "2026-07-02")
        self.assertEqual(items[1]["headcount"], 1)
        self.assertEqual(items[2]["date"], "2026-07-03")
        self.assertEqual(items[2]["headcount"], 2)

    def test_moving_average_7d_full_and_partial_windows(self):
        self._create_employee("EMP9910", department="TrendMADept", joined_on="2026-07-01")

        db.attendance_logs.insert_many([
            {"emp_code": "EMP9910", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            # 2026-07-02 no logs -> rate = 0.0
            {"emp_code": "EMP9910", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            # 2026-07-04 Saturday (weekend)
            # 2026-07-05 Sunday (weekend)
            {"emp_code": "EMP9910", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            {"emp_code": "EMP9910", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            {"emp_code": "EMP9910", "date": "2026-07-08", "status": "PRESENT", "late_minutes": 0, "half_day": False},
        ])

        resp = test_client.get("/analytics/departments/TrendMADept/trend?from=2026-07-01&to=2026-07-08")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 8)

        # Day 1 (07-01): rate 1.0 -> MA 1.0
        self.assertEqual(items[0]["attendance_rate"], 1.0)
        self.assertEqual(items[0]["moving_avg_7d"], 1.0)

        # Day 2 (07-02): rate 0.0 -> MA 0.5 (mean of 1.0, 0.0)
        self.assertEqual(items[1]["attendance_rate"], 0.0)
        self.assertEqual(items[1]["moving_avg_7d"], 0.5)

        # Day 3 (07-03): rate 1.0 -> MA 0.6667 (mean of 1.0, 0.0, 1.0)
        self.assertEqual(items[2]["attendance_rate"], 1.0)
        self.assertEqual(items[2]["moving_avg_7d"], 0.6667)

        # Day 4 (07-04, Sat): rate None -> MA 0.6667 (non-null rates in window)
        self.assertIsNone(items[3]["attendance_rate"])
        self.assertEqual(items[3]["moving_avg_7d"], 0.6667)

        # Day 5 (07-05, Sun): rate None -> MA 0.6667
        self.assertIsNone(items[4]["attendance_rate"])
        self.assertEqual(items[4]["moving_avg_7d"], 0.6667)

        # Day 6 (07-06, Mon): rate 1.0 -> MA 0.75 (mean of 1.0, 0.0, 1.0, 1.0)
        self.assertEqual(items[5]["attendance_rate"], 1.0)
        self.assertEqual(items[5]["moving_avg_7d"], 0.75)

        # Day 7 (07-07, Tue): rate 1.0 -> MA 0.8 (mean of 1.0, 0.0, 1.0, 1.0, 1.0)
        self.assertEqual(items[6]["attendance_rate"], 1.0)
        self.assertEqual(items[6]["moving_avg_7d"], 0.8)

        # Day 8 (07-08, Wed): 7-row window is 07-02 to 07-08 (07-01 is excluded).
        # Non-null rates: 07-02 (0.0), 07-03 (1.0), 07-06 (1.0), 07-07 (1.0), 07-08 (1.0) -> mean = 4.0 / 5 = 0.8
        self.assertEqual(items[7]["attendance_rate"], 1.0)
        self.assertEqual(items[7]["moving_avg_7d"], 0.8)

    def test_empty_department_logs_and_weekend_only_query(self):
        self._create_employee("EMP9911", department="TrendEmptyDept", joined_on="2026-07-01")

        # Query weekends only: 2026-07-04 (Sat) to 2026-07-05 (Sun)
        resp = test_client.get("/analytics/departments/TrendEmptyDept/trend?from=2026-07-04&to=2026-07-05")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertFalse(it["is_working_day"])
            self.assertIsNone(it["attendance_rate"])
            self.assertIsNone(it["moving_avg_7d"])

    def test_pipeline_aggregation_used_for_trend(self):
        self._create_employee("EMP9912", department="TrendPipeDept", joined_on="2026-07-01")
        with patch.object(main_module, "build_department_trend_pipeline", wraps=main_module.build_department_trend_pipeline) as mock_pipe:
            resp = test_client.get("/analytics/departments/TrendPipeDept/trend?from=2026-07-01&to=2026-07-03")
            self.assertEqual(resp.status_code, 200)
            mock_pipe.assert_called_once_with("TrendPipeDept", "2026-07-01", "2026-07-03")

        pipeline = main_module.build_department_trend_pipeline("TrendPipeDept", "2026-07-01", "2026-07-03")
        self.assertEqual(pipeline[0]["$match"]["date"]["$gte"], "2026-07-01")
        self.assertEqual(pipeline[0]["$match"]["date"]["$lte"], "2026-07-03")
        densify_stages = [st for st in pipeline if "$densify" in st]
        self.assertEqual(len(densify_stages), 1)
        self.assertEqual(densify_stages[0]["$densify"]["field"], "d_dt")
        window_stages = [st for st in pipeline if "$setWindowFields" in st]
        self.assertEqual(len(window_stages), 1)
        self.assertIn("moving_avg_7d", window_stages[0]["$setWindowFields"]["output"])


class TestAdminExplainEndpoint(unittest.TestCase):
    def test_unknown_endpoint_rejected_with_422(self):
        resp = test_client.get("/admin/explain/unknown_endpoint")
        self.assertEqual(resp.status_code, 422)

        resp2 = test_client.get("/admin/explain/arbitrary_collection")
        self.assertEqual(resp2.status_code, 422)

    def test_explain_attendance_list_success_and_structure(self):
        resp = test_client.get(
            "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31&status=PRESENT&page=1&page_size=20"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["endpoint"], "attendance_list")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)
        exp = data["explain"]
        self.assertIn("executionStats", exp)
        self.assertIn("queryPlanner", exp)

        # Confirm IXSCAN and absence of COLLSCAN
        exp_str = str(exp)
        self.assertIn("IXSCAN", exp_str)
        self.assertNotIn("COLLSCAN", exp_str)

    def test_explain_attendance_list_invalid_dates_422(self):
        resp = test_client.get("/admin/explain/attendance_list?date_from=2026-07-31&date_to=2026-07-01")
        self.assertEqual(resp.status_code, 422)

    def test_explain_employee_monthly_success_and_structure(self):
        resp = test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["endpoint"], "employee_monthly")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)
        exp = data["explain"]

        exp_str = str(exp)
        self.assertIn("IXSCAN", exp_str)
        self.assertNotIn("COLLSCAN", exp_str)

    def test_explain_employee_monthly_missing_params_422(self):
        # Missing month
        self.assertEqual(test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001").status_code, 422)
        # Missing emp_code
        self.assertEqual(test_client.get("/admin/explain/employee_monthly?month=2026-07").status_code, 422)
        # Invalid month pattern
        self.assertEqual(test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13").status_code, 422)

    def test_explain_department_summary_success_and_structure(self):
        resp = test_client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["endpoint"], "department_summary")
        self.assertEqual(data["collection"], "employees")
        self.assertIn("explain", data)
        exp = data["explain"]

        exp_str = str(exp)
        self.assertIn("IXSCAN", exp_str)
        self.assertNotIn("COLLSCAN", exp_str)

    def test_explain_department_summary_missing_month_422(self):
        self.assertEqual(test_client.get("/admin/explain/department_summary").status_code, 422)
        self.assertEqual(test_client.get("/admin/explain/department_summary?month=invalid").status_code, 422)

    def test_explain_late_leaderboard_success_and_structure(self):
        resp = test_client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10&department=Engineering")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["endpoint"], "late_leaderboard")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)
        exp = data["explain"]

        exp_str = str(exp)
        self.assertIn("IXSCAN", exp_str)
        self.assertNotIn("COLLSCAN", exp_str)

    def test_explain_late_leaderboard_validation_422(self):
        # Missing month
        self.assertEqual(test_client.get("/admin/explain/late_leaderboard").status_code, 422)
        # Invalid limit (< 1 or > 50)
        self.assertEqual(test_client.get("/admin/explain/late_leaderboard?month=2026-07&limit=0").status_code, 422)
        self.assertEqual(test_client.get("/admin/explain/late_leaderboard?month=2026-07&limit=51").status_code, 422)

    def test_explain_department_trend_success_and_structure(self):
        resp = test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["endpoint"], "department_trend")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)
        exp = data["explain"]

        exp_str = str(exp)
        self.assertIn("IXSCAN", exp_str)
        self.assertNotIn("COLLSCAN", exp_str)

    def test_explain_department_trend_validation_422(self):
        # Missing department
        self.assertEqual(test_client.get("/admin/explain/department_trend?from=2026-07-01&to=2026-07-05").status_code, 422)
        # Missing from
        self.assertEqual(test_client.get("/admin/explain/department_trend?department=Engineering&to=2026-07-05").status_code, 422)
        # Missing to
        self.assertEqual(test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01").status_code, 422)
        # to < from
        self.assertEqual(test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-05").status_code, 422)
        # range > 92 days
        self.assertEqual(test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-04-10").status_code, 422)

    def test_pipeline_builders_reused_by_explain(self):
        with patch.object(main_module, "build_employee_monthly_pipeline", wraps=main_module.build_employee_monthly_pipeline) as mock_m:
            test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
            mock_m.assert_called_once_with("EMP0001", "2026-07")

        with patch.object(main_module, "build_department_summary_pipeline", wraps=main_module.build_department_summary_pipeline) as mock_s:
            test_client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
            mock_s.assert_called_once_with("2026-07", "Engineering")

        with patch.object(main_module, "build_late_leaderboard_pipeline", wraps=main_module.build_late_leaderboard_pipeline) as mock_l:
            test_client.get("/admin/explain/late_leaderboard?month=2026-07&limit=5&department=Engineering")
            mock_l.assert_called_once_with("2026-07", 5, "Engineering")

        with patch.object(main_module, "build_department_trend_pipeline", wraps=main_module.build_department_trend_pipeline) as mock_t:
            test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
            mock_t.assert_called_once_with("Engineering", "2026-07-01", "2026-07-05")

    def test_explain_database_error_handling_500(self):
        with patch.object(main_module.db, "command", side_effect=Exception("Database connection dropped")):
            resp = test_client.get("/admin/explain/attendance_list")
            self.assertEqual(resp.status_code, 500)
            self.assertEqual(resp.json(), {"detail": "Failed to run MongoDB explain on operation"})


if __name__ == "__main__":
    unittest.main()




