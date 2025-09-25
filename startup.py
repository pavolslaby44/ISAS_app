from django.contrib.auth import get_user_model
from django.db.utils import OperationalError

def create_superuser():
    User = get_user_model()
    try:
        if not User.objects.filter(username="ISAS").exists():
            User.objects.create_superuser(
                username="ISAS",
                email="isas@example.com",
                password="Fruscante123"
            )
            print("✅ Superuser 'ISAS' created.")
        else:
            print("ℹ️ Superuser 'ISAS' already exists.")
    except OperationalError:
        print("⚠️ Database not ready for creating superuser.")
