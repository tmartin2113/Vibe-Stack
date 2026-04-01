FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Pre-download models at build time so startup is fast
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_angle_cls=True, lang='en', show_log=False)"
RUN python -c "from paddleocr import PPStructure; PPStructure(show_log=False, layout=True)"

EXPOSE 8868

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8868"]
