const loginScreen = document.querySelector("#login-screen");
const appShell = document.querySelector("#app-shell");
const loginForm = document.querySelector("#login-form");
const loginError = document.querySelector("#login-error");
const triageForm = document.querySelector("#triage-form");
const queryInput = document.querySelector("#query");
const companyInput = document.querySelector("#company");
const characterCount = document.querySelector("#character-count");
const submitButton = document.querySelector("#submit-button");
const loadingCard = document.querySelector("#loading-card");
const resultCard = document.querySelector("#result-card");
const errorBanner = document.querySelector("#error-banner");
const errorMessage = document.querySelector("#error-message");

let currentUser = null;
let agents = [];
let searchTimer = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok) {
    const ticketNote = result.ticket_id ? ` Ticket #${result.ticket_id} was saved for follow-up.` : "";
    const error = new Error(`${result.error || "The request could not be completed."}${ticketNote}`);
    error.status = response.status;
    throw error;
  }
  return result;
}

function showLogin() {
  currentUser = null;
  appShell.hidden = true;
  loginScreen.hidden = false;
  document.querySelector("#login-username").focus();
}

function showApp(user) {
  currentUser = user;
  loginScreen.hidden = true;
  appShell.hidden = false;
  document.querySelector("#account-name").textContent = user.username;
  document.querySelector("#account-role").textContent = user.role;
  document.querySelector("#nav-triage").hidden = user.role === "viewer";
  document.querySelector("#nav-users").hidden = user.role !== "admin";
  document.querySelectorAll('.page-heading > [data-page="triage"]').forEach((button) => {
    button.hidden = user.role === "viewer";
  });
  navigate("dashboard");
  refreshDashboard();
}

function showError(message) {
  errorMessage.textContent = message;
  errorBanner.hidden = false;
}

function setLoading(isLoading) {
  submitButton.disabled = isLoading;
  submitButton.querySelector(".button-label").textContent = isLoading ? "Triaging..." : "Run triage";
  loadingCard.hidden = !isLoading;
  if (isLoading) {
    resultCard.hidden = true;
    errorBanner.hidden = true;
  }
}

