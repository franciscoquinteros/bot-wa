FROM python:3.9-slim

# Instalar dependencias para mysqlclient
RUN apt-get update && apt-get install -y \
    pkg-config \
    default-libmysqlclient-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Reemplaza "main.py" con el nombre de tu archivo principal
CMD gunicorn --bind 0.0.0.0:8080 bot_whatsapp:app