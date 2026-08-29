FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目
COPY . .

EXPOSE 8000

# 默认启动命令（docker-compose 会覆盖为先 migrate 再 runserver）
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
