// Sets <html data-theme> before the app renders, so there is no flash of the wrong theme.
// Must match THEMES and the storage key in src/theme.tsx. (External file: the CSP has no inline scripts.)
(function () {
  var themes = ["system", "light", "dark", "sepia", "ocean", "contrast"];
  var t = "system";
  try { t = localStorage.getItem("afs.theme") || "system"; } catch (e) { /* storage not available */ }
  if (themes.indexOf(t) < 0) t = "system";
  if (t === "system") t = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
})();
