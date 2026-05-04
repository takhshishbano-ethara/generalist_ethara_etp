(function () {
  "use strict";

  var toggle = document.getElementById("theme-toggle");
  if (!toggle) return;

  function getTheme() {
    try {
      return localStorage.getItem("eph:theme");
    } catch (e) {
      return null;
    }
  }

  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try {
      localStorage.setItem("eph:theme", t);
    } catch (e) {}
  }

  toggle.addEventListener("click", function () {
    var current = getTheme();
    if (!current) {
      current = window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    }
    setTheme(current === "dark" ? "light" : "dark");
  });
})();
