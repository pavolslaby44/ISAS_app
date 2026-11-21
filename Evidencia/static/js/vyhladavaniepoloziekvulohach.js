(function($) {
    'use strict';

    // 🔥 ISAS CSS MÁGIA PRE HYBRIDNÚ TABUĽKU
    var style = document.createElement('style');
    style.innerHTML = `
        /* 📐 ISAS SMART LAYOUT: Nové percentuálne rozdelenie šírky stĺpcov pre dlhé texty */
        th.column-hlavna_kategoria, td.field-hlavna_kategoria { width: 24% !important; }
        th.column-content_type, td.field-content_type { width: 24% !important; }
        th.column-object_id, td.field-object_id { width: 30% !important; } 
        th.column-pocet, td.field-pocet { width: 22% !important; min-width: 150px !important; white-space: nowrap !important; }
        
        /* ✨ ZAoblené A ZLADENÉ POLE PRE POČET (S MIESTOM PRE JEDNOTKU) */
        td.field-pocet input { 
            width: 90px !important; height: 42px !important; display: inline-block !important; 
            text-align: center !important; font-weight: 800 !important; color: #0f172a !important; 
            padding-right: 28px !important; padding-left: 10px !important;
            transition: all 0.3s ease !important; border-radius: 12px !important; 
            border: 2px solid #e2e8f0 !important; box-shadow: inset 0 2px 4px rgba(0,0,0,0.02) !important;
            box-sizing: border-box !important;
        }
        
        /* 🎯 WRAPPER A ABSOLÚTNA POZÍCIA PRE MERNÚ JEDNOTKU */
        .isas-pocet-wrapper { position: relative !important; display: inline-block !important; }
        .isas-unit-span {
            position: absolute !important; right: 12px !important; top: 50% !important; 
            transform: translateY(-50%) !important; font-weight: 800 !important; 
            font-size: 13px !important; color: #6366f1 !important; 
            pointer-events: none !important;
        }
        
        /* 🔒 VIZUÁL UZAMKNUTÉHO POĽA PRE POČET */
        td.field-pocet input:disabled {
            background-color: #f8fafc !important; color: #94a3b8 !important; cursor: not-allowed !important; 
            border: 2px solid #e2e8f0 !important; opacity: 0.8 !important;
        }
        
        /* ✨ ISAS NATIVE PREMIUM SELECT STYLING (Stĺpec 1 a 2) ✨ */
        select.isas-native-select, select.sklad-select2 {
            width: 100% !important; height: 42px !important; 
            padding: 6px 30px 6px 12px !important;
            border-radius: 12px !important; border: 2px solid #e2e8f0 !important;
            background-color: #ffffff !important; font-size: 14px !important; font-weight: 700 !important;
            color: #1e293b !important; box-shadow: inset 0 2px 4px rgba(0,0,0,0.02) !important;
            transition: all 0.3s ease !important; cursor: pointer; appearance: auto;
            white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important;
        }
        select.isas-native-select:focus { border-color: #6366f1 !important; box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1) !important; outline: none !important; }
        select.isas-native-select:disabled { background-color: #f8fafc !important; color: #94a3b8 !important; cursor: not-allowed !important; }

        /* ✨ ISAS SELECT2 STYLING (IZOLOVANÉ IBA PRE 3. STĹPEC) ✨ */
        td.field-object_id .select2-container { width: 100% !important; }
        td.field-object_id .select2-selection--single {
            height: 42px !important; border-radius: 12px !important; border: 2px solid #e2e8f0 !important;
            background: #ffffff !important; display: flex; align-items: center;
            font-size: 14px; font-weight: 700; color: #1e293b; box-shadow: inset 0 2px 4px rgba(0,0,0,0.02) !important;
        }
        td.field-object_id .select2-container--focus .select2-selection--single,
        td.field-object_id .select2-container--open .select2-selection--single { border-color: #6366f1 !important; box-shadow: 0 0 0 4px rgba(99, 102, 241, 0.1) !important; }
        td.field-object_id .select2-container--disabled .select2-selection--single { background-color: #f8fafc !important; cursor: not-allowed; opacity: 0.8; }
        td.field-object_id .select2-selection__arrow { height: 40px !important; right: 10px !important; }
        td.field-object_id .select2-selection__rendered { padding-left: 15px !important; padding-right: 35px !important; color: #1e293b !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; }
        .select2-dropdown { border: 1px solid #6366f1 !important; border-radius: 12px !important; z-index: 9999999 !important; box-shadow: 0 25px 50px rgba(0,0,0,0.15); }
        .select2-search__field { border-radius: 8px !important; font-weight: 600 !important; }
    `;
    document.head.appendChild(style);

    // 1. GLOBÁLNA MAPA KATEGÓRIÍ
    var isasCategoryMap = {};
    var mapaNacitana = false;

    function vybudujMapuKategorii() {
        if (mapaNacitana) return;
        var najdenyPocet = 0;
        $('select.subkategoria-select').each(function() {
            $(this).find('option').each(function() {
                var val = $(this).val();
                var text = $(this).text();
                if (val && text.includes('::')) {
                    var casti = text.split('::');
                    var hlavnaKat = casti[0].trim();
                    var podKat = casti[1].trim();
                    if (!isasCategoryMap[hlavnaKat]) isasCategoryMap[hlavnaKat] = [];
                    if (!isasCategoryMap[hlavnaKat].some(item => item.id === val)) {
                        isasCategoryMap[hlavnaKat].push({ id: val, text: podKat });
                        najdenyPocet++;
                    }
                }
            });
        });
        if (najdenyPocet > 0) mapaNacitana = true;
    }

    // 🚀 DVA RÔZNE PLACEHOLDRE: Pre natívne polia vs. pre Select2
    function getNativePlaceholder(text) { return '<option value="" disabled selected hidden>' + text + '</option>'; }
    function getSelect2Placeholder(text) { return '<option value="" selected>' + text + '</option>'; }

    // 💡 AKTUALIZÁCIA TOOLTIPOV: Automaticky pridá 'title' pre prípad veľmi dlhých názvov
    function updatniTooltpy() {
        $('select.isas-native-select').each(function() {
            $(this).attr('title', $(this).find('option:selected').text());
        });
    }

    // 🔥 ISAS SMART UNITS
    function updateUnitDisplay($row) {
        var $pocetInput = $row.find('td.field-pocet input');
        if (!$pocetInput.length) return; 

        var subcatText = $row.find('.subkategoria-select option:selected').text().toLowerCase();
        
        // 📦 Vytvorenie wrappera, ak ešte neexistuje
        var $wrapper = $pocetInput.closest('.isas-pocet-wrapper');
        if ($wrapper.length === 0) {
            $pocetInput.wrap('<div class="isas-pocet-wrapper"></div>');
            $wrapper = $pocetInput.closest('.isas-pocet-wrapper');
            $wrapper.append('<span class="isas-unit-span"></span>');
        }
        var $unitSpan = $wrapper.find('.isas-unit-span');

        if (subcatText.includes('vyberte') || subcatText === '' || subcatText.includes('najskôr')) {
            $unitSpan.html(''); return;
        }

        var $dielSearch = $row.find('.sklad-select2');
        var unit = '';
        if ($dielSearch.val()) {
            var $opt = $dielSearch.find('option:selected');
            unit = $opt.attr('data-unit') || $dielSearch.attr('data-pre-unit') || 'ks';
        }
        var safeUnit = unit ? unit.toString().replace(/&/g, "&amp;").replace(/</g, "&lt;") : '';
        $unitSpan.html(safeUnit); // 🎨 Farba a štýl sú už definované v CSS
    }

    // 2. ISOLOVANÉ EVENT DELEGATION
    $(document).on('change', '.kategoria-select', function() {
        var $row = $(this).closest('.form-row');
        var $selectedOption = $(this).find('option:selected');
        $(this).attr('title', $selectedOption.text());

        if ($row.hasClass('empty-form')) return;

        var $subSelect = $row.find('.subkategoria-select');
        var $dielSearch = $row.find('.sklad-select2'); 
        var $pocetInput = $row.find('td.field-pocet input');

        // 🛡️ Zrušenie prebiehajúceho AJAXu na danom riadku pri zmene nadradenej kategórie
        var prevXhr = $dielSearch.data('activeXhr');
        if (prevXhr && typeof prevXhr.abort === 'function') {
            prevXhr.abort();
            $dielSearch.removeData('activeXhr');
        }
    
        // 🔥 FIX: Získame hodnotu aj text pre maximálnu istotu na iOS
        var vybranaKatVal = $(this).val() || $selectedOption.val() || '';
        var vybranaKatText = $selectedOption.text() || '';
    
        // 🔥 FIX: Hľadáme zhodu v mape podľa hodnoty aj podľa textu
        var mapKey = null;
        if (isasCategoryMap[vybranaKatVal.trim()]) {
            mapKey = vybranaKatVal.trim();
        } else if (isasCategoryMap[vybranaKatText.trim()]) {
            mapKey = vybranaKatText.trim();
        }

        $dielSearch.empty().append(getSelect2Placeholder('🔒 Najskôr vyberte podkategóriu...')).prop('disabled', true).trigger('change.select2');
        $pocetInput.prop('disabled', true); 

        if (mapKey) {
            $subSelect.empty().append(getNativePlaceholder('📑 Vyberte podkategóriu...'));
            $.each(isasCategoryMap[mapKey], function(i, item) {
                $subSelect.append(new Option(item.text, item.id));
            });
            // 🔥 FIX: removeAttr je kľúčový pre odomknutie selectu na iPhone
            $subSelect.prop('disabled', false).removeAttr('disabled');
        } else {
            $subSelect.empty().append(getNativePlaceholder('🔒 Najskôr vyberte kategóriu...')).prop('disabled', true).attr('disabled', 'disabled');
        }
    
        $subSelect.attr('title', $subSelect.find('option:selected').text());
        updateUnitDisplay($row); 
    });

    $(document).on('change', '.subkategoria-select', function() {
        var $row = $(this).closest('.form-row');
        var $subSelect = $(this);
        $subSelect.attr('title', $subSelect.find('option:selected').text()); // 💡 Tooltip update

        if ($row.hasClass('empty-form')) return;

        var ctId = $subSelect.val();
        var $dielSearch = $row.find('.sklad-select2');
        var $pocetInput = $row.find('td.field-pocet input'); 

        // 🛡️ 1. ABORT PREEDCHÁDZAJÚCEHO REQUESTU: Okamžité prerušenie neukončeného volania na riadku
        var prevXhr = $dielSearch.data('activeXhr');
        if (prevXhr && typeof prevXhr.abort === 'function') {
            prevXhr.abort();
            $dielSearch.removeData('activeXhr');
        }

        // 🛡️ Ošetrenie prázdnej voľby (návrat do bezpečného zamknutého stavu bez zaseknutia načítavania)
        if (!ctId) {
            $dielSearch.empty().append(getSelect2Placeholder('🔒 Najskôr vyberte podkategóriu...')).prop('disabled', true).trigger('change.select2');
            $pocetInput.prop('disabled', true);
            updateUnitDisplay($row);
            return;
        }

        $dielSearch.empty().append(getSelect2Placeholder('⏳ Sťahujem dáta zo skladu...')).prop('disabled', true).trigger('change.select2');
        $pocetInput.prop('disabled', true); 
        updateUnitDisplay($row); 

        // 🛡️ 2. ASYNCHRÓNNA INTEGRITA & FAIL HANDLER
        var currentXhr = $.getJSON('/evidencia/ajax/get-sklad-items/', { ct_id: ctId }, function(data) {
            // Kontrola súbehu: ignorujeme oneskorenú odpoveď, ak medzitým používateľ zmenil podkategóriu
            if ($dielSearch.data('activeXhr') !== currentXhr || $subSelect.val() !== ctId) return;

            $dielSearch.empty().append(new Option('', '', true, true)); 

            if (data && Array.isArray(data.results) && data.results.length > 0) {
                $.each(data.results, function(i, item) {
                    var unit = 'ks'; 
                    var itemText = (item && item.text) ? item.text.toString() : '';
                    var itemId = (item && item.id !== undefined && item.id !== null) ? item.id : '';
                    if (!itemId) return;

                    var match = itemText.match(/.*\((?:[\d\.,\s]+)?([a-zA-Z]+2?)\)\s*$/);
                    if (match) unit = match[1].trim();
                    
                    var opt = new Option(itemText.replace(/\s*\([\d\.,\s]+[a-zA-Z]+2?\)\s*$/, '').trim(), itemId, false, false);
                    $(opt).attr('data-unit', unit);
                    $dielSearch.append(opt);
                });
                
                $dielSearch.prop('disabled', false).select2({
                    placeholder: "🔍 Začnite písať názov...",
                    allowClear: true, width: '100%'
                });
                $pocetInput.prop('disabled', false);

            } else {
                $dielSearch.empty().append(getSelect2Placeholder('❌ Sklad je prázdny')).prop('disabled', true).trigger('change.select2');
                $pocetInput.prop('disabled', true); 
            }
        }).fail(function(jqXHR, textStatus) {
            if (textStatus === 'abort') return;
            // Overenie, že spracúvame chybový stav pre stále aktuálny riadok a hodnotu
            if ($dielSearch.data('activeXhr') !== currentXhr || $subSelect.val() !== ctId) return;

            $dielSearch.empty().append(getSelect2Placeholder('❌ Chyba pri načítaní skladu')).prop('disabled', true).trigger('change.select2');
            $pocetInput.prop('disabled', true);
        }).always(function() {
            if ($dielSearch.data('activeXhr') === currentXhr) {
                $dielSearch.removeData('activeXhr');
            }
        });

        $dielSearch.data('activeXhr', currentXhr);
    });

    $(document).on('select2:select select2:clear', '.sklad-select2', function() {
        updateUnitDisplay($(this).closest('.form-row'));
    });

    $(document).on('click', '.btn-vymazat-diel', function(e) {
        e.preventDefault();
        var $row = $(this).closest('.form-row'); 
        var $checkbox = $row.find('input[type="checkbox"][name$="-DELETE"]');
        if ($checkbox.length) { $checkbox.prop('checked', true); $row.fadeOut(300); } 
        else { $row.remove(); }
    });

    function renderCustomUI() {
        $('.form-row').not('.empty-form').each(function() {
            var $row = $(this);
            var deleteTd = $row.find('td.delete');
            if (deleteTd.find('input[type="checkbox"]').length && deleteTd.find('.btn-vymazat-diel').length === 0) {
                deleteTd.find('input[type="checkbox"], label').hide();
                deleteTd.append('<button type="button" class="btn-vymazat-diel" style="background-color: #ef4444; color: white; border: none; padding: 6px 14px; border-radius: 6px; font-weight: bold; cursor: pointer; white-space: nowrap; box-shadow: 0 2px 4px rgba(239, 68, 68, 0.3);">🗑️ Odstrániť</button>');
            }
        });
    }

    function initStareZaznamy() {
        $('.form-row').not('.empty-form').each(function() {
            var $row = $(this);
            var $subSelect = $row.find('.subkategoria-select');
            var $dielSearch = $row.find('.sklad-select2');
            var $pocetInput = $row.find('td.field-pocet input'); 
            
            $subSelect.find('option').each(function() {
                var txt = $(this).text();
                if (txt.includes('::')) $(this).text(txt.split('::')[1].trim());
            });

            if ($subSelect.val()) {
                if ($dielSearch.attr('data-pre-val')) {
                    // 🟢 SCENÁR 1: Všetko je vyplnené (Štandardný uložený záznam)
                    var opt = new Option($dielSearch.attr('data-pre-label'), $dielSearch.attr('data-pre-val'), true, true);
                    $(opt).attr('data-unit', $dielSearch.attr('data-pre-unit') || 'ks'); 
                    $dielSearch.empty().append(opt).prop('disabled', false).removeAttr('disabled');
                    $pocetInput.prop('disabled', false).removeAttr('disabled'); 
                    $dielSearch.select2({ placeholder: "🔍 Začnite písať...", width: '100%' });
                } else {
                    // 🟠 SCENÁR 2: Podkategória je vybraná, ale diel chýba (Stav po chybe validácie!)
                    // NESMIEME TO ZAMKNÚŤ! Musíme spustiť AJAX, aby sa načítali diely.
                    $dielSearch.prop('disabled', false).removeAttr('disabled');
                    $pocetInput.prop('disabled', false).removeAttr('disabled');
                    $subSelect.trigger('change'); // 🔥 Toto načíta diely zo servera a nechá pole odomknuté
                }
            } else {
                // 🔴 SCENÁR 3: Úplne prázdny riadok. Zamkneme.
                if ($dielSearch.hasClass("select2-hidden-accessible")) {
                    $dielSearch.select2('destroy');
                }
                $dielSearch.empty().append(getSelect2Placeholder('🔒 Najskôr vyberte podkategóriu...')).prop('disabled', true).attr('disabled', 'disabled');
                $pocetInput.prop('disabled', true).attr('disabled', 'disabled'); 
                
                $dielSearch.select2({ width: '100%' });
            }
            updateUnitDisplay($row);
        });
    }

    $(document).ready(function() {
        vybudujMapuKategorii();
        renderCustomUI();
        initStareZaznamy();
        updatniTooltpy(); // 💡 Nastaví pop-up bubliny hneď pri štarte

        $(document).on('formset:added', function(event, $row) {
            vybudujMapuKategorii(); renderCustomUI();

            $row.find('.select2-container').remove();
            var $dielSearch = $row.find('.sklad-select2');
            var $pocetInput = $row.find('td.field-pocet input'); 

            $dielSearch.removeClass('select2-hidden-accessible').removeAttr('data-select2-id aria-hidden tabindex').show();

            $row.find('.kategoria-select').val('').attr('title', '📂 Vyberte kategóriu...'); // 💡 Reset tooltipu
            $row.find('.subkategoria-select').empty().append(getNativePlaceholder('🔒 Najskôr vyberte kategóriu...')).prop('disabled', true).attr('title', '🔒 Najskôr vyberte kategóriu...');

            $dielSearch.empty().append(getSelect2Placeholder('🔒 Najskôr vyberte podkategóriu...')).prop('disabled', true);
            $dielSearch.select2({ width: '100%' }); 
            $pocetInput.prop('disabled', true); 

            updateUnitDisplay($row);
        });
    });        

})(django.jQuery);