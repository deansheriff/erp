// Loaded blocking in <head> so the chosen theme applies before first paint (no flash).
// An external file rather than an inline script, so the CSP can stay script-src 'self'.
(function () {
  try {
    var theme = localStorage.getItem("kx-theme");
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    }
  } catch (e) {
    /* storage unavailable: follow the OS setting */
  }
})();
