# DARAI — подготовка и проверка локального AI

Проверено 2026-09-23 на Apple M4 Pro (14 CPU, 48 GB RAM, macOS 26.6.2, arm64), Python 3.11.15 venv `backend/.venv`. Команды ниже запускались именно в таком виде. Что не проверено — в разделе «Не проверено».

## 1. Оборудование и режим вычислений

| Компонент | Устройство | Почему |
| --- | --- | --- |
| faster-whisper (CTranslate2 4.8.2) | CPU, `ASR_COMPUTE_TYPE=int8` | У CTranslate2 нет Metal-бэкенда, CUDA на Mac нет |
| pyannote / WeSpeaker (torch 2.5.1) | CPU (`TORCH_DEVICE=cpu`) | MPS доступен на хосте, но в Docker Desktop на macOS его нет; MPS с pyannote не проверялся |
| LLM | Ollama на хосте (Metal) | Контейнер `ollama` на macOS работает только на CPU |

GPU-ускорение не обещаем: на Linux с NVIDIA можно поставить `ASR_DEVICE=cuda`, `ASR_COMPUTE_TYPE=float16`, `TORCH_DEVICE=cuda` и CUDA-колёса torch (`TORCH_INDEX_URL`), но это не проверялось.

## 2. Модели, ревизии, размеры

| Модель | Репозиторий @ revision | Путь в `MODELS_DIR` | Размер | Доступ |
| --- | --- | --- | --- | --- |
| ASR | `Systran/faster-whisper-large-v3@edaa852ec7e145841d8ffdb056a99866b5f0a478` | `faster-whisper-large-v3/` | 2.9 GB | открыт |
| Live ASR (необязательно, только preview) | `mobiuslabsgmbh/faster-whisper-large-v3-turbo@0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` | `faster-whisper-large-v3-turbo/` | 1.6 GB | открыт |
| Голосовые векторы | `pyannote/wespeaker-voxceleb-resnet34-LM@837717ddb9ff5507820346191109dc79c958d614` | `pyannote/wespeaker-voxceleb-resnet34-LM/` | 25 MB | открыт |
| Сегментация | `pyannote/segmentation-3.0@e66f3d3b9eb0873085418a7b813d3b369bf160bb` | `pyannote/segmentation-3.0/` | 6 MB | **gated** |
| Диаризация (конфиг) | `pyannote/speaker-diarization-3.1@84fd25912480287da0247647c3d2b4853cb3ee5d` | `pyannote/speaker-diarization-3.1/config.yaml` | <1 MB | **gated** |
| LLM | Ollama `qwen3:8b` (digest `500a1f067a9f`) | `~/.ollama` или `models/ollama` | 5.2 GB | открыт |

Итого около 8.2 GB. Скрипт подготовки требует свободного места не меньше размера × 1.2 + 2 GB. Диаризация собирается в `app/ml.py` из локальных чекпойнтов: сегментация и **тот же объект** WeSpeaker, что для профилей. Из `config.yaml` берутся только гиперпараметры, ID моделей на Hub в рантайме не нужны.

Память процесса (измерено, пиковый RSS): WeSpeaker 0.44 GB; вместе с ASR large-v3 int8 3.05 GB, после прогона ASR 3.13 GB. Диаризация не измерялась, веса сегментации 6 MB. `qwen3:8b` Q4 в Ollama занимает около 5.2 GB весов в unified memory (по размеру модели, RSS runner на Metal не показателен). Рекомендация: ≥ 16 GB RAM на хосте, Docker Desktop ≥ 6 GB.

## 3. Подготовка (однократно, с интернетом)

```bash
# из корня репозитория, venv backend (uv): python 3.11
cd backend && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python \
    -r requirements.txt -r requirements-ml.txt -r requirements-dev.txt && cd ..

backend/.venv/bin/python scripts/ai/diagnose_env.py          # ОС, CPU/RAM/GPU, диск, версии, ffmpeg, ollama (без секретов)
backend/.venv/bin/python scripts/ai/prepare_models.py --only asr,embedding   # проверено
backend/.venv/bin/python scripts/ai/prepare_models.py --only live_asr         # turbo для live preview, проверено 2026-09-23
# gated-модели: принять условия на
#   https://hf.co/pyannote/segmentation-3.0 и https://hf.co/pyannote/speaker-diarization-3.1
HF_TOKEN=hf_xxx backend/.venv/bin/python scripts/ai/prepare_models.py --only segmentation,diarization
ollama pull qwen3:8b                                           # проверено, Ollama 0.33.2
```

