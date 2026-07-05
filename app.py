from flask import Flask, jsonify, request, render_template, redirect, Response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from functools import wraps
from google.oauth2 import service_account
from googleapiclient.discovery import build
import http.client
import json
import time
import os
import pytz

app = Flask(__name__)

# ─── Configuración de base de datos con disco persistente ───────────────────
DISK_PATH = '/var/data'
if os.path.isdir(DISK_PATH):
    DB_PATH = os.path.join(DISK_PATH, 'metapython.db')
else:
    DB_PATH = 'metapython.db'

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Log(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha_y_hora = db.Column(db.DateTime, default=datetime.utcnow)
    texto = db.Column(db.Text)

class EstadoUsuario(db.Model):
    numero = db.Column(db.String, primary_key=True)
    estado = db.Column(db.String)
    nombre = db.Column(db.String, nullable=True)
    desde = db.Column(db.DateTime, default=datetime.utcnow)

class Cita(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String, nullable=False)
    nombre = db.Column(db.String, nullable=True)
    estado = db.Column(db.String, default="pendiente")
    fecha_cita = db.Column(db.String, nullable=True)
    hora_cita = db.Column(db.String, nullable=True)
    recordatorio_enviado = db.Column(db.Boolean, default=False)
    google_event_id = db.Column(db.String, nullable=True)
    creada_en = db.Column(db.DateTime, default=datetime.utcnow)

class Llamada(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String, nullable=False)
    nombre = db.Column(db.String, nullable=True)
    estado = db.Column(db.String, default="pendiente")
    creada_en = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()
    # Migración: si la base de datos ya existía de antes (con citas reales
    # guardadas en el disco), le agrega la columna nueva sin borrar nada.
    inspector = db.inspect(db.engine)
    columnas_cita = [col['name'] for col in inspector.get_columns('cita')]
    if 'google_event_id' not in columnas_cita:
        with db.engine.connect() as conexion:
            conexion.execute(db.text('ALTER TABLE cita ADD COLUMN google_event_id VARCHAR'))
            conexion.commit()

def ordenar_por_fecha_y_hora(registros):
    return sorted(registros, key=lambda x: x.fecha_y_hora, reverse=True)

# ─── Autenticación del panel web ─────────────────────────────────────────────
# Protege todas las rutas del panel (index, calendario, confirmar, cancelar,
# etc.) con usuario y contraseña. NO se aplica a /webhook ni a
# /enviar_recordatorios, ya que esas rutas necesitan funcionar
# automáticamente (Meta y cron-job.org) y tienen su propia protección.

def verificar_credenciales(usuario, contrasena):
    return (
        usuario == os.environ.get('PANEL_USER') and
        contrasena == os.environ.get('PANEL_PASSWORD')
    )

def solicitar_autenticacion():
    return Response(
        'Acceso restringido. Ingresa tus credenciales para continuar.',
        401,
        {'WWW-Authenticate': 'Basic realm="Panel BOCA"'}
    )

def requiere_autenticacion(f):
    @wraps(f)
    def decorada(*args, **kwargs):
        auth = request.authorization
        if not auth or not verificar_credenciales(auth.username, auth.password):
            return solicitar_autenticacion()
        return f(*args, **kwargs)
    return decorada

@app.route('/')
@requiere_autenticacion
def index():
    registros = Log.query.all()
    registros_ordenados = ordenar_por_fecha_y_hora(registros)
    conversaciones_activas = EstadoUsuario.query.filter_by(estado="atencion_humana").all()
    citas_pendientes = Cita.query.filter_by(estado="pendiente").order_by(Cita.creada_en.asc()).all()
    citas_confirmadas = Cita.query.filter_by(estado="confirmada").order_by(Cita.fecha_cita.asc()).all()
    llamadas_pendientes = Llamada.query.filter_by(estado="pendiente").order_by(Llamada.creada_en.asc()).all()
    return render_template(
        'index.html',
        registros=registros_ordenados,
        conversaciones_activas=conversaciones_activas,
        citas_pendientes=citas_pendientes,
        citas_confirmadas=citas_confirmadas,
        llamadas_pendientes=llamadas_pendientes
    )

def agregar_mensajes_log(texto):
    nuevo_registro = Log(texto=texto)
    db.session.add(nuevo_registro)
    db.session.commit()

def obtener_estado(numero):
    registro = EstadoUsuario.query.get(numero)
    return registro.estado if registro else None

def guardar_estado(numero, estado, nombre=None):
    registro = EstadoUsuario.query.get(numero)
    if registro:
        registro.estado = estado
        if nombre:
            registro.nombre = nombre
    else:
        registro = EstadoUsuario(numero=numero, estado=estado, nombre=nombre)
        db.session.add(registro)
    db.session.commit()

def borrar_estado(numero):
    registro = EstadoUsuario.query.get(numero)
    if registro:
        db.session.delete(registro)
        db.session.commit()

def obtener_cita_activa(numero):
    return Cita.query.filter(
        Cita.numero == numero,
        Cita.estado.in_(["pendiente", "confirmada"])
    ).first()

# ─── Integración con Google Calendar ─────────────────────────────────────────
# Si la credencial no existe o algo falla, el bot sigue funcionando normal
# con WhatsApp; Calendar es un extra, nunca un requisito para operar.

GOOGLE_CREDENTIALS_PATH = '/etc/secrets/google-credentials.json'
GOOGLE_CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'bocalapaz@gmail.com')

