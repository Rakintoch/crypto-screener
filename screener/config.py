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
# Autoanálise 2026-09-12: EMBER, CME e UP (candidatos DEX trazidos pelo Ricardo) tinham em
# comum uma "base" relativamente calma antes da rutura de preço — diferente de um pump que
# É o próprio nascimento do token (ex. BNC4, sem qualquer base antes de rebentar e morrer
# em seguida). O scoring atual não distinguia os dois casos: tratava chg_1h/chg_6h da mesma
# forma quer já houvesse uma base estável antes, quer não. "base_breakout" dá um bónus a
# candidatos com uma base calma comprovada seguida de uma rutura real, e nenhum bónus a
# quem rebenta logo à nascença (ver scoring.detect_base_breakout). Isto NÃO separa vencedores
# de perdedores dentro da categoria "rutura genuína" (isso continua a cargo do trailing
# stop/stop-loss na saída) — só amplia a deteção desta categoria, hoje ignorada.
DEX_WEIGHTS = {"chg_1h": 0.25, "chg_6h": 0.15, "vol_liq_ratio": 0.25, "boosted_bonus": 0.10, "base_breakout": 0.25}

# --- Sinal de "base estável seguida de rutura" (dex_micro_cap) ---
# Usa dados já recolhidos (chg_6h, chg_24h) — sem chamadas extra à API.
DEX_BREAKOUT_MIN_POOL_AGE_HOURS = 24        # sem isto, o chg_24h nem reflete histórico real
DEX_BREAKOUT_MAX_PRE_WINDOW_MOVE_PCT = 25   # variação implícita nas ~18h antes da rutura tem de ser < 25%
DEX_BREAKOUT_MIN_RECENT_MOVE_PCT = 20       # chg_6h mínimo para contar como rutura real, não ruído

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

# Autoanálise 2026-09-21 (pedido direto do Ricardo): saldo inicial subido de 100 para 1000
# EUR ao arrancar a challenge #2 — a challenge #1 (10-20 set) só teve 73 trades fechados em
# 10 dias; um saldo maior não muda a % ganha/perdida por trade (POSITION_SIZE_PCT_BY_TIER é
# percentual), mas dá posições maiores em termos absolutos, o que é irrelevante para validar
# a estratégia (100% simulado) mas foi um pedido explícito, não uma decisão de estratégia.
STARTING_BALANCE_EUR = 1000.0
CHALLENGE_DURATION_DAYS = 10     # a contagem só começa na primeira compra virtual executada

# Pedido do Ricardo 2026-09-24: o saldo inicial da challenge é de $1000 (DÓLARES), não 1000 EUR.
# A contabilidade interna continua em EUR (ver fx.py), por isso isto é convertido UMA vez, à taxa
# USD->EUR da corrida em que é aplicado, e fica fixo no estado (portfolio._migrate_state). Um
# estado já em curso é reescalado proporcionalmente (saldo, posições abertas e trades fechados),
# o que mantém TODAS as percentagens de ganho/perda intactas — não é um reset, as trades e as
# posições abertas continuam as mesmas. None desativa a conversão (usa STARTING_BALANCE_EUR).
STARTING_BALANCE_USD = 1000.0

# Ciclos contínuos (pedido do Ricardo 2026-09-24): o fim de cada ciclo de
# CHALLENGE_DURATION_DAYS deixa de liquidar tudo e reiniciar o saldo. Passa a ser só um PONTO
# DE REVISÃO: o bot arquiva o resumo do ciclo em data/challenge_history.json, anuncia-o no
# Telegram e começa logo o ciclo seguinte com as MESMAS posições abertas e o MESMO saldo. O
# conhecimento acumulado (lessons.json/wins.json/challenge_history.json) é o que a revisão
# diária automática usa para atualizar o modus operandi entre ciclos.
# False repõe o comportamento antigo (liquidação forçada + "finished").
CONTINUOUS_CYCLES = True
CHALLENGE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "challenge_history.json")
# Trades fechados há menos do que isto ficam também no portfolio_state.json depois do fecho
# do ciclo (o resto passa só para o challenge_history.json) — _check_entries precisa das
# saídas das últimas 6h para não voltar a comprar logo a mesma moeda.
CYCLE_KEEP_RECENT_TRADES_HOURS = 24

