from datetime import date
from decimal import Decimal
from .models import (
    Stroj, SpotrebnyMaterial, Poruchovydiel,
    StrojGA, StrojGAVSD, StrojZRZT,
    StrojPiestovy, StrojSusicka, StrojVzdusnik,
    SpotrebnyOlej, SpotrebnyVzduchovyFilter, SpotrebneVzduchoveVlozky,
    SpotrebnyOlejovyFilter, SpotrebnyOlejovySeparator, SpotrebnyPalivovyFilter,
    SpotrebnyKlinovyRemen, SpotrebnaSpojka, SpotrebnaSaciaKlapka,
    SpotrebnePotrubie, SpotrebnaServisnaSada, SpotrebneTesnenie,
    SpotrebnyOdkalovac, SpotrebnySeparatorKondenzatu, SpotrebnyDemister,
    SpotrebnyTermostat, SpotrebnaSadaVentilov, SpotrebnyFilterOdvzdusneniaPrevodovky,
    SpotrebneChladivo, SpotrebnyDesikant, SpotrebnaChemia,
    SpotrebneOstatne, PoruchovySnimacKabelaz, PoruchovaElektrovyzbroj,
    PoruchovaHadica, PoruchovyRiadiaciModul, PoruchovyVentilatorMotor,
    PoruchovyElement, PoruchovySolenoid, PoruchoveLozisko, PoruchovyChladicVymenik,
    PoruchoveOstatne,
    ParametreKompresor, ParametreSusicka, ParametreTlakovaNadoba,
    ParametreFiltracia, ParametreOdlucovac, ParametreGeneratorPlynu,
    ParametreChladic, ParametreVyveva, ParametreDuchadlo,
    ParametreKlimatizacia, ParametreCerpadlo, ParametreVztJednotka,
)

PARENT_MODELS = {
    "Stroje": Stroj,
    "Spotrebné náhradné diely": SpotrebnyMaterial,
    "Poruchové diely": Poruchovydiel,
}

ZARIADENIA_MAP = {
    'kompresor': ParametreKompresor,
    'susic': ParametreSusicka,
    'tlakova_nadoba': ParametreTlakovaNadoba,
    'filtracia': ParametreFiltracia,
    'odlucovac': ParametreOdlucovac,
    'generator_plynu': ParametreGeneratorPlynu,
    'chladic': ParametreChladic,
    'vyveva': ParametreVyveva,
    'duchadlo': ParametreDuchadlo,
    'klimatizacia': ParametreKlimatizacia,
    'cerpadlo': ParametreCerpadlo,
    'vzt_jednotka': ParametreVztJednotka,
}

ZARIADENIA_CHOICES = [
    ('kompresor', '💨 Kompresor (Skrutkový, Piestový, Špirálový)'),
    ('susic', '💧 Sušička vzduchu (Kondenzačná, Adsorpčná)'),
    ('tlakova_nadoba', '🛢️ Tlaková nádoba / Vzdušník (VTZ Tlakové)'),
    ('filtracia', '🌪️ Filtračná stanica / Cyklón / Uhlíková veža'),
    ('odlucovac', '🚰 Odlučovač oleja a vody z kondenzátu'),
    ('generator_plynu', '⚡ Generátor dusíka / kyslíka (PSA, Membrána)'),
    ('chladic', '❄️ Priemyselný chladič vody / oleja (Chiller)'),
    ('vyveva', '🕳️ Výveva / Vákuové čerpadlo'),
    ('duchadlo', '🌀 Dúchadlo (Roots, Bočný kanál)'),
    ('klimatizacia', '🌬️ Klimatizačné zariadenie / Tepelné čerpadlo'),
    ('cerpadlo', '🌊 Čerpadlo (Priemyselné, Kalové, Dávkovacie)'),
    ('vzt_jednotka', '🏢 Vzduchotechnická / Rekuperačná jednotka (AHU)'),
]

