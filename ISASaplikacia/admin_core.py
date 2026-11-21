import base64
import copy
import datetime
import json
import logging
import urllib.parse
from urllib.parse import quote
from decimal import Decimal
from typing import Any
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType

from django.apps import apps
from django.conf import settings
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Exists, OuterRef, ProtectedError, Q
from django.http import JsonResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe

from Evidencia.utils_helper import get_firemna_mena
from ISASaplikacia.utils import (
    get_isas_magia, render_isas_badge, render_isas_layout,
    render_isas_link_btn, render_isas_text
)

logger = logging.getLogger(__name__)

class ISASHiddenAdminMixin:
    """👻 Skryje model z hlavného menu Django Adminu, ale ponechá ho dostupný cez priame URL."""
    def has_module_permission(self, request):
        return False

class ISASSingletonAdminMixin:
    """🛡️ Zabezpečuje, že v DB bude vždy len jeden záznam a zakazuje štandardné mazanie."""
    def has_add_permission(self, request):
        return not self.model.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request, action_location=None):
        actions = super().get_actions(request, action_location=action_location)
        if 'delete_selected' in actions:
            del actions['delete_selected']
        return actions

class ISASReadOnlyAdminMixin:
    """🔒 Úplne uzamkne model proti akýmkoľvek manuálnym úpravám z prostredia adminu."""
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

class ISASSecurityAdminMixin:
    """🕵️ Pridáva plnú GDPR a PII ochranu do akéhokoľvek ModelAdminu."""
    actions: Any = ['gdpr_anonymizovat_vybrane']

    @admin.action(description="🕵️ GDPR: Anonymizovať vybrané")
    def gdpr_anonymizovat_vybrane(self, request, queryset, reason="Neuvedený dôvod"):
        if not request.user.is_superuser and not request.user.has_perm(f'{self.model._meta.app_label}.can_anonymize'):
            self.message_user(request, "🛑 Nemáte oprávnenie na GDPR anonymizáciu!", level='error')
            return
        
        pii_mapa = {
            'Zamestnanec': ['meno', 'priezvisko', 'telefon', 'email', 'iban', 'cislo_op', 'datum_narodenia', 'ulica_a_cislo', 'mesto', 'psc', 'telegram_id', 'discord_id'],
            'SukromnaOsoba': ['meno_a_priezvisko', 'telefon', 'email', 'iban', 'datum_narodenia', 'ulica_a_cislo', 'mesto', 'psc', 'telegram_id', 'discord_id'],
            'Dodavatel': ['kontaktna_osoba', 'telefon', 'email'],
            'Zakaznik': ['kontaktna_osoba', 'telefon', 'email'],
            'Kandidat': ['meno', 'email', 'telefon', 'motivacna_sprava', 'interna_poznamka', 'ip_adresa'],
        }
        
        polia = pii_mapa.get(self.model.__name__, [])
        if not polia:
            self.message_user(request, "Tento model nepodporuje GDPR anonymizáciu.", level='warning')
            return

        logger = logging.getLogger(__name__)

        count = 0
        with transaction.atomic():
            for obj in queryset.select_for_update().iterator():
                if hasattr(obj, 'vykonaj_anonymizaciu'):
                    try:
                        obj.vykonaj_anonymizaciu(polia, dovod=reason)
                        count += 1
                    except ObjectDoesNotExist:
                        logger.warning(f"⚠️ Záznam ID {obj.pk} bol vymazaný iným procesom.")
                        continue
                    except Exception as e:
                        logger.error(f"❌ Chyba pri anonymizácii záznamu ID {obj.pk}: {str(e)}")
                        continue
        
        self.message_user(request, f"✅ Úspešne anonymizovaných {count} záznamov.")

    def get_readonly_fields(self, request, obj=None):
        readonly = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser and not request.user.has_perm('Evidencia.view_pii_data'):
            citlive_polia = ['mzda_brutto_hodina', 'mzda_netto_hodina', 'iban', 'cislo_op', 'zavazky']
            existujuce_citlive = [f for f in citlive_polia if hasattr(self.model, f)]
            return list(set(list(readonly) + existujuce_citlive))
        return readonly

class ERPExportAdminMixin:
    """⚙️ Pridáva vizuálny semafor, zámky a akcie pre ERP export."""
    actions: Any = ['exportuj_do_erp', 'odomkni_pre_opravu']
    
    isas_auto_poll = True

    @admin.display(description="⚙️ ERP STATUS")
    def erp_status_ikona(self, obj):
        if not hasattr(obj, 'erp_export_status'): return ""
        
        status_map = {
            'NEW': ('#fffbeb', '#d97706', '#fde68a', '🟡', 'Čaká na export', ''),
            'PROCESSING': ('#eff6ff', '#1d4ed8', '#bfdbfe', '⏳', 'Spracováva sa', 'animation: pulse 1.5s infinite;'),
            'EXPORTED': ('#f0fdf4', '#166534', '#bbf7d0', '🔒', 'Zamknuté', ''),
            'FAILED': ('#fef2f2', '#dc2626', '#fecaca', '🔴', 'Chyba v ERP', '')
        }
        
        if obj.erp_export_status not in status_map: return ""
        
        bg, color, border, icon, text, anim = status_map[obj.erp_export_status]
        
        badge = render_isas_badge(
            text=text,
            icon=icon,
            custom_bg=bg,
            custom_text=color,
            custom_border=border,
            custom_style=f"{anim} padding: 4px 10px; font-size: 11px;",
            css_class="isas-erp-status-badge",
            wrapper=False
        )
        return render_isas_layout(
            badge,
            layout="row",
            center=True,
            html_id=f"isas-erp-badge-{obj.id}",
            data_attrs={"status": obj.erp_export_status},
            custom_style="width: 100%;"
        )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and getattr(obj, 'erp_export_status', '') == 'EXPORTED':
            vsetky_polia = [f.name for f in self.model._meta.fields] + [f.name for f in self.model._meta.many_to_many]
            vynimky = {'erp_error_log', 'poznamka', 'interne_poznamky'}
            # 🛡️ DEFENSIVE: Striktné zachovanie deterministického poradia polí podľa modelu
            zamknute = set(readonly) | (set(vsetky_polia) - vynimky)
            return [f for f in vsetky_polia if f in zamknute] + [f for f in readonly if f not in vsetky_polia]
        return readonly

    @admin.action(description="📦 Exportovať vybrané do ERP (XML)")
    def exportuj_do_erp(self, request, queryset):
        self.message_user(request, "ISAS CORE: Exportný XML modul sa pripravuje.", level=messages.WARNING)

    @admin.action(description="🔓 Odomknúť pre re-export (Iba pre Adminov)")
    def odomkni_pre_opravu(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, "❌ Nemáte oprávnenie odomykať účtovné doklady.", level=messages.ERROR)
            return
        
        with transaction.atomic():
            zamknute_zaznamy = queryset.select_for_update()
            if hasattr(self.model, 'je_anonymizovany'):
                zamknute_zaznamy = zamknute_zaznamy.filter(je_anonymizovany=False)
            pocet = zamknute_zaznamy.update(erp_export_status='NEW', erp_id=None)
        self.message_user(request, f"✅ Úspešne odomknutých {pocet} záznamov.", level=messages.SUCCESS)

class ISASPremiumInlineMixin:
    """🧩 ISAS CORE: Mixin pre aplikáciu UI/UX Widget Enginu aj do Inlines."""
    isas_clean_fk_fields: dict[str, str] = {}
    isas_autocomplete_fk_fields: dict[str, str] = {}
    isas_clean_choice_fields: dict[str, str] = {}

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'sadzba_dph' and 'initial' not in kwargs:
            try:
                GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                kwargs['initial'] = GlobalneNastaveniaModel.get_zakladna_dph()
            except Exception:
                pass

        # 📖 DYNAMICKÝ ODVETVOVÝ LEXIKÓN PRE INLINES (BEZ SÉMANTICKÝCH KOLÍZIÍ)
        try:
            from Evidencia.industry_loader import get_industry_term
            m_label = get_industry_term('meter_label', 'Počítadlo')
            m_unit = get_industry_term('meter_unit', 'hod.')
            m_load_label = get_industry_term('meter_load_label', 'Počítadlo v záťaži')
            m_load_unit = get_industry_term('meter_load_unit', 'hod.')
            z_gen = get_industry_term('zariadenie_genitiv', 'zariadenia')

            field_lexicon = {
                'aktualne_motohodiny': (f"⏱️ Aktuálny stav ({m_label})", f"Aktuálny celkový stav počítadla {z_gen} ({m_unit})."),
                'stav_motohodin': (f"⏱️ Stav pri servise ({m_label})", f"Zaznamenaný stav počítadla {z_gen} pri výkone servisu ({m_unit})."),
                'planovane_motohodiny': (f"⏳ Plánovaný servis pri ({m_label})", f"Cieľová hodnota počítadla pre vykonanie údržby ({m_unit})."),
                'interval_motohodin': (f"📏 Servisný interval ({m_label})", f"Predpísaný počet jednotiek medzi servismi ({m_unit})."),
                'servisy_predstih_motohodiny': (f"⏱️ Predstih upozornenia ({m_label})", f"Koľko vopred systém upozorní na servis ({m_unit})."),
                'povodna_hodnota': (f"📉 Pôvodný stav ({m_label})", f"Pôvodná hodnota počítadla pred korekciou ({m_unit})."),
                'nova_hodnota': (f"📈 Nový stav ({m_label})", f"Nová nastavená hodnota počítadla po korekcii ({m_unit})."),
                'motohodiny_v_zatazi': (f"🏋️ {m_load_label}", f"Prevádzkový stav počítadla v záťaži ({m_load_unit})."),
                'planovane_motohodiny_zataz': (f"🏋️ Plánovaný stav v záťaži ({m_load_label})", f"Cieľová hodnota počítadla v záťaži pre servis ({m_load_unit})."),
                'interval_motohodin_zataz': (f"🏋️ Interval v záťaži ({m_load_label})", f"Predpísaný interval záťažového počítadla ({m_load_unit})."),
                'povodna_hodnota_zataz': (f"📉 Pôvodný stav v záťaži ({m_load_label})", f"Pôvodná záťažová hodnota pred korekciou ({m_load_unit})."),
                'nova_hodnota_zataz': (f"📈 Nový stav v záťaži ({m_load_label})", f"Nová záťažová hodnota po korekcii ({m_load_unit})."),
            }

            if db_field.name in field_lexicon:
                # 🛡️ DEFENSIVE: Generické polia korekcie aplikujeme výhradne na model auditu počítadla
                korekcia_fields = {'povodna_hodnota', 'nova_hodnota', 'povodna_hodnota_zataz', 'nova_hodnota_zataz'}
                je_korekcia_model = getattr(getattr(db_field, 'model', None), '_meta', None) and db_field.model._meta.model_name == 'korekciamotohodinlog'
                if db_field.name not in korekcia_fields or je_korekcia_model:
                    kwargs['label'], kwargs['help_text'] = field_lexicon[db_field.name]

        except Exception:
            pass

        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name in getattr(self, 'isas_clean_fk_fields', {}):
            kwargs["empty_label"] = self.isas_clean_fk_fields[db_field.name]
            from Evidencia.forms import PlaceholderSelect
            kwargs["widget"] = PlaceholderSelect
            formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
            if formfield and hasattr(formfield.widget, 'can_add_related'):
                formfield.widget.can_add_related = False
                formfield.widget.can_change_related = False
                formfield.widget.can_delete_related = False
                formfield.widget.can_view_related = False
            return formfield

        if db_field.name in getattr(self, 'isas_autocomplete_fk_fields', {}):
            placeholder_text = self.isas_autocomplete_fk_fields[db_field.name]
            kwargs["empty_label"] = placeholder_text
            formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
            if formfield and hasattr(formfield.widget, 'attrs'):
                formfield.widget.attrs['data-placeholder'] = placeholder_text
            return formfield

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name in getattr(self, 'isas_clean_choice_fields', {}):
            placeholder_text = self.isas_clean_choice_fields[db_field.name]
            kwargs['choices'] = [('', placeholder_text)] + [c for c in db_field.choices if c[0]]
            from Evidencia.forms import PlaceholderSelect
            kwargs['widget'] = PlaceholderSelect
            return super().formfield_for_choice_field(db_field, request, **kwargs)
        return super().formfield_for_choice_field(db_field, request, **kwargs)

