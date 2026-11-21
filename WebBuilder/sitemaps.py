from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from django.core.exceptions import FieldDoesNotExist
from django.apps import apps
from django.utils import timezone
from django.db import models

from WebBuilder.utils_helper import get_eshop_models

from Evidencia.models import TypServisuPrace, ServisnyZaznam, ServisovaneZariadenie

class StaticViewSitemap(Sitemap):
    def items(self):
        polozky = [
            'webbuilder:home', 'webbuilder:sluzby_list', 'webbuilder:referencie_list',
            'webbuilder:eshop_list', 'webbuilder:blog_list', 'webbuilder:kariera_list'
        ]
        # 🚜 FEATURE TOGGLING: Zoznam požičovne indexujeme iba ak sú v DB aktívne stroje
        if ServisovaneZariadenie.objects.filter(je_urcene_na_prenajom=True, zobrazit_na_webe_prenajom=True).exists():
            polozky.append('webbuilder:rental_list')
        # 🛡️ FEATURE TOGGLING: Nástroje indexujeme iba ak odvetvie podporuje aspoň 1 nástroj
        try:
            from Evidencia.industry_loader import (
                is_esg_audit_supported, HAS_HEAT_RECOVERY, HAS_VTZ_INSPECTION,
                HAS_FPLYNY_AUDIT, HAS_VIBRO_DIAGNOSTICS, HAS_CAPEX_CALCULATOR
            )
            if is_esg_audit_supported() or HAS_HEAT_RECOVERY or HAS_VTZ_INSPECTION or HAS_FPLYNY_AUDIT or HAS_VIBRO_DIAGNOSTICS or HAS_CAPEX_CALCULATOR:
                reverse('webbuilder:nastroje')
                polozky.append('webbuilder:nastroje')
        except Exception:
            pass

        try:
            reverse('webbuilder:reklamacie')
            polozky.append('webbuilder:reklamacie')
        except Exception:
            pass

        return polozky

    def priority(self, item):
        priority_map = {
            'webbuilder:home': 1.0,
            'webbuilder:sluzby_list': 0.9,
            'webbuilder:referencie_list': 0.8,
            'webbuilder:rental_list': 0.8,
            'webbuilder:nastroje': 0.8,
            'webbuilder:eshop_list': 0.8,
            'webbuilder:blog_list': 0.7,
            'webbuilder:kariera_list': 0.6,
            'webbuilder:reklamacie': 0.5,
        }
        return priority_map.get(item, 0.7)

    def changefreq(self, item):
        if item in ['webbuilder:home', 'webbuilder:eshop_list']:
            return 'daily'
        if item in ['webbuilder:reklamacie']:
            return 'monthly'
        return 'weekly'

    def location(self, item):
        return reverse(item)

class SluzbySitemap(Sitemap):
    priority = 0.8
    changefreq = 'weekly'

    def items(self):
        return TypServisuPrace.objects.filter(zobrazit_na_webe=True).exclude(slug__isnull=True).exclude(slug__exact='').order_by('id')

    def lastmod(self, obj):
        return getattr(obj, 'upravene', None) or getattr(obj, 'vytvorene', None)

    def location(self, obj):
        return reverse('webbuilder:sluzba_detail', args=[obj.slug])

class ReferencieSitemap(Sitemap):
    priority = 0.7
    changefreq = 'monthly'

    def items(self):
        # 🛡️ SEO & INTEGRITA: Zosúladené s detailom služby (akceptuje foto PO, foto PRED aj video)
        return ServisnyZaznam.objects.filter(
            zobrazit_na_webe=True, dokoncene=True
        ).filter(
            (models.Q(foto_po__isnull=False) & ~models.Q(foto_po='')) |
            (models.Q(foto_pred__isnull=False) & ~models.Q(foto_pred='')) |
            (models.Q(video__isnull=False) & ~models.Q(video=''))
        ).exclude(slug__isnull=True).exclude(slug__exact='').order_by('-vytvorene', '-id')

    def lastmod(self, obj):
        return getattr(obj, 'upravene', None) or getattr(obj, 'vytvorene', None)

    def location(self, obj):
        return reverse('webbuilder:referencia_detail', args=[obj.slug])

