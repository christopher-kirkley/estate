FROM python:3.12-slim
# Time zone database for zoneinfo (the slim image may not ship one).
RUN pip install --no-cache-dir tzdata
WORKDIR /srv
COPY app/ /srv/app/
ENV CONFIG=/config/config.json DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8787
CMD ["python", "/srv/app/server.py"]
