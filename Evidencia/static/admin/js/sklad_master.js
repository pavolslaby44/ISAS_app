// ==========================================
// 1. GLOBÁLNA RÝCHLA NAVIGÁCIA A DETEKTÍV (SaaS DASHBOARD)
// ==========================================
const initSkladDashboard = function() {
    const isAddPage = window.location.pathname.endsWith('/add/');
    if (isAddPage) return; 

    // Defenzívna poistka: spúšťať výhradne v rozhraní Skladu, aby sa neovplyvnili iné modely s poľom nazov
    const isSkladAdmin = window.ISAS_SKLAD_CONFIG || 
        (document.body && document.body.classList.contains("model-sklad")) || 
        (document.body && document.body.classList.contains("model-aktualnenasklade")) || 
        window.location.pathname.includes("/admin/Evidencia/sklad/");
    if (!isSkladAdmin || !document.querySelector("#id_nazov")) return;

    const nazovEl = document.querySelector("#id_nazov");
    const fieldset = nazovEl?.closest("fieldset") || nazovEl?.closest(".form-row") || nazovEl?.closest("form");
    const urlParams = new URLSearchParams(window.location.search);
    const kat = urlParams.get('kat');
    const sub = urlParams.get('sub');

    // Identifikovaný štýl pre bezpečné pridávanie a odoberanie medzi úrovňami dashboardu
    const hideStyleId = "isas-sklad-hide-submit-style";
    let existingHideStyle = document.getElementById(hideStyleId);

    if (!(kat && sub)) {
        if (!existingHideStyle) {
            let hideSubmitStyle = document.createElement("style");
            hideSubmitStyle.id = hideStyleId;
            hideSubmitStyle.innerHTML = `
                html body #content-main .submit-row, 
                html body .submit-row { 
                    display: none !important; 
                    visibility: hidden !important; 
                    opacity: 0 !important; 
                    height: 0 !important; 
                    margin: 0 !important; 
                    padding: 0 !important; 
                    overflow: hidden !important; 
                    pointer-events: none !important; 
                }
            `;
            document.documentElement.appendChild(hideSubmitStyle);
        }
    } else if (existingHideStyle) {
        // Na 3. úrovni (tabuľka položiek) musí byť tlačidlo Uložiť plne dostupné
        existingHideStyle.remove();
    }

    // 🟢 ISAS: Skryjeme detektíva hneď na začiatku (pre 1. a 2. úroveň)
    let detektiv = document.getElementById('floating-sn-search-container');
    if(detektiv) detektiv.style.setProperty('display', 'none', 'important');

    if (fieldset && !document.querySelector(".global-search-wrapper")) {
        let wrapper = document.createElement("div");
        wrapper.classList.add("global-search-wrapper");
        
        Object.assign(wrapper.style, {
            display: "flex", flexDirection: "column", gap: "20px", 
            margin: "10px 0 30px 0", width: "100%"
        });

        if (typeof window.ISAS_SKLAD_CONFIG === "string") {
            try { window.ISAS_SKLAD_CONFIG = JSON.parse(window.ISAS_SKLAD_CONFIG); } catch (e) { window.ISAS_SKLAD_CONFIG = {}; }
        }

        const config = (window.ISAS_SKLAD_CONFIG && window.ISAS_SKLAD_CONFIG.hierarchy) ? window.ISAS_SKLAD_CONFIG.hierarchy : {};
        if (Object.keys(config).length === 0 && existingHideStyle) {
            existingHideStyle.remove();
        }

        // 🎨 DYNAMICKÁ INTELIGENTNÁ CYKLÁCIA FARIEB
        const colorThemes = [
            { bg: 'linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%)', text: '#1e40af', shadow: 'rgba(59, 130, 246, 0.2)' },
            { bg: 'linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%)', text: '#065f46', shadow: 'rgba(16, 185, 129, 0.2)' },
            { bg: 'linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%)', text: '#991b1b', shadow: 'rgba(239, 68, 68, 0.2)' },
            { bg: 'linear-gradient(135deg, #faf5ff 0%, #f3e8ff 100%)', text: '#581c87', shadow: 'rgba(168, 85, 247, 0.2)' },
            { bg: 'linear-gradient(135deg, #fff7ed 0%, #ffedd5 100%)', text: '#9a3412', shadow: 'rgba(249, 115, 22, 0.2)' }
        ];

        const escapeStr = (str) => {
            return (str !== null && str !== undefined ? str : "").toString().replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
        };

        function createBackButton(text, url) {
            let topBarContainer = document.createElement("div");
            Object.assign(topBarContainer.style, { width: "100%", display: "flex", justifyContent: "flex-start" });

            let safeText = escapeStr(text);
            let backBtn = document.createElement("a");
            backBtn.innerHTML = `<span style="margin-right: 8px; font-size: 16px;">⬅</span> ${safeText}`;
            backBtn.href = url;
            
            Object.assign(backBtn.style, {
                background: "linear-gradient(135deg, #1e293b 0%, #0f172a 100%)", color: "#ffffff", padding: "12px 24px", borderRadius: "12px", 
                textDecoration: "none", fontWeight: "900", fontSize: "12px", textTransform: "uppercase", display: "inline-flex", alignItems: "center",
                boxShadow: "0 6px 15px rgba(15, 23, 42, 0.3)", transition: "all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)",
                border: "1px solid rgba(255,255,255,0.1)", letterSpacing: "0.5px", height: "max-content"
            });
            
            backBtn.addEventListener("mouseover", () => { backBtn.style.transform = "translateY(-3px)"; backBtn.style.boxShadow = "0 10px 20px rgba(15, 23, 42, 0.4)"; backBtn.style.background = "linear-gradient(135deg, #0f172a 0%, #000000 100%)"; });
            backBtn.addEventListener("mouseout", () => { backBtn.style.transform = "translateY(0)"; backBtn.style.boxShadow = "0 6px 15px rgba(15, 23, 42, 0.3)"; backBtn.style.background = "linear-gradient(135deg, #1e293b 0%, #0f172a 100%)";});
            
            topBarContainer.appendChild(backBtn);
            return topBarContainer;
        }

        if (!kat) {
            // --- 1. ÚROVEŇ (Hlavné kategórie) ---
            // (Skrývanie submit-row je už vyriešené globálnym CSS na začiatku skriptu)

            let cardsWrapper = document.createElement("div");
            Object.assign(cardsWrapper.style, { display: "flex", gap: "15px", flexWrap: "wrap", justifyContent: "center", width: "100%" });

            function createBigCard(label, bgGradient, textColor, paramValue, icon, shadowRgba) {
                let a = document.createElement("a");
                let safeLabel = escapeStr(label);
                let safeIcon = escapeStr(icon);
    
                a.href = window.location.pathname + "?kat=" + encodeURIComponent(paramValue);
                a.innerHTML = `
                    <div style="font-size: 50px; margin-bottom: 12px; filter: drop-shadow(0 6px 10px rgba(0,0,0,0.15));">${safeIcon}</div>
                    <div style="font-size: 16px; font-weight: 900; text-transform: uppercase; letter-spacing: 0.5px;">${safeLabel}</div>
                `;
                Object.assign(a.style, {
                    background: bgGradient, color: textColor, padding: "40px 20px", borderRadius: "24px", cursor: "pointer", 
                    textDecoration: "none", textAlign: "center", boxShadow: `0 15px 30px ${shadowRgba}`, border: `1px solid rgba(255,255,255,0.6)`,
                    transition: "all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)", flex: "1", minWidth: "280px", maxWidth: "380px",
                    display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center"
                });
    
                let shadowHover = (typeof shadowRgba === "string" && shadowRgba.includes('0.2)'))
                ? shadowRgba.replace('0.2)', '0.4)')
                : (typeof shadowRgba === "string" ? shadowRgba : "rgba(0, 0, 0, 0.4)");
                a.addEventListener("mouseover", () => { a.style.transform = "translateY(-8px) scale(1.02)"; a.style.boxShadow = `0 20px 40px ${shadowHover}`; });
                a.addEventListener("mouseout", () => { a.style.transform = "translateY(0) scale(1)"; a.style.boxShadow = `0 15px 30px ${shadowRgba}`; });
                return a;
            }

            Object.keys(config).forEach((mainKatName, idx) => {
                let catData = config[mainKatName] || {};
                let theme = colorThemes[idx % colorThemes.length];
                
                let rawTitle = String(catData.nazov || catData.verbose_name || catData.label || mainKatName || "");
                let displayIcon = catData.ikona || "";
                let displayTitle = rawTitle;

                // Ak ikona nie je nastavená a názov z CATEGORY_CHOICES začína emoji, oddelíme ju do 50px ikony
                let emojiMatch = null;
                try {
                    const emojiRegex = new RegExp('^(\\p{Extended_Pictographic}+)\\s+(.*)$', 'u');
                    emojiMatch = rawTitle.match(emojiRegex);
                } catch (e) {
                    emojiMatch = null;
                }
                if (emojiMatch) {
                    if (!displayIcon) displayIcon = emojiMatch[1];
                    displayTitle = emojiMatch[2];
                } else if (!displayIcon) {
                    displayIcon = "📦";
                }

                cardsWrapper.appendChild(createBigCard(
                    displayTitle, 
                    theme.bg, 
                    theme.text, 
                    mainKatName, 
                    displayIcon, 
                    theme.shadow
                ));
            });
            
            wrapper.appendChild(cardsWrapper);
        } 
        else if (kat && !sub) {
            // --- 2. ÚROVEŇ (Podkategórie) ---
            // (Skrývanie submit-row je už vyriešené globálnym CSS na začiatku skriptu)

            wrapper.style.setProperty("display", "block", "important");
            wrapper.appendChild(createBackButton("Späť na hlavný výber kategórií", window.location.pathname));

            let gridContainer = document.createElement("div");
            Object.assign(gridContainer.style, { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: "15px", width: "100%", marginTop: "30px" });

            if (config[kat] && Array.isArray(config[kat].podkategorie)) {
                config[kat].podkategorie.forEach(item => {
                    if (!item) return;
                    let card = document.createElement("a");
                    let safeNazov = escapeStr(item.nazov);
                    let itemBg = escapeStr(item.bg_color || '#eff6ff');
                    let itemColor = escapeStr(item.text_color || '#1e40af');
                    let safeIkona = escapeStr(item.ikona || '📦');
                    
                    card.href = window.location.pathname + `?kat=${encodeURIComponent(kat)}&sub=${encodeURIComponent(item.kod !== null && item.kod !== undefined ? item.kod : '')}`;
                    card.innerHTML = `<span style="font-size: 26px; margin-right: 14px; display: flex; align-items: center; justify-content: center; background: ${itemBg}; width: 45px; height: 45px; border-radius: 10px; color: ${itemColor}; border: 1px solid rgba(0,0,0,0.05); box-shadow: inset 0 2px 4px rgba(255,255,255,0.5);">${safeIkona}</span> <span style="font-size: 14px; font-weight: 800; line-height: 1.3;">${safeNazov}</span>`;
                    Object.assign(card.style, {
                        background: "#ffffff", color: "#1e293b", padding: "16px 20px", borderRadius: "16px", border: "1px solid #e2e8f0",
                        display: "flex", alignItems: "center", textDecoration: "none", boxShadow: "0 4px 6px rgba(0,0,0,0.02)", transition: "all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1)"
                    });
                    card.addEventListener("mouseover", () => { card.style.borderColor = "#3b82f6"; card.style.boxShadow = "0 10px 20px rgba(59, 130, 246, 0.15)"; card.style.transform = "translateY(-4px)"; });
                    card.addEventListener("mouseout", () => { card.style.borderColor = "#e2e8f0"; card.style.boxShadow = "0 4px 6px rgba(0,0,0,0.02)"; card.style.transform = "translateY(0)"; });
                    gridContainer.appendChild(card);
                });
            }
            wrapper.appendChild(gridContainer);
        }
        else {
            // --- 3. ÚROVEŇ (Samotná tabuľka položiek) ---
            
            // 🟢 ISAS: ZAPNUTIE DETEKTÍVA IBA TU!
            if(detektiv) detektiv.style.setProperty('display', 'flex', 'important');

            wrapper.appendChild(createBackButton("Späť na výber podkategórií", window.location.pathname + `?kat=${encodeURIComponent(kat)}`));

            let titleName = "Položky";
            let subIcon = "📦";
            let headerBg = "linear-gradient(135deg, #1e293b 0%, #0f172a 100%)"; 
            let headerText = "#ffffff";

            if (config[kat]) {
                let catIndex = Object.keys(config).indexOf(kat);
                if (catIndex !== -1) {
                    let theme = colorThemes[catIndex % colorThemes.length];
                    headerBg = theme.bg;
                    headerText = theme.text;
                }

                let subData = (Array.isArray(config[kat].podkategorie)) ? config[kat].podkategorie.find(p => p && String(p.kod) === String(sub)) : null;
                if (subData) {
                    titleName = subData.nazov || titleName;
                    subIcon = subData.ikona || subIcon;
                }
            }

            let style = document.createElement("style");
            style.innerHTML = `
                .inline-group > h2, .inline-group > .description,
                .djn-group-root > h2, .djn-group-root > fieldset > h2, .djn-group-root > .djn-fieldset > h2,
                .djn-item:not(.isas-drawer) > h3, .djn-drag-handler, .djn-no-drag,
                .inline-related:not(.isas-drawer) > h3, .djn-level-1 { display: none !important; }
                .inline-group, .djn-item { background: transparent !important; box-shadow: none !important; border: none !important; padding: 0 !important; margin: 0 !important; }
                .sn-search-btn { display: none !important; }
                .isas-drawer:not(.isas-opened) > h2, .isas-drawer:not(.isas-opened) > h3, .isas-drawer:not(.isas-opened) > .djn-module > h2 {
                    border-radius: 16px !important;
                }
                .isas-drawer.isas-opened > h2, .isas-drawer.isas-opened > h3, .isas-drawer.isas-opened > .djn-module > h2 {
                    border-radius: 16px 16px 0 0 !important;
                }
                .djn-table tr.add-row td, .tabular table tr.add-row td {
                    display: table-cell !important;
                    text-align: center !important;
                    vertical-align: middle !important;
                    padding: 25px 0 !important; 
                }
                .djn-add-item a, .djn-table tr.add-row td.djn-add-item a.djn-add-handler {
                    margin: 0 auto !important;
                    display: inline-flex !important;
                    justify-content: center !important;
                    min-width: 250px !important; 
                }
                
                /* 🛑 TRVALÉ SKRYTIE MODÁLOVÝCH HLAVIČIEK NA 3. ÚROVNI */
                th[class*="dodavatel"], th[class*="vyrobca"], th[class*="typ_zasoby"],
                th[class*="katalogove_cislo"], th[class*="ean"], th[class*="sadzba_dph"],
                th[class*="obchodny_nazov"], th[class*="skryt_vyrobcu"], th[class*="kto_som"],
                th.isas-hidden-header, td.isas-placeholder {
                    display: none !important;
                    width: 0 !important;
                    min-width: 0 !important;
                    max-width: 0 !important;
                    padding: 0 !important;
                    margin: 0 !important;
                    border: none !important;
                    visibility: hidden !important;
                    opacity: 0 !important;
                }
                
                /* 🟢 ISAS: POSUNUTIE DETEKTÍVA NIŽŠIE (Tu si môžeš ladiť pixely) */
                @media (min-width: 1025px) {
                    #floating-sn-search-container { bottom: 21px !important; right: 126px !important; }
                }
            `;
            document.head.appendChild(style);

            setTimeout(() => {
                let drawerHeader = document.querySelector('.isas-drawer > h2, .isas-drawer > h3, .isas-drawer > .djn-module > h2');
                if (drawerHeader) {
                    let safeTitleName = escapeStr(titleName);
                    let safeSubIcon = escapeStr(subIcon);
                    
                    drawerHeader.innerHTML = `<span style="margin-right: 12px; font-size: 22px; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.2));">${safeSubIcon}</span> ${safeTitleName}`;
                    Object.assign(drawerHeader.style, {
                        background: headerBg,
                        color: headerText,
                        border: "1px solid rgba(255,255,255,0.4)",
                        padding: "18px 24px",
                        fontSize: "16px",
                        fontWeight: "900",
                        textAlign: "left",
                        display: "flex",
                        alignItems: "center",
                        textTransform: "uppercase",
                        letterSpacing: "1px",
                        boxShadow: "0 4px 10px rgba(0,0,0,0.15)",
                        cursor: "pointer"
                    });
                }
                
                document.querySelectorAll('.isas-drawer').forEach(d => {
                    d.classList.remove('isas-opened');
                });
            }, 50);
        }

        fieldset.insertAdjacentElement("afterend", wrapper);
    }
};

