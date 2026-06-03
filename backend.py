# ==============================================================================
# Backend Architecture: Collaborative Desktop Text Editor
# Stack: FastAPI, SQLite, Pydantic, WebSockets
# Description: Handles user authentication, strict data validation, file 
# access permissions, and real-time concurrent text editing synchronization.
# ==============================================================================

import sqlite3
import re
import hashlib
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, field_validator, model_validator

# ==============================================================================
# 1. CORE SETUP & DATABASE INITIALIZATION
# ==============================================================================

app = FastAPI(title="Collaborative Text Editor API")

# Connect to local SQLite file. 
# check_same_thread=False is used because FastAPI handles concurrent requests 
# in multiple threads, but SQLite by default restricts connection objects to the 
# thread that created them. (OS/Concurrency concept applied).
DB_FILE = "editor_backend.db"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Allows accessing columns by name
    return conn

def initialize_database():
    """Creates the necessary tables if they do not exist."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    
    # Documents table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            owner_id INTEGER NOT NULL,
            FOREIGN KEY(owner_id) REFERENCES users(id)
        )
    ''')
    
    # Access Permissions table (Maps users to documents they can edit)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL, -- 'owner' or 'editor'
            FOREIGN KEY(document_id) REFERENCES documents(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(document_id, user_id)
        )
    ''')
    
    conn.commit()
    conn.close()

# Run DB initialization on startup
initialize_database()


# ==============================================================================
# 2. PYDANTIC MODELS & STRICT VALIDATION RULES
# ==============================================================================

class UserRegister(BaseModel):
    name: str
    email: str
    password: str
    confirm_password: str

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str):
        # Alphabetical letters only. No numbers or special characters.
        if not re.match(r"^[A-Za-z]+$", value):
            raise ValueError("Name must contain only alphabetical letters.")
        return value

    @field_validator('email')
    @classmethod
    def validate_email(cls, value: str):
        # Exactly one @ symbol
        if value.count('@') != 1:
            raise ValueError("Email must contain exactly one @ symbol.")
        # No spaces or backslashes
        if re.search(r'[\s\\]', value):
            raise ValueError("Email cannot contain spaces or backslashes.")
        # No consecutive dots
        if '..' in value:
            raise ValueError("Email cannot contain consecutive dots.")
        # No leading or trailing dots, and no dots immediately next to @
        if value.startswith('.') or value.endswith('.') or '@.' in value or '.@' in value:
            raise ValueError("Email cannot have leading/trailing dots or dots next to @.")
        # Basic latin character enforcement for email structure
        if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", value):
            raise ValueError("Email contains invalid characters or structure.")
        return value

    @field_validator('password')
    @classmethod
    def validate_password(cls, value: str):
        # Minimum 8 chars, 1 UC, 1 LC, 1 Num, 1 Special
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
    requester_id: int  # In a real app, this comes from a secure session token

class InviteUser(BaseModel):
    document_id: int
    requester_id: int  # The person sending the invite
    invitee_email: str

class RevokeAccess(BaseModel):
    document_id: int
    requester_id: int  # Must be the owner
    target_user_id: int


# ==============================================================================
# 3. UTILITY FUNCTIONS
# ==============================================================================

def hash_password(password: str) -> str:
    # Basic SHA-256 hashing. (In a production senior-level app, I would use 
    # bcrypt/passlib with salt, but hashlib is built-in and fits OS learning).
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def check_permission(doc_id: int, user_id: int) -> Optional[str]:
    """Returns the role ('owner' or 'editor') if user has access, else None."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM permissions WHERE document_id=? AND user_id=?", (doc_id, user_id))
    row = cursor.fetchone()
    conn.close()
    return row['role'] if row else None


# ==============================================================================
# 4. HTTP ENDPOINTS: AUTHENTICATION
# ==============================================================================

