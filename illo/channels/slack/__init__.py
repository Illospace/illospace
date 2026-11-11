"""Slack Socket Mode channel."""


async def setup(ctx) -> None:
    from illo.channels.slack.connection import setup as connect
    from illo.tools.slack import perform_dm, perform_post

    ctx.draft_performers["slack_send_dm"] = perform_dm
    ctx.draft_performers["slack_post_channel"] = perform_post
    from illo.channels.slack.tasks import setup as task_setup

    await task_setup(ctx)
    await connect(ctx)
