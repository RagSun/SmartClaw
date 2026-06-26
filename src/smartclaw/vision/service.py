"""
全局视觉理解服务
支持多进程并发调用，线程安全
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx

from smartclaw.console import info, warning, error

_DEFAULT_VISION_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"


@dataclass
class VisionConfig:
    """视觉服务配置"""
    enabled: bool = False
    model: str = "glm-4v"
    api_key: str = ""
    base_url: str = _DEFAULT_VISION_BASE_URL
    timeout: int = 60
    max_retries: int = 3


class VisionService:
    """
    全局视觉理解服务（单例模式）
    线程安全，支持多进程并发调用
    """
    _instance: Optional["VisionService"] = None
    _lock = threading.Lock()
    
    def __init__(self):
        self._config: Optional[VisionConfig] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
    
    @classmethod
    def get_instance(cls) -> "VisionService":
        """获取单例实例（线程安全）"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def configure(self, config: VisionConfig, *, verbose: bool = True) -> None:
        """配置视觉服务"""
        self._config = config
        if config.enabled:
            self._semaphore = asyncio.Semaphore(5)
            if verbose:
                info(f"[Vision] 已启用全局视觉理解，模型: {config.model}")
        else:
            if verbose:
                info("[Vision] 全局视觉理解已禁用")
    
    def is_enabled(self) -> bool:
        """检查是否启用"""
        return self._config is not None and self._config.enabled
    
    async def understand_image(
        self,
        image_data: str,
        prompt: str = "请描述这张图片的内容",
        agent_id: str = "unknown"
    ) -> str:
        """
        理解图片内容（异步、并发安全）
        """
        if not self.is_enabled():
            return "[视觉理解已禁用]"
        
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(5)
        
        async with self._semaphore:
            return await self._do_understand(image_data, prompt, agent_id)
    
    async def _do_understand(
        self,
        image_data: str,
        prompt: str,
        agent_id: str
    ) -> str:
        """执行图片理解"""
        retries = 0
        last_error = None
        
        while retries < (self._config.max_retries or 3):
            try:
                return await self._call_vision_api(image_data, prompt, agent_id)
            except Exception as e:
                last_error = e
                retries += 1
                warning(f"[Vision] {agent_id} 调用失败 ({retries}/{self._config.max_retries}): {e}")
                if retries < (self._config.max_retries or 3):
                    await asyncio.sleep(1 * retries)
        
        error(f"[Vision] {agent_id} 最终失败: {last_error}")
        return f"[图片理解失败: {last_error}]"
    
    async def _call_vision_api(
        self,
        image_data: str,
        prompt: str,
        agent_id: str
    ) -> str:
        """调用视觉 API"""
        # 判断是 URL 还是 base64
        if image_data.startswith("http://") or image_data.startswith("https://"):
            image_url = image_data
            image_base64 = None
        else:
            image_url = None
            image_base64 = image_data
        
        # 构建 messages
        content = [{"type": "text", "text": prompt}]
        
        if image_url:
            content.append({"type": "image_url", "image_url": {"url": image_url}})
        elif image_base64:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}})
        
        messages = [{"role": "user", "content": content}]
        
        payload = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": 1024,
        }
        
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        
        async with httpx.AsyncClient(timeout=self._config.timeout) as client:
            response = await client.post(
                f"{self._config.base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
    
    async def understand_images_batch(
        self,
        images: list[str],
        prompt: str = "请描述这些图片的内容",
        agent_id: str = "unknown"
    ) -> str:
        """批量理解多张图片"""
        if not self.is_enabled():
            return "[视觉理解已禁用]"
        
        results = []
        for i, img in enumerate(images):
            try:
                desc = await self.understand_image(img, f"图片 {i+1}: {prompt}", agent_id)
                results.append(f"图片 {i+1}: {desc}")
            except Exception as e:
                results.append(f"图片 {i+1}: [理解失败: {e}]")
        
        return "\n".join(results)


def get_vision_service() -> VisionService:
    """获取全局视觉服务实例"""
    return VisionService.get_instance()


def configure_vision(config: VisionConfig, *, verbose: bool = True) -> None:
    """配置全局视觉服务"""
    VisionService.get_instance().configure(config, verbose=verbose)


def configure_vision_for_merged_llm(
    merged_llm: Optional[Mapping[str, Any]],
    global_vision: Any,
    *,
    log_tag: str = "",
    verbose: bool = True,
) -> None:
    """
    合并 config.toml [vision] 与 agent.json 的 llm.vision（llm 已与全局 [llm] 合并），并写入视觉单例。

    与 feishu_multiprocess 原有逻辑一致：Agent 非空字段覆盖全局对应项。
    """
    if global_vision is not None:
        enabled = bool(getattr(global_vision, "enabled", False))
        model = getattr(global_vision, "model", None) or "glm-4v"
        api_key = str(getattr(global_vision, "api_key", "") or "")
        bu = str(getattr(global_vision, "base_url", "") or "").strip()
        base_url = bu or _DEFAULT_VISION_BASE_URL
        try:
            timeout = int(getattr(global_vision, "timeout", 60))
        except (TypeError, ValueError):
            timeout = 60
        try:
            max_retries = int(getattr(global_vision, "max_retries", 3))
        except (TypeError, ValueError):
            max_retries = 3
    else:
        enabled = False
        model = "glm-4v"
        api_key = ""
        base_url = _DEFAULT_VISION_BASE_URL
        timeout, max_retries = 60, 3

    agent_vision: dict[str, Any] = {}
    if merged_llm and isinstance(merged_llm.get("vision"), dict):
        agent_vision = dict(merged_llm["vision"] or {})

    if agent_vision:
        enabled = agent_vision.get("enabled", enabled)
        model = agent_vision.get("model", model)
        api_key = agent_vision.get("api_key", api_key)
        base_url = agent_vision.get("base_url", base_url) or _DEFAULT_VISION_BASE_URL
        try:
            timeout = int(agent_vision.get("timeout", timeout))
        except (TypeError, ValueError):
            pass
        try:
            max_retries = int(agent_vision.get("max_retries", max_retries))
        except (TypeError, ValueError):
            pass

    if enabled and api_key:
        get_vision_service().configure(
            VisionConfig(
                enabled=True,
                model=str(model or "glm-4v"),
                api_key=str(api_key),
                base_url=str(base_url or _DEFAULT_VISION_BASE_URL),
                timeout=timeout,
                max_retries=max_retries,
            ),
            verbose=verbose,
        )
    else:
        get_vision_service().configure(VisionConfig(enabled=False), verbose=verbose)
        if enabled and not api_key and verbose:
            prefix = f"[{log_tag}] " if log_tag else ""
            warning(
                f"[Vision] {prefix}已在配置中启用视觉但未提供 API Key，已关闭视觉服务"
            )
