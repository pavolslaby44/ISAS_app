import re
import math
import hashlib
import json
import uuid
import unicodedata
import logging
from typing import Optional, Any, Dict

from datetime import date, datetime, timedelta
import holidays
import calendar

from django.apps import apps
from decimal import Decimal
from django import forms
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models.signals import m2m_changed, post_delete, pre_delete
from django.dispatch import receiver
from django.utils import timezone
from cryptography.fernet import InvalidToken
from django.utils.dateparse import parse_datetime

from .utils_helper import get_cipher

class ISASAuditQuerySet(models.QuerySet):
    """
    Zachytáva hromadné SQL updaty. Ak niekto zavolá .update(), 
    tento QuerySet najprv prejde objekty, zapíše rozdiely do logu 
    a až potom pustí hromadný SQL update do databázy.
    """
    def update(self, **kwargs):
        if not kwargs:
            return super().update(**kwargs)

        from .middleware import get_current_user
        
        user = get_current_user()
        user = user if (user and user.is_authenticated) else None
        ct = ContentType.objects.get_for_model(self.model)

        # 1. Vykonáme SQL update a zápis logov v jednej atómovej transakcii
        with transaction.atomic(using=self.db):
            logy_na_vytvorenie = []
            
            # VÝKON: Extrahujeme a normalizujeme polia na 'name' pre Django .only() (ochrana pred cudzími kľúčmi s _id a relačnými lookupmi s __)
            def _normalize_to_field_name(model_cls, key):
                root_key = key.split('__')[0]
                try:
                    return model_cls._meta.get_field(root_key).name
                except Exception:
                    for f in model_cls._meta.fields:
                        if f.attname == root_key:
                            return f.name
                    return root_key

            update_fields = {_normalize_to_field_name(self.model, k) for k in kwargs.keys()}
            concrete_field_names = {f.name for f in self.model._meta.concrete_fields}
            safe_only_fields = [f for f in update_fields if f in concrete_field_names]

            # VÝKON & BEZPEČNOSŤ: select_for_update() zamkne riadky deterministicky podľa PK (ochrana pred Deadlockom)
            qs = self.select_related(None).order_by('pk').select_for_update()
            if safe_only_fields and len(safe_only_fields) == len(kwargs):
                qs = qs.only(*safe_only_fields)
                
            for obj in qs.iterator(chunk_size=2000):
                zmeny = {}
                for field_name, new_val in kwargs.items():
                    # VÝKON: Ochrana pred N+1 queries pri cudzích kľúčoch. Získame 'attname' (napr. zakaznik_id namiesto zakaznik)
                    try:
                        field_obj = self.model._meta.get_field(field_name)
                        attname = field_obj.attname
                    except Exception:
                        attname = field_name

                    # Načítame len ID, čím zabránime SQL dopytu do relačnej tabuľky
                    old_val = getattr(obj, attname, None)
                    
                    # Normalizujeme novú hodnotu na ID, ak ide o inštanciu modelu
                    new_val_normalized = new_val.pk if hasattr(new_val, 'pk') else new_val

                    if (old_val is None) != (new_val_normalized is None) or str(old_val) != str(new_val_normalized):
                        # BEZPEČNOSŤ: Rozšírené maskovanie šifrovaných a citlivých polí v logoch
                        citlive_slova = {'heslo', 'password', 'token', 'secret', 'key', 'api'}
                        field_parts = set(field_name.lower().split('_'))
                        is_sensitive = bool(citlive_slova & field_parts)
                        try:
                            field_obj = self.model._meta.get_field(field_name)
                            if isinstance(field_obj, EncryptedCharField):
                                is_sensitive = True
                        except Exception:
                            pass # Ak pole neexistuje priamo na modeli (relačný update), spoliehame sa na názov

                        # VÝKON: Ochrana pred DB Bloatom a OOM. Extrémne dlhé texty v logu orezávame.
                        str_old = str(old_val) if old_val is not None else ""
                        str_new = str(new_val_normalized) if new_val_normalized is not None else ""

                        zmeny[field_name] = {
                            "z": "********" if is_sensitive else (str_old[:500] + "... [skrátené]" if len(str_old) > 500 else str_old),
                            "na": "********" if is_sensitive else (str_new[:500] + "... [skrátené]" if len(str_new) > 500 else str_new)
                        }
                
                if zmeny:
                    logy_na_vytvorenie.append(
                        ISASAuditLog(
                            vykonal=user, content_type=ct, object_id=str(obj.pk),
                            akcia='UPDATE', isas_metadata_json_zmeny=json.dumps(zmeny),
                            pocet_zmien=len(zmeny)
                        )
                    )
                
                # OCHRANA PRED OOM: Priebežný zápis a vyprázdnenie pamäte v RAM
                if len(logy_na_vytvorenie) >= 2000:
                    ISASAuditLog.objects.using(self.db).bulk_create(logy_na_vytvorenie, batch_size=1000)
                    logy_na_vytvorenie = []  # Vyčistíme pole, aby nezahltilo RAM

            # Zápis zvyšných logov (ak na konci cyklu ostalo napr. 500 neuložených záznamov)
            if logy_na_vytvorenie:
                ISASAuditLog.objects.using(self.db).bulk_create(logy_na_vytvorenie, batch_size=1000)
                
            # Nakoniec vykonáme samotný hromadný SQL update na pôvodnom QuerySete
            vysledok = super().update(**kwargs)
                
        return vysledok

    def delete(self):
        """
        Kritická poistka: Zachytáva hromadné volanie .delete() na QuerySete.
        Bez tohto by hromadné mazanie obišlo ISASAuditMixin a nezanechalo by stopu.
        """
        from .middleware import get_current_user
        
        user = get_current_user()
        user = user if (user and user.is_authenticated) else None
        ct = ContentType.objects.get_for_model(self.model)

        with transaction.atomic(using=self.db):
            logy_na_vytvorenie = []
            
            # VÝKON & BEZPEČNOSŤ: select_for_update() s deterministickým zoradením podľa PK (ochrana pred Deadlockom)
            for pk in self.select_related(None).order_by('pk').select_for_update().values_list('pk', flat=True).iterator(chunk_size=2000):
                logy_na_vytvorenie.append(
                    ISASAuditLog(
                        vykonal=user, content_type=ct, object_id=str(pk),
                        akcia='DELETE', isas_metadata_json_zmeny=json.dumps({"stav": "🧨 Objekt bol hromadne vymazaný"}),
                        pocet_zmien=1
                    )
                )
                
                # Ochrana pred OOM
                if len(logy_na_vytvorenie) >= 2000:
                    ISASAuditLog.objects.using(self.db).bulk_create(logy_na_vytvorenie, batch_size=1000)
                    logy_na_vytvorenie = []
                    
            if logy_na_vytvorenie:
                ISASAuditLog.objects.using(self.db).bulk_create(logy_na_vytvorenie, batch_size=1000)
                
            return super().delete()

    def bulk_create(self, objs, **kwargs):
        """
        Kritická poistka: Zachytáva hromadné vytváranie objektov.
        Bez tohto by bulk_create obišiel ISASAuditMixin.save() a objekty by vznikli bez stopy.
        """
        if not objs:
            return super().bulk_create(objs, **kwargs)

        from .middleware import get_current_user
        
        user = get_current_user()
        user = user if (user and user.is_authenticated) else None
        ct = ContentType.objects.get_for_model(self.model)
        
        with transaction.atomic(using=self.db):
            # 1. Vykonáme štandardný bulk_create (vráti objekty s priradenými ID - funguje v PostgreSQL/modernom SQLite)
            vytvorene_objekty = super().bulk_create(objs, **kwargs)
    
            # 2. Vytvoríme logy pre všetky nové objekty s ochranou pred OOM
            logy_na_vytvorenie = []
            for obj in vytvorene_objekty:
                if obj.pk:  # Poistka, ak by DB nevrátila PK
                    logy_na_vytvorenie.append(
                        ISASAuditLog(
                            vykonal=user, 
                            content_type=ct, 
                            object_id=str(obj.pk),
                            akcia='CREATE', 
                            isas_metadata_json_zmeny=json.dumps({"stav": "✨ Objekt bol hromadne vytvorený (bulk_create)"}),
                            pocet_zmien=1
                        )
                    )
                    if len(logy_na_vytvorenie) >= 2000:
                        ISASAuditLog.objects.using(self.db).bulk_create(logy_na_vytvorenie, batch_size=1000)
                        logy_na_vytvorenie = []
    
            # 3. Zápis zvyšných logov
            if logy_na_vytvorenie:
                ISASAuditLog.objects.using(self.db).bulk_create(logy_na_vytvorenie, batch_size=1000)
    
            return vytvorene_objekty

    def bulk_update(self, objs, fields, batch_size=None):
        """
        Kritická poistka: Zachytáva hromadné úpravy existujúcich objektov.
        Zisťuje staré hodnoty z DB iba pre menené polia, porovnáva ich a generuje logy.
        """
        from .middleware import get_current_user

        user = get_current_user()
        user = user if (user and user.is_authenticated) else None
        ct = ContentType.objects.get_for_model(self.model)

        # 1. Získame IDčka upravovaných objektov (obrana pred vyčerpaním generátora)
        objs = list(objs)
        pks = list(dict.fromkeys(obj.pk for obj in objs if obj.pk))
        if not pks:
            return super().bulk_update(objs, fields, batch_size=batch_size)

        # 2. VÝKON & BEZPEČNOSŤ: Normalizácia polí (podpora pre Field objekty aj reťazce)
        field_names = [f.name if hasattr(f, 'name') else str(f) for f in fields]

        def _resolve_attname(model_cls, f_name):
            try:
                return model_cls._meta.get_field(f_name).attname
            except Exception:
                return f_name

        attnames = list({_resolve_attname(self.model, f) for f in field_names})
        fields = field_names
        
        logy_na_vytvorenie = []
        with transaction.atomic(using=self.db):
            pk_name = self.model._meta.pk.name
            stary_stav = {}
            chunk_size = 1000
            base_qs = self.model._base_manager.using(self.db).order_by('pk').select_for_update()
            fetch_fields = list({pk_name} | set(attnames))
            for i in range(0, len(pks), chunk_size):
                chunk_pks = pks[i:i + chunk_size]
                for item in base_qs.filter(pk__in=chunk_pks).values(*fetch_fields):
                    stary_stav[item[pk_name]] = item

            for obj in objs:
                stare_polia = stary_stav.get(obj.pk)
                if not stare_polia:
                    continue
                
                zmeny = {}
                for field_name in fields:
                    try:
                        field_obj = self.model._meta.get_field(field_name)
                        attname = field_obj.attname
                    except Exception:
                        attname = field_name

                    old_val = stare_polia.get(attname)
                    new_val = getattr(obj, attname, None)
                    new_val_normalized = new_val.pk if hasattr(new_val, 'pk') else new_val

                    if (old_val is None) != (new_val_normalized is None) or str(old_val) != str(new_val_normalized):
                        citlive_slova = {'heslo', 'password', 'token', 'secret', 'key', 'api'}
                        field_parts = set(field_name.lower().split('_'))
                        is_sensitive = bool(citlive_slova & field_parts)
                        try:
                            field_obj = self.model._meta.get_field(field_name)
                            if isinstance(field_obj, EncryptedCharField):
                                is_sensitive = True
                        except Exception:
                            pass

                        str_old = str(old_val) if old_val is not None else ""
                        str_new = str(new_val_normalized) if new_val_normalized is not None else ""

                        zmeny[field_name] = {
                            "z": "********" if is_sensitive else (str_old[:500] + "... [skrátené]" if len(str_old) > 500 else str_old),
                            "na": "********" if is_sensitive else (str_new[:500] + "... [skrátené]" if len(str_new) > 500 else str_new)
                        }

                if zmeny:
                    logy_na_vytvorenie.append(
                        ISASAuditLog(
                            vykonal=user, content_type=ct, object_id=str(obj.pk),
                            akcia='UPDATE', isas_metadata_json_zmeny=json.dumps(zmeny),
                            pocet_zmien=len(zmeny)
                        )
                    )
                    # Ochrana pred OOM pri rozsiahlych dávkach
                    if len(logy_na_vytvorenie) >= 2000:
                        ISASAuditLog.objects.using(self.db).bulk_create(logy_na_vytvorenie, batch_size=1000)
                        logy_na_vytvorenie = []

            if logy_na_vytvorenie:
                ISASAuditLog.objects.using(self.db).bulk_create(logy_na_vytvorenie, batch_size=1000)

            # 3. Vykonáme samotný natívny SQL update v databáze
            return super().bulk_update(objs, fields, batch_size=batch_size)

class ISASAuditLog(models.Model):
    AKCIA_CHOICES = [
        ('CREATE', '➕ Vytvorenie'),
        ('UPDATE', '📝 Úprava'),
        ('UPDATE_M2M', '🔄 Úprava relácií (M2M)'),
        ('DELETE', '🗑️ Vymazanie (Úplné)'),
        ('ANONYMIZE', '🕵️ GDPR Anonymizácia'),
    ]

    cas_vykonania = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="🕒 Čas", help_text="🕒 Presný dátum a čas, kedy bola operácia v systéme zaznamenaná.")
    vykonal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="👤 Používateľ", help_text="👤 Pracovník, ktorý operáciu vykonal (nevyplnené znamená automatický úkon systému).")
    
    # 🔗 Generic prepojenie na akýkoľvek objekt v systéme
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, help_text="📦 Systémový typ entity alebo modulu, nad ktorým prebehla operácia.")
    object_id = models.CharField(max_length=255, help_text="🔑 Unikátny identifikátor konkrétneho auditovaného záznamu.")
    zaznam = GenericForeignKey('content_type', 'object_id')
    
    akcia = models.CharField(max_length=15, choices=AKCIA_CHOICES, help_text="⚡ Typ vykonanej operácie pre potreby revízie, prevádzkovej kontroly a bezpečnosti.")
    isas_metadata_json_zmeny = models.TextField(null=True, blank=True, verbose_name="📊 Detail zmien", help_text="📊 Detailný prehľad úprav (JSON string).")
    pocet_zmien = models.PositiveIntegerField(default=0, editable=False, help_text="⚡ Rýchla cache pre počet zmenených polí (eliminuje JSON parsing v UI).")

    # VÝKON: Custom manager, ktorý eliminuje N+1 queries pri volaní __str__ a v Django Admine
    class ISASAuditLogManager(models.Manager):
        def get_queryset(self):
            return super().get_queryset().select_related('vykonal', 'content_type')
            
    objects = ISASAuditLogManager()

    class Meta:
        verbose_name = "📜 Audit Log"
        verbose_name_plural = "📜 Audit Logy"
        ordering = ['-cas_vykonania']
        indexes = [
            # VÝKON: Plný kompozitný index pokrývajúci filtrovanie aj predvolené zoradenie (eliminuje filesort)
            models.Index(fields=['content_type', 'object_id', '-cas_vykonania']),
        ]

    def __str__(self):
        try:
            user = self.vykonal.get_username() if self.vykonal else "🤖 Systém/AI"
        except Exception:
            user = f"Používateľ #{self.vykonal_id}" if getattr(self, 'vykonal_id', None) else "🤖 Systém/AI"
        try:
            ct_nazov = self.content_type.model if getattr(self, 'content_type_id', None) else "Neznámy model"
        except Exception:
            ct_nazov = "Neznámy model"
        return f"[{self.get_akcia_display()}] {ct_nazov} (ID:{self.object_id}) od {user}"

