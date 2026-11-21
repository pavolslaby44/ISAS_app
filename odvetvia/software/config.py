from datetime import date
from decimal import Decimal
from typing import Any
from .models import (
    Hardver, Licencia, SlaSluzba,
    HardverRackServer, HardverCloudVPS, HardverDiskovePole,
    HardverSietovyPrvok, HardverFirewallWAF,
    LicenciaOperacnySystem, LicenciaDatabaza, LicenciaSaaSPredplatne,
    LicenciaSSLCertifikat, LicenciaDomenaDNS, LicenciaOstatne,
    SlaIncidentPauzal, SlaClovekohodinyVyvoj,
    ParametreServer, ParametreAplikacia, ParametreDatabaza,
)

# ==============================================================================
# 🏢 KMEŇOVÉ SEKCIE SKLADU (PARENT MODELS)
# ==============================================================================

PARENT_MODELS = {
    "Hardvér a výpočtová infraštruktúra": Hardver,
    "Licencie a digitálne predplatné": Licencia,
    "SLA podpora a inžinierske služby": SlaSluzba,
}


# ==============================================================================
# ⚙️ TECHNICKÉ PARAMETRE ZARIADENÍ (ZARIADENIA MAP & CHOICES)
# ==============================================================================

ZARIADENIA_MAP = {
    'server': ParametreServer,
    'aplikacia': ParametreAplikacia,
    'databaza': ParametreDatabaza,
}

ZARIADENIA_CHOICES = [
    ('server', '🖥️ Server a výpočtový uzol (Bare-metal, Cloud VM, K8s)'),
    ('aplikacia', '📱 Webová aplikácia / API mikroslužba (Frontend, Backend)'),
    ('databaza', '🗄️ Databázový cluster a dátové úložisko (SQL, NoSQL, S3)'),
]

# ==============================================================================
# 📜 REGISTER ODBORNÝCH CERTIFIKÁCIÍ PRE IT, CLOUD A KYBERBEZPEČNOSŤ
# ==============================================================================
CERTIFIKAT_CHOICES = [
    ('BOZP_PO', '🦺 BOZP, PO a Prvá pomoc'),
    ('VODIC_REFERENT', '🚗 Školenie vodičov referentských vozidiel (Výjazdy do DC / ku klientom)'),
    ('ELEKTRO', '⚡ Elektrotechnická spôsobilosť (§20 - §22 pre Dátové centrá)'),
    ('IT_CLOUD', '☁️ Cloudová architektúra & SysOps (AWS, Azure, GCP)'),
    ('IT_VIRT', '🖥️ Virtualizácia & Hypervízory (VMware VCP, Proxmox, Hyper-V)'),
    ('IT_DEVOPS', '🐳 Kontajnerizácia & Orchestrácia (Kubernetes CKA/CKAD, Docker)'),
    ('IT_SEC', '🛡️ Kybernetická bezpečnosť & Pen-test (CISSP, CEH, OSCP)'),
    ('NET_SEC_FW', '🧱 Sieťová bezpečnosť & Firewally (Fortinet NSE, Palo Alto PCNSE, Check Point)'),
    ('OEM_NET_ROUTING', '🌐 Enterprise siete & Routing (Cisco CCNP/CCIE, Juniper JNCIA)'),
    ('OEM_HW_SERVER', '🏭 Servisná autorizácia HW & Storage (Dell, HPE Master ASE, Lenovo, NetApp)'),
    ('DATA_AI', '🗄️ Databázová správa, Big Data & AI (PostgreSQL, Oracle, Databricks)'),
    ('BCM_DR_BACKUP', '🔄 Kontinuita prevádzky & DR zálohovanie (ISO 22301 BCM, Veeam VMCE)'),
    ('DEV_LANG', '💻 Vývojárske certifikácie (Java OCP, Python, C#, Go, Node.js)'),
    ('QA_TESTING', '🧪 QA inžiniering & Testovanie softvéru (ISTQB Certified Tester)'),
    ('ITIL_SLA', '👔 ITIL 4, SLA Service Management & Incident Response'),
    ('AGILE_MGMT', '🎯 Scrum Master, Agile & IT Projektové riadenie (PSM, PMP)'),
    ('AUDIT_ISO', '📋 Audítor informačnej bezpečnosti (ISO 27001 Lead Auditor, NIS2)'),
    ('PRIVACY_GDPR', '⚖️ Ochrana osobných údajov & DPO (CIPP/E, GDPR audítor)'),
    ('DC_FACILITY', '🏢 Infraštruktúra dátových centier & Kabeláž (CDCP, Optické trasy, Fluke)'),
    ('INE', '📜 Iné odborné osvedčenie'),
]


