import logging

from django.apps import AppConfig
from django.db.models.signals import pre_save, post_save, post_delete, post_init, pre_delete, m2m_changed
from django.core.exceptions import PermissionDenied

class EvidenciaConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Evidencia'
    verbose_name = 'Evidencia – Riadiaci panel a modelový uzol pre automatizovanú správu'

    def ready(self):
        audit_logger = logging.getLogger(__name__)

        # 1. Importy pre dynamickú logiku a prepojenia
        from django.contrib.contenttypes.models import ContentType
        from django.core.signals import request_finished
        from django.db import transaction
        from django.db.models import F, Value
        from django.db.models.functions import Replace, Substr
        from .industry_loader import CATEGORY_MAP
        from ISASaplikacia.models import isas_omni_file_cleanup
        from .models import (
            TechnickaSpecifikacia, 
            CenovyKatalog, 
            zapamataj_si_pocet, 
            kontrola_stavu_zasob,
            KosikSPolozkamiNaDoobjednanie,
            StockMovement,
            VystavenaFaktura,
            PolozkaVystavenejFaktury,
            ServisnyZaznam,
            Uloha
        )

        def _get_target_db(instance):
            from django.db import DEFAULT_DB_ALIAS, router
            return (
                getattr(getattr(instance, '_state', None), 'db', None)
                or router.db_for_write(instance.__class__, instance=instance)
                or DEFAULT_DB_ALIAS
            )

        # 3. Definujeme funkcie pre signály
        def vytvor_specifikaciu(sender, instance, created, **kwargs):
            if kwargs.get('raw', False):
                return
            if created and instance.pk:
                target_db = _get_target_db(instance)
                ct = ContentType.objects.db_manager(target_db).get_for_model(instance)
                if not TechnickaSpecifikacia.objects.using(target_db).filter(content_type=ct, object_id=instance.pk).exists():
                    TechnickaSpecifikacia.objects.using(target_db).create(content_type=ct, object_id=instance.pk)

        def zmaz_specifikaciu(sender, instance, **kwargs):
            if instance.pk:
                target_db = _get_target_db(instance)
                ct = ContentType.objects.db_manager(target_db).get_for_model(instance)
                TechnickaSpecifikacia.objects.using(target_db).filter(content_type=ct, object_id=instance.pk).delete()

        def vytvor_cenovy_zaznam(sender, instance, created, **kwargs):
            if kwargs.get('raw', False):
                return
            if created and instance.pk:
                target_db = _get_target_db(instance)
                ct = ContentType.objects.db_manager(target_db).get_for_model(instance)
                CenovyKatalog.objects.using(target_db).get_or_create(content_type=ct, object_id=instance.pk)

        def zmaz_cenovy_zaznam(sender, instance, **kwargs):
            if instance.pk:
                target_db = _get_target_db(instance)
                ct = ContentType.objects.db_manager(target_db).get_for_model(instance)
                CenovyKatalog.objects.using(target_db).filter(content_type=ct, object_id=instance.pk).delete()

        # =====================================================================
        # 🧠 ISAS ARCHITECT: Globálna synchronizácia názvov (Košík & Pohyby)
        # =====================================================================
        def isas_capture_old_name(sender, instance, **kwargs):
            if kwargs.get('raw', False):
                return
            update_fields = kwargs.get('update_fields')
            # 🚀 PERFORMANCE: Ak sa ukladajú explicitné polia a žiadne nesúvisí s identifikáciou, šetríme SELECT dopyt
            if update_fields is not None:
                id_fields = {'seriove_cislo', 'nazov', 'nazov_polozky', 'name', 'model_zariadenia', 'vyrobne_cislo'}
                if not id_fields.intersection(set(update_fields)):
                    instance._isas_old_name = None
                    return

            is_adding = getattr(getattr(instance, '_state', None), 'adding', False)
            if instance.pk and not is_adding:
                try:
                    target_db = _get_target_db(instance)
                    old_instance = sender.objects.using(target_db).filter(pk=instance.pk).first()
                    if old_instance:
                        # 🛡️ DEFENSIVE: Konzistentné čítanie primárneho identifikátora naprieč všetkými odvetvovými modelmi
                        instance._isas_old_name = (
                            getattr(old_instance, 'seriove_cislo', None)
                            or getattr(old_instance, 'vyrobne_cislo', None)
                            or getattr(old_instance, 'nazov', None)
                            or getattr(old_instance, 'nazov_polozky', None)
                            or getattr(old_instance, 'name', None)
                            or getattr(old_instance, 'model_zariadenia', None)
                            or str(old_instance)
                        )
                    else:
                        instance._isas_old_name = None
                except Exception:
                    instance._isas_old_name = None
            else:
                instance._isas_old_name = None

        def isas_sync_names_globally(sender, instance, created, **kwargs):
            old_name = getattr(instance, '_isas_old_name', None)
            # 🛡️ DEFENSIVE: Okamžitý reset atribútu v pamäti pred akýmkoľvek predčasným returnom (prevencia memory leaku v procese)
            instance._isas_old_name = None

            if created or kwargs.get('raw', False) or not old_name:
                return

            # 🛡️ DEFENSIVE: Konzistentná extrakcia nového identifikátora zhodná s pre_save zachytením
            new_name = (
                getattr(instance, 'seriove_cislo', None)
                or getattr(instance, 'vyrobne_cislo', None)
                or getattr(instance, 'nazov', None)
                or getattr(instance, 'nazov_polozky', None)
                or getattr(instance, 'name', None)
                or getattr(instance, 'model_zariadenia', None)
                or str(instance)
            )

            str_old_name = str(old_name)
            str_new_name = str(new_name)

            if str_old_name and str_new_name and str_old_name != str_new_name:
                safe_new_name_index = str_new_name[:255]
                safe_new_name_stock = str_new_name[:200]
                target_db = _get_target_db(instance)
                with transaction.atomic(using=target_db):
                    # 1. AKTUALIZÁCIA KOŠÍKA (Limit modelu: 255 znakov; F() garantuje odkaz na DB stĺpec a Substr chráni pred DataError)
                    KosikSPolozkamiNaDoobjednanie.objects.using(target_db).filter(nazov_polozky=str_old_name).update(nazov_polozky=safe_new_name_index)
                    KosikSPolozkamiNaDoobjednanie.objects.using(target_db).filter(nazov_polozky__startswith=f"{str_old_name} (").update(
                        nazov_polozky=Substr(Replace(F('nazov_polozky'), Value(str_old_name), Value(safe_new_name_index)), 1, 255)
                    )

                    # 2. AKTUALIZÁCIA HISTÓRIE POHYBOV (Limit modelu: 200 znakov; F() garantuje odkaz na DB stĺpec a Substr chráni pred DataError)
                    StockMovement.objects.using(target_db).filter(product_name=str_old_name).update(product_name=safe_new_name_stock)
                    StockMovement.objects.using(target_db).filter(product_name__startswith=f"{str_old_name} (").update(
                        product_name=Substr(Replace(F('product_name'), Value(str_old_name), Value(safe_new_name_stock)), 1, 200)
                    )

                    # 3. AKTUALIZÁCIA CENOVÉHO KATALÓGU (Izolovaná aktualizácia výhradne pre naviazaný objekt)
                    ct = ContentType.objects.db_manager(target_db).get_for_model(instance)
                    CenovyKatalog.objects.using(target_db).filter(content_type=ct, object_id=instance.pk).update(search_name=safe_new_name_index)

                    # 4. AKTUALIZÁCIA TECHNICKEJ ŠPECIFIKÁCIE (Izolovaná aktualizácia výhradne pre naviazaný objekt)
                    TechnickaSpecifikacia.objects.using(target_db).filter(content_type=ct, object_id=instance.pk).update(search_name=safe_new_name_index)

        # 4. Dynamicky napojíme signály podľa BIBLIE (CATEGORY_MAP)
        registrovane_modely = set()
        for hlavna_kategoria, podkategorie in (CATEGORY_MAP or {}).items():
            if not isinstance(podkategorie, dict):
                continue
            for nazov, model_skladu in podkategorie.items():
                if not model_skladu or not hasattr(model_skladu, '_meta') or model_skladu in registrovane_modely:
                    continue
                registrovane_modely.add(model_skladu)
                lbl = model_skladu._meta.label
                
                # Signály pre Technickú špecifikáciu
                post_save.connect(vytvor_specifikaciu, sender=model_skladu, dispatch_uid=f"isas_spec_create_{lbl}")
                post_delete.connect(zmaz_specifikaciu, sender=model_skladu, dispatch_uid=f"isas_spec_delete_{lbl}")
                
                # Signály pre Cenový katalóg
                post_save.connect(vytvor_cenovy_zaznam, sender=model_skladu, dispatch_uid=f"isas_price_create_{lbl}")
                post_delete.connect(zmaz_cenovy_zaznam, sender=model_skladu, dispatch_uid=f"isas_price_delete_{lbl}")
                
                # Signály pre Skladové limity a Košík
                post_init.connect(zapamataj_si_pocet, sender=model_skladu, dispatch_uid=f"isas_stock_init_{lbl}")
                post_save.connect(kontrola_stavu_zasob, sender=model_skladu, dispatch_uid=f"isas_stock_check_{lbl}")

                # 🚀 Signály pre globálnu synchronizáciu názvov
                pre_save.connect(isas_capture_old_name, sender=model_skladu, dispatch_uid=f"isas_name_capture_{lbl}")
                post_save.connect(isas_sync_names_globally, sender=model_skladu, dispatch_uid=f"isas_name_sync_{lbl}")

        # =====================================================================
        # 5. 🚀 ISAS OMNI-CLEANUP: Dynamické napojenie mazania súborov
        # =====================================================================
        from django.apps import apps
        from django.db import models as django_models
        
        # Prejdeme všetky modely v aplikácii Evidencia aj v aktívnom odvetvovom module
        from django.conf import settings
        app_configs_to_clean = [self]
        industry_mod = (getattr(settings, 'INDUSTRY_MODULE', None) or '').strip().lower()
        if industry_mod:
            # 🛡️ DEFENSIVE: Bezpečné vyhľadanie v registrovaných AppConfigoch podľa labelu aj plného balíka bez LookupError
            for app_config in apps.get_app_configs():
                if app_config.label.lower() == industry_mod or app_config.name.lower() in (industry_mod, f"odvetvia.{industry_mod}"):
                    if app_config not in app_configs_to_clean:
                        app_configs_to_clean.append(app_config)
                    break

        modely_pre_omni_cleanup = set()
        for app_cfg in app_configs_to_clean:
            for model in app_cfg.get_models():
                if not getattr(model._meta, 'proxy', False):
                    modely_pre_omni_cleanup.add(model)
        for model in registrovane_modely:
            if not getattr(model._meta, 'proxy', False):
                modely_pre_omni_cleanup.add(model)

        for model in modely_pre_omni_cleanup:
            has_file_field = any(isinstance(field, django_models.FileField) for field in model._meta.fields)
            if has_file_field:
                post_delete.connect(isas_omni_file_cleanup, sender=model, dispatch_uid=f"isas_omni_cleanup_{model._meta.label_lower}")

        # =====================================================================
        # 6. 🛑 ZÁKONNÁ OCHRANA: Ochrana eFaktúr a položiek pred vymazaním (Aj pri QuerySet.delete)
        # =====================================================================
        def zabran_vymazaniu_vystavenej_faktury(sender, instance, **kwargs):
            target_db = _get_target_db(instance)
            cislo_dokladu = getattr(instance, 'cislo_faktury', None) or f"#{instance.pk}"
            peppol_status = getattr(instance, 'peppol_status', None)
            erp_status = getattr(instance, 'erp_export_status', None)

            # 🛡️ DEFENSIVE: Overenie nemennosti voči DB (ochrana pred modifikáciou objektu v RAM pred zmazaním)
            if instance.pk and (peppol_status not in ['SENT', 'PROCESSING'] and erp_status not in ['EXPORTED', 'PROCESSING']):
                db_stavy = sender.objects.using(target_db).filter(pk=instance.pk).values('peppol_status', 'erp_export_status').first()
                if db_stavy:
                    peppol_status = db_stavy.get('peppol_status') or peppol_status
                    erp_status = db_stavy.get('erp_export_status') or erp_status

            # 1. Kontrola odoslania do medzinárodnej siete Peppol / eFaktúra
            if peppol_status in ['SENT', 'PROCESSING']:
                raise PermissionDenied(f"🛑 ZÁKONNÁ OCHRANA: Faktúru {cislo_dokladu} je prísne zakázané vymazať. Doklad bol odoslaný do siete eFaktúra / Peppol a podlieha povinnej daňovej archivácii.")
            # 2. Kontrola exportu do ERP (Pohoda / KROS Omega)
            if erp_status in ['EXPORTED', 'PROCESSING']:
                raise PermissionDenied(f"🛑 ÚČTOVNÁ OCHRANA: Faktúru {cislo_dokladu} nie je možné vymazať, pretože bola vyexportovaná do ERP.")
            # 3. Kontrola evidovaných úhrad
            if hasattr(instance, 'uhrady') and instance.uhrady.using(target_db).exists():
                raise PermissionDenied(f"🛑 FINANČNÁ OCHRANA: Faktúru {cislo_dokladu} nie je možné vymazať, má evidované platby.")

        def zabran_vymazaniu_polozky_uzamknutej_faktury(sender, instance, **kwargs):
            # 🚀 PERFORMANCE: Používame priamo stĺpec faktura_id – žiadny SELECT dotaz na model faktúry
            faktura_id = getattr(instance, 'faktura_id', None)
            if not faktura_id:
                return

            target_db = _get_target_db(instance)
            connection = transaction.get_connection(using=target_db)

            # Transakčná pamäť stavov – zaručuje presne 1 dopyt pre celú faktúru aj pri zmazaní 100 položiek naraz
            if not hasattr(connection, '_isas_faktura_lock_cache'):
                connection._isas_faktura_lock_cache = {}

            lock_info = connection._isas_faktura_lock_cache.get(faktura_id)
            if lock_info is None:
                db_data = VystavenaFaktura.objects.using(target_db).filter(pk=faktura_id).values(
                    'cislo_faktury', 'peppol_status', 'erp_export_status'
                ).first()

                if not db_data:
                    return

                # 🛡️ DEFENSIVE: Čisté ORM overenie existencie úhrad nezávislé od privátnych deskriptorov modelu
                has_uhrady = False
                if hasattr(VystavenaFaktura, 'uhrady'):
                    try:
                        has_uhrady = VystavenaFaktura.objects.using(target_db).filter(
                            pk=faktura_id, uhrady__isnull=False
                        ).exists()
                    except Exception:
                        has_uhrady = False

                lock_info = {
                    'cislo': db_data.get('cislo_faktury') or f"#{faktura_id}",
                    'peppol': db_data.get('peppol_status') in ['SENT', 'PROCESSING'],
                    'erp': db_data.get('erp_export_status') in ['EXPORTED', 'PROCESSING'],
                    'uhrady': has_uhrady,
                }
                connection._isas_faktura_lock_cache[faktura_id] = lock_info
                # Vyčistenie po commite transakcie (prevencia memory leaku a AttributeError pri uzatvorenom spojení)
                transaction.on_commit(lambda f_id=faktura_id: getattr(connection, '_isas_faktura_lock_cache', {}).pop(f_id, None), using=target_db)

            # 🛡️ DEFENSIVE: Ak sa vyhadzuje výnimka, transakcia ide do ROLLBACK-u a on_commit sa nespustí.
            # Vyčistíme kľúč z pamäte spojenia ihneď, aby sme predišli memory leaku a zastaranému stavu.
            if lock_info['peppol']:
                connection._isas_faktura_lock_cache.pop(faktura_id, None)
                raise PermissionDenied(f"🛑 ZÁKONNÁ OCHRANA: Položku faktúry {lock_info['cislo']} nie je možné vymazať, doklad je odoslaný v štátnom systéme eFaktúra.")
            if lock_info['erp']:
                connection._isas_faktura_lock_cache.pop(faktura_id, None)
                raise PermissionDenied(f"🛑 ÚČTOVNÁ OCHRANA: Položku faktúry {lock_info['cislo']} nie je možné vymazať, doklad je uzamknutý v ERP.")
            if lock_info['uhrady']:
                connection._isas_faktura_lock_cache.pop(faktura_id, None)
                raise PermissionDenied(f"🛑 FINANČNÁ OCHRANA: Položku faktúry {lock_info['cislo']} nie je možné vymazať, faktúra má evidované platby.")

        pre_delete.connect(zabran_vymazaniu_vystavenej_faktury, sender=VystavenaFaktura, dispatch_uid="isas_lock_del_vystavena_faktura")
        pre_delete.connect(zabran_vymazaniu_polozky_uzamknutej_faktury, sender=PolozkaVystavenejFaktury, dispatch_uid="isas_lock_del_polozka_faktury")

        # =====================================================================
        # 6b. 🛡️ PRÁVNA OCHRANA: Ochrana uzamknutých servisných protokolov
        # =====================================================================
        def zabran_vymazaniu_uzamknuteho_protokolu(sender, instance, **kwargs):
            # 🛡️ DVOJITÁ OCHRANA: Kontrola stavu v pamäti RAM, v _povodny_stav a priamy poistný dopyt na DB
            povodny_stav = getattr(instance, '_povodny_stav', {})
            bol_uzavrety_v_db = povodny_stav.get('protokol_uzavrety', False) if isinstance(povodny_stav, dict) else False
            je_uzavrety_v_ram = getattr(instance, 'protokol_uzavrety', False)

            # 🛡️ DEFENSIVE: Ak stav nie je potvrdený v pamäti ani v stave z DB, vykonáme priamy SQL poistný dotaz
            if not bol_uzavrety_v_db and not je_uzavrety_v_ram and instance.pk:
                target_db = _get_target_db(instance)
                bol_uzavrety_v_db = sender.objects.using(target_db).filter(pk=instance.pk, protokol_uzavrety=True).exists()

            if je_uzavrety_v_ram or bol_uzavrety_v_db:
                raise PermissionDenied(
                    f"🚫 PRÁVNA OCHRANA: Servisný záznam #{instance.pk} má elektronicky uzamknutý a podpísaný protokol s digitálnou pečaťou! "
                    "Záznam je zakázané vymazať bez predchádzajúceho resetu konateľom."
                )

        pre_delete.connect(zabran_vymazaniu_uzamknuteho_protokolu, sender=ServisnyZaznam, dispatch_uid="isas_lock_del_servisny_zaznam")

        # =====================================================================
        # 7. 🚨 AI DISPATCH AUDITOR: Automatická kontrola úloh po uložení
        # =====================================================================
        def spust_ai_audit_ulohy(sender, instance, **kwargs):
            # 🛡️ DEFENSIVE: Ignorujeme import fixtures / loaddata
            if kwargs.get('raw', False):
                return
            from .tasks import task_ai_audit_ulohy

            def _safe_dispatch_ai_audit(u_id):
                try:
                    task_ai_audit_ulohy.delay(u_id)
                except Exception as exc:
                    audit_logger.warning("⚠️ Celery/Redis nie je dostupný pre AI audit úlohy #%s: %s", u_id, exc)

            target_db = _get_target_db(instance)

            def _schedule_deduplicated_audit(u_id):
                if not u_id:
                    return
                connection = transaction.get_connection(using=target_db)
                
                # 🚀 O(1) DEDUPLIKÁCIA: Nezávislá od verzie Djanga a bez zásahu do privátneho API connection.run_on_commit
                if not hasattr(connection, '_isas_pending_ai_audits'):
                    connection._isas_pending_ai_audits = set()

                if u_id in connection._isas_pending_ai_audits:
                    return

                connection._isas_pending_ai_audits.add(u_id)

                def _dispatch(target_id=u_id):
                    if hasattr(connection, '_isas_pending_ai_audits'):
                        connection._isas_pending_ai_audits.discard(target_id)
                    _safe_dispatch_ai_audit(target_id)

                transaction.on_commit(_dispatch, using=target_db)

            # Ignorujeme medzikroky (post_clear) počas ukladania formulára dispečera
            if 'action' in kwargs:
                if kwargs['action'] not in ['post_add', 'post_remove']:
                    return

                # 🛡️ DEFENSIVE: Ošetrenie reverzného priradenia M2M (instance nie je Uloha, ale Zamestnanec)
                if kwargs.get('reverse'):
                    pk_set = kwargs.get('pk_set') or set()
                    for u_id in pk_set:
                        _schedule_deduplicated_audit(u_id)
                    return

            target_uloha_id = getattr(instance, 'pk', None)
            if target_uloha_id:
                _schedule_deduplicated_audit(target_uloha_id)

        # Napojenie na uloženie samotného modelu s ochranou proti viacnásobnej registrácii
        post_save.connect(spust_ai_audit_ulohy, sender=Uloha, dispatch_uid="isas_ai_audit_uloha_post_save")
        # Napojenie na zmeny v ManyToMany poliach (Technici a Typy prác)
        m2m_changed.connect(spust_ai_audit_ulohy, sender=Uloha.technici.through, dispatch_uid="isas_ai_audit_uloha_m2m_technici")
        m2m_changed.connect(spust_ai_audit_ulohy, sender=Uloha.typ_servisu.through, dispatch_uid="isas_ai_audit_uloha_m2m_typ_servisu")

        # 🛡️ DEFENSIVE: Garantované vyčistenie pamäte spojení po dokončení požiadavky (Prevencia leaku pri CONN_MAX_AGE > 0)
        def _isas_cleanup_connection_caches(**kwargs):
            from django.db import connections
            for conn in connections.all():
                if hasattr(conn, '_isas_faktura_lock_cache'):
                    conn._isas_faktura_lock_cache.clear()
                if hasattr(conn, '_isas_pending_ai_audits'):
                    conn._isas_pending_ai_audits.clear()

        request_finished.connect(_isas_cleanup_connection_caches, dispatch_uid="isas_clear_connection_caches")
