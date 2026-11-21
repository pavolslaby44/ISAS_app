(function($) {
    if (!$) return;
    $(document).ready(function() {
        
        // =========================================================================
        // ISAS CORE: ZÁVISLÝ DROPDOWN (Zákazník -> Zariadenie) - NETWORK INTERCEPTOR
        // =========================================================================
        
        // Odchytíme KAŽDÝ AJAX request z jQuery predtým, než odíde na server
        $.ajaxPrefilter(function(options, originalOptions, jqXHR) {
            
            // 1. Defenzívna kontrola autocomplete endpointu (podpora aj pre lokalizované/vlastné cesty adminu)
            if (options.url && (options.url.indexOf('autocomplete/') !== -1 || options.url.indexOf('/admin/autocomplete/') !== -1)) {
                
                var zakaznikId = ($('#id_zakaznik').val() || '').toString().trim();
                if (!zakaznikId) return;

                // 2. Ošetrenie stavu: options.data ako objekt (Select2 adapter pred serializáciou)
                if (typeof options.data === 'object' && options.data !== null) {
                    if (options.data.field_name === 'zariadenie') {
                        options.data.zakaznik_id = zakaznikId;
                    }
                } 
                // 3. Ošetrenie stavu: options.data ako URL-encoded string s ochranou pred substring zhodou a duplicitou
                else if (typeof options.data === 'string') {
                    if (/(?:^|[?&])field_name=zariadenie(?:&|$)/.test(options.data)) {
                        if (!/(?:^|[?&])zakaznik_id=/.test(options.data)) {
                            var prefix = options.data.length ? '&' : '';
                            options.data += prefix + 'zakaznik_id=' + encodeURIComponent(zakaznikId);
                        }
                    }
                }
            }
        });

        // =========================================================================
        // SMART RESET: Delegovaný poslucháč + ochrana pred kaskádovým triggerovaním
        // =========================================================================
        $(document).on('change', '#id_zakaznik', function() {
            var $zariadenie = $('#id_zariadenie');
            // Zmenu odpaľujeme výhradne vtedy, ak v poli zariadenia reálne existovala hodnota
            if ($zariadenie.length && $zariadenie.val()) {
                $zariadenie.val(null).trigger('change');
            }
        });

    });
})(typeof django !== 'undefined' && django.jQuery ? django.jQuery : (typeof jQuery !== 'undefined' ? jQuery : null));