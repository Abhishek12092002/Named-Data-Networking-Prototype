// SimN packet-playback player.
//
// Renders the topology (nodes + links) and animates packets strictly from
// a real, captured event trace fetched from /jobs/<id>/trace -- nothing
// here invents packet movement. Each 'tx' event carries the real
// simulated departure time (t) and arrival time (arrival_t); a packet is
// drawn interpolating linearly between its source and destination node
// positions across exactly that interval. Link failures/recoveries and
// SDN control updates are drawn from 'link_down'/'link_up'/'control_update'
// events the same way -- real events, not decoration.
//
// Rendering approach: the whole topology + packets are drawn on one
// <canvas> per animation frame (not thousands of DOM nodes), matching the
// "SVG for the static diagram, canvas for high-volume packet animation"
// split -- this canvas is the "live" view during playback; the SVG above
// it stays the always-available static diagram.

(function () {
  'use strict';

  var container = document.getElementById('packet-player');
  if (!container) return; // no trace captured for this run -- nothing to do

  var jobId = container.dataset.jobId;
  var architectures = (container.dataset.architectures || '').split(',').filter(Boolean);
  if (!jobId || architectures.length === 0) return;

  var canvas = document.getElementById('player-canvas');
  var ctx = canvas.getContext('2d');
  var archSelect = document.getElementById('player-arch-select');
  var playBtn = document.getElementById('player-play');
  var resetBtn = document.getElementById('player-reset');
  var scrub = document.getElementById('player-scrub');
  var timeLabel = document.getElementById('player-time');
  var speedSelect = document.getElementById('player-speed');
  var logEl = document.getElementById('player-log');
  var inspectorEl = document.getElementById('player-inspector');

  var COLORS = {
    ip_packet: '#F5A623',
    interest: '#4FD1C5',
    data: '#8B7FE8',
    link_down: '#E15554',
    link_up: '#3DDC84',
    control: '#8B7FE8',
  };

  var state = {
    positions: {},   // node id (string) -> [x, y]
    edges: [],       // [[u, v], ...]
    events: [],      // sorted by t
    maxT: 1,
    consumers: [],
    producers: {},
    playing: false,
    simTime: 0,
    speed: 1,
    lastFrameWall: null,
    failedEdges: {},  // "u-v" -> true while down
    selected: null,   // {kind:'node'|'packet'|'event', ...}
    logRendered: 0,
  };

  function edgeKey(u, v) { return u < v ? u + '-' + v : v + '-' + u; }

  function loadTrace(architecture) {
    fetch('/jobs/' + jobId + '/trace?architecture=' + encodeURIComponent(architecture))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) return;
        state.positions = data.positions || {};
        state.edges = data.edges || [];
        state.consumers = data.consumers || [];
        state.producers = data.producers || {};
        state.events = (data.events || []).slice().sort(function (a, b) { return (a.t || 0) - (b.t || 0); });
        state.maxT = state.events.length ? Math.max.apply(null, state.events.map(function (e) {
          return Math.max(e.t || 0, e.arrival_t || e.t || 0);
        })) : 1;
        state.simTime = 0;
        state.failedEdges = {};
        state.logRendered = 0;
        scrub.max = String(Math.ceil(state.maxT));
        scrub.value = '0';
        if (logEl) logEl.innerHTML = '';
        draw();
      })
      .catch(function () { /* trace unavailable -- leave canvas blank, no fabricated fallback */ });
  }

  function nodeRole(id) {
    if (state.consumers.indexOf(id) !== -1) return 'consumer';
    for (var prefix in state.producers) {
      if (state.producers[prefix] === id) return 'producer';
    }
    return 'relay';
  }

  function drawNode(id, pos) {
    var role = nodeRole(id);
    var x = pos[0], y = pos[1];
    ctx.fillStyle = role === 'producer' ? '#F5A623' : role === 'consumer' ? '#4FD1C5' : '#3A4A6B';
    ctx.strokeStyle = '#0A0E14';
    ctx.lineWidth = 2;
    ctx.beginPath();
    if (role === 'producer') {
      ctx.rect(x - 9, y - 9, 18, 18);
    } else if (role === 'consumer') {
      ctx.moveTo(x, y - 11);
      ctx.lineTo(x - 10, y + 8);
      ctx.lineTo(x + 10, y + 8);
      ctx.closePath();
    } else {
      ctx.arc(x, y, 9, 0, Math.PI * 2);
    }
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = '#0A0E14';
    ctx.font = '9px "IBM Plex Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(String(id), x, y + 3);
  }

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Links.
    state.edges.forEach(function (edge) {
      var u = edge[0], v = edge[1];
      var pu = state.positions[String(u)], pv = state.positions[String(v)];
      if (!pu || !pv) return;
      var down = state.failedEdges[edgeKey(u, v)];
      ctx.strokeStyle = down ? 'rgba(225,85,84,0.7)' : '#2B3752';
      ctx.lineWidth = down ? 2 : 1.4;
      ctx.setLineDash(down ? [4, 3] : []);
      ctx.beginPath();
      ctx.moveTo(pu[0], pu[1]);
      ctx.lineTo(pv[0], pv[1]);
      ctx.stroke();
    });
    ctx.setLineDash([]);

    // Nodes.
    Object.keys(state.positions).forEach(function (id) {
      drawNode(parseInt(id, 10), state.positions[id]);
    });

    // In-flight packets: any 'tx' event whose [t, arrival_t] interval
    // contains the current sim time.
    state.events.forEach(function (e) {
      if (e.kind !== 'tx') return;
      var t0 = e.t, t1 = (e.arrival_t != null ? e.arrival_t : e.t);
      if (state.simTime < t0 || state.simTime > t1) return;
      var pu = state.positions[String(e.u)];
      var pv = state.positions[String(e.v)];
      if (!pu || !pv) return;
      var frac = t1 > t0 ? (state.simTime - t0) / (t1 - t0) : 1;
      var x = pu[0] + (pv[0] - pu[0]) * frac;
      var y = pu[1] + (pv[1] - pu[1]) * frac;
      var color = e.proto === 'ip' ? COLORS.ip_packet : (e.ptype === 'interest' ? COLORS.interest : COLORS.data);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = '#0A0E14';
      ctx.lineWidth = 1;
      ctx.stroke();
    });

    // Control-plane pulses: control_update events flash near sim time.
    state.events.forEach(function (e) {
      if (e.kind !== 'control_update') return;
      if (Math.abs(state.simTime - e.t) > 8) return;
      ctx.fillStyle = 'rgba(139,127,232,0.5)';
      ctx.beginPath();
      ctx.arc(canvas.width / 2, canvas.height / 2 - 6, 20, 0, Math.PI * 2);
      ctx.fill();
    });

    timeLabel.textContent = 't = ' + state.simTime.toFixed(1) + ' ms';
    renderLogUpTo(state.simTime);
  }

  function fmtEvent(e) {
    var label = e.kind.toUpperCase();
    if (e.kind === 'tx' || e.kind === 'drop') {
      var pname = e.ptype === 'interest' ? 'Interest' : e.ptype === 'data' ? 'Data' : 'IP Packet';
      var route = e.v != null ? ('Node ' + e.u + ' \u2192 Node ' + e.v) : ('Node ' + e.u);
      return pname + '  ' + route + (e.name ? '  ' + e.name : '');
    }
    if (e.kind === 'rx') {
      var pname2 = e.ptype === 'interest' ? 'Interest' : e.ptype === 'data' ? 'Data' : 'IP Packet';
      return pname2 + '  Node ' + e.u + (e.name ? '  ' + e.name : '');
    }
    if (e.kind === 'cache_hit' || e.kind === 'cache_miss') return 'Node ' + e.u + '  ' + (e.name || '');
    if (e.kind === 'link_down' || e.kind === 'link_up') return 'Link ' + e.u + '\u2013' + e.v;
    if (e.kind === 'control_update') return (e.op || 'update') + '  targets=' + (e.targets != null ? e.targets : '?');
    return JSON.stringify(e);
  }

  function renderLogUpTo(simTime) {
    if (!logEl) return;
    while (state.logRendered < state.events.length && state.events[state.logRendered].t <= simTime) {
      var e = state.events[state.logRendered];
      var row = document.createElement('div');
      row.className = 'log-row log-' + e.kind;
      row.innerHTML = '<span class="log-t">' + e.t.toFixed(1) + ' ms</span>' +
        '<span class="log-kind">' + e.kind.toUpperCase() + '</span>' +
        '<span class="log-body">' + fmtEvent(e) + '</span>';
      row.addEventListener('click', function () { inspect(e); });
      logEl.appendChild(row);
      state.logRendered++;
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  function inspect(e) {
    var rows = Object.keys(e).filter(function (k) { return e[k] !== null && e[k] !== undefined; }).map(function (k) {
      return '<dt>' + k + '</dt><dd>' + e[k] + '</dd>';
    }).join('');
    inspectorEl.innerHTML = '<dl class="inspector-dl">' + rows + '</dl>';
  }

  canvas.addEventListener('click', function (evt) {
    var rect = canvas.getBoundingClientRect();
    var x = (evt.clientX - rect.left) * (canvas.width / rect.width);
    var y = (evt.clientY - rect.top) * (canvas.height / rect.height);
    var closest = null, closestDist = 16;
    Object.keys(state.positions).forEach(function (id) {
      var p = state.positions[id];
      var d = Math.hypot(p[0] - x, p[1] - y);
      if (d < closestDist) { closestDist = d; closest = id; }
    });
    if (closest != null) {
      var role = nodeRole(parseInt(closest, 10));
      var txCount = state.events.filter(function (e) { return e.u === parseInt(closest, 10) && e.kind === 'tx'; }).length;
      var rxCount = state.events.filter(function (e) { return e.u === parseInt(closest, 10) && e.kind === 'rx'; }).length;
      inspectorEl.innerHTML = '<dl class="inspector-dl">' +
        '<dt>node</dt><dd>' + closest + '</dd>' +
        '<dt>role</dt><dd>' + role + '</dd>' +
        '<dt>tx (trace)</dt><dd>' + txCount + '</dd>' +
        '<dt>rx (trace)</dt><dd>' + rxCount + '</dd>' +
        '</dl>';
    }
  });

  // Playback loop. Simulated network time (microseconds-to-low-hundreds
  // of ms per hop) is nowhere near human-perceivable at a literal 1:1
  // real-time mapping -- a 2ms link traversal would flash across the
  // screen in 2 real milliseconds. SIM_MS_PER_SECOND defines how many
  // simulated milliseconds play per real second at 1x speed, tuned so
  // even a single fast hop takes a clearly visible fraction of a second.
  var SIM_MS_PER_SECOND = 6; // at 1x: 6 simulated ms of network time per real second
  function tick(wallTime) {
    if (state.playing) {
      if (state.lastFrameWall != null) {
        var dtRealMs = wallTime - state.lastFrameWall;
        var simDelta = (dtRealMs / 1000) * SIM_MS_PER_SECOND * state.speed;
        state.simTime = Math.min(state.simTime + simDelta, state.maxT);
        applyFailuresUpTo(state.simTime);
        scrub.value = String(Math.round(state.simTime));
        draw();
        if (state.simTime >= state.maxT) state.playing = false;
      }
      state.lastFrameWall = wallTime;
      requestAnimationFrame(tick);
    } else {
      state.lastFrameWall = null;
    }
  }

  function applyFailuresUpTo(simTime) {
    // Recompute link state from scratch each time we seek/scrub, so
    // scrubbing backward correctly restores links that "recovered" after
    // the point we jumped to -- cheap given typical trace sizes.
    var failed = {};
    state.events.forEach(function (e) {
      if (e.t > simTime) return;
      if (e.kind === 'link_down') failed[edgeKey(e.u, e.v)] = true;
      if (e.kind === 'link_up') delete failed[edgeKey(e.u, e.v)];
    });
    state.failedEdges = failed;
  }

  playBtn.addEventListener('click', function () {
    state.playing = !state.playing;
    playBtn.innerHTML = state.playing ? '&#10074;&#10074;' : '&#9654;';
    if (state.playing) {
      if (state.simTime >= state.maxT) state.simTime = 0;
      requestAnimationFrame(tick);
    }
  });

  resetBtn.addEventListener('click', function () {
    state.playing = false;
    playBtn.innerHTML = '&#9654;';
    state.simTime = 0;
    scrub.value = '0';
    applyFailuresUpTo(0);
    state.logRendered = 0;
    if (logEl) logEl.innerHTML = '';
    draw();
  });

  scrub.addEventListener('input', function () {
    state.playing = false;
    playBtn.innerHTML = '&#9654;';
    state.simTime = parseFloat(scrub.value);
    applyFailuresUpTo(state.simTime);
    // Rebuild the log up to the sought time (may need to truncate if
    // seeking backward).
    state.logRendered = 0;
    if (logEl) logEl.innerHTML = '';
    draw();
  });

  speedSelect.addEventListener('change', function () {
    state.speed = parseFloat(speedSelect.value);
  });

  if (archSelect) {
    archSelect.addEventListener('change', function () {
      loadTrace(archSelect.value);
    });
  }

  loadTrace(architectures[0]);
})();
