"""
dashboard.py — Portal web SATEVIS con autenticación y métricas completas.
Acceso privado con usuario/contraseña.
Actualización automática cada 30 segundos + WebSocket para precio BTC en vivo.
"""
import os
import json
import functools
from datetime import datetime, timezone
from flask import (
    Flask, render_template_string, request, redirect,
    url_for, session, jsonify, flash
)
from core.database import (
    verify_user, get_all_config, set_config, get_config,
    get_trades, get_trade_stats, get_capital_history,
    get_capital_movements, add_capital_movement,
    get_recent_signals, get_events, log_event,
    change_password, record_capital, init_db
)
from core.binance_futures import binance
import core.telegram as tg

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "satevis_secret_change_me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


# ── Auth decorator ────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Login / Logout ────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if verify_user(username, password):
            session["logged_in"] = True
            session["username"]  = username
            log_event("LOGIN", f"Usuario '{username}' inició sesión")
            return redirect(url_for("dashboard"))
        error = "Usuario o contraseña incorrectos"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Dashboard principal ───────────────────────────────────────────
@app.route("/")
@login_required
def dashboard():
    return render_template_string(DASHBOARD_HTML,
                                  username=session.get("username", ""))


# ── APIs JSON para el frontend ────────────────────────────────────
@app.route("/api/summary")
@login_required
def api_summary():
    """Resumen principal: capital, PnL, WR."""
    try:
        balance_live  = binance.get_balance()
        price_now     = binance.get_price(get_config("symbol", "BTCUSDT"))
        open_position = binance.get_position(get_config("symbol", "BTCUSDT"))
        stats         = get_trade_stats()
        cap_history   = get_capital_history(1)
        cap_initial   = float(get_config("capital_initial", "1000"))

        # PnL no realizado de posición abierta
        unrealized = 0.0
        if open_position:
            unrealized = open_position.get("unrealized_pnl", 0)

        total_value   = balance_live + unrealized
        total_pnl_net = stats.get("total_pnl_net") or 0
        total_fees    = stats.get("total_fees") or 0
        total_pnl_gross = stats.get("total_pnl_gross") or 0

        n_trades = stats.get("total") or 0
        wins     = stats.get("wins") or 0
        liqs     = stats.get("liquidations") or 0
        wr       = round(wins / max(n_trades, 1) * 100, 1)

        return jsonify({
            "ok":             True,
            "balance_live":   round(balance_live, 2),
            "capital_initial": cap_initial,
            "total_value":    round(total_value, 2),
            "unrealized_pnl": round(unrealized, 2),
            "pnl_gross":      round(total_pnl_gross, 2),
            "pnl_net":        round(total_pnl_net, 2),
            "total_fees":     round(total_fees, 2),
            "total_trades":   n_trades,
            "wins":           wins,
            "losses":         (stats.get("losses") or 0),
            "liquidations":   liqs,
            "win_rate":       wr,
            "avg_win_pct":    round(stats.get("avg_win_pct") or 0, 2),
            "avg_loss_pct":   round(stats.get("avg_loss_pct") or 0, 2),
            "best_trade":     round(stats.get("best_trade") or 0, 2),
            "worst_trade":    round(stats.get("worst_trade") or 0, 2),
            "btc_price":      price_now,
            "open_position":  open_position,
            "bot_status":     get_config("bot_status", "STOPPED"),
            "testnet":        get_config("testnet", "true"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/trades")
@login_required
def api_trades():
    """Historial completo de trades."""
    limit  = int(request.args.get("limit", 200))
    offset = int(request.args.get("offset", 0))
    trades = get_trades(limit, offset)
    return jsonify({"ok": True, "trades": trades})


@app.route("/api/candles")
@login_required
def api_candles():
    """Velas para el gráfico con operaciones marcadas."""
    interval = request.args.get("interval", "1h")
    limit    = int(request.args.get("limit", 200))
    symbol   = get_config("symbol", "BTCUSDT")
    candles  = binance.get_klines(symbol, interval, limit)

    # Obtener trades para marcar en el gráfico
    trades = get_trades(500)
    markers = []
    for t in trades:
        if t.get("opened_at"):
            markers.append({
                "ts":    t["opened_at"],
                "type":  "open",
                "side":  t["side"],
                "price": t["entry_price"],
                "id":    t["id"],
            })
        if t.get("closed_at"):
            markers.append({
                "ts":    t["closed_at"],
                "type":  "close",
                "side":  t["side"],
                "price": t["exit_price"],
                "result": t.get("result"),
                "id":    t["id"],
            })

    return jsonify({"ok": True, "candles": candles, "markers": markers})


@app.route("/api/capital")
@login_required
def api_capital():
    """Curva de equity, movimientos automáticos detectados y balance en vivo."""
    history   = get_capital_history(500)
    movements = get_capital_movements(100)
    balance   = binance.get_balance()
    return jsonify({
        "ok":            True,
        "history":       history,
        "movements":     movements,
        "balance_live":  round(balance, 2),
    })


@app.route("/api/capital/snapshot", methods=["POST"])
@login_required
def api_capital_snapshot():
    """
    Toma un snapshot del balance actual desde Binance y lo registra.
    Si detecta una diferencia significativa vs el último registro,
    la clasifica como DEPOSIT o WITHDRAWAL automáticamente.
    """
    balance = binance.get_balance()
    if balance <= 0:
        return jsonify({"ok": False, "error": "Sin conexión a Binance o balance cero"})

    history = get_capital_history(1)
    last_balance = float(history[0]["balance"]) if history else 0.0
    diff = round(balance - last_balance, 2)

    movement_type = None
    if abs(diff) >= 1.0:  # umbral: diferencias < $1 se ignoran (fees, funding)
        movement_type = "DEPOSIT" if diff > 0 else "WITHDRAWAL"
        desc = (f"Detectado automáticamente: "
                f"{'depósito' if diff > 0 else 'retiro'} de ${abs(diff):.2f}")
        add_capital_movement(movement_type, diff, desc, balance)
        log_event("CAPITAL_AUTO",
                  f"{movement_type} detectado: ${diff:+.2f} | "
                  f"balance_anterior=${last_balance:.2f} → balance_actual=${balance:.2f}")
    else:
        record_capital(balance, "AUTO", "Snapshot periódico")

    return jsonify({
        "ok":           True,
        "balance":      round(balance, 2),
        "prev_balance": round(last_balance, 2),
        "diff":         diff,
        "movement":     movement_type,
    })


@app.route("/api/indicators")
@login_required
def api_indicators():
    """
    Calcula SMA_Log(144) y EMA(200) sobre velas reales de Binance
    para superponerlas en el gráfico de velas del dashboard.
    Devuelve series de tiempo con timestamps y valores.
    """
    import math
    interval = request.args.get("interval", "1h")
    limit    = int(request.args.get("limit", 400))
    symbol   = get_config("symbol", "BTCUSDT")

    candles = binance.get_klines(symbol, interval, min(limit, 1000))
    if not candles:
        return jsonify({"ok": False, "error": "Sin datos de velas"})

    closes = [c["close"] for c in candles]
    times  = [c["t"] for c in candles]
    n      = len(closes)

    # ── SMA_Log(144) ─────────────────────────────────────────────
    sml_period = int(get_config("sma_log_period", "144"))
    log_c = [math.log10(max(x, 1e-10)) for x in closes]
    sml_series = []
    for i in range(n):
        if i >= sml_period - 1:
            val = 10 ** (sum(log_c[i - sml_period + 1:i + 1]) / sml_period)
            sml_series.append({"t": times[i], "v": round(val, 2)})

    # ── EMA(200) ──────────────────────────────────────────────────
    ema_period = int(get_config("macro_ema", "200"))
    k = 2 / (ema_period + 1)
    ema_vals = [0.0] * n
    if ema_period <= n:
        ema_vals[ema_period - 1] = sum(closes[:ema_period]) / ema_period
    for i in range(ema_period, n):
        ema_vals[i] = closes[i] * k + ema_vals[i - 1] * (1 - k)

    ema_series = [
        {"t": times[i], "v": round(ema_vals[i], 2)}
        for i in range(ema_period - 1, n)
        if ema_vals[i] > 0
    ]

    return jsonify({
        "ok":       True,
        "sma_log":  sml_series,
        "ema200":   ema_series,
        "interval": interval,
    })



    """Configuración actual de la estrategia (lectura)."""
    cfg = get_all_config()
    # No exponer secrets al frontend
    safe = {k: v for k, v in cfg.items()
            if "secret" not in k.lower() and "key" not in k.lower()}
    return jsonify({"ok": True, "config": safe})


@app.route("/api/config/update", methods=["POST"])
@login_required
def api_config_update():
    """Actualiza configuración incluyendo API keys."""
    data = request.get_json()
    allowed = {
        "binance_api_key", "binance_secret", "testnet",
        "capital_initial", "capital_per_trade", "leverage", "risk_pct",
        "sl_pct", "tp_pct", "acp_threshold",
        "bot_status",
    }
    updated = []
    for key, value in data.items():
        if key in allowed:
            set_config(key, str(value))
            updated.append(key)

    # Si actualizó API keys, verificar conexión
    if "binance_api_key" in updated or "binance_secret" in updated:
        ok = binance.ping()
        if not ok:
            return jsonify({"ok": False, "error": "API keys inválidas o sin conexión"})

    log_event("CONFIG_UPDATED", f"Campos: {', '.join(updated)}")
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/bot/toggle", methods=["POST"])
@login_required
def api_bot_toggle():
    """Iniciar o detener el bot."""
    current = get_config("bot_status", "STOPPED")
    new_status = "RUNNING" if current == "STOPPED" else "STOPPED"
    set_config("bot_status", new_status)
    log_event("BOT_STATUS", f"Bot cambiado a {new_status} por {session['username']}")
    return jsonify({"ok": True, "status": new_status})


@app.route("/api/signals")
@login_required
def api_signals():
    """Señales recientes generadas."""
    signals = get_recent_signals(50)
    return jsonify({"ok": True, "signals": signals})


@app.route("/api/events")
@login_required
def api_events():
    """Log de eventos del bot."""
    events = get_events(100)
    return jsonify({"ok": True, "events": events})


@app.route("/api/telegram/config", methods=["POST"])
@login_required
def api_telegram_config():
    """Guarda config de Telegram y opcionalmente envía mensaje de prueba."""
    data    = request.get_json()
    token   = data.get("token", "").strip()
    chat_id = data.get("chat_id", "").strip()
    notify_filtered = data.get("notify_filtered", "false")
    notify_errors   = data.get("notify_errors", "true")
    do_test = data.get("test", False)

    # Guardar siempre antes de hacer el test
    if token:
        set_config("telegram_token", token)
    if chat_id:
        set_config("telegram_chat_id", chat_id)
    set_config("telegram_notify_filtered", notify_filtered)
    set_config("telegram_notify_errors",   notify_errors)

    if do_test:
        # Usar token/chat_id del request directamente (no releer de BD)
        # para garantizar que el test usa los valores recién ingresados
        test_token   = token   or get_config("telegram_token",   "")
        test_chat_id = chat_id or get_config("telegram_chat_id", "")

        if not test_token or not test_chat_id:
            return jsonify({"ok": False,
                            "error": "Token y Chat ID son requeridos para enviar la prueba."})

        # Test directo con las credenciales del request
        ok = tg.test_connection_direct(test_token, test_chat_id)
        if not ok:
            return jsonify({"ok": False,
                            "error": "No se pudo enviar el mensaje. "
                                     "Verifica que el token sea válido y que hayas "
                                     "iniciado una conversación con tu bot en Telegram."})
        log_event("TELEGRAM_TEST", f"Prueba exitosa — chat_id={test_chat_id}")
        return jsonify({"ok": True, "message": "✓ Mensaje de prueba enviado correctamente"})

    log_event("TELEGRAM_CONFIG", "Configuración de Telegram guardada")
    return jsonify({"ok": True})


@app.route("/api/password", methods=["POST"])
@login_required
def api_change_password():
    """Cambiar contraseña."""
    data    = request.get_json()
    new_pw  = data.get("new_password", "")
    if len(new_pw) < 8:
        return jsonify({"ok": False, "error": "Mínimo 8 caracteres"})
    change_password(session["username"], new_pw)
    log_event("PASSWORD_CHANGED", f"Usuario {session['username']}")
    return jsonify({"ok": True})


@app.route("/api/credentials/status")
@login_required
def api_credentials_status():
    """
    Informa el origen de cada credencial crítica:
    'env' = viene de variable de entorno (persiste en reinicios)
    'db'  = viene de la base de datos SQLite (se pierde en redeploy)
    'missing' = no configurada en ningún lado
    """
    import os
    from core.database import get_config as _gc

    def _src(env_var, db_key):
        if os.getenv(env_var, "").strip():
            return "env"
        if _gc(db_key, ""):
            return "db"
        return "missing"

    api_key_src  = _src("BINANCE_API_KEY", "binance_api_key")
    secret_src   = _src("BINANCE_SECRET",  "binance_secret")
    pw_src       = "env" if (os.getenv("ADMIN_PASSWORD_HASH") or
                              os.getenv("ADMIN_PASSWORD")) else "db"
    tg_src       = _src("TELEGRAM_TOKEN", "telegram_token")
    testnet_src  = "env" if os.getenv("BINANCE_TESTNET", "").strip() else "db"

    # Conectividad Binance
    connected = binance.ping()

    return jsonify({
        "ok": True,
        "credentials": {
            "binance_api_key": api_key_src,
            "binance_secret":  secret_src,
            "admin_password":  pw_src,
            "telegram_token":  tg_src,
            "testnet_mode":    testnet_src,
        },
        "binance_connected": connected,
        "warning": (
            "Algunas credenciales están solo en BD (se perderán en redeploy). "
            "Configura las variables de entorno en Render para persistencia total."
            if any(s == "db" for s in [api_key_src, secret_src, pw_src]) else ""
        )
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


# ════════════════════════════════════════════════════════════════
#  TEMPLATES HTML
# ════════════════════════════════════════════════════════════════

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SATEVIS — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#06080c;color:#c8d8e8;font-family:'JetBrains Mono',monospace;
  min-height:100vh;display:flex;align-items:center;justify-content:center;}
.box{background:#0a0e14;border:1px solid #1a2535;border-radius:14px;
  padding:40px 36px;width:100%;max-width:360px;}
.logo{font-family:'Syne',sans-serif;font-size:26px;font-weight:800;
  color:#fff;text-align:center;margin-bottom:6px;}
.logo em{color:#aa66ff;font-style:normal;}
.sub{text-align:center;font-size:11px;color:#3d5470;margin-bottom:32px;}
label{display:block;font-size:11px;color:#3d5470;margin-bottom:6px;letter-spacing:.06em;}
input{width:100%;padding:10px 14px;background:#0f1520;border:1px solid #1a2535;
  border-radius:7px;color:#c8d8e8;font-family:'JetBrains Mono',monospace;
  font-size:13px;margin-bottom:16px;outline:none;transition:border-color .2s;}
input:focus{border-color:#aa66ff;}
button{width:100%;padding:12px;background:linear-gradient(135deg,#aa66ff,#7733cc);
  border:none;border-radius:7px;color:#fff;font-family:'Syne',sans-serif;
  font-size:14px;font-weight:700;cursor:pointer;margin-top:4px;}
button:hover{opacity:.9;}
.error{background:rgba(255,61,90,.1);border:1px solid rgba(255,61,90,.3);
  border-radius:7px;padding:10px 14px;font-size:12px;color:#ff3d5a;margin-bottom:16px;}
</style>
</head>
<body>
<div class="box">
  <div class="logo">SATEVIS <em>Bot</em></div>
  <div class="sub">Trading automático BTC · Acceso privado</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>USUARIO</label>
    <input name="username" type="text" autocomplete="username" required>
    <label>CONTRASEÑA</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Iniciar sesión →</button>
  </form>
</div>
</body>
</html>"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SATEVIS — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{
  --bg:#06080c;--bg1:#0a0e14;--bg2:#0f1520;--bg3:#141c28;
  --border:#1a2535;--border2:#223044;--text:#c8d8e8;--muted:#3d5470;
  --bull:#00e676;--bear:#ff3d5a;--gold:#ffb300;--blue:#2979ff;--purple:#aa66ff;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;
  font-size:13px;min-height:100vh;}

/* ── Top bar ── */
.topbar{background:var(--bg1);border-bottom:1px solid var(--border);
  padding:12px 24px;display:flex;align-items:center;gap:14px;
  position:sticky;top:0;z-index:100;}
.logo{font-family:'Syne',sans-serif;font-size:18px;font-weight:800;color:#fff;
  display:flex;align-items:center;gap:8px;}
.logo em{color:var(--purple);font-style:normal;}
.bot-status{padding:4px 12px;border-radius:99px;font-size:11px;font-weight:600;cursor:pointer;}
.bot-status.running{background:rgba(0,230,118,.12);border:1px solid rgba(0,230,118,.3);color:var(--bull);}
.bot-status.stopped{background:rgba(255,61,90,.12);border:1px solid rgba(255,61,90,.3);color:var(--bear);}
.bot-status.testnet{background:rgba(255,179,0,.12);border:1px solid rgba(255,179,0,.3);color:var(--gold);}
.price-live{font-family:'Syne',sans-serif;font-size:16px;font-weight:700;color:var(--bull);}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:12px;}
.refresh-info{font-size:10px;color:var(--muted);}
.btn-sm{padding:5px 12px;border-radius:6px;border:1px solid var(--border2);
  background:var(--bg2);color:var(--text);font-family:'JetBrains Mono',monospace;
  font-size:11px;cursor:pointer;transition:all .18s;}
.btn-sm:hover{border-color:var(--purple);color:var(--purple);}
.btn-danger{border-color:rgba(255,61,90,.4);color:var(--bear);}
.btn-danger:hover{background:rgba(255,61,90,.08);}

/* ── Nav tabs ── */
.nav{background:var(--bg1);border-bottom:1px solid var(--border);
  padding:0 24px;display:flex;gap:4px;}
.nav-tab{padding:10px 16px;font-size:12px;color:var(--muted);cursor:pointer;
  border-bottom:2px solid transparent;transition:all .18s;}
.nav-tab.active{color:var(--purple);border-color:var(--purple);}
.nav-tab:hover{color:var(--text);}

/* ── Main ── */
main{max-width:1440px;margin:0 auto;padding:20px 24px 48px;}
.section{display:none;}
.section.active{display:block;}

/* ── KPI grid ── */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
  gap:12px;margin-bottom:16px;}
.kpi{background:var(--bg1);border:1px solid var(--border);border-radius:10px;
  padding:14px 16px;}
.kpi-l{font-size:10px;color:var(--muted);letter-spacing:.07em;
  text-transform:uppercase;margin-bottom:6px;}
.kpi-v{font-family:'Syne',sans-serif;font-size:20px;font-weight:800;}
.kpi-s{font-size:10px;color:var(--muted);margin-top:3px;}
.kpi.bull{border-color:rgba(0,230,118,.25);background:rgba(0,230,118,.04);}
.kpi.bear{border-color:rgba(255,61,90,.25);background:rgba(255,61,90,.04);}
.kpi.gold{border-color:rgba(255,179,0,.25);background:rgba(255,179,0,.04);}
.kpi.purple{border-color:rgba(170,102,255,.25);background:rgba(170,102,255,.04);}
.cv{color:var(--bull);}.cr{color:var(--bear);}.cg{color:var(--gold);}.cp{color:var(--purple);}

/* ── Cards ── */
.card{background:var(--bg1);border:1px solid var(--border);border-radius:10px;
  padding:18px;margin-bottom:16px;}
.card-title{font-family:'Syne',sans-serif;font-size:10px;font-weight:700;
  letter-spacing:.12em;text-transform:uppercase;color:var(--muted);
  margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;}

/* ── Chart ── */
#candle-chart{height:400px;background:var(--bg2);border-radius:8px;}
.tf-pills{display:flex;gap:6px;margin-bottom:12px;}
.tf-pill{padding:4px 10px;border-radius:5px;border:1px solid var(--border);
  background:var(--bg2);color:var(--muted);font-size:11px;cursor:pointer;}
.tf-pill.active{border-color:var(--purple);color:var(--purple);background:rgba(170,102,255,.08);}

/* ── Table ── */
.tbl-wrap{overflow-x:auto;max-height:500px;overflow-y:auto;}
.tbl-wrap::-webkit-scrollbar{width:4px;height:4px;}
.tbl-wrap::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px;}
table{width:100%;border-collapse:collapse;font-size:12px;}
th{padding:8px 12px;text-align:left;color:var(--muted);font-weight:500;
  border-bottom:1px solid var(--border);font-size:10px;letter-spacing:.06em;
  text-transform:uppercase;position:sticky;top:0;background:var(--bg1);}
td{padding:8px 12px;border-bottom:1px solid rgba(26,37,53,.5);}
tr:hover td{background:var(--bg2);}
.pill{padding:2px 8px;border-radius:99px;font-size:10px;font-weight:600;display:inline-block;}
.pill.win{background:rgba(0,230,118,.12);color:var(--bull);}
.pill.loss{background:rgba(255,61,90,.12);color:var(--bear);}
.pill.liq{background:rgba(255,179,0,.12);color:var(--gold);}
.pill.long{background:rgba(41,121,255,.12);color:#2979ff;}
.pill.short{background:rgba(255,61,90,.12);color:var(--bear);}

/* ── Forms ── */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.form-group{display:flex;flex-direction:column;gap:5px;}
.form-group label{font-size:11px;color:var(--muted);}
.form-group input,.form-group select{
  padding:8px 12px;background:var(--bg2);border:1px solid var(--border2);
  border-radius:7px;color:var(--text);font-family:'JetBrains Mono',monospace;
  font-size:12px;outline:none;}
.form-group input:focus{border-color:var(--purple);}
.btn-primary{padding:10px 20px;background:linear-gradient(135deg,var(--purple),#7733cc);
  border:none;border-radius:7px;color:#fff;font-family:'Syne',sans-serif;
  font-size:13px;font-weight:700;cursor:pointer;}
.btn-primary:hover{opacity:.9;}

/* ── Position card ── */
.pos-card{background:var(--bg2);border:1px solid rgba(170,102,255,.3);
  border-radius:10px;padding:16px;margin-bottom:16px;}
.pos-header{font-family:'Syne',sans-serif;font-size:14px;font-weight:700;
  color:var(--purple);margin-bottom:12px;}
.pos-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;}
.pos-item{display:flex;flex-direction:column;gap:3px;}
.pos-label{font-size:10px;color:var(--muted);}
.pos-val{font-size:13px;font-weight:600;}

/* ── Alert ── */
.alert{padding:10px 14px;border-radius:7px;font-size:12px;margin-bottom:12px;}
.alert.ok{background:rgba(0,230,118,.08);border:1px solid rgba(0,230,118,.25);color:var(--bull);}
.alert.err{background:rgba(255,61,90,.08);border:1px solid rgba(255,61,90,.25);color:var(--bear);}

/* ── Config table ── */
.cfg-table{width:100%;border-collapse:collapse;font-size:12px;}
.cfg-table td{padding:8px 12px;border-bottom:1px solid rgba(26,37,53,.4);}
.cfg-key{color:var(--muted);width:40%;}
.cfg-val{color:var(--text);font-weight:500;}

/* ── Equity chart ── */
.equity-wrap{height:220px;position:relative;}

/* ── Indicadores toggle ── */
.ind-toggle{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;}
.ind-btn{padding:3px 10px;border-radius:5px;border:1px solid var(--border);
  background:var(--bg2);font-size:11px;cursor:pointer;transition:all .18s;}
.ind-btn.active-sml{border-color:#ffb300;color:#ffb300;background:rgba(255,179,0,.08);}
.ind-btn.active-ema{border-color:#2979ff;color:#2979ff;background:rgba(41,121,255,.08);}

/* ── Responsividad móvil ── */
@media(max-width:768px){
  .topbar{padding:8px 12px;gap:8px;flex-wrap:wrap;}
  .topbar-right{gap:6px;}
  .refresh-info{display:none;}
  .price-live{font-size:13px;}
  .logo{font-size:15px;}
  .btn-sm{padding:4px 8px;font-size:10px;}
  .nav{padding:0 8px;overflow-x:auto;gap:0;}
  .nav-tab{padding:8px 10px;font-size:11px;white-space:nowrap;}
  main{padding:12px 10px 48px;}
  .two-col{grid-template-columns:1fr;}
  .kpi-grid{grid-template-columns:repeat(2,1fr);}
  .form-grid{grid-template-columns:1fr;}
  .form-group[style*="grid-column"]{grid-column:1 !important;}
  #candle-chart{height:280px;}
  .equity-wrap{height:180px;}
  .tbl-wrap{max-height:360px;}
  table{font-size:11px;}
  th,td{padding:6px 8px;}
  .pos-grid{grid-template-columns:1fr 1fr;}
  .card{padding:12px;}
  .card-title{font-size:9px;}
  .kpi-v{font-size:16px;}
}
@media(max-width:420px){
  .kpi-grid{grid-template-columns:1fr 1fr;}
  .topbar{justify-content:space-between;}
  .bot-status{display:none;}
}
</style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <div class="logo">✦ SATEVIS <em>Bot</em></div>
  <div id="bot-status-badge" class="bot-status stopped" onclick="toggleBot()">● DETENIDO</div>
  <div id="testnet-badge" style="display:none" class="bot-status testnet">TESTNET</div>
  <div class="price-live" id="price-live">BTC $—</div>
  <div class="topbar-right">
    <span class="refresh-info" id="refresh-info">Actualiza en 30s</span>
    <button class="btn-sm" onclick="forceRefresh()">↻ Actualizar</button>
    <button class="btn-sm" onclick="showSection('settings')">⚙ Config</button>
    <a href="/logout"><button class="btn-sm btn-danger">Salir</button></a>
  </div>
</div>

<!-- Nav -->
<div class="nav">
  <div class="nav-tab active" onclick="showSection('overview')">Resumen</div>
  <div class="nav-tab" onclick="showSection('chart')">Gráfico BTC</div>
  <div class="nav-tab" onclick="showSection('trades')">Operaciones</div>
  <div class="nav-tab" onclick="showSection('capital')">Capital</div>
  <div class="nav-tab" onclick="showSection('signals')">Señales</div>
  <div class="nav-tab" onclick="showSection('settings')">Configuración</div>
</div>

<main>

<!-- ═══ OVERVIEW ═══ -->
<div class="section active" id="sec-overview">
  <div id="open-pos-container"></div>
  <div class="kpi-grid" id="kpi-grid"></div>
  <div class="two-col">
    <div class="card">
      <div class="card-title">Curva de equity</div>
      <div class="equity-wrap"><canvas id="equity-chart"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Distribución de resultados</div>
      <div style="height:220px;position:relative"><canvas id="dist-chart"></canvas></div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Últimas 10 operaciones</div>
    <div class="tbl-wrap" id="recent-trades-wrap"></div>
  </div>
</div>

<!-- ═══ CHART ═══ -->
<div class="section" id="sec-chart">
  <div class="card">
    <div class="card-title">BTC/USDT — Velas con operaciones marcadas</div>
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">
      <div class="tf-pills" style="margin-bottom:0">
        <button class="tf-pill active" onclick="loadChart('15m',this)">15m</button>
        <button class="tf-pill" onclick="loadChart('1h',this)">1h</button>
        <button class="tf-pill" onclick="loadChart('4h',this)">4h</button>
        <button class="tf-pill" onclick="loadChart('1d',this)">1d</button>
      </div>
      <div class="ind-toggle">
        <button class="ind-btn" id="btn-sml" onclick="toggleIndicator('sml')">SMA Log 144</button>
        <button class="ind-btn" id="btn-ema" onclick="toggleIndicator('ema')">EMA 200</button>
      </div>
    </div>
    <div id="candle-chart"></div>
    <div style="margin-top:8px;font-size:10px;color:var(--muted)">
      <span style="color:#ffb300">■</span> SMA Log 144 &nbsp;
      <span style="color:#2979ff">■</span> EMA 200 (filtro macro) &nbsp;
      <span style="color:#aa66ff">▲</span> Apertura LONG &nbsp;
      <span style="color:#00e676">●</span> TP &nbsp;
      <span style="color:#ff3d5a">●</span> SL
    </div>
  </div>
</div>

<!-- ═══ TRADES ═══ -->
<div class="section" id="sec-trades">
  <div class="card">
    <div class="card-title">Historial completo de operaciones</div>
    <div class="tbl-wrap" id="all-trades-wrap"></div>
  </div>
</div>

<!-- ═══ CAPITAL ═══ -->
<div class="section" id="sec-capital">
  <div class="kpi-grid" id="capital-kpis"></div>
  <div class="card">
    <div class="card-title">
      Movimientos de capital detectados automáticamente
      <button class="btn-sm" onclick="takeSnapshot()" style="margin-left:auto">↻ Sincronizar con Binance</button>
    </div>
    <div id="snapshot-alert" style="margin-bottom:10px"></div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:12px;line-height:1.6">
      El balance se sincroniza automáticamente cada hora. Si realizaste un depósito
      o retiro, usa el botón para detectarlo ahora. Diferencias menores a $1 se
      ignoran (son fees y funding).
    </div>
    <div class="tbl-wrap" id="movements-wrap"></div>
  </div>
  <div class="card">
    <div class="card-title">Curva de equity</div>
    <div class="equity-wrap"><canvas id="equity-chart-capital"></canvas></div>
  </div>
</div>

<!-- ═══ SIGNALS ═══ -->
<div class="section" id="sec-signals">
  <div class="card">
    <div class="card-title">Señales recientes generadas</div>
    <div class="tbl-wrap" id="signals-wrap"></div>
  </div>
  <div class="card" style="margin-top:0">
    <div class="card-title">Log de eventos del bot</div>
    <div class="tbl-wrap" id="events-wrap"></div>
  </div>
</div>

<!-- ═══ SETTINGS ═══ -->
<div class="section" id="sec-settings">

  <!-- Panel de estado de credenciales -->
  <div class="card" id="cred-status-card">
    <div class="card-title">Estado de credenciales del sistema</div>
    <div id="cred-status-wrap" style="font-size:12px;line-height:2"></div>
    <div style="margin-top:12px;padding:12px;background:var(--bg2);border-radius:8px;
      font-size:11px;color:var(--muted);line-height:1.8;">
      <strong style="color:var(--text)">Variables de entorno recomendadas en Render:</strong><br>
      <code>BINANCE_API_KEY</code> · <code>BINANCE_SECRET</code> · <code>BINANCE_TESTNET</code> (true/false)<br>
      <code>ADMIN_PASSWORD_HASH</code> · <code>TELEGRAM_TOKEN</code> · <code>TELEGRAM_CHAT_ID</code><br>
      <span style="color:var(--gold)">⚠ Credenciales marcadas como "BD" se perderán en cada redeploy.</span><br>
      Para obtener el hash de tu contraseña:<br>
      <code>python3 -c "import hashlib; print(hashlib.sha256(b'tucontraseña').hexdigest())"</code>
    </div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title">API Keys Binance</div>
      <div class="form-grid">
        <div class="form-group" style="grid-column:1/-1">
          <label>API Key</label>
          <input type="password" id="cfg-api-key" placeholder="Dejar vacío para mantener actual">
        </div>
        <div class="form-group" style="grid-column:1/-1">
          <label>Secret Key</label>
          <input type="password" id="cfg-secret" placeholder="Dejar vacío para mantener actual">
        </div>
        <div class="form-group">
          <label>Modo de operación</label>
          <select id="cfg-testnet">
            <option value="true">TESTNET (recomendado)</option>
            <option value="false">PRODUCCIÓN (dinero real)</option>
          </select>
        </div>
        <div class="form-group">
          <label>Capital inicial referencia (USDT)</label>
          <input type="number" id="cfg-capital" min="10" step="1">
        </div>
        <div class="form-group" style="grid-column:1/-1">
          <label>Capital por operación (USDT) — margen comprometido por trade</label>
          <input type="number" id="cfg-capital-trade" min="0" step="1"
            placeholder="Ej: 100 → notional $300 con 3x leverage">
        </div>
      </div>
      <div style="margin-top:12px"><button class="btn-primary" onclick="saveApiConfig()">Guardar configuración</button></div>
      <div id="api-alert" style="margin-top:10px"></div>
      <div style="margin-top:10px;font-size:11px;color:var(--muted);line-height:1.7">
        <strong style="color:var(--text)">Capital por operación:</strong>
        Es el margen que el bot compromete en cada trade. Con 3x leverage,
        un capital de $100 genera una posición notional de $300.
        La pérdida máxima por trade = capital × SL% × leverage.
        Ejemplo: $100 × 2% × 3 = $6 pérdida máxima.
      </div>
    </div>
    <div class="card">
      <div class="card-title">Parámetros de la estrategia D</div>
      <table class="cfg-table" id="cfg-table"></table>
      <div style="margin-top:14px;font-size:11px;color:var(--muted)">
        Los parámetros de estrategia están fijados por el backtesting validado.
        Modifícalos solo si entiendes el impacto en el comportamiento del bot.
      </div>
    </div>
  </div>

  <!-- Notificaciones Telegram — solo visible en esta sección -->
  <div class="card">
    <div class="card-title">🔔 Notificaciones por Telegram</div>
    <div class="form-grid">
      <div class="form-group" style="grid-column:1/-1">
        <label>Bot Token (obtenido de @BotFather)</label>
        <input type="password" id="tg-token" placeholder="Dejar vacío para mantener actual">
      </div>
      <div class="form-group" style="grid-column:1/-1">
        <label>Chat ID (tu ID personal de Telegram)</label>
        <input type="text" id="tg-chat-id" placeholder="Ej: 123456789">
      </div>
      <div class="form-group">
        <label>Notificar señales filtradas (verbose)</label>
        <select id="tg-filtered">
          <option value="false">No (recomendado)</option>
          <option value="true">Sí</option>
        </select>
      </div>
      <div class="form-group">
        <label>Notificar errores críticos</label>
        <select id="tg-errors">
          <option value="true">Sí (recomendado)</option>
          <option value="false">No</option>
        </select>
      </div>
    </div>
    <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap;">
      <button class="btn-primary" onclick="saveTelegram(false)">Guardar configuración</button>
      <button class="btn-sm" onclick="saveTelegram(true)" style="padding:10px 16px;">
        📨 Guardar y enviar mensaje de prueba
      </button>
    </div>
    <div id="tg-alert" style="margin-top:10px"></div>
    <div style="margin-top:14px;padding:12px;background:var(--bg2);border-radius:8px;
      font-size:11px;color:var(--muted);line-height:1.8;">
      <strong style="color:var(--text)">Cómo configurar en 2 minutos:</strong><br>
      1. Abre Telegram → busca <code>@BotFather</code> → envía <code>/newbot</code><br>
      2. Sigue las instrucciones → copia el <strong>token</strong> que te entrega<br>
      3. Busca <code>@userinfobot</code> en Telegram → envía cualquier mensaje → copia tu <strong>ID</strong><br>
      4. Pega token e ID aquí → clic en "Guardar y enviar mensaje de prueba"
    </div>
  </div>

  <!-- Cambiar contraseña -->
  <div class="card">
    <div class="card-title">Cambiar contraseña del dashboard</div>
    <div class="form-grid">
      <div class="form-group">
        <label>Nueva contraseña (mín. 8 caracteres)</label>
        <input type="password" id="new-pw" placeholder="Nueva contraseña">
      </div>
      <div class="form-group">
        <label>Confirmar contraseña</label>
        <input type="password" id="new-pw2" placeholder="Repetir contraseña">
      </div>
    </div>
    <div style="margin-top:12px"><button class="btn-primary" onclick="changePassword()">Cambiar contraseña</button></div>
    <div id="pw-alert" style="margin-top:10px"></div>
    <div style="margin-top:10px;font-size:11px;color:var(--muted)">
      ⚠ Si el bot está en Render sin la variable <code>ADMIN_PASSWORD_HASH</code>,
      la contraseña se perderá en el próximo redeploy. Configura esa variable de entorno
      para que persista permanentemente.
    </div>
  </div>

</div>

</main>

<script>
// ── State ──────────────────────────────────────────────────────────
let currentSection = 'overview';
let candleChart = null;
let equityChart = null;
let distChart   = null;
let refreshTimer = null;
let countdown   = 30;
let lastSummary = null;

// ── Navigation ────────────────────────────────────────────────────
function showSection(name) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('sec-' + name).classList.add('active');
  document.querySelectorAll('.nav-tab').forEach(t => {
    if (t.textContent.toLowerCase().includes(name.substring(0,4))) t.classList.add('active');
  });
  currentSection = name;
  if (name === 'chart')    loadChart('1h');
  if (name === 'trades')   loadTrades();
  if (name === 'capital')  loadCapital();
  if (name === 'signals')  loadSignals();
  if (name === 'settings') { loadConfig(); loadCredentialStatus(); }
}

// ── Zona horaria Colombia (UTC-5) ──────────────────────────────────
function toColombiaTime(utcStr) {
  if (!utcStr) return '—';
  try {
    // Asegurar que el string se interprete como UTC
    const s = utcStr.replace(' ', 'T');
    const d = new Date(s.endsWith('Z') || s.includes('+') ? s : s + 'Z');
    return d.toLocaleString('es-CO', {
      timeZone: 'America/Bogota',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false
    }).replace(',', '');
  } catch(e) {
    return utcStr.substring(0, 16);
  }
}

// ── Auto refresh ───────────────────────────────────────────────────
function startRefresh() {
  clearInterval(refreshTimer);
  countdown = 30;
  refreshTimer = setInterval(() => {
    countdown--;
    document.getElementById('refresh-info').textContent = `Actualiza en ${countdown}s`;
    if (countdown <= 0) {
      forceRefresh();
      countdown = 30;
    }
  }, 1000);
}

async function forceRefresh() {
  await loadSummary();
  if (currentSection === 'chart') loadChart(document.querySelector('.tf-pill.active')?.textContent || '1h');
  if (currentSection === 'trades') loadTrades();
  if (currentSection === 'signals') loadSignals();
  countdown = 30;
  document.getElementById('refresh-info').textContent = `Actualiza en 30s`;
}

// ── Summary ────────────────────────────────────────────────────────
async function loadSummary() {
  const r = await fetch('/api/summary');
  const d = await r.json();
  if (!d.ok) return;
  lastSummary = d;

  // Price
  document.getElementById('price-live').textContent = `BTC $${d.btc_price?.toLocaleString('es-CO', {minimumFractionDigits:2})}`;

  // Bot status
  const badge = document.getElementById('bot-status-badge');
  const tn = document.getElementById('testnet-badge');
  badge.textContent = d.bot_status === 'RUNNING' ? '● ACTIVO' : '● DETENIDO';
  badge.className = 'bot-status ' + (d.bot_status === 'RUNNING' ? 'running' : 'stopped');
  tn.style.display = d.testnet === 'true' ? 'inline-block' : 'none';

  // Open position
  const posCont = document.getElementById('open-pos-container');
  if (d.open_position) {
    const pos = d.open_position;
    const upnl = d.unrealized_pnl;
    const uc = upnl >= 0 ? 'cv' : 'cr';
    posCont.innerHTML = `
      <div class="pos-card">
        <div class="pos-header">
          ${pos.side === 'LONG' ? '↑ LONG' : '↓ SHORT'} BTCUSDT · ${pos.qty} BTC
        </div>
        <div class="pos-grid">
          <div class="pos-item"><div class="pos-label">Entrada</div><div class="pos-val">$${pos.entry_price?.toLocaleString()}</div></div>
          <div class="pos-item"><div class="pos-label">Precio actual</div><div class="pos-val">$${d.btc_price?.toLocaleString()}</div></div>
          <div class="pos-item"><div class="pos-label">PnL no realizado</div><div class="pos-val ${uc}">${upnl >= 0 ? '+' : ''}$${upnl?.toFixed(2)}</div></div>
          <div class="pos-item"><div class="pos-label">Precio liquidación</div><div class="pos-val cr">$${pos.liquidation_price?.toLocaleString()}</div></div>
          <div class="pos-item"><div class="pos-label">Apalancamiento</div><div class="pos-val">${pos.leverage}×</div></div>
          <div class="pos-item"><div class="pos-label">Margen</div><div class="pos-val">${pos.margin_type}</div></div>
        </div>
      </div>`;
  } else {
    posCont.innerHTML = '';
  }

  // KPIs
  const pnlNet = d.pnl_net;
  const wr = d.win_rate;
  document.getElementById('kpi-grid').innerHTML = `
    <div class="kpi ${d.balance_live >= d.capital_initial ? 'bull' : 'bear'}">
      <div class="kpi-l">Capital actual</div>
      <div class="kpi-v ${d.balance_live >= d.capital_initial ? 'cv' : 'cr'}">$${d.total_value?.toLocaleString('es-CO',{minimumFractionDigits:2})}</div>
      <div class="kpi-s">Inicial: $${d.capital_initial?.toLocaleString()}</div>
    </div>
    <div class="kpi ${pnlNet >= 0 ? 'bull' : 'bear'}">
      <div class="kpi-l">Ganancia neta</div>
      <div class="kpi-v ${pnlNet >= 0 ? 'cv' : 'cr'}">${pnlNet >= 0 ? '+' : ''}$${pnlNet?.toFixed(2)}</div>
      <div class="kpi-s">Bruta: ${d.pnl_gross >= 0 ? '+' : ''}$${d.pnl_gross?.toFixed(2)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-l">Costos operación</div>
      <div class="kpi-v cg">$${d.total_fees?.toFixed(2)}</div>
      <div class="kpi-s">Fees + funding acumulados</div>
    </div>
    <div class="kpi ${wr >= 50 ? 'bull' : 'bear'}">
      <div class="kpi-l">Win Rate real</div>
      <div class="kpi-v ${wr >= 50 ? 'cv' : 'cr'}">${wr}%</div>
      <div class="kpi-s">${d.wins}G · ${d.losses}P · ${d.liquidations} Liq.</div>
    </div>
    <div class="kpi purple">
      <div class="kpi-l">Total operaciones</div>
      <div class="kpi-v cp">${d.total_trades}</div>
      <div class="kpi-s">Promedio: G${d.avg_win_pct}% / P${d.avg_loss_pct}%</div>
    </div>
    <div class="kpi ${d.liquidations > 0 ? 'bear' : 'bull'}">
      <div class="kpi-l">Liquidaciones forzosas</div>
      <div class="kpi-v ${d.liquidations > 0 ? 'cr' : 'cv'}">${d.liquidations}</div>
      <div class="kpi-s">${d.liquidations === 0 ? '✓ Ninguna en todo el período' : 'Ver detalle en Operaciones'}</div>
    </div>`;

  // Recent trades
  await loadRecentTrades();
  // Charts
  updateEquityChart();
  updateDistChart(d);
}

// ── Equity chart ───────────────────────────────────────────────────
async function updateEquityChart() {
  const r = await fetch('/api/capital');
  const d = await r.json();
  if (!d.ok) return;
  const hist = d.history.reverse();
  const labels = hist.map(h => h.ts?.substring(0,10));
  const values = hist.map(h => h.balance);

  if (equityChart) equityChart.destroy();
  const ctx = document.getElementById('equity-chart').getContext('2d');
  equityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: '#aa66ff',
        backgroundColor: 'rgba(170,102,255,.1)',
        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: '#3d5470', font: { size: 9 }, maxTicksLimit: 8 } },
        y: { ticks: { color: '#3d5470', font: { size: 10 }, callback: v => '$'+v.toLocaleString() },
             grid: { color: 'rgba(26,37,53,.6)' } }
      }
    }
  });
}

function updateDistChart(d) {
  if (distChart) distChart.destroy();
  const ctx = document.getElementById('dist-chart').getContext('2d');
  distChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Ganadoras', 'Perdedoras', 'Liquidaciones'],
      datasets: [{
        data: [d.wins, d.losses, d.liquidations],
        backgroundColor: ['rgba(0,230,118,.75)', 'rgba(255,61,90,.75)', 'rgba(255,179,0,.75)'],
        borderColor: ['rgba(0,230,118,.3)', 'rgba(255,61,90,.3)', 'rgba(255,179,0,.3)'],
        borderWidth: 1,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { color: '#c8d8e8', font: { size: 11 }, padding: 12 } } }
    }
  });
}

