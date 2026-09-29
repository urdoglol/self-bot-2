# Dockerfile — repo root
FROM python:3.11-slim

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev libnacl-dev \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
COPY modifyself/ ./modifyself/
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "selfbot.py"]