# IDs de color oficiales de Google Calendar (mismo esquema que el panel):
# azul = cita confirmada, verde = asistió, gris = no vino, rojo = cancelada
COLOR_CALENDAR_CONFIRMADA = '9'   # Blueberry (azul)
COLOR_CALENDAR_ASISTIO = '10'     # Basil (verde)
COLOR_CALENDAR_NO_ASISTIO = '8'   # Graphite (gris)
COLOR_CALENDAR_CANCELADA = '11'   # Tomato (rojo)

def obtener_servicio_calendar():
    if not os.path.isfile(GOOGLE_CREDENTIALS_PATH):
        return None
    try:
        credenciales = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_PATH,
            scopes=['https://www.googleapis.com/auth/calendar']
        )
        return build('calendar', 'v3', credentials=credenciales)
    except Exception as e:
        agregar_mensajes_log(f"Error Google Calendar (credenciales): {str(e)}")
        return None

def crear_evento_calendar(cita):
    servicio = obtener_servicio_calendar()
    if not servicio:
        return None
    try:
        zona_mexico = pytz.timezone('America/Mexico_City')
        inicio_naive = datetime.strptime(f"{cita.fecha_cita} {cita.hora_cita}", "%d/%m/%Y %H:%M")
        inicio = zona_mexico.localize(inicio_naive)
        fin = inicio + timedelta(hours=1)

        evento = {
            'summary': cita.nombre or "Paciente",
            'description': (
                f'Paciente: {cita.nombre or "Sin nombre"}\n'
                f'Teléfono: {cita.numero}'
            ),
            'location': (
                'Av. Rosendo Márquez 16, 50 Doctors, Torres Médicas V, '
                'La Paz, 72160, Heroica Puebla de Zaragoza, Pue.'
            ),
            'start': {'dateTime': inicio.isoformat()},
            'end': {'dateTime': fin.isoformat()},
            'colorId': COLOR_CALENDAR_CONFIRMADA,
        }
        resultado = servicio.events().insert(
            calendarId=GOOGLE_CALENDAR_ID, body=evento
        ).execute()
        agregar_mensajes_log(f"CALENDAR: evento creado -> {resultado.get('id')}")
        return resultado.get('id')
    except Exception as e:
        agregar_mensajes_log(f"Error Google Calendar (crear evento): {str(e)}")
        return None

def actualizar_color_evento_calendar(google_event_id, color_id):
    """Cambia el color del evento sin borrarlo, para reflejar el estado
    de la cita (asistió, no vino, cancelada) igual que en el panel."""
    if not google_event_id:
        return
    servicio = obtener_servicio_calendar()
    if not servicio:
        return
    try:
        servicio.events().patch(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=google_event_id,
            body={'colorId': color_id}
        ).execute()
        agregar_mensajes_log(f"CALENDAR: color actualizado -> {google_event_id} ({color_id})")
    except Exception as e:
        agregar_mensajes_log(f"Error Google Calendar (actualizar color): {str(e)}")

def eliminar_evento_calendar(google_event_id):
    if not google_event_id:
        return
    servicio = obtener_servicio_calendar()
    if not servicio:
        return
    try:
        servicio.events().delete(
            calendarId=GOOGLE_CALENDAR_ID, eventId=google_event_id
        ).execute()
        agregar_mensajes_log(f"CALENDAR: evento eliminado -> {google_event_id}")
    except Exception as e:
        agregar_mensajes_log(f"Error Google Calendar (eliminar evento): {str(e)}")

TOKEN_CESAR = "cesar"
CLAVE_RECORDATORIOS = "boca2026xK9mP3qL7nR2vT8wJ4cF6yH1"

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return verificar_token(request)
    elif request.method == 'POST':
        return recibir_mensajes(request)

def verificar_token(req):
    token = req.args.get('hub.verify_token')
    challenge = req.args.get('hub.challenge')
    if challenge and token == TOKEN_CESAR:
        return challenge
    return jsonify({'error': 'Token invalido'}), 401

