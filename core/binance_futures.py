"""
binance_futures.py — Ejecutor de órdenes Binance Futures
Gestiona conexión, órdenes, posiciones y balance con manejo robusto de errores.
"""
import os
import time
import hmac
import hashlib
import requests
from datetime import datetime
from core.database import get_config, log_event


class BinanceFutures:
    """
    Cliente para Binance USDT-M Futures.
    Soporta testnet y producción automáticamente según configuración.
    """

    BASE_LIVE = "https://fapi.binance.com"
    BASE_TEST = "https://testnet.binancefuture.com"

    def __init__(self):
        self._refresh_keys()

    def _refresh_keys(self):
        """Recarga las keys desde la BD (permite actualización sin reiniciar)."""
        self.api_key    = get_config("binance_api_key", "")
        self.api_secret = get_config("binance_secret", "")
        testnet_str     = get_config("testnet", "true")
        self.testnet    = testnet_str.lower() == "true"
        self.base_url   = self.BASE_TEST if self.testnet else self.BASE_LIVE

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = sig
        return params

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}

    def _get(self, path: str, params: dict = None, signed: bool = False):
        self._refresh_keys()
        params = params or {}
        if signed:
            params = self._sign(params)
        try:
            r = requests.get(
                self.base_url + path,
                params=params,
                headers=self._headers(),
                timeout=10
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log_event("API_ERROR", str(e), "ERROR")
            return None

    def _post(self, path: str, params: dict):
        self._refresh_keys()
        params = self._sign(params)
        try:
            r = requests.post(
                self.base_url + path,
                params=params,
                headers=self._headers(),
                timeout=10
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log_event("ORDER_ERROR", str(e), "ERROR")
            return None

    # ── Cuenta ────────────────────────────────────────────────────
    def ping(self) -> bool:
        """Verifica conectividad."""
        r = self._get("/fapi/v1/ping")
        return r is not None

    def get_balance(self) -> float:
        """Balance disponible en USDT."""
        data = self._get("/fapi/v2/balance", signed=True)
        if not data:
            return 0.0
        for asset in data:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
        return 0.0

    def get_account_info(self) -> dict:
        """Información completa de la cuenta de futuros."""
        return self._get("/fapi/v2/account", signed=True) or {}

    def get_position(self, symbol: str = "BTCUSDT") -> dict | None:
        """Posición abierta actual."""
        data = self._get(
            "/fapi/v2/positionRisk",
            {"symbol": symbol},
            signed=True
        )
        if not data:
            return None
        for pos in data:
            if pos.get("symbol") == symbol and float(pos.get("positionAmt", 0)) != 0:
                return {
                    "symbol":       pos["symbol"],
                    "side":         "LONG" if float(pos["positionAmt"]) > 0 else "SHORT",
                    "qty":          abs(float(pos["positionAmt"])),
                    "entry_price":  float(pos["entryPrice"]),
                    "unrealized_pnl": float(pos["unRealizedProfit"]),
                    "liquidation_price": float(pos.get("liquidationPrice", 0)),
                    "leverage":     int(pos.get("leverage", 3)),
                    "margin_type":  pos.get("marginType", "isolated"),
                }
        return None

    def get_price(self, symbol: str = "BTCUSDT") -> float:
        """Precio actual."""
        data = self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"]) if data else 0.0

    # ── Configuración de apalancamiento y margen ──────────────────
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Configura el apalancamiento para un símbolo."""
        r = self._post("/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage
        })
        if r:
            log_event("LEVERAGE_SET", f"{symbol} → {leverage}×")
            return True
        return False

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> bool:
        """Configura margen aislado o cruzado."""
        r = self._post("/fapi/v1/marginType", {
            "symbol": symbol,
            "marginType": margin_type
        })
        if r or (r is None and "already" in str(r)):
            return True
        return False

    # ── Órdenes ───────────────────────────────────────────────────
    def place_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> dict | None:
        """
        Orden de mercado.
        side: 'BUY' o 'SELL'
        quantity: en BTC (mínimo 0.001 BTC en BTCUSDT perpetual)
        """
        params = {
            "symbol":   symbol,
            "side":     side,
            "type":     "MARKET",
            "quantity": round(quantity, 3),
        }
        r = self._post("/fapi/v1/order", params)
        if r:
            log_event("ORDER_PLACED",
                      f"{side} {quantity:.4f} {symbol} @ market | ID={r.get('orderId')}")
        return r

    def place_stop_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        limit_price: float,
        reduce_only: bool = True
    ) -> dict | None:
        """
        Stop-Limit para SL — más seguro que Stop-Market contra slippage.
        El limit_price debe ser ligeramente peor que stop_price (buffer del 0.1%).
        """
        params = {
            "symbol":        symbol,
            "side":          side,
            "type":          "STOP",
            "quantity":      round(quantity, 3),
            "stopPrice":     round(stop_price, 2),
            "price":         round(limit_price, 2),
            "reduceOnly":    "true" if reduce_only else "false",
            "timeInForce":   "GTC",
        }
        r = self._post("/fapi/v1/order", params)
        if r:
            log_event("SL_PLACED",
                      f"SL {side} {quantity:.4f} @ stop={stop_price:.2f} lim={limit_price:.2f}")
        return r

    def place_take_profit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        limit_price: float,
        reduce_only: bool = True
    ) -> dict | None:
        """Take-profit como orden TAKE_PROFIT_MARKET."""
        params = {
            "symbol":      symbol,
            "side":        side,
            "type":        "TAKE_PROFIT",
            "quantity":    round(quantity, 3),
            "stopPrice":   round(stop_price, 2),
            "price":       round(limit_price, 2),
            "reduceOnly":  "true" if reduce_only else "false",
            "timeInForce": "GTC",
        }
        r = self._post("/fapi/v1/order", params)
        if r:
            log_event("TP_PLACED",
                      f"TP {side} {quantity:.4f} @ stop={stop_price:.2f}")
        return r

    def cancel_all_orders(self, symbol: str) -> bool:
        """Cancela todas las órdenes abiertas de un símbolo."""
        r = self._post("/fapi/v1/allOpenOrders",
                       {"symbol": symbol})
        if r:
            log_event("ORDERS_CANCELLED", f"Todas las órdenes de {symbol} canceladas")
            return True
        return False

    def get_open_orders(self, symbol: str) -> list:
        return self._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True) or []

    def get_order(self, symbol: str, order_id: str) -> dict | None:
        return self._get(
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id},
            signed=True
        )

    # ── Klines (velas para el gráfico) ───────────────────────────
    def get_klines(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        limit: int = 200
    ) -> list:
        """
        Retorna velas OHLCV para el gráfico del dashboard.
        interval: '1m','5m','15m','1h','4h','1d'
        """
        data = self._get("/fapi/v1/klines", {
            "symbol":   symbol,
            "interval": interval,
            "limit":    limit
        })
        if not data:
            return []
        return [{
            "t":     c[0],
            "open":  float(c[1]),
            "high":  float(c[2]),
            "low":   float(c[3]),
            "close": float(c[4]),
            "vol":   float(c[5]),
        } for c in data]

    # ── Información de trades (historial real Binance) ─────────────
    def get_user_trades(self, symbol: str = "BTCUSDT", limit: int = 50) -> list:
        return self._get(
            "/fapi/v1/userTrades",
            {"symbol": symbol, "limit": limit},
            signed=True
        ) or []

    def get_income_history(self, income_type: str = "REALIZED_PNL", limit: int = 50) -> list:
        return self._get(
            "/fapi/v1/income",
            {"incomeType": income_type, "limit": limit},
            signed=True
        ) or []


# Instancia global
binance = BinanceFutures()
