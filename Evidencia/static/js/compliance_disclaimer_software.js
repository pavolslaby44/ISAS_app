function initComplianceDisclaimer() {
    // 🛡️ ISAS ARCHITECT: Hľadá buď pole v GlobalneNastavenia, alebo posledný fieldset v SoftwareNastavenia
    const anchorField = document.querySelector('.field-servisy_sledovat_legislativu') 
                     || document.querySelector('#softwarenastavenia_form fieldset:last-of-type');
    if (!anchorField) return;

    const targetFieldset = anchorField.tagName === 'FIELDSET' ? anchorField : anchorField.closest('fieldset');
    if (!targetFieldset) return;
    if (targetFieldset.querySelector('.isas-compliance-disclaimer-card')) return;

    const disclaimerCard = document.createElement('div');
    disclaimerCard.className = 'isas-compliance-disclaimer-card';
    disclaimerCard.innerHTML = `
        <div class="isas-compliance-title">
            <span class="isas-compliance-icon">⚖️</span>
            <span>DÔLEŽITÉ UPOZORNENIE PRE PREVÁDZKOVATEĽA IT INFRAŠTRUKTÚRY</span>
        </div>
        <p class="isas-compliance-body">
            Systém ISAS CORE je procesným a monitorovacím softvérovým nástrojom poskytovaným 
            v stave, v akom stojí a leží („as is“). Automatizované výpočty, alerty a notifikácie termínov 
            bezpečnostných auditov NIS2, penetračných testov, expirácie SSL/TLS certifikátov, registrácií 
            domén a periodicity Disaster Recovery testov záloh predstavujú nezáväznú informatívnu podporu 
            závislú výhradne od údajov zadaných používateľom. Systém nenahrádza odbornú spôsobilosť 
            certifikovaného audítora informačnej bezpečnosti (ISO 27001 Lead Auditor), manažéra kybernetickej 
            bezpečnosti (CISO) ani zmluvné garancie SLA poskytovateľov infraštruktúry. Poskytovateľ softvéru 
            v maximálnom rozsahu povolenom platným právnym poriadkom nezodpovedá za správnosť dát, zlyhanie 
            prenosu notifikácií tretími stranami (SMTP, API, siete), legislatívne zmeny, nedodržanie lehôt, 
            ušlý zisk, prevádzkové prestoje, kybernetické incidenty ani pokuty uložené dozornými orgánmi 
            podľa vnútroštátnych predpisov v oblasti kybernetickej bezpečnosti a Smernice NIS2. Prevádzkovateľ nesie plnú a nedeliteľnú právnu zodpovednosť za kontinuitu
            prevádzky, bezpečnosť dát a platnosť predpísaných auditov podľa platných predpisov.
        </p>
    `;

    // Vloženie priamo na koniec vnútra fieldsetu
    targetFieldset.appendChild(disclaimerCard);
}

// 🛡️ ISAS ARCHITECT: Bezpečné spustenie či už DOM ešte len vzniká alebo je už načítaný
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initComplianceDisclaimer);
} else {
    initComplianceDisclaimer();
}