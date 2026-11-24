import os
import json
import hashlib
import hmac
import re
import secrets
import uuid
from io import BytesIO

from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import slugify

from colorfield.fields import ColorField
from PIL import Image, ImageOps

from ISASaplikacia.models import ISASAuditMixin, ISASAuditQuerySet
from ISASaplikacia.utils_helper import validate_safe_webhook_url
from .utils_helper import sanitizuj_verejne_html

class WebNastaveniaQuerySet(ISASAuditQuerySet):
    def delete(self):
        raise PermissionDenied("⚠️ Kritická chyba: Hromadné zmazanie systémových nastavení je zakázané!")

class WebNastavenia(ISASAuditMixin, models.Model):
    objects = WebNastaveniaQuerySet.as_manager()  # type: ignore[misc,assignment]
    domena = models.CharField(
        "🌐 Verejná doména", 
        max_length=255, 
        help_text="Napr. indsas.com (bez https:// a www). Na túto doménu bude reagovať náš Middleware."
    )
    titulok_webu = models.CharField("📝 Titulok webu (SEO)", max_length=255, default="Profesionálny servis a údržba")
    hlavna_farba = ColorField("🎨 Hlavná farba (Brand)", default="#0f172a", help_text="Hlavná farba tlačidiel a hlavičky.")
    logo = models.ImageField("🖼️ Logo na web", upload_to="web_loga/", blank=True, null=True)
    
    # SEO a Meta dáta
    meta_popis = models.TextField("🔍 Meta popis (SEO)", blank=True, help_text="Krátky popis pre Google vyhľadávač (max 160 znakov).")

    # --- 🌐 WEB BUILDER INTEGRÁCIA ---
    zobrazit_live_sla = models.BooleanField(
        "🚦 Zobraziť Live SLA (Vyťaženosť)", 
        default=False, 
        help_text="🚦 Zobrazí na webe dynamický banner o aktuálnej vyťaženosti technikov (ťahané z počtu otvorených úloh)."
    )
    sla_text_optimalna = models.CharField(
        "🟢 Live SLA: Text pri voľnej kapacite", 
        max_length=255, blank=True, null=True, 
        help_text="Vlastný text pre voľné kapacity. Ak zostane prázdne, použije sa neutrálny systémový text."
    )
    sla_text_zvysena = models.CharField(
        "🟡 Live SLA: Text pri bežnej vyťaženosti", 
        max_length=255, blank=True, null=True, 
        help_text="Vlastný text pre štandardnú vyťaženosť. Ak zostane prázdne, použije sa neutrálny systémový text."
    )
    sla_text_kriticka = models.CharField(
        "🔴 Live SLA: Text pri vysokej záťaži", 
        max_length=255, blank=True, null=True, 
        help_text="Vlastný text pri vyťažených kapacitách. Ak zostane prázdne, použije sa neutrálny systémový text."
    )

    class Meta:
        verbose_name = "⚙️ Nastavenie verejného webu"
        verbose_name_plural = "⚙️ Nastavenia verejného webu"

    def __str__(self):
        return f"Nastavenia webu ({self.domena or 'Nenastavená'})"

    def clean(self):
        super().clean()
        # 🛡️ BLBUVZDORNOSŤ: Očistíme doménu od whitespace, protokolov, www a ciest
        if self.domena:
            cista_domena = self.domena.strip().lower()
            cista_domena = re.sub(r'^https?://', '', cista_domena)
            cista_domena = re.sub(r'^www\.', '', cista_domena)
            # Ostránime prípadnú cestu za doménou, query parametre, kotvy a koncové lomítka
            self.domena = cista_domena.split('/')[0].split('?')[0].split('#')[0].strip()
            
        if not self.domena:
            raise ValidationError({"domena": "Zadajte platnú doménu (napr. isascore.com)."})

        # Validácia limitu inštancií na úrovni formulára (predchádza 500 Internal Server Error v Admin rozhraní)
        if self.pk is None:
            existujuce_pks = set(WebNastavenia.objects.filter(pk__in=[1, 2]).values_list('pk', flat=True))
            if {1, 2}.issubset(existujuce_pks):
                raise ValidationError("⚠️ Kritická chyba: Môže existovať iba jedna LIVE (ID 1) a jedna DRAFT (ID 2) konfigurácia.")
        elif self.pk not in [1, 2]:
            raise ValidationError("⚠️ Kritická chyba: Neplatný primárny kľúč. Povolené sú výhradne ID 1 (LIVE) a ID 2 (DRAFT).")

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.pk is None:
            existujuce_pks = set(WebNastavenia.objects.filter(pk__in=[1, 2]).values_list('pk', flat=True))
            if 1 not in existujuce_pks:
                self.pk = 1
            elif 2 not in existujuce_pks:
                self.pk = 2
            else:
                raise ValidationError("⚠️ Kritická chyba: Môže existovať iba jedna LIVE (pk=1) a jedna DRAFT (pk=2) konfigurácia.")
        elif self.pk not in [1, 2]:
            raise ValidationError("⚠️ Kritická chyba: Neplatný primárny kľúč. Povolené sú výhradne ID 1 (LIVE) a ID 2 (DRAFT).")
                
        try:
            with transaction.atomic():
                # 🛡️ ISAS STORAGE LEAK PROTECTION: Bezpečné vyhľadanie pôvodného loga len pri UPDATE operácii
                stare_logo_na_zmazanie = None
                if not self._state.adding and self.pk:
                    try:
                        stare_nastavenia = WebNastavenia.objects.get(pk=self.pk)
                        if stare_nastavenia.logo and stare_nastavenia.logo != self.logo:
                            stare_logo_na_zmazanie = stare_nastavenia.logo
                    except WebNastavenia.DoesNotExist:
                        pass

                super().save(*args, **kwargs)

                if stare_logo_na_zmazanie and (not self.logo or stare_logo_na_zmazanie.name != self.logo.name):
                    def _bezpecne_zmazat_logo(subor=stare_logo_na_zmazanie):
                        try:
                            subor.delete(save=False)
                        except Exception:
                            pass  # 🛡️ Zlyhanie IO/Storage nesmie zhodiť request po úspešnom DB commite
                    transaction.on_commit(_bezpecne_zmazat_logo)

                def _invaliduj_cache():
                    try:
                        # 🛡️ Zmazanie správneho kľúča, ktorý reálne načítava PublicWebRoutingMiddleware
                        cache.delete('isas_public_web_domain')
                        cache.delete('isas_dynamicky_web_firmy')
                        try:
                            cache.incr('web_cache_version')
                        except ValueError:
                            cache.set('web_cache_version', 1, None)
                    except Exception:
                        pass  # 🛡️ Výpadok cache nesmie zhodiť request po úspešnom DB commite

                # Invalidácia sa vykoná VÝHRADNE po úspešnom commite do DB
                transaction.on_commit(_invaliduj_cache)
        except IntegrityError:
            raise ValidationError("⚠️ Kritická chyba: Súbežný pokus o vytvorenie nastavení.")

    def delete(self, *args, **kwargs):
        raise PermissionDenied("⚠️ Kritická chyba: Zmazanie tohto systémového záznamu je na úrovni jadra prísne zakázané!")

