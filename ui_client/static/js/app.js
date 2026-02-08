/**
 * RapidReach Dashboard — Client-side JavaScript
 * Handles WebSocket connection, UI updates, and workflow triggers.
 */

// ── State ────────────────────────────────────────────────────

const state = {
    ws: null,
    connected: false,
    businesses: [],
    events: [],
    sdrSessions: [],      // populated from /api/sdr_sessions
    meetingsData: [],      // populated from /api/meetings + SDR invites
    stats: {
        totalLeads: 0,
        contacted: 0,
        meetings: 0,
        emailsSent: 0,
    },
};

// ── WebSocket ────────────────────────────────────────────────

function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    state.ws = new WebSocket(`${protocol}//${location.host}/ws`);

    // Setup heartbeat to keep connection alive
    let heartbeatInterval;

    state.ws.onopen = () => {
        state.connected = true;
        updateConnectionStatus(true);
        console.log('WebSocket connected');
        
        // Start sending heartbeat every 5 minutes to prevent timeout
        heartbeatInterval = setInterval(() => {
            if (state.ws && state.ws.readyState === WebSocket.OPEN) {
                state.ws.send(JSON.stringify({ type: 'heartbeat' }));
            }
        }, 300000); // 5 minutes
    };

    state.ws.onclose = () => {
        state.connected = false;
        updateConnectionStatus(false);
        console.log('WebSocket disconnected, reconnecting in 2s...');
        
        // Clear heartbeat interval
        if (heartbeatInterval) {
            clearInterval(heartbeatInterval);
        }
        
        setTimeout(connectWebSocket, 2000);
    };

    state.ws.onerror = (err) => {
        console.error('WebSocket error:', err);
        
        // Clear heartbeat interval on error
        if (heartbeatInterval) {
            clearInterval(heartbeatInterval);
        }
    };

    state.ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleMessage(data);
        } catch (e) {
            console.error('Failed to parse message:', e);
        }
    };
}

function handleMessage(data) {
    if (data.type === 'init') {
        // Initial state dump
        if (data.businesses) {
            state.businesses = data.businesses;
            renderLeadsTable();
            updateStats();
        }
        if (data.recent_events) {
            data.recent_events.forEach(evt => addEventToLog(evt));
        }
    } else if (data.type === 'agent_event') {
        addEventToLog(data);
        handleAgentEvent(data);
    } else if (data.type === 'human_input_request') {
        showHumanInputModal(data);
    } else if (data.type === 'heartbeat') {
        // Server heartbeat - send ack back
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.ws.send(JSON.stringify({ type: 'heartbeat_ack' }));
        }
    } else if (data.type === 'keepalive' || data.type === 'pong') {
        // Server keepalive or pong response - just log
        console.log('Received keepalive/pong from server');
    }
}

function handleAgentEvent(evt) {
    if (evt.event === 'lead_found' && evt.data) {
        const existing = state.businesses.find(b => b.place_id === evt.data.place_id);
        if (!existing) {
            state.businesses.push(evt.data);
        }
        renderLeadsTable();
        updateStats();
    }

    if (evt.event === 'search_completed' && evt.data && evt.data.leads) {
        evt.data.leads.forEach(lead => {
            const existing = state.businesses.find(b => b.place_id === lead.place_id);
            if (!existing) {
                state.businesses.push(lead);
            }
        });
        renderLeadsTable();
        updateStats();
    }

    if (evt.event === 'meeting_scheduled') {
        state.stats.meetings++;
        updateStats();
    }

    // On SDR completion, refresh outreach data and stats
    if (evt.event === 'sdr_completed') {
        fetchSDRSessions();
    }

    // Re-enable buttons on completion/error
    if (['search_completed', 'sdr_completed', 'processing_completed', 'error'].includes(evt.event)) {
        enableButtons();
    }
}

// ── UI Updates ───────────────────────────────────────────────

function updateConnectionStatus(connected) {
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    if (dot) {
        dot.className = 'status-dot' + (connected ? ' connected' : '');
    }
    if (text) {
        text.textContent = connected ? 'Connected' : 'Disconnected';
    }
}

