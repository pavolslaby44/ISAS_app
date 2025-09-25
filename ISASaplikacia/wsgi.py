"""
WSGI config for ISASapp project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ISASaplikacia.settings')

application = get_wsgi_application()

try:
    from custom_auth.auto_superuser import create_superuser
    create_superuser()
except Exception as e:
    print("⚠️ Superuser creation skipped:", e)

