# modules/telegram_integration.py
import telebot # pyTelegramBotAPI
from telebot.util import smart_split
from modules.logger import logger
from modules.config_manager import ConfigManager
import threading
import time
import queue
from decimal import Decimal
from datetime import datetime, timezone
import requests
import logging
import pandas as pd

class TelegramBotHandler:
    """
    Клас для обробки взаємодії з Telegram ботом.
    Використовує черги для комунікації з BotCore.
    """
    def __init__(self, command_queue: queue.Queue, event_queue: queue.Queue):
        """
        Ініціалізація обробника Telegram.
        Args:
            command_queue (queue.Queue): Черга для надсилання команд до BotCore.
            event_queue (queue.Queue): Черга для отримання подій/сповіщень від BotCore.
        """
        self.command_queue = command_queue
        self.event_queue = event_queue
        self.config_manager = ConfigManager()
        bot_config = {}
        try:
            bot_config = self.config_manager.get_bot_config()
        except Exception as e:
            logger.error(f"TG: Не вдалося завантажити конфігурацію: {e}")

        self.is_enabled = bot_config.get('telegram_enabled', False)
        self.bot_token = bot_config.get('telegram_token')
        chat_id_str = bot_config.get('telegram_chat_id')
        self.authorized_chat_id = None
        if chat_id_str:
            try:
                self.authorized_chat_id = int(chat_id_str)
            except (ValueError, TypeError):
                logger.error(f"TG: Некоректний Chat ID: '{chat_id_str}'.")

        self.bot: telebot.TeleBot | None = None
        self.polling_thread: threading.Thread | None = None
        self.event_listener_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Атрибути для форматування
        self.quote_asset = 'USDT'
        self.base_asset = 'BASE'
        self.price_precision = 4
        self.qty_precision = 8
        try:
            symbol = bot_config.get('trading_symbol', 'BTCUSDT')
            if 'USDT' in symbol:
                self.quote_asset = 'USDT'
                self.base_asset = symbol.replace('USDT','')
            elif 'BUSD' in symbol:
                self.quote_asset = 'BUSD'
                self.base_asset = symbol.replace('BUSD','')
            elif 'BTC' in symbol:
                self.quote_asset = 'BTC'
                self.base_asset = symbol.replace('BTC','')
            elif 'ETH' in symbol:
                self.quote_asset = 'ETH'
                self.base_asset = symbol.replace('ETH','')
            # TODO: Отримувати точність з BotCore
        except Exception:
            # Залишаємо дефолтні значення у разі помилки
            pass

        if not self.is_enabled:
            logger.info("TG: Інтеграцію вимкнено.")
            return
        if not self.bot_token or not self.authorized_chat_id:
            logger.warning("TG: Токен або Chat ID не налаштовано.")
            return

        logger.info(f"TG: Ініціалізація бота. Chat ID: {self.authorized_chat_id}")
        try:
            self.bot = telebot.TeleBot(self.bot_token, threaded=False, parse_mode='HTML')
            bot_info = self.bot.get_me()
            logger.info(f"TG: Успішно підключено до бота @{bot_info.username}")
            self._setup_handlers()
            self.start_listening()
        except telebot.apihelper.ApiTelegramException as e:
            logger.error(f"TG: Помилка API Telegram (токен?): {e}")
            self.bot = None
        except Exception as e:
            logger.error(f"TG: Помилка ініціалізації TeleBot: {e}.")
            self.bot = None

    def _is_authorized(self, message) -> bool:
        """Перевіряє авторизацію чату."""
        if message.text and message.text.startswith(('/start', '/help')):
            return True
        if message.chat.id == self.authorized_chat_id:
            return True
        else:
            logger.warning(f"TG: Неавторизований доступ: chat_id={message.chat.id} (@{message.from_user.username})")
            self._safe_reply(message, "❌ Не авторизовано.")
            return False

    def _safe_reply(self, message, text):
        """Безпечна відповідь на повідомлення."""
        if not self.bot:
            return
        try:
            self.bot.reply_to(message, text, parse_mode='HTML', disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"TG Помилка відповіді: {e}")

    def _put_command(self, command: dict, message):
        """Кладе команду в чергу та відповідає користувачу."""
        reply_text = "⏳ Команду прийнято..." if command.get('type') not in ['START_CMD', 'HELP_CMD'] else None
        try:
            self.command_queue.put_nowait(command)
            logger.info(f"TG: Команду {command.get('type')} додано.")
            if reply_text:
                self._safe_reply(message, reply_text)
        except queue.Full:
            logger.error("TG: Черга команд переповнена!")
            self._safe_reply(message, "❌ Помилка: Сервер перевантажений.")
        except Exception as e:
            logger.error(f"TG: Помилка надсилання команди '{command.get('type')}': {e}", exc_info=True)
            self._safe_reply(message, f"❌ Помилка: {e}")

    def _setup_handlers(self):
        """Налаштовує обробники команд Telegram."""
        if not self.bot:
            return

        @self.bot.message_handler(commands=['start', 'help'])
        def send_welcome(message):
            is_auth = message.chat.id == self.authorized_chat_id
            logger.info(f"TG /start від chat_id: {message.chat.id} (Auth: {is_auth})")
            help_text = ( "<b>💎 Торговий бот Gemini</b>\n\n"
                          "<u>Доступні команди (власнику):</u>\n"
                          "📊 /status - Стан\n"
                          "💰 /balance - Баланс\n"
                          "📈 /position - Позиція\n"
                          "💾 /history [N] - Ост. N угод (def:5)\n"
                          "📊 /stats - Статистика сесії\n"
                          "❌ /close - Закрити позицію\n"
                          "⚙️ /reload_settings - Перезав. налаштування\n"
                          "▶️ /start_bot - Запустити ядро\n"
                          "⏹️ /stop_bot - Зупинити ядро\n"
                          "/help - Допомога" )
            self._safe_reply(message, help_text)

        @self.bot.message_handler(commands=['status'])
        def get_status(message):
            if not self._is_authorized(message):
                 return
            logger.info(f"TG /status")
            self._put_command({'type': 'GET_STATUS'}, message)

        @self.bot.message_handler(commands=['balance'])
        def get_balance(message):
             if not self._is_authorized(message):
                 return
             logger.info(f"TG /balance")
             self._put_command({'type': 'GET_BALANCE'}, message)

        @self.bot.message_handler(commands=['position'])
        def get_position(message):
            if not self._is_authorized(message):
                 return
            logger.info(f"TG /position")
            self._put_command({'type': 'GET_POSITION'}, message)

        @self.bot.message_handler(commands=['history'])
        def get_history(message):
            if not self._is_authorized(message):
                 return
            logger.info(f"TG /history")
            num_trades = 5
            try:
                args = message.text.split()
                if len(args) > 1:
                    num = int(args[1])
                    num_trades = max(1, min(num, 50))
            except (ValueError, IndexError):
                logger.debug(f"TG /history: Некор. аргумент, використано {num_trades}")
                pass
            self._put_command({'type': 'GET_HISTORY', 'payload': {'count': num_trades}}, message)

        @self.bot.message_handler(commands=['stats'])
        def get_stats(message):
            if not self._is_authorized(message):
                 return
            logger.info(f"TG /stats")
            self._put_command({'type': 'GET_STATS'}, message)

        @self.bot.message_handler(commands=['close'])
        def close_position_cmd(message):
            if not self._is_authorized(message):
                 return
            logger.info(f"TG /close")
            self._put_command({'type': 'CLOSE_POSITION', 'payload': {}}, message)

        @self.bot.message_handler(commands=['stop_bot'])
        def stop_bot_cmd(message):
              if not self._is_authorized(message):
                  return
              logger.info(f"TG /stop_bot")
              self._put_command({'type': 'STOP'}, message)

        @self.bot.message_handler(commands=['start_bot'])
        def start_bot_cmd(message):
            if not self._is_authorized(message):
                 return
            logger.info(f"TG /start_bot")
            self._put_command({'type': 'START'}, message)

        @self.bot.message_handler(commands=['reload_settings'])
        def reload_settings_cmd(message):
            if not self._is_authorized(message):
                 return
            logger.info(f"TG /reload_settings")
            self._put_command({'type': 'RELOAD_SETTINGS'}, message)

        @self.bot.message_handler(func=lambda message: True)
        def handle_unknown(message):
               if not self._is_authorized(message):
                   return
               logger.debug(f"TG Невідома команда: {message.text}")
               self._safe_reply(message, "Невідома команда. /help")


    def send_message(self, text: str, **kwargs):
        """Надсилає повідомлення авторизованому користувачу."""
        if not self.bot or not self.authorized_chat_id:
            if self.is_enabled:
                logger.warning(f"TG бот не ініціалізовано, не надіслано: {text[:100]}...")
            return
        try:
            for chunk in smart_split(text, chars_per_string=4096):
                self.bot.send_message(self.authorized_chat_id, chunk, parse_mode='HTML', disable_web_page_preview=True, **kwargs)
            logger.debug(f"TG -> {text[:100]}...")
        except telebot.apihelper.ApiTelegramException as e:
            logger.error(f"TG API Помилка надсилання ({self.authorized_chat_id}): {e}")
        except Exception as e:
            logger.error(f"TG Загальна помилка надсилання ({self.authorized_chat_id}): {e}")

    def start_listening(self):
        """Запускає потоки для polling та event listener."""
        if not self.bot:
            logger.warning("TG: Бот не ініціалізовано.")
            return
        # Зупиняємо попередні
        self.stop()
        self._stop_event.clear()
        # Запуск Polling
        self.polling_thread = threading.Thread(target=self._polling_loop, daemon=True, name="TelegramPolling")
        self.polling_thread.start()
        logger.info("TG Polling запущено.")
        # Запуск Event Listener
        self.event_listener_thread = threading.Thread(target=self._event_listener_loop, daemon=True, name="TelegramEventListener")
        self.event_listener_thread.start()
        logger.info("TG Event listener запущено.")

    def _polling_loop(self):
        """Цикл polling з обробкою помилок."""
        logger.info("TG Polling loop starting...")
        retries = 0
        max_retries = 10
        # Початкова затримка перед перепідключенням
        self.reconnect_delay = 5 # Додано для ясності
        while not self._stop_event.is_set():
            if not self.bot:
                logger.error("TG Бот відсутній, зупинка polling.")
                break
            try:
                logger.debug("TG Polling...")
                # Виклик polling в окремому блоці
                self.bot.polling(none_stop=False, interval=1, timeout=20)
                # Якщо polling завершився без винятку, але зупинка не була сигналізована
                if not self._stop_event.is_set():
                    logger.warning("TG Polling завершився несподівано. Пауза...")
                    time.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(self.reconnect_delay * 1.5, 60) # Збільшуємо затримку
                    retries = 0 # Скидаємо спроби після успішного polling (навіть якщо він перервався)

            except telebot.apihelper.ApiException as e:
                logger.error(f"TG API polling помилка: {e}")
                if "Unauthorized" in str(e):
                    logger.critical("TG Токен невалідний! Зупинка.")
                    self.bot = None
                    break
                retries += 1
                wait = min(60, 2 ** retries)
                logger.info(f"TG Пауза {wait}s (спроба {retries}/{max_retries})...")
                if retries >= max_retries:
                    logger.critical("TG Перевищено ліміт спроб polling.")
                    break
                self._stop_event.wait(wait)

            except requests.exceptions.RequestException as e:
                logger.error(f"TG Помилка з'єднання polling: {e}")
                retries += 1
                wait = min(60, 2 ** retries)
                logger.info(f"TG Пауза {wait}s (спроба {retries}/{max_retries})...")
                if retries >= max_retries:
                    logger.critical("TG Перевищено ліміт спроб polling.")
                    break
                self._stop_event.wait(wait)

            except Exception as e:
                logger.error(f"TG Критична помилка polling: {e}", exc_info=True)
                retries += 1
                wait = min(60, 2 ** retries)
                logger.info(f"TG Пауза {wait}s (спроба {retries}/{max_retries})...")
                if retries >= max_retries:
                    logger.critical("TG Перевищено ліміт спроб polling.")
                    break
                self._stop_event.wait(wait)
        logger.info("TG Polling loop завершено.")


    def _event_listener_loop(self):
        """Цикл, що прослуховує чергу подій від BotCore."""
        logger.info("TG Event listener loop starting...")
        while not self._stop_event.is_set():
            try:
                 event = self.event_queue.get(timeout=1)
                 if event:
                      event_type = event.get('type')
                      payload = event.get('payload')
                      logger.debug(f"TG <~ Event: {event_type}")
                      msg = None
                      # Формуємо повідомлення
                      if event_type=='NOTIFICATION':
                          msg = f"🔔 {payload.get('message')}"
                      elif event_type=='STATUS_RESPONSE':
                          msg = f"📊 <b>Статус:</b>\n{self._format_status_message(payload)}"
                      elif event_type=='BALANCE_RESPONSE':
                          msg = f"💰 <b>Баланс:</b>\n{self._format_balance_message(payload)}"
                      elif event_type=='POSITION_RESPONSE':
                          msg = f"📈 <b>Позиція:</b>\n{self._format_position_message(payload)}"
                      elif event_type=='STATS_RESPONSE':
                          msg = f"📉 <b>Статистика:</b>\n{self._format_stats_message(payload)}"
                      elif event_type=='HISTORY_RESPONSE':
                          msg = f"💾 {self._format_history_message(payload)}"
                      else:
                          logger.warning(f"TG: Отримано невідомий тип події: {event_type}")

                      # Надсилаємо повідомлення
                      if msg:
                          self.send_message(msg)
                      # Позначаємо завдання як виконане
                      self.event_queue.task_done()
            except queue.Empty:
                 # Якщо черга порожня, продовжуємо цикл
                 continue
            except Exception as e:
                 logger.error(f"TG Помилка event_listener_loop: {e}", exc_info=True)
                 # Пауза при помилці
                 time.sleep(1)
        logger.info("TG Event listener loop завершено.")

    # --- Допоміжні функції форматування ---
    def _format_status_message(self, status: dict) -> str:
        if not status:
            return "N/A"
        running = "🟢Запущено" if status.get('is_running') else "🔴Зупинено"
        ws = "✔️Підкл." if status.get('websocket_connected') else "❌Відкл."
        pos = "Так" if status.get('position_active') else "Ні"
        parts = [
            f"Стан: {running} | Режим: {status.get('active_mode','?')}",
            f"Символ: {status.get('symbol','?')} | Стратегія: {status.get('strategy','?')}",
            f"WebSocket: {ws} | Позиція: {pos}"
        ]
        return "\n".join(parts)

    def _format_balance_message(self, balance: dict) -> str:
        if not balance:
            return "Баланси не завантажено."
        msg = ""
        quote = getattr(self,'quote_asset','USDT')
        base = getattr(self,'base_asset','BASE')
        for asset in [quote, base]:
            if asset in balance:
                try:
                    free = Decimal(str(balance[asset].get('free',0)))
                    locked = Decimal(str(balance[asset].get('locked',0)))
                    msg += f"<b>{asset}:</b> Free={free:f} Locked={locked:f}\n"
                except Exception as e:
                    logger.warning(f"TG: Помилка форматування балансу {asset}: {e}")
                    msg += f"<b>{asset}:</b> Помилка\n"
        other_assets = []
        for asset, data in balance.items():
             if asset not in [quote, base]:
                 try:
                     free = Decimal(str(data.get('free',0)))
                     locked = Decimal(str(data.get('locked',0)))
                     total = free + locked
                     if total > 1e-9:
                         other_assets.append(f"<b>{asset}:</b> Free={free:f} Locked={locked:f}")
                 except Exception as e:
                     logger.warning(f"TG: Помилка форматування балансу {asset}: {e}")
        if other_assets:
            msg += "\n<i>Інші (>0):</i>\n"
            msg += "\n".join(other_assets)
        msg = msg.replace('.00000000', '').replace(' 0 ', ' 0.0 ')
        if not msg:
            return "Не знайдено балансів."
        else:
            return msg

    def _format_position_message(self, position: dict | None) -> str:
        if not position:
            return "Відкритих позицій немає."
        try:
            qty = Decimal(str(position.get('quantity',0)))
            entry_p = Decimal(str(position.get('entry_price',0)))
            entry_t = position.get('entry_time','N/A')
            entry_t_str = str(entry_t) # Default
            if isinstance(entry_t, datetime):
                 entry_t_str = entry_t.strftime('%Y-%m-%d %H:%M')
            elif isinstance(entry_t, str):
                 try:
                     entry_t_str = pd.to_datetime(entry_t,utc=True).strftime('%Y-%m-%d %H:%M')
                 except Exception:
                     # Залишаємо оригінальний рядок, якщо не вдалося розпарсити
                     entry_t_str = str(entry_t)

            price_prec = getattr(self,'price_precision',4)
            qty_prec = getattr(self,'qty_precision',8)
            return ( f"Символ: {position.get('symbol','?')}\n"
                     f"К-сть: {qty:.{qty_prec}f}\n"
                     f"Ціна входу: {entry_p:.{price_prec}f}\n"
                     f"Час входу: {entry_t_str}" )
        except Exception as e:
            logger.error(f"TG: Помилка форматування позиції: {e}")
            return "Помилка даних позиції."

    def _format_stats_message(self, stats: dict) -> str:
        if not stats:
            return "Статистика відсутня."
        try:
            total_pnl = float(stats.get('total_pnl',0.0))
            avg_profit = float(stats.get('avg_profit',0.0))
            avg_loss = float(stats.get('avg_loss',0.0))
            pf = stats.get('profit_factor',0.0)
            pf_str = "∞" if pf==float('inf') else f"{pf:.2f}"
            return (f"Тривалість: {stats.get('session_duration','N/A')}\n"
                    f"Угод: {stats.get('total_trades',0)} (W:{stats.get('winning_trades',0)} L:{stats.get('losing_trades',0)})\n"
                    f"Win Rate: {stats.get('win_rate',0.0):.2f}%\n"
                    f"Total PnL: {total_pnl:.4f}\n"
                    f"Avg Win/Loss: {avg_profit:.4f}/{avg_loss:.4f}\n"
                    f"Profit Factor: {pf_str}" )
        except Exception as e:
             logger.error(f"TG: Помилка форматування статистики: {e}")
             return "Помилка даних статистики."

    def _format_history_message(self, history_payload: dict) -> str:
        history = history_payload.get('trades',[])
        count = history_payload.get('count',len(history))
        if not history:
            return "Історія угод порожня."
        msg = f"<b>Останні {min(count, len(history))} угод:</b>\n"
        price_prec = getattr(self,'price_precision',4)
        qty_prec = getattr(self,'qty_precision',8)
        # Показуємо останні N угод
        for trade in history[-count:]:
             try:
                 side = trade.get('side')
                 side_emoji = "🟢" if side=='BUY' else ("🔴" if side=='SELL' else "⚪️")
                 price = Decimal(str(trade.get('price',0)))
                 qty = Decimal(str(trade.get('total_filled_qty',trade.get('quantity',0))))
                 pnl_val = trade.get('pnl')
                 pnl_str = ""
                 if pnl_val is not None:
                     try:
                         pnl_str = f" PnL:{Decimal(str(pnl_val)):.2f}"
                     except Exception:
                         pass # Ігноруємо помилку форматування PnL
                 time_val = trade.get('time')
                 time_str = "N/A"
                 if isinstance(time_val,datetime):
                     time_str = time_val.strftime('%H:%M:%S')
                 elif isinstance(time_val,str):
                     try:
                         time_str = pd.to_datetime(time_val,utc=True).strftime('%H:%M:%S')
                     except Exception:
                         pass # Ігноруємо помилку парсингу часу
                 purpose_val = trade.get('purpose')
                 purpose = ""
                 if purpose_val and purpose_val != 'UNKNOWN':
                     purpose = f"({purpose_val})"
                 msg += f"{side_emoji}{side} {qty:.{qty_prec}f}@{price:.{price_prec}f}{pnl_str}{purpose} ({time_str})\n"
             except Exception as e:
                 logger.error(f"TG: Помилка форматування угоди: {e}. Дані: {trade}")
                 msg += "<i>Помилка запису</i>\n"
        return msg

    def stop(self):
        """Зупиняє polling та listener."""
        logger.info("TG Сигнал на зупинку...")
        self._stop_event.set()
        if self.bot:
            try:
                self.bot.stop_polling()
                logger.info("TG stop_polling() викликано.")
            except Exception:
                # Ігноруємо помилки stop_polling, оскільки вони можуть виникати при вже зупиненому боті
                pass
        # Чекаємо завершення потоків
        if self.polling_thread and self.polling_thread.is_alive():
            logger.debug("TG Чекаємо polling thread...")
            self.polling_thread.join(timeout=1.5)
        if self.event_listener_thread and self.event_listener_thread.is_alive():
            logger.debug("TG Чекаємо event listener thread...")
            self.event_listener_thread.join(timeout=1.5)
        logger.info("TG Handler зупинено.")