`prepare_models.py` скачивает только перечисленные файлы по закреплённым ревизиям, пишет `REVISION`, создаёт `models/.gitignore` (`*`) и удаляет кэш загрузки. `HF_TOKEN` нужен только этой команде; в `.env` и в образ он не попадает.

## 4. Офлайн-проверка артефактов

```bash
cd backend
MODELS_DIR=../models .venv/bin/python ../scripts/ai/check_models.py
```

Скрипт блокирует все не-loopback соединения и загружает каждую модель с `HF_HUB_OFFLINE=1`. Фактический вывод на этой машине:

```
[asr] files: OK  rev=Systran/faster-whisper-large-v3@edaa852e…
[diarization] files: MISSING ../models/pyannote/speaker-diarization-3.1/config.yaml, ../models/pyannote/segmentation-3.0/pytorch_model.bin
[embedding] files: OK  rev=pyannote/wespeaker-voxceleb-resnet34-LM@837717dd…
[embedding] load: OK (2.9s, offline)
[diarization] load: FAILED Не найден config.yaml диаризации: …
[asr] load: OK (4.3s, offline)
```

## 5. Локальная LLM

Проверено: Ollama 0.33.2 на хосте, `qwen3:8b`, endpoint `http://localhost:11434/v1`.

**Отключение thinking различается по runtime, параметры не взаимозаменяемы.** Проверено запросами к `/v1/chat/completions`:

| Параметр | Ollama 0.33.2 `/v1` | Результат |
| --- | --- | --- |
| без параметров | thinking включён | 5.8 с, поле `reasoning` заполнено |
| `"think": false` | **игнорируется** | 2.6 с, `reasoning` заполнено |
| `"reasoning_effort": "none"` | работает | 0.37 с, `reasoning` пустое |

`LLM_THINKING_CONTROL=ollama` (по умолчанию) отправляет `reasoning_effort: "none"`, `vllm` — `chat_template_kwargs: {"enable_thinking": false}` (для vLLM не проверено), `off` — ничего. `llama3.1:8b` принимает `reasoning_effort` без ошибки.

Выбор модели проверен реальным тестом `test_real_llm_extraction`: RU и KZ реплики, самоназначение, поручение без исполнителя, инъекция в транскрипте.

| Модель | Время | Результат |
| --- | --- | --- |
| `qwen3:8b` | 18–22 с | ✅ «до пятницы» → 2026-09-25 (встреча в среду 23-го), самоназначение, поручение без исполнителя с `assignee=null`, инъекция проигнорирована |
| `llama3.1:8b` | 8–16 с | ❌ «до пятницы» → 2026-09-30, поручение без исполнителя пропущено |

**Рекомендация: `LLM_MODEL=qwen3:8b`** (внесено в `.env.example`). Модели `*:cloud` клиент отклоняет (`LLM_FORBIDDEN_ENDPOINT`).

## 6. Env AI-части

Уже в `config.py` / `.env.example`: `MODELS_DIR`, `ASR_*`, `DIARIZATION_CONFIG_PATH`, `SEGMENTATION_MODEL_PATH`, `EMBEDDING_MODEL_PATH`, `TORCH_DEVICE`, `VOICE_*`, `ALIGN_*`, `LLM_*`, `LLM_THINKING_CONTROL`, `TASK_LOW_CONFIDENCE`.

Новые переменные читаются через `os.environ` в AI-модулях. Backend переносит их в `.env.example` (в `config.py` по желанию):

