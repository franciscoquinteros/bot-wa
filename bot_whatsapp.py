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
            creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
            self.client = gspread.authorize(creds)
            self.sheet = self.client.open("n8n sheet").sheet1
            logger.info("Conexión con Google Sheets establecida con éxito")
        except Exception as e:
            logger.error(f"Error al conectar con Google Sheets: {e}")
            raise
        
    def get_sheet(self):
        return self.sheet

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
    
    # Extraer invitados
    lines = message.split('\n')
    category = None
    guests = []
    valid_categories = ['hombres', 'mujeres', 'niños', 'adultos', 'familia']
    
    for line in lines:
        line = line.strip()
        
        # Verificar si la línea es una categoría
        found_category = False
        for cat in valid_categories:
            if line.lower() == cat or line.lower() == cat[:-1]:  # Singular o plural
                category = line.capitalize()
                found_category = True
                break
        
        if found_category:
            continue
            
        # Si tenemos una categoría y la línea tiene contenido, es un invitado
        if line and category:
            guests.append((category, line))
    
    return {
        'command_type': 'add_guests' if guests else 'unknown',
        'data': guests
    }

def add_guests_to_sheet(sheet, guests, phone_number):
    """Agrega invitados a la hoja con información adicional"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Verificar si la hoja tiene los encabezados correctos
        headers = sheet.row_values(1)
        if not headers or len(headers) < 5:
            sheet.update('A1:E1', [['ID', 'Categoría', 'Nombre', 'Teléfono', 'Fecha']])
        
        rows_to_add = []
        for category, name in guests:
            guest_id = str(uuid.uuid4())[:8]  # ID único corto
            rows_to_add.append([guest_id, category, name, phone_number, timestamp])
        
        if rows_to_add:
            sheet.append_rows(rows_to_add)
            logger.info(f"Agregados {len(rows_to_add)} invitados para el teléfono {phone_number}")
        
        return len(rows_to_add)
    except Exception as e:
        logger.error(f"Error al agregar invitados: {e}")
        return 0

def count_guests(sheet, phone_number=None):
    """Cuenta invitados, opcionalmente filtrados por número de teléfono"""
    try:
        all_data = sheet.get_all_records()
        
        if phone_number:
            filtered_data = [row for row in all_data if str(row.get('Teléfono')) == phone_number]
        else:
            filtered_data = all_data
        
        # Contar por categoría
        categories = {}
        for row in filtered_data:
            category = row.get('Categoría', 'Sin categoría')
            categories[category] = categories.get(category, 0) + 1
        
        # Agregar total
        categories['Total'] = len(filtered_data)
        
        logger.info(f"Conteo completado para {phone_number}: {categories}")
        return categories
    except Exception as e:
        logger.error(f"Error al contar invitados: {e}")
        return {'Total': 0}

def generate_response(command, result, phone_number=None):
    """Genera respuestas personalizadas basadas en el comando y el resultado"""
    if command == 'count':
        if not result or result.get('Total', 0) == 0:
            return "No tienes invitados registrados aún."
        
        response = "📋 Resumen de invitados:\n\n"
        for category, count in result.items():
            if category != 'Total':
                response += f"- {category}: {count}\n"
        
        response += f"\nTotal: {result.get('Total', 0)} invitados"
        return response
        
    elif command == 'add_guests':
        count = result
        if count == 0:
            return "No se pudieron registrar invitados. Verifica el formato."
        elif count == 1:
            return "✅ Se ha registrado 1 invitado correctamente."
        else:
            return f"✅ Se han registrado {count} invitados correctamente."
            
    elif command == 'help':
        return """📱 *Ayuda del sistema de invitados*

Para agregar invitados:
```
Hombres
Juan Pérez
Pedro Gómez

Mujeres
María López
Ana García
```

Para consultar:
- Escribe "cuántos invitados" para ver el total
- También puedes escribir "lista de invitados"

Categorías disponibles:
- Hombres
- Mujeres
- Niños
- Adultos
- Familia"""
    
    else:
        return """No pude entender tu mensaje. Puedes:

- Agregar invitados usando categorías (Hombres, Mujeres, etc.)
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
        
        # Procesar el mensaje
        parsed = parse_message(incoming_msg)
        command_type = parsed['command_type']
        data = parsed['data']
        
        # Obtener la conexión a la hoja
        sheet_conn = SheetsConnection()
        sheet = sheet_conn.get_sheet()
        
        # Ejecutar el comando correspondiente
        result = None
        if command_type == 'add_guests':
            result = add_guests_to_sheet(sheet, data, sender_phone)
        elif command_type == 'count':
            result = count_guests(sheet, sender_phone)
        
        # Generar respuesta
        response_text = generate_response(command_type, result, sender_phone)
        
        # Enviar respuesta
        resp = MessagingResponse()
        msg = resp.message()
        msg.body(response_text)
        
        logger.info(f"Respuesta enviada a {sender_phone}")
        return str(resp)
        
    except Exception as e:
        logger.error(f"Error en el procesamiento del mensaje: {e}")
        
        # Respuesta de error
        resp = MessagingResponse()
        msg = resp.message()
        msg.body("Lo siento, hubo un error en el sistema. Inténtalo más tarde.")
        return str(resp)

@app.route('/health', methods=['GET'])
def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)