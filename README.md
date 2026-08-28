# Crypto Screener — sinais de momentum curto/médio prazo

Bot autónomo e gratuito que vasculha o mercado cripto à procura de moedas mais pequenas
com sinais de momentum de curto/médio prazo, e envia alertas para o Telegram.

**⚠️ Não é aconselhamento financeiro.** É uma ferramenta de ranking baseada em regras
(momentum de preço, volume, liquidez e verificações de segurança on-chain). Cripto,
especialmente micro-caps, tem risco real de perda total. Usa isto como um ponto de
partida para a tua própria pesquisa, nunca como sinal de compra automático.

## Como funciona

Duas camadas de deteção, correndo em paralelo:

1. **🐢 Small-cap estabelecida (CEX)** — moedas já listadas em exchanges, fora do top
   da tabela (via CoinGecko, ranks ~250 a ~3000), filtradas por market cap ($1M–$150M),
   volume mínimo e "turnover" (volume/market cap) — sinal de que há interesse a mais
   do que o normal a acontecer agora.
2. **🚀 Micro-cap agressiva (DEX)** — pools recém-criadas ou em tendência em Solana,
   Base, Ethereum e BSC (via GeckoTerminal) e tokens em destaque no DexScreener.
   Antes de entrar no ranking, cada token passa por uma verificação de segurança na
   GoPlus Security API (honeypot, taxas abusivas, mint/freeze authority ativa, etc.) —
   um token com red flag confirmada é **eliminado**, nunca alertado, seja qual for o
   score de momentum.

Cada candidato recebe um score 0–100 combinando variação de preço a 1h/6h/24h/7d e
volume relativo. Só os que passam um limiar mínimo (`MIN_SCORE_TO_ALERT` em
`screener/config.py`) e que não foram alertados recentemente (anti-spam com
"cooldown" de 12h, salvo aceleração forte do score) geram uma mensagem no Telegram.

## Porquê GitHub Actions e não "correr dentro do Claude"

Testei correr isto diretamente a partir do ambiente cloud do Claude, mas o sandbox
onde o Claude executa código tem acesso de rede bloqueado por lista branca (só
consegue falar com PyPI, npm, GitHub, etc.) — não consegue chegar a APIs cripto nem
à API do Telegram. O GitHub Actions corre em servidores com acesso total à internet,
é **gratuito** (ilimitado em repositórios públicos; ~2000 minutos/mês grátis em
privados, mais do que suficiente para isto), e continua a correr sozinho mesmo que
nunca mais abras esta conversa — por isso é, na prática, a opção mais "independente"
das duas.

## Configuração (uma vez)

### 1. Criar o repositório

Cria um repositório novo no teu GitHub (pode ser privado) e faz upload de todos
estes ficheiros mantendo a estrutura de pastas (a forma mais simples: arrastar a
pasta inteira para a página "Add file → Upload files" do GitHub, ou `git push` se
preferires linha de comandos).

### 2. Criar o bot do Telegram

1. No Telegram, procura por **@BotFather** e envia `/newbot`.
2. Dá um nome e um username ao bot (tem de acabar em `bot`, ex: `ricardo_screener_bot`).
3. O BotFather devolve-te um **token** (algo como `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`) — guarda-o.

### 3. Obter o teu chat_id

1. Envia qualquer mensagem (ex: "olá") ao bot que acabaste de criar.
2. No browser, abre:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (substitui `<TOKEN>` pelo token do passo anterior)
3. Procura `"chat":{"id":` na resposta — esse número é o teu `chat_id`.

### 4. Adicionar os secrets no GitHub

No repositório: **Settings → Secrets and variables → Actions → New repository secret**

- `TELEGRAM_BOT_TOKEN` = o token do passo 2
- `TELEGRAM_CHAT_ID` = o número do passo 3

### 5. Confirmar que corre

Vai ao separador **Actions** do repositório → workflow "Crypto Screener" →
**Run workflow** (botão manual, via `workflow_dispatch`) para testares já, sem
esperar pelo próximo horário agendado. Depois disso corre sozinho a cada 2h
(configurável no cron do ficheiro `.github/workflows/screener.yml`).

## Desafio: portfólio virtual de 10 dias (100% simulado)