@app.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(user: UserRegister):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if email already exists
    cursor.execute("SELECT id FROM users WHERE email=?", (user.email,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered.")
        
    hashed_pw = hash_password(user.password)
    
    try:
        cursor.execute(
            "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
            (user.name, user.email, hashed_pw)
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Database error occurred.")
    finally:
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
        
    # Returning user ID as a simplistic "token" for the frontend to use in subsequent requests
    return {"message": "Login successful", "user_id": user['id'], "name": user['name']}


# ==============================================================================
# 5. HTTP ENDPOINTS: FILE MANAGEMENT & ACCESS
# ==============================================================================

@app.post("/documents")
def create_document(doc: DocumentCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create the document
    cursor.execute(
        "INSERT INTO documents (title, content, owner_id) VALUES (?, '', ?)",
        (doc.title, doc.requester_id)
    )
    doc_id = cursor.lastrowid
    
    # Set the creator as the 'owner' in permissions
    cursor.execute(
        "INSERT INTO permissions (document_id, user_id, role) VALUES (?, ?, 'owner')",
        (doc_id, doc.requester_id)
    )
    conn.commit()
    conn.close()
    
    return {"message": "Document created", "document_id": doc_id}

@app.get("/documents/{doc_id}")
def get_document(doc_id: int, requester_id: int):
    # Security check: Does user have permission?
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
    # Rule: Any user with current access can invite others.
    if not check_permission(invite.document_id, invite.requester_id):
        raise HTTPException(status_code=403, detail="You do not have access to this document.")
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Find invitee by email
    cursor.execute("SELECT id FROM users WHERE email=?", (invite.invitee_email,))
    invitee = cursor.fetchone()
    if not invitee:
        conn.close()
        raise HTTPException(status_code=404, detail="Invitee email not found.")
        
    invitee_id = invitee['id']
    
    # Add permission
    try:
        cursor.execute(
            "INSERT INTO permissions (document_id, user_id, role) VALUES (?, ?, 'editor')",
            (invite.document_id, invitee_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Ignore if they already have access
        pass 
    finally:
        conn.close()
        
    return {"message": f"User {invite.invitee_email} invited successfully."}

@app.delete("/revoke")
def revoke_access(revoke: RevokeAccess):
    # STRICT RULE: Only the file 'Owner' can revoke access
    role = check_permission(revoke.document_id, revoke.requester_id)
    if role != 'owner':
        raise HTTPException(status_code=403, detail="Only the document owner can revoke access.")
        
    # Prevent owner from revoking themselves
    if revoke.requester_id == revoke.target_user_id:
        raise HTTPException(status_code=400, detail="Owner cannot revoke their own access.")
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM permissions WHERE document_id=? AND user_id=?", 
        (revoke.document_id, revoke.target_user_id)
    )
    conn.commit()
    conn.close()
    
    return {"message": "User access revoked."}


# ==============================================================================
# 6. WEBSOCKET: REAL-TIME COLLABORATION
# ==============================================================================

class ConnectionManager:
    """
    Manages active WebSocket connections to handle concurrent users safely.
    Maintains a dictionary mapping document IDs to a list of active WebSockets.
    """
    def __init__(self):
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, doc_id: int):
        await websocket.accept()
        if doc_id not in self.active_connections:
            self.active_connections[doc_id] = []
        self.active_connections[doc_id].append(websocket)

    def disconnect(self, websocket: WebSocket, doc_id: int):
        if doc_id in self.active_connections:
            self.active_connections[doc_id].remove(websocket)
            if not self.active_connections[doc_id]:
                del self.active_connections[doc_id]

    async def broadcast_text_update(self, doc_id: int, new_content: str, sender: WebSocket):
        """
        Concurrency resolution (Basic): When one user types, we broadcast the 
        new full text state to all OTHER users currently viewing the document.
        (Last Write Wins methodology).
        """
        if doc_id in self.active_connections:
            for connection in self.active_connections[doc_id]:
                # Send the update to everyone EXCEPT the person who just typed it
                if connection != sender:
                    await connection.send_text(new_content)

manager = ConnectionManager()

@app.websocket("/ws/{doc_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, doc_id: int, user_id: int):
    # 1. Verify user has access before allowing WebSocket connection
    if not check_permission(doc_id, user_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
        
    # 2. Accept and manage connection
    await manager.connect(websocket, doc_id)
    
    try:
        while True:
            # Wait for text updates from the tkinter client
            new_content = await websocket.receive_text()
            
            # 3. Persist the change to the SQLite database
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE documents SET content=? WHERE id=?", 
                (new_content, doc_id)
            )
            conn.commit()
            conn.close()
            
            # 4. Broadcast the change to other active collaborators
            await manager.broadcast_text_update(doc_id, new_content, sender=websocket)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket, doc_id)