# ==============================================================================
# 📦 HIERARCHICKÁ MAPA SKLADOVÝCH KATEGÓRIÍ (13 MODELOV)
# ==============================================================================

CATEGORY_MAP = {
    "Hardvér a výpočtová infraštruktúra": {
        "🖥️ Rackový server (Bare-metal)": HardverRackServer,
        "☁️ Virtuálny server (Cloud VPS / VM)": HardverCloudVPS,
        "💾 Diskové pole (SAN / NAS úložisko)": HardverDiskovePole,
        "🌐 Sieťový prvok (Switch, Router, Optika)": HardverSietovyPrvok,
        "🛡️ Hardvérový firewall a WAF brána": HardverFirewallWAF,
    },
    "Licencie a digitálne predplatné": {
        "🪟 Licencia operačného systému (OS)": LicenciaOperacnySystem,
        "🗄️ Databázová licencia (DB)": LicenciaDatabaza,
        "📱 SaaS cloudové predplatné": LicenciaSaaSPredplatne,
        "🔒 SSL / TLS certifikát": LicenciaSSLCertifikat,
        "🌐 Registrácia domény a DNS": LicenciaDomenaDNS,
        "🧩 Ostatné softvérové licencie": LicenciaOstatne,
    },
    "SLA podpora a inžinierske služby": {
        "⏱️ SLA paušál a incidentový servis": SlaIncidentPauzal,
        "👨‍💻 Inžinierske človekohodiny a vývoj (MD / h)": SlaClovekohodinyVyvoj,
    }
}

CATEGORY_CHOICES = [
    ("Hardvér a výpočtová infraštruktúra", "🖥️ Hardvér a výpočtová infraštruktúra (Predaj a prenájom)"),
    ("Licencie a digitálne predplatné", "🔑 Licencie a digitálne predplatné (Softvér a certifikáty)"),
    ("SLA podpora a inžinierske služby", "🤝 SLA podpora a inžinierske služby (Podpora a vývoj)"),
]


# ==============================================================================
# 🗺️ GENERÁTOR PLOCHEJ MAPY SKLADU (PRE INDUSTRY LOADER JADRA)
# ==============================================================================

def build_flat_category_map():
    flat_map = {}
    # Explicitné mapovanie cudzích kľúčov sekcií bez predčasného volania _meta.get_fields()
    parent_fk_map = {
        "Hardvér a výpočtová infraštruktúra": "hardver",
        "Licencie a digitálne predplatné": "licencia",
        "SLA podpora a inžinierske služby": "sluzba",
    }
    for top_category, subcategories in CATEGORY_MAP.items():
        parent_model_cls = PARENT_MODELS.get(top_category)
        if not parent_model_cls:
            continue
        parent_model_name = parent_model_cls.__name__
        fk_field_name = parent_fk_map.get(top_category, "hardver")

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


# 📜 Statické médiá právneho disclaimera a admin rozhrania pre IT modul
COMPLIANCE_ADMIN_MEDIA = {
    'css': {'all': ('css/compliance_disclaimer.css',)},
    'js': ('js/compliance_disclaimer_software.js',)
}


