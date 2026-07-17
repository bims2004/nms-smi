/* =========================================================
 * Alarm gangguan.
 *
 * Semua suara DISINTESIS lewat Web Audio API — tidak ada file audio,
 * tidak ada request keluar. Alasannya dua: dashboard ini harus tetap
 * jalan di jaringan management yang terisolasi tanpa internet, dan
 * sampel suara yang beredar di internet umumnya berhak cipta.
 *
 * Kalau mau memakai file sendiri, lihat NmsAlarm.setCustom() di bawah.
 * ========================================================= */
(function () {
  "use strict";

  var ctx = null;
  var unlocked = false;

  function audioCtx() {
    if (!ctx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    return ctx;
  }

  /* Browser menolak memutar audio sebelum ada interaksi pengguna.
     Ini bukan bug yang bisa diakali — harus ada klik dulu. */
  function unlock() {
    var c = audioCtx();
    if (!c) return false;
    if (c.state === "suspended") c.resume();
    unlocked = true;
    return true;
  }

  function env(gain, t0, attack, hold, release, peak) {
    gain.gain.setValueAtTime(0.0001, t0);
    gain.gain.exponentialRampToValueAtTime(peak, t0 + attack);
    gain.gain.setValueAtTime(peak, t0 + attack + hold);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + attack + hold + release);
  }

  function noiseBuffer(c, dur) {
    var n = Math.floor(c.sampleRate * dur);
    var buf = c.createBuffer(1, n, c.sampleRate);
    var d = buf.getChannelData(0);
    for (var i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    return buf;
  }

  /* ---------- suara ---------- */

  /* Kaget: hentakan tajam lalu jatuh ke bass.
     Serangan nyaris nol supaya benar-benar mengejutkan; ekornya rendah
     supaya terasa di dada kalau speakernya lumayan. */
  function playKaget(vol) {
    var c = audioCtx(); if (!c) return;
    var t = c.currentTime;

    // hentakan awal: derau pendek, di-filter supaya tidak cempreng
    var src = c.createBufferSource();
    src.buffer = noiseBuffer(c, 0.12);
    var hp = c.createBiquadFilter();
    hp.type = "bandpass"; hp.frequency.value = 1800; hp.Q.value = 0.7;
    var ng = c.createGain();
    env(ng, t, 0.001, 0.01, 0.10, 0.5 * vol);
    src.connect(hp); hp.connect(ng); ng.connect(c.destination);
    src.start(t); src.stop(t + 0.13);

    // ekor bass: sapuan turun cepat
    var osc = c.createOscillator();
    osc.type = "sine";
    osc.frequency.setValueAtTime(150, t);
    osc.frequency.exponentialRampToValueAtTime(38, t + 0.5);
    var og = c.createGain();
    env(og, t, 0.004, 0.06, 0.5, 0.9 * vol);
    // sedikit saturasi supaya terdengar di speaker kecil yang tak punya bass
    var shaper = c.createWaveShaper();
    var curve = new Float32Array(257);
    for (var i = 0; i < 257; i++) {
      var x = (i / 128) - 1;
      curve[i] = Math.tanh(x * 2.2);
    }
    shaper.curve = curve;
    osc.connect(og); og.connect(shaper); shaper.connect(c.destination);
    osc.start(t); osc.stop(t + 0.62);
  }

  /* Sirene: dua nada bergantian, seperti alarm ruang kontrol. */
  function playSirene(vol) {
    var c = audioCtx(); if (!c) return;
    var t = c.currentTime;
    var osc = c.createOscillator();
    osc.type = "square";
    var g = c.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.22 * vol, t + 0.02);

    var lp = c.createBiquadFilter();
    lp.type = "lowpass"; lp.frequency.value = 2200;

    for (var i = 0; i < 4; i++) {
      osc.frequency.setValueAtTime(760, t + i * 0.44);
      osc.frequency.setValueAtTime(560, t + i * 0.44 + 0.22);
    }
    g.gain.setValueAtTime(0.22 * vol, t + 1.7);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 1.78);
    osc.connect(g); g.connect(lp); lp.connect(c.destination);
    osc.start(t); osc.stop(t + 1.8);
  }

  /* Bip: tiga nada pendek. Untuk yang tidak mau kaget. */
  function playBip(vol, n) {
    var c = audioCtx(); if (!c) return;
    var t = c.currentTime;
    n = n || 3;
    for (var i = 0; i < n; i++) {
      var osc = c.createOscillator();
      osc.type = "triangle";
      osc.frequency.value = 920;
      var g = c.createGain();
      var t0 = t + i * 0.16;
      env(g, t0, 0.005, 0.05, 0.06, 0.3 * vol);
      osc.connect(g); g.connect(c.destination);
      osc.start(t0); osc.stop(t0 + 0.13);
    }
  }

  /* Pulih: dua nada naik. Sengaja dibedakan jauh dari suara gangguan
     supaya tidak perlu melihat layar untuk tahu ini kabar baik. */
  function playPulih(vol) {
    var c = audioCtx(); if (!c) return;
    var t = c.currentTime;
    [660, 990].forEach(function (f, i) {
      var osc = c.createOscillator();
      osc.type = "sine";
      osc.frequency.value = f;
      var g = c.createGain();
      var t0 = t + i * 0.13;
      env(g, t0, 0.01, 0.06, 0.16, 0.26 * vol);
      osc.connect(g); g.connect(c.destination);
      osc.start(t0); osc.stop(t0 + 0.25);
    });
  }

  var custom = null;   // HTMLAudioElement kalau pengguna pasang file sendiri

  var SOUNDS = {
    kaget:  playKaget,
    sirene: playSirene,
    bip:    playBip,
    pulih:  playPulih,
  };

  function play(name, vol) {
    if (!unlocked) return false;
    if (name === "custom" && custom) {
      custom.volume = Math.min(1, vol);
      custom.currentTime = 0;
      var p = custom.play();
      if (p && p.catch) p.catch(function () { /* diblok browser */ });
      return true;
    }
    var fn = SOUNDS[name];
    if (!fn) return false;
    try {
      fn(vol);
      return true;
    } catch (e) {
      return false;
    }
  }

  window.NmsAlarm = {
    unlock: unlock,
    play: play,
    isUnlocked: function () { return unlocked; },
    supported: function () {
      return !!(window.AudioContext || window.webkitAudioContext);
    },
    /* Pasang file sendiri, mis. NmsAlarm.setCustom('/static/alarm/punya-saya.mp3')
       Lisensi file yang kamu pasang jadi tanggung jawabmu. */
    setCustom: function (url) {
      custom = new Audio(url);
      custom.preload = "auto";
    },
    hasCustom: function () { return custom !== null; },
  };
})();
