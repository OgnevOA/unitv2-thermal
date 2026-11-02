#!/usr/bin/env bash
# Deploy / control the thermal overlay on the UnitV2.
#
#   ./deploy.sh info           read-only device report (devices, processes, init scripts)
#   ./deploy.sh netinfo        read-only WiFi/driver report (no passwords printed)
#   ./deploy.sh connect        log in once; later commands reuse the connection for 30 min
#   ./deploy.sh disconnect     close the shared connection
#   ./deploy.sh root-setup     run tools/root_setup.sh over root SSH: fix device perms, stop stock app + Prusa cam
#   ./deploy.sh push           copy thermal/ and tools/ to the device (keeps the device's config.json)
#   ./deploy.sh run            push, then run the server in the foreground (Ctrl-C stops it)
#   ./deploy.sh start | stop   push + (re)start in the background, restarting on crashes / stop it
#   ./deploy.sh status         is it running?
#   ./deploy.sh log            follow the background log
#   ./deploy.sh autostart      once, root password: start at every boot instead of the stock M5 app
#   ./deploy.sh autostart-off  undo: remove the boot script, re-enable the stock app
#   ./deploy.sh usb-local [off] once, root password: phones on USB keep their internet (no gateway/DNS)
#   ./deploy.sh pin-mac [MAC]  once, root password: fixed WiFi MAC (default 00:e0:4c:e1:94:c5), "off" undoes
#   ./deploy.sh probe [dev] [args]   run tools/uart_probe.py on the device (default /dev/ttyS1)
#   ./deploy.sh ssh            open a shell
#
# Override with UNITV2_HOST=user@host, UNITV2_DIR=/path.
set -euo pipefail

HOST="${UNITV2_HOST:-m5stack@192.168.1.112}"
# Created and chowned to m5stack by `./deploy.sh root-setup` (home itself isn't writable).
DIR="${UNITV2_DIR:-/home/m5stack/thermal-overlay}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG=/tmp/thermal-overlay.log

