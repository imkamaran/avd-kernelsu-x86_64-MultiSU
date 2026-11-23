#!/usr/bin/env bash
# CI boot test of one published release on one AVD target (.github/workflows/boot-test.yml).
# For a manual test on your own AVD see scripts/boot-test.sh or scripts/check-root.bat.
#
#   scripts/ci-boot-test.sh <target-id> <variant> [version]
#
# Recreates the target's exact system image (its probe_image from Google's SDK repository) as a
# throwaway AVD, patches it with avd-ksu.py from the published release (downloaded and hash-checked
# like a user's run, plus --allow-shell so adb can test su) and follows the user's steps:
#   boot 1  cold boot with -wipe-data and the patched ramdisk; install and open the variant's
#           official manager APK (it installs /data/adb/ksud only if the module trusts it)
#   boot 2  cold boot again (emulator restarted, as the README says)
# Checks:  boot     both boots reach sys.boot_completed=1 (no boot loop / panic)
#          root     boot 2: `su -c id` from adb shell is uid 0
#          manager  boot 2: /data/adb/ksud exists and runs
#          susfs    kernelsu-next-susfs only: `avd-ksu.py susfs` installs and loads the module
# Variant "stock" boots the unpatched image once (a baseline for the harness itself).
# Logs go to $OUT (default ./boot-test-out); $OUT/result.md gets one Markdown table row.
# Needs: KVM, $ANDROID_SDK_ROOT with emulator + platform-tools, jq, curl, unzip, gh (GH_TOKEN).
set -euo pipefail

TARGET=$1 VARIANT=$2 VERSION=${3:-}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
REPO=${AVD_KSU_REPO:-${GITHUB_REPOSITORY:?set AVD_KSU_REPO}}
SDK=${ANDROID_SDK_ROOT:-${ANDROID_HOME:?set ANDROID_SDK_ROOT}}
OUT=${OUT:-$PWD/boot-test-out}
BOOT_TIMEOUT=${BOOT_TIMEOUT:-900}
TMP=${RUNNER_TEMP:-/tmp}
export ANDROID_SDK_ROOT=$SDK AVD_KSU_REPO=$REPO
mkdir -p "$OUT"
ADB=("$SDK/platform-tools/adb" -s emulator-5554)
J=$ROOT/.github/avd-targets.json
LOG=$OUT/emulator.log

t=$(jq -c --arg t "$TARGET" '.targets[] | select(.id == $t)' "$J")
[ -n "$t" ] || { echo "unknown target $TARGET"; exit 2; }
API=$(jq -r .api_level <<<"$t"); PROBE=$(jq -r .probe_image <<<"$t"); KREL=$(jq -r .kernel_release <<<"$t")
case $PROBE in *ps16k*) TAG=google_apis_playstore_ps16k; PS16K=1 ;; *) TAG=google_apis_playstore; PS16K= ;; esac
# Documented limitation (README): KernelSU-Next's manager ships a ksud that is not 16 KB aligned.
KNOWN_16K=; [ -n "$PS16K" ] && [[ $VARIANT == kernelsu-next* ]] && KNOWN_16K=1