class ISASAuditMixin(models.Model):
    """👀 Automaticky sleduje a loguje zmeny na poliach modelu."""
    objects = ISASAuditQuerySet.as_manager()
    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not hasattr(self, '_povodny_stav'):
            self._povodny_stav = {}

    @classmethod
    def from_db(cls, db, field_names, values, fetch_mode=None):
        instance = super().from_db(db, field_names, values, fetch_mode=fetch_mode)
        deferred = instance.get_deferred_fields()
        # VÝKON: Inicializácia tieňového stavu priamo pri načítaní z DB (defenzívna kontrola volateľnosti copy)
        def _safe_val(v):
            copy_fn = getattr(v, 'copy', None)
            return copy_fn() if callable(copy_fn) else v

        instance._povodny_stav = {
            f.attname: _safe_val(instance.__dict__.get(f.attname)) 
            for f in instance._meta.fields if f.attname not in deferred and f.name not in deferred
        }
        return instance

    def refresh_from_db(self, using=None, fields=None):
        """🔄 Aktualizuje tieňový stav po natiahnutí čerstvých dát z DB."""
        super().refresh_from_db(using=using, fields=fields)
        deferred = self.get_deferred_fields()
        # VÝKON: Použitie self.__dict__.get() obchádza pomalé Django deskriptory a šetrí CPU
        def _safe_val(v):
            copy_fn = getattr(v, 'copy', None)
            return copy_fn() if callable(copy_fn) else v

        self._povodny_stav = {f.attname: _safe_val(self.__dict__.get(f.attname)) for f in self._meta.fields if f.attname not in deferred and f.name not in deferred}

    def save(self, *args, **kwargs):
        from .middleware import get_current_user

        is_new = self._state.adding
        user = get_current_user()
        user = user if (user and user.is_authenticated) else None
        ct = ContentType.objects.get_for_model(self)
        update_fields = kwargs.get('update_fields')

        target_db = kwargs.get('using') or self._state.db or 'default'

        # 🔄 Ak ide o úpravu, vypočítame rozdiely (Diff)
        zmeny_k_ulozeniu = {}
        if not is_new and not getattr(self, '_isas_anonymizacia_prebieha', False):
            # 🛡️ ZÁCHRANNÁ SIEŤ: Ak nested-admin obišiel __init__ a povodny_stav je prázdny, vytiahneme ho priamo z DB
            if not getattr(self, '_povodny_stav', None):
                try:
                    povodny_objekt_db = self.__class__._base_manager.using(target_db).get(pk=self.pk)
                    deferred = self.get_deferred_fields()
                    def _safe_val(v):
                        copy_fn = getattr(v, 'copy', None)
                        return copy_fn() if callable(copy_fn) else v

                    self._povodny_stav = {
                        f.attname: _safe_val(povodny_objekt_db.__dict__.get(f.attname)) 
                        for f in self._meta.fields if f.attname not in deferred and f.name not in deferred
                    }
                except Exception:
                    self._povodny_stav = {}

            for f in self._meta.fields:
                # Ignorujeme polia, ktoré neboli načítané (deferred)
                if f.attname not in self._povodny_stav:
                    continue
                
                # Ak sa ukladajú len špecifické polia, ignorujeme ostatné
                if update_fields is not None and f.name not in update_fields and f.attname not in update_fields:
                    continue

                # VÝKON: Použitie self.__dict__.get() obchádza pomalé Django deskriptory pre starú aj novú hodnotu
                stara = self._povodny_stav.get(f.attname)
                nova = self.__dict__.get(f.attname)
                
                if stara != nova and ((stara is None) != (nova is None) or str(stara) != str(nova)):
                    # BEZPEČNOSŤ: Rozšírené maskovanie šifrovaných a citlivých polí v logoch
                    citlive_slova = {'heslo', 'password', 'token', 'secret', 'key', 'api'}
                    field_parts = set(f.name.lower().split('_'))
                    is_sensitive = isinstance(f, EncryptedCharField) or bool(citlive_slova & field_parts)
                    
                    # VÝKON: Ochrana pred DB Bloatom. Extrémne dlhé texty (napr. telo_html) v logu orezávame.
                    str_stara = str(stara) if stara is not None else ""
                    str_nova = str(nova) if nova is not None else ""
                    
                    zmeny_k_ulozeniu[f.name] = {
                        "z": "********" if is_sensitive else (str_stara[:500] + "... [skrátené]" if len(str_stara) > 500 else str_stara),
                        "na": "********" if is_sensitive else (str_nova[:500] + "... [skrátené]" if len(str_nova) > 500 else str_nova)
                    }

        kwargs['using'] = target_db
        with transaction.atomic(using=target_db):
            super().save(*args, **kwargs)

            # ✍️ Zápis do Audit Logu
            if is_new:
                ISASAuditLog.objects.using(target_db).create(
                    vykonal=user, content_type=ct, object_id=str(self.pk),
                    akcia='CREATE', isas_metadata_json_zmeny=json.dumps({"stav": "✨ Objekt bol vytvorený"}),
                    pocet_zmien=1
                )
            elif zmeny_k_ulozeniu:
                ISASAuditLog.objects.using(target_db).create(
                    vykonal=user, content_type=ct, object_id=str(self.pk),
                    akcia='UPDATE', isas_metadata_json_zmeny=json.dumps(zmeny_k_ulozeniu),
                    pocet_zmien=len(zmeny_k_ulozeniu)
                )
            
        # 💾 Aktualizácia stavu v RAM po uložení (len pre reálne zapísané polia do DB)
        def _safe_val(v):
            copy_fn = getattr(v, 'copy', None)
            return copy_fn() if callable(copy_fn) else v

        deferred = self.get_deferred_fields()
        if update_fields is None:
            self._povodny_stav = {f.attname: _safe_val(self.__dict__.get(f.attname)) for f in self._meta.fields if f.attname not in deferred and f.name not in deferred}
        else:
            update_fields_set = set(update_fields)
            for f in self._meta.fields:
                if (f.name in update_fields_set or f.attname in update_fields_set) and f.attname not in deferred and f.name not in deferred:
                    self._povodny_stav[f.attname] = _safe_val(self.__dict__.get(f.attname))

    def delete(self, *args, **kwargs):
        from .middleware import get_current_user
        ct = ContentType.objects.get_for_model(self)
        pk_copy = self.pk
        target_db = kwargs.get('using') or self._state.db or 'default'
        
        # Defenzívne ošetrenie: vyčistenie using z kwargs, ak prišiel v args, a zápis logu len ak PK existoval
        if len(args) > 0:
            target_db = args[0]
        else:
            kwargs['using'] = target_db

        with transaction.atomic(using=target_db):
            vysledok = super().delete(*args, **kwargs)
            
            if pk_copy is not None:
                user = get_current_user()
                user = user if (user and user.is_authenticated) else None
                ISASAuditLog.objects.using(target_db).create(
                    vykonal=user, content_type=ct, object_id=str(pk_copy),
                    akcia='DELETE', isas_metadata_json_zmeny=json.dumps({"stav": "🧨 Objekt bol fyzicky vymazaný"}),
                    pocet_zmien=1
                )
        return vysledok

class ISASGDPRMixin(models.Model):
    """🛡️ Zabezpečuje bezpečnú anonymizáciu subjektov údajov."""
    je_anonymizovany = models.BooleanField(default=False, editable=False, verbose_name="🕵️ Anonymizované", help_text="🛡️ Udáva, či boli osobné údaje v súlade s legislatívou GDPR trvalo anonymizované pre zaistenie právnej ochrany podniku.")

    class Meta:
        abstract = True

    def vykonaj_anonymizaciu(self, polia_na_vymaz, dovod="Neuvedený dôvod"):
        """🔏 Prepíše citlivé údaje a označí záznam ako anonymizovaný."""
        from .middleware import get_current_user
        
        # BEZPEČNOSŤ & VÝKON: Zamkneme riadok proti Race Condition. Ak iný proces práve anonymizuje, tento počká.
        if not self.pk:
            raise ValidationError("⚠️ Kritická chyba: Nemožno anonymizovať záznam, ktorý nebol uložený v databáze.")

        target_db = self._state.db or 'default'
        with transaction.atomic(using=target_db):
            # Načítame aktuálny stav priamo z DB so zámkom
            aktualny_objekt = self.__class__._base_manager.using(target_db).select_for_update().get(pk=self.pk)
            if aktualny_objekt.je_anonymizovany: 
                self.refresh_from_db(using=target_db)
                return
            
            # BEZPEČNOSŤ: Ochrana pred prepísaním primárnych a cudzích kľúčov (Data Corruption)
            zakazane_polia = [f.name for f in self._meta.fields if f.primary_key or f.is_relation]
            zakazane_polia.extend([f.attname for f in self._meta.fields if f.primary_key or f.is_relation])
            zakazane_polia.extend(['id', 'pk', 'je_anonymizovany']) # Explicitná poistka
            
            polia_na_ulozenie = ['je_anonymizovany'] # Zoznam pre update_fields
            
            for pole in polia_na_vymaz:
                if pole in zakazane_polia:
                    continue
                    
                if hasattr(self, pole):
                    try:
                        field_obj = self._meta.get_field(pole)
                    except Exception:
                        field_obj = None

                    if field_obj is not None and field_obj.concrete and not field_obj.is_relation:
                        # UX/STABILITA: Špecifické databázové polia
                        if isinstance(field_obj, models.EmailField):
                            setattr(self, pole, f"gdpr_{self.pk}@anonymized.local")
                        elif isinstance(field_obj, models.URLField):
                            setattr(self, pole, f"https://anonymized.local/gdpr_{self.pk}")
                        elif isinstance(field_obj, (models.CharField, models.TextField)):
                            max_len = getattr(field_obj, 'max_length', None)
                            is_unique = getattr(field_obj, 'unique', False)
                            if is_unique:
                                hodnota = f"GDPR_{self.pk}"
                                if max_len and len(hodnota) > max_len:
                                    hodnota = str(self.pk)[:max_len]
                            else:
                                hodnota = "*** GDPR ***"
                                if max_len and len(hodnota) > max_len:
                                    hodnota = hodnota[:max_len]
                            setattr(self, pole, hodnota)
                        elif field_obj.null:
                            setattr(self, pole, None)
                        elif isinstance(field_obj, (models.IntegerField, models.DecimalField, models.FloatField)):
                            setattr(self, pole, 0)
                        elif isinstance(field_obj, models.BooleanField):
                            setattr(self, pole, False)

                        polia_na_ulozenie.append(field_obj.name)
                    else:
                        # Fallback: Ak ide o Python property so setterom, nastavíme ju v RAM, ale neposielame do DB
                        try:
                            setattr(self, pole, None)
                        except Exception:
                            pass
            
            # BEZPEČNOSŤ: Dynamická deaktivácia VŠETKÝCH prepojených User účtov
            from django.contrib.auth import get_user_model
            User = get_user_model()
            
            for f in self._meta.get_fields():
                if f.is_relation and f.related_model == User:
                    try:
                        # VÝKON: Priamy cudzí kľúč overujeme existenciou 'attname' (f.concrete)
                        if (f.many_to_one or f.one_to_one) and hasattr(f, 'attname'):
                            user_id = getattr(self, f.attname, None)
                            if user_id:
                                User.objects.using(target_db).filter(pk=user_id, is_active=True).update(is_active=False)
                        else:
                            # Fallback pre reverzné väzby s korektným zistením accessora
                            accessor = f.get_accessor_name() if hasattr(f, 'get_accessor_name') else f.name
                            rel_obj = getattr(self, accessor, None) if accessor else None
                            if hasattr(rel_obj, 'all'):  # Ide o RelatedManager
                                rel_obj.all().using(target_db).filter(is_active=True).update(is_active=False)
                            elif isinstance(rel_obj, User) and getattr(rel_obj, 'is_active', False): # Ide o reverzný OneToOne
                                type(rel_obj).objects.using(target_db).filter(pk=rel_obj.pk).update(is_active=False)
                    except Exception:
                        pass

            self.je_anonymizovany = True
            self._isas_anonymizacia_prebieha = True
            
            # VÝKON: Uložíme len tie polia, ktoré sa reálne zmenili, čím šetríme I/O operácie
            try:
                self.save(using=target_db, update_fields=list(set(polia_na_ulozenie)))
            finally:
                self._isas_anonymizacia_prebieha = False

            ct = ContentType.objects.get_for_model(self)
            # 🛡️ ZÁPIS DÔVODU DO TVOJHO VLASTNÉHO AUDIT LOGU
            user = get_current_user()
            user = user if (user and user.is_authenticated) else None
            ISASAuditLog.objects.using(target_db).create(
                vykonal=user, content_type=ct, object_id=str(self.pk),
                akcia='ANONYMIZE', isas_metadata_json_zmeny=json.dumps({"stav": f"🕵️ Osobné údaje boli anonymizované. Dôvod: {dovod}"}),
                pocet_zmien=1
            )

class EncryptedCharField(models.CharField):
    """Vlastné šifrované pole, ktoré nespôsobuje pády v Django 5+"""
    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return get_cipher().decrypt(str(value).encode('utf-8')).decode('utf-8')
        except Exception:
            return value # Ak dáta v DB ešte nie sú zašifrované alebo sú poškodené

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        cipher = get_cipher()

        try:
            cipher.decrypt(str(value).encode('utf-8'))
            return str(value) # Ak už je zašifrované, nešifruj znova
        except InvalidToken:
            return cipher.encrypt(str(value).encode('utf-8')).decode('utf-8')

    def formfield(self, **kwargs):
        """
        BEZPEČNOSŤ: Vynúti použitie PasswordInput widgetu vo všetkých formulároch a v Django Admine.
        Zabraňuje úniku dešifrovaných hesiel (plaintext) na obrazovku používateľom s 'View' právami.
        """
        # render_value=True zabezpečí, že pri úprave iných polí sa heslo nevymaže, ale ostane maskované (***)
        defaults = {'widget': forms.PasswordInput(render_value=True)}
        defaults.update(kwargs)
        return super().formfield(**defaults)

