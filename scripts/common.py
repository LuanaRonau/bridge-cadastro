"""
Infraestrutura compartilhada entre as funcionalidades do monitor:
sessão do GitHub, GraphQL, board "Ladybug", issues abertas de um milestone
e formatação do nome do projeto para o Slack.
"""

import requests

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"


# Sessão / GraphQL ---------------------------------------------------------

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



# Project "Ladybug" e todos os itens (issues) que ele contém ----------------

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


# Issues abertas de um milestone (para saber quantas restam) ----------------

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


# Slack (formatação comum) ---------------------------------------------------

def format_project_name(project, display_map, default_emoji):
    """Retorna o nome de exibição do projeto, prefixado com seu emoji custom
    do Slack, conforme mapeamento definido no config.yaml. Se o repo não estiver
    mapeado, usa o próprio nome do repo como fallback e o `default_emoji`."""
    info = display_map.get(project, {})
    emoji = info.get("emoji", default_emoji)
    display_name = info.get("display_name", project)
    return f":{emoji}: {display_name}"
