from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

class Command(BaseCommand):
    help = "Create default superuser if not exists"

    def handle(self, *args, **options):
        User = get_user_model()
        username = "ISAS"
        password = "Fruscante123"
        email = "pavolslaby44@gmail.com"

        if not User.objects.filter(username=username).exists():
            User.objects.create_superuser(username=username, password=password, email=email)
            self.stdout.write(self.style.SUCCESS(f"✅ Superuser '{username}' bol vytvorený"))
        else:
            self.stdout.write(self.style.WARNING(f"ℹ️ Superuser '{username}' už existuje"))
