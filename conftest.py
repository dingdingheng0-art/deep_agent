"""pytest 全局配置：把 testcase_agent 包目录加入 sys.path，
使测试可用裸导入（import file_utils / reqtest_agent 等），兼容 pytest 9 的 importlib 导入模式。"""

import sys
from pathlib import Path

_PKG = str(Path(__file__).parent / "testcase_agent")
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)