if (document.readyState === 'loading') {
    document.addEventListener("DOMContentLoaded", initSkladDashboard);
} else {
    initSkladDashboard();
}


// ==================================================================
// 1C. ZAFARBOVANIE HLAVIČIEK (HIERARCHIA & KONTRAST)
// ==================================================================
const initHeaderColors = function() {
    const isSkladAdmin = window.ISAS_SKLAD_CONFIG || 
        (document.body && (document.body.classList.contains("model-sklad") || document.body.classList.contains("model-aktualnenasklade"))) || 
        window.location.pathname.includes("/admin/Evidencia/sklad/");
    if (!isSkladAdmin) return;

    // 🛡️ ARCHITEKTONICKÁ OCHRANA: V 3-úrovňovom sklade s drawerom riadi dizajn hlavičky výhradne initSkladDashboard.
    // Zamedzuje prepísaniu 16px zaobleného draweru na scvrknutý 11px pásik s ostrými hranami.
    if (document.querySelector(".isas-drawer") || document.querySelector(".global-search-wrapper")) {
        return;
    }

    const mainGroups = document.querySelectorAll(".inline-group");
    if (!mainGroups.length) return;

    if (typeof window.ISAS_SKLAD_CONFIG === "string") {
        try { window.ISAS_SKLAD_CONFIG = JSON.parse(window.ISAS_SKLAD_CONFIG); } catch (e) { window.ISAS_SKLAD_CONFIG = {}; }
    }
    const config = (window.ISAS_SKLAD_CONFIG && window.ISAS_SKLAD_CONFIG.hierarchy) ? window.ISAS_SKLAD_CONFIG.hierarchy : {};
    const catKeys = Object.keys(config);
    const urlKat = new URLSearchParams(window.location.search).get('kat');

    const headerThemes = [
        { parentBg: "rgba(219, 234, 254, 0.85)", parentText: "#1e40af", accentLine: "#3b82f6", subBgGrad: "linear-gradient(to right, rgba(239, 246, 255, 0.8) 0%, rgba(255, 255, 255, 0.5) 100%)", subTextColor: "#1e3a8a" },
        { parentBg: "rgba(209, 250, 229, 0.85)", parentText: "#065f46", accentLine: "#10b981", subBgGrad: "linear-gradient(to right, rgba(236, 253, 245, 0.8) 0%, rgba(255, 255, 255, 0.5) 100%)", subTextColor: "#064e3b" },
        { parentBg: "rgba(254, 226, 226, 0.85)", parentText: "#991b1b", accentLine: "#ef4444", subBgGrad: "linear-gradient(to right, rgba(254, 242, 242, 0.8) 0%, rgba(255, 255, 255, 0.5) 100%)", subTextColor: "#7f1d1d" },
        { parentBg: "rgba(250, 245, 255, 0.85)", parentText: "#581c87", accentLine: "#a855f7", subBgGrad: "linear-gradient(to right, rgba(243, 232, 255, 0.8) 0%, rgba(255, 255, 255, 0.5) 100%)", subTextColor: "#4a044e" },
        { parentBg: "rgba(255, 247, 237, 0.85)", parentText: "#9a3412", accentLine: "#f97316", subBgGrad: "linear-gradient(to right, rgba(254, 237, 213, 0.8) 0%, rgba(255, 255, 255, 0.5) 100%)", subTextColor: "#7c2d12" }
    ];

    mainGroups.forEach((group, idx) => {
        const mainH2 = group.querySelector("h2");
        if (!mainH2) return;

        let themeIndex = -1;
        if (urlKat && catKeys.includes(urlKat)) {
            themeIndex = catKeys.indexOf(urlKat);
        } else {
            const titleUpper = (mainH2.textContent || "").toUpperCase();
            themeIndex = catKeys.findIndex(k => titleUpper.includes(k.toUpperCase()) || k.toUpperCase().includes(titleUpper));
        }
        if (themeIndex === -1) {
            themeIndex = idx;
        }

        const theme = headerThemes[themeIndex % headerThemes.length];
        const parentBg = theme.parentBg;
        const parentText = theme.parentText;
        const accentLine = theme.accentLine;
        const subBgGrad = theme.subBgGrad;
        const subTextColor = theme.subTextColor;

        if (parentBg) {
            mainH2.style.setProperty("background", parentBg, "important");
            mainH2.style.setProperty("backdrop-filter", "blur(10px)", "important");
            mainH2.style.setProperty("color", parentText, "important");
            mainH2.style.padding = "18px 25px";
            mainH2.style.borderRadius = "16px 16px 0 0";
            mainH2.style.fontSize = "15px";
            mainH2.style.fontWeight = "900";
            mainH2.style.textTransform = "uppercase";
            mainH2.style.border = "none";
        }

        if (subBgGrad) {
            // 🛡️ OCHRANA DRAWERU: Formátujeme len štandardné inlines, nikdy nie .isas-drawer hlavičky
            const subHeaders = group.querySelectorAll(".inline-related:not(.isas-drawer) h2");
            subHeaders.forEach(subH2 => {
                subH2.style.setProperty("background", subBgGrad, "important");
                subH2.style.setProperty("backdrop-filter", "blur(10px)", "important");
                subH2.style.setProperty("color", subTextColor, "important");
                subH2.style.setProperty("border-left", `5px solid ${accentLine}`, "important");
                subH2.style.setProperty("border-bottom", `1px solid rgba(226, 232, 240, 0.5)`, "important");
                subH2.style.setProperty("padding", "10px 20px", "important");
                subH2.style.setProperty("font-size", "11px", "important");
                subH2.style.setProperty("font-weight", "800", "important");
                subH2.style.setProperty("border-radius", "0", "important");
                subH2.style.textShadow = "none";
                subH2.style.boxShadow = "none";
            });
        }
    });
};

