#!/usr/bin/env bash
# Reduce host-daemon work per Mininet interface while experiments run.
#
#   sudo ./mitigate.sh on       apply (saves original state first)
#   sudo ./mitigate.sh off      restore original state
#   ./mitigate.sh status        show current state (no root needed)
#
# Changes made by "on" (all reverted by "off"):
#   1. networkd-dispatcher stopped (it reruns `networkctl list` for every new
#      interface -> cost grows with the square of the interface count).
#   2. systemd-networkd told to leave Mininet interfaces (s*-eth*, h*-eth*)
#      unmanaged.
#   3. udev does no per-interface work for Mininet interfaces:
#      - /etc overrides of 70-iscsi-network-interface.rules and
#        99-systemd.rules skip the open-iscsi handler and systemd-sysctl
#        for Mininet names (systemd 249 ignores OPTIONS+="last_rule", so the
#        affected rules themselves have to be overridden);
#      - a .link file so net_setup_link applies no naming/MAC/offload policy.
#   4. IPv6 disabled by default on NEW root-namespace interfaces (existing
#      ones such as ens18 keep IPv6). Experiments here use IPv4 only.
#   5. /etc/resolv.conf points at the upstream DNS servers directly, and
#      systemd-resolved is stopped (it tracks every link even when unused).
#   6. systemd-udevd stopped, with its sockets so nothing re-activates it.
#      Even with 3., udev 249 spent ~2600 CPU-s handling the ~8.6k uevents
#      of a 128x32 fabric. Mininet and OVS do not need udev for veths; the
#      only loss is hotplug handling while it is stopped. If mitigations are
#      already ON from an older version, add this step with:
#        sudo ./mitigate.sh udevd-stop
set -euo pipefail

STATE_DIR=/var/lib/mininet-mitigate
NETWORK_FILE=/etc/systemd/network/10-mininet-unmanaged.network
LINK_FILE=/etc/systemd/network/10-mininet.link
UDEV_ISCSI=/etc/udev/rules.d/70-iscsi-network-interface.rules
UDEV_SYSTEMD=/etc/udev/rules.d/99-systemd.rules
OLD_UDEV_FILE=/etc/udev/rules.d/00-mininet-skip.rules  # from the first version
MININET_NAMES='s*-eth* h*-eth*'
SKIP='KERNEL!="s*-eth*", KERNEL!="h*-eth*", '
MARKER='# Temporary: created by mitigate.sh for Mininet experiments.'
UDEVD_UNITS=(systemd-udevd-control.socket systemd-udevd-kernel.socket systemd-udevd.service)

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "Run with sudo." >&2
        exit 1
    fi
}

ours() { [[ -f $1 ]] && grep -qF "$MARKER" "$1"; }

status() {
    echo "mitigation state dir:  $([[ -d $STATE_DIR ]] && echo "present (ON)" || echo "absent (OFF)")"
    echo "networkd-dispatcher:   $(systemctl is-active networkd-dispatcher 2>/dev/null || true)"
    echo "systemd-resolved:      $(systemctl is-active systemd-resolved 2>/dev/null || true)"
    echo "systemd-udevd:         $(systemctl is-active systemd-udevd 2>/dev/null || true)"
    echo "networkd unmanaged:    $([[ -f $NETWORK_FILE ]] && echo yes || echo no)"
    echo "udev .link override:   $([[ -f $LINK_FILE ]] && echo yes || echo no)"
    echo "udev rule overrides:   iscsi=$(ours $UDEV_ISCSI && echo yes || echo no) systemd=$(ours $UDEV_SYSTEMD && echo yes || echo no)"
    echo "ipv6 default disabled: $(sysctl -n net.ipv6.conf.default.disable_ipv6)"
    echo "/etc/resolv.conf ->    $(readlink /etc/resolv.conf || echo '(regular file)')"
    echo "nameservers in use:    $(awk '/^nameserver/{printf "%s ", $2}' /etc/resolv.conf)"
}

