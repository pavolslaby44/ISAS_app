import copy
import json
import logging
import unicodedata

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count, Q
from django.apps import apps
from django.http import JsonResponse
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from decimal import Decimal

from django.core.exceptions import PermissionDenied

from .admin_core import (
    ISASPremiumAdmin,
    ISASReadOnlyAdminMixin,
    ISASRelatedUserFilter,
    ISASSingletonAdminMixin,
)
from .models import (
    EmailovaSprava,
    EmailovyUcet,
    EmailPriloha,
    FiremnaIdentita,
    GlobalneNastavenia,
    ISASAuditLog,
    SpotifyToken,
    StatLokalizacia,
)
from .utils import (
    get_isas_magia,
    render_isas_avatar,
    render_isas_badge,
    render_isas_flex_between,
    render_isas_header_card,
    render_isas_hidden_data,
    render_isas_key_value,
    render_isas_layout,
    render_isas_link_btn,
    render_isas_mini_stat_card,
    render_isas_premium_card,
    render_isas_singleton_card,
    render_isas_singleton_row,
    render_isas_text,
    render_isas_time_card,
)
from Evidencia.industry_loader import get_industry_term
from Evidencia.utils_helper import get_firemna_mena

logger = logging.getLogger(__name__)

# ⚙️ NASTAVENIE HLAVIČIEK ADMIN ROZHRANIA
admin.site.site_header = "ISAS CORE"
admin.site.site_title = "ISAS CORE - system of the future"
admin.site.index_title = "ISAS CORE - Plne integrovaný, automatizovaný a decentralizovaný ekosystém administratívnej správy"

if admin.site.is_registered(User):
    admin.site.unregister(User)
if admin.site.is_registered(Group):
    admin.site.unregister(Group)

class GlobalneNastaveniaForm(forms.ModelForm):
    class Meta:
        model = GlobalneNastavenia
        fields = '__all__'
        widgets = {
            'presun_notif_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'presun_notif_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'presun_notif_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'reklamacie_notif_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'reklamacie_notif_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'reklamacie_notif_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'faktury_notif_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'faktury_notif_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'faktury_notif_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'bankar_notif_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'bankar_notif_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'bankar_notif_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'objednavky_notif_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'objednavky_notif_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'objednavky_notif_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'kandidati_notif_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'kandidati_notif_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'kandidati_notif_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'servisy_notif_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'servisy_notif_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'servisy_notif_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'iot_notif_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'iot_notif_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'iot_notif_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
        }

class EmailovyUcetForm(forms.ModelForm):
    class Meta:
        model = EmailovyUcet
        fields = '__all__'
        widgets = {
            'heslo': forms.PasswordInput(render_value=True),
        }

