from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from pathlib import Path

priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub = priv.public_key()

base = Path(".")
(base / "local_analytics_private.pem").write_text(
    priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
)
(base / "local_analytics_public.pem").write_text(
    pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
)