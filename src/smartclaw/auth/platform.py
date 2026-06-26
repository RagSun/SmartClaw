"""
PlatformAuthAdapter — 统一监控 / Webhook / JWT / 飞书解密 / 防重放。
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from smartclaw.config.loader import Config


def _bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    """从 ``Authorization: Bearer <token>`` 提取 token；格式不符返回 None。"""
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    return authorization_header[7:].strip()


class PlatformAuthAdapter:
    """集中解析 Bearer、JWT、Webhook 共享密钥、飞书 encrypt 解密与事件防重放。"""

    @staticmethod
    def verify_monitoring_bearer(authorization_header: Optional[str], cfg: Config) -> bool:
        if not cfg.auth.monitoring_require_auth:
            return True
        if cfg.auth.monitoring_jwt_enabled:
            return PlatformAuthAdapter.verify_monitoring_jwt(authorization_header, cfg)
        token = (cfg.auth.monitoring_bearer_token or "").strip()
        if not token:
            return False
        provided = _bearer_token(authorization_header)
        if provided is None:
            return False
        # 恒定时间比较，避免基于响应时间差的 token 侧信道。
        return hmac.compare_digest(provided, token)

    @staticmethod
    def verify_admin_bearer(authorization_header: Optional[str], cfg: Config) -> bool:
        """管理面 /api/admin/* 鉴权（默认安全 + 与监控凭证隔离）。

        判定顺序（修复"默认不安全"硬伤）：

        1. ``admin_require_auth=False`` → 运维显式放行（仅限可信内网/网关场景）。
        2. 配置了 ``admin_bearer_token`` → **要求**精确匹配该专用凭证（恒定时间比较）。
        3. 未配置专用凭证，但监控鉴权已强制开启 → 回退复用监控凭证（向后兼容既有部署）。
        4. 以上都不满足（默认配置：无专用凭证且监控鉴权关闭）→ **拒绝**。
           即开箱即用状态下管理面是关闭的，直到运维显式配置凭证——secure by default。
        """
        auth = cfg.auth
        if not getattr(auth, "admin_require_auth", True):
            return True
        admin_token = (getattr(auth, "admin_bearer_token", "") or "").strip()
        if admin_token:
            provided = _bearer_token(authorization_header)
            if provided is None:
                return False
            return hmac.compare_digest(provided, admin_token)
        if getattr(auth, "monitoring_require_auth", False):
            return PlatformAuthAdapter.verify_monitoring_bearer(authorization_header, cfg)
        return False

    @staticmethod
    def verify_monitoring_jwt(authorization_header: Optional[str], cfg: Config) -> bool:
        if not cfg.auth.monitoring_require_auth or not cfg.auth.monitoring_jwt_enabled:
            return True
        secret = (cfg.auth.monitoring_jwt_secret or "").strip()
        if not authorization_header or not authorization_header.startswith("Bearer "):
            return False
        raw = authorization_header[7:].strip()
        if not secret:
            return False
        try:
            import jwt

            decode_kw: dict[str, Any] = {
                "algorithms": [cfg.auth.monitoring_jwt_algorithm or "HS256"],
            }
            aud = (cfg.auth.monitoring_jwt_audience or "").strip()
            if aud:
                decode_kw["audience"] = aud
            iss = (cfg.auth.monitoring_jwt_issuer or "").strip()
            if iss:
                decode_kw["issuer"] = iss
            jwt.decode(raw, secret, **decode_kw)
            return True
        except Exception:
            return False

    @staticmethod
    def verify_lark_webhook_signature_if_present(
        raw_body: bytes,
        request_headers: Any,
        encrypt_keys: list[str],
    ) -> tuple[bool, str]:
        """
        若请求携带 X-Lark-Signature 等头，则按开放平台文档校验；否则跳过。

        返回 (ok, reason)。ok=False 时应返回 401。
        """
        if not encrypt_keys:
            encrypt_keys = []
        sig = None
        ts = None
        nonce = None
        if hasattr(request_headers, "get"):
            sig = request_headers.get("X-Lark-Signature") or request_headers.get("x-lark-signature")
            ts = request_headers.get("X-Lark-Request-Timestamp") or request_headers.get(
                "x-lark-request-timestamp"
            )
            nonce = request_headers.get("X-Lark-Request-Nonce") or request_headers.get(
                "x-lark-request-nonce"
            )
        if not sig:
            return True, ""
        if not ts or not nonce:
            return False, "缺少 X-Lark-Request-Timestamp / X-Lark-Request-Nonce"
        usable = [k for k in encrypt_keys if k and str(k).strip()]
        if not usable:
            return False, "已带 X-Lark-Signature 但未配置任何飞书 encrypt_key，无法验签"
        from smartclaw.auth.feishu_payload import verify_lark_request_signature_try_keys

        if verify_lark_request_signature_try_keys(raw_body, ts, nonce, sig, usable):
            return True, ""
        return False, "X-Lark-Signature 与 encrypt_key 不匹配"

    @staticmethod
    def verify_feishu_webhook(
        query_token: Optional[str],
        header_token: Optional[str],
        cfg: Config,
    ) -> bool:
        secret = (cfg.auth.feishu_webhook_secret or "").strip()
        if not secret:
            return True
        provided = (query_token or header_token or "").strip()
        return bool(provided) and provided == secret

    @staticmethod
    def maybe_decrypt_feishu_body(body: dict[str, Any], encrypt_key: str, cfg: Config) -> dict[str, Any]:
        if not cfg.auth.feishu_decrypt_webhook or not encrypt_key:
            return body
        if not isinstance(body, dict) or "encrypt" not in body:
            return body
        try:
            from smartclaw.auth.feishu_payload import maybe_decrypt_event_body

            return maybe_decrypt_event_body(body, encrypt_key)
        except Exception:
            return body

    @staticmethod
    def webhook_replay_key(body: dict[str, Any]) -> str:
        header = body.get("header") or {}
        eid = header.get("event_id") or body.get("event_id") or ""
        return str(eid) if eid else ""

    @staticmethod
    def check_webhook_not_replay(body: dict[str, Any], cfg: Config) -> bool:
        ttl = int(cfg.auth.webhook_replay_ttl_seconds or 0)
        if ttl <= 0:
            return True
        key = PlatformAuthAdapter.webhook_replay_key(body)
        if not key:
            return True
        from smartclaw.auth.webhook_replay import get_replay_guard

        # 防重放后端跟随治理状态后端：governance.store=redis 时共用同一 Redis，
        # 使去重在多 worker / 多副本间共享（否则同一事件会被每个进程各放行一次）。
        gov = getattr(cfg, "governance", None)
        redis_url = ""
        if gov is not None and getattr(gov, "store", "memory") == "redis":
            redis_url = getattr(gov, "redis_url", "") or ""
        g = get_replay_guard(ttl, redis_url)
        return not g.is_replay(key)