| Переменная | Дефолт | Назначение |
| --- | --- | --- |
| `ASR_MULTILINGUAL` | `true` | Разрешает переопределение языка (при `ASR_LANGUAGE` пустом) |
| `ASR_REFINE_LANGUAGES` | `true` | Язык каждого VAD-сегмента определяется заново среди `ASR_ALLOWED_LANGUAGES`, сегмент перераспознаётся при несовпадении. Нужно для RU/KZ-совещаний; стоит около ×1.8 времени ASR |
| `ASR_ALLOWED_LANGUAGES` | `ru,kk,en` | Разрешённые языки при переопределении |
| `ASR_LANGUAGE_MIN_PROB` | `0.5` | Минимальная вероятность (перенормированная по разрешённым языкам) |
| `ASR_VAD_FILTER` | `true` | Silero VAD внутри faster-whisper, снижает галлюцинации на тишине |
| `DIARIZATION_MAX_SPEAKERS` | пусто | Необязательная верхняя граница числа спикеров |
| `VOICE_CLIPPING_REVIEW_RATIO` | `0.001` | Доля клиппинга в образце → `needs_review` |
| `VOICE_CLIPPING_REJECT_RATIO` | `0.02` | Доля клиппинга → отклонение образца |

Live preview (`LIVE_ASR_*`, `LIVE_PREVIEW_*`) — таблица в `docs/LIVE_AI_CONTRACT.md` § Env.

`.env.example` обновлён backend-агентом: `LLM_MODEL=qwen3:8b`, новые переменные выше. Пустое значение любой из них (или нечисловое) означает дефолт; `DIARIZATION_MAX_SPEAKERS=` — без ограничения.

Compose (сделано backend-агентом, проверено по файлам, но не запуском): `./models:/models:ro`, `MODELS_DIR=/models`, сеть без egress. На macOS использовать `docker-compose.host-llm.yml` (Ollama на хосте с Metal, `LLM_BASE_URL=http://host.docker.internal:11434/v1`; адрес Docker Desktop приватный, проверка endpoint его пропускает).

## 7. Тесты и smoke

```bash
cd backend
.venv/bin/python -m pytest tests/ai -m "not real_models and not real_llm"   # детерминированные, без моделей
MODELS_DIR=../models ASR_COMPUTE_TYPE=int8 LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen3:8b \
  .venv/bin/python -m pytest tests/ai/test_real_models.py -s                # реальные веса и LLM
MODELS_DIR=../models ASR_COMPUTE_TYPE=int8 LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen3:8b \
  .venv/bin/python ../scripts/ai/smoke.py tests/fixtures/ai/meeting_ru_kk.wav \
    --enroll "Иванова Анна=tests/fixtures/ai/enroll_ru_milena.wav" \
    --enroll "Ахметова Дана=tests/fixtures/ai/enroll_kk_aru.wav" --date 2026-09-23T10:00 --tz Asia/Almaty
python3 ../scripts/ai/make_fixtures.py    # пересоздать фикстуры (macOS say + ffmpeg)
```

Фикстуры в `backend/tests/fixtures/ai/` **смоделированы** голосами macOS TTS (Milena ru_RU, Aru kk_KZ, Milena с поднятым тоном как незарегистрированный голос). Это не живые люди. Образцы для регистрации и записи совещания содержат разный текст. `manifest.json` хранит эталонный текст, язык и интервалы.

## 8. Измерения (реальные веса, CPU M4 Pro)

| Операция | Аудио | Время |
| --- | --- | --- |
| Загрузка ASR large-v3 int8 | — | 4.1–4.3 с |
| Загрузка WeSpeaker | — | 2–3 с |
| ASR без переопределения языка | 31.4 с | 28–31 с (≈ ×1.0 реального времени; `ASR_CPU_THREADS=10` не ускорил) |
| ASR с переопределением языка | 31.4 с | 73–88 с (≈ ×2.5) |
| ASR, казахский | 8.3 с | 15 с (25 с с переопределением) |
| Регистрация голоса | 21–23 с | 0.2–2.4 с |
| Векторы спикеров | 31.4 с | 0.2 с |
| LLM: поручения + саммари, `qwen3:8b` | 7–8 реплик | 18–22 с |

Для записи 30 мин ожидаем ≈ 75 мин ASR с переопределением языка или ≈ 30 мин без него. Это главное ограничение CPU-режима. Варианты: GPU-хост; `ASR_REFINE_LANGUAGES=false` для одноязычных встреч; `large-v3-turbo`. Последнее — это замена модели, её надо согласовать и проверить на KZ.

