"""
Alerta de issues adicionadas (milestoned) ou removidas (demilestoned) de
milestones. Usa o endpoint de eventos de issues do repositório.

Ativado por `notify_milestone_changes: true` no config.yaml.
"""

from datetime import datetime

import requests

from common import GITHUB_API, format_project_name, get_open_issues_in_milestone


def get_milestone_number_by_title(session, owner, repo):
    """Mapa título -> número dos milestones do repo."""
    mapping = {}
    page = 1
    while True:
        resp = session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/milestones",
            params={"state": "all", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        for m in batch:
            mapping[m["title"]] = m["number"]
        if len(batch) < 100:
            break
        page += 1
    return mapping


def get_recent_milestone_events(session, owner, repo, since_dt, max_pages=5):
    """Eventos milestoned/demilestoned desde since_dt (só issues, não PRs)."""
    events = []
    for page in range(1, max_pages + 1):
        resp = session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/events",
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        page_has_recent = False
        for e in batch:
            created = datetime.fromisoformat(e["created_at"].replace("Z", "+00:00"))
            if created <= since_dt:
                continue
            page_has_recent = True
            if e["event"] not in ("milestoned", "demilestoned"):
                continue
            if "pull_request" in e.get("issue", {}):
                continue
            events.append((e, created))

        # assume ordem decrescente: se a página toda é antiga, para
        if not page_has_recent or len(batch) < 100:
            break
    return events


def send_slack_milestone_change(webhook_url, project, milestone, issue_title,
                                issue_url, issue_number, action, remaining,
                                ladybug_excluded_count, repo_display_map,
                                default_emoji):
    repo_label = format_project_name(project, repo_display_map, default_emoji)
    verbo = "adicionada ao" if action == "milestoned" else "removida do"
    text_lines = [
        f"*{repo_label}*",
        f"Issue {verbo} milestone *{milestone}*",
        " ",
        f"<{issue_url}|#{issue_number} - {issue_title}>",
        f"Agora faltam *{remaining}* issue(s) para fechar o milestone.",
    ]
    if ladybug_excluded_count > 0:
        text_lines.append(
            f"_({ladybug_excluded_count} issue(s) do board Ladybug não entraram nessa contagem)_"
        )
    resp = requests.post(webhook_url, json={"text": "\n".join(text_lines)}, timeout=15)
    resp.raise_for_status()


def process_milestone_events(session, owner, repos, since_dt, notified_set,
                             ladybug_set, slack_webhook, repo_display_map,
                             default_emoji):
    """Envia alerta para cada evento milestoned/demilestoned novo.
    Atualiza `notified_set` in place (chaves no formato 'event:<id>')."""
    for repo in repos:
        events = get_recent_milestone_events(session, owner, repo, since_dt)
        if not events:
            continue
        milestone_numbers = get_milestone_number_by_title(session, owner, repo)

        for e, _ in sorted(events, key=lambda t: t[1]):
            event_key = f"event:{e['id']}"
            if event_key in notified_set:
                continue
            notified_set.add(event_key)

            title = e["milestone"]["title"]
            number = milestone_numbers.get(title)
            issue = e["issue"]
            if number is None:
                continue
            # não alerta se a issue em si é do Ladybug
            if (owner.lower(), repo.lower(), issue["number"]) in ladybug_set:
                print(f"[ladybug] {issue['html_url']} {e['event']} — fora da contagem, sem alerta.")
                continue

            open_now = get_open_issues_in_milestone(session, owner, repo, number)
            open_non_ladybug = [
                i for i in open_now
                if (owner.lower(), repo.lower(), i["number"]) not in ladybug_set
            ]

            print(f"[alerta] {issue['html_url']} — {e['event']} em '{title}'")
            send_slack_milestone_change(
                webhook_url=slack_webhook,
                project=repo,
                milestone=title,
                issue_title=issue["title"],
                issue_url=issue["html_url"],
                issue_number=issue["number"],
                action=e["event"],
                remaining=len(open_non_ladybug),
                ladybug_excluded_count=len(open_now) - len(open_non_ladybug),
                repo_display_map=repo_display_map,
                default_emoji=default_emoji,
            )
