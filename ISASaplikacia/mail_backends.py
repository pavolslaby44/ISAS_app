# ISASaplikacia/mail_backends.py
import threading
import logging
import os
from django.core.mail.backends.smtp import EmailBackend
from django.core.cache import cache
from django.apps import apps
from django.conf import settings

logger = logging.getLogger(__name__)

class ISASDynamicEmailBackend(EmailBackend):
    """
    🏛️ ISAS DYNAMIC SMTP BACKEND (Django 6.1+ MAILERS)
    Dynamicky preberá SMTP konfiguráciu z DB singletonu GlobalneNastavenia s cache vrstvou.
    Thread-safe, plne kompatibilný s Django 6.1 MAILERS.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config_lock = threading.RLock()

    def _apply_dynamic_settings(self):
        with self._config_lock:
            # 1. Priorita: Cache / Databázový model GlobalneNastavenia
            gn_data = cache.get('isas_smtp_config')
            if gn_data is None:
                try:
                    GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                    gn = GlobalneNastavenia.objects.first() if GlobalneNastavenia else None
                    if gn and gn.smtp_host:
                        gn_data = {
                            'host': str(gn.smtp_host).strip(),
                            'port': int(gn.smtp_port) if gn.smtp_port else 587,
                            'username': str(gn.smtp_user or '').strip(),
                            'password': str(gn.smtp_password or ''),
                            'use_tls': bool(gn.smtp_use_tls),
                        }
                        cache.set('isas_smtp_config', gn_data, 300)  # Cache na 5 minút
                except Exception as e:
                    logger.warning(f"ISASDynamicEmailBackend: Zlyhalo načítanie SMTP z DB: {e}")

            if gn_data and gn_data.get('host'):
                self.host = gn_data['host']
                self.port = gn_data['port']
                self.username = gn_data['username']
                self.password = gn_data['password']
                self.use_ssl = (self.port == 465)
                self.use_tls = gn_data['use_tls'] if not self.use_ssl else False
                self.timeout = 45  # 🛡️ 45s rezerva pre prenos e-mailov s veľkými Playwright PDF prílohami
                return

            # 2. Priorita: Fallback na .env prostredie
            env_host = os.environ.get('EMAIL_HOST', '').strip()
            if env_host:
                self.host = env_host
                try:
                    self.port = int(os.environ.get('EMAIL_PORT', 587))
                except (ValueError, TypeError):
                    self.port = 587
                self.username = os.environ.get('EMAIL_HOST_USER', '').strip()
                self.password = os.environ.get('EMAIL_HOST_PASSWORD', '')
                self.use_ssl = (self.port == 465)
                raw_tls = os.environ.get('EMAIL_USE_TLS', 'true').strip().lower()
                self.use_tls = (raw_tls in ('true', '1', 't', 'yes')) if not self.use_ssl else False
                self.timeout = 45

    def open(self):
        self._apply_dynamic_settings()
        return super().open()

    def send_messages(self, email_messages):
        self._apply_dynamic_settings()

        # 1. Ochrana pre lokálny vývoj (DEBUG)
        if getattr(settings, 'DEBUG', False) and (not self.password or not self.host):
            logger.info("ISASDynamicEmailBackend: DEBUG režim bez SMTP hesla -> výpis mailu do konzoly.")
            from django.core.mail.backends.console import EmailBackend as ConsoleBackend
            return ConsoleBackend().send_messages(email_messages)

        # 2. Ochrana pre produkciu bez konfigurácie
        if not self.host or (self.host == 'localhost' and not self.username):
            logger.error("ISASDynamicEmailBackend: Chýba konfigurácia SMTP v DB aj v .env prostredí!")
            if self.fail_silently:
                return 0
            from django.core.exceptions import ImproperlyConfigured
            raise ImproperlyConfigured("SMTP server nie je nakonfigurovaný v Globálnych nastaveniach ani v .env!")

        # 3. 🛡️ Ochrana pred chybou 553 (Spoofing): Dynamické prispôsobenie From a zachovanie Reply-To
        for message in email_messages:
            if self.username and '@' in self.username:
                if not message.from_email or 'localhost' in message.from_email or message.from_email != self.username:
                    if message.from_email and 'localhost' not in message.from_email and not getattr(message, 'reply_to', None):
                        message.reply_to = [message.from_email]
                    message.from_email = f"ISAS Core <{self.username}>"

        return super().send_messages(email_messages)