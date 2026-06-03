import tkinter as tk
from tkinter import messagebox

# =============================================================================
# Frontend Architecture: Collaborative Desktop Text Editor
# Component: Login UI (Standalone Mock)
# Description: This file is fully isolated. It contains no backend logic,
# no database connections, and serves purely for visual UI/UX testing.
# =============================================================================

def handle_login_mock():
    """
    FUNCIONALIDAD MOCK:
    Esta función simula el envío del formulario. Ignora a propósito los 
    inputs y omite la validación para cumplir con el requisito de 
    mostrar inmediatamente un estado de éxito codificado (hardcoded).
    """
    # En un escenario real, obtendríamos los datos así:
    # email = entry_email.get()
    # password = entry_password.get()
    
    # ESTADO DE ÉXITO:
    messagebox.showinfo("Éxito", "Hola de nuevo!")

# 1. Configuración de la Ventana Principal
# -----------------------------------------------------------------------------
window = tk.Tk()
window.title("Editor de Texto Colaborativo - Iniciar Sesión")
window.geometry("500x600")
window.configure(bg="black") # Actualizado para coincidir con registro_ui.py
window.resizable(False, False)

# 2. Contenedor Principal (Frame)
# -----------------------------------------------------------------------------
main_frame = tk.Frame(window, bg="black")
main_frame.pack(expand=True)

# 3. Configuración de Widgets
# -----------------------------------------------------------------------------
# Etiqueta de Título
label_title = tk.Label(
    main_frame, 
    text="Iniciar Sesión", 
    fg="white", 
    bg="black", 
    font=("Arial", 24, "bold")
)
label_title.pack(pady=(0, 40))

# Etiqueta de Correo
label_email = tk.Label(
    main_frame, 
    text="Correo Electrónico:", 
    fg="white", 
    bg="black", 
    font=("Arial", 11)
)
label_email.pack(anchor="w", pady=(0, 5))

# Campo de Entrada de Correo
entry_email = tk.Entry(
    main_frame, 
    width=28, 
    font=("Arial", 12), 
    bg="#d9d9d9", 
    fg="black"
)
entry_email.pack(pady=(0, 20), ipady=5)

# Etiqueta de Contraseña
label_password = tk.Label(
    main_frame, 
    text="Contraseña:", 
    fg="white", 
    bg="black", 
    font=("Arial", 11)
)
label_password.pack(anchor="w", pady=(0, 5))

# Campo de Entrada de Contraseña (Oculto)
entry_password = tk.Entry(
    main_frame, 
    width=28, 
    font=("Arial", 12), 
    bg="#d9d9d9", 
    fg="black",
    show="*"
)
entry_password.pack(pady=(0, 40), ipady=5)

# Botón de Inicio de Sesión
btn_login = tk.Button(
    main_frame, 
    text="Entrar", 
    font=("Arial", 12, "bold"),
    bg="#4CAF50",
    fg="white",
    activebackground="#45a049",
    activeforeground="white",
    relief="flat",
    width=20,
    command=handle_login_mock
)
btn_login.pack(ipady=8)

# =============================================================================
# 4. Iniciar la Aplicación
# =============================================================================
window.mainloop()