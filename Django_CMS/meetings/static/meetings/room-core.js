/* Online meetings — the room engine shared by the staff window and the client page.
 *
 * Wraps the vendored LiveKit client (window.LivekitClient) and owns everything
 * both audiences see the same way: tiles, speaking rings, the assistant's orb,
 * mute state, devices, reconnects, audio autoplay. The two pages add their own
 * controls on top through the events this emits.
 *
 *   const room = CCRoom.create({ stage, audience: 'staff'|'client', i18n, me });
 *   room.on('state', s => …);           // 'connecting'|'connected'|'reconnecting'|'disconnected'
 *   await room.connect(wsUrl, token, { mic: true, cam: false, micId, camId });
 *
 * Denoising seam: the browser's own echo cancellation / noise suppression /
 * gain control are on (audioCaptureDefaults). A stronger processor can be
 * attached later with track.setProcessor() on the local microphone track —
 * see onLocalMic() — without changing anything else here.
 */
(function () {
  'use strict';
  var LK = window.LivekitClient;

  function hue(str) {
    var h = 0;
    for (var i = 0; i < (str || '').length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
    return h % 360;
  }
  function initials(name) {
    var parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return '?';
    return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
  }
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function icon(name, cls) {
    var i = el('span', 'mi' + (cls ? ' ' + cls : ''));
    i.setAttribute('aria-hidden', 'true');
    i.textContent = name;
    return i;
  }

  function create(opts) {
    var stage = opts.stage;
    var t = opts.i18n || {};
    var handlers = {};
    var tiles = {};          // identity -> { root, video, avatar, name, mic, p }
    var audioBin = el('div');
    audioBin.hidden = true;
    document.body.appendChild(audioBin);
    var room = null;
    var raf = null;
    var hiddenRoles = { scribe: true };

    function emit(name, a, b, c) {
      (handlers[name] || []).forEach(function (fn) { try { fn(a, b, c); } catch (e) { console.error(e); } });
    }

    function roleOf(p) {
      var a = (p && p.attributes) || {};
      if (a['cc.role']) return a['cc.role'];
      if (p && p.isAgent) return 'agent';
      if (p && String(p.identity || '').indexOf('staff-') === 0) return 'navigator';
      return 'guest';
    }
    function roleLabel(role) {
      return (t.roles && t.roles[role]) || '';
    }
    function isHidden(p) {
      return hiddenRoles[roleOf(p)] === true;
    }

    // ── Tiles ───────────────────────────────────────────────────────────
    function layout() {
      var n = Object.keys(tiles).length;
      // The space tiles actually get: the stage minus its padding (the dock
      // floats over the bottom of it) and the gaps between tiles.
      var cs = window.getComputedStyle(stage);
      var gap = parseFloat(cs.columnGap || cs.gap) || 0;
      var w = stage.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
      var h = stage.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
      // Tiles keep 16:10. For each column count, how wide can a tile be?
      function width(c) {
        var rows = Math.ceil(n / c);
        return Math.min((w - gap * (c - 1)) / c, ((h - gap * (rows - 1)) / rows) * 1.6);
      }
      var cols = 1, best = n ? width(1) : w;
      for (var c = 2; c <= Math.min(n, 4); c++) {
        if (width(c) > best) { best = width(c); cols = c; }
      }
      // People expect to sit side by side: take more columns whenever that
      // costs less than ~15% of tile size.
      for (var k = Math.min(n, 4); k > cols; k--) {
        if (width(k) >= best * 0.85) { cols = k; break; }
      }
      stage.style.setProperty('--cols', cols);
      stage.style.setProperty('--tile-w', Math.floor(width(cols)) + 'px');
    }

    function ensureTile(p, isLocal) {
      var id = p.identity;
      if (tiles[id]) return tiles[id];
      var role = roleOf(p);
      var agent = p.isAgent || role === 'interviewer' || role === 'assistant';
      var root = el('div', 'rm-tile' + (agent ? ' is-agent' : ''));
      root.dataset.identity = id;
      root.style.setProperty('--hue', hue(id));
      var video = el('video');
      video.autoplay = true; video.muted = true; video.playsInline = true;
      var avatar = el('div', 'rm-avatar', '');
      avatar.textContent = initials(p.name || id);
      var name = el('div', 'rm-name');
      var mic = icon('mic');
      var nm = el('b'); nm.textContent = p.name || (agent ? t.assistant : id);
      var sub = el('small');
      // "AI interviewer · AI interviewer" says nothing twice.
      sub.textContent = roleLabel(role) === nm.textContent ? '' : roleLabel(role);
      name.appendChild(mic); name.appendChild(nm); if (sub.textContent) name.appendChild(sub);
      if (agent) {
        root.appendChild(el('div', 'rm-orb'));
        var st = el('div', 'rm-agent-state');
        root.appendChild(st);
      } else {
        root.appendChild(video);
        root.appendChild(avatar);
      }
      root.appendChild(name);
      if (isLocal) {
        var you = el('span', 'rm-badge is-you'); you.textContent = t.you || 'You';
        root.appendChild(you);
        root.dataset.mirror = '1';
      }
      stage.appendChild(root);
      tiles[id] = { root: root, video: video, avatar: avatar, name: nm, mic: mic, p: p, agent: agent, local: !!isLocal };
      refreshTile(p);
      layout();
      return tiles[id];
    }

    function removeTile(id) {
      var tl = tiles[id];
      if (!tl) return;
      tl.root.remove();
      delete tiles[id];
      layout();
    }

    function refreshTile(p) {
      var tl = tiles[p.identity];
      if (!tl) return;
      tl.p = p;
      if (p.name) { tl.name.textContent = p.name; if (!tl.agent) tl.avatar.textContent = initials(p.name); }
      var micOn = p.isMicrophoneEnabled;
      tl.mic.textContent = micOn ? 'mic' : 'mic_off';
      tl.mic.classList.toggle('is-muted', !micOn);
      if (tl.agent) {
        var a = p.attributes || {};
        var state = a['lk.agent.state'] || 'initializing';
        if (a['cc.paused'] === '1') state = 'paused';
        tl.root.dataset.state = state;
        var label = (t.agentStates && t.agentStates[state]) || state;
        var st = tl.root.querySelector('.rm-agent-state');
        if (st) st.textContent = label;
      }
      var camPub = p.getTrackPublication && p.getTrackPublication(LK.Track.Source.Camera);
      var hasVideo = !!(camPub && camPub.track && !camPub.isMuted);
      tl.root.classList.toggle('has-video', hasVideo);
      if (hasVideo) {
        camPub.track.attach(tl.video);
      } else if (tl.video.srcObject) {
        tl.video.srcObject = null;
      }
    }

    function syncAll() {
      if (!room) return;
      var present = {};
      [room.localParticipant].concat(Array.from(room.remoteParticipants.values())).forEach(function (p) {
        if (isHidden(p)) return;
        present[p.identity] = true;
        ensureTile(p, p === room.localParticipant);
        refreshTile(p);
      });
      Object.keys(tiles).forEach(function (id) { if (!present[id]) removeTile(id); });
      emit('participants', participants());
    }

    function participants() {
      if (!room) return [];
      return [room.localParticipant].concat(Array.from(room.remoteParticipants.values())).map(function (p) {
        return {
          identity: p.identity, name: p.name || p.identity, role: roleOf(p), isAgent: !!p.isAgent,
          local: p === room.localParticipant, mic: p.isMicrophoneEnabled, attributes: p.attributes || {},
          hidden: isHidden(p)
        };
      });
    }

    // Speaking rings and the orb follow the audio level, sampled per frame.
    function animate() {
      raf = requestAnimationFrame(animate);
      if (!room) return;
      Object.keys(tiles).forEach(function (id) {
        var tl = tiles[id];
        var lvl = Math.min(1, (tl.p.audioLevel || 0) * 2.2);
        tl.root.style.setProperty('--lvl', lvl.toFixed(3));
        tl.root.classList.toggle('is-speaking', tl.p.isSpeaking || lvl > 0.18);
      });
    }

    // ── Connection ──────────────────────────────────────────────────────
    function wire() {
      var E = LK.RoomEvent;
      room
        .on(E.ParticipantConnected, function (p) { syncAll(); emit('joined', p); })
        .on(E.ParticipantDisconnected, function (p) { removeTile(p.identity); syncAll(); emit('left', p); })
        .on(E.TrackSubscribed, function (track, pub, p) {
          if (track.kind === LK.Track.Kind.Audio) {
            var a = track.attach();
            a.dataset.identity = p.identity;
            audioBin.appendChild(a);
          }
          refreshTile(p);
        })
        .on(E.TrackUnsubscribed, function (track, pub, p) {
          track.detach().forEach(function (n) { n.remove(); });
          refreshTile(p);
        })
        .on(E.TrackMuted, function (pub, p) { refreshTile(p); emit('participants', participants()); })
        .on(E.TrackUnmuted, function (pub, p) { refreshTile(p); emit('participants', participants()); })
        .on(E.LocalTrackPublished, function (pub) {
          refreshTile(room.localParticipant);
          if (pub.source === LK.Track.Source.Microphone && opts.onLocalMic) opts.onLocalMic(pub.track);
        })
        .on(E.LocalTrackUnpublished, function () { refreshTile(room.localParticipant); })
        .on(E.ParticipantAttributesChanged, function (changed, p) { syncAll(); emit('attributes', p, changed); })
        .on(E.ParticipantNameChanged, function (n, p) { refreshTile(p); })
        .on(E.ConnectionQualityChanged, function (q, p) {
          if (p === room.localParticipant) emit('quality', String(q));
        })
        .on(E.AudioPlaybackStatusChanged, function () { emit('autoplay', room.canPlaybackAudio); })
        .on(E.Reconnecting, function () { emit('state', 'reconnecting'); })
        .on(E.Reconnected, function () { emit('state', 'connected'); syncAll(); })
        .on(E.Disconnected, function (reason) { emit('state', 'disconnected', reason); stopAnim(); })
        .on(E.DataReceived, function (payload, p, kind, topic) {
          var data = null;
          try { data = JSON.parse(new TextDecoder().decode(payload)); } catch (e) { return; }
          emit('data', data, p, topic);
        })
        .on(E.MediaDevicesError, function (err) { emit('deviceError', err); });
      window.addEventListener('resize', layout);
    }

    function stopAnim() { if (raf) cancelAnimationFrame(raf); raf = null; }

    function connect(wsUrl, token, o) {
      o = o || {};
      room = new LK.Room({
        adaptiveStream: true,
        dynacast: true,
        // The browser's own processing is the first line of defence against a
        // noisy kitchen or a speakerphone; a stronger denoiser can be attached
        // to the published track later (see onLocalMic).
        audioCaptureDefaults: {
          echoCancellation: true, noiseSuppression: true, autoGainControl: true,
          deviceId: o.micId || undefined
        },
        videoCaptureDefaults: { deviceId: o.camId || undefined, resolution: LK.VideoPresets.h540.resolution },
        publishDefaults: { simulcast: true, dtx: true, red: true },
        disconnectOnPageLeave: true
      });
      wire();
      emit('state', 'connecting');
      return room.connect(wsUrl, token, { autoSubscribe: true }).then(function () {
        emit('state', 'connected');
        syncAll();
        animate();
        emit('autoplay', room.canPlaybackAudio);
        var jobs = [];
        if (o.mic !== false) jobs.push(room.localParticipant.setMicrophoneEnabled(true).catch(function (e) { emit('deviceError', e); }));
        if (o.cam) jobs.push(room.localParticipant.setCameraEnabled(true).catch(function (e) { emit('deviceError', e); }));
        return Promise.all(jobs).then(function () { syncAll(); return room; });
      });
    }

    function disconnect() {
      stopAnim();
      if (room) return room.disconnect();
      return Promise.resolve();
    }

    function setMic(on) {
      if (!room) return Promise.resolve(false);
      return room.localParticipant.setMicrophoneEnabled(on).then(function () {
        syncAll(); return room.localParticipant.isMicrophoneEnabled;
      });
    }
    function setCam(on) {
      if (!room) return Promise.resolve(false);
      return room.localParticipant.setCameraEnabled(on).then(function () {
        syncAll(); return room.localParticipant.isCameraEnabled;
      });
    }

    function sendTo(identity, method, payload) {
      if (!room) return Promise.reject(new Error('not connected'));
      return room.localParticipant.performRpc({
        destinationIdentity: identity, method: method,
        payload: JSON.stringify(payload || {}), responseTimeout: 8000
      });
    }

    return {
      on: function (name, fn) { (handlers[name] = handlers[name] || []).push(fn); return this; },
      connect: connect,
      disconnect: disconnect,
      setMic: setMic,
      setCam: setCam,
      rpc: sendTo,
      participants: participants,
      startAudio: function () { return room ? room.startAudio() : Promise.resolve(); },
      get room() { return room; },
      micOn: function () { return !!(room && room.localParticipant.isMicrophoneEnabled); },
      camOn: function () { return !!(room && room.localParticipant.isCameraEnabled); },
      switchDevice: function (kind, id) { return room ? room.switchActiveDevice(kind, id) : Promise.resolve(); },
      activeDevice: function (kind) { return room ? room.getActiveDevice(kind) : ''; },
      relayout: layout
    };
  }

  // ── Devices & preview (lobby / pre-join) ─────────────────────────────
  function listDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return Promise.resolve([]);
    return navigator.mediaDevices.enumerateDevices();
  }

  /* A microphone level meter over a MediaStream, for the pre-join check. */
  function meter(stream, onLevel) {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx || !stream || !stream.getAudioTracks().length) return function () {};
    var ctx = new Ctx();
    var src = ctx.createMediaStreamSource(stream);
    var an = ctx.createAnalyser();
    an.fftSize = 512;
    src.connect(an);
    var buf = new Uint8Array(an.fftSize);
    var id = null;
    (function tick() {
      an.getByteTimeDomainData(buf);
      var peak = 0;
      for (var i = 0; i < buf.length; i++) { var v = Math.abs(buf[i] - 128) / 128; if (v > peak) peak = v; }
      onLevel(Math.min(1, peak * 1.6));
      id = requestAnimationFrame(tick);
    })();
    return function stop() { if (id) cancelAnimationFrame(id); try { ctx.close(); } catch (e) {} };
  }

  /* A short tone so someone can check they can hear before joining. */
  function testSpeaker(deviceId) {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    var ctx = new Ctx();
    var o = ctx.createOscillator(), g = ctx.createGain();
    o.type = 'sine';
    o.frequency.setValueAtTime(660, ctx.currentTime);
    o.frequency.setValueAtTime(880, ctx.currentTime + 0.18);
    g.gain.setValueAtTime(0.0001, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + 0.03);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.5);
    o.connect(g); g.connect(ctx.destination);
    if (deviceId && ctx.setSinkId) { try { ctx.setSinkId(deviceId); } catch (e) {} }
    o.start(); o.stop(ctx.currentTime + 0.55);
    setTimeout(function () { try { ctx.close(); } catch (e) {} }, 900);
  }

  /* Why getUserMedia failed, in words a person can act on. */
  function explainMediaError(err, t) {
    var name = (err && err.name) || '';
    t = t || {};
    if (name === 'NotAllowedError' || name === 'SecurityError') return t.permissionDenied || 'Permission was denied.';
    if (name === 'NotFoundError' || name === 'OverconstrainedError') return t.noDevice || 'No device was found.';
    if (name === 'NotReadableError') return t.deviceBusy || 'The device is in use by another app.';
    return t.deviceGeneric || 'The device could not be started.';
  }

  /* Small helpers both pages use. */
  function toast(container, text, kind) {
    if (!container) return;
    var n = el('div', 'rm-toast' + (kind === 'error' ? ' is-error' : ''));
    n.setAttribute('role', 'status');
    n.appendChild(icon(kind === 'error' ? 'error_outline' : 'info'));
    var s = el('div'); s.textContent = text; n.appendChild(s);
    container.appendChild(n);
    setTimeout(function () { n.classList.add('is-out'); setTimeout(function () { n.remove(); }, 250); }, 5200);
  }
  function post(url, data, csrf) {
    var body = new FormData();
    Object.keys(data || {}).forEach(function (k) { body.append(k, data[k]); });
    return fetch(url, {
      method: 'POST', credentials: 'same-origin', body: body,
      headers: { 'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) { var e = new Error(d.error || ('HTTP ' + r.status)); e.status = r.status; e.data = d; throw e; }
        return d;
      });
    });
  }
  function getJSON(url) {
    return fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) { var e = new Error(d.error || ('HTTP ' + r.status)); e.status = r.status; e.data = d; throw e; }
          return d;
        });
      });
  }
  function clock(startIso, node) {
    var start = startIso ? new Date(startIso).getTime() : Date.now();
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    function tick() {
      var s = Math.max(0, Math.floor((Date.now() - start) / 1000));
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
      node.textContent = (h ? h + ':' + pad(m) : m) + ':' + pad(x);
    }
    tick();
    return setInterval(tick, 1000);
  }

  window.CCRoom = {
    create: create, listDevices: listDevices, meter: meter, testSpeaker: testSpeaker,
    explainMediaError: explainMediaError, toast: toast, post: post, getJSON: getJSON,
    clock: clock, initials: initials, hue: hue, icon: icon, el: el
  };
})();