Качество ASR на фикстурах (CER — доля ошибочных символов):

| Фрагмент | Без переопределения | С переопределением |
| --- | --- | --- |
| RU-реплики | 0.00 | 0.00 |
| KZ-реплика внутри RU-совещания | транслитерация русскими буквами («Салеметсиздерми…») | **0.00**, определена как `kk` |
| Смешанная реплика «финансовый отчётты жұмаға дейін» | «финансовый отчет жумага дейн…» | 0.15 («Синан Софи өтчу ұтты…») |
| Короткая KZ-реплика 1.9 с «Презентацияны кім жасайды?» | «тымжасайды» | 0.09, осталась `ru` |
| Отдельный KZ-файл | 0.02 | 0.02 |
| Смешанный файл (KZ + русские слова) | 0.04 | 0.04 |

Причина: `multilingual=True` в faster-whisper 1.1.1 определяет язык по 30-секундному окну, а не по реплике. Поэтому добавлено переопределение по сегментам. Переключение языка внутри одной реплики модель по-прежнему передаёт плохо.

Голосовые векторы (WeSpeaker, интервалы спикеров из эталона, без диаризации):

| Случай | similarity / 2-й | Решение |
| --- | --- | --- |
| Анна (регистрация: другой текст) | 0.966 / 0.454 | Анна |
| Дана | 0.943 / 0.390 | Дана |
| Незарегистрированный голос | 0.409 / 0.245 | Неизвестный (`below_threshold`) |
| Один кандидат, чужой голос | 0.390 | Неизвестный |
| Образец 2.3 с | — | отклонён `too_short` |
| Тишина | — | отклонён `silence` |

У синтетических голосов разброс меньше, чем у живых. Пороги 0.55 / 0.08 надо калибровать на реальных записях.

## 9. Не проверено / блокеры

1. **Диаризация pyannote 3.1 не запускалась**: gated-веса не скачаны, `HF_TOKEN` нет. Сборка пайплайна в `ml.py` из локальных `Model`, этап `diarizing`, overlap на реальной модели, полный `process_recording` и `test_full_process_recording` / `test_diarization_overlap_fixture` в итоге пропущены с явной причиной. Нужно принять условия и выполнить команду из §3, затем `check_models.py` и `pytest tests/ai/test_real_models.py`.
2. Живые записи RU/KZ/смешанной речи разных людей с согласием отсутствуют. Для калибровки порогов и оценки качества нужны 3–5 человек: образец голоса 20–30 с и отдельная запись совещания 3–5 мин с эталонной разметкой.
3. Docker (python:3.11-slim, linux/arm64, CPU-колёса torch): backend-агент собрал образ с этим `requirements-ml.txt`, `pip check` чистый, импорты `app.ai_pipeline`/pyannote/faster-whisper работают, 78 тестов без моделей (`tests/api` + `tests/ai`) проходят внутри образа. Модели и LLM в контейнере не запускались.
4. MPS для pyannote, CUDA и vLLM не проверялись.
5. На этой машине свободно около 11 GB диска (на старте было 24 GB, место уходило параллельно). Этого хватает на gated-модели (≈ 10 MB), но мало для дополнительных LLM или образов.

## 10. Live preview (постепенная транскрибация)

Интерфейс: `docs/LIVE_AI_CONTRACT.md` (1.1). Финальная обработка не менялась: large-v3, диаризация, идентификация, LLM.

```bash
cd backend
# live-модель (однократно, с интернетом): ../scripts/ai/prepare_models.py --only live_asr
MODELS_DIR=../models ASR_COMPUTE_TYPE=int8 LIVE_ASR_MODEL_PATH=faster-whisper-large-v3-turbo LIVE_ASR_CPU_THREADS=8 \
  .venv/bin/python ../scripts/ai/live_simulate.py tests/fixtures/ai/live_long.wav tests/fixtures/ai/meeting_ru_kk.wav \
  --realtime --language auto [--final] [--json out.json]
MODELS_DIR=../models ASR_COMPUTE_TYPE=int8 LIVE_ASR_MODEL_PATH=faster-whisper-large-v3-turbo LIVE_ASR_CPU_THREADS=8 \
  .venv/bin/python -m pytest tests/ai/test_live_real.py -s
.venv/bin/python -m pytest tests/ai/test_live_merge.py      # без моделей: callback, backlog, retry, язык, совместимость
python3 ../scripts/ai/make_live_fixture.py                  # пересоздать live_long.wav (macOS say)
```

