"""
LLM 适配器基类

定义所有 LLM 适配器必须实现的通用接口。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional


class LLMProvider(str, Enum):
    """LLM 提供商枚举"""

    # 国内
    GLM = "glm"  # 智谱
    DEEPSEEK = "deepseek"  # DeepSeek
    QWEN = "qwen"  # 通义千问

    # 国外
    OPENAI = "openai"  # OpenAI
    CLAUDE = "claude"  # Claude
    GEMINI = "gemini"  # Gemini

    # 自定义
    VLLM = "vllm"  # vLLM 部署
    OLLAMA = "ollama"  # Ollama
    CUSTOM = "custom"  # 自定义


@dataclass
class LLMConfig:
    """LLM 配置"""

    provider: LLMProvider = LLMProvider.OPENAI
    model_name: str = "gpt-4"
    api_key: Optional[str] = None
    base_url: Optional[str] = None  # 自定义 API 地址

    # 生成参数
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 1.0
    stream: bool = False

    # 工具调用
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[str] = "auto"

    # 额外参数
    extra_params: dict[str, Any] = field(default_factory=dict[str, Any])


def _llm_field_is_explicitly_set(value: Any) -> bool:
    """判断 agent.json llm 中某字段是否视为「用户已配置」（用于与全局 llm 合并）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, dict):
        return bool(value)
    return True


def merge_agent_llm_with_global(
    agent_llm: Optional[dict[str, Any]],
    global_llm: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """
    合并全局与 Agent 的 llm 配置：Agent 中非空字段优先，否则回退全局。

    global_llm 为与 agent 相同键风格的 dict（通常来自 config.toml [llm]）。
    """
    base = normalize_agent_llm_dict(dict(global_llm or {}))
    over = normalize_agent_llm_dict(dict(agent_llm or {}))
    out: dict[str, Any] = dict(base)
    for key, val in over.items():
        if key == "vision":
            if isinstance(val, dict) and val:
                out["vision"] = val
            continue
        if not _llm_field_is_explicitly_set(val):
            continue
        out[key] = val
    return normalize_agent_llm_dict(out)


def normalize_agent_llm_dict(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    """
    统一 agent.json 中的 llm 块：始终写入标准键 model_name。

    CLI「--model」与 OpenAI 请求字段均称「model」，持久化使用 model_name；
    兼容历史/手工配置仅含「model」键的情况。
    """
    if not raw:
        return {}
    out: dict[str, Any] = dict(raw)
    name = out.get("model_name") or out.get("model")
    if name is not None and str(name).strip():
        out["model_name"] = str(name).strip()
    return out


def resolved_model_name_from_llm_dict(
    blob: dict[str, Any],
    default: str = "gpt-4",
) -> str:
    """从原始或已规范的 llm dict 解析模型 id（供 AgentConfig 等使用）。"""
    if not blob:
        return default
    v = blob.get("model_name") or blob.get("model")
    if v is None or not str(v).strip():
        return default
    return str(v).strip()


@dataclass
class ToolCall:
    """工具调用"""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


@dataclass
class LLMResponse:
    """LLM 响应"""

    content: str
    model: str
    provider: LLMProvider

    # 使用统计
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    # 工具调用
    tool_calls: list[ToolCall] = field(default_factory=list)

    # 原始响应
    raw_response: Optional[dict[str, Any]] = None

    # 元数据
    finish_reason: str = "stop"
    latency_ms: int = 0


@dataclass
class Message:
    """消息"""

    role: str  # system / user / assistant / tool
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # 工具名称（role=tool 时）

    def to_openai_format(self) -> dict[str, Any]:
        """转换为 OpenAI 格式"""
        msg: dict[str, Any] = {"role": self.role}

        if self.content is not None:
            msg["content"] = self.content

        if self.tool_calls:
            msg["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]

        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id

        if self.name:
            msg["name"] = self.name

        return msg

    @classmethod
    def system(cls, content: str) -> "Message":
        """创建系统消息"""
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        """创建用户消息"""
        return cls(role="user", content=content)

    @classmethod
    def assistant(
        cls, content: str, tool_calls: Optional[list[ToolCall]] = None
    ) -> "Message":
        """创建助手消息"""
        return cls(role="assistant", content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> "Message":
        """创建工具结果消息"""
        return cls(role="tool", tool_call_id=tool_call_id, name=name, content=content)


class LLMAdapter(ABC):
    """
    LLM 适配器抽象基类

    所有 LLM 适配器必须实现此接口。
    """

    def __init__(self, config: LLMConfig):
        """
        初始化适配器

        参数:
            config: LLM 配置
        """
        self.config = config

    @property
    @abstractmethod
    def provider(self) -> LLMProvider:
        """提供商类型"""
        pass

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """检查是否可用（API Key 是否配置）"""
        pass

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        """
        发送对话请求

        参数:
            messages: 消息列表
            **kwargs: 额外参数

        返回:
            LLM 响应
        """
        pass

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """
        流式对话

        参数:
            messages: 消息列表
            **kwargs: 额外参数

        返回:
            异步迭代器，yield 文本片段
        """
        pass

    async def count_tokens(self, text: str) -> int:
        """
        计算 Token 数量（简单估算）

        参数:
            text: 文本内容

        返回:
            Token 数量
        """
        return len(text) // 4

    def get_model_info(self) -> dict[str, Any]:
        """
        获取模型信息

        返回:
            模型信息字典
        """
        return {
            "provider": self.provider.value,
            "model": self.config.model_name,
            "available": self.is_available,
        }
