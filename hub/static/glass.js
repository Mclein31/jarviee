/* glass.js: puts the owner's wallpaper behind the glass and sets the veil from its brightness.

   The photo comes from /api/wallpaper (the macOS desktop by default, Settings → Display → Background). Before it is
   shown, it is drawn small onto a canvas and its bright end (the 85th percentile of luminance) sets --veil: a pale
   wallpaper gets a heavier veil, a night photo a light one, so the ground under the panes stays about as dark as
   #2e2e2e and every text step keeps AA contrast whatever the desktop is. Checked again every two minutes, so a new
   desktop picture shows up without a reload. */
"use strict";
(() => {
  const root = document.documentElement;
  // ground target (sRGB 0-255): light enough that the photo's colour reads through the frost; the panes darken it
  // further (--pane-a) to about #2e for text. TINT is the veil colour, DIM the wall's own dimming.
  const TARGET = 74, PANE = 46, TINT = 15, DIM = .92;
  let shown = "";
  const G = window.JarvieeGlass = {info: null, reload};

  function veilFor(img) {
    try {
      const c = document.createElement("canvas"); c.width = 32; c.height = 20;
      const x = c.getContext("2d", {willReadFrequently: true});
      x.drawImage(img, 0, 0, 32, 20);
      const d = x.getImageData(0, 0, 32, 20).data, v = [];
      for (let i = 0; i < d.length; i += 4) v.push(.2126 * d[i] + .7152 * d[i + 1] + .0722 * d[i + 2]);
      v.sort((a, b) => a - b);
      // a vivid photo is calmed too: colour behind glass is a glow, never a fill that fights the status hues
      let sat = 0;
      for (let i = 0; i < d.length; i += 4) { const mx = Math.max(d[i], d[i + 1], d[i + 2]), mn = Math.min(d[i], d[i + 1], d[i + 2]); sat += mx ? (mx - mn) / mx : 0; }
      sat /= d.length / 4;
      const bright = v[Math.floor(v.length * .85)] * DIM;
      const veil = Math.min(.86, Math.max(.36 + sat * .28, (bright - TARGET) / Math.max(bright - TINT, 1)));
      const ground = veil * TINT + (1 - veil) * bright;                       // what a pane sits on
      const pane = Math.min(.62, Math.max(.22 + sat * .1, (ground - PANE) / Math.max(ground - TINT, 1)));
      // colour: a grey photo (Big Sur Road) is pushed so its blues and warms read through the frost; a vivid one held back
      const wallSat = Math.min(1.45, Math.max(.85, 1.6 - sat * 1.4)), paneSat = Math.round(Math.min(190, Math.max(135, 190 - sat * 110)));
      return [veil, pane, wallSat, paneSat];
    } catch (e) { return [.62, .3, .92, 150]; }
  }

  async function reload(force) {
    let info;
    try { info = await (await fetch("/api/wallpaper/info", {cache: "no-store"})).json(); } catch (e) { return; }
    G.info = info;
    const v = info.v || "none";
    if (v === shown && !force) return;
    if (v === "none") { shown = v; root.dataset.wall = "none"; root.style.setProperty("--wall-url", "none"); root.style.setProperty("--veil", ".5"); root.style.setProperty("--pane-a", ".16"); return; }
    if (!shown) root.dataset.wall = "loading";
    const url = `/api/wallpaper?v=${encodeURIComponent(v)}`, img = new Image();
    img.decoding = "async";
    img.onload = () => {
      shown = v;
      const [veil, pane, wallSat, paneSat] = veilFor(img);
      root.style.setProperty("--veil", veil.toFixed(3));
      root.style.setProperty("--pane-a", pane.toFixed(3));
      root.style.setProperty("--wall-sat", wallSat.toFixed(2));
      root.style.setProperty("--pane-sat", paneSat + "%");
      root.style.setProperty("--wall-url", `url("${url}")`);
      root.dataset.wall = "on";
    };
    img.onerror = () => { root.dataset.wall = "none"; };
    img.src = url;
  }

  reload();
  setInterval(() => { if (!document.hidden) reload(); }, 120000);
})();
