import contextvars
import re
import threading

from django.apps import apps
from django.http import Http404
from django.conf import settings
from django.core.cache import cache
from django.http.request import split_domain_port
from django.middleware.csrf import CsrfViewMiddleware
from django.urls import set_urlconf
from django.utils.functional import empty

# Regex pre RFC 1123 subdomény (predkompilovaný pre vysoký výkon pri IoT záťaži)
_SUBDOMAIN_REGEX = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')

# Lokálne loopback aliasy (RFC 1122 / RFC 6761) pre konzistentnú detekciu vývojového prostredia
_LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1', 'testserver'}

# Kontextovo-špecifické úložisko (Bezpečné pre vlákna, ASGI aj asynchrónne coroutines)
_current_user_var = contextvars.ContextVar('isas_current_user', default=None)

def get_current_user():
    """
    Vráti aktuálneho usera kdekoľvek v kóde (napr. priamo v modeli).
    Ak sa zmena vykonáva cez AI skript na pozadí alebo verejný web, vráti None.
    """
    user = _current_user_var.get()
    if not user or user is empty:
        return None
    if hasattr(user, 'is_authenticated') and not user.is_authenticated:
        return None
    unwrapped = getattr(user, '_wrapped', user)
    return None if (unwrapped is None or unwrapped is empty) else unwrapped

