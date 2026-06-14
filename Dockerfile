FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先拷依赖文件（利用缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷全部代码
COPY . .

# 云托管会注入 PORT；你 app.py 里要用 os.environ.get("PORT",...)
EXPOSE 5000

# 用 python 直接跑（小项目够用）
CMD ["sh", "-c", "python app.py"]