class ISASPremiumAdmin(admin.ModelAdmin):
    """
    🚀 ISAS CORE ARCHITECTURE: Base Admin Class
    Zlučuje Template Method Pattern (API, Mágia) a Globálne ERP Zámky.
    """
    class Media:
        css = {'all': ('css/isas_admin_master.css',)}

    # --- 🎨 ISAS CORE: UNIFIKOVANÝ UI/UX WIDGET ENGINE ---
    isas_clean_fk_fields: dict[str, str] = {}         # {'pole': '👤 Placeholder'} -> Vypne ikony, nasadí PlaceholderSelect
    isas_autocomplete_fk_fields: dict[str, str] = {}  # {'pole': '🏭 Placeholder'} -> Ponechá Select2, vnúti placeholder
    isas_clean_choice_fields: dict[str, str] = {}     # {'pole': '📂 Placeholder'} -> Nasadí PlaceholderSelect pre ChoiceField

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'sadzba_dph' and 'initial' not in kwargs:
            try:
                GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                kwargs['initial'] = GlobalneNastaveniaModel.get_zakladna_dph()
            except Exception:
                pass

        # 📖 DYNAMICKÝ ODVETVOVÝ LEXIKÓN PRE VŠETKY FORMULÁRE (BEZ SÉMANTICKÝCH KOLÍZIÍ)
        try:
            from Evidencia.industry_loader import get_industry_term
            m_label = get_industry_term('meter_label', 'Počítadlo')
            m_unit = get_industry_term('meter_unit', 'hod.')
            m_load_label = get_industry_term('meter_load_label', 'Počítadlo v záťaži')
            m_load_unit = get_industry_term('meter_load_unit', 'hod.')
            z_gen = get_industry_term('zariadenie_genitiv', 'zariadenia')

            field_lexicon = {
                'aktualne_motohodiny': (f"⏱️ Aktuálny stav ({m_label})", f"Aktuálny celkový stav počítadla {z_gen} ({m_unit})."),
                'stav_motohodin': (f"⏱️ Stav pri servise ({m_label})", f"Zaznamenaný stav počítadla {z_gen} pri výkone servisu ({m_unit})."),
                'planovane_motohodiny': (f"⏳ Plánovaný servis pri ({m_label})", f"Cieľová hodnota počítadla pre vykonanie údržby ({m_unit})."),
                'interval_motohodin': (f"📏 Servisný interval ({m_label})", f"Predpísaný počet jednotiek medzi servismi ({m_unit})."),
                'servisy_predstih_motohodiny': (f"⏱️ Predstih upozornenia ({m_label})", f"Koľko vopred systém upozorní na servis ({m_unit})."),
                'povodna_hodnota': (f"📉 Pôvodný stav ({m_label})", f"Pôvodná hodnota počítadla pred korekciou ({m_unit})."),
                'nova_hodnota': (f"📈 Nový stav ({m_label})", f"Nová nastavená hodnota počítadla po korekcii ({m_unit})."),
                'motohodiny_v_zatazi': (f"🏋️ {m_load_label}", f"Prevádzkový stav počítadla v záťaži ({m_load_unit})."),
                'planovane_motohodiny_zataz': (f"🏋️ Plánovaný stav v záťaži ({m_load_label})", f"Cieľová hodnota počítadla v záťaži pre servis ({m_load_unit})."),
                'interval_motohodin_zataz': (f"🏋️ Interval v záťaži ({m_load_label})", f"Predpísaný interval záťažového počítadla ({m_load_unit})."),
                'povodna_hodnota_zataz': (f"📉 Pôvodný stav v záťaži ({m_load_label})", f"Pôvodná záťažová hodnota pred korekciou ({m_load_unit})."),
                'nova_hodnota_zataz': (f"📈 Nový stav v záťaži ({m_load_label})", f"Nová záťažová hodnota po korekcii ({m_load_unit})."),
            }

            if db_field.name in field_lexicon:
                # 🛡️ DEFENSIVE: Generické polia korekcie aplikujeme výhradne na model auditu počítadla
                korekcia_fields = {'povodna_hodnota', 'nova_hodnota', 'povodna_hodnota_zataz', 'nova_hodnota_zataz'}
                je_korekcia_model = getattr(getattr(db_field, 'model', None), '_meta', None) and db_field.model._meta.model_name == 'korekciamotohodinlog'
                if db_field.name not in korekcia_fields or je_korekcia_model:
                    kwargs['label'], kwargs['help_text'] = field_lexicon[db_field.name]

        except Exception:
            pass

        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # 1. SCENÁR: Čistý Select bez ikoniek (PlaceholderSelect)
        if db_field.name in getattr(self, 'isas_clean_fk_fields', {}):
            kwargs["empty_label"] = self.isas_clean_fk_fields[db_field.name]
            from Evidencia.forms import PlaceholderSelect
            kwargs["widget"] = PlaceholderSelect
            
            formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
            
            if formfield and hasattr(formfield.widget, 'can_add_related'):
                formfield.widget.can_add_related = False
                formfield.widget.can_change_related = False
                formfield.widget.can_delete_related = False
                formfield.widget.can_view_related = False
            return formfield

        # 2. SCENÁR: Autocomplete (Select2) s vynúteným placeholderom
        if db_field.name in getattr(self, 'isas_autocomplete_fk_fields', {}):
            placeholder_text = self.isas_autocomplete_fk_fields[db_field.name]
            kwargs["empty_label"] = placeholder_text
            
            formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
            
            if formfield and hasattr(formfield.widget, 'attrs'):
                formfield.widget.attrs['data-placeholder'] = placeholder_text
            return formfield

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        # 3. SCENÁR: Čistý ChoiceField s PlaceholderSelect
        if db_field.name in getattr(self, 'isas_clean_choice_fields', {}):
            placeholder_text = self.isas_clean_choice_fields[db_field.name]
            kwargs['choices'] = [('', placeholder_text)] + [c for c in db_field.choices if c[0]]
            from Evidencia.forms import PlaceholderSelect
            kwargs['widget'] = PlaceholderSelect
            return super().formfield_for_choice_field(db_field, request, **kwargs)
            
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    # --- ⚙️ GLOBÁLNE ATRIBÚTY PRE VŠETKY MODELY ---
    change_list_template = "admin/zoznam_s_nacitavanim.html"
    list_per_page = 50

    # --- ⚙️ KONFIGURÁCIA PRE CHILD TRIEDY ---
    isas_title = "Zoznam"
    isas_preloader_text = "⏳ Načítavam dáta..."
    isas_enable_fast_action = True
    isas_enable_detective = True
    isas_magia_kwargs: dict[str, Any] = {}
    isas_delete_iterate = False
    isas_delete_success_message = ""

    TRANSAKCNE_MODELY = ['tankovanie', 'evidenciajazd', 'faktura', 'vystavenafaktura', 'prijatafaktura', 'skladovypohyb']

    # --- 🔒 GLOBÁLNE ERP ZÁMKY ---
    def has_change_permission(self, request, obj=None):
        if obj and getattr(obj, 'erp_export_status', '') == 'EXPORTED':
            if obj._meta.model_name in self.TRANSAKCNE_MODELY:
                if not request.user.is_superuser:
                    return False 
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and getattr(obj, 'erp_export_status', '') in ['EXPORTED', 'PROCESSING']:
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Hromadné akcie (ISAS Engine)")
    def _isas_global_placeholder_action(self, request, queryset):
        pass

    def get_actions(self, request, action_location=None):
        actions = super().get_actions(request, action_location=action_location)
        if 'delete_selected' in actions and hasattr(self.model, 'erp_export_status'):
            del actions['delete_selected']
        loc_val = str(getattr(action_location, 'value', action_location)).upper() if action_location is not None else ''
        is_changelist = action_location is None or 'CHANGELIST' in loc_val or 'CHANGE_LIST' in loc_val
        if is_changelist and '_isas_global_placeholder_action' not in actions:
            actions['_isas_global_placeholder_action'] = (self._isas_global_placeholder_action, '_isas_global_placeholder_action', "Hromadné akcie (ISAS Engine)")
        return actions

    def _inject_isas_html(self, request, response, obj=None, head_html="", body_html=""):
        """🛡️ ISAS CORE: Bezpečné a centralizované injektovanie HTML do response."""
        if not hasattr(response, 'render'):
            return response
            
        config = {}
        
        # 🛡️ ISAS CORE: Dynamické mapovanie atribútov (DRY)
        config_keys = {
            'isas_exclusive_field_groups': 'exclusive_fields', 'isas_autofill_rules': 'autofill_rules',
            'isas_auto_poll': 'auto_poll', 'isas_changelist_fast_poll': 'changelist_fast_poll',
            'isas_action_modals': 'action_modals', 'isas_hide_submit_row': 'hide_submit_row',
            'isas_custom_save_action': 'custom_save_action', 'isas_async_html_action': 'async_html_action',
            'isas_ajax_autofill': 'ajax_autofill', 'isas_row_styles': 'row_styles',
            'isas_megazaloha_poll': 'megazaloha_poll', 'isas_peppol_viz_modal': 'peppol_viz_modal',
            'isas_page_reload_poll': 'page_reload_poll', 'isas_complex_form_rules': 'complex_form_rules',
            'isas_export_excel_modal': 'export_excel_modal', 'isas_value_based_locks': 'value_based_locks',
            'isas_modern_toggles': 'modern_toggles', 'isas_vivid_box_flash': 'vivid_box_flash',
            'isas_file_action_modal': 'file_action_modal', 'isas_hide_action_checkboxes': 'hide_action_checkboxes',
            'isas_ai_sepkar_field': 'ai_sepkar_field', 'isas_open_related_on_select': 'open_related_on_select',
            'isas_inject_sklad_dashboard_css': 'inject_sklad_dashboard_css', 'isas_global_export_modal': 'global_export_modal',
            'isas_ajax_prefilter_rules': 'ajax_prefilter_rules', 'isas_dynamic_fields_ui': 'dynamic_fields_ui',
            'isas_copy_paste_assistant': 'copy_paste_assistant', 'isas_two_choice_modal': 'two_choice_modal'
        }
        
        for attr, key in config_keys.items():
            if hasattr(self, attr): config[key] = copy.deepcopy(getattr(self, attr))

        # 🛡️ ISAS CORE: Dynamická registrácia ERP tlačidiel pre Poller (iba ak je krajina podporovaná)
        if isinstance(self, ERPExportAdminMixin):
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            krajina_modal = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else 'NEZADANE'
            
            if krajina_modal in ['SK', 'CZ', 'NEZADANE']:
                if 'action_modals' not in config:
                    config['action_modals'] = {}
                config['action_modals']['btn-export-erp'] = {
                    'type': 'direct_action', 'action': 'export_erp',
                    'confirm': True,
                    'confirm_title': 'Export a uzamknutie pre ERP 🔒',
                    'confirm_text': 'Naozaj chcete vybrané doklady exportovať do účtovníctva a uzamknúť pred ďalšími úpravami?',
                    'loading_text': '⏳ ZAMYKÁM...', 'success_text': '🔒 ZAMKNUTÉ',
                    'require_selection': True, 'min_selection': 1,
                    'error_msg': 'Najskôr zaškrtnite záznamy na export.', 'reload': True
                }
                config['action_modals']['btn-unlock-erp'] = {
                    'type': 'direct_action', 'action': 'unlock_erp',
                    'confirm': True,
                    'confirm_title': 'Odomknutie účtovných dokladov 🔓',
                    'confirm_text': 'Naozaj chcete odomknúť vybrané doklady pre manuálnu opravu a re-export? Táto akcia je vyhradená výhradne pre administrátorov.',
                    'loading_text': '⏳ ODOMYKÁM...', 'success_text': '🔓 ODOMKNUTÉ',
                    'require_selection': True, 'min_selection': 1,
                    'error_msg': 'Najskôr zaškrtnite záznamy na odomknutie.', 'reload': True
                }

        # 🛡️ ISAS CORE: Dynamická registrácia GDPR tlačidla
        if isinstance(self, ISASSecurityAdminMixin):
            if 'action_modals' not in config:
                config['action_modals'] = {}
            config['action_modals']['btn-gdpr-anonymize'] = {
                'type': 'direct_action', 'action': 'gdpr_anonymize',
                'confirm': True,
                'confirm_title': 'GDPR: Trvalá anonymizácia údajov 🛡️',
                'confirm_text': 'Naozaj chcete trvalo a nezvratne anonymizovať osobné údaje vo vybraných záznamoch? Tento krok nie je možné vrátiť späť.',
                'loading_text': '⏳ ANONYMIZUJEM...', 'success_text': '🕵️ ANONYMIZOVANÉ',
                'require_selection': True, 'min_selection': 1,
                'error_msg': 'Najskôr zaškrtnite záznamy na anonymizáciu.', 'reload': True
            }

        # Boolean flagy
        for attr, key in [('isas_margin_manager', 'margin_manager'), ('isas_korekcia_mh_modal', 'korekcia_mh_modal'), ('isas_protokol_poller', 'protokol_poller')]:
            if getattr(self, attr, False): config[key] = True

        if getattr(self, 'isas_kosik_manager', False) and hasattr(self, 'get_kosik_config'): 
            config['kosik_manager'] = self.get_kosik_config(request)
        
        if hasattr(self, 'isas_sklad_config'): 
            sklad_cfg = self.isas_sklad_config
            if isinstance(sklad_cfg, str):
                try: sklad_cfg = json.loads(sklad_cfg)
                except Exception: pass
            config['sklad_config'] = sklad_cfg
            safe_sklad_json = json.dumps(sklad_cfg).replace('</', '\\u003c/')
            head_html = format_html('{}<script>window.ISAS_SKLAD_CONFIG = {};</script>', mark_safe(head_html), mark_safe(safe_sklad_json))

        if getattr(self, 'isas_ai_match_modal', False):
            modal_html = request.session.pop('isas_ai_modal', None)
            if modal_html:
                import base64
                config['ai_match_b64_payload'] = base64.b64encode(modal_html.encode('utf-8')).decode('utf-8')
        
        if obj and obj._meta.model_name == 'servisovanezariadenie' and getattr(obj, 'zakaznik', None):
            config['custom_back_url'] = f"/admin/Evidencia/zakaznik/{obj.zakaznik.id}/change/#highlight-device-{obj.id}"
            
        if getattr(self, 'isas_smart_matcher_enabled', False) and hasattr(self, 'get_smart_suggestions'):
            if obj is None and 'add' not in request.path and 'change' not in request.path:
                config['smart_matcher'] = self.get_smart_suggestions(request)

        if config:
            safe_config_json = json.dumps(config).replace('</', '\\u003c/')
            body_html = format_html("{}<script id='isas-core-config' type='application/json'>{}</script>", mark_safe(body_html), mark_safe(safe_config_json))

        if not (head_html or body_html):
            return response

        response.render()
        html_content = response.content.decode('utf-8')
        
        if head_html and '</head>' in html_content: html_content = html_content.replace('</head>', format_html('{}{}', mark_safe(head_html), mark_safe('</head>')))
        if body_html and '</body>' in html_content: html_content = html_content.replace('</body>', format_html('{}{}', mark_safe(body_html), mark_safe('</body>')))
            
        response.content = html_content.encode('utf-8')
        return response

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        response = super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )
        return self._inject_isas_html(request, response, obj=obj)

    # --- 🌐 UNIFIKOVANÉ URLS ---
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = []
        if self.isas_enable_fast_action:
            custom_urls.append(path('fast-action/', self.admin_site.admin_view(self.fast_action_api), name=f'{self.opts.app_label}_{self.opts.model_name}_fast-action'))
        if self.isas_enable_detective:
            custom_urls.append(path('detective-search/', self.admin_site.admin_view(self.detective_search), name=f'{self.opts.app_label}_{self.opts.model_name}_detective'))
        return custom_urls + urls

    # --- 🎨 UNIFIKOVANÝ CHANGELIST VIEW ---
    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['title'] = self.isas_title
        extra_context['preloader_text'] = self.isas_preloader_text
        
        response = super().changelist_view(request, extra_context=extra_context)

        magia_kwargs = self.isas_magia_kwargs.copy()
        
        # 🛡️ ISAS CORE: Automatická injektáž tlačidiel podľa aktívnych Mixinov a krajiny firmy
        custom_buttons = [mark_safe(magia_kwargs.get('custom_buttons_html', ''))]
        if isinstance(self, ERPExportAdminMixin):
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            krajina_erp = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else 'NEZADANE'
            
            # Tlačidlá zobrazíme IBA pre SK, CZ a pri čerstvej inštalácii (NEZADANE pre onboarding)
            if krajina_erp in ['SK', 'CZ', 'NEZADANE']:
                custom_buttons.append(mark_safe(
                    '<button type="button" id="btn-export-erp" data-action="export_erp" class="isas-action-btn btn-blue">🔒 Zamknúť (Export)</button>'
                    '<button type="button" id="btn-unlock-erp" data-action="unlock_erp" class="isas-action-btn btn-orange">🔓 Odomknúť</button>'
                ))
        if isinstance(self, ISASSecurityAdminMixin):
            custom_buttons.append(mark_safe(
                '<button type="button" id="btn-gdpr-anonymize" data-action="gdpr_anonymize" class="isas-action-btn" style="background: linear-gradient(135deg, #334155 0%, #0f172a 100%);">🕵️ Anonymizovať</button>'
            ))
        magia_kwargs['custom_buttons_html'] = mark_safe("".join(custom_buttons))

        if 'bez_tlacidla_odstranit' not in magia_kwargs:
            magia_kwargs['bez_tlacidla_odstranit'] = not request.user.has_perm(f"{self.opts.app_label}.delete_{self.opts.model_name}")
        
        head_html = get_isas_magia(**magia_kwargs)
        
        return self._inject_isas_html(request, response, head_html=head_html)

    # --- ⚡ UNIFIKOVANÉ FAST ACTION API ---
    def fast_action_api(self, request):
        if request.method != "POST": return JsonResponse({"status": "invalid"}, status=400)
        if len(request.body) > 1024 * 1024: return JsonResponse({"status": "error", "message": "❌ Payload je príliš veľký."}, status=413)
            
        try: data = json.loads(request.body)
        except json.JSONDecodeError: return JsonResponse({"status": "error", "message": "❌ Neplatný formát dát."}, status=400)

        action_type = data.get('type')
        ids = data.get('ids', [])
        reason = data.get('reason', 'Neuvedený dôvod')

        valid_ids = []
        if ids:
            if not isinstance(ids, list): return JsonResponse({"status": "error", "message": "⚠️ Neplatný zoznam ID."}, status=400)
            try: valid_ids = [int(pk) for pk in ids]
            except (ValueError, TypeError): return JsonResponse({"status": "error", "message": "❌ Neplatný formát ID."}, status=400)
        elif action_type not in ['smart_pair', 'smart_ignore', 'check_megazaloha_status', 'poll_global_status']:
            return JsonResponse({"status": "error", "message": "⚠️ Neplatný zoznam ID."}, status=400)

        if not self.has_view_or_change_permission(request):
            return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie na prístup k tomuto modulu."}, status=403)

        validation_error = self.fast_action_custom_validation(request, action_type, valid_ids, reason=reason)
        if validation_error: return validation_error 

        if action_type == 'delete' and not request.user.has_perm(f"{self.opts.app_label}.delete_{self.opts.model_name}"):
            return JsonResponse({"status": "error", "message": "❌ Nemáte oprávnenie na mazanie."}, status=403)

        return self.perform_fast_action(request, action_type, valid_ids, reason=reason, data=data)

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            if hasattr(self.model, 'erp_export_status'):
                zablokovane = self.model.objects.filter(id__in=valid_ids, erp_export_status__in=['EXPORTED', 'PROCESSING']).count()
                if zablokovane > 0:
                    return JsonResponse({"status": "error", "message": f"❌ {zablokovane} záznamov je uzamknutých alebo sa práve spracováva v účtovníctve a nedajú sa vymazať! 🔒"}, status=403)
            if hasattr(self.model, 'peppol_status'):
                peppol_zamknute = self.model.objects.filter(id__in=valid_ids, peppol_status__in=['SENT', 'PROCESSING']).count()
                if peppol_zamknute > 0:
                    return JsonResponse({"status": "error", "message": f"🛑 {peppol_zamknute} faktúr už bolo odoslaných (alebo sa práve odosielajú) do siete Peppol a podľa zákona nesmú byť vymazané!"}, status=403)
        return None

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        data = kwargs.get('data', {})
        
        if action_type == 'check_megazaloha_status':
            zaloha_id = kwargs.get('zaloha_id') or request.POST.get('zaloha_id') or data.get('zaloha_id')
            MegaZaloha = apps.get_model('Evidencia', 'MegaZaloha')
            try:
                zaloha = MegaZaloha.objects.get(id=zaloha_id)
                if zaloha.vyziadal_uzivatel != request.user and not request.user.is_superuser:
                    return JsonResponse({"status": "error", "message": "🚫 Prístup odmietnutý. Tento súbor nepatrí Vám."}, status=403)
                
                status_map = {
                    'hotova': {"status": "ready", "url": zaloha.subor_zalohy.url if zaloha.subor_zalohy else ""},
                    'chyba': {"status": "error", "message": "Nastala chyba pri generovaní súboru."}
                }
                return JsonResponse(status_map.get(zaloha.stav_spracovania, {"status": "processing"}))
            except (MegaZaloha.DoesNotExist, ValueError, TypeError):
                return JsonResponse({"status": "error", "message": "Záloha sa nenašla."})

        if action_type == 'gdpr_anonymize':
            if not request.user.is_superuser and not request.user.has_perm(f"{self.opts.app_label}.can_anonymize"):
                return JsonResponse({"status": "error", "message": "🛑 Nemáte oprávnenie na GDPR anonymizáciu!"}, status=403)
            if not hasattr(self, 'gdpr_anonymizovat_vybrane'):
                return JsonResponse({"status": "error", "message": "Tento model nepodporuje anonymizáciu."}, status=400)
            
            try:
                self.gdpr_anonymizovat_vybrane(request, self.model.objects.filter(id__in=valid_ids), reason=kwargs.get('reason', 'Neuvedený dôvod'))
                return JsonResponse({"status": "success", "message": f"🕵️ Úspešne anonymizovaných {len(valid_ids)} záznamov. Dôvod bol zapísaný do logu."})
            except Exception as e:
                return JsonResponse({"status": "error", "message": f"❌ Chyba pri anonymizácii: {str(e)}"}, status=500)

        if action_type in ['export_erp', 'unlock_erp'] and hasattr(self.model, 'je_anonymizovany'):
            anonymizovane = self.model.objects.filter(id__in=valid_ids, je_anonymizovany=True).count()
            if anonymizovane > 0:
                return JsonResponse({"status": "error", "message": f"{anonymizovane} z vybraných záznamov je už anonymizovaných. Tieto záznamy sú 'mŕtve' a nie je možné ich exportovať ani odomykať!"}, status=403)

        if action_type == 'export_erp':
            if not self.has_change_permission(request): return JsonResponse({"status": "error", "message": "Nemáte oprávnenie na export dokladov. 🛑"}, status=403)
            if not hasattr(self.model, 'erp_export_status'): return JsonResponse({"status": "error", "message": "Tento model nepodporuje ERP export."}, status=400)

            # 🛡️ ISAS ARCHITECT: Kontrola krajiny pred spustením Celery úlohy
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            krajina_kod = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else 'NEZADANE'

            if krajina_kod == 'NEZADANE':
                return JsonResponse({
                    "status": "error",
                    "message": "⚠️ Nastavenie systému: Pred exportom do účtovníctva musíte najskôr vyplniť 'Profil spoločnosti' (Firemná identita) v sekcii Systém – chýba IČO a štát sídla."
                }, status=400)

            if krajina_kod not in ['SK', 'CZ']:
                return JsonResponse({
                    "status": "error",
                    "message": f"🚫 ERP export (Stormware Pohoda / KROS Omega) je legislatívne určený výhradne pre firmy so sídlom v SR a ČR. Vaša firma má nastavené sídlo v '{krajina_kod}'."
                }, status=400)

            identita_obj = FiremnaIdentitaModel.objects.first()
            erp_format = (getattr(identita_obj, 'erp_format', '') or getattr(identita_obj, 'uctovny_program', '') or '').upper()
            if krajina_kod != 'SK' and any(kros_kw in erp_format for kros_kw in ['OMEGA', 'KROS']):
                return JsonResponse({
                    "status": "error",
                    "message": f"🚫 Účtovný systém KROS Omega je legislatívne a funkčne určený výhradne pre subjekty so sídlom v SR. Vaša firma má sídlo v '{krajina_kod}'. Pre ČR použite systém Stormware Pohoda."
                }, status=400)
            
            try: from Evidencia.tasks import task_vygeneruj_a_odosli_erp_export
            except ImportError: return JsonResponse({"status": "error", "message": "Chýba modul pre ERP export."}, status=500)

            with transaction.atomic():
                zaznamy = self.model.objects.select_for_update().filter(id__in=valid_ids)
                zablokovane = zaznamy.filter(erp_export_status__in=['EXPORTED', 'PROCESSING']).count()
                if zablokovane > 0: return JsonResponse({"status": "error", "message": f"❌ {zablokovane} záznamov sa už spracováva alebo sú uzamknuté v účtovníctve! 🔒"}, status=403)
                
                zaznamy.update(erp_export_status='PROCESSING')
                celery_error = []

                def dispatch_erp_task(m=self.model.__name__, v=None, u=request.user.id):
                    if v is None:
                        v = list(valid_ids)
                    try:
                        task_vygeneruj_a_odosli_erp_export.delay(m, v, u)
                    except Exception as exc:
                        logger.warning("⚠️ Výpadok Celery/Redis brokera pri odosielaní ERP exportu pre model %s: %s", m, exc)
                        messages.warning(request, "⚠️ Úloha na export do ERP nebola zaradená do fronty z dôvodu výpadku brokera správ (Redis).")
                        # Odomknutie záznamov zo stavu PROCESSING späť do NEW pre zabránenie uviaznutia
                        self.model.objects.filter(id__in=v, erp_export_status='PROCESSING').update(erp_export_status='NEW')
                        celery_error.append(str(exc))

                transaction.on_commit(dispatch_erp_task)

            if celery_error:
                return JsonResponse({
                    "status": "error",
                    "message": "⚠️ Doklady boli pripravené, ale odoslanie do spracovania zlyhalo (výpadok Celery/Redis). Záznamy boli bezpečne uvoľnené na opravu."
                }, status=503)

            return JsonResponse({"status": "success", "message": f"📦 {len(valid_ids)} záznamov sa spracováva na pozadí. Export bude odoslaný účtovníčke na e-mail. 🚀"})

        if action_type == 'unlock_erp':
            if not request.user.is_superuser: return JsonResponse({"status": "error", "message": "⛔ Na odomknutie dokladov potrebujete práva SuperAdmina."}, status=403)
            if not hasattr(self.model, 'erp_export_status'): return JsonResponse({"status": "error", "message": "Tento model nepodporuje ERP export."}, status=400)
            
            with transaction.atomic():
                self.model.objects.select_for_update().filter(id__in=valid_ids).update(erp_export_status='NEW', erp_id=None)
            return JsonResponse({"status": "success", "message": f"🔓 {len(valid_ids)} záznamov bolo odomknutých pre opravu. 🛠️"})

        if action_type == 'delete':
            from django.db.models import ProtectedError
            try:
                queryset = self.model.objects.filter(id__in=valid_ids)
                count = sum(1 for obj in queryset if obj.delete()[0] > 0) if self.isas_delete_iterate else queryset.delete()[0]
                
                if count == 0: return JsonResponse({"status": "error", "message": "⚠️ Záznamy už boli pravdepodobne vymazané iným procesom."}, status=400)
                return JsonResponse({"status": "success", "message": self.get_delete_success_message(count)})
            except ProtectedError:
                return JsonResponse({"status": "error", "message": "❌ Záznam nie je možné vymazať, pretože sa k nemu viaže historická evidencia. Záznam musí ostať v databáze ako nosič histórie."}, status=403)
            except Exception as e:
                return JsonResponse({"status": "error", "message": f"🚨 Chyba servera: {str(e)}"}, status=400)

        # --- 🔄 UNIVERZÁLNY ISAS JSON POLLER ---
        if action_type == 'poll_global_status':
            if not self.has_view_or_change_permission(request):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie."}, status=403)
                
            fields_to_render = data.get('fields', [])
            results = {}
            
            # Optimalizácia: Načítame len tie objekty, ktoré reálne potrebujeme
            queryset = self.get_queryset(request).filter(id__in=valid_ids)
            
            for obj in queryset:
                obj_data = {}
                for field in fields_to_render:
                    if hasattr(self, field):
                        method = getattr(self, field)
                        try:
                            # Dynamicky zavoláme metódu z admin.py (napr. get_akcie_card(obj))
                            obj_data[field] = method(obj)
                        except Exception as e:
                            obj_data[field] = format_html("<!-- Error rendering {}: {} -->", field, str(e))
                results[str(obj.id)] = obj_data
                
            return JsonResponse({"status": "success", "results": results})

        return JsonResponse({"status": "error", "message": "❌ Neznáma akcia."}, status=400)

    def get_delete_success_message(self, count):
        """🪝 Hook pre dynamické formátovanie hlášky po zmazaní (vrátane skloňovania)."""
        if self.isas_delete_success_message:
            return self.isas_delete_success_message.format(count=count)
        return f"✅ Úspešne odstránené: {count} záznamov!"

    # --- 🕵️ UNIFIKOVANÝ DETECTIVE SEARCH ---
    def detective_search(self, request):
        if not request.user.has_perm(f"{self.opts.app_label}.view_{self.opts.model_name}"):
            return JsonResponse({'results': []}, status=403)

        term = request.GET.get('term', '').strip()
        if len(term) < 2 or len(term) > 100: return JsonResponse({'results': []})
            
        qs = self.get_detective_queryset(request, term)
        results = [self.format_detective_result(obj) for obj in qs]
        return JsonResponse({'results': results})

    def get_detective_queryset(self, request, term): return self.get_queryset(request).none()
    def format_detective_result(self, obj): return {'id': obj.id, 'name': str(obj), 'cat': 'ZÁZNAM'}

