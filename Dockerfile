FROM python:3.13-slim

WORKDIR /app

# 先装依赖，利用 Docker 层缓存（改代码不重新装依赖）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目文件
COPY . .

# 数据目录（运行时挂载成卷，见 docker-compose.yml）
RUN mkdir -p /app/toyagent_log

# 容器内必须监听 0.0.0.0，否则外部无法访问
ENV DSH_WEB_HOST=0.0.0.0
ENV DSH_WEB_PORT=8000

EXPOSE 8000

CMD ["python", "app.py"]