class ISASAuditMiddleware:
    """Zachytí používateľa z Requestu a sprístupní ho globálne pre Audit Modul."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Bezpečné eager vyhodnotenie: garantuje, že v kontexte je VŽDY buď User alebo None (nikdy AnonymousUser ani nebezpečný SimpleLazyObject)
        user = getattr(request, 'user', None)
        if user is not None and user is not empty:
            if not getattr(user, 'is_authenticated', False):
                user = None
            else:
                unwrapped = getattr(user, '_wrapped', user)
                user = None if (unwrapped is None or unwrapped is empty) else unwrapped
        else:
            user = None
        token = _current_user_var.set(user)
        
        try:
            response = self.get_response(request)
            return response
        finally:
            # BEZPEČNOSŤ: Garantovaný reset kontextu na predchádzajúci stav aj pri HTTP 500
            _current_user_var.reset(token)

class ISASTenantMiddleware:
    """
    🧠 ISAS CORE: Multitenancy Middleware pre IoT Dashboard.
    Zachytáva subdomény (napr. klient.telemetria.sk) a izoluje dáta.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 🛡️ GARANTOVANÁ INICIALIZÁCIA: Zamedzí AttributeError v context processoroch a views
        request.tenant = None
        request.is_subdomain = False

        # 🛡️ WEB BUILDER OCHRANA: Ak ide o verejný web, ignorujeme IoT tenant kontrolu
        if getattr(request, 'is_public_web', False):
            return self.get_response(request)

        host = split_domain_port(request.get_host())[0].lower().rstrip('.')
        
        # 🛡️ ASSETS BYPASS: Statické, mediálne a systémové assety nesmú vyťažovať tenant cache a DB
        # Defenzívne zistenie lokálnych ciest pre statiku a médiá (ignorujeme CDN domény začínajúce na http)
        raw_static = getattr(settings, 'STATIC_URL', '/static/') or '/static/'
        raw_media = getattr(settings, 'MEDIA_URL', '/media/') or '/media/'
        
        static_prefix = raw_static.lstrip('/') if not raw_static.startswith(('http://', 'https://')) else 'static/'
        media_prefix = raw_media.lstrip('/') if not raw_media.startswith(('http://', 'https://')) else 'media/'
        
        # Garancia koncovej lomky zabraňuje kolíziám s aplikačnými URL (napr. /statistics/, /status/)
        if not static_prefix.endswith('/'):
            static_prefix += '/'
        if not media_prefix.endswith('/'):
            media_prefix += '/'

        path_clean = request.path_info.lower().lstrip('/')
        if path_clean.startswith((static_prefix, media_prefix)) or path_clean in ('favicon.ico', 'robots.txt'):
            return self.get_response(request)

        # Defenzívne očistenie konfiguračnej hodnoty (ochrana pred case-mismatch, protokolom, portom, bodkami a www)
        raw_main_telemetry = getattr(settings, 'MAIN_TELEMETRY_DOMAIN', '').lower().replace('https://', '').replace('http://', '')
        try:
            main_domain_clean = split_domain_port(raw_main_telemetry)[0].strip().rstrip('.')
        except Exception:
            main_domain_clean = raw_main_telemetry.split(':')[0].strip().rstrip('.')
        if main_domain_clean.startswith('www.'):
            main_domain_clean = main_domain_clean[4:]

        # Vyhradené systémové subdomény, ktoré nesmú byť považované za klientsky IoT portál ('web' patrí WebBuilderu)
        SYSTEM_RESERVED_SUBDOMAINS = {'www', 'admin', 'app', 'erp', 'mail', 'api', 'dispecing', 'status', 'flower', 'web'}
        
        # Ak ide o subdoménu (a nie je to www ani vyhradená systémová subdoména)
        if main_domain_clean and host.endswith(f".{main_domain_clean}") and host != main_domain_clean:
            subdomain = host.removesuffix(f".{main_domain_clean}").strip('.')
            # Systémové subdomény legitímne prepúšťame do hlavného ERP routingu (admin, api, atď.)
            if subdomain in SYSTEM_RESERVED_SUBDOMAINS:
                return self.get_response(request)
            
            # Bezpečnostná poistka (DNS RFC 1123): neplatné a viacúrovňové subdomény nesmú obísť tenant kontrolu a dostať sa do ERP
            if not subdomain or not _SUBDOMAIN_REGEX.fullmatch(subdomain):
                raise Http404("Klientsky portál neexistuje alebo bol deaktivovaný.")

            # 🚀 ISAS ARCHITECT FIX: Lazy import výhradne pre verifikované tenant požiadavky
            from Evidencia.models import Zakaznik
            
            # 🚀 REAL CACHE LAYER: Ukladáme priamo inštanciu Zakaznik, čím sa eliminuje 100% DB dopytov pri IoT telemetrii
            cache_key = f"isas_tenant_obj_{subdomain}"
            try:
                cached_tenant = cache.get(cache_key)
            except Exception:
                cached_tenant = None

            if cached_tenant == "__NOT_FOUND__":
                raise Http404("Klientsky portál neexistuje alebo bol deaktivovaný.")

            # Validácia cachovaného objektu: musí ísť o inštanciu Zakaznik a nesmie mať porušený PK
            if cached_tenant is not None and isinstance(cached_tenant, Zakaznik) and getattr(cached_tenant, 'pk', None):
                tenant = cached_tenant
            else:
                try:
                    tenant = Zakaznik.objects.filter(subdomena__iexact=subdomain).first()
                except Exception:
                    # 🛡️ OCHRANA PRED POISONINGOM: Pri zlyhaní DB nesmieme zapísať __NOT_FOUND__ do cache ani maskovať 500 ako 404
                    raise

                if tenant:
                    try:
                        cache.set(cache_key, tenant, 300)
                    except Exception:
                        pass
                else:
                    try:
                        cache.set(cache_key, "__NOT_FOUND__", 60)
                    except Exception:
                        pass

            if tenant:
                request.tenant = tenant
                request.is_subdomain = True

                # 🛡️ IZOLÁCIA TENANTA: Zablokovanie centrály, interných management API a personálnych nástrojov
                tenant_blocked = ('admin', '_nested_admin', 'isas-mail-api', 'api', 'spotify')
                
                # Defenzívne overenie prefixov s garanciou lomiek
                is_blocked_root = any(path_clean == prefix or path_clean.startswith(f"{prefix}/") for prefix in tenant_blocked)
                
                # V evidencii na subdoméne povoľujeme výhradne IoT Dashboard a jeho vnorené telemetrické endpointy
                is_evidencia_path = path_clean == 'evidencia' or path_clean.startswith('evidencia/')
                is_valid_iot_dashboard = path_clean == 'evidencia/iot-dashboard' or path_clean.startswith('evidencia/iot-dashboard/')
                is_unauthorized_evidencia = is_evidencia_path and not is_valid_iot_dashboard

                if is_blocked_root or is_unauthorized_evidencia:
                    raise Http404("🚫 Táto sekcia nie je na klientskom portáli dostupná.")

            else:
                raise Http404("Klientsky portál neexistuje alebo bol deaktivovaný.")

        response = self.get_response(request)
        return response