class WebBlok(ISASAuditMixin, models.Model):
    TYP_BLOKU = [
        ('HERO', '🚩 Hero Banner (Hlavný úvod s tlačidlom)'),
        ('ABOUT', 'ℹ️ O nás / Textový blok'),
        ('SERVICES', '🛠️ Katalóg služieb (Automaticky z ERP)'),
        ('RENTAL', '⚙️ Požičovňa zariadení (Agregované z ERP)'),
        ('PARTS', '🛒 Katalóg dielov (E-commerce Lite z ERP)'),
        ('BRANDS', '🏷️ Značky a Zariadenia (Automaticky z ERP)'),
        ('REFERENCES', '📸 Naše realizácie (Pred / Po z ERP)'),
        ('TEAM', '👥 Náš tím (Technici z ERP)'),
        ('TICKET', '🔍 Sledovanie stavu zákazky (Ticket Checker)'),
        ('CALCULATOR', '🧮 Kalkulačka ceny výjazdu'),
        ('ENGINEERING_HUB', '🔬 Inžinierske & Diagnostické Centrum (B2B Nástroje)'),
        ('CONTACT', '📞 Kontaktný a Dopytový formulár'),
        ('IOT_DEMO', '📡 Live IoT Telemetria (Demo)'),
        ('CAREER', '🤝 Kariéra a Nábor (Voľné pracovné pozície)'),
        ('SLA_CALCULATOR', '🛡️ SLA Kalkulačka (Generátor servisných zmlúv)'),
        ('BUYBACK', '♻️ Výkup použitých zariadení (Lead generátor)'),
        ('WARRANTY_CLAIM', '🚨 Reklamačný formulár (RMA)'),
        ('BOOKING', '📅 Smart Booking (Online rezervácia termínu)'),
        ('FAQ', '❓ Často kladené otázky (Akordeón)'),
        ('BLOG_TEASER', '📰 Odborné články / Novinky (SEO Blog)'),
    ]
    
    typ = models.CharField("🧱 Typ bloku", max_length=20, choices=TYP_BLOKU)
    verzia = models.CharField("🏷️ Verzia", max_length=10, choices=[('LIVE', 'Ostrá verzia'), ('DRAFT', 'Koncept (AI)')], default='LIVE')
    poradie = models.PositiveIntegerField("🔢 Poradie zobrazenia", default=10, help_text="Menšie číslo sa zobrazí vyššie.")
    je_aktivny = models.BooleanField("👁️ Zobraziť na webe", default=True)
    
    nadpis = models.CharField("📝 Nadpis bloku", max_length=255, blank=True)
    podnadpis = models.TextField("📝 Podnadpis / Text", blank=True)
    obrazok = models.ImageField("🖼️ Obrázok pozadia / Ilustrácia", upload_to="web_bloky/", blank=True, null=True)
    
    tlacidlo_text = models.CharField("🔘 Text tlačidla", max_length=50, blank=True, help_text="Napr. 'Kontaktujte nás' (platí pre Hero banner).")

    class Meta:
        ordering = ['poradie', 'id']
        verbose_name = "🧱 Webový blok"
        verbose_name_plural = "🧱 Webové bloky"
        # 🚀 VÝKON: Kompozitný index pre bleskové načítanie verejného webu bez File Sortu
        indexes = [
            models.Index(fields=['je_aktivny', 'verzia', 'poradie']),
        ]

    def __str__(self):
        stav = "✅" if self.je_aktivny else "❌"
        return f"{stav} [{self.poradie}] {self.get_typ_display()} - {self.nadpis}"

    def save(self, *args, **kwargs):
        # 🛡️ ISAS STORAGE LEAK PROTECTION: Detekcia zmeny/vymazania obrázka pred uložením
        je_novy_obrazok = False
        stary_obrazok_na_zmazanie = None
        if not self.pk:
            je_novy_obrazok = bool(self.obrazok)
        else:
            try:
                stary_blok = WebBlok.objects.get(pk=self.pk)
                if stary_blok.obrazok != self.obrazok:
                    je_novy_obrazok = bool(self.obrazok)
                    if stary_blok.obrazok:
                        stary_obrazok_na_zmazanie = stary_blok.obrazok
            except WebBlok.DoesNotExist:
                je_novy_obrazok = bool(self.obrazok)

        # 🛡️ ISAS PERFORMANCE: Automatická kompresia a zmenšenie obrázkov pre bleskový web
        if je_novy_obrazok and self.obrazok and self.obrazok.name and not self.obrazok.name.lower().endswith('.webp'):
            try:
                if hasattr(self.obrazok, 'seek') and not getattr(self.obrazok, 'closed', False):
                    self.obrazok.seek(0)
                with Image.open(self.obrazok) as img:
                    img = ImageOps.exif_transpose(img)  # 🛡️ Ochrana pred otočením fotiek z mobilu

                    # 🚀 ISAS ARCHITECT: Prevod do moderného formátu WebP (so zachovaním priehľadnosti pre všetky módy)
                    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                        img = img.convert("RGBA")
                    elif img.mode != "RGB":
                        img = img.convert("RGB")

                    # Zmenšenie na max 1920px šírku (FullHD)
                    img.thumbnail((1920, 1080), Image.Resampling.LANCZOS)

                    output = BytesIO()
                    # method=6 znamená maximálne úsilie kompresného algoritmu (najmenší súbor)
                    img.save(output, format='WEBP', quality=85, method=6)
                    output.seek(0)

                    cisty_nazov = slugify(os.path.basename(self.obrazok.name).rsplit('.', 1)[0])[:85].strip('-')
                    nazov_suboru = f"{cisty_nazov or 'blok'}.webp"
                    self.obrazok.save(nazov_suboru, ContentFile(output.read()), save=False)
            except Exception:
                if hasattr(self.obrazok, 'seek') and not getattr(self.obrazok, 'closed', False):
                    self.obrazok.seek(0)

        with transaction.atomic():
            super().save(*args, **kwargs)

            if stary_obrazok_na_zmazanie and (not self.obrazok or stary_obrazok_na_zmazanie.name != self.obrazok.name):
                def _bezpecne_zmazat_stary_obrazok(subor=stary_obrazok_na_zmazanie):
                    try:
                        subor.delete(save=False)
                    except Exception:
                        pass  # 🛡️ Zlyhanie IO/Storage nesmie zhodiť request po úspešnom DB commite
                transaction.on_commit(_bezpecne_zmazat_stary_obrazok)

            def _invaliduj_cache():
                try:
                    try:
                        cache.incr('web_cache_version')
                    except ValueError:
                        cache.set('web_cache_version', 1, None)
                except Exception:
                    pass  # 🛡️ Výpadok cache nesmie zhodiť transakciu po úspešnom uložení

            transaction.on_commit(_invaliduj_cache)

    def delete(self, *args, **kwargs):
        stary_obrazok = self.obrazok
        super().delete(*args, **kwargs)
        if stary_obrazok:
            def _bezpecne_zmazat_obrazok(subor=stary_obrazok):
                try:
                    subor.delete(save=False)
                except Exception:
                    pass  # 🛡️ Zlyhanie IO/Storage nesmie zhodiť request po úspešnom zmazaní z DB
            transaction.on_commit(_bezpecne_zmazat_obrazok)

        def _invaliduj_cache():
            try:
                try:
                    cache.incr('web_cache_version')
                except ValueError:
                    cache.set('web_cache_version', 1, None)
            except Exception:
                pass  # 🛡️ Výpadok cache nesmie zhodiť transakciu po úspešnom zmazaní

        transaction.on_commit(_invaliduj_cache)

