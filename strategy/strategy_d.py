"""
strategy_d.py — Motor de la Estrategia D en producción
SMA_Log 288 / EMA 144 + Filtro ACP + Filtro Macro EMA200
Adaptado de backtesting a ejecución real con Binance Futures.
"""
import numpy as np
from datetime import datetime
from core.database import get_config, insert_signal, log_event
from core.binance_futures import binance


# ── Helpers matemáticos ───────────────────────────────────────────
def ema(arr: list, period: int) -> list:
    k = 2 / (period + 1)
    res = [0.0] * len(arr)
    if period <= len(arr):
        res[period - 1] = sum(arr[:period]) / period
    for i in range(period, len(arr)):
        res[i] = arr[i] * k + res[i - 1] * (1 - k)
    return res


def sma_log(closes: list, period: int) -> list:
    """SMA en espacio logarítmico (base 10) — núcleo del indicador."""
    import math
    n = len(closes)
    result = [0.0] * n
    log_c = [math.log10(max(c, 1e-10)) for c in closes]
    for i in range(period - 1, n):
        result[i] = 10 ** (sum(log_c[i - period + 1:i + 1]) / period)
    return result


def log_slope(sma_log_series: list, window: int = 20) -> list:
    """Pendiente normalizada de la SMA Log."""
    n = len(sma_log_series)
    result = [0.0] * n
    x = list(range(window))
    for i in range(window + 1, n):
        y = sma_log_series[i - window:i]
        if y[-1] > 0:
            # Pendiente de regresión lineal normalizada
            x_mean = sum(x) / window
            y_mean = sum(y) / window
            num = sum((x[j] - x_mean) * (y[j] - y_mean) for j in range(window))
            den = sum((x[j] - x_mean) ** 2 for j in range(window))
            slope = (num / den) if den != 0 else 0
            result[i] = slope / y[-1]
    return result


def acp_angle(ema50_series: list, ema200_series: list) -> list:
    """Ángulo de cruce de pendientes EMA50/EMA200."""
    import math
    n = len(ema50_series)
    result = [0.0] * n
    for i in range(1, n):
        if ema50_series[i - 1] > 0 and ema200_series[i - 1] > 0:
            s50  = (ema50_series[i] - ema50_series[i - 1]) / ema50_series[i - 1] * 100
            s200 = (ema200_series[i] - ema200_series[i - 1]) / ema200_series[i - 1] * 100
            angle = abs(
                math.degrees(math.atan(s50 / 100)) -
                math.degrees(math.atan(s200 / 100))
            )
            result[i] = angle
    return result


