/* =========================================================================
   ISAS MASTER: ZÁKAZNÍK ZARIADENIA INLINE
   (Zlúčené: Live Filter + AJAX SweetAlert2 Delete + Drawer + Filter kategórií)
   ========================================================================= */
document.addEventListener("DOMContentLoaded", function () {
    console.log("ISAS Master pre Zariadenia - Inicializovaný");

    // 🛡️ Bezpečný wrapper pre sessionStorage chrániaci pred SecurityError v iFrame a Privacy módoch
    const safeStorage = {
        getItem: function (key) {
            try { return window.sessionStorage ? window.sessionStorage.getItem(key) : null; } catch (e) { return null; }
        },
        setItem: function (key, val) {
            try { if (window.sessionStorage) window.sessionStorage.setItem(key, val); } catch (e) {}
        },
        removeItem: function (key) {
            try { if (window.sessionStorage) window.sessionStorage.removeItem(key); } catch (e) {}
        }
    };

    // --- 0.0 KROK: ZABRÁNENIE NECHCENÉHO ODESLANIA FORMULÁRA KLÁVESOM ENTER ---
    document.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.keyCode === 13) {
            const activeEl = document.activeElement;
            if (activeEl && activeEl.tagName === "INPUT" && activeEl.type !== "submit" && activeEl.type !== "button") {
                e.preventDefault();
                return false;
            }
        }
    });

    const style = document.createElement('style');
    style.innerHTML = `

        /* NOVÝ KÓD: Animácia pre vysvietenie zariadenia po návrate */
        @keyframes isasDeviceHighlight {
            0% { background-color: rgba(255, 237, 213, 0.6); box-shadow: inset 0 0 0 3px #f97316, 0 0 20px rgba(249, 115, 22, 0.5); }
            100% { background-color: rgba(255, 237, 213, 0); box-shadow: inset 0 0 0 0 transparent, 0 0 0 transparent; }
        }
        .isas-device-flash {
            position: relative !important; /* Nutné pre absolútne pozicovanie overlayu */
            border-radius: 12px !important;
        }
        .isas-device-flash::after {
            content: "";
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            border-radius: 12px;
            pointer-events: none; /* Kliknutia prejdú skrz animáciu */
            animation: isasDeviceHighlight 2.5s ease-out forwards !important;
            z-index: 9999;
        }
    `;
    document.head.appendChild(style);

    // --- 1. ČASŤ: ÚPRAVA TLAČIDLA (SWEETALERT2 + AJAX) A HLAVIČKY ---
    function initDeleteAndHeaders() {
        const inlineGroup = document.querySelector('#servisovane_zariadenia-group, #servisovanezariadenie_set-group');
        if (!inlineGroup) return;

        // 1A. Úprava tlačidla na vymazanie (ignorujeme .empty-form šablónu Django formsetu)
        inlineGroup.querySelectorAll(".inline-related:not(.empty-form) .delete input[type='checkbox']").forEach(function (checkbox) {
            let row = checkbox.closest('.inline-related');
            if (!row || row.classList.contains('empty-form') || checkbox.dataset.isasInit) return;
            checkbox.dataset.isasInit = "true";

            checkbox.style.display = "none";

            if (checkbox.id) {
                let label = document.querySelector(`label[for='${checkbox.id}']`);
                if (label) label.style.display = "none";
            }

            let parentDelete = checkbox.closest('.delete');
            if (parentDelete) {
                parentDelete.childNodes.forEach(node => {
                    if (node.nodeType === Node.TEXT_NODE) node.textContent = "";
                });
            }

            let btn = document.createElement("button");
            btn.type = "button";
            btn.innerHTML = "🗑️ Odstrániť";
            Object.assign(btn.style, {
                backgroundColor: "#ef4444", color: "white", border: "none", padding: "8px 14px",
                borderRadius: "8px", cursor: "pointer", fontWeight: "bold", fontSize: "12px",
                boxShadow: "0 2px 4px rgba(239, 68, 68, 0.3)", transition: "all 0.2s",
                position: "relative", zIndex: "50", pointerEvents: "auto" // 🛡️ Poistka proti prekrytiu
            });

            btn.addEventListener("click", function (e) {
                e.preventDefault();
                e.stopPropagation(); // 🚀 KRITICKÝ FIX: Zabráni Djangu ukradnúť tento klik!

                let row = checkbox.closest('.inline-related');
                if (!row) return;
                let pkField = row.querySelector("input[name$='-id']");

                // 🛠️ Pomocná funkcia pre okamžité AJAX vymazanie a synchronizáciu Django Formsetu
                const executeDelete = () => {
                    if (row.dataset.isasDeleting === "true") return;
                    row.dataset.isasDeleting = "true";
                    const inlineGroup = row.closest('#servisovane_zariadenia-group, #servisovanezariadenie_set-group');
                    const reindexFormset = () => {
                        if (!inlineGroup) return;
                        const rows = inlineGroup.querySelectorAll('.inline-related:not(.empty-form)');
                        const totalForms = inlineGroup.querySelector("input[name$='-TOTAL_FORMS']");
                        const initialForms = inlineGroup.querySelector("input[name$='-INITIAL_FORMS']");
                        if (totalForms) totalForms.value = rows.length;
                        if (initialForms) {
                            let initialCount = 0;
                            rows.forEach(r => {
                                const idInput = r.querySelector("input[name$='-id']");
                                if (idInput && idInput.value && idInput.value.trim() !== "") {
                                    initialCount++;
                                }
                            });
                            initialForms.value = initialCount;
                        }
                        const prefix = totalForms ? totalForms.name.replace(/-TOTAL_FORMS$/, '') : null;
                        const prefixRegex = prefix ? new RegExp(`^${prefix}-\\d+-`) : /^.*-\d+-/;
                        const idPrefixRegex = prefix ? new RegExp(`^id_${prefix}-\\d+-`) : /^id_.*-\d+-/;

                        rows.forEach((r, idx) => {
                            r.querySelectorAll('input, select, textarea, label').forEach(el => {
                                if (el.name) el.name = el.name.replace(prefixRegex, `${prefix || 'form'}-${idx}-`);
                                if (el.id) el.id = el.id.replace(idPrefixRegex, `id_${prefix || 'form'}-${idx}-`);
                                if (el.htmlFor) el.htmlFor = el.htmlFor.replace(idPrefixRegex, `id_${prefix || 'form'}-${idx}-`);
                            });
                        });
                    };

                    if (pkField && pkField.value) {
                        btn.innerHTML = "⏳ Mažem...";
                        btn.style.pointerEvents = "none";

                        let csrfNode = document.querySelector('[name=csrfmiddlewaretoken]');
                        let csrfToken = csrfNode ? csrfNode.value : '';
                        if (!csrfToken) {
                            const cookieMatch = document.cookie.match(/csrftoken=([^;]+)/);
                            if (cookieMatch) csrfToken = cookieMatch[1];
                        }

                        fetch("/admin/Evidencia/sklad/ajax/delete-inline-final/", {
                            method: "POST",
                            headers: {
                                "X-Requested-With": "XMLHttpRequest",
                                "X-CSRFToken": csrfToken,
                                "Content-Type": "application/x-www-form-urlencoded"
                            },
                            body: "item_id=" + encodeURIComponent(pkField.value) + "&model_name=ServisovaneZariadenie"
                        })
                        .then(response => response.json().catch(() => ({ status: "error", message: "Neplatná odpoveď servera (HTTP " + response.status + ")." })).then(data => ({ ok: response.ok, status: response.status, data })))
                        .then(({ ok, status, data }) => {
                            if (ok && data && data.status === "ok") {
                                row.style.transition = "all 0.4s cubic-bezier(0.34, 1.56, 0.64, 1)";
                                row.style.opacity = "0";
                                row.style.transform = "scale(0.95)";
                                row.classList.add("isas-deleted"); 

                                setTimeout(() => { 
                                    row.remove(); 
                                    reindexFormset();
                                    applyCombinedFilter();
                                }, 400);

                                if (window.showISASToast) showISASToast('Zariadenie natrvalo vymazané z databázy', '✅', 'success');
                            } else {
                                row.dataset.isasDeleting = "false";
                                const chybovaSprava = (data && data.message) ? data.message : ("Chyba pri mazaní zariadenia (HTTP " + status + ").");
                                if (window.showISASToast) showISASToast(chybovaSprava, '❌', 'error');
                                btn.innerHTML = "🗑️ Odstrániť";
                                btn.style.pointerEvents = "auto";
                            }
                        })
                        .catch(err => {
                            row.dataset.isasDeleting = "false";
                            if (window.showISASToast) showISASToast("Nepodarilo sa spojiť so serverom.", '🔌', 'error');
                            btn.innerHTML = "🗑️ Odstrániť";
                            btn.style.pointerEvents = "auto";
                        });
                    } else {
                        row.remove();
                        reindexFormset();
                        applyCombinedFilter();
                    }
                };

                if (window.ISASModalEngine) {
                    window.ISASModalEngine({
                        maxWidth: "460px", icon: "🗑️", title: "Vymazať toto zariadenie?",
                        iconStyle: "background: #fef2f2; width: 85px; height: 85px; line-height: 85px; border-radius: 50%; margin: 0 auto 20px auto; border: 2px solid #fca5a5;",
                        text: `Zariadenie bude <strong>natrvalo odstránené</strong> z databázy zákazníka.<br><span style="color: #ef4444; font-size: 13px; font-weight: 700; margin-top: 6px; display: block;">⚠️ Táto akcia je nevratná!</span>`,
                        buttons: [
                            { id: "btn-cancel-del", text: "Zrušiť", style: "flex: 1; background: #94a3b8; color: white; border: none; padding: 12px 20px; border-radius: 10px; font-weight: 800; cursor: pointer; transition: all 0.2s; text-transform: uppercase;", onClick: (e, m) => m.close() },
                            { id: "btn-confirm-del", text: "Áno, natrvalo odstrániť", style: "flex: 2; background: #ef4444; color: white; border: none; padding: 12px 20px; border-radius: 10px; font-weight: 800; cursor: pointer; transition: all 0.2s; text-transform: uppercase; box-shadow: 0 4px 10px rgba(239,68,68,0.3);", onClick: (e, m) => { m.close(); executeDelete(); } }
                        ]
                    });
                } else {
                    if (confirm("Naozaj chcete toto zariadenie natrvalo odstrániť z databázy zákazníka?\n\n⚠️ Táto akcia je nevratná!")) {
                        executeDelete();
                    }
                }
            });

            checkbox.parentNode.appendChild(btn);
        });

        // 1B. Upratanie hlavičky (Flexbox layout + Odznak Rozbaliť + Pevné tlačidlo Vymazať)
        inlineGroup.querySelectorAll(".inline-related > h3, .inline-related > h2").forEach(function (h3) {
            let row = h3.closest('.inline-related');
            if (!row || row.classList.contains('empty-form') || h3.dataset.isasInit) return;
            h3.dataset.isasInit = "true";

            row.classList.add("isas-drawer");

            const pkInput = row.querySelector("input[name$='-id']");
            const isNewDevice = !pkInput || !pkInput.value || pkInput.value.trim() === "";

            let label = h3.querySelector(".inline_label");
            let link = h3.querySelector(".inlinechangelink");
            let deleteSpan = h3.querySelector(".delete");

            let cistyNazov = label ? label.textContent.replace("Servisované zariadenie:", "").replace("Zmeniť", "").trim() : (isNewDevice ? "Nové zariadenie" : "Otvoriť zariadenie");
            if (cistyNazov === "") cistyNazov = isNewDevice ? "Nové zariadenie" : "Otvoriť zariadenie";

            // Vytvorenie stabilnej Flexbox štruktúry v hlavičke
            h3.innerHTML = "";
            h3.className = "isas-inline-h3-flex";

            // Ľavá strana (Odznak Rozbaliť/Zbaliť + Odkaz na zariadenie)
            let leftBox = document.createElement("div");
            leftBox.className = "isas-h3-left-box";

            let toggleBadge = document.createElement("button");
            toggleBadge.type = "button";
            toggleBadge.className = "isas-toggle-badge" + (isNewDevice ? " active" : "");
            toggleBadge.innerHTML = isNewDevice ? "📂 Zbaliť" : "📂 Rozbaliť";

            if (isNewDevice) {
                row.classList.add("isas-opened");
            }

            leftBox.appendChild(toggleBadge);

            if (link) {
                let novyOdkaz = document.createElement("a");
                novyOdkaz.href = link.href;
                novyOdkaz.textContent = cistyNazov;
                novyOdkaz.className = "isas-device-title-link";

                // 🔴 ISAS CORE: ULOŽENIE ID ZARIADENIA DO SESSION STORAGE PRI KLIKNUTÍ
                novyOdkaz.addEventListener("click", function () {
                    let pkField = row.querySelector("input[name$='-id']");
                    if (pkField && pkField.value) {
                        safeStorage.setItem("isas_active_device_id", pkField.value);
                    }
                });

                leftBox.appendChild(novyOdkaz);
            } else {
                let novyText = document.createElement("span");
                novyText.textContent = cistyNazov;
                novyText.className = "isas-device-title-text";
                leftBox.appendChild(novyText);
            }

            // Pravá strana (Tlačidlo Vymazať pevne zarovnané doprava)
            let rightBox = document.createElement("div");
            rightBox.className = "isas-h3-right-box";
            if (deleteSpan) rightBox.appendChild(deleteSpan);

            h3.appendChild(leftBox);
            h3.appendChild(rightBox);

            // Kliknutie VÝHRADNE na tlačidlo/odznak prepína Rozbaliť / Zbaliť
            toggleBadge.addEventListener("click", function (e) {
                e.preventDefault();
                e.stopPropagation();
                row.classList.toggle("isas-opened");
                let isOpen = row.classList.contains("isas-opened");
                toggleBadge.innerHTML = isOpen ? "📂 Zbaliť" : "📂 Rozbaliť";
                toggleBadge.classList.toggle("active", isOpen);

                if (isOpen) window.dispatchEvent(new Event('resize'));
            });
        });
    }

    // --- POMOCNÁ FUNKCIA: KOMBINATÓRNE FILTROVANIE (KATEGÓRIA + VYHĽADÁVANIE + URL SYNC) ---
    function applyCombinedFilter() {
        const inlineGroup = document.querySelector('#servisovane_zariadenia-group, #servisovanezariadenie_set-group');
        if (!inlineGroup) return;

        const searchInput = inlineGroup.querySelector("#live-inline-search");
        const term = searchInput ? searchInput.value.toLowerCase().trim() : "";

        const filterBar = inlineGroup.querySelector('.isas-cat-filter-bar');
        const activePills = filterBar ? filterBar.querySelectorAll('.isas-cat-pill.active:not([data-value="all"])') : [];
        const selectedCats = Array.from(activePills).map(p => (p.dataset.value || '').toLowerCase()).filter(Boolean);
        const isAllActive = filterBar ? filterBar.querySelector('[data-value="all"]')?.classList.contains('active') : false;

        // 🛡️ ISAS FIX: Bezpečná synchronizácia URL stavu bez rušenia textového filtra
        if (filterBar) {
            try {
                const newUrl = new URL(window.location.href);
                if (selectedCats.length === 0 && !isAllActive) {
                    newUrl.searchParams.delete('kat');
                } else if (isAllActive) {
                    newUrl.searchParams.set('kat', 'all');
                } else {
                    newUrl.searchParams.set('kat', selectedCats.join(','));
                }
                window.history.replaceState({}, '', newUrl);
            } catch (e) {
                // Tichý fallback v striktne obmedzenom sandboxe
            }
        }

        const rows = inlineGroup.querySelectorAll('.inline-related:not(.empty-form)');

        rows.forEach(row => {
            if (row.classList.contains("isas-deleted")) return;

            // 1. Kategóriový zápas
            const rCat = (row.dataset.isasCategory || '').toLowerCase();
            let categoryMatch = false;

            if (isAllActive) {
                categoryMatch = true;
            } else if (selectedCats.length > 0) {
                categoryMatch = selectedCats.includes(rCat);
            } else {
                // Ak nie je zaškrtnutá žiadna kategória ani "Všetky", hľadáme len vtedy, ak píše text
                categoryMatch = term.length > 0;
            }

            // 2. Vyhľadávací zápas
            let searchMatch = true;
            if (term.length > 0) {
                let textToSearch = (row.textContent || '').toLowerCase();
                row.querySelectorAll("input[type='text'], input[type='number'], textarea").forEach(inp => {
                    if (inp && inp.value) {
                        textToSearch += " " + String(inp.value).toLowerCase();
                    }
                });
                searchMatch = textToSearch.includes(term);
            }

            // Výsledné striktné zobrazenie: nové neuložené riadky ponechávame viditeľné, aby používateľovi nezmizol otvorený formulár
            const pkField = row.querySelector("input[name$='-id']");
            const isNewRow = !pkField || !pkField.value || pkField.value.trim() === "";

            if ((isNewRow || categoryMatch) && searchMatch) {
                row.style.display = "";
            } else {
                row.style.display = "none";
            }
        });
    }

    // --- 2. ČASŤ: LIVE FILTER (Prepojený s Kategóriami) ---
    function initLiveSearch() {
        const inlineGroup = document.querySelector('#servisovane_zariadenia-group, #servisovanezariadenie_set-group');
        if (!inlineGroup || inlineGroup.querySelector(".isas-live-search")) return;

        const header = inlineGroup.querySelector("h2");
        if (!header) return;

        const wrapper = document.createElement("div");
        wrapper.className = "isas-live-search";

        wrapper.innerHTML = `
            <div class="isas-search-inner">
                <span class="isas-search-icon">🔍</span>
                <input type="text" id="live-inline-search" placeholder="Hľadať ID, Model, Sériové číslo..." autocomplete="off">
            </div>
        `;

        header.appendChild(wrapper);

        const input = wrapper.querySelector("#live-inline-search");
        input.addEventListener("focus", () => input.style.borderColor = "#6366f1");
        input.addEventListener("blur", () => input.style.borderColor = "#e2e8f0");

        input.addEventListener("input", function() {
            applyCombinedFilter();
        });
    }

    // --- 3. ČASŤ: AI ŠEPKÁR ---
    function initAISepkar() {
        if (!document.getElementById('isas-ai-modal-styles')) {
            const style = document.createElement('style');
            style.id = 'isas-ai-modal-styles';
            style.innerHTML = `
                .isas-ai-list { list-style: none !important; padding: 0 !important; margin: 0 !important; }
                .isas-ai-list li { list-style: none !important; position: relative; padding-left: 40px; margin-bottom: 24px; font-weight: 500; }
                .isas-ai-list li::before { content: '⚡'; position: absolute; left: 0; top: 2px; font-size: 14px; background: #eff6ff; color: #3b82f6; width: 26px; height: 26px; display: flex; align-items: center; justify-content: center; border-radius: 8px; border: 1px solid #dbeafe; }
                .isas-ai-list li strong { color: #0f172a; font-weight: 700; }
            `;
            document.head.appendChild(style);
        }

        document.body.addEventListener('click', function(e) {
            let btn = e.target.closest('.btn-isas-ai-inline, button[data-zariadenie-id], a[data-zariadenie-id]');
            if (!btn) return;
            
            e.preventDefault();
            let zariadenieId = btn.getAttribute('data-zariadenie-id');
            if (!zariadenieId || btn.classList.contains('is-processing')) return;

            let textSpan = btn.querySelector('.ai-btn-text');
            let povodnyText = textSpan ? textSpan.innerHTML : 'Spustiť AI Analýzu';

            btn.classList.add('is-processing');
            btn.style.opacity = '0.7';
            if (textSpan) textSpan.innerHTML = '⏳ Analyzujem...';

            fetch('/admin/Evidencia/servisnyzaznam/ai-sepkar/' + encodeURIComponent(zariadenieId) + '/')
            .then(response => response.json().catch(() => ({ status: "error", message: "Neplatná odpoveď servera (HTTP " + response.status + ")." })).then(data => ({ ok: response.ok, status: response.status, data })))
            .then(({ ok, status, data }) => {
                if (ok && data && data.status === 'success' && typeof data.message === 'string') {
                    const escapeHtml = (str) => String(str || '')
                        .replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;')
                        .replace(/'/g, '&#039;');

                    let riadky = data.message.split('\n').filter(line => line.trim().length > 0);
                    let listHtml = '<ul class="isas-ai-list">';
                    let maOdrazky = false;
                    
                    riadky.forEach(riadok => {
                        if (riadok.trim().startsWith('-')) {
                            maOdrazky = true;
                            let cistyText = escapeHtml(riadok.replace(/^-?\s*/, '').replace(/\*\*/g, ''));
                            listHtml += '<li>' + cistyText + '</li>';
                        }
                    });
                    listHtml += '</ul>';

                    if (!maOdrazky) {
                        listHtml = '<p style="margin:0; font-weight:500;">' + escapeHtml(data.message.replace(/\*\*/g, '')).replace(/\n/g, '<br>') + '</p>';
                    }

                    if (window.ISASModalEngine) {
                        window.ISASModalEngine({
                            maxWidth: "680px", icon: false, title: false, text: false, showClose: false,
                            customHtml: `<div style="background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); padding: 30px; text-align: center; border-top-left-radius: 24px; border-top-right-radius: 24px; margin: -35px -40px 20px -40px;"><h2 style="font-size: 24px; font-weight: 800; margin: 0; color: #ffffff; display: flex; align-items: center; justify-content: center; gap: 12px;"><span style="font-size: 28px;">🧠</span> ISAS AI: Diagnostika zariadenia</h2></div><div style="text-align: left; font-size: 15px; color: #475569; line-height: 1.8;">${listHtml}</div>`,
                            buttons: [{ id: "btn-ai-close", text: "Rozumiem", style: "background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); color: white; border: none; padding: 14px 45px; border-radius: 14px; font-size: 15px; font-weight: 700; cursor: pointer; transition: all 0.3s; box-shadow: 0 10px 20px -5px rgba(37, 99, 235, 0.4);", onClick: (e, m) => m.close() }]
                        });
                    } else if (window.showISASToast) {
                        showISASToast(data.message.replace(/\*\*/g, ''), '🤖', 'info');
                    } else {
                        alert(data.message.replace(/\*\*/g, ''));
                    }
                } else {
                    const sprava = (data && data.message) ? data.message : ("AI asistent nie je dostupný (HTTP " + status + ").");
                    if(window.showISASToast) showISASToast(sprava, '⚠️', 'warning');
                }
            })
            .catch(err => {
                if(window.showISASToast) showISASToast('Nepodarilo sa spojiť so serverom ISAS.', '❌', 'error');
            })
            .finally(() => {
                btn.classList.remove('is-processing');
                btn.style.opacity = '1';
                if (textSpan) textSpan.innerHTML = povodnyText;
            });
        });
    }

    // --- 4. ČASŤ: DYNAMICKÝ KATEGÓRIOVÝ FILTER (ZOBRAZENIE AŽ PO KLIKNUTÍ + MULTI-SELECT) ---
    function initCategoryFilterBar() {
        const group = document.querySelector('#servisovane_zariadenia-group, #servisovanezariadenie_set-group');
        if (!group || group.querySelector('.isas-cat-filter-bar')) return;

        const header = group.querySelector('h2');
        if (!header) return;

        const rows = group.querySelectorAll('.inline-related:not(.empty-form)');
        if (rows.length === 0) return;

        // Pomocná funkcia pre čisté získanie IBA názvu kategórie z riadka (bez zbytočných JS skriptov a štítkov)
        function extractCategoryName(row) {
            const dataNode = row.querySelector('.isas-category-data-node[data-category-key]');
            if (dataNode && dataNode.dataset.categoryKey) {
                return dataNode.dataset.categoryKey.trim().toLowerCase();
            }

            let katEl = row.querySelector('.field-get_kategoria_zariadenia .readonly, .field-kategoria .readonly');
            if (katEl) {
                let raw = katEl.textContent.replace(/^Kategória:\s*/i, '').replace(/[\p{Extended_Pictographic}\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, '').trim();
                let clean = raw.split('(')[0].trim().toLowerCase();
                if (clean && clean !== '➖' && clean !== '-') {
                    return clean;
                }
            }

            return 'ostatne';
        }

        const categoriesMap = new Map(); // Kategória -> Pole zariadení zákazníka
        rows.forEach(row => {
            const catName = extractCategoryName(row);
            row.dataset.isasCategory = catName.toLowerCase();
            
            if (!categoriesMap.has(catName)) {
                categoriesMap.set(catName, []);
            }
            categoriesMap.get(catName).push(row);
        });

        if (categoriesMap.size === 0) return;

        const filterBar = document.createElement('div');
        filterBar.className = 'isas-cat-filter-bar';

        const urlParams = new URLSearchParams(window.location.search);
        const rawKatUrl = urlParams.get('kat');
        const activeCategoriesFromUrl = rawKatUrl ? rawKatUrl.toLowerCase().split(',').map(s => s.trim()).filter(Boolean) : [];

        // HNEĎ NA ZAČIATKU SKRYJEME EXISTUJÚCE ZARIADENIA (Nové neuložené formuláre ponecháme viditeľné)
        rows.forEach(r => {
            const pkField = r.querySelector("input[name$='-id']");
            const isNewRow = !pkField || !pkField.value || pkField.value.trim() === "";
            if (!isNewRow) {
                r.style.display = 'none';
            } 
        });

        // Pomocná funkcia na skrášlenie kľúčov kategórií (dynamicky z dátového uzla alebo normalizáciou kľúča)
        function humanizeCatName(raw) {
            if (!raw) return 'Všeobecné';
            if (raw.toLowerCase() === 'ostatne') return 'Ostatné zariadenia';

            const dataNode = document.querySelector(`.isas-category-data-node[data-category-key="${raw}"]`);
            if (dataNode && dataNode.dataset.categoryLabel) {
                let cleanLabel = dataNode.dataset.categoryLabel.replace(/[\p{Extended_Pictographic}\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, '').trim();
                let shortName = cleanLabel.split('(')[0].trim();
                if (shortName) return shortName;
            }

            const cleanKey = raw.replace(/_/g, ' ').trim();
            return cleanKey.charAt(0).toUpperCase() + cleanKey.slice(1);
        }

        // 1. PRVÉ: Dynamické tlačidlá konkrétnych kategórií zákazníka
        categoriesMap.forEach((catRows, catName) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.dataset.value = catName.toLowerCase();
            
            const isCatActive = activeCategoriesFromUrl.includes(catName.toLowerCase());
            btn.className = 'isas-cat-pill' + (isCatActive ? ' active' : '');
            btn.textContent = `📂 ${humanizeCatName(catName)} (${catRows.length})`;

            btn.addEventListener('click', function () {
                const allBtn = filterBar.querySelector('[data-value="all"]');
                if (allBtn) allBtn.classList.remove('active');
                btn.classList.toggle('active');

                applyCombinedFilter();
            });
            filterBar.appendChild(btn);
        });

        // 2. POSLEDNÉ: Tlačidlo "📦 Zobraziť všetky" až na konci
        const allBtn = document.createElement('button');
        allBtn.type = 'button';
        allBtn.dataset.value = 'all';
        allBtn.className = 'isas-cat-pill' + (activeCategoriesFromUrl.includes('all') ? ' active' : '');
        allBtn.textContent = `📦 Zobraziť všetky (${rows.length})`;
        
        allBtn.addEventListener('click', function () {
            const willBeActive = !allBtn.classList.contains('active');
            filterBar.querySelectorAll('.isas-cat-pill').forEach(p => p.classList.remove('active'));
            if (willBeActive) {
                allBtn.classList.add('active');
            }
            applyCombinedFilter();
        });
        filterBar.appendChild(allBtn);

        header.after(filterBar);

        // Pri načítaní z URL aplikujeme kombinovaný filter, ktorý rešpektuje kategórie aj hľadanie
        if (activeCategoriesFromUrl.length > 0) {
            applyCombinedFilter();
        }
    }

    initDeleteAndHeaders();
    initLiveSearch();
    initAISepkar();
    initCategoryFilterBar();

    const handleNewRowAdded = () => {
        setTimeout(() => {
            initDeleteAndHeaders();
            applyCombinedFilter();
        }, 300);
    };

    document.body.addEventListener("click", (e) => {
        if (e.target && (e.target.classList.contains("add-row") || e.target.closest('.add-row'))) {
            handleNewRowAdded();
        }
    });

    if (window.django && window.django.jQuery) {
        window.django.jQuery(document).on('formset:added', handleNewRowAdded);
    }

    // =========================================================================
    // 🔴 5. ČASŤ: DUÁLNY ENGINE PRE AUTOMATICKÉ ROZBALENIE A SCROLL
    // (Kombinuje sessionStorage pamäť + URL Hash fallback)
    // =========================================================================
    function highlightAndExpandDevice() {
        let deviceId = null;
        const hash = window.location.hash;

        // 1. Primárne skontrolujeme URL hash
        if (hash && hash.startsWith('#highlight-device-')) {
            deviceId = hash.replace('#highlight-device-', '');
        } else {
            // 2. Sekundárne skontrolujeme SessionStorage pamäť
            deviceId = safeStorage.getItem("isas_active_device_id");
            if (deviceId === "null" || deviceId === "undefined" || (typeof deviceId === "string" && !deviceId.trim())) {
                deviceId = null;
            }
        }

        if (!deviceId) return;

        const inlineGroup = document.querySelector('#servisovane_zariadenia-group, #servisovanezariadenie_set-group');
        if (!inlineGroup) return;

        const rows = inlineGroup.querySelectorAll('.inline-related:not(.empty-form)');
        let targetRow = null;

        for (let i = 0; i < rows.length; i++) {
            const idInput = rows[i].querySelector('input[name$="-id"]');
            if (idInput && String(idInput.value) === String(deviceId)) {
                targetRow = rows[i];
                break;
            }
        }

        if (!targetRow) return;

        // Pamäť vyčistíme AŽ PO TOM, čo sa riadok reálne potvrdil v DOM (ochrana pred race condition pri pomalom renderovaní)
        if (!hash) {
            safeStorage.removeItem("isas_active_device_id");
        }

        // A. Aktivujeme kategóriu zariadenia
        const catName = targetRow.dataset.isasCategory;
        if (catName) {
            const catPills = document.querySelectorAll('.isas-cat-filter-bar .isas-cat-pill');
            const catPill = Array.from(catPills).find(
                p => (p.dataset.value || '').toLowerCase() === catName.toLowerCase()
            );
            if (catPill && !catPill.classList.contains('active')) {
                catPill.click();
            }
        }

        // B. Natvrdo zobraziť riadok
        targetRow.style.display = '';

        // C. Rozbaliť Drawer
        if (!targetRow.classList.contains('isas-opened')) {
            targetRow.classList.add('isas-opened');
            const badge = targetRow.querySelector('.isas-toggle-badge');
            if (badge) {
                badge.innerHTML = "📂 Zbaliť";
                badge.classList.add("active");
            }
        }

        // D. Vykonáme plynulý scroll a spustíme animáciu
        setTimeout(() => {
            targetRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
            targetRow.classList.add('isas-device-flash');

            if (window.location.hash) {
                try {
                    history.replaceState(null, null, window.location.pathname + window.location.search);
                } catch (e) {}
            }

            setTimeout(() => {
                targetRow.classList.remove('isas-device-flash');
            }, 3000);
        }, 300);
    }

    // Spustíme hneď a poistíme po kompletnom načítaní okna
    setTimeout(highlightAndExpandDevice, 200);
    window.addEventListener('load', function() {
        setTimeout(highlightAndExpandDevice, 300);
    });
});