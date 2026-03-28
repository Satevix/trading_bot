# SATEVIS Bot — Instrucciones de despliegue

## Estructura
```
satevis_bot/
├── main.py                  ← Punto de entrada (Railway ejecuta esto)
├── requirements.txt
├── railway.toml
├── core/
│   ├── database.py          ← SQLite: todos los datos persistentes
│   ├── binance_futures.py   ← Cliente Binance Futures API
│   └── executor.py          ← Ejecutor de trades
├── strategy/
│   └── strategy_d.py        ← Motor estrategia D (señales)
└── dashboard/
    └── dashboard.py         ← Flask: portal web con login
```

--- 

## Despliegue en Railway

### 1. Crear cuenta en Railway
- railway.app → New Project → Deploy from GitHub

### 2. Subir el código
```bash
# Desde la carpeta satevis_bot/
git init
git add .
git commit -m "SATEVIS Bot v1.0"
git remote add origin <tu-repo-github>
git push -u origin main
```

### 3. Configurar variables de entorno en Railway
En Railway → tu proyecto → Variables:
```
FLASK_SECRET=genera_una_clave_aleatoria_larga_aqui
ENVIRONMENT=production
PORT=5000
DB_PATH=/app/satevis.db
```

### 4. Agregar volumen persistente en Railway
Railway → tu proyecto → Add Volume → Mount path: `/app`
(Esto asegura que la base de datos SQLite no se borre en cada deploy)

### 5. Primer acceso
- URL del dashboard: https://tu-proyecto.railway.app
- Usuario: `admin`
- Contraseña: `satevis2024`
- **Cambiar contraseña inmediatamente en Configuración**

---

## Configuración inicial en el dashboard

### Paso 1 — API Keys
1. Ir a Configuración → API Keys Binance
2. Crear API key en https://testnet.binancefuture.com (testnet primero)
3. Pegar API Key y Secret Key
4. Seleccionar modo: TESTNET
5. Guardar → el sistema verificará la conexión automáticamente

### Paso 2 — Capital inicial
1. En Configuración → Capital inicial: ingresar el monto con el que empiezas
2. Guardar

### Paso 3 — Registrar depósito inicial
1. Ir a Capital → Registrar movimiento
2. Tipo: Ingreso
3. Monto: tu capital inicial en USDT
4. Descripción: "Capital inicial testnet"

### Paso 4 — Activar el bot
1. Ir al dashboard principal
2. Clic en el badge "DETENIDO" → cambia a "ACTIVO"
3. El bot ejecutará su primer ciclo en el próximo :01 de la hora

---

## Parámetros de la Estrategia D (no modificar sin nuevo backtesting)
| Parámetro | Valor | Descripción |
|---|---|---|
| SMA Log | 288 períodos | Media geométrica logarítmica |
| EMA señal | 144 períodos | Señal rápida en 4H |
| EMA Macro | 200 períodos | Filtro de tendencia macro |
| ACP Umbral | 0.04735° | Ángulo mínimo de cruce EMA50/EMA200 |
| Stop Loss | 1.5% | Desde precio de entrada |
| Take Profit | 3.0% | RR 2:1 efectivo (RR 3:1 con apalancamiento) |
| Riesgo/trade | 1% | Del capital disponible |
| Apalancamiento | 3× | Margen aislado (Isolated) |
| Timeframe | 4H | Para señal logarítmica |

---

## Notas de seguridad
- Las API Keys se guardan en la BD local (SQLite) en el volumen de Railway
- Nunca habilitar permisos de retiro en las API Keys de Binance
- Usar siempre restricción por IP en las API Keys (IP del servidor Railway)
- La contraseña del dashboard se almacena como hash SHA256

---

## Monitoreo
- El bot genera un ciclo cada hora en el minuto :01
- Health check cada 6 horas (verifica conexión Binance)
- Todos los eventos quedan en la tabla `bot_events` de la BD
- Ver en dashboard → Señales → Log de eventos

---

## Migración a producción (cuando el testnet sea estable)
1. Crear nuevas API Keys en binance.com (no testnet) con permisos de Futuros
2. En dashboard → Configuración → API Keys: pegar nuevas keys
3. Cambiar modo de TESTNET a PRODUCCIÓN
4. Empezar con capital mínimo ($100) y verificar 2-3 trades reales
5. Escalar capital gradualmente
