/* Online meetings — the client's page: check devices, ask to join, wait to be
 * let in, then the meeting itself, all without leaving the page.
 *
 * The browser holds no LiveKit credential until the navigator admits it: the
 * lobby only ever polls this server, which hands a short-lived token over in
 * the same response that says "admitted".
 */
(function () {
  'use strict';
  var boot = JSON.parse(document.getElementById('lb-boot').textContent);
  var T = JSON.parse(document.getElementById('lb-i18n').textContent);
  var CSRF = window.CC_CSRF;
  var R = window.CCRoom;
  var LK = window.LivekitClient;
  function $(id) { return document.getElementById(id); }
  function toast(msg, kind) { R.toast($('rmToasts'), msg, kind); }

  var S = { mic: true, cam: false, micId: '', camId: '', stream: null, stopMeter: null,
            poll: null, joined: false, leaving: false, clock: null, knocked: false };

  $('lbAvatar').textContent = R.initials(boot.name || '?');
  $('lbAvatar').style.setProperty('--hue', R.hue(boot.identity));

  function err(text) { var e = $('lbErr'); e.textContent = text || ''; e.hidden = !text; }

  // ── Device check ────────────────────────────────────────────────────────
  function stopPreview() {
    if (S.stopMeter) { S.stopMeter(); S.stopMeter = null; }
    if (S.stream) { S.stream.getTracks().forEach(function (t) { t.stop(); }); S.stream = null; }
  }
  function preview() {
    stopPreview();
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      err(T.deviceGeneric); return Promise.resolve();
    }
    var want = {
      audio: S.mic ? (S.micId ? { deviceId: { exact: S.micId } } : { echoCancellation: true, noiseSuppression: true }) : false,
      video: S.cam ? (S.camId ? { deviceId: { exact: S.camId } } : { facingMode: 'user' }) : false
    };
    if (!want.audio && !want.video) { $('lbMeter').style.width = '0'; showVideo(null); return Promise.resolve(); }
    return navigator.mediaDevices.getUserMedia(want).then(function (stream) {
      S.stream = stream; err('');
      showVideo(S.cam ? stream : null);
      S.stopMeter = R.meter(stream, function (lvl) { $('lbMeter').style.width = Math.round(lvl * 100) + '%'; });
      return fillDevices();
    }).catch(function (e) { err(R.explainMediaError(e, T)); });
  }
  function showVideo(stream) {
    var v = $('lbVideo');
    v.srcObject = stream; v.hidden = !stream;
    $('lbAvatar').hidden = !!stream; $('lbCamNote').hidden = !!stream;
  }
  function fillDevices() {
    return R.listDevices().then(function (list) {
      [['audioinput', 'lbMicSel', 'micId'], ['videoinput', 'lbCamSel', 'camId']].forEach(function (k) {
        var sel = $(k[1]);
        var items = list.filter(function (d) { return d.kind === k[0]; });
        sel.innerHTML = '';
        // One camera is not a choice; the microphone picker takes the row.
        sel.closest('label').hidden = items.length < 2 && k[0] === 'videoinput';
        items.forEach(function (d, i) {
          var o = document.createElement('option');
          o.value = d.deviceId; o.textContent = d.label || (T.defaultDevice + ' ' + (i + 1));
          if (d.deviceId === S[k[2]]) o.selected = true;
          sel.appendChild(o);
        });
      });
    });
  }
  function toggle(btn, on, a, b) {
    btn.classList.toggle('is-off', !on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.querySelector('.mi').textContent = on ? a : b;
  }
  $('lbMic').addEventListener('click', function () { S.mic = !S.mic; toggle(this, S.mic, 'mic', 'mic_off'); preview(); });
  $('lbCam').addEventListener('click', function () { S.cam = !S.cam; toggle(this, S.cam, 'videocam', 'videocam_off'); preview(); });
  $('lbMicSel').addEventListener('change', function () { S.micId = this.value; preview(); });
  $('lbCamSel').addEventListener('change', function () { S.camId = this.value; if (S.cam) preview(); });
  $('lbTest').addEventListener('click', function () { R.testSpeaker(); });
  preview();

  // ── Asking to join ──────────────────────────────────────────────────────
  function showWait(title, text, again) {
    $('lbReady').hidden = true;
    $('lbWait').hidden = false;
    $('lbWaitTitle').textContent = title;
    $('lbWaitText').textContent = text;
    $('lbAgain').hidden = !again;
    if (again) $('lbAgainLbl').textContent = again;
  }
  function showReady() { $('lbReady').hidden = false; $('lbWait').hidden = true; }

  $('lbJoin').addEventListener('click', knock);
  $('lbAgain').addEventListener('click', knock);

  function knock() {
    var b = $('lbJoin'); b.disabled = true; $('lbJoinLbl').textContent = T.joining;
    R.post(boot.urls.knock, {}, CSRF).then(function (d) {
      S.knocked = true;
      handle(d);
      startPolling();
    }).catch(function (e) {
      if (e.status === 410) { location.href = boot.urls.ended + '?reason=unavailable'; return; }
      err(e.message);
    }).then(function () { b.disabled = false; $('lbJoinLbl').textContent = T.join; });
  }

  function startPolling() {
    if (S.poll) return;
    S.poll = setInterval(function () {
      R.getJSON(boot.urls.status).then(handle).catch(function (e) {
        if (e.status === 410) { stopPolling(); location.href = boot.urls.ended + '?reason=unavailable'; }
      });
    }, 2000);
  }
  function stopPolling() { if (S.poll) { clearInterval(S.poll); S.poll = null; } }

  function handle(d) {
    if (S.joined) return;
    switch (d.state) {
      case 'no_host': showWait(T.noHostTitle, T.noHostText); break;
      case 'waiting': showWait(T.waitingTitle, T.waitingText); break;
      case 'denied': stopPolling(); showWait(T.deniedTitle, T.deniedText, T.askAgain); break;
      case 'ready': if (S.knocked) knock(); else showReady(); break;
      case 'admitted': stopPolling(); enter(d); break;
      case 'ended': stopPolling(); location.href = boot.urls.ended; break;
      default: break;
    }
  }

  // ── In the meeting ──────────────────────────────────────────────────────
  var room = R.create({ stage: $('rmStage'), audience: 'client', i18n: T, me: { identity: boot.identity, name: boot.name } });

  function enter(d) {
    S.joined = true;
    stopPreview();
    room.connect(d.ws_url, d.token, { mic: S.mic, cam: S.cam, micId: S.micId, camId: S.camId }).then(function () {
      $('lb').hidden = true;
      $('rm').hidden = false;
      document.querySelector('meta[name="theme-color"]').setAttribute('content', '#0a0f15');
      $('rmRec').hidden = !d.recording;
      S.clock = R.clock(null, $('rmClock'));
      syncDock();
      room.relayout();
    }).catch(function (e) {
      S.joined = false;
      showReady();
      err(R.explainMediaError(e, T) || e.message);
    });
  }

  room.on('state', function (st, reason) {
    var banner = $('rmBanner');
    if (st === 'reconnecting') { $('rmBannerText').textContent = T.reconnecting; banner.hidden = false; }
    else if (st === 'connected') banner.hidden = true;
    else if (st === 'disconnected') {
      banner.hidden = true;
      if (S.leaving) return;
      var DR = LK.DisconnectReason || {};
      var why = reason === DR.DUPLICATE_IDENTITY ? 'duplicate' : '';
      location.href = boot.urls.ended + (why ? '?reason=' + why : '');
    }
  });
  room.on('autoplay', function (ok) { $('rmAudio').hidden = !!ok || !S.joined; });
  $('rmAudioBtn').addEventListener('click', function () { room.startAudio().then(function () { $('rmAudio').hidden = true; }); });
  room.on('deviceError', function (e) { toast(R.explainMediaError(e, T), 'error'); });

  function syncDock() {
    var m = room.micOn(), c = room.camOn();
    toggle($('rmMic'), m, 'mic', 'mic_off');
    $('rmMic').classList.toggle('is-off', !m);
    toggle($('rmCam'), c, 'videocam', 'videocam_off');
    $('rmCam').classList.toggle('is-off', false);
    $('rmCam').classList.toggle('is-on', c);
  }
  $('rmMic').addEventListener('click', function () { room.setMic(!room.micOn()).then(syncDock, function (e) { toast(R.explainMediaError(e, T), 'error'); }); });
  $('rmCam').addEventListener('click', function () { room.setCam(!room.camOn()).then(syncDock, function (e) { toast(R.explainMediaError(e, T), 'error'); }); });
  $('rmLeave').addEventListener('click', function () {
    S.leaving = true;
    R.post(boot.urls.leave, {}, CSRF).catch(function () {}).then(function () { return room.disconnect(); })
      .then(function () { location.href = boot.urls.ended + '?reason=left'; });
  });
  // Closing the tab is not leaving: the lobby entry stays "admitted", so
  // opening the link again goes straight back in without knocking.
})();
