#!/usr/bin/env python3
"""Compare Google's current x86_64 Play Store AVD system images with .github/avd-targets.json.

For every x86_64 google_apis_playstore(_ps16k) image of API 33 or later in the official SDK
repository, read the kernel release string from its kernel-ranchu (HTTP range requests: only the
zip's directory and that one file are fetched, not the whole image) and report whether a target
covers it. A newer revision with an unknown kernel means users on the latest image get
"no prebuilt LKM" until a target is added (see README, "Adding an AVD kernel").

Usage: check_images.py [--targets .github/avd-targets.json] [--summary out.md]
Exit status: 0 when every current image's kernel is covered, 1 otherwise, 2 on errors.
"""
import argparse
import io
import json
import re
import struct
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

BASE = "https://dl.google.com/android/repository/sys-img/google_apis_playstore/"
UA = {"User-Agent": "avd-image-check"}


def fetch(url, rng=None):
    h = dict(UA)
    if rng:
        h["Range"] = rng
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120) as r:
        return r.read(), r.headers


class HttpFile(io.RawIOBase):
    """Seekable read-only view of a remote file, for zipfile."""

    def __init__(self, url):
        self.url, self.pos = url, 0
        _, hdr = fetch(url, "bytes=0-0")
        cr = hdr.get("Content-Range", "")
        if "/" not in cr:
            raise OSError(f"{url}: server ignores range requests")
        self.size = int(cr.rsplit("/", 1)[1])

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, b):
        if self.pos >= self.size or not len(b):
            return 0
        end = min(self.pos + len(b), self.size) - 1
        data, _ = fetch(self.url, f"bytes={self.pos}-{end}")
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


def kernel_release(bz):
    """Same parser as avd-ksu.py: the version string the bzImage header points at."""
    if bz[0x202:0x206] != b"HdrS":
        raise ValueError("not an x86 bzImage")
    off = struct.unpack_from("<H", bz, 0x20E)[0] + 0x200
    return bz[off:bz.index(b"\0", off)].decode().split(" ")[0]


def repo_xml():
    for n in (6, 5, 4, 3, 2, 1):
        try:
            data, _ = fetch(f"{BASE}sys-img2-{n}.xml")
            return n, ET.fromstring(data)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    raise SystemExit("no sys-img2-N.xml found")


def api_key(api):
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", api))


def images(root):
    """(api, tag, revision, zip) of each current x86_64 Play Store image, API >= 33."""
    out = []
    for pkg in root.iter():
        if not pkg.tag.endswith("remotePackage"):
            continue
        path = pkg.get("path", "")       # system-images;android-35;google_apis_playstore;x86_64
        parts = path.split(";")
        if len(parts) != 4 or parts[0] != "system-images" or parts[3] != "x86_64":
            continue
        if parts[2] not in ("google_apis_playstore", "google_apis_playstore_ps16k"):
            continue
        if any(c.tag.endswith("channelRef") and c.get("ref") not in (None, "channel-0") for c in pkg):
            continue                       # beta / canary channel
        api = parts[1].removeprefix("android-")
        m = re.match(r"\d+", api)
        if not m or int(m.group()) < 33:
            continue
        major = next((e.text for r in pkg if r.tag.endswith("revision") for e in r if e.tag.endswith("major")), "?")
        url = next((e.text for e in pkg.iter() if e.tag.endswith("url") and (e.text or "").endswith(".zip")), None)
        if url:
            out.append((api, parts[2], major, url))
    return sorted(out, key=lambda i: (api_key(i[0]), i[1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=".github/avd-targets.json")
    ap.add_argument("--summary")
    a = ap.parse_args()
    targets = json.load(open(a.targets, encoding="utf-8"))["targets"]
    by_rel = {t["kernel_release"]: t for t in targets}

    n, root = repo_xml()
    rows, missing = [], 0
    for api, tag, _rev, url in images(root):
        try:
            z = zipfile.ZipFile(io.BufferedReader(HttpFile(BASE + url), buffer_size=1 << 20))
            name = next(n for n in z.namelist() if n.endswith("/kernel-ranchu"))
            rel = kernel_release(z.read(name)[:0x10000])
        except Exception as e:  # noqa: BLE001 - report and carry on with the other images
            rows.append((api, tag, url, f"error: {e}", "?"))
            missing += 1
            continue
        t = by_rel.get(rel) or next((t for t in targets if rel.endswith("-ab" + t["build_id"])), None)
        if t:
            note = t["id"] + ("" if url == t["probe_image"] else f" (probe image is {t['probe_image']})")
        elif "beta" in api:
            note = "not covered (beta image, informational)"
        else:
            note = "**not covered**"
            missing += 1
        rows.append((api, tag, url, rel, note))
        print(f"{api:6} {tag:28} {url:42} {rel:60} {note}", flush=True)

    stale = [t for t in targets if not any(r[3] == t["kernel_release"] for r in rows)]
    md = [f"### Official x86_64 Play Store images (sys-img2-{n}.xml)", "",
          "| API | Tag | Image | Kernel | Target |", "| :--- | :--- | :--- | :--- | :--- |"]
    md += [f"| {r[0]} | `{r[1]}` | `{r[2]}` | `{r[3]}` | {r[4]} |" for r in rows]
    if stale:
        md += ["", "Targets whose kernel is not in any current image (older revisions; still valid for "
               "AVDs that were not updated): " + ", ".join(f"`{t['id']}`" for t in stale)]
    md += ["", f"{missing} current image(s) not covered." if missing else "Every current image is covered."]
    text = "\n".join(md) + "\n"
    if a.summary:
        open(a.summary, "a", encoding="utf-8").write(text)
    print(text)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
