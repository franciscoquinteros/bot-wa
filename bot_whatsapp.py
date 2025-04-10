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
STATE_AWAITING_GUEST_TYPE = 'AWAITING_GUEST_TYPE'
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

def infer_gender_llm(first_name):
    """
    Usa OpenAI (LLM) para inferir el género de un primer nombre.

    Args:
        first_name (str): El primer nombre a analizar.

    Returns:
        str: "Hombre", "Mujer", o "Desconocido".
    """
    if not first_name or not isinstance(first_name, str):
        return "Desconocido"

    # Verificar si el cliente OpenAI está disponible
    if not OPENAI_AVAILABLE or client is None:
        logger.warning("OpenAI no disponible para inferir género. Devolviendo 'Desconocido'.")
        return "Desconocido"

    try:
        logger.debug(f"Consultando OpenAI para género de: {first_name}")
        system_prompt = "Eres un asistente experto en nombres hispanohablantes, especialmente de Argentina. Tu tarea es determinar el género más probable (Hombre o Mujer) asociado a un nombre de pila. Responde únicamente con una de estas tres palabras: Hombre, Mujer, Desconocido."
        user_prompt = f"Nombre de pila: {first_name}"

        response = client.chat.completions.create(
            model="gpt-3.5-turbo", # Puedes probar con "gpt-4o" o "gpt-4" si necesitas más precisión
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0, # Queremos la respuesta más probable
            max_tokens=5 # La respuesta es muy corta
        )

        result_text = response.choices[0].message.content.strip().capitalize()
        logger.debug(f"Respuesta de OpenAI para género de '{first_name}': {result_text}")

        # Validar la respuesta
        if result_text in ["Hombre", "Mujer"]:
            return result_text
        else:
            # Si responde algo inesperado, lo marcamos como desconocido
            logger.warning(f"Respuesta inesperada de OpenAI para género de '{first_name}': {result_text}")
            return "Desconocido"

    except Exception as e:
        logger.error(f"Error al llamar a OpenAI para inferir género de '{first_name}': {e}")
        return "Desconocido"


