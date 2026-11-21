from datetime import timedelta
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

# 2. Kalendárne utilitky a compliance infraštruktúra z jadra
from Evidencia.utils_compliance import (
    to_date,
    add_calendar_interval,
    get_compliance_config,
    get_iso_pre_objekt,
)


# ==============================================================================
# 🏢 SKLADOVÉ KMEŇOVÉ SEKCIE (3 IT MODELY)
# ==============================================================================

class Hardver(ISASAuditMixin, models.Model):
    sklad = models.ForeignKey(
        'Evidencia.Sklad',
        on_delete=models.CASCADE,
        related_name="hardver_polozky",
        verbose_name="🏢 Sklad",
        help_text="🏢 Priradenie k materskému skladu pre evidenciu fyzického IT hardvéru, serverov a infraštruktúry.",
    )

    class Meta:
        db_table = 'software_hardver'
        verbose_name = "🖥️ Skladová sekcia - Hardvér a infraštruktúra"
        verbose_name_plural = "🖥️ Skladové sekcie - Hardvér a infraštruktúra"

    def __str__(self):
        nazov_skladu = getattr(self.sklad, 'nazov', str(self.sklad)) if self.sklad_id else "Bez skladu"
        return f"({nazov_skladu})"


class Licencia(ISASAuditMixin, models.Model):
    sklad = models.ForeignKey(
        'Evidencia.Sklad',
        on_delete=models.CASCADE,
        related_name="licencie_polozky",
        verbose_name="🏢 Sklad",
        help_text="🏢 Skladová sekcia pre evidenciu softvérových licencií, SSL certifikátov, domén a SaaS predplatných.",
    )

    class Meta:
        db_table = 'software_licencia'
        verbose_name = "🔑 Skladová sekcia - Licencie a predplatné"
        verbose_name_plural = "🔑 Skladové sekcie - Licencie a predplatné"

    def __str__(self):
        nazov_skladu = getattr(self.sklad, 'nazov', str(self.sklad)) if self.sklad_id else "Bez skladu"
        return f"({nazov_skladu})"


class SlaSluzba(ISASAuditMixin, models.Model):
    sklad = models.ForeignKey(
        'Evidencia.Sklad',
        on_delete=models.CASCADE,
        related_name="sluzby_polozky",
        verbose_name="🏢 Sklad",
        help_text="🏢 Skladová sekcia IT podpory, SLA zmluvných balíkov a programátorských človekohodín.",
    )

    class Meta:
        db_table = 'software_slasluzba'
        verbose_name = "🤝 Skladová sekcia - SLA podpora a inžinierske služby"
        verbose_name_plural = "🤝 Skladové sekcie - SLA podpora a inžinierske služby"

    def __str__(self):
        nazov_skladu = getattr(self.sklad, 'nazov', str(self.sklad)) if self.sklad_id else "Bez skladu"
        return f"({nazov_skladu})"


# ==============================================================================
# 🖥️ SEKCIA 1: HARDVÉR A VÝPOČTOVÁ INFRAŠTRUKTÚRA (5 MODELOV)
# ==============================================================================

class HardverRackServer(ISAS_ZakladSkladu):
    hardver = models.ForeignKey(
        "Hardver",
        on_delete=models.CASCADE,
        related_name="rack_servery",
        verbose_name="🖥️ Kmeňový sklad hardvéru",
        help_text="🔗 Prepojenie na skladovú kategóriu hardvéru.",
    )

    class Meta:
        db_table = 'software_hardverrackserver'
        verbose_name = "🖥️ Rackový server (Fyzický server / Bare-metal)"
        verbose_name_plural = "🖥️ Rackové servery (Fyzické servery / Bare-metal)"


class HardverCloudVPS(ISAS_ZakladSkladu):
    hardver = models.ForeignKey(
        "Hardver",
        on_delete=models.CASCADE,
        related_name="cloud_vps",
        verbose_name="🖥️ Kmeňový sklad hardvéru",
        help_text="🔗 Prepojenie na skladovú kategóriu virtuálnych a cloudových serverov.",
    )

    class Meta:
        db_table = 'software_hardvercloudvps'
        verbose_name = "☁️ Virtuálny server (Cloud VPS / VM inštancia)"
        verbose_name_plural = "☁️ Virtuálne servery (Cloud VPS / VM inštancie)"


