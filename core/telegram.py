"""
telegram.py — Notificaciones por Telegram para SATEVIS Bot
Envía mensajes al abrir/cerrar operaciones, señales filtradas y errores críticos.
No requiere librerías adicionales — usa requests (ya en requirements.txt).
Si Telegram falla, el bot continúa operando sin interrupción.
"""
import requests
from datetime import datetime, timezone
from core.database import get_config, log_event


# ── Envío base ────────────────────────────────────────────────────
def _send(text: str) -> bool:
    """
    Envía un mensaje al chat configurado.
    Retorna True si fue exitoso, False si falló.
    El bot nunca se interrumpe por un fallo de Telegram.
    """
    token   = get_config("telegram_token", "")
    chat_id = get_config("telegram_chat_id", "")

    if not token or not chat_id:
        return False  # Telegram no configurado — silencio total

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "HTML",
            },
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        # Nunca dejar que Telegram rompa el bot
        log_event("TELEGRAM_ERROR", str(e), "WARNING")
        return False


# ── Helpers ───────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

def _mode() -> str:
    testnet = get_config("testnet", "true")
    return "🧪 TESTNET" if testnet == "true" else "🔴 PRODUCCIÓN"

def _pnl_emoji(pnl: float) -> str:
    return "🟢" if pnl >= 0 else "🔴"


