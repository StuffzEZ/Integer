#!/usr/bin/env python3
"""Integer examination server. Keeps answer keys and attempt state on the server."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parent
DATA_FILE = Path(os.environ.get("INTEGER_DATA", ROOT / "integer-data.json"))
ADMIN_PASSWORD = os.environ.get("INTEGER_ADMIN_PASSWORD", "change-me-now")
SESSION_TTL = 60 * 60 * 8


def load_data() -> dict[str, Any]:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"exams": {}, "used_ids": {}, "attempts": {}}


DATA = load_data()
ADMIN_SESSIONS: dict[str, float] = {}
ADMIN_SOCKETS: set[web.WebSocketResponse] = set()


def save_data() -> None:
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(DATA, indent=2), encoding="utf-8")
    temporary.replace(DATA_FILE)


def json_response(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def admin_token(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else request.query.get("token")
    if token and ADMIN_SESSIONS.get(token, 0) > time.time():
        return token
    return None


def require_admin(request: web.Request) -> web.Response | None:
    if not admin_token(request):
        return json_response({"error": "Admin login required."}, 401)
    return None


def public_question(question: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in question.items() if key not in {"correct", "variants"}}


def choose_variant(question: dict[str, Any]) -> dict[str, Any]:
    variants = question.get("variants") or []
    chosen = secrets.choice(variants) if variants else question
    return {
        "prompt": chosen.get("prompt", question.get("prompt", "")),
        "prompt_type": chosen.get("prompt_type", question.get("prompt_type", "text")),
        "image": chosen.get("image", question.get("image", "")),
        "choices": chosen.get("choices", question.get("choices", [])),
        "correct": chosen.get("correct", question.get("correct", "A")),
        "letter_blocking": question.get("letter_blocking", False),
    }


async def broadcast(message: dict[str, Any]) -> None:
    dead = []
    for socket in ADMIN_SOCKETS:
        try:
            await socket.send_json(message)
        except (ConnectionResetError, RuntimeError):
            dead.append(socket)
    for socket in dead:
        ADMIN_SOCKETS.discard(socket)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


async def health(_: web.Request) -> web.Response:
    return json_response({"name": "Integer", "status": "online", "websocket": True})


async def admin_login(request: web.Request) -> web.Response:
    body = await request.json()
    password = str(body.get("password", ""))
    valid = secrets.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(ADMIN_PASSWORD.encode()).digest())
    if not valid:
        return json_response({"error": "Invalid admin password."}, 403)
    token = secrets.token_urlsafe(32)
    ADMIN_SESSIONS[token] = time.time() + SESSION_TTL
    return json_response({"token": token, "expires_in": SESSION_TTL})


async def list_exams(request: web.Request) -> web.Response:
    if (error := require_admin(request)):
        return error
    exams = []
    for exam in DATA["exams"].values():
        exams.append({"id": exam["id"], "title": exam["title"], "duration": exam["duration"], "question_count": len(exam["questions"]), "used_ids": len(exam.get("used_ids", []))})
    return json_response({"exams": exams})


async def create_exam(request: web.Request) -> web.Response:
    if (error := require_admin(request)):
        return error
    body = await request.json()
    title = str(body.get("title", "Untitled exam")).strip()[:120]
    duration = max(1, min(480, int(body.get("duration", 30))))
    questions = body.get("questions", [])
    if not questions:
        return json_response({"error": "Add at least one question."}, 400)
    normalized = []
    for item in questions:
        choices = item.get("choices", [])
        if len(choices) < 2 or len(choices) > 6:
            return json_response({"error": "Each question needs 2 to 6 choices."}, 400)
        correct = str(item.get("correct", "A")).upper()
        if correct not in "ABCDEF"[:len(choices)]:
            return json_response({"error": "Correct answers must match the available choices."}, 400)
        variants = item.get("variants", [])
        normalized.append({
            "prompt": str(item.get("prompt", "")),
            "prompt_type": str(item.get("prompt_type", "text")),
            "image": str(item.get("image", "")),
            "choices": [str(choice) for choice in choices],
            "correct": correct,
            "variants": [{"prompt": str(v.get("prompt", "")), "choices": [str(c) for c in v.get("choices", choices)], "correct": str(v.get("correct", correct)).upper()} for v in variants],
            "letter_blocking": bool(item.get("letter_blocking", False)),
        })
    exam_id = secrets.token_urlsafe(7)
    DATA["exams"][exam_id] = {"id": exam_id, "title": title, "duration": duration, "questions": normalized, "used_ids": []}
    save_data()
    await broadcast({"type": "exam-created", "id": exam_id})
    return json_response({"id": exam_id, "title": title}, 201)


async def delete_user(request: web.Request) -> web.Response:
    if (error := require_admin(request)):
        return error
    user_id = request.match_info["user_id"]
    DATA["used_ids"].pop(user_id, None)
    for exam in DATA["exams"].values():
        if user_id in exam.get("used_ids", []):
            exam["used_ids"].remove(user_id)
    save_data()
    return json_response({"deleted": user_id})


async def start_attempt(request: web.Request) -> web.Response:
    exam_id = request.match_info["exam_id"]
    exam = DATA["exams"].get(exam_id)
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    if not exam:
        return json_response({"error": "Exam not found."}, 404)
    if not user_id or len(user_id) > 80:
        return json_response({"error": "A valid user ID is required."}, 400)
    if user_id in DATA["used_ids"] or user_id in exam.get("used_ids", []):
        return json_response({"error": "This user ID has already been used."}, 409)
    token = secrets.token_urlsafe(32)
    questions = [choose_variant(question) for question in exam["questions"]]
    DATA["attempts"][token] = {"exam_id": exam_id, "user_id": user_id, "questions": questions, "answers": {}, "started": time.time(), "status": "active"}
    DATA["used_ids"][user_id] = {"exam_id": exam_id, "used": time.time()}
    exam.setdefault("used_ids", []).append(user_id)
    save_data()
    return json_response({"attempt": token, "title": exam["title"], "duration": exam["duration"], "questions": [public_question(q) for q in questions]})


def get_attempt(request: web.Request) -> tuple[dict[str, Any] | None, web.Response | None]:
    attempt = DATA["attempts"].get(request.match_info["token"])
    if not attempt:
        return None, json_response({"error": "Attempt not found."}, 404)
    if attempt["status"] != "active":
        return None, json_response({"error": "This attempt is no longer active."}, 409)
    exam = DATA["exams"][attempt["exam_id"]]
    if time.time() > attempt["started"] + exam["duration"] * 60:
        attempt["status"] = "expired"
        save_data()
        return None, json_response({"error": "Time expired."}, 409)
    return attempt, None


async def answer_attempt(request: web.Request) -> web.Response:
    attempt, error = get_attempt(request)
    if error:
        return error
    body = await request.json()
    index = int(body.get("index", -1))
    answer = str(body.get("answer", "")).upper()
    if index < 0 or index >= len(attempt["questions"]) or answer not in "ABCDEF":
        return json_response({"error": "Invalid answer."}, 400)
    attempt["answers"][str(index)] = answer
    save_data()
    return json_response({"saved": True})


async def abandon_attempt(request: web.Request) -> web.Response:
    attempt = DATA["attempts"].get(request.match_info["token"])
    if attempt and attempt["status"] == "active":
        attempt["status"] = "abandoned"
        save_data()
    return json_response({"abandoned": True})


async def submit_attempt(request: web.Request) -> web.Response:
    attempt, error = get_attempt(request)
    if error:
        return error
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 120:
        return json_response({"error": "Enter your name."}, 400)
    if len(attempt["answers"]) != len(attempt["questions"]):
        return json_response({"error": "Answer every question before submitting."}, 400)
    score = sum(attempt["answers"].get(str(i)) == question["correct"] for i, question in enumerate(attempt["questions"]))
    attempt.update({"status": "submitted", "name": name, "score": score, "submitted": time.time()})
    save_data()
    await broadcast({"type": "attempt-submitted", "exam_id": attempt["exam_id"], "name": name, "score": score, "total": len(attempt["questions"])})
    return json_response({"score": score, "total": len(attempt["questions"]), "title": DATA["exams"][attempt["exam_id"]]["title"]})


async def admin_ws(request: web.Request) -> web.StreamResponse:
    if not admin_token(request):
        return web.Response(status=401, text="Admin login required")
    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)
    ADMIN_SOCKETS.add(socket)
    await socket.send_json({"type": "connected"})
    try:
        async for message in socket:
            if message.type == web.WSMsgType.TEXT and message.data == "ping":
                await socket.send_json({"type": "pong"})
    finally:
        ADMIN_SOCKETS.discard(socket)
    return socket


async def index(request: web.Request) -> web.Response:
    filename = request.match_info.get("filename", "index.html")
    if filename not in {"index.html", "exam.html", "backend.html"}:
        raise web.HTTPNotFound()
    return web.FileResponse(ROOT / filename)


app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[cors_middleware])
app.add_routes([
    web.get("/api/health", health), web.post("/api/admin/login", admin_login), web.get("/api/admin/exams", list_exams), web.post("/api/admin/exams", create_exam), web.delete("/api/admin/users/{user_id}", delete_user),
    web.post("/api/exams/{exam_id}/start", start_attempt), web.post("/api/attempts/{token}/answer", answer_attempt), web.post("/api/attempts/{token}/abandon", abandon_attempt), web.post("/api/attempts/{token}/submit", submit_attempt), web.get("/ws/admin", admin_ws),
    web.get("/", index), web.get("/{filename}", index),
])

if __name__ == "__main__":
    print(f"Integer listening on http://0.0.0.0:{os.environ.get('INTEGER_PORT', '8765')}")
    if ADMIN_PASSWORD == "change-me-now":
        print("WARNING: set INTEGER_ADMIN_PASSWORD before exposing this server.")
    web.run_app(app, host=os.environ.get("INTEGER_HOST", "0.0.0.0"), port=int(os.environ.get("INTEGER_PORT", "8765")))
