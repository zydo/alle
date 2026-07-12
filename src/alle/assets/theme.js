// Manual appearance toggle. theme-init.js has already set the initial `dark`
// class before paint; this module wires the button and keeps following the OS
// while the user hasn't pinned an explicit choice. The whole swap is one class
// on <html>, so the dark token block in style.css lives in a single place.

const KEY = "alle-theme";
const root = document.documentElement;
const mq = window.matchMedia("(prefers-color-scheme: dark)");

function saved() {
  try {
    return localStorage.getItem(KEY);
  } catch (e) {
    // localStorage throws in private mode / when blocked — degrade to "no choice"
    console.debug("alle: appearance preference unavailable", e);
    return null;
  }
}

export function resolvedTheme() {
  const s = saved();
  if (s === "light" || s === "dark") return s;
  return mq.matches ? "dark" : "light";
}

function apply(t) {
  root.classList.toggle("dark", t === "dark");
}

export function setTheme(t) {
  try {
    localStorage.setItem(KEY, t);
  } catch (e) {
    // quota exceeded / storage blocked — the choice won't persist this session
    console.debug("alle: could not save appearance preference", e);
  }
  apply(t);
}

// While unpinned, track the OS live so changing the system appearance updates
// the page without a reload.
export function followSystem() {
  mq.addEventListener("change", () => {
    if (!saved()) apply(mq.matches ? "dark" : "light");
  });
}

const MOON = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.2 9.5A5.2 5.2 0 1 1 6.5 2.8 4.2 4.2 0 0 0 13.2 9.5z"/></svg>';
const SUN = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="8" r="2.9"/><path d="M8 1.6v1.4M8 13v1.4M14.4 8H13M3 8H1.6M12.4 3.6l-1 1M4.6 11.4l-1 1M12.4 12.4l-1-1M4.6 4.6l-1-1"/></svg>';

// Wire a button so it flips between light/dark, persists the choice, and keeps
// its icon in sync with the resolved (possibly OS-driven) theme.
export function bindToggle(btn) {
  if (!btn) return;
  const render = () => {
    const dark = root.classList.contains("dark");
    // icon = the mode a click will switch to: moon when light, sun when dark
    btn.innerHTML = dark ? SUN : MOON;
    btn.setAttribute("aria-label", dark ? "Switch to light appearance" : "Switch to dark appearance");
    btn.setAttribute("aria-pressed", String(dark));
    btn.title = dark ? "Light appearance" : "Dark appearance";
  };
  btn.addEventListener("click", () => {
    setTheme(root.classList.contains("dark") ? "light" : "dark");
    render();
  });
  // re-render if the OS flips the resolved theme while unpinned
  mq.addEventListener("change", render);
  render();
}
