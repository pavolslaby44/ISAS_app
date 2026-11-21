/* =========================================================================
   ISAS MASTER: ÚLOHA FORM (Čistý AJAX Filter Zariadení)
   ========================================================================= */
(function($) {
    'use strict';

    if (typeof $ !== 'function') return;

    // --- LOGIKA PRE FILTER ZARIADENÍ PODĽA ZÁKAZNÍKA (AJAX) ---
    function initFilterZariadenia() {
        var $zakaznikField = $('#id_zakaznik');
        var $zariadenieContainer = $('#id_zariadenia');
        var activeXhr = null;
        var initTimer = null;
        // Ak dané polia na stránke nie sú, skript sa bezpečne ukončí a nič nepokazí
        if (!$zakaznikField.length || !$zariadenieContainer.length) return;

        var inputFieldName = $zariadenieContainer.find('input[type="checkbox"]').first().attr('name') || ($zakaznikField.attr('name') ? $zakaznikField.attr('name').replace(/zakaznik(_id)?$/, 'zariadenia') : 'zariadenia');

        // Krajší vizuál pre box s checkboxami
        $zariadenieContainer.css({
            'list-style': 'none',
            'padding': '10px',
            'border': '1px solid #e2e8f0',
            'border-radius': '8px',
            'background': '#f8fafc',
            'max-height': '300px',
            'overflow-y': 'auto',
            'box-shadow': 'inset 0 2px 4px rgba(0,0,0,0.02)'
        });

        function loadDevices(zakaznikId, selectedIds, isCustomerChange) {
            if (activeXhr && typeof activeXhr.abort === 'function') {
                activeXhr.abort();
                activeXhr = null;
            }

            var safeSelected = [];
            var safeLabels = {};
            if (Array.isArray(selectedIds)) {
                $.each(selectedIds, function(_, s) {
                    if (s !== null && s !== undefined) {
                        if (typeof s === 'object' && s.id !== undefined && s.id !== null) {
                            var sId = String(s.id).trim();
                            if (sId) {
                                safeSelected.push(sId);
                                safeLabels[sId] = s.text ? String(s.text).trim() : ('Zariadenie #' + sId);
                            }
                        } else {
                            var rawStrId = String(s).trim();
                            if (rawStrId) {
                                safeSelected.push(rawStrId);
                            }
                        }
                    }
                });
            }

            var cleanCustomerId = (zakaznikId !== null && zakaznikId !== undefined) ? String(zakaznikId).trim() : '';
            if (!cleanCustomerId || !/^\d+$/.test(cleanCustomerId)) {
                $zariadenieContainer.css('opacity', '1').html('<li><em style="color: #94a3b8; font-size: 13px;">ℹ️ Najprv vyberte zákazníka z roletky vyššie...</em></li>');
                return;
            }

            var hasExistingCheckboxes = $zariadenieContainer.find('input[type="checkbox"]').length > 0;
            if (isCustomerChange || !hasExistingCheckboxes) {
                $zariadenieContainer.html('<li><em style="color: #64748b; font-size: 13px;">⏳ Načítavam zariadenia zákazníka...</em></li>');
            } else {
                $zariadenieContainer.css('opacity', '0.6');
            }

            var pathPrefixMatch = window.location.pathname.match(/^(.*?\/)admin\//);
            var safeAppRoot = pathPrefixMatch ? pathPrefixMatch[1] : '/';
            var endpointUrl = $zariadenieContainer.attr('data-autocomplete-url') || $zakaznikField.attr('data-autocomplete-url') || (safeAppRoot + 'evidencia/zariadenie_autocomplete/');
            var currentXhr = $.getJSON(endpointUrl, { customer: cleanCustomerId }, function(data) {
                if (activeXhr !== currentXhr || String($zakaznikField.val() || '').trim() !== cleanCustomerId) return;

                var currentlyCheckedMap = {};
                if (hasExistingCheckboxes) {
                    $zariadenieContainer.find('input[type="checkbox"]:checked').each(function() {
                        var val = String($(this).val()).trim();
                        if (val) currentlyCheckedMap[val] = true;
                    });
                } else {
                    $.each(safeSelected, function(_, sId) {
                        if (sId) currentlyCheckedMap[sId] = true;
                    });
                }

                $zariadenieContainer.empty();

                if (data && Array.isArray(data.results) && data.results.length > 0) {
                    var $btnWrapper = $('<li style="list-style: none; margin-bottom: 12px;"></li>');
                    var $invertBtn = $('<button type="button" style="background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: bold; cursor: pointer; transition: 0.2s;">🔄 Invertovať výber</button>');
                    
                    $invertBtn.hover(
                        function() { $(this).css({'background': '#e2e8f0', 'color': '#0f172a'}); },
                        function() { $(this).css({'background': '#f1f5f9', 'color': '#475569'}); }
                    );

                    $invertBtn.on('click', function(e) {
                        e.preventDefault();
                        $zariadenieContainer.find('input[type="checkbox"]:not(:disabled)').each(function() {
                            $(this).prop('checked', !this.checked).trigger('change');
                        });
                    });

                    $btnWrapper.append($invertBtn);
                    $zariadenieContainer.append($btnWrapper);

                    var seenDeviceIds = {};
                    $.each(data.results, function(i, item) {
                        var rawId = (item && item.id !== undefined && item.id !== null) ? item.id.toString() : '';
                        if (!rawId || seenDeviceIds[rawId]) return;
                        seenDeviceIds[rawId] = true;
                        var itemText = (item && item.text !== undefined && item.text !== null) ? item.text.toString() : '';
                        var isChecked = Boolean(currentlyCheckedMap[rawId]);

                        var $li = $('<li style="margin-bottom: 8px; padding: 8px 12px; background: white; border: 1px solid #e2e8f0; border-radius: 8px; transition: 0.2s;"></li>');
                        var $label = $('<label style="display: flex; align-items: center; gap: 14px; cursor: pointer; width: 100%; margin: 0; font-size: 13px; color: #1e293b; font-weight: 600;"></label>');
                        var $checkbox = $('<input type="checkbox" style="margin: 0; accent-color: #3b82f6; width: 18px; height: 18px; flex-shrink: 0; cursor: pointer;">')
                            .attr('name', inputFieldName)
                            .val(rawId)
                            .prop('checked', isChecked);
                        var $span = $('<span style="line-height: 1.4; flex: 1;"></span>').text(itemText);

                        $label.append($checkbox).append($span);
                        $li.append($label);
                        $zariadenieContainer.append($li);
                    });
                } else {
                    $zariadenieContainer.html('<li><em style="color: #f59e0b; font-size: 13px;">⚠️ Tento zákazník nemá v evidencii priradené žiadne zariadenia.</em></li>');
                }
            }).fail(function(jqXHR, textStatus) {

                if (textStatus === 'abort') return;
                if (activeXhr !== currentXhr || String($zakaznikField.val() || '').trim() !== cleanCustomerId) return;

                if (hasExistingCheckboxes && !isCustomerChange) {
                    if (!$zariadenieContainer.find('.isas-ajax-fallback-warning').length) {
                        $zariadenieContainer.prepend('<li class="isas-ajax-fallback-warning" style="margin-bottom: 8px;"><em style="color: #ef4444; font-size: 12px; font-weight: bold;">⚠️ Nepodarilo sa aktualizovať zoznam zo servera. Zobrazený je pôvodný výber.</em></li>');
                    }
                    return;
                }

                if (safeSelected.length > 0) {
                    $zariadenieContainer.empty();
                    var $errLi = $('<li style="margin-bottom: 8px;"><em style="color: #ef4444; font-size: 13px; font-weight: bold;">⚠️ Chyba siete: Nepodarilo sa načítať zariadenia zo servera. Pôvodný výber bol zachovaný:</em></li>');
                    $zariadenieContainer.append($errLi);
                    $.each(safeSelected, function(_, devId) {
                        if (!devId) return;
                        var fallbackText = safeLabels[devId] || ('Zariadenie #' + devId + ' (Pôvodný výber)');
                        var $fallbackLi = $('<li style="margin-bottom: 6px; padding: 6px 10px; background: #fff1f2; border: 1px solid #fecdd3; border-radius: 6px;"></li>');
                        var $fbLabel = $('<label style="display: flex; align-items: center; gap: 10px; margin: 0; font-size: 12px; color: #9f1239; font-weight: 600; cursor: pointer;"></label>');
                        var $fbCheckbox = $('<input type="checkbox" checked style="margin: 0; accent-color: #ef4444; width: 16px; height: 16px; flex-shrink: 0; cursor: pointer;">')
                            .attr('name', inputFieldName)
                            .val(devId);
                        var $fbSpan = $('<span style="line-height: 1.4; flex: 1;"></span>').text(fallbackText);
                        $fbLabel.append($fbCheckbox).append($fbSpan);
                        $fallbackLi.append($fbLabel);
                        $zariadenieContainer.append($fallbackLi);
                    });
                } else {
                    $zariadenieContainer.html('<li><em style="color: #ef4444; font-size: 13px;">⚠️ Chyba siete: Nepodarilo sa načítať zariadenia z databázy.</em></li>');
                }

            }).always(function() {
                if (activeXhr === currentXhr) {
                    $zariadenieContainer.css('opacity', '1');
                    activeXhr = null;
                }
            });
            activeXhr = currentXhr;
        }

        var initialCustomerVal = $zakaznikField.val();
        var isInitialized = false;

        $zakaznikField.off('change.filterZariadenia select2:select select2:unselect select2:clear').on('change.filterZariadenia select2:select select2:unselect select2:clear', function(e) {
            var currentVal = (e && (e.type === 'select2:clear' || e.type === 'select2:unselect')) ? '' : $(this).val();
            // Zabránenie falošnému triggeru zo Select2 pri štarte, ak sa hodnota nezmenila
            if (!isInitialized && currentVal === initialCustomerVal) {
                return;
            }
            isInitialized = true;
            if (initTimer) {
                clearInterval(initTimer);
                clearTimeout(initTimer);
                initTimer = null;
            }

            loadDevices(currentVal, [], true);
        });

        function executeInitialLoad() {
            if (isInitialized) return;
            isInitialized = true;
            if (initTimer) {
                clearInterval(initTimer);
                clearTimeout(initTimer);
                initTimer = null;
            }
            var currentVal = String($zakaznikField.val() || '').trim();

            if (currentVal) {
                // Načítame nielen ID, ale aj textové popisy zariadení pre prípad výpadku siete
                var initialSelections = $zariadenieContainer.find('input[type="checkbox"]:checked').map(function() {
                    var val = $(this).val();
                    var text = $(this).closest('label').text().trim() || $(this).parent().text().trim();
                    return { id: val ? val.toString() : '', text: text };
                }).get();

                loadDevices(currentVal, initialSelections, false);
            } else {
                loadDevices('', [], false);
            }
        }

        // Deterministický watchdog: spustí sa okamžite po nábehu Select2 (napr. o 80 ms), najneskôr však po 1 sekunde
        var attempts = 0;
        initTimer = setInterval(function() {
            attempts++;
            var isSelect2Ready = $zakaznikField.hasClass('select2-hidden-accessible') || $zakaznikField.next('.select2-container').length > 0;
            if (isSelect2Ready || attempts >= 25) {
                executeInitialLoad();
            }
        }, 40);
    }

    $(document).ready(function() {
        initFilterZariadenia();
    });

})(typeof django !== 'undefined' && django.jQuery ? django.jQuery : (typeof jQuery !== 'undefined' ? jQuery : window.$));