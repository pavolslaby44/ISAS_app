"""
ISAS Core - Celery Task Routing & Beat Schedule Configuration.
Tento modul definuje smerovanie úloh do izolovaných frontov a časový harmonogram úloh.
"""

from celery.schedules import crontab

# ==============================================================================
# 1. SMEROVANIE ÚLOH DO IZOLOVANÝCH FRONTOV
# ==============================================================================
CELERY_TASK_DEFAULT_QUEUE = 'default'

CELERY_TASK_ROUTES = {
    # ⚡ IoT telemetria a alarmy (Queue: 'iot')
    'Evidencia.tasks.process_iot_payload_task': {'queue': 'iot'},
    'Evidencia.tasks.execute_modbus_write_task': {'queue': 'iot'},
    'Evidencia.tasks.drain_fallback_iot_spool_task': {'queue': 'iot'},
    'WebBuilder.tasks.process_alarm_webhooks_task': {'queue': 'iot'},

    # 🐘 Ťažké exporty, zálohy, AI a analytika (Queue: 'heavy')
    'ISASaplikacia.tasks.task_kompletna_denna_zaloha': {'queue': 'heavy'},
    'ISASaplikacia.tasks.forward_invoice_to_evidencia_task': {'queue': 'heavy'},
    'ISASaplikacia.tasks.delete_physical_file_task': {'queue': 'heavy'},
    'cleanup_ai_pdf_cache_task': {'queue': 'heavy'},
    'Evidencia.tasks.spustit_hromadny_export_task': {'queue': 'heavy'},
    'Evidencia.tasks.task_ai_audit_ulohy': {'queue': 'heavy'},
    'Evidencia.tasks.vygeneruj_odborny_text_ai_task': {'queue': 'heavy'},
    'Evidencia.tasks.task_vygeneruj_knihu_jazd_excel': {'queue': 'heavy'},
    'Evidencia.tasks.task_vygeneruj_ocenenie_skladu_excel': {'queue': 'heavy'},
    'Evidencia.tasks.task_vygeneruj_mzdy_excel': {'queue': 'heavy'},
    'Evidencia.tasks.spracuj_import_dat_task': {'queue': 'heavy'},
    'Evidencia.tasks.generuj_protokol_pdf_task': {'queue': 'heavy'},
    'Evidencia.tasks.generuj_ponuku_pdf_task': {'queue': 'heavy'},
    'Evidencia.tasks.vygeneruj_pdf_a_notifikuj_fakturu_task': {'queue': 'heavy'},
    'Evidencia.tasks.task_vygeneruj_a_odosli_objednavky': {'queue': 'heavy'},
    'Evidencia.tasks.vygeneruj_vstupku_ai_task': {'queue': 'heavy'},
    'Evidencia.tasks.pregeneruj_a_odosli_aktualizaciu_vstupu_task': {'queue': 'heavy'},
    'Evidencia.tasks.task_vygeneruj_a_odosli_erp_export': {'queue': 'heavy'},
    'Evidencia.tasks.task_hromadne_ai_spracovanie_faktur': {'queue': 'heavy'},
    'Evidencia.tasks.spracuj_fakturu_ai_task': {'queue': 'heavy'},
    'Evidencia.tasks.task_peppol_ai_semantika': {'queue': 'heavy'},
    'Evidencia.tasks.spracuj_ai_chat_task': {'queue': 'heavy'},
    'Evidencia.tasks.task_vycisti_stare_gps_stopy': {'queue': 'heavy'},
    'Evidencia.tasks.task_tyzdenna_predikcia_nakupu': {'queue': 'heavy'},
    'Evidencia.tasks.fetch_reklamacie_emails_task': {'queue': 'heavy'},
    'Evidencia.tasks.fetch_orders_emails_task': {'queue': 'heavy'},
    'Evidencia.tasks.fetch_emails_task': {'queue': 'heavy'},
    'Evidencia.tasks.isas_bankar_task': {'queue': 'heavy'},
    'Evidencia.tasks.fetch_peppol_invoices_task': {'queue': 'heavy'},
    'Evidencia.tasks.strazca_fleet_task': {'queue': 'heavy'},
    'Evidencia.tasks.strazca_servisov_task': {'queue': 'heavy'},
    'Evidencia.tasks.task_skontroluj_sklad_jit': {'queue': 'heavy'},
    'Evidencia.tasks.task_vyhodnot_smart_matcher': {'queue': 'heavy'},
    'Evidencia.tasks.task_vyhodnot_smart_matcher_pre_ponuku': {'queue': 'heavy'},
    'Evidencia.tasks.task_vyhodnot_smart_matcher_faktura_objednavka': {'queue': 'heavy'},
    'Evidencia.tasks.skontrolovat_zamrznute_exporty_task': {'queue': 'heavy'},
    'Evidencia.tasks.task_archivovat_a_vycistit_audit_logy': {'queue': 'heavy'},
    'Evidencia.tasks.task_vycisti_staru_telemetriu': {'queue': 'heavy'},
    'WebBuilder.tasks.task_vycisti_stare_webhook_logy': {'queue': 'heavy'},

    # 📨 Notifikácie, e-maily a webhooky (Queue: 'notifications')
    'ISASaplikacia.tasks.sync_isas_mail_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.forward_mail_to_technician_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.send_email_smtp_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.mark_imap_read_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.trash_imap_email_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.save_draft_imap_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.delete_draft_imap_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.empty_trash_imap_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.bulk_trash_imap_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.restore_imap_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.delete_permanently_imap_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.bulk_restore_imap_task': {'queue': 'notifications'},
    'ISASaplikacia.tasks.bulk_delete_permanently_imap_task': {'queue': 'notifications'},
    'WebBuilder.tasks.send_webhook_task': {'queue': 'notifications'},
    'WebBuilder.tasks.send_ping_webhook_task': {'queue': 'notifications'},
    'WebBuilder.tasks.process_objednavka_webhooks_task': {'queue': 'notifications'},
    'WebBuilder.tasks.process_servis_webhooks_task': {'queue': 'notifications'},
    'WebBuilder.tasks.notify_web_inquiry_task': {'queue': 'notifications'},
    'WebBuilder.tasks.notify_new_candidate_task': {'queue': 'notifications'},
    'Evidencia.tasks.task_notifikuj_technikov_o_ulohe': {'queue': 'notifications'},
    'Evidencia.tasks.task_notifikuj_schvalenie_ponuky': {'queue': 'notifications'},
    'Evidencia.tasks.odosli_vstupku_na_vratnicu_task': {'queue': 'notifications'},
    'Evidencia.tasks.odosli_sbs_notifikacie_task': {'queue': 'notifications'},
    'Evidencia.tasks.odosli_efakturu_peppol_task': {'queue': 'notifications'},
    'Evidencia.tasks.task_notifikuj_kandidata_o_stave': {'queue': 'notifications'},
    'Evidencia.tasks.posli_email_uctovnicke_task': {'queue': 'notifications'},
    'Evidencia.tasks.posli_peppol_vizualizaciu_uctovnicke_task': {'queue': 'notifications'},
    'Evidencia.tasks.odosli_notifikaciu_o_prijme_task': {'queue': 'notifications'},
    'Evidencia.tasks.task_odosli_notifikaciu_nizky_stav': {'queue': 'notifications'},
    'Evidencia.tasks.uloha_odoslat_notifikacie_adminovi_task': {'queue': 'notifications'},
    'Evidencia.tasks.zaznam_odoslat_notifikacie_adminovi_task': {'queue': 'notifications'},
    'Evidencia.tasks.task_odoslat_knihu_jazd_uctovnicke': {'queue': 'notifications'},
    'Evidencia.tasks.task_odoslat_ocenenie_skladu_uctovnicke': {'queue': 'notifications'},
    'Evidencia.tasks.task_odoslat_mzdy_uctovnicke': {'queue': 'notifications'},
    'Evidencia.tasks.send_isas_stock_notifications_task': {'queue': 'notifications'},
    'Evidencia.tasks.send_isas_bulk_stock_notifications_task': {'queue': 'notifications'},
    'Evidencia.tasks.task_odosli_notifikaciu_presun': {'queue': 'notifications'},
    'Evidencia.tasks.task_ziskaj_gps_zakaznika': {'queue': 'notifications'},
    'Evidencia.tasks.task_odoslat_ziadost_uctovnicke': {'queue': 'notifications'},
    'Evidencia.tasks.task_odoslat_notifikaciu_o_nastaveni_erp': {'queue': 'notifications'},
}

