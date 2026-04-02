# Bug: Discord typing indicator can remain visible after Shelly has already sent the response

Date: 2026-04-01
Repo: `/home/alan/projects/hermes-agent-discord-live`
Branch: `discord-live`

## Summary

In Discord, the typing indicator can get stuck after the bot has finished responding.

Observed user-facing symptom:
- Discord still shows `Shelly is typing...`
- the bot's actual response is already visible in the channel
- the indicator does not clear when the response is sent
- it sits there until Shelly types again later

This is incorrect behavior. The typing indicator should clear as soon as the response has been delivered.

## Evidence

Screenshot captured locally:
- `/home/alan/.hermes/images/clip_20260401_053744_1.png`

What the screenshot shows:
- prior Shelly messages are already present in the chat
- Discord still shows `Shelly is typing...` above the composer
- this indicates the typing state lingered after response completion

## Expected behavior

When Shelly finishes sending the response, the typing indicator should stop immediately.

## Actual behavior

The `Shelly is typing...` indicator can remain visible after the message has already been sent and delivered.

## Suspected area

This likely lives somewhere in the Discord typing lifecycle / stop-typing path.

Relevant areas to inspect:
- `gateway/platforms/discord.py`
- `gateway/platforms/discord_impl/delivery.py`
- shared gateway typing lifecycle interactions between response completion and `stop_typing()`

## Related upstream context

There is already an upstream issue in `NousResearch/hermes-agent` that looks related:
- `#2831` — `Discord typing indicator lingers after response due to race between base.py _keep_typing and _typing_tasks`

This local note exists because the bug is still being observed on the local `discord-live` branch in real usage.

## Good fix criteria

A fix should ensure:
1. typing starts while work is in progress
2. typing is reliably stopped after the final message is sent
3. no stale typing indicator remains visible in Discord after delivery
4. repeated sends / multi-message responses do not leave orphaned typing loops behind
