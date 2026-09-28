"""测试用例生成 Agent 的自定义图状态。"""

from typing import TypedDict

from deepagents import DeepAgentState
from typing_extensions import NotRequired


class UploadedFile(TypedDict):
    """前端上传的待解析文件（base64 编码，作为图输入一次性传入）。

    由 DoclingParseMiddleware 在 agent 启动前消费：解析为 Markdown 写入
    虚拟文件系统 /uploads/ 后，该通道被清空。
    """

    filename: str
    mime_type: str
    data_base64: str


class TestcaseAgentState(DeepAgentState):
    """在 DeepAgentState 基础上增加文件上传通道。"""

    uploaded_files: NotRequired[list[UploadedFile]]
