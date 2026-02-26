import os
import sys
import logging
import asyncio
from dotenv import load_dotenv
from aiohttp import web
import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

admin_ids_str = os.getenv("MAIN_ADMIN_IDS", "")
MAIN_ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    sys.exit("Error: BOT_TOKEN is not set")
if not BASE_WEBHOOK_URL:
    sys.exit("Error: BASE_WEBHOOK_URL is not set")

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

async def init_db():
    async with aiosqlite.connect('beta_test.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'none',
                accepted_bugs INTEGER DEFAULT 0,
                rejected_bugs INTEGER DEFAULT 0
            )
        ''')
        try:
            await db.execute('ALTER TABLE users ADD COLUMN accepted_bugs INTEGER DEFAULT 0')
            await db.execute('ALTER TABLE users ADD COLUMN rejected_bugs INTEGER DEFAULT 0')
        except:
            pass
            
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bugs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tester_id INTEGER,
                actual_result TEXT,
                status TEXT DEFAULT 'pending'
            )
        ''')
        
        for admin_id in MAIN_ADMIN_IDS:
            await db.execute('INSERT OR IGNORE INTO users (user_id, role) VALUES (?, ?)', (admin_id, 'admin'))
            
        await db.commit()

async def get_user_role(user_id: int) -> str:
    async with aiosqlite.connect('beta_test.db') as db:
        cursor = await db.execute('SELECT role FROM users WHERE user_id = ?', (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 'none'

class BugReport(StatesGroup):
    choosing_group = State()
    waiting_for_version = State()
    waiting_for_steps = State()
    waiting_for_expected = State()
    waiting_for_actual = State()
    waiting_for_media = State()

class Broadcast(StatesGroup):
    writing_message = State()

groups_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Beta A"), KeyboardButton(text="Beta B")]],
    resize_keyboard=True,
    input_field_placeholder="Выберите группу тестирования"
)

skip_media_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Без медиа")]],
    resize_keyboard=True
)

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    role = await get_user_role(message.from_user.id)
    
    if role == 'admin':
        await message.answer(
            "👑 <b>Привет, Админ!</b>\n\n"
            "Доступные команды:\n"
            "/add_user [id] — добавить тестера\n"
            "/del_user [id] — удалить тестера\n"
            "/send_update — разослать новую бету\n"
            "/reply [id] [текст] — ответить тестеру\n"
            "/bugs — список активных багов\n"
            "/fix [id_бага] — отметить баг исправленным\n"
            "/stats — статистика тестеров\n"
            "/set_role [роль] — дебаг-смена роли",
            parse_mode="HTML"
        )
    elif role == 'tester':
        await message.answer(
            "🛠 <b>Привет, Тестер!</b>\n\nЧтобы отправить баг-репорт, нажми команду /report",
            parse_mode="HTML"
        )
    else:
        await message.answer("⛔️ <b>Доступ закрыт.</b>\nДля регистрации нажмите /my_id", parse_mode="HTML")

@router.message(Command("my_id"))
async def cmd_my_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>\nСкопируйте его и отправьте администратору.", parse_mode="HTML")

@router.message(Command("add_user"))
async def add_tester(message: Message):
    if await get_user_role(message.from_user.id) != 'admin': return
    try:
        new_user_id = int(message.text.split()[1])
        async with aiosqlite.connect('beta_test.db') as db:
            await db.execute('INSERT OR REPLACE INTO users (user_id, role) VALUES (?, ?)', (new_user_id, 'tester'))
            await db.commit()
        await message.answer(f"✅ Пользователь <code>{new_user_id}</code> добавлен!", parse_mode="HTML")
        try:
            await bot.send_message(new_user_id, "🎉 Вы добавлены в систему тестирования! Нажмите /start")
        except:
            pass
    except (IndexError, ValueError):
        await message.answer("⚠️ Использование: <code>/add_user 123456789</code>", parse_mode="HTML")

@router.message(Command("del_user"))
async def del_tester(message: Message):
    if await get_user_role(message.from_user.id) != 'admin': return
    try:
        del_user_id = int(message.text.split()[1])
        async with aiosqlite.connect('beta_test.db') as db:
            await db.execute('DELETE FROM users WHERE user_id = ?', (del_user_id,))
            await db.commit()
        await message.answer(f"🗑 Пользователь <code>{del_user_id}</code> удален.", parse_mode="HTML")
        try:
            await bot.send_message(del_user_id, "⛔️ Ваш доступ к системе тестирования был аннулирован.")
        except:
            pass
    except (IndexError, ValueError):
        await message.answer("⚠️ Использование: <code>/del_user 123456789</code>", parse_mode="HTML")

