import uuid
import datetime
from decimal import Decimal

from django.contrib.admin.helpers import ActionForm
from django.contrib.admin.widgets import AdminSplitDateTime
from django.utils import timezone
from django import forms
from django.apps import apps
from django.conf import settings
from django.urls import reverse, NoReverseMatch
from django.utils.safestring import mark_safe
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType

from .utils_helper import ocisti_nazov_od_jednotky, get_firemna_mena
from .utils_compliance import to_date

from .models import (
    ServisovaneZariadenie, Zamestnanec, Filialka, Sklad, ERPExportNastavenia,
    PouzityMaterial, PouzityMaterialVZaznamoch, Zakaznik, Dodavatel,
    EvidenciaUhrad, ServisnyZaznam, Uloha, Nepritomnost, Tankovanie, PovolenieNaVstup
)

# 🔄 SPÄTNÁ KOMPATIBILITA: Re-export odvetvových mapovacích štruktúr z industry_loader.py
# Žiadny iný súbor v projekte nemusí meniť existujúce importy z Evidencia.forms!
from .industry_loader import (
    PARENT_MODELS,
    ZARIADENIA_MAP,
    ZARIADENIA_CHOICES,
    CATEGORY_MAP,
    CATEGORY_CHOICES,
    FLAT_CATEGORY_MAP,
)

class UpdateStockForm(forms.Form):
    filialka = forms.ModelChoiceField(
        queryset=Filialka.objects.filter(je_aktivna=True),
        required=False,
        empty_label="🏢 Hlavný sklad",
        label="🏬 Sklad / Filiálka / Dodávka"
    )
    typ_dokladu_erp = forms.ChoiceField(
        choices=[
            ('PRIJEMKA', '📥 Príjemka (Nákup od dodávateľa)'),
            ('VYDAJKA', '📤 Výdajka (Výdaj na faktúru/predaj)'),
            ('SPOTREBA', '🔧 Vnútropodniková spotreba (Servis/Údržba)'),
            ('PREVODKA', '🚚 Prevodka (Medzi filiálkami)'),
            ('INVENTURA', '📋 Inventúrne manko/prebytok'),
        ],
        label="📑 Typ pohybu / Doklad",
        required=True
    )
    category = forms.ChoiceField(choices=CATEGORY_CHOICES, label="📁 Hlavná kategória")
    subcategory = forms.ChoiceField(choices=[], label="📂 Podkategória", required=False)
    product = forms.ChoiceField(choices=[], label="📦 Diel/Položka")
    count = forms.IntegerField(label="🔢 Počet kusov", min_value=1, max_value=999999)

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if user and not user.is_superuser and hasattr(user, 'zamestnanec'):
            self.fields['filialka'].queryset = Filialka.objects.filter(
                je_aktivna=True, 
                priradeni_zamestnanci=user.zamestnanec
            )

        try:
            hlavny_sklad = Sklad.objects.first()
            if hlavny_sklad:
                nazov = str(hlavny_sklad.nazov or '').strip()
                self.fields['filialka'].empty_label = f"🏢 Hlavný sklad ({nazov})" if nazov and nazov.lower() != "hlavný sklad" else "🏢 Hlavný sklad"
        except Exception:
            pass

        self.fields['filialka'].widget.attrs.update({'class': 'premium-input'})
        
        raw_category = self.data.get("category") or self.initial.get("category")
        raw_subcategory = self.data.get("subcategory") or self.initial.get("subcategory")
        
        category = str(raw_category) if raw_category is not None else None
        subcategory = str(raw_subcategory) if raw_subcategory is not None else None

        if category in CATEGORY_MAP:
            self.fields["subcategory"].choices = [(s, s) for s in CATEGORY_MAP[category].keys()]
        else:
            self.fields["subcategory"].choices = []
            
        if category in CATEGORY_MAP and subcategory in CATEGORY_MAP[category]:
            model_class = CATEGORY_MAP[category][subcategory]
            
            choices = []
            for obj in model_class.objects.all().iterator():
                raw_sc = getattr(obj, 'seriove_cislo', None)
                sc = raw_sc if (raw_sc is not None and str(raw_sc).strip()) else "❓ Neznáme SN"
                
                raw_pocet = getattr(obj, 'pocet_na_sklade', None)
                pocet = raw_pocet if raw_pocet is not None else 0
                
                mj = getattr(obj, 'merna_jednotka', None) or 'ks'
                choices.append((str(obj.pk), f"🏷️ SN: {sc} | 📦 Skladom: {pocet} {mj}"))
                
            self.fields["product"].choices = choices
        else:
            self.fields["product"].choices = []