// ── Trades table ───────────────────────────────────────────────────
function tradeRow(t) {
  const pnlC = (t.pnl_net || 0) >= 0 ? 'cv' : 'cr';
  const res = t.result === 'WIN' ? '<span class="pill win">✓ WIN</span>'
            : t.result === 'LIQUIDATION' ? '<span class="pill liq">⚡ LIQ</span>'
            : '<span class="pill loss">✗ LOSS</span>';
  const side = t.side === 'LONG'
    ? '<span class="pill long">↑ LONG</span>'
    : '<span class="pill short">↓ SHORT</span>';
  return `<tr>
    <td style="color:var(--muted);font-size:11px;white-space:nowrap">${toColombiaTime(t.opened_at)}</td>
    <td>${side}</td>
    <td>$${(t.entry_price||0).toLocaleString()}</td>
    <td>${t.exit_price ? '$'+t.exit_price.toLocaleString() : '—'}</td>
    <td class="${pnlC}">${(t.pnl_net||0) >= 0 ? '+' : ''}$${(t.pnl_net||0).toFixed(2)}</td>
    <td class="${pnlC}">${(t.pnl_pct||0) >= 0 ? '+' : ''}${(t.pnl_pct||0).toFixed(2)}%</td>
    <td>${res}</td>
    <td style="color:var(--muted);font-size:11px">${t.close_reason||'—'}</td>
    <td style="color:var(--muted);font-size:11px">${t.duration_hours ? t.duration_hours.toFixed(1)+'h' : '—'}</td>
  </tr>`;
}

