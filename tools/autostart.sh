#!/bin/sh
# Run as root on the UnitV2 (./deploy.sh autostart | autostart-off). Safe to re-run.
#
#   on:  install /etc/init.d/S86thermal-overlay, which starts the overlay at boot as m5stack,
#        and disable the stock M5 app (it holds the camera) by renaming its boot script.
#   off: remove the overlay boot script and re-enable the stock app.
#
# Boot order (rcS runs /etc/init.d/S??* sorted as text): S81mdev, S82setup-priv (device
# permissions, see root_setup.sh), S86thermal-overlay, ..., S110thermal (overheat guard).
set -e
ACTION="${1:?on|off}"
DIR="${2:-/home/m5stack/thermal-overlay}"
INIT=/etc/init.d
SCRIPT="$INIT/S86thermal-overlay"

case "$ACTION" in
  on)
    [ -f "$DIR/tools/overlay_ctl.sh" ] || { echo "missing $DIR/tools/overlay_ctl.sh: push first"; exit 1; }
    cat > "$SCRIPT" <<EOF
#!/bin/sh
# Thermal overlay autostart, installed by ./deploy.sh autostart (remove: ./deploy.sh autostart-off)
CTL="$DIR/tools/overlay_ctl.sh"
case "\$1" in
  start|stop|restart|status)
    printf "thermal overlay \$1: "
    # Run as m5stack, not root. Root needs no password for su, even without setuid.
    su m5stack -s /bin/sh -c "sh '\$CTL' \$1" ;;
  *) echo "Usage: \$0 {start|stop|restart|status}"; exit 1 ;;
esac
EOF
    chmod 755 "$SCRIPT"
    echo "installed $SCRIPT"
    if [ -f "$INIT/S85runpayload" ]; then
      mv "$INIT/S85runpayload" "$INIT/disabled.S85runpayload"
      echo "disabled the stock app: S85runpayload -> disabled.S85runpayload"
    fi
    # Stop the stock app now, by command line (never S85runpayload stop: killall -9 python3).
    for pat in payload/server.py server_core.py bin/camera_stream; do
      ps w | while read -r pid rest; do
        case "$rest" in *"$pat"*) kill "$pid" && echo "stopped stock $pid" ;; esac
      done
    done
    sleep 1
    "$SCRIPT" restart
    sleep 3
    "$SCRIPT" status
    ;;
  off)
    if [ -f "$SCRIPT" ]; then "$SCRIPT" stop || true; rm -f "$SCRIPT"; echo "removed $SCRIPT"; fi
    if [ -f "$INIT/disabled.S85runpayload" ]; then
      mv "$INIT/disabled.S85runpayload" "$INIT/S85runpayload"
      echo "re-enabled the stock app (starts at next boot, or: $INIT/S85runpayload start)"
    fi
    ;;
  *) echo "usage: $0 on|off [dir]"; exit 1 ;;
esac