class PublicWebRoutingMiddleware:
    """
    🌐 ISAS WEB BUILDER: Smerovanie verejnej domény.
    Ak požiadavka príde na verejnú doménu (napr. isascore.com), 
    dynamicky zmení URL routing a zablokuje prístup do ERP.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.is_public_web = False
        # 🛡️ ASSETS BYPASS: Statické a mediálne assety odchádzajú okamžite; robots.txt a favicon MUSIA pokračovať do WebBuilder URLconfu
        raw_static = getattr(settings, 'STATIC_URL', '/static/') or '/static/'
        raw_media = getattr(settings, 'MEDIA_URL', '/media/') or '/media/'
        s_pfx = raw_static.lstrip('/') if not raw_static.startswith(('http://', 'https://')) else 'static/'
        m_pfx = raw_media.lstrip('/') if not raw_media.startswith(('http://', 'https://')) else 'media/'
        
        # Garancia koncovej lomky zabraňuje kolíziám s aplikačnými URL (napr. /statistics/, /status/)
        if not s_pfx.endswith('/'):
            s_pfx += '/'
        if not m_pfx.endswith('/'):
            m_pfx += '/'
            
        cesta_lower = request.path_info.lower().lstrip('/')
        if cesta_lower.startswith((s_pfx, m_pfx)):
            return self.get_response(request)

        try:
            # 🛡️ DEV CACHE BYPASS: Pri lokálnom vývoji (DEBUG=True) načítavame doménu vždy naživo z DB bez 5-minútového čakania
            public_domain = None if getattr(settings, 'DEBUG', False) else cache.get('isas_public_web_domain')
            if public_domain is None:
                WebNastavenia = apps.get_model('WebBuilder', 'WebNastavenia')
                nastavenia = WebNastavenia.objects.filter(pk=1).first() if WebNastavenia else None
                db_domena = (nastavenia.domena or "").strip().lower() if (nastavenia and nastavenia.domena) else ""
                raw_domain = db_domena or getattr(settings, 'PUBLIC_WEB_DOMAIN', '').strip().lower()
                if raw_domain:
                    clean_dom = raw_domain.replace('https://', '').replace('http://', '').split('/')[0]
                    clean_dom = split_domain_port(clean_dom)[0].strip().rstrip('.')
                    if clean_dom.startswith('www.'):
                        clean_dom = clean_dom[4:]
                    public_domain = clean_dom
                else:
                    public_domain = ""
                if not getattr(settings, 'DEBUG', False):
                    cache.set('isas_public_web_domain', public_domain, 300)
            if not public_domain:
                public_domain = None
        except Exception:
            # 🛡️ DEFENSIVE FALLBACK: Pri výpadku DB/Cache nesmie dôjsť k úniku verejného webu do ERP
            raw_fallback = getattr(settings, 'PUBLIC_WEB_DOMAIN', '').strip().lower().replace('https://', '').replace('http://', '').split('/')[0]
            clean_fallback = split_domain_port(raw_fallback)[0].strip().rstrip('.')
            if clean_fallback.startswith('www.'):
                clean_fallback = clean_fallback[4:]
            public_domain = clean_fallback or None

        # 🛡️ Host sa načítava až po zahriatí cache, aby DynamicAllowedHosts overil dynamickú doménu z DB
        try:
            host_raw = request.get_host()
        except Exception:
            raise Http404("Neplatný alebo nepovolený Host.")
            
        host = split_domain_port(host_raw)[0].lower().rstrip('.')
        cisty_host = host[4:] if host.startswith('www.') else host

        # 🛡️ Identifikácia interných ERP požiadaviek s ochranou voči prefixovým kolíziám (login/auth ponechané pre B2B zónu)
        erp_prefixy = (
            'admin', 'evidencia', '_nested_admin', 'isas-mail-api', 
            'api/crm-intel', 'api/live-radar', 'api/history-radar', 
            'api/optimize-route', 'spotify', 'api/spotify',
            'api/easter-egg'
        )
        is_erp_path = any(
            cesta_lower == prefix or cesta_lower.startswith(f"{prefix}/")
            for prefix in erp_prefixy
        )

        # 🧭 Detekcia verejnej domény (striktne normalizovaná bez protokolu, portu, www a bodiek)
        raw_telemetry = getattr(settings, 'MAIN_TELEMETRY_DOMAIN', '').lower().replace('https://', '').replace('http://', '')
        main_telemetry = split_domain_port(raw_telemetry)[0].strip().rstrip('.')
        if main_telemetry.startswith('www.'):
            main_telemetry = main_telemetry[4:]

        # Doména zodpovedá verejnému webu pri priamej zhode alebo loopback zhode (localhost / 127.0.0.1)
        matches_public_domain = bool(
            public_domain and (
                cisty_host == public_domain 
                or (cisty_host in _LOOPBACK_HOSTS and public_domain in _LOOPBACK_HOSTS)
            )
        )

        is_telemetry_host = bool(
            main_telemetry
            and (cisty_host == main_telemetry or host.endswith(f".{main_telemetry}"))
        )

        # 🛡️ WILDCARD OCHRANA: Zamedzí otvoreniu ERP cez nepodporované subdomény verejného webu
        if public_domain and public_domain != main_telemetry and not is_telemetry_host and host.endswith(f".{public_domain}") and cisty_host != public_domain:
            raise Http404("🚫 Táto subdoména na verejnom webe nie je podporovaná.")

        # 🚀 Na produkčnej verejnej doméne blokujeme interné ERP cesty; v lokálnom vývoji prepúšťame admin do ERP
        if matches_public_domain and is_erp_path:
            if not getattr(settings, 'DEBUG', False) and public_domain != main_telemetry:
                raise Http404("🚫 Prístup odmietnutý. Táto sekcia nie je na verejnej doméne dostupná.")
            return self.get_response(request)

        is_public_web = matches_public_domain

        # 🌐 WebBuilder smerovanie: aktivuje sa výhradne pre verifikovanú verejnú doménu s garanciou resetu
        try:
            if is_public_web:
                request.is_public_web = True
                request.urlconf = 'WebBuilder.urls'
                set_urlconf('WebBuilder.urls')

            response = self.get_response(request)
            return response
        finally:
            # 🛡️ BEZPEČNOSŤ: Reset URLconfu zabraňuje úniku stavu do ďalších požiadaviek vo worker vlákne
            if is_public_web:
                set_urlconf(None)


class DynamicCsrfViewMiddleware(CsrfViewMiddleware):
    """🛡️ Obnoví nacachované CSRF originy pri zmene dynamickej domény z ERP adminu bez reštartu workerov."""
    def __init__(self, get_response):
        super().__init__(get_response)
        self._csrf_domain_lock = threading.Lock()
        self._last_dyn_domain = None

    def process_view(self, request, callback, callback_args, callback_kwargs):
        if request.method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            try:
                dyn_dom = cache.get('isas_public_web_domain') or ''
            except Exception:
                dyn_dom = None

            if isinstance(dyn_dom, str) and self._last_dyn_domain != dyn_dom:
                with self._csrf_domain_lock:
                    if self._last_dyn_domain != dyn_dom:
                        self.__dict__.pop('allowed_origins_exact', None)
                        self.__dict__.pop('allowed_origin_subdomains', None)
                        self.__dict__.pop('csrf_trusted_origins_hosts', None)
                        self._last_dyn_domain = dyn_dom

        return super().process_view(request, callback, callback_args, callback_kwargs)