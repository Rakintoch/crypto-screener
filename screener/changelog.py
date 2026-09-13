"""
Registo de mudanças autoanalisadas: cada vez que uma revisão das lições/vitórias (ver
lessons.py / playbook.py) leva a um ajuste real no comportamento do bot (um limiar em
config.py, uma regra em portfolio.py/scoring.py, etc.), fica aqui registada uma entrada com
o que mudou, os dados que motivaram a mudança, e o efeito esperado.

Duas utilizações:
1. Anúncio automático no Telegram — cada entrada nova (announced=False) é enviada como
   mensagem na próxima corrida do bot_listener.yml (ver telegram_bot._send_pending_
   changelog_announcements), para que não seja preciso perguntar "há melhorias para
   implementar?" — são anunciadas assim que implementadas.
2. Reanálise periódica — uma revisão futura pode comparar as operações fechadas antes/depois
   do "closed_ts_at_change" de cada entrada, para avaliar se a mudança teve o efeito esperado.
"""
import json
import os
import time

from . import config


def _load():
    if not os.path.exists(config.CHANGELOG_FILE):
        return []
    try:
        with open(config.CHANGELOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _save(entries):
    os.makedirs(os.path.dirname(config.CHANGELOG_FILE), exist_ok=True)
    with open(config.CHANGELOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def record_change(titulo, motivo, mudanca, efeito_esperado, num_trades_fechados_ate_aqui=None):
    """Regista uma mudança de estratégia/config decidida a partir de uma autoanálise.

    titulo: resumo curto (ex: "Tamanho de posição menor em dex_micro_cap")
    motivo: os dados/observação que motivaram a mudança
    mudanca: o que mudou de facto (ficheiro/parâmetro, valor antigo -> novo)
    efeito_esperado: o que se espera que melhore, para poder confirmar depois
    num_trades_fechados_ate_aqui: nº de posições já fechadas no momento da mudança — serve de
        marca para separar "antes" de "depois" numa reanálise futura
    """
    entry = {
        "ts": time.time(),
        "titulo": titulo,
        "motivo": motivo,
        "mudanca": mudanca,
        "efeito_esperado": efeito_esperado,
        "num_trades_fechados_ate_aqui": num_trades_fechados_ate_aqui,
        "announced": False,
    }
    entries = _load()
    entries.append(entry)
    _save(entries)
    return entry


def pending_announcements():
    return [e for e in _load() if not e.get("announced")]


def mark_announced(timestamps):
    entries = _load()
    ts_set = set(timestamps)
    for e in entries:
        if e.get("ts") in ts_set:
            e["announced"] = True
    _save(entries)


def format_announcement(entry):
    return (
        f"🛠️ *Improvement implemented:* {entry['titulo']}\n\n"
        f"*Why:* {entry['motivo']}\n\n"
        f"*What changed:* {entry['mudanca']}\n\n"
        f"*Expected effect:* {entry['efeito_esperado']}"
    )


def format_recent_changes(limit=5):
    """Mensagem Telegram com o histórico de mudanças mais recentes (comando /mudancas)."""
    entries = _load()
    if not entries:
        return "🛠️ No strategy changes recorded yet."

    recent = entries[-limit:][::-1]
    lines = [f"🛠️ *Change history* ({len(entries)} total, last {len(recent)}):\n"]
    for e in recent:
        lines.append(f"• *{e['titulo']}* — {e['mudanca']}")
    return "\n".join(lines)
