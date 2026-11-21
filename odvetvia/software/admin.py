"""
odvetvia/software/admin.py

⚖️ ISAS IT & CLOUD COMPLIANCE ADMIN – ODVETVIE: SOFTWARE & IT INFRAŠTRUKTÚRA
Správa odvetvových IT lehôt (NIS2, ISO 27001, DR testy, SSL, domény) a národnej bezpečnostnej matice.
Plne integrované do ISAS Premium UI Component Factory, Detective Search a Fast Action API.
"""

import unicodedata

from django.apps import apps
from django.contrib import admin
from django.utils.html import escape
from django import forms
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.db.models.fields import NOT_PROVIDED
from django.http import JsonResponse
from django.utils.html import format_html

from ISASaplikacia.admin import ISASPremiumAdmin, ISASSingletonAdminMixin
from ISASaplikacia.utils import (
    render_isas_badge,
    render_isas_header_card,
    render_isas_singleton_card,
    render_isas_singleton_row,
    render_isas_layout,
    render_isas_key_value,
    render_isas_flex_between,
    render_isas_text,
)
from .models import SoftwareNastavenia, SoftwareStatLokalizacia
from .config import COMPLIANCE_ADMIN_MEDIA


# ==============================================================================
# ⚙️ 1. GLOBÁLNE ODVETVOVÉ IT NASTAVENIA (SINGLETON)
# ==============================================================================

