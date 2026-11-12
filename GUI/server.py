#!/usr/bin/env python3
"""avd-ksu GUI - a local web control panel for avd-ksu.py.

  python GUI/server.py [--port 8765] [--open] [--avd-ksu PATH]

Serves GUI/static on http://127.0.0.1:<port>/ and drives this repository's avd-ksu.py: it lists the
AVDs with their kernels and ramdisk state, patches ramdisks (to a separate file or in place),
restores them, starts and stops emulators, checks root on a running AVD, installs manager APKs and
installs the SUSFS module. avd-ksu.py is imported for read-only queries and run as a subprocess
for every action, so the GUI does exactly what the command line does.

Security: it binds to 127.0.0.1 by default, and every /api request must carry the per-run token
embedded in the page and a Host header naming this server, so other web sites cannot drive it.

Standard library only, Python 3.8+.
"""
import argparse
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
OUT = HERE / "out"            # patched ramdisks made from the GUI (+ .json metadata)
UPLOADS = OUT / "uploads"     # APKs on their way to `adb install`
LOGS = HERE / "logs"          # job and emulator logs
TARGETS_JSON = ROOT / ".github" / "avd-targets.json"
GUI_VERSION = "1.0"

VERSION_RE = re.compile(r"^v[0-9A-Za-z.\-]{1,60}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}\.img$")
MAX_UPLOAD = 512 << 20
MANAGER_PACKAGES = ("me.yuki.fina", "me.yuki.fina.pr", "me.weishu.kernelsu", "com.rifsxd.ksunext")
# Kernel log lines ("[    1.071694] ...") about KernelSU, its preinit and panics.
KLOG_MARKER_RE = re.compile(r"^\[\s*\d+\.\d+\].*(ksuinit|KernelSU|kernelsu:|preinit|Kernel panic|exitcode=)")

if os.name == "nt":
    NO_WINDOW = {"creationflags": 0x08000000}                     # CREATE_NO_WINDOW
    DETACHED = {"creationflags": 0x08000000 | 0x00000200}         # + CREATE_NEW_PROCESS_GROUP
else:
    NO_WINDOW = {}
    DETACHED = {"start_new_session": True}

ak = None                       # avd-ksu.py, loaded in main()
AVD_KSU_PATH = None
TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS = set()


class Fail(Exception):
    """An error reported to the browser as {"error": msg} with an HTTP status."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def load_avd_ksu(path):
    spec = importlib.util.spec_from_file_location("avd_ksu", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def die_text(e):
    """Message of avd-ksu.py's die(), which raises SystemExit("error: ...")."""
    return str(e.code).replace("error: ", "", 1)


