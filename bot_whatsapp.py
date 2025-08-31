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
from qr_automation import PlanOutAutomation

# Configuración de logging optimizada para Google Cloud Run
import sys

# Detectar si estamos en Google Cloud Run
is_cloud_run = os.environ.get('K_SERVICE') is not None

if is_cloud_run:
    # Configuración para Google Cloud Run - solo console output
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)  # Solo salida a stdout para Cloud Logging
        ]
    )
    print("🌐 Logging configurado para Google Cloud Run")
else:
    # Configuración para desarrollo local - archivo + consola
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("whatsapp_bot.log"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    print("💻 Logging configurado para desarrollo local")

# Función para verificar secretos al inicio
def verify_secrets_and_environment():
    """Verifica que todos los secretos y variables de entorno estén disponibles"""
    logger = logging.getLogger(__name__)
    
    logger.info("🔍 VERIFICANDO SECRETOS Y VARIABLES DE ENTORNO...")
    
    # Lista de secretos requeridos
    required_secrets = {
        'TWILIO_ACCOUNT_SID': 'Twilio Account SID',
        'TWILIO_AUTH_TOKEN': 'Twilio Auth Token', 
        'TWILIO_WHATSAPP_NUMBER': 'Twilio WhatsApp Number',
        'GOOGLE_APPLICATION_CREDENTIALS': 'Google Application Credentials Path',
        'BROADCAST_API_TOKEN': 'Broadcast API Token',
        'OPENAI_API_KEY': 'OpenAI API Key',
        'PLANOUT_USERNAME': 'PlanOut Username',
        'PLANOUT_PASSWORD': 'PlanOut Password',
        'PLANOUT_HEADLESS': 'PlanOut Headless Mode'
    }
     
    # Variables de entorno de Playwright
    playwright_vars = {
        'PLAYWRIGHT_BROWSERS_PATH': 'Playwright Browsers Path',
        'DISPLAY': 'Display for X11'
    }
    
    missing_secrets = []
    found_secrets = []
    
    # Verificar secretos requeridos
    for env_var, description in required_secrets.items():
        value = os.environ.get(env_var)
        if value:
            # Ocultar valor sensible, mostrar solo primeros y últimos caracteres
            if len(value) > 10:
                masked_value = f"{value[:3]}...{value[-3:]}"
            else:
                masked_value = f"{value[:2]}...{value[-1]}"
            logger.info(f"✅ {description}: {masked_value}")
            found_secrets.append(env_var)
        else:
            logger.error(f"❌ FALTA: {description} ({env_var})")
            missing_secrets.append(env_var)
    
    # Verificar variables de Playwright
    for env_var, description in playwright_vars.items():
        value = os.environ.get(env_var)
        if value:
            logger.info(f"✅ {description}: {value}")
            found_secrets.append(env_var)
        else:
            logger.warning(f"⚠️ FALTA: {description} ({env_var})")
            missing_secrets.append(env_var)
    
    # Verificar archivo de credenciales de Google
    # Check for Google credentials (multiple possible sources)
    google_creds_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    google_creds_env = os.environ.get('GOOGLE_CREDENTIALS_FILE')
    
    if google_creds_path and os.path.exists(google_creds_path):
        logger.info(f"✅ Google Credentials File: Encontrado en {google_creds_path}")
        found_secrets.append('GOOGLE_APPLICATION_CREDENTIALS')
    elif google_creds_env:
        logger.info("✅ Google Credentials: Encontrado en variable de entorno GOOGLE_CREDENTIALS_FILE")
        found_secrets.append('GOOGLE_CREDENTIALS_FILE')
    else:
        logger.error("❌ Google Credentials: NO encontrado (ni archivo ni variable de entorno)")
        missing_secrets.append('GOOGLE_CREDENTIALS')
    
    # Resumen
    logger.info(f"📊 RESUMEN DE VERIFICACIÓN:")
    logger.info(f"✅ Secretos encontrados: {len(found_secrets)}")
    logger.info(f"❌ Secretos faltantes: {len(missing_secrets)}")
    
    if missing_secrets:
        logger.warning(f"⚠️ Secretos faltantes: {', '.join(missing_secrets)}")
        logger.warning("🔧 El bot puede no funcionar correctamente sin estos secretos")
    else:
        logger.info("🎉 TODOS LOS SECRETOS ESTÁN CONFIGURADOS CORRECTAMENTE!")
    
    # Verificar entorno específico
    if is_cloud_run:
        logger.info("🌐 Ejecutándose en Google Cloud Run")
        service_name = os.environ.get('K_SERVICE', 'unknown')
        revision = os.environ.get('K_REVISION', 'unknown')
        logger.info(f"📋 Service: {service_name}, Revision: {revision}")
    else:
        logger.info("💻 Ejecutándose en entorno local/desarrollo")
    
    return len(missing_secrets) == 0
logger = logging.getLogger(__name__)

app = Flask(__name__)

user_states = {}

# Verificar secretos al inicio del bot
verify_secrets_and_environment()

# --- Constantes para los estados ---
STATE_INITIAL = None
STATE_AWAITING_EVENT_SELECTION = 'AWAITING_EVENT_SELECTION'
STATE_AWAITING_GUEST_TYPE = 'AWAITING_GUEST_TYPE'
STATE_AWAITING_GUEST_DATA = 'AWAITING_GUEST_DATA'
STATE_QR_AUTOMATION = 'QR_AUTOMATION'

# Configuración de Twilio
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER')

def split_long_message(message, max_length=1500):
    """
    Divide un mensaje largo en partes más pequeñas respetando el límite de caracteres.
    """
    if len(message) <= max_length:
        return [message]
    
    parts = []
    current_part = ""
    lines = message.split('\n')
    
    for line in lines:
        # Si una sola línea es muy larga, la dividimos por palabras
        if len(line) > max_length:
            words = line.split(' ')
            current_line = ""
            for word in words:
                if len(current_line + word + " ") <= max_length:
                    current_line += word + " "
                else:
                    if current_part + current_line.strip():
                        parts.append(current_part + current_line.strip())
                    current_part = ""
                    current_line = word + " "
            line = current_line.strip()
        
        # Verificar si podemos agregar la línea al parte actual
        if len(current_part + line + "\n") <= max_length:
            current_part += line + "\n"
        else:
            # Si el parte actual no está vacío, lo agregamos a la lista
            if current_part.strip():
                parts.append(current_part.strip())
            current_part = line + "\n"
    
    # Agregar la última parte si no está vacía
    if current_part.strip():
        parts.append(current_part.strip())
    
    return parts

def send_twilio_message(phone_number, message):
    """ Envía un mensaje de WhatsApp usando Twilio, dividiendo mensajes largos """
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
        
        # Dividir mensaje si es muy largo
        message_parts = split_long_message(message)
        
        success = True
        for i, part in enumerate(message_parts):
            # Si hay múltiples partes, agregar numeración
            if len(message_parts) > 1:
                part_header = f"({i+1}/{len(message_parts)})\n"
                part = part_header + part
            
            twilio_message = client.messages.create(
                from_=origin_number,
                body=part,
                to=destination_number
            )
            logger.info(f"Mensaje parte {i+1}/{len(message_parts)} enviado a {destination_number}: {twilio_message.sid}")
            
            # Pequeña pausa entre mensajes para evitar spam
            if i < len(message_parts) - 1:
                time.sleep(0.5)
        
        return success
    except Exception as e:
        logger.error(f"Error al enviar mensaje de Twilio a {destination_number}: {e}")
        return False

def clear_background_color_for_new_rows(sheet, num_new_rows):
    """
    Elimina solo los colores de fondo de las filas recién agregadas,
    manteniendo otros formatos como negritas, cursivas, bordes, etc.
    """
    try:
        # Obtener el número total de filas actual
        all_values = sheet.get_all_values()
        total_rows = len(all_values)
        
        # Calcular el rango de las filas nuevas
        start_row = total_rows - num_new_rows + 1
        end_row = total_rows
        
        # Crear la solicitud para limpiar solo el color de fondo
        requests = [{
            'repeatCell': {
                'range': {
                    'sheetId': sheet.id,
                    'startRowIndex': start_row - 1,  # -1 porque es 0-indexed
                    'endRowIndex': end_row,
                    'startColumnIndex': 0,
                    'endColumnIndex': sheet.col_count
                },
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': {
                            'red': 1.0,
                            'green': 1.0, 
                            'blue': 1.0,
                            'alpha': 1.0
                        }
                    }
                },
                'fields': 'userEnteredFormat.backgroundColor'
            }
        }]
        
        # Ejecutar la solicitud
        sheet.spreadsheet.batch_update({'requests': requests})
        logger.info(f"Color de fondo eliminado de {num_new_rows} filas nuevas en hoja {sheet.title}")
        
    except Exception as e:
        logger.error(f"Error al limpiar color de fondo de filas nuevas: {e}")

def send_templated_message(phone_number, content_sid, content_variables=None):
    """ Envía un mensaje de WhatsApp usando una plantilla de Twilio """
    # Asegurarse que el número tenga el prefijo 'whatsapp:+'
    # Los números de la hoja vienen normalizados (solo dígitos), ej: 54911...
    if phone_number.startswith('whatsapp:'):
        destination_number = phone_number
    else:
        destination_number = f"whatsapp:+{phone_number}"

    # Asegurarse que el número de origen tenga el prefijo 'whatsapp:'
    if not TWILIO_WHATSAPP_NUMBER:
         logger.error("Número de WhatsApp de Twilio (TWILIO_WHATSAPP_NUMBER) no configurado.")
         return {"success": False, "error": "Twilio WhatsApp number not configured"}
    if not TWILIO_WHATSAPP_NUMBER.startswith('whatsapp:'):
        origin_number = f"whatsapp:{TWILIO_WHATSAPP_NUMBER}"
    else:
        origin_number = TWILIO_WHATSAPP_NUMBER

    try:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            logger.error("Credenciales de Twilio (SID o Token) no configuradas.")
            return {"success": False, "error": "Twilio credentials not configured"}

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        message_data = {
            'from_': origin_number,
            'to': destination_number,
            'content_sid': content_sid
        }

        if content_variables:
            # Twilio espera las variables como un string JSON
            message_data['content_variables'] = json.dumps(content_variables)

        twilio_message = client.messages.create(**message_data)
        logger.info(f"Mensaje de plantilla {content_sid} enviado a {destination_number}: {twilio_message.sid}")
        return {"success": True, "sid": twilio_message.sid}
    except Exception as e:
        logger.error(f"Error al enviar mensaje de plantilla Twilio a {destination_number}: {e}")
        # Intentar obtener más detalles del error de Twilio si es posible
        error_details = str(e)
        if hasattr(e, 'msg'):
            error_details = e.msg
        return {"success": False, "error": error_details}

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


def get_or_create_unified_event_sheet(sheet_conn, event_name):
    """
    Obtiene o crea una hoja unificada para un evento que contiene tanto invitados generales como VIP.
    
    Args:
        sheet_conn: Instancia de SheetsConnection.
        event_name (str): Nombre del evento.
        
    Returns:
        worksheet: Objeto de hoja de Google Sheets o None si hay error.
    """
    try:
        unified_sheet_name = event_name  # Usar directamente el nombre del evento
        logger.info(f"Intentando obtener/crear hoja unificada: '{unified_sheet_name}'")
        
        # Intentar obtener la hoja existente
        try:
            unified_event_sheet = sheet_conn.spreadsheet.worksheet(unified_sheet_name)
            logger.info(f"Hoja unificada '{unified_sheet_name}' ya existe.")
            
            # Verificar si la hoja existente tiene las columnas correctas
            expected_headers = ['Nombre', 'Email', 'Instagram', 'TIPO', 'PR', 'EMAIL PR', 'Timestamp', 'Enviado']
            try:
                headers = unified_event_sheet.row_values(1)
                if len(headers) < len(expected_headers) or headers[:len(expected_headers)] != expected_headers:
                    logger.info(f"Actualizando hoja existente '{unified_sheet_name}' para incluir columna Enviado...")
                    # Expandir la hoja si es necesario
                    current_cols = unified_event_sheet.col_count
                    if current_cols < len(expected_headers):
                        unified_event_sheet.add_cols(len(expected_headers) - current_cols)
                    # Actualizar encabezados
                    unified_event_sheet.update(f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers))}', [expected_headers])
                    logger.info(f"Hoja '{unified_sheet_name}' actualizada con nuevos encabezados.")
            except Exception as header_err:
                logger.warning(f"Error al verificar/actualizar encabezados en hoja existente: {header_err}")
            
            return unified_event_sheet
        except gspread.exceptions.WorksheetNotFound:
            logger.info(f"Hoja unificada '{unified_sheet_name}' no existe, creándola...")
            
            # Crear nueva hoja unificada para este evento
            # Incluye columnas para ambos tipos: General (sin Instagram) y VIP (con Instagram)
            expected_headers = ['Nombre', 'Email', 'Instagram', 'TIPO', 'PR', 'EMAIL PR', 'Timestamp', 'Enviado']
            unified_event_sheet = sheet_conn.spreadsheet.add_worksheet(
                title=unified_sheet_name, 
                rows=1, 
                cols=len(expected_headers)
            )
            
            # Añadir encabezados
            unified_event_sheet.update(
                f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers))}', 
                [expected_headers]
            )
            logger.info(f"Hoja unificada '{unified_sheet_name}' creada con encabezados: {expected_headers}")
            return unified_event_sheet
            
    except Exception as e:
        logger.error(f"Error al obtener/crear hoja unificada para evento '{event_name}': {e}")
        return None

