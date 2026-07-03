FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# To'g'ri Linux buyrug'i: bot.py fonda (nohup bilan) mustaqil ketadi, main.py esa asosiy oqimda yonadi!
CMD nohup python bot.py > bot.log 2>&1 & uvicorn main:app --host 0.0.0.0 --port 8080
