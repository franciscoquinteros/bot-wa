from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import uuid
from datetime import datetime
import re
import logging
from functools import lru_cache
import time
import os
import json

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("whatsapp_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuración de OpenAI (con manejo de importación segura)
OPENAI_AVAILABLE = False
try:
    import openai
    openai.api_key = os.environ.get("OPENAI_API_KEY")
    OPENAI_AVAILABLE = bool(openai.api_key)
    logger.info(f"OpenAI está {'disponible' if OPENAI_AVAILABLE else 'NO disponible (falta API key)'}")
except ImportError:
    logger.warning("Módulo OpenAI no está instalado. Se usará análisis básico.")
    openai = None

# Manejo de la conexión con Google Sheets
class SheetsConnection:
    _instance = None
    _last_refresh = 0
    _refresh_interval = 1800  # 30 minutos

    def __new__(cls):
        if cls._instance is None or time.time() - cls._last_refresh > cls._refresh_interval:
            cls._instance = super(SheetsConnection, cls).__new__(cls)
            cls._instance._connect()
            cls._last_refresh = time.time()
        return cls._instance
    
    def _connect(self):
        try:
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/google-credentials.json", scope)
            self.client = gspread.authorize(creds)
            self.sheet = self.client.open("n8n sheet").sheet1
            logger.info("Conexión con Google Sheets establecida con éxito")
        except Exception as e:
            logger.error(f"Error al conectar con Google Sheets: {e}")
            raise
        
    def get_sheet(self):
        return self.sheet

# Funciones de análisis de sentimientos
def analyze_sentiment(text):
    """
    Analiza el sentimiento y la intención del mensaje del usuario usando OpenAI
    
    Args:
        text (str): El mensaje del usuario
        
    Returns:
        dict: Diccionario con análisis del sentimiento e intención
    """
    try:
        if not OPENAI_AVAILABLE or openai is None:
            logger.warning("OpenAI no está disponible, usando análisis básico")
            return analyze_with_rules(text)
            
        # Usar la API de OpenAI para analizar el sentimiento
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres un asistente que analiza mensajes. Responde solo con un JSON que contiene: sentiment (positivo, negativo o neutral), intent (pregunta, solicitud, queja, adición_invitado, consulta_invitados, otro), y urgency (baja, media, alta)."},
                {"role": "user", "content": text}
            ],
            response_format={"type": "json_object"}
        )
        
        # Obtener la respuesta como JSON
        analysis_text = response.choices[0].message.content
        analysis = json.loads(analysis_text)
        
        logger.info(f"Análisis de sentimiento OpenAI: {analysis}")
        return analysis
        
    except Exception as e:
        logger.error(f"Error al analizar sentimiento con OpenAI: {e}")
        # En caso de error, usar análisis basado en reglas
        return analyze_with_rules(text)

def analyze_guests_with_ai(guest_list):
    """
    Usa OpenAI para extraer y estructurar la información de los invitados
    
    Args:
        guest_list (list): Lista de líneas con información de invitados
        
    Returns:
        list: Lista de diccionarios con información estructurada de invitados
    """
    try:
        if not OPENAI_AVAILABLE or openai is None:
            logger.warning("OpenAI no está disponible, usando análisis básico para invitados")
            return None
        
        # Convertir la lista de invitados a texto para el prompt
        guests_text = "\n".join(guest_list)
        
        prompt = f"""
        A continuación hay una lista de invitados. Por favor, extrae y estructura la información de cada invitado en formato JSON.
        Para cada invitado, identifica estos campos:
        - nombre: solo el primer nombre de la persona
        - apellido: solo el apellido de la persona
        - email: el email si está presente, si no lo está, deja el campo vacío
        - genero: "Masculino", "Femenino" u "Otro" basado en el contexto y nombre
        
        Lista de invitados:
        {guests_text}
        
        Responde solo con un array JSON. Ejemplo:
        [
          {{"nombre": "Juan", "apellido": "Pérez", "email": "juan@example.com", "genero": "Masculino"}},
          {{"nombre": "María", "apellido": "Gómez", "email": "", "genero": "Femenino"}}
        ]
        """
        
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres un asistente especializado en extraer información estructurada de textos."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        
        # Obtener la respuesta como JSON
        result_text = response.choices[0].message.content
        
        # A veces OpenAI puede envolver la respuesta en un objeto, buscamos el array
        if "{" in result_text and "[" in result_text:
            # Buscar el array JSON dentro de la respuesta
            array_match = re.search(r'\[(.*?)\]', result_text, re.DOTALL)
            if array_match:
                array_text = f"[{array_match.group(1)}]"
                try:
                    structured_guests = json.loads(array_text)
                    return structured_guests
                except:
                    pass
        
        # Intentar parsear directamente
        try:
            structured_data = json.loads(result_text)
            # Verificar si es un array o si tiene una propiedad que contiene el array
            if isinstance(structured_data, list):
                return structured_data
            for key, value in structured_data.items():
                if isinstance(value, list):
                    return value
        except Exception as e:
            logger.error(f"Error al parsear JSON de OpenAI: {e}")
        
        return None
        
    except Exception as e:
        logger.error(f"Error al analizar invitados con OpenAI: {e}")
        return None

