import hashlib
from decimal import Decimal
from datetime import timedelta
from typing import Optional
from django.utils import timezone
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.core.files.base import ContentFile
from django.apps import apps
from Evidencia.utils_helper import get_firemna_mena
from Evidencia.utils_compliance import to_date
import logging

logger = logging.getLogger(__name__)

class PeppolInboxEngine:
    """
    ISAS CORE: Deduplikačný a zlučovací engine pre prichádzajúce eFaktúry.
    Zabezpečuje Single Source of Truth a WORM archiváciu.
    """

    @staticmethod
    def spracuj_prijate_xml(xml_content: bytes, peppol_doc_id: str, parsed_data: dict):
        """
        Hlavná metóda volaná z Celery tasku po stiahnutí XML.
        parsed_data obsahuje už vytiahnuté tvrdé dáta z XML (IČO, VS, Suma, IBAN, atď.)
        """
        PrichodziaFaktura = apps.get_model('Evidencia', 'PrichodziaFaktura')
        Dodavatel = apps.get_model('Evidencia', 'Dodavatel')

        if not xml_content or not isinstance(parsed_data, dict):
            logger.error(f"❌ Peppol Inbox: Neplatný alebo prázdny obsah XML/parsed_data pre doklad {peppol_doc_id}")
            return None

        if isinstance(xml_content, str):
            xml_content = xml_content.encode('utf-8')

        peppol_doc_id = str(peppol_doc_id).strip()[:255] if peppol_doc_id else ""
        ico_raw = parsed_data.get('ico')
        ico = str(ico_raw).strip()[:20] if ico_raw and str(ico_raw).strip() else None
        ic_dph_raw = parsed_data.get('ic_dph')
        ic_dph = str(ic_dph_raw).strip()[:30] if ic_dph_raw and str(ic_dph_raw).strip() else None
        dic_raw = parsed_data.get('dic')
        dic = str(dic_raw).strip()[:30] if dic_raw and str(dic_raw).strip() else None
        vs_raw = parsed_data.get('variabilny_symbol') or parsed_data.get('cislo_faktury')
        vs = str(vs_raw).strip()[:20] if vs_raw and str(vs_raw).strip() else None
        suma = parsed_data.get('suma_na_uhradu')
        iban_raw = parsed_data.get('iban')
        iban = str(iban_raw).replace(" ", "").strip()[:34] if iban_raw and str(iban_raw).strip() else None
        datum_splatnosti = to_date(parsed_data.get('datum_splatnosti'))
        datum_vystavenia = to_date(parsed_data.get('datum_vystavenia'))
        datum_dodania = to_date(parsed_data.get('datum_dodania'))
        
        if not vs or not (ico or ic_dph or parsed_data.get('nazov_dodavatela')):
            logger.error(f"❌ Peppol Inbox: Chýba identifikácia dodávateľa alebo VS v dokumente {peppol_doc_id}")
            return None

        # 1. Vytvorenie SHA-256 Pečate
        sha256_hash = hashlib.sha256(xml_content).hexdigest()

        with transaction.atomic():
            # 0. KROK DEDUPLIKÁCIE: Absolútna ochrana pred duplicitným spracovaním totožného XML (WORM)
            existujuca_podla_hashu = PrichodziaFaktura.objects.select_for_update().filter(
                xml_sha256_hash=sha256_hash
            ).first()
            if not existujuca_podla_hashu and peppol_doc_id:
                existujuca_podla_hashu = PrichodziaFaktura.objects.select_for_update().filter(
                    peppol_document_id=peppol_doc_id
                ).first()

            if existujuca_podla_hashu:
                logger.info("⏭️ Preskakujem: Totožné XML (SHA-256 pečať / Peppol ID) už existuje v archíve.")
                return existujuca_podla_hashu

            # 2. Nájdenie alebo bezpečné vytvorenie dodávateľa (Defenzívna ochrana pred IntegrityError pri súbehu)
            dodavatel = None
            if ico:
                dodavatel = Dodavatel.objects.filter(ico=ico).first()
            if not dodavatel and ic_dph and hasattr(Dodavatel, 'ic_dph'):
                dodavatel = Dodavatel.objects.filter(ic_dph=ic_dph).first()
            if not dodavatel and parsed_data.get('nazov_dodavatela'):
                hladany_nazov = str(parsed_data.get('nazov_dodavatela')).strip()[:100]
                if hladany_nazov and hladany_nazov.lower() != 'neznámy':
                    dodavatel = Dodavatel.objects.filter(Nazov_spolocnosti__iexact=hladany_nazov).first()

            if not dodavatel:
                nazov_dodavatela = str(parsed_data.get('nazov_dodavatela') or '').strip()
                if not nazov_dodavatela:
                    id_label = ico or ic_dph
                    nazov_dodavatela = f'Neznámy dodávateľ ({id_label})' if id_label else 'Neznámy dodávateľ'
                logger.info(f"🏢 Peppol Inbox: Vytváram nového dodávateľa: {nazov_dodavatela[:100]}")
                dodavatel_kwargs = {
                    'Nazov_spolocnosti': nazov_dodavatela[:100],
                    'ico': ico,
                    'iban': iban,
                    'ostatne': "🤖 Automaticky vytvorené cez Peppol Inbox"
                }
                if hasattr(Dodavatel, 'ic_dph') and ic_dph:
                    dodavatel_kwargs['ic_dph'] = ic_dph
                if hasattr(Dodavatel, 'dic') and dic:
                    dodavatel_kwargs['dic'] = dic
                if hasattr(Dodavatel, 'adresa') and parsed_data.get('sidlo_dodavatela'):
                    dodavatel_kwargs['adresa'] = str(parsed_data.get('sidlo_dodavatela'))[:255]
                try:
                    with transaction.atomic():
                        dodavatel = Dodavatel.objects.create(**dodavatel_kwargs)
                except IntegrityError:
                    if ico:
                        dodavatel = Dodavatel.objects.filter(ico=ico).first()
                    if not dodavatel and ic_dph and hasattr(Dodavatel, 'ic_dph'):
                        dodavatel = Dodavatel.objects.filter(ic_dph=ic_dph).first()
                    if not dodavatel:
                        dodavatel = Dodavatel.objects.filter(Nazov_spolocnosti__iexact=nazov_dodavatela[:100]).first()

            if not dodavatel:
                logger.error(f"❌ Peppol Inbox: Nepodarilo sa asociovať ani vytvoriť dodávateľa pre doklad {peppol_doc_id}")
                return None

            # 3. DEDUPLIKÁCIA (Striktný Match pre párovanie s vopred zaevidovanou faktúrou)
            hranica_casu = (timezone.now().date() if hasattr(timezone.now(), 'date') else timezone.now()) - timedelta(days=60)
            
            # 🛡️ ISAS ARCHITECT FIX: Bezpečná konverzia sumy na Decimal (ochrana pred pádmi pri čiarkach a medzerách)
            try:
                clean_suma = str(suma).replace(' ', '').replace('\xa0', '').replace(',', '.') if suma is not None else '0.00'
                bezpecna_suma = Decimal(clean_suma).quantize(Decimal('0.01'))
            except Exception:
                bezpecna_suma = Decimal('0.00')

            # 🛡️ ISAS ARCHITECT FIX: Absolútne striktná deduplikácia. 
            # Ochrana voči dokladom s nevyplneným datum_prijatia (NULL v DB)
            cas_filter = Q(datum_prijatia__gte=hranica_casu) | Q(datum_prijatia__isnull=True) if hasattr(PrichodziaFaktura, 'datum_prijatia') else Q()
            existujuca_faktura = PrichodziaFaktura.objects.select_for_update().filter(
                cas_filter,
                dodavatel=dodavatel,
                variabilny_symbol=vs,
                suma_na_uhradu=bezpecna_suma
            ).first()

            if existujuca_faktura:
                logger.info(f"🔄 Peppol Inbox: Nájdená zhoda pre VS {vs}. Spúšťam Merge Engine.")
                
                # Ak už faktúra má XML, ide o duplicitné doručenie zo siete Peppol
                if existujuca_faktura.xml_sha256_hash == sha256_hash:
                    logger.info("⏭️ Preskakujem: Totožné XML už je v systéme.")
                    return existujuca_faktura

                # 🛡️ ISAS ARCHITECT FIX: Ochrana WORM – ak doklad už XML má, neprepisujeme ho, aby sme neporušili integritu
                if existujuca_faktura.xml_subor or existujuca_faktura.xml_sha256_hash:
                    logger.warning(f"🔒 Peppol Inbox: Doklad {existujuca_faktura.id} už má archivovaný XML originál. Ponechávam pôvodný súbor.")
                    return existujuca_faktura

                # Uloženie XML do archívu (faktúra bola doposiaľ evidovaná bez XML originálu)
                file_name = f"peppol_in_{sha256_hash[:32]}.xml"
                existujuca_faktura.xml_subor.save(file_name, ContentFile(xml_content), save=False)
                existujuca_faktura.xml_sha256_hash = sha256_hash
                existujuca_faktura.peppol_document_id = peppol_doc_id
                
                # Ak faktúra ešte nie je zaplatená/exportovaná, aktualizujeme dáta podľa originálu
                if existujuca_faktura.stav_uhrady != 'ZAPLATENA' and getattr(existujuca_faktura, 'erp_export_status', None) not in ['EXPORTED', 'PROCESSING']:
                    existujuca_faktura.suma_na_uhradu = bezpecna_suma
                    if hasattr(existujuca_faktura, 'mena'):
                        existujuca_faktura.mena = (str(parsed_data.get('mena')).strip()[:3].upper() if parsed_data.get('mena') else None) or getattr(existujuca_faktura, 'mena', None) or get_firemna_mena()
                    existujuca_faktura.iban = iban or existujuca_faktura.iban
                    existujuca_faktura.datum_splatnosti = datum_splatnosti or existujuca_faktura.datum_splatnosti
                    if datum_vystavenia and hasattr(existujuca_faktura, 'datum_vystavenia') and not existujuca_faktura.datum_vystavenia:
                        existujuca_faktura.datum_vystavenia = datum_vystavenia
                    if datum_dodania and hasattr(existujuca_faktura, 'datum_dodania') and not existujuca_faktura.datum_dodania:
                        existujuca_faktura.datum_dodania = datum_dodania
                    existujuca_faktura.zdroj = 'PEPPOL'
                    existujuca_faktura.poznamka = f"{existujuca_faktura.poznamka or ''}\n\n✅ [PEPPOL MERGE]: Dáta boli aktualizované podľa legislatívneho XML originálu."
                else:
                    existujuca_faktura.poznamka = f"{existujuca_faktura.poznamka or ''}\n\n🔒 [PEPPOL ARCHÍV]: Doručený XML originál. Dáta neboli prepísané, doklad je už uzamknutý."

                existujuca_faktura.save()
                return existujuca_faktura

            else:
                # 4. VYTVORENIE NOVEJ FAKTÚRY (Ak prišlo XML ako prvé)
                logger.info(f"✨ Peppol Inbox: Vytváram novú eFaktúru pre VS {vs}.")
                
                nova_faktura = PrichodziaFaktura(
                    zdroj='PEPPOL',
                    dodavatel=dodavatel,
                    variabilny_symbol=vs,
                    suma_na_uhradu=bezpecna_suma,
                    mena=(str(parsed_data.get('mena')).strip()[:3].upper() if parsed_data.get('mena') else None) or get_firemna_mena(),
                    iban=iban,
                    datum_splatnosti=datum_splatnosti,
                    peppol_document_id=peppol_doc_id,
                    xml_sha256_hash=sha256_hash,
                    spracovane=False, # Bude čakať na AI sémantiku položiek
                    poznamka=f"📥 Prijaté cez sieť Peppol (eFaktúra).\n📄 Číslo faktúry: {parsed_data.get('cislo_faktury', 'Nezistené')}"
                )
                if datum_vystavenia and hasattr(nova_faktura, 'datum_vystavenia'):
                    nova_faktura.datum_vystavenia = datum_vystavenia
                if datum_dodania and hasattr(nova_faktura, 'datum_dodania'):
                    nova_faktura.datum_dodania = datum_dodania
                if hasattr(nova_faktura, 'datum_prijatia') and not getattr(nova_faktura, 'datum_prijatia', None):
                    nova_faktura.datum_prijatia = timezone.now().date()
                if hasattr(nova_faktura, 'cislo_dokladu') and parsed_data.get('cislo_faktury'):
                    nova_faktura.cislo_dokladu = str(parsed_data.get('cislo_faktury')).strip()[:50]
                if hasattr(nova_faktura, 'firma') and not getattr(nova_faktura, 'firma', None):
                    FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                    if FiremnaIdentitaModel:
                        nova_faktura.firma = FiremnaIdentitaModel.objects.first()
                elif hasattr(nova_faktura, 'firemna_identita') and not getattr(nova_faktura, 'firemna_identita', None):
                    FiremnaIdentitaModel = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
                    if FiremnaIdentitaModel:
                        nova_faktura.firemna_identita = FiremnaIdentitaModel.objects.first()
                
                file_name = f"peppol_in_{sha256_hash[:32]}.xml"
                nova_faktura.xml_subor.save(file_name, ContentFile(xml_content), save=False)
                nova_faktura.save()
                
                return nova_faktura