# ==============================================================================
# 📖 SÉMANTICKÝ SLOVNÍK ODVETVIA (INDUSTRY LEXICON)
# Riadi dynamické pomenovanie prevádzkových veličín naprieč celým ERP (UI, PDF, AI, Boty)
# ==============================================================================

INDUSTRY_TERMS = {
    'meter_label': 'Uptime',
    'meter_label_short': 'Uptime',
    'meter_label_genitiv': 'hodín uptime',
    'meter_unit': 'h',
    'meter_load_label': 'Čas v špičke (Peak)',
    'meter_load_label_short': 'Peak h',
    'meter_load_unit': 'h',
    'meter_prompt_desc': 'celkový čas nepretržitej prevádzky systému v hodinách (uptime, iba čisté číslo, napr. 4320 pre 180 dní)',
    'meter_load_prompt_desc': 'odpracované hodiny v špičkovom zaťažení CPU peak (iba čisté číslo, napr. 450)',
    'service_interval_meter': 'Servisný interval (Uptime h)',
    'gauge_unit': '%',
    'kpi_strata_label': 'Výpadok SLA',
    'kpi_strata_tooltip': 'Odhadované finančné straty a penalizácie vyplývajúce z nedodržania garantovanej dostupnosti SLA.',
    'sla_placeholder': 'Napr. Cloud servery, Kubernetes, PostgreSQL cluster, REST API',
    'revizie_title': 'Strážca IT revízií (NIS2 & Audity)',
    'revizie_desc': 'Zistite termíny povinných bezpečnostných auditov NIS2, penetračných testov a rotácie prístupov.',
    'revizia_perioda_1_label': 'Bezpečnostný audit NIS2',
    'revizia_perioda_2_label': 'Penetračný test systémov',
    'gauge_1_title': 'Vyťaženie CPU',
    'gauge_1_unit': '%',
    'gauge_temp_unit': '%',
    'gauge_2_unit': '%',
    'gauge_current_unit': 'ms',
    'gauge_3_unit': 'ms',
    'gauge_2_title': 'Využitie RAM',
    'gauge_3_title': 'Sieťová latencia',
    'rental_title': 'Prenájom dedikovaných serverov a Cloud VPS',
    'rental_fallback_icon': '☁️',
    'rental_detail_label': 'Detail serverovej inštancie',
    'rental_catalog_btn': 'Zobraziť ponuku cloudovej infraštruktúry',
    'cistota_title': 'SLA Dostupnosť (High Availability)',
    'cistota_desc': 'Garancia dostupnosti prevádzky (99.9% až 99.999%) a architektúra vysokej dostupnosti.',
    'dig_1_title': 'Web / API Služba',
    'dig_2_title': 'Databázový cluster',
    'dig_error_title': 'Stav infraštruktúry',
    'gauge_1_base': 45.0,
    'gauge_1_max': 100,
    'gauge_2_base': 60.0,
    'gauge_2_max': 100,
    'gauge_3_base': 25.0,
    'gauge_3_max': 200,
    'stat_zariadenia_label': 'Spravovaných serverov a systémov',
    'pocet_zariadeni_label': 'Počet serverov / nodov',
    'typ_zariadeni_label': 'Typ infraštruktúry / stacku',
    'buyback_title': 'Odkúpime alebo zrecyklujeme váš starý IT hardvér',
    'buyback_device_placeholder': 'Značka a konfigurácia servera / HW *',
    'buyback_photo_label': '📸 Fotografia servera / výrobného štítku',
    'hub_tab_esg': '⚡ Cloud FinOps & Úspory',
    'hub_tab_law': '⚖️ NIS2 & Bezpečnostné audity',
    'hub_tab_diag': '🔬 SRE Telemetria & Monitoring',
    'stanoviste': 'Dátové centrum / Rack pozícia',
    'hmotnost': 'Výška v racku',
    'hmotnost_jednotka': 'U',
    'rok_vyroby': 'Rok nasadenia',
    'zaruka': 'Záruka / HW vendor SLA',
    'umiestnenie': 'Lokalita (DC / Región)',
    'zariadenie_genitiv': 'servera / softvérového systému',
    'zariadenia_mnozno': 'servery, cloudové uzly a aplikácie',
    'zariadenia_mnozno_zapor': 'servery, cloudové uzly ani aplikácie',
    'zataz_pomer_label': 'Podiel špičkového zaťaženia',
    'pas_title': 'Digitálny pas IT infraštruktúry',
    'servisny_rezim_title': 'Systém je v režime údržby (Maintenance window)',
    'poziadavka_placeholder': 'Popíšte zistený incident, chybové kódy (HTTP 5xx), stack trace alebo požiadavku na konfiguráciu...',
    'tco_title': 'TCO kalkulátor IT infraštruktúry',
    'tco_desc': 'Porovnanie nákladov: Nákup vlastného servera vs. mesačný prenájom Cloud VPS (5-ročný horizont).',
    'capex_title': 'On-Premise Server vs. Cloud VPS',
    'capex_desc': 'Porovnanie nákupu nového hardvéru voči flexibilnému cloudovému prenájmu.',
    'capex_price_label': 'Cena hardvéru (Nákup servera)',
    'capex_rent_label': 'Mesačné náklady na Cloud VPS',
    'tco_energy_label': 'Housing, elektrina a konektivita',
    'tco_service_label': 'Správa, SLA podpora a monitoring',
    'tco_capex_label': 'Nákupná cena hardvéru (Investícia)',
    'dig_1_on': 'ONLINE (V PREVÁDZKE)',
    'dig_1_off': 'OFFLINE (ODSTAVENÉ)',
    'dig_2_on': 'SYNCHRONIZOVANÁ',
    'dig_2_off': 'DESYNCHRONIZOVANÁ',
    'dig_error_ok': 'V NORME (ZDRAVÝ SYSTÉM)',
    'dig_error_fail': 'INCIDENT / ERROR',
    'ticket_placeholder': 'Napr. INC-{year}-001',
    'ticket_status_vybavena': 'Vyriešené / Nasadené',
    'polozka_fallback_nazov': 'Licencia / Komponent',
    'zariadenie_fallback_nazov': 'Server / Systém',
    'sekcia_material_label': '💻 Hardvér a digitálne licencie',
    'sekcia_prace_label': '🤝 SLA podpora a inžinierske služby',
    'vyjazd_label': 'On-site zásah / Výjazd',
    'vyjazd_jednotka': 'zásah',
    'cistota_select_label': 'Úroveň dostupnosti SLA',
    'cistota_level_label': 'Garantované SLA',
    'cistota_tech_label': 'Odporúčaná architektúra',
    'revizie_date_label': 'Dátum spustenia do produkcie',
    'tco_profile_label': 'Profil vyťaženia / Architektúra',
    'prompt_material_desc': 'Zoznam použitých licencií, digitálnych predplatných, cloudových služieb alebo hardvérových komponentov vrátane počtu (napr. 1x OS licencia, 2x RAM modul, 1x SSL certifikát, 1x NVMe SSD disk). Ak nič nemenil/nepoužil, vráť null.',
    'prompt_namerane_hodnoty': 'Telemetrické a prevádzkové IT metriky (vyťaženie CPU %, RAM %, odozva/latencia v ms, chybovosť HTTP v %, disková kapacita GB, IOPS, priepustnosť siete). Sformátuj ich prehľadne. Ak nespomenul, vráť null.',
    'prompt_zmena_hardveru': 'true/false (Boolean. Hodnota true IBA v prípade, ak technik menil fyzický server, CPU, RAM moduly, diskové pole, cloudovú instanciu/vCPU alebo sieťový prvok. Bežná konfigurácia softvéru, inštalácia patchov a nastavenie služieb je false.)',
}


