# Запуск лабораторної роботи 2 (варіант 19)

Параметри варіанта 19: розмір пулу зʼєднань **10**, набір бакетів **Г1** (5, 10, 25, 50, 100,
250, 500, 1000 мс), додаткова метрика **М1** (зайнятість пулу і гістограма часу очікування
зʼєднання). Ліміти контейнера `web` з лабораторної роботи 1 — **1 CPU, 512 МБ** — не змінюються.

Команди подано для PowerShell з кореня репозиторію. У Git Bash шлях у `--path` треба брати в
лапки і ставити `MSYS_NO_PATHCONV=1`, інакше `/noop` перетвориться на шлях Windows.

## 0. Підготовка

```powershell
copy .env.example .env   # і задати POSTGRES_PASSWORD
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # matplotlib для діаграм
```

## 1. Стенд і Prometheus

```powershell
docker compose up -d --build --scale web=4 --wait
docker compose restart lb
```

Перевірка цілей: http://127.0.0.1:19090/targets — усі чотири екземпляри `web` мають стан **UP**
(порт з `PROM_PORT` у `.env`). Метрики одного екземпляра: `docker compose exec web curl -s
localhost:8000/metrics` або через балансувальник `http://127.0.0.1:18080/metrics`.

## 2. Коректна зупинка (крок 3)

```powershell
docker compose up -d --scale web=2 --wait; docker compose restart lb
.\.venv\Scripts\python.exe scripts\graceful_check.py
```

Скрипт тримає 200 rps, зупиняє один контейнер, перевіряє код завершення 0 і відсутність
відповідей не 2xx та помилок зʼєднання у вікні ±10 с. Результат:
`results/lab2/graceful_verdict.json`.

## 3. Межа генератора G (крок 6)

```powershell
docker compose up -d --scale web=1 --wait; docker compose restart lb
.\.venv\Scripts\python.exe -m loadgen.step --path /noop --out results/lab2/noop --label "G: /noop"
```

`G` це `X` у `results/lab2/noop.json`: автоматика видала **2797 rps** (сходинка 2800 rps).
Прогін зупинився не через насичення, а через зупинку генератора на 305-й секунді, тож це
**нижня оцінка**: сходинка 3000 rps трималась 14 с із досягнутою 2997 rps. Для прогонів
N = 1…4 межа ні в якому варіанті не вмикалась — найбільша досягнута 938 rps.

## 4. Серія прогонів (крок 7)

Для кожного N окремо; скрипт сам масштабує стенд, перезапускає балансувальник, засіває 100
записів, веде прогін, знімає `docker stats` і збирає метрики Prometheus за сходинками.

```powershell
.\.venv\Scripts\python.exe -m loadgen.run_steps --instances 1 --max-rate 2797
.\.venv\Scripts\python.exe -m loadgen.run_steps --instances 2 --max-rate 2797
.\.venv\Scripts\python.exe -m loadgen.run_steps --instances 3 --max-rate 2797
.\.venv\Scripts\python.exe -m loadgen.run_steps --instances 4 --max-rate 2797
```

Кожен прогін дає `results/lab2/N<k>.json` (зведення, сходинки, метрики Prometheus, знімки
`docker stats`), `N<k>.csv` (посекундний запис) і `N<k>_prom.json` (часові ряди для діаграм).

Кількість CPU і памʼяті, виділені Docker, записати окремо:

```powershell
docker info --format "{{.NCPU}} CPU, {{.MemTotal}} bytes"
```

## 5. Таблиці і діаграми (кроки 8–11)

**До видалення томів Compose**, бо діаграми беруть дані з Prometheus.

```powershell
.\.venv\Scripts\python.exe scripts\lab2_tables.py --exclude ""
.\.venv\Scripts\python.exe scripts\charts.py --exclude ""
```

У `--exclude` перелічити точки, обмежені генератором (наприклад `--exclude 4`): такі точки не
входять у підбір σ і κ. Таблиці: `results/lab2/tables.md`. Діаграми: `results/lab2/charts/`.

## 6. Якщо точка N = 1 обмежена генератором

Зменшити ліміт CPU контейнера `web` удвічі (`cpus: "0.5"` у `docker-compose.yml`), записати новий
ліміт і повторити кроки 3–5.

## 7. Прибирання

```powershell
docker compose down -v
```

## 8. Якщо Docker Desktop капризує

Дві несправності, які трапились під час вимірювань на цій машині:

- **Docker Desktop не стартує** з помилкою `removing stale socket: ... The file cannot be accessed
  by the system`. Застарілі файли сокетів у `%LOCALAPPDATA%/Docker/run` не видаляються звичайними
  засобами; теку треба перейменувати (Docker створить нову) і запустити Docker знову. Щоб це не
  повторювалось, Docker Desktop варто закривати через «Quit», а не знімати процеси силою.
- **Опублікований порт слухає, але зʼєднання обриваються** (`curl` повертає код 000). Docker
  втратив проброс портів у WSL. Лікується перезапуском Docker Desktop; окремо
  [loadgen/run_steps.py](loadgen/run_steps.py) сам пересоздає балансувальник, якщо за 30 с не
  отримав жодної відповіді.