function navigate(page) {
  if (page === "triage" && currentUser.role === "viewer") page = "dashboard";
  if (page === "users" && currentUser.role !== "admin") page = "dashboard";
  document.querySelectorAll(".page-view").forEach((view) => {
    view.hidden = view.id !== `${page}-page`;
  });
  document.querySelectorAll(".sidebar .nav-item").forEach((item) => {
    const active = item.dataset.page === page;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  document.querySelector("#page-title").textContent =
    page === "users" ? "Team & roles" : page === "triage" ? "New triage" : "Dashboard";
  if (page === "users") refreshUsers();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function addBadge(container, label, value, style = "") {
  const badge = document.createElement("div");
  badge.className = `result-badge ${style}`.trim();
  const name = document.createElement("span");
  name.className = "badge-label";
  name.textContent = label;
  const content = document.createElement("strong");
  content.textContent = String(value || "").replaceAll("_", " ");
  badge.append(name, content);
  container.append(badge);
}

function renderResult(result, query) {
  const meta = document.querySelector("#result-meta");
  meta.replaceChildren();
  const escalated = result.status === "escalated";
  addBadge(meta, "ROUTING", escalated ? "Human review" : "Response ready", escalated ? "badge-escalated" : "badge-replied");
  addBadge(meta, "PRODUCT AREA", result.product_area || "general_support");
  addBadge(meta, "REQUEST TYPE", result.request_type || "product_issue");
  if (result.urgency) addBadge(meta, "URGENCY", result.urgency, `badge-urgency urgency-${result.urgency}`);
  document.querySelector("#result-ticket-id").textContent = `#${result.ticket_id}`;
  document.querySelector("#response-text").textContent = result.response;
  document.querySelector("#justification-text").textContent = result.justification;
  document.querySelector("#query-review-text").textContent = query;
  resultCard.hidden = false;
  resultCard.scrollIntoView({ behavior: "smooth", block: "start" });
}

function makeCell(row, value, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value == null ? "" : String(value);
  row.append(cell);
  return cell;
}

function makePill(value) {
  const pill = document.createElement("span");
  pill.className = `pill pill-${String(value).replaceAll("_", "-")}`;
  pill.textContent = String(value || "").replaceAll("_", " ");
  return pill;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function renderDashboard(dashboard, tickets) {
  const stats = [
    ["Total queries", dashboard.total, "All recorded tickets"],
    ["Successfully triaged", dashboard.triaged, "Replied or escalated"],
    ["Escalated", dashboard.escalated, "Flagged for human review"],
    ["Unresolved", dashboard.unresolved, "Still needs follow-up"],
  ];
  const statsGrid = document.querySelector("#stats-grid");
  statsGrid.replaceChildren();
  stats.forEach(([label, value, note]) => {
    const card = document.createElement("article");
    card.className = "stat-card";
    const heading = document.createElement("span");
    heading.textContent = label;
    const number = document.createElement("strong");
    number.textContent = value.toLocaleString();
    const caption = document.createElement("small");
    caption.textContent = note;
    card.append(heading, number, caption);
    statsGrid.append(card);
  });

  const companyStats = document.querySelector("#company-stats");
  companyStats.replaceChildren();
  if (dashboard.companies.length === 0) {
    const row = companyStats.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5;
    cell.textContent = "No queries recorded yet.";
  } else {
    dashboard.companies.forEach((item) => {
      const row = companyStats.insertRow();
      makeCell(row, item.company, "company-name");
      makeCell(row, item.total);
      makeCell(row, item.triaged);
      makeCell(row, item.escalated);
      makeCell(row, item.resolved);
    });
  }
  renderTickets(tickets);
}

function renderTickets(tickets) {
  const rows = document.querySelector("#ticket-rows");
  rows.replaceChildren();
  const canEdit = currentUser.role !== "viewer";
  const canAssign = ["admin", "supervisor"].includes(currentUser.role);
  tickets.forEach((ticket) => {
    const row = rows.insertRow();
    const ticketCell = row.insertCell();
    const ticketId = document.createElement("span");
    ticketId.className = "ticket-id";
    ticketId.textContent = `#${ticket.id}`;
    const queryPreview = document.createElement("div");
    queryPreview.className = "ticket-query";
    queryPreview.textContent = ticket.query;
    const details = document.createElement("details");
    details.className = "ticket-details";
    details.dataset.loaded = "true";
    const summary = document.createElement("summary");
    summary.textContent = "View details";
    const detailBody = document.createElement("div");
    const queryLabel = document.createElement("strong");
    queryLabel.textContent = "Customer query";
    const queryText = document.createElement("span");
    queryText.textContent = ticket.query;
    const responseLabel = document.createElement("strong");
    responseLabel.textContent = "Suggested response";
    const responseText = document.createElement("span");
    responseText.textContent = ticket.response || ticket.justification || "No response is available.";
    detailBody.append(queryLabel, queryText, responseLabel, responseText);
    details.append(summary, detailBody);
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.eventsLoaded) return;
      details.dataset.eventsLoaded = "true";
      try {
        const detail = await api(`/api/tickets/${ticket.id}`);
        const eventsLabel = document.createElement("strong");
        eventsLabel.textContent = "Activity";
        const events = document.createElement("ul");
        events.className = "ticket-events";
        detail.ticket.events.forEach((event) => {
          const item = document.createElement("li");
          item.textContent = `${formatDate(event.created_at)} · ${event.actor || "System"} · ${event.details}`;
          events.append(item);
        });
        detailBody.append(eventsLabel, events);
      } catch (error) {
        details.dataset.eventsLoaded = "";
        showError(error.message);
      }
    });
    ticketCell.append(ticketId, queryPreview, details);

    makeCell(row, ticket.company);
    const outcomeCell = row.insertCell();
    outcomeCell.append(makePill(ticket.triage_status));
    const workflowCell = row.insertCell();
    if (canEdit) {
      const select = document.createElement("select");
      select.className = "workflow-select";
      select.setAttribute("aria-label", `Work status for ticket ${ticket.id}`);
      [["open", "Open"], ["in_progress", "In progress"], ["resolved", "Resolved"]].forEach(([value, label]) => {
        const option = new Option(label, value);
        option.selected = ticket.workflow_status === value;
        select.add(option);
      });
      select.addEventListener("change", () => updateTicket(ticket, { workflow_status: select.value }));
      workflowCell.append(select);
    } else {
      workflowCell.append(makePill(ticket.workflow_status));
    }

    const assigneeCell = row.insertCell();
    if (canAssign) {
      const select = document.createElement("select");
      select.className = "assignee-select";
      select.setAttribute("aria-label", `Assignee for ticket ${ticket.id}`);
      select.add(new Option("Unassigned", ""));
      agents.forEach((agent) => {
        const option = new Option(agent.username, String(agent.id));
        option.selected = ticket.assigned_to_id === agent.id;
        select.add(option);
      });
      select.addEventListener("change", () => updateTicket(ticket, {
        workflow_status: ticket.workflow_status,
        assigned_to: select.value ? Number(select.value) : null,
      }));
      assigneeCell.append(select);
    } else {
      assigneeCell.textContent = ticket.assigned_to || "Unassigned";
    }
    makeCell(row, formatDate(ticket.created_at));
    makeCell(row, "");
  });
  document.querySelector("#tickets-empty").hidden = tickets.length > 0;
}

async function refreshDashboard() {
  try {
    const params = new URLSearchParams();
    const search = document.querySelector("#ticket-search").value.trim();
    const company = document.querySelector("#company-filter").value;
    const workflow = document.querySelector("#workflow-filter").value;
    if (search) params.set("search", search);
    if (company) params.set("company", company);
    if (workflow) params.set("workflow_status", workflow);
    const [dashboard, ticketData] = await Promise.all([
      api("/api/dashboard"),
      api(`/api/tickets?${params.toString()}`),
    ]);
    if (["admin", "supervisor"].includes(currentUser.role)) {
      const agentData = await api("/api/agents");
      agents = agentData.agents;
    } else {
      agents = [];
    }
    renderDashboard(dashboard, ticketData.tickets);
  } catch (error) {
    if (error.status === 401) showLogin();
    else showError(error.message);
  }
}

async function updateTicket(ticket, changes) {
  try {
    await api(`/api/tickets/${ticket.id}`, { method: "PATCH", body: JSON.stringify(changes) });
    await refreshDashboard();
  } catch (error) {
    showError(error.message);
    await refreshDashboard();
  }
}

async function refreshUsers() {
  try {
    const result = await api("/api/users");
    renderUsers(result.users);
  } catch (error) {
    if (error.status === 401) showLogin();
    else showError(error.message);
  }
}

function renderUsers(users) {
  const rows = document.querySelector("#user-rows");
  rows.replaceChildren();
  users.forEach((user) => {
    const row = rows.insertRow();
    makeCell(row, user.username);
    const roleCell = row.insertCell();
    if (user.id === currentUser.id) {
      roleCell.textContent = user.role;
    } else {
      const select = document.createElement("select");
      select.className = "workflow-select";
      select.setAttribute("aria-label", `Role for ${user.username}`);
      ["admin", "supervisor", "agent", "viewer"].forEach((role) => {
        const option = new Option(role, role);
        option.selected = user.role === role;
        select.add(option);
      });
      select.addEventListener("change", async () => {
        try {
          await api(`/api/users/${user.id}/role`, {
            method: "PATCH",
            body: JSON.stringify({ role: select.value }),
          });
          await refreshUsers();
        } catch (error) {
          document.querySelector("#user-feedback").textContent = error.message;
          await refreshUsers();
        }
      });
      roleCell.append(select);
    }
    const status = row.insertCell();
    status.append(makePill(user.active ? "active" : "inactive"));
    makeCell(row, formatDate(user.created_at));
    const action = row.insertCell();
    if (user.id !== currentUser.id) {
      const button = document.createElement("button");
      button.className = "text-button";
      button.textContent = user.active ? "Deactivate" : "Reactivate";
      button.addEventListener("click", async () => {
        try {
          await api(`/api/users/${user.id}/active`, {
            method: "POST",
            body: JSON.stringify({ active: !user.active }),
          });
          await refreshUsers();
        } catch (error) {
          document.querySelector("#user-feedback").textContent = error.message;
        }
      });
      action.append(button);
    }
  });
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.hidden = true;
  const data = new FormData(loginForm);
  try {
    const result = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username: data.get("username"), password: data.get("password") }),
    });
    loginForm.reset();
    showApp(result.user);
  } catch (error) {
    loginError.textContent = error.message;
    loginError.hidden = false;
  }
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST", body: "{}" });
  } finally {
    showLogin();
  }
});

