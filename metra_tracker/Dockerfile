FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/metra
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

CMD ["python", "-m", "app"]
