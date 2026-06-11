import asyncio
import json
import math
import queue
import re
import sys
import time
import sqlite3
import threading
from datetime import datetime
from typing import Dict, List, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, field_validator, model_validator

import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog
import tkinter.font as tkfont


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 1 — TOKENS DE DISEÑO
# Paleta estricta según las Guías de Diseño.  NUNCA usar nombres de color.
# ──────────────────────────────────────────────────────────────────────────────

APP_BACKGROUND  = "#F8F9FA"   # Blanco hueso: ventana raíz y fondos principales
CANVAS_BACKGROUND = "#FFFFFF" # Blanco puro: lienzo del editor y campos de entrada
PRIMARY_ACCENT  = "#3B82F6"   # Azul vibrante: botones de acción principal
HOVER_ACCENT    = "#2563EB"   # Azul oscuro: estado hover de botones principales
TEXT_PRIMARY    = "#1F2937"   # Gris pizarra oscuro: etiquetas, menús y texto escrito

# Tintes de apoyo derivados de la paleta
PROMO_BG     = "#EFF3FE"
INPUT_BORDER = "#E2E8F0"
SLATE_TEXT   = "#64748B"
PLACEHOLDER  = "#9CA3AF"
DIVIDER      = "#E2E8F0"
OUTLINE_HOVER = "#EFF4FE"

# Colores de estado para el modo de colaboración (extensión de la paleta)
STATE_LECTURA_BG   = "#F1F5F9"  # Fondo del badge "Modo lectura"
STATE_EDITANDO_BG  = "#FEF3C7"  # Fondo del badge "X está editando" (ámbar suave)
STATE_YO_BG        = "#D1FAE5"  # Fondo del badge "Tú estás editando" (verde suave)
STATE_LECTURA_FG   = "#475569"  # Texto modo lectura
STATE_EDITANDO_FG  = "#92400E"  # Texto modo otro editando
STATE_YO_FG        = "#065F46"  # Texto modo yo editando

# Fuente exclusiva del lienzo (Guías de Diseño, sección 3)
EDITOR_FONT = ("Open Sans", 12)

# URL base de la API — se reemplaza por la URL ngrok en modo cliente
API_BASE = "http://127.0.0.1:8000"

# Fuentes globales: se crean como objetos Font una vez que existe la raíz Tk
F_TITLE = F_H2 = F_FORM_TITLE = F_LABEL = F_INPUT = F_INPUT_ITALIC = None
F_BTN = F_BTN_ITALIC = F_SMALL = F_LINK = F_PROMO_TITLE = F_PROMO_DESC = None


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 2 — BACKEND: BASE DE DATOS
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="CoopDoc Collaborative Text Editor API")
DB_FILE = "editor_backend.db"


