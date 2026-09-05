#!/usr/bin/python
# -*- coding: utf-8 -*-
import base64
import random
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from infrastructure.vpn.config import DEFAULT_AWG_CONFIG


@dataclass
class AWGKeys:
    private_key: str
    public_key: str
    psk: str


def generate_secret_keys() -> AWGKeys:
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    private_key_str = base64.b64encode(private_bytes).decode()
    public_key_str = base64.b64encode(public_bytes).decode()

    psk_bytes = secrets.token_bytes(32)  # 32 случайных байта
    psk_str = base64.b64encode(psk_bytes).decode()

    return AWGKeys(private_key=private_key_str, public_key=public_key_str, psk=psk_str)


def pick_udp_port() -> int:
    return random.randint(20000, 60000)

def render_awg_config(private_key_str: str, udp_port: int) -> str:
    return DEFAULT_AWG_CONFIG.format(private_key_str, udp_port)