on() {
    need_root
    if [[ -d $STATE_DIR ]]; then
        echo "Already ON (found $STATE_DIR). Run 'off' first." >&2
        exit 1
    fi
    for f in "$UDEV_ISCSI" "$UDEV_SYSTEMD" "$LINK_FILE" "$NETWORK_FILE"; do
        if [[ -e $f ]] && ! ours "$f"; then
            echo "Refusing to overwrite existing $f (not created by this script)." >&2
            exit 1
        fi
    done
    upstream=/run/systemd/resolve/resolv.conf
    if ! grep -q '^nameserver' "$upstream" 2>/dev/null; then
        echo "$upstream has no upstream nameservers; aborting." >&2
        exit 1
    fi

    mkdir -p "$STATE_DIR"
    systemctl is-active networkd-dispatcher >"$STATE_DIR/dispatcher" 2>/dev/null || true
    systemctl is-active systemd-resolved >"$STATE_DIR/resolved" 2>/dev/null || true
    sysctl -n net.ipv6.conf.default.disable_ipv6 >"$STATE_DIR/ipv6_default"
    if [[ -L /etc/resolv.conf ]]; then
        readlink /etc/resolv.conf >"$STATE_DIR/resolv_link"
    else
        cp -a /etc/resolv.conf "$STATE_DIR/resolv.conf.orig"
    fi

    # 5. DNS: static copy of the upstream list (the /run file is owned by
    #    resolved, which is about to stop).
    rm -f /etc/resolv.conf
    { echo "$MARKER"; grep -E '^(nameserver|search|options)' "$upstream"; } >/etc/resolv.conf

    # 1.
    systemctl stop networkd-dispatcher

    # 2. + 3. (.link)
    printf '%s\n[Match]\nName=%s\n\n[Link]\nUnmanaged=yes\n' \
        "$MARKER" "$MININET_NAMES" >"$NETWORK_FILE"
    printf '%s\n[Match]\nOriginalName=%s\n\n[Link]\nNamePolicy=\nMACAddressPolicy=none\n' \
        "$MARKER" "$MININET_NAMES" >"$LINK_FILE"
    networkctl reload

    # 3. udev rule overrides (same file name in /etc masks the /lib copy)
    rm -f "$OLD_UDEV_FILE"
    { echo "$MARKER"
      sed -E "s/^SUBSYSTEM==\"net\", /SUBSYSTEM==\"net\", $SKIP/" \
          /lib/udev/rules.d/70-iscsi-network-interface.rules; } >"$UDEV_ISCSI"
    { echo "$MARKER"
      sed -E "s|^(ACTION==\"add\", SUBSYSTEM==\"net\", KERNEL!=\"lo\", )(RUN\+=\"/lib/systemd/systemd-sysctl)|\1$SKIP\2|" \
          /lib/udev/rules.d/99-systemd.rules; } >"$UDEV_SYSTEMD"
    if ! grep -q "$SKIP"'RUN+="/lib/systemd/systemd-sysctl' "$UDEV_SYSTEMD" ||
       ! grep -q "$SKIP"'RUN+="/lib/open-iscsi' "$UDEV_ISCSI"; then
        echo "WARNING: udev overrides did not patch the expected lines." >&2
    fi
    udevadm control --reload

    # 4.
    sysctl -q -w net.ipv6.conf.default.disable_ipv6=1

    # 5. (stop resolved last, after resolv.conf no longer points at it)
    systemctl stop systemd-resolved

    # 6.
    udevd_stop_steps

    echo "Mitigations ON."
    status
}

udevd_stop_steps() {
    systemctl is-active systemd-udevd >"$STATE_DIR/udevd" 2>/dev/null || true
    systemctl stop "${UDEVD_UNITS[@]}"
}

udevd_stop() {
    need_root
    if [[ ! -d $STATE_DIR ]]; then
        echo "Mitigations are OFF; run 'on' (it includes this step)." >&2
        exit 1
    fi
    if [[ -f $STATE_DIR/udevd ]]; then
        echo "udevd step already applied." >&2
        exit 1
    fi
    udevd_stop_steps
    echo "systemd-udevd stopped."
    status
}

off() {
    need_root
    if [[ ! -d $STATE_DIR ]]; then
        echo "Not ON (no $STATE_DIR); nothing to restore." >&2
        exit 1
    fi
    # udevd first: the steps below call `udevadm control --reload`.
    if [[ "$(cat "$STATE_DIR/udevd" 2>/dev/null)" == active ]]; then
        systemctl start systemd-udevd-control.socket systemd-udevd-kernel.socket
        systemctl start systemd-udevd.service
    fi
    if [[ "$(cat "$STATE_DIR/resolved" 2>/dev/null)" == active ]]; then
        systemctl start systemd-resolved
    fi
    if [[ -f $STATE_DIR/resolv_link ]]; then
        rm -f /etc/resolv.conf
        ln -s "$(cat "$STATE_DIR/resolv_link")" /etc/resolv.conf
    elif [[ -f $STATE_DIR/resolv.conf.orig ]]; then
        rm -f /etc/resolv.conf
        cp -a "$STATE_DIR/resolv.conf.orig" /etc/resolv.conf
    fi

    sysctl -q -w net.ipv6.conf.default.disable_ipv6="$(cat "$STATE_DIR/ipv6_default")"

    rm -f "$OLD_UDEV_FILE"
    for f in "$UDEV_ISCSI" "$UDEV_SYSTEMD"; do ours "$f" && rm -f "$f"; done
    udevadm control --reload

    rm -f "$NETWORK_FILE" "$LINK_FILE"
    networkctl reload

    if [[ "$(cat "$STATE_DIR/dispatcher")" == active ]]; then
        systemctl start networkd-dispatcher
    fi

    rm -rf "$STATE_DIR"
    echo "Mitigations OFF (original state restored)."
    status
}

case "${1:-}" in
    on) on ;;
    off) off ;;
    udevd-stop) udevd_stop ;;
    status) status ;;
    *) echo "usage: $0 on|off|udevd-stop|status" >&2; exit 2 ;;
esac