# The device's home dir isn't writable, so key auth is impossible. Instead, one
# password login opens a shared connection that later commands reuse for 30 min.
SSH=(ssh -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/cm-unitv2-%C" -o ControlPersist=30m)

push() {
  tar -C "$ROOT" --exclude config.json --exclude 'config.json.*' --exclude __pycache__ -cf - thermal tools \
    | "${SSH[@]}" "$HOST" "mkdir -p '$DIR' && tar -C '$DIR' -xf -"
  echo "pushed to $HOST:$DIR"
}

CTL="sh '$DIR/tools/overlay_ctl.sh'"

stop() {  # the background loop and any foreground server
  "${SSH[@]}" "$HOST" "$CTL stop"
}

info() {
  # Read-only. Never redirects to /dev/null: before root-setup it is root-only,
  # and a failed redirection makes the shell skip the command entirely.
  "${SSH[@]}" "$HOST" 'sh -s' <<'EOF'
sec() { echo; echo "== $*"; }
sec devnode;   ls -l /dev/null /dev/zero /dev/ttyS* /dev/video* 2>&1
sec processes; ps w 2>&1 | grep -v ' \[' | head -n 40
sec holders;   for p in /proc/[0-9]*; do
                 l=$(ls -l "$p/fd" 2>&1 | grep -E 'video|ttyS'); [ -n "$l" ] && echo "$p $(cat $p/comm 2>&1): $l"
               done; echo "(only processes we may inspect are listed)"
sec listening; netstat -tln 2>&1 | head -n 20
sec sdcard;    mount 2>&1 | grep -i -E 'sd|mmc'; ls -la /media/sdcard 2>&1 | head -n 10
sec prusa_cam; ls -la /home/m5stack/prusa_cam 2>&1
sec scripts;   for f in /etc/init.d/S85runpayload /etc/init.d/S110thermal /etc/init.d/S100setup-priv /etc/init.d/S81mdev /etc/init.d/rcS; do
                 echo "--- $f"; cat "$f" 2>&1; done
sec sshd;      grep -i -E '^[^#]*(AuthorizedKeysFile|PermitRootLogin|PasswordAuthentication)' /etc/ssh/sshd_config 2>&1
sec groups;    grep -E '^(wheel|dialout|video|tty|root):' /etc/group 2>&1
EOF
}

netinfo() {
  # Read-only: how WiFi comes up (to pin the MAC). Never prints PSKs or passwords.
  "${SSH[@]}" "$HOST" 'sh -s' <<'EOF'
sec() { echo; echo "== $*"; }
sec links;    ip link 2>&1 || ifconfig -a 2>&1
sec driver;   for n in /sys/class/net/*; do
                echo "$(basename $n): driver $(basename "$(readlink $n/device/driver 2>&1)" 2>&1)" \
                     "module $(basename "$(readlink $n/device/driver/module 2>&1)" 2>&1)" \
                     "addr $(cat $n/address 2>&1) type $(cat $n/addr_assign_type 2>&1)"
              done
sec modules;  lsmod 2>&1 | head -n 30
sec modparams; for m in /sys/module/*/parameters/rtw_initmac /sys/module/*/parameters/*mac*; do
                 [ -e "$m" ] && echo "$m = $(cat "$m" 2>&1)"; done
sec dmesg;    dmesg 2>&1 | grep -i -E 'wlan|rtl|rtw|8188|8189|8723|ssv|mac addr|efuse' | tail -n 30
sec initd;    ls -l /etc/init.d 2>&1
sec net-scripts; for f in /etc/init.d/*; do
                   if grep -q -i -E 'wlan|wpa_supplicant|insmod|modprobe|udhcpc|ifup' "$f" 2>&1; then
                     echo "--- $f"; grep -v -i -E 'psk|pass|key' "$f" 2>&1
                   fi
                 done
sec interfaces; grep -v -i -E 'psk|pass|key' /etc/network/interfaces 2>&1
sec modprobe.d; ls /etc/modprobe.d /etc/modules* 2>&1; cat /etc/modprobe.d/* /etc/modules 2>&1 | grep -v -i -E 'psk|pass'
sec wpa-files;  ls -l /etc/wpa_supplicant* /etc/wpa_supplicant/ /etc/wifi* 2>&1
sec processes;  ps w 2>&1 | grep -i -E '[w]pa|[u]dhcpc|[d]hcp|[h]ostapd'
EOF
}

case "${1:-}" in
  info)  info ;;
  netinfo) netinfo ;;
  connect)    "${SSH[@]}" -fN "$HOST" && echo "shared connection open for 30 min (close: ./deploy.sh disconnect)" ;;
  disconnect) "${SSH[@]}" -O exit "$HOST" ;;
  root-setup)
         # su is broken like sudo (not setuid root), but root may log in over SSH directly.
         echo "running tools/root_setup.sh as root@${HOST#*@} (enter the ROOT password):"
         "${SSH[@]}" "root@${HOST#*@}" "sh -s '$DIR'" < "$ROOT/tools/root_setup.sh" ;;
  push)  push ;;
  run)   push; stop; "${SSH[@]}" -t "$HOST" "cd '$DIR' && python3 thermal/server.py" ;;
  start) push; stop
         "${SSH[@]}" "$HOST" "$CTL start"
         echo "http://${HOST#*@}:8080  (log: ./deploy.sh log)" ;;
  stop)  stop ;;
  status) "${SSH[@]}" "$HOST" "$CTL status" ;;
  autostart)
         push
         echo "installing the boot script as root@${HOST#*@} (enter the ROOT password):"
         "${SSH[@]}" "root@${HOST#*@}" "sh -s on '$DIR'" < "$ROOT/tools/autostart.sh"
         echo "http://${HOST#*@}:8080" ;;
  pin-mac)
         mac="${2:-00:e0:4c:e1:94:c5}"
         echo "pinning the WiFi MAC to $mac as root@${HOST#*@} (enter the ROOT password):"
         "${SSH[@]}" "root@${HOST#*@}" "sh -s '$mac'" < "$ROOT/tools/pin_mac.sh" ;;
  usb-local)
         echo "USB/hotspot DHCP: ${2:-on} (no router/DNS for clients) as root@${HOST#*@} (enter the ROOT password):"
         "${SSH[@]}" "root@${HOST#*@}" "sh -s '${2:-on}'" < "$ROOT/tools/usb_local.sh" ;;
  autostart-off)
         echo "removing the boot script as root@${HOST#*@} (enter the ROOT password):"
         "${SSH[@]}" "root@${HOST#*@}" "sh -s off '$DIR'" < "$ROOT/tools/autostart.sh" ;;
  log)   "${SSH[@]}" "$HOST" "tail -n 50 -f $LOG" ;;
  probe) push; shift; dev="${1:-/dev/ttyS1}"; shift || true
         "${SSH[@]}" -t "$HOST" "python3 '$DIR/tools/uart_probe.py' '$dev' $*" ;;
  ssh)   "${SSH[@]}" "$HOST" ;;
  *)     sed -n '2,/^# Override/p' "$0"; exit 1 ;;
esac
