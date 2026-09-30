FROM python:3.11-slim

# 不写 .pyc、日志直出，便于容器内排障
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# 先装依赖，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷应用代码、初始化脚本与启动脚本
COPY app ./app
COPY db ./db
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

EXPOSE 8080

# 等待数据库可达 -> 应用启动时幂等执行 db/schema.sql -> 运行 uvicorn
ENTRYPOINT ["./entrypoint.sh"]
