# File: modules/binance_integration.py

# BLOCK 1: Imports and Custom Error Class
import os
import hmac
import hashlib
# --- ЗМІНА: Використовуємо aiohttp замість requests ---
import aiohttp
import asyncio
from aiohttp import ClientTimeout, WSMsgType # Додано WSMsgType
# --- КІНЕЦЬ ЗМІНИ ---
import json
import time
import pandas as pd
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, urljoin
from modules.logger import logger # Використовуємо наш основний логер
# --- ЗМІНА: threading більше не потрібен, websocket видалено ---
# import threading
# import websocket
# --- КІНЕЦЬ ЗМІНИ ---
from decimal import Decimal, InvalidOperation
import logging # Імпортуємо logging для рівнів
from typing import Any, Dict, List, Optional, Union, Callable # Додано Callable

# --- Спеціальний клас помилок (залишається без змін) ---
class BinanceAPIError(Exception):
    """Спеціальний клас помилок для BinanceAPI."""
    def __init__(self, status_code, error_data):
        self.status_code = status_code
        error_code = None
        error_msg = None
        if isinstance(error_data, dict):
            error_code = error_data.get('code')
            error_msg = error_data.get('msg', str(error_data))
        elif isinstance(error_data, str):
            error_code = -1 # Умовний код для не-JSON помилок
            error_msg = error_data
        else:
            error_code = -1 # Умовний код
            error_msg = repr(error_data)

        self.code = error_code
        self.msg = error_msg
        # Формуємо повідомлення для батьківського класу Exception
        message = f"Binance API Error: Status={status_code}, Code={self.code}, Msg='{self.msg}'"
        super().__init__(message)
# END BLOCK 1

# BLOCK 2: Async BinanceAPI Class Definition - __init__ and Constants
class BinanceAPI:
    """
    АСИНХРОННИЙ Клас для взаємодії з Binance REST API.
    Підтримує публічні, приватні ендпоінти та User Data Stream.
    Використовує aiohttp.
    """
    # Максимальна кількість Klines за один запит
    MAX_KLINE_LIMIT = 1000
    # Інтервал оновлення Listen Key (у секундах), трохи менше 60 хв
    LISTEN_KEY_KEEP_ALIVE_INTERVAL = 30 * 60
    # Таймаут за замовчуванням для API запитів
    REQUEST_TIMEOUT = 10 # Секунди

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, use_testnet: bool = False):
        """
        Ініціалізація АСИНХРОННОГО API клієнта.
        Створення сесії aiohttp та синхронізація часу перенесено в async метод initialize().

        Args:
            api_key (str, optional): Ключ API. Якщо None, береться з змінної середовища.
            api_secret (str, optional): Секретний ключ API. Якщо None, береться з змінної середовища.
            use_testnet (bool): Прапорець використання TestNet API.
        """
        self.use_testnet = use_testnet
        # Визначаємо ключі та базові URL залежно від режиму
        if use_testnet:
            self.api_key = api_key or os.environ.get('BINANCE_TESTNET_API_KEY')
            self.api_secret = api_secret or os.environ.get('BINANCE_TESTNET_SECRET_KEY')
            self.base_url = "https://testnet.binance.vision"
            self.ws_url_base = "wss://testnet.binance.vision" # Для WS Handler
            log_mode = "TestNet"
        else:
            self.api_key = api_key or os.environ.get('BINANCE_API_KEY')
            self.api_secret = api_secret or os.environ.get('BINANCE_API_SECRET')
            self.base_url = "https://api.binance.com"
            self.ws_url_base = "wss://stream.binance.com:9443" # Для WS Handler
            log_mode = "MainNet"

        logger.info(f"AsyncBinanceAPI({log_mode}) ініціалізовано (сесія та час будуть в initialize()).")

        # Перевіряємо наявність ключів
        if not self.api_key or not self.api_secret:
            logger.warning(f"AsyncBinanceAPI({log_mode}): Ключі API не надано/не знайдено. Доступ лише до публічних ендпоінтів.")

        # Формуємо повні URL для різних версій API
        self.api_v3_url = urljoin(self.base_url, "/api/v3/")
        self.sapi_v1_url = urljoin(self.base_url, "/sapi/v1/")

        # --- ЗМІНА: Сесія aiohttp створюється в initialize() ---
        self.session: aiohttp.ClientSession | None = None
        # --- КІНЕЦЬ ЗМІНИ ---

        # Змінна для зберігання зсуву часу сервера відносно локального
        self._server_time_offset = 0
        # Прапорець ініціалізації
        self._initialized: bool = False
        # Асинхронне блокування для ініціалізації/закриття
        self._init_lock = asyncio.Lock()

    # Наступні методи будуть додані в наступних блоках...

# END BLOCK 2

# BLOCK 3: Async Initialization and Cleanup Methods
    async def initialize(self):
        """
        Асинхронно створює сесію aiohttp та синхронізує час сервера.
        Має бути викликаний перед використанням API.
        """
        # Використовуємо блокування, щоб уникнути паралельної ініціалізації
        async with self._init_lock:
            if self._initialized:
                logger.debug("AsyncBinanceAPI: Вже ініціалізовано.")
                return
            if self.session is not None and not self.session.closed:
                logger.warning("AsyncBinanceAPI: Спроба ініціалізації при вже існуючій сесії.")
                return

            logger.info("AsyncBinanceAPI: Початок асинхронної ініціалізації...")
            try:
                # Створюємо таймаут для сесії
                timeout = ClientTimeout(total=self.REQUEST_TIMEOUT)
                # Створюємо сесію aiohttp
                self.session = aiohttp.ClientSession(timeout=timeout)
                logger.info("AsyncBinanceAPI: Сесію aiohttp створено.")

                # Налаштовуємо стандартні заголовки
                default_headers = {
                    'Accept': 'application/json',
                    'User-Agent': 'python-trading-bot-gemini-async/0.4.0' # Оновлено User-Agent
                }
                self.session.headers.update(default_headers)
                # Додаємо API ключ, якщо є
                if self.api_key:
                    self.session.headers['X-MBX-APIKEY'] = self.api_key

                # Виконуємо початкову синхронізацію часу
                await self._sync_time() # Викликаємо асинхронний внутрішній метод

                # Встановлюємо прапорець успішної ініціалізації
                self._initialized = True
                logger.info("AsyncBinanceAPI: Асинхронну ініціалізацію завершено успішно.")

            except Exception as e:
                logger.critical(f"AsyncBinanceAPI: Критична помилка під час initialize: {e}", exc_info=True)
                # Закриваємо сесію, якщо вона була створена
                if self.session and not self.session.closed:
                    await self.session.close()
                self.session = None
                self._initialized = False
                # Перекидаємо помилку далі
                raise BinanceAPIError(500, {'code': -1001, 'msg': f'Initialization failed: {e}'}) from e

    async def _sync_time(self):
        """
        Внутрішній асинхронний метод для синхронізації часу сервера.
        Використовує створену сесію безпосередньо.
        """
        if not self.session or self.session.closed:
            logger.error("AsyncBinanceAPI: Неможливо синхронізувати час, сесія відсутня або закрита.")
            raise BinanceAPIError(500, {'code': -1004, 'msg': 'Session not available for time sync'})

        time_url = urljoin(self.api_v3_url, 'time')
        logger.debug(f"AsyncBinanceAPI: Запит часу сервера: GET {time_url}")
        try:
            # Використовуємо явний таймаут для цього критичного запиту
            async with self.session.get(time_url, timeout=ClientTimeout(total=5.0)) as response:
                status_code = response.status
                if 200 <= status_code < 300:
                    # Використовуємо content_type=None для більшої стійкості до відповідей Binance
                    data = await response.json(content_type=None)
                    if data and 'serverTime' in data:
                        server_time_ms = data['serverTime']
                        local_time_ms = int(time.time() * 1000)
                        self._server_time_offset = server_time_ms - local_time_ms
                        offset_str = f"{self._server_time_offset} ms"
                        logger.info(f"AsyncBinanceAPI: Час сервера синхронізовано. Зсув: {offset_str}.")
                    else:
                        logger.warning(f"AsyncBinanceAPI: Не отримано 'serverTime' у відповіді ({status_code}).")
                        raise BinanceAPIError(status_code, {'code': -1005, 'msg': 'Invalid time response format'})
                else:
                    # Якщо помилка при запиті часу
                    response_text = await response.text()
                    logger.error(f"AsyncBinanceAPI: Помилка запиту часу сервера ({status_code}): {response_text[:200]}")
                    raise BinanceAPIError(status_code, {'code': -1006, 'msg': f'Time sync failed with status {status_code}'})
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"AsyncBinanceAPI: Помилка з'єднання/таймауту під час синхронізації часу: {e}")
            raise BinanceAPIError(503, {'code': -1007, 'msg': f'Connection/Timeout error during time sync: {e}'}) from e
        except Exception as e:
             logger.error(f"AsyncBinanceAPI: Невідома помилка під час синхронізації часу: {e}", exc_info=True)
             raise BinanceAPIError(500, {'code': -1000, 'msg': f'Unknown error during time sync: {e}'}) from e

    async def close(self):
        """Асинхронно закриває сесію aiohttp."""
        async with self._init_lock: # Використовуємо той самий лок, що й для ініціалізації
            if self.session and not self.session.closed:
                logger.info("AsyncBinanceAPI: Закриття сесії aiohttp...")
                await self.session.close()
                self.session = None
                self._initialized = False
                logger.info("AsyncBinanceAPI: Сесію aiohttp закрито.")
            elif self.session and self.session.closed:
                 logger.debug("AsyncBinanceAPI: Сесія вже була закрита.")
                 self.session = None # Просто скидаємо посилання
                 self._initialized = False
            else:
                logger.debug("AsyncBinanceAPI: Сесія не була створена, закривати нічого.")
                self._initialized = False # Скидаємо на випадок, якщо ініціалізація не вдалася

    # Наступні методи...
