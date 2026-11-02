#!/bin/sh
# One-time root setup on the UnitV2 for the thermal overlay. Safe to re-run.
# Run from the PC with:  ./deploy.sh root-setup   (logs in as root over SSH; su is broken)
#
# 1. Device permissions. S100setup-priv opens /dev/null, /dev/tty*, /dev/video* to all
#    users, but boot scripts run in lexical order, so S100 runs before S81mdev, which then
#    resets everything to root-only. Renaming it S82setup-priv makes it run after mdev.
# 2. Stop the stock M5 app (camera_stream holds /dev/video0) and Prusa cam, matching them
#    by command line. NOT via S85runpayload stop, which does `killall -9 python3` and
#    would also kill /etc/thermal/check_thermal.py (the SoC overheat guard).
#    The stock app comes back at reboot until the thermal overlay gets its own boot script.
# 3. Disable Prusa cam at boot (renamed, not deleted: undo by renaming it back).
# 4. Create the install folder, owned by m5stack.
set -e
DIR="${1:-/home/m5stack/thermal-overlay}"
INIT=/etc/init.d

echo "[1] device permissions"
chmod 666 /dev/null /dev/zero /dev/ttyS1 /dev/video0
if [ -f "$INIT/S100setup-priv" ]; then
  mv "$INIT/S100setup-priv" "$INIT/S82setup-priv"
  echo "    renamed S100setup-priv -> S82setup-priv (now runs after S81mdev)"
fi

echo "[2] stopping stock app and Prusa cam"
for pat in payload/server.py server_core.py bin/camera_stream prusa_cam/prusa_cam.py; do
  ps w | while read -r pid user cmd; do
    case "$cmd" in
      *"$pat"*) kill "$pid" && echo "    killed $pid: $cmd" ;;
    esac
  done
done

echo "[3] Prusa cam autostart"
if [ -f "$INIT/S99prusa-cam" ]; then
  mv "$INIT/S99prusa-cam" "$INIT/disabled.S99prusa-cam"
  echo "    renamed S99prusa-cam -> disabled.S99prusa-cam (rcS only runs S??*)"
else
  echo "    already disabled"
fi

echo "[4] install folder $DIR"
mkdir -p "$DIR"
chown m5stack:m5stack "$DIR"

sleep 1
echo
echo "check:"
ls -l /dev/null /dev/ttyS1 /dev/video0
ps w | grep -E 'camera_stream|payload|prusa|server_core' | grep -v grep || echo "    stock app and Prusa cam are stopped"
echo "done"
