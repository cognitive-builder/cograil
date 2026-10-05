"""The Slack channel (issue #27): messages to the bot start or continue a Run, one thread per
Run; approval buttons decide the Run's gates; a Slack member id maps to a Principal.

Importing this package does not need `slack-bolt`; the Bolt wiring is in `cograil.channels.slack
.bolt` and is imported only when SLACK_BOT_TOKEN is set.
"""

from cograil.channels.slack.channel import SLACK_KEY, Incoming, Poster, SlackChannel
from cograil.channels.slack.identity import principal_for_slack_user
from cograil.channels.slack.settings import SlackSettings, slack_settings

__all__ = [
    "SLACK_KEY",
    "Incoming",
    "Poster",
    "SlackChannel",
    "SlackSettings",
    "principal_for_slack_user",
    "slack_settings",
]
