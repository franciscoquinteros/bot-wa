from flask import Flask, request, jsonify
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
import requests
from twilio.rest import Client

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

# Configuración de Aisensy
AISENSY_API_KEY = os.environ.get("AISENSY_API_KEY")
AISENSY_INSTANCE_ID = os.environ.get("AISENSY_INSTANCE_ID")
AISENSY_API_URL = f"https://backend.aisensy.com/api/v1/campaign/{AISENSY_INSTANCE_ID}/sendMessage"

def send_twilio_message(phone_number, message):
    try:
        account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
        if not account_sid or not auth_token:
            logger.error("Credenciales de Twilio no configuradas")
            raise ValueError("Credenciales de Twilio no configuradas")
        client = Client(account_sid, auth_token)
        phone = phone_number.replace('whatsapp:', '').strip()
        message = client.messages.create(
            from_="whatsapp:+5491139164058",
            body=message,
            to=f"whatsapp:{phone}"
        )
        logger.info(f"Mensaje enviado a {phone}: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"Error al enviar mensaje: {e}")
        return False


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
            r"(?i)^help$",
            r"(?i)cómo\s+funciona",
            r"(?i)cómo\s+usar"
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


# Configuración de OpenAI (con manejo de importación segura)
OPENAI_AVAILABLE = False
try:
    import openai
    # Inicializar sin proxies
    openai.api_key = os.environ.get("OPENAI_API_KEY")
    # Verificar si la clave está disponible
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

def analyze_guests_with_ai(guest_list, category_info=None):
    """
    Usa OpenAI para extraer y estructurar la información de los invitados
    con soporte para formato con categorías
    
    Args:
        guest_list (list): Lista de líneas con información de invitados
        category_info (dict, optional): Información sobre categorías detectadas
        
    Returns:
        list: Lista de diccionarios con información estructurada de invitados
    """
    try:
        if not OPENAI_AVAILABLE or openai is None:
            logger.warning("OpenAI no está disponible, usando análisis básico para invitados")
            return None
        
        # Convertir la lista de invitados a texto para el prompt
        guests_text = "\n".join(guest_list)
        
        # Si hay información de categoría, incluirla en el prompt
        category_context = ""
        if category_info:
            category_context = "Ten en cuenta que los invitados están agrupados por categorías. "
            for category, lines in category_info.items():
                category_context += f"La categoría '{category}' incluye {len(lines)} invitados. "
        
        prompt = f"""
        A continuación hay una lista de invitados. {category_context}Por favor, extrae y estructura la información de cada invitado en formato JSON.
        
        Reglas importantes:
        1. Cada línea o entrada debe corresponder exactamente a un invitado.
        2. Cada invitado debe tener un nombre y un email asociado.
        3. Si ves un guión o un separador entre el nombre y el email, úsalo para separarlos.
        4. Si una línea incluye "Hombres:" o "Mujeres:", es un encabezado de categoría, no un invitado.
        5. El género debe ser "Masculino" si está en la categoría "Hombres" y "Femenino" si está en "Mujeres".
        
        Para cada invitado, identifica estos campos:
        - nombre: solo el primer nombre de la persona
        - apellido: solo el apellido de la persona
        - email: el email de la persona (debe haber exactamente un email por invitado)
        - genero: "Masculino", "Femenino" u "Otro" basado en el contexto y nombre
        
        Lista de invitados:
        {guests_text}
        
        Responde solo con un array JSON. Cada elemento del array debe corresponder a un invitado único con su email.
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
        logger.info(f"Respuesta IA para invitados: {result_text[:100]}...")
        
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
        
        # Buscar el array JSON dentro de la respuesta como fallback
        if "{" in result_text and "[" in result_text:
            array_match = re.search(r'\[(.*?)\]', result_text, re.DOTALL)
            if array_match:
                array_text = f"[{array_match.group(1)}]"
                try:
                    structured_guests = json.loads(array_text)
                    return structured_guests
                except:
                    pass
        
        return None
        
    except Exception as e:
        logger.error(f"Error al analizar invitados con OpenAI: {e}")
        return None

def parse_message(message):
    """
    Analiza el mensaje para identificar el comando, los datos y las categorías
    
    Args:
        message (str): Mensaje del usuario
        
    Returns:
        dict: Información sobre el comando, datos y categorías detectadas
    """
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
                'data': None,
                'categories': None
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
                'data': None,
                'categories': None
            }
    
    # Extraer invitados y categorías
    lines = message.split('\n')
    valid_lines = []
    categories = {}
    current_category = None
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Verificar si es un encabezado de categoría
        category_match = re.match(r'^(Hombres|Mujeres|Niños|Adultos|Familia)[\s:]*$', line, re.IGNORECASE)
        if category_match:
            current_category = category_match.group(1).capitalize()
            categories[current_category] = []
            continue
        
        # Si no es un encabezado y tiene contenido, agregarlo como línea válida
        if line and len(line) > 2:
            valid_lines.append(line)
            if current_category:
                categories[current_category] = categories.get(current_category, []) + [line]
    
    # Si no hay categorías pero hay líneas válidas, crear una categoría predeterminada
    if valid_lines and not categories:
        categories["General"] = valid_lines
    
    # Verificar si hay líneas válidas para procesar
    return {
        'command_type': 'add_guests' if valid_lines else 'unknown',
        'data': valid_lines,
        'categories': categories if categories else None
    }

def extract_guest_info_from_line(line, category=None):
    """
    Extrae nombre, apellido, email y género de una línea de texto
    
    Args:
        line (str): Línea de texto con información del invitado
        category (str, optional): Categoría a la que pertenece el invitado
        
    Returns:
        dict: Información estructurada del invitado
    """
    # Valores predeterminados
    guest_info = {
        "nombre": "",
        "apellido": "",
        "email": "",
        "genero": "Otro"
    }
    
    # Asignar género basado en la categoría si está disponible
    if category:
        if category.lower() == "hombres":
            guest_info["genero"] = "Masculino"
        elif category.lower() == "mujeres":
            guest_info["genero"] = "Femenino"
    
    # Buscar email
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', line)
    if email_match:
        guest_info["email"] = email_match.group(0)
        
        # Dividir la línea en función del email para obtener el nombre
        parts = line.split(email_match.group(0))
        name_part = parts[0].strip()
        
        # Eliminar caracteres especiales y separadores comunes
        name_part = re.sub(r'[-:,]', ' ', name_part).strip()
        
        # Dividir el nombre en palabras
        name_words = name_part.split()
        if name_words:
            guest_info["nombre"] = name_words[0]
            if len(name_words) > 1:
                guest_info["apellido"] = " ".join(name_words[1:])
    else:
        # Si no hay email, tratar toda la línea como nombre
        words = line.split()
        if words:
            guest_info["nombre"] = words[0]
            if len(words) > 1:
                guest_info["apellido"] = " ".join(words[1:])
    
    # Intentar determinar género si no se ha establecido por categoría
    if guest_info["genero"] == "Otro":
        nombre = guest_info["nombre"].lower()
        if nombre.endswith("a") or nombre.endswith("ia"):
            guest_info["genero"] = "Femenino"
        elif nombre.endswith("o") or nombre.endswith("io"):
            guest_info["genero"] = "Masculino"
    
    return guest_info

def extract_guests_manually(lines, categories=None):
    """
    Procesa manualmente las líneas de invitados cuando IA no está disponible
    
    Args:
        lines (list): Lista de líneas con información de invitados
        categories (dict, optional): Información sobre categorías detectadas
        
    Returns:
        list: Lista de diccionarios con información estructurada de invitados
    """
    guests = []
    
    if categories:
        # Procesar por categorías
        for category, category_lines in categories.items():
            for line in category_lines:
                guest_info = extract_guest_info_from_line(line, category)
                if guest_info["nombre"]:  # Solo agregar si hay al menos un nombre
                    guests.append(guest_info)
    else:
        # Procesar todas las líneas sin categorías
        for line in lines:
            guest_info = extract_guest_info_from_line(line)
            if guest_info["nombre"]:  # Solo agregar si hay al menos un nombre
                guests.append(guest_info)
    
    return guests

def add_guests_to_sheet(sheet, guests_data, phone_number, categories=None):
    """
    Agrega invitados a la hoja con información estructurada
    
    Args:
        sheet: Objeto de hoja de Google Sheets
        guests_data: Lista de líneas con datos de invitados
        phone_number: Número de teléfono del anfitrión
        categories (dict, optional): Información sobre categorías detectadas
        
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
        structured_guests = None
        
        # Primero intentar usar IA para procesar los datos
        if OPENAI_AVAILABLE and openai:
            structured_guests = analyze_guests_with_ai(guests_data, categories)
            
        # Si la IA falla o no está disponible, usar procesamiento manual
        if not structured_guests:
            structured_guests = extract_guests_manually(guests_data, categories)
        
        # Verificar que todos los invitados tengan email
        has_email_mismatch = False
        valid_guests = []
        for guest in structured_guests:
            if guest.get("email"):
                valid_guests.append(guest)
            else:
                has_email_mismatch = True
                logger.warning(f"Invitado sin email detectado: {guest.get('nombre')} {guest.get('apellido')}")
        
        # Si hay problemas con emails faltantes, devolver error específico
        if has_email_mismatch:
            logger.error("Se detectaron invitados sin email")
            return -1  # Código especial para indicar error de validación
        
        # Crear filas para añadir a la hoja
        rows_to_add = []
        for guest in valid_guests:
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
    """
    Cuenta invitados y recupera sus detalles, filtrados por número de teléfono en la columna 'Publica'
    
    Args:
        sheet: Objeto de hoja de Google Sheets
        phone_number (str): Número de teléfono del usuario que está consultando
        
    Returns:
        tuple: (dict con conteos por género, lista con detalles de invitados)
    """
    try:
        # Obtener todos los registros de la hoja
        all_data = sheet.get_all_records()
        
        # Verificar si hay datos
        if not all_data:
            logger.warning("La hoja no contiene datos o solo tiene encabezados")
            return {'Total': 0}, []
        
        # Loguear las primeras filas para verificar la estructura
        logger.info(f"Muestra de datos: {all_data[:2]}")
        
        # Filtrar por número de teléfono en la columna 'Publica'
        filtered_data = []
        if phone_number:
            # Normalizar el número de teléfono (eliminar '+' y espacios)
            normalized_phone = phone_number.replace('+', '').replace(' ', '')
            logger.info(f"Buscando invitados con teléfono normalizado: {normalized_phone}")
            
            for row in all_data:
                # Intentar encontrar la columna correcta
                phone_value = None
                for col in ['Publica', 'publica', 'Teléfono', 'telefono', 'Telefono', 'Phone']:
                    if col in row:
                        phone_value = str(row[col]).replace('+', '').replace(' ', '')
                        break
                
                # Si encontramos el teléfono y coincide, incluir esta fila
                if phone_value and phone_value == normalized_phone:
                    filtered_data.append(row)
        else:
            filtered_data = all_data
        
        # Loguear el número de invitados encontrados
        logger.info(f"Encontrados {len(filtered_data)} invitados para el teléfono {phone_number}")
        
        # Contar por género
        categories = {}
        for row in filtered_data:
            # Intentar obtener el género, con múltiples nombres posibles de columna
            gender = None
            for col in ['Genero', 'genero', 'Género', 'género', 'Gender']:
                if col in row:
                    gender = row[col]
                    break
            
            # Si no se encontró un género, usar "Sin categoría"
            category = gender if gender else 'Sin categoría'
            categories[category] = categories.get(category, 0) + 1
        
        # Agregar total
        categories['Total'] = len(filtered_data)
        
        logger.info(f"Conteo completo para {phone_number}: {categories}")
        return categories, filtered_data
    except Exception as e:
        logger.error(f"Error al contar invitados: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'Total': 0}, []

def generate_count_response(result, guests_data, phone_number, sentiment):
    """
    Genera una respuesta personalizada para la consulta de invitados con información detallada
    
    Args:
        result (dict): Resultados del conteo de invitados
        guests_data (list): Lista de diccionarios con detalles de invitados
        phone_number (str): Número de teléfono del usuario
        sentiment (str): Sentimiento detectado en el mensaje
        
    Returns:
        str: Respuesta personalizada
    """
    if not result or result.get('Total', 0) == 0:
        base_response = "No tienes invitados registrados aún con tu número de teléfono."
        
        # Añadir instrucciones si no hay invitados
        base_response += "\n\nPuedes añadir invitados usando este formato:\n\nHombres:\nJuan Pérez - juan@ejemplo.com\n\nMujeres:\nMaría López - maria@ejemplo.com"
    else:
        base_response = f"📋 Tus invitados registrados ({phone_number}):\n\n"
        
        # Mostrar conteo por género
        for category, count in result.items():
            if category != 'Total':
                # Formatear categoría para mejor visualización
                display_category = category
                if category.lower() == "masculino":
                    display_category = "Hombres"
                elif category.lower() == "femenino":
                    display_category = "Mujeres"
                
                base_response += f"- {display_category}: {count}\n"
        
        base_response += f"\nTotal: {result.get('Total', 0)} invitados\n\n"
        
        # Añadir información detallada de cada invitado agrupada por género
        base_response += "📝 Detalle de invitados:\n\n"
        
        # Agrupar invitados por género
        guests_by_gender = {}
        for guest in guests_data:
            gender = None
            for col in ['Genero', 'genero', 'Género', 'género', 'Gender']:
                if col in guest:
                    gender = guest[col]
                    break
            
            if not gender:
                gender = "Sin categoría"
                
            if gender not in guests_by_gender:
                guests_by_gender[gender] = []
            
            guests_by_gender[gender].append(guest)
        
        # Mostrar invitados por género
        for gender, guests in guests_by_gender.items():
            # Formatear género para mejor visualización
            display_gender = gender
            if gender.lower() == "masculino":
                display_gender = "Hombres"
            elif gender.lower() == "femenino":
                display_gender = "Mujeres"
                
            base_response += f"◾️ {display_gender}:\n"
            
            for guest in guests:
                # Obtener nombre y apellido
                nombre = guest.get('Nombre', '')
                apellido = guest.get('Apellido', '')
                email = guest.get('Email', '')
                
                # Añadir detalles del invitado
                base_response += f"   • {nombre} {apellido} - {email}\n"
            
            base_response += "\n"
    
    # Personalizar según sentimiento
    if sentiment == "positivo":
        return f"{base_response}\n¡Gracias por tu interés! ¿Necesitas añadir más invitados?"
    elif sentiment == "negativo":
        return f"{base_response}\n¿Hay algo específico en lo que pueda ayudarte con tu lista de invitados?"
    else:
        return base_response

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
        
        if count == -1:  # Error de validación (emails faltantes)
            return "⚠️ No se pudieron registrar todos los invitados. Por favor, asegúrate de que cada invitado tenga un email asociado. El formato correcto es: Nombre Apellido - email@ejemplo.com"
        elif count == 0:
            base_response = "No se pudieron registrar invitados. Por favor asegúrate de incluir información clara como: Juan Pérez - juan@example.com"
        elif count == 1:
            base_response = "✅ Se ha registrado 1 invitado correctamente."
        else:
            base_response = f"✅ Se han registrado {count} invitados correctamente."
        
        # Personalizar según sentimiento
        if sentiment == "positivo" and count > 0:
            return f"{base_response} ¡Gracias por usar nuestro servicio! ¿Deseas agregar más invitados?"
        elif sentiment == "negativo" and count > 0:
            return f"{base_response} Notamos cierta preocupación en tu mensaje. ¿Todo está bien con el registro?"
        else:
            return base_response
            
    elif command == 'help':
        help_text = """📱 *Ayuda del sistema de invitados*

Para agregar invitados, puedes usar estos formatos:

1) Por categorías:
```
Hombres:
Juan Pérez - juan@example.com 
Carlos Gómez - carlos@gmail.com

Mujeres:
María López - maria@example.com
Ana Rodríguez - ana@gmail.com
```

2) En formato libre:
```
Juan Pérez - juan@example.com
María López - maria@example.com
```

IMPORTANTE: Cada invitado debe tener un correo electrónico asociado.

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
        # Comando desconocido - usar la lógica de la IA
        if intent == "adición_invitado" or "agregar" in intent.lower():
            return """Para agregar invitados, puedes usar estos formatos:

1) Por categorías:
Hombres:
Juan Pérez - juan@example.com 
Carlos Gómez - carlos@gmail.com

2) En formato libre:
Juan Pérez - juan@example.com
María López - maria@example.com

