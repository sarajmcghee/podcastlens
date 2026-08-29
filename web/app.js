/* PodcastLens front end.
 *
 * No framework and no build step: the whole app is one file served as static
 * objects from S3 through CloudFront. Sign-in uses the OAuth 2.0 authorization
 * code flow with PKCE against Cognito's hosted UI, which is the correct pattern
 * for a browser app that cannot keep a client secret.
 */

const CFG = window.PODCASTLENS_CONFIG;
const TOKEN_KEY = "pl.idToken";
const VERIFIER_KEY = "pl.verifier";

/* ---------------------------------------------------------------- PKCE ---- */

function randomString(bytes = 48) {
  const buf = crypto.getRandomValues(new Uint8Array(bytes));
  return base64url(buf);
}

function base64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function challengeFor(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(digest);
}

async function signIn() {
  // The verifier stays in this browser tab. Only its SHA-256 hash travels to
  // Cognito, so intercepting the redirect does not let an attacker redeem the
  // authorization code.
  const verifier = randomString();
  sessionStorage.setItem(VERIFIER_KEY, verifier);
  const params = new URLSearchParams({
    response_type: "code",
    client_id: CFG.clientId,
    redirect_uri: CFG.redirectUri,
    scope: "openid email profile",
    code_challenge: await challengeFor(verifier),
    code_challenge_method: "S256",
  });
  location.assign(`${CFG.hostedUi}/oauth2/authorize?${params}`);
}

function signOut() {
  sessionStorage.removeItem(TOKEN_KEY);
  const params = new URLSearchParams({
    client_id: CFG.clientId,
    logout_uri: CFG.redirectUri,
  });
  location.assign(`${CFG.hostedUi}/logout?${params}`);
}

async function exchangeCode(code) {
  const verifier = sessionStorage.getItem(VERIFIER_KEY);
  if (!verifier) throw new Error("missing PKCE verifier - start sign-in again");

  const resp = await fetch(`${CFG.hostedUi}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: CFG.clientId,
      code,
      redirect_uri: CFG.redirectUri,
      code_verifier: verifier,
    }),
  });
  if (!resp.ok) throw new Error(`token exchange failed: ${await resp.text()}`);

  const tokens = await resp.json();
  sessionStorage.removeItem(VERIFIER_KEY);
  // The ID token, not the access token: API Gateway's JWT authorizer checks the
  // `aud` claim against our client id, and only the ID token carries one.
  sessionStorage.setItem(TOKEN_KEY, tokens.id_token);
}

const token = () => sessionStorage.getItem(TOKEN_KEY);

/* ------------------------------------------------------------------ API --- */

async function api(path, options = {}) {
  const resp = await fetch(`${CFG.apiUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: token(),
      ...(options.headers || {}),
    },
  });
  if (resp.status === 401) {
    sessionStorage.removeItem(TOKEN_KEY);
    render();
    throw new Error("session expired - sign in again");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `request failed (${resp.status})`);
  return data;
}

/* ------------------------------------------------------------------- UI --- */

const $ = (id) => document.getElementById(id);
let activeFeedId = null;

function toast(message, ms = 4000) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function render() {
  const signedIn = Boolean(token());
  $("app").hidden = !signedIn;
  $("signed-out").hidden = signedIn;
  $("auth-btn").textContent = signedIn ? "Sign out" : "Sign in";
  $("auth-btn").hidden = !signedIn;
  if (signedIn) { loadFeeds(); loadBudget(); }
}

async function loadBudget() {
  try {
    const b = await api("/budget");
    const used = Math.round(b.usedSeconds / 60);
    const cap = Math.round(b.capSeconds / 60);
    const el = $("budget");
    el.textContent = `Transcribe: ${used}/${cap} min this month`;
    el.hidden = false;
  } catch { /* a missing budget readout is not worth interrupting anyone over */ }
}

