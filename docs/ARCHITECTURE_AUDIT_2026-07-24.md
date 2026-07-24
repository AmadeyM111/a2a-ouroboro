# Ouroboros — архитектурный аудит

Дата: 2026-07-24  
Ветка: `architecture-audit-2026-07-24`  
Объём: чтение исходников, конфигурации, CI и тестовой структуры; изменения runtime-кода в рамках аудита не вносились.

## Резюме

Ouroboros — это single-process web runtime, который поднимает Starlette/uvicorn, supervisor в фоне, multiprocessing worker pool и файловое durable state. Agent core находится в worker-процессах; gateway, UI, skill/marketplace/MCP surfaces и A2A transport добавляют большое количество boundary-контрактов.

Сильная сторона проекта — большое число защитных инвариантов и тестов вокруг task lifecycle, worktree/process custody, skill review и ограничений ресурсов. Основная слабость — цена этой эволюции: критические пути распределены между `server.py`, `supervisor/*`, `ouroboros/*`, несколькими файловыми state stores и двумя gateway-реализациями. В результате fail-open решения, drift конфигурации и неоднозначные identity boundaries легко остаются незамеченными.

## Карта архитектуры

```text
launcher / CLI / UI
        |
        v
server.py + NetworkAuthGate
        |
        +-- gateway/* ---------------- HTTP/WebSocket API
        +-- supervisor thread
        |      +-- queue/state/events/workers/reaper/git_ops
        |      +-- multiprocessing worker pool
        |
        +-- worker process
               +-- agent -> loop -> LLM/provider
               +-- tools / skills / MCP / workspace executor
               +-- memory, task results, artifacts, project state

separate deployment path:
agent containers <-> gateway/app/main.py <-> SQLite WAL
```

## Findings

### P1 — non-loopback server может остаться полностью открытым

`validate_network_auth_configuration()` безусловно возвращает `None` (`ouroboros/server_auth.py:53-54`). При отсутствии `OUROBOROS_NETWORK_PASSWORD` middleware пропускает все запросы (`server_auth.py:321-324`), а `server.py` лишь пишет warning (`server.py:1747-1753`). При этом web/API surface содержит destructive operations: task cancellation, settings, skill lifecycle, marketplace install/delete, git control, model lifecycle и extension dispatch.

Это не только deployment footgun: достаточно выставить `OUROBOROS_SERVER_HOST=0.0.0.0` или ошибиться в Docker reverse-proxy, чтобы control plane стал доступен соседней сети.

Рекомендация: сделать non-loopback + отсутствующий пароль hard fail по умолчанию. Для намеренного dev-режима нужен явный opt-in вроде `OUROBOROS_ALLOW_INSECURE_NETWORK=1`, который должен быть виден в health/startup diagnostics и не включаться автоматически в compose.

### P1 — identity collision в A2A gateway не проверяется

`configured_tokens()` строит mapping `token -> agent_id` (`gateway/app/main.py:96-102`). Если два `AGENTn_A2A_TOKEN` совпали, более поздний агент silently перезаписывает предыдущего; один и тот же bearer затем аутентифицирует только последнего агента (`main.py:114-117`). Нет startup validation на полноту, уникальность, минимальную энтропию или rotation policy.

Рекомендация: валидировать конфигурацию на startup и завершать запуск при duplicate/missing/weak token; хранить identity отдельно от credential lookup и добавить контролируемую ротацию. Для production-режима лучше использовать secret store или mTLS/service identity вместо пяти статических env-переменных.

### P1 — delivery semantics A2A явно at-least-once, но это не оформлено как контракт

`/v1/inbox` сначала выбирает `queued/delivered`, затем обновляет `queued -> delivered` (`gateway/app/main.py:437-471`). Два параллельных poller-а одного recipient могут прочитать один и тот же набор до фиксации обновления и оба получить сообщения. Это допустимо для at-least-once, но endpoint не выдаёт lease/claim id и не имеет visibility timeout/dead-letter механизма.

Рекомендация: явно закрепить at-least-once + idempotent consumer contract либо реализовать atomic claim (`UPDATE ... RETURNING`/lease table), ack timeout и reclaim policy. В текущем виде сбой consumer после inbox-read оставляет сообщение в `delivered`, а retry semantics остаётся неочевидным.

### P1 — при проблемах с lock система допускает lost update

