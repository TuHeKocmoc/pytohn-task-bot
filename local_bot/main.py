import logging
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from apscheduler import Scheduler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from ..bitrix_bot.services.openai_service import parse_message_with_openai
from ..bitrix_bot.utils import extract_mention_username
from ..bitrix_bot.db import (
    init_db,
    add_user,
    get_user,
    get_user_by_username,
    set_user_chat_id,
    create_task,
    update_task,
    get_tasks_for_user,
    get_tasks_due_soon,
)

logging.basicConfig(level=logging.INFO)

CHOOSE_FIELD, WAIT_VALUE = range(2)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    row = get_user(telegram_id)
    if not row:
        add_user(telegram_id, username)
    await update.message.reply_text("Привет! Я автономный таск-бот.")


async def notifications_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    telegram_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    if not get_user(telegram_id):
        add_user(telegram_id, username)
    set_user_chat_id(telegram_id, chat_id)
    await update.message.reply_text("Этот чат назначен для уведомлений.")


async def create_task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    text = update.message.text
    if "#задача" not in text.lower():
        return

    parsed = parse_message_with_openai(text)
    title = parsed.get("title", "Без названия")
    description = parsed.get("description", "")
    deadline_str = parsed.get("deadline")
    deadline = None
    if deadline_str:
        try:
            deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            deadline = None

    mention_user = extract_mention_username(update.message)
    reply_user_obj = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    responsible_username = mention_user or (reply_user_obj.username if reply_user_obj else None)
    responsible_id = None
    if responsible_username:
        user_row = get_user_by_username(responsible_username)
        if user_row:
            responsible_id = user_row[1]

    chat_id = update.effective_chat.id
    task_id = create_task(chat_id, title, description, deadline, responsible_id)

    await update.message.reply_text("Задача сохранена!")

    target_chat = None
    if responsible_id:
        user_row = get_user(responsible_id)
        if user_row and user_row[4]:
            target_chat = user_row[4]
    if not target_chat:
        target_chat = chat_id

    lines = [f"*Задача {task_id}:* {title}"]
    if description:
        lines.append(f"*Описание:* {description}")
    if deadline:
        lines.append(f"*Дедлайн:* {deadline.strftime('%d.%m.%Y %H:%M')}")
    if responsible_username:
        lines.append(f"*Ответственный:* {responsible_username}")

    keyboard = [
        [InlineKeyboardButton("Изменить", callback_data=f"edit:{task_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(target_chat, "\n".join(lines), parse_mode="Markdown", reply_markup=reply_markup)


def build_task_text(task: dict) -> str:
    lines = [f"*Задача {task['id']}:* {task['title']}"]
    if task.get('description'):
        lines.append(f"*Описание:* {task['description']}")
    if task.get('deadline'):
        dt = task['deadline']
        lines.append(f"*Дедлайн:* {dt.strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)


async def edit_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, task_id_str = query.data.split(":")
    task_id = int(task_id_str)
    context.user_data['task_id'] = task_id
    keyboard = [
        [InlineKeyboardButton("Название", callback_data="field:title")],
        [InlineKeyboardButton("Описание", callback_data="field:description")],
        [InlineKeyboardButton("Дедлайн", callback_data="field:deadline")],
        [InlineKeyboardButton("Отмена", callback_data="cancel")],
    ]
    await query.message.reply_text("Что изменить?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_FIELD


async def choose_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.message.edit_text("Отменено")
        return ConversationHandler.END
    _, field = query.data.split(":")
    context.user_data['field'] = field
    await query.message.edit_text("Введите новое значение:")
    return WAIT_VALUE


async def set_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id = context.user_data.get('task_id')
    field = context.user_data.get('field')
    text = update.message.text
    kwargs = {}
    if field == 'title':
        kwargs['title'] = text
    elif field == 'description':
        kwargs['description'] = text
    elif field == 'deadline':
        try:
            kwargs['deadline'] = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            await update.message.reply_text('Неверный формат даты')
            return WAIT_VALUE
    update_task(task_id, **kwargs)
    await update.message.reply_text('Обновлено')
    return ConversationHandler.END


async def report_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    now = datetime.now()
    rows = get_tasks_for_user(telegram_id)
    if not rows:
        await update.message.reply_text('Задач нет')
        return
    lines = []
    for t in rows:
        deadline = t.get('deadline')
        remain = ''
        if deadline:
            diff = deadline - now
            remain = f"До дедлайна: {diff.days}д {diff.seconds//3600}ч"
        lines.append(f"{t['title']} - {remain}")
    await update.message.reply_text("\n".join(lines))


async def reminder_job(application):
    tasks = get_tasks_due_soon(24)
    now = datetime.now()
    for task in tasks:
        resp_id = task.get('responsible_id')
        if not resp_id:
            continue
        user_row = get_user(resp_id)
        if not user_row:
            continue
        chat_id = user_row[4]
        if not chat_id:
            continue
        deadline = task.get('deadline')
        diff = deadline - now
        text = (f"Напоминание! Задача '{task['title']}' скоро дедлайн. "
                f"Осталось {diff.days}д {diff.seconds//3600}ч")
        try:
            await application.bot.send_message(chat_id, text)
        except Exception:
            logging.exception('Failed to send reminder')


def reminder_job_wrapper(application):
    import asyncio
    asyncio.run(reminder_job(application))


def main():
    init_db()
    application = ApplicationBuilder().token(os.environ.get('BOT_TOKEN')).build()

    application.add_handler(CommandHandler('start', start_handler))
    application.add_handler(CommandHandler('notifications', notifications_command_handler))
    application.add_handler(CommandHandler('report', report_command_handler))
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_task_callback, pattern=r'^edit:\d+$')],
        states={
            CHOOSE_FIELD: [CallbackQueryHandler(choose_field_callback, pattern=r'^field:|^cancel$')],
            WAIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_new_value)],
        },
        fallbacks=[CallbackQueryHandler(lambda u, c: ConversationHandler.END, pattern='^cancel$')],
    )
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(edit_task_callback, pattern=r'^edit:\d+$'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, create_task_handler))

    with Scheduler() as scheduler:
        daily_trigger = CronTrigger(hour=9, minute=0)
        scheduler.add_schedule(reminder_job_wrapper, daily_trigger, args=[application], id='daily_reminder')
        scheduler.start_in_background()
        application.run_polling()


if __name__ == '__main__':
    import os
    main()
