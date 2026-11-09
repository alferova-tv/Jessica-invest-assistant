# Работа над кодом Jessica

Установка окружения описана в [README](README.md#установка).
Команды ниже выполняются из корня репозитория, если не указано иное.

## Проверка кода

Тесты работают на синтетических данных и не обращаются к брокерам или Telegram.
Из корня репозитория:

```bash
.venv/bin/python -m unittest discover -s tests

.venv/bin/python -m unittest discover -s research/tests
```

Проверка отдельного приложения «Портфель»:

```bash
cd portfel
../.venv/bin/python -m unittest discover -s tests
```

## Запуск на копии данных

Из корня проекта можно проверить уведомления на отдельной копии данных.
`JESSICA_HOME` должна указывать на твою рабочую папку данных:

```bash
cp -R "$JESSICA_HOME" /tmp/jessica-test

JESSICA_HOME=/tmp/jessica-test .venv/bin/python -m jessica check --dry-run
```
