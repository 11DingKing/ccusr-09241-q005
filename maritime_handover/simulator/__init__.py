"""模拟器子包：文件驱动的离散时间模拟与结果核对。"""

from .simulator import run_file, run_simulation
from .verifier import Verifier

__all__ = ["Verifier", "run_file", "run_simulation"]
