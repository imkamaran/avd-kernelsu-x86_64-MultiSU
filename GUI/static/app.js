"use strict";
// AVD Root Panel - front end for GUI/server.py (which drives avd-ksu.py).

const TOKEN = document.querySelector('meta[name="gui-token"]').content;
const $ = (id) => document.getElementById(id);

const VARIANTS = {
  kernelsu: {
    label: "KernelSU",
    manager: (v) => `https://github.com/tiann/KernelSU/releases/tag/${v}`,
    note: "Official KernelSU. Install the KernelSU manager of the same version. Works on 16 KB page-size images.",
    ok16k: true,
  },
  "kernelsu-next": {
    label: "KernelSU-Next",
    manager: (v) => `https://github.com/KernelSU-Next/KernelSU-Next/releases/tag/${v}`,
    note: "KernelSU-Next. Install the KernelSU-Next manager of the same version. On 16 KB images its ksud is not 16 KB aligned, so root does not activate.",
    ok16k: false,
  },
  "kernelsu-next-susfs": {
    label: "KernelSU-Next + SUSFS (preview)",
    manager: (v, rel) => (rel && rel.url) || "https://github.com/leemikepop/avd-kernelsu-x86_64/releases",
    note: "Preview builds (prereleases) from the upstream repository. Install the manager APK attached to the same release, then use “Install SUSFS module” on the rooted AVD. No root on 16 KB images.",
    ok16k: false,
    susfs: true,
  },
  finasu: {
    label: "FinaSU",
    manager: (v) => `https://github.com/LyraVoid/FinaSU/releases/tag/${v}`,
    note: "FinaSU: KernelSU fork with FolkZygisk, Sui and SUSFS support. Install the FinaSU manager of the same version. Tested on Android 17 (API 37.0).",
    ok16k: null,
  },
};

const S = {
  info: null, avds: [], devices: [], startedHere: {}, sel: null,
  releases: {},            // "variant|repo" -> /api/releases result
  job: null, jobSince: 0,  // job shown in the console
};

