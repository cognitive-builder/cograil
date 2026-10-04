"""Slack settings come from the environment, never from Workspace files.

- SLACK_BOT_TOKEN       the bot token (`xoxb-...`); unset means the Slack channel is off
- SLACK_SIGNING_SECRET  verifies that a request comes from Slack; required with the token
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from cograil.errors import SlackNotConfigured


class SlackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: SecretStr
    signing_secret: SecretStr


def slack_settings(env: Mapping[str, str]) -> SlackSettings | None:
    """The Slack settings in `env`; None when SLACK_BOT_TOKEN is unset."""
    token = env.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return None
    secret = env.get("SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        raise SlackNotConfigured("SLACK_BOT_TOKEN is set but SLACK_SIGNING_SECRET is not")
    return SlackSettings(bot_token=SecretStr(token), signing_secret=SecretStr(secret))