def recibir_mensajes(req):
    try:
        data = req.get_json()
        entry = data['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        objeto_mensaje = value.get('messages')

        if objeto_mensaje:
            mensaje = objeto_mensaje[0]
            numero = mensaje.get("from")
            numero_normalizado = normalizar_numero_mx(numero)
            tipo = mensaje.get("type")

            agregar_mensajes_log(json.dumps(mensaje, ensure_ascii=False))

            estado = obtener_estado(numero_normalizado)

            if estado == "atencion_humana":
                return jsonify({'message': 'EVENT_RECEIVED'}), 200

            if estado == "esperando_nombre_cita" and tipo == "text":
                nombre = mensaje["text"]["body"].strip()
                nueva_cita = Cita(
                    numero=numero_normalizado,
                    nombre=nombre,
                    estado="pendiente"
                )
                db.session.add(nueva_cita)
                db.session.commit()
                borrar_estado(numero_normalizado)
                agregar_mensajes_log(f"SOLICITUD DE CITA -> {numero_normalizado} | Nombre: {nombre}")
                enviar_solicitud_recibida(numero)
                return jsonify({'message': 'EVENT_RECEIVED'}), 200

            if estado == "esperando_nombre_atencion" and tipo == "text":
                nombre = mensaje["text"]["body"].strip()
                borrar_estado(numero_normalizado)
                guardar_estado(numero_normalizado, "atencion_humana", nombre=nombre)
                agregar_mensajes_log(f"ATENCION HUMANA -> {numero_normalizado} | Nombre: {nombre}")
                enviar_pausa_bot(numero)
                return jsonify({'message': 'EVENT_RECEIVED'}), 200

            if estado == "esperando_nombre_llamada" and tipo == "text":
                nombre = mensaje["text"]["body"].strip()
                nueva_llamada = Llamada(
                    numero=numero_normalizado,
                    nombre=nombre,
                    estado="pendiente"
                )
                db.session.add(nueva_llamada)
                db.session.commit()
                borrar_estado(numero_normalizado)
                agregar_mensajes_log(f"SOLICITUD DE LLAMADA -> {numero_normalizado} | Nombre: {nombre}")
                enviar_solicitud_llamada_recibida(numero)
                return jsonify({'message': 'EVENT_RECEIVED'}), 200

            if tipo == "interactive":
                interactive = mensaje.get("interactive", {})
                if interactive.get("type") == "button_reply":
                    boton_id = interactive["button_reply"]["id"]

                    if boton_id == "btnmensaje":
                        guardar_estado(numero_normalizado, "esperando_nombre_atencion")
                        enviar_pedir_nombre_atencion(numero)
                    elif boton_id == "btnllamada":
                        guardar_estado(numero_normalizado, "esperando_nombre_llamada")
                        enviar_pedir_nombre_llamada(numero)
                    elif boton_id == "btnvercita":
                        enviar_detalle_cita(numero, numero_normalizado)
                    elif boton_id == "btncancelarcita":
                        enviar_advertencia_cancelacion(numero, numero_normalizado)
                    elif boton_id == "btnsicancel":
                        confirmar_cancelacion(numero, numero_normalizado)
                    elif boton_id == "btnnocancel":
                        enviar_mantener_cita(numero, numero_normalizado)

            elif tipo == "text":
                texto = mensaje["text"]["body"].strip()

                if texto == "1":
                    enviar_video_tutorial(numero)
                elif texto == "2":
                    enviar_ayuda_personalizada(numero)
                elif texto == "3":
                    manejar_punto_cita(numero, numero_normalizado)
                elif texto == "0":
                    enviar_menu(numero)
                else:
                    enviar_bienvenida(numero)
            else:
                enviar_bienvenida(numero)

        return jsonify({'message': 'EVENT_RECEIVED'}), 200

    except Exception as e:
        agregar_mensajes_log(f"Error: {str(e)}")
        return jsonify({'message': 'EVENT_RECEIVED'}), 200

# ─── Ruta de recordatorios automaticos ───────────────────────────────────────

@app.route('/enviar_recordatorios', methods=['GET'])
def enviar_recordatorios():
    clave = request.args.get('clave')
    if clave != CLAVE_RECORDATORIOS:
        return jsonify({'error': 'No autorizado'}), 401

    zona_mexico = pytz.timezone('America/Mexico_City')
    hoy = datetime.now(zona_mexico).strftime('%d/%m/%Y')

    citas_hoy = Cita.query.filter_by(
        estado="confirmada",
        recordatorio_enviado=False,
        fecha_cita=hoy
    ).all()

    enviados = 0
    for cita in citas_hoy:
        mensaje_recordatorio = (
            f"🦷 *¡Buenos días!* Hoy es el día de tu cita con *BOCA* "
            f"a las {cita.hora_cita}.\n\n"
            f"📍 Te esperamos en:\n"
            f"Av. Rosendo Márquez 16, 50 Doctors, Torres Médicas V,\n"
            f"La Paz, 72160, Heroica Puebla de Zaragoza, Pue.\n\n"
            f"⚠️ Recuerda que no es posible reagendar tu cita. Si por "
            f"alguna razón no puedes asistir, deberás cancelarla desde "
            f"la opción 3️⃣ *Mi cita* del menú y ponerte en contacto "
            f"con uno de nuestros especialistas para programar una nueva.\n\n"
            f"¡Te esperamos! 😊"
        )
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": cita.numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje_recordatorio}
        }
        enviar_payload(data)
        cita.recordatorio_enviado = True
        db.session.commit()
        agregar_mensajes_log(f"RECORDATORIO ENVIADO -> {cita.numero} | {cita.nombre} | {cita.fecha_cita} a las {cita.hora_cita}")
        enviados += 1

    return jsonify({'mensaje': f'Recordatorios enviados: {enviados}', 'fecha': hoy}), 200

# ─── Rutas del panel web ──────────────────────────────────────────────────────