# ==============================================================================
# 📜 REGISTER ODBORNÝCH CERTIFIKÁCIÍ PRE STLAČENÝ VZDUCH A PRIEMYSEL
# ==============================================================================
CERTIFIKAT_CHOICES = [
    ('BOZP_PO', '🦺 BOZP, PO a Prvá pomoc'),
    ('VODIC_REFERENT', '🚗 Školenie vodičov referentských vozidiel (Zákon 124/2006 Z.z.)'),
    ('ELEKTRO', '⚡ Elektrotechnická spôsobilosť na opravy a údržbu (§21 - §23)'),
    ('ELEKTRO_RT', '⚡ Revízny technik vyhradených elektrických zariadení (§24)'),
    ('VTZ_TLAK_RT', '🛢️ Revízny technik VTZ tlakových zariadení a vzdušníkov (§16)'),
    ('VTZ_TLAK_OPRAVY', '🔧 Opravy, montáž a rekonštrukcie VTZ tlakových celkov (§15)'),
    ('VTZ_POISTNE_VENTILY', '🧭 Nastavovanie, lapovanie a overovanie poistných ventilov (TÚSR / TI)'),
    ('VTZ_PLYN', '🔥 VTZ Plynové zariadenia & Generátory dusíka / kyslíka (PSA, Membrána)'),
    ('FPLYNY', '❄️ Certifikát na chladiace médiá a F-plyny (Kondenzačné sušiče kat. I/II)'),
    ('POTRUBIA_SYSTEM', '🛤️ Certifikovaná montáž systémových rozvodov (Transair, Infinity, Viega)'),
    ('ZVARANIE', '👨‍🏭 Zváračský preukaz & Tvrdé spájkovanie Cu chladiarenských potrubí'),
    ('NDT_ULTRAZVUK', '🔬 NDT nedeštruktívne meranie hrúbky stien tlakových nádob (UT)'),
    ('AUDIT_ISO8573', '🌪️ Validácia čistoty vzduchu ISO 8573-1 (Aerosóly oleja, častice, rosný bod)'),
    ('DIAG_MERANIE', '📈 Vibrodiagnostika ložísk (ISO 18436) a ultrazvuková detekcia únikov'),
    ('ATEX_EX', '💥 Osvedčenie pre práce v prostredí s nebezpečenstvom výbuchu (ATEX / Ex)'),
    ('STROJE_VZV', '🚜 VZV a mobilné zdvíhacie pracovné plošiny'),
    ('VYSKY_POSTROJ', '🧗 Práce vo výškach a nad voľnou hĺbkou (Kotvenie a montáž potrubí)'),
    ('OEM_KOMPRESOR', '🏭 Továrenská autorizácia výrobcu (Atlas Copco, Kaeser, BOGE, ALMiG)'),
    ('AUDIT_ISO50001', '📋 Energetický audítor priemyselných systémov (ISO 50001, audit úspor)'),
    ('INE', '📜 Iné odborné osvedčenie'),
]

CATEGORY_MAP = {
    "Stroje": {
        "🌀 Skrutkový kompresor (Mazaný olejom - fixné otáčky)": StrojGA,
        "⚡ Skrutkový kompresor s frekvenčným meničom (VSD)": StrojGAVSD,
        "🍃 Bezolejový kompresor (Oil-Free Class 0)": StrojZRZT,
        "⚙️ Piestový kompresor": StrojPiestovy,
        "💧 Sušička vzduchu (Kondenzačná / Adsorpčná)": StrojSusicka,
        "🛢️ Tlaková nádoba / Vzdušník": StrojVzdusnik,
    },
    "Spotrebné náhradné diely": {
        "💧 Oleje": SpotrebnyOlej,
        "🌬️ Vzduchové filtre": SpotrebnyVzduchovyFilter,
        "💨 Filtre stlačeného vzduchu (vložky)": SpotrebneVzduchoveVlozky,
        "🚰 Olejové filtre": SpotrebnyOlejovyFilter,
        "⚗️ Olejové separátory": SpotrebnyOlejovySeparator,
        "⛽ Palivové filtre": SpotrebnyPalivovyFilter,
        "♾️ Klinové remene": SpotrebnyKlinovyRemen,
        "🔗 Spojky": SpotrebnaSpojka,
        "🎛️ Sacie klapky": SpotrebnaSaciaKlapka,
        "🛤️ Potrubia a rozvody": SpotrebnePotrubie,
        "🧰 Servisné sady": SpotrebnaServisnaSada,
        "⭕ Tesnenia": SpotrebneTesnenie,
        "🗑️ Odkalovače": SpotrebnyOdkalovac,
        "💦 Separátory kondenzátu": SpotrebnySeparatorKondenzatu,
        "🌫️ Demistre (Odlučovače hmloviny)": SpotrebnyDemister,
        "🌡️ Termostaty": SpotrebnyTermostat,
        "🧭 Sady ventilov (Spätné, MPV, VMT)": SpotrebnaSadaVentilov,
        "⚙️ Filtre odvzdušnenia prevodovky": SpotrebnyFilterOdvzdusneniaPrevodovky,
        "❄️ Chladivá (F-plyny)": SpotrebneChladivo,
        "🏜️ Sušivá / Desikanty": SpotrebnyDesikant,
        "🧪 Servisná chémia a spreje": SpotrebnaChemia,
        "🧩 Ostatné spotrebné náhradné diely": SpotrebneOstatne,
    },
    "Poruchové diely": {
        "🔌 Snímače + káble": PoruchovySnimacKabelaz,
        "⚡ Elektrovýzbroje": PoruchovaElektrovyzbroj,
        "〰️ Hadice": PoruchovaHadica,
        "💻 Riadiace moduly": PoruchovyRiadiaciModul,
        "🌀 Ventilátory a elektromotory": PoruchovyVentilatorMotor,
        "🔩 Skrutkové elementy": PoruchovyElement,
        "🧲 Solenoidy": PoruchovySolenoid,
        "🛞 Ložiská": PoruchoveLozisko,
        "🧊 Chladiče oleja a vzduchu (Výmenníky)": PoruchovyChladicVymenik,
        "🛠️ Ostatné poruchové diely": PoruchoveOstatne,
    }
}

