from pathlib import Path
import os

from dotenv import load_dotenv
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.redis import RedisIntegration

load_dotenv()

# ==============================================================================
# TYPOVÉ POMOCNÉ FUNKCIE PRE PROSTREDIE (.ENV)
# ==============================================================================
def get_env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    clean_val = val.strip().strip('\'"').lower()
    return clean_val in ('true', '1', 't', 'yes')

def get_env_str(name: str, default: str = '') -> str:
    val = os.environ.get(name)
    if val is None:
        return default
    clean_val = val.strip().strip('\'"')
    return clean_val if clean_val else default

def get_env_int(name: str, default: int = 0) -> int:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val.strip().strip('\'"'))
    except (ValueError, TypeError):
        return default

def get_env_domain(name: str, default: str = '') -> str:
    """🛡️ Bezpečne načíta a normalizuje doménu z .env (odstráni protokol, cesty, port, www a ošetrí IPv6)."""
    raw = get_env_str(name, default).lower().strip()
    if not raw:
        return default
    clean = raw.replace('https://', '').replace('http://', '').split('/')[0].strip()
    # Ochrana pre IPv6 adresy verzus bežný port :8000
    if clean.startswith('[') and ']' in clean:
        clean = clean[:clean.find(']') + 1]
    else:
        clean = clean.split(':')[0]
    clean = clean.strip().rstrip('.')
    if clean.startswith('www.'):
        clean = clean[4:]
    return clean if clean else default


# Prostredie kontajnera
IS_DOCKER = os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv') or get_env_str('DB_HOST') == 'db'

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = get_env_bool('DEBUG', False)

SECRET_KEY = get_env_str('SECRET_KEY', 'fallback-tajny-kluc')
if not DEBUG and SECRET_KEY == 'fallback-tajny-kluc':
    raise ValueError("Kritická chyba: V produkcii (DEBUG=False) nesmie chýbať bezpečný SECRET_KEY v .env!")

# ==============================================================================
# CELERY & REDIS KONFIGURÁCIA
# ==============================================================================
DEFAULT_REDIS_HOST = 'redis://redis:6379' if IS_DOCKER else 'redis://127.0.0.1:6379'
REDIS_BASE_URL = get_env_str('REDIS_URL', DEFAULT_REDIS_HOST).rstrip('/')

CELERY_BROKER_URL = get_env_str('CELERY_BROKER_URL', f"{REDIS_BASE_URL}/0")
CELERY_RESULT_BACKEND = get_env_str('CELERY_RESULT_BACKEND', f"{REDIS_BASE_URL}/0")
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {
    'visibility_timeout': 43200,  # 12 hodín: zamedzí redistribúcii bežiaceho exportu
    'health_check_interval': 15,  # TCP keep-alive ping zamedzí zamrznutiu workerov
}
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = get_env_str('CELERY_TIMEZONE') or get_env_str('TIME_ZONE') or 'UTC'
CELERY_ENABLE_UTC = get_env_bool('USE_TZ', False)
CELERY_TASK_IGNORE_RESULT = True
CELERY_RESULT_EXPIRES = 1800

# Riadenie procesov a ochrana pamäte workerov
CELERY_WORKER_MAX_TASKS_PER_CHILD = get_env_int('CELERY_MAX_TASKS_PER_CHILD', 50)
CELERY_WORKER_MAX_MEMORY_PER_CHILD = get_env_int('CELERY_MAX_MEMORY_PER_CHILD', 350000)  # 350 MB
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = False
CELERY_TASK_TIME_LIMIT = 3600
CELERY_TASK_SOFT_TIME_LIMIT = 3300
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# ==============================================================================
# CELERY ROUTING & BEAT SCHEDULE (DELEGOVANÉ DO CELERY_SCHEDULE.PY)
# ==============================================================================
from .celery_schedule import (
    CELERY_TASK_DEFAULT_QUEUE,
    CELERY_TASK_ROUTES,
    CELERY_BEAT_SCHEDULE,
)