class UzivatelRolaFilter(admin.SimpleListFilter):
    title = '🎭 Rola v systéme'
    parameter_name = 'rola_uzivatela'

    def lookups(self, request, model_admin):
        return (
            ('superadmin', '👑 Superadmin (Plný prístup)'),
            ('staff', '🛠️ Personál (Staff)'),
            ('bezny', '👤 Bežný používateľ'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'superadmin':
            return queryset.filter(is_superuser=True)
        if self.value() == 'staff':
            return queryset.filter(is_staff=True, is_superuser=False)
        if self.value() == 'bezny':
            return queryset.filter(is_staff=False, is_superuser=False)
        return queryset

@admin.register(User)
class CustomUserAdmin(ISASPremiumAdmin, UserAdmin):    
    list_display = (
        'get_identity_card', 
        'get_email_card', 
        'get_role_card', 
        'get_status_card'
    )    
    list_display_links = ('get_identity_card',)
    list_filter = (UzivatelRolaFilter, 'is_active') 
    search_fields = ("username", "first_name", "last_name", "email")

    fieldsets = (
        ('👤 Prihlasovacie údaje', {
            'fields': ('username', 'password'),
            'description': 'Základné údaje pre prístup do systému. Heslo je šifrované. 🔑'
        }),
        ('📝 Osobné informácie', {
            'fields': ('first_name', 'last_name', 'email'),
            'description': 'Kontaktné údaje a reálna identita používateľa. 📧'
        }),
        ('🚦 Stav a Rola v systéme', {
            'fields': ('is_active', 'is_staff', 'is_superuser'),
            'description': 'Aktivácia účtu a pridelenie globálnych administrátorských práv. Pozor na Superadmina! 👑'
        }),
        ('🛡️ Skupiny a Oprávnenia', {
            'fields': ('groups', 'user_permissions'),
            'description': 'Priradenie do rolí (skupín) a špecifické práva len pre tohto používateľa. 🔐'
        }),
        ('🕒 Dôležité dátumy', {
            'fields': ('last_login', 'date_joined'),
            'description': 'Časové stopy aktivity tohto účtu. ⏳'
        }),
    )

    # 🎨 ROZLOŽENIE PRI VYTVÁRANÍ NOVÉHO POUŽÍVATEĽA
    add_fieldsets = (
        ('👤 Vytvorenie nového účtu', {
            'fields': ('username', 'password', 'password2'),
            'description': 'Zadajte prihlasovacie meno a bezpečné heslo pre nového člena tímu. 🚀'
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "👥 Používatelia"
    isas_preloader_text = "⏳ Vstupujete do zoznamu používateľov..."
    isas_delete_iterate = True
    isas_delete_success_message = "✨ Úspešne odstránené: {count} používateľov!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "👤",
        'is_user_admin': True,
        'custom_buttons_html': mark_safe(
            '<button type="button" id="btn-block-users" data-action="block_users" class="isas-action-btn btn-red">🔒 Zablokovať prístup</button>'
            '<button type="button" id="btn-activate-users" data-action="activate_users" class="isas-action-btn btn-green">✅ Aktivovať účty</button>'
        ),
        'ignore_default_action_ids': ['btn-block-users', 'btn-activate-users'],
        'selector_texts': {
            'available': '📦 DOSTUPNÉ OPRÁVNENIA / SKUPINY',
            'chosen': '✅ VYBRANÉ OPRÁVNENIA / SKUPINY',
            'choose_all': '⚡ Vybrať všetky',
            'clear_all': '🗑️ Odstrániť všetky'
        }
    }

    isas_action_modals = {
        'btn-block-users': {
            'type': 'direct_action',
            'action': 'block_users',
            'confirm': True,
            'confirm_title': 'Zablokovanie používateľských účtov 🔒',
            'confirm_text': 'Naozaj chcete zablokovať prístup vybraným používateľom? Používatelia sa okamžite nebudú môcť prihlásiť do ISAS CORE.',
            'loading_text': '⏳ BLOKUJEM...',
            'success_text': '🔒 ZABLOKOVANÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jedného používateľa na zablokovanie.',
            'reload': True
        },
        'btn-activate-users': {
            'type': 'direct_action',
            'action': 'activate_users',
            'confirm': True,
            'confirm_title': 'Aktivácia používateľských účtov ✅',
            'confirm_text': 'Naozaj chcete aktivovať vybrané účty a obnoviť im prístup do systému?',
            'loading_text': '⏳ AKTIVUJEM...',
            'success_text': '✅ AKTIVOVANÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jedného používateľa na aktiváciu.',
            'reload': True
        }
    }

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        # 🛡️ EDGE CASE 1: Ochrana pred expirovanou session / neautentifikovaným dopytom
        if not getattr(request.user, 'is_authenticated', False):
            return JsonResponse({"status": "error", "message": "🚫 Relácia vypršala. Prihláste sa znova."}, status=401)

        # 🛡️ EDGE CASE 2: Ochrana sa vzťahuje na MAZANIE, BLOKOVANIE aj AKTIVÁCIU
        if action_type in ['delete', 'block_users', 'activate_users']:
            # 1. Deduplikácia a sanitizácia celých čísel s ochranou proti Buffer Overflow
            clean_ids = list(dict.fromkeys(int(i) for i in valid_ids if str(i).isdigit() and len(str(i)) <= 10))
            if not clean_ids:
                return JsonResponse({"status": "error", "message": "⚠️ Neboli vybrané žiadne platné používateľské účty."}, status=400)

            # 2. Nepriestrelná ochrana: Odstránenie VŠETKÝCH výskytov prihláseného účtu (prevencia bypassu cez duplicity)
            if action_type in ['delete', 'block_users']:
                clean_ids = [uid for uid in clean_ids if uid != request.user.id]
            
            # 3. Hierarchická ochrana: Bežný staff nesmie meniť stav ani mazať Superadminov
            if not request.user.is_superuser and clean_ids:
                superusers = set(User.objects.filter(is_superuser=True, id__in=clean_ids).values_list('id', flat=True))
                clean_ids = [uid for uid in clean_ids if uid not in superusers]

            valid_ids[:] = clean_ids

            if not valid_ids:
                return JsonResponse({
                    "status": "error", 
                    "message": "🚫 Akcia zablokovaná! Nemôžete meniť stav svojho vlastného účtu ani účtov chránených administrátorov."
                }, status=403)

        return super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs) if hasattr(super(), 'fast_action_custom_validation') else None

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        clean_ids = list(dict.fromkeys(int(i) for i in valid_ids if str(i).isdigit()))

        if action_type == 'block_users':
            if not request.user.has_perm('auth.change_user'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať používateľov."}, status=403)
            
            with transaction.atomic():
                # 🛡️ PREVENCIA DEADLOCKU: Radenie .order_by('id') zaručuje konzistentné zamykanie riadkov
                users_to_block = list(User.objects.select_for_update().filter(id__in=clean_ids, is_active=True).order_by('id'))
                if not users_to_block:
                    return JsonResponse({"status": "error", "message": "⚠️ Všetci vybraní používatelia už sú zablokovaní."}, status=400)
                
                for u in users_to_block:
                    u.is_active = False
                    u.save(update_fields=['is_active'])
            
            count = len(users_to_block)
            preskocene = len(clean_ids) - count
            info_extra = f" ({preskocene} už bolo zablokovaných predtým)" if preskocene > 0 else ""
            
            if count == 1:
                sprava = f"🔒 Úspešne zablokovaný {count} používateľský účet!{info_extra}"
            elif 2 <= count <= 4:
                sprava = f"🔒 Úspešne zablokované {count} používateľské účty!{info_extra}"
            else:
                sprava = f"🔒 Úspešne zablokovaných {count} používateľských účtov!{info_extra}"
            return JsonResponse({"status": "success", "message": sprava})

        elif action_type == 'activate_users':
            if not request.user.has_perm('auth.change_user'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať používateľov."}, status=403)
            
            with transaction.atomic():
                # 🛡️ PREVENCIA DEADLOCKU: Radenie .order_by('id') zaručuje konzistentné zamykanie riadkov
                users_to_activate = list(User.objects.select_for_update().filter(id__in=clean_ids, is_active=False).order_by('id'))
                if not users_to_activate:
                    return JsonResponse({"status": "error", "message": "⚠️ Všetci vybraní používatelia už sú aktívni."}, status=400)
                
                for u in users_to_activate:
                    u.is_active = True
                    u.save(update_fields=['is_active'])
            
            count = len(users_to_activate)
            preskocene = len(clean_ids) - count
            info_extra = f" ({preskocene} už bolo aktívnych predtým)" if preskocene > 0 else ""
            
            if count == 1:
                sprava = f"✅ Úspešne aktivovaný {count} používateľský účet!{info_extra}"
            elif 2 <= count <= 4:
                sprava = f"✅ Úspešne aktivované {count} používateľské účty!{info_extra}"
            else:
                sprava = f"✅ Úspešne aktivovaných {count} používateľských účtov!{info_extra}"
            return JsonResponse({"status": "success", "message": sprava})

    # --- 🪝 HOOKS PRE DETECTIVE SEARCH ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(username__icontains=term) | Q(first_name__icontains=term) |
            Q(last_name__icontains=term) | Q(email__icontains=term)
        ).order_by('username')[:30]

    def format_detective_result(self, obj):
        name_str = f"{obj.first_name} {obj.last_name}".strip() or obj.username
        rola = "👑 Superadmin" if obj.is_superuser else ("🛠️ Staff" if obj.is_staff else "👤 Používateľ")
        return {
            'id': obj.id, 
            'name': escape(name_str), 
            'search_value': escape(obj.username),
            'rola': rola,
            'cat': '👤 POUŽÍVATEĽ'
        }

    @admin.display(description='👤 Identita Používateľa', ordering='username')
    def get_identity_card(self, obj):
        ikona = '👑' if obj.is_superuser else ('👨‍🔧' if obj.is_staff else '👤')
        bg = '#f59e0b, #d97706' if obj.is_superuser else '#3b82f6, #1e3a8a'
        shadow = 'rgba(245, 158, 11, 0.3)' if obj.is_superuser else 'rgba(59, 130, 246, 0.3)'
        meno = f"{obj.first_name} {obj.last_name}".strip() or "Neznáme meno"
        extra = render_isas_hidden_data("user-id-hidden", obj.id)
        return render_isas_avatar(ikona, meno, f"@{obj.username}", bg, shadow, extra)

    @admin.display(description='📧 Kontakt', ordering='email')
    def get_email_card(self, obj):
        if not obj.email:
            return render_isas_badge("Nezadaný", "📭", "neutral")
        return render_isas_badge(obj.email, icon="✉️", copy_value=obj.email, wrapper=True)

    @admin.display(description='🎭 Rola v systéme')
    def get_role_card(self, obj):
        if obj.is_superuser:
            return render_isas_badge("Superadmin", "👑", "warning")
        if obj.is_staff:
            return render_isas_badge("Personál (Staff)", "🛠️", "info")
        return render_isas_badge("Bežný účet", "👤", "neutral")

    @admin.display(description='🚦 Stav', ordering='is_active')
    def get_status_card(self, obj):
        if obj.is_active:
            return render_isas_badge("Aktívny", "✅", "success")
        return render_isas_badge("Blokovaný", "❌", "danger")

class SkupinaSilaFilter(admin.SimpleListFilter):
    title = '💪 Sila oprávnení'
    parameter_name = 'sila_opravneni'

    def lookups(self, request, model_admin):
        return (
            ('prazdna', '📭 Prázdna (0 oprávnení)'),
            ('zakladna', '🥉 Základná (1-10 oprávnení)'),
            ('pokrocila', '🥈 Pokročilá (11-30 oprávnení)'),
            ('admin', '🥇 Administrátorská (31+ oprávnení)'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'prazdna':
            return queryset.filter(perm_count=0)
        if self.value() == 'zakladna':
            return queryset.filter(perm_count__gte=1, perm_count__lte=10)
        if self.value() == 'pokrocila':
            return queryset.filter(perm_count__gte=11, perm_count__lte=30)
        if self.value() == 'admin':
            return queryset.filter(perm_count__gte=31)
        return queryset

@admin.register(Group)
class CustomGroupAdmin(ISASPremiumAdmin, GroupAdmin):    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            perm_count=Count('permissions', distinct=True),
            user_count=Count('user', distinct=True)
        )

    list_display = (
        'get_identity_card', 
        'get_permissions_card', 
        'get_users_card'
    )    
    list_display_links = ('get_identity_card',)
    list_filter = (SkupinaSilaFilter,)
    search_fields = ('name',)

    fieldsets = (
        ('🛡️ Identita skupiny', {
            'fields': ('name',),
            'description': 'Základný názov systémovej roly. Zvoľte výstižný názov (napr. Dispečeri, Technici). 🏷️'
        }),
        ('🔑 Systémové oprávnenia', {
            'fields': ('permissions',),
            'description': 'Detailné nastavenie toho, čo môžu používatelia v tejto skupine v softvéri vidieť a robiť. 💪'
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "🛡️ Skupiny"
    isas_preloader_text = "⏳ Vstupujete do zoznamu skupín..."
    isas_delete_success_message = "✨ Úspešne odstránené: {count} skupín!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🛡️",
    }

    # --- 🪝 HOOKS PRE DETECTIVE SEARCH ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(name__icontains=term).order_by('name')[:30]

    def format_detective_result(self, obj):
        perm_count = getattr(obj, 'perm_count', 0)
        return {
            'id': obj.id, 
            'name': escape(obj.name), 
            'search_value': escape(obj.name),
            'rola': f"Práva: {perm_count}",
            'cat': '🛡️ SKUPINA'
        }

    @admin.display(description='👥 Identita Skupiny', ordering='name')
    def get_identity_card(self, obj):
        extra = render_isas_hidden_data("group-id-hidden", obj.id)
        return render_isas_avatar("🛡️", obj.name.upper(), "Systémová rola", "#8b5cf6, #6d28d9", "rgba(109, 40, 217, 0.3)", extra)

    @admin.display(description='💪 Sila Oprávnení', ordering='perm_count')
    def get_permissions_card(self, obj):
        perm_count = getattr(obj, 'perm_count', 0)
        if perm_count == 0:
            return render_isas_badge("Prázdna (0 Práv)", "📭", "neutral")
        elif perm_count <= 10:
            return render_isas_badge(f"Základná ({perm_count} Práv)", "🥉", "success")
        elif perm_count <= 30:
            return render_isas_badge(f"Pokročilá ({perm_count} Práv)", "🥈", "info")
        return render_isas_badge(f"Admin ({perm_count} Práv)", "🥇", "danger")

    @admin.display(description='👤 Priradení Používatelia', ordering='user_count')
    def get_users_card(self, obj):
        user_count = getattr(obj, 'user_count', 0)
        if user_count > 0:
            return render_isas_badge(f"{user_count} Aktívnych", "👥", "warning")
        return render_isas_badge("Nikto", "👻", "neutral")

@admin.register(FiremnaIdentita)
class FiremnaIdentitaAdmin(ISASSingletonAdminMixin, ISASPremiumAdmin):
    list_display = (
        'get_nazov_vizual', 
        'get_adresy_styled', 
        'get_banka_dph_styled', 
        'get_komunikacia_styled', 
        'get_dokumenty_styled'
    )
    list_display_links = ('get_nazov_vizual',)
    autocomplete_fields = ['zodpovedny_zamestnanec_sklad']
    list_select_related = ('zodpovedny_zamestnanec_sklad',)
    
    fieldsets = (
        ('🏢 1. ZÁKLADNÉ IDENTIFIKAČNÉ ÚDAJE', {
            'description': '<b>💡 ISAS Nápoveda:</b> Tu definujete základné piliere vašej spoločnosti. Tieto údaje sa budú automaticky ťahať do všetkých oficiálnych dokumentov, zmlúv a faktúr. Dbajte na ich presnosť! 📄✨',
            'fields': ('nazov_spolocnosti', 'podtitul', ('ico', 'dic'), ('ic_dph', 'je_platca_dph'))
        }),
        ('📍 2. ADRESY', {
            'description': '<b>🗺️ Logistika a Sídlo:</b> Kde vás nájdeme? Presné adresy sú kľúčové pre správnu fakturáciu, doručovanie pošty a navigáciu technikov. 🚚🏢',
            'fields': (('sidlo_ulica', 'sidlo_cislo'), ('sidlo_psc', 'sidlo_mesto', 'sidlo_stat'), 'koresp_adresa', 'adresa_prevadzky', 'veduci_skladu', 'zodpovedny_zamestnanec_sklad', ('telefon_sklad', 'email_sklad'))
        }),
        ('🏦 3. BANKOVÉ SPOJENIE', {
            'description': '<b>💰 Tok peňazí:</b> Nastavte si hlavný bankový účet. Tieto údaje sa vygenerujú aj do QR kódov na vašich faktúrach pre bleskové platby od klientov. 💳📲',
            'fields': ('iban', ('swift', 'nazov_banky'))
        }),
        ('📞 4. KONTAKTNÉ INFORMÁCIE', {
            'description': '<b>☎️ Komunikačné kanály a dispečing:</b> Správa webu, kontaktov figurujúcich v hlavičkách oficiálnych tlačív a mailových prepojení s účtovnou automatizáciou.',
            'fields': (
                ('telefon', 'email_vseobecny'), 
                ('email_uctovnicky', 'email_fakturacia'), 
                ('web',)
            )
        }),
        ('⚖️ 5. LEGISLATÍVNE ÚDAJE', {
            'description': '<b>📜 Zákonná povinnosť:</b> Tento text (napr. zápis v obchodnom alebo živnostenskom registri) sa automaticky vloží do pätičky vašich faktúr presne tak, ako to vyžaduje zákon. 🏛️⚖️',
            'fields': ('zapis_v_registri',)
        }),
        ('👥 6. PERSONÁLNE ÚDAJE', {
            'description': '<b>👔 Ľudský faktor:</b> Kto za tým všetkým stojí? Nastavte štatutára a osobu, ktorej meno sa bude predvolene tlačiť do kolónky "Vystavil". ✍️👤',
            'fields': ('konatel', 'vystavuje_faktury', 'vystavuje_ponuky', 'vystavuje_objednavky', 'vystavuje_servis_vykazy', 'vystavuje_vstupky')
        }),
        ('🎨 7. VIZUÁL A DIZAJN', {
            'description': '<b>✨ Vaša značka:</b> Nahrajte svoje logo a pečiatku s podpisom (ideálne PNG bez pozadia). Systém ich automaticky aplikuje na všetky exportované PDF dokumenty. 🖼️🖨️',
            'fields': ('logo', 'peciatka_podpis')
        }),
        ('⚙️ 8. OBCHODNÉ NASTAVENIA', {
            'description': '<b>⚙️ Pravidlá hry:</b> Ako obchodujete? Nastavte si štandardnú dobu splatnosti a menu. Tieto hodnoty sa predvyplnia pri každej novej faktúre. ⏳💰',
            'fields': (('predvolena_platnost_ponuk', 'splatnost_faktur', 'predvolena_mena'),)
        }),
        ('🌍 9. SOCIÁLNE SIETE', {
            'description': '<b>🌐 Digitálna stopa:</b> Prepojte svoj systém s vašimi sociálnymi sieťami. Odkazy môžu byť použité v klientskej zóne alebo v e-mailových šablónach. 📱🔗',
            'fields': ('facebook', 'instagram', 'linkedin')
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "🏢 Profil spoločnosti"
    isas_preloader_text = "⏳ Vstupujete do sekcie profilu vašej spoločnosti..."
    isas_enable_detective = False
    isas_magia_kwargs = {
        'typ_zobrazenia': 'karta',
        'live_search_endpoint': None,
        'vyzaduje_potvrdenie': True,
        'potvrdenie_text': "Tento krok natrvalo odstráni nastavené parametre a hodnoty z jadra systému. 💥",
    }

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if not request.user.is_superuser:
            return JsonResponse({"status": "error", "message": "❌ Resetovať firemnú identitu môže iba Superadmin!"}, status=403)
        return None

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        with transaction.atomic():
            zaznamy = self.model.objects.select_for_update().filter(id__in=valid_ids)
            
            # 1. Fyzické zmazanie súborov z disku (Django to samo neurobí)
            for zaznam in zaznamy:
                if zaznam.logo: zaznam.logo.delete(save=False)
                if zaznam.peciatka_podpis: zaznam.peciatka_podpis.delete(save=False)
                
            # 2. Zmazanie z DB. ISASAuditQuerySet sa automaticky postará o vytvorenie Audit Logu!
            zaznamy.delete()
            
        return JsonResponse({"status": "success", "message": "✨ Firemná identita bola úspešne resetovaná! Systém je čistý. 🧹"})
    
    @admin.display(description='🏢 Core Identita')
    def get_nazov_vizual(self, obj):
        nazov = obj.nazov_spolocnosti or 'Nenastavené'
        badges_elements = [
            render_isas_badge(f"IČO: {obj.ico or '-'}", "🆔", "neutral", wrapper=False),
            render_isas_badge(f"DIČ: {obj.dic or '-'}", "📊", "neutral", wrapper=False),
            render_isas_badge(f"Konateľ: {obj.konatel or 'Nenastavené'}", "👔", "success", wrapper=False),
        ]
        if obj.podtitul:
            badges_elements.append(render_isas_badge(obj.podtitul, "📢", "info", wrapper=False))

        badges_html = mark_safe("".join(badges_elements))
        badges_layout = render_isas_layout(badges_html, layout="row", gap=8)
        return render_isas_header_card("🏢", nazov, badges_layout)

    @admin.display(description='📍 Adresy a Lokácie')
    def get_adresy_styled(self, obj):
        adresa_riadok1 = f"{obj.sidlo_ulica or ''} {obj.sidlo_cislo or ''}".strip()
        adresa_riadok2 = f"{obj.sidlo_psc or ''} {obj.sidlo_mesto or ''}".strip()
        casti_sidla = [c for c in [adresa_riadok1, adresa_riadok2, obj.sidlo_stat] if c]
        sidlo = ", ".join(casti_sidla) or "Nenastavené"
        koresp = obj.koresp_adresa if obj.koresp_adresa else render_isas_text("Zhodná so sídlom", muted=True)
        prevadzka = obj.adresa_prevadzky if obj.adresa_prevadzky else render_isas_text("Nenastavená", muted=True)
        zodpovedny = str(obj.zodpovedny_zamestnanec_sklad) if obj.zodpovedny_zamestnanec_sklad else (obj.veduci_skladu or 'Nenastavené')
        kontakt_sklad = f"{obj.telefon_sklad or ''} {obj.email_sklad or ''}".strip() or 'Bez kontaktu'

        r1 = render_isas_singleton_row(render_isas_key_value("Sídlo", sidlo, icon="🏢"), is_dashed=False)
        r2 = render_isas_singleton_row(render_isas_key_value("Korešpondenčná adresa", koresp, icon="📬"))
        r3 = render_isas_singleton_row(render_isas_key_value("Prevádzka / Sklad", prevadzka, subtext=f"👷‍♂️ Zodpovedný: {zodpovedny} | 📞 {kontakt_sklad}", icon="🏭"))
        
        return render_isas_singleton_card("Sídlo spoločnosti", "📍", "#64748b", format_html('{}{}{}', r1, r2, r3))

    @admin.display(description='📞 Komunikácia a Siete')
    def get_komunikacia_styled(self, obj):
        tel = obj.telefon or '-'
        mail_v = obj.email_vseobecny or '-'
        mail_f = obj.email_fakturacia if obj.email_fakturacia else render_isas_text("Zhodný so všeobecným", muted=True)
        mail_u = obj.email_uctovnicky if obj.email_uctovnicky else render_isas_text("Nenastavený", muted=True)
        web = obj.web if obj.web else render_isas_text("Nenastavený", muted=True)

        siete = []
        if obj.facebook: siete.append(render_isas_link_btn(obj.facebook, "Facebook", icon="📘", type_color="info", wrapper=False))
        if obj.instagram: siete.append(render_isas_link_btn(obj.instagram, "Instagram", icon="📸", type_color="danger", wrapper=False))
        if obj.linkedin: siete.append(render_isas_link_btn(obj.linkedin, "LinkedIn", icon="💼", type_color="info", wrapper=False))
        
        siete_html = render_isas_layout(mark_safe("".join(siete)), layout="row", gap=8) if siete else render_isas_text("Siete nenastavené", muted=True)

        obsah = render_isas_layout(format_html('{}{}{}{}{}{}',
            render_isas_key_value("Telefón", tel, icon="📞"),
            render_isas_key_value("Všeobecný E-mail", mail_v, icon="✉️"),
            render_isas_key_value("Fakturácia", mail_f, icon="📨"),
            render_isas_key_value("AI Účtovníctvo", mail_u, icon="🤖"),
            render_isas_key_value("Web", web, icon="🌐"),
            render_isas_key_value("Sociálne siete", siete_html, icon="📱")
        ), layout="col", gap=8)
        
        return render_isas_singleton_card("Kontakty", "📞", "#334155", render_isas_singleton_row(obsah, is_dashed=False))

    @admin.display(description='💳 Financie a DPH')
    def get_banka_dph_styled(self, obj):
        iban = obj.iban or 'Nenastavené'
        swift = obj.swift or '-'
        banka = obj.nazov_banky or '-'
        
        dph_badge = render_isas_badge("PLATCA DPH", "🟢", "success", wrapper=False) if obj.je_platca_dph else render_isas_badge("NEPLATCA", "⚪", "neutral", wrapper=False)
        ic_dph_text = f"🌍 IČ DPH: {obj.ic_dph}" if (obj.je_platca_dph and obj.ic_dph) else ""

        r1 = render_isas_premium_card(
            "🏦", "Hlavný účet", f"💳 {iban}", banka, f"SWIFT: {swift}", 
            "#0f766e", "#0d9488", "rgba(13,148,136,0.2)"
        )
        
        mena_displej = obj.predvolena_mena or obj.get_country_defaults().get('mena') or get_firemna_mena()
        r2_info = render_isas_text(f"⏳ FA: {obj.splatnost_faktur} dní | PO: {obj.predvolena_platnost_ponuk} dní | 💰 {mena_displej}", size=11, color="#334155", weight=700, align="right")
        r2_content = render_isas_flex_between(dph_badge, r2_info)
        
        if ic_dph_text:
            r2_content = render_isas_layout(format_html('{}{}', r2_content, render_isas_key_value("Daňové identifikačné číslo", ic_dph_text)), layout="col", gap=6)
            
        r2 = render_isas_singleton_row(r2_content)
        
        return render_isas_layout(format_html('{}{}', r1, r2), layout="col", gap=10)

    @admin.display(description='📜 Dokumenty a Nastavenia')
    def get_dokumenty_styled(self, obj):
        zapis = obj.zapis_v_registri or 'Zápis v registri nie je vyplnený.'
        logo_status = '✅ Nahraté' if obj.logo else '❌ Chýba'
        pec_status = '✅ Nahraté' if obj.peciatka_podpis else '❌ Chýba'

        r1 = render_isas_singleton_row(render_isas_text(f'"{zapis}"', muted=True), is_dashed=False)
        
        grid_items = format_html('{}{}{}{}{}',
            render_isas_key_value("Faktúry", obj.vystavuje_faktury or '-', icon="🧾"),
            render_isas_key_value("Ponuky", obj.vystavuje_ponuky or '-', icon="📄"),
            render_isas_key_value("Objednávky", obj.vystavuje_objednavky or '-', icon="🛒"),
            render_isas_key_value("Servis", obj.vystavuje_servis_vykazy or '-', icon="🔧"),
            render_isas_key_value("Vstupky", obj.vystavuje_vstupky or '-', icon="🚪")
        )
        
        assets = render_isas_layout(format_html('{}{}', 
            render_isas_key_value("Logo", logo_status, icon="🎨"),
            render_isas_key_value("Pečiatka", pec_status, icon="🔏")
        ), layout="row", gap=16)
        
        r2 = render_isas_singleton_row(
            render_isas_layout(format_html('{}{}', render_isas_layout(grid_items, layout="grid", columns=2), assets), layout="col", gap=12)
        )

        return render_isas_singleton_card("Zápis v registri (Pätička)", "📜", "#64748b", format_html('{}{}', r1, r2))

@admin.register(GlobalneNastavenia)
class GlobalneNastaveniaAdmin(ISASSingletonAdminMixin, ISASPremiumAdmin):
    form = GlobalneNastaveniaForm

    autocomplete_fields = ['jit_kontrolovane_filialky']    
    list_display = (
        'get_core_vizual', 
        'get_financie_styled', 
        'get_servis_cesty_styled', 
        'get_ai_notifikacie_styled', 
        'get_infrastruktura_styled'
    )
    list_display_links = ('get_core_vizual',)

    fieldsets = (
        ('💰 1. DANE A FAKTURÁCIA', {
            'description': '<b>💡 ISAS Nápoveda:</b> Nastavte si základné a znížené sadzby DPH, ako aj úrok z omeškania. Tieto hodnoty sa automaticky aplikujú pri tvorbe faktúr. 📊💸',
            'fields': (('zakladna_sadzba_dph', 'znizena_sadzba_dph'), 'sadzba_uroku_omeskanie')
        }),
        ('🚚 2. FAKTURÁCIA DOPRAVY ZÁKAZNÍKOM (Výnosy)', {
            'description': '<b>🛣️ Účtovanie dopravy:</b> Koľko si účtujete za výjazd k zákazníkovi? Nastavte sadzbu za kilometer a čas strávený na ceste. 🚐💰',
            'fields': (('sadzba_za_km_zakaznik', 'sadzba_za_cas_na_ceste'),)
        }),
        ('🚗 3. CESTOVNÉ NÁHRADY (Interné náklady)', {
            'description': '<b>🍔 Interné výdavky:</b> Zákonné sadzby pre preplácanie kilometrov a stravného vašim zamestnancom. Tieto dáta slúžia pre modul cestovných príkazov. 🗺️🧾',
            'fields': ('nahrada_za_1km', ('stravne_5_12h', 'stravne_12_18h', 'stravne_nad_18h'))
        }),
        ('🛠️ 4. SERVIS A PRÁCA', {
            'description': '<b>⚙️ Hodnota práce:</b> Definujte hodinovú sadzbu technika, príplatky za pohotovosť a podmienky ich uplatnenia. Minimálna účtovacia jednotka (takt) zaokrúhľuje čas – nechajte prázdne pre presné minútové účtovanie. 🔧⏱️',
            'fields': (
                ('hodinova_sadzba_technik', 'pohotovostny_priplatok'),
                'pohotovostny_priplatok_popis',
                'min_uctovacia_jednotka'
            )
        }),
        ('⚙️ 5. SYSTÉM', {
            'description': '<b>🚧 Správa jadra:</b> Tu môžete zapnúť režim údržby (odstaví klientsku zónu), nastaviť globálny informačný banner a konfigurovať dobu retencie pre automatickú archiváciu auditných logov (v dňoch, min. 90 dní). 📢🛑🛡️',
            'fields': ('globalny_banner', 'rezim_udrzby', 'audit_log_retencia_dni')
        }),
        ('🤖 6. AI ROBOTI A IMAP E-MAILY', {
            'description': '<b>🧠 Umelá Inteligencia:</b> Zadajte prístupové údaje pre čítanie e-mailov (Poštár pre faktúry, Bankár pre úhrady). Kým polia nie sú vyplnené, AI je pozastavená. 📧🤖',
            'fields': (
                ('imap_server_faktury', 'email_pre_faktury'), 'heslo_pre_faktury',
                ('faktury_notif_tg', 'faktury_notif_discord', 'faktury_notif_mail'),
                ('imap_server_banka', 'email_pre_banku'), 'heslo_pre_banku',
                ('bankar_notif_tg', 'bankar_notif_discord', 'bankar_notif_mail'),
                ('imap_server_objednavky', 'email_pre_objednavky'), 'heslo_pre_objednavky',
                ('objednavky_notif_tg', 'objednavky_notif_discord', 'objednavky_notif_mail'),
                ('imap_server_reklamacie', 'email_pre_reklamacie'), 'heslo_pre_reklamacie',
                ('reklamacie_notif_tg', 'reklamacie_notif_discord', 'reklamacie_notif_mail')
            )
        }),
        ('📱 7. NOTIFIKÁCIE MAJITEĽA', {
            'description': '<b>🚨 C-Level Alerty:</b> Kontaktné údaje pre majiteľa. Sem budú chodiť denné zhrnutia, žiadosti o schválenie a krízové alerty z AI. 📲👔',
            'fields': (
                ('email_majitel_notifikacie', 'telegram_id_majitel', 'discord_id_majitel'),
                ('jit_kontrolovat_vsetky_sklady', 'jit_kontrolovat_hlavny_sklad'),
                'jit_kontrolovane_filialky',
                ('servisy_notif_tg', 'servisy_notif_discord', 'servisy_notif_mail'),
                ('iot_notif_tg', 'iot_notif_discord', 'iot_notif_mail'),
                ('presun_notif_tg', 'presun_notif_discord', 'presun_notif_mail'),
                ('kandidati_notif_tg', 'kandidati_notif_discord', 'kandidati_notif_mail')
            )
        }),
        ('🌍 8. INFRAŠTRUKTÚRA (ISAS CORE)', {
            'description': '<b>🔌 Pripojenie k svetu:</b> Globálna konfigurácia brán: odosielanie e-mailov (SMTP), C-Level boti (Telegram / Discord) a európska eFakturácia (Peppol AP / Broker). Pre Peppol zadávajte vždy plnú URL brány (https://www...) a Participant ID s EAS schémou (napr. 0245:1234567890). 🌐✉️🇪🇺',
            'fields': (
                ('smtp_host', 'smtp_port', 'smtp_use_tls'),
                ('smtp_user', 'smtp_password'),
                ('gemini_api_key', 'google_maps_api_key'),
                ('telegram_bot_token', 'discord_bot_token'), 
                'url_systemu',
                'efaktura_provider',
                ('peppol_api_url', 'peppol_participant_id'),
                ('peppol_client_id', 'peppol_client_secret')
            )
        }),
        ('📊 9. AI LIMITY POUŽÍVATEĽOV', {
            'description': '<b>🤖 Denné kvóty:</b> Nastavte maximálny počet správ, ktoré môžu jednotlivé roly v systéme prekonzumovať za jeden deň. Chráni váš rozpočet pred vysokými nákladmi na API. 🛑💸',
            'fields': (
                ('ai_limit_superadmin', 'ai_limit_management'),
                ('ai_limit_technik', 'ai_limit_default'),
            )
        }),
        ('🎵 10. ISAS JUKEBOX (SPOTIFY)', {
            'description': '<b>🎧 Integrácia hudby:</b> Prepojenie so Spotify Web API. Tieto kľúče získate na portáli developer.spotify.com. Kľúč "Secret" je v databáze prísne šifrovaný. 🎶🔐',
            'fields': (
                ('spotify_client_id', 'spotify_client_secret'),
            )
        }),
        ('👥 11. HR A KAPACITNÉ PLÁNOVANIE', {
            'description': '<b>📊 Kapacity technikov:</b> Nastavte firemné štandardy pre maximálne denné zaťaženie technikov a štandardnú pracovnú dobu. Tieto hodnoty riadia farby na HR Dashboarde a výpočet voľných termínov na webe.',
            'fields': (
                ('limit_uloh_zvysena', 'limit_uloh_kriticka'),
                ('pracovny_cas_od', 'pracovny_cas_do'),
                ('pracovat_vikendy', 'pracovat_sviatky'),
            )
        }),
        ('🛒 12. PREDIKCIA NÁKUPU A AI AUDITOR', {
            'description': '<b>🤖 AI a Logistika:</b> Nastavenie časového okna pre pondelkový report chýbajúceho materiálu a správa AI strážneho psa pre dispečing.',
            'fields': (
                ('zapnut_predikciu_nakupu', 'dni_predikcie_nakupu'),
                'zapnut_ai_upozornenia_dispecingu',
            )
        }),
        ('⚖️ 13. STRÁŽCA SERVISOV A PREDSTIHY NOTIFIKÁCIÍ', {
            'description': '<b>🛡️ Procesné predstihy:</b> Nastavenie predstihu notifikácií o blížiacich sa servisoch a revíziách v dňoch a prevádzkových jednotkách (meradlách), vrátane lehoty na vybavenie reklamácie.',
            'fields': (
                ('servisy_sledovat_legislativu', 'servisy_predstih_upozornenia_dni', 'servisy_predstih_motohodiny'),
                'standardna_lehota_reklamacie_dni',
            )
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "⚙️ Globálne nastavenie"
    isas_preloader_text = "⏳ Vstupujete do sekcie globálnych nastavení systému..."
    isas_enable_detective = False
    isas_delete_success_message = "✨ Systémové premenné boli úspešne premazané! 🧹"
    isas_magia_kwargs = {
        'typ_zobrazenia': 'karta',
        'live_search_endpoint': None,
        'vyzaduje_potvrdenie': True,
        'potvrdenie_text': "Tento krok natrvalo vymaže globálne nastavenia a vráti systém do výrobného stavu. 💥",
    }
    isas_modern_toggles = True

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if not request.user.is_superuser:
            return JsonResponse({"status": "error", "message": "❌ Zásah do jadra systému povolený iba pre Superadmina!"}, status=403)
        return None

    @admin.display(description='⚙️ Core Systém')
    def get_core_vizual(self, obj):
        status_badge = render_isas_badge("ÚDRŽBA AKTÍVNA", "🚧", "danger", wrapper=False) if obj.rezim_udrzby else render_isas_badge("SYSTÉM ONLINE", "✅", "success", wrapper=False)
        banner_text = obj.globalny_banner if obj.globalny_banner else render_isas_text("Žiadny aktívny banner", muted=True)

        try:
            from Evidencia.models import Sklad
            hlavny_sklad = Sklad.objects.first()
            nazov_hlavneho = str(hlavny_sklad) if hlavny_sklad else "Hlavný sklad"
        except Exception:
            nazov_hlavneho = "Hlavný sklad"

        jit_status = "Všetky sklady" if obj.jit_kontrolovat_vsetky_sklady else (f"{nazov_hlavneho} + Filiálky" if obj.jit_kontrolovat_hlavny_sklad else "Iba Filiálky")
        predikcia_status = f"Zapnutá ({obj.dni_predikcie_nakupu} dní)" if getattr(obj, 'zapnut_predikciu_nakupu', True) else "Vypnutá"
        retencia_status = f"{getattr(obj, 'audit_log_retencia_dni', 730)} dní"

        obsah = render_isas_layout(format_html('{}{}{}{}{}',
            status_badge,
            render_isas_key_value("Banner", banner_text, icon="📢"),
            render_isas_key_value("JIT Logistika", jit_status, icon="📦"),
            render_isas_key_value("Predikcia nákupu", predikcia_status, icon="🛒"),
            render_isas_key_value("Retencia audit logov", retencia_status, icon="🛡️")
        ), layout="col", gap=8)
        
        return render_isas_header_card("⚙️", "ISAS CORE", obsah)

    @admin.display(description='🤖 AI a Notifikácie')
    def get_ai_notifikacie_styled(self, obj):
        def notif_summary(tg, dc, mail):
            return f"TG: {'✅' if tg else '❌'} | DC: {'✅' if dc else '❌'} | ✉️: {'✅' if mail else '❌'}"

        ai_faktury = f"✅ ({obj.imap_server_faktury})" if obj.email_pre_faktury and obj.heslo_pre_faktury else '❌'
        ai_banka = f"✅ ({obj.imap_server_banka})" if obj.email_pre_banku and obj.heslo_pre_banku else '❌'
        ai_objednavky = f"✅ ({obj.imap_server_objednavky})" if obj.email_pre_objednavky and obj.heslo_pre_objednavky else '❌'
        ai_reklamacie = f"✅ ({obj.imap_server_reklamacie})" if obj.email_pre_reklamacie and obj.heslo_pre_reklamacie else '❌'
        
        majitel_mail = obj.email_majitel_notifikacie if obj.email_majitel_notifikacie else render_isas_text("Nenastavený", muted=True)
        majitel_tg = obj.telegram_id_majitel if obj.telegram_id_majitel else render_isas_text("Nenastavené", muted=True)
        majitel_dc = obj.discord_id_majitel if obj.discord_id_majitel else render_isas_text("Nenastavené", muted=True)

        r1_obsah = render_isas_layout(format_html('{}{}{}{}',
            render_isas_flex_between(f"🧾 Faktúry: {ai_faktury}", notif_summary(obj.faktury_notif_tg, obj.faktury_notif_discord, obj.faktury_notif_mail)),
            render_isas_flex_between(f"🏦 Banka: {ai_banka}", notif_summary(obj.bankar_notif_tg, obj.bankar_notif_discord, obj.bankar_notif_mail)),
            render_isas_flex_between(f"🛒 Objednávky: {ai_objednavky}", notif_summary(obj.objednavky_notif_tg, obj.objednavky_notif_discord, obj.objednavky_notif_mail)),
            render_isas_flex_between(f"⚠️ Reklamácie: {ai_reklamacie}", notif_summary(obj.reklamacie_notif_tg, obj.reklamacie_notif_discord, obj.reklamacie_notif_mail))
        ), layout="col", gap=4)
        
        auditor_status = "🔔 Aktívny" if getattr(obj, 'zapnut_ai_upozornenia_dispecingu', True) else "🔕 Vypnutý"
        
        r1_limity = render_isas_layout(format_html('{}{}',
            render_isas_key_value("Limity (S/M/T/D)", f"{obj.ai_limit_superadmin} / {obj.ai_limit_management} / {obj.ai_limit_technik} / {obj.ai_limit_default}"),
            render_isas_key_value("AI Dispatch Auditor", auditor_status, icon="🚨")
        ), layout="col", gap=4)
        
        r1 = render_isas_singleton_row(format_html('{}{}', r1_obsah, r1_limity), is_dashed=False)

        leg_status = "🟢 Aktívny" if getattr(obj, 'servisy_sledovat_legislativu', True) else "🔴 Vypnutý"
        predstih_dni = getattr(obj, 'servisy_predstih_upozornenia_dni', 14)
        predstih_mh = getattr(obj, 'servisy_predstih_motohodiny', 100)
        meradlo_jednotka = get_industry_term('meter_unit', 'hod.')

        # 🛡️ DEFENSIVE: Štandardná reklamačná lehota voči NoneType, prázdnym hodnotám a neplatným typom
        raw_lehota_rek = getattr(obj, 'standardna_lehota_reklamacie_dni', None)
        try:
            lehota_rek_val = int(raw_lehota_rek) if raw_lehota_rek is not None else None
            lehota_rek_str = f"{lehota_rek_val} dní" if (lehota_rek_val is not None and lehota_rek_val > 0) else "Neurčená (zmluvná)"
        except (ValueError, TypeError):
            lehota_rek_str = "Neurčená (zmluvná)"

        r2_obsah = render_isas_layout(format_html('{}{}{}{}{}{}{}{}{}',
            render_isas_key_value("E-mail", majitel_mail, icon="✉️"),
            render_isas_key_value("TG ID", majitel_tg, icon="📲"),
            render_isas_key_value("Discord ID", majitel_dc, icon="🎮"),
            render_isas_key_value("Servisy & Revízie", f"TG: {'✅' if getattr(obj, 'servisy_notif_tg', True) else '❌'} | DC: {'✅' if getattr(obj, 'servisy_notif_discord', True) else '❌'} | Mail: {'✅' if getattr(obj, 'servisy_notif_mail', True) else '❌'}"),
            render_isas_key_value("IoT Havárie", f"TG: {'✅' if getattr(obj, 'iot_notif_tg', True) else '❌'} | DC: {'✅' if getattr(obj, 'iot_notif_discord', True) else '❌'} | Mail: {'✅' if getattr(obj, 'iot_notif_mail', True) else '❌'}"),
            render_isas_key_value("Legislatíva & Servis", f"{leg_status} (Predstih: {predstih_dni} dň. | {predstih_mh} {meradlo_jednotka})", icon="⚖️"),
            render_isas_key_value("Lehota reklamácií", lehota_rek_str, icon="🛡️"),
            render_isas_key_value("Presuny", f"TG: {'✅' if obj.presun_notif_tg else '❌'} | DC: {'✅' if obj.presun_notif_discord else '❌'} | Mail: {'✅' if obj.presun_notif_mail else '❌'}"),
            render_isas_key_value("Kariéra", f"TG: {'✅' if obj.kandidati_notif_tg else '❌'} | DC: {'✅' if obj.kandidati_notif_discord else '❌'} | Mail: {'✅' if obj.kandidati_notif_mail else '❌'}")
        ), layout="col", gap=4)
        r2 = render_isas_singleton_row(r2_obsah)

        return render_isas_singleton_card("AI Roboti (IMAP & Notifikácie)", "🧠", "#10b981", format_html('{}{}', r1, r2))

    @admin.display(description='🌍 Infraštruktúra')
    def get_infrastruktura_styled(self, obj):
        smtp_status = f"✅ (Port: {obj.smtp_port}, TLS: {'✅' if obj.smtp_use_tls else '❌'})" if obj.smtp_host and obj.smtp_user else '❌'
        
        provider = getattr(obj, 'efaktura_provider', 'DIRECT_AP')
        provider_nazov = "SAPI-SK" if provider == 'DIRECT_SAPI_SK' else ("Storecove" if provider == 'BROKER_STORECOVE' else ("Pagero" if provider == 'BROKER_PAGERO' else "Peppol AP"))
        if obj.peppol_api_url and (obj.peppol_client_id or obj.peppol_client_secret):
            peppol_status = f"✅ {provider_nazov} (ID: {obj.peppol_participant_id or '-'})"
        else:
            peppol_status = '❌'

        url = obj.url_systemu if obj.url_systemu else render_isas_text("Nenastavená", muted=True)

        grid_items = format_html('{}{}{}{}{}{}{}',
            render_isas_key_value("SMTP", smtp_status, icon="📧"),
            render_isas_key_value("eFaktúra", peppol_status, icon="🇪🇺"),
            render_isas_key_value("TG", '✅' if obj.telegram_bot_token else '❌', icon="🤖"),
            render_isas_key_value("Discord", '✅' if obj.discord_bot_token else '❌', icon="🎮"),
            render_isas_key_value("Gemini", '✅' if obj.gemini_api_key else '❌', icon="🧠"),
            render_isas_key_value("Maps", '✅' if obj.google_maps_api_key else '❌', icon="🗺️"),
            render_isas_key_value("Spotify", '✅' if obj.spotify_client_id else '❌', icon="🎵")
        )

        r1 = render_isas_singleton_row(render_isas_layout(grid_items, layout="grid", columns=2, gap=8), is_dashed=False)
        r2 = render_isas_singleton_row(render_isas_key_value("URL Systému", url, icon="🌐"))

        return render_isas_singleton_card("Pripojenie k svetu", "🔌", "#3b82f6", format_html('{}{}', r1, r2))

    @admin.display(description='💰 Financie a Doprava')
    def get_financie_styled(self, obj):
        identita = FiremnaIdentita.objects.first()
        mena = (identita.predvolena_mena if identita and identita.predvolena_mena else None) or FiremnaIdentita.get_country_defaults().get('mena') or get_firemna_mena()

        dph_z = obj.zakladna_sadzba_dph if obj.zakladna_sadzba_dph is not None else obj.get_zakladna_dph()
        dph_zn = obj.znizena_sadzba_dph if obj.znizena_sadzba_dph is not None else obj.get_znizena_dph()
        urok = getattr(obj, 'sadzba_uroku_omeskanie', None)
        urok_str = f"{urok}% / deň" if urok is not None else "Nenastavený"
        km = obj.sadzba_za_km_zakaznik if obj.sadzba_za_km_zakaznik is not None else '0.00'
        cas = obj.sadzba_za_cas_na_ceste if obj.sadzba_za_cas_na_ceste is not None else '0.00'

        r1 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between(f"Základná: {dph_z}%", f"Znížená: {dph_zn}%"),
            render_isas_key_value("Úrok z omeškania", urok_str)
        ), layout="col", gap=4), is_dashed=False)
        
        r2 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("Sadzba za 1 km:", f"{km} {mena}"),
            render_isas_flex_between("Čas na ceste:", f"{cas} {mena}/h")
        ), layout="col", gap=4))

        return render_isas_singleton_card("Sadzby DPH a Úroky", "📊", "#9333ea", format_html('{}{}', r1, r2))

    @admin.display(description='🛠️ Servis a Cesty')
    def get_servis_cesty_styled(self, obj):
        identita = FiremnaIdentita.objects.first()
        mena = (identita.predvolena_mena if identita and identita.predvolena_mena else None) or FiremnaIdentita.get_country_defaults().get('mena') or get_firemna_mena()

        sadzba = obj.hodinova_sadzba_technik if obj.hodinova_sadzba_technik is not None else '0.00'
        pohotovost = obj.pohotovostny_priplatok if obj.pohotovostny_priplatok is not None else '0.00'
        
        # 🛡️ DEFENSIVE: Odstránenie hardkódovaného fallbacku 30 minút + bezpečné escapovanie podmienok
        min_jednotka_str = f"{obj.min_uctovacia_jednotka} min." if (getattr(obj, 'min_uctovacia_jednotka', None) and obj.min_uctovacia_jednotka > 1) else "Presný čas (bez zaokrúhľovania)"
        popis_pohotovost_raw = getattr(obj, 'pohotovostny_priplatok_popis', None)
        popis_pohotovost_str = f" ({escape(popis_pohotovost_raw[:50])}{'...' if len(popis_pohotovost_raw) > 50 else ''})" if popis_pohotovost_raw else " (Podľa pracovného času)"
        
        km_nahrada = obj.nahrada_za_1km if obj.nahrada_za_1km is not None else '0.000'
        stravne = obj.stravne_5_12h if obj.stravne_5_12h is not None else '0.00'
        stravne_12 = obj.stravne_12_18h if obj.stravne_12_18h is not None else '0.00'
        stravne_nad = obj.stravne_nad_18h if obj.stravne_nad_18h is not None else '0.00'
        limit_z = obj.limit_uloh_zvysena if obj.limit_uloh_zvysena else 3
        limit_k = obj.limit_uloh_kriticka if obj.limit_uloh_kriticka else 5
        
        cas_od = obj.pracovny_cas_od.strftime('%H:%M') if getattr(obj, 'pracovny_cas_od', None) else "07:30"
        cas_do = obj.pracovny_cas_do.strftime('%H:%M') if getattr(obj, 'pracovny_cas_do', None) else "16:00"
        vikendy = "✅ Áno" if getattr(obj, 'pracovat_vikendy', False) else "❌ Nie"
        sviatky = "✅ Áno" if getattr(obj, 'pracovat_sviatky', False) else "❌ Nie"

        r1 = render_isas_singleton_row(render_isas_layout(format_html('{}{}{}',
            render_isas_flex_between("Sadzba technika:", f"{sadzba} {mena}/h"),
            render_isas_key_value("Pohotovosť / Priorita", f"+{pohotovost} %{popis_pohotovost_str}"),
            render_isas_key_value("Účtovací takt", min_jednotka_str)
        ), layout="col", gap=4), is_dashed=False)

        r2 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("Náhrada za 1 km:", f"{km_nahrada} {mena}"),
            render_isas_flex_between("Stravné (5-12h | 12-18h | 18h+):", f"{stravne} | {stravne_12} | {stravne_nad} {mena}")
        ), layout="col", gap=4), is_dashed=False)

        r3 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("Pracovná doba:", f"{cas_od} - {cas_do}"),
            render_isas_key_value("Online rezervácie", f"Víkendy: {vikendy} | Sviatky: {sviatky}")
        ), layout="col", gap=4), is_dashed=False)

        r4 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("🟡 Zvýšená záťaž:", f"{limit_z} úloh"),
            render_isas_flex_between("🔴 Kritický stav:", f"{limit_k} úloh")
        ), layout="col", gap=4))

        return render_isas_singleton_card("Účtovanie a Kapacity", "🔧", "#db2777", format_html('{}{}{}{}', r1, r2, r3, r4))

