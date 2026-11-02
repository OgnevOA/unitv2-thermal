#!/usr/bin/env python3
"""Bring-up tool for the GY-MCU90640 on a UART. Stdlib only.

Examples (on the UnitV2):
    python3 uart_probe.py /dev/ttyS1                  # listen 5 s at 115200, report frames
    python3 uart_probe.py /dev/ttyS1 --scan           # try common baud rates
    python3 uart_probe.py /dev/ttyS1 --rate 4         # send "4 Hz" command, then measure
    python3 uart_probe.py /dev/ttyS1 --send A5250?    # raw command, '?' = auto checksum
    python3 uart_probe.py /dev/ttyS1 --set-baud 460800 --save
"""

import argparse
import binascii
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "thermal"))

import mlx_uart as m  # noqa: E402

SCAN_BAUDS = (115200, 460800, 9600, 57600, 230400, 921600)


def parse_hex_cmd(s):
    s = s.replace(" ", "").replace(":", "")
    auto = s.endswith("?")
    if auto:
        s = s[:-1]
    data = bytearray(binascii.unhexlify(s))
    if auto:
        data.append(sum(data) & 0xFF)
    return bytes(data)


def listen(dev, baud, seconds, send=(), quiet=False):
    fd = m.open_serial(dev, baud)
    try:
        for c in send:
            print("TX " + binascii.hexlify(c).decode())
            os.write(fd, c)
            time.sleep(0.1)
        if send:
            time.sleep(0.5)
            m.termios.tcflush(fd, m.termios.TCIFLUSH)  # drop bytes sent at the old setting

        raw = bytearray()
        t0 = time.time()
        while time.time() - t0 < seconds:
            raw += os.read(fd, 4096)
    finally:
        os.close(fd)

    n = len(raw)
    headers = raw.count(m.HEADER + m.LEN_FIELD)
    res = {"baud": baud, "bytes": n, "bps": n / seconds, "headers": headers}

    # Try every checksum flavour on the same capture.
    for mode in ("word", "byte", "none"):
        p = m.FrameParser(checksum=mode)
        frames = p.feed(bytes(raw))
        res[mode] = len(frames)
        if mode == "none":
            res["last"] = frames[-1] if frames else None

    if quiet:
        return res

    print("\n== %s @ %d baud, %.1f s" % (dev, baud, seconds))
    print("bytes received: %d (%.0f B/s)" % (n, n / seconds))
    print("first 64 bytes: %s" % binascii.hexlify(bytes(raw[:64])).decode())
    print("frame headers (5A5A0206): %d  -> %.2f frames/s" % (headers, headers / seconds))
    print("valid frames by checksum: word=%d byte=%d none=%d" % (res["word"], res["byte"], res["none"]))
    if res["last"]:
        f = res["last"]
        temps, amb = m.decode_raw(f)
        print("last frame: min %.2f  max %.2f  mean %.2f  centre %.2f  ambient %.2f degC" % (
            min(temps), max(temps), sum(temps) / len(temps),
            temps[12 * m.COLS + 16], amb))
        print("checksum field %04x, word-sum %04x, byte-sum %04x" % (
            m.frame_checksum(f), m.word_checksum(f), m.byte_checksum(f)))
        print("coarse 8x6 view (degC):")
        for r in range(0, m.ROWS, 4):
            print("  " + " ".join("%5.1f" % temps[r * m.COLS + c] for c in range(0, m.COLS, 4)))
    elif n:
        print("bytes arrive but no frame header found: wrong baud rate, or TX/RX swapped onto another device?")
    else:
        print("no bytes at all: check wiring (module TX -> UnitV2 RX), power, and device node.")
    return res


def loopback(dev, baud):
    """Write a pattern and read it back: identifies the Grove UART when its TX and RX are joined."""
    pattern = b"UnitV2-loopback-" + bytes(range(32, 96))
    fd = m.open_serial(dev, baud)
    try:
        m.termios.tcflush(fd, m.termios.TCIOFLUSH)
        os.write(fd, pattern)
        got = bytearray()
        t0 = time.time()
        while time.time() - t0 < 1.0 and len(got) < len(pattern):
            got += os.read(fd, 4096)
    finally:
        os.close(fd)
    if bytes(got) == pattern:
        print("%s: LOOPBACK OK, this is the port wired to TX/RX, and both directions work." % dev)
    elif got:
        print("%s: got %d bytes back but they differ (noise or another device): %s"
              % (dev, len(got), binascii.hexlify(bytes(got[:32])).decode()))
    else:
        print("%s: nothing came back, so this is not the port your TX/RX wires are on." % dev)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dev")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--scan", action="store_true", help="try common baud rates")
    ap.add_argument("--send", action="append", default=[], help="raw hex command; trailing ? appends checksum")
    ap.add_argument("--rate", type=float, choices=sorted(m.RATE_ARGS), help="send frame-rate command (Hz)")
    ap.add_argument("--set-baud", type=int, choices=sorted(m.BAUD_ARGS),
                    help="send baud-rate command, then listen at the new rate")
    ap.add_argument("--save", action="store_true", help="send save-to-flash command after the others")
    ap.add_argument("--start", action="store_true",
                    help="send the 'continuous output' command (A5 35 02) before listening")
    ap.add_argument("--loopback", action="store_true",
                    help="port test: with the Grove TX and RX wires joined, check sent bytes come back")
    a = ap.parse_args()

    if a.loopback:
        loopback(a.dev, a.baud)
        return

    if a.start:
        a.send.insert(0, "A53502?")

    if a.scan:
        print("baud     bytes/s  headers  word  byte")
        for b in SCAN_BAUDS:
            try:
                r = listen(a.dev, b, min(a.seconds, 2.0), quiet=True)
            except ValueError as e:
                print("%-8d %s" % (b, e))
                continue
            print("%-8d %7.0f  %7d  %4d  %4d" % (b, r["bps"], r["headers"], r["word"], r["byte"]))
        return

    cmds = [parse_hex_cmd(s) for s in a.send]
    if a.rate is not None:
        cmds.append(m.cmd_rate(a.rate))  # float keys hash like ints: 4.0 == 4
    listen_baud = a.baud
    if a.set_baud:
        # Everything so far plus the baud command goes out at the current rate,
        # then we reopen at the new rate (and save there, if asked).
        cmds.append(m.cmd_baud(a.set_baud))
        fd = m.open_serial(a.dev, a.baud)
        for c in cmds:
            print("TX " + binascii.hexlify(c).decode())
            os.write(fd, c)
            time.sleep(0.1)
        os.close(fd)
        time.sleep(0.5)
        listen_baud = a.set_baud
        cmds = []
    if a.save:
        cmds.append(m.CMD_SAVE)
    listen(a.dev, listen_baud, a.seconds, send=cmds)


if __name__ == "__main__":
    main()