class WebFAQ(ISASAuditMixin, models.Model):
    KATEGORIA_CHOICES = [
        ('VSEOBECNE', '🌐 Všeobecné informácie'),
        ('SERVIS', '🛠️ Servis a údržba'),
        ('OBCHOD', '💰 Obchod a fakturácia'),
        ('ZARUKA', '🛡️ Záruka a reklamácie'),
    ]
    
    kategoria = models.CharField("📂 Kategória", max_length=20, choices=KATEGORIA_CHOICES, default='VSEOBECNE', help_text="Zoskupenie otázok pre lepšiu prehľadnosť na webe.")
    otazka = models.CharField("❓ Otázka", max_length=255, help_text="Napr. 'Ako často je potrebné vykonávať servisnú prehliadku zariadenia?'")
    odpoved = models.TextField("💡 Odpoveď", help_text="Detailná odpoveď. Môžete použiť aj základné HTML značky (napr. &lt;b&gt;tučný text&lt;/b&gt; alebo &lt;a href='url'&gt;odkaz&lt;/a&gt;).")
    
    slug = models.SlugField("🔗 URL Kotva (Slug)", max_length=255, unique=True, blank=True, null=True, help_text="Generuje sa automaticky. Slúži na priame zdieľanie odkazu na túto otázku.")
    poradie = models.PositiveIntegerField("🔢 Poradie", default=10, help_text="Menšie číslo sa zobrazí vyššie v rámci svojej kategórie.")
    je_aktivna = models.BooleanField("👁️ Zobraziť na webe", default=True)

    class Meta:
        ordering = ['kategoria', 'poradie', '-id']
        verbose_name = "❓ Často kladená otázka (FAQ)"
        verbose_name_plural = "❓ Často kladené otázky (FAQ)"
        indexes = [
            models.Index(fields=['je_aktivna', 'kategoria', 'poradie']),
        ]

    def __str__(self):
        stav = "✅" if self.je_aktivna else "❌"
        return f"{stav} [{self.get_kategoria_display()}] {self.otazka}"

    def clean(self):
        super().clean()
        if 'odpoved' not in self.get_deferred_fields() and self.odpoved:
            self.odpoved = sanitizuj_verejne_html(self.odpoved)

    def save(self, *args, **kwargs):
        self.clean()
        # 🤖 AUTOMATIKA: Generovanie unikátnej kotvy pre priame zdieľanie (100% Safe)
        cisty_manualny_slug = slugify(self.slug)[:255] if self.slug else ""
        if not cisty_manualny_slug:
            # 🛡️ OCHRANA PRED PÁDOM DB: Orežeme base_slug na max 240 znakov, aby ostalo miesto pre UUID
            base_slug = slugify(self.otazka)[:240].rstrip('-')
            if not base_slug:
                base_slug = "faq"
            
            candidate_slug = base_slug
            # Ak slug už existuje, pridáme náhodný 6-miestny HEX kód (Ochrana pred Race Condition)
            while WebFAQ.objects.filter(slug=candidate_slug).exclude(pk=self.pk).exists():
                candidate_slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"
            self.slug = candidate_slug
        else:
            # 🛡️ DEFENSIVE: Rešpektujeme voľbu administrátora bez nanucovania náhodných hashov
            self.slug = cisty_manualny_slug

        with transaction.atomic():
            super().save(*args, **kwargs)
            # 🧹 ISAS WEB BUILDER: Bezpečná invalidácia cache až po úspešnom commite do DB
            def _invaliduj_cache():
                try:
                    try:
                        cache.incr('web_cache_version')
                    except ValueError:
                        cache.set('web_cache_version', 1, None)
                except Exception:
                    pass  # 🛡️ Výpadok cache nesmie zhodiť transakciu po úspešnom uložení

            transaction.on_commit(_invaliduj_cache)

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)

        def _invaliduj_cache():
            try:
                try:
                    cache.incr('web_cache_version')
                except ValueError:
                    cache.set('web_cache_version', 1, None)
            except Exception:
                pass  # 🛡️ Výpadok cache nesmie zhodiť transakciu po úspešnom zmazaní

        transaction.on_commit(_invaliduj_cache)