def analyze_with_rules(text):
    """
    Analiza el texto utilizando reglas simples cuando OpenAI no está disponible
    
    Args:
        text (str): El mensaje del usuario
        
    Returns:
        dict: Análisis básico del mensaje
    """
    # Patrones para detectar intenciones mediante expresiones regulares
    patterns = {
        "adición_invitado": [
            r"(?i)agregar",
            r"(?i)añadir",
            r"(?i)sumar",
            r"(?i)incluir",
            r"(?i)hombres\s*\n",
            r"(?i)mujeres\s*\n"
        ],
        "consulta_invitados": [
            r"(?i)cuántos",
            r"(?i)cantidad",
            r"(?i)lista",
            r"(?i)invitados\s+tengo",
            r"(?i)ver\s+invitados"
        ],
        "ayuda": [
            r"(?i)^ayuda$",
            r"(?i)cómo\s+funciona",
            r"(?i)instrucciones"
        ]
    }
    
    # Detectar la intención según los patrones
    intent = "otro"
    for intent_name, patterns_list in patterns.items():
        for pattern in patterns_list:
            if re.search(pattern, text):
                intent = intent_name
                break
        if intent != "otro":
            break
    
    # Análisis de sentimiento básico basado en palabras clave
    positive_words = ["gracias", "excelente", "genial", "bueno", "perfecto", "bien"]
    negative_words = ["error", "problema", "mal", "falla", "no funciona", "arregla"]
    
    text_lower = text.lower()
    sentiment = "neutral"
    
    for word in positive_words:
        if word in text_lower:
            sentiment = "positivo"
            break
            
    for word in negative_words:
        if word in text_lower:
            sentiment = "negativo"
            break
    
    # Determinar urgencia basado en signos de exclamación y palabras clave de urgencia
    urgency = "media"
    if text.count("!") > 1 or any(w in text_lower for w in ["urgente", "inmediato", "rápido", "ya"]):
        urgency = "alta"
    
    return {
        "sentiment": sentiment,
        "intent": intent,
        "urgency": urgency
    }

# Funciones para procesar mensajes
def parse_message(message):
    """Analiza el mensaje para identificar el comando y los datos"""
    message = message.strip()
    
    # Verificar si es una consulta de conteo
    count_patterns = [
        r'cu[aá]ntos invitados',
        r'contar invitados',
        r'total de invitados',
        r'invitados totales',
        r'lista de invitados'
    ]
    
    for pattern in count_patterns:
        if re.search(pattern, message.lower()):
            return {
                'command_type': 'count',
                'data': None
            }
    
    # Verificar si es una solicitud de ayuda
    help_patterns = [
        r'^ayuda$',
        r'^help$',
        r'c[oó]mo funciona',
        r'c[oó]mo usar'
    ]
    
    for pattern in help_patterns:
        if re.search(pattern, message.lower()):
            return {
                'command_type': 'help',
                'data': None
            }
    
    # Extraer invitados - ahora solo detectamos líneas de texto que podrían ser invitados
    lines = message.split('\n')
    valid_lines = []
    
    for line in lines:
        line = line.strip()
        if line and len(line) > 2:  # Ignorar líneas demasiado cortas
            valid_lines.append(line)
    
    return {
        'command_type': 'add_guests' if valid_lines else 'unknown',
        'data': valid_lines
    }