async function loadRecentTrades() {
  const r = await fetch('/api/trades?limit=10');
  const d = await r.json();
  if (!d.ok) return;
  const wrap = document.getElementById('recent-trades-wrap');
  wrap.innerHTML = `<table>
    <thead><tr><th>Fecha</th><th>Lado</th><th>Entrada</th><th>Salida</th>
    <th>PnL $</th><th>PnL %</th><th>Result</th><th>Razón</th><th>Duración</th></tr></thead>
    <tbody>${d.trades.map(tradeRow).join('')}</tbody>
  </table>`;
}

async function loadTrades() {
  const r = await fetch('/api/trades?limit=500');
  const d = await r.json();
  if (!d.ok) return;
  document.getElementById('all-trades-wrap').innerHTML = `<table>
    <thead><tr><th>Fecha</th><th>Lado</th><th>Entrada</th><th>Salida</th>
    <th>PnL $</th><th>PnL %</th><th>Result</th><th>Razón</th><th>Duración</th></tr></thead>
    <tbody>${d.trades.map(tradeRow).join('')}</tbody>
  </table>`;
}

// ── Candlestick chart + indicadores ───────────────────────────────
let showSml = false;
let showEma = false;
let currentTf = '1h';
let smlSeries = null;
let emaSeries = null;