const store = {
  get(k, d) { try { const v = localStorage.getItem("avdgui." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("avdgui." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

// ---------------------------------------------------------------- helpers
function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}
const chip = (text, kind = "") => h("span", { class: `chip ${kind}` }, text);

async function api(path, opts = {}) {
  const headers = { "X-GUI-Token": TOKEN, ...(opts.headers || {}) };
  let body = opts.body;
  if (body !== undefined && !(body instanceof Blob)) { body = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, { method: opts.method || (body !== undefined ? "POST" : "GET"), headers, body });
  let data;
  try { data = await r.json(); } catch { data = { error: `HTTP ${r.status}` }; }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3500);
}
const fail = (e) => toast(e.message || String(e), true);

const current = () => S.avds.find((a) => a.name === S.sel) || null;
const deviceOf = (name) => S.devices.find((d) => d.avd === name) || null;
const variant = () => $("variant").value;
const version = () => $("version").value;
const repo = () => $("repo").value.trim();

// ---------------------------------------------------------------- environment
function renderEnv() {
  const i = S.info;
  const env = $("env");
  env.replaceChildren(
    chip(i.sdk ? "Android SDK" : "SDK not found", i.sdk ? "ok" : "bad"),
    chip(i.adb ? "adb" : "adb missing", i.adb ? "ok" : "bad"),
    chip(i.emulator ? "emulator" : "emulator missing", i.emulator ? "ok" : "bad"),
    chip(`Python ${i.python}`, "ok"),
  );
  const rows = [
    ["avd-ksu.py", i.avd_ksu], ["Android SDK", i.sdk || "not found: set ANDROID_SDK_ROOT"], ["AVD folder", i.avd_home],
    ["adb", i.adb || "not found"], ["emulator", i.emulator || "not found"], ["Download cache", i.cache],
    ["Patched ramdisks", i.out], ["Logs", i.logs],
    ...Object.entries(i.repos).map(([v, r]) => [`Releases: ${v}`, r]),
  ];
  $("paths").replaceChildren(...rows.flatMap(([k, v]) => [h("span", {}, k), h("span", {}, v)]));
}

// ---------------------------------------------------------------- AVD list
function ramdiskChip(state) {
  if (!state) return null;
  if (state.startsWith("patched with")) return chip(state.replace(/ \(.*\)$/, ""), "ok");
  if (state === "stock") return chip("SDK ramdisk: stock");
  return chip(state, "warn");
}

function renderAvds() {
  const list = $("avd-list");
  if (!S.avds.length) {
    list.replaceChildren(h("p", { class: "muted" }, `No AVDs found in ${S.info ? S.info.avd_home : "the AVD folder"}. Create one in Android Studio's Device Manager (x86_64, Google Play image).`));
    return;
  }
  list.replaceChildren(...S.avds.map((a) => {
    const dev = deviceOf(a.name);
    const meta = [a.api && `API ${a.api}`, a.tag, a.abi].filter(Boolean).join(" · ");
    return h("button", { class: "avd", "aria-pressed": a.name === S.sel ? "true" : "false", onclick: () => selectAvd(a.name) },
      h("div", { class: "avd-top" }, h("span", { class: "avd-name" }, a.display || a.name),
        dev ? chip(dev.booted ? "running" : "booting", dev.booted ? "ok" : "warn") : null),
      h("div", { class: "avd-meta" }, meta),
      a.kernel ? h("div", { class: "avd-meta" }, a.kernel) : null,
      a.error ? h("div", { class: "avd-meta" }, `⚠ ${a.error}`) : null,
      h("div", { class: "badges" },
        a.error ? null : a.target ? chip(a.target.label, "ok") : chip("kernel not supported", "bad"),
        a.page_kb === 16 ? chip("16 KB pages", "warn") : null,
        ramdiskChip(a.ramdisk),
        a.images.length ? chip(`${a.images.length} patched image${a.images.length > 1 ? "s" : ""}`) : null),
    );
  }));
}

async function loadAvds() {
  try {
    S.avds = (await api("/api/avds")).avds;
  } catch (e) { fail(e); return; }
  if (S.sel && !current()) S.sel = null;
  if (!S.sel) {
    const remembered = store.get("avd", null);
    S.sel = (S.avds.find((a) => a.name === remembered) || S.avds.find((a) => a.target) || {}).name || null;
  }
  renderAvds();
  renderPanel();
}

function selectAvd(name) {
  S.sel = name;
  store.set("avd", name);
  renderAvds();
  renderPanel();
}

// ---------------------------------------------------------------- control panel
function renderPanel() {
  const a = current();
  $("panel-empty").hidden = !!a;
  $("panel-body").hidden = !a;
  $("panel-avd").textContent = a ? a.name : "Select an AVD";
  if (!a) return;
  const dl = h("dl", { class: "summary" });
  for (const [k, v] of [
    ["Kernel", a.kernel || a.error || "?"],
    ["Module target", a.target ? `${a.target.id} (${a.target.kmi})` : "none: no prebuilt module for this kernel"],
    ["System image", a.sysdir || "?"],
    ["SDK ramdisk", a.ramdisk || "?"],
    ["Page size", `${a.page_kb} KB`],
  ]) dl.append(h("dt", {}, k), h("dd", {}, v));
  $("avd-summary").replaceChildren(dl);
  renderImages();
  updateState();
  updateNotes();
}

function renderImages() {
  const a = current();
  if (!a) return;
  const sel = $("image");
  const prev = sel.value;
  const sdk = a.ramdisk && a.ramdisk.startsWith("patched with") ? `SDK ramdisk (${a.ramdisk})` : "SDK ramdisk (stock: no root)";
  const imgs = [...a.images].sort((x, y) => y.mtime - x.mtime);
  sel.replaceChildren(
    h("option", { value: "" }, sdk),
    ...imgs.map((i) => h("option", { value: i.file },
      `${i.variant || "?"} ${i.version || ""} · patched ${new Date(i.mtime * 1000).toLocaleString()}`)),
  );
  const want = store.get("image." + a.name, null);
  const options = [...sel.options].map((o) => o.value);
  sel.value = options.includes(prev) && prev ? prev : options.includes(want) ? want : (imgs[0] ? imgs[0].file : "");
}

function updateState() {
  const a = current();
  if (!a) return;
  const dev = deviceOf(a.name);
  const running = !!dev;
  const booted = !!(dev && dev.booted);
  const v = variant();
  const hasVersion = !!version() && !$("version").selectedOptions[0]?.disabled;
  const busy = S.job && (S.job.state === "running" || S.job.state === "queued");
  const canPatch = !!a.target && !a.error && hasVersion && !busy;
  $("do-patch").disabled = !canPatch;
  $("do-patch-boot").disabled = !canPatch || running;
  $("do-status").disabled = busy;
  $("do-restore").disabled = !a.backup || busy;
  $("do-start").disabled = running || !!a.error;
  $("do-stop").disabled = !running;
  $("do-del-image").disabled = !$("image").value;
  $("do-check").disabled = !booted;
  $("apk").disabled = !booted;
  $("apk").parentElement.toggleAttribute("aria-disabled", !booted);
  $("susfs-actions").hidden = !VARIANTS[v]?.susfs;
  $("do-susfs").disabled = $("do-susfs-remove").disabled = !booted || busy;
  $("device-state").textContent = !dev
    ? "Not running."
    : `Running as ${dev.serial}: ${dev.booted ? "booted" : dev.state === "device" ? "booting…" : dev.state}.`
      + (S.startedHere[a.name]?.running ? ` Started here with ${S.startedHere[a.name].image || "the SDK ramdisk"}.` : "");
}

function updateNotes() {
  const a = current();
  const v = variant();
  const info = VARIANTS[v] || { note: "", manager: () => "#" };
  $("variant-note").textContent = info.note;
  const rel = currentReleases()?.releases.find((r) => r.version === version());
  const link = $("manager-link");
  link.href = version() ? info.manager(version(), rel) : "#";
  link.textContent = `Get the ${info.label || v} manager${version() ? " " + version() : ""}`;
  const warn = [];
  if (a && !a.target && !a.error) warn.push(`No prebuilt module for kernel ${a.kernel}. Update the system image in SDK Manager (Google Play x86_64), or pick another AVD.`);
  if (a && a.page_kb === 16 && info.ok16k === false) warn.push(`This is a 16 KB page-size image: ${info.label} root does not activate here${v === "kernelsu-next-susfs" ? " (patching it needs Force)" : ""}. KernelSU works on 16 KB images.`);
  if (a && a.page_kb === 16 && v === "finasu") warn.push("FinaSU on 16 KB images: the boot-loop fix is in, but root has not been re-tested on these images yet.");
  $("patch-warn").hidden = !warn.length;
  $("patch-warn").textContent = warn.join(" ");
}

// ---------------------------------------------------------------- versions
const relKey = () => `${variant()}|${repo()}`;
const currentReleases = () => S.releases[relKey()];

async function loadVersions(refresh = false) {
  const v = variant();
  const sel = $("version");
  sel.replaceChildren(h("option", { disabled: true }, "Loading…"));
  updateState();
  try {
    let res = !refresh && currentReleases();
    if (!res) {
      const q = new URLSearchParams({ variant: v, ...(repo() ? { repo: repo() } : {}), ...(refresh ? { refresh: 1 } : {}) });
      res = await api(`/api/releases?${q}`);
      S.releases[relKey()] = res;
    }
    if (v !== variant()) return;   // the user switched meanwhile
    if (!res.releases.length) {
      sel.replaceChildren(h("option", { disabled: true, value: "" }, `No ${v} releases in ${res.repo}`));
    } else {
      sel.replaceChildren(...res.releases.map((r) => h("option", { value: r.version },
        r.version + (r.version === res.latest ? "  (latest)" : "") + (r.prerelease ? "  · prerelease" : ""))));
      const want = store.get("version." + v, null);
      sel.value = res.releases.some((r) => r.version === want) ? want : res.latest;
    }
  } catch (e) {
    sel.replaceChildren(h("option", { disabled: true, value: "" }, "Could not load releases"));
    fail(e);
  }
  updateNotes();
  updateState();
}

// ---------------------------------------------------------------- jobs / console
function lineClass(l) {
  if (l.startsWith("$ ")) return "hl";
  if (/\b(error|FAILED|failed|panic|denied|not found)\b/i.test(l)) return "bad";
  if (/^(wrote |patched |restored |- Done!|\[done)|uid=0\b|Success/.test(l)) return "good";
  return "";
}

function appendLines(lines) {
  const c = $("console");
  const atBottom = c.scrollHeight - c.scrollTop - c.clientHeight < 40;
  const frag = document.createDocumentFragment();
  for (const l of lines) frag.append(h("span", { class: lineClass(l) }, l + "\n"));
  c.append(frag);
  if (atBottom) c.scrollTop = c.scrollHeight;
}

function renderJobHead() {
  const j = S.job;
  if (!j) return;
  const kind = { ok: "ok", failed: "bad", running: "warn", queued: "warn" }[j.state] || "";
  $("job-head").replaceChildren(chip(j.state, kind), h("span", {}, j.title));
}

async function refreshJobList() {
  try {
    const { jobs } = await api("/api/jobs");
    const sel = $("job-select");
    sel.replaceChildren(...jobs.map((j) => h("option", { value: j.id },
      `${new Date(j.created * 1000).toLocaleTimeString()} · ${j.state} · ${j.title}`)));
    if (S.job) sel.value = S.job.id;
  } catch { /* not important */ }
}

// Show a job in the console and follow it; resolves with its final state.
function followJob(view) {
  S.job = view;
  S.jobSince = 0;
  $("console").replaceChildren();
  renderJobHead();
  refreshJobList();
  updateState();
  return new Promise((resolve) => {
    const tick = async () => {
      if (!S.job || S.job.id !== view.id) return resolve(null);   // another job took the console
      try {
        const j = await api(`/api/jobs/${view.id}?since=${S.jobSince}`);
        S.jobSince = j.total;
        appendLines(j.lines);
        S.job = j;
        renderJobHead();
        if (j.state === "running" || j.state === "queued") return setTimeout(tick, 600);
        refreshJobList();
        await loadAvds();
        updateState();
        resolve(j);
      } catch (e) { fail(e); setTimeout(tick, 2000); }
    };
    tick();
  });
}

async function showJob(id) {
  try {
    const j = await api(`/api/jobs/${id}`);
    S.job = j;
    S.jobSince = j.total;
    $("console").replaceChildren();
    appendLines(j.lines);
    renderJobHead();
    if (j.state === "running" || j.state === "queued") followJob(j);
  } catch (e) { fail(e); }
}

// ---------------------------------------------------------------- actions
function patchBody(mode) {
  return { avd: S.sel, variant: variant(), version: version(), mode, repo: repo() || undefined,
    allow_shell: $("allow-shell").checked, force: $("force").checked };
}

async function doPatch(mode) {
  const a = current();
  if (mode === "inplace" && !confirm(`Patch the SDK ramdisk of ${a.sysdir} in place?\n\nEvery AVD using this system image becomes rooted, even when started from Android Studio. "Restore stock" undoes it. Stop the emulator first if it is running.`)) return null;
  try {
    const job = await followJob(await api("/api/patch", { body: patchBody(mode) }));
    if (job && job.state === "ok") {
      toast(mode === "separate" ? "Patched ramdisk written. Start the AVD with it below." : "SDK ramdisk patched. Cold boot the AVD.");
      if (job.meta && job.meta.image) { store.set("image." + a.name, job.meta.image); renderImages(); updateState(); }
    } else if (job) toast("Patch failed: see Activity", true);
    return job;
  } catch (e) { fail(e); return null; }
}

async function doStart() {
  const a = current();
  try {
    const image = $("image").value || null;
    store.set("image." + a.name, image);
    await api("/api/emulator/start", { body: { avd: a.name, image, cold: $("cold").checked, kernel_log: $("klog").checked } });
    toast(`Starting ${a.name}${image ? " with the patched ramdisk" : ""}…`);
    pollDevices(true);
  } catch (e) { fail(e); }
}

async function doStop() {
  const a = current();
  const dev = deviceOf(a.name);
  try {
    await api("/api/emulator/stop", { body: { avd: a.name, serial: dev && dev.serial } });
    toast(`Stopping ${a.name}…`);
    setTimeout(() => pollDevices(true), 1500);
  } catch (e) { fail(e); }
}

async function doCheck() {
  const dev = deviceOf(S.sel);
  if (!dev) return;
  const out = $("check-result");
  out.replaceChildren(h("p", { class: "muted" }, "Checking…"));
  try {
    const r = await api("/api/devices/check", { body: { serial: dev.serial } });
    const top = r.root
      ? h("div", { class: "result-top ok" }, `Root works (uid=0${r.context ? ", " + r.context : ""})`)
      : h("div", { class: "result-top bad" }, "No root yet: open the manager once, then stop and start the AVD again (cold boot).");
    out.replaceChildren(h("div", { class: "result" }, top,
      h("table", {}, ...r.checks.map((c) => h("tr", {}, h("td", {}, c.label), h("td", {}, h("pre", {}, c.out || "(no output)")))))));
  } catch (e) { out.replaceChildren(); fail(e); }
}

async function doInstall(file) {
  const dev = deviceOf(S.sel);
  if (!dev || !file) return;
  try {
    toast(`Uploading ${file.name}…`);
    const view = await api(`/api/devices/install?serial=${encodeURIComponent(dev.serial)}`,
      { method: "POST", body: file, headers: { "X-Filename": file.name, "Content-Type": "application/vnd.android.package-archive" } });
    const j = await followJob(view);
    if (j) toast(j.state === "ok" ? `${file.name} installed. Open it on the AVD.` : "Install failed: see Activity", j.state !== "ok");
  } catch (e) { fail(e); }
}

async function doSusfs(remove) {
  const dev = deviceOf(S.sel);
  if (!dev) return;
  try {
    await followJob(await api("/api/susfs", { body: { serial: dev.serial, version: version() || undefined, remove, repo: repo() || undefined } }));
  } catch (e) { fail(e); }
}

async function doDeleteImage() {
  const file = $("image").value;
  if (!file || !confirm(`Delete ${file}?`)) return;
  try { await api("/api/images/delete", { body: { file } }); await loadAvds(); } catch (e) { fail(e); }
}

// ---------------------------------------------------------------- kernel log dialog
let klogTimer;
async function loadKlog() {
  const a = current();
  if (!a) return;
  try {
    const r = await api(`/api/emulator/log?avd=${encodeURIComponent(a.name)}&lines=600`);
    $("klog-flags").replaceChildren(
      chip(r.module_loaded ? "KernelSU module loaded" : "no KernelSU lines yet", r.module_loaded ? "ok" : "warn"),
      r.panic ? chip("kernel panic", "bad") : chip("no panic", "ok"),
      chip(r.running ? "emulator running" : "emulator stopped", r.running ? "ok" : ""),
    );
    const lines = $("klog-markers").checked ? r.markers : r.tail;
    const pre = $("klog-text");
    pre.replaceChildren(...lines.map((l) => h("span", { class: lineClass(l) || (/KernelSU|ksuinit/.test(l) ? "hl" : "") }, l + "\n")));
    pre.scrollTop = pre.scrollHeight;
  } catch (e) {
    $("klog-flags").replaceChildren();
    $("klog-text").textContent = e.message;
  }
}

function openKlog() {
  $("klog-avd").textContent = S.sel;
  $("klog-dialog").showModal();
  loadKlog();
  clearInterval(klogTimer);
  klogTimer = setInterval(loadKlog, 3000);
}

// ---------------------------------------------------------------- polling
let devTimer;
async function pollDevices(fresh = false) {
  clearTimeout(devTimer);
  try {
    const r = await api(`/api/devices${fresh ? "?fresh=1" : ""}`);
    const changed = JSON.stringify(r.devices) !== JSON.stringify(S.devices);
    S.devices = r.devices;
    S.startedHere = r.started_here;
    if (changed) renderAvds();
    updateState();
  } catch { /* server stopped? keep trying */ }
  devTimer = setTimeout(pollDevices, 3000);
}

// ---------------------------------------------------------------- wiring
function init() {
  $("variant").replaceChildren(...Object.entries(VARIANTS).map(([k, v]) => h("option", { value: k }, v.label)));
  $("variant").value = store.get("variant", "finasu");
  $("repo").value = store.get("repo", "");
  $("allow-shell").checked = store.get("allowShell", true);

  $("refresh-avds").onclick = loadAvds;
  $("variant").onchange = () => { store.set("variant", variant()); updateNotes(); loadVersions(); };
  $("version").onchange = () => { store.set("version." + variant(), version()); updateNotes(); updateState(); };
  $("refresh-versions").onclick = () => loadVersions(true);
  $("repo").onchange = () => { store.set("repo", repo()); loadVersions(); };
  $("allow-shell").onchange = () => store.set("allowShell", $("allow-shell").checked);
  $("image").onchange = () => { if (S.sel) store.set("image." + S.sel, $("image").value); updateState(); };

  $("do-patch").onclick = () => doPatch(document.querySelector('input[name="mode"]:checked').value);
  $("do-patch-boot").onclick = async () => {
    const job = await doPatch("separate");
    if (job && job.state === "ok") { $("cold").checked = true; doStart(); }
  };
  $("do-status").onclick = () => api("/api/status", { body: { avd: S.sel } }).then(followJob).catch(fail);
  $("do-restore").onclick = () => {
    if (confirm(`Put the stock SDK ramdisk back for ${S.sel}'s system image? Cold boot afterwards.`))
      api("/api/restore", { body: { avd: S.sel } }).then(followJob).catch(fail);
  };
  $("do-start").onclick = doStart;
  $("do-stop").onclick = doStop;
  $("do-klog").onclick = openKlog;
  $("do-del-image").onclick = doDeleteImage;
  $("do-check").onclick = doCheck;
  $("apk").onchange = (e) => { doInstall(e.target.files[0]); e.target.value = ""; };
  $("do-susfs").onclick = () => doSusfs(false);
  $("do-susfs-remove").onclick = () => doSusfs(true);
  $("job-select").onchange = (e) => showJob(e.target.value);
  $("copy-log").onclick = () => navigator.clipboard.writeText($("console").textContent)
    .then(() => toast("Copied"), () => toast("Copy failed", true));
  $("klog-close").onclick = () => $("klog-dialog").close();
  $("klog-refresh").onclick = loadKlog;
  $("klog-markers").onchange = loadKlog;
  $("klog-dialog").addEventListener("close", () => clearInterval(klogTimer));

  api("/api/info").then((i) => { S.info = i; renderEnv(); }).catch(fail);
  loadAvds();
  loadVersions();
  pollDevices();
  refreshJobList();
}

init();