# Disjuntor de capital: numa estratégia sem alavancagem/margem o saldo nunca fica negativo
# (o pior caso é perder o valor investido numa posição), mas nada impedia até agora que
# capital fresco continuasse a ser arriscado em entradas novas durante uma sequência de
# perdas severa. Introduzido 2026-08-30, em resposta direta à pergunta "e se o saldo chegar
# a zero?": se o equity cair para este limiar (fração do saldo inicial), o bot deixa de abrir
# posições NOVAS — mas continua a vigiar e fechar as posições já abertas normalmente
# (take-profit/stop-loss/trailing/liquidação forçada ao fim dos 10 dias). É um disjuntor, não
# um "auto-reset": uma vez acionado (e o Ricardo avisado no Telegram), mantém-se assim até ao
# fim do desafio — não volta a abrir posições sozinho só porque o equity recuperou um pouco.
MAX_DRAWDOWN_HALT_PCT = -0.75   # halt quando equity <= 25% do saldo inicial (250 EUR de 1000 EUR)

MAX_CONCURRENT_POSITIONS = 4
POSITION_SIZE_PCT_OF_EQUITY = 0.25   # fallback para camadas sem valor específico abaixo
# Autoanálise 2026-08-30 (13 posições fechadas): o dex_micro_cap fechou 1 vitória em 6
# (-57,85€ líquidos) contra o cex_small_cap com 2 vitórias em 7 (+6,49€ líquidos) — quase
# todo o défice do desafio veio de rugs/colapsos em meme-coins Solana recém-criadas
# (cogefone -99%, TRUMPSTACY -99,5%, Greyson -57%), um risco que nem o stop-loss nem o
# score de entrada conseguiram travar a tempo. Em vez de abandonar a camada (perde-se o
# lado bom: GTAAPE +48%), reduz-se o tamanho de posição para limitar o estrago de cada rug
# individual, mantendo a exposição a explorar o upside.
#
# Autoanálise 2026-09-18 (54 trades fechados, 37 stop-loss reais em cex_small_cap desde o
# STOP_LOSS_PCT de -8% introduzido em 2026-08-30): a perda REAL média nesses 37 fechos foi
# -15,32% — quase o dobro do gatilho de -8% — e não por dados em falta (missed_updates=0 em
# TODOS os 37 casos, ou seja, o preço estava a ser lido em toda corrida). 12 dos 37 (32%)
# fecharam pior que -15%, incluindo um colapso de -80,08% (AIN) e vários entre -20% e -29%
# (OTC -29,3%, LOOM -27,6%, PERPSPAD -26,3%, POWER -23,5%, CVC -20,6%) — mesmo excluindo o
# outlier da AIN, a média fica em -13,52%, ainda ~70% pior que o gatilho. Não há correlação
# forte com liquidez/volume de entrada (ex: CVC e REZ tinham >150M$ e 190M$ de volume 24h e
# mesmo assim ultrapassaram o gatilho em >10 pontos) nem com o tempo de posse (correlação
# 0,07 entre horas até ao fecho e pnl_pct; ANON perdeu -17% em 24 min, mas BFC só -8,3% em
# 38h) — indica que micro/small-caps CEX pouco negociadas dão saltos de preço discretos
# maiores do que os ~15 min entre verificações do position_monitor conseguem apanhar perto
# do gatilho, independentemente de quão apertado o gatilho está (já tínhamos apertado de
# -10% para -8% em 2026-08-30 e a média real não melhorou). Como isto não é resolúvel
# apertando mais o gatilho nem com filtros de entrada óbvios, aplica-se aqui a MESMA lógica
# já usada para o dex_micro_cap acima: reduz-se o tamanho de posição do cex_small_cap na
# mesma proporção do excesso de perda medido (0,08 / 0,1352 ≈ 0,59 -> 25% * 0,59 ≈ 15%) para
# manter o risco em euros por posição alinhado com o que uma perda de -8% pressupunha,
# dado que a perda real observada não é isso. Isto não muda o retorno esperado por posição
# (ganhos e perdas escalam com o mesmo fator), mas reduz o risco de cauda — a AIN sozinha, a
# 25% de tamanho, custou ~20% do equity total numa única posição, perigosamente perto do
# disjuntor de -75% (MAX_DRAWDOWN_HALT_PCT).
POSITION_SIZE_PCT_BY_TIER = {"cex_small_cap": 0.15, "dex_micro_cap": 0.12}
MIN_TRADE_EUR = 5.0                  # não abre/fecha posições de valor residual

