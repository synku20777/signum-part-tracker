"use strict";

const UI_VERSION = "review-ui-v2";
const FILTER_KEY = "reviewQueueFilters";
const state = {
  token: sessionStorage.getItem("trackerToken") || "",
  parts: [], queue: [], index: -1, progress: null, detail: null, saving: false,
};
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
    try { detail = (await response.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function image(url, alt) {
  const node = element("img");
  node.alt = alt;
  node.loading = "lazy";
  node.referrerPolicy = "no-referrer";
  node.dataset.sourceUrl = url;
  node.src = url;
  node.addEventListener("error", () => {
    node.alt = "Image unavailable";
    node.removeAttribute("src");
  });
  return node;
}

function retryButton(target) {
  const button = element("button", "quiet", "Retry image");
  button.type = "button";
  button.addEventListener("click", () => {
    target.alt = "Loading image";
    target.src = target.dataset.sourceUrl;
  });
  return button;
}

function selectField(name, values, selected = "") {
  const select = element("select");
  select.dataset.field = name;
  const blank = element("option", "", `No ${name}`); blank.value = ""; select.append(blank);
  values.forEach((value) => {
    const option = element("option", "", value); option.value = value; select.append(option);
  });
  select.value = selected || "";
  return select;
}

function fillParts() {
  document.querySelectorAll(".part-options").forEach((select) => {
    const current = select.value;
    while (select.options.length > 1) select.remove(1);
    state.parts.forEach((part) => {
      const option = element("option", "", part.name); option.value = part.id; select.append(option);
    });
    select.value = current;
  });
}

function restoreFilters() {
  try {
    const values = JSON.parse(sessionStorage.getItem(FILTER_KEY) || "{}");
    const form = byId("queue-filters");
    Object.entries(values).forEach(([name, value]) => {
      if (form.elements[name]) form.elements[name].value = value;
    });
  } catch (_) { sessionStorage.removeItem(FILTER_KEY); }
}

function saveFilters() {
  sessionStorage.setItem(
    FILTER_KEY,
    JSON.stringify(Object.fromEntries(new FormData(byId("queue-filters")).entries())),
  );
}

async function loadParts() {
  state.parts = await api("/review/parts");
  fillParts();
  restoreFilters();
}

async function loadProgress() {
  state.progress = await api("/review/progress");
  const data = state.progress;
  const root = byId("campaign-progress"); root.replaceChildren();
  const summary = element("section", "progress-summary");
  summary.append(element("strong", "", `${data.reviewed_listings} / ${data.target_reviews} listings reviewed`));
  const bar = element("progress"); bar.max = Math.max(data.target_reviews, 1); bar.value = Math.min(data.reviewed_listings, data.target_reviews); summary.append(bar);
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
      const form = byId("queue-filters"); form.elements.part_id.value = part.part_id;
      saveFilters(); loadQueue().catch((error) => setStatus(error.message));
    });
    parts.append(card);
  });
  root.append(parts);
}

async function loadQueue() {
  saveFilters();
  const form = new FormData(byId("queue-filters"));
  const params = new URLSearchParams();
  for (const [key, value] of form.entries()) if (value) params.set(key, value);
  setStatus("Loading queue…");
  let result = await api(`/review/queue?${params}`);
  let message = "Ready";
  if (!result.total && form.get("mode") === "matched-high-confidence") {
    byId("queue-filters").elements.mode.value = "matched-low-confidence";
    params.set("mode", "matched-low-confidence");
    result = await api(`/review/queue?${params}`);
    message = "High-confidence queue complete; showing low-confidence matches.";
  }
  if (!result.total && ["matched-high-confidence", "matched-low-confidence"].includes(form.get("mode")) && state.progress?.queue.unreviewed_unmatched) {
    byId("queue-filters").elements.mode.value = "unmatched-broad-candidates";
    params.set("mode", "unmatched-broad-candidates");
    result = await api(`/review/queue?${params}`);
    message = "Matched queues complete; showing unmatched broad candidates.";
  }
  saveFilters();
  state.queue = result.items; state.index = result.items.length ? 0 : -1;
  byId("queue-summary").textContent = `${result.total} listing${result.total === 1 ? "" : "s"}`;
  renderQueue();
  if (state.index >= 0) await openListing(state.index);
  else byId("detail").replaceChildren(element("p", "", "No listings match these filters."));
  setStatus(message);
}

