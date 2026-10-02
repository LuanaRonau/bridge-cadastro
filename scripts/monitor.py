#!/usr/bin/env python3
"""
Monitor de milestones, com exclusão de issues presentes no board "Ladybug"
(GitHub Projects v2) da contagem de restantes.

Versão adaptada para testes com conta de USUÁRIO (owner_type: user)

Este arquivo apenas orquestra. A lógica fica em:
  - common.py            : sessão GitHub, GraphQL, Ladybug, helpers compartilhados
  - closed_issues.py     : alerta de issues fechadas (últimas N restantes)
  - milestone_events.py  : alerta de issues adicionadas/removidas de milestones
                           (ativado por `notify_milestone_changes: true`)

Fluxo:
  1. Carrega config.yaml e o state file (última execução + itens já notificados).
  2. Busca os itens do Project "Ladybug" (GraphQL) para montar o set de
     (owner, repo, number) que devem ser excluídos da contagem.
  3. Executa o alerta de issues fechadas.
  4. Se habilitado, executa o alerta de entrada/saída de milestones.
  5. Salva o state.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import yaml

from closed_issues import process_closed_issues
from common import get_ladybug_issue_set, gh_session
from milestone_events import process_milestone_events

CONFIG_PATH = "config.yaml"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(state_file):
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_checked": None, "notified_issues": []}


def save_state(state_file, state):
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    # Limita o histórico de notificados para não crescer pra sempre
    state["notified_issues"] = state["notified_issues"][-2000:]
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def main():
    config = load_config()
    owner = config.get("owner") or config.get("organization")
    owner_type = config.get("owner_type", "organization" if "organization" in config else "user")
    if owner_type not in ("user", "organization"):
        print(f"Erro: owner_type inválido ('{owner_type}'). Use 'user' ou 'organization'.")
        sys.exit(1)

    repos = config["repositories"]
    ladybug_title = config["ladybug_project_title"]
    last_n = config.get("last_n_remaining", 5)
    state_file = config.get("state_file", "state/monitor_state.json")
    lookback_days = config.get("lookback_days", 2)
    notify_milestone_changes = config.get("notify_milestone_changes", False)

    # Mapeamento de nome de exibição + emoji custom do Slack por repositorio
    repo_display_map = config.get("project_display", {})
    default_emoji = config.get("default_emoji", "iphone")

    token = os.environ.get("GH_MONITOR_TOKEN")
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not token or not slack_webhook:
        print("Erro: defina GH_MONITOR_TOKEN e SLACK_WEBHOOK_URL como variáveis de ambiente.")
        sys.exit(1)

    session = gh_session(token)
    state = load_state(state_file)

    now = datetime.now(timezone.utc)
    first_run = not state["last_checked"]
    if first_run:
        since_dt = now - timedelta(days=lookback_days)
    else:
        since_dt = datetime.fromisoformat(state["last_checked"])

    notified_set = set(state["notified_issues"])

    print(f"Owner: {owner} (tipo: {owner_type})")
    print(f"Buscando alterações desde {since_dt.isoformat()}...")
    ladybug_set = get_ladybug_issue_set(session, owner, owner_type, ladybug_title)
    print(f"Board Ladybug: {len(ladybug_set)} issue(s) mapeada(s).")

    # Funcionalidade 1: issues fechadas
    process_closed_issues(
        session=session,
        owner=owner,
        repos=repos,
        since_dt=since_dt,
        first_run=first_run,
        notified_set=notified_set,
        ladybug_set=ladybug_set,
        slack_webhook=slack_webhook,
        last_n=last_n,
        repo_display_map=repo_display_map,
        default_emoji=default_emoji,
    )

    # Funcionalidade 2: issues adicionadas/removidas de milestones
    if notify_milestone_changes:
        process_milestone_events(
            session=session,
            owner=owner,
            repos=repos,
            since_dt=since_dt,
            notified_set=notified_set,
            ladybug_set=ladybug_set,
            slack_webhook=slack_webhook,
            repo_display_map=repo_display_map,
            default_emoji=default_emoji,
        )

    state["last_checked"] = now.isoformat()
    state["notified_issues"] = list(notified_set)
    save_state(state_file, state)
    print("Execução concluída.")


if __name__ == "__main__":
    main()
