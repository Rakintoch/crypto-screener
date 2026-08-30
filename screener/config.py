"""
Configuração central do screener.
Todos os limiares podem ser ajustados aqui sem tocar na lógica.
"""
import os

# --- Telegram (via GitHub Actions secrets) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Camada 1: CEX small-caps (CoinGecko) ---
# Janela de ranking por market cap a vasculhar (evita as top coins, foca no meio/cauda da tabela)
COINGECKO_RANK_START_PAGE = 2      # página 2 de 250 = rank ~251 em diante
COINGECKO_RANK_END_PAGE = 12       # até página 12 = rank ~3000
COINGECKO_PER_PAGE = 250
COINGECKO_MIN_MARKET_CAP = 1_000_000        # 1M USD
COINGECKO_MAX_MARKET_CAP = 150_000_000      # 150M USD (acima disto já não é "menor/rápido")
COINGECKO_MIN_VOLUME_USD = 75_000           # liquidez mínima de negociação em 24h
COINGECKO_MIN_TURNOVER = 0.12               # volume24h / market_cap mínimo (sinal de interesse)

# --- Camada 2: DEX micro-caps (GeckoTerminal + DexScreener) ---
GECKOTERMINAL_NETWORKS = ["solana", "base", "eth", "bsc"]
DEX_MIN_LIQUIDITY_USD = 15_000              # abaixo disto, risco de não conseguires sair da posição
DEX_MIN_VOLUME_24H_USD = 20_000
DEX_MAX_FDV_USD = 20_000_000                # acima disto já não é "micro-cap"
DEX_MIN_POOL_AGE_MINUTES = 20                # evita comprares no bloco de criação do par (rug instantâneo)

# --- Segurança (GoPlus) ---
# Gate eliminatório: um token que falhe isto nunca é alertado, independentemente do score.
GOPLUS_EVM_CHAIN_IDS = {
    "eth": "1",
    "bsc": "56",
    "base": "8453",
    "polygon_pos": "137",
    "arbitrum": "42161",
}

# --- Scoring ---
# Pesos (0-1) por componente, dentro de cada camada
CEX_WEIGHTS = {"chg_1h": 0.30, "chg_24h": 0.30, "turnover": 0.25, "chg_7d": 0.15}
DEX_WEIGHTS = {"chg_1h": 0.35, "chg_6h": 0.20, "vol_liq_ratio": 0.30, "boosted_bonus": 0.15}

MIN_SCORE_TO_ALERT = 55         # 0-100
TOP_N_PER_TIER = 5

# --- Anti-spam / dedup ---
STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "state.json")
ALERT_COOLDOWN_HOURS = 12       # não repete alerta do mesmo token dentro desta janela...
RE_ALERT_MIN_SCORE_INCREASE = 15  # ...a menos que o score suba pelo menos isto (aceleração)
STATE_MAX_AGE_HOURS = 72        # limpa entradas de estado mais antigas que isto

# --- HTTP ---
REQUEST_TIMEOUT = 20
USER_AGENT = "crypto-screener-bot/1.0 (+github actions; personal use)"

# --- Desafio de portfólio virtual (100% simulado, dinheiro real NUNCA é movimentado) ---
PORTFOLIO_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "portfolio_state.json")
PORTFOLIO_ENABLED = os.environ.get("PORTFOLIO_ENABLED", "true").lower() == "true"

# Histórico de "lições" sobre posições fechadas com prejuízo (ver screener/lessons.py) —
# ficheiro separado do estado do portefólio para poder crescer/ser lido independentemente.
LESSONS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lessons.json")

# Histórico de "vitórias" sobre posições fechadas com lucro (ver screener/playbook.py) —
# usado, em conjunto com LESSONS_FILE, para construir um "modus operandi" (o que costuma
# funcionar vs. o que costuma correr mal).
WINS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "wins.json")

# Registo de mudanças autoanalisadas (ver screener/changelog.py) — cada vez que uma análise de
# lições/vitórias leva a um ajuste real (ex: um limiar em config.py), fica aqui registado o
# quê, o porquê e os dados que motivaram, e é anunciado no Telegram automaticamente (ver
# telegram_bot._send_pending_changelog_announcements) para que Ricardo saiba sem ter de
# perguntar. Também é a base para as reanálises periódicas (antes/depois de cada mudança).
CHANGELOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "changelog.json")

