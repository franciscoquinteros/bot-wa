# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a WhatsApp bot built with Flask that manages guest lists for events. The bot integrates with Google Sheets for data storage and uses Twilio for WhatsApp messaging. Key features include:

- Guest registration and management through WhatsApp conversations
- Google Sheets integration for persistent storage
- Event invitation system with VIP and general guest categories
- Template-based messaging through Twilio
- Multi-state conversation handling for interactive workflows
- **Automated QR code generation and sending via PlanOut.com.ar integration**

## Architecture

### Core Components

- **bot_whatsapp.py**: Main Flask application containing all bot logic, message handling, Google Sheets integration, and Twilio messaging
- **qr_automation.py**: Automated QR code generation and sending module using Playwright web automation for PlanOut.com.ar
- **SheetsConnection class**: Handles Google Sheets API authentication and operations using service account credentials
- **State Management**: User conversation states are managed in-memory using the `user_states` dictionary

### Key Integration Points

- **Google Sheets API**: Uses `gspread` library with OAuth2 service account authentication via `credentials.json`
- **Twilio WhatsApp API**: Sends messages using both freeform text and pre-approved templates
- **PlanOut.com.ar Integration**: Automated web scraping and form submission using Playwright for QR code generation
- **Environment Variables**: Critical configuration through `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`, `GOOGLE_SHEET_NAME`, `PLANOUT_USERNAME`, `PLANOUT_PASSWORD`

### Message Processing Flow

1. Incoming WhatsApp messages trigger `/webhook` endpoint
2. User state determines conversation context and expected input
3. Message parsing extracts guest information (names, emails, categories)
4. Data validation and Google Sheets updates
5. Response generation and Twilio message sending

## Development Commands

### Running the Application

**Production (Gunicorn):**
```bash
gunicorn --bind 0.0.0.0:8080 bot_whatsapp:app
```

**Development:**
```bash
python bot_whatsapp.py
# Note: No explicit if __name__ == "__main__" block - Flask app runs via gunicorn
```

### Dependencies
```bash
pip install -r requirements.txt
```

### Testing Endpoints

- `GET /health` - Health check endpoint
- `GET /test_sheet` - Tests Google Sheets connectivity and write permissions
- `POST /broadcast` - Send templated messages to multiple recipients
- `POST /send_qrs` - Automated QR code generation and sending via PlanOut.com.ar

## Deployment Configuration

### Fly.io (Primary)
- Configuration in `fly.toml`
- App name: `bot-wa-nameless-shadow-952`
- Memory: 1GB, Shared CPU
- Region: `eze` (South America)

### Render (Alternative)
- Configuration in `render.yaml`
- Service name: `whatsapp-guest-bot`
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn bot_whatsapp:app`

### Docker
```bash
# Build
docker build -t whatsapp-bot .

# Run
docker run -p 8080:8080 whatsapp-bot
```

## Environment Setup

Required environment variables:
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN` 
- `TWILIO_WHATSAPP_NUMBER`
- `GOOGLE_SHEET_NAME`
- `GOOGLE_CREDENTIALS` (JSON string for service account)
- `BROADCAST_API_TOKEN` (for API endpoints authentication)
- `PLANOUT_USERNAME` (default: AntoSVG)
- `PLANOUT_PASSWORD` (default: AntoSVG-987\)
- `PLANOUT_HEADLESS` (default: true, set to false for debugging)

## Guest Data Format

The bot expects guest information in these formats:
- Simple: `Juan Pérez - juan@ejemplo.com`
- Categorized:
  ```
  Hombres:
  Juan Pérez - juan@ejemplo.com
  
  Mujeres:
  María López - maria@ejemplo.com
  ```

## QR Code Automation

### Overview
The system includes automated QR code generation and sending through PlanOut.com.ar integration. This feature:
- Identifies guests who have received invitations (`Enviado: true`) but haven't received QR codes yet
- Uploads guest data to PlanOut.com.ar via web automation
- Generates and sends QR codes automatically
- Updates Google Sheets with QR sending status
- **Special numbers can trigger QR sending manually and continue registering guests after automatic QR dispatch**

### API Usage

**Endpoint:** `POST /send_qrs`

**Headers:**
```
Authorization: Bearer <BROADCAST_API_TOKEN>
Content-Type: application/json
```

**Request Body:**
```json
{
  "pr_phone": "+54911XXXXXXXX",  // Optional: specific PR phone
  "event_filter": "EventName",   // Optional: filter by event
  "dry_run": false              // Optional: simulation mode
}
```

**Example Requests:**

Process all pending QRs:
```bash
curl -X POST https://your-domain/send_qrs \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Process QRs for specific PR:
```bash
curl -X POST https://your-domain/send_qrs \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pr_phone": "+549111234567"}'
```

Dry run (simulation):
```bash
curl -X POST https://your-domain/send_qrs \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

### Google Sheets Integration

The system adds a `QR_ENVIADO` column to event sheets to track QR sending status:
- `true`: QR code has been sent
- `false` or empty: QR code pending

Only guests with `Enviado: true` and `QR_ENVIADO: false` will be processed.

### PlanOut.com.ar Automation

The `qr_automation.py` module handles:
1. **Login automation**: Uses credentials to log into PlanOut backoffice
2. **File upload**: Converts guest data to CSV and uploads to PlanOut
3. **QR generation**: Triggers automated QR generation and sending
4. **Status tracking**: Monitors process completion and reports results