def extract_guest_info(guest_line):
    """
    Extrae información de invitado de una línea de texto
    usando reglas básicas cuando IA no está disponible
    
    Args:
        guest_line (str): Línea con información del invitado
        
    Returns:
        dict: Información estructurada del invitado
    """
    # Dividir por espacios para obtener nombre y apellido
    parts = guest_line.split()
    
    # Valores predeterminados
    guest_info = {
        "nombre": "",
        "apellido": "",
        "email": "",
        "genero": "Otro"
    }
    
    # Buscar email
    email_match = re.search(r'[\w\.-]+@[\w\.-]+', guest_line)
    if email_match:
        guest_info["email"] = email_match.group(0)
        # Eliminar el email del texto para procesamiento posterior
        guest_line = guest_line.replace(email_match.group(0), "").strip()
        parts = guest_line.split()
    
    # Asignar nombre y apellido
    if len(parts) >= 2:
        guest_info["nombre"] = parts[0]
        guest_info["apellido"] = " ".join(parts[1:])
    elif len(parts) == 1:
        guest_info["nombre"] = parts[0]
    
    # Intentar determinar género basado en terminaciones comunes
    nombre = guest_info["nombre"].lower()
    if nombre.endswith("a") or nombre.endswith("ia"):
        guest_info["genero"] = "Femenino"
    elif nombre.endswith("o") or nombre.endswith("io"):
        guest_info["genero"] = "Masculino"
    
    return guest_info