# Hlavná doména pre IoT Multitenancy (využíva jednotnú čistiacu funkciu)
MAIN_TELEMETRY_DOMAIN = get_env_domain('MAIN_TELEMETRY_DOMAIN', 'localhost')

SITE_URL = get_env_str('SITE_URL', f"https://{MAIN_TELEMETRY_DOMAIN}" if not DEBUG and MAIN_TELEMETRY_DOMAIN != 'localhost' else 'http://127.0.0.1:8000')

_DEFAULT_ALLOWED_HOSTS = '127.0.0.1,localhost,.ngrok-free.dev' if DEBUG else '127.0.0.1,localhost'
_BASE_ALLOWED_HOSTS = [host.strip() for host in get_env_str('ALLOWED_HOSTS', _DEFAULT_ALLOWED_HOSTS).split(',') if host.strip()]
if IS_DOCKER and 'web' not in _BASE_ALLOWED_HOSTS:
    _BASE_ALLOWED_HOSTS.append('web')
if MAIN_TELEMETRY_DOMAIN not in _BASE_ALLOWED_HOSTS:
    _BASE_ALLOWED_HOSTS.append(MAIN_TELEMETRY_DOMAIN)
if f".{MAIN_TELEMETRY_DOMAIN}" not in _BASE_ALLOWED_HOSTS:
    _BASE_ALLOWED_HOSTS.append(f".{MAIN_TELEMETRY_DOMAIN}")

PUBLIC_WEB_DOMAIN = get_env_domain('PUBLIC_WEB_DOMAIN', '')
if PUBLIC_WEB_DOMAIN and PUBLIC_WEB_DOMAIN not in _BASE_ALLOWED_HOSTS:
    _BASE_ALLOWED_HOSTS.extend([PUBLIC_WEB_DOMAIN, f".{PUBLIC_WEB_DOMAIN}"])

_DEFAULT_CSRF_ORIGINS = 'http://127.0.0.1:8000,http://localhost:8000,https://*.ngrok-free.dev' if DEBUG else ''
_BASE_CSRF_ORIGINS = [origin.strip() for origin in get_env_str('CSRF_TRUSTED_ORIGINS', _DEFAULT_CSRF_ORIGINS).split(',') if origin.strip()]
if MAIN_TELEMETRY_DOMAIN == 'localhost':
    _BASE_CSRF_ORIGINS.extend([
        'http://localhost:8000', 'http://127.0.0.1:8000', 'http://localhost', 'http://127.0.0.1',
        'http://*.localhost:8000', 'http://*.localhost'
    ])
else:
    _BASE_CSRF_ORIGINS.extend([f"https://{MAIN_TELEMETRY_DOMAIN}", f"https://*.{MAIN_TELEMETRY_DOMAIN}"])

if PUBLIC_WEB_DOMAIN and PUBLIC_WEB_DOMAIN != 'localhost':
    _BASE_CSRF_ORIGINS.extend([f"https://{PUBLIC_WEB_DOMAIN}", f"https://*.{PUBLIC_WEB_DOMAIN}"])

class DynamicAllowedHosts(list):
    """🛡️ Vysoko výkonný generátor hostov: IoT a .env domény rieši v RAM, Redis kontaktuje len pri neznámom hoste."""
    def __iter__(self):
        seen = set()
        for host in _BASE_ALLOWED_HOSTS:
            if host not in seen:
                seen.add(host)
                yield host
        # K Redisu pristúpime LEN ak predchádzajúce domény nezhodovali požiadavku
        try:
            from django.core.cache import cache
            dyn_dom = cache.get('isas_public_web_domain')
            if dyn_dom and isinstance(dyn_dom, str):
                clean_dom = dyn_dom.strip().lower().rstrip('.')
                if clean_dom and clean_dom != 'localhost' and not clean_dom.startswith('.') and not any(c.isspace() or c in '/:@*' for c in clean_dom):
                    for host_pat in (clean_dom, f".{clean_dom}"):
                        if host_pat not in seen:
                            seen.add(host_pat)
                            yield host_pat
        except Exception:
            pass

    def __contains__(self, item):
        if not item or not isinstance(item, str):
            return False
        clean_item = item.lower().strip()
        for pattern in self:
            if pattern == '*' or pattern == clean_item:
                return True
            # 🛡️ OCHRANA PRED SPOOFINGOM: item musí striktne končiť na celú príponu vrátane bodky
            if pattern.startswith('.') and (clean_item.endswith(pattern) or clean_item == pattern[1:]):
                return True
        return False

