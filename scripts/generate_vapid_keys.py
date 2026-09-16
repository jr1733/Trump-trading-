#!/usr/bin/env python3
"""Generate a VAPID keypair for Web Push.

    python scripts/generate_vapid_keys.py

Prints the two values to put in your .env. **Only the public key is ever sent
to the browser**; the private key signs the push requests and must stay on the
server.

Changing the keypair invalidates every existing push subscription, so after
rotating it each device has to re-enable notifications once.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64(data: bytes) -> str:
    """URL-safe base64 without padding, which is what the Push API expects."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private_key.public_key().public_numbers()

    # Uncompressed EC point: 0x04 || X || Y, 65 bytes. This is the applicationServerKey.
    raw_public = (
        b"\x04"
        + public_numbers.x.to_bytes(32, "big")
        + public_numbers.y.to_bytes(32, "big")
    )
    raw_private = private_key.private_numbers().private_value.to_bytes(32, "big")

    print("# Add these to your .env (public key is safe to expose):")
    print(f"WEB_PUSH_PUBLIC_KEY={b64(raw_public)}")
    print(f"WEB_PUSH_PRIVATE_KEY={b64(raw_private)}")
    print("WEB_PUSH_SUBJECT=mailto:you@example.com")
    print()
    print("# PEM form of the private key, if a tool asks for it instead:")
    print(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    )


if __name__ == "__main__":
    main()
