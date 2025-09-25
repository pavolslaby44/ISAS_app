from django.contrib.auth import get_user_model

def create_default_superuser():
    User = get_user_model()
    username = "ISAS"
    email = "pavolslaby44@gmail.com"
    password = "Fruscante123"

    if not User.objects.filter(username=username).exists():
        User.objects.create_superuser(username=username, email=email, password=password)
        print(f"✅ Superuser '{username}' created.")
    else:
        print(f"ℹ️ Superuser '{username}' already exists.")