@router.message(Command("reply"))
async def cmd_reply(message: Message):
    if await get_user_role(message.from_user.id) != 'admin': return
    try:
        parts = message.text.split(maxsplit=2)
        user_id = int(parts[1])
        text = parts[2]
        await bot.send_message(user_id, f"👨‍💻 <b>Сообщение от разработчика:</b>\n\n{text}", parse_mode="HTML")
        await message.answer("✅ Сообщение отправлено.")
    except (IndexError, ValueError):
        await message.answer("⚠️ Использование: <code>/reply [ID] [текст]</code>", parse_mode="HTML")

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if await get_user_role(message.from_user.id) != 'admin': return
    async with aiosqlite.connect('beta_test.db') as db:
        cursor = await db.execute('SELECT user_id, accepted_bugs, rejected_bugs FROM users WHERE role = "tester"')
        users = await cursor.fetchall()
    
    if not users:
        return await message.answer("Нет данных о тестерах.")
        
    text = "📊 <b>Статистика тестеров:</b>\n\n"
    for uid, acc, rej in users:
        text += f"👤 <code>{uid}</code> | ✅ {acc} | ❌ {rej}\n"
    await message.answer(text, parse_mode="HTML")

@router.message(Command("send_update"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if await get_user_role(message.from_user.id) != 'admin': return
    await message.answer("📦 Пришлите файл, фото или текст с описанием обновления.")
    await state.set_state(Broadcast.writing_message)

@router.message(Broadcast.writing_message)
async def process_broadcast(message: Message, state: FSMContext):
    async with aiosqlite.connect('beta_test.db') as db:
        cursor = await db.execute('SELECT user_id FROM users WHERE role = "tester"')
        testers = await cursor.fetchall()
    
    count = 0
    for tester in testers:
        try:
            await bot.send_message(tester[0], "🚀 <b>Доступен новый билд!</b>", parse_mode="HTML")
            await message.copy_to(tester[0])
            count += 1
        except:
            pass
            
    await message.answer(f"✅ Обновление разослано! Доставлено: {count} тестерам.")
    await state.clear()

@router.message(Command("report"))
async def start_report(message: Message, state: FSMContext):
    role = await get_user_role(message.from_user.id)
    if role not in ['tester', 'admin']: return
    await message.answer("Выберите вашу группу тестирования:", reply_markup=groups_kb)
    await state.set_state(BugReport.choosing_group)

@router.message(BugReport.choosing_group)
async def process_group(message: Message, state: FSMContext):
    if message.text not in ["Beta A", "Beta B"]: return
    await state.update_data(group=message.text)
    await message.answer("<b>Шаг 1 из 5:</b> Укажите версию (билд):", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_version)

@router.message(BugReport.waiting_for_version)
async def process_version(message: Message, state: FSMContext):
    await state.update_data(version=message.text or "Не указано")
    await message.answer("<b>Шаг 2 из 5:</b> Опишите шаги воспроизведения:", parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_steps)

@router.message(BugReport.waiting_for_steps)
async def process_steps(message: Message, state: FSMContext):
    await state.update_data(steps=message.text or "Не указано")
    await message.answer("<b>Шаг 3 из 5:</b> Ожидаемый результат:", parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_expected)

@router.message(BugReport.waiting_for_expected)
async def process_expected(message: Message, state: FSMContext):
    await state.update_data(expected=message.text or "Не указано")
    await message.answer("<b>Шаг 4 из 5:</b> Фактический результат:", parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_actual)

@router.message(BugReport.waiting_for_actual)
async def process_actual(message: Message, state: FSMContext):
    await state.update_data(actual=message.text or "Не указано")
    await message.answer("<b>Шаг 5 из 5:</b> Прикрепите медиа (или нажмите кнопку):", reply_markup=skip_media_kb, parse_mode="HTML")
    await state.set_state(BugReport.waiting_for_media)

@router.message(BugReport.waiting_for_media)
async def process_media(message: Message, state: FSMContext):
    data = await state.get_data()
    user = message.from_user
    
    async with aiosqlite.connect('beta_test.db') as db:
        cursor = await db.execute('INSERT INTO bugs (tester_id, actual_result) VALUES (?, ?)', (user.id, data.get('actual')))
        bug_id = cursor.lastrowid
        await db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"bug:accept:{bug_id}:{user.id}")],
        [InlineKeyboardButton(text="♻️ Уже было", callback_data=f"bug:dup:{bug_id}:{user.id}")],
        [InlineKeyboardButton(text="❌ Не баг", callback_data=f"bug:notbug:{bug_id}:{user.id}")]
    ])

    report_text = (
        f"🚨 <b>БАГ #{bug_id}</b>\n\n"
        f"👤 <b>От:</b> <code>{user.id}</code>\n"
        f"🏷 <b>Группа:</b> {data.get('group')}\n"
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
        except Exception:
            pass
            
    await message.answer("✅ Баг отправлен. Ожидайте ответа разработчика.", reply_markup=ReplyKeyboardRemove())
    await state.clear()

@router.callback_query(F.data.startswith("bug:"))
async def handle_bug_decision(call: CallbackQuery):
    _, action, bug_id, tester_id = call.data.split(":")
    bug_id, tester_id = int(bug_id), int(tester_id)
    
    async with aiosqlite.connect('beta_test.db') as db:
        if action == "accept":
            await db.execute('UPDATE bugs SET status = "accepted" WHERE id = ?', (bug_id,))
            await db.execute('UPDATE users SET accepted_bugs = accepted_bugs + 1 WHERE user_id = ?', (tester_id,))
            msg_to_tester = f"✅ Ваш баг-репорт #{bug_id} принят разработчиком!"
            status_text = "✅ <b>Принят</b>"
        elif action in ["dup", "notbug"]:
            await db.execute('UPDATE bugs SET status = "rejected" WHERE id = ?', (bug_id,))
            await db.execute('UPDATE users SET rejected_bugs = rejected_bugs + 1 WHERE user_id = ?', (tester_id,))
            reason = "Уже было" if action == "dup" else "Не является багом"
            msg_to_tester = f"❌ Ваш баг-репорт #{bug_id} отклонен. Причина: {reason}."
            status_text = f"❌ <b>Отклонен ({reason})</b>"
        await db.commit()

    try:
        await bot.send_message(tester_id, msg_to_tester)
    except:
        pass
        
    new_text = call.message.html_text + f"\n\n<i>Статус: {status_text}</i>"
    await call.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)

