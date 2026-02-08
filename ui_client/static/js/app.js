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
    stats: {
        totalLeads: 0,
        contacted: 0,
        meetings: 0,
        hotLeads: 0,
    },
    autoRefresh: {
        enabled: true,
        intervals: {},
        lastUpdate: {
            businesses: 0,
            sdrSessions: 0,
            meetings: 0,
        }
    }
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

    if (evt.event === 'hot_lead_detected') {
        state.stats.hotLeads++;
        updateStats();
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
    const contacted = state.businesses.filter(b => b.lead_status !== 'new').length;
    const hotLeads = state.businesses.filter(b => b.lead_status === 'hot_lead').length;

    setStatValue('stat-leads', totalLeads);
    setStatValue('stat-contacted', contacted);
    setStatValue('stat-meetings', state.stats.meetings);
    setStatValue('stat-hot', hotLeads || state.stats.hotLeads);
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

// ── Load SDR Outreach Data ───────────────────────────────────

async function loadSDROutreach(silent = false) {
    const container = document.getElementById('outreach-log');
    if (!container) return;

    if (!silent) {
        container.innerHTML = '<div style="text-align:center; padding:20px;">Loading SDR outreach data...</div>';
    }

    try {
        const resp = await fetch('/api/sdr_sessions');
        const data = await resp.json();
        
        if (data.error) {
            if (!silent) {
                container.innerHTML = `<div style="color:var(--danger); text-align:center; padding:20px;">Error loading SDR data: ${data.error}</div>`;
            }
            return;
        }

        const sessions = data.sessions || {};
        const sessionList = Object.values(sessions);

        if (sessionList.length === 0) {
            container.innerHTML = '<div style="color:var(--text-dim); text-align:center; padding:40px;">No SDR outreach sessions yet. Run SDR on a lead to see data here.</div>';
            return;
        }

        const html = sessionList.map(session => `
            <div class="event-item full-width">
                <div class="event-header">
                    <strong>${escapeHtml(session.business_name || 'Unknown Business')}</strong>
                    <span class="timestamp">${new Date(session.created_at || Date.now()).toLocaleString()}</span>
                </div>
                <div class="event-body">
                    <div><strong>Outcome:</strong> ${escapeHtml(session.call_outcome || 'unknown')}</div>
                    <div><strong>Email Sent:</strong> ${session.email_sent ? 'Yes' : 'No'}</div>
                    ${session.email_subject ? `<div><strong>Subject:</strong> ${escapeHtml(session.email_subject)}</div>` : ''}
                    <div style="margin-top:8px; font-size:13px; color:var(--text-dim);">
                        Research: ${session.research_summary ? session.research_summary.substring(0, 100) + '...' : 'N/A'}
                    </div>
                </div>
            </div>
        `).join('');

        container.innerHTML = html;

    } catch (error) {
        console.error('Failed to load SDR outreach:', error);
        container.innerHTML = `<div style="color:var(--danger); text-align:center; padding:20px;">Failed to load SDR outreach data</div>`;
    }
}

// ── Load Meetings Data ──────────────────────────────────────

async function loadMeetings(silent = false) {
    const container = document.getElementById('meetings-log');
    if (!container) return;

    if (!silent) {
        container.innerHTML = '<div style="text-align:center; padding:20px;">Loading meetings data...</div>';
    }

    try {
        const resp = await fetch('/api/meetings');
        const data = await resp.json();
        
        if (data.error) {
            if (!silent) {
                container.innerHTML = `<div style="color:var(--danger); text-align:center; padding:20px;">Error loading meetings: ${data.error}</div>`;
            }
            return;
        }

        const meetings = data.meetings || [];

        if (meetings.length === 0) {
            container.innerHTML = '<div style="color:var(--text-dim); text-align:center; padding:40px;">No meetings scheduled yet. Process emails to see scheduled meetings here.</div>';
            return;
        }

        const html = meetings.map(meeting => `
            <div class="event-item full-width">
                <div class="event-header">
                    <strong>${escapeHtml(meeting.title || 'Meeting')}</strong>
                    <span class="timestamp">${new Date(meeting.start_time || Date.now()).toLocaleString()}</span>
                </div>
                <div class="event-body">
                    <div><strong>Organizer:</strong> ${escapeHtml(meeting.organizer || 'Unknown')}</div>
                    <div><strong>Attendees:</strong> ${(meeting.attendees || []).length} people</div>
                    ${meeting.location ? `<div><strong>Location:</strong> ${escapeHtml(meeting.location)}</div>` : ''}
                    ${meeting.description ? `<div style="margin-top:8px; font-size:13px; color:var(--text-dim);">${escapeHtml(meeting.description.substring(0, 150))}${meeting.description.length > 150 ? '...' : ''}</div>` : ''}
                </div>
            </div>
        `).join('');

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

// ── Hot Reload / Auto Refresh ────────────────────────────────

function startAutoRefresh() {
    if (!state.autoRefresh.enabled) return;

    console.log('🔥 Hot reload enabled - Starting auto refresh intervals');

    // Refresh businesses data every 30 seconds
    state.autoRefresh.intervals.businesses = setInterval(async () => {
        try {
            const resp = await fetch('/api/businesses');
            const data = await resp.json();
            if (data.businesses && JSON.stringify(data.businesses) !== JSON.stringify(state.businesses)) {
                console.log('🔄 Auto-refreshing businesses data');
                state.businesses = data.businesses;
                renderLeadsTable();
                updateStats();
                state.autoRefresh.lastUpdate.businesses = Date.now();
            }
        } catch (e) {
            console.warn('Auto refresh businesses failed:', e);
        }
    }, 30000); // 30 seconds

    // Refresh active tab data every 45 seconds
    state.autoRefresh.intervals.tabs = setInterval(() => {
        const activeTab = document.querySelector('.tab.active');
        if (activeTab) {
            const tabName = activeTab.dataset.tab;
            if (tabName === 'outreach') {
                loadSDROutreach(true); // silent reload
            } else if (tabName === 'meetings') {
                loadMeetings(true); // silent reload
            }
        }
    }, 45000); // 45 seconds

    // Refresh events every 20 seconds
    state.autoRefresh.intervals.events = setInterval(async () => {
        try {
            const resp = await fetch('/api/events?limit=20');
            const data = await resp.json();
            if (data.events && data.events.length > 0) {
                const latestEvent = data.events[data.events.length - 1];
                const lastKnownEvent = state.events[state.events.length - 1];
                
                if (!lastKnownEvent || latestEvent.timestamp !== lastKnownEvent.timestamp) {
                    console.log('🔄 Auto-refreshing events');
                    // Add only new events to avoid duplicates
                    const newEvents = data.events.filter(evt => 
                        !state.events.some(existing => existing.timestamp === evt.timestamp)
                    );
                    newEvents.forEach(evt => {
                        addEventToLog(evt);
                        handleAgentEvent(evt);
                    });
                }
            }
        } catch (e) {
            console.warn('Auto refresh events failed:', e);
        }
    }, 20000); // 20 seconds
}

function stopAutoRefresh() {
    console.log('⏹️ Stopping auto refresh');
    Object.values(state.autoRefresh.intervals).forEach(interval => {
        if (interval) clearInterval(interval);
    });
    state.autoRefresh.intervals = {};
}

function toggleAutoRefresh() {
    const toggle = document.getElementById('hot-reload-toggle');
    const icon = document.getElementById('hot-reload-icon');
    const text = document.getElementById('hot-reload-text');
    
    state.autoRefresh.enabled = !state.autoRefresh.enabled;
    
    if (state.autoRefresh.enabled) {
        startAutoRefresh();
        toggle.classList.add('active');
        icon.textContent = '🔄';
        text.textContent = 'Auto-refresh: ON';
        showToast('🔥 Hot reload enabled', 'success');
    } else {
        stopAutoRefresh();
        toggle.classList.remove('active');
        icon.textContent = '🔄';
        text.textContent = 'Auto-refresh: OFF';
        showToast('⏹️ Hot reload disabled', 'info');
    }
}

function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    
    // Add toast styles if not already present
    if (!document.getElementById('toast-styles')) {
        const styles = document.createElement('style');
        styles.id = 'toast-styles';
        styles.textContent = `
            .toast {
                position: fixed;
                top: 20px;
                right: 20px;
                padding: 12px 16px;
                border-radius: 6px;
                color: white;
                font-size: 14px;
                font-weight: 500;
                z-index: 10000;
                animation: toastSlide 0.3s ease, toastFade 0.3s ease 2.7s;
            }
            .toast-success { background: #22c55e; }
            .toast-info { background: #3b82f6; }
            .toast-warning { background: #f59e0b; }
            .toast-error { background: #ef4444; }
            @keyframes toastSlide { from { transform: translateX(100%); } to { transform: translateX(0); } }
            @keyframes toastFade { from { opacity: 1; } to { opacity: 0; } }
        `;
        document.head.appendChild(styles);
    }
    
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

// ── Init ─────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    connectWebSocket();

    // Tab click handlers
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });
    
    // Initialize hot reload as enabled by default
    setTimeout(() => {
        const toggle = document.getElementById('hot-reload-toggle');
        if (toggle && !state.autoRefresh.enabled) {
            toggleAutoRefresh();
        }
    }, 1000); // Wait 1 second for everything to load before starting auto-refresh
});
