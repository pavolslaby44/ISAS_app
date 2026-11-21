import logging
import re
from django.template.loader import render_to_string
from django.core.files.base import ContentFile
from playwright.sync_api import sync_playwright
from ISASaplikacia.models import FiremnaIdentita
from Evidencia.utils_helper import get_firemna_mena
from .utils_peppol_inbox import PeppolXMLParser

logger = logging.getLogger(__name__)

class PeppolPDFGenerator:
    """
    ISAS CORE: Deterministický generátor PDF vizualizácií z Peppol XML.
    Využíva Playwright (Chromium) pre 100% pixel-perfect render moderného CSS.
    """

    @staticmethod
    def vygeneruj_pdf(faktura):
        xml_subor = getattr(faktura, 'xml_subor', None)
        if not xml_subor:
            raise ValueError("Faktúra neobsahuje XML originál.")

        # 1. Extrakcia dát z XML (defenzívne ošetrenie lokálneho aj cloudového úložiska)
        try:
            with xml_subor.open('rb') as f:
                if hasattr(f, 'seek'):
                    try:
                        f.seek(0)
                    except Exception:
                        pass
                xml_content = f.read()
        except Exception as e:
            logger.error(f"❌ Chyba pri čítaní XML súboru z úložiska: {str(e)}")
            raise ValueError(f"Súbor XML originálu nie je dostupný na úložisku: {str(e)}")

        try:
            parsed_data = PeppolXMLParser.parse(xml_content)
        except Exception as e:
            logger.error(f"❌ Chyba pri parsovaní XML Peppol dokladu: {str(e)}")
            raise ValueError(f"Nepodarilo sa vyparsovať dáta z XML: {str(e)}")
        
        if not parsed_data:
            raise ValueError("Nepodarilo sa vyparsovať dáta z XML.")

        # 2. Príprava kontextu pre šablónu (SaaS multi-tenant izolácia identity a meny)
        identita = getattr(faktura, 'firma', None) or getattr(faktura, 'firemna_identita', None) or FiremnaIdentita.objects.first()
        polozky = parsed_data.get('polozky', []) if isinstance(parsed_data, dict) else []
        mena_dokladu = (
            (parsed_data.get('mena') if isinstance(parsed_data, dict) else None)
            or getattr(faktura, 'mena', None)
            or getattr(identita, 'predvolena_mena', None)
            or getattr(identita, 'mena', None)
            or get_firemna_mena()
        )
        
        context = {
            'faktura': faktura,
            'identita': identita,
            'xml_data': parsed_data,
            'polozky': polozky,
            'mena': mena_dokladu,
        }

        # 3. Render HTML stringu
        html_string = render_to_string('admin/peppol_vizualizacia.html', context)

        # 4. Generovanie PDF cez Playwright (defenzívne ošetrenie pre Docker kontajnery a offline prostredia)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
                )
                try:
                    page = browser.new_page()
                    # Načítame HTML priamo do pamäte prehliadača; 'load' zaručuje okamžitý render inlinovaného CSS bez timeoutov
                    page.set_content(html_string, wait_until="load", timeout=15000)
                    
                    # Vygenerujeme PDF (A4, zachovanie farieb pozadia)
                    pdf_bytes = page.pdf(
                        format="A4",
                        print_background=True,
                        prefer_css_page_size=True,
                        margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"}
                    )
                finally:
                    browser.close()
        except Exception as e:
            logger.error(f"❌ Playwright zlyhal pri generovaní PDF: {str(e)}")
            raise RuntimeError(f"Chyba pri generovaní PDF: {str(e)}")

        # 5. Uloženie do modelu
        if not pdf_bytes:
            raise RuntimeError("Playwright vygeneroval prázdny PDF súbor.")

        vizualizacia_field = getattr(faktura, 'vizualizacia_subor', None)
        if vizualizacia_field is None:
            raise AttributeError("Objekt faktúry neobsahuje požadované pole 'vizualizacia_subor'.")

        # Sanitizácia názvu súboru. Peppol ID obsahuje dvojbodky a špeciálne znaky, ktoré zhodia OS File System!
        raw_id = str(getattr(faktura, 'peppol_document_id', None) or getattr(faktura, 'id', None) or '')
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_id).strip('_')
        # Skrátime názov, aby neprekročil limity OS (max 255 znakov, necháme rezervu) a zaistíme bezpečný fallback
        safe_id = safe_id[:100] or 'doklad'
        
        file_name = f"vizualizacia_{safe_id}.pdf"
        vizualizacia_field.save(name=file_name, content=ContentFile(pdf_bytes), save=False)
        if getattr(faktura, 'pk', None):
            faktura.save(update_fields=['vizualizacia_subor'])
        else:
            faktura.save()
        
        try:
            return vizualizacia_field.url
        except Exception:
            return getattr(vizualizacia_field, 'name', '')