if (document.readyState === 'loading') {
    document.addEventListener("DOMContentLoaded", initHeaderColors);
} else {
    initHeaderColors();
}


// ==========================================
// 2. mazacie_tlacidlo.js (Dynamické modely!)
// ==========================================
(function() {
    const escapeStr = (str) => {
        return (str !== null && str !== undefined ? str : "").toString().replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
    };

    // 🛠️ Vyčlenená funkcia pre samotný fetch na backend (pre riadky uložené v DB)
    function executeDelete(itemId, modelName, row, pkFieldName) {
        if (!row || row.dataset.isasDeleting === "true" || !itemId || !modelName) return;
        row.dataset.isasDeleting = "true";

        let csrfNode = document.querySelector('[name=csrfmiddlewaretoken]');
        let csrfToken = csrfNode ? csrfNode.value : '';
        if (!csrfToken) {
            const cookieMatch = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
            if (cookieMatch) csrfToken = decodeURIComponent(cookieMatch[1]);
        }

        const formData = new URLSearchParams();
        formData.append('item_id', itemId);
        formData.append('model_name', modelName);
        if (csrfToken) formData.append('csrfmiddlewaretoken', csrfToken);

        const actionBtn = row.querySelector(".custom-delete-btn");
        if (actionBtn) {
            actionBtn.disabled = true;
            actionBtn.style.opacity = "0.5";
            actionBtn.style.cursor = "wait";
        }

        const deleteUrl = (function() {
            if (window.ISAS_SKLAD_CONFIG && window.ISAS_SKLAD_CONFIG.delete_url) {
                return window.ISAS_SKLAD_CONFIG.delete_url;
            }
            const match = window.location.pathname.match(/^(.*\/Evidencia\/sklad\/)/);
            return (match ? match[1] : "/admin/Evidencia/sklad/") + "ajax/delete-inline-final/";
        })();

        fetch(deleteUrl, {
            method: "POST",
            headers: { 
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": csrfToken,
                "Content-Type": "application/x-www-form-urlencoded"
            },
            body: formData.toString()
        })

        .then(response => {
            return response.json().catch(() => ({})).then(data => ({
                ok: response.ok,
                status: response.status,
                data: data
            }));
        })
        .then(({ ok, status, data }) => {
            const isSuccess = ok && (data.status === "ok" || data.status === "success");
            const isAlreadyGone = (status === 404);

            if (isSuccess || isAlreadyGone) {
                let delCheckbox = row.querySelector("input[type='checkbox'][name$='-DELETE'], td.delete input[type='checkbox']");
                if (delCheckbox) {
                    delCheckbox.checked = true;
                    delCheckbox.disabled = false;
                    try { delCheckbox.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
                } else if (typeof pkFieldName === "string" && pkFieldName.trim() !== "") {
                    const delName = pkFieldName.replace(/-[a-zA-Z0-9_]+$/, '-DELETE');
                    let hiddenDel = row.querySelector(`input[name='${delName}']`);
                    if (!hiddenDel) {
                        hiddenDel = document.createElement("input");
                        hiddenDel.type = "hidden";
                        hiddenDel.name = delName;
                        row.appendChild(hiddenDel);
                    }
                    hiddenDel.value = "on";
                    hiddenDel.disabled = false;
                }

                // PK polia (-id, _ptr, _ptr_id, -object_id) NESMÚ byť disabled, inak ich browser neodošle a Django formset zlyhá
                row.querySelectorAll("input:not([name$='-DELETE']):not([name$='-id']):not([name$='_ptr']):not([name$='_ptr_id']):not([name$='-object_id']), select, textarea").forEach(el => {
                    el.required = false;
                    el.disabled = true;
                });
                
                row.style.transition = "all 0.3s ease";
                row.style.opacity = "0";
                row.style.transform = "scale(0.95)";
                setTimeout(() => {
                    if (row) {
                        row.style.display = "none";
                    }
                }, 300);
                
                const toastMsg = isAlreadyGone
                    ? "Položka už v databáze neexistovala, zoznam bol aktualizovaný."
                    : "Položka bola úspešne vymazaná zo skladu.";
                const toastIcon = isAlreadyGone ? "ℹ️" : "🗑️";

                if (typeof showISASToast === "function") {
                    showISASToast(toastMsg, toastIcon, isAlreadyGone ? "info" : "success");
                }
            } else {
                delete row.dataset.isasDeleting;
                if (actionBtn) {
                    actionBtn.disabled = false;
                    actionBtn.style.opacity = "1";
                    actionBtn.style.cursor = "pointer";
                }
                const errMsg = data.message || `Chyba servera pri mazaní položky (HTTP ${status}).`;
                if (window.showGlobalAlertModal) {
                    window.showGlobalAlertModal("Chyba pri mazaní", errMsg, "error");
                } else if (typeof showISASToast === "function") {
                    showISASToast(errMsg, "❌", "error");
                } else {
                    alert("Chyba: " + errMsg);
                }
            }
        })
        .catch(err => {
            delete row.dataset.isasDeleting;
            if (actionBtn) {
                actionBtn.disabled = false;
                actionBtn.style.opacity = "1";
                actionBtn.style.cursor = "pointer";
            }
            console.error("ISAS Sklad Delete Error:", err);
            if (typeof showISASToast === "function") {
                showISASToast("Chyba spojenia so serverom. Skontrolujte pripojenie.", "🔌", "error");
            } else {
                alert("Chyba spojenia so serverom.");
            }
        });
    }

    // 🎨 Vytváranie a úprava DOM tlačidiel
    function ensureDeleteButtons() {
        if (document.body && document.body.classList.contains("model-aktualnenasklade")) return;
        document.querySelectorAll("td.delete, td[class*='delete'], .djn-delete-handler").forEach(function (td) {
            let row = td.closest(".inline-related, tr");
            if (row && (row.classList.contains("empty-form") || row.classList.contains("djn-empty-form"))) return;
            
            // Zabezpečíme, aby kontajnerom bola vždy viditeľná tabuľková bunka <td>
            const container = td.closest("td") || (td.tagName && td.tagName.toLowerCase() === 'a' ? td.parentElement : td);
            if (!container) return;

            let checkbox = container.querySelector("input[type='checkbox']");
            if (checkbox) checkbox.style.display = "none";

            // Detegujeme existujúci odkaz/tlačidlo na mazanie v bunke
            let nativeLink = (td.tagName && td.tagName.toLowerCase() === 'a')
                ? td
                : container.querySelector("a.inline-deletelink, a.djn-remove-handler, .djn-delete-handler");

            if (nativeLink) {
                nativeLink.classList.add("custom-delete-btn");
                nativeLink.style.pointerEvents = "auto";
                nativeLink.style.cursor = "pointer";
                if (!nativeLink.dataset.isasDeleteInit) {
                    nativeLink.dataset.isasDeleteInit = "true";
                    nativeLink.addEventListener("click", function(e) {
                        handleDeleteRow(e, nativeLink);
                    });
                }
            } else if (!container.querySelector(".custom-delete-btn")) {
                let btn = document.createElement("button");
                btn.type = "button";
                btn.textContent = "ODSTRÁNIŤ";
                btn.className = "custom-delete-btn";
                btn.dataset.isasDeleteInit = "true";
                
                Object.assign(btn.style, {
                    backgroundColor: "#dc3545", color: "white", border: "none", padding: "5px 10px",
                    borderRadius: "4px", cursor: "pointer", fontWeight: "bold", fontSize: "11px", marginLeft: "5px",
                    position: "relative", zIndex: "50", pointerEvents: "auto"
                });

                btn.addEventListener("click", function(e) {
                    handleDeleteRow(e, btn);
                });

                container.appendChild(btn);
            }
        });
    }

    // ⚡ VÝKONNÁ OBSLUHA MAZANIA (Priame volanie + Globálna zachytávacia poistka)
    function handleDeleteRow(e, btn) {
        if (!btn || btn.dataset.isasHandlingDelete === "true") return;
        btn.dataset.isasHandlingDelete = "true";
        setTimeout(() => { if (btn) delete btn.dataset.isasHandlingDelete; }, 400);

        e.preventDefault();
        e.stopPropagation();

        let td = btn.closest("td") || btn.parentElement;
        let row = btn.closest(".inline-related, tr.djn-item, tr.form-row, tr");
        if (!row) return;

        let delCheckbox = row.querySelector("input[type='checkbox'][name$='-DELETE'], td.delete input[type='checkbox']");
        let rowPrefix = (delCheckbox && delCheckbox.name) ? delCheckbox.name.replace(/-DELETE$/, '') : '';
        let pkField = rowPrefix
            ? row.querySelector(`input[name='${rowPrefix}-id'], input[name='${rowPrefix}-object_id'], input[name^='${rowPrefix}'][name$='_ptr'], input[name^='${rowPrefix}'][name$='_ptr_id']`)
            : null;
        if (!pkField) {
            pkField = row.querySelector("input[name$='-id'], input[type='hidden'][name$='-id'], input[name$='_ptr'], input[type='hidden'][name$='_ptr'], input[type='hidden'][name$='_ptr_id']");
        }
        let isNewRow = !pkField || !pkField.value || pkField.value.trim() === "" || pkField.value === "None" || pkField.value === "undefined";

        // =========================================================================
        // SCENÁR A: NOVÝ / NEULOŽENÝ RIADOK (OKAMŽITÉ ODSTRÁNENIE Z DOM A FORMSETU)
        // =========================================================================
        if (isNewRow) {
            // 1. Pokus o odpálenie natívneho Django/Nested handleru
            let nativeLink = row.querySelector("a.inline-deletelink, a.djn-remove-handler");
            if (nativeLink && nativeLink !== btn) {
                try {
                    const jq = (window.django && window.django.jQuery) ? window.django.jQuery : (window.jQuery || null);
                    if (jq) {
                        jq(nativeLink).trigger("click");
                    } else {
                        nativeLink.click();
                    }
                    // Overenie odstránenia aj označenia nested-adminom na zabránenie dvojitému zníženiu TOTAL_FORMS
                    if (!row.isConnected || !document.body.contains(row) || row.classList.contains("djn-item-deleted")) {
                        return;
                    }
                } catch (err) {
                    console.warn("ISAS Warning: Natívny trigger zlyhal, pokračujem manuálnym cleanupom:", err);
                }
            }

            // 2. Garancia fyzického zmazania riadku z DOM, re-indexácie a úpravy TOTAL_FORMS (len ak natívny handler chýba)
            if (row && row.isConnected) {
                row.classList.add("isas-row-deleted");
                row.style.pointerEvents = "none";
                row.querySelectorAll("input, select, textarea").forEach(el => {
                    el.disabled = true;
                });

                let nameForPrefix = (pkField && pkField.name) ? pkField.name : (row.querySelector("input[name]")?.name || "");
                let matchPrefix = nameForPrefix.match(/^(.*)-(\d+)-([a-zA-Z0-9_]+)$/);
                if (matchPrefix && matchPrefix[1]) {
                    let formsetPrefix = matchPrefix[1];
                    let totalForms = document.getElementById(`id_${formsetPrefix}-TOTAL_FORMS`);
                    
                    if (totalForms) {
                        let currentCount = parseInt(totalForms.value, 10);
                        if (!isNaN(currentCount) && currentCount > 0) {
                            totalForms.value = currentCount - 1;
                        }
                    }

                    const safePrefix = formsetPrefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                    const regex = new RegExp(`(${safePrefix}-)(\\d+)(-)`);

                    // Prečíslovanie všetkých nasledujúcich priamych súrodencov formsetu (prevencia poškodenia vnorených tabuliek)
                    let parentContainer = row.parentElement;
                    let siblingRows = parentContainer ? Array.from(parentContainer.children).filter(r => r.matches && r.matches(".inline-related, tr.djn-item, tr.form-row, tr") && !r.classList.contains("empty-form") && !r.classList.contains("djn-empty-form")) : [];
                    let rowFound = false;

                    siblingRows.forEach(sib => {
                        if (sib === row) {
                            rowFound = true;
                            return;
                        }
                        if (rowFound && !sib.classList.contains("isas-row-deleted")) {
                            sib.querySelectorAll("input, select, textarea, label").forEach(el => {
                                const replacer = (m, p1, idx, p3) => {
                                    const parsedIdx = parseInt(idx, 10);
                                    return (!isNaN(parsedIdx) && parsedIdx > 0) ? `${p1}${parsedIdx - 1}${p3}` : m;
                                };
                                if (el.name) el.name = el.name.replace(regex, replacer);
                                if (el.id) el.id = el.id.replace(regex, replacer);
                                if (el.htmlFor) el.htmlFor = el.htmlFor.replace(regex, replacer);
                            });
                        }
                    });
                }

                row.style.transition = "all 0.2s ease";
                row.style.opacity = "0";
                row.style.transform = "scale(0.95)";
                setTimeout(() => {
                    if (row && row.parentNode) {
                        row.remove();
                    }
                }, 200);
            }
            return;
        }

        // =========================================================================
        // SCENÁR B: EXISTUJÚCI RIADOK Z DATABÁZY (AJAX & MODÁLNE OKNO)
        // =========================================================================
        let rawName = pkField.name || ""; 
        let modelName = "";
        let detectedKey = "";

        // 1. Extrakcia kľúča z Django Formsetu (defenzívne voči -id aj dedeným PK pointerom)
        let match = rawName.match(/(?:.*-\d+-)?([a-zA-Z0-9_]+)-\d+-[a-zA-Z0-9_]+$/);
        if (match && match[1]) {
            detectedKey = match[1];
        } else {
            detectedKey = rawName;
        }

        if (typeof window.ISAS_SKLAD_CONFIG === "string") {
            try { window.ISAS_SKLAD_CONFIG = JSON.parse(window.ISAS_SKLAD_CONFIG); } catch (e) { window.ISAS_SKLAD_CONFIG = {}; }
        }
        const mapping = (window.ISAS_SKLAD_CONFIG && window.ISAS_SKLAD_CONFIG.models) ? window.ISAS_SKLAD_CONFIG.models : {};

        // 2. Deterministické párovanie modelu s ochranou pred falošnou substring kolíziou
        if (mapping[detectedKey]) {
            modelName = mapping[detectedKey];
        } else {
            const keyClean = detectedKey.replace(/_set$/i, '').toLowerCase();
            if (mapping[keyClean]) {
                modelName = mapping[keyClean];
            } else {
                for (let key in mapping) {
                    if (key.toLowerCase() === keyClean || key.toLowerCase() === detectedKey.toLowerCase()) {
                        modelName = mapping[key];
                        break;
                    }
                }
                if (!modelName) {
                    const matchingKeys = Object.keys(mapping).filter(k => {
                        const kLower = k.toLowerCase();
                        return keyClean.includes(kLower) || kLower.includes(keyClean);
                    }).sort((a, b) => b.length - a.length);
                    if (matchingKeys.length > 0) {
                        modelName = mapping[matchingKeys[0]];
                    }
                }
            }
        }

        if (!modelName) {
            if (window.showGlobalAlertModal) {
                window.showGlobalAlertModal("Chyba konfigurácie", "Nepodarilo sa dynamicky určiť model pre vybranú položku.", "error");
            } else if (typeof showISASToast === "function") {
                showISASToast("Nepodarilo sa určiť model položky.", "⚠️", "error");
            }
            console.error("ISAS ERROR: Chýba model pre", rawName);
            return;
        }

        let nameInput = row.querySelector("input[name$='-seriove_cislo'], input[name$='-nazov'], input[name$='-katalogove_cislo']");
        let itemName = (nameInput && nameInput.value && nameInput.value.trim()) ? nameInput.value.trim() : "túto položku";
        let safeItemName = escapeStr(itemName);

        if (window.ISASModalEngine) {
            window.ISASModalEngine({
                maxWidth: "480px",
                icon: "🗑️",
                iconStyle: "background: #fef2f2; width: 70px; height: 70px; line-height: 70px; border-radius: 50%; margin: 0 auto 15px auto; font-size: 32px; box-shadow: 0 4px 15px rgba(239, 68, 68, 0.2);",
                title: "Vymazať zo skladu?",
                text: `Chystáte sa definitívne vymazať z databázy položku:<br><div style="margin: 12px 0 8px 0;"><b style="font-size: 15px; color: #0f172a; background: #f1f5f9; padding: 6px 16px; border-radius: 10px; border: 1px solid #cbd5e1; display: inline-block;">${safeItemName}</b></div><span style="color: #ef4444; font-size: 12px; font-weight: 700;">⚠️ Tento krok je nevratný a položka zmizne zo skladu.</span>`,
                showClose: false,
                buttons: [
                    {
                        id: "modal-del-cancel",
                        text: "Zrušiť",
                        style: "flex: 1; background: transparent; color: #64748b; border: 1px solid #cbd5e1; padding: 12px 20px; border-radius: 10px; font-size: 13px; font-weight: 800; cursor: pointer; transition: all 0.2s;",
                        onClick: (e, m) => m.close()
                    },
                    {
                        id: "modal-del-confirm",
                        text: "Áno, natrvalo vymazať",
                        style: "flex: 1; background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); color: white; border: none; padding: 12px 20px; border-radius: 10px; font-size: 13px; font-weight: 800; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3);",
                        onClick: (e, m) => {
                            m.close();
                            executeDelete(pkField.value, modelName, row, pkField.name);
                        }
                    }
                ]
            });
        } else if (window.showGlobalConfirmModal) {
            window.showGlobalConfirmModal(
                "Vymazať zo skladu?",
                `Naozaj chcete položku <b>${safeItemName}</b> definitívne vymazať zo skladu?`,
                () => executeDelete(pkField.value, modelName, row, pkField.name)
            );
        } else {
            if (confirm("Naozaj chcete túto položku (" + itemName + ") definitívne vymazať?")) {
                executeDelete(pkField.value, modelName, row, pkField.name);
            }
        }
    }

    // Capture-phase delegácia: zachytí kliknutie ešte predtým, než ho Django stihne zablokovať
    document.addEventListener("click", function(e) {
        let btn = e.target.closest(".custom-delete-btn, .djn-delete-handler, a.inline-deletelink, a.djn-remove-handler");
        if (btn) {
            handleDeleteRow(e, btn);
        }
    }, true);

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', ensureDeleteButtons);
    } else {
        ensureDeleteButtons();
    }

    // Podpora pre jQuery udalosti (Django & Nested Admin)
    const jq = (window.django && window.django.jQuery) ? window.django.jQuery : (window.jQuery || null);
    if (jq) {
        jq(document).on("djnesting:added formset:added", function() {
            setTimeout(ensureDeleteButtons, 20);
        });
    }

    document.addEventListener("djnesting:added", function() {
        setTimeout(ensureDeleteButtons, 50);
    });
    document.addEventListener("formset:added", function() {
        setTimeout(ensureDeleteButtons, 50);
    });

    // 🟢 Defenzívny MutationObserver spúšťaný až po načítaní DOM štruktúry
    function initObserver() {
        const targetNode = document.body || document.documentElement;
        if (!targetNode) return;

        let debounceTimer = null;
        const observer = new MutationObserver(function(mutations) {
            let hasRelevantNodes = false;
            for (let i = 0; i < mutations.length; i++) {
                for (let j = 0; j < mutations[i].addedNodes.length; j++) {
                    const n = mutations[i].addedNodes[j];
                    if (n.nodeType === 1 && !n.classList?.contains("custom-delete-btn")) {
                        hasRelevantNodes = true;
                        break;
                    }
                }
                if (hasRelevantNodes) break;
            }
            if (hasRelevantNodes) {
                if (debounceTimer) clearTimeout(debounceTimer);
                debounceTimer = setTimeout(ensureDeleteButtons, 40);
            }
        });

        observer.observe(targetNode, { childList: true, subtree: true });
    }

    window.initDeleteButtons = ensureDeleteButtons;
    window.ensureDeleteButtons = ensureDeleteButtons;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initObserver);
    } else {
        initObserver();
    }
})();


