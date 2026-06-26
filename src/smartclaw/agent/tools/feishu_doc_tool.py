"""
飞书文档创建工具

允许 Agent 创建飞书在线文档、表格、多维表格。
"""
import json
from typing import Any

import lark_oapi as lark
from lark_oapi.api.docx.v1.model.convert_document_request import ConvertDocumentRequest
from lark_oapi.api.docx.v1.model.convert_document_request_body import ConvertDocumentRequestBody
from lark_oapi.api.docx.v1.model.create_document_block_descendant_request import (
    CreateDocumentBlockDescendantRequest,
)
from lark_oapi.api.docx.v1.model.create_document_block_descendant_request_body import (
    CreateDocumentBlockDescendantRequestBody,
)
from lark_oapi.api.docx.v1.model.create_document_request import CreateDocumentRequest
from lark_oapi.api.docx.v1.model.create_document_request_body import CreateDocumentRequestBody
from lark_oapi.api.drive.v1.model.create_permission_member_request import (
    CreatePermissionMemberRequest,
)
from lark_oapi.api.drive.v1.model.member import Member
from lark_oapi.api.sheets.v3.model.create_spreadsheet_request import CreateSpreadsheetRequest
from lark_oapi.api.sheets.v3.model.spreadsheet import Spreadsheet as SpreadsheetModel
from lark_oapi.api.bitable.v1.model.create_app_request import CreateAppRequest
from lark_oapi.api.bitable.v1.model.req_app import ReqApp

from smartclaw.auth.tool_gate import get_tool_security_context


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ============================================================
# 工具定义（供 Agent 注册使用）
# ============================================================

FEISHU_DOC_TOOL_DEFINITION = {
    "name": "create_feishu_doc",
    "description": "创建飞书在线文档或表格",
    "parameters": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "description": "文档类型：doc(飞书文档)、sheet(飞书表格)、bitable(多维表格)",
                "enum": ["doc", "sheet", "bitable"]
            },
            "title": {
                "type": "string",
                "description": "文档标题"
            },
            "folder_token": {
                "type": "string",
                "description": "文件夹 token（可选，不填则创建在根目录）",
                "default": ""
            },
            "content": {
                "type": "string",
                "description": "可选，创建 doc 后写入的 Markdown 正文内容；sheet/bitable 会忽略",
                "default": ""
            },
            "grant_to_requester": {
                "type": "boolean",
                "description": "是否尝试把文档编辑权限授予当前飞书用户，默认 true",
                "default": True
            }
        },
        "required": ["type", "title"]
    }
}

WRITE_FEISHU_DOC_TOOL_DEFINITION = {
    "name": "write_feishu_doc_content",
    "description": "把 Markdown 正文写入已有飞书文档 docx；通常在 create_feishu_doc 返回 document_id 后调用。",
    "parameters": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "飞书 docx document_id"},
            "content": {"type": "string", "description": "要写入文档的 Markdown 正文"},
        },
        "required": ["document_id", "content"],
    },
}


# ============================================================
# 全局凭证（由 Worker 在启动时设置）
# ============================================================

_current_app_id: str = ""
_current_app_secret: str = ""


def set_feishu_credentials(app_id: str, app_secret: str) -> None:
    """设置当前 Agent 的飞书凭证（由 Worker 调用）"""
    global _current_app_id, _current_app_secret
    _current_app_id = app_id
    _current_app_secret = app_secret


def _get_client() -> lark.Client:
    """获取飞书 API Client"""
    if not _current_app_id or not _current_app_secret:
        raise ValueError("飞书凭证未设置，请先调用 set_feishu_credentials()")
    return (
        lark.Client.builder()
        .app_id(_current_app_id)
        .app_secret(_current_app_secret)
        .build()
    )


def _resp_value(response: Any, name: str, default: Any = None) -> Any:
    value = getattr(response, name, default)
    if callable(value):
        try:
            return value()
        except TypeError:
            return default
    return value


