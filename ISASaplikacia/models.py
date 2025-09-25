from django.contrib.auth.models import User, Group

class CustomUser(User):
    class Meta:
        proxy = True
        verbose_name = 'Používateľ'
        verbose_name_plural = 'Používatelia'

class CustomGroup(Group):
    class Meta:
        proxy = True
        verbose_name = 'Skupina'
        verbose_name_plural = 'Skupiny'