# ==============================================================================
# 🏛️ ODVETVOVÝ KONTRAKT PRE JADRO ERP (FEATURE TOGGLES)
# ==============================================================================

HAS_ENERGY_ESG_AUDIT = False       # Vylúčené: Meranie únikov stlačeného vzduchu
HAS_FPLYNY_AUDIT = False           # Vylúčené: F-plyny a chladivá
HAS_HEAT_RECOVERY = False          # Vylúčené: Rekuperácia tepla kompresorov
HAS_VTZ_INSPECTION = False         # Vylúčené: Tlakové nádoby VTZ
HAS_VIBRO_DIAGNOSTICS = False      # Vylúčené: Vibrodiagnostika ložísk ISO 10816
HAS_CAPEX_CALCULATOR = True        # Aktívne: On-Premise server vs. Cloud VPS kalkulátor
DEFAULT_STOCK_MODEL = 'LicenciaOstatne'
COMPLIANCE_ALARM_PREFIXES = ('IT-SSL-', 'IT-DOM-', 'IT-SEC-', 'IT-BCK-', 'IT-PERF-')


# ==============================================================================
# 🎮 ODVETVOVÉ NÁZVY ÚROVNÍ PRE EASTER EGG 2048
# ==============================================================================

EASTER_EGG_TILES = {
    2: "🔌 RJ45 konektor",
    4: "💾 RAM modul 16GB",
    8: "💿 NVMe SSD disk",
    16: "🧠 Procesor Xeon / EPYC",
    32: "🌐 Manažovateľný Switch",
    64: "🛡️ Hardvérový Firewall",
    128: "🖥️ Rackový Server 2U",
    256: "🏢 Dátové centrum Tier III",
    512: "📋 Ročný SLA Enterprise kontrakt",
    1024: "💰 Cloudová zákazka za 250k",
    2048: "🏆 ISAS LEGENDA (CISO / CTO)",
    4096: "👑 Tech Oligarcha",
    8192: "🚀 SaaS Jednorožec",
}


