# ==============================================================================
#  CoopDoc — Collaborative Desktop Text Editor
#  Unified prototype: FastAPI backend + tkinter frontend in a SINGLE file.
# ------------------------------------------------------------------------------
#  This file merges three previously separate modules:
#     1. registro_ui.py  (Registration UI)
#     2. login_ui.py      (Login UI)
#     3. backend.py       (FastAPI + SQLite backend)
#
#  The PRD specifies a client-server architecture (tkinter client <-> FastAPI
#  server <-> SQLite). To honour that while still shipping ONE runnable file,
#  the FastAPI server is launched in a background daemon thread and the tkinter
#  client talks to it over HTTP (localhost:8000) using the `requests` library.
#  This keeps the student's real backend intact and demonstrates the full stack:
#  the UI writes new users to the DB and reads existing users back on login.
#
#  Run:   python coopdoc_app.py
#  Deps:  pip install fastapi uvicorn requests pydantic
# ==============================================================================

import math
import re
import time
import sqlite3
import hashlib
import threading
from datetime import datetime
from typing import Dict, List, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, field_validator, model_validator

import tkinter as tk
from tkinter import messagebox
import tkinter.font as tkfont


# ==============================================================================
#  DESIGN TOKENS  (strict palette from the Design Guidelines)
# ==============================================================================
APP_BACKGROUND   = "#F8F9FA"   # Off-white: root window / main background
CANVAS_BACKGROUND = "#FFFFFF"  # Pure white: input fields (and future editor canvas)
PRIMARY_ACCENT   = "#3B82F6"   # Vibrant blue: primary CTA buttons
HOVER_ACCENT     = "#2563EB"   # Darker blue: hover state of primary buttons
TEXT_PRIMARY     = "#1F2937"   # Dark slate grey: labels, menus, typed text

# Supporting tints derived to reproduce the reference images while staying
# inside the spirit of the palette (used for promo cards, borders, placeholders).
PROMO_BG     = "#EFF3FE"   # Light blue promotional / callout panel
INPUT_BORDER = "#E2E8F0"   # Thin light-gray input/card outline (slate-200)
SLATE_TEXT   = "#64748B"   # Secondary slate body text (slate-500/600)
PLACEHOLDER  = "#9CA3AF"   # Italic gray placeholder text
DIVIDER      = "#E2E8F0"   # Hairline dividers
OUTLINE_HOVER = "#EFF4FE"  # Soft hover wash for outline buttons

# Editor canvas font is defined here for completeness (Design Guidelines), but
# the workspace itself is the next milestone and is not built in this prototype.
EDITOR_FONT = ("Open Sans", 12)

API_BASE = "http://127.0.0.1:8000"

# Fonts are real Font objects created once a Tk root exists (see init_fonts()).
F_TITLE = F_H2 = F_FORM_TITLE = F_LABEL = F_INPUT = F_INPUT_ITALIC = None
F_BTN = F_SMALL = F_LINK = F_PROMO_TITLE = F_PROMO_DESC = None


# ==============================================================================
# ==============================================================================
#  SECTION A — BACKEND  (FastAPI + SQLite + Pydantic + WebSockets)
#  Adapted from backend.py. Validation rules from the PRD are enforced here as
#  the single source of truth; the frontend mirrors them for instant feedback.
# ==============================================================================
# ==============================================================================

app = FastAPI(title="Collaborative Text Editor API")

DB_FILE = "editor_backend.db"