CATEGORY_CHOICES = [
    ("Stroje", "⚙️ Stroje a technológie (Predaj a prenájom)"),
    ("Spotrebné náhradné diely", "🔧 Spotrebné náhradné diely (Plánovaná údržba)"),
    ("Poruchové diely", "🚨 Poruchové náhradné diely (Opravy a havárie)"),
]


def build_flat_category_map():
    flat_map = {}
    # Explicitné mapovanie cudzích kľúčov sekcií bez predčasného volania _meta.get_fields()
    parent_fk_map = {
        "Stroje": "stroj",
        "Spotrebné náhradné diely": "material",
        "Poruchové diely": "diel",
    }
    for top_category, subcategories in CATEGORY_MAP.items():
        parent_model_cls = PARENT_MODELS.get(top_category)
        if not parent_model_cls:
            continue
        parent_model_name = parent_model_cls.__name__
        fk_field_name = parent_fk_map.get(top_category, "stroj")

        for verbose_name, model_class in subcategories.items():
            if not model_class or not hasattr(model_class, '__name__'):
                continue
            flat_map[model_class.__name__] = {
                "model_name": model_class.__name__,
                "parent_model_name": parent_model_name,
                "fk_field": fk_field_name,
                "stock_cat": top_category,
                "verbose_name": verbose_name
            }

    return flat_map

FLAT_CATEGORY_MAP = build_flat_category_map()


# 📜 Špecifické CSS a JS médiá právneho disclaimera pre odvetvie kompresorov
COMPLIANCE_ADMIN_MEDIA = {
    'css': {'all': ('css/compliance_disclaimer.css',)},
    'js': ('js/compliance_disclaimer.js',)
}


