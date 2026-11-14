import os
import re
import json
import base64
import sqlite3
import datetime
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types

# ----------------- КОНФИГУРАЦИЯ -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "9235975896:AAFbwppKTIT9XXDK_skvCmWMnTofmumHXtI")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "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")

# Бесплатные модели с поддержкой зрения на OpenRouter
VISION_MODELS = [
    "nvidia/nemotron-3.5-super-vl:free",
    "thinkingmachines/inkling-2-small:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-31b-it:free"
]

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ----------------- БАЗА ДАННЫХ (SQLite) -----------------
DB_PATH = os.path.join(os.path.dirname(__file__), "nutrition.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            calorie_goal INTEGER DEFAULT 2000
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date_str TEXT,
            time_str TEXT,
            title TEXT,
            weight_g INTEGER,
            calories REAL,
            protein REAL,
            fat REAL,
            carb REAL
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user_goal(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT calorie_goal FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (user_id, calorie_goal) VALUES (?, 2000)", (user_id,))
        conn.commit()
        goal = 2000
    else:
        goal = row[0]
    conn.close()
    return goal

def set_user_goal(user_id: int, goal: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO users (user_id, calorie_goal) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET calorie_goal = ?", (user_id, goal, goal))
    conn.commit()
    conn.close()

def add_meal(user_id: int, title: str, weight_g: int, calories: float, protein: float, fat: float, carb: float) -> int:
    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO meals (user_id, date_str, time_str, title, weight_g, calories, protein, fat, carb)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, date_str, time_str, title, weight_g, calories, protein, fat, carb))
    conn.commit()
    meal_id = cur.lastrowid
    conn.close()
    return meal_id

def get_today_stats(user_id: int):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, time_str, title, weight_g, calories, protein, fat, carb
        FROM meals
        WHERE user_id = ? AND date_str = ?
        ORDER BY id ASC
    """, (user_id, today))
    meals = cur.fetchall()
    conn.close()

    total_cals = sum(m[4] for m in meals)
    total_p = sum(m[5] for m in meals)
    total_f = sum(m[6] for m in meals)
    total_c = sum(m[7] for m in meals)
    return {
        "meals": meals,
        "total_calories": round(total_cals, 1),
        "total_protein": round(total_p, 1),
        "total_fat": round(total_f, 1),
        "total_carb": round(total_c, 1),
    }

def delete_last_meal(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, title, calories FROM meals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM meals WHERE id = ?", (row[0],))
        conn.commit()
    conn.close()
    return row

# ----------------- ИНТЕРФЕЙС И КЛАВИАТУРА -----------------
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("📊 Дневник за сегодня"), types.KeyboardButton("🎯 Моя цель"))
    markup.row(types.KeyboardButton("🗑 Удалить последнее"), types.KeyboardButton("ℹ️ Помощь"))
    return markup

def make_progress_bar(current: float, goal: int, length: int = 10) -> str:
    if goal <= 0:
        return "0%"
    percent = current / goal
    filled = int(min(1.0, max(0.0, percent)) * length)
    bar = "🟩" * filled + "⬜" * (length - filled)
    return f"{bar} {int(percent * 100)}%"

# ----------------- АНАЛИЗ ЕДЫ ЧЕРЕЗ OPENROUTER VISION -----------------
def analyze_food(image_bytes=None, text_desc=None) -> dict:
    prompt = """
Ты — профессиональный ИИ-диетолог и счётчик калорий (аналог приложения YAZIO).
Твоя задача — внимательно проанализировать изображение еды (или текст) и определить:
1. Название блюда или продуктов
2. Примерный вес порции в граммах (целое число)
3. Калории (ккал)
4. Белки (г)
5. Жиры (г)
6. Углеводы (г)
7. Короткий комментарий (1-2 предложения о пользе, балансе макросов или витаминах).

ОТВЕТ ВЕРНИ СТРОГО В ВИДЕ ВАЛИДНОГО JSON-ОБЪЕКТА (без пояснений, без markdown):
{
  "title": "Название блюда",
  "weight_g": 250,
  "calories": 420,
  "protein": 22.0,
  "fat": 14.5,
  "carb": 51.0,
  "comment": "Отличный баланс питательных веществ и белков."
}
"""

    user_content = []
    if image_bytes:
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}
        })
    if text_desc:
        user_content.append({
            "type": "text",
            "text": f"Описание от пользователя: {text_desc}"
        })
    user_content.append({
        "type": "text",
        "text": prompt
    })

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/glizzleglizzle/calorie-bot",
        "X-Title": "Calorie Tracker Bot"
    }

    last_error = None
    for model_name in VISION_MODELS:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.2
        }
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=35
            )
            if resp.status_code == 200:
                result = resp.json()
                raw_text = result["choices"][0]["message"]["content"].strip()

                # Извлекаем JSON из ответа
                match = re.search(r"\{.*\}", raw_text, re.DOTALL)
                if match:
                    json_str = match.group(0)
                else:
                    json_str = raw_text

                return json.loads(json_str)
            else:
                last_error = f"{model_name} HTTP {resp.status_code}: {resp.text}"
        except Exception as e:
            last_error = f"{model_name} error: {e}"

    raise RuntimeError(f"Не удалось получить ответ: {last_error}")

# ----------------- КОМАНДЫ БОТА -----------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    goal = get_user_goal(user_id)
    text = (
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "Я твой персональный <b>ИИ-трекер питания и калорий</b> (полный аналог YAZIO в Telegram).\n\n"
        "📸 <b>Как мной пользоваться:</b>\n"
        "• Просто <b>сфотографируй и отправь тарелку с едой</b> — я распознаю блюдо, посчитаю вес, калории и БЖУ.\n"
        "• Или напиши текстом: <i>«2 вареных яйца и кофе с молоком»</i>.\n\n"
        f"🎯 Твоя текущая дневная цель: <b>{goal} ккал</b> (можно изменить кнопкой «Моя цель»)."
    )
    bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())

@bot.message_handler(commands=["help"])
@bot.message_handler(func=lambda msg: msg.text == "ℹ️ Помощь")
def cmd_help(message):
    text = (
        "📖 <b>Инструкция по использованию:</b>\n\n"
        "1. <b>Фото блюда:</b> отправьте фото еды (можно с комментарием в подписи). Бот моментально распознает состав и запишет в дневник.\n"
        "2. <b>Текст:</b> можно просто написать, что вы съели.\n"
        "3. <b>Статистика:</b> нажмите «📊 Дневник за сегодня» или отправьте команду /today.\n"
        "4. <b>Цель:</b> нажмите «🎯 Моя цель» или отправьте /goal &lt;ккал&gt; (например <code>/goal 1800</code>).\n"
        "5. <b>Отмена:</b> если случайно добавили лишнее, нажмите «🗑 Удалить последнее»."
    )
    bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())

@bot.message_handler(commands=["today"])
@bot.message_handler(func=lambda msg: msg.text == "📊 Дневник за сегодня")
def cmd_today(message):
    user_id = message.from_user.id
    goal = get_user_goal(user_id)
    stats = get_today_stats(user_id)
    cals = stats["total_calories"]
    p = stats["total_protein"]
    f = stats["total_fat"]
    c = stats["total_carb"]
    meals = stats["meals"]

    bar = make_progress_bar(cals, goal)
    remain = max(0.0, goal - cals)

    msg = (
        f"📅 <b>Дневник за сегодня ({datetime.date.today().strftime('%d.%m.%Y')}):</b>\n\n"
        f"🔥 <b>Калории:</b> {int(cals)} / {goal} ккал\n"
        f"{bar}\n"
        f"Осталось: <b>{int(remain)} ккал</b>\n\n"
        f"📊 <b>Баланс БЖУ:</b>\n"
        f"🥩 Белки: <b>{p} г</b> | 🧈 Жиры: <b>{f} г</b> | 🍞 Углеводы: <b>{c} г</b>\n"
        "──────────────────\n"
    )

    if not meals:
        msg += "\n<i>Вы пока ничего не добавили сегодня. Отправьте фото еды!</i> 🍽"
    else:
        msg += "<b>Приёмы пищи:</b>\n"
        for m in meals:
            _, time_str, title, weight_g, meal_cals, _, _, _ = m
            msg += f"• <b>{time_str}</b> — {title} (~{weight_g}г): <b>{int(meal_cals)} ккал</b>\n"

    bot.send_message(message.chat.id, msg, reply_markup=get_main_keyboard())

@bot.message_handler(commands=["goal"])
@bot.message_handler(func=lambda msg: msg.text == "🎯 Моя цель")
def cmd_goal(message):
    user_id = message.from_user.id
    parts = message.text.split()
    if len(parts) == 2 and parts[1].isdigit():
        new_goal = int(parts[1])
        set_user_goal(user_id, new_goal)
        bot.send_message(message.chat.id, f"✅ Ваша новая цель успешно сохранена: <b>{new_goal} ккал/день</b>!", reply_markup=get_main_keyboard())
    else:
        current = get_user_goal(user_id)
        bot.send_message(
            message.chat.id,
            f"🎯 Ваша текущая цель: <b>{current} ккал/день</b>.\n\n"
            "Чтобы изменить, отправьте команду с числом, например:\n"
            "<code>/goal 1800</code>",
            reply_markup=get_main_keyboard()
        )

@bot.message_handler(commands=["undo"])
@bot.message_handler(func=lambda msg: msg.text == "🗑 Удалить последнее")
def cmd_undo(message):
    user_id = message.from_user.id
    deleted = delete_last_meal(user_id)
    if deleted:
        bot.send_message(message.chat.id, f"🗑 Запись <b>«{deleted[1]}»</b> ({int(deleted[2])} ккал) удалена из дневника.", reply_markup=get_main_keyboard())
    else:
        bot.send_message(message.chat.id, "Дневник пуст, нечего удалять.", reply_markup=get_main_keyboard())

# ----------------- ОБРАБОТКА ФОТО И ТЕКСТА -----------------
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    status_msg = bot.send_message(chat_id, "🔍 <i>Анализирую фото блюда и рассчитываю КБЖУ...</i>")

    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        image_bytes = bot.download_file(file_info.file_path)

        caption = message.caption if message.caption else None
        data = analyze_food(image_bytes=image_bytes, text_desc=caption)

        title = data.get("title", "Блюдо")
        weight_g = int(data.get("weight_g", 200))
        calories = float(data.get("calories", 0))
        protein = float(data.get("protein", 0))
        fat = float(data.get("fat", 0))
        carb = float(data.get("carb", 0))
        comment = data.get("comment", "")

        add_meal(user_id, title, weight_g, calories, protein, fat, carb)

        goal = get_user_goal(user_id)
        stats = get_today_stats(user_id)

        response = (
            f"🍽 <b>{title}</b> (~{weight_g} г)\n\n"
            f"🔥 <b>Калории:</b> {int(calories)} ккал\n"
            f"🥩 <b>Белки:</b> {protein} г\n"
            f"🧈 <b>Жиры:</b> {fat} г\n"
            f"🍞 <b>Углеводы:</b> {carb} г\n\n"
            f"💡 <i>{comment}</i>\n"
            "──────────────────\n"
            f"✅ <i>Добавлено в дневник!</i>\n"
            f"Всего за сегодня: <b>{int(stats['total_calories'])} / {goal} ккал</b>\n"
            f"{make_progress_bar(stats['total_calories'], goal)}"
        )
        bot.delete_message(chat_id, status_msg.message_id)
        bot.send_message(chat_id, response, reply_markup=get_main_keyboard())

    except Exception as e:
        bot.delete_message(chat_id, status_msg.message_id)
        bot.send_message(
            chat_id,
            f"⚠️ Ошибка при анализе фото: <i>{e}</i>\n\nПопробуйте ещё раз или опишите блюдо текстом.",
            reply_markup=get_main_keyboard()
        )

@bot.message_handler(func=lambda msg: not msg.text.startswith("/"))
def handle_text(message):
    text = message.text.strip()
    if text in ["📊 Дневник за сегодня", "🎯 Моя цель", "🗑 Удалить последнее", "ℹ️ Помощь"]:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    status_msg = bot.send_message(chat_id, "🔍 <i>Оцениваю калорийность блюда...</i>")

    try:
        data = analyze_food(text_desc=text)
        title = data.get("title", text)
        weight_g = int(data.get("weight_g", 200))
        calories = float(data.get("calories", 0))
        protein = float(data.get("protein", 0))
        fat = float(data.get("fat", 0))
        carb = float(data.get("carb", 0))
        comment = data.get("comment", "")

        add_meal(user_id, title, weight_g, calories, protein, fat, carb)

        goal = get_user_goal(user_id)
        stats = get_today_stats(user_id)

        response = (
            f"🍽 <b>{title}</b> (~{weight_g} г)\n\n"
            f"🔥 <b>Калории:</b> {int(calories)} ккал\n"
            f"🥩 <b>Белки:</b> {protein} г\n"
            f"🧈 <b>Жиры:</b> {fat} г\n"
            f"🍞 <b>Углеводы:</b> {carb} г\n\n"
            f"💡 <i>{comment}</i>\n"
            "──────────────────\n"
            f"✅ <i>Добавлено в дневник!</i>\n"
            f"Всего за сегодня: <b>{int(stats['total_calories'])} / {goal} ккал</b>\n"
            f"{make_progress_bar(stats['total_calories'], goal)}"
        )
        bot.delete_message(chat_id, status_msg.message_id)
        bot.send_message(chat_id, response, reply_markup=get_main_keyboard())

    except Exception as e:
        bot.delete_message(chat_id, status_msg.message_id)
        bot.send_message(
            chat_id,
            f"⚠️ Не удалось оценить блюдо: <i>{e}</i>",
            reply_markup=get_main_keyboard()
        )

# ----------------- СЕРВЕР ЗДОРОВЬЯ ДЛЯ БЕСПЛАТНОГО ХОСТИНГА (RENDER) -----------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Calorie Tracker Bot is running healthy 24/7!")

    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

if __name__ == "__main__":
    print("Запуск HealthCheck сервера на порту...")
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    print("Бот успешно запущен в режиме Long Polling...")
    bot.infinity_polling(timeout=20, long_polling_timeout=15)