# --- Приклад використання ---
if __name__ == '__main__':
    from dotenv import load_dotenv
    from modules.logger import setup_logger
    import queue
    import threading
    import logging

    load_dotenv()
    logger = setup_logger(level=logging.DEBUG, name="TelegramTestLogger")
    logger.info("Запуск прикладу TelegramBotHandler...")
    cmd_q = queue.Queue()
    evt_q = queue.Queue()
    telegram_bot = TelegramBotHandler(command_queue=cmd_q, event_queue=evt_q)
    if telegram_bot.bot:
        def simulator(): # Імітація BotCore
            time.sleep(5)
            evt_q.put({'type':'NOTIFICATION','payload':{'message':'Test Event 1'}})

            time.sleep(10)
            try:
                cmd = cmd_q.get(timeout=10)
                logger.info(f"Sim got: {cmd}")
                if cmd.get('type')=='GET_STATUS':
                    evt_q.put({'type':'STATUS_RESPONSE','payload':{'is_running':True,'active_mode':'TestNet','symbol':'SIMUSDT','strategy':'SimStrat','websocket_connected':True,'position_active':False}})
            except queue.Empty:
                pass

            time.sleep(5)
            try:
                cmd = cmd_q.get(timeout=10)
                logger.info(f"Sim got: {cmd}")
                if cmd.get('type')=='GET_BALANCE':
                    evt_q.put({'type':'BALANCE_RESPONSE','payload':{'USDT':{'free':'1000.50','locked':'1.0'},'SIM':{'free':'10000','locked':'0'}}})
            except queue.Empty:
                pass

            time.sleep(5)
            try:
                cmd = cmd_q.get(timeout=10)
                logger.info(f"Sim got: {cmd}")
                if cmd.get('type')=='GET_HISTORY':
                    count = cmd.get('payload',{}).get('count', 1)
                    evt_q.put({'type':'HISTORY_RESPONSE','payload':{'count':count, 'trades':[{'time':datetime.now(timezone.utc).isoformat(), 'symbol':'SIMUSDT', 'side':'BUY', 'price':'1', 'total_filled_qty':'10', 'purpose': 'ENTRY'}]*count }})
            except queue.Empty:
                pass
            logger.info("Симулятор завершив надсилання подій.")

        threading.Thread(target=simulator,daemon=True).start()
        try:
            logger.info("TG бот слухає... Ctrl+C для зупинки.")
            while True:
                time.sleep(1) # Тримаємо основний потік живим
        except KeyboardInterrupt:
            logger.info("Ctrl+C")
        finally:
            telegram_bot.stop()
    else:
        logger.warning("TG бот не ініціалізовано.")
    logger.info("Приклад TG Handler завершено.")
