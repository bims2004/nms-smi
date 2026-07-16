FROM python:3.11-slim

WORKDIR /opt/web

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["gunicorn", "nmsweb.wsgi:application", "-b", "0.0.0.0:8000"]
