"""What the zigbee and zwave modules share: network (PoE/Ethernet) radios reached over TCP, and starting or stopping
their containers. Both radios are network devices, so nothing is plugged into the box."""
import re
import subprocess
import threading

import solar_setup as ss

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def check_address(host, port):
    """(host, port) cleaned, or ValueError with a reason for the setup page."""
    host = (host or "").strip()
    port = str(port or "").strip()
    if not HOST_RE.match(host):
        raise ValueError("the radio's IP address or name, as its own web page shows it")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("the radio's TCP port, a number from 1 to 65535")
    return host, int(port)


def port_link(port):
    """A link to a port on this box, at whatever address the browser used to reach the setup page."""
    return (f"<a class='portlink' data-port='{int(port)}' href='#'>port {int(port)}</a><script>document.querySelectorAll"
            "('a.portlink').forEach(a=>a.href=location.protocol+'//'+location.hostname+':'+a.dataset.port)</script>")


def tcp_url(s):
    return f"tcp://{s['host']}:{s['port']}" if s.get("host") and s.get("port") else ""


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def start(stack, service, before=None):
    """(Re)create a radio's container in the background, after `before(stack)` (seeding its files)."""
    def run():
        if before:
            before(stack)
        _run(ss.compose_cmd(stack) + ["up", "-d", "--force-recreate", service])
    threading.Thread(target=run, daemon=True).start()


def stop(stack, service):
    _run(ss.compose_cmd(stack) + ["rm", "-s", "-f", service])


def running(stack, service):
    """True while the service's container runs."""
    r = _run(ss.compose_cmd(stack) + ["ps", "--status", "running", "--services", service])
    return r.returncode == 0 and service in r.stdout.split()


def reachable(host, port, timeout=3):
    """Can the box open a TCP connection to the radio?"""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False
