// audio_chat.js
// Make sure to serve this alongside index.html

// If your backend injects a global URL, you can use window.PROCESS_AUDIO_URL instead.
const PROCESS_AUDIO_URL = window.PROCESS_AUDIO_URL || '/chat/external/audio/process/';

let mediaRecorder, audioCtx, analyser, silenceTimeout;
const recBtn    = document.getElementById('recordButton'),
      recInd   = document.getElementById('recordingIndicator'),
      thinkInd = document.getElementById('thinkingIndicator'),
      overlay  = document.getElementById('overlay'),
      msgsDiv  = document.getElementById('messages');

recBtn.addEventListener('click', startRecording);

async function startRecording() {
  recBtn.disabled = true;
  recBtn.classList.add('pulsating');
  recInd.classList.remove('hidden');

  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const src = audioCtx.createMediaStreamSource(stream);
  analyser = audioCtx.createAnalyser();
  src.connect(analyser);
  analyser.fftSize = 2048;

  mediaRecorder = new MediaRecorder(stream);
  let chunks = [];
  mediaRecorder.ondataavailable = e => chunks.push(e.data);
  mediaRecorder.onstop = () => uploadAudio(new Blob(chunks));
  mediaRecorder.start();

  monitorSilence();
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
    recInd.classList.add('hidden');
    recBtn.disabled = false;
    recBtn.classList.remove('pulsating');
    clearTimeout(silenceTimeout);
  }
}

function monitorSilence() {
  let silentSince = Date.now();
  (function loop() {
    let arr = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(arr);
    let rms = Math.sqrt(arr.reduce((sum, v) =>
      sum + ((v - 128) / 128) ** 2, 0) / arr.length);
    if (rms > 0.05) silentSince = Date.now();
    if (Date.now() - silentSince > 2000) return stopRecording();
    silenceTimeout = setTimeout(loop, 200);
  })();
}

function uploadAudio(blob) {
  thinkInd.classList.remove('hidden');
  overlay.style.display = 'block';
  recBtn.disabled = true;
  recBtn.classList.remove('pulsating');

  // Stop any previous response audio
  document.querySelectorAll('audio.response-audio').forEach(a => {
    a.pause();
    a.currentTime = 0;
    a.autoplay = false;
  });

  const data = new FormData();
  data.append('audio_data', blob, 'rec.webm');

  fetch(PROCESS_AUDIO_URL, {
    method: 'POST',
    body: data
  })
  .then(r => r.json())
  .then(d => {
    thinkInd.classList.add('hidden');
    overlay.style.display = 'none';

    // show transcript & response text
    msgsDiv.innerHTML += `<p><strong>You:</strong> ${d.transcript}</p>`;
    msgsDiv.innerHTML += `<p><strong>Bot:</strong> ${d.response}</p>`;

    // user audio (no autoplay)
    const userAudio = document.createElement('audio');
    userAudio.controls = true;
    userAudio.src = d.input_audio;
    msgsDiv.appendChild(userAudio);

    // response audio (only this one autoplays)
    const respAudio = document.createElement('audio');
    respAudio.classList.add('response-audio');
    respAudio.controls = true;
    respAudio.src = d.response_audio;
    respAudio.autoplay = true;
    msgsDiv.appendChild(respAudio);

    msgsDiv.scrollTop = msgsDiv.scrollHeight;

    // when this response finishes, restart recording
    respAudio.addEventListener('ended', () => startRecording());
  })
  .catch(err => {
    console.error(err);
    thinkInd.classList.add('hidden');
    overlay.style.display = 'none';
    recBtn.disabled = false;
  });
}