// ==========================================
// 3. scroll_highlight.js
// ==========================================
const initScrollHighlight = function () {
    let full = "";
    let key = "";
    try {
        full = (sessionStorage.getItem("skladHighlight_1_full") || "").trim();
        key = (sessionStorage.getItem("skladHighlight_1_key") || "").trim().toLowerCase();
        if (full || key) {
            sessionStorage.removeItem("skladHighlight_1_full");
            sessionStorage.removeItem("skladHighlight_1_key");
        }
    } catch (e) {
        return;
    }
    if (!full && !key) return;

    const deadline = Date.now() + 4000;

    function tryFind() {
        let target = null;
        const inputs = document.querySelectorAll("input[name$='-seriove_cislo'], input[type='text']");
        for (const inp of inputs) {
            const v = (inp.value || "").toLowerCase();
            if (!v) continue;
            if (key && v.includes(key)) { target = inp; break; }
            if (full && v.includes(full.toLowerCase())) { target = inp; break; }
        }
        if (!target) {
            const needle = (full || key).toLowerCase();
            const rows = document.querySelectorAll(".inline-related, tr.djn-item, tr.form-row, tr");
            for (const row of rows) {
                if (row.classList.contains("empty-form") || row.classList.contains("djn-empty-form")) continue;
                const txt = (row.textContent || "").toLowerCase();
                if (txt.includes(needle)) { target = row; break; }
            }
        }
        if (target) {
            const host = target.closest(".inline-related") || target.closest("tr") || target;
            host.scrollIntoView({ behavior: "smooth", block: "center" });
            host.style.transition = "all .3s ease-in-out";
            host.style.outline = "3px solid #ff9800";
            host.style.backgroundColor = "#e6f0ff";
            setTimeout(() => { host.style.outline = ""; host.style.backgroundColor = ""; }, 3000);
        } else if (Date.now() < deadline) {
            setTimeout(tryFind, 200);
        }
    }
    setTimeout(tryFind, 500);
};

