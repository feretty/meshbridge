# MeshBridge: Telegram ↔ Meshtastic Bridge

MeshBridge — это сервис, который:
- Пересылает групповые сообщения из Telegram-групп в каналы Meshtastic и обратно без использования mqtt
- Позволяет писать Direct сообщения нодам из телеграм и пересылает ответы в личный чат с ботом
- Позволяет управлять нодой через команды в Telegram и только из авторизованного чата с ботом
- Отслеживает новые ноды и низкий заряд батарей избранных нод
- Работает 24/7 в фоне через Docker

---

## Требования

- Стационарная нода (тестировалось устройство T-Beam с прошивкой 2.7.11)
- Сервер или ПК (тестировалось на Ubuntu 24.04.3 LTS)
- USB-кабель для подключения ноды к серверу
- Telegram-бот (создаётся за 1 минуту)
- Два Telegram-чата: один для канала 0 (публичный), второй — для канала 1 (приватный)

---

## Шаг 1: Подключите ноду к серверу

1. Подключите T-Beam к серверу через USB
2. Настройте последовательный порт на мештастике
- Serial - Enabled
- Baud - 38400
- Mode - Text Message

2. Найдите имя последовательного порта:
   ```bash
   ls /dev/ttyACM*
   ```
   → Обычно это `/dev/ttyACM0`

3. Добавьте текущего пользователя к dialout:
   ```bash
   sudo usermod -aG dialout $USER
   ```
   → Перезагрузите сервер

---

## Шаг 2: Создайте Telegram-бота

1. Напишите в Telegram @BotFather
2. Выполните:
   ```
   /newbot
   ```
3. Следуйте инструкциям, получите токен вида:
   ```
   123456789:ABCdefGhIJKlmNoPQRstUVwXY (TELEGRAM_BOT_TOKEN в файле .env)
   ```
4. В настройках бота отключите GroupPrivacy, чтобы бот мог читать отправленные в чатах телеграм сообщения.

---

## Шаг 3: Создайте Telegram-чаты

1. Чат 1 (публичный) — для публичного канала мештастика (MESH_CHANNEL_PUBLIC в файле .env)
   - Создайте группу → запомните Chat ID (CHAT_ID_PUBLIC в файле .env)
2. Чат 2 (приватный) — для приватного канала мештастика (MESH_CHANNEL_PRIVATE в файле .env)
   - Создайте группу → запомните Chat ID (CHAT_ID_PRIVATE в файле .env)

> Чтобы узнать Chat ID, напишите в чат с ботом и выполните:
> ```bash
> curl "https://api.telegram.org/bot<ТОКЕН>/getUpdates"
> ```
> → Ищите `"id": -100...`

3. Напишите боту в ЛС, чтобы создать с ним личный чат для команд и отправки и получения директ сообщений.

> Чтобы узнать айди личного можно воспользоваться ботом @userinfobot и запомните данный номер (ADMIN_USER_ID в файле .env)

3. Добавьте бота в публичный и приватный групповые чаты телеграм

---

## Шаг 4: Настройте сервер

### 1. Установите зависимости
```bash
sudo apt update
sudo apt install -y docker.io docker-compose python3-pip
sudo systemctl enable docker
```

### 2. Создайте папку проекта
```bash
mkdir ~/meshbridge && cd ~/meshbridge
```

---

## Шаг 5: Подготовьте файлы

### `bot.py`
→ Скачайте или скопируйте содержимое с репозитория

### `requirements.txt`
```txt
meshtastic==2.7.3
python-telegram-bot==21.4
nest-asyncio
pypubsub
```

### `Dockerfile`
```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN touch meshbridge.log && chmod 666 meshbridge.log

CMD ["python", "bot.py"]
```

### `docker-compose.yml`
```yaml
services:
  meshbridge:
    build: .
    env_file:
      - .env
    devices:
      - "/dev/ttyACM0:/dev/ttyACM0"
    volumes:
      - "./:/app"
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### `.env` (пример)
```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstUVwXY
CHAT_ID_PUBLIC=-1001234567890
CHAT_ID_PRIVATE=-1009876543210
MESH_CHANNEL_PUBLIC=0
MESH_CHANNEL_PRIVATE=1
ADMIN_USER_ID=123456789
```
> номера каналов мештастика можно посмотреть в настройках ноды в разделе каналы
> обычно публичный канал мештастика - 0. Дополнительно можно создавать Secondary каналы и один из них назначить приватным для телеграм
> пока можно пересылать из 2х каналов ТГ в 2 канала Мештастика
---

## Шаг 6: Запустите сервис

```bash
cd ~/meshbridge
docker compose build
docker compose up -d
```

Проверьте логи:
```bash
tail -f meshbridge.log
```

При внесении изменений в рабочие файлы докер необходимо пересобрать:

```bash
docker compose down
docker system prune -af --volumes
docker compose up -d
```

---

## Шаг 7: Используйте команды

Напишите боту в личные сообщения:

`/help` чтобы получить список всех команд

Для отправки личного сообщения удаленная нода должна быть в базе. Проверить это и узнать ее короткое имя можно командой из списка /help.

Для отправки сообщения напишите @КОРОТКОЕ_ИМЯ тест
Важно соблюдать регистр символов в имени.

---

## Структура проекта

```
~/meshbridge/
├── bot.py                 # Основной скрипт
├── requirements.txt       # Зависимости Python
├── Dockerfile             # Сборка образа
├── docker-compose.yml     # Запуск контейнера
├── .env                   # Секреты (не шарить)
├── node_names.json        # Кэш имён нод (сохраняется между перезагрузками)
├── favorites.json         # Список избранных нод формируется вручную. Нужен для получения сообщений о низком уровне батарей избранных нод
└── meshbridge.log         # Лог работы
```

---

## Безопасность

- Все команды работают только в личном чате с ботом
- Приватные сообщения (`@Имя`) не видны в группах
- Уведомления о низком заряде приходят только админу

---

## Устранение неполадок

| Проблема | Решение |
|---------|--------|
| `Permission denied: '/dev/ttyACM0'` | Выполните `sudo usermod -aG dialout $USER` и перезагрузитесь |
| Нет имён нод — только HEX | Обновите базу имен. Обновление Базы имен происходит раз в пол часа, либо вручную через команду в /help |
| Команды не работают | Убедитесь, что `ADMIN_USER_ID` — это ваш Telegram User ID |

---

## Готово!

Теперь у вас есть полноценный шлюз между Telegram и Meshtastic, который:
- Работает автономно
- Не зависит от BLE
- Позволяет управлять сетью из любого места

Удачи в эксплуатации!

---

> Совет: добавьте `node_names.json` и `favorites.json` в резервную копию — они сохраняют историю имён и настройки между перезапусками.