# ==============================================================================
# ❄️ NÁHRADA F-PLYNOV PRE IT MODUL (PRÁZDNA ŠTRUKTÚRA PRE KOMPATIBILITU)
# ==============================================================================
FPLYNY_CHLADIVA: list[dict[str, Any]] = []


# ==============================================================================
# 🎛️ ODVETVOVÉ NASTAVENIA PORTÁLU (HUB CONFIG)
# ==============================================================================

HUB_CONFIG = {
    'tco_years': 5,
    'revizia_perioda_1_roky': 2,       # Bezpečnostný audit NIS2
    'revizia_perioda_2_roky': 1,       # Penetračný test systémov
    'gauge_warn_ratio': 0.85,          # Prah varovania pre vyťaženie CPU/RAM (85 %)
    'capex_years': 5,                  # 5-ročný horizont TCO kalkulátora
    'capex_default_price': 8000,       # Odhadovaná nákupná cena On-Premise servera
    'capex_default_rent': 220,         # Odhadovaný mesačný náklad na Cloud VPS inštanciu
    'capex_maintenance_factor': 0.15,  # 15 % ročné náklady na housing, elektrinu a správu vlastného HW
}



# ==============================================================================
# 📊 TCO PROFILY IT INFRAŠTRUKTÚRY
# ==============================================================================

