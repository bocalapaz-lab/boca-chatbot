from flask import Flask, jsonify, request, render_template, redirect
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import http.client
import json
import time
import os
import pytz

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///metapython.db'
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
    creada_en = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

def ordenar_por_fecha_y_hora(registros):
    return sorted(registros, key=lambda x: x.fecha_y_hora, reverse=True)

@app.route('/')
def index():
    registros = Log.query.all()
    registros_ordenados = ordenar_por_fecha_y_hora(registros)
    conversaciones_activas = EstadoUsuario.query.filter_by(estado="atencion_humana").all()
    citas_pendientes = Cita.query.filter_by(estado="pendiente").order_by(Cita.creada_en.asc()).all()
    citas_confirmadas = Cita.query.filter_by(estado="confirmada").order_by(Cita.fecha_cita.asc()).all()
    return render_template(
        'index.html',
        registros=registros_ordenados,
        conversaciones_activas=conversaciones_activas,
        citas_pendientes=citas_pendientes,
        citas_confirmadas=citas_confirmadas
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

            if tipo == "interactive":
                interactive = mensaje.get("interactive", {})
                if interactive.get("type") == "button_reply":
                    boton_id = interactive["button_reply"]["id"]

                    if boton_id == "btnmensaje":
                        guardar_estado(numero_normalizado, "esperando_nombre_atencion")
                        enviar_pedir_nombre_atencion(numero)
                    elif boton_id == "btnllamada":
                        agregar_mensajes_log(f"SOLICITUD DE LLAMADA -> {numero_normalizado}")
                        enviar_confirmacion_llamada(numero)
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
                    enviar_conocenos(numero)
                elif texto == "2":
                    enviar_video_construccion(numero)
                elif texto == "3":
                    enviar_ubicacion(numero)
                elif texto == "4":
                    enviar_estacionamiento(numero)
                elif texto == "5":
                    enviar_horario(numero)
                elif texto == "6":
                    enviar_ayuda_personalizada(numero)
                elif texto == "7":
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
            f"la opción 7️⃣ *Mi cita* del menú y ponerte en contacto "
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
def confirmar_cita():
    cita_id = request.form.get('cita_id')
    fecha = request.form.get('fecha')
    hora = request.form.get('hora')

    cita = Cita.query.get(cita_id)
    if cita and fecha and hora:
        cita.estado = "confirmada"
        cita.fecha_cita = fecha
        cita.hora_cita = hora
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
            f"*7* en cualquier momento.\n\n"
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
            "6️⃣ *Ayuda personalizada*, donde uno de nuestros "
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
def cancelar_cita_admin():
    cita_id = request.form.get('cita_id')
    cita = Cita.query.get(cita_id)
    if cita:
        info = f"{cita.fecha_cita} a las {cita.hora_cita}" if cita.fecha_cita else "sin fecha asignada"
        nombre = cita.nombre or "paciente"
        numero = cita.numero
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

@app.route('/responder', methods=['POST'])
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
                "De no ser así, nos pondremos en contacto contigo para "
                "orientarte.\n\n"
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
                    f"Tienes una cita confirmada con *BOCA*:\n\n"
                    f"📆 Fecha: {cita.fecha_cita}\n"
                    f"⏰ Hora: {cita.hora_cita}\n\n"
                    f"¿Qué deseas hacer?"
                )
            },
            "footer": {"text": "Selecciona una opción"},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": "btnvercita", "title": "Ver mi cita"}
                    },
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
                    f"desde la opción 7️⃣ *Mi cita* del menú principal. "
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
                    "seleccionar la opción 7️⃣ *Mi cita* del menú, o bien "
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
                "1️⃣ Conócenos\n"
                "2️⃣ Video de nosotros\n"
                "3️⃣ Ubicación del consultorio\n"
                "4️⃣ Estacionamiento\n"
                "5️⃣ Horario de atención\n"
                "6️⃣ Ayuda personalizada\n"
                "7️⃣ Mi cita\n\n"
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
                "1️⃣ Conócenos\n"
                "2️⃣ Video de nosotros\n"
                "3️⃣ Ubicación del consultorio\n"
                "4️⃣ Estacionamiento\n"
                "5️⃣ Horario de atención\n"
                "6️⃣ Ayuda personalizada\n"
                "7️⃣ Mi cita\n\n"
                "Escribe el número de la opción que te interese."
            )
        }
    }
    enviar_payload(data)