class TlacidloWidget(forms.Widget):
    def render(self, name, value, attrs=None, renderer=None):
        if value:
            try:
                url = reverse('evidencia:konfigurator_dielov', args=[value])
                uid = uuid.uuid4().hex[:8]
                
                return mark_safe(
                    f'<div class="isas-parts-widget-container">'
                    f'<a href="{url}" class="isas-parts-config-btn" data-uid="{uid}">'
                    f'<span>🛠️</span> DIELY / POLOŽKY'
                    f'</a>'
                    f'<span id="isas-reset-{uid}" class="isas-parts-reset-btn" data-uid="{uid}" title="Kliknutím znovu zapnete ochranu pred odchodom z formulára 🛡️.">'
                    f'<span>🔄 Zapnúť upozornenie</span>'
                    f'<span>o ukladaní dát vo formulári</span>'
                    f'</span>'
                    f'</div>'
                )
            except NoReverseMatch:
                pass
                
        return mark_safe('<span style="color: #64748b; font-size: 11px; font-style: italic;">⚠️ Najprv uložte zariadenie</span>')

class PlaceholderSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value == '':
            option['attrs']['disabled'] = True
            option['attrs']['hidden'] = True
        return option

class ServisovaneZariadenieForm(forms.ModelForm):
    sprava_dielov = forms.CharField(label="🛠️ Správa dielov", required=False, widget=TlacidloWidget())

    class Meta:
        model = ServisovaneZariadenie
        exclude = ['qr_kod']

        widgets = {
            "kategoria": PlaceholderSelect(),
            
            "poznamka": forms.Textarea(attrs={
                "rows": 2,
                "placeholder": "📝 Zadajte poznámku k zariadeniu..."
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial['sprava_dielov'] = self.instance.pk

        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            defaults = FiremnaIdentitaModel.get_country_defaults() if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
            identita = FiremnaIdentitaModel.objects.first()
            mena = getattr(identita, 'predvolena_mena', None) or defaults.get('mena') or get_firemna_mena()
        except Exception:
            mena = get_firemna_mena()

        if 'cena_prenajmu_na_den' in self.fields:
            self.fields['cena_prenajmu_na_den'].label = f"💰 Cena prenájmu na deň ({mena})"
            if self.fields['cena_prenajmu_na_den'].help_text and '€' in self.fields['cena_prenajmu_na_den'].help_text:
                self.fields['cena_prenajmu_na_den'].help_text = self.fields['cena_prenajmu_na_den'].help_text.replace('(€)', f'({mena})').replace('€', mena)

class ZamestnanecForm(forms.ModelForm):
    class Meta:
        model = Zamestnanec
        exclude = ['user']

class UniverzalnyDielForm(forms.ModelForm):
    """
    ISAS ENGINE: Jeden formulár vládne všetkým. 
    Aplikuje sa dynamicky na každý vygenerovaný inline v sklade.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        if 'kto_som' in self.fields:
            self.fields['kto_som'].widget = forms.Textarea(attrs={
                "rows": 1, # Znížené na 1, CSS drží min-height 40px
                "placeholder": "ℹ️ Základné info..."
            })

        for field_name in ['typ', 'kategoria', 'oslovenie_rod']:
            if field_name in self.fields and hasattr(self.fields[field_name], 'choices'):
                povodne_choices = [c for c in self.fields[field_name].choices if c[0] not in ('', None)]
                self.fields[field_name].choices = [('', '🔽 Vyberte')] + povodne_choices

        if 'merna_jednotka' in self.fields and hasattr(self.fields['merna_jednotka'], 'choices'):
            povodne_jednotky = [c for c in self.fields['merna_jednotka'].choices if c[0] not in ('', None)]
            self.fields['merna_jednotka'].choices = povodne_jednotky

        if 'sadzba_dph' in self.fields and (not self.instance or not self.instance.pk or getattr(self.instance, 'sadzba_dph', None) is None):
            try:
                GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                self.fields['sadzba_dph'].initial = GlobalneNastaveniaModel.get_zakladna_dph()
            except Exception:
                pass

        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            defaults = FiremnaIdentitaModel.get_country_defaults() if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
            identita = FiremnaIdentitaModel.objects.first()
            mena = getattr(identita, 'predvolena_mena', None) or defaults.get('mena') or get_firemna_mena()
        except Exception:
            mena = get_firemna_mena()

        for f_name in ['nakupna_cena_bez_dph', 'predajna_cena_bez_dph', 'predajna_cena_s_dph', 'nakupna_cena', 'predajna_cena', 'cena']:
            if f_name in self.fields:
                if self.fields[f_name].label and '€' in self.fields[f_name].label:
                    self.fields[f_name].label = self.fields[f_name].label.replace('(€)', f'({mena})').replace('€', mena)
                if self.fields[f_name].help_text and '€' in self.fields[f_name].help_text:
                    self.fields[f_name].help_text = self.fields[f_name].help_text.replace('(€)', f'({mena})').replace('€', mena)

class ERPAccountantForm(forms.ModelForm):
    """Formulár pre účtovníčku na konfiguráciu ERP exportu."""
    class Meta:
        model = ERPExportNastavenia
        exclude = ['auto_odosielat_mailom', 'backup_data']
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs['class'] = 'isas-input'

        # 🌍 STRIKTNÁ LOKALIZÁCIA PODĽA SÍDLA SPOLOČNOSTI
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
            iso = str(iso).strip().upper() if iso else ''
        except Exception:
            iso = ''

        # KROS Omega je povolená VÝHRADNE pre subjekty v SR (SK)
        if iso != 'SK':
            omega_fields = [
                'omega_evidencia_banka', 'omega_ciselny_rad_banka', 
                'omega_evidencia_pokladna', 'omega_ciselny_rad_pokladna_prijem', 
                'omega_ciselny_rad_pokladna_vydaj', 'omega_evidencia_id', 
                'omega_ciselny_rad_id'
            ]
            for f in omega_fields:
                if f in self.fields:
                    del self.fields[f]

        # Pre krajiny mimo SK a CZ lokálne ERP (Pohoda ani Omega) neexistuje – fakturácia ide cez Peppol
        if iso not in ['SK', 'CZ']:
            pohoda_fields = [
                'pohoda_predkontacia_fa', 'pohoda_skratka_banky',
                'pohoda_predkontacia_banka_prijem', 'pohoda_predkontacia_banka_vydaj',
                'pohoda_predkontacia_sluzby', 'pohoda_predkontacia_tovar',
                'pohoda_predkontacia_prijata_fa'
            ]
            for f in pohoda_fields:
                if f in self.fields:
                    del self.fields[f]

        try:
            GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
            zakl_dph_val = GlobalneNastaveniaModel.get_zakladna_dph()
        except Exception:
            try:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                defaults = FiremnaIdentitaModel.get_country_defaults(iso) if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
                zakl_dph_val = defaults.get('dph_zakladna', Decimal('0.00'))
            except Exception:
                zakl_dph_val = Decimal('0.00')

        if 'ucet_dph_20' in self.fields:
            self.fields['ucet_dph_20'].label = f"⚖️ Účet pre základnú sadzbu DPH {zakl_dph_val}% ({iso}):"

        if iso == 'CZ':
            if 'erp_system' in self.fields:
                self.fields['erp_system'].choices = [
                    ('POHODA', '🟢 Stormware Pohoda (.xml)')
                ]
                self.initial['erp_system'] = 'POHODA'
            if 'ucet_pokladna' in self.fields and (not self.instance or not self.instance.pk or getattr(self.instance, 'ucet_pokladna', None) in ['pokladňa', '211000', '', None]):
                self.fields['ucet_pokladna'].initial = 'pokladna'
            if 'ucet_dph_20' in self.fields and (not self.instance or not self.instance.pk or getattr(self.instance, 'ucet_dph_20', None) in ['343023', '343020', '', None]):
                self.fields['ucet_dph_20'].initial = '343021'
        elif iso == 'SK':
            if 'ucet_dph_20' in self.fields and (not self.instance or not self.instance.pk or getattr(self.instance, 'ucet_dph_20', None) in ['343020', '343021', '', None]):
                self.fields['ucet_dph_20'].initial = '343023'
        elif iso not in ['SK', 'CZ']:
            if 'erp_system' in self.fields:
                self.fields['erp_system'].choices = [
                    ('', f"⚠️ Lokálne ERP nie je pre sídlo '{iso}' podporované (Použite Peppol)")
                ]

    def clean(self):
        cleaned_data = super().clean()
        erp = cleaned_data.get('erp_system')
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
            iso = str(iso).strip().upper() if iso else ''
        except Exception:
            iso = ''

        if erp == 'OMEGA' and iso != 'SK':
            self.add_error('erp_system', f"🛑 KROS Omega je určená výhradne pre subjekty so sídlom v SR (SK). Vaša spoločnosť má sídlo v '{iso}'.")
        elif erp == 'POHODA' and iso not in ['SK', 'CZ']:
            self.add_error('erp_system', f"🛑 Stormware Pohoda je určená výhradne pre subjekty so sídlom v SK a CZ. Vaša spoločnosť má sídlo v '{iso}'.")
        elif iso not in ['SK', 'CZ']:
            self.add_error('erp_system', f"🛑 Lokálne ERP systémy nie sú pre sídlo '{iso}' podporované. Fakturácia prebieha výhradne cez sieť Peppol.")
        return cleaned_data

class BasePouzityMaterialForm(forms.ModelForm):
    """Základný formulár obsahujúci 100% zdieľanej logiky pre použité materiály."""
    hlavna_kategoria = forms.ChoiceField(
        label="📂 Kategória", 
        choices=[('', '---------')], 
        required=False
    )
    object_id = forms.CharField(
        label="📦 Názov dielu/položky", 
        required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        if 'hlavna_kategoria' in self.fields:
            self.fields['hlavna_kategoria'].widget = forms.Select(attrs={'class': 'kategoria-select isas-native-select'})
            self.fields['hlavna_kategoria'].choices = [('', '📂 Vyberte kategóriu...')] + CATEGORY_CHOICES
        
        if 'content_type' in self.fields:
            ct_attrs = {'class': 'subkategoria-select isas-native-select'}
            if not self.instance.pk or not self.instance.content_type_id:
                ct_attrs['disabled'] = 'disabled'
                
            self.fields['content_type'].widget = forms.Select(attrs=ct_attrs)
            self.fields['content_type'].label = "📑 Podkategória"
            
            if not hasattr(self.__class__, '_ct_choices_cache') or self.__class__._ct_choices_cache is None:
                choices = [('', '🔒 Najskôr vyberte kategóriu...')]
                for main_cat, subcats in CATEGORY_MAP.items():
                    for subcat_name, model_cls in subcats.items():
                        ct = ContentType.objects.get_for_model(model_cls)
                        choices.append((ct.id, f"{main_cat}::{subcat_name}"))
                self.__class__._ct_choices_cache = choices
                
            self.fields['content_type'].choices = self.__class__._ct_choices_cache

        if 'object_id' in self.fields:
            sklad_attrs = {'class': 'sklad-select2', 'style': 'width: 100%;'}
            if not self.instance.pk or not self.instance.content_type_id:
                sklad_attrs['disabled'] = 'disabled'
                self.fields['object_id'].widget = forms.Select(
                    choices=[('', '🔒 Najskôr vyberte podkategóriu...')], 
                    attrs=sklad_attrs
                )
            else:
                self.fields['object_id'].widget = forms.Select(attrs=sklad_attrs)

        if 'pocet' in self.fields and (not self.instance.pk or not self.instance.content_type_id):
            self.fields['pocet'].widget.attrs['disabled'] = 'disabled'

        if self.instance.pk and self.instance.content_type_id:
            try:
                ct_model = self.instance.content_type.model_class() if self.instance.content_type else None
                for mc, subcats in CATEGORY_MAP.items():
                    if ct_model in subcats.values():
                        self.initial['hlavna_kategoria'] = mc
                        break
            except Exception:
                pass
        
        if self.instance.pk and self.instance.object_id and self.instance.content_type_id:
            try:
                obj = self.instance.content_object
                if obj:
                    cisty_nazov = ocisti_nazov_od_jednotky(obj)
                    merna_jednotka = getattr(obj, 'merna_jednotka', None) or 'ks'

                    self.fields['object_id'].choices = [(str(obj.id), cisty_nazov)]
                    self.fields['object_id'].widget.choices = [(str(obj.id), cisty_nazov)]
                    self.initial['object_id'] = str(obj.id)
                    self.fields['object_id'].widget.attrs['data-pre-label'] = cisty_nazov
                    self.fields['object_id'].widget.attrs['data-pre-val'] = str(obj.id)
                    self.fields['object_id'].widget.attrs['data-pre-unit'] = merna_jednotka
                    self.fields['object_id'].widget.attrs['data-unit'] = merna_jednotka
            except Exception:
                pass

    def clean_object_id(self):
        oid = self.cleaned_data.get('object_id')
        if oid in [None, '', 'None']: 
            return None
        try:
            return int(oid)
        except ValueError:
            raise forms.ValidationError("⚠️ Neplatné ID položky zo skladu. Prosím, zvoľte položku zo zoznamu.")

    def clean(self):
        cleaned_data = super().clean()
        hlavna = cleaned_data.get('hlavna_kategoria')
        ct = cleaned_data.get('content_type')
        oid = cleaned_data.get('object_id')

        if hlavna and (not ct or not oid):
            if not ct: 
                self.add_error('content_type', '📑 Zabudli ste zvoliť podkategóriu.')
            if not oid: 
                self.add_error('object_id', '🔍 Musíte vybrať konkrétny diel zo skladu na dokončenie výberu.')
                
        if ct and oid:
            try:
                model_class = ct.model_class()
                # 🛡️ ISAS SECURITY AUDIT: Whitelist povolených modelov z CATEGORY_MAP
                povolene_modely = {m for sub in CATEGORY_MAP.values() for m in sub.values()}
                if model_class not in povolene_modely:
                    self.add_error('content_type', '⛔ Zvolený typ položky nepatrí medzi autorizované skladové položky ISAS.')
                elif not model_class.objects.filter(pk=oid).exists():
                    self.add_error('object_id', '⚠️ Vybraný diel už neexistuje v databáze (bol pravdepodobne vymazaný).')
            except Exception:
                self.add_error('object_id', '⚠️ Chyba pri overovaní integrity prepojenej položky.')

        pocet = cleaned_data.get('pocet')
        if pocet is not None and pocet <= 0:
            self.add_error('pocet', '⚠️ Počet použitých kusov/jednotiek musí byť väčší ako nula.')
                
        return cleaned_data

class PouzityMaterialForm(BasePouzityMaterialForm):
    class Meta:
        model = PouzityMaterial
        fields = ['hlavna_kategoria', 'content_type', 'object_id', 'pocet']

class PouzityMaterialVZaznamochForm(BasePouzityMaterialForm):
    class Meta:
        model = PouzityMaterialVZaznamoch
        fields = ['hlavna_kategoria', 'content_type', 'object_id', 'pocet']

class EvidenciaUhradForm(forms.ModelForm):
    pomocny_zakaznik = forms.ModelChoiceField(
        queryset=Zakaznik.objects.all(),
        required=False,
        empty_label="",
        label="🏢 1. KROK: Filter zákazníka (Predkrok)",
        help_text="💡 Vyberte zákazníka. Následne sa odomkne pole pre Vystavenú faktúru.",
        widget=forms.Select(attrs={'class': 'isas-zakaznik-filter'})
    )
    pomocny_dodavatel = forms.ModelChoiceField(
        queryset=Dodavatel.objects.all(),
        required=False,
        empty_label="",
        label="🏢 1. KROK: Filter dodávateľa (Predkrok)",
        help_text="💡 Vyberte dodávateľa. Následne sa odomkne pole pre Prijatú faktúru.",
        widget=forms.Select(attrs={'class': 'isas-dodavatel-filter'})
    )

    class Meta:
        model = EvidenciaUhrad
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            defaults = FiremnaIdentitaModel.get_country_defaults() if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
            identita = FiremnaIdentitaModel.objects.first()
            mena = getattr(identita, 'predvolena_mena', None) or defaults.get('mena') or get_firemna_mena()
        except Exception:
            mena = get_firemna_mena()

        faktura_mena = None
        if self.instance and self.instance.pk:
            if getattr(self.instance, 'vystavena_faktura', None):
                faktura_mena = getattr(self.instance.vystavena_faktura, 'mena', None)
                if getattr(self.instance.vystavena_faktura, 'zakaznik_id', None) and 'pomocny_zakaznik' in self.fields:
                    self.initial['pomocny_zakaznik'] = self.instance.vystavena_faktura.zakaznik_id
            elif getattr(self.instance, 'prijata_faktura', None):
                faktura_mena = getattr(self.instance.prijata_faktura, 'mena', None)
                if getattr(self.instance.prijata_faktura, 'dodavatel_id', None) and 'pomocny_dodavatel' in self.fields:
                    self.initial['pomocny_dodavatel'] = self.instance.prijata_faktura.dodavatel_id

        akt_mena = (self.instance.mena if (self.instance and self.instance.pk and self.instance.mena) else None) or faktura_mena or mena
        if 'suma_uhrady' in self.fields:
            self.fields['suma_uhrady'].label = f"💰 Suma úhrady ({akt_mena})"
            if self.fields['suma_uhrady'].help_text and '€' in self.fields['suma_uhrady'].help_text:
                self.fields['suma_uhrady'].help_text = self.fields['suma_uhrady'].help_text.replace('(€)', f'({akt_mena})').replace('€', akt_mena)

        if 'mena' in self.fields and not self.initial.get('mena') and (not self.instance or not self.instance.pk or not self.instance.mena):
            self.fields['mena'].initial = faktura_mena or mena

    def clean(self):
        cleaned_data = super().clean()
        vf = cleaned_data.get('vystavena_faktura')
        pf = cleaned_data.get('prijata_faktura')
        if vf and pf:
            raise forms.ValidationError("⚠️ Úhradu nie je možné priradiť k vystavenej aj prijatej faktúre súčasne. Zvoľte len jednu faktúru.")

        fa_mena = (getattr(vf, 'mena', None) if vf else getattr(pf, 'mena', None)) if (vf or pf) else None
        if fa_mena and str(fa_mena).strip():
            cleaned_data['mena'] = str(fa_mena).strip().upper()
        elif not cleaned_data.get('mena'):
            try:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                country_defaults = FiremnaIdentitaModel.get_country_defaults() if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
                identita = FiremnaIdentitaModel.objects.first()
                cleaned_data['mena'] = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()
            except Exception:
                cleaned_data['mena'] = get_firemna_mena()
        else:
            cleaned_data['mena'] = str(cleaned_data['mena']).strip().upper()

        suma = cleaned_data.get('suma_uhrady')
        if suma is not None and suma < Decimal('0.00'):
            self.add_error('suma_uhrady', "⚠️ Suma úhrady nemôže byť záporná.")
        return cleaned_data

class MarzaActionForm(ActionForm):
    nova_marza = forms.DecimalField(
        label='',  
        required=False,
        widget=forms.NumberInput(attrs={
            'placeholder': 'Prirážka %', 
            'class': 'isas-margin-input',
            'step': '0.01'
        }),
        help_text="💡 Zadajte percentuálnu prirážku, ktorá sa pripočíta k nákupnej cene."
    )

class ServisnyZaznamAdminForm(forms.ModelForm):
    notif_admin_tg = forms.BooleanField(
        label="📱 Telegram: Notifikácia majiteľovi (Dokončené)", 
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    notif_admin_mail = forms.BooleanField(
        label="✉️ E-mail: Notifikácia majiteľovi (Dokončené)", 
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    notif_admin_discord = forms.BooleanField(
        label="🎮 Discord: Notifikácia majiteľovi (Dokončené)", 
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    upozornit_majitela_protokol_tg = forms.BooleanField(
        label="✈️ Telegram: Upozorniť vedenie na podpis",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    upozornit_majitela_protokol_mail = forms.BooleanField(
        label="📧 E-mail: Upozorniť vedenie na podpis",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    upozornit_majitela_protokol_discord = forms.BooleanField(
        label="🎮 Discord: Upozorniť vedenie na podpis",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )

    class Meta:
        model = ServisnyZaznam
        fields = '__all__'

class OptionalTimeSplitDateTimeField(forms.SplitDateTimeField):
    def __init__(self, *args, **kwargs):
        kwargs['widget'] = AdminSplitDateTime()
        kwargs['require_all_fields'] = False
        super().__init__(*args, **kwargs)
        # 🚀 ISAS FIX: Vnútorné časové pole NESMIE byť povinné!
        if len(self.fields) > 1:
            self.fields[0].required = kwargs.get('required', True)
            self.fields[1].required = False

    def compress(self, data_list):
        if not data_list:
            return None
        d = data_list[0] if (data_list and len(data_list) > 0) else None
        has_time = bool(data_list and len(data_list) > 1 and data_list[1])
        t = data_list[1] if has_time else datetime.time.min
        if not d and has_time:
            raise forms.ValidationError("⚠️ Ak zadáte čas, musíte vyplniť aj dátum.")
        if d:
            if isinstance(d, datetime.datetime):
                d = d.date()
            if isinstance(t, datetime.datetime):
                t = t.time()
            dt = datetime.datetime.combine(d, t)
            if getattr(settings, 'USE_TZ', False):
                if timezone.is_naive(dt):
                    return timezone.make_aware(dt, timezone.get_current_timezone())
                return dt
            else:
                if dt and timezone.is_aware(dt):
                    return timezone.localtime(dt).replace(tzinfo=None)
                return dt
        return None

class UlohaAdminForm(forms.ModelForm):
    zaciatok = OptionalTimeSplitDateTimeField(
        label="🕒 Začiatok",
        required=True,
        help_text="📅 Dátum je povinný. Čas je voliteľný."
    )
    koniec = OptionalTimeSplitDateTimeField(
        label="🏁 Koniec",
        required=False,
        help_text="⏱️ Dátum a čas ukončenia práce (voliteľné)."
    )
    notif_technik_tg = forms.BooleanField(
        label="📱 Telegram: Notifikácia technikom",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    notif_technik_mail = forms.BooleanField(
        label="✉️ E-mail: Notifikácia technikom",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    notif_technik_discord = forms.BooleanField(
        label="🎮 Discord: Notifikácia technikom",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    notif_admin_tg = forms.BooleanField(
        label="📱 Telegram: Notifikácia majiteľovi (Dokončené)",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    notif_admin_mail = forms.BooleanField(
        label="✉️ E-mail: Notifikácia majiteľovi (Dokončené)",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )
    notif_admin_discord = forms.BooleanField(
        label="🎮 Discord: Notifikácia majiteľovi (Dokončené)",
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'})
    )

    class Meta:
        model = Uloha
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        technici = cleaned_data.get('technici')
        zaciatok = cleaned_data.get('zaciatok')
        koniec = cleaned_data.get('koniec')

        z_local = timezone.localtime(zaciatok) if (zaciatok and hasattr(zaciatok, 'tzinfo') and timezone.is_aware(zaciatok)) else zaciatok
        z_safe = z_local.replace(tzinfo=None) if (z_local and hasattr(z_local, 'tzinfo') and timezone.is_aware(z_local)) else z_local

        k_local = timezone.localtime(koniec) if (koniec and hasattr(koniec, 'tzinfo') and timezone.is_aware(koniec)) else koniec
        k_safe = k_local.replace(tzinfo=None) if (k_local and hasattr(k_local, 'tzinfo') and timezone.is_aware(k_local)) else k_local

        if z_safe and k_safe and k_safe < z_safe:
            self.add_error('koniec', "🏁 Dátum a čas konca úlohy nemôže byť skorší ako začiatok.")

        if not getattr(settings, 'USE_TZ', False):
            if zaciatok:
                cleaned_data['zaciatok'] = z_safe
            if koniec:
                cleaned_data['koniec'] = k_safe

        if technici and z_safe:
            datum_zaciatok = to_date(z_safe)
            datum_koniec = to_date(k_safe) if (k_safe and k_safe >= z_safe) else datum_zaciatok

            if datum_zaciatok and datum_koniec:
                absenti = Nepritomnost.objects.filter(
                    zamestnanec__in=technici,
                    stav='SCHVALENE',
                    datum_od__lte=datum_koniec,
                    datum_do__gte=datum_zaciatok
                ).select_related('zamestnanec')
                
                if absenti.exists():
                    detaily = []
                    videni = set()
                    for a in absenti:
                        rozsah = "🌗 (Iba pol dňa)" if a.pol_dna else "🛑 (Celý deň)"
                        meno_zam = f"{getattr(a.zamestnanec, 'meno', '') or ''} {getattr(a.zamestnanec, 'priezvisko', '') or ''}".strip() or str(a.zamestnanec)
                        kluc = f"{meno_zam} {rozsah}"
                        if kluc not in videni:
                            videni.add(kluc)
                            detaily.append(kluc)
                    
                    mena_str = ", ".join(detaily)
                    datum_info = datum_zaciatok.strftime('%d.%m.%Y') if datum_zaciatok == datum_koniec else f"{datum_zaciatok.strftime('%d.%m.%Y')} - {datum_koniec.strftime('%d.%m.%Y')}"
                    self.add_error('technici', f"🚫 CHYBA PLÁNOVANIA: Títo technici majú v termíne {datum_info} schválenú absenciu: {mena_str}")
                
        return cleaned_data

class TankovanieAdminForm(forms.ModelForm):
    class Meta:
        model = Tankovanie
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        defaults = {}
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            defaults = FiremnaIdentitaModel.get_country_defaults() if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
            identita = FiremnaIdentitaModel.objects.first()
            mena = getattr(identita, 'predvolena_mena', None) or defaults.get('mena') or get_firemna_mena()
        except Exception:
            mena = get_firemna_mena()

        akt_mena = (self.instance.mena if (self.instance and self.instance.pk and self.instance.mena) else None) or mena
        for f_name, prefix, emoji in [('cena_celkom', 'Cena celkom s DPH', '💰'), ('cena_bez_dph', 'Suma bez DPH', '💶'), ('suma_dph', 'Suma DPH', '🏛️')]:
            if f_name in self.fields:
                self.fields[f_name].label = f"{emoji} {prefix} ({akt_mena})"
                if self.fields[f_name].help_text and '€' in self.fields[f_name].help_text:
                    self.fields[f_name].help_text = self.fields[f_name].help_text.replace('(€)', f'({akt_mena})').replace('€', akt_mena)

        if 'mena' in self.fields and not self.initial.get('mena') and (not self.instance or not self.instance.pk or not self.instance.mena):
            self.fields['mena'].initial = mena

        if 'sadzba_dph' in self.fields and (not self.instance or not self.instance.pk or getattr(self.instance, 'sadzba_dph', None) is None):
            try:
                GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                self.fields['sadzba_dph'].initial = GlobalneNastaveniaModel.get_zakladna_dph()
            except Exception:
                pass

        if 'kod_clenenia_dph' in self.fields and (not self.instance or not self.instance.pk or not self.instance.kod_clenenia_dph):
            self.fields['kod_clenenia_dph'].initial = defaults.get('kod_dph_prijata') or 'VAT_IN'

    def clean(self):
        cleaned_data = super().clean()
        vodic = cleaned_data.get('vodič')
        osoba = cleaned_data.get('osoba')
        cerpacia_stanica = cleaned_data.get('cerpacia_stanica')
        sposob_zauctovania = cleaned_data.get('sposob_zauctovania_erp')

        if not vodic and not osoba:
            self.add_error('vodič', "⚠️ Musíte vybrať vodiča zamestnanca, alebo priradiť súkromnú osobu! 👨‍🔧")
            self.add_error('osoba', "⚠️ Musíte vybrať súkromnú osobu, alebo priradiť vodiča zamestnanca! 👤")

        if vodic and osoba:
            self.add_error('vodič', "⚠️ Zvoľte buď zamestnanca, alebo súkromnú osobu, nie oboch naraz!")
            self.add_error('osoba', "⚠️ Zvoľte buď zamestnanca, alebo súkromnú osobu, nie oboch naraz!")

        if sposob_zauctovania != 'FAKTURA' and not cerpacia_stanica:
            self.add_error('cerpacia_stanica', "⚠️ Pre priame platby (karta/hotovosť) musíte vybrať Čerpaciu stanicu kvôli odpočtu DPH a exportu! 🏢")

        if not cleaned_data.get('mena'):
            try:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                country_defaults = FiremnaIdentitaModel.get_country_defaults() if hasattr(FiremnaIdentitaModel, 'get_country_defaults') else {}
                identita = FiremnaIdentitaModel.objects.first()
                cleaned_data['mena'] = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()
            except Exception:
                cleaned_data['mena'] = get_firemna_mena()
        else:
            cleaned_data['mena'] = str(cleaned_data['mena']).strip().upper()

        cena = cleaned_data.get('cena_celkom')
        if cena is not None and cena < Decimal('0.00'):
            self.add_error('cena_celkom', "⚠️ Cena celkom nemôže byť záporná.")
        litre = cleaned_data.get('litre')
        if litre is not None and litre <= Decimal('0.00'):
            self.add_error('litre', "⚠️ Objem paliva v litroch musí byť väčší ako nula.")

        return cleaned_data

class PovolenieNaVstupAdminForm(forms.ModelForm):
    class Meta:
        model = PovolenieNaVstup
        fields = '__all__'
        widgets = {
            'notif_technik_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'notif_technik_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'notif_technik_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'notif_admin_tg': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'notif_admin_mail': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
            'notif_admin_discord': forms.CheckboxInput(attrs={'class': 'isas-modern-toggle'}),
        }