document.querySelectorAll(".sidebar .nav-item, [data-page='triage'].page-heading, .page-heading > button[data-page]").forEach((item) => {
  item.addEventListener("click", () => navigate(item.dataset.page));
});

triageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query) {
    queryInput.focus();
    showError("Enter a customer message before running triage.");
    return;
  }
  setLoading(true);
  try {
    const result = await api("/api/triage", {
      method: "POST",
      body: JSON.stringify({ query, company: companyInput.value }),
    });
    renderResult(result, query);
    await refreshDashboard();
  } catch (error) {
    showError(error.message);
    await refreshDashboard();
  } finally {
    setLoading(false);
  }
});

document.querySelector("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = new FormData(form);
  const feedback = document.querySelector("#user-feedback");
  feedback.textContent = "";
  try {
    await api("/api/users", {
      method: "POST",
      body: JSON.stringify({
        username: values.get("username"),
        password: values.get("password"),
        role: values.get("role"),
      }),
    });
    form.reset();
    feedback.textContent = "Account created.";
    await refreshUsers();
  } catch (error) {
    feedback.textContent = error.message;
  }
});

queryInput.addEventListener("input", () => {
  characterCount.textContent = `${queryInput.value.length.toLocaleString()} / 4,000`;
});
document.querySelectorAll(".example-chip").forEach((button) => {
  button.addEventListener("click", () => {
    queryInput.value = button.dataset.query;
    companyInput.value = button.dataset.company;
    characterCount.textContent = `${queryInput.value.length.toLocaleString()} / 4,000`;
    queryInput.focus();
  });
});
document.querySelector("#dismiss-error").addEventListener("click", () => { errorBanner.hidden = true; });
document.querySelector("#refresh-button").addEventListener("click", refreshDashboard);
document.querySelector("#open-dashboard").addEventListener("click", () => navigate("dashboard"));
document.querySelector("#ticket-search").addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(refreshDashboard, 250);
});
document.querySelector("#company-filter").addEventListener("change", refreshDashboard);
document.querySelector("#workflow-filter").addEventListener("change", refreshDashboard);
document.querySelector("#copy-response").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  try {
    await navigator.clipboard.writeText(document.querySelector("#response-text").textContent);
    button.textContent = "Copied";
    window.setTimeout(() => { button.textContent = "Copy response"; }, 1600);
  } catch {
    showError("Could not copy the response. Select and copy the text instead.");
  }
});

api("/api/session")
  .then((session) => session.authenticated ? showApp(session.user) : showLogin())
  .catch(() => showLogin());