TCO_PROFILES = {
    'on_premise': {
        'label': '🏢 Vlastný On-Premise server (Colocation)',
        'energy': 30, 'service': 30, 'capex': 40,
        'tip': 'Vysoká počiatočná investícia do hardvéru (40 %) a potreba pravidelnej údržby. Vhodné pri požiadavkách na lokálnu suverenitu dát.'
    },
    'cloud_vps': {
        'label': '☁️ Verejný cloud (Cloud VPS / VM inštancia)',
        'energy': 0, 'service': 25, 'capex': 75,
        'tip': 'Nulový počiatočný CAPEX. Celé náklady tvorí predplatné (OPEX) s možnosťou dynamického škálovania zdrojov.'
    },
    'hybrid_ha': {
        'label': '🛡️ Hybridná infraštruktúra s HA clustrom',
        'energy': 20, 'service': 40, 'capex': 40,
        'tip': 'Maximálna bezpečnosť a kontinuita prevádzky. Vyžaduje vyššie náklady na správu a seniorný DevOps dohľad.'
    },
    'serverless': {
        'label': '⚡ Serverless & Kontajnery (Pay-as-you-go)',
        'energy': 0, 'service': 45, 'capex': 55,
        'tip': 'Náklady sa účtujú podľa počtu požiadaviek. Ideálne pre dynamicky vyťažené API služby a mikroslužby.'
    }
}


# ==============================================================================
# 🛡️ ARCHITEKTÚRA DOSTUPNOSTI (SLA HIGH AVAILABILITY)
# ==============================================================================

MEDIA_QUALITY_APPS = [
    {
        'id': 'tier4',
        'label': 'Kritické transakčné systémy (Fintech / Zdravotníctvo)',
        'class_label': 'SLA 99.999% (High Availability)',
        'tech_recommendation': 'Multi-región HA cluster + automatický failover + 24/7 SLA podpora do 15 minút'
    },
    {
        'id': 'tier3',
        'label': 'Produkčné B2B platformy & SaaS riešenia',
        'class_label': 'SLA 99.9% (Štandard)',
        'tech_recommendation': 'Master-replica databáza + Cloud load balancing + denné DR zálohovanie'
    },
    {
        'id': 'internal',
        'label': 'Interné firemné IS a ERP systémy',
        'class_label': 'SLA 99.5% (Pracovné hodiny)',
        'tech_recommendation': 'Dedikovaný bare-metal server alebo VPS s denným snapshot zálohovaním'
    },
    {
        'id': 'dev',
        'label': 'Vývojové a testovacie prostredia (Staging)',
        'class_label': 'Best-effort bez SLA garancie',
        'tech_recommendation': 'Zdieľané cloudové inštancie s nočným vypínaním pre úsporu nákladov'
    }
]


# =========================================
# 📥 VZOROVÉ DÁTA PRE IMPORT (EXCEL / CSV)
# =========================================

def get_sample_import_data() -> dict:
    """
    🛡️ ISAS ARCHITECT: Lazy factory pre vzorové importné dáta.
    Zaručuje načítanie aktuálnej DPH a dátumu v čase požiadavky, bez AppRegistryNotReady kolízie.
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
            '12345678', 'server', 'Dell Technologies', 'PowerEdge R750', 'SN-DELL-8821',
            'Hlavný databázový uzol', str(date.today().year), date.today().strftime('%d.%m.%Y'),
            date.today().strftime('%d.%m.%Y'), '', '25', 'Dátové centrum, Rack 14',
            '4320', '450', '00:1A:2B:3C:4D:5E', '192.168.1.100', 'SNMP',
            'ÁNO', '60', 'NIE', 'NIE', '0.00', 'NIE', 'NIE',
            'Produkčný server s aktívnym dohľadom Prometheus'
        ],
        'SKLAD_KARTY': [
            'Licencie a digitálne predplatné', '🪟 Licencia operačného systému (OS)', 'LIC-WIN-SVR-2022',
            'SKU-OS-2022', '0889842886009', 'Windows Server 2022 Standard 16-Core', 'NIE',
            'OEM Softvérová licencia', '5', 'ks', 'TOVAR', dph_val, 'ORIGINÁL',
            '87654321', 'Microsoft', 'ÁNO', 'Serverová licencia pre 16 jadier'
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


# ==============================================================================
# 🎨 COLOR MAP PRE FRONTENDOVÉ ODZNAKY
# ==============================================================================

ZARIADENIA_COLOR_MAP = {
    'server': 'purple',
    'aplikacia': 'success',
    'databaza': 'warning',
}