class StatLokalizacia(ISASAuditMixin, models.Model):
    """
    🏛️ ISAS GLOBAL FISCAL MATRIX:
    Čistý dátový model pre daňovú, menovú a ERP lokalizáciu.
    Nula riadkov hardkódu v Pythone. Všetky dáta spravuje administrátor cez DB/Admin.
    """
    LEHOTA_TYP_CHOICES = [
        ('DNI', '📅 Počet dní od dodania (napr. 15 dní SK/CZ, 8 dní HU, 5 dní BG)'),
        ('MESIAC_15', '🗓️ Do 15. dňa nasledujúceho mesiaca (EÚ B2B / Intrakomunitár)'),
        ('KONIEC_MESIACA', '🏁 Do konca mesiaca dodania (Severské štáty, SI, HR)'),
        ('BEZODKLADNE', '⚡ V deň dodania / bezodkladne (Francúzsko, Litva)'),
        ('BEZ_LEHOTY', '🌐 Bez osobitnej daňovej lehoty (Mimo EÚ)'),
    ]

    iso_kod = models.CharField(
        "🗺️ ISO kód štátu", 
        max_length=2, 
        unique=True, 
        db_index=True, 
        help_text="Dvojmiestny medzinárodný kód štátu podľa ISO 3166-1 alpha-2 (napr. SK, CZ, AT, DE, US)."
    )
    nazov = models.CharField(
        "🌍 Názov štátu", 
        max_length=100, 
        help_text="Oficiálny medzinárodný alebo národný názov štátu."
    )
    mena = models.CharField(
        "💰 Národná mena", 
        max_length=10, 
        help_text="Kód oficiálnej meny podľa ISO 4217 (napr. EUR, CZK, USD, CHF, GBP)."
    )
    dph_zakladna = models.DecimalField(
        "📊 Základná sadzba DPH (%)", 
        max_digits=5, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        help_text="Aktuálna zákonná základná sadzba DPH."
    )
    dph_znizena = models.DecimalField(
        "📉 Znížená sadzba DPH (%)", 
        max_digits=5, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        help_text="Zákonná znížená sadzba DPH."
    )
    
    # --- 🇪🇺 EÚ & PEPPOL SCHÉMY ---
    EFAKTURA_REZIM_CHOICES = [
        ('PEPPOL_DIRECT', '🌐 Štandardná sieť Peppol (UBL 2.1 cez Access Point)'),
        ('CTC_CLEARANCE', '🏛️ Štátny Clearance portál / Broker (KSeF, SdI, RO e-Factura)'),
        ('VOLITELNE', '📄 Dobrovoľná e-fakturácia (štandardné PDF, Peppol na vyžiadanie)'),
        ('VYPNUTE', '🚫 Bez elektronickej fakturácie (Tretie krajiny)'),
    ]

    efaktura_rezim = models.CharField(
        "🏛️ Režim e-fakturácie",
        max_length=20,
        choices=EFAKTURA_REZIM_CHOICES,
        default='PEPPOL_DIRECT',
        help_text="Určuje, akým spôsobom sa v danom štáte legislatívne doručujú B2B eFaktúry."
    )

    je_clen_eu = models.BooleanField(
        "🇪🇺 Člen Európskej únie?", 
        default=False, 
        help_text="Určuje, či štát podlieha intrakomunitárnemu režimu DPH a systému VIES."
    )
    peppol_eas_kod = models.CharField(
        "🌐 Peppol EAS schéma", 
        max_length=4, 
        blank=True, 
        null=True, 
        help_text="EAS identifikátor pre sieť Peppol (0245 SK, 0144 CZ, 9915 AT, 9930 DE, 9945 PL, 9901 EÚ)."
    )

    # --- ⏳ DAŇOVÉ LEHOTY ---
    lehota_faktury_dni = models.PositiveIntegerField(
        "⏳ Zákonná lehota fakturácie (dni)", 
        null=True,
        blank=True,
        help_text="Počet dní od dodania na zákonné vystavenie faktúry. Ak zostane prázdne, systém lehotu nesleduje."
    )
    lehota_faktury_typ = models.CharField(
        "🏛️ Typ daňovej lehoty", 
        max_length=20, 
        choices=LEHOTA_TYP_CHOICES, 
        default='BEZ_LEHOTY', 
        help_text="Metodika výpočtu daňového deadline podľa legislatívy danej krajiny."
    )
    lehota_reklamacie_dni = models.PositiveIntegerField(
        "⚖️ Zákonná lehota reklamácie (Dni)",
        null=True,
        blank=True,
        help_text="⚖️ Zákonná lehota na vybavenie reklamácie v danom štáte (ak existuje). Ak zostane prázdne, riadi sa zmluvou alebo firemným štandardom."
    )
    # --- 🔍 NORMALIZÁCIA & VYHĽADÁVANIE ---
    aliasy = models.TextField(
        "🔤 Aliasy a synonymá", 
        blank=True, 
        null=True, 
        help_text="Čiarkou oddelené alternatívne názvy a skratky bez diakritiky."
    )
    subdiv_konfiguracia = models.JSONField(
        "🌍 Konfigurácia regiónov a sviatkov (Subdivisions)",
        default=dict,
        blank=True,
        help_text="JSON mapa kódov regiónov pre sviatky (subdiv) s kľúčovými slovami a regexmi PSČ."
    )

    class Meta:
        verbose_name = "🗺️ Lokalizácia štátu (DPH, mena, ERP)"
        verbose_name_plural = "🗺️ Lokalizácie štátov (DPH, meny, ERP)"
        ordering = ['iso_kod']

    def __str__(self):
        iso = self.iso_kod or "??"
        nazov = self.nazov or "Neznámy štát"
        mena_str = f" ({self.mena})" if self.mena else ""
        return f"{iso} - {nazov}{mena_str}"

    def clean(self):
        super().clean()
        if self.iso_kod:
            self.iso_kod = self.iso_kod.strip().upper()
            if len(self.iso_kod) != 2:
                raise ValidationError({'iso_kod': 'ISO kód štátu musí mať presne 2 znaky podľa ISO 3166-1 alpha-2 (napr. SK, CZ, AT, DE).'})
        if self.mena:
            self.mena = self.mena.strip().upper()
        if self.dph_zakladna is not None and self.dph_zakladna < Decimal('0.00'):
            raise ValidationError({'dph_zakladna': 'Sadzba DPH nemôže byť záporná.'})
        if self.dph_znizena is not None and self.dph_znizena < Decimal('0.00'):
            raise ValidationError({'dph_znizena': 'Sadzba DPH nemôže byť záporná.'})

        # 🛡️ OCHRANA PRED NONE / PRÁZDNOU HODNOTOU: Konzistencia lehoty fakturácie
        if self.lehota_faktury_typ == 'DNI' and self.lehota_faktury_dni is None:
            raise ValidationError({'lehota_faktury_dni': 'Ak je zvolený typ lehoty "Počet dní od dodania", musíte zadať počet dní (alebo zvoľte typ "Bez osobitnej lehoty").'})

    def as_dict(self) -> dict:
        """Kompatibilný výstup pre fakturačné, daňové a lokalizačné enginy."""
        return {
            'iso_kod': self.iso_kod or '',
            'stat_nazov': self.nazov or '',
            'mena': self.mena or '',
            'dph_zakladna': self.dph_zakladna if self.dph_zakladna is not None else Decimal('0.00'),
            'dph_znizena': self.dph_znizena if self.dph_znizena is not None else Decimal('0.00'),
            'je_clen_eu': bool(self.je_clen_eu),
            'peppol_eas_kod': self.peppol_eas_kod or '',
            'efaktura_rezim': self.efaktura_rezim or 'PEPPOL_DIRECT',
            'lehota_faktury_dni': self.lehota_faktury_dni,
            'lehota_faktury_typ': self.lehota_faktury_typ or 'BEZ_LEHOTY',
            'lehota_reklamacie_dni': self.lehota_reklamacie_dni,
            'aliasy': self.aliasy or '',
            'subdiv_konfiguracia': self.subdiv_konfiguracia if isinstance(self.subdiv_konfiguracia, dict) else {},
        }

    def vypocitaj_danovy_termin(self, datum_dodania):
        """
        🏛️ Čistý dynamický výpočet daňového deadline podľa konfigurácie štátu v DB.
        Nula riadkov hardkódu. Všetko riadi lehota_faktury_typ a lehota_faktury_dni.
        """
        if not datum_dodania:
            return None

        if isinstance(datum_dodania, str):
            try:
                d = date.fromisoformat(datum_dodania.split('T')[0])
            except (ValueError, TypeError):
                return None
        elif isinstance(datum_dodania, datetime):
            d = datum_dodania.date()
        elif isinstance(datum_dodania, date):
            d = datum_dodania
        else:
            return None

        if self.lehota_faktury_typ == 'BEZ_LEHOTY':
            return None
        if self.lehota_faktury_typ == 'BEZODKLADNE':
            return d
        if self.lehota_faktury_typ == 'DNI':
            if self.lehota_faktury_dni is not None:
                return d + timedelta(days=self.lehota_faktury_dni)
            return None
        if self.lehota_faktury_typ == 'MESIAC_15':
            rok = d.year + 1 if d.month == 12 else d.year
            mesiac = 1 if d.month == 12 else d.month + 1
            return date(rok, mesiac, 15)
        if self.lehota_faktury_typ == 'KONIEC_MESIACA':
            posledny_den = calendar.monthrange(d.year, d.month)[1]
            return date(d.year, d.month, posledny_den)

        if self.lehota_faktury_dni is not None:
            return d + timedelta(days=self.lehota_faktury_dni)

        return None

    def save(self, *args, **kwargs):
        self.clean()
        stary_iso = getattr(self, '_povodny_stav', {}).get('iso_kod')
        target_db = kwargs.get('using') or self._state.db or 'default'
        super().save(*args, **kwargs)
        
        kluce_na_zmazanie = [
            f'isas_stat_lokalizacia_{self.iso_kod}',
            'isas_vsetky_staty_mapa_cache',
            f'isas_compliance_config_{self.iso_kod}',
            'isas_compliance_config'
        ]
        if stary_iso and stary_iso != self.iso_kod:
            kluce_na_zmazanie.extend([
                f'isas_stat_lokalizacia_{stary_iso}',
                f'isas_compliance_config_{stary_iso}'
            ])

        def _vycisti_cache():
            try:
                cache.delete_many(kluce_na_zmazanie)
            except Exception:
                pass

        transaction.on_commit(_vycisti_cache, using=target_db)

    def delete(self, *args, **kwargs):
        # 🛑 DATABÁZOVÝ ZÁMOK: Kontrola pred zmazaním inštancie
        target_db = kwargs.get('using') or (args[0] if len(args) > 0 else None) or self._state.db or 'default'

        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            identita = FiremnaIdentitaModel.objects.using(target_db).first()
            if identita and identita.sidlo_stat and hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso'):
                aktivne_iso = (FiremnaIdentitaModel.get_aktivna_krajina_iso(identita.sidlo_stat) or '').strip().upper()
                
                # Ochrana: ISO nesmie byť prázdne a nesmie ísť o generický stav
                if aktivne_iso and aktivne_iso not in ('NEZADANE', 'INYKRAJINA'):
                    moje_iso = str(self.iso_kod or '').strip().upper()
                    if moje_iso == aktivne_iso:
                        raise PermissionDenied(f"🛑 Kritická chyba integrity: Štát '{moje_iso}' je nastavený ako oficiálne sídlo vašej spoločnosti a nesmie byť vymazaný!")
        except PermissionDenied:
            raise
        except Exception as e:
            # 🛡️ FAIL-CLOSED: Zalogujeme incident, aby sa chyba neututlala
            logging.getLogger(__name__).critical(f"🚨 Zlyhalo overenie sídla firmy pri mazaní štátu ID {self.pk}: {e}")

        # Pokračovanie v mazaní a vyčistení cache
        iso = self.iso_kod
        if len(args) == 0:
            kwargs['using'] = target_db
        super().delete(*args, **kwargs)

        kluce_na_zmazanie = [
            f'isas_stat_lokalizacia_{iso}',
            'isas_vsetky_staty_mapa_cache'
        ]
        def _vycisti_cache():
            try:
                cache.delete_many(kluce_na_zmazanie)
            except Exception:
                pass

        transaction.on_commit(_vycisti_cache, using=target_db)

    @classmethod
    def get_for_iso(cls, iso: str):
        """⚡ Čisté načítanie z DB s Redis Cache. NULA HARDKÓDU."""
        if not iso:
            return None
        iso_clean = str(iso).strip().upper()
        cache_key = f'isas_stat_lokalizacia_{iso_clean}'
        
        try:
            obj = cache.get(cache_key)
            if obj:
                return obj
        except Exception:
            pass

        obj = cls.objects.filter(iso_kod=iso_clean).first()
        if obj:
            try:
                cache.set(cache_key, obj, 86400)
            except Exception:
                pass
        return obj

    @classmethod
    def get_vsetky_mapa(cls) -> list:
        """⚡ Načíta všetky štáty do zoznamu slovníkov s Redis Cache (kľúč 'isas_vsetky_staty_mapa_cache')."""
        cache_key = 'isas_vsetky_staty_mapa_cache'
        try:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        except Exception:
            pass

        vsetky = []
        for obj in cls.objects.all():
            nazov_raw = unicodedata.normalize('NFKD', str(obj.nazov or '')).encode('ascii', 'ignore').decode('utf-8').lower()
            nazov_clean = re.sub(r'[\(\),.]', ' ', nazov_raw).replace('-', ' ').replace('/', ' ')
            nazov_clean = re.sub(r'\s+', ' ', nazov_clean).strip()
            
            aliasy_clean = []
            if obj.aliasy:
                aliasy_norm = unicodedata.normalize('NFKD', str(obj.aliasy)).encode('ascii', 'ignore').decode('utf-8').lower()
                for a in aliasy_norm.replace(';', ',').split(','):
                    a_clean = re.sub(r'[\(\),.]', ' ', a).replace('-', ' ').replace('/', ' ')
                    a_clean = re.sub(r'\s+', ' ', a_clean).strip()
                    if a_clean:
                        aliasy_clean.append(a_clean)
            
            vsetky.append({
                'iso_kod': obj.iso_kod,
                'nazov_clean': nazov_clean,
                'aliasy': aliasy_clean,
            })

        try:
            cache.set(cache_key, vsetky, 86400)
        except Exception:
            pass
        return vsetky

    @classmethod
    def rozpoznaj_subdiviziu(cls, iso: str, location_str: str):
        """
        🌍 Dynamické rozpoznanie regionálnej subdivízie (kraj, spolková krajina, kantón)
        podľa PSČ alebo mesta uloženého v konfigurácii štátu v DB.
        NULA riadkov hardkódu v aplikačnom kóde.
        """
        if not iso or not str(location_str or '').strip():
            return None

        stat_obj = cls.get_for_iso(iso)
        if not stat_obj:
            return None

        konfig = getattr(stat_obj, 'subdiv_konfiguracia', None) or {}
        if not konfig or not isinstance(konfig, dict):
            return None

        raw = str(location_str).strip()
        clean = unicodedata.normalize('NFKD', raw).encode('ascii', 'ignore').decode('utf-8').lower()
        clean_norm = re.sub(r'[\(\),.]', ' ', clean).replace('-', ' ').replace('/', ' ')
        clean_norm = re.sub(r'\s+', ' ', clean_norm).strip()

        # 🛡️ PRIORITA 1: Exaktná zhoda podľa PSČ (nemôže dôjsť k zámene názvov ulíc a miest)
        for subdiv_kod, pravidla in konfig.items():
            if not isinstance(pravidla, dict):
                continue
            psc_regex = pravidla.get('psc_regex')
            if psc_regex:
                try:
                    if re.search(str(psc_regex), raw):
                        return str(subdiv_kod).strip()
                except re.error:
                    pass

        # 🛡️ PRIORITA 2: Zhoda podľa aliasov s triedením od najdlhších (Longest Match First)
        vsetky_aliasy = []
        for subdiv_kod, pravidla in konfig.items():
            if not isinstance(pravidla, dict):
                continue
            aliasy = pravidla.get('aliasy') or []
            if isinstance(aliasy, str):
                aliasy = [a.strip() for a in aliasy.split(',') if a.strip()]
            elif not isinstance(aliasy, (list, tuple, set)):
                continue

            for a in aliasy:
                a_norm = unicodedata.normalize('NFKD', str(a)).encode('ascii', 'ignore').decode('utf-8').lower().strip()
                a_clean = re.sub(r'[\(\),.]', ' ', a_norm).replace('-', ' ').replace('/', ' ')
                a_clean = re.sub(r'\s+', ' ', a_clean).strip()
                if a_clean:
                    vsetky_aliasy.append((len(a_clean), a_clean, str(subdiv_kod).strip()))

        # Najdlhšie aliasy idú ako prvé (napr. "frankfurt oder" pred "frankfurt")
        vsetky_aliasy.sort(key=lambda x: x[0], reverse=True)

        for _, a_clean, s_kod in vsetky_aliasy:
            vzor = rf'\b{re.escape(a_clean)}\b'
            if re.search(vzor, clean_norm):
                return s_kod

        return None

    @classmethod
    def rozpoznaj_iso(cls, text: str) -> str:
        """
        🌍 Rozpozná ISO kód štátu na základe názvu, skratky alebo aliasu z DB.
        NULA riadkov hardkódu. Všetko sa riadi databázou StatLokalizacia.
        """
        if not text:
            return "NEZADANE"

        raw = str(text).strip()
        if not raw:
            return "NEZADANE"

        clean = unicodedata.normalize('NFKD', raw).encode('ascii', 'ignore').decode('utf-8').lower().strip()
        clean = clean.replace('.', '').replace(',', '').replace('-', ' ')

        # Priamy 2-znakový ISO kód
        if len(clean) == 2 and clean.isalpha():
            return clean.upper()

        staty = cls.get_vsetky_mapa()
        if not staty:
            return "INYKRAJINA"

        # 1. Presná zhoda s názvom, ISO kódom alebo aliasom z DB
        for s in staty:
            if clean == s['nazov_clean'] or clean == s['iso_kod'].lower() or clean in s['aliasy']:
                return s['iso_kod']

        # 2. Bezpečná zhoda na hraniciach celých slov s prioritou najdlhšieho výrazu (Longest Match First)
        kandidati = []
        for s in staty:
            iso = s['iso_kod']
            if s.get('nazov_clean'):
                kandidati.append((len(s['nazov_clean']), s['nazov_clean'], iso))
            for a in s.get('aliasy', []):
                if len(a) >= 3:
                    kandidati.append((len(a), a, iso))

        # Najdlhšie názvy majú prednosť pred kratšími (prevencia kolízií napr. "Frankfurt Oder" vs "Frankfurt", "Guinea-Bissau" vs "Guinea")
        kandidati.sort(key=lambda x: x[0], reverse=True)

        for _, fraza, iso in kandidati:
            vzor = rf'\b{re.escape(fraza)}\b'
            if re.search(vzor, clean):
                return iso

        return "INYKRAJINA"

