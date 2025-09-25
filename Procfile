release: python manage.py migrate && python manage.py shell -c "from ISASaplikacia.create_default_superuser import create_default_superuser; create_default_superuser()"
web: gunicorn ISASaplikacia.wsgi:application