Симулятор кодирует WAV в один WebM/Opus-контейнер (кластеры по 1 с) и режет байты на части по 2 с. С `--realtime` отдельный поток «микрофона» добавляет части по настенным часам **независимо от ASR**. Потребитель ведёт себя как backend: снимок-префикс → `transcribe_preview(on_update=...)` → сразу снова при `has_pending_audio`, пауза при `retry_after_ms`, иначе ждёт чанк. Метрики: время до первого текста, ASR на окно, аудио/ASR-секунда, лаг, задержка «конец фразы → первая публикация / финальная», CER к эталону.

Фикстура `live_long.wav` (42 с, TTS): «буду / не буду», «барамын / бармаймын», одна и та же фраза дважды, пауза 6 с, KZ и смешанные реплики, длинное предложение через границу окна. Ниже она склеена с `meeting_ru_kk.wav` (всего 73.7 с).

### Почему окно не уменьшает задержку на CPU

faster-whisper кодирует блок 30 с при любой длине окна. Стоимость одного окна (CPU M4 Pro, int8, beam 5):

| Модель | Потоки | Язык задан | auto штатно (2 прохода энкодера) | auto, один проход (новое) |
| --- | --- | --- | --- | --- |
| large-v3 | 4 (дефолт) | 6.5–6.7 с | 11.4–12.3 с | — |
| large-v3-turbo | 4 | 4.5–5.2 с | 8.7–9.6 с | 4.8–5.4 с |
| large-v3-turbo | 8 | 3.2–3.5 с | — | 5.2–5.6 с (с детекцией) |
| large-v3-turbo | 12 | 3.0–3.9 с | — | 4.9–5.7 с |

Одинаково для окон 6, 8, 10 с. 24 с дороже на 1–1.5 с. beam 1 почти не ускоряет. Значит, окно 6–10 с не даёт выигрыша, а частые вызовы только увеличивают нагрузку. Задержку задаёт стоимость окна: пока идёт окно, накапливается следующее.

### Realtime-симуляция, 73.7 с RU/KZ/mixed, `--language auto`

| Конфигурация | Первый текст (стена) | ASR на окно, среднее / макс | Аудио на 1 с ASR | Макс. лаг | Фраза → первая публикация | CER |
| --- | --- | --- | --- | --- | --- | --- |
| Было: large-v3, окно 24, 3 окна/вызов, выдача в конце вызова¹ | 13.2 с | 9.6 / 11.0 с | 0.96 | 9.3 с | 7–19 с | 0.112 |
| **turbo, 8 потоков, окно 12 (рекомендуется)** | **7.9 с** | 3.9 / 6.5 с | **1.12** | 5.6 с | **0–11 с, обычно 1–6 с** | **0.051** |
| turbo, 8 потоков, окно 8 | 7.9 с | 3.6 / 6.1 с | 1.13 | 6.8 с | 2–10 с | 0.049 |
| large-v3 (без turbo), 8 потоков, окно 12 | 10.9 с | 5.9 / 7.0 с | 1.03 | 6.0 с | 5–12 с | 0.158 |
| turbo, окно 12, язык встречи `kk` (kk + mixed + тишина 5 с, 32 с) | 8.0 с | 3.0 / 3.2 с | 1.34 | 4.6 с | 5–8 с | 0.111 |

¹ Старые настройки, но уже новый код (callback после окна, однопроходная детекция языка, без «дыр»). Прежний путь был не быстрее этого. Финальная публикация фразы обычно на 3–8 с позже первой: у живого края слова сначала черновые.

