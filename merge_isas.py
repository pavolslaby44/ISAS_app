import os

# Konfigurácia - hľadáme len mozog projektu
INCLUDE_EXTENSIONS = ('.py', '.js', '.css', '.html')

INCLUDE_EXACT_FILES = {
    'Dockerfile', 'docker-compose.yml', 'requirements.txt', 'celerybeat-schedule.bak', 'celerybeat-schedule.dat', 'celerybeat-schedule.dir'
}

# PRÍSNA Cenzúra priečinkov (šum, ktorý AI nepotrebuje)
IGNORE_DIRS = {
    'venv', '.git', '__pycache__', 'migrations', 'staticfiles', 
    'media', 'env', '.vscode', 'node_modules', 'backup'
}

# PRÍSNA Cenzúra konkrétnych súborov s dátami (settings.py je odtiaľto PREČ)
IGNORE_FILES = {
    'merge_isas.py', 'manage.py', 'tests.py', 
    'import_firiem.py', 'seed.py', 'populate_db.py'
}

OUTPUT_FILE = "isas_full_context_logic.txt"

def merge_project():
    project_root = os.getcwd()
    count = 0
    total_chars = 0
    
    # Zistíme názov projektu z priečinka
    project_name = os.path.basename(project_root)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as outfile:
        outfile.write(f"### ISAS CORE - KOMPLETNÝ ARCHITEKTONICKÝ KONTEXT ###\n")
        outfile.write(f"### PROJEKT: {project_name} ###\n\n")

        for root, dirs, files in os.walk(project_root):
            # Odfiltrovanie zakázaných priečinkov
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            
            for file in files:
                # 1. Kontrola prípony ALEBO presného názvu súboru
                if not (file.endswith(INCLUDE_EXTENSIONS) or file in INCLUDE_EXACT_FILES):
                    continue
                
                # 2. Kontrola čiernej listiny súborov (tu už settings.py nie je)
                if file in IGNORE_FILES:
                    continue
                
                # 3. Filter na skripty začínajúce 'import_' (obvykle len dáta)
                if file.startswith('import_') and file != 'import_firiem.py': # import_firiem je v blacklist hore, toto je poistka
                    continue

                file_path = os.path.join(root, file)
                relative_path = os.path.relpath(file_path, project_root)
                
                try:
                    with open(file_path, 'r', encoding='utf-8') as infile:
                        content = infile.read()
                        
                        # 4. Limit na veľkosť zvýšený na 1 milión znakov (kvôli tvojmu adminu)
                        if len(content) > 2000000:
                            print(f"⚠️ Preskočený EXTRÉMNE veľký súbor: {relative_path}")
                            continue

                        outfile.write(f"\n\n{'='*50}\n")
                        outfile.write(f"### SÚBOR: {relative_path} ###\n")
                        outfile.write(f"{'='*50}\n\n")
                        outfile.write(content)
                        
                        count += 1
                        total_chars += len(content)
                        print(f"✅ Pridané: {relative_path}")
                except Exception as e:
                    print(f"❌ Chyba pri čítaní {relative_path}: {e}")

    print(f"\n🚀 OPERÁCIA DOKONČENÁ!")
    print(f"📊 Zlúčených súborov: {count}")
    print(f"📉 Výsledná veľkosť: {total_chars} znakov.")
    print(f"📄 Súbor: {OUTPUT_FILE}")

if __name__ == "__main__":
    merge_project()