function renderQueue() {
  const queue = byId("queue"); queue.replaceChildren();
  state.queue.forEach((item, index) => {
    const card = element("button", `queue-card${index === state.index ? " active" : ""}`); card.type = "button";
    card.append(item.images.length ? image(item.images[0].source_url, "Listing thumbnail") : element("span", "", "No image"));
    const body = element("span");
    body.append(element("strong", "", item.title));
    body.append(element("small", "", `${formatPrice(item.price, item.currency)} · score ${item.deterministic_match?.total_score ?? "—"}`));
    body.append(element("small", "", `${item.source} · ${item.latest_review?.outcome ?? "unreviewed"}`));
    body.append(element("small", "queue-reason", item.queue_reason));
    card.append(body); card.addEventListener("click", () => openListing(index)); queue.append(card);
  });
}

async function openListing(index) {
  if (index < 0 || index >= state.queue.length) return;
  state.index = index; renderQueue(); setStatus("Loading listing…");
  state.detail = await api(`/review/listings/${state.queue[index].listing_id}`);
  state.detail.queue_mode = state.queue[index].queue_mode;
  state.detail.queue_reason = state.queue[index].queue_reason;
  renderDetail(state.detail); setStatus("Ready");
}

function annotationControls(reference = {}) {
  const controls = element("div", "image-annotations");
  controls.append(
    selectField("view", ["front", "rear", "left", "right", "front-three-quarter", "rear-three-quarter", "top", "underside", "detail", "unknown"], reference.view),
    selectField("context", ["fitted", "removed", "catalogue", "packaging", "unknown"], reference.context),
    selectField("quality", ["good", "usable", "poor"], reference.quality),
    selectField("obstruction", ["none", "partial", "severe"], reference.obstruction),
  );
  return controls;
}

function decisionReasonSelect(outcome, selected) {
  const reasons = {
    confirmed: ["exact-visible-part-number", "visual-shape-match", "catalogue-comparison", "known-donor-car", "other"],
    rejected: ["wrong-part", "wrong-model", "pre-facelift", "replica", "ordinary-OEM-part", "image-does-not-show-part", "listing-no-longer-available", "other"],
    uncertain: ["insufficient-angle", "low-resolution", "obstructed", "conflicting-evidence", "other"],
  };
  const select = element("select"); select.name = "decision_reason";
  const blank = element("option", "", "No decision reason"); blank.value = ""; select.append(blank);
  reasons[outcome].forEach((reason) => { const option = element("option", "", reason); option.value = reason; select.append(option); });
  select.value = selected || "";
  return select;
}