# END BLOCK 3

# BLOCK 4: Signature and Timestamp Helper Methods
    def _generate_signature(self, data: dict) -> str:
        """Генерує HMAC-SHA256 підпис для запиту (синхронний метод)."""
        secret = self.api_secret
        if not secret:
            signature_error = ValueError("API Secret не встановлено для генерації підпису.")
            # Логуємо помилку перед генерацією винятку
            logger.error(signature_error)
            raise signature_error

        # Фільтруємо None значення перед кодуванням
        filtered_data = {k: v for k, v in data.items() if v is not None}
        query_string = urlencode(filtered_data)
        signature = hmac.new(
            secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _get_timestamp(self) -> int:
        """Повертає поточний час UTC в мілісекундах, скоригований на зсув сервера (синхронний метод)."""
        # Перевіряємо, чи була ініціалізація (і, відповідно, синхронізація часу)
        # Хоча цей метод синхронний, викликати його до initialize() не має сенсу
        if not self._initialized:
             logger.warning("AsyncBinanceAPI: Спроба отримати timestamp до ініціалізації. Зсув може бути неточним.")
             # Повертаємо локальний час + поточний (можливо, нульовий) зсув
             # return int(time.time() * 1000) + self._server_time_offset
             # Або генеруємо помилку, щоб звернути увагу
             raise RuntimeError("Timestamp requested before API initialization and time sync.")

        local_time_ms = int(time.time() * 1000)
        adjusted_time_ms = local_time_ms + self._server_time_offset
        return adjusted_time_ms

    # Метод _sync_time() тепер асинхронний і знаходиться в Блоці 3

    # Наступні методи...
# END BLOCK 4

# BLOCK 5: Core Async Request Method
    async def _send_request(self,
                           method: str,
                           endpoint_path: str,
                           base_url_type: str = 'api_v3',
                           params: Optional[Dict[str, Any]] = None,
                           signed: bool = False,
                           is_user_stream: bool = False,
                           timeout: int | None = None # Дозволяємо None для використання таймауту сесії
                          ) -> Union[Dict[str, Any], List[Any], None]:
        """
        Внутрішній АСИНХРОННИЙ метод для надсилання HTTP запитів до Binance API.
        Обробляє підпис, різні типи URL, помилки за допомогою aiohttp.
        """
        # Перевіряємо ініціалізацію
        if not self._initialized or not self.session or self.session.closed:
             err_msg = "AsyncBinanceAPI не ініціалізовано або сесія закрита."
             logger.error(err_msg)
             raise BinanceAPIError(500, {'code': -1002, 'msg': err_msg})

        # Ініціалізуємо параметри
        request_params = params.copy() if params else {}

        # --- Підготовка підписаних запитів ---
        if signed:
            if not self.api_key or not self.api_secret:
                logger.error("Підписаний запит неможливий: Ключі API не налаштовані.")
                raise BinanceAPIError(401, {'code': -1002, 'msg': 'API keys not configured'})

            if not is_user_stream:
                request_params['timestamp'] = self._get_timestamp()
                signature = self._generate_signature(request_params)
                request_params['signature'] = signature

        # --- Визначення URL ---
        url_base_map = {
            'api_v3': self.api_v3_url,
            'sapi_v1': self.sapi_v1_url
        }
        # User Stream використовує api_v3_url
        url_base = self.api_v3_url if is_user_stream else url_base_map.get(base_url_type)
        if not url_base:
            logger.error(f"Невідомий тип базового URL: {base_url_type}")
            raise ValueError(f"Invalid base_url_type: {base_url_type}")

        endpoint_cleaned = endpoint_path.lstrip('/')
        url = urljoin(url_base, endpoint_cleaned)

        # --- Розподіл параметрів: query string vs data payload ---
        http_method = method.upper()
        query_string_params: Optional[Dict[str, Any]] = None
        data_payload: Optional[Dict[str, Any]] = None

        if http_method in ['GET', 'DELETE']:
            query_string_params = request_params
        elif http_method in ['POST', 'PUT']:
            if is_user_stream:
                query_string_params = request_params # User Stream завжди використовує query params
            else:
                # Для POST/PUT тіло запиту зазвичай надсилається як application/x-www-form-urlencoded
                # aiohttp використовує 'data' для цього, якщо передати словник
                data_payload = request_params
        else:
            logger.error(f"Непідтримуваний HTTP метод: {http_method}")
            raise ValueError(f"Unsupported HTTP method: {http_method}")

        # --- Логування запиту ---
        params_for_log_dict = query_string_params if query_string_params is not None else data_payload
        # Видаляємо чутливі дані з логу
        log_params_safe = {}
        if isinstance(params_for_log_dict, dict):
             log_params_safe = {k: v for k, v in params_for_log_dict.items() if k not in ['signature', 'apiKey', 'newClientOrderId', 'listClientOrderId', 'stopClientOrderId', 'limitClientOrderId']} # Додано ID ордерів
        params_for_log_str = str(log_params_safe)[:150] if log_params_safe else "{}"
        logger.debug(f"AsyncAPI -> {http_method} {url} | Params(part): {params_for_log_str}...")


        # --- Налаштування таймауту для конкретного запиту ---
        request_timeout = ClientTimeout(total=timeout if timeout is not None else self.REQUEST_TIMEOUT)

        # --- Надсилання запиту та обробка відповіді ---
        try:
            async with self.session.request(
                method=http_method,
                url=url,
                params=query_string_params, # aiohttp використовує 'params' для query string
                data=data_payload,         # aiohttp використовує 'data' для тіла POST/PUT
                # headers=... # Заголовки вже в сесії
                timeout=request_timeout
            ) as response:
                status_code = response.status
                response_text = await response.text() # Читаємо текст для логів/помилок

                # --- Спроба розпарсити JSON ---
                try:
                    # content_type=None робить парсинг стійкішим до Content-Type від Binance
                    response_data = await response.json(content_type=None)
                except json.JSONDecodeError:
                    # Не JSON відповідь
                    is_success_status = 200 <= status_code < 300
                    is_empty_body = not response_text
                    if is_success_status and is_empty_body:
                        logger.debug(f"AsyncAPI <- {status_code} | OK (Empty Body)")
                        return {} # Успішна порожня відповідь (напр., DELETE)
                    else:
                        log_text_preview = response_text[:200]
                        logger.error(f"AsyncAPI <- {status_code} | Не JSON відповідь: {log_text_preview}...")
                        if not is_success_status:
                            # Генеруємо помилку з текстом відповіді
                            raise BinanceAPIError(status_code, response_text)
                        else:
                            # Успішний статус, але не JSON і не порожнє тіло - дивно
                            return None # Повертаємо None, щоб вказати на проблему

                # --- Обробка JSON відповіді ---
                if 200 <= status_code < 300:
                    log_data_preview = str(response_data)[:200]
                    logger.debug(f"AsyncAPI <- {status_code} | OK | Data(part): {log_data_preview}...")
                    return response_data # Повертаємо успішну JSON відповідь
                else:
                    # Статус код помилки (4xx, 5xx), але є JSON
                    error_data_dict = response_data if isinstance(response_data, dict) else {}
                    error_code = error_data_dict.get('code', -1)
                    error_msg = error_data_dict.get('msg', response_text)
                    logger.error(f"AsyncAPI <- {status_code} | Помилка API Binance: Code={error_code}, Msg='{error_msg}' | URL={url}")

                    # Обробка помилки timestamp (-1021)
                    if error_code == -1021:
                        logger.warning("Помилка Timestamp (-1021). Спроба повторної синхронізації часу...")
                        try:
                            await self._sync_time() # Викликаємо асинхронну синхронізацію
                        except Exception as sync_err:
                             logger.error(f"Не вдалося повторно синхронізувати час після помилки -1021: {sync_err}")
                             # Продовжуємо з оригінальною помилкою -1021

                    # Генеруємо виняток BinanceAPIError
                    raise BinanceAPIError(status_code=status_code, error_data=error_data_dict)

        # --- Обробка помилок рівня aiohttp/asyncio ---
        except asyncio.TimeoutError:
            timeout_val = timeout if timeout is not None else self.REQUEST_TIMEOUT
            logger.error(f"AsyncAPI Timeout ({timeout_val}s): {http_method} {url}")
            raise BinanceAPIError(408, {'code':-1008, 'msg':'Request Timeout'}) from None
        except aiohttp.ClientError as e: # Базовий клас для помилок aiohttp
            logger.error(f"AsyncAPI ClientError: {e} | {http_method} {url}")
            # Можна деталізувати типи помилок (ClientConnectorError, ClientResponseError etc.)
            raise BinanceAPIError(503, {'code':-1007, 'msg':f'Connection Error: {e}'}) from e
        # --- Обробка помилок, які ми самі генеруємо (напр., відсутність ключів) ---
        except BinanceAPIError as e:
            raise e # Просто перекидаємо далі
        # --- Обробка будь-яких інших непередбачених помилок ---
        except Exception as e:
            logger.error(f"Невідома помилка під час AsyncAPI запиту: {e}", exc_info=True)
            raise BinanceAPIError(500, {'code':-1000, 'msg':f'Unknown Error: {e}'}) from e

# END BLOCK 5

# BLOCK 6: Public Async Methods (Market Data)
    async def get_server_time(self) -> int | None:
        """Асинхронно отримує поточний час сервера Binance в мілісекундах UTC."""
        try:
            # Викликаємо асинхронний метод для надсилання запиту
            response = await self._send_request('GET', 'time')
            if isinstance(response, dict) and 'serverTime' in response:
                server_time_ms = response['serverTime']
                return server_time_ms
            else:
                logger.warning("get_server_time: Не отримано 'serverTime' у відповіді.")
                return None
        except BinanceAPIError as e:
            logger.error(f"get_server_time: Помилка API: {e}")
            return None
        except Exception as e:
            logger.error(f"get_server_time: Загальна помилка: {e}", exc_info=True)
            return None

    async def get_exchange_info(self, symbol: Optional[str] = None, symbols: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """
        Асинхронно отримує інформацію про біржу та торгові правила.
        Можна вказати конкретний символ або список символів.
        """
        params: Dict[str, Any] = {}
        if symbol:
            params['symbol'] = symbol.upper()
        elif symbols:
             # Переконуємось, що передано список рядків
             if isinstance(symbols, list) and all(isinstance(s, str) for s in symbols):
                 symbols_upper = [s.upper() for s in symbols]
                 params['symbols'] = json.dumps(symbols_upper)
             else:
                  logger.error("get_exchange_info: 'symbols' має бути списком рядків.")
                  return None

        try:
            response = await self._send_request(
                method='GET',
                endpoint_path='exchangeInfo',
                params=params
            )
            # _send_request повертає dict або None у разі не-JSON або помилки парсингу
            # Перевіряємо, чи результат є словником
            if isinstance(response, dict):
                return response
            else:
                 # Якщо _send_request повернув щось інше (напр. None через помилку парсингу)
                 logger.warning("get_exchange_info: Отримано неочікувану відповідь від _send_request.")
                 return None
        except BinanceAPIError as e:
            logger.error(f"get_exchange_info: Помилка API: {e}")
            return None
        except Exception as e:
            logger.error(f"get_exchange_info: Загальна помилка: {e}", exc_info=True)
            return None

    async def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Асинхронно отримує інформацію для конкретного символу."""
        exchange_info = await self.get_exchange_info(symbol=symbol)
        if exchange_info and 'symbols' in exchange_info:
            symbols_list = exchange_info['symbols']
            if isinstance(symbols_list, list) and len(symbols_list) == 1:
                symbol_data = symbols_list[0]
                # Перевіряємо, чи отриманий символ співпадає із запитаним (про всяк випадок)
                if isinstance(symbol_data, dict) and symbol_data.get('symbol') == symbol.upper():
                     return symbol_data
                else:
                     logger.warning(f"get_symbol_info: Отримано дані для іншого символу у відповіді для {symbol}.")
                     return None
            else:
                logger.warning(f"get_symbol_info: Неочікувана структура 'symbols' у відповіді для {symbol}.")
                return None
        else:
            # Помилка вже мала бути залогована в get_exchange_info
            return None

    async def get_current_price(self, symbol: str) -> Optional[Decimal]:
        """Асинхронно отримує поточну ціну для вказаного символу."""
        params = {'symbol': symbol.upper()}
        try:
            response = await self._send_request(
                method='GET',
                endpoint_path='ticker/price',
                params=params
            )
            if isinstance(response, dict) and 'price' in response:
                price_str = response['price']
                try:
                    price_decimal = Decimal(price_str)
                    return price_decimal
                except InvalidOperation:
                    logger.error(f"get_current_price: Не вдалося конвертувати ціну '{price_str}' для {symbol} у Decimal.")
                    return None
            else:
                logger.warning(f"get_current_price: Не отримано 'price' у відповіді для {symbol}.")
                return None
        except BinanceAPIError as e:
            logger.error(f"get_current_price ({symbol}): Помилка API: {e}")
            return None
        except Exception as e:
            logger.error(f"get_current_price ({symbol}): Загальна помилка: {e}", exc_info=True)
            return None

    # Наступні методи...
# END BLOCK 6

# BLOCK 7: Public Async Method for Historical Klines (with Pagination)
    async def get_historical_klines(self,
                                    symbol: str,
                                    interval: str,
                                    start_ts: Optional[int] = None,
                                    end_ts: Optional[int] = None,
                                    limit: int = 1000
                                   ) -> Optional[List[List[Any]]]:
        """
        Асинхронно отримує історичні дані Klines (свічки).
        Автоматично обробляє пагінацію для завантаження великих періодів.

        Args:
            symbol (str): Символ (напр., 'BTCUSDT').
            interval (str): Інтервал ('1m', '1h', '1d', ...).
            start_ts (int, optional): Початковий timestamp UTC в мілісекундах.
            end_ts (int, optional): Кінцевий timestamp UTC в мілісекундах (включно).
            limit (int): Максимальна кількість свічок за один запит (max 1000).

        Returns:
            list or None: Список списків з даними Klines або None у разі помилки.
        """
        symbol_upper = symbol.upper()
        request_limit = min(limit, self.MAX_KLINE_LIMIT)
        all_klines: List[List[Any]] = []
        current_start_ts = start_ts

        # Визначаємо кінцевий timestamp, якщо не вказано (поточний час)
        # Використовуємо _get_timestamp для врахування зсуву
        end_ts_final = end_ts if end_ts is not None else self._get_timestamp()

        # Перевірка логіки дат
        if start_ts is None and end_ts is not None:
            logger.error("get_historical_klines: Якщо вказано end_ts, потрібно вказати і start_ts.")
            return None
        if start_ts is not None and end_ts is not None and start_ts >= end_ts_final:
            logger.warning(f"get_historical_klines: start_ts ({start_ts}) >= end_ts ({end_ts_final}). Даних не буде.")
            return [] # Повертаємо порожній список

        log_start_dt_str = str(start_ts)
        if start_ts: log_start_dt_str = pd.to_datetime(start_ts, unit='ms', utc=True).strftime('%Y-%m-%d %H:%M')
        log_end_dt_str = pd.to_datetime(end_ts_final, unit='ms', utc=True).strftime('%Y-%m-%d %H:%M')
        logger.info(f"Завантаження Klines: {symbol_upper}({interval}) | Start:{log_start_dt_str}| End:{log_end_dt_str}| LimitPerReq:{request_limit}")

        # --- Цикл пагінації ---
        while True:
            params: Dict[str, Any] = {
                'symbol': symbol_upper,
                'interval': interval,
                'limit': request_limit
            }
            if current_start_ts:
                params['startTime'] = current_start_ts
            # Додаємо endTime, якщо він є і період ще не завершено
            if end_ts_final:
                 # Перевіряємо, чи наступний startTime не перевищить endTime
                 if current_start_ts and current_start_ts > end_ts_final:
                      logger.debug("get_historical_klines: Наступний startTime перевищує endTime, завершення пагінації.")
                      break
                 params['endTime'] = end_ts_final # Включаємо кінцевий час

            logger.debug(f"API Запит klines batch: {params}")

            try:
                # Надсилаємо асинхронний запит
                klines_batch = await self._send_request(
                    method='GET',
                    endpoint_path='klines',
                    params=params
                )

                # Перевіряємо результат
                if klines_batch is None:
                    logger.error("get_historical_klines: Запит klines повернув None (помилка запиту або парсингу).")
                    return None # Помилка під час запиту

                # Переконуємось, що це список (навіть якщо порожній)
                if not isinstance(klines_batch, list):
                     logger.error(f"get_historical_klines: Отримано неочікуваний тип відповіді для klines: {type(klines_batch)}")
                     return None # Помилка формату відповіді

                if not klines_batch:
                    logger.debug("get_historical_klines: Отримано порожній batch, завершення пагінації.")
                    break # Виходимо з циклу, якщо даних більше немає

                # Додаємо отримані свічки до загального списку
                all_klines.extend(klines_batch)
                logger.debug(f"get_historical_klines: Завантажено batch {len(klines_batch)} свічок. Всього: {len(all_klines)}")

                # --- Логіка для наступної ітерації ---
                if start_ts is None: # Якщо завантажували тільки останні 'limit'
                    break

                # Отримуємо час останньої свічки в batch
                # Індекси Klines: 0 - Open time, 6 - Close time
                last_kline_time_ms = klines_batch[-1][0]

                # Перевіряємо умови виходу
                reached_end_time = last_kline_time_ms >= end_ts_final
                received_less_than_limit = len(klines_batch) < request_limit

                if reached_end_time or received_less_than_limit:
                    logger.debug(f"get_historical_klines: Досягнуто кінця періоду або даних ({'end_time' if reached_end_time else 'limit'}).")
                    break

                # Оновлюємо start_ts для наступного запиту
                current_start_ts = last_kline_time_ms + 1
                # Асинхронна пауза між запитами
                await asyncio.sleep(0.12)

            except BinanceAPIError as e:
                logger.error(f"get_historical_klines: Помилка API під час завантаження batch: {e}")
                return None # Повертаємо None у разі помилки API
            except Exception as e:
                logger.error(f"get_historical_klines: Загальна помилка під час завантаження batch: {e}", exc_info=True)
                return None # Повертаємо None у разі іншої помилки

        # --- Завершення ---
        final_kline_count = len(all_klines)
        logger.info(f"Завантаження Klines завершено. Загальна кількість свічок: {final_kline_count}.")
        # Видаляємо дублікати, якщо вони раптом з'явилися (за timestamp)
        if final_kline_count > 0:
             unique_klines_dict = {k[0]: k for k in all_klines}
             unique_klines = list(unique_klines_dict.values())
             # Сортуємо за timestamp про всяк випадок
             unique_klines.sort(key=lambda x: x[0])
             if len(unique_klines) < final_kline_count:
                  logger.warning(f"get_historical_klines: Видалено {final_kline_count - len(unique_klines)} дублікатів свічок.")
             return unique_klines
        else:
             return all_klines # Повертаємо порожній список, якщо нічого не завантажено
# END BLOCK 7

# BLOCK 8: Public Async Methods (Account and Order Management)
    async def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Асинхронно отримує інформацію про акаунт (баланси, дозволи). Потребує підпису."""
        try:
            response = await self._send_request(
                method='GET',
                endpoint_path='account',
                signed=True
            )
            # Перевіряємо чи відповідь є словником
            if isinstance(response, dict):
                 return response
            else:
                 logger.warning(f"get_account_info: Отримано неочікуваний тип відповіді: {type(response)}")
                 return None
        except BinanceAPIError as e:
            logger.error(f"get_account_info: Помилка API: {e}")
            return None
        except Exception as e:
            logger.error(f"get_account_info: Загальна помилка: {e}", exc_info=True)
            return None

    async def place_order(self, **params) -> Optional[Dict[str, Any]]:
        """
        Асинхронно розміщує новий ордер (MARKET, LIMIT, etc.). Потребує підпису.
        Приймає параметри ордера як key=value аргументи.
        Числові значення (quantity, price, etc.) мають бути передані як Decimal або рядок.
        """
        required_params = ['symbol', 'side', 'type']
        if not all(k in params for k in required_params):
            logger.error(f"place_order: Відсутні обов'язкові параметри: {required_params}. Отримано: {list(params.keys())}")
            return None

        order_params = params.copy()
        order_params['symbol'] = order_params['symbol'].upper()
        order_params['side'] = order_params['side'].upper()
        order_params['type'] = order_params['type'].upper()

        # Конвертуємо Decimal/float/int в рядки для API
        numeric_keys = ['quantity', 'quoteOrderQty', 'price', 'stopPrice']
        for key in numeric_keys:
            if key in order_params and order_params[key] is not None:
                 # Використовуємо format(Decimal(str(value)), 'f') для універсальності та точності
                 try:
                      decimal_value = Decimal(str(order_params[key]))
                      # Важливо: Не форматувати 'quoteOrderQty' як 'f', якщо не впевнені в точності
                      if key == 'quoteOrderQty':
                           order_params[key] = str(decimal_value) # Зберігаємо як рядок без фіксованої точки
                      else:
                           order_params[key] = format(decimal_value, 'f')
                 except (InvalidOperation, TypeError, ValueError) as fmt_err:
                       logger.error(f"place_order: Помилка форматування параметра '{key}'='{order_params[key]}': {fmt_err}")
                       return None # Помилка форматування параметра

        try:
            response = await self._send_request(
                method='POST',
                endpoint_path='order',
                params=order_params, # Для POST параметри йдуть в 'data' в _send_request
                signed=True
            )
            if isinstance(response, dict):
                return response
            else:
                 logger.warning(f"place_order: Отримано неочікуваний тип відповіді: {type(response)}")
                 return None
        except BinanceAPIError as e:
            logger.error(f"place_order: Помилка API: {e}")
            return None
        except Exception as e:
            logger.error(f"place_order: Загальна помилка: {e}", exc_info=True)
            return None

    async def place_oco_order(self, **params) -> Optional[Dict[str, Any]]:
        """
        Асинхронно розміщує OCO ордер. Потребує підпису.
        Числові значення (quantity, price, etc.) мають бути передані як Decimal або рядок.
        """
        required_params = ['symbol', 'side', 'quantity', 'price', 'stopPrice']
        if not all(k in params for k in required_params):
            logger.error(f"place_oco_order: Відсутні обов'язкові параметри: {required_params}. Отримано: {list(params.keys())}")
            return None

        oco_params = params.copy()
        oco_params['symbol'] = oco_params['symbol'].upper()
        oco_params['side'] = oco_params['side'].upper()

        # Конвертуємо числові значення в рядки
        numeric_keys = ['quantity', 'price', 'stopPrice', 'stopLimitPrice']
        for key in numeric_keys:
            if key in oco_params and oco_params[key] is not None:
                 try:
                      decimal_value = Decimal(str(oco_params[key]))
                      oco_params[key] = format(decimal_value, 'f')
                 except (InvalidOperation, TypeError, ValueError) as fmt_err:
                       logger.error(f"place_oco_order: Помилка форматування параметра '{key}'='{oco_params[key]}': {fmt_err}")
                       return None

        try:
            response = await self._send_request(
                method='POST',
                endpoint_path='order/oco',
                params=oco_params, # Для POST параметри йдуть в 'data' в _send_request
                signed=True
            )
            if isinstance(response, dict):
                return response
            else:
                 logger.warning(f"place_oco_order: Отримано неочікуваний тип відповіді: {type(response)}")
                 return None
        except BinanceAPIError as e:
            logger.error(f"place_oco_order: Помилка API: {e}")
            return None
        except Exception as e:
            logger.error(f"place_oco_order: Загальна помилка: {e}", exc_info=True)
            return None

    async def cancel_order(self, **params) -> Optional[Dict[str, Any]]:
        """
        Асинхронно скасовує активний ордер. Потребує підпису.
        Потрібно передати symbol та один з: orderId або origClientOrderId.
        """
        symbol_present = 'symbol' in params and params['symbol']
        id_present = ('orderId' in params and params['orderId']) or \
                     ('origClientOrderId' in params and params['origClientOrderId'])
        if not symbol_present or not id_present:
            logger.error("cancel_order: Потрібно вказати 'symbol' та один з 'orderId' або 'origClientOrderId'.")
            return None

        cancel_params = params.copy()
        cancel_params['symbol'] = cancel_params['symbol'].upper()

        try:
            response = await self._send_request(
                method='DELETE',
                endpoint_path='order',
                params=cancel_params,
                signed=True
            )
            if isinstance(response, dict):
                return response
            else:
                 logger.warning(f"cancel_order: Отримано неочікуваний тип відповіді: {type(response)}")
                 return None
        except BinanceAPIError as e:
            if e.code == -2011: # Order does not exist
                logger.warning(f"cancel_order: Ордер не знайдено (можливо, вже виконано або скасовано). ID: {params.get('orderId') or params.get('origClientOrderId')}")
                # Повертаємо відповідь API помилки, якщо вона є (словник), інакше порожній словник
                return e.error_data if isinstance(e.error_data, dict) else {}
            else:
                logger.error(f"cancel_order: Помилка API: {e}")
                return None
        except Exception as e:
            logger.error(f"cancel_order: Загальна помилка: {e}", exc_info=True)
            return None

    async def cancel_all_orders(self, symbol: str) -> Optional[List[Any]]:
        """
        Асинхронно скасовує всі активні ордери для вказаного символу. Потребує підпису.
        """
        if not symbol:
            logger.error("cancel_all_orders: Потрібно вказати 'symbol'.")
            return None
        params = {'symbol': symbol.upper()}
        try:
            response = await self._send_request(
                method='DELETE',
                endpoint_path='openOrders',
                params=params,
                signed=True
            )
            # Очікується список, перевіряємо тип
            if isinstance(response, list):
                 return response
            else:
                 logger.warning(f"cancel_all_orders: Отримано неочікуваний тип відповіді: {type(response)}")
                 return None
        except BinanceAPIError as e:
            if e.code == -2011: # No open orders
                logger.info(f"cancel_all_orders: Не знайдено відкритих ордерів для скасування для {symbol} (Code -2011).")
                return [] # Повертаємо порожній список
            else:
                logger.error(f"cancel_all_orders ({symbol}): Помилка API: {e}")
                return None
        except Exception as e:
            logger.error(f"cancel_all_orders ({symbol}): Загальна помилка: {e}", exc_info=True)
            return None

    async def get_open_orders(self, symbol: Optional[str] = None) -> Optional[List[Any]]:
        """
        Асинхронно отримує список активних (відкритих) ордерів. Потребує підпису.
        Можна вказати символ для фільтрації.
        """
        params = {}
        if symbol:
            params['symbol'] = symbol.upper()
        try:
            response = await self._send_request(
                method='GET',
                endpoint_path='openOrders',
                params=params,
                signed=True
            )
            if isinstance(response, list):
                 return response
            else:
                 logger.warning(f"get_open_orders: Отримано неочікуваний тип відповіді: {type(response)}")
                 return None
        except BinanceAPIError as e:
            logger.error(f"get_open_orders: Помилка API: {e}")
            return None
        except Exception as e:
            logger.error(f"get_open_orders: Загальна помилка: {e}", exc_info=True)
            return None

    async def get_order_status(self, **params) -> Optional[Dict[str, Any]]:
        """
        Асинхронно перевіряє статус конкретного ордера. Потребує підпису.
        Потрібно передати symbol та один з: orderId або origClientOrderId.
        """
        symbol_present = 'symbol' in params and params['symbol']
        id_present = ('orderId' in params and params['orderId']) or \
                     ('origClientOrderId' in params and params['origClientOrderId'])
        if not symbol_present or not id_present:
            logger.error("get_order_status: Потрібно вказати 'symbol' та один з 'orderId' або 'origClientOrderId'.")
            return None

        status_params = params.copy()
        status_params['symbol'] = status_params['symbol'].upper()

        try:
            response = await self._send_request(
                method='GET',
                endpoint_path='order',
                params=status_params,
                signed=True
            )
            if isinstance(response, dict):
                 return response
            else:
                 logger.warning(f"get_order_status: Отримано неочікуваний тип відповіді: {type(response)}")
                 return None
        except BinanceAPIError as e:
            if e.code == -2013: # Order does not exist
                logger.warning(f"get_order_status: Ордер не знайдено (Code -2013). ID: {params.get('orderId') or params.get('origClientOrderId')}")
                return None # Ордер не знайдено
            else:
                logger.error(f"get_order_status: Помилка API: {e}")
                return None
        except Exception as e:
            logger.error(f"get_order_status: Загальна помилка: {e}", exc_info=True)
            return None

    async def get_order_list(self, **params) -> Optional[Dict[str, Any]]:
        """
        Асинхронно перевіряє статус OCO ордера за orderListId або listClientOrderId/origClientOrderId.
        Потребує підпису. Ендпоінт: /api/v3/orderList
        """
        order_list_id = params.get('orderListId')
        list_client_order_id = params.get('listClientOrderId')
        orig_client_order_id = params.get('origClientOrderId')
        has_list_id = (order_list_id is not None and order_list_id != -1) or \
                      (list_client_order_id is not None) or \
                      (orig_client_order_id is not None)

        if not has_list_id:
            logger.error("get_order_list: Потрібно вказати 'orderListId' або 'listClientOrderId'/'origClientOrderId'.")
            return None

        query_params = {k: v for k, v in params.items() if v is not None}

        try:
            response = await self._send_request(
                method='GET',
                endpoint_path='orderList',
                params=query_params,
                signed=True
            )
            # _send_request поверне dict або None
            if isinstance(response, dict):
                return response
            else:
                 logger.warning(f"get_order_list: Отримано неочікуваний тип відповіді: {type(response)}")
                 return None
        except BinanceAPIError as e:
            if e.code == -2013: # Order list does not exist
                id_used_for_log = order_list_id or list_client_order_id or orig_client_order_id
                logger.warning(f"get_order_list: OCO ордер не знайдено (Code -2013). ID: {id_used_for_log}")
                # Повертаємо порожній словник, щоб відрізнити від помилки API
                return {}
            else:
                logger.error(f"get_order_list: Помилка API: {e}")
                return None
        except Exception as e:
            logger.error(f"get_order_list: Загальна помилка: {e}", exc_info=True)
            return None

# END BLOCK 8

# BLOCK 9: Public Async Methods (User Data Stream)
    async def start_user_data_stream(self) -> Optional[str]:
        """
        Асинхронно створює новий User Data Stream та повертає listenKey.
        Потребує підпису.
        """
        logger.info("AsyncAPI: Запит на створення User Data Stream...")
        try:
            response = await self._send_request(
                method='POST',
                endpoint_path='userDataStream',
                signed=True,
                is_user_stream=True # Вказуємо, що це ендпоінт User Stream
            )
            listen_key = None
            if isinstance(response, dict):
                listen_key = response.get('listenKey')

            if listen_key:
                key_preview = listen_key[:5]
                logger.info(f"AsyncAPI: Отримано listenKey: {key_preview}...")
                return listen_key
            else:
                logger.error(f"AsyncAPI: Не отримано listenKey у відповіді: {response}")
                return None
        except BinanceAPIError as e:
            logger.error(f"start_user_data_stream: Помилка API: {e}")
            return None
        except Exception as e:
            logger.error(f"start_user_data_stream: Загальна помилка: {e}", exc_info=True)
            return None

    async def keep_alive_user_data_stream(self, listen_key: str) -> bool:
        """
        Асинхронно продовжує термін дії User Data Stream (keep-alive).
        Потребує підпису.
        """
        if not listen_key:
            logger.warning("keep_alive_user_data_stream: listen_key не передано.")
            return False

        key_preview = listen_key[:5]
        logger.debug(f"AsyncAPI: Спроба оновлення listenKey: {key_preview}...")
        params = {'listenKey': listen_key}

        try:
            response = await self._send_request(
                method='PUT',
                endpoint_path='userDataStream',
                params=params,
                signed=True,
                is_user_stream=True
            )
            # Успішна відповідь - це порожній словник {}
            is_success = isinstance(response, dict) and response == {}
            if is_success:
                logger.info(f"AsyncAPI: Успішно оновлено listenKey: {key_preview}...")
                return True
            else:
                logger.error(f"AsyncAPI: Помилка оновлення listenKey {key_preview}. Відповідь: {response}")
                return False
        except BinanceAPIError as e:
            logger.error(f"keep_alive_user_data_stream ({key_preview}): Помилка API: {e}")
            return False
        except Exception as e:
            logger.error(f"keep_alive_user_data_stream ({key_preview}): Загальна помилка: {e}", exc_info=True)
            return False

    async def close_user_data_stream(self, listen_key: str) -> bool:
        """
        Асинхронно закриває User Data Stream.
        Потребує підпису.
        """
        if not listen_key:
            logger.warning("close_user_data_stream: listen_key не передано.")
            return False

        key_preview = listen_key[:5]
        logger.info(f"AsyncAPI: Спроба закриття listenKey: {key_preview}...")
        params = {'listenKey': listen_key}

        try:
            response = await self._send_request(
                method='DELETE',
                endpoint_path='userDataStream',
                params=params,
                signed=True,
                is_user_stream=True
            )
            # Успішна відповідь - порожній словник {}
            is_success = isinstance(response, dict) and response == {}
            if is_success:
                logger.info(f"AsyncAPI: Успішно закрито listenKey: {key_preview}...")
                return True
            else:
                logger.error(f"AsyncAPI: Помилка закриття listenKey {key_preview}. Відповідь: {response}")
                return False
        except BinanceAPIError as e:
            logger.error(f"close_user_data_stream ({key_preview}): Помилка API: {e}")
            return False
        except Exception as e:
            logger.error(f"close_user_data_stream ({key_preview}): Загальна помилка: {e}", exc_info=True)
            return False

# END BLOCK 9

# =============================================================================
# BLOCK 10: Async BinanceWebSocketHandler Class - Imports, Header, __init__
# =============================================================================
class BinanceWebSocketHandler:
    """
    АСИНХРОННИЙ обробник WebSocket з'єднань Binance (Market Data + User Data).
    Використовує aiohttp, керується через asyncio.Task.
    Забезпечує перепідключення та викликає callbacks.
    """
    def __init__(self,
                 streams: List[str],
                 callbacks: Dict[str, Callable],
                 api: BinanceAPI, # Тепер обов'язковий, використовуємо його сесію
                 ws_log_level: int = logging.WARNING # Рівень логування для бібліотеки (якщо потрібно)
                ):
        """
        Ініціалізація асинхронного обробника WebSocket.

        Args:
            streams (List[str]): Список ринкових потоків (напр., ['btcusdt@kline_1m']).
            callbacks (Dict[str, Callable]): Словник callback-функцій
                                             (ключі: 'kline', 'trade', 'depth', 'order_update', 'balance_update', 'other_market', 'other_user').
            api (BinanceAPI): Екземпляр АСИНХРОННОГО BinanceAPI (для сесії та оновлення listenKey).
            ws_log_level (int): Рівень логування для діагностики (не використовується безпосередньо aiohttp).
        """
        self.raw_streams = streams
        self.callbacks = callbacks
        self.api = api # Використовуємо переданий екземпляр Async BinanceAPI
        self.use_testnet = api.use_testnet # Беремо режим з API

        # Визначаємо базові URL для WebSocket (з API)
        self.ws_url_base = api.ws_url_base
        self.ws_combined_url_base = urljoin(self.ws_url_base, "/stream?streams=")
        self.ws_single_url_base = urljoin(self.ws_url_base, "/ws/")

        # Стан WebSocket
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None # З'єднання WebSocket
        self.main_task: Optional[asyncio.Task] = None # Головна задача обробки
        self.listen_key: Optional[str] = None
        self.listen_key_refresh_task: Optional[asyncio.Task] = None # Задача оновлення ключа

        # Стан з'єднання та перепідключення
        self.connected: bool = False
        self.reconnect_delay: float = 5.0 # Початкова затримка (float)

        # --- Налаштування логування (залишаємо для інформації, aiohttp логує через свій механізм) ---
        # aiohttp використовує стандартний logging, але має свої імена логерів ('aiohttp.client', 'aiohttp.web', etc.)
        # Можна налаштувати їх рівні окремо, якщо потрібно
        log_level_name = logging.getLevelName(ws_log_level)
        logger.info(f"AsyncWebSocketHandler: Налаштування логування для aiohttp (рівень: {log_level_name} або вище).")
        # Приклад налаштування логера aiohttp (якщо потрібно більше деталей):
        # logging.getLogger('aiohttp.client').setLevel(ws_log_level)

        logger.info(f"AsyncWebSocketHandler для потоків {streams} ініціалізовано.")

    # Наступні методи будуть додані в наступних блоках...

# END BLOCK 10

# BLOCK 11: Async WS Handler - Listen Key Management
    def set_listen_key(self, listen_key: Optional[str]):
        """
        Встановлює або скидає listenKey для User Data Stream.
        Керує задачею оновлення ключа та сигналізує про необхідність перепідключення.
        Цей метод сам по собі синхронний, але запускає/зупиняє асинхронні задачі.
        """
        # Використовуємо asyncio.Lock для безпечної зміни listen_key та стану задач
        # Оскільки цей метод може викликатися з іншого потоку (напр., BotCore),
        # а керує asyncio задачами, краще використовувати lock.
        # Однак, прямо зараз lock не використовується, покладаємось на GIL та
        # те, що оновлення listen_key відбувається не надто часто.
        # TODO: Розглянути використання asyncio.Lock або run_coroutine_threadsafe, якщо будуть проблеми.

        current_key = self.listen_key
        if listen_key == current_key:
            return # Ключ не змінився

        new_key_preview = listen_key[:5] if listen_key else "None"
        logger.info(f"AsyncWS: Встановлення нового listenKey: {new_key_preview}...")
        self.listen_key = listen_key

        # Зупиняємо попередню задачу оновлення (якщо є)
        # Потрібно робити це з циклу asyncio, якщо він є, або через run_coroutine_threadsafe
        # Поки що просто скасовуємо задачу, якщо вона існує
        if self.listen_key_refresh_task and not self.listen_key_refresh_task.done():
             logger.info("AsyncWS: Скасування попередньої задачі оновлення listenKey...")
             self.listen_key_refresh_task.cancel()
             # Не чекаємо тут завершення, щоб не блокувати

        # Запускаємо нову задачу оновлення, якщо ключ валідний
        if self.listen_key and self.api:
             # Запускаємо асинхронну задачу з основного потоку (або звідки викликається set_listen_key)
             # Це може потребувати наявності запущеного event loop
             try:
                 # Спробуємо отримати поточний цикл подій
                 loop = asyncio.get_running_loop()
                 # Запускаємо _start_listen_key_refresh_task в цьому циклі
                 loop.create_task(self._start_listen_key_refresh_task())
             except RuntimeError:
                 logger.error("AsyncWS: Немає активного event loop для запуску задачі оновлення listenKey.")
             except Exception as e:
                  logger.error(f"AsyncWS: Помилка запуску задачі оновлення listenKey: {e}")

        # Сигналізуємо основній задачі про необхідність перепідключення,
        # якщо вона вже запущена
        if self.main_task and not self.main_task.done():
            logger.info("AsyncWS: ListenKey змінено, сигналізуємо основній задачі про перепідключення...")
            # Найпростіший спосіб - скасувати поточне з'єднання WebSocket.
            # Цикл _run_loop() автоматично спробує перепідключитися з новим URL.
            ws_conn_to_close = self.ws # Отримуємо поточне посилання
            if ws_conn_to_close and not ws_conn_to_close.closed:
                 # Запускаємо закриття асинхронно, щоб не блокувати поточний потік
                 try:
                      loop = asyncio.get_running_loop()
                      # Викликаємо close() для об'єкту з'єднання
                      loop.create_task(ws_conn_to_close.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'Listen key updated'))
                      logger.debug("AsyncWS: Запущено задачу закриття WS для перепідключення.")
                 except RuntimeError:
                      logger.error("AsyncWS: Немає активного event loop для закриття WS з'єднання.")
                 except Exception as e:
                      logger.error(f"AsyncWS: Помилка закриття WS для перепідключення: {e}")

    def _get_stream_url(self) -> str:
        """Формує URL для WebSocket з'єднання."""
        active_market_streams = [s for s in self.raw_streams if s]
        all_active_streams = list(active_market_streams)
        if self.listen_key:
            all_active_streams.append(self.listen_key)

        if not all_active_streams:
            return ""
        else:
            streams_part = '/'.join(all_active_streams)
            return self.ws_combined_url_base + streams_part

    async def _start_listen_key_refresh_task(self):
        """Асинхронно створює та запускає задачу для оновлення listenKey."""
        # Перевіряємо ще раз умови тут, на випадок якщо стан змінився
        if not self.api or not self.listen_key:
            logger.warning("AsyncWS: Неможливо запустити задачу оновлення listenKey (API або ключ відсутні).")
            return
        # Перевіряємо, чи задача вже існує і не завершена
        if self.listen_key_refresh_task and not self.listen_key_refresh_task.done():
             logger.debug("AsyncWS: Задача оновлення listenKey вже запущена.")
             return

        logger.info("AsyncWS: Запуск нової задачі оновлення listenKey...")
        self.listen_key_refresh_task = asyncio.create_task(self._keep_listen_key_alive_loop())
        # Додаємо callback для обробки завершення задачі (опціонально)
        # self.listen_key_refresh_task.add_done_callback(self._handle_refresh_task_completion)

    async def _stop_listen_key_refresh_task(self):
        """Асинхронно скасовує та очікує завершення задачі оновлення listenKey."""
        task = self.listen_key_refresh_task
        if task and not task.done():
            logger.info("AsyncWS: Скасування задачі оновлення listenKey...")
            task.cancel()
            try:
                # Чекаємо завершення задачі (з таймаутом, щоб не блокувати надовго)
                await asyncio.wait_for(task, timeout=1.0)
                logger.debug("AsyncWS: Задача оновлення listenKey успішно скасована та завершена.")
            except asyncio.CancelledError:
                logger.debug("AsyncWS: Задача оновлення listenKey скасована (очікувано).")
            except asyncio.TimeoutError:
                 logger.warning("AsyncWS: Таймаут очікування завершення задачі оновлення listenKey.")
            except Exception as e:
                 logger.error(f"AsyncWS: Помилка під час очікування завершення задачі оновлення: {e}")
            finally:
                 self.listen_key_refresh_task = None # Скидаємо посилання
        else:
            logger.debug("AsyncWS: Немає активної задачі оновлення listenKey для зупинки.")
            self.listen_key_refresh_task = None # Про всяк випадок

    async def _keep_listen_key_alive_loop(self):
        """Асинхронний цикл, що періодично викликає keep_alive_user_data_stream."""
        task_name = asyncio.current_task().get_name() if asyncio.current_task() else "ListenKeyRefreshLoop"
        logger.info(f"AsyncWS ({task_name}): Потік оновлення listenKey розпочав роботу.")

        if not self.api:
            logger.error(f"AsyncWS ({task_name}): API відсутнє, зупинка.")
            return

        # Використовуємо інтервал з констант API
        refresh_wait_time = self.api.LISTEN_KEY_KEEP_ALIVE_INTERVAL * 0.85 # float

        while True: # Основний цикл перевірятиме на скасування
             current_key = self.listen_key # Отримуємо актуальний ключ на початку ітерації
             if not current_key:
                  logger.info(f"AsyncWS ({task_name}): ListenKey відсутній, завершення циклу.")
                  break

             try:
                 # Чекаємо перед наступним оновленням
                 wait_time_log = f"{refresh_wait_time:.1f}s"
                 key_preview_log = current_key[:5]
                 logger.debug(f"AsyncWS ({task_name}): Наступне оновлення lKey ({key_preview_log}...) через {wait_time_log}.")
                 # Використовуємо asyncio.sleep(), який можна перервати через скасування задачі
                 await asyncio.sleep(refresh_wait_time)

                 # Перевіряємо ключ ще раз (міг змінитися) і чи не скасовано задачу
                 # Перевірка на скасування не потрібна явно, бо await asyncio.sleep() підніме CancelledError
                 if self.listen_key == current_key:
                      logger.debug(f"AsyncWS ({task_name}): Спроба оновити listenKey: {key_preview_log}...")
                      # Викликаємо асинхронний метод API
                      update_success = await self.api.keep_alive_user_data_stream(current_key)

                      if not update_success:
                           logger.warning(f"AsyncWS ({task_name}): Не вдалося оновити listenKey! Можлива проблема зі з'єднанням або ключем.")
                           # Сигналізуємо про проблему основному циклу, встановивши connected=False
                           self.connected = False
                           # Можливо, варто спробувати отримати новий ключ? Поки що виходимо.
                           break # Виходимо з циклу оновлення
                 else:
                      logger.info(f"AsyncWS ({task_name}): ListenKey змінився під час очікування, цикл оновлення перезапуститься.")
                      break # Виходимо, set_listen_key має запустити новий цикл

             except asyncio.CancelledError:
                  logger.info(f"AsyncWS ({task_name}): Задача оновлення listenKey скасована.")
                  break # Виходимо з циклу при скасуванні
             except Exception as e:
                  logger.error(f"AsyncWS ({task_name}): Помилка в циклі оновлення listenKey: {e}", exc_info=True)
                  # Робимо паузу перед наступною спробою, щоб не заспамити логи
                  try:
                       await asyncio.sleep(60.0) # Пауза 1 хвилина
                  except asyncio.CancelledError:
                       logger.info(f"AsyncWS ({task_name}): Скасовано під час паузи після помилки.")
                       break

        logger.info(f"AsyncWS ({task_name}): Потік оновлення listenKey завершив роботу.")

# END BLOCK 11

# BLOCK 12: Async WS Handler - Main Task Management (start/stop)
    async def start(self):
        """Асинхронно запускає основну задачу обробки WebSocket."""
        if self.main_task and not self.main_task.done():
            logger.warning("AsyncWS: Спроба запустити, коли основна задача вже працює.")
            return

        logger.info("AsyncWS: Запуск основної задачі обробки WebSocket...")
        # Створюємо задачу, яка буде виконувати _run_loop
        self.main_task = asyncio.create_task(self._run_loop(), name="WebSocketRunLoop")
        # Додаємо callback, який буде викликано при завершенні задачі (штатному або через помилку)
        self.main_task.add_done_callback(self._handle_main_task_completion)
        logger.info("AsyncWS: Основну задачу створено та запущено.")

    async def stop(self):
        """Асинхронно зупиняє основну задачу та задачу оновлення listenKey."""
        logger.info("AsyncWS: Ініціювання зупинки WebSocket Handler...")

        # 1. Зупиняємо задачу оновлення listen key
        await self._stop_listen_key_refresh_task()

        # 2. Скасовуємо основну задачу обробки WebSocket
        task = self.main_task
        if task and not task.done():
            logger.info("AsyncWS: Скасування основної задачі обробки WebSocket...")
            task.cancel()
            try:
                # Чекаємо завершення задачі (з таймаутом)
                await asyncio.wait_for(task, timeout=2.0)
                logger.debug("AsyncWS: Основна задача обробки WebSocket успішно скасована та завершена.")
            except asyncio.CancelledError:
                logger.debug("AsyncWS: Основна задача обробки WebSocket скасована (очікувано).")
            except asyncio.TimeoutError:
                logger.warning("AsyncWS: Таймаут очікування завершення основної задачі обробки WebSocket.")
            except Exception as e:
                logger.error(f"AsyncWS: Помилка під час очікування завершення основної задачі: {e}")
            finally:
                 self.main_task = None # Скидаємо посилання
        else:
            logger.debug("AsyncWS: Немає активної основної задачі для зупинки.")
            self.main_task = None # Про всяк випадок

        # 3. Переконуємось, що стан connected = False
        # (має встановитися в finally блоці _run_loop або при помилці)
        if self.connected:
             logger.warning("AsyncWS: Статус connected все ще True після зупинки задач.")
             self.connected = False

        # 4. Закриття WebSocket з'єднання (має відбутися в _run_loop finally)
        # Якщо ws ще існує і не закритий, спробуємо закрити тут про всяк випадок
        ws_conn = self.ws
        if ws_conn and not ws_conn.closed:
             logger.warning("AsyncWS: Спроба закрити WS з'єднання в методі stop (мало б закритися раніше).")
             try:
                  # Закриваємо асинхронно, але не чекаємо довго, бо задача вже скасована
                  await asyncio.wait_for(ws_conn.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'Handler stopped'), timeout=1.0)
             except Exception as close_err:
                  logger.error(f"AsyncWS: Помилка примусового закриття WS в stop: {close_err}")
             finally:
                  self.ws = None

        logger.info("AsyncWS: Процедуру зупинки WebSocket Handler завершено.")

    def _handle_main_task_completion(self, task: asyncio.Task):
        """Callback для обробки завершення основної задачі _run_loop."""
        # Цей метод синхронний, бо викликається з add_done_callback
        try:
            # Перевіряємо, чи задача завершилася з винятком
            exception = task.exception()
            if exception and not isinstance(exception, asyncio.CancelledError):
                # Логуємо виняток, якщо він не є CancelledError
                logger.critical(f"AsyncWS: Основна задача ({task.get_name()}) завершилася з помилкою:", exc_info=exception)
            else:
                 # Логуємо нормальне завершення або скасування
                 logger.info(f"AsyncWS: Основна задача ({task.get_name()}) завершена. Скасовано: {task.cancelled()}.")
        except asyncio.CancelledError:
             # Сама add_done_callback може бути скасована? Малоймовірно, але обробимо.
             logger.info(f"AsyncWS: Callback завершення задачі ({task.get_name()}) скасовано.")
        except Exception as e:
            # Логуємо будь-які інші помилки всередині callback
            logger.error(f"AsyncWS: Помилка в _handle_main_task_completion для задачі {task.get_name()}: {e}", exc_info=True)

