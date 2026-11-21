# Základný obraz s Pythonom
FROM python:3.15-slim

# 🌍 ISAS CORE: Globálne systémové a Python prostredie
ENV TZ="Europe/Bratislava" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Inštalácia systémových závislostí a oficiálneho PostgreSQL repozitára pre klienta v18 (dynamická verzia OS)
RUN apt-get update && apt-get install -y curl ca-certificates gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql-keyring.gpg \
    && . /etc/os-release && echo "deb [signed-by=/usr/share/keyrings/postgresql-keyring.gpg] http://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y \
    libpq-dev \
    postgresql-client-18 \
    gcc \
    poppler-utils \
    tzdata \
    pkg-config \
    libcairo2-dev \
    && ln -snf /usr/share/zoneinfo/Europe/Bratislava /etc/localtime && echo Europe/Bratislava > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Pracovný adresár vnútri Dockeru
WORKDIR /app

# Inštalácia tvojich knižníc
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 🚀 ISAS CORE MÁGIA: Inštalácia fyzického Chromia pre Playwright!
RUN playwright install chromium --with-deps

# Skopírovanie celého tvojho kódu do Dockeru
COPY . .

# Zhromaždenie statických súborov pre WhiteNoise
RUN SECRET_KEY=dummy-build-key DEBUG=True CRYPTOGRAPHY_KEY=dummy-build-key-12345678901234567890123456= DB_HOST=dummy python manage.py collectstatic --noinput

# Príkaz na spustenie (CMD) je definovaný dynamicky v docker-compose.yml