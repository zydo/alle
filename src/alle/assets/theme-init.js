// Resolved before first paint: a blocking classic script in <head> (CSP allows
// 'self', and classic — not module — so it runs during head parse, before the
// body renders). It sets a single `dark` class on <html> so style.css keeps the
// dark token swap in one place, with no flash of the wrong theme.
//
// Preference order: an explicit saved choice wins; otherwise follow the OS.
(function () {
  try {
    const saved = localStorage.getItem("alle-theme");
    const dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.toggle("dark", dark);
  } catch (e) {
    // localStorage throws in private mode / when blocked — fall back to the
    // stylesheet defaults. Logged (not swallowed) so it's diagnosable.
    console.debug("alle: appearance preference unavailable", e);
  }
})();
