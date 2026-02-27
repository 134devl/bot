import os
import sys
import logging
import re
from dotenv import load_dotenv
from aiohttp import web
import asyncpg
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

admin_ids_str = os.getenv("MAIN_ADMIN_IDS", "")
MAIN_ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

if not all([BOT_TOKEN, BASE_WEBHOOK_URL, DATABASE_URL]):
    sys.exit("Error: BOT_TOKEN, BASE_WEBHOOK_URL, or DATABASE_URL is not set")

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                role TEXT DEFAULT 'none',
                group_name TEXT DEFAULT 'Не выбрана',
                accepted_bugs INTEGER DEFAULT 0,
                rejected_bugs INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bugs (
                id SERIAL PRIMARY KEY,
                tester_id BIGINT,
                actual_result TEXT,
                status TEXT DEFAULT 'pending'
            );
        ''')
        for admin_id in MAIN_ADMIN_IDS:
            await conn.execute('''
                INSERT INTO users (user_id, role) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role
            ''', admin_id, 'admin')

async def get_user(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)

async def update_user_info(user_id: int, username: str):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (user_id, username) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
        ''', user_id, username)

def get_user_mention(user_id: int, username: str) -> str:
    return f"@{username}" if username else f"<a href='tg://user?id={user_id}'>{user_id}</a>"

class BugReport(StatesGroup):
    choosing_group = State()
    waiting_for_version = State()
    waiting_for_device = State() 
    waiting_for_steps = State()
    waiting_for_expected = State()
    waiting_for_actual = State()
    waiting_for_media = State()

class AdminState(StatesGroup):
    waiting_for_add_users = State()
    waiting_for_del_users = State()
    waiting_for_broadcast = State()
    waiting_for_points_user = State()
    waiting_for_fix_bug = State()

admin_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👥 Управление тестерами"), KeyboardButton(text="🏆 Баллы")],
        [KeyboardButton(text="📢 Рассылка"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="🐛 Активные баги"), KeyboardButton(text="🛠 Закрыть баг")]
    ], resize_keyboard=True
)

tester_manage_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Добавить (списком)", callback_data="admin_add_testers")],
    [InlineKeyboardButton(text="🗑 Удалить (списком)", callback_data="admin_del_testers")]
])

groups_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Beta A"), KeyboardButton(text="Beta B")]], resize_keyboard=True)
skip_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить")]], resize_keyboard=True)
skip_media_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Без медиа")]], resize_keyboard=True)

async def ping_handler(request):
    """Отвечает UptimeRobot, чтобы бот не засыпал"""
    return web.Response(text="Bot is running OK", status=200)

