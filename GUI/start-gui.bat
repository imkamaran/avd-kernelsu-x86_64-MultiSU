@echo off
rem Start the AVD Root Panel (GUI\server.py) and open it in the default browser.
rem Extra arguments go to server.py, e.g.  start-gui.bat --port 9000
cd /d "%~dp0"
python server.py --open %*
if errorlevel 1 pause