class DynamicCsrfTrustedOrigins(list):
    """🛡️ Lenivý zoznam CSRF dôveryhodných originov napojený na cache."""
    def __iter__(self):
        seen = set()
        for origin in _BASE_CSRF_ORIGINS:
            if origin not in seen:
                seen.add(origin)
                yield origin
        try:
            from django.core.cache import cache
            dyn_dom = cache.get('isas_public_web_domain')
            if dyn_dom and isinstance(dyn_dom, str):
                clean_dom = dyn_dom.strip().lower().rstrip('.')
                if clean_dom and clean_dom != 'localhost' and not clean_dom.startswith('.') and not any(c.isspace() or c in '/:@*' for c in clean_dom):
                    for proto in (('https', 'http') if DEBUG else ('https',)):
                        for orig in (f"{proto}://{clean_dom}", f"{proto}://*.{clean_dom}"):
                            if orig not in seen:
                                seen.add(orig)
                                yield orig
        except Exception:
            pass

ALLOWED_HOSTS = DynamicAllowedHosts(_BASE_ALLOWED_HOSTS)
CSRF_TRUSTED_ORIGINS = DynamicCsrfTrustedOrigins(_BASE_CSRF_ORIGINS)

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True # KRITICKÉ PRE REVERZNÝ PROXY: Zaručí správnu detekciu tenant domén z X-Forwarded-Host

if not DEBUG:
    SECURE_SSL_REDIRECT = get_env_bool('SECURE_SSL_REDIRECT', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = get_env_int('SECURE_HSTS_SECONDS', 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

INDUSTRY_MODULE = (get_env_str('INDUSTRY_MODULE', '') or '').strip().lower()

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'custom_auth.apps.CustomAuthConfig',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sitemaps',
    'django.contrib.postgres',
    'ISASaplikacia.apps.IsasaplikaciaConfig',
    'Evidencia.apps.EvidenciaConfig',
]

if INDUSTRY_MODULE:
    INSTALLED_APPS.append(f'odvetvia.{INDUSTRY_MODULE}')

INSTALLED_APPS += [
    'WebBuilder.apps.WebbuilderConfig',
    'nested_admin',
    'colorfield',
    'import_export',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.middleware.gzip.GZipMiddleware',
    'ISASaplikacia.middleware.PublicWebRoutingMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'ISASaplikacia.middleware.DynamicCsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'ISASaplikacia.middleware.ISASAuditMiddleware', # 🛡️ SPRÁVNE UMIESTNENIE: Okamžite po Auth sprístupní usera pre Tenant a Audit modul
    'ISASaplikacia.middleware.ISASTenantMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'ISASaplikacia.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / "templates"],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.media',
                'ISASaplikacia.context_processors.firemna_identita_web',
                'ISASaplikacia.context_processors.ai_costs_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'ISASaplikacia.wsgi.application'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': get_env_str('DB_NAME', 'isas_databaza'),
        'USER': get_env_str('DB_USER', 'postgres'),
        'PASSWORD': get_env_str('DB_PASS'),
        'HOST': get_env_str('DB_HOST', 'db' if IS_DOCKER else '127.0.0.1'),
        'PORT': get_env_str('DB_PORT', '5432'),
        'CONN_MAX_AGE': get_env_int('CONN_MAX_AGE', 60),
        'CONN_HEALTH_CHECKS': True,
    }
}

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher',
    'django.contrib.auth.hashers.BCryptSHA256PasswordHasher',
    'django.contrib.auth.hashers.ScryptPasswordHasher',
]

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LOGOUT_REDIRECT_URL = '/admin/login/'
LOGIN_REDIRECT_URL = '/admin/'


