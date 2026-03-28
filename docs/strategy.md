================================================================================
SATEVIS — DOCUMENTACIÓN TÉCNICA DEL SISTEMA DE TRADING AUTOMATIZADO
================================================================================
Exchange  : Binance Futures BTCUSDT (USDM-Perpetuo)
Timeframe : 1H (señales) / 4H (filtro macro)
Estrategia activa: Strategy D — Log+EMA
Estado    : Pendiente deploy en Railway
Versión   : 1.0-rc
================================================================================


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 01 — LÓGICA DE ENTRADA (CONDICIONES EXACTAS)
────────────────────────────────────────────────────────────────────────────────

Todas las condiciones deben cumplirse simultáneamente (AND lógico).

1.1 FILTRO MACRO — EMA 200 (4H) [obligatorio]
  - El precio de cierre de la vela actual debe estar POR ENCIMA de la EMA 200
    calculada en timeframe 4H.
  - Confirma que el mercado está en tendencia alcista macro.
  - Solo se abren posiciones LONG. No hay operaciones en corto.
  - Si el precio cierra por debajo de la EMA 200 (4H), el bot omite la señal
    aunque todos los demás filtros pasen.

1.2 SEÑAL PRIMARIA — Cruce alcista SMA_Log 144 (1H)
  - Se calcula la SMA_Log 144: media aritmética de los últimos 144 logaritmos
    de precios de cierre, luego exponenciada.
  - Fórmula: SMA_Log(144) = exp( mean( log(close[-144:]) ) )
  - Condición: el precio de cierre cruza AL ALZA la SMA_Log 144 en vela 1H.
  - La vela anterior debía estar por debajo (cruce limpio, no retest ni falso).

1.3 FILTRO ACP — Ángulo de Cruce de Pendientes (calidad de señal)
  - Se calcula el ángulo entre la pendiente del precio y la pendiente de la
    SMA_Log en el momento del cruce.
  - Fórmula:
      m_precio = (close[t] - close[t-1]) / close[t-1]
      m_sma    = (SMA_Log[t] - SMA_Log[t-1]) / SMA_Log[t-1]
      ACP = arctan( |m_precio - m_sma| / (1 + m_precio * m_sma) ) * (180/pi)
  - Condición: ACP >= 0.0473° (umbral optimizado en backtesting 2020–2025).
  - Objetivo: descartar cruces débiles, laterales o con baja convicción.

1.4 ENTRADA EN POI AL 50%
  - No se entra al precio de mercado en el momento del cruce.
  - Se calcula un Point of Interest (POI) = 50% del cuerpo de la vela de cruce.
  - Fórmula: POI = high_vela_cruce - 0.5 * (high_vela_cruce - low_vela_cruce)
  - Se coloca una orden LIMIT en ese precio.
  - Si el precio retrocede al POI en las siguientes 4 velas → entrada ejecutada.
  - Si no retrocede en 4 horas → señal invalidada, orden cancelada.

RESUMEN DE CONDICIONES:
  [precio > EMA200_4H] AND [cruce alcista SMA_Log_144_1H] AND [ACP >= 0.0473°]
  AND [precio retrocede al POI 50% en <= 4 velas]


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 02 — LÓGICA DE SALIDA
────────────────────────────────────────────────────────────────────────────────

2.1 TAKE PROFIT (TP)
  - Fijo en +3% desde el precio de entrada.
  - Cálculo: precio_TP = precio_entrada * 1.03
  - Se coloca como orden LIMIT al momento de abrir la posición.
  - El TP de 3% fue optimizado como el mejor balance entre frecuencia de éxito
    y retorno por operación en backtesting.

2.2 STOP LOSS (SL)
  - Fijo en -2% desde el precio de entrada.
  - Cálculo: precio_SL = precio_entrada * 0.98
  - Se coloca como orden STOP al momento de abrir la posición.
  - Ratio TP/SL = 1.5:1.
  - Con margen aislado y 3x leverage: una pérdida del -2% en precio equivale
    a -6% del margen comprometido. El nivel de liquidación requiere -33%,
    por lo que el SL actúa siempre antes.

2.3 SALIDA POR INVALIDACIÓN DE SEÑAL
  - Si el precio cierra por debajo de la EMA 200 (4H) mientras la posición
    está abierta → cierre inmediato a mercado (invalidación macro).
  - Si la orden limit del POI no se llena en 4 velas horarias → cancelación.