class Clanok(ISASAuditMixin, models.Model):
    KATEGORIA_CHOICES = [
        ('NOVINKY', '📢 Novinky a Oznámenia'),
        ('NAVODY', '🛠️ Návody a Údržba'),
        ('TECHNOLOGIE', '⚙️ Technológie a Inovácie'),
        ('PRIPADOVE_STUDIE', '📊 Prípadové štúdie'),
    ]

    nadpis = models.CharField("📝 Nadpis článku", max_length=255)
    slug = models.SlugField("🔗 URL Slug", max_length=255, unique=True, blank=True, null=True, help_text="Generuje sa automaticky pre SEO.")
    kategoria = models.CharField("📂 Kategória", max_length=30, choices=KATEGORIA_CHOICES, default='NAVODY')
    
    autor = models.ForeignKey(
        'Evidencia.Zamestnanec', on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'zobrazit_na_webe': True},
        verbose_name="✍️ Autor článku",
        help_text="Zvyšuje dôveryhodnosť článku (Google E-E-A-T)."
    )

    perex = models.TextField("💡 Krátke zhrnutie (Perex)", help_text="Zobrazí sa v náhľade na domovskej stránke (max 2-3 vety).")
    obsah = models.TextField("📄 Obsah článku", help_text="Hlavný text článku. Podporuje HTML formátovanie.")
    obrazok = models.ImageField("🖼️ Hlavný obrázok", upload_to='blog/', blank=True, null=True)
    obrazok_alt = models.CharField("🖼️ ALT text obrázka", max_length=150, blank=True, null=True, help_text="Popis obrázka pre nevidiacich a Google SEO (napr. 'Servisný technik pri oprave zariadenia').")
    
    seo_titulok = models.CharField("🔍 SEO Titulok (Title Tag)", max_length=60, blank=True, null=True, help_text="Krátky úderný nadpis pre Google (max 60 znakov). Ak nevyplníte, použije sa hlavný nadpis.")
    meta_popis = models.CharField("🔍 SEO Meta Popis", max_length=160, blank=True, null=True, help_text="Krátky text pre Google vyhľadávač (max 160 znakov). Ak nevyplníte, použije sa Perex.")
    
    datum_vydania = models.DateField("📅 Dátum vydania", default=timezone.now)
    je_aktivny = models.BooleanField("👁️ Zobraziť na webe", default=True)

    class Meta:
        verbose_name = "📰 Článok / Novinka"
        verbose_name_plural = "📰 Odborné články (Blog)"
        ordering = ['-datum_vydania', '-id']
        indexes = [
            models.Index(fields=['je_aktivny', '-datum_vydania', '-id']),
        ]

    def clean(self):
        super().clean()
        if 'obsah' not in self.get_deferred_fields() and self.obsah:
            self.obsah = sanitizuj_verejne_html(self.obsah)

        if self.seo_titulok:
            self.seo_titulok = self.seo_titulok.strip()
        if not self.seo_titulok and self.nadpis:
            self.seo_titulok = self.nadpis.strip()[:60]

        if self.meta_popis:
            self.meta_popis = self.meta_popis.strip()
        if not self.meta_popis and self.perex:
            cisty_perex = " ".join(strip_tags(self.perex).split())
            self.meta_popis = cisty_perex[:160]

    @property
    def cas_citania(self):
        """🧠 ISAS UX: Automatický výpočet času čítania (priemerne 200 slov za minútu)."""
        if not self.obsah:
            return 1
        pocet_slov = len(strip_tags(self.obsah).split())
        # Celočíselné zaokrúhlenie nahor bez potreby importu modulu math
        return max(1, (pocet_slov + 199) // 200)

    def __str__(self):
        stav = "🟢" if self.je_aktivny else "🔴"
        return f"{stav} {self.nadpis}"

    def save(self, *args, **kwargs):
        # Zabezpečenie SEO atribútov aj pri programovom vytvorení mimo Admin formulára
        self.clean()
        # 🛡️ ISAS STORAGE LEAK PROTECTION: Zmažeme starý súbor pri zmene obrázka
        je_novy_obrazok = False
        stary_obrazok_na_zmazanie = None
        if not self.pk:
            je_novy_obrazok = bool(self.obrazok)
        else:
            try:
                stary_clanok = Clanok.objects.get(pk=self.pk)
                if stary_clanok.obrazok != self.obrazok:
                    je_novy_obrazok = bool(self.obrazok)
                    if stary_clanok.obrazok:
                        stary_obrazok_na_zmazanie = stary_clanok.obrazok
            except Clanok.DoesNotExist:
                je_novy_obrazok = bool(self.obrazok)

        # WebP Kompresia pre bleskové načítanie blogu
        if je_novy_obrazok and self.obrazok and self.obrazok.name and not self.obrazok.name.lower().endswith('.webp'):
            try:
                if hasattr(self.obrazok, 'seek') and not getattr(self.obrazok, 'closed', False):
                    self.obrazok.seek(0)
                with Image.open(self.obrazok) as img:
                    img = ImageOps.exif_transpose(img) # 🛡️ Ochrana pred otočením fotiek z mobilu
                    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                        img = img.convert("RGBA")
                    elif img.mode != "RGB":
                        img = img.convert("RGB")
                    img.thumbnail((1200, 800), Image.Resampling.LANCZOS)
                    output = BytesIO()
                    img.save(output, format='WEBP', quality=85, method=6)
                    output.seek(0)
                    cisty_nazov = slugify(os.path.basename(self.obrazok.name).rsplit('.', 1)[0])[:85].strip('-')
                    nazov_suboru = f"{cisty_nazov or 'clanok'}.webp"
                    self.obrazok.save(nazov_suboru, ContentFile(output.read()), save=False)
            except Exception:
                if hasattr(self.obrazok, 'seek') and not getattr(self.obrazok, 'closed', False):
                    self.obrazok.seek(0)

        # Generovanie unikátnej kotvy pre SEO (100% Safe)
        cisty_manualny_slug = slugify(self.slug)[:255] if self.slug else ""
        if not cisty_manualny_slug:
            base_slug = slugify(self.nadpis)[:240].rstrip('-')
            if not base_slug:
                base_slug = "clanok"
            candidate_slug = base_slug
            while Clanok.objects.filter(slug=candidate_slug).exclude(pk=self.pk).exists():
                candidate_slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"
            self.slug = candidate_slug
        else:
            # 🛡️ DEFENSIVE: Rešpektujeme voľbu administrátora bez nanucovania náhodných hashov
            self.slug = cisty_manualny_slug

        with transaction.atomic():
            super().save(*args, **kwargs)

            if stary_obrazok_na_zmazanie and (not self.obrazok or stary_obrazok_na_zmazanie.name != self.obrazok.name):
                def _bezpecne_zmazat_stary_obrazok(subor=stary_obrazok_na_zmazanie):
                    try:
                        subor.delete(save=False)
                    except Exception:
                        pass  # 🛡️ Zlyhanie IO/Storage nesmie zhodiť request po úspešnom DB commite
                transaction.on_commit(_bezpecne_zmazat_stary_obrazok)

            def _invaliduj_cache():
                try:
                    try:
                        cache.incr('web_cache_version')
                    except ValueError:
                        cache.set('web_cache_version', 1, None)
                except Exception:
                    pass  # 🛡️ Výpadok cache nesmie zhodiť transakciu po úspešnom uložení

            transaction.on_commit(_invaliduj_cache)

    def delete(self, *args, **kwargs):
        stary_obrazok = self.obrazok
        super().delete(*args, **kwargs)
        if stary_obrazok:
            def _bezpecne_zmazat_obrazok(subor=stary_obrazok):
                try:
                    subor.delete(save=False)
                except Exception:
                    pass  # 🛡️ Zlyhanie IO/Storage nesmie zhodiť request po úspešnom zmazaní z DB
            transaction.on_commit(_bezpecne_zmazat_obrazok)

        def _invaliduj_cache():
            try:
                try:
                    cache.incr('web_cache_version')
                except ValueError:
                    cache.set('web_cache_version', 1, None)
            except Exception:
                pass  # 🛡️ Výpadok cache nesmie zhodiť transakciu po úspešnom zmazaní

        transaction.on_commit(_invaliduj_cache)

class WebhookEndpoint(ISASAuditMixin, models.Model):
    UDALOST_CHOICES = [
        ('SERVIS_DOKONCENY', '🛠️ Servisný zásah bol dokončený'),
        ('OBJEDNAVKA_ZMENA', '📦 Zmena stavu objednávky/dopytu'),
        ('ZARIADENIE_PORUCHA', '🚨 Zariadenie nahlásilo poruchu (IoT)'),
    ]

    zakaznik = models.ForeignKey(
        'Evidencia.Zakaznik', on_delete=models.CASCADE, related_name='webhooks',
        verbose_name="🏢 Zákazník", help_text="Klient, do ktorého systému (napr. SAP) budeme dáta posielať."
    )
    nazov = models.CharField("🏷️ Názov integrácie", max_length=150, help_text="Napr. 'SAP Integrácia - Duslo'")
    url = models.URLField(
        "🔗 Cieľová URL (Endpoint)", max_length=500, 
        validators=[validate_safe_webhook_url], # 🛡️ Pridaný validátor
        help_text="URL adresa na serveri klienta (musí byť HTTPS)."
    )
    
    # 🛡️ BEZPEČNOSŤ & AUTENTIFIKÁCIA
    tajny_kluc = models.CharField(
        "🔑 Tajný kľúč (HMAC Secret)", max_length=128, default=secrets.token_urlsafe,
        help_text="Tento kľúč slúži na kryptografický podpis (HMAC-SHA256). Poskytnite ho IT oddeleniu klienta."
    )
    vlastne_hlavicky = models.JSONField(
        "⚙️ Vlastné HTTP Hlavičky", default=dict, blank=True,
        help_text='Napr. {"Authorization": "Bearer token123", "X-API-Key": "abc"}. Formát JSON.'
    )
    
    udalost = models.CharField("⚡ Sledovaná udalosť", max_length=50, choices=UDALOST_CHOICES, default='SERVIS_DOKONCENY')
    je_aktivny = models.BooleanField("✅ Aktívny", default=True)
    
    # 🛡️ CIRCUIT BREAKER (Ochrana pred mŕtvymi servermi)
    max_zlyhani = models.PositiveIntegerField("🛑 Max. zlyhaní po sebe", default=10, help_text="Po koľkých zlyhaniach sa webhook automaticky vypne.")
    zlyhania_po_sebe = models.PositiveIntegerField("❌ Aktuálne zlyhania", default=0, editable=False)
    dovod_deaktivacie = models.CharField("ℹ️ Dôvod deaktivácie", max_length=255, blank=True, null=True, editable=False)

    class Meta:
        verbose_name = "🔗 Webhook (API Integrácia)"
        verbose_name_plural = "🔗 Webhooky (API Integrácie)"
        constraints = [
            models.UniqueConstraint(fields=['zakaznik', 'url', 'udalost'], name='unique_zakaznik_webhook_udalost')
        ]

    def __str__(self):
        try:
            nazov_zakaznika = getattr(self.zakaznik, 'Nazov_spolocnosti', 'Neznámy zákazník') if self.zakaznik_id else 'Neznámy zákazník'
        except models.ObjectDoesNotExist:
            nazov_zakaznika = 'Neznámy zákazník'
        return f"{nazov_zakaznika} - {self.get_udalost_display()}"

    def clean(self):
        super().clean()
        if self.url:
            self.url = self.url.strip()
        if self.tajny_kluc:
            self.tajny_kluc = self.tajny_kluc.strip()

        # 🛡️ DEFENSIVE: Circuit breaker musí mať prah zlyhaní aspoň 1
        if self.max_zlyhani is None or self.max_zlyhani < 1:
            raise ValidationError({"max_zlyhani": "Maximálny počet povolených zlyhaní po sebe musí byť aspoň 1."})

        # 🛡️ BEZPEČNOSŤ: Tajný HMAC kľúč nesmie byť prázdny ani tvorený iba medzerami
        if not self.tajny_kluc:
            raise ValidationError({"tajny_kluc": "Tajný HMAC kľúč nesmie byť prázdny."})

        # 🛡️ DEFENSIVE: Garantujeme, že vlastné hlavičky sú vždy dict (ochrana pred zadaním null, zoznamu [] alebo reťazca)
        if self.vlastne_hlavicky is None:
            self.vlastne_hlavicky = {}
        elif not isinstance(self.vlastne_hlavicky, dict):
            raise ValidationError({"vlastne_hlavicky": "Hlavičky musia byť vo formáte JSON objektu (kľúč-hodnota)."})

        if self.vlastne_hlavicky:
            for k, v in self.vlastne_hlavicky.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ValidationError({"vlastne_hlavicky": "Všetky kľúče aj hodnoty hlavičiek musia byť textové reťazce."})
                if not k.strip():
                    raise ValidationError({"vlastne_hlavicky": "Názov HTTP hlavičky (kľúč) nesmie byť prázdny reťazec."})
                if any(c in f"{k}{v}" for c in ['\r', '\n']):
                    raise ValidationError({"vlastne_hlavicky": f"Zistený pokus o CRLF injection v hlavičke '{k}'."})

    def save(self, *args, **kwargs):
        # 🚀 ARCHITECT OPTIMIZATION: full_clean spúšťame iba pri plnom uložení, nie pri worker-update cez update_fields
        if kwargs.get('update_fields') is None:
            self.full_clean()

        # 🛡️ CIRCUIT BREAKER AUTO-HEAL: Resetujeme zlyhania IBA pri reálnej reaktivácii (ak bol v DB vypnutý alebo mal dôvod deaktivácie)
        je_potencialna_aktivacia = kwargs.get('update_fields') is None or 'je_aktivny' in kwargs.get('update_fields', [])
        if self.pk and self.je_aktivny and je_potencialna_aktivacia:
            predchadzajuci_stav = WebhookEndpoint.objects.filter(pk=self.pk).values('je_aktivny', 'dovod_deaktivacie').first()
            if predchadzajuci_stav and (not predchadzajuci_stav['je_aktivny'] or predchadzajuci_stav['dovod_deaktivacie']):
                self.zlyhania_po_sebe = 0
                self.dovod_deaktivacie = None
                if 'update_fields' in kwargs and kwargs['update_fields'] is not None:
                    kwargs['update_fields'] = list(set(kwargs['update_fields']).union({'zlyhania_po_sebe', 'dovod_deaktivacie'}))
        super().save(*args, **kwargs)

    def generuj_podpis(self, payload_json):
        """🧠 ISAS SECURITY: Vygeneruje HMAC-SHA256 podpis pre payload (odolné voči str, bytes aj dict)."""
        if isinstance(payload_json, (dict, list)):
            payload_bytes = json.dumps(
                payload_json, 
                cls=DjangoJSONEncoder, 
                separators=(',', ':'), 
                sort_keys=True, 
                ensure_ascii=False
            ).encode('utf-8')
        elif isinstance(payload_json, str):
            payload_bytes = payload_json.encode('utf-8')
        elif isinstance(payload_json, (bytes, bytearray)):
            payload_bytes = bytes(payload_json)
        else:
            raise TypeError(f"Nepodporovaný formát dát pre podpis HMAC: {type(payload_json)}")

        if not self.tajny_kluc:
            raise ValueError("⚠️ Kritická chyba: WebhookEndpoint nemá nakonfigurovaný tajný kľúč (HMAC Secret).")
        tajny_kluc_bytes = self.tajny_kluc.encode('utf-8')
        return hmac.new(
            tajny_kluc_bytes,
            payload_bytes,
            hashlib.sha256
        ).hexdigest()


class WebhookLog(models.Model):
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name='logy')
    event_id = models.UUIDField("🆔 Event ID", default=uuid.uuid4, editable=False, db_index=True) # 🛡️ Idempotencia
    pokus = models.PositiveSmallIntegerField("🔢 Číslo pokusu", default=1, db_index=True)
    cas_odoslania = models.DateTimeField(auto_now_add=True, db_index=True)
    
    payload = models.TextField("Odoslané dáta (JSON)")
    request_headers = models.TextField("Odoslané hlavičky", blank=True, null=True)
    
    http_status = models.IntegerField("HTTP Status", null=True, blank=True)
    odpoved_servera = models.TextField("Odpoveď klienta (Body)", blank=True, null=True)
    response_headers = models.TextField("Prijaté hlavičky", blank=True, null=True)
    
    trvanie_ms = models.PositiveIntegerField("⏱️ Latencia (ms)", null=True, blank=True)
    uspesne = models.BooleanField("Úspešné", default=False, db_index=True)

    def __str__(self):
        stav = "✅" if self.uspesne else f"❌ {self.http_status or 'ERR'}"
        cas = self.cas_odoslania.strftime('%d.%m.%Y %H:%M:%S') if self.cas_odoslania else "Nenaplánované"
        return f"[{stav}] Endpoint #{self.endpoint_id} (Pokus #{self.pokus}) ({cas})"

    def save(self, *args, **kwargs):
        # 🛡️ DEFENSIVE & POSTGRES SHIELD: Robustná normalizácia ľubovoľného typu na sanitizovaný str
        def _ocisti_textovy_vstup(hodnota, max_dlzka=None, text_skratenia=""):
            if hodnota is None:
                return None
            if isinstance(hodnota, (bytes, bytearray)):
                hodnota = hodnota.decode('utf-8', errors='replace')
            elif isinstance(hodnota, (dict, list)):
                hodnota = json.dumps(hodnota, cls=DjangoJSONEncoder, ensure_ascii=False)
            elif not isinstance(hodnota, str):
                hodnota = str(hodnota)
            
            # Odstránenie NUL bajtov (\x00) pre Postgres
            hodnota = hodnota.replace('\x00', '')
            if max_dlzka and len(hodnota) > max_dlzka:
                hodnota = hodnota[:max_dlzka] + text_skratenia
            return hodnota

        self.odpoved_servera = _ocisti_textovy_vstup(
            self.odpoved_servera, 100_000, "\n... [ODPOVEĎ SKRÁTENÁ SYSTÉMOM PRE PREKROČENIE LIMITU 100 KB]"
        )
        self.response_headers = _ocisti_textovy_vstup(
            self.response_headers, 20_000, "\n... [HLAVIČKY SKRÁTENÉ SYSTÉMOM PRE PREKROČENIE LIMITU 20 KB]"
        )
        self.payload = _ocisti_textovy_vstup(self.payload) or "{}"
        self.request_headers = _ocisti_textovy_vstup(self.request_headers)

        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "📜 Webhook Log"
        verbose_name_plural = "📜 Webhook Logy"
        ordering = ['-cas_odoslania']
        indexes = [
            models.Index(fields=['endpoint', '-cas_odoslania']),
            models.Index(fields=['endpoint', 'uspesne']),
            models.Index(fields=['event_id', 'pokus']),
        ]