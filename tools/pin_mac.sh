#!/bin/sh
# Run as root on the UnitV2 (./deploy.sh pin-mac [MAC]). Safe to re-run; takes effect at next boot.
#
# The RTL8188FU WiFi chip has no MAC in its efuse, so the 8188fu driver invents a random one
# at every boot, and the router hands out a new IP each time. The driver takes the MAC as a
# module option (rtw_initmac), but S10loadfsko loads every /lib/modules/*.ko with no options.
# This rewrites S10loadfsko to pass rtw_initmac to the 8188fu module only (original kept as
# /etc/S10loadfsko.orig, outside init.d so rcS doesn't run it). wlan1 (the AP interface)
# derives its address from wlan0's, so it becomes fixed too.
#
# dhcpcd: if it identifies itself by a DUID (option "duid"), switch it to "clientid" (the
# MAC), so the router sees the same client every boot. Original kept as /etc/dhcpcd.conf.orig.
#
# Undo: ./deploy.sh pin-mac off
set -e
ARG="${1:?MAC or off}"
INIT=/etc/init.d
F="$INIT/S10loadfsko"
DHCP=/etc/dhcpcd.conf

if [ "$ARG" = off ]; then
  [ -f /etc/S10loadfsko.orig ] && cp /etc/S10loadfsko.orig "$F" && echo "restored $F"
  [ -f /etc/dhcpcd.conf.orig ] && cp /etc/dhcpcd.conf.orig "$DHCP" && echo "restored $DHCP"
  echo "random MAC again from the next boot"
  exit 0
fi

MAC=$(echo "$ARG" | tr 'A-F' 'a-f')
echo "$MAC" | grep -q -E '^([0-9a-f]{2}:){5}[0-9a-f]{2}$' || { echo "bad MAC: $ARG"; exit 1; }

echo "[1] $F"
ls /lib/modules/*.ko
[ -f /etc/S10loadfsko.orig ] || cp "$F" /etc/S10loadfsko.orig
cat > "$F" <<EOF
#!/bin/sh
# Stock module loader, plus a fixed WiFi MAC (installed by ./deploy.sh pin-mac,
# original: /etc/S10loadfsko.orig). The RTL8188FU otherwise gets a random MAC every boot.
WIFI_MAC=$MAC

case "\$1" in
        start)
                printf "Loading kernel modules: "
                for kofile in /lib/modules/*.ko
                    do
                    case "\$kofile" in
                        *8188fu*) /sbin/insmod \$kofile rtw_initmac=\$WIFI_MAC ;;
                        *)        /sbin/insmod \$kofile ;;
                    esac
                done
                [ \$? = 0 ] && echo "OK" || echo "FAIL"
                ;;
        stop)
                ;;
        restart|reload)
                \$0 stop
                \$0 start
                ;;
        *)
                echo "Usage: \$0 {start|stop|restart}"
                exit 1
esac

exit 0
EOF
chmod 755 "$F"
echo "    8188fu will load with rtw_initmac=$MAC"

echo "[2] $DHCP"
if grep -q -E '^[[:space:]]*duid' "$DHCP"; then
  [ -f /etc/dhcpcd.conf.orig ] || cp "$DHCP" /etc/dhcpcd.conf.orig
  sed -i -E 's/^([[:space:]]*duid.*)/# \1   (replaced by pin-mac: identify by MAC instead)/' "$DHCP"
  grep -q -E '^[[:space:]]*clientid' "$DHCP" || echo "clientid" >> "$DHCP"
  echo "    switched dhcpcd from duid to clientid"
else
  echo "    no duid line, left as is"
fi
grep -n -E '^[[:space:]]*(clientid|duid|hostname)' "$DHCP" || true

echo
echo "done. Reboot to apply (WiFi drops for ~30 s):  reboot"
echo "After that the UnitV2 is $MAC: reserve an IP for it on your router."