2.4 DETECCIÓN DE LIQUIDACIÓN
  - El bot consulta el estado de la posición en cada ciclo horario via API.
  - Si la posición desaparece sin haber tocado TP ni SL → se registra como
    liquidación forzada en la base de datos y se registra en logs.
  - Resultado en backtesting 2020–2025: 0 liquidaciones forzadas.


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 03 — INDICADORES Y PARÁMETROS
────────────────────────────────────────────────────────────────────────────────

PARÁMETROS DE ESTRATEGIA:
  Indicador principal   : SMA_Log (período 144, timeframe 1H)
  Filtro macro          : EMA 200 (timeframe 4H)
  Umbral ACP            : >= 0.0473°
  Take Profit           : +3.0% sobre precio de entrada
  Stop Loss             : -2.0% sobre precio de entrada
  Leverage              : 3x (margen aislado / isolated)
  Tipo de posición      : Solo LONG
  Entrada               : Orden limit en POI 50% del cuerpo de vela
  Tiempo máximo de fill : 4 velas (4 horas)
  Par operado           : BTCUSDT Perpetuo (Binance Futures USDM)
  Ciclo del bot         : 60 minutos (APScheduler)
  Capital por operación : Configurable desde el dashboard (en USDT)

FÓRMULA SMA_Log:
  SMA_Log(n) = exp( (1/n) * Σ ln(close_i) ) para i = t-n+1 ... t
  Equivalente a la media geométrica del precio:
  SMA_Log(n) = (close_{t-n+1} * close_{t-n+2} * ... * close_t) ^ (1/n)
  Ventaja: menos reactiva a picos extremos que la SMA aritmética.
  Modela mejor el precio de BTC por su distribución log-normal.

FÓRMULA ACP:
  m_precio = (close[t] - close[t-1]) / close[t-1]
  m_sma    = (SMA_Log[t] - SMA_Log[t-1]) / SMA_Log[t-1]
  ACP = arctan( |m_precio - m_sma| / (1 + m_precio * m_sma) ) * (180 / pi)
  Umbral validado en backtesting: ACP >= 0.0473° → entrada válida.


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 04 — GESTIÓN DE RIESGO
────────────────────────────────────────────────────────────────────────────────

PARÁMETROS DE RIESGO:
  Riesgo en precio por operación  : 2% (SL a -2% del precio de entrada)
  Riesgo en margen (3x leverage)  : 6% del capital comprometido por trade
  Nivel de liquidación (3x iso.)  : ~33% de caída desde entrada → muy lejano
  Operaciones simultáneas         : 1 (no se abre nueva posición si hay una abierta)
  Tipo de margen                  : Isolated (pérdida limitada al margen del trade)
  Funding rate                    : Monitorizado y descontado en backtesting

CÁLCULO DE TAMAÑO DE POSICIÓN:
  capital_operacion  = USDT definido por el usuario en el dashboard
  tamaño_contrato    = (capital_operacion * leverage) / precio_entrada
  margen_requerido   = capital_operacion (aislado)

  Pérdida máxima real  = capital_operacion * 0.06  (SL -2% * 3x leverage)
  Ganancia máxima real = capital_operacion * 0.09  (TP +3% * 3x leverage)

COSTOS REALES (descontados en backtest y producción):
  Comisión Binance (taker) : 0.04% * tamaño_notional * 2 (entrada + salida)
  Funding rate estimado    : ~0.01% cada 8 horas en promedio (perpetuos)
  Ambos costos se descuentan del resultado neto de cada operación.