Supervisor прямо продолжает работу без lock после timeout (`supervisor/state.py:144-153`). Для task results после `TimeoutError` выполняется unlocked read/merge/write (`ouroboros/task_results.py:180-192`). Это сознательный availability trade-off, но в системе с несколькими worker-процессами именно он может потерять terminal status, budget counters или cancellation latch.

Рекомендация: разделить lock timeout на безопасные и критические операции. Для terminal task state, budget accounting и cancel latch — fail closed с durable retry/outbox; для диагностических snapshot — допускается stale/unlocked fallback. Добавить метрику/health flag, а не только log.

### P2 — границы модулей уже пересекли собственные hard gates

По текущему дереву: `ouroboros/llm.py` — 2721 строка, `supervisor/events.py` — 2086, `ouroboros/loop.py` — 1998, `ouroboros/extension_loader.py` — 1968, `server.py` — 1818; весь runtime (`ouroboros/` + `supervisor/`) — около 103k строк в 197 Python-файлах. Документация сама фиксирует grandfathered oversized modules и deferred debt (`docs/DEVELOPMENT.md:182-190`).

Риск не в размере как таковом, а в ownership: transport routing, lifecycle state, model calls, review gates и filesystem effects трудно тестировать независимо. Любая новая фича увеличивает число cross-module lazy imports и monkeypatch seams. Особенно уязвимы supervisor event handling и extension loader.

Рекомендация: выделить стабильные application ports: `TaskRepository`, `WorkerRuntime`, `ProviderRouter`, `SkillRuntime`, `EventSink`; handlers должны зависеть от интерфейсов, а не импортировать live globals (`PENDING`, `RUNNING`, `WORKERS`, `DATA_DIR`). Ввести ADR на каждый pay-down oversized module и запретить новые функции в grandfathered-файлах без migration target.

### P2 — package/dependency sources расходятся

`pyproject.toml` и `requirements.txt` дублируют runtime dependency graph, но не идентичны: `pyproject.toml` содержит `typing_extensions>=4.5.0` (`pyproject.toml:51-53`), а `requirements.txt` его не содержит; `requirements.txt` дополнительно ставит browser-пакеты без явной связи с optional extra. Версии большинства библиотек не зафиксированы.

Риск усиливается тем, что CI устанавливает requirements отдельно от package metadata (`.github/workflows/ci.yml:61-68`), а локальный запуск/packaging могут использовать другой набор. Уже есть комментарий о несовместимости tree-sitter, что показывает реальность drift-проблемы.

Рекомендация: выбрать один SSOT (лучше `pyproject.toml` с extras/lock-файлом), собирать wheel и тестировать именно его; добавить `pip check`, lock/constraints и startup dependency report.

### P2 — основной CI не защищает текущую ветку разработки

Quick test запускается только для push в ветку `ouroboros` (`.github/workflows/ci.yml:48-54`), full matrix — только для `ouroboros-stable`, manual и tags (`ci.yml:73-80`). В репозитории при аудите активна `master`, поэтому изменение в обычной рабочей ветке не обязано пройти ни быстрый, ни matrix gate.

Рекомендация: добавить pull_request trigger и required status checks для default branch; разделить expensive integration lanes, но оставить хотя бы smoke + packaging import gate обязательными для каждого PR.

## Проверки

- Рабочее дерево до аудита содержало пользовательский untracked `docs/.knowledges`; файл не изменялся.
- Создана отдельная ветка `architecture-audit-2026-07-24`.
- `python3 -m pytest -q --tb=short` не стартовал: в доступном `/opt/homebrew/opt/python@3.14/bin/python3.14` отсутствует модуль `pytest`. Это ограничение окружения, а не результат падения тестов проекта.
- Статический аудит выполнен по `server.py`, `ouroboros/`, `supervisor/`, `gateway/`, `compose*.yaml`, `pyproject.toml`, `requirements.txt`, `.github/workflows/ci.yml` и архитектурной документации.

## Приоритетный план исправлений

1. Закрыть P1 security: fail-closed non-loopback auth и startup validation A2A credentials.
2. Зафиксировать A2A delivery contract и добавить concurrency tests для inbox/ack.
3. Убрать unlocked fallback из terminal state/budget paths; оставить его только для telemetry.
4. Свести dependency/packaging SSOT и включить PR CI.
5. После этого платить module debt через ports/adapters, начиная с supervisor events и extension loader.