@router.message(Command("bugs"))
async def cmd_bugs(message: Message):
    if await get_user_role(message.from_user.id) != 'admin': return
    async with aiosqlite.connect('beta_test.db') as db:
        cursor = await db.execute('SELECT id, actual_result FROM bugs WHERE status = "accepted"')
        bugs = await cursor.fetchall()
        
    if not bugs:
        return await message.answer("🎉 Нет активных принятых багов.")
        
    text = "📝 <b>Список активных багов:</b>\n\n"
    for b_id, actual in bugs:
        text += f"▪️ <b>#{b_id}</b>: {actual}\n"
    text += "\n<i>Чтобы закрыть баг, введите /fix [ID]</i>"
    await message.answer(text, parse_mode="HTML")

@router.message(Command("fix"))
async def cmd_fix(message: Message):
    if await get_user_role(message.from_user.id) != 'admin': return
    try:
        bug_id = int(message.text.split()[1])
        async with aiosqlite.connect('beta_test.db') as db:
            await db.execute('UPDATE bugs SET status = "fixed" WHERE id = ?', (bug_id,))
            await db.commit()
        await message.answer(f"🛠 Баг #{bug_id} отмечен как исправленный!")
    except (IndexError, ValueError):
        await message.answer("⚠️ Использование: <code>/fix [ID]</code>", parse_mode="HTML")

@router.message(Command("set_role"))
async def cmd_debug_role(message: Message):
    if message.from_user.id not in MAIN_ADMIN_IDS: return
    try:
        new_role = message.text.split()[1].lower()
        async with aiosqlite.connect('beta_test.db') as db:
            await db.execute('INSERT OR REPLACE INTO users (user_id, role) VALUES (?, ?)', (message.from_user.id, new_role))
            await db.commit()
        await message.answer(f"🔧 Роль изменена на: <b>{new_role}</b>.", parse_mode="HTML")
    except IndexError:
        pass

async def on_startup(bot: Bot):
    await init_db()
    if BASE_WEBHOOK_URL:
        await bot.set_webhook(f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}", drop_pending_updates=True)

def main():
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    main()