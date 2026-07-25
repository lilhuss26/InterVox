const els = {
  setupPanel: document.getElementById("setupPanel"),
  interviewPanel: document.getElementById("interviewPanel"),
  reportPanel: document.getElementById("reportPanel"),
  startForm: document.getElementById("startForm"),
  resumeInput: document.getElementById("resumeInput"),
  jobDescriptionInput: document.getElementById("jobDescriptionInput"),
  numQuestionsInput: document.getElementById("numQuestionsInput"),
  modelSelect: document.getElementById("modelSelect"),
  startButton: document.getElementById("startButton"),
  connectionStatus: document.getElementById("connectionStatus"),
  sessionLabel: document.getElementById("sessionLabel"),
  questionAudio: document.getElementById("questionAudio"),
  questionText: document.getElementById("questionText"),
  listeningCard: document.getElementById("listeningCard"),
  liveStateTitle: document.getElementById("liveStateTitle"),
  recordingTimer: document.getElementById("recordingTimer"),
  interviewMessage: document.getElementById("interviewMessage"),
  errorToast: document.getElementById("errorToast"),
  reportSummary: document.getElementById("reportSummary"),
  overallScore: document.getElementById("overallScore"),
  recommendation: document.getElementById("recommendation"),
  dimensionScores: document.getElementById("dimensionScores"),
  strengths: document.getElementById("strengths"),
  weaknesses: document.getElementById("weaknesses"),
  improvementAreas: document.getElementById("improvementAreas"),
  studyRecommendations: document.getElementById("studyRecommendations"),
  communicationAdvice: document.getElementById("communicationAdvice"),
  missingConcepts: document.getElementById("missingConcepts"),
};

// End-of-turn detection tuning.
const calibrationMs = 400;      // sample ambient noise before we start scoring speech
const speechFactor = 2.5;       // voice must exceed noiseFloor * this to count as speech
const minThreshold = 0.012;     // absolute floor so a dead-silent room still needs real speech
const silenceLimitMs = 1200;    // sustained pause (hangover) that ends the turn
const maxAnswerMs = 90000;      // hard cap on a single answer
const minSpeechMs = 500;        // ignore blips shorter than this

let socket = null;
let sessionId = null;
let micStream = null;
let audioContext = null;
let mediaSource = null;
let analyser = null;
let mediaRecorder = null;
let recordedBlobs = [];
let recordingStartedAt = 0;
let speechStartedAt = 0;
let lastVoiceAt = 0;
let noiseFloor = 0;
let calibrating = false;
let calibrationSamples = [];
let timerId = null;
let vadId = null;
let receivedQuestionCount = 0;
let isCapturing = false;

function log(...args) {
  console.log("[interview]", ...args);
}

els.startForm.addEventListener("submit", startInterview);
els.modelSelect.addEventListener("change", selectModel);
els.questionAudio.addEventListener("ended", beginLiveAnswerCapture);

loadSelectedModel();

async function loadSelectedModel() {
  try {
    const response = await fetch("/api/interview/model");
    const payload = await response.json();
    if (response.ok && payload.model) {
      els.modelSelect.value = payload.model;
    }
  } catch (error) {
    log("failed to load model selection:", error.message);
  }
}

async function selectModel() {
  const model = els.modelSelect.value;
  try {
    const response = await fetch("/api/interview/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to switch model.");
    }
  } catch (error) {
    showError(error.message);
  }
}
els.questionAudio.addEventListener("play", () => {
  if (!isCapturing) {
    setLiveState("Question playing", "Listen to the full question. I will start listening when it ends.", "idle");
  }
});

async function startInterview(event) {
  event.preventDefault();
  hideError();

  const resume = els.resumeInput.files[0];
  const jobDescription = els.jobDescriptionInput.value.trim();
  if (!resume || !jobDescription) {
    showError("Resume PDF and job description are required.");
    return;
  }

  setStartLoading(true);
  setStatus("Requesting microphone");

  try {
    await ensureMicrophone();
    let numQuestions = parseInt(els.numQuestionsInput.value, 10);
    if (!Number.isFinite(numQuestions)) numQuestions = 5;
    numQuestions = Math.max(3, Math.min(15, numQuestions));

    const formData = new FormData();
    formData.append("resume", resume);
    formData.append("job_description", jobDescription);
    formData.append("num_questions", String(numQuestions));

    setStatus("Planning");
    const response = await fetch("/api/interview/start", {
      method: "POST",
      body: formData,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to start interview.");
    }

    sessionId = payload.session_id;
    receivedQuestionCount = 0;
    els.questionText.textContent = payload.first_question || "";
    els.sessionLabel.textContent = `Session ${sessionId}`;
    showPanel("interview");
    connectSocket();
  } catch (error) {
    showError(error.message);
    setStatus("Ready");
    stopMicrophone();
  } finally {
    setStartLoading(false);
  }
}

async function ensureMicrophone() {
  if (micStream) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("This browser does not support microphone recording.");
  }
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      // Automatic gain control constantly renormalizes the mic level, which pushes the
      // noise floor up during pauses and breaks silence/end-of-turn detection. Keep it off.
      autoGainControl: false,
    },
  });
}

