import os

from celery import Celery
from celery.signals import task_prerun, task_postrun, worker_process_init

# Nastavíme defaultný Django settings modul pre Celery PRED importom Django modulov
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ISASaplikacia.settings')

from django.db import close_old_connections, connections

app = Celery('ISASaplikacia')

# Načítame konfiguráciu z Django settings, všetky Celery premenné musia začínať na 'CELERY_'
app.config_from_object('django.conf:settings', namespace='CELERY')

# Automaticky objaví úlohy (tasks.py) vo všetkých nainštalovaných aplikáciách
app.autodiscover_tasks()

@worker_process_init.connect
def close_db_connections_on_worker_init(*args, **kwargs):
    """🛡️ OCHRANA PRED PREFORK DEADLOCKOM: Uzavrie DB sockety zdedené z rodičovského procesu."""
    connections.close_all()

@task_prerun.connect
def close_db_connections_before_task(*args, **kwargs):
    close_old_connections()

@task_postrun.connect
def close_db_connections_after_task(*args, **kwargs):
    close_old_connections()

@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')