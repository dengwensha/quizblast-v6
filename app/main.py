from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
import os
import json
import asyncio
import random
import string

from app.db import Base, engine, SessionLocal
from app.models import Question, GameRoom

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
Base.metadata.create_all(bind=engine)

QUESTION_DURATION = int(os.getenv("QUESTION_DURATION", "15"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_COOKIE_NAME = "quizblast_admin_auth"

ROOM_STATES = {}

def is_admin_authenticated(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    return token == "ok"

def require_admin(request: Request):
    if not is_admin_authenticated(request):
        return RedirectResponse(url="/admin-login", status_code=303)
    return None

def db_get_questions():
    db = SessionLocal()
    try:
        return db.query(Question).order_by(Question.id.asc()).all()
    finally:
        db.close()

def db_get_room(room_code: str):
    db = SessionLocal()
    try:
        return db.query(GameRoom).filter(GameRoom.room_code == room_code).first()
    finally:
        db.close()

def db_create_room(room_code: str, host_name: str):
    db = SessionLocal()
    try:
        row = GameRoom(room_code=room_code, host_name=host_name, is_active=True)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()

def generate_room_code(length: int = 6) -> str:
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
        if not db_get_room(code):
            return code

def create_live_room_state(room_code: str):
    ROOM_STATES[room_code] = {
        "clients": [],
        "players": {},
        "answered_players": set(),
        "answer_counts": {"A": 0, "B": 0, "C": 0, "D": 0},
        "current_question_index": 0,
        "quiz_started": False,
        "question_open": False,
        "auto_task": None,
        "host_name": None,
    }

def ensure_room_state(room_code: str):
    if room_code not in ROOM_STATES:
        create_live_room_state(room_code)
    return ROOM_STATES[room_code]

def get_current_question(room_code: str):
    rows = db_get_questions()
    if not rows:
        return {"question_index": 0, "question": "Henüz soru yok", "options": ["-", "-", "-", "-"]}
    room = ensure_room_state(room_code)
    idx = min(room["current_question_index"], len(rows) - 1)
    q = rows[idx]
    return {"id": q.id, "question_index": idx + 1, "question": q.question, "options": [q.option_a, q.option_b, q.option_c, q.option_d]}

def get_correct_letter(room_code: str):
    rows = db_get_questions()
    if not rows:
        return "A"
    room = ensure_room_state(room_code)
    idx = min(room["current_question_index"], len(rows) - 1)
    q = rows[idx]
    return ["A", "B", "C", "D"][q.correct]

async def room_broadcast(room_code: str, payload: dict):
    room = ensure_room_state(room_code)
    dead = []
    for ws in room["clients"]:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in room["clients"]:
            room["clients"].remove(ws)

async def room_broadcast_leaderboard(room_code: str):
    room = ensure_room_state(room_code)
    await room_broadcast(room_code, {"type": "leaderboard", "players": room["players"]})

async def room_broadcast_question(room_code: str):
    await room_broadcast(room_code, {"type": "question", "data": get_current_question(room_code), "duration": QUESTION_DURATION})

async def room_broadcast_answer_stats(room_code: str):
    room = ensure_room_state(room_code)
    await room_broadcast(room_code, {"type": "answer_stats", "counts": room["answer_counts"], "correct_answer": get_correct_letter(room_code)})

async def room_close_question(room_code: str):
    room = ensure_room_state(room_code)
    room["question_open"] = False
    await room_broadcast(room_code, {"type": "question_closed", "correct_answer": get_correct_letter(room_code)})
    await room_broadcast_answer_stats(room_code)

async def room_auto_close_question(room_code: str):
    await asyncio.sleep(QUESTION_DURATION)
    room = ensure_room_state(room_code)
    if room["question_open"]:
        await room_close_question(room_code)

def room_reset_answer_state(room_code: str):
    room = ensure_room_state(room_code)
    room["answered_players"] = set()
    room["answer_counts"] = {"A": 0, "B": 0, "C": 0, "D": 0}

@app.get("/")
def root():
    return FileResponse(os.path.join(BASE_DIR, "static/index.html"))

@app.get("/player")
def player():
    return FileResponse(os.path.join(BASE_DIR, "static/player.html"))

@app.get("/host")
def host():
    return FileResponse(os.path.join(BASE_DIR, "static/host.html"))

@app.get("/admin-login")
def admin_login_page():
    return FileResponse(os.path.join(BASE_DIR, "static/admin_login.html"))

@app.get("/admin")
def admin(request: Request):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect
    return FileResponse(os.path.join(BASE_DIR, "static/admin.html"))

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/admin-login")
async def api_admin_login(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return JSONResponse({"ok": False, "message": "Kullanıcı adı veya şifre hatalı"}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(key=ADMIN_COOKIE_NAME, value="ok", httponly=True, samesite="lax", secure=False)
    return response

@app.post("/api/admin-logout")
def api_admin_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response

@app.get("/api/questions")
def api_list_questions(request: Request):
    if not is_admin_authenticated(request):
        return JSONResponse({"ok": False, "message": "Yetkisiz erişim"}, status_code=401)
    rows = db_get_questions()
    return {"items": [{"id": q.id, "question": q.question, "options": [q.option_a, q.option_b, q.option_c, q.option_d], "correct": q.correct} for q in rows]}

@app.post("/api/questions")
async def api_add_question(request: Request):
    if not is_admin_authenticated(request):
        return JSONResponse({"ok": False, "message": "Yetkisiz erişim"}, status_code=401)
    body = await request.json()
    question = str(body.get("question", "")).strip()
    options = body.get("options", [])
    correct = body.get("correct", None)
    if not question:
        return JSONResponse({"ok": False, "message": "Soru boş olamaz"}, status_code=400)
    if not isinstance(options, list) or len(options) != 4:
        return JSONResponse({"ok": False, "message": "4 seçenek gerekli"}, status_code=400)
    options = [str(x).strip() for x in options]
    if any(not x for x in options):
        return JSONResponse({"ok": False, "message": "Tüm seçenekler dolu olmalı"}, status_code=400)
    if correct not in [0, 1, 2, 3]:
        return JSONResponse({"ok": False, "message": "Doğru cevap 0-3 arası olmalı"}, status_code=400)
    db = SessionLocal()
    try:
        row = Question(question=question, option_a=options[0], option_b=options[1], option_c=options[2], option_d=options[3], correct=correct)
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"ok": True, "id": row.id}
    finally:
        db.close()

@app.delete("/api/questions/{question_id}")
def api_delete_question(question_id: int, request: Request):
    if not is_admin_authenticated(request):
        return JSONResponse({"ok": False, "message": "Yetkisiz erişim"}, status_code=401)
    db = SessionLocal()
    try:
        row = db.query(Question).filter(Question.id == question_id).first()
        if not row:
            return JSONResponse({"ok": False, "message": "Soru bulunamadı"}, status_code=404)
        db.delete(row)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

@app.post("/api/rooms")
async def api_create_room(request: Request):
    body = await request.json()
    host_name = str(body.get("host_name", "")).strip()
    if not host_name:
        return JSONResponse({"ok": False, "message": "Host adı gerekli"}, status_code=400)
    room_code = generate_room_code()
    db_create_room(room_code, host_name)
    state = ensure_room_state(room_code)
    state["host_name"] = host_name
    return {"ok": True, "room_code": room_code, "host_name": host_name, "question_duration": QUESTION_DURATION}

@app.websocket("/ws/{room_code}")
async def websocket_room(websocket: WebSocket, room_code: str):
    room_db = db_get_room(room_code)
    if not room_db:
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "info", "message": "Oda bulunamadı."}))
        await websocket.close()
        return

    room = ensure_room_state(room_code)
    await websocket.accept()
    room["clients"].append(websocket)

    try:
        await websocket.send_text(json.dumps({"type": "room_info", "room_code": room_code, "quiz_started": room["quiz_started"], "question_open": room["question_open"], "question_duration": QUESTION_DURATION}))
        await websocket.send_text(json.dumps({"type": "leaderboard", "players": room["players"]}))

        if room["quiz_started"] and room["question_open"] and len(db_get_questions()) > 0:
            await websocket.send_text(json.dumps({"type": "question", "data": get_current_question(room_code), "duration": QUESTION_DURATION}))

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "join":
                name = data.get("name", "").strip()
                if not name:
                    await websocket.send_text(json.dumps({"type": "info", "message": "İsim gerekli."}))
                    continue
                if name not in room["players"]:
                    room["players"][name] = 0
                await websocket.send_text(json.dumps({"type": "join_success", "name": name, "room_code": room_code}))
                await room_broadcast_leaderboard(room_code)
                if room["quiz_started"] and room["question_open"] and len(db_get_questions()) > 0:
                    await websocket.send_text(json.dumps({"type": "question", "data": get_current_question(room_code), "duration": QUESTION_DURATION}))

            elif msg_type == "start_quiz":
                if len(db_get_questions()) == 0:
                    await websocket.send_text(json.dumps({"type": "info", "message": "Soru yok. Önce admin panelden soru ekleyin."}))
                    continue
                room["quiz_started"] = True
                room["question_open"] = True
                room["current_question_index"] = 0
                room_reset_answer_state(room_code)
                await room_broadcast_question(room_code)
                if room["auto_task"] and not room["auto_task"].done():
                    room["auto_task"].cancel()
                room["auto_task"] = asyncio.create_task(room_auto_close_question(room_code))

            elif msg_type == "next_question":
                total = len(db_get_questions())
                if total == 0:
                    continue
                if room["current_question_index"] < total - 1:
                    room["current_question_index"] += 1
                    room["question_open"] = True
                    room_reset_answer_state(room_code)
                    await room_broadcast_question(room_code)
                    if room["auto_task"] and not room["auto_task"].done():
                        room["auto_task"].cancel()
                    room["auto_task"] = asyncio.create_task(room_auto_close_question(room_code))
                else:
                    room["question_open"] = False
                    await room_broadcast(room_code, {"type": "quiz_finished"})
                    await room_broadcast_leaderboard(room_code)

            elif msg_type == "restart_quiz":
                room["current_question_index"] = 0
                room["quiz_started"] = False
                room["question_open"] = False
                room_reset_answer_state(room_code)
                for player_name in room["players"]:
                    room["players"][player_name] = 0
                await room_broadcast(room_code, {"type": "info", "message": "Quiz sıfırlandı."})
                await room_broadcast_leaderboard(room_code)

            elif msg_type == "show_answer":
                if room["question_open"] and len(db_get_questions()) > 0:
                    await room_close_question(room_code)

            elif msg_type == "answer":
                if not room["question_open"] or len(db_get_questions()) == 0:
                    await websocket.send_text(json.dumps({"type": "info", "message": "Bu soru kapandı."}))
                    continue
                player_name = data.get("name", "").strip()
                answer = data.get("answer", "").strip()
                if player_name not in room["players"]:
                    await websocket.send_text(json.dumps({"type": "info", "message": "Önce oyuna katıl."}))
                    continue
                if player_name in room["answered_players"]:
                    await websocket.send_text(json.dumps({"type": "info", "message": "Bu soruya zaten cevap verdin."}))
                    continue
                if answer not in ["A", "B", "C", "D"]:
                    continue
                room["answered_players"].add(player_name)
                room["answer_counts"][answer] += 1
                correct = get_correct_letter(room_code)
                is_correct = answer == correct
                if is_correct:
                    room["players"][player_name] += 10
                await websocket.send_text(json.dumps({"type": "answer_result", "correct": is_correct, "your_answer": answer, "correct_answer": correct, "score": room["players"][player_name]}))
                await room_broadcast_leaderboard(room_code)
                await room_broadcast(room_code, {"type": "host_answer_info", "player": player_name, "answer": answer, "correct": is_correct})

    except WebSocketDisconnect:
        if websocket in room["clients"]:
            room["clients"].remove(websocket)