declare -A R          # check -> pass | FAIL | known (documented limitation)
NOTES=()
VER=${VERSION:-?}
EMU=
stop_emu() {
  [ -n "$EMU" ] || return 0
  timeout 30 "${ADB[@]}" shell sync >/dev/null 2>&1 || true
  timeout 30 "${ADB[@]}" emu kill >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do kill -0 "$EMU" 2>/dev/null || { EMU=; return 0; }; sleep 2; done
  kill -9 "$EMU" 2>/dev/null || true; sleep 3; EMU=
}
finish() {
  local rc=$? cells="" s c
  stop_emu
  if [ $rc != 0 ] && ! printf '%s\n' "${R[@]:-}" | grep -qx FAIL; then
    NOTES+=("script error (exit $rc, see job log)")
  fi
  for c in boot root manager susfs; do
    case ${R[$c]:-} in pass) s="✅" ;; FAIL) s="❌" ;; known) s="⚠️ known" ;; *) s="–" ;; esac
    cells+=" $s |"
  done
  local IFS="; "
  echo "| $VARIANT | $VER | $TARGET |$cells ${NOTES[*]:-} |" > "$OUT/result.md"
  cat "$OUT/result.md"
  for c in boot root manager susfs; do [ "${R[$c]:-}" = FAIL ] && exit 1; done
  exit $rc
}
trap finish EXIT
fail() { R[$1]=FAIL; NOTES+=("$2"); echo "::error::$TARGET/$VARIANT: $2"; exit 1; }
diag() {
  echo "::group::diagnostics ($1): adb devices, boot props, KernelSU kernel log, emulator.log tail"
  "$SDK/platform-tools/adb" devices -l || true
  timeout 20 "${ADB[@]}" shell getprop 2>/dev/null | grep -E "boot|init.svc.(zygote|bootanim)" | head -20 || true
  grep -aE "KernelSU|kernelsu|ksuinit|susfs|preinit|Kernel panic|RIP:|Call Trace|exitcode=" "$LOG" | tail -60 | cut -c1-300 || true
  echo "----- emulator.log (last 80 lines)"
  tail -n 80 "$LOG" | cut -c1-300 || true
  echo "::endgroup::"
}
start_emu() {   # start_emu [extra emulator args...]
  "$SDK/emulator/emulator" -avd t ${RAMDISK[@]+"${RAMDISK[@]}"} -no-window -no-audio -no-boot-anim \
    -no-snapshot -accel on -show-kernel "$@" >> "$LOG" 2>&1 &
  EMU=$!
}
wait_boot() {   # wait_boot <label>; prints seconds
  local i
  for i in $(seq 1 $((BOOT_TIMEOUT / 3))); do
    sleep 3
    kill -0 "$EMU" 2>/dev/null || break
    grep -qa "Kernel panic" "$LOG" && break
    [ "$(timeout 10 "${ADB[@]}" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] && { echo $((i * 3)); return 0; }
  done
  return 1
}
boot_or_fail() {   # boot_or_fail <label>
  local s why
  if s=$(wait_boot); then echo "== $1: booted in ${s}s"; return 0; fi
  diag "$1"
  if grep -qa "Kernel panic" "$LOG"; then why="kernel panic"
  elif kill -0 "$EMU" 2>/dev/null; then why="not booted after ${BOOT_TIMEOUT}s"
  else why="emulator exited"; fi
  fail boot "$1: $why"
}
open_manager() {   # retry until the manager is the focused window
  for _ in 1 2 3 4 5 6; do
    "${ADB[@]}" shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
    sleep 5
    "${ADB[@]}" shell dumpsys window 2>/dev/null | grep -q "mCurrentFocus=.*$1" && { sleep 15; return 0; }
  done
  return 1
}
rootsh() { timeout 30 "${ADB[@]}" shell su -c "'$1'" 2>&1 | tr -d '\r'; }

# 1. The target's exact system image as a throwaway AVD.
IMG_PARENT=$SDK/system-images/android-$API/$TAG
if [ ! -f "$IMG_PARENT/x86_64/kernel-ranchu" ]; then
  echo "== image $PROBE"
  curl -fsSL --retry 3 -o "$TMP/img.zip" "$(jq -r .image_base_url "$J")$PROBE"
  mkdir -p "$IMG_PARENT"; unzip -q -o "$TMP/img.zip" -d "$IMG_PARENT"; rm -f "$TMP/img.zip"
fi
AVD_DIR=$HOME/.android/avd; mkdir -p "$AVD_DIR/t.avd"
printf 'avd.ini.encoding=UTF-8\npath=%s\npath.rel=avd/t.avd\ntarget=android-%s\n' "$AVD_DIR/t.avd" "$API" > "$AVD_DIR/t.ini"
cat > "$AVD_DIR/t.avd/config.ini" <<EOF
avd.ini.encoding=UTF-8
AvdId=t
abi.type=x86_64
hw.cpu.arch=x86_64
hw.cpu.ncore=4
hw.ramSize=4096
disk.dataPartition.size=6G
image.sysdir.1=system-images/android-$API/$TAG/x86_64/
tag.id=$TAG
PlayStore.enabled=true
hw.keyboard=yes
hw.lcd.width=1080
hw.lcd.height=2400
hw.lcd.density=420
hw.gpu.enabled=yes
hw.gpu.mode=swiftshader_indirect
fastboot.forceColdBoot=yes
EOF
# Play Store images need adb authorization: the emulator installs ~/.android/adbkey.pub in the
# guest at start, so the key must exist (and the server use it) before the first boot.
[ -f "$HOME/.android/adbkey" ] || "$SDK/platform-tools/adb" keygen "$HOME/.android/adbkey" >/dev/null 2>&1
"$SDK/platform-tools/adb" kill-server >/dev/null 2>&1 || true
"$SDK/platform-tools/adb" start-server >/dev/null 2>&1

# 2. Patch with the release, as a user would.
RAMDISK=()
if [ "$VARIANT" = stock ]; then
  VER=-
else
  args=(patch --avd t --variant "$VARIANT" --out "$OUT/patched.img" --allow-shell)
  [ -n "$VERSION" ] && args+=(--version "$VERSION")
  [ -n "$PS16K" ] && [ "$VARIANT" = kernelsu-next-susfs ] && args+=(--force)
  python3 "$ROOT/avd-ksu.py" "${args[@]}" 2>&1 | tee "$OUT/patch.log"
  TAG_REL=$(sed -n "s/^release: \($VARIANT-v[^ ]*\) .*/\1/p" "$OUT/patch.log" | head -1)
  VER=${TAG_REL#"$VARIANT"-}
  grep -q "target $TARGET " "$OUT/patch.log" || fail boot "avd-ksu.py matched another target"
  RAMDISK=(-ramdisk "$OUT/patched.img")
fi

# 3. Boot 1 (fresh user data).
start_emu -wipe-data
boot_or_fail "boot 1"
urel=$(timeout 10 "${ADB[@]}" shell uname -r | tr -d '\r' || true)
[ "$urel" = "$KREL" ] || NOTES+=("uname -r is $urel")
if [ "$VARIANT" = stock ]; then R[boot]=pass; exit 0; fi
grep -aE "welcome to KernelSU|KernelSU: .*version|patching (x64_sys_call|do_syscall_64)|dispatcher installed|KernelSU.*(fail|error|invalid)|ksuinit.*(rror|fail)" "$LOG" \
  | grep -v "load_allow_list\|modules.rc" | head -10 | tee "$OUT/kernel-ksu.log" || true
[ -s "$OUT/kernel-ksu.log" ] || NOTES+=("no KernelSU line in the kernel log")
echo "boot 1 su -c id: $(rootsh id)" | tee "$OUT/checks.log"

# Official manager of this release.
mkdir -p "$OUT/mgr"
case $VARIANT in
  kernelsu) gh release download "$VER" -R tiann/KernelSU -p '*.apk' -D "$OUT/mgr" ;;
  kernelsu-next) gh release download "$VER" -R KernelSU-Next/KernelSU-Next -p '*.apk' -D "$OUT/mgr" ;;
  finasu) gh release download "$VER" -R LyraVoid/FinaSU -p '*.apk' -D "$OUT/mgr" ;;
  bakasu) gh release download "$VER" -R Baka-SU/BakaSU -p '*.apk' -D "$OUT/mgr" ;;
  kernelsu-next-susfs) gh release download "$TAG_REL" -R "$REPO" -p '*.apk' -D "$OUT/mgr" ;;