# ==============================================================================
# 2. HARMONOGRAM PLÁNOVANÝCH ÚLOH (CELERY BEAT)
# ==============================================================================
CELERY_BEAT_SCHEDULE = {
    # --- IoT Núdzový Spool (Kontrola a odčerpanie každých 60 sekúnd) ---
    'isas-iot-drain-fallback-spool': {
        'task': 'Evidencia.tasks.drain_fallback_iot_spool_task',
        'schedule': 60.0,
        'options': {
            'queue': 'iot',
        },
    },

    # --- E-maily & Správy (Rozptýlené, aby nepreťažili IMAP hosting) ---
    'isas-postar-vseobecny': {
        'task': 'Evidencia.tasks.fetch_emails_task',
        'schedule': crontab(minute='0,5,10,15,20,25,30,35,40,45,50,55'),
    },
    'isas-mail-nexus-sync': {
        'task': 'ISASaplikacia.tasks.sync_isas_mail_task',
        'schedule': crontab(minute='1,6,11,16,21,26,31,36,41,46,51,56'),
    },
    'isas-reklamacie-inbox': {
        'task': 'Evidencia.tasks.fetch_reklamacie_emails_task',
        'schedule': crontab(minute='2,7,12,17,22,27,32,37,42,47,52,57'),
    },
    'isas-objednavky-inbox': {
        'task': 'Evidencia.tasks.fetch_orders_emails_task',
        'schedule': crontab(minute='3,8,13,18,23,28,33,38,43,48,53,58'),
    },

    # --- Financie & Faktúry ---
    'isas-bankar-denny': {
        'task': 'Evidencia.tasks.isas_bankar_task',
        'schedule': crontab(minute='4,19,34,49', hour='7-19'),
    },
    'isas-bankar-nocny': {
        'task': 'Evidencia.tasks.isas_bankar_task',
        'schedule': crontab(minute=15, hour='20,22,0,2,4,6'),
    },
    'isas-peppol-inbox': {
        'task': 'Evidencia.tasks.fetch_peppol_invoices_task',
        'schedule': crontab(minute='14,44', hour='7-20'),
    },

    # --- Ranné strážne úlohy ---
    'isas-fleet-strazca-ranny': {
        'task': 'Evidencia.tasks.strazca_fleet_task',
        'schedule': crontab(minute=30, hour=6),
    },
    'isas-servis-strazca-ranny': {
        'task': 'Evidencia.tasks.strazca_servisov_task',
        'schedule': crontab(minute=35, hour=6),
    },

    # --- Nočná údržba a zálohy ---
    'kompletna-denna-zaloha': {
        'task': 'ISASaplikacia.tasks.task_kompletna_denna_zaloha',
        'schedule': crontab(minute=0, hour=2),
        'options': {
            'time_limit': 3600,
            'soft_time_limit': 3540,
        },
    },
    'isas-ai-pdf-cache-cleanup-nocny': {
        'task': 'cleanup_ai_pdf_cache_task',
        'schedule': crontab(minute=15, hour=3),
    },
    'isas-gps-hrobar-nocny': {
        'task': 'Evidencia.tasks.task_vycisti_stare_gps_stopy',
        'schedule': crontab(minute=30, hour=3),
    },
    'isas-telemetria-retencia-nocna': {
        'task': 'Evidencia.tasks.task_vycisti_staru_telemetriu',
        'schedule': crontab(minute=40, hour=3),
        'kwargs': {'retencia_dni': 365},
        'options': {
            'time_limit': 1800,
            'soft_time_limit': 1740,
        },
    },
    'isas-webhook-logy-retencia-nocna': {
        'task': 'WebBuilder.tasks.task_vycisti_stare_webhook_logy',
        'schedule': crontab(minute=45, hour=3),
        'options': {
            'time_limit': 1800,
            'soft_time_limit': 1740,
        },
    },

    # --- Predikcie & Systémový monitoring ---
    'isas-audit-logy-retencia-archiv': {
        'task': 'Evidencia.tasks.task_archivovat_a_vycistit_audit_logy',
        'schedule': crontab(day_of_week='0', hour=3, minute=0), # Každú nedeľu o 03:00 ráno
        'kwargs': {'zmazat_po_exporte': True},
        'options': {
            'time_limit': 7200,
            'soft_time_limit': 7100,
        },
    },
    'isas-export-strazca-hodinovy': {
        'task': 'Evidencia.tasks.skontrolovat_zamrznute_exporty_task',
        'schedule': crontab(minute=45),
    },
    'isas-tyzdenna-predikcia-nakupu': {
        'task': 'Evidencia.tasks.task_tyzdenna_predikcia_nakupu',
        'schedule': crontab(day_of_week='1', hour=6, minute=0),
    },
}