Что проверено:
- Публикация после каждого окна до распознавания следующего; backlog дорабатывается из того же снимка без новых чанков; тишина не создаёт реплик и не вызывает ASR (VAD); дублей на границах окон нет; фраза «Коллеги, начинаем планёрку», сказанная дважды, сохраняется дважды; RU-отрицания «я буду / я не буду готовить», «а Марат не будет» сохраняются (CER 0.000); KZ «бармаймын» сохраняется (0.034); выбранный язык встречи передаётся как есть; отмена между окнами; финальная обработка берёт слот `asr` (large-v3), а не live-модель (`test_finalization_uses_full_pipeline`).
- Потеря слов, найденная замером: без п.6 контракта («дыры») казахская фраза «Мен бүгін есепті дайындаймын» внутри «русского» окна **исчезала целиком** (turbo, CER 0.116). С повторным распознаванием дыр она восстанавливается (0.25 CER), общий CER 0.051.
- Однопроходная детекция языка даёт тот же текст, что штатный auto faster-whisper, на 5 окнах из 5 (проверено сравнением) и на ~45 % быстрее.

Ограничения (честно):
- Запас скорости turbo на CPU всего ×1.12, и только на M4 Pro с 8 потоками. В Docker с меньшим числом CPU или на более слабом CPU preview будет отставать: лаг начнёт расти. large-v3 без turbo идёт на грани (×1.03). **Это не «realtime-гарантия».**
- Смешанные реплики в preview остаются неточными («финансовый отчётты» → «синан софи өтчетті», CER 0.2–0.45). Короткие KZ-реплики иногда транслитерируются. Финальный large-v3 с `ASR_REFINE_LANGUAGES` это исправляет (§8).
- Последняя фраза перед stop может остаться черновой и неточной (например, «совещание окончено» → «советую»); хвост короче 3 с ждёт аудио. Её заменяет финальный транскрипт.
- Все цифры на синтетических TTS-голосах (Milena, Aru), по одной записи на конфигурацию. Это не оценка качества живых встреч; разброс между прогонами ±1–2 с по задержке.
- Не проверено: живой микрофон и настоящие чанки MediaRecorder в Chrome/Safari, Docker-лимиты CPU, длинные встречи (> 2 мин) в realtime.

### Следующий шаг, если CPU не хватает

Не мигрировать проект вслепую. Кандидат для live — локальный runtime с аппаратным ускорением, только для слота `live_asr`:
- Mac: whisper.cpp (Metal) или MLX Whisper на **хосте**. В Docker Desktop на macOS Metal недоступен, поэтому нужен отдельный локальный сервис без выхода в сеть.
- Linux + NVIDIA: тот же faster-whisper, `LIVE_ASR_DEVICE=cuda`, `LIVE_ASR_COMPUTE_TYPE=float16`. Код не меняется, но это не проверялось.
Сначала замерить `live_simulate.py` с этим runtime на тех же фикстурах, затем решать.

## Профили STT и настройки встречи

`ASR_PROFILE=refined` по умолчанию сохраняет прежний алгоритм.
`ASR_PROFILE=standard` — профиль по `adilet/transcribe.py`: контекст предыдущего
текста включён, multilingual/refine отключены. `refined` сохраняет прежний алгоритм.
Настройки встречи `asr_language` (auto/ru/kk), `asr_profile` переопределяют env и
передаются и в финальную обработку, и в preview. Принудительный язык не переопределяется
повторным детектированием. Для preview refined по-прежнему учитывает
`LIVE_PREVIEW_REFINE_LANGUAGES`, для полного файла — `ASR_REFINE_LANGUAGES`.
Из live-preview убран чёрный список фраз: настоящие «Спасибо за внимание» и
«Продолжение следует» больше не удаляются только из-за их текста. VAD и фильтрация
по no_speech_prob сохранены.

Миграция 0004 добавляет настройки встречи и недостающее preview_state для ранних
установок 0003. Она не удаляет записи. Подробный контракт — docs/API_CONTRACT.md.

Сравнение 2026-09-23, одна модель CPU/int8, fixture meeting_ru_kk (31,38 с):

| Профиль | Время | CER |
| --- | --- | --- |
| standard | 36,64 с | 0,0532 |
| refined | 86,02 с | 0,0319 |

На коротком mixed-фрагменте оба профиля ранее дали одинаковый текст (CER 0,055).
Поэтому refined остаётся начальным профилем, standard доступен для сравнения и
работы с явно заданным языком. Это синтетические фикстуры, не оценка живых встреч.
