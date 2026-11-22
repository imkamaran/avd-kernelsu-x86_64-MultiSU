#!/usr/bin/env bash
# Boot test for one release on one AVD target (CI: .github/workflows/boot-test.yml).
#
#   scripts/boot_test.sh <target-id> <variant> [version]
#
# Recreates the target's exact system image (its probe_image from Google's SDK repository) as an
# AVD, patches it with avd-ksu.py from the published release (downloaded and hash-checked like a
# user's run), cold boots it under KVM with the patched ramdisk and checks:
#   boot     sys.boot_completed=1 (no boot loop / panic)
#   root     `su -c id` from adb shell is uid 0 (patched with --allow-shell)
#   manager  the variant's official manager APK, once opened, installs /data/adb/ksud
#            (it only does that when the module trusts its signature)
#   susfs    kernelsu-next-susfs only: `avd-ksu.py susfs` installs and loads the SUSFS module
# Logs go to $OUT (default ./boot-test-out); $OUT/result.md gets one Markdown table row.
# Needs: KVM, $ANDROID_SDK_ROOT with emulator + platform-tools, jq, curl, unzip, gh (GH_TOKEN).
set -euo pipefail

TARGET=$1 VARIANT=$2 VERSION=${3:-}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
REPO=${AVD_KSU_REPO:-${GITHUB_REPOSITORY:?set AVD_KSU_REPO}}
SDK=${ANDROID_SDK_ROOT:-${ANDROID_HOME:?set ANDROID_SDK_ROOT}}
OUT=${OUT:-$PWD/boot-test-out}
BOOT_TIMEOUT=${BOOT_TIMEOUT:-900}
export ANDROID_SDK_ROOT=$SDK AVD_KSU_REPO=$REPO
mkdir -p "$OUT"
ADB=("$SDK/platform-tools/adb" -s emulator-5554)
J=$ROOT/.github/avd-targets.json

t=$(jq -c --arg t "$TARGET" '.targets[] | select(.id == $t)' "$J")
[ -n "$t" ] || { echo "unknown target $TARGET"; exit 2; }
API=$(jq -r .api_level <<<"$t"); PROBE=$(jq -r .probe_image <<<"$t"); KREL=$(jq -r .kernel_release <<<"$t")
case $PROBE in *ps16k*) TAG=google_apis_playstore_ps16k; PS16K=1 ;; *) TAG=google_apis_playstore; PS16K= ;; esac

declare -A R          # check -> pass | FAIL | known (documented limitation) | skip
NOTES=()
VER=${VERSION:-?}
finish() {
  local rc=$?
  "${ADB[@]}" emu kill >/dev/null 2>&1 || true
  if [ $rc != 0 ] && ! printf '%s\n' "${R[@]:-}" | grep -qx FAIL; then
    NOTES+=("script error (exit $rc, see job log)")
  fi
  local cells=""
  for c in boot root manager susfs; do
    case ${R[$c]:-skip} in pass) s="✅" ;; FAIL) s="❌" ;; known) s="⚠️ known" ;; *) s="–" ;; esac
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

# 1. The target's exact system image as an AVD.
IMG_PARENT=$SDK/system-images/android-$API/$TAG
if [ ! -f "$IMG_PARENT/x86_64/kernel-ranchu" ]; then
  echo "== image $PROBE"
  curl -fsSL --retry 3 -o "$RUNNER_TEMP/img.zip" "$(jq -r .image_base_url "$J")$PROBE"
  mkdir -p "$IMG_PARENT"; unzip -q -o "$RUNNER_TEMP/img.zip" -d "$IMG_PARENT"; rm -f "$RUNNER_TEMP/img.zip"
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

