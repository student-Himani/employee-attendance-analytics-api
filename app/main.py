"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

import pymongo
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# Create indexes on load as well if DB is accessible
try:
    init_indexes()
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int = 2) -> float:
    d = Decimal(str(val))
    exp = Decimal(10) ** -decimals
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def to_epoch_millis(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    h, m = map(int, shift_start.split(":"))
    y, mon, d = map(int, attendance_date.split("-"))
    start_dt = datetime(y, mon, d, h, m, 0, tzinfo=IST)
    punch_in_ist = punch_in.astimezone(IST) if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc).astimezone(IST)
    elapsed_seconds = int((punch_in_ist - start_dt).total_seconds())
    # R2: strictly more than 10:00 (600 seconds) after shift_start
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    h, m = map(int, shift_end.split(":"))
    y, mon, d = map(int, date_str.split("-"))
    base_date = datetime(y, mon, d, tzinfo=IST)
    if shift_end <= shift_start:
        base_date += timedelta(days=1)
    end_dt = base_date.replace(hour=h, minute=m, second=0, microsecond=0)
    punch_out_ist = punch_out.astimezone(IST) if punch_out.tzinfo else punch_out.replace(tzinfo=timezone.utc).astimezone(IST)
    elapsed_seconds = int((punch_out_ist - end_dt).total_seconds())
    if elapsed_seconds < 0:
        return 0
    minutes = elapsed_seconds // 60
    return minutes if minutes >= 30 else 0


def determine_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = punch_in.astimezone(IST) if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc).astimezone(IST)
    cal_date = punch_in_ist.date()
    if shift_end <= shift_start:
        # Overnight shift
        end_h, end_m = map(int, shift_end.split(":"))
        punch_time_sec = punch_in_ist.hour * 3600 + punch_in_ist.minute * 60 + punch_in_ist.second
        shift_end_sec = end_h * 3600 + end_m * 60
        if punch_time_sec < shift_end_sec:
            return (cal_date - timedelta(days=1)).isoformat()
    return cal_date.isoformat()


def count_working_days(month_str: str, joined_on_str: str) -> int:
    year, month_num = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    start_date = date(year, month_num, 1)
    end_date = date(year, month_num, last_day)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > end_date:
        return 0

    effective_start = max(start_date, joined_date)
    curr = effective_start
    working_days = 0
    while curr <= end_date:
        if curr.weekday() < 5:  # Mon=0, ..., Fri=4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_weekday": {
                    "$lte": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        5
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_weekday",
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_filter = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_filter["department"] = department

    return [
        {
            "$match": match_filter
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "emp_present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$lte": [
                                                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}},
                                                        5
                                                    ]
                                                },
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                            ]
                                        },
                                        {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "emp_leave_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]}
                            ]
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]}
                            ]
                        }
                    }
                },
                "emp_late_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]}
                            ]
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$ifNull": ["$$this.late_minutes", 0]}
                            ]
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$isNumber": "$$this.work_hours"}
                                            ]
                                        },
                                        "$$this.work_hours",
                                        0.0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$isNumber": "$$this.work_hours"}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_hours_count": {"$sum": "$emp_work_hours_count"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$total_work_hours_count", 0]},
                        {"$divide": ["$total_work_hours", "$total_work_hours_count"]},
                        None
                    ]
                }
            }
        },
        {
            "$sort": {"department": 1}
        }
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {
            "$unwind": "$emp"
        }
    ]

    if department is not None:
        pipeline.append({
            "$match": {
                "emp.department": department
            }
        })

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    return pipeline


def build_department_trend_pipeline(department: str, from_date_str: str, to_date_str: str) -> list:
    from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_dt = datetime.strptime(to_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    next_dt = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_date_str, "$lte": to_date_str}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "d_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "d_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, next_dt]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d_dt"}},
                "is_working_day": {"$in": [{"$isoDayOfWeek": "$d_dt"}, [1, 2, 3, 4, 5]]},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]}
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 1}}
                ],
                "as": "dept_members"
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$dept_members"}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$sort": {"date": 1}
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]


def serialize_employee(d: dict) -> dict:
    doc = dict(d)
    doc.pop("_id", None)
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        doc["created_at"] = to_epoch_millis(created_at)
    return doc


