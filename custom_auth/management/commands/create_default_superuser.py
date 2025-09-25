from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
import os

class Command(BaseCommand):
    help = "Create a default superuser if none exists"

    def handle(self, *args, **options):
        User = get_user_model()
        username = "ISAS"
        password = "Fruscante123"
        email = "pavolslaby44@gmail.com"

        if not User.objects.filter(username=username).exists():
            User.objects.create_superuser(username=username, email=email, password=password)
            self.stdout.write(self.style.SUCCESS(f"✅ Superuser '{username}' created."))
        else:
            self.stdout.write(self.style.WARNING(f"ℹ️ Superuser '{username}' already exists."))
