from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.db import transaction, IntegrityError
from ISASaplikacia.models import StatLokalizacia

class Command(BaseCommand):
    help = "Naplní výhradne konfiguráciu regiónov a sviatkov (subdiv) pre model StatLokalizacia bez zásahu do meny a DPH."

    def handle(self, *args, **options):
        # 🌍 100 % kompletná matica regiónov: NULA vynechaných kantónov/krajov, NULA mien, NULA sadzieb DPH.
        konfiguracie = {
            # ==========================================
            # 🇩🇪 NEMECKO (16 zo 16 Bundesländer - precízne ohraničené PSČ bez kolízií)
            # ==========================================
            'DE': {
                'BW': {'aliasy': ['baden', 'wurttemberg', 'württemberg', 'stuttgart', 'karlsruhe', 'mannheim', 'freiburg', 'heidelberg', 'ulm', 'heilbronn', 'pforzheim', 'reutlingen', 'ravensburg', 'friedrichshafen'], 'psc_regex': r'\b(68[0-9]{3}|69[0-9]{3}|7[0-9]{4}|88[0-9]{3}|890[0-9]{2}|891[0-9]{2})\b'},
                'BY': {'aliasy': ['bayern', 'bavaria', 'munchen', 'münchen', 'nurnberg', 'nürnberg', 'augsburg', 'regensburg', 'ingolstadt', 'würzburg', 'erlangen', 'bamberg', 'passau', 'neu-ulm'], 'psc_regex': r'\b(8[0-7][0-9]{3}|892[0-9]{2}|89[3-9][0-9]{2}|9[0-7][0-9]{3})\b'},
                'NW': {'aliasy': ['nordrhein', 'westfalen', 'nrw', 'koln', 'köln', 'dusseldorf', 'düsseldorf', 'dortmund', 'essen', 'duisburg', 'bochum', 'wuppertal', 'bielefeld', 'bonn', 'munster', 'münster', 'aachen', 'monchengladbach', 'gelsenkirchen', 'siegen'], 'psc_regex': r'\b(32[0-9]{3}|33[0-9]{3}|4[0-8][0-9]{3}|5[0-3789][0-9]{3})\b'},
                'HE': {'aliasy': ['hessen', 'hesse', 'frankfurt', 'wiesbaden', 'kassel', 'darmstadt', 'offenbach', 'hanau', 'giessen', 'fulda', 'marburg'], 'psc_regex': r'\b(34[0-9]{3}|35[0-9]{3}|36[0-9]{3}|6[0-5][0-9]{3})\b'},
                'BE': {'aliasy': ['berlin'], 'psc_regex': r'\b(1[0-3][0-9]{3}|14[0-1][0-9]{2})\b'},
                'BB': {'aliasy': ['brandenburg', 'potsdam', 'cottbus', 'brandenburg an der havel', 'frankfurt oder', 'eberswalde'], 'psc_regex': r'\b(03[0-9]{3}|14[4-9][0-9]{2}|15[0-9]{3}|16[0-9]{3})\b'},
                'SN': {'aliasy': ['sachsen', 'saxony', 'dresden', 'leipzig', 'chemnitz', 'zwickau', 'plauen', 'gorlitz', 'görlitz'], 'psc_regex': r'\b(0[1-2][0-9]{3}|04[0-9]{3}|08[0-9]{3}|09[0-9]{3})\b'},
                'NI': {'aliasy': ['niedersachsen', 'lower saxony', 'hannover', 'braunschweig', 'osnabruck', 'osnabrück', 'oldenburg', 'wolfsburg', 'gottingen', 'göttingen', 'salzgitter', 'hildesheim'], 'psc_regex': r'\b(26[0-9]{3}|27[0-9]{3}|29[0-9]{3}|30[0-9]{3}|31[0-9]{3}|37[0-9]{3}|38[0-9]{3}|49[0-9]{3})\b'},
                'RP': {'aliasy': ['rheinland', 'pfalz', 'mainz', 'ludwigshafen', 'koblenz', 'trier', 'kaiserslautern', 'worms', 'neustadt'], 'psc_regex': r'\b(54[0-9]{3}|55[0-9]{3}|56[0-9]{3}|67[0-9]{3})\b'},
                'TH': {'aliasy': ['thuringen', 'thüringen', 'erfurt', 'jena', 'gera', 'weimar', 'gotha', 'eisenach', 'nordhausen', 'suhl'], 'psc_regex': r'\b(07[0-9]{3}|98[0-9]{3}|99[0-9]{3})\b'},
                'ST': {'aliasy': ['sachsen-anhalt', 'magdeburg', 'halle', 'dessau', 'wittenberg', 'halberstadt', 'stendal'], 'psc_regex': r'\b(06[0-9]{3}|39[0-9]{3})\b'},
                'SH': {'aliasy': ['schleswig', 'holstein', 'kiel', 'lubeck', 'lübeck', 'flensburg', 'neumunster', 'neumünster', 'norderstedt'], 'psc_regex': r'\b2[3-5][0-9]{3}\b'},
                'HH': {'aliasy': ['hamburg'], 'psc_regex': r'\b2[0-2][0-9]{3}\b'},
                'SL': {'aliasy': ['saarland', 'saarbrucken', 'saarbrücken', 'neunkirchen', 'homburg', 'volklingen', 'saarlouis'], 'psc_regex': r'\b66[0-9]{3}\b'},
                'HB': {'aliasy': ['bremen', 'bremerhaven'], 'psc_regex': r'\b28[0-9]{3}\b'},
                'MV': {'aliasy': ['mecklenburg', 'vorpommern', 'rostock', 'schwerin', 'neubrandenburg', 'stralsund', 'greifswald', 'wismar'], 'psc_regex': r'\b(17[0-9]{3}|18[0-9]{3}|19[0-9]{3})\b'}
            },

            # ==========================================
            # 🇨🇭 ŠVAJČIARSKO (26 z 26 Kantónov)
            # ==========================================
            'CH': {
                'ZH': {'aliasy': ['zurich', 'zürich', 'winterthur', 'uster'], 'psc_regex': r'\b(80[0-9]{2}|81[0-9]{2}|83[0-4][0-9]|838[0-9]|839[0-9]|84[0-9]{2}|86[0-3][0-9]|870[0-9]|871[0-4]|880[0-9]|881[0-9]|8820|890[0-9]|891[0-9]|892[0-9]|893[0-9]|894[0-9]|895[0-2])\b'},
                'BE': {'aliasy': ['bern', 'berne', 'thun', 'biel', 'bienne'], 'psc_regex': r'\b(25[0-9]{2}|27[0-9]{2}|3[0-9]{3})\b'},
                'LU': {'aliasy': ['luzern', 'lucerne', 'emmen', 'kriens'], 'psc_regex': r'\b(60[0-4][0-9]|608[0-9]|61[0-9]{2}|62[0-9]{2})\b'},
                'UR': {'aliasy': ['uri', 'altdorf'], 'psc_regex': r'\b64[6-9][0-9]\b'},
                'SZ': {'aliasy': ['schwyz', 'einsiedeln', 'freienbach'], 'psc_regex': r'\b(64[3-5][0-9]|88[3-5][0-9])\b'},
                'OW': {'aliasy': ['obwalden', 'sarnen'], 'psc_regex': r'\b60[5-7][0-9]\b'},
                'NW': {'aliasy': ['nidwalden', 'stans'], 'psc_regex': r'\b63[6-8][0-9]\b'},
                'GL': {'aliasy': ['glarus', 'näfels', 'nafels'], 'psc_regex': r'\b87[5-8][0-9]\b'},
                'ZG': {'aliasy': ['zug', 'baar', 'cham'], 'psc_regex': r'\b63[0-4][0-9]\b'},
                'FR': {'aliasy': ['fribourg', 'freiburg', 'bulle'], 'psc_regex': r'\b(14[7-9][0-9]|16[0-9]{2}|17[0-9]{2})\b'},
                'SO': {'aliasy': ['solothurn', 'olten', 'grenchen'], 'psc_regex': r'\b(45[0-9]{2}|46[0-9]{2}|47[0-9]{2})\b'},
                'BS': {'aliasy': ['basel-stadt', 'basel', 'bazilej'], 'psc_regex': r'\b40[0-9]{2}\b'},
                'BL': {'aliasy': ['basel-landschaft', 'liestal', 'allschwil', 'reinach'], 'psc_regex': r'\b(41[0-9]{2}|44[0-9]{2})\b'},
                'SH': {'aliasy': ['schaffhausen', 'neuhausen'], 'psc_regex': r'\b82[0-4][0-9]\b'},
                'AI': {'aliasy': ['appenzell innerrhoden', 'appenzell'], 'psc_regex': r'\b(905[0-9]|9108)\b'},
                'AR': {'aliasy': ['appenzell ausserrhoden', 'herisau'], 'psc_regex': r'\b(910[0-7]|9109|911[0-9])\b'},
                'SG': {'aliasy': ['st. gallen', 'st gallen', 'sankt gallen', 'rapperswil', 'wil'], 'psc_regex': r'\b(73[0-9]{2}|86[4-5][0-9]|90[0-4][0-9]|94[0-9]{2})\b'},
                'GR': {'aliasy': ['graubunden', 'graubünden', 'grisons', 'chur', 'davos', 'st. moritz'], 'psc_regex': r'\b7[0-7][0-9]{2}\b'},
                'AG': {'aliasy': ['aargau', 'aarau', 'baden', 'wettingen', 'wohlen'], 'psc_regex': r'\b(43[0-9]{2}|5[0-7][0-9]{2})\b'},
                'TG': {'aliasy': ['thurgau', 'frauenfeld', 'kreuzlingen'], 'psc_regex': r'\b(82[5-8][0-9]|83[5-7][0-9]|85[0-9]{2})\b'},
                'TI': {'aliasy': ['ticino', 'tessin', 'lugano', 'bellinzona', 'locarno', 'mendrisio'], 'psc_regex': r'\b6[5-9][0-9]{2}\b'},
                'VD': {'aliasy': ['vaud', 'lausanne', 'montreux', 'vevey', 'yverdon'], 'psc_regex': r'\b(10[0-9]{2}|11[0-9]{2}|12[6-9][0-9]|13[0-9]{2}|14[0-6][0-9]|15[0-9]{2}|18[0-9]{2})\b'},
                'VS': {'aliasy': ['valais', 'wallis', 'sion', 'sitten', 'brig', 'martigny', 'monthey', 'zermatt'], 'psc_regex': r'\b(18[7-9][0-9]|19[0-9]{2}|39[0-9]{2})\b'},
                'NE': {'aliasy': ['neuchatel', 'neuchâtel', 'la chaux-de-fonds'], 'psc_regex': r'\b2[0-4][0-9]{2}\b'},
                'GE': {'aliasy': ['geneve', 'genève', 'geneva', 'zeneva'], 'psc_regex': r'\b12[0-5][0-9]\b'},
                'JU': {'aliasy': ['jura', 'delemont', 'delémont'], 'psc_regex': r'\b(23[4-6][0-9]|28[0-9]{2}|29[0-9]{2})\b'}
            },

            # ==========================================
            # 🇪🇸 ŠPANIELSKO (19 z 19 Regiónov a miest)
            # ==========================================
            'ES': {
                'AN': {'aliasy': ['andalucia', 'andalusia', 'sevilla', 'malaga', 'cordoba', 'granada', 'jerez', 'almeria', 'cadiz', 'huelva', 'marbella', 'jaen'], 'psc_regex': r'\b(04[0-9]{3}|11[0-9]{3}|14[0-9]{3}|18[0-9]{3}|21[0-9]{3}|23[0-9]{3}|29[0-9]{3}|41[0-9]{3})\b'},
                'AR': {'aliasy': ['aragon', 'aragonie', 'zaragoza', 'huesca', 'teruel'], 'psc_regex': r'\b(22[0-9]{3}|44[0-9]{3}|50[0-9]{3})\b'},
                'AS': {'aliasy': ['asturias', 'oviedo', 'gijon', 'aviles'], 'psc_regex': r'\b33[0-9]{3}\b'},
                'CB': {'aliasy': ['cantabria', 'santander', 'torrelavega'], 'psc_regex': r'\b39[0-9]{3}\b'},
                'CL': {'aliasy': ['castilla y leon', 'valladolid', 'burgos', 'salamanca', 'leon', 'palencia', 'zamora', 'segovia', 'avila', 'soria'], 'psc_regex': r'\b(05[0-9]{3}|09[0-9]{3}|24[0-9]{3}|34[0-9]{3}|37[0-9]{3}|40[0-9]{3}|42[0-9]{3}|47[0-9]{3}|49[0-9]{3})\b'},
                'CM': {'aliasy': ['castilla-la mancha', 'toledo', 'albacete', 'ciudad real', 'guadalajara', 'cuenca'], 'psc_regex': r'\b(02[0-9]{3}|13[0-9]{3}|16[0-9]{3}|19[0-9]{3}|45[0-9]{3})\b'},
                'CT': {'aliasy': ['catalunya', 'catalonia', 'katalansko', 'barcelona', 'girona', 'lleida', 'tarragona', 'badalona', 'terrassa', 'sabadell', 'mataro'], 'psc_regex': r'\b(08[0-9]{3}|17[0-9]{3}|25[0-9]{3}|43[0-9]{3})\b'},
                'EX': {'aliasy': ['extremadura', 'badajoz', 'caceres', 'merida'], 'psc_regex': r'\b(06[0-9]{3}|10[0-9]{3})\b'},
                'GA': {'aliasy': ['galicia', 'vigo', 'a coruna', 'la coruna', 'ourense', 'lugo', 'santiago de compostela', 'pontevedra'], 'psc_regex': r'\b(15[0-9]{3}|27[0-9]{3}|32[0-9]{3}|36[0-9]{3})\b'},
                'IB': {'aliasy': ['baleares', 'illes balears', 'palma de mallorca', 'ibiza', 'menorca', 'formentera'], 'psc_regex': r'\b07[0-9]{3}\b'},
                'CN': {'aliasy': ['canarias', 'kanarske ostrovy', 'las palmas', 'santa cruz de tenerife', 'tenerife', 'gran canaria'], 'psc_regex': r'\b(35[0-9]{3}|38[0-9]{3})\b'},
                'RI': {'aliasy': ['la rioja', 'logrono', 'logroño'], 'psc_regex': r'\b26[0-9]{3}\b'},
                'MD': {'aliasy': ['madrid', 'alcala de henares', 'getafe', 'mostoles', 'leganes', 'fuenlabrada', 'alcorcon'], 'psc_regex': r'\b28[0-9]{3}\b'},
                'MC': {'aliasy': ['murcia', 'cartagena', 'lorca', 'molina de segura'], 'psc_regex': r'\b30[0-9]{3}\b'},
                'NC': {'aliasy': ['navarra', 'pamplona', 'iruna', 'tudela'], 'psc_regex': r'\b31[0-9]{3}\b'},
                'PV': {'aliasy': ['pais vasco', 'euskadi', 'baskicko', 'bilbao', 'vitoria', 'gasteiz', 'san sebastian', 'donostia', 'barakaldo'], 'psc_regex': r'\b(01[0-9]{3}|20[0-9]{3}|48[0-9]{3})\b'},
                'VC': {'aliasy': ['valencia', 'comunidad valenciana', 'alicante', 'castellon', 'elche', 'torrevieja', 'gandia'], 'psc_regex': r'\b(03[0-9]{3}|12[0-9]{3}|46[0-9]{3})\b'},
                'CE': {'aliasy': ['ceuta'], 'psc_regex': r'\b510[0-9]{2}\b'},
                'ML': {'aliasy': ['melilla'], 'psc_regex': r'\b520[0-9]{2}\b'}
            },

            # ==========================================
            # 🇫🇷 FRANCÚZSKO (Iba Alsace-Moselle s lokálnym zákonníkom práce)
            # ==========================================
            'FR': {
                'Alsace-Moselle': {
                    'aliasy': [
                        'alsace', 'moselle', 'bas-rhin', 'haut-rhin', 
                        'strasbourg', 'metz', 'mulhouse', 'colmar', 'thionville'
                    ],
                    'psc_regex': r'\b(57[0-9]{3}|67[0-9]{3}|68[0-9]{3})\b'
                }
            }
        }

        # 🛡️ DEFENZÍVNY ZÁPIS: Ukladá výhradne pole subdiv_konfiguracia v atomickej transakcii so zámkom proti race condition.
        # NIKDY nenastavuje menu ani sadzby DPH.
        with transaction.atomic():
            for iso, subdiv_data in konfiguracie.items():
                iso_clean = str(iso).strip().upper()
                stat = StatLokalizacia.objects.select_for_update().filter(iso_kod=iso_clean).first()
                if not stat:
                    try:
                        # Savepoint na izoláciu pokusu o vytvorenie pre prípad súbehu procesov
                        with transaction.atomic():
                            stat = StatLokalizacia.objects.create(
                                iso_kod=iso_clean,
                                nazov=iso_clean,
                                mena='',
                                subdiv_konfiguracia=subdiv_data
                            )
                    except IntegrityError:
                        # Ochrana pred súbehom: paralelný proces medzičasom záznam vytvoril
                        stat = StatLokalizacia.objects.select_for_update().filter(iso_kod=iso_clean).first()
                        if stat:
                            stat.subdiv_konfiguracia = subdiv_data
                            stat.save(update_fields=['subdiv_konfiguracia'])
                        else:
                            # 🛡️ DEFENSIVE FAIL-FAST: Ak záznam neexistuje, chyba nebola spôsobená kolíziou iso_kod
                            raise
                else:
                    stat.subdiv_konfiguracia = subdiv_data
                    stat.save(update_fields=['subdiv_konfiguracia'])

                self.stdout.write(self.style.SUCCESS(f"✔ Štát {iso_clean}: Všetky regióny/subdivízie boli 100 % aktualizované (mena a DPH nedotknuté)."))

        # 🛡️ OCHRANA PRED STALE CACHE RACE CONDITION:
        # Invalidácia cache prebieha až po úspešnom commite transakcie do DB,
        # čím sa zabráni tomu, aby súbežný proces načítal necommitnuté dáta z DB späť do cache.
        for iso_key in konfiguracie.keys():
            cache.delete(f'isas_stat_lokalizacia_{str(iso_key).strip().upper()}')
        cache.delete('isas_vsetky_staty_mapa_cache')
        self.stdout.write(self.style.SUCCESS("🎉 Matica regiónov bola kompletne uložená bez jediného hardkódu meny či dane."))