def enviar_conocenos(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "👋 *Conócenos*\n\n"
                "En *BOCA* contamos con un equipo especializado, comprometido "
                "con tu salud y bienestar:\n\n"
                "🦷 *Dra. Yaxcy Reyes García*\n"
                "Especialista en Cirugía Maxilofacial\n\n"
                "🦷 *Dr. Rubén Fernández Tamayo*\n"
                "Especialista en Cirugía Maxilofacial\n\n"
                "🎯 *Misión*\n"
                "Brindar atención odontológica y maxilofacial de excelencia, "
                "con un enfoque humano y profesional, utilizando técnicas "
                "actualizadas para mejorar la salud y calidad de vida de "
                "nuestros pacientes.\n\n"
                "🔭 *Visión*\n"
                "Ser un consultorio de referencia en cirugía maxilofacial, "
                "reconocido por la confianza de nuestros pacientes, la "
                "calidez de nuestro trato y la calidad de nuestros resultados.\n\n"
                "➡️ Escribe *0* para volver al menú principal, o escribe "
                "directamente el número de otra opción que te interese. 😊"
            )
        }
    }
    enviar_payload(data)

def enviar_video_construccion(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "🚧 *Video en construcción*\n\n"
                "Estamos preparando con mucho cariño un video para darte la "
                "bienvenida que te mereces y mostrarte nuestras instalaciones. "
                "Estará disponible muy pronto. ¡Gracias por tu paciencia! 😊\n\n"
                "➡️ Escribe *0* para volver al menú principal, o escribe "
                "directamente el número de otra opción que te interese."
            )
        }
    }
    enviar_payload(data)

def enviar_ubicacion(number):
    number = normalizar_numero_mx(number)
    data_ubicacion = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "location",
        "location": {
            "latitude": "19.056722627267366",
            "longitude": "-98.23117504866542",
            "name": "BOCA",
            "address": "Av. Rosendo Márquez 16, 50 Doctors, Torres Médicas V, La Paz, 72160, Heroica Puebla de Zaragoza, Pue."
        }
    }
    enviar_payload(data_ubicacion)
    time.sleep(1.5)
    data_texto = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "📍 *Nuestra ubicación*\n\n"
                "Av. Rosendo Márquez 16, 50 Doctors, Torres Médicas V\n"
                "La Paz, 72160, Heroica Puebla de Zaragoza, Pue.\n\n"
                "¡Te esperamos! 😊\n\n"
                "➡️ Escribe *0* para volver al menú principal, o escribe "
                "directamente el número de otra opción que te interese."
            )
        }
    }
    enviar_payload(data_texto)

def enviar_estacionamiento(number):
    number = normalizar_numero_mx(number)
    data_ubicacion = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "location",
        "location": {
            "latitude": "19.057766",
            "longitude": "-98.231919",
            "name": "Estacionamiento",
            "address": "La Paz, 72160 Heroica Puebla de Zaragoza, Pue."
        }
    }
    enviar_payload(data_ubicacion)
    time.sleep(1.5)
    data_texto = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "🅿️ *Estacionamiento*\n\n"
                "Como medida de comodidad para ti, te compartimos la "
                "ubicación de un estacionamiento cercano, justo cruzando "
                "la calle (ubicación enviada arriba 👆).\n\n"
                "ℹ️ Este estacionamiento es independiente y *no pertenece "
                "ni al hospital ni a nuestro consultorio BOCA*. Es un "
                "servicio externo que se encuentra cerca para tu comodidad.\n\n"
                "⚠️ Por lo anterior, *BOCA no se hace responsable* por tu "
                "vehículo, sus pertenencias, ni por cualquier situación que "
                "pudiera presentarse en dicho estacionamiento.\n\n"
                "➡️ Escribe *0* para volver al menú principal, o escribe "
                "directamente el número de otra opción que te interese. 😊"
            )
        }
    }
    enviar_payload(data_texto)

def enviar_horario(number):
    number = normalizar_numero_mx(number)
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": (
                "🕐 *Horario de Atención*\n\n"
                "📅 Lunes a Viernes\n"
                "⏰ 10:00 am – 7:00 pm\n\n"
                "ℹ️ Fuera de este horario, la opción de *Ayuda "
                "personalizada* (6️⃣) podría no tener respuesta inmediata, "
                "ya que nuestro equipo no estará disponible para contestar "
                "en ese momento.\n\n"
                "➡️ Escribe *0* para volver al menú principal, o escribe "
                "directamente el número de otra opción que te interese. 😊"
            )
        }
    }
    enviar_payload(data)

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
                    "ℹ️ Si nos escribes fuera de nuestro horario de "
                    "atención, es posible que tu mensaje no sea respondido "
                    "de inmediato. Te invitamos a revisar el punto 5️⃣ para "
                    "conocer nuestros horarios."
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
                "alguien disponible. 😊"
            )
        }
    }
    enviar_payload(data)

def enviar_confirmacion_llamada(number):
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
                "Por tu seguridad, te contactaremos únicamente desde este "
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