def serialize_attendance_record(d: dict) -> dict:
    doc = dict(d)
    doc.pop("_id", None)
    doc.pop("id", None)

    p_in = doc.get("punch_in")
    doc["punch_in"] = to_epoch_millis(p_in) if isinstance(p_in, datetime) else None

    p_out = doc.get("punch_out")
    doc["punch_out"] = to_epoch_millis(p_out) if isinstance(p_out, datetime) else None

    if "late_minutes" not in doc or doc["late_minutes"] is None:
        doc["late_minutes"] = 0
    if "overtime_minutes" not in doc or doc["overtime_minutes"] is None:
        doc["overtime_minutes"] = 0
    if "half_day" not in doc or doc["half_day"] is None:
        doc["half_day"] = False
    if "history" not in doc or doc["history"] is None:
        doc["history"] = []
    else:
        serialized_hist = []
        for h in doc["history"]:
            h_entry = dict(h)
            h_at = h_entry.get("at")
            if isinstance(h_at, datetime):
                h_entry["at"] = to_epoch_millis(h_at)
            if "changes" in h_entry and isinstance(h_entry["changes"], dict):
                ch_dict = {}
                for k, v in h_entry["changes"].items():
                    c_item = dict(v)
                    for bound in ("from", "to"):
                        val = c_item.get(bound)
                        if isinstance(val, datetime):
                            c_item[bound] = to_epoch_millis(val)
                    ch_dict[k] = c_item
                h_entry["changes"] = ch_dict
            serialized_hist.append(h_entry)
        doc["history"] = serialized_hist

    return doc


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        sec = body.punched_at // 1000
        dt_utc = datetime.fromtimestamp(sec, tz=timezone.utc)
    else:
        dt_utc = truncate_to_seconds(datetime.now(timezone.utc))

    attendance_date = determine_attendance_date(dt_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(dt_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        sec = body.punched_at // 1000
        dt_utc = datetime.fromtimestamp(sec, tz=timezone.utc)
    else:
        dt_utc = truncate_to_seconds(datetime.now(timezone.utc))

    # Closes the employee's most recent record whose punch_in is at or before punched_at
    cand = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$lte": dt_utc}},
        sort=[("punch_in", -1)],
    )

    if not cand:
        # Check if there is a punch-in record after punched_at (punch_out <= punch_in)
        future_cand = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$gt": dt_utc}},
            sort=[("punch_in", 1)],
        )
        if future_cand:
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = cand["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422 if punched_at <= punch_in or more than 24 hours after it
    if dt_utc <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    diff_seconds = (dt_utc - punch_in_dt).total_seconds()
    if diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    if cand.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    work_hours = compute_work_hours(punch_in_dt, dt_utc)
    overtime_minutes = compute_overtime(dt_utc, emp["shift_end"], cand["date"], emp["shift_start"])
    half_day = work_hours < 4.50

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": cand["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": dt_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return serialize_attendance_record(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times when setting status to ABSENT or LEAVE")

    max_retries = 5
    for attempt in range(max_retries):
        orig = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not orig:
            raise HTTPException(status_code=404, detail="Attendance record not found")

        target_status = body.status if body.status is not None else orig["status"]

        if target_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(status_code=422, detail="Cannot supply punch times for ABSENT or LEAVE records")
            target_punch_in = None
            target_punch_out = None
        else:
            if body.punch_in is not None:
                sec = body.punch_in // 1000
                target_punch_in = datetime.fromtimestamp(sec, tz=timezone.utc)
            else:
                target_punch_in = orig.get("punch_in")
                if target_punch_in is not None and target_punch_in.tzinfo is None:
                    target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

            if target_punch_in is None:
                raise HTTPException(status_code=422, detail="Presence status requires punch_in")

            calc_date = determine_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
            if calc_date != date:
                raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

            if body.punch_out is not None:
                sec = body.punch_out // 1000
                target_punch_out = datetime.fromtimestamp(sec, tz=timezone.utc)
            else:
                target_punch_out = orig.get("punch_out")
                if target_punch_out is not None and target_punch_out.tzinfo is None:
                    target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)

            if target_punch_out is not None:
                if target_punch_out <= target_punch_in:
                    raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
                if (target_punch_out - target_punch_in).total_seconds() > 86400:
                    raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

        if target_status in ("ABSENT", "LEAVE"):
            new_work_hours = None
            new_late_minutes = 0
            new_overtime_minutes = 0
            new_half_day = False
        else:
            new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
            if target_punch_out is not None:
                new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
                new_overtime_minutes = compute_overtime(target_punch_out, emp["shift_end"], date, emp["shift_start"])
                new_half_day = new_work_hours < 4.50
            else:
                new_work_hours = None
                new_overtime_minutes = 0
                new_half_day = False

        changes = {}
        if orig.get("status") != target_status:
            changes["status"] = {"from": orig.get("status"), "to": target_status}

        orig_p_in = orig.get("punch_in")
        if orig_p_in is not None and orig_p_in.tzinfo is None:
            orig_p_in = orig_p_in.replace(tzinfo=timezone.utc)
        if orig_p_in != target_punch_in:
            changes["punch_in"] = {"from": orig_p_in, "to": target_punch_in}

        orig_p_out = orig.get("punch_out")
        if orig_p_out is not None and orig_p_out.tzinfo is None:
            orig_p_out = orig_p_out.replace(tzinfo=timezone.utc)
        if orig_p_out != target_punch_out:
            changes["punch_out"] = {"from": orig_p_out, "to": target_punch_out}

        if orig.get("work_hours") != new_work_hours:
            changes["work_hours"] = {"from": orig.get("work_hours"), "to": new_work_hours}

        orig_late = orig.get("late_minutes")
        if orig_late is None:
            orig_late = 0
        if orig_late != new_late_minutes:
            changes["late_minutes"] = {"from": orig_late, "to": new_late_minutes}

        orig_ot = orig.get("overtime_minutes")
        if orig_ot is None:
            orig_ot = 0
        if orig_ot != new_overtime_minutes:
            changes["overtime_minutes"] = {"from": orig_ot, "to": new_overtime_minutes}

        orig_hd = orig.get("half_day")
        if orig_hd is None:
            orig_hd = False
        if orig_hd != new_half_day:
            changes["half_day"] = {"from": orig_hd, "to": new_half_day}

        if not changes:
            if attempt > 0:
                raise HTTPException(status_code=409, detail="Concurrent regularization conflict")
            raise HTTPException(status_code=422, detail="Request does not change any fields")

        now_utc = truncate_to_seconds(datetime.now(timezone.utc))
        history_entry = {
            "at": now_utc,
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        orig_hist = orig.get("history")
        if orig_hist:
            hist_query = {"history": orig_hist}
        else:
            hist_query = {"history": {"$in": [None, []]}}

        res = db.attendance_logs.find_one_and_update(
            {"_id": orig["_id"], **hist_query},
            {
                "$set": {
                    "status": target_status,
                    "punch_in": target_punch_in,
                    "punch_out": target_punch_out,
                    "work_hours": new_work_hours,
                    "late_minutes": new_late_minutes,
                    "overtime_minutes": new_overtime_minutes,
                    "half_day": new_half_day,
                },
                "$push": {
                    "history": history_entry
                }
            },
            return_document=pymongo.ReturnDocument.AFTER
        )

        if res is not None:
            return serialize_attendance_record(res)

    raise HTTPException(status_code=409, detail="Concurrent regularization conflict")


# --------------------------------------------------------------------------- #
# Step 4A: Analytics - Employee Monthly Attendance
# --------------------------------------------------------------------------- #
class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyOut)
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_results = list(db.attendance_logs.aggregate(pipeline))

    if agg_results:
        agg = agg_results[0]
        present_days = float(round_half_up(agg.get("present_days", 0.0), 2))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        pct_decimal = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(pct_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Step 4B: Analytics - Department Summary
# --------------------------------------------------------------------------- #
class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut)
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None)
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        raw_avg = r.get("avg_work_hours")
        avg_wh = float(round_half_up(raw_avg, 2)) if raw_avg is not None else None
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(round_half_up(r.get("present_days", 0.0), 2)),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Step 4C: Analytics - Late Attendance Leaderboard
# --------------------------------------------------------------------------- #
class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: list[LeaderboardItem]