function updateStats() {
    const totalLeads = state.businesses.length;
    // Count contacted from businesses with non-new status OR from SDR sessions
    const contactedFromLeads = state.businesses.filter(b => b.lead_status && b.lead_status !== 'new').length;
    const contacted = Math.max(contactedFromLeads, state.sdrSessions.length);
    const emailsSent = state.sdrSessions.filter(s => s.email_sent).length;
    const meetings = state.sdrSessions.filter(s =>
        s.call_outcome === 'interested' || s.call_outcome === 'agreed_to_email'
    ).length + state.meetingsData.length;

    setStatValue('stat-leads', totalLeads);
    setStatValue('stat-contacted', contacted);
    setStatValue('stat-meetings', meetings);
    setStatValue('stat-emails', emailsSent);
}

function setStatValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function addEventToLog(evt) {
    const log = document.getElementById('event-log');
    if (!log) return;

    const item = document.createElement('div');
    item.className = 'event-item';

    const time = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : new Date().toLocaleTimeString();
    const agentType = evt.agent_type || 'system';
    const message = evt.message || evt.event || '';

    item.innerHTML = `
        <span class="event-time">${time}</span>
        <span class="event-badge badge-${agentType}">${agentType.replace('_', ' ')}</span>
        <span class="event-message">${escapeHtml(message)}</span>
    `;

    // Prepend (newest first)
    log.insertBefore(item, log.firstChild);

    // Keep max 100 events
    while (log.children.length > 100) {
        log.removeChild(log.lastChild);
    }

    state.events.push(evt);
}

