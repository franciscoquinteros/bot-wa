FROM python:3.9-slim

# Instalar dependencias del sistema necesarias para Playwright y mysqlclient
RUN apt-get update && apt-get install -y \
    pkg-config \
    default-libmysqlclient-dev \
    build-essential \
    # Dependencias para Playwright/Chromium
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar browsers de Playwright con verificación
RUN playwright install chromium
RUN ls -la /root/.cache/ms-playwright/
RUN find /root/.cache/ms-playwright/ -name "chrome*" -type f

COPY . .

# Configurar variables de entorno para Playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV DISPLAY=:99
ENV DEBIAN_FRONTEND=noninteractive

# Reemplaza "main.py" con el nombre de tu archivo principal
CMD gunicorn --bind 0.0.0.0:8080 bot_whatsapp:app