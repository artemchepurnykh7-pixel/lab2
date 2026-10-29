"""API tests use an in-memory Mongo-like collection; Docker services are not required."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app import main


class Cursor(list):
    def sort(self, key, direction=-1):
        reverse = direction < 0
        return Cursor(sorted(self, key=lambda row: row.get(key, ""), reverse=reverse))

    def limit(self, amount):
        return Cursor(self[:amount])


class FakeCollection:
    def __init__(self):
        self.rows = {}

    def _matches(self, row, query):
        for key, expected in (query or {}).items():
            actual = row.get(key)
            if isinstance(expected, dict) and "$ne" in expected:
                if actual == expected["$ne"]:
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query=None):
        return Cursor([deepcopy(row) for row in self.rows.values() if self._matches(row, query or {})])

    def find_one(self, query=None):
        for row in self.rows.values():
            if self._matches(row, query or {}):
                return deepcopy(row)
        return None

    def insert_one(self, document):
        row = deepcopy(document)
        row["_id"] = row.get("_id", ObjectId())
        self.rows[row["_id"]] = row
        return SimpleNamespace(inserted_id=row["_id"])

    def update_one(self, query, update):
        for key, row in self.rows.items():
            if self._matches(row, query):
                for field, value in update.get("$set", {}).items():
                    row[field] = value
                for field, value in update.get("$inc", {}).items():
                    row[field] = float(row.get(field, 0)) + value
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def delete_one(self, query):
        for key, row in list(self.rows.items()):
            if self._matches(row, query):
                del self.rows[key]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def count_documents(self, query):
        return len(self.find(query))


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value

    def delete(self, *keys):
        count = 0
        for key in keys:
            count += int(self.data.pop(key, None) is not None)
        return count

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return iter([key for key in self.data if key.startswith(prefix)])

    def ping(self):
        return True


@pytest.fixture()
def client(monkeypatch):
    for name in ["projects", "tasks", "users", "teams", "milestones", "time_entries", "notifications"]:
        monkeypatch.setattr(main, name, FakeCollection())
    monkeypatch.setattr(main, "redis_client", FakeRedis())
    monkeypatch.setattr(main, "mongo_client", SimpleNamespace(admin=SimpleNamespace(command=lambda _cmd: {"ok": 1})))
    with TestClient(main.app) as test_client:
        yield test_client


def create_project(client):
    response = client.post("/projects", json={"name": "Demo project", "description": "For tests"})
    assert response.status_code == 201, response.text
    return response.json()


def test_project_and_task_crud(client):
    project = create_project(client)
    created = client.post("/tasks", json={"title": "Implement API", "project_id": project["id"]})
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["status"] == "todo"
    assert client.get(f"/tasks/{task['id']}").status_code == 200
    updated = client.put(f"/tasks/{task['id']}", json={"status": "in_progress"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "in_progress"
    assert client.get(f"/projects/{project['id']}/board").json()["columns"]["in_progress"][0]["id"] == task["id"]
    assert client.delete(f"/tasks/{task['id']}").status_code == 200
    assert client.get(f"/tasks/{task['id']}").status_code == 404


def test_project_required_before_task(client):
    response = client.post("/tasks", json={"title": "Orphan task", "project_id": str(ObjectId())})
    assert response.status_code == 404


def test_time_tracking_and_notification(client):
    project = create_project(client)
    task = client.post("/tasks", json={"title": "Track time", "project_id": project["id"]}).json()
    response = client.post(f"/tasks/{task['id']}/time", json={"hours": 1.5, "note": "Implementation"})
    assert response.status_code == 201, response.text
    entries = client.get(f"/tasks/{task['id']}/time").json()
    assert entries["total_hours"] == 1.5
    assert any(item["event_type"] == "time_logged" for item in client.get("/notifications").json())


def test_validation_and_health(client):
    assert client.post("/projects", json={"name": "x"}).status_code == 422
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert client.get("/metrics").status_code == 200


def test_users_teams_and_milestones_crud(client):
    project = create_project(client)
    user_response = client.post("/users", json={"name": "Alex Student", "email": "alex@example.com", "role": "developer"})
    assert user_response.status_code == 201, user_response.text
    user = user_response.json()
    team_response = client.post("/teams", json={"name": "Backend team", "description": "API work", "member_ids": [user["id"]]})
    assert team_response.status_code == 201, team_response.text
    team = team_response.json()
    assert client.put(f"/teams/{team['id']}", json={"name": "Platform team"}).json()["name"] == "Platform team"
    milestone_response = client.post("/milestones", json={"project_id": project["id"], "title": "First release", "status": "planned"})
    assert milestone_response.status_code == 201, milestone_response.text
    milestone = milestone_response.json()
    assert client.get(f"/milestones/{milestone['id']}").status_code == 200
    assert client.delete(f"/milestones/{milestone['id']}").status_code == 200
    assert client.delete(f"/teams/{team['id']}").status_code == 200
    assert client.delete(f"/users/{user['id']}").status_code == 200


def test_duplicate_user_email_is_rejected(client):
    payload = {"name": "Alex Student", "email": "alex@example.com", "role": "developer"}
    assert client.post("/users", json=payload).status_code == 201
    assert client.post("/users", json=payload).status_code == 409
