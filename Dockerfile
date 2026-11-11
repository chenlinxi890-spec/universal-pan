FROM docker.m.daocloud.io/library/python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    p7zip-full curl \
 && rm -rf /var/lib/apt/lists/*
# 注：不装 unrar（它在 non-free 里）；p7zip-full 自带 RAR 解压支持

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static

ENV UNIPAN_DATA=/data
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
