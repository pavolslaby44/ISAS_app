release: python manage.py migrate && python manage.py create_default_superuser
web: gunicorn isasaplikacia.wsgi:application
