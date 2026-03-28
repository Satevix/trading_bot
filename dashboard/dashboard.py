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
    """Curva de equity y movimientos de capital."""
    history   = get_capital_history(500)
    movements = get_capital_movements(100)
    return jsonify({
        "ok":       True,
        "history":  history,
        "movements": movements,
    })


@app.route("/api/capital/movement", methods=["POST"])
@login_required
def api_capital_movement():
    """Registrar ingreso o egreso manual de capital."""
    data   = request.get_json()
    type_  = data.get("type", "DEPOSIT").upper()
    amount = float(data.get("amount", 0))
    desc   = data.get("description", "")

    if type_ not in ("DEPOSIT", "WITHDRAWAL"):
        return jsonify({"ok": False, "error": "tipo inválido"})
    if amount <= 0:
        return jsonify({"ok": False, "error": "monto inválido"})

    balance = binance.get_balance()
    adj_amount = amount if type_ == "DEPOSIT" else -amount
    balance_after = balance + adj_amount

    add_capital_movement(type_, adj_amount, desc, balance_after)
    log_event("CAPITAL_MOVEMENT", f"{type_} ${amount:.2f} — {desc}")

    return jsonify({"ok": True, "balance_after": balance_after})


@app.route("/api/config")
@login_required
def api_config():
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
        "capital_initial", "leverage", "risk_pct",
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

    if token:
        set_config("telegram_token", token)
    if chat_id:
        set_config("telegram_chat_id", chat_id)
    set_config("telegram_notify_filtered", notify_filtered)
    set_config("telegram_notify_errors",   notify_errors)

    if do_test:
        ok = tg.test_connection()
        if not ok:
            return jsonify({"ok": False,
                            "error": "No se pudo enviar el mensaje. "
                                     "Verifica el token y chat_id."})
        log_event("TELEGRAM_TEST", f"Prueba exitosa — chat_id={chat_id}")
        return jsonify({"ok": True, "message": "Mensaje de prueba enviado ✓"})

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
    <div class="tf-pills">
      <button class="tf-pill active" onclick="loadChart('15m',this)">15m</button>
      <button class="tf-pill" onclick="loadChart('1h',this)">1h</button>
      <button class="tf-pill" onclick="loadChart('4h',this)">4h</button>
      <button class="tf-pill" onclick="loadChart('1d',this)">1d</button>
    </div>
    <div id="candle-chart"></div>
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
  <div class="card">
    <div class="card-title">Registrar movimiento de capital</div>
    <div class="form-grid">
      <div class="form-group">
        <label>Tipo</label>
        <select id="mv-type">
          <option value="DEPOSIT">Ingreso</option>
          <option value="WITHDRAWAL">Egreso</option>
        </select>
      </div>
      <div class="form-group">
        <label>Monto (USDT)</label>
        <input type="number" id="mv-amount" min="1" step="0.01" placeholder="100.00">
      </div>
      <div class="form-group" style="grid-column:1/-1">
        <label>Descripción</label>
        <input type="text" id="mv-desc" placeholder="Depósito inicial, retiro de ganancias...">
      </div>
    </div>
    <div style="margin-top:12px"><button class="btn-primary" onclick="submitMovement()">Registrar movimiento</button></div>
    <div id="mv-alert" style="margin-top:10px"></div>
  </div>
  <div class="card">
    <div class="card-title">Historial de movimientos de capital</div>
    <div class="tbl-wrap" id="movements-wrap"></div>
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
  <div class="two-col">
    <div class="card">
      <div class="card-title">API Keys Binance (encriptadas en BD)</div>
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
          <label>Modo</label>
          <select id="cfg-testnet">
            <option value="true">TESTNET (recomendado)</option>
            <option value="false">PRODUCCIÓN (dinero real)</option>
          </select>
        </div>
        <div class="form-group">
          <label>Capital inicial (USDT)</label>
          <input type="number" id="cfg-capital" min="10" step="1">
        </div>
      </div>
      <div style="margin-top:12px"><button class="btn-primary" onclick="saveApiConfig()">Guardar API Keys</button></div>
      <div id="api-alert" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <div class="card-title">Parámetros de la estrategia D (solo visualización)</div>
      <table class="cfg-table" id="cfg-table"></table>
      <div style="margin-top:14px;font-size:11px;color:var(--muted)">
        Los parámetros de estrategia se configuran directamente en el código
        para garantizar integridad del backtesting validado.
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Cambiar contraseña</div>
    <div class="form-grid">
      <div class="form-group">
        <label>Nueva contraseña (mín. 8 caracteres)</label>
        <input type="password" id="new-pw" placeholder="Nueva contraseña">
      </div>
      <div class="form-group">
        <label>Confirmar</label>
        <input type="password" id="new-pw2" placeholder="Repetir contraseña">
      </div>
    </div>
    <div style="margin-top:12px"><button class="btn-primary" onclick="changePassword()">Cambiar contraseña</button></div>
    <div id="pw-alert" style="margin-top:10px"></div>
  </div>