@router.message(Command("start", "admin", "my_id"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await update_user_info(message.from_user.id, message.from_user.username)
    user = await get_user(message.from_user.id)
    role = user['role'] if user else 'none'
    
    if message.text == "/my_id":
        return await message.answer(f"Ваш ID: <code>{message.from_user.id}</code>\nТег: @{message.from_user.username or 'Нет'}", parse_mode="HTML")

    if role == 'admin':
        await message.answer("👑 <b>Панель управления администратора</b>", reply_markup=admin_kb, parse_mode="HTML")
    elif role == 'tester':
        await message.answer("🛠 <b>Привет, Тестер!</b>\nОтправь баг-репорт командой /report", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    else:
        await message.answer("⛔️ <b>Доступ закрыт.</b>\nПередайте этот ID администратору: <code>{}</code>".format(message.from_user.id), parse_mode="HTML")

@router.message(F.text == "👥 Управление тестерами")
async def btn_manage_testers(message: Message):
    user = await get_user(message.from_user.id)
    if user and user['role'] == 'admin':
        await message.answer("Выберите действие:", reply_markup=tester_manage_kb)

@router.callback_query(F.data.in_(["admin_add_testers", "admin_del_testers"]))
async def cq_manage_testers(call: CallbackQuery, state: FSMContext):
    if call.data == "admin_add_testers":
        await call.message.edit_text("Отправьте мне список ID тестеров (через пробел или с новой строки):")
        await state.set_state(AdminState.waiting_for_add_users)
    else:
        await call.message.edit_text("Отправьте мне список ID тестеров для удаления:")
        await state.set_state(AdminState.waiting_for_del_users)

@router.message(AdminState.waiting_for_add_users)
async def process_bulk_add(message: Message, state: FSMContext):
    ids = [int(x) for x in re.findall(r'\d+', message.text)]
    if not ids: return await message.answer("ID не найдены. Попробуйте снова.")
    
    async with db_pool.acquire() as conn:
        for uid in ids:
            await conn.execute('''
                INSERT INTO users (user_id, role) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role
            ''', uid, 'tester')
            
    await message.answer(f"✅ Тихо добавлено {len(ids)} тестеров.", reply_markup=admin_kb)
    await state.clear()

@router.message(AdminState.waiting_for_del_users)
async def process_bulk_del(message: Message, state: FSMContext):
    ids = [int(x) for x in re.findall(r'\d+', message.text)]
    if not ids: return await message.answer("ID не найдены.")
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET role = 'none' WHERE user_id = ANY($1)", ids)

        
    await message.answer(f"🗑 Тихо удалено {len(ids)} тестеров.", reply_markup=admin_kb)
    await state.clear()

@router.message(F.text == "📢 Рассылка")
async def btn_broadcast(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user and user['role'] == 'admin':
        await message.answer("📦 Пришлите пост (текст, фото, файл) для рассылки всем тестерам.")
        await state.set_state(AdminState.waiting_for_broadcast)

@router.message(AdminState.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    async with db_pool.acquire() as conn:
        testers = await conn.fetch("SELECT user_id FROM users WHERE role = 'tester'")
    
    count = 0
    for t in testers:
        try:
            await message.copy_to(t['user_id'])
            count += 1
        except Exception: pass
            
    await message.answer(f"✅ Обновление разослано! Доставлено: {count} тестерам.", reply_markup=admin_kb)
    await state.clear()

@router.message(F.text == "📊 Статистика")
async def btn_stats(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user['role'] != 'admin': return
    
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, username, group_name, accepted_bugs, rejected_bugs FROM users WHERE role = 'tester' ORDER BY accepted_bugs DESC")
    
    if not users: return await message.answer("Нет данных о тестерах.")
        
    text = "📊 <b>Статистика тестеров:</b>\n\n"
    for u in users:
        mention = get_user_mention(u['user_id'], u['username'])
        text += f"👤 {mention} [{u['group_name']}] | ✅ {u['accepted_bugs']} | ❌ {u['rejected_bugs']}\n"
    
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "🏆 Баллы")
async def btn_points(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user and user['role'] == 'admin':
        await message.answer("Введите ID тестера для изменения его баллов:")
        await state.set_state(AdminState.waiting_for_points_user)

@router.message(AdminState.waiting_for_points_user)
async def process_points_user(message: Message, state: FSMContext):
    target_id = message.text.strip()
    if not target_id.isdigit(): return await message.answer("ID должен состоять из цифр.")
    
    target_id = int(target_id)
    target = await get_user(target_id)
    if not target: return await message.answer("Пользователь не найден в БД.")

    mention = get_user_mention(target['user_id'], target['username'])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принятые +1", callback_data=f"pts_acc_add_{target_id}"),
         InlineKeyboardButton(text="✅ Принятые -1", callback_data=f"pts_acc_sub_{target_id}")],
        [InlineKeyboardButton(text="❌ Отклоненные +1", callback_data=f"pts_rej_add_{target_id}"),
         InlineKeyboardButton(text="❌ Отклоненные -1", callback_data=f"pts_rej_sub_{target_id}")]
    ])
    
    await message.answer(f"Управление баллами: {mention}\nПринято: {target['accepted_bugs']} | Отклонено: {target['rejected_bugs']}", reply_markup=kb, parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("pts_"))
async def cq_edit_points(call: CallbackQuery):
    _, type_, action_, uid = call.data.split("_")
    uid = int(uid)
    
    col = "accepted_bugs" if type_ == "acc" else "rejected_bugs"
    op = "+ 1" if action_ == "add" else "- 1"
    
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE users SET {col} = {col} {op} WHERE user_id = $1", uid)
        target = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", uid)
        
    mention = get_user_mention(target['user_id'], target['username'])
    kb = call.message.reply_markup
    await call.message.edit_text(f"Управление баллами: {mention}\nПринято: {target['accepted_bugs']} | Отклонено: {target['rejected_bugs']}", reply_markup=kb, parse_mode="HTML")

@router.message(Command("report"))
async def start_report(message: Message, state: FSMContext):
    await update_user_info(message.from_user.id, message.from_user.username)
    user = await get_user(message.from_user.id)
    if not user or user['role'] not in ['tester', 'admin']: return
    
    await message.answer("Выберите вашу группу тестирования:", reply_markup=groups_kb)
    await state.set_state(BugReport.choosing_group)

@router.message(BugReport.choosing_group)
async def process_group(message: Message, state: FSMContext):
    if message.text not in ["Beta A", "Beta B"]: return
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET group_name = $1 WHERE user_id = $2", message.text, message.from_user.id)
        
    await state.update_data(group=message.text)
    await message.answer("<b>Шаг 1 из 6:</b> Укажите версию (билд):", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_version)

@router.message(BugReport.waiting_for_version)
async def process_version(message: Message, state: FSMContext):
    await state.update_data(version=message.text)
    await message.answer("<b>Шаг 2 из 6:</b> Укажите модель телефона (Или нажмите 'Пропустить'):", reply_markup=skip_kb, parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_device)

@router.message(BugReport.waiting_for_device)
async def process_device(message: Message, state: FSMContext):
    device = message.text if message.text != "Пропустить" else "Не указано"
    await state.update_data(device=device)
    await message.answer("<b>Шаг 3 из 6:</b> Опишите шаги воспроизведения:", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_steps)

@router.message(BugReport.waiting_for_steps)
async def process_steps(message: Message, state: FSMContext):
    await state.update_data(steps=message.text)
    await message.answer("<b>Шаг 4 из 6:</b> Ожидаемый результат:", parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_expected)

@router.message(BugReport.waiting_for_expected)
async def process_expected(message: Message, state: FSMContext):
    await state.update_data(expected=message.text)
    await message.answer("<b>Шаг 5 из 6:</b> Фактический результат:", parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_actual)

@router.message(BugReport.waiting_for_actual)
async def process_actual(message: Message, state: FSMContext):
    await state.update_data(actual=message.text)
    await message.answer("<b>Шаг 6 из 6:</b> Прикрепите медиа (или нажмите кнопку):", reply_markup=skip_media_kb, parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_media)

@router.message(BugReport.waiting_for_media)
async def process_media(message: Message, state: FSMContext):
    data = await state.get_data()
    user_db = await get_user(message.from_user.id)
    
    async with db_pool.acquire() as conn:
        bug_id = await conn.fetchval(
            'INSERT INTO bugs (tester_id, actual_result) VALUES ($1, $2) RETURNING id',
            message.from_user.id, data.get('actual')
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"bug:accept:{bug_id}:{message.from_user.id}")],
        [InlineKeyboardButton(text="♻️ Уже было", callback_data=f"bug:dup:{bug_id}:{message.from_user.id}")],
        [InlineKeyboardButton(text="❌ Не баг", callback_data=f"bug:notbug:{bug_id}:{message.from_user.id}")]
    ])

    mention = get_user_mention(user_db['user_id'], user_db['username'])
    report_text = (
        f"🚨 <b>БАГ #{bug_id}</b>\n\n"
        f"👤 <b>От:</b> {mention}\n"
        f"🏷 <b>Группа:</b> {data.get('group')}\n"
        f"📱 <b>Устройство:</b> {data.get('device')}\n"
        f"<b>1. Версия:</b> {data.get('version')}\n"
        f"<b>2. Шаги:</b>\n{data.get('steps')}\n"
        f"<b>3. Ожидалось:</b>\n{data.get('expected')}\n"
        f"<b>4. Факт:</b>\n{data.get('actual')}"
    )

    for admin_id in MAIN_ADMIN_IDS:
        try:
            await bot.send_message(admin_id, report_text, parse_mode="HTML", reply_markup=kb)
            if message.text != "Без медиа":
                await message.copy_to(admin_id)
        except Exception: pass
            
    await message.answer("✅ Баг отправлен. Ожидайте ответа разработчика.", reply_markup=ReplyKeyboardRemove())
    await state.clear()