class ISASBaseRelatedFilter(admin.SimpleListFilter):
    """🚀 ISAS CORE: Univerzálna base trieda pre optimalizované relačné filtre (Exists namiesto JOIN)."""
    isas_default_field: str | None = None
    isas_icon = ""
    isas_order_by: str | None = None

    def __init__(self, request, params, model, model_admin):
        self.isas_model_admin = model_admin
        super().__init__(request, params, model, model_admin)

    def get_field_name(self):
        return getattr(self.isas_model_admin, f"isas_{self.parameter_name}_filter_field", self.isas_default_field)

    def get_related_queryset(self, model_admin, field_name):
        if not field_name or not hasattr(model_admin.model, field_name):
            return model_admin.model._default_manager.none()
        field_attr = getattr(model_admin.model, field_name)
        if not hasattr(field_attr, 'field') or not hasattr(field_attr.field, 'related_model'):
            return model_admin.model._default_manager.none()
        related_model = field_attr.field.related_model
        if not related_model:
            return model_admin.model._default_manager.none()
        qs = related_model.objects.filter(Exists(model_admin.model.objects.filter(**{field_name: OuterRef('pk')})))
        if self.isas_order_by: qs = qs.order_by(self.isas_order_by)
        return qs.distinct()

    def format_item(self, obj):
        return format_html('{} {}', self.isas_icon, str(obj))

    def lookups(self, request, model_admin):
        return [(obj.id, self.format_item(obj)) for obj in self.get_related_queryset(model_admin, self.get_field_name())]

    def queryset(self, request, queryset):
        if self.value(): return queryset.filter(**{f"{self.get_field_name()}__id": self.value()})
        return queryset

class ISASRelatedUserFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon = '👤 Používateľ', 'related_user', 'user', '👤'

    def get_field_name(self):
        return getattr(self.isas_model_admin, 'isas_user_filter_field', self.isas_default_field)

    def lookups(self, request, model_admin):
        field_name = self.get_field_name()
        
        # Zachovaná špecifická optimalizácia cez values_list
        aktivni = get_user_model().objects.filter(Exists(model_admin.model.objects.filter(**{field_name: OuterRef('pk')}))).values_list('id', 'username').order_by('username')
        lookups_data = [(str(uid), format_html('{} {}', self.isas_icon, uname)) for uid, uname in aktivni if uname]
        
        if getattr(self.isas_model_admin, 'isas_user_filter_allow_auto', False) and model_admin.model.objects.filter(**{f"{field_name}__isnull": True}).exists():
            lookups_data.insert(0, ('auto', '🤖 AUTO-SYSTÉM'))
        return lookups_data

    def queryset(self, request, queryset):
        if self.value():
            field_name = self.get_field_name()
            return queryset.filter(**{f"{field_name}__isnull": True}) if self.value() == 'auto' else queryset.filter(**{f"{field_name}__id": self.value()})
        return queryset

class ISASRelatedTypServisuFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon = '🛠️ Typ servisu / práce', 'typ_servisu', 'typ_servisu', '🧰'

class ISASRelatedZariadenieFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon = '⚙️ Servisované zariadenie', 'zariadenie', 'zariadenie', '⚙️'

class ISASRelatedTechnikFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon = '👨‍🔧 Technik', 'technici', 'technici', '👷‍♂️'
    def format_item(self, obj): return format_html('{} {}', self.isas_icon, f"{getattr(obj, 'meno', '')} {getattr(obj, 'priezvisko', '')}".strip() or str(obj))

class ISASRelatedZakaznikFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon, isas_order_by = '🏢 Zákazník', 'zakaznik', 'zakaznik', '🏬', 'Nazov_spolocnosti'
    def format_item(self, obj): return format_html('{} {}', self.isas_icon, getattr(obj, 'Nazov_spolocnosti', str(obj)))

class ISASRelatedPoziciaFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon, isas_order_by = '🎯 Pracovná pozícia', 'pozicia', 'pozicia', '💼', 'poradie'
    def format_item(self, obj): return format_html('{} {}', self.isas_icon, getattr(obj, 'nazov', str(obj)))

class ISASDriverTypeFilter(admin.SimpleListFilter):
    title, parameter_name = '👤 Typ vodiča (Kto šoféroval)', 'typ_vodica'
    def __init__(self, request, params, model, model_admin):
        self.isas_model_admin = model_admin
        super().__init__(request, params, model, model_admin)
    def lookups(self, request, model_admin): return (('zamestnanec', '👨‍🔧 Zamestnanec firmy'), ('osoba', '👤 Súkromná osoba'))
    def queryset(self, request, queryset):
        if self.value() == 'zamestnanec': return queryset.filter(**{f"{getattr(self.isas_model_admin, 'isas_driver_field_employee', 'zamestnanec')}__isnull": False})
        if self.value() == 'osoba': return queryset.filter(**{f"{getattr(self.isas_model_admin, 'isas_driver_field_person', 'sukromna_osoba')}__isnull": False})
        return queryset

class ISASDriverAdminMixin:
    isas_driver_field_employee, isas_driver_field_person = 'zamestnanec', 'sukromna_osoba'
    isas_exclusive_field_groups = [('zamestnanec', ['sukromna_osoba']), ('sukromna_osoba', ['zamestnanec'])]

    @admin.display(description='👤 Vodič')
    def get_vodic_badge(self, obj):
        zamestnanec, osoba = getattr(obj, self.isas_driver_field_employee, None), getattr(obj, self.isas_driver_field_person, None)
        if zamestnanec: return render_isas_badge(text=str(zamestnanec), icon="👨‍🔧", type_color="info")
        if osoba: return render_isas_badge(text=str(osoba), icon="👤", type_color="danger")
        return render_isas_badge(text="Neznámy / Nepriradené", icon="❓", type_color="neutral")

class ISASWebstrankaFilter(admin.SimpleListFilter):
    title, parameter_name = '🌐 Webstránka (Doména)', 'domena_stav'
    def lookups(self, request, model_admin): return (('ma', '🌐 Má webstránku'), ('nema', '❌ Nemá webstránku'))
    def queryset(self, request, queryset):
        cond = Q(domena__exact='') | Q(domena__isnull=True)
        if self.value() == 'ma': return queryset.exclude(cond)
        if self.value() == 'nema': return queryset.filter(cond)
        return queryset

