# AVD Root Panel (web GUI)

A local web control panel for [`avd-ksu.py`](../avd-ksu.py): see your AVDs, pick a root solution and
version, patch, boot, check root and install the manager without typing commands. It runs on Python's
built-in HTTP server: no packages to install, nothing from the internet in the page itself.

The GUI never replaces `avd-ksu.py`. It imports it read-only to list AVDs and kernels, and runs it
as a subprocess for every patch, status, restore and SUSFS action, so it does exactly what the command
line does. The full output of every action is shown in the page and saved under `GUI/logs/`.

## Start

Requires Python 3.8+, the Android SDK (Emulator and Platform-Tools) and a clone or download of this
repository: the GUI uses the `avd-ksu.py` one folder up.

- **Windows**: double-click `GUI\start-gui.bat`
- **macOS / Linux**: `./GUI/start-gui.sh`
- or: `python GUI/server.py --open`

It prints `Open http://127.0.0.1:8765/`. Options: `--port 9000`, `--avd-ksu path\to\avd-ksu.py`,
`--open` (start the browser). Stop it with Ctrl+C or by closing its window; emulators it started keep
running.

## What the page does

| Area | What you can do |
| :--- | :--- |
| **Virtual devices** | Every AVD with its API level, image type, kernel, whether a module exists for that kernel, 16 KB page size, SDK ramdisk state (stock / patched with what), patched images made here, and whether it is running |
| **1. Patch the ramdisk** | Choose KernelSU, KernelSU-Next, KernelSU-Next + SUSFS or FinaSU and a version (read live from the releases, newest stable preselected). **Separate ramdisk** (recommended) writes `GUI/out/<avd>__<variant>-<version>.img` and leaves the SDK untouched; **In place** patches the system image itself so the AVD is rooted even from Android Studio (backup kept, *Restore stock* undoes it). *Patch & boot* patches and cold boots in one go. *Status* shows the SDK ramdisk state |
| **2. Run the emulator** | Start the AVD with a patched image (or the SDK ramdisk), cold boot without the Quick Boot snapshot, and capture the kernel log. *Kernel log* shows the KernelSU lines, whether the module loaded and whether the kernel panicked. *Stop* closes it. *Delete image* removes a patched image |
| **3. On the running AVD** | *Check root* runs `su -c id`, checks `/data/adb/ksud`, the installed managers and the kernel. *Install APK…* installs a manager APK with `adb install -r`. *Get the manager* links to the matching manager release. For the SUSFS preview: *Install SUSFS module* / *Remove SUSFS* |
| **Activity** | Live output of every action, and the history of this session |
| **Settings & paths** | Where the SDK, AVDs, cache, patched images and logs are; an optional release repository override (`owner/repo`, like `AVD_KSU_REPO`) |

The page warns before actions that cannot work: no module for the AVD's kernel (patch disabled),
KernelSU-Next on 16 KB images, and patching in place (which roots every AVD on that system image).

Typical first run: select the AVD → choose the variant and version → **Patch & boot** → install and
open the manager of the same version (*Install APK…*) → **Stop** → **Start** again → **Check root**.

## Security

The panel can run commands on your computer, so it is locked down:

- it listens on `127.0.0.1` only (`--host` changes that and prints a warning: don't, unless you know why);
- every `/api` request needs a random token that is generated at each start and embedded in the page,
  and its `Host` (and `Origin`, when sent) must name this server, so other web sites cannot drive it
  (no CSRF, no DNS rebinding);
- inputs are checked against what exists (AVD names, variants, version format, `adb` serials,
  images under `GUI/out`), and commands are run without a shell;
- the page loads nothing from other servers (strict Content-Security-Policy).

## Files

| Path | Content |
| :--- | :--- |
| `server.py` | the HTTP server and API (standard library only) |
| `static/` | the page: `index.html`, `app.js`, `style.css` |
| `start-gui.bat`, `start-gui.sh` | launchers |
| `out/` | patched ramdisks made here (+ a `.json` with variant, version and kernel); not committed |
| `logs/` | a log per action (`job-*.log`) and the emulator / kernel log of AVDs started here (`emulator-<avd>.log`); not committed |

## Troubleshooting

- **"SDK not found"**: set `ANDROID_SDK_ROOT` (or `ANDROID_HOME`) before starting the GUI.
- **No AVDs**: create one in Android Studio's Device Manager (x86_64, Google Play image). AVDs are
  read from `ANDROID_AVD_HOME`, else `~/.android/avd`.
- **"Could not load releases" / HTTP 403**: GitHub's API allows 60 unauthenticated requests per hour.
  Wait, or start the GUI with a `GITHUB_TOKEN` environment variable (any token with public read access).
- **"kernel not supported"**: the AVD's system image has a kernel with no prebuilt module; update the
  image in SDK Manager (Google Play x86_64) or use another API level. See the main README.
- **Root missing after boot**: make sure the boot was a cold boot with the patched image (the AVD card
  and step 3 say which image it was started with), then open the manager once, stop and start again.