function toggleIndicator(ind) {
  if (ind === 'sml') {
    showSml = !showSml;
    document.getElementById('btn-sml').className = 'ind-btn' + (showSml ? ' active-sml' : '');
  } else {
    showEma = !showEma;
    document.getElementById('btn-ema').className = 'ind-btn' + (showEma ? ' active-ema' : '');
  }
  loadChart(currentTf);
}

async function loadChart(tf, el) {
  currentTf = tf;
  if (el) {
    document.querySelectorAll('.tf-pill').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
  }

  const [rc, ri] = await Promise.all([
    fetch(`/api/candles?interval=${tf}&limit=300`),
    fetch(`/api/indicators?interval=${tf}&limit=400`),
  ]);
  const [dc, di] = await Promise.all([rc.json(), ri.json()]);
  if (!dc.ok) return;

  const container = document.getElementById('candle-chart');
  container.innerHTML = '';
  const h = Math.max(280, Math.min(400, window.innerHeight - 260));

  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: h,
    layout: { background: { color: '#0f1520' }, textColor: '#c8d8e8' },
    grid: {
      vertLines: { color: '#1a2535' },
      horzLines: { color: '#1a2535' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#1a2535' },
    timeScale: { borderColor: '#1a2535', timeVisible: true },
  });

  // Velas principales
  const series = chart.addCandlestickSeries({
    upColor: '#00e676', downColor: '#ff3d5a',
    borderUpColor: '#00e676', borderDownColor: '#ff3d5a',
    wickUpColor: '#00e676', wickDownColor: '#ff3d5a',
  });
  series.setData(dc.candles.map(c => ({
    time: Math.floor(c.t / 1000),
    open: c.open, high: c.high, low: c.low, close: c.close
  })));

  // ── SMA Log 144 ──────────────────────────────────────────────
  smlSeries = null;
  if (showSml && di.ok && di.sma_log?.length) {
    smlSeries = chart.addLineSeries({
      color: '#ffb300', lineWidth: 1.5,
      priceLineVisible: false, lastValueVisible: true,
      title: 'SMA Log',
    });
    smlSeries.setData(di.sma_log.map(p => ({
      time: Math.floor(p.t / 1000), value: p.v
    })));
  }

  // ── EMA 200 ───────────────────────────────────────────────────
  emaSeries = null;
  if (showEma && di.ok && di.ema200?.length) {
    emaSeries = chart.addLineSeries({
      color: '#2979ff', lineWidth: 1.5,
      priceLineVisible: false, lastValueVisible: true,
      title: 'EMA 200',
    });
    emaSeries.setData(di.ema200.map(p => ({
      time: Math.floor(p.t / 1000), value: p.v
    })));
  }

  // ── Markers de trades ─────────────────────────────────────────
  if (dc.markers?.length) {
    const markers = dc.markers
      .filter(m => m.price && m.ts)
      .map(m => ({
        time: Math.floor(new Date(
          m.ts.endsWith('Z') || m.ts.includes('+') ? m.ts : m.ts + 'Z'
        ).getTime() / 1000),
        position: m.side === 'LONG'
          ? (m.type === 'open' ? 'belowBar' : 'aboveBar')
          : (m.type === 'open' ? 'aboveBar' : 'belowBar'),
        color: m.type === 'open' ? '#aa66ff'
             : (m.result === 'WIN' ? '#00e676' : '#ff3d5a'),
        shape: m.type === 'open' ? 'arrowUp' : 'circle',
        text: m.type === 'open'
          ? `${m.side} $${m.price}`
          : `${m.result} $${m.price}`,
        size: 1,
      }))
      .sort((a, b) => a.time - b.time);
    if (markers.length) series.setMarkers(markers);
  }

  window.addEventListener('resize', () => {
    const newH = Math.max(280, Math.min(400, window.innerHeight - 260));
    chart.resize(container.clientWidth, newH);
  });
  candleChart = chart;
}

