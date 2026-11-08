/* PodcastLens operator dashboard: Cognito code + PKCE, existing pipeline data. */
const CFG = window.PODCASTLENS_CONFIG;
const TOKEN_KEY = "pl.accessToken";
const VERIFIER_KEY = "pl.verifier";
const STATE_KEY = "pl.state";
const $ = id => document.getElementById(id);
const token = () => sessionStorage.getItem(TOKEN_KEY);
let nextCursor = null;
let recordings = [];
let selectedEpisode = null;
let refreshing = false;
let paginationExpanded = false;
let session = 0;

function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function randomString() { return base64url(crypto.getRandomValues(new Uint8Array(48))); }
async function signIn() {
  const verifier = randomString(), state = randomString();
  sessionStorage.setItem(VERIFIER_KEY, verifier);
  sessionStorage.setItem(STATE_KEY, state);
  const challenge = base64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const params = new URLSearchParams({
    response_type: "code", client_id: CFG.clientId, redirect_uri: CFG.redirectUri,
    scope: "openid email profile podcastlens/access", state,
    code_challenge: challenge, code_challenge_method: "S256",
  });
  location.assign(`${CFG.hostedUi}/oauth2/authorize?${params}`);
}
function clearSession() {
  session += 1;
  sessionStorage.removeItem(TOKEN_KEY);
  recordings = []; selectedEpisode = null; nextCursor = null;
  $("episodes").replaceChildren();
  $("transcript-text").textContent = "";
  $("search-results").replaceChildren();
  $("feeds").replaceChildren();
  $("transcript-panel").hidden = true;
  $("app").hidden = true; $("signed-out").hidden = false;
  $("auth-btn").hidden = true; $("budget").hidden = true;
}
function signOut() {
  clearSession();
  const params = new URLSearchParams({client_id: CFG.clientId, logout_uri: CFG.redirectUri});
  location.assign(`${CFG.hostedUi}/logout?${params}`);
}
async function exchangeCode(code, state) {
  const verifier = sessionStorage.getItem(VERIFIER_KEY);
  const expected = sessionStorage.getItem(STATE_KEY);
  sessionStorage.removeItem(STATE_KEY);
  sessionStorage.removeItem(VERIFIER_KEY);
  if (!verifier || !expected || state !== expected) throw new Error("Sign-in could not be verified. Please start again.");
  const resp = await fetch(`${CFG.hostedUi}/oauth2/token`, {
    method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
    body: new URLSearchParams({grant_type: "authorization_code", client_id: CFG.clientId,
      code, redirect_uri: CFG.redirectUri, code_verifier: verifier}),
  });
  if (!resp.ok) throw new Error("Sign-in failed. Please try again.");
  const result = await resp.json();
  if (!result.access_token) throw new Error("Sign-in did not return an access token.");
  sessionStorage.setItem(TOKEN_KEY, result.access_token);
}
function toast(message) {
  $("toast").textContent = message; $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $("toast").hidden = true; }, 6000);
}
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
async function api(path, options = {}) {
  const current = session;
  const resp = await fetch(CFG.apiUrl + path, {...options, headers: {
    "Content-Type": "application/json", Authorization: `Bearer ${token()}`,
  }});
  if (current !== session) throw new Error("Session changed.");
  if (resp.status === 401) { clearSession(); throw new Error("Your session expired. Sign in again."); }
  const result = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(result.error || `Request failed (${resp.status}).`);
  return result;
}
function duration(seconds) {
  const value = Math.max(0, Number(seconds));
  return `${Math.floor(value / 60)}m ${value % 60}s`;
}
async function loadBudget() {
  try {
    const b = await api("/budget");
    $("budget").textContent = `${duration(b.usedSeconds)} / ${duration(b.capSeconds)} reserved this month`;
    $("budget").hidden = false;
  } catch (error) { $("budget").textContent = "Budget unavailable"; $("budget").hidden = false; }
}
function renderRecordings() {
  $("episodes").innerHTML = recordings.length ? recordings.map(e => `
    <article class="episode">
      <h3>${esc(e.title || "Untitled recording")}</h3>
      <span class="badge ${esc(e.status)}">${esc(e.status.replaceAll("_", " "))}</span>
      <p class="muted">${esc(e.source || "")} · ${esc(new Date(e.updatedAt).toLocaleString())}</p>
      ${e.errorCode ? `<p class="muted">${esc(e.errorCode)}</p>` : ""}
      ${e.status === "COMPLETED" ? `<button class="btn ghost open-transcript" data-id="${esc(e.episodeId)}">Read transcript</button>` : ""}
    </article>`).join("") : '<p class="muted">No recordings on this page yet. Check your feeds or load the next page.</p>';
  $("episodes").querySelectorAll(".open-transcript").forEach(button => {
    button.onclick = () => openTranscript(button.dataset.id);
  });
  $("more-btn").hidden = !nextCursor;
}
async function loadRecordings(more = false) {
  const data = await api("/episodes" + (more && nextCursor ? `?cursor=${encodeURIComponent(nextCursor)}` : ""));
  const byId = new Map((more ? recordings : []).map(e => [e.episodeId, e]));
  data.episodes.forEach(e => byId.set(e.episodeId, e));
  recordings = [...byId.values()].sort((a,b) => b.updatedAt.localeCompare(a.updatedAt));
  nextCursor = data.nextCursor;
  paginationExpanded = more;
  $("refresh-note").textContent = more ? "Automatic refresh is paused while browsing older recordings. Select Refresh to return to the first page."
    : "Processing status refreshes every 15 seconds while this page is open.";
  renderRecordings();
}
async function openTranscript(id) {
  selectedEpisode = id;
  $("transcript-panel").hidden = false;
  $("transcript-title").textContent = "Loading transcript…";
  $("transcript-text").textContent = "";
  $("search-results").replaceChildren();
  try {
    const data = await api(`/episodes/${encodeURIComponent(id)}/transcript`);
    if (selectedEpisode !== id) return;
    $("transcript-title").textContent = data.episode.title;
    $("transcript-text").textContent = data.text;
    $("transcript-panel").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) { $("transcript-title").textContent = "Transcript"; toast(error.message); }
}
async function loadFeeds() {
  const data = await api("/feeds");
  $("feeds").innerHTML = data.feeds.length ? data.feeds.map(f => `
    <div class="feed"><span class="feed-meta feed-title">${esc(f.url)}</span>
    <button class="link-btn remove-feed" data-id="${esc(f.feedId)}">Remove</button></div>`).join("")
    : '<p class="muted">No feeds configured yet.</p>';
  $("feeds").querySelectorAll(".remove-feed").forEach(button => {
    button.onclick = async () => {
      if (!confirm("Remove this feed from future checks? Existing recordings will remain.")) return;
      try { await api(`/feeds/${button.dataset.id}`, {method:"DELETE"}); await loadFeeds(); }
      catch(error) { toast(error.message); }
    };
  });
}
async function refresh() {
  if (refreshing || !token()) return;
  refreshing = true; $("refresh-btn").disabled = true;
  try { await Promise.all([loadRecordings(), loadBudget()]); }
  catch(error) { toast(error.message); }
  finally { refreshing = false; $("refresh-btn").disabled = false; }
}
$("feed-form").onsubmit = async event => {
  event.preventDefault();
  const button = event.target.querySelector("button"); button.disabled = true;
  try {
    await api("/feeds", {method:"POST", body:JSON.stringify({
      url:$("feed-input").value.trim(),
      allowedHosts:$("hosts-input").value.split(/[\s,]+/).filter(Boolean),
    })});
    $("feed-form").reset(); await loadFeeds(); toast("Feed saved. Choose Check feeds now to discover recordings.");
  } catch(error) { toast(error.message); }
  finally { button.disabled = false; }
};
$("search-form").onsubmit = async event => {
  event.preventDefault();
  if (!selectedEpisode) return;
  const id = selectedEpisode;
  try {
    const data = await api(`/episodes/${id}/transcript?q=${encodeURIComponent($("search-input").value.trim())}`);
    if (id !== selectedEpisode) return;
    $("search-results").innerHTML = data.matches.length ? data.matches.map(m =>
      `<div class="result"><p>${esc(m.context)}</p></div>`).join("") : '<p class="muted">No matches in this transcript.</p>';
  } catch(error) { toast(error.message); }
};
$("scan-btn").onclick = async () => {
  $("scan-btn").disabled = true;
  try { const data = await api("/scan", {method:"POST", body:"{}"}); toast(data.message); }
  catch(error) { toast(error.message); }
  finally { setTimeout(() => { $("scan-btn").disabled = false; }, 30000); }
};
$("more-btn").onclick = async () => {
  $("more-btn").disabled = true;
  try { await loadRecordings(true); } catch(error) { toast(error.message); }
  finally { $("more-btn").disabled = false; }
};
$("refresh-btn").onclick = refresh;
$("signin-cta").onclick = () => signIn().catch(error => toast(error.message));
$("auth-btn").onclick = signOut;
(async function boot() {
  if (!CFG || !["apiUrl","clientId","redirectUri","hostedUi"].every(k => CFG[k])) {
    $("signin-cta").disabled = true; $("config-error").hidden = false;
    $("config-error").textContent = "This site has not been configured yet."; return;
  }
  const params = new URLSearchParams(location.search);
  try {
    if (params.get("error")) throw new Error("Sign-in was cancelled or could not be completed.");
    if (params.get("code")) await exchangeCode(params.get("code"), params.get("state"));
  } catch(error) { clearSession(); toast(error.message); }
  finally { if (location.search) history.replaceState({}, "", location.pathname); }
  if (token()) {
    $("app").hidden = false; $("signed-out").hidden = true; $("auth-btn").hidden = false;
    await Promise.all([refresh(), loadFeeds().catch(error => toast(error.message))]);
  }
  setInterval(() => { if (!document.hidden && token() && !paginationExpanded) refresh(); }, 15000);
})();
