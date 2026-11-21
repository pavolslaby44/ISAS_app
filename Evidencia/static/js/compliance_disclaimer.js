function initComplianceDisclaimer() {
    // 🛡️ ISAS ARCHITECT: 100% nepriestrelný selektor naviazaný na legislatívny fieldset v GlobalneNastavenia
    const anchorField = document.querySelector('.field-servisy_sledovat_legislativu')
                     || document.querySelector('#kompresorynastavenia_form fieldset:last-of-type');
    if (!anchorField) return;

    const targetFieldset = anchorField.tagName === 'FIELDSET' ? anchorField : anchorField.closest('fieldset');
    if (!targetFieldset) return;
    if (targetFieldset.querySelector('.isas-compliance-disclaimer-card')) return;

    const disclaimerCard = document.createElement('div');
    disclaimerCard.className = 'isas-compliance-disclaimer-card';
    disclaimerCard.innerHTML = `
        <div class="isas-compliance-title">
            <span class="isas-compliance-icon">⚖️</span>
            <span>DÔLEŽITÉ PRÁVNE UPOZORNENIE PRE PREVÁDZKOVATEĽA</span>
        </div>
       <p class="isas-compliance-body">
            Systém ISAS CORE je procesným a evidenčným softvérovým nástrojom poskytovaným 
            v stave, v akom stojí a leží („as is“). Automatizované výpočty, alerty a notifikácie termínov 
            inšpekcií technických zariadení, kontrol únikov chladív (F-plynov), overovania meradiel a prevádzkových výmen 
            predstavujú nezáväznú informatívnu podporu závislú výhradne od údajov zadaných používateľom. 
            Systém nenahrádza odbornú spôsobilosť autorizovaného revízneho technika, inšpekčnej organizácie, 
            akreditovaného metrológa ani certifikovanej osoby. Poskytovateľ softvéru v maximálnom rozsahu 
            povolenom platným právnym poriadkom nezodpovedá za správnosť dát, zlyhanie prenosu notifikácií 
            tretími stranami (SMTP, telekomunikačné siete, API), legislatívne zmeny, nedodržanie lehôt, 
            ušlý zisk, prevádzkové prestoje, ekologické škody ani pokuty uložené dozornými orgánmi. 
            Prevádzkovateľ zariadenia nesie plnú a nedeliteľnú právnu zodpovednosť za bezpečný technický stav 
            a platnosť predpísaných kontrol podľa platných predpisov štátu prevádzky a Nariadenia (EÚ) 2024/573.
        </p>
    `;

    // Vloženie priamo na koniec vnútra Fieldsetu 13
    targetFieldset.appendChild(disclaimerCard);
}

// 🛡️ ISAS ARCHITECT: Bezpečné spustenie či už DOM ešte len vzniká alebo je už načítaný
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initComplianceDisclaimer);
} else {
    initComplianceDisclaimer();
}