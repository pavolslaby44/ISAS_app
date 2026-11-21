# Evidencia/erp_factory.py

import logging
from django.apps import apps
from .erp_generator import PohodaXMLGenerator
from .kros_generator import KrosOmegaTXTGenerator

logger = logging.getLogger(__name__)

class ERPFactory:
    """
    Produkčná továreň (Factory Pattern) pre dynamický výber XML generátora.
    Zabezpečuje absolútnu izoláciu UI vrstvy (admin.py) od účtovnej logiky.
    Dostupné moduly: Pohoda, KROS, Money S3, MRP, eFaktúra (UBL 2.1/IS EFA).
    """
    
    @staticmethod
    def get_generator():
        try:
            # 🛡️ ISAS ARCHITECT: Kontrola legislatívnej podpory krajiny pred inicializáciou generátora
            FiremnaIdentita = apps.get_model('ISASaplikacia', 'FiremnaIdentita')
            raw_krajina = FiremnaIdentita.get_aktivna_krajina_iso() if hasattr(FiremnaIdentita, 'get_aktivna_krajina_iso') else 'NEZADANE'
            krajina = str(raw_krajina).strip().upper() if raw_krajina else 'NEZADANE'

            if not krajina or krajina == 'NEZADANE':
                raise ValueError("🚨 ERP Export nie je možné spustiť: V systéme nie je vyplnený Profil spoločnosti (Firemná identita). Doplňte IČO a štát sídla firmy.")
            
            ERPExportNastavenia = apps.get_model('Evidencia', 'ERPExportNastavenia')
            nastavenia = ERPExportNastavenia.get_settings() if hasattr(ERPExportNastavenia, 'get_settings') else ERPExportNastavenia.objects.first()
            raw_system = getattr(nastavenia, 'erp_system', None) if nastavenia else None
            erp_system = str(raw_system).strip().upper() if raw_system else 'POHODA'

            if erp_system == 'OMEGA' and krajina != 'SK':
                raise ValueError(f"🚨 ERP Export KROS Omega je legislatívne určený výhradne pre Slovenskú republiku (SK DPH / KV DPH). Vaša firma má sídlo v '{krajina}'. Pre ČR a iné krajiny použite Stormware Pohoda.")

            if erp_system == 'POHODA' and krajina not in ['SK', 'CZ']:
                raise ValueError(f"🚨 Stormware Pohoda XML je podporovaná pre trhy SK a CZ. Pre expanziu do '{krajina}' je nutné použiť formát eFaktúra / UBL 2.1.")

            if erp_system == 'POHODA':
                return PohodaXMLGenerator()
            elif erp_system == 'OMEGA':
                return KrosOmegaTXTGenerator()
            else:
                logger.error(f"Kritická chyba: Neznámy ERP systém '{erp_system}'.")
                raise ValueError(f"Systém '{erp_system}' nie je podporovaný ISAS CORE.")
                
        except Exception as e:
            logger.error(f"Zlyhanie v ERPFactory: {str(e)}")
            raise