@admin.register(SoftwareNastavenia)
class SoftwareNastaveniaAdmin(ISASSingletonAdminMixin, ISASPremiumAdmin):
    """
    🏛️ Singleton administrácia pre centrálnu konfiguráciu odvetvových IT lehôt a kyberbezpečnosti.
    Umožňuje IT manažérovi a CISO nastaviť predpísané periódy auditov bez prístupu k interným údajom ERP.
    """
    list_display = (
        'get_core_vizual',
        'get_bezpecnost_lehoty_styled',
        'get_dr_kontinualita_styled',
        'get_certifikaty_domeny_styled',
    )
    list_display_links = ('get_core_vizual',)

    isas_title = "⚙️ Nastavenia odvetvia: IT infraštruktúra, Cloud a Softvér"
    isas_preloader_text = "⏳ Načítavam odvetvovú konfiguráciu IT lehôt..."
    isas_enable_detective = False  # Singleton má iba 1 riadok, live search je vypnutý
    isas_magia_kwargs = {
        'typ_zobrazenia': 'karta',
        'live_search_endpoint': None,
        'vyzaduje_potvrdenie': False,
    }

    # 🚀 ONBOARDING GUARDIAN: Zaručí, že riadok Singletonu v DB vždy existuje a changelist nikdy nie je prázdny
    def changelist_view(self, request, extra_context=None):
        self.model.get_solo()
        return super().changelist_view(request, extra_context=extra_context)

    # 🛡️ BEZPEČNOSTNÝ ZÁMOK (Tlačidlo v UI ostáva, mazanie je 100% zablokované)
    def delete_model(self, request, obj):
        raise PermissionDenied("❌ Zmazanie odvetvových nastavení je prísne zakázané!")

    def delete_queryset(self, request, queryset):
        raise PermissionDenied("❌ Hromadné mazanie odvetvových nastavení je prísne zakázané!")

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        res = super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)
        if res:
            return res
        if action_type == 'delete':
            return JsonResponse({
                "status": "error",
                "message": "❌ Zmazanie odvetvových nastavení je z bezpečnostných dôvodov prísne zakázané!"
            }, status=403)
        return None

    fieldsets = (
        ('🛡️ 1. KYBERNETICKÁ BEZPEČNOSŤ & AUDITY (NIS2 / ISO 27001)', {
            'description': '<b>⚖️ Smernica NIS2 & ISO/IEC 27001:</b> Predpísané periódy externých bezpečnostných auditov a periodických penetračných testov systémov.',
            'fields': (
                ('lehota_audit_nis2_roky', 'lehota_audit_iso27001_roky'),
                'lehota_penetracny_test_roky',
            )
        }),
        ('💾 2. KONTINUITA PREVÁDZKY & DISASTER RECOVERY (BCP / DR)', {
            'description': '<b>🧪 Testovanie obnovy dát:</b> Periodicita Disaster Recovery cvičení, overenia záloh a rotácie prístupových poverení (SSH, API kľúče).',
            'fields': (
                ('lehota_zalohovanie_dr_test_mesiace', 'lehota_rotacia_pristupov_dni'),
            )
        }),
        ('🔒 3. DIGITÁLNA IDENTITA (SSL / TLS & INTERNETOVÉ DOMÉNY)', {
            'description': '<b>🌐 Upozornenia na expirácie:</b> Predstih vo vyhlásení varovného alarmu pred vypršaním produkčných certifikátov a doménových mien.',
            'fields': (
                ('lehota_ssl_upozornenie_dni', 'lehota_domena_upozornenie_dni'),
            )
        }),
    )

    @property
    def media(self):
        """Dynamické pripojenie doložky o zodpovednosti z odvetvovej konfigurácie."""
        base_media = super().media
        if COMPLIANCE_ADMIN_MEDIA and isinstance(COMPLIANCE_ADMIN_MEDIA, dict):
            base_media = base_media + forms.Media(
                css=COMPLIANCE_ADMIN_MEDIA.get('css'),
                js=COMPLIANCE_ADMIN_MEDIA.get('js')
            )
        return base_media

    # 🛡️ DEFENSIVE FORMATTERS: Ochrana pred None a NOT_PROVIDED hodnotami
    def _val_r(self, obj, field_name):
        val = getattr(obj, field_name, None)
        if val is None:
            try:
                def_val = self.model._meta.get_field(field_name).default
                val = def_val if def_val is not NOT_PROVIDED else "—"
            except Exception:
                val = "—"
        return f"{val} r." if val != "—" and val is not None else "—"

    def _val_m(self, obj, field_name):
        val = getattr(obj, field_name, None)
        if val is None:
            try:
                def_val = self.model._meta.get_field(field_name).default
                val = def_val if def_val is not NOT_PROVIDED else "—"
            except Exception:
                val = "—"
        return f"{val} mes." if val != "—" and val is not None else "—"

    def _val_d(self, obj, field_name):
        val = getattr(obj, field_name, None)
        if val is None:
            try:
                def_val = self.model._meta.get_field(field_name).default
                val = def_val if def_val is not NOT_PROVIDED else "—"
            except Exception:
                val = "—"
        return f"{val} dní" if val != "—" and val is not None else "—"

    # --- 🎨 KARTY DO CHANGELISTU (ISAS COMPONENT FACTORY) ---

    @admin.display(description='⚙️ Odvetvie')
    def get_core_vizual(self, obj):
        badge = render_isas_badge("AKTÍVNE ODVETVIE", "💻", "info", wrapper=False)
        desc = render_isas_text("IT, Cloud & Softvér", size=11, color="#0f172a", weight=800)
        obsah = render_isas_layout(format_html('{}{}', badge, desc), layout="col", gap=6)
        return render_isas_header_card("💻", "SOFTWARE", obsah)

    @admin.display(description='🛡️ Kyberbezpečnosť & Audity')
    def get_bezpecnost_lehoty_styled(self, obj):
        r1 = render_isas_singleton_row(render_isas_layout(format_html('{}{}',
            render_isas_flex_between("Audit NIS2:", self._val_r(obj, 'lehota_audit_nis2_roky')),
            render_isas_flex_between("Audit ISO 27001:", self._val_r(obj, 'lehota_audit_iso27001_roky'))
        ), layout="col", gap=4), is_dashed=False)

        r2 = render_isas_singleton_row(
            render_isas_flex_between("Penetračný test:", self._val_r(obj, 'lehota_penetracny_test_roky'))
        )
        return render_isas_singleton_card("Kyberbezpečnosť & Audity", "🛡️", "#2563eb", format_html('{}{}', r1, r2))

    @admin.display(description='💾 Disaster Recovery & Údržba')
    def get_dr_kontinualita_styled(self, obj):
        r1 = render_isas_singleton_row(
            render_isas_flex_between("DR test obnovy:", self._val_m(obj, 'lehota_zalohovanie_dr_test_mesiace')),
            is_dashed=False
        )
        r2 = render_isas_singleton_row(
            render_isas_flex_between("Rotácia prístupov:", self._val_d(obj, 'lehota_rotacia_pristupov_dni'))
        )
        return render_isas_singleton_card("Kontinuita & BCP", "💾", "#0ea5e9", format_html('{}{}', r1, r2))

    @admin.display(description='🔒 SSL Certifikáty & Domény')
    def get_certifikaty_domeny_styled(self, obj):
        r1 = render_isas_singleton_row(
            render_isas_flex_between("Alert SSL certifikátu:", self._val_d(obj, 'lehota_ssl_upozornenie_dni')),
            is_dashed=False
        )
        r2 = render_isas_singleton_row(
            render_isas_flex_between("Alert expirácie domény:", self._val_d(obj, 'lehota_domena_upozornenie_dni'))
        )
        return render_isas_singleton_card("Digitálna identita", "🔒", "#10b981", format_html('{}{}', r1, r2))