esac
apks=$(find "$OUT/mgr" -name '*.apk' | grep -vi debug | sort || true)
[ -n "$apks" ] || fail manager "no manager APK in the $VARIANT $VER release"
apk=$(grep -E 'x86_64|universal' <<<"$apks" | head -1 || true)
[ -n "$apk" ] || apk=$(head -1 <<<"$apks")
echo "manager APK: $(basename "$apk") (of: $(xargs -n1 basename <<<"$apks" | tr '\n' ' '))" | tee -a "$OUT/checks.log"
for _ in $(seq 1 40); do "${ADB[@]}" shell pm path android >/dev/null 2>&1 && break; sleep 3; done
before=$("${ADB[@]}" shell pm list packages -3 | tr -d '\r' | sort || true)
"${ADB[@]}" install -r -g "$apk" >> "$OUT/checks.log" 2>&1 || fail manager "adb install $(basename "$apk") failed"
pkg=$(comm -13 <(echo "$before") <("${ADB[@]}" shell pm list packages -3 | tr -d '\r' | sort) | sed -n 's/^package://p' | head -1)
[ -n "$pkg" ] || fail manager "manager package not found after install"
open_manager "$pkg" || NOTES+=("manager did not reach the foreground")
"${ADB[@]}" exec-out screencap -p > "$OUT/manager-boot1.png" 2>/dev/null || true
echo "boot 1 ksud: $(rootsh 'ls -l /data/adb/ksud')" | tee -a "$OUT/checks.log"
# 16 KB images with KernelSU-Next: the manager's ksud cannot run, so follow the README and
# install the release's 16 KB aligned ksud (patch enabled --allow-shell for it).
if grep -q "avd-ksu.py ksud" "$OUT/patch.log"; then
  KNOWN_16K=
  python3 "$ROOT/avd-ksu.py" ksud --serial emulator-5554 --variant "$VARIANT" --version "$VER" 2>&1 | tee "$OUT/ksud16k.log" \
    || fail manager "avd-ksu.py ksud failed (see ksud16k.log)"
  NOTES+=("16 KB: ksud from avd-ksu.py ksud")
