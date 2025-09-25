from django.contrib import admin

admin.site.site_header = "ISAS app"
admin.site.site_title = "Industrial services and solutions - app"
admin.site.index_title = "ISAS - správca používateľov"

from django.contrib import admin
from django.contrib.auth.models import User, Group
from .models import CustomUser, CustomGroup

admin.site.unregister(User)
admin.site.unregister(Group)

@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    pass

@admin.register(CustomGroup)
class CustomGroupAdmin(admin.ModelAdmin):
    pass
