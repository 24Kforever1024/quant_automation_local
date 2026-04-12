from __future__ import annotations

from typing import Any, Mapping

import requests

from config import Settings, get_settings


class GitHubDispatchClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def dispatch(self, client_payload: Mapping[str, Any]) -> None:
        owner = self.settings.github_repository_owner
        repo = self.settings.github_repository_name
        token = self.settings.github_dispatch_token
        event_type = self.settings.github_dispatch_event_type
        if not owner or not repo or not token:
            raise RuntimeError("GitHub dispatch settings are incomplete")

        url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
        response = requests.post(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "event_type": event_type,
                "client_payload": dict(client_payload),
            },
            timeout=20,
        )
        response.raise_for_status()
