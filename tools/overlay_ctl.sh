#!/bin/sh
# Start / stop the thermal overlay in the background, with automatic restart.
# Used by the boot script (/etc/init.d/S86thermal-overlay) and by ./deploy.sh start|stop.
#
#   sh overlay_ctl.sh start | stop | restart | status
#
# "start" runs a small loop (overlay_ctl.sh loop) that restarts the server 3 s after any
# exit, so a crash or an unplugged sensor never leaves the stream down. Processes are
# matched by command line, never with killall (that would also kill the stock overheat
# guard /etc/thermal/check_thermal.py).
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${THERMAL_LOG:-/tmp/thermal-overlay.log}"
SELF="$DIR/tools/overlay_ctl.sh"

pids() {  # PIDs whose command line contains $1
  ps w | while read -r pid rest; do
    case "$rest" in *"$1"*) echo "$pid" ;; esac
  done
}

running() { [ -n "$(pids 'overlay_ctl.sh loop')" ]; }

case "$1" in
  start)
    if running; then echo "thermal overlay already running"; exit 0; fi
    : > "$LOG.in"   # empty stdin; /dev/null may be root-only before root-setup
    nohup sh "$SELF" loop < "$LOG.in" >> "$LOG" 2>&1 &
    echo "thermal overlay started (log: $LOG)"
    ;;
  stop)
    for p in $(pids 'overlay_ctl.sh loop') $(pids 'thermal/server.py'); do
      kill "$p" 2>&1 && echo "stopped $p"
    done
    ;;
  restart)
    sh "$SELF" stop; sleep 1; sh "$SELF" start
    ;;
  status)
    if running; then echo "running"; else echo "not running"; fi
    ps w | grep '[t]hermal/server.py'
    ;;
  loop)
    cd "$DIR" || exit 1
    while :; do
      # Logs live in RAM (/tmp): keep them small.
      if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 262144 ]; then : > "$LOG"; fi
      echo "$(date '+%F %T') starting server"
      python3 thermal/server.py
      rc=$?
      echo "$(date '+%F %T') server exited with $rc, restarting in 3 s"
      sleep 3
    done
    ;;
  *)
    echo "usage: $0 start|stop|restart|status"; exit 1
    ;;
esac
