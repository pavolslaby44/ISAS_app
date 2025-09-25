from django.contrib.auth import get_user_model
import os

def create_superuser():
    User = get_user_model()
    username = os.getenv("DJANGO_SUPERUSER_USERNAME", "ISAS")
    email = os.getenv("DJANGO_SUPERUSER_EMAIL", "pavolslaby44@gmail.com")
    password = os.getenv("DJANGO_SUPERUSER_PASSWORD", "Fruscante123")

    if not User.objects.filter(username=username).exists():
        User.objects.create_superuser(username=username, email=email, password=password)
        print(f"✅ Superuser '{username}' bol vytvorený")
    else:
        print(f"ℹ️ Superuser '{username}' už existuje")
