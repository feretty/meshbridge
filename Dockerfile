FROM python:3.12-slim

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Копируем зависимости и код
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Создаём файл лога и даём права
RUN touch meshbridge.log && chmod 666 meshbridge.log

# Запуск
CMD ["python", "bot.py"]