# ==============================================================================
# 📖 SÉMANTICKÝ SLOVNÍK ODVETVIA (INDUSTRY LEXICON)
# Riadi dynamické pomenovanie prevádzkových veličín naprieč celým ERP (UI, PDF, AI, Boty)
# ==============================================================================
INDUSTRY_TERMS = {
    'meter_label': 'Motohodiny',
    'meter_label_short': 'MH',
    'meter_label_genitiv': 'motohodín',
    'meter_unit': 'MH',
    'meter_load_label': 'Motohodiny v záťaži',
    'meter_load_label_short': 'Záťaž MH',
    'meter_load_unit': 'MH',
    'meter_prompt_desc': 'celkový stav počítadla motohodín stroja (iba čisté číslo, napr. 15400)',
    'meter_load_prompt_desc': 'odpracované hodiny v záťaži pod tlakom (iba čisté číslo, napr. 8000)',
    'service_interval_meter': 'Interval motohodín',
    'gauge_unit': 'BAR',
    'kpi_strata_label': 'Strata (odľahčenie)',
    'kpi_strata_tooltip': 'Odhadované náklady na elektrinu spálenú počas chodu naprázdno (kompresor beží v odľahčení bez dodávky stlačeného vzduchu).',
    'esg_audit_stroj_label': 'Príkon kompresora',
    'esg_power_unit': 'kW',
    'esg_auditor_title': 'Audítor stlačeného vzduchu & ESG špecialista',
    'esg_audit_podiel_label': 'Chod bez záťaže / naprázdno (%)',
    'esg_idle_title': 'Chod bez záťaže & CO₂',
    'esg_audit_odporucanie': 'Investíciou do kompresora s frekvenčným meničom (VSD) alebo inštaláciou nadradeného riadenia stanice dokážete straty z chodu naprázdno eliminovať takmer na nulu.',
    'esg_savings_factor': '0.85',
    'sla_placeholder': 'Napr. Skrutkové kompresory, sušičky, dúchadlá',
    'revizie_title': 'Strážca revízií VTZ (Tlak)',
    'revizie_desc': 'Zistite termíny povinných skúšok tlakovej nádoby a vyhradených zariadení.',
    'revizia_perioda_1_label': 'Vnútorná inšpekcia nádoby',
    'revizia_perioda_2_label': 'Tlaková skúška pevnosti',
    'fplyny_title': 'F-Plyny a Chladivá',
    'fplyny_desc': 'Kontrola únikov chladiacich médií a periodicita podľa legislatívy.',
    'vibro_desc': 'ISO 10816 - Rýchle vyhodnotenie stavu ložísk skrutkového bloku.',
    'gauge_1_title': 'Tlak Vzduchu',
    'gauge_1_unit': 'BAR',
    'gauge_temp_unit': '°C',
    'gauge_2_unit': '°C',
    'gauge_current_unit': 'A',
    'gauge_3_unit': 'A',
    'gauge_2_title': 'Teplota Oleja',
    'gauge_3_title': 'Odber Prúdu',
    'rental_title': 'Požičovňa kompresorov a technológií',
    'rental_fallback_icon': '🌀',
    'rental_detail_label': 'Detail kompresora',
    'rental_catalog_btn': 'Zobraziť celú ponuku prenájmu',
    'cistota_title': 'ISO 8573-1 (Čistota vzduchu)',
    'cistota_desc': 'Trieda čistoty stlačeného vzduchu a technológia úpravy podľa aplikácie.',
    'dig_1_title': 'Hlavný motor',
    'dig_2_title': 'Ventilátor chladenia',
    'dig_error_title': 'Chyba systému',
    'gauge_1_base': 7.35,
    'gauge_1_max': 10,
    'gauge_2_base': 82.0,
    'gauge_2_max': 120,
    'gauge_3_base': 45.0,
    'gauge_3_max': 60,
    'stat_zariadenia_label': 'Servisovaných kompresorov a staníc',
    'pocet_zariadeni_label': 'Počet kompresorov / staníc',
    'typ_zariadeni_label': 'Typ kompresorov / technológie',
    'buyback_title': 'Odkúpime váš starý kompresor',
    'buyback_device_placeholder': 'Značka a model kompresora *',
    'buyback_photo_label': '📸 Fotografia kompresora / štítku',
    'hub_tab_esg': '⚡ Úspora energie & ESG',
    'hub_tab_law': '⚖️ Zákony & Revízie',
    'hub_tab_diag': '🔬 Diagnostika & Dimenzovanie',
    'stanoviste': 'Stanovište / Pozícia',
    'hmotnost': 'Hmotnosť',
    'hmotnost_jednotka': 'kg',
    'rok_vyroby': 'Rok výroby',
    'zaruka': 'Záruka',
    'umiestnenie': 'Umiestnenie',
    'zariadenie_genitiv': 'kompresora / zariadenia',
    'zariadenia_mnozno': 'kompresory a technológie stlačeného vzduchu',
    'zariadenia_mnozno_zapor': 'kompresory, úpravu vzduchu ani plynové technológie',
    'zataz_pomer_label': 'Podiel prevádzky v záťaži',
    'pas_title': 'Digitálny pas kompresora',
    'servisny_rezim_title': 'Kompresor je v servisnom režime údržby',
    'poziadavka_placeholder': 'Popíšte zistené príznaky poruchy, chybové kódy alebo rozsah požadovaných servisných prác...',
    'rekuperacia_title': 'Rekuperácia odpadového tepla kompresorov',
    'rekuperacia_fuel_label': 'Cena plynu / ohrevu vody',
    'tco_title': 'TCO kalkulátor kompresorovej stanice',
    'tco_desc': 'Rozpad celkových nákladov na vlastníctvo kompresora podľa profilu prevádzky.',
    'capex_title': 'Nákup kompresora vs. Prenájom',
    'capex_desc': 'Porovnanie nákupu nového kompresora voči dlhodobému prenájmu.',
    'capex_price_label': 'Cena nového kompresora',
    'capex_rent_label': 'Mesačný prenájom kompresora',
    'vibro_title': 'Vibrodiagnostika stroja',
    'vibro_input_label': 'Nameraná hodnota RMS (mm/s)',
    'tco_energy_label': 'Elektrická energia',
    'tco_service_label': 'Servis a údržba',
    'tco_capex_label': 'Nákupná cena (Investícia)',
    'dig_1_on': 'AKTÍVNY (V PREVÁDZKE)',
    'dig_1_off': 'NEAKTÍVNY',
    'dig_2_on': 'AKTÍVNY',
    'dig_2_off': 'NEAKTÍVNY',
    'dig_error_ok': 'V NORME (BEZ PORÚCH)',
    'dig_error_fail': 'PORUCHA',
    'ticket_placeholder': 'Napr. ZAK-{year}-001',
    'ticket_status_vybavena': 'Opravené / Pripravené na expedíciu',
    'polozka_fallback_nazov': 'Náhradný diel',
    'zariadenie_fallback_nazov': 'Zariadenie / Stroj',
    'sekcia_material_label': '📦 Dodávka materiálu a dielov',
    'sekcia_prace_label': '🔧 Odborná práca a doprava',
    'vyjazd_label': 'Servisný výjazd',
    'vyjazd_jednotka': 'výjazd',
    'esg_energy_price_label': 'Cena elektriny',
    'cistota_select_label': 'Odvetvie / Prevádzka',
    'cistota_level_label': 'Požadovaná úroveň akosti',
    'cistota_tech_label': 'Doporučená technológia',
    'revizie_date_label': 'Dátum uvedenia do prevádzky',
    'fplyn_type_label': 'Typ chladiva',
    'fplyn_amount_label': 'Náplň (kg)',
    'tco_profile_label': 'Typ prevádzky / Technológia',
    'prompt_material_desc': 'Zoznam použitého materiálu a náhradných dielov vrátane množstva (napr. 1x ložisko, 2x tesnenie, 1x olejový filter, 1x sacia klapka). Ak nič nemenil, vráť null.',
    'prompt_namerane_hodnoty': 'Fyzikálne veličiny spomenuté po servise (tlaky v Bar/Pa, prietoky, teploty oleja/vzduchu v °C, prúdy v A, napätie, otáčky, diferenčné tlaky filtrov). Sformátuj ich prehľadne. Ak nespomenul, vráť null.',
    'prompt_zmena_hardveru': 'true/false (Boolean. Hodnota true IBA v prípade, ak technik vyslovene povie, že menil hlavný agregát, napr. elektromotor, skrutkový blok, frekvenčný menič, riadiacu jednotku. Bežná výmena spotrebného materiálu či dielov podliehajúcich bežnému opotrebeniu je false.)',
}

