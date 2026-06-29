from flask import Flask, jsonify, request, render_template, redirect
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import http.client
import json
import time

app = Flask(__name__)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///metapython.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Log(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha_y_hora = db.Column(db.DateTime, default=datetime.utcnow)
    texto = db.Column(db.Text)

# Recuerda que numeros estan siendo atendidos por una persona (no por el bot)
class EstadoUsuario(db.Model):
    numero = db.Column(db.String, primary_key=True)
    estado = db.Column(db.String)
    desde = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

def ordenar_por_fecha_y_hora(registros):
    return sorted(registros, key=lambda x: x.fecha_y_hora, reverse=True)

@app.route('/')
def index():
    registros = Log.query.all()
    registros_ordenados = ordenar_por_fecha_y_hora(registros)
    conversaciones_activas = EstadoUsuario.query.filter_by(estado="atencion_humana").all()
    return render_template(
        'index.html',
        registros=registros_ordenados,
        conversaciones_activas=conversaciones_activas
    )

def agregar_mensajes_log(texto):
    nuevo_registro = Log(texto=texto)
    db.session.add(nuevo_registro)
    db.session.commit()

def obtener_estado(numero):
    registro = EstadoUsuario.query.get(numero)
    return registro.estado if registro else None

def guardar_estado(numero, estado):
    registro = EstadoUsuario.query.get(numero)
    if registro:
        registro.estado = estado
    else:
        registro = EstadoUsuario(numero=numero, estado=estado)
        db.session.add(registro)
    db.session.commit()

def borrar_estado(numero):
    registro = EstadoUsuario.query.get(numero)
    if registro:
        db.session.delete(registro)
        db.session.commit()

TOKEN_CESAR = "cesar"

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
                # Ya esta en manos de una persona. El bot NO responde
                # automaticamente -- el mensaje solo queda registrado para
                # que lo veas y respondas tu mismo desde el panel.
                return jsonify({'message': 'EVENT_RECEIVED'}), 200

            if tipo == "interactive":
                interactive = mensaje.get("interactive", {})
                if interactive.get("type") == "button_reply":
                    boton_id = interactive["button_reply"]["id"]

                    if boton_id == "btnmensaje":
                        enviar_pausa_bot(numero)
                        guardar_estado(numero_normalizado, "atencion_humana")

                    elif boton_id == "btnllamada":
                        agregar_mensajes_log(f"SOLICITUD DE LLAMADA -> {numero_normalizado}")
                        enviar_confirmacion_llamada(numero)

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

# Ruta nueva: aqui llega cuando TU escribes una respuesta desde el panel web
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
            "text": {
                "preview_url": False,
                "body": mensaje_texto
            }
        }
        enviar_payload(data)
        agregar_mensajes_log(f"RESPUESTA MANUAL -> {numero}: {mensaje_texto}")

    return redirect('/')

# Ruta nueva: aqui llega cuando le das clic a "Finalizar conversacion"
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
                    "principal.\n\n"
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
        "Authorization": "Bearer EAAVEa8dSzTcBRwr2QVWtMH78l1ZCP4PKQRVvRWfgHtGFmsvOFzm7NVI58SpncFBQVREJOXZAihd10rqHDhqCAd0xOzeBZC8fyc3Y1IRLbMtQaR22x8SvAU1Hv48fku9jxVZC12cCbmy29sRVCNrvYpf1KtFqDbqHcKJq2jvyCLSAkslV4ZAYZAGg5ya2eJ2FjI9xoXdfykVrpZAdTrzoDs0uzj3DvNFWyvJ5XMQFZApcZBNqScLY16jq4wXSvCZBiHEb1b3EQdKCI5iuhSU3kpC3DNH3eQhvisZBXKMMqjbfwZDZD"
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

def enviar_bienvenida(number):
    number = normalizar_numero_mx(number)

    data_imagen = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "image",
        "image": {
            "link": "https://github.com/bocalapaz-lab/chatbot/blob/main/logo%20boca_page-0001.jpg?raw=true"
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
                "6️⃣ Ayuda personalizada\n\n"
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
                "6️⃣ Ayuda personalizada\n\n"
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
            "address": "Av. Rosendo Márquez 16, 50 Doctors Torres Médicas V, La Paz, 72160 Heroica Puebla de Zaragoza, Pue."
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
                "Av. Rosendo Márquez 16, 50 Doctors Torres Médicas V\n"
                "La Paz, 72160 Heroica Puebla de Zaragoza, Pue.\n\n"
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
                "Si es necesario, puedes intentar comunicarte directamente"
                "al teléfono del consultorio.\n\n"
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
            "footer": {
                "text": "Selecciona una opción"
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": "btnmensaje",
                            "title": "Hablar con nosotros"
                        }
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "btnllamada",
                            "title": "Solicitar llamada"
                        }
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
                "➡️ Escribe *0* para volver al menú principal."
            )
        }
    }
    enviar_payload(data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=True)