@app.get("/analytics/leaderboard/late", response_model=LeaderboardOut)
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": str(r["emp_code"]),
            "name": str(r["name"]),
            "department": str(r["department"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Step 4D: Analytics - Department Attendance Trend
# --------------------------------------------------------------------------- #
class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class TrendOut(BaseModel):
    department: str
    items: list[TrendItem]


@app.get("/analytics/departments/{department}/trend", response_model=TrendOut)
def get_department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if to_dt < from_dt:
        raise HTTPException(status_code=422, detail="to date must be greater than or equal to from date")

    if (to_dt.date() - from_dt.date()).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        raw_rate = r.get("attendance_rate")
        rate = float(round_half_up(raw_rate, 4)) if raw_rate is not None else None

        raw_ma = r.get("moving_avg_7d")
        ma = float(round_half_up(raw_ma, 4)) if raw_ma is not None else None

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(round_half_up(r.get("present_count", 0.0), 2)),
            "late_count": int(r.get("late_count", 0)),
            "attendance_rate": rate,
            "moving_avg_7d": ma
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Step 5: Admin - MongoDB Explain
# --------------------------------------------------------------------------- #
class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


@app.get("/admin/explain/{endpoint}", response_model=ExplainOut)
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown endpoint '{endpoint}'. Allowed endpoints: {', '.join(sorted(valid_endpoints))}"
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        collection_name = "attendance_logs"
        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="Missing required query parameter: emp_code")
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")

        collection_name = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")

        collection_name = "employees"
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")

        limit_val = limit if limit is not None else 10
        collection_name = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(month, limit_val, department)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="Missing required query parameter: department")
        if not from_date:
            raise HTTPException(status_code=422, detail="Missing required query parameter: from")
        if not to_date:
            raise HTTPException(status_code=422, detail="Missing required query parameter: to")

        try:
            from_dt = datetime.strptime(from_date, "%Y-%m-%d")
            to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

        if to_dt < from_dt:
            raise HTTPException(status_code=422, detail="to date must be greater than or equal to from date")

        if (to_dt.date() - from_dt.date()).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        collection_name = "attendance_logs"
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }

    try:
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to run MongoDB explain on operation")

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": jsonable_encoder(raw_explain),
    }