@app.route('/confirmar_cita', methods=['POST'])
@requiere_autenticacion
def confirmar_cita():
    cita_id = request.form.get('cita_id')
    fecha = normalizar_fecha(request.form.get('fecha'))
    hora = normalizar_hora(request.form.get('hora'))

    cita = Cita.query.get(cita_id)
    if cita and fecha and hora:
        cita.estado = "confirmada"
        cita.fecha_cita = fecha
        cita.hora_cita = hora
        db.session.commit()

        cita.google_event_id = crear_evento_calendar(cita)
        db.session.commit()

        numero = cita.numero
        nombre = cita.nombre or "paciente"
        mensaje_confirmacion = (
            f"✅ *¡Tu cita ha sido confirmada!*\n\n"
            f"👤 Nombre: {nombre}\n"
            f"📅 Fecha: {fecha}\n"
            f"⏰ Hora: {hora}\n\n"
            f"Te enviaremos un recordatorio el mismo día de tu cita "
            f"por este medio.\n\n"
            f"⚠️ Recuerda que *no realizamos cambios de horario*. Si "
            f"necesitas un horario distinto, deberás cancelar esta "
            f"cita y contactarnos nuevamente con un especialista para "
            f"agendar una nueva.\n\n"
            f"🔓 Ahora tienes acceso a tu seguimiento de cita. Si "
            f"deseas ver los detalles de tu cita o cancelarla, escribe "
            f"*3* en cualquier momento.\n\n"
            f"➡️ Escribe *0* para volver al menú principal, o escribe "
            f"directamente el número de otra opción que te interese. 😊"
        )
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje_confirmacion}
        }
        enviar_payload(data)
        agregar_mensajes_log(f"CITA CONFIRMADA -> {numero} | {nombre} | {fecha} a las {hora}")

    return redirect('/')

@app.route('/rechazar_solicitud', methods=['POST'])
@requiere_autenticacion
def rechazar_solicitud():
    cita_id = request.form.get('cita_id')
    cita = Cita.query.get(cita_id)
    if cita:
        nombre = cita.nombre or "paciente"
        numero = cita.numero
        cita.estado = "cancelada"
        db.session.commit()
        agregar_mensajes_log(f"SOLICITUD RECHAZADA -> {numero} | {nombre}")

        mensaje_rechazo = (
            "ℹ️ *Aviso sobre tu solicitud de cita*\n\n"
            "Hemos revisado tu solicitud y no encontramos información "
            "pertinente asociada a ella. Es posible que aún no hayas "
            "acordado una cita directamente con alguno de nuestros "
            "especialistas.\n\n"
            "Si deseas agendar una cita, te invitamos a comunicarte "
            "primero con nosotros a través de la opción "
            "2️⃣ *Ayuda personalizada*, donde uno de nuestros "
            "especialistas podrá orientarte y acordar contigo la fecha "
            "y hora más conveniente.\n\n"
            "¡Gracias por tu comprensión! 😊\n\n"
            "➡️ Escribe *0* para volver al menú principal, o escribe "
            "directamente el número de otra opción que te interese."
        )
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje_rechazo}
        }
        enviar_payload(data)

    return redirect('/')

@app.route('/cancelar_cita_admin', methods=['POST'])
@requiere_autenticacion
def cancelar_cita_admin():
    cita_id = request.form.get('cita_id')
    cita = Cita.query.get(cita_id)
    if cita:
        info = f"{cita.fecha_cita} a las {cita.hora_cita}" if cita.fecha_cita else "sin fecha asignada"
        nombre = cita.nombre or "paciente"
        numero = cita.numero
        actualizar_color_evento_calendar(cita.google_event_id, COLOR_CALENDAR_CANCELADA)
        cita.estado = "cancelada"
        db.session.commit()
        agregar_mensajes_log(f"CITA CANCELADA POR ADMIN -> {numero} | {nombre} | Cita del {info}")

        mensaje_cancelacion = (
            "😔 *Aviso importante sobre tu cita*\n\n"
            "Lamentamos informarte que, debido a causas ajenas a nuestra "
            "voluntad, nos hemos visto en la necesidad de cancelar tu cita "
            "programada con *BOCA*.\n\n"
            "Pedimos sinceramente una disculpa por los inconvenientes que "
            "esto pueda ocasionarte. Nuestro equipo se pondrá en contacto "
            "contigo a la brevedad para reagendar tu cita en el horario "
            "que mejor se adapte a tus necesidades.\n\n"
            "Gracias por tu comprensión y confianza en nosotros. 🙏\n\n"
            "➡️ Escribe *0* para volver al menú principal, o escribe "
            "directamente el número de otra opción que te interese."
        )
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje_cancelacion}
        }
        enviar_payload(data)

    return redirect('/')

@app.route('/marcar_asistio', methods=['POST'])
@requiere_autenticacion
def marcar_asistio():
    cita_id = request.form.get('cita_id')
    cita = Cita.query.get(cita_id)
    if cita:
        numero = cita.numero
        nombre = cita.nombre or "paciente"
        actualizar_color_evento_calendar(cita.google_event_id, COLOR_CALENDAR_ASISTIO)
        cita.estado = "asistio"
        db.session.commit()
        agregar_mensajes_log(f"CITA ASISTIDA -> {numero} | {nombre} | {cita.fecha_cita} a las {cita.hora_cita}")

        mensaje_gracias = (
            "🦷 *¡Gracias por tu visita!*\n\n"
            "Fue un gusto atenderte en *BOCA*. Esperamos que te hayas "
            "sentido cómodo durante tu consulta y te deseamos una "
            "pronta y excelente recuperación.\n\n"
            "Si en el futuro necesitas agendar una nueva cita o tienes "
            "alguna duda, aquí estaremos para ayudarte con gusto.\n\n"
            "¡Esperamos verte pronto! 😊\n\n"
            "➡️ Escribe *0* para volver al menú principal."
        )
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje_gracias}
        }
        enviar_payload(data)

    return redirect('/')