NOTA DE SEGURIDAD:
  El SL de -2% en precio con 3x leverage aislado implica -6% del margen.
  La liquidación ocurre a -33% del precio → el SL actúa siempre primero.
  Confirmado en backtesting 2020–2025: 0 liquidaciones forzadas.


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 05 — CONDICIONES DE MERCADO DONDE FUNCIONA MEJOR
────────────────────────────────────────────────────────────────────────────────

  - Mercados con tendencia alcista sostenida: el filtro EMA 200 (4H) garantiza
    operar solo cuando el mercado está en fase macro alcista. Los períodos
    2020–2021 y 2023–2024 de BTC son los de mejor rendimiento histórico.

  - Baja lateralización: la estrategia se desactiva automáticamente en mercados
    bajistas prolongados (precio bajo EMA 200). El filtro ACP adicionalmente
    filtra cruces en rangos laterales dentro de una tendencia.

  - Alta volatilidad intradiaria: el ACP se activa con más frecuencia en
    mercados volátiles, generando más señales de calidad por período.

  - Horas de KillZone: aunque no es un filtro activo en la estrategia, las
    señales en ventanas London (08:00–11:00 UTC) y NY (13:30–16:30 UTC)
    tienen históricamente mejor seguimiento de momentum.

  - Mercados bajistas (2022): al estar el filtro EMA 200 activo, el bot no
    generó entradas durante el bear market de 2022, evitando la mayor parte
    del drawdown de ese período.


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 06 — ERRORES DETECTADOS Y CORRECCIONES APLICADAS
────────────────────────────────────────────────────────────────────────────────

ERROR 1: Entrada a precio de mercado en el cruce
  Impacto   : Sobrecompra en picos, R:R deteriorado
  Corrección: Cambio a orden limit en POI 50% del cuerpo de la vela de cruce

ERROR 2: SMA aritmética (SMA simple) como indicador base
  Impacto   : Reactividad excesiva a picos de precio en BTC (distribución asimétrica)
  Corrección: Reemplazada por SMA_Log (media geométrica) → más suavidad y precisión

ERROR 3: Sin filtro de calidad del cruce
  Impacto   : Muchos cruces falsos en períodos de lateralización
  Corrección: Implementación del indicador ACP con umbral optimizado 0.0473°

ERROR 4: Sin filtro macro de tendencia
  Impacto   : Señales LONG generadas en tendencias bajistas (pérdidas sistemáticas)
  Corrección: Adición de EMA 200 en 4H como condición obligatoria de entrada

ERROR 5: Leverage inicial de 5x
  Impacto   : Riesgo de liquidación en eventos de volatilidad extrema
  Corrección: Reducción a 3x → confirmado 0 liquidaciones en backtest 5 años

ERROR 6: TP inicial de 5%
  Impacto   : Baja tasa de acierto (el precio no alcanzaba el objetivo frecuentemente)
  Corrección: Optimizado a TP=3% → mejor balance win rate / retorno por operación

ERROR 7: Costos reales no contabilizados en backtest
  Impacto   : Resultados inflados, inconsistentes con la realidad del mercado
  Corrección: Inclusión explícita de comisión taker (0.04%) y funding (~0.01% c/8h)

ERROR 8: API keys almacenadas en variables de entorno de Railway
  Impacto   : Exposición potencial en logs, dificultad de rotación sin redeploy
  Corrección: Almacenamiento en SQLite cifrado, gestionables desde el dashboard

ERROR 9: Build timeout en Railway al momento del deploy
  Impacto   : Bot no desplegado en producción
  Diagnóstico: Problema de infraestructura Railway, no de código
  Corrección: Dockerfile explícito (Python 3.11-slim) + railway.toml actualizado
              + gunicorn como servidor WSGI + evaluar cambio de región de deploy


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 07 — SUPUESTOS CLAVE DE LA ESTRATEGIA
────────────────────────────────────────────────────────────────────────────────

  1. BTC tiende a recuperar y superar máximos históricos en ciclos de 3–4 años.
     El filtro EMA 200 captura correctamente estas fases alcistas macro.

  2. Las medias geométricas (SMA_Log) modelan mejor el precio de BTC que las
     medias aritméticas, dado que el precio sigue una distribución log-normal.

  3. El mercado de futuros USDM de Binance es suficientemente líquido para
     ejecutar órdenes limit en BTCUSDT sin slippage significativo.

  4. Los costos de funding rate se mantienen en rangos históricos (~0.01% c/8h
     en promedio). Un funding rate anómalamente alto podría invalidar algunos trades.

  5. El exchange (Binance) permanece operativo y con la API disponible. Se asume
     una disponibilidad del 99.9%.

  6. La comisión de taker del 0.04% se mantiene vigente. Con BNB como token de
     pago de fees podría reducirse a 0.036%.

  7. Solo operaciones LONG. No se opera en corto. Se asume que los retornos
     asimétricos de BTC históricamente favorecen posiciones largas.

  8. El capital por operación permanece constante (no hay compounding automático
     salvo configuración manual desde el dashboard).


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 08 — BACKTESTING INSIGHTS
────────────────────────────────────────────────────────────────────────────────

