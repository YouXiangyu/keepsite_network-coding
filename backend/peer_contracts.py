"""Strict, bounded v1 control messages for optional same-account peer transfer."""
import json
import re

MAX_FRAME_BYTES = 65536
MAX_CONTROL_BYTES = 4096
MAX_SDP_BYTES = 49152
MAX_ICE_BYTES = 2048
LIMITS = {"maxInventory": 1024, "maxBatchAssets": 32, "maxControlBytes": MAX_CONTROL_BYTES,
          "maxSignalBytes": MAX_FRAME_BYTES, "maxSdpBytes": MAX_SDP_BYTES,
          "maxIceBytes": MAX_ICE_BYTES, "maxActiveTransfers": 2}
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
ASSET = re.compile(r"[a-f0-9]{64}\Z")


class PeerProtocolError(Exception):
    def __init__(self, close_code=1008):
        self.close_code = close_code


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate key")
            value[key] = item
        return value

    def constant(_value):
        raise ValueError("Non-finite number")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=constant)
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


def token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def asset(value):
    return isinstance(value, str) and ASSET.fullmatch(value) is not None


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def asset_list(value):
    return isinstance(value, list) and len(value) <= 32 and all(asset(item) for item in value) and len(set(value)) == len(value)


FIELDS = {
    "device.register": {"name"}, "device.heartbeat": set(), "device.list": set(), "device.unregister": set(),
    "cache.replace": {"revision", "batchIndex", "batchCount", "assetIds"},
    "cache.patch": {"revision", "add", "remove"}, "peer.find": {"assetId"},
    "peer.open": {"assetId", "targetPeerId"}, "peer.accept": {"transferId"},
    "peer.signal": {"transferId", "kind", "payload"}, "peer.renew": {"transferId"},
    "peer.cancel": {"transferId", "reason"},
}


def parse_message(raw):
    if not isinstance(raw, str):
        raise PeerProtocolError()
    size = len(raw.encode("utf-8"))
    if size > MAX_FRAME_BYTES:
        raise PeerProtocolError(1009)
    try:
        body = strict_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise PeerProtocolError(1009 if size > MAX_CONTROL_BYTES else 1008) from None
    if not isinstance(body, dict):
        raise PeerProtocolError()
    kind = body.get("type")
    if kind != "peer.signal" and size > MAX_CONTROL_BYTES:
        raise PeerProtocolError(1009)
    if (not isinstance(kind, str) or kind not in FIELDS or type(body.get("v")) is not int or body["v"] != 1
            or not token(body.get("requestId")) or set(body) != {"v", "type", "requestId"} | FIELDS[kind]):
        raise PeerProtocolError()
    valid = True
    if kind == "device.register":
        valid = isinstance(body["name"], str) and len(body["name"]) <= 32 and not any(ord(char) < 32 for char in body["name"])
    elif kind.startswith("cache."):
        valid = integer(body["revision"], 1, 2**53 - 1)
        if kind == "cache.replace":
            valid = valid and integer(body["batchCount"], 1, 32) and integer(body["batchIndex"], 0, body["batchCount"] - 1) and asset_list(body["assetIds"])
        else:
            valid = valid and asset_list(body["add"]) and asset_list(body["remove"]) and len(body["add"]) + len(body["remove"]) <= 32 and not set(body["add"]) & set(body["remove"])
    elif kind in {"peer.find", "peer.open"}:
        valid = asset(body["assetId"]) and (kind != "peer.open" or token(body["targetPeerId"]))
    elif kind.startswith("peer."):
        valid = token(body["transferId"])
        if kind == "peer.cancel":
            valid = valid and token(body["reason"])
        elif kind == "peer.signal":
            signal, payload = body["kind"], body["payload"]
            if isinstance(signal, str) and signal in {"offer", "answer"}:
                valid = valid and isinstance(payload, dict) and set(payload) == {"type", "sdp"} and payload["type"] == signal and isinstance(payload["sdp"], str) and 1 <= len(payload["sdp"].encode()) <= MAX_SDP_BYTES
            elif signal == "ice":
                if payload is not None:
                    valid = valid and isinstance(payload, dict) and {"candidate", "sdpMid", "sdpMLineIndex"} <= set(payload) <= {"candidate", "sdpMid", "sdpMLineIndex", "usernameFragment"}
                    if valid:
                        valid = (isinstance(payload["candidate"], str)
                                 and (payload["sdpMid"] is None or isinstance(payload["sdpMid"], str) and len(payload["sdpMid"]) <= 256)
                                 and (payload["sdpMLineIndex"] is None or integer(payload["sdpMLineIndex"], 0, 65535))
                                 and (payload.get("usernameFragment") is None or isinstance(payload["usernameFragment"], str) and len(payload["usernameFragment"]) <= 256)
                                 and len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()) <= MAX_ICE_BYTES)
            else:
                valid = False
    if not valid:
        raise PeerProtocolError()
    return body