class ISASKontaktnaOsobaFilter(admin.SimpleListFilter):
    title, parameter_name = '👤 Kontaktná osoba', 'osoba_stav'
    def lookups(self, request, model_admin): return (('priradena', '👤 Priradená'), ('chyba', '⚠️ Chýba'))
    def queryset(self, request, queryset):
        cond = Q(kontaktna_osoba__exact='-') | Q(kontaktna_osoba__exact='') | Q(kontaktna_osoba__isnull=True)
        if self.value() == 'priradena': return queryset.exclude(cond)
        if self.value() == 'chyba': return queryset.filter(cond)
        return queryset

class KontaktnaOsobaAdminMixin:
    isas_autofill_rules = {'trigger_field': 'kontaktna_osoba', 'action': 'unlock_fields', 'target_fields': ['oslovenie_rod']}
    isas_clean_choice_fields = {'oslovenie_rod': '🗣️ Vyberte oslovenie'}

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'kontaktna_osoba' and field is not None:
            orig_help = getattr(field, 'help_text', "") or ""
            field.help_text = format_html(
                '{}<br><span style="color:#64748b;font-size:11px;">💡 <i>Oslovenie sa odomkne až po zadaní mena kontaktnej osoby.</i></span>',
                orig_help
            )
        return field

class ISASObchodnyPartnerAdminMixin:
    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'stat' and 'initial' not in kwargs:
            try:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                stat_sidla = FiremnaIdentitaModel.get_country_defaults().get('stat_nazov')
                if stat_sidla and stat_sidla != 'International':
                    kwargs['initial'] = stat_sidla
            except Exception:
                pass
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    @admin.display(description='🪪 Firemné údaje')
    def get_ids_pills(self, obj):
        lines = []
        if getattr(obj, 'ico', None) and obj.ico != '-':
            # 1. Kaskádová detekcia krajiny: IČ DPH -> Štát partnera -> Doména -> Predvolená krajina firmy
            krajina = str(obj.ic_dph).upper().strip()[:2] if getattr(obj, 'ic_dph', None) and str(obj.ic_dph).upper().strip()[:2].isalpha() else None
            
            if not krajina and getattr(obj, 'stat', None):
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                detegovany_stat = FiremnaIdentitaModel.get_aktivna_krajina_iso(obj.stat) if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else None
                if detegovany_stat and detegovany_stat not in ['NEZADANE', 'INYKRAJINA']:
                    krajina = detegovany_stat

            if not krajina and getattr(obj, 'domena', None):
                clean_domena = str(obj.domena).lower().replace('https://', '').replace('http://', '').split('/')[0].strip()
                tld_map = {
                    '.sk': 'SK', '.cz': 'CZ', '.at': 'AT', '.de': 'DE', '.hu': 'HU', '.pl': 'PL',
                    '.it': 'IT', '.fr': 'FR', '.nl': 'NL', '.be': 'BE', '.es': 'ES', '.pt': 'PT',
                    '.ch': 'CH', '.uk': 'GB', '.co.uk': 'GB', '.ro': 'RO', '.bg': 'BG', '.hr': 'HR',
                    '.si': 'SI', '.ua': 'UA', '.se': 'SE', '.no': 'NO', '.dk': 'DK', '.fi': 'FI',
                    '.ie': 'IE', '.lu': 'LU', '.gr': 'GR', '.us': 'US', '.ca': 'CA'
                }
                krajina = next((iso for tld, iso in tld_map.items() if clean_domena.endswith(tld)), ('EU' if '.' in clean_domena else None))

            if not krajina:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                krajina = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else None

            # 2. Inteligentné priradenie registra: 1. DB (subdiv), 2. Verejný katalóg, 3. EÚ VIES
            link_html = ""
            if krajina:
                StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
                stat_obj = StatLokalizaciaModel.get_for_iso(krajina) if hasattr(StatLokalizaciaModel, 'get_for_iso') else None
                konfig = getattr(stat_obj, 'subdiv_konfiguracia', {}) if (stat_obj and isinstance(getattr(stat_obj, 'subdiv_konfiguracia', None), dict)) else {}
                
                # 🏛️ 100% Data-Driven priradenie registra (DB konfigurácia StatLokalizacia s fallbackom na EÚ BRIS)
                reg_url = konfig.get('register_url')
                reg_ikona = konfig.get('register_ikona', '🏛️')

                if reg_url:
                    clean_url = str(reg_url).replace('{ico}', urllib.parse.quote(str(obj.ico)))
                    link_html = render_isas_link_btn(clean_url, text="", icon=reg_ikona, type_color="neutral", square=True, wrapper=False)
                elif stat_obj and stat_obj.je_clen_eu:
                    # Oficiálny portál obchodných registrov Európskej komisie (BRIS / e-Justice)
                    link_html = render_isas_link_btn("https://e-justice.europa.eu/content_find_a_company-489-en.do", text="", icon="🇪🇺", type_color="neutral", square=True, wrapper=False)

            lines.append(render_isas_badge(obj.ico, prefix_text="IČO:", type_color="neutral", monospace=True, suffix_html=link_html, wrapper=False))
        
        if getattr(obj, 'dic', None) and obj.dic != '-': lines.append(render_isas_badge(obj.dic, prefix_text="DIČ:", type_color="neutral", monospace=True, wrapper=False))
        if getattr(obj, 'ic_dph', None) and obj.ic_dph != '-': lines.append(render_isas_badge(obj.ic_dph, prefix_text="IČ DPH:", type_color="neutral", monospace=True, wrapper=False))
        if getattr(obj, 'erp_predkontacia', None): lines.append(render_isas_badge(obj.erp_predkontacia, type_color="purple", wrapper=False, prefix_text="ERP:", monospace=True))
        
        return render_isas_layout(mark_safe("".join(lines)), layout="col", gap=4, center=True) if lines else render_isas_text("🚫 Nezadané", muted=True)

    @admin.display(description='📍 Sídlo', ordering='mesto')
    def get_hq_address(self, obj):
        plna_adresa = ", ".join(str(c).strip() for c in [getattr(obj, 'ulica_a_cislo', ''), getattr(obj, 'mesto', ''), getattr(obj, 'psc', ''), getattr(obj, 'stat', '')] if c)
        if not plna_adresa: return render_isas_text("🚫 Nezadané", muted=True)
        
        btn = render_isas_link_btn(f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(plna_adresa)}", "", icon="📍", type_color="info", square=True, wrapper=False)
        txt = render_isas_text(plna_adresa, size=11, color="#475569", weight=700, align="center", max_width=140)
        return render_isas_layout(format_html('{}{}', btn, txt), layout="col", gap=6, center=True)

    @admin.display(description='📞 Kontakt')
    def get_contact_card(self, obj):
        items = []
        if getattr(obj, 'kontaktna_osoba', None) and obj.kontaktna_osoba != '-': items.append(render_isas_text(obj.kontaktna_osoba, size=11, color="#0f172a", weight=800, icon="👤"))
        if getattr(obj, 'telefon', None) and obj.telefon != '-': items.append(render_isas_badge(obj.telefon, icon="📞", copy_value=obj.telefon, wrapper=False))
        if getattr(obj, 'email', None) and obj.email != '-': items.append(render_isas_badge(obj.email, icon="✉️", copy_value=obj.email, wrapper=False))
        return render_isas_layout(mark_safe("".join(items)), layout="col", gap=4, center=True) if items else render_isas_text("🚫 Nezadaný", muted=True)

    @admin.display(description='🌐 Web a Portál')
    def get_web_link(self, obj):
        items = []
        domena = getattr(obj, 'domena', None)
        subdomena = getattr(obj, 'subdomena', None)

        # 1. Firemný web partnera (Zakaznik aj Dodavatel)
        if domena:
            clean_dom = escape(str(domena).strip())
            web_url = clean_dom if clean_dom.startswith(('http://', 'https://')) else f"https://{clean_dom}"
            items.append(render_isas_link_btn(web_url, "Web", icon="🌍", type_color="neutral", wrapper=False))

        # 2. ISAS IoT Klientsky Portál (iba ak model obsahuje pole subdomena a je vyplnené)
        main_domain = getattr(settings, 'MAIN_TELEMETRY_DOMAIN', None)
        if subdomena and main_domain:
            clean_sub = escape(str(subdomena).lower().strip())
            portal_url = f"https://{clean_sub}.{main_domain}"
            items.append(render_isas_link_btn(portal_url, "Portál", icon="⚡", type_color="info", wrapper=False))

        if items:
            return render_isas_layout(mark_safe("".join(items)), layout="col", gap=4, center=True)

        return render_isas_link_btn(reverse(f'admin:{obj._meta.app_label}_{obj._meta.model_name}_change', args=[obj.id]), "Pridať web", icon="➕", type_color="dashed", wrapper=True)

class FilterSkladomMixin:
    extra, can_delete = 0, False
    def get_queryset(self, request): return super().get_queryset(request).filter(pocet_na_sklade__gt=0)

class ISASDynamicCategoryAjaxMixin:
    def get_urls(self):
        from django.urls import path
        return [
            path("ajax/get-subcategories/", self.admin_site.admin_view(self.get_subcategories), name=f"{self.opts.model_name}_get_subcategories"),
            path("ajax/get-products/", self.admin_site.admin_view(self.get_products), name=f"{self.opts.model_name}_get_products"),
        ] + super().get_urls()

    def get_subcategories(self, request):
        from Evidencia.industry_loader import CATEGORY_MAP
        cat = request.GET.get("category")
        return JsonResponse({"subcategories": list(CATEGORY_MAP[cat].keys()) if cat in CATEGORY_MAP else []})

    def get_products(self, request):
        from Evidencia.industry_loader import CATEGORY_MAP
        cat, subcat = request.GET.get("category"), request.GET.get("subcategory")
        if cat in CATEGORY_MAP and subcat in CATEGORY_MAP[cat]:
            return JsonResponse({"products": [{"id": obj.pk, "label": f"{obj.seriove_cislo} ({obj.pocet_na_sklade} {getattr(obj, 'merna_jednotka', 'ks')})"} for obj in CATEGORY_MAP[cat][subcat].objects.all()]})
        return JsonResponse({"products": []})

