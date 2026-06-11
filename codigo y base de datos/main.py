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


# Sección 1 — Tokens de diseño
# Paleta estricta según las guías de diseño.

fondo_app       = "#F8F9FA"
fondo_lienzo    = "#FFFFFF"
acento_primario = "#3B82F6"
acento_hover    = "#2563EB"
texto_primario  = "#1F2937"

fondo_promo    = "#EFF3FE"
borde_input    = "#E2E8F0"
texto_slate    = "#64748B"
placeholder    = "#9CA3AF"
divisor        = "#E2E8F0"
hover_outline  = "#EFF4FE"

estado_lectura_fondo  = "#F1F5F9"
estado_editando_fondo = "#FEF3C7"
estado_yo_fondo       = "#D1FAE5"
estado_lectura_texto  = "#475569"
estado_editando_texto = "#92400E"
estado_yo_texto       = "#065F46"

fuente_editor = ("Open Sans", 12)

# URL base de la API — se reemplaza por la URL ngrok en modo cliente
api_base = "http://127.0.0.1:8000"

# Fuentes globales: se inicializan una vez que existe la raíz Tk
f_titulo = f_h2 = f_titulo_form = f_etiqueta = f_input = f_input_italic = None
f_btn = f_btn_italic = f_small = f_link = f_promo_titulo = f_promo_desc = None


# Sección 2 — Backend: base de datos

app_servidor = FastAPI(title="CoopDoc API")
archivo_db = "editor_backend.db"