function renderDetail(item) {
  const root = byId("detail"); root.classList.remove("empty"); root.replaceChildren();
  const head = element("div", "detail-head"); const title = element("div");
  title.append(element("p", "eyebrow", `${item.source} · listing ${item.listing_id}${item.is_active ? "" : " · ended/inactive"}`));
  title.append(element("h2", "", item.title));
  title.append(element("p", "", `${formatPrice(item.price, item.currency)} · ${item.condition} · last seen ${formatDate(item.last_seen_at)}`));
  const link = element("a", "", "Open original listing ↗"); link.href = item.url; link.target = "_blank"; link.rel = "noopener noreferrer";
  head.append(title, link); root.append(head);
  root.append(element("p", "queue-reason", item.queue_reason));
  if (item.description) root.append(element("p", "", item.description));
  if (item.deterministic_match) {
    const match = element("section"); match.append(element("h3", "", `Deterministic match: ${item.deterministic_match.part_name}`));
    match.append(element("span", "pill", `Score ${item.deterministic_match.total_score}`));
    match.append(element("span", "pill", item.deterministic_match.compatibility_status));
    item.deterministic_match.reasons.forEach((reason) => match.append(element("span", "pill", `${reason.rule}: ${reason.points}`)));
    root.append(match);
  }
  root.append(element("p", "review-identities", `Human-selected part: ${item.latest_review?.selected_part_id || "none"} · Effective part: ${item.effective_part_id || "none"}`));

  const form = element("form", "review-form"); form.dataset.listingId = item.listing_id;
  const strip = element("div", "image-strip");
  item.images.forEach((listingImage) => {
    const activeReference = item.references.find((reference) => reference.listing_image_id === listingImage.id && reference.is_active);
    const choice = element("div", "image-choice"); const preview = image(listingImage.source_url, "Marketplace listing image");
    choice.append(preview, retryButton(preview));
    const select = element("select"); select.dataset.imageId = listingImage.id;
    [["", "Do not save"], ["positive", "Positive reference"], ["negative", "Negative reference"]].forEach(([value, text]) => {
      const option = element("option", "", text); option.value = value; select.append(option);
    });
    select.addEventListener("change", () => {
      if (activeReference && select.value && activeReference.label !== select.value) {
        setStatus("This image already has the opposite active label for the selected part.");
      }
    });
    choice.append(select, annotationControls(activeReference));
    if (activeReference) choice.append(element("small", "reference-active", `Active ${activeReference.label} reference`));
    if (!listingImage.is_current) choice.append(element("small", "", "Historical image"));
    strip.append(choice);
  });
  form.append(strip);

  const outcomes = element("div", "outcomes");
  [["confirmed", "C · Confirm"], ["rejected", "R · Reject"], ["uncertain", "U · Unsure"]].forEach(([value, text], index) => {
    const label = element("label"); const radio = element("input"); radio.type = "radio"; radio.name = "outcome"; radio.value = value; radio.required = true;
    if (item.latest_review?.outcome === value || (!item.latest_review && index === 2)) radio.checked = true;
    label.append(radio, document.createTextNode(` ${text}`)); outcomes.append(label);
  });
  form.append(outcomes);
  const partLabel = element("label", "", "Target part"); const partSelect = element("select"); partSelect.name = "selected_part_id";
  const empty = element("option", "", "No part selected"); empty.value = ""; partSelect.append(empty);
  state.parts.forEach((part) => { const option = element("option", "", part.name); option.value = part.id; partSelect.append(option); });
  partSelect.value = item.latest_review?.selected_part_id || item.deterministic_match?.part_id || ""; partLabel.append(partSelect); form.append(partLabel);
  const reasonLabel = element("label", "", "Decision reason");
  let reasonSelect = decisionReasonSelect(form.elements.outcome.value, item.latest_review?.decision_reason); reasonLabel.append(reasonSelect); form.append(reasonLabel);
  outcomes.addEventListener("change", () => { const next = decisionReasonSelect(form.elements.outcome.value, ""); reasonSelect.replaceWith(next); reasonSelect = next; });
  const notesLabel = element("label", "", "Review notes"); const notes = element("textarea"); notes.name = "notes"; notes.maxLength = 2000; notes.value = item.latest_review?.notes || ""; notesLabel.append(notes); form.append(notesLabel);
  const privacy = element("label", "privacy-warning"); const privacyCheck = element("input"); privacyCheck.type = "checkbox"; privacyCheck.name = "contact_information_checked";
  privacy.append(privacyCheck, document.createTextNode(" I checked that selected images do not visibly contain phone numbers, email addresses, or seller contact details.")); form.append(privacy);
  const submit = element("button", "", "Save review (Ctrl+Enter)"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", submitReview); root.append(form);

  if (item.review_history.length) {
    const history = element("section"); history.append(element("h3", "", `History (${item.review_history.length})`));
    item.review_history.forEach((review) => history.append(element("p", "", `${formatDate(review.reviewed_at)} · ${review.outcome} · ${review.selected_part_id || "no part"} · ${review.decision_reason || "no reason"}${review.notes ? ` · ${review.notes}` : ""}`)));
    root.append(history);
  }
}

async function submitReview(event) {
  event.preventDefault(); if (state.saving) return;
  state.saving = true; const form = event.currentTarget; const submit = form.querySelector("button[type=submit]"); submit.disabled = true;
  const references = [...form.querySelectorAll(".image-choice")].map((choice) => {
    const label = choice.querySelector("select[data-image-id]");
    if (!label.value) return null;
    const value = { listing_image_id: Number(label.dataset.imageId), label: label.value };
    choice.querySelectorAll(".image-annotations select").forEach((select) => { if (select.value) value[select.dataset.field] = select.value; });
    return value;
  }).filter(Boolean);
  const payload = {
    outcome: form.elements.outcome.value,
    selected_part_id: form.elements.selected_part_id.value || null,
    notes: form.elements.notes.value || null,
    decision_reason: form.elements.decision_reason.value || null,
    review_ui_version: UI_VERSION,
    created_from_queue_mode: byId("queue-filters").elements.mode.value,
    contact_information_checked: form.elements.contact_information_checked.checked,
    references,
  };
  try {
    setStatus("Saving review…");
    const result = await api(`/review/listings/${form.dataset.listingId}`, { method: "POST", body: JSON.stringify(payload) });
    const summary = result.references.map((reference) => reference.status).join(", ");
    const deactivated = result.deactivated_positive_reference_ids.length ? ` · ${result.deactivated_positive_reference_ids.length} stale positive deactivated` : "";
    setStatus(`${summary ? `Saved · references: ${summary}` : "Review saved"}${deactivated}`);
    await loadProgress(); await loadQueue();
  } catch (error) { setStatus(error.message); }
  finally { state.saving = false; submit.disabled = false; }
}

async function authenticatedImage(url, target) {
  target.dataset.sourceUrl = url;
  const response = await fetch(url, { headers: { Authorization: `Bearer ${state.token}` }, cache: "no-store" });
  if (!response.ok) throw new Error("Reference image unavailable");
  const blobUrl = URL.createObjectURL(await response.blob()); target.src = blobUrl;
  target.addEventListener("load", () => URL.revokeObjectURL(blobUrl), { once: true });
}

async function loadReferences() {
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(byId("reference-filters")).entries()) if (value) params.set(key, value);
  setStatus("Loading references…"); const references = await api(`/review/references?${params}`); const root = byId("references"); root.replaceChildren();
  references.forEach((reference) => {
    const card = element("article", "reference-card"); const preview = image("", "Approved reference image"); preview.removeAttribute("src");
    const load = () => authenticatedImage(reference.content_url, preview).catch(() => { preview.alt = "Image unavailable"; }); load();
    const retry = element("button", "quiet", "Retry image"); retry.type = "button"; retry.addEventListener("click", load);
    card.append(preview, retry, element("h3", reference.label, `${reference.part_id} · ${reference.label}`));
    card.append(element("p", "", `${reference.width}×${reference.height} · listing ${reference.listing_id}`));
    const annotations = annotationControls(reference); card.append(annotations);
    const notes = element("textarea"); notes.maxLength = 2000; notes.value = reference.notes || ""; card.append(notes);
    const save = element("button", "", "Save metadata"); save.type = "button";
    save.addEventListener("click", () => {
      const payload = { notes: notes.value || null };
      annotations.querySelectorAll("select").forEach((select) => { payload[select.dataset.field] = select.value || null; });
      updateReference(reference.id, payload);
    });
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
  state.token = token; sessionStorage.setItem("trackerToken", token);
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
    form.querySelector(`[name=outcome][value=${outcomes[event.key.toLowerCase()]}]`).click();
  }
  if (form && event.ctrlKey && event.key === "Enter") form.requestSubmit();
});

if (state.token) { byId("token").value = state.token; unlock(state.token); }
else setStatus("Enter your API token");