def guarded(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except SystemExit as e:
        raise Fail(die_text(e), 409)


def run(argv, timeout=20):
    """(returncode, stdout+stderr) of a short command; never raises."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, **NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except OSError as e:
        return -1, str(e)


# ------------------------------------------------------------------ SDK, AVDs, targets
def sdk_root_or_none():
    try:
        return ak.sdk_root()
    except SystemExit:
        return None


def tool(sub, name):
    exe = name + (".exe" if os.name == "nt" else "")
    sdk = sdk_root_or_none()
    if sdk and (sdk / sub / exe).exists():
        return str(sdk / sub / exe)
    return shutil.which(name)


def load_targets():
    try:
        return json.loads(TARGETS_JSON.read_text(encoding="utf-8"))["targets"]
    except (OSError, ValueError, KeyError):
        return []


def match_target(release):
    for t in load_targets():
        if release == t["kernel_release"] or release.endswith("-ab" + t["build_id"]):
            return {"id": t["id"], "label": t["label"], "kmi": t["kmi"]}
    return None


def patched_images(avd):
    res = []
    if not OUT.is_dir():
        return res
    for p in sorted(OUT.iterdir()):
        if p.suffix != ".img" or not p.name.startswith(avd + "__"):
            continue
        try:
            meta = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        st = p.stat()
        res.append({"file": p.name, "size": st.st_size, "mtime": st.st_mtime,
                    "variant": meta.get("variant"), "version": meta.get("version"),
                    "kernel": meta.get("kernel")})
    return res


def avd_rows():
    rows = []
    for name, cfg in sorted(guarded(ak.list_avds).items()):
        sysdir = cfg.get("image.sysdir.1", "").replace("\\", "/").strip("/")
        row = {"name": name, "display": cfg.get("avd.ini.displayname", name),
               "abi": cfg.get("abi.type", "?"), "device": cfg.get("hw.device.name", ""),
               "ram_mb": cfg.get("hw.ramSize", ""), "sysdir": sysdir, "api": None, "tag": None,
               "image_dir": None, "kernel": None, "ramdisk": None, "backup": False,
               "page_kb": 4, "target": None, "error": None}
        m = re.match(r"system-images/android-([^/]+)/([^/]+)/", sysdir + "/")
        if m:
            row["api"], row["tag"] = m.group(1), m.group(2)
        try:
            img = ak.image_dir_of(cfg)
            row["image_dir"] = str(img)
            row["kernel"] = ak.kernel_release(img / "kernel-ranchu")
            row["ramdisk"] = ak.ramdisk_state(img)
            row["backup"] = (img / ("ramdisk.img" + ak.STOCK_SUFFIX)).exists()
            cmdline = img / "kernel_cmdline.txt"
            if cmdline.exists() and "page_shift=14" in cmdline.read_text(errors="replace"):
                row["page_kb"] = 16
            row["target"] = match_target(row["kernel"])
        except SystemExit as e:
            row["error"] = die_text(e)
        except OSError as e:
            row["error"] = str(e)
        row["images"] = patched_images(name)
        rows.append(row)
    return rows


def need_avd(name):
    if not isinstance(name, str) or name not in guarded(ak.list_avds):
        raise Fail(f"unknown AVD {name!r}")
    return name


def need_variant(v):
    if v not in ak.VARIANTS:
        raise Fail(f"unknown variant {v!r}")
    return v


def need_version(v):
    if not isinstance(v, str) or not VERSION_RE.match(v):
        raise Fail(f"bad version {v!r}")
    return v


def need_serial(s):
    if not isinstance(s, str) or not SERIAL_RE.match(s):
        raise Fail(f"bad serial {s!r}")
    return s


def repo_override(value):
    if not value:
        return None
    if not isinstance(value, str) or not REPO_RE.match(value):
        raise Fail("release repository must look like owner/repo")
    return value


def repo_for(variant, override=None):
    if override:
        return override
    fn = getattr(ak, "repo_for", None)            # avd-ksu.py with per-variant default repos
    if fn:
        return fn(variant)
    return os.environ.get("AVD_KSU_REPO") or getattr(ak, "REPO", "")


# ------------------------------------------------------------------ releases (GitHub)
_REL_CACHE, _REL_LOCK = {}, threading.Lock()


def releases(variant, override=None, refresh=False):
    repo = repo_for(variant, override)
    key = (repo, variant)
    with _REL_LOCK:
        hit = _REL_CACHE.get(key)
        if hit and not refresh and time.time() - hit[0] < 600:
            return hit[1]
    prefix, items = f"{variant}-", []
    try:
        for page in range(1, 11):
            rels = ak.http_json(f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}")
            for rel in rels:
                tag = rel["tag_name"]
                if rel.get("draft") or not tag.startswith(prefix) or not tag[len(prefix):].startswith("v"):
                    continue
                items.append({"version": tag[len(prefix):], "tag": tag, "prerelease": bool(rel.get("prerelease")),
                              "published": rel.get("published_at"), "url": rel.get("html_url")})
            if len(rels) < 100:
                break
    except Exception as e:  # noqa: BLE001 - network / API errors go to the page
        raise Fail(f"cannot list releases of {repo}: {e}", 502)
    items.sort(key=lambda r: ak.version_key(r["version"]), reverse=True)
    stable = [r for r in items if not r["prerelease"]]
    latest = (stable or items or [None])[0]       # the one avd-ksu.py picks without --version
    res = {"repo": repo, "variant": variant, "releases": items, "latest": latest and latest["version"]}
    with _REL_LOCK:
        _REL_CACHE[key] = (time.time(), res)
    return res


# ------------------------------------------------------------------ devices (adb)
_DEV_NAMES, _DEV_CACHE, _DEV_LOCK = {}, [0.0, []], threading.Lock()


def devices(max_age=2.5):
    with _DEV_LOCK:
        if time.time() - _DEV_CACHE[0] < max_age:
            return _DEV_CACHE[1]
        adb, devs = tool("platform-tools", "adb"), []
        if adb:
            _, out = run([adb, "devices"], 10)
            seen = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 2 or not parts[0].startswith("emulator-"):
                    continue
                serial, state = parts[0], parts[1]
                seen.add(serial)
                d = {"serial": serial, "state": state, "avd": None, "booted": False}
                if state == "device":
                    if not _DEV_NAMES.get(serial):
                        rc, o = run([adb, "-s", serial, "emu", "avd", "name"], 8)
                        _DEV_NAMES[serial] = o.splitlines()[0].strip() if rc == 0 and o.strip() else None
                    d["avd"] = _DEV_NAMES.get(serial)
                    _, o = run([adb, "-s", serial, "shell", "getprop", "sys.boot_completed"], 8)
                    d["booted"] = o.strip() == "1"
                devs.append(d)
            for s in list(_DEV_NAMES):
                if s not in seen:
                    del _DEV_NAMES[s]
        _DEV_CACHE[0], _DEV_CACHE[1] = time.time(), devs
        return devs


def check_root(serial):
    adb = tool("platform-tools", "adb")
    if not adb:
        raise Fail("adb not found (install SDK Platform-Tools)")
    checks = []

    def shell(label, cmd, timeout=20):
        rc, out = run([adb, "-s", serial, "shell", cmd], timeout)
        checks.append({"label": label, "cmd": cmd, "rc": rc, "out": out.strip()})
        return out

    su = shell("Root: su -c id", "su -c id")
    shell("ksud (installed by the manager)", "su -c 'ls -l /data/adb/ksud; /data/adb/ksud --version'")
    shell("Boot completed", "getprop sys.boot_completed")
    shell("Kernel (stays stock: only a module is added)", "uname -r")
    pkgs = shell("Packages", "pm list packages")
    checks[-1]["out"] = "\n".join(l for l in pkgs.splitlines() if l.split(":")[-1].strip() in MANAGER_PACKAGES) \
        or "(no known manager installed)"
    checks[-1]["label"] = "Manager apps installed"
    m = re.search(r"context=(\S+)", su)
    return {"serial": serial, "root": "uid=0" in su, "context": m.group(1) if m else None, "checks": checks}


# ------------------------------------------------------------------ jobs (avd-ksu.py, adb install)
JOBS, JOBS_LOCK = {}, threading.Lock()
AK_LOCK = threading.Lock()      # one avd-ksu.py action at a time (they share the download cache)


class Job:
    def __init__(self, title, argv, env=None, exclusive=False, on_done=None, meta=None):
        self.id = secrets.token_hex(6)
        self.title, self.argv, self.env = title, argv, env
        self.exclusive, self.on_done, self.meta = exclusive, on_done, meta or {}
        self.lines, self.state, self.rc = [], "queued", None
        self.created, self.ended = time.time(), None
        self._lock = threading.Lock()
        with JOBS_LOCK:
            JOBS[self.id] = self
            for old in sorted(JOBS.values(), key=lambda j: j.created)[:-60]:
                del JOBS[old.id]
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, line):
        with self._lock:
            self.lines.append(line)

    def _run(self):
        if self.exclusive:
            if AK_LOCK.locked():
                self.add("(waiting for the previous avd-ksu.py action to finish)")
            AK_LOCK.acquire()
        try:
            self.state = "running"
            self.add("$ " + " ".join(shlex.quote(str(a)) for a in self.argv))
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", **(self.env or {}))
            proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env, cwd=str(ROOT), text=True,
                                    encoding="utf-8", errors="replace", bufsize=1, **NO_WINDOW)
            for line in proc.stdout:
                self.add(line.rstrip("\r\n"))
            self.rc = proc.wait()
            self.state = "ok" if self.rc == 0 else "failed"
        except OSError as e:
            self.add(f"error: {e}")
            self.state = "failed"
        finally:
            self.ended = time.time()
            if self.exclusive:
                AK_LOCK.release()
            if self.on_done:
                try:
                    self.on_done(self)
                except Exception as e:  # noqa: BLE001
                    self.add(f"(post-processing failed: {e})")
            self.add(f"[{'done' if self.state == 'ok' else 'FAILED'}: exit code {self.rc}]")
            try:
                LOGS.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.created))
                (LOGS / f"job-{stamp}-{self.id}.log").write_text("\n".join(self.lines) + "\n", encoding="utf-8")
            except OSError:
                pass

    def view(self, since=0):
        with self._lock:
            lines = self.lines[since:]
            total = len(self.lines)
        return {"id": self.id, "title": self.title, "state": self.state, "rc": self.rc,
                "created": self.created, "ended": self.ended, "meta": self.meta,
                "since": since, "total": total, "lines": lines}


def avd_ksu_job(title, args, override=None, on_done=None, meta=None):
    env = {"AVD_KSU_REPO": override} if override else None
    return Job(title, [sys.executable, "-u", str(AVD_KSU_PATH)] + args, env=env, exclusive=True,
               on_done=on_done, meta=meta)


# ------------------------------------------------------------------ emulators
EMUS = {}   # avd -> {"proc", "log", "argv", "started"}


def emulator_log_path(avd):
    return LOGS / f"emulator-{avd}.log"


def start_emulator(avd, image=None, cold=True, kernel_log=True):
    emu = tool("emulator", "emulator")
    if not emu:
        raise Fail("emulator not found (install the Android Emulator in SDK Manager)")
    cur = EMUS.get(avd)
    if cur and cur["proc"].poll() is None:
        raise Fail(f"{avd} is already running (started from this page)")
    if any(d["avd"] == avd for d in devices(0)):
        raise Fail(f"{avd} is already running; stop it first")
    argv = [emu, "-avd", avd]
    if image:
        if not FILE_RE.match(image) or not image.startswith(avd + "__") or not (OUT / image).is_file():
            raise Fail(f"no patched ramdisk {image!r} for {avd}")
        argv += ["-ramdisk", str(OUT / image)]
    if cold:
        argv.append("-no-snapshot-load")
    if kernel_log:
        argv.append("-show-kernel")
    LOGS.mkdir(parents=True, exist_ok=True)
    log = emulator_log_path(avd)
    with open(log, "wb") as f:
        f.write(("$ " + " ".join(shlex.quote(a) for a in argv) + "\n").encode())
        f.flush()
        proc = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **DETACHED)
    EMUS[avd] = {"proc": proc, "log": log, "argv": argv, "started": time.time(), "image": image}
    _DEV_CACHE[0] = 0.0
    return {"avd": avd, "argv": argv, "pid": proc.pid}


def stop_emulator(avd=None, serial=None):
    adb = tool("platform-tools", "adb")
    if not serial and avd:
        serial = next((d["serial"] for d in devices(0) if d["avd"] == avd), None)
    if serial and adb:
        rc, out = run([adb, "-s", need_serial(serial), "emu", "kill"], 15)
        _DEV_CACHE[0] = 0.0
        return {"serial": serial, "rc": rc, "out": out.strip()}
    cur = EMUS.get(avd or "")
    if cur and cur["proc"].poll() is None:
        cur["proc"].terminate()
        return {"avd": avd, "terminated": True}
    raise Fail("that AVD is not running")


def emulator_log(avd, lines=400):
    log = emulator_log_path(avd)
    if not log.exists():
        raise Fail("no kernel log yet: start the AVD from this page with 'capture kernel log'", 404)
    size = log.stat().st_size
    with open(log, "rb") as f:
        f.seek(max(0, size - (1 << 20)))
        text = f.read().decode("utf-8", "replace")
    all_lines = text.splitlines()
    marks = [l for l in all_lines if KLOG_MARKER_RE.match(l)]
    cur = EMUS.get(avd)
    return {"avd": avd, "size": size, "tail": all_lines[-lines:], "markers": marks[-200:],
            "panic": any("Kernel panic" in l for l in marks),
            "module_loaded": any("Hello, KernelSU" in l or "KernelSU: KernelSU version" in l for l in marks),
            "running": bool(cur and cur["proc"].poll() is None)}


# ------------------------------------------------------------------ HTTP
def info():
    sdk = sdk_root_or_none()
    return {"gui": GUI_VERSION, "python": sys.version.split()[0], "platform": sys.platform,
            "avd_ksu": str(AVD_KSU_PATH), "variants": list(ak.VARIANTS),
            "repos": {v: repo_for(v) for v in ak.VARIANTS}, "repo_env": os.environ.get("AVD_KSU_REPO"),
            "sdk": str(sdk) if sdk else None, "avd_home": str(ak.avd_home()),
            "adb": tool("platform-tools", "adb"), "emulator": tool("emulator", "emulator"),
            "cache": str(ak.CACHE), "out": str(OUT), "logs": str(LOGS), "targets": load_targets()}


class Handler(BaseHTTPRequestHandler):
    server_version = "avd-ksu-gui/" + GUI_VERSION

    def log_message(self, fmt, *args):  # quiet: only errors
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- plumbing
    def _send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _api_ok(self):
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in ALLOWED_HOSTS:
            return False
        return secrets.compare_digest(self.headers.get("X-GUI-Token", ""), TOKEN)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise Fail("request too large", 413)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise Fail("body must be JSON")

    def _handle(self, method):
        if not self._host_ok():
            return self._send(403, b"forbidden host", "text/plain")
        url = urlparse(self.path)
        try:
            if not url.path.startswith("/api/"):
                if method != "GET":
                    raise Fail("not found", 404)
                return self._static(url.path)
            if not self._api_ok():
                raise Fail("missing or wrong token: reload the page", 403)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            fn = ROUTES.get((method, url.path))
            if fn:
                return self._json(fn(self, q))
            m = re.match(r"^/api/jobs/([0-9a-f]{12})$", url.path)
            if method == "GET" and m:
                job = JOBS.get(m.group(1))
                if not job:
                    raise Fail("no such job", 404)
                return self._json(job.view(int(q.get("since", 0) or 0)))
            raise Fail("not found", 404)
        except Fail as e:
            self._json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"internal error: {e}"}, 500)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _static(self, path):
        if path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__GUI_TOKEN__", TOKEN)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        types = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
        rel = path.lstrip("/")
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file() or f.suffix not in types:
            raise Fail("not found", 404)
        self._send(200, f.read_bytes(), types[f.suffix])

    # -- API
    def api_info(self, q):
        return info()

    def api_avds(self, q):
        return {"avds": avd_rows()}

    def api_devices(self, q):
        emus = {a: {"running": e["proc"].poll() is None, "image": e["image"], "started": e["started"]}
                for a, e in EMUS.items()}
        return {"devices": devices(0 if q.get("fresh") else 2.5), "started_here": emus}

    def api_releases(self, q):
        return releases(need_variant(q.get("variant")), repo_override(q.get("repo")), bool(q.get("refresh")))

    def api_jobs(self, q):
        with JOBS_LOCK:
            jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
        return {"jobs": [{k: v for k, v in j.view(len(j.lines)).items() if k != "lines"} for j in jobs]}

    def api_patch(self, q):
        b = self._body()
        avd, variant, version = need_avd(b.get("avd")), need_variant(b.get("variant")), need_version(b.get("version"))
        override = repo_override(b.get("repo"))
        args = ["patch", "--avd", avd, "--variant", variant, "--version", version]
        out = None
        if b.get("mode", "separate") == "separate":
            OUT.mkdir(parents=True, exist_ok=True)
            out = OUT / f"{avd}__{variant}-{version}.img"
            args += ["--out", str(out)]
        elif b.get("mode") != "inplace":
            raise Fail("mode must be separate or inplace")
        if b.get("allow_shell"):
            args.append("--allow-shell")
        if b.get("force"):
            args.append("--force")
        row = next((r for r in avd_rows() if r["name"] == avd), {})

        def done(job):
            if job.state == "ok" and out and out.exists():
                out.with_suffix(".json").write_text(json.dumps(
                    {"avd": avd, "variant": variant, "version": version, "kernel": row.get("kernel"),
                     "target": (row.get("target") or {}).get("id"), "created": time.time()}, indent=2))

        where = "separate ramdisk" if out else "SDK ramdisk, in place"
        job = avd_ksu_job(f"Patch {avd} with {variant} {version} ({where})", args, override, done,
                          {"action": "patch", "avd": avd, "image": out.name if out else None})
        return job.view()

    def api_status(self, q):
        avd = need_avd(self._body().get("avd"))
        return avd_ksu_job(f"Status of {avd}", ["status", "--avd", avd], meta={"action": "status", "avd": avd}).view()

    def api_restore(self, q):
        avd = need_avd(self._body().get("avd"))
        return avd_ksu_job(f"Restore the stock ramdisk of {avd}", ["restore", "--avd", avd],
                           meta={"action": "restore", "avd": avd}).view()

    def api_susfs(self, q):
        b = self._body()
        serial = need_serial(b.get("serial"))
        args = ["susfs", "--serial", serial]
        if b.get("version"):
            args += ["--version", need_version(b["version"])]
        if b.get("remove"):
            args.append("--remove")
        override = repo_override(b.get("repo"))
        what = "Remove" if b.get("remove") else "Install"
        return avd_ksu_job(f"{what} SUSFS on {serial}", args, override, meta={"action": "susfs", "serial": serial}).view()

    def api_emulator_start(self, q):
        b = self._body()
        return start_emulator(need_avd(b.get("avd")), b.get("image") or None,
                              bool(b.get("cold", True)), bool(b.get("kernel_log", True)))

    def api_emulator_stop(self, q):
        b = self._body()
        return stop_emulator(b.get("avd"), b.get("serial"))

    def api_emulator_log(self, q):
        return emulator_log(need_avd(q.get("avd")), min(int(q.get("lines", 400) or 400), 5000))

    def api_check(self, q):
        return check_root(need_serial(self._body().get("serial")))

    def api_install(self, q):
        serial = need_serial(q.get("serial"))
        adb = tool("platform-tools", "adb")
        if not adb:
            raise Fail("adb not found")
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= MAX_UPLOAD:
            raise Fail("APK missing or larger than 512 MB", 413)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", self.headers.get("X-Filename", "app.apk"))[-80:]
        UPLOADS.mkdir(parents=True, exist_ok=True)
        dest = UPLOADS / f"{secrets.token_hex(4)}-{name}"
        with open(dest, "wb") as f:
            left = n
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            dest.unlink()
            raise Fail("upload interrupted")
        return Job(f"Install {name} on {serial}", [adb, "-s", serial, "install", "-r", str(dest)],
                   on_done=lambda j: dest.unlink(), meta={"action": "install", "serial": serial}).view()

    def api_image_delete(self, q):
        name = self._body().get("file")
        if not isinstance(name, str) or not FILE_RE.match(name) or not (OUT / name).is_file():
            raise Fail("no such patched ramdisk")
        if any(e["proc"].poll() is None and e["image"] == name for e in EMUS.values()):
            raise Fail("an emulator started from this page is using it")
        (OUT / name).unlink()
        (OUT / name).with_suffix(".json").unlink(missing_ok=True)
        return {"deleted": name}


ROUTES = {
    ("GET", "/api/info"): Handler.api_info,
    ("GET", "/api/avds"): Handler.api_avds,
    ("GET", "/api/devices"): Handler.api_devices,
    ("GET", "/api/releases"): Handler.api_releases,
    ("GET", "/api/jobs"): Handler.api_jobs,
    ("GET", "/api/emulator/log"): Handler.api_emulator_log,
    ("POST", "/api/patch"): Handler.api_patch,
    ("POST", "/api/status"): Handler.api_status,
    ("POST", "/api/restore"): Handler.api_restore,
    ("POST", "/api/susfs"): Handler.api_susfs,
    ("POST", "/api/emulator/start"): Handler.api_emulator_start,
    ("POST", "/api/emulator/stop"): Handler.api_emulator_stop,
    ("POST", "/api/devices/check"): Handler.api_check,
    ("POST", "/api/devices/install"): Handler.api_install,
    ("POST", "/api/images/delete"): Handler.api_image_delete,
}


def main():
    global ak, AVD_KSU_PATH
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1; anything else exposes the panel to your network)")
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("--avd-ksu", type=Path, default=ROOT / "avd-ksu.py", help="path to avd-ksu.py")
    a = ap.parse_args()
    if sys.version_info < (3, 8):
        sys.exit("Python 3.8+ is required")
    AVD_KSU_PATH = a.avd_ksu.resolve()
    if not AVD_KSU_PATH.is_file():
        sys.exit(f"avd-ksu.py not found at {AVD_KSU_PATH} (use --avd-ksu)")
    ak = load_avd_ksu(AVD_KSU_PATH)

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    ALLOWED_HOSTS.update({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        ALLOWED_HOSTS.add(f"{a.host}:{port}")
        print(f"WARNING: listening on {a.host}; anyone who can reach it and gets the page can run avd-ksu.py here")
    url = f"http://127.0.0.1:{port}/"
    print(f"avd-ksu GUI {GUI_VERSION} using {AVD_KSU_PATH}")
    print(f"Open {url}  (Ctrl+C to stop)")
    if a.open:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
