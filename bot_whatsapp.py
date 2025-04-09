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

user_states = {}

# --- Constantes para los estados ---
STATE_INITIAL = None
STATE_AWAITING_EVENT_SELECTION = 'AWAITING_EVENT_SELECTION'
STATE_AWAITING_GUEST_DATA = 'AWAITING_GUEST_DATA'

# Configuración de Twilio
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER')

def send_twilio_message(phone_number, message):
    """ Envía un mensaje de WhatsApp usando Twilio """
    # Asegurarse que el número tenga el prefijo 'whatsapp:'
    if not phone_number.startswith('whatsapp:'):
        destination_number = f"whatsapp:{phone_number}"
    else:
        destination_number = phone_number

    # Asegurarse que el número de origen tenga el prefijo 'whatsapp:'
    if not TWILIO_WHATSAPP_NUMBER:
         logger.error("Número de WhatsApp de Twilio (TWILIO_WHATSAPP_NUMBER) no configurado.")
         return False
    if not TWILIO_WHATSAPP_NUMBER.startswith('whatsapp:'):
        origin_number = f"whatsapp:{TWILIO_WHATSAPP_NUMBER}"
    else:
        origin_number = TWILIO_WHATSAPP_NUMBER

    try:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            logger.error("Credenciales de Twilio (SID o Token) no configuradas.")
            return False

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        twilio_message = client.messages.create(
            from_=origin_number,
            body=message,
            to=destination_number
        )
        logger.info(f"Mensaje enviado a {destination_number}: {twilio_message.sid}")
        return True
    except Exception as e:
        logger.error(f"Error al enviar mensaje de Twilio a {destination_number}: {e}")
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
        ],
        "saludo": [
            r"(?i)^hola$",
            r"(?i)^buenos días$",
            r"(?i)^buenas tardes$",
            r"(?i)^buenas noches$",
            r"(?i)^saludos$",
            r"(?i)^hi$",
            r"(?i)^hey$",
            r"(?i)^hello$",
            r"(?i)^ola$",
            r"(?i)^buen día$"
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
    from openai import OpenAI  # Cambiar la importación para la nueva versión
    
    # Verificar si la clave API está disponible
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        # Inicializar el cliente de forma correcta
        client = OpenAI(api_key=api_key)
        OPENAI_AVAILABLE = True
        logger.info("OpenAI está disponible")
    else:
        logger.warning("OpenAI NO disponible (falta API key)")
except ImportError:
    logger.warning("Módulo OpenAI no está instalado. Se usará análisis básico.")
    client = None

# --- Conexión a Google Sheets ---
class SheetsConnection:
    _instance = None
    _last_refresh = 0
    _refresh_interval = 1800  # 30 minutos
    _phone_cache_interval = 300

    def __new__(cls):
        if cls._instance is None or time.time() - cls._last_refresh > cls._refresh_interval:
            cls._instance = super(SheetsConnection, cls).__new__(cls)
            cls._instance._connect()
            cls._last_refresh = time.time()
        return cls._instance

    def _connect(self):
        try:
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds_path = os.environ.get("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/google-credentials.json")
            creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
            self.client = gspread.authorize(creds)
            self.spreadsheet = self.client.open("n8n sheet") # Nombre del Archivo Google Sheet

            # --- Obtener hojas principales (manejar si no existen) ---
            try:
                self.guest_sheet = self.spreadsheet.worksheet("Invitados")
            except gspread.exceptions.WorksheetNotFound:
                 logger.error("Hoja 'Invitados' no encontrada. Intentando crearla.")
                 # Ajusta las columnas/headers según necesites
                 try:
                    self.guest_sheet = self.spreadsheet.add_worksheet(title="Invitados", rows="1", cols="7")
                    self.guest_sheet.update('A1:G1', [['Nombre', 'Apellido', 'Email', 'Genero', 'Publica', 'Evento', 'Timestamp']])
                 except Exception as create_err:
                    logger.error(f"No se pudo crear la hoja 'Invitados': {create_err}")
                    self.guest_sheet = None # Marcar como no disponible

            try:
                self.event_sheet = self.spreadsheet.worksheet("Eventos")
            except gspread.exceptions.WorksheetNotFound:
                 logger.warning("Hoja 'Eventos' no encontrada. Funcionalidad de eventos limitada.")
                 self.event_sheet = None # Marcar como no disponible

            # --- Verificar hoja Telefonos ---
            try:
                self.phone_sheet_obj = self.spreadsheet.worksheet("Telefonos")
                logger.info("Hoja 'Telefonos' encontrada.")
            except gspread.exceptions.WorksheetNotFound:
                logger.error("¡CRÍTICO! Hoja 'Telefonos' para autorización no encontrada. El bot no responderá a nadie.")
                self.phone_sheet_obj = None

            # ---> ¡AQUÍ! Inicializar atributos de caché en la instancia SIEMPRE <---
            self._phone_cache = None
            self._phone_cache_last_refresh = 0
            self._pr_name_map_cache = None # NUEVO: Cache para el mapeo tel -> nombre PR
            self._pr_name_map_last_refresh = 0 # NUEVO: Timestamp para caché de mapeo
            # _phone_cache_interval es constante de clase, está bien así.

            logger.info("Conexión y configuración inicial de SheetsConnection completada.")

        # Errores CRÍTICOS de conexión principal van aquí
        except gspread.exceptions.SpreadsheetNotFound:
            logger.error(f"Error CRÍTICO: No se encontró el Google Sheet llamado 'n8n sheet'. Verifica el nombre.")
            # Decide si relanzar el error o manejarlo de otra forma
            raise # Detiene la aplicación si no puede conectar
        except gspread.exceptions.APIError as api_err:
             logger.error(f"Error CRÍTICO de API de Google al conectar: {api_err}")
             raise
        except Exception as e:
            # Otro error inesperado durante la conexión inicial
            logger.error(f"Error CRÍTICO inesperado al conectar con Google Sheets: {e}")
            raise
        
    def get_sheet(self):
        return self.spreadsheet

    def get_guest_sheet(self):
        # Podrías añadir lógica para refrescar la conexión si es necesario aquí
        return self.guest_sheet

    # --- NUEVO: Función para obtener la hoja de eventos (si la usas) ---
    def get_event_sheet(self):
        try:
            return self.spreadsheet.worksheet("Eventos")
        except gspread.exceptions.WorksheetNotFound:
            logger.error("Hoja 'Eventos' no encontrada.")
            return None

    # --- NUEVO: Función para obtener eventos disponibles ---
    def get_available_events(self):
        try:
            event_sheet = self.get_event_sheet()
            if event_sheet:
                events = event_sheet.col_values(1)
                return [event for event in events if event]
            else:
                logger.warning("Hoja de eventos no disponible. Usando eventos de ejemplo.")
                return ["Fiesta Verano 2025", "Evento Corporativo Q2", "Lanzamiento X"]
        except Exception as e:
            logger.error(f"Error al obtener eventos: {e}")
            return []
    
    # --- NUEVO: Método para obtener y cachear números autorizados ---
    def get_authorized_phones(self):
        now = time.time()
        # Ahora self._phone_cache sí existirá (inicialmente None)
        if self._phone_cache is not None and now - self._phone_cache_last_refresh < self._phone_cache_interval:
             return self._phone_cache

        logger.info("Refrescando caché de números autorizados...")
        authorized_phones_set = set()
        try:
            # Usar el objeto ya obtenido (o None) en _connect
            phone_sheet = self.phone_sheet_obj
            if phone_sheet:
                phone_list_raw = phone_sheet.col_values(1)[1:] # Asume Col A, skip header
                for phone in phone_list_raw:
                    if phone:
                        normalized_phone = re.sub(r'\D', '', str(phone))
                        if normalized_phone:
                            authorized_phones_set.add(normalized_phone)
                logger.info(f"Cargados {len(authorized_phones_set)} números autorizados.")
            else:
                 logger.error("No se puede refrescar caché porque hoja 'Telefonos' no está disponible.")
                 # Mantenemos el caché vacío o el anterior si hubo error temporal
                 authorized_phones_set = self._phone_cache if self._phone_cache is not None else set()


            self._phone_cache = authorized_phones_set
            self._phone_cache_last_refresh = now
            return self._phone_cache

        # Manejar errores específicos al leer la hoja de teléfonos
        except gspread.exceptions.APIError as e:
             logger.error(f"Error de API al leer la hoja 'Telefonos': {e}. Usando caché anterior si existe.")
             return self._phone_cache if self._phone_cache is not None else set()
        except Exception as e:
            logger.error(f"Error inesperado al obtener números autorizados: {e}. Usando caché anterior si existe.")
            return self._phone_cache if self._phone_cache is not None else set()



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
        if not OPENAI_AVAILABLE or client is None:
            logger.warning("OpenAI no está disponible, usando análisis básico")
            return analyze_with_rules(text)
            
        # Usar la API de OpenAI para analizar el sentimiento
        response = client.chat.completions.create(
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

# Actualizar la función analyze_guests_with_ai también
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
        if not OPENAI_AVAILABLE or client is None:
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
        
        response = client.chat.completions.create(
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
# --- NUEVO: Método para obtener el mapeo Telefono -> Nombre PR ---
def get_phone_pr_mapping(self):
        """
        Obtiene un diccionario que mapea números de teléfono normalizados
        a los nombres de PR correspondientes desde la hoja 'Telefonos'.
        Usa caché para eficiencia.
        """
        now = time.time()
        # Usar _phone_cache_interval también para este mapeo
        if self._pr_name_map_cache is not None and now - self._pr_name_map_last_refresh < self._phone_cache_interval:
            return self._pr_name_map_cache

        logger.info("Refrescando caché de mapeo Telefono -> Nombre PR...")
        phone_to_pr_map = {}
        try:
            phone_sheet = self.phone_sheet_obj
            if phone_sheet:
                # Leer ambas columnas (A=Telefonos, B=PR) - Ajusta índices si es necesario
                # Usamos get_all_values para asegurar que las filas coincidan
                all_values = phone_sheet.get_all_values()
                if len(all_values) > 1: # Asegurar que hay datos además del encabezado
                    # Asumimos encabezados en la fila 1, empezamos desde la fila 2 (índice 1)
                    for row in all_values[1:]:
                        if len(row) >= 2: # Asegurar que la fila tiene al menos 2 columnas
                            raw_phone = row[0] # Columna A (índice 0)
                            pr_name = row[1]   # Columna B (índice 1)
                            if raw_phone and pr_name: # Solo procesar si ambos tienen valor
                                normalized_phone = re.sub(r'\D', '', str(raw_phone))
                                if normalized_phone:
                                    phone_to_pr_map[normalized_phone] = pr_name.strip()
                        else:
                            logger.warning(f"Fila incompleta en hoja 'Telefonos': {row}")
                logger.info(f"Creado mapeo para {len(phone_to_pr_map)} teléfonos a nombres PR.")
            else:
                logger.error("No se puede refrescar mapeo PR porque hoja 'Telefonos' no está disponible.")
                # Mantenemos el caché vacío o el anterior si hubo error temporal
                phone_to_pr_map = self._pr_name_map_cache if self._pr_name_map_cache is not None else {}

            self._pr_name_map_cache = phone_to_pr_map
            self._pr_name_map_last_refresh = now
            return self._pr_name_map_cache

        except gspread.exceptions.APIError as e:
            logger.error(f"Error de API al leer la hoja 'Telefonos' para mapeo PR: {e}. Usando caché anterior si existe.")
            return self._pr_name_map_cache if self._pr_name_map_cache is not None else {}
        except Exception as e:
            logger.error(f"Error inesperado al obtener mapeo PR: {e}. Usando caché anterior si existe.")
            return self._pr_name_map_cache if self._pr_name_map_cache is not None else {}


    
def extract_guests_from_split_format(lines):
    """
    Procesa el formato BLOQUES: Nombres primero, luego Emails, opcionalmente bajo categorías.
    Versión REVISADA para mayor robustez.

    Args:
        lines (list): Lista de líneas crudas del mensaje del usuario.

    Returns:
        list: Lista de diccionarios con info estructurada, o lista vacía si hay error grave.
              {'nombre': str, 'apellido': str, 'email': str, 'genero': str}
    """
    guests = []
    # Usaremos listas separadas por categoría para nombres y emails
    data_by_category = {} # Ejemplo: {'Hombres': {'names': [], 'emails': []}, 'Mujeres': {...}}
    category_map = {"Hombres": "Masculino", "Mujeres": "Femenino"}

    current_category_key = None # Empezar sin categoría definida
    parsing_mode = 'category_or_names' # Estados: category_or_names, names, emails

    logger.info("Iniciando extracción REVISADA formato dividido (Nombres -> Emails)...")

    for line in lines:
        line = line.strip()
        if not line:
            continue # Ignorar líneas vacías completamente

        # --- Detectar Categorías ---
        is_category = False
        potential_category_key = None
        if line.lower().startswith('hombres'):
            potential_category_key = 'Hombres'
            is_category = True
        elif line.lower().startswith('mujeres'):
            potential_category_key = 'Mujeres'
            is_category = True

        if is_category:
            current_category_key = potential_category_key
            # Si la categoría no existe en nuestro dict, la inicializamos
            if current_category_key not in data_by_category:
                data_by_category[current_category_key] = {'names': [], 'emails': []}
            parsing_mode = 'names' # Después de una categoría, esperamos nombres
            logger.debug(f"Categoría detectada/cambiada a: '{current_category_key}', modo: {parsing_mode}")
            continue # Línea de categoría procesada

        # Si no se definió una categoría explícita, usamos 'General'
        if current_category_key is None:
             current_category_key = 'General'
             if current_category_key not in data_by_category:
                 data_by_category[current_category_key] = {'names': [], 'emails': []}
             # Si la primera línea útil no es categoría, asumimos que es un nombre
             parsing_mode = 'names'


        # --- Detectar Emails ---
        is_email = False
        # Regex más estricto para emails válidos
        if re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", line):
             is_email = True

        if is_email:
             # Cambiar a modo email si veníamos de modo names
             if parsing_mode != 'emails':
                  logger.debug(f"Cambiando a modo 'emails' para categoría '{current_category_key}' al encontrar: {line}")
                  parsing_mode = 'emails'
             # Añadir email a la categoría actual
             if current_category_key in data_by_category: # Asegurarse que la categoría fue inicializada
                data_by_category[current_category_key]['emails'].append(line)
                logger.debug(f"Email agregado a '{current_category_key}': {line}")
             else:
                 # Esto no debería pasar si la lógica de inicialización es correcta
                 logger.error(f"Intento de agregar email '{line}' a categoría no inicializada '{current_category_key}'")
             continue # Línea de email procesada

        # --- Si no es Vacía, Categoría ni Email: Asumir Nombre ---
        # Solo si estamos en modo 'names' (o 'category_or_names' que cambia a 'names')
        if parsing_mode == 'names' or parsing_mode == 'category_or_names':
             if parsing_mode == 'category_or_names': # Primera línea útil es un nombre
                 parsing_mode = 'names'

             # Validar que parezca un nombre (letras y espacios) y no sea demasiado corto
             if re.match(r"^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s']+$", line) and len(line) > 2:
                 if current_category_key in data_by_category:
                     data_by_category[current_category_key]['names'].append(line)
                     logger.debug(f"Nombre agregado a '{current_category_key}': {line}")
                 else:
                     logger.error(f"Intento de agregar nombre '{line}' a categoría no inicializada '{current_category_key}'")
             else:
                 logger.warning(f"Línea '{line}' ignorada en modo '{parsing_mode}' para categoría '{current_category_key}'. No parece nombre válido.")
        elif parsing_mode == 'emails':
             # Ignorar texto después de empezar emails
             logger.warning(f"Ignorando línea '{line}' en modo 'emails' para categoría '{current_category_key}'.")


    # --- Emparejar Nombres y Emails por Categoría ---
    logger.info("Emparejando nombres y emails recolectados...")
    error_found_in_pairing = False
    for category_key, data in data_by_category.items():
        names = data['names']
        emails = data['emails']
        # Usar 'Otro' si la categoría es 'General' o no está en el map
        genero = category_map.get(category_key, "Otro")

        logger.info(f"Procesando categoría '{category_key}' ({genero}): {len(names)} nombres, {len(emails)} emails.")

        if not names and not emails:
            continue # Saltar categoría vacía

        if len(names) != len(emails):
            logger.error(f"¡ERROR DE FORMATO! Desbalance en categoría '{category_key}': {len(names)} nombres vs {len(emails)} emails. ¡No se agregarán invitados de esta categoría!")
            error_found_in_pairing = True
            continue # Saltar esta categoría por error grave

        if len(names) == 0: # Si hay emails pero no nombres (o viceversa, cubierto arriba)
             logger.error(f"Categoría '{category_key}' tiene {len(emails)} emails pero 0 nombres. Saltando.")
             error_found_in_pairing = True
             continue

        # Emparejar uno a uno
        for i in range(len(names)):
            full_name = names[i].strip()
            email = emails[i].strip() # Asegurar limpieza
            name_parts = full_name.split()
            nombre = ""
            apellido = ""

            if name_parts:
                nombre = name_parts[0]
                if len(name_parts) > 1:
                    apellido = " ".join(name_parts[1:])
            else:
                # Esto no debería ocurrir si validamos antes, pero por si acaso
                logger.warning(f"Nombre vacío detectado emparejado con email '{email}'. Saltando.")
                error_found_in_pairing = True
                continue

            guest_info = {
                "nombre": nombre,
                "apellido": apellido,
                "email": email,
                "genero": genero # Usar el género de la categoría
            }
            guests.append(guest_info)
            logger.debug(f"Invitado emparejado OK: {full_name} - {email} ({genero})")

    # Si hubo errores graves de formato (desbalance), podríamos querer indicarlo
    # if error_found_in_pairing:
        # Podríamos devolver None o una bandera especial, pero por ahora devolvemos los que sí se pudieron emparejar
        # logger.error("Se encontraron errores de formato (desbalance nombre/email) en al menos una categoría.")
        # return None # Opcional: Fallar toda la operación si hay errores

    logger.info(f"Extracción formato dividido completada. Total invitados estructurados: {len(guests)}")
    return guests

def parse_message(message):
    """
    Analiza el mensaje para identificar el comando, los datos y las categorías
    
    Args:
        message (str): Mensaje del usuario
        
    Returns:
        dict: Información sobre el comando, datos y categorías detectadas
    """
    message = message.strip()
    
    # Verificar si es un saludo simple
    saludo_patterns = [
        r'(?i)^hola$',
        r'(?i)^buenos días$',
        r'(?i)^buenas tardes$',
        r'(?i)^buenas noches$',
        r'(?i)^saludos$',
        r'(?i)^hi$',
        r'(?i)^hey$',
        r'(?i)^hello$',
        r'(?i)^ola$',
        r'(?i)^buen día$'
    ]
    
    for pattern in saludo_patterns:
        if re.search(pattern, message):
            return {
                'command_type': 'saludo',
                'data': None,
                'categories': None
            }
    
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

def parse_message_enhanced(message):
    """
    Versión mejorada de parse_message que detecta mejor el formato dividido
    de nombres y emails en líneas separadas
    
    Args:
        message (str): Mensaje del usuario
        
    Returns:
        dict: Información sobre el comando, datos y categorías detectadas
    """
    # Comprobar primero si es un saludo
    saludo_patterns = [
        r'(?i)^hola$',
        r'(?i)^buenos días$',
        r'(?i)^buenas tardes$',
        r'(?i)^buenas noches$',
        r'(?i)^saludos$',
        r'(?i)^hi$',
        r'(?i)^hey$',
        r'(?i)^hello$',
        r'(?i)^ola$',
        r'(?i)^buen día$'
    ]
    
    for pattern in saludo_patterns:
        if re.search(pattern, message.strip()):
            return {
                'command_type': 'saludo',
                'data': None,
                'categories': None
            }
    
    # Comprobación especial para formato separado antes de otras lógicas
    lines = message.strip().split('\n')
    # Filtrar líneas vacías
    valid_lines = [line.strip() for line in lines if line.strip()]
    
    # Si tenemos suficientes líneas para analizar
    if len(valid_lines) >= 4:  # Al menos algunos nombres y algunos emails
        # Contar emails y no-emails
        emails = [line for line in valid_lines if '@' in line and '.' in line.split('@')[1]]
        non_emails = [line for line in valid_lines if '@' not in line]
        
        # Detectar patrón: primero nombres, luego emails (con línea vacía opcional entre ellos)
        if (len(emails) >= 1 and len(non_emails) >= 1 and 
            abs(len(emails) - len(non_emails)) <= 2):  # Permitir pequeñas diferencias
            
            # Verificar que los emails están agrupados (no mezclados con nombres)
            email_indices = [i for i, line in enumerate(valid_lines) if '@' in line]
            if email_indices and max(email_indices) - min(email_indices) < len(emails):
                # Los emails están agrupados, es probable que sea formato dividido
                logger.info(f"Detectado formato dividido: {len(non_emails)} nombres, {len(emails)} emails")
                
                return {
                    'command_type': 'add_guests_split',
                    'data': valid_lines,
                    'categories': None
                }
    
    # Si no se detectó formato dividido, usar el parse_message original
    return parse_message(message)

def extract_guests_manually_enhanced(lines, categories=None, command_type='add_guests'):
    """
    Versión mejorada de extract_guests_manually que soporta múltiples formatos
    
    Args:
        lines (list): Lista de líneas con información de invitados
        categories (dict, optional): Información sobre categorías detectadas
        command_type (str): Tipo de comando detectado
        
    Returns:
        list: Lista de diccionarios con información estructurada de invitados
    """
    # Si es formato dividido, usar el extractor específico
    if command_type == 'add_guests_split':
        return extract_guests_from_split_format(lines)
    
    # Para el formato original, usar la lógica existente
    return extract_guests_manually(lines, categories)

# Modificación a la función add_guests_to_sheet para usar el nuevo extractor
def add_guests_to_sheet_enhanced(sheet, guests_data, phone_number, categories=None, command_type='add_guests'):
    """
    Versión mejorada de add_guests_to_sheet que soporta múltiples formatos
    
    Args:
        sheet: Objeto de hoja de Google Sheets
        guests_data: Lista de líneas con datos de invitados
        phone_number: Número de teléfono del anfitrión
        categories (dict, optional): Información sobre categorías detectadas
        command_type (str): Tipo de comando detectado
        
    Returns:
        int: Número de invitados añadidos
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Verificar si la hoja tiene los encabezados correctos
        headers = sheet.row_values(1)
        if not headers or len(headers) < 5:
            sheet.update('A1:E1', [['Nombre y Apellido', 'Email', 'Tipo de ticket', 'Responsable', 'Email de Responsable', "Nombre completo del responsable"]])
        
        # Procesar datos de invitados según el formato detectado
        structured_guests = None
        
        # Primero intentar usar IA para procesar los datos (solo para formato estándar)
        if command_type == 'add_guests' and OPENAI_AVAILABLE and client:
            structured_guests = analyze_guests_with_ai(guests_data, categories)
            
        # Si la IA falla, no está disponible, o es formato dividido, usar procesamiento manual
        if not structured_guests:
            structured_guests = extract_guests_manually_enhanced(guests_data, categories, command_type)
        
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

def extract_guest_info_from_line(line, category=None):
    """
    Extrae la información de un invitado a partir de una línea de texto
    
    Args:
        line (str): Línea con información del invitado (nombre - email)
        category (str, optional): Categoría del invitado (Hombres, Mujeres, etc.)
        
    Returns:
        dict: Diccionario con información estructurada del invitado
    """
    # Inicializar el diccionario con valores predeterminados
    guest_info = {
        "nombre": "",
        "apellido": "",
        "email": "",
        "genero": "Otro"
    }
    
    # Ignorar líneas vacías o demasiado cortas
    if not line or len(line.strip()) < 3:
        return guest_info
    
    # Detectar si hay un separador entre nombre y email
    separator = None
    if " - " in line:
        separator = " - "
    elif "-" in line:
        separator = "-"
    elif ":" in line:
        separator = ":"
    
    # Extraer nombre y email según el separador
    if separator:
        parts = line.split(separator, 1)
        if len(parts) == 2:
            name_part = parts[0].strip()
            email_part = parts[1].strip()
            
            # Asignar email si parece válido (tiene @ y un punto después)
            if "@" in email_part and "." in email_part.split("@")[1]:
                guest_info["email"] = email_part
            
            # Procesar nombre y apellido
            name_parts = name_part.split()
            if name_parts:
                guest_info["nombre"] = name_parts[0]
                if len(name_parts) > 1:
                    guest_info["apellido"] = " ".join(name_parts[1:])
        else:
            # Si no hay dos partes, intentar detectar el email directamente
            if "@" in line and "." in line.split("@")[1]:
                email_match = re.search(r'\S+@\S+\.\S+', line)
                if email_match:
                    guest_info["email"] = email_match.group(0)
                    # Quitar el email de la línea para extraer el nombre
                    name_part = line.replace(guest_info["email"], "").strip()
                    name_parts = name_part.split()
                    if name_parts:
                        guest_info["nombre"] = name_parts[0]
                        if len(name_parts) > 1:
                            guest_info["apellido"] = " ".join(name_parts[1:])
    else:
        # Si no hay separador, intentar extraer email directamente
        if "@" in line and "." in line.split("@")[1]:
            email_match = re.search(r'\S+@\S+\.\S+', line)
            if email_match:
                guest_info["email"] = email_match.group(0)
                # Quitar el email de la línea para extraer el nombre
                name_part = line.replace(guest_info["email"], "").strip()
                name_parts = name_part.split()
                if name_parts:
                    guest_info["nombre"] = name_parts[0]
                    if len(name_parts) > 1:
                        guest_info["apellido"] = " ".join(name_parts[1:])
    
    # Si hay información de categoría, usarla para determinar el género
    if category:
        if category.lower() in ["hombre", "hombres", "masculino"]:
            guest_info["genero"] = "Masculino"
        elif category.lower() in ["mujer", "mujeres", "femenino"]:
            guest_info["genero"] = "Femenino"
    else:
        # Intentar determinar el género a partir del nombre
        nombre = guest_info["nombre"].lower()
        if nombre.endswith("a") or nombre.endswith("ia"):
            guest_info["genero"] = "Femenino"
        elif nombre.endswith("o") or nombre.endswith("io"):
            guest_info["genero"] = "Masculino"
    
    return guest_info

def add_guests_to_sheet(sheet, guests_data, phone_number, event_name, sheet_conn, categories=None, command_type='add_guests'):
    """
    Agrega invitados a la hoja con información estructurada, incluyendo el evento
    y usando el nombre del PR en lugar del número en la columna 'Publica'.
    ADAPTADO para columnas: Nombre y Apellido | Email | Genero | Publica | Evento | Timestamp

    Args:
        sheet: Objeto de hoja de Google Sheets ('Invitados')
        guests_data: Lista de líneas crudas con datos de invitados
        phone_number: Número de teléfono del anfitrión (NORMALIZADO)
        event_name: Nombre del evento seleccionado
        sheet_conn: Instancia de SheetsConnection para acceder al mapeo PR <--- NUEVO
        categories (dict, optional): Información sobre categorías detectadas
        command_type (str): Tipo de comando detectado

    Returns:
        int: Número de invitados añadidos (-1 si hay error de validación)
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # --- AJUSTADO: Verificar encabezados para 6 columnas ---
        expected_headers = ['Nombre y Apellido', 'Email', 'Genero', 'Publica', 'Evento', 'Timestamp']
        try:
            headers = sheet.row_values(1)
        except gspread.exceptions.APIError as api_err:
             if "exceeds grid limits" in str(api_err): # Hoja completamente vacía
                headers = []
             else:
                 raise api_err

        # Actualizar si los encabezados no coinciden o la hoja está vacía
        if not headers or len(headers) < len(expected_headers) or headers[:len(expected_headers)] != expected_headers:
            logger.info(f"Actualizando/Creando encabezados en la hoja '{sheet.title}': {expected_headers}")
            # Limpiar solo si es estrictamente necesario y estás seguro.
            # sheet.clear()
            # Actualizar el rango correcto A1:F1 para 6 columnas
            sheet.update('A1:F1', [expected_headers])


        # --- Procesar datos de invitados ---
        structured_guests = None

        # Usar IA si está disponible y NO es formato split (OpenAI no está entrenado para el formato split)
        if command_type == 'add_guests' and OPENAI_AVAILABLE and client:
            logger.info("Intentando análisis de invitados con OpenAI...")
            structured_guests = analyze_guests_with_ai(guests_data, categories)
            if structured_guests:
                 logger.info(f"OpenAI procesó {len(structured_guests)} invitados.")
            else:
                 logger.warning("OpenAI no pudo procesar los invitados o devolvió vacío.")


        # Si IA falla, no está disponible, o ES formato split, usar procesamiento manual mejorado
        if not structured_guests:
            if command_type == 'add_guests_split':
                 logger.info("Usando extractor manual para formato dividido (Nombres -> Emails)...")
            else:
                 logger.info("Usando extractor manual para formato estándar (Nombre - Email)...")
            # Esta función ahora decide internamente si llamar a extract_guests_from_split_format
            structured_guests = extract_guests_manually_enhanced(guests_data, categories, command_type)

        if not structured_guests: # Si la extracción manual también falló
            logger.error("La extracción manual de invitados devolvió una lista vacía o None.")
            return 0 # Indicar que no se añadió nada

        # --- Validar invitados estructurados ---
        valid_guests = []
        invalid_entries_found = False
        for guest in structured_guests:
            # Verificar que sea diccionario y tenga email y al menos nombre
            if isinstance(guest, dict) and guest.get("email") and guest.get("nombre"):
                # Validar email básico
                if re.match(r"[^@]+@[^@]+\.[^@]+", guest["email"]):
                    valid_guests.append(guest)
                else:
                    logger.warning(f"Formato de email inválido: {guest.get('email')} para {guest.get('nombre')}")
                    invalid_entries_found = True
            else:
                logger.warning(f"Invitado incompleto (falta email o nombre): {guest}")
                invalid_entries_found = True

        # Si se encontraron entradas inválidas, devolver error de validación
        if invalid_entries_found:
            logger.error("Se detectaron invitados sin email válido o nombre.")
            return -1  # Código especial para indicar error de validación
        
        # --- NUEVO: Obtener Nombre del PR ---
        pr_name = phone_number # Valor por defecto si no se encuentra el mapeo o el número
        try:
            # Obtener el mapeo desde la instancia de conexión
            phone_to_pr_map = sheet_conn.get_phone_pr_mapping()
            # Buscar el nombre del PR usando el número normalizado que recibimos
            pr_name_found = phone_to_pr_map.get(phone_number)
            if pr_name_found:
                pr_name = pr_name_found # Usar el nombre encontrado
                logger.info(f"Nombre PR encontrado para {phone_number}: {pr_name}")
            else:
                logger.warning(f"No se encontró nombre PR para el número {phone_number} en la hoja 'Telefonos'. Se usará el número como fallback.")
        except Exception as map_err:
            logger.error(f"Error al obtener/buscar en el mapeo PR para {phone_number}: {map_err}. Se usará el número como fallback.")
            # pr_name ya tiene el número como fallback

        # --- Crear filas para añadir a la hoja (MODIFICADO) ---
        rows_to_add = []
        for guest in valid_guests:
            full_name = f"{guest.get('nombre', '')} {guest.get('apellido', '')}".strip()
            rows_to_add.append([
                full_name,                      # Columna A: Nombre y Apellido
                guest.get("email", ""),         # Columna B: Email
                guest.get("genero", "Otro"),    # Columna C: Genero
                pr_name,                        # Columna D: Publica (Nombre del PR o número fallback) <--- MODIFICADO
                event_name,                     # Columna E: Evento
                timestamp                       # Columna F: Timestamp
            ])

        # --- Agregar a la hoja ---
        if rows_to_add:
            try:
                sheet.append_rows(rows_to_add, value_input_option='USER_ENTERED')
                logger.info(f"Agregados {len(rows_to_add)} invitados para evento '{event_name}' por {phone_number}")
                return len(rows_to_add)
            except gspread.exceptions.APIError as e:
                 logger.error(f"Error de API de Google Sheets al agregar filas: {e}")
                 return 0 # Indicar fallo
            except Exception as e:
                 logger.error(f"Error inesperado en append_rows: {e}")
                 import traceback
                 logger.error(traceback.format_exc())
                 return 0
        else:
             logger.warning("No se generaron filas válidas para añadir a la hoja.")
             # Si structured_guests no estaba vacío pero valid_guests sí, podría ser -1 por validación
             # Si ambos estaban vacíos, 0 es correcto.
             return 0

    except Exception as e:
        logger.error(f"Error GRANDE en add_guests_to_sheet: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 0 # Indicar fallo genérico
    
def count_guests(sheet, phone_number=None, event_name=None):
    """
    Cuenta invitados y recupera sus detalles, opcionalmente filtrados por publicador y/o evento.

    Args:
        sheet: Objeto de hoja de Google Sheets
        phone_number (str, optional): Número de teléfono del publicador para filtrar.
        event_name (str, optional): Nombre del evento para filtrar.

    Returns:
        tuple: (dict con conteos por género, lista con detalles de invitados filtrados)
    """
    try:
        all_data = sheet.get_all_records() # Asume que la primera fila son headers

        if not all_data:
            logger.warning("La hoja no contiene datos (después de los encabezados)")
            return {'Total': 0}, []

        filtered_data = all_data
        logger.info(f"Datos leídos: {len(all_data)} filas.")

        # Filtrar por número de teléfono (Publica)
        if phone_number:
            # Normalizar el número de teléfono (eliminar 'whatsapp:' y '+', espacios)
            normalized_phone = phone_number.replace('whatsapp:', '').replace('+', '').replace(' ', '')
            logger.info(f"Filtrando por teléfono normalizado: {normalized_phone}")
            # Asume que la columna se llama 'Publica'
            filtered_data = [row for row in filtered_data if str(row.get('Publica', '')).replace('+', '').replace(' ', '') == normalized_phone]
            logger.info(f"Después de filtrar por teléfono: {len(filtered_data)} filas.")


        # Filtrar por nombre de evento
        if event_name:
            logger.info(f"Filtrando por evento: {event_name}")
            # Asume que la columna se llama 'Evento'
            filtered_data = [row for row in filtered_data if row.get('Evento', '') == event_name]
            logger.info(f"Después de filtrar por evento: {len(filtered_data)} filas.")


        # Contar por género
        categories = {}
        for row in filtered_data:
            # Asume que la columna se llama 'Genero'
            gender = row.get('Genero', 'Sin categoría')
            if not gender: # Si está vacío, tratar como sin categoría
                 gender = 'Sin categoría'
            categories[gender] = categories.get(gender, 0) + 1

        # Agregar total
        categories['Total'] = len(filtered_data)

        logger.info(f"Conteo completo para {phone_number} / {event_name}: {categories}")
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
    if command == 'saludo':
        # AQUÍ ES DONDE SE DEFINE EL MENSAJE DE BIENVENIDA
        welcome_text = """👋 ¡Hola! Bienvenido al sistema de gestión de invitados. 

Puedo ayudarte con la administración de tu lista de invitados. Aquí tienes lo que puedes hacer:

1️⃣ *Agregar invitados*: 
   Envía los datos en cualquiera de estos formatos:
   • Juan Pérez - juan@ejemplo.com
   • O por categorías:
     Hombres:
     Juan Pérez - juan@ejemplo.com
     Mujeres:
     María López - maria@ejemplo.com

2️⃣ *Consultar invitados*:
   • Escribe "cuántos invitados" o "lista de invitados"

3️⃣ *Ayuda*:
   • Escribe "ayuda" para ver estas instrucciones de nuevo

¿En qué puedo ayudarte hoy?"""
        
        return welcome_text
        
    elif command == 'count':
    
    # Normalizar el comando para add_guests
        if command == 'add_guests_split':
            command = 'add_guests'
        
    # Usar la función original
    return generate_response(command, result, phone_number, sentiment_analysis)

# Modificación a la función principal de whatsapp_reply
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    global user_states # Accedemos al diccionario global de estados
    response_text = None
    sender_phone_raw = None
    sender_phone_normalized = None # Para usar como key en user_states
    sheet_conn = None

    try:
        data = request.form.to_dict()
        logger.info(f"Datos recibidos: {data}")

        sender_phone_raw = data.get('From') # Ej: whatsapp:+14155238886
        incoming_msg = data.get('Body', '').strip()

        if not incoming_msg or not sender_phone_raw:
            logger.error("Payload inválido: falta 'Body' o 'From'")
            return jsonify({"status": "error", "message": "Invalid payload"}), 400

        # --- INICIO: FILTRO POR NÚMERO AUTORIZADO ---
        # Normalizar número del remitente (quitar 'whatsapp:', '+', espacios, etc.)
        sender_phone_normalized = re.sub(r'\D', '', sender_phone_raw)

        # Conectar a Google Sheets (necesario para obtener la lista autorizada)
        sheet_conn = SheetsConnection()
        authorized_phones = sheet_conn.get_authorized_phones()

        if not authorized_phones:
             logger.critical("No hay números autorizados cargados (puede ser error de hoja 'Telefonos' o está vacía). Bloqueando todas las solicitudes.")
             # No responder nada, solo registrar
             return jsonify({"status": "ignored", "message": "Authorization list unavailable"}), 200


        if sender_phone_normalized not in authorized_phones:
            logger.warning(f"Mensaje recibido de número NO AUTORIZADO: {sender_phone_raw} (Normalizado: {sender_phone_normalized}). Ignorando.")
            # Devolver 200 OK a Twilio para que no reintente, pero no enviar mensaje.
            return jsonify({"status": "ignored", "message": "Unauthorized number"}), 200
        else:
            logger.info(f"Mensaje recibido de número AUTORIZADO: {sender_phone_raw} (Normalizado: {sender_phone_normalized})")

        # Obtener estado actual del usuario
        user_status = user_states.get(sender_phone_normalized, {'state': STATE_INITIAL, 'event': None})
        current_state = user_status['state']
        selected_event = user_status['event']

        logger.info(f"Usuario: {sender_phone_normalized}, Estado Actual: {current_state}, Evento Seleccionado: {selected_event}")

        guest_sheet = sheet_conn.guest_sheet # Acceder al atributo ya inicializado en _connect
        if guest_sheet is None:
                logger.error(f"Hoja 'Invitados' no disponible para {sender_phone_normalized}.")
                response_text = "Error interno: No se puede acceder a la lista de invitados. Contacta al administrador."
                # Continuar para enviar el mensaje de error
        else:
            if current_state == STATE_INITIAL:
                # Esperando un saludo para iniciar
                # Usar el parseador para detectar saludo de forma robusta
                parsed_command = parse_message_enhanced(incoming_msg)
                if parsed_command['command_type'] == 'saludo':
                    # PASO 2: Responder con eventos disponibles
                    available_events = sheet_conn.get_available_events()
                    if not available_events:
                        response_text = "¡Hola! 👋 No encontré eventos disponibles para anotar invitados en este momento."
                        user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None} # Reset state
                    else:
                        event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                        response_text = f"¡Hola! 👋 Eventos disponibles para anotar invitados:\n\n{event_list_text}\n\nPor favor, responde con el *número* del evento que deseas seleccionar."
                        # Guardar los eventos disponibles temporalmente podría ser útil si son muchos
                        user_states[sender_phone_normalized] = {
                            'state': STATE_AWAITING_EVENT_SELECTION,
                            'event': None,
                            'available_events': available_events # Guardamos la lista que mostramos
                        }
                # --- Añadido: Manejar consulta de lista aquí también ---
                elif parsed_command['command_type'] == 'count':
                    count_result, guests_list = count_guests(guest_sheet, sender_phone_normalized) # Contar todos los suyos
                    # Usar la función existente para formatear la respuesta del conteo
                    sentiment = analyze_sentiment(incoming_msg).get('sentiment', 'neutral') # Opcional: analizar sentimiento
                    response_text = generate_count_response(count_result, guests_list, sender_phone_normalized, sentiment)
                    # Mantenemos el estado inicial
                    user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None}
                else:
                    response_text = '¡Hola! 👋 Para comenzar a anotar invitados, por favor, salúdame o dime "Hola". También puedes pedirme tu "lista de invitados".'
                    user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None} # Reset state


            elif current_state == STATE_AWAITING_EVENT_SELECTION:
                # PASO 3: Esperando que el usuario elija un evento (por número)
                available_events = user_status.get('available_events', [])
                try:
                    choice = int(incoming_msg)
                    if 1 <= choice <= len(available_events):
                        selected_event = available_events[choice - 1]
                        logger.info(f"Usuario {sender_phone_normalized} seleccionó evento: {selected_event}")

                        # PASO 4: Enviar instrucciones de formato
                        response_text = (
                            f"Perfecto, evento seleccionado: *{selected_event}*.\n\n"
                            "Ahora envíame la lista de invitados. Formato:\n\n"
                            "*Hombres:* (Opcional)\n"
                            "Nombre Apellido\n"
                            "Nombre Apellido\n"
                            "...\n\n" # Línea vacía opcional entre nombres y emails
                            "email1@ejemplo.com\n"
                            "email2@ejemplo.com\n"
                            "...\n\n"
                            "*Mujeres:* (Opcional)\n"
                            "Nombre Apellido\n"
                            "...\n\n"
                            "email3@ejemplo.com\n"
                            "...\n\n"
                            "⚠️ *Importante*: Primero todos los nombres, luego todos los emails. La cantidad debe coincidir.\n"
                            "Escribe 'cancelar' para elegir otro evento."
                        )
                        user_states[sender_phone_normalized] = {
                            'state': STATE_AWAITING_GUEST_DATA,
                            'event': selected_event
                        }
                    else:
                        # Número inválido
                        event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                        response_text = f"El número '{incoming_msg}' no es válido. Por favor, elige un número de la lista:\n\n{event_list_text}"
                        # Mantenemos el estado AWAITING_EVENT_SELECTION
                except ValueError:
                    # No envió un número
                    event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                    response_text = f"Por favor, responde sólo con el *número* del evento que deseas seleccionar de la lista:\n\n{event_list_text}"
                    # Mantenemos el estado AWAITING_EVENT_SELECTION


            elif current_state == STATE_AWAITING_GUEST_DATA:
                # PASO 5: Esperando la lista de invitados en el formato especificado
                logger.info(f"Procesando datos de invitados para {sender_phone_normalized} en evento {selected_event}")
                # Usar el parseador avanzado para detectar el tipo de comando (add_guests o add_guests_split)
                parsed = parse_message_enhanced(incoming_msg)
                command_type = parsed['command_type']
                data_lines = parsed['data'] # Lista de líneas no vacías
                categories = parsed['categories'] # Diccionario con categorías detectadas (Hombres, Mujeres)

                if command_type in ['add_guests', 'add_guests_split'] and data_lines: # Asegúrate que haya datos
                    added_count = add_guests_to_sheet(
                        guest_sheet,
                        data_lines, # <- Pasar la variable
                        sender_phone_normalized,
                        selected_event,
                        sheet_conn,
                        categories, # <- Pasar la variable
                        command_type # <- Pasar la variable
                )

                    # PASO 5.2: Responder confirmación o error
                    if added_count > 0:
                        response_text = f"✅ ¡Listo! Se anotaron *{added_count}* invitados correctamente para el evento *{selected_event}*."
                        # Volver al estado inicial después de éxito
                        user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None}
                    elif added_count == 0:
                        # Podría ser que el formato era inválido y no se extrajo nada, o un error de sheet.
                        response_text = f"⚠️ No pude anotar invitados. Revisa el formato:\n\n*Hombres:*\nNombre\n...\nEmail\n...\n\n*Mujeres:*\nNombre\n...\nEmail\n...\n\nAsegúrate que la cantidad de nombres y emails coincida en cada sección."
                        # Mantenemos estado para que reintente
                        user_states[sender_phone_normalized] = {'state': STATE_AWAITING_GUEST_DATA, 'event': selected_event}
                    elif added_count == -1:
                        # Error específico de validación (ej. email faltante)
                        response_text = f"⚠️ Detecté un problema. Parece que faltan emails o algunos no son válidos. Revisa la lista y asegúrate que cada nombre tenga un email asociado y válido.\n\nIntenta enviarla de nuevo con el formato correcto."
                        # Mantenemos estado para que reintente
                        user_states[sender_phone_normalized] = {'state': STATE_AWAITING_GUEST_DATA, 'event': selected_event}
                    else: # Otro error < -1 (no definido actualmente) o error genérico (si add_guests retorna < -1)
                        response_text = "❌ Hubo un error al guardar los invitados. Por favor, inténtalo de nuevo más tarde."
                        # Volver al estado inicial en error desconocido grave
                        user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None}

                elif incoming_msg.lower() in ["cancelar", "salir", "cancel", "exit"]:
                    response_text = "Operación cancelada. Si quieres anotar invitados para otro evento, sólo salúdame de nuevo."
                    user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None} # Reset state
                else:
                    # Mensaje inesperado mientras se esperaban datos
                    response_text = (f"Estoy esperando la lista de invitados para *{selected_event}*.\n"
                                    "Por favor, usa el formato que te indiqué:\n\n"
                                    "*Hombres:*\nNombre\n...\nEmail\n...\n\n*Mujeres:*\nNombre\n...\nEmail\n...\n\n"
                                    "O escribe 'cancelar' para volver a empezar.")
                    # Mantenemos el estado AWAITING_GUEST_DATA

            # --- Fin Lógica basada en Estados ---

            # Enviar la respuesta calculada
            if not send_twilio_message(sender_phone_raw, response_text): # Usar el número raw original para enviar
                logger.error(f"Fallo al enviar mensaje de respuesta a {sender_phone_raw}")
                # No podemos informar al usuario si falla el envío
                return jsonify({"status": "error", "message": "Failed to send response"}), 500

            logger.info(f"Respuesta enviada a {sender_phone_raw}: {response_text[:100]}...") # Loguea inicio de respuesta
            return jsonify({"status": "success"}), 200

    
    except Exception as e:
        logger.error(f"Error inesperado en el webhook: {e}")
        import traceback
        logger.error(traceback.format_exc())
        # Intentar notificar al usuario del error genérico si tenemos su número
        if sender_phone_raw:
            # Evita enviar el mensaje de error por defecto si ya se envió uno específico
            if response_text == "Lo siento, hubo un error procesando tu mensaje. Intenta de nuevo.":
                 send_twilio_message(sender_phone_raw, response_text)
        return jsonify({"status": "error", "message": "Internal server error"}), 500