# 🏛️ ODVETVOVÝ KONTRAKT PRE JADRO ERP
HAS_ENERGY_ESG_AUDIT = True
HAS_FPLYNY_AUDIT = True
HAS_HEAT_RECOVERY = True
HAS_VTZ_INSPECTION = True
HAS_VIBRO_DIAGNOSTICS = True
HAS_CAPEX_CALCULATOR = True
DEFAULT_STOCK_MODEL = 'SpotrebneOstatne'
COMPLIANCE_ALARM_PREFIXES = ('VTZ-', 'FPLYN', 'TECH-', 'ENV-', 'ISO-VIB-')

# 🎮 Odvetvové názvy úrovní pre Easter Egg 2048
EASTER_EGG_TILES = {
    2: "🔩 Podložka M8",
    4: "⭕ O-krúžok (Tesnenie)",
    8: "🛢️ Olejový filter",
    16: "🔌 Elektromagnetický ventil",
    32: "⚡ Snímač tlaku 4-20 mA",
    64: "🌀 Frekvenčný menič",
    128: "⚙️ Skrutkový blok",
    256: "🏭 Strojovňa klienta",
    512: "📋 5-ročný SLA paušál",
    1024: "💰 Zákazka za 250k",
    2048: "🏆 ISAS LEGENDA (Riaditeľ)",
    4096: "👑 Priemyselný Oligarcha",
    8192: "🚀 SaaS Jednorožec",
}

