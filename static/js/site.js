document.addEventListener('DOMContentLoaded', () => {
    const buttons = document.querySelectorAll('.tab-button');
    const panels = document.querySelectorAll('.tab-panel');
    const usernameInput = document.getElementById('username');
    const usernameTitle = document.getElementById('username-title');

    if (usernameInput && usernameTitle) {
        const syncUsernameTitle = () => {
            const value = usernameInput.value.trim();
            usernameTitle.textContent = value ? `Mon pseudo : ${value}` : 'Mon pseudo';
        };

        const usernameForm = document.querySelector('form[action="/setusername"]');
        if (usernameForm) {
            usernameForm.addEventListener('submit', () => {
                const value = usernameInput.value.trim();
                if (value) {
                    syncUsernameTitle();
                }
            });
        }

        syncUsernameTitle();
    }

    buttons.forEach((button) => {
        button.addEventListener('click', () => {
            const target = button.dataset.tab;

            buttons.forEach((btn) => {
                const isActive = btn === button;
                btn.classList.toggle('active', isActive);
                btn.setAttribute('aria-selected', String(isActive));
                btn.tabIndex = isActive ? 0 : -1;
            });
            panels.forEach((panel) => {
                const isActive = panel.id === target;
                panel.classList.toggle('active', isActive);
                panel.hidden = !isActive;
            });
        });

        button.addEventListener('keydown', (event) => {
            const buttonList = Array.from(buttons);
            const currentIndex = buttonList.indexOf(button);
            let nextIndex = currentIndex;

            if (event.key === 'ArrowRight') {
                nextIndex = (currentIndex + 1) % buttonList.length;
            } else if (event.key === 'ArrowLeft') {
                nextIndex = (currentIndex - 1 + buttonList.length) % buttonList.length;
            } else if (event.key === 'Home') {
                nextIndex = 0;
            } else if (event.key === 'End') {
                nextIndex = buttonList.length - 1;
            } else {
                return;
            }

            event.preventDefault();
            buttonList[nextIndex].focus();
            buttonList[nextIndex].click();
        });
    });

    const pageContext = document.body.dataset.page || null;
    const pageState = document.getElementById('page-state');
    const gameCodeValue = document.body.dataset.gameCode || document.querySelector('input[name="game_code"]')?.value || null;

    if (gameCodeValue && pageState) {
        const updatePresence = () => fetch('/api/presence', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: `game_code=${encodeURIComponent(gameCodeValue)}`
        }).catch(() => {});

        updatePresence();
        setInterval(updatePresence, 5000);
        // Pas de beacon sur « pagehide » : cet événement part aussi à chaque rechargement
        // automatique de la page (polling du hash), ce qui supprimait la présence et le
        // curseur du joueur à chaque rafraîchissement. Si le joueur ferme l'onglet, sa
        // présence expire toute seule après le TTL côté serveur ; s'il quitte
        // explicitement, /leavegame nettoie sa présence.
    }

    // Curseurs des joueurs en direct, façon Figma/Canva : haute fréquence, pas d'animation de rattrapage.
    function initCursorTracking(containerSelector) {
        const cursorContainer = document.querySelector(containerSelector);
        if (!cursorContainer || !gameCodeValue || !pageState) {
            return;
        }

        const myUsername = pageState.dataset.username || null;
        const cursorLayer = document.createElement('div');
        cursorLayer.className = 'cursor-layer';
        cursorContainer.appendChild(cursorLayer);

        const renderedCursors = new Map();
        let lastSent = 0;
        let pendingPosition = null;
        let cursorRequestInFlight = false;
        let cursorSendTimer = null;

        async function sendPendingCursor() {
            if (cursorRequestInFlight || !pendingPosition) {
                return;
            }

            const delay = Math.max(0, 70 - (performance.now() - lastSent));
            if (delay > 0) {
                clearTimeout(cursorSendTimer);
                cursorSendTimer = setTimeout(sendPendingCursor, delay);
                return;
            }

            const position = pendingPosition;
            pendingPosition = null;
            lastSent = performance.now();
            cursorRequestInFlight = true;
            try {
                await fetch('/api/cursor', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: `game_code=${encodeURIComponent(gameCodeValue)}&x=${position.x.toFixed(2)}&y=${position.y.toFixed(2)}`
                });
            } catch (error) {
                // Une mise à jour suivante réessaiera sans bloquer le suivi.
            } finally {
                cursorRequestInFlight = false;
                if (pendingPosition) {
                    sendPendingCursor();
                }
            }
        }

        // Le curseur ne se met à jour que lorsqu'un joueur bouge réellement la souris, pas via un intervalle fixe.
        document.addEventListener('mousemove', (event) => {
            pendingPosition = {
                x: Math.max(0, Math.min(100, (event.clientX / window.innerWidth) * 100)),
                y: Math.max(0, Math.min(100, (event.clientY / window.innerHeight) * 100))
            };
            sendPendingCursor();
        });

        async function fetchCursors() {
            try {
                const response = await fetch('/api/cursors', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: `game_code=${encodeURIComponent(gameCodeValue)}`
                });
                const data = await response.json();
                return data.cursors || [];
            } catch (error) {
                return [];
            }
        }

        function syncCursors(cursors) {
            const seen = new Set();

            cursors.forEach((cursor) => {
                if (cursor.username === myUsername) {
                    return;
                }
                seen.add(cursor.username);

                let el = renderedCursors.get(cursor.username);
                if (!el) {
                    el = document.createElement('div');
                    el.className = 'remote-cursor';
                    el.innerHTML = '<svg class="remote-cursor-pointer" width="20" height="20" viewBox="0 0 20 20"><path d="M0 0 L0 16 L4.5 12.5 L7.5 19 L10 18 L7 11.5 L13 11.5 Z" fill="currentColor" stroke="white" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/></svg><span class="remote-cursor-label"></span>';
                    cursorLayer.appendChild(el);
                    renderedCursors.set(cursor.username, el);
                }

                const x = Math.min(Number(cursor.x) || 0, ((window.innerWidth - 20) / window.innerWidth) * 100);
                const y = Math.min(Number(cursor.y) || 0, ((window.innerHeight - 20) / window.innerHeight) * 100);
                el.style.left = `${x}%`;
                el.style.top = `${y}%`;
                el.classList.toggle('label-left', x > 75);
                el.classList.toggle('label-above', y > 80);
                el.classList.toggle('is-admin', !!cursor.is_admin);
                el.classList.toggle('is-current', !!cursor.is_current);
                el.querySelector('.remote-cursor-label').textContent = cursor.username;
            });

            renderedCursors.forEach((el, username) => {
                if (!seen.has(username)) {
                    el.remove();
                    renderedCursors.delete(username);
                }
            });
        }

        const refreshCursors = async () => syncCursors(await fetchCursors());
        refreshCursors();
        setInterval(refreshCursors, 1000);
    }

    if (pageContext === 'waiting') {
        initCursorTracking('.waiting-shell');

        if (!gameCodeValue) {
            return;
        }

        let currentHash = null;

        async function fetchGameHash() {
            try {
                const response = await fetch('/api/update/hash', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: `game_code=${encodeURIComponent(gameCodeValue)}`
                });
                const data = await response.json();
                return data.hash;
            } catch (error) {
                console.error('Erreur lors de la récupération du hash :', error);
                return null;
            }
        }

        async function checkForChanges() {
            const newHash = await fetchGameHash();
            if (newHash && newHash !== currentHash) {
                currentHash = newHash;
                window.location.reload();
            }
        }

        (async () => {
            currentHash = await fetchGameHash();
            setInterval(checkForChanges, 2000);
        })();
    }

    if (pageContext === 'finished') {
        initCursorTracking('.finished-shell');

        let currentHash = null;
        const fetchGameHash = async () => {
            try {
                const response = await fetch('/api/update/hash', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: `game_code=${encodeURIComponent(gameCodeValue)}`
                });
                const data = await response.json();
                return data.hash;
            } catch (error) {
                return null;
            }
        };
        const checkForChanges = async () => {
            const newHash = await fetchGameHash();
            if (newHash && currentHash && newHash !== currentHash) {
                window.location.reload();
            }
            currentHash = newHash || currentHash;
        };
        fetchGameHash().then((hash) => {
            currentHash = hash;
            setInterval(checkForChanges, 1000);
        });
    }

    if (pageContext === 'game' && pageState) {
        initCursorTracking('.cursor-container');

        if (!gameCodeValue) {
            return;
        }

        let currentHash = null;
        const revealTimer = pageState.dataset.lastEvent === 'true' ? 4000 : 0;

        async function fetchGameHash() {
            try {
                const response = await fetch(`/api/update/hash`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: `game_code=${encodeURIComponent(gameCodeValue)}`
                });
                const data = await response.json();
                return data.hash;
            } catch (error) {
                console.error('Erreur lors de la récupération du hash :', error);
                return null;
            }
        }

        async function checkForChanges() {
            const newHash = await fetchGameHash();
            if (newHash && newHash !== currentHash) {
                currentHash = newHash;
                window.location.reload();
            }
        }

        (async () => {
            currentHash = await fetchGameHash();
            const pollForChanges = async () => {
                await checkForChanges();
                setTimeout(pollForChanges, 1000);
            };
            setTimeout(pollForChanges, 1000);
            if (revealTimer) {
                setTimeout(() => window.location.reload(), revealTimer);
            }
        })();
    }
});