"""Project Tracker REST API for laboratory work variant 6.

Features: CRUD for projects, tasks, users, teams and milestones; Kanban board;
time tracking; notifications; health checks; Prometheus metrics.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import redis
from bson import ObjectId
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pymongo import MongoClient
from pymongo.collection import Collection

MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017/?directConnection=true")
MONGO_DATABASE = os.getenv("MONGO_DATABASE", "project_tracker")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))

mongo_client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000)
database = mongo_client[MONGO_DATABASE]
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=1)
projects: Collection = database["projects"]
tasks: Collection = database["tasks"]
users: Collection = database["users"]
teams: Collection = database["teams"]
milestones: Collection = database["milestones"]
time_entries: Collection = database["time_entries"]
notifications: Collection = database["notifications"]

REQUEST_COUNT = Counter(
    "project_tracker_http_requests_total",
    "Total HTTP requests handled by Project Tracker API",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "project_tracker_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
DOMAIN_EVENTS = Counter(
    "project_tracker_domain_events_total",
    "Number of project tracker domain events",
    ["event_type"],
)
PROJECT_STATUS_GAUGE = Gauge(
    "project_tracker_projects_by_status",
    "Current number of projects grouped by status",
    ["status"],
)
TEAM_TASKS_GAUGE = Gauge(
    "project_tracker_team_tasks",
    "Current number of tasks assigned to each team",
    ["team_id", "team_name"],
)
TEAM_COMPLETED_GAUGE = Gauge(
    "project_tracker_team_completed_tasks",
    "Current number of completed tasks assigned to each team",
    ["team_id", "team_name"],
)

app = FastAPI(
    title="Project Tracker API",
    description=(
        "Учебное API системы управления проектами: проекты, задачи, команды, "
        "этапы, канбан-доска, учёт времени и уведомления."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def collect_metrics(request: Request, call_next):
    started = time.perf_counter()
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        REQUEST_COUNT.labels(request.method, path, status).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(elapsed)


class APIModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class ProjectCreate(APIModel):
    name: str = Field(min_length=2, max_length=120, examples=["Учебный проект"])
    description: str = Field(default="", max_length=1000)
    owner_id: Optional[str] = None
    status: Literal["active", "on_hold", "completed"] = "active"


class ProjectUpdate(APIModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)
    owner_id: Optional[str] = None
    status: Optional[Literal["active", "on_hold", "completed"]] = None


class ProjectOut(APIModel):
    id: str
    name: str
    description: str = ""
    owner_id: Optional[str] = None
    status: str = "active"
    created_at: str


class UserCreate(APIModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254, examples=["student@example.com"])
    role: Literal["admin", "manager", "developer", "viewer"] = "developer"


class UserUpdate(APIModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    email: Optional[str] = Field(default=None, min_length=5, max_length=254)
    role: Optional[Literal["admin", "manager", "developer", "viewer"]] = None


class UserOut(APIModel):
    id: str
    name: str
    email: str
    role: str
    created_at: str


class TeamCreate(APIModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=1000)
    member_ids: list[str] = Field(default_factory=list)


class TeamUpdate(APIModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    member_ids: Optional[list[str]] = None


class TeamOut(APIModel):
    id: str
    name: str
    description: str = ""
    member_ids: list[str] = Field(default_factory=list)
    created_at: str


TaskStatus = Literal["backlog", "todo", "in_progress", "review", "done"]


class TaskCreate(APIModel):
    title: str = Field(min_length=2, max_length=160, examples=["Подготовить макет API"])
    description: str = Field(default="", max_length=2000)
    project_id: str = Field(min_length=1)
    assignee_id: Optional[str] = None
    team_id: Optional[str] = None
    status: TaskStatus = "todo"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    estimated_hours: float = Field(default=0, ge=0, le=10000)
    actual_hours: float = Field(default=0, ge=0, le=10000)


class TaskUpdate(APIModel):
    title: Optional[str] = Field(default=None, min_length=2, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)
    project_id: Optional[str] = None
    assignee_id: Optional[str] = None
    team_id: Optional[str] = None
    status: Optional[TaskStatus] = None
    priority: Optional[Literal["low", "medium", "high", "urgent"]] = None
    estimated_hours: Optional[float] = Field(default=None, ge=0, le=10000)
    actual_hours: Optional[float] = Field(default=None, ge=0, le=10000)


class TaskStatusChange(APIModel):
    status: TaskStatus


class TaskOut(APIModel):
    id: str
    title: str
    description: str = ""
    project_id: str
    assignee_id: Optional[str] = None
    team_id: Optional[str] = None
    status: str = "todo"
    priority: str = "medium"
    estimated_hours: float = 0
    actual_hours: float = 0
    created_at: str
    updated_at: str


class MilestoneCreate(APIModel):
    project_id: str
    title: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=1000)
    due_date: Optional[str] = None
    status: Literal["planned", "in_progress", "completed"] = "planned"


class MilestoneUpdate(APIModel):
    project_id: Optional[str] = None
    title: Optional[str] = Field(default=None, min_length=2, max_length=160)
    description: Optional[str] = Field(default=None, max_length=1000)
    due_date: Optional[str] = None
    status: Optional[Literal["planned", "in_progress", "completed"]] = None


class MilestoneOut(APIModel):
    id: str
    project_id: str
    title: str
    description: str = ""
    due_date: Optional[str] = None
    status: str = "planned"
    created_at: str


class TimeEntryCreate(APIModel):
    hours: float = Field(gt=0, le=24, examples=[1.5])
    note: str = Field(default="", max_length=1000)
    user_id: Optional[str] = None


class NotificationRead(APIModel):
    read: bool = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_object_id(item_id: str) -> ObjectId:
    if not ObjectId.is_valid(item_id):
        raise HTTPException(status_code=400, detail="Invalid resource ID")
    return ObjectId(item_id)


def serialize(document: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if document is None:
        return None
    result = dict(document)
    if "_id" in result:
        result["id"] = str(result.pop("_id"))
    return result


def get_document(collection: Collection, item_id: str) -> dict[str, Any]:
    document = collection.find_one({"_id": to_object_id(item_id)})
    if document is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    return document


def notify(message: str, event_type: str, related_id: Optional[str] = None) -> None:
    notifications.insert_one({
        "message": message,
        "event_type": event_type,
        "related_id": related_id,
        "read": False,
        "created_at": now_iso(),
    })
    DOMAIN_EVENTS.labels(event_type=event_type).inc()
    try:
        redis_client.delete("cache:notifications")
    except redis.RedisError:
        pass


def cached_list(cache_key: str, collection: Collection, query: Optional[dict] = None) -> list[dict[str, Any]]:
    """Cache list responses in Redis; database remains the source of truth."""
    try:
        cached = redis_client.get(cache_key)
        if cached:
            import json
            return json.loads(cached)
    except (redis.RedisError, ValueError):
        pass
    rows = [serialize(doc) for doc in collection.find(query or {}).sort("created_at", -1)]
    try:
        import json
        redis_client.setex(cache_key, CACHE_TTL, json.dumps(rows, ensure_ascii=False))
    except redis.RedisError:
        pass
    return rows


def invalidate_cache() -> None:
    try:
        for key in redis_client.scan_iter("cache:*"):
            redis_client.delete(key)
    except redis.RedisError:
        pass


def create_document(collection: Collection, payload: BaseModel, label: str) -> dict[str, Any]:
    document = payload.model_dump(mode="json")
    document["created_at"] = now_iso()
    if collection is tasks:
        document["updated_at"] = document["created_at"]
    result = collection.insert_one(document)
    document["_id"] = result.inserted_id
    invalidate_cache()
    if collection is tasks:
        notify(f"Создана задача: {document['title']}", "task_created", str(result.inserted_id))
    else:
        DOMAIN_EVENTS.labels(event_type=f"{label}_created").inc()
    return serialize(document)  # type: ignore[return-value]


def update_document(collection: Collection, item_id: str, payload: BaseModel, label: str) -> dict[str, Any]:
    document_id = to_object_id(item_id)
    changes = payload.model_dump(mode="json", exclude_unset=True)
    if not changes:
        return serialize(get_document(collection, item_id))  # type: ignore[return-value]
    if collection is tasks:
        changes["updated_at"] = now_iso()
    result = collection.update_one({"_id": document_id}, {"$set": changes})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Resource not found")
    invalidate_cache()
    DOMAIN_EVENTS.labels(event_type=f"{label}_updated").inc()
    updated = serialize(collection.find_one({"_id": document_id}))
    if collection is tasks and "status" in changes and updated:
        notify(f"Статус задачи «{updated.get('title', item_id)}» изменён на {changes['status']}", "task_status_changed", item_id)
    return updated  # type: ignore[return-value]


def delete_document(collection: Collection, item_id: str, label: str) -> dict[str, str]:
    result = collection.delete_one({"_id": to_object_id(item_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Resource not found")
    invalidate_cache()
    DOMAIN_EVENTS.labels(event_type=f"{label}_deleted").inc()
    return {"message": "Resource deleted successfully"}


@app.get("/health", tags=["system"])
def health_check():
    services = {"mongodb": "healthy", "redis": "healthy"}
    try:
        mongo_client.admin.command("ping")
    except Exception:
        services["mongodb"] = "unhealthy"
    try:
        redis_client.ping()
    except Exception:
        services["redis"] = "unhealthy"
    healthy = all(value == "healthy" for value in services.values())
    return Response(
        content=__import__("json").dumps({"status": "healthy" if healthy else "degraded", "service": "Project Tracker API", **services}),
        media_type="application/json",
        status_code=200 if healthy else 503,
    )


def refresh_business_metrics() -> None:
    """Refresh low-cardinality gauges describing project and team status."""
    PROJECT_STATUS_GAUGE.clear()
    for status in ["active", "on_hold", "completed"]:
        PROJECT_STATUS_GAUGE.labels(status=status).set(projects.count_documents({"status": status}))
    TEAM_TASKS_GAUGE.clear()
    TEAM_COMPLETED_GAUGE.clear()
    for team in teams.find({}):
        team_id = str(team["_id"])
        team_name = str(team.get("name", team_id))
        assigned = list(tasks.find({"team_id": team_id}))
        TEAM_TASKS_GAUGE.labels(team_id=team_id, team_name=team_name).set(len(assigned))
        TEAM_COMPLETED_GAUGE.labels(team_id=team_id, team_name=team_name).set(
            sum(1 for item in assigned if item.get("status") == "done")
        )


@app.get("/metrics", tags=["system"], include_in_schema=False)
def metrics():
    try:
        refresh_business_metrics()
    except Exception:
        # Keep the Prometheus endpoint available even if a storage dependency is down.
        pass
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Projects -------------------------------------------------------------------
@app.post("/projects", response_model=ProjectOut, status_code=201, tags=["projects"])
def create_project(payload: ProjectCreate):
    return create_document(projects, payload, "project")


@app.get("/projects", response_model=list[ProjectOut], tags=["projects"])
def list_projects():
    return cached_list("cache:projects", projects)


@app.get("/projects/{project_id}", response_model=ProjectOut, tags=["projects"])
def get_project(project_id: str):
    return serialize(get_document(projects, project_id))


@app.put("/projects/{project_id}", response_model=ProjectOut, tags=["projects"])
def update_project(project_id: str, payload: ProjectUpdate):
    return update_document(projects, project_id, payload, "project")


@app.delete("/projects/{project_id}", tags=["projects"])
def delete_project(project_id: str):
    return delete_document(projects, project_id, "project")


# Users ---------------------------------------------------------------------
@app.post("/users", response_model=UserOut, status_code=201, tags=["users"])
def create_user(payload: UserCreate):
    if users.find_one({"email": payload.email}):
        raise HTTPException(status_code=409, detail="Email already exists")
    return create_document(users, payload, "user")


@app.get("/users", response_model=list[UserOut], tags=["users"])
def list_users():
    return cached_list("cache:users", users)


@app.get("/users/{user_id}", response_model=UserOut, tags=["users"])
def get_user(user_id: str):
    return serialize(get_document(users, user_id))


@app.put("/users/{user_id}", response_model=UserOut, tags=["users"])
def update_user(user_id: str, payload: UserUpdate):
    if payload.email and users.find_one({"email": payload.email, "_id": {"$ne": to_object_id(user_id)}}):
        raise HTTPException(status_code=409, detail="Email already exists")
    return update_document(users, user_id, payload, "user")


@app.delete("/users/{user_id}", tags=["users"])
def delete_user(user_id: str):
    return delete_document(users, user_id, "user")


# Teams ---------------------------------------------------------------------
@app.post("/teams", response_model=TeamOut, status_code=201, tags=["teams"])
def create_team(payload: TeamCreate):
    return create_document(teams, payload, "team")


@app.get("/teams", response_model=list[TeamOut], tags=["teams"])
def list_teams():
    return cached_list("cache:teams", teams)


@app.get("/teams/{team_id}", response_model=TeamOut, tags=["teams"])
def get_team(team_id: str):
    return serialize(get_document(teams, team_id))


@app.put("/teams/{team_id}", response_model=TeamOut, tags=["teams"])
def update_team(team_id: str, payload: TeamUpdate):
    return update_document(teams, team_id, payload, "team")


@app.delete("/teams/{team_id}", tags=["teams"])
def delete_team(team_id: str):
    return delete_document(teams, team_id, "team")


# Tasks ---------------------------------------------------------------------
@app.post("/tasks", response_model=TaskOut, status_code=201, tags=["tasks"])
def create_task(payload: TaskCreate):
    get_document(projects, payload.project_id)
    return create_document(tasks, payload, "task")


@app.get("/tasks", response_model=list[TaskOut], tags=["tasks"])
def list_tasks(
    project_id: Optional[str] = None,
    status: Optional[TaskStatus] = None,
    team_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    query: dict[str, Any] = {}
    if project_id:
        query["project_id"] = project_id
    if status:
        query["status"] = status
    if team_id:
        query["team_id"] = team_id
    rows = [serialize(doc) for doc in tasks.find(query).sort("created_at", -1).limit(limit)]
    return rows


@app.get("/tasks/{task_id}", response_model=TaskOut, tags=["tasks"])
def get_task(task_id: str):
    return serialize(get_document(tasks, task_id))


@app.put("/tasks/{task_id}", response_model=TaskOut, tags=["tasks"])
def update_task(task_id: str, payload: TaskUpdate):
    if payload.project_id:
        get_document(projects, payload.project_id)
    return update_document(tasks, task_id, payload, "task")


@app.delete("/tasks/{task_id}", tags=["tasks"])
def delete_task(task_id: str):
    return delete_document(tasks, task_id, "task")


@app.post("/tasks/{task_id}/move", response_model=TaskOut, tags=["kanban"])
def move_task(task_id: str, payload: TaskStatusChange):
    return update_document(tasks, task_id, TaskUpdate(status=payload.status), "task")


@app.get("/projects/{project_id}/board", tags=["kanban"])
def get_project_board(project_id: str):
    get_document(projects, project_id)
    board: dict[str, list[dict[str, Any]]] = {key: [] for key in ["backlog", "todo", "in_progress", "review", "done"]}
    for document in tasks.find({"project_id": project_id}).sort("created_at", -1):
        item = serialize(document)
        board.setdefault(item.get("status", "todo"), []).append(item)
    return {"project_id": project_id, "columns": board}


# Milestones ----------------------------------------------------------------
@app.post("/milestones", response_model=MilestoneOut, status_code=201, tags=["milestones"])
def create_milestone(payload: MilestoneCreate):
    get_document(projects, payload.project_id)
    return create_document(milestones, payload, "milestone")


@app.get("/milestones", response_model=list[MilestoneOut], tags=["milestones"])
def list_milestones(project_id: Optional[str] = None):
    query = {"project_id": project_id} if project_id else {}
    return [serialize(doc) for doc in milestones.find(query).sort("created_at", -1)]


@app.get("/milestones/{milestone_id}", response_model=MilestoneOut, tags=["milestones"])
def get_milestone(milestone_id: str):
    return serialize(get_document(milestones, milestone_id))


@app.put("/milestones/{milestone_id}", response_model=MilestoneOut, tags=["milestones"])
def update_milestone(milestone_id: str, payload: MilestoneUpdate):
    if payload.project_id:
        get_document(projects, payload.project_id)
    return update_document(milestones, milestone_id, payload, "milestone")


@app.delete("/milestones/{milestone_id}", tags=["milestones"])
def delete_milestone(milestone_id: str):
    return delete_document(milestones, milestone_id, "milestone")


# Time tracking -------------------------------------------------------------
@app.post("/tasks/{task_id}/time", status_code=201, tags=["time tracking"])
def add_time_entry(task_id: str, payload: TimeEntryCreate):
    task = get_document(tasks, task_id)
    entry = payload.model_dump(mode="json")
    entry.update({"task_id": task_id, "created_at": now_iso()})
    result = time_entries.insert_one(entry)
    tasks.update_one({"_id": task["_id"]}, {"$inc": {"actual_hours": payload.hours}, "$set": {"updated_at": now_iso()}})
    invalidate_cache()
    notify(f"Добавлено {payload.hours:g} ч. к задаче «{task['title']}»", "time_logged", task_id)
    entry["id"] = str(result.inserted_id)
    return entry


@app.get("/tasks/{task_id}/time", tags=["time tracking"])
def list_time_entries(task_id: str):
    get_document(tasks, task_id)
    rows = [serialize(doc) for doc in time_entries.find({"task_id": task_id}).sort("created_at", -1)]
    total = round(sum(float(item["hours"]) for item in rows), 2)
    return {"task_id": task_id, "total_hours": total, "entries": rows}


# Notifications -------------------------------------------------------------
@app.get("/notifications", tags=["notifications"])
def list_notifications(unread_only: bool = False, limit: int = Query(default=100, ge=1, le=500)):
    query = {"read": False} if unread_only else {}
    rows = [serialize(doc) for doc in notifications.find(query).sort("created_at", -1).limit(limit)]
    return rows


@app.post("/notifications/{notification_id}/read", tags=["notifications"])
def mark_notification_read(notification_id: str):
    result = notifications.update_one({"_id": to_object_id(notification_id)}, {"$set": {"read": True}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Resource not found")
    invalidate_cache()
    return {"message": "Notification marked as read", "id": notification_id, "read": True}


# Summary statistics --------------------------------------------------------
@app.get("/stats", tags=["statistics"])
def get_stats():
    project_docs = list(projects.find({}))
    task_docs = list(tasks.find({}))
    team_docs = list(teams.find({}))
    tasks_by_status: dict[str, int] = {}
    projects_by_status: dict[str, int] = {}
    for task in task_docs:
        status = task.get("status", "todo")
        tasks_by_status[status] = tasks_by_status.get(status, 0) + 1
    for project in project_docs:
        status = project.get("status", "active")
        projects_by_status[status] = projects_by_status.get(status, 0) + 1
    team_performance = []
    for team in team_docs:
        team_id = str(team["_id"])
        assigned = [item for item in task_docs if item.get("team_id") == team_id]
        team_performance.append({
            "team_id": team_id,
            "team_name": team.get("name", team_id),
            "tasks": len(assigned),
            "completed_tasks": sum(1 for item in assigned if item.get("status") == "done"),
            "estimated_hours": round(sum(float(item.get("estimated_hours", 0)) for item in assigned), 2),
            "actual_hours": round(sum(float(item.get("actual_hours", 0)) for item in assigned), 2),
        })
    return {
        "projects": len(project_docs),
        "projects_by_status": projects_by_status,
        "tasks": len(task_docs),
        "teams": len(team_docs),
        "tasks_by_status": tasks_by_status,
        "team_performance": team_performance,
        "estimated_hours": round(sum(float(t.get("estimated_hours", 0)) for t in task_docs), 2),
        "actual_hours": round(sum(float(t.get("actual_hours", 0)) for t in task_docs), 2),
    }
