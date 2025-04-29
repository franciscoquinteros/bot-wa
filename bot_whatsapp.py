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
import traceback
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

    def get_sheet_by_event_name(self, event_name):
        """
        Obtiene o crea una hoja específica para un evento determinado.
        Con mejor manejo de errores y log detallado.
        """
        if not event_name:
            logger.error("Intentando obtener hoja para evento sin nombre.")
            return None
            
        try:
            # Intentar obtener la hoja existente
            logger.info(f"Intentando obtener hoja para evento '{event_name}'...")
            event_sheet = self.spreadsheet.worksheet(event_name)
            logger.info(f"Hoja para evento '{event_name}' encontrada. ID: {event_sheet.id}")
            
            # Verificar que realmente podemos acceder (prueba de lectura)
            try:
                cell_value = event_sheet.acell('A1').value
                logger.info(f"Prueba de lectura exitosa: A1 = '{cell_value}'")
                
                # Verificar si la hoja tiene la columna ENVIADO, y si no, agregarla
                headers = event_sheet.row_values(1)
                if len(headers) < 7 or 'ENVIADO' not in headers:
                    logger.info(f"Actualizando encabezados para incluir columna ENVIADO en '{event_name}'...")
                    headers.append('ENVIADO') if 'ENVIADO' not in headers else None
                    event_sheet.update('A1:G1', [headers])
                    logger.info(f"Encabezados actualizados en hoja existente '{event_name}'")
                    
                    # Aplicar casillas de verificación a la columna ENVIADO
                    try:
                        add_checkboxes_to_column(event_sheet, 7)  # 7 para columna G (ENVIADO)
                    except Exception as checkbox_err:
                        logger.error(f"Error al aplicar casillas de verificación: {checkbox_err}")
            except Exception as read_err:
                logger.error(f"La hoja existe pero no se puede leer: {read_err}")
            
            return event_sheet
        except gspread.exceptions.WorksheetNotFound:
            # Si no existe, crear nueva hoja
            logger.info(f"Hoja para evento '{event_name}' no encontrada. Intentando crearla...")
            try:
                # Crear hoja con las columnas necesarias (ahora 7 en lugar de 6)
                new_sheet = self.spreadsheet.add_worksheet(title=event_name, rows="1", cols="7")
                logger.info(f"Hoja creada con ID: {new_sheet.id}")
                
                # Definir encabezados incluyendo la columna ENVIADO
                expected_headers = ['Nombre y Apellido', 'Email', 'Genero', 'Publica', 'Evento', 'Timestamp', "ENVIADO"]
                update_result = new_sheet.update('A1:G1', [expected_headers])  # Cambiado a G1 para incluir 7 columnas
                logger.info(f"Encabezados añadidos: {update_result}")
                
                # Aplicar casillas de verificación a la columna ENVIADO
                try:
                    add_checkboxes_to_column(new_sheet, 7)
                except Exception as checkbox_err:
                    logger.error(f"Error al aplicar casillas de verificación: {checkbox_err}")
                
                # Verificar creación con prueba de lectura
                try:
                    cell_value = new_sheet.acell('A1').value
                    logger.info(f"Verificación de nueva hoja exitosa: A1 = '{cell_value}'")
                except Exception as verify_err:
                    logger.error(f"No se pudo verificar la nueva hoja: {verify_err}")
                    
                logger.info(f"Hoja para evento '{event_name}' creada exitosamente")
                return new_sheet
            except Exception as e:
                logger.error(f"Error al crear hoja para evento '{event_name}': {e}")
                import traceback
                logger.error(traceback.format_exc())
                return None
        except Exception as e:
            logger.error(f"Error al obtener hoja para evento '{event_name}': {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
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

# Definir fuera de cualquier clase (como función global)
def add_checkboxes_to_column(sheet, column_index, start_row=2, end_row=None):
    """
    Agrega casillas de verificación (checkboxes) a una columna específica.
    
    Args:
        sheet: Objeto de hoja de Google Sheets
        column_index: Índice de la columna (1-based, ejemplo: 7 para columna G)
        start_row: Fila inicial (default: 2, para saltar encabezados)
        end_row: Fila final (default: None, para toda la columna)
    """
    try:
        if end_row is None:
            # Obtener todas las filas para determinar el rango
            all_values = sheet.get_all_values()
            end_row = len(all_values) + 10  # Agregar algunas filas adicionales para futuras entradas
        
        # Construir el rango en notación A1 (ej: G2:G100)
        start_cell = gspread.utils.rowcol_to_a1(start_row, column_index)
        end_cell = gspread.utils.rowcol_to_a1(end_row, column_index)
        range_name = f"{start_cell}:{end_cell}"
        
        # Crear la regla de validación para checkboxes
        checkbox_rule = {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet.id,
                    "startRowIndex": start_row - 1,  # Ajustar a 0-based index
                    "endRowIndex": end_row,
                    "startColumnIndex": column_index - 1,  # Ajustar a 0-based index
                    "endColumnIndex": column_index
                },
                "rule": {
                    "condition": {
                        "type": "BOOLEAN"
                    }
                }
            }
        }
        
        # Aplicar la regla usando la API avanzada
        sheet.spreadsheet.batch_update({"requests": [checkbox_rule]})
        
        logger.info(f"Casillas de verificación agregadas a la columna {column_index} (rango {range_name})")
        return True
    
    except Exception as e:
        logger.error(f"Error al agregar casillas de verificación: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

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
    Versión REVISADA para mayor robustez y mensajes de error detallados.
    Permite categorías vacías mientras haya al menos una categoría válida.

    Args:
        lines (list): Lista de líneas crudas del mensaje del usuario.

    Returns:
        tuple: (list, dict) donde:
            - list: Lista de diccionarios con info estructurada, o lista vacía si hay error grave.
                   {'nombre': str, 'apellido': str, 'email': str, 'genero': str}
            - dict: Información del error si ocurrió, o None si no hubo errores:
                   {'error_type': str, 'category': str, 'names_count': int, 'emails_count': int}
    """
    guests = []
    error_info = None
    
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
    at_least_one_valid_category = False
    
    for category_key, data in data_by_category.items():
        names = data['names']
        emails = data['emails']
        # Usar 'Otro' si la categoría es 'General' o no está en el map
        genero = category_map.get(category_key, "Otro")

        logger.info(f"Procesando categoría '{category_key}' ({genero}): {len(names)} nombres, {len(emails)} emails.")

        # Permitir que una categoría esté vacía (0 nombres y 0 emails)
        if not names and not emails:
            logger.info(f"Categoría '{category_key}' está vacía. Saltando.")
            continue # Saltar categoría vacía

        # Verificar si hay desbalance entre nombres y emails
        if len(names) != len(emails):
            logger.error(f"¡ERROR DE FORMATO! Desbalance en categoría '{category_key}': {len(names)} nombres vs {len(emails)} emails. ¡No se agregarán invitados de esta categoría!")
            error_found_in_pairing = True
            
            # Guardar información del error para reportarlo específicamente
            # Solo sobrescribimos error_info si no hay categorías válidas todavía
            if not at_least_one_valid_category:
                error_info = {
                    'error_type': 'desbalance',
                    'category': category_key,
                    'names_count': len(names),
                    'emails_count': len(emails)
                }
            continue # Saltar esta categoría por error grave

        # Verificar si la categoría tiene datos válidos (al menos 1 nombre y 1 email)
        if len(names) == 0 or len(emails) == 0:
            logger.warning(f"Categoría '{category_key}' incompleta: {len(names)} nombres, {len(emails)} emails. Saltando.")
            # No marcamos esto como error si otra categoría tiene datos válidos
            continue

        # Esta es una categoría válida
        at_least_one_valid_category = True

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
                continue

            guest_info = {
                "nombre": nombre,
                "apellido": apellido,
                "email": email,
                "genero": genero # Usar el género de la categoría
            }
            guests.append(guest_info)
            logger.debug(f"Invitado emparejado OK: {full_name} - {email} ({genero})")

    # Verificar si se procesó al menos una categoría válida
    if not at_least_one_valid_category:
        if error_info:
            # Ya tenemos información de error de una categoría con desbalance
            pass
        elif data_by_category:
            # No hay categorías válidas, pero hay al menos una categoría
            error_info = {
                'error_type': 'all_categories_invalid',
                'categories': list(data_by_category.keys())
            }
        else:
            # No se encontraron categorías
            error_info = {
                'error_type': 'no_valid_data',
                'message': 'No se encontraron datos válidos para procesar'
            }
    
    # Si no hay error_info pero tampoco hay invitados, algo salió mal
    if not guests and not error_info:
        error_info = {
            'error_type': 'format_error',
            'category': 'General',
            'message': 'Formato no reconocido'
        }

    logger.info(f"Extracción formato dividido completada. Total invitados estructurados: {len(guests)}")
    logger.info(f"DEBUG PARSER OUTPUT: Estructura final devuelta por el parser: {guests}") # Imprime la lista completa
    return (guests, error_info)

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
def add_guests_to_sheet(sheet, guests_data, phone_number, event_name, sheet_conn, categories=None, command_type='add_guests'):
    """
    Agrega invitados a la hoja con información estructurada, incluyendo el evento
    y usando el nombre del PR en lugar del número en la columna 'Publica'.
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # --- Log mejorado para depuración ---
        logger.info(f"INICIO add_guests_to_sheet para evento '{event_name}' - Phone: {phone_number}")
        logger.info(f"Verificando objeto sheet: {sheet} - Tipo: {type(sheet)}")
        
        # --- Verificar encabezados para 6 columnas ---
        expected_headers = ['Nombre y Apellido', 'Email', 'Genero', 'Publica', 'Evento', 'Timestamp', "ENVIADO"]
        try:
            headers = sheet.row_values(1)
            logger.info(f"Encabezados existentes: {headers}")
        except gspread.exceptions.APIError as api_err:
            logger.error(f"Error API al leer encabezados: {api_err}")
            if "exceeds grid limits" in str(api_err):
                headers = []
                logger.info("Hoja detectada como vacía (sin encabezados)")
            else:
                raise api_err

        # Actualizar si los encabezados no coinciden o la hoja está vacía
        if not headers or len(headers) < len(expected_headers) or headers[:len(expected_headers)] != expected_headers:
            logger.info(f"Actualizando/Creando encabezados en la hoja '{sheet.title}': {expected_headers}")
            try:
                sheet.update('A1:F1', [expected_headers])
                logger.info("Encabezados actualizados correctamente")
            except Exception as header_err:
                logger.error(f"ERROR al actualizar encabezados: {header_err}")
                # Continuar intento de añadir datos incluso si falla actualización de encabezados

        # --- Procesar datos de invitados (resto del código original) ---
        # ... (código original hasta crear rows_to_add)

        # --- MEJORA: Log de las filas que se intentan añadir ---
        if rows_to_add:
            try:
                logger.info(f"Intentando añadir {len(rows_to_add)} filas. Primera fila: {rows_to_add[0]}")
                # Añadir a la hoja con manejo de errores más detallado
                try:
                    result = sheet.append_rows(rows_to_add, value_input_option='USER_ENTERED')
                    logger.info(f"Resultado de append_rows: {result}")
                    # Verificación adicional después de append
                    try:
                        all_values = sheet.get_all_values()
                        logger.info(f"Después de append_rows, la hoja tiene {len(all_values)} filas")
                        # Si hay menos de 3 filas, mostrar todo el contenido para debugging
                        if len(all_values) < 3:
                            logger.info(f"Contenido actual de la hoja: {all_values}")
                    except Exception as verify_err:
                        logger.error(f"Error al verificar contenido después de append: {verify_err}")
                    
                    logger.info(f"Agregados {len(rows_to_add)} invitados para evento '{event_name}' por {phone_number}")
                    return len(rows_to_add)
                except gspread.exceptions.APIError as api_err:
                    logger.error(f"Error API DETALLADO al agregar filas: {api_err}")
                    # Aquí puedes agregar más manejo específico según el tipo de error de API
                    if "insufficient permissions" in str(api_err).lower():
                        logger.critical("ERROR DE PERMISOS: La cuenta de servicio no tiene permisos de escritura")
                    elif "invalid value" in str(api_err).lower():
                        logger.error(f"ERROR DE VALOR: Posible formato incorrecto en los datos: {rows_to_add[0]}")
                    return 0
                except Exception as append_err:
                    logger.error(f"Error inesperado en append_rows: {append_err}")
                    import traceback
                    logger.error(traceback.format_exc())
                    return 0
            except Exception as pre_append_err:
                logger.error(f"Error antes de llamar a append_rows: {pre_append_err}")
                import traceback
                logger.error(traceback.format_exc())
                return 0
        else:
            logger.warning("No se generaron filas válidas para añadir a la hoja.")
            return 0

    except Exception as e:
        logger.error(f"Error GRANDE en add_guests_to_sheet: {e}")
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
        expected_headers = ['Nombre y Apellido', 'Email', 'Genero', 'Publica', 'Evento', 'Timestamp', "ENVIADO"]
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
                timestamp,                      # Columna F: Timestamp
                False                           # Columna G: ENVIADO (checkbox desmarcado)
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
    'Hombres:'/'Mujeres:'. Permite categorías vacías.
    
    Args:
        message_body (str): Texto del mensaje del usuario
        
    Returns:
        tuple: (list, dict) donde:
            - list: Lista de diccionarios [{'nombre': n, 'email': e, 'genero': g}] o None.
                  'genero' será "Hombre", "Mujer", o None si no había encabezado.
            - dict: Información del error si ocurrió, o None si no hubo errores.
    """
    lines = [line.strip() for line in message_body.split('\n') if line.strip()]
    error_info = None
    
    if not lines:
        logger.warning("parse_vip_guest_list: Mensaje vacío.")
        error_info = {
            'error_type': 'empty_message',
            'names_count': 0,
            'emails_count': 0
        }
        return None, error_info

    # Estructura para almacenar nombres y emails por categoría
    categories = {
        'default': {'names': [], 'emails': []},  # Categoría por defecto
        'Hombre': {'names': [], 'emails': []},
        'Mujer': {'names': [], 'emails': []}
    }
    
    current_category = 'default'
    parsing_names = True
    
    for line in lines:
        line_lower = line.lower()

        # Detectar encabezados de Género
        if line_lower.startswith('hombres'):
            current_category = 'Hombre'
            parsing_names = True  # Después de un encabezado, esperamos nombres
            logger.debug("parse_vip_guest_list: Detectado encabezado 'Hombres'.")
            continue # Saltar la línea del encabezado
            
        elif line_lower.startswith('mujeres'):
            current_category = 'Mujer'
            parsing_names = True  # Después de un encabezado, esperamos nombres
            logger.debug("parse_vip_guest_list: Detectado encabezado 'Mujeres'.")
            continue # Saltar la línea del encabezado

        # Detectar Emails
        is_email = '@' in line and '.' in line.split('@')[-1] and len(line.split('@')[0]) > 0
        if is_email:
            if parsing_names:
                # Si es el primer email que encontramos, cambiamos a modo email
                parsing_names = False
            
            if re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", line):
                categories[current_category]['emails'].append(line)
            else:
                logger.warning(f"parse_vip_guest_list: Línea '{line}' parece email pero no valida regex.")
        elif parsing_names:
            # Añadir nombre si parece un nombre válido
            if re.match(r"^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s'.]+$", line) and len(line) > 1:
                categories[current_category]['names'].append({'nombre': line, 'genero': current_category if current_category != 'default' else None})
            else:
                logger.warning(f"parse_vip_guest_list: Línea '{line}' ignorada (modo nombre).")
        else:
            # Si ya pasamos al modo email, ignoramos líneas que no son emails
            logger.warning(f"parse_vip_guest_list: Ignorando línea no-email '{line}' en modo emails.")

    # Verificar si hay datos válidos en alguna categoría
    all_names = []
    all_emails = []
    valid_categories = []
    
    for cat_name, cat_data in categories.items():
        names_count = len(cat_data['names'])
        emails_count = len(cat_data['emails'])
        
        logger.info(f"Categoría '{cat_name}': {names_count} nombres, {emails_count} emails.")
        
        # Verificar si esta categoría tiene datos
        if names_count > 0 and emails_count > 0:
            # Verificar si hay desbalance
            if names_count != emails_count:
                logger.error(f"Desbalance en categoría '{cat_name}': {names_count} nombres, {emails_count} emails.")
                # Guardamos información del error pero seguimos procesando otras categorías
                if error_info is None:
                    error_info = {
                        'error_type': 'desbalance',
                        'category': cat_name,
                        'names_count': names_count,
                        'emails_count': emails_count
                    }
            else:
                # Categoría válida
                valid_categories.append(cat_name)
                all_names.extend(cat_data['names'])
                all_emails.extend(cat_data['emails'])
        elif names_count > 0 or emails_count > 0:
            # Categoría incompleta (tiene nombres o emails, pero no ambos)
            logger.warning(f"Categoría '{cat_name}' incompleta: {names_count} nombres, {emails_count} emails.")
            if error_info is None:
                error_info = {
                    'error_type': 'incomplete_category',
                    'category': cat_name,
                    'names_count': names_count,
                    'emails_count': emails_count
                }
        # Si la categoría está vacía (0 nombres, 0 emails), la ignoramos
    
    # Si no hay categorías válidas
    if not valid_categories:
        logger.error("No hay categorías válidas.")
        if error_info is None:
            error_info = {
                'error_type': 'no_valid_categories',
                'message': 'No se encontraron categorías con datos válidos'
            }
        return None, error_info
    
    # Crear los pares de invitados
    paired_guests = []
    for category in valid_categories:
        cat_data = categories[category]
        for i in range(min(len(cat_data['names']), len(cat_data['emails']))):
            name_info = cat_data['names'][i]
            email_clean = cat_data['emails'][i].strip()
            
            if name_info.get('nombre') and email_clean:
                paired_guests.append({
                    'nombre': name_info['nombre'].strip(),
                    'email': email_clean,
                    'genero': name_info['genero']
                })
    
    logger.info(f"Total de invitados VIP emparejados: {len(paired_guests)}")
    return paired_guests, None if paired_guests else error_info
    
# MODIFICADO: Añadir sheet_conn, buscar PR name y filtrar por él.
def get_guests_by_pr(sheet_conn, phone_number):
    """
    Obtiene todos los registros de invitados asociados a un número de teléfono de publicador,
    buscando en todas las hojas de eventos.

    Args:
        sheet_conn: Instancia de SheetsConnection para acceder a las hojas
        phone_number (str): Número de teléfono NORMALIZADO del publicador

    Returns:
        dict: Diccionario {nombre_evento: [lista_invitados]} de invitados filtrados por evento
    """
    pr_name = None
    try:
        # Obtener el nombre del PR
        phone_to_pr_map = sheet_conn.get_phone_pr_mapping()
        pr_name = phone_to_pr_map.get(phone_number)

        if not pr_name:
            logger.warning(f"No se encontró PR Name para el número {phone_number} al buscar invitados.")
            pr_name = phone_number  # Usar el número como fallback para búsqueda

        # Obtener lista de eventos disponibles
        available_events = sheet_conn.get_available_events()
        if not available_events:
            logger.warning("No hay eventos disponibles para buscar invitados.")
            return {}

        # Diccionario para almacenar invitados por evento
        guests_by_event = {}

        # Buscar en cada hoja de evento
        for event_name in available_events:
            try:
                # Obtener la hoja específica del evento
                event_sheet = sheet_conn.get_sheet_by_event_name(event_name)
                if not event_sheet:
                    logger.warning(f"No se pudo acceder a la hoja del evento '{event_name}'.")
                    continue

                # Obtener todos los registros de la hoja
                all_guests = event_sheet.get_all_records()
                if not all_guests:
                    # Si la hoja está vacía (solo tiene encabezados)
                    logger.info(f"Hoja '{event_name}' no tiene invitados registrados.")
                    continue

                # Filtrar por nombre del PR o número de teléfono (como fallback)
                event_guests = [guest for guest in all_guests if 
                               guest.get('Publica') == pr_name or 
                               guest.get('Publica') == phone_number]
                
                if event_guests:
                    guests_by_event[event_name] = event_guests
                    logger.info(f"Encontrados {len(event_guests)} invitados para PR '{pr_name}' en evento '{event_name}'.")
                
            except Exception as event_err:
                logger.error(f"Error al buscar invitados en evento '{event_name}': {event_err}")
                continue

        return guests_by_event

    except Exception as e:
        logger.error(f"Error global en get_guests_by_pr: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {}

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

@app.route('/test_sheet', methods=['GET'])
def test_sheet_write():
    """
    Endpoint de prueba para verificar la capacidad de escritura en Google Sheets.
    Acceder a esta ruta intentará escribir una fila de prueba en cada hoja.
    """
    try:
        sheet_conn = SheetsConnection()
        results = {}
        
        # 1. Probar escritura en hoja principal de invitados
        try:
            guest_sheet = sheet_conn.get_guest_sheet()
            if guest_sheet:
                test_row = ["TEST", "test@example.com", "Otro", "Test PR", "Test Event", datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
                result = guest_sheet.append_row(test_row, value_input_option='USER_ENTERED')
                results["main_sheet"] = {"status": "success", "result": str(result)}
            else:
                results["main_sheet"] = {"status": "error", "message": "No se pudo obtener la hoja principal"}
        except Exception as e:
            results["main_sheet"] = {"status": "error", "message": str(e)}
        
        # 2. Probar escritura en hoja VIP
        try:
            vip_sheet = sheet_conn.get_vip_guest_sheet()
            if vip_sheet:
                test_row = ["TEST VIP", "Test PR"]
                result = vip_sheet.append_row(test_row, value_input_option='USER_ENTERED')
                results["vip_sheet"] = {"status": "success", "result": str(result)}
            else:
                results["vip_sheet"] = {"status": "error", "message": "No se pudo obtener la hoja VIP"}
        except Exception as e:
            results["vip_sheet"] = {"status": "error", "message": str(e)}
        
        # 3. Probar escritura en una hoja de evento específica
        try:
            events = sheet_conn.get_available_events()
            if events:
                event_sheet = sheet_conn.get_sheet_by_event_name(events[0])
                if event_sheet:
                    test_row = ["TEST EVENT", "test@example.com", "Otro", "Test PR", events[0], datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
                    result = event_sheet.append_row(test_row, value_input_option='USER_ENTERED')
                    results["event_sheet"] = {"status": "success", "event": events[0], "result": str(result)}
                else:
                    results["event_sheet"] = {"status": "error", "message": f"No se pudo obtener la hoja para evento {events[0]}"}
            else:
                results["event_sheet"] = {"status": "error", "message": "No hay eventos disponibles"}
        except Exception as e:
            results["event_sheet"] = {"status": "error", "message": str(e)}
        
        return jsonify({"status": "complete", "results": results})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/setup_checkboxes', methods=['GET'])
def setup_all_checkboxes():
    """Configura casillas de verificación en la columna ENVIADO de todas las hojas"""
    try:
        sheet_conn = SheetsConnection()
        results = {}
        
        # Obtener todas las hojas
        spreadsheet = sheet_conn.get_sheet()
        all_worksheets = spreadsheet.worksheets()
        
        for worksheet in all_worksheets:
            # Agregar la columna ENVIADO si no existe
            try:
                headers = worksheet.row_values(1)
                if 'ENVIADO' not in headers:
                    headers.append('ENVIADO')
                    worksheet.update('A1:G1', [headers])
                
                # Aplicar casillas de verificación
                column_index = headers.index('ENVIADO') + 1  # Convertir índice 0-based a 1-based
                add_checkboxes_to_column(worksheet, column_index)
                
                results[worksheet.title] = "Configuración exitosa"
            except Exception as e:
                results[worksheet.title] = f"Error: {str(e)}"
        
        return jsonify({"status": "complete", "results": results})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# --- Función whatsapp_reply COMPLETA con Lógica VIP ---
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    global user_states
    response_text = None
    sender_phone_raw = None
    sender_phone_normalized = None
    sheet_conn = None
    is_vip = False # Variable para saber si el usuario es VIP
    # error_info = None # CORREGIDO: Se inicializa localmente donde se usa

    try:
        data = request.form.to_dict()
        logger.info(f"Datos recibidos: {data}")
        sender_phone_raw = data.get('From')
        incoming_msg = data.get('Body', '').strip()

        if not incoming_msg or not sender_phone_raw:
            logger.error("Payload inválido: falta 'Body' o 'From'")
            return jsonify({"status": "error", "message": "Invalid payload"}), 400

        sender_phone_normalized = re.sub(r'\D', '', sender_phone_raw) # Normalizar número (quitar 'whatsapp:', '+', etc.)
        sheet_conn = SheetsConnection() # Obtener instancia

        # --- Validación de número autorizado GENERAL ---
        authorized_phones = sheet_conn.get_authorized_phones()
        if not authorized_phones:
            logger.critical("No hay números autorizados cargados. Bloqueando.")
            # Considera no retornar 200 OK si la lista es vital y no carga
            return jsonify({"status": "ignored", "message": "Authorization list unavailable"}), 503 # Service Unavailable podría ser mejor

        if sender_phone_normalized not in authorized_phones:
            logger.warning(f"Mensaje de número NO AUTORIZADO: {sender_phone_raw} ({sender_phone_normalized}). Ignorando.")
            return jsonify({"status": "ignored", "message": "Unauthorized number"}), 200 # OK para Twilio, pero ignoramos
        logger.info(f"Mensaje recibido de número AUTORIZADO: {sender_phone_raw} ({sender_phone_normalized})")
        # --- FIN Validación General ---

        # --- Chequeo VIP ---
        try:
            # Asegúrate que get_vip_phones devuelva un set para eficiencia
            vip_phones = sheet_conn.get_vip_phones()
            if vip_phones is not None and sender_phone_normalized in vip_phones:
                 is_vip = True
        except Exception as vip_err:
             logger.error(f"Error al verificar estado VIP para {sender_phone_normalized}: {vip_err}")
        logger.info(f"Usuario {sender_phone_normalized} es VIP: {is_vip}")
        # --- Fin Chequeo VIP ---

        # --- VERIFICAR SI ES UN SALUDO PARA REINICIAR EL FLUJO ---
        # Lista de patrones de saludo (compilados para eficiencia si son muchos)
        saludo_patterns = [
            re.compile(r'^hola$', re.IGNORECASE),
            re.compile(r'^buenos días$', re.IGNORECASE),
            re.compile(r'^buenas tardes$', re.IGNORECASE),
            re.compile(r'^buenas noches$', re.IGNORECASE),
            re.compile(r'^saludos$', re.IGNORECASE),
            re.compile(r'^hi$', re.IGNORECASE),
            re.compile(r'^hey$', re.IGNORECASE),
            re.compile(r'^hello$', re.IGNORECASE),
            re.compile(r'^ola$', re.IGNORECASE),
            re.compile(r'^buen día$', re.IGNORECASE)
        ]

        # Verificar si el mensaje es un saludo
        is_greeting = False
        for pattern in saludo_patterns:
            if pattern.match(incoming_msg): # Usar match para inicio de línea
                is_greeting = True
                break

        # Si es un saludo, reiniciar el flujo sin importar el estado actual
        if is_greeting:
            logger.info(f"Saludo detectado. Reiniciando flujo para {sender_phone_normalized}")
            available_events = sheet_conn.get_available_events()
            if not available_events:
                response_text = "¡Hola! 👋 No encontré eventos disponibles en este momento."
                # Resetear estado aunque no haya eventos, para que no quede 'colgado'
                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'available_events': [], 'guest_type': None}
            else:
                event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                base_response_text = f"¡Hola! 👋 Eventos disponibles:\n\n{event_list_text}\n\nResponde con el *número* del evento que deseas gestionar."
                if is_vip:
                    vip_message = "\n\n✨ *Nota: Como PR VIP, tienes acceso especial.*"
                    response_text = base_response_text + vip_message
                else:
                    response_text = base_response_text
                # Guardar estado y eventos disponibles
                user_states[sender_phone_normalized] = {'state': STATE_AWAITING_EVENT_SELECTION, 'event': None, 'available_events': available_events, 'guest_type': None}

            # Enviar la respuesta y finalizar la ejecución para el saludo
            if response_text:
                if not send_twilio_message(sender_phone_raw, response_text):
                    logger.error(f"Fallo al enviar mensaje de respuesta de saludo a {sender_phone_raw}")
                    # OK para Twilio, pero loggeamos el error de envío
                    return jsonify({"status": "processed_with_send_error"}), 200
                else:
                    logger.info(f"Respuesta a saludo enviada a {sender_phone_raw}")
                    return jsonify({"status": "success"}), 200
            else:
                # Esto no debería pasar si available_events es None o tiene elementos
                logger.error("No se generó texto de respuesta para el saludo, ¡esto es inesperado!")
                return jsonify({"status": "processed_no_reply"}), 200

        # --- FIN MANEJO DE SALUDO ---

        # --- Obtener estado actual y datos relevantes del usuario (si no fue saludo) ---
        user_status = user_states.get(sender_phone_normalized, {}) # Usar dict vacío si no existe
        current_state = user_status.get('state', STATE_INITIAL) # Default a INITIAL si no hay estado
        selected_event = user_status.get('event')
        selected_guest_type = user_status.get('guest_type')
        # Recuperar eventos si existen en el estado, crucial para AWAITING_EVENT_SELECTION
        available_events = user_status.get('available_events', [])

        logger.info(f"Usuario: {sender_phone_normalized}, VIP: {is_vip}, Estado: {current_state}, EventoSel: {selected_event}, TipoInvitadoSel: {selected_guest_type}, EventosEnEstado: {len(available_events)}")

        # Obtener referencia a la hoja de invitados VIP (puede ser None si no existe)
        # Se obtiene aquí porque puede ser necesaria en varios estados
        vip_guest_sheet = sheet_conn.get_vip_guest_sheet()

        # ====================================
        # --- Lógica Principal de Estados ---
        # ====================================

        if current_state == STATE_INITIAL:
            # En estado inicial, esperamos saludo (ya manejado arriba) o comando 'count'/'lista'
            parsed_command = parse_message_enhanced(incoming_msg) # Re-parsear por si acaso
            command_type = parsed_command['command_type']

            # Nota: El saludo ya se manejó y reinició el estado, no debería llegar aquí como 'saludo'
            # Si llega, es un flujo inesperado, pero podemos manejarlo como el saludo inicial.
            if command_type == 'saludo':
                 # Redirigir a la lógica de saludo (copiada/refactorizada)
                 logger.warning(f"Flujo inesperado: Comando 'saludo' recibido en STATE_INITIAL post-chequeo inicial. Reiniciando de nuevo.")
                 available_events = sheet_conn.get_available_events()
                 if not available_events:
                     response_text = "¡Hola de nuevo! 👋 No encontré eventos disponibles."
                     user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'available_events': [], 'guest_type': None}
                 else:
                     event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                     base_response_text = f"¡Hola de nuevo! 👋 Eventos disponibles:\n\n{event_list_text}\n\nResponde con el *número* del evento."
                     if is_vip: response_text = base_response_text + "\n\n✨ *Opciones VIP disponibles.*"
                     else: response_text = base_response_text
                     user_states[sender_phone_normalized] = {'state': STATE_AWAITING_EVENT_SELECTION, 'event': None, 'available_events': available_events, 'guest_type': None}

            elif command_type == 'count':
                # Lógica de conteo
                logger.info(f"Procesando comando 'count' para {sender_phone_normalized}")
                guests_by_event = get_guests_by_pr(sheet_conn, sender_phone_normalized)

                pr_name = sender_phone_normalized # Fallback
                try:
                    # Usar mapeo general O VIP según corresponda para mostrar el nombre correcto
                    pr_map = sheet_conn.get_vip_phone_pr_mapping() if is_vip else sheet_conn.get_phone_pr_mapping()
                    if pr_map:
                         pr_name_found = pr_map.get(sender_phone_normalized)
                         if pr_name_found: pr_name = pr_name_found
                except Exception as e:
                     logger.error(f"Error buscando nombre PR ({'VIP' if is_vip else 'General'}) para respuesta de conteo: {e}")

                response_text = generate_per_event_response(guests_by_event, pr_name, sender_phone_normalized)
                # Mantener estado INITIAL después de contar
                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Limpiar eventos también


            else: # Comando inicial no reconocido
                response_text = '¡Hola! 👋 Para ver los eventos disponibles di "Hola". Para ver tu lista actual, di "Lista".'
                if is_vip: response_text += "\n(Tienes opciones VIP disponibles)"
                # Mantener estado inicial
                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}

        # --- Estado: Esperando Selección de Evento ---
        elif current_state == STATE_AWAITING_EVENT_SELECTION:
            if not available_events: # Seguridad: si no hay eventos en estado, no se puede elegir
                 logger.error(f"Usuario {sender_phone_normalized} en AWAITING_EVENT_SELECTION pero sin eventos disponibles en estado. Reiniciando.")
                 response_text = "Hubo un problema, no recuerdo los eventos. Por favor, di 'Hola' de nuevo."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
            else:
                 try:
                     choice_index = int(incoming_msg) - 1 # Convertir a índice 0-based
                     if 0 <= choice_index < len(available_events):
                         selected_event = available_events[choice_index]
                         logger.info(f"Usuario {sender_phone_normalized} seleccionó evento: {selected_event}")

                         # Guardar el evento seleccionado y actualizar estado
                         user_status['event'] = selected_event

                         if is_vip:
                             # Preguntar tipo de invitado (VIP o Normal)
                             response_text = f"Evento *{selected_event}* seleccionado. ✨\n¿Quieres añadir invitados *Generales* (normales) o *VIP*? Responde 'Normal' o 'VIP'."
                             user_status['state'] = STATE_AWAITING_GUEST_TYPE
                             # Mantener available_events por si cancela y vuelve a este punto? O limpiar? Limpiemos por ahora.
                             # user_status['available_events'] = [] # Opcional: limpiar para evitar confusión
                             user_states[sender_phone_normalized] = user_status # Actualizar estado en memoria global
                         else:
                             # Usuario no VIP, ir directo a pedir datos normales
                             response_text = (
                                 f"Perfecto, evento seleccionado: *{selected_event}*.\n\n"
                                 "Ahora envíame la lista (Nombres primero, luego Emails)\n\n"
                                 "Ejemplo:\n"
                                 "Hombres: \n"
                                 "Nombre Apellido\n"
                                 "Nombre Apellido\n\n"
                                 "email1@ejemplo.com\n"
                                 "email2@ejemplo.com\n\n"
                                 "Mujeres: \n"
                                 "Nombre Apellido\n"
                                 "Nombre Apellido\n\n"
                                 "email1@ejemplo.com\n"
                                 "email2@ejemplo.com\n\n"
                                 "⚠️ La cantidad de nombres y emails debe coincidir.\n"
                                 "Escribe 'cancelar' si quieres cambiar de evento."
                             )
                             user_status['state'] = STATE_AWAITING_GUEST_DATA
                             user_status['guest_type'] = 'Normal' # Guardar tipo por defecto
                             # user_status['available_events'] = [] # Opcional: limpiar
                             user_states[sender_phone_normalized] = user_status # Actualizar estado
                     else: # Número fuera de rango
                         event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                         response_text = f"❌ Número '{incoming_msg}' fuera de rango. Por favor, elige un número válido de la lista:\n\n{event_list_text}"
                         # Mantener estado AWAITING_EVENT_SELECTION
                 except ValueError: # No envió un número
                     event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                     response_text = f"Por favor, responde sólo con el *número* del evento que quieres gestionar:\n\n{event_list_text}"
                     # Mantener estado AWAITING_EVENT_SELECTION

        # --- ESTADO: ESPERANDO TIPO DE INVITADO (SOLO VIPs) ---
        elif current_state == STATE_AWAITING_GUEST_TYPE:
            # Este estado solo es alcanzable por VIPs que ya seleccionaron evento
            if not selected_event: # Seguridad
                 logger.error(f"Usuario VIP {sender_phone_normalized} en AWAITING_GUEST_TYPE sin evento seleccionado. Reiniciando.")
                 response_text = "Hubo un problema, no recuerdo el evento. Por favor, di 'Hola' de nuevo."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
            else:
                 choice_lower = incoming_msg.lower()
                 if choice_lower == 'vip':
                     logger.info(f"Usuario VIP {sender_phone_normalized} eligió añadir tipo VIP para evento {selected_event}.")
                     user_status['state'] = STATE_AWAITING_GUEST_DATA
                     user_status['guest_type'] = 'VIP'
                     response_text = (
                         f"Ok, vas a añadir invitados *VIP* para *{selected_event}*.\n\n"
                         "Envíame la lista en formato Nombres -> Emails:\n\n"
                         "Ejemplo:\n"
                         "Carlos VIP\n"
                         "Ana VIP\n\n" # Línea vacía separadora
                         "carlos.vip@mail.com\n"
                         "ana.vip@mail.com\n\n"
                         "⚠️ La cantidad de nombres y emails debe coincidir.\n"
                         "Escribe 'cancelar' para volver."
                     )
                     user_states[sender_phone_normalized] = user_status # Actualizar estado
                 elif choice_lower == 'normal' or choice_lower == 'normales' or choice_lower == 'general' or choice_lower == 'generales':
                     logger.info(f"Usuario VIP {sender_phone_normalized} eligió añadir tipo Normal para evento {selected_event}.")
                     user_status['state'] = STATE_AWAITING_GUEST_DATA
                     user_status['guest_type'] = 'Normal'
                     response_text = (
                         f"Ok, vas a añadir invitados *Generales* para *{selected_event}*.\n\n"
                         "Envíame la lista en formato Nombres -> Emails:\n\n"
                         "Ejemplo:\n"
                         "Juan Perez\n"
                         "Maria Garcia\n\n" # Línea vacía separadora
                         "juan.p@ejemplo.com\n"
                         "maria.g@ejemplo.com\n\n"
                         "⚠️ La cantidad de nombres y emails debe coincidir.\n"
                         "Escribe 'cancelar' para volver."
                     )
                     user_states[sender_phone_normalized] = user_status # Actualizar estado
                 else:
                     response_text = f"No entendí '{incoming_msg}'. Por favor, responde 'Normal' o 'VIP' para indicar qué tipo de invitados quieres añadir para *{selected_event}*."
                     # Mantener estado actual (AWAITING_GUEST_TYPE)

        # --- ESTADO: ESPERANDO DATOS DEL INVITADO ---
        elif current_state == STATE_AWAITING_GUEST_DATA:
            # Debe haber un evento y un tipo (Normal o VIP) seleccionados
            if not selected_event or not selected_guest_type:
                logger.error(f"Estado AWAITING_GUEST_DATA alcanzado sin evento ({selected_event}) o tipo ({selected_guest_type}) para {sender_phone_normalized}. Reiniciando.")
                response_text = "Hubo un problema interno, no sé qué evento o tipo procesar. Por favor, empieza de nuevo diciendo 'Hola'."
                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
            elif incoming_msg.lower() in ["cancelar", "salir", "cancel", "exit"]:
                logger.info(f"Usuario {sender_phone_normalized} canceló la adición de invitados para {selected_event}.")
                response_text = "Operación cancelada. Puedes decir 'Hola' para elegir otro evento o gestionar uno diferente."
                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
            else:
                # --- Lógica de Procesamiento de Datos ---

                if selected_guest_type == 'VIP':
                    logger.info(f"Procesando datos invitados VIP para '{selected_event}' de {sender_phone_normalized}")
                    if not vip_guest_sheet:
                        logger.error(f"Intento de añadir VIPs pero la hoja 'Invitados VIP' no está disponible/accesible.")
                        response_text = "❌ Error: No se pudo acceder a la hoja de invitados VIP. Contacta al administrador."
                        # Resetear estado para evitar bucles
                        user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
                    else:
                        # Parsear usando la función específica para N->E VIP
                        parsed_vip_list = parse_vip_guest_list(incoming_msg)

                        if parsed_vip_list is None: # Error de formato N->E o conteo
                            response_text = ("⚠️ Formato incorrecto para VIPs.\n"
                                             "Asegúrate de enviar Nombres primero, luego una línea vacía, y luego los Emails.\n"
                                             "Ejemplo:\n"
                                             "Carlos VIP\n\n"
                                             "carlos.vip@mail.com\n\n"
                                             "La cantidad de nombres y emails debe coincidir. Intenta de nuevo o escribe 'cancelar'.")
                            # Mantener estado AWAITING_GUEST_DATA para reintento
                        elif not parsed_vip_list: # El parser funcionó pero no encontró datos válidos
                             response_text = ("⚠️ No encontré nombres o emails válidos en tu mensaje.\n"
                                             "Revisa que no estén vacíos y que los emails parezcan correctos.\n"
                                             "Intenta de nuevo o escribe 'cancelar'.")
                             # Mantener estado AWAITING_GUEST_DATA para reintento
                        else: # Lista VIP parseada correctamente
                            vip_pr_name = sender_phone_normalized # Fallback
                            try:
                                vip_pr_map = sheet_conn.get_vip_phone_pr_mapping()
                                if vip_pr_map:
                                    pr_name_found = vip_pr_map.get(sender_phone_normalized)
                                    if pr_name_found: vip_pr_name = pr_name_found
                                    else: logger.warning(f"No se encontró PR Name VIP mapeado para {sender_phone_normalized}")
                            except Exception as vip_map_err:
                                logger.error(f"Error buscando nombre PR VIP: {vip_map_err}")

                            # Añadir a la hoja VIP
                            added_count = add_vip_guests_to_sheet(vip_guest_sheet, parsed_vip_list, vip_pr_name)

                            if added_count > 0:
                                response_text = f"✅ ¡Éxito! Se anotaron *{added_count}* invitado(s) VIP para el evento *{selected_event}*."
                                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
                            elif added_count == -1: # Éxito parcial, algunos inválidos
                                response_text = f"⚠️ Se anotaron algunos invitados VIP para *{selected_event}*, pero otros tenían datos inválidos y fueron omitidos."
                                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
                            else: # added_count == 0 (Error interno o no se añadieron filas válidas)
                                response_text = f"❌ Hubo un error al guardar los invitados VIP en la hoja. Por favor, intenta de nuevo más tarde o contacta al administrador."
                                # Mantener estado para posible reintento o resetear? Resetear por seguridad.
                                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}

                elif selected_guest_type == 'Normal':
                    logger.info(f"Procesando datos invitados Normales para '{selected_event}' de {sender_phone_normalized}")

                    # Obtener la hoja específica del evento Normal
                    event_sheet = sheet_conn.get_sheet_by_event_name(selected_event)
                    if not event_sheet:
                        logger.error(f"No se pudo obtener o crear la hoja para el evento normal '{selected_event}'")
                        response_text = f"❌ Error: No se pudo acceder a la hoja para el evento '{selected_event}'. Contacta al administrador."
                        user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
                    else:
                        # Inicializar error_info localmente ANTES de parsear
                        error_info_parsing = None
                        structured_guests = None

                        # Intentar parsear con el formato Nombres->Emails primero
                        logger.info("Intentando extractor para formato Normal (Nombres -> Emails)...")
                        # Asumimos que extract_guests_from_split_format usa las líneas originales
                        # Necesitamos pasarle incoming_msg.split('\n') o similar
                        data_lines_list = incoming_msg.split('\n')
                        structured_guests, error_info_parsing = extract_guests_from_split_format(data_lines_list)

                        # Si el formato N->E falló o no devolvió nada, podrías intentar otros métodos
                        # (Como AI o el parseo manual estándar que tenías antes)
                        # if not structured_guests and OPENAI_AVAILABLE and client:
                        #     logger.info("Formato N->E falló, intentando con OpenAI...")
                        #     # ... lógica OpenAI ...
                        # if not structured_guests:
                        #     logger.info("Formato N->E y AI fallaron, intentando extractor manual estándar...")
                        #     # ... lógica extractor manual estándar ...

                        # Procesar resultado del parseo
                        if not structured_guests:
                            logger.error(f"La extracción de invitados normales falló para {sender_phone_normalized}. Error info: {error_info_parsing}")
                            # Dar feedback basado en error_info_parsing si existe
                            if error_info_parsing and error_info_parsing.get('error_type') == 'desbalance':
                                response_text = (f"⚠️ Formato incorrecto. Detecté un desbalance:\n"
                                                 f"• {error_info_parsing.get('names_count', 'N/A')} nombres\n"
                                                 f"• {error_info_parsing.get('emails_count', 'N/A')} emails\n\n"
                                                 f"La cantidad debe ser la misma. Revisa tu lista, separa nombres y emails con una línea vacía, e intenta de nuevo o 'cancelar'.")
                            elif error_info_parsing and error_info_parsing.get('error_type') == 'no_valid_data':
                                 response_text = ("⚠️ No pude encontrar nombres y emails válidos en el formato esperado (Nombres -> Emails separados por línea vacía).\n"
                                                  "Revisa el ejemplo e intenta de nuevo o escribe 'cancelar'.")
                            else: # Error genérico de parseo
                                response_text = ("⚠️ No pude procesar tu lista. Asegúrate que sigue el formato:\n"
                                                 "Nombres (uno por línea)\n\n" # Línea vacía
                                                 "Emails (uno por línea)\n\n"
                                                 "Intenta de nuevo o escribe 'cancelar'.")
                            # Mantener estado para reintento
                        else:
                            # --- Añadir invitados normales a la hoja del evento ---
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            expected_headers = ['Nombre y Apellido', 'Email', 'Genero', 'Publica', 'Evento', 'Timestamp', "ENVIADO"] # Ajusta tus headers

                            try:
                                # Verificar/Crear encabezados (maneja hoja vacía)
                                try:
                                    headers = event_sheet.row_values(1)
                                except gspread.exceptions.APIError as api_err:
                                    # Si la hoja está totalmente vacía, row_values(1) da error
                                    if "exceeds grid limits" in str(api_err).lower() or "out of bounds" in str(api_err).lower():
                                        headers = []
                                    else:
                                        raise api_err # Otro error de API
                                except Exception as header_err:
                                     logger.error(f"Error inesperado leyendo headers de '{event_sheet.title}': {header_err}")
                                     raise header_err # Relanzar para captura general

                                if not headers or len(headers) < len(expected_headers) or headers[:len(expected_headers)] != expected_headers:
                                    logger.info(f"Actualizando/Creando encabezados en la hoja '{event_sheet.title}'")
                                    # Usar update para asegurar que empieza en A1
                                    # El rango debe cubrir todos los headers esperados
                                    header_range = f"A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers))}"
                                    event_sheet.update(header_range, [expected_headers], value_input_option='USER_ENTERED')

                                # Validar invitados y crear filas
                                valid_guests_for_sheet = []
                                invalid_entries_found = False
                                for guest in structured_guests:
                                    # Asumiendo que structured_guests tiene dicts con 'nombre', 'email', 'genero'
                                    if isinstance(guest, dict) and guest.get("email") and guest.get("nombre"):
                                        if re.match(r"[^@]+@[^@]+\.[^@]+", guest["email"]): # Email válido
                                            valid_guests_for_sheet.append(guest)
                                        else:
                                            logger.warning(f"Invitado normal omitido (email inválido): {guest.get('email')} para {guest.get('nombre')}")
                                            invalid_entries_found = True
                                    else:
                                        logger.warning(f"Invitado normal omitido (incompleto): {guest}")
                                        invalid_entries_found = True

                                # Si no quedó ningún invitado válido
                                if not valid_guests_for_sheet:
                                    response_text = "⚠️ No encontré invitados con nombre y email válidos en tu lista. Revisa el formato e intenta de nuevo."
                                    # Mantener estado para reintento
                                else:
                                    # Obtener nombre del PR (Normal)
                                    pr_name = sender_phone_normalized # Fallback
                                    try:
                                        phone_to_pr_map = sheet_conn.get_phone_pr_mapping()
                                        if phone_to_pr_map:
                                            pr_name_found = phone_to_pr_map.get(sender_phone_normalized)
                                            if pr_name_found: pr_name = pr_name_found
                                    except Exception as e:
                                        logger.error(f"Error al buscar PR Normal: {e}")

                                    # Crear filas para Google Sheets
                                    rows_to_add = []
                                    for guest in valid_guests_for_sheet:
                                        # Asumiendo que tu parser asigna 'genero' (Hombre/Mujer/Otro)
                                        full_name = f"{guest.get('nombre', '')} {guest.get('apellido', '')}".strip()
                                        rows_to_add.append([
                                            full_name,
                                            guest.get("email", ""),
                                            guest.get("genero", "Otro"), # Default si el parser no lo da
                                            pr_name,
                                            selected_event,
                                            timestamp,
                                            '' # Columna ENVIADO (vacía inicialmente)
                                        ])

                                    # Añadir a la hoja específica del evento
                                    if rows_to_add:
                                        # ---> ¡NUEVO LOG ANTES DE ENVIAR! <---
                                        logger.info(f"DEBUG APPENDING DATA: Intentando añadir estas filas: {rows_to_add}")
                                        # ------------------------------------
                                        try: # Añadido try/except alrededor de append_rows
                                            result = event_sheet.append_rows(rows_to_add, value_input_option='USER_ENTERED')
                                            added_count = result.get('updates', {}).get('updatedRows', 0)
                                            logger.info(f"Agregados {added_count} invitados normales a '{selected_event}'")

                                            if added_count == len(rows_to_add) and not invalid_entries_found:
                                                response_text = f"✅ ¡Éxito! Se anotaron *{added_count}* invitado(s) Generales para *{selected_event}*."
                                            elif added_count > 0:
                                                response_text = f"⚠️ Se anotaron *{added_count}* invitado(s) Generales para *{selected_event}*, pero algunos de tu lista tenían datos inválidos y fueron omitidos."
                                            else:
                                                logger.error(f"Error añadiendo filas normales a '{event_sheet.title}', API reportó 0 añadidas.")
                                                response_text = f"❌ Hubo un error al guardar los invitados en la hoja '{selected_event}'. Intenta de nuevo."

                                            # Resetear estado SIEMPRE que se haya añadido algo o habido éxito parcial
                                            if added_count > 0:
                                                user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}

                                        except Exception as append_err: # Capturar error específico de append_rows
                                            logger.error(f"Error DIRECTO en event_sheet.append_rows: {append_err}")
                                            logger.error(traceback.format_exc())
                                            response_text = f"❌ Hubo un error crítico al intentar guardar en la hoja '{selected_event}'. Contacta al administrador."
                                            # No resetear estado para posible diagnóstico
                                    else:
                                        logger.error("Error lógico: Había invitados válidos pero no se generaron filas para añadir.")
                                        response_text = "❌ Hubo un error interno al preparar los datos. Intenta de nuevo."

                            except gspread.exceptions.APIError as sheet_api_err:
                                 logger.error(f"Error de API de Google Sheets al operar en '{event_sheet.title}': {sheet_api_err}")
                                 response_text = f"❌ Hubo un error de comunicación con Google Sheets ({sheet_api_err.response.status_code}). Intenta de nuevo más tarde."
                                 # No resetear estado necesariamente, puede ser temporal
                            except Exception as e:
                                logger.error(f"Error inesperado al procesar/añadir invitados normales a '{event_sheet.title}': {e}")
                                logger.error(traceback.format_exc())
                                response_text = "❌ Hubo un error interno procesando tu lista. Intenta de nuevo."
                                # No resetear estado necesariamente

                else: # Tipo de invitado desconocido en estado (no debería pasar)
                    logger.error(f"Estado AWAITING_GUEST_DATA con guest_type inválido o nulo: {selected_guest_type} para {sender_phone_normalized}")
                    response_text = "Hubo un error con tu selección de tipo de invitado. Por favor, empieza de nuevo ('Hola')."
                    user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear

        # --- Estado Desconocido ---
        else:
            logger.warning(f"Estado no reconocido '{current_state}' para {sender_phone_normalized}. Reiniciando a estado inicial.")
            response_text = "No estoy seguro de qué estábamos hablando. 🤔 Por favor, di 'Hola' para comenzar de nuevo."
            user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}

        # ====================================
        # --- Fin Lógica Principal de Estados ---
        # ====================================

        # Enviar la respuesta calculada (si hay una)
        if response_text:
            if not send_twilio_message(sender_phone_raw, response_text):
                logger.error(f"Fallo al enviar mensaje de respuesta final a {sender_phone_raw}")
                # OK para Twilio, pero loggeamos el error de envío
                return jsonify({"status": "processed_with_send_error"}), 200
            else:
                logger.info(f"Respuesta final enviada a {sender_phone_raw}: {response_text[:100]}...")
                return jsonify({"status": "success"}), 200
        else:
            # Si llegamos aquí sin response_text, algo falló en la lógica de estados
            # o una acción no generó respuesta (ej. parseo fallido sin mensaje de error)
            logger.warning(f"No se generó texto de respuesta para enviar al final del flujo (Estado: {current_state}).")
            # Enviar un mensaje genérico de fallback? O solo loggear?
            # Podría ser útil enviar algo para que el usuario no quede esperando.
            fallback_message = "No estoy seguro de cómo responder a eso. Puedes decir 'Hola' para empezar."
            send_twilio_message(sender_phone_raw, fallback_message)
            return jsonify({"status": "processed_no_reply_generated"}), 200

    except Exception as e:
        # Captura errores generales e inesperados en el flujo principal
        logger.error(f"!!! Error INESPERADO Y GRAVE en el webhook para {sender_phone_raw or '???'}: {e} !!!")
        logger.error(traceback.format_exc())
        # Intentar notificar al usuario si es posible
        if sender_phone_raw:
            error_message = "Lo siento, ocurrió un error inesperado en el sistema. Por favor, intenta de nuevo más tarde."
            send_twilio_message(sender_phone_raw, error_message) # Intentar enviar, puede fallar también
        # Devolver error 500 al webhook (Twilio reintentará)
        return jsonify({"status": "error", "message": "Internal server error"}), 500