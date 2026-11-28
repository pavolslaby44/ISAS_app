import inspect
import json
import logging
import os
import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from dateutil.relativedelta import relativedelta
from celery import chain

import nested_admin

from django import forms
from django.apps import apps
from django.conf import settings
from django.contrib.admin.exceptions import AlreadyRegistered
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError

from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import (
    Case, CharField, Count, Exists, F, IntegerField, OuterRef, Q, Sum, Value, When
)
from django.db.models.functions import Cast, Coalesce
from django.http import FileResponse, Http404, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, path, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import escape, escapejs, format_html, format_html_join
from django.utils.safestring import mark_safe

from ISASaplikacia.admin_core import (
    ERPExportAdminMixin, FilterSkladomMixin, HlavnaKategoriaKatalogFilter,
    ISASBaseRelatedFilter, ISASDateRangeFilter, ISASDriverAdminMixin,
    ISASDriverTypeFilter, ISASDynamicCategoryAjaxMixin, ISASHasMaterialFilter,
    ISASHiddenAdminMixin, ISASInvoiceGeneratorAdminMixin, ISASKontaktnaOsobaFilter,
    ISASObchodnyPartnerAdminMixin, ISASPremiumAdmin, ISASPremiumInlineMixin,
    ISASReadOnlyAdminMixin, ISASRelatedTechnikFilter, ISASRelatedPoziciaFilter, ISASRelatedTypServisuFilter,
    ISASRelatedUserFilter, ISASRelatedZakaznikFilter, ISASRelatedZariadenieFilter,
    ISASSecurityAdminMixin, ISASSingletonAdminMixin, ISASSmartMatcherAdminMixin,
    ISASWebstrankaFilter, ISASTaskConversionAdminMixin, KontaktnaOsobaAdminMixin, SkladoveModelyFilter,
    TelegramFilter, DiscordFilter
)
from ISASaplikacia.utils import (
    render_isas_animated_banner, render_isas_avatar, render_isas_badge,
    render_isas_dashboard_action_btn, render_isas_entity_card, render_isas_flex_between,
    render_isas_hero_dashboard_card, render_isas_hidden_data, render_isas_icon_stat_card,
    render_isas_image, render_isas_input_number, render_isas_key_value,
    render_isas_layout, render_isas_link_btn, render_isas_mini_stat_card,
    render_isas_progress_bar, render_isas_singleton_card, render_isas_singleton_row,
    render_isas_text
)

from .forms import (
    CATEGORY_CHOICES, CATEGORY_MAP, FLAT_CATEGORY_MAP, PARENT_MODELS, ZARIADENIA_MAP,
    EvidenciaUhradForm, MarzaActionForm, PouzityMaterialForm, PouzityMaterialVZaznamochForm,
    PovolenieNaVstupAdminForm, ServisnyZaznamAdminForm, ServisovaneZariadenieForm,
    TankovanieAdminForm, UlohaAdminForm, UniverzalnyDielForm, UpdateStockForm
)
from .models import (
    get_default_dph,
    AIAuditLog, AktualneNaSklade, AlarmLog, CenovaPonuka, CenovyKatalog, Certifikat, DashboardKvality,
    DatabazaVyrobcov, Dodavatel, ERPExportNastavenia, EvidenciaJazd,
    EvidenciaUhrad, Filialka, GlobalneNastavenieSkladu, HRDashboard, ImportDatTask, KorekciaMotohodinLog,
    KosikSPolozkamiNaDoobjednanie, LimitKategorieSkladu, LogNaskladnenia,
    MegaZaloha, ModbusKonfigurator, Nepritomnost, OdoslanaObjednavka,
    PolozkaPredpisu, PouzityMaterial, PouzityMaterialVZaznamoch,
    PovolenieNaVstup, PravidloServisnehoIntervalu, PrichodziaFaktura,
    PrijataObjednavka, ServisnyPredpis, ServisnyZaznam, ServisovaneZariadenie,
    Sklad, StockMovement, SukromnaOsoba, Tankovanie, TechnickaSpecifikacia,
    TelemetryHistory, TypServisuPrace, Uloha, Vozidlo, VystavenaFaktura,
    Zakaznik, ZakaznikPrenajom, Zamestnanec, PracovnaPozicia, Kandidat
)
from Evidencia.utils_compliance import to_date
from .utils_helper import (
    LedgerKalkulacka, get_firemna_mena, get_kategoria_nazov, get_sklad_dynamic_js_config,
    ocisti_nazov_od_jednotky
)

from .industry_loader import (
    CERTIFIKAT_CHOICES,
    get_active_parameter_reverse_names,
    get_industry_term,
    HAS_ENERGY_ESG_AUDIT,
    ZARIADENIA_COLOR_MAP,
)

logger = logging.getLogger(__name__)

class BasePouzityMaterialInline(admin.TabularInline):
    """Základný inline obsahujúci 100% zdieľanej logiky pre zobrazenie a práva."""
    extra = 0

    def is_locked(self, obj):
        """Abstraktná metóda: Každý child musí definovať, kedy je záznam uzamknutý."""
        raise NotImplementedError("Subclasses must implement is_locked()")

    @admin.display(description="📂 Hlavná kategória")
    def hlavna_kategoria(self, obj):
        if obj and obj.content_type_id:
            try:
                ct_model = obj.content_type.model_class()
                choices_dict = dict(CATEGORY_CHOICES)
                
                for main_cat, subcats in CATEGORY_MAP.items():
                    if ct_model in subcats.values():
                        return choices_dict.get(main_cat, main_cat)
            except Exception:
                pass
        return "—"

    @admin.display(description="📦 Názov dielu/položky")
    def nazov_dielu(self, obj):
        if obj and obj.content_object:
            return ocisti_nazov_od_jednotky(obj.content_object)
        return "—"

    @admin.display(description="📑 Podkategória")
    def podkategoria(self, obj):
        if obj and obj.content_type:
            return str(obj.content_type)
        return "—"

    def get_fields(self, request, obj=None):
        if self.is_locked(obj):
            return ['hlavna_kategoria', 'podkategoria', 'nazov_dielu', 'pocet']
        return ['hlavna_kategoria', 'content_type', 'object_id', 'pocet']

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if self.is_locked(obj):
            return ['hlavna_kategoria', 'podkategoria', 'nazov_dielu', 'pocet']
        if 'hlavna_kategoria' not in readonly:
            readonly.append('hlavna_kategoria')
        return readonly

    def has_view_permission(self, request, obj=None):
        return True

    def has_add_permission(self, request, obj=None):
        if self.is_locked(obj):
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if self.is_locked(obj):
            return False
        return super().has_delete_permission(request, obj)

class PouzityMaterialInline(BasePouzityMaterialInline):
    model = PouzityMaterial
    form = PouzityMaterialForm
    verbose_name_plural = "🛠️📦 DIELY/POLOŽKY ZO SKLADU POTREBNÉ NA VYKONANIE TEJTO PRÁCE/ÚLOHY"

    def is_locked(self, obj):
        return obj and getattr(obj, 'dokoncene', False)

class PouzityMaterialVZaznamochInline(BasePouzityMaterialInline):
    model = PouzityMaterialVZaznamoch
    form = PouzityMaterialVZaznamochForm
    verbose_name_plural = "🛠️📦 DIELY/POLOŽKY ZO SKLADU POUŽITÉ PRI TOMTO SERVISE"

    def is_locked(self, obj):
        return obj and getattr(obj, 'protokol_uzavrety', False)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('content_type').prefetch_related('content_object')

@admin.register(ERPExportNastavenia)
class ERPExportNastaveniaAdmin(ISASSingletonAdminMixin, ISASPremiumAdmin):
    list_display = (
        "get_erp_system_card",
        "get_core_accounts_card",
        "get_fleet_accounts_card",
        "get_automation_status_card"
    )
    list_display_links = ("get_erp_system_card",)
    
    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "🧮 Účtovníctvo: Nastavenia exportu"
    isas_preloader_text = "⏳ Vstupujete do sekcie nastavení účtovného exportu..."
    isas_enable_detective = False
    
    # Deklaratívne zamykanie polí podľa ERP systému
    isas_value_based_locks = {
        'trigger_field': 'erp_system',
        'rules': {
            'POHODA': {
                'unlock': ['ucet_banka', 'pohoda_skratka_banky', 'pohoda_predkontacia_fa', 'pohoda_predkontacia_prijata_fa', 'pohoda_predkontacia_banka_prijem', 'pohoda_predkontacia_banka_vydaj', 'pohoda_predkontacia_sluzby', 'pohoda_predkontacia_tovar'],
                'lock': ['omega_evidencia_banka', 'omega_ciselny_rad_banka', 'omega_evidencia_pokladna', 'omega_ciselny_rad_pokladna_prijem', 'omega_ciselny_rad_pokladna_vydaj', 'omega_evidencia_id', 'omega_ciselny_rad_id']
            },
            'OMEGA': {
                'unlock': ['omega_evidencia_banka', 'omega_ciselny_rad_banka', 'omega_evidencia_pokladna', 'omega_ciselny_rad_pokladna_prijem', 'omega_ciselny_rad_pokladna_vydaj', 'omega_evidencia_id', 'omega_ciselny_rad_id'],
                'lock': ['ucet_banka', 'pohoda_skratka_banky', 'pohoda_predkontacia_fa', 'pohoda_predkontacia_prijata_fa', 'pohoda_predkontacia_banka_prijem', 'pohoda_predkontacia_banka_vydaj', 'pohoda_predkontacia_sluzby', 'pohoda_predkontacia_tovar']
            }
        }
    }

    # Deklaratívna zmena labelov a help textov podľa ERP systému s daňovou lokalizáciou sídla
    @property
    def isas_dynamic_fields_ui(self):
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
            iso = str(iso).strip().upper() if iso else ''
        except Exception:
            iso = ''

        pohoda_dph_default = '343021' if iso == 'CZ' else '343023'
        pohoda_pokladna_default = 'pokladna' if iso == 'CZ' else 'pokladňa'

        return {
            'trigger_field': 'erp_system',
            'rules': {
                'OMEGA': {
                    'ucet_pokladna': {'label': '💵 Účet pre pokladňu (KROS):', 'help': '⚠️ POVINNÉ: Syntetický a analytický účet pokladne pre Omegu (napr. 211000).', 'default': '211000', 'replace': ['pokladňa', 'pokladna', '']},
                    'ucet_dph_20': {'label': '⚖️ Účet pre základnú sadzbu DPH (KROS):', 'help': '💡 Účet pre základnú sadzbu DPH (343023).', 'default': '343023', 'replace': ['343021', '']},
                    'ucet_naklady_palivo': {'label': '⛽ Nákladový účet pre tankovanie (KROS):', 'help': '💡 VOLITEĽNÉ: Nákladový účet pre Omegu (napr. 501005).', 'default': '501005', 'replace': ['']}
                },
                'POHODA': {
                    'ucet_pokladna': {'label': '💵 POHODA: Skratka pokladne (Úhrady):', 'help': "⚠️ POVINNÉ: Skratka pokladne v Pohode (napr. 'pokladňa', 'POK1').", 'default': pohoda_pokladna_default, 'replace': ['211000']},
                    'ucet_dph_20': {'label': '⚖️ Účet pre základnú sadzbu DPH (POHODA):', 'help': '💡 Účet pre základnú sadzbu DPH (343023 pre SK / 343021 pre CZ).', 'default': pohoda_dph_default, 'replace': []},
                    'ucet_naklady_palivo': {'label': '⛽ Účet / Predkontácia pre tankovanie:', 'help': "💡 VOLITEĽNÉ: Pre Pohodu skratka šablóny (napr. 'PHM').", 'default': '', 'replace': ['501005']}
                }
            }
        }

    # Deklaratívny modal s viacerými checkboxami
    isas_action_modals = {
        'btn-send-accountant': {
            'type': 'multi_checkbox_modal',
            'icon': '📧',
            'title': 'Odoslať žiadosť',
            'subtitle': 'Prajete si byť notifikovaný, keď účtovníčka formulár vyplní a schváli?',
            'action': 'send_accountant_link',
            'checkboxes': [
                {'id': 'tg', 'label': '📱 Telegram notifikácia', 'default': True},
                {'id': 'mail', 'label': '📧 E-mail notifikácia', 'default': True},
                {'id': 'discord', 'label': '🎮 Discord notifikácia', 'default': True}
            ],
            'confirm_text': '🚀 ODOSLAŤ',
            'success_msg': 'E-mail s odkazom bol úspešne odoslaný účtovníčke.'
        }
    }

    isas_magia_kwargs = {
        'skryt_vyhladavanie': True,
        'custom_buttons_html': '<button type="button" id="btn-send-accountant" class="isas-action-btn" style="background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);">📧 Odoslať účtovníčke</button>',
        'ignore_default_action_ids': ["btn-send-accountant", "btn-copy-link"]
    }

    fieldsets = (
        ('🎯 HLAVNÝ SYSTÉM A AUTOMATIZÁCIA', {
            'fields': ('erp_system', 'auto_odosielat_mailom'),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">⚙️ Zvoľte cieľový účtovný program a nastavte, či sa majú exporty automaticky odosielať účtovníčke na e-mail. 🚀</div>'
        }),
        ('📊 ZÁKLADNÁ ÚČTOVNÁ OSNOVA', {
            'fields': (
                ('ucet_pohladavky', 'ucet_zavazky'),
                ('ucet_banka', 'ucet_pokladna'),
                ('ucet_dph_20', 'ucet_dph_prenos'),
                ('pohoda_predkontacia_prijata_fa', 'pohoda_predkontacia_fa'),
                'pohoda_skratka_banky',
                ('pohoda_predkontacia_sluzby', 'pohoda_predkontacia_tovar'),
                ('pohoda_predkontacia_banka_prijem', 'pohoda_predkontacia_banka_vydaj')
            ),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">🧮 Tieto syntetické a analytické účty sa natvrdo vložia do XML/TXT súboru, aby systém automaticky spároval faktúry a úhrady. 🏦</div>'
        }),
        ('🔵 OMEGA: EVIDENCIE A ČÍSELNÉ RADY', {
            'fields': (
                ('omega_evidencia_banka', 'omega_ciselny_rad_banka'),
                ('omega_evidencia_pokladna', 'omega_ciselny_rad_pokladna_prijem', 'omega_ciselny_rad_pokladna_vydaj'),
                ('omega_evidencia_id', 'omega_ciselny_rad_id'),
            ),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">🔵 Tieto polia slúžia na presné spárovanie dokladov v systéme KROS Omega. Nastavte ich presne podľa toho, ako ich máte nakonfigurované vo Vašej Omege (Firma -> Nastavenie -> Číslovanie dokladov).</div>'
        }),
        ('🚗 FLEET, TANKOVANIE A ZAMESTNANCI', {
            'fields': (
                ('ucet_naklady_palivo', 'ucet_zavazky_zamestnanci'),
            ),
            'description': """<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">
                <p style="margin: 0 0 8px 0;">⛽ Špeciálne účty pre automatizované spracovanie bločkov z čerpacích staníc a záväzkov voči zamestnancom. 👥</p>
            </div>"""
        }),
    )

    def save_model(self, request, obj, form, change):
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
            iso = str(iso).strip().upper() if iso else ''
        except Exception:
            iso = ''

        iso_nazov = iso if iso else 'nešpecifikovaná krajina'

        if obj.erp_system == 'OMEGA' and iso != 'SK':
            self.message_user(request, f"🛑 KROS Omega (TXT) je určená výhradne pre slovenskú daňovú legislatívu a subjekty so sídlom v SR (SK). Vaša spoločnosť má sídlo v '{iso_nazov}'. Zmena nebola uložená.", level=messages.ERROR)
            return
        if obj.erp_system == 'POHODA' and iso not in ['SK', 'CZ']:
            self.message_user(request, f"🛑 Stormware Pohoda (XML) je podporovaná výhradne pre subjekty so sídlom v SK a CZ. Vaša spoločnosť má sídlo v '{iso_nazov}'. Zmena nebola uložená.", level=messages.ERROR)
            return

        super().save_model(request, obj, form, change)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
            iso = str(iso).strip().upper() if iso else ''
        except Exception:
            iso = ''

        if iso not in ['SK', 'CZ']:
            iso_nazov = iso if iso else 'nešpecifikovaná krajina'
            self.message_user(
                request,
                f"ℹ️ UPOZORNENIE PRE MEDZINÁRODNÚ PREVÁDZKU: Vaša spoločnosť má sídlo v '{iso_nazov}'. Moduly Stormware Pohoda a KROS Omega sú určené výhradne pre trhy SK a CZ. Pre váš štát systém odosiela a prijíma elektronické doklady priamo cez medzinárodnú sieť Peppol.",
                level=messages.WARNING
            )
        
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except IntegrityError:
            self.message_user(
                request, 
                "⚠️ Záznam nastavení bol práve vytvorený iným administrátorom. Vaša akcia bola zrušená, aby sa predišlo duplicite.", 
                level=messages.ERROR
            )
            return HttpResponseRedirect(request.path)

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        """🛡️ ISAS CORE: Využitie centrálneho hooku namiesto vlastného API endpointu."""
        if action_type == 'send_accountant_link':
            try:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
                iso = str(iso).strip().upper() if iso else ''
            except Exception:
                iso = ''

            # 🛑 MEDZINÁRODNÁ BRÁNA: Zákaz odosielania žiadosti pre subjekty mimo SK a CZ
            if iso not in ['SK', 'CZ']:
                iso_nazov = iso if iso else 'nešpecifikovaná krajina'
                return JsonResponse({
                    "status": "error",
                    "message": f"🛑 Exporty pre Stormware Pohoda a KROS Omega sú určené výhradne pre subjekty so sídlom v SR alebo ČR. Vaša spoločnosť má sídlo v '{iso_nazov}', kde fakturácia prebieha automaticky cez medzinárodnú sieť Peppol / UBL."
                }, status=400)

            from .tasks import task_odoslat_ziadost_uctovnicke
            try:
                data = json.loads(request.body)
                notify_tg = data.get('tg', True)
                notify_mail = data.get('mail', True)
                notify_discord = data.get('discord', True)
            except json.JSONDecodeError:
                notify_tg, notify_mail, notify_discord = True, True, True

            base_url = request.build_absolute_uri('/')[:-1]
            try:
                task_odoslat_ziadost_uctovnicke.delay(base_url, notify_tg, notify_mail, notify_discord)
            except Exception as exc:
                logger.warning(f"Zlyhalo odoslanie žiadosti účtovníčke do Celery: {exc}")
                messages.warning(request, "⚠️ Žiadosť sa nepodarilo zaradiť do fronty kvôli výpadku brokera (Celery/Redis).")
                return JsonResponse({"status": "error", "message": "Výpadok spojenia s brokerom úloh. Žiadosť nebola odoslaná."}, status=503)
            return JsonResponse({"status": "success"})
            
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description='🎯 Cieľový ERP Systém')
    def get_erp_system_card(self, obj):
        if obj.erp_system == 'POHODA':
            bg_grad = '#dcfce7, #bbf7d0'
            shadow = 'rgba(22, 101, 52, 0.1)'
            icon = '🟢'
            name = 'Stormware Pohoda'
            badge = render_isas_badge('AKTÍVNY SYSTÉM', '', 'success', wrapper=False)
        else:
            bg_grad = '#dbeafe, #bfdbfe'
            shadow = 'rgba(30, 64, 175, 0.1)'
            icon = '🔵'
            name = 'KROS Omega'
            badge = render_isas_badge('AKTÍVNY SYSTÉM', '', 'info', wrapper=False)

        return render_isas_avatar(icon=icon, title=name, subtitle='', bg_gradient=bg_grad, shadow_color=shadow, extra_html=badge)

    @admin.display(description='📊 Základná Osnova & Rady')
    def get_core_accounts_card(self, obj):
        row1 = render_isas_flex_between('📈 Pohľadávky:', render_isas_badge(obj.ucet_pohladavky, '', 'neutral', wrapper=False))
        row2 = render_isas_flex_between('📉 Záväzky:', render_isas_badge(obj.ucet_zavazky, '', 'neutral', wrapper=False))

        if obj.erp_system == 'POHODA':
            row3 = render_isas_flex_between('👨‍🔧 Služby (FAV):', render_isas_badge(obj.pohoda_predkontacia_sluzby, '', 'info', wrapper=False))
            row4 = render_isas_flex_between('📦 Tovar (FAV):', render_isas_badge(obj.pohoda_predkontacia_tovar, '', 'warning', wrapper=False))
        else:
            row3 = render_isas_flex_between('🏦 Banka (Ev/Rad):', render_isas_badge(f"{obj.omega_evidencia_banka} / {obj.omega_ciselny_rad_banka}", '', 'info', wrapper=False))
            row4 = render_isas_flex_between('💵 Pokladňa (Ev):', render_isas_badge(obj.omega_evidencia_pokladna, '', 'success', wrapper=False))

        rows_html = format_html('{}{}{}{}', row1, render_isas_singleton_row(row2), render_isas_singleton_row(row3), render_isas_singleton_row(row4))
        return render_isas_singleton_card('Účtovná osnova', '🧮', '#64748b', rows_html)

    @admin.display(description='🚗 Fleet & Zamestnanci')
    def get_fleet_accounts_card(self, obj):
        palivo = obj.ucet_naklady_palivo if obj.ucet_naklady_palivo else "Bez"
        row1 = render_isas_flex_between('⛽ Palivo:', render_isas_badge(palivo, '', 'warning', wrapper=False))
        row2 = render_isas_flex_between('👥 Záväzky (Zam.):', render_isas_badge(obj.ucet_zavazky_zamestnanci, '', 'success', wrapper=False))
        rows_html = format_html('{}{}', row1, render_isas_singleton_row(row2))
        return render_isas_singleton_card('Fleet & Zamestnanci', '🚗', '#64748b', rows_html)

    @admin.display(description='📧 Automatizácia')
    def get_automation_status_card(self, obj):
        if obj.auto_odosielat_mailom:
            badge = render_isas_badge('ZAPNUTÁ', '✅', 'success', wrapper=False)
            text = render_isas_text('Odosiela XML účtovníčke 🚀' , muted=True)
        else:
            badge = render_isas_badge('VYPNUTÁ', '🚫', 'danger', wrapper=False)
            text = render_isas_text('Len manuálne stiahnutie 📥', muted=True)
        return render_isas_layout(format_html('{}{}', badge, text), layout="col", gap=6, center=True)

@admin.register(EvidenciaUhrad)
class EvidenciaUhradAdmin(ERPExportAdminMixin, ISASPremiumAdmin):
    form = EvidenciaUhradForm
    
    isas_title = "🧮 Účtovníctvo - Evidencia úhrad"
    isas_preloader_text = "⏳ Vstupujete do sekcie evidencie úhrad..."
    
   # Deklaratívne exkluzívne zamykanie (Faktúry aj Predkroky)
    isas_exclusive_field_groups = [
        ('vystavena_faktura', ['prijata_faktura']),
        ('prijata_faktura', ['vystavena_faktura']),
        ('pomocny_zakaznik', ['pomocny_dodavatel']),
        ('pomocny_dodavatel', ['pomocny_zakaznik'])
    ]

    # Deklaratívne kaskádové odomykanie (Nahrádza chybný address_groups)
    isas_autofill_rules = [
        {'trigger_field': 'pomocny_zakaznik', 'action': 'unlock_fields', 'target_fields': ['vystavena_faktura']},
        {'trigger_field': 'pomocny_dodavatel', 'action': 'unlock_fields', 'target_fields': ['prijata_faktura']}
    ]
    
    # Deklaratívne štýlovanie riadkov pre predkroky (nahrádza CSS z CSS_A_JS_MAGIA_KASKADA_PREDKROKY)
    isas_row_styles = [
        {'contains_text': 'pomocny_zakaznik', 'styles': {'background': '#f8fafc', 'borderLeft': '4px solid #3b82f6', 'borderRadius': '8px', 'padding': '15px', 'marginBottom': '20px', 'boxShadow': '0 4px 6px -1px rgba(0,0,0,0.05)'}},
        {'contains_text': 'pomocny_dodavatel', 'styles': {'background': '#fef2f2', 'borderLeft': '4px solid #ef4444', 'borderRadius': '8px', 'padding': '15px', 'marginBottom': '20px', 'boxShadow': '0 4px 6px -1px rgba(0,0,0,0.05)'}}
    ]

    isas_magia_kwargs = {
        "live_search_endpoint": "detective-search/",
        "live_search_ikona": "🧾",
        "live_search_kategoria": "💸 Úhrada",
    }

    isas_ajax_prefilter_rules = [
        {'target_field': 'vystavena_faktura', 'source_field': 'pomocny_zakaznik', 'filter_param': 'filter_zakaznik_id'},
        {'target_field': 'prijata_faktura', 'source_field': 'pomocny_dodavatel', 'filter_param': 'filter_dodavatel_id'}
    ]

    list_display = (
        'erp_status_ikona', 
        'get_typ_dokladu_card', 
        'get_datum_card', 
        'get_sparovanie_card', 
        'get_suma_card'
    )
    list_display_links = ('get_typ_dokladu_card',)
    list_filter = ('erp_export_status', 'typ_uhrady', ISASDateRangeFilter)
    isas_date_filter_field = 'datum_uhrady'
    search_fields = ('cislo_dokladu', 'vystavena_faktura__cislo_faktury', 'prijata_faktura__variabilny_symbol', 'protiucet_iban')
    readonly_fields = ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log')
    autocomplete_fields = ['vystavena_faktura', 'prijata_faktura']

    fieldsets = (
        ('📄 IDENTIFIKÁCIA DOKLADU', {
            'fields': (('typ_uhrady', 'cislo_dokladu'), 'datum_uhrady'),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">📑 Základné informácie o type a čísle dokladu (napr. číslo bankového výpisu).</div>'
        }),
        ('💰 FINANČNÉ ÚDAJE', {
            'fields': (('suma_uhrady', 'mena'),),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">💰 Presná suma a mena transakcie.</div>'
        }),
        ('🔗 SPÁROVANIE S FAKTÚROU', {
            'fields': ('pomocny_zakaznik', 'vystavena_faktura', 'pomocny_dodavatel', 'prijata_faktura'),
            'description': '<div style="color: #ef4444; font-weight: bold; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #fecaca; background: #fef2f2; padding: 8px; border-radius: 8px;">⚠️ Úhradu prepojte LEN S JEDNOU faktúrou (buď vystavenou, alebo prijatou). Pre rýchlejšie hľadanie použite filtre (Predkroky). 🛑</div>'
        }),
        ('🏦 OSTATNÉ', {
            'fields': ('protiucet_iban', 'poznamka'),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">📝 Voliteľné údaje o protiúčte a interné poznámky.</div>'
        }),
        ('📡 ISAS ERP DIAGNOSTIKA', {
            'fields': (('erp_export_status', 'erp_id'), ('erp_last_exported_at', 'erp_error_log')),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">⚙️ Technické informácie o stave synchronizácie so systémom ERP. ⚠️ Zasahujte len v prípade chýb.</div>'
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            'vystavena_faktura',
            'vystavena_faktura__zakaznik',
            'prijata_faktura', 
            'prijata_faktura__dodavatel'
        )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj:
            readonly = list(set(readonly + ['vystavena_faktura', 'prijata_faktura']))
        return readonly

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj:
            # Vytvoríme inštančnú kópiu polí pre konkrétny request bez mutácie triedy
            form.base_fields = form.base_fields.copy()
            if 'pomocny_zakaznik' in form.base_fields:
                form.base_fields['pomocny_zakaznik'].widget = forms.HiddenInput()
            if 'pomocny_dodavatel' in form.base_fields:
                form.base_fields['pomocny_dodavatel'].widget = forms.HiddenInput()
        return form

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'vystavena_faktura__zakaznik', 'prijata_faktura__dodavatel'
        ).annotate(
            suma_str=Cast('suma_uhrady', output_field=CharField())
        ).filter(
            Q(cislo_dokladu__icontains=term) |
            Q(protiucet_iban__icontains=term) |
            Q(suma_str__icontains=term) |
            Q(vystavena_faktura__cislo_faktury__icontains=term) |
            Q(prijata_faktura__variabilny_symbol__icontains=term)
        )[:30]

    def format_detective_result(self, obj):
        safe_name = obj.cislo_dokladu if obj.cislo_dokladu else f"Úhrada #{obj.id}"
        partner = ""
        if obj.vystavena_faktura and obj.vystavena_faktura.zakaznik:
            partner = obj.vystavena_faktura.zakaznik.Nazov_spolocnosti
        elif obj.prijata_faktura and obj.prijata_faktura.dodavatel:
            partner = obj.prijata_faktura.dodavatel.Nazov_spolocnosti
            
        return {
            'id': obj.id, 
            'name': safe_name, 
            'search_value': safe_name,
            'cat': f"💸 {obj.typ_uhrady}",
            'suma': f"{obj.suma_uhrady or Decimal('0.00')} {obj.mena or get_firemna_mena()}",
            'date': obj.datum_uhrady.strftime('%d.%m.%Y') if obj.datum_uhrady else "",
            'rola': partner if partner else "Neznámy partner"
        }

    @admin.display(description='📑 Doklad', ordering='cislo_dokladu')
    def get_typ_dokladu_card(self, obj):
        ikony = {
            'BV': ('🏦', '#dbeafe, #bfdbfe', 'rgba(30, 64, 175, 0.1)', 'info', 'Banka'),
            'PPD': ('📥', '#dcfce7, #bbf7d0', 'rgba(22, 101, 52, 0.1)', 'success', 'Príjem'),
            'VPD': ('📤', '#fee2e2, #fecaca', 'rgba(153, 27, 27, 0.1)', 'danger', 'Výdavok'),
            'PK': ('💳', '#f3e8ff, #e9d5ff', 'rgba(107, 33, 168, 0.1)', 'purple', 'Karta'),
            'VZ': ('🔄', '#fef3c7, #fde68a', 'rgba(146, 64, 14, 0.1)', 'warning', 'Zápočet'),
        }
        icon, bg_grad, shadow, type_color, label = ikony.get(obj.typ_uhrady, ('❓', '#f1f5f9, #e2e8f0', 'rgba(100, 116, 139, 0.1)', 'neutral', 'Neznáme'))
        cislo = obj.cislo_dokladu if obj.cislo_dokladu else "Bez čísla"

        badge = render_isas_badge(label, '', type_color, wrapper=False)

        return render_isas_avatar(
            icon=icon,
            title=cislo,
            subtitle='',
            bg_gradient=bg_grad,
            shadow_color=shadow,
            extra_html=badge
        )

    @admin.display(description='📅 Dátum', ordering='datum_uhrady')
    def get_datum_card(self, obj):
        datum = obj.datum_uhrady.strftime('%d.%m.%Y') if obj.datum_uhrady else "Neznámy"
        return render_isas_badge(datum, '📅', 'neutral', wrapper=True)

    @admin.display(description='🔗 Spárovaná Faktúra')
    def get_sparovanie_card(self, obj):
        if obj.vystavena_faktura:
            badge = render_isas_badge('Vystavená FA', '🟢', 'success', wrapper=False)
            kv = render_isas_key_value('Číslo', obj.vystavena_faktura.cislo_faktury)
            return render_isas_layout(format_html('{}{}', badge, kv), layout="col", gap=4, center=True)
            
        elif obj.prijata_faktura:
            badge = render_isas_badge('Prijatá FA', '🔴', 'danger', wrapper=False)
            vs = obj.prijata_faktura.variabilny_symbol
            identifikator = vs if vs else str(obj.prijata_faktura.id)
            label = 'VS' if vs else 'ID'
            dodavatel_nazov = obj.prijata_faktura.dodavatel.Nazov_spolocnosti[:25] if obj.prijata_faktura.dodavatel else ""
            
            kv = render_isas_key_value(label, identifikator, subtext=dodavatel_nazov)
            return render_isas_layout(format_html('{}{}', badge, kv), layout="col", gap=4, center=True)
        
        return render_isas_badge('Nespárované', '⚠️', 'warning', wrapper=True)

    @admin.display(description='💰 Suma', ordering='suma_uhrady')
    def get_suma_card(self, obj):
        if obj.vystavena_faktura_id or obj.typ_uhrady == 'PPD':
            typ_farby = 'success'
        elif obj.prijata_faktura_id or obj.typ_uhrady == 'VPD':
            typ_farby = 'danger'
        elif obj.typ_uhrady == 'VZ':
            typ_farby = 'warning'
        elif obj.typ_uhrady == 'PK':
            typ_farby = 'purple'
        else:
            typ_farby = 'info'
        hodnota = f"{obj.suma_uhrady or Decimal('0.00')} {obj.mena or get_firemna_mena()}"
        return render_isas_badge(hodnota, '', typ_farby, wrapper=True)

class CertifikatPlatnostFilter(admin.SimpleListFilter):
    title = '⏳ Stav platnosti'
    parameter_name = 'platnost_status'

    def lookups(self, request, model_admin):
        return (
            ('platne', '✅ Platné'),
            ('blizi_sa', '⚠️ Blíži sa expirácia (do 30 dní)'),
            ('expirovane', '🚨 Expirované / Neplatné'),
            ('dozivotne', '♾️ Doživotné (Bez expirácie)'),
        )

    def queryset(self, request, queryset):
        dnes = timezone.now().date()
        if self.value() == 'platne':
            return queryset.filter(Q(je_dozivotny=True) | Q(datum_platnosti__gte=dnes))
        if self.value() == 'blizi_sa':
            from datetime import timedelta
            limit = dnes + timedelta(days=30)
            return queryset.filter(je_dozivotny=False, datum_platnosti__gte=dnes, datum_platnosti__lte=limit)
        if self.value() == 'expirovane':
            # 🛡️ SQL DEFENSIVE: Zahrnutie expirovaných aj neplatných certifikátov bez vyplneného dátumu
            return queryset.filter(Q(je_dozivotny=False) & (Q(datum_platnosti__lt=dnes) | Q(datum_platnosti__isnull=True)))
        if self.value() == 'dozivotne':
            return queryset.filter(je_dozivotny=True)
        return queryset


@admin.register(apps.get_model('Evidencia', 'Certifikat'))
class CertifikatAdmin(ISASPremiumAdmin):
    """
    🚀 ISAS CORE: Prémiová administrácia odborných certifikácií a vyhlášok.
    Plná integrácia s dizajnovým systémom, detective search a farebnými stavovými kartami.
    """
    isas_title = "📜 Register certifikácií a oprávnení"
    isas_preloader_text = "⏳ Načítavam register certifikátov..."
    isas_technici_filter_field = 'zamestnanec'
    isas_autocomplete_fk_fields = {'zamestnanec': '👨‍💼 Vyberte zamestnanca'}
    isas_clean_choice_fields = {'kategoria': '📂 Vyberte kategóriu certifikátu'}

    list_display = (
        "get_certifikat_identity",
        "get_zamestnanec_badge",
        "get_kategoria_badge",
        "get_platnost_status",
        "get_dokument_link"
    )
    list_display_links = ("get_certifikat_identity",)
    list_filter = (
        CertifikatPlatnostFilter,
        'kategoria',
        'je_dozivotny',
        ISASRelatedTechnikFilter
    )
    search_fields = (
        "nazov",
        "cislo_certifikatu",
        "kategoria",
        "vydal_organ",
        "zamestnanec__meno",
        "zamestnanec__priezvisko"
    )
    date_hierarchy = 'datum_platnosti'
    autocomplete_fields = ['zamestnanec']
    readonly_fields = ('vytvorene', 'upravene')

    isas_magia_kwargs = {
        "search_placeholder": "🔍 Vyhľadávanie...",
    }

    fieldsets = (
        ('📜 ZÁKLADNÁ IDENTIFIKÁCIA OSVEDČENIA', {
            'fields': (
                'zamestnanec',
                ('nazov', 'kategoria'),
                ('cislo_certifikatu', 'vydal_organ'),
            ),
            'description': 'Základné identifikačné údaje o certifikáte, držiteľovi a certifikačnom orgáne. 🪪'
        }),
        ('⏳ ČASOVÁ PLATNOSŤ A AUDIT LEHÔT', {
            'fields': (
                ('datum_vydania', 'je_dozivotny'),
                ('datum_platnosti', 'predstih_upozornenia_dni'),
            ),
            'description': 'Systém stráži lehotu vypršania a s nastaveným predstihom dispečing upozorní na nutnosť rekvalifikácie. ⏱️'
        }),
        ('📎 ELEKTRONICKÁ DOKUMENTÁCIA A OVERENIE', {
            'fields': (
                'subor',
                'overovaci_odkaz',
                'poznamka'
            ),
            'description': 'Originálny sken certifikátu v PDF a voliteľný overovací link (Credly, register licencií). 🔗'
        }),
        ('📡 SYSTÉMOVÝ AUDIT', {
            'fields': (('vytvorene', 'upravene'),),
            'classes': ('collapse',),
            'description': 'Časové pečiatky vytvorenia a modifikácie záznamu.'
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('zamestnanec')

    def get_detective_queryset(self, request, term):
        if not term or not str(term).strip():
            return self.get_queryset(request).none()

        term_str = str(term).strip()
        term_clean = unicodedata.normalize('NFKD', term_str).encode('ascii', 'ignore').decode('utf-8').strip().lower()
        tokens = term_str.split()

        # Spätný preklad: nájdenie interných kódov kategórií s odolnosťou voči diakritike
        zodpovedajuce_kategorie = [
            kod for kod, label in CERTIFIKAT_CHOICES
            if term_str.lower() in str(label).lower() or
            term_clean in unicodedata.normalize('NFKD', str(label)).encode('ascii', 'ignore').decode('utf-8').lower()
        ]

        filtre = (
            Q(nazov__icontains=term_str) |
            Q(cislo_certifikatu__icontains=term_str) |
            Q(kategoria__icontains=term_str) |
            Q(vydal_organ__icontains=term_str) |
            Q(zamestnanec__meno__icontains=term_str) |
            Q(zamestnanec__priezvisko__icontains=term_str)
        )

        if zodpovedajuce_kategorie:
            filtre |= Q(kategoria__in=zodpovedajuce_kategorie)

        # Vyhľadávanie doživotných certifikátov podľa kľúčového slova
        if term_clean in ('dozivotny', 'dozivotne', 'dozivotna', 'dozivotie'):
            filtre |= Q(je_dozivotny=True)

        # Riešenie celého mena: "Meno Priezvisko" aj "Priezvisko Meno"
        if len(tokens) >= 2:
            prve, zvysok = tokens[0], " ".join(tokens[1:])
            filtre |= (
                (Q(zamestnanec__meno__icontains=prve) & Q(zamestnanec__priezvisko__icontains=zvysok)) |
                (Q(zamestnanec__priezvisko__icontains=prve) & Q(zamestnanec__meno__icontains=zvysok))
            )

        # Vyhľadávanie podľa 4-miestneho roku (vydanie alebo expirácia)
        if term_str.isdigit() and len(term_str) == 4:
            rok = int(term_str)
            filtre |= Q(datum_platnosti__year=rok) | Q(datum_vydania__year=rok)

        qs = self.get_queryset(request).select_related('zamestnanec').filter(filtre)
        if term_clean and not qs.exists():
            qs = self.get_queryset(request).select_related('zamestnanec').filter(
                Q(nazov__icontains=term_clean) |
                Q(cislo_certifikatu__icontains=term_clean) |
                Q(vydal_organ__icontains=term_clean) |
                Q(zamestnanec__meno__icontains=term_clean) |
                Q(zamestnanec__priezvisko__icontains=term_clean)
            )

        return qs.order_by('-datum_platnosti', 'nazov')[:30]

    def format_detective_result(self, obj):
        drzitel = f"{obj.zamestnanec.meno} {obj.zamestnanec.priezvisko}".strip() if obj.zamestnanec else "Nepriradený"
        kat_nazov = obj.get_kategoria_display() or obj.kategoria or "Všeobecný"
        stav = getattr(obj, 'stav_platnosti', 'NEPLATNY')

        if stav == 'DOZIVOTNY':
            stav_text = "♾️ Doživotný"
        elif stav == 'EXPIROVANY':
            datum_str = obj.datum_platnosti.strftime('%d.%m.%Y') if obj.datum_platnosti else 'bez dátumu'
            stav_text = f"🚨 Expirovaný ({datum_str})"
        elif stav == 'BLIZI_SA':
            dni = obj.dni_do_expiracie if obj.dni_do_expiracie is not None else 0
            datum_str = obj.datum_platnosti.strftime('%d.%m.%Y') if obj.datum_platnosti else ''
            stav_text = f"⚠️ Končí o {dni}d ({datum_str})"
        elif obj.datum_platnosti:
            stav_text = f"✅ Platný do {obj.datum_platnosti.strftime('%d.%m.%Y')}"
        else:
            stav_text = "🛑 Bez dátumu"

        cislo_str = f" [#{obj.cislo_certifikatu}]" if obj.cislo_certifikatu else ""
        cely_nazov = f"{obj.nazov}{cislo_str}"
        vsetky_hodnoty = f"{cely_nazov} {kat_nazov} {drzitel} {obj.vydal_organ or ''} {stav_text}"

        return {
            'id': obj.id,
            'name': cely_nazov,
            'search_value': vsetky_hodnoty,
            'rola': f"👨‍💼 {drzitel} | 📂 {kat_nazov} | {stav_text}",
            'cat': '📜 CERTIFIKÁT'
        }

    @admin.display(description='📜 Názov certifikátu', ordering='nazov')
    def get_certifikat_identity(self, obj):
        if not obj:
            return render_isas_text("Neznámy", muted=True)
            
        nazov = obj.nazov or "Bez názvu"
        cislo = f"Číslo: {obj.cislo_certifikatu}" if obj.cislo_certifikatu else None
        organ_badge = render_isas_badge(obj.vydal_organ, icon='🏛️', type_color='neutral', wrapper=False) if obj.vydal_organ else ""
        
        return render_isas_avatar(
            icon="📜",
            title=nazov,
            subtitle=cislo,
            bg_gradient='#1e293b, #0f172a',
            shadow_color='rgba(15, 23, 42, 0.15)',
            extra_html=organ_badge
        )

    @admin.display(description='👨‍💼 Držiteľ', ordering='zamestnanec__priezvisko')
    def get_zamestnanec_badge(self, obj):
        if not obj.zamestnanec:
            return render_isas_text("🚫 Nepriradený", muted=True)
        meno = f"{obj.zamestnanec.meno} {obj.zamestnanec.priezvisko}".strip()
        try:
            url = reverse(f'admin:{obj.zamestnanec._meta.app_label}_{obj.zamestnanec._meta.model_name}_change', args=[obj.zamestnanec.pk])
            return render_isas_link_btn(url, meno, icon="👨‍💼", type_color="info", wrapper=True)
        except Exception:
            return render_isas_badge(meno, icon="👨‍💼", type_color="info", wrapper=True)

    @admin.display(description='📂 Kategória', ordering='kategoria')
    def get_kategoria_badge(self, obj):
        kat_nazov = str(obj.get_kategoria_display() or obj.kategoria or "Nezadaná").strip()
        # 🛡️ DEFENSIVE: Detekcia, či prvé slovo skutočne obsahuje emoji symbol (Unicode > 1000)
        prve_slovo = kat_nazov.split(' ', 1)[0] if ' ' in kat_nazov else ''
        ma_emoji = bool(prve_slovo and ord(prve_slovo[0]) > 1000)
        
        ikona = prve_slovo if ma_emoji else '📂'
        cisty_nazov = kat_nazov.split(' ', 1)[1] if (ma_emoji and ' ' in kat_nazov) else kat_nazov
        return render_isas_badge(cisty_nazov, icon=ikona, type_color="neutral", wrapper=True)

    @admin.display(description='⏳ Platnosť / Expirácia', ordering='datum_platnosti')
    def get_platnost_status(self, obj):
        stav = getattr(obj, 'stav_platnosti', 'NEPLATNY')
        if stav == 'DOZIVOTNY':
            vydane = f"Od: {obj.datum_vydania.strftime('%d.%m.%Y')}" if obj.datum_vydania else "Vydané"
            badge = render_isas_badge("Doživotná platnosť", icon="♾️", type_color="success", wrapper=False)
            txt = render_isas_text(vydane, muted=True)
            return render_isas_layout(format_html('{}{}', badge, txt), layout="col", gap=2, center=True)

        if not obj.datum_platnosti:
            return render_isas_badge("Chýba dátum", icon="⚠️", type_color="danger", wrapper=True)

        dnes = date.today()
        platnost = to_date(obj.datum_platnosti)
        datum_str = platnost.strftime('%d.%m.%Y') if platnost else ''

        if stav == 'EXPIROVANY':
            dni_po = (dnes - platnost).days if platnost else 0
            badge = render_isas_badge(f"Expirované ({datum_str})", icon="🚨", type_color="danger", wrapper=False)
            txt = render_isas_text(f"Neplatné už {dni_po} dní", color="#dc2626", weight=700)
            return render_isas_layout(format_html('{}{}', badge, txt), layout="col", gap=2, center=True)

        if stav == 'BLIZI_SA':
            dni_do = obj.dni_do_expiracie if obj.dni_do_expiracie is not None else 0
            badge = render_isas_badge(f"Platí do: {datum_str}", icon="⚠️", type_color="warning", wrapper=False)
            txt = render_isas_text(f"Zostáva {dni_do} dní!", color="#d97706", weight=800)
            return render_isas_layout(format_html('{}{}', badge, txt), layout="col", gap=2, center=True)

        badge = render_isas_badge(f"Platí do: {datum_str}", icon="✅", type_color="success", wrapper=False)
        txt = render_isas_text(f"Vydané: {obj.datum_vydania.strftime('%d.%m.%Y')}", muted=True) if obj.datum_vydania else ""
        return render_isas_layout(format_html('{}{}', badge, txt), layout="col", gap=2, center=True)

    @admin.display(description='📎 Príloha a Overenie')
    def get_dokument_link(self, obj):
        prvky = []
        # 🛡️ DEFENSIVE: Izolovaný prístup k súboru s odchytením zlyhania úložiska
        if obj.subor and getattr(obj.subor, 'name', None):
            try:
                prvky.append(render_isas_link_btn(obj.subor.url, "Sken PDF", icon="📎", type_color="info", wrapper=False))
            except Exception:
                prvky.append(render_isas_badge("Chýba súbor", "⚠️", "danger", wrapper=False))

        if obj.overovaci_odkaz:
            raw_url = str(obj.overovaci_odkaz).strip()
            # 🛡️ DEFENSIVE: Čistá URL bez predčasného escape(), format_html v komponente ju escapne bezpečne
            if raw_url.startswith(('http://', 'https://')):
                prvky.append(render_isas_link_btn(raw_url, "Online overenie", icon="🔗", type_color="neutral", wrapper=False))
            else:
                prvky.append(render_isas_badge("Neplatná URL", icon="⚠️", type_color="danger", wrapper=False))

        if not prvky:
            return render_isas_text("Bez súboru", muted=True)

        return render_isas_layout(mark_safe("".join(prvky)), layout="row", gap=4, center=True)

class ZavazkyFilter(admin.SimpleListFilter):
    title = '💰 Finančný status'
    parameter_name = 'zavazky_status'

    def lookups(self, request, model_admin):
        return (
            ('cisty', '✅ Čistý štít (Bez dlhu)'),
            ('dlh', '⚠️ Má záväzky voči firme'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'cisty':
            return queryset.filter(Q(zavazky__isnull=True) | Q(zavazky=0))
        if self.value() == 'dlh':
            return queryset.filter(zavazky__gt=0)
        return queryset


class BOZPFilter(admin.SimpleListFilter):
    title = '🦺 Platnosť BOZP'
    parameter_name = 'bozp_status'

    def lookups(self, request, model_admin):
        return (
            ('platne', '✅ Platné BOZP'),
            ('expirovane', '🚨 Expirované / Chýba'),
        )

    def queryset(self, request, queryset):
        dnes = timezone.now().date()
        if self.value() == 'platne':
            return queryset.filter(bozp_platne_do__gte=dnes)
        if self.value() == 'expirovane':
            return queryset.filter(Q(bozp_platne_do__lt=dnes) | Q(bozp_platne_do__isnull=True))
        return queryset

@admin.register(Zamestnanec)
class ZamestnanecAdmin(ISASSecurityAdminMixin, ERPExportAdminMixin, ISASPremiumAdmin):
    isas_title = "👨‍💼 Zamestnanci"
    isas_preloader_text = "⏳ Vstupujete do zoznamu zamestnancov..."
    isas_clean_fk_fields = {'user': '👤 Vyberte používateľa', 'predvolena_cerpacka': '⛽ Vyberte čerpaciu stanicu'}
    
    # Deklaratívne uzamykanie polí na základe hodnoty (nahrádza JS_MAGIA_ZAMESTNANEC_FORMULAR)
    isas_value_based_locks = {
        'trigger_field': 'sposob_odmenovania',
        'rules': {
            'HODINA': {'unlock': ['mzda_brutto_hodina'], 'lock': ['mzda_brutto_mesiac'], 'clear_value': '0.00'},
            'MESIAC': {'unlock': ['mzda_brutto_mesiac'], 'lock': ['mzda_brutto_hodina'], 'clear_value': '0.00'},
        },
        'default_rule': {'lock': ['mzda_brutto_hodina', 'mzda_brutto_mesiac'], 'clear_value': '0.00'}
    }

    # Nový deklaratívny atribút pre custom modal s dvoma voľbami (nahrádza inline JS)
    isas_two_choice_modal = {
        'trigger_id': 'btn-export-mzdy',
        'icon': '📊',
        'title': 'Podklady pre mzdy',
        'subtitle': 'Čo si prajete urobiť s vygenerovaným Excelom?',
        'action_type': 'export_mzdy',
        'action_param_name': 'mzdy_action',
        'btn1_id': 'btn-mzdy-send',
        'btn1_text': '📧 Uložiť a Odoslať účtovníčke',
        'btn1_action_val': 'send',
        'btn1_style': 'background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; border: none; box-shadow: 0 4px 15px rgba(16,185,129,0.3);',
        'btn2_id': 'btn-mzdy-download',
        'btn2_text': '📥 Iba vygenerovať (Bez odoslania)',
        'btn2_action_val': 'download',
        'btn2_style': 'background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1;',
        'empty_selection_msg': 'Najskôr zaškrtnite aspoň jedného zamestnanca zo zoznamu.'
    }

    isas_magia_kwargs = {
        "live_search_endpoint": "detective-search/",
        "live_search_ikona": "👨‍💼",
        "custom_buttons_html": mark_safe('''
            <button type="button" id="btn-export-mzdy" class="isas-action-btn" style="background: linear-gradient(135deg, #10b981 0%, #047857 100%);">📊 Generovať Mzdy (Excel)</button>
        '''),
        "ignore_default_action_ids": ["btn-export-mzdy"]
    }

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        """🛡️ ISAS CORE: Využitie centrálneho hooku namiesto vlastného API endpointu."""
        if action_type == 'export_mzdy':
            if not request.user.is_staff:
                return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie."}, status=403)

            try:
                from .tasks import task_vygeneruj_mzdy_excel, task_odoslat_mzdy_uctovnicke
            except ImportError:
                from Evidencia.tasks import task_vygeneruj_mzdy_excel, task_odoslat_mzdy_uctovnicke
            
            try:
                data = json.loads(request.body)
                mzdy_action = data.get('mzdy_action', 'download')
            except json.JSONDecodeError:
                mzdy_action = 'download'

            dnes = timezone.now().date()
            prvy_den_minuleho = (dnes.replace(day=1) - relativedelta(months=1))
            posledny_den_minuleho = dnes.replace(day=1) - relativedelta(days=1)
            
            nazov = f"Podklady pre mzdy a fakturáciu - {prvy_den_minuleho.strftime('%m/%Y')}"
            retencia = '30_dni' if mzdy_action == 'send' else '24_hodin'
            poznamka = "Vygenerované a odoslané účtovníčke." if mzdy_action == 'send' else "Vygenerované len na stiahnutie."
            
            zaloha = MegaZaloha.objects.create(
                nazov_zalohy=nazov,
                typ_zalohy='podklady_mzdy',
                stav_spracovania='spracovava_sa',
                politika_uchovania=retencia,
                obsahuje_data_od=prvy_den_minuleho,
                obsahuje_data_do=posledny_den_minuleho,
                vyziadal_uzivatel=request.user,
                poznamka=poznamka
            )
            
            def _dispatch_mzdy(u_id=request.user.id, z_id=zaloha.id, v_ids=None):
                if v_ids is None:
                    v_ids = list(valid_ids)
                try:
                    if mzdy_action == 'send':
                        chain(
                            task_vygeneruj_mzdy_excel.s(u_id, z_id, v_ids),
                            task_odoslat_mzdy_uctovnicke.s()
                        ).apply_async()
                    else:
                        task_vygeneruj_mzdy_excel.delay(u_id, z_id, v_ids)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie generovania miezd do Celery: {exc}")
                    messages.warning(request, "⚠️ Generovanie miezd zlyhalo pri zaradení do fronty (výpadok Celery/Redis).")

            transaction.on_commit(_dispatch_mzdy)
            msg = "📧 Podklady pre mzdy sa generujú a po spracovaní budú odoslané účtovníčke." if mzdy_action == 'send' else "📥 Excel sa generuje. O chvíľu si ho budete môcť stiahnuť v module Systémové exporty a Zálohy."
            
            return JsonResponse({"status": "success", "message": msg})
            
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    list_display = (
        "erp_status_ikona",
        "get_rich_identity", 
        "get_contact_card",
        "get_payroll_card",  
        "get_hr_dokumenty",
        "get_dovolenka_bar",
        "get_vykonnost_stats",
        "get_zavazky_big_badge",
        "get_bozp_status",
        "get_certifikaty_badge"
    )
    list_display_links = ("get_rich_identity",)
    list_filter = ('erp_export_status', 'typ_pomeru', 'pracovna_rola', ZavazkyFilter, BOZPFilter, 'sposob_odmenovania', TelegramFilter, DiscordFilter)
    search_fields = ("meno", "priezvisko", "email", "telefon", "telegram_id", "discord_id")
    readonly_fields = ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log', 'zdiela_polohu', 'get_certifikaty_hub')
    autocomplete_fields = ['predvolena_cerpacka']
    
    fieldsets = (
        ('👨‍💼 OSOBNÉ ÚDAJE A ROLA', {
            'fields': (
                ('typ_pomeru', 'pracovna_rola'),
                ('meno', 'priezvisko'), 
                'datum_narodenia',
            ),
            'description': 'Základná identifikácia osoby. 🪪 Typ pomeru a rola určujú prístupové práva a správanie systému voči tomuto zamestnancovi. ⚙️'
        }),
        ('📍 ÚDAJE PRE ERP (FAKTURÁCIA / DIÉTY)', {
            'fields': (
                ('ulica_a_cislo', 'mesto'),
                ('psc', 'stat'),
                'iban'
            ),
            'description': '🏦 Tieto polia sú kriticky dôležité pre export do účtovníctva (Pohoda/Kros). Slúžia na preplácanie výdavkov, diét alebo fakturáciu živnostníkom. 💸'
        }),
        ('📞 KONTAKTY A PREPOJENIA', {
            'fields': (
                ('telefon', 'email'), 
                ('telegram_id', 'discord_id'),
                ('user', 'osobny_podpis'),
                'zdiela_polohu'
            ),
            'description': '📱 Kontaktné údaje. Prepojenie s "User" umožňuje prihlásenie do systému. Telegram ID slúži na komunikáciu s ISAS Botom. 🤖'
        }),
        ('🌐 VEREJNÁ PREZENTÁCIA (WEB BUILDER)', {
            'fields': (
                'zobrazit_na_webe',
                ('verejna_rola', 'profilova_fotka')
            ),
            'description': 'Nastavenie zobrazenia zamestnanca v bloku "Náš tím" na verejnej webstránke. 🌍'
        }),
        ('🔔 NOTIFIKÁCIE K ÚLOHÁM', {
            'fields': (
                ('tg_upozornenia_ulohy', 'mail_upozornenia_ulohy', 'discord_upozornenia_ulohy'),
            ),
            'description': '📩 Nastavenie, či má systém tomuto zamestnancovi automaticky posielať denný plán úloh a výjazdov. 📅'
        }),
        ('📂 HR AGENDA A DOVOLENKY', {
            'fields': (
                ('datum_nastupu', 'zmluva_subor'), 
                ('narok_na_dovolenku', 'vycerpana_dovolenka')
            ),
            'description': '🏖️ Evidencia pracovného pomeru a čerpania dovoleniek. Systém automaticky počíta zostatok. 📊'
        }),
        ('💰 MZDOVÝ PROFIL (TRUE-MARGIN ENGINE)', {
            'fields': (
                'sposob_odmenovania',
                ('mzda_brutto_hodina', 'mzda_brutto_mesiac'),
                'mzda_zivnostnik_hodina', 
            ),
            'description': '💰 Zvoľte spôsob odmeňovania. Systém automaticky uzamkne nepotrebné polia a na pozadí vypočíta "Univerzálny hodinový náklad" pre presné finančné reporty ziskovosti. 📈'
        }),
        ('⚠️ ZÁVÄZKY VOČI FIRME', {
            'fields': (
                'zavazky', 
                'poznamka_zavazky'
            ),
            'description': '⚖️ Evidencia podlžností (napr. záloha na náradie, poškodenie majetku). Ak je hodnota 0, zamestnanec má čistý štít. ✅'
        }),
        ('🛡️ LEGISLATÍVA A VSTUPY (NEXUS ENTRY)', {
            'fields': (
                'cislo_op', 
                'bozp_platne_do',
                ('bozp_subor', 'lekarska_prehliadka_subor'),
                'get_certifikaty_hub'
            ),
            'description': '🦺 Kritické údaje pre generovanie povolení na vstup do objektov a prehľad odborných certifikácií pracovníka. 🛑'
        }),
        ('⛽ TANKOVANIE', {
            'fields': ('predvolena_cerpacka',),
            'description': '🚙 Predvolená čerpacia stanica sa automaticky predvyplní pri evidencii bločkov z tankovania. 🧾'
        }),
        ('📡 ISAS ERP DIAGNOSTIKA', {
            'fields': ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log'),
            'description': '🔗 Technické informácie o synchronizácii s externým podnikovým systémom. 🛠️'
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(total_servisy=Count('servisnyzaznam')).prefetch_related('certifikaty')

    def get_detective_queryset(self, request, term):
        term_str = str(term).strip()
        filtre = (
            Q(meno__icontains=term_str) |
            Q(priezvisko__icontains=term_str) |
            Q(email__icontains=term_str) |
            Q(telefon__icontains=term_str) |
            Q(user__username__icontains=term_str) |
            Q(certifikaty__nazov__icontains=term_str) |
            Q(certifikaty__cislo_certifikatu__icontains=term_str)
        )
        if term_str.isdigit():
            filtre |= Q(telegram_id=int(term_str)) | Q(discord_id=int(term_str))

        return self.get_queryset(request).select_related('user').filter(filtre).distinct().order_by('priezvisko')[:30]

    def format_detective_result(self, obj):
        celo_meno = f"{obj.meno} {obj.priezvisko}".strip()
        rola = obj.get_pracovna_rola_display() or "Neznáma rola"
        telefon = obj.telefon if obj.telefon else "Bez telefónu"
        return {
            'id': obj.id, 
            'name': celo_meno, 
            'search_value': celo_meno,
            'rola': f"{rola} | 📞 {telefon}",
            'cat': '👨‍💼 ZAMESTNANEC'
        }

    @admin.display(description='👨‍💼 Zamestnanec', ordering='priezvisko')
    def get_rich_identity(self, obj):
        rola_avatars = {'TE': '👨‍🔧', 'DI': '🎧', 'SK': '📦', 'AD': '👩‍💼', 'OB': '🤝'}
        emoji = rola_avatars.get(obj.pracovna_rola, '👨‍💼')

        bg_colors = {
            'IN': ('#e0f2fe, #bae6fd', 'rgba(2, 132, 199, 0.1)'),
            'EX': ('#f0fdf4, #bbf7d0', 'rgba(22, 101, 52, 0.1)'),
            'BR': ('#fef08a, #fde047', 'rgba(202, 138, 4, 0.1)'),
            'KO': ('#fff7ed, #ffedd5', 'rgba(194, 65, 12, 0.1)')
        }
        bg_grad, shadow = bg_colors.get(obj.typ_pomeru, ('#f1f5f9, #e2e8f0', 'rgba(100, 116, 139, 0.1)'))

        rola_raw = obj.get_pracovna_rola_display() or "❓ Neznáma rola"
        pomer_raw = obj.get_typ_pomeru_display() or "❓ Neznámy pomer"
        
        rola_nazov = rola_raw.split(' ', 1)[-1].upper() if ' ' in rola_raw else rola_raw.upper()
        pomer_nazov = pomer_raw.split(' ', 1)[-1].upper() if ' ' in pomer_raw else pomer_raw.upper()

        # 🛡️ ISAS UI: Zloženie odznakov (Rola + Web indikátor)
        badges = [render_isas_badge(f"{rola_nazov} | {pomer_nazov}", '', 'neutral', wrapper=False)]
        if getattr(obj, 'zobrazit_na_webe', False):
            badges.append(render_isas_badge("WEB", "🌐", "info", wrapper=False))
            
        badges_html = render_isas_layout(mark_safe("".join(badges)), layout="row", gap=4, center=False)

        # 📸 ISAS UI: 100% Nepriestrelné natiahnutie profilovky (Ochrana pred ValueError a "None")
        img_url = None
        if getattr(obj, 'profilova_fotka', None) and bool(obj.profilova_fotka):
            try:
                img_url = obj.profilova_fotka.url
            except (ValueError, AttributeError):
                img_url = None

        datum_nar = f"🎂 {obj.datum_narodenia.strftime('%d.%m.%Y')}" if obj.datum_narodenia else None
        return render_isas_avatar(
            icon=emoji,
            title=f"{obj.meno} {obj.priezvisko}",
            subtitle=datum_nar,
            bg_gradient=bg_grad,
            shadow_color=shadow,
            extra_html=badges_html,
            image_url=img_url
        )

    @admin.display(description='📞 Kontakt')
    def get_contact_card(self, obj):
        items = []
        if obj.telefon and obj.telefon != '-':
            items.append(render_isas_badge(obj.telefon, icon='📞', copy_value=obj.telefon, wrapper=False))
        if obj.email and obj.email != '-':
            items.append(render_isas_badge(obj.email, icon='✉️', copy_value=obj.email, wrapper=False))
        if obj.telegram_id:
            items.append(render_isas_badge(f"TG: {obj.telegram_id}", icon='✈️', copy_value=str(obj.telegram_id), wrapper=False))
        if obj.discord_id:
            items.append(render_isas_badge(f"DC: {obj.discord_id}", icon='🎮', copy_value=str(obj.discord_id), wrapper=False))
            
        if not items:
            return render_isas_badge('Nezadaný', '🚫', 'neutral', wrapper=True)
            
        return render_isas_layout(mark_safe("".join(items)), layout="col", gap=4, center=True)

    @admin.display(description='💰 Mzdový profil / IBAN')
    def get_payroll_card(self, obj):
        mena = get_firemna_mena()
        if obj.typ_pomeru == 'EX':
            mzda = obj.mzda_zivnostnik_hodina or Decimal('0.00')
            mzda_text = f"{mzda:.2f} {mena} / hod"
            rezim_text = "ŽIVNOSTNÍK"
        elif obj.sposob_odmenovania == 'HODINA':
            mzda = obj.mzda_brutto_hodina or Decimal('0.00')
            mzda_text = f"{mzda:.2f} {mena} / hod"
            rezim_text = "HODINOVKA"
        elif obj.sposob_odmenovania == 'MESIAC':
            mzda = obj.mzda_brutto_mesiac or Decimal('0.00')
            mzda_text = f"{mzda:.2f} {mena} / mes"
            rezim_text = "PAUŠÁL"
        else:
            mzda_text = "Podľa úkonu"
            rezim_text = "ÚKOLOVÁ MZDA"
        
        if obj.iban:
            iban_short = f"{obj.iban[:4]}...{obj.iban[-4:]}"
            iban_html = render_isas_badge(iban_short, icon='🏦', copy_value=obj.iban, wrapper=False)
        else:
            iban_html = render_isas_text('⚠️ IBAN chýba', muted=True)

        return render_isas_mini_stat_card(
            top_label=rezim_text,
            top_val=mzda_text,
            bot_label='IBAN',
            bot_val=iban_html
        )

    @admin.display(description='📂 Zmluvy a Nástup', ordering='datum_nastupu')
    def get_hr_dokumenty(self, obj):
        nastup = obj.datum_nastupu.strftime('%d.%m.%Y') if obj.datum_nastupu else "🚫 Nezadaný"
        kv_nastup = render_isas_key_value('Nástup', nastup, icon='📅')
        
        zmluva_html = render_isas_badge('Bez zmluvy', '❌', 'neutral', wrapper=False)
        if obj.zmluva_subor and getattr(obj.zmluva_subor, 'name', None):
            try:
                zmluva_html = render_isas_link_btn(obj.zmluva_subor.url, 'Zmluva PDF', '📎', 'info', wrapper=False)
            except Exception:
                zmluva_html = render_isas_badge('Chýba súbor', '⚠️', 'danger', wrapper=False)

        return render_isas_layout(format_html('{}{}', kv_nastup, zmluva_html), layout="col", gap=6, center=True)

    @admin.display(description='🏖️ Čerpanie Dovolenky')
    def get_dovolenka_bar(self, obj):
        if obj.typ_pomeru in ['EX', 'KO'] or not obj.narok_na_dovolenku or obj.narok_na_dovolenku <= 0:
            return render_isas_badge('Bez nároku', '🚫', 'neutral', wrapper=True)

        try:
            narok_val = Decimal(str(obj.narok_na_dovolenku))
            vycerpana_val = Decimal(str(obj.vycerpana_dovolenka or '0.0'))
            zostatok_val_num = Decimal(str(obj.zostatok_dovolenky or '0.0'))
            percento_val = (vycerpana_val / narok_val) * Decimal('100.0') if narok_val > Decimal('0.0') else Decimal('0.0')
            percento_val = min(percento_val, Decimal('100.0'))
        except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
            return render_isas_badge('Chyba výpočtu', '⚠️', 'danger', wrapper=True)

        zostatok_val = render_isas_text(f"{zostatok_val_num:.1f} dni", color="#0f172a", weight=800)
        label_left = format_html("🏖️ Zostatok: {}", zostatok_val)
        label_right = f"{percento_val:.0f}%"
        subtext = f"Celkovo: {obj.narok_na_dovolenku} dní"

        return render_isas_progress_bar(label_left, label_right, vycerpana_val, obj.narok_na_dovolenku, subtext)   

    @admin.display(description='🛠️ Servisný výkon')
    def get_vykonnost_stats(self, obj):
        total_servisy = getattr(obj, 'total_servisy', 0)
        
        try:
            now = timezone.now()
            servisy_mesiac = obj.servisnyzaznam_set.filter(vytvorene__year=now.year, vytvorene__month=now.month).count()
        except Exception:
            servisy_mesiac = 0

        badge_total = render_isas_badge(f"Celkovo: {total_servisy}", '🛠️', 'success', wrapper=False)
        badge_mesiac = render_isas_badge(f"Mesiac: {servisy_mesiac}", '📅', 'info', wrapper=False)

        return render_isas_layout(format_html('{}{}', badge_total, badge_mesiac), layout="col", gap=4, center=True)

    @admin.display(description='⚠️ Finančný status', ordering='zavazky')
    def get_zavazky_big_badge(self, obj):
        if not obj.zavazky or obj.zavazky <= 0:
            return render_isas_badge('Čistý štít', '✅', 'success', wrapper=True)
        
        zavazok_str = f"{obj.zavazky:.2f} {get_firemna_mena()}"
        poznamka = obj.poznamka_zavazky if getattr(obj, 'poznamka_zavazky', None) else ''
        
        badge = render_isas_badge(zavazok_str, '⚠️', 'danger', wrapper=False)
        poznamka_html = render_isas_text(poznamka, muted=True) if poznamka else ""
        
        return render_isas_layout(format_html('{}{}', badge, poznamka_html), layout="col", gap=4, center=True)

    @admin.display(description='🛡️ Legislatíva / Vstupy')
    def get_bozp_status(self, obj):
        dnes = timezone.now().date()
        
        if obj.cislo_op:
            op_status = render_isas_key_value('OP', obj.cislo_op)
        else:
            op_status = render_isas_badge('Chýba OP!', '⚠️', 'danger', wrapper=False)
        
        bozp_datum = to_date(obj.bozp_platne_do)
        if not bozp_datum:
            bozp_status = render_isas_badge('BOZP NEZADANÉ', '🚫', 'danger', wrapper=False)
        elif bozp_datum < dnes:
            bozp_status = render_isas_badge('BOZP EXPIROVALO', '⚠️', 'danger', wrapper=False)
        else:
            bozp_status = render_isas_badge(f'BOZP: {bozp_datum.strftime("%d.%m.%Y")}', '✅', 'success', wrapper=False)

        lekarska_status = render_isas_badge("Lekárska OK", '🏥', 'success', wrapper=False) if obj.lekarska_prehliadka_subor else ""
        
        # Dynamický prehľad certifikátov pripojených k zamestnancovi
        pocet_cert = getattr(obj, 'certifikaty_pocet', None)
        if pocet_cert is None:
            pocet_cert = obj.certifikaty.count()
        cert_status = render_isas_badge(f"📜 Certifikáty: {pocet_cert}", '', 'neutral', wrapper=False) if pocet_cert > 0 else ""

        return render_isas_layout(format_html('{}{}{}{}', op_status, bozp_status, lekarska_status, cert_status), layout="col", gap=4, center=True)

    @admin.display(description='📜 Certifikácie')
    def get_certifikaty_badge(self, obj):
        if not obj or not obj.pk:
            return render_isas_badge("Nezadané", "❓", "neutral", wrapper=True)

        try:
            # 🛡️ DEFENSIVE: Čítanie z prefetch cache namiesto SQL dopytu
            certifikaty = list(obj.certifikaty.all())
            pocet = len(certifikaty)
            if pocet == 0:
                return render_isas_badge("Bez certifikátov", "📜", "neutral", wrapper=True)

            stavy = {c.stav_platnosti for c in certifikaty}
            if 'EXPIROVANY' in stavy or 'NEPLATNY' in stavy:
                return render_isas_badge(f"Expirované ({pocet})", "🚨", "danger", wrapper=True)
            if 'BLIZI_SA' in stavy:
                return render_isas_badge(f"Expiruje čoskoro ({pocet})", "⚠️", "warning", wrapper=True)

            return render_isas_badge(f"Platné ({pocet})", "✅", "success", wrapper=True)
        except Exception:
            return render_isas_badge("Chyba auditu", "⚠️", "danger", wrapper=True)

    @admin.display(description='📜 Odborné certifikácie a oprávnenia')
    def get_certifikaty_hub(self, obj):
        if not obj or not obj.pk:
            return render_isas_text("💡 Certifikácie bude možné priradiť po prvom uložení zamestnanca.", muted=True)

        try:
            certifikaty = list(obj.certifikaty.all().order_by('datum_platnosti'))
            pocet = len(certifikaty)
            riadky = []

            for c in certifikaty:
                stav = getattr(c, 'stav_platnosti', 'NEPLATNY')
                datum_str = c.datum_platnosti.strftime('%d.%m.%Y') if c.datum_platnosti else "Bez dátumu"

                if stav == 'DOZIVOTNY':
                    badge_stav = render_isas_badge("Doživotný", icon="♾️", type_color="success", wrapper=False)
                elif stav == 'EXPIROVANY':
                    badge_stav = render_isas_badge(f"Expiroval ({datum_str})", icon="🚨", type_color="danger", wrapper=False)
                elif stav == 'BLIZI_SA':
                    badge_stav = render_isas_badge(f"Končí {datum_str}", icon="⚠️", type_color="warning", wrapper=False)
                elif stav == 'NEPLATNY':
                    badge_stav = render_isas_badge("Neplatný / Chýba dátum", icon="🛑", type_color="danger", wrapper=False)
                else:
                    badge_stav = render_isas_badge(f"Platný do {datum_str}", icon="✅", type_color="success", wrapper=False)

                # 🛡️ DEFENSIVE: Ochrana pred zlyhaním storage subsystému pri neexistujúcom súbore
                subor_link = ""
                if c.subor and getattr(c.subor, 'name', None):
                    try:
                        subor_link = render_isas_link_btn(c.subor.url, "PDF", icon="📎", type_color="neutral", wrapper=False)
                    except Exception:
                        subor_link = render_isas_badge("Chýba súbor", "⚠️", "danger", wrapper=False)

                try:
                    edit_url = reverse(f'admin:{c._meta.app_label}_{c._meta.model_name}_change', args=[c.pk])
                    nazov_link = render_isas_link_btn(edit_url, escape(c.nazov), icon="✏️", type_color="info", wrapper=False)
                except Exception:
                    nazov_link = render_isas_text(escape(c.nazov), weight=700)

                riadky.append(render_isas_layout(
                    format_html('{}{}{}', nazov_link, badge_stav, subor_link),
                    layout="row", gap=6, center=False
                ))

            app_label = obj._meta.app_label
            try:
                pridat_url = f"{reverse(f'admin:{app_label}_certifikat_add')}?zamestnanec={obj.pk}"
                tlacidlo_pridat = render_isas_link_btn(pridat_url, "Pridať nový certifikát", icon="➕", type_color="dashed", wrapper=False)
            except Exception:
                tlacidlo_pridat = ""

            try:
                filter_url = f"{reverse(f'admin:{app_label}_certifikat_changelist')}?zamestnanec__id__exact={obj.pk}"
                tlacidlo_register = render_isas_link_btn(filter_url, f"Zobraziť všetky v registri ({pocet})", icon="🔍", type_color="neutral", wrapper=False)
            except Exception:
                tlacidlo_register = ""

            akcie = render_isas_layout(format_html('{}{}', tlacidlo_pridat, tlacidlo_register), layout="row", gap=8, center=False)

            if not riadky:
                obsah = render_isas_text("Tento pracovník zatiaľ nemá zaevidované žiadne odborné certifikáty ani osvedčenia.", muted=True)
                return render_isas_layout(format_html('{}{}', obsah, akcie), layout="col", gap=8, center=False)

            zoznam = render_isas_layout(mark_safe("".join(riadky)), layout="col", gap=6, center=False)
            return render_isas_layout(format_html('{}{}', zoznam, akcie), layout="col", gap=10, center=False)

        except Exception as e:
            logger.error("Chyba vykreslenia get_certifikaty_hub pre Zamestnanec ID %s: %s", getattr(obj, 'pk', 'N/A'), e)
            return render_isas_text("⚠️ Informácie o certifikátoch sa nepodarilo načítať.", color="#dc2626")

@admin.register(SukromnaOsoba)
class SukromnaOsobaAdmin(ISASSecurityAdminMixin, ERPExportAdminMixin, ISASPremiumAdmin):
    isas_title = "👤 Súkromné osoby"
    isas_preloader_text = "⏳ Vstupujete do zoznamu súkromných osôb..."
    isas_magia_kwargs = {
        "live_search_endpoint": "detective-search/",
        "live_search_ikona": "👤",
    }

    list_display = (
        "erp_status_ikona",
        "get_rich_identity", 
        "get_contact_card", 
        "get_erp_fakturacia_card",
        "get_telegram_badge"
    )
    list_display_links = ("get_rich_identity",)
    
    search_fields = ("meno_a_priezvisko", "telefon", "email", "telegram_id", "discord_id")
    list_filter = ("erp_export_status", TelegramFilter, DiscordFilter)
    ordering = ("meno_a_priezvisko",)
    readonly_fields = ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log',)
    autocomplete_fields = ['predvolena_cerpacka']
    
    fieldsets = (
        ('👤 OSOBNÁ IDENTIFIKÁCIA', {
            'fields': ('meno_a_priezvisko',),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">🪪 Základné identifikačné údaje osoby pre systém a zobrazenie v aplikácii.</div>'
        }),
        ('📞 KOMUNIKÁCIA A PREPOJENIE', {
            'fields': (
                ('email', 'telefon'), 
                ('telegram_id', 'discord_id'),
                'datum_narodenia',
            ),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">🌐 Kontaktné informácie a prepojenie na notifikačné kanály (vrátane Telegram bota).</div>'
        }),
        ('📍 ÚDAJE PRE ERP (FAKTURÁCIA A DIÉTY)', {
            'fields': (
                ('ulica_a_cislo', 'mesto'),
                ('psc', 'stat'),
                'iban'
            ),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">🏦 Tieto polia sú dôležité pre správny export do účtovníctva (na preplácanie výdavkov a fakturáciu).</div>'
        }),
        ('⛽ TANKOVANIE A PREFERENCIE', {
            'fields': ('predvolena_cerpacka',),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">🚗 Nastavenia pre automatizované spracovanie bločkov a výdavkov za PHM cez Telegram bota.</div>'
        }),
        ('📡 ISAS ERP DIAGNOSTIKA', {
            'fields': (
                ('erp_export_status', 'erp_id'), 
                ('erp_last_exported_at', 'erp_error_log')
            ),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">⚙️ Technické informácie o stave synchronizácie so systémom ERP. ⚠️ Zasahujte len v prípade chýb.</div>'
        }),
    )

    def get_detective_queryset(self, request, term):
        filtre = (
            Q(meno_a_priezvisko__icontains=term) |
            Q(telefon__icontains=term) |
            Q(email__icontains=term) |
            Q(mesto__icontains=term) |
            Q(iban__icontains=term)
        )
        if term.strip().isdigit():
            filtre |= Q(telegram_id=int(term.strip())) | Q(discord_id=int(term.strip()))
        return self.get_queryset(request).filter(filtre)[:30]

    def format_detective_result(self, obj):
        kontakt = obj.telefon if obj.telefon else (obj.email if getattr(obj, 'email', None) else "Bez kontaktu")
        return {
            'id': obj.id, 
            'name': obj.meno_a_priezvisko, 
            'search_value': obj.meno_a_priezvisko,
            'rola': kontakt,
            'cat': '👤 SÚKROMNÁ OSOBA'
        }

    @admin.display(description='👤 Súkromná osoba', ordering='meno_a_priezvisko')
    def get_rich_identity(self, obj):
        avatars = ["👤", "🧑", "👩", "👨", "🧔", "👱‍♂️", "👩‍🦰", "🥷"]
        avatar_index = obj.id % len(avatars) if obj.id else 0
        emoji = avatars[avatar_index]

        meno = obj.meno_a_priezvisko if obj.meno_a_priezvisko else "❓ Neznáma osoba"
        badge = render_isas_badge('SÚKROMNÁ OSOBA', '👥', 'neutral', wrapper=False)
        datum_nar = f"🎂 {obj.datum_narodenia.strftime('%d.%m.%Y')}" if obj.datum_narodenia else None

        return render_isas_avatar(
            icon=emoji,
            title=meno,
            subtitle=datum_nar,
            bg_gradient='#f0f9ff, #e0f2fe',
            shadow_color='rgba(2, 132, 199, 0.1)',
            extra_html=badge
        )

    @admin.display(description='📞 Kontakt')
    def get_contact_card(self, obj):
        items = []
        if obj.telefon and obj.telefon != '-':
            items.append(render_isas_badge(obj.telefon, icon='📞', copy_value=obj.telefon, wrapper=False))
        if getattr(obj, 'email', None) and obj.email != '-':
            items.append(render_isas_badge(obj.email, icon='✉️', copy_value=obj.email, wrapper=False))
            
        if not items:
            return render_isas_badge('Nezadaný', '🚫', 'neutral', wrapper=True)
            
        return render_isas_layout(mark_safe("".join(items)), layout="col", gap=4, center=True)

    @admin.display(description='🏦 Fakturácia / IBAN')
    def get_erp_fakturacia_card(self, obj):
        mesto = obj.mesto if obj.mesto else "Neznáme mesto"
        
        if obj.iban:
            iban_short = f"{obj.iban[:4]}...{obj.iban[-4:]}"
            iban_html = render_isas_badge(iban_short, icon='🏦', copy_value=obj.iban, wrapper=False)
        else:
            iban_html = render_isas_text('⚠️ IBAN chýba', muted=True)

        cerpacka_val = str(obj.predvolena_cerpacka) if obj.predvolena_cerpacka else "—"
        return render_isas_mini_stat_card(
            top_label='📍 LOKALITA',
            top_val=mesto,
            bot_label='IBAN',
            bot_val=iban_html,
            third_label='⛽ ČERPAČKA',
            third_val=cerpacka_val,
            third_val_color="#0284c7"
        )

    @admin.display(description='✈️ Telegram / 🎮 Discord', ordering='telegram_id')
    def get_telegram_badge(self, obj):
        badges = []
        if obj.telegram_id and str(obj.telegram_id).strip() != '-':
            badges.append(render_isas_badge(f'TG: {obj.telegram_id}', '✈️', 'info', wrapper=False))
        if obj.discord_id and str(obj.discord_id).strip() != '-':
            badges.append(render_isas_badge(f'DC: {obj.discord_id}', '🎮', 'purple', wrapper=False))
        if not badges:
            return render_isas_badge('Neprepojené', '🚫', 'neutral', wrapper=True)
        return render_isas_layout(mark_safe("".join(badges)), layout="col", gap=4, center=True)

class AIPripravenostFilter(admin.SimpleListFilter):
    title = '🤖 AI Pripravenosť (Párovanie)'
    parameter_name = 'ai_stav'

    def lookups(self, request, model_admin):
        return (
            ('kompletne', '🧠 Má kategórie aj značky'),
            ('ciastocne', '🟡 Má aspoň jedno (Kat/Značka)'),
            ('prazdne', '❌ Úplne bez AI dát'),
        )

    def queryset(self, request, queryset):
        # FÁZA 3: Použitie .distinct() je kritické pri filtrovaní cez ManyToMany polia!
        if self.value() == 'kompletne':
            return queryset.filter(ai_zameranie__isnull=False, zastupene_znacky__isnull=False).distinct()
        if self.value() == 'ciastocne':
            return queryset.filter(Q(ai_zameranie__isnull=False) | Q(zastupene_znacky__isnull=False)).distinct()
        if self.value() == 'prazdne':
            return queryset.filter(ai_zameranie__isnull=True, zastupene_znacky__isnull=True).distinct()
        return queryset

@admin.register(Dodavatel)
class DodavatelAdmin(KontaktnaOsobaAdminMixin, ISASObchodnyPartnerAdminMixin, ISASSecurityAdminMixin, ERPExportAdminMixin, ISASPremiumAdmin):
    isas_title = "🏢 Dodávatelia"
    isas_preloader_text = "⏳ Vstupujete do zoznamu dodávateľov..."
    isas_magia_kwargs = {
        "live_search_endpoint": "detective-search/",
        "live_search_ikona": "🏢",
    }

    list_display = (
        'erp_status_ikona',
        'get_dodavatel_main', 
        'get_ids_pills', 
        'get_hq_address', 
        'get_contact_card', 
        'get_web_link'
    )
    list_display_links = ('get_dodavatel_main',)
    search_fields = ['Nazov_spolocnosti', 'ico', 'dic', 'email']
    readonly_fields = ('get_objednavky_dashboard', 'erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log',)
    list_filter = (
        'erp_export_status',
        AIPripravenostFilter, 
        ISASWebstrankaFilter, 
        ISASKontaktnaOsobaFilter, 
    )
    filter_horizontal = ('ai_zameranie',)
    autocomplete_fields = ['zastupene_znacky']

    fieldsets = (
        ('🏢 ZÁKLADNÁ IDENTIFIKÁCIA DODÁVATEĽA', {
            'fields': (
                'Nazov_spolocnosti', 
                ('ico', 'dic', 'ic_dph'),
            ),
            'description': 'Základné fakturačné údaje dodávateľa. 🪪 IČO slúži ako unikátny identifikátor v systéme. ⚙️'
        }),
        ('🏦 BANKOVÉ SPOJENIE A PLATBY', {
            'fields': (
                ('iban', 'predvolena_splatnost_dni', 'erp_predkontacia'),
            ),
            'description': '💸 Údaje potrebné pre úhrady faktúr a generovanie bankových príkazov v ERP. 📊'
        }),
        ('👤 HLAVNÝ KONTAKT A WEB', {
            'fields': (
                ('kontaktna_osoba', 'oslovenie_rod'), 
                ('telefon', 'email'),
                ('datum_narodenia', 'domena')
            ),
            'description': '📞 Kontaktné údaje na obchodného zástupcu alebo podporu. 🌐 Doména slúži pre AI a rýchly preklik na ich e-shop.'
        }),
        ('📍 SÍDLO SPOLOČNOSTI', {
            'fields': (
                ('ulica_a_cislo', 'mesto'),
                ('psc', 'stat')
            ),
            'description': '🗺️ Oficiálna adresa dodávateľa. Prepojená priamo s Google Maps navigáciou. 🚙'
        }),
        ('🤖 AI PÁROVANIE A ZNAČKY (ISAS ENGINE)', {
            'fields': ('ai_zameranie', 'zastupene_znacky'),
            'description': '🧠 Tieto polia slúžia umelej inteligencii na presnejšie automatické párovanie prijatých faktúr a položiek na sklad. ⚡'
        }),
        ('📝 OSTATNÉ INFORMÁCIE', {
            'fields': ('ostatne',),
            'description': '📓 Akékoľvek interné poznámky a špecifiká k dodávateľovi, napr. zľavové kódy alebo B2B portál. 🤫'
        }),
        ('📦 DOKUMENTY A HISTÓRIA NÁKUPU', {
            'fields': ('get_objednavky_dashboard',),
            'description': '⚡ Rýchly prístup k naposledy vygenerovaným objednávkam a faktúram od tohto dodávateľa. 📈'
        }),
        ('📡 ISAS ERP DIAGNOSTIKA', {
            'fields': ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log'),
            'description': '🔗 Technické informácie o synchronizácii s externým účtovným systémom (Pohoda / Kros). Zasahujte len v prípade chyby! 🛠️'
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            objednavky_count=Count('odoslane_objednavky', distinct=True),
            faktury_count=Count('faktury', distinct=True)
        )

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(Nazov_spolocnosti__icontains=term) |
            Q(ico__icontains=term) |
            Q(dic__icontains=term) |
            Q(ic_dph__icontains=term) |
            Q(iban__icontains=term) |
            Q(domena__icontains=term) |
            Q(email__icontains=term) |
            Q(telefon__icontains=term) |
            Q(kontaktna_osoba__icontains=term)
        )[:30]

    def format_detective_result(self, obj):
        nazov = obj.Nazov_spolocnosti or "Neznámy dodávateľ"
        ico_text = f"IČO: {obj.ico}" if obj.ico else "Bez IČO"
        lokalita = f"📍 {obj.mesto}" if obj.mesto else ""
        podtitul = f"{ico_text} {lokalita}".strip()
        
        return {
            'id': obj.id, 
            'name': nazov, 
            'search_value': nazov,
            'rola': podtitul,
            'cat': '🏢 DODÁVATEĽ'
        }

    @admin.display(description='🏢 Dodávateľ', ordering='Nazov_spolocnosti')
    def get_dodavatel_main(self, obj):
        image_url = None
        if obj.domena:
            clean_domain = str(obj.domena).lower().strip()
            for prefix in ['https://', 'http://', 'www.']:
                if clean_domain.startswith(prefix):
                    clean_domain = clean_domain[len(prefix):]
            
            clean_domain = clean_domain.split('/')[0].split('?')[0].split('#')[0]
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
                
            if '.' in clean_domain and len(clean_domain) > 3:
                image_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"

        nazov = obj.Nazov_spolocnosti if obj.Nazov_spolocnosti else "❓ Neznámy dodávateľ"

        predkontacia_badge = render_isas_badge(f"ERP: {obj.erp_predkontacia}", icon="🧾", type_color="neutral", wrapper=False) if obj.erp_predkontacia else ""
        return render_isas_avatar(
            icon="🏢",
            title=nazov,
            subtitle=None,
            bg_gradient="#f8fafc, #f1f5f9",
            shadow_color="rgba(0,0,0,0.05)",
            extra_html=predkontacia_badge,
            image_url=image_url
        )

    @admin.display(description='📁 Archív dokladov a histórie')
    def get_objednavky_dashboard(self, obj):
        if not obj.pk:
            return render_isas_text("🔒 Dostupné po uložení dodávateľa.", muted=True)
            
        pocet_objednavok = getattr(obj, 'objednavky_count', 0)
        url_vsetky_objednavky = f"/admin/Evidencia/odoslanaobjednavka/?dodavatel__id__exact={obj.id}"
        btn_objednavky = render_isas_link_btn(
            url=url_vsetky_objednavky, 
            text="Objednávky", 
            icon="📤", 
            type_color="neutral", 
            wrapper=False, 
            full_width=True, 
            counter_val=pocet_objednavok
        )

        pocet_faktur = getattr(obj, 'faktury_count', 0)
        url_vsetky_faktury = f"/admin/Evidencia/prichodziafaktura/?dodavatel__id__exact={obj.id}"
        btn_faktury = render_isas_link_btn(
            url=url_vsetky_faktury, 
            text="Faktúry", 
            icon="🧾", 
            type_color="neutral", 
            wrapper=False, 
            full_width=True, 
            counter_val=pocet_faktur
        )

        return render_isas_layout(format_html('{}{}', btn_objednavky, btn_faktury), layout="col", gap=8, center=True)

class ISASBaseStackedInline(ISASPremiumInlineMixin, admin.StackedInline):
    """Základná trieda pre všetky parametre. Definuje ISAS Vivid dizajn a UX."""
    can_delete = False
    max_num = 1
    min_num = 1  
    extra = 1   
    classes = ['isas-glass-inline']
    empty_value_display = '➖'

vygenerovane_zariadenia_inlines = {}

# 🚀 ISAS ARCHITECT: 100% dynamické generovanie inlines a choice placeholderov podľa aktívneho modelu odvetvia
for kat_kluc, model_parametru in ZARIADENIA_MAP.items():
    # 🛡️ DEFENSIVE: Preskočíme prázdne alebo neplatné definície modelov
    if not model_parametru or not hasattr(model_parametru, '_meta'):
        continue

    inline_name = f"{model_parametru.__name__}Inline"
    # Automaticky nájdeme všetky polia s definovanými choices na danom parametri bez hardkódu
    dynamicke_choice_polia = [
        f.name for f in model_parametru._meta.fields if getattr(f, 'choices', None)
    ]
    attrs = {
        'model': model_parametru,
        'isas_clean_choice_fields': dict.fromkeys(dynamicke_choice_polia, '🔽 Vyberte z možností')
    }
    InlineClass = type(inline_name, (ISASBaseStackedInline,), attrs)
    vygenerovane_zariadenia_inlines[kat_kluc] = InlineClass

class ServisovaneZariadenieInline(admin.StackedInline):
    model = ServisovaneZariadenie
    form = ServisovaneZariadenieForm
    extra = 0
    can_delete = True
    show_change_link = True  
    empty_value_display = '➖' 

    readonly_fields = (
        'zobraz_zive_data', 'zobraz_ai_sepkar_tlacidlo',
        'get_kategoria_zariadenia', 'get_vyrobca_tlacidlo', 'get_model_zariadenia', 
        'get_vyrobne_cislo', 'get_zaruka_badge', 'nazov', 'umiestnenie', 'rok_vyroby', 
        'get_v_prenajme_od_isas', 'poznamka', 'zobraz_qr_kod', 
    )

    fieldsets = (
        ('📡 TELEMETRIA ZARIADENIA', {
            'fields': ('zobraz_zive_data',),
            'classes': ('telemetria-box',) 
        }),
        ('⚙️ ZÁKLADNÉ ÚDAJE', {
            'fields': (
                ('get_kategoria_zariadenia', 'get_vyrobca_tlacidlo', 'get_model_zariadenia', 'get_vyrobne_cislo', 'get_v_prenajme_od_isas', 'sprava_dielov'), 
                ('nazov', 'umiestnenie', 'rok_vyroby'),
                ('get_zaruka_badge', 'zobraz_ai_sepkar_tlacidlo', 'zobraz_qr_kod'),
                'poznamka',
            ),
        }),
    ) 

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="📡 Živé dáta") 
    def zobraz_zive_data(self, obj):
        if not obj:
            return render_isas_text("—", muted=True)
        if obj.aktualne_motohodiny is None:
            hodiny = "Čaká na zápis"
            status_color = "#64748b" 
        else:
            hodiny = "{:,}".format(obj.aktualne_motohodiny).replace(',', ' ')
            status_color = "#10b981" 

        datum = obj.posledna_aktualizacia_stavu.strftime("%d.%m.%Y o %H:%M") if obj.posledna_aktualizacia_stavu else "Neznámy čas"
        nazov = obj.nazov or "Nezadané"

        item1 = render_isas_icon_stat_card("⏱️", obj.meter_label, hodiny, status_color, value_color=status_color, suffix=f" {obj.meter_unit}" if obj.aktualne_motohodiny is not None else "")
        item2 = render_isas_icon_stat_card("🔄", "Posledná aktualizácia", datum, "#3b82f6")
        item3 = render_isas_icon_stat_card("🏷️", "ISAS Identifikátor", nazov, "#f59e0b")

        return render_isas_layout(format_html('{}{}{}', item1, item2, item3), layout="row", gap=16)

    @admin.display(description="🤖 AI Diagnostika")
    def zobraz_ai_sepkar_tlacidlo(self, obj):
        if not obj or not obj.pk:
            return render_isas_text("Najprv uložte zariadenie", muted=True)

        return render_isas_link_btn(
            url=None,
            text="Spustiť AI Analýzu",
            icon="🤖",
            type_color="info",
            is_button=True,
            data_attrs={"zariadenie-id": str(obj.pk)}
        )

    @admin.display(description="🔳 Generovanie QR kódu")
    def zobraz_qr_kod(self, obj):
        if obj and obj.qr_kod:
            try:
                url = obj.qr_kod.url
                img = render_isas_image(url, alt="QR kód")
                btn_view = render_isas_link_btn(url, "Zobraziť", icon="👁️", type_color="neutral")
                
                # 🛡️ ISAS CORE: Využitie deklaratívneho file_action_modal z Master Enginu
                js_print = f"event.preventDefault(); if(window.showFileModal) window.showFileModal('{escapejs(url)}'); else window.open('{escapejs(url)}', '_blank');"
                btn_print = render_isas_link_btn("#", "Akcia", icon="📄", type_color="dark", onclick=js_print)
                
                return render_isas_layout(format_html('{}{}{}', img, btn_view, btn_print), layout="row", gap=12)
            except (ValueError, AttributeError):
                pass 
                
        return render_isas_text("⚠️ QR kód zatiaľ nie je vygenerovaný alebo je poškodený.", muted=True)

    @admin.display(description='📂 Kategória')
    def get_kategoria_zariadenia(self, obj):
        if not obj.kategoria: return render_isas_text("❌", muted=True)
        badge = render_isas_badge(obj.get_kategoria_display(), icon="📂", type_color="neutral", wrapper=False)
        return format_html('<span class="isas-category-data-node" data-category-key="{}" data-category-label="{}">{}</span>', obj.kategoria, obj.get_kategoria_display(), badge)

    @admin.display(description='🏭 Výrobca')
    def get_vyrobca_tlacidlo(self, obj):
        if not obj.vyrobca: return render_isas_text("❌", muted=True)
        
        vyrobca_id = getattr(obj.vyrobca, 'pk', '')
        url = f"/admin/Evidencia/databazavyrobcov/{vyrobca_id}/change/" if vyrobca_id else "#"
        
        return render_isas_link_btn(url, str(obj.vyrobca), icon="🏭", type_color="info", suffix_html="↗")

    @admin.display(description='⚙️ Model zariadenia')
    def get_model_zariadenia(self, obj):
        if not obj.model_zariadenia: return render_isas_text("❌", muted=True)
        return render_isas_text(obj.model_zariadenia, size=14, color="#0f172a", weight=900, icon="⚙️")

    @admin.display(description='🔢 Výrobné číslo (S/N)')
    def get_vyrobne_cislo(self, obj):
        if not obj.vyrobne_cislo: return render_isas_text("❌", muted=True)
        return render_isas_badge(obj.vyrobne_cislo, icon="🔢", copy_value=obj.vyrobne_cislo, wrapper=False)

    @admin.display(description='🏢 Vlastníctvo zariadenia')
    def get_v_prenajme_od_isas(self, obj):
        if not obj:
            return render_isas_text("—", muted=True)
        if obj.v_prenajme_od_isas:
            return render_isas_badge("MAJETOK ISAS (PRENÁJOM)", icon="🤝", type_color="warning", wrapper=False)
        return render_isas_badge("MAJETOK ZÁKAZNÍKA", icon="👤", type_color="neutral", wrapper=False)

    @admin.display(description='🛡️ Záručný status')
    def get_zaruka_badge(self, obj):
        if not obj:
            return render_isas_badge("BEZ ÚDAJOV", icon="➖", type_color="neutral", wrapper=False)
        dnes = timezone.now().date()
        predlzena = to_date(obj.predlzena_zaruka_do)
        zaruka = to_date(obj.zaruka_do)
        if predlzena and predlzena >= dnes:
            datum_str = predlzena.strftime("%d.%m.%Y")
            return render_isas_badge(f"PREDĹŽENÁ ZÁRUKA (do {datum_str})", icon="💎", type_color="purple", wrapper=False)
        if zaruka and zaruka >= dnes:
            datum_str = zaruka.strftime("%d.%m.%Y")
            return render_isas_badge(f"V ZÁRUKE (do {datum_str})", icon="🛡️", type_color="success", wrapper=False)
        if zaruka or predlzena:
            return render_isas_badge("PO ZÁRUKE", icon="⚪", type_color="neutral", wrapper=False)
        return render_isas_badge("BEZ ÚDAJOV", icon="➖", type_color="neutral", wrapper=False)

    class Media:
        js = (
            "js/zakaznik_zariadenia_master.js",
        )

class PrenajomFilter(admin.SimpleListFilter):
    title = '🤝 Stav prenájmu'
    parameter_name = 'prenajom'

    def lookups(self, request, model_admin):
        return (
            ('ano', '✅ V prenájme od ISAS'),
            ('nie', '🏢 Vlastní zákazník'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'ano':
            return queryset.filter(v_prenajme_od_isas=True)
        if self.value() == 'nie':
            # BEZPEČNOSŤ: Ošetrenie Null hodnôt cez Q objekt
            return queryset.filter(Q(v_prenajme_od_isas=False) | Q(v_prenajme_od_isas__isnull=True))
        return queryset

class ZarukaFilter(admin.SimpleListFilter):
    title = '🛡️ Stav záruky'
    parameter_name = 'zaruka_stav'

    def lookups(self, request, model_admin):
        return (
            ('v_zaruke', '🟢 V záruke (Štandardná / Predĺžená)'),
            ('po_zaruke', '⚪ Po záruke'),
            ('bez_udajov', '➖ Bez záručných údajov'),
        )

    def queryset(self, request, queryset):
        dnes = timezone.now().date()
        if self.value() == 'v_zaruke':
            return queryset.filter(Q(zaruka_do__gte=dnes) | Q(predlzena_zaruka_do__gte=dnes))
        if self.value() == 'po_zaruke':
            return queryset.filter(
                (Q(zaruka_do__lt=dnes) | Q(zaruka_do__isnull=True)) &
                (Q(predlzena_zaruka_do__lt=dnes) | Q(predlzena_zaruka_do__isnull=True)) &
                (Q(zaruka_do__isnull=False) | Q(predlzena_zaruka_do__isnull=False))
            )
        if self.value() == 'bez_udajov':
            return queryset.filter(zaruka_do__isnull=True, predlzena_zaruka_do__isnull=True)
        return queryset

@admin.register(ServisovaneZariadenie)
class ServisovaneZariadenieAdmin(ISASHiddenAdminMixin, ISASPremiumAdmin):
    isas_title = "⚙️ Servisované zariadenia"
    isas_preloader_text = "⏳ Vstupujete do zoznamu zariadení..."
    isas_clean_choice_fields = {'kategoria': '📂 Vyberte kategóriu', 'iot_protokol': '🔌 Vyberte komunikačný protokol'}
    isas_autocomplete_fk_fields = {'vyrobca': '🏭 Vyberte výrobcu'}
    
    isas_ai_sepkar_field = 'id'
    isas_file_action_modal = True
    isas_korekcia_mh_modal = True
    
    isas_magia_kwargs = {
        "live_search_endpoint": "detective-search/",
        "live_search_ikona": "⚙️",
    }

    list_display = (
        'get_zariadenie_card', 
        'get_kategoria_badge',
        'get_zakaznik_card', 
        'get_telemetria_card', 
        'get_zaruka_card',
        'get_iot_status', 
        'rychle_akcie_zariadenia'
    )
    list_display_links = ('get_zariadenie_card',)
    search_fields = [
        'vyrobne_cislo', 
        'nazov', 
        'model_zariadenia', 
        'zakaznik__Nazov_spolocnosti', 
        'vyrobca__nazov_vyrobcu',
        'umiestnenie'
    ]
    list_filter = (ZarukaFilter, PrenajomFilter, 'kategoria', 'vyrobca')
    readonly_fields = (
        'zobraz_zakaznika', 'posledna_aktualizacia_stavu', 
        'rychle_akcie_zariadenia', 'aktualne_motohodiny', 'motohodiny_v_zatazi', 'iot_api_kluc',  'bezpecnostny_kluc', 'slug'
    )
    autocomplete_fields = ['vyrobca', 'zakaznik']

    fieldsets = (
        ('⚡ RÝCHLE AKCIE', {
            'description': '🚀 Rýchle skratky pre okamžitú interakciu a operatívnu správu tohto zariadenia.',
            'fields': ('rychle_akcie_zariadenia',),
            'classes': ('vivid-box-flash',) 
        }),
        ('🤖 ZÁKLADNÁ IDENTIFIKÁCIA (ISAS Core AI)', {
            'description': '🧠 Kľúčové údaje pre presnú identifikáciu zariadenia a správne fungovanie AI párovania dielov a položiek.',
            'fields': (
                ('vyrobne_cislo', 'nazov'),
                'model_zariadenia',
                'vyrobca',
                'kategoria',
                ('zakaznik', 'zobraz_zakaznika')
            )
        }),
        ('🔴 ŽIVÉ DÁTA ZARIADENIA (Aktualizované automaticky)', {
            'description': '📡 Digitálne dvojča zariadenia. Hodnoty sú synchronizované zo servisných výkazov alebo priamo cez telemetriu (IoT). 🛠️ <b>Servisný režim</b> stlmí všetky alarmy počas údržby zariadenia.',
            'fields': (
                ('aktualne_motohodiny', 'motohodiny_v_zatazi'),
                ('posledna_aktualizacia_stavu', 'v_servisnom_rezime')
            ),
            'classes': ('vivid-box-flash',),
        }),
        ('🔌 IOT A KONEKTIVITA (Sieťové pripojenie)', {
            'description': '🌐 Parametre pre priame napojenie na riadiacu jednotku alebo rozhranie zariadenia v sieti zákazníka.',
            'fields': (
                ('iot_je_aktivny', 'iot_limit_zapisov_za_hodinu'),
                'iot_api_kluc',
                ('iot_mac_adresa', 'iot_ip_adresa'),
                'iot_protokol',
            ),
        }),
        ('📐 TECHNICKÉ A LOGISTICKÉ PARAMETRE', {
            'description': '🏗️ Fyzické a logistické údaje nevyhnutné pre plánovanie servisných výjazdov, sledovanie záruky a bezpečnú manipuláciu.',
            'fields': (
                ('rok_vyroby', 'datum_instalacie'),
                ('zaruka_do', 'predlzena_zaruka_do'),
                ('hmotnost_kg', 'umiestnenie'),
            )
        }),
        ('💼 ISAS CORE SPRÁVA A PRÍSLUŠENSTVO', {
            'description': '📋 Interná správa majetku, poznámky technikov a vygenerované QR štítky pre rýchly prístup v teréne.',
            'fields': ('v_prenajme_od_isas', 'poznamka', ('qr_kod', 'bezpecnostny_kluc'))
        }),
        ('⚙️ RENTAL FLOTILA A PRENÁJOM (Web Builder)', {
            'description': '🌐 Nastavenie prenájmu a zobrazenia zariadenia vo verejnom katalógu na webe. Tieto údaje sa synchronizujú s modulom Web Builder.',
            'fields': (
                'je_urcene_na_prenajom',
                ('zobrazit_na_webe_prenajom', 'cena_prenajmu_na_den'),
                ('obrazok_na_webe', 'slug')
            )
        }),
    )

    inlines = list(vygenerovane_zariadenia_inlines.values())

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('api-zakaznici/', self.admin_site.admin_view(self.api_zakaznici), name=f'{self.opts.app_label}_{self.opts.model_name}_api_zakaznici'),
        ]
        return custom_urls + urls

    def api_zakaznici(self, request):
        if not request.user.has_perm('Evidencia.change_servisovanezariadenie'):
            return JsonResponse({"results": []}, status=403)
        term = request.GET.get('q', '').strip()
        vylucit_id = request.GET.get('exclude_id')
        qs = Zakaznik.objects.all()
        if vylucit_id:
            try:
                qs = qs.exclude(pk=int(vylucit_id))
            except ValueError:
                pass
        if term:
            qs = qs.filter(
                Q(Nazov_spolocnosti__icontains=term) |
                Q(ico__icontains=term) |
                Q(mesto__icontains=term)
            )
        results = []
        for z in qs.order_by('-je_interny_profil', 'Nazov_spolocnosti')[:30]:
            prefix = "🏢 [Centrála / Sklad] " if z.je_interny_profil else "👤 "
            mesto_info = f" ({z.mesto})" if z.mesto else ""
            results.append({
                "id": z.id,
                "text": f"{prefix}{z.Nazov_spolocnosti}{mesto_info}"
            })
        return JsonResponse({"results": results})

    def get_readonly_fields(self, request, obj=None):
        rf = super().get_readonly_fields(request, obj)
        # 🛡️ Ak stroj patrí našej firme, pole "v_prenajme_od_isas" je zamknuté na False (stroj je na centrále)
        if obj and obj.zakaznik and getattr(obj.zakaznik, 'je_interny_profil', False):
            return tuple(rf) + ('v_prenajme_od_isas',)
        return rf

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('zakaznik', 'vyrobca')

    def get_search_results(self, request, queryset, search_term):
        queryset, may_have_duplicates = super().get_search_results(request, queryset, search_term)
        zakaznik_id = request.GET.get('zakaznik_id') or request.GET.get('zakaznik')
        if zakaznik_id:
            try:
                zak_id_int = int(zakaznik_id)
                queryset = queryset.filter(zakaznik__id=zak_id_int)
            except ValueError:
                pass
        return queryset, may_have_duplicates

    def get_detective_queryset(self, request, term):
        term = term.strip()
        return self.get_queryset(request).select_related(
            'zakaznik', 'vyrobca'
        ).filter(
            Q(model_zariadenia__icontains=term) |
            Q(nazov__icontains=term) |
            Q(vyrobne_cislo__icontains=term) |
            Q(iot_mac_adresa__icontains=term) |
            Q(vyrobca__nazov_vyrobcu__icontains=term) |
            Q(zakaznik__Nazov_spolocnosti__icontains=term) |
            Q(umiestnenie__icontains=term)
        ).distinct()[:30]

    def format_detective_result(self, obj):
        stroj_nazov = obj.model_zariadenia or obj.nazov or "Zariadenie"
        sn = f"S/N: {obj.vyrobne_cislo}" if obj.vyrobne_cislo else "Bez S/N"
        zakaznik = obj.zakaznik.Nazov_spolocnosti if obj.zakaznik else "Nepriradený"
        
        return {
            'id': obj.id, 
            'name': f"{stroj_nazov} ({sn})", 
            'search_value': obj.vyrobne_cislo or stroj_nazov,
            'rola': zakaznik,
            'cat': '⚙️ ZARIADENIE'
        }

    def save_model(self, request, obj, form, change):
        """
        🛡️ ISAS CORE: Kompletné ukladanie zariadenia.
        Spracováva prepojenie na zákazníka, audit motohodín aj ochranu IoT živých dát.
        """
        if not change:
            # 1. AK IDE O NOVÝ STROJ: Priradíme zákazníka z URL parametra (ak existuje a nie je manuálne zvolený)
            zakaznik_id = request.GET.get('zakaznik')
            if zakaznik_id and not obj.zakaznik_id:
                try:
                    obj.zakaznik_id = int(zakaznik_id)
                except ValueError:
                    pass
            super().save_model(request, obj, form, change)
            return

        # 2. AK IDE O ÚPRAVU EXISTUJÚCEHO STROJA (change == True):
        old_mh_form = form.initial.get('aktualne_motohodiny')
        new_mh = obj.aktualne_motohodiny
        old_mh_zataz_form = form.initial.get('motohodiny_v_zatazi')
        new_mh_zataz = obj.motohodiny_v_zatazi

        mh_zmenene = (old_mh_form != new_mh) if old_mh_form is not None else False
        zataz_zmenene = (old_mh_zataz_form != new_mh_zataz) if old_mh_zataz_form is not None else False

        # Ak sa menili motohodiny, vykonáme bezpečný audit log s uzamknutím riadku
        if mh_zmenene or zataz_zmenene:
            
            with transaction.atomic():
                locked_obj = ServisovaneZariadenie.objects.select_for_update().get(pk=obj.pk)
                real_old_mh = locked_obj.aktualne_motohodiny
                real_old_mh_zataz = locked_obj.motohodiny_v_zatazi

                if real_old_mh != new_mh or real_old_mh_zataz != new_mh_zataz:
                    KorekciaMotohodinLog.objects.create(
                        zariadenie=obj,
                        povodna_hodnota=real_old_mh or 0,
                        nova_hodnota=new_mh or 0,
                        povodna_hodnota_zataz=real_old_mh_zataz or 0,
                        nova_hodnota_zataz=new_mh_zataz or 0,
                        dovod="Manuálna korekcia cez ISAS Administráciu.",
                        vykonal=request.user
                    )
                
                # 🟢 FIX: Aj pri zmene motohodín musíme rešpektovať update_fields, aby sme neprepísali IoT dáta!
                validne_polia = [f.name for f in obj._meta.concrete_fields]
                polia_na_ulozenie = [f for f in form.changed_data if f in validne_polia]
                if polia_na_ulozenie:
                    obj.save(update_fields=polia_na_ulozenie)
                else:
                    super().save_model(request, obj, form, change)
            return

        # Ak sa motohodiny NEMENTLI, uložíme len tie polia, ktoré admin skutočne zmenil (neprepíšeme IoT dáta)
        if form.changed_data:
            validne_polia = [f.name for f in obj._meta.concrete_fields]
            polia_na_ulozenie = [f for f in form.changed_data if f in validne_polia]
            if polia_na_ulozenie:
                obj.save(update_fields=polia_na_ulozenie)
            else:
                super().save_model(request, obj, form, change)
        else:
            super().save_model(request, obj, form, change)

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        """🛡️ ISAS CORE: Využitie centrálneho hooku namiesto vlastného API endpointu."""
        if action_type == 'korekcia_mh':
            if not request.user.has_perm('Evidencia.change_servisovanezariadenie'):
                m_gen = get_industry_term('meter_label_genitiv', 'počítadla')
                return JsonResponse({"status": "error", "message": f"⛔ Nemáte oprávnenie na korekciu {m_gen}."}, status=403)
            
            try:
                data = json.loads(request.body)
                nove_mh = data.get('mh')
                nove_mh_zataz = data.get('zataz')
                dovod = data.get('dovod')
            except json.JSONDecodeError:
                return JsonResponse({"status": "error", "message": "Neplatné dáta."}, status=400)

            if (nove_mh is None or str(nove_mh).strip() == '') or not dovod:
                m_gen = get_industry_term('meter_label_genitiv', 'počítadla')
                return JsonResponse({"status": "error", "message": f"Dôvod a nová hodnota ({m_gen}) sú povinné!"}, status=400)

            if not valid_ids:
                return JsonResponse({"status": "error", "message": "Nebol vybraný žiadny záznam na korekciu."}, status=400)

            try:
                nove_mh_int = int(nove_mh)
                object_id = valid_ids[0]

                with transaction.atomic():
                    try:
                        zariadenie_locked = ServisovaneZariadenie.objects.select_for_update().get(pk=object_id)
                    except ServisovaneZariadenie.DoesNotExist:
                        return JsonResponse({"status": "error", "message": "Zariadenie nebolo nájdené."}, status=404)

                    nove_mh_zataz_int = int(nove_mh_zataz) if (nove_mh_zataz is not None and str(nove_mh_zataz).strip() != '') else (getattr(zariadenie_locked, 'motohodiny_v_zatazi', 0) or 0)

                    KorekciaMotohodinLog.objects.create(
                        zariadenie=zariadenie_locked,
                        povodna_hodnota=zariadenie_locked.aktualne_motohodiny,
                        nova_hodnota=nove_mh_int,
                        povodna_hodnota_zataz=zariadenie_locked.motohodiny_v_zatazi,
                        nova_hodnota_zataz=nove_mh_zataz_int,
                        dovod=escape(dovod),
                        vykonal=request.user
                    )

                    zariadenie_locked.aktualne_motohodiny = nove_mh_int
                    zariadenie_locked.motohodiny_v_zatazi = nove_mh_zataz_int
                    zariadenie_locked.save(update_fields=['aktualne_motohodiny', 'motohodiny_v_zatazi'])

                m_label = getattr(zariadenie_locked, 'meter_label', 'Počítadlo')
                return JsonResponse({"status": "success", "message": f"{m_label} pre zariadenie bolo úspešne korigované."})
            except ValueError:
                return JsonResponse({"status": "error", "message": "Neplatný formát čísla."}, status=400)
            except Exception as e:
                return JsonResponse({"status": "error", "message": f"Chyba: {str(e)}"}, status=500)

        if action_type == 'presun_prenajmu':
            if not request.user.has_perm('Evidencia.change_servisovanezariadenie'):
                return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie na zmenu zariadenia."}, status=403)
            try:
                data = json.loads(request.body)
                cielovy_zakaznik_id = data.get('cielovy_zakaznik_id')
                vratit_na_centralu = bool(data.get('vratit_na_centralu'))
                nove_umiestnenie = str(data.get('nove_umiestnenie') or '').strip()
                datum_presunu_str = str(data.get('datum_presunu') or '').strip()
                stav_mh_raw = data.get('stav_mh')
                cislo_protokolu = str(data.get('cislo_protokolu') or '').strip()
                poznamka_presun = str(data.get('poznamka') or '').strip()
            except (json.JSONDecodeError, ValueError):
                return JsonResponse({"status": "error", "message": "Neplatný formát požiadavky."}, status=400)

            if not valid_ids:
                return JsonResponse({"status": "error", "message": "Nebolo vybrané žiadne zariadenie na presun."}, status=400)

            object_id = valid_ids[0]

            with transaction.atomic():
                try:
                    zariadenie_locked = ServisovaneZariadenie.objects.select_for_update().get(pk=object_id)
                except ServisovaneZariadenie.DoesNotExist:
                    return JsonResponse({"status": "error", "message": "Zariadenie nebolo nájdené."}, status=404)

                # 🛡️ BEZPEČNOSTNÝ FILTER FLOTILY: Cudzí zákaznícky stroj nesmie byť svojvoľne nasadený do nájmu
                if not getattr(zariadenie_locked, 'je_urcene_na_prenajom', False) and not zariadenie_locked.v_prenajme_od_isas:
                    return JsonResponse({"status": "error", "message": "⛔ Toto zariadenie nie je zaradené v Rental flotile a nemôže byť predmetom prenájmu."}, status=400)

                if not zariadenie_locked.v_prenajme_od_isas and (vratit_na_centralu or not cielovy_zakaznik_id):
                    return JsonResponse({"status": "error", "message": "⚠️ Zariadenie sa už nachádza na centrále. Zvoľte cieľového zákazníka pre nasadenie."}, status=400)

                povodny_zakaznik = zariadenie_locked.zakaznik
                povodny_zakaznik_nazov = povodny_zakaznik.Nazov_spolocnosti if povodny_zakaznik else "Centrála / Sklad"
                d_nom = get_industry_term('zariadenie_fallback_nazov', 'Zariadenie')
                m_label = getattr(zariadenie_locked, 'meter_label', 'Počítadlo')
                m_unit = getattr(zariadenie_locked, 'meter_unit', 'hod.')

                # 🕒 NAIVE ČASOVÝ REŽIM: Získanie striktne naive dátumu
                dnes_naive = timezone.now().date()
                datum_odovzdania = dnes_naive
                if datum_presunu_str:
                    parsed_datum = to_date(datum_presunu_str)
                    if not parsed_datum:
                        return JsonResponse({"status": "error", "message": "Neplatný formát dátumu presunu (očakáva sa RRRR-MM-DD)."}, status=400)
                    datum_odovzdania = parsed_datum

                teraz_str = timezone.now().strftime("%d.%m.%Y %H:%M")
                datum_zobrazenie = datum_odovzdania.strftime("%d.%m.%Y")
                user_str = request.user.get_full_name() or request.user.username

                polia_na_ulozenie = ['zakaznik', 'v_prenajme_od_isas', 'umiestnenie']

                # ⏱️ ODOVZDÁVACÍ STAV POČÍTADLA: Voliteľná korekcia motohodín pri preberaní
                stav_mh_konecny = zariadenie_locked.aktualne_motohodiny
                if stav_mh_raw is not None and str(stav_mh_raw).strip() != '':
                    try:
                        novy_stav_mh = int(stav_mh_raw)
                        if novy_stav_mh < 0:
                            return JsonResponse({"status": "error", "message": "Stav počítadla nemôže byť záporný."}, status=400)
                        aktualne_mh_db = zariadenie_locked.aktualne_motohodiny or 0
                        zataz_mh_db = getattr(zariadenie_locked, 'motohodiny_v_zatazi', 0) or 0
                        if novy_stav_mh < zataz_mh_db:
                            m_load_lbl = getattr(zariadenie_locked, 'meter_load_label', 'Hodnota v záťaži')
                            return JsonResponse({"status": "error", "message": f"Celkový stav počítadla nemôže byť nižší ako {m_load_lbl} ({zataz_mh_db} {m_unit})."}, status=400)
                        if novy_stav_mh < aktualne_mh_db:
                            return JsonResponse({"status": "error", "message": f"Nový stav počítadla ({novy_stav_mh}) nemôže byť nižší ako doterajší evidovaný stav ({aktualne_mh_db} {m_unit}). Pre korekciu nadol použite akciu Korekcia počítadla."}, status=400)
                        if novy_stav_mh != zariadenie_locked.aktualne_motohodiny:
                            KorekciaMotohodinLog.objects.create(
                                zariadenie=zariadenie_locked,
                                povodna_hodnota=zariadenie_locked.aktualne_motohodiny or 0,
                                nova_hodnota=novy_stav_mh,
                                povodna_hodnota_zataz=zariadenie_locked.motohodiny_v_zatazi or 0,
                                nova_hodnota_zataz=zariadenie_locked.motohodiny_v_zatazi or 0,
                                dovod=escape(f"Odovzdávací stav pri presune/vrátení z prenájmu (Protokol: {cislo_protokolu or 'Bez protokolu'})"),
                                vykonal=request.user
                            )
                            zariadenie_locked.aktualne_motohodiny = novy_stav_mh
                            stav_mh_konecny = novy_stav_mh
                            polia_na_ulozenie.append('aktualne_motohodiny')
                    except ValueError:
                        return JsonResponse({"status": "error", "message": "Neplatný formát čísla počítadla."}, status=400)

                meter_info = f" [Stav {m_label}: {stav_mh_konecny} {m_unit}]" if stav_mh_konecny is not None else ""
                protokol_info = f" [Protokol: {cislo_protokolu}]" if cislo_protokolu else ""

                # 🛡️ Prevádzková kontrola otvorených servisných úloh u pôvodného zákazníka (M2M zariadenia, dokoncene=False)
                varovanie_ulohy = ""
                try:
                    UlohaModel = apps.get_model('Evidencia', 'Uloha')
                    otvorene_ulohy = UlohaModel.objects.filter(zariadenia=zariadenie_locked, dokoncene=False).count()
                    if otvorene_ulohy > 0:
                        varovanie_ulohy = f" ⚠️ Upozornenie: Na pôvodného partnera ({povodny_zakaznik_nazov}) evidujeme {otvorene_ulohy} otvorených servisných úloh."
                except Exception:
                    pass

                if vratit_na_centralu:
                    interny_zakaznik = Zakaznik.objects.filter(je_interny_profil=True).first()
                    if not interny_zakaznik:
                        return JsonResponse({"status": "error", "message": "⚠️ V systéme neexistuje interný profil centrály pre vrátenie."}, status=400)
                    
                    cielovy_zakaznik = interny_zakaznik
                    zariadenie_locked.zakaznik = interny_zakaznik
                    zariadenie_locked.v_prenajme_od_isas = False
                    zariadenie_locked.umiestnenie = nove_umiestnenie or "Centrálny sklad"
                    typ_akcie_db = 'VRATENIE'
                    
                    zariadenie_locked.save(update_fields=polia_na_ulozenie)
                    sprava = f"{d_nom} bolo úspešne vrátené z prenájmu (od '{povodny_zakaznik_nazov}') na centrálu.{varovanie_ulohy}"
                else:
                    if not cielovy_zakaznik_id:
                        return JsonResponse({"status": "error", "message": "Musíte vybrať nového zákazníka."}, status=400)
                    try:
                        novy_zakaznik = Zakaznik.objects.get(pk=int(cielovy_zakaznik_id))
                    except (Zakaznik.DoesNotExist, ValueError):
                        return JsonResponse({"status": "error", "message": "Zvolený cieľový zákazník neexistuje."}, status=404)

                    if novy_zakaznik.pk == zariadenie_locked.zakaznik_id:
                        return JsonResponse({"status": "error", "message": "Zariadenie sa už u tohto partnera nachádza."}, status=400)

                    cielovy_zakaznik = novy_zakaznik
                    if novy_zakaznik.je_interny_profil:
                        zariadenie_locked.v_prenajme_od_isas = False
                        zariadenie_locked.umiestnenie = nove_umiestnenie or "Centrálny sklad"
                        typ_akcie_db = 'VRATENIE'
                        akcia_nazov = "vrátené na centrálu"
                    else:
                        je_prvotne_nasadenie = not zariadenie_locked.v_prenajme_od_isas
                        zariadenie_locked.v_prenajme_od_isas = True
                        zariadenie_locked.umiestnenie = nove_umiestnenie or f"Prevádzka {novy_zakaznik.mesto or novy_zakaznik.Nazov_spolocnosti}"
                        typ_akcie_db = 'NASADENIE' if je_prvotne_nasadenie else 'PRESUN'
                        akcia_nazov = f"nasadené k '{novy_zakaznik.Nazov_spolocnosti}'" if je_prvotne_nasadenie else f"presunuté od '{povodny_zakaznik_nazov}' k '{novy_zakaznik.Nazov_spolocnosti}'"

                    zariadenie_locked.zakaznik = novy_zakaznik
                    zariadenie_locked.save(update_fields=polia_na_ulozenie)
                    sprava = f"{d_nom} bolo úspešne {akcia_nazov}.{varovanie_ulohy}"

                # 📜 RELAČNÝ ZÁPIS DO HISTÓRIE PRENÁJMOV:
                try:
                    HistoriaPrenajmuLog = apps.get_model('Evidencia', 'HistoriaPrenajmuLog')
                    HistoriaPrenajmuLog.objects.create(
                        zariadenie=zariadenie_locked,
                        povodny_zakaznik=povodny_zakaznik,
                        novy_zakaznik=cielovy_zakaznik,
                        typ_akcie=typ_akcie_db,
                        datum_presunu=datum_odovzdania,
                        stav_pocitadla=stav_mh_konecny,
                        cislo_protokolu=cislo_protokolu,
                        nove_umiestnenie=zariadenie_locked.umiestnenie,
                        poznamka=poznamka_presun,
                        vykonal=request.user
                    )
                except Exception:
                    pass

            return JsonResponse({"status": "success", "message": sprava})
                
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description="⚡ Rýchle akcie")
    def rychle_akcie_zariadenia(self, obj):
        if not obj.pk:
            return render_isas_text("⚠️ Najprv zariadenie uložte pre zobrazenie rýchlych akcií.", muted=True)
            
        zariadenie_id = obj.pk
        zakaznik_id = obj.zakaznik.id if obj.zakaznik else ""
        
        bezpecne_zakaznik_id = escape(str(zakaznik_id))
        bezpecne_zariadenie_id = escape(str(zariadenie_id))
        
        # 1. Tlačidlo: Cenová ponuka
        url_ponuka = f"/evidencia/api/cenove-ponuky/vytvorit/?zakaznik_id={bezpecne_zakaznik_id}&zariadenie_id={bezpecne_zariadenie_id}"
        btn_ponuka = render_isas_link_btn(
            url=url_ponuka,
            text="Naceniť servis tohto zariadenia",
            icon="💰",
            type_color="info",
            wrapper=False
        )

        # 2. Tlačidlo: Korekcia počítadla
        url_fast_action = reverse(f'admin:{self.opts.app_label}_{self.opts.model_name}_fast-action')
        aktualne_mh = obj.aktualne_motohodiny or 0
        aktualne_zataz = getattr(obj, 'motohodiny_v_zatazi', 0) or 0

        # 🛡️ ISAS CORE: Využitie deklaratívneho modal enginu
        js_onclick = f"event.preventDefault(); if(window.showKorekciaModal) window.showKorekciaModal('{escapejs(url_fast_action)}', {zariadenie_id}, {aktualne_mh}, {aktualne_zataz});"
        m_gen = get_industry_term('meter_label_genitiv', 'počítadla')
        btn_korekcia = render_isas_link_btn(
            url=None,
            text=f"Korekcia {m_gen}",
            icon="⚠️",
            type_color="danger",
            is_button=True,
            onclick=js_onclick,
            wrapper=False
        )

        btn_presun = ""
        if obj.v_prenajme_od_isas or getattr(obj, 'je_urcene_na_prenajom', False):
            url_api_zakaznici = reverse(f'admin:{self.opts.app_label}_{self.opts.model_name}_changelist') + "api-zakaznici/"
            zak_nazov = escapejs(obj.zakaznik.Nazov_spolocnosti if obj.zakaznik else "")
            zar_nazov = escapejs(obj.verejny_nazov)
            zar_sn = escapejs(obj.vyrobne_cislo or "")
            zar_umiestnenie = escapejs(obj.umiestnenie or "")
            je_v_prenajme_val = "true" if obj.v_prenajme_od_isas else "false"
            d_nom_js = escapejs(get_industry_term('zariadenie_fallback_nazov', 'zariadenie'))
            
            btn_text = "Presunúť prenájom" if obj.v_prenajme_od_isas else "Nasadiť do prenájmu"
            btn_color = "warning" if obj.v_prenajme_od_isas else "success"
            btn_icon = "🤝" if obj.v_prenajme_od_isas else "🚀"

            m_label_js = escapejs(getattr(obj, 'meter_label', 'Počítadlo'))
            m_unit_js = escapejs(getattr(obj, 'meter_unit', 'hod.'))
            otvorene_ulohy_pocet = 0
            try:
                UlohaModel = apps.get_model('Evidencia', 'Uloha')
                otvorene_ulohy_pocet = UlohaModel.objects.filter(zariadenia=obj, dokoncene=False).count()
            except Exception:
                otvorene_ulohy_pocet = 0

            js_presun = (
                f"event.preventDefault(); if(window.showPresunPrenajmuModal) "
                f"window.showPresunPrenajmuModal('{escapejs(url_fast_action)}', {zariadenie_id}, "
                f"{zakaznik_id or 'null'}, '{zak_nazov}', '{escapejs(url_api_zakaznici)}', "
                f"'{zar_nazov}', '{zar_sn}', '{zar_umiestnenie}', {je_v_prenajme_val}, '{d_nom_js}', "
                f"{aktualne_mh}, '{m_label_js}', '{m_unit_js}', {otvorene_ulohy_pocet});"
            )
            btn_presun = render_isas_link_btn(
                url=None,
                text=btn_text,
                icon=btn_icon,
                type_color=btn_color,
                is_button=True,
                onclick=js_presun,
                wrapper=False
            )
        
        all_buttons = format_html('{}{}{}', btn_ponuka, btn_korekcia, btn_presun)
        buttons_layout = render_isas_layout(all_buttons, layout="row", gap=15)
        
        return format_html('{}', buttons_layout)

    @admin.display(description="🏢 Zákazník")
    def zobraz_zakaznika(self, obj):
        if not obj.zakaznik:
            return render_isas_text("➖ Nepriradený", muted=True)

        url = reverse('admin:Evidencia_zakaznik_change', args=[obj.zakaznik.id])

        return render_isas_link_btn(
            url=url,
            text=obj.zakaznik.Nazov_spolocnosti,
            icon="🏢",
            type_color="neutral",
            wrapper=False
        )

    @admin.display(description='⚙️ Zariadenie & S/N', ordering='model_zariadenia')
    def get_zariadenie_card(self, obj):
        nazov = obj.nazov or obj.model_zariadenia or "Neznáme zariadenie"
        sn = obj.vyrobne_cislo or "Bez S/N"
        kat_display = obj.get_kategoria_display() if obj.kategoria else "Nezaradené"

        sn_badge = render_isas_badge(f"S/N: {sn}", type_color="neutral", monospace=True, wrapper=False)
        vyrobca_badge = render_isas_badge(str(obj.vyrobca) if obj.vyrobca else "Neznámy výrobca", icon="🏭", type_color="info", wrapper=False)

        badges = [sn_badge, vyrobca_badge]

        # 🚜 ISAS RENTAL: Dynamické odznaky pre flotilu a web
        if getattr(obj, 'je_urcene_na_prenajom', False):
            badges.append(render_isas_badge("RENTAL", icon="🚜", type_color="purple", wrapper=False))
        if getattr(obj, 'zobrazit_na_webe_prenajom', False):
            badges.append(render_isas_badge("WEB", icon="🌐", type_color="success", wrapper=False))
        if getattr(obj, 'cena_prenajmu_na_den', None) and obj.cena_prenajmu_na_den > Decimal('0.00'):
            badges.append(render_isas_badge(f"{obj.cena_prenajmu_na_den} {get_firemna_mena()}/deň", icon="💰", type_color="success", wrapper=False))
        if getattr(obj, 'slug', None):
            badges.append(render_isas_badge(f"/{obj.slug}", type_color="neutral", monospace=True, wrapper=False))

        badges_row = render_isas_layout(mark_safe("".join(badges)), layout="row", gap=4, custom_style="margin-top: 4px; flex-wrap: wrap; justify-content: center;")

        # 🖼️ Ak má zariadenie fotku pre web, ukážeme ju priamo v avatare
        image_url = None
        if getattr(obj, 'obrazok_na_webe', None) and getattr(obj.obrazok_na_webe, 'name', None):
            try:
                image_url = obj.obrazok_na_webe.url
            except (ValueError, AttributeError):
                image_url = None

        return render_isas_avatar(
            icon="⚙️",
            title=nazov,
            subtitle=kat_display,
            bg_gradient="#f8fafc, #f1f5f9",
            shadow_color="rgba(0,0,0,0.05)",
            extra_html=badges_row,
            image_url=image_url
        )

    @admin.display(description='🏢 Zákazník & Lokalita', ordering='zakaznik__Nazov_spolocnosti')
    def get_zakaznik_card(self, obj):
        # 🚜 ISAS RENTAL: Ak je to naše zariadenie a nie je u klienta, je na centrále (aj keď je technicky priradené internej firme)
        if getattr(obj, 'je_urcene_na_prenajom', False) and not obj.v_prenajme_od_isas:
            nazov_html = render_isas_text("🏢 Centrálny sklad", size=13, color="#0f172a", weight=800)
            dostupne_badge = render_isas_badge("DOSTUPNÉ IHNEĎ", icon="🟢", type_color="success", wrapper=False, custom_style="margin-top: 4px;")
            return render_isas_layout(format_html('{}{}', nazov_html, dostupne_badge), layout="col", center=False)

        if not obj.zakaznik:
            return render_isas_text("➖ Nepriradený", muted=True)

        nazov = obj.zakaznik.Nazov_spolocnosti
        umiestnenie = obj.umiestnenie or "Lokalita nešpecifikovaná"

        nazov_html = render_isas_text(f"🏢 {nazov}", size=13, color="#0f172a", weight=800)
        umiestnenie_html = render_isas_text(f"📍 {umiestnenie}", size=11, color="#64748b", weight=600, custom_style="margin-top: 2px;")

        prenajom_badge = ""
        if obj.v_prenajme_od_isas:
            prenajom_badge = render_isas_badge("U ZÁKAZNÍKA", icon="🤝", type_color="warning", wrapper=False, custom_style="margin-top: 4px;")

        return render_isas_layout(format_html('{}{}{}', nazov_html, umiestnenie_html, prenajom_badge), layout="col", center=False)

    @admin.display(description='⏱️ Stav počítadla', ordering='aktualne_motohodiny')
    def get_telemetria_card(self, obj):
        mh_str = obj.meter_display if obj.aktualne_motohodiny is not None else "—"
        zataz_str = obj.meter_load_display if getattr(obj, 'motohodiny_v_zatazi', None) is not None else "—"

        if obj.v_servisnom_rezime:
            stav_color = "#f59e0b"
            stav_text = "SERVISNÝ REŽIM"
        else:
            stav_color = "#10b981" if obj.aktualne_motohodiny is not None else "#64748b"
            stav_text = "AKTÍVNE" if obj.aktualne_motohodiny is not None else "ČAKÁ NA DÁTA"

        m_unit = getattr(obj, 'meter_unit', 'hod.')
        m_load_unit = getattr(obj, 'meter_load_unit', m_unit)

        return render_isas_mini_stat_card(
            top_label=f"Celkovo ({m_unit})",
            top_val=mh_str,
            top_val_color=stav_color,
            bot_label=f"Záťaž ({m_load_unit})",
            bot_val=zataz_str,
            bot_val_color="#475569"
        )

    @admin.display(description='🛡️ Záruka', ordering='zaruka_do')
    def get_zaruka_card(self, obj):
        dnes = timezone.now().date()
        predlzena = to_date(obj.predlzena_zaruka_do)
        zaruka = to_date(obj.zaruka_do)
        if predlzena and predlzena >= dnes:
            badge = render_isas_badge("PREDĹŽENÁ ZÁRUKA", icon="💎", type_color="purple", wrapper=False)
            datum = render_isas_text(f"do {predlzena.strftime('%d.%m.%Y')}", size=11, color="#7c3aed", weight=700, custom_style="margin-top: 2px;")
            return render_isas_layout(format_html('{}{}', badge, datum), layout="col", center=True)
        if zaruka and zaruka >= dnes:
            badge = render_isas_badge("V ZÁRUKE", icon="🛡️", type_color="success", wrapper=False)
            datum = render_isas_text(f"do {zaruka.strftime('%d.%m.%Y')}", size=11, color="#059669", weight=700, custom_style="margin-top: 2px;")
            return render_isas_layout(format_html('{}{}', badge, datum), layout="col", center=True)
        if zaruka or predlzena:
            return render_isas_badge("PO ZÁRUKE", icon="⚪", type_color="neutral", wrapper=True)
        return render_isas_badge("NEZADANÁ", icon="➖", type_color="neutral", wrapper=True)

    @admin.display(description='🔌 IoT Konektivita', ordering='iot_je_aktivny')
    def get_iot_status(self, obj):
        if obj.iot_je_aktivny:
            badge = render_isas_badge("ONLINE", icon="📡", type_color="success", wrapper=False, css_class="pulse-green")
            protokol = obj.get_iot_protokol_display() if obj.iot_protokol else "Neznámy protokol"
            subtext = render_isas_text(protokol, size=10, color="#059669", weight=700, custom_style="margin-top: 4px;")
            return render_isas_layout(format_html('{}{}', badge, subtext), layout="col", center=True)

        return render_isas_badge("OFFLINE", icon="🔌", type_color="neutral", wrapper=True)

    @admin.display(description="🏷️ Kategória", ordering="kategoria")
    def get_kategoria_badge(self, obj):
        if not obj.kategoria: return render_isas_text("❌", muted=True)
    
        kategoria_zobrazenie = obj.get_kategoria_display()
        type_color = ZARIADENIA_COLOR_MAP.get(obj.kategoria, "neutral")

        return render_isas_badge(kategoria_zobrazenie, type_color=type_color, wrapper=False)

    def get_inline_instances(self, request, obj=None):
        if not obj or not obj.kategoria:
            return []
        inline_class = vygenerovane_zariadenia_inlines.get(obj.kategoria)
        if inline_class:
            return [inline_class(self.model, self.admin_site)]
        return []

    def response_post_save_add(self, request, obj):
        if obj.zakaznik:
            target_url = reverse('admin:Evidencia_zakaznik_change', args=[obj.zakaznik.id])
            return redirect(f'{target_url}#highlight-device-{obj.id}')
        return super().response_post_save_add(request, obj)

    def response_post_save_change(self, request, obj):
        if obj.zakaznik:
            target_url = reverse('admin:Evidencia_zakaznik_change', args=[obj.zakaznik.id])
            return redirect(f'{target_url}#highlight-device-{obj.id}')
        return super().response_post_save_change(request, obj)

@admin.register(ServisnyPredpis)
class ServisnyPredpisAdmin(ISASHiddenAdminMixin, ISASPremiumAdmin):
    pass

@admin.register(PolozkaPredpisu)
class PolozkaPredpisuAdmin(ISASHiddenAdminMixin, ISASPremiumAdmin):
    pass

@admin.register(Zakaznik)
class ZakaznikAdmin(KontaktnaOsobaAdminMixin, ISASObchodnyPartnerAdminMixin, ISASSecurityAdminMixin, ERPExportAdminMixin, ISASPremiumAdmin):
    isas_title = "🏢 Zákazníci"
    isas_preloader_text = "⏳ Vstupujete do zoznamu zákazníkov..."
    isas_magia_kwargs = {
        "live_search_endpoint": "detective-search/",
        "live_search_ikona": "🏢",
    }

    list_display = (
        'erp_status_ikona',
        'get_zakaznik_main', 
        'get_ids_pills', 
        'get_hq_address', 
        'get_contact_card', 
        'get_web_link',
        'get_esg_stats'
    )
    list_display_links = ('get_zakaznik_main',)
    search_fields = ['Nazov_spolocnosti', 'subdomena', 'ico', 'dic', 'email', 'telefon', 'user__username', 'user__email']
    readonly_fields = ('pridat_zariadenie_tlacidlo', 'get_doklady_dashboard', 'erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log', 'lat', 'lon',)
    autocomplete_fields = ['user']  # 🛡️ UX: Rýchle vyhľadávanie používateľa cez Select2
    list_filter = ('erp_export_status', ISASWebstrankaFilter, ISASKontaktnaOsobaFilter)
    inlines = [ServisovaneZariadenieInline]

    fieldsets = (
        ('⚙️ RÝCHLE AKCIE', {
            'fields': ('pridat_zariadenie_tlacidlo',),
            'description': '⚡ Rýchle skratky pre vytvorenie cenovej ponuky alebo pridanie servisovaného zariadenia priamo z karty. 🚀'
        }),
        ('🏢 ZÁKLADNÁ IDENTIFIKÁCIA ZÁKAZNÍKA', {
            'fields': (
                ('Nazov_spolocnosti', 'ic_dph'),
                ('ico', 'dic'),
                ('zobrazit_ako_referenciu',),  # 🌐 Doplnený súhlas so zverejnením na webe (NDA)
            ),
            'description': 'Základné fakturačné údaje spoločnosti. 🪪 IČO slúži ako unikátny identifikátor v systéme. ⚙️'
        }),
        ('🏦 BANKOVÉ SPOJENIE A B2B PORTÁL', {
            'fields': (
                ('iban', 'predvolena_splatnost_dni', 'reklamacna_lehota_dni'),
                ('b2b_zlava_percenta', 'user'),  # 🔑 Prepojené B2B nastavenia (Zľava + Účet)
            ),
            'description': '💸 Fakturačné podmienky, zmluvná záručná lehota (SLA) a nastavenie B2B klientskej zóny na verejnom webe. Priradený používateľ uvidí zmluvné ceny automaticky. 📊'
        }),
        ('👤 HLAVNÝ KONTAKT A WEB', {
            'fields': (
                ('oslovenie_rod', 'kontaktna_osoba'), 
                ('telefon', 'email'),
                ('domena', 'subdomena'),
                ('datum_narodenia',)
            ),
            'description': '📞 Kontaktné údaje na zodpovednú osobu za stranu zákazníka. 🌐 Doména slúži pre AI a rýchly preklik na ich web (subdoménu vypĺňajte iba pri aktívnom multitenant wildcard režime).'
        }),
        ('📍 SÍDLO SPOLOČNOSTI', {
            'fields': (
                ('ulica_a_cislo', 'mesto'),
                ('psc', 'stat'),
                ('lat', 'lon')
            ),
            'description': '🗺️ Oficiálna adresa sídla firmy. GPS súradnice (lat, lon) sa vypočítajú automaticky pri uložení pre potreby Live Radaru. 🚙'
        }),
        ('🌱 ESG A ENERGETIKA (ISAS IOT)', {
            'fields': (
                ('cena_za_kwh_eur', 'emisny_faktor_co2'),
            ),
            'description': '📊 Parametre pre výpočet finančných strát a uhlíkovej stopy (ESG) na IoT Dashboarde. Tieto dáta slúžia ako silný predajný argument pre optimalizáciu prevádzky zákazníka. 🌍'
        }),
        ('📝 OSTATNÉ INFORMÁCIE', {
            'fields': ('Ostatne',),
            'description': '📓 Akékoľvek interné poznámky a špecifiká k zákazníkovi, ktoré sa nikdy netlačia na faktúry. 🤫'
        }),
        ('🚧 BEZPEČNOSŤ A VSTUPY DO AREÁLOV (ENTRY MATRIX)', {
            'fields': (
                'vyzaduje_povolenie_vstupu',
                'email_vratnice',
                'telefon_vratnica', 
                'instrukcie_vstupu', 
                'sablona_vstupu',
                'url_vstupneho_portalu'
            ),
            'description': '🦺 Nastavenie pravidiel pre vstup technikov na pracovisko zákazníka. Modul Entry Matrix tieto dáta spracuje a automatizuje povolenky. 🤖'
        }),
        ('📦 DOKLADY A HISTÓRIA PREDAJA', {
            'fields': ('get_doklady_dashboard',),
            'description': '⚡ Rýchly prístup k naposledy vygenerovaným cenovým ponukám, prijatým objednávkam a vystaveným faktúram. 📈'
        }),
        ('📡 ISAS ERP DIAGNOSTIKA', {
             'fields': ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log'),
             'description': '🔗 Technické informácie o synchronizácii s externým účtovným systémom (Pohoda / Kros). Zasahujte len v prípade chyby! 🛠️'
        }),
    )

    def has_delete_permission(self, request, obj=None):
        # 🛡️ OCHRANA: Interný profil firmy nie je možné vymazať z adminu ani cez bulk akcie
        if obj and getattr(obj, 'je_interny_profil', False):
            return False
        return super().has_delete_permission(request, obj)

    def get_list_display(self, request):
        ld = list(super().get_list_display(request))
        if not HAS_ENERGY_ESG_AUDIT and 'get_esg_stats' in ld:
            ld.remove('get_esg_stats')
        return ld

    def get_fieldsets(self, request, obj=None):
        fs = list(super().get_fieldsets(request, obj))
        if not HAS_ENERGY_ESG_AUDIT:
            fs = [item for item in fs if item[0] != '🌱 ESG A ENERGETIKA (ISAS IOT)']
        return tuple(fs)

    def get_readonly_fields(self, request, obj=None):
        rf = super().get_readonly_fields(request, obj)
        # 🛡️ Ak ide o našu firmu, uzamkneme fakturačné a B2B polia (editujú sa výhradne vo Firemnej Identite)
        if obj and getattr(obj, 'je_interny_profil', False):
            return tuple(rf) + (
                'Nazov_spolocnosti', 'ico', 'dic', 'ic_dph', 
                'ulica_a_cislo', 'mesto', 'psc', 'stat', 
                'email', 'telefon', 'iban', 'domena',
                'b2b_zlava_percenta', 'user', 'zobrazit_ako_referenciu',
                'reklamacna_lehota_dni'
            )
        return rf

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.annotate(
            ponuky_count=Count('cenove_ponuky', distinct=True),
            objednavky_count=Count('prijate_objednavky', distinct=True),
            faktury_count=Count('vystavene_faktury', distinct=True)
        )
        # 🛡️ VÝKON: select_related('user') eliminuje N+1 dopyty pri vykresľovaní B2B odznakov v zozname
        return qs.select_related('user').prefetch_related('servisovane_zariadenia')

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(Nazov_spolocnosti__icontains=term) |
            Q(subdomena__icontains=term) |
            Q(ico__icontains=term) |
            Q(dic__icontains=term) |
            Q(ic_dph__icontains=term) |
            Q(iban__icontains=term) |
            Q(email__icontains=term) |
            Q(telefon__icontains=term) |
            Q(kontaktna_osoba__icontains=term) |
            Q(user__username__icontains=term) |
            Q(user__email__icontains=term)
        )[:30]

    def format_detective_result(self, obj):
        nazov = obj.Nazov_spolocnosti or "Neznámy zákazník"
        ico_text = f"IČO: {obj.ico}" if obj.ico else "Bez IČO"
        lokalita = f"📍 {obj.mesto}" if obj.mesto else ""
        b2b_tag = " | 🔑 B2B Účet" if obj.user_id else ""
        podtitul = f"{ico_text} {lokalita}{b2b_tag}".strip()
        
        return {
            'id': obj.id, 
            'name': nazov, 
            'search_value': nazov,
            'rola': podtitul,
            'cat': '🏢 ZÁKAZNÍK'
        }

    @admin.display(description='⚙️ Rýchle akcie')
    def pridat_zariadenie_tlacidlo(self, obj):
        if not obj.pk:
            return render_isas_text("⚠️ Pre zobrazenie rýchlych akcií zariadenie najskôr uložte.", muted=True)

        customer_id = obj.pk
        url_zariadenie = f"/admin/Evidencia/servisovanezariadenie/add/?zakaznik={customer_id}"
        url_ponuka = f"/evidencia/api/cenove-ponuky/vytvorit/?zakaznik_id={customer_id}"
        
        btn_zariadenie = render_isas_link_btn(url_zariadenie, "Pridať zariadenie", icon="➕", type_color="success", wrapper=False)
        btn_ponuka = render_isas_link_btn(url_ponuka, "Vytvoriť cenovú ponuku", icon="💰", type_color="info", wrapper=False)

        buttons_layout = render_isas_layout(format_html('{}{}', btn_zariadenie, btn_ponuka), layout="row", gap=15)
        
        return format_html('{}', buttons_layout)

    @admin.display(description='🌱 ESG & IoT Dáta')
    def get_esg_stats(self, obj):
        items = []
        
        if obj.cena_za_kwh_eur:
            items.append(render_isas_badge(f"{obj.cena_za_kwh_eur} {get_firemna_mena()}/kWh", icon="⚡", type_color="warning", wrapper=False))
        if obj.emisny_faktor_co2:
            items.append(render_isas_badge(f"{obj.emisny_faktor_co2} CO₂", icon="🌍", type_color="success", wrapper=False))
            
        if not items:
            return render_isas_badge("Nenastavené", type_color="neutral", wrapper=True)
            
        return render_isas_layout(mark_safe("".join(items)), layout="col", gap=4, center=True)

    @admin.display(description='📦 Doklady Zákazníka')
    def get_doklady_dashboard(self, obj):
        if not obj or not obj.pk:
            return render_isas_text("⚠️ Pre zobrazenie dokladov zákazníka najskôr uložte zmeny.", muted=True)

        # Configuration: (Anotovaný atritút, ORM relácia, Názov modelu, Nadpis, Ikona, Farba)
        doklady_config = [
            ('ponuky_count', 'cenove_ponuky', 'cenovaponuka', 'Cenové ponuky', '📄', 'info'),
            ('objednavky_count', 'prijate_objednavky', 'prijataobjednavka', 'Objednávky', '🛒', 'warning'),
            ('faktury_count', 'vystavene_faktury', 'vystavenafaktura', 'Faktúry', '💰', 'success'),
        ]

        buttons = []
        for attr, rel, model, label, icon, color in doklady_config:
            cnt = getattr(obj, attr, None) or (getattr(obj, rel).count() if hasattr(obj, rel) else 0)
            try:
                url = f"{reverse(f'admin:Evidencia_{model}_changelist')}?zakaznik__id__exact={obj.pk}"
            except Exception:
                url = "#"
            buttons.append(render_isas_link_btn(url, label, icon=icon, type_color=color if cnt > 0 else "neutral", counter_val=cnt, wrapper=False))

        return render_isas_layout(mark_safe("".join(buttons)), layout="row", gap=10, center=True)

    @admin.display(description='🏢 Zákazník', ordering='Nazov_spolocnosti')
    def get_zakaznik_main(self, obj):
        image_url = None
        if obj.domena:
            clean_domain = str(obj.domena).lower().strip()
            for prefix in ['https://', 'http://', 'www.']:
                if clean_domain.startswith(prefix):
                    clean_domain = clean_domain[len(prefix):]
            
            clean_domain = clean_domain.split('/')[0].split('?')[0].split('#')[0]
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
                
            if '.' in clean_domain and len(clean_domain) > 3:
                image_url = f"https://www.google.com/s2/favicons?sz=64&domain={escape(clean_domain)}"

        badges = []
        if getattr(obj, 'je_interny_profil', False):
            badges.append(render_isas_badge("CENTRÁLA / INTERNÝ SKLAD", icon="🏛️", type_color="purple", wrapper=False))
        if getattr(obj, 'user_id', None):
            badges.append(render_isas_badge("B2B ÚČET", icon="🔑", type_color="info", wrapper=False))
        
        b2b_zlava = getattr(obj, 'b2b_zlava_percenta', None)
        if b2b_zlava is not None:
            try:
                if Decimal(str(b2b_zlava)) > Decimal('0.00'):
                    badges.append(render_isas_badge(f"ZĽAVA {b2b_zlava} %", icon="💰", type_color="success", wrapper=False))
            except (InvalidOperation, TypeError, ValueError):
                pass

        # 🛡️ DEFENZÍVNE OŠETRENIE ZMLUVNEJ LEHOTY ZÁRUKY / REKLAMÁCIÍ (SLA)
        reklamacna_lehota = getattr(obj, 'reklamacna_lehota_dni', None)
        if reklamacna_lehota is not None:
            try:
                lehota_dni = int(reklamacna_lehota)
                if lehota_dni > 0:
                    word = "deň" if lehota_dni == 1 else ("dni" if 2 <= lehota_dni <= 4 else "dní")
                    badges.append(render_isas_badge(f"ZÁRUKA SLA: {lehota_dni} {word}", icon="🛡️", type_color="info", wrapper=False))
            except (TypeError, ValueError):
                pass

        if getattr(obj, 'subdomena', None):
            badges.append(render_isas_badge(f"PORTÁL: {obj.subdomena}", icon="🔗", type_color="neutral", wrapper=False))
        if getattr(obj, 'zobrazit_ako_referenciu', False):
            badges.append(render_isas_badge("REFERENCIA", icon="🌐", type_color="info", wrapper=False))
        if getattr(obj, 'vyzaduje_povolenie_vstupu', False):
            badges.append(render_isas_badge("VYŽADUJE VSTUP", icon="🚧", type_color="warning", wrapper=False))

        extra_html = mark_safe("".join(badges)) if badges else ""

        return render_isas_avatar(
            icon="🏢",
            title=obj.Nazov_spolocnosti,
            subtitle="",
            bg_gradient="#f8fafc, #f1f5f9",
            shadow_color="rgba(0,0,0,0.05)",
            extra_html=extra_html,
            image_url=image_url
        )

class ZakaznikPrenajomFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon, isas_order_by = '👤 Zákazník', 'zakaznik', 'zakaznik', '🏢', 'Nazov_spolocnosti'

class KategoriaPrenajatehoZariadeniaFilter(admin.SimpleListFilter):
    title = '📂 Kategória zariadenia'
    parameter_name = 'kategoria_zariadenia'

    def lookups(self, request, model_admin):
        
        # 📥 Stiahneme iba unikátne kódy kategórií priamo na úrovni SQL ⚡
        kategorie_kody = ServisovaneZariadenie.objects.filter(
            v_prenajme_od_isas=True
        ).values_list('kategoria', flat=True).distinct()
        
        # 🗺️ Namapujeme ich z existujúceho slovníka bez inštanciovania modelov 🧠
        kategorie_dict = dict(ServisovaneZariadenie.KATEGORIE)
        kategorie = [(kod, kategorie_dict.get(kod, f"❓ {kod}")) for kod in kategorie_kody if kod]
        
        # 📏 Zoradíme podľa názvu kategórie
        return sorted(kategorie, key=lambda x: x[1])

    def queryset(self, request, queryset):
        if self.value():
            # 🎯 Aplikujeme filter na kategóriu zariadenia
            return queryset.filter(zariadenie__kategoria=self.value())
        return queryset

class VyrobcaZariadeniaPrenajomFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon, isas_order_by = '🏭 Výrobca zariadenia', 'vyrobca', 'zariadenie__vyrobca', '🏭', 'nazov_vyrobcu'

    def get_related_queryset(self, model_admin, field_name):
        qs = DatabazaVyrobcov.objects.filter(
            Exists(model_admin.model.objects.filter(zariadenie__vyrobca=OuterRef('pk')))
        )
        if self.isas_order_by: 
            qs = qs.order_by(self.isas_order_by)
        return qs.distinct()

@admin.register(ZakaznikPrenajom)
class ZakaznikPrenajomAdmin(ISASReadOnlyAdminMixin, ISASPremiumAdmin):
    isas_title = "🤝 Zákazníci - prenájmy zariadení"
    isas_preloader_text = "⏳ Vstupujete do zoznamu prenajímaných zariadení..."
    isas_magia_kwargs = {
        "typ_zobrazenia": "tabulka",
        "live_search_endpoint": "detective-search/",
        "live_search_ikona": "🤝",
        "live_search_kategoria": "PRENÁJOM",
        "skryt_akcie": True
    }

    list_display = (
        'get_zariadenie_card', 
        'get_zakaznik_main', 
        'get_contact_card'
    )
    
    list_display_links = None
    search_fields = ('zakaznik__Nazov_spolocnosti', 'zariadenie__nazov', 'zariadenie__model_zariadenia')
    list_filter = (ZakaznikPrenajomFilter, KategoriaPrenajatehoZariadeniaFilter, VyrobcaZariadeniaPrenajomFilter)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # ISAS ENGINE: Eliminácia masívneho N+1 dotazu v prenájmoch
        return qs.select_related('zakaznik', 'zariadenie')

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'zakaznik', 'zariadenie'
        ).filter(
            Q(zakaznik__Nazov_spolocnosti__icontains=term) |
            Q(zariadenie__nazov__icontains=term) |
            Q(zariadenie__model_zariadenia__icontains=term) |
            Q(zariadenie__vyrobne_cislo__icontains=term)
        )[:30]

    def format_detective_result(self, obj):
        zakaznik_nazov = obj.zakaznik.Nazov_spolocnosti if obj.zakaznik else "Neznámy zákazník"
        if obj.zariadenie:
            kat = obj.zariadenie.get_kategoria_display() if getattr(obj.zariadenie, 'kategoria', None) else "⚙️ Zariadenie"
            nazov = obj.zariadenie.nazov or ""
            model = obj.zariadenie.model_zariadenia or ""
            zariadenie_text = " ".join([str(p) for p in [kat, nazov, model] if p]).strip()
            sn = f"S/N: {obj.zariadenie.vyrobne_cislo}" if obj.zariadenie.vyrobne_cislo else ""
        else:
            zariadenie_text = "Neznáme zariadenie"
            sn = ""

        return {
            'id': obj.id,
            'name': f"{zakaznik_nazov} ➔ {zariadenie_text}",
            'search_value': zakaznik_nazov,
            'rola': sn,
            'cat': 'PRENÁJOM'
        }

    @admin.display(description='🏷️ Prenajaté zariadenie', ordering='zariadenie__nazov')
    def get_zariadenie_card(self, obj):
        if not obj.zariadenie:
            return render_isas_badge("Chybné dáta (Zariadenie neexistuje)", icon="⚠️", type_color="danger", wrapper=True)
        
        z = obj.zariadenie
        url_upravy = f"/admin/Evidencia/servisovanezariadenie/{z.id}/change/"
    
        kat_display = z.get_kategoria_display() or "⚙️ Nezaradené"
        emoji = kat_display.split(' ')[0] if ' ' in kat_display else '⚙️'
        kat_text = kat_display.replace(emoji, '').strip()
    
        badges = []
        if z.rok_vyroby:
            badges.append(render_isas_badge(str(z.rok_vyroby), icon="📅", type_color="neutral", wrapper=False))
        if z.model_zariadenia:
            badges.append(render_isas_badge(str(z.model_zariadenia), prefix_text="MODEL", type_color="neutral", wrapper=False))

        badges_html = render_isas_layout(mark_safe("".join(badges)), layout="row", gap=4, center=True) if badges else ""
    
        btn_html = render_isas_link_btn(url=url_upravy, text="Detail zariadenia", icon="🔗", type_color="dashed", wrapper=True)
    
        extra_html = render_isas_layout(format_html('{}{}', badges_html, btn_html), layout="col", gap=6, center=True)

        return render_isas_avatar(
            icon=emoji,
            title=z.nazov,
            subtitle=kat_text,
            bg_gradient="#64748b, #334155",
            shadow_color="rgba(0,0,0,0.1)",
            extra_html=extra_html
        )

    @admin.display(description='🏢 Zákazník', ordering='zakaznik__Nazov_spolocnosti')
    def get_zakaznik_main(self, obj):
        zakaznik = obj.zakaznik
        image_url = None
    
        if zakaznik.domena:
            clean_domain = zakaznik.domena.lower().replace('http://', '').replace('https://', '').replace('www.', '').split('/')[0].strip()
            if '@' in clean_domain: clean_domain = clean_domain.split('@')[-1]
            if '.' in clean_domain:
                image_url = f"https://www.google.com/s2/favicons?sz=64&domain={escape(clean_domain)}"

        hidden_span = render_isas_hidden_data("prenajom-id-hidden", obj.id)
    
        avatar = render_isas_avatar(
            icon="🏢",
            title=zakaznik.Nazov_spolocnosti,
            subtitle="Nájomca",
            bg_gradient="#3b82f6, #1d4ed8",
            shadow_color="rgba(59,130,246,0.2)",
            image_url=image_url
        )
    
        return format_html('{}{}', hidden_span, avatar)

    @admin.display(description='📇 Zákaznícky kontakt')
    def get_contact_card(self, obj):
        zakaznik = obj.zakaznik
        items = []
    
        if zakaznik.kontaktna_osoba and zakaznik.kontaktna_osoba != '-':
            person = render_isas_text(zakaznik.kontaktna_osoba, size=12, color="#0f172a", weight=800, icon="👤")
            items.append(render_isas_singleton_row(person, is_dashed=True))
    
        if zakaznik.telefon and zakaznik.telefon != '-':
            items.append(render_isas_badge(zakaznik.telefon, icon="📞", copy_value=zakaznik.telefon, wrapper=True))
        
        if zakaznik.email and zakaznik.email != '-':
            items.append(render_isas_badge(zakaznik.email, icon="✉️", copy_value=zakaznik.email, wrapper=True))

        if not items:
            return render_isas_text("📭 Žiadny kontakt", muted=True)

        return render_isas_layout(mark_safe("".join(items)), layout="col", gap=4, center=True)

class StavCenyFilter(admin.SimpleListFilter):
    title = '🚦 Stav predajnej ceny'
    parameter_name = 'stav_ceny'

    def lookups(self, request, model_admin):
        mena = get_firemna_mena()
        return (
            ('ok', f'✅ Cena je nastavená (> 0 {mena})'),
            ('chyba', f'⚠️ Cena chýba (0.00 {mena})'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'ok':
            return queryset.filter(predajna_cena__gt=0)
        if self.value() == 'chyba':
            return queryset.filter(Q(predajna_cena=0) | Q(predajna_cena__isnull=True))
        return queryset

class ZiskovostFilter(admin.SimpleListFilter):
    title = '💰 Úroveň prirážky'
    parameter_name = 'ziskovost'

    def lookups(self, request, model_admin):
        return (
            ('vysoka', '🚀 Vysoká (nad 30%)'),
            ('stredna', '👍 Štandardná (15% - 30%)'),
            ('nizka', '📉 Nízka (pod 15%)'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'vysoka':
            return queryset.filter(marza_percento__gte=30)
        if self.value() == 'stredna':
            return queryset.filter(marza_percento__gte=15, marza_percento__lt=30)
        if self.value() == 'nizka':
            return queryset.filter(marza_percento__lt=15)
        return queryset

@admin.register(CenovyKatalog)
class CenovyKatalogAdmin(ISASPremiumAdmin):
    list_display = (
        'get_kategoria', 
        'get_nazov_polozky', 
        'get_merna_jednotka_vizual',
        'nakupna_cena', 
        'marza_percento',
        'get_skutocna_marza',
        'predajna_cena_formatovana', 
        'get_cisty_zisk',
        'get_aktualizacia'
    )
    list_display_links = None
    list_editable = ('nakupna_cena', 'marza_percento')    
    list_filter = (HlavnaKategoriaKatalogFilter, SkladoveModelyFilter, StavCenyFilter, ZiskovostFilter)
    readonly_fields = ('predajna_cena',)
    search_fields = ('search_name',)
    action_form = MarzaActionForm

    isas_margin_manager = True

    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "📦",
        'bez_tlacidla_odstranit': True,
        'ignore_default_action_ids': ["btn-bulk-margin", "isas-custom-save-btn"],
        'katalogovy_dashboard': True,
        'katalog_choices': CATEGORY_CHOICES
    }

    def get_detective_queryset(self, request, term):
        full_qs = self.get_queryset(request)
        qs, _ = self.get_search_results(request, full_qs, term)
        return qs[:30]

    def format_detective_result(self, obj):
        cat_name = get_kategoria_nazov(obj.content_type.model_class()).upper() if obj.content_type else "KATEGÓRIA"
        cisty_nazov = ocisti_nazov_od_jednotky(obj.polozka_zo_skladu)
        safe_name = cisty_nazov
        cena = f"{obj.predajna_cena} {get_firemna_mena()}" if obj.predajna_cena else "Cena neurčená"
        return {
            'id': obj.id, 
            'name': safe_name, 
            'search_value': safe_name, 
            'suma': cena,
            'cat': cat_name
        }

    # --- 🚀 BLESKOVÉ API PRE UKLADANIE (ISAS HOOK) ---
    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if not request.user.has_perm('Evidencia.change_cenovykatalog'):
            return JsonResponse({"status": "error", "message": "⛔ Nemáte oprávnenie meniť ceny v katalógu."}, status=403)

        try:
            data = json.loads(request.body)
            updated_items_data = []

            # 1. HROMADNÉ NASTAVENIE PRIRÁŽKY
            if action_type == 'bulk_margin':
                marza_str = data.get('marza')
                if marza_str is None or str(marza_str).strip() == '':
                    return JsonResponse({"status": "error", "message": "⚠️ Nezadaná hodnota prirážky."})
                
                try:
                    marza = Decimal(str(marza_str).replace(' ', '').replace(',', '.'))
                except InvalidOperation:
                    return JsonResponse({"status": "error", "message": "❌ Neplatný formát prirážky. Použite číslo."})
                    
                with transaction.atomic():
                    int_ids = [int(i) for i in valid_ids if str(i).isdigit()]
                    qs = self.model.objects.select_for_update().filter(id__in=int_ids)
                    
                    for obj in qs:
                        obj.marza_percento = marza
                        if obj.nakupna_cena is not None and obj.nakupna_cena > Decimal('0.00'):
                            obj.predajna_cena = (obj.nakupna_cena * (Decimal('1.00') + (obj.marza_percento / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        else:
                            obj.predajna_cena = Decimal('0.00')
                        
                        if hasattr(obj, 'posledna_aktualizacia'):
                            obj.posledna_aktualizacia = timezone.now()
                            
                        obj.save()
                        
                        updated_items_data.append({
                            'id': obj.id,
                            'predajna_cena': self.predajna_cena_formatovana(obj),
                            'cisty_zisk': self.get_cisty_zisk(obj),
                            'skutocna_marza': self.get_skutocna_marza(obj),
                            'nakupna_cena_val': f"{obj.nakupna_cena:.2f}" if obj.nakupna_cena is not None else "0.00",
                            'marza_val': f"{obj.marza_percento:.2f}" if obj.marza_percento is not None else "0.00"
                        })

                count = len(updated_items_data)
                word = "položku" if count == 1 else "položky" if count < 5 else "položiek"
                msg = f"✨ Prirážka {marza}% bola aplikovaná na {count} {word}!"
                return JsonResponse({"status": "success", "message": msg, "updated_items": updated_items_data})

            # 2. MANUÁLNE ULOŽENIE ZMIEN V TABUĽKE
            elif action_type == 'manual_save':
                items = data.get('items', [])
                if not items:
                    return JsonResponse({"status": "error", "message": "Žiadne položky na uloženie."})

                updated_items_data = []
                
                with transaction.atomic():
                    item_ids = [int(item['id']) for item in items if str(item.get('id', '')).isdigit()]
                    objects_dict = self.model.objects.select_for_update().in_bulk(item_ids)
                    
                    for item in items:
                        item_id = int(item['id']) if str(item.get('id', '')).isdigit() else None
                        obj = objects_dict.get(item_id)
                        if not obj:
                            continue
                        
                        nc_str = item.get('nakupna_cena')
                        mp_str = item.get('marza_percento')
                        
                        try:
                            obj.nakupna_cena = Decimal(str(nc_str).replace(' ', '').replace(',', '.')) if nc_str and str(nc_str).strip() != '' else Decimal('0.00')
                            obj.marza_percento = Decimal(str(mp_str).replace(' ', '').replace(',', '.')) if mp_str and str(mp_str).strip() != '' else Decimal('0.00')
                            
                            if obj.nakupna_cena > Decimal('0.00'):
                                obj.predajna_cena = (obj.nakupna_cena * (Decimal('1.00') + (obj.marza_percento / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            else:
                                obj.predajna_cena = Decimal('0.00')
                        except InvalidOperation:
                            raise ValueError("❌ Niektoré zadané ceny majú neplatný formát. Použite čísla.")
                            
                        if hasattr(obj, 'posledna_aktualizacia'):
                            obj.posledna_aktualizacia = timezone.now()
                            
                        obj.save()
                        
                        updated_items_data.append({
                            'id': obj.id,
                            'predajna_cena': self.predajna_cena_formatovana(obj),
                            'cisty_zisk': self.get_cisty_zisk(obj),
                            'skutocna_marza': self.get_skutocna_marza(obj),
                            'nakupna_cena_val': f"{obj.nakupna_cena:.2f}",
                            'marza_val': f"{obj.marza_percento:.2f}"
                        })
                
                count = len(updated_items_data)
                word = "položku" if count == 1 else "položky" if count < 5 else "položiek"
                msg = f"💾 Zmeny boli úspešne uložené pre {count} {word}!"
                return JsonResponse({"status": "success", "message": msg, "updated_items": updated_items_data})

            # Fallback na jadro pre iné akcie
            return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

        except ValueError as ve:
            return JsonResponse({"status": "error", "message": str(ve)}, status=400)
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"❌ Chyba pri ukladaní: {str(e)}"}, status=400)

    def has_add_permission(self, request): return False

    def has_delete_permission(self, request, obj=None): return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        valid_ct_ids = [ct.id for ct in ContentType.objects.all() if ct.model_class() is not None]
        return qs.filter(content_type_id__in=valid_ct_ids).select_related('content_type').prefetch_related('polozka_zo_skladu')

    @admin.display(description='📦 Kategória', ordering='content_type')
    def get_kategoria(self, obj):
        if not obj.content_type: 
            return ""
        
        nazov = get_kategoria_nazov(obj.content_type.model_class())
        return render_isas_badge(text=nazov.upper(), type_color="neutral", wrapper=True)

    @admin.display(description='🏷️ Identifikácia (Názov / SKU / EAN)')
    def get_nazov_polozky(self, obj):
        if not obj.polozka_zo_skladu: 
            return ""

        cisty_nazov = ocisti_nazov_od_jednotky(obj.polozka_zo_skladu)
        sku = escape(getattr(obj.polozka_zo_skladu, 'katalogove_cislo', ''))
        ean = escape(getattr(obj.polozka_zo_skladu, 'ean_kod', ''))
    
        metadata = []
        if sku: 
            metadata.append(format_html('🏷️ SKU: <b style="color: #0f172a;">{}</b>', sku))
        if ean: 
            metadata.append(format_html('🦓 EAN: <b style="color: #0f172a;">{}</b>', ean))

        return render_isas_entity_card(
            icon="🔧", 
            title=cisty_nazov, 
            metadata_list=metadata
        )

    @admin.display(description='⚖️ Jednotka & DPH')
    def get_merna_jednotka_vizual(self, obj):
        if obj.polozka_zo_skladu:
            jednotka = getattr(obj.polozka_zo_skladu, 'merna_jednotka', 'ks')
            dph = getattr(obj.polozka_zo_skladu, 'sadzba_dph', None)
            if dph is None:
                dph = get_default_dph()
        
            badge_jednotka = render_isas_badge(f"1 {jednotka}", icon="📦", type_color="purple", wrapper=False)
            badge_dph = render_isas_badge(f"DPH {dph} %", icon="🏛️", type_color="neutral", wrapper=False)
        
            return render_isas_layout(format_html('{}{}', badge_jednotka, badge_dph), layout="col", gap=4, center=True)
        
        return render_isas_text("—", weight=700, color="#cbd5e1")

    @admin.display(description='⏱️ Aktualizácia', ordering='posledna_aktualizacia')
    def get_aktualizacia(self, obj):
        if not obj.posledna_aktualizacia: 
            return ""
        
        datum = obj.posledna_aktualizacia.strftime('%d.%m.%Y')
        cas = obj.posledna_aktualizacia.strftime('%H:%M')
    
        badge_datum = render_isas_badge(datum, icon="📅", type_color="neutral", wrapper=False)
        badge_cas = render_isas_badge(cas, icon="🕒", type_color="neutral", wrapper=False)
    
        return render_isas_layout(format_html('{}{}', badge_datum, badge_cas), layout="col", gap=4, center=True)

    @admin.display(description='📊 Marža (%)')
    def get_skutocna_marza(self, obj):
        if obj.predajna_cena and obj.predajna_cena > Decimal('0.00') and obj.nakupna_cena is not None:
            zisk = obj.predajna_cena - obj.nakupna_cena
            skutocna_marza = (zisk / obj.predajna_cena) * Decimal('100.00')
        
            if skutocna_marza >= Decimal('30.00'):
                type_color = "success"
            elif skutocna_marza >= Decimal('15.00'):
                type_color = "info"
            else:
                type_color = "danger"
            
            return render_isas_badge(f"{skutocna_marza:.2f} %", icon="📈", type_color=type_color, wrapper=True)
        
        return render_isas_text("—", weight=700, color="#cbd5e1")

    @admin.display(description='💰 Predajná cena', ordering='predajna_cena')
    def predajna_cena_formatovana(self, obj):
        jednotka = getattr(obj.polozka_zo_skladu, 'merna_jednotka', 'ks') if obj.polozka_zo_skladu else 'ks'
    
        if obj.predajna_cena and obj.predajna_cena > Decimal('0.00'):
            cena_str = f"{obj.predajna_cena:.2f}"
            suffix = render_isas_text(f"/ {jednotka}", size=11, weight=800, custom_style="opacity:0.9; background: rgba(0,0,0,0.2); padding: 3px 8px; border-radius: 12px;")
        
            return render_isas_badge(
                text=f"{cena_str} {get_firemna_mena()}",
                icon="💎",
                wrapper=True,
                custom_bg="linear-gradient(135deg, #10b981 0%, #047857 100%)",
                custom_text="white",
                custom_border="#065f46",
                custom_shadow="rgba(16, 185, 129, 0.35)",
                suffix_html=suffix
            )
    
        return render_isas_badge("Doplniť", icon="⚠️", type_color="danger", wrapper=True)

    @admin.display(description='📈 Čistý zisk')
    def get_cisty_zisk(self, obj):
        if obj.predajna_cena and obj.predajna_cena > Decimal('0.00') and obj.nakupna_cena is not None:
            zisk = obj.predajna_cena - obj.nakupna_cena
            marza = obj.marza_percento or Decimal('0.00')
        
            if zisk < Decimal('0.00'):
                type_color = "danger"
                prefix = ""
                ikona = "📉"
            else:
                prefix = "+ "
                ikona = "🚀"
                if marza >= Decimal('30.00'):
                    type_color = "success"
                elif marza >= Decimal('15.00'):
                    type_color = "info"
                else:
                    type_color = "warning"
            
            zisk_str = f"{zisk:.2f}"
            return render_isas_badge(f"{prefix}{zisk_str} {get_firemna_mena()}", icon=ikona, type_color=type_color, wrapper=True)
        
        return render_isas_text("—", weight=700, color="#cbd5e1")

class KatalogDielovFilter(admin.SimpleListFilter):
    title = '🛠️ Technický portál / Katalóg'
    parameter_name = 'katalog_stav'

    def lookups(self, request, model_admin):
        return (
            ('ma', '🛠️ Má katalóg'),
            ('nema', '❌ Nemá katalóg'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'ma':
            return queryset.exclude(Q(katalog_dielov_url__exact='') | Q(katalog_dielov_url__isnull=True))
        if self.value() == 'nema':
            return queryset.filter(Q(katalog_dielov_url__exact='') | Q(katalog_dielov_url__isnull=True))
        return queryset

class KrajinaPovoduFilter(admin.SimpleListFilter):
    title = '🌍 Krajina pôvodu'
    parameter_name = 'krajina'

    def lookups(self, request, model_admin):
        # Vytiahne len tie krajiny, ktoré sú reálne vyplnené v databáze
        krajiny = model_admin.model.objects.exclude(
            Q(krajina_povodu__exact='') | Q(krajina_povodu__isnull=True)
        ).values_list('krajina_povodu', flat=True).distinct()
        
        return [(k, f"🌍 {k}") for k in sorted(krajiny)]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(krajina_povodu=self.value())
        return queryset

@admin.register(DatabazaVyrobcov)
class DatabazaVyrobcovAdmin(ISASPremiumAdmin):
    list_display = (
        'get_kod', 
        'get_nazov_vizual', 
        'get_krajina_a_typ', 
        'get_web_a_katalog', 
        'get_poznamka_status',
        'get_web_status'
    )
    list_display_links = ('get_nazov_vizual',)
    search_fields = ('nazov_vyrobcu', 'domena', 'krajina_povodu')    
    list_filter = (
        'typ_vyroby', 
        KrajinaPovoduFilter, 
        ISASWebstrankaFilter, 
        KatalogDielovFilter
    )

    fieldsets = (
        ('🏭 ZÁKLADNÝ PROFIL VÝROBCU', {
            'fields': (
                'nazov_vyrobcu', 
                'krajina_povodu',
                'typ_vyroby'
            ),
            'description': 'Základná identifikácia značky. 🌍 Krajina pôvodu a typ výroby pomáhajú technikom a manažérom pri naceňovaní a určovaní kvality dielov. 💎'
        }),
        ('🌐 ODKAZY A PORTÁLY', {
            'fields': (
                'domena', 
                'katalog_dielov_url'
            ),
            'description': '🔗 Rýchle odkazy. Doména slúži pre marketingový web, zatiaľ čo Katalóg dielov by mal smerovať priamo na B2B portál alebo rozpadové výkresy (Explosion drawings) pre technikov. 🛠️'
        }),
        ('📝 INTERNÉ KNOW-HOW', {
            'fields': (
                'technicka_poznamka',
            ),
            'description': '🧠 Špecifické technické znalosti o tejto značke (napr. "Používajú palcové závity", "Sériové čísla sú vždy pod krytom"). Tieto informácie uvidia technici pri servise. 🤫'
        }),
        ('🌐 WEB BUILDER (Verejná prezentácia)', {
            'fields': (
                'zobrazit_na_webe',
                'logo_na_webe',
            ),
            'description': '🌍 Nastavenia pre zobrazenie tejto značky v sekcii "Značky, ktoré servisujeme" na vašom verejnom webe. Nahrajte logo s priehľadným pozadím (PNG) pre najlepší vizuálny výsledok. 🖼️'
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "🏭 Databáza výrobcov"
    isas_preloader_text = "⏳ Vstupujete do databázy výrobcov..."
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🏭",
    }

    # --- 🪝 HOOKS PRE DETECTIVE SEARCH ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(nazov_vyrobcu__icontains=term) | 
            Q(domena__icontains=term) |
            Q(krajina_povodu__icontains=term)
        ).order_by('nazov_vyrobcu')[:30]

    def format_detective_result(self, obj):
        safe_name = obj.nazov_vyrobcu
        krajina = f"🌍 {obj.krajina_povodu}" if obj.krajina_povodu else "Neznáma krajina"
        return {
            'id': obj.id, 
            'name': safe_name, 
            'search_value': safe_name, 
            'rola': krajina,
            'cat': '🏭 VÝROBCA'
        }

    @admin.display(description='Kód', ordering='id')
    def get_kod(self, obj):
        kod_cislo = str(obj.id).zfill(3)
        return render_isas_badge(f"VYR-{kod_cislo}", type_color="neutral", monospace=True, wrapper=True)

    @admin.display(description='🏭 Názov Výrobcu', ordering='nazov_vyrobcu')
    def get_nazov_vizual(self, obj):
        image_url = None
        if obj.domena:
            clean_domain = str(obj.domena).lower().strip()
            for prefix in ['https://', 'http://', 'www.']:
                if clean_domain.startswith(prefix):
                    clean_domain = clean_domain[len(prefix):]
        
            clean_domain = clean_domain.split('/')[0].split('?')[0].split('#')[0]
        
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
            
            if '.' in clean_domain and len(clean_domain) > 3:
                image_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"

        return render_isas_avatar(
            icon="🏭",
            title=obj.nazov_vyrobcu,
            subtitle=None,
            bg_gradient="#f8fafc, #e2e8f0",
            shadow_color="rgba(0,0,0,0.05)",
            image_url=image_url
        )

    @admin.display(description='🌐 Odkazy a Portály')
    def get_web_a_katalog(self, obj):
        url_upravy = reverse('admin:Evidencia_databazavyrobcov_change', args=[obj.id])
    
        if obj.domena:
            url = obj.domena if obj.domena.startswith(('http://', 'https://')) else f"http://{obj.domena}"
            btn_web = render_isas_link_btn(url, "Oficiálny Web", icon="🌍", type_color="info", wrapper=False)
        else:
            btn_web = render_isas_link_btn(url_upravy, "Pridať Web", icon="➕", type_color="dashed", wrapper=False)

        if obj.katalog_dielov_url:
            url_kat = obj.katalog_dielov_url if obj.katalog_dielov_url.startswith(('http://', 'https://')) else f"http://{obj.katalog_dielov_url}"
            btn_kat = render_isas_link_btn(url_kat, "Katalóg Dielov", icon="🛠️", type_color="purple", wrapper=False)
        else:
            btn_kat = render_isas_link_btn(url_upravy, "Pridať Katalóg", icon="➕", type_color="dashed", wrapper=False)

        return render_isas_layout(format_html('{}{}', btn_web, btn_kat), layout="col", gap=6, center=True)

    @admin.display(description='🌍 Pôvod a Typ', ordering='krajina_povodu')
    def get_krajina_a_typ(self, obj):
        items = []
    
        if obj.krajina_povodu:
            items.append(render_isas_text(obj.krajina_povodu, icon="🌍", weight=800, color="#0f172a"))
    
        if obj.typ_vyroby == 'O':
            items.append(render_isas_badge("IBA ORIGINÁLY (OEM)", icon="💎", type_color="info", wrapper=False))
        elif obj.typ_vyroby == 'N':
            items.append(render_isas_badge("IBA NÁHRADY", icon="🔄", type_color="warning", wrapper=False))
        else:
            items.append(render_isas_badge("ZMIEŠANÁ VÝROBA", icon="🔀", type_color="neutral", wrapper=False))

        return render_isas_layout(mark_safe("".join(items)), layout="col", gap=6, center=True)

    @admin.display(description='📝 Interné Know-How')
    def get_poznamka_status(self, obj):
        if obj.technicka_poznamka:
            badge = render_isas_badge("Obsahuje poznámku", icon="💡", type_color="warning", wrapper=False, tooltip=obj.technicka_poznamka)
            return render_isas_layout(badge, center=True, custom_style="cursor: help;")
    
        text = render_isas_text("🚫 Bez poznámky", weight=700, color="#cbd5e1")
        return render_isas_layout(text, center=True)

    @admin.display(description='🌐 Web Builder', ordering='zobrazit_na_webe')
    def get_web_status(self, obj):
        badges = []
        
        if obj.zobrazit_na_webe:
            badges.append(render_isas_badge("VEREJNÉ", icon="🌍", type_color="success", wrapper=False))
        else:
            badges.append(render_isas_badge("SKRYTÉ", icon="🔒", type_color="neutral", wrapper=False))
            
        if obj.logo_na_webe:
            badges.append(render_isas_badge("Logo OK", icon="🖼️", type_color="info", wrapper=False))
            
        return render_isas_layout(mark_safe("".join(badges)), layout="col", gap=4, center=True)

# 1. Pripravíme si prázdne slovníky pre bleskové vyhľadávanie pri kliknutí
SKLAD_INLINES_MAP = {}
AKTUALNE_INLINES_MAP = {}

for main_cat_name, parent_model_cls in PARENT_MODELS.items():
    if main_cat_name in CATEGORY_MAP:
        for subcat_name, child_model_cls in CATEGORY_MAP[main_cat_name].items():
            
            # --- Príprava Samotného šuflíka (Dieťa) ---
            @admin.display(description="")
            def render_viac_btn(self, obj):
                return mark_safe('<button type="button" class="btn-more-info">⚙️ Viac</button>')

            child_attrs = {
                'model': child_model_cls,
                'form': UniverzalnyDielForm,
                'extra': 0,
                'can_delete': True,
                'classes': ['isas-drawer'],
                'readonly_fields': ('slug', 'viac_btn'),
                'viac_btn': render_viac_btn,
            }
            
            # 🟢 DYNAMICKÉ FIXOVANIE PORADIA STĹPCOV (Bez straty ERP polí z mixinov!)
            all_fields = []
            for f in child_model_cls._meta.fields:
                # Preskočíme ne-editovateľné polia (napr. id) a cudzí kľúč na rodiča (aby Django nepadol)
                if not f.editable or f.auto_created:
                    continue
                if f.is_relation and f.related_model == parent_model_cls:
                    continue
                all_fields.append(f.name)
            
            # 1. Vynútime si náš začiatok tabuľky (Jednotka VŽDY pred Typom, Viac tlačidlo priamo zo servera)
            ordered_fields = ['seriove_cislo', 'pocet_na_sklade', 'merna_jednotka']
            if 'typ' in all_fields:
                ordered_fields.append('typ')
            ordered_fields.append('viac_btn')
                
            # 2. Pridáme všetky ostatné polia (ERP z mixinov, dodávateľ, výrobca, kto_som...)
            for f in all_fields:
                if f not in ordered_fields:
                    ordered_fields.append(f)
                    
            child_attrs['fields'] = ordered_fields
                
            autocomplete_fields = []
            if hasattr(child_model_cls, 'dodavatel'): autocomplete_fields.append('dodavatel')
            if hasattr(child_model_cls, 'vyrobca'): autocomplete_fields.append('vyrobca')
            if autocomplete_fields: child_attrs['autocomplete_fields'] = autocomplete_fields

            # 🟢 SENIOR FIX: Eliminácia N+1 dopytov – automatický prefetch všetkých cudzích kľúčov
            fk_fields_to_prefetch = [
                f.name for f in child_model_cls._meta.fields
                if f.is_relation and f.related_model != parent_model_cls
            ]
            
            def make_optimized_queryset(prefetch_list):
                def get_queryset(self, request):
                    qs = super(self.__class__, self).get_queryset(request)
                    return qs.select_related(*prefetch_list) if prefetch_list else qs
                return get_queryset

            child_attrs['get_queryset'] = make_optimized_queryset(fk_fields_to_prefetch)

            # Vygenerujeme triedu pre Dieťa (Sklad aj Aktuálne)
            ChildInlineClass = type(f"{child_model_cls.__name__}Inline", (nested_admin.NestedTabularInline,), child_attrs)
            ChildAktualneInlineClass = type(f"{child_model_cls.__name__}Aktualne", (FilterSkladomMixin, ChildInlineClass), {})
            
            model_key = child_model_cls.__name__.lower()
            
            # --- Príprava Rodiča, ktorý drží IBA TOTO JEDNO DIEŤA ---
            parent_attrs = {
                'model': parent_model_cls,
                'inlines': [ChildInlineClass], # <--- TU JE BEZPEČNOSŤ: Iba 1 kategória naraz
                'extra': 1,
                'can_delete': False,
                'max_num': 1
            }
            ParentInlineClass = type(f"{parent_model_cls.__name__}_{model_key}_Stacked", (nested_admin.NestedStackedInline,), parent_attrs)
            
            parent_aktualne_attrs = {
                'model': parent_model_cls,
                'inlines': [ChildAktualneInlineClass],
                'extra': 0,
                'can_delete': False,
                'max_num': 1
            }
            ParentAktualneInlineClass = type(f"{parent_model_cls.__name__}_{model_key}_AktualneStacked", (nested_admin.NestedStackedInline,), parent_aktualne_attrs)
            
            # Uložíme ich do pamäte pod ich malým názvom (napr. kľúč podkategórie skladu)
            SKLAD_INLINES_MAP[model_key] = ParentInlineClass
            AKTUALNE_INLINES_MAP[model_key] = ParentAktualneInlineClass

@admin.register(Sklad)
class SkladAdmin(ISASSingletonAdminMixin, ISASDynamicCategoryAjaxMixin, ISASPremiumAdmin, nested_admin.NestedModelAdmin):
    change_form_template = "admin/sklad_change_form.html"
    list_display = ('styled_sklad_card',)
    list_display_links = None

    class Media:
        js = ("admin/js/sklad_master.js",)

    isas_inject_sklad_dashboard_css = True
    
    @property
    def isas_sklad_config(self):
        return get_sklad_dynamic_js_config()

    isas_magia_kwargs = {
        'typ_zobrazenia': 'dashboard',
        'skryt_vyhladavanie': True,
        'skryt_akcie': True,
        'cisty_dashboard': True,
    }  

    # Vykreslí iba to, čo žiada URL. Ostatné chráni v DB!
    def get_inline_instances(self, request, obj=None):
        if not obj: return [] # Ak vytvárame nový sklad, nevykreslíme nič
            
        sub = request.GET.get('sub') # Napr. kľúč podkategórie skladu
        
        # Ak máme platný kód podkategórie, vytiahneme ho zo slovníka
        if sub and sub in SKLAD_INLINES_MAP:
            return [SKLAD_INLINES_MAP[sub](self.model, self.admin_site)]
            
        # Ak sme v Menu (Dashboard), nevrátime žiadne inlines
        return []

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = extra_context or {}
        sub = request.GET.get('sub')
        
        obj = self.get_object(request, object_id)
        sklad_nazov = f"Hlavný sklad ({obj.nazov})" if obj and obj.nazov and obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        
        extra_context['title'] = f"🏢 {sklad_nazov}"
        extra_context['sklad_nazov'] = sklad_nazov
        
        if sub or request.GET.get('kat'):
            extra_context['preloader_text'] = f"⏳ Vstupujete do databázy skladu {sklad_nazov}..."
        else:
            extra_context['preloader_text'] = f"⏳ Načítavam prehľad skladu {sklad_nazov}..."
        
        return super().change_view(request, object_id, form_url=form_url, extra_context=extra_context)

    def save_formset(self, request, form, formset, change):
        """
        Zachytáva akúkoľvek manuálnu zmenu počtu kusov priamo v inlines (v databáze skladu).
        Automaticky generuje StockMovement s typom 'INVENTURA'.
        Zoskupuje notifikácie: pri 1 úprave posiela detailnú, pri 2+ hromadnú zbernú notifikáciu.
        """
        instances = formset.save(commit=False)
        vytvorene_pohyby = []
        
        with transaction.atomic():
            for f in formset.forms:
                # Ak bol riadok označený na vymazanie, neriešime ho tu (rieši to tvoj AJAX na mazanie)
                if formset.can_delete and formset._should_delete_form(f):
                    continue

                instance = f.instance
            
                # Zaujímame sa len o formuláre, kde sa reálne niečo zmenilo, alebo sú úplne nové
                if f.has_changed() or not instance.pk:
                
                    # 🪞 ISAS ZRKADLENIE: Inteligentná synchronizácia podľa toho, čo používateľ reálne prepísal
                    if 'seriove_cislo' in f.changed_data and 'katalogove_cislo' not in f.changed_data:
                        instance.katalogove_cislo = instance.seriove_cislo
                    elif 'katalogove_cislo' in f.changed_data and 'seriove_cislo' not in f.changed_data:
                        instance.seriove_cislo = instance.katalogove_cislo
                    elif not instance.pk:
                        # Pre úplne nové záznamy
                        if instance.seriove_cislo and not instance.katalogove_cislo:
                            instance.katalogove_cislo = instance.seriove_cislo
                        elif instance.katalogove_cislo and not instance.seriove_cislo:
                            instance.seriove_cislo = instance.katalogove_cislo

                    # Skontrolujeme, či sa zmenil práve 'pocet_na_sklade' alebo ide o novú položku
                    if 'pocet_na_sklade' in f.changed_data or not instance.pk:
                    
                        # Získame starý a nový stav (defenzívne ošetrené voči explicitnému None v slovníkoch)
                        stary_stav = (f.initial.get('pocet_na_sklade') or 0) if f.initial else 0
                        novy_stav = (f.cleaned_data.get('pocet_na_sklade') or 0) if f.cleaned_data else 0
                        rozdiel = novy_stav - stary_stav

                        # Ak nastala reálna zmena množstva
                        if rozdiel != 0:
                            # 1. Zistíme kategóriu a podkategóriu dynamicky z modelu
                            cat_name, subcat_name = "Nezaradené", "Nezaradené"
                            for cat, subcats in CATEGORY_MAP.items():
                                for subcat, cls in subcats.items():
                                    if isinstance(instance, cls):
                                        cat_name, subcat_name = cat, subcat
                                        break
                                if cat_name != "Nezaradené":
                                    break  # Prerušíme aj vonkajší cyklus, ak sme už našli zhodu

                            # 2. Uložíme inštanciu, aby sme mali istotu, že má PK (ak je nová)
                            instance.save()
                        
                            # 3. Vytvoríme záznam o pohybe
                            action = StockMovement.ADD if rozdiel > 0 else StockMovement.REMOVE
                            cisty_nazov = ocisti_nazov_od_jednotky(str(instance))
                        
                            movement = StockMovement.objects.create(
                                filialka=None, # Sme v hlavnom sklade
                                user=request.user,
                                category=cat_name,
                                subcategory=subcat_name,
                                product_name=cisty_nazov,
                                count=abs(rozdiel),
                                action=action,
                                merna_jednotka=getattr(instance, 'merna_jednotka', 'ks'),
                                typ_dokladu_erp='INVENTURA' # 🟢 KĽÚČ: Priama úprava je vždy inventúra!
                            )
                        
                            # 4. Odložíme si dáta o pohybe do zoznamu pre agregátor notifikácií
                            vytvorene_pohyby.append({
                                'id': movement.id,
                                'url': f"/admin/Evidencia/stockmovement/?q={movement.id}",
                                'stary_stav': stary_stav,
                                'novy_stav': novy_stav
                            })

                # Uložíme inštanciu (pre prípady, že sa menil len názov, výrobca atď.)
                instance.save()
            
            # Uložíme ManyToMany polia, ak nejaké sú
            formset.save_m2m()

        # Ochrana pred spamom
        if vytvorene_pohyby:
            from .tasks import send_isas_stock_notifications_task, send_isas_bulk_stock_notifications_task

            if len(vytvorene_pohyby) == 1:
                # Ak bola zmenená len 1 položka -> Pošleme klasickú detailnú notifikáciu
                p = vytvorene_pohyby[0]
                def _dispatch_stock_single(item=p):
                    try:
                        send_isas_stock_notifications_task.delay(
                            item['id'], item['url'], item['stary_stav'], item['novy_stav']
                        )
                    except Exception as exc:
                        logger.warning(f"Zlyhalo odoslanie notifikácie skladu do Celery: {exc}")
                        messages.warning(request, "⚠️ Sklad bol upravený, ale notifikáciu sa nepodarilo odoslať (výpadok Celery/Redis).")
                transaction.on_commit(_dispatch_stock_single)
            else:
                # Ak bolo zmenených viacero položiek -> Pošleme JEDNU hromadnú notifikáciu!
                ids = [p['id'] for p in vytvorene_pohyby]
                def _dispatch_stock_bulk(bulk_ids=None):
                    if bulk_ids is None:
                        bulk_ids = list(ids)
                    try:
                        send_isas_bulk_stock_notifications_task.delay(bulk_ids)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo odoslanie hromadnej notifikácie skladu do Celery: {exc}")
                        messages.warning(request, "⚠️ Sklad bol upravený, ale hromadnú notifikáciu sa nepodarilo odoslať (výpadok Celery/Redis).")
                transaction.on_commit(_dispatch_stock_bulk)

    @admin.display(description="")
    def styled_sklad_card(self, obj):
        banner_scene = '<span class="worker-1">👨‍🔧</span><span class="device">🖥️<span class="spark">⚡</span></span><span class="worker-2">👨‍🔧</span><span class="van">🚐</span>'
        banner = render_isas_animated_banner("ISAS CORE", banner_scene)
        
        sklad_nazov = f"Hlavný sklad ({obj.nazov})" if obj and obj.nazov and obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        
        hero_card = render_isas_hero_dashboard_card(
            icon="🏭",
            title=sklad_nazov,
            subtitle="Centrálna databáza dielov, položiek a zariadení",
            status_text="SYSTÉM AKTÍVNY",
            button_text="VSTÚPTE DO SKLADU",
            button_url=f"{obj.pk}/change/",
            button_icon="🚪✨"
        )
        
        return format_html('{}{}', banner, hero_card)   

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<path:object_id>/ajax/save-custom/', self.admin_site.admin_view(self.save_ajax_view), name="save-custom"),
            path("ajax/delete-inline-final/", self.admin_site.admin_view(self.delete_inline_ajax), name="delete-inline-final"),
            path("add-stock/", self.admin_site.admin_view(self.add_stock_view), name="add-stock"),
            path("remove-stock/", self.admin_site.admin_view(self.remove_stock_view), name="remove-stock"),
        ]
        return custom_urls + urls
    
    def save_ajax_view(self, request, object_id):
        # Overenie oprávnení na zmenu skladu pred spustením logiky
        if not request.user.has_perm('Evidencia.change_sklad'):
            return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na úpravu skladu! 🔒"}, status=403)
            
        try:
            obj = self.get_object(request, object_id)
            if not obj:
                return JsonResponse({"status": "error", "message": "🔍 Sklad nebol nájdený! ❌"}, status=404)
            ModelForm = self.get_form(request, obj)

            form = ModelForm(request.POST, request.FILES, instance=obj)
        
            if not hasattr(form, 'save_m2m'):
                form.save_m2m = lambda: None

            formsets, _ = self._create_formsets(request, obj, change=True)

            if form.is_valid() and all(f.is_valid() for f in formsets):
                new_ids = {}
                updated_data = {} # Zoznam aktualizovaných hodnôt pre frontend
                with transaction.atomic():
                    self.save_model(request, obj, form, True)
                    form.save_m2m()
                    self.save_related(request, form, formsets, True)
                
                    for fs in formsets:
                        for f in fs.forms:
                            if f.instance and f.instance.pk:
                                new_ids[f.prefix] = f.instance.pk
                                # Pošleme späť zosynchronizované hodnoty
                                updated_data[f.prefix] = {
                                    'seriove_cislo': getattr(f.instance, 'seriove_cislo', ''),
                                    'katalogove_cislo': getattr(f.instance, 'katalogove_cislo', '')
                                }
                            
                return JsonResponse({"status": "ok", "new_ids": new_ids, "updated_data": updated_data})
            else:
                return JsonResponse({"status": "error", "message": "⚠️ Chyba validácie údajov! 📝"}, status=400)
        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)

    def delete_inline_ajax(self, request):
        if request.method != 'POST':
            return JsonResponse({"status": "error", "message": "Povolená je výhradne metóda POST."}, status=405)

        item_id = request.POST.get('item_id')
        model_name = request.POST.get('model_name')
        
        # 1. Whitelist povolených modelov a ich dynamické mapovanie (nezávislé na app_label)
        skladove_modely_map = {model_cls.__name__: model_cls for cat_group in CATEGORY_MAP.values() for model_cls in cat_group.values()}
        POVOLENE_MODELY = ['ServisovaneZariadenie'] + list(skladove_modely_map.keys())
                
        if model_name not in POVOLENE_MODELY:
            return JsonResponse({"status": "error", "message": "🚨 Pokus o narušenie integrity! Nepovolený model na mazanie. 🛡️"}, status=403)

        # 2. 🛡️ Granulárna kontrola oprávnení (Ochrana pred Privilege Escalation)
        if model_name == 'ServisovaneZariadenie':
            ma_pravo = request.user.has_perm('Evidencia.delete_servisovanezariadenie') or request.user.has_perm('Evidencia.change_zakaznik')
            if not ma_pravo:
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na vymazanie servisovaného zariadenia! 🔒"}, status=403)
        else:
            if not request.user.has_perm('Evidencia.change_sklad'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na úpravu skladu! 🔒"}, status=403)

        # 3. 🛡️ Ochrana pred IDOR a bezpečné mazanie
        if item_id and model_name:
            try:
                valid_item_id = int(item_id)
                Model = ServisovaneZariadenie if model_name == 'ServisovaneZariadenie' else skladove_modely_map.get(model_name)
                if not Model:
                    return JsonResponse({"status": "error", "message": "🔍 Model položky nebol nájdený. ❌"}, status=404)
                
                qs = Model.objects.all()
                # Ak sme na subdoméne zákazníka, overíme vlastníctvo záznamu
                if model_name == 'ServisovaneZariadenie' and getattr(request, 'is_subdomain', False) and getattr(request, 'tenant', None):
                    qs = qs.filter(zakaznik=request.tenant)

                try:
                    obj_to_delete = qs.get(pk=valid_item_id)
                    obj_to_delete.delete()
                    return JsonResponse({"status": "ok"})
                except Model.DoesNotExist:
                    return JsonResponse({"status": "error", "message": "🔍 Položka neexistuje alebo k nej nemáte prístup. ❌"}, status=404)
                    
            except (ValueError, TypeError):
                return JsonResponse({"status": "error", "message": "🔍 Neplatné ID položky! ❌"}, status=400)
            except Exception as e:
                return JsonResponse({"status": "error", "message": f"Kritická chyba: {str(e)}"}, status=500)
                
        return JsonResponse({"status": "error", "message": "⚠️ Chýbajúce dáta z formulára."}, status=400)

    def add_stock_view(self, request):
        if not request.user.has_perm('Evidencia.change_sklad'):
            return HttpResponseForbidden("🚫 Nemáte oprávnenie na úpravu skladu! 🔒")
        return self._stock_view(request, action="add")

    def remove_stock_view(self, request):
        if not request.user.has_perm('Evidencia.change_sklad'):
            return HttpResponseForbidden("🚫 Nemáte oprávnenie na úpravu skladu! 🔒")
        return self._stock_view(request, action="remove")

    def _stock_view(self, request, action):
        hlavny_sklad_obj = Sklad.objects.first()
        sklad_id = hlavny_sklad_obj.pk if hlavny_sklad_obj else 1
        nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad_obj.nazov})" if hlavny_sklad_obj and hlavny_sklad_obj.nazov and hlavny_sklad_obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        
        if request.method == "POST":
            form = UpdateStockForm(request.POST)
            if form.is_valid():
                category = form.cleaned_data["category"]
                subcategory = form.cleaned_data["subcategory"]
                product_id = form.cleaned_data["product"]
                count = form.cleaned_data["count"]
                filialka = form.cleaned_data.get("filialka")

                if category not in CATEGORY_MAP or subcategory not in CATEGORY_MAP[category]:
                    messages.error(request, "❌ Neplatná kategória alebo podkategória položky.")
                    return redirect(request.path)

                model_class = CATEGORY_MAP[category][subcategory]
                
                from .tasks import send_isas_stock_notifications_task

                with transaction.atomic():
                    
                    # 1. ZÁMOK PRE HLAVNÝ SKLAD (Zamkne konkrétny produkt v DB)
                    obj = get_object_or_404(model_class.objects.select_for_update(), pk=product_id)
                    
                    # ⚡ CPU OPTIMALIZÁCIA: Namiesto regexu použijeme priamo pole seriove_cislo
                    cisty_nazov_produktu = obj.seriove_cislo

                    # 2. ZÁMOK PRE FILIÁLKU (Ak sa robí pohyb na filiálke)
                    if filialka:
                        # 🔒 Zamkneme riadok Filiálky. Týmto zaručíme, že LedgerKalkulacka 
                        # nikdy neprečíta "starý" stav počas súbehu dvoch požiadaviek.
                        Filialka.objects.select_for_update().get(pk=filialka.pk)

                    if action == "add":
                        # 🟢 VÝHYBKA ADD
                        if filialka:
                            pass # Na filiálke len logujeme, nemeníme fyzický model
                        else:
                            obj.pocet_na_sklade = (obj.pocet_na_sklade or 0) + count
                            obj.save(update_fields=['pocet_na_sklade'])

                        movement = StockMovement.objects.create(
                            filialka=filialka,
                            user=request.user,
                            category=category,
                            subcategory=subcategory,
                            product_name=cisty_nazov_produktu,
                            action=StockMovement.ADD,
                            count=count,
                            merna_jednotka=getattr(obj, 'merna_jednotka', 'ks')
                        )
                        
                        url_detail = f"/admin/Evidencia/stockmovement/?q={movement.id}"
                        from .tasks import send_isas_stock_notifications_task
                        def _dispatch_stock_add(m_id=movement.id, url=url_detail):
                            try:
                                send_isas_stock_notifications_task.delay(m_id, url)
                            except Exception as exc:
                                logger.warning(f"Zlyhalo odoslanie notifikácie naskladnenia do Celery: {exc}")
                                messages.warning(request, "⚠️ Zásoba bola pridaná, ale notifikáciu sa nepodarilo odoslať (výpadok Celery/Redis).")
                        transaction.on_commit(_dispatch_stock_add)

                    elif action == "remove":
                        # 🟢 VÝHYBKA REMOVE
                        if filialka:
                            # Vďaka zámku na Filiálke je tento výpočet teraz 100% bezpečný
                            povodny_stav = LedgerKalkulacka.zisti_stav_polozky(filialka, cisty_nazov_produktu)
                            if povodny_stav < count:
                                messages.error(request, f"❌ Na filiálke {filialka.nazov} nie je dostatok kusov! 📉")
                                return redirect(request.path)
                        else:
                            # Vďaka zámku na produkte je toto 100% bezpečné
                            skladom_pocet = obj.pocet_na_sklade or 0
                            if skladom_pocet < count:
                                hlavny_sklad_obj = Sklad.objects.first()
                                dyn_nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad_obj.nazov})" if hlavny_sklad_obj and hlavny_sklad_obj.nazov and hlavny_sklad_obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
                                messages.error(request, f"❌ V sklade {dyn_nazov_hlavneho} nie je dostatok kusov! 📉")
                                return redirect(request.path)
                            
                            obj.pocet_na_sklade = skladom_pocet - count
                            obj.save(update_fields=['pocet_na_sklade'])
                        
                        movement = StockMovement.objects.create(
                            filialka=filialka,
                            user=request.user,
                            category=category,
                            subcategory=subcategory,
                            product_name=cisty_nazov_produktu,
                            action=StockMovement.REMOVE,
                            count=count,
                            merna_jednotka=getattr(obj, 'merna_jednotka', 'ks')
                        )
                        
                        url_detail = f"/admin/Evidencia/stockmovement/?q={movement.id}"
                        from .tasks import send_isas_stock_notifications_task
                        def _dispatch_stock_remove(m_id=movement.id, url=url_detail):
                            try:
                                send_isas_stock_notifications_task.delay(m_id, url)
                            except Exception as exc:
                                logger.warning(f"Zlyhalo odoslanie notifikácie vyskladnenia do Celery: {exc}")
                                messages.warning(request, "⚠️ Zásoba bola odobratá, ale notifikáciu sa nepodarilo odoslať (výpadok Celery/Redis).")
                        transaction.on_commit(_dispatch_stock_remove)

                if filialka:
                    sklad_nazov = filialka.nazov
                else:
                    sklad_nazov = nazov_hlavneho
                    
                mj = getattr(obj, 'merna_jednotka', 'ks')
                messages.success(request, f"✅ Zásoba upravená pre {cisty_nazov_produktu} ({'+' if action=='add' else '-'}{count} {mj}) na: {sklad_nazov}. 📦")
                return redirect(f"/admin/Evidencia/sklad/{sklad_id}/change/")

        else:
            form = UpdateStockForm()

        return render(request, "admin/update_stock.html", {
            "form": form,
            "title": "📥 Pridať zásobu" if action == "add" else "📤 Odobrať zásobu",
            "sklad_id": sklad_id,
            "nazov_hlavneho_skladu": nazov_hlavneho,
        })

    def get_products(self, request):
        category = request.GET.get("category")
        subcategory = request.GET.get("subcategory")
        filialka_id = request.GET.get("filialka")
        
        if category and subcategory and category in CATEGORY_MAP and subcategory in CATEGORY_MAP[category]:
            model_class = CATEGORY_MAP[category][subcategory]
            
            stavy_na_filialke = {}
            if filialka_id and str(filialka_id).isdigit():
                filialka = Filialka.objects.filter(id=filialka_id).first()
                if filialka:
                    stavy_na_filialke = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(filialka)

            products = []
            for obj in model_class.objects.only('id', 'seriove_cislo', 'pocet_na_sklade', 'merna_jednotka'):
                # Namiesto regexu a str(obj) použijeme priamo pole seriove_cislo
                cisty_nazov = obj.seriove_cislo
                mj = getattr(obj, 'merna_jednotka', 'ks')
                
                # Výpočet zobrazeného stavu
                if filialka_id and str(filialka_id).isdigit():
                    pocet = stavy_na_filialke.get(cisty_nazov, 0)
                else:
                    pocet = obj.pocet_na_sklade
                    
                products.append({"id": obj.pk, "label": f"{obj.seriove_cislo} ({pocet} {mj})"})
                
            return JsonResponse({"products": products})
        return JsonResponse({"products": []})

class SkladFilialkaFilter(admin.SimpleListFilter):
    title = '🏢 Sklad / Filiálka'
    parameter_name = 'sklad_filialka'

    def lookups(self, request, model_admin):
        # Dynamické načítanie názvu hlavného skladu
        hlavny_sklad = Sklad.objects.first()
        nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        lookups_data = [('hlavny', f'🏭 {nazov_hlavneho}')]
        
        # Dynamicky ťaháme aktívne filiálky
        filialky = Filialka.objects.filter(je_aktivna=True).order_by('nazov')
        for f in filialky:
            lookups_data.append((str(f.id), f'🏪 {f.nazov}'))
            
        return lookups_data

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'hlavny':
            return queryset.filter(filialka__isnull=True)
        elif val:
            return queryset.filter(filialka_id=val)
        return queryset


class KategoriaFilter(admin.SimpleListFilter):
    title = '📂 Kategória'
    parameter_name = 'category'

    def lookups(self, request, model_admin):
        
        db_categories = StockMovement.objects.exclude(category__exact='').values_list('category', flat=True).distinct()
        choices_dict = dict(CATEGORY_CHOICES)
        
        seen_labels = {}
        for cat in db_categories:
            if not cat: continue
            # Nájdeme kategórii jej reprezentáciu s emoji (ak existuje)
            label = choices_dict.get(cat, cat)
            
            # 💡 ZABRÁNENIE DUPLICITY: Ak sme tento vizuál s emoji už pridali, preskočíme ho.
            if label not in seen_labels:
                seen_labels[label] = cat
                
        # Prevedieme slovník naspäť na zoznam tuples pre Django admin
        lookups_data = [(db_val, label) for label, db_val in seen_labels.items()]
        return sorted(lookups_data, key=lambda x: x[1])

    def queryset(self, request, queryset):
        val = self.value()
        if val:
            choices_dict = dict(CATEGORY_CHOICES)
            
            # Zistíme si alternatívny názov (takže ak URL pošle bez emoji, nájdeme aj verziu s ním)
            label = choices_dict.get(val, val)
            
            # Vyhľadáme staré dáta (val) aj nové dáta (label) v jednom bleskovom kroku
            return queryset.filter(Q(category=val) | Q(category=label))
        return queryset


class PodkategoriaFilter(admin.SimpleListFilter):
    title = '📁 Podkategória'
    parameter_name = 'subcategory'

    def lookups(self, request, model_admin):
        db_subcategories = StockMovement.objects.exclude(subcategory__exact='').values_list('subcategory', flat=True).distinct()
        
        emoji_lookup = {}
        for main_cat, sub_dict in CATEGORY_MAP.items():
            for emoji_name in sub_dict.keys():
                emoji_lookup[emoji_name] = emoji_name
                # Vytvoríme mapovanie čistého textu na text s emoji
                clean_name = ''.join(c for c in emoji_name if c.isalpha() or c.isspace()).strip()
                emoji_lookup[clean_name] = emoji_name

        seen_labels = {}
        for sub in db_subcategories:
            if not sub: continue
            label = emoji_lookup.get(sub, sub)
            
            # 💡 ZABRÁNENIE DUPLICITY
            if label not in seen_labels:
                seen_labels[label] = sub
                
        lookups_data = [(db_val, label) for label, db_val in seen_labels.items()]
        return sorted(lookups_data, key=lambda x: x[1])

    def queryset(self, request, queryset):
        val = self.value()
        if val:
            emoji_lookup = {}
            for main_cat, sub_dict in CATEGORY_MAP.items():
                for emoji_name in sub_dict.keys():
                    emoji_lookup[emoji_name] = emoji_name
                    clean_name = ''.join(c for c in emoji_name if c.isalpha() or c.isspace()).strip()
                    emoji_lookup[clean_name] = emoji_name
            
            # Opäť zistíme alternatívny názov s emoji
            label = emoji_lookup.get(val, val)
            
            # Hľadá historické záznamy aj tie súčasné!
            return queryset.filter(Q(subcategory=val) | Q(subcategory=label))
        return queryset

@admin.register(StockMovement)
class StockMovementAdmin(ISASDynamicCategoryAjaxMixin, ISASPremiumAdmin):
    isas_title = "🔄 Pohyby na skladoch"
    isas_preloader_text = "⏳ Vstupujete do evidencie pohybov na sklade..."
    change_list_template = "admin/stockmovement_changelist.html"

    list_display = (
        "styled_typ_dokladu_erp",
        "formatted_timestamp", 
        "user_badge", 
        "styled_category", 
        "styled_subcategory", 
        "styled_product",
        "styled_filialka", 
        "action_badge", 
        "formatted_count", 
        "formatted_price"
    )    
    list_filter = ('typ_dokladu_erp', 'action', KategoriaFilter, PodkategoriaFilter, SkladFilialkaFilter, ISASRelatedUserFilter, ISASDateRangeFilter)
    isas_date_filter_field = 'timestamp'
    search_fields = ("product_name", "user__username", "subcategory", "category", "faktura__variabilny_symbol")

    def has_add_permission(self, request):
        return False

    isas_global_export_modal = {
        'trigger_id': 'btn-export-sklad',
        'title': '📊 Ocenenie skladov',
        'text': 'Chcete vygenerovať aktuálny finančný report zásob (Hlavný sklad + Filiálky) pre účtovníctvo?',
        'generate_action': 'generate_sklad_excel',
        'poll_action': 'check_megazaloha_status',
        'send_action': 'send_sklad_excel'
    }

    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "📦",
        'custom_buttons_html': '<button type="button" id="btn-export-sklad" class="isas-action-btn btn-orange">📊 OCENENIE SKLADOV</button>',
        'ignore_default_action_ids': ['btn-export-sklad'],
    }

    def changelist_view(self, request, extra_context=None):
        request._isas_price_cache = {}
        extra_context = extra_context or {}
        extra_context['notifs_enabled'] = cache.get('isas_stock_notifications', True)
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("obratovost/", self.admin_site.admin_view(self.obratovost_view), name="stockmovement_obratovost"),
            path("ajax/get-chart-data/", self.admin_site.admin_view(self.get_chart_data), name="get_chart_data"),
            path("toggle-notifications/", self.admin_site.admin_view(self.toggle_notifications_api), name="toggle_notifications"),
        ]
        return custom_urls + urls

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            if not request.user.has_perm('Evidencia.delete_stockmovement'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na vymazanie skladových pohybov."}, status=403)
            try:
                with transaction.atomic():
                    locked_qs = StockMovement.objects.select_for_update(of=('self',)).filter(id__in=valid_ids)
                    vymazane_pocet, _ = locked_qs.delete()
            except Exception as e:
                return JsonResponse({"status": "error", "message": f"❌ Odstránenie zlyhalo kvôli súbehu v databáze: {e}"}, status=409)

            word = "pohyb" if vymazane_pocet == 1 else "pohybov"
            return JsonResponse({"status": "success", "message": f"✨ Úspešne odstránené {vymazane_pocet} {word} zo zoznamu!"})

        elif action_type == 'generate_sklad_excel':
            if not request.user.has_perm('Evidencia.view_sklad'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na export dát skladu."}, status=403)
            
            zaloha = MegaZaloha.objects.create(
                nazov_zalohy="Finančné ocenenie skladov",
                typ_zalohy='skladove_pohyby',
                stav_spracovania='spracovava_sa',
                vyziadal_uzivatel=request.user,
                poznamka="Generuje sa na pozadí..."
            )
            
            from .tasks import task_vygeneruj_ocenenie_skladu_excel
            def _dispatch_ocenenie(u_id=request.user.id, z_id=zaloha.id):
                try:
                    task_vygeneruj_ocenenie_skladu_excel.delay(u_id, z_id)
                except Exception as exc:
                    logger.warning(f"Zlyhalo odoslanie úlohy ocenenia skladu do Celery: {exc}")
                    messages.warning(request, "⚠️ Generovanie ocenenia skladu zlyhalo pri zaradení do fronty (výpadok Celery/Redis).")
            transaction.on_commit(_dispatch_ocenenie)
            return JsonResponse({"status": "processing", "zaloha_id": zaloha.id})
        
        elif action_type == 'send_sklad_excel':
            if not request.user.has_perm('Evidencia.view_sklad'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na odosielanie dát."}, status=403)

            try:
                data = json.loads(request.body) if request.body else {}
            except Exception:
                data = {}
            zaloha_id = request.POST.get('zaloha_id') or data.get('zaloha_id')
            if not zaloha_id:
                return JsonResponse({"status": "error", "message": "Chýba identifikátor zálohy."}, status=400)

            from .tasks import task_odoslat_ocenenie_skladu_uctovnicke
            try:
                task_odoslat_ocenenie_skladu_uctovnicke.delay(zaloha_id)
            except Exception as exc:
                logger.warning(f"Zlyhalo odoslanie ocenenia skladu účtovníčke do Celery: {exc}")
                messages.warning(request, "⚠️ Úlohu na odoslanie ocenenia skladu sa nepodarilo zaradiť do fronty kvôli výpadku Celery/Redis.")
                return JsonResponse({"status": "error", "message": "⚠️ Úlohu na odoslanie sa nepodarilo zaradiť do fronty kvôli výpadku Celery/Redis."}, status=503)
            return JsonResponse({"status": "success", "message": "📧 Požiadavka na odoslanie bola úspešne zaradená do fronty."})

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    # --- 🪝 HOOKS PRE DETECTIVE SEARCH ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('user').filter(
            Q(product_name__icontains=term) |
            Q(user__username__icontains=term) |
            Q(subcategory__icontains=term) |
            Q(category__icontains=term)
        ).order_by('-timestamp')[:30]

    def format_detective_result(self, obj):
        cat_name = str(obj.subcategory).upper() if obj.subcategory else "POHYB"
        cisty_nazov = ocisti_nazov_od_jednotky(obj.product_name)
        datum_pohybu = obj.timestamp.strftime("%d.%m.%Y %H:%M") if obj.timestamp else "Neznámy čas"
        
        safe_name = cisty_nazov
        akcia_text = "📥 PRIDANIE" if obj.action == 'ADD' else "📤 ODOBRATIE"
        user_name = obj.user.username if obj.user else "AUTO-SYSTÉM"
        
        return {
            'id': obj.id, 
            'name': safe_name, 
            'search_value': safe_name, 
            'rola': f"👤 {user_name} | {akcia_text}",
            'date': datum_pohybu,
            'cat': cat_name
        }

    def save_model(self, request, obj, form, change):
        if not getattr(obj, 'user', None):
            obj.user = request.user
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # ISAS ENGINE: Eliminácia N+1 dotazov pre používateľa, sklad a menu dokladu
        return qs.select_related('user', 'filialka', 'faktura')

    @admin.display(description="🕒 Dátum a čas", ordering="timestamp")
    def formatted_timestamp(self, obj):
        if obj.timestamp:
            text_html = render_isas_text(
                text=obj.timestamp.strftime("%d. %m. %Y %H:%M"),
                icon="🕒",
                size=13,
                weight=700,
                color="#64748b"
            )
            return render_isas_layout(text_html, center=True)
        return ""

    @admin.display(description="👤 Používateľ", ordering="user")
    def user_badge(self, obj):
        if obj.user:
            name = obj.user.get_full_name() or obj.user.username
            return render_isas_badge(
                text=name,
                icon="👤",
                type_color="purple"
            )
        return render_isas_badge(
            text="AUTO-SYSTÉM",
            icon="🤖",
            type_color="neutral"
        )

    @admin.display(description="📂 Kategória", ordering="category")
    def styled_category(self, obj):
        
        choices_dict = dict(CATEGORY_CHOICES)
        kategoria_text = choices_dict.get(obj.category, obj.category) if obj.category else "Nezaradené"

        return render_isas_badge(
            text=kategoria_text,
            type_color="neutral"
        )

    @admin.display(description="📁 Podkategória", ordering="subcategory")
    def styled_subcategory(self, obj):
        
        kategoria_db = str(obj.category) if obj.category else ""
        podkategoria_text = str(obj.subcategory) if obj.subcategory else "Nezaradené"
        
        color_themes = [
            ('#eff6ff', '#1e40af', '#bfdbfe'),
            ('#ecfdf5', '#065f46', '#a7f3d0'),
            ('#fef2f2', '#991b1b', '#fecaca'),
            ('#faf5ff', '#581c87', '#e9d5ff'),
            ('#fff7ed', '#9a3412', '#fed7aa'),
            ('#f0fdfa', '#0f766e', '#99f6e4'),
        ]
        
        bg_color, text_color, border_color = '#f8fafc', '#475569', '#e2e8f0'
        
        if kategoria_db:
            keys = [k[0] for k in CATEGORY_CHOICES]
            if kategoria_db in keys:
                theme_idx = keys.index(kategoria_db) % len(color_themes)
                bg_color, text_color, border_color = color_themes[theme_idx]

        emoji_text = podkategoria_text
        if kategoria_db in CATEGORY_MAP:
            for emoji_key in CATEGORY_MAP[kategoria_db].keys():
                clean_key = ''.join(c for c in emoji_key if c.isalpha() or c.isspace()).strip()
                if podkategoria_text == clean_key or podkategoria_text == emoji_key:
                    emoji_text = emoji_key
                    break

        return render_isas_badge(
            text=emoji_text,
            custom_bg=bg_color,
            custom_text=text_color,
            custom_border=border_color
        )

    @admin.display(description="📦 Diel / Položka", ordering="product_name")
    def styled_product(self, obj):
        cisty_nazov = ocisti_nazov_od_jednotky(obj.product_name)
        return render_isas_badge(
            text=cisty_nazov,
            icon="📦",
            type_color="neutral",
            custom_bg="#ffffff"
        )

    @admin.display(description="⚡ Akcia", ordering="action")
    def action_badge(self, obj):
        if obj.action == StockMovement.ADD:
            return render_isas_badge(
                text="PRIDANIE",
                icon="📥",
                type_color="success",
                css_class="action-pulse-add"
            )
        else:
            return render_isas_badge(
                text="ODOBRATIE",
                icon="📤",
                type_color="danger",
                css_class="action-pulse-rem"
            )

    @admin.display(description="🔢 Množstvo", ordering="count")
    def formatted_count(self, obj):
        pocet = obj.count if obj.count else 0
        jednotka = getattr(obj, 'merna_jednotka', 'ks').upper()

        match = re.search(r'\([\d\.\,]+\s*([a-zA-Z2]+)\)$', str(obj.product_name or ''))
        if match:
            jednotka = match.group(1).upper()

        suffix = render_isas_text(jednotka, size=10, weight=900, color="#64748b", custom_style="text-transform: uppercase; letter-spacing: 0.5px;")
        
        return render_isas_badge(
            text=str(pocet),
            suffix_html=suffix,
            type_color="neutral"
        )

    @admin.display(description="💰 Nákupná cena")
    def formatted_price(self, obj):        
        try:
            base_name = ocisti_nazov_od_jednotky(obj.product_name)
            unit_price = None
            
            request = None
            for frame_record in inspect.stack():
                if 'request' in frame_record[0].f_locals:
                    request = frame_record[0].f_locals['request']
                    break

            if request and hasattr(request, '_isas_price_cache'):
                if base_name in request._isas_price_cache:
                    unit_price = request._isas_price_cache[base_name]
                else:
                    katalog_item = CenovyKatalog.objects.filter(search_name__icontains=base_name).first()
                    if not katalog_item:
                        katalog_item = CenovyKatalog.objects.filter(search_name=obj.product_name).first()
                    
                    unit_price = katalog_item.nakupna_cena if katalog_item else None
                    request._isas_price_cache[base_name] = unit_price
            else:
                katalog_item = CenovyKatalog.objects.filter(search_name__icontains=base_name).first()
                if not katalog_item:
                    katalog_item = CenovyKatalog.objects.filter(search_name=obj.product_name).first()
                unit_price = katalog_item.nakupna_cena if katalog_item else None
            
            if unit_price is not None:
                unit_price_dec = (unit_price or Decimal('0.00')).quantize(Decimal('0.01'))
                total_price = (unit_price_dec * Decimal(str(obj.count or 0))).quantize(Decimal('0.01'))
                mena_pohyb = getattr(obj.faktura, 'mena', None) or get_firemna_mena()
                
                total_badge = render_isas_badge(
                    text=f"{total_price:.2f} {mena_pohyb}",
                    custom_bg="linear-gradient(135deg, #1e293b 0%, #0f172a 100%)",
                    custom_text="#34d399",
                    custom_border="transparent",
                    custom_shadow="0 2px 4px rgba(0,0,0,0.15)",
                    wrapper=False
                )
                
                unit_text = render_isas_text(
                    text=f"{unit_price_dec:.2f} {mena_pohyb} / jedn.",
                    size=10,
                    weight=700,
                    muted=True
                )
                
                return render_isas_layout(format_html('{}{}', total_badge, unit_text), layout="col", gap=3, center=True)
        except Exception:
            pass
            
        return render_isas_badge(text="Nenájdené", type_color="dashed")

    @admin.display(description="🏢 Sklad / Filiálka", ordering="filialka")
    def styled_filialka(self, obj):
        if obj.filialka:
            return render_isas_badge(
                text=obj.filialka.nazov,
                icon="🏪",
                custom_bg="linear-gradient(135deg, #10b981 0%, #059669 100%)",
                custom_text="white",
                custom_border="transparent",
                custom_shadow="0 2px 4px rgba(16, 185, 129, 0.2)"
            )
            
        request = None
        for frame_record in inspect.stack():
            if 'request' in frame_record[0].f_locals:
                request = frame_record[0].f_locals['request']
                break

        if request:
            if not hasattr(request, '_isas_hlavny_sklad_nazov'):
                hlavny_sklad = Sklad.objects.first()
                request._isas_hlavny_sklad_nazov = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
            nazov_hlavneho = request._isas_hlavny_sklad_nazov
        else:
            hlavny_sklad = Sklad.objects.first()
            nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        
        return render_isas_badge(
            text=nazov_hlavneho,
            icon="🏭",
            type_color="neutral"
        )

    @admin.display(description="📑 Typ ERP dokladu", ordering="typ_dokladu_erp")
    def styled_typ_dokladu_erp(self, obj):
        typ = getattr(obj, 'typ_dokladu_erp', None)

        if not typ:
            return render_isas_badge(
                text="Nezaradené",
                icon="🚫",
                type_color="dashed"
            )

        full_text = str(obj.get_typ_dokladu_erp_display())
        
        if full_text and full_text[0] in '📥📤🔧🚚📋':
            ikona = full_text[0]
            text = full_text[1:].strip() 
        else:
            ikona = '📄'
            text = full_text

        if typ == 'SPOTREBA': 
            bg, text_c, border, shadow = 'linear-gradient(135deg, #faf5ff, #f3e8ff)', '#581c87', '#e9d5ff', '0 4px 6px -1px rgba(168, 85, 247, 0.15)'
        elif typ == 'PRIJEMKA': 
            bg, text_c, border, shadow = 'linear-gradient(135deg, #f0fdf4, #dcfce7)', '#14532d', '#bbf7d0', '0 4px 6px -1px rgba(34, 197, 94, 0.15)'
        elif typ == 'VYDAJKA': 
            bg, text_c, border, shadow = 'linear-gradient(135deg, #eff6ff, #dbeafe)', '#1e3a8a', '#bfdbfe', '0 4px 6px -1px rgba(59, 130, 246, 0.15)'
        elif typ == 'PREVODKA': 
            bg, text_c, border, shadow = 'linear-gradient(135deg, #fff7ed, #ffedd5)', '#7c2d12', '#fed7aa', '0 4px 6px -1px rgba(249, 115, 22, 0.15)'
        elif typ == 'INVENTURA': 
            bg, text_c, border, shadow = 'linear-gradient(135deg, #f0fdfa, #ccfbf1)', '#0f766e', '#99f6e4', '0 4px 6px -1px rgba(20, 184, 166, 0.15)'
        else: 
            bg, text_c, border, shadow = 'linear-gradient(135deg, #f8fafc, #f1f5f9)', '#334155', '#e2e8f0', '0 4px 6px -1px rgba(0, 0, 0, 0.05)'

        return render_isas_badge(
            text=text,
            icon=ikona,
            custom_bg=bg,
            custom_text=text_c,
            custom_border=border,
            custom_shadow=shadow
        )

    def toggle_notifications_api(self, request):
        if not request.user.has_perm('Evidencia.change_stockmovement') and not request.user.is_superuser:
            return JsonResponse({"status": "error", "message": "🚫 Zabezpečenie: Nemáte oprávnenie meniť nastavenia notifikácií."}, status=403)

        current_state = cache.get('isas_stock_notifications', True)
        new_state = not current_state
        cache.set('isas_stock_notifications', new_state, timeout=None) 
        
        stav_text = "ZAPNUTÉ" if new_state else "VYPNUTÉ"
        return JsonResponse({"status": "success", "is_enabled": new_state, "message": f"🔔 Notifikácie skladu boli úspešne {stav_text}."})

    def obratovost_view(self, request):        
        # 🟢 1. NAČÍTANIE NÁZVU HLAVNÉHO SKLADU
        hlavny_sklad = Sklad.objects.first()
        nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        
        # 🟢 2. NAČÍTANIE VŠETKÝCH AKTÍVNYCH FILIÁLOK
        filialky = Filialka.objects.filter(je_aktivna=True)

        # 🟢 3. PRIDANIE DO KONTEXTU PRE HTML ŠABLÓNU
        context = dict(
            self.admin_site.each_context(request),
            title="📊 Pohyby na sklade",
            nazov_hlavneho=nazov_hlavneho,
            filialky=filialky,
        )
        
        return render(request, "admin/stockmovement_obratovost.html", context)

    def get_chart_data(self, request):
        if not request.user.has_perm('Evidencia.view_stockmovement'):
            return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie prezerat štatistiky."}, status=403)

        try:
            days = max(1, min(int(request.GET.get("period", 30)), 3650))
        except (ValueError, TypeError):
            days = 30
            
        category = request.GET.get("category")
        subcategory = request.GET.get("subcategory")
        product_label = request.GET.get("product")

        start_date = timezone.now() - timedelta(days=days)
        qs = StockMovement.objects.filter(timestamp__gte=start_date)

        if category:
            qs = qs.filter(category=category)
        if subcategory:
            qs = qs.filter(subcategory=subcategory)
        if product_label:
            qs = qs.filter(product_name__icontains=product_label)

        grouped = {}
        for sm in qs:
            if not sm.timestamp:
                continue
            day = sm.timestamp.strftime("%Y-%m-%d")
            c = sm.count or 0
            grouped[day] = grouped.get(day, 0) + (c if sm.action == StockMovement.ADD else -c)

        labels = sorted(grouped.keys())
        values = [grouped[d] for d in labels]
        return JsonResponse({"labels": labels, "values": values})

class KorenProblemuFilter(admin.SimpleListFilter):
    """NOVÝ FILTER: Analytika reklamácií a ISO 9001."""
    title = '🔍 Koreň problému (Reklamácie)'
    parameter_name = 'koren_problemu'

    def lookups(self, request, model_admin):
        return [
            ('OPRAVNENE_VSETKY', format_html('{} {}', "🔴", "Všetky oprávnené (Naše náklady)")),
            ('NEOPRAVNENE_VSETKY', format_html('{} {}', "🟡", "Všetky neoprávnené (Fakturuje sa)")),
            ('NEHODNOTENE', format_html('{} {}', "➖", "Nehodnotené (Bežný servis)")),
            ('CHYBA_TECHNIKA', format_html('{} {}', "👨‍🔧", "Chyba montáže (Oprávnená)")),
            ('VADNY_DIEL', format_html('{} {}', "⚙️", "Vadný diel (Oprávnená)")),
            ('SOFTWARE', format_html('{} {}', "💻", "Softvérová chyba (Oprávnená)")),
            ('CHYBA_OBSLUHY', format_html('{} {}', "🤦‍♂️", "Chyba obsluhy (Neoprávnená)")),
            ('ZANEDBANA_UDRZBA', format_html('{} {}', "🧹", "Zanedbaná údržba (Neoprávnená)")),
            ('EXTERNY_VPLYV', format_html('{} {}', "⚡", "Externý vplyv (Neoprávnená)")),
            ('OPOTREBENIE', format_html('{} {}', "⏳", "Prirodzené opotrebenie (Neoprávnená)")),
        ]

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'OPRAVNENE_VSETKY':
            return queryset.filter(koren_problemu__in=getattr(ServisnyZaznam, 'KORENE_OPRAVNENE', []))
        elif val == 'NEOPRAVNENE_VSETKY':
            return queryset.filter(koren_problemu__in=getattr(ServisnyZaznam, 'KORENE_NEOPRAVNENE', []))
        elif val:
            return queryset.filter(koren_problemu=val)
        return queryset

@admin.register(ServisnyZaznam)
class ServisnyZaznamAdmin(ISASPremiumAdmin):    
    form = ServisnyZaznamAdminForm
    
    autocomplete_fields = ['technici', 'zakaznik', 'zariadenie', 'uloha']
    list_display = [
        'styled_nazov', 
        'styled_zakaznik', 
        'styled_zariadenie',
        'styled_report', 
        'styled_cas_a_stav', 
        'styled_technici', 
        'styled_protokol'
    ]
    list_display_links = ['styled_nazov']
    list_filter = [
        ISASDateRangeFilter, 
        ISASHasMaterialFilter, 
        ISASRelatedTypServisuFilter, 
        ISASRelatedTechnikFilter, 
        ISASRelatedZakaznikFilter, 
        ISASRelatedZariadenieFilter,
        KorenProblemuFilter
    ]
    isas_date_filter_field = 'zaciatok'
    isas_material_filter_field = 'pouzite_polozky_zaznamu'
    search_fields = ['zakaznik__Nazov_spolocnosti', 'poruchy']  
    readonly_fields = (
        'styled_protokol', 'cas_podpisu', 'meno_podpisujuceho', 
        'protokol_uzavrety', 'gps_suradnice', 'hodnotenie_zakaznika', 
        'uzavrety_pdf_dokument', 'bezpecnostny_hash', 'bezpecnostny_kluc',
        'vytvorene', 'upravene'
    )
    
    # 🔒 DYNAMICKÝ TVRDÝ ZÁMOK (S výnimkou pre Web Builder)
    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and getattr(obj, 'protokol_uzavrety', False):
            # 🌐 Polia verejného marketingu, ktoré môže vedenie upravovať aj po podpise protokolu
            POVOLENE_PRE_WEB = {'zobrazit_na_webe', 'verejny_popis_referencie', 'slug'}
            
            all_fields = [f.name for f in obj._meta.fields if f.name not in POVOLENE_PRE_WEB]
            m2m_fields = [f.name for f in obj._meta.many_to_many]
            custom_fields = ['styled_protokol']
            return list(set(readonly + all_fields + m2m_fields + custom_fields))
            
        return readonly

    fieldsets = (
        ('🏢 LOKALIZÁCIA A IDENTIFIKÁCIA ZÁSAHU', {
            'fields': (
                'uloha',
                'zakaznik',
                'zariadenie'
            ),
            'description': 'Prepojenie záznamu na konkrétneho zákazníka, zariadenie a prípadne plánovanú úlohu z kalendára. 📍'
        }),
        ('🛠️ ZADANIE PRÁCE A TÍM', {
            'fields': (
                'typ_servisu', 
                'technici'
            ),
            'description': 'Špecifikujte typ vykonanej práce a technikov, ktorí sa na zásahu podieľali. 👨‍🔧'
        }),
        ('⚙️ DÁTA ZARIADENIA A MERANIA', {
            'fields': (
                ('stav_motohodin', 'motohodiny_v_zatazi'),
                'zmena_hardveru',
                'namerane_hodnoty'
            ),
            'description': 'Aktuálne prevádzkové hodnoty zariadenia v čase servisu. ⏱️'
        }),
        ('📝 REPORT TECHNIKA A MULTIMÉDIA', {
            'fields': (
                'poruchy', 
                'foto_pred',
                'foto_po', 
                'video'
            ),
            'description': 'Detailný popis zistených porúch a vizuálna dokumentácia stavu. 📸'
        }),
        ('🌐 VEREJNÁ PREZENTÁCIA (WEB BUILDER)', {
            'fields': (
                'zobrazit_na_webe',
                'verejny_popis_referencie',
                'slug'
            ),
            'description': 'Nastavenie zobrazenia tohto servisného zásahu ako referencie (Before/After) na verejnej webstránke. 🌍'
        }),
        ('🔍 ANALÝZA KOREŇA PROBLÉMU (REKLAMÁCIE)', {
            'fields': (
                'koren_problemu',
            ),
            'description': (
                '⚠️ <b>Identifikácia systémovej chyby:</b> Tento blok vyplňte výhradne pri reklamačných '
                'alebo opakovane sa vyskytujúcich poruchách. Slúži na spätnú väzbu pre manažment kvality. 🎯'
            )
        }),
        ('🤖 ISAS AI DIAGNOSTIKA', {
            'fields': (
                'ai_analyza_historie', 
                'formalne_zhodnotenie_ai'
            ),
            'description': '<div style="color: #0ea5e9; font-size: 11px; font-weight: bold; margin-bottom: 10px;">Polia v tejto sekcii sú plne v réžii umelej inteligencie. Prepisujte ich len v krajnom prípade. 🧠</div>'
        }),
        ('✅ UZAVRETIE A INFO PRE VEDENIE', {
            'fields': (
                'dokoncene', 
                ('notif_admin_tg', 'notif_admin_mail', 'notif_admin_discord')
            ),
            'classes': ('vivid-box-flash',),
            'description': '🏁 <b>Ukončenie procesu:</b> Po označení záznamu ako dokončeného systém automaticky informuje vedenie. 📊'
        }),
        ('📄 ELEKTRONICKÝ PROTOKOL A NOTIFIKÁCIE', {
            'fields': (
                'styled_protokol',
                ('upozornit_majitela_protokol_tg', 'upozornit_majitela_protokol_mail', 'upozornit_majitela_protokol_discord'),
                ('meno_podpisujuceho', 'hodnotenie_zakaznika')
            ),
            'description': 'Správa elektronického protokolu a nastavenie notifikácií po jeho úspešnom podpísaní zákazníkom. ✍️'
        }),
        ('🔒 BEZPEČNOSŤ, PODPISY A KRYPTOGRAFIA', {
            'fields': (
                ('protokol_uzavrety', 'cas_podpisu'),
                ('gps_suradnice', 'bezpecnostny_hash'),
                'uzavrety_pdf_dokument',
                ('vytvorene', 'upravene')
            ),
            'description': '<div style="color: #ef4444; font-size: 11px; font-weight: bold; margin-bottom: 10px;">Tieto údaje slúžia pre právnu ochranu firmy. Sú generované automaticky pri podpise klientom a sú uzamknuté. 🛡️</div>'
        }),
        ('🛠️ RAW DÁTA (Vývojárske polia)', {
            'fields': (
                'podpis_zakaznika', 
                'podpis_technika',
                'bezpecnostny_kluc'
            ),
        }),
    )    

    inlines = [PouzityMaterialVZaznamochInline]

    isas_title = "📋 Servisné záznamy"
    isas_preloader_text = "⏳ Vstupujete do zoznamu servisných záznamov..."
    
    isas_modern_toggles = True
    isas_ai_match_modal = True
    isas_ai_sepkar_field = 'zariadenie'
    isas_open_related_on_select = ['uloha']
    
    isas_action_modals = {
        'btn-complete-selected': {
            'type': 'confirm_checkbox_modal',
            'title': 'Dokončenie záznamov',
            'subtitle': 'Chystáte sa označiť záznam(y) ako dokončené. Chcete o tom informovať majiteľa?',
            'icon': '✅',
            'checkbox_label': 'Odoslať notifikácie majiteľovi 📱',
            'checkbox_sublabel': '(Po odškrtnutí políčka sa notifikácia neodošle)',
            'checkbox_id': 'send_notifs',
            'action': 'complete',
            'confirm_text': 'Vykonať',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jeden záznam zo zoznamu.'
        },
        'btn-send-protocol-selected': {
            'type': 'confirm_dialog',
            'title': 'Odoslanie protokolov',
            'subtitle': 'Chystáte sa odoslať skontrolované PDF protokoly zákazníkom na e-mail. Pokračovať?',
            'icon': '📧',
            'action': 'send_customer_protocol',
            'confirm_text': 'Odoslať zákazníkom',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jeden záznam zo zoznamu.'
        },
        'btn-reset-protocol-selected': {
            'type': 'confirm_dialog',
            'title': 'Resetovanie protokolu',
            'subtitle': 'Naozaj chcete zmazať vygenerované PDF, podpisy a digitálnu pečať? Záznam sa odomkne pre opätovné podpísanie.',
            'icon': '⚠️',
            'action': 'reset_protocol',
            'confirm_text': 'Resetovať a odomknúť',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jeden záznam zo zoznamu.'
        }
    }

    isas_protokol_poller = True

    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "🏢",
        'custom_buttons_html': (
            "<button type='button' id='btn-complete-selected' class='isas-action-btn btn-green'>✅ Označiť ako dokončené</button>"
            "<button type='button' id='btn-send-protocol-selected' class='isas-action-btn btn-blue'>📧 Odoslať PDF zákazníkovi</button>"
            "<button type='button' id='btn-reset-protocol-selected' class='isas-action-btn btn-orange'>🗑️ Resetovať protokol</button>"
        ),
        'ignore_default_action_ids': [
            "btn-complete-selected", 
            "btn-send-protocol-selected", 
            "btn-reset-protocol-selected"
        ]
    }

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        """
        🛡️ ŠPECIFICKÁ VALIDÁCIA PRE SERVISNÉ ZÁZNAMY:
        Zabráni vymazaniu záznamov s podpísaným a uzamknutým protokolom z frontendu.
        Neporušuje bázovú triedu ISASPremiumAdmin a volá super().
        """
        if action_type == 'delete':
            uzamknute = self.model.objects.filter(id__in=valid_ids, protokol_uzavrety=True).count()
            if uzamknute > 0:
                word = "záznam má" if uzamknute == 1 else ("záznamy majú" if 2 <= uzamknute <= 4 else "záznamov má")
                return JsonResponse({
                    "status": "error", 
                    "message": f"🚫 {uzamknute} {word} elektronicky uzamknutý a podpísaný protokol! Tieto záznamy nemožno vymazať. Najskôr ich musí konateľ resetovať."
                }, status=403)

        return super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)

    def has_delete_permission(self, request, obj=None):
        # 🛡️ DEFENSIVE: V administrácii skryjeme možnosť zmazať záznam, ak má podpísaný protokol
        if obj and getattr(obj, 'protokol_uzavrety', False):
            return False
        return super().has_delete_permission(request, obj)

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        """Hook pre vlastné API akcie. Mazanie (delete) je delegované na jadro."""
        payload_data = kwargs.get('data') or {}

        if action_type == 'complete':
            if not request.user.has_perm('Evidencia.change_servisnyzaznam'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať záznamy."}, status=403)
            
            send_notifs = payload_data.get('send_notifs', True)

            with transaction.atomic():
                # 🛡️ DEFENSIVE: Berieme iba tie záznamy, ktoré ešte NIE SÚ dokončené
                zaznamy = self.model.objects.select_for_update().select_related(
                    'zariadenie'
                ).prefetch_related(
                    'typ_servisu'
                ).filter(id__in=valid_ids, dokoncene=False).order_by('zariadenie_id', 'id')
                
                zaznamy_na_update = []
                for obj in zaznamy:
                    obj.dokoncene = True
                    zaznamy_na_update.append(obj)
                    obj.vygeneruj_prediktivne_plany()
                    
                    # 🚀 ISAS WEBHOOK FIX: bulk_update nevolá save(), musíme webhook odpáliť manuálne!
                    if obj.zakaznik_id:
                        from WebBuilder.tasks import process_servis_webhooks_task
                        def _dispatch_webhook(pk=obj.pk):
                            try:
                                process_servis_webhooks_task.delay(pk)
                            except Exception as exc:
                                logger.warning(f"Zlyhalo odoslanie webhooku servisu do Celery: {exc}")
                                messages.warning(request, f"⚠️ Webhook servisu pre záznam #{pk} sa nepodarilo zaradiť do fronty (výpadok Celery/Redis).")
                        transaction.on_commit(_dispatch_webhook)
                
                if zaznamy_na_update:
                    self.model.objects.bulk_update(zaznamy_na_update, ['dokoncene'])
                    
            count = len(zaznamy_na_update)
            if count == 0:
                return JsonResponse({"status": "error", "message": "⚠️ Vybrané záznamy už boli dokončené predtým."}, status=400)

            if send_notifs:
                from .tasks import zaznam_odoslat_notifikacie_adminovi_task
                uspesne_ids = [z.id for z in zaznamy_na_update]
                def _dispatch_notif_admin(u_ids=uspesne_ids):
                    try:
                        zaznam_odoslat_notifikacie_adminovi_task.delay(u_ids)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo odoslanie notifikácií adminovi do Celery: {exc}")
                        messages.warning(request, "⚠️ Záznamy boli dokončené, no notifikácie adminovi neboli odoslané (výpadok Celery/Redis).")
                transaction.on_commit(_dispatch_notif_admin)
                    
            word = "záznam" if count == 1 else ("záznamy" if 2 <= count <= 4 else "záznamov")
            stav_notif = "(S notifikáciou 📱)" if send_notifs else "(Tichý režim 🔕)"
            return JsonResponse({"status": "success", "message": f"✅ Úspešne označené {count} {word} ako dokončené {stav_notif}!"})

        # 📨 ISAS MÁGIA: Bleskové odoslanie skontrolovaného PDF zákazníkovi
        if action_type == 'send_customer_protocol':
            if not request.user.has_perm('Evidencia.change_servisnyzaznam'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie odosielať protokoly zákazníkom."}, status=403)

            from .tasks import odoslat_protokol_zakaznikovi_manualne_task
            zaznamy = self.model.objects.filter(
                id__in=valid_ids, 
                protokol_uzavrety=True, 
                uzavrety_pdf_dokument__isnull=False
            ).select_related('zakaznik')
            
            if not zaznamy.exists():
                return JsonResponse({"status": "error", "message": "⚠️ Žiaden z vybraných záznamov nemá uzamknutý protokol s hotovým PDF."}, status=400)

            odoslane = 0
            for z in zaznamy:
                def _dispatch_protokol(pk=z.pk):
                    try:
                        odoslat_protokol_zakaznikovi_manualne_task.delay(pk)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo odoslanie protokolu #{pk} do Celery: {exc}")
                        messages.warning(request, f"⚠️ Protokol #{pk} sa nepodarilo zaradiť na odoslanie (výpadok Celery/Redis).")
                transaction.on_commit(_dispatch_protokol)
                odoslane += 1

            word = "protokol" if odoslane == 1 else ("protokoly" if 2 <= odoslane <= 4 else "protokolov")
            return JsonResponse({"status": "success", "message": f"🚀 Úspešne zaradené na odoslanie {odoslane} {word} pre zákazníkov!"})

        # 🗑️ ISAS MÁGIA: Reset protokolu (Autorizované výhradne pre konateľa a superusera)
        if action_type == 'reset_protocol':
            je_konatel = request.user.is_superuser or getattr(getattr(request.user, 'zamestnanec', None), 'typ_pomeru', '') == 'KO'
            if not je_konatel:
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie resetovať protokoly. Táto funkcia je vyhradená výhradne pre konateľa."}, status=403)

            resetovane = 0

            # 🛡️ DEFENSIVE: Transakcia a riadkové zámky pred delegovaním do modelu
            with transaction.atomic():
                zaznamy = list(self.model.objects.select_for_update().filter(
                    id__in=valid_ids, 
                    protokol_uzavrety=True
                ).order_by('id'))

                if not zaznamy:
                    return JsonResponse({"status": "error", "message": "⚠️ Žiaden z vybraných záznamov nie je v stave uzamknutého protokolu."}, status=400)

                for z in zaznamy:
                    # Delegovanie do doménovej metódy modelu
                    z.anuluj_protokol(pouzivatel=request.user)
                    resetovane += 1

            word = "protokol" if resetovane == 1 else ("protokoly" if 2 <= resetovane <= 4 else "protokolov")
            return JsonResponse({"status": "success", "message": f"🔓 Úspešne resetovaných a odomknutých {resetovane} {word} pre opätovný podpis!"})

        # Fallback na jadro (vyrieši 'delete' a iné globálne akcie)
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    def get_detective_queryset(self, request, term):
        """Hook pre vyhľadávanie v Detective Search."""
        return self.get_queryset(request).select_related(
            'zakaznik', 'zariadenie'
        ).prefetch_related(
            'typ_servisu'
        ).filter(
            Q(zakaznik__Nazov_spolocnosti__icontains=term) |
            Q(zariadenie__nazov__icontains=term) |
            Q(zariadenie__vyrobne_cislo__icontains=term) |
            Q(poruchy__icontains=term) |
            Q(koren_problemu__icontains=term)
        ).order_by('-vytvorene')[:30]

    def format_detective_result(self, obj):
        """Hook pre formátovanie výsledku v Detective Search."""
        stav = ", ".join([str(t) for t in obj.typ_servisu.all()]) or "Servisný záznam"
        datum = obj.vytvorene.strftime("%d.%m.%Y %H:%M") if obj.vytvorene else "Neuvedené"
        zakaznik = getattr(obj.zakaznik, 'Nazov_spolocnosti', str(obj.zakaznik)) if obj.zakaznik else "Neuvedené"
        zariadenie = getattr(obj.zariadenie, 'nazov', str(obj.zariadenie)) if obj.zariadenie else ""
        
        return {
            'id': obj.id, 
            'name': zakaznik, 
            'search_value': zakaznik,
            'rola': zariadenie,
            'date': datum,
            'cat': f"🚦 Stav: {stav}"
        }

    def get_urls(self):
        """Ponechávame len špecifickú URL pre AI Šepkára. Zvyšok rieši jadro."""
        urls = super().get_urls()
        custom_urls = [
            path('ai-sepkar/<int:zariadenie_id>/', self.admin_site.admin_view(self.ai_sepkar_api), name='ai-sepkar'),
        ]
        return custom_urls + urls

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('zakaznik', 'zariadenie', 'uloha').prefetch_related(
            'typ_servisu', 
            'technici'
        )

    @admin.display(description='🗂️ Záznam, Technici & ID', ordering='id')
    def styled_nazov(self, obj):
        typy = ", ".join([str(t) for t in obj.typ_servisu.all()])
        technici_list = ", ".join([str(t) for t in obj.technici.all()])
        
        display_text = typy if typy else "Všeobecný servisný záznam"
        
        badges = []
        if technici_list:
            badges.append(render_isas_badge(technici_list, icon="👨‍🔧", type_color="info", wrapper=False))
        if obj.koren_problemu and obj.koren_problemu != 'NEHODNOTENE':
            je_opravnena = getattr(obj, 'je_reklamacia_opravnena', False)
            badge_color = "danger" if je_opravnena else "warning"
            badge_icon = "🔴" if je_opravnena else "🟡"
            badges.append(render_isas_badge(obj.get_koren_problemu_display(), icon=badge_icon, type_color=badge_color, wrapper=False))
        if obj.zmena_hardveru:
            badges.append(render_isas_badge("ZMENA HW", icon="⚠️", type_color="warning", wrapper=False))
            
        badges_html = render_isas_layout(mark_safe("".join(badges)), layout="col", gap=4, center=True) if badges else ""
        
        img_url = None
        if obj.zakaznik and getattr(obj.zakaznik, 'domena', None):
            clean_domain = str(obj.zakaznik.domena).lower().replace('http://', '').replace('https://', '').replace('www.', '').split('/')[0].strip()
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
            if '.' in clean_domain:
                img_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"
        
        return render_isas_avatar(
            icon="📝",
            title=display_text,
            subtitle=f"ID ÚLOHY: #{obj.id}",
            bg_gradient="#3b82f6, #1d4ed8",
            shadow_color="rgba(59, 130, 246, 0.25)",
            extra_html=badges_html,
            image_url=img_url
        )

    @admin.display(description='🏢 Zákazník', ordering='zakaznik')
    def styled_zakaznik(self, obj):
        nazov = str(obj.zakaznik) if obj.zakaznik else "Neuvedené"
        
        content = [
            render_isas_text("🏢", size=18),
            render_isas_text(nazov, size=12, weight=800, color="#334155")
        ]
        
        if obj.uloha_id:
            content.append(render_isas_badge(f"ÚLOHA #{obj.uloha_id}", icon="🔗", type_color="info", wrapper=False))
            
        content_html = mark_safe("".join(content))
        return render_isas_layout(
            content_html, layout="col", gap=4, center=True, 
            bg_color="#f8fafc", border_color="#cbd5e1", padding="10px 12px", 
            border_radius="12px", min_width="120px", shadow="0 2px 4px rgba(0,0,0,0.02)"
        )

    @admin.display(description='⚙️ Zariadenie', ordering='zariadenie')
    def styled_zariadenie(self, obj):
        nazov = str(obj.zariadenie) if obj.zariadenie else "Neznáme zariadenie"
        
        content = [
            render_isas_text("⚙️", size=18),
            render_isas_text(nazov, size=11, weight=800, color="#334155")
        ]
        
        if obj.stav_motohodin is not None:
            mh_text = obj.meter_display
            suffix = ""
            if getattr(obj, 'motohodiny_v_zatazi', None) is not None:
                suffix += f" | 🏋️ {obj.meter_load_display}"
            if obj.namerane_hodnoty:
                suffix += " 📊"

            content.append(render_isas_badge(mh_text, icon="⏱️", type_color="neutral", suffix_html=suffix, wrapper=False))
            
        content_html = mark_safe("".join(content))
        return render_isas_layout(
            content_html, layout="col", gap=4, center=True, 
            bg_color="#f8fafc", border_color="#cbd5e1", padding="10px 12px", 
            border_radius="12px", min_width="120px", shadow="0 2px 4px rgba(0,0,0,0.02)"
        )

    @admin.display(description='📝 Report & Médiá')
    def styled_report(self, obj):
        poruchy_text = obj.poruchy.strip() if obj.poruchy else ""
        if len(poruchy_text) > 40:
            poruchy_text = poruchy_text[:37] + "..."
        
        if poruchy_text:
            text_html = render_isas_text(poruchy_text, size=10, weight=600, color="#334155")
        else:
            text_html = render_isas_text("Bez popisu", size=10, muted=True)
        
        media_ikony = []
        if obj.foto_pred: media_ikony.append("📸")
        if obj.foto_po: media_ikony.append("✅")
        if obj.video: media_ikony.append("🎥")
        if obj.formalne_zhodnotenie_ai or obj.ai_analyza_historie: 
            media_ikony.append("🤖")
        if getattr(obj, 'zobrazit_na_webe', False): 
            media_ikony.append("🌐")
        
        media_html = ""
        if media_ikony:
            icons_str = "".join([render_isas_text(icon, custom_style="cursor:help;") for icon in media_ikony])
            media_html = render_isas_layout(mark_safe(icons_str), layout="row", gap=4, center=True)

        slug_html = ""
        if getattr(obj, 'slug', None):
            slug_html = render_isas_badge(f"/{obj.slug}", type_color="neutral", monospace=True, wrapper=False)
            
        content_html = format_html('{}{}{}', text_html, media_html, slug_html)
        return render_isas_layout(
            content_html, layout="col", gap=6, center=True, 
            bg_color="#ffffff", border_color="#e2e8f0", padding="10px 12px", 
            border_radius="12px", min_width="140px", max_width="200px", shadow="0 2px 4px rgba(0,0,0,0.02)"
        )

    @admin.display(description='📅 Čas & Stav', ordering='vytvorene')
    def styled_cas_a_stav(self, obj):
        vytvorene = obj.vytvorene.strftime('%d.%m.%Y %H:%M') if obj.vytvorene else "—"
        
        content = [
            render_isas_text("Vytvorené", size=9, weight=800, color="#64748b"),
            render_isas_text(f"🕒 {vytvorene}", size=11, weight=900, color="#0f172a")
        ]
        
        if obj.dokoncene:
            content.append(render_isas_badge("DOKONČENÉ", icon="✅", type_color="success", wrapper=False))
        else:
            content.append(render_isas_badge("PREBIEHA", icon="⏳", type_color="warning", wrapper=False, css_class="action-pulse-rem"))
            
        if obj.hodnotenie_zakaznika:
            hviezdicky = "⭐" * obj.hodnotenie_zakaznika
            content.append(render_isas_badge(hviezdicky, type_color="warning", wrapper=False))
            
        content_html = mark_safe("".join(content))
        return render_isas_layout(
            content_html, layout="col", gap=4, center=True, 
            bg_color="#ffffff", border_color="#e2e8f0", padding="10px 14px", 
            border_radius="12px", min_width="110px", shadow="0 2px 6px rgba(0,0,0,0.03)"
        )

    @admin.display(description='👨‍🔧 Technici')
    def styled_technici(self, obj):
        technici = obj.technici.all()
        if not technici:
            return render_isas_text("Neobsadené", size=11, weight=800, color="#94a3b8")
        
        names = []
        for t in technici:
            full_name = f"{getattr(t, 'meno', '')} {getattr(t, 'priezvisko', '')}".strip()
            if not full_name: full_name = str(t)
            names.append(render_isas_text(full_name, size=11, weight=800, color="#166534"))
            
        content = [render_isas_text("👨‍🔧", size=16)] + names
        content_html = mark_safe("".join(content))
        
        return render_isas_layout(
            content_html, layout="col", gap=2, center=True, 
            bg_color="#f0fdf4", border_color="#86efac", padding="8px 14px", 
            border_radius="12px", min_width="110px", shadow="0 2px 4px rgba(0,0,0,0.02)"
        )

    @admin.display(description='📄 Protokol')
    def styled_protokol(self, obj):
        if not obj.pk: return "—"
        url = f"/evidencia/protokol/{obj.bezpecnostny_kluc}/"

        if not obj.protokol_uzavrety:
            return render_isas_link_btn(
                url=url, text="Vyžiadať podpis", icon="✍️", type_color="warning", layout="col"
            )

        if obj.uzavrety_pdf_dokument:
            podpisal = escape(obj.meno_podpisujuceho) if obj.meno_podpisujuceho else "Zákazník"
            gps_ikona = "📍 " if obj.gps_suradnice else ""
            
            suffix_badge = render_isas_badge(f"{gps_ikona}{podpisal}", type_color="neutral", wrapper=False)
            return render_isas_link_btn(
                url=url, text="Zobraziť PDF", icon="🔒", type_color="success", layout="col", suffix_html=suffix_badge, target="_blank"
            )

        poller_content = format_html('{}{}', render_isas_text("⏳", size=18), render_isas_text("Generuje sa...", size=9, weight=900, color="#d97706"))
        poller_box = render_isas_layout(
            poller_content, layout="col", gap=4, center=True,
            bg_color="#fffbeb", border_color="#f59e0b", padding="10px 12px",
            border_radius="12px", min_width="100px", shadow="0 0 10px rgba(245,158,11,0.2)"
        )
        
        btn_success = render_isas_link_btn(url=url, text="Zobraziť PDF", icon="🔒", type_color="success", layout="col", target="_blank")
        btn_error = render_isas_link_btn(url=url, text="Chyba Skúsiť znova", icon="⚠️", type_color="danger", layout="col")
        
        animated_box = render_isas_layout(
            poller_box,
            layout="row",
            center=True,
            css_class="isas-protokol-poller",
            data_attrs={
                "id": str(obj.id),
                "url": f"/admin/Evidencia/servisnyzaznam/{obj.id}/change/",
                "success": btn_success,
                "error": btn_error
            },
            custom_style="width: 100%;"
        )
        
        return mark_safe(animated_box)

    def ai_sepkar_api(self, request, zariadenie_id):
        try:
            if not request.user.has_perm('Evidencia.view_servisnyzaznam'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie využívať AI diagnostiku."}, status=403)

            # 🚀 ISAS ARCHITECT: Dynamický prefetch podľa aktívneho odvetvia (prevencia FieldError pri inom odvetví)
            vsetky_vztahy = ['vyrobca'] + get_active_parameter_reverse_names()
            # 🛡️ DEFENSIVE: Overenie existencie relácií na modeli (100% ochrana pred FieldError)
            vztahy_na_prefetch = [rel for rel in vsetky_vztahy if hasattr(ServisovaneZariadenie, rel)]
            
            # Ochrana pred IDOR (Tenant Isolation) a chybou 500
            if getattr(request, 'is_subdomain', False) and getattr(request, 'tenant', None):
                zariadenie = get_object_or_404(ServisovaneZariadenie.objects.select_related(*vztahy_na_prefetch), id=zariadenie_id, zakaznik=request.tenant)
            else:
                zariadenie = get_object_or_404(ServisovaneZariadenie.objects.select_related(*vztahy_na_prefetch), id=zariadenie_id)
            # 3. AI LOGIKA (Delegovaná do utils pre DRY architektúru)
            from .utils import generuj_ai_sepkar_analyzu
            try:
                technik_id = request.user.id if request.user else None
                bezpecny_text = generuj_ai_sepkar_analyzu(zariadenie, technik_id=technik_id)
                
                if bezpecny_text == "EMPTY_HISTORY_FLAG":
                    return JsonResponse({"status": "success", "message": "Zariadenie má zatiaľ čistý register a nemá aktivované IoT."})
                    
                return JsonResponse({"status": "success", "message": bezpecny_text})
                
            except ValueError as e:
                if str(e) == "AI_EMPTY_RESPONSE":
                    return JsonResponse({"status": "error", "message": "AI zablokovala odpoveď kvôli prísnym bezpečnostným filtrom alebo vrátila prázdnu odpoveď."})
                raise e
            except Exception as e:
                return JsonResponse({"status": "error", "message": f"Chyba spojenia s AI: {str(e)}"})
        except Http404:
            return JsonResponse({"status": "error", "message": "Požadované zariadenie neexistuje alebo k nemu nemáte prístup."}, status=404)
        except Exception as e:
            logging.getLogger(__name__).error(f"Kritická chyba v AI Šepkárovi: {e}")
            return JsonResponse({"status": "error", "message": "Interná systémová chyba."}, status=500)

    def save_model(self, request, obj, form, change):
        # Zistenie skutočného stavu v DB tesne pred uložením (Ochrana voči TOCTOU)
        if change:
            # Použijeme .values_list pre maximálnu rýchlosť, nepotrebujeme ťahať celý objekt
            stav = type(obj).objects.filter(pk=obj.pk).values_list('dokoncene', flat=True).first()
            obj._skutocny_stav_predtym = bool(stav)
        else:
            obj._skutocny_stav_predtym = False
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        # Namiesto zraniteľného form.initial použijeme bezpečný stav z DB
        bola_dokoncena_predtym = getattr(form.instance, '_skutocny_stav_predtym', False)

        super().save_related(request, form, formsets, change)
        
        zaznam = form.instance
        zaznam.refresh_from_db()

        # 🛡️ PREDICKÝ SERVIS: Ak je záznam dokončený a zmenil sa formulár (typy prác, motohodiny) alebo stav dokončenia
        if zaznam.dokoncene and (not bola_dokoncena_predtym or form.has_changed()):
            zaznam.vygeneruj_prediktivne_plany()

        if not change and zaznam.dokoncene and zaznam.zariadenie and zaznam.zakaznik:
            
            # 1. Základný filter (Stroj + Zákazník + Otvorená)
            base_ulohy = Uloha.objects.filter(
                dokoncene=False,
                zakaznik=zaznam.zakaznik,
                zariadenia=zaznam.zariadenie
            ).prefetch_related('typ_servisu', 'technici').annotate(
                pocet_zariadeni=Count('zariadenia')
            )
            
            pocet_uloh = base_ulohy.count()
            html_content = ""

            if pocet_uloh > 0:
                # 1. Skloňovanie pre hlavičku modálu
                if pocet_uloh == 1:
                    text_ulohy = "1 otvorenú úlohu, ktorá s týmto záznamom súvisí"
                elif 2 <= pocet_uloh <= 4:
                    text_ulohy = f"{pocet_uloh} otvorené úlohy, ktoré s týmto záznamom súvisia"
                else:
                    text_ulohy = f"{pocet_uloh} otvorených úloh, ktoré s týmto záznamom súvisia"

                header_html = format_html("""
                    <div class="isas-ai-modal-header">
                        <h2><span>🤖</span> ISAS CORE - Automatická detekcia</h2>
                        <p>Servisný záznam bol uložený. Systém našiel {}.</p>
                    </div>
                """, text_ulohy)
                
                # 2. ISAS SCORING ENGINE (Hodnotenie zhody)
                zaznam_typy = set(zaznam.typ_servisu.values_list('id', flat=True))
                zaznam_technici = set(zaznam.technici.values_list('id', flat=True))

                now_fallback = timezone.now()
                ulohy_scored = []
                for u in base_ulohy:
                    score = 0
                    u_typy = set(t.id for t in u.typ_servisu.all())
                    u_technici = set(t.id for t in u.technici.all())

                    if zaznam_typy and (zaznam_typy & u_typy):
                        score += 20  # Silná zhoda (Typ práce)
                    if zaznam_technici and (zaznam_technici & u_technici):
                        score += 10  # Stredná zhoda (Technik)

                    ulohy_scored.append((score, u.zaciatok is not None, u.zaciatok if u.zaciatok is not None else now_fallback, u))

                ulohy_scored.sort(key=lambda x: (x[0], x[1], x[2], x[3].id), reverse=True)

                if pocet_uloh == 1:
                    uloha = ulohy_scored[0][3]
                    if uloha.pocet_zariadeni == 1:
                        typy_prace = ", ".join([str(t) for t in uloha.typ_servisu.all()]) or "Všeobecný servis"
                        html_content = format_html("""{}
                            <div class="isas-ai-task-card single-match">
                                <span class="isas-ai-tag">Zhoda</span>
                                <h3>{}</h3>
                                <p><i>Plánovaný typ servisu/práce: {}</i></p>
                                <button onclick="uzavriUlohuAI_Z_Modalu({}, this)" class="isas-ai-close-task-btn">✅ UZAVRIEŤ TÚTO ÚLOHU</button>
                            </div>
                        """, header_html, str(uloha), typy_prace, uloha.id)
                    else:
                        pocet_zostava = uloha.pocet_zariadeni - 1
                        if pocet_zostava == 1:
                            slovo_zariadenia = "1 ďalšie zariadenie"
                        elif 2 <= pocet_zostava <= 4:
                            slovo_zariadenia = f"{pocet_zostava} ďalšie zariadenia"
                        else:
                            slovo_zariadenia = f"{pocet_zostava} ďalších zariadení"
                            
                        messages.warning(request, format_html('🤖 <b>ISAS AI UPOZORNENIE:</b> Toto zariadenie bolo súčasťou väčšej Úlohy #{}. Zostáva v nej vyriešiť ešte {}.', uloha.id, slovo_zariadenia))
                
                elif pocet_uloh > 1:
                    riadky_list = []
                    for score, _, datum, u in ulohy_scored:
                        datum_str = u.zaciatok.strftime("%d.%m.%Y %H:%M") if u.zaciatok else "Neznámy dátum"
                        typy_prace = ", ".join([str(t) for t in u.typ_servisu.all()]) or "Všeobecný servis"
                        
                        if score >= 30:
                            tag_class = "perfect-match"
                            tag_text = "🎯 PERFEKTNÁ ZHODA"
                        elif score >= 20:
                            tag_class = "work-match"
                            tag_text = "🛠️ ZHODA PRÁCE"
                        elif score >= 10:
                            tag_class = "tech-match"
                            tag_text = "👨‍🔧 ZHODA TECHNIKA"
                        else:
                            tag_class = "machine-match"
                            tag_text = "⚙️ ZHODA ZARIADENIA"

                        riadky_list.append(format_html("""
                            <div class="isas-ai-task-row {}">
                                <div class="isas-ai-task-info">
                                    <div class="isas-ai-task-title"><span class="isas-ai-tag {}">{}</span> <strong>{}</strong></div>
                                    <div class="isas-ai-task-meta">Dátum: <b>{}</b></div>
                                    <div class="isas-ai-task-meta">Účel: <i>{}</i></div>
                                </div>
                                <button type="button" onclick="uzavriUlohuAI_Z_Modalu({}, this)" class="isas-ai-close-task-btn small">✅ UZAVRIEŤ</button>
                            </div>
                        """, tag_class, tag_class, tag_text, str(u), datum_str, typy_prace, u.id))
                    
                    slovo_eviduje = "otvorené úlohy" if 2 <= pocet_uloh <= 4 else "otvorených úloh"
                    riadky_safe = mark_safe("".join(riadky_list))
                    html_content = format_html(
                        "{}<p class='isas-ai-warning-text'>⚠️ Systém eviduje {} {}! Ktorú si práve teraz zrealizoval?</p>{}",
                        header_html, pocet_uloh, slovo_eviduje, riadky_safe
                    )

            if html_content:
                tlacidlo_zatvorit = mark_safe("""
                <div class="isas-ai-modal-footer">
                    <button type="button" onclick="closeISASAIModal()" class="isas-ai-close-modal-btn">Zatvoriť</button>
                </div>
                """)
                html_content = format_html("{}{}", html_content, tlacidlo_zatvorit)
                
                request.session['isas_ai_modal'] = html_content
        poslat_adminovi_tg = form.cleaned_data.get('notif_admin_tg', True)
        poslat_adminovi_mail = form.cleaned_data.get('notif_admin_mail', True)
        poslat_adminovi_discord = form.cleaned_data.get('notif_admin_discord', True)

        if zaznam.dokoncene and not bola_dokoncena_predtym and (poslat_adminovi_tg or poslat_adminovi_mail or poslat_adminovi_discord):
            from .tasks import zaznam_odoslat_notifikacie_adminovi_task
            def _dispatch_zaznam_notif(z_id=zaznam.id, ptg=poslat_adminovi_tg, pm=poslat_adminovi_mail, pd=poslat_adminovi_discord):
                try:
                    zaznam_odoslat_notifikacie_adminovi_task.delay([z_id], ptg, pm, pd)
                except Exception as exc:
                    logger.warning(f"Zlyhalo odoslanie notifikácií záznamu #{z_id} do Celery: {exc}")
                    messages.warning(request, "⚠️ Záznam bol uložený, ale notifikácie sa nepodarilo zaradiť do fronty (výpadok Celery/Redis).")
            transaction.on_commit(_dispatch_zaznam_notif)
            
            messages.success(request, "✅ Záznam bol uložený. Notifikácie sa odosielajú na pozadí.")

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name in ["typ_servisu"]:
            kwargs["widget"] = forms.CheckboxSelectMultiple()
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def save_formset(self, request, form, formset, change):
        if formset.model == PouzityMaterialVZaznamoch:
            with transaction.atomic():
                instances = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                for inline_form in formset.forms:
                    if (formset.can_delete and formset._should_delete_form(inline_form)) or not inline_form.has_changed():
                        continue
                    instance = inline_form.instance
                    oid = request.POST.get(f"{inline_form.prefix}-object_id")
                    
                    if oid and str(oid).isdigit():
                        instance.object_id = int(oid)
                    if getattr(instance, 'object_id', None):
                        instance.zaznam = form.instance
                        instance.save()
                formset.save_m2m()
        else:
            super().save_formset(request, form, formset, change)

    class Media:
        js = (
            'admin/js/vendor/jquery/jquery.js',
            'admin/js/vendor/select2/select2.full.js',
            'admin/js/jquery.init.js',
            'js/vyhladavaniepoloziekvulohach.js',
            'js/vyber_zariadenia_vservisnomzazname.js',
        )
        css = {
            'all': (
                'admin/css/vendor/select2/select2.css',
            )
        }

class StavDokumentacieFilter(admin.SimpleListFilter):
    title = '📄 Stav zdroja'
    parameter_name = 'stav_dok'

    def lookups(self, request, model_admin):
        return (
            ('subor', '📎 Obsahuje nahrávaný súbor'),
            ('web', '🌐 Obsahuje webový odkaz'),
            ('chyba', '⚠️ Prázdny (Doplniť)'),
        )

    def queryset(self, request, queryset):
        # Vytvoríme podmienku, ktorá skontroluje všetky 3 reálne súborové polia
        ma_subor = ~Q(subor_manual='') | ~Q(subor_schema='') | ~Q(subor_certifikat='')
        ma_web = ~Q(web_stranka='') & Q(web_stranka__isnull=False)

        if self.value() == 'subor':
            return queryset.filter(ma_subor)
        if self.value() == 'web':
            return queryset.filter(ma_web)
        if self.value() == 'chyba':
            # Nemá ani jeden súbor a nemá ani web
            return queryset.exclude(ma_subor).exclude(ma_web)
        return queryset

class InternaPoznamkaFilter(admin.SimpleListFilter):
    title = '💡 Interné know-how (Poznámky)'
    parameter_name = 'ma_poznamku'

    def lookups(self, request, model_admin):
        return (
            ('ano', '🧠 Obsahuje tipy a poznámky'),
            ('nie', '❌ Bez poznámok'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'ano':
            return queryset.exclude(Q(interna_poznamka='') | Q(interna_poznamka__isnull=True))
        if self.value() == 'nie':
            return queryset.filter(Q(interna_poznamka='') | Q(interna_poznamka__isnull=True))
        return queryset

@admin.register(TechnickaSpecifikacia)
class TechnickaSpecifikaciaAdmin(ISASPremiumAdmin):
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Bezpečný prefetch s odfiltrovaním neplatných/stale ContentType záznamov
        valid_ct_ids = [ct.id for ct in ContentType.objects.all() if ct.model_class() is not None]
        return qs.filter(content_type_id__in=valid_ct_ids).select_related('content_type').prefetch_related('polozka')

    list_display = (
        'get_kategoria',
        'get_nazov_a_ikona',
        'get_vyrobca', 
        'get_dodavatel', 
        'get_typ', 
        'get_info_a_odkazy'
    )
    
    fieldsets = (
        ('💡 FIREMNÉ KNOW-HOW A POZNÁMKY', {
            'fields': ('interna_poznamka',),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">🧠 <b>Zlato firmy.</b> Napíšte sem to, čo v oficiálnom manuáli chýba. Na čo si dať pozor? Ktoré skrutky sa zvyknú strhnúť? Aké sú osvedčené finty?</div>'
        }),
        ('📚 SÚBORY A DOKUMENTÁCIA', {
            'fields': ('subor_manual', 'subor_schema', 'subor_certifikat'),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">📎 <b>Dedikované sloty pre súbory.</b> Nahrajte príslušné PDF súbory do správnych kolóniek. Nemusíte vyplniť všetky, nahrajte len to, čo máte k dispozícii.</div>'
        }),
        ('🌐 EXTERNÉ ODKAZY', {
            'fields': ('web_stranka',),
            'description': '<div style="color: #64748b; font-size: 12px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #e2e8f0;">🔗 <b>Odkaz na web.</b> Vložte link na inštruktážne video (YouTube), cloudové úložisko výrobcu alebo online katalóg.</div>'
        }),
    )
    
    list_display_links = None
    list_filter = (
        HlavnaKategoriaKatalogFilter,
        SkladoveModelyFilter, 
        StavDokumentacieFilter, 
        InternaPoznamkaFilter,
    )
    search_fields = ('search_name',) # Hľadáme v našom indexe, nie cez GFK!

    def has_add_permission(self, request):
        return False
        
    def has_delete_permission(self, request, obj=None):
        return False

    isas_title = "📖 Technické špecifikácie"
    isas_preloader_text = "⏳ Vstupujete do zoznamu s technickými špecifikáciami dielov/položiek a zariadení..."
    
    isas_file_action_modal = True
    isas_hide_action_checkboxes = True

    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "📄",
        'skryt_akcie': True,
        'bez_tlacidla_odstranit': True,
        'katalogovy_dashboard': True,
        'katalog_choices': CATEGORY_CHOICES
    }

    def get_detective_queryset(self, request, term):
        
        term = term.lower()
        q_obj = Q()
        
        pouzite_ct_ids = self.get_queryset(request).values_list('content_type_id', flat=True).distinct()
        
        for ct in ContentType.objects.filter(id__in=pouzite_ct_ids):
            model_class = ct.model_class()
            if model_class:
                matching_ids = model_class.objects.filter(seriove_cislo__icontains=term).values_list('id', flat=True)[:30]
                if matching_ids:
                    q_obj |= Q(content_type=ct, object_id__in=matching_ids)
        
        if q_obj:
            return self.get_queryset(request).filter(q_obj)[:30]
            
        return self.get_queryset(request).none()

    def format_detective_result(self, obj):
        cisty_nazov = ocisti_nazov_od_jednotky(obj.polozka) if obj.polozka else "Neznáma položka"
        cat_name = get_kategoria_nazov(obj.content_type.model_class()).upper() if obj.content_type else "KATEGÓRIA"
        vyrobca = getattr(obj.polozka, 'vyrobca', None)
        rola = vyrobca.nazov_vyrobcu if vyrobca else ""
        
        return {
            'id': obj.id, 
            'name': cisty_nazov, 
            'search_value': cisty_nazov,
            'rola': rola,
            'cat': cat_name
        }

    @admin.display(description='📦 Kategória', ordering='content_type')
    def get_kategoria(self, obj):
        if not obj.content_type: return ""
        nazov = get_kategoria_nazov(obj.content_type.model_class())
        return render_isas_badge(nazov.upper(), type_color="neutral", wrapper=True)

    @admin.display(description='🏷️ Identifikácia položky (Názov / SKU / EAN)')
    def get_nazov_a_ikona(self, obj):
        if not obj.polozka: return ""

        cisty_nazov = ocisti_nazov_od_jednotky(obj.polozka)
        sku_val = getattr(obj.polozka, 'katalogove_cislo', None) or ''
        ean_val = getattr(obj.polozka, 'ean_kod', None) or ''
        
        metadata = []
        if sku_val:
            metadata.append(format_html('🏷️ SKU: <b style="color: #0f172a;">{}</b>', sku_val))
        if ean_val:
            metadata.append(format_html('🦓 EAN: <b style="color: #0f172a;">{}</b>', ean_val))

        return render_isas_entity_card(icon="🔧", title=cisty_nazov, metadata_list=metadata)

    @admin.display(description='🏭 Výrobca')
    def get_vyrobca(self, obj):
        if obj.polozka and hasattr(obj.polozka, 'vyrobca') and obj.polozka.vyrobca:
            vyrobca = obj.polozka.vyrobca.nazov_vyrobcu
            return render_isas_badge(vyrobca, icon="🏭", type_color="warning", wrapper=True)
        return render_isas_layout(render_isas_text("—", weight=800, color="#cbd5e1"), center=True)

    @admin.display(description='🚚 Dodávateľ')
    def get_dodavatel(self, obj):
        if obj.polozka and hasattr(obj.polozka, 'dodavatel') and obj.polozka.dodavatel:
            dodavatel = str(obj.polozka.dodavatel)
            return render_isas_badge(dodavatel, icon="📦", type_color="success", wrapper=True)
        return render_isas_layout(render_isas_text("—", weight=800, color="#cbd5e1"), center=True)

    @admin.display(description='⚙️ Typ položky')
    def get_typ(self, obj):
        if obj.polozka and hasattr(obj.polozka, 'typ') and obj.polozka.typ:
            typ_kod = obj.polozka.typ
            nazov_typu = dict(obj.polozka.TYP_CHOICES).get(typ_kod, typ_kod)
            t_color = "info" if typ_kod == 'O' else "purple"
            return render_isas_badge(nazov_typu.upper(), type_color=t_color, wrapper=True)
        return render_isas_layout(render_isas_text("—", weight=800, color="#cbd5e1"), center=True)

    @admin.display(description='📑 Dokumentácia & 💡 Info')
    def get_info_a_odkazy(self, obj):
        content = []

        kto_som = getattr(obj.polozka, 'kto_som', '')
        if kto_som:
            kto_som_text = render_isas_text(kto_som, size=11, weight=600, color="#475569", align="left", icon="ℹ️")
            content.append(render_isas_layout(
                kto_som_text, layout="col", bg_color="#f8fafc", border_color="#cbd5e1", 
                padding="6px 10px", border_radius="8px", max_width="250px", shadow="0 2px 4px rgba(0,0,0,0.02)"
            ))

        if obj.interna_poznamka:
            poznamka_text = render_isas_text(obj.interna_poznamka, size=11, weight=700, color="#92400e", align="left", icon="💡")
            content.append(render_isas_layout(
                poznamka_text, layout="col", bg_color="#fffbeb", border_color="#fcd34d", 
                padding="6px 10px", border_radius="8px", max_width="250px", shadow="0 2px 4px rgba(0,0,0,0.02)"
            ))

        url = reverse(f'admin:{obj._meta.app_label}_{obj._meta.model_name}_change', args=[obj.pk])
        content.append(render_isas_link_btn(url=url, text="NAHRAŤ SÚBORY", icon="➕", type_color="dashed", wrapper=True))

        tlacidla = []
        if obj.subor_manual and getattr(obj.subor_manual, 'name', None):
            try:
                tlacidla.append(render_isas_link_btn(url="javascript:void(0);", text="Návod", icon="📖", type_color="info", onclick=f"showFileModal('{obj.subor_manual.url}')"))
            except (ValueError, AttributeError):
                pass
        if obj.subor_schema and getattr(obj.subor_schema, 'name', None):
            try:
                tlacidla.append(render_isas_link_btn(url="javascript:void(0);", text="Schéma", icon="⚡", type_color="danger", onclick=f"showFileModal('{obj.subor_schema.url}')"))
            except (ValueError, AttributeError):
                pass
        if obj.subor_certifikat and getattr(obj.subor_certifikat, 'name', None):
            try:
                tlacidla.append(render_isas_link_btn(url="javascript:void(0);", text="Certifikát", icon="📜", type_color="success", onclick=f"showFileModal('{obj.subor_certifikat.url}')"))
            except (ValueError, AttributeError):
                pass
        if obj.web_stranka:
            tlacidla.append(render_isas_link_btn(url=obj.web_stranka, text="Web ↗", icon="🌐", type_color="neutral"))

        if tlacidla:
            tlacidla_html = mark_safe("".join(tlacidla))
            content.append(render_isas_layout(tlacidla_html, layout="row", gap=6, center=True))

        return render_isas_layout(mark_safe("".join(content)), layout="col", gap=8, center=True)

@admin.register(AktualneNaSklade)
class AktualneNaSkladeAdmin(ISASReadOnlyAdminMixin, SkladAdmin):
    """
    Read-Only pohľad na aktuálny stav skladu.
    Využíva ISASReadOnlyAdminMixin pre tvrdý blok úprav, mazania a pridávania.
    """

    def get_inline_instances(self, request, obj=None):
        if not obj: return []
        sub = request.GET.get('sub')
        
        if sub and sub in AKTUALNE_INLINES_MAP:
            OriginalInlineClass = AKTUALNE_INLINES_MAP[sub]
            
            class ReadOnlyInline(OriginalInlineClass):
                can_delete = False
                extra = 0
                max_num = 0
                
                def has_add_permission(self, request, obj=None): return False
                def has_change_permission(self, request, obj=None): return False
                
                def get_queryset(self, request):
                    return super().get_queryset(request).select_related('sklad')
            
            if hasattr(ReadOnlyInline, 'inlines'):
                safe_nested_inlines = []
                for NestedClass in ReadOnlyInline.inlines:
                    class ReadOnlyNested(NestedClass):
                        can_delete = False
                        extra = 0
                        max_num = 0
                        
                        def has_add_permission(self, request, obj=None): return False
                        def has_change_permission(self, request, obj=None): return False
                        
                        def get_queryset(self, request):
                            qs = super().get_queryset(request)
                            select_fields = []
                            
                            if hasattr(self.model, 'dodavatel'): select_fields.append('dodavatel')
                            if hasattr(self.model, 'vyrobca'): select_fields.append('vyrobca')
                            
                            for f in self.model._meta.fields:
                                if f.is_relation and f.related_model in PARENT_MODELS.values():
                                    select_fields.append(f.name)
                                    
                            if select_fields:
                                qs = qs.select_related(*select_fields)
                            return qs
                            
                    safe_nested_inlines.append(ReadOnlyNested)
                ReadOnlyInline.inlines = tuple(safe_nested_inlines)

            return [ReadOnlyInline(self.model, self.admin_site)]
            
        return []

class KategoriaKosikFilter(admin.SimpleListFilter):
    title = '🗂️ Kategória dielu/položky'
    parameter_name = 'kategoria'

    def lookups(self, request, model_admin):
        
        # Zistíme, aké položky sú reálne v košíku
        qs = model_admin.get_queryset(request)
        nazvy_v_kosiku = set(qs.values_list('nazov_polozky', flat=True))
        
        ciste_kody = set()
        for nazov in nazvy_v_kosiku:
            if nazov:
                ciste_kody.add(nazov.split("(")[0].strip())
                ciste_kody.add(nazov.strip())

        lookups_data = []
        
        # Prechádzame mapu a pridáme len tie kategórie, ktoré majú zhodu
        for cat_group in CATEGORY_MAP.values():
            for sub_name, model_class in cat_group.items():
                # Ak model obsahuje aspoň jeden kód z košíka, pridáme ho do filtra
                if ciste_kody and model_class.objects.filter(seriove_cislo__in=list(ciste_kody)).exists():
                    model_name = model_class._meta.model_name.lower()
                    zobrazovany_nazov = getattr(model_class._meta, 'verbose_name', sub_name).title()
                    
                    polozka_html = render_isas_text(zobrazovany_nazov, color="#334155", weight=600)
                    lookups_data.append((model_name, polozka_html))
                
        return lookups_data

    def queryset(self, request, queryset):
        if self.value():
            target_model_name = self.value()
            target_model_class = None
            
            for cat_group in CATEGORY_MAP.values():
                for model_class in cat_group.values():
                    if model_class._meta.model_name.lower() == target_model_name:
                        target_model_class = model_class
                        break
                if target_model_class: break
            
            if target_model_class:
                valid_codes = target_model_class.objects.values_list('seriove_cislo', flat=True)
                q_objects = Q()
                for code in valid_codes:
                    q_objects |= Q(nazov_polozky__istartswith=code)
                
                if q_objects:
                    return queryset.filter(q_objects)
                else:
                    return queryset.none()
        return queryset

class DodavatelKosikInteligentnyFilter(ISASBaseRelatedFilter):
    title, parameter_name, isas_default_field, isas_icon, isas_order_by = '🏢 Aktívni Dodávatelia v košíku', 'dodavatel_filter', 'dodavatel', '🏢', 'Nazov_spolocnosti'

@admin.register(KosikSPolozkamiNaDoobjednanie)
class KosikSPolozkamiNaDoobjednanieAdmin(ISASPremiumAdmin):
    list_display = (
        'get_kategoria_styled',
        'get_nazov_polozky_ultimate',
        'styled_filialka_kosik',
        'get_aktualny_stav_ultimate', 
        'get_dodavatel_chip', 
        'get_datum_zistenia_styled', 
        'upravitelne_mnozstvo_premium', 
        'stav_objednavky_badge', 
        'zobraz_cas_odoslania_styled'
    )    
    list_filter = ('objednane', DodavatelKosikInteligentnyFilter, KategoriaKosikFilter)
    search_fields = ('nazov_polozky',)
    autocomplete_fields = ['dodavatel', 'filialka']
    list_display_links = None

    isas_title = "🛒 Košík s dielmi/položkami na doobjednanie"
    isas_preloader_text = "⏳ Vstupujete do košíka s dielmi/položkami na doobjednanie..."
    isas_kosik_manager = True

    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "📦",
        'custom_buttons_html': '<button type="button" id="btn-send-email" class="isas-action-btn btn-blue">🚀 Odoslať objednávky</button><button type="button" id="btn-settings" class="isas-action-btn btn-purple">⚙️ Nastavenia</button>',
        'ignore_default_action_ids': ['btn-settings', 'btn-send-email']
    }

    def get_kosik_config(self, request):
        glob, _ = GlobalneNastavenieSkladu.objects.get_or_create(pk=1)
        
        limity_data = {}
        for main_cat, subcats in CATEGORY_MAP.items():
            for sub, _ in subcats.items():
                lim_obj, _ = LimitKategorieSkladu.objects.get_or_create(
                    kategoria_nazov=sub,
                    defaults={'kriticky_limit': 2}
                )
                limity_data[sub] = lim_obj.kriticky_limit
                
        return {
            "notifikacie": glob.zapnute_notifikacie,
            "limity": limity_data
        }

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        """Hook pre vlastné API akcie. Mazanie (delete) je delegované na jadro."""
        if action_type == 'send_email':
            if not request.user.has_perm('Evidencia.change_kosikspolozkaminadoobjednanie'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie na generovanie objednávok."}, status=403)
                
            try:
                data = json.loads(request.body)
                items = data.get('items', [])
            except json.JSONDecodeError:
                return JsonResponse({"status": "error", "message": "Neplatný formát dát."}, status=400)

            if not items:
                return JsonResponse({"status": "error", "message": "Neboli vybrané žiadne diely/položky."})

            from ISASaplikacia.models import GlobalneNastavenia
            gn = GlobalneNastavenia.objects.first()
            if not gn or not gn.smtp_user:
                return JsonResponse({
                    "status": "error", 
                    "message": "SMTP údaje nie sú v systéme nastavené. Objednávky neboli vygenerované a položky zostali v košíku."
                })
            
            updates = []
            ids_to_process = []
            for item in items:
                if item.get('id') and str(item['id']).isdigit() and item.get('qty') and str(item['qty']).isdigit():
                    item_id = int(item['id'])
                    ids_to_process.append(item_id)
                    updates.append(When(pk=item_id, then=Value(int(item['qty']))))

            if not ids_to_process:
                return JsonResponse({"status": "error", "message": "Neboli vybrané žiadne platné položky na doobjednanie."}, status=400)
            
            if updates:
                KosikSPolozkamiNaDoobjednanie.objects.filter(
                    id__in=ids_to_process, 
                    objednane=False
                ).update(mnozstvo_na_objednanie=Case(*updates, output_field=IntegerField()))

            from .tasks import task_vygeneruj_a_odosli_objednavky
            base_url = request.build_absolute_uri('/')[:-1]
            
            def _dispatch_objednavky(ids=ids_to_process, u_id=request.user.id, url=base_url):
                try:
                    task_vygeneruj_a_odosli_objednavky.delay(ids, u_id, url)
                except Exception as exc:
                    logger.warning(f"Zlyhalo zaradenie generovania objednávok do Celery: {exc}")
                    messages.warning(request, "⚠️ Objednávky sa nepodarilo zaradiť do fronty na odoslanie (výpadok Celery/Redis).")
            transaction.on_commit(_dispatch_objednavky)

            return JsonResponse({
                "status": "success", 
                "message": "🚀 Objednávky sa generujú a odosielajú na pozadí. Stránka sa čoskoro obnoví."
            })

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    def get_detective_queryset(self, request, term):
        """Hook pre vyhľadávanie v Detective Search."""
        return self.get_queryset(request).select_related('dodavatel').filter(Q(nazov_polozky__icontains=term))[:30]

    def format_detective_result(self, obj):
        """Hook pre formátovanie výsledku v Detective Search."""
        
        cely_nazov = str(obj.nazov_polozky or '').strip()
        cisty_kod = ocisti_nazov_od_jednotky(cely_nazov)
        
        cat_name = "POLOŽKA"
        found = False
        for cat_group in CATEGORY_MAP.values():
            if found: break
            for model_class in cat_group.values():
                if model_class.objects.filter(seriove_cislo__in=[cisty_kod, cely_nazov]).exists():
                    cat_name = getattr(model_class._meta, 'verbose_name', 'POLOŽKA').upper()
                    found = True
                    break
        
        dodavatel = str(obj.dodavatel) if obj.dodavatel else "Neznámy dodávateľ"
        mj = getattr(obj, 'merna_jednotka', 'ks')
        mnozstvo = f"{obj.aktualny_stav} {mj}" if obj.aktualny_stav is not None else ""
                    
        return {
            'id': obj.id, 
            'name': cely_nazov, 
            'search_value': cely_nazov,
            'rola': dodavatel,
            'suma': mnozstvo,
            'cat': cat_name
        }

    def get_urls(self):
        """Registrácia vlastných endpointov. fast-action a detective-search rieši jadro."""
        urls = super().get_urls()
        custom_urls = [
            path('settings-api/', self.admin_site.admin_view(self.settings_action_api), name='kosik-settings-api'),
            path('check-status/', self.admin_site.admin_view(self.check_status_api), name='kosik-check-status'),
        ]
        return custom_urls + urls

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "aktualny_stav":
            kwargs["initial"] = 0
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "filialka":
            kwargs["queryset"] = Filialka.objects.filter(je_aktivna=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_queryset(self, request):
        if hasattr(request, '_isas_kosik_qs_processed'):
            return super().get_queryset(request)
        request._isas_kosik_qs_processed = True
        
        vsetky_limity = { limit.kategoria_nazov: limit.kriticky_limit for limit in LimitKategorieSkladu.objects.all() }
        
        nevybavene = list(KosikSPolozkamiNaDoobjednanie.objects.filter(objednane=False).select_related('filialka', 'dodavatel'))
        
        if not nevybavene:
            return super().get_queryset(request)
            
        kody_v_kosiku = set()
        for polozka in nevybavene:
            cely_nazov = str(polozka.nazov_polozky or '').strip()
            cisty_kod = ocisti_nazov_od_jednotky(cely_nazov)
            if cisty_kod: kody_v_kosiku.add(cisty_kod)
            if cely_nazov: kody_v_kosiku.add(cely_nazov)
            
        # 1. Získame "Fyzickú pravdu" z Hlavného skladu (Toto potrebujeme pre získanie dodávateľa a podkategórie pre VŠETKY položky)
        fyzicke_dielce_db = {}
        if kody_v_kosiku:
            for cat, subcats in CATEGORY_MAP.items():
                for sub, model_class in subcats.items():
                    najdene_diely = model_class.objects.select_related('dodavatel').filter(seriove_cislo__in=list(kody_v_kosiku))
                    for diel in najdene_diely:
                        kod = str(diel.seriove_cislo).strip()
                        fyzicke_dielce_db[kod] = (diel.pocet_na_sklade, getattr(diel, 'dodavatel', None), sub)

        # 🟢 2. Prednačítame si stavy filiálok do Cache (Aby sme nevolali Kalkulačku v cykle 100x!)
        cache_filialok = {}
        for polozka in nevybavene:
            if polozka.filialka and polozka.filialka.id not in cache_filialok:
                cache_filialok[polozka.filialka.id] = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(polozka.filialka)

        polozky_na_update = []
        polozky_na_zmazanie_ids = []
        
        # 3. Vyhodnotenie s Magickou Výhybkou
        for polozka in nevybavene:
            cely_nazov = str(polozka.nazov_polozky or '').strip()
            cisty_kod = ocisti_nazov_od_jednotky(cely_nazov)
            
            # Bez ohľadu na to, či je to filiálka alebo nie, meta-dáta (dodávateľ, podkategória) čerpáme z fyzickej DB
            meta_data = fyzicke_dielce_db.get(cisty_kod) or fyzicke_dielce_db.get(cely_nazov)
            
            if meta_data:
                fyzicky_stav, novy_dodavatel, nazov_podkategorie = meta_data

                # 🚀 Uložíme kategóriu do pamäte objektu pre get_kategoria_styled
                polozka._vypocitana_kategoria = nazov_podkategorie
                
                # 🟢 MAGICKÁ VÝHYBKA PRE AKTUÁLNY STAV
                if polozka.filialka:
                    # Sme na filiálke -> Pýtame sa účtovnej knihy (Ledgeru)
                    stav_slovnik = cache_filialok.get(polozka.filialka.id, {})
                    realy_stav = stav_slovnik.get(cisty_kod) or stav_slovnik.get(cely_nazov) or 0
                else:
                    # Sme na Hlavnom sklade -> Pýtame sa modelu
                    realy_stav = fyzicky_stav if fyzicky_stav is not None else 0

                zmenene = False
                
                # Update stavu z Ledgeru / Hlavného Skladu
                if polozka.aktualny_stav != realy_stav:
                    polozka.aktualny_stav = realy_stav
                    zmenene = True
                
                # Update dodávateľa (Tento platí univerzálne)
                if novy_dodavatel and polozka.dodavatel != novy_dodavatel:
                    polozka.dodavatel = novy_dodavatel
                    zmenene = True
                
                if zmenene:
                    polozky_na_update.append(polozka)
            
                # 🟢 Kontrola limitov z modálu bez akéhokoľvek hardkódu (s podporou emoji)
                limit = vsetky_limity.get(nazov_podkategorie)
                if limit is None:
                    clean_kluc = ''.join(c for c in nazov_podkategorie if c.isalnum() or c.isspace()).strip()
                    limit = vsetky_limity.get(clean_kluc, 2)
                
                aktualny_stav_num = polozka.aktualny_stav if polozka.aktualny_stav is not None else 0
                if aktualny_stav_num > limit:
                    polozky_na_zmazanie_ids.append(polozka.id)
                    
        if polozky_na_zmazanie_ids:
            # Položka sa z košíka automaticky zmaže, ak jej stav stúpol nad limit z modálu
            KosikSPolozkamiNaDoobjednanie.objects.filter(id__in=polozky_na_zmazanie_ids, objednane=False).delete()
            
        if polozky_na_update:
            KosikSPolozkamiNaDoobjednanie.objects.bulk_update(polozky_na_update, ['aktualny_stav', 'dodavatel'])
            
        return super().get_queryset(request)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj:
            fields = ['nazov_polozky', 'aktualny_stav', 'dodavatel', 'datum_zistenia', 'objednane', 'datum_odoslania', 'merna_jednotka', 'suvisiaca_objednavka']
            if obj.objednane:
                fields.append('mnozstvo_na_objednanie')
            return list(set(readonly + fields))
        return list(set(readonly + ['objednane', 'datum_odoslania', 'suvisiaca_objednavka', 'merna_jednotka']))

    def check_status_api(self, request):
        ids_str = request.GET.get('ids', '')
        if not ids_str:
            return JsonResponse({})
            
        id_list = [int(x) for x in ids_str.split(',') if x.isdigit()]
        items = KosikSPolozkamiNaDoobjednanie.objects.filter(id__in=id_list).values('id', 'objednane', 'datum_odoslania')
        
        data = {}
        for item in items:
            data[str(item['id'])] = {
                'objednane': item['objednane'],
                'datum': item['datum_odoslania'].strftime("%d. %m. %Y %H:%M") if item['datum_odoslania'] else ""
            }
        return JsonResponse(data)

    def settings_action_api(self, request):
        if not request.user.is_superuser and not request.user.has_perm('Evidencia.change_globalnenastavenieskladu'):
            return JsonResponse({"status": "error", "message": "Zabezpečenie: Nemáte oprávnenie meniť globálne nastavenia skladu."}, status=403)

        if request.method == "POST":
            try:
                data = json.loads(request.body)
                
                # Uloženie notifikácií (Ošetrené voči Race Condition)
                GlobalneNastavenieSkladu.objects.update_or_create(
                    pk=1,
                    defaults={'zapnute_notifikacie': data.get('notifikacie', True)}
                )

                # Uloženie limitov
                limity = data.get('limity', {})
                for cat_name, val in limity.items():
                    LimitKategorieSkladu.objects.update_or_create(
                        kategoria_nazov=cat_name,
                        defaults={'kriticky_limit': int(val)}
                    )

                return JsonResponse({"status": "success", "message": "⚙️ Nastavenia boli úspešne uložené!"})
            except Exception as e:
                return JsonResponse({"status": "error", "message": str(e)}, status=400)
        return JsonResponse({"status": "invalid"}, status=400)

    @admin.display(description='🗂️ Kategória')
    def get_kategoria_styled(self, obj):        
        kategoria_nazov = getattr(obj, '_vypocitana_kategoria', None)
        
        if not kategoria_nazov:
            cely_nazov = str(obj.nazov_polozky or '').strip()
            cisty_nazov = cely_nazov.split("(")[0].strip()
            found = False
            for cat_group in CATEGORY_MAP.values():
                if found: break
                for sub_name, model_class in cat_group.items():
                    if model_class.objects.filter(seriove_cislo__iexact=cisty_nazov).exists() or model_class.objects.filter(seriove_cislo__iexact=cely_nazov).exists():
                        kategoria_nazov = sub_name
                        found = True
                        break
            
            if not found:
                kategoria_nazov = 'Nezaradené ❓'

        color_themes = [
            ('#eff6ff', '#1e40af', '#bfdbfe'),
            ('#ecfdf5', '#065f46', '#a7f3d0'),
            ('#fef2f2', '#991b1b', '#fecaca'),
            ('#faf5ff', '#581c87', '#e9d5ff'),
            ('#fff7ed', '#9a3412', '#fed7aa'),
            ('#f0fdfa', '#0f766e', '#99f6e4'),
        ]
        
        vsetky_kategorie = []
        for cat_group in CATEGORY_MAP.values():
            for sub_name in cat_group.keys():
                vsetky_kategorie.append(sub_name)

        bg_color, text_color, border_color = '#f8fafc', '#475569', '#e2e8f0'
        if kategoria_nazov in vsetky_kategorie:
            theme_idx = vsetky_kategorie.index(kategoria_nazov) % len(color_themes)
            bg_color, text_color, border_color = color_themes[theme_idx]

        return render_isas_badge(
            text=kategoria_nazov,
            custom_bg=bg_color,
            custom_text=text_color,
            custom_border=border_color,
            wrapper=False
        )

    @admin.display(description='📦 Položka na doobjednanie', ordering='nazov_polozky')
    def get_nazov_polozky_ultimate(self, obj):
        cely_nazov = str(obj.nazov_polozky or '').strip()
        return render_isas_badge(
            text=cely_nazov,
            icon="📦",
            type_color="neutral",
            custom_bg="#ffffff",
            custom_border="#e2e8f0",
            custom_shadow="rgba(0,0,0,0.03)",
            wrapper=False
        )

    @admin.display(description='🏢 Sklad / Filiálka', ordering='filialka')
    def styled_filialka_kosik(self, obj):
        if obj.filialka:
            return render_isas_badge(
                text=obj.filialka.nazov,
                icon="🏪",
                type_color="success",
                wrapper=False
            )
            
        hlavny_sklad = Sklad.objects.first()
        nazov_hlavneho = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        
        return render_isas_badge(
            text=nazov_hlavneho,
            icon="🏭",
            type_color="neutral",
            wrapper=False
        )

    @admin.display(description='📉 Kritický stav', ordering='aktualny_stav')
    def get_aktualny_stav_ultimate(self, obj):
        stav = obj.aktualny_stav if obj.aktualny_stav is not None else 0
        jednotka = getattr(obj, 'merna_jednotka', 'ks').upper()

        return render_isas_badge(
            text=stav,
            suffix_html=render_isas_text(jednotka, size=10, weight=900, custom_style="text-transform: uppercase;"),
            type_color="danger",
            wrapper=False
        )

    @admin.display(description='🏢 Dodávateľ', ordering='dodavatel')
    def get_dodavatel_chip(self, obj):
        if not obj.dodavatel:
            return render_isas_badge(
                text="NEZADANÝ",
                icon="🚫",
                type_color="neutral",
                custom_text="#94a3b8",
                wrapper=False
            )
        
        initial = str(obj.dodavatel)[0].upper()
        return render_isas_badge(
            text=str(obj.dodavatel),
            prefix_text=initial,
            type_color="info",
            wrapper=False
        )

    @admin.display(description='🕒 Zistené', ordering='datum_zistenia')
    def get_datum_zistenia_styled(self, obj):
        if obj.datum_zistenia:
            return render_isas_text(
                text=obj.datum_zistenia.strftime("%d. %m. %Y %H:%M"),
                icon="🕒",
                size=13,
                muted=True
            )
        return ""

    @admin.display(description='🛒 Objednať')
    def upravitelne_mnozstvo_premium(self, obj):
        jednotka = getattr(obj, 'merna_jednotka', 'ks').upper()

        if obj.objednane:
            return render_isas_badge(
                text=obj.mnozstvo_na_objednanie,
                suffix_html=render_isas_text(jednotka, size=10, weight=900, custom_style="text-transform: uppercase;"),
                type_color="neutral",
                wrapper=False
            )
        
        return render_isas_input_number(
            name=f"mnozstvo_{obj.id}",
            value=obj.mnozstvo_na_objednanie,
            suffix=jednotka
        )

    @admin.display(description='🚦 Stav', ordering='objednane')
    def stav_objednavky_badge(self, obj):
        if obj.objednane:
            badge = render_isas_badge("Vybavená", icon="✅", type_color="success", wrapper=False)
            if obj.suvisiaca_objednavka:
                url = reverse('admin:Evidencia_odoslanaobjednavka_change', args=[obj.suvisiaca_objednavka.id])
                order_btn = render_isas_link_btn(url, obj.suvisiaca_objednavka.cislo_objednavky, icon="📄", type_color="neutral", wrapper=False)
                return render_isas_layout(format_html('{}{}', badge, order_btn), layout="col", gap=2, center=True)
            return badge
        return render_isas_badge(
            text="Čaká na objednanie",
            icon="⏳",
            type_color="warning",
            wrapper=False
        )

    @admin.display(description='📤 Odoslané', ordering='datum_odoslania')
    def zobraz_cas_odoslania_styled(self, obj):
        if not obj.datum_odoslania: 
            return render_isas_text(text="·", size=16, muted=True)
        return render_isas_text(
            text=obj.datum_odoslania.strftime("%d. %m. %Y %H:%M"),
            icon="📤",
            size=13,
            color="#0284c7"
        )

class PravidloServisnehoIntervaluInline(ISASPremiumInlineMixin, admin.TabularInline):
    model = PravidloServisnehoIntervalu
    extra = 0
    verbose_name = "📅 Pravidlo servisného intervalu"
    verbose_name_plural = "📅 Pravidlá servisných intervalov"
    isas_clean_choice_fields = {'kategoria_zariadenia': '🏷️ Vyberte kategóriu...'}

@admin.register(TypServisuPrace)
class TypServisuPraceAdmin(ISASPremiumAdmin):    
    list_display = (
        'get_nazov_card', 
        'get_uctovanie_badge', 
        'get_financie_stats', 
        'get_priorita_info',
        'get_web_status'
    )   
    list_display_links = ('get_nazov_card',)
    list_filter = (
        'sposob_uctovania',
        'je_fakturovatelna',
        'priorita',
        'zobrazit_na_webe',
        'povolit_online_booking',
    )
    search_fields = ('nazov', 'pracovny_postup')
    readonly_fields = ('slug',)
    inlines = [PravidloServisnehoIntervaluInline]
        
    fieldsets = (
        ('🛠️ Základná identifikácia služby', {
            'fields': (
                'nazov',
                'je_fakturovatelna',
                ('normohodiny', 'sposob_uctovania')
            ),
            'description': (
                'Definujte názov úkonu a jeho základné správanie v systéme. ⚙️ '
                'Normohodiny sú kritické pre plánovanie kapacity technikov v kalendári, '
                'aj keď je služba účtovaná fixne. ⏳'
            )
        }),
        ('💰 Predajná matica (Ceny pre zákazníka)', {
            'fields': (
                ('hodinova_sadzba_predaj', 'fixna_cena_predaj'),
            ),
            'description': (
                'Toto sú sumy, ktoré uvidí zákazník na cenovej ponuke a faktúre. 💼 '
                'Zadávajte sumy BEZ DPH. Systém automaticky použije správnu cenu podľa zvoleného režimu účtovania. 🎯'
            )
        }),
        ('📉 Nákladová matica (True-Margin Engine)', {
            'fields': (
                ('hodinova_sadzba_naklad', 'fixny_naklad'),
            ),
            'description': (
                'Tieto údaje sú pred zákazníkom prísne skryté! 🔒 '
                'Slúžia výhradne pre finančného riaditeľa na výpočet skutočnej ziskovosti (Marže) danej služby. 💰 '
                'Zahrňte sem mzdu technika, odvody, alebo poplatky subdodávateľom.'
            )
        }),
        ('📋 Exekutíva a Zákaznícky servis', {
            'fields': (
                ('priorita', 'zaruka_mesiace'),
                'pracovny_postup',
            ),
            'description': (
                'Nastavenia pre samotný výkon práce. 🚨 Priorita pomáha radiť úlohy v kalendári. '
                'Pracovný postup (SOP) bude automaticky odoslaný technikovi na Telegram, keď mu pridelíte túto prácu. 🤖'
            )
        }),
        ('🌐 Web Builder (Verejná prezentácia)', {
            'fields': (
                ('zobrazit_na_webe', 'zverejnit_postup_na_webe'),
                ('povolit_online_booking', 'booking_trvanie_min'),
                'popis_na_webe',
                'slug'
            ),
            'description': (
                'Nastavenia pre zobrazenie tejto služby na vašej verejnej webstránke. 🌍 '
                'Ak je služba skrytá, zákazníci ju na webe neuvidia. Popis by mal byť marketingovo pútavý.'
            )
        }),
    )

    isas_title = "🛠️ Typy servisov/prác"
    isas_preloader_text = "⏳ Vstupujete do zoznamu typov servisov/prác..."
    isas_delete_iterate = True
    
    isas_autofill_rules = [
        {
            'action': 'unlock_fields',
            'trigger_field': 'povolit_online_booking',
            'target_fields': ['booking_trvanie_min'],
            'restore_on_unlock': True,
        }
    ]

    isas_value_based_locks = {
        'trigger_field': 'sposob_uctovania',
        'rules': {
            'FIX': {
                'lock': ['hodinova_sadzba_predaj', 'hodinova_sadzba_naklad', 'normohodiny'],
                'unlock': ['fixna_cena_predaj', 'fixny_naklad'],
                'clear_value': '0.00'
            }
        },
        'default_rule': {
            'lock': ['fixna_cena_predaj', 'fixny_naklad'],
            'unlock': ['hodinova_sadzba_predaj', 'hodinova_sadzba_naklad', 'normohodiny'],
            'clear_value': '0.00'
        }
    }
    isas_delete_success_message = "✨ Služby úspešne odstránené zo systému!"
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "🛠️",
    }

    def get_detective_queryset(self, request, term):
        """Hook pre vyhľadávanie v Detective Search."""
        return self.get_queryset(request).filter(
            Q(nazov__icontains=term) | 
            Q(pracovny_postup__icontains=term)
        ).order_by('nazov')[:30]

    def format_detective_result(self, obj):
        """Hook pre formátovanie výsledkov Detective Search."""
        
        safe_name = obj.nazov
        rezim = "Fixná cena" if obj.sposob_uctovania == 'FIX' else "Hodinová sadzba"
        mena_s = get_firemna_mena()
        cena = f"{obj.fixna_cena_predaj or Decimal('0.00')} {mena_s}" if obj.sposob_uctovania == 'FIX' else f"{obj.hodinova_sadzba_predaj or Decimal('0.00')} {mena_s} / hod"

        return {
            'id': obj.id, 
            'name': safe_name, 
            'search_value': safe_name,
            'suma': cena, 
            'rola': f"⚖️ Režim: {rezim}",
            'cat': '🛠️ SLUŽBA'
        }

    @admin.display(description='🛠️ Názov služby & Čas', ordering='nazov')
    def get_nazov_card(self, obj):        
        cas_badge = render_isas_badge(
            text=f"{obj.normohodiny} Normohodín",
            icon="⏳",
            type_color="neutral",
            wrapper=False
        )
        
        return render_isas_avatar(
            icon="🔧",
            title=obj.nazov,
            subtitle=None,
            bg_gradient="#2563eb, #1d4ed8",
            shadow_color="rgba(37, 99, 235, 0.25)",
            extra_html=cas_badge
        )

    @admin.display(description='🌐 Web', ordering='zobrazit_na_webe')
    def get_web_status(self, obj):
        
        is_web_active = bool(getattr(obj, 'zobrazit_na_webe', False))
        status_badge = (
            render_isas_badge("VEREJNÉ", icon="🌍", type_color="success", wrapper=False)
            if is_web_active
            else render_isas_badge("SKRYTÉ", icon="🔒", type_color="neutral", wrapper=False)
        )
        
        content = [status_badge]
        if getattr(obj, 'slug', None):
            content.append(render_isas_badge(f"/{obj.slug}", type_color="neutral", monospace=True, wrapper=False))
            
        if is_web_active:
            if getattr(obj, 'povolit_online_booking', True):
                if getattr(obj, 'booking_trvanie_min', None):
                    content.append(render_isas_badge(f"BOOKING: {obj.booking_trvanie_min} MIN.", icon="📅", type_color="info", wrapper=False))
                else:
                    content.append(render_isas_badge("BOOKING: BLOKY", icon="📅", type_color="info", wrapper=False))
            else:
                content.append(render_isas_badge("BEZ BOOKINGU", icon="🚫", type_color="neutral", wrapper=False))

        # 🛡️ DEFENZÍVNA KONTROLA: Zverejnenie SOP na webe vrátane overenia obsahu
        if is_web_active and getattr(obj, 'zverejnit_postup_na_webe', False):
            has_postup_text = bool(getattr(obj, 'pracovny_postup', None) and str(obj.pracovny_postup).strip())
            if has_postup_text:
                content.append(render_isas_badge("SOP NA WEBE", icon="📋", type_color="info", wrapper=False))
            else:
                content.append(render_isas_badge("SOP PRÁZDNE", icon="⚠️", type_color="warning", wrapper=False))

        return render_isas_layout(mark_safe("".join(content)), layout="col", gap=4, center=True)

    @admin.display(description='⚖️ Režim účtovania', ordering='sposob_uctovania')
    def get_uctovanie_badge(self, obj):        
        if not obj.je_fakturovatelna:
            return render_isas_badge(
                text="Nefakturovateľné (Réžia)",
                icon="⛔",
                type_color="neutral",
                custom_border="#cbd5e1",
                wrapper=False
            )
            
        if obj.sposob_uctovania == 'FIX':
            return render_isas_badge(
                text="FIXNÁ CENA",
                icon="🎯",
                type_color="danger",
                wrapper=False
            )
        else:
            return render_isas_badge(
                text="HODINOVKA",
                icon="⏱️",
                type_color="info",
                wrapper=False
            )

    @admin.display(description='💰 Ziskovosť (True-Margin)')
    def get_financie_stats(self, obj):
        if obj.sposob_uctovania == 'FIX':
            predaj = obj.fixna_cena_predaj or Decimal('0.00')
            naklad = obj.fixny_naklad or Decimal('0.00')
        else:
            predaj = obj.hodinova_sadzba_predaj or Decimal('0.00')
            naklad = obj.hodinova_sadzba_naklad or Decimal('0.00')
            
        zisk = predaj - naklad
        marza = (zisk / predaj * Decimal('100.00')) if predaj > Decimal('0.00') else Decimal('0.00')
        
        farba_marze = "#0284c7" if marza >= 30 else ("#b45309" if marza > 0 else "#b91c1c")

        mena_s = get_firemna_mena()
        return render_isas_mini_stat_card(
            top_label="Predajná cena",
            top_val=f"{predaj} {mena_s}",
            bot_label="Interný náklad",
            bot_val=f"{naklad} {mena_s}",
            third_label="Čistá Marža",
            third_val=f"{marza:.1f}%",
            third_val_color=farba_marze
        )

    @admin.display(description='🚨 Priorita & Záruka', ordering='priorita')
    def get_priorita_info(self, obj):
        if obj.priorita == 'VYSOKA':
            prio_badge = render_isas_badge(text="KRITICKÁ", icon="🔴", type_color="danger", wrapper=False)
        elif obj.priorita == 'STREDNA':
            prio_badge = render_isas_badge(text="ŠTANDARD", icon="🟡", type_color="warning", wrapper=False)
        else:
            prio_badge = render_isas_badge(text="RUTINA", icon="🟢", type_color="success", wrapper=False)

        zaruka_badge = render_isas_badge(
            text=f"ZÁRUKA: {obj.zaruka_mesiace} MES.",
            icon="🛡️",
            type_color="neutral",
            wrapper=False
        )
        
        if obj.pracovny_postup:
            sop_text = render_isas_text(text="📋 SOP Priložené", size=11, weight=700, color="#64748b")
        else:
            sop_text = render_isas_text(text="Bez postupu", size=10, weight=600, muted=True)

        return render_isas_layout(
            content_html=format_html('{}{}{}', prio_badge, zaruka_badge, sop_text),
            layout="col",
            gap=4,
            center=True
        )

@admin.register(Uloha)
class UlohaAdmin(ISASPremiumAdmin):
    form = UlohaAdminForm

    autocomplete_fields = ['technici', 'zakaznik']
    list_prefetch_related = ('typ_servisu', 'zariadenia', 'technici')
    list_select_related = ('zakaznik',)
    list_display = [
        'styled_nazov', 
        'styled_zakaznik', 
        'styled_zariadenia', 
        'formatted_zaciatok', 
        'styled_technici', 
        'status_badge'
    ]
    list_filter = [
        'dokoncene', 
        ISASDateRangeFilter, 
        ISASHasMaterialFilter, 
        ISASRelatedTypServisuFilter, 
        ISASRelatedTechnikFilter, 
        ISASRelatedZakaznikFilter, 
        ISASRelatedZariadenieFilter,
        'je_reklamacia'
    ]
    isas_date_filter_field = 'zaciatok'
    isas_zariadenie_filter_field = 'zariadenia'
    isas_material_filter_field = 'pouzite_polozky'
    search_fields = ['zakaznik__Nazov_spolocnosti', 'blizsie_informacie']    
    
    fieldsets = (
        ('🏢 LOKALIZÁCIA ZÁSAHU', {
            'fields': ('zakaznik', 'zariadenia'),
            'description': (
                'Vyberte zákazníka a konkrétne zariadenia, ktorých sa servis týka. 📍 '
                'Ak zákazník vyžaduje povolenie na vstup, systém na to technikov automaticky upozorní. 🚧'
            )
        }),
        ('📅 ČASOVÉ PLÁNOVANIE', {
            'fields': (
                ('zaciatok', 'koniec'), 
                'farba'
            ),
            'description': (
                'Nastavte presný časový rámec výjazdu. ⏱️ '
                'Zvolená farba pomôže dispečerovi rýchlo identifikovať prioritu alebo typ úlohy priamo v kalendári. 🎨'
            )
        }),
        ('🛠️ ZADANIE PRÁCE A TÍM', {
            'fields': (
                'typ_servisu', 
                'technici',
                'blizsie_informacie'
            ),
            'description': (
                'Špecifikujte typ práce a priraďte technikov. 👨‍🔧 '
                'Do bližších informácií uveďte dôležité inštrukcie, ktoré technici uvidia priamo vo svojom mobile. 📱'
            )
        }),
        ('⚠️ REKLAMAČNÝ MODUL (ISO 9001)', {
            'fields': (
                ('je_reklamacia', 'datum_prijatia_reklamacie'),
            ),
            'description': (
                '🛠️ <b>Riešenie reklamácie:</b> Začiarknutím označíte, že táto úloha sa rieši v rámci reklamácie. ⚠️ '
            )
        }),
        ('📲 OZNÁMENIE O PRIDELENÍ (ZAMESTNANCI)', {
            'fields': (('notif_technik_tg', 'notif_technik_mail', 'notif_technik_discord'),),
            'description': (
                '🚀 <b>Okamžitá synchronizácia:</b> Tieto notifikácie sa odošlú technikom v momente, '
                'keď im pridelíte alebo upravíte túto úlohu. Obsahujú všetky detaily vrátane zoznamu materiálu. 📦'
            )
        }),
        ('✅ UZAVRETIE A INFO PRE VEDENIE', {
            'fields': (
                'kontrolovat_sklad_pri_ulozeni',
                'dokoncene', 
                ('notif_admin_tg', 'notif_admin_mail', 'notif_admin_discord')
            ),
            'classes': ('vivid-box-flash',),
            'description': (
                '🏁 <b>Ukončenie procesu:</b> Po označení úlohy ako dokončenej systém automaticky '
                'informuje vedenie (majiteľa) a uzavrie tento servisný prípad. 📊'
            )
        }),
    )

    inlines = [PouzityMaterialInline]

    isas_title = "📋 Úlohy a práce"
    isas_preloader_text = "⏳ Vstupujete do zoznamu úloh a prác..."
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "🏢",
        'custom_buttons_html': '<a id="btn-kniha-uloh" href="kniha-uloh/" class="isas-action-btn btn-blue">📖 KNIHA ÚLOH</a><button type="button" id="btn-complete-selected" class="isas-action-btn btn-green">✅ Označiť ako dokončené</button>',
        'ignore_default_action_ids': ["btn-kniha-uloh", "btn-complete-selected"],
    }
    
    isas_action_modals = {
        'btn-complete-selected': {
            'type': 'confirm_checkbox_modal',
            'action': 'complete',
            'icon': '✅',
            'title': 'Dokončenie úloh',
            'subtitle': 'Chystáte sa označiť vybrané úlohy ako dokončené.',
            'checkbox_label': 'Odoslať notifikácie majiteľovi 📱',
            'checkbox_sublabel': '(Po odškrtnutí políčka sa notifikácia neodošle)',
            'checkbox_id': 'send_notifs',
            'confirm_text': 'Vykonať',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jednu úlohu zo zoznamu.'
        }
    }

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        """Hook pre vlastné API akcie. Mazanie (delete) je delegované na jadro."""
        if action_type == 'complete':
            if not request.user.has_perm('Evidencia.change_uloha'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať úlohy."}, status=403)
            
            try:
                data = json.loads(request.body)
                send_notifs = data.get('send_notifs', True)
            except json.JSONDecodeError:
                send_notifs = True
            
            with transaction.atomic():
                ulohy = self.model.objects.select_for_update().filter(id__in=valid_ids)
                ulohy_na_update = []
                for obj in ulohy:
                    obj.dokoncene = True
                    ulohy_na_update.append(obj)
                
                if ulohy_na_update:
                    self.model.objects.bulk_update(ulohy_na_update, ['dokoncene'])
                    
            if send_notifs:
                from .tasks import uloha_odoslat_notifikacie_adminovi_task

                def _dispatch_uloha_admin_notif(v_ids=None):
                    if v_ids is None:
                        v_ids = list(valid_ids)
                    try:
                        uloha_odoslat_notifikacie_adminovi_task.delay(v_ids)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo odoslanie notifikácií dokončenia úloh do Celery: {exc}")
                        messages.warning(request, "⚠️ Úlohy boli dokončené, ale notifikácie neboli odoslané (výpadok Celery/Redis).")
                transaction.on_commit(_dispatch_uloha_admin_notif)
                    
            count = len(valid_ids)
            word = "úlohu" if count == 1 else ("úlohy" if 2 <= count <= 4 else "úloh")
            stav_notif = "(S notifikáciou 📱)" if send_notifs else "(Tichý režim 🔕)"
            return JsonResponse({"status": "success", "message": f"✅ Úspešne označené {count} {word} ako dokončené {stav_notif}!"})

        # Fallback na jadro (vyrieši 'delete' a iné globálne akcie)
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    def get_detective_queryset(self, request, term):
        """Hook pre vyhľadávanie v Detective Search."""
        return self.get_queryset(request).select_related(
            'zakaznik'
        ).prefetch_related(
            'technici'
        ).filter(
            Q(zakaznik__Nazov_spolocnosti__icontains=term) |
            Q(zariadenia__vyrobne_cislo__icontains=term) |
            Q(blizsie_informacie__icontains=term)
        ).order_by('-zaciatok')[:30]

    def format_detective_result(self, obj):
        """Hook pre formátovanie výsledku v Detective Search."""
        stav = "✅ Dokončená" if obj.dokoncene else "⏳ Prebieha"
        datum = obj.zaciatok.strftime("%d.%m.%Y %H:%M") if obj.zaciatok else "Neuvedené"
        zakaznik = getattr(obj.zakaznik, 'Nazov_spolocnosti', str(obj.zakaznik)) if obj.zakaznik else "Neuvedené"
        technici = ", ".join([t.priezvisko for t in obj.technici.all()])
        
        return {
            'id': obj.id, 
            'name': zakaznik, 
            'search_value': zakaznik,
            'rola': f"👨‍🔧 {technici}" if technici else "",
            'cat': stav,
            'date': datum
        }

    def get_urls(self):
        """Registrácia vlastných endpointov. fast-action a detective-search rieši jadro."""
        urls = super().get_urls()
        custom_urls = [
            path('json-events/', self.admin_site.admin_view(self.json_events), name='uloha-events'),
            path('kniha-uloh/', self.admin_site.admin_view(self.kniha_uloh_view), name='uloha-kniha'),
        ]
        return custom_urls + urls

    # 🔒 ZAMKNUTIE CELÉHO FORMULÁRA AK JE ÚLOHA DOKONČENÁ
    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.dokoncene:
            # Zozbierame iba reálne polia z modelu (vrátane ManyToMany)
            fields = [f.name for f in self.model._meta.fields]
            m2m_fields = [m2m.name for m2m in self.model._meta.many_to_many]
            return list(set(readonly + fields + m2m_fields))
            
        return readonly

    # 🧹 SKRYTIE VIRTUÁLNYCH POLÍ PRI DOKONČENEJ ÚLOHE
    def get_fieldsets(self, request, obj=None):
        fieldsets = list(super().get_fieldsets(request, obj))
        
        if obj and obj.dokoncene:
            # Ak je úloha dokončená, vymažeme z formulára tie bloky, 
            # ktoré obsahujú virtuálne notifikačné prepínače, aby na nich Django nepadol.
            filtered_fieldsets = []
            for name, opts in fieldsets:
                # Zachováme iba čisté dátové bloky bez notifikačných prepínačov
                if 'notif' not in str(opts.get('fields', [])):
                    filtered_fieldsets.append((name, opts))
            return tuple(filtered_fieldsets)
            
        return super().get_fieldsets(request, obj)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # ISAS ENGINE: Eliminácia N+1 dopytov pri načítaní stĺpcov tabuľky
        return qs.select_related('zakaznik').prefetch_related(
            'typ_servisu', 
            'zariadenia', 
            'technici', 
            'pouzite_polozky'
        )

    def kniha_uloh_view(self, request):
        """Elitný view pre zobrazenie otvorenej knihy úloh pre konkrétneho zamestnanca."""
        
        if not request.user.has_perm('Evidencia.view_uloha'):
            raise PermissionDenied
        zamestnanci = Zamestnanec.objects.all().order_by('meno', 'priezvisko')
        vybrany_zamestnanec_id = request.GET.get('technik_id')
        
        otvorene_ulohy = []
        vybrany_technik = None

        if vybrany_zamestnanec_id:
            try:
                vybrany_technik = Zamestnanec.objects.get(id=vybrany_zamestnanec_id)
                # Získame len nedokončené úlohy, zoradené od najnovších
                otvorene_ulohy = Uloha.objects.filter(
                    technici=vybrany_technik, 
                    dokoncene=False
                ).select_related('zakaznik').prefetch_related('typ_servisu', 'zariadenia').order_by('zaciatok')
            except (Zamestnanec.DoesNotExist, ValueError):
                pass

        context = dict(
            self.admin_site.each_context(request),
            title="📖 Kniha úloh",
            zamestnanci=zamestnanci,
            vybrany_technik=vybrany_technik,
            otvorene_ulohy=otvorene_ulohy,
        )
        return render(request, "admin/kniha_uloh.html", context)

    def save_related(self, request, form, formsets, change):
        
        stary_material = {}
        if change:
            # Kľúče konvertujeme na string, aby prežili JSON serializáciu do Celery
            stary_material = {str(p.id): p.pocet for p in form.instance.pouzite_polozky.all()}
            
        bola_dokoncena_predtym = form.initial.get('dokoncene', False) if change else False

        super().save_related(request, form, formsets, change)

        uloha = form.instance
        
        # --- LOGIKA PRE POVOLENIE NA VSTUP (Zostáva v admin.py, lebo vytvára DB záznamy) ---
        if uloha.zakaznik and getattr(uloha.zakaznik, 'vyzaduje_povolenie_vstupu', False):
            
            dnes = timezone.now().date()
            technici_ulohy = uloha.technici.all()
            problemovi_technici = []

            for t in technici_ulohy:
                bozp_datum = to_date(t.bozp_platne_do)
                if not bozp_datum or bozp_datum < dnes:
                    problemovi_technici.append(f"{t.meno} {t.priezvisko}")

            if problemovi_technici:
                messages.error(request, f"🚨 KRITICKÁ CHYBA: Technici s exspirovaným BOZP ({', '.join(problemovi_technici)}) boli priradení na prísny vstup do {uloha.zakaznik.Nazov_spolocnosti}!")

            bezpecny_datum_vstupu = uloha.zaciatok.date() if uloha.zaciatok else dnes

            try:
                with transaction.atomic():
                    povolenie, created = PovolenieNaVstup.objects.select_for_update().get_or_create(
                        zakaznik=uloha.zakaznik,
                        uloha=uloha,
                        datum_vstupu=bezpecny_datum_vstupu,
                        defaults={'stav': 'GENEROVANE'}
                    )
            except IntegrityError:
                # Fallback ak iný proces práve vytvoril záznam v rovnakej milisekunde
                povolenie = PovolenieNaVstup.objects.get(zakaznik=uloha.zakaznik, uloha=uloha, datum_vstupu=bezpecny_datum_vstupu)
                created = False
            
            povolenie.zamestnanci.set(technici_ulohy)
            auta = Vozidlo.objects.filter(priradeny_zamestnanec__in=technici_ulohy)
            if auta.exists():
                povolenie.vozidla.set(auta)
        
        uloha.refresh_from_db()

        poslat_technikom_tg = form.cleaned_data.get('notif_technik_tg', True)
        poslat_technikom_mail = form.cleaned_data.get('notif_technik_mail', True)
        poslat_technikom_discord = form.cleaned_data.get('notif_technik_discord', True)
        poslat_adminovi_tg = form.cleaned_data.get('notif_admin_tg', True)
        poslat_adminovi_mail = form.cleaned_data.get('notif_admin_mail', True)
        poslat_adminovi_discord = form.cleaned_data.get('notif_admin_discord', True)

        if not uloha.dokoncene:
            from .tasks import task_skontroluj_sklad_jit, task_notifikuj_technikov_o_ulohe
            
            # 1. JIT Kontrola skladu (Nezávislý task)
            def _dispatch_jit(u_id=uloha.id):
                try:
                    task_skontroluj_sklad_jit.delay(u_id)
                except Exception as exc:
                    logger.warning(f"Zlyhala JIT kontrola skladu v Celery pre úlohu #{u_id}: {exc}")
                    messages.warning(request, f"⚠️ JIT kontrolu skladu pre úlohu #{u_id} sa nepodarilo zaradiť do fronty (výpadok Celery/Redis).")
            transaction.on_commit(_dispatch_jit)
            
            # 2. Notifikácia technikom (Nezávislý task)
            if poslat_technikom_tg or poslat_technikom_mail or poslat_technikom_discord:
                def _dispatch_tech_notif(u_id=uloha.id, ch=change, sm=stary_material, ptg=poslat_technikom_tg, pm=poslat_technikom_mail, pdisc=poslat_technikom_discord):
                    try:
                        task_notifikuj_technikov_o_ulohe.delay(u_id, ch, sm, ptg, pm, pdisc)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo odoslanie notifikácie technikom pre úlohu #{u_id} do Celery: {exc}")
                        messages.warning(request, "⚠️ Úloha bola uložená, ale notifikáciu technikom sa nepodarilo odoslať (výpadok Celery/Redis).")
                transaction.on_commit(_dispatch_tech_notif)

        # 3. Notifikácia adminovi (Dokončenie)
        if uloha.dokoncene and not bola_dokoncena_predtym and (poslat_adminovi_tg or poslat_adminovi_mail or poslat_adminovi_discord):
            from .tasks import uloha_odoslat_notifikacie_adminovi_task
            def _dispatch_admin_complete(u_id=uloha.id, ptg=poslat_adminovi_tg, pm=poslat_adminovi_mail, pdisc=poslat_adminovi_discord):
                try:
                    uloha_odoslat_notifikacie_adminovi_task.delay([u_id], ptg, pm, pdisc)
                except Exception as exc:
                    logger.warning(f"Zlyhalo odoslanie notifikácie adminovi pre úlohu #{u_id} do Celery: {exc}")
                    messages.warning(request, "⚠️ Úloha bola dokončená, ale notifikáciu adminovi sa nepodarilo odoslať (výpadok Celery/Redis).")
            transaction.on_commit(_dispatch_admin_complete)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name in ["typ_servisu", "zariadenia"]:
            kwargs["widget"] = forms.CheckboxSelectMultiple()
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def save_formset(self, request, form, formset, change):
        if formset.model == PouzityMaterial:
            with transaction.atomic():
                instances = formset.save(commit=False)
                
                for obj in formset.deleted_objects:
                    obj.delete()

                for inline_form in formset.forms:
                    if (formset.can_delete and formset._should_delete_form(inline_form)) or not inline_form.has_changed():
                        continue

                    instance = inline_form.instance
                    oid = request.POST.get(f"{inline_form.prefix}-object_id")
                    
                    if oid and str(oid).isdigit():
                        instance.object_id = int(oid)
                    if getattr(instance, 'object_id', None):
                        instance.uloha = form.instance
                        instance.save()
                
                formset.save_m2m()
        else:
            super().save_formset(request, form, formset, change)

    @admin.display(description='🗂️ Názov, Technici & ID', ordering='id')
    def styled_nazov(self, obj):        
        typy = ", ".join([str(t) for t in obj.typ_servisu.all()])
        zariadenia_list = ", ".join([str(z) for z in obj.zariadenia.all()])
        technici_list = ", ".join([str(t) for t in obj.technici.all()])
        
        zakaznik_text = obj.zakaznik if obj.zakaznik else "Nešpecifikovaná"
        
        if typy and zariadenia_list: 
            display_text = f"{typy} - {zariadenia_list}"
        elif typy: 
            display_text = f"{typy} - {zakaznik_text}"
        elif zariadenia_list: 
            display_text = f"Úloha - {zariadenia_list}"
        else: 
            display_text = f"Úloha - {zakaznik_text}"
            
        info_html = ""
        if getattr(obj, 'blizsie_informacie', None):
            info_html = render_isas_text("ℹ️", size=14, tooltip=f"Poznámka: {obj.blizsie_informacie}")

        title_html = format_html('{} {}', display_text, info_html)
        
        extra_items = []
        if technici_list:
            extra_items.append(render_isas_badge(technici_list, icon="👨‍🔧", type_color="info"))
            
        extra_items.append(render_isas_text(f"ID ÚLOHY: #{obj.id}", size=10, weight=800, color="#64748b"))
        
        extra_html = render_isas_layout(
            mark_safe("".join(extra_items)),
            layout="col", gap=4, center=True
        )
        
        img_url = None
        if obj.zakaznik and getattr(obj.zakaznik, 'domena', None):
            clean_domain = str(obj.zakaznik.domena).lower().replace('http://', '').replace('https://', '').replace('www.', '').split('/')[0].strip()
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
            if '.' in clean_domain:
                img_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"

        return render_isas_avatar(
            icon="📝",
            title=title_html,
            subtitle=None,
            bg_gradient="#3b82f6, #1d4ed8",
            shadow_color="rgba(59, 130, 246, 0.25)",
            extra_html=extra_html,
            image_url=img_url
        )

    @admin.display(description='🏢 Zákazník', ordering='zakaznik')
    def styled_zakaznik(self, obj):        
        nazov = str(obj.zakaznik) if obj.zakaznik else "Neuvedené"
        
        items = [
            render_isas_text("🏢", size=18),
            render_isas_text(nazov, size=12, weight=800, color="#334155")
        ]
        
        if obj.zakaznik and getattr(obj.zakaznik, 'vyzaduje_povolenie_vstupu', False):
            items.append(render_isas_badge("VSTUPKA", icon="🚧", type_color="warning"))
            
        content = mark_safe("".join(items))
        
        return render_isas_layout(
            content,
            layout="col", gap=4, center=True,
            bg_color="#f8fafc", border_color="#cbd5e1", padding="10px 12px",
            border_radius="12px", shadow="0 2px 4px rgba(0,0,0,0.02)", min_width="120px"
        )

    @admin.display(description='📅 Plánovaný čas', ordering='zaciatok')
    def formatted_zaciatok(self, obj):        
        zaciatok = obj.zaciatok.strftime('%d.%m.%Y %H:%M') if obj.zaciatok else "—"
        koniec = obj.koniec.strftime('%d.%m.%Y %H:%M') if obj.koniec else "—"
        farba_hex = getattr(obj, 'farba', '#3788d8') or '#3788d8'
        
        time_html = render_isas_text(f"🕒 {zaciatok}", size=12, weight=900, color="#3b82f6", dot_color=farba_hex, tooltip="Farba v kalendári")
        
        start_block = render_isas_layout(
            format_html('{}{}', 
                render_isas_text("ZAČIATOK", size=9, weight=800, color="#64748b"),
                time_html
            ), layout="col", gap=2, center=True
        )
        
        end_block = render_isas_layout(
            format_html('{}{}', 
                render_isas_text("KONIEC", size=9, weight=800, color="#64748b"),
                render_isas_text(f"🏁 {koniec}", size=11, weight=800, color="#059669")
            ), layout="col", gap=2, center=True
        )
        
        end_block_with_border = render_isas_singleton_row(end_block, is_dashed=False, border_color="#f1f5f9", padding_top=6)
        
        return render_isas_layout(
            format_html('{}{}', start_block, end_block_with_border),
            layout="col", gap=6, center=True,
            bg_color="#ffffff", border_color="#e2e8f0", padding="10px 14px",
            border_radius="12px", shadow="0 2px 6px rgba(0,0,0,0.03)", min_width="130px"
        )

    @admin.display(description='🚦 Stav', ordering='dokoncene')
    def status_badge(self, obj):        
        items = []
        
        if getattr(obj, 'je_reklamacia', False):
            datum_val = getattr(obj, 'datum_prijatia_reklamacie', None)
            if datum_val and hasattr(datum_val, 'strftime'):
                datum_rek = datum_val.strftime('%d.%m.%Y')
            elif datum_val:
                datum_rek = str(datum_val)[:10]
            else:
                datum_rek = 'Nezadaný dátum'

            # 🛡️ DEFENZÍVNA KONTROLA ZMLUVNEJ SLA LEHOTY ZÁKAZNÍKA
            sla_text = ""
            zakaznik = getattr(obj, 'zakaznik', None)
            if zakaznik:
                lehota = getattr(zakaznik, 'reklamacna_lehota_dni', None)
                if lehota is not None:
                    try:
                        lehota_int = int(lehota)
                        if lehota_int > 0:
                            word = "deň" if lehota_int == 1 else ("dni" if 2 <= lehota_int <= 4 else "dní")
                            sla_text = f" | SLA: {lehota_int} {word}"
                    except (TypeError, ValueError):
                        pass

            items.append(render_isas_badge("REKLAMÁCIA", icon="⚠️", type_color="danger", tooltip=f"Prijaté: {datum_rek}{sla_text}"))
            
        if getattr(obj, 'dokoncene', False):
            items.append(render_isas_badge("DOKONČENÁ", icon="✅", type_color="success"))
        else:
            items.append(render_isas_badge("PREBIEHA", icon="⏳", type_color="warning"))
            
        return render_isas_layout(
            mark_safe("".join(items)),
            layout="col", gap=6, center=True
        )

    @admin.display(description='👨‍🔧 Priradení zamestnanci')
    def styled_technici(self, obj):        
        technici = obj.technici.all()
        if not technici:
            return render_isas_text("Neobsadené", size=11, weight=800, color="#94a3b8")
            
        names = []
        for t in technici:
            full_name = f"{getattr(t, 'meno', '')} {getattr(t, 'priezvisko', '')}".strip()
            if not full_name: full_name = str(t)
            names.append(render_isas_text(full_name, size=11, weight=800, color="#166534"))
            
        names_layout = render_isas_layout(mark_safe("".join(names)), layout="col", gap=2, center=True)
        
        content = format_html('{}{}', render_isas_text("👨‍🔧", size=16), names_layout)
        
        return render_isas_layout(
            content,
            layout="col", gap=2, center=True,
            bg_color="#f0fdf4", border_color="#86efac", padding="8px 14px",
            border_radius="12px", shadow="0 2px 4px rgba(0,0,0,0.02)", min_width="120px"
        )

    @admin.display(description='⚙️ Zariadenia')
    def styled_zariadenia(self, obj):        
        zariadenia = obj.zariadenia.all()
        if not zariadenia:
            return render_isas_text("—", size=11, weight=800, color="#cbd5e1")
            
        pills = []
        for z in zariadenia:
            pills.append(render_isas_badge(str(z), type_color="neutral"))
            
        pills_layout = render_isas_layout(mark_safe("".join(pills)), layout="col", gap=4, center=True)
        
        content = format_html('{}{}', render_isas_text("⚙️", size=16), pills_layout)
        
        return render_isas_layout(
            content,
            layout="col", gap=6, center=True,
            bg_color="#f8fafc", border_color="#cbd5e1", padding="8px 10px",
            border_radius="12px", shadow="0 2px 4px rgba(0,0,0,0.02)", min_width="130px"
        )

    def json_events(self, request):
        if not request.user.has_perm('Evidencia.view_uloha'):
            return JsonResponse({'error': '🚫 Nemáte oprávnenie na zobrazenie úloh kalendára.'}, status=403)
        
        start_param = request.GET.get('start')
        end_param = request.GET.get('end')
        
        qs = Uloha.objects.all().select_related('zakaznik').prefetch_related('technici', 'typ_servisu', 'zariadenia')
        
        if start_param and end_param:
            start_date = parse_datetime(start_param)
            end_date = parse_datetime(end_param)
            if start_date and end_date:
                c_start = timezone.localtime(start_date).replace(tzinfo=None) if timezone.is_aware(start_date) else start_date
                c_end = timezone.localtime(end_date).replace(tzinfo=None) if timezone.is_aware(end_date) else end_date
                qs = qs.filter(Q(zaciatok__range=(c_start, c_end)) | Q(koniec__range=(c_start, c_end)) | (Q(zaciatok__lt=c_start) & Q(koniec__gt=c_end)))

        events = qs
        
        data = []
        for e in events:
            typy = ", ".join([str(t).replace('[Fix]', '').replace('(Fix)', '').strip() for t in e.typ_servisu.all()])
            stroje = ", ".join([str(z) for z in e.zariadenia.all()]) or 'Neuvedené'
            technici = ", ".join([str(m) for m in e.technici.all()])
            zakaznik_meno = e.zakaznik.Nazov_spolocnosti if e.zakaznik else 'Neuvedený'
            cas_start = e.zaciatok.strftime("%H:%M") if e.zaciatok else "--:--"
            
            full_title = (
                f"⏰ {cas_start} | 🛠 {typy}\n"
                f"────────────────\n"
                f"🏢 ZÁKAZNÍK: {zakaznik_meno}\n"
                f"⚙️ ZARIADENIE: {stroje}\n"
                f"👨‍🔧 TECHNIK: {technici}"
            )
            
            start_iso = e.zaciatok.isoformat() if e.zaciatok else ""
            
            farba_eventu = '#3b82f6'
            
            if e.dokoncene:
                farba_eventu = '#10b981'
            else:
                priority = [t.priorita for t in e.typ_servisu.all() if hasattr(t, 'priorita') and t.priorita]
                if 'VYSOKA' in priority:
                    farba_eventu = '#ef4444'
                elif 'STREDNA' in priority:
                    farba_eventu = '#f59e0b'
                elif 'NIZKA' in priority:
                    farba_eventu = '#0ea5e9'

            data.append({
                'id': e.id,
                'title': full_title,
                'start': start_iso,
                'end': e.koniec.isoformat() if e.koniec else start_iso,
                'allDay': False,
                'backgroundColor': farba_eventu,
                'borderColor': farba_eventu,
                'textColor': '#1e293b',
                'url': f"/admin/Evidencia/uloha/{e.id}/change/",
            })
            
        return JsonResponse(data, safe=False)

    class Media:
        js = (
            'admin/js/vendor/jquery/jquery.js',
            'admin/js/vendor/select2/select2.full.js',
            'admin/js/jquery.init.js',
            'js/uloha_form_master.js',
            'js/vyhladavaniepoloziekvulohach.js',
        )
        css = {
            'all': (
                'admin/css/vendor/select2/select2.css',
            )
        }

for main_cat, subcats in CATEGORY_MAP.items():
    for subcat_name, model_cls in subcats.items():
        try:
            @admin.register(model_cls)
            class DynamicAdmin(ISASHiddenAdminMixin, ISASPremiumAdmin):
                search_fields = ['seriove_cislo']
        except AlreadyRegistered:
            pass

if not admin.site.is_registered(ContentType):
    @admin.register(ContentType)
    class ContentTypeAdmin(ISASHiddenAdminMixin, ISASPremiumAdmin):
        search_fields = ['model']

class VozidloSTKFilter(admin.SimpleListFilter):
    title = '📋 Platnosť STK / EK'
    parameter_name = 'stk_status'

    def lookups(self, request, model_admin):
        return (
            ('ok', '✅ Platná STK'),
            ('warning', '⚠️ Končí do 30 dní'),
            ('expired', '❌ Expirovaná STK!'),
        )

    def queryset(self, request, queryset):
        dnes = timezone.now().date()

        limit_30_dni = dnes + timedelta(days=30)
        
        if self.value() == 'ok':
            return queryset.filter(stk_plati_do__gt=limit_30_dni)
        if self.value() == 'warning':
            return queryset.filter(stk_plati_do__lte=limit_30_dni, stk_plati_do__gte=dnes)
        if self.value() == 'expired':
            return queryset.filter(Q(stk_plati_do__lt=dnes) | Q(stk_plati_do__isnull=True))
        return queryset

class VozidloServisFilter(admin.SimpleListFilter):
    title = '🔧 Stav servisu a oleja'
    parameter_name = 'servis_status'

    def lookups(self, request, model_admin):
        return (
            ('ok', '🟢 Všetko v poriadku'),
            ('olej', '🛢️ Vyžaduje výmenu oleja'),
            ('servis', '🛠️ Vyžaduje veľký servis'),
        )

    def queryset(self, request, queryset):
        if not self.value():
            return queryset

        qs = queryset.annotate(
            km_olej=Coalesce(F('olej_interval_km'), Value(0)) - (Coalesce(F('aktualny_stav_km'), Value(0)) - Coalesce(F('posledny_olej_km'), Value(0))),
            km_servis=Coalesce(F('servis_interval_km'), Value(0)) - (Coalesce(F('aktualny_stav_km'), Value(0)) - Coalesce(F('posledny_servis_km'), Value(0)))
        )

        if self.value() == 'ok':
            return qs.filter(km_olej__gt=1000, km_servis__gt=1000)
        elif self.value() == 'olej':
            return qs.filter(km_olej__lte=1000)
        elif self.value() == 'servis':
            return qs.filter(km_servis__lte=1000)
            
        return queryset

@admin.register(Vozidlo)
class VozidloAdmin(ERPExportAdminMixin, ISASDriverAdminMixin, ISASPremiumAdmin):
    # 👨‍✈️ ISAS CORE: Konfigurácia pre ISASDriverAdminMixin
    isas_driver_field_employee = 'priradeny_zamestnanec'
    isas_driver_field_person = 'sukromna_osoba'

    isas_title = "🚘 Vozový park"
    isas_preloader_text = "⏳ Vstupujete do zoznamu vozidiel..."
    
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🚘",
        'custom_buttons_html': mark_safe('<button type="button" id="btn-export-excel" class="isas-action-btn btn-blue">🖨️ GENERUJ KNIHU JÁZD</button>'),
        'ignore_default_action_ids': ["btn-export-excel"],
    }

    list_display = ('erp_status_ikona', 'get_vozidlo_card', 'get_vodic_badge', 'get_fleet_health', 'get_tankovanie_stats', 'get_erp_status')
    list_display_links = ('get_vozidlo_card',)
    list_filter = ('erp_export_status', 'je_firemne', 'typ_vozidla_erp', 'typ_paliva', 'aktualne_pneumatiky', VozidloSTKFilter, VozidloServisFilter, ISASDriverTypeFilter)
    
    autocomplete_fields = ['priradeny_zamestnanec', 'sukromna_osoba']
    search_fields = ('znacka_a_model', 'spz', 'erp_stredisko')
    readonly_fields = ('tankovanie_dashboard_btn', 'erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log',)

    fieldsets = (
        ('🚗 Základná identifikácia', {
            'fields': (
                ('znacka_a_model', 'spz', 'je_firemne'),
                'priradeny_zamestnanec',
                'sukromna_osoba',
            ),
            'description': (
                'Základné parametre vozidla a priradenie predvoleného vodiča. 🚙 '
                'Ak označíte vozidlo ako "Firemný majetok", účtovníctvo nebude pripájať amortizáciu k cestovným náhradám. 🏢'
            )
        }),
        ('⚙️ Technické dáta a Údržba', { 
            'fields': (
                ('typ_paliva', 'aktualne_pneumatiky'),
                'aktualny_stav_km',
                ('servis_interval_km', 'posledny_servis_km'), 
                ('olej_interval_km', 'posledny_olej_km'),
                ('varovanie_max_litre', 'varovanie_max_suma')
            ),
            'description': (
                'Technická špecifikácia nevyhnutná pre výpočet spotreby a stráženie servisných intervalov. 🔧 '
                'Systém automaticky upozorní Fleet Manažéra na blížiaci sa servis a anomálie pri tankovaní. 📊'
            )
        }),
        ('🛡️ Legislatíva a Poistenie (Stráženie)', {
            'fields': (
                ('stk_plati_do', 'lekarnicka_plati_do'),
                ('pzp_plati_do', 'kasko_plati_do'),
                'dialnicna_znamka_do'
            ),
            'description': (
                'Dátumy exspirácie kľúčových dokladov. 📑 '
                'Kritické pre bezpečnosť a vyhnutie sa pokutám. Systém vizuálne varuje 30 dní pred vypršaním platnosti. ⚠️'
            )
        }),
        ('💰 ERP, Účtovníctvo a Fakturácia', {
            'fields': (
                ('typ_vozidla_erp', 'erp_kod_paliva'),
                ('erp_stredisko', 'cislo_palivovej_karty'),
                'vlastna_sadzba_za_km'
            ),
            'description': (
                'Mapovanie na externý účtovný systém. 🔗 '
                'Kód paliva a typ vozidla sú absolútne kritické pre správny výpočet zákonných cestovných náhrad (diét) podľa platných sadzieb. 📉'
            )
        }),
        ('⚡ Rýchle akcie (Dashboard)', {
            'fields': ('tankovanie_dashboard_btn',),
            'description': 'Priamy prístup do analytiky a evidencie pre toto konkrétne vozidlo. 🚀'
        }),
        ('📡 ERP Diagnostika', { 
            'fields': ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log'),
            'description': 'Technické a stavové informácie o synchronizácii s podnikovým účtovným systémom. 🛠️'
        }),
    )

    # 🚀 ISAS CORE: Deklaratívny modal pre export knihy jázd
    isas_export_excel_modal = {
        'trigger_id': 'btn-export-excel',
        'generate_action': 'generate_excel',
        'send_action': 'send_excel',
        'poll_action': 'check_megazaloha_status'
    }

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # 🚀 ISAS ENGINE: Optimalizácia N+1 pre priradeného vodiča a tankovania
        return qs.select_related('priradeny_zamestnanec', 'sukromna_osoba').prefetch_related('tankovania')

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            zablokovane = Vozidlo.objects.filter(id__in=valid_ids).filter(
                Q(tankovania__erp_export_status='EXPORTED') | Q(jazdy__erp_export_status='EXPORTED')
            ).distinct()

            if zablokovane.exists():
                spz_list = ", ".join(zablokovane.values_list('spz', flat=True))
                return JsonResponse({"status": "error", "message": f"🚫 Zmazanie zablokované! Vozidlá ({spz_list}) majú záznamy už exportované do ERP."}, status=400)
        
        return super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type in ['generate_excel', 'send_excel']:
            try:
                data = json.loads(request.body)
            except Exception:
                data = {}

            if action_type == 'generate_excel':
                if not request.user.has_perm('Evidencia.view_vozidlo'):
                    return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na export dát."}, status=403)

                datum_od = data.get('datum_od')
                datum_do = data.get('datum_do')
                
                obdobie_text = f"Obdobie: {datum_od} až {datum_do}" if datum_od and datum_do else "Obdobie: Posledných 12 mesiacov"
                
                zaloha = MegaZaloha.objects.create(
                    nazov_zalohy=f"Kniha jázd ({len(valid_ids)} vozidiel)",
                    typ_zalohy='kniha_jazd',
                    stav_spracovania='spracovava_sa',
                    vyziadal_uzivatel=request.user,
                    poznamka=f"Generuje sa na pozadí... | {obdobie_text}"
                )
                
                from .tasks import task_vygeneruj_knihu_jazd_excel
                def _dispatch_kniha_jazd(v_ids=None, u_id=request.user.id, z_id=zaloha.id, d_od=datum_od, d_do=datum_do):
                    if v_ids is None:
                        v_ids = list(valid_ids)
                    try:
                        task_vygeneruj_knihu_jazd_excel.delay(v_ids, u_id, z_id, d_od, d_do)
                    except Exception as exc:
                        logger.warning(f"Zlyhalo generovanie knihy jázd v Celery: {exc}")
                        messages.warning(request, "⚠️ Generovanie knihy jázd zlyhalo pri zaradení do fronty (výpadok Celery/Redis).")
                transaction.on_commit(_dispatch_kniha_jazd)
                
                return JsonResponse({"status": "processing", "zaloha_id": zaloha.id})
            
            elif action_type == 'send_excel':
                if not request.user.has_perm('Evidencia.view_vozidlo'):
                    return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na odosielanie dát."}, status=403)

                zaloha_id = data.get('zaloha_id')
                if not zaloha_id:
                    return JsonResponse({"status": "error", "message": "Chýba identifikátor zálohy."}, status=400)
                from .tasks import task_odoslat_knihu_jazd_uctovnicke
                try:
                    task_odoslat_knihu_jazd_uctovnicke.delay(zaloha_id)
                except Exception as exc:
                    logger.warning(f"Zlyhalo odoslanie knihy jázd účtovníčke do Celery: {exc}")
                    messages.warning(request, "⚠️ Úlohu na odoslanie knihy jázd sa nepodarilo zaradiť do fronty (výpadok Celery/Redis).")
                    return JsonResponse({"status": "error", "message": "⚠️ Odoslanie knihy jázd zlyhalo kvôli výpadku Celery/Redis."}, status=503)
                return JsonResponse({"status": "success", "message": "📧 Požiadavka na odoslanie bola úspešne zaradená do fronty."})

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'priradeny_zamestnanec', 'sukromna_osoba'
        ).filter(
            Q(znacka_a_model__icontains=term) |
            Q(spz__icontains=term) |
            Q(cislo_palivovej_karty__icontains=term) |
            Q(priradeny_zamestnanec__meno__icontains=term) |
            Q(priradeny_zamestnanec__priezvisko__icontains=term) |
            Q(sukromna_osoba__meno_a_priezvisko__icontains=term)
        )[:30]

    def format_detective_result(self, obj):
        vodic = str(obj.priradeny_zamestnanec) if obj.priradeny_zamestnanec else (str(obj.sukromna_osoba) if obj.sukromna_osoba else "Nepriradené")
        return {
            'id': obj.id, 
            'name': f"{obj.znacka_a_model} [{obj.spz}]", 
            'search_value': obj.spz,
            'rola': f"Vodič: {vodic}",
            'date': '—',
            'cat': '🚘 VOZIDLO'
        }

    @admin.display(description='📝 Kniha tankovaní a bločky')
    def tankovanie_dashboard_btn(self, obj):
        if not obj or not obj.pk:
            return render_isas_text("⚠️ Najskôr uložte vozidlo pre zobrazenie rozšírených možností.", color="#94a3b8", size=12, weight=700)
        
        url = reverse('admin:Evidencia_tankovanie_changelist') + f"?vozidlo__id__exact={obj.id}"
        
        return render_isas_dashboard_action_btn(
            url=url,
            icon="⛽",
            title="Otvoriť knihu tankovaní",
            subtitle=f"Zobrazí všetky palivové bločky pre ŠPZ: {obj.spz}"
        )

    @admin.display(description='🚗 Vozidlo & ŠPZ', ordering='znacka_a_model')
    def get_vozidlo_card(self, obj):
        spz_badge = render_isas_badge(text=f"🚗 {obj.spz}", type_color="neutral")
        
        if obj.je_firemne:
            typ_badge = render_isas_badge(text="FIREMNÉ", type_color="success")
        else:
            typ_badge = render_isas_badge(text="SÚKROMNÉ", type_color="purple")
            
        palivo_badge = render_isas_badge(text=obj.get_typ_paliva_display(), type_color="neutral")
            
        badges_row = render_isas_layout(
            format_html('{}{}{}', spz_badge, typ_badge, palivo_badge),
            layout="row", gap=6, center=True, custom_style="margin-top: 4px; flex-wrap: wrap;"
        )
        
        return render_isas_avatar(
            icon="🚘",
            title=obj.znacka_a_model,
            subtitle=None,
            bg_gradient="#f59e0b, #b45309",
            shadow_color="rgba(245, 158, 11, 0.25)",
            extra_html=badges_row
        )

    @admin.display(description='🩺 Fleet Health (Servis & Doklady)')
    def get_fleet_health(self, obj):
        dnes = timezone.now().date()
        limit_dni = timedelta(days=30)
        
        akt_km = obj.aktualny_stav_km or 0
        posl_olej = obj.posledny_olej_km or 0
        interval_olej = obj.olej_interval_km or 0
        km_olej = interval_olej - (akt_km - posl_olej)
        olej_farba = "#10b981" if km_olej > 1500 else ("#f59e0b" if km_olej > 0 else "#ef4444")
        
        posl_servis = obj.posledny_servis_km or 0
        interval_servis = obj.servis_interval_km or 0
        km_servis = interval_servis - (akt_km - posl_servis)
        servis_farba = "#10b981" if km_servis > 2000 else ("#f59e0b" if km_servis > 0 else "#ef4444")

        def eval_date(datum):
            d = to_date(datum)
            if not d: return "Nezadané", "#94a3b8"
            if d < dnes: return f"❌ {d.strftime('%d.%m.%Y')}", "#ef4444"
            if (d - dnes) <= limit_dni: return f"⚠️ {d.strftime('%d.%m.%Y')}", "#f59e0b"
            return f"{d.strftime('%d.%m.%Y')}", "#10b981"

        stk_str, stk_farba = eval_date(obj.stk_plati_do)
        pzp_str, pzp_farba = eval_date(obj.pzp_plati_do)
        dz_str, dz_farba = eval_date(obj.dialnicna_znamka_do)
        kasko_str, kasko_farba = eval_date(obj.kasko_plati_do)
        lek_str, lek_farba = eval_date(obj.lekarnicka_plati_do)

        pneu_ikona = "☀️" if obj.aktualne_pneumatiky == 'LETNE' else ("❄️" if obj.aktualne_pneumatiky == 'ZIMNE' else "⛅")

        col1 = render_isas_layout(
            format_html('{}{}{}{}', render_isas_text(f"🛢️ Olej: {km_olej} km", color=olej_farba, align="left"), render_isas_text(f"⚙️ Servis: {km_servis} km", color=servis_farba, align="left"), render_isas_text(f"🛣️ DZ: {dz_str} | 🚑 {lek_str}", color=dz_farba if dz_farba != '#10b981' else lek_farba, align="left"), render_isas_text(f"{pneu_ikona} Pneu: {obj.get_aktualne_pneumatiky_display()}", color="#475569", align="left", custom_style="margin-top: 4px; border-top: 1px solid #f1f5f9; padding-top: 4px;")),
            layout="col", gap=4, custom_style="padding-right: 8px; border-right: 1px dashed #cbd5e1;"
        )
        
        col2 = render_isas_layout(
            format_html('{}{}{}{}', render_isas_text(f"📑 STK: {stk_str}", color=stk_farba, align="left"), render_isas_text(f"🛡️ PZP: {pzp_str}", color=pzp_farba, align="left"), render_isas_text(f"🚗 KASKO: {kasko_str}", color=kasko_farba, align="left"), render_isas_text(f"📟 Tach: {obj.aktualny_stav_km} km", color="#475569", align="left", custom_style="margin-top: 4px; border-top: 1px solid #f1f5f9; padding-top: 4px;")),
            layout="col", gap=4, custom_style="padding-left: 4px;"
        )

        grid = render_isas_layout(
            format_html('{}{}', col1, col2),
            layout="grid", columns=2, gap=8, bg_color="#ffffff", padding="10px", border_radius="12px", border_color="#e2e8f0", shadow="0 2px 6px rgba(0,0,0,0.03)"
        )
        
        return render_isas_layout(grid, layout="col", center=True)

    @admin.display(description='⛽ Analytika (Náklady a Spotreba)')
    def get_tankovanie_stats(self, obj):
        if not obj or not getattr(obj, 'pk', None):
            return render_isas_text("—", color="#94a3b8")

        # 1. Defenzívne načítanie nákladov a spotreby (ochrana pred None a striktné 2 desatinné miesta)
        naklady_raw = getattr(obj, 'celkove_naklady_palivo', Decimal('0.00')) or Decimal('0.00')
        try:
            naklady_dec = Decimal(str(naklady_raw))
        except Exception:
            naklady_dec = Decimal('0.00')

        spotreba = getattr(obj, 'priemerna_spotreba', None)
        spotreba_text = "— L/100km"
        spotreba_farba = "#94a3b8"
        
        if spotreba is not None:
            try:
                spotreba_val = Decimal(str(spotreba))
                if spotreba_val > Decimal('0.00'):
                    spotreba_farba = "#10b981" if spotreba_val < Decimal('8.00') else ("#f59e0b" if spotreba_val < Decimal('10.00') else "#ef4444")
                    spotreba_text = f"Ø {spotreba_val:.1f} L/100km"
            except (InvalidOperation, TypeError, ValueError):
                pass

        mena_firmy = get_firemna_mena()
        card = render_isas_mini_stat_card(
            top_label="Celkové náklady",
            top_val=f"{naklady_dec:.2f} {mena_firmy}",
            bot_label="Priemerná spotreba",
            bot_val=spotreba_text,
            bot_val_color=spotreba_farba
        )

        # 2. 🛡️ IMÚNNA KONTROLA LIMITOV: Prevod na natívne typy bez rizika TypeError
        max_litre_raw = getattr(obj, 'varovanie_max_litre', None)
        try:
            max_litre = int(max_litre_raw) if max_litre_raw is not None else 0
        except (ValueError, TypeError):
            max_litre = 0

        max_suma_raw = getattr(obj, 'varovanie_max_suma', None)
        try:
            max_suma_dec = Decimal(str(max_suma_raw)) if max_suma_raw is not None else Decimal('0.00')
        except Exception:
            max_suma_dec = Decimal('0.00')

        limity_info = []
        if max_litre > 0:
            limity_info.append(f"max {max_litre} L")
        if max_suma_dec > Decimal('0.00'):
            limity_info.append(f"max {max_suma_dec:.2f} {mena_firmy}")

        if limity_info:
            popis_limitu = " · ".join(limity_info)
            limit_badge = render_isas_badge(text=f"🛡️ {popis_limitu}", type_color="info", wrapper=False)
        else:
            limit_badge = render_isas_badge(text="🛡️ Bez limitu tankovania", type_color="neutral", wrapper=False)

        return render_isas_layout(format_html('{}{}', card, limit_badge), layout="col", center=True, gap=4)

    @admin.display(description='📡 ERP Mapovanie (Účtovníctvo)')
    def get_erp_status(self, obj):
        stredisko = obj.erp_stredisko if obj.erp_stredisko else "Nenastavené"
        farba_strediska = "#059669" if obj.erp_stredisko else "#ef4444"
        
        badge_typ = render_isas_badge(text=obj.get_typ_vozidla_erp_display(), type_color="info", wrapper=False)
        badge_palivo = render_isas_badge(text=obj.get_erp_kod_paliva_display(), type_color="neutral", wrapper=False)
        badges_row = render_isas_layout(format_html('{}{}', badge_typ, badge_palivo), layout="row", gap=4, center=True)
        text = render_isas_text(f"🏢 {stredisko}", color=farba_strediska, weight=800, custom_style="margin-top: 4px;")
        sadzba_html = render_isas_badge(f"{obj.vlastna_sadzba_za_km} {get_firemna_mena()}/km", icon="💰", type_color="success", wrapper=False) if obj.vlastna_sadzba_za_km and obj.vlastna_sadzba_za_km > Decimal('0.00') else ""
        
        return render_isas_layout(format_html('{}{}{}', badges_row, text, sadzba_html), layout="col", center=True, gap=4)

@admin.register(Tankovanie)
class TankovanieAdmin(ERPExportAdminMixin, ISASDriverAdminMixin, ISASPremiumAdmin):
    form = TankovanieAdminForm

    # 👨‍✈️ ISAS CORE: Konfigurácia pre ISASDriverAdminMixin
    isas_driver_field_employee = 'vodič'
    isas_driver_field_person = 'osoba'
    isas_exclusive_field_groups = [
        ('vodič', ['osoba']),
        ('osoba', ['vodič'])
    ]

    isas_title = "⛽ Evidencia tankovaní"
    isas_preloader_text = "⏳ Vstupujete do zoznamu evidencie tankovaní..."
    
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🚗",
    }

    list_display = (
        'erp_status_ikona', 
        'get_tankovanie_card', 
        'get_vodic_badge', 
        'get_doklad_info', 
        'get_cena_stats', 
        'get_uctovne_info', 
        'nahlad_blocku'
    )
    list_display_links = ('get_tankovanie_card',)
    list_filter = (
        ('vozidlo', admin.RelatedOnlyFieldListFilter), 
        ISASDriverTypeFilter,
        'cerpacia_stanica',
        'erp_export_status',
        'sposob_platby',
        ISASDateRangeFilter,
    )
    isas_date_filter_field = 'datum'
    
    autocomplete_fields = ['vozidlo', 'vodič', 'osoba', 'cerpacia_stanica']
    search_fields = ('vozidlo__spz', 'vodič__meno', 'vodič__priezvisko', 'cerpacia_stanica__Nazov_spolocnosti')
    readonly_fields = ('cena_bez_dph', 'suma_dph', 'erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log',)

    fieldsets = (
        ('🚗 Identifikácia (Kto a čím)', {
            'fields': (
                'vozidlo',
                'vodič',
                'osoba',
            ),
            'description': (
                'Základné priradenie vozidla k zodpovednej osobe pre presnú evidenciu nákladov. 🚛 '
                'Vyberte konkrétny automobil z vozového parku a šoféra, ktorý nákup realizoval. 👤'
            )
        }),
        ('⛽ Údaje o tankovaní a účtovaní', {
            'fields': (
                ('datum', 'stav_tachometra'),
                ('cerpacia_stanica', 'cislo_dokladu'),
                ('sposob_platby', 'sposob_zauctovania_erp'),
            ),
            'description': (
                'Presné časové a lokalizačné údaje o priebehu tankovania. 📅 '
                'Sledujte aktuálny nájazd kilometrov pre presný výpočet priemernej spotreby. 📍 '
                'Zaznamenajte spôsob platby a účtovania pre korektné párovanie v systéme. 💳'
            )
        }),
        ('💰 Množstvo, Cena a DPH', {
            'fields': (
                ('litre', 'sadzba_dph', 'kod_clenenia_dph'),
                ('cena_celkom', 'mena'),
                ('cena_bez_dph', 'suma_dph'),
            ),
            'description': (
                'Detailný rozpis finančných parametrov transakcie. 💸 '
                'Zadajte množstvo paliva a celkovú sumu priamo z dokladu, systém automaticky prepočíta daňový základ. 📊 '
                'Správne priradenie DPH a jej členenia je kritické pre finančný report a audity. 📉'
            )
        }),
        ('📸 Prílohy', {
            'fields': ('foto_blocku',),
            'description': (
                'Digitálna kópia fyzického dokladu slúži pre potreby vnútornej kontroly. 📁 '
                'Nahrajte čitateľnú fotografiu bloku pre overenie deklarovaných údajov. 📷'
            )
        }),
        ('📡 ERP Diagnostika', {
            'fields': (
                'erp_export_status', 
                'erp_id', 
                'erp_last_exported_at', 
                'erp_error_log'
            ),
            'description': (
                'Technické informácie o synchronizácii s externým podnikovým systémom. 🔗 '
                'V prípade zlyhania exportu skontrolujte chybový log pre rýchlu nápravu. 🛠️ '
                'Tieto dáta sú kľúčové pre integritu a stabilitu celého ekosystému. 🛡️'
            ),
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            'vozidlo', 
            'vodič', 
            'osoba',
            'cerpacia_stanica'
        )

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'vozidlo', 'vodič', 'osoba'
        ).filter(
            Q(cislo_dokladu__icontains=term) |
            Q(vozidlo__spz__icontains=term) |
            Q(vodič__meno__icontains=term) |
            Q(vodič__priezvisko__icontains=term) |
            Q(osoba__meno_a_priezvisko__icontains=term)
        ).order_by('-datum')[:30]

    def format_detective_result(self, obj):
        sofer_meno = f"{obj.vodič.meno} {obj.vodič.priezvisko}" if obj.vodič else (obj.osoba.meno_a_priezvisko if obj.osoba else "Neznámy vodič")
        spz_text = obj.vozidlo.spz if obj.vozidlo else "Bez ŠPZ"
        datum_text = obj.datum.strftime('%d.%m.%Y') if obj.datum else ""
        return {
            'id': obj.id, 
            'name': spz_text, 
            'rola': sofer_meno,
            'search_value': spz_text,
            'suma': f"{obj.cena_celkom or Decimal('0.00')} {obj.mena or get_firemna_mena()}",
            'date': datum_text,
            'cat': '⛽ TANKOVANIE'
        }

    @admin.display(description='🚗 Vozidlo & Údaje', ordering='datum')
    def get_tankovanie_card(self, obj):
        extra_html = ""
        if obj.stav_tachometra:
            extra_html = render_isas_badge(f"🔢 {obj.stav_tachometra} km", type_color="neutral")
            
        spz_text = obj.vozidlo.spz if obj.vozidlo else "Bez ŠPZ"
        datum_text = obj.datum.strftime('%d.%m.%Y %H:%M') if obj.datum else "Neznámy čas"
        return render_isas_avatar(
            icon="⛽",
            title=spz_text,
            subtitle=f"🕒 {datum_text}",
            bg_gradient="#2563eb, #1d4ed8",
            shadow_color="rgba(37, 99, 235, 0.25)",
            extra_html=extra_html
        )

    @admin.display(description='🏢 Doklad & Stanica', ordering='cerpacia_stanica')
    def get_doklad_info(self, obj):
        img_url = None
        if obj.cerpacia_stanica and getattr(obj.cerpacia_stanica, 'domena', None):
            clean_domain = str(obj.cerpacia_stanica.domena).lower().strip()
            for prefix in ['https://', 'http://', 'www.']:
                if clean_domain.startswith(prefix):
                    clean_domain = clean_domain[len(prefix):]
            
            clean_domain = clean_domain.split('/')[0].split('?')[0].split('#')[0]
            
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
                
            if '.' in clean_domain and len(clean_domain) > 3:
                img_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"

        stanica_nazov = obj.cerpacia_stanica.Nazov_spolocnosti if obj.cerpacia_stanica else "⚠️ Stanica chýba"
        
        if obj.cislo_dokladu:
            doklad_html = render_isas_badge(f"🧾 {obj.cislo_dokladu}", type_color="neutral")
        else:
            doklad_html = render_isas_text("Bez čísla dokladu", size=10, muted=True)

        return render_isas_avatar(
            icon="⛽",
            title=stanica_nazov,
            subtitle="",
            bg_gradient="#ef4444, #b91c1c",
            shadow_color="rgba(239, 68, 68, 0.25)",
            extra_html=doklad_html,
            image_url=img_url
        )

    @admin.display(description='💰 Financie & DPH', ordering='cena_celkom')
    def get_cena_stats(self, obj):
        try:
            cena_liter = Decimal(str(obj.cena_za_liter or '0.00').replace(',', '.')).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            cena_liter = Decimal('0.00')
        cena_liter_formatovana = f"{cena_liter:.2f}"
        mena_t = obj.mena or get_firemna_mena()
        cena_celkom = obj.cena_celkom or Decimal('0.00')
        cena_bez_dph = obj.cena_bez_dph or Decimal('0.00')
        suma_dph = obj.suma_dph or Decimal('0.00')
        litre = obj.litre or Decimal('0.00')
        return render_isas_mini_stat_card(
            top_label="Celkom",
            top_val=f"{cena_celkom} {mena_t}",
            bot_label="Základ / DPH",
            bot_val=f"{cena_bez_dph} / {suma_dph} {mena_t}",
            third_label="Objem paliva",
            third_val=f"💧 {litre} L ({cena_liter_formatovana} {mena_t}/L)",
            third_val_color="#0284c7"
        )

    @admin.display(description='⚙️ Platba & ERP', ordering='sposob_platby')
    def get_uctovne_info(self, obj):
        if obj.sposob_platby == 'PALIVOVA':
            platba = render_isas_badge("💳 KARTA (PHM)", type_color="info")
        elif obj.sposob_platby == 'PLATOBNA':
            platba = render_isas_badge("📱 BANK. KARTA", type_color="neutral")
        else:
            platba = render_isas_badge("💵 HOTOVOSŤ", type_color="warning")

        if obj.sposob_zauctovania_erp == 'FAKTURA':
            erp = render_isas_badge("🛑 ZBERNÁ FAKTÚRA", type_color="danger")
        elif obj.sposob_zauctovania_erp == 'SUKROMNE_PENIAZE':
            erp = render_isas_badge("🤝 PREPLATIŤ", type_color="purple")
        else:
            erp = render_isas_badge("🏢 FIREMNÝ ÚČET", type_color="success")

        kod_txt = obj.kod_clenenia_dph or "—"
        sadzba_txt = f"{obj.sadzba_dph or Decimal('0.00')}%"
        dph_kod = render_isas_text(f"🏛️ DPH: {kod_txt} ({sadzba_txt})", size=11, weight=700)

        return render_isas_layout(
            content_html=format_html('{}{}{}', platba, erp, dph_kod),
            layout="col",
            center=True,
            gap=4
        )

    @admin.display(description='📸 Príloha')
    def nahlad_blocku(self, obj):
        if obj.foto_blocku and getattr(obj.foto_blocku, 'name', None):
            try:
                return render_isas_link_btn(
                    url=obj.foto_blocku.url,
                    text="",
                    icon="🧾",
                    type_color="neutral",
                    square=True,
                    wrapper=True
                )
            except (ValueError, AttributeError):
                pass
            
        return render_isas_link_btn(
            url=None,
            text="",
            icon="❌",
            type_color="dashed",
            is_button=True,
            square=True,
            wrapper=True
        )

@admin.register(EvidenciaJazd)
class EvidenciaJazdAdmin(ISASDriverAdminMixin, ISASPremiumAdmin):
    list_display = ('get_jazda_card', 'get_vodic_badge', 'get_trasa_a_ciel', 'get_km_stats', 'get_typ_jazdy_badge', 'stav_jazdy_badge', 'stav_fakturacie_badge')
    list_display_links = ('get_jazda_card',)
    list_filter = ('typ_jazdy', 'vozidlo', ISASDriverTypeFilter, ISASDateRangeFilter)
    isas_date_filter_field = 'datum_cas_start'
    
    autocomplete_fields = [
        'vozidlo', 'zamestnanec', 'sukromna_osoba', 'spolujazdci',
        'zakaznik_start', 'dodavatel_start', 'filialka_start', 'zamestnanec_start',
        'zakaznik', 'dodavatel_ciel', 'filialka_ciel', 'zamestnanec_ciel', 'uloha', 'sukromna_osoba_start' ,'sukromna_osoba_ciel',
    ]
    search_fields = ('vozidlo__spz', 'zamestnanec__meno', 'zamestnanec__priezvisko', 'zakaznik__Nazov_spolocnosti', 'ucel_cesty')
    readonly_fields = ('vyfakturovane',)

    fieldsets = (
        ('🚗 Základné informácie', {
            'fields': (
                ('vozidlo', 'typ_jazdy'),
                'zamestnanec', 
                'sukromna_osoba', 
                'spolujazdci'
            ),
            'description': (
                'Presná identifikácia vozidla a osôb zúčastnených na konkrétnej jazde. 🚙 '
                'Zvoľte charakter jazdy a priraďte zodpovedného šoféra pre dokonalú evidenciu. 👨‍✈️ '
                'Vybraní spolujazdci automaticky získavajú nárok na cestovné náhrady, čo extrémne zrýchľuje prácu účtovného oddelenia. 💸'
            )
        }),
        ('📍 Odkiaľ (Začiatok cesty)', {
            'fields': (
                ('stat_startu', 'miesto_startu'),
                ('sukromna_osoba_start', 'zamestnanec_start'),
                ('zakaznik_start', 'dodavatel_start'),
                'filialka_start',
            ),
            'description': 'Vyplňte iba JEDNU možnosť. Ak vyberiete osobu, do poľa sa načíta jej adresa a pole sa uzamkne. 🔒'
        }),
        ('🏁 Kam (Cieľ cesty) a Účel', {
            'fields': (
                ('stat_ciela', 'miesto_ciela'),
                ('sukromna_osoba_ciel', 'zamestnanec_ciel'),
                ('zakaznik', 'dodavatel_ciel'),
                'filialka_ciel',
                ('ucel_cesty', 'uloha'),
            ),
            'description': 'Vyplňte iba JEDNU z možností pre cieľ. 🏁'
        }),
        ('⏱️ Čas, Tachometer a Palivo', {
            'fields': (
                ('datum_cas_start', 'datum_cas_koniec'),
                ('stav_km_start', 'stav_km_koniec'),
                'stav_nadrze_start',
            ),
            'description': (
                'Prísne logistické metriky, ktoré tvoria samotné analytické jadro evidencie vozového parku. ⏳ '
                'Poctivo zaznamenaný nájazd kilometrov garantuje bezchybný výpočet prejdenej vzdialenosti a amortizácie. 🛣️ '
                'Údaj o počiatočnom stave nádrže slúži na presnú kalibráciu priemernej spotreby paliva. ⛽'
            )
        }),
        ('🍱 Diéty, Ubytovanie a Účtovníctvo', {
            'fields': (
                ('erp_zakazka', 'vyfakturovane'),
                ('bezplatne_stravovanie', 'noclah_zabezpeceny'),
                'poznamka',
            ),
            'description': (
                'Strategické dáta pripravené priamo pre vaše finančné a mzdové oddelenie. 💼 '
                'Kód zákazky z ERP spáruje náklady na jazdu priamo s účtovníctvom. 📊 '
                'Evidencia bezplatného stravovania automaticky legislatívne kráti vyplácané diéty a nocľah mení charakter cesty. 🛏️'
            )
        }),
    )

    isas_title = "🚗 Evidencia jázd" 
    isas_preloader_text = "⏳ Vstupujete do zoznamu evidencie jázd..."
    isas_complex_form_rules = {
        'spolujazdci_protection': {
            'driver_field': 'zamestnanec',
            'passengers_field': 'spolujazdci',
            'warning_title': 'Nekompatibilná akcia!',
            'warning_text_add': 'Zamestnanec, ktorý už vozidlo <strong>šoféruje</strong>, nemôže byť pridaný aj ako spolujazdec. 🙅‍♂️',
            'warning_text_remove': 'Nový vodič bol automaticky <strong>odstránený zo zoznamu spolujazdcov</strong>, keďže aktuálne riadi vozidlo. 🚗'
        },
        'address_groups': [
            {
                'text_field': 'miesto_startu',
                'select_fields': ['zakaznik_start', 'dodavatel_start', 'filialka_start', 'zamestnanec_start', 'sukromna_osoba_start']
            },
            {
                'text_field': 'miesto_ciela',
                'select_fields': ['zakaznik', 'dodavatel_ciel', 'filialka_ciel', 'zamestnanec_ciel', 'sukromna_osoba_ciel']
            }
        ],
        'row_toggles': {
            'trigger': 'typ_jazdy',
            'rules': {
                'FIREMNA': {'show': ['zamestnanec', 'zakaznik'], 'hide': ['sukromna_osoba'], 'clear': ['sukromna_osoba']},
                'SUKROMNA': {'show': ['sukromna_osoba'], 'hide': ['zamestnanec', 'zakaznik'], 'clear': ['zamestnanec', 'zakaznik']}
            }
        }
    }
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🚗",
    }

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            'vozidlo', 'zamestnanec', 'sukromna_osoba', 'zakaznik', 
            'dodavatel_ciel', 'filialka_ciel', 'zamestnanec_ciel',
            'sukromna_osoba_ciel', 'uloha'
        )

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'vozidlo', 'zamestnanec', 'sukromna_osoba'
        ).filter(
            Q(vozidlo__spz__icontains=term) |
            Q(zamestnanec__meno__icontains=term) |
            Q(zamestnanec__priezvisko__icontains=term) |
            Q(sukromna_osoba__meno_a_priezvisko__icontains=term) |
            Q(ucel_cesty__icontains=term) |
            Q(miesto_startu__icontains=term) |
            Q(miesto_ciela__icontains=term)
        ).order_by('-datum_cas_start')[:30]

    def format_detective_result(self, obj):
        vodic = str(obj.zamestnanec) if obj.zamestnanec else (str(obj.sukromna_osoba) if obj.sukromna_osoba else "Neznámy vodič")
        spz_text = obj.vozidlo.spz if obj.vozidlo else "Bez ŠPZ"
        datum_text = obj.datum_cas_start.strftime('%d.%m.%Y %H:%M') if obj.datum_cas_start else "Neznámy čas"
        return {
            'id': obj.id, 
            'name': spz_text, 
            'search_value': spz_text,
            'rola': f"👤 {vodic}",
            'date': datum_text,
            'cat': '🚗 JAZDA'
        }

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'get_entity_address':
            if not request.user.has_perm('Evidencia.view_evidenciajazd'):
                return JsonResponse({'address': ''}, status=403)

            try:
                data = json.loads(request.body)
            except Exception:
                data = {}

            field_name = data.get('field_type')
            obj_id = data.get('obj_id')
            
            if obj_id == "DEFAULT":
                try:
                    default_val = self.model._meta.get_field('miesto_startu').get_default()
                    if callable(default_val): default_val = default_val()
                    return JsonResponse({'address': default_val})
                except Exception:
                    return JsonResponse({'address': ''})

            if not obj_id or not field_name: return JsonResponse({'address': ''})

            try:
                field = self.model._meta.get_field(field_name)
                EntityModel = field.related_model
                obj = EntityModel.objects.get(id=obj_id)

                ulica = getattr(obj, 'ulica_a_cislo', '') or getattr(obj, 'ulica', '') or getattr(obj, 'adresa', '') or ''
                mesto = getattr(obj, 'mesto', '') or getattr(obj, 'obec', '') or ''
                psc = getattr(obj, 'psc', '') or ''
                stat = getattr(obj, 'stat', '') or getattr(obj, 'krajina', '') or ''

                casti = [str(c).strip() for c in [ulica, f"{psc} {mesto}".strip(), stat] if c and str(c).strip()]
                return JsonResponse({'address': ", ".join(casti)})
            except Exception:
                return JsonResponse({'address': ''})

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description='🚗 Vozidlo & Čas', ordering='datum_cas_start')
    def get_jazda_card(self, obj):
        spz_text = obj.vozidlo.spz if obj.vozidlo else "Bez ŠPZ"
        datum_text = obj.datum_cas_start.strftime('%d.%m.%Y %H:%M') if obj.datum_cas_start else "Neznámy čas"
        return render_isas_avatar(
            icon="🚗",
            title=spz_text,
            subtitle=f"🕒 {datum_text}",
            bg_gradient="#0ea5e9, #0284c7",
            shadow_color="rgba(2, 132, 199, 0.25)"
        )

    @admin.display(description='🔢 Tachometer', ordering='stav_km_start')
    def get_km_stats(self, obj):
        km_end = obj.stav_km_koniec if obj.stav_km_koniec else "—"
        return render_isas_mini_stat_card(
            top_label="Prejdené celkom",
            top_val=f"+ {obj.prejdene_km} km",
            top_val_color="#3b82f6",
            bot_label="Stav tachometra",
            bot_val=f"{obj.stav_km_start} ➔ {km_end}",
            bot_val_color="#475569"
        )

    @admin.display(description='🏷️ Typ & Diéty', ordering='typ_jazdy')
    def get_typ_jazdy_badge(self, obj):
        items = []
        if obj.typ_jazdy == 'FIREMNA':
            items.append(render_isas_badge(text="FIREMNÁ", icon="💼", type_color="info", wrapper=False))
        else:
            items.append(render_isas_badge(text="SÚKROMNÁ", icon="🏖️", type_color="purple", wrapper=False))

        extra = []
        if obj.bezplatne_stravovanie:
            extra.append("🍽️ Strava")
        if obj.noclah_zabezpeceny:
            extra.append("🛏️ Nocľah")
        if extra:
            items.append(render_isas_badge(" | ".join(extra), type_color="neutral", wrapper=False))
        if obj.spolujazdci.exists():
            items.append(render_isas_badge(f"{obj.spolujazdci.count()} spoluj.", icon="👥", type_color="neutral", wrapper=False))

        return render_isas_layout(mark_safe("".join(items)), layout="col", gap=2, center=True)

    @admin.display(description='📍 Trasa & Účel', ordering='miesto_ciela')
    def get_trasa_a_ciel(self, obj):
        start = obj.miesto_startu if obj.miesto_startu else "Neuvedené"
        ciel = obj.miesto_ciela if obj.miesto_ciela else "Neuvedené"
        ucel = obj.ucel_cesty if obj.ucel_cesty else ""
        
        start_ciel_text = f"{start} ➔ {ciel}"

        if obj.typ_jazdy == 'SUKROMNA':
            t1 = render_isas_text(start_ciel_text, size=12, color="#8b5cf6", weight=800)
            t2 = render_isas_text(ucel, size=11, color="#a78bfa", weight=700)
            return render_isas_layout(format_html('{}{}', t1, t2), layout="col", gap=4, center=True)
        
        cielovy_subjekt = ucel
        if obj.zakaznik: cielovy_subjekt = f"🏢 {obj.zakaznik.Nazov_spolocnosti}"
        elif obj.dodavatel_ciel: cielovy_subjekt = f"📦 {obj.dodavatel_ciel.Nazov_spolocnosti}"
        elif obj.filialka_ciel: cielovy_subjekt = f"🏭 {obj.filialka_ciel.nazov}"
        elif obj.zamestnanec_ciel: cielovy_subjekt = f"🏠 {obj.zamestnanec_ciel.meno} {obj.zamestnanec_ciel.priezvisko}"
        elif obj.sukromna_osoba_ciel: cielovy_subjekt = f"🏠 {obj.sukromna_osoba_ciel.meno_a_priezvisko}"

        t1 = render_isas_text(start_ciel_text, size=12, color="#059669", weight=800)
        t2 = render_isas_text(cielovy_subjekt, size=11, color="#475569", weight=700)
        
        uloha_html = render_isas_badge(text=f"Úloha #{obj.uloha.id}", icon="🔗", type_color="info") if obj.uloha else ""
        zakazka_html = render_isas_badge(text=f"Zákazka: {obj.erp_zakazka}", icon="💼", type_color="neutral") if obj.erp_zakazka else ""
        
        return render_isas_layout(format_html('{}{}{}{}', t1, t2, uloha_html, zakazka_html), layout="col", gap=4, center=True)

    @admin.display(description='💳 Fakturácia', ordering='vyfakturovane')
    def stav_fakturacie_badge(self, obj):
        if obj.typ_jazdy == 'SUKROMNA':
            return render_isas_text("—", size=12, color="#cbd5e1", weight=900)

        if obj.vyfakturovane:
            return render_isas_badge(text="VYFAKTUROVANÉ", icon="💳", type_color="success")
        return render_isas_badge(text="ČAKÁ", icon="⏳", type_color="warning")

    @admin.display(description='🚦 Stav')
    def stav_jazdy_badge(self, obj):
        if obj.stav_km_koniec:
            return render_isas_badge(text="UKONČENÁ", icon="⚪", type_color="neutral")
        return render_isas_badge(text="NA CESTE", icon="🟢", type_color="success", css_class="pulse-green")

@admin.register(PrichodziaFaktura)
class PrichodziaFakturaAdmin(ISASSmartMatcherAdminMixin, ERPExportAdminMixin, ISASPremiumAdmin):
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # ISAS ENGINE: Eliminácia N+1 dotazov pre dodávateľov a M2M párovanie
        return qs.select_related('dodavatel').prefetch_related('suvisiace_objednavky')
    
    list_display = (
        'erp_status_ikona',
        'get_faktura_identity',
        'get_terminy_card',
        'get_financie_card',
        'get_ai_a_subor',
        'get_sparovanie_status',
        'get_stav_uhrady_badge',
        'get_platobny_terminal',
    )    
    list_display_links = ('get_faktura_identity',)
    list_filter = ['zdroj', 'erp_export_status', ISASDateRangeFilter, ('dodavatel', admin.RelatedOnlyFieldListFilter), 'stav_uhrady', 'spracovane']
    isas_date_filter_field = 'datum_prijatia'
    search_fields = ['dodavatel__Nazov_spolocnosti', 'dodavatel__ico', 'variabilny_symbol', 'iban']
    filter_horizontal = ('suvisiace_objednavky', 'navrhnute_objednavky')
    autocomplete_fields = ['dodavatel', 'ignorovane_objednavky']
    readonly_fields = (
        'get_platobny_terminal', 'zdroj', 'stav_uhrady', 'spracovane', 'poznamka', 
        'datum_prijatia', 'erp_export_status', 'erp_id', 'erp_last_exported_at', 
        'get_naskladnovaci_dashboard_link',
        'peppol_document_id', 'xml_sha256_hash', 'xml_subor', 'vizualizacia_subor'
    )

    fieldsets = (
        ('💸 EXECUTIVE PAY TERMINÁL', {
            'fields': ('get_platobny_terminal',),
            'classes': ('vivid-box-flash',),
            'description': '⚡ Rýchly prístup k platobným funkciám pre okamžitú úhradu faktúry cez terminál. 💳'
        }),
        ('📦 SPRÁVA POLOŽIEK A SKLADU', {
            'fields': (
                'get_naskladnovaci_dashboard_link',
                ('cielovy_sklad', 'prijat_na_sklad'),
                ('notif_prijem_tg', 'notif_prijem_mail', 'notif_prijem_discord'),
            ),
            'classes': ('vivid-box-flash',),
            'description': '🎯 Nastavenia a správa fyzického príjmu vyťažených položiek na pobočky alebo hlavný sklad. 🏭'
        }),
        ('📥 ZÁKLADNÉ INFORMÁCIE O DOKUMENTE', {
            'fields': (
                'dodavatel', 
                ('kod_clenenia_dph', 'erp_predkontacia'),
                ('subor', 'zdroj', 'datum_prijatia')
            ),
            'description': '📁 Primárne údaje o prijatej faktúre, jej pôvode a daňovej kategorizácii pre kontrolný výkaz. 🏛️'
        }),
        ('🇪🇺 PEPPOL XML ARCHÍV (WORM)', {
            'fields': (
                ('peppol_document_id', 'xml_sha256_hash'),
                ('xml_subor', 'vizualizacia_subor'),
            ),
            'description': '🔐 Zákonná ochrana a kryptografický archív elektronických dokumentov pre sieť Peppol.'
        }),
        ('🏦 PLATOBNÉ ÚDAJE (VYŤAŽENÉ CEZ AI)', {
            'fields': (
                ('iban', 'variabilny_symbol'),
                ('suma_na_uhradu', 'mena', 'datum_splatnosti')
            ),
            'description': '🤖 Údaje, ktoré naša AI automaticky identifikovala a vyťažila priamo z nahratého dokumentu. ✨'
        }),
        ('🔗 PÁROVANIE S OBJEDNÁVKAMI (SMART MATCHING)', {
            'fields': (
                'suvisiace_objednavky', 
                'navrhnute_objednavky',
                'ignorovane_objednavky',
            ),
            'description': '🤝 Prepojenie prijatej faktúry s našimi odoslanými objednávkami. AI automaticky navrhuje zhody. 🔗'
        }),
        ('🤖 STAV A SYSTÉMOVÉ SPRACOVANIE', {
            'fields': (
                ('stav_uhrady', 'spracovane'),
                'poznamka'
            ),
            'description': '🔄 Sledovanie aktuálneho životného cyklu dokladu a výsledky automatizovaných operácií. ⚙️'
        }),
        ('📡 ISAS ERP DIAGNOSTIKA', { 
            'fields': ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log'),
            'description': '🛰️ Technické detaily a logy o synchronizácii dokladu s externým účtovným systémom. 📟'
        }),
    )

    isas_title = "📥 Obdržané faktúry"
    isas_preloader_text = "⏳ Vstupujete do zoznamu obdržaných faktúr..."
    isas_clean_fk_fields = {'cielovy_sklad': '👤 Vyberte cieľový sklad'}
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "🏢",
        'custom_buttons_html': mark_safe("""
            <button type="button" id="btn-ai-process" data-action="ai_process" class="isas-action-btn btn-purple">🤖 AI SPRACOVANIE</button>
            <button type="button" id="btn-pay-selected" data-action="mark_paid" class="isas-action-btn btn-green">💰 ZAPLATENÉ</button>
            <button type="button" id="btn-pair-order" class="isas-action-btn btn-brown">🔗 SPÁROVAŤ S OBJEDNÁVKOU</button>
        """),
        'ignore_default_action_ids': ["btn-ai-process", "btn-pay-selected", "btn-pair-order"]
    }

    isas_action_modals = {
        'btn-ai-process': {
            'type': 'direct_action',
            'action': 'ai_process',
            'confirm': True,
            'confirm_title': 'Spustiť AI vyťažovanie? 🤖',
            'confirm_text': 'Naozaj chcete odoslať vybrané prijaté faktúry umelej inteligencii na automatické vyťaženie položiek, súm a dodávateľa?',
            'loading_text': '⏳ SPRACOVÁVAM...',
            'success_text': '✅ HOTOVO',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jednu faktúru.',
            'reload': True
        },
        'btn-pay-selected': {
            'type': 'direct_action',
            'action': 'mark_paid',
            'confirm': True,
            'confirm_title': 'Úhrada prijatých faktúr 💰',
            'confirm_text': 'Naozaj chcete vybrané došlé faktúry označiť ako zaplatené? V systéme sa automaticky zaevidujú príslušné výdavkové doklady.',
            'loading_text': '⏳ SPRACOVÁVAM...',
            'success_text': '✅ ZAPLATENÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jednu faktúru.',
            'reload': True
        },
        'btn-pair-order': {
            'type': 'ajax_select_modal',
            'title': 'Spárovať s Objednávkou',
            'subtitle': 'Vyhľadajte našu odoslanú objednávku podľa čísla.',
            'icon': '🔗',
            'action': 'pair_order',
            'ajax_url': '/admin/Evidencia/prichodziafaktura/search-orders-for-pairing/',
            'placeholder': '🔍 Zadajte číslo objednávky...',
            'confirm_text': '🔗 Spárovať',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Na párovanie označte vždy presne jednu faktúru zo zoznamu.'
        }
    }

    isas_peppol_viz_modal = True
    isas_vivid_box_flash = True

    isas_page_reload_poll = True

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<int:object_id>/naskladnovaci-dashboard/', self.admin_site.admin_view(self.dashboard_naskladnenia_view), name='naskladnenie-dashboard'),
            path('search-orders-for-pairing/', self.admin_site.admin_view(self.search_orders_for_pairing_api), name='prichodziafaktura-search-orders'),
        ]
        return custom_urls + urls

    def search_orders_for_pairing_api(self, request):
        if not request.user.has_perm('Evidencia.view_odoslanaobjednavka'):
            return JsonResponse({'results': []}, status=403)
        
        term = request.GET.get('q', '').strip()
        qs = OdoslanaObjednavka.objects.select_related('dodavatel').all()
        if term:
            qs = qs.filter(Q(cislo_objednavky__icontains=term) | Q(dodavatel__Nazov_spolocnosti__icontains=term))
        qs = qs.order_by('-vytvorene')[:30]
        
        results = []
        for o in qs:
            dodavatel = escape(o.dodavatel.Nazov_spolocnosti) if o.dodavatel else "Neznámy dodávateľ"
            mena_o = getattr(o, 'mena', '') or get_firemna_mena()
            suma = f"{o.celkova_suma_s_dph} {mena_o}" if hasattr(o, 'celkova_suma_s_dph') else (f"{o.celkova_suma_bez_dph} {mena_o}" if hasattr(o, 'celkova_suma_bez_dph') else f"— {mena_o}")
            text = f"🛒 {o.cislo_objednavky} | 🏢 {dodavatel} | 💰 {suma}"
            results.append({'id': o.id, 'text': text})
            
        return JsonResponse({'results': results})

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):

        if action_type == 'ajax_update_filialka':
            if not request.user.has_perm('Evidencia.change_lognaskladnenia'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie upravovať faktúru. 🛑"}, status=403)
            try:
                data = json.loads(request.body)
                log_id = data.get('log_id')
                filialka_id = data.get('filialka_id')
                if not log_id:
                    return JsonResponse({"status": "error", "message": "Chýba ID záznamu. ⚠️"})
                log_item = LogNaskladnenia.objects.get(id=log_id)
                if filialka_id and str(filialka_id).isdigit():
                    log_item.filialka_id = int(filialka_id)
                else:
                    log_item.filialka = None
                log_item.save(update_fields=['filialka'])
                return JsonResponse({"status": "success", "message": "Sklad bol priradený. ✅"})
            except Exception as e:
                return JsonResponse({"status": "error", "message": str(e)}, status=400)

        if action_type == 'uloz_nastavenia_naskladnenia':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie upravovať faktúru. 🛑"}, status=403)
            if not valid_ids:
                return JsonResponse({"status": "error", "message": "Nebol vybraný žiadny doklad. ⚠️"}, status=400)
            try:
                data = json.loads(request.body)
                faktura = self.model.objects.get(id=valid_ids[0])
                cielovy_sklad_id = data.get('cielovy_sklad')
                if cielovy_sklad_id and str(cielovy_sklad_id).isdigit():
                    faktura.cielovy_sklad_id = int(cielovy_sklad_id)
                else:
                    faktura.cielovy_sklad = None
                faktura.prijat_na_sklad = data.get('prijat_na_sklad', True)
                faktura.notif_prijem_tg = data.get('notif_prijem_tg', True)
                faktura.notif_prijem_mail = data.get('notif_prijem_mail', True)
                faktura.notif_prijem_discord = data.get('notif_prijem_discord', True)
                faktura.save(update_fields=['cielovy_sklad', 'prijat_na_sklad', 'notif_prijem_tg', 'notif_prijem_mail', 'notif_prijem_discord'])
                return JsonResponse({"status": "success", "message": "✅ Nastavenia skladu uložené."})
            except Exception as e:
                return JsonResponse({"status": "error", "message": str(e)}, status=400)

        if action_type == 'naskladni_polozky':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie upravovať faktúru. 🛑"}, status=403)
            if not valid_ids:
                return JsonResponse({"status": "error", "message": "Nebol vybraný žiadny doklad. ⚠️"}, status=400)
            try:
                data = json.loads(request.body)
                faktura = self.model.objects.get(id=valid_ids[0])
                polozky_ids = data.get('polozky_ids', [])
                if not polozky_ids:
                    return JsonResponse({"status": "error", "message": "⚠️ Zvoľte aspoň jednu položku pre naskladnenie."})
                
                uspesne_pocet = 0
                chyby = []
                
                logy_na_update = []
                katalog_na_create = []
                katalog_na_update = []
                pohyby_na_create = []
                
                with transaction.atomic():
                    logy = LogNaskladnenia.objects.filter(id__in=polozky_ids, faktura=faktura, uspesne=False).select_related('navrhovany_vyrobca')
                    
                    for log in logy:
                        if log.navrhovana_kategoria == 'Sluzba': continue 
                        config = FLAT_CATEGORY_MAP.get(log.navrhovana_kategoria)
                        if not config:
                            chyby.append(f"Kategória '{log.navrhovana_kategoria}' nebola nájdená.")
                            continue
                            
                        SubModel = apps.get_model('Evidencia', config["model_name"])
                        ParentModel = apps.get_model('Evidencia', config["parent_model_name"])
                        parent_obj, _ = ParentModel.objects.get_or_create(id=1)
                        field_names = [f.name for f in SubModel._meta.get_fields()]
                        defaults = {'pocet_na_sklade': 0, config["fk_field"]: parent_obj}
                        
                        if 'dodavatel' in field_names and faktura.dodavatel: defaults['dodavatel'] = faktura.dodavatel
                        if 'vyrobca' in field_names and log.navrhovany_vyrobca: defaults['vyrobca'] = log.navrhovany_vyrobca
                        if 'typ' in field_names and log.navrhovany_typ: defaults['typ'] = log.navrhovany_typ
                        if 'kto_som' in field_names: defaults['kto_som'] = log.navrhovany_popis or "Import AI 🤖"
                        if 'ean_kod' in field_names and log.navrhovany_ean: defaults['ean_kod'] = log.navrhovany_ean
                        if 'merna_jednotka' in field_names and log.navrhovana_merna_jednotka: defaults['merna_jednotka'] = log.navrhovana_merna_jednotka
                        if 'sadzba_dph' in field_names and log.navrhovana_sadzba_dph: defaults['sadzba_dph'] = log.navrhovana_sadzba_dph
                        if 'katalogove_cislo' in field_names and log.navrhovane_katalogove_cislo: defaults['katalogove_cislo'] = log.navrhovane_katalogove_cislo
                        
                        try:
                            mnozstvo = int(Decimal(str(log.mnozstvo or 0)))
                        except (ValueError, TypeError, InvalidOperation):
                            mnozstvo = 0
                            
                        search_kwargs = {'seriove_cislo': log.najdeny_kod_faktura}
                        if 'typ' in field_names and log.navrhovany_typ: search_kwargs['typ'] = log.navrhovany_typ
                        
                        try:
                            polozka = SubModel.objects.select_for_update().get(**search_kwargs)
                            created = False
                        except SubModel.DoesNotExist:
                            polozka = SubModel(**search_kwargs, **defaults)
                            created = True
                        except SubModel.MultipleObjectsReturned:
                            polozka = SubModel.objects.select_for_update().filter(**search_kwargs).first()
                            created = False
                            
                        if faktura.prijat_na_sklad and not faktura.cielovy_sklad:
                            polozka.pocet_na_sklade = (polozka.pocet_na_sklade or 0) + mnozstvo
                            
                        if not created:
                            if 'vyrobca' in field_names and not getattr(polozka, 'vyrobca', True) and log.navrhovany_vyrobca: polozka.vyrobca = log.navrhovany_vyrobca
                            if 'dodavatel' in field_names and not getattr(polozka, 'dodavatel', True) and faktura.dodavatel: polozka.dodavatel = faktura.dodavatel
                            if 'kto_som' in field_names and not getattr(polozka, 'kto_som', True) and log.navrhovany_popis: polozka.kto_som = log.navrhovany_popis
                            if 'ean_kod' in field_names and not getattr(polozka, 'ean_kod', True) and log.navrhovany_ean: polozka.ean_kod = log.navrhovany_ean
                            if 'sadzba_dph' in field_names and log.navrhovana_sadzba_dph: polozka.sadzba_dph = log.navrhovana_sadzba_dph
                            if 'katalogove_cislo' in field_names and not getattr(polozka, 'katalogove_cislo', True) and log.navrhovane_katalogove_cislo: polozka.katalogove_cislo = log.navrhovane_katalogove_cislo
                        
                        polozka.save()
                        
                        if faktura.prijat_na_sklad:
                            StockMovementModel = apps.get_model('Evidencia', 'StockMovement')
                            pohyby_na_create.append(StockMovementModel(
                                filialka=faktura.cielovy_sklad, user=request.user, category=config["stock_cat"], 
                                subcategory=config["verbose_name"], product_name=ocisti_nazov_od_jednotky(str(polozka)),
                                action=StockMovementModel.ADD, count=mnozstvo, faktura=faktura, merna_jednotka=getattr(polozka, 'merna_jednotka', 'ks')
                            ))
                            
                        if log.najdeny_kod_faktura:
                            CenovyKatalog_model = apps.get_model('Evidencia', 'CenovyKatalog')
                            ct = ContentType.objects.get_for_model(polozka)
                            
                            try:
                                katalog_obj = CenovyKatalog_model.objects.select_for_update().get(content_type=ct, object_id=polozka.pk)
                                is_katalog_new = False
                            except CenovyKatalog_model.DoesNotExist:
                                katalog_obj = CenovyKatalog_model(content_type=ct, object_id=polozka.pk)
                                is_katalog_new = True
                                
                            try:
                                if log.nakupna_cena is not None and str(log.nakupna_cena).strip() != "":
                                    katalog_obj.nakupna_cena = Decimal(str(log.nakupna_cena).replace(',', '.'))
                                else:
                                    katalog_obj.nakupna_cena = katalog_obj.nakupna_cena or Decimal('0.00')
                            except InvalidOperation:
                                katalog_obj.nakupna_cena = katalog_obj.nakupna_cena or Decimal('0.00')
                                
                            if katalog_obj.marza_percento is None:
                                katalog_obj.marza_percento = Decimal('0.00')

                            nakupna_cena_safe = katalog_obj.nakupna_cena or Decimal('0.00')
                            marza_safe = katalog_obj.marza_percento or Decimal('0.00')
                            if nakupna_cena_safe > Decimal('0.00'):
                                katalog_obj.predajna_cena = (nakupna_cena_safe * (Decimal('1.00') + (marza_safe / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            else:
                                katalog_obj.predajna_cena = Decimal('0.00')
                                
                            if is_katalog_new:
                                katalog_na_create.append(katalog_obj)
                            else:
                                katalog_na_update.append(katalog_obj)
                                
                            log.uspesne = True
                            log.content_type = ct
                            log.object_id = polozka.pk
                            logy_na_update.append(log)
                            uspesne_pocet += 1
                            
                    if pohyby_na_create:
                        StockMovementModel.objects.bulk_create(pohyby_na_create)
                    if katalog_na_create:
                        CenovyKatalog_model.objects.bulk_create(katalog_na_create)
                    if katalog_na_update:
                        CenovyKatalog_model.objects.bulk_update(katalog_na_update, ['nakupna_cena', 'marza_percento', 'predajna_cena'])
                    if logy_na_update:
                        LogNaskladnenia.objects.bulk_update(logy_na_update, ['uspesne', 'content_type', 'object_id'])

                if chyby:
                    return JsonResponse({"status": "error", "message": f"Naskladňovanie prerušené: {chyby[0]}"})
                    
                if uspesne_pocet > 0:
                    from .tasks import odosli_notifikaciu_o_prijme_task
                    uspesne_logy_ids = [log.id for log in logy_na_update]
                    def _dispatch_prijem_notif(f_id=faktura.id, log_ids=uspesne_logy_ids):
                        try:
                            odosli_notifikaciu_o_prijme_task.delay(f_id, log_ids)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo odoslanie notifikácie o príjme do Celery: {exc}")
                            messages.warning(request, "⚠️ Položky boli naskladnené, ale notifikáciu sa nepodarilo odoslať (výpadok Celery/Redis).")
                    transaction.on_commit(_dispatch_prijem_notif)
                    
                ostavajuce_polozky = faktura.polozky_log.filter(uspesne=False).exclude(navrhovana_kategoria='Sluzba').count()
                kompletne = (ostavajuce_polozky == 0)
                return JsonResponse({"status": "success", "message": f"✨ Úspešne spracovaných {uspesne_pocet} položiek.", "kompletne": kompletne, "ostava": ostavajuce_polozky})
                
            except Exception as e:
                return JsonResponse({"status": "error", "message": f"Kritická chyba: {str(e)}"})

        from .tasks import task_hromadne_ai_spracovanie_faktur
        
        if action_type == 'mark_paid':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie upravovať faktúry. 🛑"}, status=403)
            
            count = 0
            with transaction.atomic():
                faktury_na_uhradu = self.model.objects.select_for_update().filter(
                    id__in=valid_ids
                ).exclude(stav_uhrady='ZAPLATENA')
                
                uhrady_na_vytvorenie = []
                faktury_na_update = []
                
                for f in faktury_na_uhradu:
                    try:
                        suma = Decimal(str(f.suma_na_uhradu or '0.00'))
                    except (InvalidOperation, TypeError, ValueError):
                        suma = Decimal('0.00')
                    doteraz_uhradene = f.uhrady.aggregate(Sum('suma_uhrady'))['suma_uhrady__sum'] or Decimal('0.00') if hasattr(f, 'uhrady') else Decimal('0.00')
                    zostatok = suma - doteraz_uhradene
                    if zostatok > Decimal('0.00'):
                        uhrady_na_vytvorenie.append(EvidenciaUhrad(
                            typ_uhrady='VPD',
                            suma_uhrady=zostatok,
                            prijata_faktura=f,
                            datum_uhrady=timezone.now().date(),
                            mena=f.mena or get_firemna_mena(),
                            poznamka=f"Ručne označené ako zaplatené dispečerom dňa {timezone.now().strftime('%d.%m.%Y')}"
                        ))
                        count += 1
                    f.stav_uhrady = 'ZAPLATENA'
                    faktury_na_update.append(f)
                        
                if uhrady_na_vytvorenie:
                    EvidenciaUhrad.objects.bulk_create(uhrady_na_vytvorenie)
                if faktury_na_update:
                    self.model.objects.bulk_update(faktury_na_update, ['stav_uhrady'])
                        
            pocet_uzavretych = len(faktury_na_uhradu)
            word = "doklad" if pocet_uzavretych == 1 else ("doklady" if 2 <= pocet_uzavretych <= 4 else "dokladov")
            return JsonResponse({"status": "success", "message": f"✅ Úspešne zaevidovaných {count} úhrad a uzavretých {pocet_uzavretych} {word}!"})
        
        elif action_type == 'ai_process':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie upravovať faktúry. 🛑"}, status=403)
            def _dispatch_ai_process(v_ids=valid_ids):
                try:
                    task_hromadne_ai_spracovanie_faktur.delay(v_ids)
                except Exception as exc:
                    logger.warning(f"Zlyhalo spustenie AI spracovania faktúr do Celery: {exc}")
                    messages.warning(request, "⚠️ AI spracovanie faktúr sa nepodarilo spustiť (výpadok Celery/Redis).")
            transaction.on_commit(_dispatch_ai_process)
            return JsonResponse({"status": "success", "message": "🤖 AI spracovanie bolo spustené na pozadí! 🚀"})
        
        elif action_type == 'pair_order':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie upravovať faktúru. 🛑"}, status=403)
            try:
                data = json.loads(request.body) if request.body else {}
                objednavka_ids = data.get('quote_ids', []) or data.get('order_ids', [])
            except (json.JSONDecodeError, Exception):
                return JsonResponse({"status": "error", "message": "Neplatný formát dát požiadavky."}, status=400)

            if not objednavka_ids:
                return JsonResponse({"status": "error", "message": "🚫 Nebola vybraná žiadna objednávka na spárovanie."}, status=400)

            if valid_ids and objednavka_ids:
                with transaction.atomic():
                    faktury = self.model.objects.select_for_update().filter(id__in=valid_ids).order_by('id')
                    for faktura in faktury:
                        faktura.suvisiace_objednavky.add(*objednavka_ids)
                        faktura.navrhnute_objednavky.remove(*objednavka_ids)
                return JsonResponse({"status": "success", "message": "🔗 Faktúra bola úspešne spárovaná s objednávkami!"})

        elif action_type == 'smart_pair':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie upravovať faktúru. 🛑"}, status=403)
            try:
                data = json.loads(request.body)
                left_id = data.get('left_id')
                right_id = data.get('right_id')
            except Exception:
                return JsonResponse({"status": "error", "message": "Neplatné dáta."}, status=400)

            if not left_id or not right_id:
                return JsonResponse({"status": "error", "message": "Chýbajúce identifikátory pre párovanie."}, status=400)
                
            with transaction.atomic():
                try:
                    faktura = self.model.objects.select_for_update().get(id=left_id)
                    faktura.suvisiace_objednavky.add(right_id)
                    faktura.navrhnute_objednavky.remove(right_id)
                    faktura.ignorovane_objednavky.remove(right_id)
                except self.model.DoesNotExist:
                    pass
            return JsonResponse({"status": "success", "message": "🔗 Úspešne spárované!"})

        elif action_type == 'smart_ignore':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie upravovať faktúru. 🛑"}, status=403)
            try:
                data = json.loads(request.body)
                left_id = data.get('left_id')
                right_id = data.get('right_id')
            except Exception:
                return JsonResponse({"status": "error", "message": "Neplatné dáta."}, status=400)

            if not left_id or not right_id:
                return JsonResponse({"status": "error", "message": "Chýbajúce identifikátory pre akciu."}, status=400)
                
            with transaction.atomic():
                try:
                    faktura = self.model.objects.select_for_update().get(id=left_id)
                    faktura.ignorovane_objednavky.add(right_id)
                    faktura.navrhnute_objednavky.remove(right_id)
                except self.model.DoesNotExist:
                    pass
            return JsonResponse({"status": "success", "message": "🚫 Návrh bol ignorovaný."})

        elif action_type == 'generate_peppol_pdf':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie na túto akciu. 🛑"}, status=403)
            if not valid_ids:
                return JsonResponse({"status": "error", "message": "Nebol vybraný žiadny doklad."}, status=400)
            try:
                faktura = self.model.objects.get(id=valid_ids[0])
            except self.model.DoesNotExist:
                return JsonResponse({"status": "error", "message": "Faktúra nebola nájdená."}, status=404)

            if faktura.vizualizacia_subor and getattr(faktura.vizualizacia_subor, 'name', None):
                try:
                    return JsonResponse({"status": "success", "pdf_url": faktura.vizualizacia_subor.url})
                except (ValueError, AttributeError):
                    pass
                
            from .utils_peppol_pdf import PeppolPDFGenerator
            try:
                pdf_url = PeppolPDFGenerator.vygeneruj_pdf(faktura)
                return JsonResponse({"status": "success", "pdf_url": pdf_url})
            except Exception as e:
                return JsonResponse({"status": "error", "message": str(e)}, status=500)

        elif action_type == 'send_peppol_email':
            if not request.user.has_perm('Evidencia.change_prichodziafaktura'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie na túto akciu. 🛑"}, status=403)
            if not valid_ids:
                return JsonResponse({"status": "error", "message": "Nebol vybraný žiadny doklad."}, status=400)
            from .tasks import posli_peppol_vizualizaciu_uctovnicke_task
            target_id = valid_ids[0]
            def _dispatch_peppol_viz(vid=target_id):
                try:
                    posli_peppol_vizualizaciu_uctovnicke_task.delay(vid)
                except Exception as exc:
                    logger.warning(f"Zlyhalo odoslanie Peppol vizualizácie do Celery pre doklad #{vid}: {exc}")
                    messages.warning(request, "⚠️ E-mail s Peppol vizualizáciou sa nepodarilo zaradiť do fronty (výpadok Celery/Redis).")
            transaction.on_commit(_dispatch_peppol_viz)
            return JsonResponse({"status": "success", "message": "✅ E-mail s XML a PDF bol úspešne odoslaný účtovníčke."})

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    def get_smart_suggestions(self, request, obj=None):
        smart_suggestions = []
        navrhy = self.model.objects.filter(
            navrhnute_objednavky__isnull=False
        ).distinct().select_related('dodavatel').prefetch_related('navrhnute_objednavky')
        
        for faktura in navrhy:
            dodavatel_nazov = faktura.dodavatel.Nazov_spolocnosti if faktura.dodavatel else "Neznámy"
            for obj_navrh in faktura.navrhnute_objednavky.all():
                mena_fa = faktura.mena or get_firemna_mena()
                suma_str = f"{faktura.suma_na_uhradu} {mena_fa}" if faktura.suma_na_uhradu is not None else f"? {mena_fa}"
                vs_zobrazenie = faktura.variabilny_symbol if faktura.variabilny_symbol else f"ID:{faktura.id}"
                
                smart_suggestions.append({
                    'left_id': faktura.id,
                    'left_label': '🧾 Prijatá Faktúra',
                    'left_str': f"FA: {vs_zobrazenie} | {dodavatel_nazov} | {suma_str}",
                    'right_id': obj_navrh.id,
                    'right_label': '🛒 Naša Objednávka',
                    'right_str': f"OBJ: {obj_navrh.cislo_objednavky}",
                    'dovod': "🤖 AI Agent našiel zhodu v texte faktúry s našou odoslanou objednávkou."
                })
        return smart_suggestions

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            faktury_na_zmazanie = self.model.objects.filter(id__in=valid_ids)
            zablokovane_platba = faktury_na_zmazanie.filter(stav_uhrady='ZAPLATENA').count()
            ma_naviazane_uhrady = any(f.uhrady.exists() for f in faktury_na_zmazanie)
            zablokovane_peppol = faktury_na_zmazanie.exclude(xml_sha256_hash__isnull=True).exclude(xml_sha256_hash__exact='').count()
            
            if zablokovane_platba > 0 or ma_naviazane_uhrady or zablokovane_peppol > 0:
                return JsonResponse({
                    "status": "error", 
                    "message": "❌ Zmazanie zablokované! Niektoré faktúry sú zaplatené, majú úhrady alebo ide o zákonom chránený Peppol XML archív. 🔒"
                }, status=400)
        return super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('dodavatel').filter(
            Q(dodavatel__Nazov_spolocnosti__icontains=term) |
            Q(dodavatel__ico__icontains=term) |
            Q(variabilny_symbol__icontains=term) |
            Q(iban__icontains=term)
        ).order_by('-datum_prijatia')[:30]

    def format_detective_result(self, obj):
        dodavatel = getattr(obj.dodavatel, 'Nazov_spolocnosti', 'Neznámy dodávateľ') if obj.dodavatel else "Neznámy dodávateľ"
        datum = obj.datum_prijatia.strftime("%d.%m.%Y") if obj.datum_prijatia else "Neznámy čas"
        mena_fa = obj.mena or get_firemna_mena()
        suma_str = f"{obj.suma_na_uhradu} {mena_fa}" if obj.suma_na_uhradu is not None else "Neznáma suma"
        vs_zobrazenie = obj.variabilny_symbol if obj.variabilny_symbol else f"ID:{obj.id}"
        
        return {
            'id': obj.id, 
            'name': vs_zobrazenie, 
            'search_value': vs_zobrazenie,
            'rola': f"🏢 {dodavatel}",
            'suma': suma_str, 
            'date': datum,
            'cat': 'FAKTÚRA'
        }

    def has_delete_permission(self, request, obj=None):
        if obj:
            je_zaplatena = obj.stav_uhrady == 'ZAPLATENA'
            ma_uhrady = obj.uhrady.exists()
            je_peppol = bool(getattr(obj, 'xml_sha256_hash', None)) 
            
            if je_zaplatena or ma_uhrady or je_peppol:
                return False
        return super().has_delete_permission(request, obj)

    def get_search_results(self, request, queryset, search_term):
        queryset, use_distinct = super().get_search_results(request, queryset, search_term)
        
        # Filter podľa dodávateľa
        dodavatel_id = request.GET.get('filter_dodavatel_id')
        if dodavatel_id:
            queryset = queryset.filter(dodavatel_id=dodavatel_id)
            
        return queryset, use_distinct

    def dashboard_naskladnenia_view(self, request, object_id):

        faktura = get_object_or_404(PrichodziaFaktura, id=object_id)
        
        aktivne_filialky = Filialka.objects.filter(je_aktivna=True)
        emoji_map = {text: emoji_text for text, emoji_text in CATEGORY_CHOICES}
        
        # Preklad technických názvov na ľudské
        reverse_cat_map = {}
        for main_cat, subcats in CATEGORY_MAP.items():
            for verbose_name, model_class in subcats.items():
                reverse_cat_map[model_class.__name__] = verbose_name
        
        # Eliminácia N+1 dopytu pre AI Výrobcu (prefetch relácie)
        polozky_log = faktura.polozky_log.select_related('navrhovany_vyrobca').order_by('id')
        polozky_data = []
        for p in polozky_log:
            # Preklad kategórie
            human_kategoria = reverse_cat_map.get(p.navrhovana_kategoria, p.navrhovana_kategoria)
            if p.navrhovana_kategoria == 'Sluzba':
                human_kategoria = '🛠️ Služba / Práca'
                
            polozky_data.append({
                'id': p.id,
                'kod': p.najdeny_kod_faktura or '',
                'nazov': p.navrhovany_nazov or '',
                'kategoria': human_kategoria or '',
                'hlavna_kategoria': p.hlavna_kategoria or ('Služby a Práce' if p.navrhovana_kategoria == 'Sluzba' else ''),
                'mnozstvo': float(p.mnozstvo) if p.mnozstvo else 0,
                'cena': float(p.nakupna_cena) if p.nakupna_cena else 0,
                'je_sluzba': True if p.navrhovana_kategoria == 'Sluzba' else False,
                'uspesne': p.uspesne,
                'v_db': True if p.object_id else False,
                'vyrobca': p.navrhovany_vyrobca.nazov_vyrobcu if p.navrhovany_vyrobca else '',
                'typ_on': p.navrhovany_typ or '',
                'popis': p.navrhovany_popis or '',
                'ean': p.navrhovany_ean or '',
                'merna_jednotka': p.navrhovana_merna_jednotka or 'ks',
                'dph': float(p.navrhovana_sadzba_dph) if p.navrhovana_sadzba_dph else float(get_default_dph() or Decimal('0.00')),
                'katalogove_cislo': p.navrhovane_katalogove_cislo or '',
            })

        # Vytiahneme jediný existujúci záznam Hlavného Skladu (Singleton)
        hlavny_sklad_obj = Sklad.objects.first()
        nazov_hlavneho_skladu = f"Hlavný sklad ({hlavny_sklad_obj.nazov})" if hlavny_sklad_obj and hlavny_sklad_obj.nazov and hlavny_sklad_obj.nazov.lower() != "hlavný sklad" else "Hlavný sklad"

        context = dict(
            self.admin_site.each_context(request),
            original=faktura,
            aktivne_filialky=aktivne_filialky,
            js_category_emojis=json.dumps(emoji_map),
            polozky_json=json.dumps(polozky_data),
            nazov_hlavneho_skladu=nazov_hlavneho_skladu,  # Posielame dynamický názov do šablóny
            title=f"📦 Naskladnenie faktúry: {faktura}"
        )

        return render(request, "admin/naskladnenie_poloziekdielov.html", context)

    @admin.display(description='')
    def get_naskladnovaci_dashboard_link(self, obj):
        if not obj or not obj.pk:
            return render_isas_text("Najskôr faktúru uložte 💾", color="#94a3b8", weight=800)
            
        url = f"../naskladnovaci-dashboard/"
        
        btn = render_isas_link_btn(
            url=url,
            text="Naskladnenie",
            icon="🛒📦",
            type_color="purple",
            wrapper=False,
            css_class="isas-action-btn btn-purple",
            custom_style="padding: 14px 28px; border-radius: 12px; font-size: 15px; font-weight: 900; letter-spacing: 1px;"
        )
        
        return render_isas_layout(btn, padding="5px 0")  

    @admin.display(description='🏢 Identita / Dodávateľ', ordering='dodavatel')
    def get_faktura_identity(self, obj):
        dodavatel_nazov = getattr(obj.dodavatel, 'Nazov_spolocnosti', 'Neznámy dodávateľ') if obj.dodavatel else 'Neznámy dodávateľ'
        vs = obj.variabilny_symbol if obj.variabilny_symbol else "Bez VS"
        
        image_url = None
        if obj.dodavatel and getattr(obj.dodavatel, 'domena', None):
            clean_domain = str(obj.dodavatel.domena).lower().strip()
            for prefix in ['https://', 'http://', 'www.']:
                if clean_domain.startswith(prefix):
                    clean_domain = clean_domain[len(prefix):]
            
            clean_domain = clean_domain.split('/')[0].split('?')[0].split('#')[0]
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
                
            if '.' in clean_domain and len(clean_domain) > 3:
                image_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"

        badge_id = render_isas_badge(f"#{obj.id}", prefix_text="ID:", type_color="neutral", wrapper=False)
        badge_vs = render_isas_badge(vs, prefix_text="VS:", type_color="info", wrapper=False)
        
        zdroj_farba = "purple" if obj.zdroj == 'DISCORD' else ("info" if obj.zdroj == 'TELEGRAM' else ("warning" if obj.zdroj == 'PEPPOL' else "neutral"))
        badge_zdroj = render_isas_badge(obj.get_zdroj_display(), type_color=zdroj_farba, wrapper=False)
        
        badges_row = render_isas_layout(format_html('{}{}{}', badge_id, badge_vs, badge_zdroj), layout="row", gap=4, center=True)

        return render_isas_avatar(
            icon="🏢",
            title=dodavatel_nazov,
            subtitle=None,
            bg_gradient="#6366f1, #4f46e5",
            shadow_color="rgba(99, 102, 241, 0.25)",
            extra_html=badges_row,
            image_url=image_url
        )

    @admin.display(description='📅 Termíny', ordering='datum_splatnosti')
    def get_terminy_card(self, obj):
        d_prijatia = obj.datum_prijatia.strftime("%d.%m.%Y") if obj.datum_prijatia else "—"
        d_vystavenia = obj.datum_vystavenia.strftime("%d.%m.%Y") if hasattr(obj, 'datum_vystavenia') and obj.datum_vystavenia else "—"
        splatnost = to_date(obj.datum_splatnosti)
        d_splatnosti = splatnost.strftime("%d.%m.%Y") if splatnost else "—"

        dnes = timezone.now().date()
        is_overdue = bool(splatnost and obj.stav_uhrady != 'ZAPLATENA' and splatnost < dnes)

        row1 = render_isas_flex_between(
            render_isas_text("📥 Prijaté:", size=10, color="#475569"),
            render_isas_text(d_prijatia, size=10, weight=800)
        )
        row2 = render_isas_flex_between(
            render_isas_text("📝 Vystavené:", size=10, color="#475569"),
            render_isas_text(d_vystavenia, size=10, weight=800)
        )
        
        splatnost_bg = "#fff1f2" if is_overdue else "#ffffff"
        splatnost_color = "#e11d48" if is_overdue else "#0f172a"
        splatnost_border = "#fda4af" if is_overdue else "#e2e8f0"
        
        row3_content = render_isas_flex_between(
            render_isas_text("⏳ Splatnosť:", size=10, weight=600, color=splatnost_color),
            render_isas_text(d_splatnosti, size=10, weight=800, color=splatnost_color)
        )
        row3 = render_isas_layout(
            row3_content, layout="row", bg_color=splatnost_bg, border_color=splatnost_border,
            padding="2px 6px", border_radius="4px", custom_style="margin-top: 2px;"
        )
        
        return render_isas_layout(
            format_html('{}{}{}', row1, row2, row3),
            layout="col", gap=3, custom_style="min-width: 145px; font-family: sans-serif; line-height: 1.1;"
        )

    @admin.display(description='💰 Financie', ordering='suma_na_uhradu')
    def get_financie_card(self, obj):
        suma = f"{obj.suma_na_uhradu:.2f}" if obj.suma_na_uhradu else "0.00"
        
        # 🛡️ DEFENSIVE: Normalizácia, sanitizácia XSS a ošetrenie prázdnych znakov
        raw_mena = getattr(obj, 'mena', None)
        mena_str = str(raw_mena).strip().upper() if raw_mena is not None else ""
        mena_kod = escape(mena_str) if mena_str else get_firemna_mena()
        
        sum_left = render_isas_text("Suma", size=9, weight=800, color="#166534", custom_style="text-transform: uppercase; letter-spacing: 0.5px; opacity: 0.8;")
        sum_right = render_isas_text(f"{suma} {mena_kod}", size=13, weight=900, color="#166534", custom_style="white-space: nowrap;")
        sum_row = render_isas_layout(
            render_isas_flex_between(sum_left, sum_right),
            layout="row", bg_color="#f0fdf4", border_color="#bbf7d0", padding="3px 8px", border_radius="6px",
            shadow="0 1px 2px rgba(22, 163, 74, 0.05)", custom_style="box-sizing: border-box; width: 100%;"
        )
        
        if obj.iban and str(obj.iban).strip() != "None":
            safe_iban = escape(obj.iban)
            iban_short = f"{safe_iban[:4]} {safe_iban[4:8]}...{safe_iban[-4:]}"
            iban_html = render_isas_badge(iban_short, icon="💳", type_color="neutral", copy_value=obj.iban, wrapper=False)
        else:
            iban_html = render_isas_text("⚠️ IBAN chýba", size=10, weight=800, color="#94a3b8", custom_style="padding: 3px 0;")
            
        dph_farba = "warning" if obj.kod_clenenia_dph in ['PP', 'PDP', 'VAT_REVERSE'] else "neutral"
        dph_kod_text = obj.kod_clenenia_dph or "—"
        dph_badge = render_isas_badge(f"DPH: {dph_kod_text}", icon="🏛️", type_color=dph_farba, wrapper=False)
            
        return render_isas_layout(
            format_html('{}{}{}', sum_row, iban_html, dph_badge),
            layout="col", gap=3, custom_style="min-width: 140px; align-items: flex-start;"
        )

    @admin.display(description='🧠 AI & 📎 Súbory (PEPPOL)')
    def get_ai_a_subor(self, obj):
        if obj.zdroj == 'PEPPOL' or (obj.xml_subor and hasattr(obj.xml_subor, 'url')):
            if not obj.spracovane:
                ai_badge = render_isas_badge("🇪🇺 PEPPOL (Triedenie položiek... ⏳)", type_color="warning", wrapper=False, custom_style="margin-bottom: 4px;")
            else:
                ai_badge = render_isas_badge("🇪🇺 eFaktúra (PEPPOL ✅)", type_color="success", wrapper=False, custom_style="margin-bottom: 4px;")
        elif obj.spracovane:
            ai_badge = render_isas_badge("🤖 AI HOTOVO", type_color="purple", wrapper=False, custom_style="margin-bottom: 4px;")
        else:
            ai_badge = render_isas_badge("⏳ ČAKÁ NA AI", type_color="neutral", wrapper=False, custom_style="margin-bottom: 4px;")

        pdf_btn = ""
        if obj.subor and hasattr(obj.subor, 'url'): 
            pdf_btn = render_isas_link_btn(obj.subor.url, "PDF", icon="📄", type_color="neutral", wrapper=False, custom_style="padding: 4px 8px; font-size: 10px;")

        xml_btn = ""
        vizualizacia_btn = ""
        if obj.xml_subor and hasattr(obj.xml_subor, 'url'):
            safe_hash = escape(obj.xml_sha256_hash or '—')
            xml_btn = render_isas_link_btn(
                obj.xml_subor.url, "XML", icon="📄", type_color="neutral", wrapper=False,
                tooltip=f"🔐 Šifrovacia SHA-256 pečať (WORM):\n{safe_hash}", custom_style="padding: 4px 8px; font-size: 10px;"
            )
            
            if obj.vizualizacia_subor and hasattr(obj.vizualizacia_subor, 'url'):
                vizualizacia_btn = render_isas_link_btn(
                    url=obj.vizualizacia_subor.url, text="Zobraziť PDF", icon="👁️", type_color="info", is_button=False, wrapper=False,
                    css_class="isas-peppol-viz-btn", data_attrs={"id": str(obj.id), "url": obj.vizualizacia_subor.url}, full_width=True, custom_style="padding: 4px 8px; font-size: 10px;"
                )
            else:
                vizualizacia_btn = render_isas_link_btn(
                    url=None, text="Vygenerovať PDF", icon="⚙️", type_color="warning", is_button=True, wrapper=False,
                    css_class="isas-peppol-viz-btn", data_attrs={"id": obj.id}, full_width=True, custom_style="padding: 4px 8px; font-size: 10px;"
                )

        buttons_list = []
        if pdf_btn or xml_btn:
            row1 = render_isas_layout(format_html('{}{}', pdf_btn, xml_btn), layout="row", gap=4)
            buttons_list.append(row1)
            
        if vizualizacia_btn:
            buttons_list.append(vizualizacia_btn)
            
        if not pdf_btn and not xml_btn and not vizualizacia_btn:
            buttons_list.append(render_isas_text("🚫 Bez súborov", size=9, weight=800, color="#94a3b8", custom_style="padding: 2px 0;"))
            
        buttons_container = render_isas_layout(mark_safe("".join(buttons_list)), layout="col", gap=6, custom_style="width: 100%;")

        cell_layout = render_isas_layout(
            format_html('{}{}', ai_badge, buttons_container),
            layout="col", gap=4, custom_style="align-items: flex-start; justify-content: center;"
        )

        # Polling aktivujeme LEN pri bežiacom importe z PEPPOL siete, nie pri pasívnych dokladoch čakajúcich na manuálne AI vyťaženie
        je_aktivny_peppol_import = (obj.zdroj == 'PEPPOL' or bool(obj.xml_subor)) and not obj.spracovane
        if je_aktivny_peppol_import:
            return render_isas_layout(cell_layout, css_class="isas-reload-poller", data_attrs={"id": str(obj.id)})

        return cell_layout

    @admin.display(description='🔗 Párovanie')
    def get_sparovanie_status(self, obj):
        objednavky = obj.suvisiace_objednavky.all()
        
        if not objednavky:
            return render_isas_layout(
                render_isas_text("❌ Nespárované", size=10, weight=800, color="#94a3b8", custom_style="text-transform: uppercase;"),
                layout="col", center=True, bg_color="#f8fafc", border_color="#cbd5e1", padding="6px 10px", border_radius="8px",
                custom_style="border-style: dashed; min-width: 110px;"
            )
            
        html_items = []
        for o in objednavky:
            url = reverse('admin:Evidencia_odoslanaobjednavka_change', args=[o.id])
            text = f"{o.cislo_objednavky} | {o.celkovy_pocet_poloziek} pol."
            btn = render_isas_link_btn(url, text, icon="🛒", type_color="info", wrapper=False, custom_style="white-space: nowrap; padding: 4px 8px; font-size: 10px;")
            html_items.append(btn)
            
        items_str = format_html_join('', '{}', [(mark_safe(btn),) for btn in html_items])
        
        title = render_isas_text("🔗 Spárované s:", size=9, weight=900, color="#1e3a8a", custom_style="text-transform: uppercase; letter-spacing: 0.5px;")
        links_col = render_isas_layout(items_str, layout="col", gap=4)
        
        return render_isas_layout(
            format_html('{}{}', title, links_col),
            layout="col", gap=4, center=True, bg_color="#f0f9ff", border_color="#bae6fd", padding="6px", border_radius="8px",
            custom_style="min-width: 110px;"
        )

    @admin.display(description='💳 Stav', ordering='stav_uhrady')
    def get_stav_uhrady_badge(self, obj):
        if obj.stav_uhrady == 'ZAPLATENA':
            return render_isas_badge("ZAPLATENÁ", icon="✅", type_color="success", custom_style="padding: 6px 12px; font-size: 11px;")
        
        dnes = timezone.now().date()
        splatnost = to_date(obj.datum_splatnosti)
        if splatnost and splatnost < dnes:
            neuhradene_text = render_isas_text("NEUHRADENÉ", size=9, weight=700, align="center", custom_style="margin-top: 2px;")
            return render_isas_badge(
                "PO SPLATNOSTI", icon="🔴", type_color="danger", 
                suffix_html=neuhradene_text,
                custom_style="padding: 6px 12px; font-size: 11px; flex-direction: column;"
            )

        return render_isas_badge("ČAKÁ NA ÚHRADU", icon="🟡", type_color="warning", custom_style="padding: 6px 12px; font-size: 11px;")

    @admin.display(description='💸 Executive Pay Terminál')
    def get_platobny_terminal(self, obj):
        if not obj.pk or not obj.iban or not obj.suma_na_uhradu:
            content = format_html('{} {}', render_isas_text("🤖", size=16), render_isas_text("Platobné dáta chýbajú alebo neboli vyťažené.", size=10, weight=600, color="#64748b"))
            return render_isas_layout(
                content, layout="row", center=True, gap=8, bg_color="#f8fafc", border_color="#cbd5e1",
                padding="8px 12px", border_radius="10px", custom_style="border-style: dashed; min-width: 280px; height: 50px;"
            )

        mena_kod = escape(str(obj.mena).strip().upper()) if getattr(obj, 'mena', None) else get_firemna_mena()
        qr_base64 = None
        if mena_kod == 'EUR' and obj.suma_na_uhradu > Decimal('0.00') and hasattr(obj, 'get_qr_code_base64'):
            try:
                qr_base64 = obj.get_qr_code_base64()
            except Exception:
                qr_base64 = None
        is_paid = obj.stav_uhrady == 'ZAPLATENA'
        
        bg_gradient = "linear-gradient(135deg, #10b981 0%, #059669 100%)" if is_paid else "linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%)"
        text_color = "#ffffff" if is_paid else "#334155"
        box_shadow = "0 4px 6px -1px rgba(16, 185, 129, 0.15)" if is_paid else "0 2px 4px rgba(0,0,0,0.02)"
        border_color = "#059669" if is_paid else "#e2e8f0"
        
        title_text = '✅ UHRADENÉ' if is_paid else '💸 PAY TERMINÁL'
        title_color = '#ffffff' if is_paid else '#0f172a'
        splatnost_text = obj.datum_splatnosti.strftime('%d.%m.%Y') if obj.datum_splatnosti else '—'
        var_symbol = obj.variabilny_symbol or '—'
        
        title_html = render_isas_text(title_text, size=10, weight=900, color=title_color, custom_style="text-transform: uppercase; letter-spacing: 0.5px; line-height: 1;")
        
        sum_color = '#ffffff' if is_paid else '#2563eb'
        label_color = '#d1fae5' if is_paid else '#64748b'
        sum_html = render_isas_text(f"{obj.suma_na_uhradu} {mena_kod}", size=12, weight=900, color=sum_color, custom_style="white-space: nowrap;")
        
        vs_label = render_isas_text("VS: ", size=9, weight=600, color=label_color)
        vs_val = render_isas_text(var_symbol, size=9, weight=800, color=text_color)
        vs_html = render_isas_layout(format_html('{}{}', vs_label, vs_val), layout="row", gap=2, custom_style="white-space: nowrap;")
        
        splatnost_label = render_isas_text("Splatnosť: ", size=9, weight=600, color=label_color)
        splatnost_val = render_isas_text(splatnost_text, size=9, weight=800, color=text_color)
        splatnost_html = render_isas_layout(format_html('{}{}', splatnost_label, splatnost_val), layout="row", gap=2, custom_style="white-space: nowrap;")
        
        sub_row = render_isas_layout(format_html('{}{}{}', sum_html, vs_html, splatnost_html), layout="row", gap=8, custom_style="margin-top: 2px; background: transparent !important;")
        left_col = render_isas_layout(format_html('{}{}', title_html, sub_row), layout="col", gap=2, custom_style="flex: 1;")

        if not is_paid and qr_base64:
            qr_img = render_isas_image(qr_base64, width=48, height=48, custom_style="display: block; border-radius: 4px; border: none;")
            right_col = render_isas_layout(qr_img, layout="col", center=True, bg_color="white", padding="3px", border_radius="6px", shadow="0 1px 2px rgba(0,0,0,0.05)", custom_style="flex-shrink: 0;")
        elif is_paid:
            right_col = render_isas_layout(render_isas_text("🛡️", size=24, custom_style="filter: drop-shadow(0 1px 2px rgba(0,0,0,0.1));"), layout="col", center=True, custom_style="width: 48px; flex-shrink: 0;")
        else:
            right_col = ""

        return render_isas_layout(
            render_isas_flex_between(left_col, right_col, extra_style="gap: 10px;"),
            layout="row", border_color=border_color, padding="6px 12px", border_radius="10px", shadow=box_shadow,
            custom_style=f"background: {bg_gradient}; min-width: 280px; max-height: 75px;"
        )

    class Media:
        css = {
            'all': (
                'admin/css/vendor/select2/select2.css',
                'admin/css/autocomplete.css',
            )
        }
        js = (
            'admin/js/vendor/jquery/jquery.js',
            'admin/js/vendor/select2/select2.full.js',
            'admin/js/jquery.init.js',
        )

@admin.register(LogNaskladnenia)
class LogNaskladneniaAdmin(ISASHiddenAdminMixin, ISASPremiumAdmin):
    pass

class ExportTypFilter(admin.SimpleListFilter):
    title = '📂 Kategória dát'
    parameter_name = 'typ_zalohy'

    def lookups(self, request, model_admin):
        return (
            ('financie', '💰 Financie a Obchod (Faktúry, Ponuky)'),
            ('prevadzka', '🛠️ Prevádzka (Protokoly, Úlohy)'),
            ('fleet', '🚗 Fleet (Kniha jázd, Tankovania)'),
            ('sklad', '📦 Sklad (Katalóg, Pohyby, Objednávky)'),
            ('hr', '👥 HR a Vstupy (Zmluvy, Mzdy, Certifikáty)'),
            ('dokumentacia', '📖 Dokumentácia (Špecifikácie, QR)'),
            ('system', '⚙️ Systém (DB Dump, Audit Logy)'),
            ('externe', '🗂️ Externé (Mimosystémové)'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'financie':
            return queryset.filter(typ_zalohy__in=['vystavene_faktury', 'prichodzie_faktury', 'cenove_ponuky', 'evidencia_uhrad'])
        if val == 'prevadzka':
            return queryset.filter(typ_zalohy__in=['servisne_protokoly', 'ulohy_a_vykazy'])
        if val == 'fleet':
            return queryset.filter(typ_zalohy__in=['kniha_jazd', 'tankovania'])
        if val == 'sklad':
            return queryset.filter(typ_zalohy__in=['cenovy_katalog', 'skladove_pohyby', 'odoslane_objednavky', 'prijate_objednavky'])
        if val == 'hr':
            return queryset.filter(typ_zalohy__in=['hr_dokumenty', 'certifikaty', 'povolenia_vstupov', 'podklady_mzdy'])
        if val == 'dokumentacia':
            return queryset.filter(typ_zalohy__in=['technicke_specifikacie', 'qr_kody_zariadeni'])
        if val == 'system':
            return queryset.filter(typ_zalohy__in=['kompletny_dump', 'audit_logy'])
        if val == 'externe':
            return queryset.filter(typ_zalohy='mimosystemova')
        return queryset

class ExportRetenciaFilter(admin.SimpleListFilter):
    title = '⏱️ Pravidlo uchovania (Retencia)'
    parameter_name = 'politika_uchovania'

    def lookups(self, request, model_admin):
        return (
            ('kratkodobe', '⏱️ Krátkodobé (do 30 dní)'),
            ('dlhodobe', '📅 Dlhodobé (90 - 365 dní)'),
            ('navzdy', '♾️ Trvalý archív (Navždy)'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'kratkodobe':
            return queryset.filter(politika_uchovania__in=['24_hodin', '7_dni', '30_dni'])
        if val == 'dlhodobe':
            return queryset.filter(politika_uchovania__in=['90_dni', '365_dni'])
        if val == 'navzdy':
            return queryset.filter(politika_uchovania='navzdy')
        return queryset

@admin.register(MegaZaloha)
class MegaZalohaAdmin(ISASPremiumAdmin):
    list_display = (
        'get_export_card', 
        'get_typ_exportu_badge', 
        'get_retencia_a_velkost', 
        'get_stav_spracovania_badge', 
        'get_stiahnut_link'
    )
    list_display_links = ('get_export_card',)    
    list_filter = (ExportTypFilter, 'stav_spracovania', ExportRetenciaFilter, ISASDateRangeFilter)
    isas_date_filter_field = 'datum_vytvorenia'
    search_fields = ('nazov_zalohy', 'poznamka', 'vyziadal_uzivatel__username')
    list_select_related = ('vyziadal_uzivatel',)
    
    readonly_fields = ('velkost_v_mb', 'datum_exspiracie', 'datum_vytvorenia', 'stav_spracovania')

    fieldsets = (
        ('🗃️ Základná identifikácia exportu', {
            'fields': (
                ('nazov_zalohy', 'typ_zalohy'), 
                'subor_zalohy'
            ),
            'description': (
                'Zvoľte kategóriu dát a pomenujte export tak, aby bol neskôr ľahko identifikovateľný. 🏷️ '
                'Systém na základe typu automaticky zozbiera dáta z databázy a vygeneruje ZIP alebo Excel súbor. 📦'
            )
        }),
        ('📅 Filtrovanie dát (Optimalizácia výkonu)', {
            'fields': (
                ('obsahuje_data_od', 'obsahuje_data_do'),
            ),
            'description': (
                '⚠️ <b>Kritické pre veľké firmy:</b> Ak nevyplníte dátumy, systém stiahne celú históriu od založenia firmy, '
                'čo môže trvať dlho a vytvoriť obrovský súbor. Pre bežné účtovné reporty vždy zvoľte konkrétny mesiac alebo kvartál. 📊'
            )
        }),
        ('⏱️ Ochrana úložiska a Stav spracovania', {
            'fields': (
                ('stav_spracovania', 'politika_uchovania'),
                ('datum_exspiracie', 'velkost_v_mb'),
            ),
            'description': (
                '🛡️ <b>DevOps Ochrana:</b> Aby nedošlo k preplneniu firemného servera (Docker Volumes), '
                'systém po uplynutí zvolenej retencie fyzicky zmaže súbor z disku. 🗑️ '
                'Pre bežné prenosy dát účtovníčke zvoľte 7 alebo 30 dní. Trvalý archív používajte len pre ročné uzávierky. 🏛️'
            )
        }),
        ('👤 Audit a Poznámky', {
            'fields': (
                ('vyziadal_uzivatel', 'datum_vytvorenia'),
                'poznamka',
            ),
            'description': (
                'Záznam o tom, kto export vyžiadal (GDPR a ISO 27001 compliance). 🔐 '
                'Do poznámky môžete uviesť špecifický dôvod exportu (napr. "Daňová kontrola"). 📝'
            )
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "💾 Systémové exporty a Zálohy"
    isas_preloader_text = "⏳ Vstupujete do zoznamu systémových exportov a záloh..."
    isas_delete_success_message = "✨ Úspešne odstránené z disku: {count} záznamov!"
    isas_clean_fk_fields = {'vyziadal_uzivatel': '👤 Vyberte používateľa'}
    isas_auto_poll = True
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': '💾',
    }

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('download-export/<int:export_id>/', self.admin_site.admin_view(self.download_export_file), name='megazaloha-download'),
        ]
        return custom_urls + urls

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete' and not request.user.is_superuser:
            return JsonResponse({"status": "error", "message": "🚫 Systémové zálohy smie mazať iba Superadmin!"}, status=403)
        return super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(nazov_zalohy__icontains=term) | Q(poznamka__icontains=term)
        ).order_by('-datum_vytvorenia')[:30]

    def format_detective_result(self, obj):
        return {
            'id': obj.id, 
            'name': obj.nazov_zalohy, 
            'search_value': obj.nazov_zalohy,
            'rola': obj.get_typ_zalohy_display(),
            'date': f"🚦 Stav: {obj.get_stav_spracovania_display()}",
            'cat': '💾 ZÁLOHA'
        }

    def download_export_file(self, request, export_id):
        """
        🛡️ Bezpečný endpoint na stiahnutie súboru. 
        Súbor sa neposkytuje cez verejné URL, ale streamuje sa priamo cez Django po overení práv.
        """
        if not request.user.is_staff:
            raise Http404("Nemáte oprávnenie na stiahnutie tohto súboru.")

        try:
            zaloha = self.get_queryset(request).get(pk=export_id)
        except self.model.DoesNotExist:
            raise Http404("Záloha neexistuje.")

        # 🛡️ BEZPEČNOSŤ: K auditným logom má prístup výhradne Superuser alebo Konateľ (KO)
        if zaloha.typ_zalohy == 'audit_logy':
            is_konatel = False
            zamestnanec = getattr(request.user, 'zamestnanec', None)
            if zamestnanec and getattr(zamestnanec, 'typ_pomeru', None) == 'KO':
                is_konatel = True
            elif getattr(request.user, 'typ_pomeru', None) == 'KO':
                is_konatel = True
            else:
                try:
                    from Evidencia.models import Zamestnanec
                    is_konatel = Zamestnanec.objects.filter(
                        Q(pouzivatel=request.user) | Q(user=request.user),
                        typ_pomeru='KO'
                    ).exists()
                except Exception:
                    is_konatel = False

            if not (request.user.is_superuser or is_konatel):
                raise PermissionDenied("🚫 Prístup odopretý: Export bezpečnostných auditných logov smie stiahnuť iba Superadmin alebo Konateľ spoločnosti.")

        if not zaloha.subor_zalohy:
            raise Http404("Záloha neobsahuje žiadny súbor.")

        # 🛡️ Ochrana voči TOCTOU (Time-of-Check to Time-of-Use) Race Condition (univerzálne pre lokálne aj cloudové úložiská)
        try:
            file_handle = zaloha.subor_zalohy.open('rb')
        except (FileNotFoundError, OSError, ValueError):
            raise Http404("Fyzický súbor sa nenašiel na serveri (mohol byť práve zmazaný expiráciou).")

        # Otvoríme súbor a pošleme ho ako FileResponse (bezpečné streamovanie)
        response = FileResponse(file_handle)
        response['Content-Disposition'] = f'attachment; filename="{os.path.basename(zaloha.subor_zalohy.name)}"'
        return response

    @admin.display(description='🗃️ Názov & Čas', ordering='datum_vytvorenia')
    def get_export_card(self, obj):
        return render_isas_avatar(
            icon="💾",
            title=obj.nazov_zalohy,
            subtitle=f"🕒 {obj.datum_vytvorenia.strftime('%d.%m.%Y %H:%M')}",
            bg_gradient="#3b82f6, #1d4ed8",
            shadow_color="rgba(59, 130, 246, 0.25)"
        )

    @admin.display(description='📂 Kategória dát', ordering='typ_zalohy')
    def get_typ_exportu_badge(self, obj):
        typ_text = obj.get_typ_zalohy_display()
        
        bg_color, text_color, border_color = "#f8fafc", "#334155", "#cbd5e1"
        
        if obj.typ_zalohy in ['vystavene_faktury', 'prichodzie_faktury', 'cenove_ponuky', 'evidencia_uhrad']:
            bg_color, text_color, border_color = "#fefce8", "#854d0e", "#fef08a"
        elif obj.typ_zalohy in ['servisne_protokoly', 'ulohy_a_vykazy']:
            bg_color, text_color, border_color = "#e0e7ff", "#3730a3", "#c7d2fe"
        elif obj.typ_zalohy in ['kniha_jazd', 'tankovania']:
            bg_color, text_color, border_color = "#eff6ff", "#1d4ed8", "#bfdbfe"
        elif obj.typ_zalohy in ['cenovy_katalog', 'skladove_pohyby', 'odoslane_objednavky', 'prijate_objednavky']:
            bg_color, text_color, border_color = "#fff7ed", "#c2410c", "#ffedd5"
        elif obj.typ_zalohy in ['hr_dokumenty', 'certifikaty', 'povolenia_vstupov', 'podklady_mzdy']:
            bg_color, text_color, border_color = "#f0fdf4", "#166534", "#bbf7d0"
        elif obj.typ_zalohy in ['kompletny_dump', 'audit_logy']:
            bg_color, text_color, border_color = "#fef2f2", "#b91c1c", "#fecaca"
        elif obj.typ_zalohy in ['technicke_specifikacie', 'qr_kody_zariadeni']:
            bg_color, text_color, border_color = "#faf5ff", "#6b21a8", "#e9d5ff"
        elif obj.typ_zalohy == 'mimosystemova':
            bg_color, text_color, border_color = "#f1f5f9", "#475569", "#cbd5e1"

        return render_isas_badge(
            text=typ_text,
            custom_bg=bg_color,
            custom_text=text_color,
            custom_border=border_color,
            wrapper=True
        )

    @admin.display(description='💾 Veľkosť & Retencia', ordering='velkost_v_mb')
    def get_retencia_a_velkost(self, obj):
        velkost = f"{obj.velkost_v_mb} MB" if obj.velkost_v_mb else "—"
        
        if obj.politika_uchovania == 'navzdy':
            retencia_text = "♾️ Trvalý archív"
            retencia_color = "#059669"
        else:
            retencia_text = f"Do {obj.datum_exspiracie.strftime('%d.%m.%Y')}" if obj.datum_exspiracie else "Neznáme"
            retencia_color = "#ea580c"

        return render_isas_mini_stat_card(
            top_label="Veľkosť súboru",
            top_val=velkost,
            top_val_color="#3b82f6",
            bot_label="Uchovať na disku",
            bot_val=retencia_text,
            bot_val_color=retencia_color,
            css_class="isas-size-cell",
            data_attrs={"id": str(obj.id)}
        )

    @admin.display(description='🚦 Stav', ordering='stav_spracovania')
    def get_stav_spracovania_badge(self, obj):
        data_attrs = {"id": str(obj.id), "status": obj.stav_spracovania}
        css_class = "isas-status-badge"
        
        if obj.stav_spracovania == 'hotova':
            return render_isas_badge(
                text="HOTOVÉ",
                icon="✅",
                custom_bg="#ecfdf5",
                custom_text="#059669",
                custom_border="#6ee7b7",
                css_class=css_class,
                data_attrs=data_attrs,
                custom_style="text-transform: uppercase; letter-spacing: 0.5px;"
            )
        elif obj.stav_spracovania == 'spracovava_sa':
            return render_isas_badge(
                text="GENERUJE SA",
                icon="⚙️",
                custom_bg="rgba(59, 130, 246, 0.1)",
                custom_text="#2563eb",
                custom_border="rgba(59, 130, 246, 0.4)",
                css_class=css_class,
                data_attrs=data_attrs,
                custom_style="animation: pulseGreen 2s infinite; text-transform: uppercase; letter-spacing: 0.5px;"
            )
        elif obj.stav_spracovania == 'chyba':
            return render_isas_badge(
                text="ZLYHALO",
                icon="❌",
                custom_bg="#fef2f2",
                custom_text="#dc2626",
                custom_border="#fca5a5",
                css_class=css_class,
                data_attrs=data_attrs,
                custom_style="text-transform: uppercase; letter-spacing: 0.5px;"
            )
        
        return render_isas_badge(
            text="V RADE",
            icon="⏳",
            custom_bg="#fff7ed",
            custom_text="#ea580c",
            custom_border="#fdba74",
            css_class=css_class,
            data_attrs=data_attrs,
            custom_style="text-transform: uppercase; letter-spacing: 0.5px;"
        )

    @admin.display(description='📥 Akcia')
    def get_stiahnut_link(self, obj):
        data_attrs = {"id": str(obj.id)}
        css_class = "isas-action-cell"
        
        if obj.stav_spracovania == 'hotova' and obj.subor_zalohy:
            download_url = reverse('admin:megazaloha-download', args=[obj.id])
            return render_isas_link_btn(
                url=download_url,
                text="STIAHNUŤ",
                icon="⬇️",
                type_color="success",
                wrapper=True,
                css_class=css_class,
                data_attrs=data_attrs,
                custom_style="background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; border: none; box-shadow: 0 4px 6px rgba(16,185,129,0.3);"
            )
        elif obj.stav_spracovania == 'hotova' and not obj.subor_zalohy:
            text_html = render_isas_text(
                text="ℹ️ Žiadne dáta",
                color="#64748b",
                weight=700,
                css_class=css_class,
                data_attrs=data_attrs
            )
            return render_isas_layout(text_html, layout="row", center=True)
        elif obj.stav_spracovania == 'chyba':
            text_html = render_isas_text(
                text="⚠️ Chyba pri generovaní",
                color="#ef4444",
                weight=800,
                css_class=css_class,
                data_attrs=data_attrs
            )
            return render_isas_layout(text_html, layout="row", center=True)
        
        text_html = render_isas_text(
            text="Súbor sa pripravuje...",
            color="#94a3b8",
            weight=800,
            css_class=css_class,
            data_attrs=data_attrs
        )
        return render_isas_layout(text_html, layout="row", center=True)

@admin.register(CenovaPonuka)
class CenovaPonukaAdmin(ISASTaskConversionAdminMixin, ISASInvoiceGeneratorAdminMixin, ISASSmartMatcherAdminMixin, ISASPremiumAdmin):    
    fieldsets = (
        ('🔗 ZÁKLADNÉ PREPOJENIA A ENTITY', {
            'description': 'Tento blok slúži na definovanie hlavných vzťahov. Vyberte zákazníka a priraďte zariadenie. ⚙️ Umelá inteligencia (AI) podľa týchto údajov natiahne históriu a predpisy. 🧠',
            'fields': (
                'cislo_ponuky',
                ('zakaznik', 'zariadenie'),
                ('zobraz_vystavovatela', 'zobraz_ulohy'),
                ('typ_servisu',)
            )
        }),
        ('⏳ ČASOVÁ PLATNOSŤ A STAV PONUKY', {
            'description': 'Kedy bola ponuka vystavená a dokedy presne platí? Zmeňte stav ponuky podľa aktuálneho vyjadrenia zákazníka. 🚦',
            'fields': (
                ('datum_vystavenia', 'stav'),
            )
        }),
        ('⚖️ OBCHODNÁ LOGIKA A DAŇOVÉ NASTAVENIA', {
            'description': 'Nastavte prenos daňovej povinnosti (Reverse Charge) alebo plošnú globálnu zľavu pre celú ponuku. 💰 Systém to automaticky a presne prepočíta! 🧮',
            'fields': (
                ('prenos_danovej_povinnosti', 'globalna_zlava_percento', 'mena'),
            )
        }),
        ('💰 FINANČNÁ REKAPITULÁCIA (VYPOČÍTANÉ SYSTÉMOM)', {
            'description': 'Tieto dôležité polia sú prísne uzamknuté a chránené algoritmom. 🔒 Počítajú sa úplne automaticky na pozadí z priradených položiek ponuky. 📊',
            'fields': (
                ('celkom_bez_dph_pred_zlavou', 'hodnota_zlavy'), 
                ('celkom_bez_dph', 'celkom_dph', 'celkom_s_dph'),
            ),
        }),
        ('🤖 AI GENEROVANÝ OBSAH A POZNÁMKY', {
            'description': 'Nechajte našu umelú inteligenciu vygenerovať profesionálny úvodný text priamo na PDF dokument! 📄 Pridajte aj záverečné zmluvné podmienky a tajné interné poznámky. 🤫',
            'fields': (
                ('ai_uvodny_text',),
                ('zavrecny_text',),
                ('interne_poznamky',)
            )
        }),
        ('📄 VÝSTUPNÉ DOKUMENTY', {
            'description': 'Tu nájdete finálne vygenerované PDF pripravené na priame odoslanie klientovi na stôl. 📤',
            'fields': (
                ('pdf_dokument',),
            )
        }),
        ('🌐 KLIENTSKÝ PORTÁL A ONLINE SCHVAĽOVANIE', {
            'description': 'Precízna auditná stopa klienta. 🕵️‍♂️ Systém presne eviduje, kedy ponuku otvoril, z akej IP adresy ju pozeral a kto ju digitálne podpísal. ✍️',
            'fields': (
                'bezpecnostny_kluc', 
                'cas_schvalenia', 
                'ip_adresa_schvalitela', 
                'meno_schvalitela',
            ),
        }),
    )

    # 🔒 VŠETKY POLIA SÚ STRIKTNE READONLY (Ochrana integrity dokumentu)
    readonly_fields = (
        'cislo_ponuky', 'zakaznik', 'zariadenie', 'typ_servisu', 'zobraz_vystavovatela', 'zobraz_ulohy',
        'datum_vystavenia', 'stav', 'mena',
        'prenos_danovej_povinnosti', 'globalna_zlava_percento',
        'ai_uvodny_text', 'zavrecny_text', 'interne_poznamky',
        'celkom_bez_dph_pred_zlavou', 'hodnota_zlavy', 'celkom_bez_dph', 'celkom_dph', 'celkom_s_dph',
        'pdf_dokument',
        'bezpecnostny_kluc', 'cas_schvalenia', 'ip_adresa_schvalitela', 'meno_schvalitela'
    )

    # --- 🤖 AUTOMATIZÁCIA PRI UKLADANÍ ---
    def save_model(self, request, obj, form, change):
        if not getattr(obj, 'vystavil', None):
            obj.vystavil = request.user
            
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('zakaznik', 'zariadenie', 'vystavil').prefetch_related('objednavky', 'vytvorene_ulohy', 'polozky__zariadenie', 'typ_servisu')

    list_display = ('get_ponuka_card', 'get_zakaznik_badge', 'zobraz_typ_servisu', 'get_sumy', 'get_sparovanie_status', 'zobraz_vystavovatela', 'get_stav_badge', 'get_akcie', 'zobraz_ulohy')
    list_display_links = ('get_ponuka_card',)
    list_filter = (ISASRelatedUserFilter, 'stav', ISASRelatedZakaznikFilter, ISASDateRangeFilter, 'prenos_danovej_povinnosti')
    isas_user_filter_field = 'vystavil'
    isas_date_filter_field = 'datum_vystavenia'
    search_fields = ('cislo_ponuky', 'zakaznik__Nazov_spolocnosti', 'zariadenie__model_zariadenia')

    def add_view(self, request, form_url='', extra_context=None):
        url = reverse('evidencia:cenova_ponuka_builder')
        return HttpResponseRedirect(url)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj:  # 🔒 Ak objekt už existuje v databáze, vrátime všetky naše prísne zadefinované Read-Only polia!
            return list(set(readonly + list(self.readonly_fields)))
        return readonly

    def search_orders_for_pairing(self, request):
        """🔍 API pre Select2 modál: Vyhľadávanie prijatých objednávok pre spárovanie s ponukou."""
        if not request.user.has_perm('Evidencia.view_prijataobjednavka'):
            return JsonResponse({'results': []}, status=403)
            
        term = request.GET.get('q', '').strip()
        
        qs = PrijataObjednavka.objects.select_related('zakaznik').all()
        if term:
            qs = qs.filter(
                Q(cislo_objednavky_interne__icontains=term) | 
                Q(cislo_objednavky_klienta__icontains=term) |
                Q(zakaznik__Nazov_spolocnosti__icontains=term)
            )
            
        qs = qs.order_by('-datum_prijatia')[:30]
        
        results = []
        for o in qs:
            zakaznik = escape(o.zakaznik.Nazov_spolocnosti) if o.zakaznik else "Neznámy zákazník"
            po_number = f" (PO: {escape(o.cislo_objednavky_klienta)})" if o.cislo_objednavky_klienta else ""
            stav = o.get_stav_display()
            text = f"📥 {o.cislo_objednavky_interne}{po_number} | 🏢 {zakaznik} | {stav}"
            results.append({'id': o.id, 'text': text})
            
        return JsonResponse({'results': results})

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "💰 Cenové Ponuky"
    isas_preloader_text = "⏳ Vstupujete do zoznamu cenových ponúk..."
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "💰",
        'custom_buttons_html': '<button type="button" id="btn-mark-sent" data-action="mark_sent" class="isas-action-btn btn-blue">📤 ODOSLAŤ</button><button type="button" id="btn-pair-order" class="isas-action-btn btn-brown">🔗 Spárovať s objednávkou</button><button type="button" id="btn-convert-task" class="isas-action-btn btn-green">⚙️ KONVERTOVAŤ NA ÚLOHU</button><button type="button" id="btn-merge-invoice" class="isas-action-btn btn-blue">🧾 ZBERNÁ FAKTÚRA</button>',
        'ignore_default_action_ids': ["btn-mark-sent", "btn-pair-order", "btn-convert-task", "btn-merge-invoice"]
    }

    isas_auto_poll = True

    isas_action_modals = {
        'btn-mark-sent': {
            'type': 'direct_action',
            'action': 'mark_sent',
            'confirm': True,
            'confirm_title': 'Odoslanie cenových ponúk 📤',
            'confirm_text': 'Naozaj chcete vybrané cenové ponuky odoslať zákazníkom na e-mail? Zákazníkom bude doručený autorizovaný odkaz s PDF prílohou.',
            'loading_text': '⏳ ODOSIELAM...',
            'success_text': '✅ ODOSLANÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jednu cenovú ponuku.',
            'reload': True
        },
        'btn-pair-order': {
            'type': 'ajax_select_modal',
            'title': 'Spárovať s objednávkou',
            'subtitle': 'Vyhľadajte a vyberte prijaté objednávky na spárovanie.',
            'icon': '🔗',
            'action': 'pair_order',
            'ajax_url': '/admin/Evidencia/cenovaponuka/search-orders-for-pairing/',
            'placeholder': 'Hľadať podľa čísla alebo zákazníka...',
            'confirm_text': 'Spárovať',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jednu ponuku zo zoznamu.'
        },
        'btn-convert-task': {
            'type': 'convert_to_task',
            'action': 'convert_task',
            'require_selection': True,
            'min_selection': 1,
            'max_selection': 1,
            'error_msg': 'Na konverziu označte vždy presne jednu položku zo zoznamu.',
            'service_types_help_text': '💡 Tieto typy prác boli automaticky predvyplnené z podkladov. Môžete ich ľubovoľne upraviť, zmazať alebo doplniť pred vygenerovaním úlohy.'
        },
        'btn-merge-invoice': {
            'type': 'direct_action',
            'action': 'merge_invoice',
            'confirm': True,
            'confirm_title': 'Vytvorenie zbernej faktúry 🧾',
            'confirm_text': 'Naozaj chcete z vybraných schválených cenových ponúk vygenerovať jednu spoločnú zbernú faktúru?',
            'loading_text': '⏳ Generujem...',
            'success_text': '✅ HOTOVO',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jednu ponuku zo zoznamu.',
            'reload': True
        }
    }

    def get_smart_suggestions(self, request, obj=None):
        smart_suggestions = []
        navrhy_objednavok = PrijataObjednavka.objects.filter(
            navrhnute_ponuky__isnull=False
        ).prefetch_related('navrhnute_ponuky').distinct()
        
        for o in navrhy_objednavok:
            for ponuka in o.navrhnute_ponuky.all():
                mena_o = o.mena or get_firemna_mena()
                mena_p = ponuka.mena or get_firemna_mena()
                suma_obj = f"{o.celkova_suma_bez_dph} {mena_o}" if o.celkova_suma_bez_dph is not None else f"? {mena_o}"
                suma_pon = f"{ponuka.celkom_s_dph} {mena_p}" if ponuka.celkom_s_dph is not None else f"? {mena_p}"
                
                smart_suggestions.append({
                    'left_id': o.id,
                    'left_label': '📥 Objednávka',
                    'left_str': f"{o.cislo_objednavky_interne} | Suma: {suma_obj}",
                    'right_id': ponuka.id,
                    'right_label': '📄 Ponuka',
                    'right_str': f"{ponuka.cislo_ponuky} | Suma: {suma_pon}",
                    'dovod': "🤖 AI Agent vyhodnotil túto objednávku ako najlepšiu zhodu."
                })
        return smart_suggestions

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('search-orders-for-pairing/', self.admin_site.admin_view(self.search_orders_for_pairing), name='search-orders-for-pairing'),
        ]
        return custom_urls + urls

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'zakaznik', 'zariadenie'
        ).prefetch_related(
            'polozky__zariadenie'
        ).filter(
            Q(cislo_ponuky__icontains=term) |
            Q(zakaznik__Nazov_spolocnosti__icontains=term) |
            Q(zariadenie__model_zariadenia__icontains=term)
        ).order_by('-datum_vystavenia')[:30]

    def format_detective_result(self, obj):
        zariadenia_nazvy = set()
        if obj.zariadenie:
            model_val = getattr(obj.zariadenie, 'model_zariadenia', None)
            nazov = str(model_val).strip() if model_val else str(obj.zariadenie)
            zariadenia_nazvy.add(nazov)
        for polozka in obj.polozky.all():
            if polozka.zariadenie:
                model_val = getattr(polozka.zariadenie, 'model_zariadenia', None)
                nazov = str(model_val).strip() if model_val else str(polozka.zariadenie)
                zariadenia_nazvy.add(nazov)
        
        zariadenie_text = f" ({', '.join(zariadenia_nazvy)})" if zariadenia_nazvy else ""
        zakaznik_nazov = obj.zakaznik.Nazov_spolocnosti if obj.zakaznik else "Neznámy zákazník"
        customer_str = f"{zakaznik_nazov}{zariadenie_text}"
        cislo = obj.cislo_ponuky if obj.cislo_ponuky else f"DRAFT-#{obj.id}"
        
        return {
            'id': obj.id, 
            'name': cislo, 
            'search_value': cislo,
            'rola': f"🏢 {customer_str}",
            'date': obj.datum_vystavenia.strftime('%d.%m.%Y') if obj.datum_vystavenia else 'Nešpecifikované',
            'cat': '💰 PONUKA'
        }

    def get_delete_success_message(self, count):
        word = "ponuku" if count == 1 else ("ponuky" if 2 <= count <= 4 else "ponúk")
        return f"✨ Úspešne a bezpečne odstránilo {count} {word} zo zoznamu!"

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            data = {}
        
        # --- 📤 AKCIA: ODOSLAŤ PONUKU ZÁKAZNÍKOVI ---
        if action_type == 'mark_sent':
            if not request.user.has_perm('Evidencia.change_cenovaponuka'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať a odosielať cenové ponuky."}, status=403)

            from .tasks import generuj_ponuku_pdf_task

            count = 0
            base_url = request.build_absolute_uri('/')[:-1]

            with transaction.atomic():
                ponuky_na_odoslanie = self.model.objects.select_for_update().filter(
                    id__in=valid_ids, stav='DRAFT'
                )
                if not ponuky_na_odoslanie.exists():
                    return JsonResponse({"status": "error", "message": "Zvolené ponuky už boli odoslané alebo nie sú v stave DRAFT. 🛑"})

                ponuky_na_update = []
                for p in ponuky_na_odoslanie:
                    p.stav = 'SENT'
                    ponuky_na_update.append(p)

                    # 🛡️ ISAS FIX: Rešpektovanie konfigurácie notifikácií zo šablóny
                    n_tg = True
                    n_mail = True
                    n_discord = True

                    if p.interne_poznamky and p.interne_poznamky.startswith("[ISAS_METADATA_JSON]"):
                        try:
                            meta_raw = p.interne_poznamky.replace("[ISAS_METADATA_JSON]", "", 1)
                            meta_data = json.loads(meta_raw)
                            if isinstance(meta_data, dict):
                                n_tg = bool(meta_data.get('notif_tg', True))
                                n_mail = bool(meta_data.get('notif_mail', True))
                                n_discord = bool(meta_data.get('notif_discord', True))
                        except Exception:
                            pass

                    def _dispatch_ponuka_pdf(p_id=p.id, url=base_url, tg=n_tg, mail=n_mail, disc=n_discord):
                        try:
                            generuj_ponuku_pdf_task.delay(
                                p_id, url, notif_tg=tg, notif_mail=mail, notif_discord=disc, send_notif=True
                            )
                        except Exception as exc:
                            logger.warning(f"Zlyhalo generovanie PDF ponuky #{p_id} do Celery: {exc}")
                            messages.warning(request, f"⚠️ Generovanie PDF ponuky #{p_id} zlyhalo pri zaradení do fronty (výpadok Celery/Redis).")
                    transaction.on_commit(_dispatch_ponuka_pdf)
                    count += 1

                if ponuky_na_update:
                    self.model.objects.bulk_update(ponuky_na_update, ['stav'])

            return JsonResponse({"status": "success", "message": f"✅🚀 {count} ponúk sa generuje a odosiela zákazníkovi na pozadí."})

        # --- 🔗 AKCIA: SPÁROVAŤ S OBJEDNÁVKOU ---
        elif action_type == 'pair_order':
            if not request.user.has_perm('Evidencia.change_cenovaponuka'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať cenové ponuky."}, status=403)

            order_ids = data.get('quote_ids', []) or data.get('order_ids', [])
            if not order_ids:
                return JsonResponse({"status": "error", "message": "🚫 Nebola vybraná žiadna objednávka na spárovanie."})
            
            with transaction.atomic():
                ponuky = list(self.model.objects.filter(id__in=valid_ids))
                objednavky = PrijataObjednavka.objects.select_for_update().filter(id__in=order_ids).order_by('id')
                for obj in objednavky:
                    obj.zdrojove_ponuky.add(*ponuky)
                    obj.navrhnute_ponuky.remove(*ponuky)
                    obj.ignorovane_ponuky.remove(*ponuky)
            return JsonResponse({"status": "success", "message": "🔗 Ponuka bola úspešne spárovaná s objednávkami!"})

        # --- 🚫 AKCIA: IGNOROVAŤ NÁVRH ---
        elif action_type == 'ignore_order':
            if not request.user.has_perm('Evidencia.change_cenovaponuka'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať cenové ponuky."}, status=403)

            order_ids = data.get('order_ids', [])
            if valid_ids and order_ids:

                with transaction.atomic():
                    try:
                        ponuka = self.model.objects.select_for_update().get(id=valid_ids[0])
                    except self.model.DoesNotExist:
                        return JsonResponse({"status": "error", "message": "Cenová ponuka nebola nájdená."}, status=404)
                    objednavky = PrijataObjednavka.objects.select_for_update().filter(id__in=order_ids).order_by('id')
                    for obj in objednavky:
                        obj.ignorovane_ponuky.add(ponuka)
                        obj.navrhnute_ponuky.remove(ponuka)
                return JsonResponse({"status": "success", "message": "🚫 Návrh bol trvalo ignorovaný."})

        elif action_type == 'merge_invoice':
            return self.generate_merge_invoice(request, self.model.objects.filter(id__in=valid_ids), 'ponuka')
            
        elif action_type == 'smart_pair':
            if not request.user.has_perm('Evidencia.change_prijataobjednavka'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať prijaté objednávky."}, status=403)
            try:
                data = json.loads(request.body)
                left_id = data.get('left_id')
                right_id = data.get('right_id')
            except Exception:
                return JsonResponse({"status": "error", "message": "Neplatné dáta."}, status=400)

            if not left_id or not right_id:
                return JsonResponse({"status": "error", "message": "Chýbajúce identifikátory pre párovanie."}, status=400)

            with transaction.atomic():
                try:
                    objednavka = PrijataObjednavka.objects.select_for_update().get(id=left_id)
                    objednavka.zdrojove_ponuky.add(right_id)
                    objednavka.navrhnute_ponuky.remove(right_id)
                    objednavka.ignorovane_ponuky.remove(right_id)
                except PrijataObjednavka.DoesNotExist:
                    pass
            return JsonResponse({"status": "success", "message": "🔗 Úspešne spárované!"})

        elif action_type == 'smart_ignore':
            if not request.user.has_perm('Evidencia.change_prijataobjednavka'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať prijaté objednávky."}, status=403)
            try:
                data = json.loads(request.body)
                left_id = data.get('left_id')
                right_id = data.get('right_id')
            except Exception:
                return JsonResponse({"status": "error", "message": "Neplatné dáta."}, status=400)

            if not left_id or not right_id:
                return JsonResponse({"status": "error", "message": "Chýbajúce identifikátory pre akciu."}, status=400)

            with transaction.atomic():
                try:
                    objednavka = PrijataObjednavka.objects.select_for_update().get(id=left_id)
                    objednavka.ignorovane_ponuky.add(right_id)
                    objednavka.navrhnute_ponuky.remove(right_id)
                except PrijataObjednavka.DoesNotExist:
                    pass
            return JsonResponse({"status": "success", "message": "🚫 Návrh bol ignorovaný."})

        elif action_type == 'convert_task':
            if len(valid_ids) != 1:
                return JsonResponse({"status": "error", "message": "Vyberte presne jednu ponuku."})
            try:
                from .utils import ISASTaskConversionEngine
            except ImportError:
                from Evidencia.utils import ISASTaskConversionEngine
            return ISASTaskConversionEngine.convert_to_task(request, 'ponuka', valid_ids[0], data)

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description='💰 Číslo & Dátum', ordering='cislo_ponuky')
    def get_ponuka_card(self, obj):
        image_url = None
        
        if obj.zakaznik and getattr(obj.zakaznik, 'domena', None):
            clean_domain = str(obj.zakaznik.domena).lower().replace('http://', '').replace('https://', '').replace('www.', '').split('/')[0].strip()
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
            if '.' in clean_domain:
                image_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"

        typy_data = [{'id': t.id, 'text': f"🛠️ {t.nazov}"} for t in obj.typ_servisu.all()]
        hidden_data = render_isas_layout(
            content_html="",
            css_class="isas-row-service-types",
            data_attrs={'id': str(obj.id), 'typy': json.dumps(typy_data)},
            custom_style="display:none;"
        )

        return render_isas_avatar(
            icon="💰",
            title=obj.cislo_ponuky,
            subtitle=f"📅 {obj.datum_vystavenia.strftime('%d.%m.%Y')}",
            bg_gradient="#f59e0b, #b45309",
            shadow_color="rgba(245, 158, 11, 0.25)",
            extra_html=hidden_data,
            image_url=image_url
        )

    @admin.display(description='🏢 Zákazník & Zariadenia', ordering='zakaznik')
    def get_zakaznik_badge(self, obj):
        bezpecny_nazov_firmy = obj.zakaznik.Nazov_spolocnosti if obj.zakaznik else "Neznámy"
        
        zariadenia_nazvy = set()
        def pridaj_stroj(zar):
            if zar:
                nazov = getattr(zar, 'model_zariadenia', '') or getattr(zar, 'nazov', '') or str(zar)
                if nazov and str(nazov).strip():
                    zariadenia_nazvy.add(str(nazov).strip())

        pridaj_stroj(obj.zariadenie)
        for polozka in obj.polozky.all():
            pridaj_stroj(polozka.zariadenie)
                
        stroj_html = ""
        if zariadenia_nazvy:
            badges = [render_isas_badge(text=nazov, icon="⚙️", custom_bg="#e0e7ff", custom_text="#4338ca", custom_border="#c7d2fe", wrapper=False) for nazov in zariadenia_nazvy]
            stroj_html = render_isas_layout(mark_safe("".join(badges)), layout="row", center=True, gap=4, custom_style="flex-wrap: wrap; margin-top: 6px;")
        
        title_html = render_isas_text(f"🏢 {bezpecny_nazov_firmy}", size=13, color="#0f172a", weight=800)
        return render_isas_layout(format_html('{}{}', title_html, stroj_html), layout="col", center=True)

    @admin.display(description="👤 Vystavil")
    def zobraz_vystavovatela(self, obj):
        
        if not obj.vystavil:
            return render_isas_text("➖ Neznámy", muted=True)
            
        bezpecne_meno = obj.vystavil.get_full_name() or obj.vystavil.username
        
        try:
            app_label = obj.vystavil._meta.app_label
            model_name = obj.vystavil._meta.model_name
            url = reverse(f'admin:{app_label}_{model_name}_change', args=[obj.vystavil.id])
            return render_isas_link_btn(url=url, text=bezpecne_meno, icon="👤", type_color="purple", wrapper=True)
        except NoReverseMatch:
            return render_isas_badge(text=bezpecne_meno, icon="👤", type_color="purple", wrapper=True)

    @admin.display(description='💰 Hodnota ponuky', ordering='celkom_s_dph')
    def get_sumy(self, obj):
        mena_cp = obj.mena or get_firemna_mena()
        dph_label = "Režim DPH" if obj.prenos_danovej_povinnosti else "Hodnota DPH"
        dph_val = "Prenos daň. povinnosti 🔄" if obj.prenos_danovej_povinnosti else f"+ {obj.celkom_dph or Decimal('0.00')} {mena_cp}"
        dph_color = "#ea580c" if obj.prenos_danovej_povinnosti else "#64748b"
        
        return render_isas_mini_stat_card(
            top_label="Cena bez DPH",
            top_val=f"{obj.celkom_bez_dph or Decimal('0.00')} {mena_cp}",
            bot_label=dph_label,
            bot_val=dph_val,
            bot_val_color=dph_color
        )

    @admin.display(description='🔗 Prijaté Objednávky')
    def get_sparovanie_status(self, obj):
        objednavky = obj.objednavky.all()
        
        if not objednavky:
            badge = render_isas_badge("❌ Nespárované", type_color="neutral", wrapper=False, custom_style="border-style: dashed;")
            return render_isas_layout(badge, layout="col", center=True, min_width="110px")
            
        html_items = []
        for o in objednavky:
            url = reverse('admin:Evidencia_prijataobjednavka_change', args=[o.id])
            po_text = f" (PO: {o.cislo_objednavky_klienta})" if o.cislo_objednavky_klienta else ""
            text = f"📥 {o.cislo_objednavky_interne}{po_text}"
            html_items.append(render_isas_link_btn(url=url, text=text, type_color="success", wrapper=False))
            
        items_str = mark_safe("".join(html_items))
        
        title = render_isas_text("🔗 Spárované s:", size=9, color="#166534", weight=900, custom_style="text-transform: uppercase; letter-spacing: 0.5px;")
        content = render_isas_layout(items_str, layout="col", gap=4)
        
        return render_isas_layout(format_html('{}{}', title, content), layout="col", center=True, bg_color="#f0fdf4", border_color="#bbf7d0", padding="6px", border_radius="8px", min_width="110px", gap=4)

    @admin.display(description='🚦 Stav ponuky', ordering='stav')
    def get_stav_badge(self, obj):
        stavy = {
            'DRAFT': ('#f1f5f9', '#475569', '📝 ROZPRACOVANÁ', '#cbd5e1', ''),
            'SENT': ('#e0f2fe', '#0284c7', '📤 ODOSLANÁ', '#bae6fd', ''),
            'ACCEPTED': ('#ecfdf5', '#059669', '✅ SCHVÁLENÁ', '#6ee7b7', 'box-shadow: 0 1px 2px rgba(0,0,0,0.03);'),
            'REJECTED': ('#fef2f2', '#e11d48', '❌ ZAMIETNUTÁ', '#fecdd3', ''),
            'CONVERTED': ('rgba(126, 34, 206, 0.1)', '#7e22ce', '⚙️ V REALIZÁCII', 'rgba(126, 34, 206, 0.3)', 'animation: pulseGreen 2s infinite;'),
        }
        bg, color, text, border, extra_css = stavy.get(obj.stav, stavy['DRAFT'])
        return render_isas_badge(text=text, custom_bg=bg, custom_text=color, custom_border=border, custom_style=extra_css, wrapper=True)

    @admin.display(description='📄 PDF')
    def get_akcie(self, obj):       
        status_code = "DONE" if obj.pdf_dokument else "PROCESSING"
        
        buttons = []
        if obj.pdf_dokument:
            buttons.append(render_isas_link_btn(url=obj.pdf_dokument.url, text="OTVORIŤ PDF", icon="📄", type_color="neutral", wrapper=False))
        else:
            poller_badge = render_isas_badge("⏳ GENERUJE SA PDF...", custom_bg="#fffbeb", custom_text="#d97706", custom_border="#f59e0b", custom_style="border-style: dashed; box-shadow: 0 0 10px rgba(245,158,11,0.2); animation: pulse 2s infinite;", wrapper=False)
            buttons.append(poller_badge)

        if obj.stav == 'DRAFT':
            edit_url = reverse('evidencia:cenova_ponuka_builder') + f"?edit_id={obj.id}"
            buttons.append(render_isas_link_btn(url=edit_url, text="UPRAVIŤ KONCEPT", icon="✏️", type_color="purple", wrapper=False))

        buttons_inner = render_isas_layout(mark_safe("".join(buttons)), layout="row", gap=6, center=True, custom_style="flex-wrap: wrap;")
        return render_isas_layout(
            buttons_inner,
            layout="row",
            center=True,
            html_id=f"isas-pdf-badge-{obj.id}",
            css_class="isas-erp-status-badge",
            data_attrs={"status": status_code},
            custom_style="display: inline-block;"
        )

    @admin.display(description="🛠️ Typ servisu/práce")
    def zobraz_typ_servisu(self, obj):
        typy = obj.typ_servisu.all()
        
        if not typy:
            return render_isas_text("➖ Bez typu", muted=True)
            
        badges = [render_isas_badge(text=str(t), icon="🛠️", type_color="info", wrapper=False) for t in typy]
        return render_isas_layout(mark_safe("".join(badges)), layout="row", gap=4, custom_style="flex-wrap: wrap;")

    @admin.display(description="📋 Vygenerované Úlohy")
    def zobraz_ulohy(self, obj):
        ulohy = obj.vytvorene_ulohy.all()
        
        if not ulohy.exists():
            return render_isas_text("➖ Zatiaľ bez úlohy", muted=True)
        
        html_badges = []
        for uloha in ulohy:
            url = reverse(f'admin:{uloha._meta.app_label}_{uloha._meta.model_name}_change', args=[uloha.id])
            html_badges.append(render_isas_link_btn(url=url, text=str(uloha), icon="📋", type_color="warning", wrapper=False, custom_style="margin-bottom: 4px; margin-right: 4px;"))
            
        return render_isas_layout(mark_safe("".join(html_badges)), layout="row", custom_style="flex-wrap: wrap; max-width: 200px;")

    class Media:
        css = {
            'all': (
                'admin/css/vendor/select2/select2.css',
                'admin/css/autocomplete.css',
            )
        }
        js = (
            'admin/js/vendor/jquery/jquery.js',
            'admin/js/vendor/select2/select2.full.js',
            'admin/js/jquery.init.js',
        )

class ObjednavkaVelkostFilter(admin.SimpleListFilter):
    title = '🛒 Veľkosť objednávky'
    parameter_name = 'velkost_objednavky'

    def lookups(self, request, model_admin):
        return (
            ('mala', '📦 Malá (1-5 položiek)'),
            ('stredna', '🗃️ Stredná (6-20 položiek)'),
            ('velka', '🚛 Veľká (21+ položiek)'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'mala':
            return queryset.filter(celkovy_pocet_poloziek__gte=1, celkovy_pocet_poloziek__lte=5)
        if self.value() == 'stredna':
            return queryset.filter(celkovy_pocet_poloziek__gte=6, celkovy_pocet_poloziek__lte=20)
        if self.value() == 'velka':
            return queryset.filter(celkovy_pocet_poloziek__gte=21)
        return queryset

@admin.register(OdoslanaObjednavka)
class OdoslanaObjednavkaAdmin(ISASPremiumAdmin):        
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # 🚀 ISAS ENGINE: Eliminácia N+1 dotazov pre dodávateľov a M2M párovanie
        return qs.select_related('dodavatel').prefetch_related('prijate_faktury')

    list_display = (
        'get_objednavka_card', 
        'get_dodavatel_card', 
        'get_sparovanie_status',
        'stiahnut_pdf_card'
    )    
    list_display_links = ('get_objednavka_card',)
    list_filter = (ISASDateRangeFilter, ObjednavkaVelkostFilter, ('dodavatel', admin.RelatedOnlyFieldListFilter))
    isas_date_filter_field = 'vytvorene'
    search_fields = ('cislo_objednavky', 'dodavatel__Nazov_spolocnosti')
  
    readonly_fields = ('dodavatel', 'cislo_objednavky', 'poradove_cislo_dodavatela', 'pdf_dokument', 'vytvorene', 'celkovy_pocet_poloziek')
    fieldsets = (
        ('📦 Základné informácie', {
            'fields': (
                ('cislo_objednavky', 'vytvorene'),
                ('dodavatel', 'poradove_cislo_dodavatela'),
            ),
            'description': 'Presná identifikácia odoslanej objednávky dodávateľovi a jej poradie v systéme. 🏷️'
        }),
        ('📄 Rozsah a Dokumentácia', {
            'fields': (
                'celkovy_pocet_poloziek', 
                'pdf_dokument'
            ),
            'description': 'Fyzická kópia dokumentu a celkový prehľad náročnosti objednávky. 🛒'
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "📤 História odoslaných objednávok"
    isas_preloader_text = "⏳ Vstupujete do zoznamu odoslaných objednávok..."
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "📦",
    }

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('dodavatel').filter(
            Q(cislo_objednavky__icontains=term) |
            Q(dodavatel__Nazov_spolocnosti__icontains=term)
        ).order_by('-vytvorene')[:30]

    def format_detective_result(self, obj):
        dodavatel_nazov = getattr(obj.dodavatel, 'Nazov_spolocnosti', str(obj.dodavatel))
        datum = obj.vytvorene.strftime('%d.%m.%Y') if obj.vytvorene else ""
        return {
            'id': obj.id, 
            'name': obj.cislo_objednavky, 
            'search_value': obj.cislo_objednavky,
            'rola': f"🏢 {dodavatel_nazov}",
            'date': datum,
            'cat': '📦 OBJEDNÁVKA'
        }

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            if not request.user.has_perm('Evidencia.delete_odoslanaobjednavka'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie na vymazanie objednávok."}, status=403)
            try:
                with transaction.atomic():
                    objednavky_na_zmazanie = list(self.model.objects.select_for_update().filter(id__in=valid_ids))
                    for obj in objednavky_na_zmazanie:
                        if obj.pdf_dokument:
                            obj.pdf_dokument.delete(save=False)
                        obj.delete()
                return JsonResponse({"status": "success", "message": "✨ Objednávky úspešne odstránené!"})
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.error(f"Kritická chyba vo fast_action_api: {str(e)}", exc_info=True)
                return JsonResponse({"status": "error", "message": "🚨 Nastala interná chyba servera. Kontaktujte administrátora."}, status=400)
                
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description='📦 Objednávka & Čas', ordering='vytvorene')
    def get_objednavka_card(self, obj):
        datum = obj.vytvorene.strftime('%d.%m.%Y %H:%M') if obj.vytvorene else "Neznámy ❓"
        polozky = obj.celkovy_pocet_poloziek if obj.celkovy_pocet_poloziek else 0
        
        image_url = None
        dodavatel = getattr(obj, 'dodavatel', None)
        if dodavatel and getattr(dodavatel, 'domena', None):
            clean_domain = str(dodavatel.domena).lower().strip()
            for prefix in ['https://', 'http://', 'www.']:
                if clean_domain.startswith(prefix):
                    clean_domain = clean_domain[len(prefix):]
            
            clean_domain = clean_domain.split('/')[0].split('?')[0].split('#')[0]
            
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
                
            if '.' in clean_domain and len(clean_domain) > 3:
                image_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"
        
        extra_badge = render_isas_badge(
            text=f"Položiek: {polozky}",
            icon="🛒",
            type_color="warning"
        )
        
        return render_isas_avatar(
            icon="📦",
            title=obj.cislo_objednavky,
            subtitle=f"🕒 {datum}",
            bg_gradient="#f59e0b, #d97706",
            shadow_color="rgba(217, 119, 6, 0.25)",
            extra_html=extra_badge,
            image_url=image_url
        )

    @admin.display(description='🏢 Dodávateľ (Cieľ)', ordering='dodavatel__Nazov_spolocnosti')
    def get_dodavatel_card(self, obj):
        dodavatel_nazov = getattr(obj.dodavatel, 'Nazov_spolocnosti', str(obj.dodavatel))
        
        hidden_data = render_isas_hidden_data("objednavka-id-hidden", obj.id)
        
        nazov_text = render_isas_text(text=dodavatel_nazov, icon="🏢", color="#0369a1", size=12, weight=800)
        typ_text = render_isas_text(text="B2B Partner", color="#475569", size=11, weight=700)
        poradie_badge = render_isas_badge(text=f"Objednávka č. {obj.poradove_cislo_dodavatela}", icon="🔢", type_color="info")
        
        content = format_html('{}{}{}', nazov_text, typ_text, poradie_badge)
        layout = render_isas_layout(content, layout="col", center=True, gap=4)
        
        return format_html('{}{}', hidden_data, layout)

    @admin.display(description='🔗 Párovanie s Faktúrou')
    def get_sparovanie_status(self, obj):
        faktury = obj.prijate_faktury.all()
        
        if not faktury:
            return render_isas_badge(
                text="Nespárované", 
                icon="❌", 
                type_color="neutral",
                wrapper=True
            )
            
        html_items = []
        for f in faktury:
            url = reverse('admin:Evidencia_prichodziafaktura_change', args=[f.id])
            vs = escape(f.variabilny_symbol) if f.variabilny_symbol else f"ID:{f.id}"
            mena_f = getattr(f, 'mena', '') or get_firemna_mena()
            suma = f"{escape(str(f.suma_na_uhradu))} {mena_f}" if f.suma_na_uhradu is not None else f"? {mena_f}"
            text = f"VS: {vs} | {suma}"
            
            btn = render_isas_link_btn(
                url=url,
                text=text,
                icon="🧾",
                type_color="success",
                wrapper=False
            )
            html_items.append(btn)
            
        items_str = mark_safe("".join(html_items))
        
        header = render_isas_text(
            text="Spárované s:", 
            icon="🔗", 
            color="#166534", 
            size=9, 
            weight=900,
            custom_style="text-transform: uppercase; letter-spacing: 0.5px;"
        )
        
        content = format_html('{}{}', header, render_isas_layout(items_str, layout="col", gap=4, center=True))
        
        return render_isas_layout(
            content, 
            layout="col", 
            center=True, 
            gap=4, 
            bg_color="#f0fdf4", 
            border_color="#bbf7d0", 
            padding="6px", 
            border_radius="8px",
            min_width="110px"
        )

    @admin.display(description='📄 PDF Dokument')
    def stiahnut_pdf_card(self, obj):
        if obj.pdf_dokument:
            return render_isas_link_btn(
                url=obj.pdf_dokument.url,
                text="Otvoriť PDF",
                icon="📄",
                type_color="danger",
                wrapper=True
            )
        
        return render_isas_link_btn(
            url=None,
            text="Chýba PDF",
            icon="❌",
            type_color="dashed",
            is_button=True,
            wrapper=True
        )

class DatumVstupuFilter(admin.SimpleListFilter):
    title = '📅 Plánovaný termín vstupu'
    parameter_name = 'termin_vstupu'

    def lookups(self, request, model_admin):
        return (
            ('dnes', '🌅 Dnes (Aktuálne)'),
            ('zajtra', '🔜 Zajtra'),
            ('tyzden', '📆 Tento týždeň'),
            ('exspirovane', '⏳ V minulosti (Expirované)'),
        )

    def queryset(self, request, queryset):
        dnes = timezone.now().date()
        
        if self.value() == 'dnes':
            return queryset.filter(datum_vstupu=dnes)
        if self.value() == 'zajtra':
            zajtra = dnes + timedelta(days=1)
            return queryset.filter(datum_vstupu=zajtra)
        if self.value() == 'tyzden':
            koniec_tyzdna = dnes + timedelta(days=7)
            return queryset.filter(datum_vstupu__gte=dnes, datum_vstupu__lte=koniec_tyzdna)
        if self.value() == 'exspirovane':
            return queryset.filter(datum_vstupu__lt=dnes)
            
        return queryset

@admin.register(PovolenieNaVstup)
class PovolenieNaVstupAdmin(ISASPremiumAdmin):
    form = PovolenieNaVstupAdminForm

    list_display = (
        'get_vstup_card', 
        'get_posadka_badge', 
        'get_ai_status_badge', 
        'get_ai_action_button',
        'copy_paste_asistent'
    )
    list_display_links = ('get_vstup_card',)    
    list_filter = (
        'stav', 
        DatumVstupuFilter, 
        ISASRelatedZakaznikFilter, 
        ISASRelatedTechnikFilter, 
        ('vozidla', admin.RelatedOnlyFieldListFilter)
    )
    isas_technici_filter_field = 'zamestnanci'
    search_fields = ('zakaznik__Nazov_spolocnosti', 'zamestnanci__priezvisko', 'vozidla__spz')
    autocomplete_fields = ['zakaznik', 'uloha']
    filter_horizontal = ('zamestnanci', 'vozidla')

    def get_queryset(self, request):
        """🚀 ISAS PERFORMANCE: Úplná eliminácia N+1 dopytov vrátane úlohy a servisných prác."""
        qs = super().get_queryset(request)
        return qs.select_related('zakaznik', 'uloha').prefetch_related('zamestnanci', 'vozidla', 'uloha__typ_servisu')

    # 🛡️ DEFENSIVE: ai_text_emailu nie je readonly, aby ho dispečer mohol pred odoslaním skontrolovať a opraviť
    readonly_fields = (
        'stav', 
        'get_dokument_akcia',
        'bezpecnostny_kluc', 
        'get_verejny_odkaz',
        'cas_schvalenia_sbs', 
        'ip_schvalitela_sbs',
        'poznamka_sbs',
        'copy_paste_asistent'
    )

    fieldsets = (
        ('🏢 ZÁKLADNÉ ÚDAJE A TERMÍN', {
            'fields': (
                'zakaznik',
                'uloha',
                ('stav', 'datum_vstupu')
            ),
            'description': '📍 Zvoľte cieľový objekt a dátum. Stav povolenia je riadený automaticky systémom a nedá sa manuálne meniť (Ochrana proti podvodom).'
        }),
        ('👥 KTO A S ČÍM (Posádka a Technika)', {
            'fields': ('zamestnanci', 'vozidla'),
            'description': '⚠️ <b>Kritické pre SBS:</b> Nezabudnite priradiť všetky osoby a vozidlá. Ak vozidlo nebude na zozname, vrátnica ho nevpustí do areálu!'
        }),
        ('📲 NASTAVENIA NOTIFIKÁCIÍ (Po schválení/zamietnutí)', {
            'fields': (
                ('notif_admin_tg', 'notif_admin_mail', 'notif_admin_discord'),
                ('notif_technik_tg', 'notif_technik_mail', 'notif_technik_discord'),
            ),
            'description': '🔔 <b>Smart Routing:</b> Vyberte, komu a akou formou má systém doručiť výsledok, keď vrátnica klikne na schválenie alebo zamietnutie vstupu.'
        }),
        ('🤖 AI VÝSTUP A DOKUMENTY', {
            'fields': ('get_dokument_akcia', 'ai_text_emailu', 'copy_paste_asistent'),
            'description': '✨ <b>Ako to funguje?</b> Najskôr tento formulár uložte. Následne v zozname povolení kliknite na modré tlačidlo "🤖 GENERUJ PDF + EMAIL". Systém sem následne nahrá hotové PDF a text e-mailu.'
        }),
        ('🔒 BEZPEČNOSTNÁ STOPA (Audit Log & Portál)', {
            'fields': (
                'get_verejny_odkaz',
                'bezpecnostny_kluc', 
                ('cas_schvalenia_sbs', 'ip_schvalitela_sbs'),
                'poznamka_sbs' 
            ),
            'description': '🛡️ <b>ISO 27001 Audit:</b> Tieto údaje slúžia ako digitálny dôkaz o tom, kedy a z akej IP adresy vrátnica schválila vstup. Údaje sú nezmazateľné.'
        }),
    )

    isas_modern_toggles = True
    isas_copy_paste_assistant = {'trigger_class': 'isas-cpa-btn'}

    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "🚧",
        'custom_buttons_html': mark_safe(
            '<a id="btn-kniha-vstupov" href="kniha-vstupov/" class="isas-action-btn btn-blue">📖 KNIHA VSTUPOV</a>'
            '<button type="button" id="btn-send-sbs-selected" data-action="send_vstupka" class="isas-action-btn btn-green">📤 ODOSLAŤ NA VRÁTNICU</button>'
        ),
        'ignore_default_action_ids': ["btn-kniha-vstupov", "btn-send-sbs-selected"],
        'skryt_akcie': False,
    }

    isas_action_modals = {
        'btn-send-sbs-selected': {
            'type': 'direct_action',
            'action': 'send_vstupka',
            'confirm': True,
            'confirm_title': 'Odoslanie povolenia na vrátnicu 📤',
            'confirm_text': 'Naozaj chcete odoslať schválené povolenie na vstup priamo na e-mail vrátnice (SBS) zákazníka?',
            'loading_text': '⏳ ODOSIELAM...',
            'success_text': '✅ ODOSLANÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr označte aspoň jedno vygenerované povolenie.',
            'reload': False
        }
    }
    
    isas_async_html_action = {
        'trigger_selector': '.btn-ai-vstup',
        'action_type': 'generate_vstupka',
        'loading_text': '⏳ GENERUJEM...',
    }

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('kniha-vstupov/', self.admin_site.admin_view(self.kniha_vstupov_view), name='vstupky-kniha'),
        ]
        return custom_urls + urls

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'generate_vstupka':
            if not request.user.has_perm('Evidencia.change_povolenienavstup'):
                return JsonResponse({"status": "error", "message": "Zabezpečenie: Nemáte oprávnenie generovať vstupy."}, status=403)
                
            if not valid_ids:
                return JsonResponse({"status": "error", "message": "Nebol vybraný žiadny záznam na generovanie."}, status=400)

            povolenie_id = valid_ids[0]
            from .tasks import vygeneruj_vstupku_ai_task
            
            lock_id = f"lock_ai_vstupka_{povolenie_id}"
            if cache.get(lock_id):
                return JsonResponse({"status": "error", "message": "⏳ Dokument sa už generuje. Prosím, počkajte."}, status=429)

            try:
                with transaction.atomic():
                    povolenie = PovolenieNaVstup.objects.select_for_update(of=('self',)).get(id=povolenie_id)
                    # 🛡️ OCHRANA PRED TICHÝM ZLYHANÍM: Blokujeme aj už odoslané záznamy, keďže task ich odmietne
                    if povolenie.stav in ['ODOSLANE', 'SCHVALENE', 'ZAMIETNUTE']:
                        return JsonResponse({"status": "error", "message": f"Záznam je v stave {povolenie.get_stav_display()}. Pre pregenerovanie musíte vytvoriť nový záznam alebo stornovať pôvodný."}, status=400)
                    
                    def _dispatch_vstupka_ai(p_id=povolenie_id):
                        try:
                            vygeneruj_vstupku_ai_task.delay(p_id)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo generovanie vstupky #{p_id} v Celery: {exc}")
                            messages.warning(request, f"⚠️ Generovanie vstupky #{p_id} zlyhalo pri zaradení do fronty (výpadok Celery/Redis).")
                    transaction.on_commit(_dispatch_vstupka_ai)
                    
                return JsonResponse({"status": "success", "message": "🤖 Generovanie náhľadu (PDF a textu) bolo spustené na pozadí."})
            except PovolenieNaVstup.DoesNotExist:
                return JsonResponse({"status": "error", "message": "Záznam nebol nájdený."}, status=404)

        # 🛡️ FÁZA 2: Autorizované odoslanie pripraveného povolenia na vrátnicu
        elif action_type == 'send_vstupka':
            if not request.user.has_perm('Evidencia.change_povolenienavstup'):
                return JsonResponse({"status": "error", "message": "Nemáte oprávnenie odosielať vstupy."}, status=403)

            from .tasks import odosli_vstupku_na_vratnicu_task

            with transaction.atomic():
                # 🛡️ DEFENSIVE: of=('self',) chráni PostgreSQL pred zbytočným zamykaním tabuľky Zakaznik
                povolenia = list(PovolenieNaVstup.objects.select_for_update(of=('self',)).select_related('zakaznik').filter(id__in=valid_ids))

                if not povolenia:
                    return JsonResponse({"status": "error", "message": "Neboli nájdené žiadne záznamy na odoslanie."}, status=404)

                # 🛡️ PASS 1: Prísna predbežná validácia VŠETKÝCH položiek pred registráciou taskov
                for p in povolenia:
                    if p.stav != 'GENEROVANE':
                        return JsonResponse({"status": "error", "message": f"Povolenie #{p.id} nie je v stave 'Vygenerované' (Aktuálny stav: {p.get_stav_display()}). Pred odoslaním vygenerujte náhľad."}, status=400)
                    if not p.vygenerovany_dokument or not p.vygenerovany_dokument.name or not default_storage.exists(p.vygenerovany_dokument.name):
                        return JsonResponse({"status": "error", "message": f"Povolenie #{p.id} nemá platný PDF súbor na úložisku. Spustite pregenerovanie náhľadu."}, status=400)
                    email_cisty = str(getattr(p.zakaznik, 'email_vratnice', '') or '').strip()
                    if not p.zakaznik or not email_cisty:
                        return JsonResponse({"status": "error", "message": f"Zákazník '{p.zakaznik}' nemá v systéme platný e-mail vrátnice. Vstup nie je kam odoslať."}, status=400)

                # 🛡️ PASS 2: Až keď máme 100% garanciu, že celá dávka je platná, zaregistrujeme odoslanie
                for p in povolenia:
                    def _dispatch_odosli_vstupku(pid=p.id):
                        try:
                            odosli_vstupku_na_vratnicu_task.delay(pid)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo odoslanie vstupky #{pid} do Celery: {exc}")
                            messages.warning(request, f"⚠️ Odoslanie vstupky #{pid} na vrátnicu zlyhalo pri zaradení do fronty (výpadok Celery/Redis).")
                    transaction.on_commit(_dispatch_odosli_vstupku)

            return JsonResponse({"status": "success", "message": f"📤 Všetkých {len(povolenia)} povolení bolo úspešne odoslaných na spracovanie."})
                
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'zakaznik'
        ).prefetch_related(
            'vozidla'
        ).filter(
            Q(zakaznik__Nazov_spolocnosti__icontains=term) |
            Q(zamestnanci__priezvisko__icontains=term) |
            Q(vozidla__spz__icontains=term)
        ).distinct()[:15]

    def format_detective_result(self, obj):
        zakaznik_nazov = obj.zakaznik.Nazov_spolocnosti if obj.zakaznik else "Neznámy zákazník"
        datum = obj.datum_vstupu.strftime('%d.%m.%Y') if obj.datum_vstupu else ""
        auta = ", ".join([a.spz for a in obj.vozidla.all() if getattr(a, 'spz', None)])
        return {
            'id': obj.id,
            'name': f"{zakaznik_nazov} - {datum}",
            'search_value': str(obj.id),
            'rola': auta if auta else "",
            'cat': "VSTUP"
        }

    def save_model(self, request, obj, form, change):
        # PDF zmažeme LEN VTEDY, ak sa zmenili polia, ktoré sa reálne tlačia do dokumentu.
        # Úprava textu e-mailu (ai_text_emailu) ani notifikácií nesmie zmazať hotové PDF!
        if change and obj.stav == 'GENEROVANE' and obj.vygenerovany_dokument and obj.vygenerovany_dokument.name:
            POLIA_OVPLYVNUJUCE_PDF = {'zakaznik', 'uloha', 'datum_vstupu', 'zamestnanci', 'vozidla'}
            zmenene_polia = set(form.changed_data)
            
            if zmenene_polia & POLIA_OVPLYVNUJUCE_PDF:
                try:
                    if default_storage.exists(obj.vygenerovany_dokument.name):
                        default_storage.delete(obj.vygenerovany_dokument.name)
                except Exception as e:
                    logger.warning(f"⚠️ Nepodarilo sa odstrániť neaktuálny dokument pri zmene: {e}")
                obj.vygenerovany_dokument = None
                
        super().save_model(request, obj, form, change)

    @admin.display(description='🏢 Cieľ & Termín', ordering='datum_vstupu')
    def get_vstup_card(self, obj):
        image_url = None
        
        # 🌐 Dynamické načítanie loga klienta cez Google Favicon API
        if obj.zakaznik and getattr(obj.zakaznik, 'domena', None):
            clean_domain = str(obj.zakaznik.domena).lower().replace('http://', '').replace('https://', '').replace('www.', '').split('/')[0].strip()
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
            if '.' in clean_domain:
                image_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"

        # Zvýraznenie, ak je vstup DNES (s defenzívnou kontrolou prítomnosti dátumu)
        dnes = timezone.now().date()
        if obj.datum_vstupu and obj.datum_vstupu == dnes:
            date_badge = render_isas_badge("DNES", icon="🔥", type_color="warning", wrapper=False)
        elif obj.datum_vstupu:
            date_badge = render_isas_badge(obj.datum_vstupu.strftime('%d.%m.%Y'), icon="📅", type_color="neutral", wrapper=False)
        else:
            date_badge = render_isas_badge("Bez termínu", icon="⚠️", type_color="danger", wrapper=False)

        nazov_zakaznika = getattr(obj.zakaznik, 'Nazov_spolocnosti', '') or "Neznámy"
        return render_isas_avatar(
            icon="🏢",
            title=nazov_zakaznika[:25],
            subtitle=None,
            bg_gradient="#64748b, #334155",
            shadow_color="rgba(100, 116, 139, 0.25)",
            extra_html=date_badge,
            image_url=image_url
        )

    @admin.display(description="📥 Akcia")
    def get_ai_action_button(self, obj):
        if obj.stav in ['SCHVALENE', 'ZAMIETNUTE']:
            content = render_isas_text("Uzavreté", icon="🔒", muted=True)
            return render_isas_layout(content, layout="col", center=True, html_id=f"ai-action-box-{obj.id}")

        # Stav ODOSLANÉ: Už odišlo na vrátnicu
        if obj.stav == 'ODOSLANE' and obj.vygenerovany_dokument:
            btn_pdf = render_isas_link_btn(
                url=obj.vygenerovany_dokument.url,
                text="VSTUP PDF",
                icon="📄",
                type_color="neutral"
            )
            badge_odoslane = render_isas_badge("ODOSLANÉ", icon="📧", type_color="info", wrapper=False)
            content = render_isas_layout(format_html('{}{}', btn_pdf, badge_odoslane), layout="col", center=True, gap=2)
            return render_isas_layout(content, layout="col", center=True, html_id=f"ai-action-box-{obj.id}")

        # Stav GENEROVANÉ: PDF je hotové, čaká na kontrolu a schválenie
        if obj.stav == 'GENEROVANE' and obj.vygenerovany_dokument:
            btn_pdf = render_isas_link_btn(
                url=obj.vygenerovany_dokument.url,
                text="VSTUP PDF",
                icon="👁️",
                type_color="success"
            )
            badge_caka = render_isas_badge("Čaká na odoslanie", icon="⏳", type_color="warning", wrapper=False)
            content = render_isas_layout(format_html('{}{}', btn_pdf, badge_caka), layout="col", center=True, gap=2)
            return render_isas_layout(content, layout="col", center=True, html_id=f"ai-action-box-{obj.id}")

        # Ešte nevygenerované: Spustenie generovania náhľadu
        content = render_isas_link_btn(
            url="#",
            text="🤖 Generuj náhľad",
            type_color="info",
            is_button=True,
            css_class="isas-action-btn btn-blue btn-ai-vstup",
            data_attrs={"id": str(obj.id)}
        )
        return render_isas_layout(content, layout="col", center=True, html_id=f"ai-action-box-{obj.id}")

    @admin.display(description='👥 Posádka & 🚗 Technika')
    def get_posadka_badge(self, obj):
        technici_all = obj.zamestnanci.all()
        auta_all = obj.vozidla.all()
        
        technici_count = len(technici_all)
        auta_count = len(auta_all)
        
        technici_text = f"{technici_count} osôb" if technici_count > 0 else "Žiadne osoby"
        auta_platne = [a.spz for a in auta_all if getattr(a, 'spz', None)]
        auta_text = ", ".join(auta_platne) if auta_platne else "Žiadne vozidlo"

        return render_isas_mini_stat_card(
            top_label="Pridelení technici",
            top_val=f"👷 {technici_text}",
            bot_label="Evidenčné čísla",
            bot_val=f"🚗 {auta_text}",
            bot_val_color="#3b82f6"
        )

    @admin.display(description='🚥 Stav', ordering='stav')
    def get_ai_status_badge(self, obj):
        if obj.stav == 'SCHVALENE':
            return render_isas_badge("SCHVÁLENÉ", icon="✅", type_color="success")
        elif obj.stav == 'ODOSLANE':
            return render_isas_badge("ODOSLANÉ", icon="📧", type_color="info")
        elif obj.stav == 'ZAMIETNUTE':
            return render_isas_badge("ZAMIETNUTÉ", icon="❌", type_color="danger")
        
        # 🛡️ DEFENSIVE: Rozlíšenie či je koncept pripravený na kontrolu, alebo ešte len čaká na vygenerovanie
        if obj.vygenerovany_dokument:
            return render_isas_badge("PRIPRAVENÉ", icon="📝", type_color="warning")
        return render_isas_badge("ČAKÁ NA AI", icon="🤖", type_color="neutral")

    @admin.display(description="📄 Vygenerovaný dokument")
    def get_dokument_akcia(self, obj):
        """Zobrazí odkaz na stiahnutie vo formulári, ak dokument existuje."""
        if obj.vygenerovany_dokument:
            return render_isas_link_btn(
                url=obj.vygenerovany_dokument.url,
                text="Stiahnuť vygenerované PDF",
                icon="⬇️",
                type_color="danger",
                wrapper=False
            )
        return render_isas_text("Dokument zatiaľ nebol vygenerovaný. Uložte záznam a kliknite na 'Generovať' v zozname.", muted=True)

    @admin.display(description="🌐 Odkaz na Klientsky Portál (Pre SBS)")
    def get_verejny_odkaz(self, obj):
        """Vygeneruje presnú URL adresu, ktorú vidí SBSkár."""
        if not obj.pk:
            return render_isas_text("Odkaz sa vygeneruje po uložení.", muted=True)
        
        try:
            url = reverse('evidencia:schvalit_vstup_sbs', args=[obj.bezpecnostny_kluc])
            from ISASaplikacia.models import GlobalneNastavenia
            gn = GlobalneNastavenia.objects.first()
            base_url = (gn.clean_base_url if gn and getattr(gn, 'clean_base_url', None) else getattr(settings, 'SITE_URL', '')).rstrip('/')
            full_url = f"{base_url}{url}" if base_url else url
            return render_isas_badge(
                text=full_url,
                icon="🔗",
                type_color="info",
                copy_value=full_url,
                tooltip="Kliknutím skopírujete odkaz pre SBS"
            )
        except Exception:
            return render_isas_text("URL adresa portálu zatiaľ nie je nakonfigurovaná v urls.py", color="#ef4444", icon="⚠️")

    @admin.display(description="🤖 ISAS Asistent pre externé portály")
    def copy_paste_asistent(self, obj):
        if not obj.pk:
            return render_isas_text("Asistent bude dostupný až po uložení záznamu.", muted=True)

        # 1. Zozbierame údaje o NAŠEJ FIRME (Dodávateľ)
        firma_data = None
        try:
            from ISASaplikacia.models import FiremnaIdentita
            firma = FiremnaIdentita.objects.first()
            if firma:
                f_ulica = f"{getattr(firma, 'sidlo_ulica', '') or ''} {getattr(firma, 'sidlo_cislo', '') or ''}".strip()
                f_mesto = f"{getattr(firma, 'sidlo_psc', '') or ''} {getattr(firma, 'sidlo_mesto', '') or ''}".strip()
                f_stat = getattr(firma, 'sidlo_stat', '') or getattr(firma, 'stat', '') or ''
                f_adresa_parts = [p for p in [f_ulica, f_mesto, f_stat] if p]
                firma_data = {
                    "nazov": getattr(firma, 'nazov_spolocnosti', '') or "",
                    "ico": getattr(firma, 'ico', '') or "",
                    "dic": getattr(firma, 'dic', '') or "",
                    "ic_dph": getattr(firma, 'ic_dph', '') or "",
                    "adresa": ", ".join(f_adresa_parts),
                    "iban": getattr(firma, 'iban', '') or "",
                    "swift": getattr(firma, 'swift', '') or "",
                    "telefon": getattr(firma, 'telefon', '') or "",
                    "email": getattr(firma, 'email_vseobecny', '') or ""
                }
        except ImportError:
            pass # Ak by model nebol dostupný, asistent bude fungovať aj bez neho

        # 2. Zozbierame rozšírené údaje o TECHNIKOCH
        technici = []
        for t in obj.zamestnanci.all():
            technici.append({
                "meno": f"{t.meno} {t.priezvisko}",
                "op": t.cislo_op or "Bez OP",
                "narodenie": t.datum_narodenia.strftime('%d.%m.%Y') if t.datum_narodenia else "Bez dátumu",
                "stat": t.stat or "",
                "telefon": t.telefon or "Bez telefónu",
                "email": t.email or "Bez e-mailu",
                "pomer": t.get_typ_pomeru_display() if t.typ_pomeru else "Neznámy pomer",
                "bozp": t.bozp_platne_do.strftime('%d.%m.%Y') if t.bozp_platne_do else "Chýba BOZP",
                "odborna_sposobilost": ", ".join([c.nazov for c in t.certifikaty.all()[:2]]) or "Bez osvedčenia"
            })

        # 3. Zozbierame rozšírené údaje o VOZIDLÁCH
        auta = []
        for a in obj.vozidla.all():
            auta.append({
                "spz": a.spz,
                "model": a.znacka_a_model,
                "typ": a.get_typ_vozidla_erp_display() if hasattr(a, 'get_typ_vozidla_erp_display') else "Neznámy typ",
                "palivo": a.get_typ_paliva_display() if hasattr(a, 'get_typ_paliva_display') else (a.typ_paliva or "Diesel"),
                "stk": a.stk_plati_do.strftime('%d.%m.%Y') if getattr(a, 'stk_plati_do', None) else "Neznáma STK",
                "pzp": a.pzp_plati_do.strftime('%d.%m.%Y') if getattr(a, 'pzp_plati_do', None) else "Bez PZP"
            })

        # 4. Údaje o ZÁKAZNÍKOVI a VSTUPE
        url_portalu = ""
        zakaznik_data = {"nazov": "Neznámy"}
        
        if obj.zakaznik:
            if hasattr(obj.zakaznik, 'url_vstupneho_portalu') and obj.zakaznik.url_vstupneho_portalu:
                url_portalu = obj.zakaznik.url_vstupneho_portalu
                
            # Zozbierame všetky dôležité údaje o zákazníkovi
            z_ulica = str(obj.zakaznik.ulica_a_cislo or '').strip()
            z_mesto = f"{obj.zakaznik.psc or ''} {obj.zakaznik.mesto or ''}".strip()
            z_stat = str(getattr(obj.zakaznik, 'stat', '') or '').strip()
            z_adresa_parts = [p for p in [z_ulica, z_mesto, z_stat] if p]
            zakaznik_data = {
                "nazov": obj.zakaznik.Nazov_spolocnosti or "Neznámy",
                "ico": obj.zakaznik.ico or "",
                "dic": obj.zakaznik.dic or "",
                "ic_dph": obj.zakaznik.ic_dph or "",
                "adresa": ", ".join(z_adresa_parts),
                "kontakt": str(obj.zakaznik.kontaktna_osoba or ""),
                "telefon": str(obj.zakaznik.telefon or ""),
                "instrukcie": str(obj.zakaznik.instrukcie_vstupu or "")
            }

        ucel_vstupu = "Servisný zásah / Údržba"
        if obj.uloha:
            typy_prac = ", ".join([str(t.nazov) for t in obj.uloha.typ_servisu.all() if t.nazov])
            ucel_vstupu = f"Výkon prác: {typy_prac}" if typy_prac else ucel_vstupu

        # Vytvoríme bezpečný JSON
        data_json = json.dumps({
            "firma": firma_data,
            "technici": technici, 
            "auta": auta, 
            "url": url_portalu,
            "zakaznik": zakaznik_data,
            "datum_vstupu": obj.datum_vstupu.strftime('%d.%m.%Y') if obj.datum_vstupu else "",
            "ucel": ucel_vstupu
        })
        # 🛡️ DEFENSIVE: mark_safe zabráni druhotnému escapovaniu v šablóne/format_html na &amp;quot;
        safe_json = mark_safe(escape(data_json))

        btn = render_isas_link_btn(
            url="#",
            text="Copy-Paste Asistent",
            icon="📋",
            type_color="info",
            is_button=True,
            css_class="isas-cpa-btn",
            data_attrs={"payload": safe_json},
            custom_style="box-shadow: 0 4px 6px rgba(37, 99, 235, 0.2);"
        )
        
        return render_isas_layout(btn, layout="col", html_id=f"isas-cpa-container-{obj.id}")

    def kniha_vstupov_view(self, request):
        """Elitný view pre zobrazenie otvorenej knihy VSTUPOV pre konkrétneho zamestnanca."""
        
        if not request.user.has_perm('Evidencia.view_povolenienavstup'):
            raise PermissionDenied

        zamestnanci = Zamestnanec.objects.all().order_by('meno', 'priezvisko')
        vybrany_zamestnanec_id = request.GET.get('technik_id')
        
        otvorene_vstupy = []
        vybrany_technik = None
        dnes = timezone.now().date()

        if vybrany_zamestnanec_id:
            try:
                vybrany_technik = Zamestnanec.objects.get(id=vybrany_zamestnanec_id)
                # 🧠 LOGIKA: Vytiahneme iba vstupy naplánované na DNES alebo do BUDÚCNOSTI. 
                # Staré vstupy z "knihy" zmiznú (ale ostanú v DB kvôli auditu).
                otvorene_vstupy = PovolenieNaVstup.objects.filter(
                    zamestnanci=vybrany_technik, 
                    datum_vstupu__gte=dnes
                ).select_related('zakaznik').prefetch_related('vozidla', 'zamestnanci').order_by('datum_vstupu')
            except (Zamestnanec.DoesNotExist, ValueError):
                pass

        context = dict(
            self.admin_site.each_context(request),
            title="📖 Kniha Vstupov (Dispečing)",
            zamestnanci=zamestnanci,
            vybrany_technik=vybrany_technik,
            otvorene_vstupy=otvorene_vstupy,
        )
        return render(request, "admin/kniha_vstupov.html", context)

@admin.register(VystavenaFaktura)
class VystavenaFakturaAdmin(ERPExportAdminMixin, ISASPremiumAdmin):
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('zakaznik', 'vystavil', 'zariadenie').prefetch_related('typ_servisu', 'polozky', 'polozky__zariadenie')

    list_display_links = ('get_identifikacia_card',)
    list_display = ('erp_status_ikona', 'get_identifikacia_card', 'get_odberatel_card', 'zobraz_typ_servisu', 'zobraz_zdroje', 'get_financie_card', 'zobraz_vystavovatela', 'get_akcie_card')
    list_filter = ('typ_dokladu', ISASRelatedUserFilter, 'erp_export_status', 'stav', 'peppol_status', ISASDateRangeFilter, 'prenos_danovej_povinnosti', ISASRelatedZakaznikFilter)
    isas_user_filter_field = 'vystavil'
    isas_date_filter_field = 'datum_vystavenia'
    autocomplete_fields = ['zakaznik', 'zariadenie', 'povodna_faktura', 'zdrojova_uloha', 'zdrojove_ponuky', 'zdrojove_objednavky', 'zdrojove_zaznamy']
    search_fields = ('=id', 'cislo_faktury', 'zakaznik__Nazov_spolocnosti', 'zakaznik__ico', 'odberatel_nazov', 'odberatel_ico', 'variabilny_symbol')
    
    # Všetky snapshotové a automaticky generované polia musia byť tu, inak Django spadne!
    readonly_fields = (
        'dodavatel_nazov', 'odberatel_nazov', 'typ_servisu',
        'dodavatel_ico', 'odberatel_ico',
        'dodavatel_dic', 'odberatel_dic',
        'dodavatel_ic_dph', 'odberatel_ic_dph',
        'dodavatel_iban', 'dodavatel_swift', 'dodavatel_banka',
        'dodavatel_sidlo', 'dodavatel_zapis', 'odberatel_sidlo', 'vystavil_meno',
        'celkom_bez_dph', 'celkom_dph', 'celkom_s_dph',
        'cislo_faktury', 'variabilny_symbol', 'pdf_dokument',
        'erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log',
        'peppol_status', 'peppol_transaction_id', 'peppol_odoslane_dna', 'peppol_xml_hash', 'peppol_error_log', 'peppol_xml_dokument',
        'bezpecnostny_kluc'
    )
    
    fieldsets = (
        ('🏢 ZÁKLADNÉ ÚDAJE A VÄZBY', {
            'fields': (
                ('zakaznik', 'stav'),
                ('typ_dokladu', 'typ_servisu'),
                'zariadenie',
                'povodna_faktura',
                ('cislo_objednavky_klienta', 'vystavil'),
                'kod_clenenia_dph',
                ('zdrojovy_sklad_id', 'odpisat_zo_skladu'),
                ('notif_odpis_tg', 'notif_odpis_mail', 'notif_odpis_discord'),
                ('notif_majitel_tg', 'notif_majitel_mail', 'notif_majitel_discord'),
            )
        }),
        ('🔗 SYSTÉMOVÉ ZDROJE A PÁROVANIE', {
            'fields': (
                'zdrojova_uloha',
                ('zdrojove_ponuky', 'zdrojove_objednavky'),
                'zdrojove_zaznamy',
            ),
            'description': 'Prepojenie faktúry na zdrojovú zákazku, ponuky, prijaté objednávky a servisné záznamy.'
        }),
        ('💰 PLATOBNÉ ÚDAJE A DÁTUMY', {
            'fields': (
                ('sposob_uhrady', 'mena'),
                ('konstantny_symbol', 'prenos_danovej_povinnosti'),
                ('datum_vystavenia', 'datum_dodania'),
                ('datum_splatnosti', 'datum_uhrady'),
            )
        }),
        ('🤖 ISAS AI & POZNÁMKY', {
            'fields': (
                'ai_poznamka',
                'poznamka_pre_klienta'
            )
        }),
        ('🔒 ÚČTOVNÝ SNAPSHOT A SUMÁRE', {
            'fields': (
                ('cislo_faktury', 'variabilny_symbol'),
                ('celkom_bez_dph', 'celkom_dph', 'celkom_s_dph'),
                'pdf_dokument',
                ('dodavatel_nazov', 'odberatel_nazov'),
                ('dodavatel_ico', 'odberatel_ico'),
                ('dodavatel_dic', 'odberatel_dic'),
                ('dodavatel_ic_dph', 'odberatel_ic_dph'),
                ('dodavatel_iban', 'dodavatel_swift'),
                ('dodavatel_banka', 'vystavil_meno'),
                ('dodavatel_sidlo', 'odberatel_sidlo'),
                'dodavatel_zapis',
                'bezpecnostny_kluc',
            ),
            'description': '<div style="color: #ef4444; font-size: 11px; font-weight: bold;">🔒 Tieto údaje sa napevno zapíšu do databázy v momente schválenia faktúry.</div>'
        }),
        ('📡 ISAS ERP DIAGNOSTIKA', {
            'fields': ('erp_export_status', 'erp_id', 'erp_last_exported_at', 'erp_error_log'),
        }),
        ('🇪🇺 ELEKTRONICKÁ FAKTÚRA (PEPPOL)', {
            'fields': ('peppol_status', 'peppol_transaction_id', 'peppol_odoslane_dna', 'peppol_xml_hash', 'peppol_error_log', 'peppol_xml_dokument'),
            'description': '<div style="color: #3b82f6; font-size: 11px; font-weight: bold;">💡 Komunikácia so sieťou elektronických faktúr Peppol. Tu nájdete auditný XML súbor, časovú stopu a kryptografický hash pre zákonnú archiváciu.</div>'
        }),
    )

    # --- ⚙️ ISAS CORE KONFIGURÁCIA ---
    isas_title = "Vystavené Faktúry 💰"
    isas_preloader_text = "⏳ Vstupujete do zoznamu vystavených faktúr..."
    isas_delete_iterate = True  # 🛡️ Vynúti volanie .delete() po jednom pre správny návrat tovaru na sklad
    
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "💰",
        'custom_buttons_html': (
            '<button type="button" id="btn-mark-sent" data-action="mark_sent" class="isas-action-btn btn-blue">📤 ODOSLAŤ</button>'
            '<button type="button" id="btn-storno-selected" data-action="mark_storno" class="isas-action-btn btn-black" style="background: #475569;">❌ STORNO</button>'
            '<button type="button" id="btn-pay-selected" data-action="mark_paid" class="isas-action-btn btn-green">💰 ZAPLATENÉ</button>'
            '<button type="button" id="btn-peppol-selected" data-action="send_peppol" class="isas-action-btn" style="background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%);">🇪🇺 eFAKTÚRA</button>'
        )
    }

    isas_action_modals = {
        'btn-mark-sent': {
            'type': 'confirm', 
            'action': 'mark_sent',
            'icon': '📤',
            'title': 'Odoslať faktúry e-mailom? 📤',
            'subtitle': (
                '✉️ Zákazníkovi a účtovníčke bude odoslaný e-mail s PDF dokladom.<br><br>'
                '💡 <b>RADA k štátnej eFaktúre:</b> Ak riešite len opätovné odoslanie po chybe (CHYBA ❌), '
                'kliknite na <b>Zrušiť</b> a použite tlačidlo <b>🇪🇺 eFAKTÚRA</b> (zákazník tak nedostane duplicitný e-mail).'
            ),
            'confirm_text': 'Áno, odoslať',
            'loading_text': '⏳ ODOSIELAM...', 
            'success_text': '✅ ODOSLANÉ',
            'require_selection': True, 
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jednu faktúru.', 
            'reload': True
        },
        'btn-storno-selected': {
            'type': 'direct_action',
            'action': 'mark_storno',
            'confirm': True,
            'confirm_title': 'Stornovanie vystavených faktúr 🛑',
            'confirm_text': 'Naozaj chcete stornovať vybrané faktúry? Tento krok zmení ich stav a zachová ich čísla pre audity. Doklady odoslané do eFaktúry nie je možné stornovať.',
            'loading_text': '⏳ STORNUJEM...',
            'success_text': '❌ STORNOVANÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jednu faktúru.',
            'reload': True
        },
        'btn-pay-selected': {
            'type': 'direct_action',
            'action': 'mark_paid',
            'confirm': True,
            'confirm_title': 'Evidencia úhrady faktúr 💰',
            'confirm_text': 'Naozaj chcete označiť vybrané faktúry ako zaplatené? Systém automaticky vytvorí príjmové pokladničné doklady na zostatkové sumy.',
            'loading_text': '⏳ SPRACOVÁVAM...',
            'success_text': '✅ ZAPLATENÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jednu faktúru.',
            'reload': True
        },
        'btn-peppol-selected': {
            'type': 'direct_action',
            'action': 'send_peppol',
            'confirm': True,
            'confirm_title': 'Odoslanie do siete Peppol (eFaktúra) 🇪🇺',
            'confirm_text': 'Naozaj chcete vybrané daňové doklady odoslať do európskej siete elektronických faktúr (Peppol)? Úspešne doručené doklady už nebude možné zrušiť ani stornovať.',
            'loading_text': '⏳ ODOSIELAM...',
            'success_text': '🇪🇺 ODOSLANÉ',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Vyberte aspoň jednu faktúru.',
            'reload': True
        }
    }

    isas_hide_submit_row = True
    isas_custom_save_action = {
        'title': 'Uložiť a Odoslať faktúru? 🚀',
        'text': (
            '💡 Tento krok uloží doklad a odošle e-mail s PDF zákazníkovi aj účtovníčke.<br><br>'
            '💡 <b>Tip k štátnej eFaktúre:</b> Ak potrebujete len zopakovať odoslanie po chybe, '
            'použite v zozname faktúr tlačidlo <b>🇪🇺 eFAKTÚRA</b>.'
        ),
        'confirm_text': 'Áno, uložiť a odoslať',
        'button_text': "<span style='font-size: 18px; margin-right: 8px;'>🚀</span> ULOŽIŤ A ODOSLAŤ VYSTAVENÚ FAKTÚRU",
        'action_value': 'odoslat'
    }

    @property
    def isas_autofill_rules(self):
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            defaults = FiremnaIdentitaModel.get_country_defaults() if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
            kod_prenos = defaults.get('kod_dph_prenos', 'VAT_REVERSE')
            kod_vystavena = defaults.get('kod_dph_vystavena', 'VAT_OUT')
        except Exception:
            kod_prenos = 'VAT_REVERSE'
            kod_vystavena = 'VAT_OUT'

        return [
            {
                'trigger_field': 'prenos_danovej_povinnosti',
                'action': 'toggle_value',
                'target_field': 'kod_clenenia_dph',
                'value_checked': kod_prenos,
                'value_unchecked': kod_vystavena
            }
        ]
    isas_auto_poll = True

    def add_view(self, request, form_url='', extra_context=None):
        return redirect(reverse('evidencia:faktura_builder_nova'))

    def change_view(self, request, object_id, form_url='', extra_context=None):
        # 🛡️ DEFENSIVE REDIRECT: Rozpracovaný koncept patrí výhradne do Buildera
        obj = self.get_object(request, object_id)
        if obj and obj.stav == 'DRAFT':
            return redirect(reverse('evidencia:faktura_builder', args=[obj.pk]))
        return super().change_view(request, object_id, form_url, extra_context)

    def has_delete_permission(self, request, obj=None):
        # 🛡️ 100% OCHRANA: Faktúru nie je možné zmazať, ak je v štátnom systéme eFaktúra, v ERP alebo má úhrady
        if obj:
            if obj.stav not in ['DRAFT', 'NACHYSTANA']:
                return False
            je_exportovana = getattr(obj, 'erp_export_status', '') in ['EXPORTED', 'PROCESSING']
            je_peppol = getattr(obj, 'peppol_status', '') in ['SENT', 'PROCESSING']
            ma_uhrady = hasattr(obj, 'uhrady') and obj.uhrady.exists()
            if je_exportovana or je_peppol or ma_uhrady:
                return False
        return super().has_delete_permission(request, obj)

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'zakaznik', 'zariadenie'
        ).prefetch_related(
            'polozky__zariadenie'
        ).filter(
            Q(cislo_faktury__icontains=term) |
            Q(variabilny_symbol__icontains=term) |
            Q(cislo_objednavky_klienta__icontains=term) |
            Q(odberatel_nazov__icontains=term) |
            Q(zakaznik__Nazov_spolocnosti__icontains=term) |
            Q(zakaznik__ico__icontains=term)
        ).order_by('-datum_vystavenia')[:30]

    def format_detective_result(self, obj):
        customer_str = getattr(obj.zakaznik, 'Nazov_spolocnosti', None) if obj.zakaznik else (obj.odberatel_nazov or "Neznámy zákazník")
        
        zariadenia_nazvy = set()
        if obj.zariadenie:
            model_val = getattr(obj.zariadenie, 'model_zariadenia', None)
            nazov = str(model_val).strip() if model_val else str(obj.zariadenie)
            zariadenia_nazvy.add(nazov)
            
        for polozka in obj.polozky.all():
            if polozka.zariadenie:
                model_val = getattr(polozka.zariadenie, 'model_zariadenia', None)
                nazov = str(model_val).strip() if model_val else str(polozka.zariadenie)
                zariadenia_nazvy.add(nazov)
        
        if zariadenia_nazvy:
            customer_str += f" ({', '.join(zariadenia_nazvy)})"
            
        safe_name = obj.cislo_faktury if obj.cislo_faktury else f"DRAFT-#{obj.id}"
        mena_fav = obj.mena or get_firemna_mena()
        safe_suma = f"{obj.celkom_s_dph} {mena_fav}" if obj.celkom_s_dph is not None else f"0.00 {mena_fav}"
        
        return {
            'id': obj.id, 
            'name': safe_name, 
            'search_value': safe_name,
            'rola': customer_str,
            'cat': 'FAKTÚRA',
            'suma': safe_suma,
            'date': obj.datum_vystavenia.strftime('%d.%m.%Y') if obj.datum_vystavenia else 'Nešpecifikované'
        }

    def get_search_results(self, request, queryset, search_term):
        queryset, use_distinct = super().get_search_results(request, queryset, search_term)
        
        # Filter podľa zákazníka
        zakaznik_id = request.GET.get('filter_zakaznik_id')
        if zakaznik_id:
            queryset = queryset.filter(zakaznik_id=zakaznik_id)
            
        return queryset, use_distinct

    def save_model(self, request, obj, form, change):
        # Uložíme len základné dáta. Celery tasky riešime až po uložení inlines!
        super().save_model(request, obj, form, change)
        if obj.ai_poznamka and "⚠️ AUDIT:" in obj.ai_poznamka:
            self.message_user(
                request, 
                "⚠️ Upozornenie: Faktúra bola vystavená po zákonnej lehote. Podrobnosti nájdete v sekcii AI & Poznámky.", 
                level=messages.WARNING
            )

    def response_add(self, request, obj, post_url_continue=None):
        res = super().response_add(request, obj, post_url_continue)
        self._spracuj_isas_akciu_po_ulozeni(request, obj)
        return res

    def response_change(self, request, obj):
        res = super().response_change(request, obj)
        self._spracuj_isas_akciu_po_ulozeni(request, obj)
        return res

    def _spracuj_isas_akciu_po_ulozeni(self, request, obj):
        akcia = request.POST.get('isas_po_ulozeni_akcia')
        if akcia:
            with transaction.atomic():
                locked_obj = VystavenaFaktura.objects.select_for_update().get(pk=obj.pk)
                
                if locked_obj.stav in ['DRAFT', 'NACHYSTANA']:
                        try:
                            locked_obj.schvalit_a_vystavit(request.user)
                        except ValidationError as val_e:
                            # Zachytenie chyby a bezpečné zobrazenie správy bez pádu do HTTP 500
                            err_msg = "; ".join(val_e.messages) if hasattr(val_e, 'messages') else str(val_e)
                            messages.error(request, f"❌ Doklad nie je možné schváliť: {err_msg}")
                            return

                        if akcia == 'ulozit_iba':
                            from .tasks import vygeneruj_pdf_a_notifikuj_fakturu_task
                            base_url = request.build_absolute_uri('/')[:-1]
                            # Pevné zviazanie premenných (o_id, url)
                            def _dispatch_faktura_save(o_id=locked_obj.id, url=base_url):
                                try:
                                    vygeneruj_pdf_a_notifikuj_fakturu_task.delay(o_id, url, False)
                                except Exception as exc:
                                    logger.warning(f"Zlyhalo generovanie PDF faktúry #{o_id} do Celery: {exc}")
                                    messages.warning(request, f"⚠️ Faktúra #{o_id} bola uložená, ale generovanie PDF sa nepodarilo zaradiť do fronty (výpadok Celery/Redis).")
                            transaction.on_commit(_dispatch_faktura_save)
                            
                        elif akcia == 'odoslat':
                            locked_obj.stav = 'ODOSLANA'
                            locked_obj.save(update_fields=['stav'])
                            from .tasks import vygeneruj_pdf_a_notifikuj_fakturu_task
                            base_url = request.build_absolute_uri('/')[:-1]
                            # Pevné zviazanie premenných (o_id, url)
                            def _dispatch_faktura_send(o_id=locked_obj.id, url=base_url):
                                try:
                                    vygeneruj_pdf_a_notifikuj_fakturu_task.delay(o_id, url, True)
                                except Exception as exc:
                                    logger.warning(f"Zlyhalo generovanie a odoslanie faktúry #{o_id} do Celery: {exc}")
                                    messages.warning(request, f"⚠️ Faktúra #{o_id} bola vystavená, ale odoslanie zlyhalo pri zaradení do fronty (výpadok Celery/Redis).")
                            transaction.on_commit(_dispatch_faktura_send)

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        # 🛡️ 1. OCHRANA PRE MAZANIE (Vrátane kontroly Peppol / SAPI-SK)
        if action_type == 'delete':
            faktury = self.model.objects.filter(id__in=valid_ids)
            for f in faktury:
                je_exportovana = getattr(f, 'erp_export_status', '') in ['EXPORTED', 'PROCESSING']
                je_peppol = getattr(f, 'peppol_status', '') in ['SENT', 'PROCESSING']
                ma_uhrady = hasattr(f, 'uhrady') and f.uhrady.exists()
                if f.stav not in ['DRAFT', 'NACHYSTANA'] or je_exportovana or je_peppol or ma_uhrady:
                    názov_dokladu = f.cislo_faktury if f.cislo_faktury else f"ID #{f.id}"
                    return JsonResponse({
                        "status": "error", 
                        "message": f"Faktúru {názov_dokladu} nie je možné zmazať. Je evidovaná v štátnej eFaktúre, má evidované platby alebo je uzamknutá v ERP. 🔒"
                    }, status=400)
                    
        # 🛡️ 2. OCHRANA PRE ERP EXPORT (Toto garantuje, že DRAFT sa neodošle do Pohody)
        elif action_type == 'export_erp':
            faktury = self.model.objects.filter(id__in=valid_ids)
            for f in faktury:
                if f.stav == 'DRAFT':
                    return JsonResponse({
                        "status": "error", 
                        "message": "DRAFT faktúry sa nedajú exportovať do účtovníctva! 🛑"
                    }, status=400)
                    
        return super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type in ["mark_paid", "mark_sent", "mark_storno", "send_peppol"]:
            if not request.user.has_perm('Evidencia.change_vystavenafaktura'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať vystavené faktúry."}, status=403)
        
        if action_type == "mark_paid":
            
            count = 0
            with transaction.atomic():
                faktury_na_uhradu = list(self.model.objects.select_for_update().filter(
                    id__in=valid_ids
                ).exclude(stav__in=['DRAFT', 'ZAPLATENA', 'STORNO']))
                
                if not faktury_na_uhradu:
                    return JsonResponse({"status": "error", "message": "Zvolené faktúry sú buď DRAFT, Stornované alebo už zaplatené. 🛑"})

                uhrady_na_vytvorenie = []
                for f in faktury_na_uhradu:
                    try:
                        suma = Decimal(str(f.celkom_s_dph or '0.00'))
                    except (InvalidOperation, TypeError, ValueError):
                        suma = Decimal('0.00')
                    # Výpočet reálneho doplatku so zohľadnením už prijatých čiastkových platieb
                    doteraz_uhradene = f.uhrady.aggregate(Sum('suma_uhrady'))['suma_uhrady__sum'] or Decimal('0.00') if hasattr(f, 'uhrady') else Decimal('0.00')
                    zostatok = abs(suma) - doteraz_uhradene
                    
                    if zostatok > Decimal('0.00'):
                        typ_dokladu_uhrady = 'VPD' if f.typ_dokladu == 'DOBROPIS' else 'PPD'
                        uhrady_na_vytvorenie.append(EvidenciaUhrad(
                            typ_uhrady=typ_dokladu_uhrady, 
                            suma_uhrady=zostatok,
                            vystavena_faktura=f,
                            datum_uhrady=timezone.now().date(),
                            mena=f.mena or get_firemna_mena(),
                            poznamka=f"Ručne označené ako zaplatené dispečerom dňa {timezone.now().strftime('%d.%m.%Y')}"
                        ))
                        count += 1
                        
                if uhrady_na_vytvorenie:
                    EvidenciaUhrad.objects.bulk_create(uhrady_na_vytvorenie)
                
                # 🛡️ ISAS ARCHITECT FIX: Faktúry sa uzavrú vždy, vrátane nulových (0.00) dokladov
                self.model.objects.filter(id__in=[f.id for f in faktury_na_uhradu]).update(
                    stav='ZAPLATENA',
                    datum_uhrady=timezone.now().date()
                )

            pocet_uzavretych = len(faktury_na_uhradu)
            return JsonResponse({"status": "success", "message": f"✅ Úspešne zaevidovaných {count} úhrad a uzavretých {pocet_uzavretych} faktúr!"})
        
        elif action_type == "mark_sent":
            from .tasks import vygeneruj_pdf_a_notifikuj_fakturu_task
            
            count = 0
            base_url = request.build_absolute_uri('/')[:-1]
            
            with transaction.atomic():
                # 🛡️ Povolíme odoslanie aj pre už odoslané doklady (okrem STORNO)
                faktury_na_odoslanie = self.model.objects.select_for_update().filter(
                    id__in=valid_ids
                ).exclude(stav='STORNO')
                if not faktury_na_odoslanie.exists():
                    return JsonResponse({"status": "error", "message": "Zvolené faktúry nie je možné odoslať (sú stornované). 🛑"})
                faktury_na_update = []
                
                for f in faktury_na_odoslanie:
                    if f.stav == 'DRAFT':
                        try:
                            f.schvalit_a_vystavit(request.user)
                        except ValidationError as e:
                            err_msg = "; ".join(e.messages) if hasattr(e, 'messages') else str(e)
                            return JsonResponse({
                                "status": "error",
                                "message": f"Faktúru {f.cislo_faktury or f.id} nie je možné schváliť: {err_msg}"
                            }, status=400)

                    f.stav = 'ODOSLANA'

                    # Ak eFaktúra ešte nebola úspešná (SENT), okamžite nastavíme PROCESSING (poller nezamrzne a netreba F5)
                    def _je_valid_id(val):
                        return bool(val and str(val).strip().lower() not in ['none', 'null', ''])

                    je_b2b = (
                        _je_valid_id(f.odberatel_ico) or _je_valid_id(getattr(f.zakaznik, 'ico', None)) or
                        _je_valid_id(f.odberatel_dic) or _je_valid_id(getattr(f.zakaznik, 'dic', None)) or
                        _je_valid_id(f.odberatel_ic_dph) or _je_valid_id(getattr(f.zakaznik, 'ic_dph', None))
                    )
                    if je_b2b and f.typ_dokladu != 'ZALOHA' and f.peppol_status != 'SENT':
                        f.peppol_status = 'PROCESSING'
                        f.peppol_error_log = None

                    faktury_na_update.append(f)
                    
                    def _dispatch_mark_sent(f_id=f.id, url=base_url):
                        try:
                            vygeneruj_pdf_a_notifikuj_fakturu_task.delay(f_id, url, True)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo odoslanie faktúry #{f_id} do Celery: {exc}")
                            messages.warning(request, f"⚠️ Faktúra #{f_id} bola pripravená, ale odoslanie zlyhalo pri zaradení do fronty (výpadok Celery/Redis).")
                    transaction.on_commit(_dispatch_mark_sent)
                    count += 1
                
                if faktury_na_update:
                    self.model.objects.bulk_update(faktury_na_update, ['stav', 'peppol_status', 'peppol_error_log'])
                    
            return JsonResponse({"status": "success", "message": f"✅🚀 {count} faktúr sa generuje a odosiela na pozadí."})

        elif action_type == "mark_storno":
            with transaction.atomic():
                # 🛑 ZÁKONNÁ OCHRANA: Faktúry odoslané do štátnej eFaktúry sa nesmú stornovať (vyžaduje sa Dobropis)
                odoslane_peppol = self.model.objects.filter(id__in=valid_ids, peppol_status__in=['SENT', 'PROCESSING'])
                if odoslane_peppol.exists():
                    cisla = ", ".join([f.cislo_faktury or f"ID #{f.id}" for f in odoslane_peppol])
                    return JsonResponse({
                        "status": "error", 
                        "message": f"Faktúry ({cisla}) už boli odoslané do siete Peppol / eFaktúra. Zákon zakazuje ich stornovanie. Na ich zrušenie musíte vystaviť Dobropis! 🛑"
                    }, status=400)

                faktury_na_storno = self.model.objects.select_for_update().filter(id__in=valid_ids).exclude(stav='STORNO')
                pocet = faktury_na_storno.update(stav='STORNO')
            return JsonResponse({"status": "success", "message": f"❌ {pocet} faktúr bolo úspešne stornovaných (čísla zostali zachované)."})

        elif action_type == "send_peppol":
            from .tasks import odosli_efakturu_peppol_task
            
            count = 0
            with transaction.atomic():
                # 🛑 ZÁKONNÁ OCHRANA: Už úspešne doručené eFaktúry (SENT) je prísne zakázané posielať štátu duplicitne
                uz_dorucene = self.model.objects.filter(id__in=valid_ids, peppol_status='SENT')
                if uz_dorucene.exists():
                    cisla = ", ".join([f.cislo_faktury or f"ID #{f.id}" for f in uz_dorucene])
                    return JsonResponse({
                        "status": "error", 
                        "message": f"Faktúra ({cisla}) už bola úspešne odoslaná štátu (SENT). Zákon zakazuje jej opätovné odoslanie! 🛑"
                    }, status=400)

                faktury_na_peppol = self.model.objects.select_for_update().filter(
                    id__in=valid_ids
                ).exclude(stav='DRAFT')
                
                if not faktury_na_peppol.exists():
                    return JsonResponse({"status": "error", "message": "Zvolené faktúry sú v stave DRAFT. Najprv ich musíte vystaviť. 🛑"})

                faktury_na_update = []

                def _je_valid_id(val):
                    return bool(val and str(val).strip().lower() not in ['none', 'null', ''])

                for f in faktury_na_peppol:
                    je_b2b = (
                        _je_valid_id(f.odberatel_ico) or _je_valid_id(getattr(f.zakaznik, 'ico', None)) or
                        _je_valid_id(f.odberatel_dic) or _je_valid_id(getattr(f.zakaznik, 'dic', None)) or
                        _je_valid_id(f.odberatel_ic_dph) or _je_valid_id(getattr(f.zakaznik, 'ic_dph', None))
                    )
                    if not je_b2b or f.typ_dokladu == 'ZALOHA':
                        if f.typ_dokladu == 'ZALOHA': dovod = "Zálohová faktúra (Nedaňový doklad)"
                        else: dovod = "B2C (Koncový spotrebiteľ)"
                        return JsonResponse({"status": "error", "message": f"Faktúru {f.cislo_faktury} nie je možné odoslať do Peppolu. Režim nepodporovaný pre: {dovod}. 🛑"}, status=400)

                    f.peppol_status = 'PROCESSING'
                    f.peppol_error_log = None
                    faktury_na_update.append(f)

                    def _dispatch_peppol_send(f_id=f.id):
                        try:
                            odosli_efakturu_peppol_task.delay(f_id)
                        except Exception as exc:
                            logger.warning(f"Zlyhalo odoslanie eFaktúry #{f_id} do Peppol (Celery): {exc}")
                            messages.warning(request, f"⚠️ Faktúru #{f_id} sa nepodarilo odoslať do eFaktúry kvôli výpadku brokera (Celery/Redis).")
                    transaction.on_commit(_dispatch_peppol_send)
                    count += 1

            if faktury_na_update:
                self.model.objects.bulk_update(faktury_na_update, ['peppol_status', 'peppol_error_log'])
                    
            return JsonResponse({"status": "success", "message": f"🇪🇺 {count} faktúr sa práve odosiela do národného systému eFaktúra."})

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description='📄 Číslo & Dátum', ordering='-datum_vystavenia')
    def get_identifikacia_card(self, obj):
        cislo = "V BUILDERI 🏗️" if obj.stav == 'DRAFT' else obj.cislo_faktury
        
        img_url = None
        if obj.zakaznik and getattr(obj.zakaznik, 'domena', None):
            clean_domain = str(obj.zakaznik.domena).lower().strip()
            for prefix in ['https://', 'http://', 'www.']:
                if clean_domain.startswith(prefix):
                    clean_domain = clean_domain[len(prefix):]
            
            clean_domain = clean_domain.split('/')[0].split('?')[0].split('#')[0]
            
            if '@' in clean_domain:
                clean_domain = clean_domain.split('@')[-1]
                
            if '.' in clean_domain and len(clean_domain) > 3:
                img_url = f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}"

        stavy = {
            'DRAFT': ('purple', '🏗️ ROZPRACOVANÁ'),
            'NACHYSTANA': ('info', '📦 NACHYSTANÁ'),
            'ODOSLANA': ('warning', '📤 ODOSLANÁ'),
            'ZAPLATENA': ('success', '✅ ZAPLATENÁ'),
            'PO_SPLATNOSTI': ('danger', '🚨 PO SPLATNOSTI'),
            'STORNO': ('neutral', '❌ STORNOVANÁ'),
        }
        type_color, text = stavy.get(obj.stav, stavy['NACHYSTANA'])
        badge_html = render_isas_badge(text, type_color=type_color, wrapper=False)

        typ_map = {
            'FAKTURA': ('info', '🧾 FAKTÚRA'),
            'ZALOHA': ('purple', '⏳ ZÁLOHA'),
            'DOBROPIS': ('danger', '↩️ DOBROPIS'),
        }
        typ_color, typ_text = typ_map.get(obj.typ_dokladu, ('neutral', obj.get_typ_dokladu_display()))
        badge_typ = render_isas_badge(typ_text, type_color=typ_color, wrapper=False)
        badges_row = render_isas_layout(format_html('{}{}', badge_html, badge_typ), layout="row", gap=4, center=True)
        
        datum_str = obj.datum_vystavenia.strftime('%d.%m.%Y') if obj.datum_vystavenia else "N/A"
        subtitle = f"📅 Vystavené: {datum_str}"
        
        avatar = render_isas_avatar(
            icon="📄", 
            title=cislo, 
            subtitle=subtitle, 
            bg_gradient="#3b82f6, #1d4ed8", 
            shadow_color="rgba(59, 130, 246, 0.25)", 
            extra_html=badges_row, 
            image_url=img_url
        )
        
        return avatar

    @admin.display(description='🏢 Zákazník & Zariadenia', ordering='zakaznik__Nazov_spolocnosti')
    def get_odberatel_card(self, obj):
        nazov_firmy = obj.odberatel_nazov if obj.odberatel_nazov else (obj.zakaznik.Nazov_spolocnosti if obj.zakaznik and obj.zakaznik.Nazov_spolocnosti else 'Neznámy')
        ico = obj.odberatel_ico or (obj.zakaznik.ico if obj.zakaznik and obj.zakaznik.ico else 'Bez IČO')
        
        zariadenia_nazvy = set()
        
        def pridaj_stroj(zar):
            if zar:
                nazov = getattr(zar, 'model_zariadenia', '') or getattr(zar, 'nazov', '') or str(zar)
                if nazov and str(nazov).strip():
                    zariadenia_nazvy.add(str(nazov).strip())

        pridaj_stroj(obj.zariadenie)
        for polozka in obj.polozky.all():
            pridaj_stroj(polozka.zariadenie)
                
        stroj_badges = []
        for nazov in zariadenia_nazvy:
            stroj_badges.append(render_isas_badge(nazov, icon="⚙️", type_color="info", custom_bg="#e0e7ff", custom_text="#4338ca", custom_border="#c7d2fe"))
            
        stroje_html = render_isas_layout(mark_safe("".join(stroj_badges)), layout="row", gap=4, center=True, custom_style="flex-wrap: wrap; margin-top: 6px;") if stroj_badges else ""
        
        nazov_html = render_isas_text(f"🏢 {nazov_firmy}", size=13, color="#0f172a", weight=800)
        ico_html = render_isas_text(f"IČO: {ico}", size=10, color="#64748b", weight=800, custom_style="margin-top: 2px;")
        
        po_badge = ""
        if obj.cislo_objednavky_klienta:
            po_badge = render_isas_badge(f"PO: {obj.cislo_objednavky_klienta}", type_color="neutral", wrapper=False)
            
        meta_row = render_isas_layout(format_html('{}{}', ico_html, po_badge), layout="row", gap=6, center=True, custom_style="margin-top: 2px;") if po_badge else ico_html

        return render_isas_layout(format_html('{}{}{}', nazov_html, meta_row, stroje_html), layout="col", center=True)

    @admin.display(description='💰 Finančný Sumár', ordering='celkom_s_dph')
    def get_financie_card(self, obj):
        mena_fav = obj.mena or get_firemna_mena()
        if obj.prenos_danovej_povinnosti:
            kod_text = f" ({obj.kod_clenenia_dph})" if obj.kod_clenenia_dph else ""
            dph_val = f"Prenos daň. povinnosti{kod_text} 🔄"
            dph_color = "#ea580c"
        else:
            dph_val = f"+ DPH: {obj.celkom_dph or Decimal('0.00')} {mena_fav}"
            dph_color = "#64748b"
        
        return render_isas_mini_stat_card(
            top_label="Suma s DPH",
            top_val=f"{obj.celkom_s_dph or Decimal('0.00')} {mena_fav}",
            top_val_color="#3b82f6",
            bot_label="DPH Režim",
            bot_val=dph_val,
            bot_val_color=dph_color
        )

    @admin.display(description="🛠️ Typ servisu/práce")
    def zobraz_typ_servisu(self, obj):
        typy = obj.typ_servisu.all()
        
        if not typy:
            return render_isas_text("➖ Bez typu", muted=True)
            
        pills = [render_isas_badge(str(t), icon="🛠️", type_color="info") for t in typy]
        
        return render_isas_layout(mark_safe("".join(pills)), layout="row", gap=4, custom_style="flex-wrap: wrap;")

    @admin.display(description="🔗 Zdrojové doklady")
    def zobraz_zdroje(self, obj):
        badges = []
        if obj.zdrojova_uloha_id:
            url = reverse('admin:Evidencia_uloha_change', args=[obj.zdrojova_uloha_id])
            badges.append(render_isas_link_btn(url, f"Úloha #{obj.zdrojova_uloha_id}", icon="📋", type_color="info", wrapper=False))
        for p in obj.zdrojove_ponuky.all():
            url = reverse('admin:Evidencia_cenovaponuka_change', args=[p.id])
            badges.append(render_isas_link_btn(url, p.cislo_ponuky, icon="💰", type_color="neutral", wrapper=False))
        for o in obj.zdrojove_objednavky.all():
            url = reverse('admin:Evidencia_prijataobjednavka_change', args=[o.id])
            badges.append(render_isas_link_btn(url, o.cislo_objednavky_interne, icon="📥", type_color="warning", wrapper=False))
        if not badges:
            return render_isas_text("—", muted=True)
        return render_isas_layout(mark_safe("".join(badges)), layout="col", gap=2, center=True)

    @admin.display(description="👤 Vystavil")
    def zobraz_vystavovatela(self, obj):
        
        if not obj.vystavil:
            return render_isas_text("➖ Neznámy", muted=True)
            
        bezpecne_meno = obj.vystavil.get_full_name() or obj.vystavil.username
        
        try:
            app_label = obj.vystavil._meta.app_label
            model_name = obj.vystavil._meta.model_name
            url = reverse(f'admin:{app_label}_{model_name}_change', args=[obj.vystavil.id])
            return render_isas_link_btn(url, bezpecne_meno, icon="👤", type_color="purple")
        except NoReverseMatch:
            return render_isas_badge(bezpecne_meno, icon="👤", type_color="purple", custom_style="padding: 6px 14px; font-size: 13px;")

    @admin.display(description='⚙️ Akcie & eFaktúra')
    def get_akcie_card(self, obj):
        if obj.stav == 'DRAFT':
            try:
                url = reverse('evidencia:faktura_builder', args=[obj.pk])
                btn = render_isas_link_btn(
                    url=url, 
                    text="OTVORIŤ BUILDER", 
                    icon="🏗️", 
                    type_color="purple", 
                    custom_style="background: linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%); color: white; border: none; box-shadow: 0 4px 12px rgba(139, 92, 246, 0.4);"
                )
                return render_isas_layout(btn, layout="col", center=True)
            except Exception:
                return render_isas_text("-")

        peppol_status = getattr(obj, 'peppol_status', 'NEW')
        peppol_html = ""
        if peppol_status == 'PROCESSING':
            peppol_html = render_isas_badge("eFaktúra: Odosiela sa...", icon="🇪🇺", suffix_html='<span style="animation: pulse 2s infinite;">⏳</span>', type_color="warning", custom_style="margin-bottom: 6px;")
        elif peppol_status == 'SENT':
            peppol_html = render_isas_badge("eFaktúra: OK ✅", icon="🇪🇺", type_color="success", tooltip="Úspešne doručené do siete Peppol", custom_style="margin-bottom: 6px;")
        elif peppol_status == 'ERROR':
            peppol_html = render_isas_badge("eFaktúra: CHYBA ❌", icon="🇪🇺", type_color="danger", tooltip="Chyba: Pozrite detail faktúry", custom_style="margin-bottom: 6px;")

        buttons = []
        
        # 🚀 ISAS FIX: Poller musí bežať, ak chýba PDF, XML ALEBO ak sa Peppol práve spracováva!
        is_pdf_missing = not bool(obj.pdf_dokument)
        has_xml = bool(getattr(obj, 'peppol_xml_dokument', None) and obj.peppol_xml_dokument.name)
        is_peppol_processing = (peppol_status == 'PROCESSING')
        
        # 🛡️ DEFENSIVE ARCHITECT FIX: Bezpečná detekcia B2B očistená aj od textových stringov 'None' / 'null'
        def _je_valid(val):
            return bool(val and str(val).strip().lower() not in ['none', 'null', ''])

        je_b2b = (
            _je_valid(obj.odberatel_ico) or _je_valid(getattr(obj.zakaznik, 'ico', None)) or
            _je_valid(getattr(obj, 'odberatel_dic', None)) or _je_valid(getattr(obj.zakaznik, 'dic', None)) or
            _je_valid(getattr(obj, 'odberatel_ic_dph', None)) or _je_valid(getattr(obj.zakaznik, 'ic_dph', None))
        )
        caka_na_xml = (not has_xml) and (is_peppol_processing or (is_pdf_missing and je_b2b and obj.typ_dokladu != 'ZALOHA'))
        
        status_code = "PROCESSING" if (is_pdf_missing or caka_na_xml or is_peppol_processing) else "DONE"
        
        if obj.pdf_dokument:
            buttons.append(render_isas_link_btn(obj.pdf_dokument.url, "PDF", icon="📄", type_color="neutral", custom_style="color: #e11d48;"))
        else:
            poller_badge = render_isas_badge("⏳ PDF...", type_color="warning", custom_style="animation: pulse 2s infinite; border-style: dashed;")
            buttons.append(poller_badge)

        if has_xml:
            buttons.append(render_isas_link_btn(obj.peppol_xml_dokument.url, "XML", icon="📄", type_color="neutral", target="_blank", custom_style="color: #0284c7;", tooltip="Zobraziť XML pre audit"))
        elif caka_na_xml:
            poller_xml_badge = render_isas_badge("⏳ XML...", type_color="warning", custom_style="animation: pulse 2s infinite; border-style: dashed;")
            buttons.append(poller_xml_badge)

        buttons_inner = render_isas_layout(mark_safe("".join(buttons)), layout="row", gap=6, center=True, custom_style="flex-wrap: wrap;")
        buttons_html = render_isas_layout(buttons_inner, layout="row", center=True, custom_style="display: inline-block;")

        return render_isas_layout(
            format_html('{}{}', peppol_html, buttons_html),
            layout="col",
            center=True,
            html_id=f"isas-pdf-badge-{obj.id}",
            css_class="isas-erp-status-badge",
            data_attrs={"status": status_code}
        )      

class ZodpovednaOsobaFilter(admin.SimpleListFilter):
    title = '👤 Zodpovedná osoba'
    parameter_name = 'veduci_stav'

    def lookups(self, request, model_admin):
        return (
            ('ma', '🧑‍💼 Má priradeného vedúceho'),
            ('nema', '🚫 Bez vedúceho'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'ma':
            # Má buď prepojeného zamestnanca (FK) alebo ručne vypísaného vedúceho
            return queryset.filter(Q(zodpovedny_zamestnanec__isnull=False) | ~Q(veduci_skladu__exact=''))
        if self.value() == 'nema':
            # Nemá ani FK, ani ručne vypísaného vedúceho
            return queryset.filter(Q(zodpovedny_zamestnanec__isnull=True) & (Q(veduci_skladu__exact='') | Q(veduci_skladu__isnull=True)))

@admin.register(Filialka)
class FilialkaAdmin(ISASPremiumAdmin):
    list_display = (
        "get_kod_pobocky_card",
        "get_rich_identity", 
        "styled_typ_skladu", 
        "get_veduci_card", 
        "status_badge", 
        "zobraz_stav_zasob", 
        "akcia_presunut_tovar"
    )
    list_display_links = ("get_rich_identity",)
    list_select_related = ('zodpovedny_zamestnanec',)    
    search_fields = ("nazov", "kod_pobocky", "adresa", "veduci_skladu")
    autocomplete_fields = ['zodpovedny_zamestnanec', 'priradeni_zamestnanci']    
    list_filter = (
        'je_aktivna', 
        'typ_skladu', 
        ZodpovednaOsobaFilter, 
        ('poznamka', admin.EmptyFieldListFilter)
    )   

    fieldsets = (
        ('🏢 ZÁKLADNÁ IDENTIFIKÁCIA', {
            'fields': (
                ('nazov', 'kod_pobocky'),
                'typ_skladu'
            ),
            'description': 'Základné údaje o pobočke. Kód pobočky slúži na rýchlu identifikáciu v systéme a pri skenovaní. 🏷️'
        }),
        ('📍 LOKALITA A ZODPOVEDNÁ OSOBA', {
            'fields': (
                'zodpovedny_zamestnanec',
                ('veduci_skladu', 'telefon'),
                ('email', 'adresa'),
                'priradeni_zamestnanci',
            ),
            'description': 'Kde sa sklad nachádza a kto zaň zodpovedá. 🔒 Ak vyberiete zamestnanca zo systému, kontaktné údaje sa automaticky uzamknú a prevezmú z jeho profilu.'
        }),
        ('⚙️ STATUS A POZNÁMKY', {
            'fields': (
                'je_aktivna',
                'poznamka'
            ),
            'description': '⚠️ Ak sa sklad zruší, nevymazávajte ho kvôli zachovaniu účtovnej histórie. Iba ho deaktivujte. ⏸️'
        }),
    )

    isas_title = "🏢 Sklady - filiálky"
    isas_preloader_text = "⏳ Vstupujete do zoznamu skladov - filiálok..."
    isas_clean_fk_fields = {'zodpovedny_zamestnanec': '👤 Vyberte používateľa'}
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "🏢",
    }

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<int:filialka_id>/zobrazi-stav-skladu/', self.admin_site.admin_view(self.zobraz_stav_skladu_view), name='filialka_stav_skladu'),
            path('<int:filialka_id>/logistika/', self.admin_site.admin_view(self.logistika_view), name='filialka_logistika'),
        ]
        return custom_urls + urls

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'get_employee_detail':
            if not request.user.is_staff:
                return JsonResponse({"status": "error", "message": "⛔ Prístup odmietnutý"}, status=403)

            if not valid_ids:
                return JsonResponse({"status": "error", "message": "Chýba identifikátor zamestnanca."}, status=400)

            ZamestnanecModel = self.model._meta.get_field('zodpovedny_zamestnanec').related_model
            try:
                z = ZamestnanecModel.objects.get(id=valid_ids[0])
                meno_priezvisko = f"{getattr(z, 'meno', '')} {getattr(z, 'priezvisko', '')}".strip()
                return JsonResponse({
                    "status": "success",
                    "veduci_skladu": meno_priezvisko,
                    "telefon": getattr(z, 'telefon', '') or '',
                    "email": getattr(z, 'email', '') or ''
                })
            except ZamestnanecModel.DoesNotExist:
                return JsonResponse({"status": "error", "message": "❌ Zamestnanec nenájdený"}, status=404)
                
        if action_type == 'deactivate':
            if not request.user.has_perm('Evidencia.change_filialka'):
                return JsonResponse({"status": "error", "message": "❌ Nemáte oprávnenie upravovať sklady."}, status=403)

            self.model.objects.filter(id__in=valid_ids).update(je_aktivna=False)
            
            count = len(valid_ids)
            word = "pobočka bola úspešne deaktivovaná ⏸️" if count == 1 else ("pobočky boli úspešne deaktivované ⏸️" if 2 <= count <= 4 else "pobočiek bolo úspešne deaktivovaných ⏸️")
            return JsonResponse({"status": "success", "message": f"✅ {count} {word}!"})
            
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    isas_ajax_autofill = {
        'trigger_field': 'zodpovedny_zamestnanec',
        'fast_action_type': 'get_employee_detail',
        'mapping': {
            'veduci_skladu': 'veduci_skladu',
            'telefon': 'telefon',
            'email': 'email'
        }
    }
    isas_row_styles = [
        {
            'contains_text': 'ZRUŠENÁ',
            'styles': {'opacity': '0.4', 'filter': 'grayscale(100%)'},
            'hover_opacity': '0.7',
            'disable_links_with_text': ['Zásoby', 'Logistika']
        }
    ]

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(nazov__icontains=term) | 
            Q(kod_pobocky__icontains=term) |
            Q(adresa__icontains=term) |
            Q(veduci_skladu__icontains=term)
        ).order_by('nazov')[:30]

    def format_detective_result(self, obj):
        nazov_skladu = obj.nazov or "Neznámy sklad"
        kod_str = f" [{obj.kod_pobocky}]" if obj.kod_pobocky else ""
        cele_meno = f"{nazov_skladu}{kod_str}"
        adresa_alebo_veduci = obj.adresa or obj.veduci_skladu or "Bez adresy"

        return {
            'id': obj.id,
            'name': cele_meno,
            'search_value': f"{nazov_skladu} {obj.kod_pobocky or ''}".strip(),
            'rola': adresa_alebo_veduci,
            'cat': '🏢 FILIÁLKA / SKLAD',
            'date': 'AKTÍVNA' if obj.je_aktivna else 'ZRUŠENÁ',
            'suma': obj.get_typ_skladu_display()
        }

    @admin.display(description='🏢 Pobočka / Sklad', ordering='nazov')
    def get_rich_identity(self, obj):
        emoji = "🏢"
        if obj.typ_skladu == 'AUTO': emoji = "🚐"
        elif obj.typ_skladu == 'KONTAJNER': emoji = "🏗️"
        elif obj.typ_skladu == 'INE': emoji = "📦"

        bg_grad = "#3b82f6, #1d4ed8" if obj.je_aktivna else "#ef4444, #b91c1c"
        shadow = "rgba(59, 130, 246, 0.25)" if obj.je_aktivna else "rgba(239, 68, 68, 0.25)"
        nazov = obj.nazov if obj.nazov else "Neznámy sklad"
        adresa = obj.adresa if obj.adresa else None

        return render_isas_avatar(
            icon=emoji,
            title=nazov,
            subtitle=adresa,
            bg_gradient=bg_grad,
            shadow_color=shadow
        )

    @admin.display(description='🏷️ Kód pobočky', ordering='kod_pobocky')
    def get_kod_pobocky_card(self, obj):
        if not obj.kod_pobocky:
            return render_isas_text("Nezadaný", icon="🚫", color="#cbd5e1", weight=800)

        return render_isas_badge(
            obj.kod_pobocky, 
            icon="📋", 
            type_color="neutral", 
            copy_value=obj.kod_pobocky,
            wrapper=True
        )

    @admin.display(description='⚙️ Typ', ordering='typ_skladu')
    def styled_typ_skladu(self, obj):
        if obj.typ_skladu == 'BUDOVA': 
            return render_isas_badge("Budova", icon="🏢", type_color="info", wrapper=True)
        elif obj.typ_skladu == 'AUTO': 
            return render_isas_badge("Auto", icon="🚐", type_color="warning", wrapper=True)
        elif obj.typ_skladu == 'KONTAJNER': 
            return render_isas_badge("Kontajner", icon="🏗️", type_color="purple", wrapper=True)
        return render_isas_badge("Iné", icon="📦", type_color="neutral", wrapper=True)

    @admin.display(description='👤 Vedúci / Kontakt', ordering='veduci_skladu')
    def get_veduci_card(self, obj):
        if obj.zodpovedny_zamestnanec:
            meno_text = render_isas_text(str(obj.zodpovedny_zamestnanec), icon="👷‍♂️", weight=800, color="#0284c7", size=12)
            tel_text = render_isas_badge(obj.telefon, icon="📞", copy_value=obj.telefon, wrapper=False) if obj.telefon else ""
            return render_isas_layout(format_html('{}{}', meno_text, tel_text), layout="col", gap=4, center=True)
            
        if not obj.veduci_skladu:
            return render_isas_text("Bez vedúceho", icon="🚫", color="#cbd5e1", weight=800)

        meno_text = render_isas_text(obj.veduci_skladu, icon="🧑‍💼", weight=800, color="#334155", size=12)
        tel_text = render_isas_badge(obj.telefon, icon="📞", copy_value=obj.telefon, wrapper=False) if obj.telefon else ""
        return render_isas_layout(format_html('{}{}', meno_text, tel_text), layout="col", gap=4, center=True)

    @admin.display(description='📊 Stav zásob')
    def zobraz_stav_zasob(self, obj):
        try:
            url = reverse('admin:filialka_stav_skladu', args=[obj.pk])
        except Exception:
            url = "#"
            
        return render_isas_link_btn(
            url=url,
            text="Zásoby",
            icon="📦",
            type_color="warning",
            wrapper=True
        )

    @admin.display(description='🚀 Stav', ordering='je_aktivna')
    def status_badge(self, obj):
        if obj.je_aktivna:
            return render_isas_badge("AKTÍVNA", icon="🟢", type_color="success", wrapper=True)
        return render_isas_badge("ZRUŠENÁ", icon="❌", type_color="danger", wrapper=True)

    @admin.display(description="🗺️ Presun")
    def akcia_presunut_tovar(self, obj):
        if not obj.je_aktivna:
            return render_isas_badge(
                "Zrušené", 
                icon="⏸️", 
                type_color="neutral", 
                wrapper=True
            )

        request = None
        for frame_record in inspect.stack():
            if 'request' in frame_record[0].f_locals:
                request = frame_record[0].f_locals['request']
                break

        if request and not (request.user.has_perm('Evidencia.change_stockmovement') or request.user.is_superuser):
            return render_isas_badge(
                "Zamknuté", 
                icon="🔒", 
                type_color="danger", 
                tooltip="Nemáte oprávnenie na presun",
                wrapper=True
            )

        try:
            url = reverse('admin:filialka_logistika', args=[obj.pk])
        except NoReverseMatch:
            url = "#"

        return render_isas_link_btn(
            url=url,
            text="Logistika",
            icon="🚛",
            type_color="info",
            wrapper=True
        )
    
    def zobraz_stav_skladu_view(self, request, filialka_id):        
        filialka = get_object_or_404(Filialka, pk=filialka_id)
        
        # Ochrana proti IDOR (Broken Access Control)
        if not self.has_view_permission(request, filialka):
            raise PermissionDenied("🚫 Nemáte oprávnenie na prezeranie stavu tohto skladu.")
            
        dynamic_json = get_sklad_dynamic_js_config()
        
        kat = request.GET.get('kat', '')
        sub = request.GET.get('sub', '')  # napr. kľúč podkategórie skladu
        
        stav_poloziek = []
        
        # 1. 🟢 NÁJDEME PRESNÝ MODEL Z MAPY
        target_model = None
        if sub:
            for kategoria, podkategorie in CATEGORY_MAP.items():
                for display_name, model_cls in podkategorie.items():
                    if model_cls.__name__.lower() == sub.lower():
                        target_model = model_cls
                        break
                if target_model:
                    break
                    
        # 2. 🟢 100% BEZPEČNÁ A OPTIMALIZOVANÁ LOGIKA (Bez N+1)
        if target_model:
            
            # Týmto sme zlikvidovali akékoľvek riziko N+1 a znížili pamäťovú náročnosť na 1 jediný O(1) dopyt!
            aggr = StockMovement.objects.filter(filialka=filialka).values('product_name', 'merna_jednotka').annotate(
                in_qty=Coalesce(Sum('count', filter=Q(action='ADD')), 0, output_field=IntegerField()),
                out_qty=Coalesce(Sum('count', filter=Q(action='REMOVE')), 0, output_field=IntegerField())
            )
            
            for item in aggr:
                zostatok = item['in_qty'] - item['out_qty']
                if zostatok > 0:
                    stav_poloziek.append({
                        'nazov': item['product_name'],
                        'pocet': zostatok,
                        'jednotka': item['merna_jednotka']
                    })
                    
        # Zoradíme abecedne priamo v Pythone (Rýchlejšie ako v DB pri malých datasetoch)
        stav_poloziek = sorted(stav_poloziek, key=lambda x: x['nazov'])
        
        # 3. 🟢 ODOVZDANIE DO KONTEXTU
        context = dict(
            self.admin_site.each_context(request),
            opts=Filialka._meta,      # Ochrana pre Django Hlavičku (Prevencia NoReverseMatch)
            original=filialka,        # Dynamický názov pobočky do nadpisu HTML
            has_view_permission=True,
            
            filialka=filialka,
            stav_poloziek=stav_poloziek, 
            sklad_config_json=dynamic_json,
            kat=kat,
            sub=sub,
            title=f"🏢 {filialka.nazov}",
        )
        
        return render(request, 'admin/stav_skladu.html', context)

    def logistika_view(self, request, filialka_id):
        
        # Hardcore firewall pre logistiku
        if not request.user.has_perm('Evidencia.change_stockmovement') and not request.user.is_superuser:
            raise PermissionDenied("🚫 Nemáte oprávnenie na medziskladové presuny tovaru.")
        
        # Podpora pre globálne tlačidlo z Hlavného skladu (ID 0)
        if filialka_id == 0:
            filialka = Filialka.objects.filter(je_aktivna=True).first()
            if not filialka:
                messages.error(request, "Pre medziskladový presun musíte mať vytvorenú aspoň jednu aktívnu filiálku.")
                return redirect('admin:Evidencia_filialka_changelist')
        else:
            filialka = get_object_or_404(Filialka, pk=filialka_id)
        
        # Získanie povolených filiálok pre roletky
        is_admin = request.user.is_superuser
        zamestnanec = getattr(request.user, 'zamestnanec', None)

        if is_admin:
            dostupne_filialky = Filialka.objects.filter(je_aktivna=True).order_by('nazov')
        elif zamestnanec:
            dostupne_filialky = Filialka.objects.filter(je_aktivna=True, priradeni_zamestnanci=zamestnanec).order_by('nazov')
        else:
            dostupne_filialky = Filialka.objects.none()
        
        # Vytiahneme názov hlavného skladu
        hlavny_sklad = Sklad.objects.first()
        nazov_hlavneho_skladu = f"Hlavný sklad ({hlavny_sklad.nazov})" if hlavny_sklad and hlavny_sklad.nazov and hlavny_sklad.nazov.lower() != "hlavný sklad" else "Hlavný sklad"
        
        # Pripravíme mapu kategórií pre JavaScript
        js_category_map = {}
        for main_cat, subcats in CATEGORY_MAP.items():
            js_category_map[main_cat] = list(subcats.keys())
            
        context = dict(
            self.admin_site.each_context(request),
            filialka=filialka,
            dostupne_filialky=dostupne_filialky,
            hlavny_sklad_nazov=nazov_hlavneho_skladu,
            kategorie=CATEGORY_CHOICES,
            js_category_map_json=json.dumps(js_category_map),
            title=f"🚛 Logistika: {filialka.nazov} 📦"
        )
        
        return render(request, 'admin/medziskladovy_presun.html', context)

@admin.register(PrijataObjednavka)
class PrijataObjednavkaAdmin(ISASTaskConversionAdminMixin, ISASInvoiceGeneratorAdminMixin, ISASSmartMatcherAdminMixin, ISASPremiumAdmin):
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related(
            'zakaznik', 'zodpovedny_zamestnanec'
        ).prefetch_related('zariadenia', 'typ_servisu', 'zdrojove_ponuky', 'vytvorene_ulohy')

    list_display = (
        'get_objednavka_card', 
        'get_zakaznik_card', 
        'get_sparovanie_status',
        'get_prepojenia_card',
        'get_dokument_card',
        'zobraz_ulohy'
    )    
    list_display_links = ('get_objednavka_card',)
    list_filter = (ISASDateRangeFilter, 'stav', 'forma_prijatia', ISASRelatedZakaznikFilter)
    isas_date_filter_field = 'datum_prijatia'
    search_fields = ('cislo_objednavky_interne', 'cislo_objednavky_klienta', 'zakaznik__Nazov_spolocnosti')
    readonly_fields = ('cislo_objednavky_interne',)
    filter_horizontal = ('zariadenia', 'typ_servisu')
    autocomplete_fields = ('zakaznik', 'zdrojove_ponuky', 'vytvorene_ulohy', 'navrhnute_ponuky', 'ignorovane_ponuky')

    fieldsets = (
        ('📥 Základná identifikácia', {
            'description': 'Hlavné údaje o prijatej objednávke. 🏢 Nezabudnite vyplniť PO Number klienta, je to kritické pre fakturáciu!',
            'fields': (
                ('cislo_objednavky_interne', 'cislo_objednavky_klienta'), 
                'zakaznik',
                'zodpovedny_zamestnanec'
            )
        }),
        ('🚥 Stav, Priorita a Termíny', {
            'description': 'Sledovanie životného cyklu a SLA. ⏱️ Havarijné stavy označte vysokou prioritou pre okamžité upozornenie technikov.',
            'fields': (
                ('stav', 'priorita'), 
                ('datum_prijatia', 'termin_dodania')
            )
        }),
        ('💰 Rozpočet a Fakturačné inštrukcie', {
            'description': 'Finančný rámec (Blanket Order) a B2B inštrukcie. 💰 Tieto údaje si Builder faktúr automaticky natiahne pri fakturácii.',
            'fields': (
                ('celkova_suma_bez_dph', 'mena'), 
                ('vystavil_za_klienta', 'specificky_email_pre_fakturu'), 
                'vyzadovana_splatnost_dni'
            )
        }),
        ('📄 Forma prijatia a Zdroj', {
            'description': 'Fyzický alebo digitálny dôkaz o objednávke. 🌐 Vyplňte to pole, ktoré zodpovedá forme prijatia (PDF, E-mail, Portál).',
            'fields': (
                'forma_prijatia', 
                'subor_objednavky', 
                'odkaz_na_portal', 
                'text_emailu'
            )
        }),
        ('🔗 Systémové prepojenia', {
            'description': 'Väzby na iné moduly v ISAS. ⚙️ Prepojte objednávku so zariadeniami, ponukami alebo vygenerovanou úlohou.',
            'fields': (
                'zariadenia', 
                'typ_servisu', 
                'zdrojove_ponuky',
                'vytvorene_ulohy' # 👈 ZMENENÉ
            )
        }),
        ('🤖 ISAS CORE Smart Matcher', {
            'description': 'Pamäť autonómneho agenta pre párovanie objednávok s ponukami. 🧠',
            'fields': (
                'navrhnute_ponuky',
                'ignorovane_ponuky'
            )
        }),
        ('📝 Poznámky', {
            'description': 'Doplňujúce informácie. 🔒 Interná poznámka sa nikdy nezobrazí zákazníkovi.',
            'fields': (
                'poznamka_klienta', 
                'interna_poznamka'
            )
        }),
    )

    isas_title = "📥 Prijaté objednávky"
    isas_preloader_text = "⏳ Vstupujete do zoznamu prijatých objednávok..."
    isas_clean_fk_fields = {'zodpovedny_zamestnanec': '👤 Vyberte používateľa'}
    isas_magia_kwargs = {
        "live_search_endpoint": "detective-search/",
        'live_search_ikona': "📥",
        'ignore_default_action_ids': ["btn-pair-quote", "btn-convert-task", "btn-merge-invoice"],
        'custom_buttons_html': mark_safe("""
        <button type="button" id="btn-pair-quote" class="isas-action-btn btn-brown">🔗 Spárovať s ponukou</button>
        <button type="button" id="btn-convert-task" class="isas-action-btn btn-green">⚙️ KONVERTOVAŤ NA ÚLOHU</button>
        <button type="button" id="btn-merge-invoice" class="isas-action-btn btn-blue">🧾 ZBERNÁ FAKTÚRA</button>
        """)
    }

    isas_action_modals = {
        'btn-merge-invoice': {
            'type': 'direct_action',
            'action': 'merge_invoice',
            'confirm': True,
            'confirm_title': 'Vytvorenie zbernej faktúry z objednávok 🧾',
            'confirm_text': 'Naozaj chcete vybrané prijaté objednávky zlúčiť a vystaviť z nich jednu zbernú faktúru?',
            'loading_text': '⏳ GENERUJEM...',
            'success_text': '✅ HOTOVO',
            'require_selection': True,
            'min_selection': 1,
            'error_msg': 'Najskôr zaškrtnite aspoň jednu prijatú objednávku zo zoznamu.',
            'reload': True
        },
        'btn-pair-quote': {
            'type': 'ajax_select_modal',
            'action': 'pair_quote',
            'title': 'Spárovať s Cenovou ponukou',
            'subtitle': 'Vyhľadajte cenovú ponuku podľa čísla alebo názvu zákazníka.',
            'icon': '🔗',
            'ajax_url': 'search-quotes-for-pairing/',
            'placeholder': '🔍 Zadajte číslo ponuky alebo firmu...',
            'confirm_text': '🔗 Spárovať',
            'require_selection': True,
            'min_selection': 1,
            'max_selection': 1,
            'error_msg': 'Na párovanie označte vždy presne jednu objednávku zo zoznamu.'
        },
        'btn-convert-task': {
            'type': 'convert_to_task',
            'action': 'convert_task',
            'require_selection': True,
            'min_selection': 1,
            'max_selection': 1,
            'error_msg': 'Na konverziu označte vždy presne jednu položku zo zoznamu.',
            'service_types_help_text': '💡 Tieto typy prác boli automaticky predvyplnené z podkladov. Môžete ich ľubovoľne upraviť, zmazať alebo doplniť pred vygenerovaním úlohy.'
        }
    }

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('search-quotes-for-pairing/', self.admin_site.admin_view(self.search_quotes_for_pairing), name='search-quotes-for-pairing'),
        ]
        return custom_urls + urls

    def search_quotes_for_pairing(self, request):
        """🔍 API pre Select2 modál: Vyhľadávanie cenových ponúk pre spárovanie s objednávkou."""
        if not request.user.has_perm('Evidencia.view_cenovaponuka'):
            return JsonResponse({'results': []}, status=403)
            
        term = request.GET.get('q', '').strip()
        
        qs = CenovaPonuka.objects.select_related('zakaznik').all()
        if term:
            qs = qs.filter(
                Q(cislo_ponuky__icontains=term) | 
                Q(zakaznik__Nazov_spolocnosti__icontains=term)
            )
            
        qs = qs.order_by('-datum_vystavenia')[:30]
        
        results = []
        for p in qs:
            zakaznik = escape(p.zakaznik.Nazov_spolocnosti) if p.zakaznik else "Neznámy zákazník"
            suma = f"{p.celkom_s_dph or Decimal('0.00')} {p.mena or get_firemna_mena()}"
            stav = p.get_stav_display()
            # Formátujeme text tak, aby dispečer videl VŠETKO dôležité
            text = f"📄 {p.cislo_ponuky} | 🏢 {zakaznik} | 💰 {suma} | {stav}"
            results.append({'id': p.id, 'text': text})
            
        return JsonResponse({'results': results})

    def get_smart_suggestions(self, request, obj=None):
        smart_suggestions = []
        navrhy = self.model.objects.filter(
            navrhnute_ponuky__isnull=False
        ).prefetch_related('navrhnute_ponuky').select_related('zakaznik').distinct()
        
        for o in navrhy:
            for ponuka in o.navrhnute_ponuky.all():
                mena_o = o.mena or get_firemna_mena()
                mena_p = ponuka.mena or get_firemna_mena()
                suma_obj = f"{o.celkova_suma_bez_dph} {mena_o}" if o.celkova_suma_bez_dph is not None else f"? {mena_o}"
                suma_pon = f"{ponuka.celkom_s_dph} {mena_p}" if ponuka.celkom_s_dph is not None else f"? {mena_p}"
                
                smart_suggestions.append({
                    'left_id': o.id,
                    'left_label': '📥 Objednávka',
                    'left_str': f"{o.cislo_objednavky_interne} | Suma: {suma_obj}",
                    'right_id': ponuka.id,
                    'right_label': '📄 Ponuka',
                    'right_str': f"{ponuka.cislo_ponuky} | Suma: {suma_pon}",
                    'dovod': "🤖 AI Agent vyhodnotil túto ponuku ako najlepšiu zhodu na základe textu, sumy a času."
                })
        return smart_suggestions

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type in ['pair_quote', 'ignore_match', 'smart_pair', 'smart_ignore']:
            if not request.user.has_perm('Evidencia.change_prijataobjednavka'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie upravovať prijaté objednávky."}, status=403)

        if action_type == 'pair_quote':
            try:
                data = json.loads(request.body) if request.body else {}
                quote_ids = data.get('quote_ids', [])
            except (json.JSONDecodeError, Exception):
                quote_ids = []
                
            if not quote_ids:
                return JsonResponse({"status": "error", "message": "🚫 Nebola vybraná žiadna ponuka na spárovanie."})
                
            with transaction.atomic():
                objednavky = PrijataObjednavka.objects.select_for_update().filter(id__in=valid_ids).order_by('id')
                for obj in objednavky:
                    obj.zdrojove_ponuky.add(*quote_ids)
                    obj.navrhnute_ponuky.remove(*quote_ids)
                    obj.ignorovane_ponuky.remove(*quote_ids)
            
            return JsonResponse({"status": "success", "message": "🔗 Objednávka bola úspešne spárovaná s ponukami!"})

        elif action_type == 'ignore_match':
            try:
                data = json.loads(request.body) if request.body else {}
                quote_ids = data.get('quote_ids', [])
            except (json.JSONDecodeError, Exception):
                quote_ids = []
                
            if valid_ids and quote_ids:
                with transaction.atomic():
                    try:
                        objednavka = PrijataObjednavka.objects.select_for_update().get(id=valid_ids[0])
                        objednavka.ignorovane_ponuky.add(*quote_ids)
                        objednavka.navrhnute_ponuky.remove(*quote_ids)
                    except PrijataObjednavka.DoesNotExist:
                        pass
                    
                return JsonResponse({"status": "success", "message": "🚫 Návrh bol trvalo ignorovaný."})

        elif action_type == 'convert_task':
            if len(valid_ids) != 1:
                return JsonResponse({"status": "error", "message": "Vyberte presne jednu objednávku."})
            try:
                data = json.loads(request.body) if request.body else {}
            except (json.JSONDecodeError, Exception):
                data = {}
            try:
                from .utils import ISASTaskConversionEngine
            except ImportError:
                from Evidencia.utils import ISASTaskConversionEngine
            return ISASTaskConversionEngine.convert_to_task(request, 'objednavka', valid_ids[0], data)

        elif action_type == 'merge_invoice':
            return self.generate_merge_invoice(request, self.model.objects.filter(id__in=valid_ids), 'objednavka')
            
        elif action_type == 'smart_pair':
            try:
                data = json.loads(request.body)
                left_id = data.get('left_id')
                right_id = data.get('right_id')
            except Exception:
                return JsonResponse({"status": "error", "message": "Neplatné dáta."}, status=400)

            if not left_id or not right_id:
                return JsonResponse({"status": "error", "message": "Chýbajúce identifikátory pre párovanie."}, status=400)
                
            with transaction.atomic():
                try:
                    objednavka = self.model.objects.select_for_update().get(id=left_id)
                    objednavka.zdrojove_ponuky.add(right_id)
                    objednavka.navrhnute_ponuky.remove(right_id)
                    objednavka.ignorovane_ponuky.remove(right_id)
                except self.model.DoesNotExist:
                    pass
            return JsonResponse({"status": "success", "message": "🔗 Úspešne spárované!"})

        elif action_type == 'smart_ignore':
            try:
                data = json.loads(request.body)
                left_id = data.get('left_id')
                right_id = data.get('right_id')
            except Exception:
                return JsonResponse({"status": "error", "message": "Neplatné dáta."}, status=400)

            if not left_id or not right_id:
                return JsonResponse({"status": "error", "message": "Chýbajúce identifikátory pre akciu."}, status=400)
                
            with transaction.atomic():
                try:
                    objednavka = self.model.objects.select_for_update().get(id=left_id)
                    objednavka.ignorovane_ponuky.add(right_id)
                    objednavka.navrhnute_ponuky.remove(right_id)
                except self.model.DoesNotExist:
                    pass
            return JsonResponse({"status": "success", "message": "🚫 Návrh bol ignorovaný."})

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('zakaznik').filter(
            Q(cislo_objednavky_interne__icontains=term) |
            Q(cislo_objednavky_klienta__icontains=term) |
            Q(zakaznik__Nazov_spolocnosti__icontains=term)
        ).order_by('-datum_prijatia')[:30]

    def format_detective_result(self, obj):
        zakaznik_nazov = getattr(obj.zakaznik, 'Nazov_spolocnosti', str(obj.zakaznik)) if obj.zakaznik else "Neznámy zákazník"
        po_number = f" ({obj.cislo_objednavky_klienta})" if obj.cislo_objednavky_klienta else ""
        zobrazeny_nazov = f"{obj.cislo_objednavky_interne}{po_number}"
        datum_str = obj.datum_prijatia.strftime('%d.%m.%Y') if obj.datum_prijatia else "Neznámy"
        mena_obj = obj.mena or get_firemna_mena()
        safe_suma = f"{obj.celkova_suma_bez_dph} {mena_obj}" if obj.celkova_suma_bez_dph is not None else "Neurčený"
        
        return {
            'id': obj.id, 
            'name': zobrazeny_nazov, 
            'search_value': zobrazeny_nazov,
            'rola': zakaznik_nazov,
            'date': datum_str,
            'cat': 'OBJEDNÁVKA',
            'suma': safe_suma
        }

    @admin.display(description='📥 Objednávka & Stav', ordering='datum_prijatia')
    def get_objednavka_card(self, obj):
        datum = obj.datum_prijatia.strftime('%d.%m.%Y') if obj.datum_prijatia else "Neznámy ❓"
        
        stav_map = {
            'NOVA': ('info', '🆕 Nová'),
            'SCHVALENA': ('success', '✅ Schválená'),
            'VYBAVENA': ('warning', '📦 Vybavená'),
            'VYFAKTUROVANA': ('purple', '🧾 Vyfakturovaná'),
            'ZAMIETNUTA': ('danger', '❌ Zamietnutá'),
        }
        type_color, stav_text = stav_map.get(obj.stav, ('neutral', 'Neznámy'))

        typy_data = [{'id': t.id, 'text': f"🛠️ {t.nazov}"} for t in obj.typ_servisu.all()]
        for p in obj.zdrojove_ponuky.all():
            for pt in p.typ_servisu.all():
                if pt.id not in [x['id'] for x in typy_data]:
                    typy_data.append({'id': pt.id, 'text': f"🛠️ {pt.nazov}"})
        
        hidden_data = render_isas_layout(
            content_html="",
            css_class="isas-row-service-types",
            data_attrs={'id': str(obj.id), 'typy': json.dumps(typy_data)},
            custom_style="display:none;"
        )

        priorita_html = ""
        if obj.priorita == 'VYSOKA':
            priorita_html = render_isas_badge("SLA HAVÁRIA", prefix_text="🚨", type_color="danger", wrapper=False, custom_style="animation: pulse 2s infinite; margin-left: 6px;")
        elif obj.priorita == 'STREDNA':
            priorita_html = render_isas_badge("Štandard", prefix_text="🟡", type_color="warning", wrapper=False, custom_style="margin-left: 6px;")
        elif obj.priorita == 'NIZKA':
            priorita_html = render_isas_badge("Plánované", prefix_text="🟢", type_color="success", wrapper=False, custom_style="margin-left: 6px;")

        id_text = render_isas_text(obj.cislo_objednavky_interne, size=14, weight=900, color="#0f172a")
        id_row = render_isas_layout(format_html('{}{}', id_text, priorita_html), layout="row", gap=0)

        if obj.cislo_objednavky_klienta:
            po_row = render_isas_text(f"PO: {obj.cislo_objednavky_klienta}", size=10, weight=700, color="#0f172a", custom_style="margin-top: 2px;")
        else:
            po_row = render_isas_text("⚠️ Chýba PO Number", size=10, weight=700, color="#ef4444", custom_style="margin-top: 2px;")

        date_text = render_isas_text(datum, icon="📅", size=11, weight=600, color="#64748b")
        status_badge = render_isas_badge(stav_text, type_color=type_color, wrapper=False)
        termin_badge = render_isas_badge(f"Dodanie: {obj.termin_dodania.strftime('%d.%m.%Y')}", icon="⏳", type_color="warning", wrapper=False) if obj.termin_dodania else ""
        bottom_row = render_isas_layout(format_html('{}{}{}', date_text, status_badge, termin_badge), layout="row", gap=6, center=True, custom_style="margin-top: 6px; flex-wrap: wrap;")

        return render_isas_layout(
            format_html('{}{}{}{}', hidden_data, id_row, po_row, bottom_row),
            layout="col", gap=0
        )

    @admin.display(description='🏢 Zákazník & Rozpočet', ordering='zakaznik__Nazov_spolocnosti')
    def get_zakaznik_card(self, obj):
        zakaznik_nazov = getattr(obj.zakaznik, 'Nazov_spolocnosti', str(obj.zakaznik))
        
        img_tag = ""
        if obj.zakaznik and getattr(obj.zakaznik, 'domena', None):
            clean_domain = str(obj.zakaznik.domena).lower().strip()
            for prefix in ['https://', 'http://', 'www.']:
                if clean_domain.startswith(prefix):
                    clean_domain = clean_domain[len(prefix):]
            
            clean_domain = clean_domain.split('/')[0].split('?')[0].split('#')[0]
            
            if '@' in clean_domain: 
                clean_domain = clean_domain.split('@')[-1]
                
            if '.' in clean_domain and len(clean_domain) > 3:
                img_tag = render_isas_image(f"https://www.google.com/s2/favicons?sz=64&domain={clean_domain}", width=32, height=32, custom_style="border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); flex-shrink: 0;")

        if not img_tag:
            emoji_text = render_isas_text("🏢", size=16, color="white")
            img_tag = render_isas_layout(
                emoji_text, layout="col", center=True,
                custom_style="width: 32px; height: 32px; background: linear-gradient(135deg, #3b82f6, #1d4ed8); border-radius: 8px; flex-shrink: 0; box-shadow: 0 2px 5px rgba(59, 130, 246, 0.25);"
            )
        
        mena_obj = obj.mena or get_firemna_mena()
        rozpocet = f"{obj.celkova_suma_bez_dph} {mena_obj}" if obj.celkova_suma_bez_dph is not None and obj.celkova_suma_bez_dph > 0 else "Neurčený"
        rozpocet_html = render_isas_text(f"💰 Rozpočet: {rozpocet}", size=11, weight=800, color="#10b981")
        
        metadata = [rozpocet_html]
        if obj.specificky_email_pre_fakturu or obj.vyzadovana_splatnost_dni:
            instrukcie = render_isas_badge("⚠️ Špecifické fakturačné inštrukcie", type_color="warning", wrapper=False)
            metadata.append(instrukcie)

        return render_isas_entity_card(icon=img_tag, title=zakaznik_nazov, metadata_list=metadata)

    @admin.display(description='🔗 Prepojenia')
    def get_prepojenia_card(self, obj):
        links = []
        
        ponuky_count = obj.zdrojove_ponuky.count()
        if ponuky_count > 0:
            links.append(render_isas_text(f"{ponuky_count} Ponúk", icon="📄", size=11, weight=700, color="#475569"))
            
        ulohy_count = obj.vytvorene_ulohy.count()
        if ulohy_count > 0:
            links.append(render_isas_text(f"{ulohy_count} Úloh", icon="📋", size=11, weight=700, color="#475569"))
        
        zariadenia_count = obj.zariadenia.count()
        if zariadenia_count > 0:
            links.append(render_isas_text(f"{zariadenia_count} Zariadení", icon="⚙️", size=11, weight=700, color="#475569"))

        if not links:
            return render_isas_text("Žiadne prepojenia", size=11, weight=600, color="#94a3b8")

        return render_isas_layout(mark_safe("".join(links)), layout="col", gap=4)

    @admin.display(description='🔗 Spárované Ponuky')
    def get_sparovanie_status(self, obj):
        ponuky = obj.zdrojove_ponuky.all()
        
        if not ponuky:
            empty_text = render_isas_text("❌ Nespárované", size=10, weight=800, color="#94a3b8", custom_style="text-transform: uppercase;")
            return render_isas_layout(
                empty_text, layout="col", center=True, bg_color="#f8fafc", border_color="#cbd5e1", 
                padding="6px 10px", border_radius="8px", min_width="110px", custom_style="border-style: dashed;"
            )
            
        html_items = []
        for p in ponuky:
            url = reverse('admin:Evidencia_cenovaponuka_change', args=[p.id])
            text = f"📄 {p.cislo_ponuky} | {p.celkom_s_dph or Decimal('0.00')} {p.mena or get_firemna_mena()}"
            html_items.append(render_isas_link_btn(url, text, type_color="info", wrapper=False, custom_style="white-space: nowrap;"))
            
        items_str = mark_safe("".join(html_items))
        title = render_isas_text("🔗 Spárované s:", size=9, weight=900, color="#1e3a8a", custom_style="text-transform: uppercase; letter-spacing: 0.5px;")
        
        return render_isas_layout(
            format_html('{}{}', title, render_isas_layout(items_str, layout="col", gap=4)),
            layout="col", gap=4, center=True, bg_color="#f0f9ff", border_color="#bae6fd", 
            padding="6px", border_radius="8px", min_width="110px"
        )

    @admin.display(description='🌐 Zdroj & Dokument')
    def get_dokument_card(self, obj):
        forma_ikony = {
            'SUBOR': '📄', 'EMAIL': '✉️', 'PORTAL': '🏢', 'WEB': '🌐', 'TELEFON': '📞'
        }
        ikona = forma_ikony.get(obj.forma_prijatia, '📥')
        
        forma_display = obj.get_forma_prijatia_display()
        forma_text = forma_display.split(' ')[1] if ' ' in forma_display else forma_display
        title = render_isas_text(forma_text, icon=ikona, size=11, weight=800, color="#475569")
        
        if obj.forma_prijatia == 'SUBOR' and obj.subor_objednavky:
            tlacidlo = render_isas_link_btn(obj.subor_objednavky.url, "Otvoriť PDF", type_color="danger", wrapper=False)
        elif obj.forma_prijatia == 'PORTAL' and obj.odkaz_na_portal:
            tlacidlo = render_isas_link_btn(obj.odkaz_na_portal, "Otvoriť Portál", type_color="info", wrapper=False)
        elif obj.forma_prijatia == 'WEB':
            tlacidlo = render_isas_badge("Web Dopyt", type_color="success", wrapper=False)
        elif obj.forma_prijatia == 'EMAIL' and obj.text_emailu:
            tlacidlo = render_isas_badge("Obsahuje Text", type_color="purple", wrapper=False)
        elif obj.forma_prijatia == 'TELEFON' and obj.text_emailu:
            tlacidlo = render_isas_badge("Ústna dohoda", icon="📞", type_color="warning", wrapper=False)
        elif obj.forma_prijatia == 'TELEFON':
            tlacidlo = render_isas_badge("Telefonicky", icon="📞", type_color="neutral", wrapper=False)
        else:
            tlacidlo = render_isas_text("❌ Bez prílohy", size=10, weight=600, color="#94a3b8", custom_style="text-transform: uppercase;")

        return render_isas_layout(format_html('{}{}', title, tlacidlo), layout="col", gap=6, center=True)

    @admin.display(description="📋 Vygenerované Úlohy")
    def zobraz_ulohy(self, obj):
        ulohy = obj.vytvorene_ulohy.all()
        
        if not ulohy.exists():
            return render_isas_text("➖ Zatiaľ bez úlohy", muted=True)
            
        badges = []
        for uloha in ulohy:
            url = reverse(f'admin:{uloha._meta.app_label}_{uloha._meta.model_name}_change', args=[uloha.id])
            badges.append(render_isas_link_btn(url=url, text=str(uloha), icon="📋", type_color="warning", wrapper=False))
            
        return render_isas_layout(mark_safe("".join(badges)), layout="col", gap=4, center=True)

    class Media:
        css = {
            'all': (
                'admin/css/vendor/select2/select2.css',
                'admin/css/autocomplete.css',
            )
        }
        js = (
            'admin/js/vendor/jquery/jquery.js',
            'admin/js/vendor/select2/select2.full.js',
            'admin/js/jquery.init.js',
        )

@admin.register(ImportDatTask)
class ImportDatTaskAdmin(ISASPremiumAdmin):
    list_display = (
        'get_hlavne_info_card',
        'get_stav_cas_card',
        'get_statistiky_card',
        'get_log_card'
    )   
    list_display_links = ('get_hlavne_info_card',)
    list_filter = (
        'stav', 
        'typ_importu', 
        'rezim_importu',
        ISASDateRangeFilter,
    )
    isas_date_filter_field = 'vytvorene'
    search_fields = ['chybovy_log', 'vytvoril__username']
    list_select_related = ('vytvoril',)    
    readonly_fields = [
        'stav', 'vytvorene', 'dokoncene', 'celkovy_pocet_riadkov', 
        'uspesne_importovane', 'preskocene_duplicity', 'chybne_riadky', 
        'chybovy_log', 'vzorove_subory_linky', 'velkost_suboru_kb'
    ]
    
    fieldsets = (
        ('📥 1. Príprava a stiahnutie vzoru', {
            'classes': ('tlacidlo-boxy',), # 👈 TOTO AKTIVUJE TVOJU CSS VÝNIMKU!
            'fields': ('vzorove_subory_linky',),
            'description': 'Pred nahratím dát si stiahnite správny vzorový súbor. ⚠️ Nemeňte názvy stĺpcov v prvom riadku, inak import zlyhá!'
        }),
        ('⚙️ 2. Nahratie dát a Režim', {
            'fields': (('typ_importu', 'rezim_importu'), 'subor', 'vytvoril'),
            'description': 'Vyberte, aké dáta idete importovať a či chcete len pridávať nové, alebo aj aktualizovať existujúce záznamy. 🚀'
        }),
        ('📊 3. Priebeh a Štatistiky (Generuje systém)', {
            'fields': (
                ('stav', 'velkost_suboru_kb'), 
                ('vytvorene', 'dokoncene'), 
                ('celkovy_pocet_riadkov', 'uspesne_importovane'), 
                ('preskocene_duplicity', 'chybne_riadky')
            ),
            'description': 'Tieto údaje sa vyplnia automaticky po dokončení importu na pozadí. 🤖'
        }),
        ('📝 4. Chybový denník', {
            'fields': ('chybovy_log',),
            'description': 'Ak niektoré riadky zlyhali, tu nájdete presný dôvod. 🔍'
        }),
    )

    isas_title = "📥 Import dát a informácii"
    isas_preloader_text = "⏳ Vstupujete do zoznamu importov dát a informácii..."
    isas_clean_fk_fields = {'vytvoril': '👤 Vyberte používateľa'}
    isas_auto_poll = True
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "📥",
    }

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('vytvoril').filter(
            Q(chybovy_log__icontains=term) |
            Q(vytvoril__username__icontains=term)
        )[:10]

    def format_detective_result(self, obj):
        typ_nazov = obj.get_typ_importu_display() if hasattr(obj, 'get_typ_importu_display') else obj.typ_importu
        nazov = f"Import {typ_nazov}"
        autor = obj.vytvoril.username if obj.vytvoril else "Systém"
        datum = obj.vytvorene.strftime('%d.%m.%Y %H:%M') if obj.vytvorene else ""
        
        return {
            'id': obj.id,
            'name': nazov,
            'search_value': nazov,
            'rola': f"👤 {autor}",
            'date': datum,
            'cat': "IMPORT"
        }

    @admin.display(description="📥 Vzorové súbory na stiahnutie")
    def vzorove_subory_linky(self, obj):
        if obj and obj.pk:
            return render_isas_text("✅ Súbor bol nahratý, overený a odoslaný na spracovanie.", size=12, color="#059669", weight=800)
            
        # 🤖 Dynamické a bezpečné získanie URL adries
        links_data = [
            (reverse('evidencia:stiahni_vzor_importu', args=['ZAKAZNICI']), "Zákazníci", "🏢"),
            (reverse('evidencia:stiahni_vzor_importu', args=['DODAVATELIA']), "Dodávatelia", "🏭"),
            (reverse('evidencia:stiahni_vzor_importu', args=['SUKROMNE_OSOBY']), "Súkromné osoby", "👤"),
            (reverse('evidencia:stiahni_vzor_importu', args=['ZAMESTNANCI']), "Zamestnanci", "👷"),
            (reverse('evidencia:stiahni_vzor_importu', args=['VOZIDLA']), "Vozidlá", "🚗"),
            (reverse('evidencia:stiahni_vzor_importu', args=['SKLAD_KARTY']), "Skladové karty", "📦"),
            (reverse('evidencia:stiahni_vzor_importu', args=['TYPY_SERVISU']), "Typy servisu", "🛠️"),
            (reverse('evidencia:stiahni_vzor_importu', args=['VYROBCOVIA']), "Výrobcovia", "🧰"),
            (reverse('evidencia:stiahni_vzor_importu', args=['FILIALKY']), "Sklady/Filiálky", "🏪"),
            (reverse('evidencia:stiahni_vzor_importu', args=['SERVISOVANE_ZARIADENIA']), "Servisované zariadenia", "⚙️"),
        ]

        buttons_html = mark_safe("".join([
            render_isas_link_btn(
                url=url, 
                text=text, 
                icon=icon, 
                type_color="neutral", 
                full_width=True, 
                custom_style="height: 38px; display: inline-flex; align-items: center; justify-content: center; text-align: center; gap: 8px; box-sizing: border-box;"
            )
            for url, text, icon in links_data
        ]))

        return render_isas_layout(buttons_html, layout="grid", columns=5, gap=10, custom_style="margin-top: 5px; box-sizing: border-box;")

    def get_readonly_fields(self, request, obj=None):
        """
        🛡️ ISAS SECURITY: Ak už bol import vytvorený, zablokujeme zmenu súboru a typu.
        Ak sa už začal spracovávať (alebo skončil), striktne zablokujeme aj REŽIM IMPORTU, 
        aby administrátor nezmenil logiku bežiaceho tasku v polovici súboru!
        """
        readonly = list(super().get_readonly_fields(request, obj))
        uzamknute_polia = readonly + list(self.readonly_fields)
        if obj: 
            uzamknute_polia += ['typ_importu', 'subor', 'vytvoril']
            if obj.stav != 'NAHRATE':
                uzamknute_polia.append('rezim_importu')
        
        return list(dict.fromkeys(uzamknute_polia))

    def save_model(self, request, obj, form, change):
        """🛡️ ISAS SECURITY: Automatické priradenie autora a ochrana proti Race Condition."""
        if not obj.pk and not obj.vytvoril:
            obj.vytvoril = request.user

        if change:
            # Ak ide o úpravu existujúceho záznamu, uložíme IBA to, čo admin reálne zmenil.
            # Tým pádom nikdy neprepíšeme štatistiky (uspesne_importovane atď.), 
            # ktoré práve na pozadí aktualizuje Celery task.
            if form.changed_data:
                obj.save(update_fields=form.changed_data)
        else:
            # Ak ide o úplne nový import, použijeme štandardné uloženie
            super().save_model(request, obj, form, change)

    @admin.display(description='📥 Typ & Režim Importu', ordering='typ_importu')
    def get_hlavne_info_card(self, obj):
        ikony_typu = {
            'ZAKAZNICI': '🏢', 'DODAVATELIA': '🏭', 'SUKROMNE_OSOBY': '👤',
            'ZAMESTNANCI': '👷', 'VOZIDLA': '🚗', 'SKLAD_KARTY': '📦',
            'TYPY_SERVISU': '🛠️', 'VYROBCOVIA': '🧰', 'FILIALKY': '🏪',
            'SERVISOVANE_ZARIADENIA': '⚙️'
        }
        ikona = ikony_typu.get(obj.typ_importu, '📄')
        nazov_typu = obj.get_typ_importu_display() if hasattr(obj, 'get_typ_importu_display') else str(obj.typ_importu)
        
        rezim_text = obj.get_rezim_importu_display() if hasattr(obj, 'get_rezim_importu_display') else str(obj.rezim_importu)
        autor = obj.vytvoril.username if obj.vytvoril else "Systém"
        velkost = f"{obj.velkost_suboru_kb} kB" if obj.velkost_suboru_kb else "0 kB"
        
        title_html = render_isas_text(nazov_typu, size=12, weight=900, color="#0f172a", icon=ikona, custom_style="letter-spacing: 0.3px; line-height: 1.2;")
        badge_html = render_isas_badge(rezim_text, type_color="neutral", wrapper=False, custom_style="font-size: 9px; font-weight: 800; padding: 2px 6px; border-radius: 6px; letter-spacing: 0.2px; text-transform: uppercase;")
        
        subor_span = ""
        if obj.subor and hasattr(obj.subor, 'url'):
            subor_span = format_html(
                '<span onclick="event.preventDefault(); event.stopPropagation(); window.open(\'{}\', \'_blank\');" '
                'style="background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; padding: 1px 6px; border-radius: 6px; font-size: 9px; font-weight: 800; cursor: pointer; display: inline-flex; align-items: center; gap: 3px; transition: all 0.2s;" '
                'onmouseover="this.style.background=\'#dbeafe\';" onmouseout="this.style.background=\'#eff6ff\';" '
                'title="Stiahnuť nahratý súbor">💾 SÚBOR</span>',
                obj.subor.url
            )
            
        autor_text = render_isas_text(f"👤 {autor} | 📊 {velkost}", size=9, color="#64748b", weight=600)
        
        if subor_span:
            meta_row = render_isas_layout(format_html('{}{}', autor_text, subor_span), layout="row", center=True, gap=6)
        else:
            meta_row = render_isas_layout(autor_text, layout="row", center=True, gap=0)
            
        return render_isas_layout(format_html('{}{}{}', title_html, badge_html, meta_row), layout="col", center=True, gap=3, html_id=f"import-info-{obj.id}")

    @admin.display(description='🚥 Stav & Časovanie', ordering='stav')
    def get_stav_cas_card(self, obj):
        stav_farby = {
            'NAHRATE': ('info', '⏳ Čaká v rade', ''),
            'SPRACOVAVA_SA': ('warning', '⚙️ Spracováva sa', 'animation: pulse 1.5s infinite;'),
            'HOTOVO': ('success', '✅ Dokončené', ''),
            'CHYBA': ('danger', '❌ Zlyhalo', ''),
        }
        type_color, stav_text, anim = stav_farby.get(obj.stav, ('neutral', 'Neznámy', ''))

        vytvorene = obj.vytvorene.strftime('%d.%m.%Y %H:%M') if obj.vytvorene else "---"
        dokoncene = obj.dokoncene.strftime('%H:%M:%S') if obj.dokoncene else "---"

        badge_html = render_isas_badge(stav_text, type_color=type_color, wrapper=False, custom_style=f"padding: 3px 8px; font-size: 10px; font-weight: 900; text-transform: uppercase; {anim}")
        
        cas_start = render_isas_text(f"📥 {vytvorene}", size=9, color="#475569", weight=700)
        cas_end = render_isas_text(f"🏁 {dokoncene}", size=9, color="#64748b", weight=700)
        
        cas_box = render_isas_layout(format_html('{}{}', cas_start, cas_end), layout="col", center=True, gap=1, bg_color="#f8fafc", border_color="#e2e8f0", padding="3px 8px", border_radius="6px")

        return render_isas_layout(format_html('{}{}', badge_html, cas_box), layout="col", center=True, gap=3, html_id=f"import-stav-{obj.id}", css_class="isas-import-poller", data_attrs={"status": obj.stav})

    @admin.display(description='📊 Štatistiky dát')
    def get_statistiky_card(self, obj):
        celkom = obj.celkovy_pocet_riadkov or 0
        uspesne = obj.uspesne_importovane or 0
        duplicity = obj.preskocene_duplicity or 0
        chyby = obj.chybne_riadky or 0

        if celkom == 0 and obj.stav in ['NAHRATE', 'CHYBA']:
            placeholder = render_isas_text("Zatiaľ bez dát", size=11, muted=True, weight=600)
            return render_isas_layout(placeholder, layout="col", center=True, html_id=f"import-stat-{obj.id}")

        title_html = render_isas_text(f"Celkom: {celkom}", size=11, weight=900, color="#0f172a", custom_style="border-bottom: 1px solid #f1f5f9; padding-bottom: 2px; width: 100%; text-align: center;")
        
        stat_uspesne = render_isas_text(f"✅ {uspesne}", size=11, weight=800, color="#10b981", tooltip="Úspešne importované")
        stat_duplicity = render_isas_text(f"⏭️ {duplicity}", size=11, weight=800, color="#f59e0b", tooltip="Preskočené duplicity")
        stat_chyby = render_isas_text(f"❌ {chyby}", size=11, weight=800, color="#ef4444", tooltip="Chybné riadky")
        
        stats_row = render_isas_layout(format_html('{}{}{}', stat_uspesne, stat_duplicity, stat_chyby), layout="row", center=True, gap=8)

        return render_isas_layout(format_html('{}{}', title_html, stats_row), layout="col", center=True, gap=3, bg_color="#ffffff", border_color="#e2e8f0", padding="4px 8px", border_radius="8px", shadow="0 1px 3px rgba(0,0,0,0.02)", html_id=f"import-stat-{obj.id}")

    @admin.display(description='📝 Log & Výsledok')
    def get_log_card(self, obj):
        if obj.stav == 'NAHRATE':
            obsah = render_isas_text("➖ Čaká na štart", size=11, color="#94a3b8", weight=600)
        elif obj.stav == 'SPRACOVAVA_SA':
            obsah = render_isas_text("🔄 Zapisujem dáta...", size=11, color="#3b82f6", weight=800, custom_style="animation: pulse 1.5s infinite;")
        else:
            chyby = obj.chybne_riadky or 0
            if obj.stav == 'CHYBA' or chyby > 0:
                badge = render_isas_badge("⚠️ Obsahuje chyby", type_color="danger", wrapper=False, custom_style="padding: 3px 8px; font-size: 10px; font-weight: 800;")
                subtext = render_isas_text(f"Zlyhalo: {chyby} riadkov", size=9, color="#ef4444", weight=700)
                obsah = render_isas_layout(format_html('{}{}', badge, subtext), layout="col", center=True, gap=2)
            else:
                obsah = render_isas_badge("Čistý import", icon="✨", type_color="success", wrapper=False, custom_style="padding: 4px 10px; font-size: 11px; font-weight: 800;")
                
        return render_isas_layout(obsah, layout="col", center=True, html_id=f"import-log-{obj.id}")

@admin.register(KorekciaMotohodinLog)
class KorekciaMotohodinLogAdmin(ISASReadOnlyAdminMixin, ISASPremiumAdmin):
    list_display = (
        'get_zariadenie_card',
        'get_zmena_mh_card',
        'get_zmena_zataz_card',
        'get_dovod_autor_card'
    )
    
    list_display_links = None 
    list_filter = (ISASRelatedUserFilter, 'datum')
    isas_user_filter_field = 'vykonal'
    search_fields = ['zariadenie__vyrobne_cislo', 'zariadenie__nazov', 'dovod']
    list_select_related = ('zariadenie', 'vykonal')
    readonly_fields = (
        'zariadenie', 'povodna_hodnota', 'nova_hodnota', 
        'povodna_hodnota_zataz', 'nova_hodnota_zataz', 
        'dovod', 'vykonal', 'datum'
    )

    fieldsets = (
        ('⚙️ Identifikácia zariadenia', {
            'description': 'Zariadenie, na ktorom bola korekcia počítadla vykonaná. 🖥️',
            'fields': ('zariadenie',)
        }),
        ('⏱️ Zmeny počítadiel', {
            'description': 'Porovnanie pôvodných a nových stavov počítadiel. 📊',
            'fields': (
                ('povodna_hodnota', 'nova_hodnota'),
                ('povodna_hodnota_zataz', 'nova_hodnota_zataz'),
            )
        }),
        ('🛡️ Audit a dôvod korekcie', {
            'description': 'Záznam o tom, kto, kedy a prečo zmenu vykonal. ✍️',
            'fields': ('dovod', 'vykonal', 'datum')
        }),
    )

    isas_title = f"🕒 Logy korekcií {get_industry_term('meter_label_genitiv', 'počítadiel')}"
    isas_preloader_text = f"⏳ Vstupujete do zoznamu logov korekcií {get_industry_term('meter_label_genitiv', 'počítadiel')}..."
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "🕒",
    }

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('zariadenie', 'vykonal').filter(
            Q(zariadenie__vyrobne_cislo__icontains=term) |
            Q(zariadenie__nazov__icontains=term) |
            Q(dovod__icontains=term)
        )[:10]

    def format_detective_result(self, obj):
        nazov = obj.zariadenie.nazov if obj.zariadenie else "Neznáme zariadenie"
        sn = obj.zariadenie.vyrobne_cislo if obj.zariadenie else "Bez S/N"
        autor = obj.vykonal.username if obj.vykonal else "Systém"
        vysledok = f"{nazov} (S/N: {sn})"
        datum = obj.datum.strftime('%d.%m.%Y %H:%M') if obj.datum else ""
        
        return {
            'id': obj.id,
            'name': vysledok,
            'search_value': vysledok,
            'rola': f"👤 {autor}",
            'date': datum,
            'cat': "KOREKCIA"
        }

    @admin.display(description='⚙️ Zariadenie', ordering='zariadenie')
    def get_zariadenie_card(self, obj):
        if not obj.zariadenie:
            return render_isas_text("Zariadenie odstránené", size=13, weight=900, color="#ef4444", icon="⚠️")
            
        nazov = obj.zariadenie.nazov or "Neznáme zariadenie"
        sn = obj.zariadenie.vyrobne_cislo or "Bez S/N"
        
        title_html = render_isas_text(nazov, size=13, weight=900, color="#0f172a", icon="🖥️")
        sn_badge = render_isas_badge(f"S/N: {sn}", type_color="neutral", monospace=True, wrapper=False)
        
        return render_isas_layout(format_html('{}{}', title_html, sn_badge), layout="col", gap=6, center=True)

    # --- 2. KARTA: ZMENA CELKOVÉHO STAVU POČÍTADLA ---
    @admin.display(description='⏱️ Celkový stav', ordering='nova_hodnota')
    def get_zmena_mh_card(self, obj):
        povodna = obj.povodna_hodnota or 0
        nova = obj.nova_hodnota or 0

        if povodna == nova:
            return render_isas_badge(obj.nova_hodnota_display, type_color="neutral")

        # Logika farieb: Ak sa znížili (oprava preklepu), dáme červenú. Ak sa zvýšili, zelenú.
        type_color = "danger" if nova < povodna else "success"
        ikona = "📉" if nova < povodna else "📈"

        old_text = render_isas_text(obj.povodna_hodnota_display, size=12, weight=800, color="#475569", custom_style="text-decoration: line-through; opacity: 0.6;")
        arrow = render_isas_text("➡️", size=12)
        new_badge = render_isas_badge(obj.nova_hodnota_display, icon=ikona, type_color=type_color, wrapper=False)

        row = render_isas_layout(format_html('{}{}{}', old_text, arrow, new_badge), layout="row", gap=8, center=True)
        return render_isas_layout(row, layout="col", gap=6, center=True)

    # --- 3. KARTA: ZMENA STAVU V ZÁŤAŽI ---
    @admin.display(description='🏋️ Stav v záťaži', ordering='nova_hodnota_zataz')
    def get_zmena_zataz_card(self, obj):
        povodna = obj.povodna_hodnota_zataz
        nova = obj.nova_hodnota_zataz

        # Ak zariadenie vôbec netrackuje záťažové počítadlo (v DB je NULL)
        if povodna is None and nova is None:
            return render_isas_text("Neeviduje sa", size=11, weight=600, muted=True, icon="➖")

        povodna_val = povodna or 0
        nova_val = nova or 0

        # Ak nedošlo k zmene záťažového stavu
        if povodna_val == nova_val:
            return render_isas_badge(obj.nova_hodnota_zataz_display, type_color="neutral")

        type_color = "warning" if nova_val < povodna_val else "info"
        ikona = "📉" if nova_val < povodna_val else "📈"

        old_text = render_isas_text(obj.povodna_hodnota_zataz_display, size=11, weight=800, color="#64748b", custom_style="text-decoration: line-through; opacity: 0.6;")
        arrow = render_isas_text("➡️", size=11)
        new_badge = render_isas_badge(obj.nova_hodnota_zataz_display, icon=ikona, type_color=type_color, wrapper=False)

        row = render_isas_layout(format_html('{}{}{}', old_text, arrow, new_badge), layout="row", gap=8, center=True)
        return render_isas_layout(row, layout="col", gap=6, center=True)

    # --- 4. KARTA: DÔVOD A AUDIT ---
    @admin.display(description='🛡️ Audit & Dôvod', ordering='datum')
    def get_dovod_autor_card(self, obj):
        autor = obj.vykonal.username if obj.vykonal else "Systém"
        datum = obj.datum.strftime('%d.%m.%Y %H:%M') if obj.datum else "-"
        dovod = obj.dovod or ""

        header = render_isas_text(
            f"👤 {autor} | 🕒 {datum}", 
            size=10, 
            weight=800, 
            color="#64748b", 
            custom_style="border-bottom: 1px solid #f1f5f9; padding-bottom: 4px; width: 100%;"
        )
        body = render_isas_text(
            f'"{dovod}"', 
            size=11, 
            weight=600, 
            color="#0f172a", 
            custom_style="line-height: 1.4; word-wrap: break-word;"
        )

        return render_isas_layout(
            format_html('{}{}', header, body),
            layout="col",
            gap=4,
            center=True,
            bg_color="white",
            border_color="#e2e8f0",
            border_radius="8px",
            padding="6px 12px",
            shadow="0 1px 3px rgba(0,0,0,0.02)",
            max_width="250px",
            custom_style="margin: 0 auto;"
        )

class AIAuditIdentitaFilter(admin.SimpleListFilter):
    """🧠 Zjednotený filter pre AI logy - Zobrazuje Zamestnancov aj Súkromné osoby v jednom zozname."""
    title = '👤 Identita (Kto sa pýtal)'
    parameter_name = 'ai_identita'

    def lookups(self, request, model_admin):
        User = get_user_model()
        lookups_data = []

        # 1. 👨‍💼 Načítanie aktívnych používateľov (Zamestnanci / Admini)
        # select_related('zamestnanec') je teraz 100% bezpečné, lebo vieme, že OneToOneField existuje
        aktivni_useri = User.objects.filter(
            Exists(model_admin.model.objects.filter(pouzivatel=OuterRef('pk')))
        ).select_related('zamestnanec')
        
        for u in aktivni_useri:
            meno = u.get_full_name() or u.username
            # Defenzíva: User nemusí mať vytvorený profil Zamestnanca (vyhodí ObjectDoesNotExist)
            try:
                if hasattr(u, 'zamestnanec') and u.zamestnanec:
                    cele_meno = f"{u.zamestnanec.meno} {u.zamestnanec.priezvisko}".strip()
                    if cele_meno:
                        meno = cele_meno
            except ObjectDoesNotExist:
                pass 
            
            lookups_data.append((f"user_{u.id}", f"👨‍💼 {meno}"))

        # 2. 🤝 Načítanie aktívnych súkromných osôb
        aktivne_osoby = SukromnaOsoba.objects.filter(
            Exists(model_admin.model.objects.filter(sukromna_osoba=OuterRef('pk')))
        )
        
        for o in aktivne_osoby:
            lookups_data.append((f"osoba_{o.id}", f"🤝 {o.meno_a_priezvisko}"))

        # 3. Zoradíme celý zlúčený zoznam abecedne podľa zobrazeného mena
        return sorted(lookups_data, key=lambda x: x[1])

    def queryset(self, request, queryset):
        val = self.value()
        if val:
            # Bezpečné rozdelenie prefixu a ID
            if val.startswith('user_'):
                user_id = val.replace('user_', '')
                return queryset.filter(pouzivatel_id=user_id)
            elif val.startswith('osoba_'):
                osoba_id = val.replace('osoba_', '')
                return queryset.filter(sukromna_osoba_id=osoba_id)
        return queryset


@admin.register(AIAuditLog)
class AIAuditLogAdmin(ISASReadOnlyAdminMixin, ISASPremiumAdmin):
    list_display = (
        'get_identita_card',
        'get_otazka_card',
        'get_tokeny_card',
        'get_cena_card'
    )   
    list_display_links = ('get_otazka_card',) 
    list_filter = (AIAuditIdentitaFilter, 'pouzity_model', 'zdroj_poziadavky', 'uspesne_dokoncene', 'cas_dopytu')
    search_fields = ['pouzivatel__first_name', 'pouzivatel__last_name', 'sukromna_osoba__meno_a_priezvisko', 'otazka', 'celery_task_id', 'pouzity_model']
    list_select_related = ('pouzivatel', 'sukromna_osoba')

    readonly_fields = (
        'pouzivatel', 'sukromna_osoba', 'zdroj_poziadavky', 
        'cas_dopytu', 'otazka', 'odpoved', 'celery_task_id', 'pouzity_model',
        'vstupne_tokeny', 'vystupne_tokeny', 'presna_cena_usd', 'uspesne_dokoncene'
    )

    fieldsets = (
        ('👤 Identifikácia používateľa', {
            'description': 'Kto a odkiaľ položil otázku umelej inteligencii. 🕵️‍♂️',
            'fields': (('pouzivatel', 'sukromna_osoba'), 'zdroj_poziadavky')
        }),
        ('💬 Obsah komunikácie', {
            'description': 'Presné znenie dopytu, vygenerovaná odpoveď a časový záznam. ⏱️',
            'fields': ('cas_dopytu', 'otazka', 'odpoved')
        }),
        ('⚙️ Technické dáta a Fakturácia', {
            'description': 'Spotreba tokenov, použitý model a presná cena za API volanie. 💰',
            'fields': (
                ('pouzity_model', 'celery_task_id'),
                ('vstupne_tokeny', 'vystupne_tokeny'), 
                ('presna_cena_usd', 'uspesne_dokoncene'),
            )
        }),
    )

    isas_title = "🧠 AI Audit & Náklady"
    isas_preloader_text = "⏳ Vstupujete do zoznamu bezpečnostných logov AI..."
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'live_search_ikona': "🧠",
    }

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related(
            'pouzivatel', 'sukromna_osoba'
        ).filter(
            Q(pouzivatel__first_name__icontains=term) |
            Q(pouzivatel__last_name__icontains=term) |
            Q(sukromna_osoba__meno_a_priezvisko__icontains=term) |
            Q(otazka__icontains=term) |
            Q(celery_task_id__icontains=term) |
            Q(pouzity_model__icontains=term)
        )[:15]

    def format_detective_result(self, obj):
        meno = "Neznámy"
        if obj.pouzivatel:
            meno = obj.pouzivatel.get_full_name() or obj.pouzivatel.username
        elif obj.sukromna_osoba:
            meno = obj.sukromna_osoba.meno_a_priezvisko
            
        otazka_text = (obj.otazka[:50] + "...") if obj.otazka and len(obj.otazka) > 50 else (obj.otazka or "")
        datum = obj.cas_dopytu.strftime('%d.%m.%Y %H:%M') if obj.cas_dopytu else ""
            
        return {
            'id': obj.id,
            'name': meno,
            'search_value': meno,
            'rola': otazka_text,
            'date': datum,
            'cat': 'AI LOG'
        }

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'delete':
            with transaction.atomic():
                bezpecny_queryset = self.get_queryset(request).filter(id__in=valid_ids)
                zmazane_pocet, _ = bezpecny_queryset.delete()
            return JsonResponse({"status": "success", "message": f"✨ Úspešne odstránené ({zmazane_pocet} záznamov)!"})
            
        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    @admin.display(description='👤 Identita & Zdroj', ordering='cas_dopytu')
    def get_identita_card(self, obj):
        if obj.pouzivatel:
            meno = obj.pouzivatel.get_full_name() or obj.pouzivatel.username
            typ_ikona = "👨‍💼"
        elif obj.sukromna_osoba:
            meno = obj.sukromna_osoba.meno_a_priezvisko
            typ_ikona = "🤝"
        elif obj.zdroj_poziadavky == 'SYSTEM':
            meno = "ISAS Systém (Celery / Autonómny audit)"
            typ_ikona = "🤖"
        else:
            meno = "Neznámy používateľ"
            typ_ikona = "❓"

        zdroj_map = {
            'WEB': ('💻 Web', 'info', None, None, None),
            'TELEGRAM': ('✈️ Telegram', 'custom', '#f0fdfa', '#0891b2', '#a5f3fc'),
            'DISCORD': ('🎮 Discord', 'purple', None, None, None),
            'SYSTEM': ('⚙️ Systém', 'custom', '#f1f5f9', '#334155', '#cbd5e1'),
        }
        zdroj_data = zdroj_map.get(obj.zdroj_poziadavky, ('🌐 Iné', 'neutral', None, None, None))
        zdroj_text, type_color, custom_bg, custom_text, custom_border = zdroj_data

        icon_html = render_isas_text(typ_ikona, size=18, custom_style="filter: drop-shadow(0 2px 4px rgba(0,0,0,0.1));")
        text_html = render_isas_text(meno, size=13, weight=900, color="#0f172a", custom_style="letter-spacing: 0.5px;")
        name_row = render_isas_layout(format_html('{}{}', icon_html, text_html), layout="row", center=True, gap=8)

        badge_html = render_isas_badge(
            zdroj_text, 
            type_color=type_color if type_color != 'custom' else 'neutral',
            custom_bg=custom_bg,
            custom_text=custom_text,
            custom_border=custom_border,
            monospace=True,
            wrapper=False
        )

        return render_isas_layout(
            format_html('{}{}', name_row, badge_html),
            layout="col",
            center=True,
            gap=6
        )

    @admin.display(description='💬 Komunikácia & Čas', ordering='cas_dopytu')
    def get_otazka_card(self, obj):
        datum = obj.cas_dopytu.strftime('%d.%m.%Y %H:%M') if obj.cas_dopytu else "-"
        otazka_text = obj.otazka or "Bez textu"
        odpoved_text = obj.odpoved or "Bez odpovede (Chyba/Spracováva sa)"
        
        kratka_otazka = (otazka_text[:35] + '...') if len(otazka_text) > 35 else otazka_text
        kratka_odpoved = (odpoved_text[:35] + '...') if len(odpoved_text) > 35 else odpoved_text

        time_html = render_isas_text(f"🕒 {datum}", size=10, weight=800, color="#64748b")
        detail_html = render_isas_text("Detail 🔍", size=9, weight=900, color="#2563eb", custom_style="text-transform: uppercase; letter-spacing: 0.5px;")
        header_container = render_isas_flex_between(
            time_html, 
            detail_html, 
            extra_style="border-bottom: 1px solid #f1f5f9; padding-bottom: 4px; margin-bottom: 6px;"
        )

        q_label = render_isas_text("❓ Otázka:", size=9, weight=800, color="#475569", custom_style="text-transform: uppercase; margin-bottom: 1px;")
        q_val = render_isas_text(f'"{kratka_otazka}"', size=11, weight=600, color="#0f172a", custom_style="font-style: italic;")
        q_container = render_isas_layout(format_html('{}{}', q_label, q_val), layout="col", gap=0, custom_style="margin-bottom: 4px; line-height: 1.3;")

        a_label = render_isas_text("🤖 Odpoveď:", size=9, weight=800, color="#10b981", custom_style="text-transform: uppercase; margin-bottom: 1px;")
        a_val = render_isas_text(kratka_odpoved, size=11, weight=500, color="#334155")
        a_content = render_isas_layout(format_html('{}{}', a_label, a_val), layout="col", gap=0, custom_style="line-height: 1.3;")
        a_container = render_isas_singleton_row(a_content, is_dashed=True, border_color="#f1f5f9", padding_top=4)

        content = format_html('{}{}{}', header_container, q_container, a_container)
        return render_isas_layout(
            content,
            layout="col",
            bg_color="#ffffff",
            border_color="#e2e8f0",
            border_radius="8px",
            padding="8px 12px",
            min_width="260px",
            max_width="320px",
            shadow="0 1px 3px rgba(0,0,0,0.02)",
            custom_style="margin: 0 auto; text-align: left;"
        )

    @admin.display(description='⚙️ Tokeny & Model', ordering='uspesne_dokoncene')
    def get_tokeny_card(self, obj):
        in_tok = obj.vstupne_tokeny or 0
        out_tok = obj.vystupne_tokeny or 0
        
        in_html = render_isas_text(f"📥 {in_tok}", size=11, weight=800, color="#64748b", tooltip="Vstupné tokeny (Prompt)")
        sep_html = render_isas_text("|", size=11, weight=800, color="#64748b")
        out_html = render_isas_text(f"📤 {out_tok}", size=11, weight=800, color="#64748b", tooltip="Výstupné tokeny (Odpoveď)")
        
        tokens_row = render_isas_layout(
            format_html('{}{}{}', in_html, sep_html, out_html),
            layout="row",
            center=True,
            gap=8
        )

        model_nazov = obj.pouzity_model or "gemini-4-hydrogen"
        model_badge = render_isas_badge(model_nazov, type_color="neutral", monospace=True, wrapper=False, custom_style="font-size: 9px; padding: 1px 6px; color: #475569; background: #f1f5f9; border: 1px solid #cbd5e1;")

        if obj.uspesne_dokoncene:
            status_badge = render_isas_badge("ÚSPECH", icon="✅", type_color="success", wrapper=False, custom_style="box-shadow: 0 2px 4px rgba(0,0,0,0.05); padding: 2px 6px; font-size: 10px;")
        else:
            status_badge = render_isas_badge("ZLYHANIE", icon="❌", type_color="danger", wrapper=False, custom_style="box-shadow: 0 2px 4px rgba(0,0,0,0.05); padding: 2px 6px; font-size: 10px;")

        status_row = render_isas_layout(
            format_html('{}{}', model_badge, status_badge),
            layout="row",
            center=True,
            gap=6
        )

        return render_isas_layout(
            format_html('{}{}', tokens_row, status_row),
            layout="col",
            center=True,
            gap=6
        )

    @admin.display(description='💵 Fakturácia (Cena)', ordering='presna_cena_usd')
    def get_cena_card(self, obj):
        try:
            cena_dec = Decimal(str(obj.presna_cena_usd or '0.00'))
        except (InvalidOperation, TypeError, ValueError):
            cena_dec = Decimal('0.00')
        
        if cena_dec == Decimal('0.00'):
            return render_isas_badge(
                "$ 0.0000", 
                type_color="neutral", 
                custom_style="font-size: 12px; padding: 4px 10px;",
                wrapper=True
            )

        cena_formatovana = f"{cena_dec:.6f}"

        return render_isas_badge(
            f"$ {cena_formatovana}", 
            icon="💰", 
            type_color="warning", 
            custom_style="font-size: 13px; padding: 4px 10px; box-shadow: 0 2px 4px rgba(217, 119, 6, 0.1);",
            wrapper=True
        )

@admin.register(Nepritomnost)
class NepritomnostAdmin(ISASPremiumAdmin):        
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('zamestnanec', 'schvalil')

    list_display = (
        'get_zamestnanec_card', 
        'get_termin_card', 
        'get_stav_card', 
        'get_dokument_card'
    )
    list_display_links = ('get_zamestnanec_card',)
    list_filter = ('stav', 'typ', ISASRelatedTechnikFilter)
    isas_technici_filter_field = 'zamestnanec'
    search_fields = ('zamestnanec__meno', 'zamestnanec__priezvisko')
    readonly_fields = ('pocet_pracovnych_dni', 'schvalil')
    
    fieldsets = (
        ('👤 Zamestnanec a Typ', {
            'description': 'Základná identifikácia absencie. 📌 Vyberte zamestnanca, typ voľna a aktuálny stav schvaľovania.',
            'fields': (
                'zamestnanec',
                ('typ', 'stav')
            )
        }),
        ('📅 Trvanie a Rozsah', {
            'description': 'Časový úsek neprítomnosti. ⏱️ Systém automaticky preskočí víkendy pri dovolenke, ale započíta ich pri PN.',
            'fields': (
                ('datum_od', 'datum_do'),
                'pol_dna',
                'pocet_pracovnych_dni'
            )
        }),
        ('📎 Legislatíva a Audit', {
            'description': 'Doklady a stopa schvaľovania. 👑 Pole "Schválil" sa vyplní automaticky po zmene stavu na Schválené.',
            'fields': (
                'priloha',
                'schvalil'
            )
        }),
        ('📝 Doplňujúce informácie', {
            'description': 'Interná poznámka k absencii. ✍️ Napríklad dôvod neplateného voľna alebo špecifikácia lekára.',
            'fields': (
                'poznamka',
            )
        }),
    )

    isas_title = "🌴 Evidencia Absencií"
    isas_preloader_text = "⏳ Vstupujete do zoznamu HR dát a kapacity..."
    isas_clean_fk_fields = {'zamestnanec': '👤 Vyberte používateľa'}
    isas_magia_kwargs = {
        'live_search_endpoint': 'detective-search/',
        'ignore_default_action_ids': ['btn-hr-dashboard'],
        'live_search_ikona': "🌴",
    }
    isas_delete_success_message = "✨ Úspešne odstránené ({count} záznamov)!"

    def changelist_view(self, request, extra_context=None):
        dashboard_url = reverse('evidencia:hr_dashboard')
        
        # 🛡️ ISAS CORE: Bezpečné dynamické vloženie URL bez prepisovania class atribútu
        povodna_magia = self.isas_magia_kwargs.copy()
        self.isas_magia_kwargs = povodna_magia.copy()
        self.isas_magia_kwargs['custom_buttons_html'] = format_html('<a id="btn-hr-dashboard" class="isas-action-btn btn-blue" href="{}">📊 Otvoriť HR Dashboard</a>', dashboard_url)
        
        response = super().changelist_view(request, extra_context)
        
        self.isas_magia_kwargs = povodna_magia
        return response

    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('zamestnanec').filter(
            Q(zamestnanec__meno__icontains=term) |
            Q(zamestnanec__priezvisko__icontains=term)
        )[:15]

    def format_detective_result(self, obj):
        meno = f"{obj.zamestnanec.meno} {obj.zamestnanec.priezvisko}" if obj.zamestnanec else "Neznámy"
        datum_od = obj.datum_od.strftime('%d.%m.%Y') if obj.datum_od else "?"
        datum_do = obj.datum_do.strftime('%d.%m.%Y') if obj.datum_do else "?"
        
        return {
            'id': obj.id,
            'name': meno,
            'search_value': meno,
            'rola': obj.get_typ_display(),
            'date': f"{datum_od} - {datum_do}",
            'cat': 'ABSENCIA'
        }
    
    @admin.display(description='👤 Zamestnanec & Typ', ordering='zamestnanec__priezvisko')
    def get_zamestnanec_card(self, obj):
        typ_farby = {
            'DOVOLENKA': ('#d1fae5', '#065f46', '🌴 Dovolenka'),
            'PN': ('#fee2e2', '#991b1b', '🤒 PN'),
            'PARAGRAF': ('#dbeafe', '#1e40af', '⚖️ Paragraf'),
            'NEPLATENE': ('#f1f5f9', '#475569', '🚫 Neplatené'),
        }
        bg, color, text = typ_farby.get(obj.typ, ('#f3f4f6', '#374151', obj.get_typ_display()))
        
        meno_zamestnanca = str(obj.zamestnanec) if obj.zamestnanec else "Neznámy zamestnanec"
        name_html = render_isas_text(meno_zamestnanca, size=13, color="#0f172a", weight=800)
        badge_html = render_isas_badge(text, custom_bg=bg, custom_text=color, custom_shadow="rgba(0,0,0,0.05)", wrapper=False)
        
        return render_isas_layout(format_html('{}{}', name_html, badge_html), layout="col", center=True, gap=4)

    @admin.display(description='📅 Termín & Rozsah', ordering='datum_od')
    def get_termin_card(self, obj):
        if not obj.datum_od or not obj.datum_do:
            return render_isas_text("Chýba dátum", size=12, color="#ef4444", weight=800)
            
        od_str = obj.datum_od.strftime('%d.%m.%Y')
        do_str = obj.datum_do.strftime('%d.%m.%Y')
        
        termin_parts = [render_isas_text(od_str, size=12, color="#1e293b", weight=800)]
        if obj.datum_od != obj.datum_do:
            termin_parts.append(render_isas_text("-", size=12, color="#94a3b8", weight=800))
            termin_parts.append(render_isas_text(do_str, size=12, color="#1e293b", weight=800))
            
        if obj.pol_dna:
            termin_parts.append(render_isas_badge("🌗 Pol dňa", custom_bg="#fef3c7", custom_text="#92400e", custom_border="#fde68a", wrapper=False))
            
        termin_row = render_isas_layout(mark_safe("".join(termin_parts)), layout="row", center=True, gap=4)
        
        trvanie_label = render_isas_text("⏱️ Trvanie:", size=11, color="#64748b", weight=600)
        trvanie_val = render_isas_text(f"{obj.pocet_pracovnych_dni} dni", size=11, color="#0f172a", weight=800)
        trvanie_row = render_isas_layout(format_html('{}{}', trvanie_label, trvanie_val), layout="row", center=True, gap=4)
        
        return render_isas_layout(format_html('{}{}', termin_row, trvanie_row), layout="col", center=True, gap=4)

    @admin.display(description='🚥 Stav & Audit', ordering='stav')
    def get_stav_card(self, obj):
        stav_farby = {
            'CAKA': ('#fffbeb', '#d97706', '#fde68a', '⏳ Čaká na schválenie'),
            'SCHVALENE': ('#f0fdf4', '#10b981', '#bbf7d0', '✅ Schválené'),
            'ZAMIETNUTE': ('#fef2f2', '#ef4444', '#fecaca', '❌ Zamietnuté'),
        }
        bg, color, border, text = stav_farby.get(obj.stav, ('#f8fafc', '#64748b', '#e2e8f0', 'Neznámy'))
        
        schvalil_text = f"👑 Schválil: {obj.meno_schvalujuceho}" if obj.stav == 'SCHVALENE' else "—"
        
        badge_html = render_isas_badge(text, custom_bg=bg, custom_text=color, custom_border=border, wrapper=False)
        audit_html = render_isas_text(schvalil_text, size=10, color="#64748b", weight=600)
        
        return render_isas_layout(format_html('{}{}', badge_html, audit_html), layout="col", center=True, gap=6)

    @admin.display(description='📎 Príloha & Poznámka')
    def get_dokument_card(self, obj):
        if obj.priloha and hasattr(obj.priloha, 'url'):
            priloha_html = render_isas_link_btn(
                url=obj.priloha.url, 
                text="Otvoriť doklad", 
                icon="📄", 
                type_color="info", 
                wrapper=False, 
                custom_style="padding: 3px 8px; font-size: 10px;"
            )
        else:
            priloha_html = render_isas_text("✖️ Bez prílohy", size=10, color="#94a3b8", weight=600)
            
        poznamka_html = ""
        if obj.poznamka:
            poznamka_html = render_isas_text(
                f"📝 {obj.poznamka}", 
                size=10, 
                color="#64748b", 
                weight=400, 
                max_width=180, 
                tooltip=obj.poznamka, 
                custom_style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            
        return render_isas_layout(format_html('{}{}', priloha_html, poznamka_html), layout="col", center=True, gap=6)

    def save_model(self, request, obj, form, change):
        # 🧠 ISAS AUTO-AUDIT: Ak sa stav mení na SCHVALENE a ešte to nemá schvaľovateľa
        if obj.stav == 'SCHVALENE' and not obj.schvalil:
            # Pokúsime sa nájsť Zamestnanca priradeného k aktuálne prihlásenému Userovi
            zamestnanec = getattr(request.user, 'zamestnanec', None)
            if zamestnanec:
                obj.schvalil = zamestnanec
                
        super().save_model(request, obj, form, change)

@admin.register(DashboardKvality)
class DashboardKvalityAdmin(admin.ModelAdmin):
    """Tlačidlo v ľavom menu, ktoré presmeruje na vizuálny dashboard kvality."""
    def changelist_view(self, request, extra_context=None):
        return redirect('evidencia:dashboard_reklamacie')
        
    def has_add_permission(self, request):
        return False

@admin.register(HRDashboard)
class HRDashboardAdmin(admin.ModelAdmin):
    """Tlačidlo v ľavom menu, ktoré presmeruje na vizuálny kalendár."""
    def changelist_view(self, request, extra_context=None):
        return redirect('evidencia:hr_dashboard')
        
    def has_add_permission(self, request):
        return False

@admin.register(TelemetryHistory)
class TelemetryHistoryAdmin(ISASHiddenAdminMixin, ISASReadOnlyAdminMixin, admin.ModelAdmin):
    # Nepoužívame ISASPremiumAdmin, pretože toto je len Read-Only log s obrovským objemom dát
    list_display = ('zariadenie', 'zakaznik', 'casova_peciatka')
    search_fields = ('zariadenie__vyrobne_cislo',)
    list_filter = ('casova_peciatka',)
    readonly_fields = ('zariadenie', 'zakaznik', 'casova_peciatka', 'analogove_hodnoty', 'digitalne_stavy')

@admin.register(AlarmLog)
class AlarmLogAdmin(ISASHiddenAdminMixin, ISASPremiumAdmin):
    list_display = ('zariadenie', 'zakaznik', 'zavaznost', 'kod_chyby', 'casova_peciatka', 'vyriesene')
    search_fields = ('zariadenie__vyrobne_cislo', 'kod_chyby', 'popis')
    list_filter = ('zavaznost', 'vyriesene', 'casova_peciatka')
    readonly_fields = ('zariadenie', 'zakaznik', 'casova_peciatka', 'snapshot_dat')

@admin.register(ModbusKonfigurator)
class ModbusKonfiguratorAdmin(admin.ModelAdmin):
    """Tlačidlo v ľavom menu, ktoré presmeruje na VIVID SPA Modbus Konfigurátor (ISAS Vzor)."""
    def changelist_view(self, request, extra_context=None):
        return redirect('evidencia:modbus_konfigurator')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

class StavPlatnostiPozicieFilter(admin.SimpleListFilter):
    """Filter pre reálny stav inzerátu zohľadňujúci prepínač aj dátum expirácie."""
    title = "⏱️ Reálna platnosť na webe"
    parameter_name = "realna_platnost"

    def lookups(self, request, model_admin):
        return (
            ('live', '🟢 Aktívne na webe (Platné)'),
            ('expired', '⚠️ Expirované (Vyžaduje pozornosť)'),
            ('inactive', '🔴 Manuálne deaktivované'),
        )

    def queryset(self, request, queryset):
        today = timezone.now().date()
        val = self.value()
        if val == 'live':
            return queryset.filter(je_aktivna=True).filter(Q(platnost_do__gte=today) | Q(platnost_do__isnull=True))
        if val == 'expired':
            return queryset.filter(je_aktivna=True, platnost_do__lt=today)
        if val == 'inactive':
            return queryset.filter(je_aktivna=False)
        return queryset

@admin.register(PracovnaPozicia)
class PracovnaPoziciaAdmin(ISASPremiumAdmin):
    isas_title = "🤝 Pracovné pozície (Kariéra)"
    isas_preloader_text = "⏳ Vstupujete do sekcie vašich inzerátov..."
    isas_clean_choice_fields = {
        'typ_uvazku': '💼 Vyberte typ úväzku',
        'cielova_rola': '👔 Vyberte pracovnú rolu v ERP',
    }
    isas_delete_success_message = "✨ Pracovné pozície boli úspešne odstránené ({count} záznamov)!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🤝",
    }

    list_display = (
        'get_pozicia_card',
        'get_podmienky_card',
        'get_poziadavky_card',
        'poradie',
        'je_aktivna'
    )
    list_display_links = ('get_pozicia_card',)
    list_editable = ('poradie', 'je_aktivna')
    list_filter = (StavPlatnostiPozicieFilter, 'cielova_rola', 'typ_uvazku', 'je_aktivna', 'vytvorene')
    readonly_fields = ('vytvorene',)
    search_fields = ('nazov', 'lokalita', 'mzda', 'poziadavky_skratka', 'benefity_skratka')
    prepopulated_fields = {'slug': ('nazov',)}
    ordering = ('poradie', '-id')

    fieldsets = (
        ('💼 1. ZÁKLADNÁ IDENTIFIKÁCIA POZÍCIE', {
            'description': '<b>💡 ISAS Nápoveda:</b> Zadajte oficiálny názov pozície, typ úväzku, cieľovú rolu v ERP a platnosť inzerátu na webe. 🔗',
            'fields': (
                ('nazov', 'slug'),
                ('cielova_rola', 'typ_uvazku'),
                ('platnost_do', 'poradie', 'je_aktivna'),
                ('vytvorene',),
            )
        }),
        ('💰 2. MZDA, LOKALITA A NÁSTUP', {
            'description': '<b>💰 Mzdové podmienky:</b> Tieto údaje sa zobrazia ako hlavné ťaháky inzerátu na webe. 📍',
            'fields': (
                ('mzda', 'lokalita', 'nastup'),
            )
        }),
        ('⚡ 3. RÝCHLE PÚTAČE PRE WEB KARTY', {
            'description': '<b>🎁 Rýchly prehľad:</b> Krátky zoznam kľúčových požiadaviek a benefitov, ktoré sa zobrazia na náhľadovej karte inzerátu. ✨',
            'fields': (
                'poziadavky_skratka',
                'benefity_skratka',
            )
        }),
        ('📝 4. PODROBNÝ POPIS A NÁPLŇ PRÁCE', {
            'description': '<b>📄 Detail inzerátu:</b> Podrobný popis práce, zodpovedností a všetkého, čo kandidátovi ponúkate. 🛠️',
            'fields': (
                'popis_naplne',
                'poziadavky_detail',
                'ponukame_detail',
            )
        }),
    )

    # --- 🕵️ ISAS DETECTIVE SEARCH API ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).filter(
            Q(nazov__icontains=term) |
            Q(lokalita__icontains=term) |
            Q(mzda__icontains=term) |
            Q(poziadavky_skratka__icontains=term)
        )[:15]

    def format_detective_result(self, obj):
        return {
            'id': obj.id,
            'name': obj.nazov,
            'search_value': obj.nazov,
            'cat': 'KARIÉRA',
            'rola': f"{obj.get_typ_uvazku_display()} | {obj.mzda}",
        }

    # --- 🎨 ISAS UI DISPLAY METÓDY ---
    @admin.display(description="🏷️ Pracovná pozícia", ordering="nazov")
    def get_pozicia_card(self, obj):
        safe_nazov = escape(obj.nazov or "(Bez názvu)")
        safe_lokalita = escape(obj.lokalita or "Neurčená lokalita")
        
        typ_color = 'success' if obj.typ_uvazku == 'TPP' else ('info' if obj.typ_uvazku == 'ZIVNOST' else 'neutral')
        badge_uvazok = render_isas_badge(escape(obj.get_typ_uvazku_display()), icon="💼", type_color=typ_color, wrapper=False)
        
        # 👔 Dynamický odznak roly v ERP
        rola_color = 'purple' if obj.cielova_rola == 'TE' else ('warning' if obj.cielova_rola == 'DI' else ('info' if obj.cielova_rola == 'SK' else ('success' if obj.cielova_rola == 'OB' else 'neutral')))
        badge_rola = render_isas_badge(escape(obj.get_cielova_rola_display()), icon="👔", type_color=rola_color, wrapper=False)
        
        badges_html = render_isas_layout(format_html('{}{}', badge_uvazok, badge_rola), layout="row", gap=4, center=True)

        return render_isas_avatar(
            icon="🤝",
            title=safe_nazov,
            subtitle=f"📍 {safe_lokalita}",
            bg_gradient="#3b82f6, #1d4ed8",
            shadow_color="rgba(59, 130, 246, 0.3)",
            extra_html=badges_html
        )

    @admin.display(description="💰 Mzda, Nástup a Platnosť", ordering="vytvorene")
    def get_podmienky_card(self, obj):
        safe_mzda = escape(obj.mzda or "Dohodou")
        safe_nastup = escape(obj.nastup or "Ihneď")
        
        mzda_txt = render_isas_text(safe_mzda, size=12, color="#15803d", weight=900, icon="💰", align="center")
        nastup_txt = render_isas_text(f"Nástup: {safe_nastup}", size=10, color="#64748b", weight=600, align="center")
        
        items = [mzda_txt, nastup_txt]
        platnost_do = to_date(obj.platnost_do)
        if platnost_do:
            je_po_platnosti = platnost_do < timezone.now().date()
            badge_color = "danger" if je_po_platnosti else "neutral"
            badge_text = f"Expirované ({platnost_do.strftime('%d.%m.%Y')})" if je_po_platnosti else f"Platné do: {platnost_do.strftime('%d.%m.%Y')}"
            items.append(render_isas_badge(escape(badge_text), icon="⏳", type_color=badge_color, wrapper=False))
        if getattr(obj, 'vytvorene', None):
            vytvorene_txt = render_isas_text(f"Pridané: {obj.vytvorene.strftime('%d.%m.%Y')}", size=9, color="#94a3b8", weight=600, align="center")
            items.append(vytvorene_txt)
            
        return render_isas_layout(format_html(''.join(['{}'] * len(items)), *items), layout="col", gap=2, center=True)

    @admin.display(description="✨ Požiadavky a Benefity")
    def get_poziadavky_card(self, obj):
        items = []
        if obj.poziadavky_skratka:
            items.append(render_isas_badge(escape(obj.poziadavky_skratka), icon="⚡", type_color="neutral", wrapper=False))
        if obj.benefity_skratka:
            items.append(render_isas_badge(escape(obj.benefity_skratka), icon="🎁", type_color="purple", wrapper=False))
        if not items:
            return render_isas_text("Bez skratiek", muted=True)
        return render_isas_layout(format_html(''.join(['{}'] * len(items)), *items), layout="col", gap=4, center=True)

class PlanovanyPohovorFilter(admin.SimpleListFilter):
    """Operatívny filter pre HR optimalizovaný pre DB indexy a časové zóny."""
    title = "📅 Termín pohovoru"
    parameter_name = "filter_pohovor"

    def lookups(self, request, model_admin):
        return (
            ('dnes', '📍 Pohovory dnes'),
            ('buduce', '⏳ Nadchádzajúce pohovory'),
            ('minule_neuzavrete', '⚠️ Prebehnuté (Vyžadujú vyhodnotenie)'),
            ('bez_terminu', '❓ Bez naplánovaného termínu'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if not val:
            return queryset

        now = timezone.now()
        now = timezone.localtime(now).replace(tzinfo=None) if timezone.is_aware(now) else now
        
        if val == 'dnes':
            # 🚀 DB INDEX OPTIMALIZÁCIA: Namiesto __date používame presný časový rozsah dňa (Index Range Scan)
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)
            return queryset.filter(datum_pohovoru__range=(start_of_day, end_of_day))
            
        if val == 'buduce':
            return queryset.filter(datum_pohovoru__gt=now).exclude(stav__in=['PRIJATY', 'ZAMIETNUTY', 'ARCHIV'])
            
        if val == 'minule_neuzavrete':
            return queryset.filter(datum_pohovoru__lt=now, stav='POHOVOR')
            
        if val == 'bez_terminu':
            return queryset.filter(datum_pohovoru__isnull=True).exclude(stav__in=['PRIJATY', 'ZAMIETNUTY', 'ARCHIV'])
            
        return queryset


class GDPRRetenciaFilter(admin.SimpleListFilter):
    """100% nepriestrelný GDPR filter ošetrujúci NULL hodnoty a odvolané súhlasy."""
    title = "⚖️ GDPR Retencia (Súhlas)"
    parameter_name = "gdpr_retencia"

    def lookups(self, request, model_admin):
        return (
            ('expirovane', '🚨 Expirovaný súhlas / Bez súhlasu (Nutný výmaz!)'),
            ('blizi_sa', '⚠️ Končí do 30 dní'),
            ('platne', '✅ Platný súhlas'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if not val:
            return queryset

        now = timezone.now()
        today = timezone.localtime(now).date() if timezone.is_aware(now) else now.date()
        
        if val == 'expirovane':
            # 🛡️ DEFENZÍVNA LOGIKA: Zahrnie expirované, NULL záznamy aj explicitne odvolané súhlasy
            return queryset.filter(
                Q(suhlas_do__lt=today) | 
                Q(suhlas_do__isnull=True) | 
                Q(gdpr_suhlas=False)
            )
            
        if val == 'blizi_sa':
            warning_date = today + timedelta(days=30)
            return queryset.filter(
                gdpr_suhlas=True,
                suhlas_do__gte=today, 
                suhlas_do__lte=warning_date
            )
            
        if val == 'platne':
            return queryset.filter(
                gdpr_suhlas=True,
                suhlas_do__gte=today
            )
            
        return queryset

class KandidatHodnotenieFilter(admin.SimpleListFilter):
    """Segmentácia kandidátov so striktnými rozsahmi (1-5)."""
    title = "⭐ Kvalita kandidáta"
    parameter_name = "kvalita"

    def lookups(self, request, model_admin):
        return (
            ('top', '⭐⭐⭐⭐+ (Top talent: 4-5)'),
            ('priemer', '⭐⭐⭐ (Priemer: 3)'),
            ('slabi', '⭐-⭐⭐ (Nevyhovujúci: 1-2)'),
            ('nehodnoteni', '❓ Neohodnotení (Nové CV)'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'top':
            return queryset.filter(hodnotenie__gte=4, hodnotenie__lte=5)
        if val == 'priemer':
            return queryset.filter(hodnotenie=3)
        if val == 'slabi':
            return queryset.filter(hodnotenie__gte=1, hodnotenie__lte=2)
        if val == 'nehodnoteni':
            return queryset.filter(Q(hodnotenie__isnull=True) | Q(hodnotenie=0))
        return queryset

class StavPreklopeniaFilter(admin.SimpleListFilter):
    """Filter pre rýchle zobrazenie kandidátov preklopených do ERP."""
    title = "👨‍💼 Karta v ERP"
    parameter_name = "erp_stav"

    def lookups(self, request, model_admin):
        return (
            ('ano', '✅ Preklopený do ERP'),
            ('caka', '⏳ Prijatý (Čaká na kartu)'),
            ('nie', '👤 Bez karty (V procese)'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'ano':
            return queryset.filter(vytvoreny_zamestnanec__isnull=False)
        if val == 'caka':
            return queryset.filter(stav='PRIJATY', vytvoreny_zamestnanec__isnull=True)
        if val == 'nie':
            return queryset.filter(vytvoreny_zamestnanec__isnull=True).exclude(stav='PRIJATY')
        return queryset

@admin.register(Kandidat)
class KandidatAdmin(ISASSecurityAdminMixin, ISASPremiumAdmin):
    isas_title = "🎓 Uchádzači o zamestnanie (ATS)"
    isas_preloader_text = "⏳ Vstupujete do zoznamu uchádzačov o prácu..."
    isas_clean_fk_fields = {
        'pozicia': '🎯 Všetky / Všeobecný záujem',
    }
    isas_clean_choice_fields = {
        'stav': '🚥 Zvoľte stav výberového konania',
    }
    isas_delete_success_message = "✨ Profily uchádzačov boli úspešne odstránené ({count} záznamov)!"
    isas_magia_kwargs = {
        'live_search_endpoint': "detective-search/",
        'live_search_ikona': "🎓",
        'custom_buttons_html': '<button type="button" id="btn-convert-employee" data-action="convert_to_employee" class="isas-action-btn btn-green">🎉 Založiť kartu zamestnanca</button>',
    }

    isas_action_modals = {
        'btn-convert-employee': {
            'type': 'direct_action',
            'action': 'convert_to_employee',
            'confirm': True,
            'confirm_title': 'Založenie karty zamestnanca 🎉',
            'confirm_text': 'Naozaj chcete vybraného uchádzača prijať a automaticky mu vytvoriť kartu zamestnanca v ERP?',
            'loading_text': '⏳ PREKLÁPAM...',
            'success_text': '🎉 PREKLOPENÉ',
            'require_selection': True,
            'min_selection': 1,
            'max_selection': 1,
            'error_msg': 'Vyberte presne jedného kandidáta na konverziu.',
            'reload': True
        }
    }

    def perform_fast_action(self, request, action_type, valid_ids, **kwargs):
        if action_type == 'convert_to_employee':
            if not request.user.has_perm('Evidencia.add_zamestnanec'):
                return JsonResponse({"status": "error", "message": "🚫 Nemáte oprávnenie vytvárať zamestnancov."}, status=403)
            
            if len(valid_ids) != 1:
                return JsonResponse({"status": "error", "message": "⚠️ Vyberte presne jedného kandidáta na konverziu."}, status=400)
            
            with transaction.atomic():
                kandidat = self.model.objects.select_for_update().select_related('pozicia', 'vytvoreny_zamestnanec').filter(id=valid_ids[0]).first()
                if not kandidat:
                    return JsonResponse({"status": "error", "message": "❌ Kandidát nebol nájdený."}, status=404)
                if kandidat.vytvoreny_zamestnanec:
                    return JsonResponse({"status": "error", "message": f"⚠️ Tento kandidát už má vytvorenú kartu zamestnanca (#{kandidat.vytvoreny_zamestnanec_id})."}, status=400)

                # 🧠 Dynamické preklopenie: Typ pomeru aj rola sa načítajú automaticky z inzerátu
                novy_zamestnanec = kandidat.preklop_na_zamestnanca()
                if not novy_zamestnanec:
                    return JsonResponse({"status": "error", "message": "❌ Preklopenie kandidáta na zamestnanca zlyhalo."}, status=400)

            redirect_url = reverse('admin:Evidencia_zamestnanec_change', args=[novy_zamestnanec.id])
            return JsonResponse({
                "status": "success",
                "message": f"🎉 Zamestnanec {novy_zamestnanec.meno} {novy_zamestnanec.priezvisko} bol úspešne preklopený! Otváram profil...",
                "redirect_url": redirect_url
            })

        return super().perform_fast_action(request, action_type, valid_ids, **kwargs)

    def save_model(self, request, obj, form, change):
        # 🛡️ DEFENZÍVNOSŤ: Notifikácie a preklopenie riadi priamo model Kandidat.save()
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('pozicia', 'vytvoreny_zamestnanec')

    list_display = (
        'get_kandidat_card',
        'get_pozicia_badge',
        'stav',
        'get_zamestnanec_badge',
        'get_hodnotenie_stars',
        'get_contact_card',
        'get_cv_action',
        'get_vytvorene',
    )
    list_display_links = ('get_kandidat_card',)
    list_editable = ('stav',)
    list_filter = (
        'stav',
        StavPreklopeniaFilter,
        PlanovanyPohovorFilter,
        KandidatHodnotenieFilter,
        ISASRelatedPoziciaFilter,
        GDPRRetenciaFilter,
        ISASDateRangeFilter,
    )
    isas_date_filter_field = 'vytvorene'
    search_fields = ('meno', 'email', 'telefon', 'motivacna_sprava', 'interna_poznamka', 'pozicia__nazov', 'vytvoreny_zamestnanec__meno', 'vytvoreny_zamestnanec__priezvisko')
    readonly_fields = ('vytvoreny_zamestnanec', 'ip_adresa', 'gdpr_suhlas', 'suhlas_do', 'vytvorene', 'upravene')
    ordering = ('-vytvorene',)

    fieldsets = (
        ('👤 1. OSOBNÉ A KONTAKTNÉ ÚDAJE', {
            'description': '<b>💡 ISAS ATS Nápoveda:</b> Základné kontaktné údaje uchádzača získané z webového formulára. 📱',
            'fields': (
                ('meno', 'pozicia'),
                ('telefon', 'email'),
            )
        }),
        ('🎯 2. STAV VÝBEROVÉHO KONANIA A ONBOARDING', {
            'description': '<b>📊 HR Pipeline:</b> Posúvajte kandidáta fázami náboru. Pri stave PRIJATÝ systém automaticky založí kartu zamestnanca. 👨‍💼',
            'fields': (
                ('stav', 'hodnotenie'),
                ('datum_pohovoru', 'vytvoreny_zamestnanec'),
            )
        }),
        ('📄 3. ŽIVOTOPIS A MOTIVÁCIA KANDIDÁTA', {
            'description': '<b>📥 Podklady:</b> Nahraný originálny životopis a sprievodný text z formulára. 📑',
            'fields': (
                'zivotopis',
                'motivacna_sprava',
            )
        }),
        ('🔒 4. DÔVERNÉ POZNÁMKY (VEDENIE / HR)', {
            'description': '<b>🤫 Neverejné:</b> Zápis z pohovoru, mzdové požiadavky a interné hodnotenie technických znalostí. 📝',
            'fields': (
                'interna_poznamka',
            )
        }),
        ('⚖️ 5. BEZPEČNOSŤ A GDPR AUDIT', {
            'description': '<b>🛡️ Právna ochrana:</b> Technické záznamy o udelení súhlasu so spracovaním osobných údajov. 🌐',
            'fields': (
                ('gdpr_suhlas', 'suhlas_do'),
                ('ip_adresa',),
                ('vytvorene', 'upravene'),
            )
        }),
    )

    # --- 🕵️ ISAS DETECTIVE SEARCH API ---
    def get_detective_queryset(self, request, term):
        return self.get_queryset(request).select_related('pozicia').filter(
            Q(meno__icontains=term) |
            Q(email__icontains=term) |
            Q(telefon__icontains=term) |
            Q(pozicia__nazov__icontains=term)
        )[:15]

    def format_detective_result(self, obj):
        pozicia_str = obj.pozicia.nazov if obj.pozicia else "Všeobecný záujem"
        datum_str = obj.vytvorene.strftime('%d.%m.%Y') if obj.vytvorene else ""
        safe_meno = obj.meno or "(Bez mena)"
        return {
            'id': obj.id,
            'name': safe_meno,
            'search_value': safe_meno,
            'cat': 'UCHÁDZAČ',
            'email': obj.email or '',
            'telefon': obj.telefon or '',
            'date': datum_str,
            'rola': f"{obj.get_stav_display()} | {pozicia_str}"
        }

    # --- 🎨 ISAS UI DISPLAY METÓDY ---
    @admin.display(description="👤 Kandidát", ordering="meno")
    def get_kandidat_card(self, obj):
        safe_meno = escape(obj.meno or "(Bez mena)")
        datum_str = obj.vytvorene.strftime('%d.%m.%Y %H:%M') if obj.vytvorene else ""
        badges = []
        if obj.datum_pohovoru:
            pohovor_str = obj.datum_pohovoru.strftime('%d.%m.%Y %H:%M')
            badges.append(render_isas_badge(f"Pohovor: {pohovor_str}", icon="📅", type_color="warning", wrapper=False))
        suhlas_do = to_date(obj.suhlas_do)
        if not obj.gdpr_suhlas or (suhlas_do and suhlas_do < timezone.now().date()):
            badges.append(render_isas_badge("EXPIROVANÉ GDPR", icon="🚨", type_color="danger", wrapper=False))
            
        extra_html = render_isas_layout(mark_safe("".join(badges)), layout="row", gap=4, center=True) if badges else None
            
        return render_isas_avatar(
            icon="👤",
            title=safe_meno,
            subtitle=datum_str,
            bg_gradient="#6366f1, #4338ca",
            shadow_color="rgba(99, 102, 241, 0.3)",
            extra_html=extra_html
        )

    @admin.display(description="🎯 Pozícia", ordering="pozicia")
    def get_pozicia_badge(self, obj):
        if obj.pozicia and obj.pozicia.nazov:
            safe_nazov = escape(obj.pozicia.nazov)
            return render_isas_badge(safe_nazov, icon="🎯", type_color="info", wrapper=False)
        return render_isas_badge("Všeobecný záujem", icon="🌐", type_color="neutral", wrapper=False)

    @admin.display(description="👨‍💼 Zamestnanec v ERP", ordering="vytvoreny_zamestnanec")
    def get_zamestnanec_badge(self, obj):
        if obj.vytvoreny_zamestnanec:
            url = reverse('admin:Evidencia_zamestnanec_change', args=[obj.vytvoreny_zamestnanec.id])
            safe_cele_meno = escape(f"{obj.vytvoreny_zamestnanec.meno} {obj.vytvoreny_zamestnanec.priezvisko}".strip())
            return render_isas_link_btn(
                url=url,
                text=safe_cele_meno,
                icon="👨‍💼",
                type_color="success",
                wrapper=False,
                custom_style="padding: 3px 8px; font-size: 11px; font-weight: 700;"
            )
        if obj.stav == 'PRIJATY':
            return render_isas_badge("Čaká na preklopenie", icon="⏳", type_color="warning", wrapper=False)
        return render_isas_text("Nie je zamestnanec", muted=True)

    @admin.display(description="⭐ Hodnotenie", ordering="hodnotenie")
    def get_hodnotenie_stars(self, obj):
        if obj.hodnotenie is not None and obj.hodnotenie > 0:
            rating = max(1, min(int(obj.hodnotenie), 5))
            full_stars = '★' * rating
            empty_stars = '☆' * (5 - rating)
            return render_isas_badge(f"{full_stars}{empty_stars}", icon="⭐", type_color="warning", wrapper=False)
        return render_isas_text("Nehodnotený", muted=True)

    @admin.display(description="📞 Kontakt")
    def get_contact_card(self, obj):
        items = []
        if obj.telefon:
            safe_tel = escape(obj.telefon)
            items.append(render_isas_badge(safe_tel, icon="📞", copy_value=obj.telefon, wrapper=False))
        if obj.email:
            safe_email = escape(obj.email)
            items.append(render_isas_badge(safe_email, icon="✉️", copy_value=obj.email, wrapper=False))
        if not items:
            return render_isas_text("Bez kontaktu", muted=True)
        return render_isas_layout(format_html('{}', mark_safe("".join(items))), layout="col", gap=4, center=True)

    @admin.display(description="📄 Životopis (CV)")
    def get_cv_action(self, obj):
        if obj.zivotopis and obj.zivotopis.name:
            try:
                if hasattr(obj.zivotopis, 'url') and obj.zivotopis.url:
                    return render_isas_link_btn(
                        url=obj.zivotopis.url, 
                        text="Stiahnuť CV", 
                        icon="📥", 
                        type_color="success", 
                        wrapper=False,
                        custom_style="padding: 3px 8px; font-size: 10px;"
                    )
            except (ValueError, Exception):
                pass
        return render_isas_text("✖️ Bez prílohy", size=10, color="#94a3b8", weight=600)

    @admin.display(description="📅 Prijaté", ordering="vytvorene")
    def get_vytvorene(self, obj):
        if not obj.vytvorene:
            return render_isas_text("—", muted=True)
        datum_str = obj.vytvorene.strftime('%d.%m.%Y')
        cas_str = obj.vytvorene.strftime('%H:%M')
        return render_isas_badge(f"{datum_str} {cas_str}", icon="📅", type_color="neutral", wrapper=False)