LANGUAGE_CODE = get_env_str('LANGUAGE_CODE', 'en')
TIME_ZONE = get_env_str('TIME_ZONE', 'UTC')
USE_I18N = True
USE_TZ = get_env_bool('USE_TZ', False)

LOCALE_PATHS = [os.path.join(BASE_DIR, 'locale')]

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

DATA_UPLOAD_MAX_NUMBER_FIELDS = 20000

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

_email_backend = get_env_str('EMAIL_BACKEND', 'ISASaplikacia.mail_backends.ISASDynamicEmailBackend')

MAILERS = {
    'default': {
        'BACKEND': _email_backend,
        'OPTIONS': {
            'host': get_env_str('EMAIL_HOST', 'localhost'),
            'port': get_env_int('EMAIL_PORT', 587),
            'username': get_env_str('EMAIL_HOST_USER', ''),
            'password': get_env_str('EMAIL_HOST_PASSWORD', ''),
            'use_tls': get_env_bool('EMAIL_USE_TLS', True),
        },
    }
}

DEFAULT_FROM_EMAIL = get_env_str('DEFAULT_FROM_EMAIL', 'ISAS Core <webmaster@localhost>')
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# ==========================================
# TAJNÉ KĽÚČE A HESLÁ (Načítavané z .env)
# ==========================================
GEMINI_MODEL = get_env_str('GEMINI_MODEL')
_valid_thinking_levels = {'MINIMAL','LOW', 'MEDIUM', 'HIGH', 'XHIGH'}
_raw_thinking_level = str(get_env_str('GEMINI_THINKING_LEVEL', 'MEDIUM')).strip().upper()
GEMINI_THINKING_LEVEL = _raw_thinking_level if _raw_thinking_level in _valid_thinking_levels else 'MEDIUM'

GEMINI_API_KEY = get_env_str('GEMINI_API_KEY')
GOOGLE_MAPS_API_KEY = get_env_str('GOOGLE_MAPS_API_KEY')

CRYPTOGRAPHY_KEY = get_env_str('CRYPTOGRAPHY_KEY')
if not DEBUG and not CRYPTOGRAPHY_KEY:
    raise ValueError("Kritická chyba: V produkcii nesmie chýbať CRYPTOGRAPHY_KEY v .env!")

# ==========================================
# ISAS RAM CACHE (Pre Live Radar a rýchle dáta)
# ==========================================
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        # Databáza 1 pre Cache (izolovaná od Celery DB 0)
        'LOCATION': get_env_str('REDIS_CACHE_URL', f"{REDIS_BASE_URL}/1"),
        'OPTIONS': {
            'socket_connect_timeout': 5,
            'socket_timeout': 5,
        }
    }
}


X_FRAME_OPTIONS = 'SAMEORIGIN'
SILENCED_SYSTEM_CHECKS = ['security.W019']  # SAMEORIGIN je vyžadované pre vnorené admin formuláre (nested_admin)

SENTRY_DSN = get_env_str('SENTRY_DSN')
_force_sentry_dev = get_env_bool('SENTRY_FORCE_DEV', False)
SENTRY_ACTIVE = bool(SENTRY_DSN) and (not DEBUG or _force_sentry_dev)

SENTRY_ENVIRONMENT = get_env_str('SENTRY_ENVIRONMENT', 'development' if DEBUG else 'production')

if SENTRY_ACTIVE:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(monitor_beat_tasks=True),
            RedisIntegration(),
        ],
        environment=SENTRY_ENVIRONMENT,
        traces_sample_rate=0.1,
        send_default_pii=False,
    )