/* Tells the server this page is open, so it can stop itself when the last page closes, and runs the Quit button.
   Used by both the dashboard and the budget picker. */

const LIFECYCLE_PAGE_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);
let lifecycleTimer = null;

function lifecyclePost(path) {
    return fetch(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page: LIFECYCLE_PAGE_ID }), keepalive: true
    });
}

function lifecycleBeat() {
    lifecyclePost('/api/heartbeat').catch(() => { /* the server is already stopped */ });
}

lifecycleBeat();
lifecycleTimer = setInterval(lifecycleBeat, 10000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) lifecycleBeat(); });
// Coming back to a page from the back/forward cache: it said goodbye when it left
window.addEventListener('pageshow', e => { if (e.persisted) lifecycleBeat(); });
// A reload or moving to another page also fires this, but the next page's first beat arrives well inside the server's grace period.
window.addEventListener('pagehide', () => {
    navigator.sendBeacon('/api/page-closed', new Blob([JSON.stringify({ page: LIFECYCLE_PAGE_ID })], { type: 'application/json' }));
});

function showStopped() {
    clearInterval(lifecycleTimer);
    document.body.innerHTML = '<div style="max-width:420px;margin:18vh auto;padding:0 20px;text-align:center;font-family:Inter,system-ui,sans-serif;color:var(--text-main);">'
        + '<div style="font-size:20px;font-weight:700;margin-bottom:8px;">EVBudget has stopped</div>'
        + '<div style="color:var(--text-muted);font-size:14px;line-height:1.5;">Your data is saved. You can close this tab. To use EVBudget again, open it from its shortcut.</div></div>';
    try { window.close(); } catch (e) { /* the browser only lets a page close tabs it opened itself */ }
}

async function quitApp() {
    if (!confirm('Stop EVBudget? Your data is already saved.')) return;
    try {
        await lifecyclePost('/api/shutdown');
    } catch (err) {
        // If the server went away before answering, it did stop.
    }
    showStopped();
}
