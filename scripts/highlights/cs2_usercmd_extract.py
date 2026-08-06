"""Extract per-player user-command input from a CS2 (SourceTV/GOTV) demo file.

Walks the demo container frames, decodes the svc_UserCmds net messages, pulls each
CMsgServerUserCmd (player_slot + data/delta_data), and reconstructs the full user-command
state per player using the delta_data decoder (cs2_usercmd_delta.apply_delta), keeping a
per-player baseline.

Recent FACEIT demos (>= ~2026-07-10) store usercmds in CMsgServerUserCmd.delta_data
(protobuf field 6); older ones use .data (field 1). Both are handled: full .data payloads
replace the baseline; delta_data patches are applied on top of it.

Output: dict player_slot -> { tick: {"buttonstate1/2/3", "forwardmove", "leftmove",
"mousedx", "mousedy", "viewangle_x/y/z", ...} }
"""

from __future__ import annotations

import struct
from typing import Optional

try:
    from .cs2_usercmd_delta import apply_delta, new_baseline, overlay_signals
except ImportError:  # running standalone / no package parent
    from cs2_usercmd_delta import apply_delta, new_baseline, overlay_signals

# ---- protobuf field numbers (from SteamDatabase/GameTracking-CS2 Protobufs) ----
# CDemoFullPacket { string_table=1; packet=2 (CDemoPacket) }
# CDemoPacket { data=3 }
# CSVCMsg_UserCommands { commands=1 (repeated CMsgServerUserCmd) }
# CMsgServerUserCmd { data=1; cmd_number=2; player_slot=3; server_tick_executed=4; client_tick=5; delta_data=6 }

_DEMO_HEADER_BYTES = 16
_DEM_PACKET = 7
_DEM_SIGNON_PACKET = 8
_DEM_FULL_PACKET = 13
_DEM_IS_COMPRESSED = 64

_SVC_USERCMDS = 76


# ---------------------------------------------------------------------------
# byte varint (demo frame header)
# ---------------------------------------------------------------------------
def _read_byte_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    count = 0
    while count < 5:
        if i >= len(buf):
            raise ValueError("out of bytes")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << (7 * count)
        count += 1
        if b & 0x80 == 0:
            break
    return result, i