@router.callback_query(F.data.startswith("bug:"))
async def handle_bug_decision(call: CallbackQuery):
    _, action, bug_id, tester_id = call.data.split(":")
    bug_id, tester_id = int(bug_id), int(tester_id)
    
    async with db_pool.acquire() as conn:
        if action == "accept":
            await conn.execute("UPDATE bugs SET status = 'accepted' WHERE id = $1", bug_id)
            await conn.execute("UPDATE users SET accepted_bugs = accepted_bugs + 1 WHERE user_id = $1", tester_id)
            msg_to_tester, status_text = f"✅ Ваш баг-репорт #{bug_id} принят!", "✅ <b>Принят</b>"
        elif action in ["dup", "notbug"]:
            await conn.execute("UPDATE bugs SET status = 'rejected' WHERE id = $1", bug_id)
            await conn.execute("UPDATE users SET rejected_bugs = rejected_bugs + 1 WHERE user_id = $1", tester_id)
            reason = "Уже было" if action == "dup" else "Не является багом"
            msg_to_tester, status_text = f"❌ Баг-репорт #{bug_id} отклонен ({reason}).", f"❌ <b>Отклонен ({reason})</b>"

    try: await bot.send_message(tester_id, msg_to_tester)
    except Exception: pass
        
    new_text = call.message.html_text + f"\n\n<i>Статус: {status_text}</i>"
    await call.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)