def _response_error(response: Any, action: str) -> str | None:
    """Return a normalized error string when the Feishu SDK response failed."""
    success = getattr(response, "success", None)
    if callable(success):
        try:
            if success():
                return None
        except Exception:
            pass

    code = _resp_value(response, "code", 0)
    try:
        code_i = int(code)
    except Exception:
        code_i = 0 if code in (None, "") else -1
    if code_i == 0:
        return None

    msg = _resp_value(response, "msg", "") or _resp_value(response, "message", "")
    return f"{action}失败: {msg or 'unknown error'} (code={code_i})"


# ============================================================
# 工具执行函数
# ============================================================

async def feishu_doc_handler(
    type: str,
    title: str,
    folder_token: str = "",
    content: str = "",
    grant_to_requester: bool | None = True,
) -> str:
    """
    创建飞书在线文档/表格/多维表格
    
    Args:
        type: 文档类型 - doc/sheet/bitable
        title: 文档标题
        folder_token: 文件夹 token（可选）
    
    Returns:
        JSON 格式的结果，包含 document_id 和 url
    """
    try:
        client = _get_client()
        
        if type == "doc":
            return await _create_document(
                client,
                title,
                folder_token,
                content=content or "",
                grant_to_requester=bool(grant_to_requester if grant_to_requester is not None else True),
            )
        elif type == "sheet":
            return await _create_spreadsheet(client, title, folder_token)
        elif type == "bitable":
            return await _create_bitable(client, title, folder_token)
        else:
            return _json_result({
                "success": False,
                "error": f"不支持的类型: {type}，支持: doc, sheet, bitable"
            })
            
    except Exception as e:
        return _json_result({
            "success": False,
            "error": f"创建飞书文档失败: {str(e)}"
        })


def _doc_url(document_id: str) -> str:
    return f"https://feishu.cn/docx/{document_id}"


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


async def _grant_doc_to_requester(client: lark.Client, document_id: str) -> tuple[bool, str]:
    ctx = get_tool_security_context()
    open_id = (ctx.feishu_open_id if ctx else "") or ""
    if not open_id:
        return False, "no_requester_open_id"
    try:
        request = (
            CreatePermissionMemberRequest.builder()
            .token(document_id)
            .type("docx")
            .need_notification(False)
            .request_body(
                Member.builder()
                .member_type("openid")
                .member_id(open_id)
                .perm("edit")
                .build()
            )
            .build()
        )
        response = client.drive.v1.permission_member.create(request)
        err = _response_error(response, "授权文档给请求用户")
        if err:
            return False, err
        return True, ""
    except Exception as e:
        return False, str(e)


async def _convert_markdown_to_blocks(client: lark.Client, content: str) -> tuple[list[Any], list[str]]:
    request = (
        ConvertDocumentRequest.builder()
        .request_body(
            ConvertDocumentRequestBody.builder()
            .content_type("markdown")
            .content(content)
            .build()
        )
        .build()
    )
    response = client.docx.v1.document.convert(request)
    err = _response_error(response, "转换 Markdown")
    if err:
        raise RuntimeError(err)
    data = response.data
    blocks = _list_value(getattr(data, "blocks", []))
    first_level_ids = _list_value(getattr(data, "first_level_block_ids", []))
    if not first_level_ids:
        first_level_ids = [
            str(getattr(block, "block_id", "") or "")
            for block in blocks
            if getattr(block, "block_id", None)
        ]
    return blocks, first_level_ids