class RentalSitemap(Sitemap):
    priority = 0.8
    changefreq = 'weekly'

    def items(self):
        # 🛡️ SEO DEDUPLIKÁCIA: Do sitemapy posielame iba 1 reprezentatívnu URL za model (ochrana pred duplicate content)
        active_param_rels = []
        try:
            from Evidencia.industry_loader import get_active_parameter_reverse_names
            active_param_rels = get_active_parameter_reverse_names()
        except Exception:
            pass

        qs = ServisovaneZariadenie.objects.filter(
            je_urcene_na_prenajom=True, zobrazit_na_webe_prenajom=True
        ).exclude(slug__isnull=True).exclude(slug__exact='').select_related(
            'vyrobca', *active_param_rels
        ).order_by('v_prenajme_od_isas', 'v_servisnom_rezime', 'id')

        reprezentativne_stroje = []
        modely_mapa = set()

        for stroj in qs:
            vyrobca_nazov = stroj.vyrobca.nazov_vyrobcu if stroj.vyrobca else ""
            model_nazov = stroj.model_zariadenia or ""
            kluc = f"{vyrobca_nazov} {model_nazov}".strip()

            if kluc not in modely_mapa:
                modely_mapa.add(kluc)
                reprezentativne_stroje.append(stroj)

        return reprezentativne_stroje

    def lastmod(self, obj):
        return getattr(obj, 'upravene', None) or getattr(obj, 'vytvorene', None)

    def location(self, obj):
        return reverse('webbuilder:rental_detail', args=[obj.slug])

class EshopSitemap(Sitemap):
    priority = 0.9
    changefreq = 'daily'

    def items(self):
        povolene_modely = get_eshop_models()
        diely = []
        for model in povolene_modely:
            try:
                model._meta.get_field('slug')
                model._meta.get_field('zobrazit_na_webe')
                diely.extend(list(model.objects.filter(zobrazit_na_webe=True).exclude(slug__isnull=True).exclude(slug__exact='').order_by('id')))
            except FieldDoesNotExist:
                pass

        return diely

    def lastmod(self, obj):
        return getattr(obj, 'upravene', None) or getattr(obj, 'vytvorene', None)

    def location(self, obj):
        return reverse('webbuilder:eshop_detail', args=[obj.slug])

class KarieraSitemap(Sitemap):
    priority = 0.8
    changefreq = 'weekly'

    def items(self):
        # 🛡️ SEO & STABILITA: Do sitemapy púšťame len aktívne a neexpirované inzeráty s platným slugom (USE_TZ=False safe)
        dnes = timezone.now().date()
        PracovnaPozicia = apps.get_model('Evidencia', 'PracovnaPozicia')
        return PracovnaPozicia.objects.filter(
            je_aktivna=True
        ).filter(
            models.Q(platnost_do__isnull=True) | models.Q(platnost_do__gte=dnes)
        ).exclude(slug__isnull=True).exclude(slug__exact='').order_by('poradie', '-id')

    def lastmod(self, obj):
        return getattr(obj, 'upravene', None) or getattr(obj, 'vytvorene', None)

    def location(self, obj):
        return reverse('webbuilder:kariera_detail', args=[obj.slug])

class BlogSitemap(Sitemap):
    priority = 0.7
    changefreq = 'weekly'

    def items(self):
        # 🛡️ STABILITA & SEO: Do sitemapy pustíme len aktívne články s platným slugom a dátumom vydania <= DNES (USE_TZ=False safe)
        dnes = timezone.now().date()
        return apps.get_model('WebBuilder', 'Clanok').objects.filter(
            je_aktivny=True,
            datum_vydania__lte=dnes
        ).exclude(slug__isnull=True).exclude(slug__exact='').order_by('-datum_vydania', '-id')

    def lastmod(self, obj):
        return getattr(obj, 'upravene', None) or getattr(obj, 'datum_vydania', None) or getattr(obj, 'vytvorene', None)

    def location(self, obj):
        return reverse('webbuilder:blog_detail', args=[obj.slug])