def add_guests_to_sheet(sheet, guests_data, phone_number):
    """
    Agrega invitados a la hoja con información estructurada
    
    Args:
        sheet: Objeto de hoja de Google Sheets
        guests_data: Lista de líneas o diccionarios con datos de invitados
        phone_number: Número de teléfono del anfitrión
        
    Returns:
        int: Número de invitados añadidos
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Verificar si la hoja tiene los encabezados correctos
        headers = sheet.row_values(1)
        if not headers or len(headers) < 5:
            sheet.update('A1:E1', [['Nombre', 'Apellido', 'Email', 'Genero', 'Publica']])
        
        # Procesar datos de invitados
        rows_to_add = []
        
        # Primero intentar usar IA para procesar los datos
        if isinstance(guests_data, list) and all(isinstance(item, str) for item in guests_data):
            # Lista de strings - intentar procesar con IA
            structured_guests = analyze_guests_with_ai(guests_data)
            
            if structured_guests:
                # Usar datos estructurados de IA
                for guest in structured_guests:
                    rows_to_add.append([
                        guest.get("nombre", ""),
                        guest.get("apellido", ""),
                        guest.get("email", ""),
                        guest.get("genero", "Otro"),
                        phone_number
                    ])
            else:
                # Fallback a reglas básicas
                for line in guests_data:
                    guest_info = extract_guest_info(line)
                    rows_to_add.append([
                        guest_info["nombre"],
                        guest_info["apellido"],
                        guest_info["email"],
                        guest_info["genero"],
                        phone_number
                    ])
        else:
            # Ya es una lista de diccionarios
            for guest in guests_data:
                rows_to_add.append([
                    guest.get("nombre", ""),
                    guest.get("apellido", ""),
                    guest.get("email", ""),
                    guest.get("genero", "Otro"),
                    phone_number
                ])
        
        # Agregar a la hoja
        if rows_to_add:
            sheet.append_rows(rows_to_add)
            logger.info(f"Agregados {len(rows_to_add)} invitados para el teléfono {phone_number}")
        
        return len(rows_to_add)
    except Exception as e:
        logger.error(f"Error al agregar invitados: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 0

def count_guests(sheet, phone_number=None):
    """Cuenta invitados, opcionalmente filtrados por número de teléfono"""
    try:
        all_data = sheet.get_all_records()
        
        if phone_number:
            filtered_data = [row for row in all_data if str(row.get('Publica')) == phone_number]
        else:
            filtered_data = all_data
        
        # Contar por género
        categories = {}
        for row in filtered_data:
            category = row.get('Genero', 'Sin categoría')
            categories[category] = categories.get(category, 0) + 1
        
        # Agregar total
        categories['Total'] = len(filtered_data)
        
        logger.info(f"Conteo completado para {phone_number}: {categories}")
        return categories
    except Exception as e:
        logger.error(f"Error al contar invitados: {e}")
        return {'Total': 0}

def generate_response(command, result, phone_number=None, sentiment_analysis=None):
    """
    Genera respuestas personalizadas basadas en el comando, resultado y análisis de sentimiento
    
    Args:
        command (str): Tipo de comando detectado
        result: Resultado de la ejecución del comando
        phone_number (str, opcional): Número de teléfono del usuario
        sentiment_analysis (dict, opcional): Análisis de sentimiento del mensaje
    
    Returns:
        str: Respuesta personalizada
    """
    # Si no hay análisis de sentimiento, usar comportamiento original
    if sentiment_analysis is None:
        sentiment_analysis = {
            "sentiment": "neutral",
            "intent": "otro",
            "urgency": "media"
        }
    
    sentiment = sentiment_analysis.get("sentiment", "neutral")
    intent = sentiment_analysis.get("intent", "otro")
    urgency = sentiment_analysis.get("urgency", "media")
    
    # Para comandos específicos, mantener la lógica original pero añadir personalización
    if command == 'count':
        if not result or result.get('Total', 0) == 0:
            base_response = "No tienes invitados registrados aún."
        else:
            base_response = "📋 Resumen de invitados:\n\n"
            for category, count in result.items():
                if category != 'Total':
                    base_response += f"- {category}: {count}\n"
            
            base_response += f"\nTotal: {result.get('Total', 0)} invitados"
        
        # Personalizar según sentimiento
        if sentiment == "positivo":
            return f"{base_response}\n\n¡Gracias por tu interés! ¿Necesitas añadir más invitados?"
        elif sentiment == "negativo":
            return f"{base_response}\n\nNotamos que podrías estar preocupado. ¿Hay algo específico en lo que podamos ayudarte con tu lista?"
        else:
            return base_response
        
    elif command == 'add_guests':
        count = result
        base_response = ""
        
        if count == 0:
            base_response = "No se pudieron registrar invitados. Por favor asegúrate de incluir información clara como: Juan Pérez juan@example.com"
        elif count == 1:
            base_response = "✅ Se ha registrado 1 invitado correctamente."
        else:
            base_response = f"✅ Se han registrado {count} invitados correctamente."
        
        # Personalizar según sentimiento
        if sentiment == "positivo":
            return f"{base_response} ¡Gracias por usar nuestro servicio! ¿Deseas agregar más invitados?"
        elif sentiment == "negativo" and count > 0:
            return f"{base_response} Notamos cierta preocupación en tu mensaje. ¿Todo está bien con el registro?"
        else:
            return base_response
            
    elif command == 'help':
        help_text = """📱 *Ayuda del sistema de invitados*

Para agregar invitados, simplemente envía sus datos en formato libre. Por ejemplo:

```
Juan Pérez juan@example.com
María Gómez
Pedro Sánchez pedro.sanchez@gmail.com
```

El sistema identificará automáticamente nombres, apellidos, correos electrónicos y género.

