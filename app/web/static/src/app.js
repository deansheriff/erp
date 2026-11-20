// Kxmeleon ERP browser behaviour. Loaded before Alpine (CSP build), so every component
// is registered with Alpine.data: the CSP build cannot evaluate inline expressions.
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function storageGet(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------- theme
  function currentTheme() {
    var explicit = document.documentElement.getAttribute("data-theme");
    if (explicit) return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  // ------------------------------------------------------------- count-up for KPI tiles
  function countUp(el) {
    var target = parseFloat(el.getAttribute("data-count-to"));
    if (isNaN(target) || reduceMotion.matches || el.dataset.counted) return;
    el.dataset.counted = "1";
    var decimals = parseInt(el.getAttribute("data-decimals") || "0", 10);
    var prefix = el.getAttribute("data-prefix") || "";
    var final = el.textContent;
    var start = performance.now();
    var duration = 600;
    function frame(now) {
      var t = Math.min(1, (now - start) / duration);
      var eased = 1 - Math.pow(1 - t, 3);
      var value = (target * eased).toLocaleString("en-NG", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      });
      el.textContent = prefix + value;
      if (t < 1) requestAnimationFrame(frame);
      else el.textContent = final;
    }
    requestAnimationFrame(frame);
  }

  function initCountUps(root) {
    var nodes = (root || document).querySelectorAll("[data-count-to]");
    if (!("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          countUp(entry.target);
          io.unobserve(entry.target);
        }
      });
    });
    nodes.forEach(function (n) { io.observe(n); });
  }

  // ------------------------------------------------------------- Alpine components
  document.addEventListener("alpine:init", function () {
    var Alpine = window.Alpine;

    Alpine.data("shell", function () {
      return {
        collapsed: storageGet("kx-sidebar") === "collapsed",
        theme: currentTheme(),
        toggleSidebar: function () {
          this.collapsed = !this.collapsed;
          storageSet("kx-sidebar", this.collapsed ? "collapsed" : "expanded");
        },
        toggleTheme: function () {
          this.theme = this.theme === "dark" ? "light" : "dark";
          document.documentElement.setAttribute("data-theme", this.theme);
          storageSet("kx-theme", this.theme);
        },
        get isDark() { return this.theme === "dark"; },
        get isLight() { return this.theme !== "dark"; },
      };
    });

    Alpine.data("menu", function () {
      return {
        open: false,
        toggle: function () { this.open = !this.open; },
        close: function () { this.open = false; },
      };
    });

    Alpine.data("passwordField", function () {
      return {
        shown: false,
        length: 0,
        min: 12,
        init: function () { this.min = parseInt(this.$root.getAttribute("data-min") || "12", 10); },
        toggle: function () { this.shown = !this.shown; },
        update: function (e) { this.length = e.target.value.length; },
        get hidden() { return !this.shown; },
        get inputType() { return this.shown ? "text" : "password"; },
        get toggleLabel() { return this.shown ? "Hide password" : "Show password"; },
        get longEnough() { return this.length >= this.min; },
        get tooShort() { return this.length < this.min; },
        get lengthClass() { return this.longEnough ? "text-success-text" : "text-ink-3"; },
      };
    });

    Alpine.data("recoveryCodes", function () {
      return {
        copied: false,
        saved: false,
        codes: function () { return (this.$root.getAttribute("data-codes") || "").split(",").join("\n"); },
        copy: function () {
          var self = this;
          if (navigator.clipboard) {
            navigator.clipboard.writeText(this.codes()).then(function () {
              self.copied = true;
              setTimeout(function () { self.copied = false; }, 1600);
            });
          }
        },
        download: function () {
          var blob = new Blob(["Kxmeleon recovery codes\n\n" + this.codes() + "\n"], { type: "text/plain" });
          var link = document.createElement("a");
          link.href = URL.createObjectURL(blob);
          link.download = "kxmeleon-recovery-codes.txt";
          link.click();
          setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
        },
        toggleSaved: function () { this.saved = !this.saved; },
        get notSaved() { return !this.saved; },
        get notCopied() { return !this.copied; },
      };
    });

    Alpine.data("modal", function () {
      return {
        open: false,
        lastFocus: null,
        init: function () { this.open = this.$root.getAttribute("data-open") === "true"; },
        get closed() { return !this.open; },
        show: function () {
          this.lastFocus = document.activeElement;
          this.open = true;
          var self = this;
          this.$nextTick(function () {
            var first = self.$refs.panel && self.$refs.panel.querySelector("[autofocus], button, input, a[href]");
            if (first) first.focus();
          });
        },
        hide: function () {
          this.open = false;
          if (this.lastFocus) this.lastFocus.focus();
        },
      };
    });

    Alpine.data("toast", function () {
      return {
        visible: true,
        init: function () {
          var self = this;
          var persistent = this.$el.getAttribute("data-persistent") === "true";
          if (!persistent) setTimeout(function () { self.visible = false; }, 6000);
        },
        dismiss: function () { this.visible = false; },
      };
    });

    // Attendance: finds the browser's location for the clock-in form and previews the
    // photo. With data-required="true" (a geofenced branch) the button waits for it.
    Alpine.data("clockIn", function () {
      return {
        state: "idle",
        accuracy: null,
        photoUrl: "",
        required: false,
        init: function () {
          this.required = this.$root.getAttribute("data-required") === "true";
          this.locate();
        },
        locate: function () {
          var self = this;
          if (!navigator.geolocation) { self.state = "unavailable"; return; }
          self.state = "locating";
          navigator.geolocation.getCurrentPosition(function (pos) {
            self.$refs.lat.value = pos.coords.latitude.toFixed(6);
            self.$refs.lng.value = pos.coords.longitude.toFixed(6);
            self.$refs.acc.value = String(Math.round(pos.coords.accuracy));
            self.accuracy = Math.round(pos.coords.accuracy);
            self.state = "found";
          }, function (err) {
            self.state = err && err.code === 1 ? "denied" : "unavailable";
          }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
        },
        pickPhoto: function (e) {
          var self = this;
          var file = e.target.files && e.target.files[0];
          if (!file) { self.photoUrl = ""; return; }
          // The CSP allows data: images, not blob: URLs.
          var reader = new FileReader();
          reader.onload = function () { self.photoUrl = String(reader.result); };
          reader.readAsDataURL(file);
        },
        get locating() { return this.state === "locating" || this.state === "idle"; },
        get found() { return this.state === "found"; },
        get denied() { return this.state === "denied"; },
        get unavailable() { return this.state === "unavailable"; },
        get blocked() { return this.required && this.state !== "found"; },
        get foundText() { return this.accuracy ? "Location found (within " + this.accuracy + " m)" : "Location found"; },
        get hasPhoto() { return this.photoUrl !== ""; },
        get noPhoto() { return this.photoUrl === ""; },
        get photoLabel() { return this.photoUrl ? "Photo ready. Tap to retake" : "Take a photo"; },
      };
    });

    // Sends a form as soon as a file is chosen (e.g. a receipt), so one tap is enough.
    Alpine.data("autoSubmit", function () {
      return {
        send: function () {
          var form = this.$root;
          if (form.requestSubmit) form.requestSubmit();
          else form.submit();
        },
      };
    });

    Alpine.data("liveClock", function () {
      return {
        time: "",
        timer: null,
        init: function () {
          var self = this;
          var tz = this.$el.getAttribute("data-tz") || undefined;
          self.time = this.$el.textContent.trim();
          function tick() {
            try {
              self.time = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: tz });
            } catch (e) { /* keep the server's time */ }
          }
          tick();
          self.timer = setInterval(tick, 15000);
        },
        destroy: function () { clearInterval(this.timer); },
      };
    });

    Alpine.data("copyText", function () {
      return {
        copied: false,
        get notCopied() { return !this.copied; },
        copy: function () {
          var self = this;
          var text = this.$el.getAttribute("data-copy");
          if (navigator.clipboard && text) {
            navigator.clipboard.writeText(text).then(function () {
              self.copied = true;
              setTimeout(function () { self.copied = false; }, 1600);
            });
          }
        },
      };
    });
  });

  // ------------------------------------------------------------- "Go back" buttons
  document.addEventListener("click", function (e) {
    var target = e.target instanceof Element ? e.target.closest("[data-history-back]") : null;
    if (target) {
      e.preventDefault();
      if (window.history.length > 1) window.history.back();
      else window.location.assign("/");
    }
  });

  // ------------------------------------------------------------- keyboard: Ctrl/Cmd+K
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      var search = document.getElementById("global-search");
      if (search) {
        e.preventDefault();
        search.focus();
      }
    }
  });

  // ------------------------------------------------------------- progress bars
  // Inline style attributes are blocked by the CSP; setting styles from script is not.
  function initProgress(root) {
    (root || document).querySelectorAll("[data-progress]").forEach(function (el) {
      var value = Math.max(0, Math.min(100, parseFloat(el.getAttribute("data-progress")) || 0));
      el.style.width = value + "%";
    });
  }

  // ------------------------------------------------------------- HTMX hooks
  function initAll(root) {
    initCountUps(root);
    initProgress(root);
  }
  document.addEventListener("DOMContentLoaded", function () { initAll(document); });
  document.addEventListener("htmx:afterSwap", function (e) { initAll(e.target); });
})();
