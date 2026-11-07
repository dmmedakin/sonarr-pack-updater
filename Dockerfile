# Only the Python standard library is used, so the image is just the script.
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/dmmedakin/sonarr-pack-updater" \
      org.opencontainers.image.description="Grabs updated season packs from Russian trackers that Sonarr rejects because part of the season is already on disk" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    STATE_FILE=/data/state.json HEARTBEAT_FILE=/tmp/heartbeat

COPY pack_updater.py /app/pack_updater.py
VOLUME /data

# Healthy while passes keep starting: the heartbeat is refreshed at the start of
# every pass, so it must be younger than the interval plus time for a long pass.
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
  CMD python -c "import os,time,sys; h=float(os.environ.get('INTERVAL_HOURS','3')); t=float(open(os.environ['HEARTBEAT_FILE']).read()); sys.exit(0 if time.time()-t < (h+3)*3600 else 1)"

CMD ["python", "/app/pack_updater.py"]
