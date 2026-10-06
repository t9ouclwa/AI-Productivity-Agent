# Sanitized Demo

The conversations below are synthetic examples. They illustrate behavior without exposing private chat history, email, or vault content.

## Natural-language reminder

```text
User: 明天晚上 8 点提醒我交周报。
DOCTOR: 了解。明日20:00に「交周报」ってリマインドする。
        [tap-to-reveal Chinese translation]
```

DOCTOR parses the time locally, persists the reminder, registers a scheduler job, and restores pending jobs after restart.

## Quiet period with deterministic follow-up

```text
User: 我在开会，半小时后再来找我。
DOCTOR: 好的。
```

The local quiet gate suppresses proactive messages during the meeting. A one-shot follow-up job is registered for the end of the requested period.

## Note-grounded question

```text
User: /ask 我上次记录的实验下一步是什么？
DOCTOR: 根据本地 Markdown 笔记，下一步是……
```

The `/ask` path gathers local Markdown notes and instructs the model to answer from those notes rather than general memory.

## Gmail summary

```text
User: /gmail
DOCTOR: 有两封未读邮件需要处理：一封要求确认时间，另一封包含待提交材料……
```

Only sender, subject, date, snippet, and message identifiers are fetched with the Gmail read-only scope.

## Proactive safety gates

Before a check-in or memory-driven spark is generated, deterministic code checks opt-in state, quiet windows, idle time, cooldowns, pending user input, and repetition risk. The model is not allowed to bypass these gates.