def add_vip_guests_to_sheet(sheet, vip_guests_list, pr_name):
    """
    Agrega invitados VIP (nombre, email, género) a la hoja 'Invitados VIP'.
    Infiere género si no se proporcionó. Columna de género se llama "Ingreso".

    Args:
        sheet: Objeto de hoja de Google Sheets ('Invitados VIP').
        vip_guests_list (list): Lista de diccionarios [{'nombre': n, 'email': e, 'genero': g_o_None}, ...].
        pr_name (str): Nombre del PR que los está añadiendo.

    Returns:
        int: Número de invitados VIP añadidos. 0 si hay error o no se añadió nada.
             -1 si hubo datos pero se filtraron todos por inválidos.
    """
    if not sheet:
        logger.error("Intento de añadir VIPs pero la hoja 'Invitados VIP' no es válida.")
        return 0
    if not vip_guests_list:
        logger.warning("Se llamó a add_vip_guests_to_sheet con lista vacía o inválida.")
        return 0

    rows_to_add = []
    added_count = 0
    original_count = len(vip_guests_list)

    try:
        logger.info(f"DEBUG Add VIP: Recibido tipo={type(vip_guests_list)}, contenido={vip_guests_list}") # DEBUG
        # --- Verificar/Crear encabezados (Nombre | Email | Ingreso | PR) ---
        expected_headers = ['Nombre', 'Email', 'Ingreso', 'PR'] # <-- NUEVOS HEADERS
        try:
            headers = sheet.row_values(1)
        except gspread.exceptions.APIError as api_err:
             if "exceeds grid limits" in str(api_err): headers = []
             else: raise api_err
        if not headers or headers[:len(expected_headers)] != expected_headers:
             logger.info(f"Actualizando/Creando encabezados en 'Invitados VIP': {expected_headers}")
             # Actualizar rango A1:D1 para 4 columnas
             sheet.update(f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers))}', [expected_headers])

        # --- Crear las filas ---
        for guest_data in vip_guests_list:
            logger.info(f"DEBUG Add VIP Loop: Iterando, tipo={type(guest_data)}, item={guest_data}") # DEBUG
            name = guest_data.get('nombre', '').strip()
            email = guest_data.get('email', '').strip()
            parsed_gender = guest_data.get('genero') # Será 'Hombre', 'Mujer' o None

            if name and email: # Validar nombre y email
                # --- Determinar/Inferir Género ---
                # --- Determinar/Inferir Género ---
                final_gender = "Desconocido" # Valor por defecto
                if parsed_gender: # Si el parser detectó Hombres/Mujeres
                    final_gender = parsed_gender
                else:
                    # Intentar inferir si no vino del encabezado
                    first_name = name.split()[0] if name else ""
                    if first_name:
                         # --- LLAMAR A LA FUNCIÓN DE IA ---
                         inferred = infer_gender_llm(first_name) # <<-- ¡CAMBIO AQUÍ!
                         final_gender = inferred # Será Hombre, Mujer, o Desconocido
                         logger.debug(f"Género inferido por IA para '{first_name}': {final_gender} (Parseado: {parsed_gender})")
                         # Pequeña pausa opcional para no saturar API si son muchos nombres seguidos
                         # time.sleep(0.1)
                    else:
                         logger.warning(f"No se pudo inferir género para nombre vacío.")
                         final_gender = "Desconocido" # Asegurar default si el nombre estaba vacío

                # Añadir fila con Nombre, Email, Género (Ingreso), PR
                rows_to_add.append([name, email, final_gender, pr_name]) # <-- NUEVO FORMATO FILA
                added_count += 1
            else:
                logger.warning(f"Se omitió invitado VIP (nombre='{name}', email='{email}') por datos faltantes. PR: {pr_name}.")

        # --- Agregar a la hoja ---
        if rows_to_add:
            sheet.append_rows(rows_to_add, value_input_option='USER_ENTERED')
            logger.info(f"Agregados {added_count} invitados VIP (con género) por PR '{pr_name}'.")
            return added_count if added_count == original_count else -1 # Indica si algunos fallaron la validación interna
        else:
            logger.warning(f"No se generaron filas VIP válidas para añadir por {pr_name}.")
            return -1 if original_count > 0 else 0

    # ... (Manejo de excepciones como antes) ...
    except gspread.exceptions.APIError as e:
        logger.error(f"Error API Google Sheets al agregar filas VIP: {e}")
        return 0
    except Exception as e:
        logger.error(f"Error inesperado en add_vip_guests_to_sheet: {e}")
        import traceback
        logger.error(traceback.format_exc()) # Log completo del error
        return 0


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
            
            # --- NUEVO: Hoja VIP ---
            try:
                self.vip_sheet_obj = self.spreadsheet.worksheet("VIP")
                logger.info("Hoja 'VIP' encontrada.")
            except gspread.exceptions.WorksheetNotFound:
                logger.warning("Hoja 'VIP' no encontrada. La funcionalidad VIP no estará disponible.")
                self.vip_sheet_obj = None
            # --- FIN NUEVO ---

            # --- NUEVO: Obtener hoja Invitados VIP ---
            try:
                self.vip_guest_sheet_obj = self.spreadsheet.worksheet("Invitados VIP")
                logger.info("Hoja 'Invitados VIP' encontrada.")
            except gspread.exceptions.WorksheetNotFound:
                logger.warning("Hoja 'Invitados VIP' no encontrada. Intentando crearla...")
                try:
                    # Crear con encabezados "Nombre" y "PR"
                    expected_headers_vip = ['Nombre', 'PR']
                    self.vip_guest_sheet_obj = self.spreadsheet.add_worksheet(title="Invitados VIP", rows="1", cols=len(expected_headers_vip))
                    self.vip_guest_sheet_obj.update(f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers_vip))}', [expected_headers_vip])
                    logger.info("Hoja 'Invitados VIP' creada con encabezados.")
                except Exception as create_err:
                    logger.error(f"No se pudo crear la hoja 'Invitados VIP': {create_err}")
                    self.vip_guest_sheet_obj = None # Marcar como no disponible
            # --- FIN NUEVO ---

            # ---> ¡AQUÍ! Inicializar atributos de caché en la instancia SIEMPRE <---
            self._phone_cache = None
            self._phone_cache_last_refresh = 0
            self._pr_name_map_cache = None # NUEVO: Cache para el mapeo tel -> nombre PR
            self._vip_phone_cache = None # NUEVO: Cache para teléfonos VIP
            self._vip_phone_cache_last_refresh = 0 # NUEVO: Timestamp para caché VIP
            self._vip_pr_map_cache = None # NUEVO: Cache para mapeo VIP -> PR Name
            self._vip_pr_map_last_refresh = 0 # NUEVO: Timestamp para caché mapeo VIP

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

     # --- NUEVO: Método para obtener mapeo Telefono VIP -> Nombre PR ---
    def get_vip_phone_pr_mapping(self):
        """
        Obtiene un diccionario que mapea números de teléfono VIP normalizados
        a los nombres de PR correspondientes desde la hoja 'VIP'.
        Usa caché para eficiencia.
        """
        now = time.time()
        if self._vip_pr_map_cache is not None and now - self._vip_pr_map_last_refresh < self._phone_cache_interval:
            return self._vip_pr_map_cache

        logger.info("Refrescando caché de mapeo VIP Telefono -> Nombre PR...")
        vip_phone_to_pr_map = {}
        try:
            # Usa la referencia guardada en self.vip_sheet_obj
            vip_sheet = self.vip_sheet_obj
            if vip_sheet:
                all_values = vip_sheet.get_all_values()
                if len(all_values) > 1: # Si hay filas además del encabezado
                    for row in all_values[1:]: # Empezar desde la segunda fila
                        if len(row) >= 2: # Necesitamos Teléfono (A) y PR (B)
                            raw_phone = row[0] # Columna A (índice 0) - Telefonos VIP
                            pr_name = row[1]   # Columna B (índice 1) - Nombre PR VIP
                            if raw_phone and pr_name:
                                normalized_phone = re.sub(r'\D', '', str(raw_phone))
                                if normalized_phone:
                                    vip_phone_to_pr_map[normalized_phone] = pr_name.strip()
                        else:
                             logger.warning(f"Fila incompleta encontrada en 'VIP' al crear mapeo PR: {row}")
                logger.info(f"Creado mapeo VIP para {len(vip_phone_to_pr_map)} teléfonos a nombres PR.")
            else:
                logger.warning("No se puede refrescar mapeo PR VIP porque hoja 'VIP' no está disponible.")
                vip_phone_to_pr_map = self._vip_pr_map_cache if self._vip_pr_map_cache is not None else {}

            self._vip_pr_map_cache = vip_phone_to_pr_map
            self._vip_pr_map_last_refresh = now
            return self._vip_pr_map_cache

        except gspread.exceptions.APIError as e:
            logger.error(f"Error de API al leer la hoja 'VIP' para mapeo PR: {e}. Usando caché anterior si existe.")
            return self._vip_pr_map_cache if self._vip_pr_map_cache is not None else {}
        except Exception as e:
            logger.error(f"Error inesperado al obtener mapeo PR VIP: {e}. Usando caché anterior si existe.")
            return self._vip_pr_map_cache if self._vip_pr_map_cache is not None else {}

    # --- NUEVO: Método para obtener la hoja Invitados VIP ---
    def get_vip_guest_sheet(self):
        """ Devuelve la referencia a la hoja 'Invitados VIP'. """
        # La referencia ya se obtuvo (o se intentó crear) en _connect
        return self.vip_guest_sheet_obj
    
    # --- NUEVO: Método para obtener teléfonos VIP ---
    def get_vip_phones(self):
        """
        Obtiene un set con los números de teléfono normalizados de la hoja 'VIP'.
        Usa caché para eficiencia.
        """
        now = time.time()
        # Usar el mismo intervalo de caché que los otros teléfonos
        if self._vip_phone_cache is not None and now - self._vip_phone_cache_last_refresh < self._phone_cache_interval:
            return self._vip_phone_cache

        logger.info("Refrescando caché de números VIP...")
        vip_phones_set = set()
        try:
            # Usa la referencia guardada en self.vip_sheet_obj
            vip_sheet = self.vip_sheet_obj
            if vip_sheet:
                # Asume Col A = Telefonos en hoja VIP, salta encabezado
                vip_phone_list_raw = vip_sheet.col_values(1)[1:]
                for phone in vip_phone_list_raw:
                    if phone:
                        normalized_phone = re.sub(r'\D', '', str(phone))
                        if normalized_phone:
                            vip_phones_set.add(normalized_phone)
                logger.info(f"Cargados {len(vip_phones_set)} números VIP.")
            else:
                # Hoja VIP no encontrada o no accesible
                logger.warning("No se puede refrescar caché VIP porque la hoja 'VIP' no está disponible.")
                # Devolver caché anterior o vacío
                vip_phones_set = self._vip_phone_cache if self._vip_phone_cache is not None else set()

            self._vip_phone_cache = vip_phones_set
            self._vip_phone_cache_last_refresh = now
            return self._vip_phone_cache

        except gspread.exceptions.APIError as e:
            logger.error(f"Error de API al leer la hoja 'VIP': {e}. Usando caché VIP anterior si existe.")
            return self._vip_phone_cache if self._vip_phone_cache is not None else set()
        except Exception as e:
            logger.error(f"Error inesperado al obtener números VIP: {e}. Usando caché VIP anterior si existe.")
            return self._vip_phone_cache if self._vip_phone_cache is not None else set()
        
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
        """ Obtiene la lista de eventos desde la hoja 'Eventos', ignorando el encabezado A1. """
        try:
            event_sheet = self.get_event_sheet() # Usa el método que devuelve self.event_sheet
            if event_sheet:
                # Obtener todos los valores de la primera columna
                all_event_values = event_sheet.col_values(1)
                # **CORRECCIÓN:** Ignorar el primer elemento (A1) y filtrar vacíos
                events = [event for event in all_event_values[1:] if event]
                logger.info(f"Eventos disponibles encontrados (sin encabezado): {events}")
                # CORREGIDO: Devolver la lista completa 'events'
                return events # <---- CORREGIDO
            else:
                # Hoja no encontrada durante _connect
                logger.warning("Hoja 'Eventos' no disponible. No se pueden listar eventos.")
                return [] # Devolver lista vacía si la hoja no existe
        except gspread.exceptions.APIError as e:
             logger.error(f"Error de API al leer eventos: {e}")
             return []
        except Exception as e:
            logger.error(f"Error inesperado al obtener eventos: {e}")
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

    # --- Método para obtener el mapeo Telefono -> Nombre PR ---

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

    
# Asegúrate que esta es la ÚNICA definición de parse_vip_guest_list
def parse_vip_guest_list(message_body):
    """
    Parsea formato VIP (Nombres->Emails) detectando encabezados opcionales
    'Hombres:'/'Mujeres:'. Devuelve lista de dicts
    [{'nombre': n, 'email': e, 'genero': g}] o None.
    'genero' será "Hombre", "Mujer", o None si no había encabezado.
    """
    lines = [line.strip() for line in message_body.split('\n') if line.strip()]
    if not lines:
        logger.warning("parse_vip_guest_list: Mensaje vacío.")
        return None

    names_data = [] # Guardará {'nombre': n, 'genero': g}
    emails = []
    parsing_names = True
    current_gender = None # Género actual detectado por encabezado

    for line in lines:
        line_lower = line.lower()

        # Detectar encabezados de Género
        if line_lower.startswith('hombres'):
            current_gender = "Hombre"
            logger.debug("parse_vip_guest_list: Detectado encabezado 'Hombres'.")
            continue # Saltar la línea del encabezado
        elif line_lower.startswith('mujeres'):
            current_gender = "Mujer"
            logger.debug("parse_vip_guest_list: Detectado encabezado 'Mujeres'.")
            continue # Saltar la línea del encabezado

        # Detectar Emails
        is_email = '@' in line and '.' in line.split('@')[-1] and len(line.split('@')[0]) > 0
        if is_email:
            parsing_names = False
            if re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", line):
                 emails.append(line)
            else:
                 logger.warning(f"parse_vip_guest_list: Línea '{line}' parece email pero no valida regex.")
                 # Ignorar email inválido
        elif parsing_names:
            # Añadir nombre CON el género detectado HASTA ESE MOMENTO
            if re.match(r"^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s'.]+$", line) and len(line) > 1:
                 names_data.append({'nombre': line, 'genero': current_gender}) # Guarda dict nombre+genero
                 # logger.debug(f"parse_vip_guest_list: Nombre añadido: '{line}', Genero: {current_gender}")
            else:
                 logger.warning(f"parse_vip_guest_list: Línea '{line}' ignorada (modo nombre).")
        # else: Ignorar líneas que no son email si ya estamos en modo email

    logger.info(f"Parseo VIP: {len(names_data)} nombres encontrados, {len(emails)} emails encontrados.")

    # Validar cantidades
    if not names_data or not emails or len(names_data) != len(emails):
        logger.error(f"Error formato VIP: Faltan/Desbalance - N:{len(names_data)} E:{len(emails)}.")
        return None

    # Emparejar Nombres (con su género) y Emails
    paired_guests = []
    for i in range(len(names_data)):
        name_info = names_data[i] # Esto es {'nombre': n, 'genero': g}
        email_clean = emails[i].strip()
        if name_info.get('nombre') and email_clean:
             paired_guests.append({
                 'nombre': name_info['nombre'].strip(),
                 'email': email_clean,
                 'genero': name_info['genero'] # Puede ser 'Hombre', 'Mujer' o None
             })

    if len(paired_guests) != len(names_data):
         logger.warning("Algunos pares nombre/email VIP fueron omitidos por datos vacíos.")

    return paired_guests if paired_guests else None
    