class KonfiguraciaFilter(admin.SimpleListFilter):
    title = '⚙️ Konfigurácia serverov'
    parameter_name = 'konfiguracia'

    def lookups(self, request, model_admin):
        return (
            ('ok', '🟢 Kompletná (IMAP aj SMTP)'),
            ('chyba', '🔴 Chýbajúce nastavenia'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'ok':
            return queryset.exclude(imap_server__isnull=True).exclude(imap_server__exact='').exclude(smtp_server__isnull=True).exclude(smtp_server__exact='')
        if self.value() == 'chyba':
            return queryset.filter(Q(imap_server__isnull=True) | Q(imap_server__exact='') | Q(smtp_server__isnull=True) | Q(smtp_server__exact=''))
        return queryset

@admin.register(EmailovyUcet)
class EmailovyUcetAdmin(ISASPremiumAdmin):
    form = EmailovyUcetForm
    list_display = (
        "get_rich_identity", 
        "get_credentials_card", 
        "get_servers_card",
        "get_status_badge"
    )
    list_display_links = ("get_rich_identity",)    
    search_fields = ("nazov", "email_adresa", "prihlasovacie_meno")
    list_filter = ('je_aktivny', KonfiguraciaFilter) 
    # primárny kľúč (pk) ako fallback. Garantuje 100% konzistentné stránkovanie.
    ordering = ("nazov", "pk")

    fieldsets = (
        ('🏢 1. ZÁKLADNÁ IDENTIFIKÁCIA', {
            'fields': (
                'je_aktivny',
                ('nazov', 'email_adresa'),
            ),
            'description': '🪪 Základné identifikačné údaje e-mailovej schránky pre systém a zobrazenie v aplikácii.'
        }),
        ('🔐 2. PRÍSTUPOVÉ ÚDAJE (APP PASSWORD)', {
            'fields': (('prihlasovacie_meno', 'heslo'),),
            'description': '⚠️ Použite vygenerované Heslo aplikácie (App Password). Nikdy nepoužívajte svoje hlavné heslo!'
        }),
        ('📡 3. IMAP & SMTP SERVERY', {
            'fields': (
                ('imap_server', 'imap_port'),
                ('smtp_server', 'smtp_port'),
            ),
            'description': '⚙️ Nastavenia pre príjem (IMAP) a odosielanie (SMTP). Podporujeme štandardné porty (993 pre IMAP SSL, 465 pre SMTP SSL).'
        }),
        ('✍️ 4. FIREMNÝ PODPIS', {
            'fields': ('podpis_html',),
            'classes': ('vivid-box-flash',), 
            'description': '📝 Automatický HTML podpis, ktorý sa pripojí na koniec každej odoslanej správy.'
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "📧 E-mailové účty"
    isas_preloader_text = "⏳ Vstupujete do zoznamu e-mailových účtov..."
    isas_delete_success_message = "✅ Úspešne odstránené: {count} schránok!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🗄️",
    }
    isas_vivid_box_flash = True

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if not request.user.is_staff:
            return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie na túto akciu."}, status=403)
        return None

    # --- 🪝 HOOKS PRE DETECTIVE SEARCH ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(nazov__icontains=term) | Q(email_adresa__icontains=term)
        ).values('id', 'nazov', 'email_adresa')[:30]

    def format_detective_result(self, obj):
        safe_name = escape(obj['nazov'] or "Neznámy účet")
        safe_email = escape(obj['email_adresa'] or "Bez e-mailu")
        return {
            'id': obj['id'], 
            'name': safe_name, 
            'search_value': safe_name,
            'rola': safe_email,
            'cat': '📧 E-MAILOVÝ ÚČET'
        }

    # BEZPEČNOSŤ: Ochrana pred únikom hesla v Read-Only režime.
    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.has_perm(f"{self.opts.app_label}.change_{self.opts.model_name}"):
            if 'heslo' not in readonly:
                readonly.append('heslo_maskovane')
            if 'heslo' in readonly:
                readonly.remove('heslo')
        return readonly

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if not request.user.has_perm(f"{self.opts.app_label}.change_{self.opts.model_name}"):
            fieldsets = copy.deepcopy(fieldsets)
            for name, opts in fieldsets:
                if 'fields' in opts:
                    new_fields = []
                    for f in opts['fields']:
                        if isinstance(f, tuple):
                            new_fields.append(tuple('heslo_maskovane' if x == 'heslo' else x for x in f))
                        else:
                            new_fields.append('heslo_maskovane' if f == 'heslo' else f)
                    opts['fields'] = tuple(new_fields)
        return fieldsets

    @admin.display(description='🔑 Heslo')
    def heslo_maskovane(self, obj):
        return "******** (Skryté z bezpečnostných dôvodov)"

    @admin.display(description='🗄️ E-mailová schránka', ordering='nazov')
    def get_rich_identity(self, obj):
        nazov = obj.nazov if obj.nazov else "❓ Neznámy účet"
        podpis = render_isas_badge("MÁ PODPIS", "✍️", "success", wrapper=False) if obj.podpis_html else render_isas_badge("BEZ PODPISU", "🚫", "neutral", wrapper=False)
        extra = render_isas_layout(podpis, layout="col", center=True)
        return render_isas_avatar("📧", nazov, "🗄️ E-MAILOVÝ ÚČET", "#38bdf8, #0284c7", "rgba(2, 132, 199, 0.3)", extra)

    @admin.display(description='🔑 Prihlasovacie údaje')
    def get_credentials_card(self, obj):
        html_parts = []
        if obj.email_adresa:
            html_parts.append(render_isas_badge(obj.email_adresa, icon="✉️", copy_value=obj.email_adresa))
        if obj.prihlasovacie_meno:
            html_parts.append(render_isas_badge(obj.prihlasovacie_meno, icon="👤", copy_value=obj.prihlasovacie_meno))
            
        if obj.heslo:
            html_parts.append(render_isas_badge("Heslo zadané", "🔐", "danger", wrapper=False))
        else:
            html_parts.append(render_isas_badge("Chýba heslo", "⚠️", "neutral", wrapper=False))
            
        return render_isas_layout(mark_safe("".join(html_parts)), layout="col", center=True, gap=4)

    @admin.display(description='📡 Servery (IMAP/SMTP)')
    def get_servers_card(self, obj):
        imap_text = f"{obj.imap_server} : {obj.imap_port}" if obj.imap_server else "Nenastavené"
        smtp_text = f"{obj.smtp_server} : {obj.smtp_port}" if obj.smtp_server else "Nenastavené"
        
        return render_isas_mini_stat_card("📥 IMAP SERVER", imap_text, "📤 SMTP SERVER", smtp_text)

    @admin.display(description='🚦 Stav', ordering='je_aktivny')
    def get_status_badge(self, obj):
        if obj.je_aktivny:
            return render_isas_badge("Aktívny zber", "✅", "success")
        return render_isas_badge("Pozastavené", "⏸️", "danger")

@admin.register(EmailovaSprava)
class EmailovaSpravaAdmin(ISASReadOnlyAdminMixin, ISASPremiumAdmin):
    change_list_template = "admin/sablonapremaily.html"

    # VÝKON: Prevencia N+1 dopytov.
    list_select_related = ('ucet', 'zakaznik', 'dodavatel', 'zamestnanec')
    
    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "📬 E-maily"
    isas_preloader_text = "⏳ Vstupujete do e-mailového rozhrania..."
    isas_enable_fast_action = False
    isas_enable_detective = False

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        # Bezpečné odovzdanie iba aktívnych schránok pre prepínač a odosielanie
        extra_context['aktivne_ucty'] = EmailovyUcet.objects.filter(je_aktivny=True).values('id', 'nazov', 'email_adresa')
        return super().changelist_view(request, extra_context=extra_context)

@admin.register(ISASAuditLog)
class ISASAuditLogAdmin(ISASReadOnlyAdminMixin, ISASPremiumAdmin):    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('vykonal', 'content_type')

    list_display = (
        'get_akcia_card', 
        'get_detail_card', 
        'get_zmeny_card'
    )    
    list_display_links = ('get_akcia_card',)
    list_filter = ('cas_vykonania', 'akcia', ISASRelatedUserFilter)
    isas_user_filter_field = 'vykonal'
    readonly_fields = ('cas_vykonania', 'vykonal', 'content_type', 'object_id', 'akcia', 'pocet_zmien', 'isas_metadata_json_zmeny', 'zaznam')

    # Znak '=' pred object_id vynúti presnú zhodu (využije index). 
    search_fields = ('=object_id', 'vykonal__username', 'content_type__model')
    
    def has_view_permission(self, request, obj=None):
        super().has_view_permission(request, obj)
        return True # Povolí nádherný Read-Only detail! 👀

    fieldsets = (
        ('🛡️ Základné informácie o akcii', {
            'fields': (
                ('akcia', 'cas_vykonania'),
                ('vykonal', 'content_type'),
                'object_id'
            ),
            'description': 'Presná identifikácia vykonanej akcie, času a používateľa v systéme. 🕵️'
        }),
        ('📊 Detailné zmeny (JSON)', {
            'fields': (
                'pocet_zmien',
                'isas_metadata_json_zmeny',
            ),
            'description': 'Technický výpis presných zmien v databáze (Stará hodnota ➡️ Nová hodnota). 💾'
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "🛡️ Audit Logy"
    isas_preloader_text = "⏳ Vstupujete do zoznamu audit logov..."
    isas_delete_success_message = "✨ Bezpečnostné logy boli úspešne odstránené!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🛡️",
    }

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            if not request.user.is_superuser:
                return JsonResponse({"status": "error", "message": "🚫 Len SuperAdmin môže mazať bezpečnostné logy!"}, status=403)
            if len(valid_ids) > 1000:
                return JsonResponse({"status": "error", "message": "🚫 Z bezpečnostných dôvodov je možné zmazať max 1000 logov naraz."}, status=400)
        return None

    # --- 🪝 HOOKS PRE DETECTIVE SEARCH ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('vykonal', 'content_type').filter(
            Q(object_id__startswith=term) |
            Q(vykonal__username__istartswith=term) |
            Q(content_type__model__startswith=term.lower())
        ).order_by('-cas_vykonania')[:30]

    def format_detective_result(self, obj):
        user_name = obj.vykonal.username if obj.vykonal else "🤖 Systém"
        akcia = obj.get_akcia_display() if hasattr(obj, 'get_akcia_display') else str(obj.akcia)
        model_name = obj.content_type.model.upper() if obj.content_type else "OBJEKT"
        titulok = f"{akcia} ➔ {model_name} (ID: {obj.object_id})"
        datum = obj.cas_vykonania.strftime('%d.%m.%Y %H:%M') if obj.cas_vykonania else ""
        
        return {
            'id': obj.id, 
            'name': escape(titulok),
            'search_value': escape(str(obj.object_id)),
            'rola': f"👤 {escape(user_name)}",
            'date': escape(datum),
            'cat': '🛡️ AUDIT LOG'
        }

    @admin.display(description='⚡ Akcia & Čas', ordering='cas_vykonania')
    def get_akcia_card(self, obj):
        datum = obj.cas_vykonania.strftime('%d.%m.%Y %H:%M') if obj.cas_vykonania else "Neznámy ❓"
        farby = {
            'CREATE': ('#10b981, #059669', 'rgba(16, 185, 129, 0.3)', '➕', 'Vytvorenie'),
            'UPDATE': ('#3b82f6, #2563eb', 'rgba(59, 130, 246, 0.3)', '📝', 'Úprava'),
            'DELETE': ('#ef4444, #dc2626', 'rgba(239, 68, 68, 0.3)', '🗑️', 'Vymazanie'),
            'ANONYMIZE': ('#8b5cf6, #7c3aed', 'rgba(139, 92, 246, 0.3)', '🕵️', 'Anonymizácia'),
        }
        bg, shadow, icon, text = farby.get(obj.akcia, ('#64748b, #475569', 'rgba(100, 116, 139, 0.3)', '❓', 'Neznáma'))
        return render_isas_avatar(icon, text.upper(), f"🕒 {datum}", bg, shadow)

    @admin.display(description='📂 Cieľ & Používateľ', ordering='content_type__model')
    def get_detail_card(self, obj):
        user_name = obj.vykonal.username if obj.vykonal else "🤖 Systém / AI"
        model_name = obj.content_type.model.upper()
        
        hidden_id = render_isas_hidden_data("audit-id-hidden", obj.id)
        obsah = render_isas_layout(format_html('{}{}{}',
            render_isas_key_value("Cieľ", model_name, icon="📂"),
            render_isas_key_value("Používateľ", user_name, icon="👤"),
            render_isas_badge(f"ID: {obj.object_id}", "🔑", "neutral", wrapper=False)
        ), layout="col", center=True, gap=4)
        
        return format_html('{}{}', hidden_id, obsah)

    @admin.display(description='📊 Detail Zmien')
    def get_zmeny_card(self, obj):
        if obj.pocet_zmien > 0:
            return render_isas_badge(f"{obj.pocet_zmien} Zmien", "💾", "success")
        elif obj.isas_metadata_json_zmeny:
            # Fallback pre historické logy vytvorené pred touto optimalizáciou
            return render_isas_badge("Záznam", "💾", "success")
        return render_isas_badge("Bez zmien", "📭", "neutral")

class SpotifyStavFilter(admin.SimpleListFilter):
    title = '🟢 Stav prepojenia'
    parameter_name = 'stav'

    def lookups(self, request, model_admin):
        return (
            ('aktivny', '✅ Aktívny (Hrá)'),
            ('expirovany', '⚠️ Expirovaný (Čaká na obnovu)'),
        )

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == 'aktivny':
            return queryset.filter(expires_at__gt=now)
        if self.value() == 'expirovany':
            return queryset.filter(expires_at__lte=now)
        return queryset

@admin.register(SpotifyToken)
class SpotifyTokenAdmin(ISASReadOnlyAdminMixin, ISASPremiumAdmin):     
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('user')

    list_display = (
        'get_user_card', 
        'get_status_card', 
        'get_time_card'
    )
    
    list_display_links = ('get_user_card',)
    list_filter = (SpotifyStavFilter, ISASRelatedUserFilter)
    isas_user_filter_field = 'user'
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')
    
    def has_delete_permission(self, request, obj=None):
        super().has_delete_permission(request, obj)
        return True

    readonly_fields = ('user', 'access_token_masked', 'refresh_token_masked', 'expires_at', 'vytvorene', 'upravene')
    fieldsets = (
        ('👤 Identifikácia', {
            'fields': ('user', 'spotify_account_name'),
            'description': 'Používateľ, ktorému patrí toto prepojenie so Spotify.'
        }),
        ('🔐 Bezpečnostné kľúče (Šifrované)', {
            'fields': ('access_token_masked', 'refresh_token_masked'),
            'description': 'Kľúče sú v databáze šifrované. Z bezpečnostných dôvodov sa zobrazujú len prvé 4 znaky.'
        }),
        ('⏱️ Časová os', {
            'fields': ('expires_at', 'vytvorene', 'upravene'),
        }),
    )

    # --- 🛡️ BEZPEČNÉ MASKOVANIE TOKENOV V DETAILE ---
    @admin.display(description="🔑 Access Token")
    def access_token_masked(self, obj):
        if not obj.access_token: return "Nezadaný"
        return f"{obj.access_token[:4]}********************************"

    @admin.display(description="🔄 Refresh Token")
    def refresh_token_masked(self, obj):
        if not obj.refresh_token: return "Nezadaný"
        return f"{obj.refresh_token[:4]}********************************"

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "🎵 Spotify"
    isas_preloader_text = "⏳ Vstupujete do sekcie nastavenia prepojenia Spotify s ISAS CORE..."
    isas_delete_success_message = "🎵 Spotify účty boli úspešne odpojené!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🎧",
    }

    # --- 🪝 HOOKS PRE FAST ACTION ---
    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            if not self.has_delete_permission(request):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na odpojenie Spotify účtov!"}, status=403)
            if len(valid_ids) > 500:
                return JsonResponse({"status": "error", "message": "🚫 Z bezpečnostných dôvodov je možné odpojiť max 500 účtov naraz."}, status=400)
        return None

    # --- 🪝 HOOKS PRE DETECTIVE SEARCH ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('user').filter(
            Q(user__username__icontains=term) |
            Q(user__first_name__icontains=term) |
            Q(user__last_name__icontains=term) |
            Q(spotify_account_name__icontains=term)
        ).order_by('user__username')[:30]

    def format_detective_result(self, obj):
        name_str = f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username
        spotify_acc = obj.spotify_account_name or "Neznámy účet"
        return {
            'id': obj.id, 
            'name': escape(name_str), 
            'search_value': escape(obj.user.username),
            'rola': f"Účet: {escape(spotify_acc)}",
            'cat': '🎵 SPOTIFY'
        }

    @admin.display(description='👤 Používateľ', ordering='user__username')
    def get_user_card(self, obj):
        full_name = f"{obj.user.first_name} {obj.user.last_name}".strip() or "Meno nezadané"
        spotify_name = obj.spotify_account_name if obj.spotify_account_name else "Neznámy Spotify účet"
        
        extra = render_isas_badge(f"Účet: {spotify_name}", "🎵", "success", wrapper=False)
        extra_html = render_isas_layout(extra, layout="col", center=True)
        
        return render_isas_avatar("🎧", obj.user.username, full_name, "#10b981, #059669", "rgba(16, 185, 129, 0.3)", extra_html)

    @admin.display(description='🟢 Stav Prepojenia')
    def get_status_card(self, obj):
        if obj.is_expired():
            badge = render_isas_badge("EXPIROVANÝ", "⚠️", "danger", wrapper=False)
            subtext = "Čaká na automatickú obnovu"
        else:
            badge = render_isas_badge("AKTÍVNY", "✅", "success", wrapper=False)
            subtext = "Pripravený na prehrávanie"

        return render_isas_layout(format_html('{}{}', badge, render_isas_text(subtext, muted=True)), layout="col", center=True, gap=6)

    @admin.display(description='⏱️ Časová os', ordering='expires_at')
    def get_time_card(self, obj):
        def safe_format(dt):
            if not dt:
                return "Neznáme"
            c_cas = timezone.localtime(dt) if (dt and timezone.is_aware(dt)) else dt
            return c_cas.strftime('%d.%m.%Y %H:%M') if c_cas else ""

        vytvorene = safe_format(obj.vytvorene)
        upravene = safe_format(obj.upravene)
        expires = safe_format(obj.expires_at)
        
        return render_isas_time_card("Vytvorené", vytvorene, "Upravené", upravene, "Vyprší", expires)

class RegionTrhuFilter(admin.SimpleListFilter):
    """🌍 Filter pre okamžité triedenie štátov podľa ekonomických zón a ERP."""
    title = '🌐 Ekonomická zóna / ERP'
    parameter_name = 'zona'

    def _get_aktivne_iso(self, request):
        aktivne_iso = getattr(request, '_isas_aktivne_iso_cache', None)
        if aktivne_iso is None:
            try:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso'):
                    aktivne_iso = (FiremnaIdentitaModel.get_aktivna_krajina_iso() or '').strip().upper()
            except Exception:
                aktivne_iso = ''
            setattr(request, '_isas_aktivne_iso_cache', aktivne_iso or '')
        return aktivne_iso

    def lookups(self, request, model_admin):
        polozky = []
        domace_iso = self._get_aktivne_iso(request)
        if domace_iso and domace_iso not in ('NEZADANE', 'INYKRAJINA'):
            polozky.append(('domaci', f'🏢 Domáci trh (Sídlo: {domace_iso})'))
        
        polozky.extend([
            ('crm', '💼 Aktívne trhy v CRM (Partneri)'),
            ('eu', '🇪🇺 Členské štáty EÚ'),
            ('noneu', '🌐 Tretie krajiny (Mimo EÚ)'),
        ])
        return tuple(polozky)

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'domaci':
            domace_iso = self._get_aktivne_iso(request)
            return queryset.filter(iso_kod=domace_iso) if domace_iso else queryset.none()
        if val == 'crm':
            crm_iso_kody = set()
            try:
                Zakaznik = apps.get_model('Evidencia', 'Zakaznik')
                Dodavatel = apps.get_model('Evidencia', 'Dodavatel')
                staty = set(Zakaznik.objects.exclude(stat__isnull=True).exclude(stat__exact='').values_list('stat', flat=True).distinct())
                staty.update(Dodavatel.objects.exclude(stat__isnull=True).exclude(stat__exact='').values_list('stat', flat=True).distinct())
                
                StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
                for s in staty:
                    iso = StatLokalizaciaModel.rozpoznaj_iso(s) if hasattr(StatLokalizaciaModel, 'rozpoznaj_iso') else ''
                    if iso and iso not in ('NEZADANE', 'INYKRAJINA'):
                        crm_iso_kody.add(iso)
            except Exception:
                pass
            return queryset.filter(iso_kod__in=crm_iso_kody)
        if val == 'eu':
            return queryset.filter(je_clen_eu=True)
        if val == 'noneu':
            return queryset.filter(je_clen_eu=False)
        return queryset

@admin.register(StatLokalizacia)
class StatLokalizaciaAdmin(ISASPremiumAdmin):
    list_display = (
        'get_stat_card',
        'get_mena_dph_card',
        'get_peppol_eu_card',
        'get_subdiv_card',
        'get_lehota_card',
        'get_reklamacia_card'
    )
    list_display_links = ('get_stat_card',)
    list_filter = (RegionTrhuFilter, 'je_clen_eu', 'efaktura_rezim', 'lehota_faktury_typ')
    search_fields = ('iso_kod', 'nazov', 'mena', 'aliasy')
    ordering = ('iso_kod',)

    fieldsets = (
        ('🗺️ 1. IDENTIFIKÁCIA ŠTÁTU', {
            'description': '<b>💡 ISAS Nápoveda:</b> Základný kód štátu a jeho názov. Kód ISO určuje daňovú príslušnosť partnerov a pobočiek.',
            'fields': (('iso_kod', 'nazov'), 'mena', 'aliasy')
        }),
        ('📊 2. DAŇOVÝ REŽIM A SADZBY DPH', {
            'description': '<b>📈 Zákonné dane:</b> Nastavte sadzby DPH platné v danej krajine. Tieto sadzby sa automaticky prenášajú do fakturácie a cenníkov.',
            'fields': (('dph_zakladna', 'dph_znizena'),)
        }),
        ('🇪🇺 3. MEDZINÁRODNÝ REŽIM A eFAKTÚRA', {
            'description': '<b>🌐 Európska sieť a eFaktúra:</b> Určuje príslušnosť k EÚ, kód EAS schémy pre Peppol (0245 SK, 0144 CZ, 9915 AT, 9930 DE) a legislatívny režim odovzdávania faktúr (Peppol vs. Štátny CTC portál).',
            'fields': (('je_clen_eu', 'peppol_eas_kod'), 'efaktura_rezim')
        }),
        ('⏳ 4. ZÁKONNÉ LEHOTY FAKTURÁCIE A REKLAMÁCIÍ', {
            'description': '<b>🏛️ Daňový a reklamačný deadline:</b> Pravidlá a lehoty na vystavenie faktúry a vybavenie reklamácie podľa legislatívy danej krajiny.',
            'fields': (('lehota_faktury_dni', 'lehota_faktury_typ'), 'lehota_reklamacie_dni')
        }),
        ('🌍 5. REGIONÁLNE SUBDIVÍZIE A SVIATKY', {
            'description': '<b>📅 Kalendár a sviatky:</b> JSON konfigurácia kódov regiónov (subdiv), aliasov miest a regulárnych výrazov PSČ pre výpočet servisných kapacít.',
            'fields': ('subdiv_konfiguracia',),
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "🗺️ Lokalizácie štátov"
    isas_preloader_text = "⏳ Vstupujete do sekcie daňových a menových lokalizácií..."
    isas_delete_success_message = "✨ Úspešne odstránené: {count} lokalizácií štátov!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🗺️",
    }

    def _ziskaj_pouzite_staty_v_crm(self, request):
        """
        🚀 O(1) PERFORMANCE: Načíta všetky použité štáty z CRM presne 1-krát za HTTP request.
        Eliminuje stovky zbytočných SQL dotazov pri renderovaní tabuľky v admine.
        """
        cache_key = '_isas_crm_pouzite_staty_cache'
        pouzite = getattr(request, cache_key, None)
        if pouzite is not None:
            return pouzite

        pouzite_staty = set()
        try:
            Zakaznik = apps.get_model('Evidencia', 'Zakaznik')
            Dodavatel = apps.get_model('Evidencia', 'Dodavatel')

            # 2 rýchle agregačné dotazy namiesto stoviek jednotlivých exist() dotazov
            zak_staty = Zakaznik.objects.exclude(stat__isnull=True).exclude(stat__exact='').values_list('stat', flat=True).distinct()
            dod_staty = Dodavatel.objects.exclude(stat__isnull=True).exclude(stat__exact='').values_list('stat', flat=True).distinct()

            def _clean(val):
                if not val: return ''
                return unicodedata.normalize('NFKD', str(val)).encode('ascii', 'ignore').decode('utf-8').strip().lower()

            for s in zak_staty:
                c = _clean(s)
                if c:
                    pouzite_staty.add(c)
                    iso = StatLokalizacia.rozpoznaj_iso(s) if hasattr(StatLokalizacia, 'rozpoznaj_iso') else ''
                    if iso and iso not in ('NEZADANE', 'INYKRAJINA'):
                        pouzite_staty.add(_clean(iso))
            for s in dod_staty:
                c = _clean(s)
                if c:
                    pouzite_staty.add(c)
                    iso = StatLokalizacia.rozpoznaj_iso(s) if hasattr(StatLokalizacia, 'rozpoznaj_iso') else ''
                    if iso and iso not in ('NEZADANE', 'INYKRAJINA'):
                        pouzite_staty.add(_clean(iso))

        except Exception as e:
            logger.warning(f"⚠️ Nepodarilo sa načítať použité štáty z CRM: {e}")

        setattr(request, cache_key, pouzite_staty)
        return pouzite_staty

    def _over_moznost_vymazania_objektu(self, request, stat_obj):
        """
        🛡️ ISAS ENTERPRISE GUARDIAN:
        Kompletné overenie jedného objektu StatLokalizacia.
        Chráni sídlo firmy AJ štáty, ktoré majú partnerov v CRM (podľa ISO aj Názvu).
        """
        if not stat_obj:
            return None

        # 1. KONTROLA SÍDLA MATERSKEJ FIRMY (s Request-Cache)
        aktivne_iso = getattr(request, '_isas_aktivne_iso_cache', None)
        if aktivne_iso is None:
            try:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso'):
                    aktivne_iso = (FiremnaIdentitaModel.get_aktivna_krajina_iso() or '').strip().upper()
                else:
                    aktivne_iso = ''
            except Exception as e:
                logger.critical(f"Kritická chyba pri overovaní sídla firmy: {e}")
                aktivne_iso = ''
            setattr(request, '_isas_aktivne_iso_cache', aktivne_iso)

        obj_iso = str(getattr(stat_obj, 'iso_kod', '') or '').strip().upper()

        if aktivne_iso and aktivne_iso not in ('NEZADANE', 'INYKRAJINA'):
            if obj_iso == aktivne_iso:
                return f"🛑 Štát '{obj_iso}' je nastavený ako oficiálne sídlo vašej firmy a nie je možné ho vymazať!"

        # 2. KONTROLA REFERENČNEJ INTEGRITY V CRM (ISO kód, Názov aj Aliasy)
        pouzite_v_crm = self._ziskaj_pouzite_staty_v_crm(request)
        if pouzite_v_crm:
            import unicodedata
            def _clean(val):
                if not val: return ''
                return unicodedata.normalize('NFKD', str(val)).encode('ascii', 'ignore').decode('utf-8').strip().lower()

            hladane_tvary = {_clean(obj_iso), _clean(getattr(stat_obj, 'nazov', ''))}
            aliasy = getattr(stat_obj, 'aliasy', '')
            if aliasy:
                for a in str(aliasy).replace(';', ',').split(','):
                    ca = _clean(a)
                    if ca: hladane_tvary.add(ca)

            # Odstránime prázdne stringy
            hladane_tvary.discard('')

            # Ak sa akýkoľvek tvar štátu nachádza medzi partnermi v CRM, zablokujeme mazanie
            prienik = hladane_tvary & pouzite_v_crm
            if prienik:
                nazov_zobrazenie = getattr(stat_obj, 'nazov', None) or obj_iso
                return f"⚠️ Štát '{nazov_zobrazenie}' ({obj_iso}) nie je možné vymazať, pretože je priradený k existujúcim partnerom v CRM!"

        return None

    def has_delete_permission(self, request, obj=None):
        if obj:
            dovod = self._over_moznost_vymazania_objektu(request, obj)
            if dovod:
                return False
        return super().has_delete_permission(request, obj)

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        res = super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)
        if res:
            return res

        if action_type == 'delete' and valid_ids:
            # Natiahneme objekty (vrátane polí nazov a aliasy) pre plnú kontrolu integrity
            objekty_na_zmazanie = list(self.get_queryset(request).filter(pk__in=valid_ids).only('pk', 'iso_kod', 'nazov', 'aliasy'))
            
            for obj in objekty_na_zmazanie:
                dovod = self._over_moznost_vymazania_objektu(request, obj)
                if dovod:
                    return JsonResponse({"status": "error", "message": dovod}, status=403)

        return None

    # --- 🪝 HOOKS PRE DETECTIVE SEARCH (100% DIAKRITICKY A TYPOVO ODOLNÉ) ---
    def get_detective_queryset(self, request, term):
        if not term or not str(term).strip():
            return self.get_queryset(request).none()

        term_str = str(term).strip()
        term_clean = unicodedata.normalize('NFKD', term_str).encode('ascii', 'ignore').decode('utf-8').strip().lower()

        base_qs = self.get_queryset(request)

        # 1. Štandardný DB filter (priama zhoda na ISO, názov, menu, aliasy a Peppol EAS kód)
        filtre = (
            Q(iso_kod__icontains=term_str) |
            Q(nazov__icontains=term_str) |
            Q(mena__icontains=term_str) |
            Q(aliasy__icontains=term_str) |
            Q(peppol_eas_kod__icontains=term_str)
        )
        if term_clean:
            filtre |= Q(aliasy__icontains=term_clean)

        zhody_pks = set(base_qs.filter(filtre).values_list('pk', flat=True)[:30])

        # 2. 100% DIAKRITICKÝ MATCH (O(1) in-memory fallback pre databázy bez unaccentu):
        # Ak hľadaný výraz bez diakritiky nenašiel v DB plný limit, preveríme normalizované názvy
        if len(zhody_pks) < 30 and term_clean:
            kandidati = base_qs.exclude(pk__in=zhody_pks).values_list('pk', 'nazov', 'aliasy')
            for pk, nazov, aliasy in kandidati:
                nazov_ascii = unicodedata.normalize('NFKD', str(nazov or '')).encode('ascii', 'ignore').decode('utf-8').strip().lower()
                aliasy_ascii = unicodedata.normalize('NFKD', str(aliasy or '')).encode('ascii', 'ignore').decode('utf-8').strip().lower()
                if term_clean in nazov_ascii or term_clean in aliasy_ascii:
                    zhody_pks.add(pk)
                    if len(zhody_pks) >= 30:
                        break

        return base_qs.filter(pk__in=zhody_pks).order_by('iso_kod')[:30]

    def format_detective_result(self, obj):
        safe_nazov = escape(obj.nazov or "Neznámy štát")
        safe_iso = escape(obj.iso_kod or "")
        safe_mena = escape(obj.mena or "-")

        # 🛡️ 100% defenzívna konverzia sadzieb DPH voči None, float, int a neplatným typom
        def _bezpecny_decimal(val):
            if val is None:
                return Decimal('0.00')
            try:
                return Decimal(str(val)).quantize(Decimal('0.01'))
            except Exception:
                return Decimal('0.00')

        dph_z = _bezpecny_decimal(getattr(obj, 'dph_zakladna', None))
        dph_n = _bezpecny_decimal(getattr(obj, 'dph_znizena', None))

        eu_tag = "🇪🇺 EÚ" if getattr(obj, 'je_clen_eu', False) else "🌐 Non-EU"
        eas_val = str(getattr(obj, 'peppol_eas_kod', '') or '').strip()
        eas_str = f" | EAS: {escape(eas_val)}" if eas_val else ""

        return {
            'id': obj.id,
            'name': f"{safe_nazov} ({safe_iso})",
            'search_value': safe_iso,
            'rola': f"{eu_tag} | Mena: {safe_mena} | DPH: {dph_z} % (zníž. {dph_n} %){eas_str}",
            'cat': '🗺️ ŠTÁT'
        }

    # --- 🎨 VIZUÁLNE METÓDY (100% DEFENSIVE VOČI NoneType) ---
    @admin.display(description='🗺️ Štát', ordering='iso_kod')
    def get_stat_card(self, obj):
        iso = escape(obj.iso_kod or "??")
        nazov = escape((obj.nazov or "Nenastavený štát")).upper()
        extra = render_isas_badge(f"ISO: {iso}", "🌍", "info", wrapper=False)
        return render_isas_avatar(
            "🗺️", 
            nazov, 
            f"Kód: {iso}", 
            "#3b82f6, #1d4ed8", 
            "rgba(59, 130, 246, 0.3)", 
            extra
        )

    @admin.display(description='💰 Mena & DPH')
    def get_mena_dph_card(self, obj):
        mena = escape(obj.mena or "-")
        dph_z = obj.dph_zakladna if obj.dph_zakladna is not None else Decimal('0.00')
        dph_n = obj.dph_znizena if obj.dph_znizena is not None else Decimal('0.00')
        top_val = f"{mena} (Základ: {dph_z} %)"
        bot_val = f"Znížená: {dph_n} %"
        return render_isas_mini_stat_card(
            "💱 MENA & ZÁKLADNÁ DPH", top_val,
            "📉 ZNÍŽENÁ DPH", bot_val
        )

    @admin.display(description='🇪🇺 EÚ & eFaktúra')
    def get_peppol_eu_card(self, obj):
        eu_badge = render_isas_badge("ČLEN EÚ", "🇪🇺", "success", wrapper=False) if obj.je_clen_eu else render_isas_badge("NON-EU", "🌐", "neutral", wrapper=False)
        eas = escape(obj.peppol_eas_kod or "-")
        peppol_badge = render_isas_badge(f"EAS: {eas}", "📬", "info", wrapper=False)
        
        rezim = getattr(obj, 'efaktura_rezim', 'PEPPOL_DIRECT') or 'PEPPOL_DIRECT'
        if rezim == 'CTC_CLEARANCE':
            rezim_badge = render_isas_badge("CTC PORTÁL", "🏛️", "warning", wrapper=False)
        elif rezim == 'VOLITELNE':
            rezim_badge = render_isas_badge("VOLITEĽNÉ", "📄", "neutral", wrapper=False)
        elif rezim == 'VYPNUTE':
            rezim_badge = render_isas_badge("BEZ eFAKTÚRY", "🚫", "neutral", wrapper=False)
        else:
            rezim_badge = render_isas_badge("PEPPOL DIRECT", "🌐", "primary", wrapper=False)

        return render_isas_layout(format_html('{}{}{}', eu_badge, peppol_badge, rezim_badge), layout="col", center=True, gap=4)

    @admin.display(description='🏛️ Regióny & Sviatky')
    def get_subdiv_card(self, obj):
        raw_konfig = getattr(obj, 'subdiv_konfiguracia', None) if obj else None
        konfig = raw_konfig if isinstance(raw_konfig, dict) else {}
        pocet = len(konfig)
        
        if pocet > 0:
            # 🛡️ Ochrana pred TypeError: zaručíme, že každý kľúč je string
            kluce = [str(k).strip() for k in list(konfig.keys())[:3] if str(k).strip()]
            ukazka = ", ".join(kluce)
            zvysok = f" (+{pocet - len(kluce)})" if pocet > len(kluce) else ""
            cely_text = f"{ukazka}{zvysok}" if ukazka else f"{pocet} aktívnych"
            
            badge = render_isas_badge(f"{pocet} regiónov", "🏛️", "success", wrapper=False)
            # 🛡️ Ochrana pred double-escaping: render_isas_text sám bezpečne escapuje cely_text
            popis = render_isas_text(cely_text, size=11, color="#475569", muted=True)
            return render_isas_layout(format_html('{}{}', badge, popis), layout="col", center=True, gap=2)
        
        badge = render_isas_badge("Iba celoštátne", "🌐", "neutral", wrapper=False)
        popis = render_isas_text("Bez členenia", size=10, color="#94a3b8", muted=True)
        return render_isas_layout(format_html('{}{}', badge, popis), layout="col", center=True, gap=2)

    @admin.display(description='⏳ Lehota Fakturácie')
    def get_lehota_card(self, obj):
        typ = obj.lehota_faktury_typ or 'DNI'
        if typ == 'BEZ_LEHOTY':
            badge_text = "Bez lehoty"
            badge_type = "neutral"
        elif typ == 'BEZODKLADNE':
            badge_text = "V deň dodania"
            badge_type = "info"
        elif typ == 'MESIAC_15':
            badge_text = "Do 15. nasl. m."
            badge_type = "info"
        elif typ == 'KONIEC_MESIACA':
            badge_text = "Koniec mesiaca"
            badge_type = "info"
        else:
            badge_text = f"{obj.lehota_faktury_dni or 0} dní"
            badge_type = "warning"

        typ_popis = obj.get_lehota_faktury_typ_display() if hasattr(obj, 'get_lehota_faktury_typ_display') else str(obj.lehota_faktury_typ or "")
        kratky_typ = escape(typ_popis.split('(')[0].strip() if '(' in typ_popis else typ_popis)
        
        return render_isas_layout(format_html('{}{}',
            render_isas_badge(badge_text, "⏳", badge_type, wrapper=False),
            render_isas_text(kratky_typ, size=10, color="#64748b", muted=True)
        ), layout="col", center=True, gap=2)

    @admin.display(description='⚖️ Lehota Reklamácie', ordering='lehota_reklamacie_dni')
    def get_reklamacia_card(self, obj):
        # 🛡️ DEFENSIVE: Zákonná lehota reklamácií voči NoneType, bool pasci, extrémnym číslam a neplatným typom
        raw_dni = getattr(obj, 'lehota_reklamacie_dni', None)
        
        # Ochrana pred bool (True/False)
        if raw_dni is not None and not isinstance(raw_dni, bool):
            try:
                pocet = int(raw_dni)
                if pocet > 0:
                    # Správne slovenské skloňovanie bez externých knižníc
                    if pocet == 1:
                        jednotka = "deň"
                    elif 2 <= pocet <= 4:
                        jednotka = "dni"
                    else:
                        jednotka = "dní"

                    badge = render_isas_badge(f"{pocet} {jednotka}", "⚖️", "warning", wrapper=False)
                    popis = render_isas_text("Zákonná lehota", size=10, color="#64748b", muted=True)
                else:
                    badge = render_isas_badge("Bez lehoty", "🌐", "neutral", wrapper=False)
                    popis = render_isas_text("Podľa zmluvy / VOP", size=10, color="#94a3b8", muted=True)
            except (ValueError, TypeError):
                badge = render_isas_badge("Neurčená", "❓", "neutral", wrapper=False)
                popis = render_isas_text("Neplatná hodnota", size=10, color="#ef4444", muted=True)
        else:
            badge = render_isas_badge("Zmluvná", "🛡️", "neutral", wrapper=False)
            popis = render_isas_text("Bez osobitnej lehoty", size=10, color="#94a3b8", muted=True)

        return render_isas_layout(format_html('{}{}', badge, popis), layout="col", center=True, gap=2)