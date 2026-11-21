# -*- coding: utf-8 -*-
from django.db import migrations

def upratat_data(apps, schema_editor):
    """
    Nájdeme všetkých zamestnancov, ktorí majú v textovom poli 'zavazky'
    prázdny reťazec "" a zmeníme ho na NULL (None), aby ho databáza
    vedela správne konvertovať na číslo 0.
    """
    Zamestnanec = apps.get_model('Evidencia', 'Zamestnanec')
    # Použijeme .exclude(zavazky=None) aby sme neprepisovali už správne NULL hodnoty
    Zamestnanec.objects.exclude(zavazky=None).filter(zavazky="").update(zavazky=None)

class Migration(migrations.Migration):

    dependencies = [
        ('Evidencia', '0051_remove_typservisuprace_hodinova_sadzba_and_more'), # Sem daj názov tvojej poslednej funkčnej migrácie (0051)
    ]

    operations = [
        migrations.RunPython(upratat_data, migrations.RunPython.noop),
    ]