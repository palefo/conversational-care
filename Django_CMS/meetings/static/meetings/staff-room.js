/* Online meetings — the navigator's window.
 *
 * Pre-join check → join → the room, with the lobby, the client's link, the
 * voice interviewer and the push-to-talk assistant around it → end → record
 * the outcome. Server state (who is waiting, which agents are in, whether the
 * recorder is running) is read from /online/state/ every few seconds and on
 * every nudge LiveKit delivers; the server is always the source of truth.
 */
(function () {
  'use strict';
  var boot = JSON.parse(document.getElementById('rm-boot').textContent);
  var T = JSON.parse(document.getElementById('rm-i18n').textContent);
  var CSRF = window.CC_CSRF;
  var R = window.CCRoom;
  var LK = window.LivekitClient;
  function $(id) { return document.getElementById(id); }
  function toast(msg, kind) { R.toast($('rmToasts'), msg, kind); }

  var room = R.create({ stage: $('rmStage'), audience: 'staff', i18n: T, me: boot.me });
  var S = {
    joined: false, ending: false, srv: null, poll: null, clock: null,
    lobbySeen: {}, answers: {}, ivProtocol: null, ivRun: null, asRun: null, held: false,
    micId: '', preStream: null, stopMeter: null, preMic: true, preCam: false
  };
  var bc = ('BroadcastChannel' in window) ? new BroadcastChannel('cc-live') : null;

  // ── Pre-join ────────────────────────────────────────────────────────────
  $('rmPreviewAvatar').textContent = R.initials(boot.me.name);
  $('rmPreviewAvatar').style.setProperty('--hue', R.hue(boot.me.identity));
  if (!boot.configured) {
    $('rmJoin').disabled = true;
    showPreErr(T.notConfigured || 'Online meetings are not connected to a meeting server yet.');
  }

  function showPreErr(text) { var e = $('rmPreErr'); e.textContent = text; e.hidden = !text; }

  function stopPreview() {
    if (S.stopMeter) { S.stopMeter(); S.stopMeter = null; }
    if (S.preStream) { S.preStream.getTracks().forEach(function (t) { t.stop(); }); S.preStream = null; }
  }

  function startPreview() {
    stopPreview();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showPreErr(T.deviceGeneric); return Promise.resolve();
    }
    var want = { audio: S.preMic ? (S.micId ? { deviceId: { exact: S.micId } } : true) : false,
                 video: S.preCam };
    if (!want.audio && !want.video) { $('rmPreMeter').style.width = '0'; return Promise.resolve(); }
    return navigator.mediaDevices.getUserMedia(want).then(function (stream) {
      S.preStream = stream;
      showPreErr('');
      var v = $('rmPreviewVideo');
      if (S.preCam) { v.srcObject = stream; v.hidden = false; $('rmPreviewAvatar').hidden = true; }
      else { v.srcObject = null; v.hidden = true; $('rmPreviewAvatar').hidden = false; }
      S.stopMeter = R.meter(stream, function (lvl) { $('rmPreMeter').style.width = Math.round(lvl * 100) + '%'; });
      return fillMicSelect();
    }).catch(function (err) { showPreErr(R.explainMediaError(err, T)); });
  }

  function fillMicSelect() {
    return R.listDevices().then(function (list) {
      var sel = $('rmPreMicSel');
      var mics = list.filter(function (d) { return d.kind === 'audioinput'; });
      sel.innerHTML = '';
      mics.forEach(function (d, i) {
        var o = document.createElement('option');
        o.value = d.deviceId; o.textContent = d.label || (T.selectMic + ' ' + (i + 1));
        if (d.deviceId === S.micId) o.selected = true;
        sel.appendChild(o);
      });
    });
  }

  function toggleBtn(btn, on, iconOn, iconOff) {
    btn.classList.toggle('is-off', !on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.querySelector('.mi').textContent = on ? iconOn : iconOff;
    var tip = btn.querySelector('.rm-tip');
    if (tip && tip.dataset.on) tip.textContent = on ? tip.dataset.on : tip.dataset.off;
  }

  $('rmPreMic').addEventListener('click', function () {
    S.preMic = !S.preMic; toggleBtn(this, S.preMic, 'mic', 'mic_off'); startPreview();
  });
  $('rmPreCam').addEventListener('click', function () {
    S.preCam = !S.preCam; toggleBtn(this, S.preCam, 'videocam', 'videocam_off'); startPreview();
  });
  $('rmPreMicSel').addEventListener('change', function () { S.micId = this.value; startPreview(); });
  $('rmJoin').addEventListener('click', join);
  startPreview();

  // ── Joining ─────────────────────────────────────────────────────────────
  function banner(text) {
    var b = $('rmBanner');
    if (!text) { b.hidden = true; return; }
    $('rmBannerText').textContent = text; b.hidden = false;
  }

  function join() {
    var btn = $('rmJoin');
    btn.disabled = true;
    showPreErr('');
    R.post(boot.urls.start, {}, CSRF).then(function (d) {
      stopPreview();
      return room.connect(d.ws_url, d.token, { mic: S.preMic, cam: S.preCam, micId: S.micId }).then(function () {
        S.joined = true;
        $('rmPrejoin').hidden = true;
        $('rmLeft').hidden = true;
        applyState(d.state);
        if (!S.clock) S.clock = R.clock(d.state.started_at, $('rmClock'));
        syncDock();
        startPolling();
        broadcast(true);
      });
    }).catch(function (err) {
      btn.disabled = false;
      showPreErr(err.message || T.deviceGeneric);
      if (!$('rmLeft').hidden) toast(err.message || '', 'error');
    });
  }

  function startPolling() {
    if (S.poll) clearInterval(S.poll);
    S.poll = setInterval(poll, 3000);
    poll();
  }

  function poll() {
    return R.getJSON(boot.urls.state).then(function (d) {
      if (!d.live && S.joined && !S.ending) {
        // Closed from somewhere else (the panel, the reaper, an outcome).
        onEnded(T.removed);
        return;
      }
      applyState(d);
    }).catch(function () {});
  }

  room.on('state', function (st, reason) {
    if (st === 'reconnecting') banner(T.reconnecting);
    else if (st === 'connected') banner('');
    else if (st === 'disconnected') {
      banner('');
      if (S.ending) return;
      S.joined = false;
      var R_ = LK.DisconnectReason || {};
      if (reason === R_.DUPLICATE_IDENTITY) showLeft(T.leftTitle, T.duplicate, true);
      else if (reason === R_.ROOM_DELETED || reason === R_.PARTICIPANT_REMOVED) onEnded(T.removed);
      else if (reason !== R_.CLIENT_INITIATED) showLeft(T.leftTitle, T.reconnecting, true);
    }
  });
  room.on('quality', function (q) { $('rmQuality').dataset.q = q; });
  room.on('autoplay', function (ok) { $('rmAudio').hidden = !!ok || !S.joined; });
  $('rmAudioBtn').addEventListener('click', function () { room.startAudio().then(function () { $('rmAudio').hidden = true; }); });
  room.on('deviceError', function (err) { toast(R.explainMediaError(err, T), 'error'); });
  room.on('participants', function () { renderPeople(); fillRespondents(); });
  room.on('attributes', function () { renderPeople(); });
  room.on('data', function (d, p, topic) {
    if (topic === 'cc.lobby') { poll(); chime(); }
    if (topic === 'cc.interview') {
      if (d.type === 'answer') loadQuestions(true);
      if (d.type === 'status') poll();
      if (d.type === 'flag') toast(d.text || '', 'error');
    }
  });

  // ── Server state ────────────────────────────────────────────────────────
  function applyState(d) {
    if (!d) return;
    S.srv = d;
    var rec = $('rmRec');
    if (d.recording && d.recording.enabled) {
      rec.hidden = false;
      var st = d.recording.state;
      rec.dataset.state = st === 'on' ? 'on' : (st === 'lost' ? 'lost' : 'starting');
      rec.textContent = st === 'on' ? T.recOn : (st === 'lost' ? T.recLost : T.recStarting);
      $('rmPreRec').textContent = T.recOn;
    } else {
      rec.hidden = true;
    }
    $('rmAutoAdmit').checked = !!d.auto_admit;
    renderLobby(d.lobby || []);
    var runs = d.runs || [];
    S.ivRun = runs.filter(function (r) { return r.role === 'interviewer'; })[0] || null;
    S.asRun = runs.filter(function (r) { return r.role === 'assistant' && ['dispatched', 'running', 'paused'].indexOf(r.state) >= 0; })[0] || null;
    renderInterview();
    syncAsk();
    renderPeople();
  }

  // ── Lobby ───────────────────────────────────────────────────────────────
  function chime() {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    try {
      var c = new Ctx(), o = c.createOscillator(), g = c.createGain();
      o.frequency.value = 880; g.gain.setValueAtTime(0.0001, c.currentTime);
      g.gain.exponentialRampToValueAtTime(0.12, c.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + 0.35);
      o.connect(g); g.connect(c.destination); o.start(); o.stop(c.currentTime + 0.4);
      setTimeout(function () { c.close(); }, 600);
    } catch (e) {}
  }

  function renderLobby(list) {
    var box = $('rmLobby');
    var count = $('rmKnockCount');
    count.hidden = !list.length; count.textContent = list.length;
    if (!list.length) { box.innerHTML = ''; var e = R.el('div', 'rm-empty'); e.textContent = T.nobodyWaiting; box.appendChild(e); return; }
    box.innerHTML = '';
    list.forEach(function (w) {
      if (!S.lobbySeen[w.invite]) { S.lobbySeen[w.invite] = true; toast(w.name + ' ' + T.knocking); chime(); }
      var row = R.el('div', 'rm-person is-knocking');
      var mini = R.el('div', 'rm-mini'); mini.textContent = R.initials(w.name); mini.style.setProperty('--hue', R.hue('inv-' + w.invite));
      var txt = R.el('div', 'rm-person-txt');
      var b = R.el('b'); b.textContent = w.name; var s = R.el('span'); s.textContent = w.role;
      txt.appendChild(b); txt.appendChild(s);
      var deny = R.el('button', 'rm-sbtn rm-sbtn-ghost'); deny.type = 'button'; deny.textContent = T.deny;
      var admit = R.el('button', 'rm-sbtn rm-sbtn-pri'); admit.type = 'button'; admit.textContent = T.admit;
      deny.onclick = function () { decide(w.invite, 'deny'); };
      admit.onclick = function () { decide(w.invite, 'admit'); };
      row.appendChild(mini); row.appendChild(txt); row.appendChild(deny); row.appendChild(admit);
      box.appendChild(row);
    });
  }

  function decide(pid, how) {
    var url = (how === 'admit' ? boot.urls.admit : boot.urls.deny).replace('__pid__', encodeURIComponent(pid));
    R.post(url, {}, CSRF).then(poll).catch(function (e) { toast(e.message, 'error'); });
  }

  $('rmAutoAdmit').addEventListener('change', function () {
    R.post(boot.urls.auto_admit, { on: this.checked ? '1' : '0' }, CSRF).then(poll)
      .catch(function (e) { toast(e.message, 'error'); });
  });

  // ── People ──────────────────────────────────────────────────────────────
  function renderPeople() {
    var box = $('rmPeople');
    box.innerHTML = '';
    room.participants().forEach(function (p) {
      var row = R.el('div', 'rm-person' + (p.isAgent ? ' is-agent' : ''));
      var mini = R.el('div', 'rm-mini'); mini.textContent = p.isAgent ? '' : R.initials(p.name);
      mini.style.setProperty('--hue', R.hue(p.identity));
      if (p.isAgent) mini.appendChild(R.icon(p.role === 'scribe' ? 'fiber_manual_record' : 'graphic_eq'));
      var txt = R.el('div', 'rm-person-txt');
      var b = R.el('b'); b.textContent = p.local ? (p.name + ' (' + T.you + ')') : p.name;
      var s = R.el('span');
      var role = (T.roles && T.roles[p.role]) || '';
      if (p.isAgent && p.attributes['lk.agent.state']) role += ' · ' + ((T.agentStates || {})[p.attributes['lk.agent.state']] || '');
      s.textContent = role;
      txt.appendChild(b); txt.appendChild(s);
      row.appendChild(mini); row.appendChild(txt);
      if (!p.isAgent) row.appendChild(R.icon(p.mic ? 'mic' : 'mic_off'));
      box.appendChild(row);
    });
  }

  // ── The link ────────────────────────────────────────────────────────────
  $('rmCopy').addEventListener('click', function () {
    var url = $('rmLinkUrl').textContent.trim();
    function ok() { toast(T.copied); }
    if (url.indexOf('/m/') < 0) {
      R.post(boot.urls.invite, {}, CSRF).then(function (d) { $('rmLinkUrl').textContent = d.url; return navigator.clipboard.writeText(d.url); }).then(ok)
        .catch(function (e) { toast(e.message, 'error'); });
      return;
    }
    (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject()).then(ok, function () {
      var r = document.createRange(); r.selectNodeContents($('rmLinkUrl'));
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-send]'), function (b) {
    b.addEventListener('click', function () {
      b.disabled = true;
      R.post(boot.urls.send, { channel: b.dataset.send }, CSRF).then(function (d) {
        toast(d.message || T.saved, d.ok ? null : 'error');
      }).catch(function (e) { toast((e.data && (e.data.error || e.data.message)) || T.sendFail, 'error'); })
        .then(function () { b.disabled = false; });
    });
  });
  $('rmRotate').addEventListener('click', function () {
    R.post(boot.urls.invite, { rotate: '1' }, CSRF).then(function (d) {
      $('rmLinkUrl').textContent = d.url; toast(d.message || T.rotated);
    }).catch(function (e) { toast(e.message, 'error'); });
  });

  // ── Dock ────────────────────────────────────────────────────────────────
  function syncDock() {
    toggleBtn($('rmMic'), room.micOn(), 'mic', 'mic_off');
    toggleBtn($('rmCam'), room.camOn(), 'videocam', 'videocam_off');
  }
  $('rmMic').addEventListener('click', function () { room.setMic(!room.micOn()).then(syncDock, function (e) { toast(R.explainMediaError(e, T), 'error'); }); });
  $('rmCam').addEventListener('click', function () { room.setCam(!room.camOn()).then(syncDock, function (e) { toast(R.explainMediaError(e, T), 'error'); }); });
  document.addEventListener('keydown', function (e) {
    if (!S.joined || e.metaKey || e.ctrlKey || e.altKey) return;
    var tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
    if (e.key === 'm' || e.key === 'M') { e.preventDefault(); $('rmMic').click(); }
    if (e.key === 'v' || e.key === 'V') { e.preventDefault(); $('rmCam').click(); }
  });

  function menu(btn, box, build) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = box.hidden;
      document.querySelectorAll('.rm-menu').forEach(function (m) { m.hidden = true; });
      if (open) { build && build(); box.hidden = false; btn.setAttribute('aria-expanded', 'true'); }
      else btn.setAttribute('aria-expanded', 'false');
    });
  }
  document.addEventListener('click', function () {
    document.querySelectorAll('.rm-menu').forEach(function (m) { m.hidden = true; });
  });

  menu($('rmDevBtn'), $('rmDevMenu'), function () {
    var box = $('rmDevMenu');
    box.innerHTML = '';
    R.listDevices().then(function (list) {
      [['audioinput', T.selectMic], ['videoinput', T.selectCam], ['audiooutput', T.selectSpeaker]].forEach(function (k) {
        var items = list.filter(function (d) { return d.kind === k[0]; });
        if (!items.length) return;
        var h = R.el('h4'); h.textContent = k[1]; box.appendChild(h);
        var active = room.activeDevice(k[0]);
        items.forEach(function (d, i) {
          var o = R.el('button', 'rm-opt' + (d.deviceId === active ? ' is-sel' : ''));
          o.type = 'button';
          o.appendChild(R.icon(d.deviceId === active ? 'radio_button_checked' : 'radio_button_unchecked'));
          var s = R.el('span'); s.textContent = d.label || (k[1] + ' ' + (i + 1)); o.appendChild(s);
          o.onclick = function () { room.switchDevice(k[0], d.deviceId).catch(function (e) { toast(R.explainMediaError(e, T), 'error'); }); };
          box.appendChild(o);
        });
        if (k[0] === 'audiooutput') {
          var t = R.el('button', 'rm-opt'); t.type = 'button';
          t.appendChild(R.icon('volume_up')); var s2 = R.el('span'); s2.textContent = T.testSpeaker; t.appendChild(s2);
          t.onclick = function () { R.testSpeaker(room.activeDevice('audiooutput')); };
          box.appendChild(t);
        }
      });
    });
  });

  menu($('rmLeaveMore'), $('rmLeaveMenu'));
  $('rmLeave').addEventListener('click', function () { endForAll(); });
  $('rmLeaveMenu').addEventListener('click', function (e) {
    var b = e.target.closest('[data-act]');
    if (!b) return;
    if (b.dataset.act === 'end') endForAll();
    else leaveOnly();
  });

  $('rmRailToggle').addEventListener('click', function () {
    var rail = $('rmRail');
    var open = rail.classList.toggle('is-closed') === false;
    this.setAttribute('aria-pressed', open ? 'true' : 'false');
    setTimeout(room.relayout, 280);
  });
  Array.prototype.forEach.call(document.querySelectorAll('.rm-tab'), function (tab) {
    tab.addEventListener('click', function () {
      document.querySelectorAll('.rm-tab').forEach(function (t) { t.classList.toggle('is-on', t === tab); t.setAttribute('aria-selected', t === tab ? 'true' : 'false'); });
      document.querySelectorAll('.rm-pane').forEach(function (p) { p.hidden = p.dataset.pane !== tab.dataset.tab; });
      if (tab.dataset.tab === 'interview') loadQuestions(false);
    });
  });

  // ── Interview ───────────────────────────────────────────────────────────
  (function fillProtocols() {
    var sel = $('rmIvProtocol');
    if (!boot.protocols.length) {
      $('rmIvStart').disabled = true;
      var n = $('rmIvNote'); n.textContent = T.ivNoProtocols; n.hidden = false;
      return;
    }
    boot.protocols.forEach(function (p) {
      var o = document.createElement('option');
      o.value = p.number;
      o.textContent = p.number + '. ' + p.title + ' (' + p.questions + ')';
      sel.appendChild(o);
    });
    if (!boot.agents) { $('rmIvStart').disabled = true; }
  })();

  function fillRespondents() {
    var sel = $('rmIvWho');
    var prev = sel.value;
    sel.innerHTML = '';
    var people = room.participants().filter(function (p) { return !p.local && !p.isAgent && p.role !== 'navigator'; });
    people.forEach(function (p) {
      var o = document.createElement('option');
      o.value = p.identity; o.textContent = p.name + (T.roles[p.role] ? ' — ' + T.roles[p.role] : '');
      o.dataset.name = p.name;
      if (p.identity === prev) o.selected = true;
      sel.appendChild(o);
    });
    if (!people.length) {
      var o = document.createElement('option'); o.value = ''; o.textContent = T.ivNoOne; sel.appendChild(o);
    }
  }

  $('rmIvStart').addEventListener('click', function () {
    var who = $('rmIvWho');
    if (!who.value) { toast(T.ivNoOne, 'error'); return; }
    var btn = this; btn.disabled = true;
    S.ivProtocol = parseInt($('rmIvProtocol').value, 10);
    R.post(boot.urls.interview, {
      protocol: $('rmIvProtocol').value, respondent: who.value,
      respondent_name: (who.selectedOptions[0] || {}).dataset ? who.selectedOptions[0].dataset.name : '',
      language: boot.language || ''
    }, CSRF).then(function () { poll(); loadQuestions(true); })
      .catch(function (e) { toast(e.message, 'error'); })
      .then(function () { btn.disabled = false; });
  });

  function ivLive(run) { return run && ['dispatched', 'running', 'paused'].indexOf(run.state) >= 0; }

  function renderInterview() {
    var run = S.ivRun;
    var live = ivLive(run);
    $('rmIvIdle').hidden = live;
    $('rmIvLive').hidden = !live && !(run && run.state === 'finished');
    if (!run) return;
    if (run.protocol) S.ivProtocol = run.protocol;
    $('rmIvTitle').textContent = run.protocol_title || '';
    var sub = run.state === 'dispatched' ? T.ivStarting
            : run.state === 'paused' ? T.ivPaused
            : run.state === 'finished' ? T.ivFinished
            : run.state === 'failed' ? (T.ivFailed + (run.error ? ': ' + run.error : ''))
            : (T.ivAsking + ' ' + (run.respondent_name || ''));
    $('rmIvSub').textContent = sub;
    var pause = $('rmIvPause');
    pause.querySelector('.mi').textContent = run.state === 'paused' ? 'play_arrow' : 'pause';
    pause.querySelector('span:last-child').textContent = run.state === 'paused' ? T.ivResume : T.ivPause;
    ['rmIvPause', 'rmIvSayBtn', 'rmIvStop'].forEach(function (id) { $(id).disabled = !live || !run.identity; });
    if (!live && run.state === 'failed') { $('rmIvIdle').hidden = false; var n = $('rmIvNote'); n.textContent = sub; n.hidden = false; }
    if (live && document.querySelector('.rm-tab.is-on').dataset.tab === 'interview') loadQuestions(false);
  }

  var qBusy = false;
  function loadQuestions(markNew) {
    var num = S.ivProtocol || (S.ivRun && S.ivRun.protocol) || parseInt($('rmIvProtocol').value, 10);
    if (!num || qBusy) return;
    qBusy = true;
    R.getJSON(boot.urls.questions + '?protocol=' + encodeURIComponent(num)).then(function (d) {
      renderQuestions(d, markNew);
    }).catch(function () {}).then(function () { qBusy = false; });
  }

  function renderQuestions(d, markNew) {
    var box = $('rmIvQs');
    box.innerHTML = '';
    var pct = d.total ? Math.round(100 * d.answered / d.total) : 0;
    $('rmIvBar').style.setProperty('--p', pct + '%');
    $('rmIvCount').textContent = d.answered + ' ' + T.ivOf + ' ' + d.total + ' ' + T.ivAnswered;
    if (!$('rmIvTitle').textContent) $('rmIvTitle').textContent = d.protocol.number + '. ' + d.protocol.title;
    d.questions.forEach(function (q, i) {
      var was = S.answers[q.id];
      var fresh = markNew && q.answer && was !== q.answer;
      S.answers[q.id] = q.answer;
      var row = R.el('div', 'rm-q' + (q.answer ? ' is-done' : '') + (fresh ? ' is-new' : ''));
      var dot = R.el('div', 'rm-q-dot'); dot.textContent = q.answer ? '✓' : (i + 1);
      var body = R.el('div');
      var p = R.el('p'); p.textContent = q.prompt; body.appendChild(p);
      if (q.answer) {
        var a = R.el('div', 'rm-a' + (q.source === 'voice' ? ' is-voice' : '')); a.textContent = q.answer; body.appendChild(a);
      } else if (q.carried) {
        var c = R.el('div', 'rm-a is-carried'); c.textContent = T.ivCarried + ' (' + q.carried.when + '): ' + q.carried.text; body.appendChild(c);
      }
      row.appendChild(dot); row.appendChild(body);
      box.appendChild(row);
      if (fresh) toast(T.ivNewAnswer + ': ' + q.prompt.slice(0, 60));
    });
  }

  function rpcRun(run, method, payload) {
    if (!run || !run.identity) return Promise.reject(new Error('agent not ready'));
    return room.rpc(run.identity, method, payload);
  }
  $('rmIvPause').addEventListener('click', function () {
    var run = S.ivRun;
    rpcRun(run, run.state === 'paused' ? 'cc.resume' : 'cc.pause').then(poll).catch(function (e) { toast(e.message, 'error'); });
  });
  $('rmIvSayBtn').addEventListener('click', function () { var b = $('rmIvSayBox'); b.hidden = !b.hidden; if (!b.hidden) $('rmIvSay').focus(); });
  $('rmIvSaySend').addEventListener('click', function () {
    var text = $('rmIvSay').value.trim();
    if (!text) return;
    rpcRun(S.ivRun, 'cc.say', { text: text }).then(function () { $('rmIvSay').value = ''; $('rmIvSayBox').hidden = true; toast(T.saved); })
      .catch(function (e) { toast(e.message, 'error'); });
  });
  $('rmIvStop').addEventListener('click', function () {
    var run = S.ivRun;
    rpcRun(run, 'cc.stop').catch(function () {
      return R.post(boot.urls.interview_stop.replace('__id__', run.id), {}, CSRF);
    }).then(poll);
  });

  // ── The assistant (push to talk; everyone hears it) ──────────────────
  function syncAsk() {
    var btn = $('rmAsk');
    btn.hidden = !boot.assistant; $('rmAskSep').hidden = !boot.assistant;
    if (!boot.assistant) return;
    var run = S.asRun;
    var lbl = $('rmAskLbl');
    if (!run) lbl.textContent = T.askStart;
    else if (!run.identity || run.state === 'dispatched') lbl.textContent = T.askJoining;
    else lbl.textContent = S.held ? T.askListening : T.askHold;
  }
  function askDown(e) {
    var run = S.asRun;
    if (!run) {
      R.post(boot.urls.assistant, {}, CSRF).then(poll).catch(function (er) { toast(er.message, 'error'); });
      return;
    }
    if (!run.identity) return;
    e.preventDefault();
    S.held = true; $('rmAsk').classList.add('is-held'); syncAsk();
    rpcRun(run, 'cc.ptt.start').catch(function (er) { toast(er.message, 'error'); });
  }
  function askUp() {
    if (!S.held) return;
    S.held = false; $('rmAsk').classList.remove('is-held'); syncAsk();
    rpcRun(S.asRun, 'cc.ptt.end').catch(function () {});
  }
  var ask = $('rmAsk');
  ask.addEventListener('pointerdown', askDown);
  ask.addEventListener('pointerup', askUp);
  ask.addEventListener('pointerleave', askUp);
  ask.addEventListener('keydown', function (e) { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) askDown(e); });
  ask.addEventListener('keyup', function (e) { if (e.key === ' ' || e.key === 'Enter') askUp(); });

  // ── Leaving and ending ─────────────────────────────────────────────────
  function showLeft(title, text, canRejoin) {
    $('rmLeftTitle').textContent = title;
    $('rmLeftText').textContent = text;
    $('rmRejoin').hidden = !canRejoin;
    $('rmLeft').hidden = false;
    broadcast(false);
  }
  $('rmRejoin').addEventListener('click', function () { $('rmLeft').hidden = true; $('rmJoin').disabled = false; join(); });
  $('rmClose').addEventListener('click', closeWindow);

  function leaveOnly() {
    S.ending = true;
    room.disconnect().then(function () {
      S.ending = false; S.joined = false;
      if (S.poll) clearInterval(S.poll);
      showLeft(T.leftTitle, T.leftText || '', true);
    });
  }

  function endForAll() {
    if (!window.confirm(T.endConfirm)) return;
    S.ending = true;
    R.post(boot.urls.end, {}, CSRF).catch(function () {}).then(function () {
      return room.disconnect();
    }).then(function () { onEnded(null); });
  }

  function onEnded(msg) {
    S.ending = true; S.joined = false;
    if (S.poll) clearInterval(S.poll);
    if (S.clock) clearInterval(S.clock);
    room.disconnect();
    broadcast(false);
    if (msg) toast(msg);
    showOutcome();
  }

  function showOutcome() {
    var box = $('rmOutcomes');
    box.innerHTML = '';
    var kinds = { 1: 'done', 3: 'miss', 2: 'part' };
    boot.outcomes.forEach(function (o) {
      var b = R.el('button', 'rm-outcome'); b.type = 'button'; b.dataset.k = kinds[o[0]] || 'part';
      b.appendChild(R.el('i')); var s = R.el('span'); s.textContent = o[1]; b.appendChild(s);
      b.onclick = function () {
        R.post(boot.urls.outcome, { status: o[0], next: boot.urls.panel }, CSRF)
          .catch(function () {}).then(function () { toast(T.saved); setTimeout(closeWindow, 700); });
      };
      box.appendChild(b);
    });
    $('rmOutcome').hidden = false;
  }
  $('rmOutcomeSkip').addEventListener('click', closeWindow);

  function closeWindow() {
    broadcast(false);
    window.close();
    // Not opened by script (a bookmark, a new tab): go back to the meeting.
    setTimeout(function () { location.href = boot.urls.panel; }, 250);
  }

  // ── Telling the main app a meeting is running ──────────────────────────
  function broadcast(live) {
    if (!bc) return;
    try {
      bc.postMessage({ type: 'cc-meeting', id: boot.meeting, live: !!live,
                       since: S.srv && S.srv.started_at, title: boot.patient,
                       panel: boot.urls.panel, room: location.pathname });
    } catch (e) {}
  }
  if (bc) {
    setInterval(function () { if (S.joined) broadcast(true); }, 5000);
    bc.onmessage = function (ev) {
      if (ev.data && ev.data.type === 'cc-meeting-ping' && S.joined) broadcast(true);
    };
  }
  window.addEventListener('pagehide', function () { broadcast(false); });
  window.addEventListener('beforeunload', function (e) {
    if (S.joined && !S.ending) { e.preventDefault(); e.returnValue = ''; }
  });
})();