@app.route('/marcar_no_asistio', methods=['POST'])
@requiere_autenticacion
def marcar_no_asistio():
    cita_id = request.form.get('cita_id')
    cita = Cita.query.get(cita_id)
    if cita:
        numero = cita.numero
        nombre = cita.nombre or "paciente"
        info = f"{cita.fecha_cita} a las {cita.hora_cita}"
        actualizar_color_evento_calendar(cita.google_event_id, COLOR_CALENDAR_NO_ASISTIO)
        cita.estado = "no_asistio"
        db.session.commit()
        agregar_mensajes_log(f"CITA NO ASISTIDA -> {numero} | {nombre} | Cita del {info}")

        mensaje_no_asistio = (
            "😔 *Aviso sobre tu cita*\n\n"
            f"Notamos que no llegaste a tu cita programada para el "
            f"{info} con *BOCA*, por lo que ha sido cancelada "
            f"automáticamente.\n\n"
            "Si necesitas reagendar, escribe la opción 3️⃣ *Mi cita* "
            "del menú principal para hacer una nueva solicitud.\n\n"
            "¡Esperamos verte pronto! 😊\n\n"
            "➡️ Escribe *0* para volver al menú principal."
        )
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje_no_asistio}
        }
        enviar_payload(data)

    return redirect('/')

@app.route('/llamada_hecha', methods=['POST'])
@requiere_autenticacion
def llamada_hecha():
    llamada_id = request.form.get('llamada_id')
    llamada = Llamada.query.get(llamada_id)
    if llamada:
        numero = llamada.numero
        nombre = llamada.nombre or "paciente"
        llamada.estado = "completada"
        db.session.commit()
        agregar_mensajes_log(f"LLAMADA COMPLETADA -> {numero} | {nombre}")

        mensaje_llamada_hecha = (
            "✅ *¡Gracias por tu paciencia!*\n\n"
            "Esperamos haber resuelto todas tus dudas durante la "
            "llamada. Si necesitas algo más, no dudes en contactarnos "
            "nuevamente.\n\n"
            "¡Gracias por confiar en *BOCA*! 😊\n\n"
            "➡️ Escribe *0* para volver al menú principal, o escribe "
            "directamente el número de otra opción que te interese."
        )
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje_llamada_hecha}
        }
        enviar_payload(data)

    return redirect('/')

@app.route('/quitar_llamada', methods=['POST'])
@requiere_autenticacion
def quitar_llamada():
    llamada_id = request.form.get('llamada_id')
    llamada = Llamada.query.get(llamada_id)
    if llamada:
        agregar_mensajes_log(f"SOLICITUD DE LLAMADA DESCARTADA -> {llamada.numero} | {llamada.nombre or 'Sin nombre'}")
        db.session.delete(llamada)
        db.session.commit()

    return redirect('/')

# ─── Calendario semanal ───────────────────────────────────────────────────────

@app.route('/calendario')
@requiere_autenticacion
def calendario():
    inicio_str = request.args.get('inicio')
    inicio = None
    if inicio_str:
        try:
            inicio = datetime.strptime(inicio_str, '%d/%m/%Y').date()
        except ValueError:
            inicio = None

    if not inicio:
        zona_mexico = pytz.timezone('America/Mexico_City')
        hoy = datetime.now(zona_mexico).date()
        inicio = hoy - timedelta(days=hoy.weekday())  # Lunes de esta semana

    dias = [inicio + timedelta(days=i) for i in range(7)]  # Semana completa
    dias_str = [d.strftime('%d/%m/%Y') for d in dias]

    horas = [f"{h:02d}:00" for h in range(24)]  # Día completo, 00:00 a 23:00

    citas_semana = Cita.query.filter(
        Cita.estado.in_(["confirmada", "asistio", "no_asistio", "cancelada"]),
        Cita.fecha_cita.in_(dias_str)
    ).all()

    grid = {hora: {fecha: [] for fecha in dias_str} for hora in horas}
    for cita in citas_semana:
        if cita.hora_cita and ':' in cita.hora_cita:
            hora_key = cita.hora_cita.split(':')[0].zfill(2) + ":00"
            if hora_key in grid and cita.fecha_cita in grid[hora_key]:
                grid[hora_key][cita.fecha_cita].append(cita)

    dias_info = list(zip(dias, dias_str))

    semana_anterior = (inicio - timedelta(days=7)).strftime('%d/%m/%Y')
    semana_siguiente = (inicio + timedelta(days=7)).strftime('%d/%m/%Y')

    return render_template(
        'calendario.html',
        dias_info=dias_info,
        horas=horas,
        grid=grid,
        semana_anterior=semana_anterior,
        semana_siguiente=semana_siguiente
    )

@app.route('/responder', methods=['POST'])
@requiere_autenticacion
def responder():
    numero = request.form.get('numero')
    mensaje_texto = request.form.get('mensaje')

    if numero and mensaje_texto:
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje_texto}
        }
        enviar_payload(data)
        agregar_mensajes_log(f"RESPUESTA MANUAL -> {numero}: {mensaje_texto}")

    return redirect('/')