function connectSocket() {
  if (socket) {
    socket.disconnect();
  }

  setLiveState("Connecting", "Connecting to the voice interview...", "idle");
  socket = io();

  socket.on("connect", () => {
    setStatus("Connected");
    socket.emit("join", { session_id: sessionId });
  });

  socket.on("disconnect", () => {
    setStatus("Disconnected");
    stopCapture(false);
  });

  socket.on("question", ({ audio }) => {
    log("received question audio | bytes=", normalizeBinary(audio).byteLength || (audio && audio.length));
    receivedQuestionCount += 1;
    if (receivedQuestionCount > 1) {
      els.questionText.textContent = "";
    }
    stopCapture(false);
    setLiveState("Question playing", "Listen to the question. Live listening starts automatically afterward.", "idle");
    playQuestionAudio(audio);
  });

  socket.on("finished", () => {
    log("interview finished");
    setStatus("Finished");
    stopCapture(false);
    stopMicrophone();
    setLiveState("Complete", "Interview complete. Loading report...", "idle");
    loadReport();
  });

  socket.on("error", ({ message }) => {
    log("socket error:", message);
    showError(message || "Interview socket error.");
    setLiveState("Error", "An error occurred. Check the message and try again.", "idle");
    stopCapture(false);
  });
}

function playQuestionAudio(audio) {
  const bytes = normalizeBinary(audio);
  const blob = new Blob([bytes], { type: "audio/mpeg" });
  const url = URL.createObjectURL(blob);
  if (els.questionAudio.src) {
    URL.revokeObjectURL(els.questionAudio.src);
  }
  els.questionAudio.src = url;
  els.questionAudio.play().catch(() => {
    // Autoplay was blocked. Don't strand the mic waiting for an "ended" event that will
    // never fire: prompt the user to play, and begin listening shortly regardless so they
    // can still answer.
    setLiveState("Question ready", "Press play to hear the question. Listening will start automatically.", "idle");
    window.setTimeout(() => {
      if (!isCapturing) beginLiveAnswerCapture();
    }, 1200);
  });
}

async function beginLiveAnswerCapture() {
  if (!socket || !sessionId || isCapturing) return;

  try {
    await ensureMicrophone();
    startCapture();
  } catch (error) {
    showError(error.message);
    setLiveState("Microphone blocked", "Allow microphone access, then restart the interview.", "idle");
  }
}

function startCapture() {
  stopCapture(false);
  recordedBlobs = [];

  // AnalyserNode is used only for voice-activity / end-of-turn detection.
  audioContext = new AudioContext();
  if (audioContext.state === "suspended") audioContext.resume();
  mediaSource = audioContext.createMediaStreamSource(micStream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 1024;
  mediaSource.connect(analyser);

  // MediaRecorder does the actual capture. It is far more reliable than ScriptProcessorNode
  // and produces webm/opus, which the server's Whisper model decodes directly.
  const mimeType = pickRecorderMime();
  mediaRecorder = new MediaRecorder(micStream, mimeType ? { mimeType } : undefined);
  mediaRecorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) recordedBlobs.push(event.data);
  };
  mediaRecorder.start();
  log("capture started | mime=", mediaRecorder.mimeType, "| sampleRate=", audioContext.sampleRate);

  recordingStartedAt = Date.now();
  speechStartedAt = 0;
  lastVoiceAt = 0;
  noiseFloor = 0;
  calibrating = true;
  calibrationSamples = [];
  isCapturing = true;
  timerId = window.setInterval(updateTimer, 250);
  vadId = window.setInterval(checkVoiceActivity, 100);
  updateTimer();
  setLiveState("Listening", "Calibrating microphone... then start speaking.", "listening");
}