DATASET Y METODOLOGÍA:
  - Datos reales: velas 1H y 4H de BTCUSDT en Binance Futures, período 2020–2025.
  - Simulación realista: se modelaron comisiones taker (0.04%), funding rates
    históricos reales, y slippage estimado en órdenes limit.
  - No se usó look-ahead bias: los indicadores se calculan solo con datos
    disponibles hasta el cierre de la vela evaluada.
  - El umbral ACP (0.0473°) fue determinado por optimización exhaustiva sobre
    el dataset completo y validado en subperíodos para evitar overfitting.

HALLAZGOS CLAVE:
  - El filtro EMA 200 (4H) eliminó prácticamente todas las operaciones durante
    el bear market de 2022, reduciendo el drawdown máximo de forma sustancial.

  - El ACP >= 0.0473° redujo el número de señales en aproximadamente 40%
    pero aumentó la tasa de acierto en ~18 puntos porcentuales respecto a
    versiones sin este filtro.

  - La entrada en POI 50% mejoró el ratio riesgo/beneficio promedio vs entrada
    a precio de mercado en aproximadamente 0.3 unidades por operación.

  - El período 2020–2021 generó los mayores retornos absolutos (bull market BTC).
    El período 2023–2024 confirma la robustez de la estrategia en nuevos ciclos
    alcistas con parámetros sin modificar.

  - Ninguna operación fue liquidada en 5 años de backtest completo (2020–2025)
    con 3x leverage aislado y SL fijo en -2%.

  - Todos los retornos reportados son NETOS de comisiones y funding rates.


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 09 — RESULTADOS Y VERSIONES ANTERIORES
────────────────────────────────────────────────────────────────────────────────

VERSIONES PRELIMINARES (Estrategias A / B / C):
  - SMA simple (aritmética) como indicador base.
  - Sin filtro ACP → muchas señales falsas en lateralización.
  - Sin filtro EMA 200 → señales LONG en tendencias bajistas.
  - Win rate bajo (~45–52% dependiendo de la versión).
  - Drawdowns significativos durante el bear market de 2022.
  - Costos reales no modelados → backtest inflado respecto a la realidad.
  - Descartadas tras análisis comparativo con Strategy D.

STRATEGY D — Log+EMA [ESTRATEGIA ACTIVA]:
  - SMA_Log 144 + EMA 200 (4H) + ACP >= 0.0473°
  - TP 3%, SL 2%, Leverage 3x, POI 50%
  - Retorno anualizado: fuertemente positivo en el período 2020–2025
  - Win rate optimizado con filtros de calidad de señal
  - 0 liquidaciones en 5 años de backtesting
  - Todos los costos reales (fees + funding) descontados explícitamente

STRATEGY G — Spot (variante conservadora):
  - SMA_Log 144 / EMA 72 en mercado spot (sin leverage).
  - Sin funding rate, sin riesgo de liquidación, sin margen.
  - Win rate más alto que Strategy D.
  - Retorno absoluto menor (sin apalancamiento).
  - Alternativa para perfil conservador o capital que no puede estar en futuros.
  - Estado: validada en backtest. No es la estrategia principal activa.

NOTA: Los valores exactos de retorno anualizado y win rate dependen del capital
inicial y configuración de tamaño de posición. Los rangos fueron validados
internamente en sesiones de backtesting conjunto con datos reales de Binance.


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 10 — DASHBOARD — CRITERIOS DE DESARROLLO
────────────────────────────────────────────────────────────────────────────────

ACCESO Y AUTENTICACIÓN:
  - Dashboard privado con acceso por usuario/contraseña (sin registro público).
  - Sesión basada en Flask-Login con token de sesión seguro (cookie firmada).
  - Credenciales almacenadas en SQLite con hash bcrypt. No en variables de entorno.
  - URL accesible vía dominio Railway (HTTPS automático).