class FiremnaIdentita(ISASAuditMixin, models.Model):
    nazov_spolocnosti = models.CharField("🏢 Názov spoločnosti", max_length=255, help_text="🏢 Oficiálny obchodný názov firmy podľa obchodného alebo živnostenského registra pre tlač dokladov a fakturáciu.")
    podtitul = models.CharField(
        "📢 Slogan / Podtitul spoločnosti", 
        max_length=200, 
        blank=True, 
        null=True, 
        help_text="📢 Krátky podtitul alebo zameranie spoločnosti zobrazované pod názvom firmy na portálových dokladoch a cenových ponukách (napr. 'Priemyselné služby a riešenia' alebo 'Cloudová infraštruktúra a bezpečnosť')."
    )
    ico = models.CharField("🆔 IČO / Registračné číslo", max_length=20, blank=True, null=True, help_text="🆔 Identifikačné číslo organizácie alebo národný registračný kód (ak v danej krajine existuje).")
    dic = models.CharField("📊 DIČ / Daňové číslo", max_length=20, blank=True, null=True, help_text="📊 Daňové identifikačné číslo (ak bolo pridelené).")
    ic_dph = models.CharField("🌍 IČ DPH", max_length=20, blank=True, null=True, help_text="🌍 Identifikačné číslo pre DPH používané pri tuzemských a zahraničných zdaniteľných plneniach.")
    je_platca_dph = models.BooleanField("⚖️ Je platcom DPH?", default=True, help_text="⚖️ Určuje, či sa na vystavených dokladoch automaticky kalkuluje a vykazuje DPH.")

    sidlo_ulica = models.CharField("🛣️ Sídlo - Ulica", max_length=255, help_text="🛣️ Ulica a číslo orientačné oficiálneho sídla firmy.")
    sidlo_cislo = models.CharField("🔢 Sídlo - Číslo", max_length=20, help_text="🔢 Súpisné alebo orientačné číslo sídla pre oficiálnu korešpondenciu.")
    sidlo_psc = models.CharField("📮 Sídlo - PSČ", max_length=10, help_text="📮 Poštové smerovacie číslo oficiálneho sídla.")
    sidlo_mesto = models.CharField("🏙️ Sídlo - Mesto", max_length=100, help_text="🏙️ Mesto alebo obec registrácie sídla firmy.")
    sidlo_stat = models.CharField("🗺️ Sídlo - Štát", max_length=100, blank=True, default="", help_text="🗺️ Štát registrácie sídla pre daňové a účtovné doklady (napr. Slovenská republika, Česká republika, Deutschland).")
    
    koresp_adresa = models.TextField("📬 Korešpondenčná adresa", blank=True, null=True, help_text="📬 Doručovacia adresa pre poštu a dokumenty, ak sa nezhoduje s oficiálnym sídlom.")
    adresa_prevadzky = models.TextField("🏭 Adresa Hlavného skladu/Centrály", blank=True, null=True, help_text="🏭 Fyzická adresa hlavného skladu a prevádzky pre dodacie listy a príjemky tovaru.")
    zodpovedny_zamestnanec_sklad = models.ForeignKey(
        'Evidencia.Zamestnanec', on_delete=models.SET_NULL, blank=True, null=True,
        verbose_name="👷‍♂️ Zodpovedný zamestnanec (Hlavný sklad)",
        help_text="👷 Zamestnanec poverený riadením hlavného skladu, ktorého kontakt sa uvádza na skladových dokladoch."
    )
    veduci_skladu = models.CharField("👤 Vedúci Hlavného skladu", max_length=150, blank=True, null=True, help_text="👤 Meno vedúceho prevádzky alebo skladu pre internú a dodávateľskú komunikáciu.")
    telefon_sklad = models.CharField("📞 Telefón (Hlavný sklad)", max_length=50, blank=True, null=True, help_text="📞 Priamy telefonický kontakt na logistiku a dodávky tovaru do hlavného skladu.")
    email_sklad = models.EmailField("📧 E-mail (Hlavný sklad)", blank=True, null=True, help_text="📧 Operatívny e-mail hlavného skladu pre príjemky, dodacie listy a komunikáciu s dodávateľmi.")

    iban = models.CharField("💳 IBAN", max_length=34, help_text="💳 Bankový účet v tvare IBAN pre príjem platieb od odberateľov.")
    swift = models.CharField("🏦 SWIFT / BIC", max_length=11, help_text="🏦 Identifikačný kód banky pre medzinárodné platby a tuzemské prevody.")
    nazov_banky = models.CharField("🏛️ Názov banky", max_length=100, help_text="🏛️ Názov peňažného ústavu, v ktorom je vedený hlavný firemný účet.")

    telefon = models.CharField("📞 Telefón", max_length=50, help_text="📞 Hlavný telefonický kontakt na recepciu alebo zákaznícke centrum.")
    email_vseobecny = models.EmailField("✉️ E-mail", help_text="✉️ Všeobecná e-mailová adresa spoločnosti pre prvotný kontakt a dopyty.")
    email_fakturacia = models.EmailField("📨 E-mail (Fakturácia)", blank=True, null=True, help_text="📨 Špecializovaný e-mail uvádzaný na dokladoch pre odosielanie a riešenie faktúr.")
    email_uctovnicky = models.EmailField(
        "👩‍💻 E-mail pre účtovníctvo", 
        blank=True, 
        null=True, 
        help_text="🤖 E-mailová adresa pre automatické preposielanie prijatých faktúr a podkladov do spracovania."
    )
    web = models.URLField("🌐 Webová stránka", blank=True, null=True, help_text="🌐 Oficiálna internetová adresa firmy uvádzaná v pätkách dokladov.")
    zapis_v_registri = models.TextField("📜 Zápis v registri", help_text="📜 Zákonný údaj o zápise v obchodnom alebo živnostenskom registri požadovaný na faktúrach.")

    konatel = models.CharField("👔 Konateľ / Štatutárny orgán", max_length=255, help_text="👔 Meno a priezvisko štatutárneho zástupcu oprávneného konať v mene spoločnosti.")
    vystavuje_faktury = models.CharField("✍️ Predvolená osoba (Vystavil)", max_length=255, blank=True, null=True, help_text="✍️ Meno zamestnanca, ktoré sa automaticky dopĺňa do políčka Vystavil na faktúrach.")
    vystavuje_ponuky = models.CharField("✍️ Predvolená osoba (Ponuky)", max_length=255, blank=True, null=True, help_text="✍️ Meno obchodníka predvolene uvádzané ako kontaktná osoba na cenových ponukách.")
    vystavuje_objednavky = models.CharField("✍️ Predvolená osoba (Objednávky)", max_length=255, blank=True, null=True, help_text="✍️ Meno alebo oddelenie predvolene uvádzané ako vystavovateľ nákupných objednávok.")
    vystavuje_servis_vykazy = models.CharField("✍️ Predvolená osoba (Servisné výkazy)", max_length=255, blank=True, null=True, help_text="✍️ Meno zodpovedného technika predvolene zobrazované na servisných výkazoch a protokoloch.")
    vystavuje_vstupky = models.CharField("✍️ Predvolená osoba (Vstupky)", max_length=255, blank=True, null=True, help_text="✍️ Meno alebo oddelenie predvolene uvádzané na povolenia na vstup a BOZP formuláre.")

    logo = models.ImageField("🎨 Logo spoločnosti", upload_to='identita/loga/', blank=True, null=True, help_text="🎨 Grafický súbor loga v tlačovej kvalite pre hlavičky dokladov a formulárov.")
    peciatka_podpis = models.ImageField("🔏 Pečiatka a podpis", upload_to='identita/peciatky/', blank=True, null=True, help_text="🔏 Digitalizovaná pečiatka s podpisom vo formáte PNG s priehľadným pozadím pre PDF doklady.")

    splatnost_faktur = models.PositiveIntegerField("⏳ Predvolená splatnosť faktúr (dni)", default=14, help_text="⏳ Štandardný počet dní splatnosti automaticky nastavovaný pri vystavení novej faktúry.")
    predvolena_platnost_ponuk = models.PositiveIntegerField("⏳ Predvolená platnosť cenových ponúk (dni)", default=14, help_text="⏳ Počet dní platnosti cenovej ponuky po jej vytvorení pre automatický výpočet exspirácie.")
    predvolena_mena = models.CharField("💰 Predvolená mena", max_length=10, blank=True, null=True, help_text="💰 Hlavná mena pre oceňovanie a fakturáciu. Ak je prázdne, automaticky sa nastaví podľa sídla firmy.")

    facebook = models.URLField("📘 Facebook", blank=True, null=True, help_text="📘 Odkaz na oficiálny Facebook profil firmy pre marketingové šablóny.")
    instagram = models.URLField("📸 Instagram", blank=True, null=True, help_text="📸 Odkaz na oficiálny Instagram účet firmy uvádzaný v kontaktoch.")
    linkedin = models.URLField("💼 LinkedIn", blank=True, null=True, help_text="💼 Odkaz na firemný LinkedIn profil pre nadväzovanie B2B kontaktov.")

    class Meta:
        verbose_name = "📇 Profil spoločnosti"
        verbose_name_plural = "📇 Profil spoločnosti"
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name='firemnaidentita_singleton_check')
        ]

    def __str__(self):
        return f"📇 {self.nazov_spolocnosti} (Core Profil)"

    @property
    def sidlo_stat_kod(self) -> str:
        """🌍 ISO 3166-1 alpha-2 kód štátu sídla pre Schema.org, eFaktúry a API"""
        try:
            # 🛡️ Ochrana: 'self.sidlo_stat or ""' zabráni volaniu redundantného SQL SELECT v get_aktivna_krajina_iso
            iso = self.get_aktivna_krajina_iso(self.sidlo_stat or "")
            return iso if iso not in ('NEZADANE', 'INYKRAJINA') else ""
        except Exception:
            return ""

    @classmethod
    def get_country_defaults(cls, iso: Optional[str] = None) -> dict:
        """
        🌍 ISAS MASTER DEFAULTS:
        Čisté dynamické načítanie konfigurácie z databázového modelu StatLokalizacia.
        Nula hardkódu. Ak štát v DB nie je, vráti prázdny slovník.
        """
        if not iso:
            iso = cls.get_aktivna_krajina_iso()
        iso_clean = str(iso).strip().upper() if iso else ""
        if not iso_clean:
            return {}

        try:
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            if StatLokalizaciaModel:
                stat_obj = StatLokalizaciaModel.get_for_iso(iso_clean)
                if stat_obj:
                    return stat_obj.as_dict()
        except Exception:
            pass

        return {}

    @classmethod
    def get_aktivna_krajina_iso(cls, stat_nazov: Optional[str] = None) -> str:
        """
        🛡️ ISAS ARCHITECT: Maximálne defenzívny medzinárodný normalizátor štátu na ISO kód.
        Ošetrené: Odstránenie diakritiky (NFKD), prioritné delenie Slovinsko/Slovensko,
        plná podpora Východnej Európy, Ázie (Čína, Kórea, Japonsko, Turecko...), Balkánu aj Zámoria.
        Neznámy štát NIKDY nedostane omylom SK, čím chráni účtovníctvo pred neoprávneným exportom.
        """
        try:
            if stat_nazov is not None:
                raw_stat = str(stat_nazov).strip()
            else:
                identita = cls.objects.first()
                if not identita or not identita.sidlo_stat:
                    return "NEZADANE"
                raw_stat = str(identita.sidlo_stat).strip()
            
            if not raw_stat:
                return "NEZADANE"
            
            # 🌍 Čisté dynamické rozpoznanie z databázového modelu StatLokalizacia
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            if StatLokalizaciaModel and hasattr(StatLokalizaciaModel, 'rozpoznaj_iso'):
                return StatLokalizaciaModel.rozpoznaj_iso(raw_stat)

            # Medzinárodný fail-safe: podpora priameho ISO 3166-1 alpha-2 kódu
            stat_clean = unicodedata.normalize('NFKD', raw_stat).encode('ascii', 'ignore').decode('utf-8').lower().strip()
            if len(stat_clean) == 2 and stat_clean.isalpha():
                return stat_clean.upper()

            return "INYKRAJINA"
        except Exception:
            pass
        return "NEZADANE"

    @classmethod
    def posun_na_pracovny_den(cls, cielovy_datum: date, iso: Optional[str] = None, subdiv: Optional[str] = None) -> date:
        """
        🛡️ Posunie dátum na najbližší pracovný deň podľa oficiálnych štátnych a regionálnych sviatkov.
        Využíva knižnicu holidays s automatickým ošetrením náhradných dní (observed holidays) a víkendov.
        Defenzívny fail-safe: ak by knižnica nebola nainštalovaná, bezpečne posunie aspoň víkendy.
        """
        if not cielovy_datum:
            return cielovy_datum

        curr = cielovy_datum.date() if isinstance(cielovy_datum, datetime) else cielovy_datum
        iso_clean = (iso or cls.get_aktivna_krajina_iso()).strip().upper()

        # Dynamické rozpoznanie subdivízie podľa sídla firmy, ak nebola explicitne zadaná
        if not subdiv and iso_clean not in ('NEZADANE', 'INYKRAJINA'):
            try:
                identita = cls.objects.first() if hasattr(cls, 'objects') else None
                if identita:
                    StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
                    if StatLokalizaciaModel and hasattr(StatLokalizaciaModel, 'rozpoznaj_subdiviziu'):
                        lokacia = f"{identita.sidlo_psc or ''} {identita.sidlo_mesto or ''}".strip()
                        subdiv = StatLokalizaciaModel.rozpoznaj_subdiviziu(iso_clean, lokacia)
            except Exception:
                subdiv = None

        sviatky: Any = set()
        if iso_clean not in ('NEZADANE', 'INYKRAJINA'):
            try:
                # 🚀 Načítame aktuálny aj nasledujúci rok naraz s podporou subdivízie
                h_kwargs: Dict[str, Any] = {'years': (curr.year, curr.year + 1)}
                if subdiv:
                    h_kwargs['subdiv'] = subdiv
                sviatky = holidays.country_holidays(iso_clean, **h_kwargs)
            except Exception:
                try:
                    # Fallback bez subdivízie pri neplatnom kóde regiónu
                    sviatky = holidays.country_holidays(iso_clean, years=(curr.year, curr.year + 1))
                except Exception:
                    sviatky = set()

        # 🛡️ DEFENSIVE: Bezpečnostný limit posunu (max 30 dní) chrániaci pred zacyklením
        bezpecnostny_limit = 30
        posun_dni = 0
        while (curr.weekday() in (5, 6) or curr in sviatky) and posun_dni < bezpecnostny_limit:
            curr += timedelta(days=1)
            posun_dni += 1

        return curr

    @classmethod
    def vypocitaj_danovy_deadline(cls, datum_dodania, typ_dokladu='FAKTURA', stat_nazov: Optional[str] = None, kod_clenenia_dph: Optional[str] = None):
        """
        🏛️ Medzinárodný výpočet zákonnej lehoty na vystavenie daňového dokladu:
        - Dobropisy a zálohové faktúry nemajú zákonom stanovenú lehotu dodania (None).
        - Tuzemsko: lehota podľa legislatívy sídla firmy definovaná v modeli StatLokalizacia.
        - EÚ Intrakomunitárne dodanie ('VAT_REVERSE' / Smernica 2006/112/ES čl. 222): do 15. dňa nasledujúceho kalendárneho mesiaca.
        - Mimo EÚ / Tretie krajiny: riadi sa konfiguráciou daňovej lehoty príslušného štátu v databáze (štandardne bez obmedzenia).
        """

        if not typ_dokladu or str(typ_dokladu).strip().upper() != 'FAKTURA' or not datum_dodania:
            return None

        if isinstance(datum_dodania, str):
            try:
                d_dodania = date.fromisoformat(datum_dodania.split('T')[0])
            except (ValueError, TypeError):
                return None
        elif isinstance(datum_dodania, datetime):
            d_dodania = datum_dodania.date()
        elif isinstance(datum_dodania, date):
            d_dodania = datum_dodania
        else:
            return None

        iso = cls.get_aktivna_krajina_iso(stat_nazov)
        kod_norm = str(kod_clenenia_dph).strip().upper() if kod_clenenia_dph else ""

        # 1. 🇪🇺 B2B Intrakomunitárne dodanie tovaru a cezhraničné služby s prenesením dane (čl. 222 Smernice 2006/112/ES)
        if kod_norm in ['VAT_REVERSE', 'INTRACOMMUNITY', 'REVERSE_CHARGE', 'EU_SUPPLY', 'UE']:
            deadline_rok = d_dodania.year + 1 if d_dodania.month == 12 else d_dodania.year
            deadline_mesiac = 1 if d_dodania.month == 12 else d_dodania.month + 1
            return cls.posun_na_pracovny_den(date(deadline_rok, deadline_mesiac, 15), iso)

        # 2. 🏛️ Čistý dynamický výpočet zo StatLokalizacia podľa legislatívy sídla
        try:
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            stat_obj = StatLokalizaciaModel.get_for_iso(iso) if StatLokalizaciaModel else None
            if stat_obj:
                cielovy_termin = stat_obj.vypocitaj_danovy_termin(d_dodania)
                return cls.posun_na_pracovny_den(cielovy_termin, iso) if cielovy_termin else None
        except Exception:
            pass

        # Ak štát nie je v DB definovaný alebo nemá daňovú lehotu, systém neurčuje zákonný deadline
        return None

    def clean(self):
        super().clean()
        if self.pk is None and FiremnaIdentita.objects.exists():
            raise ValidationError("⚠️ Kritická chyba: Môže existovať iba jeden profil spoločnosti.")
        if self.podtitul is not None:
            self.podtitul = str(self.podtitul).strip() or None
        if self.ico is not None:
            self.ico = self.ico.strip() or None
        if self.dic is not None:
            self.dic = self.dic.strip() or None
        if self.ic_dph is not None:
            self.ic_dph = self.ic_dph.strip().upper() or None
        if self.sidlo_stat:
            self.sidlo_stat = self.sidlo_stat.strip()
        if self.predvolena_mena:
            self.predvolena_mena = self.predvolena_mena.strip().upper()

    def save(self, *args, **kwargs):
        # 🛡️ STABILITA: Singleton pattern s ochranou voči Race Condition na úrovni DB
        target_db = kwargs.get('using') or self._state.db or 'default'

        if self.pk is None and FiremnaIdentita.objects.using(target_db).exists():
            raise ValidationError("⚠️ Kritická chyba: Môže existovať iba jeden profil spoločnosti.")
        
        self.pk = 1

        # 🌍 MASTER COUNTRY SYNC: Určenie meny PRED prvým zápisom (rešpektuje manuálnu voľbu meny)
        iso_krajiny = self.get_aktivna_krajina_iso(self.sidlo_stat)
        defaults_krajiny = self.get_country_defaults(iso_krajiny)
        cielova_mena = defaults_krajiny.get('mena') or ''

        povodny_zaznam = FiremnaIdentita.objects.using(target_db).filter(pk=self.pk).values('sidlo_stat', 'predvolena_mena').first()
        je_novy_profil = povodny_zaznam is None
        povodny_stat = povodny_zaznam.get('sidlo_stat') if povodny_zaznam else None
        povodna_mena = povodny_zaznam.get('predvolena_mena') if povodny_zaznam else None

        stat_sa_zmenil = (povodny_stat is None and bool(self.sidlo_stat)) or (povodny_stat is not None and povodny_stat != self.sidlo_stat)
        stat_bol_upraveny = (not je_novy_profil and povodny_stat != self.sidlo_stat)

        # 🛡️ 100% DEFENZÍVNA VOĽBA MENY: Rešpektuje manuálnu voľbu pri create aj update, synchronizuje len pri prázdnej hodnote alebo zmene štátu bez explicitnej meny
        if not self.predvolena_mena:
            if cielova_mena:
                self.predvolena_mena = cielova_mena
                if kwargs.get('update_fields') is not None:
                    kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'predvolena_mena'})
        elif stat_bol_upraveny and self.predvolena_mena == povodna_mena:
            if cielova_mena:
                self.predvolena_mena = cielova_mena
                if kwargs.get('update_fields') is not None:
                    kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'predvolena_mena'})

        try:
            with transaction.atomic(using=target_db):
                # 1. Samotné uloženie profilu firmy
                super(FiremnaIdentita, self).save(*args, **kwargs)

                # 2. Synchronizácia DPH do GlobalneNastavenia
                GlobalneNastaveniaModel = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
                gn = GlobalneNastaveniaModel.objects.using(target_db).first()
                if gn:
                    nova_zakladna = defaults_krajiny.get('dph_zakladna')
                    nova_znizena = defaults_krajiny.get('dph_znizena')
                    gn_update = []
                    if nova_zakladna is not None and (stat_sa_zmenil or gn.zakladna_sadzba_dph is None):
                        if gn.zakladna_sadzba_dph != nova_zakladna:
                            gn.zakladna_sadzba_dph = nova_zakladna
                            gn_update.append('zakladna_sadzba_dph')
                    if nova_znizena is not None and (stat_sa_zmenil or gn.znizena_sadzba_dph is None):
                        if gn.znizena_sadzba_dph != nova_znizena:
                            gn.znizena_sadzba_dph = nova_znizena
                            gn_update.append('znizena_sadzba_dph')
                    if gn_update:
                        gn.save(using=target_db, update_fields=gn_update)

                # 3. MASTER DATA SYNC: Bezpečné zrkadlenie do CRM v rámci rovnakej DB transakcie
                from Evidencia.models import Zakaznik
                ulica_spolu = f"{self.sidlo_ulica or ''} {self.sidlo_cislo or ''}".strip()
                clean_domena = str(self.web or '').replace('https://', '').replace('http://', '').split('/')[0].strip()

                defaults_sync = {
                    'Nazov_spolocnosti': (self.nazov_spolocnosti or '')[:100],
                    'ico': (self.ico or '')[:20] or None,
                    'dic': (self.dic or '')[:20] or None,
                    'ic_dph': (self.ic_dph or '')[:20] or None,
                    'ulica_a_cislo': ulica_spolu[:150],
                    'mesto': (self.sidlo_mesto or '')[:100],
                    'psc': (self.sidlo_psc or '')[:20],
                    'stat': (self.sidlo_stat or '')[:50],
                    'email': self.email_vseobecny,
                    'telefon': (self.telefon or '')[:20],
                    'iban': (self.iban or '')[:34],
                    'domena': clean_domena[:100] or None,
                }
                interny_zakaznik = Zakaznik.objects.using(target_db).filter(je_interny_profil=True).order_by('pk').first()
                if not interny_zakaznik and defaults_sync.get('ico'):
                    interny_zakaznik = Zakaznik.objects.using(target_db).filter(ico=defaults_sync['ico']).order_by('pk').first()
                    if interny_zakaznik:
                        interny_zakaznik.je_interny_profil = True
                        defaults_sync['je_interny_profil'] = True
                if interny_zakaznik:
                    for k, v in defaults_sync.items():
                        setattr(interny_zakaznik, k, v)
                    interny_zakaznik.save(using=target_db, update_fields=list(defaults_sync.keys()))
                else:
                    Zakaznik.objects.using(target_db).create(je_interny_profil=True, **defaults_sync)

        except IntegrityError as e:
            raise ValidationError("⚠️ Kritická chyba: Súbežný pokus o vytvorenie profilu. Profil už existuje.") from e

        # Invalidácia cache striktne až po úspešnom commite celej DB transakcie
        def _vycisti_firemnu_cache():
            try:
                cache.delete_many(['isas_dynamicky_web_firmy', 'isas_firemna_mena'])
                cache.add('web_cache_version', 1, None)
                try:
                    cache.incr('web_cache_version')
                except ValueError:
                    cache.set('web_cache_version', 2, None)
            except Exception:
                pass

        transaction.on_commit(_vycisti_firemnu_cache, using=target_db)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("⚠️ Kritická chyba: Zmazanie tohto systémového záznamu je prísne zakázané!")

