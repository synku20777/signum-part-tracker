"use strict";

const state = { token: sessionStorage.getItem("trackerToken") || "", parts: [], queue: [], index: -1, progress: null };
const byId = (id) => document.getElementById(id);
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const setStatus = (message) => { byId("status").textContent = message; };
const formatPrice = (value, currency) => value == null ? "Price unavailable" : `${value} ${currency}`;
const formatDate = (value) => value ? new Date(value).toLocaleString() : "Unknown";

async function api(path, options = {}) {
  if (!state.token) throw new Error("Enter the API token to load review data.");
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* response is not JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function image(url, alt) {
  const node = element("img");
  node.alt = alt;
  node.loading = "lazy";
  node.referrerPolicy = "no-referrer";
  node.src = url;
  node.addEventListener("error", () => { node.alt = "Image unavailable"; node.removeAttribute("src"); });
  return node;
}

function fillParts() {
  document.querySelectorAll(".part-options").forEach((select) => {
    const current = select.value;
    while (select.options.length > 1) select.remove(1);
    state.parts.forEach((part) => {
      const option = element("option", "", part.name);
      option.value = part.id;
      select.append(option);
    });
    select.value = current;
  });
}

async function loadParts() {
  state.parts = await api("/review/parts");
  fillParts();
}

async function loadProgress() {
  state.progress = await api("/review/progress");
  const data = state.progress;
  const root = byId("campaign-progress"); root.replaceChildren();
  const summary = element("section", "progress-summary");
  summary.append(element("strong", "", `${data.reviewed_listings} / ${data.target_reviews} listings reviewed`));
  const bar = element("progress"); bar.max = data.target_reviews; bar.value = Math.min(data.reviewed_listings, data.target_reviews); summary.append(bar);
  summary.append(element("small", "", `${data.outcomes.confirmed} confirmed · ${data.outcomes.rejected} rejected · ${data.outcomes.uncertain} uncertain`));
  summary.append(element("small", "", `${data.queue.unreviewed_matched} matched · ${data.queue.unreviewed_unmatched} broad candidates left`));
  summary.append(element("small", "", data.sources.map((source) => `${source.source}: ${source.reviewed_listings}`).join(" · ") || "No reviews yet"));
  root.append(summary);
  const parts = element("div", "progress-parts");
  data.parts.forEach((part) => {
    const card = element("button", `part-progress${part.coverage_ready ? " ready" : ""}`); card.type = "button";
    card.append(element("strong", "", part.part_name));
    card.append(element("small", "", `${part.confirmed_listings} confirmed · ${part.positive_references} positive · ${part.negative_references} negative`));
    card.append(element("small", "", part.coverage_ready ? "Coverage ready" : part.missing_requirements.join("; ")));
    card.addEventListener("click", () => {
      const form = byId("queue-filters"); form.elements.part_id.value = part.part_id; form.elements.match_state.value = "all";
      loadQueue().catch((error) => setStatus(error.message));
    });
    parts.append(card);
  });
  root.append(parts);
}

async function loadQueue() {
  const form = new FormData(byId("queue-filters"));
  const params = new URLSearchParams();
  for (const [key, value] of form.entries()) if (value) params.set(key, value);
  setStatus("Loading queue…");
  let result = await api(`/review/queue?${params}`);
  let readyMessage = "Ready";
  if (result.total === 0 && form.get("status") === "unreviewed" && form.get("match_state") === "matched" && state.progress && !state.progress.campaign_complete && state.progress.queue.unreviewed_unmatched > 0) {
    byId("queue-filters").elements.match_state.value = "unmatched";
    params.set("match_state", "unmatched");
    result = await api(`/review/queue?${params}`);
    readyMessage = "Matched queue complete; showing broad candidates.";
  }
  state.queue = result.items;
  state.index = result.items.length ? 0 : -1;
  byId("queue-summary").textContent = `${result.total} listing${result.total === 1 ? "" : "s"}`;
  renderQueue();
  if (state.index >= 0) await openListing(state.index);
  else { byId("detail").replaceChildren(element("p", "", "No listings match these filters.")); }
  setStatus(readyMessage);
}

function renderQueue() {
  const queue = byId("queue");
  queue.replaceChildren();
  state.queue.forEach((item, index) => {
    const card = element("button", `queue-card${index === state.index ? " active" : ""}`);
    card.type = "button";
    card.append(item.images.length ? image(item.images[0].source_url, "Listing thumbnail") : element("span", "", "No image"));
    const body = element("span");
    body.append(element("strong", "", item.title));
    body.append(element("small", "", `${formatPrice(item.price, item.currency)} · score ${item.deterministic_match?.total_score ?? "—"}`));
    body.append(element("small", "", `${item.source} · ${item.latest_review?.outcome ?? "unreviewed"}`));
    card.append(body);
    card.addEventListener("click", () => openListing(index));
    queue.append(card);
  });
}

async function openListing(index) {
  if (index < 0 || index >= state.queue.length) return;
  state.index = index;
  renderQueue();
  setStatus("Loading listing…");
  const detail = await api(`/review/listings/${state.queue[index].listing_id}`);
  renderDetail(detail);
  setStatus("Ready");
}

function renderDetail(item) {
  const root = byId("detail");
  root.classList.remove("empty");
  root.replaceChildren();
  const head = element("div", "detail-head");
  const title = element("div");
  title.append(element("p", "eyebrow", `${item.source} · listing ${item.listing_id}`));
  title.append(element("h2", "", item.title));
  title.append(element("p", "", `${formatPrice(item.price, item.currency)} · ${item.condition} · last seen ${formatDate(item.last_seen_at)}`));
  const link = element("a", "", "Open marketplace listing ↗");
  link.href = item.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  head.append(title, link);
  root.append(head);
  if (item.description) root.append(element("p", "", item.description));
  if (item.deterministic_match) {
    const match = element("section");
    match.append(element("h3", "", `Deterministic match: ${item.deterministic_match.part_name}`));
    match.append(element("span", "pill", `Score ${item.deterministic_match.total_score}`));
    match.append(element("span", "pill", item.deterministic_match.compatibility_status));
    item.deterministic_match.reasons.forEach((reason) => match.append(element("span", "pill", `${reason.rule}: ${reason.points}`)));
    root.append(match);
  }

  const form = element("form", "review-form");
  form.dataset.listingId = item.listing_id;
  const strip = element("div", "image-strip");
  item.images.forEach((listingImage) => {
    const choice = element("label", "image-choice");
    choice.append(image(listingImage.source_url, "Marketplace listing image"));
    const select = element("select");
    select.dataset.imageId = listingImage.id;
    [["", "Do not save"], ["positive", "Positive reference"], ["negative", "Negative reference"]].forEach(([value, text]) => {
      const option = element("option", "", text); option.value = value; select.append(option);
    });
    choice.append(select);
    if (!listingImage.is_current) choice.append(element("small", "", "Historical image"));
    strip.append(choice);
  });
  form.append(strip);
  const outcomes = element("div", "outcomes");
  [["confirmed", "C · Confirm"], ["rejected", "R · Reject"], ["uncertain", "U · Unsure"]].forEach(([value, text], index) => {
    const label = element("label");
    const radio = element("input"); radio.type = "radio"; radio.name = "outcome"; radio.value = value; radio.required = true;
    if (item.latest_review?.outcome === value || (!item.latest_review && index === 2)) radio.checked = true;
    label.append(radio, document.createTextNode(` ${text}`)); outcomes.append(label);
  });
  form.append(outcomes);
  const partLabel = element("label", "", "Target part");
  const partSelect = element("select"); partSelect.name = "selected_part_id";
  const empty = element("option", "", "No part selected"); empty.value = ""; partSelect.append(empty);
  state.parts.forEach((part) => { const option = element("option", "", part.name); option.value = part.id; partSelect.append(option); });
  partSelect.value = item.latest_review?.selected_part_id || item.deterministic_match?.part_id || "";
  partLabel.append(partSelect); form.append(partLabel);
  const notesLabel = element("label", "", "Review notes");
  const notes = element("textarea"); notes.name = "notes"; notes.maxLength = 2000; notes.value = item.latest_review?.notes || "";
  notesLabel.append(notes); form.append(notesLabel);
  const submit = element("button", "", "Save review (Ctrl+Enter)"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", submitReview);
  root.append(form);

  if (item.review_history.length) {
    const history = element("section"); history.append(element("h3", "", `History (${item.review_history.length})`));
    item.review_history.forEach((review) => history.append(element("p", "", `${formatDate(review.reviewed_at)} · ${review.outcome} · ${review.selected_part_id || "no part"}${review.notes ? ` · ${review.notes}` : ""}`)));
    root.append(history);
  }
}

async function submitReview(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  const references = [...form.querySelectorAll(".image-choice select")]
    .filter((select) => select.value)
    .map((select) => ({ listing_image_id: Number(select.dataset.imageId), label: select.value }));
  const payload = {
    outcome: form.elements.outcome.value,
    selected_part_id: form.elements.selected_part_id.value || null,
    notes: form.elements.notes.value || null,
    references,
  };
  try {
    setStatus("Saving review…");
    const result = await api(`/review/listings/${form.dataset.listingId}`, { method: "POST", body: JSON.stringify(payload) });
    const summary = result.references.map((reference) => reference.status).join(", ");
    setStatus(summary ? `Saved · references: ${summary}` : "Review saved");
    await loadProgress(); await loadQueue();
  } catch (error) { setStatus(error.message); }
  finally { submit.disabled = false; }
}

async function authenticatedImage(url, target) {
  const response = await fetch(url, { headers: { Authorization: `Bearer ${state.token}` }, cache: "no-store" });
  if (!response.ok) throw new Error("Reference image unavailable");
  const blobUrl = URL.createObjectURL(await response.blob());
  target.src = blobUrl;
  target.addEventListener("load", () => URL.revokeObjectURL(blobUrl), { once: true });
}

async function loadReferences() {
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(byId("reference-filters")).entries()) if (value) params.set(key, value);
  setStatus("Loading references…");
  const references = await api(`/review/references?${params}`);
  const root = byId("references"); root.replaceChildren();
  references.forEach((reference) => {
    const card = element("article", "reference-card");
    const preview = image("", "Approved reference image"); preview.removeAttribute("src");
    authenticatedImage(reference.content_url, preview).catch(() => { preview.alt = "Image unavailable"; });
    card.append(preview, element("h3", reference.label, `${reference.part_id} · ${reference.label}`));
    card.append(element("p", "", `${reference.width}×${reference.height} · listing ${reference.listing_id}`));
    const notes = element("textarea"); notes.maxLength = 2000; notes.value = reference.notes || ""; card.append(notes);
    const save = element("button", "", "Save note"); save.type = "button";
    save.addEventListener("click", () => updateReference(reference.id, { notes: notes.value || null }));
    const toggle = element("button", "quiet", reference.is_active ? "Deactivate" : "Activate"); toggle.type = "button";
    toggle.addEventListener("click", () => updateReference(reference.id, { is_active: !reference.is_active }));
    card.append(save, document.createTextNode(" "), toggle); root.append(card);
  });
  setStatus(`${references.length} reference${references.length === 1 ? "" : "s"}`);
}

async function updateReference(id, payload) {
  try { await api(`/review/references/${id}`, { method: "PATCH", body: JSON.stringify(payload) }); await loadReferences(); }
  catch (error) { setStatus(error.message); }
}

async function unlock(token) {
  state.token = token;
  sessionStorage.setItem("trackerToken", token);
  try { await loadParts(); await loadProgress(); await loadQueue(); }
  catch (error) { setStatus(error.message); }
}

byId("token-form").addEventListener("submit", (event) => { event.preventDefault(); unlock(byId("token").value); });
byId("forget-token").addEventListener("click", () => { sessionStorage.removeItem("trackerToken"); state.token = ""; byId("token").value = ""; setStatus("Token removed"); });
byId("queue-filters").addEventListener("submit", (event) => { event.preventDefault(); loadQueue().catch((error) => setStatus(error.message)); });
byId("reference-filters").addEventListener("submit", (event) => { event.preventDefault(); loadReferences().catch((error) => setStatus(error.message)); });
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab, .panel").forEach((node) => node.classList.remove("active"));
  tab.classList.add("active"); byId(tab.dataset.panel).classList.add("active");
  if (tab.dataset.panel === "references-panel") loadReferences().catch((error) => setStatus(error.message));
}));
document.addEventListener("keydown", (event) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
  const form = byId("detail").querySelector("form");
  if (event.key === "ArrowDown" || event.key === "ArrowRight") openListing(Math.min(state.index + 1, state.queue.length - 1));
  if (event.key === "ArrowUp" || event.key === "ArrowLeft") openListing(Math.max(state.index - 1, 0));
  if (form && ["c", "r", "u"].includes(event.key.toLowerCase())) {
    const outcomes = { c: "confirmed", r: "rejected", u: "uncertain" };
    form.querySelector(`[name=outcome][value=${outcomes[event.key.toLowerCase()]}]`).checked = true;
  }
});
document.addEventListener("keydown", (event) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
  if (event.ctrlKey && event.key === "Enter") byId("detail").querySelector("form")?.requestSubmit();
});

if (state.token) { byId("token").value = state.token; unlock(state.token); }
else setStatus("Enter your API token");