class HardverDiskovePole(ISAS_ZakladSkladu):
    hardver = models.ForeignKey(
        "Hardver",
        on_delete=models.CASCADE,
        related_name="diskove_polia",
        verbose_name="🖥️ Kmeňový sklad hardvéru",
        help_text="🔗 Skladová evidencia dátových polí a centrálnych diskových úložísk.",
    )

    class Meta:
        db_table = 'software_hardverdiskovepole'
        verbose_name = "💾 Diskové pole (SAN / NAS / JBOD úložisko)"
        verbose_name_plural = "💾 Diskové polia (SAN / NAS / JBOD úložiská)"


class HardverSietovyPrvok(ISAS_ZakladSkladu):
    hardver = models.ForeignKey(
        "Hardver",
        on_delete=models.CASCADE,
        related_name="sietove_prvky",
        verbose_name="🖥️ Kmeňový sklad hardvéru",
        help_text="🔗 Prepojenie na aktívne sieťové prvky (switche, routery, optické transceivery).",
    )

    class Meta:
        db_table = 'software_hardversietovyprvok'
        verbose_name = "🌐 Sieťový prvok (Switch, Router, Optický prevodník)"
        verbose_name_plural = "🌐 Sieťové prvky (Switche, Routery, Optické prevodníky)"


class HardverFirewallWAF(ISAS_ZakladSkladu):
    hardver = models.ForeignKey(
        "Hardver",
        on_delete=models.CASCADE,
        related_name="firewally_waf",
        verbose_name="🖥️ Kmeňový sklad hardvéru",
        help_text="🔗 Bezpečnostné zariadenia na ochranu perimetra siete a webových aplikácií.",
    )

    class Meta:
        db_table = 'software_hardverfirewallwaf'
        verbose_name = "🛡️ Hardvérový firewall a WAF bezpečnostná brána"
        verbose_name_plural = "🛡️ Hardvérové firewally a WAF bezpečnostné brány"


# ==============================================================================
# 🔑 SEKCIA 2: LICENCIE A DIGITÁLNE PREDPLATNÉ (6 MODELOV)
# ==============================================================================

class LicenciaOperacnySystem(ISAS_DielSTypom):
    licencia = models.ForeignKey(
        "Licencia",
        on_delete=models.CASCADE,
        related_name="os_licencie",
        verbose_name="🔑 Kmeňový sklad licencií",
        help_text="🔗 Väzba na skladovú kategóriu licencií operačných systémov.",
    )
    merna_jednotka = models.CharField(
        "⚖️ Jednotka",
        max_length=10,
        choices=MERNE_JEDNOTKY_CHOICES,
        default='ks',
        help_text="⚖️ Merná jednotka pre licencovanie (ks, core, socket).",
    )

    class Meta:
        db_table = 'software_licenciaoperacnysystem'
        verbose_name = "🪟 Licencia operačného systému (Per-core / Per-server)"
        verbose_name_plural = "🪟 Licencie operačných systémov (Per-core / Per-server)"


class LicenciaDatabaza(ISAS_DielSTypom):
    licencia = models.ForeignKey(
        "Licencia",
        on_delete=models.CASCADE,
        related_name="db_licencie",
        verbose_name="🔑 Kmeňový sklad licencií",
        help_text="🔗 Väzba na licencovanie databázových strojov a serverov.",
    )
    merna_jednotka = models.CharField(
        "⚖️ Jednotka",
        max_length=10,
        choices=MERNE_JEDNOTKY_CHOICES,
        default='ks',
    )

    class Meta:
        db_table = 'software_licenciadatabaza'
        verbose_name = "🗄️ Databázová licencia (Enterprise / Per-core)"
        verbose_name_plural = "🗄️ Databázové licencie (Enterprise / Per-core)"


class LicenciaSaaSPredplatne(ISAS_DielSTypom):
    licencia = models.ForeignKey(
        "Licencia",
        on_delete=models.CASCADE,
        related_name="saas_predplatne",
        verbose_name="🔑 Kmeňový sklad licencií",
        help_text="🔗 Väzba na predplatné cloudových služieb a používateľských účtov.",
    )
    merna_jednotka = models.CharField(
        "⚖️ Jednotka",
        max_length=10,
        choices=MERNE_JEDNOTKY_CHOICES,
        default='ks',
        help_text="⚖️ Merná jednotka účtovania (používateľ, mesiac, token).",
    )

    class Meta:
        db_table = 'software_licenciasaaspredplatne'
        verbose_name = "📱 SaaS softvérové predplatné (Per-user / Per-mesiac)"
        verbose_name_plural = "📱 SaaS softvérové predplatné"


