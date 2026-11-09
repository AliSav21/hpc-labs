-- Лабораторна робота 4, варіант 19: черга задач обробки і результати.
-- Накочується окремим Job db-migrate-lab4 після схеми ЛР1.

CREATE TABLE IF NOT EXISTS tasks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id    BIGINT      NOT NULL REFERENCES donor_registry (id) ON DELETE CASCADE,
    status       TEXT        NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'running', 'done', 'failed')),
    -- seed у домені генератора зрізу: 1 ≤ seed < 2^31. Нуль лишається дозволеним у
    -- таблиці навмисно — на ньому крок обробки викидає виняток (сценарій Ф3).
    seed         BIGINT      NOT NULL CHECK (seed >= 0 AND seed < 2147483648),
    attempts     INTEGER     NOT NULL DEFAULT 0,
    max_attempts INTEGER     NOT NULL DEFAULT 3,
    token        INTEGER     NOT NULL DEFAULT 0,
    lease_until  TIMESTAMPTZ,
    worker_id    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);

-- Під захоплення: WHERE status = 'queued' ORDER BY created_at LIMIT 1
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks (status, created_at);
-- Під повернення задач зі спливлим lease: WHERE status = 'running' AND lease_until < now()
CREATE INDEX IF NOT EXISTS idx_tasks_running_lease ON tasks (lease_until) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS task_results (
    task_id    BIGINT      PRIMARY KEY REFERENCES tasks (id) ON DELETE CASCADE,
    token      INTEGER     NOT NULL,
    worker_id  TEXT        NOT NULL,
    result     JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Додаткова вимога Д5: тригер на вставку в tasks надсилає повідомлення, воркер у режимі
-- LISTEN чекає на нього замість опитування. Канал один на всю чергу, payload не потрібен:
-- воркер усе одно захоплює найстаршу задачу зі статусом queued.
CREATE OR REPLACE FUNCTION tasks_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('tasks_new', '');
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tasks_notify_insert ON tasks;
CREATE TRIGGER tasks_notify_insert
    AFTER INSERT ON tasks
    -- один раз на команду, а не на рядок: пакетна вставка не має давати пачку повідомлень
    FOR EACH STATEMENT EXECUTE FUNCTION tasks_notify();