STARTING_BALANCE_EUR = 100.0
CHALLENGE_DURATION_DAYS = 10     # a contagem só começa na primeira compra virtual executada

# Disjuntor de capital: numa estratégia sem alavancagem/margem o saldo nunca fica negativo
# (o pior caso é perder o valor investido numa posição), mas nada impedia até agora que
# capital fresco continuasse a ser arriscado em entradas novas durante uma sequência de
# perdas severa. Introduzido 2026-08-30, em resposta direta à pergunta "e se o saldo chegar
# a zero?": se o equity cair para este limiar (fração do saldo inicial), o bot deixa de abrir
# posições NOVAS — mas continua a vigiar e fechar as posições já abertas normalmente
# (take-profit/stop-loss/trailing/liquidação forçada ao fim dos 10 dias). É um disjuntor, não
# um "auto-reset": uma vez acionado (e o Ricardo avisado no Telegram), mantém-se assim até ao
# fim do desafio — não volta a abrir posições sozinho só porque o equity recuperou um pouco.
MAX_DRAWDOWN_HALT_PCT = -0.75   # halt quando equity <= 25% do saldo inicial (25 EUR de 100 EUR)

MAX_CONCURRENT_POSITIONS = 4
POSITION_SIZE_PCT_OF_EQUITY = 0.25   # fallback para camadas sem valor específico abaixo
# Autoanálise 2026-08-30 (13 posições fechadas): o dex_micro_cap fechou 1 vitória em 6
# (-57,85€ líquidos) contra o cex_small_cap com 2 vitórias em 7 (+6,49€ líquidos) — quase
# todo o défice do desafio veio de rugs/colapsos em meme-coins Solana recém-criadas
# (cogefone -99%, TRUMPSTACY -99,5%, Greyson -57%), um risco que nem o stop-loss nem o
# score de entrada conseguiram travar a tempo. Em vez de abandonar a camada (perde-se o
# lado bom: GTAAPE +48%), reduz-se o tamanho de posição para limitar o estrago de cada rug
# individual, mantendo a exposição a explorar o upside.
POSITION_SIZE_PCT_BY_TIER = {"cex_small_cap": 0.25, "dex_micro_cap": 0.12}
MIN_TRADE_EUR = 5.0                  # não abre/fecha posições de valor residual

# Score mínimo para COMPRAR de facto (mais exigente que o limiar de alerta, porque aqui
# está a comprometer-se capital, ainda que virtual)
ENTRY_MIN_SCORE = 65
SCORE_DECAY_EXIT = 30           # se o score cair abaixo disto, a tese de momentum invalidou-se

# Take-profit / stop-loss por camada — DEX é mais volátil, por isso janelas mais largas
TAKE_PROFIT_PCT = {"cex_small_cap": 0.20, "dex_micro_cap": 0.40}
# Autoanálise 2026-08-30: os 5 stop-loss reais em cex_small_cap fecharam sempre bastante
# além do gatilho de -10% (entre -11,8% e -18,4%, ~5 pontos de atraso em média, por causa
# do intervalo de 15 min entre verificações) — desce-se o gatilho para -8% para que a perda
# real fique mais perto da intenção original de ~-10%.
STOP_LOSS_PCT = {"cex_small_cap": -0.08, "dex_micro_cap": -0.20}

# --- Trailing stop após atingir o take-profit ---
# Em vez de vender assim que o valor-alvo é atingido, vigia o preço durante uma janela curta
# para tentar apanhar mais da subida, mas vende ao primeiro sinal real de reversão.
TRAILING_STOP_ENABLED = True
TRAILING_STOP_WINDOW_SECONDS = 300          # vigia durante 5 minutos após atingir o alvo
TRAILING_STOP_CHECK_INTERVAL_SECONDS = 60   # verifica a cada 60s (5 verificações na janela)
TRAILING_STOP_DRAWDOWN_PCT = 0.07           # vende se cair 7% desde o pico visto na vigilância

# --- Monitor leve de posições (position_monitor.py, correndo dentro do bot_listener.yml) ---
# Reavalia posições abertas e verifica saídas com muito mais frequência do que o screener
# principal (que só corre a cada 2h), sem repetir a descoberta cara de tokens novos.
POSITION_MONITOR_ENABLED = os.environ.get("POSITION_MONITOR_ENABLED", "true").lower() == "true"
