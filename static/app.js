const state = {
  leads: [],
  counts: null,
  audit: [],
  deliveries: [],
  sourceMetrics: [],
  settings: {},
  setup: null,
  automationHealth: null,
  notifications: [],
  selectedLeadId: null,
  selectedLeadName: "",
  filter: "ALL",
  tab: "deliveries",
  lastRefreshAt: null,
};

const els = {
  kpis: document.querySelector("#kpis"),
  leadRows: document.querySelector("#leadRows"),
  leadForm: document.querySelector("#leadForm"),
  dncForm: document.querySelector("#dncForm"),
  optOutForm: document.querySelector("#optOutForm"),
  settingsForm: document.querySelector("#settingsForm"),
  runOps: document.querySelector("#runOps"),
  sourceMetrics: document.querySelector("#sourceMetrics"),
  aiForm: document.querySelector("#aiForm"),
  aiMessages: document.querySelector("#aiMessages"),
  aiProvider: document.querySelector("#aiProvider"),
  setupChecks: document.querySelector("#setupChecks"),
  setupProgress: document.querySelector("#setupProgress"),
  runTestFlow: document.querySelector("#runTestFlow"),
  runQualifier: document.querySelector("#runQualifier"),
  runSetupAutopilot: document.querySelector("#runSetupAutopilot"),
  runBotTeam: document.querySelector("#runBotTeam"),
  runCalendlySync: document.querySelector("#runCalendlySync"),
  ownerAlerts: document.querySelector("#ownerAlerts"),
  alertCount: document.querySelector("#alertCount"),
  threadLeadName: document.querySelector("#threadLeadName"),
  threadMessages: document.querySelector("#threadMessages"),
  threadForm: document.querySelector("#threadForm"),
  markPhoneVerified: document.querySelector("#markPhoneVerified"),
  markWrongNumber: document.querySelector("#markWrongNumber"),
  saveStatus: document.querySelector("#saveStatus"),
  activityFeed: document.querySelector("#activityFeed"),
  autopilotStatus: document.querySelector("#autopilotStatus"),
  automationCards: document.querySelector("#automationCards"),
  automationPriorities: document.querySelector("#automationPriorities"),
  lastUpdated: document.querySelector("#lastUpdated"),
  dialog: document.querySelector("#actionDialog"),
  actionForm: document.querySelector("#actionForm"),
  dialogTitle: document.querySelector("#dialogTitle"),
  dialogFields: document.querySelector("#dialogFields"),
  cancelDialog: document.querySelector("#cancelDialog"),
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      "X-Actor": "dashboard",
      ...(options.headers || {}),
    },
    ...options,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (res.status === 401) {
    window.location.href = "/login";
    return {};
  }
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function refresh() {
  const data = await api("/api/dashboard");
  state.leads = data.leads || [];
  state.counts = data.counts;
  state.audit = data.audit || [];
  state.deliveries = data.deliveries || [];
  state.sourceMetrics = data.source_metrics || [];
  state.settings = data.settings || {};
  state.notifications = data.notifications || [];
  state.automationHealth = data.automation_health || null;
  state.lastRefreshAt = new Date();
  renderKpis();
  renderAutomationHealth();
  renderLeads();
  renderActivity();
  renderSources();
  renderAlerts();
  renderSettings();
  await renderSetup();
  if (state.selectedLeadId) await renderThread();
}

function renderKpis() {
  const counts = state.counts || { total: 0, stage_counts: {}, avg_score: 0 };
  const cards = [
    ["Total", counts.total],
    ["New", counts.stage_counts.NEW || 0],
    ["Qualified", counts.stage_counts.QUALIFIED || 0],
    ["Ready", counts.stage_counts.READY || 0],
    ["Won", counts.stage_counts.WON || 0],
    ["Avg Score", counts.avg_score],
  ];
  els.kpis.innerHTML = cards.map(([label, value]) => `<article class="kpi"><span>${label}</span><strong>${value}</strong></article>`).join("");
}