# ==============================================================================
# 🗺️ 2. NÁRODNÉ IT LEHOTY (MULTI-COUNTRY MATICA)
# ==============================================================================

@admin.register(SoftwareStatLokalizacia)
class SoftwareStatLokalizaciaAdmin(ISASPremiumAdmin):
    """
    🗺️ Administrácia špecifických národných lehôt kybernetickej bezpečnosti pre jednotlivé štáty.
    Umožňuje nadradiť globálne odvetvové lehoty predpismi danej krajiny (napr. transpozícia NIS2).
    """
    list_display = (
        'get_stat_badge',
        'get_kyber_lehoty_styled',
        'get_operativa_styled',
    )
    list_display_links = ('get_stat_badge',)
    list_filter = ('stat__je_clen_eu',)
    autocomplete_fields = ['stat']
    search_fields = ['stat__nazov', 'stat__iso_kod', 'stat__aliasy']
    ordering = ['stat__iso_kod']

    isas_title = "🗺️ Národné lehoty IT a kybernetickej bezpečnosti"
    isas_preloader_text = "⏳ Načítavam národnú IT legislatívnu maticu..."
    isas_delete_success_message = "✅ Úspešne odstránené pravidlo národných IT lehôt!"
    isas_autocomplete_fk_fields = {'stat': '🗺️ Vyberte štát (krajinu pôsobnosti)'}
    isas_magia_kwargs = {
        'search_placeholder': "🔍 Vyhľadávanie...",
    }

    # 🚀 ZERO N+1 OPTIMALIZÁCIA: Okamžité spojenie tabuliek jedným SQL dopytom
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('stat')

    # 🛡️ INTEGRITY GUARD: Vylúčenie už priradených štátov pri pridávaní (100% ochrana pred duplicitou)
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "stat" and request and "add" in request.path:
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            kwargs["queryset"] = StatLokalizaciaModel.objects.exclude(software_lehoty__isnull=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    # 🛡️ OCHRANA SÍDLA FIRMY: Zákaz vymazania pravidla pre štát, v ktorom sídli materská spoločnosť
    def _over_ochranu_sidla(self, obj):
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            aktivne_iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else None
            if aktivne_iso and aktivne_iso not in ('NEZADANE', 'INYKRAJINA') and obj.stat and obj.stat.iso_kod:
                if str(obj.stat.iso_kod).strip().upper() == str(aktivne_iso).strip().upper():
                    return f"🛑 Štát '{obj.stat.iso_kod}' je sídlom materskej firmy. Národné odvetvové lehoty sídla je zakázané vymazať!"
        except Exception:
            pass
        return None

    def has_delete_permission(self, request, obj=None):
        if obj and self._over_ochranu_sidla(obj):
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        dovod = self._over_ochranu_sidla(obj)
        if dovod:
            raise PermissionDenied(dovod)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            dovod = self._over_ochranu_sidla(obj)
            if dovod:
                raise PermissionDenied(dovod)
        super().delete_queryset(request, queryset)

    def fast_action_custom_validation(self, request, action_type, valid_ids, **kwargs):
        res = super().fast_action_custom_validation(request, action_type, valid_ids, **kwargs)
        if res:
            return res
        if action_type == 'delete' and valid_ids:
            for obj in self.get_queryset(request).filter(pk__in=valid_ids):
                dovod = self._over_ochranu_sidla(obj)
                if dovod:
                    return JsonResponse({"status": "error", "message": dovod}, status=403)
        return None

    # 🕵️ ISAS DETECTIVE LIVE SEARCH: 100% diakriticky a typovo odolné vyhľadávanie
    def get_detective_queryset(self, request, term):
        if not term or not str(term).strip():
            return self.get_queryset(request).none()

        term_str = str(term).strip()
        term_clean = unicodedata.normalize('NFKD', term_str).encode('ascii', 'ignore').decode('utf-8').strip().lower()

        filtre = (
            Q(stat__nazov__icontains=term_str) |
            Q(stat__iso_kod__iexact=term_str) |
            Q(stat__iso_kod__icontains=term_str) |
            Q(stat__aliasy__icontains=term_str)
        )
        if term_clean:
            filtre |= Q(stat__aliasy__icontains=term_clean)

        term_num = term_str.rstrip('rmd. ').strip()
        if term_num.isdigit():
            cislo = int(term_num)
            filtre |= (
                Q(lehota_audit_nis2_roky=cislo) |
                Q(lehota_zalohovanie_dr_test_mesiace=cislo) |
                Q(lehota_penetracny_test_roky=cislo) |
                Q(lehota_rotacia_pristupov_dni=cislo)
            )

        qs = self.get_queryset(request).filter(filtre)
        if term_clean and not qs.exists():
            qs = self.get_queryset(request).filter(
                Q(stat__nazov__icontains=term_clean) |
                Q(stat__aliasy__icontains=term_clean)
            )
        return qs.order_by('stat__nazov')[:10]

    def format_detective_result(self, obj):
        iso = getattr(obj.stat, 'iso_kod', '??') if obj.stat else '??'
        nazov = getattr(obj.stat, 'nazov', str(obj.stat)) if obj.stat else 'Neznámy štát'

        # 🛡️ DYNAMICKÝ REKAPITULAČNÝ RIADOK VŠETKÝCH NÁRODNÝCH IT LEHÔT
        nis2 = f"{obj.lehota_audit_nis2_roky}r" if obj.lehota_audit_nis2_roky is not None else "Globál"
        pent = f"{obj.lehota_penetracny_test_roky}r" if obj.lehota_penetracny_test_roky is not None else "Globál"
        dr = f"{obj.lehota_zalohovanie_dr_test_mesiace}m" if obj.lehota_zalohovanie_dr_test_mesiace is not None else "Globál"
        rot = f"{obj.lehota_rotacia_pristupov_dni}d" if obj.lehota_rotacia_pristupov_dni is not None else "Globál"

        detail_lehoty = f"NIS2:{nis2} | Pentest:{pent} | DR:{dr} | Kľúče:{rot}"
        vsetky_hodnoty = f"{iso} {nazov} {getattr(obj.stat, 'aliasy', '') or ''} {detail_lehoty}"

        return {
            'id': obj.id,
            'name': escape(f"{iso} - {nazov}"),
            'search_value': escape(vsetky_hodnoty),
            'rola': escape(f"🛡️ {detail_lehoty}"),
            'cat': '🗺️ LEHOTY IT',
        }

    fieldsets = (
        ('🗺️ ŠTÁT A PÔSOBNOSŤ', {
            'description': 'Zvoľte štát, pre ktorý definujete špecifické zákonné lehoty IT bezpečnosti.',
            'fields': ('stat',)
        }),
        ('🛡️ NÁRODNÉ LEHOTY KYBERBEZPEČNOSTI (NIS2 & PENTEST)', {
            'description': 'Lehoty povinných auditov podľa národnej legislatívy v rokoch (ak necháte prázdne, použije sa globálna hodnota).',
            'fields': (
                ('lehota_audit_nis2_roky', 'lehota_penetracny_test_roky'),
            )
        }),
        ('💾 KONTINUITA & OPERATÍVNA BEZPEČNOSŤ', {
            'description': 'Periodicita Disaster Recovery testovania záloh a povinná rotácia kľúčov v regulovanom sektore.',
            'fields': (
                ('lehota_zalohovanie_dr_test_mesiace', 'lehota_rotacia_pristupov_dni'),
            )
        }),
    )

    @admin.display(description='🗺️ Štát', ordering='stat__iso_kod')
    def get_stat_badge(self, obj):
        if not obj.stat:
            return render_isas_badge("Nezadaný štát", icon="❓", type_color="danger")
        iso = getattr(obj.stat, 'iso_kod', '??')
        nazov = getattr(obj.stat, 'nazov', str(obj.stat))
        return render_isas_badge(f"{iso} - {nazov}", icon="🗺️", type_color="info")

    @admin.display(description='🛡️ Audity (NIS2 / Pentest)')
    def get_kyber_lehoty_styled(self, obj):
        has_kyber = any(getattr(obj, f, None) is not None for f in [
            'lehota_audit_nis2_roky', 'lehota_penetracny_test_roky'
        ])
        if not has_kyber:
            badge = render_isas_badge("Globálne lehoty", icon="⚙️", type_color="neutral", wrapper=False)
            popis = render_isas_text("Podľa odvetvia", size=10, color="#94a3b8", muted=True)
            return render_isas_layout(format_html('{}{}', badge, popis), layout="col", center=True, gap=2)

        nis2 = f"{obj.lehota_audit_nis2_roky} r." if obj.lehota_audit_nis2_roky is not None else "—"
        pent = f"{obj.lehota_penetracny_test_roky} r." if obj.lehota_penetracny_test_roky is not None else "—"
        txt = format_html(
            '<span style="font-weight:800;color:#1e40af;">NIS2: {}</span> / '
            '<span style="font-weight:800;color:#b91c1c;">Pentest: {}</span>',
            nis2, pent
        )
        return render_isas_text(txt, size=11, align="center")

    @admin.display(description='💾 Prevádzka (DR test / Rotácia kľúčov)')
    def get_operativa_styled(self, obj):
        has_op = any(getattr(obj, f, None) is not None for f in [
            'lehota_zalohovanie_dr_test_mesiace', 'lehota_rotacia_pristupov_dni'
        ])
        if not has_op:
            badge = render_isas_badge("Globálne lehoty", icon="⚙️", type_color="neutral", wrapper=False)
            popis = render_isas_text("Podľa odvetvia", size=10, color="#94a3b8", muted=True)
            return render_isas_layout(format_html('{}{}', badge, popis), layout="col", center=True, gap=2)

        dr = f"{obj.lehota_zalohovanie_dr_test_mesiace} mes." if obj.lehota_zalohovanie_dr_test_mesiace is not None else "—"
        rot = f"{obj.lehota_rotacia_pristupov_dni} dní" if obj.lehota_rotacia_pristupov_dni is not None else "—"
        txt = format_html(
            '<span style="font-weight:800;color:#15803d;">💾 DR: {}</span> | '
            '<span style="font-weight:800;color:#b45309;">🔑 Kľúče: {}</span>',
            dr, rot
        )
        return render_isas_text(txt, size=11, align="center")