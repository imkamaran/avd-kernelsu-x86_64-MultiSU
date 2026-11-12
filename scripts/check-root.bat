@echo off
setlocal EnableExtensions
rem check-root.bat [avd-name] [variant] [version]  (Windows)
rem Patch an AVD with avd-ksu.py into a separate ramdisk (fina.img next to this file; the SDK is
rem not touched), cold boot it without a Quick Boot snapshot, and collect root diagnostics in
rem check-root.log and boot-kernel.log next to this file.
rem   avd-name  default: asked (the AVDs are listed first)
rem   variant   default: finasu   (kernelsu, kernelsu-next, kernelsu-next-susfs, finasu)
rem   version   default: the newest release of the variant
rem Needs Python on PATH and the Android SDK in %LOCALAPPDATA%\Android\Sdk (or ANDROID_SDK_ROOT).

set "HERE=%~dp0"
set "AVD=%~1"
set "VARIANT=%~2"
if "%VARIANT%"=="" set "VARIANT=finasu"
set "VERSION_ARGS="
if not "%~3"=="" set "VERSION_ARGS=--version %~3"

set "SDK=%ANDROID_SDK_ROOT%"
if "%SDK%"=="" set "SDK=%LOCALAPPDATA%\Android\Sdk"
set "ADB=%SDK%\platform-tools\adb.exe"
set "EMU=%SDK%\emulator\emulator.exe"
set "RAMDISK=%HERE%patched-ramdisk.img"
set "LOG=%HERE%check-root.log"
set "KLOG=%HERE%boot-kernel.log"

rem avd-ksu.py next to this file (downloaded together) or one level up (repository checkout).
set "AVDKSU=%HERE%avd-ksu.py"
if not exist "%AVDKSU%" set "AVDKSU=%HERE%..\avd-ksu.py"
if not exist "%AVDKSU%" (echo avd-ksu.py not found next to this file or one folder up. & pause & exit /b 1)
if not exist "%ADB%" (echo adb not found at %ADB% - install SDK Platform-Tools or set ANDROID_SDK_ROOT. & pause & exit /b 1)
if not exist "%EMU%" (echo emulator not found at %EMU% - install the Android Emulator or set ANDROID_SDK_ROOT. & pause & exit /b 1)

if "%AVD%"=="" (
  python "%AVDKSU%" list
  echo.
  set /p "AVD=AVD name to patch and boot: "
)
if "%AVD%"=="" (echo No AVD name given. & pause & exit /b 1)

> "%LOG%" echo check-root.bat  %date% %time%  avd=%AVD%  variant=%VARIANT%  %VERSION_ARGS%

call :hdr "avd-ksu.py list"
python "%AVDKSU%" list >> "%LOG%" 2>&1

call :hdr "avd-ksu.py status (state of the SDK ramdisk)"
python "%AVDKSU%" status --avd %AVD% >> "%LOG%" 2>&1

call :hdr "stop any running emulator"
"%ADB%" emu kill >> "%LOG%" 2>&1
ping -n 8 127.0.0.1 >nul

call :hdr "patch into a separate ramdisk (always fresh, so a stale image is never booted)"
if exist "%RAMDISK%" del "%RAMDISK%"
python "%AVDKSU%" patch --avd %AVD% --variant %VARIANT% %VERSION_ARGS% --out "%RAMDISK%" --allow-shell >> "%LOG%" 2>&1
if not exist "%RAMDISK%" (
  >> "%LOG%" echo PATCH FAILED: no patched ramdisk written
  echo Patch failed - see check-root.log
  goto :report
)

call :hdr "cold boot with -ramdisk and no snapshot; kernel log in boot-kernel.log"
start "emulator %AVD%" /min cmd /s /c ""%EMU%" -avd %AVD% -ramdisk "%RAMDISK%" -no-snapshot-load -show-kernel > "%KLOG%" 2>&1"

echo Waiting for the AVD to boot (up to 5 minutes)...
set /a N=0
:wait
set /a N+=1
if %N% gtr 60 (
  >> "%LOG%" echo RESULT: not booted after 5 minutes
  goto :collect
)
ping -n 6 127.0.0.1 >nul
if exist "%KLOG%" findstr /c:"Kernel panic" "%KLOG%" >nul && (
  >> "%LOG%" echo RESULT: KERNEL PANIC during boot - see boot-kernel.log
  goto :collect
)
"%ADB%" shell getprop sys.boot_completed 2>nul | findstr /b "1" >nul && (
  >> "%LOG%" echo RESULT: booted after about %N%x5 seconds
  goto :collect
)
goto :wait

:collect
rem The module hides itself from /sys/module and lsmod once loaded, so the kernel log is the
rem place to see that it loaded.
call :hdr "kernel log: KernelSU / preinit / panic lines (module loaded at boot?)"
findstr /i /c:"Hello, KernelSU" /c:"KernelSU: KernelSU version" /c:"preinit" /c:"Kernel panic" /c:"exitcode=" "%KLOG%" >> "%LOG%" 2>&1

call :hdr "root: su -c id (expect uid=0 ... context=u:r:ksu:s0)"
"%ADB%" shell su -c id >> "%LOG%" 2>&1

call :hdr "ksud installed by the manager? (as root)"
"%ADB%" shell su -c "ls -l /data/adb/ksud; /data/adb/ksud --version" >> "%LOG%" 2>&1

call :hdr "manager packages installed"
"%ADB%" shell pm list packages me.yuki.fina >> "%LOG%" 2>&1
"%ADB%" shell pm list packages me.weishu.kernelsu >> "%LOG%" 2>&1
"%ADB%" shell pm list packages com.rifsxd.ksunext >> "%LOG%" 2>&1

call :hdr "uname -a (always the stock kernel: only a module is added)"
"%ADB%" shell uname -a >> "%LOG%" 2>&1

:report
echo.
echo Done. Results:
echo   %LOG%
echo   %KLOG%
echo The emulator keeps running, so you can install and open the manager now.
pause
exit /b 0

:hdr
>> "%LOG%" echo.
>> "%LOG%" echo ===== %~1 =====
echo %~1
exit /b 0