# 2. Patch with the release, as a user would (plus --allow-shell so adb can test su).
args=(patch --avd t --variant "$VARIANT" --out "$OUT/patched.img" --allow-shell)
[ -n "$VERSION" ] && args+=(--version "$VERSION")
[ -n "$PS16K" ] && [ "$VARIANT" = kernelsu-next-susfs ] && args+=(--force)
python3 "$ROOT/avd-ksu.py" "${args[@]}" 2>&1 | tee "$OUT/patch.log"
TAG_REL=$(sed -n "s/^release: \($VARIANT-v[^ ]*\) .*/\1/p" "$OUT/patch.log" | head -1)
VER=${TAG_REL#"$VARIANT"-}
grep -q "target $TARGET " "$OUT/patch.log" || fail boot "avd-ksu.py matched another target"

# 3. Cold boot with the patched ramdisk.
"$SDK/emulator/emulator" -avd t -ramdisk "$OUT/patched.img" -no-window -no-audio -no-boot-anim \
  -no-snapshot -wipe-data -gpu swiftshader_indirect -accel on -skip-adb-auth -show-kernel \
  > "$OUT/emulator.log" 2>&1 &
EMU=$!
"${ADB[@]}" start-server >/dev/null 2>&1 || true
booted=
for _ in $(seq 1 $((BOOT_TIMEOUT / 5))); do
  kill -0 $EMU 2>/dev/null || break
  [ "$("${ADB[@]}" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] && { booted=1; break; }
  sleep 5
done
grep -aE "KernelSU|kernelsu|susfs|preinit|Kernel panic|exitcode=" "$OUT/emulator.log" > "$OUT/kernel-ksu.log" || true
[ -n "$booted" ] || {
  kill -0 $EMU 2>/dev/null && why="not booted after ${BOOT_TIMEOUT}s" || why="emulator exited"
  grep -aq "Kernel panic" "$OUT/emulator.log" && why+=", kernel panic"
  fail boot "$why (see emulator.log)"
}
R[boot]=pass
[ "$("${ADB[@]}" shell uname -r | tr -d '\r')" = "$KREL" ] || NOTES+=("uname -r differs from $KREL")

# 4. Root from the shell.
id=$("${ADB[@]}" shell su -c id 2>&1 | tr -d '\r' || true)
echo "su -c id: $id" | tee "$OUT/checks.log"
[[ "$id" == uid=0* ]] || fail root "su -c id: ${id:0:80}"
R[root]=pass
[[ "$id" == *context=u:r:ksu:s0* ]] || NOTES+=("su context: ${id##*context=}")

# 5. The official manager recognises the module and installs ksud.
mkdir -p "$OUT/mgr"
case $VARIANT in
  kernelsu) gh release download "$VER" -R tiann/KernelSU -p '*.apk' -D "$OUT/mgr" ;;
  kernelsu-next) gh release download "$VER" -R KernelSU-Next/KernelSU-Next -p '*.apk' -D "$OUT/mgr" ;;
  finasu) gh release download "$VER" -R LyraVoid/FinaSU -p '*.apk' -D "$OUT/mgr" ;;
  kernelsu-next-susfs) gh release download "$TAG_REL" -R "$REPO" -p '*.apk' -D "$OUT/mgr" ;;
esac
apks=$(find "$OUT/mgr" -name '*.apk' | grep -vi debug | sort || true)
[ -n "$apks" ] || fail manager "no manager APK in the $VARIANT $VER release"
apk=$(grep -E 'x86_64|universal' <<<"$apks" | head -1 || true)
[ -n "$apk" ] || apk=$(head -1 <<<"$apks")
echo "manager: $(basename "$apk")" | tee -a "$OUT/checks.log"
before=$("${ADB[@]}" shell pm list packages -3 | tr -d '\r' | sort)
"${ADB[@]}" install -r -g "$apk" >> "$OUT/checks.log" 2>&1 || fail manager "adb install $(basename "$apk") failed"
pkg=$(comm -13 <(echo "$before") <("${ADB[@]}" shell pm list packages -3 | tr -d '\r' | sort) | sed -n 's/^package://p' | head -1)
[ -n "$pkg" ] || fail manager "manager package not found after install"
"${ADB[@]}" shell monkey -p "$pkg" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
ksud=
for _ in $(seq 1 36); do
  ksud=$("${ADB[@]}" shell su -c "'test -x /data/adb/ksud && /data/adb/ksud --version'" 2>/dev/null | tr -d '\r' || true)
  [ -n "$ksud" ] && break
  sleep 5
done
echo "manager $pkg, /data/adb/ksud: ${ksud:-missing}" | tee -a "$OUT/checks.log"
"${ADB[@]}" exec-out screencap -p > "$OUT/manager.png" 2>/dev/null || true
if [ -n "$ksud" ]; then
  R[manager]=pass
elif [ -n "$PS16K" ] && [[ $VARIANT == kernelsu-next* ]]; then
  R[manager]=known; NOTES+=("16 KB image: KernelSU-Next manager's ksud is not 16 KB aligned (README)")
else
  fail manager "$pkg did not install /data/adb/ksud within 180s (module does not trust it?)"
fi

# 6. SUSFS module (preview variant).
if [ "$VARIANT" = kernelsu-next-susfs ]; then
  if [ "${R[manager]:-}" != pass ]; then
    R[susfs]=known
  elif python3 "$ROOT/avd-ksu.py" susfs --serial emulator-5554 --version "$VER" 2>&1 | tee "$OUT/susfs.log" \
       && grep -q "SUSFS loaded\|SUSFS already loaded" "$OUT/susfs.log"; then
    R[susfs]=pass
  else
    fail susfs "avd-ksu.py susfs did not load the module (see susfs.log)"
  fi
fi