# Score mínimo para COMPRAR de facto (mais exigente que o limiar de alerta, porque aqui
# está a comprometer-se capital, ainda que virtual)
ENTRY_MIN_SCORE = 65
SCORE_DECAY_EXIT = 30           # se o score cair abaixo disto, a tese de momentum invalidou-se

# Autoanálise 2026-09-10 (fim do 2º desafio, 79 trades fechados): a correlação entre
# entry_score e pnl_pct foi -0,019 (praticamente zero) — o score não prevê o resultado.
# O score é uma soma de sigmoides sobre variação de curto prazo (chg_1h/chg_24h/chg_6h/
# turnover), que satura rapidamente: 46 dos 79 trades (58%) entraram já com score 90-100,
# e foi precisamente esse escalão o pior em €  (-36,22€ líquidos, o maior défice de
# qualquer escalão) — a média de entry_score das perdas (90,4) foi MAIOR que a das vitórias
# (82,0). Ou seja: dentro do que já passa nos filtros, o candidato "mais extremo" (que já
# subiu mais, mais depressa) não é melhor escolha — tende a já estar esticado/perto do
# topo do movimento (ex.: PERPSPAD comprado 2x com score ~99, caiu as duas vezes). Isto NÃO
# prova que sinal específico é o culpado (ainda não guardamos os componentes brutos por
# trade — só o score final), mas justifica deixar de tratar o topo do intervalo (90-100)
# como "melhor" para efeitos de escolha entre candidatos elegíveis em simultâneo: acima
# deste teto, o score deixa de ser tratado como diferenciador (ver portfolio._check_entries).
# Não mexe no stop-loss nem no take-profit — o problema medido está na seleção, não na saída.
SELECTION_SCORE_CEILING = 90

# --- Rotação de posições fracas (pedido do Ricardo 2026-09-16) ---
# Antes desta mudança, com as MAX_CONCURRENT_POSITIONS vagas todas ocupadas, _check_entries
# devolvia-se de imediato (ver "if slots_free <= 0: return actions") — um candidato novo já
# qualificado (score >= ENTRY_MIN_SCORE) era sempre ignorado até uma posição existente fechar
# sozinha, por melhor que esse candidato fosse. Ricardo notou isto diretamente: sinais fortes
# no alerta de descoberta "nunca entram no Virtual Portfolio Challenge".
#
# Esta rotação NÃO compara o score do candidato novo com o score da posição fraca — a
# autoanálise de 2026-09-10 já mostrou que o score bruto não prevê retorno (correlação -0,019
# em 79 trades fechados, e o escalão 90-100 foi o PIOR em € líquidos). Um candidato novo com
# score maior não é, por si só, motivo válido para vender uma posição já aberta. Em vez disso,
# reaplica ANTECIPADAMENTE o mesmo sinal de decadência já confiado pelo bot (SCORE_DECAY_EXIT):
# uma posição só é candidata a rotação se já estiver a decair para perto desse limiar E sem
# ganho não realizado nenhum — ou seja, teria uma probabilidade razoável de acabar em
# score-decay-exit de qualquer forma, só que mais tarde. A rotação só antecipa esse desfecho
# para não desperdiçar, entretanto, um candidato novo já qualificado.
ROTATION_ENABLED = True
ROTATION_SCORE_WATCH = 45         # entre o neutro e o SCORE_DECAY_EXIT (30) — "a enfraquecer" mas ainda não invalidado
ROTATION_MIN_HOLD_HOURS = 4       # não roda posições recém-abertas, ainda sem tempo de desenvolver a tese
ROTATION_MIN_INTERVAL_HOURS = 12  # cooldown global — no máximo 1 rotação a cada 12h, para não gerar churn

