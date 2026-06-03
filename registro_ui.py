import tkinter as tk
from tkinter import messagebox
import sqlite3
import threading

conexion = sqlite3.connect("usuarios.db")
cursor = conexion.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS usuarios(

    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT,
    fecha TEXT,
    correo TEXT UNIQUE,
    password TEXT
)
""")

conexion.commit()
conexion.close()


def formatear_fecha(event):

    texto = entry_fecha.get()

    if len(texto) == 2 and "/" not in texto:
        entry_fecha.insert(tk.END, "/")

    if len(texto) == 5 and texto.count("/") == 1:
        entry_fecha.insert(tk.END, "/")


def registrar_usuario():

    nombre = entry_nombre.get()
    fecha = entry_fecha.get()
    correo = entry_correo.get()
    password = entry_password.get()
    verificar = entry_verificar.get()

    if nombre == "" or fecha == "" or correo == "" or password == "" or verificar == "":
        messagebox.showerror("Error", "Completa todos los campos")
        return

    for caracter in nombre:

        if not(caracter.isalpha() or caracter.isspace()):

            messagebox.showerror(
                "Error",
                "El nombre solo puede contener letras"
            )

            return

    if "@" not in correo or ".com" not in correo:

        messagebox.showerror(
            "Error",
            "Correo incorrecto"
        )

        return

    if password != verificar:

        messagebox.showerror(
            "Error",
            "Las contraseñas no coinciden"
        )

        return

    try:

        conexion = sqlite3.connect("usuarios.db")
        cursor = conexion.cursor()

        cursor.execute("""

        INSERT INTO usuarios(nombre, fecha, correo, password)

        VALUES (?, ?, ?, ?)

        """, (nombre, fecha, correo, password))

        conexion.commit()
        conexion.close()

        messagebox.showinfo(
            "Registro",
            "Usuario registrado correctamente"
        )

        limpiar_campos()

    except sqlite3.IntegrityError:

        messagebox.showerror(
            "Error",
            "Ese correo ya está registrado"
        )


def iniciar_hilo():

    hilo = threading.Thread(target=registrar_usuario)

    hilo.start()


def limpiar_campos():

    entry_nombre.delete(0, tk.END)
    entry_fecha.delete(0, tk.END)
    entry_correo.delete(0, tk.END)
    entry_password.delete(0, tk.END)
    entry_verificar.delete(0, tk.END)


ventana = tk.Tk()

ventana.title("CoopDoc")

ventana.geometry("950x580")

ventana.config(bg="#d9d9d9")

ventana.resizable(False, False)


frame = tk.Frame(ventana, bg="black")

frame.place(x=60, y=60, width=880, height=520)


titulo = tk.Label(

    frame,

    text="Únete junto a tus amigos a CoopDoc",

    fg="#00e5ff",

    bg="black",

    font=("Arial", 26)
)

titulo.place(x=180, y=30)


descripcion = tk.Label(

    frame,

    text="Usando CoopDoc, termina de volada tus trabajos grupales.\nPara ello, crea una cuenta nueva con nosotros",

    fg="#00e5ff",

    bg="black",

    font=("Arial", 14),

    justify="center"
)

descripcion.place(x=20, y=270)


color = "#00e5ff"


label_nombre = tk.Label(
    frame,
    text="Ingresa tu nombre y apellidos:",
    fg=color,
    bg="black",
    font=("Arial", 11)
)

label_nombre.place(x=570, y=150)


label_fecha = tk.Label(
    frame,
    text="Ingresa tu fecha de nacimiento:",
    fg=color,
    bg="black",
    font=("Arial", 11)
)

label_fecha.place(x=560, y=210)

label_correo = tk.Label(
    frame,
    text="Ingresa tu correo de verificación:",
    fg=color,
    bg="black",
    font=("Arial", 11)
)

label_correo.place(x=540, y=270)


label_password = tk.Label(
    frame,
    text="Ingresa tu contraseña:",
    fg=color,
    bg="black",
    font=("Arial", 11)
)

label_password.place(x=590, y=330)


label_verificar = tk.Label(
    frame,
    text="Verifica tu contraseña:",
    fg=color,
    bg="black",
    font=("Arial", 11)
)

label_verificar.place(x=600, y=390)


entry_nombre = tk.Entry(
    frame,
    width=28,
    font=("Arial", 12),
    bg="#d9d9d9"
)

entry_nombre.place(x=580, y=180)


entry_fecha = tk.Entry(
    frame,
    width=28,
    font=("Arial", 12),
    bg="#d9d9d9"
)

entry_fecha.place(x=580, y=240)

entry_fecha.bind("<KeyRelease>", formatear_fecha)


entry_correo = tk.Entry(
    frame,
    width=28,
    font=("Arial", 12),
    bg="#d9d9d9"
)

entry_correo.place(x=580, y=300)


entry_password = tk.Entry(
    frame,
    width=28,
    font=("Arial", 12),
    bg="#d9d9d9",
    show="*"
)

entry_password.place(x=580, y=360)


entry_verificar = tk.Entry(
    frame,
    width=28,
    font=("Arial", 12),
    bg="#d9d9d9",
    show="*"
)

entry_verificar.place(x=580, y=420)


boton = tk.Button(

    frame,

    text="Registrarse",

    bg="#00e5ff",

    fg="black",

    font=("Arial", 12, "bold"),

    width=18,

    command=iniciar_hilo
)

boton.place(x=620, y=470)


ventana.mainloop()