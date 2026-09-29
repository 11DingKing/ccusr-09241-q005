"""接口层：本地 JSON API。"""

from .http_api import ApiContext, build_default_context, build_server

__all__ = ["ApiContext", "build_default_context", "build_server"]
