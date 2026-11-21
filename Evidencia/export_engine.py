import os
import zipfile
import tempfile
import csv
import shutil
from datetime import datetime
from django.core.files.base import ContentFile, File
from django.core.management import call_command
from django.utils import timezone
from io import StringIO
from django.apps import apps

class ISASExportEngine:
    """
    🚀 ISAS DATA NEXUS ENGINE
    Produkčný a bezpečný generátor hromadných exportov. 
    Využíva NamedTemporaryFile pre zamedzenie zahltenia RAM (OOM ochrana).
    """

    def __init__(self, mega_zaloha_id):
        self.mega_zaloha_id = mega_zaloha_id
        self.ZalohaModel = apps.get_model('Evidencia', 'MegaZaloha')

    def spustit_export(self):
        try:
            zaloha = self.ZalohaModel.objects.get(pk=self.mega_zaloha_id)
        except self.ZalohaModel.DoesNotExist:
            return # Záznam bol medzičasom zmazaný

        try:
            # 1. 🛡️ Vytvorenie bezpečného dočasného súboru priamo na disku (Ničí riziko pádu RAM)
            with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as temp_file:
                temp_file_path = temp_file.name

            # 2. 🚦 Smerovanie (Routing) podľa typu exportu
            if zaloha.typ_zalohy == 'kompletny_dump':
                self._vygeneruj_databazovy_dump(zaloha, temp_file_path)
            elif zaloha.typ_zalohy == 'audit_logy':
                from Evidencia.tasks import task_archivovat_a_vycistit_audit_logy
                task_archivovat_a_vycistit_audit_logy(mega_zaloha_id=zaloha.id, zmazat_po_exporte=False)
                return
            elif zaloha.typ_zalohy in ['kniha_jazd', 'evidencia_uhrad', 'skladove_pohyby', 'cenovy_katalog', 'ulohy_a_vykazy']:
                self._vygeneruj_csv_report(zaloha, temp_file_path)
            elif zaloha.typ_zalohy in ['iny', 'mimosystemova']:
                # 🛡️ Ochrana: Ak používateľ vybral mimosystémovú zálohu, ale zabudol nahrať súbor
                with zipfile.ZipFile(temp_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    zipf.writestr(
                        "UPOZORNENIE.txt",
                        "UPOZORNENIE: Zvolili ste typ 'Mimosystémová záloha' alebo 'Iný', ale nenahrali ste žiadny súbor.\n"
                        "Tento súbor bol vygenerovaný automaticky ako záchrana pred pádom systému."
                    )
            elif zaloha.typ_zalohy == 'podklady_mzdy':
                # 🚀 ISAS ARCHITECTURE FIX: Delegovanie na špecializovaný Excel Task
                from Evidencia.tasks import task_vygeneruj_mzdy_excel
                user_id = zaloha.vyziadal_uzivatel.id if zaloha.vyziadal_uzivatel else None
                task_vygeneruj_mzdy_excel.delay(user_id, zaloha.id)
                return  # 🛑 Ukončíme engine tu! Task si to spracuje, uloží súbor a zmení stav sám.
            else:
                # Pre všetky ostatné (faktúry, protokoly, tankovania, ponuky, objednávky) robíme ZIP fyzických súborov
                self._vygeneruj_zip_fyzickych_suborov(zaloha, temp_file_path)

            # 3. 💾 Uloženie hotového súboru do modelu
            if zaloha.subor_zalohy and zaloha.subor_zalohy.name:
                try:
                    if hasattr(zaloha.subor_zalohy, 'path') and os.path.exists(zaloha.subor_zalohy.path):
                        os.remove(zaloha.subor_zalohy.path)
                except (NotImplementedError, AttributeError, Exception):
                    pass
                zaloha.subor_zalohy.delete(save=False)

            with open(temp_file_path, 'rb') as final_file:
                # 🛡️ OPRAVA: Všetko okrem CSV je teraz ZIP (aj databázový dump)
                koncovka = '.csv' if zaloha.typ_zalohy in ['kniha_jazd', 'evidencia_uhrad', 'skladove_pohyby', 'cenovy_katalog', 'ulohy_a_vykazy'] else '.zip'
                _now = timezone.now()
                _local_now = timezone.localtime(_now) if (_now and timezone.is_aware(_now)) else _now
                nazov_suboru = f"ISAS_{zaloha.typ_zalohy}_{_local_now.strftime('%Y%m%d_%H%M%S')}{koncovka}"
                
                zaloha.subor_zalohy.save(nazov_suboru, File(final_file), save=False)

            # 4. ✅ Aktualizácia stavu
            _now = timezone.now()
            _local_now = timezone.localtime(_now) if (_now and timezone.is_aware(_now)) else _now
            zaloha.stav_spracovania = 'hotova'
            aktualna_poznamka = zaloha.poznamka or ""
            zaloha.poznamka = aktualna_poznamka + f"\n✅ [{_local_now.strftime('%d.%m.%Y %H:%M')}] Export bol úspešne vygenerovaný ISAS enginom."
            zaloha.save(update_fields=['subor_zalohy', 'stav_spracovania', 'poznamka'])

        except Exception as e:
            _now = timezone.now()
            _local_now = timezone.localtime(_now) if (_now and timezone.is_aware(_now)) else _now
            zaloha.stav_spracovania = 'chyba'
            aktualna_poznamka = zaloha.poznamka or ""
            zaloha.poznamka = aktualna_poznamka + f"\n❌ [{_local_now.strftime('%d.%m.%Y %H:%M')}] KRITICKÁ CHYBA: {str(e)}"
            zaloha.save(update_fields=['stav_spracovania', 'poznamka'])
            
        finally:
            # 🧹 DEVOPS OCHRANA: Fyzické upratanie dočasného súboru z temp zložky OS
            if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
                try: os.remove(temp_file_path)
                except: pass

    # =========================================================================
    # 🗂️ 1. GENEROVANIE ZIP ARCHÍVOV (FAKTÚRY, PROTOKOLY, BLOČKY)
    # =========================================================================
    def _vygeneruj_zip_fyzickych_suborov(self, zaloha, temp_file_path):
        # Dynamické vytiahnutie modelov, aby sme nemali Circular Importy
        VystavenaFaktura = apps.get_model('Evidencia', 'VystavenaFaktura')
        PrichodziaFaktura = apps.get_model('Evidencia', 'PrichodziaFaktura')
        CenovaPonuka = apps.get_model('Evidencia', 'CenovaPonuka')
        ServisnyZaznam = apps.get_model('Evidencia', 'ServisnyZaznam')
        OdoslanaObjednavka = apps.get_model('Evidencia', 'OdoslanaObjednavka')
        PrijataObjednavka = apps.get_model('Evidencia', 'PrijataObjednavka')
        Tankovanie = apps.get_model('Evidencia', 'Tankovanie')
        Zamestnanec = apps.get_model('Evidencia', 'Zamestnanec')
        TechnickaSpecifikacia = apps.get_model('Evidencia', 'TechnickaSpecifikacia')
        PovolenieNaVstup = apps.get_model('Evidencia', 'PovolenieNaVstup')
        ServisovaneZariadenie = apps.get_model('Evidencia', 'ServisovaneZariadenie') # <-- PRIDANÉ PRE QR KÓDY
        Certifikat = apps.get_model('Evidencia', 'Certifikat')

        # 🎯 Konfigurácia: Aký model, aké dátumové pole na filter, aké fieldy so súbormi
        config = {
            'vystavene_faktury': {'model': VystavenaFaktura, 'date_field': 'datum_vystavenia', 'file_fields': ['pdf_dokument']},
            'prichodzie_faktury': {'model': PrichodziaFaktura, 'date_field': 'datum_prijatia', 'file_fields': ['subor']},
            'cenove_ponuky': {'model': CenovaPonuka, 'date_field': 'datum_vystavenia', 'file_fields': ['pdf_dokument']},
            'servisne_protokoly': {'model': ServisnyZaznam, 'date_field': 'vytvorene', 'file_fields': ['uzavrety_pdf_dokument', 'foto_pred', 'foto_po', 'video']},
            'odoslane_objednavky': {'model': OdoslanaObjednavka, 'date_field': 'vytvorene', 'file_fields': ['pdf_dokument']},
            'prijate_objednavky': {'model': PrijataObjednavka, 'date_field': 'datum_prijatia', 'file_fields': ['subor_objednavky']},
            'tankovania': {'model': Tankovanie, 'date_field': 'datum', 'file_fields': ['foto_blocku']},
            'hr_dokumenty': {'model': Zamestnanec, 'date_field': 'datum_nastupu', 'file_fields': ['zmluva_subor', 'bozp_subor', 'lekarska_prehliadka_subor']},
            'certifikaty': {'model': Certifikat, 'date_field': 'datum_vydania', 'file_fields': ['subor']},
            'technicke_specifikacie': {'model': TechnickaSpecifikacia, 'date_field': None, 'file_fields': ['subor_manual', 'subor_schema', 'subor_certifikat']},
            'povolenia_vstupov': {'model': PovolenieNaVstup, 'date_field': 'datum_vstupu', 'file_fields': ['vygenerovany_dokument']},
            'qr_kody_zariadeni': {'model': ServisovaneZariadenie, 'date_field': None, 'file_fields': ['qr_kod']},
        }

        cfg = config.get(zaloha.typ_zalohy)
        if not cfg:
            raise ValueError(f"Neznámy typ pre ZIP export: {zaloha.typ_zalohy}")

        # 🔍 Vytvorenie QuerySetu s aplikovaním dátumových filtrov zákazníka
        qs = cfg['model'].objects.all()
        
        # 🛡️ OCHRANA: Ak model nemá dátumové pole (date_field je None), preskočíme filtrovanie!
        if cfg.get('date_field'):
            if zaloha.obsahuje_data_od:
                qs = qs.filter(**{f"{cfg['date_field']}__gte": zaloha.obsahuje_data_od})
            if zaloha.obsahuje_data_do:
                qs = qs.filter(**{f"{cfg['date_field']}__lte": zaloha.obsahuje_data_do})

        # 📦 Otvorenie dočasného ZIP súboru v režime "Append" alebo "Write"
        with zipfile.ZipFile(temp_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zabalene_subory = set() # Ochrana proti duplicitným názvom

            # 🛡️ OOM OCHRANA: Použitie .iterator(chunk_size=2000) zabráni pádu RAM pri veľkom množstve dát
            for obj in qs.iterator(chunk_size=2000):
                for file_field_name in cfg['file_fields']:
                    file_field = getattr(obj, file_field_name, None)
                    
                    if file_field and file_field.name:
                        try:
                            # 🛡️ Ochrana: Existuje ten súbor fyzicky na disku (alebo v S3)?
                            if file_field.storage.exists(file_field.name):
                                povodny_nazov = os.path.basename(file_field.name)
                                
                                # Unikátny názov: Pridáme ID objektu do názvu, ak by sa volali rovnako
                                cisty_nazov = f"ID{obj.id}_{povodny_nazov}"
                                
                                if cisty_nazov not in zabalene_subory:
                                    with file_field.open('rb') as f:
                                        # OOM OCHRANA: Zápis po chunkoch (streamovanie) namiesto načítania do RAM
                                        with zipf.open(cisty_nazov, 'w') as dest:
                                            shutil.copyfileobj(f, dest)
                                    zabalene_subory.add(cisty_nazov)
                        except Exception as e:
                            # Tichý preskok (Súbor chýba/poškodený), nezabijeme celý export
                            aktualna_poznamka = zaloha.poznamka or ""
                            zaloha.poznamka = aktualna_poznamka + f"\n⚠️ Preskočený súbor ID {obj.id}: {str(e)}"

            # 🛑 Ak je ZIP prázdny, pridáme aspoň info texták, inak by bol ZIP poškodený
            if not zabalene_subory:
                zipf.writestr("INFO_ISAS.txt", "Pre zvolené obdobie neboli nájdené žiadne súbory na export.")
                
        # ZIP súbor sa bezpečne uzavrie vďaka 'with' bloku ešte PREDTÝM, než sa s ním ďalej manipuluje.

    # =========================================================================
    # 📊 2. GENEROVANIE EXCEL/CSV (ÚČTOVNÍCTVO, FLEET, SKLAD)
    # =========================================================================
    def _vygeneruj_csv_report(self, zaloha, temp_file_path):
        """
        Vytvorí CSV súbor kódovaný vo Windows UTF-8-SIG, 
        aby ho Excel u zákazníčky okamžite prečítal aj so slovenskými znakmi!
        """
        Tankovanie = apps.get_model('Evidencia', 'Tankovanie')
        EvidenciaJazd = apps.get_model('Evidencia', 'EvidenciaJazd')
        EvidenciaUhrad = apps.get_model('Evidencia', 'EvidenciaUhrad')
        StockMovement = apps.get_model('Evidencia', 'StockMovement')
        CenovyKatalog = apps.get_model('Evidencia', 'CenovyKatalog')
        Uloha = apps.get_model('Evidencia', 'Uloha')

        with open(temp_file_path, 'w', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.writer(csvfile, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)

            if zaloha.typ_zalohy == 'kniha_jazd':
                qs = EvidenciaJazd.objects.all()
                if zaloha.obsahuje_data_od: qs = qs.filter(datum_cas_start__date__gte=zaloha.obsahuje_data_od)
                if zaloha.obsahuje_data_do: qs = qs.filter(datum_cas_start__date__lte=zaloha.obsahuje_data_do)
                
                # PRIDANÉ ERP STĹPCE PRE ÚČTOVNÍČKU
                writer.writerow(['ID', 'Dátum štartu', 'Vozidlo (ŠPZ)', 'Vodič', 'Trasa (Štart)', 'Trasa (Cieľ)', 'Prejdené KM', 'Účel cesty', 'Typ jazdy', 'Vyfakturované', 'Štát štartu', 'Štát cieľa', 'Nocľah', 'Bezplatná strava', 'ERP Zákazka'])
                # 🛡️ OOM OCHRANA: .iterator(chunk_size=2000)
                # Pridané select_related pre optimalizáciu databázových dopytov na relačné polia trás
                for j in qs.select_related('vozidlo', 'zamestnanec', 'sukromna_osoba', 'zakaznik_start', 'dodavatel_start', 'filialka_start', 'zakaznik', 'dodavatel_ciel', 'filialka_ciel').iterator(chunk_size=2000):
                    vodic = str(j.zamestnanec) if j.zamestnanec else (str(j.sukromna_osoba) if j.sukromna_osoba else "")
                    
                    # ISAS CORE: Dynamické zistenie skutočného miesta štartu a cieľa z relačných polí
                    start_val = j.zakaznik_start or j.dodavatel_start or j.filialka_start or j.zamestnanec_start or j.sukromna_osoba_start or j.miesto_startu
                    start = str(start_val) if start_val else ""
                    ciel_val = j.zakaznik or j.dodavatel_ciel or j.filialka_ciel or j.zamestnanec_ciel or j.sukromna_osoba_ciel or j.miesto_ciela
                    ciel = str(ciel_val) if ciel_val else ""
                    
                    # PRIDANÉ DÁTA PRE ERP
                    noclah = 'Áno' if j.noclah_zabezpeceny else 'Nie'
                    strava = 'Áno' if j.bezplatne_stravovanie else 'Nie'
                    zakazka = j.erp_zakazka if j.erp_zakazka else ""
                    
                    local_start = timezone.localtime(j.datum_cas_start) if (j.datum_cas_start and timezone.is_aware(j.datum_cas_start)) else j.datum_cas_start
                    datum_startu = local_start.strftime('%d.%m.%Y %H:%M') if local_start else ""
                    spz_vozidla = j.vozidlo.spz if j.vozidlo else ""
                    writer.writerow([j.id, datum_startu, spz_vozidla, vodic, start, ciel, j.prejdene_km, j.ucel_cesty, j.get_typ_jazdy_display(), 'Áno' if j.vyfakturovane else 'Nie', j.stat_startu, j.stat_ciela, noclah, strava, zakazka])

            elif zaloha.typ_zalohy == 'evidencia_uhrad':
                qs = EvidenciaUhrad.objects.all()
                if zaloha.obsahuje_data_od: qs = qs.filter(datum_uhrady__gte=zaloha.obsahuje_data_od)
                if zaloha.obsahuje_data_do: qs = qs.filter(datum_uhrady__lte=zaloha.obsahuje_data_do)
                
                writer.writerow(['ID', 'Dátum', 'Typ úhrady', 'Číslo dokladu', 'Spárovaná Faktúra (Predaj)', 'Spárovaná Faktúra (Nákup)', 'Suma', 'Mena', 'Poznámka'])
                # 🛡️ OOM OCHRANA: .iterator(chunk_size=2000)
                for u in qs.select_related('vystavena_faktura', 'prijata_faktura').iterator(chunk_size=2000):
                    # OCHRANA: Ak je faktúra v stave DRAFT, nemá ešte cislo_faktury. Použijeme ID.
                    vyst_fa = (u.vystavena_faktura.cislo_faktury or f"DRAFT ID {u.vystavena_faktura.id}") if u.vystavena_faktura else ""
                    prij_fa = f"ID {u.prijata_faktura.id}" if u.prijata_faktura else ""
                    datum_uhrady_str = u.datum_uhrady.strftime('%d.%m.%Y') if u.datum_uhrady else ""
                    writer.writerow([u.id, datum_uhrady_str, u.get_typ_uhrady_display(), u.cislo_dokladu, vyst_fa, prij_fa, u.suma_uhrady, u.mena, u.poznamka])

            elif zaloha.typ_zalohy == 'skladove_pohyby':
                qs = StockMovement.objects.all()
                if zaloha.obsahuje_data_od: qs = qs.filter(timestamp__date__gte=zaloha.obsahuje_data_od)
                if zaloha.obsahuje_data_do: qs = qs.filter(timestamp__date__lte=zaloha.obsahuje_data_do)
                
                writer.writerow(['Dátum a Čas', 'Akcia', 'Produkt', 'Množstvo', 'Merná jednotka', 'Typ ERP', 'Kategória', 'Používateľ'])
                # 🛡️ OOM OCHRANA: .iterator(chunk_size=2000)
                for s in qs.select_related('user').iterator(chunk_size=2000):
                    uzivatel = str(s.user) if s.user else "Systém"
                    local_timestamp = timezone.localtime(s.timestamp) if (s.timestamp and timezone.is_aware(s.timestamp)) else s.timestamp
                    cas_pohybu = local_timestamp.strftime('%d.%m.%Y %H:%M') if local_timestamp else ""
                    writer.writerow([cas_pohybu, s.get_action_display(), s.product_name, s.count, s.merna_jednotka, s.get_typ_dokladu_erp_display(), f"{s.category} / {s.subcategory}", uzivatel])

            elif zaloha.typ_zalohy == 'cenovy_katalog':
                qs = CenovyKatalog.objects.all()
                writer.writerow(['ID Katalogu', 'Názov (Vyhľadávanie)', 'Nákupná cena', 'Marža (%)', 'Predajná cena'])
                # 🛡️ OOM OCHRANA: .iterator(chunk_size=2000)
                for c in qs.iterator(chunk_size=2000):
                    writer.writerow([c.id, c.search_name, c.nakupna_cena, c.marza_percento, c.predajna_cena])
                    
            elif zaloha.typ_zalohy == 'ulohy_a_vykazy':
                qs = Uloha.objects.filter(dokoncene=True)
                if zaloha.obsahuje_data_od: qs = qs.filter(zaciatok__date__gte=zaloha.obsahuje_data_od)
                if zaloha.obsahuje_data_do: qs = qs.filter(zaciatok__date__lte=zaloha.obsahuje_data_do)
                
                # PRIDANÉ HLAVIČKY PRE REKLAMÁCIE
                writer.writerow(['ID Úlohy', 'Začiatok', 'Koniec', 'Zákazník', 'Technici', 'Typy servisu', 'Detaily', 'Je Reklamácia', 'Dátum prijatia reklamácie'])
                # 🛡️ OOM OCHRANA: .iterator(chunk_size=2000)
                for u in qs.select_related('zakaznik').prefetch_related('technici', 'typ_servisu').iterator(chunk_size=2000):
                    techs = ", ".join([str(t) for t in u.technici.all()])
                    typy = ", ".join([str(t) for t in u.typ_servisu.all()])
                    zaka = str(u.zakaznik) if u.zakaznik else "Interná úloha"
                    
                    local_zaciatok = timezone.localtime(u.zaciatok) if (u.zaciatok and timezone.is_aware(u.zaciatok)) else u.zaciatok
                    zaciatok_str = local_zaciatok.strftime('%d.%m.%Y %H:%M') if local_zaciatok else ""
                    
                    if u.koniec:
                        local_koniec = timezone.localtime(u.koniec) if (u.koniec and timezone.is_aware(u.koniec)) else u.koniec
                        kon = local_koniec.strftime('%d.%m.%Y %H:%M') if local_koniec else ""
                    else:
                        kon = ""
                    
                    # PRIDANÉ DÁTA PRE REKLAMÁCIE
                    reklamacia_text = "Áno" if u.je_reklamacia else "Nie"
                    if u.datum_prijatia_reklamacie:
                        local_rekl = timezone.localtime(u.datum_prijatia_reklamacie) if (u.datum_prijatia_reklamacie and timezone.is_aware(u.datum_prijatia_reklamacie)) else u.datum_prijatia_reklamacie
                        datum_reklamacie = local_rekl.strftime('%d.%m.%Y %H:%M') if local_rekl else ""
                    else:
                        datum_reklamacie = ""
                    
                    detaily = str(u.blizsie_informacie).replace('\n', ' ') if u.blizsie_informacie else ""
                    writer.writerow([u.id, zaciatok_str, kon, zaka, techs, typy, detaily, reklamacia_text, datum_reklamacie])

    # =========================================================================
    # 🗄️ 3. GENEROVANIE DATABÁZOVÉHO DUMPU (JSON ZABALENÝ V ZIPe)
    # =========================================================================
    def _vygeneruj_databazovy_dump(self, zaloha, temp_file_path):
        """
        Extrémne silná funkcia, ktorá urobí Snapshot celej databázy.
        🛡️ PRODUKČNÁ ÚPRAVA: JSON sa najskôr vygeneruje a potom sa okamžite 
        zabalí do ZIP archívu, čím ušetríme až 90% miesta na disku!
        """
        # 1. Vytvoríme dočasný čistý JSON súbor
        # Vytvoríme a hneď uvoľníme file descriptor (prevencia PermissionError)
        temp_json_fd, temp_json_path = tempfile.mkstemp(suffix='.json', text=True)
        os.close(temp_json_fd)

        try:
            with open(temp_json_path, 'w', encoding='utf-8') as temp_json:
                call_command(
                    'dumpdata', 
                    exclude=['contenttypes', 'auth.Permission', 'admin.LogEntry', 'sessions'], 
                    # indent=2, <-- ODSTRÁNENÉ: Formátovanie zabíja RAM pri veľkých DB!
                    stdout=temp_json
                )

            # 2. Zabalíme tento JSON do nášho hlavného ZIP súboru
            with zipfile.ZipFile(temp_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Vnútri ZIPu sa súbor bude volať 'kompletna_databaza_dump.json'
                zipf.write(temp_json_path, arcname='kompletna_databaza_dump.json')
        finally:
            # 3. Upraceme nekomprimovaný obrovský JSON z disku
            if os.path.exists(temp_json_path):
                os.remove(temp_json_path)