# Autoanálise 2026-09-23 (10 trades fechados no arranque da challenge #2, 2 dias): 7 perdas,
# 3 ganhos (30% win rate). As duas piores perdas (ZRC -22,7%, SURGE -27,0%) tinham entry_chg_1h
# extremos (209,7% e 28,0%) — candidatos já esticados ao limite na última hora antes da compra.
# Alargando a análise aos 78 trades cex_small_cap com sinais brutos guardados desde 2026-09-10:
# os 5 com entry_chg_1h >= 45% (HODL 203,3%, ANON 49,6%, PERPSPAD 50,7%, ZFORGE 51,2%, ZRC
# 209,7%) tiveram 0% de win rate e -19,43% de pnl médio, claramente pior que os restantes 73
# (27% win rate, -0,25% médio). O único trade com chg_1h alto mas logo abaixo do corte (TRUE,
# 43,2%) foi o único ganho nessa vizinhança (+4,5%) — o corte de 45% separa-os sem sacrificar
# esse caso. Faz sentido mecanicamente: um candidato que já subiu >45% SÓ na última hora está
# mais perto da exaustão/reversão do que do início de um movimento sustentável, ao contrário de
# chg_24h/chg_7d altos (que podem refletir uma subida mais gradual e ainda saudável). Isto é uma
# NOVA porta de entrada (elimina candidatos, não só desempata como o SELECTION_SCORE_CEILING) —
# só afeta a compra automática no desafio (portfolio._check_entries), não os alertas do Telegram
# (Ricardo pode querer ver o candidato para investigação manual mesmo que o bot não lhe arrisque
# capital). Amostra pequena (5 casos no corte) — a vigiar em revisões futuras.
CEX_MAX_ENTRY_CHG_1H_PCT = 45.0

# Take-profit / stop-loss por camada — DEX é mais volátil, por isso janelas mais largas
TAKE_PROFIT_PCT = {"cex_small_cap": 0.20, "dex_micro_cap": 0.40}
# Autoanálise 2026-08-30: os 5 stop-loss reais em cex_small_cap fecharam sempre bastante
# além do gatilho de -10% (entre -11,8% e -18,4%, ~5 pontos de atraso em média, por causa
# do intervalo de 15 min entre verificações) — desce-se o gatilho para -8% para que a perda
# real fique mais perto da intenção original de ~-10%.
STOP_LOSS_PCT = {"cex_small_cap": -0.08, "dex_micro_cap": -0.20}

# --- Trailing stop após atingir o take-profit ---
# Em vez de vender assim que o valor-alvo é atingido, continua a vigiar o preço para tentar
# apanhar mais da subida, mas vende ao primeiro sinal real de reversão.
#
# Autoanálise 2026-09-12: o desenho original vigiava só TRAILING_STOP_WINDOW_SECONDS (5 min)
# bloqueantes logo a seguir ao primeiro toque no take-profit, e fechava a posição incondicio-
# nalmente no fim dessa janela — mesmo que o preço continuasse a subir com força. Numa corrida
# sustentada (ex.: um token que continua a subir horas a fio depois do alvo, +2000% num dia),
# isto venderia quase no início do movimento, perdendo a maior parte do lucro potencial. Agora
# o pico é persistido na própria posição (`trailing_active`/`trailing_peak_eur`) e reavaliado
# em CADA corrida (screener principal a cada 2h, monitor leve a cada poucos minutos via
# bot_listener.yml), sem prazo fixo — só fecha quando há um recuo real desde o pico mais alto
# já visto. Isto também elimina o time.sleep() bloqueante que existia dentro do próprio ciclo
# do screener (até 5 min por posição a atingir o alvo na mesma corrida).
TRAILING_STOP_ENABLED = True
TRAILING_STOP_DRAWDOWN_PCT = 0.07           # vende se cair 7% desde o pico mais alto já visto

# --- Monitor leve de posições (position_monitor.py, correndo dentro do bot_listener.yml) ---
# Reavalia posições abertas e verifica saídas com muito mais frequência do que o screener
# principal (que só corre a cada 2h), sem repetir a descoberta cara de tokens novos.
POSITION_MONITOR_ENABLED = os.environ.get("POSITION_MONITOR_ENABLED", "true").lower() == "true"

