"""
main.py — Punto de entrada SATEVIS Bot
Arranca el scheduler del bot y el dashboard Flask en paralelo.
Railway ejecuta este archivo directamente.
"""
import os
import sys
import time
import threading
import schedule
from datetime import datetime, timezone

# Añadir raíz al path
sys.path.insert(0, os.path.dirname(__file__))

# ── INICIALIZAR BASE DE DATOS PRIMERO ─────────────────────────────
# Esto debe ir ANTES de importar cualquier módulo que use la BD
from core.database import init_db, get_config, log_event, record_capital

# Inicializar la base de datos inmediatamente
init_db()
print("✅ Base de datos lista")

# ── AHORA importar los demás módulos ─────────────────────────────
from core.binance_futures import binance
from core.executor import executor
from dashboard.dashboard import run_dashboard
import core.telegram as tg


# ── Ciclo principal del bot ───────────────────────────────────────
def bot_cycle():
    """
    Se ejecuta cada hora exacta.
    Análisis en 4H → señal → ejecutar si corresponde.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status = get_config("bot_status", "STOPPED")

    if status != "RUNNING":
        return  # Bot detenido, no hacer nada

    print(f"[{ts}] 🔄 Ciclo de análisis...")
    log_event("CYCLE_START", ts)

    try:
        result = executor.run_cycle()
        action = result.get("action", "unknown")
        print(f"[{ts}] ✅ Ciclo completado — acción: {action}")
        log_event("CYCLE_END", str(result))

        # Registrar capital actual periódicamente y detectar movimientos
        if action in ("hold", "monitoring", "idle"):
            balance = binance.get_balance()
            if balance > 0:
                from core.database import get_capital_history, add_capital_movement
                hist = get_capital_history(1)
                last_b = float(hist[0]["balance"]) if hist else 0.0
                diff = round(balance - last_b, 2)
                if abs(diff) >= 1.0:
                    mv_type = "DEPOSIT" if diff > 0 else "WITHDRAWAL"
                    desc = (f"Detectado automáticamente: "
                            f"{'depósito' if diff > 0 else 'retiro'} de ${abs(diff):.2f}")
                    add_capital_movement(mv_type, diff, desc, balance)
                    log_event("CAPITAL_AUTO",
                              f"{mv_type} ${diff:+.2f} | "
                              f"anterior=${last_b:.2f} → actual=${balance:.2f}")
                else:
                    record_capital(balance, "AUTO", "Ciclo periódico")

    except Exception as e:
        print(f"[{ts}] ❌ Error en ciclo: {e}")
        log_event("CYCLE_ERROR", str(e), "ERROR")
        try:
            tg.notify_error("CYCLE_ERROR", str(e))
        except Exception:
            pass


def health_check():
    """Cada 6 horas verifica conectividad con Binance."""
    ok = binance.ping()
    status = "OK" if ok else "FALLO"
    log_event("HEALTH_CHECK", f"Binance API: {status}",
              "INFO" if ok else "ERROR")
    print(f"[HEALTH] Binance API: {status}")


# ── Scheduler ────────────────────────────────────────────────────
def run_scheduler():
    """
    Corre en hilo separado.
    El ciclo del bot se lanza cada hora en el minuto 1
    (dar tiempo a que la vela de 4H cierre y los datos estén disponibles).
    """
    print("🕐 Scheduler iniciado")

    # Ciclo cada hora en el minuto :01
    schedule.every().hour.at(":01").do(bot_cycle)

    # Health check cada 6 horas
    schedule.every(6).hours.do(health_check)

    # Primer ciclo inmediato al arrancar
    time.sleep(5)
    bot_cycle()

    while True:
        schedule.run_pending()
        time.sleep(30)  # revisar cada 30 segundos


# ── Arranque ──────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  SATEVIS Bot — Sistema de Trading Automático BTC")
    print("  Estrategia D: Log+EMA · ACP · Macro · RR 3:1")
    print("=" * 55)

    # 1. Inicializar base de datos
    init_db()
    print("✅ Base de datos lista")

    # 2. Verificar conexión Binance (no bloquear si falla)
    testnet = get_config("testnet", "true")
    print(f"🔗 Conectando a Binance {'TESTNET' if testnet == 'true' else 'PRODUCCIÓN'}...")
    if binance.ping():
        print("✅ Binance conectado")
        log_event("STARTUP", f"Bot iniciado — modo {'testnet' if testnet=='true' else 'producción'}")
        try:
            tg.notify_bot_started()
        except Exception:
            pass
    else:
        print("⚠️  Sin conexión a Binance (configura API keys en el dashboard)")
        log_event("STARTUP", "Bot iniciado sin conexión Binance — API keys pendientes", "WARNING")

    # 3. Arrancar scheduler en hilo daemon
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print("✅ Scheduler activo — ciclo cada hora en :01")

    # 4. Arrancar dashboard Flask (bloquea el hilo principal)
    print("✅ Dashboard iniciando...")
    print(f"   URL: http://0.0.0.0:{os.getenv('PORT', 5000)}")
    print(f"   Usuario por defecto: admin / satevis2024")
    print(f"   ⚠️  Cambiar contraseña en el primer login")
    print("=" * 55)

    run_dashboard()


if __name__ == "__main__":
    main()
