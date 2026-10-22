# 공개 데모용 컨테이너 (Render 블루프린트 `render.yaml` 가 사용). 키 없이 DEMO 모드로 뜬다.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
# Read-only connection diagnosis emits categories only; never credentials or driver messages.
# Render 는 PORT 를 넘긴다. 로컬에서는 8501.
CMD ["sh", "-c", "python scripts/check_team_storage.py; exec streamlit run app.py --server.port=${PORT:-8501} --server.address=0.0.0.0 --server.headless=true --browser.gatherUsageStats=false"]
