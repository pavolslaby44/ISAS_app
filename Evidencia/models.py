from django.db import models

from django.db import models

class Zamestnanec(models.Model):
    INTERNI = 'IN'
    EXTERNI = 'EX'
    BRIGADNICI = 'BR'

    KATEGORIE_CHOICES = [
        (INTERNI, 'Interní'),
        (EXTERNI, 'Externí'),
        (BRIGADNICI, 'Brigádnici'),
    ]

    kategoria = models.CharField(max_length=2, choices=KATEGORIE_CHOICES, verbose_name='Kategória')
    meno = models.CharField(max_length=100, verbose_name='Meno')
    priezvisko = models.CharField(max_length=100, verbose_name='Priezvisko')
    datum_narodenia = models.DateField(verbose_name='Dátum narodenia')
    telefon = models.CharField(max_length=20, verbose_name='Telefón')
    email = models.EmailField(verbose_name='Email')
    zmluva = models.TextField(verbose_name='Zmluva')
    zavazky = models.TextField(verbose_name='Záväzky voči firme')

    def __str__(self):
        return f"{self.meno} {self.priezvisko}"

class Meta:
        verbose_name = "Zamestnanec"
        verbose_name_plural = "Zamestnanci"
       
from django.db import models

class Dodavatel(models.Model):
    kontaktna_osoba = models.CharField(max_length=100, verbose_name="Kontaktná osoba")
    telefon = models.CharField(max_length=20, verbose_name="Telefón")
    email = models.EmailField(verbose_name="Email")
    ostatne = models.TextField(blank=True, verbose_name="Ostatné")

    def __str__(self):
        return self.kontaktna_osoba

    class Meta:
        verbose_name = "Dodávateľ"
        verbose_name_plural = "Dodávatelia"

from django.db import models

class Zakaznik(models.Model):
    Kontaktna_osoba = models.CharField(max_length=100, verbose_name="Kontaktná osoba")
    Telefon = models.CharField(max_length=20, verbose_name="Telefón")
    Email = models.EmailField(verbose_name="Email")
    Ostatne = models.TextField(blank=True, verbose_name="Ostatné")

    def __str__(self):
        return self.Kontaktna_osoba

    class Meta:
        verbose_name = "Zákazník"
        verbose_name_plural = "Zákazníci"


from django.db import models

class Sklad(models.Model):
    nazov = models.CharField(max_length=100)

    class Meta:
        verbose_name = "Sklad"
        verbose_name_plural = "Sklady"


    def __str__(self):
        return self.nazov

class Stroj(models.Model):
    sklad = models.ForeignKey(Sklad, on_delete=models.CASCADE, related_name="stroje")
    nazov = models.CharField(max_length=100)

    class Meta:
        verbose_name = "Stroj"
        verbose_name_plural = "Stroje"

    def __str__(self):
        return self.nazov


class SpotrebnyMaterial(models.Model):
    sklad = models.ForeignKey(Sklad, on_delete=models.CASCADE, related_name="materialy")

    class Meta:
        verbose_name = "V skratke o mne"
        verbose_name_plural = "Spotrebné náhradné diely"

    def __str__(self):
        return "Ahoj. Som šuflík spotrebných náhradných dielov :)"


class Poruchovydiel(models.Model):
    sklad = models.ForeignKey(Sklad, on_delete=models.CASCADE, related_name="diely")

    class Meta:
        verbose_name = "V skratke o mne"
        verbose_name_plural = "Poruchové diely"

    def __str__(self):
        return "Ahoj. Som šuflík poruchových dielov :)"


from django.db import models

class PoruchovySnimacKabelaz(models.Model):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="snimace_kabelaze")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)


    class Meta:
        verbose_name = "Snímač + kábel"
        verbose_name_plural = "Snímače + kabeláž"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class PoruchovaElektrovyzbroj(models.Model):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="elektrovyzbroje")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)


    class Meta:
        verbose_name = "Elektrovýzbroj"
        verbose_name_plural = "Elektrovýzbroje"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class PoruchovyRiadiaciModul(models.Model):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="riadiace_moduly")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Riadiaci modul"
        verbose_name_plural = "Riadiace moduly"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class PoruchovyVentilatorMotor(models.Model):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="ventilatory_motory")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Ventilátor a elektromotor"
        verbose_name_plural = "Ventilátory a elektromotory"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class PoruchovaSaciaKlapka(models.Model):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="sacie_klapky")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Solenoid"
        verbose_name_plural = "Solenoidy"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class PoruchoveOstatne(models.Model):
    diel = models.ForeignKey("Poruchovydiel", on_delete=models.CASCADE, related_name="ostatne_diely")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Ostatný diel"
        verbose_name_plural = "Ostatné diely"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"

class SpotrebnyOlej(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="oleje")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Olej"
        verbose_name_plural = "Oleje"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebnyVzduchovyFilter(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="vzduchove_filtre")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Vzduchový filter"
        verbose_name_plural = "Vzduchové filtre"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebnyOlejovyFilter(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="olejove_filtre")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Olejový filter"
        verbose_name_plural = "Olejové filtre"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebnyOlejovySeparator(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="olejove_separatory")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Olejový separátor"
        verbose_name_plural = "Olejové separátory"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebnyKlinovyRemen(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="klinove_remene")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Klinový remeň"
        verbose_name_plural = "Klinové remene"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebnaSpojka(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="spojky")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Spojka"
        verbose_name_plural = "Spojky"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebnaSaciaKlapka(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="sacie_klapky")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Sacia klapka"
        verbose_name_plural = "Sacie klapky"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebnyOdkalovac(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="odkalovace")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Odkalovač"
        verbose_name_plural = "Odkalovače"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebnaSadaVentilov(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="sady_ventilov")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Sada spätných klapiek, oilstop ventilov a ventilov minimálnych tlakov"
        verbose_name_plural = "Sady spätných klapiek a ventilov"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"


class SpotrebneOstatne(models.Model):
    material = models.ForeignKey(SpotrebnyMaterial, on_delete=models.CASCADE, related_name="ostatne_diely")
    seriove_cislo = models.CharField("Sériové číslo", max_length=100)
    pocet_na_sklade = models.PositiveIntegerField("Počet na sklade", default=0)
    kto_som = models.TextField("V skratke o mne", blank=True)

    class Meta:
        verbose_name = "Ostatný diel"
        verbose_name_plural = "Ostatné diely"

    def __str__(self):
        return f"{self.seriove_cislo} (ks: {self.pocet_na_sklade})"