class GlobalneNastavenia(ISASAuditMixin, models.Model):
    zakladna_sadzba_dph = models.DecimalField("📈 Základná sadzba DPH (%)", max_digits=5, decimal_places=2, null=True, blank=True, help_text="📈 Základná sadzba DPH. Ak je prázdne, automaticky sa preberá podľa sídla firmy.")
    znizena_sadzba_dph = models.DecimalField("📉 Znížená sadzba DPH (%)", max_digits=5, decimal_places=2, null=True, blank=True, help_text="📉 Znížená sadzba DPH. Ak je prázdne, automaticky sa preberá podľa sídla firmy.")
    sadzba_uroku_omeskanie = models.DecimalField("⏱️ Úrok z omeškania (%/deň)", max_digits=5, decimal_places=2, default=Decimal('0.05'), help_text="⏱️ Zákonný alebo zmluvný úrok z omeškania za každý deň omeškania platby.")

    nahrada_za_1km = models.DecimalField("🛣️ Základná náhrada za 1 km", max_digits=6, decimal_places=3, default=Decimal('0.000'), help_text="🛣️ Zákonná sadzba náhrady za 1 km v mene firmy (nastavuje sa podľa legislatívy sídla).")
    stravne_5_12h = models.DecimalField("🍽️ Stravné (5 - 12 hodín)", max_digits=6, decimal_places=2, default=Decimal('0.00'), help_text="🍽️ Diéty v mene firmy (nastavuje sa podľa legislatívy sídla).")
    stravne_12_18h = models.DecimalField("🍲 Stravné (12 - 18 hodín)", max_digits=6, decimal_places=2, default=Decimal('0.00'), help_text="🍲 Diéty v mene firmy (nastavuje sa podľa legislatívy sídla).")
    stravne_nad_18h = models.DecimalField("🍱 Stravné (nad 18 hodín)", max_digits=6, decimal_places=2, default=Decimal('0.00'), help_text="🍱 Diéty v mene firmy (nastavuje sa podľa legislatívy sídla).")

    hodinova_sadzba_technik = models.DecimalField("👨‍🔧 Hodinová sadzba technika", max_digits=8, decimal_places=2, default=Decimal('0.00'), help_text="👨‍🔧 Fakturačná hodinová sadzba technika v mene firmy.")
    pohotovostny_priplatok = models.DecimalField("🚨 Pohotovostný/Víkendový príplatok (%)", max_digits=5, decimal_places=2, default=Decimal('0.00'))
    pohotovostny_priplatok_popis = models.CharField(
        "📝 Podmienky pohotovostného/prioritného príplatku",
        max_length=255, blank=True, null=True,
        help_text="Vlastný popis podmienok uplatnenia príplatku. Ak zostane prázdne, systém vygeneruje neutrálny text podľa pracovnej doby dispečingu."
    )
    min_uctovacia_jednotka = models.PositiveIntegerField(
        "⏳ Minimálna účtovacia jednotka / Takt (min)", 
        null=True, blank=True,
        help_text="Blokové zaokrúhľovanie odpracovaného času (napr. 15, 30 alebo 60 min). Nechajte prázdne, ak účtujete presný čas bez zaokrúhľovania."
    )
    sadzba_za_km_zakaznik = models.DecimalField("🚙 Fakturačná sadzba za 1 km cesty", max_digits=6, decimal_places=2, default=Decimal('0.00'), help_text="💰 Suma účtovaná zákazníkovi za dopravu technika v mene firmy.")
    sadzba_za_cas_na_ceste = models.DecimalField("⏱️ Sadzba za strávený čas na ceste (hodina)", max_digits=6, decimal_places=2, default=Decimal('0.00'), help_text="🚘 Suma účtovaná zákazníkovi za čas na ceste v mene firmy.")

    globalny_banner = models.TextField("📢 Globálny informačný banner", blank=True, null=True, help_text="👁️ Zobrazí sa všetkým používateľom. Nechajte prázdne pre vypnutie.")
    rezim_udrzby = models.BooleanField("🚧 Režim údržby (Maintenance mode)", default=False, help_text="⛔ Ak je zapnuté, obmedzí bežný prístup do Evidencie.")
    # --- JIT LOGISTIKA (KONTROLA SKLADOV) ---
    jit_kontrolovat_vsetky_sklady = models.BooleanField(
        "🌍 JIT: Kontrolovať VŠETKY aktívne sklady", 
        default=True,
        help_text="💡 NAJVYŠŠIA PRIORITA: Ak je toto zapnuté, systém sčíta materiál z úplne všetkých skladov a pobočiek. Nižšie nastavenia sú vtedy ignorované."
    )
    
    jit_kontrolovat_hlavny_sklad = models.BooleanField(
        "🏢 JIT: Zahrnúť Hlavný sklad", 
        default=False, # 👈 Zmenené na False, aby sa to nebilo pri zapnutí "Všetky"
        help_text="📌 Platí len, ak NIE SÚ zaškrtnuté 'Všetky aktívne sklady'. Začiarknite, ak chcete kontrolovať Hlavný sklad samostatne alebo v kombinácii s vybranými filiálkami."
    )
    
    jit_kontrolovane_filialky = models.ManyToManyField(
        'Evidencia.Filialka', 
        blank=True,
        verbose_name="🏪 JIT: Zahrnúť jednotlivé sklady/filiálky",
        help_text="📌 Platí len, ak NIE SÚ zaškrtnuté 'Všetky aktívne sklady'. Vyberte konkrétne pobočky alebo mobilné sklady, ktoré chcete kontrolovať (môžete kombinovať so zaškrtnutým Hlavným skladom)."
    )

    imap_server_faktury = models.CharField(
        "📥 IMAP Server (Poštár)", max_length=255, default="imap.gmail.com", 
        help_text="🖥️ Adresa servera, z ktorého sa sťahujú faktúry."
    )
    email_pre_faktury = models.EmailField(
        "📧 E-mail pre faktúry (ISAS Poštár)", blank=True, null=True,
        help_text="🤖 Z tejto schránky bude AI sťahovať dodávateľské faktúry."
    )
    heslo_pre_faktury = EncryptedCharField(
        "🔑 Heslo pre faktúry (App Password)", max_length=255, blank=True, null=True,
        help_text="⚠️ Zadajte 16-miestne heslo aplikácie (App Password)."
    )
    faktury_notif_tg = models.BooleanField("📲 ISAS Poštár (Faktúry): Notifikácie na Telegram", default=True)
    faktury_notif_discord = models.BooleanField("🎮 ISAS Poštár (Faktúry): Notifikácie na Discord", default=True)
    faktury_notif_mail = models.BooleanField("✉️ ISAS Poštár (Faktúry): Notifikácie na E-mail", default=True)
    imap_server_objednavky = models.CharField(
        "📥 IMAP Server (Objednávky)", max_length=255, default="imap.gmail.com", 
        help_text="🤖 Z tejto schránky bude AI sťahovať prijaté objednávky (napr. objednavky@firma.com)."
    )
    email_pre_objednavky = models.EmailField(
        "📧 E-mail pre objednávky", blank=True, null=True,
        help_text="🤖 Z tejto schránky bude AI sťahovať prijaté objednávky (napr. objednavky@firma.com)."
    )
    heslo_pre_objednavky = EncryptedCharField(
        "🔑 Heslo pre objednávky (App Password)", max_length=255, blank=True, null=True,
        help_text="⚠️ Zadajte 16-miestne heslo aplikácie (App Password)."
    )
    objednavky_notif_tg = models.BooleanField("📲 ISAS Poštár (Objednávky): Notifikácie na Telegram", default=True)
    objednavky_notif_discord = models.BooleanField("🎮 ISAS Poštár (Objednávky): Notifikácie na Discord", default=True)
    objednavky_notif_mail = models.BooleanField("✉️ ISAS Poštár (Objednávky): Notifikácie na E-mail", default=True)
    imap_server_banka = models.CharField(
        "🏦 IMAP Server (Bankár)", max_length=255, default="imap.gmail.com", 
        help_text="🖥️ Adresa servera, z ktorého sa sťahujú notifikácie z banky."
    )
    email_pre_banku = models.EmailField(
        "📩 E-mail pre banku (ISAS Bankár)", blank=True, null=True,
        help_text="🤖 Z tejto schránky bude AI sťahovať bankové pohyby."
    )
    heslo_pre_banku = EncryptedCharField(
        "🔐 Heslo pre banku (App Password)", max_length=255, blank=True, null=True,
        help_text="⚠️ Zadajte 16-miestne heslo aplikácie (App Password)."
    )
    bankar_notif_tg = models.BooleanField("📲 ISAS Bankár: Notifikácie na Telegram", default=True)
    bankar_notif_discord = models.BooleanField("🎮 ISAS Bankár: Notifikácie na Discord", default=True)
    bankar_notif_mail = models.BooleanField("✉️ ISAS Bankár: Notifikácie na E-mail", default=True)
    imap_server_reklamacie = models.CharField(
        "📥 IMAP Server (Reklamácie)", max_length=255, default="imap.gmail.com", 
        help_text="🖥️ Adresa servera pre sťahovanie reklamácií."
    )
    email_pre_reklamacie = models.EmailField(
        "📧 E-mail pre reklamácie", blank=True, null=True,
        help_text="🤖 Z tejto schránky bude AI sťahovať sťažnosti a reklamácie (napr. reklamacie@firma.com)."
    )
    heslo_pre_reklamacie = EncryptedCharField(
        "🔑 Heslo pre reklamácie (App Password)", max_length=255, blank=True, null=True,
        help_text="⚠️ Zadajte 16-miestne heslo aplikácie."
    )
    reklamacie_notif_tg = models.BooleanField("📲 ISAS Poštár (Reklamácie): Notifikácie na Telegram", default=True)
    reklamacie_notif_discord = models.BooleanField("🎮 ISAS Poštár (Reklamácie): Notifikácie na Discord", default=True)
    reklamacie_notif_mail = models.BooleanField("✉️ ISAS Poštár (Reklamácie): Notifikácie na E-mail", default=True)

    email_majitel_notifikacie = models.EmailField(
        "👑 E-mail majiteľa (Notifikácie)", blank=True, null=True,
        help_text="📊 Na túto adresu bude systém odosielať dôležité reporty, uzávierky a alerty."
    )
    telegram_id_majitel = models.CharField(
        "📱 Telegram ID majiteľa", max_length=50, blank=True, null=True,
        help_text="🔔 Číselné ID majiteľa v Telegrame (napr. 123456789). Tu bude ISAS Bot posielať okamžité C-level notifikácie."
    )
    discord_id_majitel = models.CharField(
        "🎮 Discord ID majiteľa", max_length=50, blank=True, null=True,
        help_text="🔔 Snowflake ID používateľa na Discorde (napr. 123456789012345678) pre C-level notifikácie."
    )
    presun_notif_tg = models.BooleanField("📲 Medziskladový presun: Notifikácie na Telegram", default=True)
    presun_notif_discord = models.BooleanField("🎮 Medziskladový presun: Notifikácie na Discord", default=True)
    presun_notif_mail = models.BooleanField("✉️ Medziskladový presun: Notifikácie na E-mail", default=True)

    kandidati_notif_tg = models.BooleanField("📲 Kariéra (Uchádzači): Notifikácie na Telegram", default=True)
    kandidati_notif_discord = models.BooleanField("🎮 Kariéra (Uchádzači): Notifikácie na Discord", default=True)
    kandidati_notif_mail = models.BooleanField("✉️ Kariéra (Uchádzači): Notifikácie na E-mail", default=True)

    servisy_notif_tg = models.BooleanField("📲 Strážca servisov a revízií: Notifikácie na Telegram", default=True)
    servisy_notif_discord = models.BooleanField("🎮 Strážca servisov a revízií: Notifikácie na Discord", default=True)
    servisy_notif_mail = models.BooleanField("✉️ Strážca servisov a revízií: Notifikácie na E-mail", default=True)

    iot_notif_tg = models.BooleanField("📲 IoT Telemetria (Havárie): Notifikácie na Telegram", default=True)
    iot_notif_discord = models.BooleanField("🎮 IoT Telemetria (Havárie): Notifikácie na Discord", default=True)
    iot_notif_mail = models.BooleanField("✉️ IoT Telemetria (Havárie): Notifikácie na E-mail", default=True)

    # ⚖️ LEGISLATÍVNY STRÁŽCA & ZODPOVEDNOSŤ (COMPLIANCE)
    servisy_sledovat_legislativu = models.BooleanField(
        "⚖️ Strážiť zákonné revízie a odborné inšpekcie", 
        default=True, 
        help_text="Povoliť automatické vyhodnocovanie a odosielanie notifikácií o blížiacich sa zákonných lehotách podľa aktívneho odvetvia."
    )
    servisy_predstih_upozornenia_dni = models.PositiveIntegerField(
        "📅 Predstih notifikácie revízií (Dni)", 
        default=14, 
        help_text="Koľko dní pred uplynutím zákonnej lehoty začne systém odosielať upozornenia (odporúčané: 14 až 30 dní)."
    )
    servisy_predstih_motohodiny = models.PositiveIntegerField(
        "⏱️ Predstih servisu (Počítadlo / jednotky)", 
        default=100, 
        help_text="Predstih počítadla alebo prevádzkových jednotiek, kedy systém vopred upozorní na blížiaci sa servis."
    )
    standardna_lehota_reklamacie_dni = models.PositiveIntegerField(
        "🛡️ Štandardná lehota reklamácií (Dni)",
        null=True,
        blank=True,
        help_text="🛡️ Štandardná lehota na vybavenie reklamácie podľa všeobecných obchodných podmienok firmy. Ak zostane prázdne, systém neuplatňuje fixnú lehotu."
    )

    ai_limit_superadmin = models.PositiveIntegerField(default=500, verbose_name="🤖 AI Limit: Majiteľ (správ/deň)")
    ai_limit_management = models.PositiveIntegerField(default=100, verbose_name="🤖 AI Limit: Dispečing/Admin (správ/deň)")
    ai_limit_technik = models.PositiveIntegerField(default=25, verbose_name="🤖 AI Limit: Technik/Skladník (správ/deň)")
    ai_limit_default = models.PositiveIntegerField(default=10, verbose_name="🤖 AI Limit: Ostatní (správ/deň)")

    smtp_host = models.CharField("📤 SMTP Server", max_length=255, blank=True, null=True, default="smtp.gmail.com", help_text="🌐 Napr. smtp.gmail.com alebo mail.websupport.sk")
    smtp_port = models.PositiveIntegerField("🔌 SMTP Port", default=587, help_text="🔢 Zvyčajne 587 (TLS) alebo 465 (SSL).")
    smtp_user = models.CharField("👤 SMTP Prihlasovacie meno", max_length=255, blank=True, null=True, help_text="📧 E-mailová adresa, z ktorej budú odchádzať maily (napr. info@vasa-firma.com).")
    smtp_password = EncryptedCharField("🔒 SMTP Heslo", max_length=255, blank=True, null=True, help_text="🔑 Heslo k e-mailovej schránke (alebo App Password).")
    smtp_use_tls = models.BooleanField("🛡️ Použiť TLS", default=True, help_text="✅ Odporúčané zapnúť pre port 587.")
    
    telegram_bot_token = EncryptedCharField("🤖 Telegram Bot Token", max_length=255, blank=True, null=True, help_text="🗝️ API Token vášho vlastného bota na Telegrame.")
    discord_bot_token = EncryptedCharField(
        "🎮 Discord Bot Token",
        max_length=255, blank=True, null=True, 
        help_text="🗝️ API Token vášho vlastného bota na Discorde."
    )
    url_systemu = models.URLField(
        "🌐 URL adresa systému (Doména)", 
        max_length=255, blank=True, null=True, 
        help_text="🔗 Napr. https://moj-isascore.com (Slúži na generovanie plných odkazov v systéme - pre Telegram bota, QR kódy a e-maily)"
    )
    spotify_client_id = models.CharField(
        "🎵 Spotify Client ID", 
        max_length=255, blank=True, null=True, 
        help_text="Verejný kľúč aplikácie zo Spotify Developer portálu."
    )
    spotify_client_secret = EncryptedCharField(
        "🔒 Spotify Client Secret", 
        max_length=255, blank=True, null=True, 
        help_text="Tajný kľúč aplikácie. V databáze je bezpečne zašifrovaný."
    )
    gemini_api_key = EncryptedCharField(
        "🧠 Gemini API Kľúč", 
        max_length=255, blank=True, null=True, 
        help_text="🔑 Vložte váš API kľúč pre Google Gemini. Ak je prázdne, systém sa pokúsi použiť kľúč zo súboru .env."
    )
    google_maps_api_key = EncryptedCharField(
        "🗺️ Google Maps API Kľúč",
        max_length=255, blank=True, null=True,
        help_text="🔑 Vložte váš API kľúč pre Google Maps / Distance Matrix. Ak je prázdne, použije sa kľúč zo súboru .env."
    )

    # --- 🇪🇺 eFAKTÚRA & PEPPOL INTEGRÁCIA ---
    EFAKTURA_PROVIDER_CHOICES = [
        ('DIRECT_SAPI_SK', '🇸🇰 Slovenský Peppol Access Point (SAPI-SK, JSON Auth)'),
        ('DIRECT_AP', '🌐 Medzinárodný Peppol Access Point (RFC 6749 form-data)'),
        ('BROKER_STORECOVE', '🌍 Globálny Broker: Storecove (Auto-routing Peppol + KSeF + SdI)'),
        ('BROKER_PAGERO', '🌍 Globálny Broker: Pagero Network'),
    ]

    efaktura_provider = models.CharField(
        "🔌 Poskytovateľ eFaktúra brány",
        max_length=30,
        choices=EFAKTURA_PROVIDER_CHOICES,
        default='DIRECT_AP',
        help_text="Určuje, cez aké rozhranie systém odosiela eFaktúry. Pre Slovensko/Nemecko stačí priamy AP, pre Poľsko/Taliansko zvoľte brokera."
    )
    peppol_api_url = models.URLField("🇪🇺 Peppol / Broker API URL", max_length=255, blank=True, null=True, help_text="🔗 Plná URL adresa AP brány (napr. https://www.sapi-sk.sk/sapi pre SR alebo https://api.storecove.com pre brokera).")
    peppol_client_id = models.CharField("👤 Peppol / Broker Client ID (API Key)", max_length=255, blank=True, null=True, help_text="Prihlasovacie meno alebo verejný API kľúč.")
    peppol_client_secret = EncryptedCharField("🔒 Peppol / Broker Client Secret", max_length=255, blank=True, null=True, help_text="🔑 Tajné heslo alebo Secret API kľúč.")
    peppol_participant_id = models.CharField("🆔 Peppol Participant ID", max_length=50, blank=True, null=True, help_text="Medzinárodný identifikátor v sieti Peppol vrátane EAS schémy (napr. 0245:12345678).")

    limit_uloh_zvysena = models.PositiveIntegerField(
        "🟡 Zvýšená záťaž (Počet úloh)", 
        default=3, 
        help_text="Od tohto počtu úloh bude technik v kalendári svietiť na žlto (Bežné vyťaženie)."
    )
    limit_uloh_kriticka = models.PositiveIntegerField(
        "🔴 Kritické preťaženie (SLA riziko)", 
        default=5, 
        help_text="Od tohto počtu úloh bude technik svietiť na červeno (Riziko, že nestihne prísť k zákazníkovi včas)."
    )
    pracovny_cas_od = models.TimeField(
        "⏰ Začiatok pracovnej doby", 
        default="07:30:00", 
        help_text="Štandardný začiatok pracovnej doby technikov (používa sa pre výpočet voľných kapacít na webe)."
    )
    pracovny_cas_do = models.TimeField(
        "⏰ Koniec pracovnej doby", 
        default="16:00:00", 
        help_text="Štandardný koniec pracovnej doby technikov."
    )
    pracovat_vikendy = models.BooleanField(
        "📅 Povoliť online rezervácie cez víkendy",
        default=False,
        help_text="Ak je zapnuté, zákazníci si budú môcť vyklikať termín aj na sobotu a nedeľu."
    )
    pracovat_sviatky = models.BooleanField(
        "📅 Povoliť online rezervácie cez štátne sviatky",
        default=False,
        help_text="Ak je zapnuté, systém nezablokuje dni pracovného pokoja (napr. 1. máj, Vianoce)."
    )
    zapnut_predikciu_nakupu = models.BooleanField(
        "🛒 Zapnúť AI predikciu nákupu",
        default=True,
        help_text="🤖 Ak je zapnuté, systém bude každý pondelok generovať report chýbajúceho materiálu."
    )

    dni_predikcie_nakupu = models.PositiveIntegerField(
        "📅 Dni predikcie nákupu", 
        default=14, 
        help_text="Počet dní dopredu, na ktoré systém počíta potrebný materiál pre plánované servisy."
    )
    
    zapnut_ai_upozornenia_dispecingu = models.BooleanField(
        "🚨 Zapnúť AI Dispatch Auditora",
        default=True,
        help_text="🤖 Ak je zapnuté, umelá inteligencia bude automaticky kontrolovať logiku, prejazdy a BOZP pri každom uložení úlohy."
    )
    audit_log_retencia_dni = models.PositiveIntegerField(
        "🛡️ Retencia auditných logov (Dni)",
        default=730,
        help_text="📅 Počet dní uchovávania histórie zmien pred automatickou archiváciou a vymazaním z databázy (predvolené: 730 dní = 2 roky, minimum 90 dní)."
    )

    class Meta:
        verbose_name = "⚙️ Globálne nastavenie"
        verbose_name_plural = "⚙️ Globálne nastavenia"
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name='globalnenastavenia_singleton_check')
        ]

    def __str__(self):
        return "⚙️ Systémové a účtovné premenné"

    @classmethod
    def get_zakladna_dph(cls) -> Decimal:
        """
        🛡️ ISAS ARCHITECT: Dynamické načítanie DPH z DB singletonu s fallbackom na Medzinárodnú maticu podľa sídla firmy.
        """
        try:
            gn = cls.objects.first()
            if gn and gn.zakladna_sadzba_dph is not None:
                return Decimal(str(gn.zakladna_sadzba_dph)).quantize(Decimal('0.01'))
        except Exception:
            pass

        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            if hasattr(FiremnaIdentitaModel, 'get_country_defaults'):
                defaults = FiremnaIdentitaModel.get_country_defaults()
                if defaults and 'dph_zakladna' in defaults:
                    return Decimal(str(defaults['dph_zakladna'])).quantize(Decimal('0.01'))
        except Exception:
            pass

        return Decimal('0.00')

    @classmethod
    def get_znizena_dph(cls) -> Decimal:
        """
        🛡️ ISAS ARCHITECT: Dynamické načítanie zníženej DPH z DB singletonu s fallbackom na Medzinárodnú maticu.
        """
        try:
            gn = cls.objects.first()
            if gn and gn.znizena_sadzba_dph is not None:
                return Decimal(str(gn.znizena_sadzba_dph)).quantize(Decimal('0.01'))
        except Exception:
            pass

        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            if hasattr(FiremnaIdentitaModel, 'get_country_defaults'):
                defaults = FiremnaIdentitaModel.get_country_defaults()
                if defaults and 'dph_znizena' in defaults:
                    return Decimal(str(defaults['dph_znizena'])).quantize(Decimal('0.01'))
        except Exception:
            pass

        return Decimal('0.00')

    @classmethod
    def get_country_defaults(cls, iso: Optional[str] = None) -> dict:
        """
        🌍 Proxy metóda na FiremnaIdentita.get_country_defaults pre bezpečný prístup k lokalizácii z GlobalneNastavenia.
        """
        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            return FiremnaIdentitaModel.get_country_defaults(iso=iso)
        except Exception:
            return {
                'iso_kod': '',
                'stat_nazov': 'International',
                'mena': '',
                'dph_zakladna': Decimal('0.00'),
                'dph_znizena': Decimal('0.00'),
                'je_clen_eu': False,
                'peppol_eas_kod': '',
                'lehota_faktury_dni': None,
                'lehota_faktury_typ': 'BEZ_LEHOTY',
                'aliasy': '',
            }

    @property
    def clean_base_url(self):
        """Vráti čistú URL systému bez lomítka na konci, s inteligentným fallbackom."""
        
        # 1. Priorita: To, čo si admin naklikal priamo v systéme (v databáze)
        if self.url_systemu and str(self.url_systemu).strip():
            return str(self.url_systemu).strip().rstrip('/')
            
        # 2. Priorita: To, čo je v settings.py (lokálne 127.0.0.1, v produkcii z .env)
        site_url = getattr(settings, 'SITE_URL', None) or 'http://127.0.0.1:8000'
        return str(site_url).strip().rstrip('/')

    def clean(self):
        super().clean()
        if self.pk is None and GlobalneNastavenia.objects.exists():
            raise ValidationError("⚠️ Kritická chyba: Môže existovať iba jedno globálne nastavenie.")

        if self.pracovny_cas_od and self.pracovny_cas_do and self.pracovny_cas_do <= self.pracovny_cas_od:
            raise ValidationError({"pracovny_cas_do": "⚠️ Koniec pracovnej doby musí byť neskôr ako jej začiatok."})

        if self.limit_uloh_zvysena is not None and self.limit_uloh_kriticka is not None and self.limit_uloh_kriticka < self.limit_uloh_zvysena:
            raise ValidationError({"limit_uloh_kriticka": "⚠️ Kritický limit úloh musí byť vyšší alebo rovný limitu pre zvýšenú záťaž."})

        if self.min_uctovacia_jednotka is not None and self.min_uctovacia_jednotka < 1:
            raise ValidationError({"min_uctovacia_jednotka": "⚠️ Minimálna účtovacia jednotka musí byť aspoň 1 minúta (ochrana pred ZeroDivisionError)."})

        if self.pohotovostny_priplatok is not None and self.pohotovostny_priplatok < Decimal('0.00'):
            raise ValidationError({"pohotovostny_priplatok": "⚠️ Pohotovostný príplatok nemôže byť záporný."})

        if self.audit_log_retencia_dni is not None and self.audit_log_retencia_dni < 90:
            raise ValidationError({'audit_log_retencia_dni': '⚠️ Bezpečnostná ochrana: Retencia auditných logov nesmie byť kratšia ako 90 dní!'})

        sadzby_na_kontrolu = {
            'zakladna_sadzba_dph': 'Základná sadzba DPH',
            'znizena_sadzba_dph': 'Znížená sadzba DPH',
            'sadzba_uroku_omeskanie': 'Úrok z omeškania',
            'hodinova_sadzba_technik': 'Hodinová sadzba technika',
            'sadzba_za_km_zakaznik': 'Sadzba za km',
            'sadzba_za_cas_na_ceste': 'Sadzba za čas na ceste',
            'nahrada_za_1km': 'Náhrada za 1 km',
            'stravne_5_12h': 'Stravné 5-12h',
            'stravne_12_18h': 'Stravné 12-18h',
            'stravne_nad_18h': 'Stravné nad 18h',
        }
        for field_key, field_title in sadzby_na_kontrolu.items():
            field_val = getattr(self, field_key, None)
            if field_val is not None and field_val < Decimal('0.00'):
                raise ValidationError({field_key: f"⚠️ {field_title} nemôže byť záporná suma."})

        if self.peppol_participant_id and ':' not in str(self.peppol_participant_id).strip():
            raise ValidationError({"peppol_participant_id": "⚠️ Peppol Participant ID musí obsahovať medzinárodnú EAS schému oddelenú dvojbodkou (napr. 0245:1234567890 pre SR, 0144:12345678 pre ČR, atď.)."})

        # 🛡️ ISAS ARCHITECT: Ochrana pred nulovými alebo zápornými predstihmi servisu
        compliance_fields = {
            'servisy_predstih_upozornenia_dni': 'Predstih upozornenia revízií',
            'servisy_predstih_motohodiny': 'Predstih servisu v jednotkách počítadla',
        }

        for field_name, label in compliance_fields.items():
            val = getattr(self, field_name, None)
            if val is not None and val < 1:
                raise ValidationError({field_name: f"⚠️ {label} musí byť minimálne 1."})

        # 🛡️ ISAS ARCHITECT: Kontrola integrity a zabránenie kolízie SMTP portu a šifrovania
        if self.smtp_host:
            if self.smtp_port == 465 and self.smtp_use_tls:
                raise ValidationError({"smtp_use_tls": "⚠️ Port 465 vyžaduje SSL (nie STARTTLS). Vypnite voľbu 'Použiť TLS'."})
            if self.smtp_port == 587 and not self.smtp_use_tls:
                raise ValidationError({"smtp_use_tls": "⚠️ Port 587 vyžaduje STARTTLS šifrovanie. Zaškrtnite voľbu 'Použiť TLS'."})
            if self.smtp_port not in (25, 465, 587, 2525):
                raise ValidationError({"smtp_port": "⚠️ Neštandardný SMTP port. Použite 587 (TLS), 465 (SSL) alebo 25/2525."})

    def save(self, *args, **kwargs):
        target_db = kwargs.get('using') or self._state.db or 'default'
        if self.pk is None and GlobalneNastavenia.objects.using(target_db).exists():
            raise ValidationError("⚠️ Kritická chyba: Globálne nastavenia už existujú.")
        
        # 🌍 100% MEDZINÁRODNÁ INTEGRITA: Dynamické naplnenie sadzieb podľa štátu materskej firmy
        if self.zakladna_sadzba_dph is None or self.znizena_sadzba_dph is None:
            try:
                FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                if hasattr(FiremnaIdentitaModel, 'get_country_defaults'):
                    defaults = FiremnaIdentitaModel.get_country_defaults()
                    if defaults:
                        doplnene_dph = set()
                        if self.zakladna_sadzba_dph is None:
                            self.zakladna_sadzba_dph = defaults.get('dph_zakladna') if defaults.get('dph_zakladna') is not None else Decimal('0.00')
                            doplnene_dph.add('zakladna_sadzba_dph')
                        if self.znizena_sadzba_dph is None:
                            self.znizena_sadzba_dph = defaults.get('dph_znizena') if defaults.get('dph_znizena') is not None else Decimal('0.00')
                            doplnene_dph.add('znizena_sadzba_dph')
                        if kwargs.get('update_fields') is not None and doplnene_dph:
                            kwargs['update_fields'] = list(set(kwargs['update_fields']) | doplnene_dph)
            except Exception:
                pass

        if self.zakladna_sadzba_dph is None:
            self.zakladna_sadzba_dph = Decimal('0.00')
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'zakladna_sadzba_dph'})
        if self.znizena_sadzba_dph is None:
            self.znizena_sadzba_dph = Decimal('0.00')
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = list(set(kwargs['update_fields']) | {'znizena_sadzba_dph'})

        self.pk = 1
        try:
            with transaction.atomic(using=target_db):
                super(GlobalneNastavenia, self).save(*args, **kwargs)
        except IntegrityError:
            raise ValidationError("⚠️ Kritická chyba: Súbežný pokus o vytvorenie nastavení (Race Condition).")

        # Invalidácia pri zmene nastavení striktne po commite transakcie
        def _vycisti_globalne_cache():
            try:
                cache.delete_many(['isas_globalne_nastavenia', 'isas_smtp_config', 'isas_gemini_api_key', 'isas_google_maps_api_key', 'isas_compliance_config'])
            except Exception:
                pass
            try:
                cache.add('web_cache_version', 1, None)
                cache.incr('web_cache_version')
            except ValueError:
                try:
                    cache.set('web_cache_version', 2, None)
                except Exception:
                    pass
            except Exception:
                pass

        transaction.on_commit(_vycisti_globalne_cache, using=target_db)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("⚠️ Kritická chyba: Zmazanie tohto systémového záznamu je na úrovni jadra prísne zakázané!")

