# odvetvia/kompresory/apps.py
from django.apps import AppConfig


class KompresoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'odvetvia.kompresory'
    label = 'kompresory'
    verbose_name = 'Odvetvie: Kompresory a stlačený vzduch'