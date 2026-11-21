import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from django.utils import timezone
from django.apps import apps
import uuid

from Evidencia.utils_helper import get_firemna_mena

# =========================================================================
# ⚙️ ISAS XML ENGINE: STORMWARE POHODA 🛡️
# =========================================================================

class PohodaXMLGenerator:
    """
    Ultimátny XML Generátor pre export Vystavených FA, Prijatých FA, Zákazníkov, 
    Skladových Pohybov, Tankovaní a Knihy Jázd. 🚀 Zvláda párovanie IBANov, 
    súkromných úhrad a komplexnú analytiku pre KV DPH.
    """
    
    def get_file_prefix(self):
        """Produkčná identifikácia výstupného formátu pre ZIP archív."""
        return "pohoda"
    
    NAMESPACES = {
        'dat': 'http://www.stormware.cz/schema/version_2/data.xsd',
        'inv': 'http://www.stormware.cz/schema/version_2/invoice.xsd',
        'typ': 'http://www.stormware.cz/schema/version_2/type.xsd',
        'adb': 'http://www.stormware.cz/schema/version_2/addressbook.xsd',
        'int': 'http://www.stormware.cz/schema/version_2/intDoc.xsd',
        'vch': 'http://www.stormware.cz/schema/version_2/voucher.xsd',
        'bnk': 'http://www.stormware.cz/schema/version_2/bank.xsd',
    }

    def __init__(self):
        for prefix, uri in self.NAMESPACES.items():
            ET.register_namespace(prefix, uri)
            
        ERPExportNastavenia = apps.get_model('Evidencia', 'ERPExportNastavenia')
        self.nastavenia = ERPExportNastavenia.get_settings()
        if not self.nastavenia:
            raise ValueError("🚨 Kritická chyba: ERPExportNastavenia nie sú v databáze vytvorené.")

        # --- DEFENZÍVNA KONTROLA MANDATÓRNYCH POLÍ PRE POHODU ---
        if not getattr(self.nastavenia, 'ucet_banka', None):
            raise ValueError("🚨 Nastavenie exportu zlyhalo: Nemáte vyplnenú 'Skratku banky' v nastaveniach ERP exportu.")
        if not getattr(self.nastavenia, 'ucet_pokladna', None):
            raise ValueError("🚨 Nastavenie exportu zlyhalo: Nemáte vyplnenú 'Skratku pokladne' v nastaveniach ERP exportu.")

        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        self.identita = FiremnaIdentita.objects.first()

        # 🛡️ ISAS ARCHITECT (Defense-in-Depth): Overenie sídla pred inicializáciou XML enginu
        krajina_iso = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else 'NEZADANE'
        if krajina_iso == 'NEZADANE':
            raise ValueError("🚨 Kritická chyba: V systéme chýba Profil spoločnosti (Firemná identita). Pred exportom do Pohody vyplňte IČO a sídlo.")
        if krajina_iso not in ['SK', 'CZ']:
            raise ValueError(f"🚨 Neplatný export: Stormware Pohoda XML je legislatívne určená výhradne pre SK a CZ trh. Sídlo firmy je v '{krajina_iso}'.")
        self.krajina_iso = krajina_iso

        self.moje_ico = getattr(self.identita, 'ico', None) or 'NEZADANE'
        if self.moje_ico == 'NEZADANE':
            raise ValueError("🚨 Kritická chyba: Nemáte vyplnené IČO vo Firemnej Identite.")

        # 🛡️ ISAS ARCHITECT: Dynamické DPH sadzby z konfigurácie s fail-safe fallbackom z _meta
        GlobalneNastavenia = apps.get_model('ISASaplikacia', 'GlobalneNastavenia')
        self.gn = GlobalneNastavenia.objects.first()
        self.zakladna_dph = Decimal(str(self.gn.zakladna_sadzba_dph if (self.gn and self.gn.zakladna_sadzba_dph is not None) else GlobalneNastavenia.get_zakladna_dph()))
        self.znizena_dph = Decimal(str(self.gn.znizena_sadzba_dph if (self.gn and self.gn.znizena_sadzba_dph is not None) else GlobalneNastavenia.get_znizena_dph()))

    def _vytvor_koren_dokumentu(self, id_exportu):
        now = timezone.now()
        cas_exportu = timezone.localtime(now).strftime('%d.%m.%Y %H:%M:%S') if timezone.is_aware(now) else now.strftime('%d.%m.%Y %H:%M:%S')
        root = ET.Element(f"{{{self.NAMESPACES['dat']}}}dataPack", {
            'id': f"ISAS_{id_exportu}",
            'ico': str(self.moje_ico).replace(" ", ""),
            'application': "ISAS CORE SaaS 🤖",
            'version': "2.0",
            'note': f"Hromadný export dát z ISAS ({cas_exportu})"
        })
        return root

    def formataj_datum(self, datum):
        if not datum: return None  # Vrátime None, aby sme tag vôbec nevytvorili
        if hasattr(datum, 'strftime'): return datum.strftime('%Y-%m-%d') # Duck-typing je v Djangu bezpečnejší
        return str(datum)

    def formatuj_sumu(self, suma):
        if not suma: return "0.00"
        try:
            return str(Decimal(str(suma).replace(' ', '').replace(',', '.')).quantize(Decimal('0.01')))
        except:
            return "0.00"

    def formatuj_jednotkovu_cenu(self, suma):
        """Špeciálny formátovač pre unitPrice na 4 desatinné miesta (zabraňuje centovým rozdielom v Pohode)"""
        if not suma: return "0.0000"
        try:
            return str(Decimal(str(suma).replace(' ', '').replace(',', '.')).quantize(Decimal('0.0001')))
        except:
            return "0.0000"

    def ziskaj_xml_string(self, root_element):
        return ET.tostring(root_element, encoding='UTF-8', xml_declaration=True)

    def _get_iso_stat(self, stat_raw=None):
        """🛡️ ISAS ARCHITECT: Čisté dynamické načítanie ISO kódu štátu cez DB model StatLokalizacia bez hardkódu."""
        fallback_iso = getattr(self, 'krajina_iso', '')
        if not stat_raw:
            return fallback_iso

        raw_stat = str(stat_raw).strip()
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

    # =========================================================================
    # 1. GENERÁTOR VYSTAVENÝCH FAKTÚR (PREDAJ) 📈
    # =========================================================================
    def _vytvor_adresu_odberatela(self, faktura, parent_element):
        partner_id = ET.SubElement(parent_element, f"{{{self.NAMESPACES['inv']}}}partnerIdentity")
        address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
        
        ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = str(faktura.odberatel_nazov or 'Neznámy odberateľ')[:255]
        
        if getattr(faktura, 'odberatel_mesto', None):
            ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}city").text = str(faktura.odberatel_mesto)[:45]
        if getattr(faktura, 'odberatel_ulica', None):
            ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}street").text = str(faktura.odberatel_ulica)[:64]
        if getattr(faktura, 'odberatel_psc', None):
            ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}zip").text = str(faktura.odberatel_psc).replace(" ", "")[:15]
        if getattr(faktura, 'odberatel_stat', None):
            country = ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}country")
            ET.SubElement(country, f"{{{self.NAMESPACES['typ']}}}ids").text = self._get_iso_stat(faktura.odberatel_stat)[:19]

        if faktura.odberatel_ico and faktura.odberatel_ico != '-': ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}ico").text = str(faktura.odberatel_ico).replace(" ", "")[:15]
        if faktura.odberatel_dic and faktura.odberatel_dic != '-': ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}dic").text = str(faktura.odberatel_dic).replace(" ", "")[:18]
        if faktura.odberatel_ic_dph and faktura.odberatel_ic_dph != '-': ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}icDph").text = str(faktura.odberatel_ic_dph).replace(" ", "")[:18]

    def vygeneruj_faktury_xml(self, queryset_faktur):
        batch_id = str(uuid.uuid4())[:8].upper()
        root = self._vytvor_koren_dokumentu(batch_id)

        defaults = self.identita.get_country_defaults() if (self.identita and hasattr(self.identita, 'get_country_defaults')) else {}
        domaca_mena = getattr(self.identita, 'predvolena_mena', None) or defaults.get('mena') or get_firemna_mena()
        # Poistka: Ak má česká firma v databáze omylom EUR, vnútime menu podľa krajiny sídla
        if self.krajina_iso == 'CZ' and domaca_mena == 'EUR':
            domaca_mena = 'CZK'
        for faktura in queryset_faktur:
            # Dynamická detekcia meny faktúry voči domácej mene účtovnej jednotky
            faktura_mena = getattr(faktura, 'mena', domaca_mena) or domaca_mena
            je_cudzia_mena = (faktura_mena != domaca_mena)

            data_pack_item = ET.SubElement(root, f"{{{self.NAMESPACES['dat']}}}dataPackItem", {'id': str(faktura.cislo_faktury or f"FA_{faktura.id}"), 'version': "2.0"})
            invoice = ET.SubElement(data_pack_item, f"{{{self.NAMESPACES['inv']}}}invoice", {'version': "2.0"})

            header = ET.SubElement(invoice, f"{{{self.NAMESPACES['inv']}}}invoiceHeader")
            invoice_type = "issuedInvoice"
            if getattr(faktura, 'typ_dokladu', 'FAKTURA') == 'ZALOHA': invoice_type = "issuedAdvanceInvoice"
            elif getattr(faktura, 'typ_dokladu', 'FAKTURA') == 'DOBROPIS': invoice_type = "issuedCorrectiveTax"
            
            ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}invoiceType").text = invoice_type
            
            # OPRAVA: Tag 'number' je komplexný typ, nesmie obsahovať priamo text.
            # Musí obsahovať vnorený tag 'typ:numberRequested'
            number_el = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}number")
            ET.SubElement(number_el, f"{{{self.NAMESPACES['typ']}}}numberRequested").text = str(faktura.cislo_faktury)
            
            if faktura.variabilny_symbol:
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}symVar").text = str(faktura.variabilny_symbol)[:20]
                
            # 🏗️ ISAS ARCHITECT FIX: Prepojenie Dobropisu na pôvodnú faktúru v Pohode (Nutné pre KV DPH)
            povodna_fa = getattr(faktura, 'povodna_faktura', None)
            if getattr(faktura, 'typ_dokladu', 'FAKTURA') == 'DOBROPIS' and povodna_fa:
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}originalDocument").text = str(getattr(povodna_fa, 'cislo_faktury', ''))[:32]
            
            # symConst musí ísť podľa XSD až za dátumy a partnerIdentity
            if faktura.datum_vystavenia:
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}date").text = self.formataj_datum(faktura.datum_vystavenia)
            if faktura.datum_dodania:
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}dateTax").text = self.formataj_datum(faktura.datum_dodania)
            
            # Pridaný chýbajúci dátum účtovného prípadu (odstráni varovanie 603)
            if faktura.datum_vystavenia:
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}dateAccounting").text = self.formataj_datum(faktura.datum_vystavenia)
            
            if faktura.datum_splatnosti:
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}dateDue").text = self.formataj_datum(faktura.datum_splatnosti)
            
            # OPRAVA: Použitie nového poľa pohoda_predkontacia_fa z modelu ERPExportNastavenia
            predkontacia = getattr(self.nastavenia, 'pohoda_predkontacia_fa', None)
            if predkontacia:
                acc = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}accounting")
                ET.SubElement(acc, f"{{{self.NAMESPACES['typ']}}}ids").text = str(predkontacia).strip()[:19]
            
            vat_class = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}classificationVAT")

            # 🛡️ ISAS ERP GATEWAY: Inteligentný preklad neutrálnych SaaS kódov do schémy Pohody (SK vs CZ)
            kod_dph_raw = getattr(faktura, 'kod_clenenia_dph', None)
            je_prenos = getattr(faktura, 'prenos_danovej_povinnosti', False) or kod_dph_raw in ['VAT_REVERSE', 'UK', 'UDpdp']

            if self.krajina_iso == 'CZ':
                if je_prenos:
                    kod_dph_fa = 'UDpdp'
                elif not kod_dph_raw or kod_dph_raw in ['UD', 'VAT_OUT', 'UDPH']:
                    kod_dph_fa = 'UDPH'
                else:
                    kod_dph_fa = kod_dph_raw
            else:  # SK
                if je_prenos:
                    kod_dph_fa = 'UK'
                elif not kod_dph_raw or kod_dph_raw in ['UDPH', 'VAT_OUT', 'UD']:
                    kod_dph_fa = 'UD'
                else:
                    kod_dph_fa = kod_dph_raw

            ET.SubElement(vat_class, f"{{{self.NAMESPACES['typ']}}}ids").text = str(kod_dph_fa)[:19]

            # 2. Podľa XSD je element <inv:text> striktne vyžadovaný pri vytváraní nového dokladu
            ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}text").text = f"Faktúra {faktura.cislo_faktury}"[:240]

            # 3. Potom musí ísť adresa odberateľa (partnerIdentity)
            self._vytvor_adresu_odberatela(faktura, header)

            # 4. Až potom paymentType
            payment_type = "draft"
            if faktura.sposob_uhrady == 'HOTOVOST': payment_type = "cash"
            elif faktura.sposob_uhrady == 'KARTA': payment_type = "creditcard"
            elif faktura.sposob_uhrady == 'PREVOD': payment_type = "draft"
            
            payment_el = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}paymentType")
            ET.SubElement(payment_el, f"{{{self.NAMESPACES['typ']}}}paymentType").text = payment_type

            # OPRAVA: Použitie nového poľa pohoda_skratka_banky z modelu ERPExportNastavenia
            ucet_skratka = getattr(self.nastavenia, 'pohoda_skratka_banky', None)
            if ucet_skratka:
                account_el = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}account")
                ET.SubElement(account_el, f"{{{self.NAMESPACES['typ']}}}ids").text = str(ucet_skratka).strip()[:19]

            # 5. Konštantný symbol musí ísť podľa XSD až za paymentType / account
            if faktura.konstantny_symbol:
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}symConst").text = str(faktura.konstantny_symbol)[:4]

            # 6. Poznámka (note) ide podľa XSD takmer na koniec hlavičky
            text_poznamky = f"{faktura.poznamka_pre_klienta or ''}\n{faktura.ai_poznamka or ''}".strip()
            if text_poznamky: ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}note").text = text_poznamky

            invoice_detail = ET.SubElement(invoice, f"{{{self.NAMESPACES['inv']}}}invoiceDetail")
            for polozka in faktura.polozky.all():
                item = ET.SubElement(invoice_detail, f"{{{self.NAMESPACES['inv']}}}invoiceItem")
                
                # 🧠 ISAS SMART FALLBACK: Do účtovníctva musí ísť presne ten istý názov ako na PDF
                zobrazeny_nazov = polozka.obchodny_nazov if getattr(polozka, 'obchodny_nazov', None) else polozka.nazov
                ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}text").text = str(zobrazeny_nazov)[:90]
                
                # Doplnené chýbajúce množstvo (quantity) pred mernú jednotku podľa XSD
                mnozstvo_fa_safe = getattr(polozka, 'mnozstvo', None) if getattr(polozka, 'mnozstvo', None) is not None else Decimal('1.00')
                ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}quantity").text = self.formatuj_sumu(mnozstvo_fa_safe)
                ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}unit").text = str(polozka.merna_jednotka)[:10]
                
                # Ceny posielame bez DPH (unitPrice = bez DPH), preto payVAT musí byť striktne 'false'
                ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}payVAT").text = "false"
                
                # Extrémne defenzívne načítanie sadzby DPH (zvládne 23, 20, 19, 5, 10 aj 0.23)
                try:
                    sadzba_dph_val = Decimal(str(polozka.sadzba_dph)) if polozka.sadzba_dph is not None else Decimal('0.00')
                    if Decimal('0.00') < sadzba_dph_val <= Decimal('1.00'):
                        sadzba_dph_val = (sadzba_dph_val * Decimal('100.00')).quantize(Decimal('0.01'))
                except (ValueError, TypeError):
                    sadzba_dph_val = Decimal('0.00')

                # Rozhodnutie o obalovom elemente meny pre položku podľa XSD schémy
                currency_tag = 'foreignCurrency' if je_cudzia_mena else 'homeCurrency'

                # 1. OSLOBODENÉ (0 % alebo prenos daňovej povinnosti)
                if faktura.prenos_danovej_povinnosti or sadzba_dph_val == Decimal('0.00'):
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}rateVAT").text = "none"
                # 2. DRUHÁ ZNÍŽENÁ SADZBA (napr. 5 %)
                elif Decimal('0.00') < sadzba_dph_val <= Decimal('10.00'):
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}rateVAT").text = "third"
                # 3. PRVÁ ZNÍŽENÁ SADZBA (dynamicky, napr. 19 % SK / 12 % CZ)
                elif Decimal('10.00') < sadzba_dph_val <= self.znizena_dph:
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}rateVAT").text = "low"
                # 4. ZÁKLADNÁ SADZBA (dynamicky, napr. 23 % SK / 21 % CZ)
                else:
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}rateVAT").text = "high"

                # ARCHITECT FIX: Prenos percentuálnej zľavy pre vizuál faktúry v Pohode
                if polozka.zlava_percento and polozka.zlava_percento > 0:
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}discountPercentage").text = self.formatuj_sumu(polozka.zlava_percento)

                # Zápis cien (DRY princíp - spoločné pre všetky sadzby)
                curr_el = ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}{currency_tag}")
                # ARCHITECT FIX: unitPrice musí ísť na 4 desatinné miesta pre presnosť pri násobení množstvom
                ET.SubElement(curr_el, f"{{{self.NAMESPACES['typ']}}}unitPrice").text = self.formatuj_jednotkovu_cenu(polozka.jednotkova_cena_bez_dph)
                ET.SubElement(curr_el, f"{{{self.NAMESPACES['typ']}}}price").text = self.formatuj_sumu(polozka.cena_po_zlave_bez_dph)

                # ARCHITECT FIX: Prenos analytického účtu výnosov (Predkontácia položky)
                if polozka.ucet_vynosov:
                    acc_item = ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}accounting")
                    ET.SubElement(acc_item, f"{{{self.NAMESPACES['typ']}}}ids").text = str(polozka.ucet_vynosov)[:19]

            summary = ET.SubElement(invoice, f"{{{self.NAMESPACES['inv']}}}invoiceSummary")
            # OPRAVA: roundingDocument je simpleType, nesmie mať vnorené tagy. Hodnota ide priamo do neho.
            ET.SubElement(summary, f"{{{self.NAMESPACES['inv']}}}roundingDocument").text = "none"

            if je_cudzia_mena:
                # Generovanie bloku pre cudziu menu (napr. CZK)
                sum_currency = ET.SubElement(summary, f"{{{self.NAMESPACES['inv']}}}foreignCurrency")
                
                currency_el = ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}currency")
                ET.SubElement(currency_el, f"{{{self.NAMESPACES['typ']}}}ids").text = str(faktura_mena)[:19]
                
                # Defenzívne načítanie kurzu z modelu s ochranou pred nulou a floatom
                try:
                    kurz_val = Decimal(str(getattr(faktura, 'kurz', None) or '1.00')).quantize(Decimal('0.01'))
                    if kurz_val <= Decimal('0.00'):
                        kurz_val = Decimal('1.00')
                except (ValueError, TypeError):
                    kurz_val = Decimal('1.00')
                ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}rate").text = str(kurz_val)
                
                # 🟢 OPRAVA: Správny tag podľa Pohoda type.xsd je 'amount', nie 'rateQuantity'
                ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}amount").text = "1"
                
                # Hodnoty v cudzej mene (Podľa type.xsd -> typeCurrencyForeign je povolený iba priceSum)
                ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceSum").text = self.formatuj_sumu(faktura.celkom_s_dph)
            else:
                # Štandardné generovanie pre domácu menu (EUR)
                sum_currency = ET.SubElement(summary, f"{{{self.NAMESPACES['inv']}}}homeCurrency")
                
                # 🏗️ ARCHITECT FIX: Skutočný rozpad DPH podľa položiek (plná podpora kombinovaných sadzieb)
                b_none = Decimal('0.00')
                b_third, v_third = Decimal('0.00'), Decimal('0.00')
                b_low, v_low = Decimal('0.00'), Decimal('0.00')
                b_high, v_high = Decimal('0.00'), Decimal('0.00')

                for pol in faktura.polozky.all():
                    cena_bez = Decimal(str(pol.cena_po_zlave_bez_dph or 0))
                    try:
                        s = Decimal(str(pol.sadzba_dph)) if pol.sadzba_dph is not None else Decimal('0.00')
                        if Decimal('0.00') < s <= Decimal('1.00'):
                            s = (s * Decimal('100.00')).quantize(Decimal('0.01'))
                    except (ValueError, TypeError):
                        s = Decimal('0.00')

                    s_dec = s / Decimal('100.00')
                    if faktura.prenos_danovej_povinnosti or s == Decimal('0.00'):
                        b_none += cena_bez
                    elif Decimal('0.00') < s <= Decimal('10.00'):
                        dph_pol = pol.hodnota_dph if (getattr(pol, 'hodnota_dph', None) is not None and not faktura.prenos_danovej_povinnosti) else (cena_bez * s_dec).quantize(Decimal('0.01'))
                        b_third += cena_bez
                        v_third += dph_pol
                    elif Decimal('10.00') < s <= self.znizena_dph:
                        dph_pol = pol.hodnota_dph if (getattr(pol, 'hodnota_dph', None) is not None and not faktura.prenos_danovej_povinnosti) else (cena_bez * s_dec).quantize(Decimal('0.01'))
                        b_low += cena_bez
                        v_low += dph_pol
                    else:
                        dph_pol = pol.hodnota_dph if (getattr(pol, 'hodnota_dph', None) is not None and not faktura.prenos_danovej_povinnosti) else (cena_bez * s_dec).quantize(Decimal('0.01'))
                        b_high += cena_bez
                        v_high += dph_pol

                if b_none > 0:
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceNone").text = self.formatuj_sumu(b_none)
                if b_low > 0:
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceLow").text = self.formatuj_sumu(b_low)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceLowVAT").text = self.formatuj_sumu(v_low)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceLowSum").text = self.formatuj_sumu(b_low + v_low)
                if b_high > 0 or (b_none == 0 and b_third == 0 and b_low == 0):
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceHigh").text = self.formatuj_sumu(b_high)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceHighVAT").text = self.formatuj_sumu(v_high)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceHighSum").text = self.formatuj_sumu(b_high + v_high)
                if b_third > 0:
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceThird").text = self.formatuj_sumu(b_third)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceThirdVAT").text = self.formatuj_sumu(v_third)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceThirdSum").text = self.formatuj_sumu(b_third + v_third)

        return self.ziskaj_xml_string(root)


    # =========================================================================
    # 2. GENERÁTOR PRIJATÝCH FAKTÚR (NÁKUPY) 🛒
    # =========================================================================
    def _vytvor_adresu_dodavatela(self, dodavatel, parent_element):
        partner_id = ET.SubElement(parent_element, f"{{{self.NAMESPACES['inv']}}}partnerIdentity")
        address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
        
        ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = str(dodavatel.Nazov_spolocnosti)[:255] if dodavatel else 'Neznámy dodávateľ'
        if dodavatel and dodavatel.mesto: ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}city").text = str(dodavatel.mesto)[:45]
        if dodavatel and dodavatel.ulica_a_cislo: ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}street").text = str(dodavatel.ulica_a_cislo)[:64]
        if dodavatel and dodavatel.psc: ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}zip").text = str(dodavatel.psc).replace(" ", "")[:15]
        if dodavatel and getattr(dodavatel, 'stat', None):
            country = ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}country")
            ET.SubElement(country, f"{{{self.NAMESPACES['typ']}}}ids").text = self._get_iso_stat(dodavatel.stat)[:19]
        if dodavatel and getattr(dodavatel, 'ico', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}ico").text = str(dodavatel.ico).replace(" ", "")[:15]
        if dodavatel and getattr(dodavatel, 'dic', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}dic").text = str(dodavatel.dic).replace(" ", "")[:18]
        if dodavatel and getattr(dodavatel, 'ic_dph', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}icDph").text = str(dodavatel.ic_dph).replace(" ", "")[:18]

    def vygeneruj_prijate_faktury_xml(self, queryset_faktur):
        batch_id = str(uuid.uuid4())[:8].upper()
        root = self._vytvor_koren_dokumentu(batch_id)

        for faktura in queryset_faktur:
            data_pack_item = ET.SubElement(root, f"{{{self.NAMESPACES['dat']}}}dataPackItem", {'id': f"NAKUP_{faktura.id}", 'version': "2.0"})
            invoice = ET.SubElement(data_pack_item, f"{{{self.NAMESPACES['inv']}}}invoice", {'version': "2.0"})

            header = ET.SubElement(invoice, f"{{{self.NAMESPACES['inv']}}}invoiceHeader")
            ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}invoiceType").text = "receivedInvoice"

            if faktura.variabilny_symbol:
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}symVar").text = str(faktura.variabilny_symbol)[:20]
                ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}originalDocument").text = str(faktura.variabilny_symbol)[:32]
            
            ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}date").text = self.formataj_datum(faktura.datum_prijatia)
            # ARCHITECT FIX: Model PrichodziaFaktura nemá datum_dodania, pre daňovú povinnosť a účtovníctvo používame datum_prijatia
            ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}dateTax").text = self.formataj_datum(faktura.datum_prijatia)
            ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}dateAccounting").text = self.formataj_datum(faktura.datum_prijatia)
            if faktura.datum_splatnosti: ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}dateDue").text = self.formataj_datum(faktura.datum_splatnosti)
            
            # Pridaná predkontácia (musí byť striktne pred classificationVAT)
            # 🏗️ ARCHITECT FIX: Hierarchia pre hlavičku: 
            # 1. Priamy prepis na faktúre -> 2. Predvolená na karte Dodávateľa -> 3. Globálny default
            predkontacia_nakup = (
                getattr(faktura, 'erp_predkontacia', None) or 
                (faktura.dodavatel and getattr(faktura.dodavatel, 'erp_predkontacia', None)) or 
                getattr(self.nastavenia, 'pohoda_predkontacia_prijata_fa', None)
            )
            if predkontacia_nakup:
                acc = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}accounting")
                ET.SubElement(acc, f"{{{self.NAMESPACES['typ']}}}ids").text = str(predkontacia_nakup).strip()[:19]
            
            # 🛡️ ISAS ARCHITECT: Rozlíšenie predvoleného členenia DPH podľa legislatívy krajiny (PD pre SK, PDPH pre CZ)
            kod_dph_raw = getattr(faktura, 'kod_clenenia_dph', None)
            if self.krajina_iso == 'CZ':
                if kod_dph_raw in ['VAT_IN', 'PD', 'PDPH']:
                    kod_dph = 'PDPH'
                elif kod_dph_raw in ['VAT_REVERSE', 'PP']:
                    kod_dph = 'PP'
                else:
                    kod_dph = kod_dph_raw or 'PDPH'
            else:  # SK
                if kod_dph_raw in ['VAT_IN', 'PDPH', 'PD']:
                    kod_dph = 'PD'
                elif kod_dph_raw in ['VAT_REVERSE', 'PP']:
                    kod_dph = 'PP'
                else:
                    kod_dph = kod_dph_raw or 'PD'
            
            vat_class = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}classificationVAT")
            ET.SubElement(vat_class, f"{{{self.NAMESPACES['typ']}}}ids").text = str(kod_dph)[:19]
            
            # Podľa XSD je element <inv:text> striktne vyžadovaný pri vytváraní nového dokladu
            ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}text").text = f"Prijatá faktúra {faktura.variabilny_symbol or faktura.id}"[:240]

            # 1. Podľa XSD musí ísť najprv adresa dodávateľa (partnerIdentity)
            self._vytvor_adresu_dodavatela(faktura.dodavatel, header)

            # Pridaná forma úhrady pre elimináciu varovania 603 (musí byť za adresou)
            payment_el = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}paymentType")
            ET.SubElement(payment_el, f"{{{self.NAMESPACES['typ']}}}paymentType").text = "draft"

            # 🏗️ ARCHITECT FIX: paymentAccount generujeme LEN vtedy, ak máme platný accountNo AJ 4-miestny bankCode (SK/CZ).
            # Pri zahraničných IBANoch (DE, AT...) obal nevytvárame, pretože bez 4-miestneho bankCode Pohoda zlyhá na XSD validácii.
            iban_raw = getattr(faktura, 'iban', None)
            if iban_raw and str(iban_raw).strip().lower() not in ['none', 'null', '', 'nan']:
                iban_clean = str(iban_raw).replace(" ", "")[:34]
                if len(iban_clean) >= 15 and iban_clean.startswith(('SK', 'CZ')):
                    payment_account = ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}paymentAccount")
                    ET.SubElement(payment_account, f"{{{self.NAMESPACES['typ']}}}accountNo").text = iban_clean
                    ET.SubElement(payment_account, f"{{{self.NAMESPACES['typ']}}}bankCode").text = iban_clean[4:8]

            # 2. Až potom môže nasledovať poznámka (note)
            if faktura.poznamka: ET.SubElement(header, f"{{{self.NAMESPACES['inv']}}}note").text = str(faktura.poznamka)

            invoice_detail = ET.SubElement(invoice, f"{{{self.NAMESPACES['inv']}}}invoiceDetail")
            
            # ARCHITECT FIX: Do priceNone idú IBA Neplatcovia (PN) a Oslobodené (U0). 
            # EU, PP a PZ MAJÚ základ dane a musia ísť do high/low/third!
            je_mimo_dph = kod_dph in ['PN', 'U0']

            for polozka in faktura.polozky_log.all():
                item = ET.SubElement(invoice_detail, f"{{{self.NAMESPACES['inv']}}}invoiceItem")
                ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}text").text = str(polozka.navrhovany_nazov or polozka.najdeny_kod_faktura)[:90]
                # 🛡️ DEFENSIVE: Ochrana pred pádom na Decimal('None') pri paušálnych položkách bez zadaného množstva
                mnozstvo_safe = Decimal(str(polozka.mnozstvo)) if polozka.mnozstvo is not None else Decimal('1.00')
                ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}quantity").text = self.formatuj_sumu(mnozstvo_safe)
                ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}payVAT").text = "false"
                
                cena_bez = polozka.nakupna_cena or Decimal('0.00')
                celkom_za_polozku = cena_bez * mnozstvo_safe
                
                # Defenzívne načítanie sadzby DPH pre prijaté faktúry (podpora navrhovana_sadzba_dph)
                pol_dph = getattr(polozka, 'navrhovana_sadzba_dph', getattr(polozka, 'sadzba_dph', None))
                def_zakladna_dec = self.zakladna_dph
                try:
                    sadzba_dph_val = Decimal(str(pol_dph)) if pol_dph is not None else (def_zakladna_dec if not je_mimo_dph else Decimal('0.00'))
                    if Decimal('0.00') < sadzba_dph_val <= Decimal('1.00'):
                        sadzba_dph_val = (sadzba_dph_val * Decimal('100.00')).quantize(Decimal('0.01'))
                except (ValueError, TypeError):
                    sadzba_dph_val = def_zakladna_dec if not je_mimo_dph else Decimal('0.00')

                if je_mimo_dph or sadzba_dph_val == Decimal('0.00'):
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}rateVAT").text = "none"
                elif Decimal('0.00') < sadzba_dph_val <= Decimal('10.00'):
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}rateVAT").text = "third"
                elif Decimal('10.00') < sadzba_dph_val <= self.znizena_dph:
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}rateVAT").text = "low"
                else:
                    ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}rateVAT").text = "high"

                home_currency = ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}homeCurrency")
                # ARCHITECT FIX: unitPrice na 4 desatinné miesta
                ET.SubElement(home_currency, f"{{{self.NAMESPACES['typ']}}}unitPrice").text = self.formatuj_jednotkovu_cenu(cena_bez)
                ET.SubElement(home_currency, f"{{{self.NAMESPACES['typ']}}}price").text = self.formatuj_sumu(celkom_za_polozku)

                # 🏗️ ARCHITECT FIX: Kaskáda predkontácie pre položku prijatej faktúry
                # 1. Na položke -> 2. Na faktúre -> 3. Na karte Dodávateľa -> 4. Globálny default
                predkontacia_polozky = (
                    getattr(polozka, 'erp_predkontacia', None) or 
                    getattr(faktura, 'erp_predkontacia', None) or 
                    (faktura.dodavatel and getattr(faktura.dodavatel, 'erp_predkontacia', None)) or 
                    getattr(self.nastavenia, 'pohoda_predkontacia_prijata_fa', None)
                )
                
                if predkontacia_polozky:
                    acc_item = ET.SubElement(item, f"{{{self.NAMESPACES['inv']}}}accounting")
                    ET.SubElement(acc_item, f"{{{self.NAMESPACES['typ']}}}ids").text = str(predkontacia_polozky).strip()[:19]

            # ✅ Oprava Summary (Hlavička faktúry)
            summary = ET.SubElement(invoice, f"{{{self.NAMESPACES['inv']}}}invoiceSummary")
            sum_currency = ET.SubElement(summary, f"{{{self.NAMESPACES['inv']}}}homeCurrency")
            
            suma_celkom = faktura.suma_na_uhradu or Decimal('0.00')
            
            if not je_mimo_dph:
                # ARCHITECT FIX: Dynamické zistenie sadzby z prvej položky
                sadzba_sumar = self.zakladna_dph
                prva_polozka = faktura.polozky_log.first()
                if prva_polozka:
                    try:
                        pol_dph_prva = getattr(prva_polozka, 'navrhovana_sadzba_dph', getattr(prva_polozka, 'sadzba_dph', None))
                        s = Decimal(str(pol_dph_prva)) if pol_dph_prva is not None else self.zakladna_dph
                        if Decimal('0.00') < s <= Decimal('1.00'):
                            s = (s * Decimal('100.00')).quantize(Decimal('0.01'))
                        if s > Decimal('0.00'):
                            sadzba_sumar = s
                    except (ValueError, TypeError):
                        pass

                # Dynamický výpočet koeficientu (napr. 1.23 pre 23%, 1.19 pre 19%, 1.21 pre 21%) s ochranou pred ZeroDivisionError
                sadzba_dec = sadzba_sumar
                koeficient = Decimal('1.00') + (sadzba_dec / Decimal('100.00'))
                if koeficient > Decimal('0.00'):
                    zaklad_dane = (suma_celkom / koeficient).quantize(Decimal('0.01'))
                else:
                    zaklad_dane = suma_celkom
                suma_dph = suma_celkom - zaklad_dane
                
                # Zápis do správneho tagu podľa sadzby
                if Decimal('0.00') < sadzba_sumar <= Decimal('10.00'):
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceThird").text = self.formatuj_sumu(zaklad_dane)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceThirdVAT").text = self.formatuj_sumu(suma_dph)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceThirdSum").text = self.formatuj_sumu(suma_celkom)
                elif Decimal('10.00') < sadzba_sumar <= self.znizena_dph:
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceLow").text = self.formatuj_sumu(zaklad_dane)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceLowVAT").text = self.formatuj_sumu(suma_dph)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceLowSum").text = self.formatuj_sumu(suma_celkom)
                else:
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceHigh").text = self.formatuj_sumu(zaklad_dane)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceHighVAT").text = self.formatuj_sumu(suma_dph)
                    ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceHighSum").text = self.formatuj_sumu(suma_celkom)
            else:
                ET.SubElement(sum_currency, f"{{{self.NAMESPACES['typ']}}}priceNone").text = self.formatuj_sumu(suma_celkom)

        return self.ziskaj_xml_string(root)


    # =========================================================================
    # 3. GENERÁTOR UNIVERZÁLNEHO ADRESÁRA 📇
    # (Zákazníci, Dodávatelia, Zamestnanci, Súkromné osoby + IBAN)
    # =========================================================================
    def vygeneruj_adresare_xml(self, queryset_partnerov):
        batch_id = str(uuid.uuid4())[:8].upper()
        root = self._vytvor_koren_dokumentu(batch_id)

        for partner in queryset_partnerov:
            typ_modelu = partner.__class__.__name__
            
            # ARCHITECT FIX: Rozlíšenie medzi Firmou a Fyzickou osobou pre Pohodu
            je_fyzicka_osoba = False
            if typ_modelu == 'Zamestnanec': 
                nazov = f"{partner.meno} {partner.priezvisko}"
                je_fyzicka_osoba = True
            elif typ_modelu == 'SukromnaOsoba': 
                nazov = partner.meno_a_priezvisko
                je_fyzicka_osoba = True
            else: 
                nazov = getattr(partner, 'Nazov_spolocnosti', 'Neznámy partner')

            data_pack_item = ET.SubElement(root, f"{{{self.NAMESPACES['dat']}}}dataPackItem", {'id': f"ADR_{typ_modelu}_{partner.id}", 'version': "2.0"})
            adb = ET.SubElement(data_pack_item, f"{{{self.NAMESPACES['adb']}}}addressbook", {'version': "2.0"})
            header = ET.SubElement(adb, f"{{{self.NAMESPACES['adb']}}}addressbookHeader")
            
            identity = ET.SubElement(header, f"{{{self.NAMESPACES['adb']}}}identity")
            address = ET.SubElement(identity, f"{{{self.NAMESPACES['typ']}}}address")
            
            # ARCHITECT FIX: Ak je to FO, zapisujeme do <typ:name>, inak do <typ:company>
            if je_fyzicka_osoba:
                ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}name").text = str(nazov)[:255]
            else:
                ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = str(nazov)[:255]
                # ARCHITECT FIX: Zapíšeme kontaktnú osobu (ošetrené k/K) do poľa Meno, čím sa zobrazí presne pod názvom firmy
                kontakt_osoba = getattr(partner, 'kontaktna_osoba', getattr(partner, 'kontaktna_osoba', None))
                if kontakt_osoba:
                    ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}name").text = str(kontakt_osoba)[:255]
            
            ulica = getattr(partner, 'ulica_a_cislo', getattr(partner, 'adresa', None))
            if ulica: ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}street").text = str(ulica)[:64]
            # Defenzívne mesto pre Pohoda Adresár (vyžadované schémou adb:addressbook)
            mesto = getattr(partner, 'mesto', None) or getattr(self.identita, 'sidlo_mesto', None) or 'Nezadané'
            ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}city").text = str(mesto)[:45]
            psc = getattr(partner, 'psc', None)
            if psc: ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}zip").text = str(psc).replace(" ", "")[:15]
            stat = getattr(partner, 'stat', None)
            if stat: 
                country = ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}country")
                ET.SubElement(country, f"{{{self.NAMESPACES['typ']}}}ids").text = self._get_iso_stat(stat)[:20]
            
            ico = getattr(partner, 'ico', None)
            if ico: ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}ico").text = str(ico).replace(" ", "")
            dic = getattr(partner, 'dic', None)
            if dic: ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}dic").text = str(dic).replace(" ", "")
            ic_dph = getattr(partner, 'ic_dph', None)
            if ic_dph: ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}icDph").text = str(ic_dph).replace(" ", "")

            email_val = getattr(partner, 'email', getattr(partner, 'email', None))
            telefon_val = getattr(partner, 'telefon', getattr(partner, 'telefon', None))
            # ARCHITECT FIX: Načítanie webovej adresy partnera (ošetrené w/W)
            web_val = getattr(partner, 'domena', None)
            # Podľa XSD schémy adresára musí ísť prvok 'phone' pred prvkom 'email'
            if telefon_val: ET.SubElement(header, f"{{{self.NAMESPACES['adb']}}}phone").text = str(telefon_val)[:40]
            if email_val: ET.SubElement(header, f"{{{self.NAMESPACES['adb']}}}email").text = str(email_val)[:98]
            # ARCHITECT FIX: Zápis webovej adresy partnera do XML podľa XSD schémy Pohody
            if web_val: ET.SubElement(header, f"{{{self.NAMESPACES['adb']}}}web").text = str(web_val)[:255]

            # 🏦 IBAN pre Záväzky a vyplácanie diét
            iban = getattr(partner, 'iban', None)
            if iban:
                account = ET.SubElement(header, f"{{{self.NAMESPACES['adb']}}}paymentAccount")
                ET.SubElement(account, f"{{{self.NAMESPACES['adb']}}}accountNo").text = str(iban).replace(" ", "")

        return self.ziskaj_xml_string(root)
    
    # =========================================================================
    # 5. GENERÁTOR TANKOVANÍ (POKLADŇA A ZÁVÄZKY) ⛽
    # =========================================================================
    def vygeneruj_tankovania_xml(self, queryset_tankovani):
        batch_id = str(uuid.uuid4())[:8].upper()
        root = self._vytvor_koren_dokumentu(batch_id)

        for tankovanie in queryset_tankovani:
            # 🛑 Blokovanie duplicít z CCS kariet (Rieši sa cez zbernú FA)
            if tankovanie.sposob_zauctovania_erp == 'FAKTURA':
                continue 

            data_pack_item = ET.SubElement(root, f"{{{self.NAMESPACES['dat']}}}dataPackItem", {'id': f"TANK_{tankovanie.id}", 'version': "2.0"})
            spz_vozidla = getattr(tankovanie.vozidlo, 'spz', '') if getattr(tankovanie, 'vozidlo', None) else ''
            text_polozky = f"PHM {spz_vozidla} - {getattr(tankovanie, 'litre', '') or '0'}L".strip()
            
            # --- VÝDAVKOVÝ POKLADNIČNÝ DOKLAD (HOTOVOSŤ FIRMY) 💵 ---
            if tankovanie.sposob_zauctovania_erp == 'FIREMNY_UCET' and tankovanie.sposob_platby == 'HOTOVOST':
                doc = ET.SubElement(data_pack_item, f"{{{self.NAMESPACES['vch']}}}voucher", {'version': "2.0"})
                header = ET.SubElement(doc, f"{{{self.NAMESPACES['vch']}}}voucherHeader")
                detail = ET.SubElement(doc, f"{{{self.NAMESPACES['vch']}}}voucherDetail")
                item_tag = f"{{{self.NAMESPACES['vch']}}}voucherItem"
                ns = 'vch'
                
                # 1. voucherType (Podľa XSD musí byť 'expense' pre výdavkový doklad)
                ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}voucherType").text = "expense"
                
                # 2. cashAccount (Podľa XSD musí ísť hneď za voucherType)
                cashAccount = ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}cashAccount")
                ET.SubElement(cashAccount, f"{{{self.NAMESPACES['typ']}}}ids").text = str(self.nastavenia.ucet_pokladna)[:19]

                # 3. OPRAVA: V pokladni (voucher) element symVar NEEXISTUJE. Musí sa použiť number.
                if tankovanie.cislo_dokladu:
                    number_el = ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}number")
                    ET.SubElement(number_el, f"{{{self.NAMESPACES['typ']}}}numberRequested").text = str(tankovanie.cislo_dokladu)[:20]
                
                # 4. date a súvisiace účtovné dátumy (Eliminácia varovania 603)
                ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}date").text = self.formataj_datum(tankovanie.datum)
                ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}dateTax").text = self.formataj_datum(tankovanie.datum)
                ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}dateAccounting").text = self.formataj_datum(tankovanie.datum)
                
                # 5. text
                ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}text").text = text_polozky
                
                # 6. partnerIdentity
                if tankovanie.sposob_zauctovania_erp == 'SUKROMNE_PENIAZE':
                    partner_id = ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}partnerIdentity")
                    address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
                    
                    nazov_partnera = "Neznáma osoba"
                    if tankovanie.vodič:
                        nazov_partnera = f"{tankovanie.vodič.meno} {tankovanie.vodič.priezvisko}"
                    elif tankovanie.osoba:
                        nazov_partnera = tankovanie.osoba.meno_a_priezvisko
                    ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = nazov_partnera
                    
                    # Poznámka musí ísť až za partnerIdentity
                    ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}note").text = "⚠️ Záväzok - Preplatiť palivo"
                elif getattr(tankovanie, 'cerpacia_stanica', None):
                    partner_id = ET.SubElement(header, f"{{{self.NAMESPACES[ns]}}}partnerIdentity")
                    address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
                    cs = tankovanie.cerpacia_stanica
                    
                    ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = str(cs.Nazov_spolocnosti or 'Neznáma ČS')[:255]
                    # ARCHITECT FIX: Doplnenie kompletnej adresy čerpacej stanice
                    if getattr(cs, 'mesto', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}city").text = str(cs.mesto)[:45]
                    if getattr(cs, 'ulica_a_cislo', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}street").text = str(cs.ulica_a_cislo)[:64]
                    if getattr(cs, 'psc', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}zip").text = str(cs.psc).replace(" ", "")[:15]
                    if getattr(cs, 'ico', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}ico").text = str(cs.ico).replace(" ", "")[:15]
                    if getattr(cs, 'dic', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}dic").text = str(cs.dic).replace(" ", "")[:18]
                    if getattr(cs, 'ic_dph', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}icDph").text = str(cs.ic_dph).replace(" ", "")[:18]

                # 7. Predkontácia a DPH na hlavičke (Eliminácia varovania 603)
                if self.nastavenia.ucet_naklady_palivo:
                    acc_header = ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}accounting")
                    ET.SubElement(acc_header, f"{{{self.NAMESPACES['typ']}}}ids").text = str(self.nastavenia.ucet_naklady_palivo).strip()[:19]
                
                if tankovanie.kod_clenenia_dph:
                    vat_class_header = ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}classificationVAT")
                    ET.SubElement(vat_class_header, f"{{{self.NAMESPACES['typ']}}}ids").text = str(tankovanie.kod_clenenia_dph)[:19]

            # --- INTERNÝ DOKLAD (KARTA FIRMY / ZÁVÄZOK ZAMESTNANCA) 💳 ---
            else:
                doc = ET.SubElement(data_pack_item, f"{{{self.NAMESPACES['int']}}}intDoc", {'version': "2.0"})
                header = ET.SubElement(doc, f"{{{self.NAMESPACES['int']}}}intDocHeader")
                detail = ET.SubElement(doc, f"{{{self.NAMESPACES['int']}}}intDocDetail")
                item_tag = f"{{{self.NAMESPACES['int']}}}intDocItem"
                ns = 'int'


                # 1. symVar
                if tankovanie.cislo_dokladu:
                    ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}symVar").text = str(tankovanie.cislo_dokladu)[:20]
                # 2. date a súvisiace účtovné dátumy (Eliminácia varovania 603)
                ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}date").text = self.formataj_datum(tankovanie.datum)
                ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}dateTax").text = self.formataj_datum(tankovanie.datum)
                ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}dateAccounting").text = self.formataj_datum(tankovanie.datum)
                
                # 3. text (Povinný element)
                ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}text").text = text_polozky
                
                # 4. partnerIdentity (Musí byť presne v tomto poradí podľa XSD)
                if tankovanie.sposob_zauctovania_erp == 'SUKROMNE_PENIAZE':
                    partner_id = ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}partnerIdentity")
                    address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
                    
                    nazov_partnera = "Neznáma osoba"
                    if tankovanie.vodič:
                        nazov_partnera = f"{tankovanie.vodič.meno} {tankovanie.vodič.priezvisko}"
                    elif tankovanie.osoba:
                        nazov_partnera = tankovanie.osoba.meno_a_priezvisko
                    ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = nazov_partnera
                    
                    # Poznámka musí ísť až za partnerIdentity
                    ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}note").text = "⚠️ Záväzok - Preplatiť palivo"
                elif getattr(tankovanie, 'cerpacia_stanica', None):
                    partner_id = ET.SubElement(header, f"{{{self.NAMESPACES[ns]}}}partnerIdentity")
                    address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
                    cs = tankovanie.cerpacia_stanica
                    
                    ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = str(cs.Nazov_spolocnosti or 'Neznáma ČS')[:255]
                    # ARCHITECT FIX: Doplnenie kompletnej adresy čerpacej stanice
                    if getattr(cs, 'mesto', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}city").text = str(cs.mesto)[:45]
                    if getattr(cs, 'ulica_a_cislo', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}street").text = str(cs.ulica_a_cislo)[:64]
                    if getattr(cs, 'psc', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}zip").text = str(cs.psc).replace(" ", "")[:15]
                    if getattr(cs, 'ico', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}ico").text = str(cs.ico).replace(" ", "")[:15]
                    if getattr(cs, 'dic', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}dic").text = str(cs.dic).replace(" ", "")[:18]
                    if getattr(cs, 'ic_dph', None): ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}icDph").text = str(cs.ic_dph).replace(" ", "")[:18]

                # 5. Predkontácia a DPH na hlavičke (Eliminácia varovania 603)
                if self.nastavenia.ucet_naklady_palivo:
                    acc_header = ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}accounting")
                    ET.SubElement(acc_header, f"{{{self.NAMESPACES['typ']}}}ids").text = str(self.nastavenia.ucet_naklady_palivo).strip()[:19]
                
                if tankovanie.kod_clenenia_dph:
                    vat_class_header = ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}classificationVAT")
                    ET.SubElement(vat_class_header, f"{{{self.NAMESPACES['typ']}}}ids").text = str(tankovanie.kod_clenenia_dph)[:19]

            # --- POLOŽKA DOKLADU (voucherItem / intDocItem) ---
            item = ET.SubElement(detail, item_tag)
            ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}text").text = text_polozky
            ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}quantity").text = self.formatuj_sumu(tankovanie.litre)
            ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}unit").text = "L"
            
            # Defenzívne ošetrenie None hodnôt pred matematickými operáciami
            cena_bez_dph_safe = tankovanie.cena_bez_dph if tankovanie.cena_bez_dph is not None else Decimal('0.00')
            cena_celkom_safe = tankovanie.cena_celkom if tankovanie.cena_celkom is not None else Decimal('0.00')
            try:
                litre_dec = Decimal(str(tankovanie.litre)) if getattr(tankovanie, 'litre', None) is not None else Decimal('0.00')
            except (ValueError, TypeError):
                litre_dec = Decimal('0.00')
            bezpecne_litre = litre_dec if litre_dec > Decimal('0.00') else Decimal('1.00')

            try:
                sadzba_dph_val = Decimal(str(tankovanie.sadzba_dph)) if getattr(tankovanie, 'sadzba_dph', None) is not None else Decimal('0.00')
                if Decimal('0.00') < sadzba_dph_val <= Decimal('1.00'):
                    sadzba_dph_val = (sadzba_dph_val * Decimal('100.00')).quantize(Decimal('0.01'))
            except (ValueError, TypeError):
                sadzba_dph_val = Decimal('0.00')

            uplatnit_dph = (sadzba_dph_val > Decimal('0.00'))

            # Explicitne povieme Pohode, že ceny posielame BEZ DPH
            ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}payVAT").text = "false"

            if not uplatnit_dph or sadzba_dph_val == Decimal('0.00'):
                ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}rateVAT").text = "none"
                home_currency = ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}homeCurrency")
                # ARCHITECT FIX: unitPrice na 4 desatinné miesta pre absolútnu presnosť
                ET.SubElement(home_currency, f"{{{self.NAMESPACES['typ']}}}unitPrice").text = self.formatuj_jednotkovu_cenu(cena_celkom_safe / bezpecne_litre)
                ET.SubElement(home_currency, f"{{{self.NAMESPACES['typ']}}}price").text = self.formatuj_sumu(cena_celkom_safe)
            else:
                if Decimal('0.00') < sadzba_dph_val <= Decimal('10.00'):
                    ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}rateVAT").text = "third"
                elif Decimal('10.00') < sadzba_dph_val <= self.znizena_dph:
                    ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}rateVAT").text = "low"
                else:
                    ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}rateVAT").text = "high"
                    
                home_currency = ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}homeCurrency")
                # ARCHITECT FIX: unitPrice na 4 desatinné miesta pre absolútnu presnosť
                ET.SubElement(home_currency, f"{{{self.NAMESPACES['typ']}}}unitPrice").text = self.formatuj_jednotkovu_cenu(cena_bez_dph_safe / bezpecne_litre)
                ET.SubElement(home_currency, f"{{{self.NAMESPACES['typ']}}}price").text = self.formatuj_sumu(cena_bez_dph_safe)


            # Účtovné predkontácie a strediská (Iba ak sú vyplnené v databáze)
            if self.nastavenia.ucet_naklady_palivo:
                acc = ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}accounting")
                ET.SubElement(acc, f"{{{self.NAMESPACES['typ']}}}ids").text = str(self.nastavenia.ucet_naklady_palivo)[:19]

            if tankovanie.kod_clenenia_dph:
                vat_class = ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}classificationVAT")
                ET.SubElement(vat_class, f"{{{self.NAMESPACES['typ']}}}ids").text = str(tankovanie.kod_clenenia_dph)[:19]

            erp_stredisko = getattr(tankovanie.vozidlo, 'erp_stredisko', None) if getattr(tankovanie, 'vozidlo', None) else None
            if erp_stredisko:
                centre = ET.SubElement(item, f"{{{self.NAMESPACES[ns]}}}centre")
                ET.SubElement(centre, f"{{{self.NAMESPACES['typ']}}}ids").text = str(erp_stredisko)[:19]

        return self.ziskaj_xml_string(root)

    # =========================================================================
    # 7. GENERÁTOR EVIDENCIE ÚHRAD (LIKVIDÁCIA FAKTÚR) 💸
    # =========================================================================
    def vygeneruj_uhrady_xml(self, queryset_uhrad):
        """
        Vygeneruje XML pre import úhrad (likvidácií) faktúr do Pohody.
        STRIKTNÉ XSD PORADIE:
        - Header: documentType -> account -> statementNumber -> symVar -> datePayment -> text
        - Item: sourceDocument -> text -> homeCurrency
        """
        batch_id = str(uuid.uuid4())[:8].upper()
        root = self._vytvor_koren_dokumentu(batch_id)

        for uhrada in queryset_uhrad:
            data_pack_item = ET.SubElement(root, f"{{{self.NAMESPACES['dat']}}}dataPackItem", {
                'id': f"UHR_{uhrada.id}", 
                'version': "2.0"
            })
            
            typ_dokladu = uhrada.typ_uhrady
            smer_prijem = True
            var_sym = ""
            cislo_faktury = ""
            source_agenda = ""

            if uhrada.vystavena_faktura:
                cislo_faktury = uhrada.vystavena_faktura.cislo_faktury
                var_sym = getattr(uhrada.vystavena_faktura, 'variabilny_symbol', cislo_faktury)
                # 🏗️ ISAS ARCHITECT FIX: Ak hradíme Dobropis, peniaze odchádzajú z firmy (Expense)
                je_dobropis = getattr(uhrada.vystavena_faktura, 'typ_dokladu', 'FAKTURA') == 'DOBROPIS'
                smer_prijem = not je_dobropis
                source_agenda = "issuedInvoice"
            elif uhrada.prijata_faktura:
                cislo_faktury = getattr(uhrada.prijata_faktura, 'cislo_faktury', str(uhrada.prijata_faktura.id))
                var_sym = getattr(uhrada.prijata_faktura, 'variabilny_symbol', cislo_faktury)
                smer_prijem = False
                source_agenda = "receivedInvoice"

            text_uhrady = f"Úhrada dokladu {cislo_faktury}"
            # ARCHITECT FIX: Poznámku nezliepame do textu, pošleme ju do natívneho tagu <note>
            
            # --- A. BANKOVÁ ÚHRADA (BV) ---
            if typ_dokladu == 'BV':
                doc = ET.SubElement(data_pack_item, f"{{{self.NAMESPACES['bnk']}}}bank", {'version': "2.0"})
                
                # 1. HLAVIČKA (Striktné poradie)
                header = ET.SubElement(doc, f"{{{self.NAMESPACES['bnk']}}}bankHeader")
                
                # OPRAVA: Element sa volá bankType, nie documentType
                ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}bankType").text = "receipt" if smer_prijem else "expense"
                
                account = ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}account")
                # 🏗️ ARCHITECT FIX: Defenzívny prístup. Ak by hodnota zlyhala, pošleme prázdny string, nie fiktívnu banku.
                ET.SubElement(account, f"{{{self.NAMESPACES['typ']}}}ids").text = str(getattr(self.nastavenia, 'ucet_banka', '')).strip()[:19]
                
                if uhrada.cislo_dokladu:
                    statement = ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}statementNumber")
                    # OPRAVA: Pod-element sa volá statementNumber (max 10 znakov)
                    ET.SubElement(statement, f"{{{self.NAMESPACES['bnk']}}}statementNumber").text = str(uhrada.cislo_dokladu)[:10]
                
                if var_sym:
                    ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}symVar").text = str(var_sym)[:20]
                    
                if uhrada.datum_uhrady:
                    # Dátum výpisu (dateStatement) musí ísť podľa XSD pred dátumom platby (datePayment)
                    ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}dateStatement").text = self.formataj_datum(uhrada.datum_uhrady)
                    ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}datePayment").text = self.formataj_datum(uhrada.datum_uhrady)
                
                # Predkontácia (accounting) musí ísť podľa XSD hneď za dátumami
                accounting = ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}accounting")
                predkontacia = self.nastavenia.pohoda_predkontacia_banka_prijem if smer_prijem else self.nastavenia.pohoda_predkontacia_banka_vydaj
                ET.SubElement(accounting, f"{{{self.NAMESPACES['typ']}}}ids").text = str(predkontacia)[:19]

                # ARCHITECT FIX: Doplnenie partnerIdentity do hlavičky banky, aby svietila Firma
                partner = (getattr(uhrada.vystavena_faktura, 'zakaznik', None) or getattr(uhrada.vystavena_faktura, 'odberatel', None)) if uhrada.vystavena_faktura else getattr(uhrada.prijata_faktura, 'dodavatel', None)
                if partner:
                    partner_id = ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}partnerIdentity")
                    address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
                    nazov_partnera = getattr(partner, 'Nazov_spolocnosti', getattr(partner, 'nazov', 'Neznámy partner'))
                    ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = str(nazov_partnera)[:255]
                    if getattr(partner, 'ico', None):
                        ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}ico").text = str(partner.ico).replace(" ", "")[:15]

                # ARCHITECT FIX: Prenos IBANu partnera do bankového výpisu
                if uhrada.protiucet_iban:
                    payment_acc = ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}paymentAccount")
                    ET.SubElement(payment_acc, f"{{{self.NAMESPACES['bnk']}}}accountNo").text = str(uhrada.protiucet_iban).replace(" ", "")[:50]

                # Text musí ísť podľa XSD až za partnerIdentity a paymentAccount
                ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}text").text = text_uhrady[:240]

                # ARCHITECT FIX: Zápis poznámky do správneho tagu (musí ísť na koniec hlavičky)
                if uhrada.poznamka:
                    ET.SubElement(header, f"{{{self.NAMESPACES['bnk']}}}note").text = str(uhrada.poznamka)

                # 2. POLOŽKA (OPRAVA: Likvidácia musí ísť cez bankLiquidationItem)
                detail = ET.SubElement(doc, f"{{{self.NAMESPACES['bnk']}}}bankDetail")
                
                if cislo_faktury and source_agenda:
                    liq_item = ET.SubElement(detail, f"{{{self.NAMESPACES['bnk']}}}bankLiquidationItem")
                    
                    # A. Nastavenie likvidácie (Väzba na faktúru)
                    settings_liq = ET.SubElement(liq_item, f"{{{self.NAMESPACES['bnk']}}}settingsLiquidation")
                    ET.SubElement(settings_liq, f"{{{self.NAMESPACES['bnk']}}}sourceAgenda").text = source_agenda
                    src_doc = ET.SubElement(settings_liq, f"{{{self.NAMESPACES['bnk']}}}sourceDocument")
                    ET.SubElement(src_doc, f"{{{self.NAMESPACES['typ']}}}number").text = str(cislo_faktury)[:32]
                    
                    # B. Nastavenie sumy likvidácie (Suma ide do liquidationPrice v rámci settingsLiquidation)
                    ET.SubElement(settings_liq, f"{{{self.NAMESPACES['bnk']}}}liquidationPrice").text = self.formatuj_sumu(uhrada.suma_uhrady)
                    
                    # C. Samotná položka likvidácie (Pre banku musia byť tagy striktne 'bnk')
                    liq_item_detail = ET.SubElement(liq_item, f"{{{self.NAMESPACES['bnk']}}}liquidationItem")
                    ET.SubElement(liq_item_detail, f"{{{self.NAMESPACES['bnk']}}}quantity").text = "1"
                    liq_home_cur = ET.SubElement(liq_item_detail, f"{{{self.NAMESPACES['bnk']}}}homeCurrency")
                    # OPRAVA: XSD schéma bank.xsd striktne vyžaduje, aby unitPrice v likvidácii bol 'bnk', nie 'typ'
                    ET.SubElement(liq_home_cur, f"{{{self.NAMESPACES['bnk']}}}unitPrice").text = self.formatuj_sumu(uhrada.suma_uhrady)
                else:
                    # Fallback ak by chýbalo číslo faktúry (vytvorí sa bežná položka bez párovania)
                    item = ET.SubElement(detail, f"{{{self.NAMESPACES['bnk']}}}bankItem")
                    ET.SubElement(item, f"{{{self.NAMESPACES['bnk']}}}text").text = text_uhrady[:90]
                    item_home_cur = ET.SubElement(item, f"{{{self.NAMESPACES['bnk']}}}homeCurrency")
                    # OPRAVA: Banka používa výhradne unitPrice a podľa bank.xsd patrí do menného priestoru 'bnk', nie 'typ'
                    ET.SubElement(item_home_cur, f"{{{self.NAMESPACES['bnk']}}}unitPrice").text = self.formatuj_sumu(uhrada.suma_uhrady)

                # 3. SÚHRN
                summary = ET.SubElement(doc, f"{{{self.NAMESPACES['bnk']}}}bankSummary")
                home_cur = ET.SubElement(summary, f"{{{self.NAMESPACES['bnk']}}}homeCurrency")
                ET.SubElement(home_cur, f"{{{self.NAMESPACES['typ']}}}priceNone").text = self.formatuj_sumu(uhrada.suma_uhrady)

            # --- B. POKLADNIČNÁ ÚHRADA (PPD / VPD) ---
            elif typ_dokladu in ['PPD', 'VPD']:
                doc = ET.SubElement(data_pack_item, f"{{{self.NAMESPACES['vch']}}}voucher", {'version': "2.0"})
                
                # 1. HLAVIČKA
                header = ET.SubElement(doc, f"{{{self.NAMESPACES['vch']}}}voucherHeader")
                
                # OPRAVA: Správne typy podľa XSD sú 'receipt' a 'expense'
                ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}voucherType").text = "receipt" if typ_dokladu == 'PPD' else "expense"
                
                # OPRAVA: Správny názov elementu je cashAccount
                cash_account = ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}cashAccount")
                ET.SubElement(cash_account, f"{{{self.NAMESPACES['typ']}}}ids").text = str(self.nastavenia.ucet_pokladna)[:19]

                if uhrada.cislo_dokladu:
                    number_el = ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}number")
                    ET.SubElement(number_el, f"{{{self.NAMESPACES['typ']}}}numberRequested").text = str(uhrada.cislo_dokladu)[:20]

                if uhrada.datum_uhrady:
                    ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}date").text = self.formataj_datum(uhrada.datum_uhrady)
                    ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}datePayment").text = self.formataj_datum(uhrada.datum_uhrady)
                
                # 1. Text musí ísť podľa XSD pred partnerIdentity
                ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}text").text = text_uhrady[:240]

                # 2. ARCHITECT FIX: Doplnenie partnerIdentity do hlavičky pokladne
                partner = (getattr(uhrada.vystavena_faktura, 'zakaznik', None) or getattr(uhrada.vystavena_faktura, 'odberatel', None)) if uhrada.vystavena_faktura else getattr(uhrada.prijata_faktura, 'dodavatel', None)
                if partner:
                    partner_id = ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}partnerIdentity")
                    address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
                    nazov_partnera = getattr(partner, 'Nazov_spolocnosti', getattr(partner, 'nazov', 'Neznámy partner'))
                    ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = str(nazov_partnera)[:255]
                    if getattr(partner, 'ico', None):
                        ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}ico").text = str(partner.ico).replace(" ", "")[:15]

                # 3. symPar musí ísť podľa XSD až za partnerIdentity
                if var_sym:
                    ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}symPar").text = str(var_sym)[:20]

                # ARCHITECT FIX: Zápis poznámky do Pokladne (musí ísť striktne za symPar)
                if uhrada.poznamka:
                    ET.SubElement(header, f"{{{self.NAMESPACES['vch']}}}note").text = str(uhrada.poznamka)

                # 2. POLOŽKA (OPRAVA: Úplne nová štruktúra pre likvidáciu podľa vzoru Stormware)
                detail = ET.SubElement(doc, f"{{{self.NAMESPACES['vch']}}}voucherDetail")
                
                if cislo_faktury and source_agenda:
                    liq_item = ET.SubElement(detail, f"{{{self.NAMESPACES['vch']}}}voucherLiquidationItem")
                    
                    # A. Nastavenie likvidácie (Väzba na faktúru)
                    settings_liq = ET.SubElement(liq_item, f"{{{self.NAMESPACES['vch']}}}settingsLiquidation")
                    ET.SubElement(settings_liq, f"{{{self.NAMESPACES['vch']}}}sourceAgenda").text = source_agenda
                    src_doc = ET.SubElement(settings_liq, f"{{{self.NAMESPACES['vch']}}}sourceDocument")
                    ET.SubElement(src_doc, f"{{{self.NAMESPACES['typ']}}}number").text = str(cislo_faktury)[:32]
                    
                    # B. Nastavenie sumy likvidácie (Suma ide do liquidationPrice v rámci settingsLiquidation)
                    ET.SubElement(settings_liq, f"{{{self.NAMESPACES['vch']}}}liquidationPrice").text = self.formatuj_sumu(uhrada.suma_uhrady)
                    
                    # C. Samotná položka likvidácie (Aj pokladňa striktne vyžaduje liquidationItem!)
                    liq_item_detail = ET.SubElement(liq_item, f"{{{self.NAMESPACES['vch']}}}liquidationItem")
                    ET.SubElement(liq_item_detail, f"{{{self.NAMESPACES['vch']}}}quantity").text = "1"
                    liq_home_cur = ET.SubElement(liq_item_detail, f"{{{self.NAMESPACES['vch']}}}homeCurrency")
                    ET.SubElement(liq_home_cur, f"{{{self.NAMESPACES['typ']}}}unitPrice").text = self.formatuj_sumu(uhrada.suma_uhrady)
                else:
                    # Fallback ak by chýbalo číslo faktúry (vytvorí sa bežná položka bez párovania)
                    item = ET.SubElement(detail, f"{{{self.NAMESPACES['vch']}}}voucherItem")
                    ET.SubElement(item, f"{{{self.NAMESPACES['vch']}}}text").text = text_uhrady[:90]
                    item_home_cur = ET.SubElement(item, f"{{{self.NAMESPACES['vch']}}}homeCurrency")
                    ET.SubElement(item_home_cur, f"{{{self.NAMESPACES['typ']}}}price").text = self.formatuj_sumu(uhrada.suma_uhrady)

                # 3. SÚHRN (Doplnené pre architektonickú konzistenciu s Bankou a Internými dokladmi)
                summary = ET.SubElement(doc, f"{{{self.NAMESPACES['vch']}}}voucherSummary")
                home_cur = ET.SubElement(summary, f"{{{self.NAMESPACES['vch']}}}homeCurrency")
                ET.SubElement(home_cur, f"{{{self.NAMESPACES['typ']}}}priceNone").text = self.formatuj_sumu(uhrada.suma_uhrady)

            # --- C. INTERNÝ DOKLAD / PLATBA KARTOU / ZÁPOČET (PK / VZ) ---
            else:
                doc = ET.SubElement(data_pack_item, f"{{{self.NAMESPACES['int']}}}intDoc", {'version': "2.0"})
                
                # 1. HLAVIČKA (Striktné poradie)
                header = ET.SubElement(doc, f"{{{self.NAMESPACES['int']}}}intDocHeader")
                
                if uhrada.cislo_dokladu:
                    number_el = ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}number")
                    ET.SubElement(number_el, f"{{{self.NAMESPACES['typ']}}}numberRequested").text = str(uhrada.cislo_dokladu)[:20]

                if var_sym:
                    ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}symVar").text = str(var_sym)[:20]

                if uhrada.datum_uhrady:
                    ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}date").text = self.formataj_datum(uhrada.datum_uhrady)
                
                # 1. Text musí ísť podľa XSD pred partnerIdentity
                ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}text").text = text_uhrady[:240]

                # 2. ARCHITECT FIX: Doplnenie partnerIdentity do hlavičky interného dokladu
                partner = (getattr(uhrada.vystavena_faktura, 'zakaznik', None) or getattr(uhrada.vystavena_faktura, 'odberatel', None)) if uhrada.vystavena_faktura else getattr(uhrada.prijata_faktura, 'dodavatel', None)
                if partner:
                    partner_id = ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}partnerIdentity")
                    address = ET.SubElement(partner_id, f"{{{self.NAMESPACES['typ']}}}address")
                    nazov_partnera = getattr(partner, 'Nazov_spolocnosti', getattr(partner, 'nazov', 'Neznámy partner'))
                    ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}company").text = str(nazov_partnera)[:255]
                    if getattr(partner, 'ico', None):
                        ET.SubElement(address, f"{{{self.NAMESPACES['typ']}}}ico").text = str(partner.ico).replace(" ", "")[:15]

                # ARCHITECT FIX: Zápis poznámky do Interného dokladu (musí ísť pred Detail)
                if uhrada.poznamka:
                    ET.SubElement(header, f"{{{self.NAMESPACES['int']}}}note").text = str(uhrada.poznamka)

                # 3. POLOŽKA (Striktné poradie: sourceDocument MUSÍ byť prvý)
                detail = ET.SubElement(doc, f"{{{self.NAMESPACES['int']}}}intDocDetail")
                item = ET.SubElement(detail, f"{{{self.NAMESPACES['int']}}}intDocItem")

                ET.SubElement(item, f"{{{self.NAMESPACES['int']}}}text").text = text_uhrady[:90]
                
                item_home_cur = ET.SubElement(item, f"{{{self.NAMESPACES['int']}}}homeCurrency")
                ET.SubElement(item_home_cur, f"{{{self.NAMESPACES['typ']}}}price").text = self.formatuj_sumu(uhrada.suma_uhrady)

                # 3. SÚHRN
                summary = ET.SubElement(doc, f"{{{self.NAMESPACES['int']}}}intDocSummary")
                home_cur = ET.SubElement(summary, f"{{{self.NAMESPACES['int']}}}homeCurrency")
                ET.SubElement(home_cur, f"{{{self.NAMESPACES['typ']}}}priceNone").text = self.formatuj_sumu(uhrada.suma_uhrady)

        return self.ziskaj_xml_string(root)