// ── Capital ────────────────────────────────────────────────────────
async function loadCapital() {
  const r = await fetch('/api/capital');
  const d = await r.json();
  if (!d.ok) return;

  // KPIs de capital
  const hist = d.history || [];
  const first = hist.length ? hist[hist.length - 1].balance : 0;
  const live  = d.balance_live || 0;
  const pnl   = live - first;
  const pnlC  = pnl >= 0 ? 'cv' : 'cr';
  document.getElementById('capital-kpis').innerHTML = `
    <div class="kpi ${live >= first ? 'bull':'bear'}">
      <div class="kpi-l">Balance en Binance</div>
      <div class="kpi-v ${live >= first ? 'cv':'cr'}">$${live.toLocaleString('es-CO',{minimumFractionDigits:2})}</div>
      <div class="kpi-s">Balance disponible en cuenta</div>
    </div>
    <div class="kpi ${pnl >= 0 ? 'bull':'bear'}">
      <div class="kpi-l">Variación total</div>
      <div class="kpi-v ${pnlC}">${pnl >= 0?'+':''}$${pnl.toFixed(2)}</div>
      <div class="kpi-s">Desde primer registro</div>
    </div>
    <div class="kpi">
      <div class="kpi-l">Registros</div>
      <div class="kpi-v cp">${hist.length}</div>
      <div class="kpi-s">Snapshots históricos</div>
    </div>
    <div class="kpi">
      <div class="kpi-l">Movimientos</div>
      <div class="kpi-v cp">${d.movements?.length || 0}</div>
      <div class="kpi-s">Depósitos y retiros detectados</div>
    </div>`;

  // Tabla de movimientos
  const wrap = document.getElementById('movements-wrap');
  if (!d.movements?.length) {
    wrap.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px">Sin movimientos detectados aún. El bot los registra automáticamente al detectar diferencias de balance ≥ $1.</div>';
  } else {
    wrap.innerHTML = `<table>
      <thead><tr><th>Fecha (Col.)</th><th>Tipo</th><th>Monto</th><th>Saldo después</th><th>Descripción</th></tr></thead>
      <tbody>${d.movements.map(m => `<tr>
        <td style="color:var(--muted);font-size:11px;white-space:nowrap">${toColombiaTime(m.ts)}</td>
        <td><span class="pill ${m.type==='DEPOSIT'?'win':'loss'}">${m.type==='DEPOSIT'?'↑ Depósito':'↓ Retiro'}</span></td>
        <td class="${(m.amount||0)>=0?'cv':'cr'}">${(m.amount||0)>=0?'+':''}$${Math.abs(m.amount||0).toFixed(2)}</td>
        <td>$${(m.balance_after||0).toLocaleString('es-CO',{minimumFractionDigits:2})}</td>
        <td style="color:var(--muted);font-size:11px">${m.description||'—'}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  }

  // Gráfico de equity en la pestaña Capital
  const histRev  = [...hist].reverse();
  const labels   = histRev.map(h => toColombiaTime(h.ts).substring(0, 10));
  const values   = histRev.map(h => h.balance);
  if (window._capChart) window._capChart.destroy();
  const ctx = document.getElementById('equity-chart-capital');
  if (ctx) {
    window._capChart = new Chart(ctx.getContext('2d'), {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: values,
          borderColor: '#aa66ff',
          backgroundColor: 'rgba(170,102,255,.1)',
          fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false }, ticks: { color: '#3d5470', font: { size: 9 }, maxTicksLimit: 8 } },
          y: { ticks: { color: '#3d5470', font: { size: 10 }, callback: v => '$'+v.toLocaleString() },
               grid: { color: 'rgba(26,37,53,.6)' } }
        }
      }
    });
  }
}