# ── Motor principal de señal ──────────────────────────────────────
class StrategyD:
    """
    Genera señales de trading en tiempo real usando los indicadores
    definidos en el backtesting validado sobre 52,608 velas reales.
    """

    def __init__(self):
        self._reload_params()

    def _reload_params(self):
        """Recarga parámetros desde la BD (sin reiniciar el bot)."""
        self.acp_thr      = float(get_config("acp_threshold", "0.04735"))
        self.sma_period   = int(get_config("sma_log_period", "288"))
        self.ema_period   = int(get_config("ema_period", "144"))
        self.macro_ema    = int(get_config("macro_ema", "200"))
        self.sl_pct       = float(get_config("sl_pct", "1.5")) / 100
        self.tp_pct       = float(get_config("tp_pct", "3.0")) / 100
        self.leverage     = int(get_config("leverage", "3"))
        self.risk_pct     = float(get_config("risk_pct", "1.0")) / 100
        self.symbol       = get_config("symbol", "BTCUSDT")
        self.min_candles  = max(self.sma_period, self.macro_ema) + 50

    def get_signal(self) -> dict:
        """
        Obtiene la señal actual descargando velas reales de Binance.
        Retorna: {direction, log_bias, acp_angle, macro_ok, slope_ok, reason}
        """
        self._reload_params()

        # Descargar velas 4H (necesitamos sma_period + buffer)
        limit = min(self.min_candles + 20, 1500)
        candles = binance.get_klines(self.symbol, "4h", limit)

        if len(candles) < self.min_candles:
            log_event("SIGNAL", f"Datos insuficientes: {len(candles)} velas", "WARNING")
            return {"direction": 0, "reason": "insufficient_data"}

        closes = [c["close"] for c in candles]
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        n = len(closes)

        # ── Calcular indicadores ──────────────────────────────────
        sml    = sma_log(closes, self.sma_period)
        e144   = ema(closes, self.ema_period)
        e200   = ema(closes, self.macro_ema)
        e50    = ema(closes, 50)
        slopes = log_slope(sml, 20)
        acps   = acp_angle(e50, e200)

        # Valores actuales (última vela)
        i = n - 1
        sml_now   = sml[i]
        sml_prev  = sml[i - 1]
        e144_now  = e144[i]
        e144_prev = e144[i - 1]
        e200_now  = e200[i]
        slope_now = slopes[i]
        acp_now   = acps[i]
        price     = closes[i]

        # ── Log Bias (dirección logarítmica) ─────────────────────
        cross_bull = sml_prev <= e144_prev and sml_now > e144_now
        cross_bear = sml_prev >= e144_prev and sml_now < e144_now

        if cross_bull:
            log_bias = 1
        elif cross_bear:
            log_bias = -1
        elif sml_now > e144_now and slope_now > 0:
            log_bias = 1
        elif sml_now < e144_now and slope_now < 0:
            log_bias = -1
        else:
            log_bias = 1 if sml_now > e144_now else -1

        # ── Filtro ACP ────────────────────────────────────────────
        acp_ok = acp_now >= self.acp_thr

        # ── Filtro Macro EMA200 ───────────────────────────────────
        macro_ok = (
            (log_bias == 1  and price > e200_now) or
            (log_bias == -1 and price < e200_now)
        )

        # ── Filtro de pendiente ───────────────────────────────────
        slope_ok = abs(slope_now) > 0.00001

        # ── Señal final ───────────────────────────────────────────
        direction = 0
        reason = "hold"

        if not acp_ok:
            reason = f"acp_too_low ({acp_now:.5f} < {self.acp_thr})"
        elif not macro_ok:
            reason = f"macro_filter (price={'above' if price > e200_now else 'below'} EMA200)"
        elif not slope_ok:
            reason = f"slope_flat ({slope_now:.6f})"
        else:
            direction = log_bias
            reason = "all_filters_passed"

        result = {
            "direction":  direction,
            "log_bias":   log_bias,
            "acp_angle":  round(acp_now, 6),
            "acp_ok":     acp_ok,
            "macro_ok":   macro_ok,
            "slope_ok":   slope_ok,
            "slope_val":  round(slope_now, 8),
            "price":      price,
            "sml_now":    round(sml_now, 4),
            "e144_now":   round(e144_now, 4),
            "e200_now":   round(e200_now, 4),
            "cross_bull": cross_bull,
            "cross_bear": cross_bear,
            "reason":     reason,
            "ts":         datetime.utcnow().isoformat(),
        }

        # Registrar señal en BD
        insert_signal({
            "direction":  direction,
            "log_bias":   log_bias,
            "acp_angle":  round(acp_now, 6),
            "macro_ok":   int(macro_ok),
            "slope_ok":   int(slope_ok),
            "executed":   0,
            "reason_skip": "" if direction != 0 else reason,
        })

        return result

    def calculate_order_params(self, direction: int, price: float, balance: float) -> dict:
        """
        Calcula los parámetros de la orden:
        - Tamaño de posición
        - Stop loss (precio)
        - Take profit (precio)
        - Precio de liquidación estimado (Binance, margen aislado)
        """
        risk_amount = balance * self.risk_pct
        sl_distance = price * self.sl_pct

        # Tamaño de posición basado en riesgo fijo
        # (cuánto BTC comprar para que el SL represente el 1% del capital)
        position_size_usdt = risk_amount / self.sl_pct
        position_size_usdt = min(position_size_usdt, balance * 0.95)
        quantity_btc = position_size_usdt / price
        quantity_btc = round(quantity_btc, 3)  # mínimo 0.001 BTC

        if direction == 1:  # LONG
            sl_price  = price * (1 - self.sl_pct)
            tp_price  = price * (1 + self.tp_pct)
            liq_price = price * (1 - (1/self.leverage - 0.004))
            # Stop-Limit: límite ligeramente por debajo del stop
            sl_limit  = sl_price * 0.999
        else:               # SHORT
            sl_price  = price * (1 + self.sl_pct)
            tp_price  = price * (1 - self.tp_pct)
            liq_price = price * (1 + (1/self.leverage - 0.004))
            sl_limit  = sl_price * 1.001

        return {
            "quantity":        quantity_btc,
            "position_usdt":   round(position_size_usdt, 2),
            "sl_price":        round(sl_price, 2),
            "sl_limit":        round(sl_limit, 2),
            "tp_price":        round(tp_price, 2),
            "liq_price":       round(liq_price, 2),
            "open_fee":        round(position_size_usdt * 0.0004, 4),
            "margin_required": round(position_size_usdt / self.leverage, 2),
        }


strategy = StrategyD()