function renderAutomationHealth() {
  if (!els.automationCards || !els.automationPriorities) return;
  const health = state.automationHealth || { label: "Checking", cards: [], priorities: [] };
  els.autopilotStatus.textContent = health.label || "Checking";
  els.autopilotStatus.className = `status-pill ${health.status || ""}`;
  els.automationCards.innerHTML = (health.cards || []).map((card) => `
    <article class="automation-card">
      <span>${escapeHtml(card.label)}</span>
      <strong>${escapeHtml(card.value)}</strong>
    </article>
  `).join("");
  els.automationPriorities.innerHTML = (health.priorities || []).map((item) => `
    <div class="priority-item ${escapeAttr(item.level || "info")}">${escapeHtml(item.text)}</div>
  `).join("");
  if (els.lastUpdated && state.lastRefreshAt) {
    els.lastUpdated.textContent = `Updated ${state.lastRefreshAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }
}

function renderLeads() {
  const leads = state.filter === "ALL" ? state.leads : state.leads.filter((lead) => lead.stage === state.filter);
  if (!leads.length) {
    els.leadRows.innerHTML = `<tr><td colspan="6">No leads in this view.</td></tr>`;
    return;
  }
  els.leadRows.innerHTML = leads.map((lead) => `
    <tr>
      <td>
        <div class="lead-name">${escapeHtml(lead.name)}</div>
        <span class="lead-meta">${escapeHtml(lead.source)} | ${escapeHtml(lead.state || "NA")} | ${escapeHtml(lead.phone || "")}</span>
        <span class="lead-meta">${escapeHtml(lead.compliance_status || "PENDING")} | ${escapeHtml(lead.contact_status || "CONTACTABLE")}</span>
        <span class="lead-meta">${qualificationCompleteness(lead)}</span>
        <span class="lead-meta">${leadStatusLine(lead)}</span>
      </td>
      <td>${escapeHtml(lead.segment)}</td>
      <td><span class="score">${lead.score}</span></td>
      <td><span class="stage-pill ${lead.stage}">${lead.stage}</span></td>
      <td>${escapeHtml(lead.owner)}<span class="lead-meta">${escapeHtml(lead.assigned_to || "Unassigned")}</span></td>
      <td>${actionsForLead(lead)}</td>
    </tr>
  `).join("");
}

function qualificationCompleteness(lead) {
  const fields = ["coverage_for", "plan_start_timing", "needs_dental_vision", "conditions_meds", "dob", "height", "weight", "annualized_income_text"];
  const done = fields.filter((field) => String(lead[field] || "").trim()).length;
  return `Intake ${done}/${fields.length}`;
}

function leadStatusLine(lead) {
  if (lead.phone_status === "WRONG_NUMBER") return "Wrong number: replace or suppress";
  if (lead.compliance_status === "BLOCKED") return "Blocked by compliance";
  if (lead.stage === "READY") return lead.appointment_at ? `Booked ${formatShortDate(lead.appointment_at)}` : "Ready for owner review";
  if (lead.stage === "QUALIFIED") {
    return lead.booking_link_sent_at ? "Calendly link sent; waiting for booking" : "Qualified; booking link needed";
  }
  if (lead.stage === "NEW" || lead.stage === "TRIAGE") {
    return lead.qualification_step ? `Waiting on ${humanStep(lead.qualification_step)}` : "Auto qualifier starting";
  }
  return lead.last_contacted_at ? `Last touch ${formatShortDate(lead.last_contacted_at)}` : "No recent touch";
}

function humanStep(step) {
  return String(step || "")
    .replaceAll("_", " ")
    .replace("dob height weight income", "DOB, height, weight, income");
}

function formatShortDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function renderSources() {
  if (!els.sourceMetrics) return;
  if (!state.sourceMetrics.length) {
    els.sourceMetrics.innerHTML = `<div class="activity-item"><strong>No source data</strong><span>Leads will appear here by source.</span></div>`;
    return;
  }
  els.sourceMetrics.innerHTML = state.sourceMetrics.map((source) => `
    <div class="source-row">
      <div><strong>${escapeHtml(source.source)}</strong><span>Total ${source.total}</span></div>
      <div><span>Avg</span><strong>${source.avg_score || 0}</strong></div>
      <div><span>Ready</span><strong>${source.ready || 0}</strong></div>
      <div><span>Won</span><strong>${source.won || 0}</strong></div>
      <div><span>Blocked</span><strong>${source.blocked || 0}</strong></div>
    </div>
  `).join("");
}

function renderSettings() {
  if (!els.settingsForm || els.settingsForm.dataset.rendered === "true") return;
  const s = state.settings || {};
  els.settingsForm.innerHTML = `
    <label>Business<input name="business_name" value="${escapeAttr(s.business_name || "")}" /></label>
    <label>Principal Email<input name="principal_email" value="${escapeAttr(s.principal_email || "")}" /></label>
    <label class="wide">Licensed States<input name="licensed_states" value="${escapeAttr(s.licensed_states || "")}" /></label>
    <label class="wide">HubSpot Access Token<input name="hubspot_access_token" value="${escapeAttr(s.hubspot_access_token || "")}" /></label>
    <label class="wide">Booking URL<input name="calendar_booking_url" value="${escapeAttr(s.calendar_booking_url || "")}" /></label>
    <label class="wide">Calendly API Token<input name="calendly_api_token" value="${escapeAttr(s.calendly_api_token || "")}" placeholder="Personal access token" /></label>
    <div class="settings-help wide">
      <a href="https://calendly.com/integrations/api_webhooks" target="_blank" rel="noreferrer">Open Calendly API setup</a>
      <span>Create a personal access token, paste it above, save, then press Sync Calendly.</span>
    </div>
    <label>Calendly Sync Days<input name="calendly_sync_days" type="number" min="1" max="365" value="${escapeAttr(s.calendly_sync_days || "60")}" /></label>
    <label>Qualifier SLA Minutes<input name="qualifier_sla_minutes" type="number" min="1" value="${escapeAttr(s.qualifier_sla_minutes || "15")}" /></label>
    <label>Qualifier Follow-up Hours<input name="qualifier_followup_hours" type="number" min="1" value="${escapeAttr(s.qualifier_followup_hours || "2")}" /></label>
    <label>Qualifier Max Follow-ups<input name="qualifier_max_followups" type="number" min="0" value="${escapeAttr(s.qualifier_max_followups || "2")}" /></label>
    <label>Booking Follow-up Hours<input name="booking_followup_hours" type="number" min="1" value="${escapeAttr(s.booking_followup_hours || "24")}" /></label>
    <label>Booking Max Follow-ups<input name="booking_max_followups" type="number" min="0" value="${escapeAttr(s.booking_max_followups || "2")}" /></label>
    <label>Auto Owner<input name="auto_assign_owner" value="${escapeAttr(s.auto_assign_owner || "")}" /></label>
    <label class="wide">Email Template<textarea name="email_template" rows="4">${escapeHtml(s.email_template || "")}</textarea></label>
    <button class="primary-button" type="submit">Save Settings</button>
  `;
  els.settingsForm.dataset.rendered = "true";
}

async function renderSetup() {
  if (!els.setupChecks) return;
  const data = await api("/api/setup/status");
  state.setup = data;
  els.setupProgress.textContent = `${data.complete}/${data.total}`;
  els.setupChecks.innerHTML = data.checks.map((check) => `
    <div class="setup-check ${check.ok ? "ok" : "missing"}">
      <span class="mark">${check.ok ? "✓" : "!"}</span>
      <div>
        <strong>${escapeHtml(check.label)}</strong>
        <p>${escapeHtml(check.detail || "")}</p>
        ${check.key === "calendly_api" && !check.ok ? `<a class="inline-link" href="https://calendly.com/integrations/api_webhooks" target="_blank" rel="noreferrer">Open Calendly API setup</a>` : ""}
      </div>
    </div>
  `).join("");
}

function renderAlerts() {
  if (!els.ownerAlerts) return;
  els.alertCount.textContent = String(state.notifications.length);
  if (!state.notifications.length) {
    els.ownerAlerts.innerHTML = `<div class="activity-item"><strong>No ready alerts</strong><span>You will see booked READY leads here.</span></div>`;
    return;
  }
  els.ownerAlerts.innerHTML = state.notifications.map((alert) => `
    <div class="owner-alert">
      <strong>${escapeHtml(alert.title)}</strong>
      <p>${escapeHtml(alert.body)}</p>
      <div class="action-stack">
        ${leadExists(alert.lead_id) ? `<button type="button" data-lead="${escapeAttr(alert.lead_id)}" data-action="thread">Thread</button>` : ""}
        <button type="button" data-alert-read="${escapeAttr(alert.id)}">Done</button>
      </div>
    </div>
  `).join("");
}

function leadExists(leadId) {
  return Boolean(leadId && state.leads.some((lead) => lead.id === leadId));
}

function actionsForLead(lead) {
  const buttons = [actionButton(lead.id, "thread", "Thread")];
  if (lead.stage === "NEW") buttons.push(actionButton(lead.id, "assign", "Assign"));
  if (lead.stage === "TRIAGE") buttons.push(actionButton(lead.id, "qualify", "Qualify"));
  if (lead.stage === "QUALIFIED") {
    buttons.push(actionButton(lead.id, "sendBooking", "Prep Link"));
    buttons.push(`<a href="/api/leads/${lead.id}/booking" target="_blank" rel="noreferrer">Book</a>`);
    buttons.push(actionButton(lead.id, "emailBooking", "Email"));
    buttons.push(actionButton(lead.id, "ready", "Ready"));
  }
  if (lead.stage === "READY") {
    buttons.push(actionButton(lead.id, "won", "Won"));
    buttons.push(actionButton(lead.id, "lost", "Lost"));
    buttons.push(`<a href="/api/leads/${lead.id}/calendar/ics">ICS</a>`);
  }
  if (!buttons.length) buttons.push(`<span class="lead-meta">Closed</span>`);
  return `<div class="action-stack">${buttons.join("")}</div>`;
}

function actionButton(id, action, label) {
  return `<button type="button" data-lead="${id}" data-action="${action}">${label}</button>`;
}

function renderActivity() {
  const items = state.tab === "deliveries" ? state.deliveries : state.audit;
  if (!items.length) {
    els.activityFeed.innerHTML = `<div class="activity-item"><strong>No activity yet</strong><span>Waiting for pipeline events.</span></div>`;
    return;
  }
  els.activityFeed.innerHTML = items.map((item) => {
    if (state.tab === "deliveries") {
      return `<article class="activity-item"><strong>${escapeHtml(item.target)}: ${escapeHtml(item.status)}</strong><span>${escapeHtml(item.created_at)}</span><p>${escapeHtml(item.response)}</p></article>`;
    }
    return `<article class="activity-item"><strong>${escapeHtml(item.action)}</strong><span>${escapeHtml(item.created_at)} by ${escapeHtml(item.actor)}</span><p>${escapeHtml(item.detail)}</p></article>`;
  }).join("");
}

function openActionDialog(leadId, action) {
  const lead = state.leads.find((item) => item.id === leadId);
  if (!lead) return;
  if (action === "thread") {
    selectThread(lead);
    return;
  }
  if (action === "emailBooking") {
    openBookingEmail(leadId);
    return;
  }
  if (action === "sendBooking") {
    prepareBookingNote(leadId);
    return;
  }
  const titles = {
    assign: `Assign ${lead.name}`,
    qualify: `Qualify ${lead.name}`,
    ready: `Mark READY`,
    won: `Mark WON`,
    lost: `Mark LOST`,
  };
  els.dialogTitle.textContent = titles[action] || "Lead Action";
  els.actionForm.lead_id.value = leadId;
  els.actionForm.action.value = action;
  els.dialogFields.innerHTML = fieldsForAction(action, lead);
  els.dialog.showModal();
}

async function selectThread(lead) {
  state.selectedLeadId = lead.id;
  state.selectedLeadName = lead.name;
  els.threadLeadName.textContent = lead.name;
  await renderThread();
}

async function renderThread() {
  if (!state.selectedLeadId) {
    els.threadMessages.innerHTML = `<div class="thread-empty">Select a lead to view the manual follow-up thread.</div>`;
    return;
  }
  const data = await api(`/api/leads/${state.selectedLeadId}/conversation`);
  const messages = data.messages || [];
  if (!messages.length) {
    els.threadMessages.innerHTML = `<div class="thread-empty">No messages saved yet.</div>`;
    return;
  }
  els.threadMessages.innerHTML = messages.map((message) => `
    <div class="thread-message ${escapeAttr(message.direction)}">
      <span>${escapeHtml(message.direction)} | ${escapeHtml(message.sender)} | ${escapeHtml(message.created_at)}</span>
      ${escapeHtml(message.body)}
    </div>
  `).join("");
  els.threadMessages.scrollTop = els.threadMessages.scrollHeight;
}

async function openBookingEmail(leadId) {
  const data = await api(`/api/leads/${leadId}/booking-link`);
  if (data.email_url) {
    window.location.href = data.email_url;
    return;
  }
  if (data.booking_url) {
    await navigator.clipboard.writeText(data.booking_url);
    addAiMessage("assistant", "Booking link copied. No lead email was available for a mail draft.");
    return;
  }
  addAiMessage("assistant", "Booking URL is not configured. Add your Calendly link in the Automation panel.");
}

async function prepareBookingNote(leadId) {
  const lead = state.leads.find((item) => item.id === leadId);
  const name = lead ? lead.name : "lead";
  addAiMessage("assistant", `Preparing Calendly link for ${name}...`);
  try {
    const data = await api(`/api/leads/${leadId}/booking-note`, { method: "POST", body: "{}" });
    const body = data.message && data.message.body ? data.message.body : "Booking link prepared.";
    addAiMessage("assistant", `Manual follow-up note saved: ${body}`);
    await refresh();
    if (state.selectedLeadId === leadId) await renderThread();
  } catch (error) {
    addAiMessage("assistant", `Could not prepare booking link: ${error.message}`);
  }
}

function fieldsForAction(action, lead) {
  if (action === "assign") {
    return `<label>Qualifier<input name="assigned_to" required value="${escapeAttr(lead.assigned_to || "Qualifier Team")}" /></label>`;
  }
  if (action === "qualify") {
    return `
      <label>Coverage For
        <select name="coverage_for" required>
          <option value="">Select</option>
          <option value="Just myself" ${lead.coverage_for === "Just myself" ? "selected" : ""}>Just myself</option>
          <option value="Family" ${lead.coverage_for === "Family" ? "selected" : ""}>Family</option>
        </select>
      </label>
      <label>How soon did they need a plan to begin?<input name="plan_start_timing" required value="${escapeAttr(lead.plan_start_timing || "")}" placeholder="ASAP, next month, after COBRA ends..." /></label>
      <label>Medical only or dental/vision too?
        <select name="needs_dental_vision" required>
          <option value="">Select</option>
          <option value="Medical only" ${lead.needs_dental_vision === "Medical only" ? "selected" : ""}>Medical only</option>
          <option value="Medical + dental" ${lead.needs_dental_vision === "Medical + dental" ? "selected" : ""}>Medical + dental</option>
          <option value="Medical + vision" ${lead.needs_dental_vision === "Medical + vision" ? "selected" : ""}>Medical + vision</option>
          <option value="Medical + dental + vision" ${lead.needs_dental_vision === "Medical + dental + vision" ? "selected" : ""}>Medical + dental + vision</option>
        </select>
      </label>
      <label>Pre-existing conditions or medications that need covered?<textarea name="conditions_meds" rows="3" placeholder="Keep this minimal. No diagnosis or med names unless necessary.">${escapeHtml(lead.conditions_meds || "")}</textarea></label>
      <div class="form-pair">
        <label>DOB<input name="dob" required placeholder="MM/DD/YYYY" value="${escapeAttr(lead.dob || "")}" /></label>
        <label>Height<input name="height" required placeholder="5'10 or 70 in" value="${escapeAttr(lead.height || "")}" /></label>
      </div>
      <div class="form-pair">
        <label>Weight<input name="weight" required placeholder="180 lb" value="${escapeAttr(lead.weight || "")}" /></label>
        <label>Annualized Income<input name="annualized_income_text" required placeholder="$85,000" value="${escapeAttr(lead.annualized_income_text || lead.annual_income || "")}" /></label>
      </div>
      <label>Notes<textarea name="notes" rows="4">${escapeHtml(lead.notes || "Intent confirmed. Budget and state fit verified.")}</textarea></label>
      <label>Appointment<input name="appointment_at" type="datetime-local" value="${defaultLocalDateTime()}" /></label>
      <label class="check-line"><input type="checkbox" name="intent_confirmed" checked /> Intent confirmed</label>
    `;
  }
  if (action === "ready") {
    return `
      <label>Appointment<input name="appointment_at" type="datetime-local" required value="${lead.appointment_at ? toLocalInput(lead.appointment_at) : defaultLocalDateTime()}" /></label>
      <label class="check-line"><input type="checkbox" name="intent_confirmed" checked /> Intent confirmed</label>
    `;
  }
  if (action === "lost") {
    return `<label>Reason<textarea name="lost_reason" rows="3">Not a fit after principal review.</textarea></label>`;
  }
  return `<p>${action.toUpperCase()}</p>`;
}

async function submitAction(event) {
  event.preventDefault();
  const form = new FormData(els.actionForm);
  const leadId = form.get("lead_id");
  const action = form.get("action");
  const payload = Object.fromEntries(form.entries());
  payload.intent_confirmed = form.get("intent_confirmed") === "on";
  let path = `/api/leads/${leadId}`;
  if (action === "assign") path += "/assign";
  if (action === "qualify") path += "/qualify";
  if (action === "ready") path += "/stage/ready";
  if (action === "won" || action === "lost") {
    path += "/stage/result";
    payload.result = action.toUpperCase();
  }
  await api(path, { method: "POST", body: JSON.stringify(payload) });
  els.dialog.close();
  await refresh();
}

els.leadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(els.leadForm);
  const payload = Object.fromEntries(form.entries());
  for (const key of ["healthy", "has_current_coverage", "tcpa_consent"]) payload[key] = form.get(key) === "on";
  for (const key of ["age", "household_size", "annual_income"]) payload[key] = Number(payload[key] || 0);
  els.saveStatus.textContent = "Saving";
  try {
    await api("/api/leads", { method: "POST", body: JSON.stringify(payload) });
    els.saveStatus.textContent = "Saved";
    await refresh();
  } catch (error) {
    els.saveStatus.textContent = error.message;
  }
});

els.dncForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(els.dncForm).entries());
  await api("/api/dnc", { method: "POST", body: JSON.stringify(payload) });
  els.dncForm.reset();
  await refresh();
});

els.optOutForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(els.optOutForm);
  const kind = form.get("kind");
  const value = form.get("value");
  const payload = kind === "email" ? { email: value } : { phone: value };
  await api("/api/opt-out", { method: "POST", body: JSON.stringify(payload) });
  els.optOutForm.reset();
  await refresh();
});

els.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(els.settingsForm).entries());
  await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
  els.settingsForm.dataset.rendered = "false";
  await refresh();
});

els.runOps.addEventListener("click", async () => {
  els.runOps.textContent = "Running";
  await api("/api/automation/run", { method: "POST", body: "{}" });
  els.runOps.textContent = "Run Ops";
  await refresh();
});

els.runTestFlow.addEventListener("click", async () => {
  els.runTestFlow.textContent = "Running";
  try {
    const data = await api("/api/test-run", { method: "POST", body: "{}" });
    await refresh();
    const lead = data.lead;
    state.selectedLeadId = lead.id;
    state.selectedLeadName = lead.name;
    els.threadLeadName.textContent = lead.name;
    await renderThread();
    addAiMessage("assistant", `Test run created: ${lead.name}\nStage: ${lead.stage}\nBooking: ${data.booking_url}`);
  } finally {
    els.runTestFlow.textContent = "Test Run";
  }
});

els.runQualifier.addEventListener("click", async () => {
  els.runQualifier.textContent = "Running";
  try {
    const data = await api("/api/automation/qualifier/run", { method: "POST", body: "{}" });
    addAiMessage("assistant", `Auto qualifier processed ${data.processed} lead(s).`);
    await refresh();
  } finally {
    els.runQualifier.textContent = "Run Qualifier";
  }
});

els.runSetupAutopilot.addEventListener("click", async () => {
  els.runSetupAutopilot.textContent = "Running";
  try {
    const data = await api("/api/automation/setup/run", { method: "POST", body: "{}" });
    const fixed = data.fixed && data.fixed.length ? `Fixed: ${data.fixed.join(", ")}` : "No local settings needed repair.";
    const actions = data.actions && data.actions.length ? `Actions: ${data.actions.length}` : "No lead actions needed.";
    const needs = data.needs && data.needs.length ? `Needs: ${data.needs.join(" ")}` : "No outside setup blockers found.";
    addAiMessage("assistant", `Setup Bot finished.\n${fixed}\n${actions}\n${needs}`);
    await refresh();
  } finally {
    els.runSetupAutopilot.textContent = "Run Setup Bot";
  }
});

els.runBotTeam.addEventListener("click", async () => {
  els.runBotTeam.textContent = "Running";
  try {
    const data = await api("/api/automation/bots/run", { method: "POST", body: "{}" });
    addAiMessage(
      "assistant",
      `Bot Team finished.\nQualifier processed: ${data.qualifier?.processed || 0}\nQualifier follow-ups: ${data.qualifier_followup?.sent || 0}\nBooking follow-ups: ${data.booking_followup?.sent || 0}\nCalendly synced: ${data.calendly?.synced || 0}\nSource insights: ${(data.source_intelligence?.insights || []).length}`
    );
    await refresh();
  } finally {
    els.runBotTeam.textContent = "Run Bot Team";
  }
});

els.runCalendlySync.addEventListener("click", async () => {
  els.runCalendlySync.textContent = "Syncing";
  try {
    const data = await api("/api/calendly/sync", { method: "POST", body: "{}" });
    addAiMessage(
      "assistant",
      `Calendly sync: ${data.status}\nSynced: ${data.synced || 0}\nCreated: ${data.created || 0}\nReady: ${data.ready || 0}${data.detail ? `\n${data.detail}` : ""}`
    );
    await refresh();
  } catch (error) {
    addAiMessage("assistant", `Calendly sync failed: ${error.message}`);
  } finally {
    els.runCalendlySync.textContent = "Sync Calendly";
  }
});

els.aiForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(els.aiForm);
  const message = String(form.get("message") || "").trim();
  if (!message) return;
  addAiMessage("user", message);
  els.aiForm.reset();
  const waiting = addAiMessage("assistant", "Thinking...");
  try {
    const data = await api("/api/ai/chat", { method: "POST", body: JSON.stringify({ message }) });
    waiting.textContent = data.answer || "No answer returned.";
    els.aiProvider.textContent = data.provider === "openai" ? "OpenAI" : "Local";
  } catch (error) {
    waiting.textContent = error.message;
  }
  els.aiMessages.scrollTop = els.aiMessages.scrollHeight;
});

els.threadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selectedLeadId) {
    addAiMessage("assistant", "Select a lead before adding a thread message.");
    return;
  }
  const payload = Object.fromEntries(new FormData(els.threadForm).entries());
  await api(`/api/leads/${state.selectedLeadId}/conversation`, { method: "POST", body: JSON.stringify(payload) });
  els.threadForm.reset();
  await refresh();
  await renderThread();
});

els.markPhoneVerified.addEventListener("click", async () => {
  if (!state.selectedLeadId) return;
  await api(`/api/leads/${state.selectedLeadId}/phone-status`, { method: "POST", body: JSON.stringify({ status: "VERIFIED" }) });
  await refresh();
  await renderThread();
});

els.markWrongNumber.addEventListener("click", async () => {
  if (!state.selectedLeadId) return;
  await api(`/api/leads/${state.selectedLeadId}/phone-status`, { method: "POST", body: JSON.stringify({ status: "WRONG_NUMBER" }) });
  await refresh();
  await renderThread();
});

function addAiMessage(role, text) {
  const node = document.createElement("div");
  node.className = `ai-message ${role}`;
  node.textContent = text;
  els.aiMessages.appendChild(node);
  els.aiMessages.scrollTop = els.aiMessages.scrollHeight;
  return node;
}

async function testIntegration(button) {
  const target = button.dataset.testIntegration;
  const original = button.textContent;
  button.textContent = "Testing";
  try {
    const data = await api(`/api/integrations/${target}/test`, { method: "POST", body: "{}" });
    addAiMessage("assistant", `${target} test: ${data.status}\n${data.detail || ""}`);
    await refresh();
  } catch (error) {
    addAiMessage("assistant", `${target} test failed: ${error.message}`);
  } finally {
    button.textContent = original;
  }
}

document.addEventListener("click", (event) => {
  const actionButton = event.target.closest("[data-lead][data-action]");
  if (actionButton) openActionDialog(actionButton.dataset.lead, actionButton.dataset.action);
  const filterButton = event.target.closest("[data-filter]");
  if (filterButton) {
    document.querySelectorAll("[data-filter]").forEach((button) => button.classList.toggle("active", button === filterButton));
    state.filter = filterButton.dataset.filter;
    renderLeads();
  }
  const tabButton = event.target.closest("[data-tab]");
  if (tabButton) {
    document.querySelectorAll("[data-tab]").forEach((button) => button.classList.toggle("active", button === tabButton));
    state.tab = tabButton.dataset.tab;
    renderActivity();
  }
  const integrationButton = event.target.closest("[data-test-integration]");
  if (integrationButton) testIntegration(integrationButton);
  const readButton = event.target.closest("[data-alert-read]");
  if (readButton) {
    api(`/api/notifications/${readButton.dataset.alertRead}/read`, { method: "POST", body: "{}" }).then(refresh);
  }
});

els.actionForm.addEventListener("submit", submitAction);
els.cancelDialog.addEventListener("click", () => els.dialog.close());

function defaultLocalDateTime() {
  const date = new Date(Date.now() + 24 * 60 * 60 * 1000);
  date.setMinutes(Math.ceil(date.getMinutes() / 15) * 15, 0, 0);
  return toLocalInput(date.toISOString());
}

function toLocalInput(value) {
  const date = new Date(value);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

function canAutoRefresh() {
  const active = document.activeElement;
  const isTyping = active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName);
  return !isTyping && (!els.dialog || !els.dialog.open);
}

refresh().catch((error) => {
  els.saveStatus.textContent = error.message;
});

setInterval(() => {
  if (!canAutoRefresh()) return;
  refresh().catch((error) => {
    if (els.lastUpdated) els.lastUpdated.textContent = error.message;
  });
}, 30000);
