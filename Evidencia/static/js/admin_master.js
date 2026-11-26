document.addEventListener("DOMContentLoaded", function() {
    
    // =============================================================================
    // 1. ISAS NOTIFIKÁCIE A TOASTY
    // =============================================================================
    const messageList = document.querySelector('.messagelist');
    if (messageList) {
        messageList.querySelectorAll('li').forEach((msg, index) => {
            const rawText = msg.innerText;
            let finalIcon = '✨', finalText = rawText, toastType = 'success';
            
            if (msg.classList.contains('error')) toastType = 'error';
            else if (msg.classList.contains('warning')) toastType = 'warning';

            const match = rawText.match(/^Objekt (.+?) „(.+?)“ (bol úspešne .+)\./);
            if (match) {
                const [_, modelName, objectName, actionText] = match;
                let translatedAction = "";
                if (actionText.includes("zmenený")) { translatedAction = "bol úspešne aktualizovaný."; finalIcon = '✅'; }
                else if (actionText.includes("pridaný")) { translatedAction = "bol úspešne vytvorený."; finalIcon = '🚀'; }
                else if (actionText.includes("odstránený")) { translatedAction = "bol natrvalo vymazaný."; finalIcon = '🗑️'; }
                finalText = `${modelName} „${objectName}“ ${translatedAction}`;
            } else {
                if (rawText.toLowerCase().includes('odstránen')) finalIcon = '🗑️';
                else if (toastType === 'success') finalIcon = '👍';
            }
            setTimeout(() => showISASToast(finalText, finalIcon, toastType), index * 300);
        });
        messageList.style.display = 'none';
    }

    // =============================================================================
    // 2. ISAS PREMIUM UI MASTER LOGIC & DEKLARATÍVNY ENGINE
    // =============================================================================
    let isasConfig = {};
    const configScript = document.getElementById('isas-core-config');
    if (configScript) {
        try { isasConfig = JSON.parse(configScript.textContent); } catch(e) { console.error("ISAS Config parse error", e); }
    }

    // 🌟 ISAS CORE ENGINES (Zjednotené funkcie pre Modaly, AJAX a Polling)
    window.ISASModalEngine = function(cfg) {
        document.querySelectorAll('.isas-modal-overlay').forEach(oldOverlay => oldOverlay.remove());

        const overlay = document.createElement("div");
        overlay.className = "isas-modal-overlay";
        overlay.style.cssText = "position:fixed;inset:0;background:rgba(15,23,42,0.65);backdrop-filter:blur(8px);z-index:999999;display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity 0.3s ease;";
        
        const card = document.createElement("div");
        card.style.cssText = `background:#fff;padding:35px 40px;border-radius:24px;box-shadow:0 25px 50px rgba(0,0,0,0.25);max-width:${cfg.maxWidth||"450px"};width:100%;text-align:${cfg.textAlign||"center"};transform:scale(0.9) translateY(20px);transition:all 0.4s cubic-bezier(0.34,1.56,0.64,1);border:1px solid #e2e8f0;max-height:90vh;overflow-y:auto;box-sizing:border-box;position:relative;`;

        const renderField = (f) => {
            const lbl = f.label ? `<label style="display:block;font-size:12px;font-weight:800;color:#475569;margin-bottom:5px;text-transform:uppercase;">${f.label}</label>` : '';
            const baseStyle = "width:100%;padding:10px 15px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px;box-sizing:border-box;outline:none;background:#f8fafc;transition:all 0.3s;";
            
            switch(f.type) {
                case 'row': return `<div style="display:flex;gap:15px;margin-bottom:15px;${f.style||''}">${f.fields.map(subF => `<div style="flex:1;">${renderField(subF)}</div>`).join('')}</div>`;
                case 'alert': return `<div style="background:${f.bg||'#fff1f2'};border-left:4px solid ${f.border||'#e11d48'};padding:14px;border-radius:8px;display:flex;gap:12px;text-align:left;margin-bottom:20px;align-items:center;"><span style="font-size:24px;">${f.icon||'🚨'}</span><p style="margin:0;font-size:13px;color:${f.color||'#9f1239'};line-height:1.5;">${f.text}</p></div>`;
                case 'textarea': return `<div style="margin-bottom:15px;text-align:left;">${lbl}<textarea id="${f.id}" rows="${f.rows||3}" placeholder="${f.placeholder||''}" style="${baseStyle}resize:vertical;font-family:inherit;">${f.value||''}</textarea></div>`;
                case 'select': return `<div id="${f.id}-wrapper" style="margin-bottom:15px;text-align:left;position:relative;">${lbl}<select id="${f.id}" ${f.multiple?'multiple="multiple"':''} style="width:100%;">${(f.options||[]).map(o => `<option value="${o.value}">${o.text}</option>`).join('')}</select></div>`;
                case 'toggle': return `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:15px;"><div style="font-size:13px;font-weight:700;color:#0f172a;">${f.label}</div><label class="isas-toggle" style="position:relative;display:inline-block;width:44px;height:24px;"><input type="checkbox" id="${f.id}" ${f.checked?'checked':''} style="opacity:0;width:0;height:0;"><span class="isas-slider" style="position:absolute;cursor:pointer;inset:0;background-color:#cbd5e1;transition:.4s;border-radius:24px;"></span></label></div>`;
                case 'checkbox': return `<label style="display:flex;align-items:center;gap:12px;margin-bottom:12px;cursor:pointer;"><input type="checkbox" id="${f.id}" ${f.checked?'checked':''} style="width:20px;height:20px;accent-color:#10b981;cursor:pointer;"><div style="font-size:13px;font-weight:800;color:#1e293b;">${f.label}${f.sublabel ? `<br><span style="font-size:10px;font-weight:600;color:#64748b;">${f.sublabel}</span>` : ''}</div></label>`;
                case 'html': return `<div style="margin-bottom:15px;text-align:left;">${f.html}</div>`;
                default: return `<div style="margin-bottom:15px;text-align:left;">${lbl}<input type="${f.type}" id="${f.id}" value="${f.value||''}" min="${f.min||''}" step="${f.step||''}" placeholder="${f.placeholder||''}" style="${baseStyle}"></div>`;
            }
        };

        const renderHTML = (c) => `
            ${c.showCloseX !== false ? `<button id="isas-modal-close-x" style="position:absolute;top:20px;right:20px;background:transparent;border:none;font-size:20px;color:#94a3b8;cursor:pointer;transition:color 0.2s;z-index:10;">✖</button>` : ''}
            ${c.icon ? `<div style="font-size:45px;margin-bottom:15px;display:flex;align-items:center;justify-content:center;${c.iconStyle||''}">${c.icon}</div>` : ''}
            ${c.title ? `<h3 style="margin:0 0 10px 0;color:#0f172a;font-size:24px;font-weight:900;letter-spacing:-0.5px;">${c.title}</h3>` : ''}
            ${c.text ? `<p style="margin:0 0 25px 0;color:#475569;font-size:14px;font-weight:500;line-height:1.6;">${c.text}</p>` : ''}
            ${c.fields?.length ? `<div class="isas-modal-fields">${c.fields.map(renderField).join('')}</div>` : ''}
            ${c.customHtml ? `<div style="margin-bottom:20px;text-align:left;">${c.customHtml}</div>` : ''}
            ${c.checkboxes?.length ? `<div style="text-align:left;background:#f8fafc;padding:15px;border-radius:12px;border:1px solid #e2e8f0;margin-bottom:25px;">${c.checkboxes.map(chk => renderField({type:'checkbox', ...chk})).join('')}</div>` : ''}
            ${c.buttons?.length ? `<div style="display:flex;gap:12px;flex-direction:${c.buttonsDirection||'row'};justify-content:center;">${c.buttons.map(btn => `<button id="${btn.id}" class="${btn.className||''}" style="${btn.style||'flex:1;padding:14px;border-radius:10px;font-size:13px;font-weight:800;cursor:pointer;text-transform:uppercase;transition:all 0.2s;'}">${btn.text}</button>`).join('')}</div>` : ''}
            ${c.showClose !== false ? `<button id="isas-modal-close-btn" style="display:block;margin:20px auto 0 auto;background:transparent;border:none;color:#94a3b8;font-size:13px;font-weight:700;cursor:pointer;text-decoration:underline;transition:color 0.2s;">${c.closeText||'Zrušiť'}</button>` : ''}
        `;

        const bindEvents = (c, mObj) => {
            const cx = card.querySelector('#isas-modal-close-x'); 
            if(cx) { cx.onmouseover = () => cx.style.color = '#ef4444'; cx.onmouseout = () => cx.style.color = '#94a3b8'; cx.onclick = mObj.close; }
            card.querySelector('#isas-modal-close-btn')?.addEventListener('click', mObj.close);
            c.buttons?.forEach(btn => card.querySelector(`#${btn.id}`)?.addEventListener('click', (e) => btn.onClick?.(e, mObj, e.currentTarget)));
            
            if (c.fields && typeof django !== 'undefined' && django.jQuery) {
                c.fields.filter(f => f.type === 'select').forEach(f => {
                    const $el = django.jQuery(`#${f.id}`);
                    if (f.ajaxUrl) {
                        $el.select2({
                            width: '100%',
                            ajax: { url: f.ajaxUrl, dataType: 'json', delay: 250, data: p => ({ q: p.term }), processResults: d => ({ results: d.results || d.technici || d }), cache: true },
                            placeholder: f.placeholder || 'Vyhľadajte...', minimumInputLength: f.minInput || 0, allowClear: f.allowClear || false, dropdownParent: django.jQuery(`#${f.id}-wrapper`),
                            language: { inputTooShort: a => `Zadajte aspoň ${a.minimum} znaky...`, noResults: () => "Nenašli sa žiadne výsledky.", searching: () => "Vyhľadávam..." }
                        });
                    } else if (f.select2) {
                        $el.select2({ width: '100%', placeholder: f.placeholder || 'Vyberte...', allowClear: f.allowClear || false, dropdownParent: django.jQuery(`#${f.id}-wrapper`) });
                    }
                });
            }
            c.onRender?.(mObj);
        };

        card.innerHTML = renderHTML(cfg);
        overlay.appendChild(card); document.body.appendChild(overlay);
        if(cfg.lockBody !== false) document.body.style.overflow = "hidden";
        
        requestAnimationFrame(() => { overlay.style.opacity = "1"; card.style.transform = "scale(1) translateY(0)"; });

        const modalObj = {
            overlay, card,
            close: () => {
                overlay.style.opacity = "0"; card.style.transform = "scale(0.9) translateY(20px)";
                if(cfg.lockBody !== false) document.body.style.overflow = "";
                setTimeout(() => { overlay.remove(); cfg.onClose?.(); }, 300);
            },
            updateContent: function(newCfg) { 
                if (typeof newCfg === 'string') { card.innerHTML = newCfg; return; }
                const merged = {...cfg, ...newCfg};
                card.innerHTML = renderHTML(merged);
                bindEvents(merged, this);
            }
        };
        bindEvents(cfg, modalObj);
        return modalObj;
    };

    window.ISASAction = function(payload, btn, opts = {}) {
        if (btn?.classList.contains("is-processing")) return;
        const origHtml = btn?.innerHTML || "";
        if (btn) { btn.classList.add("is-processing"); btn.innerHTML = opts.loadingText || "⏳ SPRACOVÁVAM..."; btn.style.pointerEvents = "none"; }
        
        const fetchOpts = { 
            method: opts.method || "POST", 
            headers: { "X-CSRFToken": document.querySelector('[name=csrfmiddlewaretoken]')?.value || '', ...(opts.headers || {}) } 
        };

        if (payload instanceof FormData) fetchOpts.body = payload;
        else if (payload) { fetchOpts.headers["Content-Type"] = "application/json"; fetchOpts.body = JSON.stringify(payload); }
        
        const defaultActionUrl = (window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/') + 'fast-action/';
        fetch(opts.url || defaultActionUrl, fetchOpts)
        .then(async res => {
            if (opts.handleRedirect && res.redirected) return { status: "redirect", url: res.url };
            let data;
            try { data = await res.json(); } catch(e) { data = null; }
            if (!res.ok && !opts.ignoreErrors) {
                const errMsg = data?.message || data?.error || `HTTP ${res.status}: ${res.statusText || 'Server Error'}`;
                return Promise.reject(errMsg);
            }
            return data || { status: "error", message: "Neplatná odpoveď zo servera." };
        })
        .then(data => {
            if (opts.onComplete) {
                const result = opts.onComplete(data, btn, origHtml);
                // 🛡️ Defenzívny watchdog: ak callback tlačidlo nechal visieť v načítavaní, vrátime ho späť
                if (btn && btn.innerHTML === (opts.loadingText || "⏳ SPRACOVÁVAM...")) {
                    btn.classList.remove("is-processing");
                    btn.style.pointerEvents = "auto";
                    btn.innerHTML = origHtml;
                }
                return result;
            }
            const isOk = data.status === "success" || data.status === "ok";
            showISASToast(opts[isOk ? 'successMsg' : 'errorMsg'] || data.message || (isOk ? "Úspešne dokončené" : "Nastala chyba"), isOk ? '✅' : '❌', isOk ? 'success' : 'error');
            if (btn) {
                btn.innerHTML = isOk ? (opts.successText || "✅ HOTOVO") : origHtml;
                btn.classList.toggle('success', isOk); btn.classList.toggle('error', !isOk);
                if (!isOk) { btn.classList.remove("is-processing"); btn.style.pointerEvents = "auto"; setTimeout(()=>btn.classList.remove('error'), 3000); }
            }
            if (isOk && opts.reload !== false) setTimeout(() => data.redirect_url ? (window.location.href = data.redirect_url) : window.location.reload(), 1500);
        }).catch(err => {
            const detailMsg = (typeof err === 'string' ? err : err?.message) || "Kritická chyba spojenia.";
            showISASToast(`🚨 ${detailMsg}`, '🔌', 'error');
            if (btn) { btn.classList.remove("is-processing"); btn.innerHTML = origHtml; btn.style.pointerEvents = "auto"; btn.classList.add('error'); setTimeout(()=>btn.classList.remove('error'), 3000); }
            opts.onError?.(err);
        });
    };

    window.ISASPoller = function(opts) {
        let attempts = 0;
        const intervalId = setInterval(() => {
            if (++attempts > (opts.maxAttempts || 30)) { clearInterval(intervalId); opts.onTimeout?.(); return; }
            fetch(opts.url, opts.fetchOptions || { method: 'GET' })
            .then(r => opts.type === 'json' ? r.json() : r.text())
            .then(data => { if (opts.onData(data, intervalId)) clearInterval(intervalId); })
            .catch(err => { if (opts.onError?.(err, intervalId)) clearInterval(intervalId); });
        }, opts.interval || 2500);
        return intervalId;
    };

    window.setISASFieldState = function(elId, isLocked, clearVal, softLock = false) {
        const el = document.getElementById('id_' + elId);
        if (!el) return;
        const container = el.closest('.fieldBox') || el.closest('.form-row');

        if (softLock) {
            el.disabled = false;
            isLocked ? el.setAttribute('readonly', 'readonly') : el.removeAttribute('readonly');
            if (isLocked) {
                el.dataset.softLocked = '1';
                if (el.required && !String(el.value || '').trim()) {
                    el.value = el.defaultValue || (el.type === 'number' ? '10' : '');
                }
            } else {
                delete el.dataset.softLocked;
            }

            if (!el.dataset.softLockBound) {
                el.dataset.softLockBound = '1';
                const blockIfLocked = (ev) => {
                    if (el.dataset.softLocked === '1' && ev.key !== 'Tab') ev.preventDefault();
                };
                el.addEventListener('keydown', blockIfLocked);
                el.addEventListener('mousedown', blockIfLocked);
                el.addEventListener('click', blockIfLocked);
            }

            const focusables = container ? container.querySelectorAll('a, input, select, textarea, button') : [el];
            focusables.forEach(node => {
                if (isLocked) {
                    if (!('origTabindex' in node.dataset)) node.dataset.origTabindex = node.getAttribute('tabindex') ?? '';
                    node.setAttribute('tabindex', '-1');
                } else if ('origTabindex' in node.dataset) {
                    if (node.dataset.origTabindex !== '') node.setAttribute('tabindex', node.dataset.origTabindex);
                    else node.removeAttribute('tabindex');
                    delete node.dataset.origTabindex;
                }
            });
        } else {
            el.disabled = isLocked;
            isLocked ? el.setAttribute('readonly', 'readonly') : el.removeAttribute('readonly');
        }

        if (isLocked && clearVal !== undefined) el.value = clearVal;

        if (container) {
            Object.assign(container.style, { transition: "all 0.3s ease", opacity: isLocked ? "0.4" : "1", filter: isLocked ? "grayscale(100%)" : "none", pointerEvents: isLocked ? "none" : "auto" });
        }
        if (!softLock && typeof django !== 'undefined' && django.jQuery) {
            const $sel = django.jQuery(el);
            if ($sel.hasClass('select2-hidden-accessible')) $sel.prop('disabled', isLocked);
        }
    };

    window.ISASBindFieldEvent = function(fieldId, events, handler, initDelay = 100) {
        const el = document.getElementById('id_' + fieldId);
        if (!el) return null;
        const wrappedHandler = () => handler({ target: el });
        if (typeof django !== 'undefined' && django.jQuery) django.jQuery(el).on(events, wrappedHandler);
        else events.split(' ').forEach(evt => el.addEventListener(evt, wrappedHandler));
        if (initDelay !== false) setTimeout(wrappedHandler, initDelay);
        return el;
    };

    window.ISASStyleEngine = function(id, css) {
        if (!document.getElementById(id)) document.head.insertAdjacentHTML('beforeend', `<style id="${id}">${css}</style>`);
    };

    window.ISASClickHandler = function(selector, callback) {
        document.body.addEventListener('click', e => {
            const target = e.target.closest(selector);
            if (target) callback(e, target);
        });
    };

    window.ISASAsyncExport = function(cfg, payload, mObj) {
        mObj.updateContent({ customHtml: '<div class="isas-spinner"></div>', title: cfg.loadingTitle || "Generujem dáta...", text: cfg.loadingText || "Systém analyzuje dáta. Môže to trvať niekoľko sekúnd.", showClose: false, showCloseX: false });
        window.ISASAction(payload, null, {
            onComplete: (data) => {
                if(data.status === "processing" && data.zaloha_id) {
                    window.ISASPoller({
                        url: "fast-action/", type: "json", interval: 2000,
                        fetchOptions: { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": document.querySelector('[name=csrfmiddlewaretoken]')?.value || '' }, body: JSON.stringify({ type: cfg.poll_action, zaloha_id: data.zaloha_id }) },
                        onData: (pData) => {
                            if(pData.status === "ready") {
                                mObj.updateContent({
                                    icon: "✅", title: "Report je pripravený", text: "Súbor bol úspešne vygenerovaný a bezpečne uložený do záloh.", buttonsDirection: "column", closeText: "Zavrieť okno", showCloseX: true,
                                    buttons: [
                                        { id: "btn-dl", text: "📥 Stiahnuť report", className: "isas-modal-btn isas-btn-download", onClick: () => { Object.assign(document.createElement('a'), {href: pData.url, download: ''}).click(); } },
                                        { id: "btn-send", text: "📧 Odoslať účtovníčke", className: "isas-modal-btn isas-btn-send", onClick: (e, m, b) => window.ISASAction({ type: cfg.send_action, zaloha_id: data.zaloha_id }, b, { loadingText: "⏳ Odosielam...", successText: "✅ Úspešne odoslané", reload: false, onComplete: (d, btn) => { if(d.status === "success") { btn.style.background = "#10b981"; setTimeout(() => m.close(), 2000); } else { btn.classList.remove('is-processing'); btn.innerHTML = "📧 Skúsiť znova"; } } }) }
                                    ]
                                });
                                return true;
                            }
                            if (pData.status === "error") { mObj.updateContent({ icon: "❌", title: "Chyba", text: pData.message, closeText: "Zavrieť", showCloseX: true }); return true; }
                            return false;
                        }
                    });
                } else { mObj.close(); showISASToast(data.message || "Chyba pri spustení generovania.", '❌', 'error'); }
            }, onError: () => mObj.close()
        });
    };

    /* --- FYZICKÉ ZMAZANIE PREDVOLENÝCH DJANGO ELEMENTOV --- */
    document.querySelectorAll('.submit-row [name="_addanother"], .submit-row [name="_continue"], .submit-row .deletelink-box, .submit-row a.deletelink, .related-widget-wrapper-link, .add-another, .change-related, .delete-related').forEach(el => el.remove());

    /* --- UNIFIKOVANÝ HIGHLIGHT ENGINE (Nasvietenie riadkov a inlines) --- */
    const startupUrl = window.location.href;
    if (startupUrl.includes('#highlight-')) {
        const match = window.location.hash.match(/#highlight(?:-device)?-(.+)/);
        if (match && match[1]) {
            const highlightId = match[1].replace(/[^a-zA-Z0-9_-]/g, '');
            let targetEl = null;

            // 1. Skúsime nájsť v inlines (formuláre)
            const inlineInputs = Array.from(document.querySelectorAll('input[name$="-id"]'));
            const matchedInput = inlineInputs.find(inp => inp.value === highlightId);
            if (matchedInput) targetEl = matchedInput.closest('.inline-related') || matchedInput.closest('tr');

            // 2. Skúsime nájsť v changeliste (tabuľky)
            if (!targetEl) {
                const hiddenSpan = document.querySelector(`[data-id="${highlightId}"]`);
                if (hiddenSpan) targetEl = hiddenSpan.closest('tr');
            }
            if (!targetEl) {
                const cb = document.querySelector(`input[name="_selected_action"][value="${highlightId}"]`);
                if (cb) targetEl = cb.closest('tr');
            }

            // 3. Aplikujeme efekt
            if (targetEl) {
                setTimeout(() => {
                    targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    targetEl.classList.add('vivid-box-flash');
                    if (targetEl.tagName === 'TR') targetEl.classList.add('vivid-row-flash');
                    
                    setTimeout(() => {
                        targetEl.classList.remove('vivid-box-flash', 'vivid-row-flash');
                    }, 4000); 
                }, 300); 
            }
        }
    }

    // Konsolidované zahladenie stôp po 1.5 sekunde
    if (startupUrl.includes('#highlight-') || startupUrl.includes('?q=')) {
        setTimeout(() => {
            const cleanUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
            window.history.replaceState({ path: cleanUrl }, '', cleanUrl);
        }, 1500);
    }

    /* --- TLAČIDLÁ: NASPÄŤ, HISTÓRIA, BLESKOVÉ ULOŽENIE --- */
    // SENIOR FIX: Ochranná podmienka pre Login, Sklad (Úroveň 1 a 2), Zmenu hesla a Zoznamy (Changelisty)
    const urlParams = new URLSearchParams(window.location.search);
    const isSkladLevel1or2 = window.location.pathname.includes('/sklad/') && (!urlParams.get('kat') || !urlParams.get('sub'));
    const isPasswordChangePage = window.location.pathname.includes('password_change') || document.body.classList.contains('isas-password-page');
    const isChangelist = document.body.classList.contains('change-list') || document.getElementById('changelist') || document.getElementById('changelist-form');

    if (!document.body.classList.contains('login') && !document.getElementById('login-form') && !isSkladLevel1or2 && !isPasswordChangePage && !isChangelist) {
        const submitRows = document.querySelectorAll('.submit-row');
        
        // Ak z nejakého dôvodu neexistuje .submit-row, skúsime nájsť samotné tlačidlo a jeho obal
        if (submitRows.length === 0) {
            const fallbackBtn = document.querySelector('input[name="_save"], button[name="_save"]');
            if (fallbackBtn && fallbackBtn.parentNode) {
                fallbackBtn.parentNode.classList.add('submit-row');
                applyButtonsToRow(fallbackBtn.parentNode);
            }
        } else {
            // Aplikujeme na všetky nájdené submit riadky
            submitRows.forEach(row => applyButtonsToRow(row));
        }
    }

    function applyButtonsToRow(row) {
        // Hľadáme tlačidlo striktne IBA vo vnútri tohto riadku
        const saveBtn = row.querySelector('[name="_save"]') || row.querySelector('input[type="submit"]') || row.querySelector('button[type="submit"]');
        if (!saveBtn) return;

        let backUrl = isasConfig.custom_back_url || '../';
        if (!isasConfig.custom_back_url) {
            const brd = document.querySelectorAll('.breadcrumbs a');
            if (brd.length >= 2) backUrl = brd[brd.length - 1].href; 
        }
        if (window.ISAS_CUSTOM_BACK_URL && window.ISAS_CUSTOM_BACK_URL !== "") {
            backUrl = window.ISAS_CUSTOM_BACK_URL;
        }

        if (!row.querySelector('.back-btn-premium')) {
            const backBtn = document.createElement('a');
            backBtn.href = backUrl; 
            backBtn.className = 'back-btn-premium'; 
            backBtn.innerHTML = '<span class="back-icon">⬅</span> NASPÄŤ';
            row.insertBefore(backBtn, saveBtn);
        }

        const historyLink = document.querySelector('.object-tools .historylink');
        if (historyLink && !row.querySelector('.history-btn-premium')) {
            const clonedHistory = historyLink.cloneNode(true);
            clonedHistory.className = 'history-btn-premium'; 
            clonedHistory.innerHTML = '🕒 HISTÓRIA ZMIEN'; 
            const refNode = row.querySelector('.back-btn-premium') || saveBtn;
            row.insertBefore(clonedHistory, refNode); 
        }

        if (!row.querySelector('.bleskove-ulozenie-btn')) {
            const ajaxBtn = document.createElement('button');
            ajaxBtn.type = 'button'; 
            ajaxBtn.className = 'bleskove-ulozenie-btn'; 
            ajaxBtn.innerHTML = '<span>⚡</span> BLESKOVÉ ULOŽENIE';
            
            if (saveBtn.nextSibling) {
                saveBtn.parentNode.insertBefore(ajaxBtn, saveBtn.nextSibling);
            } else {
                saveBtn.parentNode.appendChild(ajaxBtn);
            }

            ajaxBtn.onclick = function(e) {
                e.preventDefault();
                const form = document.querySelector('#content-main form') || document.querySelector('form');
                if (!form) return;
                
                const formData = new FormData(form);
                const isSklad = window.location.pathname.includes('/sklad/');
                if (!isSklad) formData.append('_continue', 'Znovu uložiť a upraviť');

                window.ISASAction(formData, ajaxBtn, {
                    url: isSklad ? `../ajax/save-custom/${window.location.search}` : (form.action || window.location.href),
                    loadingText: '<span>⏳</span> UKLADÁM...',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' },
                    handleRedirect: !isSklad,
                    onComplete: (data, btn, origHtml) => {
                        if (isSklad) {
                            if (data.status === 'ok') {
                                btn.innerHTML = '<span>✅</span> ULOŽENÉ!'; btn.classList.add('success');
                                if (data.new_ids) Object.keys(data.new_ids).forEach(prefix => { const idInput = document.querySelector(`input[name="${prefix}-id"]`); if (idInput && !idInput.value) idInput.value = data.new_ids[prefix]; });
                                if (data.updated_data) Object.keys(data.updated_data).forEach(prefix => {
                                    const snInput = document.querySelector(`input[name="${prefix}-seriove_cislo"]`), catInput = document.querySelector(`input[name="${prefix}-katalogove_cislo"]`);
                                    if (snInput) snInput.value = data.updated_data[prefix].seriove_cislo;
                                    if (catInput) catInput.value = data.updated_data[prefix].katalogove_cislo;
                                });
                                setTimeout(() => { btn.innerHTML = origHtml; btn.classList.remove('success'); btn.classList.remove('is-processing'); btn.disabled = false; }, 2000);
                            } else {
                                btn.innerHTML = '<span>❌</span> CHYBA'; btn.classList.add('error'); showISASToast(data.message, '❌', 'error');
                                setTimeout(() => { btn.innerHTML = origHtml; btn.classList.remove('error'); btn.classList.remove('is-processing'); btn.disabled = false; }, 3000);
                            }
                        } else {
                            if (data.status === 'redirect') {
                                btn.innerHTML = '<span>✅</span> ULOŽENÉ!'; btn.classList.add('success');
                                const hasInlines = document.querySelector('.inline-group, .djn-group');
                                if (window.location.pathname.endsWith('/add/') || hasInlines) { setTimeout(() => { window.location.href = data.url; }, 400); return; }
                                setTimeout(() => { btn.innerHTML = origHtml; btn.classList.remove('success'); btn.classList.remove('is-processing'); btn.disabled = false; }, 3000);
                            } else {
                                btn.innerHTML = '<span>❌</span> SKONTROLUJ POLIA'; btn.classList.add('error');
                                setTimeout(() => { btn.innerHTML = origHtml; btn.classList.remove('error'); btn.classList.remove('is-processing'); btn.disabled = false; }, 3000);
                            }
                        }
                    }
                });
            };
        }
        
        // Vynútenie správneho zobrazenia (ignoruje prípadné blokovanie z témy)
        if (isasConfig.hide_submit_row) {
            row.style.display = 'none';
        } else {
            row.style.display = 'flex';
            row.style.flexWrap = 'wrap';
            row.style.gap = '12px';
            row.style.alignItems = 'center';
            row.style.justifyContent = 'center';
        }
    }

    /* --- ISAS ČERVENÉ TLAČIDLO ODSTRÁNIŤ PRE VŠETKY INLINES --- */
    function renderCustomUI() {
        document.querySelectorAll('.inline-related .form-row:not(.empty-form)').forEach(row => {
            // 🟢 ISAS FIX: Ignorujeme riadky v Nested Admin (Sklad), kde si to rieši vlastný sklad_master.js
            if (row.closest('.djn-item') || row.closest('.djn-tbody') || row.closest('.djn-module')) return;

            let delContainer = row.querySelector('td.delete') || row.querySelector('.delete');
            if (!delContainer || delContainer.querySelector('.btn-vymazat-diel') || delContainer.querySelector('.custom-delete-btn')) return;
            
            let checkbox = delContainer.querySelector('input[type="checkbox"]');
            let djangoDeleteLink = delContainer.querySelector('.inline-deletelink') || row.querySelector('.inline-deletelink');

            if (checkbox) {
                checkbox.style.display = 'none';
                let label = delContainer.querySelector('label'); if (label) label.style.display = 'none';
                let btn = document.createElement('button');
                btn.type = 'button'; btn.className = 'btn-vymazat-diel'; btn.innerHTML = '🗑️ Odstrániť';
                btn.style.cssText = 'background-color: #ef4444; color: white; border: none; padding: 6px 14px; border-radius: 6px; font-weight: bold; cursor: pointer; transition: background 0.2s; white-space: nowrap; box-shadow: 0 2px 4px rgba(239, 68, 68, 0.3); display: inline-block;';
                btn.onclick = function(e) { e.preventDefault(); checkbox.checked = true; row.style.opacity = '0.3'; btn.innerHTML = 'Označené na zmazanie'; btn.style.backgroundColor = '#6b7280'; };
                delContainer.appendChild(btn);
            } else if (djangoDeleteLink) { djangoDeleteLink.style.display = 'inline-block'; }
        });
    }

    renderCustomUI();
    let jqAttempts = 0;
    let jqCheckInterval = setInterval(() => {
        if (typeof django !== 'undefined' && typeof django.jQuery !== 'undefined') {
            clearInterval(jqCheckInterval);
            
            // Bind na pridanie nového riadku (iba pre Custom UI, Select2 je presunutý do lokálnych skriptov)
            django.jQuery(document).on('formset:added', function(event, $row, formsetName) {
                renderCustomUI();
            });

        } else if (++jqAttempts > 50) clearInterval(jqCheckInterval); 
    }, 100);

    /* --- GLOBÁLNE VYČISTENIE STAVU POLOŽIEK --- */
    function cleanIsasTextGlobally() {
        const isasRegex = /\s*\(\d+\s+[a-zA-Z2]+\)/g;
        document.querySelectorAll('option').forEach(opt => {
            if(opt.textContent) { let newText = opt.textContent.replace(isasRegex, '').trim(); if (opt.textContent !== newText) opt.textContent = newText; }
        });
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        let node;
        while(node = walker.nextNode()) {
            if (node.parentElement && !['SCRIPT','STYLE'].includes(node.parentElement.tagName) && isasRegex.test(node.textContent)) {
                node.textContent = node.textContent.replace(isasRegex, '').trim();
            }
        }
    }
    cleanIsasTextGlobally();
    new MutationObserver((mutations, obs) => { obs.disconnect(); cleanIsasTextGlobally(); obs.observe(document.body, { childList: true, subtree: true }); }).observe(document.body, { childList: true, subtree: true });

    /* --- DYNAMICKÉ PREPISOVANIE HLAVIČIEK PARAMETROV --- */
    document.querySelectorAll('.inline-group').forEach(group => {
        let h2 = group.querySelector('h2');
        if (h2 && h2.textContent.includes('Parametre -')) {
            h2.textContent = 'Špecifické parametre servisovaných zariadení';
            group.querySelectorAll('.inline-related h3').forEach(h3 => {
                let spanTag = h3.querySelector('.inline_label'); if (spanTag) spanTag.style.display = 'none';
                let bTag = h3.querySelector('b'); if (bTag) bTag.textContent = bTag.textContent.replace(':', '').trim();
            });
        }
    });

    // =============================================================================
    // 3. SPRACOVANIE DEKLARATÍVNYCH PRAVIDIEL Z PYTHONU
    // =============================================================================
    if (isasConfig.sklad_config) window.ISAS_SKLAD_CONFIG = isasConfig.sklad_config;

    if (isasConfig.inject_sklad_dashboard_css) {
        window.ISASStyleEngine('isas-sklad-dashboard-css', `#content h1, .titles-and-tools, .paginator, #result_list thead { display: none !important; } .object-tools { margin-top: 0 !important; margin-bottom: 20px !important; } #result_list tbody tr { background: transparent !important; border: none !important; } .premium-sklad-card { background: linear-gradient(145deg, #ffffff 0%, #f8fafc 100%); border: 1px solid #e2e8f0; border-radius: 24px; padding: 30px 40px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 15px 35px -5px rgba(15, 23, 42, 0.05), inset 0 2px 4px rgba(255,255,255,0.8); width: 100%; box-sizing: border-box; cursor: default; gap: 20px; } .sklad-left-section { display: flex; align-items: center; gap: 25px; flex: 1; min-width: 0; } @keyframes floatIcon { 0%, 100% { transform: translateY(0px) rotate(-3deg); box-shadow: 0 10px 20px rgba(15, 23, 42, 0.15); } 50% { transform: translateY(-6px) rotate(0deg); box-shadow: 0 20px 30px rgba(15, 23, 42, 0.25); } } .sklad-icon-box { width: 80px !important; height: 80px !important; min-width: 80px !important; min-height: 80px !important; max-width: 80px !important; max-height: 80px !important; line-height: 80px !important; flex-shrink: 0 !important; background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%) !important; border-radius: 20px !important; display: flex !important; align-items: center !important; justify-content: center !important; font-size: 38px !important; box-shadow: 0 15px 30px rgba(15, 23, 42, 0.25), inset 0 2px 4px rgba(255,255,255,0.1) !important; animation: floatIcon 5s ease-in-out infinite !important; } .sklad-text-content { display: flex; flex-direction: column; gap: 6px; overflow: hidden; } .sklad-title { background: transparent !important; color: #0f172a !important; font-size: 32px !important; font-weight: 900 !important; letter-spacing: -0.5px !important; margin: 0 !important; line-height: 1.1 !important; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; } .sklad-subtitle { font-size: 12px; font-weight: 800; color: #64748b; text-transform: uppercase; letter-spacing: 1px; border-left: 3px solid #3b82f6; padding-left: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; } .sklad-right-section { display: flex; align-items: center; gap: 20px; flex-shrink: 0; } .sklad-status { background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; padding: 10px 20px; border-radius: 50px; font-size: 12px; font-weight: 900; text-transform: uppercase; letter-spacing: 0.5px; display: flex; align-items: center; gap: 8px; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.1); white-space: nowrap; } .status-dot { width: 10px; height: 10px; background: #10b981; border-radius: 50%; box-shadow: 0 0 10px #10b981; animation: pulseDot 2s infinite; } @keyframes pulseDot { 0%, 100% { opacity: 0.5; transform: scale(0.8); } 50% { opacity: 1; transform: scale(1.2); } } .sklad-btn-vstup { background: linear-gradient(135deg, #4f46e5 0%, #3730a3 100%); color: #ffffff !important; padding: 16px 32px; border-radius: 16px; font-size: 13px; font-weight: 900; text-transform: uppercase; letter-spacing: 1px; display: flex; align-items: center; gap: 10px; box-shadow: 0 10px 20px rgba(79, 70, 229, 0.3), inset 0 2px 4px rgba(255,255,255,0.2); transition: all 0.3s ease; text-decoration: none !important; cursor: pointer; position: relative; overflow: hidden; white-space: nowrap; } .sklad-btn-vstup::after { content: ''; position: absolute; top: 0; left: -100%; width: 50%; height: 100%; background: linear-gradient(to right, transparent, rgba(255,255,255,0.3), transparent); transform: skewX(-25deg); animation: buttonShine 4s infinite; } @keyframes buttonShine { 0%, 50% { left: -100%; } 100% { left: 200%; } } .sklad-btn-vstup:hover { transform: translateY(-3px); box-shadow: 0 15px 30px rgba(79, 70, 229, 0.4); background: linear-gradient(135deg, #4338ca 0%, #312e81 100%); } .sklad-btn-icon { font-size: 18px; transition: transform 0.3s ease; } .sklad-btn-vstup:hover .sklad-btn-icon { transform: translateX(6px); } @media (max-width: 1024px) { .premium-sklad-card { flex-direction: column; padding: 25px 20px; gap: 25px; } .sklad-left-section { flex-direction: column; text-align: center; gap: 15px; align-items: center !important; } .sklad-icon-box { margin: 0 auto !important; } .sklad-title { font-size: 26px !important; white-space: normal; } .sklad-subtitle { white-space: normal; border-left: none; border-bottom: 2px solid #3b82f6; padding-left: 0; padding-bottom: 8px; display: inline-block; } .sklad-right-section { flex-direction: column; width: 100%; gap: 15px; } .sklad-btn-vstup { width: 100%; justify-content: center; } } .isas-core-workshop { background: #0f172a !important; border-radius: 20px !important; padding: 0 30px !important; display: flex !important; flex-direction: row !important; align-items: center !important; justify-content: center !important; gap: 20px !important; height: 60px !important; border: 1px solid #1e293b !important; margin-bottom: 20px !important; width: 100% !important; box-sizing: border-box !important; box-shadow: inset 0 0 20px rgba(52, 211, 153, 0.05), 0 10px 25px rgba(0,0,0,0.1) !important; } .workshop-title { color: #34D399 !important; font-weight: 900 !important; letter-spacing: 3px !important; font-size: 15px !important; text-shadow: 0 0 12px rgba(52, 211, 153, 0.6) !important; } .workshop-scene { display: flex !important; flex-direction: row !important; align-items: center !important; gap: 15px !important; font-size: 20px !important; }`);
    }

    if (isasConfig.global_export_modal) {
        const cfg = isasConfig.global_export_modal;
        window.ISASStyleEngine('isas-export-modal-css', `.isas-modal-actions { display: flex; flex-direction: column; gap: 12px; } .isas-modal-btn { display: flex; align-items: center; justify-content: center; gap: 10px; width: 100%; padding: 16px; border-radius: 14px; font-size: 14px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; cursor: pointer; transition: all 0.3s ease; text-decoration: none; border: none; } .isas-btn-download { background: #f1f5f9; color: #0f172a; border: 2px solid #e2e8f0; } .isas-btn-download:hover { background: #e2e8f0; transform: translateY(-2px); } .isas-btn-send { background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%); color: white; box-shadow: 0 10px 20px rgba(59, 130, 246, 0.3); } .isas-btn-send:hover { transform: translateY(-2px); box-shadow: 0 15px 25px rgba(59, 130, 246, 0.4); filter: brightness(1.1); } .isas-btn-send.is-loading { pointer-events: none; opacity: 0.8; transform: scale(0.98); } .isas-modal-close { margin-top: 20px; background: transparent; border: none; color: #94a3b8; font-size: 13px; font-weight: 700; cursor: pointer; text-decoration: underline; transition: color 0.2s; } .isas-modal-close:hover { color: #ef4444; } .isas-spinner { width: 60px; height: 60px; border: 5px solid #e2e8f0; border-top-color: #3b82f6; border-radius: 50%; animation: isasSpin 1s linear infinite; margin: 0 auto 25px auto; } @keyframes isasSpin { to { transform: rotate(360deg); } } .isas-date-row { display: flex; gap: 15px; margin-bottom: 20px; text-align: left; } .isas-date-group { flex: 1; display: flex; flex-direction: column; gap: 6px; } .isas-date-group label { font-size: 12px; font-weight: 800; color: #475569; text-transform: uppercase; letter-spacing: 0.5px; } .isas-date-input { width: 100%; padding: 12px 15px; border-radius: 10px; border: 2px solid #cbd5e1; font-size: 14px; font-weight: 700; color: #0f172a; outline: none; transition: all 0.3s; box-sizing: border-box; font-family: inherit; background: #f8fafc; } .isas-date-input:focus { border-color: #3b82f6; background: #ffffff; box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.1); } .isas-preset-row { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-bottom: 25px; } .isas-preset-btn { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; padding: 6px 12px; border-radius: 20px; font-size: 11px; font-weight: 800; cursor: pointer; transition: all 0.2s; text-transform: uppercase; } .isas-preset-btn:hover { background: #e2e8f0; color: #0f172a; border-color: #94a3b8; } .isas-preset-btn.active { background: #dbeafe; color: #1d4ed8; border-color: #93c5fd; }`);
        window.ISASClickHandler("#" + cfg.trigger_id, (e, btn) => {
            if (btn.classList.contains("is-processing")) return;
            e.preventDefault();
            window.ISASModalEngine({
                maxWidth: "450px", icon: "📊", title: cfg.title, text: cfg.text, buttonsDirection: "column",
                buttons: [{
                    id: "btn-start-global-export", text: "🚀 Spustiť výpočet", className: "isas-modal-btn isas-btn-send",
                    onClick: (e, m) => window.ISASAsyncExport(cfg, { type: cfg.generate_action }, m)
                }]
            });
        });
    }

    if (isasConfig.hide_action_checkboxes) {
        window.ISASStyleEngine('isas-hide-checkboxes-css', `#changelist table thead th.action-checkbox-column, #changelist table tbody td.action-checkbox { display: none !important; width: 0 !important; padding: 0 !important; }`);
    }

    if (isasConfig.file_action_modal) {
        window.showFileModal = function(fileUrl) {
            window.ISASModalEngine({
                maxWidth: "420px", icon: "📄", iconStyle: "background: #f0f9ff; width: 85px; height: 85px; line-height: 85px; border-radius: 50%; border: 2px solid #bae6fd; margin: 0 auto 20px auto;",
                title: "Akcia so súborom", text: "✨ Chcete tento dokument otvoriť na novej karte vo vašom prehliadači, alebo ho chcete rovno stiahnuť do vášho zariadenia? 🤔",
                buttons: [
                    { id: "btn-open-file", text: "👁️ Otvoriť", style: "background: linear-gradient(135deg, #0ea5e9 0%, #0284c7 100%); color: white; border: none; padding: 12px 24px; border-radius: 12px; font-size: 13px; font-weight: 900; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 15px rgba(14,165,233,0.4); text-transform: uppercase;", onClick: (e, m) => { window.open(fileUrl, '_blank'); m.close(); } },
                    { id: "btn-download-file", text: "💾 Stiahnuť", style: "background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; border: none; padding: 12px 24px; border-radius: 12px; font-size: 13px; font-weight: 900; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 15px rgba(16,185,129,0.4); text-transform: uppercase;", onClick: (e, m) => { const a = document.createElement('a'); a.href = fileUrl; a.download = fileUrl.split('/').pop(); document.body.appendChild(a); a.click(); document.body.removeChild(a); m.close(); } }
                ], closeText: "❌ Zrušiť akciu"
            });
        };
    }
    
    // Modern toggles sú teraz trvalo a čisto definované priamo v base_site.html

    if (isasConfig.vivid_box_flash) {
        window.ISASStyleEngine('isas-vivid-box-flash-css', `.vivid-box-flash { background: rgba(255,255,255,0.8); backdrop-filter: blur(10px); border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.05); border: 1px solid rgba(255,255,255,0.9); }`);
    }

    // A. EXKLUZÍVNE ZAMYKANIE POLÍ
    if (isasConfig.exclusive_fields) {
        isasConfig.exclusive_fields.forEach(group => {
            window.ISASBindFieldEvent(group[0], 'change', (e) => {
                const hasValue = e.target.value && e.target.value !== '';
                group[1].forEach(targetName => window.setISASFieldState(targetName, hasValue));
            });
        });
    }

    // B. ODOMYKANIE POLÍ A TOGGLE HODNÔT
    if (isasConfig.autofill_rules) {
        (Array.isArray(isasConfig.autofill_rules) ? isasConfig.autofill_rules : [isasConfig.autofill_rules]).forEach(rule => {
            if (rule.action === 'unlock_fields') {
                window.ISASBindFieldEvent(rule.trigger_field, 'input change', (e) => {
                    const isCheckbox = e.target.type === 'checkbox';
                    const hasValue = isCheckbox ? Boolean(e.target.checked) : Boolean(e.target.value && e.target.value.trim() !== '');
                    rule.target_fields.forEach(targetName => {
                        const targetEl = document.getElementById('id_' + targetName);
                        if (targetEl && rule.restore_on_unlock) {
                            if (!hasValue && targetEl.value !== '') {
                                targetEl.dataset.isasPrevVal = targetEl.value;
                            }
                            window.setISASFieldState(targetName, !hasValue, !hasValue ? '' : undefined);
                            if (hasValue && targetEl.value === '' && targetEl.dataset.isasPrevVal) {
                                targetEl.value = targetEl.dataset.isasPrevVal;
                                delete targetEl.dataset.isasPrevVal;
                            }
                        } else {
                            window.setISASFieldState(targetName, !hasValue, !hasValue ? '' : undefined);
                        }
                    });
                });
            } else if (rule.action === 'toggle_value') {
                window.ISASBindFieldEvent(rule.trigger_field, 'change', (e) => {
                    const targetEl = document.getElementById('id_' + rule.target_field);
                    if (targetEl) targetEl.value = e.target.checked ? rule.value_checked : rule.value_unchecked;
                });
            }
        });
    }

    // B2. AJAX AUTOFILL & LOCKING
    if (isasConfig.ajax_autofill) {
        const cfg = isasConfig.ajax_autofill;
        let isInitialLoad = true; setTimeout(() => isInitialLoad = false, 800);
        
        const targetKeys = Object.keys(cfg.mapping);
        const applyLockState = (isLocked) => targetKeys.forEach(k => window.setISASFieldState(k, isLocked));
        
        if (document.getElementById('id_' + cfg.trigger_field)?.value) applyLockState(true);

        window.ISASBindFieldEvent(cfg.trigger_field, 'change', (e) => {
            const val = e.target.value;
            if (!val) {
                applyLockState(false);
                targetKeys.forEach(k => { const el = document.getElementById('id_' + k); if (el) el.value = ''; });
                return;
            }
            
            applyLockState(true);
            const firstTarget = document.getElementById('id_' + targetKeys[0]);
            if (isInitialLoad && firstTarget?.value && !firstTarget.value.includes('Chyba')) return;
            
            targetKeys.forEach(k => { const el = document.getElementById('id_' + k); if (el) el.value = '⏳...'; });
            
            window.ISASAction({type: cfg.fast_action_type, ids: [val]}, null, {
                url: (window.location.pathname.match(/^(.*\/admin\/[^\/]+\/[^\/]+\/)/)?.[1] || '/') + 'fast-action/',
                reload: false,
                onComplete: (data) => {
                    if (data.status === 'success') {
                        Object.entries(cfg.mapping).forEach(([tKey, dKey]) => {
                            const el = document.getElementById('id_' + tKey);
                            if (el) { 
                                el.value = data[dKey] || ''; 
                                Object.assign(el.style, { backgroundColor: '#ecfdf5', borderColor: '#10b981', transition: 'all 0.4s' }); 
                                setTimeout(() => Object.assign(el.style, { backgroundColor: '', borderColor: '' }), 800); 
                            }
                        });
                    } else {
                        targetKeys.forEach((k, i) => { const el = document.getElementById('id_' + k); if (el) el.value = i === 0 ? '❌ Chyba: ' + data.message : ''; });
                    }
                },
                onError: () => targetKeys.forEach((k, i) => { const el = document.getElementById('id_' + k); if (el) el.value = i === 0 ? '🔌 Chyba spojenia' : ''; })
            });
        }, false);
    }

    // D. MANAŽÉR MARŽÍ (Cenový katalóg)
    if (isasConfig.margin_manager) {
        window.ISASStyleEngine('isas-margin-manager-css', `.actions > *:not(.isas-action-btn):not(#isas-custom-save-btn):not(#isas-margin-wrapper){display:none!important;}#isas-margin-wrapper{display:flex!important;position:relative;align-items:center;height:48px;}#isas-margin-wrapper span{position:absolute;left:14px;font-size:16px;pointer-events:none;}#isas-margin-wrapper input{display:block!important;height:48px!important;border-radius:8px!important;border:2px solid #cbd5e1!important;padding:0 15px 0 40px!important;font-size:14px!important;font-weight:800!important;color:#0f172a!important;width:140px!important;outline:none!important;transition:all 0.3s ease!important;box-shadow:0 4px 6px rgba(0,0,0,0.05)!important;}#isas-margin-wrapper input:focus{border-color:#0ea5e9!important;box-shadow:0 0 0 4px rgba(14,165,233,0.15)!important;}#isas-custom-save-btn{flex:1!important;color:white!important;border:none!important;border-radius:8px!important;font-size:12px!important;font-weight:900!important;text-transform:uppercase!important;letter-spacing:1px!important;cursor:pointer!important;transition:all 0.3s;height:48px!important;display:flex!important;align-items:center!important;justify-content:center!important;box-shadow:0 4px 6px rgba(0,0,0,0.1)!important;margin:0!important;}.is-success{background:linear-gradient(135deg,#10b981 0%,#059669 100%)!important;box-shadow:0 0 20px rgba(16,185,129,0.6)!important;transform:scale(1.02)!important;pointer-events:none!important;}.btn-text-fade{display:inline-block;animation:popIn 0.4s cubic-bezier(0.175,0.885,0.32,1.275) forwards;}@keyframes popIn{from{opacity:0;transform:scale(0.8) translateY(2px);}to{opacity:1;transform:scale(1) translateY(0);}}table#result_list tbody td input[type="text"],table#result_list tbody td input[type="number"]{border:2px solid #cbd5e1!important;border-radius:12px!important;padding:8px 12px!important;font-size:14px!important;font-weight:900!important;color:#0f172a!important;text-align:center!important;width:110px!important;background:linear-gradient(180deg,#f8fafc 0%,#ffffff 100%)!important;box-shadow:inset 0 2px 4px rgba(0,0,0,0.03),0 2px 5px rgba(0,0,0,0.02)!important;transition:all 0.3s ease!important;}table#result_list tbody td input[type="text"]:focus,table#result_list tbody td input[type="number"]:focus{border-color:#0ea5e9!important;background:#ffffff!important;box-shadow:0 0 0 4px rgba(14,165,233,0.2)!important;outline:none!important;transform:scale(1.03)!important;}table#result_list tbody tr.vivid-row-flash td input{background-color:#ffffff!important;color:#0f172a!important;border:2px solid #f97316!important;box-shadow:0 2px 5px rgba(0,0,0,0.1)!important;}`);

        const actionsContainer = document.querySelector(".isas-forced-actions") || document.querySelector(".actions");
        if (actionsContainer) {
            const marzaInput = document.querySelector('input[name="nova_marza"]');
            let inputWrapper;
            if (marzaInput) {
                inputWrapper = document.createElement("div"); 
                Object.assign(inputWrapper, { id: "isas-margin-wrapper", className: "isas-input-wrapper", innerHTML: "<span>📈</span>" });
                marzaInput.parentNode.insertBefore(inputWrapper, marzaInput); 
                inputWrapper.appendChild(marzaInput); 
                Object.assign(marzaInput, { placeholder: "Prirážka %", style: "" });
            }
            
            const oldActionBtn = actionsContainer.querySelector("button.button");
            if (oldActionBtn) {
                const newActionBtn = oldActionBtn.cloneNode(true); 
                Object.assign(newActionBtn, { className: "isas-action-btn btn-blue", id: "btn-bulk-margin", innerHTML: "<span class='btn-text-fade'>✨ NASTAVIŤ ZADANÚ PRIRÁŽKU</span>" });
                oldActionBtn.parentNode.replaceChild(newActionBtn, oldActionBtn);
                if (inputWrapper) actionsContainer.insertBefore(inputWrapper, newActionBtn);
            }
            
            if (!document.querySelector("#isas-custom-save-btn")) {
                const saveBtn = document.createElement("button"); 
                Object.assign(saveBtn, { id: "isas-custom-save-btn", type: "button", className: "isas-action-btn btn-green", innerHTML: "<span class='btn-text-fade'>💾 ULOŽIŤ ZMENY V KATALÓGU</span>" });
                actionsContainer.appendChild(saveBtn);
            }
        }

        document.body.addEventListener("input", e => { 
            if (e.target.tagName === "INPUT" && e.target.name.startsWith("form-")) e.target.closest("tr")?.classList.add("isas-modified-row"); 
        });

        const updateDOM = (items) => {
            items.forEach(item => {
                const row = document.querySelector(`input[name="_selected_action"][value="${item.id}"]`)?.closest('tr');
                if (!row) return;
                
                const updateCell = (selector, val) => { const td = row.querySelector(selector); if (td && val !== undefined) td.innerHTML = val; };
                const updateInput = (nameSuffix, val) => { const inp = row.querySelector(`input[name$="${nameSuffix}"]`); if (inp && val !== undefined) { inp.value = val; inp.defaultValue = val; } };
                
                updateCell('.field-predajna_cena_formatovana', item.predajna_cena);
                updateCell('.field-get_cisty_zisk', item.cisty_zisk);
                updateCell('.field-get_skutocna_marza', item.skutocna_marza);
                updateInput('-nakupna_cena', item.nakupna_cena_val);
                updateInput('-marza_percento', item.marza_val);
                
                row.querySelectorAll('td').forEach(td => { 
                    const origBg = td.style.backgroundColor; 
                    td.style.transition = "background-color 0.4s ease"; td.style.backgroundColor = "#dcfce7"; 
                    setTimeout(() => td.style.backgroundColor = origBg, 1000); 
                });
            });
        };

        window.ISASClickHandler("#btn-bulk-margin", (e, btn) => {
            e.preventDefault(); e.stopImmediatePropagation(); 
            if (btn.classList.contains("is-processing") || btn.classList.contains("is-success")) return;
            
            const marzaInput = document.querySelector('input[name="nova_marza"]');
            if (!marzaInput?.value) return showISASToast("Prosím, najskôr zadaj číselnú hodnotu prirážky.", "⚠️", "warning");
            
            const checked = document.querySelectorAll('input[name="_selected_action"]:checked');
            if (!checked.length) return showISASToast("Najskôr zaškrtni aspoň jednu položku!", "📦", "warning");
            
            const selectedIds = Array.from(checked).map(cb => cb.value);
            window.showGlobalConfirmModal(
                "Hromadná zmena prirážky 📈",
                `Naozaj chcete nastaviť prirážku <b>${marzaInput.value} %</b> pre <b>${selectedIds.length} položiek</b>? Predajné ceny budú okamžite prepočítané.`,
                () => {
                    window.ISASAction({ type: 'bulk_margin', marza: marzaInput.value, ids: selectedIds }, btn, {
                        reload: false,
                        onComplete: (data, b, origHtml) => {
                    if(data.status === "success") { 
                        updateDOM(data.updated_items); showISASToast(data.message, '✅', 'success'); 
                        b.classList.replace("is-processing", "is-success"); b.innerHTML = "<span class='btn-text-fade'>✅ HOTOVO</span>";
                        setTimeout(() => { 
                            b.classList.remove("is-success"); b.innerHTML = origHtml; marzaInput.value = ""; 
                            const selectAll = document.querySelector("#action-toggle"); if (selectAll) selectAll.checked = false; 
                            checked.forEach(cb => cb.checked = false); 
                        }, 2000);
                    } else { showISASToast(data.message, '❌', 'error'); b.classList.remove("is-processing"); b.innerHTML = origHtml; }
                }
            });
        });
        });

        window.ISASClickHandler("#isas-custom-save-btn", (e, btn) => {
            e.preventDefault(); e.stopImmediatePropagation(); 
            if (btn.classList.contains("is-processing") || btn.classList.contains("is-success")) return;
            
            const modifiedRows = document.querySelectorAll('.isas-modified-row');
            if (!modifiedRows.length) return showISASToast("Žiadne zmeny na uloženie.", "✅", "success");

            const items = Array.from(modifiedRows).map(row => ({
                id: (row.querySelector('input[name="_selected_action"]') || row.querySelector('input[name$="-id"]'))?.value,
                nakupna_cena: row.querySelector('input[name$="-nakupna_cena"]')?.value || null,
                marza_percento: row.querySelector('input[name$="-marza_percento"]')?.value || null
            })).filter(i => i.id);

            window.ISASAction({ type: 'manual_save', items, ids: items.map(i => i.id) }, btn, {
                reload: false,
                onComplete: (data, b, origHtml) => {
                    if(data.status === "success") { 
                        updateDOM(data.updated_items); showISASToast(data.message, '✅', 'success'); 
                        b.classList.replace("is-processing", "is-success"); b.innerHTML = "<span class='btn-text-fade'>✅ HOTOVO</span>";
                        modifiedRows.forEach(row => row.classList.remove('isas-modified-row'));
                        setTimeout(() => { b.classList.remove("is-success"); b.innerHTML = origHtml; }, 2000);
                    } else { showISASToast(data.message, '❌', 'error'); b.classList.remove("is-processing"); b.innerHTML = origHtml; }
                }
            });
        });
    }

    // D. SMART MATCHER AI BANNER
    if (isasConfig.smart_matcher?.length > 0) {
        const suggestions = isasConfig.smart_matcher;
        const getDeclension = (n) => n === 1 ? "návrh" : (n >= 2 && n <= 4 ? "návrhy" : "návrhov");
        
        const banner = document.createElement("div");
        banner.id = "isas-smart-banner";
        banner.style.cssText = "position:fixed;bottom:-100px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);color:white;padding:12px 24px;border-radius:50px;box-shadow:0 10px 25px rgba(0,0,0,0.3);z-index:9999;display:flex;align-items:center;gap:15px;border:1px solid #334155;cursor:pointer;transition:all 0.5s cubic-bezier(0.68,-0.55,0.265,1.55);";
        banner.innerHTML = `<span style="font-size:22px;animation:pulse 2s infinite;">🤖</span><span style="font-size:14px;font-weight:600;letter-spacing:0.5px;">ISAS AI našla <strong style="color:#38bdf8;" id="isas-smart-count">${suggestions.length} ${getDeclension(suggestions.length)}</strong> na spárovanie</span><span style="background:#3b82f6;color:white;font-weight:900;padding:4px 12px;border-radius:20px;font-size:11px;text-transform:uppercase;box-shadow:0 2px 4px rgba(0,0,0,0.2);margin-right:10px;">Zobraziť</span><div id="isas-banner-close" style="display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;background:rgba(255,255,255,0.1);color:#94a3b8;font-size:10px;transition:all 0.2s;border:1px solid rgba(255,255,255,0.05);" title="Skryť banner">✖</div>`;
        
        document.body.appendChild(banner);
        setTimeout(() => banner.style.bottom = "35px", 500);

        banner.querySelector('#isas-banner-close').addEventListener("click", e => { e.stopPropagation(); banner.style.bottom = "-100px"; setTimeout(() => banner.remove(), 500); });
        banner.addEventListener("mouseenter", () => { banner.style.transform = "translateX(-50%) translateY(-5px)"; banner.style.boxShadow = "0 15px 35px rgba(0,0,0,0.4)"; });
        banner.addEventListener("mouseleave", () => { banner.style.transform = "translateX(-50%) translateY(0)"; banner.style.boxShadow = "0 10px 25px rgba(0,0,0,0.3)"; });

        banner.addEventListener("click", () => {
            const modalFields = [];
            suggestions.forEach((m, i) => {
                modalFields.push({
                    type: 'html',
                    html: `<div class="isas-match-item" id="match-item-${i}" style="border:1px solid #e2e8f0;border-radius:16px;padding:15px;background:#f8fafc;transition:all 0.3s;margin-bottom:15px;">
                        <div style="display:flex;gap:15px;margin-bottom:12px;">
                            <div style="flex:1;background:white;border:1px solid #e2e8f0;border-radius:10px;padding:10px;"><div style="font-size:10px;font-weight:900;color:#94a3b8;text-transform:uppercase;margin-bottom:4px;">${m.left_label||'Položka 1'}</div><div style="font-size:12px;font-weight:700;color:#0f172a;">${m.left_str}</div></div>
                            <div style="flex:1;background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:10px;"><div style="font-size:10px;font-weight:900;color:#3b82f6;text-transform:uppercase;margin-bottom:4px;">${m.right_label||'Položka 2'}</div><div style="font-size:12px;font-weight:700;color:#1e3a8a;">${m.right_str}</div></div>
                        </div>
                        <div style="font-size:11px;font-weight:700;color:#166534;margin-bottom:12px;display:flex;align-items:center;gap:6px;"><span style="font-size:14px;">🎯</span> ${m.dovod}</div>
                        <div style="display:flex;gap:10px;">
                            <button class="smart-ignore-btn" data-index="${i}" data-left="${m.left_id}" data-right="${m.right_id}" style="flex:1;background:#fef2f2;color:#e11d48;border:1px solid #fecaca;padding:10px;border-radius:8px;font-size:12px;font-weight:800;cursor:pointer;text-transform:uppercase;transition:all 0.2s;">🚫 Ignorovať</button>
                            <button class="smart-pair-btn" data-index="${i}" data-left="${m.left_id}" data-right="${m.right_id}" style="flex:2;background:linear-gradient(135deg,#3b82f6 0%,#1d4ed8 100%);color:white;border:none;padding:10px;border-radius:8px;font-size:12px;font-weight:800;cursor:pointer;text-transform:uppercase;box-shadow:0 4px 10px rgba(59,130,246,0.3);transition:all 0.2s;">🔗 Spárovať</button>
                        </div>
                    </div>`
                });
            });

            window.ISASModalEngine({
                maxWidth: "700px", icon: "🤖", iconStyle: "background:linear-gradient(135deg,#6366f1 0%,#4f46e5 100%);width:70px;height:70px;line-height:70px;border-radius:50%;margin:0 auto 10px auto;color:white;box-shadow:0 10px 20px rgba(99,102,241,0.3);",
                title: `Návrhy na spárovanie (${suggestions.length})`, text: "Potvrďte, či navrhované záznamy k sebe patria.", 
                fields: modalFields, 
                closeText: "Zavrieť okno",
                onRender: (m) => {
                    m.card.querySelector(".isas-modal-fields").addEventListener("click", e => {
                        const btn = e.target.closest('button');
                        if (!btn || (!btn.classList.contains('smart-ignore-btn') && !btn.classList.contains('smart-pair-btn'))) return;
                        
                        const isIgnore = btn.classList.contains('smart-ignore-btn');
                        const itemDiv = m.card.querySelector('#match-item-' + btn.dataset.index);
                        
                        window.ISASAction({ type: isIgnore ? 'smart_ignore' : 'smart_pair', left_id: btn.dataset.left, right_id: btn.dataset.right }, btn, {
                            url: window.location.pathname.split('?')[0] + "fast-action/", reload: false,
                            onComplete: (data, b, origHtml) => {
                                if(data.status === "success") { 
                                    showISASToast(data.message, '🚀', 'success');
                                    if (itemDiv) {
                                        itemDiv.style.opacity = "0"; itemDiv.style.transform = "scale(0.95)";
                                        setTimeout(() => { 
                                            itemDiv.remove(); 
                                            const remaining = m.card.querySelectorAll('.isas-match-item').length;
                                            if (remaining === 0) { 
                                                m.close(); document.getElementById("isas-smart-banner")?.remove();
                                                setTimeout(() => window.location.reload(), 400); 
                                            } else { 
                                                const titleEl = m.card.querySelector('h3'); 
                                                if (titleEl) titleEl.innerText = `Návrhy na spárovanie (${remaining})`; 
                                                const countEl = document.getElementById("isas-smart-count");
                                                if (countEl) countEl.innerText = `${remaining} ${getDeclension(remaining)}`;
                                            }
                                        }, 300);
                                    }
                                } else { 
                                    showISASToast(data.message, '⚠️', 'error'); 
                                    b.innerHTML = origHtml; b.style.pointerEvents = "auto"; b.classList.remove("is-processing");
                                }
                            }
                        });
                    });
                }
            });
        });
    }

// F. ACTION MODALS & DIRECT ACTIONS
    if (isasConfig.action_modals) {
        window.ISASStyleEngine('isas-action-modals-css', `.isas-toggle{position:relative;display:inline-block;width:44px;height:24px;}.isas-toggle input{opacity:0;width:0;height:0;}.isas-slider{position:absolute;cursor:pointer;inset:0;background-color:#cbd5e1;transition:.4s;border-radius:24px;}.isas-slider:before{position:absolute;content:"";height:18px;width:18px;left:3px;bottom:3px;background-color:white;transition:.4s;border-radius:50%;box-shadow:0 2px 4px rgba(0,0,0,0.2);}input:checked+.isas-slider{background-color:#10b981;}input:checked+.isas-slider:before{transform:translateX(20px);}.select2-container--default .select2-selection--multiple{border:1px solid #cbd5e1!important;border-radius:8px!important;min-height:42px!important;padding:4px!important;}.select2-container--default.select2-container--focus .select2-selection--multiple{border-color:#3b82f6!important;box-shadow:0 0 0 3px rgba(59,130,246,0.1)!important;}.select2-container--default .select2-selection--single{border:1px solid #cbd5e1!important;border-radius:8px!important;height:42px!important;padding:6px 12px!important;background:#f8fafc!important;display:flex!important;align-items:center!important;box-sizing:border-box!important;}.select2-container--default .select2-selection--single .select2-selection__rendered{line-height:28px!important;font-size:14px!important;font-weight:700!important;color:#0f172a!important;padding-left:0!important;}.select2-container--default .select2-selection--single .select2-selection__arrow{height:40px!important;right:10px!important;}.select2-container--default.select2-container--focus .select2-selection--single{border-color:#3b82f6!important;box-shadow:0 0 0 3px rgba(59,130,246,0.1)!important;background:#ffffff!important;}.select2-dropdown{border:1px solid #cbd5e1!important;border-radius:8px!important;box-shadow:0 10px 25px rgba(0,0,0,0.1)!important;z-index:11000!important;}`);

        window.ISASClickHandler(".isas-action-btn", (e, btnAction) => {
            const cfg = isasConfig.action_modals[btnAction.id];
            if (!cfg) return;
            
            const ids = Array.from(document.querySelectorAll('input.action-select:checked')).map(cb => cb.value);
            if (cfg.require_selection && ((cfg.min_selection && ids.length < cfg.min_selection) || (cfg.max_selection && ids.length > cfg.max_selection))) {
                return showISASToast(cfg.error_msg, '⚠️', 'warning');
            }

            e.preventDefault(); e.stopImmediatePropagation();
            if (cfg.type === 'direct_action') {
                const execDirect = (extraPayload = {}) => {
                    if (cfg.action === 'send_vstupka') {
                        ids.forEach(id => {
                            const box = document.getElementById(`ai-action-box-${id}`);
                            if (box) box.innerHTML = '<span class="is-processing" style="color:#2563eb;font-weight:800;font-size:12px;">⏳ ODOSIELAM NA VRÁTNICU...</span>';
                        });
                    }
                    window.ISASAction({ type: cfg.action, ids, ...extraPayload }, btnAction, { loadingText: cfg.loading_text || "⏳...", successText: cfg.success_text || "✅ HOTOVO", reload: cfg.reload });
                };
                const act = (cfg.action || btnAction.dataset.action || btnAction.id || '').toLowerCase();
                
                // 🛡️ 1. Dedikovaný GDPR Anonymizačný Modal (Hard Confirm)
                if (act.includes('anonymiz') || act === 'gdpr_anonymize') {
                    if (typeof window.showGdprHardConfirmModal === 'function') {
                        return window.showGdprHardConfirmModal(ids.length, (reason) => execDirect({ reason }));
                    }
                    return window.showGlobalConfirmModal("Anonymizácia dát 🛡️", `Naozaj chcete anonymizovať ${ids.length} vybraných záznamov? Tento krok je nezvratný.`, () => execDirect());
                }
                // 🔓 2. Interceptor pre odomknutie
                if (act.includes('odomkn') || act.includes('unlock') || act === 'unlock_erp') {
                    return window.showGlobalConfirmModal("Odomknutie záznamov 🔓", `Naozaj chcete odomknúť ${ids.length} vybraných záznamov pre úpravy?`, () => execDirect());
                }
                // 🔒 3. Interceptor pre uzamknutie (Export do ERP)
                if (act.includes('zamkn') || act.includes('lock') || act === 'export_erp') {
                    return window.showGlobalConfirmModal("Uzamknutie záznamov 🔒", `Naozaj chcete uzamknúť ${ids.length} vybraných záznamov (Export do ERP)?`, () => execDirect());
                }
                // 🛑 4. Interceptor pre Storno a Zrušenie
                if (act.includes('storno') || act.includes('cancel') || act.includes('zrus') || act === 'mark_storno') {
                    return window.showGlobalConfirmModal("Stornovanie záznamov 🛑", `Naozaj chcete stornovať / zrušiť ${ids.length} vybraných položiek?`, () => execDirect());
                }
                // 🔄 5. Interceptor pre Reset stavov
                if (act.includes('reset')) {
                    return window.showGlobalConfirmModal("Resetovanie hodnôt 🔄", `Naozaj chcete resetovať stav pre ${ids.length} vybraných záznamov?`, () => execDirect());
                }
                // 📤 6. Interceptor pre Odoslanie (Faktúry, Peppol)
                if (act.includes('send') || act.includes('odosl') || act === 'mark_sent' || act === 'send_peppol') {
                    return window.showGlobalConfirmModal("Odoslanie záznamov 📤", `Naozaj chcete odoslať ${ids.length} vybraných záznamov?`, () => execDirect());
                }
                // 💳 7. Interceptor pre Platby
                if (act.includes('pay') || act.includes('plat') || act.includes('uhrad') || act === 'mark_paid') {
                    return window.showGlobalConfirmModal("Úhrada záznamov 💳", `Naozaj chcete označiť ${ids.length} vybraných záznamov ako zaplatené?`, () => execDirect());
                }
                // 🧾 8. Interceptor pre Zbernú faktúru
                if (act.includes('merge') || act.includes('zbern') || act === 'merge_invoice') {
                    return window.showGlobalConfirmModal("Zberná faktúra 🧾", `Naozaj chcete vygenerovať zbernú faktúru z ${ids.length} vybraných záznamov?`, () => execDirect());
                }
                // 🤖 9. Interceptor pre AI Spracovanie
                if (act.includes('ai') || act === 'ai_process') {
                    return window.showGlobalConfirmModal("AI Spracovanie 🤖", `Naozaj chcete odoslať ${ids.length} vybraných záznamov na analýzu umelej inteligencii?`, () => execDirect());
                }
                // ⚠️ 10. Všeobecné voliteľné potvrdenie (Fallback)
                if (cfg.confirm) {
                    return window.showGlobalConfirmModal(cfg.confirm_title || "Potvrdenie akcie ⚠️", cfg.confirm_text || `Naozaj chcete vykonať túto akciu pre ${ids.length} záznamov?`, () => execDirect());
                }
                return execDirect();
            }

            btnAction.classList.add("is-processing");

            const modalConfig = {
                maxWidth: cfg.type === 'ajax_select_modal' ? "550px" : (cfg.type === 'convert_to_task' ? "450px" : "420px"),
                icon: cfg.icon || (cfg.type === 'convert_to_task' ? '⚙️' : '🔗'),
                iconStyle: cfg.type === 'convert_to_task' ? "background:#ecfdf5;width:70px;height:70px;line-height:70px;border-radius:50%;margin:0 auto 15px auto;" : "background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 100%);width:80px;height:80px;line-height:80px;border-radius:50%;margin:0 auto 15px auto;box-shadow:0 10px 20px rgba(59,130,246,0.15);",
                title: cfg.title, text: cfg.subtitle, textAlign: "center",
                buttons: [{
                    id: "modal-confirm-btn", text: cfg.confirm_text || 'Potvrdiť', style: "flex:1;background:linear-gradient(135deg,#3b82f6 0%,#1d4ed8 100%);color:white;border:none;padding:14px;border-radius:10px;font-size:13px;font-weight:800;cursor:pointer;text-transform:uppercase;box-shadow:0 8px 15px rgba(59,130,246,0.25);transition:all 0.2s;",
                    onClick: (e, m, btn) => {
                        let payload = { type: cfg.action, ids };
                        
                        if (cfg.type === 'ajax_select_modal') {
                            const selectedIds = django.jQuery('#pair-quote-select').val();
                            if (!selectedIds?.length) return showISASToast("Najskôr musíte vybrať aspoň jednu položku.", "⚠️", "warning");
                            payload.quote_ids = selectedIds;
                        } else if (cfg.type === 'multi_checkbox_modal') {
                            cfg.checkboxes?.forEach(chk => payload[chk.id] = m.card.querySelector('#' + chk.id).checked);
                        } else if (cfg.type === 'confirm_checkbox_modal') {
                            payload[cfg.checkbox_id || 'checkbox_val'] = m.card.querySelector('#modal-custom-checkbox').checked;
                        } else if (cfg.type === 'convert_to_task') {
                            Object.assign(payload, {
                                datum_cas: m.card.querySelector("#conv-datetime").value,
                                technici_ids: (django.jQuery('#conv-tech-select').val() || []).map(Number),
                                typ_servisu_ids: (django.jQuery('#conv-typy-prac-select').val() || []).map(Number),
                                poznamka: m.card.querySelector("#conv-poznamka").value,
                                notif_tech_tg: m.card.querySelector("#conv-notif-tech-tg")?.checked ?? true,
                                notif_tech_mail: m.card.querySelector("#conv-notif-tech-mail")?.checked ?? true,
                                notif_tech_discord: m.card.querySelector("#conv-notif-tech-discord")?.checked ?? true,
                                notif_jit_tg: m.card.querySelector("#conv-notif-jit-tg")?.checked ?? true,
                                notif_jit_mail: m.card.querySelector("#conv-notif-jit-mail")?.checked ?? true,
                                notif_jit_discord: m.card.querySelector("#conv-notif-jit-discord")?.checked ?? true
                            });
                        }

                        window.ISASAction(payload, btn, {
                            loadingText: "⏳ SPRACOVÁVAM...",
                            onComplete: (data, actionBtn, origHtml) => {
                                m.close();
                                if(data.status === "success") {
                                    showISASToast(cfg.success_msg || data.message, '✅', 'success');
                                    btnAction.innerHTML = "✅ HOTOVO";
                                    if (cfg.reload !== false) {
                                        setTimeout(() => data.redirect_url ? window.location.href = data.redirect_url : window.location.reload(), data.redirect_url ? 800 : 1500);
                                    }
                                } else {
                                    showISASToast(data.message, '❌', 'error');
                                    btnAction.classList.remove("is-processing");
                                }
                            }
                        });
                    }
                }],
                onClose: () => btnAction.classList.remove("is-processing")
            };

            if (cfg.type === 'ajax_select_modal') {
                modalConfig.fields = [{ type: 'select', id: 'pair-quote-select', multiple: true, ajaxUrl: cfg.ajax_url, placeholder: cfg.placeholder || 'Vyhľadajte...', minInput: 2 }];
            } else if (cfg.type === 'multi_checkbox_modal') {
                modalConfig.checkboxes = cfg.checkboxes;
            } else if (cfg.type === 'confirm_checkbox_modal') {
                modalConfig.checkboxes = [{ id: 'modal-custom-checkbox', checked: true, label: cfg.checkbox_label, sublabel: cfg.checkbox_sublabel }];
            } else if (cfg.type === 'convert_to_task') {
                const zajtra = new Date(); zajtra.setDate(zajtra.getDate() + 1); zajtra.setHours(8, 0, 0, 0);
                const localISOTime = new Date(zajtra.getTime() - (zajtra.getTimezoneOffset() * 60000)).toISOString().slice(0, 16);
                const helpText = cfg.service_types_help_text || '💡 Tieto typy prác boli automaticky predvyplnené z podkladov. Môžete ich ľubovoľne upraviť, zmazať alebo doplniť pred vygenerovaním úlohy.';
                
                // Dynamická detekcia aktuálnej cesty v admine (napr. /admin/Evidencia/prijataobjednavka/ alebo /admin/Evidencia/cenovaponuka/)
                const basePath = window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/';
                const apiUrlTechnici = basePath + 'api-technici/';
                const apiUrlTypyServisu = basePath + 'api-typy-servisu/';

                modalConfig.fields = [
                    { type: 'datetime-local', id: 'conv-datetime', label: '📅 Dátum a čas', value: localISOTime },
                    { type: 'select', id: 'conv-tech-select', label: '👨‍🔧 Priradiť technikov', multiple: true, ajaxUrl: apiUrlTechnici, placeholder: "Vyhľadajte a vyberte technikov...", allowClear: true },
                    { type: 'select', id: 'conv-typy-prac-select', label: '🛠️ Typy servisu / prác', multiple: true, ajaxUrl: apiUrlTypyServisu, placeholder: "Zvoľte servisné práce a úkony...", allowClear: true },
                    { type: 'html', html: `<div style="font-size:11px;color:#64748b;margin-top:-6px;margin-bottom:14px;line-height:1.4;background:#f8fafc;padding:8px 10px;border-radius:8px;border:1px dashed #cbd5e1;">${helpText}</div>` },
                    { type: 'textarea', id: 'conv-poznamka', label: '📝 Bližšie informácie (Voliteľné)', placeholder: 'Zadajte doplňujúce informácie pre technika na výjazd...', rows: 3 },
                    { type: 'html', html: '<div style="font-size:11px;font-weight:900;color:#64748b;text-transform:uppercase;margin-bottom:10px;border-bottom:1px solid #e2e8f0;padding-bottom:4px;">📱 Notifikácie pre technikov (Nová úloha)</div>' },
                    { type: 'toggle', id: 'conv-notif-tech-tg', label: 'Telegram', checked: true },
                    { type: 'toggle', id: 'conv-notif-tech-mail', label: 'E-mail', checked: true },
                    { type: 'toggle', id: 'conv-notif-tech-discord', label: 'Discord', checked: true },
                    { type: 'html', html: '<div style="font-size:11px;font-weight:900;color:#64748b;text-transform:uppercase;margin-bottom:10px;border-bottom:1px solid #e2e8f0;padding-bottom:4px;margin-top:15px;">📦 Notifikácie pre majiteľa (Chýbajúci materiál)</div>' },
                    { type: 'toggle', id: 'conv-notif-jit-tg', label: 'Telegram', checked: true },
                    { type: 'toggle', id: 'conv-notif-jit-mail', label: 'E-mail', checked: true },
                    { type: 'toggle', id: 'conv-notif-jit-discord', label: 'Discord', checked: true }
                ];
            }
            window.ISASModalEngine(modalConfig);

            // 🚀 BLESKOVÉ PREDVYPLNENIE SELECT2 BUBLINIEK Z DÁT RIADKU (0 ms lag)
            if (cfg.type === 'convert_to_task' && ids.length === 1) {
                setTimeout(() => {
                    const rowSpan = document.querySelector(`.isas-row-service-types[data-id="${ids[0]}"]`);
                    if (rowSpan) {
                        try {
                            const typyData = JSON.parse(rowSpan.dataset.typy || '[]');
                            const selectEl = django.jQuery('#conv-typy-prac-select');
                            if (selectEl.length && typyData.length) {
                                selectEl.empty();
                                typyData.forEach(item => {
                                    const opt = new Option(item.text, item.id, true, true);
                                    selectEl.append(opt);
                                });
                                selectEl.trigger('change');
                            }
                        } catch (err) {
                            console.warn("ISAS: Chyba pri predvyplnení typov servisu:", err);
                        }
                    }
                }, 50);
            }
        });
    }

    // F3. TWO CHOICE MODAL
    if (isasConfig.two_choice_modal) {
        const cfg = isasConfig.two_choice_modal;
        window.ISASClickHandler("#" + cfg.trigger_id, (e, btn) => {
            if (btn.classList.contains("is-processing")) return;
            e.preventDefault();
            const ids = Array.from(document.querySelectorAll('input.action-select:checked')).map(cb => cb.value);
            if (!ids.length) return showISASToast(cfg.empty_selection_msg, "📭", "warning");
            
            const origHtml = btn.innerHTML; btn.classList.add("is-processing");
            
            const exec = (val, m) => {
                m.updateContent({ customHtml: '<div class="isas-spinner"></div>', title: "Spracovávam...", showClose: false });
                window.ISASAction({ type: cfg.action_type, ids, [cfg.action_param_name]: val }, null, {
                    onComplete: (data) => { 
                        m.close(); 
                        if(data.status === "success") { showISASToast(data.message, "✅"); btn.innerHTML = "✅ SPUSTENÉ"; } 
                        else { showISASToast(data.message, "❌", "error"); btn.classList.remove("is-processing"); btn.innerHTML = origHtml; } 
                    }
                });
            };

            window.ISASModalEngine({
                icon: cfg.icon, iconStyle: "background:#f0fdf4;width:85px;height:85px;line-height:85px;border-radius:50%;border:1px solid #bbf7d0;margin:0 auto 20px auto;",
                title: cfg.title, text: cfg.subtitle, buttonsDirection: "column",
                buttons: [
                    { id: cfg.btn1_id, text: cfg.btn1_text, style: `${cfg.btn1_style} padding:14px 20px;border-radius:12px;font-weight:800;cursor:pointer;text-transform:uppercase;transition:all 0.2s;`, onClick: (e, m) => exec(cfg.btn1_action_val, m) },
                    { id: cfg.btn2_id, text: cfg.btn2_text, style: `${cfg.btn2_style} padding:14px 20px;border-radius:12px;font-weight:800;cursor:pointer;text-transform:uppercase;transition:all 0.2s;`, onClick: (e, m) => exec(cfg.btn2_action_val, m) }
                ],
                onClose: () => { btn.classList.remove("is-processing"); btn.innerHTML = origHtml; }
            });
        });
    }

    // I. PEPPOL VIZUALIZÁCIA MODAL
    if (isasConfig.peppol_viz_modal || document.querySelector('.isas-peppol-viz-btn')) {
        window.ISASClickHandler(".isas-peppol-viz-btn", (e, btn) => {
            e.preventDefault();
            e.stopPropagation();
            const id = btn.dataset.id;
            const url = btn.dataset.url || (btn.getAttribute('href') && btn.getAttribute('href') !== '#' ? btn.getAttribute('href') : null);
            
            const openModal = (pdfUrl) => window.ISASModalEngine({
                icon: "📄", iconStyle: "background:#eff6ff;width:80px;height:80px;line-height:80px;border-radius:50%;margin:0 auto 15px auto;",
                title: "PDF Vizualizácia pripravená", text: "Vyberte akciu pre vygenerovaný dokument.", buttonsDirection: "column",
                buttons: [
                    { id: "viz-view-btn", text: "👁️ Zobraziť PDF", style: "background:#f1f5f9;color:#0f172a;border:1px solid #cbd5e1;padding:14px;border-radius:10px;font-size:13px;font-weight:800;cursor:pointer;text-transform:uppercase;transition:all 0.2s;", onClick: () => window.open(pdfUrl, '_blank') },
                    { id: "viz-email-btn", text: "📧 Odoslať účtovníčke", style: "background:linear-gradient(135deg,#3b82f6 0%,#1d4ed8 100%);color:white;border:none;padding:14px;border-radius:10px;font-size:13px;font-weight:800;cursor:pointer;text-transform:uppercase;box-shadow:0 8px 15px rgba(59,130,246,0.25);transition:all 0.2s;", onClick: (e, m, b) => window.ISASAction({type: 'send_peppol_email', ids: [id]}, b, { onComplete: (d, b, o) => d.status === "success" ? m.close() : (b.innerHTML = o, b.style.pointerEvents = "auto") }) }
                ], closeText: "Zavrieť okno"
            });

            if (url) openModal(url);
            else window.ISASAction({type: 'generate_peppol_pdf', ids: [id]}, btn, { onComplete: (d, b, o) => { 
                b.classList.remove("is-processing");
                b.style.pointerEvents = "auto";
                if(d.status === "success"){ 
                    b.innerHTML = '<span style="margin-right: 6px; font-size: 12px;">👁️</span><span>ZOBRAZIŤ PDF</span>'; 
                    b.dataset.url = d.pdf_url; 
                    if (b.tagName === 'A') b.href = d.pdf_url; 
                    Object.assign(b.style, { 
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        textAlign: "center",
                        alignSelf: "flex-start",
                        background: "#eff6ff", 
                        borderColor: "#bfdbfe", 
                        color: "#1d4ed8", 
                        opacity: "1", 
                        pointerEvents: "auto",
                        textDecoration: "none"
                    }); 
                    openModal(d.pdf_url); 
                } else { 
                    b.innerHTML = o; 
                } 
            }});
        });
    }

    // L. EXPORT EXCEL MODAL
    if (isasConfig.export_excel_modal) {
        const cfg = isasConfig.export_excel_modal;
        window.ISASClickHandler("#" + cfg.trigger_id, (e, btn) => {
            if (btn.classList.contains("is-processing")) return;
            e.preventDefault();
            const ids = Array.from(document.querySelectorAll('input.action-select:checked')).map(cb => cb.value);
            if (!ids.length) return showISASToast("Najskôr zaškrtnite aspoň jeden záznam.", "📭", "warning");

            const fmt = d => [d.getFullYear(), String(d.getMonth()+1).padStart(2,'0'), String(d.getDate()).padStart(2,'0')].join('-');
            const now = new Date();

            window.ISASModalEngine({
                icon: "📅", title: "Parametre exportu", text: `Zvoľte obdobie pre vygenerovanie reportu pre ${ids.length} záznamov.`,
                fields: [
                    { type: 'html', html: `<div class="isas-preset-row"><button type="button" class="isas-preset-btn active" data-preset="this_month">Tento mesiac</button><button type="button" class="isas-preset-btn" data-preset="last_month">Minulý mesiac</button><button type="button" class="isas-preset-btn" data-preset="this_year">Tento rok</button><button type="button" class="isas-preset-btn" data-preset="last_year">Minulý rok</button><button type="button" class="isas-preset-btn" data-preset="last_12">Posledných 12 mesiacov</button></div>` },
                    { type: 'row', fields: [
                        { type: 'date', id: 'exp-od', label: 'OD:', value: fmt(new Date(now.getFullYear(), now.getMonth(), 1)) },
                        { type: 'date', id: 'exp-do', label: 'DO:', value: fmt(now) }
                    ]}
                ],
                buttons: [{ id: "btn-start-generate", text: "🚀 Spustiť generovanie", className: "isas-modal-btn isas-btn-send", onClick: (e, m) => {
                    const od = m.card.querySelector('#exp-od').value, do_ = m.card.querySelector('#exp-do').value;
                    if (!od || !do_ || new Date(od) > new Date(do_)) return showISASToast("Neplatný rozsah dátumov.", "⚠️", "error");
                    window.ISASAsyncExport(cfg, { type: cfg.generate_action, ids, datum_od: od, datum_do: do_ }, m);
                }}],
                onRender: (m) => {
                    const btns = m.card.querySelectorAll('.isas-preset-btn');
                    btns.forEach(b => b.onclick = function() {
                        btns.forEach(x => x.classList.remove('active')); this.classList.add('active');
                        const p = this.dataset.preset, n = new Date();
                        const dates = {
                            'this_month': [new Date(n.getFullYear(), n.getMonth(), 1), n],
                            'last_month': [new Date(n.getFullYear(), n.getMonth()-1, 1), new Date(n.getFullYear(), n.getMonth(), 0)],
                            'this_year': [new Date(n.getFullYear(), 0, 1), n],
                            'last_year': [new Date(n.getFullYear()-1, 0, 1), new Date(n.getFullYear()-1, 11, 31)],
                            'last_12': [new Date(n.getFullYear()-1, n.getMonth(), n.getDate()), n]
                        }[p];
                        if (dates) { m.card.querySelector('#exp-od').value = fmt(dates[0]); m.card.querySelector('#exp-do').value = fmt(dates[1]); }
                    });
                }
            });
        });
    }

    // D2. DYNAMICKÉ ŠTÝLOVANIE RIADKOV
    if (isasConfig.row_styles && isasConfig.row_styles.length > 0) {
        document.querySelectorAll('#result_list tbody tr').forEach(row => {
            isasConfig.row_styles.forEach(rule => {
                if (row.innerHTML.includes(rule.contains_text)) {
                    Object.assign(row.style, rule.styles);
                    row.style.transition = 'opacity 0.3s';
                    if (rule.hover_opacity) {
                        row.addEventListener('mouseenter', () => row.style.opacity = rule.hover_opacity);
                        row.addEventListener('mouseleave', () => row.style.opacity = rule.styles.opacity || '1');
                    }
                    if (rule.disable_links_with_text) {
                        row.querySelectorAll('a').forEach(btn => {
                            if (rule.disable_links_with_text.some(text => btn.innerText.includes(text))) {
                                btn.style.pointerEvents = 'none'; btn.style.cursor = 'not-allowed';
                            }
                        });
                    }
                }
            });
        });
    }

    // E. CHANGELIST FAST ACTION POLLING - Kompletne nahradené unifikovaným JSON pollerom

    // F2. CUSTOM SAVE ACTIONS & SUBMIT ROW OVERRIDES
    if (isasConfig.hide_submit_row) {
        const sr = document.querySelector('.submit-row'); if (sr) sr.style.display = 'none';
    }

    if (isasConfig.custom_save_action) {
        if (document.body.classList.contains('change-form')) {
            const changeForm = document.getElementById("content-main")?.querySelector("form:not(#changelist-form)");
            if (changeForm && !document.getElementById("isas-custom-save-btn")) {
                const cfg = isasConfig.custom_save_action;
                const btnContainer = document.createElement("div");
                
                btnContainer.style.cssText = "padding: 20px 0px; text-align: left; margin-bottom: 25px; background: rgba(248, 250, 252, 0.6); border-radius: 16px; border: 1px dashed #cbd5e1;";
                const customSaveBtn = document.createElement("button");
                customSaveBtn.type = "button"; customSaveBtn.className = "isas-action-btn btn-ai"; customSaveBtn.id = "isas-custom-save-btn"; customSaveBtn.innerHTML = cfg.button_text;
                customSaveBtn.style.cssText = "display: inline-flex; align-items: center; justify-content: center; gap: 10px; height: 50px; padding: 0 40px; font-size: 14px; border-radius: 12px; color: white; background: linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%); border: none; font-weight: 900; cursor: pointer; box-shadow: 0 4px 15px rgba(139, 92, 246, 0.4); text-transform: uppercase;";
                btnContainer.appendChild(customSaveBtn); changeForm.insertBefore(btnContainer, changeForm.firstChild);

                customSaveBtn.addEventListener('click', e => {
                    e.preventDefault();
                    window.ISASModalEngine({
                        icon: '⚠️', title: cfg.title, text: cfg.text,
                        buttons: [{
                            id: "btn-custom-save-confirm", text: cfg.confirm_text, style: "background: linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%); color: white; border: none; padding: 14px 25px; border-radius: 12px; font-size: 14px; font-weight: 800; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 15px rgba(139, 92, 246, 0.4); text-transform: uppercase;",
                            onClick: (e, m) => {
                                m.close();
                                customSaveBtn.innerHTML = "⏳ Odosielam..."; customSaveBtn.style.pointerEvents = "none";
                                const actionInput = document.createElement("input"); actionInput.type = "hidden"; actionInput.name = "isas_po_ulozeni_akcia"; actionInput.value = cfg.action_value;
                                changeForm.appendChild(actionInput); changeForm.submit();
                            }
                        }]
                    });
                });
            }
        }
    }

    // E. KOREKCIA MOTOHODÍN MODAL
    if (isasConfig.korekcia_mh_modal) {
        window.ISASStyleEngine('isas-korekcia-mh-css', `.isas-swal-alert{background:#fff1f2;border-left:4px solid #e11d48;padding:14px;border-radius:8px;display:flex;gap:12px;text-align:left;margin-bottom:24px;align-items:center;}.isas-swal-alert p{margin:0;font-size:13px;color:#9f1239;line-height:1.5;}.isas-swal-group{text-align:left;margin-bottom:16px;}.isas-swal-row{display:flex;gap:16px;margin-bottom:16px;}.isas-swal-row .isas-swal-group{flex:1;margin-bottom:0;}.isas-swal-label-row{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:8px;}.isas-swal-label-row label{display:block;font-size:12px;font-weight:800;color:#475569;text-transform:uppercase;letter-spacing:0.5px;margin:0;}.isas-swal-original-val{font-size:11px;color:#94a3b8;font-weight:600;background:#f1f5f9;padding:2px 6px;border-radius:4px;}.isas-swal-group .req{color:#e11d48;font-size:14px;}.isas-swal-group input,.isas-swal-group textarea{width:100%;box-sizing:border-box;padding:12px 16px;border:1px solid #cbd5e1;border-radius:10px;font-size:15px;font-weight:700;color:#0f172a;background:#f8fafc;transition:all 0.2s ease;font-family:inherit;text-align:left!important;}.isas-swal-group input:focus,.isas-swal-group textarea:focus{outline:none;border-color:#3b82f6;box-shadow:0 0 0 4px rgba(59,130,246,0.15);background:#ffffff;}.isas-swal-group textarea{resize:vertical;min-height:90px;font-weight:500;}.field-rychle_akcie_zariadenia label,.field-pridat_zariadenie_tlacidlo label{display:none!important;}.field-rychle_akcie_zariadenia .readonly,.field-pridat_zariadenie_tlacidlo .readonly{margin-left:0!important;padding-left:0!important;border:none!important;}`);

        window.showKorekciaModal = (url, zariadenie_id, mh, zataz) => {
            const fmt = v => new Intl.NumberFormat(document.documentElement.lang || navigator.language || undefined).format(v || 0);
            window.ISASModalEngine({
                maxWidth: "600px", icon: "⚠️", title: "KOREKCIA MOTOHODÍN", text: "",
                fields: [
                    { type: 'alert', icon: '🚨', text: 'Túto akciu použite <b>výhradne v prípade chyby</b>. Každá zmena je trvalo zaznamenaná v bezpečnostnom audite.' },
                    { type: 'row', fields: [
                        { type: 'number', id: 'swal-mh', label: `Celkové MH <span style="color:#e11d48">*</span> <span style="float:right;font-size:10px;color:#94a3b8;background:#f1f5f9;padding:2px 6px;border-radius:4px;">Pôvodne: ${fmt(mh)}</span>`, min: 0, step: 1, value: mh },
                        { type: 'number', id: 'swal-zataz', label: `MH v záťaži <span style="float:right;font-size:10px;color:#94a3b8;background:#f1f5f9;padding:2px 6px;border-radius:4px;">Pôvodne: ${fmt(zataz)}</span>`, min: 0, step: 1, value: zataz }
                    ]},
                    { type: 'textarea', id: 'swal-dovod', label: 'Dôvod korekcie <span style="color:#e11d48">*</span>', placeholder: 'Podrobne popíšte dôvod zmeny...' }
                ],
                buttons: [{
                    id: "btn-confirm-korekcia", text: "Potvrdiť a prepísať", style: "background:linear-gradient(135deg,#e11d48 0%,#be123c 100%);color:white;border:none;padding:12px 24px;border-radius:10px;font-weight:800;font-size:13px;text-transform:uppercase;letter-spacing:0.5px;cursor:pointer;box-shadow:0 4px 6px -1px rgba(225,29,72,0.3);transition:all 0.2s;",
                    onClick: (e, m, btn) => {
                        const n_mh = m.card.querySelector('#swal-mh').value, n_zat = m.card.querySelector('#swal-zataz').value, dov = m.card.querySelector('#swal-dovod').value;
                        if (!n_mh || !dov.trim()) return showISASToast('Stav počítadla a dôvod sú povinné!', '⚠️', 'warning');
                        if (parseInt(n_mh) < 0 || (n_zat && parseInt(n_zat) < 0)) return showISASToast('Hodnota počítadla nemôže byť záporná!', '⚠️', 'warning');
                        
                        window.ISASAction({ type: 'korekcia_mh', ids: [zariadenie_id], mh: n_mh, zataz: n_zat, dovod: dov }, btn, {
                            url, loadingText: "⏳ Spracovávam...",
                            onComplete: (data, b, origHtml) => {
                                if (data.status === 'success') { m.close(); showISASToast(data.message, '✅', 'success'); setTimeout(() => window.location.reload(), 1500); }
                                else { showISASToast(data.message, '❌', 'error'); b.classList.remove('is-processing'); b.innerHTML = origHtml; }
                            }
                        });
                    }
                }]
            });
        };

        window.showPresunPrenajmuModal = (url, zariadenie_id, zakaznik_id, zakaznik_nazov, ajax_url, zariadenie_nazov, vyrobne_cislo, povodne_umiestnenie, je_v_prenajme, device_term, current_mh, meter_label, meter_unit, otvorene_ulohy_pocet) => {
            const isRented = je_v_prenajme !== false && je_v_prenajme !== 'false';
            const termLabel = (device_term || 'zariadenia').toUpperCase();
            const modalTitle = isRented ? `PRESUN PRENAJATÉHO ${termLabel}` : `NASADENIE ${termLabel} DO PRENÁJMU`;
            const mLabel = meter_label || 'Počítadlo';
            const mUnit = meter_unit || 'hod.';
            
            const infoDeviceHtml = zariadenie_nazov ? `<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px;margin-bottom:14px;text-align:left;"><div style="font-size:13px;font-weight:900;color:#0f172a;">⚙️ ${zariadenie_nazov}</div><div style="font-size:11px;font-weight:700;color:#64748b;margin-top:3px;">S/N: ${vyrobne_cislo || 'Bez S/N'}${povodne_umiestnenie ? ` | 📍 ${povodne_umiestnenie}` : ''} | ⏱️ ${mLabel}: ${current_mh || 0} ${mUnit}</div></div>` : '';
            const partnerInfo = isRented ? `Aktuálny nájomca: <b>${zakaznik_nazov || 'Nezadaný'}</b>` : `Aktuálny stav: <b>🟢 Na sklade (Dostupné ihneď)</b>`;
            const ulohyVarovanieHtml = (otvorene_ulohy_pocet && parseInt(otvorene_ulohy_pocet) > 0)
                ? `<div style="background:#fffbeb;border-left:4px solid #f59e0b;padding:10px 14px;border-radius:8px;text-align:left;margin-top:10px;margin-bottom:6px;display:flex;align-items:center;gap:10px;"><span style="font-size:20px;">⚠️</span><div style="font-size:12px;color:#92400e;line-height:1.4;"><b>Evidujeme ${otvorene_ulohy_pocet} otvorených servisných úloh</b> u pôvodného partnera. Presunom zariadenia zostanú tieto zákazky priradené pôvodnému zákazníkovi.</div></div>`
                : '';
            const headerText = `${infoDeviceHtml}${partnerInfo}${ulohyVarovanieHtml}<br><span style="font-size:12px;color:#64748b;">${isRented ? 'Zvoľte nového partnera alebo vráťte zariadenie na centrálu.' : 'Vyberte cieľového partnera pre vyskladnenie do nájmu.'}</span>`;

            // 🕒 Dynamický dnešný dátum bez hardkódu
            const now = new Date();
            const todayStr = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'), String(now.getDate()).padStart(2, '0')].join('-');

            const modalFields = [];
            if (isRented) {
                modalFields.push({ type: 'toggle', id: 'presun-vratit-centrala', label: '📥 Vrátiť na centrálu / sklad (Ukončiť prenájom)', checked: false });
            }
            modalFields.push({
                type: 'select',
                id: 'presun-novy-zakaznik',
                label: '🏢 Cieľový nájomca / partner <span style="color:#e11d48">*</span>',
                ajaxUrl: ajax_url ? `${ajax_url}?exclude_id=${zakaznik_id || 0}` : null,
                placeholder: "Vyhľadajte firmu podľa názvu, mesta alebo IČO...",
                minInput: 0,
                allowClear: true,
                options: [{ value: '', text: '' }]
            });
            modalFields.push({
                type: 'row',
                fields: [
                    { type: 'date', id: 'presun-datum', label: '📅 Dátum presunu <span style="color:#e11d48">*</span>', value: todayStr },
                    { type: 'number', id: 'presun-stav-mh', label: `⏱️ Stav ${mLabel} (${mUnit})`, value: current_mh !== undefined && current_mh !== null ? current_mh : '', min: 0, step: 1, placeholder: 'Aktuálny stav...' }
                ]
            });
            modalFields.push({
                type: 'row',
                fields: [
                    { type: 'text', id: 'presun-protokol', label: '📄 Číslo zmluvy / protokolu', placeholder: 'Napr. PZ-2025/001' },
                    { type: 'text', id: 'presun-umiestnenie', label: '📍 Nové umiestnenie', placeholder: povodne_umiestnenie ? `Pôvodné: ${povodne_umiestnenie}` : 'Napr. Hala A / Výroba' }
                ]
            });
            modalFields.push({
                type: 'textarea',
                id: 'presun-poznamka',
                label: '📝 Doplňujúca poznámka k presunu (voliteľné)',
                placeholder: 'Dôvod presunu, technický stav pri odovzdaní, príslušenstvo...',
                rows: 2
            });

            window.ISASModalEngine({
                maxWidth: "600px",
                icon: isRented ? "🤝" : "🚀",
                iconStyle: isRented ? "background:linear-gradient(135deg,#fef3c7 0%,#fde68a 100%);width:70px;height:70px;line-height:70px;border-radius:50%;margin:0 auto 15px auto;box-shadow:0 8px 16px rgba(245,158,11,0.2);font-size:32px;" : "background:linear-gradient(135deg,#ecfdf5 0%,#d1fae5 100%);width:70px;height:70px;line-height:70px;border-radius:50%;margin:0 auto 15px auto;box-shadow:0 8px 16px rgba(16,185,129,0.2);font-size:32px;",
                title: modalTitle,
                text: headerText,
                fields: modalFields,
                buttons: [{
                    id: "btn-confirm-presun",
                    text: isRented ? "Potvrdiť presun" : "Potvrdiť nasadenie",
                    style: isRented ? "background:linear-gradient(135deg,#f59e0b 0%,#d97706 100%);color:white;border:none;padding:14px 24px;border-radius:10px;font-weight:800;font-size:13px;text-transform:uppercase;letter-spacing:0.5px;cursor:pointer;box-shadow:0 4px 12px rgba(217,119,6,0.3);transition:all 0.2s;" : "background:linear-gradient(135deg,#10b981 0%,#059669 100%);color:white;border:none;padding:14px 24px;border-radius:10px;font-weight:800;font-size:13px;text-transform:uppercase;letter-spacing:0.5px;cursor:pointer;box-shadow:0 4px 12px rgba(16,185,129,0.3);transition:all 0.2s;",
                    onClick: (e, m, btn) => {
                        const vratitCentrala = m.card.querySelector('#presun-vratit-centrala')?.checked || false;
                        const novyZakaznikId = vratitCentrala ? null : (typeof django !== 'undefined' && django.jQuery ? django.jQuery('#presun-novy-zakaznik').val() : m.card.querySelector('#presun-novy-zakaznik')?.value);
                        const datumPresunu = m.card.querySelector('#presun-datum')?.value || '';
                        const stavMh = m.card.querySelector('#presun-stav-mh')?.value;
                        const cisloProtokolu = m.card.querySelector('#presun-protokol')?.value || '';
                        const umiestnenie = m.card.querySelector('#presun-umiestnenie')?.value || '';
                        const poznamka = m.card.querySelector('#presun-poznamka')?.value || '';

                        if (!vratitCentrala && !novyZakaznikId) {
                            return showISASToast('Zvoľte cieľového partnera alebo zvoľte vrátenie na centrálu!', '⚠️', 'warning');
                        }
                        if (!datumPresunu) {
                            return showISASToast('Zadajte dátum presunu!', '⚠️', 'warning');
                        }
                        if (stavMh !== undefined && stavMh !== '') {
                            const stavMhInt = parseInt(stavMh);
                            if (stavMhInt < 0) {
                                return showISASToast('Stav počítadla nemôže byť záporný!', '⚠️', 'warning');
                            }
                            if (current_mh !== undefined && current_mh !== null && stavMhInt < parseInt(current_mh)) {
                                return showISASToast(`Nový stav (${stavMhInt} ${mUnit}) nemôže byť nižší ako aktuálny (${current_mh} ${mUnit})!`, '⚠️', 'warning');
                            }
                        }

                        window.ISASAction({
                            type: 'presun_prenajmu',
                            ids: [zariadenie_id],
                            cielovy_zakaznik_id: novyZakaznikId,
                            vratit_na_centralu: vratitCentrala,
                            datum_presunu: datumPresunu,
                            stav_mh: stavMh !== '' ? stavMh : null,
                            cislo_protokolu: cisloProtokolu,
                            nove_umiestnenie: umiestnenie,
                            poznamka: poznamka
                        }, btn, {
                            url: url,
                            loadingText: "⏳ Spracovávam...",
                            onComplete: (data, b, origHtml) => {
                                if (data.status === 'success') {
                                    m.close();
                                    showISASToast(data.message, '✅', 'success');
                                    setTimeout(() => window.location.reload(), 1200);
                                } else {
                                    showISASToast(data.message, '❌', 'error');
                                    b.classList.remove('is-processing');
                                    b.innerHTML = origHtml;
                                }
                            }
                        });
                    }
                }],
                onRender: (m) => {
                    const chkCentrala = m.card.querySelector('#presun-vratit-centrala');
                    const selWrapper = m.card.querySelector('#presun-novy-zakaznik-wrapper');
                    const umiestnenieInput = m.card.querySelector('#presun-umiestnenie');
                    
                    // 🛡️ Inicializácia šírky Select2 kontajnera
                    if (typeof django !== 'undefined' && django.jQuery) {
                        django.jQuery('#presun-novy-zakaznik').next('.select2-container').css('width', '100%');
                    }

                    if (chkCentrala) {
                        chkCentrala.addEventListener('change', (e) => {
                            const isChecked = e.target.checked;
                            if (selWrapper) selWrapper.style.display = isChecked ? 'none' : 'block';
                            if (umiestnenieInput) {
                                if (isChecked) {
                                    umiestnenieInput.dataset.origVal = umiestnenieInput.value;
                                    umiestnenieInput.value = 'Centrálny sklad / Dielňa';
                                    umiestnenieInput.placeholder = 'Centrálny sklad / Dielňa';
                                } else {
                                    umiestnenieInput.value = umiestnenieInput.dataset.origVal || '';
                                    umiestnenieInput.placeholder = povodne_umiestnenie ? `Pôvodné: ${povodne_umiestnenie}` : 'Napr. Hala A / Výroba';
                                }
                            }
                            if (!isChecked && typeof django !== 'undefined' && django.jQuery) {
                                django.jQuery('#presun-novy-zakaznik').next('.select2-container').css('width', '100%');
                            }
                        });
                    }
                }
            });
        };
    }

    // F4. ASYNC HTML ACTIONS (Povolenie na vstup a pod.)
    if (isasConfig.async_html_action) {
        const cfg = isasConfig.async_html_action;
        window.ISASClickHandler(cfg.trigger_selector, (e, btn) => {
            e.preventDefault();
            e.stopImmediatePropagation();
            if (btn.classList.contains("is-processing")) return;
            const id = btn.dataset.id;
            if (!id) return;
            
            window.ISASAction({ type: cfg.action_type, ids: [id] }, btn, {
                loadingText: cfg.loading_text || "⏳ GENERUJEM...",
                reload: false,
                onComplete: (data, b, origHtml) => {
                    if (data.status === "success" || data.status === "ok") {
                        // 🛡️ ISAS FIX: Ponecháme načítavací stav, aby ho Poller kontinuálne sledoval
                        b.innerHTML = cfg.loading_text || "⏳ GENERUJEM...";
                        b.classList.add("is-processing");
                        b.style.pointerEvents = "none";
                        showISASToast(data.message || "Generovanie spustené na pozadí...", "🤖", "info");
                    } else {
                        showISASToast(data.message || "Chyba pri spustení generovania.", "❌", "error");
                        b.classList.remove("is-processing");
                        b.style.pointerEvents = "auto";
                        b.innerHTML = origHtml;
                    }
                }
            });
        });
    }

    // =============================================================================
    // UNIFIKOVANÝ ISAS JSON POLLER (Zero-Refresh Engine pre celý systém)
    // =============================================================================
    if (isasConfig.auto_poll || isasConfig.protokol_poller || isasConfig.async_html_action || isasConfig.page_reload_poll) {
        
        // CSS animácia pre Megazálohy
        window.ISASStyleEngine('isas-pulse-green-css', `@keyframes pulseGreen { 0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.4); } 70% { box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); } 100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); } }`);

        const getBaseUrl = () => {
            const match = window.location.pathname.match(/^(.*\/admin\/[^\/]+\/[^\/]+\/)/);
            return match ? match[1] : window.location.pathname.replace(/\/$/, '') + '/';
        };

        const pollEngine = () => {
            const targets = [];
            
            // 🛡️ UNIFIKOVANÝ WATCHDOG: Jednotný limit 40 cyklov (~100 sekúnd) pre všetky dokumenty bez výnimky
            const registerPollTarget = (id, el, fields) => {
                let attempts = parseInt(el.dataset.pollAttempts || '0', 10) + 1;
                el.dataset.pollAttempts = attempts;

                if (attempts > 40) {
                    el.removeAttribute('data-status');
                    el.classList.remove('is-processing', 'isas-reload-poller', 'isas-protokol-poller');
                    const btn = el.querySelector('.is-processing') || el.querySelector('a, button');
                    if (btn) {
                        btn.classList.remove('is-processing');
                        btn.classList.add('error');
                        btn.style.pointerEvents = 'auto';
                    }
                    showISASToast("Spracovanie dokumentu trvá nezvyčajne dlho. Skontrolujte worker alebo obnovte stránku.", "⚠️", "warning");
                    return;
                }
                targets.push({ id, el, fields });
            };

            // 1. ERP a PDF odznaky
            document.querySelectorAll('[id^="isas-erp-badge-"][data-status="PROCESSING"], [id^="isas-pdf-badge-"][data-status="PROCESSING"]').forEach(el => {
                const id = el.id.split('-').pop();
                if (id && !isNaN(id)) registerPollTarget(id, el, ['erp_status_ikona', 'get_akcie_card', 'get_akcie', 'get_ai_a_subor']);
            });

            // 2. Protokoly
            document.querySelectorAll('.isas-protokol-poller').forEach(el => {
                const id = el.getAttribute('data-id');
                if (id) registerPollTarget(id, el, ['styled_protokol']);
            });

            // 3. Vstupky a AI Akcie
            document.querySelectorAll('[id^="ai-action-box-"]').forEach(el => {
                const isBusy = el.innerHTML.includes('GENERUJEM') || el.innerHTML.includes('ODOSIELAM') || el.querySelector('.is-processing');
                const isFinished = el.innerHTML.includes('Uzavreté') || (el.innerHTML.includes('ODOSLANÉ') && !el.innerHTML.includes('ODOSIELAM'));
                if (isBusy && !isFinished) {
                    const id = el.id.split('-').pop();
                    if (id) registerPollTarget(id, el, ['get_ai_action_button', 'get_ai_status_badge']);
                }
            });

            // 4. Prichodzie Faktury (AI Spracovanie)
            document.querySelectorAll('.isas-reload-poller').forEach(el => {
                const id = el.getAttribute('data-id');
                if (id) registerPollTarget(id, el, ['get_ai_a_subor']);
            });

            // 5. MEGAZÁLOHY (Watchdog na primárny badge, synchronizovaný zber buniek riadku)
            document.querySelectorAll('.isas-status-badge[data-status="vyziadana"], .isas-status-badge[data-status="spracovava_sa"]').forEach(badge => {
                const id = badge.getAttribute('data-id');
                if (!id) return;
                
                let attempts = parseInt(badge.dataset.pollAttempts || '0', 10) + 1;
                badge.dataset.pollAttempts = attempts;

                if (attempts > 40) {
                    badge.removeAttribute('data-status');
                    showISASToast("Generovanie systémovej zálohy trvá pridlho. Skontrolujte Celery worker.", "⚠️", "warning");
                    return;
                }

                targets.push({ id, el: badge, fields: ['get_stav_spracovania_badge'] });
                
                const row = badge.closest('tr');
                const actionCell = row ? row.querySelector(`.isas-action-cell[data-id="${id}"]`) : document.querySelector(`.isas-action-cell[data-id="${id}"]`);
                if (actionCell) targets.push({ id, el: actionCell, fields: ['get_stiahnut_link'] });
                
                const sizeCell = row ? row.querySelector(`.isas-size-cell[data-id="${id}"]`) : document.querySelector(`.isas-size-cell[data-id="${id}"]`);
                if (sizeCell) targets.push({ id, el: sizeCell, fields: ['get_retencia_a_velkost'] });
            });

            // 6. IMPORTY DÁT (Watchdog na primárny stavový box, synchronizovaný zber kariet)
            document.querySelectorAll('.isas-import-poller[data-status="SPRACOVAVA_SA"], .isas-import-poller[data-status="NAHRATE"]').forEach(el => {
                const id = el.id.split('-').pop();
                if (!id) return;

                let attempts = parseInt(el.dataset.pollAttempts || '0', 10) + 1;
                el.dataset.pollAttempts = attempts;

                if (attempts > 40) {
                    el.removeAttribute('data-status');
                    showISASToast("Spracovanie importu dát trvá pridlho. Skontrolujte Celery worker.", "⚠️", "warning");
                    return;
                }

                targets.push({ id, el: el, fields: ['get_stav_cas_card'] });
                
                const row = el.closest('tr');
                const infoCard = row ? row.querySelector(`[id="import-info-${id}"]`) : document.getElementById(`import-info-${id}`);
                if (infoCard) targets.push({ id, el: infoCard, fields: ['get_hlavne_info_card'] });
                
                const statCard = row ? row.querySelector(`[id="import-stat-${id}"]`) : document.getElementById(`import-stat-${id}`);
                if (statCard) targets.push({ id, el: statCard, fields: ['get_statistiky_card'] });
                
                const logCard = row ? row.querySelector(`[id="import-log-${id}"]`) : document.getElementById(`import-log-${id}`);
                if (logCard) targets.push({ id, el: logCard, fields: ['get_log_card'] });
            });

            if (targets.length === 0) return; 

            const requestMap = {};
            targets.forEach(t => {
                if (!requestMap[t.id]) requestMap[t.id] = { elements: [], fields: new Set() };
                requestMap[t.id].elements.push(t.el);
                t.fields.forEach(f => requestMap[t.id].fields.add(f));
            });

            const idsToPoll = Object.keys(requestMap);
            const allFields = [...new Set(targets.flatMap(t => t.fields))];

            fetch(getBaseUrl() + 'fast-action/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]')?.value || '' },
                body: JSON.stringify({ type: 'poll_global_status', ids: idsToPoll, fields: allFields })
            })
            .then(res => {
                if (!res.ok) return null;
                return res.json();
            })
            .then(data => {
                if (data.status === 'success' && data.results) {
                    let changed = false;
                    Object.entries(data.results).forEach(([id, fieldsData]) => {
                        const req = requestMap[id];
                        if (!req) return;

                        req.elements.forEach(el => {
                            let newHtml = null;
                            
                            if (el.id.includes('erp-badge') && fieldsData['erp_status_ikona']) {
                                newHtml = fieldsData['erp_status_ikona'];
                                const row = el.closest('tr');
                                const actionCell = row?.querySelector('.field-get_akcie_card') || row?.querySelector('.field-get_akcie');
                                if (actionCell && (fieldsData['get_akcie_card'] || fieldsData['get_akcie'])) {
                                    actionCell.innerHTML = fieldsData['get_akcie_card'] || fieldsData['get_akcie'];
                                }
                            }
                            else if (el.id.includes('pdf-badge')) {
                                newHtml = fieldsData['get_akcie_card'] || fieldsData['get_akcie'];
                            }
                            else if (el.classList.contains('isas-protokol-poller') && fieldsData['styled_protokol']) newHtml = fieldsData['styled_protokol'];
                            else if (el.id.includes('ai-action-box') && fieldsData['get_ai_action_button']) {
                                const btnHtml = fieldsData['get_ai_action_button'];
                                const isOdoslane = btnHtml.includes('ODOSLANÉ');
                                const isPripravene = btnHtml.includes('VSTUP PDF') && !el.innerHTML.includes('ODOSIELAM');

                                if (isOdoslane || isPripravene || btnHtml.includes('Uzavreté')) {
                                    newHtml = btnHtml;
                                    const row = el.closest('tr');
                                    const statusCell = row?.querySelector('.field-get_ai_status_badge');
                                    if (statusCell && fieldsData['get_ai_status_badge']) {
                                        statusCell.innerHTML = fieldsData['get_ai_status_badge'];
                                    }
                                    showISASToast(isOdoslane ? "📧 Povolenie bolo úspešne odoslané na vrátnicu!" : "✅ Náhľad vstupu pripravený na kontrolu!", isOdoslane ? "📤" : "📄", "success");
                                }
                            }
                            else if (el.classList.contains('isas-reload-poller') && fieldsData['get_ai_a_subor']) {
                                newHtml = fieldsData['get_ai_a_subor'];
                                // 🛡️ Bezpečné overenie: reload a toast spustíme LEN vtedy, keď AI reálne dokončila prácu
                                const jeUkoncene = (newHtml.includes('AI HOTOVO') || newHtml.includes('PEPPOL ✅')) && !newHtml.includes('isas-reload-poller');
                                if (jeUkoncene) {
                                    el.classList.remove('isas-reload-poller');
                                    showISASToast("✨ AI úspešne vyťažila údaje z faktúry!", "📄", "success");
                                    setTimeout(() => window.location.reload(), 800);
                                }
                            }
                            else if (el.classList.contains('isas-status-badge') && fieldsData['get_stav_spracovania_badge']) newHtml = fieldsData['get_stav_spracovania_badge'];
                            else if (el.classList.contains('isas-action-cell') && fieldsData['get_stiahnut_link']) newHtml = fieldsData['get_stiahnut_link'];
                            else if (el.classList.contains('isas-size-cell') && fieldsData['get_retencia_a_velkost']) newHtml = fieldsData['get_retencia_a_velkost'];
                            // Mapovanie pre Importy
                            else if (el.id.includes('import-stav') && fieldsData['get_stav_cas_card']) newHtml = fieldsData['get_stav_cas_card'];
                            else if (el.id.includes('import-info') && fieldsData['get_hlavne_info_card']) newHtml = fieldsData['get_hlavne_info_card'];
                            else if (el.id.includes('import-stat') && fieldsData['get_statistiky_card']) newHtml = fieldsData['get_statistiky_card'];
                            else if (el.id.includes('import-log') && fieldsData['get_log_card']) newHtml = fieldsData['get_log_card'];

                            if (newHtml) {
                                const temp = document.createElement('div');
                                temp.innerHTML = newHtml.trim();
                                const newNode = temp.firstElementChild || temp.firstChild;
                                
                                if (newNode && !el.isEqualNode(newNode)) {
                                    el.replaceWith(newNode);
                                }
                            }
                        });
                    });
                }
            })
            .catch(() => { /* Tichý retry pri reštarte Django servera */ });
        };

        setInterval(pollEngine, 2500);
    }

    // =============================================================================
    // CENTRÁLNE GLOBÁLNE MODÁLY A ENGINE DIKTOVANIA (Dostupné pre celý ISAS CORE)
    // =============================================================================
    window.showGlobalAlertModal = function(title, text, type = "warning") {
        const isError = type === "error";
        return window.ISASModalEngine({
            maxWidth: "400px", 
            icon: isError ? "🚨" : "ℹ️", 
            title: title, 
            text: text,
            showClose: false, 
            showCloseX: false,
            iconStyle: `background: ${isError ? '#fef2f2' : '#eff6ff'}; width: 64px; height: 64px; line-height: 64px; border-radius: 50%; margin: 0 auto 20px auto; font-size: 28px; box-shadow: 0 4px 15px ${isError ? 'rgba(239, 68, 68, 0.15)' : 'rgba(59, 130, 246, 0.15)'};`,
            buttons: [{ id: "global-alert-close", text: "Rozumiem", style: `background: ${isError ? '#ef4444' : '#3b82f6'}; color: white; border: none; padding: 12px 30px; border-radius: 10px; font-size: 14px; font-weight: 700; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 12px ${isError ? 'rgba(239, 68, 68, 0.25)' : 'rgba(59, 130, 246, 0.25)'}; width: 100%;`, onClick: (e, m) => m.close() }]
        });
    };

    window.showGlobalConfirmModal = function(title, text, onConfirm) {
        return window.ISASModalEngine({
            maxWidth: "400px", 
            icon: "⚠️", 
            title: title, 
            text: text,
            showClose: false, 
            showCloseX: false,
            iconStyle: "background: #fffbeb; color: #f59e0b; width: 64px; height: 64px; line-height: 64px; border-radius: 50%; margin: 0 auto 20px auto; font-size: 28px; box-shadow: 0 4px 15px rgba(245, 158, 11, 0.15);",
            buttons: [
                { id: "global-modal-cancel", text: "Zrušiť", style: "background: transparent; color: #64748b; border: 1px solid #cbd5e1; padding: 12px 20px; border-radius: 10px; font-size: 14px; font-weight: 700; cursor: pointer; transition: all 0.2s; flex: 1;", onClick: (e, m) => m.close() },
                { id: "global-modal-confirm", text: "Potvrdiť", style: "background: #3b82f6; color: white; border: none; padding: 12px 20px; border-radius: 10px; font-size: 14px; font-weight: 700; cursor: pointer; transition: all 0.2s; box-shadow: 0 4px 12px rgba(59, 130, 246, 0.25); flex: 1;", onClick: (e, m) => { m.close(); if (typeof onConfirm === "function") onConfirm(); } }
            ]
        });
    };

    window.showGdprHardConfirmModal = function(count, onConfirm) {
        return window.ISASModalEngine({
            maxWidth: "440px", 
            icon: "🛡️", 
            title: "Bezpečnostné overenie",
            showClose: false, 
            showCloseX: false,
            iconStyle: "background: #fef2f2; width: 64px; height: 64px; line-height: 64px; border-radius: 50%; margin: 0 auto 20px auto; font-size: 28px; box-shadow: 0 4px 15px rgba(239, 68, 68, 0.15);",
            text: `Chystáte sa trvalo anonymizovať <b>${count} záznamov</b>. Tento krok nie je možné vrátiť späť.`,
            fields: [
                { type: 'textarea', id: 'gdpr-reason-input', label: 'Dôvod anonymizácie (Povinné)', placeholder: 'Napr. Žiadosť zákazníka o výmaz...', rows: 2 },
                { type: 'html', html: '<div style="margin-top: 15px;"><label style="display: block; font-size: 12px; font-weight: 700; color: #475569; margin-bottom: 8px;">Pre potvrdenie napíšte: <span style="color: #ef4444; user-select: all;">ANONYMIZOVAT</span></label><input type="text" id="gdpr-hard-input" autocomplete="off" placeholder="ANONYMIZOVAT" style="width: 100%; box-sizing: border-box; padding: 12px 16px; border-radius: 10px; border: 2px solid #e2e8f0; font-size: 14px; font-weight: 800; text-align: center; color: #0f172a; outline: none; transition: all 0.3s; background: #f8fafc;"></div>' }
            ],
            buttons: [
                { id: "gdpr-modal-cancel", text: "Zrušiť", style: "flex: 1; background: transparent; color: #64748b; border: 1px solid #cbd5e1; padding: 12px 20px; border-radius: 10px; font-size: 14px; font-weight: 700; cursor: pointer; transition: all 0.2s;", onClick: (e, m) => m.close() },
                { id: "gdpr-modal-confirm", text: "Anonymizovať", style: "flex: 1; background: #ef4444; color: white; border: none; padding: 12px 20px; border-radius: 10px; font-size: 14px; font-weight: 700; cursor: not-allowed; opacity: 0.4; transition: all 0.3s;", onClick: (e, m) => {
                    const inputReason = m.card.querySelector("#gdpr-reason-input");
                    if (!m.card.querySelector("#gdpr-modal-confirm").disabled) {
                        m.close();
                        if (typeof onConfirm === "function") onConfirm(inputReason.value.trim());
                    }
                }}
            ],
            onRender: (m) => {
                const inputWord = m.card.querySelector("#gdpr-hard-input");
                const inputReason = m.card.querySelector("#gdpr-reason-input");
                const confirmBtn = m.card.querySelector("#gdpr-modal-confirm");
                confirmBtn.disabled = true;
                function validateForm() {
                    const isWordCorrect = inputWord.value === "ANONYMIZOVAT";
                    const isReasonFilled = inputReason.value.trim().length >= 5;
                    if (isWordCorrect && isReasonFilled) {
                        confirmBtn.disabled = false; confirmBtn.style.opacity = "1"; confirmBtn.style.cursor = "pointer"; confirmBtn.style.boxShadow = "0 4px 12px rgba(239, 68, 68, 0.3)";
                        inputWord.style.borderColor = "#10b981"; inputWord.style.backgroundColor = "#f0fdf4"; inputWord.style.color = "#166534";
                    } else {
                        confirmBtn.disabled = true; confirmBtn.style.opacity = "0.4"; confirmBtn.style.cursor = "not-allowed"; confirmBtn.style.boxShadow = "none";
                        inputWord.style.borderColor = isWordCorrect ? "#10b981" : "#e2e8f0"; inputWord.style.backgroundColor = isWordCorrect ? "#f0fdf4" : "#f8fafc"; inputWord.style.color = "#0f172a";
                    }
                }
                inputWord.addEventListener("input", validateForm);
                inputReason.addEventListener("input", validateForm);
                setTimeout(() => inputReason.focus(), 100);
            }
        });
    };

    // Spätný alias pre existujúce volania v šablónach
    window.showIsasModal = (title, text, icon = '⚠️') => window.showGlobalAlertModal(title, text, icon === '🚨' ? 'error' : 'warning');

    // 🎙️ Univerzálny motor hlasového diktovania (Speech-to-Text)
    let isasActiveRecognition = null;
    let isasActiveDictateBtn = null;
    let isasActiveDictateDefaultHtml = "";

    window.ISASStopDictation = function() {
        if (isasActiveRecognition) {
            try { isasActiveRecognition.abort(); } catch(e) {}
            isasActiveRecognition = null;
        }
        if (isasActiveDictateBtn) {
            isasActiveDictateBtn.classList.remove('recording');
            isasActiveDictateBtn.innerHTML = isasActiveDictateDefaultHtml || '🎙️';
            isasActiveDictateBtn = null;
        }
        document.querySelectorAll('.recording').forEach(b => b.classList.remove('recording'));

        // 🛡️ JUKEBOX AUTO-RESUME: Po ukončení diktovania vrátime hudbu späť
        if (typeof window.ISASNotifyJukeboxTtsEnd === 'function') window.ISASNotifyJukeboxTtsEnd();
        try { localStorage.setItem('isas_global_tts_active', 'false_' + Date.now()); } catch(e) {}
    };

    window.ISASDictateEngine = function(btnEl, inputEl, defaultBtnHtml, recordingBtnHtml) {
        // Ak už toto konkrétne tlačidlo nahráva, opätovným kliknutím diktovanie manuálne zastavíme
        if (btnEl.classList.contains('recording')) {
            window.ISASStopDictation();
            return;
        }
        window.ISASStopDictation();

        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition) {
            window.showGlobalAlertModal("Chyba", "Váš prehliadač nepodporuje hlasové diktovanie. Odporúčame Google Chrome alebo Safari.", "error");
            return;
        }
        const recognition = new SpeechRecognition();
        isasActiveRecognition = recognition;
        isasActiveDictateBtn = btnEl;
        isasActiveDictateDefaultHtml = defaultBtnHtml;

        recognition.lang = document.documentElement.lang || navigator.language || 'sk-SK'; recognition.interimResults = false; recognition.maxAlternatives = 1;
        recognition.onstart = function() { 
            // 🛡️ AKUSTICKÁ POISTKA: Okamžite umlčíme asistenta aj Jukebox, aby mikrofón nenahrával hudbu z reproduktorov
            if (window.ISASStopSpeech) window.ISASStopSpeech();
            document.querySelectorAll('.ai-tts-btn.is-playing').forEach(b => {
                b.classList.remove('is-playing');
                b.innerHTML = '🔊 Prečítať';
            });

            if (typeof window.ISASNotifyJukeboxTtsStart === 'function') window.ISASNotifyJukeboxTtsStart();
            try { localStorage.setItem('isas_global_tts_active', 'true_' + Date.now()); } catch(e) {}

            btnEl.classList.add('recording'); 
            btnEl.innerHTML = recordingBtnHtml; 
        };
        recognition.onresult = function(event) {
            const transcript = event.results[0][0].transcript;
            if (inputEl.value.length > 0 && !inputEl.value.endsWith(' ') && !inputEl.value.endsWith('\n')) inputEl.value += ' ';
            inputEl.value += transcript.charAt(0).toUpperCase() + transcript.slice(1) + (inputEl.tagName.toLowerCase() === 'textarea' ? '. ' : '');
            if (inputEl.id === 'quicknote-textarea' && typeof saveQuickNote === 'function') saveQuickNote();
        };
        recognition.onerror = function(event) {
            console.error("Chyba diktovania: ", event.error);
            if (event.error !== 'aborted') {
                window.showGlobalAlertModal("Chyba", "Nepodarilo sa rozpoznať hlas. Skontrolujte mikrofón.", "error");
            }
        };
        recognition.onend = function() { 
            btnEl.classList.remove('recording'); 
            btnEl.innerHTML = defaultBtnHtml; 
            inputEl.focus(); 
            if (isasActiveRecognition === recognition) isasActiveRecognition = null;

            // 🛡️ JUKEBOX AUTO-RESUME: Po prirodzenom dohovorení tichom vrátime hudbu späť
            if (typeof window.ISASNotifyJukeboxTtsEnd === 'function') window.ISASNotifyJukeboxTtsEnd();
            try { localStorage.setItem('isas_global_tts_active', 'false_' + Date.now()); } catch(e) {}
        };
        recognition.start();
    };

    // 🧹 Inteligentný čistič textu pre hlasový výstup (Odstráni Markdown, HTML, URL, odrážky a Emoji)
    window.ISASCleanTextForSpeech = function(rawText) {
        if (!rawText) return "";
        return rawText
            .replace(/<[^>]*>/g, ' ')                                    // Odstránenie HTML tagov
            .replace(/\[(.*?)\]\(.*?\)/g, '$1')                          // Markdown linky [text](url) -> ponechá len text
            .replace(/https?:\/\/\S+/gi, '')                             // Odstránenie samotných URL adries
            .replace(/[*_~`#]/g, '')                                     // Odstránenie Markdown značiek
            .replace(/^\s*[-–—•*]\s+/gm, '')                             // 🛡️ Odstránenie odrážok (asistent nebude čítať "pomlčka")
            // 🛡️ Normalizácia nedeliteľných HTML medzier (&nbsp; / \u00A0)
            .replace(/\u00A0/g, ' ')
            // 🛡️ Likvidácia oddeľovacích čiar tabuliek (---), aby syntetizátor nečítal "mínus mínus mínus"
            .replace(/[-–—]{2,}/g, ' ')
            // 🛡️ Prirodzený prednes technických jednotiek strojov (ešte pred zmazaním lomítok)
            .replace(/°C/g, ' stupňov Celzia ')
            .replace(/\bkm\/h\b/gi, ' kilometrov za hodinu ')
            .replace(/\bot\/min\b/gi, ' otáčok za minútu ')
            .replace(/\bm3\/h\b/gi, ' metrov kubických za hodinu ')
            .replace(/\bm3\/min\b/gi, ' metrov kubických za minútu ')
            .replace(/[|\\\/\[\]{}()<>_]/g, ' ')                        // 🛡️ Odstránenie zvislých čiar tabuliek a zátvoriek (nebude čítať "zvislá čiara")
            .replace(/[€$£]/g, ' ')                                      // 🛡️ Odstránenie menových symbolov pre neutrálny prednes
            .replace(/%/g, ' percent ')                                  // Prepis percent
            .replace(/\+/g, ' plus ')                                    // Prepis plus
            .replace(/(&amp;|&)/g, ' a ')                                // 🛡️ Prepis & aj &amp; na slovenské "a" (nebude čítať "ampersand")
            // 🛡️ Prepis frekventovaných slovenských skratiek pre prirodzený prednes
            .replace(/\bč\.\s*/gi, ' číslo ')
            .replace(/\bnapr\.\s*/gi, ' napríklad ')
            .replace(/\bobj\.\s*/gi, ' objednávka ')
            .replace(/\btj\.\s*/gi, ' to jest ')
            // 🛡️ Oprava desatinných bodiek z AI (12.50 -> 12,50), aby syntetizátor nespravil bodku a pauzu uprostred sumy
            .replace(/\b(\d+)\.(\d{1,2})\b/g, '$1,$2')
            // 🛡️ Nepriestrelné odstránenie všetkých Unicode Emoji symbolov (nový štandard)
            .replace(/\p{Extended_Pictographic}/gu, '')
            // 🛡️ Dychová pauza: ak riadok/nadpis nekončí interpunkciou, pridáme bodku pre prirodzenú intonáciu
            .replace(/([^\.\!\?\,\:\;])\s*\n+/g, '$1. ')
            .replace(/\s+/g, ' ')                                        // Konsolidácia medzier a riadkov
            .trim();
    };

    // 🔊 Prednačítanie a vyrovnávacia pamäť hlasov cez bezpečný EventListener
    let isasAvailableVoices = [];
    function isasInitVoices() {
        if ('speechSynthesis' in window) {
            isasAvailableVoices = window.speechSynthesis.getVoices();
        }
    }
    isasInitVoices();
    if ('speechSynthesis' in window) {
        window.speechSynthesis.addEventListener('voiceschanged', isasInitVoices);
    }

    let isasTtsTimer = null;
    let isasPendingSpeechTimeout = null; // 🛡️ Ochrana pred duchom v zatvorenom okne
    let isasActiveUtterance = null; // 🛡️ Globálna referencia proti V8 Garbage Collectoru

    window.ISASStopSpeech = function(skipJukeboxResume = false) {
        // 🛡️ Okamžite zrušíme čakajúci odložený štart novej správy
        if (isasPendingSpeechTimeout) {
            clearTimeout(isasPendingSpeechTimeout);
            isasPendingSpeechTimeout = null;
        }
        if ('speechSynthesis' in window) {
            if (isasTtsTimer) {
                clearInterval(isasTtsTimer);
                isasTtsTimer = null;
            }
            // 🛡️ Chromium Freeze Fix: Ak bol engine uspaný, musíme zavolať resume pred cancelom
            if (window.speechSynthesis.paused) {
                window.speechSynthesis.resume();
            }
            if (window.speechSynthesis.speaking || window.speechSynthesis.pending) {
                window.speechSynthesis.cancel();
            }
        }
        isasActiveUtterance = null;

        // 🛡️ JUKEBOX INTEGRÁCIA: Hudbu prebudíme iba vtedy, ak nejdeme okamžite púšťať novú správu
        if (!skipJukeboxResume) {
            if (typeof window.ISASNotifyJukeboxTtsEnd === 'function') window.ISASNotifyJukeboxTtsEnd();
            try { localStorage.setItem('isas_global_tts_active', 'false_' + Date.now()); } catch(e) {}
        }
    };

    window.ISASTextToSpeechEngine = function(text, onStart, onEnd, onError) {
        if (!('speechSynthesis' in window)) {
            if (typeof window.showGlobalAlertModal === 'function') {
                window.showGlobalAlertModal("Hlasový výstup", "Váš prehliadač nepodporuje syntézu reči.", "warning");
            }
            return false;
        }

        // 🛡️ OBOJSMERNÁ POISTKA: Ak používateľ práve niečo diktoval, mikrofón okamžite vypneme
        if (window.ISASStopDictation) window.ISASStopDictation();

        // Zistíme, či reálne bežala reč pred zastavením (s príznakom true, aby rádio nezaseklo na 60ms)
        const wasSpeaking = ('speechSynthesis' in window) && (window.speechSynthesis.speaking || window.speechSynthesis.pending);
        window.ISASStopSpeech(true);

        const cleanText = window.ISASCleanTextForSpeech(text);
        if (!cleanText) return false;

        const utterance = new SpeechSynthesisUtterance(cleanText);
        isasActiveUtterance = utterance; // Uložené v pamäti proti GC
        
        utterance.lang = document.documentElement.lang || 'sk-SK';
        utterance.rate = 1.0;
        utterance.pitch = 1.0;

        // Výber hlasu + Android zhoda jazyka
        const currentVoices = isasAvailableVoices.length > 0 ? isasAvailableVoices : window.speechSynthesis.getVoices();
        const skVoice = currentVoices.find(v => v.lang.startsWith('sk') || v.lang.includes('SK')) ||
                        currentVoices.find(v => v.lang.startsWith('cs') || v.lang.includes('CZ'));
        if (skVoice) {
            utterance.voice = skVoice;
            utterance.lang = skVoice.lang;
        }

        utterance.onstart = function() {
            if (typeof onStart === 'function') onStart();
            
            // 🛡️ JUKEBOX INTEGRÁCIA: Ak hrá rádio alebo Spotify, automaticky ho pozastavíme
            if (typeof window.ISASNotifyJukeboxTtsStart === 'function') window.ISASNotifyJukeboxTtsStart();
            try { localStorage.setItem('isas_global_tts_active', 'true_' + Date.now()); } catch(e) {}

            // KeepAlive iba pre Chromium
            const isChromium = !!window.chrome || (navigator.userAgent.indexOf("Chrome") !== -1);
            if (isChromium) {
                if (isasTtsTimer) clearInterval(isasTtsTimer);
                isasTtsTimer = setInterval(() => {
                    if (!window.speechSynthesis.speaking) {
                        clearInterval(isasTtsTimer);
                        isasTtsTimer = null;
                    } else if (!window.speechSynthesis.paused) {
                        window.speechSynthesis.pause();
                        setTimeout(() => {
                            if (window.speechSynthesis.paused) window.speechSynthesis.resume();
                        }, 50);
                    }
                }, 10000);
            }
        };

        // 🛡️ Bezpečný cleanup: stará správa nesmie zrušiť časovače a pamäť novej správy
        const cleanup = () => {
            if (isasActiveUtterance === utterance) {
                if (isasTtsTimer) {
                    clearInterval(isasTtsTimer);
                    isasTtsTimer = null;
                }
                if (isasPendingSpeechTimeout) {
                    clearTimeout(isasPendingSpeechTimeout);
                    isasPendingSpeechTimeout = null;
                }
                isasActiveUtterance = null;

                // 🛡️ JUKEBOX INTEGRÁCIA: Asistent dohovoril -> obnovíme pozastavenú hudbu
                if (typeof window.ISASNotifyJukeboxTtsEnd === 'function') window.ISASNotifyJukeboxTtsEnd();
                try { localStorage.setItem('isas_global_tts_active', 'false_' + Date.now()); } catch(e) {}
            }
        };

        utterance.onend = function() {
            cleanup();
            if (typeof onEnd === 'function') onEnd();
        };

        utterance.onerror = function(err) {
            cleanup();
            if (err.error !== 'canceled' && err.error !== 'interrupted') {
                if (typeof onError === 'function') onError(err);
            } else {
                if (typeof onEnd === 'function') onEnd();
            }
        };

        // 🛡️ Spustenie so sledovaným časovačom (bezpečné pre WebKit aj okamžité zatvorenie okna)
        if (wasSpeaking) {
            isasPendingSpeechTimeout = setTimeout(() => {
                isasPendingSpeechTimeout = null;
                window.speechSynthesis.speak(utterance);
            }, 60);
        } else {
            window.speechSynthesis.speak(utterance);
        }
        return true;
    };

    // K. COMPLEX FORM RULES
    if (isasConfig.complex_form_rules) {
        const rules = isasConfig.complex_form_rules;

        if (rules.spolujazdci_protection && typeof django !== 'undefined' && django.jQuery) {
            const { driver_field, passengers_field, warning_title, warning_text_add, warning_text_remove } = rules.spolujazdci_protection;
            const $ = django.jQuery, $driver = $('#id_' + driver_field), $passengers = $('#id_' + passengers_field);
            
            if ($driver.length && $passengers.length) {
                $passengers.on('select2:select', e => {
                    const selectedId = e.params.data.id, driverId = $driver.val();
                    if (driverId && selectedId === driverId) {
                        const currentValues = $passengers.val();
                        if (currentValues) $passengers.val(currentValues.filter(v => v !== driverId)).trigger('change');
                        showIsasModal(warning_title, warning_text_add, "⚠️");
                    }
                });
                $driver.on('change', function() {
                    const driverId = $(this).val(); if (!driverId) return;
                    const currentValues = $passengers.val();
                    if (currentValues?.includes(driverId)) {
                        $passengers.val(currentValues.filter(v => v !== driverId)).trigger('change');
                        showIsasModal("Systémová korekcia", warning_text_remove, "ℹ️");
                    }
                });
            }
        }

        if (rules.address_groups) {
            const getContainer = el => el?.closest('.fieldBox') || el?.closest('.form-row');
            rules.address_groups.forEach(group => {
                const textEl = document.getElementById('id_' + group.text_field);
                const selectEls = group.select_fields.map(id => document.getElementById('id_' + id)).filter(Boolean);

                const updateState = (e, isInit) => {
                    const activeSelect = selectEls.find(el => el.value !== "");
                    if (activeSelect) {
                        if (textEl) { 
                            window.setISASFieldState(group.text_field, false);
                            if (!isInit && textEl.dataset.lastSelect !== activeSelect.value) {
                                textEl.dataset.lastSelect = activeSelect.value; textEl.value = "⏳ Načítavam dáta..."; textEl.dataset.isAjaxData = "true"; 
                                window.ISASAction({ type: 'get_entity_address', field_type: activeSelect.name, obj_id: activeSelect.value, ids: [0] }, null, {
                                    url: (window.location.pathname.match(/.*\/admin\/[^\/]+\/[^\/]+\//)?.[0] || "") + "fast-action/", reload: false,
                                    onComplete: (data) => { textEl.value = data.address || ""; Object.assign(textEl.style, { transition: 'none', backgroundColor: '#ecfdf5', color: '#065f46', fontWeight: 'bold' }); setTimeout(() => Object.assign(textEl.style, { transition: 'all 0.5s ease', backgroundColor: '', color: '', fontWeight: '' }), 1500); },
                                    onError: () => textEl.value = ""
                                });
                            } else if (isInit) { textEl.dataset.lastSelect = activeSelect.value; textEl.dataset.isAjaxData = "true"; }
                        }
                        selectEls.forEach(el => { if (el !== activeSelect) window.setISASFieldState(el.id.replace('id_', ''), true); });
                        getContainer(textEl)?.classList.remove("required");
                        selectEls.forEach(el => getContainer(el)?.classList.toggle("required", el === activeSelect));
                    } else {
                        if (textEl) {
                            window.setISASFieldState(group.text_field, false); textEl.dataset.lastSelect = ""; 
                            if (!isInit && textEl.dataset.isAjaxData === "true") {
                                textEl.value = "⏳ Načítavam centrálu...";
                                window.ISASAction({ type: 'get_entity_address', field_type: 'default', obj_id: 'DEFAULT', ids: [0] }, null, {
                                    url: (window.location.pathname.match(/.*\/admin\/[^\/]+\/[^\/]+\//)?.[0] || "") + "fast-action/", reload: false,
                                    onComplete: (data) => textEl.value = data.address || "", onError: () => textEl.value = ""
                                });
                                textEl.dataset.isAjaxData = "false"; 
                            }
                        }
                        selectEls.forEach(el => window.setISASFieldState(el.id.replace('id_', ''), false));
                        const isTextActive = textEl?.value.trim() !== "";
                        getContainer(textEl)?.classList.add("required");
                        selectEls.forEach(el => getContainer(el)?.classList.toggle("required", !isTextActive));
                    }
                };
                if (textEl) textEl.addEventListener('input', e => updateState(e, false));
                selectEls.forEach(el => { if (typeof django !== 'undefined' && django.jQuery) django.jQuery(el).on('change select2:clear select2:unselect', e => setTimeout(() => updateState(e, false), 50)); else el.addEventListener('change', e => updateState(e, false)); });
                setTimeout(() => updateState(null, true), 200);
            });
        }

        if (rules.row_toggles) {
            window.ISASBindFieldEvent(rules.row_toggles.trigger, 'change', (e) => {
                const rule = rules.row_toggles.rules[e.target.value];
                if (rule) {
                    rule.show?.forEach(f => { const row = document.querySelector('.field-' + f); if (row) row.style.display = ''; });
                    rule.hide?.forEach(f => { const row = document.querySelector('.field-' + f); if (row) row.style.display = 'none'; });
                    rule.clear?.forEach(f => { const el = document.getElementById('id_' + f); if (el) { el.value = ''; if(typeof django !== 'undefined' && django.jQuery) django.jQuery(el).trigger('change'); } });
                }
            });
        }
    }

    // K2. AJAX PREFILTER RULES
    if (isasConfig.ajax_prefilter_rules && typeof django !== 'undefined' && django.jQuery) {
        django.jQuery.ajaxPrefilter((options) => {
            if (options.url && options.url.indexOf('/autocomplete/') !== -1) {
                isasConfig.ajax_prefilter_rules.forEach(rule => {
                    if (options.url.indexOf('field_name=' + rule.target_field) !== -1 || (options.data && options.data.indexOf('field_name=' + rule.target_field) !== -1)) {
                        var sourceVal = django.jQuery('#id_' + rule.source_field).val();
                        if (sourceVal) options.url += (options.url.indexOf('?') === -1 ? '?' : '&') + rule.filter_param + '=' + sourceVal;
                    }
                });
            }
        });
    }

    // O. KOSIK MANAGER
    if (isasConfig.kosik_manager) {
        window.ISASStyleEngine('isas-kosik-css', `.toggle-wrapper{background:white;padding:20px;border-radius:16px;border:1px solid #e2e8f0;display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;box-shadow:0 4px 6px rgba(0,0,0,0.02);}.toggle-wrapper span{font-weight:800;color:#334155;font-size:15px;}.switch{position:relative;display:inline-block;width:54px;height:30px;}.switch input{opacity:0;width:0;height:0;}.slider{position:absolute;cursor:pointer;inset:0;background-color:#cbd5e1;transition:.4s;border-radius:34px;}.slider:before{position:absolute;content:"";height:22px;width:22px;left:4px;bottom:4px;background-color:white;transition:.4s;border-radius:50%;}input:checked+.slider{background-color:#10b981;}input:checked+.slider:before{transform:translateX(24px);}.limit-item{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px dashed #cbd5e1;}.limit-item:last-child{border-bottom:none;}.limit-name{font-size:13px;font-weight:700;color:#475569;width:70%;line-height:1.4;}.limit-input{width:70px;padding:8px;text-align:center;border:2px solid #e2e8f0;border-radius:10px;font-weight:800;color:#0f172a;outline:none;transition:0.3s;}.limit-input:focus{border-color:#8b5cf6;box-shadow:0 0 0 3px rgba(139,92,246,0.2);}`);

        window.ISASClickHandler("#btn-send-email", (e, btn) => {
            e.preventDefault(); e.stopImmediatePropagation(); 
            if (btn.classList.contains("is-processing")) return;
            
            const checked = document.querySelectorAll('input.action-select:checked');
            if (!checked.length) return showISASToast("Najskôr zaškrtnite aspoň jednu položku.", "📭", "warning");
            
            const invalidItem = Array.from(checked).some(cb => { const html = cb.closest('tr').innerHTML; return html.includes("— Nezadaný —") || html.includes("Vybavená"); });
            if (invalidItem) return showISASToast("Uistite sa, že všetky diely majú dodávateľa a neboli objednané.", "🛑", "warning");

            const items = Array.from(checked).map(cb => ({ id: cb.value, qty: cb.closest('tr').querySelector(`input[name="mnozstvo_${cb.value}"]`)?.value || null }));
            window.showGlobalConfirmModal(
                "Odoslanie objednávok 📤",
                `Naozaj chcete vygenerovať a odoslať objednávku pre <b>${items.length} položiek</b> príslušným dodávateľom?`,
                () => window.ISASAction({ type: 'send_email', items }, btn, { reload: false, loadingText: "⏳ ODOSIELAM...", successText: "📤 SPRACOVÁVA SA" })
            );
        });

        window.ISASClickHandler("#btn-settings", (e) => {
            e.preventDefault();
            const limitsHtml = Object.entries(isasConfig.kosik_manager.limity || {}).map(([cat, val]) => `<div class="limit-item"><div class="limit-name">${cat}</div><input type="number" class="limit-input" data-cat="${cat}" value="${val}" min="0"></div>`).join('');
            
            window.ISASModalEngine({
                maxWidth: "550px", icon: "⚙️", title: "Globálne nastavenia", textAlign: "left",
                fields: [
                    { type: 'toggle', id: 'toggle-notif', label: '🔔 Telegram, E-mail & Discord notifikácie', checked: isasConfig.kosik_manager.notifikacie },
                    { type: 'html', html: `<h4 style="margin:0 0 10px 0;color:#0f172a;font-weight:900;font-size:15px;">Kritické limity kategórií</h4><p style="margin:0 0 15px 0;color:#64748b;font-size:12px;">Množstvo, pri ktorom položka automaticky spadne do košíka.</p><div style="background:white;border-radius:16px;border:1px solid #e2e8f0;padding:10px 20px;">${limitsHtml}</div>` }
                ],
                buttons: [{
                    id: "btn-save-settings", text: "💾 Uložiť zmeny", style: "background:#8b5cf6;color:white;border:none;padding:14px 25px;border-radius:12px;font-size:14px;font-weight:800;cursor:pointer;width:100%;transition:all 0.2s;",
                    onClick: (e, m, btn) => {
                        const newLimits = {}; 
                        m.card.querySelectorAll(".limit-input").forEach(inp => newLimits[inp.dataset.cat] = parseInt(inp.value));
                        
                        window.ISASAction({ notifikacie: m.card.querySelector("#toggle-notif").checked, limity: newLimits }, btn, {
                            url: "settings-api/", reload: false,
                            onComplete: (data, b, origHtml) => {
                                if(data.status === "success") {
                                    showISASToast(data.message, '✅', 'success'); b.innerHTML = "✅ ULOŽENÉ";
                                    Object.assign(isasConfig.kosik_manager, { notifikacie: m.card.querySelector("#toggle-notif").checked, limity: newLimits });
                                    setTimeout(() => m.close(), 1000);
                                } else { showISASToast(data.message, '🚨', 'error'); b.classList.remove("is-processing"); b.innerHTML = origHtml; }
                            }
                        });
                    }
                }]
            });
        });

        const ids = Array.from(document.querySelectorAll('input[name^="mnozstvo_"]')).map(inp => inp.name.split('_')[1]).join(',');
        if (ids) {
            window.ISASPoller({
                url: "check-status/?ids=" + ids, type: 'json', interval: 3000, maxAttempts: 999999,
                onData: (data) => {
                    Object.entries(data).forEach(([id, info]) => {
                        if (!info.objednane) return;
                        const inputEl = document.querySelector(`input[name="mnozstvo_${id}"]`);
                        if (!inputEl) return;
                        
                        const row = inputEl.closest('tr');
                        const qtyTd = row.querySelector('.field-upravitelne_mnozstvo_premium');
                        if (qtyTd) qtyTd.innerHTML = `<div style="display:flex;align-items:center;gap:8px;justify-content:flex-start;flex-wrap:nowrap;white-space:nowrap;"><div style="background:#f8fafc;border:1px solid #e2e8f0;color:#64748b;width:50px;padding:8px 6px;border-radius:10px;font-weight:800;font-size:14px;text-align:center;box-shadow:inset 0 2px 4px rgba(0,0,0,0.03);box-sizing:border-box;">${inputEl.value}</div>${inputEl.nextElementSibling?.outerHTML || ''}</div>`;
                        
                        const badgeTd = row.querySelector('.field-stav_objednavky_badge');
                        if (badgeTd) badgeTd.innerHTML = '<div style="background:#dcfce7;color:#15803d;border:1px solid #86efac;padding:6px 12px;border-radius:20px;font-size:11px;font-weight:800;display:inline-flex;align-items:center;text-transform:uppercase;letter-spacing:0.8px;box-shadow:0 1px 2px rgba(0,0,0,0.03);transition:all 0.5s;"><span style="margin-right:6px;font-size:14px;">✅</span> Vybavená</div>';
                        
                        const dateTd = row.querySelector('.field-zobraz_cas_odoslania_styled');
                        if (dateTd) dateTd.innerHTML = `<span style="color:#0284c7;font-weight:700;font-size:13px;display:flex;align-items:center;gap:6px;animation:fadeIn 0.5s;"><span style="font-size:15px;">📤</span> ${info.datum}</span>`;
                        
                        const checkbox = row.querySelector('input.action-select'); if (checkbox) checkbox.checked = false;
                        row.style.backgroundColor = '#f0fdf4'; setTimeout(() => row.style.backgroundColor = '', 2000);
                    });
                    return false;
                }
            });
        }
    }

    // N. AI MATCH MODAL & AI SEPKAR & OPEN RELATED
    if (isasConfig.ai_match_b64_payload) {
        window.ISASStyleEngine('isas-ai-task-modal-css', `.isas-ai-modal-header { text-align: center; margin-bottom: 25px; } .isas-ai-modal-header h2 { margin: 0; color: #0f172a; font-size: 24px; font-weight: 900; letter-spacing: -0.5px; display: flex; align-items: center; justify-content: center; gap: 10px; } .isas-ai-modal-header h2 span { font-size: 32px; line-height: 1; } .isas-ai-modal-header p { color: #64748b; font-size: 14px; margin-top: 8px; line-height: 1.4; } .isas-ai-task-card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 16px; padding: 20px; text-align: center; } .isas-ai-tag { font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; padding: 3px 8px; border-radius: 6px; color: white; } .isas-ai-tag.single-match { color: #6366f1; background: transparent; padding: 0; } .isas-ai-tag.perfect-match { background: #10b981; } .isas-ai-tag.work-match { background: #3b82f6; } .isas-ai-tag.tech-match { background: #8b5cf6; } .isas-ai-tag.machine-match { background: #f59e0b; } .isas-ai-task-card h3 { margin: 8px 0; color: #1e293b; font-size: 18px; } .isas-ai-task-card p { margin: 0 0 20px 0; color: #475569; font-size: 13px; } .isas-ai-close-task-btn { background: linear-gradient(135deg, #10b981, #059669); color: white; border: none; padding: 14px 28px; border-radius: 12px; font-weight: 800; font-size: 13px; cursor: pointer; width: 100%; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3); transition: all 0.2s; } .isas-ai-close-task-btn:hover { transform: translateY(-2px); box-shadow: 0 6px 15px rgba(16, 185, 129, 0.4); } .isas-ai-close-task-btn.small { padding: 10px 16px; border-radius: 8px; font-size: 11px; width: auto; flex-shrink: 0; margin-left: 15px; } .isas-ai-warning-text { text-align: center; font-weight: bold; color: #ef4444; margin-bottom: 15px; } .isas-ai-task-row { border: 1px solid; border-radius: 12px; padding: 16px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; } .isas-ai-task-row.perfect-match { background: #f0fdf4; border-color: #86efac; } .isas-ai-task-row.work-match { background: #eff6ff; border-color: #bfdbfe; } .isas-ai-task-row.tech-match { background: #f5f3ff; border-color: #ddd6fe; } .isas-ai-task-row.machine-match { background: #fffbeb; border-color: #fde68a; } .isas-ai-task-title { margin-bottom: 6px; } .isas-ai-task-title strong { font-weight: 800; color: #0f172a; margin-left: 5px; } .isas-ai-task-meta { font-size: 12px; color: #475569; } .isas-ai-modal-footer { text-align: center; margin-top: 25px; padding-top: 15px; border-top: 1px dashed #e2e8f0; } .isas-ai-close-modal-btn { background: transparent; color: #64748b; border: 1px solid #cbd5e1; padding: 8px 24px; border-radius: 8px; font-size: 11px; font-weight: 800; text-transform: uppercase; cursor: pointer; transition: all 0.2s; letter-spacing: 0.5px; } .isas-ai-close-modal-btn:hover { background: #f1f5f9; color: #0f172a; border-color: #94a3b8; }`);

        window.uzavriUlohuAI_Z_Modalu = function(ulohaId, btn) {
            const adminBase = (window.location.pathname.match(/^(.*?\/admin\/)/) || ['', '/admin/'])[1];
            window.ISASAction({ type: 'complete', ids: [ulohaId], send_notifs: true }, btn, {
                url: `${adminBase}Evidencia/uloha/fast-action/`, reload: false,
                onComplete: (data, b) => {
                    if(data.status === "success") { b.innerHTML = "✅ ÚSPEŠNE UZAVRETÉ"; b.style.background = "#64748b"; setTimeout(() => { if(window.aiModalInstance) window.aiModalInstance.close(); }, 1200); }
                    else { showISASToast("Chyba: " + data.message, '❌', 'error'); b.innerHTML = "✅ ZNOVA SKÚSIŤ"; b.style.pointerEvents = "auto"; }
                },
                onError: (err) => { showISASToast("Chyba siete: " + err, '🔌', 'error'); btn.style.pointerEvents = "auto"; }
            });
        };

        setTimeout(() => {
            window.aiModalInstance = window.ISASModalEngine({
                maxWidth: "550px", textAlign: "left", customHtml: decodeURIComponent(escape(window.atob(isasConfig.ai_match_b64_payload))), showClose: false, showCloseX: true,
                onRender: (m) => {
                    m.card.style.padding = "35px 40px";
                }
            });
            window.closeISASAIModal = () => { if(window.aiModalInstance) window.aiModalInstance.close(); };
        }, 150);
    }

    if (isasConfig.ai_sepkar_field) {
        setTimeout(() => {
            const $zariadenieInput = document.getElementById('id_' + isasConfig.ai_sepkar_field); 
            if (!$zariadenieInput || document.getElementById('btn-ai-sepkar')) return;

            const aiBtn = document.createElement('button'); 
            aiBtn.type = 'button'; aiBtn.id = 'btn-ai-sepkar'; aiBtn.innerHTML = '<span style="font-size:16px;">🤖</span> Diagnostika (AI)';
            aiBtn.style.cssText = 'margin-left:15px;background:linear-gradient(135deg,#f8fafc,#f1f5f9);color:#3b82f6;border:1px solid #bfdbfe;padding:6px 15px;border-radius:8px;font-weight:800;font-size:11px;cursor:pointer;box-shadow:0 4px 6px rgba(59,130,246,0.1);display:inline-flex;align-items:center;gap:8px;height:32px;vertical-align:middle;';
            
            const wrapper = $zariadenieInput.closest('.related-widget-wrapper');
            if (wrapper) { wrapper.style.display = 'inline-flex'; wrapper.style.alignItems = 'center'; wrapper.appendChild(aiBtn); } 
            else { $zariadenieInput.parentNode.insertBefore(aiBtn, $zariadenieInput.nextSibling); }

            aiBtn.addEventListener('click', e => {
                e.preventDefault(); 
                const zar_id = $zariadenieInput.value;
                if (!zar_id) return showISASToast('Najprv musíte vybrať zariadenie zo zoznamu.', '⚠️', 'warning');
                if (aiBtn.classList.contains('is-processing')) return; 
                
                aiBtn.style.opacity = '0.7';
                const adminBase = (window.location.pathname.match(/^(.*?\/admin\/)/) || ['', '/admin/'])[1];
                window.ISASAction(null, aiBtn, {
                    url: `${adminBase}Evidencia/servisnyzaznam/ai-sepkar/${zar_id}/`, method: 'GET', loadingText: '<span>⏳</span> Analyzujem...', reload: false,
                    onComplete: (data, btn, origHtml) => {
                        btn.style.opacity = '1';
                        if(data.status === 'success') {
                            window.ISASStyleEngine('isas-ai-modal-styles', `.isas-ai-list{list-style:none!important;padding:0!important;margin:0!important;}.isas-ai-list li{position:relative;padding-left:40px;margin-bottom:24px;font-weight:500;}.isas-ai-list li::before{content:'⚡';position:absolute;left:0;top:2px;font-size:14px;background:#eff6ff;color:#3b82f6;width:26px;height:26px;display:flex;align-items:center;justify-content:center;border-radius:8px;border:1px solid #dbeafe;}.isas-ai-list li strong{color:#0f172a;font-weight:700;}`);
                            
                            const lines = data.message.split('\n').filter(l => l.trim());
                            const isList = lines.some(l => l.trim().startsWith('-'));
                            const contentHtml = isList 
                                ? `<ul class="isas-ai-list">${lines.filter(l => l.trim().startsWith('-')).map(l => `<li>${l.replace(/^-?\s*/, '').replace(/\*\*/g, '')}</li>`).join('')}</ul>`
                                : `<p style="margin:0;font-weight:500;">${data.message.replace(/\n/g, '<br>').replace(/\*\*/g, '')}</p>`;

                            window.ISASModalEngine({
                                maxWidth: "680px", icon: "🧠", title: "ISAS AI: Diagnostika zariadenia", text: false, showClose: false,
                                fields: [{ type: 'html', html: `<div style="text-align:left;font-size:15px;color:#475569;line-height:1.8;">${contentHtml}</div>` }],
                                buttons: [{ id: "btn-ai-close", text: "Rozumiem", className: "isas-modal-btn isas-btn-send", onClick: (e, m) => m.close() }]
                            });
                        } else { showISASToast(data.message, '⚠️', 'warning'); }
                        btn.classList.remove('is-processing'); btn.innerHTML = origHtml;
                    },
                    onError: () => { aiBtn.style.opacity = '1'; }
                });
            });
        }, 500);
    }

    if (isasConfig.open_related_on_select && typeof django !== 'undefined' && django.jQuery) {
        const adminBase = (window.location.pathname.match(/^(.*?\/admin\/)/) || ['', '/admin/'])[1];
        isasConfig.open_related_on_select.forEach(fieldName => {
            django.jQuery('#id_' + fieldName).on('select2:select', e => { if (e.params.data && e.params.data.id) window.open(`${adminBase}Evidencia/${fieldName}/${e.params.data.id}/change/`, '_blank'); });
        });
    }

    // M2. DYNAMIC FIELDS UI
    if (isasConfig.dynamic_fields_ui) {
        const cfg = isasConfig.dynamic_fields_ui;
        window.ISASBindFieldEvent(cfg.trigger_field, 'change', (e) => {
            const rule = cfg.rules[e.target.value];
            if (rule) {
                Object.entries(rule).forEach(([fieldId, fieldConfig]) => {
                    const field = document.getElementById('id_' + fieldId);
                    if (field) {
                        const label = document.querySelector(`label[for="id_${fieldId}"]`); 
                        if (label && fieldConfig.label) label.innerHTML = fieldConfig.label;
                        
                        const helpText = field.closest(".form-row")?.querySelector('.help'); 
                        if (helpText && fieldConfig.help) helpText.innerHTML = fieldConfig.help;
                        
                        if (fieldConfig.replace?.includes(field.value.trim())) field.value = fieldConfig.default || '';
                    }
                });
            }
        }, 150);
    }

    // M. VALUE BASED LOCKS
    if (isasConfig.value_based_locks) {
        const cfg = isasConfig.value_based_locks;
        const useSoftLock = Boolean(cfg.soft_lock);
        window.ISASBindFieldEvent(cfg.trigger_field, 'change', (e) => {
            const rule = cfg.rules[e.target.value] || cfg.default_rule;
            if (rule) {
                rule.lock?.forEach(f => window.setISASFieldState(f, true, rule.clear_value, useSoftLock));
                rule.unlock?.forEach(f => window.setISASFieldState(f, false, undefined, useSoftLock));
            }
        });
    }

    // P. COPY-PASTE ASISTENT
    if (document.querySelector('.isas-cpa-btn') || isasConfig?.copy_paste_assistant) {
        const cfg = isasConfig?.copy_paste_assistant || { trigger_class: 'isas-cpa-btn' };
        
        window.ISASStyleEngine('isas-cpa-css', `.isas-cpa-minimized{width:300px!important;position:fixed!important;bottom:20px!important;left:50%!important;right:auto!important;top:auto!important;transform:translateX(-50%)!important;margin:0!important;padding:15px!important;box-shadow:0 10px 25px rgba(0,0,0,0.2)!important;transition:all 0.3s ease-in-out;z-index:9999!important;}.isas-cpa-minimized h3{font-size:15px!important;margin:0!important;text-align:left!important;}.isas-cpa-minimized .isas-cpa-content{display:none!important;}.isas-minimize-btn{background:none;border:none;font-size:18px;cursor:pointer;color:#878787;position:absolute;top:12px;right:45px;z-index:2;transition:color 0.2s;padding:0;line-height:1;}.isas-minimize-btn:hover{color:#000;}.isas-cpa-content{max-height:60vh!important;overflow-y:auto!important;padding-right:5px!important;}.isas-badge{display:inline-block;background:#f1f5f9;color:#475569;padding:4px 10px;border-radius:15px;font-size:12px;font-weight:600;cursor:pointer;margin-right:5px;margin-bottom:5px;border:1px solid #e2e8f0;transition:all 0.2s;}.isas-badge:hover{opacity:0.8;}.isas-badge-blue{background:#e0f2fe;color:#0369a1;border-color:#bae6fd;}.isas-badge-orange{background:#fef3c7;color:#b45309;border-color:#fde68a;}.isas-badge-red{background:#fee2e2;color:#b91c1c;border-color:#fecaca;}.isas-badge-green{background:#dcfce7;color:#15803d;border-color:#bbf7d0;}`);

        window.copyToClipboard = (text, el) => {
            const fallbackCopy = () => {
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                try { document.execCommand('copy'); } catch(e) {}
                document.body.removeChild(ta);
            };
            const applySuccess = () => {
                if (!el) return;
                const { background, color, innerHTML } = el.style;
                Object.assign(el.style, { background: '#10b981', color: 'white' });
                el.innerHTML = '✅ Skopírované';
                setTimeout(() => { Object.assign(el.style, { background, color }); el.innerHTML = innerHTML; }, 1000);
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(applySuccess).catch(() => { fallbackCopy(); applySuccess(); });
            } else {
                fallbackCopy();
                applySuccess();
            }
        };

        const escapeHtml = (str) => String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        const badge = (val, label, icon, colorClass = '', title = '') => {
            if (!val || val === '-' || val === 'Bez e-mailu' || val === 'Bez PZP' || val === ', ') return '';
            const safeVal = escapeHtml(val);
            const safeTitle = escapeHtml(title || label || '');
            const safeLabel = label ? escapeHtml(label) + ': ' : '';
            return `<span data-copy="${safeVal}" onclick="copyToClipboard(this.getAttribute('data-copy'), this)" class="isas-badge ${colorClass}" title="Kopírovať ${safeTitle}">${icon} ${safeLabel}${safeVal}</span>`;
        };

        window.ISASClickHandler('.' + cfg.trigger_class, (e, btn) => {
            e.preventDefault();
            e.stopImmediatePropagation();
            if (btn.classList.contains("is-processing")) return;
            btn.classList.add("is-processing");
            setTimeout(() => btn.classList.remove("is-processing"), 400);

            const payloadStr = btn.getAttribute('data-payload'); 
            if (!payloadStr) return;
            
            let data;
            try { data = JSON.parse(payloadStr.replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>')); } 
            catch (err) { return showISASToast("Chyba pri načítaní dát pre asistenta.", "❌", "error"); }

            const modalFields = [];
            
            if (data.url) {
                modalFields.push({ type: 'html', html: `<div style="margin-bottom:20px;padding:15px;background:#f1f5f9;border-radius:8px;border-left:4px solid #2563eb;"><strong style="color:#0f172a;display:block;margin-bottom:5px;">🔗 Portál fabriky:</strong><a href="${data.url}" target="_blank" style="color:#2563eb;text-decoration:none;font-weight:600;">Otvoriť portál v novom okne ↗</a></div>` });
            }
            
            if (data.firma) {
                modalFields.push({ type: 'html', html: `<h4 style="color:#0f172a;margin-bottom:10px;border-bottom:1px solid #e2e8f0;padding-bottom:5px;">🏢 Naša spoločnosť (Dodávateľ)</h4>
                    <div style="margin-bottom:20px;">
                        ${badge(data.firma.nazov, '', '🏢', 'isas-badge-blue', 'Názov')}
                        ${badge(data.firma.ico, 'IČO', '🆔')}
                        ${badge(data.firma.dic, 'DIČ', '📊')}
                        ${badge(data.firma.ic_dph, 'IČ DPH', '🌍')}
                        ${badge(data.firma.adresa, '', '📍', '', 'Adresu')}
                        ${badge(data.firma.telefon, '', '📞', '', 'Telefón')}
                        ${badge(data.firma.email, '', '✉️', '', 'E-mail')}
                        ${badge(data.firma.iban, 'IBAN', '🏦')}
                        ${badge(data.firma.swift, 'SWIFT', '🏦')}
                    </div>` });
            }

            const zakaznik = data.zakaznik || {};
            const technici = Array.isArray(data.technici) ? data.technici : [];
            const auta = Array.isArray(data.auta) ? data.auta : [];

            modalFields.push({ type: 'html', html: `<h4 style="color:#0f172a;margin-bottom:10px;border-bottom:1px solid #e2e8f0;padding-bottom:5px;">📍 Údaje o vstupe a Zákazníkovi</h4>
                ${zakaznik.instrukcie ? `<div style="margin-bottom:10px;padding:10px;background:#fef2f2;border-radius:6px;border-left:4px solid #ef4444;font-size:12px;color:#991b1b;"><strong style="display:block;margin-bottom:4px;">⚠️ Inštrukcie pre vstup:</strong>${zakaznik.instrukcie.replace(/\\n/g, '<br>')}</div>` : ''}
                <div style="margin-bottom:20px;">
                    ${badge(zakaznik.nazov, '', '🏢', 'isas-badge-blue', 'Zákazníka')}
                    ${badge(zakaznik.ico, 'IČO', '🆔')}
                    ${badge(zakaznik.dic, 'DIČ', '🧾')}
                    ${badge(zakaznik.ic_dph, 'IČ DPH', '🏛️')}
                    ${badge(zakaznik.adresa, '', '📍', '', 'Adresu')}
                    ${badge(zakaznik.kontakt, '', '👤', 'isas-badge-green', 'Kontaktnú osobu')}
                    ${badge(zakaznik.telefon, '', '📞', 'isas-badge-green', 'Telefón')}
                    ${badge(data.datum_vstupu, '', '📅', 'isas-badge-orange', 'Dátum')}
                    ${badge(data.ucel, '', '🛠️', 'isas-badge-orange', 'Účel')}
                </div>` });

            modalFields.push({ type: 'html', html: `<h4 style="color:#0f172a;margin-bottom:10px;border-bottom:1px solid #e2e8f0;padding-bottom:5px;">👷 Pracovníci</h4>
                ${technici.map(t => `
                    <div style="margin-bottom:15px;padding-bottom:10px;border-bottom:1px dashed #e2e8f0;">
                        ${badge(t.meno, '', '👤', 'isas-badge-blue', 'Meno')}
                        ${badge(t.op, '', '🆔', '', 'OP')}
                        ${badge(t.narodenie, '', '🎂', '', 'Dátum narodenia')}
                        ${badge(t.stat, '', '🌍', '', 'Štát')}
                        ${badge(t.pomer, '', '🤝', '', 'Typ pomeru')}
                        ${badge(t.telefon, '', '📞', '', 'Telefón')}
                        ${badge(t.email, '', '✉️', '', 'E-mail')}
                        ${badge(t.bozp, 'BOZP', '🦺', 'isas-badge-green')}
                        ${badge(t.vyhlaska, '', '⚡', 'isas-badge-orange', 'Osvedčenie')}
                    </div>`).join('')}` });

            if (auta.length) {
                modalFields.push({ type: 'html', html: `<h4 style="color:#0f172a;margin-top:10px;margin-bottom:10px;border-bottom:1px solid #e2e8f0;padding-bottom:5px;">🚗 Vozidlá</h4>
                    ${data.auta.map(a => `
                        <div style="margin-bottom:10px;">
                            ${badge(a.spz, '', '🚙', 'isas-badge-orange', 'ŠPZ')}
                            ${badge(a.model, '', '📋', '', 'Model')}
                            ${badge(a.typ, '', '📑', '', 'Typ')}
                            ${badge(a.palivo, '', '⛽', '', 'Typ paliva')}
                            ${badge(a.stk, 'STK do', '📋', 'isas-badge-red')}
                            ${badge(a.pzp, 'PZP do', '🛡️', 'isas-badge-red')}
                        </div>`).join('')}` });
            }

            window.ISASModalEngine({
                maxWidth: "650px", icon: false, title: "Copy-Paste Asistent pre Vstupy", text: false,
                fields: modalFields, showClose: false, showCloseX: true, lockBody: true,
                onRender: (m) => {
                    m.overlay.style.background = "transparent"; m.overlay.style.pointerEvents = "none"; m.card.style.pointerEvents = "auto";
                    const minBtn = document.createElement('button'); 
                    minBtn.innerHTML = '🗕'; minBtn.className = 'isas-minimize-btn'; minBtn.title = 'Minimalizovať / Maximalizovať';
                    minBtn.style.cssText = "position:absolute;top:22px;right:50px;background:transparent;border:none;font-size:18px;color:#94a3b8;cursor:pointer;transition:color 0.2s;z-index:10;";
                    minBtn.onclick = () => {
                        const isMin = m.card.classList.toggle('isas-cpa-minimized');
                        minBtn.innerHTML = isMin ? '🗖' : '🗕';
                        document.body.style.overflow = isMin ? '' : 'hidden';
                    };
                    m.card.appendChild(minBtn);
                }
            });
        });
    }

    // =============================================================================
    // WIDGET: KONFIGURÁTOR DIELOV
    // =============================================================================
    if (document.querySelector('.isas-parts-widget-container')) {
        window.ISASStyleEngine('isas-parts-widget-css', `.isas-parts-widget-container { display: inline-flex; align-items: center; gap: 15px; } .isas-parts-config-btn, .isas-parts-config-btn:visited, .isas-parts-config-btn:hover, .isas-parts-config-btn:active { background-color: #9c7751; color: #ffffff !important; padding: 10px 20px; border-radius: 50px; text-decoration: none !important; font-weight: 800; font-size: 13px; text-transform: uppercase; display: inline-flex; align-items: center; gap: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); transition: all 0.2s ease; } .isas-parts-config-btn:hover { background-color: #856342; transform: translateY(-1px); color: #ffffff !important; } .isas-parts-reset-btn { display: none; flex-direction: column; justify-content: center; align-items: center; cursor: pointer; font-size: 10px; font-weight: 800; color: #000000; text-transform: uppercase; text-align: center; line-height: 1.4; transition: all 0.2s ease; letter-spacing: 0.5px; background-color: #f8fafc; border: 1px solid #e2e8f0; padding: 6px 12px; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); } .isas-parts-reset-btn:hover { color: #dc2626; background-color: #fef2f2; border-color: #fca5a5; }`);

        if (localStorage.getItem('isas_skip_save_warning') === 'true') document.querySelectorAll('.isas-parts-reset-btn').forEach(btn => btn.style.display = 'inline-flex');

        window.ISASClickHandler('.isas-parts-config-btn', (e, configBtn) => {
            e.preventDefault(); const targetUrl = configBtn.href, uid = configBtn.getAttribute('data-uid');
            if (localStorage.getItem('isas_skip_save_warning') === 'true') window.location.href = targetUrl;
            else {
                window.ISASModalEngine({
                    icon: '💾', title: 'Máte uložené zmeny?', text: '⚠️ Neuložené dáta vo formulári sa stratia. Chcete pokračovať na správu dielov?',
                    checkboxes: [{ id: 'skip-warning-chk', label: 'Už sa ma to nabudúce nepýtať' }],
                    buttons: [{
                        id: "btn-parts-confirm", text: "✅ Áno, pokračovať", style: "background: #9c7751; color: white; border: none; padding: 14px 25px; border-radius: 12px; font-size: 14px; font-weight: 800; cursor: pointer; transition: all 0.2s;",
                        onClick: (e, m) => {
                            if (m.card.querySelector('#skip-warning-chk').checked) {
                                localStorage.setItem('isas_skip_save_warning', 'true');
                                const resetBtn = document.getElementById('isas-reset-' + uid); if (resetBtn) resetBtn.style.display = 'inline-flex';
                            }
                            window.location.href = targetUrl;
                        }
                    }]
                });
            }
        });

        window.ISASClickHandler('.isas-parts-reset-btn', (e, resetBtn) => {
            localStorage.removeItem('isas_skip_save_warning'); document.querySelectorAll('.isas-parts-reset-btn').forEach(btn => btn.style.display = 'none');
            showISASToast('Upozornenia sú opäť zapnuté 🔔', '✅', 'success');
        });
    }

    // =============================================================================
    // UI CORE AUTOMÁCIE: AKORDEÓNY, SLOVENSKÉ SKLOŇOVANIE A MOBILNÉ KARTY
    // =============================================================================

    /* --- A. AUTOMATICKÁ EXTRAKCIA HLAVIČIEK PRE MOBILNÉ KARTY --- */
    const table = document.getElementById('result_list');
    if (table) {
        const headers = Array.from(table.querySelectorAll('thead th')).map(th => {
            let targetElement = th.querySelector('.text') || th;
            let clone = targetElement.cloneNode(true);
            clone.querySelectorAll('style, script, .sortoptions').forEach(el => el.remove());
            return clone.textContent.replace(/▲|▼/g, '').trim();
        });
        table.querySelectorAll('tbody tr').forEach(row => {
            row.querySelectorAll('td, th').forEach((cell, index) => {
                if (headers[index]) cell.setAttribute('data-label', headers[index]);
            });
        });
    }

    /* --- B. AUTOMATICKÉ SLOVENSKÉ SKLOŇOVANIE (AKUZATÍV PRE TLAČIDLÁ "PRIDAŤ") --- */
    document.querySelectorAll('.object-tools a.addlink').forEach(btn => {
        let text = btn.textContent.trim();
        if (text.toLowerCase().startsWith("pridať ")) {
            let noun = text.substring(7).trim();
            let lowerNoun = noun.toLowerCase();
            let newNoun = noun;
            if (lowerNoun.endsWith("ateľ")) newNoun = noun + "a";
            else if (lowerNoun.endsWith("faktúra")) newNoun = noun.slice(0, -1) + "u";
            else if (lowerNoun.endsWith("á osoba")) newNoun = noun.slice(0, -7) + "ú osobu";
            else if (lowerNoun.endsWith("á položka")) newNoun = noun.slice(0, -9) + "ú položku";
            else if (lowerNoun.endsWith("kategória")) newNoun = noun.slice(0, -9) + "ú kategóriu";
            else if (lowerNoun.endsWith("skupina")) newNoun = noun.slice(0, -1) + "u";
            btn.innerHTML = '<span style="font-size: 16px; margin-right: 6px;">➕</span> PRIDAŤ ' + newNoun.toUpperCase();
        }
    });

    /* --- C. AKORDEÓNOVÉ ZLOŽKY V BOČNOM PANELI A NA DASHBOARDE --- */
    function createIsasFolder(containerId, folderTitle, modelClasses) {
        // 🛡️ OCHRANNÝ ŠTÍT: Na zoznamoch (changelist) a formulároch nikdy nesiahame na #content-main!
        if (containerId === 'content-main' && (document.getElementById('changelist') || document.getElementById('result_list') || document.body.classList.contains('change-list') || document.body.classList.contains('change-form'))) {
            return;
        }

        const container = document.getElementById(containerId);
        if (!container) return;
        
        const rows = [];
        let parentNode = null;

        // Vyberáme striktne iba modelové riadky z navigácie, nie dáta z tabuliek
        modelClasses.forEach(modelClass => {
            const row = container.querySelector('tr.model-' + modelClass);
            if (row && !row.closest('#result_list') && !row.closest('.inline-related') && !row.closest('.djn-items')) {
                rows.push(row);
                if (!parentNode) parentNode = row.parentNode;
            }
        });

        if (rows.length === 0 || !parentNode) return;

        const folderId = 'folder-' + folderTitle.replace(/[^a-zA-Z0-9]/g, '');
        if (parentNode.querySelector('#' + folderId) || container.querySelector('#' + folderId)) return;

        const wrapper = document.createElement('details');
        wrapper.id = folderId;
        wrapper.className = 'isas-acc-folder';
        wrapper.style.cssText = "margin-bottom: 10px; background: #f8fafc; border-radius: 12px; overflow: hidden; border: 1px solid #e2e8f0; width: 100%; box-shadow: 0 2px 4px rgba(0,0,0,0.02);";
        
        const summary = document.createElement('summary');
        summary.style.cssText = "padding: 12px 15px; font-size: 13px; font-weight: 800; color: #0f172a; cursor: pointer; display: flex; align-items: center; gap: 8px; outline: none; transition: background 0.2s; list-style: none;";
        summary.innerHTML = folderTitle;
        
        const contentDiv = document.createElement('div');
        contentDiv.style.cssText = "padding: 5px 0; background: white; border-top: 1px solid #f1f5f9; width: 100%;";

        wrapper.appendChild(summary);
        wrapper.appendChild(contentDiv);
        
        parentNode.insertBefore(wrapper, rows[0]);
        
        rows.forEach(row => {
            row.style.display = "block";
            row.style.width = "100%";
            
            const ths = row.querySelectorAll('th');
            ths.forEach(th => {
                th.style.display = "block";
                th.style.padding = "8px 15px";
                th.style.border = "none";
            });
            
            const tds = row.querySelectorAll('td');
            tds.forEach(td => {
                td.style.display = "none";
            });
            
            contentDiv.appendChild(row);
        });
    }

    function initIsasFolders() {
        ['nav-sidebar', 'content-main'].forEach(cId => {
            createIsasFolder(cId, '🧮 Účtovné a dátové centrum', ['erpexportnastavenia', 'evidenciauhrad', 'megazaloha', 'importdattask', 'aiauditlog']);
            createIsasFolder(cId, '💼 Dodávatelská zóna', ['dodavatel', 'prichodziafaktura', 'odoslanaobjednavka']);
            createIsasFolder(cId, '🚚 Správa vozového parku', ['vozidlo', 'tankovanie', 'evidenciajazd']);
            createIsasFolder(cId, '📦 Sklad a zásoby', ['sklad', 'aktualnenasklade', 'filialka', 'stockmovement', 'kosikspolozkaminadoobjednanie']);
            createIsasFolder(cId, '👥 Personálna agenda', ['zamestnanec', 'certifikat', 'sukromnaosoba', 'hrdashboard', 'nepritomnost', 'pracovnapozicia', 'kandidat']);
            createIsasFolder(cId, '🤝 Klientská zóna', ['zakaznik', 'zakaznikprenajom', 'prijataobjednavka', 'vystavenafaktura', 'cenovaponuka', 'povolenienavstup']);
            createIsasFolder(cId, '📖 Produktová knižnica', ['cenovykatalog', 'technickaspecifikacia', 'databazavyrobcov']);
            createIsasFolder(cId, '🛠️ Servisná prevádzka', ['uloha', 'servisnyzaznam', 'korekciamotohodinlog', 'dashboardkvality', 'typservisuprace', 'modbuskonfigurator']);
            createIsasFolder(cId, '🌐 Základné nastavenia systému', ['firemnaidentita', 'globalnenastavenia', 'statlokalizacia']);
            createIsasFolder(cId, '🔐 Používatelia a Bezpečnosť', ['user', 'group', 'isasauditlog', 'spotifytoken']);
            createIsasFolder(cId, '📨 E-mailové stredisko', ['emailovyucet', 'emailovasprava']);
            createIsasFolder(cId, '🌍 Správa Webstránky', ['webnastavenia', 'webblok', 'webfaq', 'clanok']);
            createIsasFolder(cId, '🔗 API a Webhook Integrácie', ['webhookendpoint', 'webhooklog']);
        });
    }

    initIsasFolders();
    setTimeout(initIsasFolders, 150);

    /* --- D. DETEKCIA STRÁNKY ZMENY HESLA, TLAČIDLO NASPÄŤ A PREPÍNAČE HESIEL (👁️/🙈) --- */
    if (window.location.href.includes('password_change')) {
        document.body.classList.add('isas-password-page');
        const formDiv = document.querySelector('.isas-password-page #content-main form > div');
        if (formDiv && !formDiv.querySelector('.isas-back-btn-top')) {
            const backBtn = document.createElement('a');
            backBtn.href = (window.location.pathname.match(/^(.*?\/admin\/)/) || ['', '/admin/'])[1];
            backBtn.className = "isas-back-btn-top";
            backBtn.innerHTML = "⬅️ Naspäť";
            formDiv.insertBefore(backBtn, formDiv.firstChild);
        }

        // 👁️ Dynamické pridanie oka ku všetkým poliam s heslom
        document.querySelectorAll('.isas-password-page input[type="password"]').forEach(passInput => {
            if (passInput.parentElement.classList.contains('password-wrapper')) return;

            const wrapper = document.createElement('div');
            wrapper.className = 'password-wrapper';
            passInput.parentNode.insertBefore(wrapper, passInput);
            wrapper.appendChild(passInput);

            const toggleBtn = document.createElement('button');
            toggleBtn.type = 'button';
            toggleBtn.className = 'toggle-password-btn';
            toggleBtn.title = 'Zobraziť / Skryť heslo';
            toggleBtn.setAttribute('aria-label', 'Zobraziť heslo');
            toggleBtn.tabIndex = -1;
            toggleBtn.textContent = '👁️';

            toggleBtn.addEventListener('click', function(e) {
                e.preventDefault();
                const isPass = passInput.type === 'password';
                passInput.type = isPass ? 'text' : 'password';
                toggleBtn.textContent = isPass ? '🙈' : '👁️';
                toggleBtn.setAttribute('aria-label', isPass ? 'Skryť heslo' : 'Zobraziť heslo');
                passInput.focus();
            });

            wrapper.appendChild(toggleBtn);
        });
    }

    // =============================================================================
    // 4. ISAS MAGIA (Changelist Engine - Presunuté z utils.py)
    // =============================================================================
    const magiaScript = document.getElementById('isas-magia-config');
    if (magiaScript) {
        let mCfg = {};
        try { mCfg = JSON.parse(magiaScript.textContent); } catch(e) { console.error("ISAS Magia parse error", e); }

        // 1. CSS Injection
        let css = `
            #changelist .errornote { display: none !important; }
            input[name="_save"], .paginator input[type="submit"] { display: none !important; }
            .actions > *:not(.isas-action-btn):not(.isas-singleton-btn) { display: none !important; }
            #changelist-search label { display: none !important; }
            #changelist-search .small, #changelist-search .quiet, #changelist-search span.search-results { display: none !important; width: 0 !important; height: 0 !important; opacity: 0 !important; pointer-events: none !important; }
            .isas-top-bar { display: flex !important; flex-direction: column !important; gap: 0 !important; margin-bottom: 0 !important; width: 100% !important; background: transparent !important; box-sizing: border-box !important; }
            .isas-actions-row { display: flex !important; width: 100% !important; gap: 12px !important; }
            .actions { width: 100% !important; background: transparent !important; border: none !important; padding: 0 !important; display: flex !important; margin: 0 !important; gap: 12px !important; }
            .isas-action-btn, .isas-singleton-btn, .isas-action-btn:hover, .isas-singleton-btn:hover, .isas-action-btn:focus, .isas-singleton-btn:focus, .isas-action-btn:visited, .isas-singleton-btn:visited { flex: 1 !important; color: white !important; border: none !important; border-radius: 8px !important; font-size: 12px !important; font-weight: 900 !important; text-transform: uppercase !important; letter-spacing: 1px !important; cursor: pointer !important; transition: all 0.3s; height: 48px !important; display: flex !important; align-items: center !important; justify-content: center !important; box-shadow: 0 4px 6px rgba(0,0,0,0.1) !important; text-decoration: none !important; }
            .btn-red { background: linear-gradient(135deg, #ef4444 0%, #b91c1c 100%) !important; }
            .btn-blue { background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%) !important; }
            .btn-orange { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%) !important; }
            .btn-green { background: linear-gradient(135deg, #10b981 0%, #059669 100%) !important; }
            .btn-red:hover, .btn-blue:hover, .btn-orange:hover, .btn-green:hover { transform: translateY(-2px) !important; filter: brightness(1.1); box-shadow: 0 6px 12px rgba(0,0,0,0.15) !important;}
            .is-processing { pointer-events: none !important; transform: scale(0.98) !important; opacity: 0.8 !important; }
            .isas-search-anim-column { display: flex !important; flex-direction: column !important; width: 100% !important; gap: 0 !important; box-sizing: border-box !important; }
            #toolbar { margin: 0 !important; padding: 0 !important; background: transparent !important; border: none !important; display: flex !important; width: 100% !important; max-width: 100% !important; box-sizing: border-box !important; }
            #changelist-search { width: 100% !important; display: flex !important; }
            #changelist-search > div:not(.isas-autocomplete-results) { display: flex !important; align-items: stretch !important; gap: 12px !important; width: 100% !important; height: 100% !important; }
            #changelist-search input[type="text"] { border-radius: 8px !important; border: 2px solid #cbd5e1 !important; padding: 0 16px !important; font-size: 14px !important; font-weight: 600 !important; color: #0f172a !important; outline: none !important; transition: all 0.3s ease !important; background: #ffffff !important; height: 48px !important; flex: 1 1 auto !important; width: 100% !important; box-sizing: border-box !important; box-shadow: 0 2px 4px rgba(0,0,0,0.02) !important; }
            #changelist-search input[type="text"]:focus { border-color: #f97316 !important; box-shadow: 0 0 0 4px rgba(249, 115, 22, 0.15) !important; }
            #changelist-search input[type="submit"] { background: #ffffff !important; color: #475569 !important; border: 2px solid #cbd5e1 !important; border-radius: 8px !important; padding: 0 24px !important; font-weight: 900 !important; font-size: 12px !important; text-transform: uppercase !important; cursor: pointer !important; transition: all 0.3s ease !important; height: 48px !important; display: flex !important; align-items: center !important; flex-shrink: 0 !important; box-sizing: border-box !important; box-shadow: 0 2px 4px rgba(0,0,0,0.02) !important; }
            #changelist-search input[type="submit"]:hover { background: #f1f5f9 !important; color: #0f172a !important; border-color: #94a3b8 !important; }
            .isas-reset-search-btn { background: #ffffff !important; color: #ef4444 !important; border: 2px solid #fca5a5 !important; border-radius: 8px !important; padding: 0 20px !important; font-weight: 900 !important; font-size: 12px !important; text-transform: uppercase !important; letter-spacing: 0.5px !important; cursor: pointer !important; transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1) !important; height: 48px !important; display: inline-flex !important; align-items: center !important; justify-content: center !important; flex-shrink: 0 !important; text-decoration: none !important; box-sizing: border-box !important; box-shadow: 0 2px 4px rgba(239, 68, 68, 0.08) !important; gap: 8px !important; }
            .isas-reset-search-btn:hover { background: #fef2f2 !important; border-color: #ef4444 !important; color: #b91c1c !important; transform: translateY(-2px) !important; box-shadow: 0 6px 15px rgba(239, 68, 68, 0.15) !important; }
            .isas-core-workshop { background: #000000 !important; border-radius: 8px !important; padding: 0 20px !important; display: flex !important; align-items: center !important; justify-content: center !important; gap: 15px !important; height: 48px !important; border: 1px solid #1f2937 !important; width: 100% !important; max-width: 100% !important; box-sizing: border-box !important; box-shadow: inset 0 0 15px rgba(52, 211, 153, 0.15), 0 4px 6px rgba(0,0,0,0.1) !important; }
            .workshop-title { color: #34D399 !important; font-weight: 900 !important; letter-spacing: 2px !important; font-size: 15px !important; text-shadow: 0 0 8px rgba(52, 211, 153, 0.6) !important; }
            .workshop-scene { display: flex !important; align-items: center !important; gap: 8px !important; font-size: 18px !important; }
            .worker-1 { animation: workBounce 0.8s infinite alternate ease-in-out; }
            .worker-2 { animation: workBounce 1.1s infinite alternate-reverse ease-in-out; }
            .device { position: relative; animation: deviceGlow 2s infinite alternate; filter: drop-shadow(0 0 5px #34D399); }
            .spark { position: absolute; top: -5px; right: -5px; font-size: 12px; animation: sparkFlash 0.5s infinite alternate; }
            .van { animation: vanIdle 1.5s infinite ease-in-out; }
            @keyframes workBounce { 0% { transform: translateY(0px) rotate(0deg); } 100% { transform: translateY(-3px) rotate(15deg); } }
            @keyframes deviceGlow { 0% { filter: drop-shadow(0 0 2px #34D399); } 100% { filter: drop-shadow(0 0 10px #34D399); } }
            @keyframes sparkFlash { 0% { opacity: 0; transform: scale(0.5); color: #fde047; } 100% { opacity: 1; transform: scale(1.3); color: #34D399; } }
            @keyframes vanIdle { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-2px); } }
        `;
        if (mCfg.skryt_vyhladavanie) css += `#changelist-search { display: none !important; }`;
        if (mCfg.skryt_akcie) css += `.actions { display: none !important; }`;

        if (mCfg.typ_zobrazenia === 'karta') {
            css += `
            #result_list { border: none !important; box-shadow: none !important; background: transparent !important; width: 100% !important; }
            #result_list thead { display: none !important; } 
            #result_list tbody tr { display: grid !important; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)) !important; gap: 20px !important; padding: 35px !important; border-radius: 24px !important; align-items: stretch !important; margin-bottom: 25px !important; transition: all 0.3s ease !important; background: linear-gradient(135deg, hsl(210, 20%, 96%) 0%, hsl(170, 20%, 95%) 100%) !important; box-shadow: 0 10px 30px -10px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.03) !important; border: 1px solid #f1f5f9 !important; }
            #result_list tbody tr:hover { transform: translateY(-3px) !important; box-shadow: 0 20px 40px -15px rgba(0,0,0,0.1), 0 2px 5px rgba(0,0,0,0.04) !important; border-color: #e2e8f0 !important; }
            #result_list tbody td, #result_list tbody th { display: block !important; border: none !important; padding: 0 !important; vertical-align: top !important; text-align: left !important; background: transparent !important; box-shadow: none !important; }
            #result_list .action-checkbox { display: none !important; width: 0 !important; height: 0 !important; margin: 0 !important; padding: 0 !important; }
            #result_list tbody th:first-child, #result_list tbody th.field-get_nazov_vizual, #result_list tbody th.field-get_core_vizual { grid-column: 1 / -1 !important; padding: 20px !important; border-radius: 16px !important; margin-bottom: 15px !important; box-shadow: inset 0 2px 5px rgba(0,0,0,0.02); }
            #result_list tbody td { pointer-events: none !important; }
            #result_list tbody td * { pointer-events: auto !important; }
            `;
        } else if (mCfg.typ_zobrazenia === 'dashboard') {
            css += `
            #result_list { border: none !important; box-shadow: none !important; background: transparent !important; width: 100% !important; }
            #result_list thead { display: none !important; }
            #result_list tbody tr { background: transparent !important; border: none !important; display: block !important; width: 100% !important; }
            #result_list tbody td, #result_list tbody th { padding: 0 !important; border: none !important; display: block !important; width: 100% !important; text-align: left !important; }
            #result_list .action-checkbox { display: none !important; }
            `;
        } else {
            css += `
            table#result_list { width: 100% !important; table-layout: auto !important; border-collapse: collapse !important; }
            table#result_list th.action-checkbox-column, table#result_list td.action-checkbox { width: 1% !important; padding: 0 10px !important; white-space: nowrap !important; text-align: center !important; vertical-align: middle !important; }
            table#result_list td.action-checkbox input { margin: 0 auto !important; display: block !important; cursor: pointer !important; }
            table#result_list thead th:not(.action-checkbox-column) { text-align: center !important; vertical-align: middle !important; padding: 0 10px !important; white-space: nowrap !important; width: auto !important; height: 34px !important; }
            table#result_list thead th .text { display: flex !important; justify-content: center !important; align-items: center !important; width: max-content !important; margin: 0 auto !important; height: 34px !important; padding: 0 !important; }
            table#result_list tbody tr { height: 85px !important; }
            table#result_list tbody td, table#result_list tbody th { text-align: center !important; vertical-align: middle !important; padding: 4px 6px !important; height: 85px !important; box-sizing: border-box !important; }
            #changelist table#result_list tbody th a, #changelist table#result_list tbody td a, #changelist table tbody th a, #changelist table tbody td a, table#result_list tbody td a, table#result_list tbody th a { display: flex !important; align-items: center !important; justify-content: center !important; width: 100% !important; height: 100% !important; padding: 0 !important; margin: 0 !important; text-decoration: none !important; -webkit-user-drag: none !important; user-select: auto !important; text-align: center !important; }
            table#result_list tbody td > div, table#result_list tbody th > div, table#result_list tbody td > a > div, table#result_list tbody th > a > div { display: flex !important; flex-direction: column !important; align-items: center !important; justify-content: safe center !important; width: 100% !important; max-width: 100% !important; max-height: 77px !important; overflow-y: auto !important; overflow-x: hidden !important; padding: 2px 4px !important; box-sizing: border-box !important; scrollbar-width: none !important; margin: 0 auto !important; }
            table#result_list tbody td > span, table#result_list tbody th > span { display: inline-flex !important; align-items: center !important; justify-content: center !important; margin: 0 auto !important; }
            table#result_list tbody img, table#result_list tbody svg { max-height: 32px !important; width: auto !important; object-fit: contain !important; display: block !important; margin: 0 auto !important; }
            .isas-autocomplete-results { position: absolute; top: calc(100% + 8px); right: 0; left: 0; background: white; border-radius: 12px; box-shadow: 0 20px 40px rgba(0,0,0,0.15); max-height: 350px; overflow-y: auto; z-index: 10000; display: none; border: 1px solid #e2e8f0; }
            .isas-autocomplete-results:empty { display: none !important; border: none !important; }
            .isas-autocomplete-item { padding: 12px 18px; border-bottom: 1px solid #f1f5f9; cursor: pointer; transition: all 0.2s; text-align: left !important; }
            .isas-autocomplete-item:last-child { border-bottom: none; }
            .isas-autocomplete-item:hover { background: #fff7ed; }
            .isas-highlight-match { background-color: #ffedd5 !important; color: #c2410c !important; font-weight: 900 !important; padding: 2px 4px !important; border-radius: 4px !important; border-bottom: 2px solid #f97316 !important; }
            table#result_list tbody tr.vivid-row-flash td, table#result_list tbody tr.vivid-row-flash th { background-color: #ffedd5 !important; border-top: 4px solid #f97316 !important; border-bottom: 4px solid #f97316 !important; transition: all 0.4s ease !important; color: #431407 !important;}
            table#result_list tbody tr.vivid-row-flash td:first-child { border-left: 4px solid #f97316 !important; border-top-left-radius: 12px !important; border-bottom-left-radius: 12px !important; }
            table#result_list tbody tr.vivid-row-flash td:last-child { border-right: 4px solid #f97316 !important; border-top-right-radius: 12px !important; border-bottom-right-radius: 12px !important; }
            `;
        }

        if (mCfg.is_user_admin && mCfg.selector_texts) {
            css += `
            .selector-available-title::before, .selector-available h2::before { content: "${mCfg.selector_texts.available || '📦 DOSTUPNÉ DÁTA'}" !important; }
            .selector-chosen-title::before, .selector-chosen h2::before { content: "${mCfg.selector_texts.chosen || '✅ VYBRANÉ DÁTA'}" !important; }
            .selector-chooseall::before { content: "${mCfg.selector_texts.choose_all || '⚡ Vybrať všetky dáta'}" !important; }
            .selector-clearall::before { content: "${mCfg.selector_texts.clear_all || '🗑️ Odstrániť všetky dáta'}" !important; }
            `;
        }

        window.ISASStyleEngine('isas-magia-css', css);

        // 2. User Admin Text Replacement
        if (mCfg.is_user_admin) {
            const walkerUser = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            let nodeUser;
            while(nodeUser = walkerUser.nextNode()) {
                let text = nodeUser.nodeValue;
                if (text.includes('After you') && text.includes('created a user')) {
                    nodeUser.nodeValue = '👤 Po vytvorení používateľa budete môcť upravovať ďalšie nastavenia a oprávnenia účtu. ⚙️';
                }
            }
            document.querySelectorAll('.selector-filter input').forEach(el => { 
                if (el.placeholder !== 'Filtrovať...') el.placeholder = 'Filtrovať...'; 
            });
        }

        // 3. Katalog Dashboard
        const urlParams = new URLSearchParams(window.location.search);
        const isKatalogPrePage = mCfg.katalogovy_dashboard && !urlParams.get('hlavna_kat') && !urlParams.has('q');

        if (mCfg.katalogovy_dashboard && mCfg.katalog_choices) {
            const hlavnaKat = urlParams.get('hlavna_kat');

            if (isKatalogPrePage) {
                document.body.classList.add('isas-katalog-dashboard-active');

                window.ISASStyleEngine('isas-katalog-dash-hide-css', `
                    body.change-list.isas-katalog-dashboard-active,
                    body.isas-katalog-dashboard-active,
                    body.isas-katalog-dashboard-active #container,
                    body.isas-katalog-dashboard-active #content,
                    body.isas-katalog-dashboard-active #content-main {
                        height: auto !important;
                        max-height: none !important;
                        overflow: visible !important;
                    }
                    body.change-list.isas-katalog-dashboard-active #changelist,
                    body.list.isas-katalog-dashboard-active #changelist,
                    body.isas-katalog-dashboard-active #changelist,
                    body.isas-katalog-dashboard-active .changelist-form-container,
                    body.isas-katalog-dashboard-active #changelist-form,
                    body.isas-katalog-dashboard-active .results,
                    body.isas-katalog-dashboard-active #result_list,
                    body.isas-katalog-dashboard-active #changelist-filter,
                    body.isas-katalog-dashboard-active .isas-top-bar,
                    body.isas-katalog-dashboard-active #toolbar,
                    body.isas-katalog-dashboard-active .actions,
                    body.isas-katalog-dashboard-active .paginator,
                    body.isas-katalog-dashboard-active .filtered,
                    body.isas-katalog-dashboard-active #changelist-search {
                        display: none !important;
                        height: 0 !important;
                        min-height: 0 !important;
                        max-height: 0 !important;
                        margin: 0 !important;
                        padding: 0 !important;
                        border: none !important;
                        visibility: hidden !important;
                        pointer-events: none !important;
                    }
                    #isas-katalog-wrapper {
                        display: flex !important;
                        flex-direction: column !important;
                        align-items: center !important;
                        justify-content: center !important;
                        width: 100% !important;
                        padding: 20px 0 !important;
                    }
                `);

                const changelist = document.querySelector('#changelist');
                if (changelist) changelist.style.display = 'none';
                
                let dash = document.createElement('div');
                dash.id = "isas-katalog-dashboard-grid";
                dash.style.cssText = "display: flex; gap: 20px; flex-wrap: wrap; justify-content: center; width: 100%;";

                mCfg.katalog_choices.forEach(cat => {
                    let card = document.createElement('a');
                    card.href = "?hlavna_kat=" + encodeURIComponent(cat.key);
                    card.innerHTML = `
                        <div style="font-size: 55px; margin-bottom: 15px; filter: drop-shadow(0 6px 10px rgba(0,0,0,0.1));">${cat.icon}</div>
                        <div style="font-size: 16px; font-weight: 900; text-transform: uppercase; letter-spacing: 1px;">${cat.label}</div>
                    `;
                    card.style.cssText = `
                        background: ${cat.bg}; color: ${cat.text}; padding: 50px 30px; border-radius: 24px; 
                        text-decoration: none; text-align: center; flex: 1; min-width: 280px; max-width: 380px; 
                        box-shadow: 0 15px 30px ${cat.shadow}; border: 1px solid rgba(255,255,255,0.6); 
                        transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1); display: flex; flex-direction: column; 
                        align-items: center; justify-content: center; cursor: pointer;
                    `;
                    card.onmouseover = () => { card.style.transform = "translateY(-8px) scale(1.02)"; card.style.boxShadow = `0 25px 50px ${cat.shadow}`; };
                    card.onmouseout = () => { card.style.transform = "translateY(0) scale(1)"; card.style.boxShadow = `0 15px 30px ${cat.shadow}`; };
                    dash.appendChild(card);
                });
                
                let animContainer = document.createElement('div');
                animContainer.id = "isas-katalog-workshop-banner";
                animContainer.style.cssText = "width: 100%; margin-bottom: 30px; display: flex; justify-content: center;";
                animContainer.innerHTML = `
                    <div class="isas-core-workshop" style="max-width: 450px;">
                        <span class="workshop-title">ISAS CORE</span>
                        <div class="workshop-scene">
                            <span class="worker-1">👨‍🔧</span>
                            <span class="device">🖥️<span class="spark">⚡</span></span>
                            <span class="worker-2">👨‍🔧</span>
                            <span class="van">🚐</span>
                        </div>
                    </div>
                `;
                
                let contentMain = document.querySelector('#content-main');
                if(contentMain && !document.querySelector('#isas-katalog-dashboard-grid')) {
                    let wrapper = document.createElement('div');
                    wrapper.id = 'isas-katalog-wrapper';
                    wrapper.appendChild(animContainer);
                    wrapper.appendChild(dash);
                    contentMain.appendChild(wrapper);
                }
            } 
            else if (hlavnaKat) {
                let backBtn = document.createElement('a');
                backBtn.href = window.location.pathname;
                backBtn.className = 'isas-action-btn isas-katalog-back-btn';
                backBtn.id = 'isas-katalog-back-btn';
                backBtn.title = 'Späť na výber hlavných kategórií ⬅';
                backBtn.innerHTML = "<span class='btn-text-fade'>⬅ SPÄŤ NA KATEGÓRIE</span>";
                backBtn.style.cssText = `
                    background: linear-gradient(135deg, #475569 0%, #1e293b 100%) !important; 
                    color: #ffffff !important; border: none !important; border-radius: 8px !important; 
                    font-size: 12px !important; font-weight: 900 !important; text-transform: uppercase !important; 
                    letter-spacing: 0.5px !important; cursor: pointer !important; transition: all 0.3s !important; 
                    height: 48px !important; display: inline-flex !important; align-items: center !important; 
                    justify-content: center !important; box-shadow: 0 4px 6px rgba(0,0,0,0.1) !important; 
                    text-decoration: none !important; flex-shrink: 0 !important; padding: 0 22px !important; 
                    margin: 0 !important; white-space: nowrap !important; width: auto !important;
                `;
                backBtn.onmouseover = () => { backBtn.style.transform = "translateY(-2px)"; backBtn.style.filter = "brightness(1.15)"; };
                backBtn.onmouseout = () => { backBtn.style.transform = "translateY(0)"; backBtn.style.filter = "none"; };
                
                const saveBtn = document.querySelector("#isas-custom-save-btn");
                const searchDiv = document.querySelector("#changelist-search > div:not(.isas-autocomplete-results)") || document.querySelector("#toolbar");

                if (saveBtn && saveBtn.parentNode) {
                    backBtn.style.flex = "1";
                    saveBtn.parentNode.appendChild(backBtn);
                } else if (searchDiv) {
                    searchDiv.appendChild(backBtn);
                } else {
                    let contentMain = document.querySelector('#content-main');
                    if (contentMain) contentMain.insertBefore(backBtn, contentMain.firstChild);
                }
            }
        }

        // 4. Top Bar & Workshop & Live Search
        if (!mCfg.cisty_dashboard) {
            const changelistForm = document.querySelector("#changelist-form");
            const toolbar = document.querySelector("#toolbar");
            let actionsContainer = document.querySelector("#changelist-form .actions") || document.querySelector(".actions");

            if (!actionsContainer) {
                actionsContainer = document.createElement("div");
                actionsContainer.className = "actions isas-forced-actions";
                if (changelistForm) changelistForm.insertBefore(actionsContainer, changelistForm.firstChild);
            }
            
            Array.from(actionsContainer.children).forEach(child => { 
                if (!child.classList.contains('isas-action-btn') && !child.classList.contains('isas-singleton-btn')) child.style.display = 'none'; 
            });

            const btnId = mCfg.vyzaduje_potvrdenie ? 'btn-fast-delete' : 'btn-delete-selected';
            if (!mCfg.bez_tlacidla_odstranit && !document.querySelector("#" + btnId)) {
                const btnDelete = document.createElement("button"); 
                btnDelete.type = "button"; btnDelete.id = btnId; 
                btnDelete.className = (btnId === 'btn-fast-delete') ? "isas-singleton-btn btn-red" : "isas-action-btn btn-red";
                btnDelete.innerHTML = "🗑️ Odstrániť"; 
                actionsContainer.appendChild(btnDelete);
            }
            
            // 🛡️ OCHRANA PROTI DUPLICITÁM: Vložíme tlačidlá iba ak tam ešte nie sú
            if (mCfg.custom_buttons_html && !actionsContainer.dataset.customInjected) {
                actionsContainer.insertAdjacentHTML('beforeend', mCfg.custom_buttons_html);
                actionsContainer.dataset.customInjected = 'true';
            }

            if (changelistForm && changelistForm.parentNode) {
                // 🛡️ DJANGO 6.1 FIX: Vytiahnutie filtra z form-containeru späť do changelistu ako súrodenca (stav 5.2)
                const filterEl = document.querySelector('#changelist-filter');
                const changelistEl = document.querySelector('#changelist');
                if (filterEl && changelistEl && filterEl.parentNode !== changelistEl) {
                    changelistEl.appendChild(filterEl);
                }

                let topBar = document.querySelector('.isas-top-bar');
                if (!topBar) { 
                    topBar = document.createElement('div'); topBar.className = 'isas-top-bar'; 
                    const actionsRow = document.createElement('div'); actionsRow.className = 'isas-actions-row'; actionsRow.appendChild(actionsContainer);
                    const searchAnimColumn = document.createElement('div'); searchAnimColumn.className = 'isas-search-anim-column';
                    const animBox = document.createElement('div'); animBox.className = 'isas-core-workshop';
                    animBox.innerHTML = '<span class="workshop-title">ISAS CORE</span><div class="workshop-scene"><span class="worker-1">👨‍🔧</span><span class="device">🖥️<span class="spark">⚡</span></span><span class="worker-2">👨‍🔧</span><span class="van">🚐</span></div>';
                    
                    if(toolbar) searchAnimColumn.appendChild(toolbar);
                    searchAnimColumn.appendChild(animBox);
                    topBar.appendChild(actionsRow); topBar.appendChild(searchAnimColumn);
                    changelistForm.parentNode.insertBefore(topBar, changelistForm); 
                }
                
                if (toolbar) {
                    const searchInput = toolbar.querySelector('input[type="text"]');
                    const searchContainer = document.querySelector('#changelist-search');
                    
                    // 🛡️ OCHRANA PROTI DUPLICITÁM: Vytvoríme resultsDiv iba ak neexistuje
                    if (searchInput && searchContainer && !searchContainer.querySelector('.isas-autocomplete-results')) {
                        searchInput.placeholder = mCfg.search_placeholder || "🔍 Vyhľadávanie..."; 
                        searchInput.setAttribute("autocomplete", "off"); 
                        searchContainer.style.position = "relative"; 
                        
                        const resultsDiv = document.createElement("div"); 
                        resultsDiv.className = "isas-autocomplete-results"; 
                        searchContainer.appendChild(resultsDiv);
                        
                        if (mCfg.live_search_endpoint) {
                            const hideResults = () => { resultsDiv.style.display = 'none'; resultsDiv.innerHTML = ''; };
                            
                            searchInput.addEventListener('input', function(e) {
                                e.preventDefault(); 
                                const term = this.value.trim(); 
                                if (term.length < 2) { hideResults(); return; }
                                
                                fetch(`${mCfg.live_search_endpoint}?term=${encodeURIComponent(term)}`).then(res => res.json()).then(data => {
                                    if (searchInput.value.trim().length < 2) { hideResults(); return; }
                                    resultsDiv.innerHTML = '';
                                    if (data.results && data.results.length > 0) {
                                        const regex = new RegExp("(" + term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ")", "gi");
                                        
                                        data.results.forEach(item => {
                                            const div = document.createElement('div'); div.className = 'isas-autocomplete-item';
                                            const highlightedName = item.name ? item.name.replace(regex, "<span class='isas-highlight-match'>$1</span>") : (item.model ? item.model.replace(regex, "<span class='isas-highlight-match'>$1</span>") : "");
                                            const extraInfo = item.cat || mCfg.live_search_kategoria || "ZÁZNAM";
                                            
                                            const itemIcon = item.icon || mCfg.live_search_ikona || '🔍';
                                            if(item.action_display) {
                                                const highlightedUser = item.user.replace(regex, "<span class='isas-highlight-match'>$1</span>");
                                                const highlightedId = item.obj_id.replace(regex, "<span class='isas-highlight-match'>$1</span>");
                                                div.innerHTML = `<div style="display:flex; justify-content:space-between; align-items:center;"><strong>${itemIcon} ${item.action_display}</strong></div><div style="margin-top: 4px;"><span style="font-size: 11px; color: #475569; font-weight: 700;">📂 ${highlightedName} (ID: ${highlightedId}) | 👤 ${highlightedUser}</span></div>`;
                                            } else {
                                                const emailInfo = item.email ? `<div style="margin-top: 4px;"><span style="font-size: 11px; color: #475569; font-weight: 700;">📧 ${item.email}</span></div>` : '';
                                                const telefonInfo = item.telefon ? `<div style="margin-top: 4px;"><span style="font-size: 11px; color: #475569; font-weight: 700;">📞 ${item.telefon}</span></div>` : '';
                                                const sumaInfo = item.suma ? `<div style="margin-top: 4px;"><span style="font-size: 12px; color: #059669; font-weight: 900;">💳 ${item.suma}</span></div>` : '';
                                                const dateInfo = item.date ? `<div style="margin-top: 4px;"><span style="font-size: 10px; color: #94a3b8; font-weight: 700;">🕒 ${item.date}</span></div>` : '';
                                                const rolaInfo = item.rola ? item.rola : extraInfo;
                                                div.innerHTML = `<div style="display:flex; justify-content:space-between; align-items:center;"><strong>${itemIcon} ${highlightedName}</strong><span style="font-size: 11px; font-weight: 900; color: #0f172a; background: #f1f5f9; padding: 2px 6px; border-radius: 4px;">${rolaInfo}</span></div>${emailInfo}${telefonInfo}${sumaInfo}${dateInfo}`;
                                            }
                                            
                                            div.onclick = function(e) { 
                                                e.stopPropagation(); 
                                                const searchVal = item.search_value || item.name || item.obj_id;
                                                window.location.href = "?q=" + encodeURIComponent(searchVal) + "#highlight-" + item.id; 
                                            };
                                            resultsDiv.appendChild(div);
                                        });
                                        resultsDiv.style.display = 'block';
                                    } else { hideResults(); }
                                });
                            });
                            searchInput.addEventListener('search', function() { if (this.value.trim().length < 2) hideResults(); });
                            document.addEventListener('click', function(e) { if (e.target !== searchInput && !resultsDiv.contains(e.target)) { hideResults(); } });
                            document.addEventListener('keydown', function(e) { if (e.key === "Escape") { hideResults(); searchInput.blur(); } });
                        }
                    }
                    const searchBtn = toolbar.querySelector('input[type="submit"]'); 
                    if (searchBtn) {
                        searchBtn.value = "Vyhľadať 🔎";
                        const urlParams = new URLSearchParams(window.location.search);
                        if (urlParams.toString().length > 0 && !document.querySelector('.isas-reset-search-btn')) {
                            const resetBtn = document.createElement('a');
                            resetBtn.className = 'isas-reset-search-btn';
                            resetBtn.href = window.location.pathname;
                            resetBtn.title = 'Vyčistiť vyhľadávanie a resetovať všetky aktívne filtre 🔄';
                            resetBtn.innerHTML = '<span>🔄</span> Obnoviť';
                            searchBtn.parentNode.insertBefore(resetBtn, searchBtn.nextSibling);
                        }
                    }
                }
                
                // 🛡️ Zabránenie odoslaniu formulára pri stlačení ENTER vo vyhľadávaní
                changelistForm.addEventListener("submit", function(e) {
                    if (document.activeElement === document.querySelector('#changelist-search input[type="text"]')) {
                        e.preventDefault();
                    }
                });
            }
        }

        // 5. Generic Action Button Handler (Pre tlačidlá mimo action_modals)
        window.ISASClickHandler('.isas-action-btn, .isas-singleton-btn', (e, btnAction) => {
            // 🛡️ ARCHITEKTONICKÝ FILTER: Odkazy (<a>) a prvky bez data-action majú trvalú imunitu
            const isDeleteBtn = btnAction.id === "btn-delete-selected" || btnAction.id === "btn-fast-delete";
            const hasAction = btnAction.hasAttribute('data-action') || isDeleteBtn;
            if (!hasAction || (btnAction.tagName === 'A' && !btnAction.hasAttribute('data-action'))) return;

            if (isasConfig.action_modals && isasConfig.action_modals[btnAction.id]) return;
            
            const ignoreIds = mCfg.ignore_default_action_ids || [];
            if (ignoreIds.includes(btnAction.id)) return;

            e.preventDefault(); e.stopImmediatePropagation();

            e.preventDefault(); e.stopImmediatePropagation();
            if (btnAction.classList.contains("is-processing")) return;

            let id = null;
            let ids = [];
            if (mCfg.vyzaduje_potvrdenie) {
                const cb = document.querySelector('#result_list tbody tr input.action-select');
                if (cb) { id = cb.value; }
                else {
                    const editLink = document.querySelector('#result_list tbody tr a');
                    const match = editLink ? editLink.getAttribute('href').match(/\/(\d+)\//) : null;
                    if (match) id = match[1];
                }
                if (!id) return showISASToast("Nenašiel sa žiadny záznam na odstránenie.", "📭", "warning");
                ids = [id];
            } else {
                const checked = document.querySelectorAll('input.action-select:checked');
                if (checked.length === 0) return showISASToast(mCfg.upozornenie_prazdny_vyber || "Najskôr zaškrtnite aspoň jeden záznam zo zoznamu.", "⚠️", "warning");
                ids = Array.from(checked).map(cb => cb.value);
            }

            const actionType = btnAction.getAttribute('data-action') || (btnAction.id === "btn-delete-selected" || btnAction.id === "btn-fast-delete" ? 'delete' : '');
            const reason = btnAction.dataset.gdprReason || '';

            const executeFastAction = (customReason) => {
                const finalReason = (typeof customReason === 'string') ? customReason : reason;
                
                // 📥 Vlastný onComplete callback pre spracovanie download_url
                window.ISASAction({ type: actionType, ids: ids, reason: finalReason }, btnAction, {
                    url: "fast-action/",
                    loadingText: "⏳...",
                    onComplete: (data, btn, origHtml) => {
                        if (data.status === "success") {
                            window.showISASToast(data.message, '✅', 'success');
                            btn.innerHTML = "✅ HOTOVO";
                            if (data.download_url) {
                                const a = document.createElement("a");
                                a.href = data.download_url;
                                a.setAttribute("download", "");
                                document.body.appendChild(a);
                                a.click();
                                document.body.removeChild(a);
                            }
                            setTimeout(() => window.location.reload(), 1500);
                        } else {
                            window.showISASToast(data.message, '❌', 'error');
                            btn.classList.remove("is-processing");
                            btn.innerHTML = origHtml;
                        }
                    }
                });
            };

            if (mCfg.vyzaduje_potvrdenie) {
                window.showGlobalConfirmModal(mCfg.potvrdenie_nadpis || "Ste si istý? 🚨", mCfg.potvrdenie_text || "Tento krok natrvalo odstráni dáta. 💥", executeFastAction);
                return;
            }

            const actLower = (actionType || '').toLowerCase();
            if (actLower === 'delete') {
                window.showGlobalConfirmModal("Potvrdenie vymazania 🗑️", "Naozaj chcete natrvalo odstrániť vybrané záznamy? Tento krok sa nedá vrátiť späť.", executeFastAction);
            } else if (actLower === 'gdpr_anonymize' || actLower.includes('anonymiz')) {
                if (typeof window.showGdprHardConfirmModal === 'function') {
                    window.showGdprHardConfirmModal(ids.length, executeFastAction);
                } else {
                    window.showGlobalConfirmModal("Anonymizácia dát 🛡️", "Naozaj chcete anonymizovať vybrané záznamy? Tento krok je nezvratný.", executeFastAction);
                }
            } else if (actLower === 'lock' || actLower.includes('zamkn')) {
                window.showGlobalConfirmModal("Uzamknutie záznamov 🔒", "Naozaj chcete uzamknúť vybrané záznamy pred úpravami?", executeFastAction);
            } else if (actLower === 'unlock' || actLower.includes('odomkn')) {
                window.showGlobalConfirmModal("Odomknutie záznamov 🔓", "Naozaj chcete odomknúť vybrané záznamy pre úpravy?", executeFastAction);
            } else if (actLower.includes('storno') || actLower.includes('cancel') || actLower.includes('zrus')) {
                window.showGlobalConfirmModal("Stornovanie záznamov 🛑", "Naozaj chcete stornovať / zrušiť vybrané záznamy?", executeFastAction);
            } else if (actLower.includes('reset')) {
                window.showGlobalConfirmModal("Resetovanie hodnôt 🔄", "Naozaj chcete resetovať stav vybraných záznamov?", executeFastAction);
            } else {
                window.showGlobalConfirmModal("Potvrdenie akcie ⚠️", "Naozaj chcete vykonať túto akciu s vybranými záznamami?", executeFastAction);
            }
        });
    }

    // 🛡️ JUKEBOX INTEGRÁCIA: Ak používateľ zapne Jukebox v inej karte, AI asistent okamžite stíchne
    window.addEventListener('storage', function(e) {
        if (e.key === 'isas_global_command_stop_tts') {
            if (window.ISASStopSpeech) window.ISASStopSpeech();
            document.querySelectorAll('.ai-tts-btn.is-playing').forEach(b => {
                b.classList.remove('is-playing');
                b.innerHTML = '🔊 Prečítať';
            });
        }
    });
});

// =============================================================================
// FUNKCIE MIMO DOMContentLoaded (Dostupné globálne pre toasty)
// =============================================================================
function showISASToast(text, icon, type = 'success') {
    const toast = document.createElement("div");
    const colors = { success: { color: '#10b981', bg: '#ecfdf5', shadow: 'rgba(16, 185, 129, 0.2)' }, warning: { color: '#f59e0b', bg: '#fffbeb', shadow: 'rgba(245, 158, 11, 0.2)' }, error: { color: '#ef4444', bg: '#fef2f2', shadow: 'rgba(239, 68, 68, 0.2)' } };
    const theme = colors[type] || colors.success;
    const safeText = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");

    toast.style.cssText = `position: fixed; top: -100px; left: 50%; transform: translateX(-50%); background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); color: #0f172a; padding: 14px 24px 14px 18px; border-radius: 50px; font-family: system-ui, -apple-system, sans-serif; font-weight: 700; font-size: 14px; box-shadow: 0 15px 40px rgba(0,0,0,0.12), 0 5px 10px rgba(0,0,0,0.05), inset 0 0 0 1px ${theme.color}40; display: flex; align-items: center; gap: 12px; z-index: 999999; transition: all 0.6s cubic-bezier(0.34, 1.56, 0.64, 1); opacity: 0; pointer-events: none; max-width: 90%;`;
    toast.innerHTML = `<div style="background: ${theme.bg}; color: ${theme.color}; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; border-radius: 50%; font-size: 14px; box-shadow: 0 2px 5px ${theme.shadow}; flex-shrink: 0;">${icon}</div><span style="letter-spacing: -0.2px;">${safeText}</span>`;
    
    document.body.appendChild(toast);
    requestAnimationFrame(() => setTimeout(() => { toast.style.top = "40px"; toast.style.opacity = "1"; }, 50));
    setTimeout(() => { toast.style.top = "-100px"; toast.style.opacity = "0"; setTimeout(() => toast.remove(), 600); }, 4000);
}