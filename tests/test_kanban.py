import pytest
from sqlalchemy import select

from app.models import CardStatus, Employee, KanbanCard
from app.services import kanban
from app.services.errors import PermissionDenied, ValidationError
from tests.conftest import MARKETING_REMELY, OWNER_TG, SALES_FLER, SALES_HEAD, WAREHOUSE_VELSE


async def _emp_id(session, tg):
    return await session.scalar(select(Employee.id).where(Employee.telegram_id == tg))


async def test_employee_creates_and_moves_own_department_card(session, actor):
    emp = await actor(SALES_FLER)
    p = await kanban.create_card(session, emp, company="fler", department="sales", title="Позвонить клиенту",
                                 assignee_id=await _emp_id(session, SALES_FLER))
    board = await kanban.board(session, emp, "fler", "sales")
    assert board[CardStatus.BACKLOG][-1].id == p.entity_id

    await kanban.move_card(session, emp, card_id=p.entity_id, status="in_progress", position=0)
    board = await kanban.board(session, emp, "fler", "sales")
    column = board[CardStatus.IN_PROGRESS]
    assert column[0].id == p.entity_id
    assert [c.position for c in column] == list(range(len(column)))


async def test_reorder_within_column(session, actor):
    owner = await actor(OWNER_TG)
    ids = []
    for t in ["A", "B", "C"]:
        ids.append((await kanban.create_card(session, owner, company="velse", department="finance",
                                             title=t, status="review")).entity_id)
    await kanban.move_card(session, owner, card_id=ids[2], status="review", position=0)
    col = [c.id for c in (await kanban.board(session, owner, "velse", "finance"))[CardStatus.REVIEW]]
    assert col.index(ids[2]) < col.index(ids[0]) < col.index(ids[1])


async def test_employee_cannot_touch_other_department_or_company(session, actor):
    emp = await actor(SALES_FLER)
    other = await session.scalar(select(KanbanCard).where(KanbanCard.department_id != 1))
    with pytest.raises(PermissionDenied):
        await kanban.move_card(session, emp, card_id=other.id, status="done")
    with pytest.raises(PermissionDenied):
        await kanban.create_card(session, emp, company="remely", department="sales", title="x")


async def test_delete_only_head_or_owner(session, actor):
    emp, head = await actor(SALES_FLER), await actor(SALES_HEAD)
    p = await kanban.create_card(session, emp, company="fler", department="sales", title="Удалить меня")
    with pytest.raises(PermissionDenied):
        await kanban.delete_card(session, emp, card_id=p.entity_id)
    await kanban.delete_card(session, head, card_id=p.entity_id)
    assert await session.get(KanbanCard, p.entity_id) is None


async def test_employee_edits_only_own_or_assigned(session, actor):
    emp, owner = await actor(SALES_FLER), await actor(OWNER_TG)
    p = await kanban.create_card(session, owner, company="fler", department="sales", title="Чужая")
    with pytest.raises(PermissionDenied):
        await kanban.update_card(session, emp, card_id=p.entity_id, title="Моя")
    await kanban.update_card(session, owner, card_id=p.entity_id, assignee_id=await _emp_id(session, SALES_FLER))
    await kanban.update_card(session, emp, card_id=p.entity_id, title="Теперь моя")
    # двигать любую карточку своего отдела можно
    await kanban.move_card(session, emp, card_id=p.entity_id, status="review")


async def test_assignee_must_work_in_department(session, actor):
    owner = await actor(OWNER_TG)
    with pytest.raises(ValidationError):
        await kanban.create_card(session, owner, company="fler", department="sales", title="x",
                                 assignee_id=await _emp_id(session, WAREHOUSE_VELSE))
    # грант на чтение ОП FLÉR позволяет быть исполнителем
    await kanban.create_card(session, owner, company="fler", department="sales", title="x",
                             assignee_id=await _emp_id(session, MARKETING_REMELY))


async def test_holding_board_merges_companies(session, actor):
    owner = await actor(OWNER_TG)
    total = sum(len(v) for v in (await kanban.board(session, owner, "holding", "marketing")).values())
    parts = 0
    for c in ["fler", "remely", "velse"]:
        parts += sum(len(v) for v in (await kanban.board(session, owner, c, "marketing")).values())
    assert total == parts == 9