if (document.readyState === 'loading') {
    document.addEventListener("DOMContentLoaded", initScrollHighlight);
} else {
    initScrollHighlight();
}


// ==========================================
// 4. ajax_save.js
// ==========================================
(function() {
    const getFullUrlKey = () => window.location.pathname + window.location.search;

    window.addEventListener('beforeunload', () => {
        try {
            sessionStorage.setItem('scrollPos_path', getFullUrlKey());
            sessionStorage.setItem('scrollPos_val', String(window.scrollY || 0));
        } catch (e) {}
    });

    const restoreScrollPos = () => {
        try {
            const savedPath = sessionStorage.getItem('scrollPos_path');
            const scrollPos = sessionStorage.getItem('scrollPos_val');
            sessionStorage.removeItem('scrollPos_path');
            sessionStorage.removeItem('scrollPos_val');
            if (savedPath === getFullUrlKey() && scrollPos !== null) {
                const parsedPos = parseInt(scrollPos, 10);
                if (!isNaN(parsedPos) && parsedPos >= 0) {
                    window.scrollTo(0, parsedPos);
                    setTimeout(() => window.scrollTo(0, parsedPos), 100);
                }
            }
        } catch (e) {}
    };

    if (document.readyState === 'complete') {
        restoreScrollPos();
    } else {
        window.addEventListener('load', restoreScrollPos);
    }
})();


