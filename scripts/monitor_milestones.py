#!/usr/bin/env python3
"""
Monitor de fechamento de issues em milestones, com exclusão de issues
presentes no board "Ladybug" (GitHub Projects v2) da contagem de restantes.

Versão adaptada para testes com conta de USUÁRIO (owner_type: user)

Fluxo:
  1. Carrega config.yaml e o state file (última execução + issues já notificadas).
  2. Busca (Search API) issues fechadas desde a última execução, em todos os
     repositórios configurados do owner (usuário ou organização).
  3. Busca todos os itens do Project "Ladybug" (GraphQL) para montar um set
     de (owner, repo, number) que devem ser excluídos da contagem.
  4. Agrupa as issues fechadas por milestone e calcula, para cada uma, quantas
     issues (não-Ladybug) restavam no milestone no momento exato do fechamento
     dela (importante quando várias issues fecham entre duas execuções).
  5. Envia mensagem no Slack quando a issue fechada não é do Ladybug E estava
     entre as últimas N (default 5) restantes do milestone.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
import yaml

CONFIG_PATH = "config.yaml"
GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"


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


def gh_session(token):
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return s


def graphql(session, query, variables):
    resp = session.post(GITHUB_GRAPHQL, json={"query": query, "variables": variables})
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Erro GraphQL: {data['errors']}")
    return data["data"]


# ---------------------------------------------------------------------------
# 1. Descobrir o Project "Ladybug" e todos os itens (issues) que ele contém
# ---------------------------------------------------------------------------

def find_project_id(session, owner, owner_type, project_title):
    root_field = "user" if owner_type == "user" else "organization"
    query = f"""
    query($owner: String!, $cursor: String) {{
      {root_field}(login: $owner) {{
        projectsV2(first: 50, after: $cursor) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{ id title }}
        }}
      }}
    }}
    """
    cursor = None
    while True:
        data = graphql(session, query, {"owner": owner, "cursor": cursor})
        proj = data[root_field]["projectsV2"]
        for node in proj["nodes"]:
            if node["title"].strip().lower() == project_title.strip().lower():
                return node["id"]
        if not proj["pageInfo"]["hasNextPage"]:
            break
        cursor = proj["pageInfo"]["endCursor"]
    return None


def get_ladybug_issue_set(session, owner, owner_type, project_title):
    """Retorna um set de (owner, repo, number) para todas as issues do board Ladybug."""
    project_id = find_project_id(session, owner, owner_type, project_title)
    if not project_id:
        print(f"[aviso] Project '{project_title}' não encontrado para '{owner}' ({owner_type}).")
        return set()

    query = """
    query($id: ID!, $cursor: String) {
      node(id: $id) {
        ... on ProjectV2 {
          items(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              content {
                ... on Issue {
                  number
                  repository { name owner { login } }
                }
              }
            }
          }
        }
      }
    }
    """
    result = set()
    cursor = None
    while True:
        data = graphql(session, query, {"id": project_id, "cursor": cursor})
        items = data["node"]["items"]
        for node in items["nodes"]:
            content = node.get("content")
            if not content:
                continue  # item sem issue vinculada (draft, PR, etc)
            issue_owner = content["repository"]["owner"]["login"]
            repo = content["repository"]["name"]
            result.add((issue_owner.lower(), repo.lower(), content["number"]))
        if not items["pageInfo"]["hasNextPage"]:
            break
        cursor = items["pageInfo"]["endCursor"]
    return result


# ---------------------------------------------------------------------------
# 2. Buscar issues fechadas recentemente
# ---------------------------------------------------------------------------

def search_recently_closed_issues(session, owner, repos, since_dt):
    """Usa a Search API para achar issues fechadas desde `since_dt` nos repos configurados."""
    since_date = since_dt.strftime("%Y-%m-%d")
    repo_filters = " ".join(f"repo:{owner}/{r}" for r in repos)
    query = f"{repo_filters} is:issue is:closed closed:>={since_date}"

    results = []
    page = 1
    while True:
        resp = session.get(
            f"{GITHUB_API}/search/issues",
            params={"q": query, "per_page": 100, "page": page, "sort": "updated"},
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("items", []))
        if len(data.get("items", [])) < 100:
            break
        page += 1
        if page > 10:  # limite de segurança
            break
    return results


# ---------------------------------------------------------------------------
# 3. Issues abertas de um milestone (para saber quantas restam)
# ---------------------------------------------------------------------------

def get_open_issues_in_milestone(session, owner, repo, milestone_number):
    issues = []
    page = 1
    while True:
        resp = session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues",
            params={
                "milestone": milestone_number,
                "state": "open",
                "per_page": 100,
                "page": page,
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        # a API de issues também retorna PRs; filtramos só issues de verdade
        issues.extend([i for i in batch if "pull_request" not in i])
        if len(batch) < 100:
            break
        page += 1
    return issues


# ---------------------------------------------------------------------------
# 4. Slack
# ---------------------------------------------------------------------------

def send_slack_message(webhook_url, project, milestone, issue_title, issue_url,
                        remaining, ladybug_excluded_count):
    text_lines = [
        f":white_check_mark: Issue fechada em *{project}* — milestone *{milestone}*",
        f"<{issue_url}|{issue_title}>",
        f"Faltam *{remaining}* issue(s) para fechar o milestone.",
    ]
    if ladybug_excluded_count > 0:
        text_lines.append(
            f"_({ladybug_excluded_count} issue(s) do board Ladybug não entraram nessa contagem)_"
        )
    payload = {"text": "\n".join(text_lines)}
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

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

    token = os.environ.get("GH_MONITOR_TOKEN")
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not token or not slack_webhook:
        print("Erro: defina GH_MONITOR_TOKEN e SLACK_WEBHOOK_URL como variáveis de ambiente.")
        sys.exit(1)

    session = gh_session(token)
    state = load_state(state_file)

    now = datetime.now(timezone.utc)
    if state["last_checked"]:
        since_dt = datetime.fromisoformat(state["last_checked"])
    else:
        since_dt = now - timedelta(days=lookback_days)

    notified_set = set(state["notified_issues"])

    print(f"Owner: {owner} (tipo: {owner_type})")
    print(f"Buscando issues fechadas desde {since_dt.isoformat()}...")
    ladybug_set = get_ladybug_issue_set(session, owner, owner_type, ladybug_title)
    print(f"Board Ladybug: {len(ladybug_set)} issue(s) mapeada(s).")

    closed_issues = search_recently_closed_issues(session, owner, repos, since_dt)
    print(f"{len(closed_issues)} issue(s) fechada(s) encontrada(s) na janela de busca.")

    # Filtra: só issues com milestone, com closed_at > since_dt real, e ainda não notificadas
    candidates = []
    for issue in closed_issues:
        if not issue.get("milestone"):
            continue
        closed_at = issue.get("closed_at")
        if not closed_at:
            continue
        closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        if state["last_checked"] and closed_dt <= since_dt:
            continue
        key = issue["html_url"]
        if key in notified_set:
            continue
        candidates.append((issue, closed_dt))

    # Agrupa por (owner, repo, milestone_number) pra tratar corretamente
    # o caso de múltiplas issues do mesmo milestone fechadas na mesma janela
    groups = {}
    for issue, closed_dt in candidates:
        repo_url = issue["repository_url"]  # https://api.github.com/repos/OWNER/REPO
        issue_owner, repo = repo_url.split("/repos/")[1].split("/")
        milestone_number = issue["milestone"]["number"]
        milestone_title = issue["milestone"]["title"]
        gkey = (issue_owner, repo, milestone_number)
        groups.setdefault(gkey, {"title": milestone_title, "issues": []})
        groups[gkey]["issues"].append((issue, closed_dt))

    for (issue_owner, repo, milestone_number), info in groups.items():
        milestone_title = info["title"]
        open_now = get_open_issues_in_milestone(session, issue_owner, repo, milestone_number)

        # separa em "não-ladybug" (contam) e "ladybug" (não contam)
        open_non_ladybug = [
            i for i in open_now
            if (issue_owner.lower(), repo.lower(), i["number"]) not in ladybug_set
        ]
        open_ladybug_count = len(open_now) - len(open_non_ladybug)
        remaining_now = len(open_non_ladybug)  # estado real, após todos os fechamentos do lote

        # ordena as issues fechadas deste milestone por data de fechamento (mais antiga -> mais nova)
        batch = sorted(info["issues"], key=lambda t: t[1])
        # separa as que são do Ladybug (não contam, não notificam) das demais
        non_ladybug_batch = [
            (issue, dt) for issue, dt in batch
            if (issue_owner.lower(), repo.lower(), issue["number"]) not in ladybug_set
        ]
        ladybug_batch = [
            (issue, dt) for issue, dt in batch
            if (issue_owner.lower(), repo.lower(), issue["number"]) in ladybug_set
        ]

        # issues do Ladybug: apenas marca como notificadas (não envia Slack)
        for issue, _ in ladybug_batch:
            notified_set.add(issue["html_url"])
            print(f"[ladybug] {issue['html_url']} fechada — fora da contagem, sem alerta.")

        # issues fora do Ladybug: calcula o "remaining" no momento exato do fechamento de cada uma,
        # considerando que outras do mesmo lote podem ter fechado depois dela
        k = len(non_ladybug_batch)
        for idx, (issue, _) in enumerate(non_ladybug_batch):
            # quantas do lote fecharam DEPOIS desta (ainda estavam abertas quando ela fechou)
            closed_after_this = k - (idx + 1)
            remaining_after_this_close = remaining_now + closed_after_this
            remaining_before_this_close = remaining_after_this_close + 1  # incluindo ela mesma

            notified_set.add(issue["html_url"])

            if remaining_before_this_close <= last_n:
                project_name = repo  # nome do repositório = nome do "projeto"
                print(
                    f"[alerta] {issue['html_url']} — restavam {remaining_before_this_close}, "
                    f"agora restam {remaining_after_this_close}"
                )
                send_slack_message(
                    webhook_url=slack_webhook,
                    project=project_name,
                    milestone=milestone_title,
                    issue_title=issue["title"],
                    issue_url=issue["html_url"],
                    remaining=remaining_after_this_close,
                    ladybug_excluded_count=open_ladybug_count,
                )
            else:
                print(
                    f"[info] {issue['html_url']} fechada, mas restavam "
                    f"{remaining_before_this_close} (> {last_n}), sem alerta."
                )

    state["last_checked"] = now.isoformat()
    state["notified_issues"] = list(notified_set)
    save_state(state_file, state)
    print("Execução concluída.")


if __name__ == "__main__":
    main()
