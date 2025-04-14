# modules/backtesting.py
# Фінальна версія з усіма виправленнями синтаксису (model-104)
import pandas as pd
from datetime import datetime, timezone, timedelta
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext, DivisionByZero
import requests
from modules.binance_integration import BinanceAPI, BinanceAPIError
from modules.logger import logger
from modules.config_manager import ConfigManager
import os
import matplotlib.pyplot as plt
import numpy as np
import math
import copy
import logging

# Імпортуємо BaseStrategy з обробкою помилки
try:
    from strategies.base_strategy import BaseStrategy
except ImportError:
    logger.error("Backtester: Не вдалося імпортувати BaseStrategy.")
    class BaseStrategy: # <-- Коректна заглушка
        pass

getcontext().prec = 28

class Backtester:
    """
    Клас для бек-тестування торгових стратегій.
    Приймає параметри стратегії, імітує TP/SL, використовує Decimal.
    """
    def __init__(self, binance_api: BinanceAPI | None, data_directory='data/backtest_data'):
        if binance_api is not None and not isinstance(binance_api, BinanceAPI):
             logger.error("BT: Передано невалідний об'єкт BinanceAPI!")
             self.binance_api = None
        else:
             self.binance_api = binance_api
        self.data_directory = data_directory
        os.makedirs(self.data_directory, exist_ok=True)
        self.config_manager = ConfigManager()
        self.symbol_info_cache = {}
        logger.info(f"Backtester ініціалізовано. Data dir: {self.data_directory}")

    # --- Приватні методи ---
    def _get_symbol_info(self, symbol: str) -> dict | None:
        if not self.binance_api:
            logger.warning("BT: API недоступний для _get_symbol_info")
            return None
        if symbol not in self.symbol_info_cache:
            logger.debug(f"BT: Запит інфо {symbol}...") # <-- Коректний символ
            try:
                exchange_info = self.binance_api.get_exchange_info(symbol=symbol)
                if exchange_info and 'symbols' in exchange_info and len(exchange_info['symbols']) == 1:
                    self.symbol_info_cache[symbol] = exchange_info['symbols'][0]
                    logger.debug(f"BT: Отримано інфо {symbol}.")
                else:
                    logger.error(f"BT: Не отримано інфо {symbol}. API:{exchange_info}")
                    self.symbol_info_cache[symbol] = None
            except Exception as e:
                logger.error(f"BT: Помилка інфо {symbol}: {e}", exc_info=True)
                self.symbol_info_cache[symbol] = None
        return self.symbol_info_cache[symbol]

    def _adjust_quantity_backtest(self, quantity: Decimal, symbol_info: dict | None) -> Decimal | None:
        if not symbol_info:
            return quantity
        lot_size_filter = next((f for f in symbol_info.get('filters',[]) if f.get('filterType')=='LOT_SIZE'),None) # <-- Коректні filters
        if not lot_size_filter:
            return quantity
        min_q = Decimal(lot_size_filter['minQty'])
        step = Decimal(lot_size_filter['stepSize'])
        max_q = Decimal(lot_size_filter['maxQty'])
        if quantity < min_q:
            logger.warning(f"[BT] Qty {quantity:.8f}<min {min_q}")
            return None
        quantity = min(quantity,max_q)
        if step > 0:
            quantity = (quantity//step)*step
        if quantity < min_q:
            logger.warning(f"[BT] Qty {quantity:.8f} after step<min {min_q}")
            return None
        return quantity

    def _check_min_notional_backtest(self, quantity: Decimal, price: Decimal, symbol_info: dict | None) -> bool:
        if not symbol_info:
             return True
        notional_filter = next((f for f in symbol_info.get('filters',[]) if f.get('filterType')=='MIN_NOTIONAL'),None) # <-- Коректні filters
        if not notional_filter:
             return True
        min_n = Decimal(notional_filter['minNotional'])
        if price is None or quantity is None:
             return False
        try:
            notional_value = quantity * price
            if notional_value < min_n:
                 logger.warning(f"[BT] Notional {notional_value:.4f}<min {min_n}")
                 return False
            return True
        except Exception as e:
             logger.error(f"Помилка MinNotional: q={quantity}, p={price}, e={e}")
             return False

    def _get_filename(self, symbol, interval, start_dt, end_dt):
        s = start_dt.strftime('%Y%m%d_%H%M%S')
        e = end_dt.strftime('%Y%m%d_%H%M%S')
        return f"{symbol}_{interval}_{s}_to_{e}.csv"

    # --- Завантаження даних ---
    def load_historical_data(self, symbol, interval, start_date_str, end_date_str):
        if not self.binance_api:
            logger.error("BT: BinanceAPI не доступний.")
            return pd.DataFrame()

        start_dt = None
        end_dt = None
        start_ts = 0
        end_ts = 0
        try:
            if isinstance(start_date_str,str):
                 start_dt = pd.to_datetime(start_date_str,utc=True)
            else:
                 start_dt = pd.to_datetime(start_date_str,unit='ms',utc=True)
            if isinstance(end_date_str,str):
                 end_dt = pd.to_datetime(end_date_str,utc=True)
            else:
                 end_dt = pd.to_datetime(end_date_str,unit='ms',utc=True)
            start_ts = int(start_dt.timestamp()*1000)
            end_ts = int(end_dt.timestamp()*1000)
        except Exception as e:
             logger.error(f"Некор. формат дати: {e}")
             return pd.DataFrame()

        filename = self._get_filename(symbol,interval,start_dt,end_dt)
        filepath = os.path.join(self.data_directory,filename)

        if os.path.exists(filepath):
            logger.info(f"Завантаження з файлу: {filepath}")
            try:
                df = pd.read_csv(filepath,index_col='timestamp',parse_dates=True)
                if df.index.tz is None:
                     df.index = df.index.tz_localize('UTC')
                cols = ['open','high','low','close','volume','quote_asset_volume','num_trades','taker_base_vol','taker_quote_vol']
                for c in cols:
                    if c in df.columns:
                         df[c] = df[c].apply(lambda x: Decimal(str(x)) if pd.notna(x) else Decimal('NaN'))
                df.dropna(subset=['open','high','low','close'], inplace=True)
                logger.info(f"Завантажено {len(df)} з файлу.")
                return df
            except Exception as e:
                logger.error(f"Помилка завантаження з файлу {filepath}: {e}")
        else:
            logger.info(f"Файл {filepath} не знайдено.")

        logger.info(f"Запит з API: {symbol}({interval}) з {start_dt} по {end_dt}")
        try:
            klines = self.binance_api.get_historical_klines(symbol,interval,start_ts=start_ts,end_ts=end_ts)
            if klines:
                cols = ['t','o','h','l','c','v','ct','qav','n','tbv','tqv','i']
                df = pd.DataFrame(klines,columns=cols[:len(klines[0])])
                df.rename(columns={'t':'timestamp','o':'open','h':'high','l':'low','c':'close','v':'volume','n':'num_trades','qav':'quote_asset_volume','tbv':'taker_base_vol','tqv':'taker_quote_vol'},inplace=True)
                df['timestamp'] = pd.to_datetime(df['timestamp'],unit='ms',utc=True)
                df.set_index('timestamp',inplace=True)
                num_cols = ['open','high','low','close','volume','quote_asset_volume','num_trades','taker_base_vol','taker_quote_vol']
                for c in num_cols:
                    if c in df.columns:
                         df[c] = pd.to_numeric(df[c],errors='coerce').apply(lambda x: Decimal(str(x)) if pd.notna(x) else Decimal('NaN'))
                df.drop(columns=['close_time','ignore'],inplace=True,errors='ignore') # ct and i are usually close_time and ignore
                df.dropna(subset=['open','high','low','close'],inplace=True)
                logger.info(f"Отримано {len(df)} з API.")
                try:
                    df.to_csv(filepath)
                    logger.info(f"Збережено: {filepath}")
                except Exception as e:
                    logger.error(f"Помилка збереження: {e}")
                return df
            else:
                logger.warning(f"Не отримано даних з API.")
                return pd.DataFrame()
        except BinanceAPIError as e:
             logger.error(f"API Помилка klines: {e}")
             return pd.DataFrame()
        except Exception as e:
             logger.error(f"Загальна помилка klines: {e}")
             return pd.DataFrame()

    # --- ОСНОВНИЙ МЕТОД БЕКТЕСТУ ---
    def run_backtest(self, symbol: str, interval: str, strategy_class: type[BaseStrategy],
                         start_date_str: str | int, end_date_str: str | int,
                         initial_capital: float, position_size_pct: float,
                         commission_pct: float, tp_pct: float, sl_pct: float,
                         strategy_params: dict | None = None # <-- В кінці
                        ) -> dict:
        log_prefix = f"Backtest [{strategy_class.__name__},{symbol},{interval}]"
        logger.info(f"{log_prefix} Запуск...")
        logger.info(f" Params: Cap={initial_capital}, Pos={position_size_pct*100:.1f}%, Comm={commission_pct*100:.3f}%, TP={tp_pct:.2f}%, SL={sl_pct:.2f}%")
        logger.info(f" Strategy Params: {strategy_params}")

        initial_capital_dec = Decimal(str(initial_capital))
        position_size = Decimal(str(position_size_pct))
        commission = Decimal(str(commission_pct))
        tp_ratio = Decimal(1) + Decimal(str(tp_pct))/100
        sl_ratio = Decimal(1) - Decimal(str(sl_pct))/100

        historical_data = self.load_historical_data(symbol,interval,start_date_str,end_date_str)
        if historical_data.empty:
             logger.warning(f"{log_prefix} Немає даних.")
             return {}

        symbol_info = self._get_symbol_info(symbol)
        if not symbol_info:
             logger.warning(f"{log_prefix} Не отримано інфо символу. Фільтри можуть працювати некоректно.")
        quote_asset = 'USDT'
        if symbol_info:
            quote_asset = symbol_info.get('quoteAsset','USDT')
        price_precision = 8
        if symbol_info:
            price_precision = int(symbol_info.get('quotePrecision', 8))

        # --- Ініціалізація стратегії З ПАРАМЕТРАМИ ---
        strategy = None
        required_data_length = 100
        try:
            strategy = strategy_class(symbol=symbol, config_manager=self.config_manager, config=strategy_params)
            logger.info(f"{log_prefix} Стратегію ініціалізовано з конфігом: {strategy.config}")
            required_data_length = getattr(strategy, 'required_klines_length', 100)
        except Exception as e:
            logger.error(f"{log_prefix} Помилка ініц. стратегії: {e}", exc_info=True)
            return {}

        # --- Стан бек-тесту ---
        quote_balance = initial_capital_dec
        base_balance = Decimal(0)
        in_position = False
        position_qty = Decimal(0)
        entry_price = Decimal(0)
        entry_time = None
        entry_commission_cost = Decimal(0)
        take_profit_price = Decimal(0)
        stop_loss_price = Decimal(0)
        trades = []
        balance_history = {}

        logger.info(f"{log_prefix} Початок циклу: {len(historical_data)} свічок...")
        # --- Цикл бек-тесту ---
        for current_time, row in historical_data.iterrows():
            open_p = row['open']
            high_p = row['high']
            low_p = row['low']
            close_p = row['close']
            if pd.isna(close_p) or pd.isna(open_p) or pd.isna(high_p) or pd.isna(low_p) or not isinstance(close_p, Decimal):
                logger.warning(f"{current_time}: Пропуск свічки з NaN.")
                continue

            current_portfolio_value = quote_balance + base_balance * close_p
            balance_history[current_time] = current_portfolio_value

            exit_signal_triggered = False
            exit_price = None
            exit_reason = None

            # 1. Перевірка TP/SL
            if in_position:
                sl_hit = low_p <= stop_loss_price
                tp_hit = high_p >= take_profit_price
                if sl_hit:
                    exit_price = stop_loss_price
                    exit_reason = "Stop Loss"
                    logger.debug(f"{current_time}: SL @{exit_price:.{price_precision}f}")
                elif tp_hit:
                    exit_price = take_profit_price
                    exit_reason = "Take Profit"
                    logger.debug(f"{current_time}: TP @{exit_price:.{price_precision}f}")
                if exit_price is not None:
                    exit_signal_triggered = True
                    exit_value = position_qty * exit_price
                    exit_commission = exit_value * commission
                    received_quote = exit_value - exit_commission
                    quote_balance += received_quote
                    base_balance = Decimal(0)
                    pnl = (exit_price - entry_price) * position_qty - entry_commission_cost - exit_commission
                    total_commission = entry_commission_cost + exit_commission
                    trades.append({'entry_time':entry_time,'entry_price':entry_price,'exit_time':current_time,'exit_price':exit_price,'quantity':position_qty,'side':'BUY','pnl_quote':pnl,'commission':total_commission,'exit_reason':exit_reason})
                    logger.info(f"{current_time}: {log_prefix} Закрито ({exit_reason}). PnL:{pnl:.4f}")
                    in_position = False
                    position_qty = Decimal(0)

            # 2. Сигнал стратегії
            if not exit_signal_triggered:
                signal = None
                try:
                    start_idx = max(0, historical_data.index.get_loc(current_time) - (required_data_length + 10))
                    data_for_strategy = historical_data.iloc[start_idx : historical_data.index.get_loc(current_time)+1]
                    if len(data_for_strategy) >= required_data_length:
                         signal = strategy.calculate_signals(data_for_strategy.copy())
                    # else: signal = None (вже None за замовчуванням)
                except Exception as e:
                    logger.error(f"{current_time}: Помилка стратегії: {e}", exc_info=True)
                    # signal залишається None

                # Обробка сигналу BUY
                if signal=='BUY' and not in_position:
                    quote_to_invest = quote_balance * position_size
                    entry_price_sim = close_p
                    if quote_to_invest > 0 and entry_price_sim > 0:
                        base_qty_gross = quote_to_invest / entry_price_sim
                        base_qty_adjusted = self._adjust_quantity_backtest(base_qty_gross, symbol_info)
                        if base_qty_adjusted and self._check_min_notional_backtest(base_qty_adjusted, entry_price_sim, symbol_info):
                            entry_commission = (base_qty_adjusted*entry_price_sim) * commission
                            quote_balance -= (base_qty_adjusted * entry_price_sim)
                            base_balance += base_qty_adjusted
                            in_position = True
                            position_qty = base_qty_adjusted
                            entry_price = entry_price_sim
                            entry_time = current_time
                            entry_commission_cost = entry_commission
                            take_profit_price = entry_price * tp_ratio
                            stop_loss_price = entry_price * sl_ratio
                            logger.info(f"{current_time}: {log_prefix} Відкрито BUY. Qty:{position_qty:.6f}@{entry_price:.{price_precision}f}. TP:{take_profit_price:.{price_precision}f}, SL:{stop_loss_price:.{price_precision}f}")

                # Обробка сигналу CLOSE
                elif signal == 'CLOSE' and in_position:
                    exit_price_sim = close_p
                    exit_reason = "Strategy Signal"
                    logger.debug(f"{current_time}: {log_prefix} Сигнал CLOSE.")
                    exit_signal_triggered = True # Встановлюємо прапорець, що угода закривається сигналом
                    exit_value = position_qty * exit_price_sim
                    exit_commission = exit_value * commission
                    received_quote = exit_value - exit_commission
                    quote_balance += received_quote
                    base_balance = Decimal(0)
                    pnl = (exit_price_sim - entry_price) * position_qty - entry_commission_cost - exit_commission
                    total_commission = entry_commission_cost + exit_commission
                    trades.append({'entry_time':entry_time,'entry_price':entry_price,'exit_time':current_time,'exit_price':exit_price_sim,'quantity':position_qty,'side':'BUY','pnl_quote':pnl,'commission':total_commission,'exit_reason':exit_reason})
                    logger.info(f"{current_time}: {log_prefix} Закрито ({exit_reason}). PnL:{pnl:.4f}")
                    in_position = False
                    position_qty = Decimal(0)

        # --- Кінець циклу ---
        logger.info(f"{log_prefix} Цикл завершено.")
        last_valid_close = Decimal(0)
        if not historical_data['close'].dropna().empty:
            last_valid_close = historical_data['close'].dropna().iloc[-1]

        final_portfolio_value = quote_balance + base_balance * last_valid_close
        if in_position:
            logger.warning(f"{log_prefix} Завершено з відкритою позицією.")
        profit_total = final_portfolio_value - initial_capital_dec
        profit_percentage = Decimal(0)
        if initial_capital_dec > 0:
            profit_percentage = (profit_total / initial_capital_dec) * 100

        # Конвертуємо balance_history в float ПЕРЕД збереженням
        balance_history_series = pd.Series(balance_history)
        balance_history_float = balance_history_series.astype(float)

        results = {
            'initial_capital':float(initial_capital_dec),
            'final_balance':float(final_portfolio_value),
            'total_profit':float(profit_total),
            'profit_percentage':float(profit_percentage),
            'trades':[self._decimal_trade_to_float(t) for t in trades],
            'balance_history':balance_history_float, # Зберігаємо вже float
            'parameters':{
                'symbol':symbol,
                'interval':interval,
                'strategy':strategy_class.__name__,
                'strategy_params':strategy_params,
                'start_date':str(start_date_str),
                'end_date':str(end_date_str),
                'position_size_pct':position_size_pct,
                'commission_pct':commission_pct,
                'tp_pct':tp_pct,
                'sl_pct':sl_pct
            }
        }
        logger.info(f"{log_prefix} Бек-тест завершено. Баланс:{results['final_balance']:.2f}, Прибуток:{results['profit_percentage']:.2f}%")
        metrics = self.calculate_metrics(results) # Передаємо results з balance_history як float
        results['metrics'] = metrics
        return results

    # --- Допоміжні методи ---
    def _decimal_trade_to_float(self, trade_dict: dict) -> dict:
        float_dict={}
        dt_keys=['entry_time','exit_time']
        for k,v in trade_dict.items():
            if isinstance(v,Decimal):
                 float_dict[k] = float(v)
            elif k in dt_keys and isinstance(v,(datetime,pd.Timestamp)):
                 float_dict[k] = v.isoformat()
            else:
                 float_dict[k] = v
        return float_dict

    def calculate_metrics(self, results: dict):
        # --- ПОВНИЙ КОД calculate_metrics з коректними відступами ---
        metrics = {}
        trades_df = pd.DataFrame(results.get('trades',[]))

        if 'pnl_quote' not in trades_df.columns:
            trades_df['pnl_quote'] = 0.0
        else:
            trades_df['pnl_quote'] = pd.to_numeric(trades_df['pnl_quote'],errors='coerce').fillna(0.0)

        balance_history = results.get('balance_history', pd.Series()) # Вже float
        initial_capital = results.get('initial_capital', 1.0)
        total_trades = len(trades_df)
        metrics['total_trades'] = total_trades

        if total_trades > 0:
            winning_df = trades_df[trades_df['pnl_quote'] > 1e-9]
            losing_df = trades_df[trades_df['pnl_quote'] < -1e-9]
            metrics['winning_trades'] = len(winning_df)
            metrics['losing_trades'] = len(losing_df)
            metrics['breakeven_trades'] = total_trades - metrics['winning_trades'] - metrics['losing_trades']
            if total_trades > 0:
                 metrics['win_rate'] = (metrics['winning_trades']/total_trades)*100
            else:
                 metrics['win_rate'] = 0.0

            total_pnl = trades_df['pnl_quote'].sum()
            metrics['total_pnl_quote'] = total_pnl
            if total_trades > 0:
                metrics['average_profit_per_trade'] = total_pnl/total_trades
            else:
                metrics['average_profit_per_trade'] = 0.0
            total_profit_wins = winning_df['pnl_quote'].sum()
            total_loss_losses = abs(losing_df['pnl_quote'].sum())
            if metrics['winning_trades'] > 0:
                metrics['average_win_amount'] = total_profit_wins / metrics['winning_trades']
            else:
                metrics['average_win_amount'] = 0.0
            if metrics['losing_trades'] > 0:
                metrics['average_loss_amount'] = total_loss_losses / metrics['losing_trades']
            else:
                metrics['average_loss_amount'] = 0.0
            if not trades_df.empty:
                metrics['largest_win'] = trades_df['pnl_quote'].max()
                metrics['largest_loss'] = trades_df['pnl_quote'].min()
            else:
                metrics['largest_win'] = 0.0
                metrics['largest_loss'] = 0.0

            try:
                if total_loss_losses > 0:
                    metrics['profit_factor'] = total_profit_wins / total_loss_losses
                else:
                    metrics['profit_factor'] = float('inf') # Або 0, якщо total_profit_wins теж 0
                if metrics['average_loss_amount'] > 0:
                    metrics['payoff_ratio'] = metrics['average_win_amount'] / metrics['average_loss_amount']
                else:
                    metrics['payoff_ratio'] = float('inf') # Або 0, якщо average_win_amount теж 0
            except DivisionByZero: # Хоча попередні перевірки мали б це покрити
                if total_profit_wins > 0:
                     metrics['profit_factor'] = float('inf')
                else:
                     metrics['profit_factor'] = 0.0
                if metrics['average_win_amount'] > 0:
                     metrics['payoff_ratio'] = float('inf')
                else:
                     metrics['payoff_ratio'] = 0.0
        else:
            metrics.update({k:0 for k in ['winning_trades','losing_trades','breakeven_trades','win_rate','total_pnl_quote','average_profit_per_trade','average_win_amount','average_loss_amount','largest_win','largest_loss','profit_factor','payoff_ratio']})

        if not balance_history.empty:
            returns = balance_history.pct_change().dropna()
            returns_numeric = pd.to_numeric(returns,errors='coerce').dropna()
            if not returns_numeric.empty:
                 # Max Drawdown
                 cum_ret = (1 + returns_numeric).cumprod()
                 peak = cum_ret.cummax()
                 if not peak.empty and peak.iloc[-1]!=0:
                      dd = (cum_ret - peak) / peak
                 else:
                      dd = pd.Series([0])
                 if not dd.empty:
                      metrics['max_drawdown_pct'] = float(dd.min()*100)
                 else:
                      metrics['max_drawdown_pct'] = 0.0

                 # Sharpe & Sortino Ratios
                 mean_ret = returns_numeric.mean()
                 std_dev = returns_numeric.std()
                 interval = results.get('parameters',{}).get('interval','1h')
                 ppy = Decimal(252) # Default annualization factor
                 try:
                     td = pd.to_timedelta(interval)
                     ppy = Decimal(pd.Timedelta('365 days')/td)
                 except Exception: # Keep default if parsing fails
                     pass
                 # Sharpe
                 sharpe_ratio = 0.0
                 if std_dev!=0 and std_dev > 1e-12:
                      sharpe_ratio = (mean_ret / std_dev) * math.sqrt(float(ppy))
                 metrics['sharpe_ratio'] = sharpe_ratio
                 # Sortino
                 downside_returns = returns_numeric[returns_numeric<0]
                 downside_std_dev = downside_returns.std()
                 sortino_ratio = 0.0
                 if downside_std_dev!=0 and downside_std_dev > 1e-12:
                      sortino_ratio = (mean_ret / downside_std_dev) * math.sqrt(float(ppy))
                 metrics['sortino_ratio'] = sortino_ratio
            else: # Якщо немає числових returns
                 metrics.update({'max_drawdown_pct':0.0,'sharpe_ratio':0.0,'sortino_ratio':0.0})
        else: # Якщо історія балансу порожня
             metrics.update({'max_drawdown_pct':0.0,'sharpe_ratio':0.0,'sortino_ratio':0.0})

        logger.info("Метрики розраховано.")
        # results['metrics'] = metrics; # Метрики вже додано в run_backtest
        return metrics

    def generate_report(self, results: dict):
        params = results.get('parameters',{})
        metrics = results.get('metrics',{})
        log_prefix = f"Backtest Report [{params.get('strategy','N/D')}]"
        logger.info(f"{log_prefix} Генерація звіту...")
        quote_asset = "USDT"
        sym = params.get('symbol')
        if sym:
            sym_info = self._get_symbol_info(sym)
            if sym_info:
                 quote_asset = sym_info.get('quoteAsset','USDT')
            # else: quote_asset remains 'USDT'

        report = f"""
===============================================================
 Звіт Бек-тесту: {params.get('strategy', 'Н/Д')}
===============================================================
Параметри Тесту:
  Символ: {params.get('symbol', 'Н/Д')} ({params.get('interval', 'Н/Д')})
  Період: {params.get('start_date', 'Н/Д')} - {params.get('end_date', 'Н/Д')}
  Капітал: {results.get('initial_capital', 0):.2f} {quote_asset}
  Позиція: {params.get('position_size_pct', 0)*100:.1f}% | Комісія: {params.get('commission_pct', 0)*100:.3f}%
  TP/SL: {params.get('tp_pct', 0.0):.1f}% / {params.get('sl_pct', 0.0):.1f}%
Стратегія Params: {params.get('strategy_params', {})}

Результати:
  Кінцевий баланс: {results.get('final_balance', 0):.2f} {quote_asset}
  Total PnL: {results.get('total_profit', 0):.2f} {quote_asset} ({results.get('profit_percentage', 0):.2f}%)

Угоди:
  Всього: {metrics.get('total_trades', 0)} (W:{metrics.get('winning_trades',0)} L:{metrics.get('losing_trades',0)} B:{metrics.get('breakeven_trades', 0)})
  Win Rate: {metrics.get('win_rate', 0):.2f}%
  Profit Factor: {metrics.get('profit_factor', 0.0):.2f}
  Payoff Ratio: {metrics.get('payoff_ratio', 0.0):.2f}

Середні Показники (на угоду):
  Avg PnL: {metrics.get('average_profit_per_trade', 0):.4f} {quote_asset}
  Avg Win: {metrics.get('average_win_amount', 0):.4f} {quote_asset}
  Avg Loss: {metrics.get('average_loss_amount', 0):.4f} {quote_asset}
  Largest Win: {metrics.get('largest_win', 0):.4f} {quote_asset}
  Largest Loss: {metrics.get('largest_loss', 0):.4f} {quote_asset}

Ризик:
  Max Drawdown: {metrics.get('max_drawdown_pct', 0):.2f}%
  Sharpe Ratio: {metrics.get('sharpe_ratio', 0):.2f}
  Sortino Ratio: {metrics.get('sortino_ratio', 0):.2f}
==============================================================="""
        print(report)
        return report

    def plot_results(self, results: dict):
        # --- ПОВНИЙ КОД plot_results З КОРЕКТНИМИ ВІДСТУПАМИ ---
        balance_history = results.get('balance_history', pd.Series()) # Вже float
        params = results.get('parameters',{})
        metrics = results.get('metrics',{})
        log_prefix = f"Backtest Plot [{params.get('strategy','N/D')}]" # <-- ВИПРАВЛЕНО
        if balance_history.empty: # <-- ВИПРАВЛЕНО
            logger.warning(f"{log_prefix} Історія балансу порожня.")
            return None
        logger.info(f"{log_prefix} Створення графіку...")
        fig, ax = plt.subplots(figsize=(12,6))
        try:
            quote_asset = "USDT"
            sym = params.get('symbol')
            if sym:
                sym_info = self._get_symbol_info(sym)
                if sym_info:
                    quote_asset = sym_info.get('quoteAsset','USDT')

            # Перевірка та конвертація індексу, якщо він не DatetimeIndex
            if not isinstance(balance_history.index, pd.DatetimeIndex):
                 balance_history.index = pd.to_datetime(balance_history.index)

            ax.plot(balance_history.index.to_numpy(), balance_history.to_numpy(), label=f'Баланс ({params.get("symbol", "")})', color='blue') # Не потрібно astype(float)
            title = (f"Крива балансу: {params.get('strategy','N/Д')}({params.get('symbol','N/Д')})\n"
                     f"Profit:{results.get('profit_percentage',0):.2f}%|WinRate:{metrics.get('win_rate',0):.2f}%|Sharpe:{metrics.get('sharpe_ratio',0):.2f}|MaxDD:{metrics.get('max_drawdown_pct',0):.2f}%")
            ax.set_title(title)
            ax.set_xlabel('Час')
            ax.set_ylabel(f'Баланс ({quote_asset})')
            ax.legend()
            ax.grid(True)
            plt.tight_layout()
            return fig
        except Exception as plot_e:
            logger.error(f"Помилка графіку Matplotlib: {plot_e}", exc_info=True)
            return None


# --- Блок if __name__ == '__main__' ---
if __name__ == '__main__':
    from modules.logger import setup_logger
    from decimal import getcontext
    from strategies.moving_average_crossover import MovingAverageCrossoverStrategy

    logger = setup_logger(level=logging.DEBUG, name="BacktesterTestLogger") # Виправлено: використовуємо logger = ...
    getcontext().prec = 28
    logger.info("Тест Backtester...")
    class MockBinanceAPI:
        def get_historical_klines(self, symbol, interval, start_ts, end_ts, limit=1000): # <-- ВИПРАВЛЕНО ТУТ
            logger.debug(f"Mock: get_klines {symbol} {interval} from {start_ts} to {end_ts}")
            klines = []
            p = Decimal('50000') # Змінено на '50000' для більшої реалістичності
            ct = start_ts
            while ct <= end_ts:
                op = p
                hp = p + Decimal(np.random.uniform(0,100))
                lp = p - Decimal(np.random.uniform(0,100))
                cp = p + Decimal(np.random.uniform(-80,80))
                v = Decimal(np.random.uniform(1,10))
                klines.append([ct,str(op),str(hp),str(lp),str(cp),str(v),ct+59999,'0',0,'0','0','0'])
                p = cp
                ct += 60000 # 1 хв
            return klines
        def get_server_time(self):
            return int(time.time()*1000)
        def get_exchange_info(self, symbol=None):
            return {'symbols': [{'symbol':'BTCUSDT','baseAsset':'BTC','quoteAsset':'USDT','quotePrecision':8,'baseAssetPrecision':8,'filters':[{'filterType':'LOT_SIZE','minQty':'0.00001','maxQty':'100','stepSize':'0.00001'},{'filterType':'MIN_NOTIONAL','minNotional':'10.0'}]}]}

    binance_api = MockBinanceAPI()
    backtester = Backtester(binance_api)

    sym = 'BTCUSDT'
    inter = '1m'
    start_d = int((datetime.now() - timedelta(days=1)).timestamp()*1000)
    end_d = int(datetime.now().timestamp()*1000)
    cap = 10000.0
    p_size = 0.1
    comm = 0.001
    tp = 1.5
    sl = 0.8
    strategy_cls = MovingAverageCrossoverStrategy
    strat_prms = {'fast_period': 10, 'slow_period': 20}
    # Викликаємо з усіма аргументами
    results = backtester.run_backtest(
        sym, inter, strategy_cls,
        start_date_str=start_d, end_date_str=end_d,
        initial_capital=cap, position_size_pct=p_size,
        commission_pct=comm, tp_pct=tp, sl_pct=sl,
        strategy_params=strat_prms
    )
    if results:
         report_str = backtester.generate_report(results)
         figure = backtester.plot_results(results)
         if figure:
             try:
                 plt.show()
             except Exception as e:
                 logger.error(f"Не показано графік: {e}") # <-- Повний рядок
    else:
         logger.error("Бек-тест не повернув результатів.")
    logger.info("Завершення тесту бектестера.")