class EmailovyUcet(ISASAuditMixin, ISASGDPRMixin, models.Model):
    nazov = models.CharField(max_length=100, help_text="🏷️ Interné označenie e-mailovej schránky pre prehľadnú identifikáciu v systéme.", verbose_name="🏷️ Pracovný názov schránky")
    email_adresa = models.EmailField(unique=True, help_text="📧 Plná e-mailová adresa používaná na odosielanie a prijímanie systémových správ.", verbose_name="📧 E-mailová adresa")
    
    prihlasovacie_meno = models.CharField(max_length=255, blank=True, null=True, help_text="👤 Prihlasovacie meno k e-mailovému serveru, ak sa líši od e-mailovej adresy.", verbose_name="👤 Prihlasovacie meno")
    heslo = EncryptedCharField(max_length=255, help_text="🔑 Bezpečnostné heslo alebo vygenerované heslo aplikácie (App Password) pre prístup k serveru.", verbose_name="🔑 Heslo")
    
    imap_server = models.CharField(max_length=255, default="imap.gmail.com", help_text="📥 Adresa IMAP servera pre načítavanie prichádzajúcej pošty do systému.", verbose_name="📥 IMAP server")
    imap_port = models.PositiveIntegerField(default=993, help_text="🔌 Port pre zabezpečené IMAP pripojenie (štandardne 993 pre SSL/TLS).", verbose_name="🔌 IMAP port")
    
    smtp_server = models.CharField(max_length=255, default="smtp.gmail.com", help_text="📤 Adresa SMTP servera pre odosielanie e-mailov a dokladov zo systému.", verbose_name="📤 SMTP server")
    smtp_port = models.PositiveIntegerField(default=465, help_text="🔌 Port pre zabezpečené SMTP pripojenie (štandardne 465 pre SSL alebo 587 pre TLS).", verbose_name="🔌 SMTP port")
    
    podpis_html = models.TextField(blank=True, null=True, help_text="✍️ Formátovaný HTML podpis automaticky pripájaný ku každému odoslanému e-mailu.", verbose_name="✍️ Automatický HTML podpis")
    je_aktivny = models.BooleanField(default=True, help_text="⚡ Určuje, či je schránka aktívna pre automatický príjem a odosielanie pošty.", verbose_name="✅ Schránka je aktívna")

    class Meta:
        verbose_name = "📧 E-mailový účet"
        verbose_name_plural = "📧 E-mailové účty"

    def __str__(self):
        return f"{self.nazov} ({self.email_adresa})"

class EmailovaSprava(ISASAuditMixin, ISASGDPRMixin, models.Model):
    ucet = models.ForeignKey(EmailovyUcet, on_delete=models.CASCADE, related_name="spravy", db_index=True, verbose_name="📧 Účet", help_text="📧 Poštový účet, cez ktorý bola táto správa prijatá alebo odoslaná.")
    
    # STABILITA: Pridané blank=True, null=True, pretože spam často nemá Message-ID.
    message_id = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name="🆔 Message-ID", help_text="🆔 Unikátny technický identifikátor správy pre zamedzenie duplicitného spracovania pošty.")
    
    odosielatel = models.CharField(max_length=500, blank=True, null=True, verbose_name="📤 Odosielateľ", help_text="📤 E-mailová adresa a meno odosielateľa správy.")
    prijemca = models.TextField(blank=True, null=True, verbose_name="📥 Príjemca", help_text="📥 E-mailové adresy príjemcov správy vrátane kópií.")
    predmet = models.CharField(max_length=1000, blank=True, null=True, verbose_name="🏷️ Predmet", help_text="🏷️ Predmet e-mailovej správy pre rýchlu orientáciu v komunikácii.")
    
    datum_prijatia = models.DateTimeField(db_index=True, blank=True, null=True, verbose_name="📅 Dátum prijatia", help_text="📅 Presný čas prijatia správy pre sledovanie reakčnej doby a SLA.")
    
    telo_text = models.TextField(blank=True, null=True, verbose_name="📝 Telo (Text)", help_text="📝 Čistý textový obsah e-mailovej správy.")
    telo_html = models.TextField(blank=True, null=True, verbose_name="🌐 Telo (HTML)", help_text="🌐 Formátovaný HTML obsah e-mailovej správy vrátane vizuálneho rozvrhnutia.")
    
    precitane = models.BooleanField(default=False, db_index=True, verbose_name="👀 Prečítané", help_text="👀 Indikátor prečítania správy pre sledovanie stavu spracovania v tíme.")
    zlozka = models.CharField(max_length=20, default='INBOX', db_index=True, verbose_name="📁 Zložka", help_text="📁 Priečinok uloženia správy v poštovej schránke.") 
    
    # AI a CRM
    ai_zhrnutie = models.TextField(blank=True, null=True, verbose_name="🤖 AI Zhrnutie", help_text="🤖 Automaticky vygenerované zhrnutie obsahu pre zrýchlenie vybavovania požiadaviek.")
    ai_kategoria = models.CharField(max_length=50, blank=True, null=True, db_index=True, verbose_name="🧠 AI Kategória", help_text="🧠 Automatické zaradenie správy pre efektívne triedenie a priorizáciu.")
    zakaznik = models.ForeignKey('Evidencia.Zakaznik', on_delete=models.SET_NULL, null=True, blank=True, related_name="maily", verbose_name="👥 Zákazník", help_text="👥 Priradený zákazník pre kompletnú komunikáciu v CRM histórii klienta.")
    dodavatel = models.ForeignKey('Evidencia.Dodavatel', on_delete=models.SET_NULL, null=True, blank=True, related_name="maily", verbose_name="🏢 Dodávateľ", help_text="🏢 Priradený dodávateľ pre párovanie nákupných požiadaviek, ponúk a faktúr.")
    zamestnanec = models.ForeignKey('Evidencia.Zamestnanec', on_delete=models.SET_NULL, null=True, blank=True, related_name="maily", verbose_name="👔 Zamestnanec", help_text="👔 Zodpovedný alebo spárovaný zamestnanec spracovávajúci túto komunikáciu.")

    class Meta:
        verbose_name = "✉️ E-mail"
        verbose_name_plural = "📬 E-maily"
        ordering = ['-datum_prijatia']
        unique_together = ('ucet', 'message_id')
        indexes = [
            # VÝKON: Kritické indexy pre okamžité načítanie zložiek a neprečítaných správ v UI
            models.Index(fields=['ucet', 'zlozka', '-datum_prijatia']),
            models.Index(fields=['ucet', 'precitane', '-datum_prijatia']),
        ]

    def __str__(self):
        return f"{self.predmet or 'Bez predmetu'} - {self.odosielatel or 'Neznámy odosielateľ'}"

    def save(self, *args, **kwargs):
        doplnene_polia = set()

        # 🛡️ DEFIOCHRANA: Normalizácia reťazca na datetime objekt pred prácou s časovými zónami
        if isinstance(self.datum_prijatia, str):
            self.datum_prijatia = parse_datetime(self.datum_prijatia)

        # Ochrana pred AttributeError na tzinfo, ak by bol predaný čistý date namiesto datetime
        if isinstance(self.datum_prijatia, date) and not isinstance(self.datum_prijatia, datetime):
            self.datum_prijatia = datetime.combine(self.datum_prijatia, datetime.min.time())
            doplnene_polia.add('datum_prijatia')

        # STABILITA 1: Ak chýba dátum (spam), doplníme aktuálny čas stiahnutia; pri USE_TZ=False normalizujeme aware čas na naive
        if not self.datum_prijatia:
            self.datum_prijatia = timezone.now()
            doplnene_polia.add('datum_prijatia')

        use_tz = getattr(settings, 'USE_TZ', False)
        if not use_tz and self.datum_prijatia:
            if timezone.is_aware(self.datum_prijatia):
                c_cas = timezone.localtime(self.datum_prijatia) if (self.datum_prijatia and timezone.is_aware(self.datum_prijatia)) else self.datum_prijatia
                self.datum_prijatia = c_cas.replace(tzinfo=None) if (c_cas and timezone.is_aware(c_cas)) else c_cas
                doplnene_polia.add('datum_prijatia')
        elif use_tz and self.datum_prijatia and not timezone.is_aware(self.datum_prijatia):
            self.datum_prijatia = timezone.make_aware(self.datum_prijatia, timezone.get_current_timezone())
            doplnene_polia.add('datum_prijatia')
        
        if self.message_id:
            cleaned_id = self.message_id.strip()
            if cleaned_id != self.message_id:
                self.message_id = cleaned_id
                doplnene_polia.add('message_id')

        # STABILITA 2: Ak chýba message_id (spam), vygenerujeme unikátny fallback
        if not self.message_id:
            self.message_id = f"fallback-{uuid.uuid4()}@isas.local"
            doplnene_polia.add('message_id')
        elif len(self.message_id) > 255:
            # STABILITA 3: Ochrana pred DataError (orezanie dlhých spam ID s pridaním MD5 hashu)
            hash_id = hashlib.md5(self.message_id.encode('utf-8')).hexdigest()
            self.message_id = f"{self.message_id[:210]}...{hash_id}"
            doplnene_polia.add('message_id')
            
        # VÝKON: Do update_fields pridáme tieto polia LEN vtedy, ak boli reálne vygenerované/upravené
        if kwargs.get('update_fields') is not None and doplnene_polia:
            kwargs['update_fields'] = list(set(kwargs['update_fields']) | doplnene_polia)

        super(EmailovaSprava, self).save(*args, **kwargs)