class SkladoveModelyFilter(admin.SimpleListFilter):
    title, parameter_name = '📁 Kategória dielu/položky/zariadenia', 'model_filter'

    def lookups(self, request, model_admin):
        from Evidencia.industry_loader import CATEGORY_MAP
        hlavna_kat = request.GET.get('hlavna_kat')
        lookups_data = [(cls.__name__, emoji) for emoji, cls in CATEGORY_MAP[hlavna_kat].items()] if hlavna_kat in CATEGORY_MAP else [(cls.__name__, emoji) for sub in CATEGORY_MAP.values() for emoji, cls in sub.items()]
        return sorted(lookups_data, key=lambda x: x[1])

    def queryset(self, request, queryset):
        if self.value():
            try:
                from Evidencia.industry_loader import CATEGORY_MAP
                target_model = next((cls for sub in CATEGORY_MAP.values() for cls in sub.values() if cls.__name__ == self.value()), None)
                if target_model:
                    return queryset.filter(content_type=ContentType.objects.get_for_model(target_model))
            except Exception: pass
        return queryset

class HlavnaKategoriaKatalogFilter(admin.SimpleListFilter):
    title, parameter_name, template = '📂 Hlavná kategória', 'hlavna_kat', 'filters_and_notifications/locked_filter.html'

    def lookups(self, request, model_admin):
        from Evidencia.industry_loader import CATEGORY_CHOICES
        return CATEGORY_CHOICES

    def queryset(self, request, queryset):
        from Evidencia.industry_loader import CATEGORY_MAP
        if self.value() in CATEGORY_MAP:
            return queryset.filter(content_type_id__in=[ct.id for ct in ContentType.objects.get_for_models(*CATEGORY_MAP[self.value()].values()).values()])
        return queryset

class ISASDateRangeFilter(admin.SimpleListFilter):
    title, parameter_name = '📅 Časové obdobie', 'obdobie'

    def __init__(self, request, params, model, model_admin):
        self.isas_model_admin = model_admin
        super().__init__(request, params, model, model_admin)

    def lookups(self, request, model_admin):
        return [('dnes', '🌅 Dnes'), ('vcera', '⏪ Včera'), ('tyzden', '📆 Posledných 7 dní / Tento týždeň'), ('mesiac', '🗓️ Posledných 30 dní / Tento mesiac'), ('minuly_mesiac', '🕰️ Minulý mesiac'), ('rok', '📊 Tento rok'), ('buducnost', '🔜 Budúce / Plánované')]

    def queryset(self, request, queryset):
        val = self.value()
        if not val: return queryset
            
        date_field = getattr(self.isas_model_admin, 'isas_date_filter_field', 'vytvorene')
        dnes = timezone.now().date()
        
        filters = {
            'dnes': Q(**{f"{date_field}__date": dnes}) | Q(**{f"{date_field}": dnes}),
            'vcera': Q(**{f"{date_field}__date": dnes - datetime.timedelta(days=1)}) | Q(**{f"{date_field}": dnes - datetime.timedelta(days=1)}),
            'tyzden': Q(**{f"{date_field}__gte": dnes - datetime.timedelta(days=dnes.weekday())}),
            'mesiac': Q(**{f"{date_field}__year": dnes.year, f"{date_field}__month": dnes.month}),
            'minuly_mesiac': Q(**{f"{date_field}__year": (dnes.replace(day=1) - datetime.timedelta(days=1)).year, f"{date_field}__month": (dnes.replace(day=1) - datetime.timedelta(days=1)).month}),
            'rok': Q(**{f"{date_field}__year": dnes.year}),
            'buducnost': Q(**{f"{date_field}__gt": timezone.now()})
        }
        return queryset.filter(filters[val]) if val in filters else queryset

class ISASHasMaterialFilter(admin.SimpleListFilter):
    title, parameter_name = '📦 Spotrebovaný materiál', 'ma_material'

    def __init__(self, request, params, model, model_admin):
        self.isas_model_admin = model_admin
        super().__init__(request, params, model, model_admin)

    def lookups(self, request, model_admin): return [('ano', '🛠️ Obsahuje použitý materiál'), ('nie', '🚫 Bez použitého materiálu')]

    def queryset(self, request, queryset):
        field_name = getattr(self.isas_model_admin, 'isas_material_filter_field', 'pouzite_polozky')
        if self.value() == 'ano': return queryset.filter(**{f"{field_name}__isnull": False}).distinct()
        if self.value() == 'nie': return queryset.filter(**{f"{field_name}__isnull": True})
        return queryset

