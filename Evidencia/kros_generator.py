import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from django.apps import apps

from Evidencia.utils_helper import get_firemna_mena

logger = logging.getLogger(__name__)

class KrosOmegaTXTGenerator:
    """
    💎 ENTERPRISE TXT GENERÁTOR PRE KROS OMEGA
    Generuje striktne formátované textové súbory (TSV - Tab Separated Values) 
    podľa oficiálnej špecifikácie KROS.
    
    Poznámka: Názvy metód (napr. vygeneruj_faktury_xml) sú zachované z dôvodu 
    spätnej kompatibility s ERPFactory a Celery taskom, aj keď vracajú TXT.
    """

    def __init__(self):
        ERPExportNastavenia = apps.get_model('Evidencia', 'ERPExportNastavenia')
        # 🏗️ ARCHITECT FIX: Použitie bezpečnej Singleton metódy z modelu
        self.nastavenia = ERPExportNastavenia.get_settings()
        
        if not self.nastavenia:
            logger.error("Chýbajú ERPExportNastavenia v databáze.")
            raise ValueError("Kritická chyba: ERPExportNastavenia nie sú vytvorené.")

        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        self.identita = FiremnaIdentita.objects.first()

        # 🛡️ ISAS ARCHITECT (Defense-in-Depth): Overenie sídla pred inicializáciou TXT enginu
        krajina_iso = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else 'SK'
        if krajina_iso == 'NEZADANE':
            raise ValueError("🚨 Kritická chyba: V systéme chýba Profil spoločnosti (Firemná identita). Pred exportom do Omegy vyplňte IČO a sídlo.")
        if krajina_iso != 'SK':
            raise ValueError(f"🚨 Neplatný export: KROS Omega TXT je viazaná striktne na slovenskú daňovú legislatívu a slovenský KV DPH. Firma so sídlom v '{krajina_iso}' musí použiť Pohoda XML.")
        self.krajina_iso = krajina_iso

        self.moje_ico = getattr(self.identita, 'ico', '')
        
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        self.globalne_nastavenia = GlobalneNastavenia.objects.first()
        self.GlobalneNastavenia = GlobalneNastavenia
        self.zakladna_dph = Decimal(str(self.globalne_nastavenia.zakladna_sadzba_dph)) if self.globalne_nastavenia and self.globalne_nastavenia.zakladna_sadzba_dph is not None else GlobalneNastavenia.get_zakladna_dph()
        self.znizena_dph = Decimal(str(self.globalne_nastavenia.znizena_sadzba_dph)) if self.globalne_nastavenia and self.globalne_nastavenia.znizena_sadzba_dph is not None else GlobalneNastavenia.get_znizena_dph()

    def get_file_prefix(self):
        return "omega"

    def get_file_extension(self):
        """Kritické pre Celery task: Povie mu, aby súbor v ZIPe uložil ako .txt"""
        return "txt"

    # =========================================================================
    # 🛡️ POMOCNÉ BEZPEČNOSTNÉ A FORMÁTOVACIE METÓDY
    # =========================================================================
    def _format_date(self, datum):
        """Omega vyžaduje dátum vo formáte DD.MM.YYYY."""
        if not datum: return ""
        if isinstance(datum, datetime):
            if timezone.is_aware(datum):
                datum = timezone.localtime(datum)
            return datum.strftime('%d.%m.%Y')
        return datum.strftime('%d.%m.%Y')

    def _format_decimal(self, suma):
        """Omega vyžaduje bodku ako oddelovač desatinných miest"""
        if suma is None or suma == "": return "0.00"
        try:
            suma_cista = str(suma).replace(',', '.').strip()
            return str(Decimal(suma_cista).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        except Exception:
            return "0.00"

    def _sanitize_string(self, text, max_length=None):
        """
        Odstráni Emoji a neviditeľné znaky, ktoré by zhodili Windows-1250 parser Omegy.
        """
        if not text: return ""
        text = str(text).replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
        # Prekódovanie do Windows-1250 s ignorovaním nepodporovaných znakov (Emoji)
        text = text.encode('windows-1250', errors='ignore').decode('windows-1250').strip()
        if max_length:
            return text[:max_length]
        return text

    def _get_iso_stat(self, stat_z_modelu=None):
        """
        🛡️ ISAS ARCHITECT: Čisté dynamické načítanie ISO kódu štátu cez DB model StatLokalizacia bez hardkódu.
        """
        fallback_iso = getattr(self, 'krajina_iso', '')
        if not stat_z_modelu:
            return fallback_iso

        raw_stat = str(stat_z_modelu).strip()
        if len(raw_stat) == 2 and raw_stat.isalpha():
            return raw_stat.upper()

        try:
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            if StatLokalizaciaModel and hasattr(StatLokalizaciaModel, 'rozpoznaj_iso'):
                iso = StatLokalizaciaModel.rozpoznaj_iso(raw_stat)
                if iso and iso not in ('NEZADANE', 'INYKRAJINA'):
                    return iso
        except Exception:
            pass

        return fallback_iso

    def _build_row(self, columns_array):
        """Spojí pole do jedného riadku oddeleného tabulátorom"""
        return "\t".join([str(col) for col in columns_array])

    def _split_account(self, ucet, fallback=""):
        """
        ARCHITECT FIX: Pomocná metóda na rozdelenie účtu na syntetiku (3 znaky) a analytiku.
        Obsahuje ochranu voči textovým skratkám z Pohody (napr. 'pokladňa' -> '211000').
        """
        if not ucet:
            return self._split_account(fallback) if fallback else ("", "")
            
        ucet_clean = str(ucet).replace('.', '').replace('/', '').replace(' ', '').strip()
        
        # Ak účet obsahuje písmená (napr. default 'pokladňa' z ERP Nastavení), použijeme fallback
        # 🛡️ Ochrana: Ak nie je číslo ALEBO má menej ako 3 znaky (Pohoda skratky '2', '3'), použi fallback
        if not ucet_clean.isdigit() or len(ucet_clean) < 3:
            return self._split_account(fallback) if fallback else ("", "")
            
        return ucet_clean[:3], ucet_clean[3:]

    def _get_partner_data(self, partner_obj):
        """Univerzálny extraktor dát pre akýkoľvek typ partnera"""
        je_fyzicka_osoba = False
        if hasattr(partner_obj, 'meno') and hasattr(partner_obj, 'priezvisko'):
            nazov = f"{partner_obj.meno} {partner_obj.priezvisko}"
            je_fyzicka_osoba = True
        elif hasattr(partner_obj, 'meno_a_priezvisko'):
            nazov = partner_obj.meno_a_priezvisko
            je_fyzicka_osoba = True
        else:
            nazov = getattr(partner_obj, 'Nazov_spolocnosti', 'Neznámy partner')

        return {
            'nazov': self._sanitize_string(nazov, 75),
            'ulica': self._sanitize_string(getattr(partner_obj, 'ulica_a_cislo', ''), 40),
            'mesto': self._sanitize_string(getattr(partner_obj, 'mesto', ''), 40),
            'psc': self._sanitize_string(getattr(partner_obj, 'psc', ''), 6).replace(" ", ""),
            'stat': self._get_iso_stat(getattr(partner_obj, 'stat', '')),
            'ico': self._sanitize_string(getattr(partner_obj, 'ico', ''), 12).replace(" ", ""),
            'dic': self._sanitize_string(getattr(partner_obj, 'dic', ''), 12).replace(" ", ""),
            'ic_dph': self._sanitize_string(getattr(partner_obj, 'ic_dph', ''), 20).replace(" ", ""),
            'iban': self._sanitize_string(getattr(partner_obj, 'iban', ''), 34).replace(" ", ""),
            'email': self._sanitize_string(getattr(partner_obj, 'email', getattr(partner_obj, 'email', '')), 100),
            'telefon': self._sanitize_string(getattr(partner_obj, 'telefon', getattr(partner_obj, 'telefon', '')), 20),
            'web': self._sanitize_string(getattr(partner_obj, 'domena', ''), 100),
            'je_fo': "T" if je_fyzicka_osoba else "F",
        }

    # =========================================================================
    # 1. ADRESÁR (ZÁKAZNÍCI, DODÁVATELIA, ZAMESTNANCI) -> T04
    # =========================================================================
    def vygeneruj_adresare_xml(self, queryset_partnerov):
        lines = []
        # Hlavička bloku
        lines.append(self._build_row(["R00", "T04"]))

        for partner in queryset_partnerov:
            p_data = self._get_partner_data(partner)
            
            # R01 pre T04 má presne 35 stĺpcov (Index 0 až 34)
            row = [""] * 35
            row[0] = "R01"
            row[1] = p_data['nazov']
            row[2] = p_data['ico']
            row[3] = p_data['ulica']
            row[4] = p_data['psc']
            row[5] = p_data['mesto']
            row[6] = p_data['dic']
            row[7] = p_data['stat']
            
            # ARCHITECT FIX: Index 10 je príznak "Fyzická osoba" (stĺpec hneď vedľa e-mailu)
            row[10] = p_data['je_fo']
            
            row[13] = p_data['email']
            # ARCHITECT FIX: Zápis webovej adresy partnera na index 14 podľa vzorovej schémy Omegy
            row[14] = p_data['web']
            
            # Omega vyžaduje rozpad IČ DPH na Štát (24) a samotné číslo (25 a 26 pre KV DPH)
            if p_data['ic_dph']:
                row[24] = p_data['ic_dph'][:2] if p_data['ic_dph'][:2].isalpha() else self.krajina_iso
                row[25] = p_data['ic_dph']
                row[26] = p_data['ic_dph']
                
            # ARCHITECT FIX: Presun príznaku Fyzickej osoby na správny index 31 podľa vzorky
            row[31] = p_data['je_fo']
            row[32] = p_data['iban']
            row[34] = "-1" # Kritický ukončovací príznak riadku v Omege
            
            lines.append(self._build_row(row))

            # ARCHITECT FIX: Ak existuje telefónne číslo, vygenerujeme podriadok R03 (kód kontaktu T22) presne podľa schémy Omegy
            if p_data['telefon']:
                import re
                tel_raw = str(p_data['telefon']).strip()
                tel_ciste = re.sub(r'[^\d+]', '', tel_raw)
                
                predvolba = "00421"
                telefon_final = tel_ciste

                # 1. AK MÁ ČÍSLO MEDZINÁRODNÝ FORMÁT (+ alebo 00), dynamicky oddelíme predvoľbu
                if tel_ciste.startswith('+') or tel_ciste.startswith('00'):
                    cifry = tel_ciste[1:] if tel_ciste.startswith('+') else tel_ciste[2:]
                    if cifry.startswith(('421', '420', '380', '359', '385', '386', '370', '371', '372')):
                        predvolba = f"00{cifry[:3]}"
                        telefon_final = cifry[3:]
                    elif len(cifry) >= 10:
                        predvolba = f"00{cifry[:2]}"
                        telefon_final = cifry[2:]
                    elif len(cifry) > 0:
                        predvolba = f"00{cifry[:3]}"
                        telefon_final = cifry[3:]
                else:
                    # 2. Tuzemské číslo bez medzinárodnej predvoľby (napr. 0903123456 -> predvoľba 00421, číslo 903123456)
                    telefon_final = re.sub(r'^0+', '', tel_ciste) if tel_ciste.startswith('0') else tel_ciste
                    predvolba = "00421"

                # ARCHITECT FIX: Omega striktne vyžaduje prázdny riadok R02 (Hlavička kontaktu) pred riadkom R03!
                lines.append(self._build_row(["R02", ""]))
                
                # ARCHITECT FIX: Rozpad mena kontaktnej osoby (ošetrené k/K) pre potreby Omegy
                kontakt_osoba = getattr(partner, 'kontaktna_osoba', getattr(partner, 'kontaktna_osoba', ''))
                ko_meno = getattr(partner, 'meno', '')
                ko_priezvisko = getattr(partner, 'priezvisko', '')

                if not ko_priezvisko and kontakt_osoba:
                    casti = str(kontakt_osoba).strip().split(" ", 1)
                    ko_meno = casti[0] if len(casti) == 2 else ""
                    ko_priezvisko = casti[1] if len(casti) == 2 else casti[0]
                elif not ko_priezvisko:
                    ko_priezvisko = p_data['nazov']

                r03 = [""] * 8
                r03[0] = "R03"
                r03[1] = "T22"
                r03[2] = ko_meno
                r03[3] = ko_priezvisko
                r03[4] = predvolba
                r03[5] = telefon_final
                
                lines.append(self._build_row(r03))

        return "\r\n".join(lines).encode('windows-1250', errors='replace')

    # =========================================================================
    # 2. VYSTAVENÉ FAKTÚRY (PREDAJ) -> T01
    # =========================================================================
    def vygeneruj_faktury_xml(self, queryset_faktur):
        lines = []
        lines.append(self._build_row(["R00", "T01"]))

        for faktura in queryset_faktur:
            # --- HLAVIČKA FAKTÚRY (R01 - presne 95 stĺpcov podľa KROS špecifikácie) ---
            r01 = [""] * 95
            r01[0] = "R01"
            r01[1] = self._sanitize_string(faktura.cislo_faktury or f"FA-{faktura.id}", 20)
            
            # ARCHITECT FIX: Správne mapovanie partnera
            if getattr(faktura, 'odberatel_nazov', None):
                r01[2] = self._sanitize_string(faktura.odberatel_nazov, 75)
                r01[3] = self._sanitize_string(faktura.odberatel_ico, 12).replace(" ", "")
                r01[24] = self._sanitize_string(faktura.odberatel_ulica, 40)
                r01[25] = self._sanitize_string(faktura.odberatel_psc, 6).replace(" ", "")
                r01[26] = self._sanitize_string(faktura.odberatel_mesto, 40)
                r01[27] = self._sanitize_string(faktura.odberatel_dic, 12).replace(" ", "")
                # 🏗️ ARCHITECT FIX: Dynamické určenie štátu - ak je zahraničný, zapíšeme ho, ak domáci (podľa sídla firmy), necháme prázdne pre tuzemský režim
                stat_kod = self._get_iso_stat(getattr(faktura, 'odberatel_stat', getattr(faktura, 'odberatel_krajina', '')))
                r01[28] = stat_kod if stat_kod != self.krajina_iso else ""
                ic_dph = getattr(faktura, 'odberatel_ic_dph', '')
            elif faktura.zakaznik:
                p_data = self._get_partner_data(faktura.zakaznik)
                r01[2] = p_data['nazov']
                r01[3] = p_data['ico']
                r01[24] = p_data['ulica']
                r01[25] = p_data['psc']
                r01[26] = p_data['mesto']
                r01[27] = p_data['dic']
                # 🏗️ ARCHITECT FIX: Dynamické určenie štátu - ak je zahraničný, zapíšeme ho, ak domáci (podľa sídla firmy), necháme prázdne pre tuzemský režim
                r01[28] = p_data['stat'] if p_data['stat'] != self.krajina_iso else ""
                ic_dph = p_data['ic_dph']
            else:
                ic_dph = ""

            r01[4] = self._format_date(faktura.datum_vystavenia)
            r01[5] = self._format_date(faktura.datum_splatnosti)
            r01[6] = self._format_date(faktura.datum_dodania)
            
            # Typ dokladu a Evidenčné kódy (0 = OF, 1 = zOF/Záloha, 4 = DO/Dobropis)
            if getattr(faktura, 'typ_dokladu', '') == 'ZALOHA': 
                r01[17], r01[18], r01[19] = "1", "zOF", "zOF"
            elif getattr(faktura, 'typ_dokladu', '') == 'DOBROPIS': 
                r01[17], r01[18], r01[19] = "4", "DO", "DO"
            else:
                r01[17], r01[18], r01[19] = "0", "OF", "OF"
            
            # ARCHITECT FIX: Zohľadnenie 'kod_clenenia_dph' z modelu VystavenaFaktura s podporou neutrálnych SaaS kódov
            kod_dph = getattr(faktura, 'kod_clenenia_dph', 'UD')
            bez_dph = faktura.prenos_danovej_povinnosti or kod_dph in ['UN', 'UK', 'UE', 'UZ', 'U0', 'VAT_REVERSE', 'UDpdp', 'VAT_NONE']
            
            # Rozpad DPH na hlavičke (Indexy 7 až 16)
            if bez_dph:
                r01[7] = self._format_decimal(faktura.celkom_bez_dph) # Základ 0% / Oslobodené
            else:
                # 🏗️ ARCHITECT FIX: Presná agregácia rozpadu DPH z položiek pre hlavičku Omegy
                zaklad_vyssi, dph_vyssie = Decimal('0.00'), Decimal('0.00')
                zaklad_nizsi, dph_nizsie = Decimal('0.00'), Decimal('0.00')
                zaklad_nulovy = Decimal('0.00')
                for pol in faktura.polozky.all():
                    cena_bez = Decimal(str(pol.cena_po_zlave_bez_dph or 0))
                    pol_sadzba = pol.sadzba_dph if pol.sadzba_dph is not None else self.zakladna_dph
                    if pol_sadzba == 0:
                        zaklad_nulovy += cena_bez
                    elif pol_sadzba >= self.zakladna_dph:
                        dph_hodnota = (cena_bez * (Decimal(str(pol_sadzba)) / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        zaklad_vyssi += cena_bez
                        dph_vyssie += dph_hodnota
                    else:
                        dph_hodnota = (cena_bez * (Decimal(str(pol_sadzba)) / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        zaklad_nizsi += cena_bez
                        dph_nizsie += dph_hodnota

                if zaklad_nulovy > 0:
                    r01[7] = self._format_decimal(zaklad_nulovy)
                if zaklad_vyssi > 0 or (zaklad_nizsi == 0 and zaklad_nulovy == 0):
                    r01[8] = self._format_decimal(zaklad_vyssi)
                    r01[12] = f"{self.zakladna_dph:g}"
                    r01[14] = self._format_decimal(dph_vyssie)
                if zaklad_nizsi > 0:
                    r01[9] = self._format_decimal(zaklad_nizsi)
                    r01[13] = f"{self.znizena_dph:g}"
                    r01[15] = self._format_decimal(dph_nizsie)
                
            # ARCHITECT FIX: Pridanie poznámky pre klienta do textu dokladu (Index 23)
            if getattr(faktura, 'poznamka_pre_klienta', None):
                r01[23] = self._sanitize_string(faktura.poznamka_pre_klienta, 60)
            
            r01[16] = self._format_decimal(faktura.celkom_s_dph) # Suma spolu CM/TM
            
            # ARCHITECT FIX: Zápis Účtu pohľadávok (napr. 311000) z ERP Nastavení do hlavičky faktúry
            ucet_pohladavky_syn, ucet_pohladavky_ana = self._split_account(self.nastavenia.ucet_pohladavky, fallback="311000") if self.nastavenia else ("311", "000")
            r01[20] = ucet_pohladavky_syn
            r01[21] = ucet_pohladavky_ana
            
            r01[42] = self._format_decimal(faktura.celkom_s_dph) # Suma spolu TM
            
            # Ďalšie polia hlavičky
            if faktura.variabilny_symbol:
                r01[32] = self._sanitize_string(faktura.variabilny_symbol, 20)
                
            # 🏗️ ISAS ARCHITECT FIX: Prepojenie Dobropisu na pôvodnú faktúru v Omege (Index 33)
            if getattr(faktura, 'typ_dokladu', 'FAKTURA') == 'DOBROPIS' and faktura.povodna_faktura:
                r01[33] = self._sanitize_string(faktura.povodna_faktura.cislo_faktury, 20)
            
            r01[34] = self._sanitize_string(faktura.vystavil_meno, 30)
            
            forma_uhrady_map = {'PREVOD': 'Prevodný príkaz', 'HOTOVOST': 'Hotovosť', 'KARTA': 'Platobná karta'}
            r01[37] = forma_uhrady_map.get(faktura.sposob_uhrady, 'Prevodný príkaz')
            
            domaca_mena = getattr(self.identita, 'predvolena_mena', None) or get_firemna_mena()
            r01[39] = self._sanitize_string(getattr(faktura, 'mena', domaca_mena) or domaca_mena, 5)
            r01[40] = "1" # Kurzové množstvo
            r01[41] = "1" # Kurz
            
            # IČ DPH rozpad
            if ic_dph:
                r01[47] = ic_dph[:2] if ic_dph[:2].isalpha() else self.krajina_iso
                r01[48] = ic_dph
                
            lines.append(self._build_row(r01))

            # --- POLOŽKY FAKTÚRY (R02 - presne 57 stĺpcov) ---
            for pol in faktura.polozky.all():
                r02 = [""] * 57
                r02[0] = "R02"
                
                # 🧠 ISAS SMART FALLBACK: Do účtovníctva musí ísť presne ten istý názov ako na PDF
                zobrazeny_nazov = pol.obchodny_nazov if getattr(pol, 'obchodny_nazov', None) else pol.nazov
                r02[1] = self._sanitize_string(zobrazeny_nazov, 100)
                
                r02[2] = self._format_decimal(pol.mnozstvo)
                r02[3] = self._sanitize_string(pol.merna_jednotka, 5)
                
                # Jednotková cena bez DPH
                mnozstvo = Decimal(str(pol.mnozstvo)) if pol.mnozstvo else Decimal('1')
                suma_bez_dph = Decimal(str(pol.cena_po_zlave_bez_dph or 0))
                cena_zmj = (suma_bez_dph / mnozstvo).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if mnozstvo != Decimal('0.00') else Decimal('0.00')
                r02[4] = self._format_decimal(cena_zmj)
                
                # ARCHITECT FIX: Dynamické určenie sadzby DPH podľa modelu PolozkaVystavenejFaktury s podporou neutrálnych SaaS kódov
                kod_dph = getattr(faktura, 'kod_clenenia_dph', 'UD')
                bez_dph = faktura.prenos_danovej_povinnosti or kod_dph in ['UN', 'UK', 'UE', 'UZ', 'U0', 'VAT_REVERSE', 'UDpdp', 'VAT_NONE']
                
                # 🏗️ ARCHITECT FIX: Podpora pre sadzby podľa GlobalneNastavenia
                pol_sadzba = pol.sadzba_dph if pol.sadzba_dph is not None else self.zakladna_dph
                if bez_dph or pol_sadzba == 0:
                    r02[5] = "0"
                elif pol_sadzba < self.zakladna_dph:
                    r02[5] = "N" # Nižšia sadzba (napr. 19% alebo 5%)
                else:
                    r02[5] = "V" # Vyššia sadzba (napr. 23%)
                
                r02[7] = self._format_decimal(suma_bez_dph) # Celková cena položky bez DPH
                
                # Účtovná analytika (Index 13 = Syntetika, Index 14 = Analytika)
                fallback_ucet = '604000' if getattr(pol, 'typ_polozky', 'MAT') == 'MAT' else '602000'
                syn, ana = self._split_account(pol.ucet_vynosov, fallback=fallback_ucet)
                r02[13] = syn
                r02[14] = ana 
                
                # Duplicita MJ a Množstva (Omega to vyžaduje aj na konci riadku)
                r02[37] = self._sanitize_string(pol.merna_jednotka, 5)
                r02[38] = self._format_decimal(pol.mnozstvo)
                
                # 🛡️ ISAS ERP GATEWAY: Podpora neutrálnych SaaS kódov aj národných skratiek pre KV DPH v Omege
                kod_dph = getattr(faktura, 'kod_clenenia_dph', 'UD')
                je_prenos = faktura.prenos_danovej_povinnosti or kod_dph in ['UK', 'VAT_REVERSE', 'UDpdp']
                if not je_prenos and kod_dph in ['UD', 'VAT_OUT']:
                    r02[52] = "A1"
                elif je_prenos:
                    r02[52] = "A2" # Prenesenie daňovej povinnosti (§ 69)
                
                lines.append(self._build_row(r02))

        return "\r\n".join(lines).encode('windows-1250', errors='replace')

    # =========================================================================
    # 3. PRIJATÉ FAKTÚRY (NÁKUP) -> T00 (Evidencia účtovných dokladov)
    # =========================================================================
    def vygeneruj_prijate_faktury_xml(self, queryset_faktur):
        lines = []
        lines.append(self._build_row(["R00", "T00"]))

        for faktura in queryset_faktur:
            # --- HLAVIČKA DOKLADU (R01 - presne 71 stĺpcov podľa KROS špecifikácie) ---
            r01 = [""] * 71
            r01[0] = "R01"
            r01[1] = "130" # Okruh: Došlé faktúry
            r01[2] = "DF"  # Evidencia
            r01[3] = "DF"  # Skratka
            
            interne_cislo = f"DF-{faktura.id}"
            r01[4] = self._sanitize_string(interne_cislo, 20)
            
            externe_cislo = faktura.variabilny_symbol if faktura.variabilny_symbol else interne_cislo
            r01[5] = self._sanitize_string(externe_cislo, 20)
            
            ic_dph = ""
            stat_dodavatela = self.krajina_iso
            if faktura.dodavatel:
                p_data = self._get_partner_data(faktura.dodavatel)
                r01[6] = p_data['nazov']
                r01[7] = p_data['ico']
                r01[8] = p_data['ic_dph']
                ic_dph = p_data['ic_dph']
                stat_dodavatela = p_data['stat']

            datum_prijatia = self._format_date(faktura.datum_prijatia)
            r01[9] = datum_prijatia  # Vyhotovenie
            r01[10] = datum_prijatia # Prijatie
            r01[11] = self._format_date(faktura.datum_splatnosti) # Splatnosť
            r01[12] = datum_prijatia # UZP
            r01[13] = datum_prijatia # Dátum DPH
            
            predvolena_mena = getattr(self.identita, 'predvolena_mena', None) or get_firemna_mena()
            r01[14] = self._sanitize_string(getattr(faktura, 'mena', predvolena_mena) or predvolena_mena, 5)
            r01[15] = "1" # Kurzové množstvo
            r01[16] = "1" # Kurz
            r01[17] = "0"
            
            suma_spolu = faktura.suma_na_uhradu or 0
            r01[18] = self._format_decimal(suma_spolu) # Suma CM
            r01[19] = self._format_decimal(suma_spolu) # Suma TM
            
            r01[20] = f"{self.znizena_dph:g}" # Nižšia sadzba (napr. 19 alebo 8)
            r01[21] = f"{self.zakladna_dph:g}" # Vyššia sadzba (napr. 23 alebo 21)
            
            r01[22] = "0.00"  # Základ DPH základný
            r01[23] = "0.00"  # Základ DPH znížený
            r01[24] = "0.00"  # Základ DPH znížený 2 (5%)
            r01[25] = "0.00"  # Základ DPH nulový / oslobodený
            r01[26] = "0.00"  # Suma DPH základná
            r01[27] = "0.00"  # Suma DPH znížená
            r01[28] = "0.00"  # Suma DPH znížená 2 (5%)
            r01[29] = "Administrátor" # Vystavil

            sadzba_dph = self.zakladna_dph
            polozky_log = getattr(faktura, 'polozky_log', None)
            if polozky_log is not None and hasattr(polozky_log, 'exists') and polozky_log.exists():
                prva_pol = polozky_log.first()
                raw_pol_dph = getattr(prva_pol, 'navrhovana_sadzba_dph', getattr(prva_pol, 'sadzba_dph', None))
                if raw_pol_dph is not None:
                    sadzba_dph = Decimal(str(raw_pol_dph))

            kod_dph = getattr(faktura, 'kod_clenenia_dph', 'PD')
            je_mimo_dph = kod_dph in ['PN', 'U0', 'VAT_NONE']
            
            if je_mimo_dph:
                r01[25] = self._format_decimal(suma_spolu)
                zaklad = Decimal(str(suma_spolu))
                dph = Decimal('0.00')
            elif kod_dph in ['EU', 'PP', 'PZ', 'VAT_REVERSE']:
                r01[22] = self._format_decimal(suma_spolu)
                zaklad = Decimal(str(suma_spolu))
                dph = Decimal('0.00')
            else: 
                koeficient = Decimal('1.00') + (sadzba_dph / Decimal('100.00'))
                zaklad = (Decimal(str(suma_spolu)) / koeficient).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if koeficient > Decimal('0.00') else Decimal(str(suma_spolu))
                dph = Decimal(str(suma_spolu)) - zaklad
                
                if sadzba_dph >= self.zakladna_dph:
                    r01[22] = self._format_decimal(zaklad)
                    r01[26] = self._format_decimal(dph)
                else:
                    r01[23] = self._format_decimal(zaklad)
                    r01[27] = self._format_decimal(dph)
            
            # 🏗️ ARCHITECT FIX: Presné odsadenie (Time musí byť na 38)
            if isinstance(faktura.datum_prijatia, datetime):
                ts = timezone.localtime(faktura.datum_prijatia) if timezone.is_aware(faktura.datum_prijatia) else faktura.datum_prijatia
                r01[38] = ts.strftime('%H:%M:%S')
            else:
                r01[38] = "12:00:00"
                
            text_dokladu = self._sanitize_string(getattr(faktura, 'poznamka_pre_klienta', getattr(faktura, 'poznamka', '')) or "Došlá faktúra", 60)
            r01[40] = text_dokladu
            r01[41] = "14" # Typ dokladu: Došlá faktúra
            r01[42] = ""   # OSS Štát (musí byť prázdny)
            r01[43] = stat_dodavatela if stat_dodavatela else self.krajina_iso # Štát DPH
            r01[44] = ic_dph # IČ DPH
            
            # Povinný koncový balast pre EUD
            r01[45] = "0.0000"
            r01[46] = "0"
            r01[47] = "X"
            r01[48] = "(Nedefinované)"
            r01[49] = "HS"
            r01[50] = "0"
            r01[51] = "X"
            r01[52] = "(Nedefinované)"
            r01[53] = self._sanitize_string(externe_cislo, 20)
            r01[57] = "0"
            r01[60] = "0"
            # ODSTRÁNENÉ: r01[62], r01[66], r01[67], r01[68] - spôsobovali chybu OSS!
            r01[69] = "0.0000"
            r01[70] = "0.0000"
            
            lines.append(self._build_row(r01))

            # --- POLOŽKY DOKLADU (R02 - presne 28 stĺpcov) ---
            def build_r02(md_s, md_a, dal_s, dal_a, suma, text, typ_dph, kv_dph="X"):
                r02 = [""] * 37  # OPRAVA: R02 pre T00 má presne 37 stĺpcov
                r02[0] = "R02"
                r02[1] = "0"
                r02[2] = md_s
                r02[3] = md_a
                r02[4] = dal_s
                r02[5] = dal_a
                r02[6] = self._format_decimal(suma)
                r02[7] = self._format_decimal(suma)
                r02[8] = self._sanitize_string(text, 60)
                r02[9] = typ_dph
                r02[11] = "X"
                r02[12] = "(Nedefinované)"
                r02[13] = "X"
                r02[14] = "(Nedefinované)"
                r02[15] = "X"
                r02[16] = "(Nedefinované)"
                r02[17] = "X"
                r02[18] = "(Nedefinované)"
                r02[25] = kv_dph      # OPRAVA: Oddiel KV DPH je na indexe 25
                r02[29] = "0.0000"    # OPRAVA: Množstvo KV DPH je na indexe 29
                r02[30] = "0"         # OPRAVA: ID strediska je na indexe 30
                return self._build_row(r02)

            # 1. Riadok: Záväzok voči dodávateľovi (DAL)
            ucet_zavazky_syn, ucet_zavazky_ana = self._split_account(self.nastavenia.ucet_zavazky, fallback="321000") if self.nastavenia else ("321", "000")
            lines.append(build_r02("", "", ucet_zavazky_syn, ucet_zavazky_ana, suma_spolu, r01[6], "S"))

            # 2. Riadok: Náklad / Základ dane (MD)
            # 🏗️ ARCHITECT FIX: Kaskáda predkontácie (Položka -> Faktúra -> Dodávateľ -> Globálne)
            predkontacia_nakup = getattr(self.nastavenia, 'pohoda_predkontacia_prijata_fa', '501000')
            if getattr(faktura, 'erp_predkontacia', None):
                predkontacia_nakup = faktura.erp_predkontacia
            elif faktura.dodavatel and getattr(faktura.dodavatel, 'erp_predkontacia', None):
                predkontacia_nakup = faktura.dodavatel.erp_predkontacia

            nazov_polozky = text_dokladu
            if polozky_log is not None and hasattr(polozky_log, 'exists') and polozky_log.exists():
                prva_pol = polozky_log.first()
                nazov_polozky = getattr(prva_pol, 'navrhovany_nazov', None) or getattr(prva_pol, 'najdeny_kod_faktura', None) or text_dokladu
                if getattr(prva_pol, 'erp_predkontacia', None):
                    predkontacia_nakup = prva_pol.erp_predkontacia

            ucet_naklad_syn, ucet_naklad_ana = self._split_account(predkontacia_nakup, fallback="501000")
            
            # 🛡️ ISAS ARCHITECT: Bezpečné formátovanie sadzieb bez orezania desatinných miest (ochrana pred stratou dát)
            dph_zakl_str = f"{self.zakladna_dph:g}"
            dph_zniz_str = f"{self.znizena_dph:g}"
            kod_zakladna_velke = f"{dph_zakl_str}A"
            kod_znizena_velke = f"{dph_zniz_str}B"
            kod_zakladna_male = f"{dph_zakl_str}a"
            kod_znizena_male = f"{dph_zniz_str}b"

            # 🏗️ ARCHITECT FIX: Správne zaúčtovanie samozdanenia (Prenos B.1 a EÚ) vs bežný nákup B.2 vs Mimo DPH
            dph_kod_val = dph_zakl_str if sadzba_dph >= self.zakladna_dph else dph_zniz_str
            if kod_dph in ['PP', 'VAT_REVERSE']:
                typ_dph_zaklad = f"{dph_kod_val}P"
                kv_dph_zaklad = "B1"
            elif kod_dph == 'EU':
                typ_dph_zaklad = f"{dph_kod_val}E"
                kv_dph_zaklad = "X"
            elif kod_dph == 'PZ':
                typ_dph_zaklad = f"{dph_kod_val}S"
                kv_dph_zaklad = "X"
            elif dph == 0 or je_mimo_dph:
                typ_dph_zaklad = "V"
                kv_dph_zaklad = "X"
            else:
                typ_dph_zaklad = kod_zakladna_velke if sadzba_dph >= self.zakladna_dph else kod_znizena_velke
                kv_dph_zaklad = "B2"
                
            lines.append(build_r02(ucet_naklad_syn, ucet_naklad_ana, "", "", zaklad, nazov_polozky, typ_dph_zaklad, kv_dph_zaklad))

            # 3. Riadok: DPH (MD) - Iba ak je DPH > 0
            if dph > 0:
                fallback_dph = "343021" if self.krajina_iso == 'CZ' else "343023"
                ucet_dph_syn, ucet_dph_ana = self._split_account(self.nastavenia.ucet_dph_20, fallback=fallback_dph) if self.nastavenia else (fallback_dph[:3], fallback_dph[3:])
                typ_dph_dph = kod_zakladna_male if sadzba_dph >= self.zakladna_dph else kod_znizena_male
                lines.append(build_r02(ucet_dph_syn, ucet_dph_ana, "", "", dph, "DPH", typ_dph_dph, "B2"))

        return "\r\n".join(lines).encode('windows-1250', errors='replace')

    # =========================================================================
    # 5. TANKOVANIA -> T00 (Evidencia účtovných dokladov)
    # =========================================================================
    def vygeneruj_tankovania_xml(self, queryset_tankovani):
        lines = []
        lines.append(self._build_row(["R00", "T00"]))

        for tankovanie in queryset_tankovani:
            if tankovanie.sposob_zauctovania_erp == 'FAKTURA':
                continue

            is_pokladna = tankovanie.sposob_zauctovania_erp == 'FIREMNY_UCET' and tankovanie.sposob_platby == 'HOTOVOST'

            # --- HLAVIČKA DOKLADU (R01 - presne 71 stĺpcov podľa KROS špecifikácie) ---
            r01 = [""] * 71
            r01[0] = "R01"
            r01[1] = "160" if is_pokladna else "180"
            r01[2] = "P1" if is_pokladna else "IDX"
            r01[3] = "V" if is_pokladna else "IDx"
            
            evidencne_cislo = tankovanie.cislo_dokladu if tankovanie.cislo_dokladu else f"PHM-{tankovanie.id}"
            r01[4] = self._sanitize_string(evidencne_cislo, 20)
            r01[5] = self._sanitize_string(evidencne_cislo, 20)
            
            ic_dph = ""
            stat_partnera = self.krajina_iso
            if tankovanie.sposob_zauctovania_erp == 'SUKROMNE_PENIAZE':
                vodic = tankovanie.vodič or tankovanie.osoba
                if vodic:
                    p_data = self._get_partner_data(vodic)
                    r01[6] = p_data['nazov']
                    r01[7] = p_data['ico']
                    r01[8] = p_data['ic_dph']
                    ic_dph = p_data['ic_dph']
                    stat_partnera = p_data['stat']
            elif tankovanie.cerpacia_stanica:
                p_data = self._get_partner_data(tankovanie.cerpacia_stanica)
                r01[6] = p_data['nazov']
                r01[7] = p_data['ico']
                r01[8] = p_data['ic_dph']
                ic_dph = p_data['ic_dph']
                stat_partnera = p_data['stat']

            datum = self._format_date(tankovanie.datum)
            r01[9] = datum
            r01[11] = datum
            r01[12] = datum
            r01[13] = datum
            
            predvolena_mena = getattr(self.identita, 'predvolena_mena', None) or get_firemna_mena()
            r01[14] = self._sanitize_string(getattr(tankovanie, 'mena', predvolena_mena) or predvolena_mena, 5)
            r01[15] = "1"
            r01[16] = "1"
            r01[17] = "0"
            
            r01[18] = self._format_decimal(tankovanie.cena_celkom)
            r01[19] = self._format_decimal(tankovanie.cena_celkom)
            
            sadzba_dph_tank = Decimal(str(tankovanie.sadzba_dph)) if getattr(tankovanie, 'sadzba_dph', None) is not None else self.zakladna_dph
            r01[20] = f"{self.znizena_dph:g}"
            r01[21] = f"{self.zakladna_dph:g}"
            
            kod_dph = getattr(tankovanie, 'kod_clenenia_dph', 'PD')
            ma_narok_na_odpocet = (kod_dph in ['PD', 'VAT_IN', 'PDPH'] and sadzba_dph_tank > 0)

            r01[22] = "0.00"
            r01[23] = "0.00"
            r01[24] = "0.00"
            r01[25] = "0.00"
            r01[26] = "0.00"
            r01[27] = "0.00"
            r01[28] = "0.00"
            r01[29] = "Administrátor"

            if ma_narok_na_odpocet:
                if sadzba_dph_tank >= self.zakladna_dph:
                    r01[22] = self._format_decimal(tankovanie.cena_bez_dph)
                    r01[26] = self._format_decimal(tankovanie.suma_dph)
                else:
                    r01[23] = self._format_decimal(tankovanie.cena_bez_dph)
                    r01[27] = self._format_decimal(tankovanie.suma_dph)
            else:
                r01[25] = self._format_decimal(tankovanie.cena_celkom)
                
            r01[34] = "-" if is_pokladna else ""
            
            if isinstance(tankovanie.datum, datetime):
                ts = timezone.localtime(tankovanie.datum) if timezone.is_aware(tankovanie.datum) else tankovanie.datum
                r01[38] = ts.strftime('%H:%M:%S')
            else:
                r01[38] = "12:00:00"
                
            kod_paliva = tankovanie.vozidlo.erp_kod_paliva if tankovanie.vozidlo else ""
            text_dokladu = self._sanitize_string(f"PHM {kod_paliva} {tankovanie.vozidlo.spz} - {tankovanie.litre}L", 60)
            r01[40] = text_dokladu
            r01[41] = "0" # Typ dokladu
            r01[42] = "" # OSS Štát
            r01[43] = stat_partnera if stat_partnera else self.krajina_iso # Štát DPH
            r01[44] = ic_dph # IČ DPH
            
            # Povinný koncový balast pre EUD
            r01[45] = "0.0000"
            r01[46] = "0"
            r01[47] = "X"
            r01[48] = "(Nedefinované)"
            r01[49] = "HS"
            r01[50] = "0"
            r01[51] = "X"
            r01[52] = "(Nedefinované)"
            r01[53] = self._sanitize_string(evidencne_cislo, 20)
            r01[56] = "/"
            r01[57] = "0"
            r01[60] = "0"
            # ODSTRÁNENÉ: r01[62], r01[66], r01[67], r01[68] - spôsobovali chybu OSS!
            r01[69] = "0.0000"
            r01[70] = "0.0000"
            
            lines.append(self._build_row(r01))

            # ARCHITECT FIX: Bezpečné mapovanie účtov s fallbackom pre prípad, že používateľ nechal textové skratky z Pohody
            ucet_md_syn, ucet_md_ana = self._split_account(self.nastavenia.ucet_naklady_palivo, fallback="501000") if self.nastavenia else ("501", "000")
            
            ucet_dal_full = self.nastavenia.ucet_pokladna if is_pokladna else self.nastavenia.ucet_zavazky_zamestnanci
            fallback_dal = "211000" if is_pokladna else "333000"
            ucet_dal_syn, ucet_dal_ana = self._split_account(ucet_dal_full, fallback=fallback_dal) if self.nastavenia else (fallback_dal[:3], fallback_dal[3:])
            
            stredisko = self._sanitize_string(tankovanie.vozidlo.erp_stredisko, 5) if tankovanie.vozidlo.erp_stredisko else "(Nedefinované)"
            
            def build_r02(md_s, md_a, dal_s, dal_a, suma, text, typ_dph, kv_dph="X"):
                r02 = [""] * 37  # OPRAVA: R02 pre T00 má presne 37 stĺpcov
                r02[0] = "R02"
                r02[1] = "0"
                r02[2] = md_s
                r02[3] = md_a
                r02[4] = dal_s
                r02[5] = dal_a
                r02[6] = self._format_decimal(suma)
                r02[7] = self._format_decimal(suma)
                r02[8] = text
                r02[9] = typ_dph
                r02[11] = "X"
                r02[12] = stredisko
                r02[13] = "X"
                r02[14] = "(Nedefinované)"
                r02[15] = "X"
                r02[16] = "(Nedefinované)"
                r02[17] = "X"
                r02[18] = "(Nedefinované)"
                r02[25] = kv_dph      # OPRAVA: Oddiel KV DPH je na indexe 25
                r02[29] = "0.0000"    # OPRAVA: Množstvo KV DPH je na indexe 29
                r02[30] = "0"         # OPRAVA: ID strediska je na indexe 30
                return self._build_row(r02)

            # ARCHITECT FIX: Ak je DPH a máme nárok na odpočet (PD), Omega vyžaduje rozpad na 3 riadky (Spolu, Základ, DPH)
            if ma_narok_na_odpocet:
                lines.append(build_r02("", "", ucet_dal_syn, ucet_dal_ana, tankovanie.cena_celkom, text_dokladu, "S"))
                
                dph_zakl_tank_str = f"{self.zakladna_dph:g}"
                dph_zniz_tank_str = f"{self.znizena_dph:g}"
                kod_zakladna_velke = f"{dph_zakl_tank_str}A"
                kod_znizena_velke = f"{dph_zniz_tank_str}B"
                kod_zakladna_male = f"{dph_zakl_tank_str}a"
                kod_znizena_male = f"{dph_zniz_tank_str}b"

                # 🏗️ ARCHITECT FIX: Dynamické priradenie kódu DPH pre základ
                typ_dph_zaklad = kod_zakladna_velke if sadzba_dph_tank >= self.zakladna_dph else kod_znizena_velke
                lines.append(build_r02(ucet_md_syn, ucet_md_ana, "", "", tankovanie.cena_bez_dph, text_dokladu, typ_dph_zaklad, "B3"))
                
                fallback_dph = "343021" if self.krajina_iso == 'CZ' else "343023"
                dph_syn, dph_ana = self._split_account(self.nastavenia.ucet_dph_20, fallback=fallback_dph) if self.nastavenia else (fallback_dph[:3], fallback_dph[3:])
                # 🏗️ ARCHITECT FIX: Dynamické priradenie kódu pre DPH
                typ_dph_dph = kod_zakladna_male if sadzba_dph_tank >= self.zakladna_dph else kod_znizena_male
                lines.append(build_r02(dph_syn, dph_ana, "", "", tankovanie.suma_dph, "DPH", typ_dph_dph, "B3"))
            else:
                # Ak je to EU nákup alebo Neplatca (PN), celá suma ide do nákladov (Typ DPH "V" = Voľný základ / Mimo DPH)
                lines.append(build_r02(ucet_md_syn, ucet_md_ana, ucet_dal_syn, ucet_dal_ana, tankovanie.cena_celkom, text_dokladu, "V"))

        return "\r\n".join(lines).encode('windows-1250', errors='replace')

    # =========================================================================
    # 6. EVIDENCIA ÚHRAD -> T00 (Evidencia účtovných dokladov - Banka/Pokladňa/Zápočet)
    # =========================================================================
    def vygeneruj_uhrady_xml(self, queryset_uhrad):
        lines = []
        lines.append(self._build_row(["R00", "T00"]))

        for uhrada in queryset_uhrad:
            faktura = getattr(uhrada, 'vystavena_faktura', None) or getattr(uhrada, 'prijata_faktura', None) or getattr(uhrada, 'prichodzia_faktura', None)
            if not faktura:
                continue

            # 1. MAPOVANIE TYPU ÚHRADY Z DATABÁZY NA OMEGA OKRUHY A ČÍSELNÉ RADY
            typ = uhrada.typ_uhrady
            if typ == 'BV':
                okruh = "170"
                evidencia = getattr(self.nastavenia, 'omega_evidencia_banka', 'BV')
                ciselny_rad = getattr(self.nastavenia, 'omega_ciselny_rad_banka', 'B')
                ucet_fin_full = getattr(self.nastavenia, 'ucet_banka', '221000')
            elif typ == 'PPD':
                okruh = "160"
                evidencia = getattr(self.nastavenia, 'omega_evidencia_pokladna', 'PD')
                ciselny_rad = getattr(self.nastavenia, 'omega_ciselny_rad_pokladna_prijem', 'P')
                ucet_fin_full = getattr(self.nastavenia, 'ucet_pokladna', '211000')
            elif typ == 'VPD':
                okruh = "160"
                evidencia = getattr(self.nastavenia, 'omega_evidencia_pokladna', 'PD')
                ciselny_rad = getattr(self.nastavenia, 'omega_ciselny_rad_pokladna_vydaj', 'V')
                ucet_fin_full = getattr(self.nastavenia, 'ucet_pokladna', '211000')
            elif typ == 'PK':
                okruh = "180"
                evidencia = getattr(self.nastavenia, 'omega_evidencia_id', 'ID')
                ciselny_rad = getattr(self.nastavenia, 'omega_ciselny_rad_id', 'ID')
                ucet_fin_full = "261000"  # Peniaze na ceste
            elif typ == 'VZ':
                okruh = "180"
                evidencia = getattr(self.nastavenia, 'omega_evidencia_id', 'ID')
                ciselny_rad = getattr(self.nastavenia, 'omega_ciselny_rad_id', 'ID')
                ucet_fin_full = None      # Zápočet nepoužíva finančný účet
            else:
                okruh = "170"
                evidencia = getattr(self.nastavenia, 'omega_evidencia_banka', 'BV')
                ciselny_rad = getattr(self.nastavenia, 'omega_ciselny_rad_banka', 'B')
                ucet_fin_full = getattr(self.nastavenia, 'ucet_banka', '221000')

            # 🏗️ ARCHITECT FIX: Bezpečné volanie _split_account. 
            # Odstránený inline if/else, ktorý pri prázdnom stringu obchádzal fallback!
            ucet_fin_s, ucet_fin_a = self._split_account(ucet_fin_full, "221000")
            ucet_poh_s, ucet_poh_a = self._split_account(getattr(self.nastavenia, 'ucet_pohladavky', '311000'), "311000")
            ucet_zav_s, ucet_zav_a = self._split_account(getattr(self.nastavenia, 'ucet_zavazky', '321000'), "321000")

            je_vystavena = getattr(uhrada, 'vystavena_faktura', None) is not None
            je_dobropis = getattr(faktura, 'typ_dokladu', '') == 'DOBROPIS'

            # 2. LOGIKA ÚČTOVANIA (MD/DAL) A ZNAMIENOK
            znamienko = ""
            if typ == 'VZ':
                # Vzájomný zápočet (Interný doklad)
                md_s, md_a = ucet_zav_s, ucet_zav_a
                dal_s, dal_a = ucet_poh_s, ucet_poh_a
                # Znamienko pre IDx musí byť podľa schémy prázdne!
            elif je_vystavena:
                # Úhrada odberateľskej faktúry
                if je_dobropis or typ == 'VPD': 
                    znamienko = "-" # Vraciame peniaze
                    md_s, md_a = ucet_poh_s, ucet_poh_a
                    dal_s, dal_a = ucet_fin_s, ucet_fin_a
                else: 
                    znamienko = "+" # Prijímame peniaze
                    md_s, md_a = ucet_fin_s, ucet_fin_a
                    dal_s, dal_a = ucet_poh_s, ucet_poh_a
            else:
                # Úhrada dodávateľskej faktúry
                if je_dobropis or typ == 'PPD': 
                    znamienko = "+" # Dodávateľ vracia peniaze nám
                    md_s, md_a = ucet_fin_s, ucet_fin_a
                    dal_s, dal_a = ucet_zav_s, ucet_zav_a
                else: 
                    znamienko = "-" # Platíme dodávateľovi
                    md_s, md_a = ucet_zav_s, ucet_zav_a
                    dal_s, dal_a = ucet_fin_s, ucet_fin_a

            # Ak je to interný doklad (PK, VZ), Omega zakazuje znamienko
            if okruh == "180":
                znamienko = ""

            # 3. PRÍPRAVA TEXTOV A DÁT
            # 🏗️ ARCHITECT FIX: Číslo dokladu nechávame prázdne. Ak by sme poslali interné ID (napr. BV-4), 
            # Omega by ho odmietla, pretože sa nezhoduje s jej maskou číselného radu (B1-002-002).
            # Prázdna hodnota zabezpečí, že Omega si automaticky vygeneruje správne nasledujúce číslo.
            cislo_dokladu = ""
            
            if je_vystavena:
                cislo_fa = getattr(faktura, 'cislo_faktury', f"FA-{faktura.id}")
                vs = getattr(faktura, 'variabilny_symbol', cislo_fa)
                partner = faktura.zakaznik
            else:
                cislo_fa = getattr(faktura, 'variabilny_symbol', f"DF-{faktura.id}")
                vs = cislo_fa
                partner = faktura.dodavatel

            partner_nazov = self._get_partner_data(partner)['nazov'] if partner else ""
            suma = self._format_decimal(uhrada.suma_uhrady)
            datum = self._format_date(uhrada.datum_uhrady)
            
            if uhrada.poznamka:
                text_uhrady = self._sanitize_string(uhrada.poznamka, 60)
            else:
                text_uhrady = self._sanitize_string(f"Úhrada faktúry {cislo_fa}", 60)

            # --- HLAVIČKA DOKLADU (R01 - presne 71 stĺpcov) ---
            r01 = [""] * 71
            r01[0] = "R01"
            r01[1] = okruh
            r01[2] = evidencia
            r01[3] = ciselny_rad
            r01[4] = cislo_dokladu
            r01[5] = cislo_dokladu
            r01[6] = partner_nazov
            r01[9] = datum
            r01[11] = datum
            r01[12] = datum
            r01[13] = datum
            domaca_mena = getattr(self.identita, 'predvolena_mena', None) or get_firemna_mena()
            r01[14] = self._sanitize_string(getattr(uhrada, 'mena', domaca_mena) or domaca_mena, 5)
            r01[15] = "1"
            r01[16] = "1"
            r01[17] = "0"
            r01[18] = suma
            r01[19] = suma
            r01[20] = f"{self.znizena_dph:g}"
            r01[21] = f"{self.zakladna_dph:g}"
            
            # Úhrada je vždy mimo DPH (Voľný základ)
            r01[22] = "0.00"
            r01[23] = "0.00"
            r01[24] = "0.00"
            r01[25] = suma
            r01[26] = "0.00"
            r01[27] = "0.00"
            r01[28] = "0.00"
            r01[29] = "Administrátor"
            
            # Kľúčové polia pre spárovanie úhrady s faktúrou v Omege
            r01[32] = self._sanitize_string(cislo_fa, 20) # Interné číslo uhrádzaného dokladu
            r01[33] = self._sanitize_string(vs, 20)       # Externé číslo uhrádzaného dokladu
            r01[34] = znamienko                           # Znamienko (+ alebo -)
            
            if uhrada.protiucet_iban:
                iban_clean = self._sanitize_string(uhrada.protiucet_iban, 50)
                r01[35] = iban_clean
                r01[61] = iban_clean
            
            r01[38] = "12:00:00"
            r01[40] = text_uhrady
            r01[41] = "0"
            
            # Koncový balast pre EUD
            r01[45] = "0.0000"
            r01[46] = "0"
            r01[47] = ""
            r01[48] = ""
            r01[49] = "HS"
            r01[50] = "0"
            r01[51] = ""
            r01[52] = ""
            r01[53] = self._sanitize_string(vs, 20)
            r01[56] = "/"
            r01[57] = "0"
            r01[60] = "0"
            r01[69] = "0.0000"
            r01[70] = "0.0000"
            
            lines.append(self._build_row(r01))

            # --- POLOŽKA DOKLADU (R02 - presne 37 stĺpcov) ---
            r02 = [""] * 37
            r02[0] = "R02"
            r02[1] = "0"
            r02[2] = md_s
            r02[3] = md_a
            r02[4] = dal_s
            r02[5] = dal_a
            r02[6] = suma
            r02[7] = suma
            r02[8] = text_uhrady
            r02[9] = "V" # Typ DPH: Voľný základ (mimo DPH)
            
            # Kľúčové pole pre spárovanie v položke
            r02[10] = self._sanitize_string(cislo_fa, 20) # Číslo uhrádzaného dokladu
            
            r02[11] = ""
            r02[12] = ""
            r02[13] = ""
            r02[14] = ""
            r02[15] = ""
            r02[16] = ""
            r02[17] = ""
            r02[18] = ""
            r02[25] = "" # KV DPH
            r02[29] = "0.0000"
            r02[30] = "0"
            
            lines.append(self._build_row(r02))

        return "\r\n".join(lines).encode('windows-1250', errors='replace')