fi

# 4. Boot 2: cold restart, as the README tells users.
stop_emu
echo "----- boot 2" >> "$LOG"
start_emu
boot_or_fail "boot 2"
R[boot]=pass
id=$(rootsh id || true)
echo "boot 2 su -c id: $id" | tee -a "$OUT/checks.log"
if [[ "$id" == uid=0* ]]; then
  R[root]=pass
  [[ "$id" == *context=u:r:ksu:s0* ]] || NOTES+=("su context ${id##*context=}")
elif [ -n "$KNOWN_16K" ]; then
  R[root]=known
else
  fail root "boot 2: su -c id gave: ${id:0:100}"
fi
ksud=; [ "${R[root]}" = pass ] && ksud=$(rootsh 'test -x /data/adb/ksud && /data/adb/ksud --version' || true)
echo "boot 2 ksud: ${ksud:-missing} (manager $pkg)" | tee -a "$OUT/checks.log"
open_manager "$pkg" >/dev/null || true
"${ADB[@]}" exec-out screencap -p > "$OUT/manager-boot2.png" 2>/dev/null || true
if [ -n "$ksud" ]; then
  R[manager]=pass; NOTES+=("${ksud#ksud }")
elif [ -n "$KNOWN_16K" ]; then
  R[manager]=known; NOTES+=("16 KB image: KernelSU-Next manager's ksud is not 16 KB aligned (README)")
else
  fail manager "$pkg did not install /data/adb/ksud (does the module trust its signature?)"
fi

# 5. SUSFS module (preview variant).
if [ "$VARIANT" = kernelsu-next-susfs ]; then
  if [ "${R[manager]}" != pass ]; then
    R[susfs]=known
  elif python3 "$ROOT/avd-ksu.py" susfs --serial emulator-5554 --version "$VER" 2>&1 | tee "$OUT/susfs.log" \
       && grep -q "SUSFS loaded\|SUSFS already loaded" "$OUT/susfs.log"; then
    R[susfs]=pass
  else
    fail susfs "avd-ksu.py susfs did not load the module (see susfs.log)"
  fi
fi