FPLYNY_CHLADIVA = [
    {'kod': 'R410A', 'nazov': 'R410A (GWP 2088)', 'gwp': 2088},
    {'kod': 'R134a', 'nazov': 'R134a (GWP 1430)', 'gwp': 1430},
    {'kod': 'R404A', 'nazov': 'R404A (GWP 3922)', 'gwp': 3922},
    {'kod': 'R32', 'nazov': 'R32 (GWP 675)', 'gwp': 675},
]

HUB_CONFIG = {
    'esg_annual_hours': 4000,
    'esg_co2_factor': 0.115,
    'esg_savings_factor': 0.85,
    'esg_idle_factor': float(getattr(ParametreKompresor, 'koeficient_naprazdno', Decimal('0.30'))),
    'tco_years': 10,
    'revizia_perioda_1_roky': 5,
    'revizia_perioda_2_roky': 10,
    'esg_default_kw': 55,
    'esg_default_percent': 30,
    'esg_default_price': 0.15,
    'rekuperacia_factor': 0.72,
    'rekuperacia_default_kw': 75,
    'rekuperacia_default_hours': 4000,
    'rekuperacia_default_price': 0.08,
    'fplyn_default_kg': 3.5,
    'fplyn_limits': {'small': 5, 'medium': 50, 'large': 500},
    'fplyn_male_mesiace': 12,
    'fplyn_stredne_mesiace': 6,
    'fplyn_velke_mesiace': 3,
    'fplyn_label_none': 'Bez povinnej kontroly',
    'vibro_limits': {'limit_a': 2.8, 'limit_b': 7.1, 'limit_c': 18.0},
    'vibro_zones': {
        'a': {'zone': 'Zóna A (Výborný stav)', 'text': 'Stroj je v poriadku'},
        'b': {'zone': 'Zóna B (Prijateľný stav)', 'text': 'Vhodné pre trvalú prevádzku'},
        'c': {'zone': 'Zóna C (Varovanie)', 'text': 'Naplánovať servisný zásah'},
        'd': {'zone': 'Zóna D (Kritický stav)', 'text': 'Riziko zadretia! Odstaviť.'},
    },
    'gauge_warn_ratio': 0.75,
    'capex_maintenance_factor': 0.05,
    'capex_years': 5,
    'capex_default_price': 15000,
    'capex_default_rent': 350,
}

TCO_PROFILES = {
    'heavy_fixed': {
        'label': '🏭 Nepretržitá 3-zmenná (Fixné otáčky)',
        'energy': 78, 'service': 14, 'capex': 8,
        'tip': 'Pri nepretržitej prevádzke tvorí energia prevažnú časť nákladov (78 %). Kľúčom k zisku je minimalizácia strát energie.'
    },
    'vsd_smart': {
        'label': '⚡ S frekvenčným meničom (VSD Invertor)',
        'energy': 55, 'service': 20, 'capex': 25,
        'tip': 'Inteligentná regulácia otáčok eliminuje chod naprázdno a prináša výraznú úsporu elektriny.'
    },
    'single_shift': {
        'label': '🛠️ 1-zmenná dielňa (8 hod / deň)',
        'energy': 65, 'service': 18, 'capex': 17,
        'tip': 'Pri jednozmennej prevádzke tvoria energie väčšinu nákladov. Odporúčame technológiu mimo pracovných hodín odpájať.'
    },
    'oil_free': {
        'label': '🧪 Bezolejová čistá technológia (Class 0)',
        'energy': 62, 'service': 26, 'capex': 12,
        'tip': 'Špecializované čisté technológie vyžadujú certifikovaný servis pre trvalé garantovanie čistoty média.'
    }
}

