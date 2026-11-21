import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from django.apps import apps
import uuid
from Evidencia.utils_helper import get_firemna_mena
from Evidencia.industry_loader import get_industry_term

# =========================================================================
# 🇪🇺 ISAS XML ENGINE: PEPPOL BIS BILLING 3.0 (EN 16931 UBL 2.1 🛡️)
# =========================================================================

class PeppolXMLGenerator:
    """
    Ultimátny XML Generátor pre európsku sieť Peppol (UBL 2.1).
    Pripravené pre európsky štandard EN 16931 (eFaktúra).
    Striktne dodržiava Schematron validácie, mapuje UN/ECE číselníky
    a garantuje na cent presnú matematiku v TaxTotal uzloch.
    """
    
    NAMESPACES = {
        'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
        'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
        '': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2' # Default namespace
    }

    # UN/ECE Recommendation 20 (Merné jednotky)
    UNIT_MAPPING = {
        'ks': 'C62',      # Piece (Kus)
        'hod': 'HUR',     # Hour (Hodina)
        'l': 'LTR',       # Liter
        'km': 'KMT',      # Kilometer
        'm': 'MTR',       # Meter
        'm2': 'MTK',      # Square meter
        'kg': 'KGM',      # Kilogram
        'paušál': 'LS',   # Lump sum
        'sada': 'SET',    # Set
    }

    # UNCL4461 (Spôsoby úhrady)
    PAYMENT_MAPPING = {
        'PREVOD': '30',   # Credit transfer
        'HOTOVOST': '10', # In cash
        'KARTA': '48',    # Bank card
    }

    def __init__(self, identita=None):
        for prefix, uri in self.NAMESPACES.items():
            ET.register_namespace(prefix, uri)
            
        FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
        self.identita = identita or (FiremnaIdentita.objects.first() if FiremnaIdentita else None)

    def _format_date(self, datum):
        if not datum: return None
        if hasattr(datum, 'strftime'): return datum.strftime('%Y-%m-%d')
        return str(datum)

    def _format_amount(self, suma):
        """Peppol vyžaduje striktne 2 desatinné miesta pre sumy a elimináciu záporných núl (-0.00)."""
        if not suma: return "0.00"
        try:
            val = Decimal(str(suma)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            return "0.00" if val == Decimal('0.00') else str(val)
        except Exception:
            return "0.00"

    def _format_price(self, suma):
        """Jednotková cena môže mať viac desatinných miest pre presnosť a elimináciu -0.0000."""
        if not suma: return "0.0000"
        try:
            val = Decimal(str(suma)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
            return "0.0000" if val == Decimal('0.0000') else str(val)
        except Exception:
            return "0.0000"

    def _get_iso_stat(self, stat_raw):
        """🛡️ ISAS ARCHITECT: Dynamický normalizátor štátu na 2-znakový ISO kód pre Peppol cez DB maticu"""
        if stat_raw:
            raw_stat = str(stat_raw).strip()
            if len(raw_stat) == 2 and raw_stat.isalpha():
                return raw_stat.upper()

            try:
                StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
                if StatLokalizaciaModel:
                    if hasattr(StatLokalizaciaModel, 'rozpoznaj_iso'):
                        iso = StatLokalizaciaModel.rozpoznaj_iso(raw_stat)
                        if iso and iso not in ('NEZADANE', 'INYKRAJINA'):
                            return str(iso).strip().upper()

                    # Dynamické vyhľadanie v DB podľa názvu štátu
                    for col in ['nazov', 'name']:
                        if hasattr(StatLokalizaciaModel, col):
                            obj = StatLokalizaciaModel.objects.filter(**{f"{col}__iexact": raw_stat}).first()
                            if obj:
                                for code_f in ['iso_kod', 'iso', 'kod']:
                                    code_val = getattr(obj, code_f, None)
                                    if code_val and len(str(code_val).strip()) == 2 and str(code_val).strip().isalpha():
                                        return str(code_val).strip().upper()
            except Exception:
                pass

        try:
            FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            if FiremnaIdentitaModel and hasattr(FiremnaIdentitaModel, 'get_aktivna_krajina_iso'):
                iso = FiremnaIdentitaModel.get_aktivna_krajina_iso()
                if iso and len(str(iso).strip()) == 2 and str(iso).strip().isalpha():
                    return str(iso).strip().upper()
        except Exception:
            pass

        return ''

    def _get_eas_endpoint(self, ico, dic, ic_dph, stat):
        """
        Určuje oficiálny EAS kód pre Peppol EndpointID podľa normy ISO 6523.
        Dynamicky preberá peppol_eas_kod výhradne z dátového modelu StatLokalizacia bez národného hardkódu.
        """
        iso = self._get_iso_stat(stat)
        clean_ico = str(ico).replace(" ", "") if ico and str(ico).strip() not in ['-', 'None', ''] else None
        clean_dic = str(dic).replace(" ", "") if dic and str(dic).strip() not in ['-', 'None', ''] else None
        clean_ic_dph = str(ic_dph).replace(" ", "").upper() if ic_dph and str(ic_dph).strip() not in ['-', 'None', ''] else None

        # Dynamický fallback ISO kódu z prefixu IČ DPH, ak štát nebol textovo vyplnený
        if not iso and clean_ic_dph and len(clean_ic_dph) >= 2 and clean_ic_dph[:2].isalpha():
            iso = clean_ic_dph[:2]

        eas_kod = None
        stat_obj = None
        try:
            StatLokalizaciaModel = apps.get_model('ISASaplikacia', 'StatLokalizacia')
            if StatLokalizaciaModel and iso:
                if hasattr(StatLokalizaciaModel, 'get_for_iso'):
                    stat_obj = StatLokalizaciaModel.get_for_iso(iso)
                if not stat_obj:
                    for code_f in ['iso_kod', 'iso', 'kod']:
                        if hasattr(StatLokalizaciaModel, code_f):
                            stat_obj = StatLokalizaciaModel.objects.filter(**{f"{code_f}__iexact": iso}).first()
                            if stat_obj:
                                break
                if stat_obj and getattr(stat_obj, 'peppol_eas_kod', None):
                    eas_kod = str(stat_obj.peppol_eas_kod).strip()
        except Exception:
            pass

        if not eas_kod:
            raise ValueError(
                f"Peppol validačná chyba: Pre štát '{iso or 'Neznámy'}' nie je v modeli StatLokalizacia "
                f"definovaný Peppol EAS kód (peppol_eas_kod). Doplňte ho v DB konfigurácii štátu."
            )

        # Dynamická normalizácia identifikátora – orezanie ISO prefixu pre schémy vyžadujúce čisté číslo (napr. 0245, 9945)
        clean_dic_bez_iso = clean_dic[len(iso):] if (iso and clean_dic and clean_dic.startswith(iso)) else clean_dic
        clean_ic_dph_bez_iso = clean_ic_dph[len(iso):] if (iso and clean_ic_dph and clean_ic_dph.startswith(iso)) else clean_ic_dph

        if eas_kod == '0245':
            endpoint = clean_dic_bez_iso or clean_ic_dph_bez_iso or clean_ico
            if endpoint:
                return eas_kod, endpoint
            raise ValueError("Validačná chyba: Subjekt nemá vyplnené DIČ ani IČO pre Peppol sieť (schéma 0245).")

        if eas_kod == '0144':
            endpoint = clean_ico or clean_dic or clean_ic_dph
            if endpoint:
                return eas_kod, endpoint
            raise ValueError("Validačná chyba: Subjekt nemá vyplnené IČO pre Peppol sieť (schéma 0144).")

        if eas_kod == '9945':
            endpoint = clean_ic_dph_bez_iso or clean_dic_bez_iso or clean_ico
            if endpoint:
                return eas_kod, endpoint
            raise ValueError("Validačná chyba: Subjekt nemá vyplnené daňové číslo pre Peppol sieť (schéma 9945).")

        # Pre všetky ostatné medzinárodné schémy z databázy (napr. 9915, 9930, 0208, 0007...)
        endpoint = clean_ic_dph or clean_dic or clean_ico
        if endpoint:
            return eas_kod, endpoint

        raise ValueError(f"Peppol chyba: Pre subjekt zo štátu '{iso}' chýba IČ DPH, DIČ aj IČO.")

    def _get_vat_category(self, faktura, sadzba_dph, identita=None):
        """
        Určuje Peppol VAT Category Code (UNCL5305).
        S = Standard, AE = Reverse Charge, O = Outside scope (Neplatca), Z = Zero rated.
        """
        target_identita = identita or getattr(faktura, 'firma', None) or getattr(faktura, 'firemna_identita', None) or self.identita
        # Ak firma nie je platcom DPH vôbec
        if not getattr(target_identita, 'je_platca_dph', True):
            return 'O', 'Not subject to VAT'
            
        # Ak ide o prenesenie daňovej povinnosti (reverse charge)
        if getattr(faktura, 'prenos_danovej_povinnosti', False):
            return 'AE', 'Reverse charge'
            
        # Ak je sadzba 0% (napr. oslobodené)
        if sadzba_dph == Decimal('0.00'):
            return 'Z', 'Zero rated goods'
            
        # Štandardná sadzba
        return 'S', None

    def vygeneruj_peppol_xml(self, faktura):
        """Hlavná metóda pre vygenerovanie UBL 2.1 XML pre jednu faktúru."""
        # 🛡️ ISAS MULTI-TENANT FIX: Dynamické naviazanie na materskú firmu dokladu
        identita = getattr(faktura, 'firma', None) or getattr(faktura, 'firemna_identita', None) or self.identita
        if not identita:
            raise ValueError("🚨 Kritická chyba: Firemná identita pre faktúru nie je nastavená.")
        if not getattr(identita, 'ico', None) and not getattr(identita, 'ic_dph', None) and not getattr(identita, 'dic', None):
            raise ValueError("🚨 Kritická chyba: Pre eFaktúru musíte mať vo Firemnej identite vyplnené IČO, DIČ alebo IČ DPH / daňové číslo.")
        if not getattr(identita, 'iban', None):
            raise ValueError("🚨 Kritická chyba: Pre eFaktúru musíte mať vo Firemnej identite vyplnený IBAN.")

        # 1. KOREŇOVÝ ELEMENT (Vždy Invoice, aj pre dobropisy používame Invoice s kódom 381)
        root = ET.Element(f"{{{self.NAMESPACES['']}}}Invoice")
        
        # 2. POVINNÉ IDENTIFIKÁTORY NORMY (Peppol BIS Billing 3.0)
        ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}CustomizationID").text = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
        ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}ProfileID").text = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
        
        # 3. ZÁKLADNÉ ÚDAJE FAKTÚRY
        ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}ID").text = str(faktura.cislo_faktury)
        ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}IssueDate").text = self._format_date(faktura.datum_vystavenia)
        if getattr(faktura, 'datum_splatnosti', None):
            ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}DueDate").text = self._format_date(faktura.datum_splatnosti)
            
        # EN 16931 / UNCL1001: Typy dokladov
        # 380 = Faktúra, 381 = Dobropis, 388 = Daňový doklad k prijatej platbe
        typ_dokladu = getattr(faktura, 'typ_dokladu', 'FAKTURA')
        if typ_dokladu == 'ZALOHA':
            raise ValueError("Zálohová faktúra (výzva na úhradu) nie je daňovým dokladom a nesmie sa zasielať cez sieť Peppol.")
            
        typ_kodu = '380'
        if typ_dokladu == 'DOBROPIS': 
            typ_kodu = '381'
        elif typ_dokladu == 'DANOVY_DOKLAD_PLATBA':
            typ_kodu = '388'
        ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}InvoiceTypeCode").text = typ_kodu
        
        # Defenzívna referencia na pôvodnú faktúru pri Dobropise (BR-50)
        povodna_fa = getattr(faktura, 'povodna_faktura', None)
        cislo_povodnej = getattr(povodna_fa, 'cislo_faktury', None) or (str(povodna_fa).strip() if povodna_fa else None) or getattr(faktura, 'povodna_faktura_cislo', None)
        if typ_dokladu == 'DOBROPIS' and cislo_povodnej:
            billing_ref = ET.SubElement(root, f"{{{self.NAMESPACES['cac']}}}BillingReference")
            inv_doc_ref = ET.SubElement(billing_ref, f"{{{self.NAMESPACES['cac']}}}InvoiceDocumentReference")
            ET.SubElement(inv_doc_ref, f"{{{self.NAMESPACES['cbc']}}}ID").text = str(cislo_povodnej).strip()
        
        # Poznámka
        poznamka = f"{faktura.poznamka_pre_klienta or ''}".strip()
        if poznamka:
            ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}Note").text = poznamka[:1000]
            
        if faktura.datum_dodania:
            ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}TaxPointDate").text = self._format_date(faktura.datum_dodania)
            
        mena_dokladu = str(faktura.mena or get_firemna_mena()).strip()
        ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}DocumentCurrencyCode").text = mena_dokladu
        
        # BuyerReference je v Peppole POVINNÝ! Očistíme whitespace a pri absencii použijeme medzinárodný neutrálny identifikátor (BT-10).
        clean_buyer_ref = str(faktura.cislo_objednavky_klienta or '').strip()
        ET.SubElement(root, f"{{{self.NAMESPACES['cbc']}}}BuyerReference").text = clean_buyer_ref[:150] if clean_buyer_ref else "NA"

        # EN 16931: OrderReference (Referencia na objednávku)
        # Ak existuje neprázdne číslo objednávky, musí byť v tomto špecifickom tagu (BT-13).
        if clean_buyer_ref:
            order_ref = ET.SubElement(root, f"{{{self.NAMESPACES['cac']}}}OrderReference")
            ET.SubElement(order_ref, f"{{{self.NAMESPACES['cbc']}}}ID").text = clean_buyer_ref[:150]

        # 4. DODÁVATEĽ (Naša firma - s multi-tenant izoláciou)
        supplier_party = ET.SubElement(root, f"{{{self.NAMESPACES['cac']}}}AccountingSupplierParty")
        party_s = ET.SubElement(supplier_party, f"{{{self.NAMESPACES['cac']}}}Party")
        
        # Dynamické zistenie ISO kódu sídla firmy s rešpektovaním multi-tenant identity dokladu
        sidlo_firmy = getattr(identita, 'sidlo_stat', None)
        my_iso = self._get_iso_stat(sidlo_firmy)
        if not my_iso and getattr(identita, 'ic_dph', None):
            ic_raw = str(identita.ic_dph).strip().replace(" ", "").upper()
            if len(ic_raw) >= 2 and ic_raw[:2].isalpha():
                my_iso = ic_raw[:2]
        if not my_iso and getattr(identita, 'iban', None):
            iban_raw = str(identita.iban).strip().replace(" ", "").upper()
            if len(iban_raw) >= 2 and iban_raw[:2].isalpha():
                my_iso = iban_raw[:2]

        eas_scheme_s, eas_id_s = self._get_eas_endpoint(identita.ico, identita.dic, identita.ic_dph, my_iso)
        ET.SubElement(party_s, f"{{{self.NAMESPACES['cbc']}}}EndpointID", {'schemeID': eas_scheme_s}).text = eas_id_s
        
        party_name_s = ET.SubElement(party_s, f"{{{self.NAMESPACES['cac']}}}PartyName")
        ET.SubElement(party_name_s, f"{{{self.NAMESPACES['cbc']}}}Name").text = str(identita.nazov_spolocnosti)[:255]
        
        postal_s = ET.SubElement(party_s, f"{{{self.NAMESPACES['cac']}}}PostalAddress")
        ulica_s = f"{identita.sidlo_ulica or ''} {identita.sidlo_cislo or ''}".strip()
        if ulica_s:
            ET.SubElement(postal_s, f"{{{self.NAMESPACES['cbc']}}}StreetName").text = ulica_s[:255]
        if identita.sidlo_mesto:
            ET.SubElement(postal_s, f"{{{self.NAMESPACES['cbc']}}}CityName").text = str(identita.sidlo_mesto)[:45]
        if identita.sidlo_psc:
            ET.SubElement(postal_s, f"{{{self.NAMESPACES['cbc']}}}PostalZone").text = str(identita.sidlo_psc).replace(" ", "")[:15]
        country_s = ET.SubElement(postal_s, f"{{{self.NAMESPACES['cac']}}}Country")
        ET.SubElement(country_s, f"{{{self.NAMESPACES['cbc']}}}IdentificationCode").text = my_iso
        
        if identita.ic_dph and str(identita.ic_dph).strip() not in ['-', 'None', '']:
            tax_scheme_s = ET.SubElement(party_s, f"{{{self.NAMESPACES['cac']}}}PartyTaxScheme")
            # Peppol BR-CO-09 vyžaduje platný ISO 3166-1 alpha-2 prefix (EN 16931)
            ic_dph_raw = str(identita.ic_dph).replace(" ", "").upper()
            iso_prefix = my_iso if (len(str(my_iso)) == 2 and str(my_iso).isalpha()) else self._get_iso_stat(identita.sidlo_stat)
            
            # Ak už začína ľubovoľným platným 2-písmenovým kódom štátu (napr. SK, CZ, AT, DE), ponecháme ho
            if len(ic_dph_raw) >= 2 and ic_dph_raw[:2].isalpha():
                ic_dph_clean_s = ic_dph_raw
            else:
                ic_dph_clean_s = f"{iso_prefix}{ic_dph_raw}"
                
            ET.SubElement(tax_scheme_s, f"{{{self.NAMESPACES['cbc']}}}CompanyID").text = ic_dph_clean_s
            ts_s = ET.SubElement(tax_scheme_s, f"{{{self.NAMESPACES['cac']}}}TaxScheme")
            ET.SubElement(ts_s, f"{{{self.NAMESPACES['cbc']}}}ID").text = "VAT"
            
        legal_s = ET.SubElement(party_s, f"{{{self.NAMESPACES['cac']}}}PartyLegalEntity")
        ET.SubElement(legal_s, f"{{{self.NAMESPACES['cbc']}}}RegistrationName").text = str(identita.nazov_spolocnosti)[:255]
        # Bezpečný identifikátor dodávateľa podľa EN 16931 BT-30 (bez atribútu schemeID, ktorý vyžaduje len ICD)
        company_id_s = str(identita.ico or identita.ic_dph or identita.dic or '').replace(" ", "")
        if company_id_s:
            ET.SubElement(legal_s, f"{{{self.NAMESPACES['cbc']}}}CompanyID").text = company_id_s
        
        # EN 16931: Zápis v registri (BT-33)
        if getattr(identita, 'zapis_v_registri', None):
            ET.SubElement(legal_s, f"{{{self.NAMESPACES['cbc']}}}CompanyLegalForm").text = str(identita.zapis_v_registri)[:255]

        # 5. ODBERATEĽ (Zákazník)
        customer_party = ET.SubElement(root, f"{{{self.NAMESPACES['cac']}}}AccountingCustomerParty")
        party_c = ET.SubElement(customer_party, f"{{{self.NAMESPACES['cac']}}}Party")
        
        # Pridaný parameter DIČ pre správnu identifikáciu (0245)
        eas_scheme_c, eas_id_c = self._get_eas_endpoint(faktura.odberatel_ico, faktura.odberatel_dic, faktura.odberatel_ic_dph, faktura.odberatel_stat)
        ET.SubElement(party_c, f"{{{self.NAMESPACES['cbc']}}}EndpointID", {'schemeID': eas_scheme_c}).text = eas_id_c
        
        party_name_c = ET.SubElement(party_c, f"{{{self.NAMESPACES['cac']}}}PartyName")
        ET.SubElement(party_name_c, f"{{{self.NAMESPACES['cbc']}}}Name").text = str(faktura.odberatel_nazov)[:255]
        
        postal_c = ET.SubElement(party_c, f"{{{self.NAMESPACES['cac']}}}PostalAddress")
        if faktura.odberatel_ulica: ET.SubElement(postal_c, f"{{{self.NAMESPACES['cbc']}}}StreetName").text = str(faktura.odberatel_ulica)[:255]
        if faktura.odberatel_mesto: ET.SubElement(postal_c, f"{{{self.NAMESPACES['cbc']}}}CityName").text = str(faktura.odberatel_mesto)[:45]
        if faktura.odberatel_psc: ET.SubElement(postal_c, f"{{{self.NAMESPACES['cbc']}}}PostalZone").text = str(faktura.odberatel_psc).replace(" ", "")[:15]
        country_c = ET.SubElement(postal_c, f"{{{self.NAMESPACES['cac']}}}Country")
        # Dynamické zistenie reálneho ISO kódu štátu odberateľa s fallbackom na IČ DPH a sídlo firmy
        kod_statu_c = self._get_iso_stat(faktura.odberatel_stat)
        if not kod_statu_c and getattr(faktura, 'odberatel_ic_dph', None):
            c_ic = str(faktura.odberatel_ic_dph).strip().replace(" ", "").upper()
            if len(c_ic) >= 2 and c_ic[:2].isalpha():
                kod_statu_c = c_ic[:2]
        if not kod_statu_c:
            kod_statu_c = my_iso
        ET.SubElement(country_c, f"{{{self.NAMESPACES['cbc']}}}IdentificationCode").text = kod_statu_c
        
        if faktura.odberatel_ic_dph and str(faktura.odberatel_ic_dph).strip() not in ['-', 'None', '']:
            tax_scheme_c = ET.SubElement(party_c, f"{{{self.NAMESPACES['cac']}}}PartyTaxScheme")
            ic_dph_clean_c = str(faktura.odberatel_ic_dph).replace(" ", "").upper()
            # Ak IČ DPH neobsahuje kód štátu na začiatku, doplníme ISO štátu odberateľa
            if len(ic_dph_clean_c) >= 2 and not ic_dph_clean_c[:2].isalpha():
                ic_dph_clean_c = f"{kod_statu_c}{ic_dph_clean_c}"
            ET.SubElement(tax_scheme_c, f"{{{self.NAMESPACES['cbc']}}}CompanyID").text = ic_dph_clean_c
            ts_c = ET.SubElement(tax_scheme_c, f"{{{self.NAMESPACES['cac']}}}TaxScheme")
            ET.SubElement(ts_c, f"{{{self.NAMESPACES['cbc']}}}ID").text = "VAT"
            
        legal_c = ET.SubElement(party_c, f"{{{self.NAMESPACES['cac']}}}PartyLegalEntity")
        ET.SubElement(legal_c, f"{{{self.NAMESPACES['cbc']}}}RegistrationName").text = str(faktura.odberatel_nazov)[:255]
        # Identifikátor odberateľa podľa EN 16931 BT-47 (bez atribútu schemeID, ktorý vyžaduje len ICD)
        cust_company_id = str(faktura.odberatel_ico or faktura.odberatel_ic_dph or faktura.odberatel_dic or '').replace(" ", "")
        if cust_company_id and cust_company_id not in ['-', 'None']:
            ET.SubElement(legal_c, f"{{{self.NAMESPACES['cbc']}}}CompanyID").text = cust_company_id

        # 6. PLATOBNÉ ÚDAJE
        payment_means = ET.SubElement(root, f"{{{self.NAMESPACES['cac']}}}PaymentMeans")
        payment_code = self.PAYMENT_MAPPING.get(faktura.sposob_uhrady, '30')
        ET.SubElement(payment_means, f"{{{self.NAMESPACES['cbc']}}}PaymentMeansCode").text = payment_code
        
        if faktura.variabilny_symbol:
            ET.SubElement(payment_means, f"{{{self.NAMESPACES['cbc']}}}PaymentID").text = str(faktura.variabilny_symbol)
            
        iban_uhrady = str(getattr(faktura, 'iban', None) or getattr(identita, 'iban', '') or '').replace(" ", "")
        if iban_uhrady:
            payee_acc = ET.SubElement(payment_means, f"{{{self.NAMESPACES['cac']}}}PayeeFinancialAccount")
            ET.SubElement(payee_acc, f"{{{self.NAMESPACES['cbc']}}}ID").text = iban_uhrady
            swift_uhrady = str(getattr(faktura, 'swift', None) or getattr(identita, 'swift', '') or '').replace(" ", "")
            if swift_uhrady:
                branch = ET.SubElement(payee_acc, f"{{{self.NAMESPACES['cac']}}}FinancialInstitutionBranch")
                ET.SubElement(branch, f"{{{self.NAMESPACES['cbc']}}}ID").text = swift_uhrady

        # 7. POLOŽKY FAKTÚRY A ZOSKUPENIE DPH (Kritická matematika)
        tax_groups = {}
        line_extension_total = Decimal('0.00')
        
        # Najprv si pripravíme XML elementy pre položky, ale zatiaľ ich nepridáme do rootu
        # (Podľa XSD musia ísť InvoiceLine až na úplný koniec za TaxTotal)
        invoice_lines_elements = []
        
        # (BR-CO-13): Získame globálnu zľavu, aby sme ju rozpustili do riadkov
        globalna_zlava = getattr(faktura, 'globalna_zlava_percento', None) or Decimal('0.00')
        koeficient_global = Decimal('1.00') - (Decimal(str(globalna_zlava)) / Decimal('100.00'))

        for idx, polozka in enumerate(faktura.polozky.all(), start=1):
            # Preskočíme manuálne zľavové riadky (Peppol vyžaduje kladné sumy, zľavy riešime koeficientom)
            if getattr(polozka, 'typ_polozky', None) == 'ZLA':
                continue
                
            line = ET.Element(f"{{{self.NAMESPACES['cac']}}}InvoiceLine")
            ET.SubElement(line, f"{{{self.NAMESPACES['cbc']}}}ID").text = str(idx)
            
            # Peppol CreditNote (381) MUSÍ mať absolútne (kladné) hodnoty!
            mnozstvo = abs(Decimal(str(polozka.mnozstvo or '0.00')))
            
            # Bezpečné očistenie mernej jednotky pred mapovaním a dynamické overenie odvetvovej jednotky výjazdu/zásahu
            cista_mj = str(polozka.merna_jednotka or '').strip().lower().replace('.', '')
            vyjazd_term = get_industry_term('vyjazd_jednotka', '').strip().lower().replace('.', '')
            if vyjazd_term and cista_mj == vyjazd_term:
                unit_code = 'E54'
            else:
                unit_code = self.UNIT_MAPPING.get(cista_mj, 'C62')
            
            ET.SubElement(line, f"{{{self.NAMESPACES['cbc']}}}InvoicedQuantity", {'unitCode': unit_code}).text = self._format_amount(mnozstvo)
            
            # Aplikujeme globálnu zľavu a zabezpečíme absolútnu hodnotu
            cena_polozky_bez_dph = Decimal(str(polozka.cena_po_zlave_bez_dph or '0.00'))
            line_amount = abs((cena_polozky_bez_dph * koeficient_global).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            line_extension_total += line_amount
            ET.SubElement(line, f"{{{self.NAMESPACES['cbc']}}}LineExtensionAmount", {'currencyID': mena_dokladu}).text = self._format_amount(line_amount)
            
            # DPH Kategória pre položku
            sadzba_dph = Decimal(str(polozka.sadzba_dph or '0.00')) if not getattr(faktura, 'prenos_danovej_povinnosti', False) else Decimal('0.00')
            vat_cat_code, vat_reason = self._get_vat_category(faktura, sadzba_dph, identita=identita)
            
            # Zoskupovanie pre TaxTotal
            group_key = (vat_cat_code, sadzba_dph, vat_reason)
            if group_key not in tax_groups:
                tax_groups[group_key] = {'taxable_amount': Decimal('0.00'), 'tax_amount': Decimal('0.00')}
            
            tax_groups[group_key]['taxable_amount'] += line_amount
            # V Peppole sa DPH nesmie sčítavať po riadkoch! 
            # Výpočet sa musí vykonať až na konci z celkového základu dane pre danú kategóriu.

            item = ET.SubElement(line, f"{{{self.NAMESPACES['cac']}}}Item")
            
            # Do štátneho XML musí ísť presne ten istý názov ako na PDF (s defenzívnym fallbackom proti textu 'None')
            zobrazeny_nazov = getattr(polozka, 'obchodny_nazov', None) or getattr(polozka, 'nazov', None) or ''
            nazov_final = str(zobrazeny_nazov).strip()
            ET.SubElement(item, f"{{{self.NAMESPACES['cbc']}}}Name").text = (nazov_final if nazov_final else "Položka")[:150]
            
            tax_cat = ET.SubElement(item, f"{{{self.NAMESPACES['cac']}}}ClassifiedTaxCategory")
            ET.SubElement(tax_cat, f"{{{self.NAMESPACES['cbc']}}}ID").text = vat_cat_code
            # Peppol BR-O-05: Pri kategórii 'O' (Not subject to VAT) nesmie byť element Percent prítomný
            if vat_cat_code != 'O':
                ET.SubElement(tax_cat, f"{{{self.NAMESPACES['cbc']}}}Percent").text = self._format_amount(sadzba_dph)
            ts_item = ET.SubElement(tax_cat, f"{{{self.NAMESPACES['cac']}}}TaxScheme")
            ET.SubElement(ts_item, f"{{{self.NAMESPACES['cbc']}}}ID").text = "VAT"
            
            price = ET.SubElement(line, f"{{{self.NAMESPACES['cac']}}}Price")
            # (BR-28): PriceAmount musí byť cena PO ZĽAVE (Net Price) a musí obsahovať aj rozpustenú globálnu zľavu!
            # Inak by zlyhala rovnica: InvoicedQuantity * PriceAmount == LineExtensionAmount
            zlava_polozky = Decimal(str(getattr(polozka, 'zlava_percento', None) or '0.00'))
            zlava_koeficient = Decimal('1.00') - (zlava_polozky / Decimal('100.00'))
            jednotkova_cena = abs(Decimal(str(polozka.jednotkova_cena_bez_dph or '0.00')))
            if jednotkova_cena > Decimal('0.00'):
                cena_po_zlave_za_kus = (jednotkova_cena * zlava_koeficient) * koeficient_global
            elif mnozstvo > Decimal('0.00'):
                cena_po_zlave_za_kus = (line_amount / mnozstvo).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
            else:
                cena_po_zlave_za_kus = Decimal('0.0000')
            ET.SubElement(price, f"{{{self.NAMESPACES['cbc']}}}PriceAmount", {'currencyID': mena_dokladu}).text = self._format_price(cena_po_zlave_za_kus)
            
            invoice_lines_elements.append(line)

        # 8. TAX TOTAL (Sumár DPH)
        # (BR-CO-14): Najprv vypočítame presnú DPH z kategórií, aby sme predišli 1-centovej odchýlke voči DB
        xml_total_tax = Decimal('0.00')
        tax_subtotals_elements = []
        
        for (cat_code, percent, reason), totals in tax_groups.items():
            spravna_dan_kategorie = (totals['taxable_amount'] * (percent / Decimal('100.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            xml_total_tax += spravna_dan_kategorie
            
            subtotal = ET.Element(f"{{{self.NAMESPACES['cac']}}}TaxSubtotal")
            ET.SubElement(subtotal, f"{{{self.NAMESPACES['cbc']}}}TaxableAmount", {'currencyID': mena_dokladu}).text = self._format_amount(totals['taxable_amount'])
            ET.SubElement(subtotal, f"{{{self.NAMESPACES['cbc']}}}TaxAmount", {'currencyID': mena_dokladu}).text = self._format_amount(spravna_dan_kategorie)
            
            cat = ET.SubElement(subtotal, f"{{{self.NAMESPACES['cac']}}}TaxCategory")
            ET.SubElement(cat, f"{{{self.NAMESPACES['cbc']}}}ID").text = cat_code
            # Peppol BR-O-08: Pri kategórii 'O' (Not subject to VAT) nesmie byť element Percent prítomný
            if cat_code != 'O':
                ET.SubElement(cat, f"{{{self.NAMESPACES['cbc']}}}Percent").text = self._format_amount(percent)
            if reason:
                ET.SubElement(cat, f"{{{self.NAMESPACES['cbc']}}}TaxExemptionReason").text = reason
            ts_sub = ET.SubElement(cat, f"{{{self.NAMESPACES['cac']}}}TaxScheme")
            ET.SubElement(ts_sub, f"{{{self.NAMESPACES['cbc']}}}ID").text = "VAT"
            
            tax_subtotals_elements.append(subtotal)

        tax_total = ET.SubElement(root, f"{{{self.NAMESPACES['cac']}}}TaxTotal")
        # Poistka pre absolútnu hodnotu celkovej DPH (CreditNote nesmie mať mínus)
        ET.SubElement(tax_total, f"{{{self.NAMESPACES['cbc']}}}TaxAmount", {'currencyID': mena_dokladu}).text = self._format_amount(abs(xml_total_tax))
        for el in tax_subtotals_elements:
            tax_total.append(el)

        # 9. LEGAL MONETARY TOTAL (Finančný sumár)
        # (BR-CO-15): Celková suma s DPH musí byť presným súčtom základu a vypočítanej DPH v XML
        # Pre Dobropis (CreditNote) musia byť všetky sumy v XML absolútne (kladné) hodnoty!
        abs_celkom_bez_dph = line_extension_total if invoice_lines_elements else abs(Decimal(str(faktura.celkom_bez_dph or '0.00')))
        abs_xml_total_tax = abs(xml_total_tax)
        xml_total_with_tax = abs_celkom_bez_dph + abs_xml_total_tax
        
        legal_total = ET.SubElement(root, f"{{{self.NAMESPACES['cac']}}}LegalMonetaryTotal")
        ET.SubElement(legal_total, f"{{{self.NAMESPACES['cbc']}}}LineExtensionAmount", {'currencyID': mena_dokladu}).text = self._format_amount(line_extension_total)
        ET.SubElement(legal_total, f"{{{self.NAMESPACES['cbc']}}}TaxExclusiveAmount", {'currencyID': mena_dokladu}).text = self._format_amount(abs_celkom_bez_dph)
        ET.SubElement(legal_total, f"{{{self.NAMESPACES['cbc']}}}TaxInclusiveAmount", {'currencyID': mena_dokladu}).text = self._format_amount(xml_total_with_tax)
        ET.SubElement(legal_total, f"{{{self.NAMESPACES['cbc']}}}PayableAmount", {'currencyID': mena_dokladu}).text = self._format_amount(xml_total_with_tax)

        # 10. PRIDANIE POLOŽIEK NA KONIEC DOKUMENTU
        for line_el in invoice_lines_elements:
            root.append(line_el)

        # Vrátime čistý XML string s deklaráciou
        return ET.tostring(root, encoding='UTF-8', xml_declaration=True)