@app.route('/finalizar', methods=['POST'])
@requiere_autenticacion
def finalizar():
    numero = request.form.get('numero')

    if numero:
        data_cierre = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": (
                    "✅ *Conversación finalizada*\n\n"
                    "Tu atención personalizada con nuestro especialista ha "
                    "concluido. Esperamos haber resuelto tus dudas.\n\n"
                    "Si necesitas algo más, escribe *0* para volver al menú "
                    "principal, o escribe directamente el número de otra "
                    "opción que te interese.\n\n"
                    "¡Gracias por contactar a *BOCA*! 😊"
                )
            }
        }
        enviar_payload(data_cierre)
        borrar_estado(numero)
        agregar_mensajes_log(f"CONVERSACION FINALIZADA -> {numero}")

    return redirect('/')

def normalizar_numero_mx(numero):
    if numero.startswith("521") and len(numero) == 13:
        return "52" + numero[3:]
    return numero

def normalizar_fecha(fecha_str):
    """Acepta 6/7/2026, 06/07/2026, 6-7-2026, etc. y siempre devuelve
    el formato exacto DD/MM/AAAA con ceros, que es lo que usa el
    calendario interno para hacer coincidir las citas."""
    fecha_str = (fecha_str or '').strip()
    for separador in ['/', '-', '.']:
        if separador in fecha_str:
            partes = fecha_str.split(separador)
            if len(partes) == 3:
                try:
                    dia, mes, anio = partes
                    if len(anio) == 2:
                        anio = '20' + anio
                    return f"{int(dia):02d}/{int(mes):02d}/{int(anio):04d}"
                except ValueError:
                    return fecha_str
    return fecha_str

def normalizar_hora(hora_str):
    """Acepta 10, 10:0, 10:00, etc. y siempre devuelve HH:MM con ceros,
    que es lo que usa el calendario interno para ubicar la cita en su
    casilla correcta."""
    hora_str = (hora_str or '').strip()
    try:
        if ':' in hora_str:
            partes = hora_str.split(':')
            horas = int(partes[0])
            minutos = int(partes[1]) if len(partes) > 1 and partes[1] != '' else 0
        else:
            horas = int(hora_str)
            minutos = 0
        return f"{horas:02d}:{minutos:02d}"
    except ValueError:
        return hora_str

def enviar_payload(data):
    data = json.dumps(data)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ.get('WHATSAPP_TOKEN')}"
    }
    connection = http.client.HTTPSConnection("graph.facebook.com")
    try:
        connection.request("POST", "/v25.0/1158458244021223/messages", data, headers)
        response = connection.getresponse()
        response_body = response.read().decode('utf-8')
        agregar_mensajes_log(f"WhatsApp API -> Status: {response.status} {response.reason} | Body: {response_body}")
    except Exception as e:
        agregar_mensajes_log(f"Error de conexion: {str(e)}")
    finally:
        connection.close()

# ─── Funciones de citas ───────────────────────────────────────────────────────

def enviar_pedir_nombre(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "📅 *Mi cita*\n\n"
                "Para registrar tu solicitud de cita, por favor "
                "escríbenos tu nombre completo. 😊"
            )
        }
    }
    enviar_payload(data)

def enviar_pedir_nombre_atencion(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "💬 *Ayuda personalizada*\n\n"
                "Para poder atenderte mejor, por favor "
                "escríbenos tu nombre completo. 😊"
            )
        }
    }
    enviar_payload(data)

def enviar_pedir_nombre_llamada(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "📞 *Solicitar llamada*\n\n"
                "Para registrar tu solicitud, por favor "
                "escríbenos tu nombre completo. 😊"
            )
        }
    }
    enviar_payload(data)

def enviar_solicitud_recibida(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "📅 *Solicitud de cita recibida*\n\n"
                "Hemos recibido tu solicitud. Uno de nuestros especialistas "
                "la revisará y te confirmará fecha y hora a la brevedad.\n\n"
                "ℹ️ Recuerda que tu cita únicamente podrá ser confirmada si "
                "previamente fue acordada con alguno de nuestros especialistas. "
                "Puedes hacerlo a través de la opción 2️⃣ *Ayuda "
                "personalizada*, ya sea escribiéndonos directamente o "
                "solicitando una llamada. Si aún no la has acordado, "
                "nos pondremos en contacto contigo para orientarte.\n\n"
                "⏳ Si en algún momento necesitas cancelar tu cita, te "
                "pedimos hacerlo con la mayor anticipación posible para "
                "liberar el espacio a otros pacientes.\n\n"
                "⚠️ Ten en cuenta que *no realizamos cambios de horario*. "
                "Si necesitas un horario distinto, deberás cancelar la cita "
                "y contactarnos nuevamente con un especialista para agendar "
                "una nueva.\n\n"
                "¡Gracias por tu paciencia! 😊"
            )
        }
    }
    enviar_payload(data)