function pickRecorderMime() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return "";
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function currentRms() {
  const data = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(data);
  let sum = 0;
  for (const sample of data) sum += sample * sample;
  return Math.sqrt(sum / data.length);
}

function checkVoiceActivity() {
  if (!analyser || !isCapturing) return;

  const rms = currentRms();
  const now = Date.now();

  // Phase 1: learn the ambient noise floor so the speech threshold adapts to the room.
  if (calibrating) {
    calibrationSamples.push(rms);
    if (now - recordingStartedAt >= calibrationMs) {
      calibrationSamples.sort((a, b) => a - b);
      noiseFloor = calibrationSamples[Math.floor(calibrationSamples.length / 2)] || 0;
      calibrating = false;
      log("calibrated | noiseFloor=", noiseFloor.toFixed(4), "| speechThreshold=", Math.max(noiseFloor * speechFactor, minThreshold).toFixed(4));
      setLiveState("Listening", "Start speaking. I will submit automatically after you pause.", "listening");
    }
    return;
  }

  const threshold = Math.max(noiseFloor * speechFactor, minThreshold);

  if (rms > threshold) {
    if (!speechStartedAt) {
      speechStartedAt = now;
      log("speech detected | rms=", rms.toFixed(4));
    }
    lastVoiceAt = now;
    setLiveState("Listening", "I can hear you. Keep going until your answer is complete.", "speaking");
    return;
  }

  if (!speechStartedAt) {
    if (now - recordingStartedAt > 12000) {
      setLiveState("Listening", "I am still listening. Start your answer when ready.", "listening");
    }
    return;
  }

  // Phase 2: the speaker has talked and then gone quiet long enough -> end of turn.
  const speechDuration = lastVoiceAt - speechStartedAt;
  const silenceDuration = now - lastVoiceAt;
  const totalDuration = now - recordingStartedAt;
  if ((speechDuration >= minSpeechMs && silenceDuration >= silenceLimitMs) || totalDuration >= maxAnswerMs) {
    finishAndSubmitAnswer();
  } else {
    setLiveState("Listening", "Brief pause detected. Continue speaking or pause to submit.", "listening");
  }
}

function finishAndSubmitAnswer() {
  if (!isCapturing || !mediaRecorder) return;

  // Stop scoring immediately so this only fires once, but keep the recorder alive until its
  // final "stop" event flushes the last chunk.
  isCapturing = false;
  if (vadId) window.clearInterval(vadId);
  if (timerId) window.clearInterval(timerId);
  vadId = null;
  timerId = null;

  const hadSpeech = Boolean(speechStartedAt);
  const recorder = mediaRecorder;
  recorder.onstop = async () => {
    teardownAudioNodes();
    mediaRecorder = null;

    if (!hadSpeech || !recordedBlobs.length) {
      log("no speech captured -> restarting capture | hadSpeech=", hadSpeech, "| blobs=", recordedBlobs.length);
      setLiveState("Listening", "I did not catch enough audio. Listening again...", "listening");
      beginLiveAnswerCapture();
      return;
    }

    const blob = new Blob(recordedBlobs, { type: recorder.mimeType || "audio/webm" });
    const buffer = await blob.arrayBuffer();
    log(
      "submitting answer | bytes=", buffer.byteLength,
      "| speechMs=", lastVoiceAt - speechStartedAt,
      "| totalMs=", Date.now() - recordingStartedAt,
    );
    setLiveState("Processing", "Submitting your answer for transcription and evaluation...", "idle");
    socket.emit("answer", { session_id: sessionId, audio: buffer });
  };

  try {
    recorder.stop();
  } catch (error) {
    recorder.onstop = null;
    teardownAudioNodes();
    mediaRecorder = null;
  }
}

function teardownAudioNodes() {
  if (mediaSource) mediaSource.disconnect();
  if (analyser) analyser.disconnect();
  if (audioContext && audioContext.state !== "closed") audioContext.close();
  mediaSource = null;
  analyser = null;
  audioContext = null;
}

function stopCapture(closeStream) {
  if (vadId) window.clearInterval(vadId);
  if (timerId) window.clearInterval(timerId);
  vadId = null;
  timerId = null;

  if (mediaRecorder) {
    // Cancel any pending submit and discard the in-flight recording.
    mediaRecorder.onstop = null;
    if (mediaRecorder.state !== "inactive") {
      try {
        mediaRecorder.stop();
      } catch (error) {
        /* recorder already stopping */
      }
    }
    mediaRecorder = null;
  }
  recordedBlobs = [];

  teardownAudioNodes();
  if (closeStream) stopMicrophone();

  isCapturing = false;
  els.recordingTimer.textContent = "00:00";
}

