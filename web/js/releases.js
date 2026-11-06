/* What's new: a popup once per release (and the full history from Help), plus the Submit feedback email.
   Release notes come from evbudget/releases.json. Each budget remembers the last release it was shown. */

let relData = null;   // {current, releases, unseen}

async function relLoad() {
    relData = await api('/api/releases');
    return relData;
}

const REL_TAGS = { new: 'New', improved: 'Improved', fixed: 'Fixed' };

function relNotesHtml(r) {
    return `<ul class="rel-notes">${r.notes.map(n => `<li>${n.type ? `<span class="rel-tag rel-tag-${n.type}">${REL_TAGS[n.type]}</span>` : ''}${escapeHtml(n.text)}</li>`).join('')}</ul>`;
}

function relHeadHtml(r) {
    return `<div class="rel-head"><span class="rel-version">Version ${escapeHtml(r.version)}</span>
                <span class="rel-date">${escapeHtml(formatDisplayDate(r.date))}</span></div>
            <div class="rel-name">${escapeHtml(r.title)}</div>`;
}

// One section per version. With collapseOlder, only the newest is open; the others open with a click.
function relEntriesHtml(list, collapseOlder) {
    if (!list.length) return '<div class="import-more">No release notes yet.</div>';
    return list.map((r, i) => (collapseOlder && i > 0)
        ? `<details class="rel-entry"><summary>${relHeadHtml(r)}</summary>${relNotesHtml(r)}</details>`
        : `<section class="rel-entry${i === 0 ? ' rel-latest' : ''}">${relHeadHtml(r)}${relNotesHtml(r)}</section>`).join('');
}

function relNameList(versions) {
    const v = versions.map(x => x);
    return v.length < 2 ? (v[0] || '') : v.slice(0, -1).join(', ') + ' and ' + v[v.length - 1];
}

function relShow(title, sub, html) {
    document.getElementById('relTitle').textContent = title;
    document.getElementById('relSub').textContent = sub;
    document.getElementById('relBody').innerHTML = html;
    document.getElementById('whatsNewOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('relDone').focus();
}

function closeWhatsNew() {
    document.getElementById('whatsNewOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

// Help > What's new: every version, newest first, one section each
async function openReleaseHistory() {
    try {
        const d = relData || await relLoad();
        relShow("What's new", `You're on version ${d.current}. Older versions are below; click one to open it.`, relEntriesHtml(d.releases, true));
    } catch (err) {
        showToast('Could not load release notes: ' + err.message, true);
    }
}

// On startup: show what's new since this budget last looked, and remember that it was shown
async function relCheckOnStart() {
    let d;
    try { d = await relLoad(); } catch (err) { return; }
    if (!d.unseen.length) return;
    const names = relNameList(d.unseen.map(r => r.version).reverse());
    if (d.from_version) relShow(`Updated to version ${d.current}`, `You were on ${d.from_version}. Here is what changed in ${names}.`, relEntriesHtml(d.unseen, false));
    else relShow("What's new", `Here is what's new in version${d.unseen.length > 1 ? 's' : ''} ${names}.`, relEntriesHtml(d.unseen, false));
    apiPost('/api/releases/seen', { version: d.unseen[0].version }).catch(() => { /* shown again next time */ });
}

// ---------------------------------------------------------------------
// Submit feedback: an email with the details needed to find a reported problem
// ---------------------------------------------------------------------
// "Chrome 156" rather than the long user-agent string, so the email link stays short enough to open
function relBrowserName() {
    const ua = navigator.userAgent;
    const m = /(Edg|Chrome|Firefox|Version)\/(\d+)/.exec(ua);
    const name = m ? ({ Edg: 'Edge', Version: 'Safari' }[m[1]] || m[1]) + ' ' + m[2] : 'unknown';
    return name;
}

async function relFeedbackText() {
    let about = {};
    try { about = await api('/api/about'); } catch (err) { /* send what the page knows */ }
    const g = about.git || {};
    const line = (label, value) => value ? `${label}: ${value}` : '';
    const lines = [
        line('EVBudget version', about.version || '(unknown)'),
        line('Release notes', about.latest_release ? `${about.latest_release} (${about.release_date})` : ''),
        g.commit ? `Git: ${g.commit} on ${g.branch || '?'}, ${g.commit_date || '?'}${g.local_changes ? ', with local changes' : ''}` : 'Git: not a git copy',
        line('Database version', about.schema_version),
        line('Python', about.python),
        line('System', about.os),
        line('Browser', relBrowserName()),
        line('Window', `${window.innerWidth}x${window.innerHeight}`),
        line('Theme', currentTheme),
        line('Screen', typeof activeTab === 'string' ? activeTab : ''),
    ].filter(Boolean);
    return lines.join('\n');
}

// The server hands the email to the operating system, so it opens in the mail app or browser as its own window.
// Opening it from this page would replace the EVBudget window, which has no back button.
async function submitFeedback() {
    const details = await relFeedbackText();
    const body = 'What happened, or what would you like to see?\n\n\n\n'
        + 'What did you expect instead?\n\n\n\n'
        + '--- Details for troubleshooting (please leave these in) ---\n' + details + '\n';
    let email = 'scott.reiling@gmail.com';
    try { email = (await api('/api/about')).feedback_email || email; } catch (err) { /* keep the default */ }
    let opened = true;
    try { await apiPost('/api/feedback/open', { body }); } catch (err) { opened = false; }
    // Not every computer has a mail app set up, so keep a copy to paste into any email
    let copied = false;
    try { await navigator.clipboard.writeText('To: ' + email + '\nSubject: EVBudget feedback\n\n' + body); copied = true; } catch (e) { /* clipboard blocked */ }
    if (opened) showToast(copied ? `Opening your email app. If nothing opens, the message was copied: paste it into an email to ${email}.` : `Opening your email app. If nothing opens, email ${email}.`);
    else showToast(copied ? `Couldn't open your email app. The message was copied: paste it into an email to ${email}.` : `Couldn't open your email app. Email ${email}.`, true);
}

document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeWhatsNew(); });
    // After the walkthrough has had its turn to start, so a first-time visitor sees one popup, not two
    setTimeout(relCheckOnStart, 600);
});