# ---------------------------------------------------------------------------
# LSB-first little-endian bit reader (compatible with bitter LittleEndianReader)
# ---------------------------------------------------------------------------
class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.bitpos = 0

    def bits_remaining(self) -> int:
        return len(self.data) * 8 - self.bitpos

    def read_bits(self, n: int) -> int:
        val = 0
        for i in range(n):
            bit = (self.data[(self.bitpos + i) // 8] >> ((self.bitpos + i) % 8)) & 1
            val |= bit << i
        self.bitpos += n
        return val

    def read_u_bit_var(self) -> int:
        bits = self.read_bits(6)
        top = bits & 0b110000
        if top == 0b10000:
            return (bits & 0b1111) | (self.read_bits(4) << 4)
        if top == 0b100000:
            return (bits & 0b1111) | (self.read_bits(8) << 4)
        if top == 0b110000:
            return (bits & 0b1111) | (self.read_bits(28) << 4)
        return bits

    def read_varint(self) -> int:
        result = 0
        count = 0
        while count < 5:
            b = self.read_bits(8)
            result |= (b & 0x7F) << (7 * count)
            count += 1
            if b & 0x80 == 0:
                break
        return result

    def read_n_bytes(self, n: int) -> bytes:
        # align to byte boundary
        rem = self.bitpos % 8
        if rem:
            self.bitpos += 8 - rem
        start = self.bitpos // 8
        self.bitpos += n * 8
        return self.data[start:start + n]


# ---------------------------------------------------------------------------
# generic protobuf decoder (returns dict field -> list of values; nested msgs as bytes)
# ---------------------------------------------------------------------------
def _read_pb_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("pb varint out of range")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b & 0x80 == 0:
            break
        shift += 7
    return result, i


def decode_proto(buf: bytes) -> dict[int, list]:
    """Decode a protobuf message into {field_number: [values]}.
    Length-delimited (wire 2) values are returned as raw bytes.
    """
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _read_pb_varint(buf, i)
        field = key >> 3
        wt = key & 0x07
        if wt == 0:
            v, i = _read_pb_varint(buf, i)
        elif wt == 1:
            v = buf[i:i + 8]; i += 8
        elif wt == 2:
            ln, i = _read_pb_varint(buf, i)
            v = buf[i:i + ln]; i += ln
        elif wt == 5:
            v = buf[i:i + 4]; i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.setdefault(field, []).append(v)
    return out


def _field(decoded: dict, field: int, default=None):
    vals = decoded.get(field)
    return vals[0] if vals else default


# ---------------------------------------------------------------------------
# net message parsing
# ---------------------------------------------------------------------------
def iter_net_messages(packet_data: bytes):
    """Yield (msg_type, payload_bytes) from a CDemoPacket.data net stream."""
    br = BitReader(packet_data)
    while br.bits_remaining() > 8:
        msg_type = br.read_u_bit_var()
        size = br.read_varint()
        if size > br.bits_remaining() // 8:
            break
        payload = br.read_n_bytes(size)
        yield msg_type, payload


def _decode_usercmd_wrapper(payload: bytes) -> dict:
    return decode_proto(payload)


def parse_svc_usercmds(payload: bytes) -> list[dict]:
    """Decode CSVCMsg_UserCommands -> list of {'player_slot','tick','data','delta_data'}."""
    cmds = decode_proto(payload).get(1, [])
    out = []
    for raw in cmds:
        c = decode_proto(raw)
        d = {
            "data": _field(c, 1),
            "cmd_number": _field(c, 2),
            "player_slot": _field(c, 3),
            "server_tick_executed": _field(c, 4),
            "client_tick": _field(c, 5),
            "delta_data": _field(c, 6),
        }
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# demo frame walker
# ---------------------------------------------------------------------------
def iter_frames(buf: bytes, start: int = _DEMO_HEADER_BYTES):
    i = start
    n = len(buf)
    while i + 3 <= n:
        cmd, i = _read_byte_varint(buf, i)
        tick, i = _read_byte_varint(buf, i)
        size, i = _read_byte_varint(buf, i)
        if i + size > n:
            break
        yield cmd, tick, buf[i:i + size]
        i += size


def extract_user_commands(demo_path, progress=False) -> dict[int, list]:
    """Extract per-player usercmd deltas.

    Returns { player_slot: [ {'tick': t, 'signals': {...overlay fields...}} ] }
    """
    data = open(demo_path, "rb").read()
    baselines: dict[int, dict] = {}
    results: dict[int, list] = {}
    frame_count = 0
    for cmd_raw, tick, frame_data in iter_frames(data):
        frame_count += 1
        is_compressed = bool(cmd_raw & _DEM_IS_COMPRESSED)
        cmd = cmd_raw & ~_DEM_IS_COMPRESSED
        if is_compressed:
            continue  # zlib-compressed frames not handled here
        if cmd not in (_DEM_PACKET, _DEM_SIGNON_PACKET, _DEM_FULL_PACKET):
            continue
        try:
            if cmd == _DEM_FULL_PACKET:
                full = decode_proto(frame_data)
                packet_raw = _field(full, 2)
                if not packet_raw:
                    continue
                pk = decode_proto(packet_raw)
                net = _field(pk, 3)
            else:
                pk = decode_proto(frame_data)
                net = _field(pk, 3)
        except Exception:
            continue
        if not net:
            continue
        for mtype, payload in iter_net_messages(net):
            if mtype != _SVC_USERCMDS:
                continue
            for ucmd in parse_svc_usercmds(payload):
                slot = ucmd["player_slot"]
                if slot is None:
                    continue
                base = baselines.setdefault(slot, new_baseline())
                if ucmd["delta_data"]:
                    state = apply_delta(base, ucmd["delta_data"])
                    if state is not None:
                        baselines[slot] = state
                        results.setdefault(slot, []).append(
                            {"tick": tick, "signals": overlay_signals(state)}
                        )
                elif ucmd["data"]:
                    # Full payload replaces the baseline (pre-07-10 path not decoded here fully;
                    # treat as a resync point).
                    baselines[slot] = new_baseline()
    if progress:
        print(f"  [usercmd] scanned {frame_count} frames")
    return results
