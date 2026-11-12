#!/bin/sh
# Start the AVD Root Panel (GUI/server.py) and open it in the default browser.
# Extra arguments go to server.py, e.g.  ./start-gui.sh --port 9000
cd "$(dirname "$0")" && exec python3 server.py --open "$@"