import xml.etree.ElementTree as ET

class PeppolXMLParser:
    """
    ISAS CORE: Bezpečný parser pre Peppol BIS Billing 3.0 (UBL 2.1).
    Extrahuje tvrdé dáta (IČO, VS, Sumy, Položky) bez použitia AI.
    """
    NAMESPACES = {
        'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
        'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
        'ns': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
        'cn': 'urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2'
    }

    @classmethod
    def parse(cls, xml_content: bytes) -> dict:
        if not xml_content:
            return {}
        try:
            if isinstance(xml_content, str):
                xml_content = xml_content.encode('utf-8')

            # Odstránenie BOM (Byte Order Mark), ak existuje, inak ET.fromstring padne
            if xml_content.startswith(b'\xef\xbb\xbf'):
                xml_content = xml_content[3:]
                
            root = ET.fromstring(xml_content)
            
            # Podpora pre UBL 2.1 Faktúry (<Invoice>) AJ Dobropisy (<CreditNote>)
            is_doc = any(root.tag.endswith(t) for t in ['Invoice', 'CreditNote'])
            invoice_node = root if is_doc else (root.find('.//ns:Invoice', cls.NAMESPACES) or root.find('.//cn:CreditNote', cls.NAMESPACES))
            
            if invoice_node is None:
                logger.error("❌ PeppolXMLParser: Dokument neobsahuje tag <Invoice> ani <CreditNote>. Nie je to UBL 2.1 doklad.")
                return {}
            
            def get_text(element, xpath, default=None):
                if element is None:
                    return default
                node = element.find(xpath, cls.NAMESPACES)
                if node is not None and node.text:
                    val = node.text.strip()
                    return val if val else default
                return default

            # 1. Základné údaje (Hľadáme v invoice_node, nie v root!)
            cislo_faktury = get_text(invoice_node, 'cbc:ID')
            datum_vystavenia = get_text(invoice_node, 'cbc:IssueDate')
            datum_splatnosti = get_text(invoice_node, 'cbc:DueDate')
            mena = get_text(invoice_node, 'cbc:DocumentCurrencyCode')
            
            # 2. Dodávateľ (IČO, DIČ, IČ DPH, Názov, Sídlo)
            supplier_node = invoice_node.find('cac:AccountingSupplierParty/cac:Party', cls.NAMESPACES)
            nazov_dodavatela = (get_text(supplier_node, 'cac:PartyLegalEntity/cbc:RegistrationName') or get_text(supplier_node, 'cac:PartyName/cbc:Name') or "Neznámy") if supplier_node else "Neznámy"
            
            endpoint_elem = supplier_node.find('cbc:EndpointID', cls.NAMESPACES) if supplier_node is not None else None
            endpoint_id = endpoint_elem.text.strip() if endpoint_elem is not None and endpoint_elem.text else None
            endpoint_scheme = endpoint_elem.attrib.get('schemeID', '').strip() if endpoint_elem is not None else ''
            
            company_id = get_text(supplier_node, 'cac:PartyLegalEntity/cbc:CompanyID') if supplier_node else None
            tax_company_id = get_text(supplier_node, 'cac:PartyTaxScheme/cbc:CompanyID') if supplier_node else None
            
            ic_dph = None
            dic = None
            ico = None
            
            # Bezpečné rozlíšenie IČ DPH a DIČ
            if tax_company_id:
                clean_tax = tax_company_id.strip()
                if clean_tax[:2].isalpha():
                    ic_dph = clean_tax
                else:
                    dic = clean_tax

            # Rozlíšenie registračného čísla (IČO) od daňového identifikátora
            if company_id:
                clean_comp = company_id.strip()
                if clean_comp[:2].isalpha() and not ic_dph:
                    ic_dph = clean_comp
                else:
                    ico = clean_comp

            # Fallback z EndpointID podľa registračnej schémy Peppol
            if not ico and endpoint_id:
                raw_endpoint: Optional[str] = endpoint_id.split(':', 1)[1] if ':' in endpoint_id else endpoint_id
                raw_endpoint = raw_endpoint.strip() if raw_endpoint else None
                if endpoint_scheme in ['0245', '0144']:
                    ico = raw_endpoint
                elif raw_endpoint and raw_endpoint[:2].isalpha() and not ic_dph:
                    ic_dph = raw_endpoint
                elif not ico and not ic_dph:
                    ico = raw_endpoint

            # Extrakcia sídla dodávateľa
            postal_node = supplier_node.find('cac:PostalAddress', cls.NAMESPACES) if supplier_node else None
            ulica = get_text(postal_node, 'cbc:StreetName') if postal_node is not None else None
            mesto = get_text(postal_node, 'cbc:CityName') if postal_node is not None else None
            psc = get_text(postal_node, 'cbc:PostalZone') if postal_node is not None else None
            stat = get_text(postal_node, 'cac:Country/cbc:IdentificationCode') if postal_node is not None else None
            adresa_diely = [p for p in [ulica, psc, mesto, stat] if p]
            sidlo_dodavatela = ", ".join(adresa_diely) if adresa_diely else None

            # Dátum dodania (ActualDeliveryDate alebo z InvoicePeriod)
            datum_dodania = (
                get_text(invoice_node, 'cac:Delivery/cbc:ActualDeliveryDate') or
                get_text(invoice_node, 'cac:InvoicePeriod/cbc:EndDate') or
                get_text(invoice_node, 'cac:InvoicePeriod/cbc:StartDate')
            )

            # 3. Platobné údaje (IBAN a VS)
            payment_node = invoice_node.find('cac:PaymentMeans', cls.NAMESPACES)
            if not datum_splatnosti and payment_node is not None:
                datum_splatnosti = get_text(payment_node, 'cbc:PaymentDueDate')
            # VS je primárne v PaymentID, ak chýba, použijeme číslo faktúry
            vs: Optional[str] = get_text(payment_node, 'cbc:PaymentID', default=cislo_faktury) if payment_node else cislo_faktury
            iban = get_text(payment_node, 'cac:PayeeFinancialAccount/cbc:ID') if payment_node else None

            # 4. Sumy (Základ dane, DPH, Celkom)
            totals_node = invoice_node.find('cac:LegalMonetaryTotal', cls.NAMESPACES)
            suma_na_uhradu = get_text(totals_node, 'cbc:PayableAmount', default="0.00")
            zaklad_dph = get_text(totals_node, 'cbc:TaxExclusiveAmount') if totals_node is not None else None
            
            tax_total_node = invoice_node.find('cac:TaxTotal', cls.NAMESPACES)
            suma_dph = get_text(tax_total_node, 'cbc:TaxAmount') if tax_total_node is not None else None

            # 5. Položky faktúry alebo dobropisu (cac:InvoiceLine alebo cac:CreditNoteLine)
            polozky = []
            riadky = invoice_node.findall('cac:InvoiceLine', cls.NAMESPACES) or invoice_node.findall('cac:CreditNoteLine', cls.NAMESPACES)
            for line in riadky:
                item_node = line.find('cac:Item', cls.NAMESPACES)
                nazov = get_text(item_node, 'cbc:Name', default="Neznáma položka") if item_node is not None else "Neznáma položka"
                
                # 🛡️ ISAS ARCHITECT: Množstvo môže byť InvoicedQuantity (FA) alebo CreditedQuantity (Dobropis)
                qty_node = line.find('cbc:InvoicedQuantity', cls.NAMESPACES) or line.find('cbc:CreditedQuantity', cls.NAMESPACES)
                mnozstvo = qty_node.text.strip() if qty_node is not None and qty_node.text else "1.00"
                merna_jednotka = qty_node.attrib.get('unitCode', 'C62') if qty_node is not None else 'C62'
                
                cena_bez_dph = get_text(line, 'cac:Price/cbc:PriceAmount', default="0.00")
                
                # 🛡️ ISAS ARCHITECT FIX: Extrakcia presných identifikátorov a DPH priamo z XML
                kod = get_text(item_node, 'cac:SellersItemIdentification/cbc:ID') if item_node is not None else None
                ean = get_text(item_node, 'cac:StandardItemIdentification/cbc:ID') if item_node is not None else None
                sadzba_dph = get_text(item_node, 'cac:ClassifiedTaxCategory/cbc:Percent') if item_node is not None else None
                tax_cat = get_text(item_node, 'cac:ClassifiedTaxCategory/cbc:ID') if item_node is not None else None
                if not sadzba_dph and tax_cat in ['AE', 'E', 'Z', 'O']:
                    sadzba_dph = "0.00"
                
                polozky.append({
                    'nazov': nazov,
                    'mnozstvo': mnozstvo,
                    'merna_jednotka': merna_jednotka,
                    'cena_bez_dph': cena_bez_dph,
                    'kod': kod,
                    'ean': ean,
                    'sadzba_dph': sadzba_dph
                })

            return {
                'ico': ico,
                'dic': dic,
                'ic_dph': ic_dph,
                'nazov_dodavatela': nazov_dodavatela,
                'sidlo_dodavatela': sidlo_dodavatela,
                'adresa_dodavatela': sidlo_dodavatela,
                'cislo_faktury': cislo_faktury,
                'variabilny_symbol': vs,
                'iban': iban,
                'bankovy_ucet': iban,
                'suma_na_uhradu': suma_na_uhradu,
                'zaklad_dph': zaklad_dph,
                'suma_dph': suma_dph,
                'mena': mena,
                'datum_vystavenia': datum_vystavenia,
                'datum_dodania': datum_dodania,
                'datum_splatnosti': datum_splatnosti,
                'polozky': polozky
            }

        except Exception as e:
            logger.error(f"❌ PeppolXMLParser zlyhal: {str(e)}")
            return {}