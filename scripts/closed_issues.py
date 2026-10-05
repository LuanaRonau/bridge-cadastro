"""
Alerta de issues fechadas em milestones.

Envia mensagem no Slack quando a issue fechada não é do Ladybug E estava
entre as últimas N restantes do milestone (contagem exclui issues do Ladybug).
"""

from datetime import datetime

import requests

from common import GITHUB_API, format_project_name, get_open_issues_in_milestone


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


def send_slack_message(webhook_url, project, milestone, issue_title, issue_url,
                       issue_number, remaining, ladybug_excluded_count,
                       repo_display_map, default_emoji):
    repo_label = format_project_name(project, repo_display_map, default_emoji)
    text_lines = [
        f"*{repo_label}*",
        f"Issue fechada no milestone *{milestone}*",
        " ",
        f"<{issue_url}|#{issue_number} - {issue_title}>",
        f"Faltam *{remaining}* issue(s) para fechar o milestone.",
    ]
    if ladybug_excluded_count > 0:
        text_lines.append(
            f"_({ladybug_excluded_count} issue(s) do board Ladybug não entraram nessa contagem)_"
        )
    payload = {"text": "\n".join(text_lines)}
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def process_closed_issues(session, owner, repos, since_dt, first_run, notified_set,
                          ladybug_set, slack_webhook, last_n, repo_display_map,
                          default_emoji):
    """Processa as issues fechadas desde `since_dt`.

    `first_run` é True quando não existe `last_checked` no state (nesse caso
    o filtro fino por horário de fechamento não é aplicado).
    Atualiza `notified_set` in place.
    """
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
        if not first_run and closed_dt <= since_dt:
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
                    issue_number=issue["number"],
                    remaining=remaining_after_this_close,
                    ladybug_excluded_count=open_ladybug_count,
                    repo_display_map=repo_display_map,
                    default_emoji=default_emoji,
                )
            else:
                print(
                    f"[info] {issue['html_url']} fechada, mas restavam "
                    f"{remaining_before_this_close} (> {last_n}), sem alerta."
                )