def get_or_create_vip_event_sheet(sheet_conn, event_name):
    """
    Obtiene o crea una hoja VIP específica para un evento.
    
    Args:
        sheet_conn: Instancia de SheetsConnection.
        event_name (str): Nombre del evento.
        
    Returns:
        worksheet: Objeto de hoja de Google Sheets o None si hay error.
    """
    try:
        vip_sheet_name = f"VIP {event_name}"
        logger.info(f"Intentando obtener/crear hoja VIP: '{vip_sheet_name}'")
        
        # Intentar obtener la hoja existente
        try:
            vip_event_sheet = sheet_conn.spreadsheet.worksheet(vip_sheet_name)
            logger.info(f"Hoja VIP '{vip_sheet_name}' ya existe.")
            
            # Verificar si la hoja existente tiene las columnas correctas
            expected_headers = ['Nombre', 'Email', 'Instagram', 'Ingreso', 'PR', 'Enviado']
            try:
                headers = vip_event_sheet.row_values(1)
                if len(headers) < len(expected_headers) or headers[:len(expected_headers)] != expected_headers:
                    logger.info(f"Actualizando hoja VIP existente '{vip_sheet_name}' para incluir columna Enviado...")
                    # Expandir la hoja si es necesario
                    current_cols = vip_event_sheet.col_count
                    if current_cols < len(expected_headers):
                        vip_event_sheet.add_cols(len(expected_headers) - current_cols)
                    # Actualizar encabezados
                    vip_event_sheet.update(f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers))}', [expected_headers])
                    logger.info(f"Hoja VIP '{vip_sheet_name}' actualizada con nuevos encabezados.")
            except Exception as header_err:
                logger.warning(f"Error al verificar/actualizar encabezados en hoja VIP existente: {header_err}")
            
            return vip_event_sheet
        except gspread.exceptions.WorksheetNotFound:
            logger.info(f"Hoja VIP '{vip_sheet_name}' no existe, creándola...")
            
            # Crear nueva hoja VIP para este evento
            expected_headers = ['Nombre', 'Email', 'Instagram', 'Ingreso', 'PR', 'Enviado']
            vip_event_sheet = sheet_conn.spreadsheet.add_worksheet(
                title=vip_sheet_name, 
                rows=1, 
                cols=len(expected_headers)
            )
            
            # Añadir encabezados
            vip_event_sheet.update(
                f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers))}', 
                [expected_headers]
            )
            logger.info(f"Hoja VIP '{vip_sheet_name}' creada con encabezados: {expected_headers}")
            return vip_event_sheet
            
    except Exception as e:
        logger.error(f"Error al obtener/crear hoja VIP para evento '{event_name}': {e}")
        return None

