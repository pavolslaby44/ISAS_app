from django.contrib import admin
from .models import Zamestnanec, Dodavatel

@admin.register(Zamestnanec)
class ZamestnanecAdmin(admin.ModelAdmin):
    list_display = (
        'meno',
        'priezvisko',
        'kategoria',
        'email',
        'telefon',
        'datum_narodenia',
        'zmluva',
        'zavazky'
    )
    list_filter = ('kategoria',)
    search_fields = ('meno', 'priezvisko', 'email', 'telefon')

Zamestnanec._meta.verbose_name = "Zamestnanec"
Zamestnanec._meta.verbose_name_plural = "Zamestnanci"

@admin.register(Dodavatel)
class DodavatelAdmin(admin.ModelAdmin):
    list_display = ('kontaktna_osoba', 'telefon', 'email', 'ostatne')
    search_fields = ('kontaktna_osoba', 'telefon', 'email')

from django.contrib import admin
from .models import Zakaznik

@admin.register(Zakaznik)
class ZakaznikAdmin(admin.ModelAdmin):
    list_display = ('Kontaktna_osoba', 'Telefon', 'Email', 'Ostatne')
    search_fields = ('Kontaktna_osoba', 'Email', 'Telefon')

# Evidencia/admin.py

import nested_admin
from django.contrib import admin
from .models import Sklad, Stroj, SpotrebnyMaterial, Poruchovydiel


class StrojInline(nested_admin.NestedTabularInline):
    model = Stroj
    extra = 1



from .models import (
    PoruchovySnimacKabelaz,
    PoruchovaElektrovyzbroj,
    PoruchovyRiadiaciModul,
    PoruchovyVentilatorMotor,
    PoruchovaSaciaKlapka,
    PoruchoveOstatne
)


class SnimacKabelazInline(nested_admin.NestedTabularInline):
    model = PoruchovySnimacKabelaz
    extra = 1

class ElektrovyzbrojInline(nested_admin.NestedTabularInline):
    model = PoruchovaElektrovyzbroj
    extra = 1

class RiadiaciModulInline(nested_admin.NestedTabularInline):
    model = PoruchovyRiadiaciModul
    extra = 1

class VentilatorMotorInline(nested_admin.NestedTabularInline):
    model = PoruchovyVentilatorMotor
    extra = 1

class SaciaKlapkaInline(nested_admin.NestedTabularInline):
    model = PoruchovaSaciaKlapka
    extra = 1

class OstatneInline(nested_admin.NestedTabularInline):
    model = PoruchoveOstatne
    extra = 1


class PoruchovydielInline(nested_admin.NestedStackedInline):
    model = Poruchovydiel
    inlines = [
        SnimacKabelazInline,
        ElektrovyzbrojInline,
        RiadiaciModulInline,
        VentilatorMotorInline,
        SaciaKlapkaInline,
        OstatneInline,
    ]
    extra = 0
    can_delete = False
    max_num = 1

from .models import (
    SpotrebnyMaterial,
    SpotrebnyOlej,
    SpotrebnyVzduchovyFilter,
    SpotrebnyOlejovyFilter,
    SpotrebnyOlejovySeparator,
    SpotrebnyKlinovyRemen,
    SpotrebnaSpojka,
    SpotrebnaSaciaKlapka,
    SpotrebnyOdkalovac,
    SpotrebnaSadaVentilov,
    SpotrebneOstatne,
)

class OlejInline(nested_admin.NestedTabularInline):
    model = SpotrebnyOlej
    extra = 1

class VzduchovyFilterInline(nested_admin.NestedTabularInline):
    model = SpotrebnyVzduchovyFilter
    extra = 1

class OlejovyFilterInline(nested_admin.NestedTabularInline):
    model = SpotrebnyOlejovyFilter
    extra = 1

class OlejovySeparatorInline(nested_admin.NestedTabularInline):
    model = SpotrebnyOlejovySeparator
    extra = 1

class KlinovyRemenInline(nested_admin.NestedTabularInline):
    model = SpotrebnyKlinovyRemen
    extra = 1

class SpojkaInline(nested_admin.NestedTabularInline):
    model = SpotrebnaSpojka
    extra = 1

class SaciaKlapkaInline(nested_admin.NestedTabularInline):
    model = SpotrebnaSaciaKlapka
    extra = 1

class OdkalovacInline(nested_admin.NestedTabularInline):
    model = SpotrebnyOdkalovac
    extra = 1

class SadaVentilovInline(nested_admin.NestedTabularInline):
    model = SpotrebnaSadaVentilov
    extra = 1

class OstatneInline(nested_admin.NestedTabularInline):
    model = SpotrebneOstatne
    extra = 1

class SpotrebnyMaterialInline(nested_admin.NestedStackedInline):
    model = SpotrebnyMaterial
    inlines = [
        OlejInline,
        VzduchovyFilterInline,
        OlejovyFilterInline,
        OlejovySeparatorInline,
        KlinovyRemenInline,
        SpojkaInline,
        SaciaKlapkaInline,
        OdkalovacInline,
        SadaVentilovInline,
        OstatneInline,
    ]
    extra = 0
    can_delete = False
    max_num = 1



@admin.register(Sklad)
class SkladAdmin(nested_admin.NestedModelAdmin):
    inlines = [StrojInline, SpotrebnyMaterialInline, PoruchovydielInline]

    class Media:
        js = ("admin/js/custom_inline_search.js",)
