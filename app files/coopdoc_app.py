import math
import re
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


# Tokens de diseno: paleta estricta de las Guias de Diseno
APP_BACKGROUND = "#F8F9FA"      # Blanco hueso: ventana raiz y fondos principales
CANVAS_BACKGROUND = "#FFFFFF"   # Blanco puro: campos de entrada y lienzo del editor
PRIMARY_ACCENT = "#3B82F6"      # Azul vibrante: botones de accion principal
HOVER_ACCENT = "#2563EB"        # Azul oscuro: estado hover de botones principales
TEXT_PRIMARY = "#1F2937"        # Gris pizarra: etiquetas, menus y texto escrito

# Tintes de apoyo derivados de la paleta para tarjetas, bordes y textos secundarios
PROMO_BG = "#EFF3FE"
INPUT_BORDER = "#E2E8F0"
SLATE_TEXT = "#64748B"
PLACEHOLDER = "#9CA3AF"
DIVIDER = "#E2E8F0"
OUTLINE_HOVER = "#EFF4FE"

# Fuente exclusiva del lienzo del editor (Guias de Diseno)
EDITOR_FONT = ("Open Sans", 12)

API_BASE = "http://127.0.0.1:8000"

# Las fuentes son objetos reales que se crean cuando ya existe una raiz Tk
F_TITLE = F_H2 = F_FORM_TITLE = F_LABEL = F_INPUT = F_INPUT_ITALIC = None
F_BTN = F_BTN_ITALIC = F_SMALL = F_LINK = F_PROMO_TITLE = F_PROMO_DESC = None


# Backend: FastAPI + SQLite + Pydantic + WebSockets.
# Las reglas de validacion del PRD se aplican aqui. El frontend las replica para dar retroalimentacion inmediata.
app = FastAPI(title="Collaborative Text Editor API")

DB_FILE = "editor_backend.db"


def get_db_connection():
    # check_same_thread=False: FastAPI atiende peticiones en varios hilos
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    # Si existe una base de datos de una version anterior con contrasena cifrada,
    # conviene borrar editor_backend.db para que se cree el esquema actual.
    conn = get_db_connection()
    cursor = conn.cursor()

    # date_of_birth alinea el backend con la interfaz de registro (el PRD pide
    # Nombre/Correo/Contrasena, pero la UI tambien recoge la fecha de nacimiento)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date_of_birth TEXT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')

    # Migracion ligera para archivos antiguos que no tenian date_of_birth
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


# Modelos Pydantic y validacion estricta (PRD Seccion 5).
# Se aceptan letras Unicode (incluidas acentuadas y la n) mas espacios simples entre palabras.
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
            raise ValueError("El nombre solo puede contener letras y espacios.")
        return value

    @field_validator('date_of_birth')
    @classmethod
    def validate_dob(cls, value: str):
        value = value.strip()
        if not DATE_RE.match(value):
            raise ValueError("La fecha debe tener el formato dd/mm/aaaa.")
        try:
            datetime.strptime(value, "%d/%m/%Y")
        except ValueError:
            raise ValueError("La fecha de nacimiento no es una fecha valida.")
        return value

    @field_validator('email')
    @classmethod
    def validate_email(cls, value: str):
        if value.count('@') != 1:
            raise ValueError("El correo debe contener exactamente un simbolo @.")
        if re.search(r'[\s\\]', value):
            raise ValueError("El correo no puede contener espacios ni barras invertidas.")
        if '..' in value:
            raise ValueError("El correo no puede contener puntos consecutivos.")
        if value.startswith('.') or value.endswith('.') or '@.' in value or '.@' in value:
            raise ValueError("El correo no puede tener puntos al inicio/final ni junto a la @.")
        if not EMAIL_RE.match(value):
            raise ValueError("El correo tiene un formato invalido.")
        return value

    @field_validator('password')
    @classmethod
    def validate_password(cls, value: str):
        if len(value) < 8:
            raise ValueError("La contrasena debe tener al menos 8 caracteres.")
        if not re.search(r"[A-Z]", value):
            raise ValueError("La contrasena debe incluir al menos una mayuscula.")
        if not re.search(r"[a-z]", value):
            raise ValueError("La contrasena debe incluir al menos una minuscula.")
        if not re.search(r"\d", value):
            raise ValueError("La contrasena debe incluir al menos un numero.")
        if not re.search(r"[^A-Za-z0-9]", value):
            raise ValueError("La contrasena debe incluir al menos un caracter especial.")
        return value

    @model_validator(mode='after')
    def check_passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Las contrasenas no coinciden.")
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