function stopMicrophone() {
  if (!micStream) return;
  micStream.getTracks().forEach((track) => track.stop());
  micStream = null;
}

async function loadReport() {
  showPanel("report");
  try {
    const response = await fetch(`/api/interview/${sessionId}/report`);
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to load report.");
    }
    renderReport(payload);
  } catch (error) {
    showError(error.message);
    els.reportSummary.textContent = "Report could not be loaded.";
  }
}

function renderReport(payload) {
  const report = payload.final_report || {};
  const notes = payload.coaching_notes || {};

  els.reportSummary.textContent = report.summary || "No summary available.";
  els.overallScore.textContent = formatScore(report.overall_score);
  setOverallScore(report.overall_score);
  els.recommendation.textContent = report.recommendation || "No recommendation available.";

  renderScores(report.scores_by_dimension || {});
  renderList(els.strengths, report.strengths);
  renderList(els.weaknesses, report.weaknesses);
  renderList(els.improvementAreas, notes.improvement_areas);
  renderList(els.studyRecommendations, notes.study_recommendations);
  renderList(els.communicationAdvice, notes.communication_advice);
  renderList(els.missingConcepts, notes.missing_concepts);
}

function renderScores(scores) {
  els.dimensionScores.innerHTML = "";
  const entries = Object.entries(scores);
  if (!entries.length) {
    els.dimensionScores.textContent = "No dimension scores available.";
    return;
  }
  for (const [name, score] of entries) {
    const percent = scorePercent(score);
    const row = document.createElement("div");
    row.className = "score-row";
    row.innerHTML = `
      <div class="score-row-top">
        <span>${escapeHtml(name)}</span>
        <strong>${escapeHtml(formatScore(score))}</strong>
      </div>
      <div class="score-track"><span class="score-fill" style="--score-width: ${percent}%"></span></div>
    `;
    els.dimensionScores.appendChild(row);
  }
}

function renderList(target, values) {
  target.innerHTML = "";
  const items = Array.isArray(values) ? values : [];
  if (!items.length) {
    const li = document.createElement("li");
    li.textContent = "No items available.";
    target.appendChild(li);
    return;
  }
  for (const value of items) {
    const li = document.createElement("li");
    li.textContent = value;
    target.appendChild(li);
  }
}

function normalizeBinary(value) {
  if (value instanceof ArrayBuffer) return value;
  if (ArrayBuffer.isView(value)) return value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
  if (Array.isArray(value)) return new Uint8Array(value).buffer;
  return value;
}

function updateTimer() {
  const elapsed = Math.floor((Date.now() - recordingStartedAt) / 1000);
  const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
  const seconds = String(elapsed % 60).padStart(2, "0");
  els.recordingTimer.textContent = `${minutes}:${seconds}`;
}

function setStartLoading(isLoading) {
  els.startButton.disabled = isLoading;
  els.startButton.textContent = isLoading ? "Starting live interview..." : "Start live interview";
}

function setStatus(value) {
  els.connectionStatus.textContent = value;
}

function setLiveState(title, message, state) {
  els.liveStateTitle.textContent = title;
  els.interviewMessage.textContent = message;
  els.listeningCard.classList.toggle("is-listening", state === "listening");
  els.listeningCard.classList.toggle("is-speaking", state === "speaking");
}

function showPanel(name) {
  els.setupPanel.classList.toggle("hidden", name !== "setup");
  els.interviewPanel.classList.toggle("hidden", name !== "interview");
  els.reportPanel.classList.toggle("hidden", name !== "report");
}

function showError(message) {
  els.errorToast.textContent = message;
  els.errorToast.classList.remove("hidden");
}

function hideError() {
  els.errorToast.classList.add("hidden");
  els.errorToast.textContent = "";
}

function formatScore(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  return numeric.toFixed(1);
}

function scorePercent(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.max(0, Math.min(100, numeric * 10));
}

function setOverallScore(value) {
  const ring = document.getElementById("scoreRing");
  if (!ring) return;
  ring.style.setProperty("--score-angle", `${scorePercent(value) * 3.6}deg`);
}

function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = String(value);
  return span.innerHTML;
}