def enviar_solicitud_llamada_recibida(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "📞 *Solicitud de llamada recibida*\n\n"
                "Hemos registrado tu solicitud y uno de nuestros "
                "especialistas se pondrá en contacto contigo por teléfono "
                "lo antes posible.\n\n"
                "ℹ️ No siempre estamos disponibles de inmediato y podríamos "
                "estar ocupados en otras consultas, pero ten la seguridad "
                "de que tu solicitud ya quedó registrada y será atendida.\n\n"
                "💬 Si mientras tanto prefieres dejarnos un mensaje con tu "
                "duda o necesitas algo urgente, puedes usar la opción "
                "*Hablar con nosotros* dentro de 2️⃣ *Ayuda personalizada*.\n\n"
                "🔒 Por tu seguridad, te contactaremos únicamente desde este "
                "mismo número de WhatsApp. Si recibes una llamada de un "
                "número distinto que diga representarnos, te recomendamos "
                "no confiar en ella y reportarlo directamente con "
                "nosotros.\n\n"
                "Gracias por confiar en *BOCA* para tu atención. 😊\n\n"
                "➡️ Escribe *0* para volver al menú principal, o escribe "
                "directamente el número de otra opción que te interese."
            )
        }
    }
    enviar_payload(data)

def enviar_solicitud_en_espera(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "📅 *Ya tienes una solicitud en espera*\n\n"
                "Recibimos tu solicitud de cita anteriormente y aún está "
                "siendo revisada por nuestro equipo. Es posible que nuestros "
                "especialistas estén ocupados en este momento.\n\n"
                "En cuanto sea posible, te confirmaremos tu fecha y hora "
                "por este mismo medio. ¡Gracias por tu paciencia! 😊"
            )
        }
    }
    enviar_payload(data)

def enviar_opciones_cita(number, cita):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"📅 *Mi cita*\n\n"
                    f"Ya tienes una cita confirmada con *BOCA*. Si "
                    f"necesitas cancelarla, puedes hacerlo aquí abajo."
                )
            },
            "footer": {"text": "Selecciona una opción"},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": "btncancelarcita", "title": "Cancelar cita"}
                    }
                ]
            }
        }
    }
    enviar_payload(data)

def enviar_detalle_cita(number, numero_normalizado):
    number = normalizar_numero_mx(number)
    cita = obtener_cita_activa(numero_normalizado)
    if cita and cita.estado == "confirmada":
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": number,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": (
                    f"🗓️ *Tu cita*\n\n"
                    f"📆 Fecha: {cita.fecha_cita}\n"
                    f"⏰ Hora: {cita.hora_cita}\n\n"
                    f"📍 Te esperamos en:\n"
                    f"Av. Rosendo Márquez 16, 50 Doctors, Torres Médicas V,\n"
                    f"La Paz, 72160, Heroica Puebla de Zaragoza, Pue.\n\n"
                    f"➡️ Escribe *0* para volver al menú principal, o escribe "
                    f"directamente el número de otra opción que te interese. 😊"
                )
            }
        }
        enviar_payload(data)

def enviar_advertencia_cancelacion(number, numero_normalizado):
    number = normalizar_numero_mx(number)
    cita = obtener_cita_activa(numero_normalizado)
    if cita and cita.estado == "confirmada":
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": number,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": (
                        f"⚠️ *¿Seguro que deseas cancelar tu cita?*\n\n"
                        f"📆 Fecha: {cita.fecha_cita}\n"
                        f"⏰ Hora: {cita.hora_cita}\n\n"
                        f"⏳ Te recomendamos cancelar con la mayor "
                        f"anticipación posible para liberar el espacio "
                        f"a otros pacientes que puedan necesitarlo.\n\n"
                        f"🚫 Recuerda que *no realizamos cambios de "
                        f"horario*. Si necesitas un horario distinto, "
                        f"deberás cancelar esta cita y contactarnos "
                        f"nuevamente con un especialista para agendar "
                        f"una nueva."
                    )
                },
                "footer": {"text": "Esta acción no se puede deshacer"},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {"id": "btnsicancel", "title": "Sí, cancelar"}
                        },
                        {
                            "type": "reply",
                            "reply": {"id": "btnnocancel", "title": "No, mantener"}
                        }
                    ]
                }
            }
        }
        enviar_payload(data)

def enviar_mantener_cita(number, numero_normalizado):
    number = normalizar_numero_mx(number)
    cita = obtener_cita_activa(numero_normalizado)
    if cita and cita.estado == "confirmada":
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": number,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": (
                    f"✅ *Tu cita se mantiene*\n\n"
                    f"No hay problema, tu cita sigue confirmada:\n\n"
                    f"📆 Fecha: {cita.fecha_cita}\n"
                    f"⏰ Hora: {cita.hora_cita}\n\n"
                    f"Si en algún momento deseas cancelarla, puedes hacerlo "
                    f"desde la opción 3️⃣ *Mi cita* del menú principal. "
                    f"Recuerda hacerlo con la mayor anticipación posible.\n\n"
                    f"¡Te esperamos en *BOCA*! 😊\n\n"
                    f"➡️ Escribe *0* para volver al menú principal, o escribe "
                    f"directamente el número de otra opción que te interese."
                )
            }
        }
        enviar_payload(data)