Para consultar:
- Escribe "cuántos invitados" para ver el total
- También puedes escribir "lista de invitados"
"""

        # Personalizar según sentimiento
        if sentiment == "negativo" and urgency == "alta":
            return f"Entendemos tu frustración. Estamos aquí para ayudarte de inmediato.\n\n{help_text}"
        else:
            return help_text
    
    else:
        # Si el comando no se reconoce, responder según el análisis de sentimiento
        if intent == "adición_invitado" or "agregar" in intent.lower():
            return "Para agregar invitados, envía sus datos línea por línea. Por ejemplo:\n\nJuan Pérez juan@example.com\nMaría Gómez\nPedro Sánchez pedro@gmail.com"
        
        elif intent == "consulta_invitados" or "consultar" in intent.lower():
            return "Para ver tus invitados, escribe 'cuántos invitados tengo' o 'lista de invitados'."
        
        elif sentiment == "positivo":
            return "¡Gracias por tu mensaje! Para gestionar tu lista de invitados, puedes añadir invitados enviando sus datos o consultar tu lista escribiendo 'cuántos invitados'."
        
        elif sentiment == "negativo":
            if urgency == "alta":
                return "Lamento la inconveniencia. Tu problema es importante para nosotros. ¿Podrías explicar con más detalle qué necesitas? Estamos aquí para ayudarte."
            else:
                return "Entiendo tu frustración. Estamos trabajando para mejorar nuestro servicio. ¿Puedo ayudarte con algo específico sobre tu lista de invitados?"
        
        else:
            return """No pude entender tu mensaje. Puedes:

- Agregar invitados enviando sus datos (nombre, apellido, email)
- Consultar tu lista con "cuántos invitados"
- Escribir "ayuda" para ver instrucciones detalladas"""

# Rutas de la aplicación
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    try:
        # Obtener datos de la solicitud
        incoming_msg = request.values.get('Body', '')
        sender_phone = request.values.get('From', '').replace('whatsapp:', '')
        
        logger.info(f"Mensaje recibido de {sender_phone}: {incoming_msg[:50]}...")
        
        # Analizar el sentimiento del mensaje
        sentiment_analysis = analyze_sentiment(incoming_msg)
        logger.info(f"Análisis de sentimiento: {sentiment_analysis}")
        
        # Procesar el mensaje con la lógica original
        parsed = parse_message(incoming_msg)
        command_type = parsed['command_type']
        data = parsed['data']
        
        # Si el análisis de IA detectó una intención específica, sobreescribir el comando
        ai_intent = sentiment_analysis.get("intent", "").lower()
        if command_type == "unknown" and ai_intent in ["adición_invitado", "consulta_invitados"]:
            if ai_intent == "adición_invitado":
                logger.info("IA detectó intención de añadir invitados, pero el formato no es el esperado")
                command_type = "unknown_add"
            elif ai_intent == "consulta_invitados":
                logger.info("IA detectó intención de consultar invitados")
                command_type = "count"
                data = None
        
        # Obtener la conexión a la hoja
        sheet_conn = SheetsConnection()
        sheet = sheet_conn.get_sheet()
        
        # Ejecutar el comando correspondiente
        result = None
        if command_type == 'add_guests':
            result = add_guests_to_sheet(sheet, data, sender_phone)
        elif command_type == 'count':
            result = count_guests(sheet, sender_phone)
        
        # Generar respuesta personalizada según sentimiento
        response_text = generate_response(command_type, result, sender_phone, sentiment_analysis)
        
        # Enviar respuesta
        resp = MessagingResponse()
        msg = resp.message()
        msg.body(response_text)
        
        logger.info(f"Respuesta enviada a {sender_phone}")
        return str(resp)
        
    except Exception as e:
        logger.error(f"Error en el procesamiento del mensaje: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        # Respuesta de error
        resp = MessagingResponse()
        msg = resp.message()
        msg.body("Lo siento, hubo un error en el sistema. Inténtalo más tarde.")
        return str(resp)

@app.route('/health', methods=['GET'])
def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.route('/test-ai', methods=['GET'])
def test_ai():
    """Ruta para probar el análisis de invitados con IA"""
    text = request.args.get('text', 'Juan Pérez juan@example.com\nMaría Gómez')
    lines = text.split('\n')
    analysis = analyze_guests_with_ai(lines)
    return {
        "text": text,
        "structured_data": analysis,
        "openai_available": OPENAI_AVAILABLE
    }

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)