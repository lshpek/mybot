from aiogram.fsm.state import State, StatesGroup


class OrderForm(StatesGroup):
    service = State()
    description = State()
    features = State()
    budget = State()
    budget_custom = State()
    deadline = State()
    repository_url = State()
    contact_choice = State()
    custom_contact = State()
    confirm = State()


class AdminNote(StatesGroup):
    text = State()


class AdminReply(StatesGroup):
    text = State()


class AdminSearch(StatesGroup):
    order_id = State()


class AdminPrice(StatesGroup):
    amount = State()
    note = State()
    confirm = State()


class AdminClarification(StatesGroup):
    question = State()
    confirm = State()


class CustomerClarification(StatesGroup):
    answer = State()