class LicenciaSSLCertifikat(ISAS_DielSTypom):
    licencia = models.ForeignKey(
        "Licencia",
        on_delete=models.CASCADE,
        related_name="ssl_certifikaty",
        verbose_name="🔑 Kmeňový sklad licencií",
        help_text="🔗 Komerčné TLS/SSL certifikáty s ročnou či viacročnou platnosťou.",
    )
    merna_jednotka = models.CharField(
        "⚖️ Jednotka",
        max_length=10,
        choices=MERNE_JEDNOTKY_CHOICES,
        default='ks',
    )

    class Meta:
        db_table = 'software_licenciasslcertifikat'
        verbose_name = "🔒 SSL / TLS certifikát (Komerčný / EV / Wildcard)"
        verbose_name_plural = "🔒 SSL / TLS certifikáty"


class LicenciaDomenaDNS(ISAS_DielSTypom):
    licencia = models.ForeignKey(
        "Licencia",
        on_delete=models.CASCADE,
        related_name="domeny_dns",
        verbose_name="🔑 Kmeňový sklad licencií",
        help_text="🔗 Registrácie a prolongácie internetových domén.",
    )
    merna_jednotka = models.CharField(
        "⚖️ Jednotka",
        max_length=10,
        choices=MERNE_JEDNOTKY_CHOICES,
        default='rok',
    )

    class Meta:
        db_table = 'software_licenciadomenadns'
        verbose_name = "🌐 Doménová registrácia a DNS zóna"
        verbose_name_plural = "🌐 Doménové registrácie a DNS zóny"


class LicenciaOstatne(ISAS_DielSTypom):
    licencia = models.ForeignKey(
        "Licencia",
        on_delete=models.CASCADE,
        related_name="ostatne_licencie",
        verbose_name="🔑 Kmeňový sklad licencií",
        help_text="🔗 Všeobecné a doplnkové softvérové licencie (DEFAULT_STOCK_MODEL).",
    )
    merna_jednotka = models.CharField(
        "⚖️ Jednotka",
        max_length=10,
        choices=MERNE_JEDNOTKY_CHOICES,
        default='ks',
    )

    class Meta:
        db_table = 'software_licenciaostatne'
        verbose_name = "🧩 Ostatná softvérová licencia / predplatné"
        verbose_name_plural = "🧩 Ostatné softvérové licencie a predplatné"


# ==============================================================================
# 🤝 SEKCIA 3: SLA PODPORA A INŽINIERSKE SLUŽBY (2 MODELY)
# ==============================================================================

class SlaIncidentPauzal(ISAS_DielSTypom):
    sluzba = models.ForeignKey(
        "SlaSluzba",
        on_delete=models.CASCADE,
        related_name="sla_pauzaly",
        verbose_name="🤝 Kmeňový sklad služieb",
        help_text="🔗 Mesačný balík pohotovosti, garantovaný reakčný čas a incidentový servis.",
    )
    merna_jednotka = models.CharField(
        "⚖️ Jednotka",
        max_length=10,
        choices=MERNE_JEDNOTKY_CHOICES,
        default='mesiac',
    )

    class Meta:
        db_table = 'software_slaincidentpauzal'
        verbose_name = "⏱️ SLA paušál a incidentový servis"
        verbose_name_plural = "⏱️ SLA paušály a incidentové servisy"


class SlaClovekohodinyVyvoj(ISAS_DielSTypom):
    sluzba = models.ForeignKey(
        "SlaSluzba",
        on_delete=models.CASCADE,
        related_name="vyvoj_clovekohodiny",
        verbose_name="🤝 Kmeňový sklad služieb",
        help_text="🔗 Seniorné vývojové, architektonické a DevOps človekohodiny (MD / h).",
    )
    merna_jednotka = models.CharField(
        "⚖️ Jednotka",
        max_length=10,
        choices=MERNE_JEDNOTKY_CHOICES,
        default='hod',
    )

    class Meta:
        db_table = 'software_slaclovekohodinyvyvoj'
        verbose_name = "👨‍💻 Inžinierske človekohodiny a vývoj (MD / h)"
        verbose_name_plural = "👨‍💻 Inžinierske človekohodiny a vývoj"


# ==============================================================================
# 📊 IOT GAUGE LIMITS MIXIN PRE IT INFRAŠTRUKTÚRU
# ==============================================================================