async function takeSnapshot() {
  const alertEl = document.getElementById('snapshot-alert');
  alertEl.innerHTML = '<div class="alert ok" style="margin-bottom:0">Consultando Binance...</div>';
  const r = await fetch('/api/capital/snapshot', { method: 'POST' });
  const d = await r.json();
  if (!d.ok) {
    alertEl.innerHTML = `<div class="alert err">${d.error}</div>`;
    return;
  }
  const msg = d.movement
    ? `${d.movement === 'DEPOSIT' ? '↑ Depósito' : '↓ Retiro'} detectado: $${Math.abs(d.diff).toFixed(2)} | Balance: $${d.balance.toFixed(2)}`
    : `Sin cambios significativos | Balance: $${d.balance.toFixed(2)}`;
  alertEl.innerHTML = `<div class="alert ok">${msg}</div>`;
  loadCapital();
  loadSummary();
}

// ── Signals ────────────────────────────────────────────────────────
async function loadSignals() {
  const [rs, re] = await Promise.all([fetch('/api/signals'), fetch('/api/events')]);
  const [ds, de] = await Promise.all([rs.json(), re.json()]);

  if (ds.ok) {
    const reasonLabel = (s) => {
      const r = s.reason_skip || '';
      if (!r || r === '') return s.executed ? '—' : '<span style="color:var(--muted)">sin razón registrada</span>';
      if (r.includes('long_only_filter'))  return '<span style="color:var(--bear)">Solo LONG — señal bajista bloqueada</span>';
      if (r.includes('acp_too_low'))       return '<span style="color:var(--muted)">ACP insuficiente</span>';
      if (r.includes('macro_filter'))      return '<span style="color:var(--muted)">Precio bajo EMA200</span>';
      if (r.includes('slope_flat'))        return '<span style="color:var(--muted)">Pendiente plana</span>';
      if (r.includes('all_filters_passed'))return '<span style="color:var(--bull)">Ejecutada</span>';
      return `<span style="color:var(--muted);font-size:11px">${r.substring(0,50)}</span>`;
    };
    document.getElementById('signals-wrap').innerHTML = `<table>
      <thead><tr><th>Fecha (Col.)</th><th>Dirección</th><th>Bias Log</th><th>ACP</th><th>Macro</th><th>Ejecutada</th><th>Razón</th></tr></thead>
      <tbody>${ds.signals.map(s => `<tr>
        <td style="color:var(--muted);font-size:11px">${toColombiaTime(s.ts)}</td>
        <td>${s.direction===1?'<span class="pill long">↑ LONG</span>':s.direction===-1?'<span class="pill short">↓ SHORT</span>':'<span style="color:var(--muted)">HOLD</span>'}</td>
        <td>${s.log_bias===1?'▲ Alcista':'▼ Bajista'}</td>
        <td>${(s.acp_angle||0).toFixed(4)}°</td>
        <td>${s.macro_ok?'<span style="color:var(--bull)">✓</span>':'<span style="color:var(--bear)">✗</span>'}</td>
        <td>${s.executed?'<span class="pill win">✓</span>':'<span class="pill loss">✗</span>'}</td>
        <td>${reasonLabel(s)}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  }

  if (de.ok) {
    document.getElementById('events-wrap').innerHTML = `<table>
      <thead><tr><th>Fecha (Col.)</th><th>Nivel</th><th>Evento</th><th>Detalle</th></tr></thead>
      <tbody>${de.events.map(e => `<tr>
        <td style="color:var(--muted);font-size:11px;white-space:nowrap">${toColombiaTime(e.ts)}</td>
        <td><span style="color:${e.level==='ERROR'?'var(--bear)':e.level==='WARNING'?'var(--gold)':'var(--muted)'};font-size:11px">${e.level}</span></td>
        <td style="font-size:12px">${e.event}</td>
        <td style="color:var(--muted);font-size:11px;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${(e.detail||'').replace(/"/g,"'")}">${e.detail||''}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  }
}