async function loadFeeds() {
  const box = $("feeds");
  try {
    const { feeds } = await api("/feeds");
    if (!feeds.length) {
      box.innerHTML = `<p class="muted">No feeds yet. Add one above to get started.</p>`;
      return;
    }
    box.innerHTML = "";
    for (const f of feeds) {
      const row = document.createElement("div");
      row.className = "row";
      row.style.gap = ".25rem";
      row.innerHTML = `
        <button class="feed" aria-current="${f.feedId === activeFeedId}">
          ${f.image ? `<img src="${esc(f.image)}" alt="">` : ""}
          <span class="feed-meta">
            <span class="feed-title">${esc(f.title)}</span>
            <span class="feed-sub">${f.episodeCount || 0} episodes${
              f.lastPolledAt ? ` · checked ${new Date(f.lastPolledAt).toLocaleDateString()}` : " · never checked"
            }</span>
          </span>
        </button>
        <button class="link-btn" title="Remove feed">remove</button>`;
      row.querySelector(".feed").onclick = () => selectFeed(f);
      row.querySelector(".link-btn").onclick = () => removeFeed(f);
      box.appendChild(row);
    }
  } catch (err) {
    box.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

async function selectFeed(feed) {
  activeFeedId = feed.feedId;
  $("episodes-title").textContent = `Episodes — ${feed.title}`;
  $("episodes").innerHTML = `<p class="muted">Loading…</p>`;
  loadFeeds();
  try {
    const { episodes } = await api(`/feeds/${feed.feedId}/episodes`);
    if (!episodes.length) {
      $("episodes").innerHTML =
        `<p class="muted">Nothing ingested yet. The poller runs on a schedule — or trigger it now with <code>./scripts/poll-now.sh</code>.</p>`;
      return;
    }
    $("episodes").innerHTML = episodes.map((e) => `
      <article class="episode">
        <h3>${esc(e.title)}</h3>
        <span class="badge ${esc(e.status)}">${esc(e.status)}</span>
        <span class="muted" style="font-size:.78rem">
          ${new Date(e.publishedAt).toLocaleDateString()}
          ${e.wordCount ? ` · ${e.wordCount} words transcribed` : ""}
        </span>
        ${(e.topics || []).length
          ? `<div class="tags">${e.topics.slice(0, 12).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>`
          : ""}
      </article>`).join("");
  } catch (err) {
    $("episodes").innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

async function removeFeed(feed) {
  if (!confirm(`Remove "${feed.title}"?`)) return;
  try {
    await api(`/feeds/${feed.feedId}`, { method: "DELETE" });
    if (activeFeedId === feed.feedId) {
      activeFeedId = null;
      $("episodes").innerHTML = `<p class="muted">Pick a feed above.</p>`;
    }
    loadFeeds();
  } catch (err) { toast(err.message); }
}

/* -------------------------------------------------------------- events --- */

$("feed-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = $("feed-input");
  const btn = e.target.querySelector("button");
  btn.disabled = true;
  btn.textContent = "Reading feed…";
  try {
    const { feed } = await api("/feeds", {
      method: "POST",
      body: JSON.stringify({ url: input.value.trim() }),
    });
    input.value = "";
    toast(`Added "${feed.title}".`);
    loadFeeds();
  } catch (err) { toast(err.message, 6000); }
  finally { btn.disabled = false; btn.textContent = "Add feed"; }
};

$("search-form").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("search-input").value.trim();
  const box = $("search-results");
  box.innerHTML = `<p class="muted">Searching…</p>`;
  try {
    const data = await api(`/search?q=${encodeURIComponent(q)}`);
    box.innerHTML = data.results.length
      ? data.results.map((r) => `
          <div class="result">
            <h3>${esc(r.episodeTitle)}</h3>
            <p>${esc(r.snippet) || "<em>no transcript excerpt</em>"}</p>
          </div>`).join("")
      : `<p class="muted">No episodes mention “${esc(q)}” yet.</p>`;
  } catch (err) {
    box.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
};

$("auth-btn").onclick = () => (token() ? signOut() : signIn());
$("signin-cta").onclick = signIn;

/* ---------------------------------------------------------------- boot --- */

(async function boot() {
  const params = new URLSearchParams(location.search);
  if (params.get("code")) {
    try {
      await exchangeCode(params.get("code"));
    } catch (err) {
      toast(err.message, 8000);
    }
    // Strip the code from the URL so a refresh does not try to redeem it twice.
    history.replaceState({}, "", location.pathname);
  } else if (params.get("error")) {
    toast(`${params.get("error")}: ${params.get("error_description") || ""}`, 8000);
    history.replaceState({}, "", location.pathname);
  }
  render();
})();
