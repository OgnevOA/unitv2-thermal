#!/bin/sh
# Run as root on the UnitV2 (./deploy.sh usb-local [off]). Safe to re-run.
#
# The UnitV2's DHCP server (USB Ethernet + its own hotspot, 10.254.239.x) tells clients that
# 10.254.239.1 is their router and DNS server. The UnitV2 has no internet behind that network,
# so a phone that prefers wired links (iPhone) loses internet while plugged in. This stops the
# server from announcing a router and DNS: clients then use the link for 10.254.239.x only,
# and keep WiFi/cellular for everything else.
#
# Handles ISC dhcpd ("option routers ...;"), busybox udhcpd ("opt router ...") and dnsmasq.
# Every changed file is backed up as <file>.orig-usblocal; "off" restores the backups.
set -e
ACTION="${1:-on}"
INIT=/etc/init.d
SUFFIX=.orig-usblocal

echo "== DHCP servers running"
ps w | grep -E '[d]hcpd|[u]dhcpd|[d]nsmasq' || echo "   (none found)"

# Candidate config files: the usual places, plus any *.conf the boot scripts mention.
CANDS="/etc/dhcp/dhcpd.conf /etc/dhcpd.conf /etc/udhcpd.conf /etc/dnsmasq.conf"
for s in "$INIT/S80dhcp-server" "$INIT/S80dnsmasq"; do
  [ -f "$s" ] && CANDS="$CANDS $(grep -o -E '/[^ "]+\.conf' "$s" | tr '\n' ' ')"
done
FILES=""
for f in $CANDS; do
  [ -f "$f" ] || continue
  case " $FILES " in *" $f "*) ;; *) FILES="$FILES $f" ;; esac
done
echo "== config files:$FILES"

restart_servers() {
  for s in "$INIT/S80dhcp-server" "$INIT/S80dnsmasq"; do
    [ -x "$s" ] && "$s" restart 2>&1 | sed 's/^/   /'
  done
  true
}

if [ "$ACTION" = off ]; then
  for f in $FILES; do
    [ -f "$f$SUFFIX" ] && cp "$f$SUFFIX" "$f" && echo "restored $f"
  done
  restart_servers
  echo "done: clients get router/DNS 10.254.239.1 again (re-plug the phone)"
  exit 0
fi

CHANGED=0
for f in $FILES; do
  # Lines that hand out a router or DNS server, in ISC dhcpd / udhcpd syntax (not yet commented).
  RE='^[[:space:]]*(option[[:space:]]+(routers|domain-name-servers|router|dns)|opt[[:space:]]+(router|dns))([[:space:]]|$)'
  if grep -q -E "$RE" "$f"; then
    [ -f "$f$SUFFIX" ] || cp "$f" "$f$SUFFIX"
    echo "-- $f: disabling"
    grep -n -E "$RE" "$f" | sed 's/^/   /'
    sed -i -E "s/$RE/# usb-local: &/" "$f"
    CHANGED=1
  fi
  # dnsmasq serving DHCP: an option given with no value is not sent at all.
  if grep -q -E '^[[:space:]]*dhcp-range' "$f" && ! grep -q '^dhcp-option=3$' "$f"; then
    [ -f "$f$SUFFIX" ] || cp "$f" "$f$SUFFIX"
    printf '\n# usb-local: no router (3) or DNS (6) for clients\ndhcp-option=3\ndhcp-option=6\n' >> "$f"
    echo "-- $f: dnsmasq, added empty dhcp-option=3 / 6"
    CHANGED=1
  fi
done

if [ "$CHANGED" = 0 ] && grep -q '# usb-local' $FILES 2>&1; then
  echo "already applied, nothing to change"
  exit 0
fi
if [ "$CHANGED" = 0 ]; then
  echo
  echo "Nothing recognised, nothing changed. Config files for reference:"
  for f in $FILES; do echo "--- $f"; grep -v -E '^[[:space:]]*(#|$)' "$f" | grep -v -i -E 'key|secret|pass'; done
  for s in "$INIT/S80dhcp-server" "$INIT/S80dnsmasq"; do echo "--- $s"; cat "$s" 2>&1; done
  exit 1
fi

echo "== restarting DHCP"
restart_servers
echo
echo "done. Unplug and re-plug the phone so it gets a new lease."