def get_db_connection():
    # check_same_thread=False: FastAPI serves requests across threads.
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    """Create tables if missing and migrate the users table if needed."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # NOTE: date_of_birth was added to align the backend with the registration
    # UI (the PRD lists Name/Email/Password, but the UI + reference images also
    # collect a birth date). Keeping schema and UI consistent is best practice.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date_of_birth TEXT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')

    # Lightweight migration for older DB files that predate date_of_birth.
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
            role TEXT NOT NULL,                 -- 'owner' or 'editor'
            FOREIGN KEY(document_id) REFERENCES documents(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(document_id, user_id)
        )
    ''')

    conn.commit()
    conn.close()


initialize_database()


# ------------------------------------------------------------------------------
#  Pydantic models & strict validation (PRD Section 5)
# ------------------------------------------------------------------------------
# Accept any Unicode letter (incl. accented Spanish letters and ñ) plus single
# spaces between words. Numbers and special characters are rejected. The original
# `^[A-Za-z]+$` rule was too strict — it rejected the spec's own example name
# "César Ríos Oliváres" — so it is relaxed here to letters + spaces.
NAME_RE = re.compile(r"^[^\W\d_]+(?: [^\W\d_]+)*$", re.UNICODE)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


class UserRegister(BaseModel):
    name: str
    date_of_birth: str
    email: str
    password: str
    confirm_password: str

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str):
        value = value.strip()
        if not NAME_RE.match(value):
            raise ValueError("Name must contain only letters and spaces.")
        return value

    @field_validator('date_of_birth')
    @classmethod
    def validate_dob(cls, value: str):
        value = value.strip()
        if not DATE_RE.match(value):
            raise ValueError("Date of birth must use the format dd/mm/aaaa.")
        try:
            datetime.strptime(value, "%d/%m/%Y")
        except ValueError:
            raise ValueError("Date of birth is not a valid calendar date.")
        return value

    @field_validator('email')
    @classmethod
    def validate_email(cls, value: str):
        if value.count('@') != 1:
            raise ValueError("Email must contain exactly one @ symbol.")
        if re.search(r'[\s\\]', value):
            raise ValueError("Email cannot contain spaces or backslashes.")
        if '..' in value:
            raise ValueError("Email cannot contain consecutive dots.")
        if value.startswith('.') or value.endswith('.') or '@.' in value or '.@' in value:
            raise ValueError("Email cannot have leading/trailing dots or dots next to @.")
        if not EMAIL_RE.match(value):
            raise ValueError("Email contains invalid characters or structure.")
        return value

    @field_validator('password')
    @classmethod
    def validate_password(cls, value: str):
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if not re.search(r"[A-Z]", value):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", value):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r"\d", value):
            raise ValueError("Password must contain at least one number.")
        if not re.search(r"[^A-Za-z0-9]", value):
            raise ValueError("Password must contain at least one special character.")
        return value

    @model_validator(mode='after')
    def check_passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match.")
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


# ------------------------------------------------------------------------------
#  Utilities
# ------------------------------------------------------------------------------
def hash_password(password: str) -> str:
    # SHA-256 keeps things dependency-free for this learning prototype.
    # (Production should use bcrypt/argon2 with a per-user salt.)
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


def check_permission(doc_id: int, user_id: int) -> Optional[str]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM permissions WHERE document_id=? AND user_id=?",
                   (doc_id, user_id))
    row = cursor.fetchone()
    conn.close()
    return row['role'] if row else None


# ------------------------------------------------------------------------------
#  HTTP endpoints
# ------------------------------------------------------------------------------
@app.get("/")
def health():
    """Health probe used by the client to wait for server readiness."""
    return {"status": "ok"}


@app.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(user: UserRegister):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM users WHERE email=?", (user.email,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered.")

    hashed_pw = hash_password(user.password)
    try:
        cursor.execute(
            "INSERT INTO users (name, date_of_birth, email, password_hash) VALUES (?, ?, ?, ?)",
            (user.name, user.date_of_birth, user.email, hashed_pw)
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail="Database error occurred.")
    conn.close()
    return {"message": "User registered successfully."}


@app.post("/login")
def login_user(credentials: UserLogin):
    conn = get_db_connection()
    cursor = conn.cursor()
    hashed_pw = hash_password(credentials.password)
    cursor.execute(
        "SELECT id, name FROM users WHERE email=? AND password_hash=?",
        (credentials.email, hashed_pw)
    )
    user = cursor.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return {"message": "Login successful", "user_id": user['id'], "name": user['name']}


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
    return {"message": "Document created", "document_id": doc_id}


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, requester_id: int):
    if not check_permission(doc_id, requester_id):
        raise HTTPException(status_code=403, detail="Access denied.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT title, content FROM documents WHERE id=?", (doc_id,))
    doc = cursor.fetchone()
    conn.close()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"title": doc['title'], "content": doc['content']}


@app.post("/invite")
def invite_user(invite: InviteUser):
    if not check_permission(invite.document_id, invite.requester_id):
        raise HTTPException(status_code=403, detail="You do not have access to this document.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email=?", (invite.invitee_email,))
    invitee = cursor.fetchone()
    if not invitee:
        conn.close()
        raise HTTPException(status_code=404, detail="Invitee email not found.")
    invitee_id = invitee['id']
    try:
        cursor.execute(
            "INSERT INTO permissions (document_id, user_id, role) VALUES (?, ?, 'editor')",
            (invite.document_id, invitee_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return {"message": f"User {invite.invitee_email} invited successfully."}


@app.delete("/revoke")
def revoke_access(revoke: RevokeAccess):
    role = check_permission(revoke.document_id, revoke.requester_id)
    if role != 'owner':
        raise HTTPException(status_code=403, detail="Only the document owner can revoke access.")
    if revoke.requester_id == revoke.target_user_id:
        raise HTTPException(status_code=400, detail="Owner cannot revoke their own access.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM permissions WHERE document_id=? AND user_id=?",
                   (revoke.document_id, revoke.target_user_id))
    conn.commit()
    conn.close()
    return {"message": "User access revoked."}


# ------------------------------------------------------------------------------
#  WebSocket: real-time collaboration (wired for the future editor milestone)
# ------------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, doc_id: int):
        await websocket.accept()
        self.active_connections.setdefault(doc_id, []).append(websocket)

    def disconnect(self, websocket: WebSocket, doc_id: int):
        if doc_id in self.active_connections:
            self.active_connections[doc_id].remove(websocket)
            if not self.active_connections[doc_id]:
                del self.active_connections[doc_id]

    async def broadcast_text_update(self, doc_id: int, new_content: str, sender: WebSocket):
        for connection in self.active_connections.get(doc_id, []):
            if connection != sender:
                await connection.send_text(new_content)


manager = ConnectionManager()


@app.websocket("/ws/{doc_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, doc_id: int, user_id: int):
    if not check_permission(doc_id, user_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await manager.connect(websocket, doc_id)
    try:
        while True:
            new_content = await websocket.receive_text()
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE documents SET content=? WHERE id=?", (new_content, doc_id))
            conn.commit()
            conn.close()
            await manager.broadcast_text_update(doc_id, new_content, sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket, doc_id)


def start_backend():
    """Run uvicorn in a daemon thread (signal handlers disabled off main thread)."""
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


# ==============================================================================
# ==============================================================================
#  SECTION B — CLIENT-SIDE VALIDATION (Spanish messages, mirrors the backend)
#  PRD requires rules to be enforced on the frontend AND validated on the API.
# ==============================================================================
# ==============================================================================
def validate_registration(name, dob, email, password, confirm):
    """Return None if valid, otherwise a Spanish error message for the user."""
    if not all([name, dob, email, password, confirm]):
        return "Completa todos los campos."

    if not NAME_RE.match(name):
        return "El nombre solo puede contener letras y espacios."

    if not DATE_RE.match(dob):
        return "La fecha debe tener el formato dd/mm/aaaa."
    try:
        datetime.strptime(dob, "%d/%m/%Y")
    except ValueError:
        return "La fecha de nacimiento no es una fecha válida."

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


# ==============================================================================
# ==============================================================================
#  SECTION C — FRONTEND (tkinter)
# ==============================================================================
# ==============================================================================
def resolve_font_family(preferred, fallbacks):
    """Use 'Rubik' if installed; otherwise fall back to a clean system font."""
    available = {f.lower() for f in tkfont.families()}
    for fam in [preferred] + fallbacks:
        if fam.lower() in available:
            return fam
    return preferred  # tkinter silently substitutes if unavailable


def init_fonts():
    """Create the named fonts once a Tk root exists (Design Guidelines: Rubik)."""
    global F_TITLE, F_H2, F_FORM_TITLE, F_LABEL, F_INPUT, F_INPUT_ITALIC
    global F_BTN, F_SMALL, F_LINK, F_PROMO_TITLE, F_PROMO_DESC
    fam = resolve_font_family("Rubik", ["Segoe UI", "Helvetica Neue", "Arial"])
    F_TITLE       = tkfont.Font(family=fam, size=30, weight="bold")
    F_H2          = tkfont.Font(family=fam, size=20, weight="bold")
    F_FORM_TITLE  = tkfont.Font(family=fam, size=26, weight="bold")
    F_LABEL       = tkfont.Font(family=fam, size=11, weight="bold")
    F_INPUT       = tkfont.Font(family=fam, size=12)
    F_INPUT_ITALIC = tkfont.Font(family=fam, size=12, slant="italic")
    F_BTN         = tkfont.Font(family=fam, size=13, weight="bold")
    F_SMALL       = tkfont.Font(family=fam, size=10, weight="bold")
    F_LINK        = tkfont.Font(family=fam, size=11, weight="bold")
    F_PROMO_TITLE = tkfont.Font(family=fam, size=22, weight="bold")
    F_PROMO_DESC  = tkfont.Font(family=fam, size=12)


# ------------------------------------------------------------------------------
#  Reusable widgets
# ------------------------------------------------------------------------------
class PlaceholderEntry(tk.Entry):
    """Entry with italic gray placeholder text and password masking support."""

    def __init__(self, master, placeholder, is_password=False, **kw):
        super().__init__(master, **kw)
        self.placeholder = placeholder
        self.is_password = is_password
        self.mask = is_password          # True -> hide characters with '*'
        self.showing_placeholder = False
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self._show_placeholder()

    def _show_placeholder(self):
        self.delete(0, tk.END)
        if self.is_password:
            self.config(show="")
        self.config(fg=PLACEHOLDER, font=F_INPUT_ITALIC)
        self.insert(0, self.placeholder)
        self.showing_placeholder = True

    def _on_focus_in(self, _event=None):
        if self.showing_placeholder:
            self.delete(0, tk.END)
            self.config(fg=TEXT_PRIMARY, font=F_INPUT)
            if self.is_password and self.mask:
                self.config(show="*")
            self.showing_placeholder = False

    def _on_focus_out(self, _event=None):
        if not self.get():
            self._show_placeholder()

    def value(self):
        """Real user value ('' if only the placeholder is showing)."""
        return "" if self.showing_placeholder else self.get()

    def set_mask(self, mask: bool):
        self.mask = mask
        if not self.showing_placeholder:
            self.config(show="*" if mask else "")

    def reset(self):
        self.mask = self.is_password
        self._show_placeholder()


def make_field(parent, placeholder, is_password=False):
    """A bordered white input box; returns (container, PlaceholderEntry, toggle)."""
    border = tk.Frame(parent, bg=INPUT_BORDER)
    inner = tk.Frame(border, bg=CANVAS_BACKGROUND)
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
            toggle.config(text="Show" if new_mask else "Hide")

        toggle = tk.Button(
            inner, text="Show", command=_toggle, relief="flat", bd=0,
            bg=CANVAS_BACKGROUND, fg=PRIMARY_ACCENT, activebackground=CANVAS_BACKGROUND,
            activeforeground=HOVER_ACCENT, font=F_SMALL, cursor="hand2",
        )
        toggle.pack(side="right", padx=(0, 12))

    entry.pack(side="left", fill="x", expand=True, padx=(14, 8), ipady=9)
    return border, entry, toggle


def style_primary(btn):
    """Solid blue CTA that darkens on hover."""
    btn.config(bg=PRIMARY_ACCENT, fg=CANVAS_BACKGROUND, activebackground=HOVER_ACCENT,
               activeforeground=CANVAS_BACKGROUND, relief="flat", bd=0,
               cursor="hand2", font=F_BTN)
    btn.bind("<Enter>", lambda e: btn.config(bg=HOVER_ACCENT))
    btn.bind("<Leave>", lambda e: btn.config(bg=PRIMARY_ACCENT))
    return btn


def make_outline_button(parent, text, command):
    """White button with a blue border and bold blue text (border via frame trick)."""
    border = tk.Frame(parent, bg=PRIMARY_ACCENT)
    btn = tk.Button(border, text=text, command=command, bg=CANVAS_BACKGROUND,
                    fg=PRIMARY_ACCENT, activebackground=OUTLINE_HOVER,
                    activeforeground=HOVER_ACCENT, relief="flat", bd=0,
                    cursor="hand2", font=F_BTN)
    btn.pack(fill="both", expand=True, padx=2, pady=2, ipady=7)
    return border


def draw_star_logo(parent, size=84, bg=CANVAS_BACKGROUND):
    """A small blue star inside a circle, approximating the brand mark."""
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


# ------------------------------------------------------------------------------
#  Application controller
# ------------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CoopDoc — Editor de Texto Colaborativo")
        # Design Guidelines: 4:3 horizontal window.
        self.geometry("1024x768")
        self.resizable(False, False)
        self.configure(bg=APP_BACKGROUND)

        init_fonts()

        container = tk.Frame(self, bg=APP_BACKGROUND)
        container.pack(fill="both", expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.frames = {}
        for FrameClass in (LoginFrame, RegisterFrame, DashboardFrame):
            frame = FrameClass(container, self)
            self.frames[FrameClass.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.show("LoginFrame")

    def show(self, name):
        self.frames[name].tkraise()

    def on_login_success(self, user_name):
        self.frames["DashboardFrame"].greet(user_name)
        self.show("DashboardFrame")


# ------------------------------------------------------------------------------
#  Login screen  (centered single-column card)
# ------------------------------------------------------------------------------
class LoginFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller

        card = tk.Frame(self, bg=APP_BACKGROUND)
        card.place(relx=0.5, rely=0.5, anchor="center", width=470)

        # --- Header: welcome title, star logo, subtitle ---
        tk.Label(card, text="¡Bienvenido!", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_TITLE).pack(pady=(0, 8))
        draw_star_logo(card, size=84, bg=APP_BACKGROUND).pack(pady=4)
        tk.Label(card, text="Inicia sesión", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_H2).pack(pady=(4, 28))

        # --- Email field ---
        tk.Label(card, text="Ingresa tu correo", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 5))
        box, self.entry_email, _ = make_field(card, "ej. usuario@correo.com")
        box.pack(fill="x", pady=(0, 16))

        # --- Password field with Show/Hide toggle ---
        tk.Label(card, text="Ingresa tu contraseña", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 5))
        box, self.entry_password, _ = make_field(card, "ej. Contraseña1!", is_password=True)
        box.pack(fill="x", pady=(0, 10))

        # --- Forgot password link ---
        forgot = tk.Label(card, text="¿Olvidaste tu contraseña?", bg=APP_BACKGROUND,
                          fg=PRIMARY_ACCENT, font=F_LINK)
        forgot.pack(pady=(0, 18))
        add_hover_underline(forgot)
        forgot.bind("<Button-1>", lambda e: messagebox.showinfo(
            "Recuperar contraseña",
            "La recuperación de contraseña aún no está disponible en este prototipo."))

        # --- Submit button ---
        submit = tk.Button(card, text="Iniciar sesión", command=self.handle_login)
        style_primary(submit)
        submit.pack(fill="x", ipady=9, pady=(0, 28))

        # --- Bottom registration panel ---
        panel_border = tk.Frame(card, bg=INPUT_BORDER)
        panel_border.pack(fill="x")
        panel = tk.Frame(panel_border, bg="#F1F5F9")
        panel.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(panel, text="¿No cuentas con cuenta propia?", bg="#F1F5F9",
                 fg=SLATE_TEXT, font=F_INPUT).pack(pady=(18, 12))
        reg_btn = make_outline_button(panel, "Regístrate aquí",
                                      lambda: controller.show("RegisterFrame"))
        reg_btn.pack(pady=(0, 18))

    def handle_login(self):
        email = self.entry_email.value().strip()
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
            self.controller.on_login_success(data.get("name", ""))
        elif resp.status_code == 401:
            messagebox.showerror(
                "Error", "Correo o contraseña incorrectos. Revisa tu información e intenta de nuevo.")
        else:
            messagebox.showerror("Error", "Ocurrió un problema. Intenta de nuevo.")


# ------------------------------------------------------------------------------
#  Register screen  (split pane: ~40% promo card / ~60% form)
# ------------------------------------------------------------------------------
class RegisterFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller

        # Two-column grid: 40% / 60%
        self.grid_columnconfigure(0, weight=40, uniform="cols")
        self.grid_columnconfigure(1, weight=60, uniform="cols")
        self.grid_rowconfigure(0, weight=1)

        self._build_promo_panel()
        self._build_form_panel()

    # ---- Left promotional panel ----
    def _build_promo_panel(self):
        promo = tk.Frame(self, bg=PROMO_BG)
        promo.grid(row=0, column=0, sticky="nsew", padx=(24, 12), pady=24)

        inner = tk.Frame(promo, bg=PROMO_BG)
        inner.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.86)

        draw_star_logo(inner, size=90, bg=PROMO_BG).pack(pady=(0, 14))
        tk.Label(inner, text="Únete junto a tus\namigos a CoopDoc", bg=PROMO_BG,
                 fg=TEXT_PRIMARY, font=F_PROMO_TITLE, justify="center").pack()

        # Tiny accent divider bar under the title
        tk.Frame(inner, bg=PRIMARY_ACCENT, height=3, width=48).pack(pady=16)

        tk.Label(
            inner,
            text=("Usando CoopDoc, termina de volada tus trabajos grupales. "
                  "Para ello, crea una cuenta nueva con nosotros."),
            bg=PROMO_BG, fg=SLATE_TEXT, font=F_PROMO_DESC, justify="center",
            wraplength=300,
        ).pack(pady=(0, 30))

        # Footer: "Ya tengo cuenta  Regresar a inicio de sesión"
        footer = tk.Frame(inner, bg=PROMO_BG)
        footer.pack()
        tk.Label(footer, text="Ya tengo cuenta", bg=PROMO_BG, fg=SLATE_TEXT,
                 font=F_INPUT).pack(side="left", padx=(0, 6))
        link = tk.Label(footer, text="Regresar a inicio de sesión", bg=PROMO_BG,
                        fg=PRIMARY_ACCENT, font=F_LINK)
        link.pack(side="left")
        add_hover_underline(link)
        link.bind("<Button-1>", lambda e: self.controller.show("LoginFrame"))

    # ---- Right form panel ----
    def _build_form_panel(self):
        panel = tk.Frame(self, bg=APP_BACKGROUND)
        panel.grid(row=0, column=1, sticky="nsew", padx=(12, 40), pady=24)

        form = tk.Frame(panel, bg=APP_BACKGROUND)
        form.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)

        # Title + accent divider
        tk.Label(form, text="¡Bienvenido!", bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                 font=F_FORM_TITLE).pack()
        tk.Frame(form, bg=PRIMARY_ACCENT, height=3, width=70).pack(pady=(8, 18))

        def field(label_text, placeholder, is_password=False):
            tk.Label(form, text=label_text, bg=APP_BACKGROUND, fg=TEXT_PRIMARY,
                     font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 4))
            box, entry, _ = make_field(form, placeholder, is_password=is_password)
            box.pack(fill="x", pady=(0, 10))
            return entry

        self.entry_name = field("Ingresa tu nombre y apellidos:", "ej. César Ríos Oliváres")

        self.entry_dob = field("Ingresa tu fecha de nacimiento (dd/mm/aaaa):", "ej. 17/09/2001")
        self.entry_dob.bind("<KeyRelease>", self._auto_format_date)

        self.entry_email = field("Ingresa tu correo de verificación:", "ej. usuario@correo.com")
        self.entry_password = field("Ingresa tu contraseña:", "ej. Contraseña1!", is_password=True)
        self.entry_confirm = field("Verifica tu contraseña:", "Repite tu contraseña", is_password=True)

        # Primary CTA
        create = tk.Button(form, text="Crear cuenta", command=self.handle_register)
        style_primary(create)
        create.pack(fill="x", ipady=10, pady=(8, 14))

        # Divider with central "o" badge
        divider = tk.Frame(form, bg=APP_BACKGROUND)
        divider.pack(fill="x", pady=(0, 14))
        tk.Frame(divider, bg=DIVIDER, height=1).pack(side="left", fill="x", expand=True, pady=8)
        tk.Label(divider, text="o", bg=APP_BACKGROUND, fg=PLACEHOLDER,
                 font=F_INPUT).pack(side="left", padx=12)
        tk.Frame(divider, bg=DIVIDER, height=1).pack(side="left", fill="x", expand=True, pady=8)

        # Google sign-up (visual stub for the prototype)
        google = make_outline_button(
            form, "Registrarse con Google",
            lambda: messagebox.showinfo(
                "Google", "El registro con Google aún no está implementado en este prototipo."))
        google.pack(fill="x")

    def _auto_format_date(self, event):
        # Auto-insert "/" as the user types dd/mm/aaaa (ignore placeholder & edits).
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
        name = self.entry_name.value().strip()
        dob = self.entry_dob.value().strip()
        email = self.entry_email.value().strip()
        password = self.entry_password.value()
        confirm = self.entry_confirm.value()

        # 1) Client-side validation (PRD: enforced on the frontend).
        error = validate_registration(name, dob, email, password, confirm)
        if error:
            messagebox.showerror("Error", error)
            return

        # 2) Backend validation + persistence (PRD: validated on the API).
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
            messagebox.showinfo("Registro", "¡Cuenta creada correctamente! Ya puedes iniciar sesión.")
            self._reset_form()
            self.controller.show("LoginFrame")
        elif resp.status_code == 400:
            messagebox.showerror("Error", "Este correo ya está registrado.")
        else:
            # 422 (schema) or anything unexpected: keep the message friendly.
            messagebox.showerror("Error", "Revisa tu información e intenta de nuevo.")


# ------------------------------------------------------------------------------
#  Dashboard placeholder (shown after a successful login)
#  The full collaborative editor workspace is the next milestone (PRD §4B–4D).
# ------------------------------------------------------------------------------
class DashboardFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller

        box = tk.Frame(self, bg=APP_BACKGROUND)
        box.place(relx=0.5, rely=0.5, anchor="center")

        draw_star_logo(box, size=90, bg=APP_BACKGROUND).pack(pady=(0, 16))
        self.greeting = tk.Label(box, text="¡Hola!", bg=APP_BACKGROUND,
                                 fg=TEXT_PRIMARY, font=F_TITLE)
        self.greeting.pack(pady=(0, 10))
        tk.Label(box, text="Tu espacio de trabajo colaborativo estará disponible muy pronto.",
                 bg=APP_BACKGROUND, fg=SLATE_TEXT, font=F_PROMO_DESC,
                 wraplength=420, justify="center").pack(pady=(0, 28))

        logout = tk.Button(box, text="Cerrar sesión",
                           command=lambda: controller.show("LoginFrame"))
        style_primary(logout)
        logout.pack(ipady=8, ipadx=24)

    def greet(self, name):
        nice = name.split(" ")[0] if name else ""
        self.greeting.config(text=f"¡Hola, {nice}!" if nice else "¡Hola!")


# ==============================================================================
#  MAIN
# ==============================================================================
if __name__ == "__main__":
    start_backend()
    if not wait_until_ready():
        print("Advertencia: el servidor no respondió a tiempo; "
              "las acciones de red podrían fallar.")
    App().mainloop()