FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# mega.py purono tenacity (<6) chay jeta Python 3.12-e broken —
# notun tenacity force install (mega.py etateo chole, verified).
RUN pip install --no-cache-dir --upgrade --no-deps tenacity

COPY bot.py ./

# Dokploy / VPS-er temp dir
ENV TMPDIR=/tmp

CMD ["python", "bot.py"]