# END BLOCK 12

# BLOCK 13: Async WS Handler - Core Connection and Processing Loop
    async def _run_loop(self):
        """Основний асинхронний цикл для підключення та обробки повідомлень WebSocket."""
        task_name = asyncio.current_task().get_name() if asyncio.current_task() else "WebSocketRunLoop"
        logger.info(f"AsyncWS ({task_name}): Запуск основного циклу.")

        while True: # Нескінченний цикл перепідключення
            connection_closed_normally = False
            try:
                # Перевірка скасування на початку ітерації
                # --- ВИПРАВЛЕННЯ: Краще перевіряти через task.cancelled() ---
                # if asyncio.current_task().cancelled():
                current_task = asyncio.current_task()
                if current_task and current_task.cancelled():
                     logger.info(f"AsyncWS ({task_name}): Задача скасована перед підключенням.")
                     break

                # Перевірка наявності API та сесії
                if not self.api or not self.api.session or self.api.session.closed:
                     logger.error(f"AsyncWS ({task_name}): API або сесія aiohttp недоступні. Пауза...")
                     await asyncio.sleep(self.reconnect_delay)
                     self.reconnect_delay = min(self.reconnect_delay * 1.5, 60.0)
                     continue # Наступна спроба

                # Отримуємо URL
                stream_url = self._get_stream_url()
                if not stream_url:
                    logger.warning(f"AsyncWS ({task_name}): Немає потоків для підписки. Пауза 5 сек...")
                    await asyncio.sleep(5.0)
                    continue

                logger.info(f"AsyncWS ({task_name}): Спроба підключення до {stream_url}")
                # Встановлюємо з'єднання за допомогою сесії API
                async with self.api.session.ws_connect(stream_url, heartbeat=60) as ws:
                    # Успішне підключення
                    self.ws = ws # Зберігаємо посилання на з'єднання
                    self.connected = True
                    self.reconnect_delay = 5.0 # Скидаємо затримку при успішному підключенні
                    self._on_open() # Викликаємо callback
                    logger.info(f"AsyncWS ({task_name}): Успішно підключено.")

                    # Цикл обробки повідомлень
                    async for msg in ws:
                        # Перевірка скасування всередині циклу обробки
                        # --- ВИПРАВЛЕННЯ: Краще перевіряти через task.cancelled() ---
                        # if asyncio.current_task().cancelled():
                        if current_task and current_task.cancelled():
                             logger.info(f"AsyncWS ({task_name}): Задача скасована під час отримання повідомлень.")
                             # Закриваємо з'єднання перед виходом
                             await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b'Task cancelled')
                             connection_closed_normally = True # Вважаємо це нормальним закриттям у контексті скасування
                             break # Виходимо з циклу async for

                        # Обробляємо повідомлення
                        await self._on_message(msg) # <--- Зробили _on_message асинхронним

                        # Перевірка на помилки WebSocket
                        if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED, WSMsgType.CLOSING):
                             ws_exception = ws.exception()
                             log_msg = f"AsyncWS ({task_name}): Отримано {msg.type}."
                             if ws_exception:
                                 log_msg += f" Помилка: {ws_exception}."
                             logger.warning(log_msg + " Розрив з'єднання.")
                             if msg.type == WSMsgType.ERROR and ws_exception:
                                 self._on_error(ws_exception) # Передаємо виняток в обробник
                             connection_closed_normally = True # Вважаємо нормальним закриттям, якщо це не помилка з'єднання
                             break # Виходимо з циклу async for, щоб перепідключитися

                    # Якщо цикл async for завершився без break (тобто з'єднання закрито сервером або іншою стороною)
                    # --- ВИПРАВЛЕННЯ: Краще перевіряти через task.cancelled() ---
                    # if not asyncio.current_task().cancelled() and not connection_closed_normally:
                    if not (current_task and current_task.cancelled()) and not connection_closed_normally:
                         logger.warning(f"AsyncWS ({task_name}): Цикл обробки повідомлень завершився (з'єднання закрито?).")
                         connection_closed_normally = True # Вважаємо нормальним закриттям з боку сервера

            except asyncio.CancelledError:
                logger.info(f"AsyncWS ({task_name}): Основний цикл скасовано.")
                break # Виходимо з головного циклу while True
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"AsyncWS ({task_name}): Помилка підключення/таймаут WebSocket: {e}")
                self._on_error(e) # Викликаємо callback помилки
                # Пауза перед перепідключенням буде в блоці finally
            except Exception as e:
                logger.error(f"AsyncWS ({task_name}): Невідома помилка в основному циклі: {e}", exc_info=True)
                self._on_error(e) # Викликаємо callback помилки
                # Пауза перед перепідключенням буде в блоці finally
            finally:
                # Цей блок виконується завжди після завершення try або при виході з except
                logger.debug(f"AsyncWS ({task_name}): Блок finally основного циклу.")
                # Переконуємось, що стан оновлено
                was_connected = self.connected
                self.connected = False
                self.ws = None
                if was_connected: # Викликаємо on_close тільки якщо були підключені
                     self._on_close()

                # Перевіряємо, чи потрібно перепідключатися
                # Не перепідключаємось, якщо задача була скасована ззовні
                # --- ВИПРАВЛЕННЯ: Краще перевіряти через task.cancelled() ---
                # if asyncio.current_task().cancelled():
                current_task_final = asyncio.current_task() # Отримуємо задачу ще раз
                if current_task_final and current_task_final.cancelled():
                     logger.debug(f"AsyncWS ({task_name}): Задача скасована, перепідключення не потрібне.")
                     # Вихід з циклу while відбудеться через перевірку на початку або в except CancelledError
                else:
                     # Якщо не було скасування, чекаємо перед наступною спробою
                     delay_str = f"{self.reconnect_delay:.1f}"
                     logger.info(f"AsyncWS ({task_name}): Пауза перед перепідключенням {delay_str} сек...")
                     try:
                          await asyncio.sleep(self.reconnect_delay)
                          # Збільшуємо затримку для наступного разу
                          self.reconnect_delay = min(self.reconnect_delay * 1.5, 60.0)
                     except asyncio.CancelledError:
                          logger.info(f"AsyncWS ({task_name}): Скасовано під час паузи перед перепідключенням.")
                          break # Виходимо з головного циклу

        logger.info(f"AsyncWS ({task_name}): Основний цикл завершено.")

