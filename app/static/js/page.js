/*
 * PWPage - page chrome shared by the Console (index.html) and the History
 * page (history.html): the header's Cards menu and kiosk mode. Styles live
 * in /static/css/page.css. Both pages carry the same header markup
 * (#card-menu-toggle, #kiosk-toggle, #kiosk-controls, #card-menu).
 *
 *   PWPage.cardsAndKiosk(opts) -> { apply }
 *       opts.hiddenKey     localStorage key for the hidden card ids (JSON list)
 *       opts.kioskKey      localStorage key for kiosk mode ('1' / '0')
 *       opts.urlHideParam  optional URL parameter listing card ids to hide
 *                          instead of the saved choice (not saved)
 *       opts.onLayout      optional fn(hiddenSet), run after cards are
 *                          shown or hidden (e.g. to re-balance rows)
 *       apply()            re-applies the hidden cards; call it after the
 *                          page adds or rebuilds cards
 *
 * The cards are every .card[data-card-id] on the page, in page order,
 * labelled by their .card-title. ?kiosk=1 (or 0) in the URL sets kiosk mode
 * without saving it. Esc closes the menu, then leaves kiosk (not while a
 * dialog is open).
 */
(function () {
    'use strict';

    function storageGet(key) {
        try { return localStorage.getItem(key); } catch (e) { return null; }
    }
    function storageSet(key, value) {
        try { localStorage.setItem(key, value); } catch (e) { /* ignore */ }
    }

    function cardsAndKiosk(opts) {
        const $ = id => document.getElementById(id);
        const menu = $('card-menu');
        const list = $('card-menu-list');
        const kioskBox = $('kiosk-checkbox');
        const controls = $('kiosk-controls');
        const menuToggle = $('card-menu-toggle');
        const kioskToggle = $('kiosk-toggle');
        const kioskCardsBtn = $('kiosk-cards-btn');
        if (!menu || !list || !kioskBox || !controls || !menuToggle || !kioskToggle || !kioskCardsBtn) {
            return { apply() {} };
        }
        const onLayout = opts.onLayout || (() => {});

        const params = new URLSearchParams(location.search);
        const urlHide = opts.urlHideParam && params.has(opts.urlHideParam)
            ? params.get(opts.urlHideParam).split(',').map(s => s.trim()).filter(Boolean)
            : null;
        const urlKiosk = params.has('kiosk') ? params.get('kiosk') !== '0' : null;

        let hidden;
        if (urlHide) {
            hidden = new Set(urlHide);
        } else {
            let saved = [];
            try { saved = JSON.parse(storageGet(opts.hiddenKey) || '[]'); } catch (e) { /* ignore */ }
            hidden = new Set(Array.isArray(saved)
                ? saved.filter(id => typeof id === 'string') : []);
        }
        let kiosk = urlKiosk !== null ? urlKiosk : storageGet(opts.kioskKey) === '1';

        function cards() {
            return Array.from(document.querySelectorAll('.card[data-card-id]'));
        }

        function saveHidden() {
            if (!urlHide) storageSet(opts.hiddenKey, JSON.stringify([...hidden]));
        }

        function apply() {
            cards().forEach(card => {
                card.classList.toggle('user-hidden', hidden.has(card.dataset.cardId));
            });
            onLayout(hidden);
        }

        function cardLabel(card) {
            const title = card.querySelector('.card-title');
            const text = title ? title.textContent.replace(/\s+/g, ' ').trim() : '';
            return text || card.dataset.cardId;
        }

        function buildMenu() {
            list.innerHTML = '';
            cards().forEach(card => {
                const id = card.dataset.cardId;
                const label = document.createElement('label');
                const box = document.createElement('input');
                box.type = 'checkbox';
                box.checked = !hidden.has(id);
                box.addEventListener('change', () => {
                    if (box.checked) hidden.delete(id); else hidden.add(id);
                    saveHidden();
                    apply();
                });
                label.appendChild(box);
                label.appendChild(document.createTextNode(cardLabel(card)));
                // A card the page itself isn't showing (e.g. a feature that
                // isn't active on this server)
                if (!hidden.has(id) && getComputedStyle(card).display === 'none') {
                    const note = document.createElement('span');
                    note.className = 'card-menu-note';
                    note.textContent = 'not active';
                    label.appendChild(note);
                }
                list.appendChild(label);
            });
            kioskBox.checked = kiosk;
        }

        function setKiosk(on) {
            kiosk = on;
            document.body.classList.toggle('kiosk', on);
            kioskBox.checked = on;
            if (urlKiosk === null) storageSet(opts.kioskKey, on ? '1' : '0');
            // Cards may change size; let charts/iframes re-measure
            window.dispatchEvent(new Event('resize'));
        }

        const openers = [menuToggle, kioskCardsBtn];
        let lastOpener = null;

        // restoreFocus: return focus to the opener on close. Skipped for
        // outside clicks, where focus should go wherever the user clicked.
        function toggleMenu(show, opener, restoreFocus = true) {
            const open = show !== undefined ? show : menu.hidden;
            if (open === !menu.hidden) return;
            if (open) {
                lastOpener = opener || null;
                buildMenu();
            }
            menu.hidden = !open;
            openers.forEach(el => el.setAttribute('aria-expanded', String(open)));
            if (open) {
                const first = menu.querySelector('input');
                if (first) first.focus();
            } else if (restoreFocus && lastOpener
                       && lastOpener.offsetParent !== null) {
                lastOpener.focus();
            }
        }

        // role="button" links: Space activates them like a real button
        function activateOnSpace(el) {
            el.addEventListener('keydown', e => {
                if (e.key === ' ') { e.preventDefault(); el.click(); }
            });
        }
        activateOnSpace(menuToggle);
        activateOnSpace(kioskToggle);

        menuToggle.addEventListener('click', e => {
            e.preventDefault(); toggleMenu(undefined, menuToggle);
        });
        kioskToggle.addEventListener('click', e => {
            e.preventDefault(); toggleMenu(false, null, false); setKiosk(true);
        });
        kioskCardsBtn.addEventListener('click', () => toggleMenu(undefined, kioskCardsBtn));
        $('kiosk-exit-btn').addEventListener('click', () => {
            toggleMenu(false, null, false); setKiosk(false);
        });
        kioskBox.addEventListener('change', () => setKiosk(kioskBox.checked));
        $('card-menu-reset').addEventListener('click', () => {
            hidden.clear(); saveHidden(); apply(); buildMenu();
        });

        // Close the menu on outside click; Esc closes it, then leaves kiosk
        document.addEventListener('click', e => {
            if (!menu.hidden && !menu.contains(e.target)
                && !e.target.closest('#card-menu-toggle, #kiosk-cards-btn')) {
                toggleMenu(false, null, false);
            }
        });
        document.addEventListener('keydown', e => {
            // Leave Esc to open dialogs (e.g. the islanding confirmation)
            if (e.defaultPrevented || document.querySelector('dialog[open]')) return;
            if (e.key !== 'Escape') return;
            if (!menu.hidden) toggleMenu(false);
            else if (kiosk) setKiosk(false);
        });

        // Touch screens have no hover: a tap reveals the kiosk buttons briefly
        let fadeTimer;
        document.addEventListener('touchstart', () => {
            if (!kiosk) return;
            controls.classList.add('active');
            clearTimeout(fadeTimer);
            fadeTimer = setTimeout(() => controls.classList.remove('active'), 4000);
        }, { passive: true });

        apply();
        setKiosk(kiosk);
        return { apply };
    }

    window.PWPage = { cardsAndKiosk };
})();