# --- Pump Watch: sinal de acumulação/distribuição (CEX small-cap), independente do desafio ---
# Pedido do Ricardo 2026-09-14: um sistema à parte, com o seu próprio saldo virtual, para testar
# um sinal DIFERENTE do momentum que o desafio principal usa — não "o que já está a subir", mas
# "onde há volume de compra a acumular-se sem o preço ainda ter reagido" (o padrão clássico
# pré-rutura, aproximado aqui por OBV — On-Balance Volume — normalizado sobre o histórico
# horário do CoinGecko, já que dados reais ao nível de carteira não são possíveis de obter de
# graça com a profundidade necessária). Mantido totalmente separado do desafio principal (saldo,
# posições, ficheiro de estado próprios) para que o ciclo de autoanálise consiga avaliar este
# sinal isoladamente, sem contaminar (ou ser contaminado por) o resultado do desafio de momentum.
# Só cobre cex_small_cap por agora — um pool dex_micro_cap recém-criado no pump.fun raramente
# tem histórico suficiente para o sinal de OBV ter qualquer significado.
PUMP_WATCH_ENABLED = os.environ.get("PUMP_WATCH_ENABLED", "true").lower() == "true"
PUMP_WATCH_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pump_watch_state.json")
PUMP_WATCH_STARTING_BALANCE_EUR = 100.0   # equivalente a $100 virtuais, dedicados só a este sistema
PUMP_WATCH_MAX_POSITIONS = 3
PUMP_WATCH_POSITION_SIZE_PCT = 1 / 3      # tamanho-alvo por posição, sobre o capital negociável (exclui a reserva)
PUMP_WATCH_MIN_TRADE_EUR = 5.0

# Saída: SEMPRE por trailing stop desde a entrada (pedido do Ricardo) — sem take-profit fixo.
# O "pico" arranca no próprio preço de entrada, por isso uma posição que cai logo após a
# entrada, sem nunca subir, comporta-se como um stop-loss simples de -10% — só fica mais
# protetora à medida que o preço sobe (inclui naturalmente o ajuste a alguma oscilação normal).
PUMP_WATCH_TRAILING_DRAWDOWN_PCT = 0.10

# Gestão de lucro (pedido do Ricardo): quando uma posição fecha com lucro, 20% desse lucro fica
# reservado (nunca mais é reinvestido) e só os 80% restantes, mais o capital investido, voltam
# ao saldo negociável. Perdas não sofrem este corte — o valor todo da venda volta ao saldo
# negociável, para não acelerar a erosão do capital numa sequência de perdas.
PUMP_WATCH_PROFIT_RESERVE_PCT = 0.20

# Universo de candidatos: reutiliza os candidatos CEX já descobertos pelo screener principal
# nesta mesma corrida (sem chamadas extra à API) — mas só vale a pena calcular o sinal de
# acumulação (que exige uma chamada de histórico por candidato) quando há pelo menos um slot
# livre, e só para um shortlist pequeno (pré-filtrado por um critério já disponível sem custo
# extra), para respeitar o limite de chamadas partilhado da API pública do CoinGecko.
PUMP_WATCH_SHORTLIST_SIZE = 10
PUMP_WATCH_LOOKBACK_DAYS = 3        # dentro da janela 2-90 dias em que o CoinGecko dá granularidade horária

# Limiares do sinal de acumulação (OBV normalizado + banda de preço "ainda calma") — hipótese
# inicial (2026-09-14), por validar/ajustar pelo ciclo de autoanálise com base nos primeiros
# fechos reais, tal como todos os outros limiares deste projeto.
PUMP_WATCH_MIN_OBV_SCORE = 0.15             # pressão compradora líquida mínima (fração do volume total na janela)
PUMP_WATCH_MAX_PRICE_MOVE_PCT = 0.15        # acima disto já não é "acumulação silenciosa" — é o próprio momentum tier
PUMP_WATCH_MIN_PRICE_MOVE_PCT = -0.08       # abaixo disto pode ser capitulação, não acumulação

