from factory.config import ROLES


PERMISSIONS = {
    "monitor.read": ("设备读数与趋势", ("viewer", "operator", "admin")),
    "screen.read": ("管理大屏", ("viewer", "operator", "admin")),
    "assistant.ask": ("智能体问数", ("viewer", "operator", "admin")),
    "command.draft": ("预览控制指令", ("operator", "admin")),
    "command.confirm": ("确认控制指令", ("operator", "admin")),
    "alert.close": ("关闭告警工单", ("operator", "admin")),
    "alert.review": ("登记工单回访", ("operator", "admin")),
    "records.read": ("查询业务和操作记录", ("viewer", "operator", "admin")),
    "reports.export": ("导出 Word 报告", ("viewer", "operator", "admin")),
    "rules.manage": ("维护阈值", ("admin",)),
    "audit.read": ("查询操作审计", ("admin",)),
    "demo.run": ("注入模拟设备场景", ("operator", "admin")),
    "business.write": ("维护产量与库存", ("operator", "admin")),
    "decision.adopt": ("采纳处置建议", ("operator", "admin")),
    "handoff.manage": ("发起与完成人工处置", ("operator", "admin")),
    "pass.manage": ("签发、核验并确认通行", ("operator", "admin")),
    "skills.manage": ("维护话术和测试模型工具", ("admin",)),
}


def allowed(role: str | None, permission: str) -> bool:
    return role in PERMISSIONS[permission][1]


def role_permissions(role: str | None) -> list[str]:
    return [name for name in PERMISSIONS if allowed(role, name)]


def permission_matrix() -> dict:
    return {
        "roles": ROLES,
        "permissions": [{"key": key, "label": label, "roles": list(roles)}
                        for key, (label, roles) in PERMISSIONS.items()],
    }
