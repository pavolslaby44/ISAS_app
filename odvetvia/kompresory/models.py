from decimal import Decimal
from django.db import models, transaction
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.cache import cache
from django.apps import apps

# 1. Bázické modely a mixiny z jadra ERP
from Evidencia.models import (
    ISASAuditMixin,
    ISAS_ZakladSkladu,
    ISAS_DielSTypom,
    MERNE_JEDNOTKY_CHOICES,
)

# 2. Univerzálne kalendárne utilitky z jadra
from Evidencia.utils_compliance import (
    to_date,
    add_calendar_interval,
    get_compliance_config,
    get_iso_pre_objekt,
)


# ==============================================================================
# 🏢 SKLADOVÉ SEKCIE (3 MODELY)
# ==============================================================================

class Stroj(ISASAuditMixin, models.Model):
    sklad = models.ForeignKey(
        'Evidencia.Sklad',
        on_delete=models.CASCADE,
        related_name="stroje",
        verbose_name="🏢 Sklad",
        help_text="🏢 Priradenie k materskému skladu pre evidenciu majetku, inventarizáciu a sledovanie lokácie stroja.",
    )

    class Meta:
        db_table = 'Evidencia_stroj'
        verbose_name = "⚙️ Skladová sekcia - Stroj"
        verbose_name_plural = "⚙️ Skladové sekcie - Stroje"

    def __str__(self):
        nazov_skladu = getattr(self.sklad, 'nazov', str(self.sklad)) if self.sklad_id else "Bez skladu"
        return f"({nazov_skladu})"


class SpotrebnyMaterial(ISASAuditMixin, models.Model):
    sklad = models.ForeignKey(
        'Evidencia.Sklad',
        on_delete=models.CASCADE,
        related_name="materialy",
        verbose_name="🏢 Sklad",
        help_text="🏢 Sklad, v ktorom je evidovaný a fyzicky naskladnený tento spotrebný materiál.",
    )

    class Meta:
        db_table = 'Evidencia_spotrebnymaterial'
        verbose_name = "🔩 Skladová sekcia - Spotrebný materiál"
        verbose_name_plural = "🔩 Skladové sekcie - Spotrebný materiál"

    def __str__(self):
        nazov_skladu = getattr(self.sklad, 'nazov', str(self.sklad)) if self.sklad_id else "Bez skladu"
        return f"({nazov_skladu})"


class Poruchovydiel(ISASAuditMixin, models.Model):
    sklad = models.ForeignKey(
        'Evidencia.Sklad',
        on_delete=models.CASCADE,
        related_name="diely",
        verbose_name="🏢 Sklad",
        help_text="🏢 Skladová sekcia náhradných komponentov a dielov určených na opravy porúch, havárií a generálne opravy (korektívna údržba).",
    )

    class Meta:
        db_table = 'Evidencia_poruchovydiel'
        verbose_name = "🚨 Skladová sekcia - Poruchový náhradný diel (Opravy)"
        verbose_name_plural = "🚨 Skladové sekcie - Poruchové náhradné diely (Opravy)"

    def __str__(self):
        nazov_skladu = getattr(self.sklad, 'nazov', str(self.sklad)) if self.sklad_id else "Bez skladu"
        return f"({nazov_skladu})"


# ==============================================================================
# ⚙️ SKLAD: STROJE A TECHNOLÓGIE (6 MODELOV)
# ==============================================================================

class StrojGA(ISAS_ZakladSkladu):
    stroj = models.ForeignKey("Stroj", on_delete=models.CASCADE, related_name="ga_stroje", verbose_name="⚙️ Priradený stroj", help_text="⚙️ Väzba na hlavnú kartu stroja pre sledovanie motohodín, servisnej histórie a SLA zmlúv.")

    class Meta:
        db_table = 'Evidencia_strojga'
        verbose_name = "🌀 Skrutkový kompresor (Mazaný olejom - fixné otáčky)"
        verbose_name_plural = "🌀 Skrutkové kompresory (Mazaný olejom - fixné otáčky)"


class StrojGAVSD(ISAS_ZakladSkladu):
    stroj = models.ForeignKey("Stroj", on_delete=models.CASCADE, related_name="gavsd_stroje", verbose_name="⚙️ Priradený stroj", help_text="🔗 Prepojenie na konkrétne zariadenie v evidencii strojov pre sledovanie servisu a motohodín.")

    class Meta:
        db_table = 'Evidencia_strojgavsd'
        verbose_name = "⚡ Skrutkový kompresor s frekvenčným meničom (VSD)"
        verbose_name_plural = "⚡ Skrutkové kompresory s frekvenčným meničom (VSD)"


class StrojZRZT(ISAS_ZakladSkladu):
    stroj = models.ForeignKey("Stroj", on_delete=models.CASCADE, related_name="zrzt_stroje", verbose_name="⚙️ Priradený stroj", help_text="⚙️ Hlavný profil stroja v evidencii, ku ktorému prislúcha táto špecifikácia a servisná história.")

    class Meta:
        db_table = 'Evidencia_strojzrzt'
        verbose_name = "🍃 Bezolejový kompresor (Oil-Free Class 0)"
        verbose_name_plural = "🍃 Bezolejové kompresory (Oil-Free Class 0)"


class StrojPiestovy(ISAS_ZakladSkladu):
    stroj = models.ForeignKey("Stroj", on_delete=models.CASCADE, related_name="piestove_stroje", verbose_name="⚙️ Priradený stroj", help_text="⚙️ Väzba na hlavnú kartu stroja pre evidenciu a predaj.")

    class Meta:
        db_table = 'Evidencia_strojpiestovy'
        verbose_name = "⚙️ Piestový kompresor (Dielenský / Vysokotlakový)"
        verbose_name_plural = "⚙️ Piestové kompresory"


class StrojSusicka(ISAS_ZakladSkladu):
    stroj = models.ForeignKey("Stroj", on_delete=models.CASCADE, related_name="susicky_stroje", verbose_name="⚙️ Priradený stroj", help_text="⚙️ Väzba na kartu zariadenia v sklade nových/rental strojov.")

    class Meta:
        db_table = 'Evidencia_strojsusicka'
        verbose_name = "💧 Sušička vzduchu (Kondenzačná / Adsorpčná)"
        verbose_name_plural = "💧 Sušičky vzduchu"


class StrojVzdusnik(ISAS_ZakladSkladu):
    stroj = models.ForeignKey("Stroj", on_delete=models.CASCADE, related_name="vzdusniky_stroje", verbose_name="⚙️ Priradený stroj", help_text="⚙️ Skladová evidencia nových a repasovaných tlakových nádob.")

    class Meta:
        db_table = 'Evidencia_strojvzdusnik'
        verbose_name = "🛢️ Tlaková nádoba / Vzdušník"
        verbose_name_plural = "🛢️ Tlakové nádoby / Vzdušníky"


# ==============================================================================
# 🔧 SKLAD: SPOTREBNÉ NÁHRADNÉ DIELY (22 MODELOV)
# ==============================================================================