MEDIA_QUALITY_APPS = [
    {
        'id': 'food',
        'label': 'Potravinárstvo / Farmácia (Vysoká čistota)',
        'class_label': 'ISO 8573-1 Trieda 1 (Sterilné)',
        'tech_recommendation': 'Viacstupňová ultrafiltrácia + adsorpčné sušenie + sterilný filter'
    },
    {
        'id': 'laser',
        'label': 'Presná technológia / CNC / Laser',
        'class_label': 'ISO 8573-1 Trieda 1-2 (Bez vlhkosti)',
        'tech_recommendation': 'Adsorpčné sušenie s tlakovým rosným bodom -40 °C + mikrofiltrácia'
    },
    {
        'id': 'paint',
        'label': 'Povrchová úprava a lakovanie',
        'class_label': 'Technologická akosť (Bez silikónu a oleja)',
        'tech_recommendation': 'Kondenzačné odvlhčenie + uhlíková veža na odstránenie olejových pár'
    },
    {
        'id': 'workshop',
        'label': 'Štandardná priemyselná prevádzka',
        'class_label': 'Štandardná dielenská akosť (Trieda 4)',
        'tech_recommendation': 'Kondenzačná sušička + cyklónový odlučovač kondenzátu'
    }
]

# =========================================
# 📥 VZOROVÉ DÁTA PRE IMPORT (EXCEL / CSV)
# =========================================

def get_sample_import_data() -> dict:
    """
    🛡️ ISAS ARCHITECT: Lazy factory pre vzorové importné dáta kompresorov.
    """
    dph_val = ""
    try:
        from django.apps import apps
        if apps.ready:
            GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
            if GlobalneNastavenia and hasattr(GlobalneNastavenia, 'get_zakladna_dph'):
                dph_dec = GlobalneNastavenia.get_zakladna_dph()
                if dph_dec and dph_dec > 0:
                    val = str(dph_dec)
                    dph_val = val.rstrip('0').rstrip('.') if '.' in val else val

    except Exception:
        dph_val = ""

    return {
        'SERVISOVANE_ZARIADENIA': [
            '12345678', 'kompresor', 'Atlas Copco', 'GA 11 VSD', 'SN-998877',
            'Kompresor Hala A', str(date.today().year), date.today().strftime('%d.%m.%Y'),
            date.today().strftime('%d.%m.%Y'), '', '350', 'Miestnosť údržby',
            '4500', '3200', '00:1A:2B:3C:4D:5F', '192.168.1.50', 'MODBUS',
            'ÁNO', '60', 'NIE', 'NIE', '0.00', 'NIE', 'NIE',
            'Stroj je v prašnom prostredí'
        ],
        'SKLAD_KARTY': [
            'Spotrebné náhradné diely', '💧 Oleje', 'OL-5W40-10L',
            'SKU-123', '8581234567890', 'Syntetický olej pre kompresory', 'NIE',
            'Kompresorový olej Roto-Inject Fluid', '15', 'l', 'TOVAR', dph_val, 'ORIGINÁL',
            '87654321', 'Atlas Copco', 'ÁNO', 'Vysoko výkonný syntetický olej pre skrutkové kompresory'
        ],
    }

class _LazySampleImportData(dict):
    """
    🛡️ ISAS ARCHITECT: Plne defenzívny lazy proxy slovník.
    Garantuje korektnú reprezentáciu v logoch, bezpečné kopírovanie a transparentnú delegáciu.
    """
    def _data(self):
        return get_sample_import_data()

    def __getitem__(self, item):
        return self._data()[item]
    def get(self, k, default=None):
        return self._data().get(k, default)
    def values(self):
        return self._data().values()
    def items(self):
        return self._data().items()
    def keys(self):
        return self._data().keys()
    def __iter__(self):
        return iter(self._data())
    def __len__(self):
        return len(self._data())
    def __bool__(self):
        return bool(self._data())
    def __contains__(self, key):
        return key in self._data()
    def __repr__(self):
        return repr(self._data())
    def __str__(self):
        return str(self._data())
    def copy(self):
        return self._data().copy()

SAMPLE_IMPORT_DATA = _LazySampleImportData()

ZARIADENIA_COLOR_MAP = {
    'kompresor': 'purple',
    'generator_plynu': 'purple',
    'duchadlo': 'purple',
    'vyveva': 'purple',
    'susic': 'success',
    'filtracia': 'success',
    'odlucovac': 'success',
    'tlakova_nadoba': 'warning',
    'chladic': 'info',
    'klimatizacia': 'info',
    'vzt_jednotka': 'info',
    'cerpadlo': 'info',
}