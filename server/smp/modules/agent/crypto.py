"""Device identity and request signing for agents.

Each agent creates an ECDSA P-256 key pair when it's installed (P-256 because TPMs
support it, so the key can later live in the TPM without a protocol change). The
public key, as an uncompressed SEC1 point (65 bytes), is the device's identity; its
SHA-256 (hex) is the fingerprint, stored as Device.agent_id. IP addresses play no part.

Every agent request carries three headers:

    X-SMP-Key:        fingerprint (64 hex chars)
    X-SMP-Time:       unix seconds when the request was made
    X-SMP-Signature:  base64url(r || s), 64 bytes, over the message below

    message = "SMP-AGENT-1\\n" + METHOD + "\\n" + path_and_query + "\\n" + time + "\\n"
              + hex(sha256(body))

The server accepts a request if the signature verifies against the stored public key,
the time is within MAX_SKEW of the server's clock (agents correct their clock offset
from `server_time` in every response), and the same signature wasn't seen before
(replay protection, see hub.ReplayGuard). There is no bearer token to steal.
"""

import base64
import binascii
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

PROTOCOL = b"SMP-AGENT-1"
MAX_SKEW_SECONDS = 300
POINT_LEN = 65  # uncompressed P-256 point: 0x04 || X (32) || Y (32)


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def parse_public_key(encoded: str) -> tuple[ec.EllipticCurvePublicKey, bytes] | None:
    """(key, raw point) from base64url of an uncompressed P-256 point, or None."""
    try:
        raw = b64url_decode(encoded)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != POINT_LEN or raw[0] != 0x04:
        return None
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw), raw
    except ValueError:  # not a point on the curve
        return None


def fingerprint(raw_point: bytes) -> str:
    return hashlib.sha256(raw_point).hexdigest()


def display_fingerprint(fp: str) -> str:
    """The first 80 bits in groups of 4, for people to compare: "3F2A-91C4-..."."""
    head = fp[:20].upper()
    return "-".join(head[i : i + 4] for i in range(0, len(head), 4))


def signed_message(method: str, path_and_query: str, timestamp: str, body: bytes) -> bytes:
    return b"\n".join(
        [PROTOCOL, method.upper().encode(), path_and_query.encode(), timestamp.encode(), hashlib.sha256(body).hexdigest().encode()]
    )


def verify(public_key: ec.EllipticCurvePublicKey, signature_b64: str, message: bytes) -> bool:
    try:
        sig = b64url_decode(signature_b64)
    except (binascii.Error, ValueError):
        return False
    if len(sig) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        public_key.verify(der, message, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