class SpotrebnyOlej(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="oleje", verbose_name="📦 Materiál", help_text="📦 Väzba na kmeňovú kartu spotrebného materiálu pre evidenciu zásoby a kalkuláciu servisných nákladov.")
    merna_jednotka = models.CharField("⚖️ Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='l', help_text="⚖️ Merná jednotka použitá pre presné dávkovanie, fakturáciu a skladové odpisy.")

    class Meta:
        db_table = 'Evidencia_spotrebnyolej'
        verbose_name = "🛢️ Olej"
        verbose_name_plural = "🛢️ Oleje"


class SpotrebnyVzduchovyFilter(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="vzduchove_filtre", verbose_name="📦 Materiál", help_text="📦 Odkaz na materskú kartu spotrebného materiálu pre správne zúčtovanie, evidenciu skladových zásob a nákupných cien.")

    class Meta:
        db_table = 'Evidencia_spotrebnyvzduchovyfilter'
        verbose_name = "💨 Vzduchový filter"
        verbose_name_plural = "💨 Vzduchové filtre"


class SpotrebneVzduchoveVlozky(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="vzduchove_vlozky", verbose_name="📦 Materiál", help_text="📦 Odkaz na materskú položku spotrebného materiálu pre správnu evidenciu skladových zásob a účtovanie nákladov.")

    class Meta:
        db_table = 'Evidencia_spotrebnevzduchovevlozky'
        verbose_name = "🌪️ Filter stlačeného vzduchu"
        verbose_name_plural = "🌪️ Filtre stlačeného vzduchu"


class SpotrebnyOlejovyFilter(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="olejove_filtre", verbose_name="📦 Materiál", help_text="📦 Odkaz na materskú kartu spotrebného materiálu pre riadenie skladových zásob, nákupných cien a účtovných predkontácií.")

    class Meta:
        db_table = 'Evidencia_spotrebnyolejovyfilter'
        verbose_name = "🛢️ Olejový filter"
        verbose_name_plural = "🛢️ Olejové filtre"


class SpotrebnyOlejovySeparator(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="olejove_separatory", verbose_name="📦 Materiál", help_text="🔗 Odkaz na kmeňovú kartu spotrebného materiálu pre evidenciu skladových zásob, nákupných cien a automatické účtovné odpisy.")

    class Meta:
        db_table = 'Evidencia_spotrebnyolejovyseparator'
        verbose_name = "⚗️ Olejový separátor"
        verbose_name_plural = "⚗️ Olejové separátory"


class SpotrebnyPalivovyFilter(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="palivove_filtre", verbose_name="📦 Materiál", help_text="📦 Odkaz na kmeňovú kartu spotrebného materiálu pre evidenciu skladových zásob a účtovanie nákladov.")

    class Meta:
        db_table = 'Evidencia_spotrebnypalivovyfilter'
        verbose_name = "⛽ Palivový filter"
        verbose_name_plural = "⛽ Palivové filtre"


class SpotrebnyKlinovyRemen(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="klinove_remene", verbose_name="📦 Materiál", help_text="📦 Prepojenie na hlavnú kartu spotrebného materiálu pre presnú evidenciu skladových zásob a sledovanie nákladov.")

    class Meta:
        db_table = 'Evidencia_spotrebnyklinovyremen'
        verbose_name = "⚙️ Klinový remeň"
        verbose_name_plural = "⚙️ Klinové remene"


class SpotrebnaSpojka(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="spojky", verbose_name="📦 Materiál", help_text="📦 Priradený spotrebný materiál pre presnú evidenciu skladových zásob a účtovanie nákladov.")

    class Meta:
        db_table = 'Evidencia_spotrebnaspojka'
        verbose_name = "🔗 Spojka"
        verbose_name_plural = "🔗 Spojky"


class SpotrebnaSaciaKlapka(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="sacie_klapky", verbose_name="📦 Materiál", help_text="📦 Odkaz na hlavnú kartu spotrebného materiálu pre správu skladových zásob, sledovanie nákladov a automatické doobjednávanie.")

    class Meta:
        db_table = 'Evidencia_spotrebnasaciaklapka'
        verbose_name = "🎛️ Sacia klapka"
        verbose_name_plural = "🎛️ Sacie klapky"


class SpotrebnePotrubie(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="potrubia", verbose_name="📦 Materiál", help_text="📦 Odkaz na špecifický spotrebný materiál rozvodu pre presnú evidenciu zásoby a kalkuláciu nákladov.")
    merna_jednotka = models.CharField("⚖️ Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='m', help_text="📏 Merná jednotka použitá pri výpočte spotreby, odpisoch zo skladu a fakturácii.")

    class Meta:
        db_table = 'Evidencia_spotrebnepotrubie'
        verbose_name = "🚰 Rozvodný systém stlačeného vzduchu"
        verbose_name_plural = "🚰 Rozvodné systémy stlačeného vzduchu"


class SpotrebnaServisnaSada(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="servisne_sady", verbose_name="📦 Materiál", help_text="📦 Spotrebný materiál pridelený k servisnej sade pre štandardizované normodiny a údržbu.")
    merna_jednotka = models.CharField("⚖️ Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='sada', help_text="📏 Merná jednotka použitá pri vyskladnení a účtovnom odpise spotreby tejto položky.")

    class Meta:
        db_table = 'Evidencia_spotrebnaservisnasada'
        verbose_name = "🧰 Servisná sada"
        verbose_name_plural = "🧰 Servisné sady"


class SpotrebneTesnenie(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="tesnenia", verbose_name="📦 Materiál", help_text="📦 Hlavný spotrebný materiál pre evidenciu skladových zásob, cenníkov a účtovných nákladov.")

    class Meta:
        db_table = 'Evidencia_spotrebnetesnenie'
        verbose_name = "⭕ Tesnenie"
        verbose_name_plural = "⭕ Tesnenia"


class SpotrebnyOdkalovac(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="odkalovace", verbose_name="📦 Materiál", help_text="📦 Viazaná položka spotrebného materiálu zo skladu pre presnú evidenciu spotreby a nákladov.")

    class Meta:
        db_table = 'Evidencia_spotrebnyodkalovac'
        verbose_name = "💧 Odkalovač"
        verbose_name_plural = "💧 Odkalovače"


class SpotrebnySeparatorKondenzatu(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="separatory_kondenzatu", verbose_name="📦 Materiál", help_text="📦 Odkaz na materský spotrebný materiál pre presnú evidenciu skladových zásob a cenotvorbu.")

    class Meta:
        db_table = 'Evidencia_spotrebnyseparatorkondenzatu'
        verbose_name = "🚰 Separátor kondenzátu"
        verbose_name_plural = "🚰 Separátory kondenzátu"


class SpotrebnyDemister(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="demistre", verbose_name="📦 Materiál", help_text="📦 Odkaz na kmeňový záznam spotrebného materiálu pre účely evidencie skladových zásob a presného zúčtovania nákladov.")

    class Meta:
        db_table = 'Evidencia_spotrebnydemister'
        verbose_name = "🌫️ Demister (Odlučovač hmloviny)"
        verbose_name_plural = "🌫️ Demistre (Odlučovače hmloviny)"


class SpotrebnyTermostat(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="termostaty", verbose_name="📦 Materiál", help_text="📦 Odkaz na konkrétnu položku spotrebného materiálu v sklade pre evidenciu zásob a nákupných cien.")

    class Meta:
        db_table = 'Evidencia_spotrebnytermostat'
        verbose_name = "🌡️ Termostat"
        verbose_name_plural = "🌡️ Termostaty"


class SpotrebnaSadaVentilov(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="sady_ventilov", verbose_name="📦 Materiál", help_text="📦 Odkaz na materský spotrebný materiál pre správne účtovanie, evidenciu zásob a kalkuláciu nákladov.")
    merna_jednotka = models.CharField("⚖️ Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='sada', help_text="⚖️ Merná jednotka pre fakturáciu, skladový odpis a presnú evidenciu spotreby.")

    class Meta:
        db_table = 'Evidencia_spotrebnasadaventilov'
        verbose_name = "🎛️ Sada ventilov (Spätné, Oilstop, VMT)"
        verbose_name_plural = "🎛️ Sady ventilov (Spätné, Oilstop, VMT)"


class SpotrebnyFilterOdvzdusneniaPrevodovky(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="filtre_odvzdusnenia_prevodovky", verbose_name="📦 Materiál", help_text="📦 Odkaz na skladovú kartu spotrebného materiálu pre presnú evidenciu nákladov a automatické odpisovanie zo zásob.")

    class Meta:
        db_table = 'Evidencia_spotrebnyfilterodvzdusneniaprevodovky'
        verbose_name = "🌬️ Filter odvzdušnenia prevodovky"
        verbose_name_plural = "🌬️ Filtre odvzdušnenia prevodovky"


class SpotrebneChladivo(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="chladiva", verbose_name="📦 Materiál", help_text="📦 Skladová fľaša s chladivom pre sušičky a chillery.")
    merna_jednotka = models.CharField("⚖️ Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='kg')

    class Meta:
        db_table = 'Evidencia_spotrebnechladivo'
        verbose_name = "❄️ Chladivo (R134a, R410A, R32, R407C, R513A)"
        verbose_name_plural = "❄️ Chladivá"


class SpotrebnyDesikant(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="desikanty", verbose_name="📦 Materiál", help_text="📦 Náplň sušiva pre adsorpčné sušičky a generátory plynov.")
    merna_jednotka = models.CharField("⚖️ Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='kg')

    class Meta:
        db_table = 'Evidencia_spotrebnydesikant'
        verbose_name = "🏜️ Sušivo / Desikant pre adsorpčné sušičky"
        verbose_name_plural = "🏜️ Sušivá / Desikanty"


class SpotrebnaChemia(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="chemia", verbose_name="📦 Materiál", help_text="📦 Servisná chémia, odmasťovače, testery netesností, čističe.")
    merna_jednotka = models.CharField("⚖️ Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='l')

    class Meta:
        db_table = 'Evidencia_spotrebnachemia'
        verbose_name = "🧪 Servisná chémia a spreje"
        verbose_name_plural = "🧪 Servisná chémia a spreje"


class SpotrebneOstatne(ISAS_DielSTypom):
    material = models.ForeignKey("SpotrebnyMaterial", on_delete=models.CASCADE, related_name="ostatne_diely", verbose_name="📦 Materiál", help_text="📦 Výber konkrétneho spotrebného materiálu zo skladu pre presnú evidenciu nákladov a automatický odpočet zo skladových zásob.")

    class Meta:
        db_table = 'Evidencia_spotrebneostatne'
        verbose_name = "🧩 Ostatný diel - spotrebný náhradný"
        verbose_name_plural = "🧩 Ostatné diely - spotrebné náhradné"


# ==============================================================================
# 🚨 SKLAD: PORUCHOVÉ NÁHRADNÉ DIELY (10 MODELOV)
# ==============================================================================

class PoruchovySnimacKabelaz(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="snimace_kabelaze", verbose_name="🔧 Poruchový diel", help_text="🔗 Nadradený poruchový diel, ku ktorému prislúcha tento snímač alebo kabeláž pre presnú evidenciu servisu a kalkuláciu nákladov.")

    class Meta:
        db_table = 'Evidencia_poruchovysnimackabelaz'
        verbose_name = "🔌 Snímač + kábel"
        verbose_name_plural = "🔌 Snímače + kabeláž"


class PoruchovaElektrovyzbroj(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="elektrovyzbroje", verbose_name="🔧 Poruchový diel", help_text="🔗 Väzba na konkrétny poruchový diel pre presnú evidenciu a diagnostiku elektrovýzbroje v servise.")

    class Meta:
        db_table = 'Evidencia_poruchovaelektrovyzbroj'
        verbose_name = "⚡ Elektrovýzbroj"
        verbose_name_plural = "⚡ Elektrovýzbroje"


class PoruchovaHadica(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="hadice", verbose_name="🔧 Poruchový diel", help_text="🔗 Nadriadený poruchový diel, ku ktorému je táto špecifická hadica priradená.")
    merna_jednotka = models.CharField(verbose_name="📏 Jednotka", max_length=10, choices=MERNE_JEDNOTKY_CHOICES, default='m', help_text="📏 Merná jednotka pre presné účtovanie spotreby materiálu a skladovú evidenciu.")

    class Meta:
        db_table = 'Evidencia_poruchovahadica'
        verbose_name = "🚰 Hadica"
        verbose_name_plural = "🚰 Hadice"


class PoruchovyRiadiaciModul(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="riadiace_moduly", verbose_name="🔧 Poruchový diel", help_text="🧩 Väzba na konkrétny poruchový diel pre evidenciu opravy, reklamácií a presnú identifikáciu komponentu.")

    class Meta:
        db_table = 'Evidencia_poruchovyriadiacimodul'
        verbose_name = "🎛️ Riadiaci modul"
        verbose_name_plural = "🎛️ Riadiace moduly"


class PoruchovyVentilatorMotor(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="ventilatory_motory", verbose_name="🔧 Poruchový diel", help_text="🔗 Odkaz na nadriadený poruchový diel pre presnú evidenciu a diagnostiku konkrétneho ventilátora alebo elektromotora.")

    class Meta:
        db_table = 'Evidencia_poruchovyventilatormotor'
        verbose_name = "🌀 Ventilátor a elektromotor"
        verbose_name_plural = "🌀 Ventilátory a elektromotory"


class PoruchovyElement(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="element", verbose_name="🔧 Poruchový diel", help_text="⚙️ Priradenie konkrétneho elementu k nadradenému poruchovému dielu pre presnú diagnostiku a evidenciu servisných zásahov.")

    class Meta:
        db_table = 'Evidencia_poruchovyelement'
        verbose_name = "🔩 Skrutkový element"
        verbose_name_plural = "🔩 Skrutkové elementy"


class PoruchovySolenoid(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="solenoidy", verbose_name="🔧 Poruchový diel", help_text="🔗 Nadradený poruchový diel, ku ktorému je tento solenoid priradený pre presnú identifikáciu opravy.")

    class Meta:
        db_table = 'Evidencia_poruchovysolenoid'
        verbose_name = "🧲 Solenoid"
        verbose_name_plural = "🧲 Solenoidy"


class PoruchoveLozisko(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="loziska", verbose_name="🔧 Poruchový diel", help_text="⚙️ Priradenie k hlavnému poruchovému dielu v evidencii servisu.")

    class Meta:
        db_table = 'Evidencia_poruchovelozisko'
        verbose_name = "🔘 Ložisko"
        verbose_name_plural = "🔘 Ložiská"


class PoruchovyChladicVymenik(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="chladice_vymeniky", verbose_name="🔧 Poruchový diel")

    class Meta:
        db_table = 'Evidencia_poruchovychladicvymenik'
        verbose_name = "🧊 Chladič oleja / vzduchu (Kombinovaný blok výmenníka)"
        verbose_name_plural = "🧊 Chladiče oleja a vzduchu (Výmenníky)"


class PoruchoveOstatne(ISAS_DielSTypom):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="ostatne_diely", verbose_name="⚙️ Priradený diel", help_text="🔗 Nadradený poruchový diel, ku ktorému prislúcha táto doplňujúca špecifikácia alebo položka.")

    class Meta:
        db_table = 'Evidencia_poruchoveostatne'
        verbose_name = "🛠️ Ostatný diel - poruchový"
        verbose_name_plural = "🛠️ Ostatné diely - poruchové"


# ==============================================================================
# ⚙️ TECHNICKÉ PARAMETRE ZARIADENÍ (12 MODELOV)
# ==============================================================================

class KompresoryGaugeLimitsMixin:
    """📊 ISAS INDUSTRIAL GAUGES: Poskytuje presné limity pre IoT budíky podľa parametrov technológie."""
    def get_gauge_limits(self) -> dict:
        limity = {}

        def _val(field_name):
            raw = getattr(self, field_name, None)
            if raw is None or raw == '':
                return None
            try:
                num = float(str(raw).replace(',', '.').strip())
                return num if num > 0 else None
            except (ValueError, TypeError):
                return None

        # 1. Tlak (Bar / mbar)
        t = _val('max_tlak_bar') or _val('tlak_vystup_bar') or _val('max_pretlak_mbar')
        if t is not None:
            limity['tlak'] = t

        # 2. Podtlak / Vákuum (mbar)
        pt = _val('max_vakuum_mbar')
        if pt is not None:
            limity['podtlak'] = pt

        # 3. Tlakové straty (Pa)
        pa = _val('externe_tlakove_straty_pa')
        if pa is not None:
            limity['tlak_pa'] = pa

        # 4. Teplota (°C)
        tep = _val('max_teplota_vytlaku_c')
        if tep is not None:
            limity['teplota'] = tep

        # 5. Prietok (m³/h)
        pr = _val('prietok_m3_h') or _val('vykonnost_m3_h') or _val('prietok_vzduchu_m3_h')
        if pr is not None:
            limity['prietok'] = pr

        # 6. Vibrodiagnostika (ISO 10816 - RMS & Ložiská)
        v_rms = _val('max_vibracie_rms_mm_s')
        if v_rms is not None:
            limity['vibracie_rms'] = v_rms
            limity['Vibracie_RMS'] = v_rms

        v_bpfo = _val('max_vibracie_bpfo_g')
        if v_bpfo is not None:
            limity['vibracie_bpfo'] = v_bpfo
            limity['Vibracie_BPFO'] = v_bpfo
            limity['Vibracie_BSF'] = v_bpfo

        return limity


class ParametreKompresor(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP_TECHNOLOGIE = [('skrutkovy', '🔩 Skrutkový'), ('piestovy', '⚙️ Piestový'), ('scroll', '🌀 Scroll (Špirálový)'), ('lamelovy', '🎛️ Lamelový')]
    POHON = [('fixed', '🔄 Fixed speed (Konštantné otáčky)'), ('vsd', '🎚️ VSD (Frekvenčný menič)')]
    MAZANIE = [('olej', '🛢️ Mazaný olejom'), ('bezolejovy', '🚫🛢️ Bezolejový (Oil-free)')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_kompresor", verbose_name="⚙️ Zariadenie", help_text="⚙️ Servisované zariadenie, ku ktorému patria tieto špecifické technické parametre kompresora.")
    typ_technologie = models.CharField("🛠️ Typ technológie", max_length=50, choices=TYP_TECHNOLOGIE, help_text="🛠️ Konštrukčné vyhotovenie kompresora určujúce náročnosť údržby a plánované servisné úkony.")
    pohon = models.CharField("⚡ Typ pohonu", max_length=50, choices=POHON, help_text="⚡ Spôsob regulácie otáčok pre vyhodnotenie energetickej efektivity a opotrebovania motora.")
    mazanie = models.CharField("🛢️ Typ mazania", max_length=50, choices=MAZANIE, default='olej', help_text="🛢️ Typ mazacieho okruhu určujúci požiadavky na kvalitu oleja, separátory a ekologické limity.")
    chladenie = models.CharField("❄️ Typ chladenia", max_length=50, choices=[('vzduch', '🌬️ Vzduchom'), ('voda', '💧 Vodou')], default='vzduch', help_text="❄️ Spôsob chladenia stroja; vodné chladenie vyžaduje špecifickú údržbu výmenníkov a kontrolu okruhu.")
    elektricke_napatie_v = models.IntegerField("🔌 Napätie (V)", default=400, help_text="🔌 Požadované napájacie napätie pre kontrolu elektroinštalácie na mieste servisu a revízie.")
    vykon_kw = models.DecimalField("💪 Výkon motora (kW)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="💪 Menovitý elektrický výkon motora pre výpočet spotreby energie a dimenzovanie ochrách.")
    koeficient_naprazdno = Decimal('0.30')  # ⚡ Odvetvový koeficient odberu pri chode kompresora v odľahčení
    max_tlak_bar = models.DecimalField("⏱️ Max. pracovný tlak (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="⏱️ Maximálny povolený prevádzkový tlak pre bezpečnú prevádzku a dimenzovanie tlakovej nádoby.")
    vykonnost_m3_h = models.DecimalField("💨 Výkonnosť (m³/h)", max_digits=7, decimal_places=2, null=True, blank=True, help_text="💨 Objemový prietok stlačeného vzduchu slúžiaci na overenie kapacity a efektivity kompresora.")
    max_teplota_vytlaku_c = models.IntegerField("🌡️ Max. teplota na výtlaku (°C)", null=True, blank=True, help_text="🌡️ Kritická teplota pre IoT monitoring; prekročenie signalizuje poruchu chladenia alebo degradáciu oleja.")
    pripojovaci_zavit = models.CharField("🔗 Pripojovací rozmer (napr. 1\" alebo DN50)", max_length=50, blank=True, help_text="🔗 Rozmer a typ výstupného pripojenia pre správny výber servisného materiálu a redukcií.")
    typ_oleja = models.CharField("🛢️ Predpísaný typ oleja", max_length=100, blank=True, help_text="Napr. Syntetický PAG (Roto-Xtend), Minerálny (Roto-Inject), Potravinársky FG.")
    objem_olejovej_naplne_l = models.DecimalField("🧪 Objem olejovej náplne (l)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="Presný objem náplne potrebný na výmenu pri servise.")
    poistny_ventil_vyrobne_cislo = models.CharField("🛡️ PV - Výrobné číslo", max_length=100, blank=True, help_text="Výrobné číslo poistného ventilu na separačnej nádobe.")
    poistny_ventil_tlak_bar = models.DecimalField("🛡️ PV - Nastavený odfukovací tlak (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="Menovitý otvárací tlak poistného ventilu kompresora.")
    poistny_ventil_datum_kontroly = models.DateField("🛡️ PV - Dátum posledného preskúšania / výmeny", null=True, blank=True, help_text="Dátum kontroly alebo výmeny poistného ventilu.")
    datum_poslednej_elektrorevizie = models.DateField("⚡ Dátum poslednej elektrorevízie", null=True, blank=True, help_text="Dátum odbornej prehliadky a skúšky elektroinštalácie kompresora.")
    datum_dalsej_elektrorevizie = models.DateField("⚡ Dátum ďalšej elektrorevízie", null=True, blank=True, help_text="Lehota ďalšej elektrorevízie kompresora.")
    datum_vymeny_tlakovych_hadic = models.DateField("🛡️ Dátum poslednej výmeny tlakových hadíc", null=True, blank=True, help_text="Dátum bezpečnostnej obmeny flexibilných hadíc oleja a stlačeného vzduchu.")
    ma_rekuperaciu_tepla = models.BooleanField("♻️ Integrovaný rekuperátor tepla", default=False, help_text="Indikácia výmenníka tepla voda/olej pre ohrev TÚV.")
    max_vibracie_rms_mm_s = models.DecimalField("📉 Max. vibrácie RMS (mm/s)", max_digits=5, decimal_places=2, default=Decimal('4.50'), help_text="ISO 10816 limit celkových vibrácií (napr. 4.5 mm/s pre stroje triedy II).")
    max_vibracie_bpfo_g = models.DecimalField("📉 Max. amplitúda ložiska BPFO (g)", max_digits=5, decimal_places=3, default=Decimal('0.500'), help_text="Limit pre poškodenie vonkajšej dráhy ložiska (Ball Pass Frequency Outer).")

    class Meta:
        db_table = 'Evidencia_parametrekompresor'
        verbose_name = "⚙️ Parametre - Kompresor"
        verbose_name_plural = "⚙️ Parametre - Kompresory"

    def save(self, *args, **kwargs):
        cfg = get_compliance_config(iso=get_iso_pre_objekt(self))
        zmenene_polia = set()
        d_posl_el = to_date(self.datum_poslednej_elektrorevizie)
        d_dalsia_el = to_date(self.datum_dalsej_elektrorevizie)
        if d_posl_el and (not d_dalsia_el or d_dalsia_el <= d_posl_el):
            self.datum_dalsej_elektrorevizie = add_calendar_interval(d_posl_el, years=cfg.get('elektro_roky', 3))
            zmenene_polia.add('datum_dalsej_elektrorevizie')
        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


class ParametreSusicka(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP_TECHNOLOGIE = [('kondenzacna', '💧 Kondenzačná'), ('adsorpcna', '❄️ Adsorpčná za studena'), ('adsorpcna_tepla', '🔥 Adsorpčná za tepla'), ('membranova', '🧬 Membránová')]
    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_susicka", verbose_name="⚙️ Zariadenie", help_text="🏭 Servisované zariadenie, ku ktorému patria tieto technické parametre sušičky stlačeného vzduchu.")
    typ_technologie = models.CharField("🛠️ Typ technológie", max_length=50, choices=TYP_TECHNOLOGIE, help_text="🛠️ Technologický princíp sušenia pre správne plánovanie údržby a náhradných dielov.")
    tlakovy_rosny_bod_c = models.IntegerField("🌡️ Tlakový rosný bod (°C)", null=True, blank=True, help_text="🌡️ Požadovaná hodnota tlakového rosného bodu (°C) pre kontrolu kvality stlačeného vzduchu.")
    max_tlak_bar = models.DecimalField("⏱️ Max. pracovný tlak (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="⏲️ Maximálny prevádzkový tlak v baroch pre overenie bezpečnosti a dimenzovania rozvodov.")
    max_prietok_m3_h = models.DecimalField("💨 Max. prietok vzduchu (m³/h)", max_digits=7, decimal_places=2, null=True, blank=True, help_text="💨 Maximálna kapacita prietoku vzduchu (m³/h) pre posúdenie dimenzovania prevádzky.")
    chladivo = models.CharField("❄️ Typ chladiva (len kondenz.)", max_length=50, null=True, blank=True, help_text="❄️ Typ použitého chladiaceho média (napr. R410A, R134a, R513A) pre evidenciu F-plynov.")
    mnozstvo_chladiva_kg = models.DecimalField("⚖️ Množstvo chladiva (kg)", max_digits=5, decimal_places=2, null=True, blank=True, help_text="⚖️ Celková hmotnosť chladiva v kg.")
    ekvivalent_co2_t = models.DecimalField("🌍 Ekvivalent CO₂ (tony)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="🌍 Ekvivalent CO2 v tonách. Podľa Nariadenia (EÚ) 2024/573: pri prekročení stanoveného limitu CO2 eq je povinná periodická kontrola únikov.")
    datum_kontroly_fplynov = models.DateField("📅 Dátum poslednej kontroly tesnosti chladiva", null=True, blank=True, help_text="📅 Dátum povinnej kontroly únikov podľa platného Nariadenia EÚ o F-plynoch.")
    ma_detekcny_system_unikov = models.BooleanField("🚨 Systém detekcie únikov (LDS)", default=False, help_text="Začiarknite, ak má sušička certifikovaný systém včasnej detekcie únikov (zdvojnásobuje zákonnú lehotu revízie).")
    typ_desikantu = models.CharField("🏜️ Typ náplne sušiva", max_length=100, blank=True, help_text="Napr. Aktivovaný oxid hlinitý, Molekulové sito, Silikagél.")
    hmotnost_desikantu_kg = models.DecimalField("⚖️ Hmotnosť náplne sušiva (kg)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="Množstvo sušiva v kilogramoch potrebné na kompletnú výmenu oboch kolón.")
    datum_vymeny_desikantu = models.DateField("📅 Dátum poslednej výmeny sušiva", null=True, blank=True, help_text="Dátum posledného presypu a výmeny sušiva.")
    pripojovaci_zavit = models.CharField("🔗 Pripojovací rozmer", max_length=50, blank=True, help_text="🔗 Rozmer a typ pripojovacieho závitu pre rýchly výber správnych fitinkov a dielov.")

    class Meta:
        db_table = 'Evidencia_parametresusicka'
        verbose_name = "♨️ Parametre - Sušička"
        verbose_name_plural = "♨️ Parametre - Sušičky"

    def save(self, *args, **kwargs):
        if not self.ekvivalent_co2_t and self.chladivo and self.mnozstvo_chladiva_kg:
            from .compliance import vypocitaj_co2_ekvivalent
            co2_val, valid = vypocitaj_co2_ekvivalent(self.chladivo, self.mnozstvo_chladiva_kg)
            if valid and co2_val > 0:
                self.ekvivalent_co2_t = Decimal(str(co2_val))
                if kwargs.get('update_fields') is not None:
                    kwargs['update_fields'] = set(kwargs['update_fields']) | {'ekvivalent_co2_t'}
        super().save(*args, **kwargs)


class ParametreTlakovaNadoba(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    ORIENTACIA = [('stojata', '↕️ Stojatá (Vertikálna)'), ('lezata', '↔️ Ležatá (Horizontálna)')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_nadoba", verbose_name="⚙️ Zariadenie", help_text="🔗 Vazba na konkrétne servisované zariadenie v evidencii.")
    objem_l = models.IntegerField("🛢️ Objem (Litre)", null=True, blank=True, help_text="🛢️ Celkový objem tlakovej nádoby v litroch.")
    max_tlak_bar = models.DecimalField("⏱️ Max. konštrukčný tlak (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="⏱️ Maximálny povolený konštrukčný tlak v baroch.")
    orientacia = models.CharField("↕️ Orientácia", max_length=20, choices=ORIENTACIA, default='stojata', help_text="↔️ Usporiadanie nádoby pre plánovanie montáže a servisného prístupu.")
    material = models.CharField("🧱 Materiál nádoby", max_length=50, choices=[('lakovana', 'Lakovaná oceľ'), ('pozinkovana', 'Pozinkovaná oceľ'), ('nerez', 'Nerezová oceľ (Inox)')], default='lakovana', help_text="🧱 Typ materiálu určujúci podmienky revízie a náchylnosť na koróziu.")
    SKUPINA_VTZ_CHOICES = [
        ('Bb', '🟡 Štandardná inšpekcia / Kategória I-II (Revízny technik)'),
        ('Ba', '🔴 Notifikovaná inšpekcia / Kategória III-IV (Inšpekčný orgán / Notifikovaná osoba)'),
        ('PED_I', '🔵 Kategória I (Smernica EÚ 2014/68/EÚ - PED)'),
        ('PED_II', '🟢 Kategória II (Smernica EÚ 2014/68/EÚ - PED)'),
        ('PED_III', '🟠 Kategória III (Smernica EÚ 2014/68/EÚ - PED)'),
        ('PED_IV', '🔴 Kategória IV (Smernica EÚ 2014/68/EÚ - PED)'),
    ]
    skupina_vtz = models.CharField(
        "🏛️ Zaradenie a bezpečnostná kategória nádoby",
        max_length=10,
        choices=SKUPINA_VTZ_CHOICES,
        default='Bb',
        help_text="Klasifikácia nádoby podľa európskej smernice PED 2014/68/EÚ a národných predpisov štátu prevádzky."
    )
    skusobny_tlak_pt_bar = models.DecimalField("⏱️ Skúšobný tlak PT (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="Tlak pri tlakovej skúške uvedený na výrobnom štítku.")
    pasport_cislo = models.CharField("📘 Číslo pasportu (Revízna kniha)", max_length=100, blank=True, help_text="📘 Číslo revíznej knihy vyžadované legislatívou pre vyhradené technické zariadenia.")
    datum_poslednej_prevadzkovej_revizie = models.DateField("📅 Dátum poslednej PREVÁDZKOVEJ revízie", null=True, blank=True, help_text="📅 Dátum vykonania poslednej vonkajšej prehliadky revíznym technikom.")
    datum_dalsej_prevadzkovej_revizie = models.DateField("📅 Dátum ďalšej PREVÁDZKOVEJ revízie", null=True, blank=True, help_text="📅 Lehota ďalšej revízie (ak necháte prázdne, dopočíta sa z lehoty v nastaveniach).")
    datum_poslednej_vnutornej_skusky = models.DateField("📅 Dátum poslednej VNÚTORNEJ skúšky", null=True, blank=True, help_text="📅 Dátum vykonania poslednej vnútornej inšpekcie nádoby.")
    datum_dalsej_vnutornej_skusky = models.DateField("📅 Dátum ďalšej VNÚTORNEJ skúšky", null=True, blank=True, help_text="📅 Lehota ďalšej vnútornej skúšky (ak necháte prázdne, dopočíta sa z nastavení).")
    datum_poslednej_tlakovej_skusky = models.DateField("📆 Dátum poslednej TLAKOVEJ skúšky", null=True, blank=True, help_text="📆 Dátum vykonania poslednej tlakovej skúšky skúšobným pretlakom.")
    datum_dalsej_tlakovej_skusky = models.DateField("📆 Dátum ďalšej TLAKOVEJ skúšky", null=True, blank=True, help_text="📆 Lehota ďalšej tlakovej skúšky (ak necháte prázdne, dopočíta sa z nastavení).")
    poistny_ventil_typ = models.CharField("🛡️ PV - Typ a výrobca", max_length=100, blank=True, help_text="Typové označenie poistného ventilu.")
    poistny_ventil_otvaraci_tlak_bar = models.DecimalField("🛡️ PV - Nastavený tlak (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="Odfukovací pretlak poistného ventilu.")
    poistny_ventil_vyrobne_cislo = models.CharField("🛡️ PV - Výrobné číslo", max_length=100, blank=True, help_text="Výrobné číslo poistného ventilu.")
    datum_kontroly_poistneho_ventilu = models.DateField("🛡️ PV - Dátum posledného preskúšania", null=True, blank=True, help_text="Dátum vystavenia protokolu o nastavení a preskúšaní poistného ventilu.")
    datum_dalsej_kontroly_poistneho_ventilu = models.DateField("🛡️ PV - Dátum ďalšej kontroly / výmeny", null=True, blank=True, help_text="Lehota ďalšieho preskúšania poistného ventilu.")
    manometer_vyrobne_cislo = models.CharField("⏱️ Manometer - Výrobné číslo / Kód", max_length=100, blank=True, help_text="Identifikátor tlakomera nádoby.")
    datum_kontroly_manometra = models.DateField("⏱️ Manometer - Dátum poslednej kontroly / kalibrácie", null=True, blank=True, help_text="Dátum overenia alebo výmeny manometra.")
    datum_dalsej_kontroly_manometra = models.DateField("⏱️ Manometer - Dátum ďalšej kontroly", null=True, blank=True, help_text="Lehota ďalšej kontroly manometra.")

    class Meta:
        db_table = 'Evidencia_parametretlakovanadoba'
        verbose_name = "🛢️ Parametre - Tlaková nádoba"
        verbose_name_plural = "🛢️ Parametre - Tlakové nádoby"

    def save(self, *args, **kwargs):
        cfg = get_compliance_config(iso=get_iso_pre_objekt(self))
        zmenene_polia = set()
        d_posl_prev = to_date(self.datum_poslednej_prevadzkovej_revizie)
        d_dalsia_prev = to_date(self.datum_dalsej_prevadzkovej_revizie)
        if d_posl_prev and (not d_dalsia_prev or d_dalsia_prev <= d_posl_prev):
            self.datum_dalsej_prevadzkovej_revizie = add_calendar_interval(d_posl_prev, years=cfg.get('prevadzkova_roky', 1))
            zmenene_polia.add('datum_dalsej_prevadzkovej_revizie')

        d_posl_vnut = to_date(self.datum_poslednej_vnutornej_skusky)
        d_dalsia_vnut = to_date(self.datum_dalsej_vnutornej_skusky)
        if d_posl_vnut and (not d_dalsia_vnut or d_dalsia_vnut <= d_posl_vnut):
            self.datum_dalsej_vnutornej_skusky = add_calendar_interval(d_posl_vnut, years=cfg.get('vnutorna_roky', 5))
            zmenene_polia.add('datum_dalsej_vnutornej_skusky')

        d_posl_tlak = to_date(self.datum_poslednej_tlakovej_skusky)
        d_dalsia_tlak = to_date(self.datum_dalsej_tlakovej_skusky)
        if d_posl_tlak and (not d_dalsia_tlak or d_dalsia_tlak <= d_posl_tlak):
            self.datum_dalsej_tlakovej_skusky = add_calendar_interval(d_posl_tlak, years=cfg.get('tlakova_roky', 10))
            zmenene_polia.add('datum_dalsej_tlakovej_skusky')

        d_posl_pv = to_date(self.datum_kontroly_poistneho_ventilu)
        d_dalsia_pv = to_date(self.datum_dalsej_kontroly_poistneho_ventilu)
        if d_posl_pv and (not d_dalsia_pv or d_dalsia_pv <= d_posl_pv):
            self.datum_dalsej_kontroly_poistneho_ventilu = add_calendar_interval(d_posl_pv, years=cfg.get('poistny_ventil_roky', 1))
            zmenene_polia.add('datum_dalsej_kontroly_poistneho_ventilu')

        d_posl_mano = to_date(getattr(self, 'datum_kontroly_manometra', None))
        d_dalsia_mano = to_date(getattr(self, 'datum_dalsej_kontroly_manometra', None))
        if d_posl_mano and (not d_dalsia_mano or d_dalsia_mano <= d_posl_mano):
            self.datum_dalsej_kontroly_manometra = add_calendar_interval(d_posl_mano, years=cfg.get('manometer_roky', 2))
            zmenene_polia.add('datum_dalsej_kontroly_manometra')

        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


class ParametreFiltracia(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    STUPNE = [('cyklon', '🌪️ Cyklónový odlučovač'), ('hruby', '🪨 Hrubý prach (Pre-filter)'), ('jemny', '🌫️ Jemný prach / Olej (Mikrofilter)'), ('uhlie', '⬛ Aktívne uhlie'), ('sterilny', '🏥 Sterilný / Medicínsky')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_filtracia", verbose_name="⚙️ Zariadenie", help_text="⚙️ Servisované zariadenie, ku ktorému prislúchajú tieto filtračné parametre.")
    stupen_filtracie = models.CharField("🧽 Stupeň filtrácie", max_length=50, choices=STUPNE, help_text="🧽 Klasifikácia filtračného stupňa určujúca typ prevádzky a požadovanú čistotu média.")
    mikrony = models.DecimalField("🔬 Priepustnosť (µm)", max_digits=6, decimal_places=3, null=True, blank=True, help_text="🔬 Jemnosť filtrácie v mikrónoch (µm) pre garanciu požadovanej kvality a čistoty výstupu.")
    max_tlak_bar = models.DecimalField("⏱️ Max. pracovný tlak (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="⏱️ Maximálny povolený prevádzkový tlak zariadenia v baroch pre bezpečnú prevádzku.")
    max_diferencny_tlak_mbar = models.IntegerField("📉 Max. diferenčný tlak (mbar)", null=True, blank=True, help_text="📉 Tlaková strata pre výmenu filtračnej vložky (zvyčajne 350-400 mbar) využívaná pre prediktívny servis.")
    max_prietok_m3_h = models.DecimalField("💨 Max. prietok (m³/h)", max_digits=7, decimal_places=2, null=True, blank=True, help_text="💨 Maximálny dimenzovaný prietok média v m³/h pre dodržanie kapacity a účinnosti filtrácie.")
    pripojovaci_zavit = models.CharField("🔗 Pripojovací rozmer", max_length=50, blank=True, help_text="🔗 Rozmer a typ pripojovacieho závitu pre správny výber náhradných dielov a montáž.")
    datum_poslednej_vymeny_vloziek = models.DateField("🌪️ Dátum poslednej výmeny filtračnej vložky", null=True, blank=True, help_text="Dátum výmeny filtračného elementu.")
    datum_dalsej_vymeny_vloziek = models.DateField("🌪️ Lehota ďalšej výmeny vložky", null=True, blank=True, help_text="Lehota výmeny filtračného elementu.")

    class Meta:
        db_table = 'Evidencia_parametrefiltracia'
        verbose_name = "🚰 Parametre - Filtrácia"
        verbose_name_plural = "🚰 Parametre - Filtrácie"

    def save(self, *args, **kwargs):
        cfg = get_compliance_config()
        zmenene_polia = set()
        d_posl = to_date(self.datum_poslednej_vymeny_vloziek)
        d_dalsia = to_date(self.datum_dalsej_vymeny_vloziek)
        if d_posl and (not d_dalsia or d_dalsia <= d_posl):
            self.datum_dalsej_vymeny_vloziek = add_calendar_interval(d_posl, years=cfg.get('filter_vlozky_roky', 1))
            zmenene_polia.add('datum_dalsej_vymeny_vloziek')
        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


class ParametreOdlucovac(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_odlucovac", verbose_name="⚙️ Zariadenie", help_text="🔗 Odkaz na konkrétne servisované zariadenie, ku ktorému patria tieto špecifické parametre odlučovača.")
    max_vykon_kompresora_kw = models.IntegerField("💪 Pre max. výkon kompresora (kW)", null=True, blank=True, help_text="⚡ Maximálny celkový výkon kompresorov v kW, pre ktorý je tento odlučovač dimenzovaný.")
    max_prietok_kondenzatu_l_h = models.DecimalField("💧 Max. prietok kondenzátu (l/h)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="💧 Maximálny povolený prietok kondenzátu v litroch za hodinu pre garantovanú účinnosť separácie.")
    typ_vlozky = models.CharField("🧻 Typ filtračnej vložky (Uhlík/Oleo-filná)", max_length=100, blank=True, help_text="📑 Presné označenie alebo typ filtračnej vložky potrebné pre správne doobjednávanie spotrebného materiálu.")
    datum_poslednej_vymeny_vloziek = models.DateField("🚰 Dátum poslednej výmeny sady filtrov", null=True, blank=True, help_text="Dátum výmeny uhlíkových/oleofilných vložiek separátora podľa environmentálnych predpisov.")
    datum_dalsej_vymeny_vloziek = models.DateField("🚰 Lehota ďalšej výmeny filtrov", null=True, blank=True, help_text="Lehota výmeny filtračných vložiek.")

    class Meta:
        db_table = 'Evidencia_parametreodlucovac'
        verbose_name = "♻️ Parametre - Odlučovač oleja z vody"
        verbose_name_plural = "♻️ Parametre - Odlučovače"

    def save(self, *args, **kwargs):
        cfg = get_compliance_config()
        zmenene_polia = set()
        d_posl = to_date(self.datum_poslednej_vymeny_vloziek)
        d_dalsia = to_date(self.datum_dalsej_vymeny_vloziek)
        if d_posl and (not d_dalsia or d_dalsia <= d_posl):
            self.datum_dalsej_vymeny_vloziek = add_calendar_interval(d_posl, years=cfg.get('odlucovac_filtre_roky', 1))
            zmenene_polia.add('datum_dalsej_vymeny_vloziek')
        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


class ParametreGeneratorPlynu(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP_PLYNU = [('dusik', '🔵 Dusík (N2)'), ('kyslik', '🟢 Kyslík (O2)')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_generator", verbose_name="⚙️ Zariadenie", help_text="⚙️ Konkrétne servisované zariadenie, ku ktorému prislúchajú tieto technické parametre generátora.")
    typ_plynu = models.CharField("☁️ Typ generovaného plynu", max_length=50, choices=TYP_PLYNU, help_text="☁️ Druh vyvíjaného plynu určujúci prevádzkové vlastnosti a špecifikáciu technológie.")
    cistota_percent = models.DecimalField("✨ Čistota plynu (%)", max_digits=6, decimal_places=4, null=True, blank=True, help_text="✨ Požadovaná úroveň čistoty výstupného plynu v percentách pre dodržanie technologických štandardov.")
    vykonnost_m3_h = models.DecimalField("💨 Výkonnosť (m³/h)", max_digits=7, decimal_places=2, null=True, blank=True, help_text="💨 Maximálny objem vygenerovaného plynu za hodinu v m³ pre dimenzovanie prevádzky.")
    tlak_vstup_bar = models.DecimalField("📥 Požadovaný vstupný tlak (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="📥 Minimálny požadovaný tlak stlačeného vzduchu na vstupe do generátora v baroch.")
    tlak_vystup_bar = models.DecimalField("📤 Výstupný tlak plynu (Bar)", max_digits=4, decimal_places=1, null=True, blank=True, help_text="📤 Výstupný prevádzkový tlak vygenerovaného plynu v baroch k dispozícii pre odbernú sieť.")
    datum_poslednej_kalibracie_o2 = models.DateField("✨ Dátum poslednej kalibrácie O₂ senzora", null=True, blank=True, help_text="Dátum kalibrácie zirkóniového/elektrochemického článku.")
    datum_dalsej_kalibracie_o2 = models.DateField("✨ Lehota ďalšej kalibrácie O₂ senzora", null=True, blank=True, help_text="Lehota periodickej kalibrácie.")

    class Meta:
        db_table = 'Evidencia_parametregeneratorplynu'
        verbose_name = "🏭 Parametre - Generátor plynu"
        verbose_name_plural = "🏭 Parametre - Generátory plynov"

    def save(self, *args, **kwargs):
        cfg = get_compliance_config()
        zmenene_polia = set()
        d_posl = to_date(self.datum_poslednej_kalibracie_o2)
        d_dalsia = to_date(self.datum_dalsej_kalibracie_o2)
        if d_posl and (not d_dalsia or d_dalsia <= d_posl):
            self.datum_dalsej_kalibracie_o2 = add_calendar_interval(d_posl, years=cfg.get('kalibracia_o2_roky', 1))
            zmenene_polia.add('datum_dalsej_kalibracie_o2')
        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


class ParametreChladic(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP_CHLADENIA = [('vzduch', '🌬️ Vzduchom chladený'), ('voda', '💧 Vodou chladený')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_chladic", verbose_name="⚙️ Zariadenie", help_text="🔗 Servisované zariadenie, ku ktorému patria tieto technické parametre chladiča.")
    typ_chladenia = models.CharField("🧊 Typ chladenia", max_length=20, choices=TYP_CHLADENIA, default='vzduch', help_text="❄️ Spôsob odvodu tepla zo systému pre určenie nárokov na inštaláciu a servis.")
    chladiaci_vykon_kw = models.DecimalField("❄️ Chladiaci výkon (kW)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="⚡ Menovitý chladiaci výkon v kilowattoch pre posúdenie kapacity a energetickej náročnosti.")
    integrovane_cerpadlo = models.BooleanField("🌊 Integrované čerpadlo", default=False, help_text="🌊 Indikácia, či jednotka obsahuje vlastné obehové čerpadlo pre cirkuláciu chladiacej kvapaliny.")
    objem_nadrze_l = models.IntegerField("🛢️ Objem integrovanej nádrže (l)", null=True, blank=True, help_text="🛢️ Celkový objem vnútornej nádrže v litroch potrebný pri dopĺňaní a výmene prevádzkových kvapalín.")
    chladivo = models.CharField("🧪 Typ chladiva", max_length=50, null=True, blank=True, help_text="🧪 Chemické označenie chladiva pre dodržanie legislatívnych noriem (R410A, R32, R134a, R407C).")
    mnozstvo_chladiva_kg = models.DecimalField("⚖️ Množstvo chladiva (kg)", max_digits=5, decimal_places=2, null=True, blank=True, help_text="⚖️ Hmotnosť chladiacej náplne v kilogramoch.")
    ekvivalent_co2_t = models.DecimalField("🌍 Ekvivalent CO₂ (tony)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="🌍 Ekvivalent CO2 v tonách pre evidenciu podľa platného Nariadenia EÚ o F-plynoch.")
    datum_kontroly_fplynov = models.DateField("📅 Dátum poslednej kontroly tesnosti chladiva", null=True, blank=True, help_text="📅 Termín vykonania kontroly únikov certifikovanou osobou.")
    ma_detekcny_system_unikov = models.BooleanField("🚨 Systém detekcie únikov (LDS)", default=False, help_text="Začiarknite, ak má chiller inštalovaný systém detekcie únikov (povinné nad 500 t CO2 eq; zdvojnásobuje periódu kontroly).")
    datum_poslednej_elektrorevizie = models.DateField("⚡ Dátum poslednej elektrorevízie", null=True, blank=True, help_text="Dátum odbornej prehliadky a skúšky elektroinštalácie chladiča.")
    datum_dalsej_elektrorevizie = models.DateField("⚡ Dátum ďalšej elektrorevízie", null=True, blank=True, help_text="Lehota ďalšej elektrorevízie chladiča.")

    class Meta:
        db_table = 'Evidencia_parametrechladic'
        verbose_name = "🧊 Parametre - Priemyselný Chladič"
        verbose_name_plural = "🧊 Parametre - Chladiče"

    def save(self, *args, **kwargs):
        zmenene_polia = set()
        if not self.ekvivalent_co2_t and self.chladivo and self.mnozstvo_chladiva_kg:
            from .compliance import vypocitaj_co2_ekvivalent
            co2_val, valid = vypocitaj_co2_ekvivalent(self.chladivo, self.mnozstvo_chladiva_kg)
            if valid and co2_val > 0:
                self.ekvivalent_co2_t = Decimal(str(co2_val))
                zmenene_polia.add('ekvivalent_co2_t')

        cfg = get_compliance_config(iso=get_iso_pre_objekt(self))
        d_posl_el = to_date(self.datum_poslednej_elektrorevizie)
        d_dalsia_el = to_date(self.datum_dalsej_elektrorevizie)
        if d_posl_el and (not d_dalsia_el or d_dalsia_el <= d_posl_el):
            self.datum_dalsej_elektrorevizie = add_calendar_interval(d_posl_el, years=cfg.get('elektro_roky', 3))
            zmenene_polia.add('datum_dalsej_elektrorevizie')

        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


class ParametreVyveva(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP = [('olejova', '🛢️ Olejová'), ('sucha', '🏜️ Suchá'), ('vodokruzna', '💦 Vodokružná')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_vyveva", verbose_name="⚙️ Zariadenie", help_text="⚙️ Servisované zariadenie, ku ktorému patria tieto technické špecifikácie vývevy.")
    objem_olejovej_naplne_l = models.DecimalField("🧪 Objem olejovej náplne (l)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="Presný objem olejovej náplne vo výveve potrebný na výmenu pri servise (len pre olejové vývevy).")
    typ_technologie = models.CharField("🛠️ Typ technológie", max_length=50, choices=TYP, help_text="🛠️ Technologický princíp vývevy určujúci typ údržby, potrebné prevádzkové kvapaliny a servisné intervaly.")
    max_vakuum_mbar = models.IntegerField("🕳️ Max. vákuum (mbar)", null=True, blank=True, help_text="🕳️ Maximálny dosiahnuteľný podtlak v milibaroch pre kontrolu účinnosti a diagnostiku porúch.")
    saci_vykon_m3_h = models.DecimalField("🌪️ Sací výkon (m³/h)", max_digits=7, decimal_places=2, null=True, blank=True, help_text="🌪️ Objemový prietok nasávaného vzduchu v m³/h pre overenie požadovaného výkonu technológie.")
    pripojovaci_zavit = models.CharField("🔗 Pripojovací rozmer", max_length=50, blank=True, help_text="🔗 Rozmer a typ pripojovacieho závitu pre správny výber náhradných dielov a príslušenstva.")
    max_vibracie_rms_mm_s = models.DecimalField("📉 Max. vibrácie RMS (mm/s)", max_digits=5, decimal_places=2, default=Decimal('4.50'), help_text="Limit celkových vibrácií.")
    max_vibracie_bpfo_g = models.DecimalField("📉 Max. amplitúda ložiska BPFO (g)", max_digits=5, decimal_places=3, default=Decimal('0.500'), help_text="Limit pre poškodenie vonkajšej dráhy ložiska.")

    class Meta:
        db_table = 'Evidencia_parametrevyveva'
        verbose_name = "🕳️ Parametre - Výveva"
        verbose_name_plural = "🕳️ Parametre - Vývevy"


class ParametreDuchadlo(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP = [('roots', '⚙️ Roots / Lobe'), ('postranny_kanal', '🌀 S postranným kanálom')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_duchadlo", verbose_name="⚙️ Zariadenie", help_text="🔗 Odkaz na konkrétne servisované zariadenie, ku ktorému sa štítkové parametre vzťahujú.")
    typ_technologie = models.CharField("🛠️ Typ technológie", max_length=50, choices=TYP, help_text="🛠️ Konštrukčný typ dúchadla určujúci postup údržby a špecifikáciu kompatibilných náhradných dielov.")
    vykon_kw = models.DecimalField("💪 Výkon motora (kW)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="⚡ Menovitý výkon motora v kW pre dimenzovanie elektroinštalácie a posúdenie energetickej náročnosti.")
    max_pretlak_mbar = models.IntegerField("⏱️ Max. pretlak (mbar)", null=True, blank=True, help_text="📊 Maximálny prevádzkový pretlak v mbar potrebný na nastavenie poistných ventilov a diagnostiku.")
    prietok_m3_h = models.DecimalField("💨 Prietok (m³/h)", max_digits=7, decimal_places=2, null=True, blank=True, help_text="💨 Objemový prietok vzduchu v m³/h na overenie kapacity a prevádzkovej účinnosti technológie.")
    max_hlucnost_db = models.IntegerField("🔊 Hlučnosť (dB)", null=True, blank=True, help_text="🔊 Hladina hluku v decibeloch pre posúdenie požiadaviek BOZP a dimenzovanie protihlukových krytov.")
    max_vibracie_rms_mm_s = models.DecimalField("📉 Max. vibrácie RMS (mm/s)", max_digits=5, decimal_places=2, default=Decimal('7.10'), help_text="Limit celkových vibrácií (Dúchadlá majú často vyššie limity, napr. 7.1 mm/s).")
    max_vibracie_bpfo_g = models.DecimalField("📉 Max. amplitúda ložiska BPFO (g)", max_digits=5, decimal_places=3, default=Decimal('0.500'), help_text="Limit pre poškodenie vonkajšej dráhy ložiska.")

    class Meta:
        db_table = 'Evidencia_parametreduchadlo'
        verbose_name = "🌬️ Parametre - Dúchadlo"
        verbose_name_plural = "🌬️ Parametre - Dúchadlá"


class ParametreKlimatizacia(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP = [('split', '🔲 Split'), ('multisplit', '🔲🔲 Multisplit'), ('vrv', '🏢 VRV / VRF')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_klimatizacia", verbose_name="⚙️ Zariadenie", help_text="🔗 Odkaz na konkrétne servisované zariadenie, ku ktorému sa vzťahujú tieto technické parametre.")
    typ_jednotky = models.CharField("🏢 Typ jednotky", max_length=50, choices=TYP, help_text="🏢 Konfigurácia chladiaceho systému určujúca rozsah servisných úkonov.")
    chladiaci_vykon_kw = models.DecimalField("❄️ Chladiaci výkon (kW)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="❄️ Nominálny chladiaci výkon v kW pre posúdenie kapacity a energetickej náročnosti.")
    vykurovaci_vykon_kw = models.DecimalField("🔥 Vykurovací výkon (kW)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="🔥 Tepelný výkon v kW pre hodnotenie energetickej bilancie pri vykurovaní.")
    chladivo = models.CharField("🧪 Typ chladiva", max_length=50, null=True, blank=True, help_text="🧪 Použitý typ chladiva požadovaný pre evidenciu fluorovaných skleníkových plynov.")
    mnozstvo_chladiva_kg = models.DecimalField("⚖️ Množstvo chladiva (kg)", max_digits=5, decimal_places=2, null=True, blank=True, help_text="⚖️ Celková hmotnosť náplne v kg pre stanovenie CO2 ekvivalentu a lehoty zákonných revízií.")
    ekvivalent_co2_t = models.DecimalField("🌍 Ekvivalent CO₂ (tony)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="🌍 Ekvivalent CO2 v tonách pre evidenciu podľa Nariadenia (EÚ) 2024/573.")
    datum_kontroly_fplynov = models.DateField("📅 Dátum poslednej kontroly tesnosti chladiva", null=True, blank=True, help_text="📅 Termín vykonania kontroly únikov certifikovanou osobou podľa platného Nariadenia EÚ o F-plynoch.")
    ma_detekcny_system_unikov = models.BooleanField("🚨 Systém detekcie únikov (LDS)", default=False, help_text="Začiarknite, ak má klimatizačný okruh systém detekcie únikov (zdvojnásobuje lehotu kontroly).")

    class Meta:
        db_table = 'Evidencia_parametreklimatizacia'
        verbose_name = "❄️ Parametre - Klimatizácia"
        verbose_name_plural = "❄️ Parametre - Klimatizácie"

    def save(self, *args, **kwargs):
        if not self.ekvivalent_co2_t and self.chladivo and self.mnozstvo_chladiva_kg:
            from .compliance import vypocitaj_co2_ekvivalent
            co2_val, valid = vypocitaj_co2_ekvivalent(self.chladivo, self.mnozstvo_chladiva_kg)
            if valid and co2_val > 0:
                self.ekvivalent_co2_t = Decimal(str(co2_val))
                if kwargs.get('update_fields') is not None:
                    kwargs['update_fields'] = set(kwargs['update_fields']) | {'ekvivalent_co2_t'}
        super().save(*args, **kwargs)


class ParametreCerpadlo(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP = [('odstredive', '🌀 Odstredivé'), ('ponorne', '⬇️ Ponorné'), ('davkovacie', '💧 Dávkovacie'), ('zubove', '⚙️ Zubové')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_cerpadlo", verbose_name="⚙️ Zariadenie", help_text="🔗 Priradené servisované zariadenie, ku ktorému tieto technické parametre patria.")
    typ_cerpadla = models.CharField("🛠️ Typ čerpadla", max_length=50, choices=TYP, help_text="🛠️ Konštrukčný typ čerpadla pre určenie správnych servisných postupov a náhradných dielov.")
    vykon_kw = models.DecimalField("💪 Výkon motora (kW)", max_digits=6, decimal_places=2, null=True, blank=True, help_text="⚡ Menovitý elektrický výkon motora v kW pre posúdenie energetickej náročnosti a revízie.")
    max_prietok_l_min = models.IntegerField("🌊 Max. prietok (l/min)", null=True, blank=True, help_text="💧 Maximálny objem prečerpanej kvapaliny za minútu pri optimálnych prevádzkových podmienkach.")
    max_vytlak_m = models.IntegerField("📏 Max. výtlak (metre)", null=True, blank=True, help_text="📏 Maximálna dopravná výška alebo výtlačný tlak, ktorý je čerpadlo schopné vyvinúť.")
    teplota_media_c = models.IntegerField("🌡️ Max. teplota média (°C)", null=True, blank=True, help_text="🌡️ Maximálna povolená teplota čerpanej kvapaliny na prevenciu poškodenia tesnení.")
    material_telesa = models.CharField("🧱 Materiál telesa", max_length=50, blank=True, help_text="🧱 Materiálové vyhotovenie tela čerpadla (napr. Liatina, Nerez SS304) pre posúdenie odolnosti.")
    max_vibracie_rms_mm_s = models.DecimalField("📉 Max. vibrácie RMS (mm/s)", max_digits=5, decimal_places=2, default=Decimal('4.50'), help_text="Limit celkových vibrácií podľa ISO 10816-7 (Čerpadlá).")
    max_vibracie_bpfo_g = models.DecimalField("📉 Max. amplitúda ložiska BPFO (g)", max_digits=5, decimal_places=3, default=Decimal('0.500'), help_text="Limit pre poškodenie vonkajšej dráhy ložiska.")

    class Meta:
        db_table = 'Evidencia_parametrecerpadlo'
        verbose_name = "🌊 Parametre - Čerpadlo"
        verbose_name_plural = "🌊 Parametre - Čerpadlá"


class ParametreVztJednotka(KompresoryGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP = [('podstropna', '🔲 Podstropná kompaktná'), ('stacionarna', '🏢 Stacionárna komorová'), ('stresna', '🏠 Strešná (Rooftop)')]
    REKUPERATOR = [('rotacny', '🔄 Rotačný regeneračný'), ('doskovy', '🔲 Doskový krížový/protiprúdový'), ('glykolovy', '🧪 Glykolový okruh'), ('bez', '🚫 Bez rekuperácie')]

    zariadenie = models.OneToOneField('Evidencia.ServisovaneZariadenie', on_delete=models.CASCADE, related_name="parametre_vzt", verbose_name="⚙️ Zariadenie")
    typ_jednotky = models.CharField("🏢 Prevedenie", max_length=50, choices=TYP, default='stacionarna')
    prietok_vzduchu_m3_h = models.DecimalField("💨 Menovitý prietok vzduchu (m³/h)", max_digits=8, decimal_places=2, null=True, blank=True)
    externe_tlakove_straty_pa = models.IntegerField("📉 Externý tlakový spád (Pa)", null=True, blank=True)
    typ_rekuperacie = models.CharField("♻️ Typ rekuperátora", max_length=50, choices=REKUPERATOR, default='doskovy')
    ohrev_typ = models.CharField("🔥 Typ ohrevu", max_length=50, choices=[('vodny', '💧 Teplovodný'), ('elektricky', '⚡ Elektrický'), ('bez', 'Bez ohrevu')], default='vodny')
    chladenie_typ = models.CharField("❄️ Typ chladenia", max_length=50, choices=[('vodne', '💧 Vodné'), ('priame', '🧪 Priamy výparník (DX)'), ('bez', 'Bez chladenia')], default='vodne')
    filtre_privod = models.CharField("🧽 Trieda filtrácie - Prívod", max_length=50, default="ePM1 55% (F7)")
    filtre_odvod = models.CharField("🧽 Trieda filtrácie - Odvod", max_length=50, default="Coarse 65% (G4)")

    class Meta:
        db_table = 'Evidencia_parametrevztjednotka'
        verbose_name = "🏢 Parametre - VZT Jednotka"
        verbose_name_plural = "🏢 Parametre - VZT Jednotky"

# ==============================================================================
# ⚖️ ODVETVOVÁ KONFIGURÁCIA A NÁRODNÉ LEHOTY (KOMPRESORY & VTZ)
# ==============================================================================

class KompresoryNastavenia(ISASAuditMixin, models.Model):
    """
    🏛️ Singleton model pre centrálnu konfiguráciu odvetvových lehôt revízií
    pre tlakové nádoby, kompresory, sušičky, chillery a F-plyny.
    """
    lehota_vtz_prevadzkova_roky = models.PositiveIntegerField(
        "⏱️ Lehota: Prevádzková revízia nádoby (roky)", 
        default=1, 
        help_text="Odporúčaná lehota podľa technických predpisov a Smernice PED (štandardne 1 rok)."
    )
    lehota_vtz_vnutorna_roky = models.PositiveIntegerField(
        "⏱️ Lehota: Vnútorná skúška nádoby (roky)", 
        default=5, 
        help_text="Odporúčaná lehota podľa technických predpisov a Smernice PED (štandardne 5 rokov)."
    )
    lehota_vtz_tlakova_roky = models.PositiveIntegerField(
        "⏱️ Lehota: Tlaková skúška nádoby (roky)", 
        default=10, 
        help_text="Odporúčaná lehota podľa technických predpisov a Smernice PED (štandardne 10 rokov)."
    )
    lehota_vtz_poistny_ventil_roky = models.PositiveIntegerField(
        "⏱️ Lehota: Preskúšanie poistného ventilu (roky)", 
        default=1, 
        help_text="Odporúčaná lehota podľa EN ISO 4126 (štandardne 1 rok pre nádoby aj kompresory)."
    )
    lehota_fplyn_5_az_50t_mesiace = models.PositiveIntegerField(
        "❄️ Lehota F-plyny: 5 až 50 t CO₂ eq (mesiace)", 
        default=12, 
        help_text="Odporúčaná lehota podľa Nariadenia EÚ 2024/573 (štandardne 12 mesiacov)."
    )
    lehota_fplyn_50_az_500t_mesiace = models.PositiveIntegerField(
        "❄️ Lehota F-plyny: 50 až 500 t CO₂ eq (mesiace)", 
        default=6, 
        help_text="Odporúčaná lehota podľa Nariadenia EÚ 2024/573 (štandardne 6 mesiacov)."
    )
    lehota_fplyn_nad_500t_mesiace = models.PositiveIntegerField(
        "❄️ Lehota F-plyny: nad 500 t CO₂ eq (mesiace)", 
        default=3, 
        help_text="Odporúčaná lehota podľa Nariadenia EÚ 2024/573 (štandardne 3 mesiace)."
    )
    lehota_vtz_elektro_roky = models.PositiveIntegerField(
        "⚡ Lehota: Bezpečnostná elektrorevízia (roky)", 
        default=3, 
        help_text="Odporúčaná lehota podľa technických noriem EN a predpisov pre priemyselné prostredie (štandardne 3 roky)."
    )
    lehota_vtz_manometer_roky = models.PositiveIntegerField(
        "⏱️ Lehota: Kontrola manometra nádoby (roky)", 
        default=2, 
        help_text="Odporúčaná lehota podľa metrologických predpisov a technických noriem (štandardne 2 roky)."
    )
    lehota_desikant_roky = models.PositiveIntegerField(
        "🏜️ Lehota: Výmena sušiva sušičky (roky)", 
        default=4, 
        help_text="Odporúčaná životnosť adsorpčného sušiva výrobcom (štandardne 3 až 5 rokov)."
    )
    lehota_tlakove_hadice_roky = models.PositiveIntegerField(
        "🛡️ Lehota: Výmena tlakových hadíc kompresora (roky)", 
        default=5, 
        help_text="Bezpečnostná výmena pružných hadíc podľa EN ISO 4414 (štandardne 5 rokov)."
    )
    lehota_odlucovac_filtre_roky = models.PositiveIntegerField(
        "🚰 Lehota: Výmena filtrov odlučovača kondenzátu (roky)", 
        default=1, 
        help_text="Odporúčaná environmentálna lehota pre zamedzenie úniku prevádzkových kvapalín (štandardne 1 rok)."
    )
    lehota_filter_vlozky_roky = models.PositiveIntegerField(
        "🌪️ Lehota: Výmena vložiek filtrácie (roky)", 
        default=1, 
        help_text="Garantovaná životnosť filtračných elementov stlačeného vzduchu (štandardne 1 rok)."
    )
    lehota_kalibracia_o2_roky = models.PositiveIntegerField(
        "✨ Lehota: Kalibrácia O₂ senzora generátora (roky)", 
        default=1, 
        help_text="Metrologická kalibrácia kyslíkovej sondy pre generátory plynov (štandardne 1 rok)."
    )

    class Meta:
        db_table = 'kompresory_nastavenia'
        verbose_name = "⚙️ Nastavenia odvetvia (Kompresory a VTZ)"
        verbose_name_plural = "⚙️ Nastavenia odvetvia (Kompresory a VTZ)"

    def clean(self):
        super().clean()
        if self.pk is None and KompresoryNastavenia.objects.exists():
            raise ValidationError("⚠️ Kritická chyba: Môže existovať iba jedno globálne nastavenie odvetvia.")
        polia_na_kontrolu = [
            ('lehota_vtz_prevadzkova_roky', 'Prevádzková revízia'),
            ('lehota_vtz_vnutorna_roky', 'Vnútorná skúška'),
            ('lehota_vtz_tlakova_roky', 'Tlaková skúška'),
            ('lehota_vtz_poistny_ventil_roky', 'Poistný ventil'),
            ('lehota_fplyn_5_az_50t_mesiace', 'F-plyny 5-50t'),
            ('lehota_fplyn_50_az_500t_mesiace', 'F-plyny 50-500t'),
            ('lehota_fplyn_nad_500t_mesiace', 'F-plyny nad 500t'),
            ('lehota_vtz_elektro_roky', 'Elektrorevízia'),
            ('lehota_vtz_manometer_roky', 'Manometer'),
            ('lehota_desikant_roky', 'Výmena sušiva'),
            ('lehota_tlakove_hadice_roky', 'Tlakové hadice'),
            ('lehota_odlucovac_filtre_roky', 'Filtre odlučovača'),
            ('lehota_filter_vlozky_roky', 'Vložky filtrácie'),
            ('lehota_kalibracia_o2_roky', 'Kalibrácia O₂ senzora'),
        ]
        for field_name, label in polia_na_kontrolu:
            val = getattr(self, field_name, None)
            if val is not None and val < 1:
                raise ValidationError({field_name: f"⚠️ {label} musí byť minimálne 1."})

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        def _clear_cache():
            try:
                cache.delete_pattern('isas_compliance_config*')
            except AttributeError:
                cache.delete('isas_compliance_config')
                try:
                    for obj in KompresoryStatLokalizacia.objects.select_related('stat').all():
                        if obj.stat and obj.stat.iso_kod:
                            cache.delete(f'isas_compliance_config_{obj.stat.iso_kod}')
                except Exception:
                    pass
        transaction.on_commit(_clear_cache)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("⚠️ Kritická chyba: Zmazanie odvetvových nastavení je na úrovni jadra prísne zakázané!")

    def __str__(self):
        return "⚙️ Odvetvové lehoty revízií (Kompresory a VTZ)"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class KompresoryStatLokalizacia(ISASAuditMixin, models.Model):
    """
    🗺️ Odvetvová národná matica: Definuje špecifické zákonné lehoty VTZ
    pre tlakové zariadenia v jednotlivých štátoch (napr. SK, CZ, DE, AT).
    """
    stat = models.OneToOneField(
        'ISASaplikacia.StatLokalizacia',
        on_delete=models.CASCADE,
        related_name='kompresory_lehoty',
        verbose_name="🗺️ Štát",
        help_text="Výber štátu, pre ktorý platia tieto národné odvetvové lehoty."
    )
    lehota_vtz_prevadzkova_roky = models.PositiveIntegerField(
        "⏱️ Prevádzková revízia nádoby (roky)", 
        null=True, blank=True,
        help_text="Zákonná lehota prevádzkovej revízie / vonkajšej inšpekcie tlakových nádob v danom štáte."
    )
    lehota_vtz_vnutorna_roky = models.PositiveIntegerField(
        "⏱️ Vnútorná inšpekcia nádoby (roky)", 
        null=True, blank=True,
        help_text="Zákonná lehota vnútornej prehliadky tlakovej nádoby v danom štáte."
    )
    lehota_vtz_tlakova_roky = models.PositiveIntegerField(
        "⏱️ Tlaková skúška pevnosti (roky)", 
        null=True, blank=True,
        help_text="Zákonná lehota pevnostnej tlakovej skúšky nádoby v danom štáte."
    )
    lehota_vtz_poistny_ventil_roky = models.PositiveIntegerField(
        "🛡️ Preskúšanie poistného ventilu (roky)", 
        null=True, blank=True,
        help_text="Národná alebo normová lehota kontroly/výmeny poistného ventilu."
    )
    lehota_vtz_elektro_roky = models.PositiveIntegerField(
        "⚡ Bezpečnostná elektrorevízia (roky)", 
        null=True, blank=True,
        help_text="Národná lehota periodickej revízie priemyselných elektrických inštalácií."
    )
    lehota_vtz_manometer_roky = models.PositiveIntegerField(
        "⏱️ Overenie manometra (roky)", 
        null=True, blank=True,
        help_text="Národná metrologická lehota kalibrácie/overenia funkčnosti meradiel tlaku."
    )

    class Meta:
        db_table = 'kompresory_statlokalizacia'
        verbose_name = "🗺️ Národné lehoty VTZ (Kompresory)"
        verbose_name_plural = "🗺️ Národné lehoty VTZ (Kompresory)"

    def clean(self):
        super().clean()
        polia = [
            ('lehota_vtz_prevadzkova_roky', 'Prevádzková revízia'),
            ('lehota_vtz_vnutorna_roky', 'Vnútorná skúška'),
            ('lehota_vtz_tlakova_roky', 'Tlaková skúška'),
            ('lehota_vtz_poistny_ventil_roky', 'Poistný ventil'),
            ('lehota_vtz_elektro_roky', 'Elektrorevízia'),
            ('lehota_vtz_manometer_roky', 'Manometer'),
        ]
        for field_name, label in polia:
            val = getattr(self, field_name, None)
            if val is not None and val < 1:
                raise ValidationError({field_name: f"⚠️ {label} musí byť minimálne 1 rok."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        stat_iso = getattr(self.stat, 'iso_kod', None)
        def _clear_cache():
            try:
                cache.delete_pattern('isas_compliance_config*')
            except AttributeError:
                cache.delete('isas_compliance_config')
                if stat_iso:
                    cache.delete(f'isas_compliance_config_{stat_iso}')
        transaction.on_commit(_clear_cache)

    def delete(self, *args, **kwargs):
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            aktivne_iso = FiremnaIdentitaModel.get_aktivna_krajina_iso() if hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso') else None
            if aktivne_iso and aktivne_iso not in ('NEZADANE', 'INYKRAJINA') and self.stat and self.stat.iso_kod:
                if str(self.stat.iso_kod).strip().upper() == str(aktivne_iso).strip().upper():
                    raise PermissionDenied(f"🛑 Štát '{self.stat.iso_kod}' je sídlom materskej firmy. Národné odvetvové lehoty sídla je zakázané vymazať!")
        except PermissionDenied:
            raise
        except Exception:
            pass
        super().delete(*args, **kwargs)

    def __str__(self):
        iso = getattr(self.stat, 'iso_kod', '??') if self.stat_id else '??'
        return f"⚖️ VTZ Lehoty - {iso}"