class TelegramFilter(admin.SimpleListFilter):
    title = '✈️ Telegram Spojenie'
    parameter_name = 'has_telegram'

    def lookups(self, request, model_admin):
        return (
            ('yes', '✅ Má prepojený Telegram'),
            ('no', '🚫 Bez Telegramu'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(telegram_id__isnull=True).exclude(telegram_id__exact='')
        if self.value() == 'no':
            return queryset.filter(Q(telegram_id__isnull=True) | Q(telegram_id__exact=''))
        return queryset


class DiscordFilter(admin.SimpleListFilter):
    title = '🎮 Discord Spojenie'
    parameter_name = 'has_discord'

    def lookups(self, request, model_admin):
        return (
            ('yes', '✅ Má prepojený Discord'),
            ('no', '🚫 Bez Discordu'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(discord_id__isnull=True).exclude(discord_id__exact='')
        if self.value() == 'no':
            return queryset.filter(Q(discord_id__isnull=True) | Q(discord_id__exact=''))
        return queryset

class ISASInvoiceGeneratorAdminMixin:
    """🧾 ISAS CORE: Generátor zberných faktúr z ponúk a objednávok."""
    def generate_merge_invoice(self, request, source_queryset, source_type='ponuka'):        
        VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
        PolozkaVystavenejFaktury = apps.get_model('Evidencia', 'PolozkaVystavenejFaktury')
        CenovaPonuka = apps.get_model('Evidencia', 'CenovaPonuka')
        PrijataObjednavka = apps.get_model('Evidencia', 'PrijataObjednavka')
        
        with transaction.atomic():
            source_list = list(source_queryset.select_for_update().order_by('id'))
            if not source_list: return JsonResponse({"status": "error", "message": "Neboli vybrané žiadne záznamy."})

            zakaznici = {s.zakaznik_id for s in source_list}
            if len(zakaznici) > 1: return JsonResponse({"status": "error", "message": "🚫 Pozor! Zvolené záznamy patria rôznym zákazníkom. Zberná faktúra sa dá vytvoriť vždy len pre jedného klienta."})
            if None in zakaznici: return JsonResponse({"status": "error", "message": "🚫 Niektoré záznamy nemajú priradeného zákazníka."})
            
            zakaznik_id = zakaznici.pop()

            # 1. Zjednotenie extrakcie dát
            ponuky, objednavky = [], []
            if source_type == 'ponuka':
                ponuky = source_list
                if any(p.vyfakturovane.exists() for p in ponuky):
                    return JsonResponse({"status": "error", "message": f"🚫 Zlyhanie: Niektoré ponuky už boli vyfakturované!"})
                # OPRAVA: Zmena 'ponuky__in' na 'zdrojove_ponuky__in'
                objednavky = list(PrijataObjednavka.objects.filter(zdrojove_ponuky__in=ponuky).distinct())
            else:
                objednavky = source_list
                if any(o.faktury.exists() for o in objednavky):
                    return JsonResponse({"status": "error", "message": f"🚫 Niektoré objednávky už sú priradené k inej faktúre!"})
                vsetky_ponuky_ids = set()
                for o in objednavky:
                    if hasattr(o, 'zdrojove_ponuky'):
                        vsetky_ponuky_ids.update(o.zdrojove_ponuky.values_list('id', flat=True))
                ponuky = list(CenovaPonuka.objects.filter(id__in=vsetky_ponuky_ids).distinct()) if vsetky_ponuky_ids else []

            vsetky_zdroje = ponuky if ponuky else source_list
            dph_rezimy = {getattr(s, 'prenos_danovej_povinnosti', False) for s in vsetky_zdroje}
            if len(dph_rezimy) > 1: return JsonResponse({"status": "error", "message": "🚫 Nemôžete zlúčiť záznamy s odlišným DPH režimom (bežná DPH vs prenos daňovej povinnosti / reverse charge)!"})

            vsetky_meny = {getattr(s, 'mena', None) for s in (source_list + ponuky) if getattr(s, 'mena', None)}
            if len(vsetky_meny) > 1: return JsonResponse({"status": "error", "message": "🚫 Nemôžete zlúčiť doklady s rôznymi menami dokladov!"})

            # 2. Extrakcia metadát a prác
            vsetci_technici, vsetky_prace, zariadenie_id, metadata = set(), set(), None, {}
            
            for p in ponuky:
                zariadenie_id = zariadenie_id or p.zariadenie_id
                vsetky_prace.update(p.typ_servisu.values_list('id', flat=True))
                if p.interne_poznamky and p.interne_poznamky.startswith("[ISAS_METADATA_JSON]"):
                    try:
                        meta = json.loads(p.interne_poznamky[20:])
                        vsetci_technici.update(meta.get('technici_ids', []))
                        if not metadata: metadata = meta
                    except Exception: pass
            
            for o in objednavky: vsetky_prace.update(o.typ_servisu.values_list('id', flat=True))

            metadata.update({
                'zdrojove_objednavky_ids': [str(o.id) for o in objednavky],
                'zdrojove_ponuky_ids': [str(p.id) for p in ponuky],
                'technici_ids': list(vsetci_technici),
                'globalna_zlava_percento': "0.00"
            })

            # 3. Vytvorenie faktúry s rešpektovaním domácej meny a DPH členenia sídla firmy
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            identita = FiremnaIdentitaModel.objects.first()
            country_defaults = FiremnaIdentitaModel.get_country_defaults() if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
            mena_z_dokladov = vsetky_meny.pop() if vsetky_meny else None
            predvolena_mena = mena_z_dokladov or getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()
            je_prenos_dph = dph_rezimy.pop() if dph_rezimy else False
            default_dph_kod = country_defaults.get('kod_dph_prenos', 'VAT_REVERSE') if je_prenos_dph else country_defaults.get('kod_dph_vystavena', 'VAT_OUT')

            faktura = VystavenaFaktura.objects.create(
                zakaznik_id=zakaznik_id, zariadenie_id=zariadenie_id, stav='DRAFT',
                mena=predvolena_mena,
                kod_clenenia_dph=default_dph_kod,
                prenos_danovej_povinnosti=je_prenos_dph,
                cislo_objednavky_klienta=", ".join(filter(None, [o.cislo_objednavky_klienta or o.cislo_objednavky_interne for o in objednavky])),
                ai_poznamka="[ISAS_METADATA_JSON]" + json.dumps(metadata)
            )

            if ponuky: faktura.zdrojove_ponuky.set(ponuky)
            if objednavky: faktura.zdrojove_objednavky.set(objednavky)
            if vsetky_prace: faktura.typ_servisu.set(list(vsetky_prace))

            # 4. Agregácia položiek (Dynamické načítanie DPH zo sídla firmy cez GlobalneNastavenia)
            GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
            doprava_cena_spolu = Decimal('0.00')
            doprava_sadzba_dph = GlobalneNastaveniaModel.get_zakladna_dph() if hasattr(GlobalneNastaveniaModel, 'get_zakladna_dph') else Decimal('0.00')
            doprava_zdroje = {}
            agregovane_polozky = {}

            for p in ponuky:
                zlava_val = Decimal(str(p.globalna_zlava_percento)) if getattr(p, 'globalna_zlava_percento', None) is not None else Decimal('0.00')
                koef = Decimal('1.00') - (zlava_val / Decimal('100.00'))
                for pol in p.polozky.all():
                    pol_cena = Decimal(str(pol.jednotkova_cena_bez_dph)) if getattr(pol, 'jednotkova_cena_bez_dph', None) is not None else Decimal('0.00')
                    pol_mnozstvo = Decimal(str(pol.mnozstvo)) if getattr(pol, 'mnozstvo', None) is not None else Decimal('0.00')
                    pol_zlava = Decimal(str(pol.zlava_percento)) if getattr(pol, 'zlava_percento', None) is not None else Decimal('0.00')
                    pol_sadzba = Decimal(str(pol.sadzba_dph)) if getattr(pol, 'sadzba_dph', None) is not None else doprava_sadzba_dph
                    
                    cena = (pol_cena * koef).quantize(Decimal('0.01'))
                    if pol.typ_polozky == 'DOPRAVA':
                        hodnota = (cena * pol_mnozstvo).quantize(Decimal('0.01'))
                        doprava_cena_spolu += hodnota
                        doprava_sadzba_dph = pol_sadzba
                        doprava_zdroje[str(p.id)] = str(Decimal(doprava_zdroje.get(str(p.id), '0.00')) + hodnota)
                        continue

                    kluc = (pol.typ_polozky, (pol.nazov or "")[:500], pol.merna_jednotka or "ks", cena, pol_zlava, pol_sadzba, pol.content_type_id, pol.object_id, pol.zariadenie_id)
                    if kluc not in agregovane_polozky: agregovane_polozky[kluc] = {'mnozstvo': Decimal('0.00'), 'zdroje': {}}
                    
                    agregovane_polozky[kluc]['mnozstvo'] += pol_mnozstvo
                    agregovane_polozky[kluc]['zdroje'][str(p.id)] = str(Decimal(agregovane_polozky[kluc]['zdroje'].get(str(p.id), '0.00')) + pol_mnozstvo)

            # 5. Bulk Create položiek s okamžitým prepočtom medzisúčtov (bulk_create nevolá save)
            polozky_na_vytvorenie = []
            zdroje_zoznam = []
            
            for kluc, data in agregovane_polozky.items():
                mnoz = data['mnozstvo']
                cena = kluc[3]
                zlava = kluc[4]
                sadzba = kluc[5]
                cena_po_zlave = (mnoz * cena * (Decimal('1.00') - (zlava / Decimal('100.00')))).quantize(Decimal('0.01'))
                dph_pol = Decimal('0.00') if faktura.prenos_danovej_povinnosti else (cena_po_zlave * (sadzba / Decimal('100.00'))).quantize(Decimal('0.01'))
                
                polozky_na_vytvorenie.append(PolozkaVystavenejFaktury(
                    faktura=faktura, typ_polozky=kluc[0], nazov=kluc[1], merna_jednotka=kluc[2],
                    jednotkova_cena_bez_dph=cena, zlava_percento=zlava, sadzba_dph=sadzba,
                    content_type_id=kluc[6], object_id=kluc[7], zariadenie_id=kluc[8], mnozstvo=mnoz,
                    cena_po_zlave_bez_dph=cena_po_zlave, hodnota_dph=dph_pol, spolu_s_dph=cena_po_zlave + dph_pol
                ))
                zdroje_zoznam.append(data['zdroje'])
                    
            if doprava_cena_spolu > 0:
                doprava_dph = Decimal('0.00') if faktura.prenos_danovej_povinnosti else (doprava_cena_spolu * (doprava_sadzba_dph / Decimal('100.00'))).quantize(Decimal('0.01'))
                polozky_na_vytvorenie.append(PolozkaVystavenejFaktury(
                    faktura=faktura, typ_polozky='DOPRAVA', nazov="Zberný servisný výjazd a logistika",
                    mnozstvo=Decimal('1.00'), merna_jednotka='výjazd', jednotkova_cena_bez_dph=doprava_cena_spolu,
                    zlava_percento=Decimal('0.00'), sadzba_dph=doprava_sadzba_dph,
                    cena_po_zlave_bez_dph=doprava_cena_spolu, hodnota_dph=doprava_dph, spolu_s_dph=doprava_cena_spolu + doprava_dph
                ))
                zdroje_zoznam.append(doprava_zdroje)

            PolozkaVystavenejFaktury.objects.bulk_create(polozky_na_vytvorenie)

            metadata['zdroje_poloziek'] = zdroje_zoznam
            faktura.ai_poznamka = "[ISAS_METADATA_JSON]" + json.dumps(metadata)
            faktura.celkom_bez_dph = sum((p.cena_po_zlave_bez_dph for p in polozky_na_vytvorenie), Decimal('0.00'))
            faktura.celkom_dph = sum((p.hodnota_dph for p in polozky_na_vytvorenie), Decimal('0.00'))
            faktura.celkom_s_dph = sum((p.spolu_s_dph for p in polozky_na_vytvorenie), Decimal('0.00'))
            faktura.save(update_fields=['ai_poznamka', 'celkom_bez_dph', 'celkom_dph', 'celkom_s_dph'])
                    
        return JsonResponse({
            "status": "success", 
            "message": "✅ Zberná faktúra bola úspešne vygenerovaná! Presmerovávam do buildera faktúry ➡️...", 
            "redirect_url": reverse('evidencia:faktura_builder', args=[faktura.id])
        })

class ISASSmartMatcherAdminMixin:
    """🤖 ISAS CORE: Zdieľaná UI/UX logika pre AI párovanie (Smart Matcher)."""
    
    isas_smart_matcher_enabled = True
    
    def get_smart_suggestions(self, request, obj=None):
        """Musí byť implementované v child triede. Vracia zoznam slovníkov."""
        return []

class ISASTaskConversionAdminMixin:
    """
    ⚙️ ISAS CORE ARCHITECTURE: Konverzný Mixin pre modálne okná
    Poskytuje bezpečné a dynamické API endpointy pre načítanie technikov a typov servisu.
    """
    def api_get_technici(self, request):
        perm = f"{self.opts.app_label}.change_{self.opts.model_name}"
        if not request.user.has_perm(perm):
            return JsonResponse({'technici': []}, status=403)
        try:
            Zamestnanec = apps.get_model('Evidencia', 'Zamestnanec')
            technici = Zamestnanec.objects.all()
            if hasattr(Zamestnanec, 'je_anonymizovany'):
                technici = technici.filter(je_anonymizovany=False)
            technici = technici.order_by('priezvisko', 'meno')
            data = [{'id': t.id, 'text': f"{(getattr(t, 'meno', '') or '').strip()} {(getattr(t, 'priezvisko', '') or '').strip()}".strip() or f"Technik #{t.id}"} for t in technici]
            return JsonResponse({'technici': data})
        except Exception as e:
            logger.error(f"ISAS TaskConversion API Technici Error: {e}")
            return JsonResponse({'technici': []}, status=500)

    def api_get_typy_servisu(self, request):
        perm = f"{self.opts.app_label}.change_{self.opts.model_name}"
        if not request.user.has_perm(perm):
            return JsonResponse({'results': []}, status=403)
        try:
            TypServisuPrace = apps.get_model('Evidencia', 'TypServisuPrace')
            typy = TypServisuPrace.objects.all().order_by('nazov')
            data = [{'id': t.id, 'text': f"🛠️ {t.nazov}"} for t in typy]
            return JsonResponse({'results': data})
        except Exception as e:
            logger.error(f"ISAS TaskConversion API TypyServisu Error: {e}")
            return JsonResponse({'results': []}, status=500)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('api-technici/', self.admin_site.admin_view(self.api_get_technici), name=f'{self.opts.app_label}_{self.opts.model_name}_api-technici'),
            path('api-typy-servisu/', self.admin_site.admin_view(self.api_get_typy_servisu), name=f'{self.opts.app_label}_{self.opts.model_name}_api-typy-servisu'),
        ]
        return custom_urls + urls