@router.message(F.text == "🐛 Активные баги")
async def btn_active_bugs(message: Message):
    user = await get_user(message.from_user.id)
    if not user or user['role'] != 'admin': return
    
    async with db_pool.acquire() as conn:
        bugs = await conn.fetch("SELECT id, actual_result FROM bugs WHERE status = 'accepted'")
        
    if not bugs: return await message.answer("🎉 Нет активных принятых багов.")
        
    text = "📝 <b>Список активных багов:</b>\n\n"
    for b in bugs: text += f"▪️ <b>#{b['id']}</b>: {b['actual_result'][:50]}...\n"
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "🛠 Закрыть баг")
async def btn_fix_bug(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user or user['role'] != 'admin': return
    await message.answer("Введите ID бага, который был исправлен:")
    await state.set_state(AdminState.waiting_for_fix_bug)

@router.message(AdminState.waiting_for_fix_bug)
async def process_fix_bug(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("ID должен состоять из цифр.")
    bug_id = int(message.text)
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE bugs SET status = 'fixed' WHERE id = $1", bug_id)
        
    await message.answer(f"🛠 Баг #{bug_id} отмечен как исправленный!", reply_markup=admin_kb)
    await state.clear()

async def on_startup(bot: Bot):
    await init_db()
    if BASE_WEBHOOK_URL:
        await bot.set_webhook(f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}", drop_pending_updates=True)

def main():
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get('/', ping_handler)
    
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    main()