def check_permission(doc_id: int, user_id: int) -> Optional[str]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM permissions WHERE document_id=? AND user_id=?",
                   (doc_id, user_id))
    row = cursor.fetchone()
    conn.close()
    return row['role'] if row else None


# Endpoints HTTP
@app.get("/")
def health():
    # Sonda de salud que el cliente usa para esperar a que el servidor este listo
    return {"status": "ok"}


@app.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(user: UserRegister):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM users WHERE email=?", (user.email,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="El correo ya esta registrado.")

    try:
        cursor.execute(
            "INSERT INTO users (name, date_of_birth, email, password) VALUES (?, ?, ?, ?)",
            (user.name, user.date_of_birth, user.email, user.password)
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail="Ocurrio un error en la base de datos.")
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
        raise HTTPException(status_code=401, detail="Correo o contrasena incorrectos.")
    return {"message": "Inicio de sesion exitoso", "user_id": user['id'], "name": user['name']}


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
    return {"message": "Documento creado", "document_id": doc_id}


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, requester_id: int):
    if not check_permission(doc_id, requester_id):
        raise HTTPException(status_code=403, detail="Acceso denegado.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT title, content FROM documents WHERE id=?", (doc_id,))
    doc = cursor.fetchone()
    conn.close()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    return {"title": doc['title'], "content": doc['content']}


