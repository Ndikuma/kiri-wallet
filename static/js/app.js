/* ==========================================================================
   Kiri Wallet — console shell behaviour
   Sidebar collapse / mobile drawer, theme toggle, toast auto-dismiss,
   nav-group memory.
   ========================================================================== */
(function () {
  "use strict";

  var shell = document.getElementById("app-shell");
  var COLLAPSE_KEY = "btcwallet.sidebar.collapsed";
  var THEME_KEY = "btcwallet.theme";
  var NAV_KEY = "btcwallet.nav.open";

  // --- Theme --------------------------------------------------------------
  function applyTheme(mode) {
    if (mode === "dark" || mode === "light") {
      document.documentElement.setAttribute("data-theme", mode);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }
  try {
    var savedTheme = localStorage.getItem(THEME_KEY);
    if (savedTheme) applyTheme(savedTheme);
  } catch (err) {}

  var themeToggle = document.querySelector("[data-theme-toggle]");
  if (themeToggle) {
    themeToggle.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      if (!current) {
        current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      var next = current === "dark" ? "light" : "dark";
      applyTheme(next);
      try { localStorage.setItem(THEME_KEY, next); } catch (err) {}
    });
  }

  if (!shell) return;

  var desktopToggle = document.getElementById("sidebar-toggle");
  var mobileToggle = document.getElementById("mobile-toggle");
  var overlay = document.getElementById("shell-overlay");

  function isDesktop() { return window.innerWidth > 1080; }

  function syncDesktopToggle() {
    if (!desktopToggle) return;
    var collapsed = shell.classList.contains("collapsed");
    var label = desktopToggle.querySelector(".sidebar-toggle-label");
    desktopToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    desktopToggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    if (label) label.textContent = collapsed ? "Expand" : "Collapse";
  }

  function syncMobileToggle() {
    if (!mobileToggle) return;
    mobileToggle.setAttribute("aria-expanded", shell.classList.contains("mobile-open") ? "true" : "false");
  }

  function closeMobile() {
    shell.classList.remove("mobile-open");
    syncMobileToggle();
  }

  try {
    if (isDesktop() && localStorage.getItem(COLLAPSE_KEY) === "true") {
      shell.classList.add("collapsed");
    }
  } catch (err) {}

  syncDesktopToggle();
  syncMobileToggle();

  if (desktopToggle) {
    desktopToggle.addEventListener("click", function () {
      shell.classList.toggle("collapsed");
      try { localStorage.setItem(COLLAPSE_KEY, shell.classList.contains("collapsed") ? "true" : "false"); } catch (err) {}
      syncDesktopToggle();
    });
  }

  if (mobileToggle) {
    mobileToggle.addEventListener("click", function () {
      shell.classList.toggle("mobile-open");
      syncMobileToggle();
    });
  }

  if (overlay) overlay.addEventListener("click", closeMobile);

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeMobile();
  });

  window.addEventListener("resize", function () {
    if (isDesktop()) closeMobile();
  });

  // --- Nav groups: expand on click when collapsed, remember open state ----
  var openSet = new Set();
  try { openSet = new Set(JSON.parse(localStorage.getItem(NAV_KEY) || "[]")); } catch (err) {}

  document.querySelectorAll("[data-nav-group]").forEach(function (group, index) {
    var key = (group.querySelector(".nav-text") || {}).textContent || String(index);
    key = key.trim();
    if (openSet.has(key)) group.open = true;

    var summary = group.querySelector("summary");
    if (summary) {
      summary.addEventListener("click", function (e) {
        if (isDesktop() && shell.classList.contains("collapsed")) {
          e.preventDefault();
          shell.classList.remove("collapsed");
          try { localStorage.setItem(COLLAPSE_KEY, "false"); } catch (err) {}
          syncDesktopToggle();
        }
      });
    }

    group.addEventListener("toggle", function () {
      if (group.open) openSet.add(key); else openSet.delete(key);
      try { localStorage.setItem(NAV_KEY, JSON.stringify(Array.from(openSet))); } catch (err) {}
    });
  });

  // --- Click-to-copy (.copy-box) --------------------------------------
  document.querySelectorAll(".copy-box").forEach(function (box) {
    box.setAttribute("role", "button");
    box.setAttribute("tabindex", "0");
    box.title = "Click to copy";
    function copy() {
      var text = (box.textContent || "").trim();
      function done() {
        box.classList.add("is-copied");
        setTimeout(function () { box.classList.remove("is-copied"); }, 1400);
      }
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        try {
          var r = document.createRange();
          r.selectNodeContents(box);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(r);
          document.execCommand("copy");
          done();
        } catch (e) {}
      }
    }
    box.addEventListener("click", copy);
    box.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); copy(); }
    });
  });

  // --- Toast / message auto-dismiss -------------------------------------
  document.querySelectorAll(".messages .message").forEach(function (msg) {
    setTimeout(function () {
      msg.style.transition = "opacity .3s ease, transform .3s ease";
      msg.style.opacity = "0";
      msg.style.transform = "translateY(-6px)";
      setTimeout(function () { msg.remove(); }, 320);
    }, 5200);
  });
})();
