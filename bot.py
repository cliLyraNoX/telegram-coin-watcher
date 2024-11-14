import os
import sqlite3
import asyncio
import aiohttp
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes, 
    MessageHandler, 
    filters
)

# ---------------------------
# Konfiguration und Setup
# ---------------------------

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 3600))
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"
NEWS_API_URL = "https://cryptopanic.com/api/v1/posts/"

# Logging konfigurieren
logging.basicConfig(
    filename='bot_usage.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Datenbankverbindung und Initialisierung
def init_db():
    conn = sqlite3.connect("coins.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS coins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id TEXT UNIQUE NOT NULL,
            threshold REAL NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            price REAL NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE NOT NULL,
            is_admin INTEGER DEFAULT 0
        )
    """)
    # Standardkonfiguration setzen
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('RUN_THRESHOLD_PERCENT', '10')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('RUN_CONSECUTIVE_PERIODS', '5')")
    # Admin-Benutzer hinzufügen
    cursor.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, ?)", (ADMIN_CHAT_ID, 1))
    conn.commit()
    return conn

conn = init_db()

# ---------------------------
# Klassen zur Handhabung
# ---------------------------

class NotificationManager:
    def __init__(self, bot, chat_id):
        self.bot = bot
        self.chat_id = chat_id

    async def send_notification(self, message: str):
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=message)
            logging.info(f"Notification sent: {message}")
        except Exception as e:
            logging.error(f"Failed to send notification: {e}")

class NewsManager:
    def __init__(self, bot, chat_id):
        self.bot = bot
        self.chat_id = chat_id

    async def fetch_latest_news(self, session: aiohttp.ClientSession, coin_ids: list):
        params = {
            'auth_token': NEWS_API_KEY,
            'currencies': ','.join([coin.upper() for coin in coin_ids]),
            'kind': 'news'
        }
        try:
            async with session.get(NEWS_API_URL, params=params) as response:
                data = await response.json()
                return data.get('results', [])
        except Exception as e:
            logging.error(f"Fehler beim Abrufen der Nachrichten: {e}")
            return []

    async def send_news_notifications(self, news_items: list):
        for item in news_items:
            message = f"🔔 Neue Nachricht: {item.get('title')}\n{item.get('url')}"
            await self.send_notification(message)

    async def send_notification(self, message: str):
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=message)
            logging.info(f"Nachricht gesendet: {message}")
        except Exception as e:
            logging.error(f"Fehler beim Senden der Nachricht: {e}")

class CoinWatcher:
    def __init__(self, bot, chat_id):
        self.notification_manager = NotificationManager(bot, chat_id)
        self.news_manager = NewsManager(bot, chat_id)

    def add_coin(self, coin_id: str, threshold: float):
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO coins (coin_id, threshold) VALUES (?, ?)", 
                (coin_id, threshold)
            )
            conn.commit()
            asyncio.create_task(
                self.notification_manager.send_notification(
                    f"{coin_id.capitalize()} zur Überwachung hinzugefügt mit Schwelle {threshold}%.")
            )
            logging.info(f"Coin added: {coin_id}, Threshold: {threshold}")
        except Exception as e:
            logging.error(f"Error adding coin: {e}")

    def remove_coin(self, coin_id: str):
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM coins WHERE coin_id = ?", (coin_id,))
            conn.commit()
            asyncio.create_task(
                self.notification_manager.send_notification(
                    f"{coin_id.capitalize()} aus der Überwachung entfernt.")
            )
            logging.info(f"Coin removed: {coin_id}")
        except Exception as e:
            logging.error(f"Error removing coin: {e}")

    def get_coins(self):
        cursor = conn.cursor()
        cursor.execute("SELECT coin_id, threshold FROM coins")
        return cursor.fetchall()

    def log_price(self, coin_id: str, price: float):
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO price_history (coin_id, price) VALUES (?, ?)", 
                (coin_id, price)
            )
            conn.commit()
        except Exception as e:
            logging.error(f"Fehler beim Speichern des Preises: {e}")

    def get_config(self, key: str):
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        result = cursor.fetchone()
        return float(result[0]) if result else None

    def set_config(self, key: str, value: str):
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE config SET value = ? WHERE key = ?", (value, key))
            conn.commit()
            logging.info(f"Configuration updated: {key} = {value}")
        except Exception as e:
            logging.error(f"Error setting config {key}: {e}")

    async def check_run(self, coin_id: str):
        try:
            cursor = conn.cursor()
            run_consecutive_periods = self.get_config('RUN_CONSECUTIVE_PERIODS') or 5
            run_threshold_percent = self.get_config('RUN_THRESHOLD_PERCENT') or 10
            cursor.execute("""
                SELECT price FROM price_history 
                WHERE coin_id = ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (coin_id, run_consecutive_periods))
            prices = [row[0] for row in cursor.fetchall()]
            if len(prices) < run_consecutive_periods:
                return
            # Überprüfen, ob jeder Preis höher ist als der vorherige
            is_run = all(x < y for x, y in zip(prices[::-1], prices[::-1][1:]))
            if is_run:
                total_change = (prices[0] - prices[-1]) / prices[-1] * 100
                if total_change >= run_threshold_percent:
                    message = f"🚀 {coin_id.capitalize()} hat einen Run von {total_change:.2f}% über die letzten {run_consecutive_periods} Intervalle erreicht!"
                    await self.notification_manager.send_notification(message)
                    logging.info(f"Run erkannt für {coin_id}: {total_change}%")
        except Exception as e:
            logging.error(f"Fehler bei der Run-Erkennung für {coin_id}: {e}")

    async def check_price_increase_async(self):
        coins = self.get_coins()
        if not coins:
            logging.info("Keine Coins zur Überwachung.")
            return

        params = {
            "ids": ",".join([coin[0] for coin in coins]),
            "vs_currencies": "eur",  # Vergleich in EUR
            "include_24hr_change": "true",
            "include_current_price": "true"
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(COINGECKO_API_URL, params=params) as response:
                    response_json = await response.json()
                for coin_id, threshold in coins:
                    coin_data = response_json.get(coin_id)
                    if coin_data and "eur_24h_change" in coin_data:
                        price_change = coin_data["eur_24h_change"]
                        current_price = coin_data.get("eur")
                        if current_price:
                            self.log_price(coin_id, current_price)
                        if price_change >= threshold:
                            message = f"{coin_id.capitalize()} ist um {price_change:.2f}% gestiegen!"
                            await self.notification_manager.send_notification(message)
                            logging.info(f"Preissteigerung erkannt für {coin_id}: {price_change}%")
                        # Run-Erkennung aufrufen
                        await self.check_run(coin_id)
            except Exception as e:
                logging.error(f"Fehler beim asynchronen Abrufen der Preisdaten: {e}")
                # Fehlerbenachrichtigung an Admin
                await self.notification_manager.send_notification(f"Fehler beim Abrufen der Preisdaten: {e}")

    async def check_news_async(self):
        coins = self.get_coins()
        if not coins:
            logging.info("Keine Coins zur Überwachung für Nachrichten.")
            return
        coin_ids = [coin[0] for coin in coins]
        async with aiohttp.ClientSession() as session:
            news_items = await self.news_manager.fetch_latest_news(session, coin_ids)
            if news_items:
                await self.news_manager.send_news_notifications(news_items)
                logging.info(f"{len(news_items)} neue Nachrichten gesendet.")

    async def start_watching(self):
        while True:
            await self.check_price_increase_async()
            await self.check_news_async()
            await asyncio.sleep(CHECK_INTERVAL)

# ---------------------------
# Helper-Funktionen
# ---------------------------

def is_admin(user_id: str) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] == 1 if result else False

