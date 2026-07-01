FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH="/app"
COPY requirements.txt .

RUN pip install uv && uv pip install --system --no-cache -r requirements.txt

COPY src/data/faiss_index/ /app/data/faiss_index/

COPY src/ /app/src/



EXPOSE 8080

CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8080"]