VISTAS Y FUNCIONES DEL DASHBOARD:

  Vista 1 — Métricas principales:
    Balance actual en Binance, P&L total acumulado, P&L del día en curso,
    número total de operaciones, win rate en tiempo real, drawdown máximo.

  Vista 2 — Posición activa:
    Par operado, lado (LONG), precio de entrada, precio TP, precio SL,
    PnL flotante actualizado en tiempo real, tiempo abierta la posición.

  Vista 3 — Historial de operaciones:
    Tabla con: fecha/hora, precio entrada, precio salida, resultado (%),
    resultado (USDT neto), comisiones pagadas, funding descontado, tipo de salida
    (TP / SL / manual / liquidación).

  Vista 4 — Control del bot:
    Botón Start/Stop del bot, configuración de capital por operación (USDT),
    activar/desactivar estrategia D, estado del scheduler (activo/inactivo).

  Vista 5 — Gestión de API keys:
    Campo para ingresar/actualizar Binance API Key y Secret.
    Las claves se almacenan cifradas en la base de datos SQLite.
    No se muestran en texto plano después de guardadas.

  Vista 6 — Log de actividad:
    Registro cronológico de todas las acciones del bot:
    señales detectadas, órdenes enviadas, fills confirmados, errores de API,
    inicio/fin de ciclos horarios.

TECNOLOGÍA DEL DASHBOARD:
  Backend  : Flask (Python). Rutas REST + templates Jinja2.
  Frontend : HTML/CSS/JS mínimo. Sin frameworks pesados. Auto-refresh cada 30s.
  DB       : SQLite. Tablas: trades, config, logs, api_keys (cifradas).


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 11 — FUNCIONES ACTUALES Y PROYECTADAS DEL BOT
────────────────────────────────────────────────────────────────────────────────

FUNCIONES ACTUALES (implementadas en código de producción):
  - Consulta de velas 1H y 4H en Binance Futures via API REST.
  - Cálculo de SMA_Log 144, EMA 200 (4H) y ACP en cada ciclo horario.
  - Detección de cruce alcista y validación de filtros (ACP + EMA 200).
  - Colocación de orden limit en POI 50% del cuerpo de vela de cruce.
  - Colocación automática de TP (+3%) y SL (-2%) al llenar la entrada.
  - Cancelación de orden limit si no se llena en 4 velas.
  - Detección de liquidación forzada (posición desaparece sin TP/SL).
  - Persistencia de todas las operaciones y logs en SQLite.
  - Dashboard Flask con métricas en tiempo real.
  - Ciclo horario automático con APScheduler.
  - Gestión de API keys cifradas en base de datos.

FUNCIONES PROYECTADAS (próximas iteraciones):
  - Soporte multi-par: extender señales a ETHUSDT, SOLUSDT y otros pares
    con parámetros independientes por par.
  - Compounding automático: reinversión configurable de ganancias en el
    tamaño de la próxima operación.
  - Notificaciones Telegram: alertas de entrada, salida, error de API
    y resumen diario de rendimiento.
  - Exportación de historial a Google Sheets para análisis externo.
  - Trailing stop dinámico: mover el SL a breakeven cuando el precio
    alcance el 50% del TP.
  - Backtesting on-demand desde el dashboard: ingresar parámetros y
    ver resultado simulado sobre datos históricos.
  - Modo paper trading: simular operaciones con lógica real sin capital
    real para validación en vivo antes de arriesgar capital.


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 12 — HERRAMIENTAS Y PLATAFORMAS
────────────────────────────────────────────────────────────────────────────────

  Componente              | Herramienta/Plataforma        | Rol
  ─────────────────────────────────────────────────────────────────────────────
  Exchange de trading     | Binance Futures (USDM)        | Ejecución de órdenes reales
  Conexión API            | Binance REST API v2 /         | Consulta de mercado, envío de
                          | python-binance                | órdenes, estado de cuenta
  Backend del bot         | Python 3.11 + Flask           | Servidor web, lógica de estrategia
  Scheduler de ciclos     | APScheduler (Blocking)        | Ciclo horario automático
  Base de datos           | SQLite (sqlite3 nativo)       | Trades, config, logs, API keys
  Deploy / Hosting        | Railway.app                   | Contenedor cloud. Dashboard URL
  Containerización        | Docker (Dockerfile explícito) | Build reproducible Python 3.11-slim
  WSGI server             | Gunicorn + wsgi.py            | Servidor producción Flask en Railway
  Lenguaje principal      | Python 3.11                   | Stack completo del bot
  Integraciones futuras   | Telegram Bot API              | Notificaciones (proyectado)
                          | Google Sheets API             | Exportación de datos (proyectado)
  Análisis / Backtesting  | Pandas, NumPy, Jupyter        | Desarrollo y optimización offline
  ─────────────────────────────────────────────────────────────────────────────


