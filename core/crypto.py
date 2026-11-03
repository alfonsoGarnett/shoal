"""Ed25519 (RFC 8032): code signing, each hub's identity and capabilities between hubs.

The `cryptography` package does the work when it is installed (well under a millisecond per
operation). Without it, a pure-Python reference implementation (D. J. Bernstein / RFC 8032) takes
over: same keys, same signatures, but 0.2-0.4 s per verification on a laptop CPU and more on a
phone. A coordinator verifies every signed request from the other hubs, so the native path is what
keeps an idle hub near zero CPU. Verifications are also cached: the same (key, signature, message)
is checked again on every gossip round.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

try:  # optional native backend
    from cryptography.exceptions import InvalidSignature as _InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _PrivateKey,
        Ed25519PublicKey as _PublicKey,
    )
    BACKEND = "cryptography"
except Exception:  # ImportError, or a broken/partial install
    _PrivateKey = _PublicKey = _InvalidSignature = None
    BACKEND = "pure-python"

_CACHE_MAX = 4096
_cache: "OrderedDict[bytes, bool]" = OrderedDict()
_cache_lock = threading.Lock()

Q = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493


def _sha512(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _inv(x: int) -> int:
    return pow(x, Q - 2, Q)


_D = -121665 * _inv(121666) % Q
_I = pow(2, (Q - 1) // 4, Q)


def _bit(h: bytes, i: int) -> int:
    return (h[i >> 3] >> (i & 7)) & 1


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q != 0:
        x = (x * _I) % Q
    if x % 2 != 0:
        x = Q - x
    return x


_By = 4 * _inv(5) % Q
_Bx = _xrecover(_By)
_B = (_Bx % Q, _By % Q)


def _edwards(p, q):
    x1, y1 = p
    x2, y2 = q
    k = _D * x1 * x2 * y1 * y2 % Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + k) % Q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - k) % Q
    return (x3, y3)


def _scalarmult(p, e: int):
    if e == 0:
        return (0, 1)
    q = _scalarmult(p, e >> 1)
    q = _edwards(q, q)
    if e & 1:
        q = _edwards(q, p)
    return q


def _encodepoint(p) -> bytes:
    x, y = p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decodepoint(s: bytes):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (s[31] >> 7):
        x = Q - x
    p = (x, y)
    if (-x * x + y * y - 1 - _D * x * x * y * y) % Q != 0:
        raise ValueError("point not on curve")
    return p


def publickey(seed: bytes) -> bytes:
    if _PrivateKey is not None:
        from cryptography.hazmat.primitives import serialization
        return _PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _publickey_py(seed)


def _publickey_py(seed: bytes) -> bytes:
    h = _sha512(seed)
    a = 2**254 + sum(2**i * _bit(h, i) for i in range(3, 254))
    return _encodepoint(_scalarmult(_B, a))


def sign(msg: bytes, seed: bytes, pub: bytes | None = None) -> bytes:
    if _PrivateKey is not None:
        return _PrivateKey.from_private_bytes(seed).sign(msg)
    return _sign_py(msg, seed, pub)


def _sign_py(msg: bytes, seed: bytes, pub: bytes | None = None) -> bytes:
    h = _sha512(seed)
    a = 2**254 + sum(2**i * _bit(h, i) for i in range(3, 254))
    r = int.from_bytes(_sha512(h[32:64] + msg), "little") % L
    rp = _encodepoint(_scalarmult(_B, r))
    pk = pub or _publickey_py(seed)
    s = (r + int.from_bytes(_sha512(rp + pk + msg), "little") * a) % L
    return rp + s.to_bytes(32, "little")


def verify(sig: bytes, msg: bytes, pub: bytes) -> bool:
    if len(sig) != 64 or len(pub) != 32:
        return False
    key = hashlib.sha256(pub + sig + msg).digest()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    ok = _verify_native(sig, msg, pub) if _PublicKey is not None else _verify_py(sig, msg, pub)
    with _cache_lock:
        _cache[key] = ok
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return ok


def _verify_native(sig: bytes, msg: bytes, pub: bytes) -> bool:
    try:
        _PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except _InvalidSignature:
        return False
    except Exception:
        return False


def _verify_py(sig: bytes, msg: bytes, pub: bytes) -> bool:
    try:
        s = int.from_bytes(sig[32:], "little")
        if s >= L:
            return False
        r = _decodepoint(sig[:32])
        a = _decodepoint(pub)
        k = int.from_bytes(_sha512(sig[:32] + pub + msg), "little") % L
        return _scalarmult(_B, s) == _edwards(r, _scalarmult(a, k))
    except Exception:
        return False
