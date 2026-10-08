FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY rentbot ./rentbot
RUN mkdir /app/data && useradd -u 10001 -r bot && chown -R bot:bot /app
USER bot
CMD ["python", "-m", "rentbot", "run"]
