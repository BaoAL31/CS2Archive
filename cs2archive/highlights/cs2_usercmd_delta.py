"""Decoder for CMsgServerUserCmd.delta_data payloads emitted by CS2's
codegen_delta_encoder (port of demoparser#343 src/parser/src/second_pass/usercmd_delta.rs).

Recent CS2 FACEIT demos (recorded >= ~2026-07-10) store per-player user commands in
CMsgServerUserCmd.delta_data (protobuf field 6) instead of the plain .data. This module
decodes those deltas back into the full user-command state (button states, movement,
mouse deltas) by keeping a per-player baseline and applying the delta-encoded updates.

Singular fields retain protobuf wire encoding except for wire type 7, which resets a
field to its declared default. Repeated input-history/subtick fields use the
replacement-list encoding (0x0f reset marker + sequential indices).

Call apply_delta(baseline, delta_bytes) -> new_state dict (or None on malformed input).
The returned dict exposes the fields the keyboard overlay needs:
  buttonstate1 / buttonstate2 / buttonstate3
  forwardmove / leftmove / upmove
  mousedx / mousedy
  viewangle_x / viewangle_y / viewangle_z
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# protobuf primitives
# ---------------------------------------------------------------------------
def _read_varint(buf: memoryview, i: int) -> tuple[Optional[int], int]:
    value = 0
    shift = 0
    while True:
        if i >= len(buf):
            return None, i
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if b & 0x80 == 0:
            return value, i
        shift += 7
        if shift > 70:
            return None, i


def _write_varint(value: int, out: bytearray) -> None:
    value &= 0xFFFFFFFFFFFFFFFF
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


# ---------------------------------------------------------------------------
# Message schema (field -> wire type, child message, reset fields, defaults)
# ---------------------------------------------------------------------------
class Schema:
    __slots__ = ("field_wire", "children", "reset", "varint_default")

    def __init__(self, field_wire: dict[int, int], children: dict[int, "Schema"],
                 reset: list[tuple[int, int]], varint_default: dict[int, int] = None):
        self.field_wire = field_wire
        self.children = children
        self.reset = reset
        self.varint_default = varint_default or {}

    def child(self, f: int) -> Optional["Schema"]:
        return self.children.get(f)

    def explicit_defaults(self) -> bytearray:
        out = bytearray()
        for f, wt in self.reset:
            _write_varint((f << 3) | wt, out)
            self._write_default(f, wt, out)
        return out

    def _write_default(self, f: int, wt: int, out: bytearray) -> bool:
        if wt == 0:
            _write_varint(self.varint_default.get(f, 0), out)
        elif wt == 1:
            out += b"\x00\x00\x00\x00\x00\x00\x00\x00"
        elif wt == 2:
            ch = self.child(f)
            nested = ch.explicit_defaults() if ch else bytearray()
            _write_varint(len(nested), out)
            out += nested
        elif wt == 5:
            out += b"\x00\x00\x00\x00"
        else:
            return False
        return True


BUTTONS = Schema({1: 0, 2: 0, 3: 0}, {}, [(1, 0), (2, 0), (3, 0)])
QANGLE = Schema({1: 5, 2: 5, 3: 5}, {}, [(1, 5), (2, 5), (3, 5)])

BASE_USERCMD = Schema(
    {1: 0, 2: 0, 8: 0, 9: 0, 10: 0, 11: 0, 12: 0, 14: 0, 17: 0, 20: 0, 21: 0,
     3: 2, 4: 2, 18: 2, 19: 2, 22: 2, 5: 5, 6: 5, 7: 5},
    {3: BUTTONS, 4: QANGLE},
    [(1, 0), (2, 0), (3, 2), (4, 2), (5, 5), (6, 5), (7, 5), (8, 0), (9, 0),
     (10, 0), (11, 0), (12, 0), (14, 0), (17, 0), (18, 2), (19, 2), (20, 0),
     (21, 0), (22, 2)],
    {14: 0x00FF_FFFF},
)

CSGO_USERCMD = Schema(
    {1: 2, 2: 2, 6: 0, 7: 0, 9: 0, 11: 0, 12: 0, 13: 0},
    {1: BASE_USERCMD},
    [(1, 2), (2, 2), (6, 0), (7, 0), (9, 0), (11, 0), (12, 0), (13, 0)],
    {6: 0xFFFFFFFF_FFFFFFFF, 7: 0xFFFFFFFF_FFFFFFFF},
)

INPUT_HISTORY = Schema(
    {2: 2, 12: 2, 13: 2, 14: 2, 15: 2, 66: 2, 67: 2, 68: 2, 69: 2,
     4: 0, 6: 0, 64: 0, 65: 0, 5: 5, 7: 5},
    {2: QANGLE, 69: QANGLE},
    [(2, 2), (4, 0), (5, 5), (6, 0), (7, 5), (64, 0), (65, 0)],
    {65: 0xFFFFFFFF_FFFFFFFF},
)

SUBTICK_MOVE = Schema(
    {1: 0, 2: 0, 3: 5, 4: 5, 5: 5, 8: 5, 9: 5},
    {},
    [(1, 0), (2, 0), (3, 5), (4, 5), (5, 5), (8, 5), (9, 5)],
)


def _sanitize_message(buf: bytes, schema: Schema) -> Optional[bytes]:
    out = bytearray()
    i = 0
    b = memoryview(buf)
    while i < len(b):
        key, i = _read_varint(b, i)
        if key is None:
            return None
        fld = key >> 3
        wt = key & 0x07
        if fld == 0:
            return None

        if wt == 7:
            nwt = schema.field_wire.get(fld)
            if nwt is None:
                return None
            _write_varint((fld << 3) | nwt, out)
            if not schema._write_default(fld, nwt, out):
                return None
            continue

        _write_varint(key, out)
        if wt == 0:
            val, i = _read_varint(b, i)
            if val is None:
                return None
            _write_varint(val, out)
        elif wt == 1:
            if i + 8 > len(b):
                return None
            out += b[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = _read_varint(b, i)
            if ln is None or i + ln > len(b):
                return None
            chunk = b[i:i + ln]
            i += ln
            child = schema.child(fld)
            val = _sanitize_message(chunk, child) if child else chunk
            if val is None:
                return None
            _write_varint(len(val), out)
            out += val
        elif wt == 5:
            if i + 4 > len(b):
                return None
            out += b[i:i + 4]
            i += 4
        else:
            return None
    return bytes(out)


def _decode_repeated(payloads: list[bytes], schema: Schema) -> Optional[list[dict]]:
    messages: list[dict] = []
    for payload in payloads:
        b = memoryview(payload)
        i = 0
        if b and b[0] == 0x0F:
            messages.clear()
            i = 1
        while i < len(b):
            key, i = _read_varint(b, i)
            if key is None or (key & 0x07) != 2:
                return None
            idx = key >> 3
            if idx != len(messages):
                return None
            ln, i = _read_varint(b, i)
            if ln is None or i + ln > len(b):
                return None
            raw = bytes(b[i:i + ln])
            i += ln
            sanitized = _sanitize_message(raw, schema)
            if sanitized is None:
                return None
            messages.append(_decode_msg(sanitized, schema))
    return messages


# Decode a sanitized message into a dict according to its schema.
def _decode_msg(buf: bytes, schema: Schema) -> dict:
    out: dict[int, object] = {}
    i = 0
    b = memoryview(buf)
    while i < len(b):
        key, i = _read_varint(b, i)
        if key is None:
            break
        fld = key >> 3
        wt = key & 0x07
        if wt == 0:
            val, i = _read_varint(b, i)
            if val is None:
                break
            out[fld] = val
        elif wt == 1:
            out[fld] = bytes(b[i:i + 8]); i += 8
        elif wt == 2:
            ln, i = _read_varint(b, i)
            if ln is None:
                break
            chunk = bytes(b[i:i + ln]); i += ln
            ch = schema.child(fld)
            out[fld] = _decode_msg(chunk, ch) if ch else chunk
        elif wt == 5:
            out[fld] = bytes(b[i:i + 4]); i += 4
    return out


# ---------------------------------------------------------------------------
# Delta message field numbers (from demoparser#343 structs)
# ---------------------------------------------------------------------------
def _parse_delta_user_cmd(buf: bytes) -> dict:
    """Decode DeltaCsgoUserCmdPb (after sanitize). Returns dict keyed by field number."""
    return _decode_msg(buf, CSGO_USERCMD)


def _parse_delta_base(buf: bytes) -> dict:
    return _decode_msg(buf, BASE_USERCMD)


# ---------------------------------------------------------------------------
# Main entry: apply a delta to a per-player baseline.
# baseline/result are dicts with keys:
#   base: dict with buttonstate1/2/3, forwardmove/leftmove/upmove, mousedx/mousedy,
#         viewangle_x/y/z, ...
# ---------------------------------------------------------------------------
def new_baseline() -> dict:
    return {"base": {}, "input_history": [], "attack1_start_history_index": None,
            "attack2_start_history_index": None, "left_hand_desired": None,
            "is_predicting_body_shot_fx": None, "is_predicting_head_shot_fx": None,
            "is_predicting_kill_ragdolls": None}


def apply_delta(baseline: dict, delta_data: bytes) -> Optional[dict]:
    sanitized = _sanitize_message(delta_data, CSGO_USERCMD)
    if sanitized is None:
        return None
    delta = _parse_delta_user_cmd(sanitized)

    import copy
    next_state = copy.deepcopy(baseline)
    base = next_state.setdefault("base", {})

    def _replace_if_some(target, value):
        if value is not None:
            return value
        return target

    # input_history
    ih_delta = delta.get(2)
    if ih_delta is not None:
        ih = _decode_repeated([ih_delta], INPUT_HISTORY) if isinstance(ih_delta, bytes) else None
        if ih is None:
            return None
        next_state["input_history"] = ih

    for tag, key in ((6, "attack1_start_history_index"), (7, "attack2_start_history_index"),
                     (9, "left_hand_desired"), (11, "is_predicting_body_shot_fx"),
                     (12, "is_predicting_head_shot_fx"), (13, "is_predicting_kill_ragdolls")):
        if tag in delta:
            next_state[key] = delta[tag]

    delta_base = delta.get(1)
    if isinstance(delta_base, dict):
        for tag, key in ((1, "legacy_command_number"), (2, "client_tick"), (17, "prediction_offset_ticks_x256"),
                         (5, "forwardmove"), (6, "leftmove"), (7, "upmove"), (8, "impulse"),
                         (9, "weaponselect"), (10, "random_seed"), (11, "mousedx"), (12, "mousedy"),
                         (14, "pawn_entity_handle"), (20, "consumed_server_angle_changes"),
                         (21, "cmd_flags")):
            if tag in delta_base:
                base[key] = delta_base[tag]

        # buttons (field 3 -> CInButtonStatePb: buttonstate1/2/3 = fields 1/2/3)
        if 3 in delta_base:
            db = delta_base[3]
            if isinstance(db, dict):
                buttons = base.setdefault("buttons", {})
                if 1 in db:
                    buttons["buttonstate1"] = db[1]
                if 2 in db:
                    buttons["buttonstate2"] = db[2]
                if 3 in db:
                    buttons["buttonstate3"] = db[3]

        # viewangles (field 4 -> CMsgQAngle: x/y/z = fields 1/2/3)
        if 4 in delta_base:
            dv = delta_base[4]
            if isinstance(dv, dict):
                va = base.setdefault("viewangles", {})
                if 1 in dv:
                    va["x"] = dv[1]
                if 2 in dv:
                    va["y"] = dv[2]
                if 3 in dv:
                    va["z"] = dv[3]

        # subtick moves (field 18 repeated)
        if 18 in delta_base and isinstance(delta_base[18], bytes):
            st = _decode_repeated([delta_base[18]], SUBTICK_MOVE)
            if st is None:
                return None
            base["subtick_moves"] = st

    return next_state


def floats_from_bytes4(raw: bytes) -> float:
    import struct
    return struct.unpack("<f", raw)[0]


# ---------------------------------------------------------------------------
# Convenience: read the friendly fields a keyboard overlay needs.
# ---------------------------------------------------------------------------
def overlay_signals(state: dict) -> dict:
    """Project a decoded usercmd state onto the fields overlay_pov.py uses."""
    base = state.get("base", {})
    buttons = base.get("buttons", {})
    va = base.get("viewangles", {})
    return {
        "buttonstate1": buttons.get("buttonstate1", 0),
        "buttonstate2": buttons.get("buttonstate2", 0),
        "buttonstate3": buttons.get("buttonstate3", 0),
        "forwardmove": base.get("forwardmove", 0.0),
        "leftmove": base.get("leftmove", 0.0),
        "upmove": base.get("upmove", 0.0),
        "mousedx": base.get("mousedx", 0),
        "mousedy": base.get("mousedy", 0),
        "viewangle_x": va.get("x", 0.0),
        "viewangle_y": va.get("y", 0.0),
        "viewangle_z": va.get("z", 0.0),
    }