async def _append_markdown_to_document(client: lark.Client, document_id: str, content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        return {"success": True, "blocks_added": 0, "output": "content 为空，跳过写入"}
    blocks, first_level_ids = await _convert_markdown_to_blocks(client, text)
    if not blocks:
        return {"success": True, "blocks_added": 0, "output": "Markdown 未生成可写入块"}
    request = (
        CreateDocumentBlockDescendantRequest.builder()
        .document_id(document_id)
        .block_id(document_id)
        .request_body(
            CreateDocumentBlockDescendantRequestBody.builder()
            .children_id(first_level_ids)
            .descendants(blocks)
            .index(-1)
            .build()
        )
        .build()
    )
    response = client.docx.v1.document_block_descendant.create(request)
    err = _response_error(response, "写入文档内容")
    if err:
        raise RuntimeError(err)
    children = _list_value(getattr(response.data, "children", []))
    return {
        "success": True,
        "blocks_added": len(blocks),
        "inserted_blocks": len(children),
    }


async def write_feishu_doc_content_handler(document_id: str, content: str) -> str:
    """Write Markdown content into an existing Feishu docx."""
    try:
        doc_id = (document_id or "").strip()
        if not doc_id:
            return _json_result({"success": False, "error": "document_id 不能为空"})
        result = await _append_markdown_to_document(_get_client(), doc_id, content)
        return _json_result({
            **result,
            "document_id": doc_id,
            "url": _doc_url(doc_id),
            "type": "doc",
        })
    except Exception as e:
        return _json_result({"success": False, "error": f"写入飞书文档失败: {e}"})


async def _create_document(
    client: lark.Client,
    title: str,
    folder_token: str,
    *,
    content: str = "",
    grant_to_requester: bool = True,
) -> str:
    """创建飞书文档"""
    try:
        request = (
            CreateDocumentRequest.builder()
            .request_body(
                CreateDocumentRequestBody.builder()
                .title(title)
                .folder_token(folder_token or None)
                .build()
            )
            .build()
        )
        
        response = client.docx.v1.document.create(request)
        
        err = _response_error(response, "创建文档")
        if err:
            return _json_result({
                "success": False,
                "error": err,
            })
        
        data = response.data
        doc = data.document
        document_id = doc.document_id
        write_result: dict[str, Any] = {}
        if content:
            write_result = await _append_markdown_to_document(client, document_id, content)
        granted, grant_error = (False, "")
        if grant_to_requester:
            granted, grant_error = await _grant_doc_to_requester(client, document_id)
        return _json_result({
            "success": True,
            "document_id": document_id,
            "url": getattr(doc, "url", "") or _doc_url(document_id),
            "title": title,
            "type": "doc",
            "content_written": bool(content),
            **({"write_result": write_result} if write_result else {}),
            "requester_permission_added": granted,
            **({"requester_permission_error": grant_error} if grant_error else {}),
        })
        
    except Exception as e:
        return _json_result({
            "success": False,
            "error": f"创建文档异常: {str(e)}"
        })


async def _create_spreadsheet(client: lark.Client, title: str, folder_token: str) -> str:
    """创建飞书表格"""
    try:
        request = (
            CreateSpreadsheetRequest.builder()
            .request_body(
                SpreadsheetModel.builder()
                .title(title)
                .folder_token(folder_token or None)
                .build()
            )
            .build()
        )
        
        response = client.sheets.v3.spreadsheet.create(request)
        
        err = _response_error(response, "创建表格")
        if err:
            return _json_result({
                "success": False,
                "error": err,
            })
        
        data = response.data
        spreadsheet = data.spreadsheet
        return _json_result({
            "success": True,
            "spreadsheet_id": spreadsheet.spreadsheet_id,
            "url": getattr(spreadsheet, "url", "") or "",
            "title": title,
            "type": "sheet"
        })
        
    except Exception as e:
        return _json_result({
            "success": False,
            "error": f"创建表格异常: {str(e)}"
        })


async def _create_bitable(client: lark.Client, title: str, folder_token: str) -> str:
    """创建多维表格"""
    try:
        request = (
            CreateAppRequest.builder()
            .request_body(
                ReqApp.builder()
                .name(title)
                .folder_token(folder_token or None)
                .build()
            )
            .build()
        )
        
        response = client.bitable.v1.app.create(request)
        
        err = _response_error(response, "创建多维表格")
        if err:
            return _json_result({
                "success": False,
                "error": err,
            })
        
        data = response.data
        app = data.app
        return _json_result({
            "success": True,
            "bitable_id": app.app_token,
            "url": getattr(app, "url", "") or "",
            "title": title,
            "type": "bitable"
        })
        
    except Exception as e:
        return _json_result({
            "success": False,
            "error": f"创建多维表格异常: {str(e)}"
        })
