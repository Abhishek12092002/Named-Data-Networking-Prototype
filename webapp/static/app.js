// SimN dashboard interactivity.
//
// This is a classic server-rendered app: the form POSTs to /run and the
// browser navigates to a freshly rendered page. Nothing here simulates
// network events or fabricates progress -- it only reacts to real DOM
// state (the form, the rendered results) and to genuine browser
// lifecycle events (submit, navigation, viewport intersection).

(function () {
  'use strict';

  // ---- 1. SDN-conditional field visibility ------------------------------
  // Fields that only matter when an SDN architecture is in play (controller
  // RTT, SDN cache top-K) are dimmed rather than hidden outright, so the
  // form's shape doesn't jump around as the user toggles checkboxes.
  function sdnArchitecturesSelected() {
    var boxes = document.querySelectorAll('.arch-checkbox');
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].checked && (boxes[i].dataset.arch === 'ip_sdn' || boxes[i].dataset.arch === 'ndn_sdn')) {
        return true;
      }
    }
    var forwardingSelect = document.querySelector('select[name="forwarding"]');
    var forwardingsSweep = document.querySelector('input[name="forwardings"]');
    if (forwardingSelect && forwardingSelect.value === 'sdn-centralized') return true;
    if (forwardingsSweep && forwardingsSweep.value.indexOf('sdn-centralized') !== -1) return true;
    return false;
  }

  function updateSdnFieldVisibility() {
    var active = sdnArchitecturesSelected();
    var fields = document.querySelectorAll('[data-sdn-field]');
    fields.forEach(function (el) {
      el.classList.toggle('field-inactive', !active);
    });
  }

  document.addEventListener('change', function (e) {
    if (e.target.matches('.arch-checkbox') || e.target.matches('select[name="forwarding"]') || e.target.matches('input[name="forwardings"]')) {
      updateSdnFieldVisibility();
    }
    if (e.target.matches('#network-profile-select')) {
      updateNetworkProfileFields();
    }
  });

  // ---- 2. Network profile: dim + disable manual link fields --------------
  function updateNetworkProfileFields() {
    var select = document.getElementById('network-profile-select');
    var wrap = document.getElementById('manual-link-fields');
    if (!select || !wrap) return;
    var manual = select.value === '';
    wrap.classList.toggle('field-inactive', !manual);
    wrap.querySelectorAll('input').forEach(function (inp) {
      inp.disabled = !manual;
    });
  }

  // ---- 3. Honest loading state on submit ---------------------------------
  // The backend is a synchronous, single request/response simulation run --
  // there is no server-sent progress channel. So this shows one honest
  // "running" state (not a fabricated multi-stage progress bar) and lets
  // the browser's own navigation-in-progress behavior carry the rest.
  var form = document.getElementById('sim-form');
  var overlay = document.getElementById('loading-overlay');
  var runBtn = document.getElementById('run-btn');
  var statusPill = document.getElementById('sim-status');

  // ---- 3. Async job submission with honest, real progress polling -------
  // The backend runs the simulation on a background thread and returns a
  // job id immediately (see /run/async in webapp/app.py) -- this is what
  // actually fixes a blocked/stuck page for slow or large sweeps, not just
  // a nicer-looking spinner. If anything about the async path fails (JS
  // disabled, fetch blocked, unexpected error), the code falls back to
  // letting the browser submit the form normally to POST /run, which still
  // works exactly as it always has (see webapp/app.py's run()).
  var loadingText = document.getElementById('loading-text-line');
  var pollTimer = null;

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function pollJob(jobId) {
    fetch('/jobs/' + jobId + '/status')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.status === 'not_found') {
          failAsync('Job not found (server may have restarted).');
          return;
        }
        if (loadingText) {
          var parts = [];
          if (data.combos_total) parts.push('config ' + Math.min(data.combos_done + 1, data.combos_total) + ' of ' + data.combos_total);
          if (data.elapsed != null) parts.push(data.elapsed.toFixed(1) + 's elapsed');
          loadingText.textContent = parts.length ? parts.join(' \u00b7 ') : 'Running\u2026';
        }
        if (data.status === 'completed') {
          window.location.href = '/jobs/' + jobId + '/view';
          return;
        }
        if (data.status === 'failed') {
          failAsync(data.error || 'Simulation failed.');
          return;
        }
        pollTimer = setTimeout(function () { pollJob(jobId); }, 400);
      })
      .catch(function () {
        // Network hiccup while polling -- keep trying a few times rather
        // than giving up on the first blip.
        pollTimer = setTimeout(function () { pollJob(jobId); }, 1000);
      });
  }

  function failAsync(message) {
    stopPolling();
    if (overlay) overlay.hidden = true;
    if (runBtn) runBtn.textContent = 'Run simulation \u2192';
    if (statusPill) { statusPill.textContent = 'idle'; statusPill.dataset.state = 'idle'; }
    window.alert('SimN: ' + message + '\nYour configuration was preserved -- adjust and try again.');
  }

  if (form) {
    form.addEventListener('submit', function (evt) {
      if (typeof window.fetch !== 'function') return;  // let native submit proceed
      evt.preventDefault();

      // IMPORTANT: do not set `.disabled = true` on any named form field.
      // Disabled form controls are excluded from FormData just like they
      // are from native form submission, which would silently submit an
      // empty form. The full-page overlay below already blocks pointer
      // events across the whole viewport, which is enough to prevent a
      // double submit.
      if (overlay) overlay.hidden = false;
      if (runBtn) runBtn.textContent = 'Running\u2026';
      if (statusPill) { statusPill.textContent = 'running'; statusPill.dataset.state = 'running'; }
      if (loadingText) loadingText.textContent = 'Starting\u2026';

      var formData = new FormData(form);
      fetch('/run/async', { method: 'POST', body: formData })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function (data) {
          if (!data.job_id) throw new Error('no job id returned');
          pollJob(data.job_id);
        })
        .catch(function (err) {
          failAsync('Could not start the simulation (' + err.message + ').');
        });
    });
  }

  // ---- 4. Session-scoped experiment history ------------------------------
  // Purely client-side: the backend does not persist runs across requests,
  // so this reads/writes sessionStorage rather than claiming a server-side
  // history the app doesn't have. Each successful run embeds a small JSON
  // summary (#run-summary-data) that gets appended here.
  var HISTORY_KEY = 'simn_history_v1';
  var MAX_HISTORY = 12;

  function loadHistory() {
    try {
      return JSON.parse(sessionStorage.getItem(HISTORY_KEY) || '[]');
    } catch (e) {
      return [];
    }
  }

  function saveHistory(list) {
    try {
      sessionStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_HISTORY)));
    } catch (e) { /* storage unavailable -- degrade silently, no history */ }
  }

  function renderHistory() {
    var list = loadHistory();
    var container = document.getElementById('history-list');
    var emptyMsg = document.getElementById('history-empty');
    if (!container) return;
    if (list.length === 0) {
      container.hidden = true;
      if (emptyMsg) emptyMsg.hidden = false;
      return;
    }
    if (emptyMsg) emptyMsg.hidden = true;
    container.hidden = false;
    container.innerHTML = list.map(function (entry) {
      var archs = (entry.architectures || []).join(', ') || 'ip, ndn';
      var time = new Date(entry.ts).toLocaleTimeString();
      return (
        '<div class="history-row">' +
        '<span class="history-time">' + time + '</span>' +
        '<span class="history-topo">' + entry.topology + ' &middot; ' + entry.nodes + ' nodes</span>' +
        '<span class="history-arch">' + archs + '</span>' +
        '<span class="history-workload">' + entry.workload + ' / ' + entry.network_profile + '</span>' +
        '<span class="history-latency">' + (entry.latency || '—') + '</span>' +
        '<span class="history-elapsed">' + entry.elapsed + 's</span>' +
        '</div>'
      );
    }).join('');
  }

  function recordThisRunIfAny() {
    var dataEl = document.getElementById('run-summary-data');
    if (!dataEl) return;
    try {
      var entry = JSON.parse(dataEl.textContent);
      entry.ts = Date.now();
      var list = loadHistory();
      list.unshift(entry);
      saveHistory(list);
    } catch (e) { /* malformed/missing summary -- skip recording, never fabricate */ }
  }

  // ---- 5. Nav active-section highlighting --------------------------------
  function setupNavHighlighting() {
    var links = document.querySelectorAll('.nav-link');
    var sections = Array.prototype.map.call(links, function (l) {
      return document.getElementById(l.dataset.nav);
    }).filter(Boolean);
    if (!sections.length || !('IntersectionObserver' in window)) return;

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          links.forEach(function (l) { l.classList.toggle('active', l.dataset.nav === entry.target.id); });
        }
      });
    }, { rootMargin: '-40% 0px -50% 0px' });

    sections.forEach(function (s) { observer.observe(s); });
  }

  // ---- init ---------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    updateSdnFieldVisibility();
    updateNetworkProfileFields();
    recordThisRunIfAny();
    renderHistory();
    setupNavHighlighting();
    if (statusPill) {
      statusPill.textContent = 'idle';
      statusPill.dataset.state = 'idle';
    }
  });
})();