// ── Config ─────────────────────────────────────────────────────────
async function loadConfig() {
  const r = await fetch('/api/config');
  const d = await r.json();
  if (!d.ok) return;
  const cfg = d.config;
  const skip = ['binance_api_key','binance_secret','bot_status',
                'telegram_token','telegram_chat_id'];
  const labels = {
    strategy:'Estrategia', symbol:'Par', leverage:'Apalancamiento',
    risk_pct:'Riesgo por trade (%)', sl_pct:'Stop Loss (%)', tp_pct:'Take Profit (%)',
    capital_per_trade:'Capital por operación (USDT)',
    acp_threshold:'Umbral ACP (°)', sma_log_period:'Período SMA Log',
    ema_period:'Período EMA', macro_ema:'EMA Macro (filtro)',
    capital_initial:'Capital inicial (USDT)', testnet:'Modo testnet',
  };
  document.getElementById('cfg-table').innerHTML = Object.entries(cfg)
    .filter(([k]) => !skip.includes(k))
    .map(([k,v]) => `<tr>
      <td class="cfg-key">${labels[k]||k}</td>
      <td class="cfg-val">${v}</td>
    </tr>`).join('');

  if (cfg.testnet) document.getElementById('cfg-testnet').value = cfg.testnet;
  if (cfg.capital_initial) document.getElementById('cfg-capital').value = cfg.capital_initial;
  if (cfg.capital_per_trade && cfg.capital_per_trade !== '0')
    document.getElementById('cfg-capital-trade').value = cfg.capital_per_trade;
  if (cfg.telegram_notify_filtered)
    document.getElementById('tg-filtered').value = cfg.telegram_notify_filtered;
  if (cfg.telegram_notify_errors)
    document.getElementById('tg-errors').value = cfg.telegram_notify_errors;
  if (cfg.telegram_chat_id)
    document.getElementById('tg-chat-id').value = cfg.telegram_chat_id;
}

