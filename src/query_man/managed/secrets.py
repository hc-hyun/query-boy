from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretKeyConfigurationError(ValueError):
    pass


class SecretDecryptionError(ValueError):
    pass


@dataclass(frozen=True)
class EncryptedSecret:
    nonce: bytes
    ciphertext: bytes


class SourceSecretCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise SecretKeyConfigurationError("The source encryption key must contain 32 bytes")
        self._cipher = AESGCM(key)
        self._mutation_hash_key = hmac.new(
            key,
            b"query-man/mutation-request-hash-key/v1",
            hashlib.sha256,
        ).digest()

    @classmethod
    def from_base64(cls, encoded: str) -> SourceSecretCipher:
        try:
            key = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise SecretKeyConfigurationError("The source encryption key is invalid") from error
        return cls(key)

    def encrypt(self, source_id: str, generation: int, secret: str) -> EncryptedSecret:
        if not secret:
            raise ValueError("The source credential must not be empty")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            secret.encode("utf-8"),
            _associated_data(source_id, generation),
        )
        return EncryptedSecret(nonce, ciphertext)

    def decrypt(
        self,
        source_id: str,
        generation: int,
        encrypted: EncryptedSecret,
    ) -> str:
        try:
            plaintext = self._cipher.decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                _associated_data(source_id, generation),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, ValueError, UnicodeDecodeError) as error:
            raise SecretDecryptionError("The source credential could not be decrypted") from error

    def mutation_request_hash(self, canonical_request: bytes) -> str:
        if not canonical_request:
            raise ValueError("The canonical mutation request must not be empty")
        digest = hmac.new(
            self._mutation_hash_key,
            b"query-man/mutation-request/v1\x00" + canonical_request,
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"


def _associated_data(source_id: str, generation: int) -> bytes:
    return f"query-man/source/{source_id}/generation/{generation}".encode("ascii")