# MODIFICADO: Añadir sheet_conn, buscar PR name y filtrar por él.
def get_guests_by_pr(sheet, sheet_conn, phone_number):
    """
    Obtiene todos los registros de invitados asociados a un número de teléfono de publicador.

    Args:
        sheet: Objeto de hoja de Google Sheets ('Invitados').
        sheet_conn: Instancia de SheetsConnection para buscar el nombre PR.
        phone_number (str): Número de teléfono NORMALIZADO del publicador.

    Returns:
        list: Lista de diccionarios de invitados filtrados, o lista vacía si no se encuentra PR o no hay invitados.
    """
    pr_name = None
    try:
        # Obtener el nombre del PR
        phone_to_pr_map = sheet_conn.get_phone_pr_mapping()
        pr_name = phone_to_pr_map.get(phone_number)

        if not pr_name:
            logger.warning(f"No se encontró PR Name para el número {phone_number} al buscar invitados.")
            return [] # No hay invitados si no hay PR

        # Obtener todos los registros
        all_guests = sheet.get_all_records()
        if not all_guests:
            logger.warning("La hoja 'Invitados' está vacía.")
            return []

        # Filtrar por nombre del PR
        user_guests = [guest for guest in all_guests if guest.get('Publica') == pr_name]
        logger.info(f"Encontrados {len(user_guests)} invitados para PR '{pr_name}' ({phone_number}).")
        return user_guests

    except gspread.exceptions.APIError as api_err:
        logger.error(f"Error de API al leer invitados para PR '{pr_name}': {api_err}")
        return []
    except Exception as e:
        logger.error(f"Error inesperado al obtener invitados para PR '{pr_name}': {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []

def generate_per_event_response(guests_by_event, pr_name, phone_number):
    """
    Genera una respuesta detallada agrupada por evento.

    Args:
        guests_by_event (dict): Diccionario {event_name: [lista_invitados_evento]}.
        pr_name (str): Nombre del PR.
        phone_number (str): Número de teléfono normalizado del PR.

    Returns:
        str: Respuesta formateada.
    """
    if not guests_by_event:
        return f"No tienes invitados registrados aún ({pr_name} / {phone_number}).\n\n(Selecciona un evento y envía la lista para agregar)."

    response_parts = [f"Resumen de tus invitados ({pr_name} / {phone_number}):"]
    grand_total = 0

    # Iterar sobre cada evento encontrado
    for event_name, event_guest_list in guests_by_event.items():
        if not event_guest_list: continue # Saltar si la lista está vacía por alguna razón

        response_parts.append(f"\n\n--- Evento: *{event_name}* ---")

        # Calcular conteos por género para ESTE evento
        event_categories = {}
        for guest in event_guest_list:
            gender_keys = ['Genero', 'genero', 'Género', 'Gender']
            gender = next((guest[k] for k in gender_keys if k in guest and guest[k]), 'Sin categoría')
            if not gender: gender = 'Sin categoría'
            event_categories[gender] = event_categories.get(gender, 0) + 1

        # Añadir conteos por género para el evento
        has_gender_counts = False
        for category, count in event_categories.items():
             if count > 0:
                display_category = category
                if category.lower() == "masculino": display_category = "Hombres"
                elif category.lower() == "femenino": display_category = "Mujeres"
                response_parts.append(f"📊 {display_category}: {count}")
                has_gender_counts = True
        if not has_gender_counts:
             response_parts.append("(No se especificó género)")


        # Añadir total para el evento
        total_event = len(event_guest_list)
        grand_total += total_event
        response_parts.append(f"Total Evento: {total_event} invitado{'s' if total_event != 1 else ''}")

        # Añadir detalle de invitados para el evento
        response_parts.append("\n📝 Detalle:")
        guests_by_gender_in_event = {}
        for guest in event_guest_list:
            gender_keys = ['Genero', 'genero', 'Género', 'Gender']
            gender = next((guest[k] for k in gender_keys if k in guest and guest[k]), 'Sin categoría')
            if not gender: gender = 'Sin categoría'
            if gender not in guests_by_gender_in_event:
                guests_by_gender_in_event[gender] = []
            guests_by_gender_in_event[gender].append(guest)

        for gender, guests in guests_by_gender_in_event.items():
            display_gender = gender
            if gender.lower() == "masculino": display_gender = "Hombres"
            elif gender.lower() == "femenino": display_gender = "Mujeres"
            response_parts.append(f"*{display_gender}*:")
            for guest in guests:
                name_keys = ['Nombre y Apellido', 'Nombre', 'nombre']
                email_keys = ['Email', 'email']
                full_name = next((guest[k] for k in name_keys if k in guest and guest[k]), '').strip()
                if not full_name:
                     nombre = guest.get('nombre', '')
                     apellido = guest.get('apellido', '')
                     full_name = f"{nombre} {apellido}".strip() or "?(sin nombre)"
                email = next((guest[k] for k in email_keys if k in guest and guest[k]), '?(sin email)')
                response_parts.append(f"  • {full_name} - {email}")

    # Añadir un total general al final (opcional pero útil)
    response_parts.append(f"\n\n---\nTotal General: {grand_total} invitado{'s' if grand_total != 1 else ''} en {len(guests_by_event)} evento{'s' if len(guests_by_event) != 1 else ''}.")

    return "\n".join(response_parts)


# MODIFICADO: Añadir event_name y usarlo en la respuesta
def generate_count_response(result, guests_data, phone_number, sentiment, event_name=None):
    """
    Genera una respuesta personalizada para la consulta de invitados con información detallada,
    opcionalmente específica para un evento.

    Args:
        result (dict): Resultados del conteo de invitados ({'Genero': count, 'Total': total})
        guests_data (list): Lista de diccionarios con detalles de invitados filtrados
        phone_number (str): Número de teléfono normalizado del usuario
        sentiment (str): Sentimiento detectado en el mensaje
        event_name (str, optional): Nombre del evento si el conteo fue filtrado. <-- NUEVO

    Returns:
        str: Respuesta personalizada
    """
    # Construir el encabezado dinámicamente
    if event_name:
        header_intro = f"Para el evento *{event_name}*, tus invitados registrados"
    else:
        header_intro = "Tus invitados registrados TOTALES"

    # Mensaje si no hay invitados
    if not result or result.get('Total', 0) == 0:
        if event_name:
            base_response = f"{header_intro} ({phone_number}):\n\n-- Ninguno --"
        else:
             base_response = f"{header_intro} ({phone_number}):\n\n-- Ninguno --"
        # Añadir instrucciones si no hay invitados
        base_response += "\n\n(Puedes añadir invitados seleccionando un evento y enviando la lista)."
        return base_response # Salir temprano si no hay invitados

    # Construir respuesta si SÍ hay invitados
    base_response = f"{header_intro} ({phone_number}):\n\n"

    # Mostrar conteo por género (excluyendo 'Total')
    has_gender_counts = False
    for category, count in result.items():
        if category != 'Total' and count > 0:
            display_category = category
            if category.lower() == "masculino":
                display_category = "Hombres"
            elif category.lower() == "femenino":
                display_category = "Mujeres"
            # Añadir emoji o formato
            base_response += f"📊 {display_category}: {count}\n"
            has_gender_counts = True

    if not has_gender_counts: # Si solo había 'Total' > 0 pero no géneros específicos
         base_response += "(No se especificó género para los invitados)\n"


    # Mostrar Total
    base_response += f"\nTotal: {result.get('Total', 0)} invitados\n\n"

    # Añadir detalle si hay datos
    if guests_data:
        base_response += "📝 Detalle de invitados:\n"
        # Agrupar invitados por género (usando los datos ya filtrados)
        guests_by_gender = {}
        for guest in guests_data:
            # Intentar obtener el género de forma flexible
            gender_keys = ['Genero', 'genero', 'Género', 'Gender']
            gender = next((guest[k] for k in gender_keys if k in guest and guest[k]), 'Sin categoría')
            if not gender: gender = 'Sin categoría' # Doble chequeo por si era ''

            if gender not in guests_by_gender:
                guests_by_gender[gender] = []
            guests_by_gender[gender].append(guest)

        # Mostrar invitados por género
        for gender, guests in guests_by_gender.items():
            display_gender = gender
            if gender.lower() == "masculino": display_gender = "Hombres"
            elif gender.lower() == "femenino": display_gender = "Mujeres"

            base_response += f"\n*{display_gender}*:\n"
            for guest in guests:
                # Intentar obtener nombre/apellido/email de forma flexible
                name_keys = ['Nombre y Apellido', 'Nombre', 'nombre']
                email_keys = ['Email', 'email']
                full_name = next((guest[k] for k in name_keys if k in guest and guest[k]), '').strip()
                # Si no encontramos 'Nombre y Apellido', intentar construirlo
                if not full_name:
                     nombre = guest.get('nombre', '')
                     apellido = guest.get('apellido', '')
                     full_name = f"{nombre} {apellido}".strip()

                email = next((guest[k] for k in email_keys if k in guest and guest[k]), '?(sin email)')

                base_response += f"  • {full_name} - {email}\n"

    # Personalizar según sentimiento (opcional, se puede quitar si no es necesario)
    if sentiment == "positivo":
        return f"{base_response}\n¡Gracias por tu interés!"
    elif sentiment == "negativo":
        return f"{base_response}\n¿Hay algo específico en lo que pueda ayudarte?"
    else:
        return base_response
    return base_response # Devolver la respuesta base sin personalización de sentimiento por ahora
    
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

# --- Función whatsapp_reply COMPLETA con Lógica VIP ---
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    global user_states
    response_text = None
    sender_phone_raw = None
    sender_phone_normalized = None
    sheet_conn = None
    is_vip = False # Variable para saber si el usuario es VIP

    try:
        data = request.form.to_dict()
        logger.info(f"Datos recibidos: {data}")
        sender_phone_raw = data.get('From')
        incoming_msg = data.get('Body', '').strip()

        if not incoming_msg or not sender_phone_raw:
            logger.error("Payload inválido: falta 'Body' o 'From'")
            return jsonify({"status": "error", "message": "Invalid payload"}), 400

        sender_phone_normalized = re.sub(r'\D', '', sender_phone_raw)
        sheet_conn = SheetsConnection() # Obtener instancia

        # --- Validación de número autorizado GENERAL ---
        authorized_phones = sheet_conn.get_authorized_phones()
        if not authorized_phones:
             logger.critical("No hay números autorizados cargados. Bloqueando.")
             return jsonify({"status": "ignored", "message": "Authorization list unavailable"}), 200
        if sender_phone_normalized not in authorized_phones:
            logger.warning(f"Mensaje de número NO AUTORIZADO: {sender_phone_raw}. Ignorando.")
            return jsonify({"status": "ignored", "message": "Unauthorized number"}), 200
        logger.info(f"Mensaje recibido de número AUTORIZADO: {sender_phone_raw}")
        # --- FIN Validación General ---

        # --- Chequeo VIP ---
        try:
            vip_phones = sheet_conn.get_vip_phones()
            if sender_phone_normalized in vip_phones: is_vip = True
        except Exception as vip_err: logger.error(f"Error al verificar estado VIP: {vip_err}")
        logger.info(f"Usuario {sender_phone_normalized} es VIP: {is_vip}")
        # --- Fin Chequeo VIP ---

        # Obtener estado actual y datos relevantes del usuario
        user_status = user_states.get(sender_phone_normalized, {}) # Usar dict vacío si no existe
        current_state = user_status.get('state', STATE_INITIAL) # Default a INITIAL si no hay estado
        selected_event = user_status.get('event')
        selected_guest_type = user_status.get('guest_type')
        available_events = user_status.get('available_events', []) # Recuperar eventos si existen

        logger.info(f"Usuario: {sender_phone_normalized}, VIP: {is_vip}, Estado: {current_state}, EventoSel: {selected_event}, TipoInvitadoSel: {selected_guest_type}")

        # Obtener referencias a las hojas (pueden ser None si falló la conexión/creación)
        guest_sheet = sheet_conn.get_guest_sheet()
        vip_guest_sheet = sheet_conn.get_vip_guest_sheet()

        # --- Lógica Principal de Estados ---
        if current_state == STATE_INITIAL:
            parsed_command = parse_message_enhanced(incoming_msg)
            command_type = parsed_command['command_type']

            if command_type == 'saludo':
                available_events = sheet_conn.get_available_events() # Obtener eventos frescos
                if not available_events:
                    response_text = "¡Hola! 👋 No encontré eventos disponibles."
                else:
                    event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                    base_response_text = f"¡Hola! 👋 Eventos disponibles:\n\n{event_list_text}\n\nResponde con el *número* del evento."
                    if is_vip:
                        vip_message = "\n\n✨ *Nota: Tienes opciones VIP disponibles.*"
                        response_text = base_response_text + vip_message
                    else:
                        response_text = base_response_text
                    # Guardar estado y eventos disponibles
                    user_states[sender_phone_normalized] = {'state': STATE_AWAITING_EVENT_SELECTION, 'event': None, 'available_events': available_events, 'guest_type': None} # Limpiar guest_type
            elif command_type == 'count':
                 # Lógica de conteo por evento (como en la respuesta anterior)
                 logger.info(f"Procesando comando 'count' para {sender_phone_normalized}")
                 all_user_guests = get_guests_by_pr(guest_sheet, sheet_conn, sender_phone_normalized)
                 guests_by_event = {}
                 # ... (agrupación por evento) ...
                 if all_user_guests:
                     for guest in all_user_guests:
                         event_name = guest.get('Evento', 'Sin Evento Asignado')
                         if not event_name: event_name = 'Sin Evento Asignado'
                         if event_name not in guests_by_event: guests_by_event[event_name] = []
                         guests_by_event[event_name].append(guest)
                 pr_name = sender_phone_normalized # Fallback
                 try: # Obtener nombre PR para respuesta
                     # Usar mapeo general para identificar al PR en la respuesta
                     phone_to_pr_map = sheet_conn.get_phone_pr_mapping()
                     pr_name_found = phone_to_pr_map.get(sender_phone_normalized)
                     if pr_name_found: pr_name = pr_name_found
                 except Exception as e: logger.error(f"Error buscando nombre PR para respuesta de conteo: {e}")
                 response_text = generate_per_event_response(guests_by_event, pr_name, sender_phone_normalized)
                 # Podríamos añadir conteo VIP aquí si se desea

            else: # Comando inicial no reconocido
                response_text = '¡Hola! 👋 Di "Hola" para ver eventos o pide tu "lista de invitados".'
                if is_vip: response_text += "\n(Tienes opciones VIP disponibles)"
                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Reset


        elif current_state == STATE_AWAITING_EVENT_SELECTION:
            try:
                choice = int(incoming_msg)
                if 1 <= choice <= len(available_events):
                    selected_event = available_events[choice - 1]
                    logger.info(f"Usuario {sender_phone_normalized} seleccionó evento: {selected_event}")

                    # Guardar el evento seleccionado *antes* de decidir el siguiente paso
                    user_status['event'] = selected_event

                    if is_vip:
                        # Preguntar tipo de invitado
                        response_text = f"Evento *{selected_event}* seleccionado. ¿Quieres añadir invitados *Normales* o *VIP*? Responde 'Normal' o 'VIP'."
                        # Pasar a estado de espera de tipo
                        user_status['state'] = STATE_AWAITING_GUEST_TYPE
                        user_states[sender_phone_normalized] = user_status # Actualizar estado
                    else:
                        # Usuario no VIP, ir directo a pedir datos normales
                        response_text = ( # Instrucciones para formato normal
                            f"Perfecto, evento: *{selected_event}*.\n\n"
                            "Ahora envíame la lista (Nombres primero, luego Emails):\n\n"
                            "*Hombres:* (Opcional)\nNombre Apellido\n...\n"
                            "email1@ejemplo.com\n...\n\n"
                            # ... (resto de instrucciones) ...
                            "Escribe 'cancelar' para cambiar."
                        )
                        user_status['state'] = STATE_AWAITING_GUEST_DATA
                        user_status['guest_type'] = 'Normal' # Guardar tipo por defecto
                        user_states[sender_phone_normalized] = user_status # Actualizar estado
                else: # Número inválido ...
                    event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                    response_text = f"Número '{incoming_msg}' inválido. Elige de la lista:\n\n{event_list_text}"
            except ValueError: # No envió número ...
                event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                response_text = f"Responde sólo con el *número* del evento:\n\n{event_list_text}"


        # --- NUEVO ESTADO: ESPERANDO TIPO DE INVITADO (SOLO VIPs) ---
        elif current_state == STATE_AWAITING_GUEST_TYPE:
            choice_lower = incoming_msg.lower()
            if choice_lower == 'vip':
                logger.info(f"Usuario VIP {sender_phone_normalized} eligió añadir tipo VIP.")
                user_status['state'] = STATE_AWAITING_GUEST_DATA
                user_status['guest_type'] = 'VIP'
                # --- INSTRUCCIONES VIP ACTUALIZADAS ---
                response_text = (
                    "Ok, modo VIP. Envíame la lista usando encabezados *opcionales* Hombres:/Mujeres: y el formato Nombres -> Emails:\n\n"
                    "*Hombres:* (Opcional)\n"
                    "Nombre Apellido VIP 1\n"
                    "...\n\n" # Separador
                    "email.vip1@ejemplo.com\n"
                    "...\n\n"
                    "*Mujeres:* (Opcional)\n"
                    "Nombre Apellido VIP Fem 1\n"
                    "...\n\n" # Separador
                    "email.vipfem1@ejemplo.com\n"
                    "...\n\n"
                    "⚠️ *Importante*: Cantidad de nombres y emails debe coincidir. Si no pones Hombres/Mujeres, intentaré adivinar.\n"
                    "(Escribe 'cancelar' para volver)."
                 )
                user_states[sender_phone_normalized] = user_status
            # ... (resto del elif para 'normal' sin cambios) ...
            elif choice_lower == 'normal' or choice_lower == 'normales':
                 # ... (instrucciones normales) ...
                 logger.info(f"Usuario VIP {sender_phone_normalized} eligió añadir tipo Normal.")
                 user_status['state'] = STATE_AWAITING_GUEST_DATA
                 user_status['guest_type'] = 'Normal'
                 response_text = ( # Instrucciones para formato normal
                    f"Ok, modo Normal. Envíame la lista (Nombres->Emails):\n\n"
                    "*Hombres:* (Opcional)\nNombre Apellido\n...\n"
                    "email1@ejemplo.com\n...\n\n"
                    "⚠️ *Importante*: La cantidad debe coincidir.\n"
                    "Escribe 'cancelar' para cambiar."
                 )
                 user_states[sender_phone_normalized] = user_status
            else:
                 response_text = f"No entendí '{incoming_msg}'. Por favor, responde 'Normal' o 'VIP'."
                # Mantener estado actual


        # --- ESTADO: ESPERANDO DATOS DEL INVITADO ---
        elif current_state == STATE_AWAITING_GUEST_DATA:
            if not selected_event: # Seguridad: si no hay evento, no continuar
                 logger.error(f"Estado AWAITING_GUEST_DATA pero no hay evento seleccionado para {sender_phone_normalized}")
                 response_text = "Hubo un problema, no sé para qué evento anotar. Por favor, empieza de nuevo diciendo 'Hola'."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear
            elif incoming_msg.lower() in ["cancelar", "salir", "cancel", "exit"]:
                response_text = "Operación cancelada. Salúdame de nuevo para elegir otro evento."
                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear
            else:
                # --- Decidir qué lógica de añadido usar ---
                if selected_guest_type == 'VIP':
                    logger.info(f"Procesando datos VIP (formato N->E) para {sender_phone_normalized}")
                    if not vip_guest_sheet:
                         logger.error(f"Hoja 'Invitados VIP' no disponible.")
                         response_text = "❌ Hubo un error interno (hoja VIP no accesible)."
                         user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear
                    else:
                        # --- Parsear Nombres y Emails VIP ---
                        # Llama a la función que espera formato Nombres->Emails y devuelve lista de dicts o None
                        parsed_vip_list = parse_vip_guest_list(incoming_msg)

                        if parsed_vip_list is None: # Error de formato o conteo detectado por el parser
                             response_text = ("⚠️ Formato incorrecto para VIPs. Asegúrate de enviar:\n\n"
                                              "Nombres (uno por línea)\n[Línea Vacía]\nEmails (uno por línea)\n\n"
                                              "La cantidad de nombres y emails debe coincidir.")
                             # Mantener estado para reintento (no resetear user_states)
                             # user_states[sender_phone_normalized]['state'] = STATE_AWAITING_GUEST_DATA # Ya está en este estado
                        else: # El parser devolvió una lista de diccionarios válida
                             # Obtener nombre del PR VIP (desde hoja VIP)
                             vip_pr_name = sender_phone_normalized # Fallback
                             try:
                                 vip_pr_map = sheet_conn.get_vip_phone_pr_mapping() # Usar mapeo VIP
                                 pr_name_found = vip_pr_map.get(sender_phone_normalized)
                                 if pr_name_found: vip_pr_name = pr_name_found
                                 else: logger.warning(f"No se encontró PR Name VIP para {sender_phone_normalized}")
                             except Exception as vip_map_err: logger.error(f"Error buscando nombre PR VIP: {vip_map_err}")

                             # --- Logging DEBUG ---
                             logger.info(f"DEBUG WHATSAPP_REPLY: Llamando a add_vip_guests_to_sheet con:")
                             logger.info(f"DEBUG WHATSAPP_REPLY:   vip_guest_sheet tipo: {type(vip_guest_sheet)}")
                             logger.info(f"DEBUG WHATSAPP_REPLY:   parsed_vip_list tipo: {type(parsed_vip_list)}")
                             logger.info(f"DEBUG WHATSAPP_REPLY:   parsed_vip_list contenido: {parsed_vip_list}")
                             logger.info(f"DEBUG WHATSAPP_REPLY:   vip_pr_name: {vip_pr_name}")
                             # --- FIN DEBUG ---

                             # Llamar a función de añadir VIPs (espera lista de dicts)
                             added_count = add_vip_guests_to_sheet(vip_guest_sheet, parsed_vip_list, vip_pr_name)

                             # Procesar resultado
                             if added_count > 0:
                                 response_text = f"✅ ¡Listo! Se anotaron *{added_count}* invitados VIP para *{selected_event}*."
                                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear
                             elif added_count == -1: # Datos parseados pero inválidos (nombre/email vacíos)
                                 response_text = f"⚠️ Algunos invitados VIP no tenían nombre o email válido y fueron omitidos."
                                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear igual
                             else: # added_count == 0 (Error interno o no se añadieron filas válidas)
                                 response_text = f"❌ Hubo un error al guardar los invitados VIP."
                                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear

                elif selected_guest_type == 'Normal' or selected_guest_type is None: # Tratar None como Normal
                    logger.info(f"Procesando datos Normales para {sender_phone_normalized}")
                    if not guest_sheet:
                         logger.error(f"Hoja 'Invitados' no disponible.")
                         response_text = "❌ Hubo un error interno (hoja Invitados no accesible)."
                         user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear
                    else:
                        # Lógica anterior para parsear y añadir normales
                        parsed = parse_message_enhanced(incoming_msg)
                        command_type = parsed['command_type']
                        data_lines = parsed['data']
                        categories = parsed['categories']
                        if command_type in ['add_guests', 'add_guests_split'] and data_lines:
                            # Llamada a función existente add_guests_to_sheet
                            added_count = add_guests_to_sheet(
                                guest_sheet, data_lines, sender_phone_normalized,
                                selected_event, sheet_conn, categories, command_type
                            )
                            if added_count > 0:
                                response_text = f"✅ ¡Listo! Se anotaron *{added_count}* invitados normales para *{selected_event}*."
                                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear
                            elif added_count == 0: response_text = f"⚠️ No pude anotar invitados normales. Revisa el formato."
                            elif added_count == -1: response_text = f"⚠️ Faltan emails normales o no son válidos. Revisa."
                            else:
                                response_text = "❌ Hubo un error al guardar invitados normales."
                                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear
                        else: # Mensaje no reconocido como lista normal
                             response_text = (f"Espero la lista normal para *{selected_event}* (Nombres->Emails).\nO 'cancelar'.")
                else: # Tipo de invitado desconocido
                     logger.error(f"Estado AWAITING_GUEST_DATA con guest_type inválido: {selected_guest_type}")
                     response_text = "Hubo un error con tu selección. Empieza de nuevo ('Hola')."
                     user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None} # Resetear


        # --- Fin Lógica Principal de Estados ---

        # Enviar la respuesta calculada (si hay una)
        if response_text:
            if not send_twilio_message(sender_phone_raw, response_text):
                 logger.error(f"Fallo al enviar mensaje de respuesta a {sender_phone_raw}")
                 return jsonify({"status": "processed_with_send_error"}), 200
            else:
                 logger.info(f"Respuesta enviada a {sender_phone_raw}: {response_text[:100]}...")
                 return jsonify({"status": "success"}), 200
        else:
             logger.warning("No se generó texto de respuesta para enviar.")
             return jsonify({"status": "processed_no_reply"}), 200

    # ... (Manejo de excepción general) ...
    except Exception as e:
        logger.error(f"Error inesperado GRANDE en el webhook: {e}")
        import traceback
        logger.error(traceback.format_exc())
        if sender_phone_raw and response_text is None:
             error_message = "Lo siento, ocurrió un error inesperado."
             send_twilio_message(sender_phone_raw, error_message)
        return jsonify({"status": "error", "message": "Internal server error"}), 500