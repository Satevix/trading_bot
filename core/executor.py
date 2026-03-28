"""
executor.py — Ejecutor de trades en producción
Conecta señales de la estrategia D con órdenes reales en Binance Futures.
Incluye gestión de posición abierta, SL, TP y detección de liquidaciones.
"""
from datetime import datetime, timezone
from core.database import (
    get_config, insert_trade, close_trade, get_open_trade,
    record_capital, log_event, get_trade_stats
)
from core.binance_futures import binance
from strategy.strategy_d import strategy
import core.telegram as tg


class TradeExecutor:

    def __init__(self):
        self.symbol = get_config("symbol", "BTCUSDT")

    def run_cycle(self) -> dict:
        """
        Ciclo completo de análisis y ejecución.
        Llamado por el scheduler cada hora (vela 4H cerrada).
        Retorna un resumen del ciclo para logging.
        """
        self.symbol = get_config("symbol", "BTCUSDT")
        bot_status  = get_config("bot_status", "STOPPED")

        if bot_status != "RUNNING":
            return {"action": "skipped", "reason": "bot_stopped"}

        # ── 1. Verificar si hay posición abierta en BD ────────────
        open_trade_db = get_open_trade()

        # ── 2. Verificar posición real en Binance ─────────────────
        live_position = binance.get_position(self.symbol)
        price_now     = binance.get_price(self.symbol)
        balance       = binance.get_balance()

        # ── 3. Detectar liquidación forzosa ───────────────────────
        if open_trade_db and not live_position:
            # Había posición en BD pero Binance ya no la muestra
            # → fue liquidada o cerrada externamente
            self._handle_unexpected_close(open_trade_db, price_now, balance)
            log_event("POSITION_CLOSED_EXTERNAL",
                      f"Posición {open_trade_db['id']} cerrada externamente (liquidación o manual)",
                      "WARNING")
            record_capital(balance)
            return {"action": "position_externally_closed", "balance": balance}

        # ── 4. Gestión de posición abierta ────────────────────────
        if open_trade_db and live_position:
            result = self._manage_open_position(open_trade_db, live_position, price_now, balance)
            return result

        # ── 5. Sin posición — evaluar nueva señal ─────────────────
        if not open_trade_db and not live_position:
            signal = strategy.get_signal()
            log_event("SIGNAL", str(signal))

            if signal["direction"] == 0:
                # Notificar señal filtrada (solo si está habilitado en config)
                try:
                    tg.notify_signal_filtered(signal)
                except Exception:
                    pass
                return {"action": "hold", "reason": signal["reason"], "signal": signal}

            # Señal válida → ejecutar
            result = self._open_position(signal, price_now, balance)
            record_capital(binance.get_balance())
            return result

        return {"action": "idle"}

    def _open_position(self, signal: dict, price: float, balance: float) -> dict:
        """Abre una nueva posición."""
        direction = signal["direction"]
        params    = strategy.calculate_order_params(direction, price, balance)

        if params["quantity"] < 0.001:
            log_event("ORDER_SKIP", "Cantidad mínima no alcanzada (< 0.001 BTC)", "WARNING")
            return {"action": "skipped", "reason": "min_quantity"}

        # Configurar apalancamiento y margen aislado
        lev = int(get_config("leverage", "3"))
        binance.set_leverage(self.symbol, lev)
        binance.set_margin_type(self.symbol, "ISOLATED")

        # Orden de entrada a mercado
        side = "BUY" if direction == 1 else "SELL"
        order = binance.place_market_order(
            self.symbol, side, params["quantity"]
        )

        if not order:
            log_event("ORDER_FAILED", f"Error al abrir {side}", "ERROR")
            return {"action": "error", "reason": "order_failed"}

        # Precio real de ejecución
        entry_price = float(order.get("avgPrice", price) or price)
        if entry_price == 0:
            entry_price = price

        # Colocar SL como Stop-Limit
        sl_side = "SELL" if direction == 1 else "BUY"
        binance.place_stop_limit_order(
            self.symbol, sl_side, params["quantity"],
            params["sl_price"], params["sl_limit"]
        )

        # Colocar TP
        binance.place_take_profit_order(
            self.symbol, sl_side, params["quantity"],
            params["tp_price"], params["tp_price"] * (0.999 if direction == 1 else 1.001)
        )

        # Registrar en BD
        now = datetime.now(timezone.utc).isoformat()
        trade_id = insert_trade({
            "binance_order_id": str(order.get("orderId", "")),
            "symbol":           self.symbol,
            "side":             "LONG" if direction == 1 else "SHORT",
            "entry_price":      round(entry_price, 2),
            "quantity":         params["quantity"],
            "size_usdt":        round(entry_price * params["quantity"], 2),
            "sl_price":         params["sl_price"],
            "tp_price":         params["tp_price"],
            "liq_price":        params["liq_price"],
            "leverage":         lev,
            "open_fee":         params["open_fee"],
            "signal_source":    "D_LOG_ACP",
            "acp_angle":        signal["acp_angle"],
            "log_bias":         signal["log_bias"],
            "opened_at":        now,
            "capital_before":   round(balance, 2),
        })

        log_event("TRADE_OPENED",
                  f"#{trade_id} {side} {params['quantity']} BTC @ ${entry_price:.2f} "
                  f"SL=${params['sl_price']:.2f} TP=${params['tp_price']:.2f}")

        # Notificación Telegram (no interrumpe si falla)
        try:
            tg.notify_trade_opened({
                "side":           "LONG" if direction == 1 else "SHORT",
                "entry_price":    entry_price,
                "quantity":       params["quantity"],
                "size_usdt":      params["position_usdt"],
                "sl_price":       params["sl_price"],
                "tp_price":       params["tp_price"],
                "liq_price":      params["liq_price"],
                "leverage":       lev,
                "capital_before": balance,
                "acp_angle":      signal.get("acp_angle", 0),
                "open_fee":       params["open_fee"],
            })
        except Exception:
            pass

        return {
            "action":    "opened",
            "trade_id":  trade_id,
            "side":      side,
            "entry":     entry_price,
            "quantity":  params["quantity"],
            "sl":        params["sl_price"],
            "tp":        params["tp_price"],
        }

    def _manage_open_position(
        self, trade_db: dict, live_pos: dict, price: float, balance: float
    ) -> dict:
        """
        Verifica si la posición activa debe cerrarse por señal contraria.
        El SL y TP están colocados como órdenes en Binance — se activan solos.
        Aquí solo gestionamos cierre por señal contraria.
        """
        direction = 1 if trade_db["side"] == "LONG" else -1
        signal    = strategy.get_signal()

        # Si la señal cambió a la dirección contraria → cierre por señal
        if signal["direction"] != 0 and signal["direction"] != direction:
            return self._close_position(trade_db, live_pos, price, balance, "SIGNAL_REVERSE")

        # Actualizar PnL no realizado en BD cada ciclo
        entry    = float(trade_db["entry_price"])
        qty      = float(trade_db["quantity"])
        lev      = int(trade_db.get("leverage", 3))
        raw_pnl  = (price / entry - 1) * direction * lev
        unreal   = qty * price * raw_pnl

        return {
            "action":       "monitoring",
            "trade_id":     trade_db["id"],
            "unrealized_pnl": round(unreal, 2),
            "price":        price,
            "signal":       signal["direction"],
        }

    def _close_position(
        self, trade_db: dict, live_pos: dict,
        price: float, balance: float, reason: str
    ) -> dict:
        """Cierra la posición activa enviando orden de cierre."""
        direction = 1 if trade_db["side"] == "LONG" else -1
        close_side = "SELL" if direction == 1 else "BUY"
        qty = live_pos["qty"]

        # Cancelar órdenes SL/TP pendientes
        binance.cancel_all_orders(self.symbol)

        # Orden de cierre a mercado
        order = binance.place_market_order(self.symbol, close_side, qty)
        if not order:
            log_event("CLOSE_FAILED", f"Error al cerrar trade #{trade_db['id']}", "ERROR")
            return {"action": "error"}

        exit_price = float(order.get("avgPrice", price) or price)
        if exit_price == 0:
            exit_price = price

        entry = float(trade_db["entry_price"])
        raw   = (exit_price / entry - 1) * direction
        size  = float(trade_db["size_usdt"])
        fees  = size * 0.0004  # close fee estimado
        pnl_gross = size * raw * int(trade_db.get("leverage", 3))
        pnl_net   = pnl_gross - fees

        opened_at = datetime.fromisoformat(trade_db["opened_at"].replace("Z", "+00:00"))
        now       = datetime.now(timezone.utc)
        duration  = (now - opened_at).total_seconds() / 3600

        close_trade(trade_db["id"], {
            "exit_price":    round(exit_price, 2),
            "close_fee":     round(fees, 4),
            "pnl_gross":     round(pnl_gross, 2),
            "pnl_net":       round(pnl_net, 2),
            "pnl_pct":       round(raw * 100, 2),
            "result":        "WIN" if pnl_net > 0 else "LOSS",
            "close_reason":  reason,
            "closed_at":     now.isoformat(),
            "duration_hours": round(duration, 2),
            "capital_after": round(balance + pnl_net, 2),
        })

        record_capital(balance + pnl_net)
        log_event("TRADE_CLOSED",
                  f"#{trade_db['id']} cerrado @ ${exit_price:.2f} "
                  f"PnL=${pnl_net:+.2f} ({raw*100:+.2f}%) Razón={reason}")

        # Notificación Telegram
        try:
            closed_data = {
                "side":           trade_db["side"],
                "entry_price":    float(trade_db["entry_price"]),
                "exit_price":     exit_price,
                "pnl_net":        pnl_net,
                "pnl_pct":        round(raw * 100, 2),
                "result":         "WIN" if pnl_net > 0 else "LOSS",
                "close_reason":   reason,
                "duration_hours": round(duration, 2),
                "capital_after":  round(balance + pnl_net, 2),
                "open_fee":       float(trade_db.get("open_fee") or 0),
                "close_fee":      round(fees, 4),
                "funding_cost":   0,
            }
            tg.notify_trade_closed(closed_data)
        except Exception:
            pass

        return {
            "action":    "closed",
            "trade_id":  trade_db["id"],
            "exit":      exit_price,
            "pnl_net":   round(pnl_net, 2),
            "reason":    reason,
        }

    def _handle_unexpected_close(
        self, trade_db: dict, price: float, balance: float
    ):
        """Maneja cierre inesperado (liquidación forzosa o cierre manual en Binance)."""
        entry     = float(trade_db["entry_price"])
        direction = 1 if trade_db["side"] == "LONG" else -1
        liq_price = float(trade_db.get("liq_price", 0) or 0)

        # Determinar si fue liquidación por precio
        was_liquidated = (
            liq_price > 0 and (
                (direction == 1  and price <= liq_price * 1.02) or
                (direction == -1 and price >= liq_price * 0.98)
            )
        )

        # Estimar PnL basado en precio actual
        raw       = (price / entry - 1) * direction
        size      = float(trade_db["size_usdt"])
        pnl_gross = size * raw * int(trade_db.get("leverage", 3))
        lev       = int(trade_db.get("leverage", 3))

        if was_liquidated:
            margin_init  = size / lev
            margin_maint = size * 0.004
            liq_fee      = size * 0.0005
            pnl_net = -(margin_init - margin_maint) - liq_fee
            reason  = "LIQUIDATION"
            result  = "LIQUIDATION"
        else:
            pnl_net = pnl_gross - size * 0.0004
            reason  = "EXTERNAL_CLOSE"
            result  = "WIN" if pnl_net > 0 else "LOSS"

        now = datetime.now(timezone.utc)
        opened_at = datetime.fromisoformat(trade_db["opened_at"].replace("Z", "+00:00"))
        duration = (now - opened_at).total_seconds() / 3600

        close_trade(trade_db["id"], {
            "exit_price":    round(price, 2),
            "pnl_gross":     round(pnl_gross, 2),
            "pnl_net":       round(pnl_net, 2),
            "pnl_pct":       round(raw * 100, 2),
            "result":        result,
            "close_reason":  reason,
            "closed_at":     now.isoformat(),
            "duration_hours": round(duration, 2),
            "capital_after": round(balance, 2),
        })

        # Notificación Telegram — urgente si fue liquidación
        try:
            notif_data = {
                "side":          trade_db["side"],
                "entry_price":   float(trade_db["entry_price"]),
                "exit_price":    round(price, 2),
                "liq_price":     float(trade_db.get("liq_price") or 0),
                "pnl_net":       round(pnl_net, 2),
                "pnl_pct":       round(raw * 100, 2),
                "result":        result,
                "close_reason":  reason,
                "duration_hours": round(duration, 2),
                "capital_after": round(balance, 2),
                "open_fee":      float(trade_db.get("open_fee") or 0),
                "close_fee":     0,
                "funding_cost":  0,
            }
            if was_liquidated:
                tg.notify_liquidation(notif_data)
            else:
                tg.notify_trade_closed(notif_data)
        except Exception:
            pass


executor = TradeExecutor()