# ── Mensajes ──────────────────────────────────────────────────────
def notify_trade_opened(trade: dict):
    """
    Notifica apertura de posición.

    trade keys: side, entry_price, quantity, size_usdt,
                sl_price, tp_price, liq_price, leverage,
                capital_before, acp_angle, open_fee
    """
    side      = trade.get("side", "?")
    entry     = trade.get("entry_price", 0)
    qty       = trade.get("quantity", 0)
    size      = trade.get("size_usdt", 0)
    sl        = trade.get("sl_price", 0)
    tp        = trade.get("tp_price", 0)
    liq       = trade.get("liq_price", 0)
    lev       = trade.get("leverage", 3)
    cap       = trade.get("capital_before", 0)
    risk_amt  = round(cap * float(get_config("risk_pct", "1.0")) / 100, 2)
    acp       = trade.get("acp_angle", 0)
    fee       = trade.get("open_fee", 0)
    side_icon = "↑" if side == "LONG" else "↓"
    side_col  = "📗" if side == "LONG" else "📕"

    msg = (
        f"{side_col} <b>SATEVIS — Nueva Operación</b>\n"
        f"{'─' * 28}\n"
        f"{side_icon} <b>{side} BTCUSDT</b> · {lev}× · {qty} BTC\n\n"
        f"💵 <b>Entrada:</b>  ${entry:,.2f}\n"
        f"🛑 <b>Stop Loss:</b> ${sl:,.2f}  "
        f"<i>({abs(entry-sl)/entry*100:.1f}%)</i>\n"
        f"🎯 <b>Take Profit:</b> ${tp:,.2f}  "
        f"<i>({abs(tp-entry)/entry*100:.1f}%)</i>\n"
        f"⚡ <b>Liquidación:</b> ${liq:,.2f}\n\n"
        f"💼 <b>Tamaño posición:</b> ${size:,.2f}\n"
        f"⚠️ <b>Capital en riesgo:</b> ${risk_amt}\n"
        f"💰 <b>Capital disponible:</b> ${cap:,.2f}\n"
        f"📐 <b>ACP ángulo:</b> {acp:.5f}°\n"
        f"💸 <b>Fee apertura:</b> ${fee:.4f}\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def notify_trade_closed(trade: dict):
    """
    Notifica cierre de posición (TP, SL o señal contraria).

    trade keys: side, entry_price, exit_price, pnl_net,
                pnl_pct, result, close_reason, duration_hours,
                capital_after, open_fee, close_fee, funding_cost
    """
    side     = trade.get("side", "?")
    entry    = trade.get("entry_price", 0)
    exit_p   = trade.get("exit_price", 0)
    pnl_net  = trade.get("pnl_net", 0)
    pnl_pct  = trade.get("pnl_pct", 0)
    result   = trade.get("result", "?")
    reason   = trade.get("close_reason", "?")
    dur_h    = trade.get("duration_hours", 0)
    cap_aft  = trade.get("capital_after", 0)
    fees     = (trade.get("open_fee", 0) or 0) + \
               (trade.get("close_fee", 0) or 0) + \
               (trade.get("funding_cost", 0) or 0)

    # Íconos por resultado
    if result == "WIN":
        res_icon = "✅"
        res_text = "GANADORA"
    elif result == "LOSS":
        res_icon = "❌"
        res_text = "PERDEDORA"
    else:
        res_icon = "⚡"
        res_text = "LIQUIDACIÓN"

    reason_map = {
        "TP":             "🎯 Take Profit alcanzado",
        "SL":             "🛑 Stop Loss activado",
        "SIGNAL_REVERSE": "🔄 Señal contraria",
        "LIQUIDATION":    "⚡ Liquidación forzosa",
        "MANUAL":         "👤 Cierre manual",
        "END":            "🏁 Fin del período",
    }
    reason_txt = reason_map.get(reason, reason)

    side_icon = "↑" if side == "LONG" else "↓"
    pnl_icon  = _pnl_emoji(pnl_net)

    # Duración legible
    if dur_h >= 24:
        dur_txt = f"{dur_h/24:.1f} días"
    else:
        dur_txt = f"{dur_h:.1f}h"

    msg = (
        f"{res_icon} <b>SATEVIS — Operación Cerrada</b>\n"
        f"{'─' * 28}\n"
        f"{side_icon} <b>{side} BTCUSDT</b> · {res_text}\n\n"
        f"📥 <b>Entrada:</b>  ${entry:,.2f}\n"
        f"📤 <b>Salida:</b>   ${exit_p:,.2f}\n"
        f"📊 <b>Movimiento:</b> {pnl_pct:+.2f}%\n\n"
        f"{pnl_icon} <b>PnL neto:</b> "
        f"{'+'if pnl_net>=0 else ''}${pnl_net:.2f}\n"
        f"💸 <b>Fees totales:</b> ${fees:.4f}\n"
        f"⏱ <b>Duración:</b> {dur_txt}\n"
        f"📋 <b>Razón:</b> {reason_txt}\n\n"
        f"💰 <b>Capital actual:</b> ${cap_aft:,.2f}\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def notify_liquidation(trade: dict):
    """
    Notificación especial para liquidaciones forzosas.
    Más urgente y destacada que el cierre normal.
    """
    entry   = trade.get("entry_price", 0)
    liq_p   = trade.get("liq_price", 0)
    loss    = trade.get("pnl_net", 0)
    cap     = trade.get("capital_after", 0)
    side    = trade.get("side", "?")

    msg = (
        f"⚡⚡ <b>SATEVIS — LIQUIDACIÓN FORZOSA</b> ⚡⚡\n"
        f"{'─' * 28}\n"
        f"<b>{side} BTCUSDT</b>\n\n"
        f"📥 <b>Entrada:</b>    ${entry:,.2f}\n"
        f"💥 <b>Liquidación:</b> ${liq_p:,.2f}\n\n"
        f"🔴 <b>Pérdida:</b> ${loss:.2f}\n"
        f"💰 <b>Capital restante:</b> ${cap:,.2f}\n\n"
        f"⚠️ Revisar el dashboard inmediatamente.\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def notify_signal_filtered(signal: dict):
    """
    Notifica señales evaluadas que NO se ejecutaron.
    Siempre se envía para señales SHORT bloqueadas (long_only_filter) —
    es una alerta de seguridad, no verbose opcional.
    Para el resto, solo se envía si telegram_notify_filtered = 'true'.
    """
    reason    = signal.get("reason", "desconocido")
    direction = signal.get("log_bias", 0)
    acp       = signal.get("acp_angle", 0)
    acp_thr   = float(get_config("acp_threshold", "0.04735"))
    macro_ok  = signal.get("macro_ok", False)
    acp_ok    = signal.get("acp_ok", False)
    slope_ok  = signal.get("slope_ok", False)

    # Señal SHORT bloqueada: siempre notificar — es una irregularidad de seguridad
    is_blocked_short = "long_only_filter" in reason

    # Para el resto, respetar preferencia del usuario
    if not is_blocked_short and get_config("telegram_notify_filtered", "false") != "true":
        return

    dir_txt  = "↑ LONG" if direction == 1 else "↓ SHORT"
    dir_icon = "📗" if direction == 1 else "🚨"

    filters = []
    if not acp_ok:
        filters.append(f"  ✗ ACP: {acp:.5f}° &lt; {acp_thr}°")
    else:
        filters.append(f"  ✓ ACP: {acp:.5f}°")
    if not macro_ok:
        filters.append("  ✗ Macro EMA200: en contra")
    else:
        filters.append("  ✓ Macro EMA200: OK")
    if not slope_ok:
        filters.append("  ✗ Pendiente: plana")
    else:
        filters.append("  ✓ Pendiente: OK")

    prefix = "🚨 <b>SATEVIS — SHORT BLOQUEADO</b>" if is_blocked_short else "○ <b>SATEVIS — Señal Filtrada</b>"

    msg = (
        f"{prefix}\n"
        f"{'─' * 28}\n"
        f"{dir_icon} <b>Dirección:</b> {dir_txt}\n\n"
        f"<b>Filtros:</b>\n"
        f"{chr(10).join(filters)}\n\n"
        f"<i>Razón: {reason}</i>\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def notify_signal_executed(signal: dict, trade_id: int):
    """
    Notifica que una señal LONG pasó todos los filtros y se va a ejecutar.
    Se envía ANTES de abrir la orden, como confirmación de decisión.
    """
    acp    = signal.get("acp_angle", 0)
    price  = signal.get("price", 0)
    e200   = signal.get("e200_now", 0)
    sml    = signal.get("sml_now", 0)

    msg = (
        f"📡 <b>SATEVIS — Señal LONG detectada</b>\n"
        f"{'─' * 28}\n"
        f"↑ <b>LONG BTCUSDT</b> · Todos los filtros OK\n\n"
        f"💵 <b>Precio:</b> ${price:,.2f}\n"
        f"📐 <b>ACP:</b> {acp:.5f}°\n"
        f"📊 <b>SMA Log:</b> ${sml:,.2f}\n"
        f"📈 <b>EMA 200:</b> ${e200:,.2f}\n\n"
        f"<i>Abriendo posición LONG #{trade_id}...</i>\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def notify_capital_change(type_: str, amount: float,
                          balance_before: float, balance_after: float,
                          description: str = ""):
    """
    Notifica cambios de capital detectados automáticamente:
    depósitos, retiros, o diferencias significativas de balance.
    type_: 'DEPOSIT' | 'WITHDRAWAL'
    """
    icon  = "💰" if type_ == "DEPOSIT" else "💸"
    label = "Depósito detectado" if type_ == "DEPOSIT" else "Retiro detectado"
    color = "+" if amount > 0 else ""

    msg = (
        f"{icon} <b>SATEVIS — {label}</b>\n"
        f"{'─' * 28}\n"
        f"<b>Movimiento:</b> {color}${amount:,.2f} USDT\n\n"
        f"📊 <b>Balance anterior:</b> ${balance_before:,.2f}\n"
        f"📊 <b>Balance actual:</b>   ${balance_after:,.2f}\n"
        f"{f'<i>{description}</i>' if description else ''}\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def notify_position_anomaly(position: dict, balance: float):
    """
    Notifica cuando se detecta una posición abierta en Binance que
    no está registrada en la BD del bot — puede indicar una operación
    manual, un error del bot anterior, o una irregularidad.
    """
    side  = position.get("side", "?")
    qty   = position.get("qty", 0)
    entry = position.get("entry_price", 0)
    upnl  = position.get("unrealized_pnl", 0)
    liq   = position.get("liquidation_price", 0)

    msg = (
        f"⚠️ <b>SATEVIS — Posición no registrada</b>\n"
        f"{'─' * 28}\n"
        f"Se detectó una posición en Binance que\n"
        f"<b>no está registrada</b> en la base de datos del bot.\n\n"
        f"<b>Posición detectada:</b>\n"
        f"  · Lado: <b>{side}</b>\n"
        f"  · Tamaño: {qty} BTC\n"
        f"  · Entrada: ${entry:,.2f}\n"
        f"  · PnL no realizado: {'+'if upnl>=0 else ''}${upnl:.2f}\n"
        f"  · Precio liquidación: ${liq:,.2f}\n\n"
        f"💰 <b>Balance cuenta:</b> ${balance:,.2f}\n\n"
        f"⚠️ Verificar y cerrar manualmente si corresponde.\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def notify_bot_started():
    """Notifica cuando el bot arranca correctamente."""
    testnet  = get_config("testnet", "true")
    mode     = "TESTNET 🧪" if testnet == "true" else "PRODUCCIÓN 🔴"
    symbol   = get_config("symbol", "BTCUSDT")
    lev      = get_config("leverage", "3")
    sl       = get_config("sl_pct", "1.5")
    tp       = get_config("tp_pct", "3.0")
    acp_thr  = get_config("acp_threshold", "0.04735")

    msg = (
        f"🚀 <b>SATEVIS Bot — Iniciado</b>\n"
        f"{'─' * 28}\n"
        f"📊 <b>Par:</b> {symbol}\n"
        f"⚙️ <b>Modo:</b> {mode}\n"
        f"📐 <b>Estrategia:</b> D · Log+EMA · ACP\n\n"
        f"<b>Parámetros activos:</b>\n"
        f"  · Apalancamiento: {lev}×\n"
        f"  · Stop Loss: {sl}%\n"
        f"  · Take Profit: {tp}%\n"
        f"  · ACP umbral: {acp_thr}°\n\n"
        f"🕐 {_ts()}"
    )
    _send(msg)


def notify_bot_stopped(reason: str = "Manual"):
    """Notifica cuando el bot se detiene."""
    msg = (
        f"⏹ <b>SATEVIS Bot — Detenido</b>\n"
        f"{'─' * 28}\n"
        f"<b>Razón:</b> {reason}\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def notify_error(event: str, detail: str):
    """Notifica errores críticos del bot."""
    if get_config("telegram_notify_errors", "true") != "true":
        return

    msg = (
        f"🆘 <b>SATEVIS — Error Crítico</b>\n"
        f"{'─' * 28}\n"
        f"<b>Evento:</b> {event}\n"
        f"<b>Detalle:</b> <code>{detail[:300]}</code>\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    _send(msg)


def test_connection() -> bool:
    """
    Envía un mensaje de prueba usando las credenciales almacenadas en BD.
    """
    token   = get_config("telegram_token", "")
    chat_id = get_config("telegram_chat_id", "")
    return test_connection_direct(token, chat_id)


def test_connection_direct(token: str, chat_id: str) -> bool:
    """
    Envía un mensaje de prueba con token y chat_id explícitos.
    Usado desde el dashboard al guardar la configuración, para garantizar
    que se prueba con los valores recién ingresados y no con los de BD.
    """
    if not token or not chat_id:
        return False

    msg = (
        f"✅ <b>SATEVIS — Conexión verificada</b>\n"
        f"{'─' * 28}\n"
        f"Las notificaciones de Telegram están\n"
        f"configuradas correctamente.\n\n"
        f"🕐 {_ts()} · {_mode()}"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        log_event("TELEGRAM_ERROR", f"test_connection_direct: {e}", "WARNING")
        return False
