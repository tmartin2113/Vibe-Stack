# Use the pre-built paddlecloud image. Python 3.7, paddlepaddle + paddleocr
# 2.6 already installed with ABI-compatible native deps. This avoids the pip
# install ABI hell we hit on python:3.11-slim bases, where paddlepaddle 2.x
# wheels from PyPI crash at import with double-free corruption on modern
# glibc / protobuf / numpy combos.
FROM paddlecloud/paddleocr:2.6-cpu-latest

# The base image keeps PaddleOCR as a source checkout at /home/PaddleOCR
# (top-level paddleocr.py relies on the CWD being in sys.path). Setting
# PYTHONPATH explicitly makes it importable from any working directory,
# including /app where we put server.py.
ENV PYTHONPATH=/home/PaddleOCR

WORKDIR /app

# Everything paddleocr-related is already in the base image. We only need to
# install the FastAPI server layer on top. Pinned to versions that still
# support Python 3.7 (the base image's Python) — FastAPI 0.95.2 is the last
# minor line on 3.7 and pulls pydantic v1 transitively, which matches the
# BaseModel usage in server.py.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Create a non-root user and switch to it before downloading models so the
# model cache lives under the non-root user's $HOME and the runtime process
# never executes as root (Trivy DS-0002).
RUN useradd --create-home --uid 10001 ocr \
    && chown -R ocr:ocr /app

USER ocr
ENV HOME=/home/ocr

# Pre-download OCR + structure models into /home/ocr/.paddleocr/whl so first
# request doesn't pay the download latency. Runs as the ocr user so the cache
# lands in the correct $HOME and is owned by the runtime user.
RUN python3 -c "from paddleocr import PaddleOCR, PPStructure; \
    PaddleOCR(use_angle_cls=True, lang='en', show_log=False); \
    PPStructure(show_log=False, layout=True)"

EXPOSE 8868

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8868"]