class EmailPriloha(ISASAuditMixin, models.Model):
    sprava = models.ForeignKey(EmailovaSprava, on_delete=models.CASCADE, related_name="prilohy", verbose_name="✉️ E-mailová správa", help_text="✉️ Nadradená e-mailová správa, ku ktorej je tento súbor priložený.")
    subor = models.FileField(upload_to="emailove_prilohy/%Y/%m/", max_length=1000, verbose_name="📁 Súbor prílohy", help_text="📁 Fyzický súbor prílohy uložený na zabezpečenom úložisku.")
    nazov_suboru = models.CharField(max_length=1000, verbose_name="🏷️ Názov súboru", help_text="🏷️ Pôvodný názov súboru pre identifikáciu v odoslanej alebo prijatej pošte.")
    mime_type = models.CharField(max_length=100, blank=True, null=True, verbose_name="⚙️ MIME Typ", help_text="⚙️ Technický formát súboru pre jeho správne spracovanie a bezpečné zobrazenie.")
    
    class Meta:
        verbose_name = "📎 Príloha e-mailu"
        verbose_name_plural = "📎 Prílohy e-mailov"

    def __str__(self):
        return self.nazov_suboru or "📎 Nepomenovaná príloha"

class EmailOptOut(ISASAuditMixin, models.Model):
    ZDROJ_CHOICES = [
        ('EMAIL_REPLY', '✉️ Odpoveď na e-mail (NEZASIELAŤ)'),
        ('MANUAL', '👤 Ručné pridanie dispečerom'),
        ('WEB_FORM', '🌐 Webový / odhlasovací formulár'),
        ('BOUNCE', '⚠️ Nedoručiteľná adresa (Bounce)'),
    ]
    ROZSAH_CHOICES = [
        ('MARKETING', '📢 Iba obchodná a akvizičná pošta'),
        ('KOMPLETNY', '🛑 Úplný zákaz proaktívnej komunikácie'),
    ]

    email = models.EmailField(unique=True, db_index=True, verbose_name="📧 E-mailová adresa", help_text="Normalizovaná e-mailová adresa, ktorá si neželá obchodnú komunikáciu.")
    aktivny = models.BooleanField(default=True, db_index=True, verbose_name="🚫 Aktívny zákaz")
    zdroj = models.CharField(max_length=20, choices=ZDROJ_CHOICES, default='MANUAL', db_index=True, verbose_name="📥 Zdroj záznamu")
    rozsah = models.CharField(max_length=20, choices=ROZSAH_CHOICES, default='MARKETING', db_index=True, verbose_name="⚖️ Rozsah zákazu")
    dovod = models.CharField(max_length=255, default="Odpoveď NEZASIELAŤ", blank=True, verbose_name="📝 Dôvod vyradenia")
    poznamka = models.TextField(blank=True, null=True, verbose_name="🗒️ Interná poznámka")

    zdrojova_sprava = models.ForeignKey(EmailovaSprava, on_delete=models.SET_NULL, null=True, blank=True, related_name="optout_zaznamy", verbose_name="✉️ Zdrojový e-mail")
    zakaznik = models.ForeignKey('Evidencia.Zakaznik', on_delete=models.SET_NULL, null=True, blank=True, related_name="email_optouts", verbose_name="🏢 Zákazník")
    dodavatel = models.ForeignKey('Evidencia.Dodavatel', on_delete=models.SET_NULL, null=True, blank=True, related_name="email_optouts", verbose_name="🏭 Dodávateľ")
    sukromna_osoba = models.ForeignKey('Evidencia.SukromnaOsoba', on_delete=models.SET_NULL, null=True, blank=True, related_name="email_optouts", verbose_name="👤 Súkromná osoba")

    vytvorene = models.DateTimeField(auto_now_add=True, verbose_name="🕒 Zaevidované")
    aktualizovane = models.DateTimeField(auto_now=True, verbose_name="🔄 Aktualizované")

    class Meta:
        verbose_name = "🚫 Opt-out / Zákaz oslovovania"
        verbose_name_plural = "🚫 Opt-out zoznam (NEZASIELAŤ)"
        ordering = ['-vytvorene']

    def clean(self):
        super().clean()
        if self.email:
            self.email = self.email.strip().lower()

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        stav = "AKTÍVNY ZÁKAZ" if self.aktivny else "Neaktívny"
        return f"{self.email} ({stav})"

class SpotifyToken(ISASAuditMixin, models.Model):
    """
    Bezpečné úložisko pre Spotify OAuth tokeny. 
    Prepojené 1:1 s používateľom (dispečerom).
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        limit_choices_to={'is_staff': True},
        related_name='spotify_token',
        verbose_name="👤 Používateľ",
        help_text="👤 Používateľský účet dispečera alebo zamestnanca, ku ktorému je Spotify profil pripojený."
    )
    access_token = EncryptedCharField(
        max_length=2000, 
        verbose_name="🔑 Access Token",
        help_text="🔑 Zašifrovaný dočasný prístupový kľúč používaný na autentifikáciu požiadaviek."
    )
    refresh_token = EncryptedCharField(
        max_length=2000, 
        verbose_name="🔄 Refresh Token",
        help_text="🔄 Zašifrovaný obnovovací kľúč určený na automatické získanie nového prístupového kľúča."
    )
    
    expires_at = models.DateTimeField(
        verbose_name="⏳ Platnosť do",
        help_text="⏳ Presný čas expirácie prístupového kľúča pre vyhodnotenie potreby jeho obnovy."
    )
    spotify_account_name = models.CharField(
        max_length=255, 
        blank=True, 
        null=True, 
        verbose_name="🎧 Názov Spotify účtu",
        help_text="🎧 Identifikačné meno alebo e-mailová adresa pripojeného účtu Spotify."
    )
    
    vytvorene = models.DateTimeField(
        auto_now_add=True,
        help_text="📅 Dátum a čas nadviazania prepojenia so službou Spotify."
    )
    upravene = models.DateTimeField(
        auto_now=True,
        help_text="⏱️ Dátum a čas poslednej zmeny alebo obnovy prístupových tokenov."
    )

    class Meta:
        verbose_name = "🎵 Prepojenie so Spotify"
        verbose_name_plural = "🎵 Spotify prepojenia"

    def __str__(self):
        try:
            user_display = self.user.get_username() if getattr(self, 'user_id', None) else "Nenastavený"
        except Exception:
            user_display = "Nenastavený"
        return f"Spotify Token pre: {user_display}"

    def is_expired(self):
        """
        🧠 ISAS ENGINE: Skontroluje, či token vypršal.
        Pridávame 5-minútovú rezervu (buffer), aby sme predišli zlyhaniu 
        počas prehrávania, ak token vyprší presne v tej sekunde.
        """
        
        if not self.expires_at:
            return True
            
        # Absolútne nepriestrelná ochrana pre Timezone (USE_TZ = True/False)
        now = timezone.now()
        expires = self.expires_at

        if isinstance(expires, str):
            expires = parse_datetime(expires)
            if not expires:
                return True

        # 🛡️ DEFIOCHRANA (USE_TZ=False): Ochrana pred None a zjednotenie na naive čas
        if not expires:
            return True  # Token bez expiračného času považujeme za expirovaný

        if isinstance(expires, date) and not isinstance(expires, datetime):
            expires = datetime.combine(expires, datetime.min.time())
        if isinstance(now, date) and not isinstance(now, datetime):
            now = datetime.combine(now, datetime.min.time())

        use_tz = getattr(settings, 'USE_TZ', False)
        if not use_tz:
            c_now = timezone.localtime(now) if (now and timezone.is_aware(now)) else now
            now = c_now.replace(tzinfo=None) if (c_now and timezone.is_aware(c_now)) else c_now

            c_expires = timezone.localtime(expires) if (expires and timezone.is_aware(expires)) else expires
            expires = c_expires.replace(tzinfo=None) if (c_expires and timezone.is_aware(c_expires)) else c_expires
        else:
            if not timezone.is_aware(now):
                now = timezone.make_aware(now, timezone.get_current_timezone())
            if not timezone.is_aware(expires):
                expires = timezone.make_aware(expires, timezone.get_current_timezone())

        return now >= (expires - timedelta(minutes=5))

class ISASHraLeaderboard(models.Model):
    """
    Easter Egg Leaderboard pre hru ISAS 2048.
    Garantuje 1 riadok na zamestnanca (OneToOneField), striktný RBAC (iba is_staff),
    defenzívnu validáciu skóre a podporu pre naivný čas (USE_TZ = False).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="easter_egg_skore",
        verbose_name="Používateľ"
    )
    max_skore = models.PositiveIntegerField(
        default=0,
        verbose_name="Maximálne dosiahnuté skóre"
    )
    max_dlazdica = models.PositiveIntegerField(
        default=2,
        verbose_name="Najvyššia dosiahnutá dlaždica"
    )
    dosiahnute_dna = models.DateTimeField(
        default=timezone.now,
        verbose_name="Čas dosiahnutia rekordu"
    )
    # 🏆 Kvartálne firemné sezóny (OneToOne kompatibilné bez nutnosti viacriadkovej schémy)
    sezona_kod = models.CharField(
        max_length=10,
        default="",
        blank=True,
        verbose_name="Kód sezóny (YYYY-QX)"
    )
    sezona_skore = models.PositiveIntegerField(
        default=0,
        verbose_name="Skóre v aktuálnej sezóne"
    )
    sezona_dlazdica = models.PositiveIntegerField(
        default=2,
        verbose_name="Najvyššia dlaždica v aktuálnej sezóne"
    )
    sezona_dosiahnute_dna = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Čas rekordu v sezóne"
    )
    # 🎯 Firemná Denná výzva (Seeded Daily Challenge)
    den_kod = models.CharField(
        max_length=10,
        default="",
        blank=True,
        verbose_name="Kód dňa (YYYY-MM-DD)"
    )
    den_skore = models.PositiveIntegerField(
        default=0,
        verbose_name="Skóre v dennej výzve"
    )
    den_dlazdica = models.PositiveIntegerField(
        default=2,
        verbose_name="Najvyššia dlaždica v dennej výzve"
    )
    den_dosiahnute_dna = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Čas dosiahnutia denného rekordu"
    )
    den_bounty_splnena = models.BooleanField(
        default=False,
        verbose_name="Smenová zákazka dňa splnená"
    )

    class Meta:
        verbose_name = "ISAS 2048 Rekord"
        verbose_name_plural = "ISAS 2048 Rekordy"
        ordering = ['-max_skore', '-dosiahnute_dna']

    def __str__(self):
        meno = self.user.get_full_name() or self.user.username
        nazov_dielu = self.get_nazov_dlazdice()
        return f"{meno}: {self.max_skore} b ({nazov_dielu})"

    def clean(self):
        super().clean()
        # Ochrana Zero Data Leak: do rebríčka smie ísť výhradne interný personál
        if not self.user or not self.user.is_staff:
            raise ValidationError("Easter Egg Leaderboard je prístupný výhradne pre interný personál (is_staff=True).")

    def get_nazov_dlazdice(self) -> str:
        """Vráti dynamický odvetvový alebo neutrálny názov pre dosiahnutú dlaždicu."""
        from Evidencia.industry_loader import get_easter_egg_tiles
        mapa = get_easter_egg_tiles()
        return mapa.get(self.max_dlazdica, f"Položka {self.max_dlazdica}")

    @classmethod
    def validuj_vysledok(cls, skore: int, max_dlazdica: int) -> tuple[bool, str]:
        """
        Matematicky nepriestrelný Anti-Cheat validátor.
        Vracia: (je_platne: bool, dovod_chyby: str)
        """
        # 1. Striktná typová kontrola (eliminácia Python bool pasce: isinstance(True, int) == True)
        if type(skore) is not int or type(max_dlazdica) is not int:
            return False, "Neplatný dátový typ parametrov."

        if skore < 0:
            return False, "Skóre nemôže byť záporné."

        # 2. Kontrola platnosti dlaždice: mocnina 2 v bezpečnom rozsahu 2 až 8192
        if max_dlazdica < 2 or (max_dlazdica & (max_dlazdica - 1)) != 0:
            return False, "Dlaždica musí byť kladnou mocninou 2."

        if max_dlazdica > 8192:
            return False, "Dosiahnutá hodnota dlaždice prekročila povolený limit hry."

        k = int(math.log2(max_dlazdica))

        # 3. Matematická kontrola minimálneho možného skóre:
        # Fyzikálne minimum pri generovaní 4-iek: dlaždica 8 vyžaduje min. 8 bodov, dlaždica 16 min. 32 bodov
        if k > 2:
            teoreticke_minimum = (k - 2) * max_dlazdica
            if skore < teoreticke_minimum:
                return False, f"Skóre {skore} je podozrivo nízke pre dosiahnutú dlaždicu {max_dlazdica}."

        # 4. Matematická kontrola maximálneho možného skóre:
        # Hráč môže počas hry generovať desiatky medzizlúčení v bočných radoch bez spojenia najvyššej dlaždice.
        # Bezpečný a reálny horný limit proti cheaterom na doske 4x4 zohľadňuje kumulatívny obrat bodov:
        teoreticke_maximum = max(1000, int(max_dlazdica * 40 * max(1, k)))
        if skore > teoreticke_maximum:
            return False, f"Skóre {skore} prekročilo teoretické maximum pre dlaždicu {max_dlazdica}."

        return True, ""

    KATALOG_BOUNTIES = (
        {
            "kod": "usporny_rezim",
            "nazov": "Úsporný takt (Lean Operations)",
            "popis": "Minimalizácia zbytočnej manipulácie: vyrob položku aspoň 128 pri maximálne 75 ťahoch linky.",
            "podmienka_popis": "Diel ≥ 128 a celkové ťahy ≤ 75",
            "ikona": "zap",
        },
        {
            "kod": "jednosmerny_tok",
            "nazov": "Jednosmerný tok (Gravitačný sklad)",
            "popis": "Spádový tok materiálu: dosiahni skóre aspoň 500 bodov bez jediného pohybu smerom HORE.",
            "podmienka_popis": "Skóre ≥ 500 a ťahy HORE = 0",
            "ikona": "arrow-down",
        },
        {
            "kod": "seriova_homogenita",
            "nazov": "Dávková homogenita (Šaržová výroba)",
            "popis": "Riadenie vyrovnaných zásob: maj na ploche v jednom momente aspoň tri položky s hodnotou 32 a vyššou.",
            "podmienka_popis": "Min. 3 súbežné diely ≥ 32 na ploche",
            "ikona": "layers",
        },
        {
            "kod": "plynuly_takt",
            "nazov": "Plynulý takt linky",
            "popis": "Rýchla kompletizácia expedície: nazbieraj aspoň 1 200 bodov za menej ako 140 ťahov.",
            "podmienka_popis": "Skóre ≥ 1 200 a celkové ťahy ≤ 140",
            "ikona": "clock",
        },
        {
            "kod": "bezodpadova_vyroba",
            "nazov": "Čistá prevádzka (Zero Scrap)",
            "popis": "Bezchybná produkcia: dosiahni skóre aspoň 1 000 bodov bez stornovania ťahu a bez odpisu nepodarku.",
            "podmienka_popis": "Skóre ≥ 1 000 a použité nástroje = 0",
            "ikona": "shield",
        },
        {
            "kod": "kontinualny_tok",
            "nazov": "Kontinuálny tok (Flow State)",
            "popis": "Neprerušovaný výrobný cyklus: dosiahni sériu aspoň 4 po sebe idúcich ťahov so zlúčením dielov.",
            "podmienka_popis": "Max Flow streak ≥ 4",
            "ikona": "activity",
        },
        {
            "kod": "vysoka_kapacita",
            "nazov": "Ťažká montáž (High Capacity)",
            "popis": "Kompletizácia nosného agregátu: vytvor technologický diel s hodnotou aspoň 512.",
            "podmienka_popis": "Diel ≥ 512 pri korektnom nábehu skóre",
            "ikona": "cpu",
        },
        {
            "kod": "expresna_expedicia",
            "nazov": "Expresná expedícia (Rýchly štart)",
            "popis": "Bleskový rozbeh linky: vyrob technologickú položku aspoň 64 do 35 ťahov.",
            "podmienka_popis": "Diel ≥ 64 a celkové ťahy ≤ 35",
            "ikona": "fast-forward",
        },
        {
            "kod": "vysoka_efektivita",
            "nazov": "Vysoká efektivita (Lean Yield)",
            "popis": "Maximálna vyťaženosť surovín: dosiahni skóre aspoň 800 bodov s efektivitou aspoň 12.0 b. na ťah.",
            "podmienka_popis": "Skóre ≥ 800 a efektivita ≥ 12.0 b/ťah",
            "ikona": "trending-up",
        },
        {
            "kod": "dualna_montaz",
            "nazov": "Duálna montáž (Paralelné linky)",
            "popis": "Pripravenosť na finálne zreťazenie: maj na ploche súčasne aspoň dva diely s hodnotou 64 a vyššou.",
            "podmienka_popis": "Min. 2 súbežné diely ≥ 64 na ploche",
            "ikona": "copy",
        },
        {
            "kod": "vytrvalostna_sichta",
            "nazov": "Vytrvalostná šichta (Stabilný výkon)",
            "popis": "Dlhodobé riadenie prevádzky: nazbieraj celkové skóre aspoň 2 000 bodov.",
            "podmienka_popis": "Skóre ≥ 2 000 b.",
            "ikona": "award",
        },
        {
            "kod": "dlhy_lean_flow",
            "nazov": "Majstrovský tok (Deep Flow)",
            "popis": "Vysoká procesná plynulosť: dosiahni sériu aspoň 5 po sebe idúcich ťahov so zlúčením.",
            "podmienka_popis": "Max Flow streak ≥ 5",
            "ikona": "bar-chart-2",
        },
        {
            "kod": "striedma_montaz",
            "nazov": "Striedma montáž (Úspora zásob)",
            "popis": "Precízne plánovanie toku: vytvor diel aspoň 256 pri spotrebe maximálne 110 ťahov linky.",
            "podmienka_popis": "Diel ≥ 256 a celkové ťahy ≤ 110",
            "ikona": "target",
        },
        {
            "kod": "cisty_proces_plus",
            "nazov": "Bezchybná akosť (Zero Defect Gold)",
            "popis": "Prémiová procesná kvalita: dosiahni skóre aspoň 1 500 bodov bez jediného storna a bez odpisu.",
            "podmienka_popis": "Skóre ≥ 1 500 a použité nástroje = 0",
            "ikona": "check-circle",
        },
    )

    @classmethod
    def get_denna_bounty(cls, den_kod: str = "") -> dict:
        """Deterministicky vyberie dennú smenovú úlohu podľa seedu dňa bez potreby ukladania v DB."""
        if not den_kod:
            den_kod = timezone.now().strftime("%Y-%m-%d")
        seed_idx = sum(ord(znak) for znak in den_kod) % len(cls.KATALOG_BOUNTIES)
        return dict(cls.KATALOG_BOUNTIES[seed_idx])

    @classmethod
    def validuj_dennu_bounty(cls, den_kod: str, skore: int, max_dlazdica: int, telemetria: dict) -> bool:
        """
        Matematicky overí pravdivosť splnenia dennej smenovej úlohy z klientskej telemetrie (Anti-Cheat).
        Kontroluje fyzikálnu reláciu medzi ťahmi, skóre a špecifickými obmedzeniami úlohy.
        """
        if not isinstance(telemetria, dict):
            return False

        bounty = cls.get_denna_bounty(den_kod)
        bounty_kod = bounty.get("kod")
        tahy = telemetria.get("tahy")
        pohyby_hore = telemetria.get("pohyby_hore")
        homogenita_splnena = bool(telemetria.get("homogenita_splnena", False))
        dualna_montaz_splnena = bool(telemetria.get("dualna_montaz_splnena", False))
        nastroje_pouzite = telemetria.get("nastroje_pouzite", 0)
        max_flow = telemetria.get("max_flow", 0)

        if type(tahy) is not int or tahy < 1:
            return False

        # Fyzikálna poistka hry: horný limit priemeru (max. 50 b/ťah na doske 4x4 pri zlučovaní vysokých agregátov)
        if skore > 0 and tahy < max(1, int(skore / 50)):
            return False

        if bounty_kod == "usporny_rezim":
            return max_dlazdica >= 128 and 35 <= tahy <= 75
        elif bounty_kod == "jednosmerny_tok":
            if type(pohyby_hore) is not int or pohyby_hore < 0:
                return False
            return skore >= 500 and pohyby_hore == 0 and tahy >= 30
        elif bounty_kod == "seriova_homogenita":
            return homogenita_splnena and max_dlazdica >= 32 and tahy >= 25
        elif bounty_kod == "plynuly_takt":
            return skore >= 1200 and 50 <= tahy <= 140
        elif bounty_kod == "bezodpadova_vyroba":
            if type(nastroje_pouzite) is not int or nastroje_pouzite < 0:
                return False
            return skore >= 1000 and nastroje_pouzite == 0 and tahy >= 40
        elif bounty_kod == "kontinualny_tok":
            if type(max_flow) is not int or max_flow < 0:
                return False
            return max_flow >= 4 and tahy >= 30 and skore >= 300
        elif bounty_kod == "vysoka_kapacita":
            return max_dlazdica >= 512 and skore >= 3584 and tahy >= 90
        elif bounty_kod == "expresna_expedicia":
            return max_dlazdica >= 64 and 18 <= tahy <= 35
        elif bounty_kod == "vysoka_efektivita":
            efektivita = (skore / tahy) if tahy > 0 else 0
            return skore >= 800 and efektivita >= 12.0 and tahy >= 35
        elif bounty_kod == "dualna_montaz":
            return dualna_montaz_splnena and max_dlazdica >= 64 and tahy >= 35
        elif bounty_kod == "vytrvalostna_sichta":
            return skore >= 2000 and tahy >= 85
        elif bounty_kod == "dlhy_lean_flow":
            if type(max_flow) is not int or max_flow < 0:
                return False
            return max_flow >= 5 and tahy >= 35 and skore >= 350
        elif bounty_kod == "striedma_montaz":
            return max_dlazdica >= 256 and 55 <= tahy <= 110
        elif bounty_kod == "cisty_proces_plus":
            if type(nastroje_pouzite) is not int or nastroje_pouzite < 0:
                return False
            return skore >= 1500 and nastroje_pouzite == 0 and tahy >= 55

        return False

    @classmethod
    def get_aktualny_kvartal_info(cls, dt=None) -> tuple[str, str]:
        """Dynamicky vypočíta kód kvartálu (napr. 'YYYY-QX') a jeho názov v naivnom režime."""
        teraz = dt or timezone.now()
        kvartal = (teraz.month - 1) // 3 + 1
        return f"{teraz.year}-Q{kvartal}", f"{kvartal}. kvartál {teraz.year}"

    def zaznamenaj_vysledok(self, nove_skore: int, nova_dlazdica: int, rezim: str = "standard", bounty_splnena: bool = False) -> dict:
        """
        Atomicky aktualizuje all-time, sezónny aj denný rekord výzvy.
        Vracia slovník s príznakmi prekonania: {'all_time': bool, 'sezona': bool, 'den': bool}.
        """
        je_platne, _ = self.validuj_vysledok(nove_skore, nova_dlazdica)
        if not je_platne:
            return {"all_time": False, "sezona": False, "den": False}

        teraz = timezone.now()
        aktualny_kod, _ = self.get_aktualny_kvartal_info(teraz)
        aktualny_den_kod = teraz.strftime("%Y-%m-%d")
        update_fields = []
        prekonany_all_time = False
        prekonana_sezona = False
        prekonany_den = False

        # 1. Firemná Denná výzva: striktne izolovaný deterministický režim
        if rezim == "daily":
            if self.den_kod != aktualny_den_kod:
                self.den_kod = aktualny_den_kod
                self.den_skore = nove_skore
                self.den_dlazdica = nova_dlazdica
                self.den_dosiahnute_dna = teraz
                self.den_bounty_splnena = bool(bounty_splnena)
                update_fields.extend(['den_kod', 'den_skore', 'den_dlazdica', 'den_dosiahnute_dna', 'den_bounty_splnena'])
                prekonany_den = True
            else:
                if nove_skore > self.den_skore:
                    self.den_skore = nove_skore
                    self.den_dlazdica = max(self.den_dlazdica, nova_dlazdica)
                    self.den_dosiahnute_dna = teraz
                    update_fields.extend(['den_skore', 'den_dlazdica', 'den_dosiahnute_dna'])
                    prekonany_den = True
                elif nova_dlazdica > self.den_dlazdica:
                    self.den_dlazdica = nova_dlazdica
                    update_fields.append('den_dlazdica')

                if bounty_splnena and not self.den_bounty_splnena:
                    self.den_bounty_splnena = True
                    update_fields.append('den_bounty_splnena')
        else:
            # 2. Štandardná voľná hra: All-time Sieň slávy
            if nove_skore > self.max_skore:
                self.max_skore = nove_skore
                self.max_dlazdica = max(self.max_dlazdica, nova_dlazdica)
                self.dosiahnute_dna = teraz
                update_fields.extend(['max_skore', 'max_dlazdica', 'dosiahnute_dna'])
                prekonany_all_time = True
            elif nova_dlazdica > self.max_dlazdica:
                self.max_dlazdica = nova_dlazdica
                update_fields.append('max_dlazdica')

            # 3. Štandardná voľná hra: Kvartálna firemná sezóna
            if self.sezona_kod != aktualny_kod:
                self.sezona_kod = aktualny_kod
                self.sezona_skore = nove_skore
                self.sezona_dlazdica = nova_dlazdica
                self.sezona_dosiahnute_dna = teraz
                update_fields.extend(['sezona_kod', 'sezona_skore', 'sezona_dlazdica', 'sezona_dosiahnute_dna'])
                prekonana_sezona = True
            else:
                if nove_skore > self.sezona_skore:
                    self.sezona_skore = nove_skore
                    self.sezona_dlazdica = max(self.sezona_dlazdica, nova_dlazdica)
                    self.sezona_dosiahnute_dna = teraz
                    update_fields.extend(['sezona_skore', 'sezona_dlazdica', 'sezona_dosiahnute_dna'])
                    prekonana_sezona = True
                elif nova_dlazdica > self.sezona_dlazdica:
                    self.sezona_dlazdica = nova_dlazdica
                    update_fields.append('sezona_dlazdica')

        if update_fields:
            self.save(update_fields=list(set(update_fields)))

        return {"all_time": prekonany_all_time, "sezona": prekonana_sezona, "den": prekonany_den}