async function loadCredentialStatus() {
  const r = await fetch('/api/credentials/status');
  const d = await r.json();
  if (!d.ok) return;
  const c = d.credentials;
  const srcBadge = (s) => {
    if (s === 'env')     return '<span style="color:var(--bull);font-weight:600">✓ Env var</span> <span style="color:var(--muted)">(persiste en reinicios)</span>';
    if (s === 'db')      return '<span style="color:var(--gold);font-weight:600">⚠ Solo BD</span> <span style="color:var(--muted)">(se pierde en redeploy)</span>';
    return '<span style="color:var(--bear);font-weight:600">✗ No configurada</span>';
  };
  const conn = d.binance_connected
    ? '<span style="color:var(--bull)">✓ Conectado</span>'
    : '<span style="color:var(--bear)">✗ Sin conexión</span>';
  document.getElementById('cred-status-wrap').innerHTML = `
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr><td style="padding:4px 0;color:var(--muted);width:200px">Binance API Key</td><td>${srcBadge(c.binance_api_key)}</td></tr>
      <tr><td style="padding:4px 0;color:var(--muted)">Binance Secret</td><td>${srcBadge(c.binance_secret)}</td></tr>
      <tr><td style="padding:4px 0;color:var(--muted)">Contraseña admin</td><td>${srcBadge(c.admin_password)}</td></tr>
      <tr><td style="padding:4px 0;color:var(--muted)">Telegram Token</td><td>${srcBadge(c.telegram_token)}</td></tr>
      <tr><td style="padding:4px 0;color:var(--muted)">Modo testnet</td><td>${srcBadge(c.testnet_mode)}</td></tr>
      <tr><td style="padding:4px 0;color:var(--muted)">Conexión Binance</td><td>${conn}</td></tr>
    </table>
    ${d.warning ? `<div class="alert err" style="margin-top:10px">${d.warning}</div>` : ''}
  `;
}

async function saveApiConfig() {
  const data = {};
  const key    = document.getElementById('cfg-api-key').value.trim();
  const secret = document.getElementById('cfg-secret').value.trim();
  const testnet = document.getElementById('cfg-testnet').value;
  const capital = document.getElementById('cfg-capital').value;
  const capitalTrade = document.getElementById('cfg-capital-trade').value;
  if (key)    data['binance_api_key'] = key;
  if (secret) data['binance_secret']  = secret;
  data['testnet']           = testnet;
  data['capital_initial']   = capital;
  if (capitalTrade) data['capital_per_trade'] = capitalTrade;

  const r = await fetch('/api/config/update', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(data)
  });
  const d = await r.json();
  const alertEl = document.getElementById('api-alert');
  alertEl.innerHTML = d.ok
    ? '<div class="alert ok">✓ Configuración guardada. Recargando estado...</div>'
    : `<div class="alert err">Error: ${d.error}</div>`;
  if (d.ok) { loadCredentialStatus(); loadSummary(); }
}

async function changePassword() {
  const pw1 = document.getElementById('new-pw').value;
  const pw2 = document.getElementById('new-pw2').value;
  const alert = document.getElementById('pw-alert');
  if (pw1 !== pw2) { alert.innerHTML='<div class="alert err">Las contraseñas no coinciden</div>'; return; }
  const r = await fetch('/api/password', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({new_password:pw1})
  });
  const d = await r.json();
  alert.innerHTML = d.ok
    ? '<div class="alert ok">✓ Contraseña actualizada</div>'
    : `<div class="alert err">${d.error}</div>`;
}

// ── Bot toggle ─────────────────────────────────────────────────────
async function toggleBot() {
  const r = await fetch('/api/bot/toggle', {method:'POST'});
  const d = await r.json();
  if (d.ok) loadSummary();
}

// ── Init ───────────────────────────────────────────────────────────
window.addEventListener('load', () => {
  loadSummary();
  startRefresh();
});
</script>
</body>
</html>"""


def run_dashboard():
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("ENVIRONMENT", "production") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
