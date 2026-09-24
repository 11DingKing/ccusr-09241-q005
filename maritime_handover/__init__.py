"""海上任务天地链路交接计划器的服务端包入口。"""

PROJECT_CODE = "maritime_handover"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识，供运行检查和诊断使用。"""
    return {"code": PROJECT_CODE, "title": "海上任务天地链路交接计划器"}