# END BLOCK 13

# BLOCK 14: Async WS Handler - Callback Methods
    def _on_open(self):
        """Callback-функція при відкритті WebSocket з'єднання."""
        # Статус connected та скидання reconnect_delay тепер в _run_loop
        logger.info(f"AsyncWS: З'єднання WebSocket ВІДКРИТО.")
        # Можна додати надсилання події про підключення, якщо потрібно

    async def _on_message(self, msg: aiohttp.WSMessage):
        """Асинхронний Callback при отриманні повідомлення."""
        if msg.type == WSMsgType.TEXT:
            message_text = msg.data
            msg_preview = message_text[:250]
            logger.debug(f"AsyncWS <~ RAW: {msg_preview}...")
            try:
                data = json.loads(message_text)
                is_dict_data = isinstance(data, dict)

                # Ігноруємо службові відповіді (напр., на підписку)
                if is_dict_data and ('result' in data or 'id' in data):
                    logger.debug(f"AsyncWS: Ігноруємо службове повідомлення: {data}")
                    return

                # Обробка комбінованого потоку
                elif is_dict_data and 'stream' in data and 'data' in data:
                    stream_name = data.get('stream')
                    payload = data.get('data')
                    event_time_ms = payload.get('E') if isinstance(payload, dict) else None
                    if not stream_name or payload is None:
                         logger.warning(f"AsyncWS: Порожні 'stream' або 'data' в комбінованому повідомленні.")
                         return

                    stream_parts = stream_name.split('@')
                    symbol_part = stream_parts[0]
                    symbol_upper = symbol_part.upper()
                    stream_type_part = stream_parts[1] if len(stream_parts) > 1 else None
                    logger.debug(f"AsyncWS: Подія: {stream_type_part}, Символ: {symbol_upper}")

                    callback_invoked = False
                    # --- Виклик відповідних callback-ів ---
                    # Klines
                    if stream_type_part and stream_type_part.startswith('kline'):
                        callback = self.callbacks.get('kline')
                        if callback:
                            kline_data = payload.get('k') if isinstance(payload, dict) else None
                            if kline_data:
                                try:
                                    # --- ЗМІНА: Викликаємо асинхронний callback через await ---
                                    # callback(symbol_upper, kline_data, event_time_ms) # Синхронний виклик
                                    await callback(symbol_upper, kline_data, event_time_ms) # Асинхронний виклик
                                    # --- КІНЕЦЬ ЗМІНИ ---
                                    callback_invoked = True
                                except Exception as cb_err: logger.error(f"AsyncWS: Помилка callback 'kline': {cb_err}", exc_info=True)
                            else: logger.warning(f"AsyncWS: Не знайдено 'k' в kline payload.")

                    # Trades
                    elif stream_type_part and 'trade' in stream_type_part:
                         callback = self.callbacks.get('trade')
                         if callback:
                              price_str = payload.get('p') if isinstance(payload, dict) else None
                              if price_str:
                                   try:
                                        price_float = float(price_str)
                                        # --- ЗМІНА: Викликаємо асинхронний callback через await (якщо він async) ---
                                        if asyncio.iscoroutinefunction(callback):
                                             await callback(symbol_upper, price_float, event_time_ms)
                                        else:
                                             callback(symbol_upper, price_float, event_time_ms) # Залишаємо синхронний виклик
                                        # --- КІНЕЦЬ ЗМІНИ ---
                                        callback_invoked = True
                                   except (ValueError, TypeError): logger.warning(f"AsyncWS: Помилка конвертації ціни '{price_str}' в trade.")
                                   except Exception as cb_err: logger.error(f"AsyncWS: Помилка callback 'trade': {cb_err}", exc_info=True)
                              else: logger.warning(f"AsyncWS: Не знайдено 'p' в trade payload.")

                    # Depth
                    elif stream_type_part and 'depth' in stream_type_part:
                         callback = self.callbacks.get('depth')
                         if callback:
                              try:
                                   # --- ЗМІНА: Викликаємо асинхронний callback через await (якщо він async) ---
                                   if asyncio.iscoroutinefunction(callback):
                                        await callback(symbol_upper, payload, event_time_ms)
                                   else:
                                        callback(symbol_upper, payload, event_time_ms) # Залишаємо синхронний виклик
                                   # --- КІНЕЦЬ ЗМІНИ ---
                                   callback_invoked = True
                              except Exception as cb_err: logger.error(f"AsyncWS: Помилка callback 'depth': {cb_err}", exc_info=True)

                    # Інші ринкові потоки
                    if not callback_invoked:
                         other_callback = self.callbacks.get('other_market')
                         if other_callback:
                              try:
                                   # --- ЗМІНА: Викликаємо асинхронний callback через await (якщо він async) ---
                                   if asyncio.iscoroutinefunction(other_callback):
                                        await other_callback(stream_name, payload)
                                   else:
                                        other_callback(stream_name, payload) # Залишаємо синхронний виклик
                                   # --- КІНЕЦЬ ЗМІНИ ---
                                   callback_invoked = True
                              except Exception as cb_err: logger.error(f"AsyncWS: Помилка callback 'other_market': {cb_err}", exc_info=True)

                    if not callback_invoked:
                         logger.warning(f"AsyncWS: Не знайдено обробника для ринкового потоку: {stream_name}")


                # Обробка User Data Stream
                elif is_dict_data and 'e' in data:
                    event_type = data['e']
                    event_time_ms_user = data.get('E')
                    logger.debug(f"AsyncWS: Подія користувача: {event_type}")
                    callback_invoked_user = False

                    if event_type == 'executionReport':
                        callback = self.callbacks.get('order_update')
                        if callback:
                            try:
                                 # --- ЗМІНА: Викликаємо асинхронний callback через await ---
                                 # callback(data, event_time_ms_user) # Синхронний виклик
                                 await callback(data, event_time_ms_user) # Асинхронний виклик
                                 # --- КІНЕЦЬ ЗМІНИ ---
                                 callback_invoked_user = True
                            except Exception as cb_err: logger.error(f"AsyncWS: Помилка callback 'order_update': {cb_err}", exc_info=True)

                    elif event_type in ['outboundAccountPosition', 'balanceUpdate']:
                        callback = self.callbacks.get('balance_update')
                        if callback:
                             try:
                                 # --- ЗМІНА: Викликаємо асинхронний callback через await ---
                                 # callback(data, event_time_ms_user) # Синхронний виклик
                                 await callback(data, event_time_ms_user) # Асинхронний виклик
                                 # --- КІНЕЦЬ ЗМІНИ ---
                                 callback_invoked_user = True
                             except Exception as cb_err: logger.error(f"AsyncWS: Помилка callback 'balance_update': {cb_err}", exc_info=True)

                    # Інші події користувача
                    if not callback_invoked_user:
                        other_callback = self.callbacks.get('other_user')
                        if other_callback:
                            try:
                                 # --- ЗМІНА: Викликаємо асинхронний callback через await (якщо він async) ---
                                 if asyncio.iscoroutinefunction(other_callback):
                                      await other_callback(event_type, data)
                                 else:
                                      other_callback(event_type, data) # Залишаємо синхронний виклик
                                 # --- КІНЕЦЬ ЗМІНИ ---
                                 callback_invoked_user = True
                            except Exception as cb_err: logger.error(f"AsyncWS: Помилка callback 'other_user': {cb_err}", exc_info=True)

                    if not callback_invoked_user:
                         logger.warning(f"AsyncWS: Не знайдено відповідного обробника для події користувача: {event_type}")

                # Якщо формат не розпізнано
                else:
                    logger.warning(f"AsyncWS: Отримано повідомлення невідомого JSON формату: {msg_preview}...")

            except json.JSONDecodeError:
                logger.error(f"AsyncWS: Помилка парсингу JSON: {msg_preview}...")
            except Exception as e:
                logger.error(f"AsyncWS: Помилка обробки повідомлення: {e}", exc_info=True)

        elif msg.type == WSMsgType.BINARY:
            logger.warning("AsyncWS: Отримано бінарне повідомлення (не очікується).")
        # Обробка WSMsgType.ERROR/CLOSED/CLOSING тепер відбувається в _run_loop

    def _on_error(self, error: Exception):
        """Callback-функція при виникненні помилки WebSocket."""
        # Логування помилки тепер відбувається в _run_loop перед викликом цього методу
        # Встановлення self.connected = False також відбувається в _run_loop
        logger.debug(f"AsyncWS: Викликано _on_error. Помилка: {error}")
        # Можна додати надсилання події про помилку, якщо потрібно

    def _on_close(self):
        """Callback-функція при закритті WebSocket з'єднання."""
        # Логування та встановлення self.connected = False тепер в _run_loop
        logger.info(f"AsyncWS: Викликано _on_close.")
        # Можна додати надсилання події про закриття, якщо потрібно

# END BLOCK 14