────────────────────────────────────────────────────────────────────────────────
SECCIÓN 13 — ETAPAS REALIZADAS Y PENDIENTES
────────────────────────────────────────────────────────────────────────────────

ETAPAS COMPLETADAS:

  [✓] Definición de la estrategia base
      Exploración de conceptos SMC/ICT, indicador ACP personalizado,
      modelado logarítmico (SMA_Log). Selección del enfoque Log+EMA
      como estrategia principal de desarrollo.

  [✓] Backtesting multi-versión (Estrategias A, B, C, D, G)
      Pruebas con datos reales BTCUSDT 1H y 4H, período 2020–2025.
      Selección de Strategy D como la de mejor desempeño ajustado al riesgo.

  [✓] Optimización de parámetros
      TP, SL, leverage, umbral ACP, período SMA_Log optimizados iterativamente.
      Validación explícita de 0 liquidaciones con parámetros finales.

  [✓] Modelado real de costos en backtesting
      Inclusión de comisiones taker (0.04%), funding rates históricos reales
      y slippage estimado. Resultados netos vs resultados brutos.

  [✓] Desarrollo del bot de producción
      Código completo: Flask dashboard, SQLite, Binance API client,
      motor de Strategy D, trade executor, ciclo APScheduler,
      detección de liquidación, sistema de logs.

  [✓] Configuración de Railway + Dockerfile
      Dockerfile explícito (Python 3.11-slim), railway.toml con comando
      de inicio gunicorn, wsgi.py como punto de entrada WSGI.
      Intento de deploy completado (bloqueado por build timeout).

ETAPAS PENDIENTES:

  [ ] Resolver build timeout en Railway
      Pasos: Redeploy con configuración actualizada → cambiar región de
      deploy si persiste → verificar plan (gratuito tiene límites de build)
      → usar Dockerfile multi-stage para reducir tiempo de build.

  [ ] Primer deploy exitoso y verificación en vivo
      Confirmar que el dashboard es accesible via HTTPS, el scheduler
      arranca correctamente, y la API de Binance responde sin errores.

  [ ] Configurar API keys reales de Binance Futures
      Ingresar API Key y Secret en el dashboard.
      Verificar permisos: Futures Trading habilitado, IP restringida
      a la IP saliente de Railway, sin permisos de retiro.

  [ ] Paper trading en vivo (modo simulado)
      Correr el bot con lógica real pero sin capital comprometido.
      Verificar que las señales detectadas en vivo coinciden con las
      expectativas del backtesting (frecuencia, calidad de señales).
      Duración recomendada: mínimo 1–2 semanas de observación.

  [ ] Inicio de trading real con capital mínimo
      Primera operación real con capital reducido para validar la cadena
      completa: señal → orden enviada → fill confirmado → TP/SL activos
      → registro correcto en base de datos → dashboard actualizado.

  [ ] Monitoreo de desempeño vs benchmarks del backtest
      Comparar win rate, retorno neto y drawdown en vivo vs los valores
      del backtest. Identificar divergencias y ajustar si es necesario.

  [ ] Implementar notificaciones Telegram
      Integrar bot de Telegram para alertas de: señal detectada, orden
      ejecutada, TP/SL alcanzado, error crítico de API, resumen diario.

  [ ] Escalado de capital y optimización continua
      Con resultados validados en vivo durante varias semanas:
      incrementar el capital por operación gradualmente.
      Evaluar activación de compounding automático.
      Evaluar expansión a otros pares (ETHUSDT, SOLUSDT).

PRÓXIMO PASO CRÍTICO:
  Resolver el build timeout de Railway. Una vez el bot esté online,
  la prioridad absoluta es el paper trading antes de exponer capital real.


================================================================================
FIN DEL DOCUMENTO — SATEVIS v1.0-rc
Generado: 2026-03-28
================================================================================