def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date_of_birth TEXT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')

    existing = [row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()]
    if "date_of_birth" not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN date_of_birth TEXT")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            owner_id INTEGER NOT NULL,
            FOREIGN KEY(owner_id) REFERENCES users(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(document_id, user_id)
        )
    ''')

    conn.commit()
    conn.close()


initialize_database()


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 3 — BACKEND: MODELOS PYDANTIC
# Las reglas de validación del PRD se aplican aquí; el frontend las replica.
# ──────────────────────────────────────────────────────────────────────────────

NAME_RE  = re.compile(r"^[^\W\d_]+(?: [^\W\d_]+)*$", re.UNICODE)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
DATE_RE  = re.compile(r"^\d{2}/\d{2}/\d{4}$")


class UserRegister(BaseModel):
    name: str
    date_of_birth: str
    email: str
    password: str
    confirm_password: str

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError("El nombre solo puede contener letras y espacios.")
        return v

    @field_validator('date_of_birth')
    @classmethod
    def validate_dob(cls, v):
        v = v.strip()
        if not DATE_RE.match(v):
            raise ValueError("La fecha debe tener el formato dd/mm/aaaa.")
        try:
            datetime.strptime(v, "%d/%m/%Y")
        except ValueError:
            raise ValueError("La fecha de nacimiento no es válida.")
        return v

    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        if v.count('@') != 1:
            raise ValueError("El correo debe contener exactamente un símbolo @.")
        if re.search(r'[\s\\]', v):
            raise ValueError("El correo no puede contener espacios ni barras invertidas.")
        if '..' in v:
            raise ValueError("El correo no puede contener puntos consecutivos.")
        if v.startswith('.') or v.endswith('.') or '@.' in v or '.@' in v:
            raise ValueError("El correo no puede tener puntos al inicio/final ni junto a la @.")
        if not EMAIL_RE.match(v):
            raise ValueError("El correo tiene un formato inválido.")
        return v

    @field_validator('password')
    @classmethod
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError("La contraseña debe tener al menos 8 caracteres.")
        if not re.search(r"[A-Z]", v):
            raise ValueError("La contraseña debe incluir al menos una mayúscula.")
        if not re.search(r"[a-z]", v):
            raise ValueError("La contraseña debe incluir al menos una minúscula.")
        if not re.search(r"\d", v):
            raise ValueError("La contraseña debe incluir al menos un número.")
        if not re.search(r"[^A-Za-z0-9]", v):
            raise ValueError("La contraseña debe incluir al menos un carácter especial.")
        return v

    @model_validator(mode='after')
    def check_passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Las contraseñas no coinciden.")
        return self


class UserLogin(BaseModel):
    email: str
    password: str


class DocumentCreate(BaseModel):
    title: str
    requester_id: int


class InviteUser(BaseModel):
    document_id: int
    requester_id: int
    invitee_email: str


class RevokeAccess(BaseModel):
    document_id: int
    requester_id: int
    target_user_id: int


class LockRequest(BaseModel):
    document_id: int
    requester_id: int


class SubmitChanges(BaseModel):
    document_id: int
    requester_id: int
    content: str


class ReleaseRequest(BaseModel):
    document_id: int
    requester_id: int


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 4 — BACKEND: ESTADO EN MEMORIA — BLOQUEOS Y COOLDOWNS
# Dicts simples protegidos por el GIL; uvicorn corre en un solo worker.
# ──────────────────────────────────────────────────────────────────────────────

# doc_id → user_id del editor activo (None = libre)
document_locks: Dict[int, Optional[int]] = {}
# doc_id → nombre del editor activo (para difundir en lock_update)
document_lock_names: Dict[int, Optional[str]] = {}
# (doc_id, user_id) → timestamp en que expira el cooldown
cooldowns: Dict[tuple, float] = {}

COOLDOWN_SECONDS = 3.0


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 5 — BACKEND: WEBSOCKET — ConnectionManager
# Canal de difusión unidireccional servidor → cliente.
# El cliente NUNCA envía texto por WS; toda mutación va por HTTP.
# ──────────────────────────────────────────────────────────────────────────────

class ConnectionManager:
    """Gestiona todas las conexiones WS agrupadas por documento."""

    def __init__(self):
        # doc_id → lista de (user_id, WebSocket)
        self.active_connections: Dict[int, List[tuple]] = {}

    async def connect(self, websocket: WebSocket, doc_id: int, user_id: int):
        await websocket.accept()
        self.active_connections.setdefault(doc_id, []).append((user_id, websocket))

    def disconnect(self, websocket: WebSocket, doc_id: int):
        if doc_id in self.active_connections:
            self.active_connections[doc_id] = [
                (uid, ws) for uid, ws in self.active_connections[doc_id]
                if ws is not websocket
            ]
            if not self.active_connections[doc_id]:
                del self.active_connections[doc_id]

    async def broadcast(self, doc_id: int, data: dict):
        """Difunde un evento JSON a todos los clientes del documento."""
        payload = json.dumps(data, ensure_ascii=False)
        dead = []
        for uid, ws in list(self.active_connections.get(doc_id, [])):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, doc_id)

    async def send_to_user(self, doc_id: int, user_id: int, data: dict):
        """Envía un evento JSON solo al socket del usuario indicado."""
        payload = json.dumps(data, ensure_ascii=False)
        for uid, ws in list(self.active_connections.get(doc_id, [])):
            if uid == user_id:
                try:
                    await ws.send_text(payload)
                except Exception:
                    pass


manager = ConnectionManager()


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 6 — BACKEND: ENDPOINTS HTTP
# ──────────────────────────────────────────────────────────────────────────────

def check_permission(doc_id: int, user_id: int) -> Optional[str]:
    """Devuelve el rol del usuario en el documento, o None si no tiene acceso."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role FROM permissions WHERE document_id=? AND user_id=?",
        (doc_id, user_id)
    )
    row = cursor.fetchone()
    conn.close()
    return row['role'] if row else None


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(user: UserRegister):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email=?", (user.email,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="El correo ya está registrado.")
    try:
        cursor.execute(
            "INSERT INTO users (name, date_of_birth, email, password) VALUES (?, ?, ?, ?)",
            (user.name, user.date_of_birth, user.email, user.password)
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail="Error en la base de datos.")
    conn.close()
    return {"message": "Usuario registrado correctamente."}


@app.post("/login")
def login_user(credentials: UserLogin):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name FROM users WHERE email=? AND password=?",
        (credentials.email, credentials.password)
    )
    user = cursor.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")
    return {"message": "Inicio de sesión exitoso", "user_id": user['id'], "name": user['name']}


@app.post("/documents")
def create_document(doc: DocumentCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO documents (title, content, owner_id) VALUES (?, '', ?)",
        (doc.title, doc.requester_id)
    )
    doc_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO permissions (document_id, user_id, role) VALUES (?, ?, 'owner')",
        (doc_id, doc.requester_id)
    )
    conn.commit()
    conn.close()
    return {"message": "Documento creado.", "document_id": doc_id}


@app.get("/documents")
def list_documents(user_id: int):
    """Lista todos los documentos accesibles por el usuario (propios + invitados)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT d.id, d.title, p.role
        FROM documents d
        JOIN permissions p ON d.id = p.document_id
        WHERE p.user_id = ?
        ORDER BY d.id DESC
        """,
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return {
        "documents": [
            {"id": r["id"], "title": r["title"], "role": r["role"]}
            for r in rows
        ]
    }


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, requester_id: int):
    role = check_permission(doc_id, requester_id)
    if not role:
        raise HTTPException(status_code=403, detail="Acceso denegado.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT title, content FROM documents WHERE id=?", (doc_id,))
    doc = cursor.fetchone()
    conn.close()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    return {"title": doc['title'], "content": doc['content'], "role": role}


@app.get("/documents/{doc_id}/collaborators")
def list_collaborators(doc_id: int, requester_id: int):
    """Lista todos los usuarios con acceso al documento."""
    if not check_permission(doc_id, requester_id):
        raise HTTPException(status_code=403, detail="Acceso denegado.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.id, u.name, u.email, p.role
        FROM permissions p
        JOIN users u ON p.user_id = u.id
        WHERE p.document_id = ?
        ORDER BY p.role DESC, u.name
        """,
        (doc_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return {
        "collaborators": [
            {"id": r["id"], "name": r["name"], "email": r["email"], "role": r["role"]}
            for r in rows
        ]
    }


@app.post("/invite")
def invite_user(invite: InviteUser):
    # Solo el propietario puede invitar (PRD: Access management)
    role = check_permission(invite.document_id, invite.requester_id)
    if role != 'owner':
        raise HTTPException(status_code=403,
                            detail="Solo el propietario puede invitar colaboradores.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email=?", (invite.invitee_email,))
    invitee = cursor.fetchone()
    if not invitee:
        conn.close()
        raise HTTPException(status_code=404, detail="El correo invitado no existe.")
    invitee_id = invitee['id']
    if invitee_id == invite.requester_id:
        conn.close()
        raise HTTPException(status_code=400, detail="No puedes invitarte a ti mismo.")
    try:
        cursor.execute(
            "INSERT INTO permissions (document_id, user_id, role) VALUES (?, ?, 'editor')",
            (invite.document_id, invitee_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # Ya tenía acceso; ignorar silenciosamente
    conn.close()
    return {"message": f"Usuario {invite.invitee_email} invitado correctamente."}


@app.delete("/revoke")
async def revoke_access(revoke: RevokeAccess):
    role = check_permission(revoke.document_id, revoke.requester_id)
    if role != 'owner':
        raise HTTPException(status_code=403,
                            detail="Solo el propietario puede revocar accesos.")
    if revoke.requester_id == revoke.target_user_id:
        raise HTTPException(status_code=400,
                            detail="El propietario no puede revocar su propio acceso.")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM permissions WHERE document_id=? AND user_id=?",
        (revoke.document_id, revoke.target_user_id)
    )
    conn.commit()
    conn.close()

    # Si el usuario revocado tenía el bloqueo, liberarlo
    if document_locks.get(revoke.document_id) == revoke.target_user_id:
        document_locks[revoke.document_id] = None
        document_lock_names[revoke.document_id] = None
        await manager.broadcast(revoke.document_id, {
            "type": "lock_update",
            "locked_by_id": None,
            "locked_by_name": None
        })

    # Notificar al usuario revocado (su cliente navegará fuera del editor)
    await manager.send_to_user(revoke.document_id, revoke.target_user_id, {
        "type": "access_revoked"
    })

    return {"message": "Acceso de usuario revocado."}


# ── Endpoints de colaboración: bloqueo pesimista ──────────────────────────────

@app.post("/request_lock")
async def request_lock(req: LockRequest):
    """Solicita el bloqueo de edición. Rechaza si ya lo tiene otro usuario."""
    role = check_permission(req.document_id, req.requester_id)
    if not role:
        raise HTTPException(status_code=403, detail="Acceso denegado.")

    # Verificar cooldown
    key = (req.document_id, req.requester_id)
    remaining = cooldowns.get(key, 0) - time.time()
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Espera {remaining:.1f}s antes de volver a editar."
        )

    current_holder = document_locks.get(req.document_id)

    if current_holder == req.requester_id:
        # El usuario ya tenía el bloqueo (idempotente)
        return {"message": "Ya tienes el bloqueo de edición."}

    if current_holder is not None:
        raise HTTPException(status_code=423,
                            detail="El documento ya está siendo editado por otro usuario.")

    # Conceder bloqueo
    document_locks[req.document_id] = req.requester_id

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM users WHERE id=?", (req.requester_id,))
    row = cursor.fetchone()
    conn.close()
    editor_name = row["name"] if row else "Usuario"
    document_lock_names[req.document_id] = editor_name

    await manager.broadcast(req.document_id, {
        "type": "lock_update",
        "locked_by_id": req.requester_id,
        "locked_by_name": editor_name
    })
    return {"message": "Bloqueo concedido.", "editor_name": editor_name}


@app.post("/submit_changes")
async def submit_changes(sub: SubmitChanges):
    """Guarda el contenido, libera el bloqueo e inicia el cooldown del editor."""
    if document_locks.get(sub.document_id) != sub.requester_id:
        raise HTTPException(status_code=423,
                            detail="No tienes el bloqueo de edición.")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE documents SET content=? WHERE id=?",
        (sub.content, sub.document_id)
    )
    conn.commit()
    conn.close()

    # Liberar bloqueo y aplicar cooldown
    document_locks[sub.document_id] = None
    document_lock_names[sub.document_id] = None
    cooldowns[(sub.document_id, sub.requester_id)] = time.time() + COOLDOWN_SECONDS

    # Difundir contenido actualizado y liberación del bloqueo
    await manager.broadcast(sub.document_id, {
        "type": "content_update",
        "content": sub.content
    })
    await manager.broadcast(sub.document_id, {
        "type": "lock_update",
        "locked_by_id": None,
        "locked_by_name": None
    })
    return {"message": "Cambios guardados correctamente."}


@app.post("/release_lock")
async def release_lock(req: ReleaseRequest):
    """Cancela la sesión de edición sin guardar cambios."""
    if document_locks.get(req.document_id) != req.requester_id:
        raise HTTPException(status_code=423,
                            detail="No tienes el bloqueo de edición.")

    document_locks[req.document_id] = None
    document_lock_names[req.document_id] = None

    await manager.broadcast(req.document_id, {
        "type": "lock_update",
        "locked_by_id": None,
        "locked_by_name": None
    })
    return {"message": "Bloqueo liberado."}


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 7 — BACKEND: ENDPOINT WEBSOCKET
# Canal servidor → cliente.  El cliente nunca envía texto útil por WS.
# Al conectar, el servidor envía el estado actual del bloqueo al nuevo cliente.
# ──────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/{doc_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, doc_id: int, user_id: int):
    if not check_permission(doc_id, user_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket, doc_id, user_id)

    # Sincronizar estado actual del bloqueo al cliente que acaba de conectarse
    holder_id   = document_locks.get(doc_id)
    holder_name = document_lock_names.get(doc_id)
    await websocket.send_text(json.dumps({
        "type": "lock_update",
        "locked_by_id": holder_id,
        "locked_by_name": holder_name
    }))

    try:
        while True:
            # Solo mantener la conexión viva; el cliente no envía datos útiles
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, doc_id)
        # Si este cliente tenía el bloqueo, liberarlo automáticamente
        if document_locks.get(doc_id) == user_id:
            document_locks[doc_id] = None
            document_lock_names[doc_id] = None
            await manager.broadcast(doc_id, {
                "type": "lock_update",
                "locked_by_id": None,
                "locked_by_name": None
            })


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 8 — BACKEND: ARRANQUE DEL SERVIDOR
# ──────────────────────────────────────────────────────────────────────────────

def start_backend():
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server


def wait_until_ready(timeout=15.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if requests.get(f"{API_BASE}/", timeout=1).status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.2)
    return False


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 9 — FRONTEND: VALIDACIÓN DEL LADO DEL CLIENTE
# Replica las reglas del PRD para dar retroalimentación inmediata.
# ──────────────────────────────────────────────────────────────────────────────

def validate_registration(name, dob, email, password, confirm):
    if not all([name, dob, email, password, confirm]):
        return "Completa todos los campos."
    if not NAME_RE.match(name):
        return "El nombre solo puede contener letras y espacios."
    if not DATE_RE.match(dob):
        return "La fecha debe tener el formato dd/mm/aaaa."
    try:
        datetime.strptime(dob, "%d/%m/%Y")
    except ValueError:
        return "La fecha de nacimiento no es válida."
    if email.count("@") != 1:
        return "El correo debe contener exactamente un símbolo @."
    if re.search(r"[\s\\]", email):
        return "El correo no puede contener espacios ni barras invertidas."
    if ".." in email:
        return "El correo no puede contener puntos consecutivos."
    if email.startswith(".") or email.endswith(".") or "@." in email or ".@" in email:
        return "El correo no puede tener puntos al inicio/final ni junto a la @."
    if not EMAIL_RE.match(email):
        return "El correo tiene un formato inválido."
    if len(password) < 8:
        return "La contraseña debe tener al menos 8 caracteres."
    if not re.search(r"[A-Z]", password):
        return "La contraseña debe incluir al menos una mayúscula."
    if not re.search(r"[a-z]", password):
        return "La contraseña debe incluir al menos una minúscula."
    if not re.search(r"\d", password):
        return "La contraseña debe incluir al menos un número."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "La contraseña debe incluir al menos un carácter especial."
    if password != confirm:
        return "Las contraseñas no coinciden."
    return None


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 10 — FRONTEND: FUENTES Y UTILIDADES DE WIDGETS
# ──────────────────────────────────────────────────────────────────────────────

def resolve_font_family(preferred, fallbacks):
    available = {f.lower() for f in tkfont.families()}
    for fam in [preferred] + fallbacks:
        if fam.lower() in available:
            return fam
    return preferred


def init_fonts():
    global F_TITLE, F_H2, F_FORM_TITLE, F_LABEL, F_INPUT, F_INPUT_ITALIC
    global F_BTN, F_BTN_ITALIC, F_SMALL, F_LINK, F_PROMO_TITLE, F_PROMO_DESC
    fam = resolve_font_family("Rubik", ["Segoe UI", "Helvetica Neue", "Arial"])
    F_TITLE      = tkfont.Font(family=fam, size=30, weight="bold")
    F_H2         = tkfont.Font(family=fam, size=20, weight="bold")
    F_FORM_TITLE = tkfont.Font(family=fam, size=26, weight="bold")
    F_LABEL      = tkfont.Font(family=fam, size=11, weight="bold")
    F_INPUT      = tkfont.Font(family=fam, size=12)
    F_INPUT_ITALIC = tkfont.Font(family=fam, size=12, slant="italic")
    F_BTN        = tkfont.Font(family=fam, size=13, weight="bold")
    F_BTN_ITALIC = tkfont.Font(family=fam, size=13, weight="bold", slant="italic")
    F_SMALL      = tkfont.Font(family=fam, size=10, weight="bold")
    F_LINK       = tkfont.Font(family=fam, size=11, weight="bold")
    F_PROMO_TITLE = tkfont.Font(family=fam, size=22, weight="bold")
    F_PROMO_DESC  = tkfont.Font(family=fam, size=12)


class PlaceholderEntry(tk.Entry):
    """Entry con marcador en cursiva gris y soporte de enmascarado de contraseña."""

    def __init__(self, master, placeholder, is_password=False, **kw):
        super().__init__(master, **kw)
        self.placeholder  = placeholder
        self.is_password  = is_password
        self.mask         = is_password
        self.showing_placeholder = False
        self.bind("<FocusIn>",  self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self._show_placeholder()

    def _show_placeholder(self):
        self.delete(0, tk.END)
        if self.is_password:
            self.config(show="")
        self.config(fg=PLACEHOLDER, font=F_INPUT_ITALIC)
        self.insert(0, self.placeholder)
        self.showing_placeholder = True

    def _on_focus_in(self, _=None):
        if self.showing_placeholder:
            self.delete(0, tk.END)
            self.config(fg=TEXT_PRIMARY, font=F_INPUT)
            if self.is_password and self.mask:
                self.config(show="*")
            self.showing_placeholder = False

    def _on_focus_out(self, _=None):
        if not self.get():
            self._show_placeholder()

    def value(self):
        return "" if self.showing_placeholder else self.get()

    def set_mask(self, mask: bool):
        self.mask = mask
        if not self.showing_placeholder:
            self.config(show="*" if mask else "")

    def reset(self):
        self.mask = self.is_password
        self._show_placeholder()


def make_field(parent, placeholder, is_password=False):
    """Devuelve (borde_frame, PlaceholderEntry, toggle_btn_o_None)."""
    border = tk.Frame(parent, bg=INPUT_BORDER)
    inner  = tk.Frame(border, bg=CANVAS_BACKGROUND)
    inner.pack(fill="both", expand=True, padx=1, pady=1)

    entry = PlaceholderEntry(
        inner, placeholder, is_password=is_password,
        relief="flat", bg=CANVAS_BACKGROUND, font=F_INPUT_ITALIC,
        fg=PLACEHOLDER, insertbackground=TEXT_PRIMARY, highlightthickness=0, bd=0,
    )

    toggle = None
    if is_password:
        def _toggle():
            if entry.showing_placeholder:
                return
            new_mask = not entry.mask
            entry.set_mask(new_mask)
            toggle.config(text="Mostrar" if new_mask else "Ocultar")

        toggle = tk.Button(
            inner, text="Mostrar", command=_toggle, relief="flat", bd=0,
            bg=CANVAS_BACKGROUND, fg=PRIMARY_ACCENT,
            activebackground=CANVAS_BACKGROUND, activeforeground=HOVER_ACCENT,
            font=F_SMALL, cursor="hand2",
        )
        toggle.pack(side="right", padx=(0, 12))

    entry.pack(side="left", fill="x", expand=True, padx=(14, 8), ipady=9)
    return border, entry, toggle


def style_primary(btn):
    btn.config(
        bg=PRIMARY_ACCENT, fg=CANVAS_BACKGROUND,
        activebackground=HOVER_ACCENT, activeforeground=CANVAS_BACKGROUND,
        relief="flat", bd=0, cursor="hand2", font=F_BTN,
    )
    btn.bind("<Enter>", lambda e: btn.config(bg=HOVER_ACCENT))
    btn.bind("<Leave>", lambda e: btn.config(bg=PRIMARY_ACCENT))
    return btn


def make_outline_button(parent, text, command):
    border = tk.Frame(parent, bg=PRIMARY_ACCENT)
    btn = tk.Button(
        border, text=text, command=command,
        bg=CANVAS_BACKGROUND, fg=PRIMARY_ACCENT,
        activebackground=OUTLINE_HOVER, activeforeground=HOVER_ACCENT,
        relief="flat", bd=0, cursor="hand2", font=F_BTN,
    )
    btn.pack(fill="both", expand=True, padx=2, pady=2, ipady=7)
    return border


def draw_star_logo(parent, size=84, bg=CANVAS_BACKGROUND):
    canvas = tk.Canvas(parent, width=size, height=size, bg=bg,
                       highlightthickness=0, bd=0)
    pad = 4
    canvas.create_oval(pad, pad, size - pad, size - pad,
                       fill=CANVAS_BACKGROUND, outline=PRIMARY_ACCENT, width=3)
    cx = cy = size / 2
    outer, inner = size * 0.30, size * 0.13
    pts = []
    for i in range(10):
        r = outer if i % 2 == 0 else inner
        a = -math.pi / 2 + i * math.pi / 5
        pts.extend([cx + r * math.cos(a), cy + r * math.sin(a)])
    canvas.create_polygon(pts, fill=PRIMARY_ACCENT, outline="")
    return canvas


def add_hover_underline(label):
    base = label.cget("font")
    over = tkfont.Font(font=base)
    over.config(underline=True)
    label.bind("<Enter>", lambda e: label.config(font=over, cursor="hand2"))
    label.bind("<Leave>", lambda e: label.config(font=base))


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 11 — FRONTEND: CONTROLADOR (App)
# ──────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CoopDoc — Editor de Texto Colaborativo")
        self.geometry("1024x768")
        self.resizable(False, False)
        self.configure(bg=APP_BACKGROUND)

        self.usuario_id     = None
        self.usuario_nombre = ""

        init_fonts()

        container = tk.Frame(self, bg=APP_BACKGROUND)
        container.pack(fill="both", expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.frames = {}
        for FrameClass in (LoginFrame, RegisterFrame, DashboardFrame, EditorFrame):
            frame = FrameClass(container, self)
            self.frames[FrameClass.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.show("LoginFrame")

    def show(self, name):
        frame = self.frames[name]
        frame.tkraise()
        if hasattr(frame, 'on_show'):
            frame.on_show()

    def on_login_success(self, user_id, user_name):
        self.usuario_id     = user_id
        self.usuario_nombre = user_name
        self.frames["DashboardFrame"].greet(user_name)
        self.show("DashboardFrame")

    def abrir_editor(self, documento_id, titulo, contenido="", rol="editor"):
        self.frames["EditorFrame"].cargar_documento(documento_id, titulo, contenido, rol)
        self.show("EditorFrame")

    def get_ws_base(self):
        """Convierte la URL HTTP base en su equivalente WS."""
        if API_BASE.startswith("https://"):
            return "wss://" + API_BASE[len("https://"):]
        return "ws://" + API_BASE[len("http://"):]


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 12 — FRONTEND: PANTALLA DE INICIO DE SESIÓN
# ──────────────────────────────────────────────────────────────────────────────

class LoginFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller

        card = tk.Frame(self, bg=APP_BACKGROUND)
        card.place(relx=0.5, rely=0.5, anchor="center", width=470)

        tk.Label(card, text="¡Bienvenido!", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_TITLE).pack(pady=(0, 8))
        draw_star_logo(card, size=84, bg=APP_BACKGROUND).pack(pady=4)
        tk.Label(card, text="Inicia sesión", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_H2).pack(pady=(4, 28))

        tk.Label(card, text="Ingresa tu correo", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 5))
        box, self.entry_email, _ = make_field(card, "ej. usuario@correo.com")
        box.pack(fill="x", pady=(0, 16))

        tk.Label(card, text="Ingresa tu contraseña", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 5))
        box, self.entry_password, _ = make_field(card, "ej. Contraseña1!", is_password=True)
        box.pack(fill="x", pady=(0, 10))

        forgot = tk.Label(card, text="¿Olvidaste tu contraseña?", bg=APP_BACKGROUND,
                          fg=PRIMARY_ACCENT, font=F_LINK)
        forgot.pack(pady=(0, 18))
        add_hover_underline(forgot)
        forgot.bind("<Button-1>", lambda e: messagebox.showinfo(
            "Recuperar contraseña",
            "La recuperación de contraseña aún no está disponible en este prototipo."))

        submit = tk.Button(card, text="Iniciar sesión", command=self.handle_login)
        style_primary(submit)
        submit.pack(fill="x", ipady=9, pady=(0, 28))

        panel_border = tk.Frame(card, bg=INPUT_BORDER)
        panel_border.pack(fill="x")
        panel = tk.Frame(panel_border, bg="#F1F5F9")
        panel.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(panel, text="¿No cuentas con cuenta propia?", bg="#F1F5F9",
                 fg=SLATE_TEXT, font=F_INPUT).pack(pady=(18, 12))
        make_outline_button(panel, "Regístrate aquí",
                            lambda: controller.show("RegisterFrame")).pack(pady=(0, 18))

    def handle_login(self):
        email    = self.entry_email.value().strip()
        password = self.entry_password.value()
        if not email or not password:
            messagebox.showerror("Error", "Completa todos los campos.")
            return
        try:
            resp = requests.post(f"{API_BASE}/login",
                                 json={"email": email, "password": password}, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar con el servidor. Intenta de nuevo.")
            return
        if resp.status_code == 200:
            data = resp.json()
            self.entry_email.reset()
            self.entry_password.reset()
            self.controller.on_login_success(data.get("user_id"), data.get("name", ""))
        elif resp.status_code == 401:
            messagebox.showerror("Error",
                "Correo o contraseña incorrectos. Revisa tu información e intenta de nuevo.")
        else:
            messagebox.showerror("Error", "Ocurrió un problema. Intenta de nuevo.")


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 13 — FRONTEND: PANTALLA DE REGISTRO
# ──────────────────────────────────────────────────────────────────────────────

class RegisterFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller
        self.grid_columnconfigure(0, weight=40, uniform="cols")
        self.grid_columnconfigure(1, weight=60, uniform="cols")
        self.grid_rowconfigure(0, weight=1)
        self._build_promo_panel()
        self._build_form_panel()

    def _build_promo_panel(self):
        promo = tk.Frame(self, bg=PROMO_BG)
        promo.grid(row=0, column=0, sticky="nsew", padx=(24, 12), pady=24)
        inner = tk.Frame(promo, bg=PROMO_BG)
        inner.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.86)
        draw_star_logo(inner, size=90, bg=PROMO_BG).pack(pady=(0, 14))
        tk.Label(inner, text="Únete junto a tus\namigos a CoopDoc", bg=PROMO_BG,
                 fg=TEXT_PRIMARY, font=F_PROMO_TITLE, justify="center").pack()
        tk.Frame(inner, bg=PRIMARY_ACCENT, height=3, width=48).pack(pady=16)
        tk.Label(inner,
                 text=("Usando CoopDoc, termina de volada tus trabajos grupales. "
                       "Para ello, crea una cuenta nueva con nosotros."),
                 bg=PROMO_BG, fg=SLATE_TEXT, font=F_PROMO_DESC,
                 justify="center", wraplength=300).pack(pady=(0, 30))
        footer = tk.Frame(inner, bg=PROMO_BG)
        footer.pack()
        tk.Label(footer, text="Ya tengo cuenta", bg=PROMO_BG, fg=SLATE_TEXT,
                 font=F_INPUT).pack(side="left", padx=(0, 6))
        link = tk.Label(footer, text="Regresar a inicio de sesión", bg=PROMO_BG,
                        fg=PRIMARY_ACCENT, font=F_LINK)
        link.pack(side="left")
        add_hover_underline(link)
        link.bind("<Button-1>", lambda e: self.controller.show("LoginFrame"))

    def _build_form_panel(self):
        panel = tk.Frame(self, bg=APP_BACKGROUND)
        panel.grid(row=0, column=1, sticky="nsew", padx=(12, 40), pady=24)
        form = tk.Frame(panel, bg=APP_BACKGROUND)
        form.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)
        tk.Label(form, text="¡Bienvenido!", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_FORM_TITLE).pack()
        tk.Frame(form, bg=PRIMARY_ACCENT, height=3, width=70).pack(pady=(8, 18))

        def field(label_text, placeholder, is_password=False):
            tk.Label(form, text=label_text, bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                     font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 4))
            box, entry, _ = make_field(form, placeholder, is_password=is_password)
            box.pack(fill="x", pady=(0, 10))
            return entry

        self.entry_name    = field("Ingresa tu nombre y apellidos:", "ej. César Ríos Oliváres")
        self.entry_dob     = field("Ingresa tu fecha de nacimiento (dd/mm/aaaa):", "ej. 17/09/2001")
        self.entry_dob.bind("<KeyRelease>", self._auto_format_date)
        self.entry_email   = field("Ingresa tu correo de verificación:", "ej. usuario@correo.com")
        self.entry_password = field("Ingresa tu contraseña:", "ej. Contraseña1!", is_password=True)
        self.entry_confirm  = field("Verifica tu contraseña:", "Repite tu contraseña", is_password=True)

        create = tk.Button(form, text="Crear cuenta", command=self.handle_register)
        style_primary(create)
        create.pack(fill="x", ipady=10, pady=(8, 14))

        divider = tk.Frame(form, bg=APP_BACKGROUND)
        divider.pack(fill="x", pady=(0, 14))
        tk.Frame(divider, bg=DIVIDER, height=1).pack(side="left", fill="x", expand=True, pady=8)
        tk.Label(divider, text="o", bg=APP_BACKGROUND, fg=PLACEHOLDER,
                 font=F_INPUT).pack(side="left", padx=12)
        tk.Frame(divider, bg=DIVIDER, height=1).pack(side="left", fill="x", expand=True, pady=8)

        make_outline_button(
            form, "Registrarse con Google",
            lambda: messagebox.showinfo(
                "Google",
                "El registro con Google aún no está implementado en este prototipo.")
        ).pack(fill="x")

    def _auto_format_date(self, event):
        if self.entry_dob.showing_placeholder:
            return
        if event.keysym in ("BackSpace", "Delete", "Left", "Right", "Tab"):
            return
        text = self.entry_dob.get()
        if len(text) == 2 and text.count("/") == 0:
            self.entry_dob.insert(tk.END, "/")
        elif len(text) == 5 and text.count("/") == 1:
            self.entry_dob.insert(tk.END, "/")

    def _reset_form(self):
        for e in (self.entry_name, self.entry_dob, self.entry_email,
                  self.entry_password, self.entry_confirm):
            e.reset()

    def handle_register(self):
        name     = self.entry_name.value().strip()
        dob      = self.entry_dob.value().strip()
        email    = self.entry_email.value().strip()
        password = self.entry_password.value()
        confirm  = self.entry_confirm.value()

        error = validate_registration(name, dob, email, password, confirm)
        if error:
            messagebox.showerror("Error", error)
            return

        payload = {
            "name": name, "date_of_birth": dob, "email": email,
            "password": password, "confirm_password": confirm,
        }
        try:
            resp = requests.post(f"{API_BASE}/register", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar con el servidor. Intenta de nuevo.")
            return
        if resp.status_code == 201:
            messagebox.showinfo("Registro",
                                "¡Cuenta creada correctamente! Ya puedes iniciar sesión.")
            self._reset_form()
            self.controller.show("LoginFrame")
        elif resp.status_code == 400:
            messagebox.showerror("Error", "Este correo ya está registrado.")
        else:
            messagebox.showerror("Error", "Revisa tu información e intenta de nuevo.")


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 14 — FRONTEND: TABLERO (DASHBOARD)
# Lista todos los documentos accesibles; permite crear uno nuevo o importar local.
# ──────────────────────────────────────────────────────────────────────────────

class DashboardFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller
        self._lista_canvas  = None
        self._lista_inner   = None
        self._canvas_window = None
        self._construir_ui()

    # ── Construcción de la UI ────────────────────────────────────────────────

    def _construir_ui(self):
        # ── Cabecera ──────────────────────────────────────────────────────────
        cabecera = tk.Frame(self, bg=APP_BACKGROUND)
        cabecera.pack(fill="x", padx=28, pady=(20, 0))

        logo_row = tk.Frame(cabecera, bg=APP_BACKGROUND)
        logo_row.pack(side="left")
        draw_star_logo(logo_row, size=38, bg=APP_BACKGROUND).pack(side="left", padx=(0, 10))
        tk.Label(logo_row, text="CoopDoc", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_H2).pack(side="left")

        self.lbl_saludo = tk.Label(cabecera, text="¡Hola!", bg=APP_BACKGROUND,
                                   fg=TEXT_PRIMARY, font=F_LABEL)
        self.lbl_saludo.pack(side="left", padx=20)

        logout = tk.Label(cabecera, text="Cerrar sesión", bg=APP_BACKGROUND,
                          fg=PRIMARY_ACCENT, font=F_LINK)
        logout.pack(side="right")
        add_hover_underline(logout)
        logout.bind("<Button-1>", lambda e: self.controller.show("LoginFrame"))

        # ── Separador ─────────────────────────────────────────────────────────
        tk.Frame(self, bg=DIVIDER, height=1).pack(fill="x", padx=28, pady=(14, 0))

        # ── Acciones rápidas ──────────────────────────────────────────────────
        acciones = tk.Frame(self, bg=APP_BACKGROUND)
        acciones.pack(fill="x", padx=28, pady=14)

        crear = tk.Button(acciones, text="+ Nuevo documento",
                          command=self._crear_archivo)
        style_primary(crear)
        crear.pack(side="left", ipady=7, ipadx=16)

        importar = make_outline_button(acciones, "Importar .txt local",
                                       self._abrir_archivo_local)
        importar.pack(side="left", padx=(12, 0))

        self.btn_actualizar = make_outline_button(acciones, "Actualizar",
                                                  self._cargar_documentos)
        self.btn_actualizar.pack(side="right")

        # ── Título de sección ─────────────────────────────────────────────────
        tk.Label(self, text="Mis documentos", bg=APP_BACKGROUND,
                 fg=TEXT_PRIMARY, font=F_LABEL).pack(anchor="w", padx=28)

        tk.Frame(self, bg=DIVIDER, height=1).pack(fill="x", padx=28, pady=(6, 0))

        # ── Lista scrollable ──────────────────────────────────────────────────
        lista_contenedor = tk.Frame(self, bg=APP_BACKGROUND)
        lista_contenedor.pack(fill="both", expand=True, padx=28, pady=(8, 20))

        scrollbar = tk.Scrollbar(lista_contenedor, bg=APP_BACKGROUND)
        scrollbar.pack(side="right", fill="y")

        self._lista_canvas = tk.Canvas(lista_contenedor, bg=APP_BACKGROUND,
                                       highlightthickness=0,
                                       yscrollcommand=scrollbar.set)
        self._lista_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self._lista_canvas.yview)

        self._lista_inner = tk.Frame(self._lista_canvas, bg=APP_BACKGROUND)
        self._canvas_window = self._lista_canvas.create_window(
            (0, 0), window=self._lista_inner, anchor="nw"
        )

        self._lista_inner.bind(
            "<Configure>",
            lambda e: self._lista_canvas.configure(
                scrollregion=self._lista_canvas.bbox("all")
            )
        )
        self._lista_canvas.bind(
            "<Configure>",
            lambda e: self._lista_canvas.itemconfig(
                self._canvas_window, width=e.width
            )
        )

        # Mensaje de estado (cargando / vacío / error)
        self.lbl_estado_lista = tk.Label(
            self._lista_inner, text="", bg=APP_BACKGROUND,
            fg=SLATE_TEXT, font=F_INPUT
        )
        self.lbl_estado_lista.pack(pady=20)

    # ── Carga de documentos ──────────────────────────────────────────────────

    def on_show(self):
        self._cargar_documentos()

    def greet(self, name):
        nice = name.split(" ")[0] if name else ""
        self.lbl_saludo.config(text=f"¡Hola, {nice}!" if nice else "¡Hola!")

    def _cargar_documentos(self):
        self.lbl_estado_lista.config(text="Cargando documentos…")
        for w in self._lista_inner.winfo_children():
            if w is not self.lbl_estado_lista:
                w.destroy()

        try:
            resp = requests.get(
                f"{API_BASE}/documents",
                params={"user_id": self.controller.usuario_id},
                timeout=5
            )
        except requests.exceptions.RequestException:
            self.lbl_estado_lista.config(
                text="No se pudo conectar con el servidor.")
            return

        if resp.status_code != 200:
            self.lbl_estado_lista.config(text="Error al cargar documentos.")
            return

        docs = resp.json().get("documents", [])
        self.lbl_estado_lista.config(text="")

        if not docs:
            self.lbl_estado_lista.config(
                text="Aún no tienes documentos. ¡Crea uno nuevo!")
            return

        for doc in docs:
            self._agregar_tarjeta(doc["id"], doc["title"], doc["role"])

    def _agregar_tarjeta(self, doc_id, title, role):
        borde = tk.Frame(self._lista_inner, bg=INPUT_BORDER)
        borde.pack(fill="x", pady=4, padx=2)

        card = tk.Frame(borde, bg=CANVAS_BACKGROUND)
        card.pack(fill="both", expand=True, padx=1, pady=1)

        info = tk.Frame(card, bg=CANVAS_BACKGROUND)
        info.pack(side="left", fill="x", expand=True, padx=14, pady=10)

        tk.Label(info, text=title, bg=CANVAS_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_LABEL).pack(anchor="w")

        role_text = "Propietario" if role == "owner" else "Colaborador"
        role_color = PRIMARY_ACCENT if role == "owner" else SLATE_TEXT
        tk.Label(info, text=role_text, bg=CANVAS_BACKGROUND,
                 fg=role_color, font=F_SMALL).pack(anchor="w", pady=(2, 0))

        abrir_btn = tk.Button(
            card, text="Abrir →",
            command=lambda d=doc_id, t=title: self._abrir_doc_servidor(d, t)
        )
        style_primary(abrir_btn)
        abrir_btn.pack(side="right", padx=14, pady=10, ipadx=12, ipady=5)

    # ── Acciones ─────────────────────────────────────────────────────────────

    def _abrir_doc_servidor(self, doc_id, title):
        try:
            resp = requests.get(
                f"{API_BASE}/documents/{doc_id}",
                params={"requester_id": self.controller.usuario_id},
                timeout=5
            )
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo cargar el documento.")
            return
        if resp.status_code == 200:
            data = resp.json()
            self.controller.abrir_editor(
                doc_id, data["title"], data["content"], data.get("role", "editor")
            )
        elif resp.status_code == 403:
            messagebox.showerror("Error", "No tienes acceso a este documento.")
        else:
            messagebox.showerror("Error", "No se pudo abrir el documento.")

    def _crear_archivo(self):
        titulo = simpledialog.askstring(
            "Nuevo documento", "Nombre del documento:", parent=self)
        if titulo is None:
            return
        titulo = titulo.strip()
        if not titulo:
            messagebox.showerror("Error", "El nombre del documento no puede estar vacío.")
            return
        payload = {"title": titulo, "requester_id": self.controller.usuario_id}
        try:
            resp = requests.post(f"{API_BASE}/documents", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar con el servidor.")
            return
        if resp.status_code == 200:
            doc_id = resp.json().get("document_id")
            self.controller.abrir_editor(doc_id, titulo, "", "owner")
        else:
            messagebox.showerror("Error", "No se pudo crear el documento.")

    def _abrir_archivo_local(self):
        ruta = filedialog.askopenfilename(
            filetypes=[("Archivos de texto", "*.txt"), ("Todos los archivos", "*.*")])
        if not ruta:
            return
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
        except OSError:
            messagebox.showerror("Error", "No se pudo abrir el archivo.")
            return
        nombre = ruta.replace("\\", "/").split("/")[-1]
        self.controller.abrir_editor(None, nombre, contenido, "owner")


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 15 — FRONTEND: EDITOR COLABORATIVO
# Implementa el ciclo completo de colaboración: lectura → solicitar edición →
# edición exclusiva → guardar cambios → difusión → vuelta a lectura.
# ──────────────────────────────────────────────────────────────────────────────

class EditorFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller

        # Estado del documento
        self.documento_id = None
        self.mi_rol       = "editor"
        self.modo         = "lectura"  # "lectura" | "edicion"

        # Cooldown local (cuenta regresiva visual)
        self._cooldown_restante = 0

        # WebSocket cliente
        self._ws_queue      = queue.Queue()
        self._ws_running    = False
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_connection = None  # objeto websockets.WebSocketClientProtocol

        # Fuentes del lienzo (Open Sans exclusivo del área de escritura)
        self.fuente_editor  = tkfont.Font(family=EDITOR_FONT[0], size=EDITOR_FONT[1])
        self.fuente_negrita = tkfont.Font(family=EDITOR_FONT[0], size=EDITOR_FONT[1],
                                          weight="bold")
        self.fuente_cursiva = tkfont.Font(family=EDITOR_FONT[0], size=EDITOR_FONT[1],
                                          slant="italic")

        self._construir_barra_superior()
        self._construir_barra_acciones()
        self._construir_lienzo()
        self._configurar_formatos()

    # ── Construcción de la UI ─────────────────────────────────────────────────

    def _construir_barra_superior(self):
        barra = tk.Frame(self, bg=APP_BACKGROUND)
        barra.pack(fill="x", padx=16, pady=(14, 6))

        # Título del documento
        self.etiqueta_titulo = tk.Label(
            barra, text="Documento", bg=APP_BACKGROUND, fg=TEXT_PRIMARY, font=F_H2)
        self.etiqueta_titulo.pack(side="left")

        # Badge de estado de colaboración
        self.badge_frame = tk.Frame(barra, bg=STATE_LECTURA_BG, padx=10, pady=4)
        self.badge_frame.pack(side="left", padx=(14, 0))
        self.badge_label = tk.Label(
            self.badge_frame, text="Modo lectura",
            bg=STATE_LECTURA_BG, fg=STATE_LECTURA_FG, font=F_SMALL)
        self.badge_label.pack()

        # Botones derechos
        derecha = tk.Frame(barra, bg=APP_BACKGROUND)
        derecha.pack(side="right")

        volver = make_outline_button(derecha, "Volver", self._on_volver)
        volver.pack(side="right", padx=(8, 0))

        self.btn_guardar_txt = make_outline_button(derecha, "Guardar .txt",
                                                   self._guardar_local)
        self.btn_guardar_txt.pack(side="right", padx=8)

        self.btn_abrir_txt = make_outline_button(derecha, "Abrir .txt",
                                                 self._abrir_local)
        self.btn_abrir_txt.pack(side="right", padx=8)

        self.btn_colaboradores = tk.Button(
            derecha, text="Colaboradores",
            command=self._abrir_colaboradores)
        style_primary(self.btn_colaboradores)
        self.btn_colaboradores.pack(side="right", padx=8, ipady=5, ipadx=12)

    def _construir_barra_acciones(self):
        barra = tk.Frame(self, bg=APP_BACKGROUND)
        barra.pack(fill="x", padx=16, pady=(0, 8))

        # Formato (izquierda)
        formato = tk.Frame(barra, bg=APP_BACKGROUND)
        formato.pack(side="left")
        self._boton_formato(formato, "Negrita",  F_BTN,        self._alternar_negrita).pack(
            side="left", padx=(0, 8))
        self._boton_formato(formato, "Cursiva",  F_BTN_ITALIC, self._alternar_cursiva).pack(
            side="left")

        # Acciones de colaboración (derecha)
        colaboracion = tk.Frame(barra, bg=APP_BACKGROUND)
        colaboracion.pack(side="right")

        # "Cancelar edición" — visible en modo edición
        self.btn_cancelar = make_outline_button(colaboracion, "Cancelar edición",
                                                self._cancelar_edicion)
        self.btn_cancelar.pack(side="right", padx=(8, 0))

        # "Guardar cambios" — visible en modo edición
        self.btn_guardar_cambios = tk.Button(
            colaboracion, text="Guardar cambios",
            command=self._guardar_cambios)
        style_primary(self.btn_guardar_cambios)
        self.btn_guardar_cambios.pack(side="right", padx=8, ipady=5, ipadx=14)

        # "Solicitar edición" — visible en modo lectura
        self.btn_solicitar = tk.Button(
            colaboracion, text="Solicitar edición",
            command=self._solicitar_edicion)
        style_primary(self.btn_solicitar)
        self.btn_solicitar.pack(side="right", ipady=5, ipadx=14)

    def _boton_formato(self, parent, texto, fuente, comando):
        return tk.Button(
            parent, text=texto, font=fuente, command=comando,
            bg=CANVAS_BACKGROUND, fg=TEXT_PRIMARY,
            activebackground=OUTLINE_HOVER, activeforeground=HOVER_ACCENT,
            relief="flat", bd=1, cursor="hand2", padx=14, pady=4
        )

    def _construir_lienzo(self):
        contenedor = tk.Frame(self, bg=INPUT_BORDER)
        contenedor.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.area_texto = tk.Text(
            contenedor, wrap="word", undo=True, font=self.fuente_editor,
            bg=CANVAS_BACKGROUND, fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY,
            relief="flat", bd=0, padx=12, pady=12,
            state="disabled",  # comienza en modo lectura
        )
        self.area_texto.pack(fill="both", expand=True, padx=1, pady=1)

    def _configurar_formatos(self):
        self.area_texto.tag_configure("negrita", font=self.fuente_negrita)
        self.area_texto.tag_configure("cursiva", font=self.fuente_cursiva)

    # ── Carga del documento ───────────────────────────────────────────────────

    def cargar_documento(self, documento_id, titulo, contenido="", rol="editor"):
        """Punto de entrada al abrir un documento desde el Dashboard."""
        # Limpiar sesión anterior
        if self.documento_id is not None:
            self._limpiar_sesion_ws(liberar_bloqueo=False)

        self.documento_id      = documento_id
        self.mi_rol            = rol
        self._cooldown_restante = 0

        self.etiqueta_titulo.config(text=titulo or "Documento")

        # Deshabilitar colaboración si el documento es solo local (sin id)
        if documento_id is None:
            self.btn_solicitar.config(state="disabled", text="Solicitar edición")
            self.btn_colaboradores.config(state="disabled")
        else:
            self.btn_colaboradores.config(state="normal")

        self._cargar_contenido_en_lienzo(contenido)
        self._entrar_modo_lectura(actualizar_badge=True)

        if documento_id is not None:
            self._conectar_ws()

    # ── Gestión de modos ──────────────────────────────────────────────────────

    def _entrar_modo_lectura(self, actualizar_badge=True, bloqueado_por=None):
        self.modo = "lectura"
        self.area_texto.config(state="disabled")

        # Visibilidad de botones de acción
        self.btn_guardar_cambios.config(state="disabled")
        # Obtener el botón interior del frame de outline de cancelar
        for child in self.btn_cancelar.winfo_children():
            child.config(state="disabled")

        can_request = (self.documento_id is not None
                       and self._cooldown_restante <= 0
                       and bloqueado_por is None)
        self.btn_solicitar.config(
            state="normal" if can_request else "disabled",
            text="Solicitar edición"
        )

        if actualizar_badge:
            if bloqueado_por:
                self._set_badge(f"{bloqueado_por} está editando",
                                STATE_EDITANDO_BG, STATE_EDITANDO_FG)
            else:
                self._set_badge("Modo lectura", STATE_LECTURA_BG, STATE_LECTURA_FG)

    def _entrar_modo_edicion(self):
        self.modo = "edicion"
        self.area_texto.config(state="normal")
        self.area_texto.focus_set()

        self.btn_solicitar.config(state="disabled")
        self.btn_guardar_cambios.config(state="normal")
        for child in self.btn_cancelar.winfo_children():
            child.config(state="normal")

        self._set_badge("Tú estás editando", STATE_YO_BG, STATE_YO_FG)

    def _set_badge(self, text, bg_color, fg_color):
        self.badge_frame.config(bg=bg_color)
        self.badge_label.config(text=text, bg=bg_color, fg=fg_color)

    # ── Cooldown visual ───────────────────────────────────────────────────────

    def _iniciar_cooldown(self):
        self._cooldown_restante = int(COOLDOWN_SECONDS)
        self._tick_cooldown()

    def _tick_cooldown(self):
        if self._cooldown_restante > 0:
            self.btn_solicitar.config(
                state="disabled",
                text=f"Solicitar edición ({self._cooldown_restante}s)"
            )
            self._cooldown_restante -= 1
            self.after(1000, self._tick_cooldown)
        else:
            # Cooldown expiró: re-habilitar si el documento está libre
            self.btn_solicitar.config(text="Solicitar edición")
            if self.modo == "lectura":
                current_badge = self.badge_label.cget("text")
                doc_libre = current_badge == "Modo lectura"
                self.btn_solicitar.config(
                    state="normal" if doc_libre else "disabled"
                )

    # ── Acciones de colaboración ──────────────────────────────────────────────

    def _solicitar_edicion(self):
        if self.documento_id is None:
            return
        payload = {
            "document_id": self.documento_id,
            "requester_id": self.controller.usuario_id
        }
        try:
            resp = requests.post(f"{API_BASE}/request_lock", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar con el servidor. Intenta de nuevo.")
            return

        if resp.status_code == 200:
            self._entrar_modo_edicion()
        elif resp.status_code == 423:
            messagebox.showinfo("Documento bloqueado",
                                "El documento ya está siendo editado por otro usuario.")
        elif resp.status_code == 429:
            detail = resp.json().get("detail", "Espera antes de volver a editar.")
            messagebox.showinfo("Cooldown activo", detail)
        else:
            messagebox.showerror("Error", "No se pudo obtener el bloqueo de edición.")

    def _guardar_cambios(self):
        if self.documento_id is None:
            return
        contenido = self.area_texto.get("1.0", "end-1c")
        payload = {
            "document_id": self.documento_id,
            "requester_id": self.controller.usuario_id,
            "content": contenido,
        }
        try:
            resp = requests.post(f"{API_BASE}/submit_changes", json=payload, timeout=10)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo guardar. Intenta de nuevo.")
            return

        if resp.status_code == 200:
            self._entrar_modo_lectura(actualizar_badge=True)
            self._iniciar_cooldown()
        elif resp.status_code == 423:
            messagebox.showerror("Error", "Perdiste el bloqueo de edición.")
            self._entrar_modo_lectura(actualizar_badge=True)
        else:
            messagebox.showerror("Error", "No se pudieron guardar los cambios.")

    def _cancelar_edicion(self):
        if self.documento_id is None:
            return
        if not messagebox.askyesno("Cancelar edición",
                                   "¿Descartar tus cambios y volver a modo lectura?"):
            return

        # Restaurar el contenido del servidor
        try:
            resp = requests.get(
                f"{API_BASE}/documents/{self.documento_id}",
                params={"requester_id": self.controller.usuario_id},
                timeout=5
            )
            if resp.status_code == 200:
                contenido_servidor = resp.json().get("content", "")
                self._cargar_contenido_en_lienzo(contenido_servidor)
        except requests.exceptions.RequestException:
            pass

        payload = {
            "document_id": self.documento_id,
            "requester_id": self.controller.usuario_id
        }
        try:
            requests.post(f"{API_BASE}/release_lock", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            pass

        self._entrar_modo_lectura(actualizar_badge=True)

    def _abrir_colaboradores(self):
        if self.documento_id is None:
            return
        CollaboratorsWindow(self, self.controller, self.documento_id, self.mi_rol)

    def _on_volver(self):
        if self.modo == "edicion":
            respuesta = messagebox.askyesnocancel(
                "Edición activa",
                "Tienes cambios sin guardar.\n\n"
                "¿Guardar cambios antes de salir?"
            )
            if respuesta is None:
                return  # Cancelar
            if respuesta:
                self._guardar_cambios()
                # Si el guardado falla, _guardar_cambios ya mostró el error;
                # no navegamos fuera en ese caso.
                if self.modo == "edicion":
                    return
            else:
                # Descartar: liberar bloqueo
                payload = {
                    "document_id": self.documento_id,
                    "requester_id": self.controller.usuario_id
                }
                try:
                    requests.post(f"{API_BASE}/release_lock", json=payload, timeout=3)
                except requests.exceptions.RequestException:
                    pass

        self._limpiar_sesion_ws(liberar_bloqueo=False)
        self.controller.show("DashboardFrame")

    # ── WebSocket cliente ─────────────────────────────────────────────────────

    def _conectar_ws(self):
        """Inicia el listener WS en un hilo demonio con su propio event loop."""
        try:
            import websockets as _ws_lib
        except ImportError:
            # Biblioteca no disponible: operación sin sincronización en tiempo real
            return

        self._ws_running = True
        url = (f"{self.controller.get_ws_base()}"
               f"/ws/{self.documento_id}/{self.controller.usuario_id}")

        async def _listener():
            try:
                async with _ws_lib.connect(url, ping_interval=20) as ws:
                    self._ws_connection = ws
                    async for message in ws:
                        if not self._ws_running:
                            break
                        self._ws_queue.put(message)
            except Exception:
                pass
            finally:
                self._ws_connection = None

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._ws_loop = loop
            try:
                loop.run_until_complete(_listener())
            finally:
                loop.close()
                self._ws_loop = None

        self._ws_thread = threading.Thread(target=_run, daemon=True)
        self._ws_thread.start()
        self._poll_ws_queue()

    def _poll_ws_queue(self):
        """Sondea la cola del hilo WS en el hilo principal de tkinter."""
        if not self._ws_running:
            return
        while True:
            try:
                msg = self._ws_queue.get_nowait()
                self._on_ws_mensaje(msg)
            except queue.Empty:
                break
        self.after(150, self._poll_ws_queue)

    def _on_ws_mensaje(self, msg_str):
        """Procesa un evento JSON recibido desde el servidor."""
        try:
            data = json.loads(msg_str)
        except (json.JSONDecodeError, TypeError):
            return

        tipo = data.get("type")

        if tipo == "content_update":
            # Solo actualizar el lienzo si no somos el editor activo
            if self.modo == "lectura":
                self._cargar_contenido_en_lienzo(data.get("content", ""))

        elif tipo == "lock_update":
            locked_by_id   = data.get("locked_by_id")
            locked_by_name = data.get("locked_by_name")

            if locked_by_id is None:
                # Bloqueo liberado
                if self.modo == "lectura":
                    self._set_badge("Modo lectura", STATE_LECTURA_BG, STATE_LECTURA_FG)
                    if self._cooldown_restante <= 0 and self.documento_id is not None:
                        self.btn_solicitar.config(state="normal")

            elif locked_by_id == self.controller.usuario_id:
                # Confirmación de que tenemos el bloqueo (broadcast propio)
                if self.modo != "edicion":
                    self._entrar_modo_edicion()

            else:
                # Otro usuario tomó el bloqueo
                if self.modo == "lectura":
                    self._set_badge(
                        f"{locked_by_name} está editando",
                        STATE_EDITANDO_BG, STATE_EDITANDO_FG
                    )
                    self.btn_solicitar.config(state="disabled")

        elif tipo == "access_revoked":
            self._limpiar_sesion_ws(liberar_bloqueo=False)
            self.controller.show("DashboardFrame")
            messagebox.showwarning(
                "Acceso revocado",
                "El propietario ha revocado tu acceso a este documento."
            )

    def _limpiar_sesion_ws(self, liberar_bloqueo: bool):
        """Detiene el WS y opcionalmente libera el bloqueo."""
        self._ws_running = False

        if liberar_bloqueo and self.modo == "edicion" and self.documento_id is not None:
            try:
                requests.post(
                    f"{API_BASE}/release_lock",
                    json={
                        "document_id": self.documento_id,
                        "requester_id": self.controller.usuario_id
                    },
                    timeout=3
                )
            except requests.exceptions.RequestException:
                pass

        # Cerrar la conexión WS desde el hilo de asyncio
        if self._ws_loop is not None and self._ws_connection is not None:
            try:
                self._ws_loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        self._ws_connection.close(),
                        loop=self._ws_loop
                    )
                )
            except RuntimeError:
                pass

        # Limpiar el documento_id para evitar doble limpieza
        self.documento_id = None
        self.modo = "lectura"

    # ── Lienzo: contenido ─────────────────────────────────────────────────────

    def _cargar_contenido_en_lienzo(self, contenido):
        estado_previo = self.area_texto.cget("state")
        self.area_texto.config(state="normal")
        self.area_texto.tag_remove("negrita", "1.0", "end")
        self.area_texto.tag_remove("cursiva", "1.0", "end")
        self.area_texto.delete("1.0", "end")
        if contenido:
            self.area_texto.insert("1.0", contenido)
        self.area_texto.edit_reset()
        self.area_texto.config(state=estado_previo)

    # ── Formato ───────────────────────────────────────────────────────────────

    def _alternar_etiqueta(self, nombre):
        if self.modo != "edicion":
            return
        try:
            inicio = self.area_texto.index("sel.first")
            fin    = self.area_texto.index("sel.last")
        except tk.TclError:
            return
        if nombre in self.area_texto.tag_names(inicio):
            self.area_texto.tag_remove(nombre, inicio, fin)
        else:
            self.area_texto.tag_add(nombre, inicio, fin)

    def _alternar_negrita(self):
        self._alternar_etiqueta("negrita")

    def _alternar_cursiva(self):
        self._alternar_etiqueta("cursiva")

    # ── IO local ──────────────────────────────────────────────────────────────

    def _guardar_local(self):
        ruta = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Archivos de texto", "*.txt"), ("Todos los archivos", "*.*")])
        if not ruta:
            return
        try:
            estado_previo = self.area_texto.cget("state")
            self.area_texto.config(state="normal")
            contenido = self.area_texto.get("1.0", "end-1c")
            self.area_texto.config(state=estado_previo)
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(contenido)
        except OSError:
            messagebox.showerror("Error", "No se pudo guardar el archivo.")
            return
        messagebox.showinfo("Guardar", "El documento se guardó correctamente.")

    def _abrir_local(self):
        if self.modo == "edicion":
            if not messagebox.askyesno("Importar archivo",
                                       "Se reemplazará el contenido actual. ¿Continuar?"):
                return
        ruta = filedialog.askopenfilename(
            filetypes=[("Archivos de texto", "*.txt"), ("Todos los archivos", "*.*")])
        if not ruta:
            return
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
        except OSError:
            messagebox.showerror("Error", "No se pudo abrir el archivo.")
            return
        if self.modo == "edicion":
            self._cargar_contenido_en_lienzo(contenido)
        else:
            messagebox.showinfo(
                "Importar",
                "Solicita la edición primero para poder importar un archivo al documento.")


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 16 — FRONTEND: VENTANA DE COLABORADORES
# Permite ver la lista de colaboradores, invitar nuevos y revocar accesos.
# ──────────────────────────────────────────────────────────────────────────────

class CollaboratorsWindow(tk.Toplevel):
    def __init__(self, parent_frame, controller, documento_id, mi_rol):
        super().__init__(parent_frame)
        self.controller   = controller
        self.documento_id = documento_id
        self.mi_rol       = mi_rol

        self.title("Colaboradores — CoopDoc")
        self.configure(bg=APP_BACKGROUND)
        self.resizable(False, False)
        self.geometry("480x520")

        self.transient(parent_frame)
        self.grab_set()

        self._build()
        self._cargar_colaboradores()

    def _build(self):
        # Cabecera
        cabecera = tk.Frame(self, bg=APP_BACKGROUND, padx=24, pady=18)
        cabecera.pack(fill="x")

        tk.Label(cabecera, text="Colaboradores", bg=APP_BACKGROUND,
                 fg=TEXT_PRIMARY, font=F_H2).pack(side="left")

        tk.Button(cabecera, text="Cerrar", command=self.destroy,
                  bg=APP_BACKGROUND, fg=SLATE_TEXT, font=F_SMALL,
                  relief="flat", bd=0, cursor="hand2").pack(side="right")

        tk.Frame(self, bg=DIVIDER, height=1).pack(fill="x", padx=24)

        # Área scrollable de colaboradores
        scroll_cont = tk.Frame(self, bg=APP_BACKGROUND)
        scroll_cont.pack(fill="both", expand=True, padx=24, pady=(12, 0))

        sb = tk.Scrollbar(scroll_cont)
        sb.pack(side="right", fill="y")

        self._canvas = tk.Canvas(scroll_cont, bg=APP_BACKGROUND,
                                 highlightthickness=0, yscrollcommand=sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb.config(command=self._canvas.yview)

        self._lista = tk.Frame(self._canvas, bg=APP_BACKGROUND)
        self._cw = self._canvas.create_window((0, 0), window=self._lista, anchor="nw")

        self._lista.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfig(self._cw, width=e.width))

        # Sección de invitación (solo propietario)
        if self.mi_rol == 'owner':
            tk.Frame(self, bg=DIVIDER, height=1).pack(fill="x", padx=24, pady=(8, 0))

            inv_frame = tk.Frame(self, bg=APP_BACKGROUND, padx=24, pady=14)
            inv_frame.pack(fill="x")

            tk.Label(inv_frame, text="Invitar por correo electrónico", bg=APP_BACKGROUND,
                     fg=TEXT_PRIMARY, font=F_LABEL).pack(anchor="w", pady=(0, 8))

            fila = tk.Frame(inv_frame, bg=APP_BACKGROUND)
            fila.pack(fill="x")

            borde, self.entry_invitar, _ = make_field(
                fila, "correo@ejemplo.com")
            borde.pack(side="left", fill="x", expand=True)

            inv_btn = tk.Button(fila, text="Invitar", command=self._invitar)
            style_primary(inv_btn)
            inv_btn.pack(side="left", padx=(10, 0), ipady=5, ipadx=16)

    def _cargar_colaboradores(self):
        for w in self._lista.winfo_children():
            w.destroy()

        try:
            resp = requests.get(
                f"{API_BASE}/documents/{self.documento_id}/collaborators",
                params={"requester_id": self.controller.usuario_id},
                timeout=5
            )
        except requests.exceptions.RequestException:
            tk.Label(self._lista, text="Error al cargar colaboradores.",
                     bg=APP_BACKGROUND, fg=SLATE_TEXT, font=F_INPUT).pack(pady=12)
            return

        if resp.status_code != 200:
            tk.Label(self._lista, text="No se pudo obtener la lista.",
                     bg=APP_BACKGROUND, fg=SLATE_TEXT, font=F_INPUT).pack(pady=12)
            return

        colaboradores = resp.json().get("collaborators", [])

        if not colaboradores:
            tk.Label(self._lista,
                     text="Aún no hay colaboradores en este documento.",
                     bg=APP_BACKGROUND, fg=SLATE_TEXT, font=F_INPUT).pack(pady=12)
            return

        for colab in colaboradores:
            self._agregar_fila_colab(colab)

    def _agregar_fila_colab(self, colab):
        borde = tk.Frame(self._lista, bg=INPUT_BORDER)
        borde.pack(fill="x", pady=3)

        fila = tk.Frame(borde, bg=CANVAS_BACKGROUND)
        fila.pack(fill="both", expand=True, padx=1, pady=1)

        info = tk.Frame(fila, bg=CANVAS_BACKGROUND, padx=12, pady=8)
        info.pack(side="left", fill="x", expand=True)

        tk.Label(info, text=colab["name"], bg=CANVAS_BACKGROUND,
                 fg=TEXT_PRIMARY, font=F_LABEL).pack(anchor="w")

        role_text = "Propietario" if colab["role"] == "owner" else "Colaborador"
        subtexto  = f"{colab['email']}  ·  {role_text}"
        tk.Label(info, text=subtexto, bg=CANVAS_BACKGROUND,
                 fg=SLATE_TEXT, font=F_SMALL).pack(anchor="w", pady=(2, 0))

        # Botón "Revocar" — solo el propietario, para colaboradores que no son owners
        if (self.mi_rol == 'owner'
                and colab["id"] != self.controller.usuario_id
                and colab["role"] != 'owner'):
            rev = tk.Button(
                fila, text="Revocar",
                command=lambda uid=colab["id"], nom=colab["name"]: self._revocar(uid, nom),
                bg=CANVAS_BACKGROUND, fg=HOVER_ACCENT,
                activebackground=OUTLINE_HOVER, activeforeground=HOVER_ACCENT,
                relief="flat", font=F_SMALL, cursor="hand2", bd=1,
                padx=10, pady=6
            )
            rev.pack(side="right", padx=10, pady=8)

    def _invitar(self):
        correo = self.entry_invitar.value().strip()
        if not correo:
            messagebox.showerror("Error", "Ingresa un correo.", parent=self)
            return

        payload = {
            "document_id": self.documento_id,
            "requester_id": self.controller.usuario_id,
            "invitee_email": correo,
        }
        try:
            resp = requests.post(f"{API_BASE}/invite", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar.", parent=self)
            return

        if resp.status_code == 200:
            messagebox.showinfo("Invitar",
                                f"Se invitó a {correo} correctamente.", parent=self)
            self.entry_invitar.reset()
            self._cargar_colaboradores()
        elif resp.status_code == 404:
            messagebox.showerror("Error",
                                 "El correo no pertenece a ningún usuario registrado.",
                                 parent=self)
        elif resp.status_code == 403:
            messagebox.showerror("Error",
                                 "Solo el propietario puede invitar colaboradores.",
                                 parent=self)
        elif resp.status_code == 400:
            messagebox.showerror("Error",
                                 "Este usuario ya tiene acceso o es el propietario.",
                                 parent=self)
        else:
            messagebox.showerror("Error", "No se pudo invitar. Intenta de nuevo.", parent=self)

    def _revocar(self, target_user_id, nombre):
        if not messagebox.askyesno(
                "Revocar acceso",
                f"¿Estás seguro de que quieres revocar el acceso de {nombre}?",
                parent=self):
            return

        payload = {
            "document_id": self.documento_id,
            "requester_id": self.controller.usuario_id,
            "target_user_id": target_user_id,
        }
        try:
            resp = requests.delete(f"{API_BASE}/revoke", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar.", parent=self)
            return

        if resp.status_code == 200:
            messagebox.showinfo("Revocar",
                                f"Se revocó el acceso de {nombre}.", parent=self)
            self._cargar_colaboradores()
        elif resp.status_code == 403:
            messagebox.showerror("Error",
                                 "No tienes permiso para realizar esta acción.", parent=self)
        else:
            messagebox.showerror("Error", "No se pudo revocar. Intenta de nuevo.", parent=self)


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 17 — FRONTEND: DIÁLOGO DE INICIO (modo anfitrión / cliente)
# ──────────────────────────────────────────────────────────────────────────────

class StartupDialog(tk.Tk):
    """Permite al usuario elegir entre iniciar como anfitrión o como cliente."""

    def __init__(self):
        super().__init__()
        self.result_mode = None   # "host" | "client"
        self.result_url  = None   # URL del anfitrión (solo en modo cliente)

        self.title("CoopDoc")
        self.resizable(False, False)
        self.configure(bg=APP_BACKGROUND)

        init_fonts()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Centrar ventana
        self.update_idletasks()
        w, h = 1024, 768
        x = (self.winfo_screenwidth()  - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        frame = tk.Frame(self, bg=APP_BACKGROUND, padx=40, pady=30)
        frame.pack(fill="both", expand=True)

        draw_star_logo(frame, size=64, bg=APP_BACKGROUND).pack(pady=(0, 10))

        tk.Label(frame, text="CoopDoc", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_TITLE).pack()
        tk.Label(frame, text="Editor de Texto Colaborativo", bg=APP_BACKGROUND,
                 fg=SLATE_TEXT, font=F_PROMO_DESC).pack(pady=(4, 6))

        tk.Frame(frame, bg=PRIMARY_ACCENT, height=3, width=50).pack(pady=(0, 20))

        tk.Label(frame, text="¿Cómo deseas conectarte?", bg=APP_BACKGROUND,
                 fg=TEXT_PRIMARY, font=F_LABEL).pack(pady=(0, 14))

        host_btn = tk.Button(frame, text="Iniciar como anfitrión",
                             command=self._seleccionar_anfitrion)
        style_primary(host_btn)
        host_btn.pack(fill="x", ipady=10, pady=(0, 10))

        make_outline_button(frame, "Conectarme a un anfitrión",
                            self._seleccionar_cliente).pack(fill="x")

    def _seleccionar_anfitrion(self):
        self.result_mode = "host"
        self.destroy()

    def _seleccionar_cliente(self):
        url = simpledialog.askstring(
            "Conectar con anfitrión",
            "Ingresa la URL del anfitrión\n(ej. https://xxxx.ngrok-free.app):",
            parent=self
        )
        if url and url.strip():
            self.result_url  = url.strip().rstrip("/")
            self.result_mode = "client"
            self.destroy()

    def _on_close(self):
        self.result_mode = None
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# SECCIÓN 18 — PUNTO DE ENTRADA
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. Selección de modo
    dialogo = StartupDialog()
    dialogo.mainloop()

    if dialogo.result_mode is None:
        sys.exit(0)

    # 2. Arranque del servidor (solo modo anfitrión)
    if dialogo.result_mode == "host":
        start_backend()
        if not wait_until_ready(timeout=15):
            print("Advertencia: el servidor no respondió a tiempo; "
                  "las acciones de red podrían fallar.")
    else:
        # Modo cliente: apuntar a la URL del anfitrión
        API_BASE = dialogo.result_url  # type: ignore[assignment]

    # 3. Lanzar interfaz principal
    App().mainloop()