def add_guests_to_unified_sheet(sheet, guests_list, pr_name, guest_type, sheet_conn):
    """
    Agrega invitados (General o VIP) a la hoja unificada del evento.
    
    Args:
        sheet: Objeto de hoja de Google Sheets unificada.
        guests_list (list): Lista de diccionarios con info de invitados.
        pr_name (str): Nombre del PR que los está añadiendo.
        guest_type (str): 'VIP' o 'Normal' para determinar el tipo.
        sheet_conn: Instancia de SheetsConnection para obtener email del PR.
        
    Returns:
        int: Número de invitados añadidos. 0 si hay error o no se añadió nada.
             -1 si hubo datos pero se filtraron todos por inválidos.
    """
    if not sheet:
        logger.error("Intento de añadir invitados pero la hoja unificada no es válida.")
        return 0
    if not guests_list:
        logger.warning("Se llamó a add_guests_to_unified_sheet con lista vacía o inválida.")
        return 0

    rows_to_add = []
    added_count = 0
    original_count = len(guests_list)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        logger.info(f"DEBUG Add Unified: Recibido tipo={type(guests_list)}, contenido={guests_list}, guest_type={guest_type}")
        
        # --- Verificar/Crear encabezados (Nombre | Email | Instagram | TIPO | PR | EMAIL PR | Timestamp | Enviado) ---
        expected_headers = ['Nombre', 'Email', 'Instagram', 'TIPO', 'PR', 'EMAIL PR', 'Timestamp', 'Enviado']
        try:
            headers = sheet.row_values(1)
        except gspread.exceptions.APIError as api_err:
             if "exceeds grid limits" in str(api_err): 
                 headers = []
             else: 
                 raise api_err

        # Actualizar encabezados si es necesario
        if headers != expected_headers:
            logger.info(f"Actualizando/Creando encabezados en hoja unificada: {expected_headers}")
            # Expandir la hoja para tener suficientes columnas si es necesario
            current_cols = sheet.col_count
            if current_cols < len(expected_headers):
                sheet.add_cols(len(expected_headers) - current_cols)
            sheet.update(f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers))}', [expected_headers])

        # --- Obtener email del PR desde la hoja Telefonos ---
        pr_email = ""  # Fallback vacío
        try:
            # Obtener el mapeo de teléfono del PR desde el contexto (necesitamos encontrar el teléfono del PR)
            # Por ahora, intentamos obtener el email basado en el mapeo reverso
            pr_email_mapping = sheet_conn.get_phone_pr_email_mapping()
            
            # Buscar el email correspondiente al pr_name
            for phone, email in pr_email_mapping.items():
                # Obtener el mapeo de teléfono a nombre PR para hacer la correlación
                pr_name_mapping = sheet_conn.get_phone_pr_mapping()
                if phone in pr_name_mapping and pr_name_mapping[phone] == pr_name:
                    pr_email = email
                    logger.info(f"Email PR encontrado para '{pr_name}': {pr_email}")
                    break
            
            if not pr_email:
                logger.warning(f"No se encontró email para PR '{pr_name}'. Usando vacío.")
                
        except Exception as e:
            logger.error(f"Error al obtener email del PR '{pr_name}': {e}")
            pr_email = ""  # Fallback vacío

        # --- Crear las filas ---
        for guest_data in guests_list:
            logger.info(f"DEBUG Add Unified Loop: Iterando, tipo={type(guest_data)}, item={guest_data}")
            
            # Combinar nombre y apellido si están separados
            nombre = guest_data.get('nombre', '').strip()
            apellido = guest_data.get('apellido', '').strip()
            name = f"{nombre} {apellido}".strip() if apellido else nombre
            email = guest_data.get('email', '').strip()
            instagram = guest_data.get('instagram', '').strip() if guest_type == 'VIP' else ""  # Solo VIP tiene Instagram
            parsed_gender = guest_data.get('genero') # Será 'Masculino', 'Femenino' o None

            # Validar datos requeridos según el tipo
            valid_data = False
            if guest_type == 'VIP':
                valid_data = name and email and instagram  # VIP requiere Instagram
            else:
                valid_data = name and email  # Normal solo requiere nombre y email

            if valid_data:
                # --- Determinar/Inferir Género ---
                final_gender = "Desconocido"
                if parsed_gender:
                    # Convertir género a formato esperado para TIPO
                    if parsed_gender.lower() in ['masculino', 'hombre']:
                        gender_for_tipo = "HOMBRE"
                    elif parsed_gender.lower() in ['femenino', 'mujer']:
                        gender_for_tipo = "MUJER"
                    else:
                        gender_for_tipo = "DESCONOCIDO"
                else:
                    # Intentar inferir si no vino del encabezado
                    first_name = name.split()[0] if name else ""
                    if first_name:
                         # Llamar a la función de IA
                         inferred = infer_gender_llm(first_name)
                         if inferred.lower() in ['hombre', 'masculino']:
                             gender_for_tipo = "HOMBRE"
                         elif inferred.lower() in ['mujer', 'femenino']:
                             gender_for_tipo = "MUJER"
                         else:
                             gender_for_tipo = "DESCONOCIDO"
                    else:
                         gender_for_tipo = "DESCONOCIDO"

                # Crear el valor TIPO: "GENERAL HOMBRE", "VIP MUJER", etc.
                if guest_type.upper() == 'NORMAL':
                    tipo_value = f"GENERAL {gender_for_tipo}"
                else:  # VIP
                    tipo_value = f"VIP {gender_for_tipo}"

                # Añadir fila con Nombre, Email, Instagram, TIPO, PR, EMAIL PR, Timestamp, Enviado
                row_data = [name, email, instagram, tipo_value, pr_name, pr_email, timestamp, False]
                rows_to_add.append(row_data)
                added_count += 1
            else:
                logger.warning(f"Se omitió invitado (nombre='{name}', email='{email}', instagram='{instagram}', tipo='{guest_type}') por datos faltantes. PR: {pr_name}.")

        # --- Agregar a la hoja ---
        if rows_to_add:
            sheet.append_rows(rows_to_add, value_input_option='USER_ENTERED')
            # Limpiar colores de fondo de las filas recién agregadas
            clear_background_color_for_new_rows(sheet, len(rows_to_add))
            logger.info(f"Agregados {added_count} invitados {guest_type} a hoja unificada por PR '{pr_name}'.")
            return added_count if added_count == original_count else -1
        else:
            logger.warning(f"No se generaron filas válidas para añadir por {pr_name}.")
            return -1 if original_count > 0 else 0

    except gspread.exceptions.APIError as e:
        logger.error(f"Error API Google Sheets al agregar filas a hoja unificada: {e}")
        return 0
    except Exception as e:
        logger.error(f"Error inesperado en add_guests_to_unified_sheet: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 0

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
        # --- Verificar/Crear encabezados (Nombre | Email | Instagram | Ingreso | PR | Enviado) ---
        expected_headers = ['Nombre', 'Email', 'Instagram', 'Ingreso', 'PR', 'Enviado'] # <-- NUEVOS HEADERS
        try:
            headers = sheet.row_values(1)
        except gspread.exceptions.APIError as api_err:
             if "exceeds grid limits" in str(api_err): headers = []
             else: raise api_err
        if not headers or headers[:len(expected_headers)] != expected_headers:
             logger.info(f"Actualizando/Creando encabezados en 'Invitados VIP': {expected_headers}")
             # Expandir la hoja para tener suficientes columnas si es necesario
             current_cols = sheet.col_count
             if current_cols < len(expected_headers):
                 sheet.add_cols(len(expected_headers) - current_cols)
             sheet.update(f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers))}', [expected_headers])

        # --- Crear las filas ---
        for guest_data in vip_guests_list:
            logger.info(f"DEBUG Add VIP Loop: Iterando, tipo={type(guest_data)}, item={guest_data}") # DEBUG
            # Combinar nombre y apellido si están separados
            nombre = guest_data.get('nombre', '').strip()
            apellido = guest_data.get('apellido', '').strip()
            name = f"{nombre} {apellido}".strip() if apellido else nombre
            email = guest_data.get('email', '').strip()
            instagram = guest_data.get('instagram', '').strip()
            parsed_gender = guest_data.get('genero') # Será 'Masculino', 'Femenino' o None

            if name and email and instagram: # Validar nombre, email e Instagram
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

                # Añadir fila con Nombre, Email, Instagram, Género (Ingreso), PR, Enviado
                rows_to_add.append([name, email, instagram, final_gender, pr_name, False]) # <-- NUEVO FORMATO FILA
                added_count += 1
            else:
                logger.warning(f"Se omitió invitado VIP (nombre='{name}', email='{email}', instagram='{instagram}') por datos faltantes. PR: {pr_name}.")

        # --- Agregar a la hoja ---
        if rows_to_add:
            sheet.append_rows(rows_to_add, value_input_option='USER_ENTERED')
            # Limpiar colores de fondo de las filas recién agregadas
            clear_background_color_for_new_rows(sheet, len(rows_to_add))
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
            r"(?i)lista\s+de\s+invitados",
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
            
            # Try to get credentials from mounted file first (Cloud Run with secret mounts)
            creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if creds_path and os.path.exists(creds_path):
                logger.info(f"Using Google credentials from mounted file: {creds_path}")
                creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
            else:
                # Fallback to JSON string in environment variable (other deployments)
                creds_json = os.environ.get("GOOGLE_CREDENTIALS_FILE")
                if creds_json:
                    logger.info("Using Google credentials from environment variable")
                    import json
                    creds_dict = json.loads(creds_json)
                    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                else:
                    # Final fallback to old path method
                    creds_path_fallback = os.environ.get("GOOGLE_CREDENTIALS_PATH", "/etc/secrets/google-credentials.json")
                    logger.info(f"Using Google credentials from fallback path: {creds_path_fallback}")
                    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path_fallback, scope)
            
            self.client = gspread.authorize(creds)
            self.spreadsheet = self.client.open("n8n sheet") # Nombre del Archivo Google Sheet

            # --- Obtener hojas principales (manejar si no existen) ---
            try:
                self.guest_sheet = self.spreadsheet.worksheet("Invitados")
            except gspread.exceptions.WorksheetNotFound:
                 logger.error("Hoja 'Invitados' no encontrada. Intentando crearla.")
                 # Ajusta las columnas/headers según necesites
                 try:
                    self.guest_sheet = self.spreadsheet.add_worksheet(title="Invitados", rows="1", cols="8")
                    self.guest_sheet.update('A1:H1', [['Nombre', 'Apellido', 'Email', 'Genero', 'Publica', 'Evento', 'Timestamp', 'Enviado']])
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
            
            # --- NUEVO: Hoja QR Especiales ---
            try:
                self.qr_special_sheet_obj = self.spreadsheet.worksheet("QR_Especiales")
                logger.info("Hoja 'QR_Especiales' encontrada.")
            except gspread.exceptions.WorksheetNotFound:
                logger.warning("Hoja 'QR_Especiales' no encontrada. Intentando crearla...")
                try:
                    # Crear con encabezados "Telefono" para números especiales de QR
                    expected_headers_qr = ['Telefono']
                    self.qr_special_sheet_obj = self.spreadsheet.add_worksheet(title="QR_Especiales", rows="1", cols=len(expected_headers_qr))
                    self.qr_special_sheet_obj.update(f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers_qr))}', [expected_headers_qr])
                    logger.info("Hoja 'QR_Especiales' creada con encabezados.")
                except Exception as create_err:
                    logger.error(f"No se pudo crear la hoja 'QR_Especiales': {create_err}")
                    self.qr_special_sheet_obj = None # Marcar como no disponible
            # --- FIN NUEVO QR ESPECIALES ---
            
            # --- NUEVO: Hoja Estado Eventos (para controlar envío automático QR) ---
            try:
                self.event_state_sheet_obj = self.spreadsheet.worksheet("Estado_Eventos")
                logger.info("Hoja 'Estado_Eventos' encontrada.")
            except gspread.exceptions.WorksheetNotFound:
                logger.warning("Hoja 'Estado_Eventos' no encontrada. Intentando crearla...")
                try:
                    # Crear con encabezados para rastrear estado de envío de QRs por evento
                    expected_headers_state = ['Evento', 'QR_Automatico_Enviado', 'Fecha_Envio', 'Hora_Envio']
                    self.event_state_sheet_obj = self.spreadsheet.add_worksheet(title="Estado_Eventos", rows="1", cols=len(expected_headers_state))
                    self.event_state_sheet_obj.update(f'A1:{gspread.utils.rowcol_to_a1(1, len(expected_headers_state))}', [expected_headers_state])
                    logger.info("Hoja 'Estado_Eventos' creada con encabezados.")
                except Exception as create_err:
                    logger.error(f"No se pudo crear la hoja 'Estado_Eventos': {create_err}")
                    self.event_state_sheet_obj = None # Marcar como no disponible
            # --- FIN NUEVO ESTADO EVENTOS ---

            # ---> ¡AQUÍ! Inicializar atributos de caché en la instancia SIEMPRE <---
            self._phone_cache = None
            self._phone_cache_last_refresh = 0
            self._pr_name_map_cache = None # NUEVO: Cache para el mapeo tel -> nombre PR
            self._vip_phone_cache = None # NUEVO: Cache para teléfonos VIP
            self._vip_phone_cache_last_refresh = 0 # NUEVO: Timestamp para caché VIP
            self._vip_pr_map_cache = None # NUEVO: Cache para mapeo VIP -> PR Name
            self._vip_pr_map_last_refresh = 0 # NUEVO: Timestamp para caché mapeo VIP
            self._qr_special_cache = None # NUEVO: Cache para números especiales QR
            self._qr_special_cache_last_refresh = 0 # NUEVO: Timestamp para caché QR especiales
            self._event_state_cache = None # NUEVO: Cache para estado de eventos QR
            self._event_state_cache_last_refresh = 0 # NUEVO: Timestamp para caché estado eventos

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
                if len(headers) < 8 or 'ENVIADO' not in headers:
                    logger.info(f"Actualizando encabezados para incluir columna ENVIADO en '{event_name}'...")
                    # Expandir la hoja para tener suficientes columnas si es necesario
                    current_cols = event_sheet.col_count
                    if current_cols < 8:
                        event_sheet.add_cols(8 - current_cols)
                    if 'ENVIADO' not in headers:
                        headers.append('ENVIADO')
                    # Asegurar que solo tenemos exactamente 8 columnas
                    headers = headers[:8]  # Truncar a máximo 8 elementos
                    event_sheet.update('A1:H1', [headers])
                    logger.info(f"Encabezados actualizados en hoja existente '{event_name}'")
                    
                    # Aplicar casillas de verificación a la columna ENVIADO
                    try:
                        add_checkboxes_to_column(event_sheet, 8)  # 8 para columna H (ENVIADO)
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
                new_sheet = self.spreadsheet.add_worksheet(title=event_name, rows="1", cols="8")
                logger.info(f"Hoja creada con ID: {new_sheet.id}")
                
                # Definir encabezados incluyendo la columna ENVIADO
                expected_headers = ['Nombre y Apellido', 'Email', 'Genero', 'Publica', 'Evento', 'Timestamp', "ENVIADO"]
                update_result = new_sheet.update('A1:H1', [expected_headers])  # Cambiado a H1 para incluir 8 columnas
                logger.info(f"Encabezados añadidos: {update_result}")
                
                # Aplicar casillas de verificación a la columna ENVIADO
                try:
                    add_checkboxes_to_column(new_sheet, 8)
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
    
    # --- NUEVO: Método para obtener números especiales para QR ---
    def get_qr_special_phones(self):
        """
        Obtiene un set con los números de teléfono normalizados de la hoja 'QR_Especiales'.
        Estos números pueden seguir registrando invitados y enviar comandos de QR incluso 
        cuando el bot esté configurado para envío automático de QRs.
        Usa caché para eficiencia.
        """
        now = time.time()
        # Usar el mismo intervalo de caché que los otros teléfonos
        if self._qr_special_cache is not None and now - self._qr_special_cache_last_refresh < self._phone_cache_interval:
            return self._qr_special_cache

        logger.info("Refrescando caché de números especiales QR...")
        qr_special_phones_set = set()
        try:
            # Usa la referencia guardada en self.qr_special_sheet_obj
            qr_special_sheet = self.qr_special_sheet_obj
            if qr_special_sheet:
                # Asume Col A = Telefonos en hoja QR_Especiales, salta encabezado
                qr_special_phone_list_raw = qr_special_sheet.col_values(1)[1:]
                for phone in qr_special_phone_list_raw:
                    if phone:
                        normalized_phone = re.sub(r'\D', '', str(phone))
                        if normalized_phone:
                            qr_special_phones_set.add(normalized_phone)
                logger.info(f"Cargados {len(qr_special_phones_set)} números especiales QR.")
            else:
                # Hoja QR_Especiales no encontrada o no accesible
                logger.warning("No se puede refrescar caché QR especiales porque la hoja 'QR_Especiales' no está disponible.")
                # Devolver caché anterior o vacío
                qr_special_phones_set = self._qr_special_cache if self._qr_special_cache is not None else set()

            self._qr_special_cache = qr_special_phones_set
            self._qr_special_cache_last_refresh = now
            return self._qr_special_cache

        except gspread.exceptions.APIError as e:
            logger.error(f"Error de API al leer la hoja 'QR_Especiales': {e}. Usando caché QR especiales anterior si existe.")
            return self._qr_special_cache if self._qr_special_cache is not None else set()
        except Exception as e:
            logger.error(f"Error inesperado al obtener números especiales QR: {e}. Usando caché QR especiales anterior si existe.")
            return self._qr_special_cache if self._qr_special_cache is not None else set()
    
    # --- NUEVO: Métodos para gestionar estado de eventos QR ---
    def get_event_qr_states(self):
        """
        Obtiene el estado de envío automático de QRs para todos los eventos.
        Retorna un diccionario {evento: True/False} indicando si ya se envió QR automático.
        Usa caché para eficiencia.
        """
        now = time.time()
        # Usar el mismo intervalo de caché que los otros datos
        if self._event_state_cache is not None and now - self._event_state_cache_last_refresh < self._phone_cache_interval:
            return self._event_state_cache

        logger.info("Refrescando caché de estados de eventos QR...")
        event_states = {}
        try:
            # Usa la referencia guardada en self.event_state_sheet_obj
            event_state_sheet = self.event_state_sheet_obj
            if event_state_sheet:
                # Obtener todos los registros de la hoja
                records = event_state_sheet.get_all_records()
                for record in records:
                    evento = record.get('Evento', '').strip()
                    qr_enviado = record.get('QR_Automatico_Enviado', False)
                    
                    if evento:
                        # Convertir a booleano si viene como string
                        if isinstance(qr_enviado, str):
                            qr_enviado = qr_enviado.upper() in ['TRUE', 'SI', 'SÍ', 'YES', '1']
                        elif qr_enviado is None:
                            qr_enviado = False
                        
                        event_states[evento] = bool(qr_enviado)
                
                logger.info(f"Cargados estados de {len(event_states)} eventos QR.")
            else:
                # Hoja Estado_Eventos no encontrada o no accesible
                logger.warning("No se puede refrescar caché de estados eventos porque la hoja 'Estado_Eventos' no está disponible.")
                # Devolver caché anterior o vacío
                event_states = self._event_state_cache if self._event_state_cache is not None else {}

            self._event_state_cache = event_states
            self._event_state_cache_last_refresh = now
            return self._event_state_cache

        except gspread.exceptions.APIError as e:
            logger.error(f"Error de API al leer la hoja 'Estado_Eventos': {e}. Usando caché de estados anterior si existe.")
            return self._event_state_cache if self._event_state_cache is not None else {}
        except Exception as e:
            logger.error(f"Error inesperado al obtener estados de eventos QR: {e}. Usando caché anterior si existe.")
            return self._event_state_cache if self._event_state_cache is not None else {}
    
    def is_event_qr_sent(self, event_name):
        """
        Verifica si un evento específico ya tuvo envío automático de QRs.
        
        Args:
            event_name (str): Nombre del evento a verificar
            
        Returns:
            bool: True si ya se enviaron QRs automáticamente, False en caso contrario
        """
        try:
            event_states = self.get_event_qr_states()
            return event_states.get(event_name, False)
        except Exception as e:
            logger.error(f"Error verificando estado QR del evento '{event_name}': {e}")
            return False  # En caso de error, asumir que no se envió (permitir registro)
    
    def mark_event_qr_sent(self, event_name):
        """
        Marca un evento como que ya tuvo envío automático de QRs.
        
        Args:
            event_name (str): Nombre del evento a marcar
            
        Returns:
            bool: True si se marcó exitosamente, False en caso contrario
        """
        try:
            if not self.event_state_sheet_obj:
                logger.error("No se puede marcar estado QR: hoja 'Estado_Eventos' no disponible")
                return False
            
            # Buscar si ya existe registro para este evento
            records = self.event_state_sheet_obj.get_all_records()
            row_to_update = None
            
            for i, record in enumerate(records, start=2):  # Start at row 2 (after headers)
                if record.get('Evento', '').strip() == event_name:
                    row_to_update = i
                    break
            
            current_time = datetime.now()
            fecha_envio = current_time.strftime('%Y-%m-%d')
            hora_envio = current_time.strftime('%H:%M:%S')
            
            if row_to_update:
                # Actualizar registro existente
                self.event_state_sheet_obj.update_cell(row_to_update, 2, True)  # QR_Automatico_Enviado
                self.event_state_sheet_obj.update_cell(row_to_update, 3, fecha_envio)  # Fecha_Envio
                self.event_state_sheet_obj.update_cell(row_to_update, 4, hora_envio)  # Hora_Envio
                logger.info(f"Actualizado estado QR automático para evento '{event_name}'")
            else:
                # Crear nuevo registro
                new_row = [event_name, True, fecha_envio, hora_envio]
                self.event_state_sheet_obj.append_row(new_row, value_input_option='USER_ENTERED')
                logger.info(f"Creado nuevo estado QR automático para evento '{event_name}'")
            
            # Invalidar caché para forzar actualización
            self._event_state_cache = None
            self._event_state_cache_last_refresh = 0
            
            return True
            
        except Exception as e:
            logger.error(f"Error marcando estado QR para evento '{event_name}': {e}")
            return False
        
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

    def get_phone_pr_email_mapping(self):
        """
        Obtiene un diccionario que mapea números de teléfono normalizados
        a los emails de PR correspondientes desde la hoja 'Telefonos'.
        Usa caché para eficiencia.
        """
        now = time.time()
        # Usar el mismo intervalo de caché que otros mapeos
        if hasattr(self, '_pr_email_map_cache') and self._pr_email_map_cache is not None and hasattr(self, '_pr_email_map_last_refresh') and now - self._pr_email_map_last_refresh < self._phone_cache_interval:
            return self._pr_email_map_cache

        logger.info("Refrescando caché de mapeo Telefono -> Email PR...")
        phone_to_pr_email_map = {}
        try:
            phone_sheet = self.phone_sheet_obj
            if phone_sheet:
                # Leer columnas (A=Telefonos, B=PR, C=Email)
                # Usamos get_all_values para asegurar que las filas coincidan
                all_values = phone_sheet.get_all_values()
                if len(all_values) > 1: # Asegurar que hay datos además del encabezado
                    # Asumimos encabezados en la fila 1, empezamos desde la fila 2 (índice 1)
                    for row in all_values[1:]:
                        if len(row) >= 3: # Asegurar que la fila tiene al menos 3 columnas (Telefono, PR, Email)
                            raw_phone = row[0] # Columna A (índice 0) - Telefonos
                            pr_name = row[1]   # Columna B (índice 1) - PR
                            pr_email = row[2]  # Columna C (índice 2) - Email
                            if raw_phone and pr_email: # Solo procesar si teléfono y email tienen valor
                                normalized_phone = re.sub(r'\D', '', str(raw_phone))
                                if normalized_phone:
                                    phone_to_pr_email_map[normalized_phone] = pr_email.strip()
                        else:
                            logger.warning(f"Fila incompleta en hoja 'Telefonos' (necesita 3 columnas): {row}")
                logger.info(f"Creado mapeo para {len(phone_to_pr_email_map)} teléfonos a emails PR.")
            else:
                logger.error("No se puede refrescar mapeo Email PR porque hoja 'Telefonos' no está disponible.")
                # Mantenemos el caché vacío o el anterior si hubo error temporal
                phone_to_pr_email_map = getattr(self, '_pr_email_map_cache', {})

            self._pr_email_map_cache = phone_to_pr_email_map
            self._pr_email_map_last_refresh = now
            return self._pr_email_map_cache

        except gspread.exceptions.APIError as e:
            logger.error(f"Error de API al leer la hoja 'Telefonos' para mapeo Email PR: {e}. Usando caché anterior si existe.")
            return getattr(self, '_pr_email_map_cache', {})
        except Exception as e:
            logger.error(f"Error inesperado al obtener mapeo Email PR: {e}. Usando caché anterior si existe.")
            return getattr(self, '_pr_email_map_cache', {})


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
    Versión MEJORADA para mayor robustez con detección flexible de categorías de género.
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

        # --- Detectar Categorías con patrones más flexibles ---
        is_category = False
        potential_category_key = None
        
        # Patrones flexibles para categorías masculinas
        if re.match(r'(?i)^(hombres?|varones?)[\s:]*$', line):
            potential_category_key = 'Hombres'
            is_category = True
        # Patrones flexibles para categorías femeninas
        elif re.match(r'(?i)^(mujeres?|damas?)[\s:]*$', line):
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

            # Determinar género final
            final_genero = genero
            
            # Si no hay género específico (categoría 'General' o similar) y tenemos nombre, usar IA
            if genero in ["Otro", None] and nombre:
                inferred = infer_gender_llm(nombre)
                if inferred.lower() in ['hombre', 'masculino']:
                    final_genero = "Masculino"
                elif inferred.lower() in ['mujer', 'femenino']:
                    final_genero = "Femenino"
                else:
                    final_genero = "Otro"  # Fallback si no se pudo determinar
                logger.debug(f"Género inferido por IA para '{nombre}': {inferred} -> {final_genero}")
            
            guest_info = {
                "nombre": nombre,
                "apellido": apellido,
                "email": email,
                "genero": final_genero
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
    
    
    
    # Verificar si es una consulta de conteo
    count_patterns = [
        r'(?i)cu[aá]ntos invitados',
        r'(?i)contar invitados',
        r'(?i)total de invitados',
        r'(?i)invitados totales',
        r'(?i)lista de invitados'
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
    Versión simplificada que solo detecta comandos específicos (count/help)
    y trata todo lo demás como mensaje genérico
    
    Args:
        message (str): Mensaje del usuario
        
    Returns:
        dict: Información sobre el comando, datos y categorías detectadas
    """
    # Comprobar comandos específicos que deben ser tratados aparte
    
    # Verificar si es una consulta de conteo
    count_patterns = [
        r'(?i)cu[aá]ntos invitados',
        r'(?i)contar invitados',
        r'(?i)total de invitados',
        r'(?i)invitados totales',
        r'(?i)lista de invitados'
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
    
    # Cualquier otro mensaje se trata como genérico para mostrar eventos
    # (incluidos saludos, texto aleatorio, emojis, etc.)
    lines = message.strip().split('\n')
    valid_lines = [line.strip() for line in lines if line.strip()]
    
    return {
        'command_type': 'generic_message',
        'data': valid_lines if valid_lines else [message.strip()],
        'categories': None
    }

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
                # Expandir la hoja para tener suficientes columnas si es necesario
                current_cols = sheet.col_count
                if current_cols < len(expected_headers):
                    sheet.add_cols(len(expected_headers) - current_cols)
                sheet.update('A1:G1', [expected_headers])
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
                    # Limpiar colores de fondo de las filas recién agregadas
                    clear_background_color_for_new_rows(sheet, len(rows_to_add))
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
            # Expandir la hoja para tener suficientes columnas si es necesario
            current_cols = sheet.col_count
            if current_cols < len(expected_headers):
                sheet.add_cols(len(expected_headers) - current_cols)
            # Actualizar el rango correcto A1:G1 para 7 columnas
            sheet.update('A1:G1', [expected_headers])


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
                False                           # Columna G: ENVIADO (casilla de verificación)
            ])

        # --- Agregar a la hoja ---
        if rows_to_add:
            try:
                sheet.append_rows(rows_to_add, value_input_option='USER_ENTERED')
                # Limpiar colores de fondo de las filas recién agregadas
                clear_background_color_for_new_rows(sheet, len(rows_to_add))
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
def parse_vip_guest_list_with_instagram(message_body):
    """
    Parsea formato VIP con Instagram: Nombres -> Emails -> Instagram Links
    Detecta encabezados opcionales 'Hombres:'/'Mujeres:'. Permite categorías vacías.
    
    Args:
        message_body (str): Mensaje del usuario con formato N->E->I
        
    Returns:
        tuple: (list, dict) donde:
            - list: Lista de diccionarios con info estructurada [{'nombre': n, 'apellido': a, 'email': e, 'instagram': i, 'genero': g}, ...]
            - dict: Información del error si ocurrió, o None si no hubo errores
    """
    guests = []
    error_info = None
    
    # Dividir por líneas y limpiar
    lines = [line.strip() for line in message_body.split('\n') if line.strip()]
    
    # Usaremos listas separadas por categoría para nombres, emails e Instagram
    data_by_category = {} # Ejemplo: {'Hombres': {'names': [], 'emails': [], 'instagrams': []}, 'Mujeres': {...}}
    category_map = {"Hombres": "Masculino", "Mujeres": "Femenino"}

    current_category_key = None # Empezar sin categoría definida
    parsing_mode = 'category_or_names' # Estados: category_or_names, names, emails, instagrams

    logger.info("Iniciando extracción VIP con Instagram (Nombres -> Emails -> Instagram)...")

    for line in lines:
        if not line:
            continue # Ignorar líneas vacías

        # --- Detectar Categorías ---
        is_category = False
        potential_category_key = None
        
        # Patrones flexibles para categorías masculinas
        if re.match(r'^hombres?\s*:?\s*$', line, re.IGNORECASE):
            potential_category_key = "Hombres"
            is_category = True
        # Patrones flexibles para categorías femeninas  
        elif re.match(r'^mujeres?\s*:?\s*$', line, re.IGNORECASE):
            potential_category_key = "Mujeres"
            is_category = True
        
        if is_category:
            current_category_key = potential_category_key
            parsing_mode = 'names'
            # Inicializar categoría si no existe
            if current_category_key not in data_by_category:
                data_by_category[current_category_key] = {'names': [], 'emails': [], 'instagrams': []}
            logger.debug(f"Detectado encabezado de categoría: '{current_category_key}'")
            continue

        # --- Detectar Emails ---
        is_email = '@' in line and '.' in line.split('@')[-1] and len(line.split('@')[0]) > 0
        
        if is_email and parsing_mode in ['names', 'emails']:
            parsing_mode = 'emails'
            # Asegurarse que tenemos una categoría
            if current_category_key is None:
                current_category_key = "Default"
                if current_category_key not in data_by_category:
                    data_by_category[current_category_key] = {'names': [], 'emails': [], 'instagrams': []}
            
            # Buscar múltiples emails en la línea usando regex
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            found_emails = re.findall(email_pattern, line)
            
            if found_emails:
                # Añadir todos los emails encontrados en orden
                for email in found_emails:
                    data_by_category[current_category_key]['emails'].append(email)
                logger.debug(f"Emails encontrados en línea: {found_emails}")
            else:
                # Fallback: usar el método anterior para emails que no pasen el regex más estricto
                if re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", line):
                    data_by_category[current_category_key]['emails'].append(line)
            continue

        # --- Detectar Instagram Links ---
        is_instagram = ('instagram.com' in line.lower() or 'instagram' in line.lower() or 
                       line.startswith('@') or line.startswith('http'))
        
        if is_instagram and parsing_mode in ['emails', 'instagrams']:
            parsing_mode = 'instagrams'
            # Asegurarse que tenemos una categoría
            if current_category_key is None:
                current_category_key = "Default"
                if current_category_key not in data_by_category:
                    data_by_category[current_category_key] = {'names': [], 'emails': [], 'instagrams': []}
            
            data_by_category[current_category_key]['instagrams'].append(line)
            continue

        # --- Procesar Nombres ---
        if parsing_mode in ['category_or_names', 'names']:
            parsing_mode = 'names'
            # Asegurarse que tenemos una categoría
            if current_category_key is None:
                current_category_key = "Default"
                if current_category_key not in data_by_category:
                    data_by_category[current_category_key] = {'names': [], 'emails': [], 'instagrams': []}
            
            # Validar que parece un nombre válido
            if re.match(r"^[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ\s'.]+$", line) and len(line) > 1:
                data_by_category[current_category_key]['names'].append(line)
            continue

    # --- Procesar datos por categoría ---
    for category_key, category_data in data_by_category.items():
        names = category_data['names']
        emails = category_data['emails']
        instagrams = category_data['instagrams']
        
        # Verificar balance
        if len(names) != len(emails) or len(names) != len(instagrams):
            error_info = {
                'error_type': 'desbalance',
                'category': category_key,
                'names_count': len(names),
                'emails_count': len(emails),
                'instagrams_count': len(instagrams)
            }
            logger.error(f"Desbalance en categoría '{category_key}': {len(names)} nombres, {len(emails)} emails, {len(instagrams)} instagrams")
            return [], error_info
        
        # Crear invitados
        for i in range(len(names)):
            name_parts = names[i].split()
            nombre = name_parts[0] if name_parts else ""
            apellido = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
            
            # Determinar género
            genero = category_map.get(category_key, None)
            
            # Si no hay categoría específica (Default) o no se pudo mapear, usar IA
            if genero is None and category_key == "Default" and nombre:
                inferred = infer_gender_llm(nombre)
                if inferred.lower() in ['hombre', 'masculino']:
                    genero = "Masculino"
                elif inferred.lower() in ['mujer', 'femenino']:
                    genero = "Femenino"
                else:
                    genero = None  # Mantener como None si no se pudo determinar
                logger.debug(f"Género inferido por IA para '{nombre}': {inferred} -> {genero}")
            
            guest = {
                'nombre': nombre,
                'apellido': apellido,
                'email': emails[i],
                'instagram': instagrams[i],
                'genero': genero
            }
            guests.append(guest)
    
    if not guests and lines:  # Si había líneas pero no se procesaron invitados
        error_info = {'error_type': 'no_valid_pairs'}
        
    return guests, error_info

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
                # Determinar género final
                genero = name_info['genero']
                nombre = name_info['nombre'].strip()
                
                # Si no hay género específico y tenemos nombre, usar IA
                if genero is None and nombre:
                    inferred = infer_gender_llm(nombre)
                    if inferred.lower() in ['hombre', 'masculino']:
                        genero = "Masculino"
                    elif inferred.lower() in ['mujer', 'femenino']:
                        genero = "Femenino"
                    else:
                        genero = None  # Mantener None si no se pudo determinar
                    logger.debug(f"Género inferido por IA para '{nombre}': {inferred} -> {genero}")
                
                paired_guests.append({
                    'nombre': nombre,
                    'email': email_clean,
                    'genero': genero
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
                               guest.get('PR') == pr_name or 
                               guest.get('PR') == phone_number]
                
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
            # Usar directamente el valor de la columna TIPO
            tipo = guest.get('TIPO', 'Sin categoría')
            event_categories[tipo] = event_categories.get(tipo, 0) + 1

        # Añadir conteos por género para el evento
        has_gender_counts = False
        for category, count in event_categories.items():
             if count > 0:
                response_parts.append(f"📊 {category}: {count}")
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
            # Usar directamente el valor de la columna TIPO
            tipo = guest.get('TIPO', 'Sin categoría')
            if tipo not in guests_by_gender_in_event:
                guests_by_gender_in_event[tipo] = []
            guests_by_gender_in_event[tipo].append(guest)

        for tipo, guests in guests_by_gender_in_event.items():
            response_parts.append(f"*{tipo}*:")
            for guest in guests:
                name_keys = ['Nombre y Apellido', 'Nombre', 'nombre']
                email_keys = ['Email', 'email']
                full_name = next((guest[k] for k in name_keys if k in guest and guest[k]), '').strip()
                if not full_name:
                     nombre = guest.get('nombre', '')
                     apellido = guest.get('apellido', '')
                     full_name = f"{nombre} {apellido}".strip() or "?(sin nombre)"
                email = next((guest[k] for k in email_keys if k in guest and guest[k]), '?(sin email)')
                
                # Obtener el estado de 'Enviado' (casilla de verificación)
                logger.info(f"DEBUG SUMMARY: Claves disponibles en guest: {list(guest.keys())}")
                enviado = guest.get('Enviado', '') or guest.get('enviado', '')
                logger.info(f"DEBUG SUMMARY: Valor de enviado para {full_name}: '{enviado}' (tipo: {type(enviado)})")
                
                if enviado is True or str(enviado).upper() == 'TRUE':
                    enviado_status = '✅ Enviado'
                elif enviado is False or str(enviado).upper() == 'FALSE':
                    enviado_status = '❌ No enviado'
                elif enviado == '' or enviado is None:
                    enviado_status = '⚪ Sin verificar'
                else:
                    enviado_status = f'❓ {enviado}'

                logger.info(f"DEBUG SUMMARY: Estado final para {full_name}: {enviado_status}")
                response_parts.append(f"  • {full_name} - {email} ({enviado_status})")

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
            # Usar directamente el valor de la columna TIPO
            tipo = guest.get('TIPO', 'Sin categoría')

            if tipo not in guests_by_gender:
                guests_by_gender[tipo] = []
            guests_by_gender[tipo].append(guest)

        # Mostrar invitados por tipo
        for tipo, guests in guests_by_gender.items():
            base_response += f"\n*{tipo}*:\n"
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
                
                # Obtener el estado de 'Enviado' (casilla de verificación)
                logger.info(f"DEBUG: Claves disponibles en guest: {list(guest.keys())}")
                enviado = guest.get('Enviado', '') or guest.get('enviado', '')
                logger.info(f"DEBUG: Valor de enviado para {full_name}: '{enviado}' (tipo: {type(enviado)})")
                
                if enviado is True or str(enviado).upper() == 'TRUE':
                    enviado_status = '✅ Enviado'
                elif enviado is False or str(enviado).upper() == 'FALSE':
                    enviado_status = '❌ No enviado'
                elif enviado == '' or enviado is None:
                    enviado_status = '⚪ Sin verificar'
                else:
                    enviado_status = f'❓ {enviado}'

                logger.info(f"DEBUG: Estado final para {full_name}: {enviado_status}")
                base_response += f"  • {full_name} - {email} ({enviado_status})\n"

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
                test_row = ["TEST", "test@example.com", "Otro", "Test PR", "Test Event", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), False]
                result = guest_sheet.append_row(test_row, value_input_option='USER_ENTERED')
                # Limpiar color de fondo de la fila de test
                clear_background_color_for_new_rows(guest_sheet, 1)
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
                # Limpiar color de fondo de la fila de test
                clear_background_color_for_new_rows(vip_sheet, 1)
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
                    test_row = ["TEST EVENT", "test@example.com", "Otro", "Test PR", events[0], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), False]
                    result = event_sheet.append_row(test_row, value_input_option='USER_ENTERED')
                    # Limpiar color de fondo de la fila de test
                    clear_background_color_for_new_rows(event_sheet, 1)
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

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "message": "WhatsApp bot is running"}), 200


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
                    # Expandir la hoja para tener suficientes columnas si es necesario
                    worksheet.add_cols(1)
                # Asegurar que solo tenemos exactamente los elementos necesarios
                headers = headers[:len(headers)]  # Mantener todos los headers válidos
                max_cols = worksheet.col_count
                range_end = gspread.utils.rowcol_to_a1(1, min(len(headers), max_cols))
                worksheet.update(f'A1:{range_end}', [headers])
                
                # Aplicar casillas de verificación
                column_index = headers.index('ENVIADO') + 1  # Convertir índice 0-based a 1-based
                add_checkboxes_to_column(worksheet, column_index)
                
                results[worksheet.title] = "Configuración exitosa"
            except Exception as e:
                results[worksheet.title] = f"Error: {str(e)}"
        
        return jsonify({"status": "complete", "results": results})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ====================================
# --- Funciones para QR Automation ---
# ====================================

def get_pending_qr_guests_by_pr(sheet_conn, pr_phone_normalized, event_filter=None):
    """
    Obtiene invitados pendientes de recibir QR para un PR específico
    
    Args:
        sheet_conn: Instancia de SheetsConnection
        pr_phone_normalized: Número del PR normalizado  
        event_filter: Nombre del evento para filtrar (opcional)
    
    Returns:
        List[Dict]: Lista de invitados pendientes de QR
    """
    try:
        guests_by_event = get_guests_by_pr(sheet_conn, pr_phone_normalized)
        pending_guests = []
        
        for event_name, event_guests in guests_by_event.items():
            # Aplicar filtro de evento si existe
            if event_filter and event_name != event_filter:
                continue
                
            for guest in event_guests:
                # Verificar si el QR ya fue enviado
                qr_sent = guest.get('QR_ENVIADO', False) or guest.get('qr_enviado', False)
                enviado = guest.get('Enviado', False) or guest.get('enviado', False)
                
                # Solo incluir si no se ha enviado el QR y la invitación está enviada
                if not qr_sent and enviado:
                    guest_data = {
                        'name': guest.get('Nombre y Apellido') or guest.get('Nombre', 'Sin nombre'),
                        'email': guest.get('Email') or guest.get('email', ''),
                        'category': guest.get('TIPO', 'General'),
                        'event': event_name,
                        'pr_phone': pr_phone_normalized
                    }
                    pending_guests.append(guest_data)
        
        logger.info(f"Encontrados {len(pending_guests)} invitados pendientes de QR para PR {pr_phone_normalized}")
        return pending_guests
        
    except Exception as e:
        logger.error(f"Error obteniendo invitados pendientes de QR para PR {pr_phone_normalized}: {e}")
        return []


def get_all_pending_qr_guests(sheet_conn, event_filter=None):
    """
    Obtiene todos los invitados pendientes de recibir QR de todos los PRs
    
    Args:
        sheet_conn: Instancia de SheetsConnection
        event_filter: Nombre del evento para filtrar (opcional)
    
    Returns:
        List[Dict]: Lista de invitados pendientes de QR
    """
    try:
        # Obtener todos los números autorizados
        authorized_phones = sheet_conn.get_authorized_phones()
        if not authorized_phones:
            logger.warning("No hay números autorizados para procesar QRs")
            return []
        
        all_pending_guests = []
        
        for phone in authorized_phones:
            pr_guests = get_pending_qr_guests_by_pr(sheet_conn, phone, event_filter)
            all_pending_guests.extend(pr_guests)
        
        logger.info(f"Total de invitados pendientes de QR: {len(all_pending_guests)}")
        return all_pending_guests
        
    except Exception as e:
        logger.error(f"Error obteniendo todos los invitados pendientes de QR: {e}")
        return []


def update_qr_sent_status(sheet_conn, processed_guests, status=True):
    """
    Actualiza el estado QR_ENVIADO en Google Sheets para los invitados procesados
    
    Args:
        sheet_conn: Instancia de SheetsConnection
        processed_guests: Lista de invitados que fueron procesados
        status: Estado a establecer (True/False)
    """
    try:
        # Agrupar invitados por evento
        guests_by_event = {}
        for guest in processed_guests:
            event = guest.get('event')
            if event not in guests_by_event:
                guests_by_event[event] = []
            guests_by_event[event].append(guest)
        
        updated_count = 0
        
        # Actualizar cada evento
        for event_name, event_guests in guests_by_event.items():
            try:
                event_sheet = sheet_conn.get_sheet_by_event_name(event_name)
                if not event_sheet:
                    logger.warning(f"No se pudo acceder a la hoja del evento '{event_name}' para actualizar QR status")
                    continue
                
                # Obtener todos los registros actuales
                all_records = event_sheet.get_all_records()
                
                # Crear índice por nombre y email para encontrar filas
                for i, record in enumerate(all_records, start=2):  # Start at row 2 (after headers)
                    record_name = record.get('Nombre y Apellido') or record.get('Nombre', '')
                    record_email = record.get('Email') or record.get('email', '')
                    
                    # Buscar coincidencia en los invitados procesados
                    for guest in event_guests:
                        if (guest.get('name') == record_name and 
                            guest.get('email') == record_email):
                            
                            # Actualizar la columna QR_ENVIADO
                            try:
                                # Buscar la columna QR_ENVIADO o crearla si no existe
                                headers = event_sheet.row_values(1)
                                qr_col_index = None
                                
                                for idx, header in enumerate(headers, 1):
                                    if header == 'QR_ENVIADO':
                                        qr_col_index = idx
                                        break
                                
                                if qr_col_index is None:
                                    # Agregar columna QR_ENVIADO
                                    qr_col_index = len(headers) + 1
                                    event_sheet.update_cell(1, qr_col_index, 'QR_ENVIADO')
                                    logger.info(f"Creada columna QR_ENVIADO en evento {event_name}")
                                
                                # Actualizar el estado
                                event_sheet.update_cell(i, qr_col_index, status)
                                updated_count += 1
                                logger.info(f"Actualizado QR status para {record_name} en {event_name}")
                                
                            except Exception as update_error:
                                logger.error(f"Error actualizando QR status para {record_name}: {update_error}")
                            
                            break  # Found the guest, no need to continue searching
                            
            except Exception as event_error:
                logger.error(f"Error procesando evento {event_name} para actualización QR: {event_error}")
        
        logger.info(f"Total de registros actualizados con QR status: {updated_count}")
        
    except Exception as e:
        logger.error(f"Error actualizando estados QR en Google Sheets: {e}")


# --- Función whatsapp_reply COMPLETA con Lógica VIP ---
@app.route('/whatsapp', methods=['POST'])
def whatsapp_reply():
    global user_states
    response_text = None
    sender_phone_raw = None
    sender_phone_normalized = None
    sheet_conn = None
    is_vip = False # Variable para saber si el usuario es VIP
    error_info_parsing = None # Inicializar aquí para usar en STATE_AWAITING_GUEST_DATA


    try:
        data = request.form.to_dict()
        # logger.info(f"Datos recibidos (crudos): {data}") # Demasiado detallado, loggear solo lo necesario
        sender_phone_raw = data.get('From')
        incoming_msg = data.get('Body', '').strip()
        # incoming_msg_lower = incoming_msg.lower() # Opcional: usar versión lower si hay muchos checks case-insensitive


        if not incoming_msg or not sender_phone_raw:
            logger.warning("Payload inválido o mensaje vacío: falta 'Body' o 'From'")
            # No responder a mensajes vacíos
            return jsonify({"status": "ignored", "message": "Empty message or invalid payload"}), 200 # Retornar 200 OK for empty messages


        sender_phone_normalized = re.sub(r'\D', '', sender_phone_raw) # Normalizar número (quitar 'whatsapp:', '+', etc.)
        sheet_conn = SheetsConnection() # Obtener instancia

        # --- Validación de número autorizado GENERAL ---
        authorized_phones = sheet_conn.get_authorized_phones()
        # Primero verificar si authorized_phones se cargó correctamente
        if authorized_phones is None: # get_authorized_phones podría devolver None en caso de error CRÍTICO
             logger.critical("La lista de números autorizados es None. No se puede procesar.")
             # Devuelve un error 503 para que Twilio reintente, pero no envíes mensaje al usuario.
             return jsonify({"status": "ignored", "message": "Authorization list unavailable (critical error)"}), 503

        if not authorized_phones:
            # Esto ocurre si la hoja 'Telefonos' está vacía o solo tiene encabezados.
            logger.critical("La lista de números autorizados está vacía. Bloqueando todos los mensajes entrantes.")
            # Devuelve un error 503 para que Twilio reintente, pero no envíes mensaje al usuario.
            return jsonify({"status": "ignored", "message": "Authorization list is empty"}), 503


        if sender_phone_normalized not in authorized_phones:
            logger.warning(f"Mensaje de número NO AUTORIZADO: {sender_phone_raw} ({sender_phone_normalized}). Ignorando.")
            # Responder con un mensaje de "no autorizado" si quieres, o simplemente ignorar.
            # Ignorar es más seguro si no quieres exponer la existencia del bot a números no listados.
            # Si decides responder, hazlo AQUÍ y luego return 200.
            # response_text = "Lo siento, tu número no está autorizado para usar este servicio."
            # send_twilio_message(sender_phone_raw, response_text)
            return jsonify({"status": "ignored", "message": "Unauthorized number"}), 200 # OK para Twilio, pero ignoramos

        logger.info(f"Mensaje recibido de número AUTORIZADO: {sender_phone_raw} ({sender_phone_normalized})")
        # --- Fin Validación General ---

        # --- Chequeo VIP ---
        try:
            # Asegúrate que get_vip_phones devuelva un set o None
            vip_phones = sheet_conn.get_vip_phones()
            if vip_phones is not None and sender_phone_normalized in vip_phones:
                 is_vip = True
        except Exception as vip_err:
            logger.error(f"Error al verificar estado VIP para {sender_phone_normalized}: {vip_err}")
        logger.info(f"Usuario {sender_phone_normalized} es VIP: {is_vip}")
        # --- Fin Chequeo VIP ---

        # --- Obtener estado actual y datos relevantes del usuario ---
        # Si el número no está en user_states, .get() devuelve {}, y .get('state', STATE_INITIAL) será STATE_INITIAL.
        # Esto maneja automáticamente la primera interacción.
        user_status = user_states.get(sender_phone_normalized, {})
        current_state = user_status.get('state', STATE_INITIAL)
        selected_event = user_status.get('event')
        selected_guest_type = user_status.get('guest_type')
        # Recuperar eventos disponibles si estaban guardados en el estado (para STATE_AWAITING_EVENT_SELECTION)
        available_events = user_status.get('available_events', []) # Asegurarse que siempre es una lista


        logger.info(f"Usuario: {sender_phone_normalized}, VIP: {is_vip}, Estado: {current_state}, EventoSel: {selected_event}, TipoInvitadoSel: {selected_guest_type}, EventosEnEstado: {len(available_events)}")

        # Obtener referencia a la hoja de invitados VIP (puede ser None si no existe)
        # Se obtiene aquí porque puede ser necesaria en varios estados
        vip_guest_sheet = sheet_conn.get_vip_guest_sheet()

        # ====================================
        # --- Verificar comandos globales primero ---
        # ====================================
        
        # Verificar si es una consulta de conteo (funciona en cualquier estado)
        count_patterns = [
            r'cu[aá]ntos invitados',
            r'contar invitados',
            r'total de invitados',
            r'invitados totales',
            r'lista de invitados'
        ]
        
        is_count_command = False
        for pattern in count_patterns:
            if re.search(pattern, incoming_msg.lower()):
                is_count_command = True
                break
        
        if is_count_command:
            logger.info(f"Comando 'count' detectado en estado {current_state}.")
            guests_by_event = get_guests_by_pr(sheet_conn, sender_phone_normalized)

            # Obtener el nombre del PR (usando mapeo General o VIP según corresponda) para la respuesta
            pr_name_display = sender_phone_normalized # Fallback
            try:
                pr_map = sheet_conn.get_vip_phone_pr_mapping() if is_vip else sheet_conn.get_phone_pr_mapping()
                if pr_map:
                     pr_name_found = pr_map.get(sender_phone_normalized)
                     if pr_name_found: pr_name_display = pr_name_found
                     else: logger.warning(f"No se encontró nombre PR ({'VIP' if is_vip else 'General'}) mapeado para {sender_phone_normalized} para respuesta de conteo.")
                else:
                     logger.warning(f"Mapeo PR ({'VIP' if is_vip else 'General'}) no disponible para respuesta de conteo.")
            except Exception as e:
                 logger.error(f"Error buscando nombre PR para respuesta de conteo: {e}")

            response_text = generate_per_event_response(guests_by_event, pr_name_display, sender_phone_normalized)

            # Resetear estado a INITIAL después de mostrar el conteo
            user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
            
            # Enviar respuesta y salir
            if response_text:
                if not send_twilio_message(sender_phone_raw, response_text):
                    logger.error(f"Fallo al enviar mensaje de respuesta de conteo a {sender_phone_raw}")
                    return jsonify({"status": "processed_with_send_error"}), 200
                else:
                    logger.info(f"Respuesta de conteo enviada a {sender_phone_raw}")
                    return jsonify({"status": "success"}), 200

        # ====================================
        # --- Verificar comando QR (solo números especiales) ---
        # ====================================
        
        # Verificar si es un comando QR
        qr_patterns = [
            r'(?i)^enviar\s+qr',
            r'(?i)^enviar\s+qrs?',
            r'(?i)^send\s+qr',
            r'(?i)^qr\s+send',
            r'(?i)^procesar\s+qr',
            r'(?i)^mandar\s+qr'
        ]
        
        is_qr_command = False
        for pattern in qr_patterns:
            if re.search(pattern, incoming_msg.strip()):
                is_qr_command = True
                break
        
        if is_qr_command:
            logger.info(f"Comando QR detectado en estado {current_state} desde {sender_phone_normalized}.")
            
            # Verificar si el número está en la lista de números especiales QR
            try:
                qr_special_phones = sheet_conn.get_qr_special_phones()
                if sender_phone_normalized not in qr_special_phones:
                    logger.warning(f"Comando QR denegado para número no especial: {sender_phone_normalized}")
                    response_text = """🚫 Lo siento, tu número no tiene permisos para enviar comandos de QR.
                    
Solo los números especiales configurados pueden usar esta función. Si necesitas acceso, contacta al administrador."""
                    send_twilio_message(sender_phone_raw, response_text)
                    return jsonify({"status": "success"}), 200
                
                logger.info(f"Número especial QR confirmado: {sender_phone_normalized}")
                
                # Procesar comando QR - obtener invitados pendientes de este PR
                pending_guests = get_pending_qr_guests_by_pr(sheet_conn, sender_phone_normalized)
                
                if not pending_guests:
                    response_text = """📋 No tienes invitados pendientes de recibir códigos QR en este momento.
                    
Los códigos QR solo se envían a invitados que ya tienen la invitación marcada como "Enviado: ✅" pero aún no han recibido su QR."""
                    send_twilio_message(sender_phone_raw, response_text)
                    return jsonify({"status": "success"}), 200
                
                # Confirmar y procesar
                total_pending = len(pending_guests)
                logger.info(f"Iniciando proceso QR manual para {total_pending} invitados del número especial {sender_phone_normalized}")
                
                # Enviar confirmación inmediata
                response_text = f"""🚀 Iniciando envío de códigos QR para {total_pending} invitados pendientes.

El proceso se ejecutará en segundo plano y puede tomar unos minutos. Te notificaremos cuando esté completo."""
                send_twilio_message(sender_phone_raw, response_text)
                
                # Procesar en background
                import threading
                
                def process_qr_for_special_number():
                    try:
                        logger.info(f"🚀 INICIO PROCESO QR ESPECIAL para {sender_phone_normalized}")
                        logger.info(f"📊 Invitados a procesar: {len(pending_guests)}")
                        
                        # Log detallado de los invitados
                        for i, guest in enumerate(pending_guests[:3], 1):  # Mostrar primeros 3
                            logger.info(f"👤 Invitado {i}: {guest.get('name', 'Sin nombre')} - {guest.get('email', 'Sin email')}")
                        
                        logger.info("🌐 Iniciando PlanOutAutomation...")
                        with PlanOutAutomation() as automation:
                            logger.info("🔐 Ejecutando full_automation_workflow...")
                            result = automation.full_automation_workflow(pending_guests)
                            logger.info(f"✅ Resultado de automatización: {result}")
                        
                        if result.get("success"):
                            # Actualizar Google Sheets
                            update_qr_sent_status(sheet_conn, pending_guests, True)
                            
                            # Marcar eventos como que ya tuvieron envío automático de QRs
                            events_processed = set()
                            for guest in pending_guests:
                                event_name = guest.get('event')
                                if event_name and event_name not in events_processed:
                                    if sheet_conn.mark_event_qr_sent(event_name):
                                        logger.info(f"Evento '{event_name}' marcado como QR automático enviado (comando especial)")
                                        events_processed.add(event_name)
                                    else:
                                        logger.warning(f"No se pudo marcar evento '{event_name}' como QR enviado (comando especial)")
                            
                            success_msg = f"""✅ ¡Códigos QR enviados exitosamente!

📊 Procesados: {total_pending} invitados
⏰ Completado en: {datetime.now().strftime('%H:%M:%S')}

Los invitados recibirán sus códigos QR por email."""
                            send_twilio_message(sender_phone_raw, success_msg)
                            logger.info(f"Proceso QR especial completado exitosamente para {sender_phone_normalized}")
                            
                        else:
                            error_msg = f"""❌ Error en el envío de códigos QR.

Error: {result.get('error', 'Error desconocido')}

Por favor intenta nuevamente en unos minutos o contacta al administrador."""
                            send_twilio_message(sender_phone_raw, error_msg)
                            logger.error(f"Error en proceso QR especial para {sender_phone_normalized}: {result.get('error', 'Error desconocido')}")
                            
                    except Exception as e:
                        error_msg = f"""❌ Error crítico en el proceso de QR.

Error técnico: {str(e)}

Contacta al administrador."""
                        send_twilio_message(sender_phone_raw, error_msg)
                        logger.error(f"Error crítico en proceso QR especial para {sender_phone_normalized}: {e}")
                        logger.error(traceback.format_exc())
                
                # Iniciar proceso en background
                thread = threading.Thread(target=process_qr_for_special_number)
                thread.daemon = True
                thread.start()
                
                return jsonify({"status": "success"}), 200
                
            except Exception as qr_err:
                logger.error(f"Error procesando comando QR para {sender_phone_normalized}: {qr_err}")
                response_text = """❌ Error interno procesando comando QR.

Por favor intenta nuevamente en unos minutos."""
                send_twilio_message(sender_phone_raw, response_text)
                return jsonify({"status": "success"}), 200

        # ====================================
        # --- Lógica Principal de Estados ---
        # ====================================

        # MODIFICACIÓN: En STATE_INITIAL, CUALQUIER mensaje (que no sea count o help)
        # desencadena la lista de eventos.
        if current_state == STATE_INITIAL:
            logger.info(f"Procesando mensaje en STATE_INITIAL para {sender_phone_normalized}")
            parsed_command = parse_message_enhanced(incoming_msg)
            command_type = parsed_command['command_type']
            logger.info(f"Comando parseado en INITIAL: '{command_type}'")

            # Manejar el comando 'help'
            if command_type == 'help':
                 logger.info(f"Comando 'help' detectado.")
                 # Verificar si es número especial QR para mostrar funcionalidad adicional
                 qr_special_phones = sheet_conn.get_qr_special_phones()
                 is_qr_special = sender_phone_normalized in qr_special_phones
                 
                 welcome_text = """👋 ¡Hola! Bienvenido al sistema de gestión de invitados. 

Puedo ayudarte con la administración de tu lista de invitados. Aquí tienes lo que puedes hacer:

1️⃣ *Agregar invitados*: 
   Envía cualquier mensaje (excepto 'lista' o 'ayuda') para ver los eventos disponibles, elige uno, y luego sigue las instrucciones para enviar la lista en el formato Nombres -> Emails.

2️⃣ *Consultar invitados*:
  • Escribe "cuántos invitados" o "lista de invitados" para ver tu total por evento.

3️⃣ *Ayuda*:
  • Escribe "ayuda" para ver estas instrucciones de nuevo.
  • Si estás en medio de una operación, escribe "cancelar" para empezar de nuevo."""

                 if is_qr_special:
                     welcome_text += """

🚀 *Funciones especiales* (disponibles para tu número):
  • Escribe "enviar qr" o "mandar qr" para procesar y enviar códigos QR a tus invitados pendientes.
  • **Privilegio especial**: Puedes seguir registrando invitados DESPUÉS de que se dispare el envío automático de QRs (8pm)."""

                 welcome_text += """

¿En qué puedo ayudarte hoy?""" # Mensaje de ayuda actualizado
                 response_text = welcome_text
                 # Mantener estado INITIAL después de la ayuda
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}


            # ESTA ES LA RAMA CLAVE: Cualquier otro comando o texto en STATE_INITIAL
            # (incluyendo 'saludo' si no fue match exacto al inicio, 'unknown', 'add_guests_split', etc.)
            # ahora inicia el flujo de selección de evento.
            else:
                logger.info(f"Mensaje genérico o no específico recibido. Iniciando flujo de selección de evento.")
                available_events = sheet_conn.get_available_events()
                if not available_events:
                    response_text = "¡Hola! 👋 No encontré eventos disponibles en este momento."
                    # Mantener estado INITIAL si no hay eventos
                    user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'available_events': [], 'guest_type': None}
                else:
                    event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                    base_response_text = f"""¡Hola! 👋 Soy el Agente de Invitaciones de SVG 😎

Conmigo vas a poder anotar tus Invitaciones para nuestros eventos con cortesias disponibles ! 🤩

Eventos disponibles:

{event_list_text}

Responde con el número del evento en el que deseas anotar tus invitaciones.

Si quieres saber tus invitados ya anotados en la lista, y ademas cuales de esos QR ya fueron enviados, escribe:
"cuántos invitados" o "lista de invitados"

Ante cualquier duda, falla o feedback comunicate con Anto: wa.me/5491164855744
"""
                    if is_vip:
                        vip_message = "\n\n✨ *Nota: Como PR VIP, tienes acceso especial.*"
                        response_text = base_response_text + vip_message
                    else:
                        response_text = base_response_text

                    # Transición a estado de espera de selección de evento
                    user_status['state'] = STATE_AWAITING_EVENT_SELECTION
                    user_status['event'] = None # Asegurar que no hay evento seleccionado previo
                    user_status['available_events'] = available_events # Almacenar para el siguiente paso
                    user_status['guest_type'] = None # Resetear tipo de invitado
                    user_states[sender_phone_normalized] = user_status # Actualizar estado en memoria global


        # --- Estado: Esperando Selección de Evento ---
        elif current_state == STATE_AWAITING_EVENT_SELECTION:
             logger.info(f"Procesando mensaje en STATE_AWAITING_EVENT_SELECTION para {sender_phone_normalized}")
             # Debe haber eventos disponibles guardados en el estado para que esto funcione
             if not available_events:
                 logger.error(f"Usuario {sender_phone_normalized} en AWAITING_EVENT_SELECTION pero sin eventos disponibles guardados. Reiniciando a INITIAL.")
                 response_text = "Hubo un problema, no recuerdo los eventos. Por favor, envía cualquier mensaje para empezar de nuevo."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
             # Permitir "cancelar" en este estado
             elif incoming_msg.lower() in ["cancelar", "salir", "cancel", "exit"]:
                 logger.info(f"Usuario {sender_phone_normalized} canceló la selección de evento.")
                 response_text = "Selección cancelada. Puedes enviar cualquier mensaje para ver los eventos disponibles de nuevo."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
             else:
                  try:
                      choice_index = int(incoming_msg) - 1 # Convertir a índice 0-based
                      if 0 <= choice_index < len(available_events):
                          selected_event = available_events[choice_index]
                          logger.info(f"Usuario {sender_phone_normalized} seleccionó evento: {selected_event}")

                          # Guardar el evento seleccionado y actualizar estado
                          user_status['event'] = selected_event
                          # Ya no necesitamos available_events en el siguiente estado, limpiamos.
                          user_status['available_events'] = []

                          # Preguntar tipo de invitado para TODOS los usuarios
                          response_text = f"Evento *{selected_event}* seleccionado. ✨\n\nResponde solo con el numero:\n1) General\n2) VIP"
                          user_status['state'] = STATE_AWAITING_GUEST_TYPE
                          user_status['guest_type'] = None # Limpiar por si acaso
                          user_states[sender_phone_normalized] = user_status # Actualizar estado en memoria global
                          # Este código se eliminó - ahora todos pasan por STATE_AWAITING_GUEST_TYPE
                      else: # Número fuera de rango
                          event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                          response_text = f"❌ Número '{incoming_msg}' fuera de rango. Por favor, elige un número válido de la lista:\n\n{event_list_text}"
                          # Mantener estado AWAITING_EVENT_SELECTION
                  except ValueError: # No envió un número válido y no fue "cancelar"
                      event_list_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(available_events)])
                      response_text = f"Por favor, responde sólo con el *número* del evento que quieres gestionar:\n\n{event_list_text}"
                      # Mantener estado AWAITING_EVENT_SELECTION


        # --- ESTADO: ESPERANDO TIPO DE INVITADO (SOLO VIPs) ---
        elif current_state == STATE_AWAITING_GUEST_TYPE:
             logger.info(f"Procesando mensaje en STATE_AWAITING_GUEST_TYPE para {sender_phone_normalized}")
             # Este estado solo es alcanzable por VIPs que ya seleccionaron evento. Debe haber un evento seleccionado.
             if not selected_event:
                 logger.error(f"Usuario VIP {sender_phone_normalized} en AWAITING_GUEST_TYPE sin evento seleccionado guardado. Reiniciando a INITIAL.")
                 response_text = "Hubo un problema, no recuerdo el evento. Por favor, envía cualquier mensaje para empezar de nuevo."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
             # Permitir "cancelar" en este estado
             elif incoming_msg.lower() in ["cancelar", "salir", "cancel", "exit"]:
                 logger.info(f"Usuario {sender_phone_normalized} canceló la selección de tipo de invitado para {selected_event}.")
                 response_text = f"Selección de tipo cancelada para el evento *{selected_event}*. Puedes enviar cualquier mensaje para empezar de nuevo."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
             else:
                  try:
                      choice_number = int(incoming_msg.strip())
                      if choice_number == 2:  # VIP
                          logger.info(f"Usuario {sender_phone_normalized} eligió añadir tipo VIP para evento {selected_event}.")
                          user_status['state'] = STATE_AWAITING_GUEST_DATA
                          user_status['guest_type'] = 'VIP'
                          response_text = (
                              f"Perfecto, evento seleccionado: *{selected_event}*.\n\n"
                              "Ahora envíame la lista en formato Nombres primero, luego una línea vacía, luego los Emails y finalmente el link de instagram.\n\n"
                              "Ejemplo:\n\n"
                              "Hombres: \n"
                              "Nombre Apellido\n"
                              "Nombre Apellido\n\n" # Línea vacía separadora
                              "email1@ejemplo.com\n"
                              "email2@ejemplo.com\n\n"
                              "link instagram persona 1\n"
                              "link instagram persona 2\n\n"
                              "Mujeres: \n"
                              "Nombre Apellido\n"
                              "Nombre Apellido\n\n" # Línea vacía separadora
                              "email1@ejemplo.com\n"
                              "email2@ejemplo.com\n\n"
                              "link instagram persona 1\n"
                              "link instagram persona 2\n\n"
                              "⚠️ La cantidad de nombres y emails debe coincidir.\n"
                              "Escribe 'cancelar' si quieres cambiar de evento."
                          )
                          user_states[sender_phone_normalized] = user_status # Actualizar estado
                      elif choice_number == 1:  # General
                          logger.info(f"Usuario {sender_phone_normalized} eligió añadir tipo General para evento {selected_event}.")
                          user_status['state'] = STATE_AWAITING_GUEST_DATA
                          user_status['guest_type'] = 'Normal'
                          response_text = (
                              f"Ok, vas a añadir invitados *Generales* para *{selected_event}*.\n\n"
                              "Envíame la lista en formato Nombres primero, luego una línea vacía, y luego los Emails.\n\n"
                              "Ejemplo:\n\n"
                              "Hombres: \n"
                              "Juan Perez\n"
                              "Carlos Lopez\n\n" # Línea vacía separadora
                              "juan.p@ejemplo.com\n"
                              "carlos.l@ejemplo.com\n\n"
                              "Mujeres: \n"
                              "Maria Garcia\n"
                              "Ana Martinez\n\n" # Línea vacía separadora
                              "maria.g@ejemplo.com\n"
                              "ana.m@ejemplo.com\n\n"
                              "⚠️ La cantidad de nombres y emails debe coincidir.\n"
                              "Escribe 'cancelar' para volver."
                          )
                          user_states[sender_phone_normalized] = user_status # Actualizar estado
                      else:  # Número fuera de rango
                          response_text = f"❌ Número '{incoming_msg}' fuera de rango. Responde solo con el numero:\n1) General\n2) VIP"
                          # Mantener estado AWAITING_GUEST_TYPE
                  except ValueError:  # No envió un número válido
                      response_text = f"Por favor, responde solo con el numero:\n1) General\n2) VIP"
                      # Mantener estado AWAITING_GUEST_TYPE
                      user_states[sender_phone_normalized] = user_status # Actualizar estado
                  # Este else no debería existir porque ya está manejado en el except ValueError


        # --- ESTADO: ESPERANDO DATOS DEL INVITADO ---
        elif current_state == STATE_AWAITING_GUEST_DATA:
             logger.info(f"Procesando mensaje en STATE_AWAITING_GUEST_DATA para {sender_phone_normalized}")
             # Debe haber un evento y un tipo (Normal o VIP) seleccionados
             if not selected_event or not selected_guest_type:
                 logger.error(f"Estado AWAITING_GUEST_DATA alcanzado sin evento ({selected_event}) o tipo ({selected_guest_type}) para {sender_phone_normalized}. Reiniciando a INITIAL.")
                 response_text = "Hubo un problema interno, no sé qué evento o tipo procesar. Por favor, envía cualquier mensaje para empezar de nuevo."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
             # Manejar "cancelar" en este estado
             elif incoming_msg.lower() in ["cancelar", "salir", "cancel", "exit"]:
                 logger.info(f"Usuario {sender_phone_normalized} canceló la adición de invitados para {selected_event}.")
                 response_text = f"Operación de añadir invitados cancelada para el evento *{selected_event}*. Puedes enviar cualquier mensaje para elegir otro evento o gestionar uno diferente."
                 user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
             else:
                  # --- Verificar estado del evento antes de procesar invitados ---
                  # Solo permitir registro después del envío automático de QRs a números especiales
                  try:
                      event_qr_sent = sheet_conn.is_event_qr_sent(selected_event)
                      qr_special_phones = sheet_conn.get_qr_special_phones()
                      is_special_number = sender_phone_normalized in qr_special_phones
                      
                      if event_qr_sent and not is_special_number:
                          # El evento ya tuvo envío automático de QRs y este número NO es especial
                          logger.warning(f"Registro bloqueado para número regular {sender_phone_normalized} en evento '{selected_event}' que ya tuvo envío automático de QRs")
                          response_text = f"""⏰ El evento *{selected_event}* ya tuvo su envío automático de códigos QR.

🚫 Los registros de nuevos invitados están cerrados para este evento.

Si necesitas agregar invitados después del envío automático, contacta al administrador para obtener permisos especiales.

Puedes elegir otro evento enviando cualquier mensaje."""
                          user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
                          send_twilio_message(sender_phone_raw, response_text)
                          return jsonify({"status": "success"}), 200
                      
                      elif event_qr_sent and is_special_number:
                          # El evento ya tuvo envío automático pero este es un número especial
                          logger.info(f"Permitiendo registro a número especial {sender_phone_normalized} para evento '{selected_event}' con QRs ya enviados")
                          
                  except Exception as state_check_err:
                      logger.error(f"Error verificando estado del evento '{selected_event}': {state_check_err}")
                      # En caso de error, permitir el registro (comportamiento seguro)
                  
                  # --- Lógica de Procesamiento de Datos ---

                  if selected_guest_type == 'VIP':
                      logger.info(f"Procesando datos invitados VIP para '{selected_event}' de {sender_phone_normalized}")
                      
                      # Usar parser específico VIP con Instagram
                      logger.info("Usando extractor para formato VIP (Nombres -> Emails -> Instagram)...")
                      # parse_vip_guest_list_with_instagram devuelve (lista_invitados, error_info)
                      structured_guests, error_info_parsing = parse_vip_guest_list_with_instagram(incoming_msg)

                      # Verificar si hubo error de formato grave O si la lista parseada está vacía a pesar de haber texto original
                      if not structured_guests:
                          # No hubo invitados válidos parseados. Reportar el error si lo hubo.
                          logger.error(f"La extracción de invitados VIP falló o no encontró invitados válidos para {sender_phone_normalized}. Error info: {error_info_parsing}")
                          # Dar feedback basado en error_info_parsing si existe
                          if error_info_parsing and error_info_parsing.get('error_type') == 'desbalance':
                               response_text = (f"⚠️ Formato incorrecto.\n"
                                                f"Detecté un desbalance en la categoría '{error_info_parsing.get('category', 'desconocida')}':\n"
                                                f"• {error_info_parsing.get('names_count', 'N/A')} nombres\n"
                                                f"• {error_info_parsing.get('emails_count', 'N/A')} emails\n"
                                                f"• {error_info_parsing.get('instagrams_count', 'N/A')} links de instagram\n\n"
                                                f"La cantidad debe ser la misma para nombres, emails e instagram *en cada categoría con datos*. Revisa tu lista e intenta de nuevo o 'cancelar'.")
                          elif error_info_parsing and error_info_parsing.get('error_type') in ['no_valid_categories', 'empty_message', 'incomplete_category', 'no_valid_pairs']:
                               response_text = ("⚠️ No pude encontrar nombres, emails e Instagram válidos en el formato esperado (Nombres -> Emails -> Instagram separados por líneas vacías, opcionalmente por categorías).\n"
                                                "Revisa el ejemplo e intenta de nuevo o escribe 'cancelar'.")
                          # Si no hubo un error_info_parsing específico pero la lista parseada estaba vacía, es un error de datos.
                          elif error_info_parsing is None and incoming_msg.strip(): # Asegurarse que el mensaje original no estaba vacío
                               response_text = ("⚠️ No encontré invitados con nombre, email e Instagram válidos en tu lista. Revisa el formato y los datos.\n"
                                                "Asegúrate que sigue el formato Nombres -> Emails -> Instagram (separados por líneas vacías) y que cada nombre tiene un email e Instagram.\n"
                                                "Intenta de nuevo o escribe 'cancelar'.")
                          else: # Fallback genérico si no se pudo determinar el error específico
                               response_text = ("⚠️ No pude procesar tu lista. Asegúrate que sigue el formato Nombres -> Emails -> Instagram (separados por líneas vacías).\n"
                                                "Intenta de nuevo o escribe 'cancelar'.")
                          
                          # Mantener estado AWAITING_GUEST_DATA para reintento si hubo un problema de parseo/datos
                          # Si el error_info era 'empty_message', es mejor resetear.
                          if error_info_parsing and error_info_parsing.get('error_type') == 'empty_message':
                              user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}

                      else: # Lista VIP parseada correctamente (hay al menos 1 invitado válido)
                          # Obtener el nombre del PR para la columna 'PR' en la hoja VIP
                          pr_name = sender_phone_normalized # Fallback
                          try:
                              pr_name_map = sheet_conn.get_phone_pr_mapping()
                              if pr_name_map:
                                   pr_name_found = pr_name_map.get(sender_phone_normalized)
                                   if pr_name_found: pr_name = pr_name_found
                                   else: logger.warning(f"No se encontró PR Name para {sender_phone_normalized}. Usando número.")
                              else:
                                   logger.warning("Mapeo PR no disponible. Usando número.")
                          except Exception as map_err:
                              logger.error(f"Error buscando nombre PR: {map_err}")

                          # Obtener o crear hoja unificada para este evento
                          unified_event_sheet = get_or_create_unified_event_sheet(sheet_conn, selected_event)
                          
                          if unified_event_sheet:
                              # Usar la función unificada para guardar invitados VIP
                              added_count = add_guests_to_unified_sheet(unified_event_sheet, structured_guests, pr_name, 'VIP', sheet_conn)
                          else:
                              logger.error(f"No se pudo crear/obtener hoja unificada para evento '{selected_event}'")
                              added_count = 0

                          if added_count > 0:
                              response_text = f"✅ ¡Éxito! Se anotaron *{added_count}* invitado(s) VIP para el evento *{selected_event}*."
                              # Resetear estado después de éxito
                              user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
                          elif added_count == -1: # add_vip_guests_to_sheet devolvió -1 (hubo items pero todos inválidos)
                               response_text = f"⚠️ Intenté anotar invitados VIP para *{selected_event}*, pero no encontré datos válidos (ej. email o nombre faltante) en tu lista. Revisa el formato y los datos. Intenta de nuevo o escribe 'cancelar'."
                               # Mantener estado para reintento
                          else: # added_count == 0 (Error interno en add_vip_guests_to_sheet o no se añadieron filas)
                               response_text = f"❌ Hubo un error al guardar los invitados VIP en la hoja. Por favor, intenta de nuevo más tarde o contacta al administrador."
                               # Resetear por seguridad en caso de error de escritura
                               user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}


                  elif selected_guest_type == 'Normal':
                      logger.info(f"Procesando datos invitados Normales para '{selected_event}' de {sender_phone_normalized}")

                      # Obtener la hoja unificada del evento
                      unified_event_sheet = get_or_create_unified_event_sheet(sheet_conn, selected_event)
                      if not unified_event_sheet:
                          logger.error(f"No se pudo obtener o crear la hoja unificada para el evento '{selected_event}'.")
                          response_text = f"❌ Error: No se pudo acceder a la hoja para el evento '{selected_event}'. Contacta al administrador."
                          user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear
                      else:
                           # Inicializar error_info localmente ANTES de parsear
                           error_info_parsing = None
                           structured_guests = None

                           # Usar el extractor para formato dividido Nombres->Emails (incluye categorías opcionales)
                           logger.info("Usando extractor para formato Normal (Nombres -> Emails)...")
                           data_lines_list = incoming_msg.split('\n')
                           # extract_guests_from_split_format devuelve (lista_invitados, error_info)
                           structured_guests, error_info_parsing = extract_guests_from_split_format(data_lines_list)


                           # Procesar resultado del parseo
                           if not structured_guests: # La lista parseada está vacía
                                # No hubo invitados válidos parseados. Reportar el error si lo hubo.
                               logger.error(f"La extracción de invitados normales falló o no encontró invitados válidos para {sender_phone_normalized}. Error info: {error_info_parsing}")
                               # Dar feedback basado en error_info_parsing si existe
                               if error_info_parsing and error_info_parsing.get('error_type') == 'desbalance':
                                    response_text = (f"⚠️ Formato incorrecto.\n"
                                                     f"Detecté un desbalance en la categoría '{error_info_parsing.get('category', 'desconocida')}':\n"
                                                     f"• {error_info_parsing.get('names_count', 'N/A')} nombres\n"
                                                     f"• {error_info_parsing.get('emails_count', 'N/A')} emails\n\n"
                                                     f"La cantidad debe ser la misma *en cada categoría con datos*. Revisa tu lista, separa nombres y emails con una línea vacía, e intenta de nuevo o 'cancelar'.")
                               elif error_info_parsing and error_info_parsing.get('error_type') in ['no_valid_categories', 'empty_message', 'incomplete_category', 'no_valid_pairs']:
                                    response_text = ("⚠️ No pude encontrar nombres y emails válidos en el formato esperado (Nombres -> Emails separados por línea vacía, opcionalmente por categorías).\n"
                                                     "Revisa el ejemplo e intenta de nuevo o escribe 'cancelar'.")
                               # Si no hubo un error_info_parsing específico pero la lista parseada estaba vacía, es un error de datos.
                               elif error_info_parsing is None and incoming_msg.strip(): # Asegurarse que el mensaje original no estaba vacío
                                    response_text = ("⚠️ No encontré invitados con nombre y email válidos en tu lista. Revisa el formato y los datos.\n"
                                                     "Asegúrate que sigue el formato Nombres -> Emails (separados por línea vacía) y que cada nombre tiene un email.\n"
                                                     "Intenta de nuevo o escribe 'cancelar'.")
                               else: # Fallback genérico si no se pudo determinar el error específico
                                     response_text = ("⚠️ No pude procesar tu lista. Asegúrate que sigue el formato Nombres -> Emails (separados por línea vacía).\n"
                                                      "Intenta de nuevo o escribe 'cancelar'.")

                               # Mantener estado AWAITING_GUEST_DATA para reintento si hubo un problema de parseo/datos
                               # Si el error_info era 'empty_message', es mejor resetear.
                               if error_info_parsing and error_info_parsing.get('error_type') == 'empty_message':
                                   user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}


                           else: # Lista Normal parseada correctamente (hay al menos 1 invitado válido)
                                # --- Obtener nombre del PR para invitados normales ---
                                pr_name = sender_phone_normalized # Fallback
                                try:
                                    phone_to_pr_map = sheet_conn.get_phone_pr_mapping()
                                    if phone_to_pr_map:
                                         pr_name_found = phone_to_pr_map.get(sender_phone_normalized)
                                         if pr_name_found: pr_name = pr_name_found
                                         else: logger.warning(f"No se encontró PR Name Normal mapeado para {sender_phone_normalized}. Usando número.")
                                    else:
                                         logger.warning("Mapeo PR Normal no disponible. Usando número.")
                                except Exception as e:
                                    logger.error(f"Error al buscar PR Normal: {e}")

                                # --- Usar función unificada para guardar invitados Normal ---
                                added_count = add_guests_to_unified_sheet(unified_event_sheet, structured_guests, pr_name, 'Normal', sheet_conn)

                                # --- Procesar resultado ---
                                if added_count > 0:
                                    response_text = f"✅ ¡Éxito! Se anotaron *{added_count}* invitado(s) Generales para el evento *{selected_event}*."
                                    # Resetear estado después de éxito
                                    user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}
                                elif added_count == -1:
                                    response_text = f"⚠️ Intenté anotar invitados Generales para *{selected_event}*, pero no encontré datos válidos (ej. email o nombre faltante) en tu lista. Revisa el formato y los datos. Intenta de nuevo o escribe 'cancelar'."
                                    # Mantener estado para reintento
                                else: # added_count == 0
                                    response_text = f"❌ Hubo un error al guardar los invitados Generales en la hoja. Por favor, intenta de nuevo más tarde o contacta al administrador."
                                    # Resetear por seguridad en caso de error de escritura
                                    user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []}



                  else: # Tipo de invitado desconocido en estado (no debería pasar)
                      logger.error(f"Estado AWAITING_GUEST_DATA con guest_type inválido o nulo: {selected_guest_type} para {sender_phone_normalized}")
                      response_text = "Hubo un error con tu selección de tipo de invitado. Por favor, envía cualquier mensaje para empezar de nuevo."
                      user_states[sender_phone_normalized] = {'state': STATE_INITIAL, 'event': None, 'guest_type': None, 'available_events': []} # Resetear


        # --- Estado Desconocido ---
        else:
            logger.warning(f"Estado no reconocido '{current_state}' para {sender_phone_normalized}. Reiniciando a estado inicial.")
            response_text = "No estoy seguro de qué estábamos hablando. 🤔 Por favor, envía cualquier mensaje para comenzar de nuevo."
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
            # Esto debería ser raro con la lógica de fallback en cada estado.
            logger.warning(f"No se generó texto de respuesta para enviar al final del flujo (Estado: {current_state}). Esto es inesperado.")
            # Enviar un mensaje genérico de fallback para que el usuario no quede esperando.
            fallback_message = "Lo siento, no pude procesar tu mensaje. Ocurrió un problema inesperado. Por favor, envía cualquier mensaje para intentar empezar de nuevo."
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

@app.route('/difusion', methods=['POST'])
def broadcast_message():
    """
    Endpoint para enviar un mensaje de difusión usando una plantilla de Twilio.
    Espera un JSON con:
    {
        "template_sid": "HX...",
        "target_group": "all_prs" | "vips",
        "template_variables": { "1": "valor1", ... } // opcional
    }
    Requiere cabecera Authorization: Bearer <API_TOKEN>
    """
    # Verificar autenticación
    auth_header = request.headers.get('Authorization')
    expected_token = os.getenv('BROADCAST_API_TOKEN')
    
    if not expected_token:
        logger.warning("Variable BROADCAST_API_TOKEN no configurada. Endpoint de difusión deshabilitado.")
        return jsonify({"status": "error", "message": "Servicio no disponible"}), 503
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({"status": "error", "message": "Token de autorización requerido"}), 401
    
    token = auth_header.split(' ', 1)[1] if len(auth_header.split(' ')) > 1 else ''
    if token != expected_token:
        return jsonify({"status": "error", "message": "Token de autorización inválido"}), 401

    if not request.is_json:
        return jsonify({"status": "error", "message": "Request must be JSON"}), 400

    data = request.get_json()
    template_sid = data.get('template_sid')
    target_group = data.get('target_group')
    template_variables = data.get('template_variables') # opcional

    if not template_sid or not target_group:
        return jsonify({"status": "error", "message": "Faltan parámetros 'template_sid' o 'target_group'"}), 400

    try:
        sheet_conn = SheetsConnection()
        phone_numbers = set() # Usar un set para evitar duplicados

        if target_group == 'all_prs':
            phone_numbers = sheet_conn.get_authorized_phones()
            logger.info(f"Objetivo 'all_prs' seleccionado. Se enviará a {len(phone_numbers)} números.")
        elif target_group == 'vips':
            phone_numbers = sheet_conn.get_vip_phones()
            logger.info(f"Objetivo 'vips' seleccionado. Se enviará a {len(phone_numbers)} números.")
        else:
            return jsonify({"status": "error", "message": f"Valor de 'target_group' inválido: '{target_group}'. Usa 'all_prs' o 'vips'."}), 400

        if not phone_numbers:
            logger.info(f"No se encontraron números de teléfono para el grupo '{target_group}'.")
            return jsonify({"status": "success", "message": f"No se encontraron números para el grupo '{target_group}'."}), 200

        # Respuesta inmediata para evitar timeout
        total_phones = len(phone_numbers)
        logger.info(f"Iniciando difusión a {total_phones} números en background...")
        
        # Procesar en background con threading
        import threading
        
        def send_broadcast_async():
            results = {"sent": [], "failed": []}
            current_phone = 0
            
            for phone in phone_numbers:
                current_phone += 1
                try:
                    # Los números ya vienen normalizados de las funciones get_..._phones()
                    result = send_templated_message(phone, template_sid, template_variables)
                    if result.get("success"):
                        results["sent"].append({"phone": phone, "sid": result.get("sid")})
                        logger.info(f"Mensaje enviado {current_phone}/{total_phones} a {phone}")
                    else:
                        results["failed"].append({"phone": phone, "error": result.get("error")})
                        logger.warning(f"Fallo al enviar {current_phone}/{total_phones} a {phone}: {result.get('error')}")
                except Exception as e:
                    results["failed"].append({"phone": phone, "error": str(e)})
                    logger.error(f"Error crítico al enviar a {phone}: {e}")
                
                # Rate limiting: pausa de 1 segundo entre envíos (excepto el último)
                if current_phone < total_phones:
                    time.sleep(1)
            
            total_sent = len(results["sent"])
            total_failed = len(results["failed"])
            logger.info(f"Difusión completada. Enviados: {total_sent}, Fallidos: {total_failed}")
        
        # Iniciar proceso en background
        thread = threading.Thread(target=send_broadcast_async)
        thread.daemon = True
        thread.start()
        
        # Respuesta inmediata
        return jsonify({
            "status": "started",
            "message": f"Difusión iniciada para {total_phones} números. El proceso continuará en background.",
            "total_recipients": total_phones
        }), 200

    except Exception as e:
        logger.error(f"Error CRÍTICO en el endpoint de difusión: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Ocurrió un error interno en el servidor."}), 500


@app.route('/send_qrs', methods=['POST'])
def send_qrs():
    """
    Endpoint para enviar códigos QR automáticamente via PlanOut.com.ar.
    Espera un JSON con:
    {
        "pr_phone": "+54911XXXXXXXX",  // Número del PR (opcional, si no se envía procesa todos)
        "event_filter": "EventName",   // Filtrar por evento específico (opcional)
        "dry_run": false              // Si es true, solo simula el proceso sin enviar (opcional)
    }
    Requiere cabecera Authorization: Bearer <API_TOKEN>
    """
    # Verificar autenticación
    auth_header = request.headers.get('Authorization')
    expected_token = os.getenv('BROADCAST_API_TOKEN')  # Usar el mismo token que difusión
    
    if not expected_token:
        logger.warning("Variable BROADCAST_API_TOKEN no configurada. Endpoint de QRs deshabilitado.")
        return jsonify({"status": "error", "message": "Servicio no disponible"}), 503
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({"status": "error", "message": "Token de autorización requerido"}), 401
    
    token = auth_header.split(' ', 1)[1] if len(auth_header.split(' ')) > 1 else ''
    if token != expected_token:
        return jsonify({"status": "error", "message": "Token de autorización inválido"}), 401

    if not request.is_json:
        return jsonify({"status": "error", "message": "Request must be JSON"}), 400

    data = request.get_json()
    pr_phone = data.get('pr_phone')  # Opcional
    event_filter = data.get('event_filter')  # Opcional
    dry_run = data.get('dry_run', False)  # Por defecto False

    try:
        sheet_conn = SheetsConnection()
        
        # Obtener invitados pendientes de QR
        if pr_phone:
            # Normalizar número de teléfono
            pr_phone_normalized = re.sub(r'\D', '', pr_phone)
            logger.info(f"Procesando QRs para PR específico: {pr_phone_normalized}")
            pending_guests = get_pending_qr_guests_by_pr(sheet_conn, pr_phone_normalized, event_filter)
        else:
            logger.info("Procesando QRs para todos los PRs")
            pending_guests = get_all_pending_qr_guests(sheet_conn, event_filter)

        if not pending_guests:
            return jsonify({
                "status": "success", 
                "message": "No se encontraron invitados pendientes de recibir códigos QR.",
                "total_guests": 0
            }), 200

        total_guests = len(pending_guests)
        logger.info(f"Encontrados {total_guests} invitados pendientes de QR")

        if dry_run:
            return jsonify({
                "status": "success",
                "message": f"Simulación: Se procesarían {total_guests} invitados.",
                "total_guests": total_guests,
                "guests_preview": pending_guests[:5],  # Mostrar primeros 5
                "dry_run": True
            }), 200

        # Respuesta inmediata para evitar timeout
        logger.info(f"Iniciando proceso de QRs para {total_guests} invitados en background...")
        
        # Procesar en background con threading
        import threading
        
        def process_qrs_async():
            try:
                logger.info("🤖 INICIO PROCESO QR AUTOMÁTICO (endpoint /send_qrs)")
                logger.info(f"📊 Total invitados pendientes: {len(pending_guests)}")
                
                # Log detallado de algunos invitados
                for i, guest in enumerate(pending_guests[:3], 1):  # Mostrar primeros 3
                    logger.info(f"👤 Invitado {i}: {guest.get('name', 'Sin nombre')} - {guest.get('email', 'Sin email')} - Evento: {guest.get('event', 'Sin evento')}")
                
                logger.info("🌐 Iniciando PlanOutAutomation desde endpoint...")
                # Usar la automatización de PlanOut
                with PlanOutAutomation() as automation:
                    logger.info("🔐 Ejecutando full_automation_workflow desde /send_qrs...")
                    result = automation.full_automation_workflow(pending_guests)
                    logger.info(f"✅ Resultado de automatización (endpoint): {result}")
                
                if result.get("success"):
                    # Actualizar Google Sheets marcando QRs como enviados
                    update_qr_sent_status(sheet_conn, pending_guests, True)
                    
                    # Marcar eventos como que ya tuvieron envío automático de QRs
                    events_processed = set()
                    for guest in pending_guests:
                        event_name = guest.get('event')
                        if event_name and event_name not in events_processed:
                            if sheet_conn.mark_event_qr_sent(event_name):
                                logger.info(f"Evento '{event_name}' marcado como QR automático enviado")
                                events_processed.add(event_name)
                            else:
                                logger.warning(f"No se pudo marcar evento '{event_name}' como QR enviado")
                    
                    logger.info(f"Proceso de QRs completado exitosamente para {total_guests} invitados")
                else:
                    logger.error(f"Error en proceso de QRs: {result.get('error', 'Error desconocido')}")
                    
            except Exception as e:
                logger.error(f"Error crítico en proceso de QRs: {e}")
                logger.error(traceback.format_exc())
        
        # Iniciar proceso en background
        thread = threading.Thread(target=process_qrs_async)
        thread.daemon = True
        thread.start()
        
        # Respuesta inmediata
        return jsonify({
            "status": "started",
            "message": f"Proceso de QRs iniciado para {total_guests} invitados. El proceso continuará en background.",
            "total_guests": total_guests,
            "pr_phone": pr_phone if pr_phone else "todos",
            "event_filter": event_filter if event_filter else "todos los eventos"
        }), 200

    except Exception as e:
        logger.error(f"Error CRÍTICO en el endpoint de QRs: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Ocurrió un error interno en el servidor."}), 500