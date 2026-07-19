FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py .
COPY app.py .

VOLUME /data
EXPOSE 20129

CMD ["python", "app.py"]