class SoftwareGaugeLimitsMixin:
    """📊 ISAS IT GAUGES: Poskytuje presné limity pre IoT budíky podľa parametrov technológie."""

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

        # 1. Vyťaženie CPU (%)
        cpu = _val('max_vykon_cpu_percent')
        if cpu is not None:
            limity['cpu'] = cpu

        # 2. Využitie pamäte RAM (%)
        ram = _val('max_vyuzitie_ram_percent')
        if ram is not None:
            limity['ram'] = ram

        # 3. Sieťová odozva a latencia (ms)
        lat = _val('max_latencia_ms')
        if lat is not None:
            limity['latency'] = lat

        # 4. Miera chybovosti / HTTP Error Rate (%)
        err = _val('max_error_rate_percent')
        if err is not None:
            limity['error_rate'] = err

        return limity


# ==============================================================================
# ⚙️ TECHNICKÉ PARAMETRE ZARIADENÍ (3 IT MODELY)
# ==============================================================================

class ParametreServer(SoftwareGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP_SERVERA = [
        ('vps', '☁️ Cloud VPS'),
        ('baremetal', '🖥️ Dedikovaný bare-metal'),
        ('vm', '🏢 On-Premise VM'),
        ('node', '🌐 Sieťový HW uzol'),
        ('k8s', '☸️ Kubernetes Node'),
    ]

    zariadenie = models.OneToOneField(
        'Evidencia.ServisovaneZariadenie',
        on_delete=models.CASCADE,
        related_name="parametre_server",
        verbose_name="⚙️ Zariadenie",
        help_text="⚙️ Servisované zariadenie, ku ktorému patria tieto technické parametre servera.",
    )
    typ_servera = models.CharField(
        "🖥️ Typ servera",
        max_length=50,
        choices=TYP_SERVERA,
        default='vps',
        help_text="Architektúra a prostredie serverovej infraštruktúry.",
    )
    hostname = models.CharField(
        "🌐 Hostname (FQDN)",
        max_length=255,
        blank=True,
        help_text="Plne kvalifikované doménové meno (FQDN), napr. app01.cloud.isas.sk.",
    )
    ip_adresa = models.CharField(
        "🔌 IP adresa",
        max_length=50,
        blank=True,
        help_text="Primárna IPv4 alebo IPv6 adresa servera pre vzdialenú správu a telemetriu.",
    )
    operacny_system = models.CharField(
        "🪟 / 🐧 Operačný systém",
        max_length=100,
        blank=True,
        help_text="Napr. Debian 12, Ubuntu 24.04 LTS, AlmaLinux 9, Windows Server 2022.",
    )
    pocet_jadier_cpu = models.PositiveIntegerField(
        "🧠 Počet jadier CPU (vCPU)",
        null=True,
        blank=True,
        help_text="Počet fyzických jadier alebo virtuálnych vCPU pridelených serveru.",
    )
    ram_gb = models.PositiveIntegerField(
        "💾 Operačná pamäť RAM (GB)",
        null=True,
        blank=True,
        help_text="Kapacita operačnej pamäte RAM v gigabajtoch.",
    )
    disk_kapacita_gb = models.PositiveIntegerField(
        "💿 Úložisko (GB)",
        null=True,
        blank=True,
        help_text="Celková kapacita primárneho diskového priestoru v gigabajtoch (SSD / NVMe).",
    )
    datum_posledneho_patchu = models.DateField(
        "🛡️ Dátum posledného OS patchingu",
        null=True,
        blank=True,
        help_text="Dátum poslednej inštalácie bezpečnostných aktualizácií jadra a balíkov.",
    )
    datum_dalsej_udrzby = models.DateField(
        "🛠️ Dátum plánovanej údržby / údržbového okna",
        null=True,
        blank=True,
        help_text="Plánované servisné okno pre profylaktiku a aktualizáciu infraštruktúry.",
    )
    ma_aktivny_monitoring = models.BooleanField(
        "📡 Aktívny monitoring (Prometheus / Zabbix / IoT)",
        default=True,
        help_text="Indikátor, či je server nepretržite monitorovaný automatizovaným dohľadom.",
    )
    max_vykon_cpu_percent = models.DecimalField(
        "📊 Max. povolené vyťaženie CPU (%)",
        max_digits=5,
        decimal_places=2,
        default=Decimal('85.00'),
        help_text="Prahová hodnota vyťaženia CPU pre IoT varovanie pred saturáciou.",
    )
    max_vyuzitie_ram_percent = models.DecimalField(
        "📊 Max. povolené využitie RAM (%)",
        max_digits=5,
        decimal_places=2,
        default=Decimal('90.00'),
        help_text="Prahová hodnota zaplnenia RAM pred spustením OOM killer mechanizmu.",
    )
    max_latencia_ms = models.PositiveIntegerField(
        "⏱️ Max. prípustná latencia (ms)",
        default=150,
        help_text="Maximálna akceptovateľná sieťová odozva (ms).",
    )
    max_error_rate_percent = models.DecimalField(
        "📉 Max. chybovosť (Error Rate %)",
        max_digits=5,
        decimal_places=2,
        default=Decimal('1.00'),
        help_text="Maximálna povolená miera chybových paketov a odpovedí v percentách.",
    )

    class Meta:
        db_table = 'software_parametreserver'
        verbose_name = "🖥️ Parametre - Server"
        verbose_name_plural = "🖥️ Parametre - Servery"

    def save(self, *args, **kwargs):
        cfg = get_compliance_config(iso=get_iso_pre_objekt(self))
        zmenene_polia = set()
        d_posl = to_date(self.datum_posledneho_patchu)
        d_dalsia = to_date(self.datum_dalsej_udrzby)
        if d_posl and (not d_dalsia or d_dalsia <= d_posl):
            try:
                rotacia_dni = max(1, int(cfg.get('rotacia_pristupov_dni') or 90))
            except (TypeError, ValueError):
                rotacia_dni = 90
            self.datum_dalsej_udrzby = d_posl + timedelta(days=rotacia_dni)
            zmenene_polia.add('datum_dalsej_udrzby')
        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


class ParametreAplikacia(SoftwareGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP_APLIKACIE = [
        ('web', '🌐 Webová aplikácia'),
        ('api', '🔌 REST API Gateway'),
        ('saas', '☁️ SaaS platforma'),
        ('mobile_backend', '📱 Mobilný backend'),
        ('microservice', '🧩 Mikroslužba'),
    ]

    zariadenie = models.OneToOneField(
        'Evidencia.ServisovaneZariadenie',
        on_delete=models.CASCADE,
        related_name="parametre_aplikacia",
        verbose_name="⚙️ Zariadenie",
        help_text="⚙️ Servisované zariadenie / softvérová služba, ku ktorej patria tieto parametre.",
    )
    typ_aplikacie = models.CharField(
        "📱 Typ aplikácie",
        max_length=50,
        choices=TYP_APLIKACIE,
        default='web',
        help_text="Kategorizácia aplikačnej služby.",
    )
    runtime_prostredie = models.CharField(
        "⚙️ Runtime / Technologický stack",
        max_length=100,
        blank=True,
        help_text="Napr. Python 3.12 / Django, Node.js 20 / NestJS, Java 21 / Spring Boot, Go 1.22, Docker.",
    )
    url_adresa = models.URLField(
        "🔗 URL adresa aplikácie",
        blank=True,
        help_text="Verejný alebo interný prístupový endpoint aplikácie.",
    )
    produkcna_verzia = models.CharField(
        "🏷️ Aktuálna produkčná verzia",
        max_length=50,
        blank=True,
        help_text="Napr. v2.4.1 (Git commit hash alebo release tag).",
    )
    datum_posledneho_nasadenia = models.DateField(
        "🚀 Dátum posledného nasadenia (Deploy)",
        null=True,
        blank=True,
        help_text="Dátum úspešného nasadenia aktuálnej produkčnej verzie.",
    )
    ssl_certifikat_vyprsanie = models.DateField(
        "🔒 Expirácia SSL certifikátu",
        null=True,
        blank=True,
        help_text="Dátum vypršania platnosti produkčného TLS/SSL certifikátu.",
    )
    ssl_auto_obnova = models.BooleanField(
        "🔄 Automatická obnova SSL (Let's Encrypt / Certbot)",
        default=False,
        help_text="Začiarknite, ak certifikát obnovuje automatický proces (Certbot / ACME).",
    )
    domena_vyprsanie = models.DateField(
        "🌐 Expirácia registrácie domény",
        null=True,
        blank=True,
        help_text="Dátum vypršania platnosti hlavnej domény.",
    )
    datum_posledneho_pentestu = models.DateField(
        "🛡️ Dátum posledného penetračného testu",
        null=True,
        blank=True,
        help_text="Dátum vykonania bezpečnostného auditu alebo penetračného testu.",
    )
    datum_dalsieho_pentestu = models.DateField(
        "🛡️ Lehota ďalšieho penetračného testu",
        null=True,
        blank=True,
        help_text="Lehota ďalšieho penetračného testu (dopočíta sa z bezpečnostných noriem).",
    )
    max_latencia_ms = models.PositiveIntegerField(
        "⏱️ Max. prípustná latencia (ms)",
        default=300,
        help_text="Prahová hodnota odozvy HTTP endpointu pre vystavenie varovania.",
    )
    max_error_rate_percent = models.DecimalField(
        "📉 Max. chybovosť HTTP 5xx (%)",
        max_digits=5,
        decimal_places=2,
        default=Decimal('1.00'),
        help_text="Maximálna akceptovateľná chybovosť požiadaviek (5xx odpovede).",
    )

    class Meta:
        db_table = 'software_parametreaplikacia'
        verbose_name = "📱 Parametre - Aplikácia"
        verbose_name_plural = "📱 Parametre - Aplikácie"

    def save(self, *args, **kwargs):
        cfg = get_compliance_config(iso=get_iso_pre_objekt(self))
        zmenene_polia = set()
        d_posl = to_date(self.datum_posledneho_pentestu)
        d_dalsia = to_date(self.datum_dalsieho_pentestu)
        if d_posl and (not d_dalsia or d_dalsia <= d_posl):
            roky = cfg.get('penetracny_test_roky', 1)
            self.datum_dalsieho_pentestu = add_calendar_interval(d_posl, years=roky)
            zmenene_polia.add('datum_dalsieho_pentestu')
        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


class ParametreDatabaza(SoftwareGaugeLimitsMixin, ISASAuditMixin, models.Model):
    TYP_DATABAZY = [
        ('postgresql', '🐘 PostgreSQL'),
        ('mariadb', '🐬 MySQL / MariaDB'),
        ('redis', '⚡ Redis In-Memory Cache'),
        ('mongodb', '🍃 MongoDB NoSQL'),
        ('s3', '🪣 S3 Object Storage'),
    ]
    REZIM_HA = [
        ('standalone', '🖥️ Standalone (Jediný uzol)'),
        ('master_replica', '👥 Master-Replica (Čítacie repliky)'),
        ('ha_cluster', '🛡️ HA Cluster (Automatický Failover)'),
    ]

    zariadenie = models.OneToOneField(
        'Evidencia.ServisovaneZariadenie',
        on_delete=models.CASCADE,
        related_name="parametre_databaza",
        verbose_name="⚙️ Zariadenie",
        help_text="⚙️ Servisované zariadenie / databázový cluster, ku ktorému patria tieto parametre.",
    )
    typ_databazy = models.CharField(
        "🗄️ Typ databázového enginu",
        max_length=50,
        choices=TYP_DATABAZY,
        default='postgresql',
        help_text="Typ a architektúra dátového úložiska.",
    )
    engine_verzia = models.CharField(
        "🏷️ Verzia databázového enginu",
        max_length=50,
        blank=True,
        help_text="Napr. PostgreSQL 16.2, MariaDB 11.3, Redis 7.2.",
    )
    velkost_dat_gb = models.DecimalField(
        "💾 Veľkosť dátovej bázy (GB)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Aktuálna veľkosť dátových súborov a indexov v GB.",
    )
    rezim_vysokej_dostupnosti = models.CharField(
        "🛡️ Režim vysokej dostupnosti (HA)",
        max_length=50,
        choices=REZIM_HA,
        default='standalone',
        help_text="Konfigurácia replikácie a odolnosti voči výpadkom.",
    )
    datum_poslednej_zalohy = models.DateField(
        "💾 Dátum poslednej overenej zálohy",
        null=True,
        blank=True,
        help_text="Dátum vykonania posledného úspešného full/snapshot zálohovania.",
    )
    datum_posledneho_dr_testu = models.DateField(
        "🧪 Dátum posledného DR testu (Disaster Recovery)",
        null=True,
        blank=True,
        help_text="Dátum vykonania overenia obnovy dát zo zálohy (Disaster Recovery cvičenie).",
    )
    datum_dalsieho_dr_testu = models.DateField(
        "🧪 Lehota ďalšieho DR testu",
        null=True,
        blank=True,
        help_text="Zákonná alebo interná lehota ďalšieho DR cvičenia obnovy.",
    )
    rpo_hodiny = models.PositiveIntegerField(
        "⏱️ RPO - Recovery Point Objective (hodiny)",
        default=24,
        help_text="Maximálna tolerovateľná strata dát vyjadrená v hodinách.",
    )
    rto_hodiny = models.PositiveIntegerField(
        "⏱️ RTO - Recovery Time Objective (hodiny)",
        default=4,
        help_text="Maximálny prípustný čas na obnovu prevádzky databázy v hodinách.",
    )
    max_vyuzitie_ram_percent = models.DecimalField(
        "📊 Max. vyťaženie pamäte Buffer/Pool (%)",
        max_digits=5,
        decimal_places=2,
        default=Decimal('90.00'),
        help_text="Limit využitia pamäte buffer poolu pre DB.",
    )
    max_latencia_ms = models.PositiveIntegerField(
        "⏱️ Max. latencia dopytov (Query Latency ms)",
        default=100,
        help_text="Limit latencie pre pomalé SQL dopyty.",
    )

    class Meta:
        db_table = 'software_parametredatabaza'
        verbose_name = "🗄️ Parametre - Databáza"
        verbose_name_plural = "🗄️ Parametre - Databázy"

    def save(self, *args, **kwargs):
        cfg = get_compliance_config(iso=get_iso_pre_objekt(self))
        zmenene_polia = set()
        d_posl = to_date(self.datum_posledneho_dr_testu)
        d_dalsia = to_date(self.datum_dalsieho_dr_testu)
        if d_posl and (not d_dalsia or d_dalsia <= d_posl):
            mesiace = cfg.get('zalohovanie_dr_test_mesiace', 6)
            self.datum_dalsieho_dr_testu = add_calendar_interval(d_posl, months=mesiace)
            zmenene_polia.add('datum_dalsieho_dr_testu')
        if kwargs.get('update_fields') is not None and zmenene_polia:
            kwargs['update_fields'] = set(kwargs['update_fields']) | zmenene_polia
        super().save(*args, **kwargs)


# ==============================================================================
# ⚖️ CENTRÁLNA KONFIGURÁCIA A NÁRODNÉ IT LEHOTY (NIS2, ISO, DR)
# ==============================================================================

class SoftwareNastavenia(ISASAuditMixin, models.Model):
    """
    🏛️ Singleton model pre centrálnu konfiguráciu odvetvových IT lehôt,
    kybernetickej bezpečnosti (NIS2, ISO 27001), SSL certifikátov,
    domén a Disaster Recovery testovania.
    """
    lehota_ssl_upozornenie_dni = models.PositiveIntegerField(
        "🔒 Lehota: Upozornenie na SSL certifikát (dni)",
        default=30,
        help_text="Počet dní pred vypršaním SSL/TLS certifikátu pre vyvolanie varovného alarmu (štandardne 30 dní).",
    )
    lehota_domena_upozornenie_dni = models.PositiveIntegerField(
        "🌐 Lehota: Upozornenie na expiráciu domény (dni)",
        default=60,
        help_text="Počet dní pred vypršaním domény pre zaslanie notifikácie na obnovu registrácie (štandardne 60 dní).",
    )
    lehota_zalohovanie_dr_test_mesiace = models.PositiveIntegerField(
        "💾 Lehota: Disaster Recovery test obnovy dát (mesiace)",
        default=6,
        help_text="Odporúčaná periodicita DR cvičenia a testu obnovy dát zo zálohy (štandardne 6 mesiacov).",
    )
    lehota_audit_nis2_roky = models.PositiveIntegerField(
        "🛡️ Lehota: Bezpečnostný audit NIS2 (roky)",
        default=2,
        help_text="Zákonná lehota periodického bezpečnostného auditu podľa Smernice NIS2 (štandardne 2 roky).",
    )
    lehota_audit_iso27001_roky = models.PositiveIntegerField(
        "📜 Lehota: Dozorný audit ISO/IEC 27001 (roky)",
        default=1,
        help_text="Periodicita recertifikačného alebo dozorného auditu riadenia bezpečnosti informácií (štandardne 1 rok).",
    )
    lehota_penetracny_test_roky = models.PositiveIntegerField(
        "🎯 Lehota: Penetračný test systémov (roky)",
        default=1,
        help_text="Odporúčaná periodicita komplexného penetračného testu infraštruktúry a webových aplikácií (štandardne 1 rok).",
    )
    lehota_rotacia_pristupov_dni = models.PositiveIntegerField(
        "🔑 Lehota: Rotácia prístupových kľúčov a údržba (dni)",
        default=90,
        help_text="Bezpečnostný interval na rotáciu SSH kľúčov, API tokenov a servisných hesiel (štandardne 90 dní).",
    )

    class Meta:
        db_table = 'software_nastavenia'
        verbose_name = "⚙️ Nastavenia odvetvia (IT, Cloud a Softvér)"
        verbose_name_plural = "⚙️ Nastavenia odvetvia (IT, Cloud a Softvér)"

    def clean(self):
        super().clean()
        if self.pk is None and SoftwareNastavenia.objects.exists():
            raise ValidationError("⚠️ Kritická chyba: Môže existovať iba jedno globálne nastavenie odvetvia.")
        polia_na_kontrolu = [
            ('lehota_ssl_upozornenie_dni', 'Upozornenie na SSL certifikát'),
            ('lehota_domena_upozornenie_dni', 'Upozornenie na expiráciu domény'),
            ('lehota_zalohovanie_dr_test_mesiace', 'DR test obnovy dát'),
            ('lehota_audit_nis2_roky', 'Audit NIS2'),
            ('lehota_audit_iso27001_roky', 'Audit ISO 27001'),
            ('lehota_penetracny_test_roky', 'Penetračný test'),
            ('lehota_rotacia_pristupov_dni', 'Rotácia prístupov a kľúčov'),
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
                    for obj in SoftwareStatLokalizacia.objects.select_related('stat').all():
                        if obj.stat and obj.stat.iso_kod:
                            cache.delete(f'isas_compliance_config_{obj.stat.iso_kod}')
                except Exception:
                    pass

        transaction.on_commit(_clear_cache)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("⚠️ Kritická chyba: Zmazanie odvetvových nastavení je na úrovni jadra prísne zakázané!")

    def __str__(self):
        return "⚙️ Odvetvové lehoty a bezpečnosť (IT a Softvér)"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SoftwareStatLokalizacia(ISASAuditMixin, models.Model):
    """
    🗺️ Odvetvová národná matica: Definuje špecifické zákonné lehoty IT a kybernetickej
    bezpečnosti v jednotlivých štátoch (napr. transpozícia NIS2 a národné predpisy).
    """
    stat = models.OneToOneField(
        'ISASaplikacia.StatLokalizacia',
        on_delete=models.CASCADE,
        related_name='software_lehoty',
        verbose_name="🗺️ Štát",
        help_text="Výber štátu, pre ktorý platia tieto národné odvetvové lehoty.",
    )
    lehota_audit_nis2_roky = models.PositiveIntegerField(
        "🛡️ Audit NIS2 v danom štáte (roky)",
        null=True,
        blank=True,
        help_text="Zákonná národná lehota bezpečnostného auditu podľa implementácie smernice NIS2 v danej krajine.",
    )
    lehota_zalohovanie_dr_test_mesiace = models.PositiveIntegerField(
        "💾 Periodicita DR testu obnovy (mesiace)",
        null=True,
        blank=True,
        help_text="Národné odporúčanie alebo regulačná požiadavka pre testovanie obnovy záloh (Disaster Recovery).",
    )
    lehota_penetracny_test_roky = models.PositiveIntegerField(
        "🎯 Penetračný test infraštruktúry (roky)",
        null=True,
        blank=True,
        help_text="Národná lehota pre povinné penetračné testovanie kritických prvkov infraštruktúry.",
    )
    lehota_rotacia_pristupov_dni = models.PositiveIntegerField(
        "🔑 Rotácia prístupov a oprávnení (dni)",
        null=True,
        blank=True,
        help_text="Národný štandard pre rotáciu poverení v regulovaných IT prostrediach.",
    )

    class Meta:
        db_table = 'software_statlokalizacia'
        verbose_name = "🗺️ Národné lehoty IT a kyberbezpečnosti"
        verbose_name_plural = "🗺️ Národné lehoty IT a kyberbezpečnosti"

    def clean(self):
        super().clean()
        polia = [
            ('lehota_audit_nis2_roky', 'Audit NIS2'),
            ('lehota_zalohovanie_dr_test_mesiace', 'DR test záloh'),
            ('lehota_penetracny_test_roky', 'Penetračný test'),
            ('lehota_rotacia_pristupov_dni', 'Rotácia prístupov'),
        ]
        for field_name, label in polia:
            val = getattr(self, field_name, None)
            if val is not None and val < 1:
                raise ValidationError({field_name: f"⚠️ {label} musí byť minimálne 1."})

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
                    raise PermissionDenied(
                        f"🛑 Štát '{self.stat.iso_kod}' je sídlom materskej firmy. Národné odvetvové lehoty sídla je zakázané vymazať!"
                    )
        except PermissionDenied:
            raise
        except Exception:
            pass
        super().delete(*args, **kwargs)

    def __str__(self):
        iso = getattr(self.stat, 'iso_kod', '??') if self.stat_id else '??'
        return f"⚖️ IT Lehoty - {iso}"