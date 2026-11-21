from django.apps import AppConfig
from django.db.models.signals import post_delete

class IsasaplikaciaConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ISASaplikacia'
    verbose_name = 'ISAS CORE - Centrum účtov so správou digitálneho ekosystému'

    def ready(self):
        from django.apps import apps
        from django.db import models
        from django.contrib.auth.models import Group, User
        
        # 🏷️ Nastavenie ISAS Emoji a názvov pre štandardné User a Group modely
        User._meta.verbose_name = '🧑‍💻 Používateľ'
        User._meta.verbose_name_plural = '🧑‍💻 Používatelia'
        Group._meta.verbose_name = '🛡️ Skupina'
        Group._meta.verbose_name_plural = '🛡️ Skupiny'

        # Importujeme náš dynamický čistič z models.py
        from .models import isas_omni_file_cleanup 

        # Prejdeme úplne všetky modely v ISASaplikacia
        for model in apps.get_app_config(self.name).get_models():
            
            # Zistíme, či model obsahuje aspoň jedno pole typu FileField alebo ImageField
            has_file_field = any(isinstance(field, models.FileField) for field in model._meta.fields)
            
            # Ak áno, dynamicky naň zavesíme náš signál s garanciou idempotencie (ochrana pred duplicitou)
            if has_file_field:
                post_delete.connect(
                    isas_omni_file_cleanup,
                    sender=model,
                    dispatch_uid=f"isas_omni_cleanup_{model._meta.label_lower}"
                )
