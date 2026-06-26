"""飞书事件「encrypt」字段解密（AES-256-CBC），与开放平台加密策略一致；及 Webhook 签名校验。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any


def compute_lark_request_signature(
    raw_body: bytes,
    timestamp: str,
    nonce: str,
    encrypt_key: str,
) -> str:
    """
    计算 X-Lark-Signature（飞书开放平台文档「签名校验」Python 示例）。

    bytes_b1 = (timestamp + nonce + encrypt_key).encode('utf-8')
    bytes_b = bytes_b1 + raw_body  # raw_body 须为 HTTP 原始 body，勿用反序列化后再序列化的结果
    signature = sha256(bytes_b).hexdigest()
    """
    b1 = (timestamp + nonce + encrypt_key).encode("utf-8")
    b = b1 + raw_body
    return hashlib.sha256(b).hexdigest()


def verify_lark_request_signature(
    raw_body: bytes,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    encrypt_key: str,
) -> bool:
    """校验收到的请求是否来自飞书（单笔 encrypt_key）。"""
    if not encrypt_key or not signature or not timestamp or not nonce:
        return False
    expected = compute_lark_request_signature(raw_body, timestamp, nonce, encrypt_key)
    return bool(signature) and signature == expected


def verify_lark_request_signature_try_keys(
    raw_body: bytes,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    encrypt_keys: list[str],
) -> bool:
    """多机器人：任一只读成功的 encrypt_key 通过即认为验签通过。"""
    if not signature or not timestamp or not nonce:
        return False
    for key in encrypt_keys:
        if not key:
            continue
        if verify_lark_request_signature(raw_body, timestamp, nonce, signature, key):
            return True
    return False


def decrypt_feishu_encrypt_field(encrypt_key: str, encrypt_b64: str) -> dict[str, Any]:
    """解密 body['encrypt']，返回事件 JSON 对象。"""
    from Crypto.Cipher import AES  # pycryptodome

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    raw = base64.b64decode(encrypt_b64)
    iv = raw[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(raw[16:])
    pad_len = decrypted[-1]
    plaintext = decrypted[:-pad_len].decode("utf-8")
    return json.loads(plaintext)


def maybe_decrypt_event_body(body: dict[str, Any], encrypt_key: str) -> dict[str, Any]:
    """若存在 encrypt 且配置了 encrypt_key，则解密并返回内层事件结构。"""
    if not encrypt_key or not isinstance(body, dict):
        return body
    enc = body.get("encrypt")
    if not enc or not isinstance(enc, str):
        return body
    return decrypt_feishu_encrypt_field(encrypt_key, enc)


def decrypt_feishu_body_try_adapters(
    body: dict[str, Any],
    feishu_decrypt_enabled: bool,
    adapters: list[Any],
) -> tuple[dict[str, Any], Any | None]:
    """
    多机器人场景：密文仅能通过 encrypt_key  trial 解密；成功后返回明文与命中的适配器对象（携带 app_id）。
    若未加密或未启用解密，返回 (body, None)。
    """
    if not feishu_decrypt_enabled or not isinstance(body, dict) or "encrypt" not in body:
        return body, None
    enc = body.get("encrypt")
    if not enc or not isinstance(enc, str):
        return body, None
    for ad in adapters:
        key = getattr(ad, "encrypt_key", None) or ""
        if not key:
            continue
        try:
            inner = decrypt_feishu_encrypt_field(key, enc)
            return inner, ad
        except Exception:
            continue
    return body, None