@app.post("/invite")
def invite_user(invite: InviteUser):
    if not check_permission(invite.document_id, invite.requester_id):
        raise HTTPException(status_code=403, detail="No tienes acceso a este documento.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email=?", (invite.invitee_email,))
    invitee = cursor.fetchone()
    if not invitee:
        conn.close()
        raise HTTPException(status_code=404, detail="El correo invitado no existe.")
    invitee_id = invitee['id']
    try:
        cursor.execute(
            "INSERT INTO permissions (document_id, user_id, role) VALUES (?, ?, 'editor')",
            (invite.document_id, invitee_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # El usuario ya tenia acceso: se ignora el duplicado
        pass
    conn.close()
    return {"message": f"Usuario {invite.invitee_email} invitado correctamente."}


@app.delete("/revoke")
def revoke_access(revoke: RevokeAccess):
    role = check_permission(revoke.document_id, revoke.requester_id)
    if role != 'owner':
        raise HTTPException(status_code=403, detail="Solo el propietario puede revocar accesos.")
    if revoke.requester_id == revoke.target_user_id:
        raise HTTPException(status_code=400, detail="El propietario no puede revocar su propio acceso.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM permissions WHERE document_id=? AND user_id=?",
                   (revoke.document_id, revoke.target_user_id))
    conn.commit()
    conn.close()
    return {"message": "Acceso de usuario revocado."}


# WebSocket: sincronizacion en tiempo real entre clientes activos del mismo documento
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
    # Ejecuta uvicorn en un hilo demonio (sin manejadores de senales fuera del hilo principal)
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


# Validacion del lado del cliente (mensajes en espanol que reflejan el backend).
# El PRD exige aplicar las reglas en el frontend y validarlas en la API.
def validate_registration(name, dob, email, password, confirm):
    # Devuelve None si es valido; en caso contrario un mensaje para el usuario
    if not all([name, dob, email, password, confirm]):
        return "Completa todos los campos."

    if not NAME_RE.match(name):
        return "El nombre solo puede contener letras y espacios."

    if not DATE_RE.match(dob):
        return "La fecha debe tener el formato dd/mm/aaaa."
    try:
        datetime.strptime(dob, "%d/%m/%Y")
    except ValueError:
        return "La fecha de nacimiento no es una fecha valida."

    if email.count("@") != 1:
        return "El correo debe contener exactamente un simbolo @."
    if re.search(r"[\s\\]", email):
        return "El correo no puede contener espacios ni barras invertidas."
    if ".." in email:
        return "El correo no puede contener puntos consecutivos."
    if email.startswith(".") or email.endswith(".") or "@." in email or ".@" in email:
        return "El correo no puede tener puntos al inicio/final ni junto a la @."
    if not EMAIL_RE.match(email):
        return "El correo tiene un formato invalido."

    if len(password) < 8:
        return "La contrasena debe tener al menos 8 caracteres."
    if not re.search(r"[A-Z]", password):
        return "La contrasena debe incluir al menos una mayuscula."
    if not re.search(r"[a-z]", password):
        return "La contrasena debe incluir al menos una minuscula."
    if not re.search(r"\d", password):
        return "La contrasena debe incluir al menos un numero."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "La contrasena debe incluir al menos un caracter especial."

    if password != confirm:
        return "Las contrasenas no coinciden."

    return None


# Frontend (tkinter)
def resolve_font_family(preferred, fallbacks):
    # Usa la fuente solicitada si esta instalada; si no, recurre a una del sistema
    available = {f.lower() for f in tkfont.families()}
    for fam in [preferred] + fallbacks:
        if fam.lower() in available:
            return fam
    return preferred


def init_fonts():
    # Crea las fuentes nombradas una vez que existe la raiz Tk (Guias: Rubik para la UI)
    global F_TITLE, F_H2, F_FORM_TITLE, F_LABEL, F_INPUT, F_INPUT_ITALIC
    global F_BTN, F_BTN_ITALIC, F_SMALL, F_LINK, F_PROMO_TITLE, F_PROMO_DESC
    fam = resolve_font_family("Rubik", ["Segoe UI", "Helvetica Neue", "Arial"])
    F_TITLE = tkfont.Font(family=fam, size=30, weight="bold")
    F_H2 = tkfont.Font(family=fam, size=20, weight="bold")
    F_FORM_TITLE = tkfont.Font(family=fam, size=26, weight="bold")
    F_LABEL = tkfont.Font(family=fam, size=11, weight="bold")
    F_INPUT = tkfont.Font(family=fam, size=12)
    F_INPUT_ITALIC = tkfont.Font(family=fam, size=12, slant="italic")
    F_BTN = tkfont.Font(family=fam, size=13, weight="bold")
    F_BTN_ITALIC = tkfont.Font(family=fam, size=13, weight="bold", slant="italic")
    F_SMALL = tkfont.Font(family=fam, size=10, weight="bold")
    F_LINK = tkfont.Font(family=fam, size=11, weight="bold")
    F_PROMO_TITLE = tkfont.Font(family=fam, size=22, weight="bold")
    F_PROMO_DESC = tkfont.Font(family=fam, size=12)


# Widgets reutilizables
class PlaceholderEntry(tk.Entry):
    # Entry con texto de marcador en cursiva gris y soporte de enmascarado de contrasena

    def __init__(self, master, placeholder, is_password=False, **kw):
        super().__init__(master, **kw)
        self.placeholder = placeholder
        self.is_password = is_password
        self.mask = is_password
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
        # Valor real del usuario ('' si solo se muestra el marcador)
        return "" if self.showing_placeholder else self.get()

    def set_mask(self, mask: bool):
        self.mask = mask
        if not self.showing_placeholder:
            self.config(show="*" if mask else "")

    def reset(self):
        self.mask = self.is_password
        self._show_placeholder()


def make_field(parent, placeholder, is_password=False):
    # Caja de entrada blanca con borde; devuelve (contenedor, PlaceholderEntry, toggle)
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
            toggle.config(text="Mostrar" if new_mask else "Ocultar")

        toggle = tk.Button(
            inner, text="Mostrar", command=_toggle, relief="flat", bd=0,
            bg=CANVAS_BACKGROUND, fg=PRIMARY_ACCENT, activebackground=CANVAS_BACKGROUND,
            activeforeground=HOVER_ACCENT, font=F_SMALL, cursor="hand2",
        )
        toggle.pack(side="right", padx=(0, 12))

    entry.pack(side="left", fill="x", expand=True, padx=(14, 8), ipady=9)
    return border, entry, toggle


def style_primary(btn):
    # Boton azul solido que se oscurece al pasar el cursor
    btn.config(bg=PRIMARY_ACCENT, fg=CANVAS_BACKGROUND, activebackground=HOVER_ACCENT,
               activeforeground=CANVAS_BACKGROUND, relief="flat", bd=0,
               cursor="hand2", font=F_BTN)
    btn.bind("<Enter>", lambda e: btn.config(bg=HOVER_ACCENT))
    btn.bind("<Leave>", lambda e: btn.config(bg=PRIMARY_ACCENT))
    return btn


def make_outline_button(parent, text, command):
    # Boton blanco con borde y texto azul (borde simulado con un frame)
    border = tk.Frame(parent, bg=PRIMARY_ACCENT)
    btn = tk.Button(border, text=text, command=command, bg=CANVAS_BACKGROUND,
                    fg=PRIMARY_ACCENT, activebackground=OUTLINE_HOVER,
                    activeforeground=HOVER_ACCENT, relief="flat", bd=0,
                    cursor="hand2", font=F_BTN)
    btn.pack(fill="both", expand=True, padx=2, pady=2, ipady=7)
    return border


def draw_star_logo(parent, size=84, bg=CANVAS_BACKGROUND):
    # Estrella azul dentro de un circulo que aproxima la marca
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
    # Subraya un enlace de texto al pasar el cursor
    base = label.cget("font")
    over = tkfont.Font(font=base)
    over.config(underline=True)
    label.bind("<Enter>", lambda e: label.config(font=over, cursor="hand2"))
    label.bind("<Leave>", lambda e: label.config(font=base))


# Controlador de la aplicacion
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CoopDoc — Editor de Texto Colaborativo")
        # Guias de Diseno: ventana en formato 4:3
        self.geometry("1024x768")
        self.resizable(False, False)
        self.configure(bg=APP_BACKGROUND)

        # Sesion del usuario actual (necesaria para crear documentos e invitar)
        self.usuario_id = None
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
        self.frames[name].tkraise()

    def on_login_success(self, user_id, user_name):
        self.usuario_id = user_id
        self.usuario_nombre = user_name
        self.frames["DashboardFrame"].greet(user_name)
        self.show("DashboardFrame")

    def abrir_editor(self, documento_id, titulo, contenido=""):
        self.frames["EditorFrame"].cargar_documento(documento_id, titulo, contenido)
        self.show("EditorFrame")


# Pantalla de inicio de sesion (tarjeta centrada de una sola columna)
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
            self.controller.on_login_success(data.get("user_id"), data.get("name", ""))
        elif resp.status_code == 401:
            messagebox.showerror(
                "Error", "Correo o contraseña incorrectos. Revisa tu información e intenta de nuevo.")
        else:
            messagebox.showerror("Error", "Ocurrió un problema. Intenta de nuevo.")


# Pantalla de registro (panel dividido: ~40% promocional / ~60% formulario)
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

        tk.Label(
            inner,
            text=("Usando CoopDoc, termina de volada tus trabajos grupales. "
                  "Para ello, crea una cuenta nueva con nosotros."),
            bg=PROMO_BG, fg=SLATE_TEXT, font=F_PROMO_DESC, justify="center",
            wraplength=300,
        ).pack(pady=(0, 30))

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

        self.entry_name = field("Ingresa tu nombre y apellidos:", "ej. César Ríos Oliváres")

        self.entry_dob = field("Ingresa tu fecha de nacimiento (dd/mm/aaaa):", "ej. 17/09/2001")
        self.entry_dob.bind("<KeyRelease>", self._auto_format_date)

        self.entry_email = field("Ingresa tu correo de verificación:", "ej. usuario@correo.com")
        self.entry_password = field("Ingresa tu contraseña:", "ej. Contraseña1!", is_password=True)
        self.entry_confirm = field("Verifica tu contraseña:", "Repite tu contraseña", is_password=True)

        create = tk.Button(form, text="Crear cuenta", command=self.handle_register)
        style_primary(create)
        create.pack(fill="x", ipady=10, pady=(8, 14))

        divider = tk.Frame(form, bg=APP_BACKGROUND)
        divider.pack(fill="x", pady=(0, 14))
        tk.Frame(divider, bg=DIVIDER, height=1).pack(side="left", fill="x", expand=True, pady=8)
        tk.Label(divider, text="o", bg=APP_BACKGROUND, fg=PLACEHOLDER,
                 font=F_INPUT).pack(side="left", padx=12)
        tk.Frame(divider, bg=DIVIDER, height=1).pack(side="left", fill="x", expand=True, pady=8)

        google = make_outline_button(
            form, "Registrarse con Google",
            lambda: messagebox.showinfo(
                "Google", "El registro con Google aún no está implementado en este prototipo."))
        google.pack(fill="x")

    def _auto_format_date(self, event):
        # Inserta "/" automaticamente al teclear dd/mm/aaaa (ignora marcador y ediciones)
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

        # Validacion en el cliente (PRD: aplicada en el frontend)
        error = validate_registration(name, dob, email, password, confirm)
        if error:
            messagebox.showerror("Error", error)
            return

        # Validacion y persistencia en el backend (PRD: validada en la API)
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
            messagebox.showerror("Error", "Revisa tu información e intenta de nuevo.")


# Tablero posterior al inicio de sesion: crear o abrir un archivo (PRD: flujo de usuario)
class DashboardFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller

        box = tk.Frame(self, bg=APP_BACKGROUND)
        box.place(relx=0.5, rely=0.5, anchor="center", width=460)

        draw_star_logo(box, size=90, bg=APP_BACKGROUND).pack(pady=(0, 16))
        self.greeting = tk.Label(box, text="¡Hola!", bg=APP_BACKGROUND,
                                 fg=TEXT_PRIMARY, font=F_TITLE)
        self.greeting.pack(pady=(0, 6))
        tk.Label(box, text="¿Qué te gustaría hacer hoy?", bg=APP_BACKGROUND,
                 fg=SLATE_TEXT, font=F_PROMO_DESC).pack(pady=(0, 24))

        crear = tk.Button(box, text="Crear un archivo nuevo", command=self._crear_archivo)
        style_primary(crear)
        crear.pack(fill="x", ipady=10, pady=(0, 12))

        abrir = make_outline_button(box, "Abrir un archivo existente", self._abrir_archivo)
        abrir.pack(fill="x", pady=(0, 24))

        logout = tk.Label(box, text="Cerrar sesión", bg=APP_BACKGROUND,
                          fg=PRIMARY_ACCENT, font=F_LINK)
        logout.pack()
        add_hover_underline(logout)
        logout.bind("<Button-1>", lambda e: self.controller.show("LoginFrame"))

    def greet(self, name):
        nice = name.split(" ")[0] if name else ""
        self.greeting.config(text=f"¡Hola, {nice}!" if nice else "¡Hola!")

    def _crear_archivo(self):
        titulo = simpledialog.askstring("Nuevo archivo",
                                        "Nombre del documento:", parent=self)
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
                                 "No se pudo conectar con el servidor. Intenta de nuevo.")
            return

        if resp.status_code == 200:
            documento_id = resp.json().get("document_id")
            self.controller.abrir_editor(documento_id, titulo)
        else:
            messagebox.showerror("Error", "No se pudo crear el documento. Intenta de nuevo.")

    def _abrir_archivo(self):
        # "Abrir existente" carga un .txt local en el lienzo (PRD: importar archivos locales)
        ruta = filedialog.askopenfilename(
            filetypes=[("Archivos de texto", "*.txt"), ("Todos los archivos", "*.*")])
        if not ruta:
            return
        try:
            with open(ruta, "r", encoding="utf-8") as archivo:
                contenido = archivo.read()
        except OSError:
            messagebox.showerror("Error", "No se pudo abrir el archivo.")
            return
        nombre = ruta.replace("\\", "/").split("/")[-1]
        self.controller.abrir_editor(None, nombre, contenido)


# Espacio de trabajo del editor (integra la funcionalidad de "editor de texto.py")
class EditorFrame(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=APP_BACKGROUND)
        self.controller = controller
        self.documento_id = None

        # Fuentes del lienzo (Open Sans, exclusivas del area de escritura)
        self.fuente_editor = tkfont.Font(family=EDITOR_FONT[0], size=EDITOR_FONT[1])
        self.fuente_negrita = tkfont.Font(family=EDITOR_FONT[0], size=EDITOR_FONT[1], weight="bold")
        self.fuente_cursiva = tkfont.Font(family=EDITOR_FONT[0], size=EDITOR_FONT[1], slant="italic")

        self._construir_barra()
        self._construir_lienzo()
        self._configurar_formatos()

    def _construir_barra(self):
        barra = tk.Frame(self, bg=APP_BACKGROUND)
        barra.pack(fill="x", padx=16, pady=(16, 8))

        self.etiqueta_titulo = tk.Label(barra, text="Documento", bg=APP_BACKGROUND,
                                        fg=TEXT_PRIMARY, font=F_H2)
        self.etiqueta_titulo.pack(side="left")

        acciones = tk.Frame(barra, bg=APP_BACKGROUND)
        acciones.pack(side="right")

        volver = make_outline_button(acciones, "Volver",
                                     lambda: self.controller.show("DashboardFrame"))
        volver.pack(side="right", padx=(8, 0))

        abrir = make_outline_button(acciones, "Abrir .txt", self._abrir_local)
        abrir.pack(side="right", padx=8)

        invitar = tk.Button(acciones, text="Invitar", command=self._invitar)
        style_primary(invitar)
        invitar.pack(side="right", padx=8, ipady=4, ipadx=10)

        guardar = tk.Button(acciones, text="Guardar .txt", command=self._guardar_local)
        style_primary(guardar)
        guardar.pack(side="right", padx=8, ipady=4, ipadx=10)

        formato = tk.Frame(self, bg=APP_BACKGROUND)
        formato.pack(fill="x", padx=16, pady=(0, 8))

        self._boton_formato(formato, "Negrita", F_BTN, self._alternar_negrita).pack(
            side="left", padx=(0, 8))
        self._boton_formato(formato, "Cursiva", F_BTN_ITALIC, self._alternar_cursiva).pack(
            side="left")

    def _boton_formato(self, parent, texto, fuente, comando):
        # Boton de formato con etiqueta de UI (Rubik), no de lienzo
        return tk.Button(parent, text=texto, font=fuente, command=comando,
                         bg=CANVAS_BACKGROUND, fg=TEXT_PRIMARY,
                         activebackground=OUTLINE_HOVER, activeforeground=HOVER_ACCENT,
                         relief="flat", bd=1, cursor="hand2", padx=14, pady=4)

    def _construir_lienzo(self):
        contenedor = tk.Frame(self, bg=INPUT_BORDER)
        contenedor.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        self.area_texto = tk.Text(
            contenedor, wrap="word", undo=True, font=self.fuente_editor,
            bg=CANVAS_BACKGROUND, fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY,
            relief="flat", bd=0, padx=12, pady=12,
        )
        self.area_texto.pack(fill="both", expand=True, padx=1, pady=1)

    def _configurar_formatos(self):
        self.area_texto.tag_configure("negrita", font=self.fuente_negrita)
        self.area_texto.tag_configure("cursiva", font=self.fuente_cursiva)

    def _alternar_etiqueta(self, nombre):
        # Activa o desactiva una etiqueta de formato sobre la seleccion actual
        try:
            inicio = self.area_texto.index("sel.first")
            fin = self.area_texto.index("sel.last")
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

    def cargar_documento(self, documento_id, titulo, contenido=""):
        # Prepara el lienzo para un documento (remoto si hay id, o local importado)
        self.documento_id = documento_id
        self.etiqueta_titulo.config(text=titulo or "Documento")
        self.area_texto.tag_remove("negrita", "1.0", "end")
        self.area_texto.tag_remove("cursiva", "1.0", "end")
        self.area_texto.delete("1.0", "end")
        if contenido:
            self.area_texto.insert("1.0", contenido)
        self.area_texto.edit_reset()
        self.area_texto.focus_set()

    def _guardar_local(self):
        ruta = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Archivos de texto", "*.txt"), ("Todos los archivos", "*.*")])
        if not ruta:
            return
        try:
            with open(ruta, "w", encoding="utf-8") as archivo:
                archivo.write(self.area_texto.get("1.0", "end-1c"))
        except OSError:
            messagebox.showerror("Error", "No se pudo guardar el archivo.")
            return
        messagebox.showinfo("Guardar", "El documento se guardó correctamente.")

    def _abrir_local(self):
        ruta = filedialog.askopenfilename(
            filetypes=[("Archivos de texto", "*.txt"), ("Todos los archivos", "*.*")])
        if not ruta:
            return
        try:
            with open(ruta, "r", encoding="utf-8") as archivo:
                contenido = archivo.read()
        except OSError:
            messagebox.showerror("Error", "No se pudo abrir el archivo.")
            return
        self.area_texto.tag_remove("negrita", "1.0", "end")
        self.area_texto.tag_remove("cursiva", "1.0", "end")
        self.area_texto.delete("1.0", "end")
        self.area_texto.insert("1.0", contenido)
        self.area_texto.edit_reset()

    def _invitar(self):
        # Invita colaboradores usando el endpoint REST existente (PRD: gestion de acceso)
        if self.documento_id is None:
            messagebox.showinfo(
                "Invitar",
                "Crea un archivo nuevo desde el panel para poder invitar colaboradores.")
            return

        correo = simpledialog.askstring("Invitar colaborador",
                                        "Correo de la persona a invitar:", parent=self)
        if not correo:
            return

        payload = {
            "document_id": self.documento_id,
            "requester_id": self.controller.usuario_id,
            "invitee_email": correo.strip(),
        }
        try:
            resp = requests.post(f"{API_BASE}/invite", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar con el servidor. Intenta de nuevo.")
            return

        if resp.status_code == 200:
            messagebox.showinfo("Invitar", f"Se invitó a {correo} correctamente.")
        elif resp.status_code == 404:
            messagebox.showerror("Error", "El correo no pertenece a ningún usuario registrado.")
        elif resp.status_code == 403:
            messagebox.showerror("Error", "No tienes acceso a este documento.")
        else:
            messagebox.showerror("Error", "Ocurrió un problema al invitar. Intenta de nuevo.")


if __name__ == "__main__":
    start_backend()
    if not wait_until_ready():
        print("Advertencia: el servidor no respondió a tiempo; "
              "las acciones de red podrían fallar.")
    App().mainloop()