def confirmar_cancelacion(number, numero_normalizado):
    number = normalizar_numero_mx(number)
    cita = obtener_cita_activa(numero_normalizado)
    if cita:
        info = f"{cita.fecha_cita} a las {cita.hora_cita}"
        nombre = cita.nombre or "paciente"
        actualizar_color_evento_calendar(cita.google_event_id, COLOR_CALENDAR_CANCELADA)
        cita.estado = "cancelada"
        db.session.commit()
        agregar_mensajes_log(f"CITA CANCELADA -> {numero_normalizado} | {nombre} | Cita del {info}")

        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": number,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": (
                    "✅ *Tu cita ha sido cancelada exitosamente*\n\n"
                    "Si deseas agendar una nueva cita, puedes volver a "
                    "seleccionar la opción 3️⃣ *Mi cita* del menú, o bien "
                    "contactarte directamente con uno de nuestros "
                    "especialistas para acordar una nueva fecha.\n\n"
                    "¡Que tengas un excelente día! 😊\n\n"
                    "➡️ Escribe *0* para volver al menú principal, o escribe "
                    "directamente el número de otra opción que te interese."
                )
            }
        }
        enviar_payload(data)

# ─── Funciones generales del bot ─────────────────────────────────────────────

def enviar_bienvenida(number):
    number = normalizar_numero_mx(number)
    data_imagen = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "image",
        "image": {
            "link": "https://github.com/bocalapaz-lab/boca-assets/blob/main/logo%20boca_page-0001.jpg?raw=true"
        }
    }
    enviar_payload(data_imagen)
    time.sleep(1.5)
    data_bienvenida = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "¡Hola! 👋 Gracias por escribirnos a *BOCA*.\n\n"
                "Soy el asistente virtual del consultorio. Estoy aquí para "
                "ayudarte en lo que necesites. 😊\n\n"
                "Elige una opción escribiendo el número:\n\n"
                "1️⃣ Video tutorial (recomendado)\n"
                "2️⃣ Ayuda personalizada\n"
                "3️⃣ Mi cita\n\n"
                "Escribe el número de la opción que te interese."
            )
        }
    }
    enviar_payload(data_bienvenida)

def enviar_menu(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "📋 *Menú principal*\n\n"
                "1️⃣ Video tutorial (recomendado)\n"
                "2️⃣ Ayuda personalizada\n"
                "3️⃣ Mi cita\n\n"
                "Escribe el número de la opción que te interese."
            )
        }
    }
    enviar_payload(data)

def enviar_video_tutorial(number):
    number = normalizar_numero_mx(number)
    video_url = os.environ.get('VIDEO_TUTORIAL_URL')

    texto_explicativo = (
        "🎥 *Video tutorial (recomendado)*\n\n"
        "Hemos preparado este video para que puedas familiarizarte "
        "con nuestro chatbot y aprender a usarlo sin ningún "
        "problema.\n\n"
        "Lo creamos pensando en tu comodidad y en agilizar cada "
        "proceso, para que puedas resolver tus dudas, agendar tu "
        "cita o contactarnos de la forma más sencilla posible. 😊\n\n"
        "➡️ Escribe *0* para volver al menú principal, o escribe "
        "directamente el número de otra opción que te interese."
    )

    if video_url:
        data_video = {
            "messaging_product": "whatsapp",
            "to": number,
            "type": "video",
            "video": {
                "link": video_url,
                "caption": texto_explicativo
            }
        }
        enviar_payload(data_video)
    else:
        data_sin_video = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": number,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": (
                    "🎥 *Video tutorial*\n\n"
                    "Estamos preparando este video con mucho cariño para "
                    "ayudarte a usar nuestro chatbot con mayor facilidad. "
                    "Muy pronto estará disponible aquí mismo. "
                    "¡Gracias por tu paciencia! 😊\n\n"
                    "➡️ Escribe *0* para volver al menú principal, o escribe "
                    "directamente el número de otra opción que te interese."
                )
            }
        }
        enviar_payload(data_sin_video)

def enviar_ayuda_personalizada(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    "💬 *Ayuda personalizada*\n\n"
                    "Cuéntanos cómo prefieres que te ayudemos:\n\n"
                    "ℹ️ Ten en cuenta que en ocasiones podríamos estar "
                    "ocupados atendiendo otras consultas, por lo que tu "
                    "mensaje podría no ser respondido de inmediato. "
                    "Agradecemos tu paciencia. 😊"
                )
            },
            "footer": {"text": "Selecciona una opción"},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": "btnmensaje", "title": "Hablar con nosotros"}
                    },
                    {
                        "type": "reply",
                        "reply": {"id": "btnllamada", "title": "Solicitar llamada"}
                    }
                ]
            }
        }
    }
    enviar_payload(data)

def enviar_pausa_bot(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "🙏 *¡Gracias por contactarnos!*\n\n"
                "En breve estarás en contacto con uno de nuestros "
                "especialistas, quien te atenderá personalmente por este "
                "mismo medio.\n\n"
                "Te pedimos un poco de paciencia mientras te asignamos con "
                "alguien disponible. 😊\n\n"
                "💬 Mientras tanto, si ya tienes clara tu duda o el motivo "
                "de tu mensaje, puedes escribírnosla desde ahora. Así, "
                "cuando uno de nuestros especialistas revise tu "
                "conversación, podrá leerla directamente y responderte "
                "más rápido."
            )
        }
    }
    enviar_payload(data)

def manejar_punto_cita(numero, numero_normalizado):
    cita = obtener_cita_activa(numero_normalizado)

    if cita is None:
        guardar_estado(numero_normalizado, "esperando_nombre_cita")
        enviar_pedir_nombre(numero)
    elif cita.estado == "pendiente":
        enviar_solicitud_en_espera(numero)
    elif cita.estado == "confirmada":
        enviar_opciones_cita(numero, cita)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=False)
