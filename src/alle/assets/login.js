// The sign-in flow: post the pasted token/secret to /api/v1/login, which
// exchanges it for an HttpOnly session cookie. Kept as an external module so
// the page's CSP can require script-src 'self' (no inline script).
import { followSystem, bindToggle } from "./theme.js";

followSystem();
bindToggle(document.getElementById("theme"));

const form = document.getElementById("form");
const err = document.getElementById("err");
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  err.classList.remove("show");
  const token = document.getElementById("token").value.trim();
  try {
    const res = await fetch("/api/v1/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (res.ok) {
      location.href = "/";
      return;
    }
    err.textContent = "That token wasn't accepted. Check it and try again.";
  } catch (error_) {
    const detail = error_ instanceof Error ? error_.message : String(error_);
    err.textContent = `Couldn't reach the dashboard: ${detail}`;
  }
  err.classList.add("show");
});
