ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.11-alpine3.19
FROM $BUILD_FROM

RUN apk add --no-cache tzdata \
 && cp /usr/share/zoneinfo/UTC /etc/localtime \
 && echo "UTC" > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dradis/ ./dradis/

CMD ["python3", "-u", "dradis/main.py"]
