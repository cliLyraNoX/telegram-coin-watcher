# Basis-Image mit Python 3.11
FROM python:3.11-slim

# Setze Umgebungsvariablen
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Arbeitsverzeichnis erstellen
WORKDIR /app

# Systemabhängigkeiten installieren
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Abhängigkeiten kopieren und installieren
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Anwendungscode kopieren
COPY bot.py .

# Ports freigeben (optional, da Telegram-Bot keine eingehenden Verbindungen benötigt)
# EXPOSE 8443

# Startbefehl
CMD ["python", "bot.py"]
