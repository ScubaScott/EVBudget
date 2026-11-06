/* Disney theme: a small pair of ears hidden in a corner of one card, the header, or the footer. It moves to a new
   spot every time EVBudget opens (never the same one twice in a row) and only shows on the Disney theme (CSS). */

(function () {
    const KEY = 'evbudget_hidden_mickey_spot';
    const CORNERS = ['tl', 'tr', 'bl', 'br'];
    // Places that are part of the page itself, so nothing redraws over the ears. Cards inside a tab or an Import
    // section count (the hunt can go there); cards hidden for another reason might never show, so they are skipped.
    const HOSTS = 'header.app-header, footer.app-footer, section.table-card, section.import-card, section.controls-card';
    const SEEN_THROUGH = '.tab-panel, #importSubImport, #importSubAccounts, #importSubRules, #importSubHistory';

    function canShow(el) {
        for (let n = el; n && n !== document.body; n = n.parentElement) {
            if (n.hidden && !n.matches(SEEN_THROUGH)) return false;
        }
        return true;
    }

    function place() {
        const hosts = Array.from(document.querySelectorAll(HOSTS)).filter(canShow);
        if (!hosts.length) return;
        let last = '';
        try { last = localStorage.getItem(KEY) || ''; } catch (e) { /* storage blocked */ }
        let spot, i = 0;
        do {
            spot = `${Math.floor(Math.random() * hosts.length)}-${CORNERS[Math.floor(Math.random() * CORNERS.length)]}`;
        } while (spot === last && hosts.length * CORNERS.length > 1 && ++i < 20);
        try { localStorage.setItem(KEY, spot); } catch (e) { /* storage blocked */ }

        const [index, corner] = spot.split('-');
        const host = hosts[parseInt(index, 10)];
        if (getComputedStyle(host).position === 'static') host.classList.add('hidden-mickey-host');
        const ears = document.createElement('span');
        ears.className = `hidden-mickey hidden-mickey-${corner}`;
        ears.setAttribute('aria-hidden', 'true');
        // Three circles: a head and two ears
        ears.innerHTML = '<svg viewBox="0 0 24 22" width="14" height="13"><circle cx="5" cy="6" r="4.6"/><circle cx="19" cy="6" r="4.6"/><circle cx="12" cy="14.5" r="7"/></svg>';
        ears.addEventListener('click', (e) => {
            e.stopPropagation();
            ears.classList.add('hidden-mickey-found');
            showToast('You found the hidden Mickey! It hides somewhere new every time EVBudget opens.');
        });
        host.appendChild(ears);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', place);
    else place();
})();
