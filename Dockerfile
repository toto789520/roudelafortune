# Base image
FROM python:3.11-slim

# Répertoire de travail
WORKDIR /app

# Installation des dépendances système
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copie UNIQUEMENT requirements.txt d'abord (cache pip)
COPY requirements.txt .

# Installation des dépendances Python
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copie TOUT le reste (y compris uwsgi.ini et app.py)
COPY . .

# Port par défaut, modifiable au build (docker build --build-arg PORT=...) ou à l'exécution (-e PORT=...)
ARG PORT=8000
ENV PORT=${PORT}

# Port exposé
EXPOSE ${PORT}

# Commande de lancement
CMD ["sh", "-c", "export PORT=${PORT:-8000}; exec uwsgi --ini uwsgi.ini"]