IMPORTANTE: Cada invitado debe tener un correo electrónico asociado."""
        
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

- Agregar invitados enviando sus datos (nombre, apellido - email)
- Consultar tu lista con "cuántos invitados"
- Escribir "ayuda" para ver instrucciones detalladas"""

@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    sender_phone = None  # Define la variable al inicio
    try:
        data = request.form.to_dict()
        logger.info(f"Datos recibidos: {data}")
        
        sender_phone = data.get('From', '').replace('whatsapp:', '')
        incoming_msg = data.get('Body', '')
        
        if not incoming_msg or not sender_phone:
            logger.error("Payload inválido")
            return jsonify({"status": "error", "message": "Invalid payload"}), 400

        sentiment_analysis = analyze_sentiment(incoming_msg)
        parsed = parse_message(incoming_msg)
        command_type = parsed['command_type']
        data = parsed['data']
        categories = parsed['categories']

        sheet_conn = SheetsConnection()
        sheet = sheet_conn.get_sheet()

        if command_type == 'add_guests':
            result = add_guests_to_sheet(sheet, data, sender_phone, categories)
            response_text = generate_response(command_type, result, sender_phone, sentiment_analysis)
        elif command_type == 'count':
            result, guests_data = count_guests(sheet, sender_phone)
            response_text = generate_count_response(result, guests_data, sender_phone, sentiment_analysis['sentiment'])
        else:
            response_text = generate_response(command_type, None, sender_phone, sentiment_analysis)

        if not send_twilio_message(sender_phone, response_text):
            logger.error("Fallo al enviar mensaje de respuesta")
            return jsonify({"status": "error", "message": "Failed to send response"}), 500
        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"Error: {e}")
        if sender_phone:
            send_twilio_message(sender_phone, "Lo siento, hubo un error.")
        return jsonify({"status": "error"}), 500