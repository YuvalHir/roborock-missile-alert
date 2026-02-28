FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir python-roborock aiohttp pyyaml

COPY mamad_roborock.py alert_monitor.py room_scheduler.py \
     vacuum_controller.py notifications.py ./

# State file + logs + config.yaml live in the mounted volume
WORKDIR /data

ENTRYPOINT ["python", "/app/mamad_roborock.py"]
CMD ["--config", "/data/config.yaml"]