</div>

<!-- Telegram config (full width below settings grid) -->
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
  if (name === 'chart') loadChart('1h');
  if (name === 'trades') loadTrades();
  if (name === 'capital') loadCapital();
  if (name === 'signals') loadSignals();
  if (name === 'settings') loadConfig();
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
    <td style="color:var(--muted);font-size:11px">${t.opened_at?.substring(0,16)||'—'}</td>
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

// ── Candlestick chart ──────────────────────────────────────────────
async function loadChart(tf, el) {
  if (el) {
    document.querySelectorAll('.tf-pill').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
  }
  const r = await fetch(`/api/candles?interval=${tf}&limit=200`);
  const d = await r.json();
  if (!d.ok) return;

  const container = document.getElementById('candle-chart');
  container.innerHTML = '';

  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 400,
    layout: { background: { color: '#0f1520' }, textColor: '#c8d8e8' },
    grid: {
      vertLines: { color: '#1a2535' },
      horzLines: { color: '#1a2535' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#1a2535' },
    timeScale: { borderColor: '#1a2535', timeVisible: true },
  });

  const series = chart.addCandlestickSeries({
    upColor: '#00e676', downColor: '#ff3d5a',
    borderUpColor: '#00e676', borderDownColor: '#ff3d5a',
    wickUpColor: '#00e676', wickDownColor: '#ff3d5a',
  });

  const candles = d.candles.map(c => ({
    time: Math.floor(c.t / 1000),
    open: c.open, high: c.high, low: c.low, close: c.close
  }));
  series.setData(candles);

  // Markers para trades
  if (d.markers?.length) {
    const markers = d.markers
      .filter(m => m.price && m.ts)
      .map(m => ({
        time: Math.floor(new Date(m.ts).getTime() / 1000),
        position: m.side === 'LONG'
          ? (m.type === 'open' ? 'belowBar' : 'aboveBar')
          : (m.type === 'open' ? 'aboveBar' : 'belowBar'),
        color: m.type === 'open' ? '#aa66ff' : (m.result === 'WIN' ? '#00e676' : '#ff3d5a'),
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
    chart.resize(container.clientWidth, 400);
  });

  candleChart = chart;
}

// ── Capital ────────────────────────────────────────────────────────
async function loadCapital() {
  const r = await fetch('/api/capital');
  const d = await r.json();
  if (!d.ok) return;
  const wrap = document.getElementById('movements-wrap');
  wrap.innerHTML = `<table>
    <thead><tr><th>Fecha</th><th>Tipo</th><th>Monto</th><th>Saldo después</th><th>Descripción</th></tr></thead>
    <tbody>${d.movements.map(m => `<tr>
      <td style="color:var(--muted);font-size:11px">${m.ts?.substring(0,16)}</td>
      <td><span class="pill ${m.type==='DEPOSIT'?'win':'loss'}">${m.type==='DEPOSIT'?'↑ Ingreso':'↓ Egreso'}</span></td>
      <td class="${(m.amount||0)>=0?'cv':'cr'}">${(m.amount||0)>=0?'+':''}$${Math.abs(m.amount||0).toFixed(2)}</td>
      <td>$${(m.balance_after||0).toLocaleString()}</td>
      <td style="color:var(--muted)">${m.description||'—'}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

async function submitMovement() {
  const type   = document.getElementById('mv-type').value;
  const amount = parseFloat(document.getElementById('mv-amount').value);
  const desc   = document.getElementById('mv-desc').value;
  const alert  = document.getElementById('mv-alert');
  if (!amount || amount <= 0) {
    alert.innerHTML = '<div class="alert err">Monto inválido</div>'; return;
  }
  const r = await fetch('/api/capital/movement', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({type, amount, description: desc})
  });
  const d = await r.json();
  alert.innerHTML = d.ok
    ? `<div class="alert ok">✓ Registrado. Saldo: $${d.balance_after?.toFixed(2)}</div>`
    : `<div class="alert err">Error: ${d.error}</div>`;
  if (d.ok) { loadCapital(); loadSummary(); }
}

// ── Signals ────────────────────────────────────────────────────────
async function loadSignals() {
  const [rs, re] = await Promise.all([fetch('/api/signals'), fetch('/api/events')]);
  const [ds, de] = await Promise.all([rs.json(), re.json()]);

  if (ds.ok) {
    document.getElementById('signals-wrap').innerHTML = `<table>
      <thead><tr><th>Fecha</th><th>Dirección</th><th>Bias Log</th><th>ACP</th><th>Macro</th><th>Ejecutada</th><th>Razón</th></tr></thead>
      <tbody>${ds.signals.map(s => `<tr>
        <td style="color:var(--muted);font-size:11px">${s.ts?.substring(0,16)}</td>
        <td>${s.direction===1?'<span class="pill long">↑ LONG</span>':s.direction===-1?'<span class="pill short">↓ SHORT</span>':'<span style="color:var(--muted)">HOLD</span>'}</td>
        <td>${s.log_bias===1?'▲ Alcista':'▼ Bajista'}</td>
        <td>${(s.acp_angle||0).toFixed(4)}°</td>
        <td>${s.macro_ok?'✓':'✗'}</td>
        <td>${s.executed?'<span class="pill win">✓</span>':'<span class="pill loss">✗</span>'}</td>
        <td style="color:var(--muted);font-size:11px">${s.reason_skip||'—'}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  }

  if (de.ok) {
    document.getElementById('events-wrap').innerHTML = `<table>
      <thead><tr><th>Fecha</th><th>Nivel</th><th>Evento</th><th>Detalle</th></tr></thead>
      <tbody>${de.events.map(e => `<tr>
        <td style="color:var(--muted);font-size:11px">${e.ts?.substring(0,16)}</td>
        <td><span style="color:${e.level==='ERROR'?'var(--bear)':e.level==='WARNING'?'var(--gold)':'var(--muted)'}">${e.level}</span></td>
        <td>${e.event}</td>
        <td style="color:var(--muted);font-size:11px">${e.detail||''}</td>
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

  // Prellenar campos editables
  if (cfg.testnet) document.getElementById('cfg-testnet').value = cfg.testnet;
  if (cfg.capital_initial) document.getElementById('cfg-capital').value = cfg.capital_initial;
  // Telegram (no mostramos token/chat_id por seguridad, solo opciones)
  if (cfg.telegram_notify_filtered) {
    document.getElementById('tg-filtered').value = cfg.telegram_notify_filtered;
  }
  if (cfg.telegram_notify_errors) {
    document.getElementById('tg-errors').value = cfg.telegram_notify_errors;
  }
  if (cfg.telegram_chat_id) {
    document.getElementById('tg-chat-id').value = cfg.telegram_chat_id;
  }
}

async function saveApiConfig() {
  const data = {};
  const key    = document.getElementById('cfg-api-key').value.trim();
  const secret = document.getElementById('cfg-secret').value.trim();
  const testnet = document.getElementById('cfg-testnet').value;
  const capital = document.getElementById('cfg-capital').value;
  if (key)    data['binance_api_key'] = key;
  if (secret) data['binance_secret']  = secret;
  data['testnet']          = testnet;
  data['capital_initial']  = capital;

  const r = await fetch('/api/config/update', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(data)
  });
  const d = await r.json();
  const alert = document.getElementById('api-alert');
  alert.innerHTML = d.ok
    ? '<div class="alert ok">✓ Configuración guardada. Conexión Binance verificada.</div>'
    : `<div class="alert err">Error: ${d.error}</div>`;
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
