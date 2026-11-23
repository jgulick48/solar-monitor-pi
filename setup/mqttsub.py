"""A small MQTT 3.1.1 subscriber (standard library only): connect with a user and password, subscribe to topic
filters, and call on_message(topic, payload) for each message until the connection drops. The box only needs to
listen to its own Mosquitto (Home Assistant's statestream), so this does QoS 0 and nothing more.

  run("127.0.0.1", 1883, "homeassistant", password, ["solar/ha/#"], on_message)   # returns when disconnected
"""
import socket
import struct
import time

KEEPALIVE = 60


def _str(s):
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _packet(ptype, body):
    """Fixed header (type and flags) + remaining length + body."""
    n, rl = len(body), b""
    while True:
        byte, n = n % 128, n // 128
        rl += bytes([byte | (0x80 if n else 0)])
        if not n:
            break
    return bytes([ptype]) + rl + body


def connect_packet(client_id, user, password):
    flags = 0x02 | (0x80 if user else 0) | (0x40 if password else 0)     # clean session
    body = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", KEEPALIVE) + _str(client_id)
    if user:
        body += _str(user)
    if password:
        body += _str(password)
    return _packet(0x10, body)


def subscribe_packet(packet_id, topics):
    body = struct.pack("!H", packet_id) + b"".join(_str(t) + b"\x00" for t in topics)
    return _packet(0x82, body)


class Closed(Exception):
    pass


def _read(sock, n):
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise Closed("the broker closed the connection")
        out += chunk
    return out


def read_packet(sock):
    """(type byte, body) of the next packet."""
    first = _read(sock, 1)[0]
    mult, length = 1, 0
    while True:
        b = _read(sock, 1)[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return first, _read(sock, length)


def parse_publish(first, body):
    """(topic, payload) of a PUBLISH packet (a retained message, the last one the broker kept, has bit 0 set)."""
    tl = struct.unpack("!H", body[:2])[0]
    topic = body[2:2 + tl].decode("utf-8", "replace")
    rest = body[2 + tl:]
    if (first >> 1) & 0x03:          # QoS 1/2 carry a packet id
        rest = rest[2:]
    return topic, rest


def run(host, port, user, password, topics, on_message, client_id=None, timeout=10, stop=None):
    """Connect, subscribe and deliver messages until the connection drops (raises Closed or OSError), or until
    `stop` (a threading.Event) is set."""
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(connect_packet(client_id or f"solar-box-{int(time.time())}", user, password))
        first, body = read_packet(sock)
        if first >> 4 != 2 or len(body) < 2 or body[1] != 0:
            raise Closed(f"the broker refused the connection (code {body[1] if len(body) > 1 else '?'})")
        sock.sendall(subscribe_packet(1, topics))
        sock.settimeout(KEEPALIVE / 2)
        last_ping = time.time()
        while not (stop and stop.is_set()):
            try:
                first, body = read_packet(sock)
            except socket.timeout:
                first = None
            if first is not None and first >> 4 == 3:
                topic, payload = parse_publish(first, body)
                on_message(topic, payload)
            if time.time() - last_ping >= KEEPALIVE / 2:
                sock.sendall(b"\xc0\x00")        # PINGREQ
                last_ping = time.time()
    finally:
        sock.close()
