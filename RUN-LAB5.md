# Запуск ЛР5 (варіант 19: С9, n = 6, місткість 1024)

Докер, кластер і база для ЛР5 не потрібні — усе виконується в одному процесі Python.
Потрібен лише Python 3.10 або новіший; `numpy` і `matplotlib` беруться з
`requirements-dev.txt` (numpy — для сортування масиву тривалостей, matplotlib — для
діаграм). Усі команди запускаються з кореня репозиторію.

Якщо Docker Desktop або кластер kind з ЛР3–ЛР4 підняті, зупиніть їх перед прогонами:
прогони міряють затримки в мікросекундах і чутливі до сторонніх навантажень.

## 0. Залежності

```powershell
pip install -r requirements-dev.txt
```

## 1. Усі прогони і таблиці 1-6 за один раз

```powershell
python -m scripts.lab5_run --out-dir results/lab5
```

Знімає, по три прогони на кожну конфігурацію по D = 10 с після прогріву 1 с:

| Конфігурація | Для чого |
|---|---|
| `cs`, глобальний замок | t_cs,w і t_cs,r без конкуренції (крок 2) |
| `single`, глобальний замок і RWLock | X_1 одного потоку в пропорції 3 : 3 (таблиця 1) |
| `threads`, глобальний замок | крок 2, таблиця 2 |
| `threads`, RWLock з перевагою читача | крок 3, таблиця 3 |
| `threads`, RWLock з перевагою писця | додатковий прогін про голодування ролі |
| `threads`, обидва замки, суміш запису `pair` | контроль другого читання операції запису |

Триває близько 5 хвилин. Пише `results/lab5/*.json`,
[results/lab5/tables.md](results/lab5/tables.md) з таблицями 1-6 і
`results/lab5/summary.json`.

Перебудувати таблиці з уже знятих файлів, без повторних прогонів:

```powershell
python -m scripts.lab5_run --tables-only --out-dir results/lab5
```

Без контрольної серії (на хвилину швидше):

```powershell
python -m scripts.lab5_run --out-dir results/lab5 --no-control
```

## 2. Перевірка, що читання не змінює структуру (крок 3)

```powershell
python -m scripts.lab5_readonly_check --out results/lab5/readonly_check.json
```

Статична перевірка тіл `peek_front`, `peek_back` і `size` плюс порівняння повного
знімка структури до і після 200 000 читань з одного потоку і читань із трьох потоків
під RWLock. Код виходу 0 — перевірка пройдена.

## 3. Дослід про хвіст тривалості під глобальним замком

```powershell
python -m scripts.lab5_switch --out results/lab5/switch_interval.json
```

Та сама конфігурація кроку 2 на трьох значеннях `sys.setswitchinterval` (5; 1; 0,2 мс),
по три прогони на кожне. Триває близько 6 хвилин.

## 4. Діаграми

```powershell
python -m scripts.lab5_charts --out-dir results/lab5/charts
```

## Окремий прогін руками

Стенд можна запустити й напряму — він друкує все, що вимагає крок 1 умови:

```powershell
python -m lab5.bench threads --lock global --n 6 --duration 10 --out results/lab5/my_run.json
```

```powershell
python -m lab5.bench threads --lock rw --n 6 --duration 10
```

```powershell
python -m lab5.bench single --lock global --n 6 --duration 10
```

```powershell
python -m lab5.bench cs --lock global --n 6 --duration 10
```

Ключі: `--lock` (`global`, `rw` — перевага читача, `rw-writer` — перевага писця),
`--n`, `--capacity`, `--warmup`, `--duration`, `--seed`, `--write-mix`
(`random` за умовою варіанта, `pair` — контрольне строге чергування push і pop), `--out`.
