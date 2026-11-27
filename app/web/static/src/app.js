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

    // Journal line editors: live debit and credit totals, and whether they balance.
    Alpine.data("journalTotals", function () {
      function kobo(root, name) {
        var total = 0;
        root.querySelectorAll('input[name="' + name + '"]').forEach(function (el) {
          var value = parseFloat((el.value || "").replace(/[,\s₦]/g, ""));
          if (!isNaN(value)) total += Math.round(value * 100);
        });
        return total;
      }
      function naira(k) {
        return "₦" + (k / 100).toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      }
      return {
        debit: "₦0.00",
        credit: "₦0.00",
        difference: "",
        balanced: false,
        unbalanced: false,
        init: function () { this.recalc(); },
        recalc: function () {
          var d = kobo(this.$root, "debit");
          var c = kobo(this.$root, "credit");
          this.debit = naira(d);
          this.credit = naira(c);
          this.balanced = d === c && d > 0;
          this.unbalanced = d !== c;
          this.difference = d === c ? "" : "Out by " + naira(Math.abs(d - c));
        },
      };
    });

    // Sales document lines (ADR 0024): net per row and VAT at the standard rate, rounded
    // per line to the kobo as the server does. The server's figures are the ones that count.
    Alpine.data("salesTotals", function () {
      function num(el) {
        var value = parseFloat(((el && el.value) || "").replace(/[,\s₦]/g, ""));
        return isNaN(value) ? 0 : value;
      }
      function naira(k) {
        return "₦" + (k / 100).toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      }
      return {
        net: "₦0.00",
        vat: "₦0.00",
        total: "₦0.00",
        init: function () { this.recalc(); },
        recalc: function () {
          var rate = parseFloat(this.$root.getAttribute("data-vat-rate") || "0") || 0;
          var net = 0;
          var vat = 0;
          this.$root.querySelectorAll("[data-line]").forEach(function (row) {
            var qtyEl = row.querySelector('input[name="quantity"]');
            var qty = qtyEl && qtyEl.value.trim() === "" ? 1 : num(qtyEl);
            var line = Math.round(qty * num(row.querySelector('input[name="unit_price"]')) * 100);
            var code = row.querySelector('select[name="vat_code"]');
            net += line;
            if (code && code.value === "standard") vat += Math.round((line * rate) / 100);
            var cell = row.querySelector("[data-line-net]");
            if (cell) cell.textContent = line ? naira(line) : "";
          });
          this.net = naira(net);
          this.vat = naira(vat);
          this.total = naira(net + vat);
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

    // Chat (ADR 0034). The socket only says what changed; content comes from ordinary
    // routes, which check membership. One fetch of new messages at a time, and messages
    // already on the page are never added twice.
    Alpine.data("chatRoom", function () {
      return {
        socket: null,
        closing: false,
        retry: 1000,
        busy: false,
        again: false,
        fileName: "",
        conversation: "",
        init: function () {
          var self = this;
          this.conversation = this.$el.getAttribute("data-conversation") || "";
          this.scrollDown();
          this.connect();
          this.onSent = function () { self.resetComposer(); self.fetchNew(); };
          document.body.addEventListener("chat-sent", this.onSent);
        },
        destroy: function () {
          this.closing = true;
          document.body.removeEventListener("chat-sent", this.onSent);
          if (this.socket) this.socket.close();
        },
        connect: function () {
          var self = this;
          if (!("WebSocket" in window)) return;
          var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
          var ws = new WebSocket(scheme + "//" + window.location.host + "/ws/chat");
          this.socket = ws;
          ws.onopen = function () { self.retry = 1000; };
          ws.onmessage = function (e) {
            try { self.handle(JSON.parse(e.data)); } catch (err) { /* ignore a bad notice */ }
          };
          ws.onclose = function (e) {
            if (self.closing) return;
            if (e.code === 4401) { window.location.reload(); return; }  // signed out
            if (e.code === 4403) return;
            setTimeout(function () { if (!self.closing) self.connect(); }, self.retry);
            self.retry = Math.min(self.retry * 2, 30000);
          };
        },
        handle: function (ev) {
          var here = ev.conversation === this.conversation;
          if (ev.type === "message") {
            if (here) this.fetchNew(); else this.refreshList();
          } else if (ev.type === "changed" && here) {
            this.refreshMessage(ev.message);
            this.refresh("/chat/" + this.conversation + "/pins", "chat-pins");
          } else if (ev.type === "read" && here) {
            this.refresh("/chat/" + this.conversation + "/receipt", "chat-receipt");
          } else if (ev.type === "members") {
            if (here) window.location.reload(); else this.refreshList();
          }
        },
        get: function (url) {
          return fetch(url, { headers: { "HX-Request": "true" }, credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.text() : ""; });
        },
        lastId: function () {
          var items = this.$refs.stream ? this.$refs.stream.querySelectorAll("[data-message-id]") : [];
          return items.length ? items[items.length - 1].getAttribute("data-message-id") : "";
        },
        nearBottom: function () {
          var s = this.$refs.stream;
          return !s || s.scrollHeight - s.scrollTop - s.clientHeight < 120;
        },
        scrollDown: function () {
          var s = this.$refs.stream;
          if (s) s.scrollTop = s.scrollHeight;
        },
        fetchNew: function () {
          var self = this;
          if (!this.conversation || !this.$refs.stream) return;
          if (this.busy) { this.again = true; return; }
          this.busy = true;
          var stick = this.nearBottom();
          this.get("/chat/" + this.conversation + "/messages?after=" + this.lastId())
            .then(function (html) { self.append(html, stick); })
            .finally(function () {
              self.busy = false;
              if (self.again) { self.again = false; self.fetchNew(); return; }
              self.refreshList();
              self.refresh("/chat/" + self.conversation + "/receipt", "chat-receipt");
            });
        },
        append: function (html, stick) {
          var stream = this.$refs.stream;
          var tpl = document.createElement("template");
          tpl.innerHTML = html;
          var empty = stream.querySelector("[data-chat-empty]");
          if (empty && tpl.content.querySelector("[data-message-id]")) empty.remove();
          Array.prototype.slice.call(tpl.content.children).forEach(function (node) {
            var id = node.getAttribute("data-message-id");
            if (!id || document.getElementById("msg-" + id)) return;
            stream.appendChild(node);
            if (window.htmx) window.htmx.process(node);
          });
          if (stick) this.scrollDown();
        },
        refreshMessage: function (id) {
          this.get("/chat/" + this.conversation + "/messages/" + id).then(function (html) {
            var old = document.getElementById("msg-" + id);
            if (!old || !html) return;
            var tpl = document.createElement("template");
            tpl.innerHTML = html.trim();
            var node = tpl.content.firstElementChild;
            if (!node) return;
            old.replaceWith(node);
            if (window.htmx) window.htmx.process(node);
          });
        },
        refresh: function (url, targetId) {
          this.get(url).then(function (html) {
            var target = document.getElementById(targetId);
            if (target) {
              target.innerHTML = html;
              if (window.htmx) window.htmx.process(target);
            }
          });
        },
        refreshList: function () {
          this.refresh("/chat/list?current=" + this.conversation, "chat-list");
        },
        resetComposer: function () {
          var form = document.getElementById("chat-composer");
          if (form) form.reset();
          var error = document.getElementById("chat-composer-error");
          if (error) error.innerHTML = "";
          this.fileName = "";
        },
        fileChosen: function (e) {
          var file = e.target.files && e.target.files[0];
          this.fileName = file ? file.name : "";
        },
        sendOnEnter: function (e) {
          if (e.shiftKey || e.isComposing) return;  // Shift+Enter makes a new line
          e.preventDefault();
          var form = document.getElementById("chat-composer");
          if (form && form.requestSubmit) form.requestSubmit();
        },
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