def obtener_conexion_db():
    conn = sqlite3.connect(archivo_db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def inicializar_base_de_datos():
    conn = obtener_conexion_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            correo TEXT UNIQUE NOT NULL,
            contrasena TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            contenido TEXT DEFAULT '',
            propietario_id INTEGER NOT NULL,
            FOREIGN KEY(propietario_id) REFERENCES usuarios(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS permisos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            documento_id INTEGER NOT NULL,
            usuario_id INTEGER NOT NULL,
            rol TEXT NOT NULL,
            FOREIGN KEY(documento_id) REFERENCES documentos(id),
            FOREIGN KEY(usuario_id) REFERENCES usuarios(id),
            UNIQUE(documento_id, usuario_id)
        )
    ''')

    # Migración: tabla legacy "users" → "usuarios"
    existentes = {r[0] for r in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "users" in existentes and "usuarios" in existentes:
        cursor.execute(
            "INSERT OR IGNORE INTO usuarios (id, nombre, correo, contrasena) "
            "SELECT id, name, email, password FROM users"
        )
    if "documents" in existentes and "documentos" in existentes:
        cursor.execute(
            "INSERT OR IGNORE INTO documentos (id, titulo, contenido, propietario_id) "
            "SELECT id, title, content, owner_id FROM documents"
        )
    if "permissions" in existentes and "permisos" in existentes:
        cursor.execute(
            "INSERT OR IGNORE INTO permisos (id, documento_id, usuario_id, rol) "
            "SELECT id, document_id, user_id, role FROM permissions"
        )

    conn.commit()
    conn.close()


inicializar_base_de_datos()


# Sección 3 — Backend: modelos Pydantic
# Las reglas de validación del PRD se aplican aquí; el frontend las replica.

regex_nombre = re.compile(r"^[^\W\d_]+(?: [^\W\d_]+)*$", re.UNICODE)
regex_correo = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class RegistroUsuario(BaseModel):
    nombre: str
    correo: str
    contrasena: str
    confirmar_contrasena: str

    @field_validator('nombre')
    @classmethod
    def validar_nombre(cls, v):
        v = v.strip()
        if not regex_nombre.match(v):
            raise ValueError("El nombre solo puede contener letras y espacios.")
        return v

    @field_validator('correo')
    @classmethod
    def validar_correo(cls, v):
        if v.count('@') != 1:
            raise ValueError("El correo debe contener exactamente un símbolo @.")
        if re.search(r'[\s\\]', v):
            raise ValueError("El correo no puede contener espacios ni barras invertidas.")
        if '..' in v:
            raise ValueError("El correo no puede contener puntos consecutivos.")
        if v.startswith('.') or v.endswith('.') or '@.' in v or '.@' in v:
            raise ValueError("El correo no puede tener puntos al inicio/final ni junto a la @.")
        if not regex_correo.match(v):
            raise ValueError("El correo tiene un formato inválido.")
        return v

    @field_validator('contrasena')
    @classmethod
    def validar_contrasena(cls, v):
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
    def verificar_contrasenas(self):
        if self.contrasena != self.confirmar_contrasena:
            raise ValueError("Las contraseñas no coinciden.")
        return self


class LoginUsuario(BaseModel):
    correo: str
    contrasena: str


class CrearDocumento(BaseModel):
    titulo: str
    solicitante_id: int


class InvitarUsuario(BaseModel):
    documento_id: int
    solicitante_id: int
    correo_invitado: str


class RevocarAcceso(BaseModel):
    documento_id: int
    solicitante_id: int
    usuario_objetivo_id: int


class SolicitudBloqueo(BaseModel):
    documento_id: int
    solicitante_id: int


class EnviarCambios(BaseModel):
    documento_id: int
    solicitante_id: int
    contenido: str


class LiberarBloqueo(BaseModel):
    documento_id: int
    solicitante_id: int


# Sección 4 — Backend: estado en memoria

# documento_id → usuario_id del editor activo (None = libre)
bloqueos_documentos: Dict[int, Optional[int]] = {}
# documento_id → nombre del editor activo
nombres_bloqueos: Dict[int, Optional[str]] = {}
# (documento_id, usuario_id) → timestamp de expiración del cooldown
cooldowns: Dict[tuple, float] = {}

segundos_cooldown = 3.0


# Sección 5 — Backend: WebSocket — GestorConexiones
# Canal de difusión unidireccional servidor → cliente.
# El cliente nunca envía texto por WS; toda mutación va por HTTP.

class GestorConexiones:

    def __init__(self):
        # documento_id → lista de (usuario_id, WebSocket)
        self.conexiones_activas: Dict[int, List[tuple]] = {}

    async def conectar(self, websocket: WebSocket, doc_id: int, usuario_id: int):
        await websocket.accept()
        self.conexiones_activas.setdefault(doc_id, []).append((usuario_id, websocket))

    def desconectar(self, websocket: WebSocket, doc_id: int):
        if doc_id in self.conexiones_activas:
            self.conexiones_activas[doc_id] = [
                (uid, ws) for uid, ws in self.conexiones_activas[doc_id]
                if ws is not websocket
            ]
            if not self.conexiones_activas[doc_id]:
                del self.conexiones_activas[doc_id]

    async def difundir(self, doc_id: int, datos: dict):
        payload = json.dumps(datos, ensure_ascii=False)
        muertos = []
        for uid, ws in list(self.conexiones_activas.get(doc_id, [])):
            try:
                await ws.send_text(payload)
            except Exception:
                muertos.append(ws)
        for ws in muertos:
            self.desconectar(ws, doc_id)

    async def enviar_a_usuario(self, doc_id: int, usuario_id: int, datos: dict):
        payload = json.dumps(datos, ensure_ascii=False)
        for uid, ws in list(self.conexiones_activas.get(doc_id, [])):
            if uid == usuario_id:
                try:
                    await ws.send_text(payload)
                except Exception:
                    pass


gestor = GestorConexiones()


# Sección 6 — Backend: endpoints HTTP

def verificar_permiso(doc_id: int, usuario_id: int) -> Optional[str]:
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rol FROM permisos WHERE documento_id=? AND usuario_id=?",
        (doc_id, usuario_id)
    )
    fila = cursor.fetchone()
    conn.close()
    return fila['rol'] if fila else None


@app_servidor.get("/")
def health():
    return {"status": "ok"}


@app_servidor.post("/register", status_code=status.HTTP_201_CREATED)
def registrar_usuario(usuario: RegistroUsuario):
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM usuarios WHERE correo=?", (usuario.correo,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="El correo ya está registrado.")
    try:
        cursor.execute(
            "INSERT INTO usuarios (nombre, correo, contrasena) VALUES (?, ?, ?)",
            (usuario.nombre, usuario.correo, usuario.contrasena)
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail="Error en la base de datos.")
    conn.close()
    return {"message": "Usuario registrado correctamente."}


@app_servidor.post("/login")
def iniciar_sesion(credenciales: LoginUsuario):
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, nombre FROM usuarios WHERE correo=? AND contrasena=?",
        (credenciales.correo, credenciales.contrasena)
    )
    usuario = cursor.fetchone()
    conn.close()
    if not usuario:
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")
    return {"message": "Inicio de sesión exitoso", "user_id": usuario['id'], "name": usuario['nombre']}


@app_servidor.post("/documents")
def crear_documento(doc: CrearDocumento):
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO documentos (titulo, contenido, propietario_id) VALUES (?, '', ?)",
        (doc.titulo, doc.solicitante_id)
    )
    doc_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO permisos (documento_id, usuario_id, rol) VALUES (?, ?, 'owner')",
        (doc_id, doc.solicitante_id)
    )
    conn.commit()
    conn.close()
    return {"message": "Documento creado.", "document_id": doc_id}


@app_servidor.get("/documents")
def listar_documentos(user_id: int):
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT d.id, d.titulo, p.rol
        FROM documentos d
        JOIN permisos p ON d.id = p.documento_id
        WHERE p.usuario_id = ?
        ORDER BY d.id DESC
        """,
        (user_id,)
    )
    filas = cursor.fetchall()
    conn.close()
    return {
        "documents": [
            {"id": f["id"], "title": f["titulo"], "role": f["rol"]}
            for f in filas
        ]
    }


@app_servidor.get("/documents/{doc_id}")
def obtener_documento(doc_id: int, requester_id: int):
    rol = verificar_permiso(doc_id, requester_id)
    if not rol:
        raise HTTPException(status_code=403, detail="Acceso denegado.")
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute("SELECT titulo, contenido FROM documentos WHERE id=?", (doc_id,))
    doc = cursor.fetchone()
    conn.close()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    return {"title": doc['titulo'], "content": doc['contenido'], "role": rol}


@app_servidor.get("/documents/{doc_id}/collaborators")
def listar_colaboradores(doc_id: int, requester_id: int):
    if not verificar_permiso(doc_id, requester_id):
        raise HTTPException(status_code=403, detail="Acceso denegado.")
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.id, u.nombre, u.correo, p.rol
        FROM permisos p
        JOIN usuarios u ON p.usuario_id = u.id
        WHERE p.documento_id = ?
        ORDER BY p.rol DESC, u.nombre
        """,
        (doc_id,)
    )
    filas = cursor.fetchall()
    conn.close()
    return {
        "collaborators": [
            {"id": f["id"], "name": f["nombre"], "email": f["correo"], "role": f["rol"]}
            for f in filas
        ]
    }


@app_servidor.post("/invite")
def invitar_usuario(invitacion: InvitarUsuario):
    # Solo el propietario puede invitar
    rol = verificar_permiso(invitacion.documento_id, invitacion.solicitante_id)
    if rol != 'owner':
        raise HTTPException(status_code=403,
                            detail="Solo el propietario puede invitar colaboradores.")
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM usuarios WHERE correo=?", (invitacion.correo_invitado,))
    invitado = cursor.fetchone()
    if not invitado:
        conn.close()
        raise HTTPException(status_code=404, detail="El correo invitado no existe.")
    invitado_id = invitado['id']
    if invitado_id == invitacion.solicitante_id:
        conn.close()
        raise HTTPException(status_code=400, detail="No puedes invitarte a ti mismo.")
    try:
        cursor.execute(
            "INSERT INTO permisos (documento_id, usuario_id, rol) VALUES (?, ?, 'editor')",
            (invitacion.documento_id, invitado_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return {"message": f"Usuario {invitacion.correo_invitado} invitado correctamente."}


@app_servidor.delete("/revoke")
async def revocar_acceso(revocacion: RevocarAcceso):
    rol = verificar_permiso(revocacion.documento_id, revocacion.solicitante_id)
    if rol != 'owner':
        raise HTTPException(status_code=403,
                            detail="Solo el propietario puede revocar accesos.")
    if revocacion.solicitante_id == revocacion.usuario_objetivo_id:
        raise HTTPException(status_code=400,
                            detail="El propietario no puede revocar su propio acceso.")

    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM permisos WHERE documento_id=? AND usuario_id=?",
        (revocacion.documento_id, revocacion.usuario_objetivo_id)
    )
    conn.commit()
    conn.close()

    if bloqueos_documentos.get(revocacion.documento_id) == revocacion.usuario_objetivo_id:
        bloqueos_documentos[revocacion.documento_id] = None
        nombres_bloqueos[revocacion.documento_id] = None
        await gestor.difundir(revocacion.documento_id, {
            "type": "lock_update",
            "locked_by_id": None,
            "locked_by_name": None
        })

    await gestor.enviar_a_usuario(revocacion.documento_id, revocacion.usuario_objetivo_id, {
        "type": "access_revoked"
    })

    return {"message": "Acceso de usuario revocado."}


@app_servidor.post("/request_lock")
async def solicitar_bloqueo(req: SolicitudBloqueo):
    rol = verificar_permiso(req.documento_id, req.solicitante_id)
    if not rol:
        raise HTTPException(status_code=403, detail="Acceso denegado.")

    clave = (req.documento_id, req.solicitante_id)
    restante = cooldowns.get(clave, 0) - time.time()
    if restante > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Espera {restante:.1f}s antes de volver a editar."
        )

    titular_actual = bloqueos_documentos.get(req.documento_id)

    if titular_actual == req.solicitante_id:
        return {"message": "Ya tienes el bloqueo de edición."}

    if titular_actual is not None:
        raise HTTPException(status_code=423,
                            detail="El documento ya está siendo editado por otro usuario.")

    bloqueos_documentos[req.documento_id] = req.solicitante_id

    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute("SELECT nombre FROM usuarios WHERE id=?", (req.solicitante_id,))
    fila = cursor.fetchone()
    conn.close()
    nombre_editor = fila["nombre"] if fila else "Usuario"
    nombres_bloqueos[req.documento_id] = nombre_editor

    await gestor.difundir(req.documento_id, {
        "type": "lock_update",
        "locked_by_id": req.solicitante_id,
        "locked_by_name": nombre_editor
    })
    return {"message": "Bloqueo concedido.", "editor_name": nombre_editor}


@app_servidor.post("/submit_changes")
async def enviar_cambios(sub: EnviarCambios):
    if bloqueos_documentos.get(sub.documento_id) != sub.solicitante_id:
        raise HTTPException(status_code=423, detail="No tienes el bloqueo de edición.")

    conn = obtener_conexion_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE documentos SET contenido=? WHERE id=?",
        (sub.contenido, sub.documento_id)
    )
    conn.commit()
    conn.close()

    bloqueos_documentos[sub.documento_id] = None
    nombres_bloqueos[sub.documento_id] = None
    cooldowns[(sub.documento_id, sub.solicitante_id)] = time.time() + segundos_cooldown

    await gestor.difundir(sub.documento_id, {
        "type": "content_update",
        "content": sub.contenido
    })
    await gestor.difundir(sub.documento_id, {
        "type": "lock_update",
        "locked_by_id": None,
        "locked_by_name": None
    })
    return {"message": "Cambios guardados correctamente."}


@app_servidor.post("/release_lock")
async def liberar_bloqueo(req: LiberarBloqueo):
    if bloqueos_documentos.get(req.documento_id) != req.solicitante_id:
        raise HTTPException(status_code=423, detail="No tienes el bloqueo de edición.")

    bloqueos_documentos[req.documento_id] = None
    nombres_bloqueos[req.documento_id] = None

    await gestor.difundir(req.documento_id, {
        "type": "lock_update",
        "locked_by_id": None,
        "locked_by_name": None
    })
    return {"message": "Bloqueo liberado."}


# Sección 7 — Backend: endpoint WebSocket
# Al conectar, el servidor envía el estado actual del bloqueo al nuevo cliente.

@app_servidor.websocket("/ws/{doc_id}/{usuario_id}")
async def websocket_endpoint(websocket: WebSocket, doc_id: int, usuario_id: int):
    if not verificar_permiso(doc_id, usuario_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await gestor.conectar(websocket, doc_id, usuario_id)

    titular_id   = bloqueos_documentos.get(doc_id)
    titular_nombre = nombres_bloqueos.get(doc_id)
    await websocket.send_text(json.dumps({
        "type": "lock_update",
        "locked_by_id": titular_id,
        "locked_by_name": titular_nombre
    }))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        gestor.desconectar(websocket, doc_id)
        if bloqueos_documentos.get(doc_id) == usuario_id:
            bloqueos_documentos[doc_id] = None
            nombres_bloqueos[doc_id] = None
            await gestor.difundir(doc_id, {
                "type": "lock_update",
                "locked_by_id": None,
                "locked_by_name": None
            })


# Sección 8 — Backend: arranque del servidor

def iniciar_backend():
    config = uvicorn.Config(app_servidor, host="127.0.0.1", port=8000, log_level="warning")
    servidor = uvicorn.Server(config)
    servidor.install_signal_handlers = lambda: None
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()
    return servidor


def esperar_servidor(timeout=15.0):
    inicio = time.time()
    while time.time() - inicio < timeout:
        try:
            if requests.get(f"{api_base}/", timeout=1).status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.2)
    return False


# Sección 9 — Frontend: validación del lado del cliente
# Replica las reglas del PRD para retroalimentación inmediata.

def validar_registro(nombre, correo, contrasena, confirmar):
    if not all([nombre, correo, contrasena, confirmar]):
        return "Completa todos los campos."
    if not regex_nombre.match(nombre):
        return "El nombre solo puede contener letras y espacios."
    if correo.count("@") != 1:
        return "El correo debe contener exactamente un símbolo @."
    if re.search(r"[\s\\]", correo):
        return "El correo no puede contener espacios ni barras invertidas."
    if ".." in correo:
        return "El correo no puede contener puntos consecutivos."
    if correo.startswith(".") or correo.endswith(".") or "@." in correo or ".@" in correo:
        return "El correo no puede tener puntos al inicio/final ni junto a la @."
    if not regex_correo.match(correo):
        return "El correo tiene un formato inválido."
    if len(contrasena) < 8:
        return "La contraseña debe tener al menos 8 caracteres."
    if not re.search(r"[A-Z]", contrasena):
        return "La contraseña debe incluir al menos una mayúscula."
    if not re.search(r"[a-z]", contrasena):
        return "La contraseña debe incluir al menos una minúscula."
    if not re.search(r"\d", contrasena):
        return "La contraseña debe incluir al menos un número."
    if not re.search(r"[^A-Za-z0-9]", contrasena):
        return "La contraseña debe incluir al menos un carácter especial."
    if contrasena != confirmar:
        return "Las contraseñas no coinciden."
    return None


# Sección 10 — Frontend: fuentes y utilidades de widgets

def resolver_familia_fuente(preferida, alternativas):
    disponibles = {f.lower() for f in tkfont.families()}
    for fam in [preferida] + alternativas:
        if fam.lower() in disponibles:
            return fam
    return preferida


def inicializar_fuentes():
    global f_titulo, f_h2, f_titulo_form, f_etiqueta, f_input, f_input_italic
    global f_btn, f_btn_italic, f_small, f_link, f_promo_titulo, f_promo_desc
    fam = resolver_familia_fuente("Rubik", ["Segoe UI", "Helvetica Neue", "Arial"])
    f_titulo      = tkfont.Font(family=fam, size=30, weight="bold")
    f_h2          = tkfont.Font(family=fam, size=20, weight="bold")
    f_titulo_form = tkfont.Font(family=fam, size=26, weight="bold")
    f_etiqueta    = tkfont.Font(family=fam, size=11, weight="bold")
    f_input       = tkfont.Font(family=fam, size=12)
    f_input_italic = tkfont.Font(family=fam, size=12, slant="italic")
    f_btn         = tkfont.Font(family=fam, size=13, weight="bold")
    f_btn_italic  = tkfont.Font(family=fam, size=13, weight="bold", slant="italic")
    f_small       = tkfont.Font(family=fam, size=10, weight="bold")
    f_link        = tkfont.Font(family=fam, size=11, weight="bold")
    f_promo_titulo = tkfont.Font(family=fam, size=22, weight="bold")
    f_promo_desc   = tkfont.Font(family=fam, size=12)


class EntradaConPlaceholder(tk.Entry):
    """Entry con marcador en cursiva gris y soporte de enmascarado de contraseña."""

    def __init__(self, master, placeholder_texto, es_contrasena=False, **kw):
        super().__init__(master, **kw)
        self.placeholder_texto  = placeholder_texto
        self.es_contrasena      = es_contrasena
        self.mascara            = es_contrasena
        self.mostrando_placeholder = False
        self.bind("<FocusIn>",  self._al_enfocar)
        self.bind("<FocusOut>", self._al_desenfocar)
        self._mostrar_placeholder()

    def _mostrar_placeholder(self):
        self.delete(0, tk.END)
        if self.es_contrasena:
            self.config(show="")
        self.config(fg=placeholder, font=f_input_italic)
        self.insert(0, self.placeholder_texto)
        self.mostrando_placeholder = True

    def _al_enfocar(self, _=None):
        if self.mostrando_placeholder:
            self.delete(0, tk.END)
            self.config(fg=texto_primario, font=f_input)
            if self.es_contrasena and self.mascara:
                self.config(show="*")
            self.mostrando_placeholder = False

    def _al_desenfocar(self, _=None):
        if not self.get():
            self._mostrar_placeholder()

    def valor(self):
        return "" if self.mostrando_placeholder else self.get()

    def set_mascara(self, mascara: bool):
        self.mascara = mascara
        if not self.mostrando_placeholder:
            self.config(show="*" if mascara else "")

    def resetear(self):
        self.mascara = self.es_contrasena
        self._mostrar_placeholder()


def crear_campo(parent, placeholder_texto, es_contrasena=False):
    """Devuelve (frame_borde, EntradaConPlaceholder, boton_toggle_o_None)."""
    borde = tk.Frame(parent, bg=borde_input)
    inner = tk.Frame(borde, bg=fondo_lienzo)
    inner.pack(fill="both", expand=True, padx=1, pady=1)

    entrada = EntradaConPlaceholder(
        inner, placeholder_texto, es_contrasena=es_contrasena,
        relief="flat", bg=fondo_lienzo, font=f_input_italic,
        fg=placeholder, insertbackground=texto_primario, highlightthickness=0, bd=0,
    )

    toggle = None
    if es_contrasena:
        def _toggle():
            if entrada.mostrando_placeholder:
                return
            nueva_mascara = not entrada.mascara
            entrada.set_mascara(nueva_mascara)
            toggle.config(text="Mostrar" if nueva_mascara else "Ocultar")

        toggle = tk.Button(
            inner, text="Mostrar", command=_toggle, relief="flat", bd=0,
            bg=fondo_lienzo, fg=acento_primario,
            activebackground=fondo_lienzo, activeforeground=acento_hover,
            font=f_small, cursor="hand2",
        )
        toggle.pack(side="right", padx=(0, 12))

    entrada.pack(side="left", fill="x", expand=True, padx=(14, 8), ipady=9)
    return borde, entrada, toggle


def estilo_primario(btn):
    btn.config(
        bg=acento_primario, fg=fondo_lienzo,
        activebackground=acento_hover, activeforeground=fondo_lienzo,
        relief="flat", bd=0, cursor="hand2", font=f_btn,
    )
    btn.bind("<Enter>", lambda e: btn.config(bg=acento_hover))
    btn.bind("<Leave>", lambda e: btn.config(bg=acento_primario))
    return btn


def crear_boton_outline(parent, texto, comando):
    borde = tk.Frame(parent, bg=acento_primario)
    btn = tk.Button(
        borde, text=texto, command=comando,
        bg=fondo_lienzo, fg=acento_primario,
        activebackground=hover_outline, activeforeground=acento_hover,
        relief="flat", bd=0, cursor="hand2", font=f_btn,
    )
    btn.pack(fill="both", expand=True, padx=2, pady=2, ipady=7)
    return borde


def dibujar_logo(parent, size=84, bg=fondo_lienzo):
    canvas = tk.Canvas(parent, width=size, height=size, bg=bg,
                       highlightthickness=0, bd=0)
    pad = 4
    canvas.create_oval(pad, pad, size - pad, size - pad,
                       fill=bg, outline=acento_primario, width=3)
    cx = cy = size / 2
    radio_externo = size * 0.30
    radio_interno = size * 0.13
    pts = []
    for i in range(10):
        r = radio_externo if i % 2 == 0 else radio_interno
        a = -math.pi / 2 + i * math.pi / 5
        pts.extend([cx + r * math.cos(a), cy + r * math.sin(a)])
    canvas.create_polygon(pts, fill=acento_primario, outline="")
    return canvas


def agregar_subrayado_hover(etiqueta):
    base = etiqueta.cget("font")
    sobre = tkfont.Font(font=base)
    sobre.config(underline=True)
    etiqueta.bind("<Enter>", lambda e: etiqueta.config(font=sobre, cursor="hand2"))
    etiqueta.bind("<Leave>", lambda e: etiqueta.config(font=base))


# Sección 11 — Frontend: controlador (App)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CoopDoc — Editor de Texto Colaborativo")
        self.geometry("1024x768")
        self.resizable(False, False)
        self.configure(bg=fondo_app)

        self.usuario_id     = None
        self.usuario_nombre = ""

        inicializar_fuentes()

        contenedor = tk.Frame(self, bg=fondo_app)
        contenedor.pack(fill="both", expand=True)
        contenedor.grid_rowconfigure(0, weight=1)
        contenedor.grid_columnconfigure(0, weight=1)

        self.frames = {}
        for ClaseFrame in (PantallaLogin, PantallaRegistro, PantallaTablero, PantallaEditor):
            frame = ClaseFrame(contenedor, self)
            self.frames[ClaseFrame.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.mostrar("PantallaLogin")

    def mostrar(self, nombre):
        frame = self.frames[nombre]
        frame.tkraise()
        if hasattr(frame, 'al_mostrar'):
            frame.al_mostrar()

    def al_iniciar_sesion(self, usuario_id, nombre_usuario):
        self.usuario_id     = usuario_id
        self.usuario_nombre = nombre_usuario
        self.frames["PantallaTablero"].saludar(nombre_usuario)
        self.mostrar("PantallaTablero")

    def abrir_editor(self, documento_id, titulo, contenido="", rol="editor"):
        self.frames["PantallaEditor"].cargar_documento(documento_id, titulo, contenido, rol)
        self.mostrar("PantallaEditor")

    def get_ws_base(self):
        if api_base.startswith("https://"):
            return "wss://" + api_base[len("https://"):]
        return "ws://" + api_base[len("http://"):]


# Sección 12 — Frontend: pantalla de inicio de sesión

class PantallaLogin(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=fondo_app)
        self.controller = controller

        tarjeta = tk.Frame(self, bg=fondo_app)
        tarjeta.place(relx=0.5, rely=0.5, anchor="center", width=470)

        tk.Label(tarjeta, text="¡Bienvenido!", bg=fondo_app, fg=texto_primario,
                 font=f_titulo).pack(pady=(0, 8))
        dibujar_logo(tarjeta, size=84, bg=fondo_app).pack(pady=4)
        tk.Label(tarjeta, text="Inicia sesión", bg=fondo_app, fg=texto_primario,
                 font=f_h2).pack(pady=(4, 28))

        tk.Label(tarjeta, text="Ingresa tu correo", bg=fondo_app, fg=texto_primario,
                 font=f_etiqueta, anchor="w").pack(fill="x", pady=(0, 5))
        caja, self.entrada_correo, _ = crear_campo(tarjeta, "ej. usuario@correo.com")
        caja.pack(fill="x", pady=(0, 16))

        tk.Label(tarjeta, text="Ingresa tu contraseña", bg=fondo_app, fg=texto_primario,
                 font=f_etiqueta, anchor="w").pack(fill="x", pady=(0, 5))
        caja, self.entrada_contrasena, _ = crear_campo(tarjeta, "ej. Contraseña1!", es_contrasena=True)
        caja.pack(fill="x", pady=(0, 28))

        enviar = tk.Button(tarjeta, text="Iniciar sesión", command=self._manejar_login)
        estilo_primario(enviar)
        enviar.pack(fill="x", ipady=9, pady=(0, 28))

        panel_borde = tk.Frame(tarjeta, bg=borde_input)
        panel_borde.pack(fill="x")
        panel = tk.Frame(panel_borde, bg="#F1F5F9")
        panel.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(panel, text="¿No cuentas con cuenta propia?", bg="#F1F5F9",
                 fg=texto_slate, font=f_input).pack(pady=(18, 12))
        crear_boton_outline(panel, "Regístrate aquí",
                            lambda: controller.mostrar("PantallaRegistro")).pack(pady=(0, 18))

    def _manejar_login(self):
        correo     = self.entrada_correo.valor().strip()
        contrasena = self.entrada_contrasena.valor()
        if not correo or not contrasena:
            messagebox.showerror("Error", "Completa todos los campos.")
            return
        try:
            resp = requests.post(f"{api_base}/login",
                                 json={"correo": correo, "contrasena": contrasena}, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar con el servidor. Intenta de nuevo.")
            return
        if resp.status_code == 200:
            datos = resp.json()
            self.entrada_correo.resetear()
            self.entrada_contrasena.resetear()
            self.controller.al_iniciar_sesion(datos.get("user_id"), datos.get("name", ""))
        elif resp.status_code == 401:
            messagebox.showerror("Error",
                "Correo o contraseña incorrectos. Revisa tu información e intenta de nuevo.")
        else:
            messagebox.showerror("Error", "Ocurrió un problema. Intenta de nuevo.")


# Sección 13 — Frontend: pantalla de registro

class PantallaRegistro(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=fondo_app)
        self.controller = controller
        self.grid_columnconfigure(0, weight=40, uniform="cols")
        self.grid_columnconfigure(1, weight=60, uniform="cols")
        self.grid_rowconfigure(0, weight=1)
        self._construir_panel_promo()
        self._construir_panel_formulario()

    def _construir_panel_promo(self):
        promo = tk.Frame(self, bg=fondo_promo)
        promo.grid(row=0, column=0, sticky="nsew", padx=(24, 12), pady=24)
        inner = tk.Frame(promo, bg=fondo_promo)
        inner.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.86)
        dibujar_logo(inner, size=90, bg=fondo_promo).pack(pady=(0, 14))
        tk.Label(inner, text="Únete junto a tus\namigos a CoopDoc", bg=fondo_promo,
                 fg=texto_primario, font=f_promo_titulo, justify="center").pack()
        tk.Frame(inner, bg=acento_primario, height=3, width=48).pack(pady=16)
        tk.Label(inner,
                 text="Edita archivos de texto con tus compañeros con CoopDoc. "
                      "Empieza gratis con tu cuenta.",
                 bg=fondo_promo, fg=texto_slate, font=f_promo_desc,
                 justify="center", wraplength=300).pack(pady=(0, 30))
        pie = tk.Frame(inner, bg=fondo_promo)
        pie.pack()
        tk.Label(pie, text="Ya tengo cuenta", bg=fondo_promo, fg=texto_slate,
                 font=f_input).pack(side="left", padx=(0, 6))
        enlace = tk.Label(pie, text="Regresar a inicio de sesión", bg=fondo_promo,
                          fg=acento_primario, font=f_link)
        enlace.pack(side="left")
        agregar_subrayado_hover(enlace)
        enlace.bind("<Button-1>", lambda e: self.controller.mostrar("PantallaLogin"))

    def _construir_panel_formulario(self):
        panel = tk.Frame(self, bg=fondo_app)
        panel.grid(row=0, column=1, sticky="nsew", padx=(12, 40), pady=24)
        formulario = tk.Frame(panel, bg=fondo_app)
        formulario.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)
        tk.Label(formulario, text="¡Bienvenido!", bg=fondo_app, fg=texto_primario,
                 font=f_titulo_form).pack()
        tk.Frame(formulario, bg=acento_primario, height=3, width=70).pack(pady=(8, 18))

        def campo(texto_etiqueta, placeholder_texto, es_contrasena=False):
            tk.Label(formulario, text=texto_etiqueta, bg=fondo_app, fg=texto_primario,
                     font=f_etiqueta, anchor="w").pack(fill="x", pady=(0, 4))
            caja, entrada, _ = crear_campo(formulario, placeholder_texto,
                                           es_contrasena=es_contrasena)
            caja.pack(fill="x", pady=(0, 10))
            return entrada

        self.entrada_nombre    = campo("Ingresa tu nombre y apellidos:", "ej. César Ríos Oliváres")
        self.entrada_correo    = campo("Ingresa tu correo de verificación:", "ej. usuario@correo.com")
        self.entrada_contrasena = campo("Ingresa tu contraseña:", "ej. Contraseña1!", es_contrasena=True)
        self.entrada_confirmar  = campo("Verifica tu contraseña:", "Repite tu contraseña", es_contrasena=True)

        crear_btn = tk.Button(formulario, text="Crear cuenta", command=self._manejar_registro)
        estilo_primario(crear_btn)
        crear_btn.pack(fill="x", ipady=10, pady=(8, 0))

    def _resetear_formulario(self):
        for e in (self.entrada_nombre, self.entrada_correo,
                  self.entrada_contrasena, self.entrada_confirmar):
            e.resetear()

    def _manejar_registro(self):
        nombre     = self.entrada_nombre.valor().strip()
        correo     = self.entrada_correo.valor().strip()
        contrasena = self.entrada_contrasena.valor()
        confirmar  = self.entrada_confirmar.valor()

        error = validar_registro(nombre, correo, contrasena, confirmar)
        if error:
            messagebox.showerror("Error", error)
            return

        payload = {
            "nombre": nombre,
            "correo": correo,
            "contrasena": contrasena,
            "confirmar_contrasena": confirmar,
        }
        try:
            resp = requests.post(f"{api_base}/register", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión",
                                 "No se pudo conectar con el servidor. Intenta de nuevo.")
            return
        if resp.status_code == 201:
            messagebox.showinfo("Registro",
                                "¡Cuenta creada correctamente! Ya puedes iniciar sesión.")
            self._resetear_formulario()
            self.controller.mostrar("PantallaLogin")
        elif resp.status_code == 400:
            messagebox.showerror("Error", "Este correo ya está registrado.")
        else:
            messagebox.showerror("Error", "Revisa tu información e intenta de nuevo.")


# Sección 14 — Frontend: tablero (Dashboard)

class PantallaTablero(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=fondo_app)
        self.controller = controller
        self._canvas_lista   = None
        self._inner_lista    = None
        self._ventana_canvas = None
        self._construir_ui()

    def _construir_ui(self):
        cabecera = tk.Frame(self, bg=fondo_app)
        cabecera.pack(fill="x", padx=28, pady=(20, 0))

        fila_logo = tk.Frame(cabecera, bg=fondo_app)
        fila_logo.pack(side="left")
        dibujar_logo(fila_logo, size=38, bg=fondo_app).pack(side="left", padx=(0, 10))
        tk.Label(fila_logo, text="CoopDoc", bg=fondo_app, fg=texto_primario,
                 font=f_h2).pack(side="left")

        self.lbl_saludo = tk.Label(cabecera, text="¡Hola!", bg=fondo_app,
                                   fg=texto_primario, font=f_etiqueta)
        self.lbl_saludo.pack(side="left", padx=20)

        cerrar_sesion = tk.Label(cabecera, text="Cerrar sesión", bg=fondo_app,
                                 fg=acento_primario, font=f_link)
        cerrar_sesion.pack(side="right")
        agregar_subrayado_hover(cerrar_sesion)
        cerrar_sesion.bind("<Button-1>", lambda e: self.controller.mostrar("PantallaLogin"))

        tk.Frame(self, bg=divisor, height=1).pack(fill="x", padx=28, pady=(14, 0))

        acciones = tk.Frame(self, bg=fondo_app)
        acciones.pack(fill="x", padx=28, pady=14)

        nuevo = tk.Button(acciones, text="Nuevo", command=self._crear_archivo)
        estilo_primario(nuevo)
        nuevo.pack(side="left", ipady=7, ipadx=16)

        importar = crear_boton_outline(acciones, "Importar", self._abrir_archivo_local)
        importar.pack(side="left", padx=(12, 0))

        self.btn_actualizar = crear_boton_outline(acciones, "Actualizar",
                                                  self._cargar_documentos)
        self.btn_actualizar.pack(side="right")

        tk.Label(self, text="Mis documentos", bg=fondo_app,
                 fg=texto_primario, font=f_etiqueta).pack(anchor="w", padx=28)

        tk.Frame(self, bg=divisor, height=1).pack(fill="x", padx=28, pady=(6, 0))

        contenedor_lista = tk.Frame(self, bg=fondo_app)
        contenedor_lista.pack(fill="both", expand=True, padx=28, pady=(8, 20))

        scrollbar = tk.Scrollbar(contenedor_lista, bg=fondo_app)
        scrollbar.pack(side="right", fill="y")

        self._canvas_lista = tk.Canvas(contenedor_lista, bg=fondo_app,
                                       highlightthickness=0,
                                       yscrollcommand=scrollbar.set)
        self._canvas_lista.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self._canvas_lista.yview)

        self._inner_lista = tk.Frame(self._canvas_lista, bg=fondo_app)
        self._ventana_canvas = self._canvas_lista.create_window(
            (0, 0), window=self._inner_lista, anchor="nw"
        )

        self._inner_lista.bind(
            "<Configure>",
            lambda e: self._canvas_lista.configure(
                scrollregion=self._canvas_lista.bbox("all")
            )
        )
        self._canvas_lista.bind(
            "<Configure>",
            lambda e: self._canvas_lista.itemconfig(
                self._ventana_canvas, width=e.width
            )
        )

        self.lbl_estado = tk.Label(
            self._inner_lista, text="", bg=fondo_app,
            fg=texto_slate, font=f_input
        )
        self.lbl_estado.pack(pady=20)

    def al_mostrar(self):
        self._cargar_documentos()

    def saludar(self, nombre):
        primero = nombre.split(" ")[0] if nombre else ""
        self.lbl_saludo.config(text=f"¡Hola, {primero}!" if primero else "¡Hola!")

    def _cargar_documentos(self):
        self.lbl_estado.config(text="Cargando documentos…")
        for w in self._inner_lista.winfo_children():
            if w is not self.lbl_estado:
                w.destroy()

        try:
            resp = requests.get(
                f"{api_base}/documents",
                params={"user_id": self.controller.usuario_id},
                timeout=5
            )
        except requests.exceptions.RequestException:
            self.lbl_estado.config(text="No se pudo conectar con el servidor.")
            return

        if resp.status_code != 200:
            self.lbl_estado.config(text="Error al cargar documentos.")
            return

        docs = resp.json().get("documents", [])
        self.lbl_estado.config(text="")

        if not docs:
            self.lbl_estado.config(text="Aún no tienes documentos. ¡Crea uno nuevo!")
            return

        for doc in docs:
            self._agregar_tarjeta(doc["id"], doc["title"], doc["role"])

    def _agregar_tarjeta(self, doc_id, titulo, rol):
        borde = tk.Frame(self._inner_lista, bg=borde_input)
        borde.pack(fill="x", pady=4, padx=2)

        tarjeta = tk.Frame(borde, bg=fondo_lienzo)
        tarjeta.pack(fill="both", expand=True, padx=1, pady=1)

        info = tk.Frame(tarjeta, bg=fondo_lienzo)
        info.pack(side="left", fill="x", expand=True, padx=14, pady=10)

        tk.Label(info, text=titulo, bg=fondo_lienzo, fg=texto_primario,
                 font=f_etiqueta).pack(anchor="w")

        texto_rol = "Propietario" if rol == "owner" else "Colaborador"
        color_rol = acento_primario if rol == "owner" else texto_slate
        tk.Label(info, text=texto_rol, bg=fondo_lienzo,
                 fg=color_rol, font=f_small).pack(anchor="w", pady=(2, 0))

        btn_abrir = tk.Button(
            tarjeta, text="Abrir →",
            command=lambda d=doc_id, t=titulo: self._abrir_documento_servidor(d, t)
        )
        estilo_primario(btn_abrir)
        btn_abrir.pack(side="right", padx=14, pady=10, ipadx=12, ipady=5)

    def _abrir_documento_servidor(self, doc_id, titulo):
        try:
            resp = requests.get(
                f"{api_base}/documents/{doc_id}",
                params={"requester_id": self.controller.usuario_id},
                timeout=5
            )
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión", "No se pudo cargar el documento.")
            return
        if resp.status_code == 200:
            datos = resp.json()
            self.controller.abrir_editor(
                doc_id, datos["title"], datos["content"], datos.get("role", "editor")
            )
        elif resp.status_code == 403:
            messagebox.showerror("Error", "No tienes acceso a este documento.")
        else:
            messagebox.showerror("Error", "No se pudo abrir el documento.")

    def _crear_archivo(self):
        titulo = "Nuevo archivo"
        payload = {"titulo": titulo, "solicitante_id": self.controller.usuario_id}
        try:
            resp = requests.post(f"{api_base}/documents", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión", "No se pudo conectar con el servidor.")
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


# Sección 15 — Frontend: editor colaborativo
# Implementa el ciclo completo: lectura → edición exclusiva → guardar → difusión → lectura.

class PantallaEditor(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=fondo_app)
        self.controller = controller

        self.documento_id = None
        self.mi_rol       = "editor"
        self.modo         = "lectura"  # "lectura" | "edicion"

        self._cooldown_restante = 0

        self._cola_ws       = queue.Queue()
        self._ws_activo     = False
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_conexion   = None

        self._fuente_editor  = tkfont.Font(family=fuente_editor[0], size=fuente_editor[1])
        self._fuente_negrita = tkfont.Font(family=fuente_editor[0], size=fuente_editor[1],
                                           weight="bold")
        self._fuente_cursiva = tkfont.Font(family=fuente_editor[0], size=fuente_editor[1],
                                           slant="italic")

        self._construir_barra_superior()
        self._construir_barra_acciones()
        self._construir_lienzo()
        self._configurar_etiquetas_formato()

    def _construir_barra_superior(self):
        barra = tk.Frame(self, bg=fondo_app)
        barra.pack(fill="x", padx=16, pady=(14, 6))

        # Título del documento — clic para renombrar
        self.etiqueta_titulo = tk.Label(
            barra, text="Documento", bg=fondo_app, fg=texto_primario,
            font=f_h2, cursor="hand2")
        self.etiqueta_titulo.pack(side="left")
        self.etiqueta_titulo.bind("<Button-1>", lambda e: self._renombrar_documento())

        self.badge_frame = tk.Frame(barra, bg=estado_lectura_fondo, padx=10, pady=4)
        self.badge_frame.pack(side="left", padx=(14, 0))
        self.badge_label = tk.Label(
            self.badge_frame, text="Modo lectura",
            bg=estado_lectura_fondo, fg=estado_lectura_texto, font=f_small)
        self.badge_label.pack()

        derecha = tk.Frame(barra, bg=fondo_app)
        derecha.pack(side="right")

        volver = crear_boton_outline(derecha, "Volver", self._al_volver)
        volver.pack(side="right", padx=(8, 0))

        self.btn_guardar_txt = crear_boton_outline(derecha, "Guardar", self._guardar_local)
        self.btn_guardar_txt.pack(side="right", padx=8)

        self.btn_colaboradores = tk.Button(
            derecha, text="Colaboradores",
            command=self._abrir_colaboradores)
        estilo_primario(self.btn_colaboradores)
        self.btn_colaboradores.pack(side="right", padx=8, ipady=5, ipadx=12)

    def _construir_barra_acciones(self):
        barra = tk.Frame(self, bg=fondo_app)
        barra.pack(fill="x", padx=16, pady=(0, 8))

        formato = tk.Frame(barra, bg=fondo_app)
        formato.pack(side="left")
        self._boton_formato(formato, "B", f_btn, self._alternar_negrita).pack(
            side="left", padx=(0, 8))
        self._boton_formato(formato, "I", f_btn_italic, self._alternar_cursiva).pack(
            side="left")

        colaboracion = tk.Frame(barra, bg=fondo_app)
        colaboracion.pack(side="right")

        # "Cancelar" — solo visible en modo edición
        self.btn_cancelar = crear_boton_outline(colaboracion, "Cancelar",
                                                self._cancelar_edicion)
        # No se empaqueta hasta entrar en modo edición

        # Botón que alterna entre "Editar" y "Guardar cambios"
        self.btn_editar_guardar = tk.Button(
            colaboracion, text="Editar",
            command=self._accion_editar_o_guardar)
        estilo_primario(self.btn_editar_guardar)
        self.btn_editar_guardar.pack(side="right", ipady=5, ipadx=14)

    def _boton_formato(self, parent, texto, fuente, comando):
        return tk.Button(
            parent, text=texto, font=fuente, command=comando,
            bg=fondo_lienzo, fg=texto_primario,
            activebackground=hover_outline, activeforeground=acento_hover,
            relief="flat", bd=1, cursor="hand2", padx=14, pady=4
        )

    def _construir_lienzo(self):
        contenedor = tk.Frame(self, bg=borde_input)
        contenedor.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.area_texto = tk.Text(
            contenedor, wrap="word", undo=True, font=self._fuente_editor,
            bg=fondo_lienzo, fg=texto_primario, insertbackground=texto_primario,
            relief="flat", bd=0, padx=12, pady=12,
            state="disabled",
        )
        self.area_texto.pack(fill="both", expand=True, padx=1, pady=1)

    def _configurar_etiquetas_formato(self):
        self.area_texto.tag_configure("negrita", font=self._fuente_negrita)
        self.area_texto.tag_configure("cursiva", font=self._fuente_cursiva)

    def cargar_documento(self, documento_id, titulo, contenido="", rol="editor"):
        if self.documento_id is not None:
            self._limpiar_ws(liberar_bloqueo=False)

        self.documento_id       = documento_id
        self.mi_rol             = rol
        self._cooldown_restante = 0

        self.etiqueta_titulo.config(text=titulo or "Documento")

        if documento_id is None:
            self.btn_editar_guardar.config(state="disabled", text="Editar")
            self.btn_colaboradores.config(state="disabled")
        else:
            self.btn_editar_guardar.config(state="normal")
            self.btn_colaboradores.config(state="normal")

        self._cargar_contenido(contenido)
        self._entrar_modo_lectura(actualizar_badge=True)

        if documento_id is not None:
            self._conectar_ws()

    def _entrar_modo_lectura(self, actualizar_badge=True, bloqueado_por=None):
        self.modo = "lectura"
        self.area_texto.config(state="disabled")

        # Ocultar "Cancelar"
        self.btn_cancelar.pack_forget()

        puede_editar = (self.documento_id is not None
                        and self._cooldown_restante <= 0
                        and bloqueado_por is None)
        self.btn_editar_guardar.config(
            state="normal" if puede_editar else "disabled",
            text="Editar"
        )
        estilo_primario(self.btn_editar_guardar)

        if actualizar_badge:
            if bloqueado_por:
                self._set_badge(f"{bloqueado_por} está editando",
                                estado_editando_fondo, estado_editando_texto)
            else:
                self._set_badge("Modo lectura", estado_lectura_fondo, estado_lectura_texto)

    def _entrar_modo_edicion(self):
        self.modo = "edicion"
        self.area_texto.config(state="normal")
        self.area_texto.focus_set()

        # Mostrar "Cancelar" a la izquierda del botón principal
        self.btn_cancelar.pack(side="right", padx=(8, 0))

        self.btn_editar_guardar.config(state="normal", text="Guardar cambios")
        estilo_primario(self.btn_editar_guardar)

        self._set_badge("Tú estás editando", estado_yo_fondo, estado_yo_texto)

    def _set_badge(self, texto, color_fondo, color_texto):
        self.badge_frame.config(bg=color_fondo)
        self.badge_label.config(text=texto, bg=color_fondo, fg=color_texto)

    def _iniciar_cooldown(self):
        self._cooldown_restante = int(segundos_cooldown)
        self._tick_cooldown()

    def _tick_cooldown(self):
        if self._cooldown_restante > 0:
            self.btn_editar_guardar.config(
                state="disabled",
                text=f"Editar ({self._cooldown_restante}s)"
            )
            self._cooldown_restante -= 1
            self.after(1000, self._tick_cooldown)
        else:
            self.btn_editar_guardar.config(text="Editar")
            if self.modo == "lectura":
                doc_libre = self.badge_label.cget("text") == "Modo lectura"
                self.btn_editar_guardar.config(
                    state="normal" if doc_libre else "disabled"
                )

    def _accion_editar_o_guardar(self):
        if self.modo == "lectura":
            self._solicitar_edicion()
        else:
            self._guardar_cambios()

    def _solicitar_edicion(self):
        if self.documento_id is None:
            return
        payload = {
            "documento_id": self.documento_id,
            "solicitante_id": self.controller.usuario_id
        }
        try:
            resp = requests.post(f"{api_base}/request_lock", json=payload, timeout=5)
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
            detalle = resp.json().get("detail", "Espera antes de volver a editar.")
            messagebox.showinfo("Cooldown activo", detalle)
        else:
            messagebox.showerror("Error", "No se pudo obtener el bloqueo de edición.")

    def _guardar_cambios(self):
        if self.documento_id is None:
            return

        # Serializar contenido con etiquetas de formato
        contenido_con_formato = self._serializar_contenido()

        payload = {
            "documento_id": self.documento_id,
            "solicitante_id": self.controller.usuario_id,
            "contenido": contenido_con_formato,
        }
        try:
            resp = requests.post(f"{api_base}/submit_changes", json=payload, timeout=10)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión", "No se pudo guardar. Intenta de nuevo.")
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

        try:
            resp = requests.get(
                f"{api_base}/documents/{self.documento_id}",
                params={"requester_id": self.controller.usuario_id},
                timeout=5
            )
            if resp.status_code == 200:
                contenido_servidor = resp.json().get("content", "")
                self._cargar_contenido(contenido_servidor)
        except requests.exceptions.RequestException:
            pass

        payload = {
            "documento_id": self.documento_id,
            "solicitante_id": self.controller.usuario_id
        }
        try:
            requests.post(f"{api_base}/release_lock", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            pass

        self._entrar_modo_lectura(actualizar_badge=True)

    def _renombrar_documento(self):
        if self.documento_id is None:
            return
        titulo_actual = self.etiqueta_titulo.cget("text")
        nuevo_titulo = simpledialog.askstring(
            "Renombrar documento", "Nuevo nombre:",
            initialvalue=titulo_actual, parent=self)
        if nuevo_titulo and nuevo_titulo.strip():
            nuevo_titulo = nuevo_titulo.strip()
            # Actualizar título en servidor si el documento existe
            # (por ahora solo actualiza la etiqueta localmente; el backend no tiene endpoint de renombrar)
            self.etiqueta_titulo.config(text=nuevo_titulo)

    def _abrir_colaboradores(self):
        if self.documento_id is None:
            return
        VentanaColaboradores(self, self.controller, self.documento_id, self.mi_rol)

    def _al_volver(self):
        if self.modo == "edicion":
            respuesta = messagebox.askyesnocancel(
                "Edición activa",
                "Tienes cambios sin guardar.\n\n¿Guardar cambios antes de salir?"
            )
            if respuesta is None:
                return
            if respuesta:
                self._guardar_cambios()
                if self.modo == "edicion":
                    return
            else:
                payload = {
                    "documento_id": self.documento_id,
                    "solicitante_id": self.controller.usuario_id
                }
                try:
                    requests.post(f"{api_base}/release_lock", json=payload, timeout=3)
                except requests.exceptions.RequestException:
                    pass

        self._limpiar_ws(liberar_bloqueo=False)
        self.controller.mostrar("PantallaTablero")

    def _conectar_ws(self):
        try:
            import websockets as _ws_lib
        except ImportError:
            return

        self._ws_activo = True
        url = (f"{self.controller.get_ws_base()}"
               f"/ws/{self.documento_id}/{self.controller.usuario_id}")

        async def _escuchar():
            while self._ws_activo:
                try:
                    async with _ws_lib.connect(url, ping_interval=20) as ws:
                        self._ws_conexion = ws
                        async for mensaje in ws:
                            if not self._ws_activo:
                                return
                            self._cola_ws.put(mensaje)
                except Exception:
                    pass
                finally:
                    self._ws_conexion = None
                if self._ws_activo:
                    await asyncio.sleep(2)

        def _correr():
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._ws_loop = loop
            try:
                loop.run_until_complete(_escuchar())
            finally:
                loop.close()
                self._ws_loop = None

        self._ws_hilo = threading.Thread(target=_correr, daemon=True)
        self._ws_hilo.start()
        self._sondear_cola_ws()

    def _sondear_cola_ws(self):
        if not self._ws_activo:
            return
        while True:
            try:
                msg = self._cola_ws.get_nowait()
                self._al_recibir_ws(msg)
            except queue.Empty:
                break
        self.after(150, self._sondear_cola_ws)

    def _al_recibir_ws(self, msg_str):
        try:
            datos = json.loads(msg_str)
        except (json.JSONDecodeError, TypeError):
            return

        tipo = datos.get("type")

        if tipo == "content_update":
            if self.modo == "lectura":
                self._cargar_contenido(datos.get("content", ""))

        elif tipo == "lock_update":
            bloqueado_por_id   = datos.get("locked_by_id")
            bloqueado_por_nombre = datos.get("locked_by_name")

            if bloqueado_por_id is None:
                if self.modo == "lectura":
                    self._set_badge("Modo lectura", estado_lectura_fondo, estado_lectura_texto)
                    if self._cooldown_restante <= 0 and self.documento_id is not None:
                        self.btn_editar_guardar.config(state="normal", text="Editar")
            elif bloqueado_por_id == self.controller.usuario_id:
                if self.modo != "edicion":
                    self._entrar_modo_edicion()
            else:
                if self.modo == "lectura":
                    self._set_badge(
                        f"{bloqueado_por_nombre} está editando",
                        estado_editando_fondo, estado_editando_texto
                    )
                    self.btn_editar_guardar.config(state="disabled")

        elif tipo == "access_revoked":
            self._limpiar_ws(liberar_bloqueo=False)
            self.controller.mostrar("PantallaTablero")
            messagebox.showwarning(
                "Acceso revocado",
                "El propietario ha revocado tu acceso a este documento."
            )

    def _limpiar_ws(self, liberar_bloqueo: bool):
        self._ws_activo = False

        if liberar_bloqueo and self.modo == "edicion" and self.documento_id is not None:
            try:
                requests.post(
                    f"{api_base}/release_lock",
                    json={
                        "documento_id": self.documento_id,
                        "solicitante_id": self.controller.usuario_id
                    },
                    timeout=3
                )
            except requests.exceptions.RequestException:
                pass

        if self._ws_loop is not None and self._ws_conexion is not None:
            try:
                self._ws_loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        self._ws_conexion.close(),
                        loop=self._ws_loop
                    )
                )
            except RuntimeError:
                pass

        self.documento_id = None
        self.modo = "lectura"

    def _cargar_contenido(self, contenido):
        """Carga el contenido del documento en el lienzo, aplicando formato si corresponde."""
        estado_previo = self.area_texto.cget("state")
        self.area_texto.config(state="normal")
        self.area_texto.tag_remove("negrita", "1.0", "end")
        self.area_texto.tag_remove("cursiva", "1.0", "end")
        self.area_texto.delete("1.0", "end")
        if contenido:
            # Si el contenido incluye marcadores de formato, deserializarlo
            if "\x00" in contenido:
                self._deserializar_contenido(contenido)
            else:
                self.area_texto.insert("1.0", contenido)
        self.area_texto.edit_reset()
        self.area_texto.config(state=estado_previo)

    def _serializar_contenido(self) -> str:
        """Convierte el texto y sus etiquetas de formato a una cadena serializada.

        Formato: texto plano con bloques de formato codificados como
        \x00TAG_INICIO:pos\x00 y \x00TAG_FIN:pos\x00 intercalados.
        Si no hay formato, devuelve el texto plano directamente.
        """
        texto = self.area_texto.get("1.0", "end-1c")
        rangos_negrita  = self._obtener_rangos_etiqueta("negrita")
        rangos_cursiva  = self._obtener_rangos_etiqueta("cursiva")

        if not rangos_negrita and not rangos_cursiva:
            return texto

        # Construir lista de eventos (inicio/fin de etiqueta por índice de carácter)
        eventos = []
        for inicio, fin in rangos_negrita:
            eventos.append((inicio, "NEGRITA_ON"))
            eventos.append((fin,   "NEGRITA_OFF"))
        for inicio, fin in rangos_cursiva:
            eventos.append((inicio, "CURSIVA_ON"))
            eventos.append((fin,   "CURSIVA_OFF"))
        eventos.sort(key=lambda x: x[0])

        resultado = []
        cursor_pos = 0
        for pos, etiqueta in eventos:
            resultado.append(texto[cursor_pos:pos])
            resultado.append(f"\x00{etiqueta}\x00")
            cursor_pos = pos
        resultado.append(texto[cursor_pos:])
        return "".join(resultado)

    def _obtener_rangos_etiqueta(self, nombre_etiqueta) -> list:
        """Devuelve lista de (inicio_int, fin_int) en índices de carácter para una etiqueta."""
        rangos = []
        inicio = "1.0"
        while True:
            inicio = self.area_texto.tag_nextrange(nombre_etiqueta, inicio, "end")
            if not inicio:
                break
            i_str, f_str = inicio[0], inicio[1]
            i_idx = self._tk_index_a_int(i_str)
            f_idx = self._tk_index_a_int(f_str)
            rangos.append((i_idx, f_idx))
            inicio = f_str
        return rangos

    def _tk_index_a_int(self, index_str: str) -> int:
        """Convierte un índice tkinter 'línea.col' a índice de carácter absoluto."""
        linea, col = map(int, index_str.split("."))
        lineas = self.area_texto.get("1.0", "end-1c").split("\n")
        total = sum(len(lineas[i]) + 1 for i in range(linea - 1))
        return total + col

    def _int_a_tk_index(self, pos: int) -> str:
        """Convierte un índice de carácter absoluto a índice tkinter 'línea.col'."""
        texto = self.area_texto.get("1.0", "end-1c")
        linea = texto[:pos].count("\n") + 1
        col   = pos - texto[:pos].rfind("\n") - 1
        return f"{linea}.{col}"

    def _deserializar_contenido(self, contenido: str):
        """Inserta texto y aplica etiquetas de formato a partir del contenido serializado."""
        partes = contenido.split("\x00")
        texto_puro = []
        eventos = []
        pos_actual = 0

        for i, parte in enumerate(partes):
            if parte in ("NEGRITA_ON", "NEGRITA_OFF", "CURSIVA_ON", "CURSIVA_OFF"):
                eventos.append((pos_actual, parte))
            else:
                texto_puro.append(parte)
                pos_actual += len(parte)

        texto = "".join(texto_puro)
        self.area_texto.insert("1.0", texto)

        # Aplicar etiquetas
        pilas = {"NEGRITA": [], "CURSIVA": []}
        for pos, evento in eventos:
            tag_nombre = "negrita" if "NEGRITA" in evento else "cursiva"
            clave = "NEGRITA" if "NEGRITA" in evento else "CURSIVA"
            if evento.endswith("_ON"):
                pilas[clave].append(pos)
            elif evento.endswith("_OFF") and pilas[clave]:
                inicio = pilas[clave].pop()
                self.area_texto.tag_add(
                    tag_nombre,
                    self._int_a_tk_index(inicio),
                    self._int_a_tk_index(pos)
                )

    def _alternar_negrita(self):
        self._alternar_etiqueta("negrita")

    def _alternar_cursiva(self):
        self._alternar_etiqueta("cursiva")

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

    def _guardar_local(self):
        titulo_actual = self.etiqueta_titulo.cget("text")
        nombre_sugerido = titulo_actual if titulo_actual else "documento"
        # Eliminar caracteres inválidos para nombres de archivo
        nombre_sugerido = re.sub(r'[\\/*?:"<>|]', "", nombre_sugerido)

        ruta = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=nombre_sugerido,
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


# Sección 16 — Frontend: ventana de colaboradores

class VentanaColaboradores(tk.Toplevel):
    def __init__(self, parent_frame, controller, documento_id, mi_rol):
        super().__init__(parent_frame)
        self.controller   = controller
        self.documento_id = documento_id
        self.mi_rol       = mi_rol

        self.title("Colaboradores — CoopDoc")
        self.configure(bg=fondo_app)
        self.resizable(False, False)
        self.geometry("480x520")

        self.transient(parent_frame)
        self.grab_set()

        self._construir()
        self._cargar_colaboradores()

    def _construir(self):
        cabecera = tk.Frame(self, bg=fondo_app, padx=24, pady=18)
        cabecera.pack(fill="x")

        tk.Label(cabecera, text="Colaboradores", bg=fondo_app,
                 fg=texto_primario, font=f_h2).pack(side="left")

        tk.Button(cabecera, text="Cerrar", command=self.destroy,
                  bg=fondo_app, fg=texto_slate, font=f_small,
                  relief="flat", bd=0, cursor="hand2").pack(side="right")

        tk.Frame(self, bg=divisor, height=1).pack(fill="x", padx=24)

        contenedor_scroll = tk.Frame(self, bg=fondo_app)
        contenedor_scroll.pack(fill="both", expand=True, padx=24, pady=(12, 0))

        sb = tk.Scrollbar(contenedor_scroll)
        sb.pack(side="right", fill="y")

        self._canvas = tk.Canvas(contenedor_scroll, bg=fondo_app,
                                 highlightthickness=0, yscrollcommand=sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb.config(command=self._canvas.yview)

        self._lista = tk.Frame(self._canvas, bg=fondo_app)
        self._cw = self._canvas.create_window((0, 0), window=self._lista, anchor="nw")

        self._lista.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfig(self._cw, width=e.width))

        if self.mi_rol == 'owner':
            tk.Frame(self, bg=divisor, height=1).pack(fill="x", padx=24, pady=(8, 0))

            frame_invitar = tk.Frame(self, bg=fondo_app, padx=24, pady=14)
            frame_invitar.pack(fill="x")

            tk.Label(frame_invitar, text="Invitar por correo electrónico", bg=fondo_app,
                     fg=texto_primario, font=f_etiqueta).pack(anchor="w", pady=(0, 8))

            fila = tk.Frame(frame_invitar, bg=fondo_app)
            fila.pack(fill="x")

            borde, self.entrada_invitar, _ = crear_campo(fila, "correo@ejemplo.com")
            borde.pack(side="left", fill="x", expand=True)

            btn_invitar = tk.Button(fila, text="Invitar", command=self._invitar)
            estilo_primario(btn_invitar)
            btn_invitar.pack(side="left", padx=(10, 0), ipady=5, ipadx=16)

    def _cargar_colaboradores(self):
        for w in self._lista.winfo_children():
            w.destroy()

        try:
            resp = requests.get(
                f"{api_base}/documents/{self.documento_id}/collaborators",
                params={"requester_id": self.controller.usuario_id},
                timeout=5
            )
        except requests.exceptions.RequestException:
            tk.Label(self._lista, text="Error al cargar colaboradores.",
                     bg=fondo_app, fg=texto_slate, font=f_input).pack(pady=12)
            return

        if resp.status_code != 200:
            tk.Label(self._lista, text="No se pudo obtener la lista.",
                     bg=fondo_app, fg=texto_slate, font=f_input).pack(pady=12)
            return

        colaboradores = resp.json().get("collaborators", [])

        if not colaboradores:
            tk.Label(self._lista, text="Aún no hay colaboradores en este documento.",
                     bg=fondo_app, fg=texto_slate, font=f_input).pack(pady=12)
            return

        for colab in colaboradores:
            self._agregar_fila_colaborador(colab)

    def _agregar_fila_colaborador(self, colab):
        borde = tk.Frame(self._lista, bg=borde_input)
        borde.pack(fill="x", pady=3)

        fila = tk.Frame(borde, bg=fondo_lienzo)
        fila.pack(fill="both", expand=True, padx=1, pady=1)

        info = tk.Frame(fila, bg=fondo_lienzo, padx=12, pady=8)
        info.pack(side="left", fill="x", expand=True)

        tk.Label(info, text=colab["name"], bg=fondo_lienzo,
                 fg=texto_primario, font=f_etiqueta).pack(anchor="w")

        texto_rol = "Propietario" if colab["role"] == "owner" else "Colaborador"
        subtexto  = f"{colab['email']}  ·  {texto_rol}"
        tk.Label(info, text=subtexto, bg=fondo_lienzo,
                 fg=texto_slate, font=f_small).pack(anchor="w", pady=(2, 0))

        if (self.mi_rol == 'owner'
                and colab["id"] != self.controller.usuario_id
                and colab["role"] != 'owner'):
            btn_revocar = tk.Button(
                fila, text="Revocar",
                command=lambda uid=colab["id"], nom=colab["name"]: self._revocar(uid, nom),
                bg=fondo_lienzo, fg=acento_hover,
                activebackground=hover_outline, activeforeground=acento_hover,
                relief="flat", font=f_small, cursor="hand2", bd=1,
                padx=10, pady=6
            )
            btn_revocar.pack(side="right", padx=10, pady=8)

    def _invitar(self):
        correo = self.entrada_invitar.valor().strip()
        if not correo:
            messagebox.showerror("Error", "Ingresa un correo.", parent=self)
            return

        payload = {
            "documento_id": self.documento_id,
            "solicitante_id": self.controller.usuario_id,
            "correo_invitado": correo,
        }
        try:
            resp = requests.post(f"{api_base}/invite", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión", "No se pudo conectar.", parent=self)
            return

        if resp.status_code == 200:
            messagebox.showinfo("Invitar", f"Se invitó a {correo} correctamente.", parent=self)
            self.entrada_invitar.resetear()
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

    def _revocar(self, usuario_objetivo_id, nombre):
        if not messagebox.askyesno(
                "Revocar acceso",
                f"¿Estás seguro de que quieres revocar el acceso de {nombre}?",
                parent=self):
            return

        payload = {
            "documento_id": self.documento_id,
            "solicitante_id": self.controller.usuario_id,
            "usuario_objetivo_id": usuario_objetivo_id,
        }
        try:
            resp = requests.delete(f"{api_base}/revoke", json=payload, timeout=5)
        except requests.exceptions.RequestException:
            messagebox.showerror("Error de conexión", "No se pudo conectar.", parent=self)
            return

        if resp.status_code == 200:
            messagebox.showinfo("Revocar", f"Se revocó el acceso de {nombre}.", parent=self)
            self._cargar_colaboradores()
        elif resp.status_code == 403:
            messagebox.showerror("Error",
                                 "No tienes permiso para realizar esta acción.", parent=self)
        else:
            messagebox.showerror("Error", "No se pudo revocar. Intenta de nuevo.", parent=self)


# Sección 17 — Frontend: diálogo de inicio (modo anfitrión / cliente)

class DialogoInicio(tk.Tk):

    def __init__(self):
        super().__init__()
        self.modo_resultado = None  # "host" | "client"
        self.url_resultado  = None

        self.title("CoopDoc")
        self.resizable(False, False)
        self.configure(bg=fondo_app)

        inicializar_fuentes()
        self._construir()
        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)

        self.update_idletasks()
        w, h = 1024, 768
        x = (self.winfo_screenwidth()  - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _construir(self):
        # Contenedor centrado verticalmente
        contenedor = tk.Frame(self, bg=fondo_app)
        contenedor.place(relx=0.5, rely=0.5, anchor="center", width=400)

        dibujar_logo(contenedor, size=64, bg=fondo_app).pack(pady=(0, 10))

        tk.Label(contenedor, text="CoopDoc", bg=fondo_app, fg=texto_primario,
                 font=f_titulo).pack()
        tk.Label(contenedor, text="Editor de Texto Colaborativo", bg=fondo_app,
                 fg=texto_slate, font=f_promo_desc).pack(pady=(4, 6))

        tk.Frame(contenedor, bg=acento_primario, height=3, width=50).pack(pady=(0, 20))

        tk.Label(contenedor, text="¿Cómo deseas conectarte?", bg=fondo_app,
                 fg=texto_primario, font=f_etiqueta).pack(pady=(0, 14))

        btn_anfitrion = tk.Button(contenedor, text="Iniciar como anfitrión",
                                  command=self._seleccionar_anfitrion)
        estilo_primario(btn_anfitrion)
        btn_anfitrion.pack(ipady=10, pady=(0, 10))

        crear_boton_outline(contenedor, "Conectarme a un anfitrión",
                            self._seleccionar_cliente).pack()

    def _seleccionar_anfitrion(self):
        self.modo_resultado = "host"
        self.destroy()

    def _seleccionar_cliente(self):
        url = simpledialog.askstring(
            "Conectar con anfitrión",
            "Ingresa la URL del anfitrión\n(ej. https://xxxx.ngrok-free.app):",
            parent=self
        )
        if url and url.strip():
            self.url_resultado  = url.strip().rstrip("/")
            self.modo_resultado = "client"
            self.destroy()

    def _al_cerrar(self):
        self.modo_resultado = None
        self.destroy()


# Sección 18 — Punto de entrada

if __name__ == "__main__":
    dialogo = DialogoInicio()
    dialogo.mainloop()

    if dialogo.modo_resultado is None:
        sys.exit(0)

    if dialogo.modo_resultado == "host":
        iniciar_backend()
        if not esperar_servidor(timeout=15):
            print("Advertencia: el servidor no respondió a tiempo.")
    else:
        api_base = dialogo.url_resultado  # type: ignore[assignment]

    App().mainloop()