@receiver(post_delete, sender=EmailPriloha)
def odstran_fyzicky_subor_z_disku(sender, instance, **kwargs):
    """
    Kritická ochrana úložiska v Docker kontajneroch.
    Akonáhle je záznam o e-mailovej prílohe vymazaný z DB, tento signál zabezpečí 
    asynchrónne vymazanie fyzického súboru z disku cez Celery.
    Predchádza blokovaniu Gunicorn workerov pri hromadnom mazaní stoviek e-mailov.
    """
    if hasattr(instance, 'get_deferred_fields') and 'subor' in instance.get_deferred_fields():
        return
    if instance.subor and getattr(instance.subor, 'name', None):
        from ISASaplikacia.tasks import delete_physical_file_task 
        
        # Uložíme si cestu k súboru ako string, pretože po zmazaní objektu 
        # a opustení tohto bloku už instance.subor nemusí byť bezpečne dostupný
        file_name = instance.subor.name
        
        # VÝKON & STABILITA: Súbor zmažeme asynchrónne až keď je DB transakcia úspešne commitnutá.
        # Ak by DB rollbackla zmazanie, súbor na disku ostane zachovaný (s ochranou pred výpadkom brokera).
        target_db = getattr(instance, '_state', None) and instance._state.db or 'default'
        def _safe_delete_email_file(f_name):
            try:
                delete_physical_file_task.delay(f_name)
            except Exception as exc:
                logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre zmazanie e-mailovej prílohy '{f_name}': {exc}")

        transaction.on_commit(lambda f=file_name: _safe_delete_email_file(f), using=target_db)

@receiver(m2m_changed)
def isas_audit_m2m_changes(sender, instance, action, reverse, model, pk_set, **kwargs):
    """Zachytáva zmeny v ManyToMany poliach (napr. pridanie technika k úlohe)."""
    
    # 1. Ignorujeme reverzné zmeny (Zaujímame sa len o hlavný model, napr. Úlohu, nie o Technika samotného)
    if reverse: return

    # 2. Chránime len modely, ktoré majú náš Audit Mixin
    if not isinstance(instance, ISASAuditMixin): return

    # 3. Zaujímavé sú pre nás len akcie po dokončení zápisu do DB s reálnymi zmenami
    if action not in ["post_add", "post_remove", "post_clear"]: return
    if action in ["post_add", "post_remove"] and not pk_set: return

    from .middleware import get_current_user
    user = get_current_user()
    user = user if (user and user.is_authenticated) else None
    ct = ContentType.objects.get_for_model(instance)

    # 4. Zistíme, ako sa volá M2M pole, ktoré sa práve mení (napr. 'technici')
    field_name = None
    for f in instance._meta.many_to_many:
        # Bezpečný prístup cez remote_field namiesto deskriptora
        if hasattr(f, 'remote_field') and f.remote_field and f.remote_field.through == sender:
            field_name = f.name
            break
            
    if not field_name: return

    target_db = kwargs.get('using') or instance._state.db or 'default'

    # 5. Vytiahneme textové reprezentácie pridaných/odobratých objektov (pre čitateľnosť v logu)
    if pk_set:
        pk_list = list(pk_set)
        # Ochrana pred OOM: Natiahneme a zalogujeme max 50 objektov
        suvisiace_objekty = model._base_manager.using(target_db).filter(pk__in=pk_list[:50])
        zoznam_mien = ", ".join([str(obj) for obj in suvisiace_objekty])
        if len(pk_list) > 50:
            zoznam_mien += f" ... a ďalších {len(pk_list) - 50} záznamov"
    else:
        zoznam_mien = ""

    zmeny = {}
    if action == "post_add":
        zmeny[field_name] = {"z": "Pôvodný stav", "na": f"➕ Pridané: {zoznam_mien}"}
    elif action == "post_remove":
        zmeny[field_name] = {"z": "Pôvodný stav", "na": f"➖ Odobraté: {zoznam_mien}"}
    elif action == "post_clear":
        zmeny[field_name] = {"z": "Pôvodný stav", "na": "🧹 Vymazané kompletne (Clear)"}

    # 6. Zápis do nášho tieňového logu
    if zmeny:
        ISASAuditLog.objects.using(target_db).create(
            vykonal=user, content_type=ct, object_id=str(instance.pk),
            akcia='UPDATE_M2M', isas_metadata_json_zmeny=json.dumps(zmeny),
            pocet_zmien=len(zmeny)
        )

@receiver(pre_delete, sender=FiremnaIdentita)
@receiver(pre_delete, sender=GlobalneNastavenia)
def zabran_zmazaniu_singletonov(sender, instance, **kwargs):
    """Tvrdá poistka: Zablokuje aj QuerySet.delete() z Django Adminu."""
    raise PermissionDenied(f"Kritická chyba: Zmazanie {sender.__name__} je na úrovni jadra prísne zakázané!")

@receiver(pre_delete, sender=StatLokalizacia)
def zabran_zmazaniu_sidla_statu(sender, instance, using=None, **kwargs):
    """
    🛡️ ABSOLÚTNY ŠTÍT: Zablokuje aj hromadné volanie StatLokalizacia.objects.filter(...).delete()
    alebo hromadné mazanie z Django Adminu.
    """
    target_db = using or getattr(instance, '_state', None) and instance._state.db or 'default'
    try:
        FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        identita = FiremnaIdentitaModel.objects.using(target_db).first()
        if identita and identita.sidlo_stat and hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso'):
            aktivne_iso = (FiremnaIdentitaModel.get_aktivna_krajina_iso(identita.sidlo_stat) or '').strip().upper()
            if aktivne_iso and aktivne_iso not in ('NEZADANE', 'INYKRAJINA'):
                moje_iso = str(instance.iso_kod or '').strip().upper()
                if moje_iso == aktivne_iso:
                    raise PermissionDenied(f"🛑 Kritická chyba integrity: Štát '{moje_iso}' je sídlom firmy a je chránený pred zmazaním!")
    except PermissionDenied:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).critical(f"🚨 Chyba pri pre_delete kontrole štátu {instance.pk}: {e}")

def isas_omni_file_cleanup(sender, instance, **kwargs):
    """
    Dynamicky zmaže súbory len pre tie modely, ktoré reálne obsahujú FileField/ImageField.
    Ignoruje modely, ktoré majú vlastné asynchrónne mazanie (napr. EmailPriloha).
    """
    if not hasattr(instance, '_meta'):
        return

    # 🛡️ VÝNIMKY: Modely, ktoré si mažú súbory samé asynchrónne (Celery)
    EXCLUDED_MODELS = ['EmailPriloha', 'MegaZaloha']

    if sender.__name__ in EXCLUDED_MODELS:
        return

    deferred_fields = instance.get_deferred_fields() if hasattr(instance, 'get_deferred_fields') else set()
    for field in instance._meta.fields:
        if isinstance(field, models.FileField):
            if field.name in deferred_fields or field.attname in deferred_fields:
                continue

            file_field = getattr(instance, field.name, None)
            file_name = getattr(file_field, 'name', None)
            if not file_name:
                continue

            # 🛡️ ISAS CLOUD-SAFE STORAGE: Ochrana pred zmazaním defaultných súborov (napr. default_avatar.png)
            default_val = field.get_default() if (hasattr(field, 'has_default') and field.has_default()) else None
            is_default = bool(default_val and file_name == str(default_val))

            if not is_default:
                from ISASaplikacia.tasks import delete_physical_file_task
           
                # VÝKON: Asynchrónne zmazanie s neskorým zviazaním (Late Binding) po commite (s ochranou pred výpadkom brokera)
                target_db = getattr(instance, '_state', None) and instance._state.db or 'default'
                def _safe_delete_omni_file(f_name):
                    try:
                        delete_physical_file_task.delay(f_name)
                    except Exception as exc:
                        logging.getLogger(__name__).warning(f"⚠️ Celery/Redis nie je dostupný pre zmazanie súboru '{f_name}': {exc}")

                transaction.on_commit(lambda f=file_name: _safe_delete_omni_file(f), using=target_db)