// ==========================================
// 6. dodavatel_focus.js (Select2 Fix & UI Cleanup)
// ==========================================
(function() {
    var isSkladContext = function() {
        return !!(window.ISAS_SKLAD_CONFIG || 
            (document.body && (document.body.classList.contains("model-sklad") || document.body.classList.contains("model-aktualnenasklade"))) || 
            window.location.pathname.includes("/admin/Evidencia/sklad/"));
    };

    var removeIcons = function() {
        if (!isSkladContext()) return;
        var icons = document.querySelectorAll('.related-widget-wrapper-links, .add-related, .change-related, .delete-related, .view-related, .related-lookup');
        for (var i = 0; i < icons.length; i++) {
            if (!icons[i].classList.contains('select2-selection__clear')) {
                icons[i].style.display = 'none';
            }
        }
    };

    document.addEventListener('mousedown', function (e) {
        var targetEl = e.target && e.target.nodeType === 1 ? e.target : (e.target ? e.target.parentElement : null);
        if (targetEl && ((targetEl.classList && targetEl.classList.contains('select2-selection__clear')) || (typeof targetEl.closest === 'function' && targetEl.closest('.select2-selection__clear')))) {
            return;
        }
        var container = (targetEl && typeof targetEl.closest === 'function') ? targetEl.closest('.select2-container') : null;
        if (container && !container.classList.contains('select2-container--disabled')) {
            setTimeout(function () {
                var searchField = document.querySelector('.select2-container--open .select2-search__field') ||
                                  document.querySelector('.select2-dropdown .select2-search__field') ||
                                  (container ? container.querySelector('.select2-search__field') : null) ||
                                  document.querySelector('.select2-search__field');
                if (searchField) searchField.focus();
            }, 50);
        }
    }, true);

    var initIconsCleanup = function() {
        removeIcons();
        var jq = (window.django && window.django.jQuery) ? window.django.jQuery : (window.jQuery || null);
        if (jq) {
            jq(document).on("djnesting:added formset:added", removeIcons);
        }
        document.addEventListener('djnesting:added', removeIcons);
        document.addEventListener('formset:added', removeIcons);
    };

    if (document.readyState === 'complete') {
        initIconsCleanup();
    } else {
        window.addEventListener('load', initIconsCleanup);
    }
})();