"""Все правила доступа в одном месте. Функции чистые: решают только по Actor.

Владелец — всё. Руководитель и сотрудник — свой отдел (в своей компании или во всех,
если company_id не задан) плюс выданные гранты. Сводку видят все в пределах своих компаний.
"""
from app.models import KanbanCard
from app.services.context import Actor
from app.services.errors import PermissionDenied


def _own_scope(actor: Actor, department: str, company_id: int) -> bool:
    return actor.department == department and actor.company_id in (None, company_id)


def _granted(actor: Actor, department: str, company_id: int, write: bool = False) -> bool:
    return any(
        g.department == department and g.company_id in (None, company_id) and (g.can_write_kanban or not write)
        for g in actor.grants
    )


def can_read_department(actor: Actor, department: str, company_id: int) -> bool:
    return actor.is_owner or _own_scope(actor, department, company_id) or _granted(actor, department, company_id)


def can_read_summary(actor: Actor, company_id: int) -> bool:
    if actor.is_owner or (actor.department is not None and actor.company_id in (None, company_id)):
        return True
    return any(g.company_id in (None, company_id) for g in actor.grants)


def can_write_kanban(actor: Actor, department: str, company_id: int) -> bool:
    return (
        actor.is_owner
        or _own_scope(actor, department, company_id)
        or _granted(actor, department, company_id, write=True)
    )


def can_edit_card(actor: Actor, card: KanbanCard, department: str) -> bool:
    if not can_write_kanban(actor, department, card.company_id):
        return False
    if actor.is_owner or (actor.is_head and _own_scope(actor, department, card.company_id)):
        return True
    return actor.employee_id is not None and actor.employee_id in (card.created_by_id, card.assignee_id)


def can_delete_card(actor: Actor, department: str, company_id: int) -> bool:
    return actor.is_owner or (actor.is_head and _own_scope(actor, department, company_id))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PermissionDenied(message)


def require_owner(actor: Actor, what: str) -> None:
    require(actor.is_owner, f"Нет прав: {what} может только владелец")


def can_read_department_somewhere(actor: Actor, department: str) -> bool:
    """Видит ли отдел хотя бы в одной компании — для показа страницы в меню."""
    return actor.is_owner or actor.department == department or any(g.department == department for g in actor.grants)