async def register_user(user_id: str):
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, ?)", (user_id, 0))
    conn.commit()

# ---------------------------
# Bot-Kommandos und Handler
# ---------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    await register_user(user_id)
    if is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton("Coin hinzufügen", callback_data='add_coin')],
            [InlineKeyboardButton("Coin entfernen", callback_data='remove_coin')],
            [InlineKeyboardButton("Überwachte Coins anzeigen", callback_data='list_coins')],
            [InlineKeyboardButton("Run-Konfiguration", callback_data='config_run')],
            [InlineKeyboardButton("Broadcast Nachricht", callback_data='broadcast')]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("Überwachte Coins anzeigen", callback_data='list_coins')]
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Willkommen zum CoinWatcher Bot! Wähle eine Option:", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if not is_admin(user_id):
        await query.edit_message_text(text="Du hast keine Berechtigung, diese Aktion durchzuführen.")
        return

    if query.data == 'add_coin':
        await query.edit_message_text(text="Bitte sende den Coin-ID und die Schwelle in folgendem Format: `coin_id,schwelle`.\nBeispiel: `bitcoin,5`")
        context.user_data['awaiting_input'] = 'add_coin'

    elif query.data == 'remove_coin':
        await query.edit_message_text(text="Bitte sende den Coin-ID zum Entfernen.\nBeispiel: `bitcoin`")
        context.user_data['awaiting_input'] = 'remove_coin'

    elif query.data == 'list_coins':
        await list_coins(update, context)

    elif query.data == 'config_run':
        keyboard = [
            [InlineKeyboardButton("RUN_THRESHOLD_PERCENT ändern", callback_data='set_run_threshold')],
            [InlineKeyboardButton("RUN_CONSECUTIVE_PERIODS ändern", callback_data='set_run_periods')],
            [InlineKeyboardButton("Zurück zum Hauptmenü", callback_data='main_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text="Run-Konfiguration wählen:", reply_markup=reply_markup)

    elif query.data == 'set_run_threshold':
        await query.edit_message_text(text="Bitte sende den neuen `RUN_THRESHOLD_PERCENT`.\nBeispiel: `15`")
        context.user_data['awaiting_input'] = 'set_run_threshold'

    elif query.data == 'set_run_periods':
        await query.edit_message_text(text="Bitte sende den neuen `RUN_CONSECUTIVE_PERIODS`.\nBeispiel: `7`")
        context.user_data['awaiting_input'] = 'set_run_periods'

    elif query.data == 'main_menu':
        keyboard = [
            [InlineKeyboardButton("Coin hinzufügen", callback_data='add_coin')],
            [InlineKeyboardButton("Coin entfernen", callback_data='remove_coin')],
            [InlineKeyboardButton("Überwachte Coins anzeigen", callback_data='list_coins')],
            [InlineKeyboardButton("Run-Konfiguration", callback_data='config_run')],
            [InlineKeyboardButton("Broadcast Nachricht", callback_data='broadcast')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text="Willkommen zum CoinWatcher Bot! Wähle eine Option:", reply_markup=reply_markup)

    elif query.data == 'broadcast':
        await query.edit_message_text(text="Bitte sende die Nachricht, die du an alle Nicht-Admin-Nutzer senden möchtest.")
        context.user_data['awaiting_input'] = 'broadcast'

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    await register_user(user_id)

    if is_admin(user_id):
        awaiting = context.user_data.get('awaiting_input')
        if awaiting == 'add_coin':
            try:
                text = update.message.text.strip()
                coin_id, threshold = text.split(',')
                coin_id = coin_id.strip().lower()
                threshold = float(threshold.strip())
                context.application.watcher.add_coin(coin_id, threshold)
                await update.message.reply_text(f"{coin_id.capitalize()} wird jetzt mit einer Schwelle von {threshold}% überwacht.")
            except (IndexError, ValueError):
                await update.message.reply_text("Fehlerhaftes Format. Verwende: `coin_id,schwelle`.\nBeispiel: `bitcoin,5`")
            finally:
                context.user_data['awaiting_input'] = None

        elif awaiting == 'remove_coin':
            try:
                coin_id = update.message.text.strip().lower()
                context.application.watcher.remove_coin(coin_id)
                await update.message.reply_text(f"{coin_id.capitalize()} wurde aus der Überwachung entfernt.")
            except Exception as e:
                await update.message.reply_text(f"Fehler beim Entfernen des Coins: {e}")
            finally:
                context.user_data['awaiting_input'] = None

        elif awaiting == 'set_run_threshold':
            try:
                threshold = float(update.message.text.strip())
                if threshold <= 0:
                    raise ValueError
                context.application.watcher.set_config('RUN_THRESHOLD_PERCENT', str(threshold))
                await update.message.reply_text(f"`RUN_THRESHOLD_PERCENT` wurde auf {threshold}% gesetzt.")
            except ValueError:
                await update.message.reply_text("Ungültiger Wert. Bitte sende eine positive Zahl.\nBeispiel: `15`")
            finally:
                context.user_data['awaiting_input'] = None

        elif awaiting == 'set_run_periods':
            try:
                periods = int(update.message.text.strip())
                if periods <= 0:
                    raise ValueError
                context.application.watcher.set_config('RUN_CONSECUTIVE_PERIODS', str(periods))
                await update.message.reply_text(f"`RUN_CONSECUTIVE_PERIODS` wurde auf {periods} gesetzt.")
            except ValueError:
                await update.message.reply_text("Ungültiger Wert. Bitte sende eine positive ganze Zahl.\nBeispiel: `7`")
            finally:
                context.user_data['awaiting_input'] = None

        elif awaiting == 'broadcast':
            try:
                message = update.message.text.strip()
                if not message:
                    raise ValueError("Leere Nachricht.")
                # Abrufen aller Nicht-Admin-Nutzer
                cursor = conn.cursor()
                cursor.execute("SELECT user_id FROM users WHERE is_admin = 0")
                users = cursor.fetchall()
                if not users:
                    await update.message.reply_text("Es gibt keine Nicht-Admin-Nutzer, an die eine Nachricht gesendet werden kann.")
                    return
                for user in users:
                    try:
                        await context.application.bot.send_message(chat_id=user[0], text=message)
                    except Exception as e:
                        logging.error(f"Fehler beim Senden der Broadcast-Nachricht an {user[0]}: {e}")
                await update.message.reply_text("Broadcast-Nachricht erfolgreich gesendet.")
            except Exception as e:
                await update.message.reply_text(f"Fehler beim Senden der Broadcast-Nachricht: {e}")
            finally:
                context.user_data['awaiting_input'] = None
    else:
        # Nicht-Admin-Nutzer können nur die Liste der überwachten Coins anzeigen
        if context.user_data.get('awaiting_input') == None:
            await update.message.reply_text("Nutze /start, um das Hauptmenü aufzurufen.")
        else:
            await update.message.reply_text("Du hast keine Berechtigung, diese Aktion durchzuführen.")

async def list_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins = context.application.watcher.get_coins()
    if coins:
        message = "Überwachte Coins:\n" + "\n".join(
            f"- {coin.capitalize()} (Schwelle: {threshold}%)" for coin, threshold in coins
        )
    else:
        message = "Es werden derzeit keine Coins überwacht."
    await update.callback_query.message.reply_text(message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    if is_admin(user_id):
        message = (
            "📋 **Hilfe-Menü für Admins**\n\n"
            "/start - Zeigt das Hauptmenü an.\n"
            "/help - Zeigt dieses Hilfemenü an.\n\n"
            "Im Hauptmenü stehen folgende Optionen zur Verfügung:\n"
            "• Coin hinzufügen - Fügt eine neue Kryptowährung zur Überwachung hinzu.\n"
            "• Coin entfernen - Entfernt eine Kryptowährung aus der Überwachung.\n"
            "• Überwachte Coins anzeigen - Listet alle derzeit überwachten Kryptowährungen auf.\n"
            "• Run-Konfiguration - Ermöglicht das Ändern der Run-Parameter.\n"
            "• Broadcast Nachricht - Sendet eine Nachricht an alle Nicht-Admin-Nutzer.\n"
        )
    else:
        message = (
            "📋 **Hilfe-Menü**\n\n"
            "/start - Zeigt das Hauptmenü an.\n"
            "/help - Zeigt dieses Hilfemenü an.\n\n"
            "Im Hauptmenü steht folgende Option zur Verfügung:\n"
            "• Überwachte Coins anzeigen - Listet alle derzeit überwachten Kryptowährungen auf.\n"
        )
    await update.message.reply_text(message, parse_mode='Markdown')

# ---------------------------
# Bot-Initialisierung und Start
# ---------------------------

async def main():
    # Bot und Watcher initialisieren
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    bot = application.bot
    watcher = CoinWatcher(bot, ADMIN_CHAT_ID)
    application.watcher = watcher  # Watcher zur Application hinzufügen

    # Handler hinzufügen
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Hintergrund-Task für die Überwachung starten
    asyncio.create_task(watcher.start_watching())

    # Bot starten
    await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot wurde gestoppt.")