Além dos alertas, o mesmo workflow gere um **portfólio virtual**: parte de um saldo
simulado de €100 e usa **dados de mercado reais** para simular compras e vendas segundo
regras fixas de gestão de risco. Nenhum dinheiro real é movimentado — não há chaves de
exchange nem carteira, apenas um livro-razão (`data/portfolio_state.json`) que regista
preços reais.

**Regras de gestão** (ajustáveis em `screener/config.py`):

- Até 4 posições simultâneas, cada uma usando ~25% do capital disponível nesse momento.
- Só compra candidatos com score ≥ 65 (mais exigente que o limiar de alerta, 55).
- Take-profit: +20% (small-cap CEX) / +40% (micro-cap DEX).
- Stop-loss: −10% (CEX) / −20% (DEX).
- Sai também se o score do token cair abaixo de 30 (a tese de momentum invalidou-se),
  ou se o token deixar de ter dados de mercado em corridas sucessivas (assume-se perda
  quase total — cenário de rug).
- **A contagem dos 10 dias começa na primeira compra virtual executada**, não na data
  de ativação do workflow — se o mercado não gerar nenhum sinal com score ≥ 65 logo de
  início, o relógio só arranca quando isso acontecer. Ao fim de 10 dias, todas as
  posições abertas são liquidadas automaticamente aos preços de mercado desse momento e
  é enviado um relatório final para o Telegram com o saldo, o resultado e o histórico
  de trades.

Enquanto o desafio decorre, cada corrida do workflow envia uma atualização de portfólio
para o Telegram (equity atual, posições abertas, compras/vendas dessa corrida). Para
veres o estado em qualquer momento sem esperar pela próxima corrida, corre o workflow
manualmente (`workflow_dispatch`) — a atualização é enviada de imediato.

## Ajustar o comportamento

Tudo o que é limiar/parâmetro está em `screener/config.py`, comentado em português:
market cap mínimo/máximo, liquidez mínima, score mínimo para alertar, frequência de
"cooldown" dos alertas, redes DEX cobertas, pesos do score, etc. Não devias precisar
de tocar em mais nenhum ficheiro para afinar isto.

Para mudar a frequência das corridas, edita a linha `cron:` em
`.github/workflows/screener.yml` (formato cron padrão, em UTC).

## Testar localmente sem tocar na internet real

```
python -m tests.test_pipeline_offline
python -m tests.test_portfolio_offline
```

O primeiro corre a pipeline de deteção (scoring, filtros de segurança, anti-spam,
formatação da mensagem); o segundo corre o motor do portfólio virtual (compra,
take-profit, stop-loss, decaimento de score, liquidação forçada ao fim de 10 dias) —
ambos com dados falsos que imitam a forma real das APIs, úteis para validar que uma
alteração ao `config.py` não partiu nada antes de a levares para produção.

## Limitações conhecidas (sê honesto contigo sobre isto)

- As APIs gratuitas (CoinGecko, GeckoTerminal, DexScreener, GoPlus) têm limites de
  taxa. Se uma corrida falhar ocasionalmente por 429/timeout, o script já tem
  retry/backoff, mas em dias de tráfego alto pode perder uma corrida — não é crítico
  dado o cron correr de 2 em 2 horas.
- A verificação de segurança (GoPlus) apanha honeypots e red flags óbvias, **não**
  garante que um token é seguro — rugs mais sofisticados (ex: liquidez retirada
  manualmente pela equipa sem isso estar no contrato) não são detetados.
- "Boosted" no DexScreener significa que o projeto **pagou** para aparecer em
  destaque — é tratado como sinal secundário fraco, nunca como confirmação de
  qualidade.
- Isto não tem em conta narrativa, redes sociais, ou atividade de whales — é
  puramente técnico (preço/volume/liquidez/segurança on-chain).
- O portfólio virtual é uma **simulação de gestão de risco com dados reais**, não um
  backtest científico: não modela slippage, spread, taxas de exchange/gas, nem o facto
  de que comprar €25 de um micro-cap ilíquido na vida real pode mover o próprio preço.
  O resultado ao fim de 10 dias diz-te como a *estratégia* se saiu com o mercado real,
  não é uma previsão do que ganharias a negociar isto de verdade.
- Repreçar uma posição depende de o token ainda aparecer nas APIs gratuitas usadas; se
  desaparecer (ex: pool DEX removida), o sistema assume perda quase total ao fim de 3
  corridas sem dados — é uma aproximação conservadora, não uma deteção real de rug.
