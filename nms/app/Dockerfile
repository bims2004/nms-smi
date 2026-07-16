# Python 3.11 (pysnmp 4.x butuh asyncore yang dihapus di 3.12)
FROM python:3.11-slim

WORKDIR /opt/nms

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nms ./nms

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "nms.collector"]