function renderLeadsTable() {
    const tbody = document.getElementById('leads-tbody');
    if (!tbody) return;

    tbody.innerHTML = '';

    state.businesses.forEach(biz => {
        const tr = document.createElement('tr');
        const status = biz.lead_status || 'new';
        const pid = escapeHtml(biz.place_id || '');
        tr.innerHTML = `
            <td><strong>${escapeHtml(biz.business_name || '')}</strong></td>
            <td>${escapeHtml(biz.address || '')}</td>
            <td>
                <input type="text" class="phone-input" id="phone-${pid}" value="${escapeHtml(biz.phone || '')}" placeholder="Enter phone" style="width:130px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg-secondary);color:var(--text-primary);font-size:0.85rem;" />
            </td>
            <td>${biz.rating ? biz.rating.toFixed(1) + ' ⭐' : '—'}</td>
            <td><span class="status status-${status}">${status.replace('_', ' ')}</span></td>
            <td>
                <button class="btn btn-primary btn-small" onclick="startSDR('${escapeHtml(biz.business_name)}', '${pid}', '${escapeHtml(biz.email || '')}', '${escapeHtml(biz.address || '')}', '${escapeHtml(biz.city || '')}')">
                    Run SDR
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// ── Actions ──────────────────────────────────────────────────

async function findLeads() {
    const city = document.getElementById('city-input').value.trim();
    if (!city) {
        alert('Please enter a city name');
        return;
    }

    const maxResults = parseInt(document.getElementById('max-results').value) || 20;
    const btn = document.getElementById('find-leads-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Searching...';

    try {
        const resp = await fetch('/start_lead_finding', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                city: city,
                max_results: maxResults,
                business_types: [],
                exclude_chains: true,
                min_rating: 0,
            }),
        });
        const data = await resp.json();
        if (data.status === 'error') {
            alert('Error: ' + data.message);
        }
    } catch (e) {
        alert('Failed to start lead finding: ' + e.message);
    }

    enableButtons();
}

async function startSDR(name, placeId, email, address, city) {
    const skipCall = document.getElementById('skip-call')?.checked ?? false;
    const deckTemplate = document.getElementById('deck-template')?.value ?? 'professional';

    // Read phone from the editable input field (user may have changed it)
    const phoneInput = document.getElementById(`phone-${placeId}`);
    const phone = phoneInput ? phoneInput.value.trim() : '';

    try {
        const resp = await fetch('/start_sdr', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                business_name: name,
                phone: phone,
                email: email,
                address: address,
                city: city,
                place_id: placeId,
                skip_call: skipCall,
                deck_template: deckTemplate,
            }),
        });
        const data = await resp.json();
        if (data.status === 'error') {
            alert('SDR Error: ' + data.message);
        }
    } catch (e) {
        alert('Failed to start SDR: ' + e.message);
    }
}

async function processEmails() {
    const btn = document.getElementById('process-emails-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Processing...';

    try {
        const resp = await fetch('/start_email_processing', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ max_emails: 10 }),
        });
        const data = await resp.json();
        if (data.status === 'error') {
            alert('Error: ' + data.message);
        }
    } catch (e) {
        alert('Failed to process emails: ' + e.message);
    }

    enableButtons();
}

function enableButtons() {
    const findBtn = document.getElementById('find-leads-btn');
    if (findBtn) {
        findBtn.disabled = false;
        findBtn.innerHTML = '🔍 Find Leads';
    }
    const emailBtn = document.getElementById('process-emails-btn');
    if (emailBtn) {
        emailBtn.disabled = false;
        emailBtn.innerHTML = '📧 Process Inbox';
    }
}

// ── Tabs ─────────────────────────────────────────────────────

function switchTab(tabName) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));

    document.querySelector(`.tab[data-tab="${tabName}"]`)?.classList.add('active');
    document.getElementById(`tab-${tabName}`)?.classList.add('active');

    // Load data when switching to specific tabs
    if (tabName === 'outreach') {
        loadSDROutreach();
    } else if (tabName === 'meetings') {
        loadMeetings();
    }
}

// ── Fetch SDR Sessions (for stats + outreach tab) ────────────

async function fetchSDRSessions() {
    try {
        const resp = await fetch('/api/sdr_sessions');
        const data = await resp.json();
        if (data.sessions) {
            state.sdrSessions = Object.values(data.sessions);
            updateStats();
            // If outreach tab is active, re-render it
            const activeTab = document.querySelector('.tab.active');
            if (activeTab && activeTab.dataset.tab === 'outreach') {
                renderOutreachTab();
            }
        }
    } catch (e) {
        console.warn('Failed to fetch SDR sessions:', e);
    }
}

// ── Load SDR Outreach Data ───────────────────────────────────

function renderOutreachTab() {
    const container = document.getElementById('outreach-log');
    if (!container) return;

    if (state.sdrSessions.length === 0) {
        container.innerHTML = '<div style="color:var(--text-dim); text-align:center; padding:40px;">No SDR outreach sessions yet. Run SDR on a lead to see data here.</div>';
        return;
    }

    const html = state.sdrSessions.map(session => {
        const outcomeColors = {
            'interested': 'var(--green)',
            'agreed_to_email': 'var(--accent)',
            'not_interested': 'var(--red)',
            'no_answer': 'var(--text-dim)',
            'other': 'var(--yellow)',
        };
        const outcomeColor = outcomeColors[session.call_outcome] || 'var(--text-dim)';
        const outcomeLabel = (session.call_outcome || 'unknown').replace('_', ' ');

        return `
            <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:12px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    <strong style="font-size:15px;">${escapeHtml(session.business_name || 'Unknown Business')}</strong>
                    <span style="font-size:12px;color:var(--text-dim);">${session.created_at ? new Date(session.created_at).toLocaleString() : ''}</span>
                </div>
                <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px;">
                    <span style="font-size:12px;padding:3px 10px;border-radius:12px;background:${outcomeColor}22;color:${outcomeColor};border:1px solid ${outcomeColor}44;">
                        📞 ${escapeHtml(outcomeLabel)}
                    </span>
                    <span style="font-size:12px;padding:3px 10px;border-radius:12px;background:${session.email_sent ? 'var(--green)' : 'var(--red)'}22;color:${session.email_sent ? 'var(--green)' : 'var(--red)'};border:1px solid ${session.email_sent ? 'var(--green)' : 'var(--red)'}44;">
                        ✉️ ${session.email_sent ? 'Email Sent' : 'No Email'}
                    </span>
                </div>
                ${session.email_subject ? `<div style="font-size:13px;color:var(--text-dim);margin-bottom:4px;">📧 <em>${escapeHtml(session.email_subject)}</em></div>` : ''}
                ${session.research_summary ? `<div style="font-size:12px;color:var(--text-dim);margin-top:6px;line-height:1.4;">${escapeHtml((session.research_summary || '').substring(0, 150))}${(session.research_summary || '').length > 150 ? '...' : ''}</div>` : ''}
            </div>
        `;
    }).join('');

    container.innerHTML = html;
}

async function loadSDROutreach(silent = false) {
    const container = document.getElementById('outreach-log');
    if (!container) return;

    if (!silent) {
        container.innerHTML = '<div style="text-align:center; padding:20px;">Loading SDR outreach data...</div>';
    }

    await fetchSDRSessions();
    renderOutreachTab();
}

// ── Load Meetings Data ──────────────────────────────────────

async function loadMeetings(silent = false) {
    const container = document.getElementById('meetings-log');
    if (!container) return;

    if (!silent) {
        container.innerHTML = '<div style="text-align:center; padding:20px;">Loading meetings data...</div>';
    }

    try {
        // Fetch from lead_manager
        let meetings = [];
        try {
            const resp = await fetch('/api/meetings');
            const data = await resp.json();
            meetings = data.meetings || [];
        } catch (e) {
            console.warn('Lead manager meetings fetch failed:', e);
        }

        // Also derive meetings from SDR sessions that sent calendar invites
        const sdrMeetings = state.sdrSessions
            .filter(s => s.email_sent && (s.call_outcome === 'interested' || s.call_outcome === 'agreed_to_email' || s.email_sent))
            .map(s => ({
                title: `Follow-up: ${s.business_name || 'Unknown'}`,
                organizer: 'RapidReach Team',
                attendees: [],
                start_time: s.created_at,
                source: 'sdr_outreach',
                business_name: s.business_name,
                call_outcome: s.call_outcome,
            }));

        const allMeetings = [...meetings, ...sdrMeetings];
        state.meetingsData = allMeetings;
        updateStats();

        if (allMeetings.length === 0) {
            container.innerHTML = '<div style="color:var(--text-dim); text-align:center; padding:40px;">No meetings scheduled yet. Run SDR outreach or process emails to see meetings here.</div>';
            return;
        }

        const html = allMeetings.map(meeting => {
            const isSDR = meeting.source === 'sdr_outreach';
            const outcomeLabel = meeting.call_outcome ? ` • ${meeting.call_outcome.replace('_', ' ')}` : '';
            return `
                <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:12px;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <strong style="font-size:15px;">${escapeHtml(meeting.title || 'Meeting')}</strong>
                        <span style="font-size:12px;color:var(--text-dim);">${meeting.start_time ? new Date(meeting.start_time).toLocaleString() : ''}</span>
                    </div>
                    <div style="display:flex;gap:12px;flex-wrap:wrap;">
                        <span style="font-size:12px;color:var(--text-dim);">👤 ${escapeHtml(meeting.organizer || 'RapidReach')}</span>
                        ${isSDR ? `<span style="font-size:12px;padding:2px 8px;border-radius:10px;background:var(--accent)22;color:var(--accent);border:1px solid var(--accent)44;">📅 Calendar Invite Sent${outcomeLabel}</span>` : ''}
                        ${!isSDR && meeting.location ? `<span style="font-size:12px;color:var(--text-dim);">📍 ${escapeHtml(meeting.location)}</span>` : ''}
                    </div>
                    ${meeting.description ? `<div style="font-size:12px;color:var(--text-dim);margin-top:8px;line-height:1.4;">${escapeHtml(meeting.description.substring(0, 150))}${meeting.description.length > 150 ? '...' : ''}</div>` : ''}
                </div>
            `;
        }).join('');

        container.innerHTML = html;

    } catch (error) {
        console.error('Failed to load meetings:', error);
        if (!silent) {
            container.innerHTML = `<div style="color:var(--danger); text-align:center; padding:20px;">Failed to load meetings data</div>`;
        }
    }
}

// ── Human Input Modal ────────────────────────────────────────

function showHumanInputModal(data) {
    const overlay = document.getElementById('modal-overlay');
    const prompt = document.getElementById('modal-prompt');
    const input = document.getElementById('modal-input');
    const submitBtn = document.getElementById('modal-submit');

    if (!overlay || !prompt) return;

    prompt.textContent = data.prompt || 'Agent needs your input:';
    input.value = '';
    overlay.classList.add('active');

    submitBtn.onclick = async () => {
        const response = input.value.trim();
        if (!response) return;

        try {
            await fetch('/api/human-input/respond', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    request_id: data.request_id,
                    response: response,
                }),
            });
        } catch (e) {
            console.error('Failed to send human input:', e);
        }

        overlay.classList.remove('active');
    };
}

function closeModal() {
    document.getElementById('modal-overlay')?.classList.remove('active');
}

// ── Helpers ──────────────────────────────────────────────────

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ── Init ─────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    connectWebSocket();

    // Tab click handlers
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    // Fetch SDR sessions on page load to populate stats
    fetchSDRSessions();
});