### Troubleshooting QR Automation

**Common Issues:**
- Login failures: Check `PLANOUT_USERNAME` and `PLANOUT_PASSWORD`
- Upload errors: Verify guest data format (name, email, category)
- Timeout issues: PlanOut.com.ar may be slow, increase timeouts if needed
- Browser issues: Set `PLANOUT_HEADLESS=false` for debugging

**Debugging:**
```python
# Test QR automation independently
python qr_automation.py
```

## Common Development Tasks

- **Add new message handlers**: Extend the `whatsapp_reply()` function in bot_whatsapp.py:3055
- **Modify guest parsing**: Update `parse_vip_guest_list()` or `extract_guests_manually()` functions
- **Add new conversation states**: Define new state constants and handlers in the main message processing logic
- **Extend Google Sheets operations**: Modify `SheetsConnection` class methods
- **Template message changes**: Update `send_templated_message()` function and Twilio template configurations
- **QR automation modifications**: Update `qr_automation.py` module for PlanOut.com.ar changes
- **QR endpoint enhancements**: Modify `/send_qrs` endpoint in bot_whatsapp.py:3696 for new features

## Special Numbers System

### Overview
The bot implements a special numbers system that provides additional privileges for designated phone numbers after automatic QR sending has been triggered.

### Key Features

#### **Event State Control**
- **Estado_Eventos sheet**: Tracks when automatic QR sending occurs for each event
- **Automatic marking**: Events are marked as "QR sent" when `/send_qrs` is executed
- **State validation**: Before allowing guest registration, the bot checks event QR status

#### **Special Number Privileges**
**Before automatic QR dispatch (typically 8pm):**
- ✅ All authorized numbers can register guests normally

**After automatic QR dispatch:**
- ❌ **Regular numbers**: Blocked from registering new guests
- ✅ **Special numbers**: Can continue registering guests without restrictions

#### **Manual QR Commands**
Special numbers can trigger QR generation manually using these commands:
- `enviar qr` / `enviar qrs`
- `send qr`
- `qr send`
- `procesar qr`
- `mandar qr`

### Configuration

#### **QR_Especiales Sheet**
Add phone numbers (normalized format) to grant special privileges:
```
Telefono
5491164855744
5491198765432
```

#### **Estado_Eventos Sheet**
Automatically managed, tracks:
- `Evento`: Event name
- `QR_Automatico_Enviado`: Boolean status
- `Fecha_Envio`: Date of QR sending
- `Hora_Envio`: Time of QR sending

### Bot Responses

#### **Special Number QR Command Examples**

**User sends:**
```
hombres:
joaquin gomez

joacogomez@gmail.com

enviar qr
```

**Bot processes as two separate actions:**

1. **Guest registration:**
```
✅ ¡Éxito! Se anotó 1 invitado General para el evento [EventoSeleccionado].
```

2. **QR command:**
```
🚀 Iniciando envío de códigos QR para 5 invitados pendientes.

El proceso se ejecutará en segundo plano y puede tomar unos minutos. 
Te notificaremos cuando esté completo.
```

**Completion notification:**
```
✅ ¡Códigos QR enviados exitosamente!

📊 Procesados: 5 invitados
⏰ Completado en: 20:45:32

Los invitados recibirán sus códigos QR por email.
```

#### **Access Control Messages**

**Regular number after QR dispatch:**
```
⏰ El evento [EventName] ya tuvo su envío automático de códigos QR.

🚫 Los registros de nuevos invitados están cerrados para este evento.

Si necesitas agregar invitados después del envío automático, 
contacta al administrador para obtener permisos especiales.
```

**Unauthorized QR command:**
```
🚫 Lo siento, tu número no tiene permisos para enviar comandos de QR.

Solo los números especiales configurados pueden usar esta función. 
Si necesitas acceso, contacta al administrador.
```

**No pending guests:**
```
📋 No tienes invitados pendientes de recibir códigos QR en este momento.

Los códigos QR solo se envían a invitados que ya tienen la invitación 
marcada como "Enviado: ✅" pero aún no han recibido su QR.
```

#### **Enhanced Help for Special Numbers**
```
🚀 Funciones especiales (disponibles para tu número):
• Escribe "enviar qr" para procesar códigos QR
• Privilegio especial: Puedes seguir registrando invitados 
  DESPUÉS de que se dispare el envío automático de QRs (8pm).
```

### Implementation Details

#### **Core Methods**
- `get_qr_special_phones()`: Retrieves special phone numbers with caching
- `is_event_qr_sent(event_name)`: Checks if event had automatic QR dispatch
- `mark_event_qr_sent(event_name)`: Marks event as QR dispatched
- `get_event_qr_states()`: Gets all event QR states with caching

#### **Workflow Integration**
1. **QR Command Detection**: Pattern matching for QR commands
2. **Permission Validation**: Check against QR_Especiales sheet
3. **Guest Processing**: Find pending guests (Enviado: true, QR_ENVIADO: false)
4. **PlanOut Automation**: Execute QR generation workflow
5. **State Updates**: Mark events as QR dispatched and update guest status

### Benefits
- **Flexible Access Control**: Specific numbers can work after hours
- **Event-Specific Tracking**: Granular control per event
- **Manual QR Triggers**: On-demand QR generation for special numbers
- **Clear User Feedback**: Informative messages for all scenarios
- **Secure Permissions**: Unauthorized numbers are properly blocked