# Sem prazo fixo que force o encerramento (ao contrário do desafio principal, de 10 dias) —
# pedido do Ricardo 2026-09-14 foi deixar isto como um teste de sinal contínuo, não uma corrida
# com fim. Mas para não ficar sem nenhum ponto de avaliação formal, este é o marco que uma
# futura revisão periódica deve usar para fazer a PRIMEIRA análise séria deste sistema
# (correlação sinal->resultado, comparar com o desafio de momentum, decidir se ajusta limiares,
# expande ao tier DEX, ou descontinua) — o que vier primeiro:
PUMP_WATCH_REVIEW_AFTER_TRADES = 10
PUMP_WATCH_REVIEW_AFTER_DAYS = 20
# --- Pump Watch v2: "despertar do volume" (autoanálise 2026-09-25, caso LSK) ---
# Replay do sinal sobre os dados horários reais da LSK (9-13 set): o sinal OBV disparou a 9/09
# 18h UTC a ~$0,115 — ~2,8 dias antes da compra do desafio de momentum ($0,22) e ~3,3 dias antes
# do pico ($1,15). Mas o Pump Watch provavelmente NÃO a teria comprado, por dois motivos:
# (1) o shortlist era o top-10 por turnover ABSOLUTO do universo inteiro — a LSK tinha turnover
#     0,14-0,50, muito abaixo de moedas já a subir com turnover 2-5, que ocupam o top-10 mas
#     estão fora da banda de "acumulação silenciosa" (e depois são descartadas). Agora o
#     shortlist só considera moedas ainda CALMAS (chg_24h/chg_7d abaixo dos limites abaixo),
#     e só dentro dessas ordena por turnover — é aí que o volume a acordar se destaca.
# (2) os "volumes" do market_chart do CoinGecko são o volume ACUMULADO de 24h em cada hora, não
#     o volume da hora — o OBV pesava cada hora pelo volume do dia inteiro. Acrescenta-se um
#     segundo critério que usa estes dados como eles são: o "salto de volume" = média das
#     últimas 6 leituras de volume-24h / mediana das primeiras 24 da janela (3 dias). Na LSK:
#     ~3x no momento do primeiro sinal, ~12x um dia depois; numa moeda simplesmente parada ~1x.
PUMP_WATCH_CALM_MAX_CHG_24H_PCT = 15.0
PUMP_WATCH_CALM_MAX_CHG_7D_PCT = 20.0
PUMP_WATCH_MIN_VOLUME_SURGE = 2.0
# Bónus de ordenação (não de elegibilidade) para moedas com catalisador fundamental recente —
# ver catalysts.py. Pequeno de propósito até haver dados que provem o efeito.
PUMP_WATCH_CATALYST_BONUS = 0.10

# --- Catalisadores fundamentais (notícias) — pedido do Ricardo 2026-09-25 ---
# O bot só via preço/volume (CoinGecko/GeckoTerminal/DexScreener) e segurança (GoPlus); nenhuma
# fonte de notícias/eventos, por isso anúncios como o da Lisk (fim da chain + queima de 25% da
# oferta a 25/08, migração obrigatória a 10/09) nunca entravam na decisão. Fonte gratuita, sem
# key: RSS de pesquisa do Google News (ver sources_news.py). Só é chamado para o shortlist do
# Pump Watch e para cada compra real do desafio (poucas chamadas por corrida).
CATALYST_ENABLED = os.environ.get("CATALYST_ENABLED", "true").lower() == "true"
CATALYST_LOOKBACK_DAYS = 30
CATALYST_TAGS = {
    "supply_cut": ["burn", "burns", "burning", "buyback", "buy back", "buybacks", "supply cut",
                   "supply reduction", "halving", "deflationary"],
    "restructuring": ["shut down", "shutting down", "shuts down", "sunset", "pivot", "pivots",
                      "rebrand", "rebrands", "token swap", "migration", "migrate", "migrates", "bridge"],
    "listing": ["listing", "lists", "listed on", "to list", "perpetual", "perpetuals", "futures",
                "adds support"],
    "upgrade": ["mainnet", "upgrade", "hard fork", "launches", "launch"],
    "partnership": ["partner", "partners", "partnership", "integration", "integrates"],
    "exchange_risk": ["delisting risk", "monitoring tag", "delist", "delisting", "delisted"],
    "unlock": ["token unlock", "unlock", "unlocks"],
    "security_incident": ["hack", "hacked", "exploit", "exploited", "drained", "rug pull",
                          "insolvency", "bankrupt", "bankruptcy"],
}
CATALYST_TAG_WEIGHTS = {"supply_cut": 0.5, "restructuring": 0.4, "listing": 0.4, "upgrade": 0.2,
                        "partnership": 0.1}
CATALYST_BLOCKING_TAGS = ["security_incident"]

# Pedido do Ricardo 2026-09-24: o marco acima repete-se em ciclos contínuos. Quando um ciclo
# de revisão fecha (10 trades fechados OU 20 dias, o que vier primeiro), o bot guarda o resumo
# em pump_watch_state["review_history"], anuncia-o no Telegram e abre logo o ciclo seguinte,
# SEM reiniciar saldo nem posições. A revisão diária automática usa esse resumo para decidir
# se os limiares do Pump Watch devem mudar (só com justificação numérica).
