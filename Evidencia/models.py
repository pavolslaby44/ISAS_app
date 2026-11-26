import os
import base64
import csv
import logging
import re
import secrets
import unicodedata
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from PIL import Image

from colorfield.fields import ColorField
import openpyxl
import qrcode

from django.core.cache import cache
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.contrib.postgres.indexes import BrinIndex
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.files import File
from django.contrib import admin
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Func, Max, Q, Sum
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from ISASaplikacia.models import ISASAuditMixin, ISASGDPRMixin
from Evidencia.utils_helper import (
    generate_dynamic_greeting,
    get_default_miesto_startu,
    get_firemna_mena,
    get_skladove_kategorie_z_mapy,
    ocisti_nazov_od_jednotky,
    ziskaj_povolene_modely,
    SmartDateTimeField,
    LedgerKalkulacka
)
from .utils_compliance import add_calendar_interval, get_compliance_config, to_date

logger = logging.getLogger(__name__)

def get_default_dph():
    try:
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        if hasattr(GlobalneNastavenia, 'get_zakladna_dph'):
            return GlobalneNastavenia.get_zakladna_dph()
        gn = GlobalneNastavenia.objects.first()
        if gn and hasattr(gn, 'zakladna_sadzba_dph') and gn.zakladna_sadzba_dph is not None:
            return Decimal(str(gn.zakladna_sadzba_dph))
    except Exception:
        pass
    return Decimal('0.00')

def get_default_kod_clenenia_prichodzia():
    try:
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        if hasattr(FiremnaIdentita, 'get_country_defaults'):
            defaults = FiremnaIdentita.get_country_defaults()
            if defaults and defaults.get('kod_dph_prijata'):
                return defaults['kod_dph_prijata']
    except Exception:
        pass
    return 'VAT_IN'

def get_default_kod_clenenia_vystavena():
    try:
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        if hasattr(FiremnaIdentita, 'get_country_defaults'):
            defaults = FiremnaIdentita.get_country_defaults()
            if defaults and defaults.get('kod_dph_vystavena'):
                return defaults['kod_dph_vystavena']
    except Exception:
        pass
    return 'VAT_OUT'

class ISASERPExportMixin(models.Model):
    """
    Abstraktný model. Pridáva sa každému modelu, ktorý sa bude exportovať do XML (Pohoda/Kros).
    Zabezpečuje, že vieme, čo už v účtovníctve je, a chráni záznamy pred spätnou úpravou.
    """
    ERP_STATUS_CHOICES = [
        ('NEW', '🟡 Čaká na export'),
        ('EXPORTED', '🟢 Exportované (Uzamknuté)'),
        ('FAILED', '🔴 Chyba pri exporte'),
    ]
    
    erp_export_status = models.CharField(
        max_length=15, 
        choices=ERP_STATUS_CHOICES, 
        default='NEW', 
        verbose_name="📡 ERP Status",
        help_text="📡 Stav synchronizácie s účtovníctvom (Pohoda/Omega); v stave Exportované je doklad zablokovaný proti úpravám."
    )
    erp_id = models.CharField(
        max_length=100, blank=True, null=True, 
        verbose_name="🔑 ERP ID (Externé)", 
        help_text="🔑 Unikátne ID z účtovného programu zamedzujúce vytváraniu duplicitných dokladov."
    )
    erp_last_exported_at = models.DateTimeField(
        blank=True, null=True, 
        verbose_name="⏱️ Dátum posledného exportu",
        help_text="⏱️ Presný dátum a čas úspešného prenosu údajov do účtovného softvéru."
    )
    erp_error_log = models.TextField(
        blank=True, null=True, 
        verbose_name="🚨 ERP Chybový denník",
        help_text="🚨 Detailný záznam systémovej chyby pri neúspešnom prenose pre rýchlu opravu údajov."
    )

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        # Globálna ochrana proti úprave uzamknutých dokladov
        if self.pk:
            try:
                stary_zaznam = type(self).objects.get(pk=self.pk)
                if stary_zaznam.erp_export_status == 'EXPORTED' and self.erp_export_status == 'EXPORTED':
                    raise ValidationError("🛑 TENTO ZÁZNAM JE UZAMKNUTÝ! Bol už exportovaný do účtovníctva a nie je možné ho upravovať.")
            except type(self).DoesNotExist:
                pass

    def delete(self, *args, **kwargs):
        # Globálna ochrana proti zmazaniu uzamknutých dokladov
        if self.erp_export_status in ['EXPORTED', 'PROCESSING']:
            raise PermissionDenied("🛑 ZÁKONNÁ OCHRANA: Záznam, ktorý je exportovaný (alebo sa práve exportuje) do účtovníctva, je prísne zakázané vymazať.")
        super().delete(*args, **kwargs)

class ISASSkladovaKartaMixin(models.Model):
    """
    👻 Abstraktný model. 🏗️ Pridáva sa každému fyzickému modelu v sklade 🏭.
    🛡️ Zabezpečuje, že položka 🛒 má všetky náležitosti 📋 pre vytvorenie skladovej karty 📇 v ERP 💻.
    """
    TYP_ZASOBY_CHOICES = [
        ('TOVAR', '📦 Tovar (Nakupujem a predávam 🤝)'),
        ('MATERIAL', '🔩 Materiál (Na vlastnú spotrebu 🍽️ / servis 🔧)'),
        ('SLUZBA', '🛠️ Služba (Nehmotné ☁️)'),
    ]
    
    typ_zasoby_erp = models.CharField(
        max_length=15, choices=TYP_ZASOBY_CHOICES, default='TOVAR', 
        verbose_name="📂 Typ zásoby (ERP 💻)",
        help_text="📂 Kategória položky pre správne zaúčtovanie a synchronizáciu skladových kariet s účtovným systémom."
    )
    katalogove_cislo = models.CharField(
        max_length=100, blank=True, null=True, unique=True,
        verbose_name="🏷️ Katalógové číslo (SKU 🔢)",
        help_text="🏷️ Unikátny kód (SKU) pre párovanie s ERP. 🔄 Zrkadlenie: Akékoľvek vyplnenie alebo zmena tohto poľa sa automaticky prekopíruje do Sériového čísla (a naopak). Systém ich udržiava totožné."
    )
    ean_kod = models.CharField(
        max_length=50, blank=True, null=True, 
        verbose_name="🔣 EAN čiarový kód 🦓",
        help_text="🔍 Medzinárodný čiarový kód pre rýchlu identifikáciu položky pri skladových pohyboch pomocou čítačky."
    )
    sadzba_dph = models.DecimalField(
        max_digits=4, decimal_places=2, default=get_default_dph, 
        verbose_name="🏛️ Sadzba DPH (%) 📉",
        help_text="Platná zákonná sadzba DPH v percentách pre výpočet dane pri nákupe, predaji a exporte do účtovníctva."
    )

    class Meta:
        abstract = True

class Sklad(ISASAuditMixin, models.Model):
    nazov = models.CharField(
        verbose_name="📝 Názov",
        max_length=100,
        help_text="🏢 Názov hlavného skladu pre evidenciu zásoby a interný obeh materiálu."
    )

    class Meta:
        verbose_name = "🏭 Sklad"
        verbose_name_plural = "🏭 Sklad"

    def __str__(self):
        return self.nazov

    # 🟢 ISAS FIREWALL: ZABETONOVANIE HLAVNÉHO SKLADU 🧱
    def clean(self):
        super().clean()
    
        # Ak vytvárame nový objekt (nemá ešte ID) a už existuje nejaký sklad, zablokujeme to!
        if not self.pk and Sklad.objects.exists():
            raise ValidationError("🛑 KRITICKÁ CHYBA ARCHITEKTÚRY: Hlavný Sklad už existuje (ID=1). Pre vytvorenie ďalších pobočiek použite sekciu 'Sklady - filiálky' 🏢.")

    def save(self, *args, **kwargs):
        # 🧹 Vynútená validácia pred samotným uložením do databázy
        self.full_clean()
        
        # Hardcore Singleton ochrana proti Race Condition na úrovni DB
        with transaction.atomic():
            if not self.pk:
                # Vynútime ID=1. Ak sa dvaja pokúsia uložiť naraz, databáza vyhodí IntegrityError
                # a zabráni vytvoreniu duplikátu.
                self.pk = 1 
                
            super().save(*args, **kwargs)

class AktualneNaSklade(Sklad):
    class Meta:
        proxy = True
        verbose_name = "🟩 Aktuálne na sklade"
        verbose_name_plural = "🟩 Aktuálne na sklade"

MERNE_JEDNOTKY_CHOICES = [
    ('ks', '📦 kus'), ('sada', '🧰 sada'), ('bal', '🛍️ balenie'), 
    ('par', '🖇️ pár'), ('rola', '📜 rola'), ('kg', '⚖️ kilogram'), 
    ('g', '🪶 gram'), ('l', '🛢️ liter'), ('ml', '🧪 mililiter'), 
    ('m', '📏 meter'), ('m2', '📐 meter štvorcový'),
]

class ISAS_ZakladSkladu(ISASAuditMixin, ISASSkladovaKartaMixin, models.Model):
    """
    🧱 Master šablóna. Django z tohto nevytvorí tabuľku, iba dedí polia.
    """
    seriove_cislo = models.CharField(verbose_name="🔢 Sériové číslo", max_length=100, db_index=True, help_text="🔢 Unikátny identifikátor položky pre presnú skladovú evidenciu. 🔄 Zrkadlenie: Akékoľvek vyplnenie alebo zmena tohto poľa sa automaticky prekopíruje do Katalógového čísla (a naopak). Systém ich udržiava totožné.")
    pocet_na_sklade = models.PositiveIntegerField(verbose_name="📦 Počet", default=0, help_text="📦 Aktuálne dostupné množstvo položky na sklade určené pre servisné zákazky a inventarizáciu.")
    merna_jednotka = models.CharField(verbose_name="📏 Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='ks', help_text="📏 Základná merná jednotka pre presné účtovanie, vyskladňovanie a fakturáciu.")

    obchodny_nazov = models.CharField(verbose_name="🛒 Obchodný názov", max_length=255, blank=True, null=True, help_text="🛒 Ak vyplníte, tento názov sa zobrazí v PDF a XLM dokumentoch adresovaným vašim zákazníkom namiesto údaju z pola sériové číslo, resp. katalógového čísla.")
    skryt_vyrobcu = models.BooleanField(verbose_name="🕵️ Skryť výrobcu v PDF", default=False, help_text="🕵️ Zaškrtnite, ak chcete na PDF dokumentoch skryť výrobcu tohto dielu (ochrana know-how).")
    
    dodavatel = models.ForeignKey('Evidencia.Dodavatel', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="🚚 Dodávateľ", help_text="🚚 Primárny dodávateľ dielu pre zjednodušenie doobjednávkového procesu a správu dodávateľských vzťahov.")
    vyrobca = models.ForeignKey('Evidencia.DatabazaVyrobcov', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="🏭 Výrobca dielu", help_text="🏭 Originálny výrobca položky pre overenie kompatibility a uplatňovanie záručných nárokov.")
    kto_som = models.TextField(verbose_name="ℹ️ V skratke o mne", blank=True, help_text="ℹ️ Stručný interný popis vlastností a špecifikácie dielu pre servisných technikov a skladníkov.")

    # --- 🌐 WEB BUILDER INTEGRÁCIA (E-commerce Lite) ---
    zobrazit_na_webe = models.BooleanField(
        default=False, 
        verbose_name="🌐 Zobraziť v E-shope na webe", 
        help_text="🌐 Ak je zaškrtnuté, diel sa zobrazí vo verejnom katalógu náhradných dielov."
    )
    popis_na_webe = models.TextField(
        blank=True, null=True, 
        verbose_name="📝 Marketingový popis (Web)", 
        help_text="📝 Pútavý popis dielu pre zákazníkov na webe (ak je prázdne, použije sa interný popis 'kto_som')."
    )
    obrazok_na_webe = models.ImageField(
        upload_to='eshop_diely/', blank=True, null=True, 
        verbose_name="🖼️ Produktová fotka (Web)", 
        help_text="🖼️ Reprezentatívna fotka dielu pre verejný katalóg."
    )
    slug = models.SlugField(
        max_length=255, unique=True, blank=True, null=True, 
        verbose_name="🔗 URL Slug",
        help_text="🔗 Unikátny URL identifikátor pre e-shop (generuje sa automaticky)."
    )

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.seriove_cislo} ({self.pocet_na_sklade} {self.merna_jednotka})"

    def save(self, *args, **kwargs):
        # 🚀 ISAS ARCHITECT: WebP Kompresia pre E-shop obrázky
        if self.obrazok_na_webe and not self.obrazok_na_webe.name.lower().endswith('.webp'):
            try:                
                img = Image.open(self.obrazok_na_webe)
                if img.mode == "P": img = img.convert("RGBA")
                elif img.mode not in ("RGB", "RGBA"): img = img.convert("RGB")
                img.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
                
                output = BytesIO()
                img.save(output, format='WEBP', quality=85, method=6)
                output.seek(0)
                
                nazov_bez_pripony = os.path.basename(self.obrazok_na_webe.name).rsplit('.', 1)[0][:85]
                self.obrazok_na_webe.save(f"{nazov_bez_pripony}.webp", ContentFile(output.read()), save=False)
            except Exception:
                pass # Ak zlyhá, uloží sa originál

        with transaction.atomic():
            # Obojsmerné preklopenie (Sériové číslo <-> Katalógové číslo)
            if self.pk:
                # Záznam už existuje (UPDATE) - porovnáme s databázou
                try:
                    stary_zaznam = type(self).objects.get(pk=self.pk)
                    # Ak sa zmenilo sériové číslo, prepíšeme katalógové
                    if self.seriove_cislo != stary_zaznam.seriove_cislo:
                        self.katalogove_cislo = self.seriove_cislo or None
                    # Ak sa zmenilo katalógové číslo, prepíšeme sériové
                    elif self.katalogove_cislo != stary_zaznam.katalogove_cislo:
                        self.seriove_cislo = self.katalogove_cislo or ""
                except type(self).DoesNotExist:
                    pass
            else:
                # Nový záznam (CREATE) - funguje ako doteraz
                if self.seriove_cislo and not self.katalogove_cislo:
                    self.katalogove_cislo = self.seriove_cislo
                elif self.katalogove_cislo and not self.seriove_cislo:
                    self.seriove_cislo = self.katalogove_cislo

            # 🛡️ PostgreSQL FIX: Prázdny reťazec v unikátnom poli musí byť vždy None (NULL)
            if self.katalogove_cislo is not None and not str(self.katalogove_cislo).strip():
                self.katalogove_cislo = None
                    
            # 🤖 AUTOMATIKA: Generovanie unikátneho URL slugu pre E-shop
            if not self.slug:
                base_slug = slugify(self.obchodny_nazov or self.seriove_cislo or f"diel-{self.pk or 'novy'}")[:240]
                if not base_slug:
                    base_slug = f"diel-{self.pk or uuid.uuid4().hex[:6]}"
                slug = base_slug
                counter = 1
                while type(self).objects.filter(slug=slug).exclude(pk=self.pk).exists():
                    slug = f"{base_slug[:240]}-{counter}"
                    counter += 1
                self.slug = slug

            super().save(*args, **kwargs)

class ISAS_DielSTypom(ISAS_ZakladSkladu):
    """
    🛠️ Šablóna pre diely, ktoré majú na výber "Originál / Náhrada" (Poruchové a Spotrebné).
    Stroje tento typ nemajú, preto dedia len z Master šablóny.
    """
    TYP_CHOICES = [('O', '💎 originál'), ('N', '🔄 náhrada')]
    typ = models.CharField(verbose_name="🔄 O/N", max_length=1, choices=TYP_CHOICES, blank=True, null=True, help_text="🏷️ Klasifikácia dielu na originál (OEM) alebo kompatibilnú náhradu pre riadenie nákupných nákladov, cenotvorbu a záručné podmienky.")

    class Meta:
        abstract = True

class Filialka(ISASAuditMixin, models.Model):
    TYP_SKLADU_CHOICES = [
        ('BUDOVA', '🏢 Kamenná prevádzka / Budova'),
        ('AUTO', '🚐 Montážne vozidlo / Dodávka'),
        ('KONTAJNER', '🏗️ Dočasný sklad na stavbe'),
        ('INE', '📦 Iné / Virtuálny sklad'),
    ]

    nazov = models.CharField(max_length=150, verbose_name="🏢 Názov filiálky / Skladu", help_text="🏢 Oficiálny názov prevádzky alebo skladu pre internú evidenciu a doklady.")
    kod_pobocky = models.CharField(max_length=50, blank=True, null=True, verbose_name="🏷️ Kód pobočky", help_text="🏷️ Unikátny skrátený kód prevádzky (napr. WH-BA-01) pre rýchlu identifikáciu v ERP.")
    typ_skladu = models.CharField(max_length=20, choices=TYP_SKLADU_CHOICES, default='BUDOVA', verbose_name="⚙️ Typ skladu", help_text="⚙️ Kategória skladu pre správne riadenie zásob na stavbách, vo vozidlách alebo prevádzkach.")
    
    adresa = models.TextField(blank=True, null=True, verbose_name="📍 Adresa prevádzky", help_text="📍 Presná adresa prevádzky pre doručovanie materiálu a plánovanie výjazdov.")    
    zodpovedny_zamestnanec = models.ForeignKey(
        'Zamestnanec', # 👈 ZMENENÉ: Odkazuje na tvoj model Zamestnanec
        on_delete=models.SET_NULL,
        blank=True, 
        null=True,
        verbose_name="👷‍♂️ Priradený vedúci skladu/zodpovedná osoba",
        help_text="👤 Hlavný vedúci prevádzky; po výbere sa kontaktné údaje načítajú z jeho profilu."
    )
    priradeni_zamestnanci: models.ManyToManyField = models.ManyToManyField(
        'Zamestnanec', 
        blank=True, 
        related_name='pristupne_filialky', 
        verbose_name="👥 Priradení zamestnanci (Prístup)",
        help_text="👥 Zamestnanci s oprávnením manipulovať so zásobami a vykonávať skladové operácie."
    )
    
    veduci_skladu = models.CharField(max_length=150, blank=True, null=True, verbose_name="👤 Vedúci skladu / Zodpovedná osoba", help_text="📋 Meno zodpovednej osoby, ak nie je zvolená zo zoznamu zamestnancov.")
    telefon = models.CharField(max_length=50, blank=True, null=True, verbose_name="📞 Telefónny kontakt", help_text="📞 Priamy telefonický kontakt na prevádzku pre dodávateľov a logistiku.")
    email = models.EmailField(blank=True, null=True, verbose_name="📧 E-mail na pobočku", help_text="📧 E-mailová adresa prevádzky pre zasielanie skladových dokladov a objednávok.")

    je_aktivna = models.BooleanField(default=True, verbose_name="🟢 Aktívna prevádzka", help_text="🟢 Deaktivujte pri zrušení prevádzky; záznam nemazať kvôli zachovaniu účtovnej a skladovej histórie.")
    poznamka = models.TextField(blank=True, null=True, verbose_name="📝 Interná poznámka", help_text="📝 Interné poznámky, špecifické pokyny pre príjem tovaru alebo prevádzkové hodiny.")
    
    class Meta:
        verbose_name = "🏪 Sklad/Filiálka"
        verbose_name_plural = "🏪 Sklady/filiálky"
        ordering = ['-je_aktivna', 'nazov']

    def __str__(self):
        kod = f" [{self.kod_pobocky}]" if self.kod_pobocky else ""
        stav = "" if self.je_aktivna else " (❌ ZRUŠENÁ)"
        return f"{self.nazov}{kod}{stav}"

class SukromnaOsoba(ISASAuditMixin, ISASGDPRMixin, ISASERPExportMixin, models.Model):
    meno_a_priezvisko = models.CharField(
        max_length=100, 
        verbose_name="🪪 Meno a priezvisko",
        help_text="👤 Oficiálne meno a priezvisko fyzickej osoby pre správne vystavenie dokladov a zmluvnú dokumentáciu."
    )
    email = models.EmailField(
        max_length=254, 
        blank=True, null=True, 
        verbose_name="📧 E-mail",
        help_text="✉️ Primárny kontakt pre doručovanie elektronických faktúr, prevádzkovej komunikácie a notifikácií."
    )
    telefon = models.CharField(
        max_length=20, 
        blank=True, null=True, 
        verbose_name="📞 Telefón",
        help_text="📞 Telefónne číslo v medzinárodnom tvare pre rýchly operátorský a servisný kontakt."
    )
    telegram_id = models.BigIntegerField(
        null=True, blank=True, unique=True, 
        verbose_name='✈️ Telegram ID',
        help_text="💬 Unikátne číselné ID používateľa pre automatizovanú komunikáciu prostredníctvom Telegram bota."
    )
    discord_id = models.BigIntegerField(
        null=True, blank=True, unique=True, 
        verbose_name='🎮 Discord ID',
        help_text="🤖 Číselné ID používateľa pre integráciu s Discord botom a správu interných hlásení."
    )
    ulica_a_cislo = models.CharField(
        max_length=150, 
        blank=True, null=True,
        verbose_name="📍 Ulica a číslo", 
        help_text="📍 Ulica a súpisné/orientačné číslo pre fakturačné účely a identifikáciu miesta plnenia."
    )
    mesto = models.CharField(
        max_length=100, 
        blank=True, null=True,
        verbose_name="🏙️ Mesto", 
        help_text="🏙️ Názov mesta alebo obce pre fakturáciu a správne spárovanie v ERP systéme."
    )
    psc = models.CharField(
        max_length=20, 
        blank=True, null=True,
        verbose_name="📮 PSČ", 
        help_text="📮 Poštové smerovacie číslo pre korektné doručovanie a daňové doklady."
    )
    stat = models.CharField(
        max_length=50, 
        blank=True, default="", 
        verbose_name="🌍 Štát",
        help_text="🌐 Krajina trvalého pobytu pre daňové posúdenie a exporty do účtovného systému."
    )
    iban = models.CharField(
        max_length=34, 
        blank=True, null=True, 
        verbose_name="🏦 IBAN pre preplácanie výdavkov", 
        help_text="🏦 Číslo účtu v tvare IBAN pre automatizované preplácanie bločkov, diét a cestovných náhrad."
    )
    predvolena_cerpacka = models.ForeignKey(
        'Dodavatel', 
        on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="⛽ Predvolená Čerpacia stanica",
        help_text="⛽ Preferovaný dodávateľ paliva pre automatické priradenie pri evidencii tankovania."
    )
    datum_narodenia = models.DateField(
        blank=True, null=True, 
        verbose_name="🎂 Dátum narodenia",
        help_text="📅 Dátum narodenia pre účely overenia identity, HR evidenciu a CRM notifikácie."
    )

    class Meta:
        verbose_name = "👤 Súkromná osoba"
        verbose_name_plural = "👤 Súkromné osoby"

    def __str__(self):
        return self.meno_a_priezvisko

class Zamestnanec(ISASAuditMixin, ISASGDPRMixin, ISASERPExportMixin, models.Model):
    INTERNI = 'IN'
    EXTERNI = 'EX'
    BRIGADNICI = 'BR'
    KONATEL = 'KO'

    POMER_CHOICES = [
        (INTERNI, '👨‍💼 Interný (Zamestnanec)'),
        (EXTERNI, '👨‍🔧 Externý (Živnostník)'),
        (BRIGADNICI, '🎒 Brigádnik'),
        (KONATEL, '👑 Konateľ / Majiteľ'),
    ]

    TECHNIK = 'TE'
    DISPECER = 'DI'
    SKLADNIK = 'SK'
    ADMINISTRATIVA = 'AD'
    OBCHODNIK = 'OB'

    ROLA_CHOICES = [
        (TECHNIK, '🛠️ Servisný technik'),
        (DISPECER, '🎧 Dispečer / Plánovač'),
        (SKLADNIK, '📦 Skladník'),
        (ADMINISTRATIVA, '📁 Administratíva'),
        (OBCHODNIK, '🤝 Obchodník'),
    ]

    typ_pomeru = models.CharField(max_length=2, choices=POMER_CHOICES, default=INTERNI, verbose_name='🤝 Typ pomeru', help_text="⚖️ Právny vzťah pracovníka k firme pre mzdové, daňové a odvodové účely.")
    pracovna_rola = models.CharField(max_length=2, choices=ROLA_CHOICES, default=TECHNIK, db_index=True, verbose_name='👔 Pracovná rola', help_text="👔 Hlavná pracovná náplň určujúca prístupové práva a role v pracovných postupoch.")  
    meno = models.CharField(max_length=100, verbose_name='📝 Meno', help_text="👤 Krstné meno zamestnanca pre identifikáciu v dokladoch a výkazoch.")
    priezvisko = models.CharField(max_length=100, verbose_name='📝 Priezvisko', help_text="👤 Priezvisko zamestnanca pre oficiálnu dokumentáciu a účtovníctvo.")
    datum_narodenia = models.DateField(verbose_name='🎂 Dátum narodenia', blank=True, null=True, help_text="🎂 Dátum narodenia pre HR evidenciu, zákonné hlásenia a nároky.")
    
    ulica_a_cislo = models.CharField(max_length=150, verbose_name="📍 Ulica a číslo", blank=True, null=True, help_text="📍 Ulica a súpisné číslo trvalého pobytu pre mzdové výkazy a zmluvy.")
    mesto = models.CharField(max_length=100, verbose_name="🏙️ Mesto", blank=True, null=True, help_text="🏙️ Mesto trvalého pobytu pre HR evidenciu a zmluvnú dokumentáciu.")
    psc = models.CharField(max_length=20, verbose_name="📮 PSČ", blank=True, null=True, help_text="📮 Poštové smerovacie číslo pre oficiálnu korešpondenciu a mzdové výkazy.")
    stat = models.CharField(max_length=50, blank=True, default="", verbose_name="🌍 Štát", help_text="🌍 Štát daňovej rezidencie pre správne určenie daňových a odvodových povinností.")
    iban = models.CharField(
        max_length=34, blank=True, null=True, 
        verbose_name="🏦 IBAN pre preplácanie výdavkov", 
        help_text="🏦 Číslo účtu pre výplatu miezd, preplácanie cestovných náhrad a služobných výdavkov."
    )
    
    telefon = models.CharField(max_length=20, verbose_name='📞 Telefón', blank=True, null=True, help_text="📞 Kontaktné telefónne číslo pre operatívne riadenie a dispečing.")
    email = models.EmailField(verbose_name='📧 Email', blank=True, null=True, help_text="📧 E-mailová adresa pre doručovanie výplatných pások, rozpisov práce a notifikácií.")
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, limit_choices_to={'is_staff': True}, verbose_name='👤 Priradený User', help_text="👤 Systémové používateľské konto pre prístup a prihlasovanie do aplikácie.")
    telegram_id = models.BigIntegerField(null=True, blank=True, unique=True, verbose_name='✈️ Telegram ID', help_text="✈️ Identifikátor pre doručovanie okamžitých notifikácií a plánu prác cez Telegram.")
    discord_id = models.BigIntegerField(
        null=True, blank=True, unique=True, 
        verbose_name='🎮 Discord ID',
        help_text="💬 Unikátny identifikátor pre internú komunikáciu a notifikácie cez Discord."
    )

    datum_nastupu = models.DateField(verbose_name='🏁 Dátum nástupu', blank=True, null=True, help_text="📅 Dátum vzniku pracovného pomeru pre výpočet odpracovaných rokov a nárokov.")
    zmluva_subor = models.FileField(
        upload_to='hr_zmluvy/', blank=True, null=True, 
        verbose_name='📄 Pracovná zmluva (PDF/Word)',
        help_text="📄 Priložený sken pracovnej zmluvy, dohody alebo dodatočných zmluvných podmienok."
    )
    osobny_podpis = models.ImageField(
        upload_to='hr_podpisy/', blank=True, null=True, 
        verbose_name='✍️ Osobný digitálny podpis',
        help_text="✍️ Digitalizovaný podpis na PNG s priehľadným pozadím pre automatickú generáciu dokladov."
    )    
    narok_na_dovolenku = models.PositiveIntegerField(
        default=20, verbose_name='🏖️ Nárok na dovolenku (Dni)',
        help_text="🏖️ Celkový ročný nárok na dovolenku v dňoch podľa platných predpisov a zmluvy."
    )
    vycerpana_dovolenka = models.FloatField(
        default=0.0, verbose_name='📉 Vyčerpaná dovolenka (Dni)',
        help_text="📉 Manuálne zadaná vyčerpaná dovolenka pre korekciu zostatku."
    )
    predvolena_cerpacka = models.ForeignKey(
        'Dodavatel', on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="⛽ Predvolená Čerpacia stanica",
        help_text="⛽ Východzia čerpacia stanica pre automatické predvyplnenie pri evidencii tankovania."
    )

    zavazky = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'),
        verbose_name='💸 Záväzky voči firme',
        help_text="💸 Aktuálna finančná pohľadávka firmy voči zamestnancovi."
    )
    poznamka_zavazky = models.TextField(
        verbose_name='🗒️ Poznámka k záväzkom', blank=True, null=True,
        help_text="📝 Dôvod a podrobnosti vzniku finančného záväzku zamestnanca."
    )

    SPOSOB_ODMENY = [
        ('HODINA', '⏱️ Hodinová mzda'),
        ('MESIAC', '📅 Mesačný paušál'),
        ('UKOL', '🎯 Úkolová mzda (Živnostníci)'),
    ]
    
    sposob_odmenovania = models.CharField(
        max_length=10, choices=SPOSOB_ODMENY, default='HODINA',
        verbose_name='⚖️ Spôsob odmeňovania',
        help_text="⚙️ Model odmeňovania určený pre výpočet priamych osobnostných nákladov na výjazdy."
    )
    mzda_brutto_hodina = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0.00'), blank=True,
        verbose_name='💰 Hodinová mzda BRUTTO',
        help_text="Hrubá hodinová mzda pre výpočet zákazkových nákladov pri hodinovej odmene."
    )
    mzda_brutto_mesiac = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'), blank=True,
        verbose_name='💰 Mesačná mzda BRUTTO',
        help_text="Hrubý mesačný plat pre výpočet priemerného hodinového nákladu na prácu."
    )
    mzda_zivnostnik_hodina = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0.00'), blank=True,
        verbose_name='💰 Hodinová sadzba (Živnostník)',
        help_text="Zmluvná hodinová sadzba pre externých dodávateľov pri vyúčtovaní servisu."
    )

    cislo_op = models.CharField(
        max_length=20, blank=True, null=True, 
        verbose_name='🪪 Číslo Občianskeho preukazu / Pasu',
        help_text="🪪 Číslo dokladu totožnosti pre vybavenie vstupných povolení do objektov zákazníkov."
    )
    bozp_platne_do = models.DateField(
        blank=True, null=True, 
        verbose_name='🦺 BOZP platné do',
        help_text="🦺 Dátum exspirácie školenia BOZP pre sledovanie platnosti zákonných certifikácií."
    )
    bozp_subor = models.FileField(
        upload_to='hr_certifikaty/bozp/', blank=True, null=True, 
        verbose_name='📎 Sken BOZP (PDF)',
        help_text="📎 Priložený sken platného osvedčenia o absolvovaní školenia BOZP."
    )
    lekarska_prehliadka_subor = models.FileField(
        upload_to='hr_certifikaty/lekarska/', blank=True, null=True, 
        verbose_name='🏥 Sken Lekárskej prehliadky (PDF)',
        help_text="🏥 Priložený sken lekárskeho posudku o zdravotnej spôsobilosti na prácu."
    )
    tg_upozornenia_ulohy = models.BooleanField(
        default=True, 
        verbose_name='🔔 Posielať upozornenia na úlohy (Telegram)',
        help_text="🔔 Povolenie na automatické odosielanie denných plánov a zákaziek cez Telegram."
    )
    mail_upozornenia_ulohy = models.BooleanField(
        default=True, 
        verbose_name='📧 Posielať upozornenia na úlohy (E-mail)',
        help_text="📧 Povolenie na zasielanie sumáru úloh a výjazdov na e-mail."
    )
    discord_upozornenia_ulohy = models.BooleanField(
        default=True, 
        verbose_name='🎮 Posielať upozornenia na úlohy (Discord)',
        help_text="💬 Povolenie na automatické odosielanie denných plánov a zákaziek cez Discord."
    )
    zdiela_polohu = models.BooleanField(
        default=True, 
        verbose_name="📍 Zdieľa polohu (Radar)",
        help_text="📍 Zdieľanie aktuálnej GPS polohy pre dispečing a optimalizáciu výjazdov."
    )

    # --- 🌐 WEB BUILDER INTEGRÁCIA ---
    zobrazit_na_webe = models.BooleanField(
        default=False, 
        verbose_name="🌐 Zobraziť v tíme na webe", 
        help_text="🌐 Ak je zaškrtnuté, zamestnanec sa zobrazí v bloku 'Náš tím' na verejnej stránke."
    )
    profilova_fotka = models.ImageField(
        upload_to='hr_profilovky/', blank=True, null=True, 
        verbose_name="📸 Profilová fotka (Web)", 
        help_text="📸 Reprezentatívna fotka zamestnanca pre webovú stránku."
    )
    verejna_rola = models.CharField(
        max_length=100, blank=True, null=True, 
        verbose_name="🗣️ Verejná rola (Web)", 
        help_text="📝 Napr. 'Hlavný servisný technik' alebo 'Dispečer'. Zobrazí sa pod menom."
    )

    def __str__(self):
        return f"{self.meno} {self.priezvisko}"

    def vykonaj_anonymizaciu(self, polia_na_vymaz, dovod="Neuvedený dôvod"):
        """🛡️ GDPR & PRIVACY BY DESIGN: Atomické a nevratné zrušenie verejného profilu, súborov a PII."""
        if not self.pk:
            raise ValidationError("⚠️ Kritická chyba: Nemožno anonymizovať záznam, ktorý nebol uložený v databáze.")

        target_db = self._state.db or 'default'
        with transaction.atomic(using=target_db):
            aktualny = self.__class__._base_manager.using(target_db).select_for_update().get(pk=self.pk)
            if aktualny.je_anonymizovany:
                self.refresh_from_db(using=target_db)
                return

            self.zobrazit_na_webe = False
            update_polia = ['zobrazit_na_webe']
            for fname in ('profilova_fotka', 'zmluva_subor', 'bozp_subor', 'lekarska_prehliadka_subor', 'osobny_podpis'):
                fval = getattr(self, fname, None)
                if fval:
                    try:
                        fval.delete(save=False)
                    except Exception:
                        pass
                if hasattr(self, fname):
                    setattr(self, fname, None)
                    update_polia.append(fname)

            for cert in self.certifikaty.all():
                if cert.subor:
                    try:
                        cert.subor.delete(save=False)
                    except Exception:
                        pass
                cert.delete()

            self.save(using=target_db, update_fields=update_polia)
            return super().vykonaj_anonymizaciu(polia_na_vymaz, dovod=dovod)

    def anonymizuj(self, polia_na_vymaz=None, dovod="Neuvedený dôvod"):
        predvolene = ['meno', 'priezvisko', 'telefon', 'email', 'iban', 'cislo_op', 'datum_narodenia', 'ulica_a_cislo', 'mesto', 'psc', 'telegram_id', 'discord_id']
        return self.vykonaj_anonymizaciu(polia_na_vymaz or predvolene, dovod=dovod)

    @property
    def zostatok_dovolenky(self):
        """🧠 ISAS ABSENCE ENGINE: Dynamický výpočet zostatku dovolenky zo schválených absencií."""
        # Ochrana pred N+1. Ak je hodnota anotovaná z view, použijeme ju z RAM.
        if hasattr(self, 'vycerpane_z_absencii_db'):
            vycerpane_z_absencii = self.vycerpane_z_absencii_db
        elif not self.pk:
            vycerpane_z_absencii = 0.0
        else:
            vycerpane_z_absencii = self.nepritomnosti.filter(
                typ='DOVOLENKA', stav='SCHVALENE'
            ).aggregate(spolu=Sum('pocet_pracovnych_dni'))['spolu'] or 0.0
        
        # Zohľadníme aj manuálnu korekciu z poľa vycerpana_dovolenka (ak by účtovníčka robila korekciu)
        return float(self.narok_na_dovolenku or 0) - float(self.vycerpana_dovolenka or 0.0) - float(vycerpane_z_absencii or 0.0)

    @property
    def interny_hodinovy_naklad(self):
        """
        🧠 ISAS TRUE-MARGIN ENGINE:
        Systém tu vždy vypočíta hodinovú nákladovosť zamestnanca bez ohľadu na typ zmluvy.
        """
        if self.sposob_odmenovania == 'HODINA':
            if self.typ_pomeru == self.EXTERNI and self.mzda_zivnostnik_hodina is not None and self.mzda_zivnostnik_hodina > Decimal('0.00'):
                return self.mzda_zivnostnik_hodina
            return self.mzda_brutto_hodina if self.mzda_brutto_hodina is not None else Decimal('0.00')
        elif self.sposob_odmenovania == 'MESIAC':
            # Priemerný mesiac = 160 pracovných hodín
            if self.mzda_brutto_mesiac and self.mzda_brutto_mesiac > Decimal('0.00'):
                return (self.mzda_brutto_mesiac / Decimal('160.00')).quantize(Decimal('0.01'))
            return Decimal('0.00')
        else:
            # Režim 'UKOL' zatiaľ vracia 0, náklad si doťahuješ priamo z ceny úkonu v Katalógu.
            return Decimal('0.00')

    def save(self, *args, **kwargs):
        # 🚀 ISAS ARCHITECT: WebP Kompresia pre profilovky zamestnancov
        if self.profilova_fotka and getattr(self.profilova_fotka, 'name', None) and not self.profilova_fotka.name.lower().endswith('.webp'):
            try:                
                img = Image.open(self.profilova_fotka)
                if img.mode == "P": img = img.convert("RGBA")
                elif img.mode not in ("RGB", "RGBA"): img = img.convert("RGB")
                img.thumbnail((800, 800), Image.Resampling.LANCZOS)
                
                output = BytesIO()
                img.save(output, format='WEBP', quality=85, method=6)
                output.seek(0)
                
                nazov_bez_pripony = os.path.basename(self.profilova_fotka.name).rsplit('.', 1)[0][:85]
                self.profilova_fotka.save(name=f"{nazov_bez_pripony}.webp", content=ContentFile(output.read()), save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "👨‍💼 Zamestnanec"
        verbose_name_plural = "👨‍💼 Zamestnanci"
        permissions = [
            ("can_anonymize", "Môže vykonávať GDPR anonymizáciu"),
            ("view_pii_data", "Môže vidieť citlivé mzdové a osobné údaje"),
        ]

from Evidencia.industry_loader import CERTIFIKAT_CHOICES

class Certifikat(ISASAuditMixin, models.Model):
    """
    📜 ISAS CORE: Multiodvetvový register odborných certifikácií, oprávnení a vyhlášok.
    Kategórie sa načítavajú 100% dynamicky podľa aktívneho odvetvia cez industry_loader.py.
    """

    zamestnanec = models.ForeignKey(
        'Zamestnanec', on_delete=models.CASCADE, related_name='certifikaty',
        verbose_name='👨‍💼 Držiteľ (Zamestnanec)',
        help_text="👨‍💼 Pracovník, ktorému bolo osvedčenie alebo certifikát udelený."
    )
    kategoria = models.CharField(
        max_length=50, choices=CERTIFIKAT_CHOICES, default='BOZP_PO', db_index=True,
        verbose_name='📂 Kategória certifikácie',
        help_text="📂 Odvetvové zaradenie certifikátu pre filtre a automatický audit."
    )
    nazov = models.CharField(
        max_length=200, db_index=True,
        verbose_name='📝 Názov certifikácie / Osvedčenia',
        help_text="📝 Presný oficiálny názov (napr. '§22 Samostatný elektrotechnik' alebo 'AWS Solutions Architect')."
    )
    cislo_certifikatu = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name='🪪 Číslo osvedčenia / Evidenčné číslo',
        help_text="🪪 Jednoznačné identifikačné číslo udelené certifikačnou autoritou."
    )
    vydal_organ = models.CharField(
        max_length=150, blank=True, null=True,
        verbose_name='🏛️ Vystavil / Certifikačná autorita',
        help_text="🏛️ Názov inštitúcie, skúšobnej komisie alebo vendora (napr. TÚSR, Amazon Web Services, TÜV SÜD)."
    )
    datum_vydania = models.DateField(
        verbose_name='📅 Dátum vydania / Skúšky',
        help_text="📅 Deň úspešného absolvovania skúšky alebo vydania certifikátu."
    )
    je_dozivotny = models.BooleanField(
        default=False,
        verbose_name='♾️ Doživotná platnosť (Bez expirácie)',
        help_text="♾️ Zaškrtnite, ak certifikát neobsahuje dátum vypršania a má neobmedzenú platnosť."
    )
    datum_platnosti = models.DateField(
        blank=True, null=True, db_index=True,
        verbose_name='⏳ Platnosť do (Expirácia)',
        help_text="⏳ Dátum ukončenia platnosti certifikátu. Ponechajte prázdne, ak je doživotný."
    )
    predstih_upozornenia_dni = models.PositiveIntegerField(
        default=30,
        verbose_name='⏱️ Predstih upozornenia (Dni)',
        help_text="⏱️ Počet dní pred expiráciou, kedy začne dispečing hlásiť potrebu preškolenia."
    )
    subor = models.FileField(
        upload_to='hr_certifikaty/vseobecne/', blank=True, null=True,
        verbose_name='📎 Sken certifikátu / Osvedčenia (PDF)',
        help_text="📎 Priložený oficiálny certifikát v elektronickej podobe (PDF alebo scan)."
    )
    overovaci_odkaz = models.URLField(
        max_length=500, blank=True, null=True,
        verbose_name='🔗 Overovací odkaz (URL / Credly)',
        help_text="🔗 Verejný odkaz na online overenie pravosti certifikátu u vendora."
    )
    poznamka = models.TextField(
        blank=True, null=True,
        verbose_name='🗒️ Interná poznámka',
        help_text="📝 Doplňujúce údaje, rozsah oprávnenia, obmedzenia napätia alebo špecifikácie."
    )
    vytvorene = models.DateTimeField(auto_now_add=True, null=True, blank=True, verbose_name='📅 Vytvorené')
    upravene = models.DateTimeField(auto_now=True, null=True, blank=True, verbose_name='🔄 Upravené')

    class Meta:
        verbose_name = "📜 Odborný certifikát a osvedčenie"
        verbose_name_plural = "📜 Odborné certifikáty a osvedčenia"
        ordering = ['-je_dozivotny', '-datum_platnosti', 'nazov']

    def __str__(self):
        zamestnanec_str = "Bez držiteľa"
        if self.zamestnanec_id:
            try:
                zamestnanec_str = str(self.zamestnanec)
            except Exception:
                zamestnanec_str = f"ID #{self.zamestnanec_id}"
        return f"{self.nazov or 'Bez názvu'} ({zamestnanec_str})"

    def clean(self):
        super().clean()

        if self.je_dozivotny:
            self.datum_platnosti = None
        elif not self.datum_platnosti:
            raise ValidationError({'datum_platnosti': "Ak certifikát nie je doživotný, musíte zadať dátum ukončenia platnosti."})

        platnost = to_date(self.datum_platnosti)
        vydanie = to_date(self.datum_vydania)

        if platnost and vydanie and platnost < vydanie:
            raise ValidationError({'datum_platnosti': "Dátum ukončenia platnosti nemôže predchádzať dátumu vydania."})

    @property
    def stav_platnosti(self):
        if self.je_dozivotny:
            return 'DOZIVOTNY'
        platnost = to_date(self.datum_platnosti)
        if not platnost:
            return 'NEPLATNY'
        dnes = date.today()
        if platnost < dnes:
            return 'EXPIROVANY'
        dni_do = (platnost - dnes).days
        predstih = 30 if self.predstih_upozornenia_dni is None else self.predstih_upozornenia_dni
        if dni_do <= predstih:
            return 'BLIZI_SA'
        return 'PLATNY'

    @property
    def dni_do_expiracie(self):
        if self.je_dozivotny or not self.datum_platnosti:
            return None
        platnost = to_date(self.datum_platnosti)
        if not platnost:
            return None
        return (platnost - date.today()).days

class OdoslanaObjednavka(ISASAuditMixin, models.Model):
    dodavatel = models.ForeignKey('Dodavatel', on_delete=models.CASCADE, related_name='odoslane_objednavky', verbose_name="🏢 Dodávateľ", help_text="🏢 Dodávateľ, ktorému je objednávka určená pre doobjednanie tovaru alebo služieb.")
    cislo_objednavky = models.CharField("🏷️ Referenčné číslo", max_length=50, unique=True, help_text="🏷️ Unikátne evidenčné číslo objednávky pre párovanie s dokladmi dodávateľa a účtovníctvom.")
    
    # Skryté pole, ktoré si pamätá izolované poradie pre dodávateľa
    poradove_cislo_dodavatela = models.PositiveIntegerField("🔢 Poradie u dodávateľa", default=1, editable=False, help_text="🔢 Interné poradové číslo objednávky v rámci daného dodávateľa.")
    
    pdf_dokument = models.FileField("📄 PDF Objednávka", upload_to='objednavky_dodavatelom/%Y/%m/', blank=True, null=True, help_text="📄 Vygenerovaný PDF dokument objednávky určený na archiváciu a odoslanie.")
    vytvorene = models.DateTimeField("📅 Dátum vystavenia", auto_now_add=True, help_text="📅 Automaticky zaznamenaný dátum a čas vystavenia objednávky.")
    celkovy_pocet_poloziek = models.PositiveIntegerField("🛒 Počet položiek", default=0, help_text="🛒 Celkový počet položiek zahrnutých v tejto objednávke.")

    class Meta:
        verbose_name = "📨 Odoslaná objednávka"
        verbose_name_plural = "📨 História odoslaných objednávok"
        ordering = ['-vytvorene']

    def __str__(self):
        return f"{self.cislo_objednavky} | {self.dodavatel}"

    def save(self, *args, **kwargs):
        if not self.pk and self.dodavatel_id:
            with transaction.atomic():
                dodavatel_model = self._meta.get_field('dodavatel').remote_field.model
                dodavatel_locked = dodavatel_model.objects.select_for_update().get(pk=self.dodavatel_id)
                max_poradie = OdoslanaObjednavka.objects.filter(dodavatel=dodavatel_locked).aggregate(Max('poradove_cislo_dodavatela'))['poradove_cislo_dodavatela__max']
                self.poradove_cislo_dodavatela = (max_poradie or 0) + 1
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

class Dodavatel(ISASAuditMixin, ISASGDPRMixin, ISASERPExportMixin, models.Model):
    domena = models.CharField(max_length=100, blank=True, null=True, verbose_name="🌐 Doména", help_text="🌐 Webové sídlo dodávateľa pre rýchly prístup k B2B portálu alebo katalógu.")
    Nazov_spolocnosti = models.CharField(max_length=100, verbose_name="🏢 Názov spoločnosti", default="", help_text="🏢 Oficiálny obchodný názov firmy podľa obchodného registra pre fakturáciu.")
    ico = models.CharField(max_length=20, verbose_name="🆔 IČO", blank=True, null=True, unique=True, help_text="🆔 Identifikačné číslo organizácie pre overenie subjektu a synchronizáciu s ERP.")
    dic = models.CharField(max_length=20, verbose_name="🧾 DIČ", blank=True, null=True, help_text="🧾 Daňové identifikačné číslo pre daňové doklady a kontrolný výkaz DPH.")
    ic_dph = models.CharField(max_length=20, verbose_name="🏛️ IČ DPH", blank=True, null=True, help_text="🏛️ Identifikačné číslo pre DPH pre správne uplatnenie odpočtu DPH pri nákupe.")
    ulica_a_cislo = models.CharField(max_length=150, verbose_name="📍 Ulica a číslo", blank=True, null=True, help_text="📍 Sídlo alebo prevádzka dodávateľa pre poštovú korešpondenciu a dovoz tovaru.")
    mesto = models.CharField(max_length=100, verbose_name="🏙️ Mesto", blank=True, null=True, help_text="🏙️ Mesto sídla pre oficiálne doklady a smerovanie zásielok.")
    psc = models.CharField(max_length=20, verbose_name="📮 PSČ", blank=True, null=True, help_text="📮 Poštové smerovacie číslo pre presné doručenie poštových zásielok.")
    stat = models.CharField(max_length=50, blank=True, default="", verbose_name="🌍 Štát", help_text="🌍 Krajina sídla pre určenie daňového režimu a tuzemského či zahraničného styku.")
    iban = models.CharField(max_length=34, blank=True, null=True, verbose_name="🏦 IBAN Dodávateľa", help_text="🏦 Bankový účet v tvare IBAN pre automatické generovanie úhrad v ERP systéme.")
    predvolena_splatnost_dni = models.PositiveIntegerField(default=14, verbose_name="⏳ Predvolená splatnosť (Dni)", help_text="⏳ Štandardná lehota splatnosti dodávateľských faktúr pre plánovanie cash-flow.")
    kontaktna_osoba = models.CharField(max_length=100, blank=True, null=True, verbose_name="👤 Kontaktná osoba", help_text="👤 Meno a priezvisko zodpovedného obchodníka alebo operátora u dodávateľa.")
    oslovenie_rod = models.CharField(
        "🗣️ Oslovenie",
        max_length=10,
        choices=[
            ('', '😶 Bez oslovenia'),
            ('pán', '👨 pán'), 
            ('pani', '👩 pani')
        ],
        blank=True,
        null=True,  # Dôležité pre databázu!
        help_text="🗣️ Formálne oslovenie pre automatizovanú e-mailovú komunikáciu."
    )
    datum_narodenia = models.DateField(
        blank=True, 
        null=True, 
        verbose_name="🎂 Dátum narodenia kontaktnej osoby",
        help_text="🎂 Dátum narodenia kontaktnej osoby pre CRM pripomienky a budovanie vzťahov."
    )
    telefon = models.CharField(max_length=20, blank=True, null=True, verbose_name="📞 Telefón", help_text="📞 Priamy telefonický kontakt pre operatívne riešenie objednávok a reklamácií.")
    email = models.EmailField(verbose_name="📧 Email", blank=True, null=True, help_text="📧 E-mailová adresa na zasielanie objednávok, dopytov a komunikáciu.")
    ostatne = models.TextField(blank=True, verbose_name="📝 Ostatné", help_text="📝 Interné poznámky, špecifické dohodnuté podmienky alebo zľavy dodávateľa.")
    ai_zameranie = models.ManyToManyField(
        ContentType,
        blank=True,
        verbose_name="🤖 Ťahák pre AI (Kategórie)",
        help_text="🤖 Kategórie produktov pre automatické spárovanie nákupných dokladov pomocou AI.",
        limit_choices_to=get_skladove_kategorie_z_mapy # ⚠️ Dôležité: Bez zátvoriek () na konci!
    )
    zastupene_znacky: models.ManyToManyField = models.ManyToManyField(
        'DatabazaVyrobcov',
        blank=True,
        verbose_name="🏷️ Zastúpené značky (Križovatka pre AI)",
        help_text="🏷️ Značky a výrobcovia v sortimente dodávateľa pre urýchlenie doobjednávok dielov."
    )
    erp_predkontacia = models.CharField(
        max_length=19, blank=True, null=True, verbose_name="🧾 ERP Predkontácia dodávateľa",
        help_text="🧾 Predvolený účtovný kód predkontácie pre automatický export faktúr do ERP systému."
    )

    def get_email_greeting(self):
        return generate_dynamic_greeting(self, fallback="Dobrý deň,", use_formal=False)

    def __str__(self):
        return self.Nazov_spolocnosti

    def save(self, *args, **kwargs):
        if self.ico is not None and not str(self.ico).strip():
            self.ico = None
        if self.dic is not None and not str(self.dic).strip():
            self.dic = None
        if self.ic_dph is not None and not str(self.ic_dph).strip():
            self.ic_dph = None
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "🏢 Dodávateľ"
        verbose_name_plural = "🏢 Dodávatelia"

class Zakaznik(ISASAuditMixin, ISASGDPRMixin, ISASERPExportMixin, models.Model):
    je_interny_profil = models.BooleanField(
        "🛡️ Interný profil spoločnosti", 
        default=False, 
        editable=False, 
        db_index=True, 
        help_text="Označuje primárny profil reprezentujúci prevádzkovateľa systému pre evidenciu vlastného majetku."
    )
    domena = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="🌐 Doména",
        help_text="🌐 Webová doména zákazníka pre rýchly prístup k informáciám a verifikáciu firmy."
    )
    subdomena = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        unique=True,
        verbose_name="🔗 Subdoména portálu",
        help_text="🔗 Alias pre klientsky portál (napr. 'duslo' vytvorí prístup na duslo.telemetria.sk). Píšte len malými písmenami bez medzier."
    )
    ico = models.CharField(
        max_length=20,
        verbose_name="🆔 IČO",
        blank=True,
        null=True,
        unique=True,
        help_text="🆔 Identifikačné číslo organizácie pre fakturáciu a overenie v registroch."
    )
    dic = models.CharField(
        max_length=20,
        verbose_name="🧾 DIČ",
        blank=True,
        null=True,
        help_text="🧾 Daňové identifikačné číslo pre potreby daňových dokladov a účtovníctva."
    )
    ic_dph = models.CharField(
        max_length=20,
        verbose_name="🏛️ IČ DPH",
        blank=True,
        null=True,
        help_text="🏛️ Identifikačné číslo pre DPH potrebné pre kontrolný výkaz a daňový režim."
    )
    ulica_a_cislo = models.CharField(
        max_length=150,
        verbose_name="📍 Ulica a číslo",
        blank=True,
        null=True,
        help_text="📍 Ulica a číslo pre fakturáciu a výpočet geolokácie výjazdu."
    )
    mesto = models.CharField(
        max_length=100,
        verbose_name="🏙️ Mesto",
        blank=True,
        null=True,
        help_text="🏙️ Mesto sídla pre určenie spádovej oblasti a fakturačné účely."
    )
    psc = models.CharField(
        max_length=20,
        verbose_name="📮 PSČ",
        blank=True,
        null=True,
        help_text="📮 Poštové smerovacie číslo pre presnú poštovú a fakturačnú identifikáciu."
    )
    stat = models.CharField(
        max_length=50,
        blank=True,
        default="",
        verbose_name="🌍 Štát",
        help_text="🌍 Štát sídla firmy určujúci daňový režim a tuzemskú či zahraničnú fakturáciu."
    )
    iban = models.CharField(
        max_length=34,
        blank=True,
        null=True,
        verbose_name="🏦 IBAN Zákazníka",
        help_text="🏦 Bankový účet zákazníka uľahčujúci automatické párovanie prijatých platieb."
    )
    predvolena_splatnost_dni = models.PositiveIntegerField(
        default=14,
        verbose_name="⏳ Predvolená splatnosť (Dni)",
        help_text="⏳ Štandardná lehota splatnosti faktúr v dňoch pre riadenie pohľadávok."
    )
    reklamacna_lehota_dni = models.PositiveIntegerField(
        verbose_name="🛡️ Zmluvná lehota reklamácií / SLA (Dni)",
        blank=True,
        null=True,
        help_text="🛡️ Individuálna lehota vybavenia reklamácií podľa zmluvy s týmto partnerom (prebíja globálne aj zákonné lehoty)."
    )
    oslovenie_rod = models.CharField(
        "🗣️ Oslovenie",
        max_length=10,
        choices=[
            ('', '😶 Bez oslovenia'),
            ('pán', '👨 pán'), 
            ('pani', '👩 pani')
        ],
        blank=True,
        null=True,
        help_text="🗣️ Gramatické oslovenie pre automatizovanú a personalizovanú komunikáciu."
    )
    datum_narodenia = models.DateField(
        blank=True, 
        null=True, 
        verbose_name="🎂 Dátum narodenia kontaktnej osoby",
        help_text="🎂 Dátum narodenia pre CRM upozornenia na dashboarde a budovanie vzťahov."
    )
    Nazov_spolocnosti = models.CharField(
        max_length=100,
        verbose_name="🏢 Názov spoločnosti",
        default="",
        help_text="🏢 Obchodné meno firmy uvádzané na faktúrach a zmluvných dokladoch."
    )
    kontaktna_osoba = models.CharField(
        max_length=100,
        verbose_name="👤 Kontaktná osoba",
        blank=True,
        null=True,
        help_text="👤 Meno hlavnej zodpovednej osoby pre operatívu a servisné zásahy."
    )
    telefon= models.CharField(
        max_length=20,
        verbose_name="📞 Telefón",
        blank=True,
        null=True,
        help_text="📞 Hlavné telefónne číslo pre dispečing a rýchlu komunikáciu technikov."
    )
    email= models.EmailField(
        verbose_name="📧 Email",
        blank=True,
        null=True,
        help_text="📧 Kontaktný e-mail pre doručovanie faktúr, ponúk a servisných výkazov."
    )
    Ostatne = models.TextField(
        blank=True,
        verbose_name="📝 Ostatné",
        help_text="📝 Doplňujúce interné informácie, špecifiká zákazníka alebo dojednané zľavy."
    )
    lat = models.FloatField(
        "Zemepisná šírka (Latitude)",
        null=True,
        blank=True,
        editable=False,
        help_text="🗺️ Automaticky dopočítaná zemepisná šírka pre zobrazenie v Live Radare."
    )
    lon = models.FloatField(
        "Zemepisná dĺžka (Longitude)",
        null=True,
        blank=True,
        editable=False,
        help_text="🗺️ Automaticky dopočítaná zemepisná dĺžka pre navigáciu a výpočet diét."
    )
    vyzaduje_povolenie_vstupu = models.BooleanField(
        default=False, 
        verbose_name="🛂 Vyžaduje špeciálne povolenie na vstup (Gate Pass)",
        help_text="🛂 Označte, ak je pre prístup technikov do objektu potrebná priepustka."
    )
    email_vratnice = models.EmailField(
        blank=True, null=True, 
        verbose_name="📬 E-mail na SBS/Vrátnicu",
        help_text="📬 Kontaktný e-mail vrátnice pre automatické odesielanie priepustiek technikov."
    )
    telefon_vratnica = models.CharField(
        max_length=20, blank=True, null=True, 
        verbose_name="📞 Telefón na vrátnicu/SBS",
        help_text="📞 Rýchly telefonický kontakt pre dispečera v prípade problémov so vstupom."
    )
    instrukcie_vstupu = models.TextField(
        blank=True, null=True, 
        verbose_name="📋 Inštrukcie pre vrátnicu",
        help_text="📋 Postup a požiadavky pre autorizáciu vstupu technikov a vozidiel do areálu."
    )
    sablona_vstupu = models.FileField(
        upload_to='sablony_vstupov/', blank=True, null=True,
        verbose_name="📄 Prázdny formulár fabriky (Excel/Word/PDF)",
        help_text="📄 Prázdny formulár vyžadovaný zákazníkom na spracovanie priepustky."
    )
    url_vstupneho_portalu = models.URLField(
        max_length=500, blank=True, null=True, 
        verbose_name="🔗 Link na externý portál fabriky",
        help_text="🔗 Odkaz na externý portál pre predchádzajúcu registráciu vstupu a BOZP."
    )
    cena_za_kwh_eur = models.DecimalField(
        "⚡ Cena za elektrinu (za kWh)", 
        max_digits=5, decimal_places=3, default=Decimal('0.000'),
        help_text="Priemerná cena elektriny pre tohto zákazníka. Slúži na výpočet prevádzkových nákladov a energetickej efektivity."
    )
    emisny_faktor_co2 = models.DecimalField(
        "🍃 Emisný faktor CO2 (kg/kWh)", 
        max_digits=6, decimal_places=4, default=Decimal('0.0000'),
        help_text="Certifikovaný emisný faktor od dodávateľa elektriny zákazníka (napr. podľa energetického mixu, 0.000 pre 100% zelenú energiu). Nutné pre presný ESG audit."
    )
    # --- 🌐 WEB BUILDER INTEGRÁCIA ---
    zobrazit_ako_referenciu = models.BooleanField(
        "🌐 Súhlas so zverejnením referencie", 
        default=False,
        help_text="🌐 Ak je zaškrtnuté, názov tejto firmy sa môže zobraziť na verejnom webe (napr. pri ukážke, kde je nasadené naše prenajaté zariadenie)."
    )
    b2b_zlava_percenta = models.DecimalField(
        "💰 B2B Zľava (%)",
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        help_text="💰 Individuálna zľava pre B2B klienta automaticky aplikovaná v E-shope a Požičovni na webe."
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'is_staff': False},  # 🛡️ FIREWALL: Zákazníkom môžu byť IBA bežní B2B používatelia, nikdy nie personál!
        related_name='zakaznik_profil', verbose_name="👤 B2B Účet (Web)",
        help_text="👤 Priradený používateľský účet pre prihlásenie zákazníka do B2B zóny na verejnom webe."
    )

    def get_email_greeting(self):
        return generate_dynamic_greeting(self, fallback="Vážený zákazník / Vážená zákazníčka,", use_formal=True)

    def __str__(self):
        return self.Nazov_spolocnosti

    def save(self, *args, **kwargs):
        if self.ico is not None and not str(self.ico).strip():
            self.ico = None
        if self.dic is not None and not str(self.dic).strip():
            self.dic = None
        if self.ic_dph is not None and not str(self.ic_dph).strip():
            self.ic_dph = None
        # 🛡️ DB INTEGRITA: Ochrana pred PostgreSQL unique constraint kolíziou prázdnych reťazcov ""
        if self.subdomena is not None:
            cista_sub = str(self.subdomena).strip().lower()
            self.subdomena = cista_sub if cista_sub else None

        potrebuje_gps = False
        stara_subdomena = None
        
        # 1. Ak je to úplne nový zákazník a má adresu
        if not self.pk:
            if self.ulica_a_cislo and self.mesto:
                potrebuje_gps = True
        # 2. Ak je to existujúci zákazník, načítame stav PRED zmenou jediným dopytom
        else:
            stary_zaznam = Zakaznik.objects.filter(pk=self.pk).first()
            if stary_zaznam:
                stara_subdomena = stary_zaznam.subdomena
                if stary_zaznam.ulica_a_cislo != self.ulica_a_cislo or stary_zaznam.mesto != self.mesto:
                    potrebuje_gps = True
                elif not self.lat or not self.lon:
                    if self.ulica_a_cislo and self.mesto:
                        potrebuje_gps = True

        super().save(*args, **kwargs)

        # 🛡️ Okamžitá invalidácia cache kľúčov v Redis po commite transakcie
        def _invaliduj_tenant_cache():
            try:
                if self.subdomena:
                    cache.delete(f"isas_tenant_obj_{self.subdomena.lower()}")
                if stara_subdomena and stara_subdomena != self.subdomena:
                    cache.delete(f"isas_tenant_obj_{stara_subdomena.lower()}")
            except Exception:
                pass

        transaction.on_commit(_invaliduj_tenant_cache)

        if potrebuje_gps:
            from Evidencia.tasks import task_ziskaj_gps_zakaznika
            def _safe_dispatch_gps(doc_pk):
                try:
                    task_ziskaj_gps_zakaznika.delay(doc_pk)
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre GPS zákazníka {doc_pk}: {exc}")

            transaction.on_commit(lambda pk=self.pk: _safe_dispatch_gps(pk))

    def delete(self, *args, **kwargs):
        if self.je_interny_profil:
            raise PermissionDenied("⚠️ Kritická chyba: Profil reprezentujúci vašu vlastnú firmu je systémový a nie je možné ho vymazať!")
        
        subdomena_na_zmazanie = self.subdomena
        super().delete(*args, **kwargs)

        if subdomena_na_zmazanie:
            def _invaliduj_po_zmazani():
                try:
                    cache.delete(f"isas_tenant_obj_{subdomena_na_zmazanie.lower()}")
                except Exception:
                    pass
            transaction.on_commit(_invaliduj_po_zmazani)

    class Meta:
        verbose_name = "🏢 Zákazník"
        verbose_name_plural = "🏢 Zákazníci"
        ordering = ['id']
        constraints = [
            # 🛡️ DB INTEGRITA: V databáze smie existovať striktne iba jeden interný profil
            models.UniqueConstraint(
                fields=['je_interny_profil'],
                condition=models.Q(je_interny_profil=True),
                name='unique_interny_zakaznik_profil'
            )
        ]

class DatabazaVyrobcov(ISASAuditMixin, models.Model):
    nazov_vyrobcu = models.CharField(
        "🏷️ Názov Výrobcu", 
        max_length=200, 
        unique=True,
        help_text="🏢 Oficiálny názov výrobcu pre jednoznačnú identifikáciu v sklade a na nákupných dokladoch."
    )
    domena = models.URLField(
        "🌐 Doména webovej stránky", 
        max_length=500, 
        blank=True, null=True, 
        help_text="🌐 Oficiálny web výrobcu v tvare https://www.priklad.com pre rýchly prístup k prezentácii značky."
    )
    
    katalog_dielov_url = models.URLField(
        "🛠️ Technický portál / Katalóg dielov", 
        max_length=500, 
        blank=True, null=True, 
        help_text="🛠️ Priamy odkaz na servisný portál a katalóg náhradných dielov pre efektívnu prácu technikov."
    )

    krajina_povodu = models.CharField(
        "🌍 Krajina pôvodu", 
        max_length=100, 
        blank=True, null=True,
        help_text="🌍 Krajina pôvodu značky pre evidenciu, dodávateľské reťazce a colné špecifikácie."
    )

    TYP_VYROBY_CHOICES = [
        ('O', '💎 Iba Originály (OEM)'),
        ('N', '🔄 Iba Náhrady (Aftermarket)'),
        ('M', '🔀 Zmiešané (Robia aj O aj N)')
    ]
    typ_vyroby = models.CharField(
        "⚙️ Typ produkcie", 
        max_length=1, 
        choices=TYP_VYROBY_CHOICES, 
        default='M',
        help_text="⚙️ Špecifikácia sortimentu pre správne vyhodnotenie záruk, dodacích lehôt a cenovej stratégie."
    )
    technicka_poznamka = models.TextField(
        "📝 Interná technická poznámka", 
        blank=True, null=True,
        help_text="📝 Dôležité servisné špecifiká výrobcu určené pre technikov a nákupné oddelenie."
    )
    # --- 🌐 WEB BUILDER INTEGRÁCIA ---
    zobrazit_na_webe = models.BooleanField(
        default=False,
        verbose_name="🌐 Zobraziť značku na webe",
        help_text="🌐 Zobrazí logo tohto výrobcu v sekcii 'Značky, ktoré servisujeme'."
    )
    logo_na_webe = models.ImageField(
        upload_to='vyrobcovia_loga/', blank=True, null=True,
        verbose_name="🖼️ Logo výrobcu",
        help_text="🖼️ Logo s priehľadným pozadím (PNG/SVG) pre verejný web."
    )
    
    class Meta:
        verbose_name = "🧰 Výrobca dielov/položiek/zariadení"
        verbose_name_plural = "🧰 Databáza výrobcov"
        ordering = ['nazov_vyrobcu']

    def __str__(self):
        return self.nazov_vyrobcu

    def save(self, *args, **kwargs):
        # 🚀 ISAS ARCHITECT: WebP Kompresia pre logá výrobcov
        if self.logo_na_webe and not self.logo_na_webe.name.lower().endswith('.webp'):
            try:                
                img = Image.open(self.logo_na_webe)
                if img.mode == "P": img = img.convert("RGBA")
                elif img.mode not in ("RGB", "RGBA"): img = img.convert("RGB")
                img.thumbnail((800, 800), Image.Resampling.LANCZOS)
                
                output = BytesIO()
                img.save(output, format='WEBP', quality=85, method=6)
                output.seek(0)
                
                nazov_bez_pripony = os.path.basename(self.logo_na_webe.name).rsplit('.', 1)[0][:85]
                self.logo_na_webe.save(name=f"{nazov_bez_pripony}.webp", content=ContentFile(output.read()), save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

class PrichodziaFaktura(ISASAuditMixin, ISASERPExportMixin, models.Model):
    DPH_CHOICES = [
        ('PD', '🇸🇰 PD - Tuzemský nákup s DPH (Bežná faktúra)'),
        ('PDPH', '🇨🇿 PDPH - Tuzemský nákup s DPH (ČR)'),
        ('V', '🇪🇺 V - Tuzemský nákup / Vorsteuer (DE/AT)'),
        ('P_K', '🇵🇱 P_K - Tuzemský nákup (PL)'),
        ('VAT_IN', '🌍 VAT IN - Medzinárodný nákup'),
        ('PN', '🚫 PN - Neplatca DPH / Bez nároku na odpočet'),
        ('EU', '🇪🇺 EU - Prijaté z iného štátu EÚ (Samozdanenie)'),
        ('PP', '🏗️ PP - Prenesenie daňovej povinnosti (Stavebníctvo, kovy)'),
        ('VAT_REVERSE', '🌍 Prenesenie daňovej povinnosti / Reverse charge'),
        ('PZ', '🌍 PZ - Dovoz z tretích krajín (Mimo EÚ)'),
        ('U0', '0️⃣ U0 - Nulová sadzba / Oslobodené od DPH'),
    ]

    kod_clenenia_dph = models.CharField(
        verbose_name="🏛️ Kód členenia DPH", 
        max_length=20, 
        choices=DPH_CHOICES,
        default=get_default_kod_clenenia_prichodzia, 
        help_text="🏛️ Kód členenia DPH určuje zaradenie nákupu pre Kontrolný výkaz DPH a daňové priznanie."
    )
    subor = models.FileField(
        upload_to='faktury/%Y/%m/', 
        blank=True, 
        null=True, 
        verbose_name="📄 Súbor faktúry (Vizuál)",
        help_text="📄 Priložený vizuálny dokument faktúry (PDF alebo scan) pre kontrolu a archiváciu."
    )
    zdroj = models.CharField(
        max_length=20, 
        choices=[
            ('TELEGRAM', '✈️ Telegram'),
            ('DISCORD', '🎮 Discord'),
            ('EMAIL', '📧 Email'),
            ('PEPPOL', '🇪🇺 Peppol (XML)'),
            ('MANUAL', '✍️ Ručne nahodená')
        ], 
        default='MANUAL', 
        verbose_name="📲 Zdroj prijatia",
        help_text="📲 Vstupný kanál, prostredníctvom ktorého bola faktúra doručená do systému."
    )
    
    peppol_document_id = models.CharField(
        max_length=255, 
        blank=True, 
        null=True, 
        verbose_name="🆔 Peppol ID dokumentu", 
        help_text="🆔 Unikátny identifikátor elektronického dokladu pre verifikáciu v európskej sieti Peppol."
    )
    xml_subor = models.FileField(
        upload_to='faktury_xml/%Y/%m/', 
        blank=True, 
        null=True, 
        verbose_name="📄 Originál XML (Zákonný doklad)",
        help_text="📄 Štruktúrovaný XML súbor slúžiaci ako právne záväzný daňový originál."
    )
    xml_sha256_hash = models.CharField(
        max_length=64, 
        blank=True, 
        null=True, 
        verbose_name="🔐 SHA-256 Pečať", 
        help_text="🔐 Kryptografický odtlačok garantujúci nezmeniteľnosť dokladu v zmysle WORM legislatívy."
    )
    vizualizacia_subor = models.FileField(
        upload_to='faktury_vizualizacie/%Y/%m/', 
        blank=True, 
        null=True, 
        verbose_name="👁️ PDF Vizualizácia",
        help_text="👁️ Ľudsky čitateľná PDF kópia vygenerovaná z Peppol XML originálu."
    )
    dodavatel = models.ForeignKey(
        'Dodavatel', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='faktury', 
        verbose_name="🏢 Dodávateľ",
        help_text="🏢 Obchodný partner, ktorý faktúru vystavil a komu prináleží úhrada."
    )
    datum_prijatia = models.DateTimeField(
        auto_now_add=True, 
        verbose_name="📅 Dátum prijatia",
        help_text="📅 Časové razítko zaevidovania faktúry do systému pre audit a sledovanie spracovania."
    )
    spracovane = models.BooleanField(
        default=False, 
        verbose_name="⚙️ Spracované",
        help_text="⚙️ Označuje, či faktúra prešla automatickým vytiahnutím dát a internou kontrolou."
    )
    poznamka = models.TextField(
        blank=True, 
        verbose_name="📝 Výsledok spracovania",
        help_text="📝 Interná poznámka alebo detailný opis výsledku automatického AI spracovania dokladu."
    )

    suvisiace_objednavky: models.ManyToManyField = models.ManyToManyField(
        'OdoslanaObjednavka', 
        blank=True, 
        related_name='prijate_faktury',
        verbose_name="🛒 Spárované objednávky",
        help_text="🛒 Schválené vystavené objednávky prepojené s touto faktúrou pre kontrolovanie nákladov."
    )
    navrhnute_objednavky: models.ManyToManyField = models.ManyToManyField(
        'OdoslanaObjednavka', 
        blank=True, 
        related_name='navrhnute_faktury',
        verbose_name="🤖 AI Návrhy objednávok",
        help_text="🤖 Objednávky automaticky navrhnuté systémom na základe zhody dodávateľa a položiek."
    )
    ignorovane_objednavky: models.ManyToManyField = models.ManyToManyField(
        'OdoslanaObjednavka', 
        blank=True, 
        related_name='ignorovane_vo_fakturach',
        verbose_name="🚫 Ignorované návrhy",
        help_text="🚫 Návrhy automatického spárovania, ktoré obsluha manuálne zamietla."
    )

    iban = models.CharField(
        max_length=34, 
        blank=True, 
        null=True, 
        verbose_name="🏦 IBAN",
        help_text="🏦 Bankový účet dodávateľa pre generovanie platobných príkazov a QR kódov."
    )
    variabilny_symbol = models.CharField(
        max_length=20, 
        blank=True, 
        null=True, 
        verbose_name="🔢 Variabilný symbol",
        help_text="🔢 Variabilný symbol pre identifikáciu platby v banke a účtovnom ERP systéme."
    )

    erp_predkontacia = models.CharField(
        max_length=19, 
        blank=True, 
        null=True, 
        verbose_name="🧾 ERP Predkontácia (Hlavička)",
        help_text="🧾 Účtovná predkontácia pre ERP (Pohoda/Omega), ktorá prepisuje východiskové nastavenie."
    )

    suma_na_uhradu = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        blank=True, 
        verbose_name="💰 Suma na úhradu",
        help_text="Celková konečná finančná čiastka s DPH určená na úhradu dodávateľovi."
    )
    mena = models.CharField(
        max_length=10, 
        blank=True,
        default="", 
        verbose_name="💱 Mena dokladu",
        help_text="💱 Mena prijatého dokladu podľa firemných nastavení."
    )
    datum_splatnosti = models.DateField(
        null=True, 
        blank=True, 
        verbose_name="⏳ Dátum splatnosti",
        help_text="⏳ Krajný termín úhrady záväzku pre zamedzenie omeškaniam a penále."
    )
    
    STAV_UHRADY_CHOICES = [
        ('NEZAPLATENA', '🔴 Nezaplatená'),
        ('ZAPLATENA', '✅ Zaplatená')
    ]
    stav_uhrady = models.CharField(
        max_length=15, 
        choices=STAV_UHRADY_CHOICES, 
        default='NEZAPLATENA', 
        verbose_name="💳 Stav úhrady",
        help_text="💳 Aktuálny finančný stav úhrady záväzku pre riadenie cash-flow."
    )

    cielovy_sklad = models.ForeignKey(
        'Filialka', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        verbose_name="🏢 Cieľový sklad pre príjem",
        help_text="🏢 Skladové miesto pre fyzický príjem tovaru z faktúry (prázdne = Hlavný sklad)."
    )
    prijat_na_sklad = models.BooleanField(
        default=True, 
        verbose_name="📥 Povoliť fyzický príjem",
        help_text="📥 Riadi automatické fyzické pripočítanie kusov na skladové karty pri spracovaní."
    )
    notif_prijem_tg = models.BooleanField(
        default=True, 
        verbose_name="✈️ Telegram Notifikácia (Príjem)",
        help_text="✈️ Odošle okamžité upozornenie vedeniu cez Telegram po fyzickom naskladnení."
    )
    notif_prijem_mail = models.BooleanField(
        default=True, 
        verbose_name="✉️ E-mail Notifikácia (Príjem)",
        help_text="✉️ Odošle e-mailovú správu s rozpisom naskladnených položiek pre evidenciu."
    )
    notif_prijem_discord = models.BooleanField(
        default=True, 
        verbose_name="🎮 Discord Notifikácia (Príjem)",
        help_text="💬 Odošle okamžité upozornenie vedeniu cez Discord po fyzickom naskladnení."
    )
    class Meta:
        verbose_name = "📥 Obdržaná faktúra"
        verbose_name_plural = "📥 Obdržané faktúry"
        ordering = ['-datum_prijatia']

    def __str__(self):
        vs = self.variabilny_symbol if self.variabilny_symbol else "Bez VS"
        dodavatel_nazov = self.dodavatel.Nazov_spolocnosti if self.dodavatel else "Neznámy dodávateľ"
        
        # Ošetrenie prázdnej sumy s dynamickou menou
        mena_symbol = getattr(self, 'mena', '') or ''
        suma_str = f"{self.suma_na_uhradu} {mena_symbol}".strip() if self.suma_na_uhradu is not None else f"0.00 {mena_symbol}".strip()
        
        # Stav úhrady vyjadrený ikonou
        stav_emoji = "✅ Zaplatená" if self.stav_uhrady == 'ZAPLATENA' else "🔴 Nezaplatená"
        
        # Bezpečné ošetrenie dátumu pred prvým uložením do databázy
        datum_str = self.datum_prijatia.strftime('%d.%m.%Y') if self.datum_prijatia else "Nedatované"
        
        return f"📥 {vs} | {dodavatel_nazov} | {suma_str} | {stav_emoji} ({datum_str})"

    def save(self, *args, **kwargs):
        # Dynamické doplnenie meny a daňového členenia podľa sídla firmy s multi-tenant izoláciou
        FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentitaModel.objects.first()
        country_defaults = FiremnaIdentitaModel.get_country_defaults() if (FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'get_country_defaults')) else {}
        if not self.mena:
            self.mena = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()
        if (not self.kod_clenenia_dph or self.kod_clenenia_dph == 'VAT_IN') and country_defaults.get('kod_dph_prijata'):
            self.kod_clenenia_dph = country_defaults['kod_dph_prijata']

        # 🛡️ ISAS ARCHITECT FIX: Zistíme, či ide o cielený update (napr. odomknutie ERP)
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'mena', 'kod_clenenia_dph'}
        update_fields = kwargs.get('update_fields', None)
        is_targeted_update = update_fields is not None

        old_file = None
        force_ai = False
        
        # AI detekciu zmien robíme len pri plnom uložení (nie pri zmene ERP statusu)
        if self.pk and not is_targeted_update:
            try:
                old_instance = PrichodziaFaktura.objects.get(pk=self.pk)
                old_file = old_instance.subor
                if old_instance.spracovane and not self.spracovane:
                    force_ai = True
                    
                # 🧹 ISAS ARCHITECT FIX: Storage Leak prevencia. Ak sa súbor zmenil, starý fyzicky vymažeme.
                if old_file and self.subor and old_file != self.subor:
                    transaction.on_commit(lambda f=old_file: f.delete(save=False))

            except PrichodziaFaktura.DoesNotExist:
                pass
        
        super().save(*args, **kwargs)
        
        # 🛡️ Spustíme AI task IBA ak to nie je cielený update (s defenzívnou ochranou pred výpadkom brokera)
        if not is_targeted_update or (update_fields and any(f in update_fields for f in ['spracovane', 'subor'])):
            if not self.spracovane or force_ai:
                from Evidencia.tasks import spracuj_fakturu_ai_task
                def _bezpecne_spusti_ai(doc_pk):
                    try:
                        spracuj_fakturu_ai_task.delay(doc_pk)
                    except Exception as exc:
                        import logging
                        logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný. AI spracovanie faktúry ID {doc_pk} zlyhalo pri odoslaní: {exc}")

                transaction.on_commit(lambda pk=self.pk: _bezpecne_spusti_ai(pk))

    def delete(self, *args, **kwargs):
        # Zákonná ochrana pred zmazaním elektronickej faktúry
        if self.xml_subor or self.xml_sha256_hash:
            raise PermissionDenied("⚠️ Zákonná ochrana (WORM): Faktúru prijatú cez Peppol (XML) nie je možné zmazať. Musí byť archivovaná 10 rokov.")
        super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        # Ochrana pred úpravou elektronickej faktúry
        if self.pk:
            try:
                stary_zaznam = PrichodziaFaktura.objects.get(pk=self.pk)
                if stary_zaznam.xml_subor or stary_zaznam.xml_sha256_hash:
                    if self.suma_na_uhradu != stary_zaznam.suma_na_uhradu or self.xml_subor != stary_zaznam.xml_subor:
                        raise ValidationError("⚠️ Zákonná ochrana (WORM): Finančné údaje a XML súbor faktúry prijatej cez Peppol nie je možné upravovať.")
            except PrichodziaFaktura.DoesNotExist:
                pass

    def posli_email_uctovnicke(self):
        from Evidencia.tasks import posli_email_uctovnicke_task
        def _bezpecne_odosli_email(doc_pk):
            try:
                posli_email_uctovnicke_task.delay(doc_pk)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(f"⚠️ Celery broker nedostupný pri odosielaní emailu k faktúre {doc_pk}: {exc}")

        transaction.on_commit(lambda pk=self.pk: _bezpecne_odosli_email(pk))

    def get_qr_code_base64(self):
        """
        Generuje 100% natívny Európsky SEPA (EPC) QR kód.
        Všetky slovenské banky ho bez problémov prečítajú a vyplnia údaje.
        Bez závislosti na externých slovenských knižniciach.
        """
        # 🛡️ ISAS ARCHITECT FIX: EPC QR kód povoľuje výhradne menu EUR. Pri iných menách alebo nevyplnenej mene sa nesmie vygenerovať!
        if str(getattr(self, 'mena', '') or '').strip().upper() != 'EUR':
            return None

        if not self.iban or not self.suma_na_uhradu or self.suma_na_uhradu <= Decimal('0.00'):
            return None
            
        try:
            # 1. Očistenie a príprava dát
            iban_clean = str(self.iban).replace(" ", "").strip()
            suma_str = f"EUR{Decimal(str(self.suma_na_uhradu)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
            
            # Aby slovenská banka pochopila variabilný symbol zo SEPA formátu, 
            # najlepšie je poslať ho v tvare /VS123456/
            vs_text = f"/VS{self.variabilny_symbol}/" if self.variabilny_symbol else "Uhrada faktury"
            
            dodavatel_nazov = self.dodavatel.Nazov_spolocnosti if self.dodavatel else "Neznamy dodavatel"
            # Očistíme názov od špeciálnych znakov a obmedzíme na 70 znakov (štandard SEPA)
            dodavatel_nazov = "".join(c for c in dodavatel_nazov if c.isalnum() or c in " ._-")[:70]

            # 2. Stavba SEPA (EPC) Payloadu (Oddelené presne znakom nového riadku \n)
            # Riadky: BCD, verzia, charset(1=UTF8), SCT, BIC(prázdny), Názov, IBAN, Suma, Účel(prázdny), Referencia(VS)
            epc_lines = [
                "BCD",
                "002",
                "1",
                "SCT",
                "", 
                dodavatel_nazov,
                iban_clean,
                suma_str,
                "",  # Riadok 9: Purpose Code (prázdny)
                "",  # Riadok 10: Structured Reference (prázdny, nepoužíva sa ISO 11649)
                vs_text  # Riadok 11: Unstructured Remittance Information (Variabilný symbol / Text)
            ]
            qr_text = "\n".join(epc_lines)
            
            # 3. Vygenerovanie QR obrázka
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=4,
            )
            qr.add_data(qr_text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#0f172a", back_color="white") # Tmavá modrá ISAS
            
            # 4. Prevod do Base64 pre HTML
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
            
            return f"data:image/png;base64,{img_str}"
        except Exception as e:
            # V prípade akejkoľvek chyby pri generovaní to jednoducho nepadne
            return None

    def odosli_notifikaciu_o_prijme(self, naskladnene_logy):
        if not naskladnene_logy:
            return

        log_ids = [log.id for log in naskladnene_logy]
        from Evidencia.tasks import odosli_notifikaciu_o_prijme_task
        def _bezpecne_odosli_notifikaciu(doc_pk, ids):
            try:
                odosli_notifikaciu_o_prijme_task.delay(doc_pk, ids)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(f"⚠️ Celery broker nedostupný pri notifikácii o naskladnení pre faktúru {doc_pk}: {exc}")

        transaction.on_commit(lambda pk=self.pk, lids=log_ids: _bezpecne_odosli_notifikaciu(pk, lids))

class LogNaskladnenia(ISASAuditMixin, models.Model):
    faktura = models.ForeignKey(PrichodziaFaktura, on_delete=models.CASCADE, related_name='polozky_log', verbose_name="📄 Faktúra", help_text="📄 Došlá faktúra, ku ktorej sa príkaz viaže pre spárovanie nákupných nákladov.")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True, verbose_name="🏷️ Typ obsahu", help_text="🏷️ Typ priradeného systémového objektu pre spárovanie položky dokladu so skladom.")
    object_id = models.PositiveIntegerField(null=True, blank=True, verbose_name="🆔 ID objektu", help_text="🆔 Unikátne ID spárovanej skladovej karty alebo položky v systéme.")
    polozka = GenericForeignKey('content_type', 'object_id')
    najdeny_kod_faktura = models.CharField(max_length=100, verbose_name="🔍 Nájdený kód z faktúry", help_text="🔍 Identifikačný kód tovaru vyťažený priamo zo skenu alebo elektronickej faktúry.")
    navrhovany_nazov = models.CharField(max_length=255, blank=True, null=True, verbose_name="💡 Navrhovaný názov", help_text="💡 Automaticky rozpoznaný názov položky z dokladu pripravený na overenie.")
    hlavna_kategoria = models.CharField(max_length=100, blank=True, null=True, verbose_name="🗂️ Hlavná kategória", help_text="⚙️ Kmeňová skupina tovaru zo skladu pre analytické členenie a evidenciu.")
    navrhovana_kategoria = models.CharField(max_length=100, blank=True, null=True, verbose_name="📂 Podkategória (AI Návrh)", help_text="📂 Navrhnutá podkategória pre detailné zaradenie položky do skladového kmeňa.")
    mnozstvo = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('1.000'), verbose_name="📦 Množstvo", help_text="📦 Počet prijatých jednotiek, o ktorý sa pri spárovaní navýši fyzický stav skladu.")
    nakupna_cena = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), blank=True, verbose_name="💰 Cena z faktúry", help_text="Jednotková nákupná cena bez DPH pre správne určenie obstarávacej ceny a marže.")
    uspesne = models.BooleanField(default=False, verbose_name="✅ Zaradené do skladu", help_text="✅ Indikuje, či bola položka úspešne spracovaná a fyzicky zaradená do skladu.")
    navrhovany_vyrobca = models.ForeignKey('DatabazaVyrobcov', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="🤖 AI Výrobca", help_text="🤖 Automaticky rozpoznaný výrobca pre zrýchlenie evidencie a záručného servisu.")
    navrhovany_typ = models.CharField(max_length=1, choices=[('O', 'originál'), ('N', 'náhradný diel')], blank=True, null=True, verbose_name="🔄 AI Typ (O/N)", help_text="🔄 Klasifikácia dielu (originál vs. náhrada) pre cenotvorbu a garančné podmienky.")
    navrhovany_ean = models.CharField(max_length=50, blank=True, null=True, verbose_name="🔣 AI EAN kód", help_text="🔣 Vyťažený čiarový kód EAN pre rýchlu identifikáciu a skenovanie pri príjme.")
    navrhovana_merna_jednotka = models.CharField(max_length=20, blank=True, null=True, verbose_name="📏 AI Merná jednotka", help_text="📏 Stanovená jednotka množstva (napr. ks, l, m) pre presnú evidenciu spotreby.")
    navrhovana_sadzba_dph = models.DecimalField(max_digits=4, decimal_places=2, blank=True, null=True, default=get_default_dph, verbose_name="💰 AI Sadzba DPH (%)", help_text="💰 Sadzba DPH vyťažená z dokladu pre daňové výkazy a export do ERP.")
    navrhovane_katalogove_cislo = models.CharField(max_length=100, blank=True, null=True, verbose_name="🏷️ AI Katalógové číslo (SKU)", help_text="🏷️ Katalógové číslo dielu pre automatické zrkadlenie a doobjednávanie.")
    erp_predkontacia = models.CharField(
        max_length=19, blank=True, null=True, verbose_name="🧾 ERP Predkontácia (Položka)",
        help_text="🧾 Voliteľný účtovný kód položky prepisujúci hlavičku pre export do Pohoda/Omega."
    )
    navrhovany_popis = models.TextField(blank=True, null=True, verbose_name="📝 AI Technický popis (kto_som)", help_text="📝 Podrobný technický popis položky vyťažený z podkladov pre potreby servisu.")
    filialka = models.ForeignKey(
        Filialka, on_delete=models.CASCADE, null=True, blank=True, 
        verbose_name="🏢 Naskladniť do Filiálky",
        help_text="🏢 Určenie cieľovej pobočky; pri nevylnení sa položka priradí na Hlavný sklad."
    )

    class Meta:
        verbose_name = "📥 Príkaz na naskladnenie"
        verbose_name_plural = "📥 Príkazy na naskladnenie"
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        mnozstvo_fmt = f"{self.mnozstvo:g}" if self.mnozstvo is not None else "0"
        jednotka = "ks"
        try:
            if self.polozka and hasattr(self.polozka, 'merna_jednotka'):
                jednotka = self.polozka.merna_jednotka
        except ObjectDoesNotExist:
            pass
        return f"{self.najdeny_kod_faktura} ({mnozstvo_fmt} {jednotka})"

class StockMovement(ISASAuditMixin, models.Model):
    ADD = "ADD"
    REMOVE = "REMOVE"
    ACTION_CHOICES = [
        (ADD, "➕ Pridanie"),
        (REMOVE, "➖ Odobratie"),
    ]

    TYP_DOKLADU_CHOICES = [
        ('PRIJEMKA', '📥 Príjemka (Nákup od dodávateľa)'),
        ('VYDAJKA', '📤 Výdajka (Výdaj na faktúru/predaj)'),
        ('SPOTREBA', '🔧 Vnútropodniková spotreba (Servis/Údržba)'),
        ('PREVODKA', '🚚 Prevodka (Medzi filiálkami)'),
        ('INVENTURA', '📋 Inventúrne manko/prebytok'),
    ]
    typ_dokladu_erp = models.CharField(
        max_length=20, choices=TYP_DOKLADU_CHOICES, default='SPOTREBA',
        verbose_name="📑 Typ účtovného dokladu",
        help_text="📑 Účtovná klasifikácia pohybu pre správny export do ERP systému a vykazovanie DPH."
    )

    timestamp = models.DateTimeField(
        auto_now_add=True, verbose_name="🕒 Dátum a čas",
        help_text="⏱️ Automaticky zaznamenaný dátum a čas realizácie skladového pohybu."
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, limit_choices_to={'is_staff': True}, verbose_name="👤 Používateľ",
        help_text="👤 Zodpovedný zamestnanec, ktorý tento pohyb na sklade zaevidoval."
    )
    category = models.CharField(
        max_length=50, verbose_name="📂 Kategória",
        help_text="📂 Hlavná kategória tovaru alebo materiálu pre prehľad, kontrolu a audit zásob."
    )
    subcategory = models.CharField(
        max_length=100, verbose_name="📁 Podkategória",
        help_text="📁 Špecifická podkategória pre detailnejšie členenie skladových položiek."
    )
    product_name = models.CharField(
        max_length=200, verbose_name="📦 Produkt",
        help_text="📦 Presný názov naskladňovaného alebo vyskladňovaného tovaru či materiálu."
    )
    action = models.CharField(
        max_length=6, choices=ACTION_CHOICES, verbose_name="⚡ Akcia",
        help_text="⚡ Smer pohybu na sklade – určuje, či ide o príjem alebo výdaj zo zásob."
    )
    count = models.PositiveIntegerField(
        verbose_name="🔢 Počet kusov",
        help_text="🔢 Fyzické množstvo položiek dotknutých týmto skladovým pohybom."
    )
    merna_jednotka = models.CharField(
        max_length=10, default='ks', verbose_name="📏 Merná jednotka",
        help_text="📏 Merná jednotka množstva pre presnú evidenciu, spotrebu a účtovníctvo."
    )
    faktura = models.ForeignKey(
        PrichodziaFaktura, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="🧾 Faktúra",
        help_text="🧾 Súvisiaca nákupná faktúra pre spárovanie nákupných cien a účtovný audit."
    )
    filialka = models.ForeignKey(
        Filialka, on_delete=models.CASCADE, null=True, blank=True, 
        verbose_name="🏢 Filiálka",
        help_text="🏢 Pobočka alebo prevádzka, kde k pohybu došlo (ak zostane prázdne, ide o Hlavný sklad)."
    )

    def __str__(self):
        cas = self.timestamp.strftime('%d.%m.%Y %H:%M') if self.timestamp else "⏳ Nezaznamenané"
        return f"[{self.get_action_display()}] {self.product_name} ({self.count} {self.merna_jednotka}, {cas})"

    class Meta:
        verbose_name = "🔄 Pohyb na sklade"
        verbose_name_plural = "🔄 Pohyby na skladoch"
        ordering = ["-timestamp"]

# Tento import musí byť umiestnený až za deklaráciami bázických tried (ISASAuditMixin, ISAS_ZakladSkladu, ISAS_DielSTypom)
from .industry_loader import ZARIADENIA_CHOICES

class ServisovaneZariadenie(ISASAuditMixin, models.Model):
    KATEGORIE = ZARIADENIA_CHOICES

    zakaznik = models.ForeignKey("Zakaznik", on_delete=models.CASCADE, related_name="servisovane_zariadenia", verbose_name="🏢 Zákazník", help_text="🏢 Zákazník, ktorému zariadenie patrí alebo u ktorého je prevádzkované.")
    kategoria = models.CharField("📑 Kategória zariadenia", max_length=50, choices=KATEGORIE, help_text="📑 Technická kategória pre správne zaraďovanie v servise a výkazoch.")
    vyrobca = models.ForeignKey('DatabazaVyrobcov', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="🏭 Výrobca", help_text="🏭 Výrobca stroja pre presné doobjednávanie kompatibilných dielov.")
    model_zariadenia = models.CharField("🏷️ Typ", max_length=100, blank=True, help_text="🏷️ Presné typové označenie modelu pre identifikáciu náhradných dielov.")
    
    vyrobne_cislo = models.CharField("🔢 Výrobné číslo", max_length=100, default="Nezadané", db_index=True, help_text="🔢 Unikátne výrobné číslo pre presnú identifikáciu stroja a párovanie dielov.")
    nazov = models.CharField("📌 Interný názov / Pozícia", max_length=200, help_text="📌 Interné označenie alebo stanovište stroja v prevádzke zákazníka.", blank=True) 
    
    rok_vyroby = models.PositiveIntegerField("📅 Rok výroby", null=True, blank=True, help_text="📅 Rok výroby pre posúdenie opotrebenia a plánovanie obmeny stroja.")
    datum_instalacie = models.DateField("🛠️ Dátum inštalácie / Spustenia", null=True, blank=True, help_text="🛠️ Dátum sprevádzkovania pre výpočet záruky a plánovanie servisných cyklov.")
    
    # 🛡️ DVOJÚROVŇOVÝ ZÁRUČNÝ MANAŽMENT (ISAS INDUSTRIAL WARRANTY)
    zaruka_do = models.DateField(
        "🛡️ Štandardná záruka do", 
        null=True, blank=True, 
        help_text="🛡️ Koniec základnej záruky výrobcu (štandardne 24 mesiacov od inštalácie; ak necháte prázdne, dopočíta sa automaticky)."
    )
    predlzena_zaruka_do = models.DateField(
        "💎 Predĺžená záruka do", 
        null=True, blank=True, 
        help_text="💎 Koniec zmluvnej predĺženej záruky (napr. viacročný SLA program partnera)."
    )

    @property
    def je_v_zaruke(self):
        """🛡️ Globálna systémová property pre Live Radar, Bota a Dispečing."""
        dnes = timezone.now().date()
        if self.predlzena_zaruka_do and self.predlzena_zaruka_do >= dnes:
            return True
        if self.zaruka_do and self.zaruka_do >= dnes:
            return True
        return False

    hmotnost_kg = models.IntegerField("⚖️ Hmotnosť (kg)", null=True, blank=True, help_text="⚖️ Celková hmotnosť v kg dôležitá pre logistiku a výber manipulácie.")
    umiestnenie = models.CharField("📍 Umiestnenie u zákazníka", max_length=150, blank=True, help_text="📍 Presné miesto u zákazníka pre rýchlu orientáciu servisného technika.")

    aktualne_motohodiny = models.IntegerField(
        "⏱️ Celkové motohodiny / Prevádzkový stav", 
        default=0, 
        help_text="⏱️ Tento údaj sa aktualizuje automaticky z posledného Servisného záznamu."
    )
    motohodiny_v_zatazi = models.IntegerField(
        "🏋️ Motohodiny v záťaži / Záťažový stav", 
        default=0, 
        help_text="🏋️ Čas alebo cykly, kedy stroj reálne pracoval pod plnou prevádzkovou záťažou."
    )

    # --- 📖 DYNAMICKÉ ODVETVOVÉ PROPERTIES (INDUSTRY LEXICON) ---
    @property
    def meter_label(self):
        from .industry_loader import get_industry_term
        return get_industry_term('meter_label', 'Motohodiny')

    @property
    def meter_unit(self):
        from .industry_loader import get_industry_term
        return get_industry_term('meter_unit', 'hod.')

    @property
    def meter_load_label(self):
        from .industry_loader import get_industry_term
        return get_industry_term('meter_load_label', 'Motohodiny v záťaži')

    @property
    def meter_load_unit(self):
        from .industry_loader import get_industry_term
        return get_industry_term('meter_load_unit', 'hod.')

    @property
    @admin.display(description="Stav počítadla")
    def meter_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'aktualne_motohodiny', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    @property
    @admin.display(description="Stav v záťaži")
    def meter_load_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'motohodiny_v_zatazi', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_load_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    @property
    def verejny_nazov(self):
        """🛡️ ZERO DATA LEAK: Bezpečný verejný názov zariadenia bez úniku interného výrobného čísla klienta."""
        vyrobca_nazov = ""
        try:
            if self.vyrobca and self.vyrobca.nazov_vyrobcu:
                vyrobca_nazov = str(self.vyrobca.nazov_vyrobcu).strip()
        except ObjectDoesNotExist:
            vyrobca_nazov = ""

        # Dôsledné očistenie whitespace, aby nepreskočilo fallback
        model = (self.model_zariadenia or "").strip() or (self.nazov or "").strip()

        if vyrobca_nazov and model:
            return f"{vyrobca_nazov} {model}"
        return vyrobca_nazov or model or "Servisované zariadenie"

    @property
    def ui_parametre(self):
        """
        🚀 ISAS ARCHITECT: Dynamicky vráti zoznam kľúčových technických parametrov
        pre aktívne odvetvie bez hardkódu konkrétnych modelov či väzieb.
        """
        from .industry_loader import get_zariadenie_parametre
        param_obj = get_zariadenie_parametre(self)
        if not param_obj or not hasattr(param_obj, '_meta'):
            return []

        vysledok = []
        ignorovane = {'id', 'zariadenie', 'zariadenie_id', 'vytvorene', 'upravene'}
        for f in param_obj._meta.fields:
            if f.name in ignorovane or f.is_relation:
                continue
            val = getattr(param_obj, f.name, None)
            if val is not None and val != '' and val != '-':
                if f.choices:
                    display_method = getattr(param_obj, f"get_{f.name}_display", None)
                    val_str = display_method() if callable(display_method) else str(val)
                elif isinstance(val, bool):
                    val_str = "Áno" if val else "Nie"
                else:
                    val_str = str(val)

                vysledok.append({
                    'nazov': f.verbose_name,
                    'hodnota': val_str
                })
        return vysledok
    
    iot_mac_adresa = models.CharField("📡 MAC / Sériové číslo IoT modulu", max_length=50, blank=True, help_text="📡 Hardware MAC adresa telemetrického modulu pre vzdialený zber dát.")
    iot_ip_adresa = models.GenericIPAddressField("🌐 Lokálna IP adresa (Výroba)", null=True, blank=True, help_text="🌐 Sieťová IP adresa stroja v sieti zákazníka pre priame pripojenie.")
    iot_protokol = models.CharField("🔌 Komunikačný protokol", max_length=50, choices=[('modbus_tcp', 'Modbus TCP'), ('opc_ua', 'OPC UA'), ('mqtt', 'MQTT'), ('api', 'REST API')], blank=True, help_text="🔌 Priemyselný štandard používaný pre prenos dát zo zariadenia.")

    iot_api_kluc = models.CharField("🔑 IoT API Kľúč", max_length=64, unique=True, blank=True, null=True, db_index=True, help_text="🔑 Unikátny token pre autentifikáciu IoT modulu (generuje sa automaticky).")
    iot_je_aktivny = models.BooleanField("🟢 Aktívny IoT prenos", default=False, help_text="🟢 Zaškrtnite pre povolenie príjmu telemetrie.")
    iot_limit_zapisov_za_hodinu = models.PositiveIntegerField("⏱️ Limit zápisov (hodina)", default=60, help_text="⏱️ Maximálny počet prijatých správ za hodinu (Rate Limit).")
    
    posledna_aktualizacia_stavu = models.DateTimeField(
        "🔄 Posledná obnova dát", 
        null=True, blank=True,
        help_text="🔄 Čas posledného príjmu telemetrických dát alebo manuálnej aktualizácie."
    )
    
    v_prenajme_od_isas = models.BooleanField("🤝 V prenájme od ISAS", default=False, help_text="🤝 Označte, ak je zariadenie aktuálne fyzicky prenajaté zákazníkovi.")
    
    # --- 🚜 RENTAL FLOTILA A WEB BUILDER ---
    je_urcene_na_prenajom = models.BooleanField(
        "🚜 Patrí do Rental flotily", default=False, 
        help_text="🚜 Označte, ak je toto zariadenie majetkom vašej firmy a je určené na požičiavanie."
    )
    zobrazit_na_webe_prenajom = models.BooleanField(
        "🌐 Zobraziť v požičovni na webe", default=False, 
        help_text="🌐 Ak je zaškrtnuté, typ tohto zariadenia sa zobrazí vo verejnom katalógu požičovne."
    )
    cena_prenajmu_na_den = models.DecimalField(
        "💰 Cena prenájmu na deň", max_digits=8, decimal_places=2, default=Decimal('0.00'), blank=True, 
        help_text="Orientačná cena prenájmu bez DPH. Ak necháte prázdne (0.00), na webe bude 'Cena na vyžiadanie'."
    )
    obrazok_na_webe = models.ImageField(
        "🖼️ Fotka zariadenia (Web)", upload_to='rental_stroje/', blank=True, null=True, 
        help_text="🖼️ Reprezentatívna fotka zariadenia pre verejný katalóg požičovne."
    )
    slug = models.SlugField(
        max_length=255, unique=True, blank=True, null=True, 
        verbose_name="🔗 URL Slug",
        help_text="🔗 Unikátny URL identifikátor pre požičovňu (generuje sa automaticky)."
    )

    poznamka = models.TextField("📝 V skratke o tomto zariadení", blank=True, help_text="📝 Doplňujúce informácie o stave, špecifikách alebo histórii zariadenia.")
    qr_kod = models.ImageField("🔳 QR Kód", upload_to='qr_kody/', blank=True, null=True, help_text="🔳 Automaticky vygenerovaný QR kód pre rýchly prístup technika k histórii.")

    v_servisnom_rezime = models.BooleanField(
        "🛠️ Servisný režim (Mute Alarmov)", 
        default=False, 
        help_text="Ak je zapnuté, IoT modul naďalej zbiera dáta, ale systém NEODOSIELA žiadne alarmy ani notifikácie (ideálne počas údržby)."
    )
    bezpecnostny_kluc = models.UUIDField(
        "🔑 Verejný kľúč (Digitálny pas)", 
        unique=True, 
        null=True, 
        blank=True, 
        editable=False, 
        help_text="🔑 Unikátny kľúč pre bezpečný verejný prístup k digitálnemu pasu zariadenia cez QR kód."
    )

    class Meta:
        verbose_name = "⚙️ Servisované zariadenie"
        verbose_name_plural = "⚙️ Zoznam servisovaných zariadení"
        ordering = ['id']

    def __str__(self):
        return f"Výrobné číslo: {self.vyrobne_cislo} - (Typ: {self.model_zariadenia}); (Výrobca: {self.vyrobca})"

    def save(self, *args, **kwargs):
        # 🚀 ISAS ARCHITECT: WebP Kompresia pre Požičovňu
        if self.obrazok_na_webe and not self.obrazok_na_webe.name.lower().endswith('.webp'):
            try:                
                img = Image.open(self.obrazok_na_webe)
                if img.mode == "P": img = img.convert("RGBA")
                elif img.mode not in ("RGB", "RGBA"): img = img.convert("RGB")
                img.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
                
                output = BytesIO()
                img.save(output, format='WEBP', quality=85, method=6)
                output.seek(0)
                
                nazov_bez_pripony = os.path.basename(self.obrazok_na_webe.name).rsplit('.', 1)[0][:85]
                self.obrazok_na_webe.save(f"{nazov_bez_pripony}.webp", ContentFile(output.read()), save=False)
            except Exception:
                pass

        doplnene_update_polia = set()

        # 🤖 AUTOMATIKA: Generovanie bezpecneho API kluca ak je IoT aktivne
        if self.iot_je_aktivny and not self.iot_api_kluc:
            self.iot_api_kluc = secrets.token_urlsafe(32)
            doplnene_update_polia.add('iot_api_kluc')

        # 🤖 AUTOMATIKA: Generovanie unikátneho URL slugu pre Požičovňu
        if not self.slug:
            vyrobca_nazov = self.vyrobca.nazov_vyrobcu if self.vyrobca else ""
            base_slug = slugify(f"{vyrobca_nazov} {self.model_zariadenia or self.vyrobne_cislo}")[:240]
            if not base_slug:
                base_slug = f"zariadenie-{self.pk or 'novy'}"
            slug = base_slug
            counter = 1
            while ServisovaneZariadenie.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug[:240]}-{counter}"
                counter += 1
            self.slug = slug
            doplnene_update_polia.add('slug')

        # 🤖 AUTOMATIKA: Generovanie bezpečného kľúča (Pas stroja) ak chýba
        if not self.bezpecnostny_kluc:
            self.bezpecnostny_kluc = uuid.uuid4()
            doplnene_update_polia.add('bezpecnostny_kluc')

        # 🛡️ AUTOMATIKA: Inteligentný výpočet 24-mesačnej záruky z dátumu inštalácie
        d_inst = to_date(self.datum_instalacie)
        if d_inst and not self.zaruka_do:
            self.zaruka_do = add_calendar_interval(d_inst, years=2)
            doplnene_update_polia.add('zaruka_do')

        # 🛡️ Ochrana integrity update_fields: Novo odvodené polia nesmú byť zahodené
        if 'update_fields' in kwargs and kwargs['update_fields'] is not None and doplnene_update_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | doplnene_update_polia

        # 🛡️ ISAS ARCHITECT: Ak je stroj u nás na centrále (interný profil), NEMÔŽE byť označený ako prenajatý klientovi
        if self.zakaznik and getattr(self.zakaznik, 'je_interny_profil', False):
            self.v_prenajme_od_isas = False
            # Ak sa ukladá cez update_fields, musíme zaistiť uloženie tohto poľa do DB
            if 'update_fields' in kwargs and kwargs['update_fields'] is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'v_prenajme_od_isas'}

        stary_iot_kluc = None
        potrebuje_invalidaciu_iot = False
        if self.pk:
            stary_iot = ServisovaneZariadenie.objects.filter(pk=self.pk).values(
                'iot_api_kluc', 'iot_je_aktivny', 'iot_limit_zapisov_za_hodinu'
            ).first()
            if stary_iot:
                stary_iot_kluc = stary_iot.get('iot_api_kluc')
                if (
                    stary_iot_kluc != self.iot_api_kluc
                    or stary_iot.get('iot_je_aktivny') != self.iot_je_aktivny
                    or stary_iot.get('iot_limit_zapisov_za_hodinu') != self.iot_limit_zapisov_za_hodinu
                ):
                    potrebuje_invalidaciu_iot = True

        with transaction.atomic():
            # 1. Najprv záznam uložíme, aby zariadenie dostalo svoje ID
            super().save(*args, **kwargs)

        if potrebuje_invalidaciu_iot:
            kluce_na_vymaz = [k for k in {stary_iot_kluc, self.iot_api_kluc} if k]
            def _invaliduj_iot_cache(keys=kluce_na_vymaz):
                try:
                    cache.delete_many([f"iot_auth_{k}" for k in keys])
                except Exception:
                    pass
            transaction.on_commit(_invaliduj_iot_cache)
            
            # =========================================================
            # 🤖 AUTOMATIKA 1: GENERÁTOR QR KÓDOV
            # =========================================================
            # Ak zariadenie ešte nemá vygenerovaný QR kód
            if not self.qr_kod:
                # 🚀 ISAS ARCHITECT: Využívame čistú property clean_base_url
                try:
                    GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                    gn = GlobalneNastavenia.objects.first()
                    base_url = gn.clean_base_url if gn else getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000').rstrip('/')
                except Exception:
                    base_url = getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000').rstrip('/')
                    
                # 🚀 ISAS BIZNIS: QR kód teraz smeruje na verejný pas stroja, nie do ERP
                public_url = reverse('webbuilder:machine_passport', kwargs={'kluc': self.bezpecnostny_kluc})
                full_url = f"{base_url}{public_url}"
                
                # Vygenerovanie QR kódu
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_L,
                    box_size=10,
                    border=4,
                )
                qr.add_data(full_url)
                qr.make(fit=True)

                img = qr.make_image(fill_color="black", back_color="white")
                
                # Uloženie do pamäte a následne do modelu
                buffer = BytesIO()
                img.save(buffer, format="PNG")
                file_name = f'qr_zariadenie_{self.pk}.png'
                
                self.qr_kod.save(file_name, File(buffer), save=False)
                
                # Znovu uložíme iba toto konkrétne pole pre QR
                super().save(update_fields=['qr_kod'])

            # =========================================================
            # 🤝 AUTOMATIKA 2: SPRÁVA PRENÁJMOV (ZakaznikPrenajom)
            # =========================================================
            update_fields_set = set(kwargs.get('update_fields') or [])
            potrebuje_sync_prenajmu = (not update_fields_set) or bool(update_fields_set & {'v_prenajme_od_isas', 'zakaznik', 'zakaznik_id'})
            
            if potrebuje_sync_prenajmu:
                if self.v_prenajme_od_isas and self.zakaznik_id:
                    # Odstránime prenájmy pre iných zákazníkov na tomto stroji a zaistíme aktuálny bez MultipleObjectsReturned
                    ZakaznikPrenajom.objects.filter(zariadenie=self).exclude(zakaznik_id=self.zakaznik_id).delete()
                    if not ZakaznikPrenajom.objects.filter(zakaznik_id=self.zakaznik_id, zariadenie=self).exists():
                        ZakaznikPrenajom.objects.create(
                            zakaznik_id=self.zakaznik_id, 
                            zariadenie=self
                        )
                else:
                    ZakaznikPrenajom.objects.filter(zariadenie=self).delete()

@receiver(post_delete, sender=ServisovaneZariadenie)
def invaliduj_iot_auth_cache_pri_zmazani(sender, instance, **kwargs):
    kluc = getattr(instance, 'iot_api_kluc', None)
    if kluc:
        def _vymaz(k=kluc):
            try:
                cache.delete(f"iot_auth_{k}")
            except Exception:
                pass
        transaction.on_commit(_vymaz)

class ZakaznikPrenajom(ISASAuditMixin, models.Model):
    zakaznik = models.ForeignKey(
        "Zakaznik", 
        on_delete=models.CASCADE, 
        related_name="prenajmy", 
        verbose_name="👤 Zákazník",
        help_text="👤 Klient alebo odberateľ, ktorému je zariadenie prenechané do užívania na základe nájomnej zmluvy."
    )
    zariadenie = models.ForeignKey(
        "ServisovaneZariadenie", 
        on_delete=models.CASCADE, 
        related_name="prenajmy", 
        verbose_name="⚙️ Zariadenie",
        help_text="⚙️ Konkrétne servisované zariadenie z firemnej evidencie, ktoré je predmetom prenájmu."
    )

    class Meta:
        verbose_name = "🏷️ Prenájom zariadenia"
        verbose_name_plural = "🏷️ Prenájmy zariadení"
        constraints = [
            models.UniqueConstraint(fields=['zariadenie'], name='unique_zariadenie_prenajom')
        ]

    def clean(self):
        super().clean()
        if self.zariadenie_id:
            duplicity = ZakaznikPrenajom.objects.filter(zariadenie_id=self.zariadenie_id)
            if self.pk:
                duplicity = duplicity.exclude(pk=self.pk)
            if duplicity.exists():
                raise ValidationError({'zariadenie': "⚠️ Toto zariadenie už je evidované v prenájme. Zariadenie nemôže byť prenajaté viackrát."})

    def __str__(self):
        return f"{self.zakaznik} – {self.zariadenie.nazov}"


class HistoriaPrenajmuLog(ISASAuditMixin, models.Model):
    """📜 Štruktúrovaný auditný log presunov a nasadení zariadení v prenájme."""
    zariadenie = models.ForeignKey(
        "ServisovaneZariadenie",
        on_delete=models.CASCADE,
        related_name="historia_prenajmov",
        verbose_name="⚙️ Zariadenie"
    )
    povodny_zakaznik = models.ForeignKey(
        "Zakaznik",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prenajmy_odovzdane",
        verbose_name="🏢 Pôvodný partner / Sklad"
    )
    novy_zakaznik = models.ForeignKey(
        "Zakaznik",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prenajmy_prevzate",
        verbose_name="🏢 Nový nájomca / Sklad"
    )
    typ_akcie = models.CharField(
        "Typ akcie",
        max_length=30,
        choices=[
            ('NASADENIE', '🚀 Nasadenie do prenájmu'),
            ('PRESUN', '🤝 Presun prenájmu'),
            ('VRATENIE', '📥 Vrátenie na centrálu'),
        ]
    )
    datum_presunu = models.DateField("📅 Dátum odovzdania / presunu")
    stav_pocitadla = models.IntegerField("⏱️ Stav počítadla pri odovzdaní", null=True, blank=True)
    cislo_protokolu = models.CharField("📄 Číslo protokolu / zmluvy", max_length=100, blank=True)
    nove_umiestnenie = models.CharField("📍 Umiestnenie", max_length=150, blank=True)
    poznamka = models.TextField("📝 Doplňujúca poznámka", blank=True)
    vykonal = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="👤 Vykonal"
    )
    vytvorene = models.DateTimeField(auto_now_add=True, verbose_name="🕒 Dátum zápisu")

    class Meta:
        verbose_name = "📜 História prenájmu"
        verbose_name_plural = "📜 História prenájmov zariadení"
        ordering = ['-datum_presunu', '-id']

    def __str__(self):
        return f"{self.zariadenie} ({self.get_typ_akcie_display()} - {self.datum_presunu})"

class TechnickaSpecifikacia(ISASAuditMixin, models.Model):
    content_type = models.ForeignKey(
        ContentType, 
        on_delete=models.CASCADE, 
        verbose_name="🏷️ Typ obsahu",
        help_text="🏷️ Systémový typ záznamu, ku ktorému sa špecifikácia vzťahuje."
    )
    object_id = models.PositiveIntegerField(
        verbose_name="🆔 ID objektu",
        help_text="🆔 Číselný identifikátor konkrétnej položky v systéme."
    )
    polozka = GenericForeignKey('content_type', 'object_id')

    # Vyhľadávací index (Rovnako ako v Cenovom katalógu)
    search_name = models.CharField(
        max_length=255, 
        editable=False, 
        db_index=True, 
        null=True, 
        verbose_name="🔍 Hľadaný názov",
        help_text="🔍 Automaticky generovaný identifikátor pre rýchle vyhľadávanie."
    )

    interna_poznamka = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="💡 Interná servisná poznámka",
        help_text="💡 Interné poznámky a servisné tipy pre technikov na zefektívnenie montáže a údržby."
    )

    # --- DEDIKOVANÉ SLOTY PRE DOKUMENTÁCIU ---
    subor_manual = models.FileField(
        upload_to='manualy/', blank=True, null=True, 
        verbose_name="📖 Návod na obsluhu / Servisný manuál",
        help_text="📖 Oficiálny návod na obsluhu alebo servisný manuál na urýchlenie zásahov v teréne."
    )
    subor_schema = models.FileField(
        upload_to='schemy/', blank=True, null=True, 
        verbose_name="⚡ Elektrická / Hydraulická schéma",
        help_text="⚡ Elektrická, hydraulická alebo iná výkresová schéma potrebná pre diagnostiku a opravy."
    )
    subor_certifikat = models.FileField(
        upload_to='certifikaty/', blank=True, null=True, 
        verbose_name="📜 Certifikát / Karta bezpečnostných údajov (KBU)",
        help_text="📜 Certifikát, vyhlásenie o zhode alebo karta bezpečnostných údajov vyžadovaná legislatívou."
    )
    web_stranka = models.URLField(
        max_length=500, blank=True, null=True, 
        verbose_name="🌐 Webová stránka (Link na video / cloud)",
        help_text="🌐 Odkaz na externú dokumentáciu, inštruktážne video alebo cloudové úložisko dodávateľa."
    )

    class Meta:
        verbose_name = "📄 Technická špecifikácia dielov/položiek"
        verbose_name_plural = "📖 Technické špecifikácie"
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def save(self, *args, **kwargs):
        try:
            polozka_obj = self.polozka
        except ObjectDoesNotExist:
            polozka_obj = None

        if polozka_obj:
            if hasattr(polozka_obj, 'seriove_cislo') and polozka_obj.seriove_cislo:
                self.search_name = str(polozka_obj.seriove_cislo)[:255]
            else:
                self.search_name = str(polozka_obj)[:255]

        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'search_name'}

        super().save(*args, **kwargs)

    def __str__(self):
        try:
            return f"{self.polozka}" if self.polozka else f"Položka #{self.object_id}"
        except ObjectDoesNotExist:
            return f"Zmazaná položka #{self.object_id}"

class KosikSPolozkamiNaDoobjednanie(ISASAuditMixin, models.Model):
    nazov_polozky = models.CharField(max_length=255, verbose_name="📦 Názov položky", help_text="📦 Presný názov dielu alebo materiálu požadovaného na doobjednanie.")
    aktualny_stav = models.IntegerField(verbose_name="📊 Aktuálny stav na sklade", help_text="📊 Fyzický stav zásoby na sklade v čase zaevidovania požiadavky.")
    dodavatel = models.ForeignKey('Dodavatel', on_delete=models.CASCADE, verbose_name="🏢 Dodávateľ", null=True, blank=True, help_text="🏢 Preferovaný dodávateľ pre vystavenie nákupnej objednávky.")
    datum_zistenia = models.DateTimeField(auto_now_add=True, verbose_name="📅 Dátum vytvorenia požiadavky", help_text="📅 Dátum a čas, kedy systém automaticky zaevidoval požiadavku na doobjednanie.")
    mnozstvo_na_objednanie = models.PositiveIntegerField(default=1, verbose_name="🔢 Objednávané množstvo", help_text="🔢 Požadovaný počet jednotiek na doplnenie optimalizovanej zásoby.")
    merna_jednotka = models.CharField(max_length=10, default='ks', verbose_name="⚖️ Merná jednotka", help_text="⚖️ Základná jednotka množstva (napr. ks, m, l).") # 🟢 NOVÉ
    objednane = models.BooleanField(default=False, verbose_name="✅ Stav objednávky", help_text="✅ Priznak určujúci, či bola položka uzatvorená a zaradená do odoslanej objednávky.")
    datum_odoslania = models.DateTimeField(null=True, blank=True, verbose_name="🕒 Čas odoslania objednávky", help_text="🕒 Dátum a čas, kedy bola požiadavka spracovaná a odoslaná dodávateľovi.")
    suvisiaca_objednavka = models.ForeignKey(
        OdoslanaObjednavka, 
        on_delete=models.SET_NULL, 
        null=True, blank=True, 
        related_name="polozky",
        verbose_name="📄 Zdrojové PDF",
        help_text="📄 Odkaz na vygenerovanú dodávateľskú objednávku, v ktorej je položka zaradená."
    )
    filialka = models.ForeignKey(
        Filialka, on_delete=models.CASCADE, null=True, blank=True, 
        verbose_name="🏢 Pre filiálku",
        help_text="🏢 Cieľová pobočka alebo prevádzka. Ak zostane prázdne, tovar sa objednáva pre hlavný sklad."
    )

    class Meta:
        verbose_name = "🛒 Košík s dielmi/položkami na doobjednanie"
        verbose_name_plural = "🛒 Košík s dielmi/položkami na doobjednanie"
        ordering = ['-datum_zistenia']

    def __str__(self):
        return f"{self.nazov_polozky} ({self.mnozstvo_na_objednanie} {self.merna_jednotka})"

class GlobalneNastavenieSkladu(ISASAuditMixin, models.Model):
    zapnute_notifikacie = models.BooleanField(default=True, verbose_name="🔔 Zapnuté notifikácie", help_text="🔔 Povolenie automatických systémových upozornení na pokles skladových zásob pod minimálny limit pre včasné doobjednanie tovaru.")

    class Meta:
        verbose_name = "⚙️ Globálne nastavenie skladu"
        verbose_name_plural = "⚙️ Globálne nastavenia skladu"

    def __str__(self):
        stav = "ZAPNUTÉ" if self.zapnute_notifikacie else "VYPNUTÉ"
        return f"Notifikácie skladu: {stav}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            self.pk = 1
            super().save(*args, **kwargs)

class LimitKategorieSkladu(ISASAuditMixin, models.Model):
    kategoria_nazov = models.CharField(
        max_length=150,
        unique=True,
        verbose_name="🏷️ Názov kategórie",
        help_text="🏷️ Unikátne označenie kategórie tovaru alebo dielov pre prehľadné zoskupenie a riadenie doobjednávok."
    )
    kriticky_limit = models.PositiveIntegerField(
        default=2,
        verbose_name="⚠️ Kritický limit (ks)",
        help_text="⚠️ Minimálny počet kusov na sklade, pri poklese pod ktorý systém signalizuje potrebu doobjednania."
    )

    class Meta:
        verbose_name = "📉 Limit kategórie skladu"
        verbose_name_plural = "📉 Limity kategórií skladu"

    def __str__(self):
        return f"{self.kategoria_nazov} (Limit: {self.kriticky_limit})"

# --- DYNAMICKÉ SIGNÁLY SKLADU ---
def zapamataj_si_pocet(sender, instance, **kwargs):
    instance._povodny_pocet = getattr(instance, 'pocet_na_sklade', None)

def kontrola_stavu_zasob(sender, instance, created, **kwargs):
    """
    🚨 ISAS ENGINE SIGNAL: Tento signál počúva priamo na fyzických modeloch v DB.
    Preto vždy platí výhradne pre HLAVNÝ SKLAD.
    """
    from .industry_loader import CATEGORY_MAP, CATEGORY_CHOICES
    choices_dict = dict(CATEGORY_CHOICES or ())
    
    novy_pocet_raw = getattr(instance, 'pocet_na_sklade', None)
    # 🛡️ DEFENSIVE: Využijeme tieňový auditný stav, ak explicitný atribút chýba
    povodny_stav_map = getattr(instance, '_povodny_stav', {})
    povodny_pocet_raw = getattr(instance, '_povodny_pocet', povodny_stav_map.get('pocet_na_sklade', None))
    
    try:
        novy_pocet = int(novy_pocet_raw) if novy_pocet_raw is not None else None
    except (ValueError, TypeError):
        novy_pocet = None

    try:
        povodny_pocet = int(povodny_pocet_raw) if povodny_pocet_raw is not None else None
    except (ValueError, TypeError):
        povodny_pocet = None

    if novy_pocet is None:
        return
    
    jednotka = getattr(instance, 'merna_jednotka', 'ks')

    if created or novy_pocet != povodny_pocet:
        nazov = ocisti_nazov_od_jednotky(str(instance))
        
        hlavna_kategoria = "Ostatné"
        podkategoria = "Nezaradené"
        found = False
        
        for main_cat, subcats in CATEGORY_MAP.items():
            if found: break
            for sub_name, model_cls in subcats.items():
                if sender == model_cls:
                    hlavna_kategoria = main_cat
                    podkategoria = sub_name
                    found = True
                    break

        kategoria_s_emoji = choices_dict.get(hlavna_kategoria, f"✨ {hlavna_kategoria}")

        limit_obj, _ = LimitKategorieSkladu.objects.get_or_create(
            kategoria_nazov=podkategoria,
            defaults={'kriticky_limit': 2}
        )
        limit = limit_obj.kriticky_limit

        # 🛒 Pridanie do košíka (Defenzívne ošetrenie voči MultipleObjectsReturned a Race Conditions – výhradne pre Hlavný sklad)
        if novy_pocet <= limit:
            with transaction.atomic():
                kosik_item = KosikSPolozkamiNaDoobjednanie.objects.select_for_update().filter(
                    nazov_polozky=nazov,
                    objednane=False,
                    filialka__isnull=True
                ).first()
                
                if not kosik_item:
                    KosikSPolozkamiNaDoobjednanie.objects.create(
                        nazov_polozky=nazov,
                        objednane=False,
                        filialka=None,
                        aktualny_stav=novy_pocet,
                        merna_jednotka=jednotka,
                        dodavatel=getattr(instance, 'dodavatel', None),
                        datum_zistenia=timezone.now()
                    )
                else:
                    kosik_item.aktualny_stav = novy_pocet
                    kosik_item.merna_jednotka = jednotka
                    kosik_item.dodavatel = getattr(instance, 'dodavatel', None)
                    kosik_item.datum_zistenia = timezone.now()
                    kosik_item.save(update_fields=['aktualny_stav', 'merna_jednotka', 'dodavatel', 'datum_zistenia'])
        else:
            KosikSPolozkamiNaDoobjednanie.objects.filter(nazov_polozky=nazov, objednane=False, filialka__isnull=True).delete()

        # 🔔 4. Notifikácie (Delegovanie na Celery)
        if povodny_pocet is not None and novy_pocet == povodny_pocet:
            return

        typ_notifikacie = None
        if novy_pocet == 0:
            typ_notifikacie = 'VYPREDANE'
        elif novy_pocet <= limit and novy_pocet > 0:
            if created or (povodny_pocet is not None and povodny_pocet > limit):
                typ_notifikacie = 'KRITICKY_LIMIT'

        if typ_notifikacie:
            from Evidencia.tasks import task_odosli_notifikaciu_nizky_stav
            def _safe_dispatch_sklad_notif(n, k, p, np, j, l, tn):
                try:
                    task_odosli_notifikaciu_nizky_stav.delay(
                        nazov=n, kategoria_s_emoji=k, podkategoria=p,
                        novy_pocet=np, jednotka=j, limit=l, typ_notifikacie=tn
                    )
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre skladovú notifikáciu '{n}': {exc}")

            transaction.on_commit(lambda n=nazov, k=kategoria_s_emoji, p=podkategoria, np=novy_pocet, j=jednotka, l=limit, tn=typ_notifikacie: _safe_dispatch_sklad_notif(n, k, p, np, j, l, tn))

class TypServisuPrace(ISASAuditMixin, models.Model):
    PRIORITA_CHOICES = [
        ('NIZKA', '🟢 Nízka (Rutina)'),
        ('STREDNA', '🟡 Stredná (Štandard)'),
        ('VYSOKA', '🔴 Vysoká (Kritická)'),
    ]

    TYP_UCTOVANIA_CHOICES = [
        ('HODINA', '⏱️ Podľa odpracovaných hodín'),
        ('FIX', '🎯 Fixná cena (Úkon)'),
    ]

    nazov = models.CharField(
        max_length=255,
        verbose_name="🏷️ Názov typu práce / servisu",
        help_text="🏷️ Názov úkonu alebo servisnej práce zobrazovaný na dokladoch a v ponukách."
    )
    
    # --- 1. SPÔSOB ÚČTOVANIA A ČASOVÁ NÁROČNOSŤ ---
    je_fakturovatelna = models.BooleanField(
        default=True, 
        verbose_name="🧾 Fakturovateľná položka",
        help_text="🧾 Určuje, či sa táto práca predvolene vkladá do ponuky a účtuje zákazníkovi."
    )
    sposob_uctovania = models.CharField(
        max_length=10, choices=TYP_UCTOVANIA_CHOICES, default='HODINA',
        verbose_name="⚖️ Predvolený spôsob účtovania",
        help_text="⚙️ Predvolený spôsob kalkulácie v ponukách s možnosťou manuálnej úpravy."
    )
    normohodiny = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('1.00'), blank=True,
        verbose_name="⏱️ Normohodiny (Odhadovaný čas)",
        help_text="⏱️ Odhadovaný čas trvania úkonu v hodinách pre plánovanie kapacít a kalkuláciu nákladov."
    )
    booking_trvanie_min = models.PositiveIntegerField(
        null=True, blank=True,
        verbose_name="📅 Online rezervácia: dĺžka okna (min)",
        help_text="📅 Voliteľná dĺžka jedného časového slotu v minútach pre Stredisko / Online rezervácie (napr. 30, 60, 120). Ak zostane prázdne, použijú sa poldenné bloky."
    )

    hodinova_sadzba_predaj = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'), blank=True,
        verbose_name="💰 Hodinová sadzba - PREDAJ",
        help_text="Predajná cena bez DPH pre zákazníka za 1 hodinu práce."
    )
    fixna_cena_predaj = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'), blank=True,
        verbose_name="🎯 Fixná cena - PREDAJ",
        help_text="Paušálna predajná cena bez DPH za celý úkon bez ohľadu na trvanie."
    )

    hodinova_sadzba_naklad = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'), blank=True,
        verbose_name="📉 Hodinový náklad - INTERNÝ",
        help_text="Interné náklady na mzdu a réžiu za hodinu pre výpočet marže."
    )
    fixny_naklad = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'), blank=True,
        verbose_name="📉 Fixný náklad - INTERNÝ",
        help_text="Priamy interný náklad na úkon nepriamo závislý od času pre presnú ziskovosť."
    )

    priorita = models.CharField(
        max_length=20, choices=PRIORITA_CHOICES, default='STREDNA',
        verbose_name="🚨 Úroveň priority",
        help_text="🚨 Predvolená priorita pre automatické radenie úloh v kalendári."
    )
    zaruka_mesiace = models.PositiveIntegerField(
        default=0,
        verbose_name="🛡️ Záruka na prácu (Mesiace)",
        help_text="🛡️ Dĺžka záruky v mesiacoch uvádzaná na protokole a faktúre (0 = bez osobitnej záruky)."
    )
    pracovny_postup = models.TextField(
        blank=True, null=True,
        verbose_name="📋 SOP / Pracovný postup",
        help_text="📋 Štandardný pracovný postup zobrazený technikom pri pridelení úlohy."
    )
    zverejnit_postup_na_webe = models.BooleanField(
        default=False,
        verbose_name="🌐 Zverejniť pracovný postup na webe",
        help_text="⚠️ Ak je zaškrtnuté, text z poľa 'Pracovný postup / SOP' sa zobrazí verejným návštevníkom na detaile služby. Ponechajte vypnuté, ak postup obsahuje interné know-how."
    )
    # --- 🌐 WEB BUILDER INTEGRÁCIA ---
    zobrazit_na_webe = models.BooleanField(
        default=False,
        verbose_name="🌐 Zobraziť v katalógu na webe",
        help_text="🌐 Ak je zaškrtnuté, táto služba sa dynamicky zobrazí na verejnej webstránke."
    )
    popis_na_webe = models.TextField(
        blank=True, null=True,
        verbose_name="📝 Marketingový popis pre web",
        help_text="📝 Pútavý popis služby určený pre verejnosť (zákazníkov)."
    )

    slug = models.SlugField(
        max_length=255, unique=True, blank=True, null=True, 
        verbose_name="🔗 URL Slug",
        help_text="🔗 Unikátny URL identifikátor pre katalóg služieb (generuje sa automaticky)."
    )

    class Meta:
        verbose_name = "🗂️ Typ servisu/práce"
        verbose_name_plural = "🗂️ Typy servisov/prác"

    def __str__(self):
        return f"{self.nazov}"

    def save(self, *args, **kwargs):
        self.full_clean()
        # 🤖 AUTOMATIKA: Generovanie unikátneho URL slugu pre Služby
        if not self.slug:
            base_slug = slugify(self.nazov or f"sluzba-{self.pk or 'nova'}")[:240]
            if not base_slug:
                base_slug = f"sluzba-{self.pk or uuid.uuid4().hex[:6]}"
            slug = base_slug
            counter = 1
            while TypServisuPrace.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug[:240]}-{counter}"
                counter += 1
            self.slug = slug

        with transaction.atomic():
            super().save(*args, **kwargs)

            def _invaliduj_web_cache():
                try:
                    cache.add('web_cache_version', 1, None)
                    try:
                        cache.incr('web_cache_version')
                    except ValueError:
                        cache.set('web_cache_version', 2, None)
                except Exception:
                    pass

            transaction.on_commit(_invaliduj_web_cache)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            super().delete(*args, **kwargs)

            def _invaliduj_web_cache():
                try:
                    cache.add('web_cache_version', 1, None)
                    try:
                        cache.incr('web_cache_version')
                    except ValueError:
                        cache.set('web_cache_version', 2, None)
                except Exception:
                    pass

            transaction.on_commit(_invaliduj_web_cache)

    def clean(self):
        """
        🛡️ BIZNIS VALIDÁCIA (OWASP Databázová integrita)
        Zabráni obchodníkom/adminom uložiť nezmyselné alebo stratové kombinácie cien.
        """
        super().clean()
        
        # 0. Ošetrenie dynamicky zamknutých polí (disabled polia prehliadač neodosiela, Django ich vidí ako None)
        if self.normohodiny is None: self.normohodiny = Decimal('1.00')
        if self.hodinova_sadzba_predaj is None: self.hodinova_sadzba_predaj = Decimal('0.00')
        if self.fixna_cena_predaj is None: self.fixna_cena_predaj = Decimal('0.00')
        if self.hodinova_sadzba_naklad is None: self.hodinova_sadzba_naklad = Decimal('0.00')
        if self.fixny_naklad is None: self.fixny_naklad = Decimal('0.00')
        
        if self.booking_trvanie_min is not None and (self.booking_trvanie_min < 15 or self.booking_trvanie_min > 1440):
            raise ValidationError({'booking_trvanie_min': "⏳ Dĺžka okna online rezervácie musí byť medzi 15 a 1440 minútami (alebo nechajte prázdne)."})

        # 1. Zamedzenie záporných čísel
        if self.hodinova_sadzba_predaj < Decimal('0.00') or self.fixna_cena_predaj < Decimal('0.00'):
            raise ValidationError("⛔ Predajná cena nesmie byť záporná.")
            
        # 2. Ochrana True-Margin (Zabráni predaju pod cenu)
        if self.hodinova_sadzba_predaj > Decimal('0.00') and self.hodinova_sadzba_predaj < self.hodinova_sadzba_naklad:
            raise ValidationError("⚠️ Hodinová predajná sadzba je nižšia ako váš interný náklad. Generujete stratu!")
            
        if self.fixna_cena_predaj > Decimal('0.00') and self.fixna_cena_predaj < self.fixny_naklad:
            raise ValidationError("⚠️ Fixná predajná cena je nižšia ako váš fixný náklad. Generujete stratu!")

class Uloha(ISASAuditMixin, models.Model):
    typ_servisu: models.ManyToManyField = models.ManyToManyField(
        'TypServisuPrace',
        blank=True,
        verbose_name="🛠️ Typ servisu/práce",
        help_text="🛠️ Kategória vykonávanej práce pre normohodiny, cenníkové sadzby a vyhodnotenie typu servisu."
    )
    blizsie_informacie = models.TextField(
        verbose_name="ℹ️ Bližšie informácie ku tejto úlohe/práci",
        blank=True,
        null=True,
        help_text="📝 Detailný popis požiadavky zákazníka, špecifikácia poruchy alebo pokyny pre technika."
    )
    zakaznik = models.ForeignKey(
        'Zakaznik',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name="🏢 Zákazník",
        help_text="🏢 Odberateľ servisu pre párovanie fakturácie, výkazov a zmluvných podmienok."
    )
    zariadenia: models.ManyToManyField = models.ManyToManyField(
        'ServisovaneZariadenie',
        blank=True,
        verbose_name="⚙️ Zariadenia na servis",
        help_text="⚙️ Konkrétne stroje alebo zariadenia, na ktorých sa vykonáva servisná činnosť."
    )
    technici: models.ManyToManyField = models.ManyToManyField(
        'Zamestnanec',
        verbose_name="👨‍🔧 Priradení technici",
        help_text="👷 Zodpovední pracovníci určení na realizáciu výjazdu a evidenciu odpracovaného času."
    )
    zaciatok = SmartDateTimeField(
        db_index=True,
        verbose_name="🕒 Začiatok",
        help_text="📅 Plánovaný alebo reálny dátum a čas začiatku práce pre dispečing a SLA."
    )
    koniec = SmartDateTimeField(
        verbose_name="🏁 Koniec",
        null=True,
        blank=True,
        help_text="⏱️ Dátum a čas ukončenia práce využívaný pre výpočet trvania a rozúčtovanie."
    )
    farba = ColorField(
        default='#3788d8',
        verbose_name="🎨 Farba v kalendári",
        help_text="🎨 Farebné rozlíšenie úlohy v dispečerskom kalendári pre rýchlu vizuálnu orientáciu."
    )
    dokoncene = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Dokončené ✅",
        help_text="✅ Priamy príznak ukončenia práce umožňujúci uzatvorenie úlohy a následnú fakturáciu."
    )
    kontrolovat_sklad_pri_ulozeni = models.BooleanField(
        default=True, 
        verbose_name="🛒 Skontrolovať stav skladu a upozorniť na chýbajúce diely/položky",
        help_text="📦 Automatická kontrola dostupnosti potrebných náhradných dielov na sklade pred výjazdom."
    )
    # --- ⚠️ REKLAMAČNÝ MODUL ---
    je_reklamacia = models.BooleanField(
        default=False,
        verbose_name="⚠️ Ide o reklamáciu",
        help_text="⚠️ Označenie bezplatného záručného servisu s automatickým sledovaním dohodnutej lehoty vybavenia."
    )
    datum_prijatia_reklamacie = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="📅 Dátum prijatia reklamácie",
        help_text="⚖️ Dátum uplatnenia reklamácie zákazníkom, od ktorého sa odvíja legislatívna lehota na vybavenie."
    )

    class Meta:
        verbose_name = "📋 Úloha/Práca pre zamestnanca"
        verbose_name_plural = "📝 Úlohy a Práce"

    def __str__(self):
        if self.pk:
            zakaznik_str = str(self.zakaznik) if self.zakaznik else "Nešpecifikovaný zákazník"
            zaciatok_str = self.zaciatok.strftime('%d.%m.%Y %H:%M') if self.zaciatok else "Nezadaný čas"
            stav_ikona = "✅" if self.dokoncene else "⏳"
            
            # Načítanie ManyToMany polí (vyžiadané pre detailný prehľad)
            typy_str = ", ".join([str(t) for t in self.typ_servisu.all()]) or "Bez typu"
            zariadenia_str = ", ".join([str(z) for z in self.zariadenia.all()]) or "Bez zariadenia"
            technici_str = ", ".join([str(t) for t in self.technici.all()]) or "Bez technika"
            
            return f"{stav_ikona} Úloha #{self.pk} | 🏢 {zakaznik_str} | ⚙️ {zariadenia_str} | 🛠️ {typy_str} | 👨‍🔧 {technici_str} | 🕒 {zaciatok_str}"
        return "✨ Nová úloha"

    def clean(self):
        super().clean()
        # 🛡️ 100% NAIVE ČASOVÝ REŽIM (settings.USE_TZ = False)
        if not getattr(settings, 'USE_TZ', False):
            if self.zaciatok and timezone.is_aware(self.zaciatok):
                self.zaciatok = timezone.localtime(self.zaciatok).replace(tzinfo=None)
            if self.koniec and timezone.is_aware(self.koniec):
                self.koniec = timezone.localtime(self.koniec).replace(tzinfo=None)
        else:
            if self.zaciatok and timezone.is_naive(self.zaciatok):
                self.zaciatok = timezone.make_aware(self.zaciatok)
            if self.koniec and timezone.is_naive(self.koniec):
                self.koniec = timezone.make_aware(self.koniec)

        # ⏱️ INTELIGENTNÉ ODVODENIE KONCA (Ak nie je vyplnený v praxi):
        if self.dokoncene:
            if not self.koniec:
                # Priorita 1: Skutočný čas podpisu servisného protokolu technikom/klientom
                posledny_protokol = self.servisne_zaznamy.filter(protokol_uzavrety=True).order_by('-cas_podpisu', '-vytvorene').first() if self.pk else None
                realny_koniec = None
                if posledny_protokol:
                    realny_koniec = posledny_protokol.cas_podpisu or posledny_protokol.vytvorene
                    if not getattr(settings, 'USE_TZ', False) and realny_koniec and timezone.is_aware(realny_koniec):
                        realny_koniec = timezone.localtime(realny_koniec).replace(tzinfo=None)

                # Priorita 2: Aktuálny čas schválenia/dokončenia dispečerom
                if not realny_koniec:
                    teraz = timezone.now()
                    realny_koniec = timezone.localtime(teraz).replace(tzinfo=None) if (not getattr(settings, 'USE_TZ', False) and timezone.is_aware(teraz)) else teraz

                # 🛡️ EDGE CASE: Ochrana pred koncom skôr ako začiatok pri skorom vyriešení
                if self.zaciatok and realny_koniec < self.zaciatok:
                    self.koniec = self.zaciatok
                else:
                    self.koniec = realny_koniec
        else:
            # 🛡️ EDGE CASE: Ak dispečer zrušil príznak dokončenia (Re-open), vynulujeme automatický koniec
            if self._state.adding is False and self.pk:
                povodna_dokoncena = type(self).objects.filter(pk=self.pk).values_list('dokoncene', flat=True).first()
                if povodna_dokoncena and not self.dokoncene:
                    self.koniec = None

        if self.zaciatok and self.koniec and self.koniec < self.zaciatok:
            raise ValidationError({'koniec': "🏁 Dátum a čas konca úlohy nemôže byť skorší ako začiatok."})

    def save(self, *args, **kwargs):
        self.clean()

        # 🛡️ DEFENSIVE: Ochrana pred vynechaním poľa koniec pri cielenom update_fields
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'koniec', 'dokoncene'}

        super().save(*args, **kwargs)

        # 🔄 AUTOMATICKÁ SYNCHRONIZÁCIA ŽIVOTNÉHO CYKLU DO PRIJATEJ OBJEDNÁVKY
        if self.pk:
            nadradene_objednavky = list(self.zdrojove_objednavky_ulohy.all())
            for obj in nadradene_objednavky:
                # 1. Všetky úlohy zákazky sú dokončené -> VYBAVENÁ
                if self.dokoncene:
                    ostavajuce_nedokoncene = obj.vytvorene_ulohy.filter(dokoncene=False).exclude(pk=self.pk).exists()
                    if not ostavajuce_nedokoncene and obj.stav not in ['VYBAVENA', 'VYFAKTUROVANA', 'ZAMIETNUTA']:
                        obj.stav = 'VYBAVENA'
                        obj.save(update_fields=['stav'])
                # 2. Úloha bola znovuotvorená (re-opened) -> Vrátiť z VYBAVENÁ späť do SCHVALENA
                elif not self.dokoncene and obj.stav == 'VYBAVENA':
                    obj.stav = 'SCHVALENA'
                    obj.save(update_fields=['stav'])
                # 3. Úloha je nová a začína sa riešiť -> SCHVALENA (V riešení)
                elif not self.dokoncene and obj.stav == 'NOVA':
                    obj.stav = 'SCHVALENA'
                    obj.save(update_fields=['stav'])

    def odoslat_notifikacie_adminovi(self, poslat_tg=True, poslat_mail=True, poslat_discord=True):
        from Evidencia.tasks import uloha_odoslat_notifikacie_adminovi_task
        def _safe_admin_notif(doc_pk, tg, mail, disc):
            try:
                uloha_odoslat_notifikacie_adminovi_task.delay([doc_pk], tg, mail, disc)
            except Exception as exc:
                logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre admin notifikáciu úlohy {doc_pk}: {exc}")

        transaction.on_commit(lambda pk=self.pk, tg=poslat_tg, mail=poslat_mail, disc=poslat_discord: _safe_admin_notif(pk, tg, mail, disc))

    def poslat_vsetko_technikom(self, text_spravy, poslat_tg=True, poslat_mail=True, poslat_discord=True):
        from Evidencia.tasks import task_notifikuj_technikov_o_ulohe
        def _safe_technici_notif(doc_pk, tg, mail, disc):
            try:
                task_notifikuj_technikov_o_ulohe.delay(doc_pk, False, {}, tg, mail, disc)
            except Exception as exc:
                logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre notifikáciu technikov úlohy {doc_pk}: {exc}")

        transaction.on_commit(lambda pk=self.pk, tg=poslat_tg, mail=poslat_mail, disc=poslat_discord: _safe_technici_notif(pk, tg, mail, disc))

class PouzityMaterial(ISASAuditMixin, models.Model):
    uloha = models.ForeignKey(
        'Uloha', 
        on_delete=models.CASCADE, 
        related_name='pouzite_polozky', 
        verbose_name="📋 Úloha",
        help_text="📋 Servisná úloha, v rámci ktorej bol materiál alebo náhradný diel spotrebovaný."
    )
    
    content_type = models.ForeignKey(
        ContentType, 
        on_delete=models.CASCADE, 
        limit_choices_to=ziskaj_povolene_modely, 
        verbose_name="🗂️ Kategória dielu",
        help_text="🗂️ Typ evidovaného objektu alebo kategória dielu čerpaného zo skladu."
    )

    object_id = models.PositiveIntegerField(
        verbose_name="🔢 Sériové číslo položky", 
        null=True, 
        blank=True,
        help_text="🔢 Unikátne systémové ID konkrétneho skladového dielu pre automatické odpísanie zo zásob."
    )
    content_object = GenericForeignKey('content_type', 'object_id')
    seriove_cislo_text = models.CharField(
        "🏷️ Sériové číslo", 
        max_length=100, 
        blank=True,
        help_text="🏷️ Konkrétne sériové číslo použitého dielu pre potreby záruky a evidencie u zákazníka."
    )
    pocet = models.PositiveIntegerField(
        default=1, 
        verbose_name="🔢 Počet",
        help_text="📦 Vydané množstvo materiálu pre presné zúčtovanie a prepočet skladových zásob."
    )

    class Meta:
        verbose_name = "📦 Použitý materiál v úlohe"
        verbose_name_plural = "📦 Použitý materiál v úlohách"
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        try:
            return f"{self.content_object}" if self.content_object else f"Položka #{self.object_id}"
        except ObjectDoesNotExist:
            return f"Zmazaná položka #{self.object_id}"

class CenovyKatalog(ISASAuditMixin, models.Model):
    content_type = models.ForeignKey(
        ContentType, 
        on_delete=models.CASCADE, 
        verbose_name="🏷️ Typ obsahu",
        help_text="📦 Systémový typ priradeného objektu alebo dielu v sklade."
    )
    object_id = models.PositiveIntegerField(
        verbose_name="🆔 ID objektu",
        help_text="🆔 Unikátny identifikačný kód konkrétnej položky zo skladu."
    )
    polozka_zo_skladu = GenericForeignKey('content_type', 'object_id')

    search_name = models.CharField(
        max_length=255, 
        editable=False, 
        db_index=True, 
        null=True, 
        verbose_name="🔍 Hľadaný názov",
        help_text="🔍 Automaticky generovaný identifikátor pre rýchle vyhľadávanie v cenníku."
    )

    nakupna_cena = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'),
        validators=[MinValueValidator(0)],
        verbose_name="🛒 Nákupná cena",
        help_text="Nákupná cena od dodávateľa bez DPH slúžiaca ako základ pre výpočet marže."
    )
    marza_percento = models.DecimalField(
        max_digits=5, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        verbose_name="📈 Prirážka (%)",
        help_text="📈 Percentuálna prirážka k nákupnej cene určujúca ziskovú maržu."
    )
    predajna_cena = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'),
        verbose_name="💰 Predajná cena",
        help_text="⚠️ Predajná cena pre zákazníka bez DPH. Pri zadanej nákupnej cene sa po uložení automaticky prepočíta podľa prirážky."
    )
    posledna_aktualizacia = models.DateTimeField(
        auto_now=True, 
        verbose_name="⏱️ Posledná aktualizácia",
        help_text="⏱️ Dátum a čas poslednej úpravy cenových údajov v katalógu."
    )

    def clean(self):
        super().clean()
        if self.nakupna_cena is not None and self.nakupna_cena < Decimal('0.00'):
            raise ValidationError({'nakupna_cena': "🛒 Nákupná cena nesmie byť záporná."})
        if self.predajna_cena is not None and self.predajna_cena < Decimal('0.00'):
            raise ValidationError({'predajna_cena': "💰 Predajná cena nesmie byť záporná."})
        if self.marza_percento is not None and self.marza_percento < Decimal('-100.00'):
            raise ValidationError({'marza_percento': "📈 Prirážka nemôže byť nižšia ako -100%."})

    def save(self, *args, **kwargs):
        # Ochrana: Ošetrenie prázdnych hodnôt (aby nepadol Decimal na None)
        nakup = Decimal(str(self.nakupna_cena or '0.00'))
        percento = Decimal(str(self.marza_percento or '0.00'))
        
        # ISAS ENGINE: Inteligentný prepočet
        if nakup > Decimal('0.00'):
            # Ak máme nákupnú cenu > 0, predajnú cenu vypočítame tvrdo podľa marže
            vypocitana_cena = nakup * (Decimal('1.00') + (percento / Decimal('100.00')))
            self.predajna_cena = vypocitana_cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            # Ak nákupnú cenu nemáme (je 0.00), ale ty ručne do adminu vpíšeš predajnú, 
            # tak ti ju systém nezmaže a ponechá ju.
            if not self.predajna_cena:
                self.predajna_cena = Decimal('0.00')
        
        # Vyplnenie vyhľadávacieho poľa pre rýchle hľadanie cez Select2
        try:
            polozka = self.polozka_zo_skladu
        except ObjectDoesNotExist:
            polozka = None

        if polozka:
            if hasattr(polozka, 'seriove_cislo') and polozka.seriove_cislo:
                self.search_name = str(polozka.seriove_cislo)[:255]
            else:
                self.search_name = str(polozka)[:255]

        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'predajna_cena', 'search_name'}
            
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "📒 Cenový katalóg dielov/položiek"
        verbose_name_plural = "📒 Cenový katalóg"
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]
        # 🔒 ISAS ARCHITECT FIX: Zabráni vytvoreniu dvoch cenníkov pre ten istý diel
        unique_together = [('content_type', 'object_id')]

    def __str__(self):
        try:
            return f"{self.polozka_zo_skladu}" if self.polozka_zo_skladu else f"Položka #{self.object_id}"
        except ObjectDoesNotExist:
            return f"Zmazaná položka #{self.object_id}"

class Vozidlo(ISASAuditMixin, ISASERPExportMixin, models.Model):
    TYP_PALIVA_CHOICES = [
        ('DIESEL', '⛽ Diesel'),
        ('BENZIN', '⛽ Benzín'),
        ('LPG', '⛽ Benzín + LPG'),
        ('ELEKTRO', '⚡ Elektro / PHEV'),
    ]
    
    PNEUMATIKY_CHOICES = [
        ('LETNE', '☀️ Letné pneumatiky'),
        ('ZIMNE', '❄️ Zimné pneumatiky'),
        ('CELOROCNE', '⛅ Celoročné pneumatiky'),
    ]

    # 🚨 NOVÉ: Účtovné mapovanie pre ERP
    TYP_VOZIDLA_ERP = [
        ('OSOBNE', '🚗 Osobné vozidlo (M1)'), 
        ('NAKLADNE', '🚚 Nákladné / Dodávka (N1, N2)')
    ]
    typ_vozidla_erp = models.CharField(
        max_length=15, choices=TYP_VOZIDLA_ERP, default='OSOBNE', 
        verbose_name="📑 Kategória vozidla (ERP)", 
        help_text="⚠️ Určuje kategóriu vozidla pre správny výpočet zákonných cestovných náhrad a odpisov v účtovníctve."
    )
    je_firemne = models.BooleanField(
        default=True, 
        verbose_name="🏢 Firemný majetok", 
        help_text="🏢 Určuje, či ide o majetok firmy; pri súkromnom vozidle sa v účtovníctve uplatňuje aj amortizácia."
    )

    # 🚨 NOVÉ: Analytika a mapovanie do účtovníctva
    erp_stredisko = models.CharField(
        max_length=50, blank=True, null=True,
        verbose_name="🏢 Kód strediska (ERP)",
        help_text="🏢 Kód nákladového strediska v účtovníctve pre presné alokovanie prevádzkových nákladov vozidla."
    )
    
    ERP_PALIVO_CHOICES = [
        ('NM', '🛢️ Nafta motorová (NM)'),
        ('BA95', '⛽ Benzín 95 (BA95)'),
        ('BA98', '⛽ Benzín 98+ (BA98)'),
        ('LPG', '💨 LPG'),
        ('EL', '⚡ Elektrina'),
    ]
    erp_kod_paliva = models.CharField(
        max_length=10, choices=ERP_PALIVO_CHOICES, default='NM',
        verbose_name="🔠 Kód paliva pre knihu jázd (ERP)",
        help_text="⛽ Kód paliva pre export do účtovníctva a výpočet náhrad podľa cien Štatistického úradu."
    )

    # --- ZÁKLADNÁ IDENTIFIKÁCIA ---
    znacka_a_model = models.CharField(max_length=100, verbose_name="🚙 Vozidlo (napr. Fiat Doblo)", help_text="🚘 Značka a model vozidla pre evidenciu v tíme a na výkazoch.")
    spz = models.CharField(max_length=20, unique=True, verbose_name="🪪 ŠPZ", help_text="🪪 Evidenčné číslo vozidla (EČV/ŠPZ) slúžiace ako unikátny identifikátor.")
    priradeny_zamestnanec = models.ForeignKey('Zamestnanec', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="👤 Priradený vodič", help_text="👤 Hlavný vodič zodpovedný za prevádzku, technický stav a evidenciu jázd.")
    sukromna_osoba = models.ForeignKey('SukromnaOsoba', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="👤 Súkromná osoba (Vodič)", help_text="👤 Súkromná osoba alebo externý vodič pripojený k prevádzke vozidla.")
    vlastna_sadzba_za_km = models.DecimalField(verbose_name="💰 Vlastná fakturačná sadzba za km", max_digits=5, decimal_places=2, default=Decimal('0.00'), blank=True, help_text="Špecifická fakturačná sadzba za kilometer, ktorá má prednosť pred globálnym cenníkom.")

    # 🚨 FLEET ANOMÁLIE: Voliteľné individuálne limity tankovania pre konkrétne vozidlo
    varovanie_max_litre = models.PositiveIntegerField(
        blank=True, 
        null=True, 
        verbose_name="⛽ Varovanie: Max. litre na tankovanie", 
        help_text="Kapacita nádrže s rezervou. Pri prekročení tohto objemu systém odošle varovanie. Nechajte prázdne pre vypnutie kontroly."
    )
    varovanie_max_suma = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        verbose_name="💳 Varovanie: Max. suma tankovania", 
        help_text="Maximálna očakávaná cena jedného tankovania v mene firmy. Pri hodnote 0.00 je kontrola sumy vypnutá."
    )
    
    # --- TECHNICKÉ DÁTA A ÚDRŽBA ---
    typ_paliva = models.CharField(max_length=15, choices=TYP_PALIVA_CHOICES, default='DIESEL', verbose_name="⛽ Typ paliva", help_text="⛽ Druh pohonu pre prevádzkovú evidenciu a overenie správnosti tankovania.")
    aktualne_pneumatiky = models.CharField(max_length=15, choices=PNEUMATIKY_CHOICES, default='LETNE', verbose_name="🛞 Aktuálne obutie", help_text="🛞 Aktuálne obuté pneumatiky pre plánovanie sezónneho prezúvania a bezpečnosť.")
    
    aktualny_stav_km = models.PositiveIntegerField(default=0, verbose_name="📟 Aktuálny stav tachometra (km)", help_text="📟 Celkový stav tachometra v kilometroch pre sledovanie opotrebovania a servisu.")
    servis_interval_km = models.PositiveIntegerField(default=30000, verbose_name="🔧 Veľký servis každých (km)", help_text="🔧 Plánovaný kilometrový nájazd medzi veľkými servisnými prehliadkami.")
    posledny_servis_km = models.PositiveIntegerField(default=0, verbose_name="🛠️ KM pri posl. veľkom servise", help_text="🛠️ Stav tachometra pri poslednom vykonanom veľkom servise.")
    olej_interval_km = models.PositiveIntegerField(default=15000, verbose_name="🛢️ Výmena oleja každých (km)", help_text="🛢️ Odporúčaný kilometrový interval pre pravidelnú výmenu motorového oleja.")
    posledny_olej_km = models.PositiveIntegerField(default=0, verbose_name="💧 KM pri posl. výmene oleja", help_text="💧 Stav tachometra pri poslednej evidovanej výmene motorového oleja.")
    
    # --- LEGISLATÍVA, POISTENIE A ÚČTOVNÍCTVO ---
    cislo_palivovej_karty = models.CharField(max_length=50, blank=True, null=True, verbose_name="💳 Číslo palivovej karty", help_text="💳 Číslo priradenej tankovacej karty pre automatické párovanie účtovných dokladov.")
    stk_plati_do = models.DateField(null=True, blank=True, verbose_name="📋 STK/EK platná do", help_text="📅 Dátum exspirácie technickej a emisnej kontroly pre dodržanie legislatívnych povinností.")
    dialnicna_znamka_do = models.DateField(null=True, blank=True, verbose_name="🛣️ Diaľničná známka platná do", help_text="🛣️ Dátum platnosti diaľničnej známky na zabránenie pokutám pri služobných cestách.")
    pzp_plati_do = models.DateField(null=True, blank=True, verbose_name="🛡️ Zákonné poistenie (PZP) do", help_text="🛡️ Dátum konca platnosti povinného zmluvného poistenia.")
    kasko_plati_do = models.DateField(null=True, blank=True, verbose_name="🚗 Havarijné poistenie (KASKO) do", help_text="🚗 Dátum exspirácie havarijného poistenia pre ochranu firemného majetku.")
    lekarnicka_plati_do = models.DateField(null=True, blank=True, verbose_name="🚑 Lekárnička expirovaná od", help_text="🚑 Dátum expirácie autolekárničky pre zaistenie legislatívnej zhody vozidla.")

    class Meta:
        verbose_name = "🚘 Vozidlo"
        verbose_name_plural = "🚘 Vozidlá"
        ordering = ['znacka_a_model']

    def __str__(self):
        return f"{self.znacka_a_model} [{self.spz}]"

    # 🧠 ISAS ANALYTIKA: Vypočíta sa vždy "on-the-fly" pre karty v admine
    @property
    def celkove_naklady_palivo(self):
        # 🚀 Využíva prefetch_related cache z adminu (0 extra SQL dotazov)
        if not self.pk:
            return Decimal('0.00')
        tankovania = self.tankovania.all()
        suma = sum((t.cena_celkom for t in tankovania if t.cena_celkom), Decimal('0.00'))
        return Decimal(str(suma)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def celkom_litrov(self):
        # 🚀 Využíva prefetch_related cache z adminu (0 extra SQL dotazov)
        if not self.pk:
            return Decimal('0.00')
        tankovania = self.tankovania.all()
        litre = sum((t.litre for t in tankovania if t.litre), Decimal('0.00'))
        return Decimal(str(litre)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def priemerna_spotreba(self):
        """Presnejší odhad priemernej spotreby (Ignoruje tankovania bez zadaných KM)."""
        # 🚀 Filtrovanie a zoradenie prebieha v RAM nad prefetchnutými dátami (0 extra SQL dotazov)
        if not self.pk:
            return Decimal('0.00')
        tankovania = [t for t in self.tankovania.all() if t.stav_tachometra and t.stav_tachometra > 0]
        tankovania.sort(key=lambda x: x.stav_tachometra)
        
        if len(tankovania) < 2:
            return Decimal('0.00')
        
        prve_km = tankovania[0].stav_tachometra
        posledne_km = tankovania[-1].stav_tachometra
        rozdiel_km = posledne_km - prve_km
        
        spotrebovane_litre = sum(((t.litre or Decimal('0.00')) for t in tankovania[1:]), Decimal('0.00'))
        
        if rozdiel_km > 0 and spotrebovane_litre > Decimal('0.00'):
            return ((Decimal(str(spotrebovane_litre)) / Decimal(str(rozdiel_km))) * Decimal('100.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return Decimal('0.00')

class Tankovanie(ISASAuditMixin, ISASERPExportMixin, models.Model):
    SPOSOB_PLATBY = [
        ('PALIVOVA', '💳 Palivová karta'),
        ('PLATOBNA', '📱 Banková karta'),
        ('HOTOVOST', '💵 Hotovosť'),
    ]

    DPH_CHOICES = [
        ('PD', '🇸🇰 PD - Tuzemský nákup s DPH (Štandardný bloček z eKasy) 🧾'),
        ('PDPH', '🇨🇿 PDPH - Tuzemský nákup s DPH (ČR) 🧾'),
        ('V', '🇪🇺 V - Tuzemský nákup / Vorsteuer (DE/AT) 🧾'),
        ('P_K', '🇵🇱 P_K - Tuzemský nákup (PL) 🧾'),
        ('VAT_IN', '🌍 VAT IN - Medzinárodný nákup 🧾'),
        ('PN', '🚫 PN - Neplatca DPH / Bez nároku na odpočet ❌'),
        ('EU', '🇪🇺 EU - Nákup paliva v zahraničí (EÚ) 🌍'),
    ]
    kod_clenenia_dph = models.CharField(
        verbose_name="🏛️ Kód členenia DPH (ERP)", 
        max_length=10, 
        choices=DPH_CHOICES,
        default=get_default_kod_clenenia_prichodzia, 
        help_text="🏛️ Určuje zaradenie v Kontrolnom výkaze DPH. Pre bežné tuzemské doklady ponechajte štandardné členenie."
    )

    # 2. Spôsob zaúčtovania (Absolútne kritické pre XML)
    ERP_UCTOVANIE_CHOICES = [
        ('FIREMNY_UCET', '💳 Firemná karta (Ide do banky/pokladne)'),
        ('SUKROMNE_PENIAZE', '💵 Platil vodič z vlastného (Vytvorí sa Záväzok na preplatenie) 🤝'),
        ('FAKTURA', '📄 Bude hradené zbernou faktúrou (CCS / Slovnaft karta) - Neúčtovať bloček! 🛑'),
    ]
    sposob_zauctovania_erp = models.CharField(
        max_length=20, choices=ERP_UCTOVANIE_CHOICES, default='FIREMNY_UCET',
        verbose_name="⚙️ Spôsob zaúčtovania v ERP",
        help_text="⚙️ Určuje cieľovú účtovnú agendu v ERP systéme pre správne zaúčtovanie nákladu alebo záväzku."
    )

    vozidlo = models.ForeignKey(
        Vozidlo, 
        on_delete=models.CASCADE, 
        related_name="tankovania", 
        verbose_name="🚗 Vozidlo",
        help_text="🚗 Firemné vozidlo, pre ktoré bolo palivo načerpané."
    )
    vodič = models.ForeignKey(
        'Zamestnanec', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        verbose_name="👤 Vodič - zamestnanec",
        help_text="👤 Zamestnanec, ktorý vykonal tankovanie a zodpovedá za priložený doklad."
    )
    osoba = models.ForeignKey(
        'SukromnaOsoba', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        verbose_name="👤 Vodič - súkromná osoba",
        help_text="👤 Súkromná osoba alebo externý vodič, ak tankovanie nevykonal interný zamestnanec."
    )
    
    datum = models.DateTimeField(
        default=timezone.now, 
        verbose_name="📅 Dátum a čas",
        help_text="📅 Presný dátum a čas tankovania podľa pokladničného dokladu."
    )
    stav_tachometra = models.PositiveIntegerField(
        null=True, 
        blank=True, 
        verbose_name="🔢 Tachometer pri tankovaní",
        help_text="🔢 Aktuálny stav počítadla kilometrov pri tankovaní pre kontrolu spotreby a aktualizáciu stavu vozidla."
    )

    cerpacia_stanica = models.ForeignKey(
        'Dodavatel', on_delete=models.SET_NULL, null=True, blank=True, 
        verbose_name="🏢 Čerpacia stanica (Dodávateľ)", 
        help_text="🏢 Dodávateľ paliva vyžadovaný pre správny odpočet DPH a výkazníctvo, okrem zberných faktúr."
    )
    cislo_dokladu = models.CharField(
        max_length=50, blank=True, null=True, 
        verbose_name="🧾 Číslo dokladu z eKasy", 
        help_text="🧾 Identifikačné číslo dokladu z eKasy vyžadované účtovným systémom na párovanie a audit."
    )
    
    litre = models.DecimalField(
        max_digits=6, decimal_places=2, 
        verbose_name="⛽ Litre (l)",
        help_text="⛽ Celkový objem načerpaného paliva v litroch."
    )
    sadzba_dph = models.DecimalField(
        max_digits=4, decimal_places=2, default=get_default_dph, 
        verbose_name="🏛️ Sadzba DPH (%)",
        help_text="Aplikovaná percentuálna sadzba DPH pre výpočet nákladov a odpočtu."
    )
    cena_celkom = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'),
        verbose_name="💰 Cena celkom s DPH", 
        help_text="Celková úhrada za tankovanie s DPH uvedená na pokladničnom bloku."
    )
    cena_bez_dph = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'), editable=False, 
        verbose_name="💰 Suma bez DPH",
        help_text="Vypočítaná suma základu dane bez DPH pre účtovný rozpad nákladov."
    )
    suma_dph = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('0.00'), editable=False, 
        verbose_name="🏛️ Suma DPH",
        help_text="Vypočítaná výška DPH pre uplatnenie odpočtu na dani."
    )
    
    mena = models.CharField(
        max_length=10, 
        blank=True,
        default="", 
        verbose_name="💱 Mena dokladu",
        help_text="💱 Mena pokladničného dokladu za tankovanie podľa firemných nastavení."
    )
    foto_blocku = models.ImageField(
        upload_to='blocky/%Y/%m/', 
        verbose_name="📸 Foto bločku",
        help_text="📸 Scan alebo fotografia pokladničného dokladu pre audit a archiváciu."
    )
    sposob_platby = models.CharField(
        max_length=10, choices=SPOSOB_PLATBY, default='PALIVOVA', 
        verbose_name="💳 Spôsob platby",
        help_text="💳 Použitý platobný prostriedok pri úhrade na čerpacej stanici."
    )

    class Meta:
        verbose_name = "⛽ Záznam o tankovaní"
        verbose_name_plural = "⛽ Evidencia tankovaní"
        ordering = ['-datum']

    def __str__(self):
        mena_str = f" {self.mena}".rstrip() if getattr(self, 'mena', None) else ""
        datum_str = self.datum.strftime('%d.%m.%Y') if self.datum else "Nedatované"
        return f"{self.vozidlo.spz} - {self.litre}l | {self.cena_celkom}{mena_str} ({datum_str})"

    @property
    def cena_za_liter(self):
        if self.litre and self.litre > Decimal('0.00') and self.cena_celkom is not None:
            return (self.cena_celkom / self.litre).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return Decimal('0.00')

    def clean(self):
        super().clean()

        if self.litre is not None and self.litre <= Decimal('0.00'):
            raise ValidationError({"litre": "⛽ Objem načerpaného paliva musí byť väčší ako 0 litrov."})

        if self.cena_celkom is not None and self.cena_celkom < Decimal('0.00'):
            raise ValidationError({"cena_celkom": "💰 Celková cena tankovania nesmie byť záporná."})

        if self.sposob_zauctovania_erp != 'FAKTURA' and not self.cerpacia_stanica_id:
            raise ValidationError({"cerpacia_stanica": "⚠️ Pre priame platby (karta/hotovosť) musíte vybrať Čerpaciu stanicu kvôli odpočtu DPH a exportu! 🏢"})

    def save(self, *args, **kwargs):
        FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentitaModel.objects.first()
        country_defaults = FiremnaIdentitaModel.get_country_defaults() if (FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'get_country_defaults')) else {}
        if not self.mena:
            self.mena = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()
        if (not self.kod_clenenia_dph or self.kod_clenenia_dph == 'VAT_IN') and country_defaults.get('kod_dph_prijata'):
            self.kod_clenenia_dph = country_defaults['kod_dph_prijata']

        # 1. Automatický rozpad DPH do skrytých polí
        if self.cena_celkom is not None and self.sadzba_dph is not None:
            koeficient = Decimal('1.00') + (Decimal(str(self.sadzba_dph)) / Decimal('100.00'))
            if koeficient > Decimal('0.00'):
                self.cena_bez_dph = (Decimal(str(self.cena_celkom)) / koeficient).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                self.cena_bez_dph = Decimal(str(self.cena_celkom))
            self.suma_dph = Decimal(str(self.cena_celkom)) - self.cena_bez_dph

        # 🛡️ DEFENSIVE: Ak ukladáme cez update_fields, musíme zabezpečiť uloženie vypočítanej DPH, meny a daňového členenia
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'cena_bez_dph', 'suma_dph', 'mena', 'kod_clenenia_dph'}

        # 2. Aktualizácia tachometra vo vozidle a uloženie záznamu v JEDNEJ transakcii
        with transaction.atomic():
            if self.stav_tachometra is not None and self.vozidlo_id:
                # Uzamkneme vozidlo, aby sa kilometre neprepísali inou jazdou/tankovaním
                vozidlo_locked = Vozidlo.objects.select_for_update().get(pk=self.vozidlo_id)
                if self.stav_tachometra > vozidlo_locked.aktualny_stav_km:
                    vozidlo_locked.aktualny_stav_km = self.stav_tachometra
                    vozidlo_locked.save(update_fields=['aktualny_stav_km'])
                    
            super().save(*args, **kwargs)

class EvidenciaJazd(ISASAuditMixin, models.Model):
    TYP_JAZDY_CHOICES = [
        ('FIREMNA', '💼 Firemná jazda'),
        ('SUKROMNA', '🏖️ Súkromná jazda'),
    ]

    vozidlo = models.ForeignKey(Vozidlo, on_delete=models.CASCADE, related_name="jazdy", verbose_name="🚗 Vozidlo", help_text="Služobné alebo súkromné motorové vozidlo použité na túto jazdu.")
    typ_jazdy = models.CharField(max_length=15, choices=TYP_JAZDY_CHOICES, default='FIREMNA', verbose_name="🏷️ Typ jazdy", help_text="Určuje daňovú obhájiteľnosť cesty (služobná jazda do nákladov vs. súkromné kilometre).")
    
    zamestnanec = models.ForeignKey('Zamestnanec', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="👷 Vodič (Zamestnanec)", help_text="Interný zamestnanec alebo technik, ktorý vozidlo šoféroval.")
    sukromna_osoba = models.ForeignKey('SukromnaOsoba', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="👤 Vodič (Súkromná osoba)", help_text="Vodič mimo zamestnaneckého pomeru (napr. externý spolupracovník alebo rodinný príslušník).")

    stat_startu = models.CharField(
        max_length=50, blank=True, default="", 
        verbose_name="🌍 Štát odjazdu", 
        help_text="Dôležité pre správny výpočet stravného v ERP. 🍲"
    )
    stat_ciela = models.CharField(
        max_length=50, blank=True, default="", 
        verbose_name="🌍 Štát príchodu", 
        help_text="Ak ide o zahraničnú pracovnú cestu, zmeňte štát. ✈️"
    )

    erp_zakazka = models.CharField(
        max_length=50, blank=True, null=True,
        verbose_name="💼 Kód zákazky (ERP)",
        help_text="Ak bola jazda viazaná na konkrétny projekt v účtovníctve, zadajte kód zákazky. 🔗"
    )
    
    noclah_zabezpeceny = models.BooleanField(
        "🛏️ Zabezpečené ubytovanie", default=False, 
        help_text="⚠️ Ak zamestnanec prespal v hoteli a doniesol faktúru, zaškrtnite. Mení to výpočet cestovného. 🌙"
    )
    
    spolujazdci: models.ManyToManyField = models.ManyToManyField('Zamestnanec', related_name='spolujazdy', verbose_name="👥 Spolujazdci", blank=True, help_text="💡 Účtovníctvo im automaticky vygeneruje cestovný príkaz a nárok na diéty.")
    
    miesto_startu = models.CharField(max_length=150, default=get_default_miesto_startu, blank=True, verbose_name="📍 Začiatok (Textom)", help_text="Slovný popis štartovacieho miesta (napr. 'Sklad Nitra' alebo ulica), ak nevyberáte entitu nižšie.")
    zakaznik_start = models.ForeignKey('Zakaznik', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_start', verbose_name="🏢 Začiatok / Zákazník", help_text="Vyberte, ak jazda začínala priamo v sídle alebo prevádzke zákazníka.")
    dodavatel_start = models.ForeignKey('Dodavatel', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_dodavatel_start', verbose_name="🏭 Začiatok / Dodávateľ", help_text="Vyberte, ak jazda začínala u dodávateľa (napr. vyzdvihnutie dielov v sklade vendor-a).")
    filialka_start = models.ForeignKey('Filialka', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_filialka_start', verbose_name="🏪 Začiatok / Filiálka", help_text="Vyberte, ak sa vyrážalo z vlastnej pobočky alebo prevádzky firmy.")
    zamestnanec_start = models.ForeignKey('Zamestnanec', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_z_domu', verbose_name="🏠 Začiatok / Bydlisko (Zamestnanec)", help_text="Vyberte, ak technik vyrážal na výjazd priamo z miesta svojho bydliska.")
    sukromna_osoba_start = models.ForeignKey('SukromnaOsoba', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_z_domu_so', verbose_name="👤 Začiatok / Bydlisko (Súkr. osoba)", help_text="Vyberte, ak cesta začínala v bydlisku súkromnej osoby.")

    miesto_ciela = models.CharField(max_length=150, default=get_default_miesto_startu, blank=True, verbose_name="🏁 Cieľ (Textom)", help_text="Slovný popis cieľovej lokality (napr. 'Mesto Žilina' alebo adresa), ak nevyberáte entitu nižšie.")
    zakaznik = models.ForeignKey('Zakaznik', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_ciel', verbose_name="🏢 Cieľ / Zákazník", help_text="Cieľový zákazník, u ktorého sa vykonával servis. Kľúčové pre prefaktúrovanie dopravy.")
    dodavatel_ciel = models.ForeignKey('Dodavatel', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_dodavatel_ciel', verbose_name="🏭 Cieľ / Dodávateľ", help_text="Vyberte, ak bola cieľom cesty návšteva dodávateľa alebo odber materiálu.")
    filialka_ciel = models.ForeignKey('Filialka', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_filialka_ciel', verbose_name="🏪 Cieľ / Filiálka", help_text="Vyberte, ak bola cieľom cesty vlastná pobočka firmy.")
    zamestnanec_ciel = models.ForeignKey('Zamestnanec', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_domov', verbose_name="🏠 Cieľ / Bydlisko (Zamestnanec)", help_text="Vyberte, ak jazda končila návratom technika priamo do jeho bydliska.")
    sukromna_osoba_ciel = models.ForeignKey('SukromnaOsoba', on_delete=models.SET_NULL, null=True, blank=True, related_name='jazdy_domov_so', verbose_name="👤 Cieľ / Bydlisko (Súkr. osoba)", help_text="Vyberte, ak jazda končila v bydlisku súkromnej osoby.")

    uloha = models.ForeignKey('Uloha', on_delete=models.SET_NULL, null=True, blank=True, related_name='vyjazdy', verbose_name="📋 Priradená Úloha", help_text="Prepojenie výjazdu priamo na servisnú úlohu pre automatické spárovanie výdavkov.")
    ucel_cesty = models.CharField(max_length=255, blank=True, null=True, verbose_name="🎯 Účel cesty", help_text="Dôvod cesty pre daňovú obhájiteľnosť (napr. 'Pravidelná údržba zariadenia'). Ak necháte prázdne, doplní sa automaticky.")
    
    bezplatne_stravovanie = models.BooleanField("🍽️ Zabezpečené bezplatné stravovanie", default=False, help_text="⚠️ Ak je zaškrtnuté (napr. zákazník zaplatil obed), účtovníctvo zamestnancovi pokráti denné diéty.")
    
    datum_cas_start = models.DateTimeField(default=timezone.now, verbose_name="🛫 Dátum a čas odjazdu", help_text="Presný dátum a čas štartu cesty. Dôležité pre výpočet trvania služobnej cesty a diét.")
    datum_cas_koniec = models.DateTimeField(null=True, blank=True, verbose_name="🛬 Dátum a čas príchodu", help_text="Presný dátum a čas ukončenia cesty pre generovanie knihy jázd a overenie prekrývania.")
    
    stav_nadrze_start = models.DecimalField("💧 Stav nádrže pri odjazde (Litre)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="📊 Ideálne pre krížovú kontrolu priemernej spotreby voči tankovaniam v ERP.")
    
    stav_km_start = models.PositiveIntegerField(null=True, blank=True, verbose_name="⏱️ Tachometer pri odjazde", help_text="Ak necháte prázdne, doplní sa automaticky z poslednej jazdy vozidla.")
    stav_km_koniec = models.PositiveIntegerField(null=True, blank=True, verbose_name="🛑 Tachometer v cieli", help_text="Konečný stav počítadla kilometrov. Rozdiel voči štartu vypočíta celkovú dĺžku trasy.")

    poznamka = models.TextField(blank=True, null=True, verbose_name="📝 Poznámka k jazde", help_text="Interné poznámky (napr. trasa obchádzkou, dopravná zápcha, poplatky za diaľnicu).")
    vyfakturovane = models.BooleanField(default=False, verbose_name="💰 Vyfakturované zákazníkovi", help_text="Priznak, či bola doprava/kilometrovné už zúčtovaná zákazníkovi na faktúre.")

    class Meta:
        verbose_name = "🛣️ Záznam z jazdy"
        verbose_name_plural = "🛣️ Evidencia jázd"
        ordering = ['-datum_cas_start']
        constraints = [
            # 🛑 DB ZÁMOK: Jedno auto nemôže byť na dvoch jazdách súčasne (len uzavreté jazdy)
            ExclusionConstraint(
                name='prevent_overlapping_jazdy_vozidlo',
                expressions=[
                    ('vozidlo', RangeOperators.EQUAL),
                    (Func('datum_cas_start', 'datum_cas_koniec', function='tstzrange'), RangeOperators.OVERLAPS),
                ],
                condition=Q(datum_cas_koniec__isnull=False),
            ),
            # 🛑 DB ZÁMOK: Jeden zamestnanec nemôže šoférovať dve autá súčasne (len uzavreté jazdy)
            ExclusionConstraint(
                name='prevent_overlapping_jazdy_zamestnanec',
                expressions=[
                    ('zamestnanec', RangeOperators.EQUAL),
                    (Func('datum_cas_start', 'datum_cas_koniec', function='tstzrange'), RangeOperators.OVERLAPS),
                ],
                condition=Q(zamestnanec__isnull=False, datum_cas_koniec__isnull=False),
            )
        ]

    def __str__(self):
        vodic = self.zamestnanec if self.zamestnanec else self.sukromna_osoba
        return f"{self.vozidlo.spz} | {vodic} ({self.datum_cas_start.strftime('%d.%m.%Y')})"

    @property
    def prejdene_km(self):
        if self.stav_km_koniec is not None and self.stav_km_start is not None:
            return max(0, self.stav_km_koniec - self.stav_km_start)
        return 0

    # 🧠 ISAS ENGINE: Tvrdá validácia dát proti chybám účtovníctva
    def clean(self):
        super().clean()

        # 🚨 ISAS LOGIKA: Skontrolujeme, či je vyplnený aspoň JEDEN začiatok (z 5 možností)
        start_moznosti = [self.miesto_startu, self.zakaznik_start, self.dodavatel_start, self.filialka_start, self.zamestnanec_start, self.sukromna_osoba_start]
        if not any(start_moznosti):
            raise ValidationError({
                "miesto_startu": "⚠️ Vyplňte manuálne textové pole, alebo vyberte jednu z entít nižšie!",
            })

        # 🚨 ISAS LOGIKA: Skontrolujeme, či je vyplnený aspoň JEDEN cieľ (z 5 možností)
        ciel_moznosti = [self.miesto_ciela, self.zakaznik, self.dodavatel_ciel, self.filialka_ciel, self.zamestnanec_ciel, self.sukromna_osoba_ciel]
        if not any(ciel_moznosti):
            raise ValidationError({
                "miesto_ciela": "⚠️ Vyplňte manuálne textové pole, alebo vyberte jednu z entít nižšie!",
            })
        
        start_km = self.stav_km_start
        if start_km is None and self.vozidlo_id:
            start_km = self.vozidlo.aktualny_stav_km

        if self.stav_km_koniec is not None and start_km is not None:
            if self.stav_km_koniec < start_km:
                raise ValidationError({"stav_km_koniec": "❌ Konečný stav tachometra nemôže byť nižší ako počiatočný!"})
                
        if self.datum_cas_start:
            if not getattr(settings, 'USE_TZ', False):
                if timezone.is_aware(self.datum_cas_start):
                    self.datum_cas_start = timezone.localtime(self.datum_cas_start).replace(tzinfo=None)
                if self.datum_cas_koniec and timezone.is_aware(self.datum_cas_koniec):
                    self.datum_cas_koniec = timezone.localtime(self.datum_cas_koniec).replace(tzinfo=None)
            else:
                if timezone.is_naive(self.datum_cas_start):
                    self.datum_cas_start = timezone.make_aware(self.datum_cas_start)
                if self.datum_cas_koniec and timezone.is_naive(self.datum_cas_koniec):
                    self.datum_cas_koniec = timezone.make_aware(self.datum_cas_koniec)
            if self.datum_cas_koniec and self.datum_cas_koniec <= self.datum_cas_start:
                raise ValidationError({"datum_cas_koniec": "❌ Čas príchodu musí nastať logicky až po čase odjazdu!"})
                
            # 🛡️ TEMPORAL OVERLAP CHECK: Ochrana proti prekrývaniu pre uzavreté aj otvorené jazdy
            if self.vozidlo_id:
                v_query = Q(vozidlo_id=self.vozidlo_id)
                if self.datum_cas_koniec:
                    v_query &= (Q(datum_cas_start__lt=self.datum_cas_koniec) & (Q(datum_cas_koniec__gt=self.datum_cas_start) | Q(datum_cas_koniec__isnull=True, datum_cas_start__lte=self.datum_cas_koniec)))
                else:
                    v_query &= (Q(datum_cas_koniec__gt=self.datum_cas_start) | Q(datum_cas_koniec__isnull=True))

                overlaps = EvidenciaJazd.objects.filter(v_query)
                if self.pk:
                    overlaps = overlaps.exclude(pk=self.pk)
                
                if overlaps.exists():
                    raise ValidationError({"datum_cas_start": "⚠️ KRITICKÁ CHYBA: Pre toto vozidlo už v tomto čase existuje iná (alebo neuzavretá) jazda!"})

            if self.zamestnanec_id:
                z_query = Q(zamestnanec_id=self.zamestnanec_id)
                if self.datum_cas_koniec:
                    z_query &= (Q(datum_cas_start__lt=self.datum_cas_koniec) & (Q(datum_cas_koniec__gt=self.datum_cas_start) | Q(datum_cas_koniec__isnull=True, datum_cas_start__lte=self.datum_cas_koniec)))
                else:
                    z_query &= (Q(datum_cas_koniec__gt=self.datum_cas_start) | Q(datum_cas_koniec__isnull=True))

                overlaps_driver = EvidenciaJazd.objects.filter(z_query)
                if self.pk:
                    overlaps_driver = overlaps_driver.exclude(pk=self.pk)
            
                if overlaps_driver.exists():
                    raise ValidationError({"zamestnanec": "⚠️ Tento zamestnanec už v tomto čase šoféruje iné vozidlo!"})

    def save(self, *args, **kwargs):
        # Dynamické doplnenie štátu podľa sídla firmy s multi-tenant izoláciou
        if not self.stat_startu or not self.stat_ciela:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentitaModel.objects.first()
            predvoleny_stat = getattr(identita, 'sidlo_stat', '') or ''
            if not self.stat_startu and predvoleny_stat:
                self.stat_startu = predvoleny_stat
            if not self.stat_ciela and predvoleny_stat:
                self.stat_ciela = predvoleny_stat

        # 1. Legislatívna úprava účelu
        if not self.ucel_cesty:
            if self.typ_jazdy == 'SUKROMNA':
                self.ucel_cesty = "Súkromná jazda (Neuplatňuje sa do nákladov)"
            elif self.zakaznik:
                self.ucel_cesty = f"Servisný výjazd - {self.zakaznik.Nazov_spolocnosti}"

        # 2. Uloženie záznamu a synchro tachometra v JEDNEJ transakcii so zamknutým vozidlom
        with transaction.atomic():
            if self.vozidlo_id:
                vozidlo_locked = Vozidlo.objects.select_for_update().get(pk=self.vozidlo_id)
                if self.stav_km_start is None:
                    self.stav_km_start = vozidlo_locked.aktualny_stav_km
                if self.stav_km_koniec is not None and self.stav_km_koniec > vozidlo_locked.aktualny_stav_km:
                    vozidlo_locked.aktualny_stav_km = self.stav_km_koniec
                    vozidlo_locked.save(update_fields=['aktualny_stav_km'])

            # 🛡️ DEFENSIVE: Ak ukladáme cez update_fields, musíme zabezpečiť uloženie odvodených polí
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'stav_km_start', 'ucel_cesty', 'stat_startu', 'stat_ciela'}

            super().save(*args, **kwargs)

class MegaZaloha(ISASAuditMixin, models.Model):
    TYPY_ZALOH = [
        ('vystavene_faktury', '📄 Vystavené faktúry (PDF ZIP & Excel sumár)'),
        ('prichodzie_faktury', '📥 Prijaté faktúry dodávateľov (PDF ZIP)'),
        ('cenove_ponuky', '💰 Cenové ponuky (PDF ZIP)'),
        ('evidencia_uhrad', '🧮 Evidencia úhrad / Banka (Excel/CSV)'),
        
        ('servisne_protokoly', '📑 Servisné protokoly zásahov (PDF ZIP)'),
        ('ulohy_a_vykazy', '📋 Výkazy práce technikov (Excel)'),
        
        ('kniha_jazd', '🗺️ Kniha jázd - Evidencia (Excel report)'),
        ('tankovania', '⛽ Tankovania a bločky (PDF ZIP & Excel sumár)'),
        
        ('cenovy_katalog', '💰 Cenový katalóg / Skladové karty (Excel pre precenenie)'),
        ('skladove_pohyby', '🔄 Skladové pohyby - Príjemky/Výdajky (Excel audit)'),
        ('odoslane_objednavky', '📤 Odoslané objednávky dodávateľom (PDF ZIP)'),
        ('prijate_objednavky', '📥 Prijaté objednávky od zákazníkov (PDF ZIP)'),

        ('hr_dokumenty', '📁 HR Dokumenty (Zmluvy, BOZP, Osvedčenia - PDF ZIP)'),
        ('certifikaty', '📜 Odborné certifikáty a osvedčenia (PDF ZIP)'),
        ('povolenia_vstupov', '🚧 Povolenia na vstup (Vygenerované PDF pre SBS)'),
        ('podklady_mzdy', '📊 Podklady pre mzdy a fakturáciu (Inteligentný Excel)'),

        ('technicke_specifikacie', '📖 Technické špecifikácie (Manuály, Schémy, KBU - ZIP)'),
        ('qr_kody_zariadeni', '🔳 QR Kódy zariadení (Obrázky pre tlač - ZIP)'),
        
        ('kompletny_dump', '🗄️ Kompletná záloha databázy (SQL/JSON)'),
        ('audit_logy', '🛡️ Audit Logy (Archív JSON.GZ)'),
        ('mimosystemova', '🗂️ Mimosystémová záloha (Vlastný nahratý súbor)'),
    ]

    STAVY_SPRACOVANIA = [
        ('vyziadana', '⏳ V rade (Čaká na spracovanie na pozadí)'),
        ('spracovava_sa', '⚙️ Generuje sa (Získavanie dát z databázy)'),
        ('hotova', '✅ Hotová a pripravená na stiahnutie'),
        ('chyba', '❌ Zlyhala (Kritická chyba systému)'),
    ]

    VOLBY_UCHOVANIA = [
        ('24_hodin', '⏱️ 24 hodín (Na rýchle zdieľanie)'),
        ('7_dni', '🗑️ 7 dní (Bežný týždenný export)'),
        ('30_dni', '🗑️ 30 dní (Mesačná uzávierka)'),
        ('90_dni', '🗑️ 90 dní (Kvartálny audit)'),
        ('365_dni', '📅 1 rok (Ročná uzávierka)'),
        ('navzdy', '♾️ Ponechať navždy (Trvalý trezor)'),
    ]

    nazov_zalohy = models.CharField(
        max_length=255, 
        verbose_name="🏷️ Názov exportu",
        help_text="🏷️ Interné pomenovanie exportu pre rýchlu orientáciu a dohľadanie v histórii."
    )
    typ_zalohy = models.CharField(
        max_length=30, 
        choices=TYPY_ZALOH, 
        default='prichodzie_faktury', 
        verbose_name="⚙️ Kategória dát",
        help_text="📊 Kategória vyexportovaných dát určená pre účtovníctvo, revízie, servis alebo archív."
    )
    stav_spracovania = models.CharField(
        max_length=20, 
        choices=STAVY_SPRACOVANIA, 
        default='vyziadana', 
        verbose_name="🚦 Stav",
        help_text="🚦 Priebeh spracovania požiadavky na pozadí od zaevidovania až po dokončenie."
    )
    
    politika_uchovania = models.CharField(
        max_length=20, choices=VOLBY_UCHOVANIA, default='30_dni',
        verbose_name="⏱️ Pravidlo uchovania",
        help_text="⏳ Pravidlo automatického zmazania súboru z úložiska pre optimalizáciu serverových nákladov."
    )
    
    datum_vytvorenia = models.DateTimeField(
        auto_now_add=True, 
        verbose_name="📅 Dátum vytvorenia",
        help_text="📅 Dátum a čas zaevidovania požiadavky na vygenerovanie exportu."
    )
    datum_exspiracie = models.DateTimeField(
        null=True, 
        blank=True, 
        verbose_name="⏰ Dátum expirácie súboru",
        help_text="⏰ Vypočítaný termín automatického vymazania súboru podľa zvolenej politiky uchovania."
    )
    
    obsahuje_data_od = models.DateField(
        null=True, 
        blank=True, 
        verbose_name="📅 Filtrovať dáta OD", 
        help_text="📅 Začiatok sledovaného obdobia pre presné ohraničenie exportovaných dokladov a záznamov."
    )
    obsahuje_data_do = models.DateField(
        null=True, 
        blank=True, 
        verbose_name="📅 Filtrovať dáta DO", 
        help_text="📅 Koniec sledovaného obdobia pre vygenerovanie podkladov k uzávierkam a auditom."
    )

    subor_zalohy = models.FileField(
        upload_to='systemove_exporty/%Y/%m/', 
        null=True, 
        blank=True, 
        verbose_name="📁 Zazipovaný súbor / Excel",
        help_text="📁 Vygenerovaný archív na stiahnutie alebo ručne priložený externý súbor zálohy."
    )
    velkost_v_mb = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        null=True, 
        blank=True, 
        verbose_name="💾 Veľkosť súboru (MB)",
        help_text="💾 Veľkosť vygenerovaného súboru v MB slúžiaca na kontrolu spotreby diskovej kapacity."
    )
    
    poznamka = models.TextField(
        blank=True, 
        verbose_name="📝 Detailná poznámka k exportu",
        help_text="📝 Doplňujúci popis zálohy alebo automatický systémový protokol o jej spracovaní."
    )

    vyziadal_uzivatel = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, limit_choices_to={'is_staff': True},
        verbose_name="👤 Vlastník / Spustil export",
        help_text="👤 Zamestnanec alebo administrátor, ktorý požiadal o vygenerovanie exportu."
    )

    class Meta:
        verbose_name = "💾 Systémový export a Záloha"
        verbose_name_plural = "💾 Systémové exporty a Zálohy"
        ordering = ['-datum_vytvorenia']
        indexes = [
            models.Index(fields=['typ_zalohy', 'stav_spracovania']),
            models.Index(fields=['datum_exspiracie']), # Kritické pre rýchly Cron cleanup
        ]

    def __str__(self):
        return f"{self.get_typ_zalohy_display()} - {self.nazov_zalohy} [{self.get_stav_spracovania_display()}]"

    def save(self, *args, **kwargs):
        # 1. Výpočet exspirácie pred uložením
        if self.politika_uchovania == '24_hodin': self.datum_exspiracie = timezone.now() + timedelta(days=1)
        elif self.politika_uchovania == '7_dni': self.datum_exspiracie = timezone.now() + timedelta(days=7)
        elif self.politika_uchovania == '30_dni': self.datum_exspiracie = timezone.now() + timedelta(days=30)
        elif self.politika_uchovania == '90_dni': self.datum_exspiracie = timezone.now() + timedelta(days=90)
        elif self.politika_uchovania == '365_dni': self.datum_exspiracie = timezone.now() + timedelta(days=365)
        elif self.politika_uchovania == 'navzdy': self.datum_exspiracie = None

        # 2. Výpočet MB z existujúceho súboru
        if self.subor_zalohy and not self.velkost_v_mb:
            try: self.velkost_v_mb = round(self.subor_zalohy.size / (1024 * 1024), 2)
            except Exception: self.velkost_v_mb = None

        # 3. Detekcia, či máme spustiť workera
        spustit_worker = False
        if self.stav_spracovania == 'vyziadana':
            # 🛡️ VYLEPŠENIE: Ak používateľ manuálne nahral súbor, preskočíme generovanie!
            if self.subor_zalohy:
                self.stav_spracovania = 'hotova'
                if "manuálne" not in (self.poznamka or ""):
                    self.poznamka = (self.poznamka or "") + f"\n✅ [{timezone.now().strftime('%d.%m.%Y %H:%M')}] Súbor bol manuálne nahratý používateľom."
            else:
                # Súbor tam nie je, systém ho musí vygenerovať
                spustit_worker = True

        super().save(*args, **kwargs)

        # 4. Asynchrónny generátor exportov (Spustí sa len ak nebol nahratý manuálny súbor)
        if spustit_worker:
            self.stav_spracovania = 'spracovava_sa'
            MegaZaloha.objects.filter(pk=self.pk).update(stav_spracovania='spracovava_sa')

            from Evidencia.tasks import spustit_hromadny_export_task
            def _safe_dispatch_export(doc_pk):
                try:
                    spustit_hromadny_export_task.delay(doc_pk)
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre generovanie zálohy {doc_pk}: {exc}")
                    MegaZaloha.objects.filter(pk=doc_pk).update(
                        stav_spracovania='chyba',
                        poznamka=f"⚠️ Celery/Redis broker je nedostupný: {exc}"
                    )

            transaction.on_commit(lambda pk=self.pk: _safe_dispatch_export(pk))

# 🧹 DEVOPS OCHRANA DOCKERA: Tvrdé fyzické mazanie súborov
@receiver(post_delete, sender=MegaZaloha)
def auto_delete_megazaloha_file_on_delete(sender, instance, **kwargs):
    """
    🚨 ISAS DEVOPS: Keď sa v databáze zmaže záznam (napríklad cez Cron expiráciu),
    tento signál zabezpečí, že sa fyzický súbor zmaže aj z disku Docker kontajnera.
    Zabraňuje to tichému zapĺňaniu miesta osirelými ZIP/Excel súbormi.
    """
    if instance.subor_zalohy:
        try:
            # 🛡️ ELITNÁ OCHRANA: Používame natívnu Django metódu namiesto 'os.remove'.
            # Je to bezpečnejšie voči OS zámkom a nevybuchne to, ak v budúcnosti 
            # prejdete z Docker disku na cloudové úložisko (AWS S3 / MinIO).
            instance.subor_zalohy.delete(save=False)
        except Exception as e:
            logging.getLogger(__name__).warning(f"⚠️ [ISAS DEVOPS] Nepodarilo sa zmazať fyzický súbor pre zálohu ID {instance.pk}: {e}")

class ServisnyPredpis(ISASAuditMixin, models.Model):
    zariadenie = models.ForeignKey(
        "ServisovaneZariadenie", 
        on_delete=models.CASCADE, 
        related_name="predpisy",
        verbose_name="🔧 Servisované zariadenie",
        help_text="⚙️ Servisované zariadenie, pre ktoré sa definuje tento servisný predpis a plán údržby."
    )
    typ_prace = models.ForeignKey(
        "TypServisuPrace", 
        on_delete=models.CASCADE, 
        verbose_name="🛠️ Typ servisu/práce",
        help_text="🛠️ Konkrétny typ servisného úkonu alebo práce, ktorý sa má na danom zariadení vykonávať."
    )

    class Meta:
        verbose_name = "📑 Servisný predpis"
        verbose_name_plural = "📑 Servisné predpisy"
        unique_together = ('zariadenie', 'typ_prace')

    def __str__(self):
        return f"{self.zariadenie} – {self.typ_prace}"

class PolozkaPredpisu(ISASAuditMixin, models.Model):
    predpis = models.ForeignKey(
        ServisnyPredpis, 
        on_delete=models.CASCADE, 
        related_name="polozky", 
        verbose_name="📄 Servisný predpis",
        help_text="📋 Nadradený servisný predpis, ku ktorému sa táto náhradná položka alebo materiál vzťahuje."
    )
    mnozstvo = models.PositiveIntegerField(
        verbose_name="🔢 Množstvo", 
        default=1,
        help_text="📊 Počet kusov alebo jednotiek materiálu potrebných na realizáciu servisného úkonu."
    )
    
    # Generic relation pre prepojenie na akýkoľvek skladový model
    content_type = models.ForeignKey(
        ContentType, 
        on_delete=models.CASCADE, 
        verbose_name="🗂️ Typ dielu",
        help_text="📦 Systémový typ skladovej položky pre korektné spárovanie dielu."
    )
    object_id = models.PositiveIntegerField(
        verbose_name="🆔 ID položky",
        help_text="🔢 Unikátny identifikátor konkrétneho dielu alebo materiálu zo skladu."
    )
    diel = GenericForeignKey('content_type', 'object_id')

    class Meta:
        verbose_name = "📌 Položka predpisu"
        verbose_name_plural = "📌 Položky predpisov"
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        try:
            diel_obj = self.diel if (self.content_type_id and self.content_type and self.content_type.model_class()) else None
            diel_str = f"{diel_obj}" if diel_obj else f"Položka #{self.object_id}"
        except (ObjectDoesNotExist, AttributeError):
            diel_str = f"Zmazaná položka #{self.object_id}"
        return f"{self.mnozstvo}x {diel_str}"

class ServisnyZaznam(ISASAuditMixin, models.Model):
    vytvorene = models.DateTimeField(auto_now_add=True, verbose_name="📅 Vytvorené", help_text="Automatický časový odtlačok vytvorenia servisného záznamu.")
    upravene = models.DateTimeField(auto_now=True, verbose_name="🔄 Naposledy upravené", help_text="Automatický časový odtlačok poslednej úpravy údajov v zázname.")
    
    zakaznik = models.ForeignKey('Zakaznik', on_delete=models.CASCADE, null=True, blank=True, verbose_name="🏢 Zákazník", help_text="Odberateľ, u ktorého bol servis vykonaný. Určuje fakturačné a kontaktné údaje.")
    zariadenie = models.ForeignKey('ServisovaneZariadenie', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="⚙️ Servisované zariadenie", help_text="Konkrétny stroj, na ktorom sa práca vykonala. Dôležité pre sledovanie histórie a výpočet motohodín.")
    technici: models.ManyToManyField = models.ManyToManyField('Zamestnanec', verbose_name="👨‍🔧 Priradení technici", help_text="Výjazdoví technici, ktorí reálne vykonali servisný zásah.")
    typ_servisu: models.ManyToManyField = models.ManyToManyField('TypServisuPrace', blank=True, verbose_name="🛠️ Typ servisu/práce", help_text="Kategórie vykonaných prác (napr. Údržba, Oprava) potrebné pre automatické plánovanie budúceho servisu.")

    stav_motohodin = models.IntegerField(
        "⏱️ Stav motohodín / Počítadlo", 
        null=True, blank=True, 
        help_text="Aktuálny celkový stav počítadla zariadenia pri servise. Slúži na výpočet plánovaných intervalov údržby."
    )
    motohodiny_v_zatazi = models.IntegerField(
        "🏋️ Motohodiny v záťaži / Záťažové počítadlo", 
        null=True, blank=True, 
        help_text="Čas alebo cykly, kedy zariadenie reálne pracovalo pod plnou záťažou. Kľúčový údaj pre diagnostiku a efektivitu."
    )

    # --- 📖 DYNAMICKÉ ODVETVOVÉ PROPERTIES (INDUSTRY LEXICON) ---
    @property
    def meter_label(self):
        from .industry_loader import get_industry_term
        return get_industry_term('meter_label', 'Motohodiny')

    @property
    def meter_unit(self):
        from .industry_loader import get_industry_term
        return get_industry_term('meter_unit', 'hod.')

    @property
    def meter_load_label(self):
        from .industry_loader import get_industry_term
        return get_industry_term('meter_load_label', 'Motohodiny v záťaži')

    @property
    @admin.display(description="Stav počítadla")
    def meter_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'stav_motohodin', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    @property
    @admin.display(description="Stav v záťaži")
    def meter_load_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'motohodiny_v_zatazi', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_load_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    namerane_hodnoty = models.TextField(
        "📊 Namerané hodnoty (Stav PO servise)", 
        blank=True, 
        help_text="Prevádzkové parametre po servise (fyzikálne a telemetrické veličiny podľa typu zariadenia). Určené pre diagnostiku a IoT synchronizáciu."
    )
    zmena_hardveru = models.BooleanField(
        "⚠️ Došlo k zmene parametrov zariadenia?", 
        default=False, 
        help_text="Zaškrtnite, ak pri servise došlo k výmene kľúčových komponentov za iný typ alebo výkonnostnú triedu. Vyžaduje úpravu v karte zariadenia."
    )
    # --- ⚠️ VÝSLEDOK REKLAMÁCIE (Vypĺňa sa len ak išlo o reklamačný výjazd) ---
    KORENE_OPRAVNENE = {'CHYBA_TECHNIKA', 'VADNY_DIEL', 'SOFTWARE'}
    KORENE_NEOPRAVNENE = {'CHYBA_OBSLUHY', 'ZANEDBANA_UDRZBA', 'EXTERNY_VPLYV', 'OPOTREBENIE'}

    KOREN_PROBLEMU_CHOICES = [
        ('NEHODNOTENE', '➖ Nehodnotené (Bežný servis)'),
        
        # --- OPRÁVNENÉ (Naše náklady) ---
        ('CHYBA_TECHNIKA', '👨‍🔧 Chyba montáže / postupu (Oprávnená)'),
        ('VADNY_DIEL', '⚙️ Skrytá vada dielu z výroby (Oprávnená)'),
        ('SOFTWARE', '💻 Softvérová chyba / Firmvér (Oprávnená)'),
        
        # --- NEOPRÁVNENÉ (Fakturuje sa zákazníkovi) ---
        ('CHYBA_OBSLUHY', '🤦‍♂️ Nesprávna obsluha zákazníkom (Neoprávnená)'),
        ('ZANEDBANA_UDRZBA', '🧹 Zanedbaná denná údržba zákazníkom (Neoprávnená)'),
        ('EXTERNY_VPLYV', '⚡ Externý vplyv - prepätie, počasie, prach (Neoprávnená)'),
        ('OPOTREBENIE', '⏳ Prirodzené opotrebenie nesúvisiace so servisom (Neoprávnená)'),
    ]
    koren_problemu = models.CharField(
        "🔍 Koreň problému (Pri reklamácii)", max_length=20, choices=KOREN_PROBLEMU_CHOICES, default='NEHODNOTENE',
        help_text="Určenie príčiny poruchy pri garančnom zásahu (napr. chyba montáže vs. zanedbaná údržba). Kľúčové pre vyhodnotenie ISO 9001 a nákladov."
    )

    @property
    def je_reklamacia_opravnena(self):
        """🏛️ SINGLE SOURCE OF TRUTH: Vyhodnotí, či ide o uznanú záručnú vadu."""
        return self.koren_problemu in self.KORENE_OPRAVNENE

    @property
    def verejny_koren_problemu_display(self):
        """🛡️ ZERO CONFRONTATION B2B: Formálna a neutrálna prezentácia príčiny pre klienta bez interných emotikonov."""
        mapovanie = {
            'CHYBA_TECHNIKA': 'Garančné plnenie (Uznaná reklamácia – servisný postup)',
            'VADNY_DIEL': 'Garančné plnenie (Uznaná reklamácia – materiálová vada dielu)',
            'SOFTWARE': 'Garančné plnenie (Uznaná reklamácia – softvérová aktualizácia)',
            'CHYBA_OBSLUHY': 'Mimozáručný servis (Nesúlad s prevádzkovým predpisom)',
            'ZANEDBANA_UDRZBA': 'Mimozáručný servis (Nevykonaná predpísaná údržba)',
            'EXTERNY_VPLYV': 'Mimozáručný servis (Vonkajšie vplyvy prostredia a napájania)',
            'OPOTREBENIE': 'Mimozáručný servis (Bežné prevádzkové opotrebenie)',
        }
        return mapovanie.get(self.koren_problemu, self.get_koren_problemu_display())

    poruchy = models.TextField(blank=True, verbose_name="📝 Poruchy / poznámky technika", help_text="Detailný popis zistených závad, vykonaných úkonov a odporúčaní pre zákazníka z terénu.")
    foto_pred = models.ImageField(upload_to="servisne_zaznamy/foto_pred/", blank=True, null=True, verbose_name="📸 Foto pred servisom", help_text="Fotodokumentácia neukončeného/poškodeného stavu pred začatím prác.")
    foto_po = models.ImageField(upload_to="servisne_zaznamy/foto_po/", blank=True, null=True, verbose_name="📸 Foto po servise", help_text="Fotodokumentácia ukončeného servisu alebo vymeneného dielu.")
    video = models.FileField(upload_to="servisne_zaznamy/video/", blank=True, null=True, verbose_name="🎥 Video záznam (voliteľné)", help_text="Videozáznam zachytávajúci neštandardný chod zariadenia, akustické prejavy či poruchové stavy.")
    
    # --- 🌐 WEB BUILDER INTEGRÁCIA ---
    zobrazit_na_webe = models.BooleanField(
        default=False, 
        verbose_name="🌐 Zobraziť ako referenciu na webe", 
        help_text="🌐 Ak je zaškrtnuté a záznam má fotky, zobrazí sa na verejnom webe ako ukážka vašej práce."
    )
    verejny_popis_referencie = models.CharField(
        max_length=255, blank=True, null=True, 
        verbose_name="📝 Verejný popis referencie", 
        help_text="📝 Krátky marketingový popis pre web (napr. 'Generálna oprava a údržba zariadenia')."
    )
    slug = models.SlugField(
        max_length=255, unique=True, blank=True, null=True, 
        verbose_name="🔗 URL Slug",
        help_text="🔗 Unikátny URL identifikátor pre referencie (generuje sa automaticky)."
    )
    dokoncene = models.BooleanField(default=True, verbose_name="✅ Dokončené", help_text="Priznak, či je servisný zásah fyzicky kompletne dokončený.")
    bezpecnostny_kluc = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, null=True, blank=True, verbose_name="🔑 Bezpečnostný kľúč", help_text="Unikátny kryptografický kľúč (UUID) slúžiaci na vygenerovanie bezpečnej verejnej URL adresy pre podpis zákazníka.")
    formalne_zhodnotenie_ai = models.TextField(blank=True, null=True, verbose_name="🤖 AI Odborné zhodnotenie (na protokol)", help_text="Gramaticky a odborne vybrúsený text pre zákazníka vygenerovaný z poznámok technika pomocou AI.")
    upozornit_majitela_protokol_tg = models.BooleanField("✈️ Telegram: Upozorniť vedenie na podpis", default=True, help_text="Automaticky odosle správu s PDF protokolom do Telegram kanála vedenia ihneď po podpísaní zákazníkom.")
    upozornit_majitela_protokol_mail = models.BooleanField("📧 E-mail: Upozorniť vedenie na podpis", default=True, help_text="Odošle e-mailovú notifikáciu s priloženým PDF protokolom na adresu vedenia ihneď po podpísaní.")
    upozornit_majitela_protokol_discord = models.BooleanField("🎮 Discord: Upozorniť vedenie na podpis", default=True, help_text="💬 Automaticky odošle správu s PDF protokolom do Discord kanála vedenia ihneď po podpísaní zákazníkom.")

    bezpecnostny_hash = models.CharField(
        "🔒 SHA-256 Digitálna pečať", 
        max_length=64, 
        blank=True, 
        null=True, 
        help_text="Kryptografický odtlačok (SHA-256) preukazujúci právnu nezmenenosť dát v protokole od momentu podpisu."
    )

    ai_analyza_historie = models.TextField(blank=True, null=True, verbose_name="🧠 Skrytá AI analýza (TCO a Anomálie)", help_text="Interná AI analýza prediktívnej údržby, opotrebovania a celkových nákladov (TCO) viditeľná len pre vedenie.")
    gps_suradnice = models.CharField(max_length=150, blank=True, null=True, verbose_name="📍 GPS poloha podpisu", help_text="Zemepisné súradnice miesta, kde zákazník fyzicky podpísal protokol na tablete/fóne.")
    hodnotenie_zakaznika = models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="⭐ NPS Hodnotenie (1-5)", help_text="Hodnotenie spokojnosti zákazníka s vykonaným servisom (1 = najhoršie, 5 = najlepšie).")
    uzavrety_pdf_dokument = models.FileField(upload_to="servisne_protokoly_pdf/%Y/%m/", blank=True, null=True, verbose_name="📄 Fyzické PDF uzamknutého protokolu", help_text="Finálne, neupravovateľné PDF vygenerované automaticky po elektronickom podpísaní.")
    
    podpis_zakaznika = models.TextField(blank=True, null=True, verbose_name="✍️ Podpis zákazníka (Base64)", help_text="Vektorové alebo snímkové dáta podpisu zákazníka priradené k protokolu.")
    meno_podpisujuceho = models.CharField(max_length=150, blank=True, null=True, verbose_name="👤 Meno osoby, ktorá prevzala zariadenie", help_text="Meno a priezvisko zástupcu zákazníka, ktorý protokol prebral a podpísal.")
    podpis_technika = models.TextField(blank=True, null=True, verbose_name="✍️ Podpis technika (Base64)", help_text="Elektronický podpis zodpovedného technika.")
    
    protokol_uzavrety = models.BooleanField(default=False, verbose_name="🔐 Protokol elektronicky podpísaný a uzamknutý", help_text="Označuje, že protokol bol podpísaný. Zabraňuje dodatočným neoprávneným úpravám dát.")
    cas_podpisu = models.DateTimeField(blank=True, null=True, verbose_name="🕒 Presný čas podpísania", help_text="Presný dátum a čas, kedy prišlo k elektronickému potvrdeniu a podpisu zákazníkom.")

    uloha = models.ForeignKey('Uloha', on_delete=models.SET_NULL, null=True, blank=True, related_name="servisne_zaznamy", verbose_name="📋 Súvisiaca úloha s týmto servisným záznamom", help_text="Prepojenie protokolu s nadradenou plánovanou zákazkou pre potreby zbernej fakturácie.")

    class Meta:
        verbose_name = "📝 Servisný záznam"
        verbose_name_plural = "📋 Servisné záznamy"

    @classmethod
    def from_db(cls, db, field_names, values, fetch_mode=None):
        instance = super().from_db(db, field_names, values, fetch_mode=fetch_mode)
        instance._povodny_stav = dict(zip(field_names, values))
        return instance

    def __str__(self):
        if self.pk:
            zakaznik_str = str(self.zakaznik) if self.zakaznik else "Nešpecifikovaný zákazník"
            zariadenie_str = str(self.zariadenie) if self.zariadenie else "Neznáme zariadenie"
            datum_str = self.vytvorene.strftime('%d.%m.%Y') if self.vytvorene else "Neznámy dátum"
            stav_ikona = "🔐" if self.protokol_uzavrety else "🔓"
            
            # Načítanie ManyToMany polí (vyžiadané pre detailný prehľad)
            typy_str = ", ".join([str(t) for t in self.typ_servisu.all()]) or "Bez typu"
            technici_str = ", ".join([str(t) for t in self.technici.all()]) or "Bez technika"
            
            return f"{stav_ikona} Záznam #{self.pk} | 🏢 {zakaznik_str} | ⚙️ {zariadenie_str} | 🛠️ {typy_str} | 👨‍🔧 {technici_str} | 📅 {datum_str}"
        return "✨ Nový servisný záznam"

    def save(self, *args, **kwargs):
        # 🚀 ISAS ARCHITECT: WebP Kompresia pre Referencie (Pred a Po)
        try:            
            for field_name in ['foto_pred', 'foto_po']:
                foto = getattr(self, field_name)
                if foto and not foto.name.lower().endswith('.webp'):
                    img = Image.open(foto)
                    if img.mode == "P": img = img.convert("RGBA")
                    elif img.mode not in ("RGB", "RGBA"): img = img.convert("RGB")
                    img.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
                    
                    output = BytesIO()
                    img.save(output, format='WEBP', quality=85, method=6)
                    output.seek(0)
                    
                    nazov_bez_pripony = os.path.basename(foto.name).rsplit('.', 1)[0][:85]
                    foto.save(f"{nazov_bez_pripony}.webp", ContentFile(output.read()), save=False)
        except Exception:
            pass

        # 🤖 AUTOMATIKA: Generovanie unikátneho URL slugu pre Referencie
        if not self.slug and self.zobrazit_na_webe:
            base_slug = slugify(self.verejny_popis_referencie or f"referencia-{self.zariadenie.vyrobne_cislo if self.zariadenie else self.pk or 'nova'}")[:240]
            if not base_slug:
                base_slug = f"referencia-{self.pk or 'nova'}"
            slug = base_slug
            counter = 1
            while ServisnyZaznam.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug[:240]}-{counter}"
                counter += 1
            self.slug = slug

        with transaction.atomic():
            
            zariadenie_locked = None
            # 1. Najprv uzamkneme stroj (ak existuje). Týmto zablokujeme IoT aj iných technikov 
            # ešte PREDTÝM, než vôbec začneme ukladať tento záznam. Absolútna serializácia.
            if self.zariadenie_id:
                try:
                    # Používame priamo triedu modelu, čím sa vyhneme zbytočnému lazy-load dopytu
                    zariadenie_locked = ServisovaneZariadenie.objects.select_for_update().get(pk=self.zariadenie_id)
                except ObjectDoesNotExist:
                    zariadenie_locked = None

            # 🛡️ ANTI-TAMPERING: Ochrana integrity podpísaného protokolu
            povodny = getattr(self, '_povodny_stav', {})
            bol_uzavrety = povodny.get('protokol_uzavrety', False) if isinstance(povodny, dict) else False

            # Ak bol protokol už podpísaný a zostáva podpísaný, zakazujeme tichú zmenu kľúčových dát aj podpisov
            if bol_uzavrety and self.protokol_uzavrety:
                strazene_polia = [
                    'stav_motohodin', 'motohodiny_v_zatazi', 'namerane_hodnoty', 
                    'koren_problemu', 'poruchy', 'bezpecnostny_hash', 
                    'podpis_zakaznika', 'podpis_technika', 'meno_podpisujuceho'
                ]
                for f in strazene_polia:
                    if f in povodny and getattr(self, f) != povodny[f]:
                        raise ValidationError(f"🔒 Záznam má právne uzamknutý protokol. Pole '{f}' nie je možné upravovať bez predchádzajúceho resetovania protokolu konateľom.")

            # 2. Až TERAZ bezpečne uložíme samotný servisný záznam
            super().save(*args, **kwargs)

            # 🎯 DETEKCIA ZMENY V PREDIKTÍVNYCH POLIACH (Motohodiny, Stroj, Dokončenie)
            zmena_predikcie = False
            stary_dokoncene = False

            # Kontrolujeme výhradne existujúce záznamy s históriou z DB
            if isinstance(povodny, dict) and povodny:
                stary_dokoncene = povodny.get('dokoncene', False)
                for pole in ['stav_motohodin', 'motohodiny_v_zatazi', 'zariadenie_id', 'dokoncene']:
                    if pole in povodny and getattr(self, pole) != povodny[pole]:
                        zmena_predikcie = True
                        break

            # Reakcia na reálnu zmenu v prediktívnych dátach existujúceho záznamu:
            if zmena_predikcie and self.pk:
                PlanovanyServisZariadenia = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')
                if not self.dokoncene and stary_dokoncene:
                    # Ak bol servis zrušený / odznačený z dokončeného, zmažeme neodoslané plány
                    PlanovanyServisZariadenia.objects.filter(posledny_zaznam=self, upozornenie_odoslane=False).delete()
                elif self.dokoncene:
                    # Ak servis zostáva dokončený a zmenili sa motohodiny alebo stroj, prepočítame plány
                    self.vygeneruj_prediktivne_plany()

            # 3. 🧠 ISAS NEUROLINK: Automatická aktualizácia stroja
            if zariadenie_locked:
                najnovsi_zaznam = type(self).objects.filter(zariadenie=zariadenie_locked).order_by('-vytvorene', '-id').first()
                je_najnovsi = (najnovsi_zaznam is None) or (najnovsi_zaznam.pk == self.pk)

                if je_najnovsi:
                    zmena_zariadenia = False
                    polia_na_ulozenie = ['posledna_aktualizacia_stavu']

                    # 🛡️ OCHRANA PROTI IOT DOWNGRADE: Motohodiny môžu iba STÚPAŤ! (Používame > namiesto !=)
                    aktualne_mh = zariadenie_locked.aktualne_motohodiny or 0
                    if self.stav_motohodin is not None and self.stav_motohodin > aktualne_mh:
                        zariadenie_locked.aktualne_motohodiny = self.stav_motohodin
                        polia_na_ulozenie.append('aktualne_motohodiny')
                        zmena_zariadenia = True

                    # To isté pre motohodiny v záťaži
                    aktualne_zataz = getattr(zariadenie_locked, 'motohodiny_v_zatazi', 0) or 0
                    if self.motohodiny_v_zatazi is not None and self.motohodiny_v_zatazi > aktualne_zataz:
                        zariadenie_locked.motohodiny_v_zatazi = self.motohodiny_v_zatazi
                        polia_na_ulozenie.append('motohodiny_v_zatazi')
                        zmena_zariadenia = True

                    # Ak nastala zmena, uložíme len zmenené polia
                    if zmena_zariadenia:
                        zariadenie_locked.posledna_aktualizacia_stavu = timezone.now()
                        zariadenie_locked.save(update_fields=polia_na_ulozenie)

            # 🚀 ISAS WEBHOOK TRIGGER (Odpálenie do Celery)
            # Spustí sa IBA ak bol servis práve dokončený a má priradeného zákazníka
            bol_dokonceny_teraz = False
            if self.dokoncene:
                povodny_stav = getattr(self, '_povodny_stav', {})
                stary_dokoncene = povodny_stav.get('dokoncene', False) if isinstance(povodny_stav, dict) else False
                if not stary_dokoncene:
                    bol_dokonceny_teraz = True

            if bol_dokonceny_teraz and self.zakaznik_id:
                from WebBuilder.tasks import process_servis_webhooks_task
                def _safe_servis_webhook(doc_pk):
                    try:
                        process_servis_webhooks_task.delay(doc_pk)
                    except Exception as exc:
                        logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre servisný webhook záznamu {doc_pk}: {exc}")

                transaction.on_commit(lambda pk=self.pk: _safe_servis_webhook(pk))

            # 🛡️ IDEMPOTENCIA: Synchronizácia in-memory stavu pre prípad opakovaného save() v rovnakom procese
            if not hasattr(self, '_povodny_stav') or not isinstance(self._povodny_stav, dict):
                self._povodny_stav = {}
            self._povodny_stav['dokoncene'] = self.dokoncene

    # Vytiahne historické dáta zariadenia pre RAG kontext
    def get_historia_stroja(self):
        if not self.zariadenie_id:
            return "❌ Zariadenie nie je priradené, história nie je k dispozícii."
        
        historia_qs = ServisnyZaznam.objects.filter(
            zariadenie_id=self.zariadenie_id, 
            dokoncene=True
        )
        if self.pk:
            historia_qs = historia_qs.exclude(pk=self.pk)
        historia_qs = historia_qs.order_by('-vytvorene')[:5]
        
        historia = list(historia_qs) # Evaluácia do RAM (1 DB dopyt)
        
        if not historia: # Kontrola prázdneho listu v RAM (0 DB dopytov)
            return "🆕 Toto je prvý evidovaný servis tohto zariadenia. Nemáme historické dáta."
            
        from .industry_loader import get_industry_term
        m_unit = get_industry_term('meter_unit', 'hod.')
        m_load_unit = get_industry_term('meter_load_unit', 'hod.')
        text_historie = ""
        for z in historia:
            mh = f"{z.stav_motohodin} {m_unit}" if z.stav_motohodin is not None else "Nezadané"
            mh_load = f" (v záťaži: {z.motohodiny_v_zatazi} {m_load_unit})" if z.motohodiny_v_zatazi is not None else ""
            text_historie += f"- {z.vytvorene.strftime('%d.%m.%Y')}, Stav: {mh}{mh_load}, Úkony/Poruchy: {z.poruchy}\n"
        return text_historie
    
    def vygeneruj_odborny_text_ai(self):
        if not self.poruchy or self.formalne_zhodnotenie_ai:
            return
        from Evidencia.tasks import vygeneruj_odborny_text_ai_task
        def _safe_ai_text(doc_pk):
            try:
                vygeneruj_odborny_text_ai_task.delay(doc_pk)
            except Exception as exc:
                logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre generovanie AI textu k záznamu {doc_pk}: {exc}")

        transaction.on_commit(lambda pk=self.pk: _safe_ai_text(pk))

    def vygeneruj_prediktivne_plany(self):
        if not self.zariadenie_id or not self.dokoncene:
            return

        PravidloServisnehoIntervalu = apps.get_model('Evidencia', 'PravidloServisnehoIntervalu')
        PlanovanyServisZariadenia = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')
            
        # 🛡️ DEFENSIVE: Pred vygenerovaním nových plánov zmažeme neodoslané plány viazané na tento konkrétny záznam
        PlanovanyServisZariadenia.objects.filter(posledny_zaznam=self, upozornenie_odoslane=False).delete()

        plany_na_vytvorenie = []
        for typ in self.typ_servisu.all():
            pravidlo = PravidloServisnehoIntervalu.objects.filter(
                typ_servisu=typ, 
                kategoria_zariadenia=self.zariadenie.kategoria
            ).first()

            if pravidlo:
                # Vymažeme staré neodoslané plány tohto typu
                PlanovanyServisZariadenia.objects.filter(
                    zariadenie=self.zariadenie,
                    typ_servisu=typ,
                    upozornenie_odoslane=False
                ).delete()

                novy_datum = None
                nove_mh = None

                if pravidlo.interval_dni:
                    novy_datum = timezone.now().date() + timedelta(days=pravidlo.interval_dni)
                
                if pravidlo.interval_motohodin and self.stav_motohodin is not None:
                    nove_mh = self.stav_motohodin + pravidlo.interval_motohodin
                    
                nove_mh_zataz = None
                if getattr(pravidlo, 'interval_motohodin_zataz', None) and getattr(self, 'motohodiny_v_zatazi', None) is not None:
                    nove_mh_zataz = self.motohodiny_v_zatazi + pravidlo.interval_motohodin_zataz

                if novy_datum or nove_mh or nove_mh_zataz:
                    plany_na_vytvorenie.append(PlanovanyServisZariadenia(
                        zariadenie=self.zariadenie,
                        typ_servisu=typ,
                        posledny_zaznam=self,
                        planovany_datum=novy_datum,
                        planovane_motohodiny=nove_mh,
                        planovane_motohodiny_zataz=nove_mh_zataz,
                        vaha_hierarchie=pravidlo.vaha_hierarchie
                    ))
        
        if plany_na_vytvorenie:
            PlanovanyServisZariadenia.objects.bulk_create(plany_na_vytvorenie)

    def odoslat_notifikacie_adminovi(self, poslat_tg=True, poslat_mail=True, poslat_discord=True):
        from Evidencia.tasks import zaznam_odoslat_notifikacie_adminovi_task
        def _safe_zaznam_admin_notif(doc_pk, tg, m, d):
            try:
                zaznam_odoslat_notifikacie_adminovi_task.delay([doc_pk], tg, m, d)
            except Exception as exc:
                logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre notifikáciu záznamu {doc_pk}: {exc}")

        transaction.on_commit(lambda pk=self.pk, tg=poslat_tg, m=poslat_mail, d=poslat_discord: _safe_zaznam_admin_notif(pk, tg, m, d))

    def anuluj_protokol(self, pouzivatel=None):
        """
        🛡️ ENTERPRISE DOMAIN LOGIC:
        Bezpečné stornovanie elektronického protokolu, vymazanie PDF z disku,
        anulovanie digitálnej pečate a vyčistenie naviazaných prediktívnych plánov.
        """
        PlanovanyServisZariadenia = apps.get_model('Evidencia', 'PlanovanyServisZariadenia')

        with transaction.atomic():
            # 1. Fyzické zmazanie PDF z úložiska
            if self.uzavrety_pdf_dokument:
                try:
                    self.uzavrety_pdf_dokument.delete(save=False)
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Chyba pri mazaní PDF súboru záznamu #{self.pk}: {e}")

            # 2. Bezpečnostný výmaz podpisov, GPS a digitálnej pečate
            # (Prediktívny plán stroja zostáva nedotknutý, pretože fyzický stav motohodín a servisu sa nezmenil)
            self.protokol_uzavrety = False
            self.cas_podpisu = None
            self.podpis_zakaznika = None
            self.podpis_technika = None
            self.meno_podpisujuceho = None
            self.bezpecnostny_hash = None
            self.gps_suradnice = None
            self.hodnotenie_zakaznika = None
            self.uzavrety_pdf_dokument = None

            self.save(update_fields=[
                'protokol_uzavrety', 'cas_podpisu', 'podpis_zakaznika',
                'podpis_technika', 'meno_podpisujuceho', 'bezpecnostny_hash',
                'gps_suradnice', 'hodnotenie_zakaznika', 'uzavrety_pdf_dokument'
            ])

            # 🛡️ IN-MEMORY SYNC: Synchronizácia stavu inštancie v RAM pre prípad ďalších volaní v tom istom procese
            if hasattr(self, '_povodny_stav') and isinstance(self._povodny_stav, dict):
                self._povodny_stav['protokol_uzavrety'] = False
                self._povodny_stav['bezpecnostny_hash'] = None
                self._povodny_stav['podpis_zakaznika'] = None

    def delete(self, *args, **kwargs):
        # 🛡️ PRÁVNA OCHRANA DÁT: Podpísaný protokol s digitálnou pečaťou je zakázané zmazať z DB!
        if self.protokol_uzavrety:
            raise ValidationError("🚫 Nie je možné vymazať servisný záznam s právne uzamknutým a podpísaným protokolom! Najskôr musí konateľ protokol resetovať.")
        
        # Prečistenie súborov z disku pred zmazaním neuzamknutého záznamu
        for field in [self.foto_pred, self.foto_po, self.video, self.uzavrety_pdf_dokument]:
            if field:
                try:
                    field.delete(save=False)
                except Exception:
                    pass
        return super().delete(*args, **kwargs)

    @property
    def poradove_cislo(self):
        """
        Dynamicky vypočíta lokálne poradové číslo protokolu pre konkrétneho zákazníka.
        """
        # Ochrana: Ak sme hodnotu už anotovali vo view (napr. cez Window funkciu), použijeme ju
        if hasattr(self, '_poradove_cislo_cache'):
            return self._poradove_cislo_cache
            
        if self.zakaznik_id and self.id:
            ZaznamModel = apps.get_model('Evidencia', 'ServisnyZaznam')
            return ZaznamModel.objects.filter(zakaznik_id=self.zakaznik_id, id__lte=self.id).count()
        return self.id

class PouzityMaterialVZaznamoch(ISASAuditMixin, models.Model):
    zaznam = models.ForeignKey(
        'ServisnyZaznam', 
        on_delete=models.CASCADE, 
        related_name='pouzite_polozky_zaznamu',
        verbose_name="📄 Servisný záznam",
        help_text="Servisný protokol, ku ktorému priraďujete tento použitý materiál alebo náhradný diel."
    )
    
    content_type = models.ForeignKey(
        ContentType, 
        on_delete=models.CASCADE, 
        limit_choices_to=ziskaj_povolene_modely, 
        verbose_name="🗂️ Kategória dielu",
        help_text="Vyberte typ skladového objektu podľa skladových kategórií aktívneho odvetvia."
    )

    object_id = models.PositiveIntegerField(verbose_name="🆔 Sériové číslo položky", null=True, blank=True, help_text="Interné ID systémovej položky z vybraného skladu.")
    content_object = GenericForeignKey('content_type', 'object_id')
    seriove_cislo_text = models.CharField("🔢 Sériové číslo", max_length=100, blank=True, help_text="Unikátne výrobné / sériové číslo inštalovaného dielu (ak je k dispozícii).")
    pocet = models.PositiveIntegerField(default=1, verbose_name="🔢 Počet", help_text="Vydané množstvo náhradného dielu / materiálu zo skladu na tento výjazd.")

    # 📸 ISAS SNAPSHOT: Kópia obchodných nastavení zo skladu
    obchodny_nazov = models.CharField(max_length=255, blank=True, null=True, editable=False)
    skryt_vyrobcu = models.BooleanField(default=False, editable=False)

    class Meta:
        verbose_name = "📦 Použitý materiál v servisnom zázname"
        verbose_name_plural = "📦 Použitý materiál v servisných záznamoch"
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        try:
            return f"{self.content_object}" if self.content_object else f"Položka #{self.object_id}"
        except ObjectDoesNotExist:
            return f"Zmazaná položka #{self.object_id}"

    def save(self, *args, **kwargs):
        # 📸 ISAS SNAPSHOT LOGIC: Prekopírovanie dát zo skladu pri vytvorení položky
        if not self.pk and self.content_type_id and self.object_id:
            try:
                diel = self.content_object
            except ObjectDoesNotExist:
                diel = None
            if diel:
                self.obchodny_nazov = getattr(diel, 'obchodny_nazov', None)
                self.skryt_vyrobcu = getattr(diel, 'skryt_vyrobcu', False)
        super().save(*args, **kwargs)

    # ====================================================
    # 🧠 ISAS CORE: DYNAMICKÉ VLASTNOSTI PRE PDF PROTOKOL
    # ====================================================
    @property
    def cisty_nazov(self):
        """Vráti čistý názov dielu bez skladovej zátvorky s podporou celých aj desatinných jednotiek."""
        if self.obchodny_nazov:
            return self.obchodny_nazov
        try:
            obj = self.content_object
            nazov = str(obj) if obj else str(self.seriove_cislo_text or "")
        except ObjectDoesNotExist:
            nazov = str(self.seriove_cislo_text or "")
        return ocisti_nazov_od_jednotky(nazov)

    @property
    def merna_jednotka_zobrazenie(self):
        """Inteligentne vytiahne presnú mernú jednotku z podradeného modelu (l, ks, bal, m, ...)"""
        try:
            obj = self.content_object
            if obj and hasattr(obj, 'merna_jednotka'):
                return obj.merna_jednotka
        except ObjectDoesNotExist:
            pass
        return 'ks' # Fallback, ak by jednotka chýbala

    @property
    def kategoria_zobrazenie(self):
        """Vytiahne krásny názov kategórie priamo z meta-dát modelu"""
        if self.content_type:
            model_cls = self.content_type.model_class()
            if model_cls:
                nazov = model_cls._meta.verbose_name
                return str(nazov).capitalize() # Prvé písmeno veľké pre profi vzhľad
        return "Nezaradené"

class PravidloServisnehoIntervalu(ISASAuditMixin, models.Model):
    typ_servisu = models.ForeignKey(
        'TypServisuPrace', 
        on_delete=models.CASCADE, 
        related_name="intervalove_pravidla", 
        verbose_name="🔧 Typ servisu/práce",
        help_text="🔧 Druh servisného úkonu, ktorý sa má automaticky plánovať pri dosiahnutí stanovených prevádzkových limitov."
    )
    
    # Pre akú kategóriu zariadenia toto pravidlo platí? (z modelu ServisovaneZariadenie.KATEGORIE)
    kategoria_zariadenia = models.CharField(
        "🏷️ Kategória zariadenia", max_length=50, choices=ServisovaneZariadenie.KATEGORIE,
        help_text="🏷️ Špecifická kategória zariadení, pre ktorú platí toto pravidlo preventívnej údržby."
    )
    
    interval_motohodin = models.PositiveIntegerField(
        "⏱️ Interval počítadla (Celkový beh / motohodiny)", null=True, blank=True, 
        help_text="⏱️ Maximálny počet prevádzkových hodín alebo cyklov od posledného servisu pre spustenie údržby (napr. 4000)."
    )
    interval_motohodin_zataz = models.PositiveIntegerField(
        "🏋️ Interval (Motohodiny v záťaži / Záťažové počítadlo)", null=True, blank=True, 
        help_text="🏋️ Počet odpracovaných hodín alebo cyklov v záťaži pre plánovanie špecifických generálnych úkonov."
    )
    interval_dni = models.PositiveIntegerField(
        "📅 Interval (Čas - Dni)", null=True, blank=True, 
        help_text="📅 Maximálny časový odstup v dňoch pre kalendárne plánovanú revíziu alebo údržbu (napr. 365 pre ročný servis)."
    )

    # --- 📖 DYNAMICKÉ ODVETVOVÉ PROPERTIES (INDUSTRY LEXICON) ---
    @property
    @admin.display(description="Interval počítadla")
    def interval_meter_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'interval_motohodin', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    @property
    @admin.display(description="Interval v záťaži")
    def interval_meter_load_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'interval_motohodin_zataz', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_load_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"
    
    # 🔥 INTELIGENCIA: Hierarchia prekrývania
    vaha_hierarchie = models.PositiveIntegerField(
        "⚖️ Váha / Priorita servisu", default=10,
        help_text="⚖️ Priorita pri súbehu viacerych servisov; vyššie číslo prebíja nižšie (napr. Generálka s váhou 30 nahradí Malý servis s váhou 10)."
    )

    class Meta:
        verbose_name = "📏 Pravidlo servisného intervalu"
        verbose_name_plural = "📏 Pravidlá servisných intervalov"
        unique_together = ('typ_servisu', 'kategoria_zariadenia')

    def __str__(self):
        return f"{self.typ_servisu.nazov} pre {self.get_kategoria_zariadenia_display()} (Váha: {self.vaha_hierarchie})"

class PlanovanyServisZariadenia(ISASAuditMixin, models.Model):
    zariadenie = models.ForeignKey('ServisovaneZariadenie', on_delete=models.CASCADE, related_name="planovane_servisy", verbose_name="🚜 Zariadenie", help_text="🚜 Konkrétne servisované zariadenie, pre ktoré sa plánuje preventívna údržba.")
    typ_servisu = models.ForeignKey('TypServisuPrace', on_delete=models.CASCADE, verbose_name="🔧 Typ servisu", help_text="🔧 Druh predpísaného servisného úkonu, ktorý má byť na zariadení vykonaný.")
    posledny_zaznam = models.ForeignKey('ServisnyZaznam', on_delete=models.SET_NULL, null=True, blank=True, help_text="📝 Posledný realizovaný servisný záznam, z ktorého bol tento plán odvodený.", verbose_name="📝 Posledný záznam")
    
    planovany_datum = models.DateField("🗓️ Plánovaný dátum (Časový servis)", null=True, blank=True, help_text="🗓️ Najneskorší kalendárny dátum pre vykonanie údržby podľa časového intervalu.")
    planovane_motohodiny = models.IntegerField("⏳ Plánované pri (Celkový stav počítadla)", null=True, blank=True, help_text="⏳ Cielený celkový stav počítadla zariadenia, pri ktorom musí prebehnúť servis.")
    planovane_motohodiny_zataz = models.IntegerField("🏋️ Plánované pri (Stav v záťaži)", null=True, blank=True, help_text="🏋️ Cielený stav počítadla v záťaži pre spustenie údržby.")
    
    vaha_hierarchie = models.PositiveIntegerField("⚖️ Váha", default=10, help_text="⚖️ Priorita úkonu v hierarchii servisu určujúca nadradenosť pri prekrývaní intervalov.") # Skopírujeme si ju sem pre rýchlejšie dotazy
    
    upozornenie_odoslane = models.BooleanField("🔔 Upozornenie odoslané", default=False, help_text="🔔 Indikátor, či už bola zákazníkovi alebo manažérovi odoslaná notifikácia o nadchádzajúcom servise.")
    vytvorene = models.DateTimeField(auto_now_add=True, verbose_name="🕒 Vytvorené", help_text="🕒 Systémový dátum a čas automatického vygenerovania tohto plánovaného úkonu.")

    # --- 📖 DYNAMICKÉ ODVETVOVÉ PROPERTIES (INDUSTRY LEXICON) ---
    @property
    @admin.display(description="Plánovaný stav počítadla")
    def meter_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'planovane_motohodiny', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    @property
    @admin.display(description="Plánovaný stav v záťaži")
    def meter_load_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'planovane_motohodiny_zataz', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_load_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    class Meta:
        verbose_name = "📅 Plánovaný úkon"
        verbose_name_plural = "📅 Plánované úkony zariadení"
        ordering = ['planovany_datum', 'planovane_motohodiny']

    def __str__(self):
        return f"Plán: {self.typ_servisu} pre {self.zariadenie}"

class CenovaPonuka(ISASAuditMixin, models.Model):
    STAV_CHOICES = [
        ('DRAFT', '📝 Rozpracovaná'),
        ('SENT', '📤 Odoslaná zákazníkovi'),
        ('ACCEPTED', '✅ Schválená zákazníkom'),
        ('REJECTED', '❌ Zamietnutá'),
        ('CONVERTED', '⚙️ Konvertovaná na Úlohu')
    ]

    cislo_ponuky = models.CharField("🔢 Číslo ponuky", max_length=50, unique=True, editable=False, help_text="🔢 Unikátny systémový kód ponuky generovaný automaticky pri uložení.")
    
    # --- 1. PREPOJENIA NA ISAS CORE ---
    zakaznik = models.ForeignKey('Zakaznik', on_delete=models.CASCADE, related_name="cenove_ponuky", verbose_name="🏢 Zákazník", help_text="🏢 Odberateľ, pre ktorého je cenová ponuka vypracovaná.")
    zariadenie = models.ForeignKey('ServisovaneZariadenie', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="⚙️ Týka sa zariadenia", help_text="⚙️ Špecifické servisované zariadenie pre automatické načítanie predpisov a histórie údržby.")
    typ_servisu: models.ManyToManyField = models.ManyToManyField('TypServisuPrace', blank=True, verbose_name="🛠️ Typ servisu/práce", help_text="🛠️ Kategórie servisných prác pre správne plánovanie a vygenerovanie servisnej úlohy.")
    vystavil = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, limit_choices_to={'is_staff': True}, verbose_name="👤 Vystavil", help_text="👤 Interný pracovník zodpovedný za vypracovanie a správu cenovej ponuky.")
    vytvorene_ulohy: models.ManyToManyField = models.ManyToManyField('Uloha', blank=True, related_name='zdrojove_ponuky_ulohy', verbose_name="📋 Vygenerované Úlohy pre technikov", help_text="📋 Servisné úlohy automaticky vygenerované a priradené technikom po schválení ponuky.")

    # --- 2. DÁTUMY A PLATNOSŤ ---
    datum_vystavenia = models.DateField("📅 Dátum vystavenia", default=timezone.now, help_text="📅 Dátum vystavenia ponuky rozhodujúci pre určenie doby platnosti.")
    stav = models.CharField("📊 Stav ponuky", max_length=20, choices=STAV_CHOICES, default='DRAFT', help_text="📊 Aktuálny stav obchodného prípadu v životnom cykle ponuky.")
    mena = models.CharField("💱 Mena", max_length=10, blank=True, default="", help_text="💱 Mena cenovej ponuky podľa firemných nastavení.")

    # --- 3. OBCHODNÁ LOGIKA A LEGISLATÍVA ---
    prenos_danovej_povinnosti = models.BooleanField("⚖️ Režim prenesenia daňovej povinnosti (Reverse charge)", default=False, help_text="⚖️ Aplikácia režimu prenesenia daňovej povinnosti (Reverse charge) bez vyčíslenia dane.")
    globalna_zlava_percento = models.DecimalField("🏷️ Globálna zľava na celú ponuku (%)", max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="🏷️ Percentuálna zľava aplikovaná na celkovú sumu ponuky po zrátaní položiek.")

    # --- 4. AI A TEXTOVÉ POLIA ---
    ai_uvodny_text = models.TextField("🤖 Úvodný text ponuky (Generované AI)", blank=True, null=True, help_text="🤖 Úvodný sprievodný text ponuky zobrazovaný na prvej strane PDF dokumentu.")
    zavrecny_text = models.TextField("📜 Záverečné obchodné podmienky", blank=True, null=True, help_text="📜 Obchodné podmienky, dodacie lehoty, záručné podmienky a platnosť ponuky.")
    interne_poznamky = models.TextField("🔒 Interné poznámky (Skryté pre klienta)", blank=True, null=True, help_text="🔒 Interné poznámky pre manažment a technikov neprístupné pre zákazníka.")

    # --- 5. PEVNÉ FINANČNÉ SUMÁRE (Ukladané tvrdo pre maximálnu rýchlosť) ---
    celkom_bez_dph_pred_zlavou = models.DecimalField("💰 Celkom bez DPH (pred zľavou)", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Suma všetkých položiek ponuky bez DPH pred uplatnením globálnej zľavy.")
    hodnota_zlavy = models.DecimalField("📉 Hodnota zľavy", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Vypočítaná finančná hodnota poskytnutej zľavy.")
    celkom_bez_dph = models.DecimalField("💰 Celkom bez DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Celková základná suma ponuky bez DPH po uplatnení zľavy.")
    celkom_dph = models.DecimalField("🏛️ Suma DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Celková vypočítaná suma DPH pre výkazníctvo a účtovný export.")
    celkom_s_dph = models.DecimalField("💳 Celkom s DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Konečná celková suma ponuky vrátane DPH na úhradu zákazníkom.")

    # --- 6. VÝSTUP ---
    pdf_dokument = models.FileField(upload_to="cenove_ponuky_pdf/%Y/%m/", blank=True, null=True, verbose_name="📄 Vygenerované PDF", help_text="📄 Vygenerovaný oficiálny PDF dokument cenovej ponuky určený na odoslanie.")

    # --- 7. AUTOMATIZÁCIA A KLIENTSKÝ PORTÁL (Nové polia) ---
    bezpecnostny_kluc = models.UUIDField("🔑 Magic Link Kľúč", default=uuid.uuid4, editable=False, unique=True, help_text="🔑 Unikátny zabezpečený kľúč pre online prístup zákazníka bez nutnosti prihlásenia.")
    cas_schvalenia = models.DateTimeField("⏱️ Čas online schválenia", null=True, blank=True, help_text="⏱️ Presná časová pečiatka online schválenia alebo zamietnutia ponuky zákazníkom.")
    ip_adresa_schvalitela = models.CharField("🌐 IP Adresa schvaľovateľa", max_length=50, null=True, blank=True, help_text="🌐 IP adresa zariadenia, z ktorého zákazník vykonal online schválenie ponuky.")
    meno_schvalitela = models.CharField("✍️ Meno schvaľovateľa (E-Podpis)", max_length=150, null=True, blank=True, help_text="✍️ Meno a priezvisko osoby, ktorá ponuku elektronicky schválila.")

    class Meta:
        verbose_name = "💰 Cenová ponuka"
        verbose_name_plural = "💰 Cenové ponuky"
        ordering = ['-datum_vystavenia', '-id']

    def __str__(self):
        if self.pk:
            # 1. Bezpečné načítanie základných údajov 🛡️
            cislo = self.cislo_ponuky if self.cislo_ponuky else f"DRAFT-#{self.pk}"
            zakaznik_str = str(self.zakaznik) if self.zakaznik else "Nešpecifikovaný zákazník"
            
            # Zozbierame unikátne NÁZVY strojov (Ochrana pred prázdnymi poliami)
            zariadenia_nazvy = set()
            
            def pridaj_stroj(zar):
                if zar:
                    # Fallback: Ak chýba model, skúsime interný názov, inak str()
                    nazov = getattr(zar, 'model_zariadenia', '') or getattr(zar, 'nazov', '') or str(zar)
                    if nazov and str(nazov).strip():
                        zariadenia_nazvy.add(str(nazov).strip())

            # Pridáme hlavný stroj z hlavičky
            pridaj_stroj(self.zariadenie)
            
            # Pridáme všetky stroje z položiek buildera
            for polozka in self.polozky.all():
                pridaj_stroj(polozka.zariadenie)
                    
            zariadenie_str = ", ".join(zariadenia_nazvy) if zariadenia_nazvy else "Bez zariadenia"
            
            # 2. Dynamická ikona podľa stavu ponuky 📊
            stav_ikony = {'DRAFT': '📝', 'SENT': '📤', 'ACCEPTED': '✅', 'REJECTED': '❌', 'CONVERTED': '⚙️'}
            ikona = stav_ikony.get(self.stav, '💰')
            
            # 3. Načítanie ManyToMany poľa (Typ servisu) 🛠️
            typy_str = ", ".join([str(t) for t in self.typ_servisu.all()]) or "Bez typu"
            
            mena_str = f" {self.mena}".rstrip() if getattr(self, 'mena', None) else ""
            return f"{ikona} {cislo} | 🏢 {zakaznik_str} | ⚙️ {zariadenie_str} | 🛠️ {typy_str} | 💳 {self.celkom_s_dph}{mena_str}"
        return "✨ Nová cenová ponuka"

    def save(self, *args, **kwargs):
        if not self.mena:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentitaModel.objects.first()
            country_defaults = FiremnaIdentitaModel.get_country_defaults() if (FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'get_country_defaults')) else {}
            self.mena = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()

        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'mena', 'cislo_ponuky'}

        if not self.cislo_ponuky:
            with transaction.atomic():
                rok = timezone.now().strftime('%y')
                
                # Uzamkneme zákazníka pre bezpečné generovanie sekvencie
                zakaznik_locked = None
                if self.zakaznik_id:
                    zakaznik_locked = Zakaznik.objects.select_for_update().get(pk=self.zakaznik_id)
                
                surovy_nazov = getattr(zakaznik_locked, 'Nazov_spolocnosti', None) if zakaznik_locked else None
                cisty_nazov = str(surovy_nazov or 'FIRMA')
                cisty_nazov = re.sub(r'(?i)\b(s\.r\.o\.|a\.s\.|spol\. s r\.o\.|inc\.|ltd\.|gmbh|kft|sp\. z o\.o\.|sp\. z oo|s\.a\.|s\.r\.l\.|b\.v\.|corp\.)\b', '', cisty_nazov).strip()
                cisty_nazov = ''.join(c for c in unicodedata.normalize('NFD', cisty_nazov) if unicodedata.category(c) != 'Mn')
                skratka_firmy = re.sub(r'[^a-zA-Z0-9]', '', cisty_nazov)[:6].upper()
                if not skratka_firmy:
                    skratka_firmy = "KLIENT"

                hladany_prefix = f"CP-{skratka_firmy}-{rok}-"
                
                vsetky_cisla = CenovaPonuka.objects.filter(
                    cislo_ponuky__startswith=hladany_prefix
                ).values_list('cislo_ponuky', flat=True)
                
                max_cislo = 0
                for c_str in vsetky_cisla:
                    try:
                        cislo = int(c_str.split('-')[-1])
                        if cislo > max_cislo:
                            max_cislo = cislo
                    except (ValueError, IndexError):
                        pass
                        
                uspesne_ulozene = False
                for pokus in range(10):
                    nove_cislo = max_cislo + 1 + pokus
                    self.cislo_ponuky = f"{hladany_prefix}{nove_cislo:03d}"
                    try:
                        with transaction.atomic():
                            super().save(*args, **kwargs)
                            uspesne_ulozene = True
                            break
                    except IntegrityError:
                        continue
                if not uspesne_ulozene:
                    raise ValidationError("🚨 Systém je momentálne vyťažený. Nepodarilo sa vygenerovať unikátne číslo cenovej ponuky. Skúste znova.")
        else:
            super().save(*args, **kwargs)

        if self.stav in ['SENT', 'ACCEPTED']:
            from Evidencia.tasks import task_vyhodnot_smart_matcher_pre_ponuku
            def _safe_smart_matcher_ponuka(doc_pk):
                try:
                    task_vyhodnot_smart_matcher_pre_ponuku.delay(doc_pk)
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre Smart Matcher ponuky {doc_pk}: {exc}")

            transaction.on_commit(lambda pk=self.pk: _safe_smart_matcher_ponuka(pk))

class PolozkaCenovejPonuky(ISASAuditMixin, models.Model):
    TYP_POLOZKY = [
        ('MAT', '📦 Materiál / Diel'),
        ('PRACA', '👨‍🔧 Práca / Služba'),
        ('DOPRAVA', '🚗 Doprava / Výjazd'),
        ('ZLA', '📉 Zľava'),
        ('INE', '🔖 Iné')
    ]

    ponuka = models.ForeignKey(CenovaPonuka, on_delete=models.CASCADE, related_name="polozky", verbose_name="📄 Cenová ponuka", help_text="📄 Nadriadená cenová ponuka, ku ktorej táto položka prislúcha.")
    typ_polozky = models.CharField(max_length=10, choices=TYP_POLOZKY, default='MAT', verbose_name="🏷️ Typ položky", help_text="🏷️ Kategória položky pre správne vyhodnotenie nákladov a štruktúru ponuky.")
    
    # --- 1. TVRDÉ (PEVNÉ) ÚDAJE PRE ÚČTOVNÍCTVO A PDF ---
    # Toto zabezpečí, že ak niekto premenuje diel v sklade, ponuka zostane navždy rovnaká!
    nazov = models.CharField("📝 Názov položky (Na PDF)", max_length=500, help_text="📝 Textový názov položky zobrazený na PDF ponuke, nezávislý od zmien v sklade.")
    mnozstvo = models.DecimalField("⚖️ Množstvo", max_digits=8, decimal_places=2, default=Decimal('1.00'), help_text="⚖️ Počet jednotiek materiálu alebo rozsahu poskytnutej služby.")
    merna_jednotka = models.CharField("📏 M.J.", max_length=10, default='ks', help_text="📏 Merná jednotka pre fakturáciu a kalkuláciu.")
    
    jednotkova_cena_bez_dph = models.DecimalField("💰 Jedn. cena bez DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Základná cenníková alebo dohodnutá cena za jednotku bez DPH.")
    zlava_percento = models.DecimalField("📉 Riadková zľava (%)", max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="📉 Percentuálna zľava aplikovaná priamo na túto riadkovú položku.")
    sadzba_dph = models.DecimalField("🏛️ Sadzba DPH (%)", max_digits=4, decimal_places=2, default=get_default_dph, help_text="Zákonná sadzba DPH v percentách pre výpočet dane a export do účtovníctva.")

    # 📸 ISAS SNAPSHOT: Kópia obchodných nastavení zo skladu
    obchodny_nazov = models.CharField(max_length=255, blank=True, null=True, editable=False)
    skryt_vyrobcu = models.BooleanField(default=False, editable=False)

    # --- 2. 🧠 SOFT-LINK PRE AUTOMATIZÁCIU (Košík / Uloha) ---
    # Ak položka pochádza z nášho skladu, uložíme si jej ID. 
    # Ak je to položka pridaná "ručne obchodníkom" textom, tieto polia ostanú prázdne (Null).
    content_type = models.ForeignKey(
        ContentType, 
        on_delete=models.SET_NULL, 
        null=True, blank=True,
        limit_choices_to=ziskaj_povolene_modely,
        verbose_name="🔗 Typ obsahu",
        help_text="🔗 Systémové prepojenie na konkrétny modul ERP pre automatickú väzbu."
    )
    object_id = models.PositiveIntegerField(null=True, blank=True, verbose_name="🆔 ID objektu", help_text="🆔 Identifikátor prepojeného záznamu vo vybranom module.")
    skladovy_diel = GenericForeignKey('content_type', 'object_id')
    zariadenie = models.ForeignKey('ServisovaneZariadenie', on_delete=models.SET_NULL, null=True, blank=True, related_name='ponuknute_polozky', verbose_name="Priradené zariadenie", help_text="🚜 Konkrétne servisované zariadenie, ku ktorému sa viaže tento materiál alebo práca.")

    @property
    def cena_po_zlave_bez_dph(self):
        # Bezpečný fallback pre prípad, že by databáza vrátila None alebo float
        bezpecne_mnozstvo = Decimal(str(self.mnozstvo)) if self.mnozstvo is not None else Decimal('0.00')
        bezpecna_cena = Decimal(str(self.jednotkova_cena_bez_dph)) if self.jednotkova_cena_bez_dph is not None else Decimal('0.00')
        bezpecna_zlava = Decimal(str(self.zlava_percento)) if self.zlava_percento is not None else Decimal('0.00')
        
        zlava_koeficient = Decimal('1.00') - (bezpecna_zlava / Decimal('100.00'))
        cena = bezpecne_mnozstvo * bezpecna_cena * zlava_koeficient
        return cena.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def hodnota_dph(self):
        # Ochrana: Ak má ponuka režim prenesenia daňovej povinnosti, DPH je nula.
        if self.ponuka and self.ponuka.prenos_danovej_povinnosti:
            return Decimal('0.00')
            
        bezpecna_sadzba = Decimal(str(self.sadzba_dph)) if self.sadzba_dph is not None else Decimal('0.00')
        return (self.cena_po_zlave_bez_dph * (bezpecna_sadzba / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def spolu_s_dph(self):
        return self.cena_po_zlave_bez_dph + self.hodnota_dph

    class Meta:
        verbose_name = "🏷️ Položka cenovej ponuky"
        verbose_name_plural = "🏷️ Položky cenovej ponuky"
        ordering = ['id'] # Zabezpečí, že sa na PDF vypíšu v rovnakom poradí, ako ich tam technik naťukal
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        return f"{self.nazov} ({self.mnozstvo} {self.merna_jednotka})"

    def save(self, *args, **kwargs):
        # 📸 ISAS SNAPSHOT LOGIC: Prekopírovanie dát zo skladu pri vytvorení položky
        if not self.pk and self.content_type_id and self.object_id:
            try:
                diel = self.skladovy_diel
            except ObjectDoesNotExist:
                diel = None
            if diel:
                self.obchodny_nazov = getattr(diel, 'obchodny_nazov', None)
                self.skryt_vyrobcu = getattr(diel, 'skryt_vyrobcu', False)
        super().save(*args, **kwargs)

class PovolenieNaVstup(ISASAuditMixin, models.Model):
    STAV_CHOICES = [
        ('GENEROVANE', '🤖 Vygenerované AI'),
        ('ODOSLANE', '📧 Odoslané na schválenie'),
        ('SCHVALENE', '✅ Schválené (Môžu vstúpiť)'),
        ('ZAMIETNUTE', '❌ Zamietnuté'),
    ]

    zakaznik = models.ForeignKey(Zakaznik, on_delete=models.CASCADE, related_name='povolenia_vstupov', verbose_name="🏢 Zákazník", help_text="🏢 Klient, do ktorého objektu alebo areálu sa povolenie na vstup vybavuje.")
    uloha = models.ForeignKey('Uloha', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="📋 Spojené s typom vykonávanej práce/servisu", help_text="📋 Konkrétna servisná alebo montážna zákazka, ku ktorej prístup personálu prináleží.")
    
    # Kto a s čím ide do fabriky
    zamestnanci = models.ManyToManyField(Zamestnanec, verbose_name="👷 Pridelení zamestnanci", help_text="👷 Zoznam technikov a pracovníkov, pre ktorých sa žiada bezpečnostné schválenie vstupu.")
    vozidla = models.ManyToManyField(Vozidlo, blank=True, verbose_name="🚗 Vozidlá na vstup", help_text="🚗 Firemné vozidlá a technika požadujúce vjazd do stráženého areálu klienta.")
    
    datum_vstupu = models.DateField(verbose_name="📅 Dátum plánovaného vstupu", help_text="📅 Plánovaný deň realizácie prác a fyzického vstupu personálu na lokálnu pobočku.")
    stav = models.CharField(max_length=15, choices=STAV_CHOICES, default='GENEROVANE', verbose_name="🚥 Stav", help_text="🚥 Aktuálny stav schvaľovacieho procesu vstupu zo strany zákazníka alebo SBS.")
    
    # Výsledok práce AI
    vygenerovany_dokument = models.FileField(upload_to='vygenerovane_vstupy/', blank=True, null=True, verbose_name="📄 Vygenerovaný dokument", help_text="📄 Oficiálny tlačivový dokument povolenia určený pre kontrolu vrátnicou.")
    ai_text_emailu = models.TextField(blank=True, null=True, verbose_name="🤖 Vygenerovaný text pre SBS", help_text="🤖 Automaticky vygenerovaný sprievodný text žiadosti odosielaný na SBS zákazníka.")

    bezpecnostny_kluc = models.UUIDField("🔑 Kľúč pre vrátnicu", default=uuid.uuid4, editable=False, unique=True, help_text="🔑 Unikátny overovací kód slúžiaci SBS na priame schválenie vstupu cez portál.")
    cas_schvalenia_sbs = models.DateTimeField("⏱️ Čas schválenia vrátnicou", null=True, blank=True, help_text="⏱️ Presný časový záznam o potvrdení vstupu službou vrátnice.")
    ip_schvalitela_sbs = models.CharField("🌐 IP Adresa vrátnice", max_length=50, null=True, blank=True, help_text="🌐 Sieťová IP adresa zariadenia SBS pre auditnú sledovateľnosť schválenia.")

    # --- 🎛️ NASTAVENIA NOTIFIKÁCIÍ PRE SBS PORTÁL ---
    notif_technik_tg = models.BooleanField("📱 Telegram: Notifikácia technikom", default=True, help_text="📱 Zaslanie okamžitej správy technikom cez Telegram pri zmene stavu vstupu.")
    notif_technik_mail = models.BooleanField("✉️ E-mail: Notifikácia technikom", default=True, help_text="✉️ Odoslanie e-mailovej notifikácie technikom o výsledku schvaľovania.")
    notif_technik_discord = models.BooleanField("🎮 Discord: Notifikácia technikom", default=True, help_text="💬 Zaslanie správy technikom cez Discord o výsledku schvaľovania vstupu.")
    notif_admin_tg = models.BooleanField("📱 Telegram: Notifikácia majiteľovi", default=True, help_text="📱 Notifikovanie vedenia firmy cez Telegram o schválení či zamietnutí vstupu.")
    notif_admin_mail = models.BooleanField("✉️ E-mail: Notifikácia majiteľovi", default=True, help_text="✉️ Zaslanie sumárnej e-mailovej notifikácie vedúcim pracovníkom o stave žiadosti.")
    notif_admin_discord = models.BooleanField("🎮 Discord: Notifikácia majiteľovi", default=True, help_text="💬 Notifikovanie vedenia firmy cez Discord o schválení či zamietnutí vstupu.")

    poznamka_sbs = models.TextField(verbose_name="Poznámka od SBS", blank=True, null=True, help_text="📝 Dôvod zamietnutia alebo špeciálne bezpečnostné inštrukcie zadané vrátnicou.")

    class Meta:
        verbose_name = "🚧 Povolenie na vstup"
        verbose_name_plural = "🚧 Povolenia na vstup"
        ordering = ['-datum_vstupu']
        # 🛡️ ISAS ARCHITEKTÚRA: Ochrana databázy proti duplicitám pri súbehu
        unique_together = ('zakaznik', 'uloha', 'datum_vstupu')

    def __str__(self):
        datum_str = self.datum_vstupu.strftime('%d.%m.%Y') if self.datum_vstupu else 'Bez dátumu'
        return f"Vstup: {self.zakaznik or 'Neznámy zákazník'} ({datum_str})"

    @property
    def vytvoril_zamestnanec(self):
        """
        🧠 ISAS NEXUS: Dynamicky vytiahne zamestnanca, ktorý vytvoril tento záznam.
        Prehľadá ISASAuditLog pre akciu 'CREATE', získa Usera a cez OneToOneField vráti Zamestnanca.
        """
        if not self.pk:
            return None

        from ISASaplikacia.models import ISASAuditLog
        
        try:
            ct = ContentType.objects.get_for_model(self)
            # Nájdeme prvý log (vytvorenie záznamu)
            log = ISASAuditLog.objects.filter(content_type=ct, object_id=self.pk, akcia='CREATE').first()
            
            if log and log.vykonal:
                # Využijeme tvoje OneToOne prepojenie z modelu Zamestnanec
                return getattr(log.vykonal, 'zamestnanec', None)
        except Exception:
            pass
        return None

class PrijataObjednavka(ISASAuditMixin, models.Model):
    STAV_CHOICES = [
        ('NOVA', '🆕 Nová (Čaká na spracovanie)'),
        ('SCHVALENA', '✅ Schválená (V riešení / Plánuje sa)'),
        ('VYBAVENA', '📦 Vybavená (Čaká na fakturáciu)'),
        ('VYFAKTUROVANA', '🧾 Vyfakturovaná (Uzavretá)'),
        ('ZAMIETNUTA', '❌ Zamietnutá / Stornovaná'),
    ]

    PRIORITA_CHOICES = [
        ('NIZKA', '🟢 Nízka (Plánovaná údržba)'),
        ('STREDNA', '🟡 Stredná (Štandard)'),
        ('VYSOKA', '🔴 Vysoká (Havarijný stav / SLA)'),
    ]

    FORMA_PRIJATIA_CHOICES = [
        ('SUBOR', '📄 Súbor (PDF / Word / Sken)'),
        ('EMAIL', '✉️ Text v e-maile'),
        ('PORTAL', '🏢 B2B Portál (SAP, Ariba, Coupa...)'),
        ('WEB', '🌐 Verejný web (Dopytový formulár)'),
        ('TELEFON', '📞 Telefonicky / Ústne (Havarijný stav)'),
    ]

    cislo_objednavky_interne = models.CharField(
        "🔢 Naše číslo objednávky", max_length=50, unique=True, editable=False,
        help_text="🔢 Unikátny interný identifikátor vygenerovaný systémom pre evidenciu a účtovníctvo."
    )
    cislo_objednavky_klienta = models.CharField(
        "🏷️ Číslo objednávky klienta (PO Number)", max_length=100, blank=True, null=True,
        help_text="🏷️ Číslo objednávky od zákazníka (PO Number) vyžadované na faktúre pre schválenie úhrady."
    )

    zakaznik = models.ForeignKey(
        'Zakaznik', on_delete=models.PROTECT, related_name="prijate_objednavky", verbose_name="🏢 Zákazník",
        help_text="🏢 Zákazník, pre ktorého sa zákazka realizuje a ktorému bude vystavená faktúra."
    )
    zariadenia: models.ManyToManyField = models.ManyToManyField(
        'ServisovaneZariadenie', blank=True, verbose_name="⚙️ Týka sa zariadení",
        help_text="⚙️ Zariadenia určené na servis pre správne sledovanie histórie a nákladov."
    )
    typ_servisu: models.ManyToManyField = models.ManyToManyField(
        'TypServisuPrace', blank=True, verbose_name="🛠️ Typ servisu/práce",
        help_text="🛠️ Požadované kategórie servisných prác pre plánovanie a priradenie technikov."
    )
    
    zodpovedny_zamestnanec = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, 
        limit_choices_to={'is_staff': True},  # 🛡️ Zamedzí výberu externého B2B klienta
        verbose_name="👤 Zodpovedná osoba",
        help_text="👤 Interný zamestnanec zodpovedný za vybavovanie a priebeh objednávky."
    )
    
    zdrojove_ponuky: models.ManyToManyField = models.ManyToManyField(
        'CenovaPonuka', blank=True, 
        related_name="objednavky", verbose_name="📄 Zdrojové cenové ponuky",
        help_text="📄 Schválené cenové ponuky spárované s touto objednávkou pre automatické zlúčenie do faktúry."
    )
    vytvorene_ulohy: models.ManyToManyField = models.ManyToManyField(
        'Uloha', blank=True, 
        related_name='zdrojove_objednavky_ulohy', verbose_name="📋 Vygenerované Úlohy pre technikov",
        help_text="📋 Pracovné úlohy vygenerované pre technikov v teréne na realizáciu objednávky."
    )
    navrhnute_ponuky: models.ManyToManyField = models.ManyToManyField(
        'CenovaPonuka', blank=True, 
        related_name="navrhovane_objednavky", verbose_name="🤖 AI Navrhnuté ponuky",
        help_text="🤖 Návrhy cenových ponúk vyhodnotené systémom pre rýchle spárovanie."
    )
    ignorovane_ponuky: models.ManyToManyField = models.ManyToManyField(
        'CenovaPonuka', blank=True, related_name="ignorovane_v_objednavkach",
        verbose_name="🚫 Trvalo ignorované ponuky",
        help_text="🚫 Cenové ponuky odmietnuté dispečerom pri spárovaní s touto objednávkou."
    )
    stav = models.CharField(
        "🚥 Stav objednávky", max_length=20, choices=STAV_CHOICES, default='NOVA',
        help_text="🚥 Aktuálny stav spracovania objednávky v rámci interného prevádzkového procesu."
    )
    priorita = models.CharField(
        "🚨 Priorita / SLA", max_length=20, choices=PRIORITA_CHOICES, default='STREDNA',
        help_text="🚨 Úroveň dôležitosti určujúca čas reakcie a priority pri plánovaní kapacít."
    )
    
    datum_prijatia = models.DateField(
        "📅 Dátum prijatia", default=timezone.now,
        help_text="📅 Dátum doručenia objednávky pre evidenciu a plnenie dohodnutých SLA limitov."
    )
    termin_dodania = models.DateField(
        "⏳ Požadovaný termín dodania / servisu", null=True, blank=True,
        help_text="⏳ Zákazníkom požadovaný konečný termín dokončenia dodávky alebo servisu."
    )

    celkova_suma_bez_dph = models.DecimalField(
        "💰 Schválený rozpočet bez DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Schválený finančný limit zákazky bez DPH pre kontrolu rozpočtu."
    )
    mena = models.CharField(
        "💱 Mena", max_length=10, blank=True, default="",
        help_text="💱 Mena objednávky podľa firemných nastavení."
    )

    forma_prijatia = models.CharField(
        "📥 Forma prijatia", max_length=15, choices=FORMA_PRIJATIA_CHOICES, default='SUBOR',
        help_text="📥 Kanál, ktorým bola objednávka doručená pre spätnú overiteľnosť."
    )
    
    subor_objednavky = models.FileField(
        upload_to="prijate_objednavky/%Y/%m/", blank=True, null=True, 
        verbose_name="📄 Súbor objednávky",
        help_text="📄 Originálny dokument od zákazníka pre overenie a rýchlu kontrolu pri fakturácii."
    )
    text_emailu = models.TextField(
        "✉️ Text e-mailu / Ústna dohoda", blank=True, null=True,
        help_text="✉️ Kópia e-mailu alebo zápis ústnej dohody slúžiaci ako podklad pre realizáciu."
    )
    odkaz_na_portal = models.URLField(
        "🌐 Odkaz na B2B Portál", max_length=500, blank=True, null=True,
        help_text="🌐 Odkaz na detail objednávky v externom B2B nákupnom portáli zákazníka."
    )

    vystavil_za_klienta = models.CharField(
        "👤 Vystavil za klienta (Nákupca)", max_length=150, blank=True, null=True,
        help_text="👤 Meno kontaktej osoby nákupcu, ktorá objednávku u zákazníka vystavila."
    )
    specificky_email_pre_fakturu = models.EmailField(
        "📧 Špecifický e-mail pre faktúru", blank=True, null=True,
        help_text="📧 Špecifická e-mailová adresa odberateľa určená na doručenie faktúry."
    )
    vyzadovana_splatnost_dni = models.PositiveIntegerField(
        "⏳ Vyžadovaná splatnosť (Dni)", blank=True, null=True,
        help_text="⏳ Požadovaná lehota splatnosti faktúry v dňoch určená v objednávke."
    )

    poznamka_klienta = models.TextField(
        "📝 Poznámka od klienta", blank=True, null=True,
        help_text="📝 Požiadavky a doplňujúce pokyny zadané priamo zákazníkom."
    )
    interna_poznamka = models.TextField(
        "🔒 Interná poznámka (Skrytá)", blank=True, null=True,
        help_text="🔒 Dôverné prevádzkové poznámky určené výhradne pre interný tím."
    )

    class Meta:
        verbose_name = "📥 Prijatá objednávka"
        verbose_name_plural = "📥 Prijaté objednávky"
        ordering = ['-datum_prijatia', '-id']

    @classmethod
    def from_db(cls, db, field_names, values, fetch_mode=None):
        instance = super().from_db(db, field_names, values, fetch_mode=fetch_mode)
        instance._povodny_stav = dict(zip(field_names, values))
        return instance

    def __str__(self):
        if self.pk:
            # 1. Dynamická ikona podľa stavu
            stav_ikony = {
                'NOVA': '🆕', 
                'SCHVALENA': '✅', 
                'VYBAVENA': '📦', 
                'VYFAKTUROVANA': '🧾', 
                'ZAMIETNUTA': '❌'
            }
            ikona = stav_ikony.get(self.stav, '📥')
            
            # 2. Číslo objednávky klienta (PO Number) - kritické pre vyhľadávanie
            po_text = f" [PO: {self.cislo_objednavky_klienta}]" if self.cislo_objednavky_klienta else ""
            
            # 3. Zákazník
            zakaznik_str = str(self.zakaznik) if self.zakaznik else "Neznámy zákazník"
            
            # 4. Rozpočet / Suma
            mena_str = f" {self.mena}".rstrip() if getattr(self, 'mena', None) else ""
            suma_str = f" | 💰 {self.celkova_suma_bez_dph}{mena_str}" if self.celkova_suma_bez_dph > Decimal('0.00') else ""
            
            # 5. Informácia o spárovaní (Ak už má ponuky, ukážeme ich)
            ponuky_str = ""
            if self.pk and self.zdrojove_ponuky.exists():
                cisla = [p.cislo_ponuky for p in self.zdrojove_ponuky.all()]
                ponuky_str = f" | 🔗 {', '.join(cisla)}"
            
            return f"{ikona} {self.cislo_objednavky_interne}{po_text} | 🏢 {zakaznik_str}{suma_str}{ponuky_str}"
            
        return "✨ Nová prijatá objednávka"

    def save(self, *args, **kwargs):
        if not self.mena:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentitaModel.objects.first()
            country_defaults = FiremnaIdentitaModel.get_country_defaults() if (FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'get_country_defaults')) else {}
            self.mena = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()

        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'mena', 'cislo_objednavky_interne'}

        # 🚀 ISAS WEBHOOK DETEKCIA ZMENY STAVU
        stav_zmeneny = False
        povodny_stav = getattr(self, '_povodny_stav', {})
        stary_stav = povodny_stav.get('stav') if isinstance(povodny_stav, dict) else None
        if not self.pk or stary_stav != self.stav:
            stav_zmeneny = True

        if not self.cislo_objednavky_interne:
            with transaction.atomic():
                d_prijatia = to_date(self.datum_prijatia) or timezone.now().date()
                rok = d_prijatia.strftime('%y')
                
                # Uzamkneme zákazníka
                zakaznik_locked = None
                if self.zakaznik_id:
                    zakaznik_locked = Zakaznik.objects.select_for_update().get(pk=self.zakaznik_id)
                
                surovy_nazov = getattr(zakaznik_locked, 'Nazov_spolocnosti', None) if zakaznik_locked else None
                cisty_nazov = str(surovy_nazov or 'FIRMA')
                cisty_nazov = re.sub(r'(?i)\b(s\.r\.o\.|a\.s\.|spol\. s r\.o\.|inc\.|ltd\.|gmbh|kft|sp\. z o\.o\.|sp\. z oo|s\.a\.|s\.r\.l\.|b\.v\.|corp\.)\b', '', cisty_nazov).strip()
                cisty_nazov = ''.join(c for c in unicodedata.normalize('NFD', cisty_nazov) if unicodedata.category(c) != 'Mn')
                skratka_firmy = re.sub(r'[^a-zA-Z0-9]', '', cisty_nazov)[:6].upper()
                if not skratka_firmy:
                    skratka_firmy = "KLIENT"

                hladany_prefix = f"OBJP-{skratka_firmy}-{rok}-"
                
                vsetky_cisla = PrijataObjednavka.objects.filter(
                    cislo_objednavky_interne__startswith=hladany_prefix
                ).values_list('cislo_objednavky_interne', flat=True)
                
                max_cislo = 0
                for c_str in vsetky_cisla:
                    try:
                        cislo = int(c_str.split('-')[-1])
                        if cislo > max_cislo:
                            max_cislo = cislo
                    except (ValueError, IndexError):
                        pass
                        
                uspesne_ulozene = False
                for pokus in range(10):
                    nove_cislo = max_cislo + 1 + pokus
                    self.cislo_objednavky_interne = f"{hladany_prefix}{nove_cislo:03d}"
                    try:
                        with transaction.atomic():
                            super().save(*args, **kwargs)
                            uspesne_ulozene = True
                            break
                    except IntegrityError:
                        continue
                if not uspesne_ulozene:
                    raise ValidationError("🚨 Systém je momentálne vyťažený. Nepodarilo sa vygenerovať unikátne interné číslo prijatej objednávky. Skúste znova.")
        else:
            super().save(*args, **kwargs)

        if not self.zdrojove_ponuky.exists() and not self.navrhnute_ponuky.exists():
            from Evidencia.tasks import task_vyhodnot_smart_matcher
            def _safe_smart_matcher_obj(doc_pk):
                try:
                    task_vyhodnot_smart_matcher.delay(doc_pk)
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre Smart Matcher objednávky {doc_pk}: {exc}")

            transaction.on_commit(lambda pk=self.pk: _safe_smart_matcher_obj(pk))

        # 🚀 ISAS WEBHOOK TRIGGER (Odpálenie do Celery pri zmene stavu objednávky)
        if stav_zmeneny and self.zakaznik_id:
            from WebBuilder.tasks import process_objednavka_webhooks_task
            def _safe_obj_webhook(doc_pk):
                try:
                    process_objednavka_webhooks_task.delay(doc_pk)
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre webhook objednávky {doc_pk}: {exc}")

            transaction.on_commit(lambda pk=self.pk: _safe_obj_webhook(pk))

        # 🔓 UVOĽNENIE TERMÍNU A KALENDÁRA PRI ZAMIETNUTÍ OBJEDNÁVKY (cez on_commit kvôli Admin save_m2m)
        if stary_stav != 'ZAMIETNUTA' and self.stav == 'ZAMIETNUTA' and self.pk:
            def _uvolni_ulohy_zamietnutej_obj(obj_pk):
                try:
                    obj = PrijataObjednavka.objects.filter(pk=obj_pk, stav='ZAMIETNUTA').first()
                    if not obj:
                        return
                    for u in list(obj.vytvorene_ulohy.filter(dokoncene=False)):
                        ma_inu_aktivnu = u.zdrojove_objednavky_ulohy.exclude(pk=obj.pk).exclude(stav='ZAMIETNUTA').exists()
                        ma_ponuku = u.zdrojove_ponuky_ulohy.exists()
                        ma_zaznamy = u.servisne_zaznamy.exists()
                        if not ma_inu_aktivnu and not ma_ponuku and not ma_zaznamy:
                            try:
                                with transaction.atomic():
                                    obj.vytvorene_ulohy.remove(u)
                                    u.delete()
                            except Exception:
                                pass
                except Exception:
                    pass

            transaction.on_commit(lambda pk=self.pk: _uvolni_ulohy_zamietnutej_obj(pk))

        # 🛡️ IDEMPOTENCIA: Synchronizácia in-memory stavu pre prípad opakovaného save() v rovnakom procese
        if not hasattr(self, '_povodny_stav') or not isinstance(self._povodny_stav, dict):
            self._povodny_stav = {}
        self._povodny_stav['stav'] = self.stav

class VystavenaFaktura(ISASAuditMixin, ISASERPExportMixin, models.Model):
    PEPPOL_STATUS_CHOICES = [
        ('NEW', 'Nezaslané'),
        ('PROCESSING', 'Odosiela sa...'),
        ('SENT', '✅ Odoslané do siete Peppol'),
        ('ERROR', '❌ Chyba validácie (XML/Sieť)'),
    ]
    peppol_status = models.CharField("🇪🇺 Stav eFaktúry", max_length=20, choices=PEPPOL_STATUS_CHOICES, default='NEW', help_text="🇪🇺 Aktuálny stav spracovania a doručenia eFaktúry v sieti PEPPOL.")
    peppol_transaction_id = models.CharField("🆔 Peppol ID", max_length=100, blank=True, null=True, help_text="🆔 Unikátne ID transakcie z Access Pointu pre audit eFaktúry.")
    peppol_odoslane_dna = models.DateTimeField("⏱️ Časová pečiatka prijatia (Audit)", blank=True, null=True, help_text="⏱️ Presný čas potvrdenia prijatia XML štátnym systémom pre zákonnú 10-ročnú archiváciu.")
    peppol_xml_hash = models.CharField("🔒 SHA-256 Hash (Neporušenosť)", max_length=64, blank=True, null=True, editable=False, help_text="🔒 Kryptografický odtlačok XML súboru garantujúci jeho neporušenosť pri audite.")
    peppol_error_log = models.TextField("🚨 Peppol Chybový log", blank=True, null=True, help_text="🚨 Detailný zápis chýb pri neúspešnej validácii alebo odoslaní eFaktúry.")
    peppol_xml_dokument = models.FileField(upload_to="peppol_xml/%Y/%m/", blank=True, null=True, verbose_name="🇪🇺 Peppol XML (Audit)", help_text="📄 Originálny zálohovaný XML súbor eFaktúry pre daňový audit.")

    TYP_DOKLADU_CHOICES = [
        ('FAKTURA', '🧾 Riadna faktúra'),
        ('ZALOHA', '⏳ Zálohová faktúra (Proforma)'),
        ('DOBROPIS', '↩️ Dobropis (Opravný doklad)'),
    ]

    typ_dokladu = models.CharField(
        verbose_name="📄 Typ dokladu", 
        max_length=20, 
        choices=TYP_DOKLADU_CHOICES, 
        default='FAKTURA',
        help_text="📑 Určuje účtovný druh dokladu (ostrá faktúra, proforma alebo opravný dobropis)."
    )

    VYSTAVENE_DPH_CHOICES = [
        ('UD', '🇸🇰 UD - Tuzemský predaj s DPH (Bežná faktúra)'),
        ('UDPH', '🇨🇿 UDPH - Tuzemský predaj s DPH (ČR)'),
        ('U', '🇪🇺 U - Tuzemský predaj / Umsatzsteuer (DE/AT)'),
        ('SP_K', '🇵🇱 SP_K - Tuzemský predaj (PL)'),
        ('VAT_OUT', '🌍 VAT OUT - Medzinárodný predaj'),
        ('UN', '🚫 UN - Neobsahuje DPH / Nie je predmetom dane'),
        ('UK', '🏗️ UK - Prenesenie daňovej povinnosti na odberateľa (§ 69)'),
        ('UDpdp', '🇨🇿 UDpdp - Prenesenie daňovej povinnosti (ČR)'),
        ('VAT_REVERSE', '🌍 Prenesenie daňovej povinnosti / Reverse charge'),
        ('UE', '🇪🇺 UE - Dodanie do iného štátu EÚ (Ide do Súhrnného výkazu)'),
        ('UZ', '🌍 UZ - Vývoz do tretích krajín (Mimo EÚ)'),
        ('U0', '0️⃣ U0 - Oslobodené od DPH (napr. poštové/finančné služby)'),
    ]

    kod_clenenia_dph = models.CharField(
        verbose_name="🏛️ Kód členenia DPH", 
        max_length=20, 
        choices=VYSTAVENE_DPH_CHOICES,
        default=get_default_kod_clenenia_vystavena, 
        help_text="🏛️ Kód členenia predaja pre správne zaradenie do Kontrolného výkazu DPH."
    )

    STAV_CHOICES = [
        ('DRAFT', '🏗️ Rozpracovaná (V Builderi)'),
        ('NACHYSTANA', '📦 Nachystaná (Uložená, neodoslaná)'),
        ('ODOSLANA', '📤 Odoslaná zákazníkovi'),
        ('ZAPLATENA', '✅ Zaplatená'),
        ('PO_SPLATNOSTI', '🚨 Po splatnosti'),
        ('STORNO', '❌ Stornovaná'),
    ]

    SPOSOB_UHRADY_CHOICES = [
        ('PREVOD', 'Bankový prevod 🏦'),
        ('HOTOVOST', 'Hotovosť 💰'),
        ('KARTA', 'Platobná karta 💳'),
    ]

    cislo_faktury = models.CharField("🔢 Číslo faktúry", max_length=50, unique=True, blank=True, null=True, help_text="🔢 Unikátne evidenčné číslo daňového dokladu vygenerované systémom.")
    
    # --- 🔗 RELAČNÉ PREPOJENIA (Navigácia v systéme) ---
    zakaznik = models.ForeignKey('Zakaznik', on_delete=models.PROTECT, related_name="vystavene_faktury", verbose_name="🏢 Zákazník", help_text="🏢 Odberateľ, ktorému je faktúra vystavená.")
    povodna_faktura = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name="dobropisy", verbose_name="↩️ Pôvodná faktúra (Pre Dobropis)", help_text="↩️ Odkaz na pôvodnú faktúru vyžadovaný legislatívou pri dobropisoch.")
    vystavil = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, limit_choices_to={'is_staff': True}, verbose_name="👤 Vystavil", help_text="👤 Používateľ, ktorý faktúru v systéme vyhotovil.")
    
    # Tieto polia v admine skryjeme, ale slúžia na analytiku a import dát z Buildera
    zdrojova_uloha = models.ForeignKey('Uloha', on_delete=models.SET_NULL, null=True, blank=True, related_name="faktury", verbose_name="📋 Zdrojová úloha", help_text="📋 Prepojenie na servisnú úlohu pre sledovanie ziskovosti výjazdu.")
    zdrojove_ponuky: models.ManyToManyField = models.ManyToManyField('CenovaPonuka', blank=True, related_name="vyfakturovane", verbose_name="📄 Zdrojové cenové ponuky", help_text="💰 Cenové ponuky spárované s touto faktúrou.")
    zdrojove_objednavky: models.ManyToManyField = models.ManyToManyField('PrijataObjednavka', blank=True, related_name="faktury", verbose_name="📥 Zdrojové prijaté objednávky", help_text="📥 Prijaté objednávky od klienta zlúčené do tejto faktúry.")
    zdrojove_zaznamy: models.ManyToManyField = models.ManyToManyField('ServisnyZaznam', blank=True, verbose_name="📑 Zdrojové servisné záznamy", help_text="📑 Servisné protokoly, z ktorých boli natiahnuté odpracované hodiny a diely.")
    zariadenie = models.ForeignKey('ServisovaneZariadenie', on_delete=models.SET_NULL, null=True, blank=True, related_name="faktury", verbose_name="⚙️ Zariadenie", help_text="⚙️ Hlavné servisované zariadenie týkajúce sa tejto faktúry.")
    typ_servisu: models.ManyToManyField = models.ManyToManyField('TypServisuPrace', blank=True, verbose_name="🛠️ Typ servisu / prác", help_text="🛠️ Kategórie vykonaných servisných prác na faktúre.")

    # 🔒 ÚČTOVNÝ SNAPSHOT PRE B2B (Nemenné dáta)
    cislo_objednavky_klienta = models.CharField(
        "🏷️ Číslo objednávky klienta (PO Number)", max_length=255, blank=True, null=True,
        help_text="🏷️ Externé číslo objednávky zákazníka požadované na PDF doklade."
    )

    # =========================================================
    # 🔒 ÚČTOVNÝ SNAPSHOT (NEMENNÉ DÁTA V ČASE VYSTAVENIA)
    # =========================================================
    # 1. Dodávateľ (Naša firma)
    dodavatel_nazov = models.CharField("🏢 Náš názov", max_length=255, blank=True, null=True, editable=False, help_text="🏢 Názov vystavovateľa v čase vystavenia (nemenný snapshot).")
    dodavatel_sidlo = models.TextField("📍 Naše sídlo", blank=True, null=True, editable=False, help_text="📍 Adresa sídla vystavovateľa pri vystavení dokladu.")
    dodavatel_ico = models.CharField("🆔 Naše IČO", max_length=20, blank=True, null=True, editable=False, help_text="🆔 IČO vystavovateľa v čase vystavenia.")
    dodavatel_dic = models.CharField("🧾 Naše DIČ", max_length=20, blank=True, null=True, editable=False, help_text="🧾 DIČ vystavovateľa v čase vystavenia.")
    dodavatel_ic_dph = models.CharField("🏛️ Naše IČ DPH", max_length=20, blank=True, null=True, editable=False, help_text="🏛️ IČ DPH vystavovateľa v čase vystavenia.")
    dodavatel_iban = models.CharField("🏦 Náš IBAN", max_length=34, blank=True, null=True, editable=False, help_text="🏦 Bankový účet dodávateľa určený na úhradu dokladu.")
    dodavatel_swift = models.CharField("🔀 Náš SWIFT", max_length=11, blank=True, null=True, editable=False, help_text="🔀 SWIFT/BIC kód banky dodávateľa.")
    dodavatel_banka = models.CharField("🏦 Naša banka", max_length=100, blank=True, null=True, editable=False, help_text="🏦 Názov bankového ústavu dodávateľa.")
    dodavatel_zapis = models.TextField("📜 Zápis v registri", blank=True, null=True, editable=False, help_text="📜 Registračný zápis vystavovateľa na faktúre.")
    vystavil_meno = models.CharField("✍️ Meno vystavovateľa", max_length=150, blank=True, null=True, editable=False, help_text="✍️ Meno a priezvisko osoby, ktorá doklad vyhotovila.")
    
    # 2. Odberateľ (Zákazník)
    odberatel_nazov = models.CharField("🏢 Názov zákazníka", max_length=255, blank=True, null=True, editable=False, help_text="🏢 Oficiálny názov odberateľa v čase vystavenia dokladu.")
    odberatel_ulica = models.CharField("📍 Ulica zákazníka", max_length=255, blank=True, null=True, editable=False, help_text="📍 Ulica a číslo odberateľa v čase vystavenia.")
    odberatel_mesto = models.CharField("🏙️ Mesto zákazníka", max_length=255, blank=True, null=True, editable=False, help_text="🏙️ Mesto odberateľa v čase vystavenia.")
    odberatel_psc = models.CharField("📮 PSČ zákazníka", max_length=20, blank=True, null=True, editable=False, help_text="📮 PSČ odberateľa v čase vystavenia.")
    odberatel_stat = models.CharField("🌍 Štát zákazníka", max_length=100, blank=True, null=True, editable=False, help_text="🌍 Štát odberateľa v čase vystavenia.")
    odberatel_sidlo = models.TextField("📍 Sídlo zákazníka", blank=True, null=True, editable=False, help_text="📍 Kompletná adresa sídla odberateľa na doklade.")
    odberatel_ico = models.CharField("🆔 IČO zákazníka", max_length=20, blank=True, null=True, editable=False, help_text="🆔 IČO odberateľa v čase vystavenia.")
    odberatel_dic = models.CharField("🧾 DIČ zákazníka", max_length=20, blank=True, null=True, editable=False, help_text="🧾 DIČ odberateľa v čase vystavenia.")
    odberatel_ic_dph = models.CharField("🏛️ IČ DPH zákazníka", max_length=20, blank=True, null=True, editable=False, help_text="🏛️ IČ DPH odberateľa v čase vystavenia.")
    # =========================================================

    stav = models.CharField("🚥 Stav faktúry", max_length=20, choices=STAV_CHOICES, default='DRAFT', help_text="🚦 Aktuálny životný cyklus dokladu od rozpracovania po úhradu.")
    
    # --- PLATOBNÉ ÚDAJE ---
    sposob_uhrady = models.CharField("💳 Spôsob úhrady", max_length=20, choices=SPOSOB_UHRADY_CHOICES, default='PREVOD', help_text="💳 Dohodnutá forma platby odberateľom.")
    variabilny_symbol = models.CharField("🔢 Variabilný symbol", max_length=20, blank=True, null=True, help_text="🔢 Číselný identifikátor platby pre bankové párovanie.")
    konstantny_symbol = models.CharField("📌 Konštantný symbol", max_length=4, default='0308', help_text="📌 Štandardný bankový symbol pre platobný styk (napr. 0308 pre služby).")
    mena = models.CharField("💱 Mena", max_length=10, blank=True, default="", help_text="💱 Mena, v ktorej je doklad vystavený.")
    
    # --- DÁTUMY ---
    datum_vystavenia = models.DateField("📅 Dátum vystavenia", default=timezone.now, help_text="📅 Dátum vyhotovenia faktúry.")
    datum_dodania = models.DateField("📦 Dátum dodania (Daňová povinnosť)", default=timezone.now, help_text="📦 Dátum vzniku daňovej povinnosti pre DPH.")
    datum_splatnosti = models.DateField("⏳ Dátum splatnosti", null=True, blank=True, help_text="⏳ Krajný termín na úhradu faktúry.")
    datum_uhrady = models.DateField("✅ Dátum úhrady", null=True, blank=True, help_text="✅ Reálny dátum pripísania platby na účet.")

    # --- FINANČNÉ SUMÁRE ---
    prenos_danovej_povinnosti = models.BooleanField("🔄 Prenesenie daňovej povinnosti (Reverse charge)", default=False, help_text="⚖️ Režim prenesenia daňovej povinnosti na odberateľa (Reverse charge).")
    # --- ODPIS PRI PREDAJI Z PULTU ---
    zdrojovy_sklad_id = models.CharField(
        "🏢 Zdrojový sklad pre výdajku", 
        max_length=50, 
        blank=True, 
        null=True, 
        help_text="🏭 Určuje sklad, z ktorého sa pri schválení odpíše materiál."
    )

    odpisat_zo_skladu = models.BooleanField(
        "📦 Vytvoriť Výdajku (Odpísať zo skladu)", 
        default=False,
        help_text="📦 Aktivuje automatický odpis tovaru zo skladu pri schválení."
    )
    notif_odpis_tg = models.BooleanField("📲 Notif. o odpise skladu (Telegram)", default=True, help_text="📱 Posielať notifikáciu o odpise materiálu do Telegramu.")
    notif_odpis_mail = models.BooleanField("✉️ Notif. o odpise skladu (E-mail)", default=True, help_text="📧 Posielať notifikáciu o odpise materiálu e-mailom.")
    notif_odpis_discord = models.BooleanField("🎮 Notif. o odpise skladu (Discord)", default=True, help_text="💬 Posielať notifikáciu o odpise materiálu do Discordu.")

    notif_majitel_tg = models.BooleanField("📲 Notifikácia majiteľovi (Telegram)", default=True, help_text="📱 Upozorniť majiteľa v Telegrame pri vystavení faktúry.")
    notif_majitel_mail = models.BooleanField("✉️ Notifikácia majiteľovi (E-mail)", default=True, help_text="📧 Upozorniť majiteľa e-mailom pri vystavení faktúry.")
    notif_majitel_discord = models.BooleanField("🎮 Notifikácia majiteľovi (Discord)", default=True, help_text="💬 Upozorniť majiteľa na Discorde pri vystavení faktúry.")

    celkom_bez_dph = models.DecimalField("💰 Celkom bez DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="💰 Suma celkom bez DPH vypočítana systémom.")
    celkom_dph = models.DecimalField("🏛️ Suma DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="🏛️ Vypočítaná výška DPH pre celý doklad.")
    celkom_s_dph = models.DecimalField("💰 Celkom na úhradu", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="💰 Konečná suma faktúry vrátane DPH na úhradu.")

    # --- 🤖 AI & VÝSTUP ---
    ai_poznamka = models.TextField("🤖 AI Finančný Audítor", blank=True, null=True, help_text="🤖 Automatické poznámky a audity vygenerované AI.")
    poznamka_pre_klienta = models.TextField("📝 Poznámka na faktúre", blank=True, null=True, help_text="📝 Text zobrazený na vytlačenej faktúre pre zákazníka.")
    pdf_dokument = models.FileField(upload_to="vystavene_faktury_pdf/%Y/%m/", blank=True, null=True, verbose_name="📄 PDF Dokument", help_text="📄 Vygenerovaný vizuálny PDF súbor faktúry.")
    bezpecnostny_kluc = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name="🔑 Bezpečnostný kľúč", help_text="🔑 Unikátny kľúč pre bezpečný prístup klienta k faktúre.")

    class Meta:
        verbose_name = "📄 Vystavená faktúra"
        verbose_name_plural = "💰 Vystavené faktúry"
        ordering = ['-datum_vystavenia', '-id']

    def __str__(self):
        cislo = self.cislo_faktury if self.cislo_faktury else f"DRAFT-#{self.id}"
        
        # 🧠 ISAS SMART EXTRACT: Zozbierame unikátne NÁZVY strojov (Ochrana pred prázdnymi poliami)
        zariadenia_nazvy = set()
        
        def pridaj_stroj(zar):
            if zar:
                # Fallback: Ak chýba model, skúsime interný názov, inak str()
                nazov = getattr(zar, 'model_zariadenia', '') or getattr(zar, 'nazov', '') or str(zar)
                if nazov and str(nazov).strip():
                    zariadenia_nazvy.add(str(nazov).strip())

        # Pridáme hlavný stroj z hlavičky
        pridaj_stroj(self.zariadenie)
        
        # Pridáme všetky stroje z položiek buildera (ak už je faktúra uložená v DB)
        if self.pk:
            for polozka in self.polozky.all():
                pridaj_stroj(polozka.zariadenie)
                
        zariadenie_str = f" | ⚙️ {', '.join(zariadenia_nazvy)}" if zariadenia_nazvy else ""
        
        mena_str = f" {self.mena}".rstrip() if getattr(self, 'mena', None) else ""
        return f"{cislo} | {self.zakaznik}{zariadenie_str} | {self.celkom_s_dph}{mena_str}"

    def clean(self):
        super().clean()
        
        # 🔒 0. ZÁKONNÁ OCHRANA: Uzamknutie odoslanej eFaktúry
        if self.pk:
            stara_faktura = VystavenaFaktura.objects.filter(pk=self.pk).first()
            if stara_faktura and stara_faktura.peppol_status in ['SENT', 'PROCESSING']:
                # 🛡️ ISAS ARCHITECT FIX: Povolíme prvotné vygenerovanie a pripojenie XML súboru
                xml_zmenene = bool(stara_faktura.peppol_xml_dokument) and self.peppol_xml_dokument != stara_faktura.peppol_xml_dokument
                if self.celkom_s_dph != stara_faktura.celkom_s_dph or xml_zmenene:
                    raise ValidationError("🛑 ZÁKONNÁ OCHRANA: Faktúra už bola prijatá v sieti Peppol. Finančné údaje a XML súbor sú kryptograficky uzamknuté a nesmú sa meniť.")

        # 🛡️ 1. Prísna kontrola súm podľa typu dokladu
        if self.typ_dokladu == 'DOBROPIS':
            if self.celkom_s_dph is not None and self.celkom_s_dph > Decimal('0.00'):
                raise ValidationError("📉 Dobropis (opravný doklad) musí mať zápornú (alebo nulovú) sumu, keďže vraciate peniaze.")
            # 🛡️ DEFENSIVE: Kontrolujeme priamo ID stĺpec, čím predídeme zlyhaniu na ObjectDoesNotExist
            if not self.povodna_faktura_id:
                raise ValidationError({'povodna_faktura': "🔗 Pre Dobropis musíte vybrať 'Pôvodnú faktúru'. Vyžaduje to legislatíva eFaktúry (Peppol)."})
            if self.pk and self.povodna_faktura_id == self.pk:
                raise ValidationError({'povodna_faktura': "🔄 Dobropis nemôže odkazovať sám na seba ako na pôvodnú faktúru."})
            if self.povodna_faktura_id:
                try:
                    if self.povodna_faktura:
                        if self.povodna_faktura.typ_dokladu == 'DOBROPIS':
                            raise ValidationError({'povodna_faktura': "🛑 Pôvodnou faktúrou pre dobropis nemôže byť iný dobropis."})
                        if self.zakaznik_id and self.povodna_faktura.zakaznik_id != self.zakaznik_id:
                            raise ValidationError({'povodna_faktura': "🛑 Pôvodná faktúra musí patriť rovnakému zákazníkovi."})
                except ObjectDoesNotExist:
                    pass
            
        if self.typ_dokladu in ['FAKTURA', 'ZALOHA'] and self.celkom_s_dph is not None and self.celkom_s_dph < Decimal('0.00'):
            raise ValidationError("📈 Riadna a Zálohová faktúra nesmú mať zápornú sumu. Pre vrátenie peňazí zvoľte typ dokladu 'Dobropis'.")

        # 🛡️ 2. Zálohová faktúra a Dobropis nesmú odpisovať sklad
        if self.typ_dokladu in ['ZALOHA', 'DOBROPIS'] and self.odpisat_zo_skladu:
            raise ValidationError(f"📦 {self.get_typ_dokladu_display()} nesmie meniť stav skladu. Vypnite možnosť 'Povoliť fyzický odpis'.")
        
        if self.datum_splatnosti and self.datum_vystavenia:
            d_splatnosti = to_date(self.datum_splatnosti)
            d_vystavenia = to_date(self.datum_vystavenia)
            if d_splatnosti and d_vystavenia and d_splatnosti < d_vystavenia:
                # 🛡️ ISAS FIX: Presné namapovanie na formulárové pole pre vizuálne zvýraznenie v UI
                raise ValidationError({'datum_splatnosti': "⏳ Dátum splatnosti nemôže byť skorší ako dátum vystavenia."})

        # 🛑 3. LEGISLATÍVNA OCHRANA: Medzinárodná lehota na vystavenie faktúry (Smernica 2006/112/ES / národné právo)
        prefix_varovania = "⚠️ AUDIT: Prekročená zákonná lehota na vystavenie dokladu."
        riadky_poznamky = [r for r in (self.ai_poznamka or "").split("\n") if not r.strip().startswith(prefix_varovania)]

        d_vystavenia = to_date(self.datum_vystavenia)
        d_dodania = to_date(self.datum_dodania)
        if self.typ_dokladu == 'FAKTURA' and d_dodania and d_vystavenia:

            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            identita_obj = FiremnaIdentitaModel.objects.first()
            stat_firmy = getattr(identita_obj, 'sidlo_stat', None)
            
            hranicny_termin = FiremnaIdentitaModel.vypocitaj_danovy_deadline(
                datum_dodania=d_dodania,
                typ_dokladu=self.typ_dokladu,
                stat_nazov=stat_firmy,
                kod_clenenia_dph=self.kod_clenenia_dph
            )
            
            if hranicny_termin and d_vystavenia > hranicny_termin:
                varovanie = f"{prefix_varovania} Pre dátum dodania {d_dodania.strftime('%d.%m.%Y')} bol hraničný termín {hranicny_termin.strftime('%d.%m.%Y')}."
                riadky_poznamky.append(varovanie)
        
        nova_poznamka = "\n".join([r.strip() for r in riadky_poznamky if r.strip()])
        self.ai_poznamka = nova_poznamka if nova_poznamka else None

    @transaction.atomic
    def delete(self, *args, **kwargs):
        # 🛑 ZÁKONNÁ OCHRANA: Absolútny zákaz výmazu odoslanej eFaktúry a exportov do ERP (Fail-Fast)
        if self.peppol_status in ['SENT', 'PROCESSING']:
            raise PermissionDenied("🛑 ZÁKONNÁ OCHRANA: Faktúru odoslanú (alebo práve odosielanú) do štátneho systému eFaktúra je prísne zakázané vymazať. Musí byť archivovaná 10 rokov.")
        if self.erp_export_status in ['EXPORTED', 'PROCESSING']:
            raise PermissionDenied("🛑 ZÁKONNÁ OCHRANA: Záznam, ktorý je exportovaný (alebo sa práve exportuje) do účtovníctva, je prísne zakázané vymazať.")
        # 🛡️ ISAS ARCHITECT FIX: Modelová ochrana pred zmazaním faktúry s existujúcimi platbami
        if hasattr(self, 'uhrady') and self.uhrady.exists():
            raise PermissionDenied("🛑 ZÁKONNÁ OCHRANA: Faktúru s evidovanými úhradami nie je možné vymazať. Najprv stornujte prijaté platby.")

        # Ak faktúra odpísala tovar, pri jej zmazaní ho musíme vrátiť!
        if self.odpisat_zo_skladu and self.stav not in ['DRAFT']:
            StockMovement = apps.get_model('Evidencia', 'StockMovement')

            polozky_na_vratenie = self.polozky.filter(typ_polozky='MAT', content_type__isnull=False, object_id__isnull=False)
            
            # 🛡️ DETERMINISTICKÉ UZAMYKANIE (Absolútna ochrana pred Deadlockom)
            ct_to_ids = defaultdict(list)
            for pol in polozky_na_vratenie:
                ct_to_ids[pol.content_type_id].append(pol.object_id)
                
            uzamknute_diely_mapa = {}
            for ct_id in sorted(ct_to_ids.keys()):
                ct = ContentType.objects.get_for_id(ct_id)
                ModelTrieda = ct.model_class()
                if not ModelTrieda or not hasattr(ModelTrieda, 'pocet_na_sklade'):
                    continue
                
                # Zoradíme IDčka, aby sme zamykali riadky vždy zhora nadol
                zoradene_ids = sorted(list(set(ct_to_ids[ct_id])))
                uzamknute_objekty = ModelTrieda.objects.select_for_update().filter(pk__in=zoradene_ids).order_by('pk')
                for obj in uzamknute_objekty:
                    uzamknute_diely_mapa[(ct_id, obj.pk)] = obj

            # 🛡️ Samotné vrátenie tovaru z bezpečne uzamknutých dát
            pohyby_na_zapis = []
            diely_na_update = defaultdict(dict)
            
            filialka_obj = None
            cache_stavov_filialky = {}
            if self.zdrojovy_sklad_id:
                ciste_filialka_id = str(self.zdrojovy_sklad_id).replace('FILIALKA_', '').strip()
                if ciste_filialka_id.isdigit():
                    # 🔒 PESIMISTICKÝ ZÁMOK: Pri kompenzácii pohybov zamkneme filiálku
                    filialka_obj = Filialka.objects.select_for_update().filter(id=int(ciste_filialka_id)).first()
                    if filialka_obj:
                        cache_stavov_filialky = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(filialka_obj)

            for pol in polozky_na_vratenie:
                diel = uzamknute_diely_mapa.get((pol.content_type_id, pol.object_id))
                if diel:
                    mnozstvo_dec = pol.mnozstvo if pol.mnozstvo is not None else Decimal('0.00')
                    mnozstvo = int(mnozstvo_dec.to_integral_value(rounding=ROUND_HALF_UP))
                    if mnozstvo == 0:
                        continue

                    cisty_nazov = ocisti_nazov_od_jednotky(str(diel))

                    # Fyzický stav na modeli vraciame LEN VTEDY, ak sa pôvodne odpisovalo z Hlavného skladu
                    if not filialka_obj:
                        aktualny_stav = diel.pocet_na_sklade if diel.pocet_na_sklade is not None else 0
                        novy_stav = aktualny_stav + mnozstvo
                        if novy_stav < 0:
                            raise PermissionDenied(
                                f"🛑 Nie je možné zmazať doklad. Skladová zásoba položky '{cisty_nazov}' "
                                f"by klesla do záporných hodnôt ({novy_stav} {pol.merna_jednotka})."
                            )
                        diel._povodny_pocet = aktualny_stav
                        diel.pocet_na_sklade = novy_stav 
                        diely_na_update[type(diel)][diel.pk] = diel
                    else:
                        if mnozstvo < 0:
                            stav_na_filialke = cache_stavov_filialky.get(cisty_nazov, 0)
                            if stav_na_filialke < abs(mnozstvo):
                                raise PermissionDenied(
                                    f"🛑 Nie je možné zmazať doklad. Na filiálke {filialka_obj.nazov} "
                                    f"nie je dostatok zásob pre položku '{cisty_nazov}' (dostupné: {stav_na_filialke}, potrebné: {abs(mnozstvo)})."
                                )
                            cache_stavov_filialky[cisty_nazov] = stav_na_filialke + mnozstvo

                    akcia = StockMovement.ADD if mnozstvo > 0 else StockMovement.REMOVE

                    typ_erp = 'PRIJEMKA' if akcia == StockMovement.ADD else 'VYDAJKA'
                    pohyby_na_zapis.append(StockMovement(
                        filialka=filialka_obj,
                        user=self.vystavil, 
                        category="Fakturácia", 
                        subcategory="Zmazanie faktúry (Storno pohybu)",
                        product_name=cisty_nazov,
                        action=akcia, 
                        count=abs(mnozstvo),
                        merna_jednotka=pol.merna_jednotka,
                        typ_dokladu_erp=typ_erp
                    ))
            
            for model_class, diely_dict in diely_na_update.items():
                diely_list = list(diely_dict.values())
                model_class.objects.bulk_update(diely_list, ['pocet_na_sklade'])
                for d in diely_list:
                    kontrola_stavu_zasob(sender=model_class, instance=d, created=False)

            if pohyby_na_zapis:
                StockMovement.objects.bulk_create(pohyby_na_zapis)
                        
        # Až po vrátení tovaru faktúru fyzicky zmažeme
        super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentitaModel.objects.first()
        country_defaults = FiremnaIdentitaModel.get_country_defaults() if (FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'get_country_defaults')) else {}
        if not self.mena:
            if self.typ_dokladu == 'DOBROPIS' and self.povodna_faktura_id:
                try:
                    if self.povodna_faktura and getattr(self.povodna_faktura, 'mena', None):
                        self.mena = self.povodna_faktura.mena
                except ObjectDoesNotExist:
                    pass
            if not self.mena:
                self.mena = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()

        # Dynamická daňová lokalizácia bez hardkódu (riadená DB konfiguráciou alebo medzinárodným štandardom)
        override_prenos = country_defaults.get('kod_dph_prenos')
        override_vystavena = country_defaults.get('kod_dph_vystavena')
        kod_prenos = override_prenos or 'VAT_REVERSE'
        kod_vystavena = override_vystavena or 'VAT_OUT'

        if self.prenos_danovej_povinnosti:
            # Ak je zapnutý prenos a faktúra má neutrálny alebo DB kód bežného predaja, prepneme na prenos
            if not self.kod_clenenia_dph or self.kod_clenenia_dph in ['VAT_OUT', 'VAT_REVERSE', override_vystavena]:
                self.kod_clenenia_dph = kod_prenos
        elif self.kod_clenenia_dph in [kod_prenos, 'VAT_REVERSE']:
            # Ak používateľ odškrtol prenos, vrátime späť bežný predaj
            self.kod_clenenia_dph = kod_vystavena

        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'mena', 'kod_clenenia_dph'}

        # 🛑 ZÁKONNÁ OCHRANA (Hard Lock na úrovni ORM)
        if self.pk:
            try:
                stara_faktura = VystavenaFaktura.objects.get(pk=self.pk)
                
                # 1. OCHRANA POČAS ODOSIELANIA AJ PO ODOSLANÍ (Web nesmie manipulovať so sumou ani vrátiť doklad do DRAFTu)
                if stara_faktura.peppol_status in ['SENT', 'PROCESSING']:
                    if self.celkom_s_dph != stara_faktura.celkom_s_dph or self.typ_dokladu != stara_faktura.typ_dokladu:
                        raise PermissionDenied("🛑 FATAL: Finančné dáta a typ dokladu eFaktúry sú počas spracovania a po odoslaní štátu nemeniteľné.")
                    if self.peppol_status == 'NEW':
                        raise PermissionDenied("🛑 FATAL: Odosielaná alebo odoslaná eFaktúra nesmie byť vrátená do stavu DRAFT/NEW.")

                # 2. OCHRANA KRYPTOGRAFICKÉHO XML HASHU (Nemeniteľný výhradne po úspešnom doručení štátu - SENT)
                if stara_faktura.peppol_status == 'SENT':
                    if stara_faktura.peppol_xml_hash is not None and self.peppol_xml_hash != stara_faktura.peppol_xml_hash:
                        raise PermissionDenied("🛑 FATAL: XML hash faktúry odoslanej do štátneho systému je zapečatený a nesmie sa meniť.")

            except VystavenaFaktura.DoesNotExist:
                pass

        super().save(*args, **kwargs)

    @transaction.atomic
    def schvalit_a_vystavit(self, schvalil_user):
        """CRITICAL CORE: Atomický presun z Buildera do ostrej účtovnej prevádzky."""
        # 🛡️ ISAS ARCHITECT FIX: Garancia existencie PK pred použitím update_fields
        if not self.pk:
            self.save()

        # Úprava: Zohľadňujeme nové stavy
        if self.stav not in ['DRAFT', 'NACHYSTANA']:
            raise ValidationError("📝 Iba rozpracovaná faktúra môže byť schválená a vystavená.")

        # Ochrana proti dvojitému spusteniu!
        if self.cislo_faktury:
            return

        # 🛡️ DEFENSIVE: Tvrdá validácia dát dokladu a pravidiel typu dokladu PRED vystavením
        self.full_clean()

        # 1. 🧠 INTELIGENTNÉ ČÍSLOVANIE (Per-Customer izolácia)
        d_vystavenia = to_date(self.datum_vystavenia) or timezone.now().date()
        rok = d_vystavenia.strftime('%y')
        
        # Uzamkneme zákazníka pre bezpečné generovanie čísla faktúry
        zakaznik_locked = None
        if self.zakaznik_id:
            zakaznik_locked = Zakaznik.objects.select_for_update().get(pk=self.zakaznik_id)
            
        surovy_nazov = getattr(zakaznik_locked, 'Nazov_spolocnosti', None) if zakaznik_locked else None
        cisty_nazov = str(surovy_nazov or 'FIRMA')
        
        # Očistíme názov od právnych foriem a diakritiky (podpora celej EÚ a medzinárodných foriem)
        cisty_nazov = re.sub(r'(?i)\b(s\.r\.o\.|a\.s\.|spol\. s r\.o\.|inc\.|ltd\.|gmbh|kft|sp\. z o\.o\.|sp\. z oo|s\.a\.|s\.r\.l\.|b\.v\.|corp\.)\b', '', cisty_nazov).strip()
        cisty_nazov = ''.join(c for c in unicodedata.normalize('NFD', cisty_nazov) if unicodedata.category(c) != 'Mn')
        skratka_firmy = re.sub(r'[^a-zA-Z0-9]', '', cisty_nazov)[:6].upper()
        if not skratka_firmy: skratka_firmy = "KLIENT"

        # 🏛️ LEGISLATÍVNA SEPARÁCIA ČÍSELNÝCH RADÍ (Ochrana pred pokutami DÚ)
        if self.typ_dokladu == 'DOBROPIS':
            hladany_prefix = f"DOB-{skratka_firmy}-{rok}-"  # Dobropis (Opravný doklad)
        elif self.typ_dokladu == 'ZALOHA':
            hladany_prefix = f"ZAF-{skratka_firmy}-{rok}-"  # Zálohová/Proforma faktúra (Nedaňový doklad)
        else:
            hladany_prefix = f"FAV-{skratka_firmy}-{rok}-"  # Riadna vystavená faktúra
        
        max_pokusov = 10
        uspesne_ulozene = False
        
        for pokus in range(max_pokusov):
            # Extrémna optimalizácia pamäte. Neťaháme celé objekty, len čisté stringy!
            vsetky_cisla = VystavenaFaktura.objects.filter(cislo_faktury__startswith=hladany_prefix).values_list('cislo_faktury', flat=True)
            max_cislo = 0
            for cislo_str in vsetky_cisla:
                try:
                    cislo = int(cislo_str.split('-')[-1])
                    if cislo > max_cislo:
                        max_cislo = cislo
                except (ValueError, IndexError, AttributeError):
                    pass
                    
            nove_cislo = max_cislo + 1 + pokus
            navrhnute_cislo = f"{hladany_prefix}{nove_cislo:03d}"
            
            # 🏦 VARIABILNÝ SYMBOL (Banky na Slovensku a v ČR vyžadujú max. 10 číslic!)
            zid = (self.zakaznik_id or 9999) % 10000
            cislo_part = nove_cislo % 10000
            navrhnuty_vs = f"{rok}{zid:04d}{cislo_part:04d}"[:10]
            
            try:
                # 🛡️ VNORENÁ TRANSAKCIA (Savepoint): Ak zlyhá unique constraint, nezruší to celú metódu
                with transaction.atomic():
                    self.cislo_faktury = navrhnute_cislo
                    self.variabilny_symbol = navrhnuty_vs
                    self.stav = 'NACHYSTANA'
                    # Fyzicky si "rezervujeme" číslo v databáze okamžite
                    self.save(update_fields=['cislo_faktury', 'variabilny_symbol', 'stav'])
                    uspesne_ulozene = True
                    break # Úspech, vyskakujeme z cyklu
            except IntegrityError:
                # Číslo už niekto obsadil v inej transakcii v rovnakej milisekunde, ideme na ďalší pokus
                continue
                
        if not uspesne_ulozene:
            raise ValidationError("🚨 Systém je momentálne extrémne vyťažený. Nepodarilo sa vygenerovať unikátne číslo faktúry. Skúste to prosím znova.")

        # 2. 📸 ÚČTOVNÝ SNAPSHOT
        from ISASaplikacia.models import FiremnaIdentita
        identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentita.objects.first()

        if not self.datum_splatnosti:
            d_vyst = d_vystavenia
            splatnost_dni = 14
            if zakaznik_locked and getattr(zakaznik_locked, 'predvolena_splatnost_dni', None):
                splatnost_dni = zakaznik_locked.predvolena_splatnost_dni
            elif identita and getattr(identita, 'splatnost_faktur', None):
                splatnost_dni = identita.splatnost_faktur
            try:
                splatnost_dni = int(splatnost_dni)
            except (ValueError, TypeError):
                splatnost_dni = 14
            self.datum_splatnosti = d_vyst + timedelta(days=splatnost_dni)

        if schvalil_user:
            self.vystavil = schvalil_user
            self.vystavil_meno = schvalil_user.get_full_name() or schvalil_user.username
        elif self.vystavil:
            self.vystavil_meno = self.vystavil.get_full_name() or self.vystavil.username

        if identita:
            self.dodavatel_nazov = identita.nazov_spolocnosti
            cislo_sidla = f" {identita.sidlo_cislo}".strip() if getattr(identita, 'sidlo_cislo', None) else ""
            ulica_cislo = f"{identita.sidlo_ulica or ''} {cislo_sidla}".strip()
            psc_mesto = f"{identita.sidlo_psc or ''} {identita.sidlo_mesto or ''}".strip()
            self.dodavatel_sidlo = ", ".join([cast for cast in [ulica_cislo, psc_mesto, getattr(identita, 'sidlo_stat', None)] if cast])
            self.dodavatel_ico = identita.ico
            self.dodavatel_dic = identita.dic
            self.dodavatel_ic_dph = identita.ic_dph
            self.dodavatel_iban = identita.iban
            self.dodavatel_swift = identita.swift
            self.dodavatel_banka = identita.nazov_banky
            self.dodavatel_zapis = identita.zapis_v_registri
            # 🛡️ ISAS ARCHITECT FIX: Menová nezávislosť – zachováva menu používateľa, pri dobropise dedí menu pôvodnej faktúry
            country_defaults = identita.get_country_defaults() if hasattr(identita, 'get_country_defaults') else {}
            predvolena_mena_firmy = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or ''
            
            if self.typ_dokladu == 'DOBROPIS' and self.povodna_faktura_id and getattr(self.povodna_faktura, 'mena', None):
                self.mena = self.povodna_faktura.mena
            elif not self.mena:
                self.mena = predvolena_mena_firmy or get_firemna_mena()
            
            # Dynamická daňová lokalizácia bez hardkódu (riadená DB konfiguráciou alebo medzinárodným štandardom)
            override_prenos = country_defaults.get('kod_dph_prenos')
            override_vystavena = country_defaults.get('kod_dph_vystavena')
            kod_prenos = override_prenos or 'VAT_REVERSE'
            kod_vystavena = override_vystavena or 'VAT_OUT'

            if self.prenos_danovej_povinnosti:
                if not self.kod_clenenia_dph or self.kod_clenenia_dph in ['VAT_OUT', 'VAT_REVERSE', override_vystavena]:
                    self.kod_clenenia_dph = kod_prenos
            elif self.kod_clenenia_dph in [kod_prenos, 'VAT_REVERSE']:
                self.kod_clenenia_dph = kod_vystavena

        # Uloženie snapshotu zákazníka priamo pri schválení (Zabraňuje prázdnym poliam v DB)
        if zakaznik_locked:
            self.odberatel_nazov = getattr(zakaznik_locked, 'Nazov_spolocnosti', '') or getattr(zakaznik_locked, 'nazov_spolocnosti', '')
            # Oprava malých/veľkých písmen podľa reálneho modelu Zakaznik
            self.odberatel_ulica = getattr(zakaznik_locked, 'ulica_a_cislo', '') or getattr(zakaznik_locked, 'Ulica_a_cislo', '') or getattr(zakaznik_locked, 'ulica', '')
            self.odberatel_psc = getattr(zakaznik_locked, 'psc', '') or getattr(zakaznik_locked, 'PSC', '')
            self.odberatel_mesto = getattr(zakaznik_locked, 'mesto', '') or getattr(zakaznik_locked, 'Mesto', '')
            self.odberatel_stat = getattr(zakaznik_locked, 'stat', '') or getattr(zakaznik_locked, 'Stat', '')
            
            self.odberatel_ico = getattr(zakaznik_locked, 'Ico', '') or getattr(zakaznik_locked, 'ico', '')
            self.odberatel_dic = getattr(zakaznik_locked, 'Dic', '') or getattr(zakaznik_locked, 'dic', '')
            self.odberatel_ic_dph = getattr(zakaznik_locked, 'Ic_dph', '') or getattr(zakaznik_locked, 'ic_dph', '')
            
            adresa_diely = [p for p in [self.odberatel_ulica, f"{self.odberatel_psc} {self.odberatel_mesto}".strip(), self.odberatel_stat] if p]
            self.odberatel_sidlo = ", ".join(adresa_diely)

        # 3. 📦 BEZPEČNÝ ODPIS ZO SKLADU (S ochranou proti Race Condition a Deadlockom)
        if self.odpisat_zo_skladu:
            StockMovement = apps.get_model('Evidencia', 'StockMovement')

            # 🛡️ KROK 1: Zozbierame všetky materiálové položky a zoskupíme ich podľa tabuľky (ContentType)
            polozky_na_odpis = self.polozky.filter(typ_polozky='MAT', content_type__isnull=False, object_id__isnull=False)
            ct_to_ids = defaultdict(list)
            for pol in polozky_na_odpis:
                ct_to_ids[pol.content_type_id].append(pol.object_id)

            # 🛡️ KROK 2: Deterministické uzamykanie (Zabraňuje Deadlockom!)
            uzamknute_diely_mapa = {}
            for ct_id in sorted(ct_to_ids.keys()):
                ct = ContentType.objects.get_for_id(ct_id)
                ModelTrieda = ct.model_class()
                
                if not ModelTrieda or not hasattr(ModelTrieda, 'pocet_na_sklade'):
                    continue
                
                # Zoradíme IDčka, aby sme zamykali riadky vždy zhora nadol
                zoradene_ids = sorted(list(set(ct_to_ids[ct_id])))
                
                # Hromadný pesimistický zámok pre danú tabuľku
                uzamknute_objekty = ModelTrieda.objects.select_for_update().filter(pk__in=zoradene_ids).order_by('pk')
                for obj in uzamknute_objekty:
                    uzamknute_diely_mapa[(ct_id, obj.pk)] = obj

            # 🛡️ KROK 3: Samotný odpis z už bezpečne uzamknutých dát
            pohyby_na_zapis = []
            diely_na_update = defaultdict(dict)

            filialka_obj = None
            cache_stavov_filialky = {}
            if self.zdrojovy_sklad_id:
                ciste_filialka_id = str(self.zdrojovy_sklad_id).replace('FILIALKA_', '').strip()
                if ciste_filialka_id.isdigit():
                    # 🔒 PESIMISTICKÝ ZÁMOK: Zamkneme filiálku, aby iný proces nemohol meniť jej pohyby počas overovania
                    filialka_obj = Filialka.objects.select_for_update().filter(id=int(ciste_filialka_id)).first()
                    if filialka_obj:
                        # 🚀 O(1) PERFORMANCE: Načítame stav celej filiálky do pamäte jedným SQL dopytom
                        cache_stavov_filialky = LedgerKalkulacka.zisti_stav_vsetkych_poloziek(filialka_obj)

            for pol in polozky_na_odpis:
                uzamknuty_diel = uzamknute_diely_mapa.get((pol.content_type_id, pol.object_id))
                
                if uzamknuty_diel:
                    mnozstvo_dec = pol.mnozstvo if pol.mnozstvo is not None else Decimal('0.00')
                    odpis_mnozstvo = int(mnozstvo_dec.to_integral_value(rounding=ROUND_HALF_UP))
                    if odpis_mnozstvo == 0:
                        continue

                    cisty_nazov_do_ledgeru = ocisti_nazov_od_jednotky(str(uzamknuty_diel))

                    # 1. EXPEDÍCIA Z FILIÁLKY / POBOČKY (Čerpanie z Ledgeru, fyzický model Hlavného skladu ostáva nedotknutý)
                    if filialka_obj:
                        stav_na_filialke = cache_stavov_filialky.get(cisty_nazov_do_ledgeru, 0)
                        if odpis_mnozstvo > 0 and stav_na_filialke < odpis_mnozstvo:
                            raise ValidationError(
                                f"📦 Nedostatok zásob na filiálke {filialka_obj.nazov} pre položku: {cisty_nazov_do_ledgeru}. "
                                f"Dostupné: {stav_na_filialke} {pol.merna_jednotka}, požadované: {odpis_mnozstvo} {pol.merna_jednotka}."
                            )
                        # Aktualizujeme pamäťový stav pre prípad, že rovnaký diel je na faktúre viackrát
                        cache_stavov_filialky[cisty_nazov_do_ledgeru] = stav_na_filialke - odpis_mnozstvo

                    # 2. EXPEDÍCIA Z HLAVNÉHO SKLADU (Centrála - odpis z fyzického modelu)
                    else:
                        aktualny_stav_db = uzamknuty_diel.pocet_na_sklade if uzamknuty_diel.pocet_na_sklade is not None else 0
                        if odpis_mnozstvo > 0 and aktualny_stav_db < odpis_mnozstvo:
                            raise ValidationError(
                                f"📦 Nedostatok zásob na Hlavnom sklade pre položku: {cisty_nazov_do_ledgeru}. "
                                f"Dostupné: {aktualny_stav_db} {pol.merna_jednotka}, požadované: {odpis_mnozstvo} {pol.merna_jednotka}."
                            )

                        if not hasattr(uzamknuty_diel, '_povodny_pocet'):
                            uzamknuty_diel._povodny_pocet = aktualny_stav_db

                        # Aktualizujeme pamäťový stav inštancie (ak je diel na faktúre viackrát, ďalší riadok vidí už nový zostatok)
                        uzamknuty_diel.pocet_na_sklade = aktualny_stav_db - odpis_mnozstvo
                        diely_na_update[type(uzamknuty_diel)][uzamknuty_diel.pk] = uzamknuty_diel

                    akcia_pohybu = StockMovement.ADD if odpis_mnozstvo < 0 else StockMovement.REMOVE
                    typ_erp = 'PRIJEMKA' if odpis_mnozstvo < 0 else 'VYDAJKA'
                    subkategoria = "Vrátenie tovaru (Dobropis)" if self.typ_dokladu == 'DOBROPIS' else "Predaj na faktúru"

                    pohyby_na_zapis.append(StockMovement(
                        filialka=filialka_obj,
                        user=schvalil_user, 
                        category="Fakturácia", 
                        subcategory=subkategoria,
                        product_name=cisty_nazov_do_ledgeru,
                        action=akcia_pohybu, 
                        count=abs(odpis_mnozstvo),
                        merna_jednotka=pol.merna_jednotka,
                        typ_dokladu_erp=typ_erp
                    ))
            
            for model_class, diely_dict in diely_na_update.items():
                diely_list = list(diely_dict.values())
                model_class.objects.bulk_update(diely_list, ['pocet_na_sklade'])
                # 🛡️ ISAS ARCHITECT: bulk_update nespúšťa signály, musíme skontrolovať zásoby manuálne
                for d in diely_list:
                    kontrola_stavu_zasob(sender=model_class, instance=d, created=False)

            if pohyby_na_zapis:
                StockMovement.objects.bulk_create(pohyby_na_zapis)
                    
        self.save()

    def get_qr_code_base64(self):
        """Generuje natívny európsky SEPA QR kód pre rýchlu platbu (výhradne v mene EUR)."""
        # Dobropis nesmie mať QR kód (my vraciame peniaze klientovi, on neplatí nám!)
        # SEPA štandard navyše striktne zakazuje záporné sumy, bankové aplikácie by padli.
        if self.typ_dokladu == 'DOBROPIS':
            return None
            
        # 🛡️ ISAS ARCHITECT FIX: EPC QR kód podporuje IBA menu EUR. Pri iných menách alebo nevyplnenej mene sa nesmie vygenerovať!
        if str(getattr(self, 'mena', '') or '').strip().upper() != 'EUR':
            return None

        if not self.dodavatel_iban or not self.celkom_s_dph or self.celkom_s_dph <= Decimal('0.00'):
            return None
            
        try:
            iban_clean = str(self.dodavatel_iban).replace(" ", "").strip()
            suma_str = f"EUR{Decimal(str(self.celkom_s_dph)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
            vs_text = f"/VS{self.variabilny_symbol}/" if self.variabilny_symbol else "Uhrada faktury"
            nazov = "".join(c for c in (self.dodavatel_nazov or "ISAS") if c.isalnum() or c in " ._-")[:70]
            
            epc_lines = ["BCD", "002", "1", "SCT", "", nazov, iban_clean, suma_str, "", "", vs_text]
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data("\n".join(epc_lines))
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="#0f172a", back_color="white")
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('utf-8')}"
        except Exception:
            return None

class PolozkaVystavenejFaktury(ISASAuditMixin, models.Model):
    TYP_POLOZKY = [
        ('MAT', '📦 Materiál / Diel'),
        ('PRACA', '👨‍🔧 Práca / Služba'),
        ('DOPRAVA', '🚗 Doprava / Výjazd'),
        ('INE', '🔖 Iné'),
        ('ZLA', '📉 Zľava')
    ]

    faktura = models.ForeignKey(VystavenaFaktura, on_delete=models.CASCADE, related_name="polozky", verbose_name="🧾 Faktúra", help_text="🧾 Nadriadená faktúra, ku ktorej táto položka patrí a na ktorej sa zobrazí.")
    typ_polozky = models.CharField(max_length=10, choices=TYP_POLOZKY, default='MAT', verbose_name="🏷️ Typ položky", help_text="🏷️ Klasifikácia položky pre správne vykazovanie tržieb a automatickú účtovnú predkontáciu.")
    praca_naviac = models.BooleanField(default=False, verbose_name="➕ Extra (Mimo cenovej ponuky)", help_text="➕ Označenie práce alebo materiálu dodaných nad rámec pôvodnej cenovej ponuky.")
    
    nazov = models.CharField(max_length=500, verbose_name="📝 Názov položky (Na PDF)", help_text="📝 Popis tovaru alebo služby, ktorý bude vytlačený priamo na faktúre pre zákazníka.")
    mnozstvo = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('1.00'), verbose_name="⚖️ Množstvo", help_text="⚖️ Počet dodaných kusov, hodín práce alebo iných účtovaných jednotiek.")
    merna_jednotka = models.CharField(max_length=10, default='ks', verbose_name="📏 M.J.", help_text="📏 Merná jednotka množstva (napr. ks, hod, km alebo súbor).")

    jednotkova_cena_bez_dph = models.DecimalField("💰 Jednotková cena bez DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="💰 Základná predajná cena za jednu mernú jednotku bez DPH a pred uplatnením zľavy.")
    zlava_percento = models.DecimalField("📉 Zľava (%)", max_digits=5, decimal_places=2, default=Decimal('0.00'), help_text="📉 Percentuálna zľava poskytnutá zákazníkovi na túto konkrétnu položku.")
    sadzba_dph = models.DecimalField("🏛️ Sadzba DPH (%)", max_digits=5, decimal_places=2, default=get_default_dph, help_text="🏛️ Zákonná sadzba DPH v percentách platná pre tento druh tovaru alebo služby.")

    # 📸 ISAS SNAPSHOT: Kópia obchodných nastavení zo skladu
    obchodny_nazov = models.CharField(max_length=255, blank=True, null=True, editable=False)
    skryt_vyrobcu = models.BooleanField(default=False, editable=False)
    
    ucet_vynosov = models.CharField(
        "📊 Účet výnosov (Predkontácia)", 
        max_length=20, 
        blank=True, 
        null=True, 
        help_text="📊 Účtovný predkontačný účet výnosov pre export do ERP systémov (napr. 604 pre tovar, 602 pre služby)."
    )
    cena_po_zlave_bez_dph = models.DecimalField("💰 Cena po zľave bez DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), editable=False, help_text="💰 Vypočítaná celková suma položky bez DPH po zohľadnení zľavy.")
    hodnota_dph = models.DecimalField("🏛️ Hodnota DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), editable=False, help_text="🏛️ Vypočítaná výška DPH pre túto položku (pri prenose daňovej povinnosti je nulová).")
    spolu_s_dph = models.DecimalField("💰 Spolu s DPH", max_digits=10, decimal_places=2, default=Decimal('0.00'), editable=False, help_text="💰 Konečná celková suma položky vrátane DPH.")

    content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True, limit_choices_to=ziskaj_povolene_modely, verbose_name="🔗 Typ obsahu", help_text="🔗 Systémové prepojenie na typ naviazaného modulového objektu.")
    object_id = models.PositiveIntegerField(null=True, blank=True, verbose_name="🆔 ID objektu", help_text="🆔 Unikátne identifikačné číslo konkrétneho naviazaného objektu v systéme.")
    skladovy_diel = GenericForeignKey('content_type', 'object_id')
    zariadenie = models.ForeignKey('ServisovaneZariadenie', on_delete=models.SET_NULL, null=True, blank=True, related_name='fakturovane_polozky', verbose_name="Priradené zariadenie", help_text="🛠️ Servisované zariadenie, ku ktorému sa dodaný diel alebo vykonaná práca vzťahuje.")

    class Meta:
        verbose_name = "Položka vystavenej faktúry"
        verbose_name_plural = "🧾 Položky vystavenej faktúry"
        ordering = ['id'] 
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        return f"{self.nazov} ({self.mnozstvo} {self.merna_jednotka})"

    def save(self, *args, **kwargs):
        # 📸 ISAS SNAPSHOT LOGIC: Prekopírovanie dát zo skladu pri vytvorení položky
        if not self.pk and self.content_type_id and self.object_id:
            try:
                diel = self.skladovy_diel
            except ObjectDoesNotExist:
                diel = None
            if diel:
                self.obchodny_nazov = getattr(diel, 'obchodny_nazov', None)
                self.skryt_vyrobcu = getattr(diel, 'skryt_vyrobcu', False)

        mnozstvo = Decimal(str(self.mnozstvo)) if self.mnozstvo is not None else Decimal('0.00')
        cena = Decimal(str(self.jednotkova_cena_bez_dph)) if self.jednotkova_cena_bez_dph is not None else Decimal('0.00')
        zlava = Decimal(str(self.zlava_percento)) if self.zlava_percento is not None else Decimal('0.00')
        sadzba = Decimal(str(self.sadzba_dph)) if self.sadzba_dph is not None else Decimal('0.00')

        # 1. Výpočet ceny po zľave
        zaklad = (mnozstvo * cena * (Decimal('1.00') - (zlava / Decimal('100.00')))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.cena_po_zlave_bez_dph = zaklad

        # 2. Výpočet DPH
        if self.faktura and self.faktura.prenos_danovej_povinnosti:
            self.hodnota_dph = Decimal('0.00')
        else:
            self.hodnota_dph = (zaklad * (sadzba / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # 3. Spolu
        self.spolu_s_dph = self.cena_po_zlave_bez_dph + self.hodnota_dph

        # 4. Automatická predkontácia podľa typu (Ak nie je vyplnená ručne) - ARCHITECT FIX: Načítanie z globálnych nastavení
        if not self.ucet_vynosov:
            is_omega = False
            try:
                nastavenia = ERPExportNastavenia.get_settings()
                is_omega = getattr(nastavenia, 'erp_system', 'POHODA') == 'OMEGA'
                predkont_sluzby = '602000' if is_omega else (getattr(nastavenia, 'pohoda_predkontacia_sluzby', None) or '2')
                predkont_tovar = '604000' if is_omega else (getattr(nastavenia, 'pohoda_predkontacia_tovar', None) or '3')
            except Exception:
                predkont_sluzby = '602000' if is_omega else '2'
                predkont_tovar = '604000' if is_omega else '3'

            if self.typ_polozky in ['PRACA', 'DOPRAVA', 'INE', 'ZLA']: 
                self.ucet_vynosov = predkont_sluzby
            elif self.typ_polozky == 'MAT': 
                self.ucet_vynosov = predkont_tovar

        # 🛡️ DEFENSIVE: Ak ukladáme cez update_fields, zabezpečíme uloženie všetkých odvodených finančných polí
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {
                'cena_po_zlave_bez_dph', 'hodnota_dph', 'spolu_s_dph', 'ucet_vynosov', 'obchodny_nazov', 'skryt_vyrobcu'
            }

        super().save(*args, **kwargs)

class EvidenciaUhrad(ISASAuditMixin, ISASERPExportMixin, models.Model):
    """
    👻 Tento model predstavuje fyzický pohyb peňazí 🪙 (Bankový výpis 📜 alebo Pokladničný doklad 🎫).
    📤 Exportuje sa do ERP systému ⚙️ a spáruje 🔗 sa s konkrétnou faktúrou 🧾.
    """
    TYP_UHRADY_CHOICES = [
        ('BV', '🏦 Bankový výpis (Prevod 💸)'),
        ('PPD', '💵 Príjmový pokladničný doklad (Hotovosť dnu 📥)'),
        ('VPD', '💸 Výdavkový pokladničný doklad (Hotovosť von 📤)'),
        ('PK', '💳 Platba kartou cez terminál 📠'),
        ('VZ', '🔄 Vzájomný zápočet ⚖️'),
    ]

    # --- 1. 🆔 IDENTIFIKÁCIA DOKLADU 📋 ---
    typ_uhrady = models.CharField(
        "📑 Typ dokladu", max_length=5, choices=TYP_UHRADY_CHOICES, default='BV',
        help_text="🏷️ Typ finančného dokladu pre správne zaúčtovanie transakcie."
    )
    cislo_dokladu = models.CharField(
        "🔢 Číslo dokladu", max_length=50, blank=True, null=True,
        help_text="🔢 Identifikačné číslo dokladu z banky alebo účtovníctva (napr. BV-2024-001 alebo PPD-001)."
    )
    datum_uhrady = models.DateField(
        "📅 Dátum úhrady", default=timezone.now,
        help_text="📅 Skutočný dátum realizácie platby pre správne párovanie a kontrolu splatnosti."
    )

    # --- 2. 💰 FINANČNÉ ÚDAJE 📈 ---
    suma_uhrady = models.DecimalField(
        "💰 Suma úhrady", max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Prijatá alebo vyhradená finančná čiastka určená na úhradu faktúry."
    )
    mena = models.CharField(
        "💱 Mena", max_length=10, blank=True, default="",
        help_text="💱 Mena transakcie pre správny prepočet a účtovnú evidenciu."
    )

    # --- 3. 🔗 PREPOJENIE NA FAKTÚRY 🧾 (Spárovanie 🤝) ---
    vystavena_faktura = models.ForeignKey(
        'VystavenaFaktura', on_delete=models.CASCADE, related_name="uhrady",
        null=True, blank=True, verbose_name="📄 Uhradená Vystavená faktúra (Predaj 🤝)",
        limit_choices_to=models.Q(stav__in=['NACHYSTANA', 'ODOSLANA', 'PO_SPLATNOSTI', 'ZAPLATENA']),
        help_text="📄 Vystavená odberateľská faktúra, ku ktorej prislúcha táto prichádzajúca platba."
    )
    prijata_faktura = models.ForeignKey(
        'PrichodziaFaktura', on_delete=models.CASCADE, related_name="uhrady",
        null=True, blank=True, verbose_name="📥 Uhradená Prijatá faktúra (Nákup 🛒)",
        help_text="📥 Prijatá dodávateľská faktúra, na ktorú bola poukázaná táto odchádzajúca platba."
    )

    # --- 4. 🧮 ÚČTOVNÉ A BANKOVÉ 🏦 ÚDAJE 📊 ---
    protiucet_iban = models.CharField(
        "🏦 IBAN partnera", max_length=34, blank=True, null=True,
        help_text="🏦 IBAN účtu partnera pre overenie platcu a správny export do ERP."
    )
    poznamka = models.TextField(
        "📝 Poznámka / Popis transakcie", blank=True, null=True,
        help_text="📝 Interná poznámka alebo doplňujúci text z bankového výpisu pre internú kontrolu."
    )

    class Meta:
        verbose_name = "💳 Evidencia úhrad"
        verbose_name_plural = "💳 Evidencia úhrad"
        ordering = ['-datum_uhrady', '-id']

    def __str__(self):
        # 🛑 ISAS ARCHITECT FIX: Ochrana pred N+1. V __str__ používame len _id, neťaháme relačné objekty.
        fk_cislo = f"FAV-ID:{self.vystavena_faktura_id}" if self.vystavena_faktura_id else (f"FAP-ID:{self.prijata_faktura_id}" if self.prijata_faktura_id else "Bez párovania")
        mena_str = f" {self.mena}".rstrip() if getattr(self, 'mena', None) else ""
        return f"{self.get_typ_uhrady_display()} | {fk_cislo} | {self.suma_uhrady}{mena_str}"

    def clean(self):
        super().clean()
        
        # Validácia: Úhrada musí byť priradená aspoň jednej faktúre
        if not self.vystavena_faktura_id and not self.prijata_faktura_id:
            raise ValidationError("❌ Úhrada musí byť spárovaná buď s Vystavenou 📄 alebo Prijatou 📥 faktúrou.")
            
        # Validácia: Úhrada nesmie byť priradená OBOM faktúram naraz
        if self.vystavena_faktura_id and self.prijata_faktura_id:
            raise ValidationError("⚠️ Jedna úhrada nemôže hradiť Vystavenú 📄 aj Prijatú 📥 faktúru naraz. Vyberte iba jednu ☝️.")

        # Validacia 3: Kontrola kladnej sumy uhrady
        if not self.suma_uhrady or self.suma_uhrady <= Decimal('0.00'):
            raise ValidationError({'suma_uhrady': "🛑 Suma úhrady musí byť vyššia ako 0."})

        if self.vystavena_faktura_id:
            try:
                fa = self.vystavena_faktura
                if fa:
                    if fa.stav in ['DRAFT', 'STORNO']:
                        raise ValidationError({'vystavena_faktura': f"🛑 Nie je možné zaevidovať úhradu pre faktúru v stave '{fa.get_stav_display()}'."})
                    if self.mena and fa.mena and self.mena.strip().upper() != fa.mena.strip().upper():
                        raise ValidationError({'mena': f"🛑 Mena úhrady ({self.mena}) sa nezhoduje s menou faktúry ({fa.mena})."})
            except ObjectDoesNotExist:
                pass

        if self.prijata_faktura_id:
            try:
                pf = self.prijata_faktura
                if pf and self.mena and pf.mena and self.mena.strip().upper() != pf.mena.strip().upper():
                    raise ValidationError({'mena': f"🛑 Mena úhrady ({self.mena}) sa nezhoduje s menou prijatej faktúry ({pf.mena})."})
            except ObjectDoesNotExist:
                pass

    def save(self, *args, **kwargs):
        if not self.mena:
            try:
                if self.vystavena_faktura_id and self.vystavena_faktura and self.vystavena_faktura.mena:
                    self.mena = self.vystavena_faktura.mena
                elif self.prijata_faktura_id and self.prijata_faktura and self.prijata_faktura.mena:
                    self.mena = self.prijata_faktura.mena
            except ObjectDoesNotExist:
                pass
            if not self.mena:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentitaModel.objects.first()
                country_defaults = FiremnaIdentitaModel.get_country_defaults() if (FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'get_country_defaults')) else {}
                self.mena = getattr(identita, 'predvolena_mena', None) or country_defaults.get('mena') or get_firemna_mena()
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'mena'}
        super().save(*args, **kwargs)
          
class ERPExportNastavenia(models.Model):
    """
    💎 Tento model funguje ako Singleton (vždy len 1 záznam v databáze).
    🌍 Obsahuje globálne konštanty, ktoré XML generátor potrebuje pre správne spárovanie v účtovníctve.
    """
    ERP_CHOICES = [
        ('POHODA', '🟢 Stormware Pohoda (.xml)'),
        ('OMEGA', '🔵 KROS Omega (.txt)'),
    ]

    erp_system = models.CharField(
        "🎯 Cieľový ERP systém", max_length=20, 
        choices=ERP_CHOICES, default='POHODA',
        help_text="🛠️ Určuje, v akom formáte sa budú generovať XML súbory."
    )
    
    # --- 📊 ÚČTOVNÁ OSNOVA (Predkontácie) ---
    ucet_pohladavky = models.CharField("📈 Účet pre Odberateľov (Vystavené FA)", max_length=15, default='311000')
    ucet_zavazky = models.CharField("📉 Účet pre Dodávateľov (Prijaté FA)", max_length=15, default='321000')
    # Mandatórne polia pre úhrady (Vynútené vyplnenie v Admini, žiadny hardkód v kóde)
    ucet_banka = models.CharField(
        "🏦 POHODA: Skratka banky (Úhrady)", max_length=19, default='BÚ', blank=True,
        help_text="⚠️ POVINNÉ: Skratka bankového účtu v Pohode (napr. 'BÚ', 'VUB', 'TATRA')."
    )
    ucet_pokladna = models.CharField(
        "💵 POHODA: Skratka pokladne (Úhrady)", max_length=19, default='pokladňa',
        help_text="⚠️ POVINNÉ: Skratka pokladne v Pohode (napr. 'pokladňa', 'POK1')."
    )
    ucet_dph_20 = models.CharField("⚖️ Účet pre základnú sadzbu DPH", max_length=15, default='343023')
    ucet_dph_prenos = models.CharField("🔄 Účet pre DPH Prenesenie daň. povinnosti", max_length=15, default='343000')

    pohoda_skratka_banky = models.CharField(
        "🏦 POHODA: Skratka bankového účtu", max_length=19, blank=True, null=True,
        help_text="💡 VOLITEĽNÉ: Skratka bankového účtu v Pohode (napr. 'TATRA', 'CSOB', 'SLSP'). Ak necháte prázdne, bankový účet sa k faktúre nepriradí."
    )
    pohoda_predkontacia_fa = models.CharField(
        "🧾 POHODA: Predkontácia vydanej FA", max_length=19, default='2', blank=True,
        help_text="⚠️ POVINNÉ (Iba pre POHODA): Skratka predkontácie pre vystavené faktúry (napr. '2' alebo '2Fv'). Určuje presný spôsob zaúčtovania tržieb a pohľadávok v systéme Stormware Pohoda."
    )

    pohoda_predkontacia_prijata_fa = models.CharField(
        "🧾 POHODA: Predkontácia prijatej FA", max_length=19, default='1', blank=True,
        help_text="⚠️ POVINNÉ (Iba pre POHODA): Skratka predkontácie pre prijaté faktúry (napr. '1' pre nákup materiálu, alebo '2' pre služby). Určuje spôsob zaúčtovania nákladov."
    )

    pohoda_predkontacia_banka_prijem = models.CharField(
        "🏦 POHODA: Predkontácia banka (Príjem)", max_length=19, default='1P', blank=True,
        help_text="💡 Predvolená skratka predkontácie v Pohode pre prijaté platby (štandardne '1P')."
    )
    pohoda_predkontacia_banka_vydaj = models.CharField(
        "🏦 POHODA: Predkontácia banka (Výdaj)", max_length=19, default='2V', blank=True,
        help_text="💡 Predvolená skratka predkontácie v Pohode pre odoslané platby (štandardne '2V')."
    )

    pohoda_predkontacia_sluzby = models.CharField(
        "🧾 POHODA: Predkontácia pre Služby / Prácu", max_length=19, default='2', blank=True,
        help_text="💡 Skratka predkontácie v Pohode pre riadky so službami a dopravou (napr. '2' alebo 'Tržby_Služby')."
    )
    pohoda_predkontacia_tovar = models.CharField(
        "🧾 POHODA: Predkontácia pre Materiál / Tovar", max_length=19, default='3', blank=True,
        help_text="💡 Skratka predkontácie v Pohode pre riadky s materiálom (napr. '3' alebo 'Tržby_Tovar')."
    )

    # --- 🔵 OMEGA ŠPECIFICKÉ NASTAVENIA (Evidencie a Číselné rady) ---
    omega_evidencia_banka = models.CharField(
        "🔵 OMEGA: Evidencia Banka", max_length=50, blank=True,
        help_text="Kód evidencie pre bankové výpisy (napr. 'BV', 'TB', alebo číslo účtu)."
    )
    omega_ciselny_rad_banka = models.CharField(
        "🔵 OMEGA: Číselný rad Banka", max_length=20, blank=True,
        help_text="Kód číselného radu pre banku (napr. 'B1', 'TB')."
    )
    omega_evidencia_pokladna = models.CharField(
        "🔵 OMEGA: Evidencia Pokladňa", max_length=50, blank=True,
        help_text="Kód evidencie pre pokladničné doklady (napr. 'PD', 'Pokladnica č. 1')."
    )
    omega_ciselny_rad_pokladna_prijem = models.CharField(
        "🔵 OMEGA: Číselný rad Pokladňa (Príjem)", max_length=20, blank=True,
        help_text="Číselný rad pre Príjmové pokladničné doklady (napr. 'P')."
    )
    omega_ciselny_rad_pokladna_vydaj = models.CharField(
        "🔵 OMEGA: Číselný rad Pokladňa (Výdaj)", max_length=20, blank=True,
        help_text="Číselný rad pre Výdavkové pokladničné doklady (napr. 'V')."
    )
    omega_evidencia_id = models.CharField(
        "🔵 OMEGA: Evidencia Interné doklady", max_length=50, blank=True,
        help_text="Používa sa pre zápočty a peniaze na ceste (napr. 'ID', 'ID iný')."
    )
    omega_ciselny_rad_id = models.CharField(
        "🔵 OMEGA: Číselný rad Interné doklady", max_length=20, blank=True,
        help_text="Číselný rad pre interné doklady (napr. 'ID', 'IDX')."
    )

    # Voliteľné predkontácie (Ak zostanú prázdne, do XML sa neodošlú a Pohoda ich nastaví ako 'Bez')
    ucet_naklady_palivo = models.CharField(
        "⛽ Účet / Predkontácia pre tankovanie", max_length=19, blank=True, null=True,
        help_text="💡 VOLITEĽNÉ: Pre Pohodu skratka šablóny (napr. 'PHM'). Pre Omegu nákladový účet (napr. '501001'). Ak necháte prázdne, zaúčtuje sa ako 'Bez'."
    )
    
    # --- 🤖 AUTOMATIZÁCIA ---
    auto_odosielat_mailom = models.BooleanField(
        "📧 Automaticky odosielať vygenerovaný ZIP/XML účtovníčke", 
        default=False,
        help_text="🚀 Ak je zapnuté, po kliknutí na hromadný export v Admine sa súbor pošle rovno na e-mail."
    )

    ucet_zavazky_zamestnanci = models.CharField(
        "👥 Účet pre záväzky voči zamestnancom", max_length=15, default='333000',
        help_text="💡 Účet, kam sa zaúčtujú nevyplatené diéty alebo bločky za palivo platené z vlastného vrecka. 💸"
    )

    # 💾 SKRYTÝ TREZOR: Pamätá si nastavenia oboch systémov pri preklikávaní
    backup_data = models.JSONField(default=dict, blank=True, null=True)

    class Meta:
        verbose_name = "🏦 Nastavenia účtovného exportu"
        verbose_name_plural = "🏦 Nastavenie účtovného exportu"
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name='singleton_erp_nastavenia_id_1')
        ]

    def __str__(self):
        return f"🌟 Hlavné nastavenia pre {self.get_erp_system_display()}"

    # --- OPRAVENÉ RIADKY (models.py) ---
    @classmethod
    def get_settings(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    def clean(self):
        super().clean()
        
        # Získame pôvodný stav z DB pred uložením na detekciu zmeny ERP
        orig = None
        if self.pk:
            try:
                orig = self.__class__.objects.get(pk=self.pk)
            except self.__class__.DoesNotExist:
                pass

        if not self.backup_data:
            self.backup_data = {}

        pohoda_fields = [
            'ucet_banka', 'pohoda_skratka_banky', 'pohoda_predkontacia_fa', 
            'pohoda_predkontacia_prijata_fa', 'pohoda_predkontacia_banka_prijem', 
            'pohoda_predkontacia_banka_vydaj', 'pohoda_predkontacia_sluzby', 'pohoda_predkontacia_tovar'
        ]
        
        omega_fields = [
            'omega_evidencia_banka', 'omega_ciselny_rad_banka', 'omega_evidencia_pokladna', 
            'omega_ciselny_rad_pokladna_prijem', 'omega_ciselny_rad_pokladna_vydaj', 
            'omega_evidencia_id', 'omega_ciselny_rad_id'
        ]

        zmena_erp = bool(orig and orig.erp_system != self.erp_system)

        # Ak došlo k zmene ERP systému, zálohujeme starý systém priamo z pôvodného DB stavu (orig)
        if zmena_erp:
            if orig.erp_system == 'POHODA':
                for f in pohoda_fields:
                    self.backup_data[f] = getattr(orig, f)
            elif orig.erp_system == 'OMEGA':
                for f in omega_fields:
                    self.backup_data[f] = getattr(orig, f)

        # Tieto polia sú číselné v oboch systémoch, medzery tu nemajú čo robiť.
        if self.ucet_pohladavky: self.ucet_pohladavky = self.ucet_pohladavky.replace(" ", "")
        if self.ucet_zavazky: self.ucet_zavazky = self.ucet_zavazky.replace(" ", "")
        if self.ucet_dph_20: self.ucet_dph_20 = self.ucet_dph_20.replace(" ", "")
        if self.ucet_dph_prenos: self.ucet_dph_prenos = self.ucet_dph_prenos.replace(" ", "")
        if self.ucet_zavazky_zamestnanci: self.ucet_zavazky_zamestnanci = self.ucet_zavazky_zamestnanci.replace(" ", "")
        
        if self.erp_system == 'OMEGA':
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            iso_krajiny = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
            if iso_krajiny != 'SK':
                raise ValidationError({
                    'erp_system': f"KROS Omega TXT je určený výhradne pre slovenskú daňovú legislatívu a slovenský KV DPH. Vaša spoločnosť má sídlo v '{iso_krajiny}'."
                })
            if self.ucet_pokladna: self.ucet_pokladna = self.ucet_pokladna.replace(" ", "")
            if self.ucet_naklady_palivo: self.ucet_naklady_palivo = self.ucet_naklady_palivo.replace(" ", "")
            # 1. OMEGA: Nastavenie dynamických defaultov pre zdieľané polia
            if not self.ucet_pokladna: self.ucet_pokladna = '211000'
            # 🛡️ OMEGA je výhradne slovenský systém: DPH účet je vždy striktne slovenský (343023)
            if not self.ucet_dph_20: self.ucet_dph_20 = '343023'
            if not self.ucet_naklady_palivo: self.ucet_naklady_palivo = '501005'

            # Obnova Omega polí zo zálohy IBA pri prepnutí z iného ERP systému
            if zmena_erp:
                for f in omega_fields:
                    backup_val = self.backup_data.get(f)
                    if backup_val is not None:
                        setattr(self, f, backup_val)

            # 100% Ochrana - Ak je to OMEGA, tieto polia nesmú ostať prázdne!
            errors = {}
            if not self.omega_evidencia_banka: errors['omega_evidencia_banka'] = "Toto pole je pre Omegu povinné."
            if not self.omega_ciselny_rad_banka: errors['omega_ciselny_rad_banka'] = "Toto pole je pre Omegu povinné."
            if not self.omega_evidencia_pokladna: errors['omega_evidencia_pokladna'] = "Toto pole je pre Omegu povinné."
            if not self.omega_ciselny_rad_pokladna_prijem: errors['omega_ciselny_rad_pokladna_prijem'] = "Toto pole je pre Omegu povinné."
            if not self.omega_ciselny_rad_pokladna_vydaj: errors['omega_ciselny_rad_pokladna_vydaj'] = "Toto pole je pre Omegu povinné."
            if not self.omega_evidencia_id: errors['omega_evidencia_id'] = "Toto pole je pre Omegu povinné."
            if not self.omega_ciselny_rad_id: errors['omega_ciselny_rad_id'] = "Toto pole je pre Omegu povinné."
            
            if errors:
                raise ValidationError(errors)

            # Bezpečné premazanie Pohoda polí (už sú uložené v backup_data)
            for f in pohoda_fields:
                setattr(self, f, "")

        elif self.erp_system == 'POHODA':
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            iso_krajiny = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else ''
            if iso_krajiny not in ['SK', 'CZ']:
                raise ValidationError({
                    'erp_system': f"Stormware Pohoda XML je podporovaná výhradne pre trhy SK a CZ. Vaša spoločnosť má sídlo v '{iso_krajiny}'."
                })
            # 1. POHODA: Nastavenie dynamických defaultov pre zdieľané polia podľa sídla firmy
            if iso_krajiny == 'CZ':
                if not self.ucet_pokladna or self.ucet_pokladna == 'pokladňa':
                    self.ucet_pokladna = 'pokladna'
                if not self.ucet_dph_20 or self.ucet_dph_20 == '343023':
                    self.ucet_dph_20 = '343021'
            else:
                if not self.ucet_pokladna:
                    self.ucet_pokladna = 'pokladňa'
                if not self.ucet_dph_20:
                    self.ucet_dph_20 = '343023'

            # Obnova Pohoda polí zo zálohy IBA pri prepnutí z iného ERP systému
            defaults_pohoda = {
                'ucet_banka': 'BÚ', 'pohoda_predkontacia_fa': '2', 'pohoda_predkontacia_prijata_fa': '1',
                'pohoda_predkontacia_banka_prijem': '1P', 'pohoda_predkontacia_banka_vydaj': '2V',
                'pohoda_predkontacia_sluzby': '2', 'pohoda_predkontacia_tovar': '3'
            }
            if zmena_erp:
                for f in pohoda_fields:
                    backup_val = self.backup_data.get(f)
                    if backup_val is not None:
                        setattr(self, f, backup_val)
                    elif not getattr(self, f):
                        setattr(self, f, defaults_pohoda.get(f, ''))

            # Bezpečné premazanie Omega polí (už sú uložené v backup_data)
            for f in omega_fields:
                setattr(self, f, "")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            self.pk = 1 
            self.full_clean() 
            super().save(*args, **kwargs)


@receiver(post_save, sender=EvidenciaUhrad)
@receiver(post_delete, sender=EvidenciaUhrad)
def prepocti_stav_faktury_pri_uhrade(sender, instance, **kwargs):
    """
    Dynamický prepočet. Ak sa spočítajú všetky úhrady a sedia s celkovou sumou faktúry,
    faktúra sa automaticky označí ako ZAPLATENÁ.
    """

    with transaction.atomic():
        # Kontrola pre Vystavenú faktúru
        if instance.vystavena_faktura_id:
            fa = VystavenaFaktura.objects.select_for_update().filter(pk=instance.vystavena_faktura_id).first()
            if not fa:
                return
            spolu_uhradene = fa.uhrady.aggregate(Sum('suma_uhrady'))['suma_uhrady__sum'] or Decimal('0.00')
            
            # Absolútna hodnota! Ochrana pred auto-zaplatením Dobropisov (0 >= -100)
            fa_celkom = fa.celkom_s_dph or Decimal('0.00')
            if abs(fa_celkom) > Decimal('0.00') and spolu_uhradene >= abs(fa_celkom):
                if fa.stav != 'ZAPLATENA':
                    fa.stav = 'ZAPLATENA'
                    posledny_datum = fa.uhrady.aggregate(Max('datum_uhrady'))['datum_uhrady__max'] or instance.datum_uhrady
                    fa.datum_uhrady = posledny_datum
                    fa.save(update_fields=['stav', 'datum_uhrady'])
                    
                    # Automatizácia Zálohová -> Ostrá faktúra
                    if fa.typ_dokladu == 'ZALOHA':
                        # Ochrana proti duplicite (ak by sa signál odpálil viackrát)
                        uz_existuje = VystavenaFaktura.objects.filter(
                            zakaznik=fa.zakaznik, 
                            ai_poznamka__icontains=f"[AUTO-ZALOHA:{fa.id}]"
                        ).exclude(stav='STORNO').exists()
                        
                        if not uz_existuje:
                            # 1. Vytvoríme DRAFT ostrej faktúry (so zachovaním meny pôvodnej zálohy)
                            nova_fa = VystavenaFaktura.objects.create(
                                typ_dokladu='FAKTURA',
                                stav='DRAFT',
                                zakaznik=fa.zakaznik,
                                zariadenie=fa.zariadenie,
                                vystavil=fa.vystavil,
                                mena=fa.mena,
                                prenos_danovej_povinnosti=fa.prenos_danovej_povinnosti,
                                kod_clenenia_dph=fa.kod_clenenia_dph,
                                ai_poznamka=f"[AUTO-ZALOHA:{fa.id}] Automaticky vygenerované z uhradenej zálohy {fa.cislo_faktury}.",
                                poznamka_pre_klienta=f"Faktúra k prijatej platbe za zálohovú faktúru {fa.cislo_faktury}."
                            )
                            
                            # 2. Prekopírujeme všetky pôvodné položky zo zálohy
                            # Sadzbu odpočtu zálohy nastavíme podľa najvyššej sadzby na položkách zálohy
                            GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                            def_sadzba = GlobalneNastaveniaModel.get_zakladna_dph() if hasattr(GlobalneNastaveniaModel, 'get_zakladna_dph') else Decimal('0.00')

                            prva_polozka = fa.polozky.first()
                            max_dph = Decimal(str(prva_polozka.sadzba_dph)) if prva_polozka and prva_polozka.sadzba_dph is not None else def_sadzba
                            
                            polozky_na_vytvorenie = []
                            for p in fa.polozky.all():
                                if p.sadzba_dph and p.sadzba_dph > max_dph: 
                                    max_dph = p.sadzba_dph
                                polozky_na_vytvorenie.append(PolozkaVystavenejFaktury(
                                    faktura=nova_fa, typ_polozky=p.typ_polozky, nazov=p.nazov,
                                    praca_naviac=p.praca_naviac,
                                    mnozstvo=p.mnozstvo, merna_jednotka=p.merna_jednotka,
                                    jednotkova_cena_bez_dph=p.jednotkova_cena_bez_dph,
                                    zlava_percento=p.zlava_percento, sadzba_dph=p.sadzba_dph,
                                    cena_po_zlave_bez_dph=p.cena_po_zlave_bez_dph,
                                    hodnota_dph=p.hodnota_dph,
                                    spolu_s_dph=p.spolu_s_dph,
                                    ucet_vynosov=p.ucet_vynosov,
                                    obchodny_nazov=p.obchodny_nazov,
                                    skryt_vyrobcu=p.skryt_vyrobcu,
                                    content_type=p.content_type, object_id=p.object_id,
                                    zariadenie=p.zariadenie
                                ))
                                
                            # 3. Pridáme mínusovú položku "Odpočet zálohy" s exaktným výpočtom pred bulk_create
                            odpocet_zaklad = -abs(fa.celkom_bez_dph or Decimal('0.00'))
                            odpocet_dph = Decimal('0.00') if nova_fa.prenos_danovej_povinnosti else (odpocet_zaklad * (max_dph / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                            is_omega = False
                            try:
                                nastavenia_erp = ERPExportNastavenia.get_settings()
                                is_omega = getattr(nastavenia_erp, 'erp_system', 'POHODA') == 'OMEGA'
                                predkontacia_odpocet = '602000' if is_omega else (getattr(nastavenia_erp, 'pohoda_predkontacia_sluzby', None) or '2')
                            except Exception:
                                predkontacia_odpocet = '602000' if is_omega else '2'
                            polozky_na_vytvorenie.append(PolozkaVystavenejFaktury(
                                faktura=nova_fa,
                                typ_polozky='INE',
                                nazov=f"Odpočet uhradenej zálohy ({fa.cislo_faktury})",
                                mnozstvo=Decimal('-1.00'),
                                merna_jednotka='ks',
                                jednotkova_cena_bez_dph=abs(odpocet_zaklad),
                                zlava_percento=Decimal('0.00'),
                                sadzba_dph=max_dph,
                                cena_po_zlave_bez_dph=odpocet_zaklad,
                                hodnota_dph=odpocet_dph,
                                spolu_s_dph=odpocet_zaklad + odpocet_dph,
                                ucet_vynosov=predkontacia_odpocet
                            ))

                            PolozkaVystavenejFaktury.objects.bulk_create(polozky_na_vytvorenie)
                            
                            # 4. Vynulujeme hlavičku (Builder si ju prepočíta sám, keď ju dispečer otvorí)
                            nova_fa.celkom_bez_dph = Decimal('0.00')
                            nova_fa.celkom_dph = Decimal('0.00')
                            nova_fa.celkom_s_dph = Decimal('0.00')
                            nova_fa.save(update_fields=['celkom_bez_dph', 'celkom_dph', 'celkom_s_dph'])
            else:
                if fa.stav == 'ZAPLATENA':
                    # 🛡️ CASH-FLOW & LEGISLATÍVNA DOKONALOSŤ: 
                    # Dobropis je náš záväzok vrátiť peniaze, nemôže byť penalizovaný ako pohľadávka po splatnosti odberateľa.
                    dnes = timezone.now().date()
                    d_splatnosti = to_date(fa.datum_splatnosti)
                    if fa.typ_dokladu != 'DOBROPIS' and d_splatnosti and d_splatnosti < dnes:
                        fa.stav = 'PO_SPLATNOSTI'
                    else:
                        fa.stav = 'ODOSLANA'
                    fa.datum_uhrady = None
                    fa.save(update_fields=['stav', 'datum_uhrady'])

        # Kontrola pre Prijatú faktúru
        if instance.prijata_faktura_id:
            # Používame _id, aby sme predišli zbytočnému N+1 dopytu pred uzamknutím.
            pf = PrichodziaFaktura.objects.select_for_update().filter(pk=instance.prijata_faktura_id).first()
            if not pf:
                return
            spolu_uhradene = pf.uhrady.aggregate(Sum('suma_uhrady'))['suma_uhrady__sum'] or Decimal('0.00')
            
            if pf.suma_na_uhradu and spolu_uhradene >= pf.suma_na_uhradu:
                if pf.stav_uhrady != 'ZAPLATENA':
                    pf.stav_uhrady = 'ZAPLATENA'
                    pf.save(update_fields=['stav_uhrady'])
            else:
                if pf.stav_uhrady == 'ZAPLATENA':
                    pf.stav_uhrady = 'NEZAPLATENA'
                    pf.save(update_fields=['stav_uhrady'])

class DashboardKvality(Uloha):
    class Meta:
        proxy = True
        verbose_name = "📊 Prehľad reklamácií a kvality"
        verbose_name_plural = "📊 Prehľad reklamácií a kvality"

class ImportDatTask(ISASAuditMixin, models.Model):
    TYP_IMPORTU_CHOICES = [
        ('ZAKAZNICI', '🏢 Zákazníci (Firmy)'),
        ('DODAVATELIA', '🏭 Dodávatelia'),
        ('SUKROMNE_OSOBY', '👤 Súkromné osoby'),
        ('ZAMESTNANCI', '👷 Zamestnanci'),
        ('VOZIDLA', '🚗 Vozidlá (Fleet)'),
        ('SKLAD_KARTY', '📦 Skladové karty (Cenový katalóg)'),
        ('TYPY_SERVISU', '🛠️ Typy servisu a prác (Cenník)'),
        ('VYROBCOVIA', '🧰 Databáza výrobcov (Značky)'),
        ('FILIALKY', '🏪 Sklady a Filiálky'),
        ('SERVISOVANE_ZARIADENIA', '⚙️ Servisované zariadenia'),
    ]

    STAV_CHOICES = [
        ('NAHRATE', '⏳ Nahraté (Čaká na spracovanie)'),
        ('SPRACOVAVA_SA', '⚙️ Spracováva sa (AI / Worker)'),
        ('HOTOVO', '✅ Úspešne dokončené'),
        ('CHYBA', '❌ Zlyhalo / Čiastočné chyby'),
    ]

    typ_importu = models.CharField(
        max_length=30, choices=TYP_IMPORTU_CHOICES,
        default='ZAKAZNICI', 
        verbose_name="🗂️ Typ importovaných dát",
        help_text="📂 Určuje kategóriu dát a požadovanú štruktúru stĺpcov pre hromadné spracovanie."
    )
    stav = models.CharField(
        max_length=20, choices=STAV_CHOICES, default='NAHRATE', 
        verbose_name="🚥 Stav importu",
        help_text="📊 Aktuálny stav automatického spracovania nahratého súboru na pozadí."
    )
    
    subor = models.FileField(
        upload_to='importy_dat/%Y/%m/', 
        verbose_name="📄 Súbor na import (Excel / CSV)",
        help_text="📁 Importný súbor, ktorý musí presne odpovedať štruktúre stiahnutej vzorovej šablóny."
    )
    
    vytvoril = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, limit_choices_to={'is_staff': True},
        verbose_name="👤 Spustil import",
        help_text="👤 Používateľ, ktorý súbor nahral a inicioval proces hromadného importu."
    )
    vytvorene = models.DateTimeField(
        auto_now_add=True, 
        verbose_name="🕒 Čas nahratia",
        help_text="🕒 Časová pečiatka vytvorenia požiadavky na import v systéme."
    )
    dokoncene = models.DateTimeField(
        null=True, blank=True, 
        verbose_name="🏁 Čas dokončenia",
        help_text="🏁 Presný čas, kedy sa dokončilo spracovanie všetkých riadkov súboru."
    )

    # --- ŠTATISTIKY A LOGY ---
    celkovy_pocet_riadkov = models.PositiveIntegerField(
        default=0, 
        verbose_name="📊 Celkový počet riadkov",
        help_text="📊 Celkový počet načítaných dátových riadkov v importovanom súbore."
    )
    uspesne_importovane = models.PositiveIntegerField(
        default=0, 
        verbose_name="✅ Úspešne vytvorené",
        help_text="✅ Počet záznamov, ktoré boli úspešne zapísané alebo aktualizované v databáze."
    )
    preskocene_duplicity = models.PositiveIntegerField(
        default=0, 
        verbose_name="⏭️ Preskočené (Duplicity)",
        help_text="⏭️ Počet riadkov vynechaných z dôvodu existencie duplicitných údajov."
    )
    chybne_riadky = models.PositiveIntegerField(
        default=0, 
        verbose_name="⚠️ Chybné riadky",
        help_text="❌ Počet riadkov, ktoré zlyhali pri spracovaní z dôvodu nesprávnych alebo neúplných dát."
    )
    
    chybovy_log = models.TextField(
        blank=True, null=True, 
        verbose_name="📝 Detailný chybový log",
        help_text="📝 Detailný výpis chýb s označením riadkov pre rýchlu korekciu neplatných údajov."
    )
    REZIM_CHOICES = [
        ('INSERT', '➕ Iba pridať nové (Preskočiť existujúce)'),
        ('UPDATE', '🔄 Aktualizovať existujúce (Podľa IČO / ŠPZ / Kódu)'),
    ]
    rezim_importu = models.CharField(
        max_length=10, choices=REZIM_CHOICES, default='INSERT', 
        verbose_name="⚙️ Režim importu",
        help_text="⚙️ Stratégia spracovania pri zhode kľúčových údajov (vytvorenie nových alebo úprava existujúcich)."
    )
    velkost_suboru_kb = models.PositiveIntegerField(
        null=True, blank=True, editable=False, 
        verbose_name="💾 Veľkosť súboru (KB)",
        help_text="💾 Automaticky vypočítaná veľkosť nahratého súboru v kilobajtoch."
    )

    class Meta:
        verbose_name = "📥 Import dát a informácii"
        verbose_name_plural = "📥 Importy dát a informácii"
        ordering = ['-vytvorene']

    def __str__(self):
        return f"Import {self.get_typ_importu_display()} [{self.get_stav_display()}]"

    STLPCE_MAPA = {
        'ZAKAZNICI': ['Nazov_spolocnosti', 'ICO', 'DIC', 'IC_DPH', 'Ulica_a_cislo', 'Mesto', 'PSC', 'Stat', 'email', 'telefon', 'IBAN', 'Domena', 'Subdomena', 'kontaktna_osoba', 'Predvolena_splatnost_dni', 'Reklamacna_lehota_dni', 'Oslovenie_rod', 'Datum_narodenia', 'Vyzaduje_povolenie_vstupu', 'Email_vratnice', 'Telefon_vratnica', 'Instrukcie_vstupu', 'Url_vstupneho_portalu', 'Cena_za_kwh_eur', 'Emisny_faktor_co2', 'B2b_zlava_percenta', 'Zobrazit_ako_referenciu', 'Ostatne'],
        
        'DODAVATELIA': ['Nazov_spolocnosti', 'ICO', 'DIC', 'IC_DPH', 'Ulica_a_cislo', 'Mesto', 'PSC', 'Stat', 'email', 'telefon', 'IBAN', 'kontaktna_osoba', 'Domena', 'Predvolena_splatnost_dni', 'Oslovenie_rod', 'Datum_narodenia', 'Erp_predkontacia', 'Ostatne'],
        
        'SUKROMNE_OSOBY': ['Meno_a_priezvisko', 'email', 'telefon', 'Ulica_a_cislo', 'Mesto', 'PSC', 'Stat', 'IBAN', 'Datum_narodenia', 'Telegram_id', 'Discord_id', 'Predvolena_cerpacka_ico'],
        
        'ZAMESTNANCI': ['Meno', 'Priezvisko', 'email', 'telefon', 'Typ_pomeru', 'Pracovna_rola', 'IBAN', 'Ulica_a_cislo', 'Mesto', 'PSC', 'Stat', 'Datum_narodenia', 'Datum_nastupu', 'Narok_na_dovolenku', 'Vycerpana_dovolenka', 'Sposob_odmenovania', 'Mzda_brutto_hodina', 'Mzda_brutto_mesiac', 'Mzda_zivnostnik_hodina', 'Zavazky', 'Poznamka_zavazky', 'Cislo_op', 'Bozp_platne_do', 'Tg_upozornenia_ulohy', 'Mail_upozornenia_ulohy', 'Discord_upozornenia_ulohy', 'Zdiela_polohu', 'Telegram_id', 'Discord_id', 'Predvolena_cerpacka_ico', 'Zobrazit_na_webe', 'Verejna_rola'],
        
        'VOZIDLA': ['Znacka_a_model', 'SPZ', 'Typ_paliva', 'Aktualny_stav_km', 'Je_firemne', 'Typ_vozidla_erp', 'Erp_stredisko', 'Erp_kod_paliva', 'Vlastna_sadzba_za_km', 'Varovanie_max_litre', 'Varovanie_max_suma', 'Aktualne_pneumatiky', 'Servis_interval_km', 'Posledny_servis_km', 'Olej_interval_km', 'Posledny_olej_km', 'Cislo_palivovej_karty', 'Stk_plati_do', 'Dialnicna_znamka_do', 'Pzp_plati_do', 'Kasko_plati_do', 'Lekarnicka_plati_do', 'Priradeny_zamestnanec_email', 'Sukromna_osoba_email'],
        
        'TYPY_SERVISU': ['Nazov', 'Hodinova_sadzba_predaj', 'Fixna_cena_predaj', 'Sposob_uctovania', 'Je_fakturovatelna', 'Normohodiny', 'Hodinova_sadzba_naklad', 'Fixny_naklad', 'Priorita', 'Zaruka_mesiace', 'Pracovny_postup', 'Zverejnit_postup_na_webe', 'Zobrazit_na_webe', 'Popis_na_webe'],
        
        'VYROBCOVIA': ['Nazov_vyrobcu', 'Domena_webu', 'Krajina_povodu', 'Typ_vyroby', 'Katalog_dielov_url', 'Technicka_poznamka', 'Zobrazit_na_webe'],
        
        'FILIALKY': ['Nazov', 'Kod_pobocky', 'Typ_skladu', 'Adresa', 'Veduci_skladu', 'telefon', 'email', 'Je_aktivna', 'Poznamka', 'Zodpovedny_zamestnanec_email'],
        
        'SERVISOVANE_ZARIADENIA': [
            'Zakaznik_ico', 'Kategoria', 'Vyrobca_nazov', 'Model_zariadenia', 'Vyrobne_cislo', 
            'Nazov', 'Rok_vyroby', 'Datum_instalacie', 'Zaruka_do', 'Predlzena_zaruka_do', 
            'Hmotnost_kg', 'Umiestnenie', 'Aktualne_motohodiny', 'Motohodiny_v_zatazi', 
            'Iot_mac_adresa', 'Iot_ip_adresa', 'Iot_protokol', 'Iot_je_aktivny', 
            'Iot_limit_zapisov_za_hodinu', 'V_prenajme_od_isas', 'Je_urcene_na_prenajom', 
            'Cena_prenajmu_na_den', 'Zobrazit_na_webe_prenajom', 'V_servisnom_rezime', 'Poznamka'
        ],
        
        'SKLAD_KARTY': [
            'Hlavna_kategoria', 'Podkategoria', 'Seriove_cislo', 'Katalogove_cislo', 'Ean_kod', 
            'Obchodny_nazov', 'Skryt_vyrobcu', 'Nazov_kto_som', 'Pocet_na_sklade', 'Merna_jednotka', 
            'Typ_zasoby_erp', 'Sadzba_dph', 'Typ_dielu_ON', 'Dodavatel_ico', 'Vyrobca_nazov', 
            'Zobrazit_na_webe', 'Popis_na_webe'
        ],

    
    }
            
    def clean(self):
        super().clean()
        if not self.subor:
            return

        MAX_SIZE_MB = 20
        if self.subor.size > MAX_SIZE_MB * 1024 * 1024:
            raise ValidationError({'subor': f"❌ Súbor je príliš veľký. Maximálna povolená veľkosť je {MAX_SIZE_MB} MB."})

        if self.subor.size == 0:
            raise ValidationError({'subor': "❌ Nahratý súbor je prázdny (0 bytov)."})

        filename = self.subor.name.lower()
        ocakavane_hlavicky = self.STLPCE_MAPA.get(self.typ_importu, [])

        if not ocakavane_hlavicky:
            raise ValidationError({'typ_importu': "⚠️ Zvolený typ importu nemá definovanú štruktúru stĺpcov."})

        prve_riadky = []

        try:
            if filename.endswith('.csv'):
                self.subor.seek(0)
                first_line_bytes = self.subor.readline()
                
                # 🛡️ 3. OCHRANA: Inteligentná detekcia kódovania celej prvej riadky (zamedzuje odseknutiu multibyte znaku)
                try:
                    text_data = first_line_bytes.decode('utf-8-sig')
                except UnicodeDecodeError:
                    try:
                        text_data = first_line_bytes.decode('windows-1250')
                    except UnicodeDecodeError:
                        raise ValidationError({'subor': "❌ Nepodarilo sa prečítať kódovanie súboru. Uložte CSV vo formáte UTF-8."})

                separator = ';' if ';' in text_data else ','
                reader = csv.reader([text_data.strip('\r\n')], delimiter=separator)
                try:
                    prve_riadky = next(reader)
                except StopIteration:
                    prve_riadky = []

            elif filename.endswith('.xlsx'):
                self.subor.seek(0)
                try:
                    wb = openpyxl.load_workbook(self.subor, read_only=True, data_only=True)
                    sheet = wb.active
                    for row in sheet.iter_rows(max_row=1, values_only=True):
                        prve_riadky = [str(cell).strip() if cell is not None else "" for cell in row]
                        break
                    wb.close()
                except zipfile.BadZipFile:
                    # 🛡️ 4. OCHRANA: Falošné alebo poškodené Excel súbory
                    raise ValidationError({'subor': "❌ Súbor nie je platný Excel (.xlsx). Skontrolujte, či ste nepremenovali starý .xls súbor."})
            else:
                raise ValidationError({'subor': "❌ Nepodporovaný formát. Systém akceptuje iba súbory typu .CSV alebo .XLSX."})

        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError({'subor': f"🚨 Súbor je poškodený alebo ho nebolo možné prečítať. Chyba: {str(e)}"})
        finally:
            # 🛡️ 5. OCHRANA: Bezpečné vrátenie pointra na začiatok bez pádu na zatvorenom deskriptore
            try:
                if hasattr(self.subor, 'closed') and self.subor.closed:
                    self.subor.open('rb')
                self.subor.seek(0)
            except Exception:
                pass

        # Očistenie hlavičiek od medzier
        vycistene_hlavicky = [h.strip() for h in prve_riadky if h.strip()]

        # 🛡️ 6. OCHRANA: Case-insensitive porovnávanie (Ignoruje veľké/malé písmená)
        ocakavane_lower = {h.lower(): h for h in ocakavane_hlavicky}
        vycistene_lower = {h.lower(): h for h in vycistene_hlavicky}

        from Evidencia.industry_loader import INDUSTRY_TERMS
        # 🛡️ ISAS ARCHITECT: Dynamická normalizácia stĺpcov cez INDUSTRY_TERMS bez hardkódu
        aliasy_stlpsov = {
            'aktualne_motohodiny': 'aktualne_motohodiny',
            'motohodiny_v_zatazi': 'motohodiny_v_zatazi',
        }
        for term_klic, ciel in [('meter_label', 'aktualne_motohodiny'), ('meter_label_short', 'aktualne_motohodiny'), ('meter_load_label', 'motohodiny_v_zatazi')]:
            t_val = INDUSTRY_TERMS.get(term_klic)
            if t_val:
                aliasy_stlpsov[re.sub(r'[^\w]', '_', str(t_val)).lower().strip('_')] = ciel
                aliasy_stlpsov[str(t_val).lower().strip()] = ciel
        normalizovane_vycistene = {aliasy_stlpsov.get(h, h): h for h in vycistene_lower}
        canonical_ocakavane = {aliasy_stlpsov.get(h, h): orig for h, orig in ocakavane_lower.items()}

        missing_fields = [canonical_ocakavane[c] for c in canonical_ocakavane if c not in normalizovane_vycistene]
        extra_fields = [vycistene_lower[h] for h in vycistene_lower if aliasy_stlpsov.get(h, h) not in canonical_ocakavane]

        error_msg = ""
        if missing_fields:
            error_msg += f"Chýbajúce stĺpce: <code style='color:#dc2626;'>{escape(', '.join(missing_fields))}</code><br>"
        if extra_fields:
            error_msg += f"Neznáme stĺpce navyše: <code style='color:#ea580c;'>{escape(', '.join(extra_fields))}</code><br>"

        if error_msg:
            raise ValidationError({
                'subor': format_html(
                    "❌ <b>Súbor nezodpovedá našej vzorovej šablóne!</b><br>{}"
                    "Stiahnite si prosím originálny vzor zo sekcie 1 vyššie a nemeňte prvý riadok.",
                    mark_safe(error_msg)
                )
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        
        is_new = self.pk is None
        if self.subor and not self.velkost_suboru_kb:
            try:
                self.velkost_suboru_kb = round(self.subor.size / 1024)
            except Exception:
                self.velkost_suboru_kb = None
                
        super().save(*args, **kwargs)
        
        if is_new and self.stav == 'NAHRATE':
            from Evidencia.tasks import spracuj_import_dat_task
            def _safe_dispatch_import(doc_pk):
                try:
                    spracuj_import_dat_task.delay(doc_pk)
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre import dát {doc_pk}: {exc}")
                    ImportDatTask.objects.filter(pk=doc_pk).update(
                        stav='CHYBA',
                        chybovy_log=f"⚠️ Celery/Redis broker nedostupný pri zaradení do fronty: {exc}"
                    )

            transaction.on_commit(lambda pk=self.pk: _safe_dispatch_import(pk))

class KorekciaMotohodinLog(models.Model):
    """🛡️ ISAS AUDIT: Bezpečnostný log manuálnych korekcií prevádzkového počítadla stroja."""
    zariadenie = models.ForeignKey(
        'ServisovaneZariadenie', 
        on_delete=models.CASCADE, 
        related_name='korekcie_motohodin', 
        verbose_name="⚙️ Zariadenie",
        help_text="⚙️ Servisované zariadenie, pri ktorom došlo k manuálnej úprave stavu počítadla."
    )
    povodna_hodnota = models.IntegerField(
        "📉 Pôvodný stav počítadla",
        help_text="📉 Hodnota celkového počítadla pred vykonaním manuálnej korekcie."
    )
    nova_hodnota = models.IntegerField(
        "📈 Nový stav počítadla",
        help_text="📈 Nová nastavená hodnota celkového počítadla pre správne plánovanie údržby."
    )
    povodna_hodnota_zataz = models.IntegerField(
        "📉 Pôvodný stav v záťaži", 
        null=True, 
        blank=True,
        help_text="📉 Pôvodný stav záťažového počítadla pred manuálnou úpravou."
    )
    nova_hodnota_zataz = models.IntegerField(
        "📈 Nový stav v záťaži", 
        null=True, 
        blank=True,
        help_text="📈 Nový nastavený stav záťažového počítadla po úprave."
    )
    dovod = models.TextField(
        "📝 Dôvod korekcie", 
        help_text="📝 Biznisové zdôvodnenie manuálnej úpravy pre účely auditu a prevádzkovej kontroly."
    )
    vykonal = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        limit_choices_to={'is_staff': True},
        verbose_name="👤 Vykonal",
        help_text="👤 Používateľ, ktorý manuálnu korekciu počítadla zaevidoval."
    )
    datum = models.DateTimeField(
        "🕒 Dátum a čas", 
        auto_now_add=True,
        help_text="🕒 Presný dátum a čas vytvorenia auditného záznamu o korekcii."
    )

    class Meta:
        verbose_name = "🕒 Korekcia počítadla / motohodín"
        verbose_name_plural = "🕒 Logy korekcií počítadla / motohodín"
        ordering = ['-datum']

    # --- 📖 DYNAMICKÉ ODVETVOVÉ PROPERTIES (INDUSTRY LEXICON) ---
    @property
    @admin.display(description="Pôvodný stav počítadla")
    def povodna_hodnota_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'povodna_hodnota', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    @property
    @admin.display(description="Nový stav počítadla")
    def nova_hodnota_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'nova_hodnota', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    @property
    @admin.display(description="Pôvodný stav v záťaži")
    def povodna_hodnota_zataz_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'povodna_hodnota_zataz', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_load_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    @property
    @admin.display(description="Nový stav v záťaži")
    def nova_hodnota_zataz_display(self):
        from .industry_loader import get_industry_term
        val = getattr(self, 'nova_hodnota_zataz', None)
        if val is not None and val >= 0:
            try:
                formatted = f"{int(val):,}".replace(',', ' ')
                return f"{formatted} {get_industry_term('meter_load_unit', 'hod.')}"
            except (ValueError, TypeError):
                pass
        return "—"

    def __str__(self):
        return f"Korekcia {self.zariadenie.vyrobne_cislo} ({self.povodna_hodnota_display} -> {self.nova_hodnota_display})"

class GPSStopa(models.Model):
    """
    📍 ISAS TRACKING: Extrémne ľahká tabuľka pre ukladanie histórie pohybu technikov.
    Slúži výhradne na vykreslenie "hada" (breadcrumbs) na Live Radare.
    """
    zamestnanec = models.ForeignKey(
        'Zamestnanec',
        on_delete=models.CASCADE,
        related_name='gps_stopy',
        help_text="👤 Servisný technik, ku ktorému prislúcha tento bod trasy pre evidenciu pohybov a kontrolu efektivity."
    )
    lat = models.FloatField(
        help_text="🌐 Zemepisná šírka (latitude) pre presné zobrazenie polohy na Live Radare."
    )
    lon = models.FloatField(
        help_text="🌐 Zemepisná dĺžka (longitude) pre presné zobrazenie polohy na Live Radare."
    )
    timestamp = models.DateTimeField(
        default=timezone.now,
        help_text="⏱️ Presný čas zaznamenania polohy pre rekonštrukciu trasy a overenie dodržania SLA."
    )

    class Meta:
        verbose_name = "📍 GPS Stopa"
        verbose_name_plural = "📍 GPS Stopy"
        # 🚀 KRITICKÁ OPTIMALIZÁCIA: Index pre bleskové vyhľadávanie dnešnej trasy
        indexes = [
            models.Index(fields=['zamestnanec', 'timestamp']),
        ]
        ordering = ['timestamp']

    def __str__(self):
        return f"{self.zamestnanec} - {self.timestamp.strftime('%H:%M:%S')}"

class AIAuditLog(models.Model):
    """
    Trvalá evidencia komunikácie s AI pre účely fakturácie, limitov a auditu.
    """
    pouzivatel = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, limit_choices_to={'is_staff': True}, verbose_name="👤 Používateľ (Zamestnanec)", help_text="👤 Interný zamestnanec, ktorý vytvoril požiadavku, pre účely kontroly čerpania limitov.")
    sukromna_osoba = models.ForeignKey('SukromnaOsoba', on_delete=models.CASCADE, null=True, blank=True, verbose_name="👤 Súkromná osoba (Zákazník)", help_text="👤 Externý zákazník komunikujúci s AI, využívané pre rozúčtovanie nákladov a SLA.")
    zdroj_poziadavky = models.CharField(max_length=20, choices=[('WEB', 'Webový portál'), ('TELEGRAM', 'Telegram Bot'), ('DISCORD', 'Discord Bot'), ('SYSTEM', 'Systémový proces / AI Audítor')], default='WEB', verbose_name="🌐 Zdroj", help_text="🌐 Vstupné rozhranie alebo komunikačný kanál, cez ktorý požiadavka prišla.")
    cas_dopytu = models.DateTimeField(auto_now_add=True, verbose_name="⏱️ Čas dopytu", help_text="⏱️ Presný čas odoslania dopytu pre auditné účely a výpočet denných limitov.")
    otazka = models.TextField(blank=True, null=True, verbose_name="❓ Znenie otázky", help_text="❓ Pôvodný text zadaného dopytu pre spätné overenie a audit komunikácie.") # 🔒 NOVÉ
    odpoved = models.TextField(blank=True, null=True, verbose_name="🤖 Odpoveď AI", help_text="🤖 Vygenerovaná odpoveď z AI pre kontrolu kvality a dodržiavania predpisov.") # 🔒 NOVÉ
    celery_task_id = models.CharField(max_length=50, blank=True, null=True, verbose_name="🆔 Celery Task ID", help_text="🆔 Technický identifikátor úlohy na pozadí pre diagnostiku spracovania.")
    pouzity_model = models.CharField(max_length=50, blank=True, null=True, verbose_name="🤖 Model", help_text="🤖 Názov Gemini modelu, ktorý požiadavku spracoval.")
    vstupne_tokeny = models.PositiveIntegerField(default=0, verbose_name="📥 Vstupné tokeny (Prompt)", help_text="📥 Počet spracovaných vstupných tokenov ovplyvňujúci variabilné náklady dopytu.")
    vystupne_tokeny = models.PositiveIntegerField(default=0, verbose_name="📤 Výstupné tokeny (Odpoveď)", help_text="📤 Počet vygenerovaných tokenov odpovede vstupujúci do kalkulácie ceny.")
    presna_cena_usd = models.DecimalField(max_digits=8, decimal_places=6, default=Decimal('0.000000'), verbose_name="💵 Presná cena (USD)", help_text="💵 Priame náklady na spracovanie požiadavky v USD pre finančné vyhodnotenie.")
    uspesne_dokoncene = models.BooleanField(default=False, verbose_name="✅ Dokončené", help_text="✅ Indikátor úspešného vybavení požiadavky bez systémovej chyby.")

    class Meta:
        verbose_name = "🧠 AI Audit & Náklady"
        verbose_name_plural = "🧠 AI Audit & Náklady"
        ordering = ['-cas_dopytu']
        # Index pre extrémne rýchle počítanie denných limitov
        indexes = [
            models.Index(fields=['pouzivatel', 'cas_dopytu']),
            models.Index(fields=['sukromna_osoba', 'cas_dopytu']),
            models.Index(fields=['zdroj_poziadavky']),
            models.Index(fields=['pouzity_model']),
        ]

    @property
    @admin.display(description="Dĺžka otázky (znaky)")
    def dlzka_otazky(self):
        """Vráti dĺžku textu otázky pre admin prehľad."""
        return len(self.otazka) if self.otazka else 0

    @property
    @admin.display(description="Odhadovaná cena (¢)")
    def odhadovana_cena_centy(self):
        """Prepočíta cenu v USD na centy pre zobrazenie v adminovom prehľade a __str__."""
        if self.presna_cena_usd is not None:
            return (self.presna_cena_usd * Decimal('100.00')).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
        return Decimal('0.0000')

    def __str__(self):
        osoba = self.pouzivatel if self.pouzivatel else (self.sukromna_osoba if self.sukromna_osoba else "Neznámy používateľ")
        cas_str = self.cas_dopytu.strftime('%d.%m.%Y %H:%M') if self.cas_dopytu else "Nedatované"
        return f"{osoba} [{self.zdroj_poziadavky}] | {cas_str} | {self.odhadovana_cena_centy} ¢"

class Nepritomnost(ISASAuditMixin, models.Model):
    """
    🧠 ISAS ABSENCE ENGINE PRO: Model pre evidenciu dovoleniek, PN a paragrafov.
    Obsahuje ochranu proti prekrývaniu, podporu pre pol-dne a legislatívne presný výpočet dní.
    """
    TYP_CHOICES = [
        ('DOVOLENKA', '🌴 Dovolenka'),
        ('PN', '🤒 Práceneschopnosť (PN)'),
        ('PARAGRAF', '⚖️ Paragraf (Lekár/Úrad)'),
        ('NEPLATENE', '🚫 Neplatené voľno'),
    ]
    
    STAV_CHOICES = [
        ('CAKA', '⏳ Čaká na schválenie'),
        ('SCHVALENE', '✅ Schválené'),
        ('ZAMIETNUTE', '❌ Zamietnuté'),
    ]

    zamestnanec = models.ForeignKey(
        'Zamestnanec', 
        on_delete=models.CASCADE, 
        related_name='nepritomnosti', 
        verbose_name="👨‍💼 Zamestnanec",
        help_text="👨‍💼 Zamestnanec, ktorého sa neprítomnosť týka, pre účely kapacitného plánovania a mzdovej agendy."
    )
    typ = models.CharField(
        max_length=15, 
        choices=TYP_CHOICES, 
        default='DOVOLENKA', 
        verbose_name="📌 Typ absencie",
        help_text="📌 Druh neprítomnosti určujúci zákonný nárok na hradenie miezd a legislatívny zápočet."
    )
    stav = models.CharField(
        max_length=15, 
        choices=STAV_CHOICES, 
        default='CAKA', 
        verbose_name="🚥 Stav",
        help_text="🚥 Aktuálne štádium schvaľovacieho procesu pre internú riadiacu kontrolu."
    )
    
    datum_od = models.DateField(
        verbose_name="📅 Dátum OD",
        help_text="📅 Prvý deň začiatku absencie zamestnanca."
    )
    datum_do = models.DateField(
        verbose_name="📅 Dátum DO",
        help_text="📅 Posledný deň absencie vrátane, rozhodujúci pre správny výpočet celkového nároku."
    )
    
    pol_dna = models.BooleanField(
        default=False, 
        verbose_name="🌗 Iba pol dňa (0.5)",
        help_text="🌗 Zaškrtnite pri poldennej absencii (napr. krátky návštevný čas u lekára alebo pol dňa dovolenky)."
    )
    
    pocet_pracovnych_dni = models.FloatField(
        default=0.0, editable=False, 
        verbose_name="⏱️ Počet dní (Na preplatenie)",
        help_text="⏱️ Automaticky vypočítaná hodnota pre mzdy (PN zahŕňa kalendárne dni, dovolenka a paragraf pracovné dni)."
    )
    
    priloha = models.FileField(
        upload_to='hr_absencie/', blank=True, null=True, 
        verbose_name="📎 Doklad (Priepustka / PN)",
        help_text="📎 Priložený naskenovaný doklad (ePN, potvrdenie od lekára) pre legislatívne účely a audit miezd."
    )
    
    schvalil = models.ForeignKey(
        'Zamestnanec', on_delete=models.SET_NULL, null=True, blank=True, 
        related_name='schvalene_absencie', verbose_name="👑 Schválil",
        help_text="👑 Vedúci pracovník alebo dispečer, ktorý túto žiadosť autorizoval."
    )
    
    poznamka = models.TextField(
        blank=True, null=True, 
        verbose_name="📝 Poznámka / Dôvod",
        help_text="📝 Doplňujúce informácie k absencii určene pre vedenie alebo mzdové oddelenie."
    )

    class Meta:
        verbose_name = "🌴 Absencia / Neprítomnosť"
        verbose_name_plural = "🌴 Absencie a Neprítomnosti"
        ordering = ['-datum_od']

    def __str__(self):
        pol_text = " (Pol dňa)" if self.pol_dna else ""
        return f"{self.zamestnanec} | {self.get_typ_display()}{pol_text} ({self.datum_od.strftime('%d.%m.%Y')} - {self.datum_do.strftime('%d.%m.%Y')})"

    @property
    def meno_schvalujuceho(self):
        """Bezpečné vrátenie mena človeka, ktorý absenciu schválil."""
        if self.schvalil:
            return f"{self.schvalil.meno} {self.schvalil.priezvisko}"
        return "Systém / Neznámy"

    def clean(self):
        super().clean()
        d_od = to_date(self.datum_od)
        d_do = to_date(self.datum_do)
        if d_od and d_do:
            if d_od > d_do:
                raise ValidationError({'datum_do': "🚫 Dátum DO nemôže byť pred dátumom OD."})
            
            if self.pol_dna and d_od != d_do:
                raise ValidationError({'pol_dna': "🌗 Pol dňa je možné zadať len pre jeden konkrétny deň."})
            
            # 🛡️ OCHRANA PROTI PREKRÝVANIU (Vylučujeme zamietnuté žiadosti a exclude robíme len pre existujúci PK)
            if self.zamestnanec_id:
                prekrytia = Nepritomnost.objects.filter(
                    zamestnanec_id=self.zamestnanec_id,
                    datum_od__lte=d_do,
                    datum_do__gte=d_od
                ).exclude(stav='ZAMIETNUTE')
                if self.pk:
                    prekrytia = prekrytia.exclude(pk=self.pk)
            else:
                prekrytia = Nepritomnost.objects.none()
            
            if self.pol_dna:
                # Ak ide o pol dňa, konflikt nastane len ak existuje celodenná absencia alebo už 2 iné poldňové
                ma_celodennu = prekrytia.filter(pol_dna=False).exists()
                pocet_poldni = prekrytia.filter(pol_dna=True).count()
                if ma_celodennu or pocet_poldni >= 2:
                    raise ValidationError("🚫 Tento zamestnanec už má v tomto termíne vyčerpanú celú pracovnú zmenu (celodenná alebo 2 poldenné absencie).")
            else:
                if prekrytia.exists():
                    raise ValidationError("🚫 Tento zamestnanec už má v tomto termíne zaevidovanú inú absenciu.")

    def save(self, *args, **kwargs):
        # Výpočet dní podľa typu absencie
        d_od = to_date(self.datum_od)
        d_do = to_date(self.datum_do)
        if d_od and d_do:
            if self.pol_dna:
                self.pocet_pracovnych_dni = 0.5
            else:
                dni = 0.0
                aktualny_den = d_od
                
                while aktualny_den <= d_do:
                    if self.typ == 'PN':
                        # PN sa počíta na KALENDÁRNE dni (vrátane víkendov)
                        dni += 1.0
                    else:
                        # Dovolenka a Paragraf sa počítajú na PRACOVNÉ dni (bez víkendov)
                        if aktualny_den.weekday() < 5:
                            dni += 1.0
                            
                    aktualny_den += timedelta(days=1)
                    
                self.pocet_pracovnych_dni = dni

        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'pocet_pracovnych_dni'}
            
        super().save(*args, **kwargs)


class HRDashboard(models.Model):
    """Falošný model slúžiaci len ako tlačidlo v menu pre vizuálny kalendár."""
    class Meta:
        managed = False # 🚀 Zabraňuje Djangu vytvoriť tabuľku v databáze
        verbose_name = "📊 HR Dashboard (Kalendár)"
        verbose_name_plural = "📊 HR Dashboard (Kalendár)"

class SignalDictionary(ISASAuditMixin, models.Model):
    """
    🧠 ISAS CORE: Prekladový slovník pre Modbus/OPC UA registre.
    Mapuje surové adresy (napr. 40001) na sémantické názvy signálov.
    """
    TYP_SIGNALU = [
        ('ANALOG', '📈 Analógová hodnota (Číslo)'),
        ('DIGITAL', '💡 Digitálny stav (Boolean)'),
    ]

    zariadenie = models.ForeignKey(
        'ServisovaneZariadenie', 
        on_delete=models.CASCADE, 
        related_name="slovnik_signalov",
        verbose_name="⚙️ Zariadenie",
        help_text="🔌 Zariadenie, ku ktorému tento Modbus/OPC UA register patrí."
    )
    register_adresa = models.CharField(
        "📍 Adresa registra", 
        max_length=50,
        help_text="📍 Surová adresa z PLC (napr. 40001, DB1.DBD4)."
    )
    nazov_signalu = models.CharField(
        "🏷️ Názov signálu", 
        max_length=100,
        help_text="🏷️ Ľudský názov pre zobrazenie na Dashboarde (napr. Primárna prevádzková veličina)."
    )
    typ_signalu = models.CharField(
        "🎛️ Typ signálu", 
        max_length=20, 
        choices=TYP_SIGNALU, 
        default='ANALOG',
        help_text="🎛️ Určuje, či ide o číselnú hodnotu (Analóg) alebo stav ZAP/VYP (Digitál)."
    )
    PRAVA_CHOICES = [
        ('R', '👁️ Iba na čítanie (Read-Only)'),
        ('RW', '✍️ Čítanie aj zápis (Read/Write)'),
    ]
    prava_pristupu = models.CharField(
        "🔐 Práva prístupu", 
        max_length=2, 
        choices=PRAVA_CHOICES, 
        default='R',
        help_text="🔐 Určuje, či je možné do tohto registra z ISAS CORE aj zapisovať."
    )
    merna_jednotka = models.CharField(
        "📏 Merná jednotka", 
        max_length=20, 
        blank=True, 
        help_text="📏 Merná jednotka veličiny podľa aktívneho odvetvia."
    )
    nasobic = models.DecimalField(
        "✖️ Násobič (Multiplier)", 
        max_digits=10, 
        decimal_places=4, 
        default=Decimal('1.0000'),
        help_text="✖️ Konštanta pre prepočet surovej hodnoty (napr. 0.1 pre posun desatinnej čiarky)."
    )

    class Meta:
        verbose_name = "📖 IoT Slovník signálov"
        verbose_name_plural = "📖 IoT Slovníky signálov"
        unique_together = ('zariadenie', 'register_adresa')

    def __str__(self):
        return f"{self.register_adresa} -> {self.nazov_signalu}"

class TelemetryHistory(models.Model):
    """
    🧠 ISAS CORE: Časový rad (Time-Series) pre telemetrické dáta.
    ZÁMERNE BEZ ISASAuditMixin - chránime databázu pred gigantickým Audit Logom.
    """
    zariadenie = models.ForeignKey(
        'ServisovaneZariadenie', 
        on_delete=models.CASCADE, 
        related_name="telemetria_historia",
        help_text="⚙️ Zariadenie, ktoré odoslalo tieto telemetrické dáta."
    )
    zakaznik = models.ForeignKey(
        'Zakaznik', 
        on_delete=models.CASCADE, 
        null=True,
        blank=True,
        related_name="telemetria_historia",
        help_text="🏢 Denormalizované pre bleskové filtrovanie na subdoménach."
    )
    casova_peciatka = models.DateTimeField(
        "⏱️ Čas záznamu", 
        default=timezone.now, 
        db_index=True,
        help_text="⏱️ Presný čas, kedy boli dáta prijaté zo zariadenia."
    )
    
    # 🚀 JSONB polia pre absolútnu flexibilitu (PostgreSQL 18 optimalizácia)
    analogove_hodnoty = models.JSONField(
        "📈 Analógové dáta", 
        default=dict,
        help_text="📈 Uložené ako Key-Value (telemetrické číselné metriky)."
    )
    digitalne_stavy = models.JSONField(
        "💡 Digitálne stavy", 
        default=dict,
        help_text="💡 Uložené ako Key-Value (binárne stavy senzorov a subsystémov)."
    )

    class Meta:
        verbose_name = "📊 Telemetrický záznam"
        verbose_name_plural = "📊 História telemetrie"
        ordering = ['-casova_peciatka']
        indexes = [
            # 🚀 BRIN Index je extrémne efektívny pre časové rady (šetrí RAM a disk)
            BrinIndex(fields=['casova_peciatka']),
            # 🚀 Kompozitný B-Tree index pre bleskové načítanie grafov na klientskom portáli
            models.Index(fields=['zakaznik', '-casova_peciatka']),
            # 🚀 Kompozitný B-Tree index pre zariadenie a čas (eliminácia Full Table Scan pri grafe)
            models.Index(fields=['zariadenie', '-casova_peciatka']),
        ]

    def __str__(self):
        return f"{self.zariadenie.vyrobne_cislo} - {self.casova_peciatka.strftime('%d.%m.%Y %H:%M:%S')}"

    def save(self, *args, **kwargs):
        # 🤖 AUTOMATIKA: Dedenie zákazníka pre zachovanie relačnej integrity bez JOINov
        if not self.zakaznik_id and self.zariadenie_id:
            self.zakaznik_id = self.zariadenie.zakaznik_id
        super().save(*args, **kwargs)

class AlarmLog(ISASAuditMixin, models.Model):
    """
    🧠 ISAS CORE: Čierna skrinka. Zaznamenáva kritické stavy a havárie.
    """
    SEVERITY_CHOICES = [
        ('WARNING', '⚠️ Varovanie (Warning)'),
        ('CRITICAL', '🚨 Kritická chyba (Trip/Fault)'),
    ]

    zariadenie = models.ForeignKey(
        'ServisovaneZariadenie', 
        on_delete=models.CASCADE, 
        related_name="alarmy",
        help_text="⚙️ Zariadenie, na ktorom vznikol tento alarm."
    )
    zakaznik = models.ForeignKey(
        'Zakaznik', 
        on_delete=models.CASCADE, 
        null=True,
        blank=True,
        related_name="alarmy",
        help_text="🏢 Zákazník (vlastník zariadenia) pre rýchle filtrovanie."
    )
    casova_peciatka = models.DateTimeField(
        "⏱️ Čas vzniku", 
        default=timezone.now, 
        db_index=True,
        help_text="⏱️ Presný čas, kedy riadiaca jednotka nahlásila chybu."
    )
    zavaznost = models.CharField(
        "🔥 Závažnosť", 
        max_length=20, 
        choices=SEVERITY_CHOICES,
        help_text="🔥 Úroveň kritickosti (Varovanie vs. Odstavenie stroja)."
    )
    kod_chyby = models.CharField(
        "🔢 Kód chyby", 
        max_length=50, 
        blank=True,
        help_text="🔢 Originálny chybový kód z riadiacej jednotky (napr. E04, F.012)."
    )
    popis = models.TextField(
        "📝 Popis alarmu",
        help_text="📝 Preložený text alarmu pre dispečera a zákazníka."
    )
    snapshot_dat = models.JSONField(
        "📸 Snapshot dát (Čierna skrinka)", 
        default=dict,
        help_text="📸 Zmrazený stav všetkých senzorov v milisekunde vzniku alarmu."
    )
    vyriesene = models.BooleanField(
        "✅ Vyriešené", 
        default=False,
        help_text="✅ Označí dispečer alebo technik po odstránení poruchy."
    )

    class Meta:
        verbose_name = "🚨 Alarm / Čierna skrinka"
        verbose_name_plural = "🚨 Alarmy a výpadky"
        ordering = ['-casova_peciatka']
        indexes = [
            models.Index(fields=['zakaznik', 'vyriesene', '-casova_peciatka']),
        ]

    def __str__(self):
        return f"[{self.zavaznost}] {self.zariadenie.vyrobne_cislo} - {self.kod_chyby}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if not self.zakaznik_id and self.zariadenie_id:
            self.zakaznik_id = self.zariadenie.zakaznik_id
        super().save(*args, **kwargs)

        # 🚀 ISAS WEBHOOK TRIGGER (Odpálenie do Celery pri novom alarme)
        if is_new and self.zakaznik_id:
            from WebBuilder.tasks import process_alarm_webhooks_task
            def _safe_alarm_webhook(doc_pk):
                try:
                    process_alarm_webhooks_task.delay(doc_pk)
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre IoT alarm webhook {doc_pk}: {exc}")

            transaction.on_commit(lambda pk=self.pk: _safe_alarm_webhook(pk))

class ModbusKonfigurator(SignalDictionary):
    """
    🧠 ISAS CORE: Proxy model pre vytvorenie vlastnej položky v Admin menu.
    Neotvára changelist, ale priamo našu VIVID SPA stránku.
    """
    class Meta:
        proxy = True
        verbose_name = "📡 Telemetrický slovník registrov (Modbus / IoT)"
        verbose_name_plural = "📡 Telemetrický slovník registrov (Modbus / IoT)"

class PracovnaPozicia(ISASAuditMixin, models.Model):
    TYP_UVAZKU_CHOICES = [
        ('TPP', '💼 Trvalý pracovný pomer (TPP)'),
        ('ZIVNOST', '📑 Živnosť / Zmluva (IČO)'),
        ('DOHODA', '⏱️ Dohoda / Brigáda'),
        ('DOHODOU', '🤝 Podľa dohody (TPP / Živnosť)'),
    ]

    nazov = models.CharField("🏷️ Názov pozície", max_length=150, help_text="Napr. Servisný technik / Špecialista podpory")
    slug = models.SlugField("🔗 URL Slug", max_length=160, unique=True, blank=True, null=True, help_text="Generuje sa automaticky pre priame zdieľanie inzerátu.")

    cielova_rola = models.CharField(
        "👔 Pracovná rola v ERP", max_length=2, 
        choices=Zamestnanec.ROLA_CHOICES, 
        default=Zamestnanec.TECHNIK,
        help_text="👔 Určuje systémovú rolu, ktorú kandidát dostane pri automatickom vytvorení v ERP."
    )
    
    typ_uvazku = models.CharField("📋 Typ úväzku", max_length=20, choices=TYP_UVAZKU_CHOICES, default='TPP')
    mzda = models.CharField("💰 Ponúkaná mzda", max_length=100, help_text="Napr. '1 800 - 2 500 / mesiac + variabilná zložka'")
    lokalita = models.CharField("📍 Miesto výkonu práce", max_length=100, blank=True, default="")
    nastup = models.CharField("📅 Termín nástupu", max_length=100, default="Ihneď / Dohodou")
    
    # ⚡ Rýchle predajné argumenty pre karty na webe
    poziadavky_skratka = models.CharField("⚡ Kľúčové požiadavky (v skratke)", max_length=255, blank=True, help_text="Napr. 'Odborná elektrotechnická spôsobilosť, Vodičský preukaz sk. B, Prax 2 roky'")
    benefity_skratka = models.CharField("🎁 Hlavné benefity (v skratke)", max_length=255, blank=True, help_text="Napr. 'Služobné auto aj na súkromie, 13. plat, špičkové náradie'")
    
    # Detailný popis inzerátu
    popis_naplne = models.TextField("📝 Náplň práce a zodpovednosti", help_text="Čo bude technik u nás reálne robiť.")
    poziadavky_detail = models.TextField("🎓 Podrobné požiadavky na kandidáta", blank=True, help_text="Vzdelanie, certifikáty, osobnostné predpoklady.")
    ponukame_detail = models.TextField("✨ Čo všetko ti ponúkame", blank=True, help_text="Kompletný zoznam benefitov, firemná kultúra.")
    
    platnost_do = models.DateField("📅 Platnosť inzerátu do", blank=True, null=True, help_text="Po tomto dátume sa inzerát automaticky prestane zobrazovať na webe.")
    je_aktivna = models.BooleanField("🟢 Zobraziť na webe (Aktívny nábor)", default=True)
    poradie = models.PositiveIntegerField("🔢 Poradie zobrazenia", default=10)
    vytvorene = models.DateTimeField("📅 Dátum vytvorenia", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "🤝 Pracovná pozícia"
        verbose_name_plural = "🤝 Pracovné pozície"
        ordering = ['poradie', '-id']

    @property
    def je_aktualna(self):
        """Pragmatická kontrola: aktívna a neexpirovaná."""
        from django.utils import timezone
        if not self.je_aktivna:
            return False
        if self.platnost_do and self.platnost_do < timezone.now().date():
            return False
        return True

    def __str__(self):
        stav = "🟢" if self.je_aktivna else "🔴"
        return f"{stav} {self.nazov} ({self.get_typ_uvazku_display()})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.nazov)[:150] or "pozicia"
            candidate_slug = base_slug
            while PracovnaPozicia.objects.filter(slug=candidate_slug).exclude(pk=self.pk).exists():
                candidate_slug = f"{base_slug[:150]}-{uuid.uuid4().hex[:6]}"
            self.slug = candidate_slug
        super().save(*args, **kwargs)


class Kandidat(ISASAuditMixin, ISASGDPRMixin, models.Model):
    STAV_CHOICES = [
        ('NOVY', '🆕 Nový uchádzač (Neotvorený)'),
        ('KONTAKTOVANY', '📞 Telefonický predvýber'),
        ('POHOVOR', '🤝 Pozvaný na osobný pohovor'),
        ('PRIJATY', '🎉 Prijatý do tímu'),
        ('ZAMIETNUTY', '❌ Nezhoda / Odmietnutý'),
        ('ARCHIV', '📁 V databáze talentov (Budúcnosť)'),
    ]

    pozicia = models.ForeignKey(
        PracovnaPozicia, on_delete=models.SET_NULL, null=True, blank=True, 
        related_name="uchadzaci", verbose_name="🎯 Vybraná pozícia"
    )
    vytvoreny_zamestnanec = models.OneToOneField(
        'Zamestnanec', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='povodny_kandidat', verbose_name="👨‍💼 Vytvorený zamestnanec v ERP",
        help_text="🔗 Prepojenie na kartu zamestnanca po úspešnom prijatí a nábore."
    )
    stav = models.CharField("🚥 Stav výberového konania", max_length=20, choices=STAV_CHOICES, default='NOVY')
    hodnotenie = models.PositiveSmallIntegerField(
        "⭐ Hodnotenie kandidáta (1-5)", null=True, blank=True,
        help_text="Interné hodnotenie po pohovore (1 = najslabší, 5 = okamžite brať)."
    )

    # Kontaktné údaje
    meno = models.CharField("👤 Meno a priezvisko", max_length=150)
    email = models.EmailField("📧 E-mail")
    telefon = models.CharField("📞 Telefón", max_length=50)
    
    # Prílohy a správa
    zivotopis = models.FileField("📄 Životopis (CV / Dokument)", upload_to="kariera/zivotopisy/%Y/%m/")
    motivacna_sprava = models.TextField("📝 Správa / Motivácia kandidáta", blank=True, null=True)
    
    datum_pohovoru = models.DateTimeField("📅 Termín osobného/online pohovoru", blank=True, null=True)

    # 🔒 Interné dáta pre vedenie / HR
    interna_poznamka = models.TextField(
        "🔒 Interná poznámka majiteľa/HR", blank=True, null=True,
        help_text="Dôverné poznámky z pohovoru, mzdové očakávania, dôvody odchodu z predošlej práce..."
    )
    
    # ⚖️ GDPR a bezpečnosť
    gdpr_suhlas = models.BooleanField("⚖️ GDPR Súhlas", default=True)
    suhlas_do = models.DateField("⚖️ GDPR súhlas platný do", blank=True, null=True, help_text="Dátum, do ktorého má firma právo uchovávať životopis (štandardne 2 roky).")
    ip_adresa = models.GenericIPAddressField("🌐 IP adresa odoslania", null=True, blank=True)

    # 🕒 Časové pečiatky
    vytvorene = models.DateTimeField("📅 Vytvorené", auto_now_add=True, db_index=True)
    upravene = models.DateTimeField("⏱️ Upravené", auto_now=True)

    class Meta:
        verbose_name = "🎓 Uchádzač o zamestnanie"
        verbose_name_plural = "🎓 Uchádzači o zamestnanie"
        ordering = ['-vytvorene']

    @classmethod
    def from_db(cls, db, field_names, values, fetch_mode=None):
        instance = super().from_db(db, field_names, values, fetch_mode=fetch_mode)
        instance._povodny_stav = dict(zip(field_names, values))
        return instance

    def preklop_na_zamestnanca(self, typ_pomeru=None, pracovna_rola=None, uloz_kandidata=True):
        """
        🧠 ISAS ONBOARDING ENGINE: Bezpečne preklopí prijatého kandidáta na zamestnanca.
        Dynamicky odvodí rolu (Technik/Skladník/Dispečer/Admin/Obchodník) aj pomer (TPP/Živnosť/Dohoda) z inzerátu.
        """
        if self.vytvoreny_zamestnanec:
            return self.vytvoreny_zamestnanec

        # 1. Čistenie akademických titulov pred a za menom
        ciste_meno = re.sub(r'(?i)\b(ing\.|bc\.|mgr\.|mudr\.|mvdr\.|judr\.|phdr\.|rndr\.|doc\.|prof\.|phd\.|mba|ll\.m\.)\b', '', self.meno or '')
        ciste_meno = re.sub(r'[,.]', '', ciste_meno).strip()
        casti = ciste_meno.split()

        krstne_meno = casti[0] if casti else "Kandidát"
        priezvisko = " ".join(casti[1:]) if len(casti) > 1 else "(Doplniť)"

        # 2. 🧠 DYNAMICKÉ ODVODENIE ROLY A TYPU POMERU Z INZERÁTU
        final_typ_pomeru = typ_pomeru
        if not final_typ_pomeru:
            if self.pozicia:
                mapa_pomerov = {'TPP': 'IN', 'ZIVNOST': 'EX', 'DOHODA': 'BR', 'DOHODOU': 'IN'}
                final_typ_pomeru = mapa_pomerov.get(self.pozicia.typ_uvazku, 'IN')
            else:
                final_typ_pomeru = 'IN'

        final_pracovna_rola = pracovna_rola
        if not final_pracovna_rola:
            if self.pozicia and getattr(self.pozicia, 'cielova_rola', None):
                final_pracovna_rola = self.pozicia.cielova_rola
            else:
                final_pracovna_rola = 'TE'  # Štandardný fallback na servisného technika

        # 3. Ochrana pred duplicitným vytvorením zamestnanca s rovnakým e-mailom (rešpektuje 1:1 integritu)
        if self.email:
            kandidat_q = models.Q(povodny_kandidat__isnull=True)
            if self.pk:
                kandidat_q |= models.Q(povodny_kandidat=self)
            zamestnanec = Zamestnanec.objects.filter(email=self.email).filter(kandidat_q).first()
        else:
            zamestnanec = None
        if not zamestnanec:
            novy_subor_zmluvy = None
            if self.zivotopis:
                try:
                    self.zivotopis.open()
                    novy_subor_zmluvy = ContentFile(self.zivotopis.read(), name=os.path.basename(self.zivotopis.name))
                except Exception:
                    novy_subor_zmluvy = None
                finally:
                    try:
                        self.zivotopis.close()
                    except Exception:
                        pass

            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            identita = getattr(self, 'firma', None) or getattr(self, 'firemna_identita', None) or FiremnaIdentitaModel.objects.first()
            stat_firmy = getattr(identita, 'sidlo_stat', '') or ''

            zamestnanec = Zamestnanec.objects.create(
                meno=krstne_meno[:100],
                priezvisko=priezvisko[:100],
                email=self.email,
                telefon=self.telefon or '',
                typ_pomeru=final_typ_pomeru,
                pracovna_rola=final_pracovna_rola,
                stat=stat_firmy[:50],
                datum_nastupu=timezone.now().date(),
                zmluva_subor=novy_subor_zmluvy
            )

        self.vytvoreny_zamestnanec = zamestnanec
        self.stav = 'PRIJATY'
        if uloz_kandidata and self.pk:
            self.save(update_fields=['vytvoreny_zamestnanec', 'stav'])
        return zamestnanec

    def save(self, *args, **kwargs):
        # 1. Automatické nastavenie GDPR retencie na 2 roky pri prvom vytvorení
        if not self.suhlas_do and not self.pk:
            self.suhlas_do = timezone.now().date() + timedelta(days=730)

        # 2. 🚀 AUTOMATICKÝ ONBOARDING: Ak HR nastavilo stav 'PRIJATY', automaticky vytvoríme zamestnanca
        if self.stav == 'PRIJATY' and not self.vytvoreny_zamestnanec:
            self.preklop_na_zamestnanca(uloz_kandidata=False)

        # 3. Detekcia zmeny stavu pre automatické e-mailové notifikácie
        povodny_stav_dict = getattr(self, '_povodny_stav', {})
        stary_stav = povodny_stav_dict.get('stav') if isinstance(povodny_stav_dict, dict) else None
        stav_sa_zmenil = (stary_stav is not None and stary_stav != self.stav)

        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'suhlas_do', 'vytvoreny_zamestnanec', 'stav'}

        super().save(*args, **kwargs)

        # 4. Odoslanie notifikácie kandidátovi po úspešnom uložení
        if stav_sa_zmenil and self.email:
            from Evidencia.tasks import task_notifikuj_kandidata_o_stave
            def _safe_kandidat_notif(doc_pk, s, n):
                try:
                    task_notifikuj_kandidata_o_stave.delay(doc_pk, s, n)
                except Exception as exc:
                    logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre notifikáciu kandidáta {doc_pk}: {exc}")

            transaction.on_commit(lambda pk=self.pk, s=stary_stav, n=self.stav: _safe_kandidat_notif(pk, s, n))

        # 5. Synchronizácia in-memory stavu pre opakované uloženia
        if not hasattr(self, '_povodny_stav') or not isinstance(self._povodny_stav, dict):
            self._povodny_stav = {}
        self._povodny_stav['stav'] = self.stav

    def __str__(self):
        pozicia_nazov = self.pozicia.nazov if self.pozicia else "Všeobecný záujem"
        return f"[{self.get_stav_display()}] {self.meno} – {pozicia_nazov}"